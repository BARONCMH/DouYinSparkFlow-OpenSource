"""Persistent SMTP outbox used by the panel notification worker.

The queue stores recipient, subject and rendered status text only. SMTP
credentials stay in environment variables and are never written to the
outbox or included in error responses.
"""

from __future__ import annotations

import json
import os
import re
import smtplib
import ssl
import tempfile
import threading
import time
from email.message import EmailMessage
from email.utils import formataddr, parseaddr
from pathlib import Path
from uuid import uuid4


_EMAIL_RE = re.compile(r"^[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+$")


def valid_email(value: str) -> bool:
    text = str(value or "").strip()
    if len(text) > 254 or "\r" in text or "\n" in text:
        return False
    _name, address = parseaddr(text)
    return bool(address and _EMAIL_RE.fullmatch(address))


def smtp_ready() -> bool:
    return bool(str(os.getenv("SMTP_HOST") or "").strip())


class EmailOutbox:
    def __init__(self, path: str | Path, *, max_items: int = 1000) -> None:
        self.path = Path(path)
        self.max_items = max(50, int(max_items))
        self._lock = threading.RLock()

    def _read(self) -> dict:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {"items": []}
        except (FileNotFoundError, OSError, ValueError, TypeError):
            return {"items": []}

    def _write(self, value: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=self.path.name + ".", suffix=".tmp", dir=str(self.path.parent))
        try:
            os.chmod(name, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(value, ensure_ascii=False, indent=2))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, self.path)
        finally:
            try:
                os.unlink(name)
            except FileNotFoundError:
                pass

    def enqueue(self, recipient: str, subject: str, body: str, *, kind: str = "run") -> dict:
        recipient = str(recipient or "").strip()
        if not valid_email(recipient):
            raise ValueError("收件邮箱格式不正确")
        subject = str(subject or "").replace("\r", " ").replace("\n", " ").strip()[:180]
        body = str(body or "").replace("\x00", "")[:12000]
        now = int(time.time())
        item = {
            "id": uuid4().hex,
            "to": recipient,
            "subject": subject or "DouYinSparkFlow 通知",
            "body": body,
            "kind": str(kind or "run")[:40],
            "status": "pending",
            "attempts": 0,
            "created_at": now,
            "next_attempt_at": now,
            "last_error": "",
            "sent_at": 0,
        }
        with self._lock:
            data = self._read()
            items = [x for x in data.get("items", []) if isinstance(x, dict)]
            items.insert(0, item)
            # Keep pending work and a bounded history.  Old failed entries are
            # retained long enough for an administrator to diagnose SMTP setup.
            items = items[: self.max_items]
            self._write({"items": items})
        return {"id": item["id"], "status": "queued"}

    def snapshot(self, limit: int = 100) -> list[dict]:
        with self._lock:
            items = [dict(x) for x in self._read().get("items", []) if isinstance(x, dict)]
        return items[: max(1, min(500, int(limit)))]

    def _update(self, item_id: str, fn) -> dict | None:
        with self._lock:
            data = self._read()
            items = [x for x in data.get("items", []) if isinstance(x, dict)]
            found = None
            for item in items:
                if str(item.get("id") or "") == str(item_id):
                    fn(item)
                    found = dict(item)
                    break
            self._write({"items": items[: self.max_items]})
            return found

    @staticmethod
    def _send(item: dict) -> None:
        host = str(os.getenv("SMTP_HOST") or "").strip()
        if not host:
            raise RuntimeError("SMTP 尚未配置")
        try:
            port = int(os.getenv("SMTP_PORT", "587") or 587)
        except ValueError:
            port = 587
        username = str(os.getenv("SMTP_USERNAME") or "").strip()
        password = str(os.getenv("SMTP_PASSWORD") or "")
        sender = str(os.getenv("SMTP_FROM") or username or "").strip()
        if not valid_email(sender):
            raise RuntimeError("SMTP_FROM 或 SMTP_USERNAME 不是有效邮箱")
        from_name = str(os.getenv("SMTP_FROM_NAME") or "DouYinSparkFlow").strip()[:80]
        message = EmailMessage()
        message["From"] = formataddr((from_name, sender))
        message["To"] = str(item["to"])
        message["Subject"] = str(item.get("subject") or "DouYinSparkFlow 通知")
        message.set_content(str(item.get("body") or ""))
        use_ssl = str(os.getenv("SMTP_USE_SSL", "0") or "").strip().lower() in {"1", "true", "yes", "on"}
        starttls = str(os.getenv("SMTP_STARTTLS", "1") or "").strip().lower() in {"1", "true", "yes", "on"}
        context = ssl.create_default_context()
        if use_ssl:
            with smtplib.SMTP_SSL(host, port, timeout=20, context=context) as client:
                if username:
                    client.login(username, password)
                client.send_message(message)
            return
        with smtplib.SMTP(host, port, timeout=20) as client:
            client.ehlo()
            if starttls:
                client.starttls(context=context)
                client.ehlo()
            if username:
                client.login(username, password)
            client.send_message(message)

    def process_once(self, *, max_items: int = 5) -> dict:
        now = int(time.time())
        selected = []
        with self._lock:
            data = self._read()
            items = [x for x in data.get("items", []) if isinstance(x, dict)]
            changed = False
            for item in items:
                if len(selected) >= max(1, int(max_items)):
                    break
                if item.get("status") not in {"pending", "retry"}:
                    continue
                if int(item.get("next_attempt_at") or 0) > now:
                    continue
                item["status"] = "sending"
                item["attempts"] = int(item.get("attempts") or 0) + 1
                changed = True
                selected.append(dict(item))
            if changed:
                self._write({"items": items[: self.max_items]})
        sent = failed = 0
        for item in selected:
            try:
                self._send(item)
            except Exception as error:
                failed += 1
                attempts = int(item.get("attempts") or 1)
                delay = min(3600, 30 * (2 ** min(attempts - 1, 6)))
                safe_error = type(error).__name__
                self._update(item["id"], lambda row, d=delay, e=safe_error: row.update({
                    "status": "failed" if attempts >= 6 else "retry",
                    "next_attempt_at": int(time.time()) + d,
                    "last_error": e,
                }))
            else:
                sent += 1
                self._update(item["id"], lambda row: row.update({
                    "status": "sent", "sent_at": int(time.time()), "last_error": ""
                }))
        return {"picked": len(selected), "sent": sent, "failed": failed}
