"""按账号保存浏览器登录状态快照。

快照只用于恢复当前账号自己的浏览器上下文，不会把账号数据写入代码仓库。
配置中的 Cookie 或指纹发生变化时，旧快照自动失效并回退到配置值。
"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import uuid


_LOCK = threading.RLock()


def _default_directory() -> Path:
    configured = str(os.getenv("DOUYIN_SESSION_DIR") or "").strip()
    if configured:
        return Path(configured).expanduser()
    app_data = str(os.getenv("APP_DATA_DIR") or "").strip()
    if app_data:
        return Path(app_data).expanduser() / "sessions"
    return Path(__file__).resolve().parent.parent / "logs" / "sessions"


class SessionStore:
    """一个抖音号对应一个原子替换的 storage_state 文件。"""

    def __init__(self, unique_id, cookies, fingerprint="", directory=None):
        account = str(unique_id or "").strip().upper()
        self.enabled = bool(account)
        self.directory = Path(directory or _default_directory())
        self.path = self.directory / (
            hashlib.sha256(account.encode("utf-8")).hexdigest() + ".json"
        )
        canonical = [
            {key: value for key, value in item.items() if key != "sameSite"}
            for item in (cookies or [])
            if isinstance(item, dict)
        ]
        canonical.sort(
            key=lambda item: (
                str(item.get("domain") or ""),
                str(item.get("path") or ""),
                str(item.get("name") or ""),
            )
        )
        self.source = hashlib.sha256(
            json.dumps([canonical, str(fingerprint or "")], sort_keys=True).encode(
                "utf-8"
            )
        ).hexdigest()
        self.revision = None

    @contextmanager
    def _locked(self):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with _LOCK:
            lock_path = self.path.with_suffix(".lock")
            with open(lock_path, "a+b") as lock:
                try:
                    if os.name == "posix":
                        import fcntl

                        os.chmod(lock.name, 0o600)
                        fcntl.flock(lock, fcntl.LOCK_EX)
                    else:
                        import msvcrt

                        if lock.tell() == 0:
                            lock.write(b"0")
                            lock.flush()
                        lock.seek(0)
                        msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
                    yield
                finally:
                    if os.name == "posix":
                        fcntl.flock(lock, fcntl.LOCK_UN)
                    else:
                        lock.seek(0)
                        msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)

    def _read(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("state"), dict):
                return data
        except (OSError, ValueError, AttributeError):
            pass
        return {}

    def load(self):
        if not self.enabled:
            return None
        with self._locked():
            data = self._read()
            self.revision = data.get("revision")
            if data.get("source") != self.source:
                return None
            return data.get("state")

    def save(self, state):
        if not self.enabled or not isinstance(state, dict):
            return False
        cookies = state.get("cookies") or []
        if not any(
            item.get("name") in {"sessionid", "sessionid_ss", "sid_tt"}
            and item.get("value")
            for item in cookies
            if isinstance(item, dict)
        ):
            return False
        with self._locked():
            current = self._read()
            # 防止并行任务用旧快照覆盖刚写入的新登录状态。
            if current.get("revision") != self.revision:
                return False
            revision = uuid.uuid4().hex
            fd, name = tempfile.mkstemp(prefix=".session-", dir=self.directory)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(
                        {
                            "source": self.source,
                            "revision": revision,
                            "state": state,
                        },
                        handle,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(name, self.path)
                try:
                    os.chmod(self.path, 0o600)
                except OSError:
                    pass
                self.revision = revision
            finally:
                if os.path.exists(name):
                    os.unlink(name)
        return True


def capture_state(context):
    """优先保存 IndexedDB；旧 Playwright 版本不支持时自动降级。"""
    try:
        return context.storage_state(indexed_db=True)
    except TypeError:
        return context.storage_state()
