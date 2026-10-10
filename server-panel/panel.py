#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DouYinSparkFlow 控制台（仅用标准库，依赖镜像内已有的 playwright / python-dotenv）

功能：
  * 配置消息模板、目标好友和账号，并手动运行发送任务
  * 抖音扫码登录，登录成功后自动把 Cookies 导出写入 .env
  * 手动立即执行一次，查看运行状态与日志
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import os
import queue
import re
import secrets
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from http import HTTPStatus
from http.cookies import SimpleCookie
try:
    import fcntl  # Linux 上用来避免并发启动多个发送进程；Windows 没有
except ImportError:  # pragma: no cover
    fcntl = None
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlparse, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from dotenv import dotenv_values

BASE_DIR = Path("/app")
# 配置目录是整目录挂载，/app/.env 是指向它的软链。
# 这样即使宿主机上用 sed -i 或编辑器保存导致文件被替换（inode 变了），容器里也能读到最新内容。
ENV_PATH = Path("/app/config/.env")
LINK_PATH = Path("/app/.env")
LOG_DIR = BASE_DIR / "logs"
DOWNLOAD_DIR = LOG_DIR / "app-downloads"
ANDROID_APK_PATH = DOWNLOAD_DIR / "DouYinSparkFlow.apk"
ANDROID_APK_SHA_PATH = DOWNLOAD_DIR / "DouYinSparkFlow.apk.sha256"
COOKIE_TOOL_EXE_PATH = DOWNLOAD_DIR / "Get-Douyin-Cookies.exe"
COOKIE_TOOL_SHA_PATH = DOWNLOAD_DIR / "Get-Douyin-Cookies.exe.sha256"
APP_LOG = LOG_DIR / "app.log"
RUN_LOG = LOG_DIR / "panel-run.log"
STATE_PATH = LOG_DIR / "panel-state.json"
SEND_LOG = LOG_DIR / "send-status.json"
SHOT_DIR = LOG_DIR / "send-shots"
SEND_LOCK_PATH = LOG_DIR / "send.lock"  # 跨进程互斥，防止并发重复发送
SCHEDULE_STATE_PATH = LOG_DIR / "schedule-state.json"
RELOGIN_PATH = LOG_DIR / "need-relogin.json"
FORCE_LOG = LOG_DIR / "force-actions.log"
SMS_PROBE_LOG = LOG_DIR / "sms-probe.log"  # 验证码页结构的诊断记录（不记输入的内容）
ABORT_PATH = LOG_DIR / "abort-run.json"
RESTART_FLAG = LOG_DIR / "restart-requested.json"
AUTO_AUTH_SECONDS = 20 * 60  # 一次授权的绝对上限（兜底），正常情况下面那个"没动静"就会先把它关掉
AUTH_IDLE_STOP = 3 * 60  # 秒：授权浏览器超过 3 分钟没人扫码/没操作，就自动关掉，别占着浏览器
# 发送记录：worker（tasks.py 里的 KEEP_RUNS_MAX）只保留最近 100 次，面板这边照这个上限给管理员看
SEND_STORE_MAX = 100
SEND_MAX_USER = 2  # 普通用户最多看自己名下最近 2 次
SEND_MAX_ADMIN = 100  # 管理员最多看最近 100 次
# 同时允许多少个"授权浏览器"（每个号一个独立会话，各扫各的）。
#
# 数字来自 2026-09-18 在容器里的真实测量（加载抖音页、量 PSS 真实占用）：
#   * 一个无头 Chromium ≈ 7 个进程、320-400MB；
#   * 整机 2G，系统 + 两个容器常驻约 500MB，空载可用约 1450MB。
# 所以能稳定并存的是 3 个（≈1000MB，还剩约 450MB 给发送任务）；第 4 个要等有人结束。
#
# 实测过的两条"看起来能省内存"的路子都是坑，别再试：
#   * 堆一堆 --disable-* 低内存参数：占用反而更高，而且浏览器关不干净（残留 690MB 不释放）；
#   * chromium-headless-shell：内存没降下来，还出现 goto 超时。
SESSION_COST_MB = 400  # 一个授权会话实测要吃的内存（加载抖音页 320-400MB，取上限）
TASK_RESERVE_MB = 380  # 有发送任务 / 检测在跑时，额外留这么多再开新会话，别把它们挤死
MAX_AUTH_SESSIONS = max(1, min(4, int(os.getenv("PANEL_MAX_AUTH_SESSIONS", "3") or 3)))
MIN_FREE_MB = 300  # 兜底阈值：可用内存低于这个数，任何新浏览器都不再开
AUTO_AUTH_INTERVAL = 30  # 秒：后台心跳每隔多久看一眼有没有账号需要重新扫码
# [本地增强] 秒：后台回收 send.lock 的间隔。
# 以前这把锁只在网页轮询 /api/status 时顺带释放（snapshot -> _reap），
# 页面一关就没人轮询，面板会把锁一直攥在手里，后续手动运行会被误判为已有任务。
LOCK_REAP_INTERVAL = 15
SCHEDULE_INTERVAL = 5  # 秒：检查到期账号并启动排队中的定时任务

CHECK_TIMEOUT = 120  # 秒：登录检测整体上限，超过就自动收尾，绝不允许一直卡着
STUCK_AFTER = 90  # 秒：授权浏览器超过这么久没有新画面，就认为卡住了
RUN_STUCK_AFTER = 180  # 秒：发送任务超过这么久没有新日志，就认为卡住了

PANEL_USERNAME = os.getenv("PANEL_USERNAME", "admin")
PANEL_PASSWORD = os.getenv("PANEL_PASSWORD", "")
PANEL_HOST = os.getenv("PANEL_HOST", "0.0.0.0")
PANEL_PORT = int(os.getenv("PANEL_PORT", "8080"))

USERS_PATH = Path("/app/config/panel-users.json")  # 每个账号自己的控制台登录名/密码
ADMIN_PATH = Path("/app/config/panel-admin.json")  # 管理员自己改过的登录密码
SECRET_PATH = Path("/app/config/panel-secret")  # 没设 PANEL_SECRET 时把会话密钥固化在这
ACCOUNTS_PATH = Path("/app/config/accounts.json")  # 抖音号 + Cookie（Cookie 加密存放）
LOGIN_FAILS_PATH = LOG_DIR / "login-fails.json"  # 登录失败计数（面板重启也不忘）
SESSION_REVOKE_PATH = Path("/app/config/session-revoked.json")  # 退出登录后作废的令牌（把 Cookie 拄走也用不了）
NOTICE_PATH = Path("/app/config/panel-notice.json")  # 主界面公告条 + 管理员联系方式（给「全体同志」看的那份）
MESSAGES_PATH = Path("/app/config/panel-messages.json")  # 定向消息（管理员单独发给指定用户），显示在他们控制台最上方
CODES_PATH = Path("/app/config/panel-redeem-codes.json")  # 一次性兑换码（只保存哈希）
WEBPUSH_SUBSCRIPTIONS_PATH = Path("/app/config/panel-webpush-subscriptions.json")
WEBPUSH_VAPID_PATH = Path("/app/config/panel-webpush-vapid.json")
WEBPUSH_STATE_PATH = LOG_DIR / "webpush-state.json"
WEBPUSH_LIB_DIR = Path("/app/config/webpush-lib")
MAX_BODY_BYTES = 1000000  # 单次请求体上限，超过直接回 413
BODY_READ_TIMEOUT = 15  # 秒：读请求体的总时限，客户端只报长度不发内容时不能一直等

# 登录态有效期（秒）。以前令牌是"永久有效"的：只要 Cookie 被抄走就再也踢不掉。
SESSION_TTL = 7 * 24 * 3600
# 登录失败限速：同一个「来源 + 登录名」失败 5 次锁 10 分钟；
# 同一个来源（IP）换着登录名试，失败 10 次也锁 —— 专门挡"换用户名爆破"。
LOGIN_MAX_FAILS = 5
LOGIN_MAX_FAILS_IP = 10
LOGIN_WINDOW = 10 * 60
LOGIN_BLOCK_SECONDS = 10 * 60
LOGIN_FAIL_DELAY = 0.7
# 计数表的键是"来源 + 登录名"直接拼出来的，登录名来自表单原文。
# 不截断的话，POST 一个超长用户名就能往计数文件里灌一条超长记录；
# 光截断还不够 —— 登录名可以无限换，所以还要给整张表一个条数上限。
LOGIN_NAME_MAX = 64
LOGIN_FAIL_KEYS_MAX = 1000
# 是否相信 X-Real-IP / X-Forwarded-For 里的来源 IP。默认**不信**：
# 面板挂在 frp / 隧道后面时，所有请求的对端都是 127.0.0.1，
# 一旦相信这两个头，攻击者每换一个假 IP 就能把登录限速绕过去。
# 只有当前面确实有「会强制覆盖这两个头」的反代时，才设 PANEL_TRUST_PROXY=1。
TRUST_PROXY_HEADERS = (os.getenv("PANEL_TRUST_PROXY", "").strip().lower() in ("1", "true", "yes", "on"))
# 注册限速：同一来源 10 分钟内最多提交 8 次注册（面板是公网的，防脚本批量建号）
REGISTER_MAX_PER_WINDOW = 8
# 「授权浏览器 / 登录检测 / 发送任务」三件事互斥：同一时刻只允许一个在被启动
_ENGINE_LOCK = threading.RLock()
# 「登录失效后自动准备二维码」的锁：多个页面同时刷新也只准备一次
_AUTO_AUTH_LOCK = threading.RLock()
# .env 的读写锁：多个请求同时改配置时，不会互相覆盖
_ENV_LOCK = threading.RLock()
_SCHEDULE_LOCK = threading.RLock()


# --------------------------------------------------------------------------
# 落盘工具：所有配置/状态文件都走「临时文件 + 原子替换」，并且一律 0600。
# 别人在读的时候，要么看到完整的旧内容，要么看到完整的新内容，
# 绝不会读到写了一半的半个文件。
# --------------------------------------------------------------------------
def atomic_write(path, text: str, mode: int = 0o600) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = "%s.tmp.%d.%d" % (target, os.getpid(), threading.get_ident())
    handle = None
    try:
        # 直接以 0600 建临时文件，不要"先 644 再 chmod"（中间那一小会儿别人是能读的）
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        handle = os.fdopen(fd, "w", encoding="utf-8")
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        handle = None
        # 偶尔会有别的东西正开着这个文件（Windows 上尤其明显、杀毒软件也会凑热闹），
        # 撞上了就等一眨眼再换，别把这一次写入整个丢掉。
        for attempt in range(20):
            try:
                os.replace(tmp, target)
                break
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(0.02)
    finally:
        if handle is not None:
            try:
                handle.close()
            except Exception:
                pass
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except Exception:
                pass


def append_text(path, text: str, mode: int = 0o600) -> None:
    """追加日志。文件不存在时直接以 0600 建出来，不留"先 644"的空档。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, mode)
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        handle.write(text)


def read_json(path, default=None):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {} if default is None else default()
    return data


class JsonStore:
    """一个小 JSON 文件的读写门。

    每个文件一把锁；update(fn) 把"读 → 改 → 写"整段包在同一次加锁里：
    两个请求同时改同一个文件时，后一个会基于前一个刚写好的结果继续改，
    不会互相覆盖（以前会丢更新）。
    """

    def __init__(self, path, default=None, mode: int = 0o600) -> None:
        self.path = Path(path)
        self._default = default if callable(default) else (lambda: {})
        self.mode = mode
        self.lock = threading.RLock()

    def read(self):
        with self.lock:
            return read_json(self.path, self._default)

    def write(self, data) -> None:
        with self.lock:
            atomic_write(self.path, json.dumps(data, ensure_ascii=False, indent=2), self.mode)

    def update(self, fn):
        """原子地"读取 → fn(内容) → 写回"。fn 可以就地改，也可以返回新内容。"""
        with self.lock:
            data = read_json(self.path, self._default)
            result = fn(data)
            if result is not None:
                data = result
            atomic_write(self.path, json.dumps(data, ensure_ascii=False, indent=2), self.mode)
            return data


def _load_panel_secret() -> bytes:
    """会话密钥：优先用 panel.env 里的 PANEL_SECRET；没设就固化到配置目录。

    以前没设时是随机生成的 —— 那样面板一重启，所有人的登录 Cookie 立刻失效。
    """
    env_value = (os.getenv("PANEL_SECRET") or "").strip()
    if env_value:
        # 顺手往配置目录固化一份：发送任务那个容器要用同一个密钥解 Cookie
        try:
            saved_now = SECRET_PATH.read_text(encoding="utf-8").strip()
        except Exception:
            saved_now = ""
        if saved_now != env_value:
            try:
                atomic_write(SECRET_PATH, env_value + "\n", 0o600)
            except Exception:
                pass
        return env_value.encode("utf-8")
    try:
        saved = SECRET_PATH.read_text(encoding="utf-8").strip()
        if saved:
            return saved.encode("utf-8")
    except Exception:
        pass
    generated = secrets.token_hex(32)
    try:
        atomic_write(SECRET_PATH, generated + "\n", 0o600)
    except Exception:
        pass
    return generated.encode("utf-8")


SESSION_SECRET = _load_panel_secret()


# --------------------------------------------------------------------------
# Cookie 落盘加密：密钥从 PANEL_SECRET 派生（PBKDF2），
# 密文用 HMAC-SHA256 计数器模式异或 + HMAC 认证标签（先加密、后认证）。
# 全部只用标准库，容器里不用额外装 cryptography。
# --------------------------------------------------------------------------
_KDF_SALT = b"douyinsparkflow-cookie-v1"
_KDF_ROUNDS = 20000
_KEY_CACHE = {}


def _derive_key() -> bytes:
    key = _KEY_CACHE.get("k")
    if key is None:
        key = hashlib.pbkdf2_hmac("sha256", SESSION_SECRET, _KDF_SALT, _KDF_ROUNDS, 32)
        _KEY_CACHE["k"] = key
    return key


def _stream(key: bytes, nonce: bytes, length: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < length:
        out += hmac.new(key, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest()
        counter += 1
    return bytes(out[:length])


def encrypt_text(plain: str) -> str:
    data = plain.encode("utf-8")
    key = _derive_key()
    mac_key = hashlib.sha256(key + b"|mac").digest()
    nonce = os.urandom(16)
    body = bytes(a ^ b for a, b in zip(data, _stream(key, nonce, len(data))))
    tag = _mac_tag(mac_key, nonce, body)
    return "v1:" + base64.b64encode(nonce + tag + body).decode("ascii")


def _mac_tag(mac_key: bytes, nonce: bytes, body: bytes, versioned: bool = True) -> bytes:
    """认证标签绑上"这份密文是干什么用的"，防止不同用途的密文互相顶替。"""
    payload = (b"v1|cookie|" if versioned else b"") + nonce + body
    return hmac.new(mac_key, payload, hashlib.sha256).digest()[:16]


def decrypt_text(blob) -> str:
    text = str(blob or "")
    if not text.startswith("v1:"):
        raise ValueError("不是加密后的内容")
    raw = base64.b64decode(text[3:].encode("ascii"))
    if len(raw) < 32:
        raise ValueError("密文太短")
    nonce, tag, body = raw[:16], raw[16:32], raw[32:]
    key = _derive_key()
    mac_key = hashlib.sha256(key + b"|mac").digest()
    if not hmac.compare_digest(tag, _mac_tag(mac_key, nonce, body)):
        # 兼容老格式：加固前写下的密文，标签没绑版本前缀，不然老 Cookie 全废
        if not hmac.compare_digest(tag, _mac_tag(mac_key, nonce, body, versioned=False)):
            raise ValueError("密文校验不通过（密钥变过？）")
    return bytes(a ^ b for a, b in zip(body, _stream(key, nonce, len(body)))).decode("utf-8")


# 四个文件的读写门（每个文件一把锁）
STATE_STORE = JsonStore(STATE_PATH)
USERS_STORE = JsonStore(USERS_PATH)
ACCOUNTS_STORE = JsonStore(ACCOUNTS_PATH)
CODES_STORE = JsonStore(CODES_PATH, default=lambda: {"codes": []})
LOGIN_FAILS_STORE = JsonStore(LOGIN_FAILS_PATH)
WEBPUSH_SUBSCRIPTIONS_STORE = JsonStore(
    WEBPUSH_SUBSCRIPTIONS_PATH, default=lambda: {"users": {}}
)
WEBPUSH_STATE_STORE = JsonStore(
    WEBPUSH_STATE_PATH, default=lambda: {"initialized": False, "seen": []}
)

_WEBPUSH_KEY_LOCK = threading.Lock()
_WEBPUSH_CRYPTO_CACHE = None
_WEBPUSH_SUBJECT = (os.getenv("WEBPUSH_SUBJECT") or "https://124.220.96.161/").strip()
_WEBPUSH_MAX_SUBSCRIPTIONS_PER_USER = 5
_WEBPUSH_POLL_SECONDS = 5


def _webpush_crypto():
    """Load the optional cryptography wheel from the persistent config volume."""
    global _WEBPUSH_CRYPTO_CACHE
    if _WEBPUSH_CRYPTO_CACHE is False:
        return None
    if _WEBPUSH_CRYPTO_CACHE is not None:
        return _WEBPUSH_CRYPTO_CACHE
    if WEBPUSH_LIB_DIR.is_dir() and str(WEBPUSH_LIB_DIR) not in sys.path:
        sys.path.insert(0, str(WEBPUSH_LIB_DIR))
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec, utils
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except Exception:
        _WEBPUSH_CRYPTO_CACHE = False
        return None
    _WEBPUSH_CRYPTO_CACHE = (hashes, serialization, ec, utils, AESGCM)
    return _WEBPUSH_CRYPTO_CACHE


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    text = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", text):
        raise ValueError("invalid base64url")
    return base64.urlsafe_b64decode(text + "=" * ((4 - len(text) % 4) % 4))


def webpush_vapid_keys() -> dict:
    """Create one persistent P-256 VAPID key pair, then reuse it for all devices."""
    crypto = _webpush_crypto()
    if not crypto:
        raise RuntimeError("Web Push 的加密组件尚未安装")
    _hashes, _serialization, ec, _utils, _aesgcm = crypto
    with _WEBPUSH_KEY_LOCK:
        if WEBPUSH_VAPID_PATH.exists():
            data = read_json(WEBPUSH_VAPID_PATH, dict)
            private_hex = str(data.get("private_hex") or "") if isinstance(data, dict) else ""
            public_text = str(data.get("public_key") or "") if isinstance(data, dict) else ""
            if not re.fullmatch(r"[0-9a-f]{64}", private_hex):
                raise RuntimeError("VAPID 密钥文件格式无效")
            private_value = int(private_hex, 16)
            private = ec.derive_private_key(private_value, ec.SECP256R1())
            public_raw = private.public_key().public_bytes(
                _serialization.Encoding.X962, _serialization.PublicFormat.UncompressedPoint
            )
            if not hmac.compare_digest(_b64url_encode(public_raw), public_text):
                raise RuntimeError("VAPID 密钥对不匹配")
            return {"private": private, "public_key": public_text, "public_raw": public_raw}
        private = ec.generate_private_key(ec.SECP256R1())
        private_value = private.private_numbers().private_value
        public_raw = private.public_key().public_bytes(
            _serialization.Encoding.X962, _serialization.PublicFormat.UncompressedPoint
        )
        data = {
            "private_hex": "%064x" % private_value,
            "public_key": _b64url_encode(public_raw),
            "created_at": now_text(),
        }
        atomic_write(WEBPUSH_VAPID_PATH, json.dumps(data, ensure_ascii=False, indent=2), 0o600)
        return {"private": private, "public_key": data["public_key"], "public_raw": public_raw}


def _hkdf_extract(salt: bytes, key_material: bytes) -> bytes:
    return hmac.new(salt or (b"\x00" * 32), key_material, hashlib.sha256).digest()


def _hkdf_expand(prk: bytes, info: bytes, length: int) -> bytes:
    output = bytearray()
    previous = b""
    counter = 1
    while len(output) < length:
        previous = hmac.new(prk, previous + info + bytes([counter]), hashlib.sha256).digest()
        output.extend(previous)
        counter += 1
        if counter > 256:
            raise ValueError("HKDF output is too long")
    return bytes(output[:length])


def _webpush_endpoint_origin(endpoint: str) -> str:
    parsed = urlsplit(str(endpoint or ""))
    host = (parsed.hostname or "").lower().rstrip(".")
    allowed = (
        host == "fcm.googleapis.com" or host.endswith(".fcm.googleapis.com")
        or host == "push.services.mozilla.com" or host.endswith(".push.services.mozilla.com")
        or host == "push.apple.com" or host.endswith(".push.apple.com")
    )
    if (
        parsed.scheme != "https" or not allowed or not parsed.path.startswith("/")
        or parsed.username or parsed.password or parsed.port not in (None, 443)
        or len(endpoint) > 2048
    ):
        raise ValueError("推送地址不受支持")
    return "https://" + parsed.netloc.lower()


def _vapid_authorization(endpoint: str, keypair: dict) -> str:
    crypto = _webpush_crypto()
    if not crypto:
        raise RuntimeError("Web Push 的加密组件尚未安装")
    hashes, _serialization, _ec, utils, _aesgcm = crypto
    header = _b64url_encode(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    claims = {
        "aud": _webpush_endpoint_origin(endpoint),
        "exp": int(time.time()) + 12 * 60 * 60,
        "sub": _WEBPUSH_SUBJECT,
    }
    body = _b64url_encode(json.dumps(claims, separators=(",", ":")).encode())
    signing_input = (header + "." + body).encode("ascii")
    der = keypair["private"].sign(signing_input, _ec_signature_algorithm())
    r, s = utils.decode_dss_signature(der)
    return "vapid t=%s.%s.%s, k=%s" % (
        header, body, _b64url_encode(r.to_bytes(32, "big") + s.to_bytes(32, "big")),
        keypair["public_key"],
    )


def _ec_signature_algorithm():
    crypto = _webpush_crypto()
    if not crypto:
        raise RuntimeError("Web Push 的加密组件尚未安装")
    return crypto[2].ECDSA(crypto[0].SHA256())


def _webpush_encrypt(subscription: dict, payload: bytes) -> tuple:
    crypto = _webpush_crypto()
    if not crypto:
        raise RuntimeError("Web Push 的加密组件尚未安装")
    _hashes, _serialization, ec, _utils, aes_gcm = crypto
    keys = subscription.get("keys") if isinstance(subscription, dict) else None
    if not isinstance(keys, dict):
        raise ValueError("订阅密钥无效")
    user_public = _b64url_decode(keys.get("p256dh") or "")
    auth_secret = _b64url_decode(keys.get("auth") or "")
    if len(user_public) != 65 or user_public[0] != 4 or len(auth_secret) != 16:
        raise ValueError("订阅密钥长度无效")
    user_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), user_public)
    server_private = ec.generate_private_key(ec.SECP256R1())
    server_public = server_private.public_key().public_bytes(
        _serialization.Encoding.X962, _serialization.PublicFormat.UncompressedPoint
    )
    shared_secret = server_private.exchange(ec.ECDH(), user_key)
    key_info = b"WebPush: info\x00" + user_public + server_public
    input_key_material = _hkdf_expand(_hkdf_extract(auth_secret, shared_secret), key_info, 32)
    salt = os.urandom(16)
    prk = _hkdf_extract(salt, input_key_material)
    content_key = _hkdf_expand(prk, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf_expand(prk, b"Content-Encoding: nonce\x00", 12)
    encrypted = aes_gcm(content_key).encrypt(nonce, payload + b"\x02", None)
    record_size = 4096
    body = salt + record_size.to_bytes(4, "big") + bytes([len(server_public)]) + server_public + encrypted
    return body, server_public


class _NoWebPushRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def send_webpush(subscription: dict, message: dict) -> str:
    """Send one encrypted Web Push. Returns sent, expired, or retry."""
    try:
        endpoint = str(subscription.get("endpoint") or "")
        _webpush_endpoint_origin(endpoint)
        keys = webpush_vapid_keys()
        body, _server_public = _webpush_encrypt(
            subscription, json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        request = Request(
            endpoint,
            data=body,
            method="POST",
            headers={
                "Authorization": _vapid_authorization(endpoint, keys),
                "Content-Encoding": "aes128gcm",
                "Content-Type": "application/octet-stream",
                "TTL": "86400",
                "Urgency": "high",
            },
        )
        opener = build_opener(_NoWebPushRedirect())
        with opener.open(request, timeout=6) as response:
            return "sent" if 200 <= response.status < 300 else "retry"
    except HTTPError as error:
        if error.code in (404, 410):
            return "expired"
        log_force("Web Push 服务暂不可用", "HTTP %d" % error.code)
        return "retry"
    except (URLError, TimeoutError, OSError) as error:
        # Never put subscription endpoints or cryptographic material in logs.
        log_force("Web Push 发送失败", type(error).__name__)
        return "retry"
    except (ValueError, TypeError, RuntimeError) as error:
        log_force("Web Push 订阅无效", type(error).__name__)
        return "expired"


def _clean_push_subscriptions(data) -> dict:
    users = data.get("users") if isinstance(data, dict) else None
    cleaned = {}
    if isinstance(users, dict):
        for name, records in users.items():
            if not isinstance(records, list):
                continue
            valid = [
                item for item in records
                if isinstance(item, dict) and isinstance(item.get("endpoint"), str)
                and isinstance(item.get("keys"), dict)
            ]
            if valid:
                cleaned[str(name)] = valid[:_WEBPUSH_MAX_SUBSCRIPTIONS_PER_USER]
    return {"users": cleaned}


def save_webpush_subscription(username: str, subscription: dict) -> dict:
    endpoint = str(subscription.get("endpoint") or "")
    _webpush_endpoint_origin(endpoint)
    keys = subscription.get("keys") if isinstance(subscription.get("keys"), dict) else {}
    p256dh = _b64url_encode(_b64url_decode(str(keys.get("p256dh") or "")))
    auth = _b64url_encode(_b64url_decode(str(keys.get("auth") or "")))
    if len(_b64url_decode(p256dh)) != 65 or len(_b64url_decode(auth)) != 16:
        raise ValueError("订阅密钥长度无效")
    crypto = _webpush_crypto()
    if not crypto:
        raise RuntimeError("Web Push 的加密组件尚未安装")
    try:
        crypto[2].EllipticCurvePublicKey.from_encoded_point(
            crypto[2].SECP256R1(), _b64url_decode(p256dh)
        )
    except Exception as error:
        raise ValueError("订阅公钥无效") from error
    record = {"endpoint": endpoint, "keys": {"p256dh": p256dh, "auth": auth}, "at": now_text()}
    result = {"ok": True}

    def apply(data):
        users = _clean_push_subscriptions(data)["users"]
        current_records = [item for item in users.get(username, []) if item.get("endpoint") != endpoint]
        if len(current_records) >= _WEBPUSH_MAX_SUBSCRIPTIONS_PER_USER:
            result.update({"ok": False, "error": "一个账号最多开启 5 台设备的通知，请先关闭其他设备"})
            return {"users": users}
        # A push endpoint belongs to one panel login only, even on a shared device.
        for name in list(users):
            users[name] = [item for item in users[name] if item.get("endpoint") != endpoint]
            if not users[name]:
                users.pop(name, None)
        users[username] = current_records + [record]
        return {"users": users}

    WEBPUSH_SUBSCRIPTIONS_STORE.update(apply)
    return result


def remove_webpush_subscription(username: str, endpoint: str) -> None:
    if endpoint:
        _webpush_endpoint_origin(endpoint)

    def apply(data):
        users = _clean_push_subscriptions(data)["users"]
        if username in users:
            users[username] = [item for item in users[username] if item.get("endpoint") != endpoint]
            if not users[username]:
                users.pop(username, None)
        return {"users": users}

    WEBPUSH_SUBSCRIPTIONS_STORE.update(apply)


def _push_run_visible(run: dict, username: str, users: dict, accounts_by_name: dict) -> bool:
    if username == PANEL_USERNAME:
        return True
    item = users.get(username) if isinstance(users, dict) else None
    scopes = set(str(uid) for uid in (item.get("accounts") or [])) if isinstance(item, dict) else set()
    uid = str(run.get("unique_id") or "")
    if uid:
        return uid in scopes
    owned_names = {
        str(accounts_by_name.get(uid) or "") for uid in scopes
    }
    owned_names.discard("")
    return str(run.get("account") or "") in owned_names


def _push_event_id(run: dict) -> str:
    stable = json.dumps(run, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()[:32]


def _push_message(run: dict, event_id: str) -> dict:
    status = str(run.get("status") or "")
    labels = {
        "ok": "发送成功", "partial": "部分发送成功", "failed": "发送失败",
        "error": "发送出错", "no_login": "账号需要重新登录", "no_friend": "未找到目标好友",
    }
    account = str(run.get("account") or run.get("unique_id") or "你的账号")[:60]
    label = labels.get(status, "发送任务已结束")
    return {
        "title": label,
        "body": account + " · " + label,
        "url": "/",
        "tag": "send-" + event_id,
    }


def _webpush_notify_loop(stop_event: threading.Event) -> None:
    """Watch completed send records and deliver account-scoped Web Push notifications."""
    try:
        state = WEBPUSH_STATE_STORE.read()
        if not isinstance(state, dict) or not state.get("initialized"):
            baseline = [_push_event_id(run) for run in load_sends(SEND_STORE_MAX) if isinstance(run, dict)]
            WEBPUSH_STATE_STORE.write({"initialized": True, "seen": baseline[-500:], "retry_after": {}})
    except Exception as error:
        log_force("Web Push 初始化失败", type(error).__name__)
    while not stop_event.wait(_WEBPUSH_POLL_SECONDS):
        try:
            state = WEBPUSH_STATE_STORE.read()
            seen_order = [str(item) for item in (state.get("seen") or [])]
            seen = set(seen_order)
            retry_after = state.get("retry_after") if isinstance(state.get("retry_after"), dict) else {}
            runs = [item for item in load_sends(SEND_STORE_MAX) if isinstance(item, dict)]
            events = []
            for run in reversed(runs):
                status = str(run.get("status") or "")
                event_id = _push_event_id(run)
                if (status in ("running", "queued") or event_id in seen
                        or int(retry_after.get(event_id) or 0) > int(time.time())):
                    continue
                events.append((run, event_id))
            if not events:
                continue
            subscriptions = _clean_push_subscriptions(WEBPUSH_SUBSCRIPTIONS_STORE.read())["users"]
            users = load_users()
            accounts_by_name = {
                str(task.get("unique_id") or ""): str(task.get("username") or "")
                for task in load_tasks()
            }
            for run, event_id in events:
                message = _push_message(run, event_id)
                retry = False
                for username, records in subscriptions.items():
                    if not _push_run_visible(run, username, users, accounts_by_name):
                        continue
                    expired = []
                    for subscription in records:
                        result = send_webpush(subscription, message)
                        if result == "expired":
                            expired.append(str(subscription.get("endpoint") or ""))
                        elif result == "retry":
                            retry = True
                    if expired:
                        for endpoint in expired:
                            try:
                                remove_webpush_subscription(username, endpoint)
                            except Exception:
                                pass
                if retry:
                    retry_after[event_id] = int(time.time()) + 30
                else:
                    seen.add(event_id)
                    seen_order.append(event_id)
                    retry_after.pop(event_id, None)
            pending_ids = {
                _push_event_id(run) for run in runs
                if str(run.get("status") or "") not in ("running", "queued")
                and _push_event_id(run) not in seen
            }
            retry_after = {
                key: value for key, value in retry_after.items()
                if key in pending_ids and int(value or 0) > int(time.time()) - 86400
            }
            WEBPUSH_STATE_STORE.write({
                "initialized": True, "seen": seen_order[-500:], "retry_after": retry_after,
            })
        except Exception as error:
            log_force("Web Push 后台检查失败", type(error).__name__)


# 退出登录「黑名单」：令牌一旦进来，签名再对也不认。
# 以前退出只是让浏览器把 Cookie 扈掉 —— 把 Cookie 拄走的人照样能用满 7 天。
SESSION_REVOKE_STORE = JsonStore(SESSION_REVOKE_PATH)
_REVOKE_LOCK = threading.RLock()
_REVOKED = {}
# 盘上的作废表读过没有。以前判断条件是 "if not _REVOKED"：只要表是空的，
# 每个请求都会去读一次磁盘 —— 空表也要记成"读过了"。
_REVOKE_LOADED = False


def token_id(token: str) -> str:
    """令牌的指纹（只存指纹，不存令牌本身）。"""
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()[:32]


def _revoke_expiry(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def revoked_tokens() -> dict:
    """当前的作废表；顺手把已经自然过期的条目清掉，别让文件越涨越大。"""
    now = int(time.time())
    global _REVOKE_LOADED
    with _REVOKE_LOCK:
        if not _REVOKE_LOADED:
            _REVOKE_LOADED = True
            try:
                saved = SESSION_REVOKE_STORE.read()
            except Exception:
                saved = None
            if isinstance(saved, dict):
                _REVOKED.update({
                    str(k): _revoke_expiry(v) for k, v in saved.items()
                    if _revoke_expiry(v) > now
                })
        dropped = [k for k, v in _REVOKED.items() if _revoke_expiry(v) <= now]
        for k in dropped:
            _REVOKED.pop(k, None)
        if dropped:
            try:
                SESSION_REVOKE_STORE.write(dict(_REVOKED))
            except Exception:
                pass
        return dict(_REVOKED)


def revoke_token(token: str) -> None:
    """把令牌拉黑 —— 点「退出」时调用，之后拿着同一个 Cookie 也进不来。"""
    if not token:
        return
    with _REVOKE_LOCK:
        _REVOKED[token_id(token)] = int(time.time()) + SESSION_TTL + 60
        try:
            SESSION_REVOKE_STORE.write(dict(_REVOKED))
        except Exception:
            pass

_LOGIN_FAILS = {}
_LOGIN_LOCK = threading.Lock()
try:
    _saved_fails = LOGIN_FAILS_STORE.read()
    if isinstance(_saved_fails, dict):
        _LOGIN_FAILS.update(_saved_fails)
except Exception:
    pass


def load_admin() -> dict:
    try:
        data = json.loads(ADMIN_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def admin_material() -> str:
    """派生长效登录态用的材料：管理员密码一变，它跟着变。"""
    item = load_admin()
    if item.get("hash"):
        return str(item["hash"])
    return "env:" + PANEL_PASSWORD


def admin_key() -> str:
    """管理员的登录态里带上它：改了面板密码，所有旧的管理员登录立刻失效。"""
    return hashlib.sha256(("panel-admin:" + admin_material()).encode("utf-8")).hexdigest()[:16]


def _eq_secret(a, b) -> bool:
    """恒定时间地比较两个字符串，允许出现非 ASCII。

    secrets 里那个 compare_digest 收字符串时只认 ASCII，直接拿用户输入去比会抛
    TypeError（在登录页打中文用户名 / 中文密码就会 500）。先各自按 UTF-8
    编码成 bytes 再比，没有这个限制，恒定时间比较的性质也一样保留。
    """
    try:
        return secrets.compare_digest(
            str(a).encode("utf-8", "surrogatepass"),
            str(b).encode("utf-8", "surrogatepass"),
        )
    except Exception:
        return False


def verify_admin(user: str, password: str) -> bool:
    """管理员登录校验：自己改过密码就用新的，没改过就用 panel.env 里那个。"""
    if not password or not _eq_secret(user or "", PANEL_USERNAME):
        return False
    item = load_admin()
    if item.get("salt") and item.get("hash"):
        return hmac.compare_digest(_pass_hash(str(password), str(item["salt"])), str(item["hash"]))
    if not PANEL_PASSWORD:
        return False
    return _eq_secret(password, PANEL_PASSWORD)


def set_admin_password(password: str, again=None) -> dict:
    password = str(password or "")
    if again not in (None, "") and str(again) != password:
        return {"ok": False, "error": "两次输入的新密码不一样"}
    if len(password) < 6:
        return {"ok": False, "error": "新密码至少 6 位"}
    salt = secrets.token_hex(16)
    item = {"salt": salt, "hash": _pass_hash(password, salt), "at": now_text()}
    try:
        atomic_write(ADMIN_PATH, json.dumps(item, ensure_ascii=False, indent=2))
    except Exception as error:
        return {"ok": False, "error": "写不进配置文件：" + str(error)}
    log_force("改管理员密码", "管理员密码已更新")
    return {"ok": True, "message": "管理员密码改好了，下次用新密码登录"}


def _login_key(ip: str, user: str) -> str:
    # 登录名只取前 LOGIN_NAME_MAX 个字符：键的长度必须有界
    short = str(user or "").strip().lower()[:LOGIN_NAME_MAX]
    return "%s|%s" % (str(ip or ""), short)


# --------------------------------------------------------------------------
# 登录限速：同时盯「来源 + 登录名」和「来源(IP)」两个计数，
# 而且每次都落盘 —— 面板被重启也忘不掉已经失败过几次。
# --------------------------------------------------------------------------
def _fail_keys(ip: str, user: str) -> list:
    ip = str(ip or "")
    keys = [(_login_key(ip, user), LOGIN_MAX_FAILS)]
    if not _is_proxy_ip(ip):
        keys.append(("ip@" + ip, LOGIN_MAX_FAILS_IP))
    return keys


def _is_proxy_ip(ip: str) -> bool:
    """这个来源是不是"本机/内网里的代理"。

    面板通常挂在隧道（frp 之类）后面，所有访客看起来都是 127.0.0.1。
    这时候如果还按来源 IP 计数，随便几个人输错密码就会把所有人一起锁死，
    所以只有真正直连过来的公网 IP 才吃"单个 IP 最多错 10 次"这一条。
    """
    text = str(ip or "").strip()
    if not text:
        return True
    try:
        addr = ipaddress.ip_address(text)
    except ValueError:
        return True
    return bool(addr.is_loopback or addr.is_private or addr.is_link_local)


def pick_client_ip(peer: str, real: str = "", forwarded: str = "") -> str:
    """还原真实访客 IP。

    直连过来的就用对端地址；只有本机/内网代理（nginx、frp 之类）转发时，
    才相信代理自己填的 X-Real-IP / X-Forwarded-For —— 公网访客伪造的头部不认。
    """
    peer = str(peer or "").strip()
    # 默认只认对端地址：转发头是客户端自己就能填的，信了等于没限速（见 TRUST_PROXY_HEADERS）
    if not TRUST_PROXY_HEADERS:
        return peer
    if not _is_proxy_ip(peer):
        return peer
    real = str(real or "").strip()
    if real and not _is_proxy_ip(real):
        return real
    for item in reversed([part.strip() for part in str(forwarded or "").split(",")]):
        if item and not _is_proxy_ip(item):
            return item
    return peer


def _sweep_fails(now: float = None) -> None:
    """清掉过期的计数；换着登录名刷的时候，再按"最早到期的优先"砍掉多出来的行。

    以前这张表只增不减：每换一个登录名就多一条，永远不清理。
    于是拿脚本换用户名试密码，内存和 login-fails.json 会一路涨，
    而且每失败一次都要把整张表重写一遍，越写越慢。
    """
    now = time.time() if now is None else now
    stale = [
        key for key, row in _LOGIN_FAILS.items()
        if not isinstance(row, dict)
        or (row.get("until", 0) <= now and now - row.get("first", 0) > LOGIN_WINDOW)
    ]
    for key in stale:
        _LOGIN_FAILS.pop(key, None)
    extra = len(_LOGIN_FAILS) - LOGIN_FAIL_KEYS_MAX
    if extra > 0:
        # 按 until 升序丢：还没解锁的（until 在未来）排在后面，会被保住
        ordered = sorted(
            _LOGIN_FAILS.items(),
            key=lambda kv: (kv[1].get("until", 0), kv[1].get("first", 0)),
        )
        for key, _row in ordered[:extra]:
            _LOGIN_FAILS.pop(key, None)


def _save_fails() -> None:
    with _LOGIN_LOCK:
        _sweep_fails()
        snapshot = dict(_LOGIN_FAILS)
    try:
        LOGIN_FAILS_STORE.write(snapshot)
    except Exception:
        pass


def login_block_left(ip: str, user: str) -> int:
    """还要等多少秒才能再试（0 = 现在可以试）"""
    now = time.time()
    left = 0
    with _LOGIN_LOCK:
        for key, _limit in _fail_keys(ip, user):
            row = _LOGIN_FAILS.get(key)
            if not row:
                continue
            if row.get("until", 0) > now:
                left = max(left, int(row["until"] - now) + 1)
            elif now - row.get("first", 0) > LOGIN_WINDOW:
                _LOGIN_FAILS.pop(key, None)
    return left


def login_tries_left(ip: str, user: str) -> int:
    """「来源 + 登录名」这条计数上还能再错几次（没记录 = 一次都还没错过）。"""
    now = time.time()
    with _LOGIN_LOCK:
        row = _LOGIN_FAILS.get(_login_key(ip, user))
        if not row or now - row.get("first", 0) > LOGIN_WINDOW:
            return LOGIN_MAX_FAILS
        return max(0, LOGIN_MAX_FAILS - int(row.get("count", 0)))


def login_failed(ip: str, user: str) -> None:
    now = time.time()
    with _LOGIN_LOCK:
        for key, limit in _fail_keys(ip, user):
            row = _LOGIN_FAILS.get(key)
            if not row or now - row.get("first", 0) > LOGIN_WINDOW:
                row = {"count": 0, "first": now, "until": 0}
            row["count"] = int(row.get("count", 0)) + 1
            if row["count"] >= limit:
                row["until"] = now + LOGIN_BLOCK_SECONDS
                row["count"] = 0
                row["first"] = now
            _LOGIN_FAILS[key] = row
    _save_fails()


def login_ok(ip: str, user: str) -> None:
    with _LOGIN_LOCK:
        for key, _limit in _fail_keys(ip, user):
            _LOGIN_FAILS.pop(key, None)
    _save_fails()


def register_block_left(ip: str) -> int:
    """注册还要等多少秒（0 = 现在可以提交）。和登录共用一份计数文件。"""
    now = time.time()
    key = "reg@" + str(ip or "")
    with _LOGIN_LOCK:
        row = _LOGIN_FAILS.get(key)
        if not row:
            return 0
        if now - row.get("first", 0) > LOGIN_WINDOW:
            _LOGIN_FAILS.pop(key, None)
            return 0
        if int(row.get("count", 0)) >= REGISTER_MAX_PER_WINDOW:
            return max(1, int(LOGIN_WINDOW - (now - row.get("first", now))) + 1)
    return 0


def note_register_attempt(ip: str) -> None:
    """记一次注册提交（成功失败都算）：超过阈值就先挡一会儿。"""
    now = time.time()
    key = "reg@" + str(ip or "")
    with _LOGIN_LOCK:
        row = _LOGIN_FAILS.get(key)
        if not row or now - row.get("first", 0) > LOGIN_WINDOW:
            row = {"count": 0, "first": now, "until": 0}
        row["count"] = int(row.get("count", 0)) + 1
        _LOGIN_FAILS[key] = row
    _save_fails()


def shot_name_of(text) -> str:
    """截图文件名里的"账号名"段：必须和 tasks.py 的 _safe_name 算法一致。"""
    cleaned = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_-]+", "_", str(text or "")).strip("_")
    return cleaned[:24]

CHAT_URL = "https://www.douyin.com/chat"
# 「开始授权」后自动走的登录流程：自己点开登录弹窗 -> 把二维码扒出来给用户扫 ->
# 扫完可能要短信二级验证 -> 把验证码填进去并点验证。
LOGIN_ENTRY_TEXTS = ["登录", "立即登录", "登录/注册"]
# 抖音的 class 名是随时会变的哈希（fe8GGOyG 这种），一律按「文字 + 尺寸 + 可见性」找，
# 所以这里全都是"见到这些字就认为是那个东西"的词表。
# 还留在「登录弹窗」里的特征词：看到这些就说明没到二级验证那一步。
# 手机号那一栏也有个「请输入验证码」的框，认错了会把验证码填进登录框里，所以宁可不认。
MODAL_WORDS = ("如何扫码", "扫码登录", "验证码登录", "密码登录",
               "请输入手机号", "手机号登录", "国家/地区")
SMS_STRONG_WORDS = ("安全验证", "身份验证", "验证身份", "短信验证", "已发送", "发送至",
                    "为了你的账号安全", "设备验证", "二次验证",
                    # 二级验证还有「验证登录密码」这一档：弹的是密码框，不是验证码框。
                    # 见到它就说明确实进了二级验证，别再被"还在登录弹窗里"那条挡掉。
                    "验证登录密码", "请输入登录密码")
# 二级验证里「要用已登录的设备扫码/扫脸」那一档
VERIFY_QR_WORDS = ("扫码验证", "使用已登录", "已登录账号", "设备扫码", "以确保为本人操作",
                   "本人操作", "扫脸", "人脸识别", "面部识别", "请使用抖音 App")
# 只有页面明确显示「选择验证方式」，或同时显示两种以上验证选项时，才按选择页处理。
# 直接进入二维码/人脸验证页时不点击，避免误触返回或换方式。
VERIFY_CHOICE_PROMPT_WORDS = ("请选择验证方式", "选择验证方式", "请选择一种验证方式",
                              "选择一种验证方式", "原设备扫码还是人脸", "原设备验证还是人脸",
                              "登录双重验证", "双重验证", "安全验证方式", "身份验证", "安全验证", "请选择验证", "验证方式")
VERIFY_PHONE_WORDS = ("接收短信验证码", "通过短信验证", "使用短信验证", "短信验证",
                      "手机短信验证", "手机号验证", "手机号码验证", "手机验证",
                      "通过手机号验证", "通过手机验证", "使用手机号验证", "使用手机验证",
                      "接收验证码", "短信验证码", "手机验证码", "手机号验证码",
                      "使用手机验证码", "通过手机验证码", "发送短信验证码")
VERIFY_PHONE_PICK_TEXTS = ("接收短信验证码", "通过短信验证", "使用短信验证", "短信验证",
                           "手机短信验证", "手机号验证", "手机号码验证", "手机验证",
                           "通过手机号验证", "通过手机验证", "使用手机号验证", "使用手机验证",
                           "接收验证码", "短信验证码", "手机验证码", "手机号验证码",
                           "使用手机验证码", "通过手机验证码", "发送短信验证码")
VERIFY_DEVICE_WORDS = ("用原设备扫码", "使用原设备扫码", "原设备扫码", "原设备验证",
                       "使用原设备验证", "在原设备上验证", "用已登录设备扫码", "使用已登录设备扫码",
                       "已登录设备扫码", "用已登录设备验证", "用本机抖音扫码", "已登录设备验证")
VERIFY_FACE_WORDS = ("人脸验证", "人脸识别", "扫脸验证", "扫脸", "面部验证", "面部识别", "刷脸验证")
VERIFY_DEVICE_PICK_TEXTS = ("用原设备扫码", "使用原设备扫码", "原设备扫码", "原设备验证",
                            "使用原设备验证", "在原设备上验证", "用已登录设备扫码", "使用已登录设备扫码",
                            "已登录设备扫码", "用已登录设备验证", "用本机抖音扫码", "已登录设备验证")
VERIFY_FACE_PICK_TEXTS = ("人脸验证", "使用人脸验证", "通过人脸验证", "进行人脸验证", "人脸核验",
                          "人脸识别", "使用人脸识别", "通过人脸识别", "扫脸验证", "扫脸",
                          "面部验证", "面部识别", "刷脸验证", "刷脸")
VERIFY_FACE_STAGE_WORDS = ("请进行人脸验证", "请完成人脸验证", "正在进行人脸验证",
                           "人脸识别", "扫脸验证", "面部识别", "活体检测", "请眨眼", "正对屏幕")
VERIFY_PHONE_FALLBACK_AFTER = 10.0   # 手机号验证仍停留在选择页，再回退到原设备扫码
VERIFY_DEVICE_FALLBACK_AFTER = 10.0  # 原设备扫码仍停留在选择页，再回退到人脸
VERIFY_FACE_MANUAL_AFTER = 8.0       # 人脸入口点选后仍停留在选择页，交给用户在画面中操作
# 「已经进二级验证了」的强特征词。特意比 SMS_STRONG_WORDS 窄：
# 不能把「已发送 / 发送至」算进来 —— 那是手机号登录第一步就会出现的字，
# 混进来会让面板在正常登录途中就误判成"到二级验证了"。
VERIFY_STAGE_WORDS = ("设备验证", "二次验证", "二次校验", "为了你的账号安全",
                      "人脸识别", "人脸验证", "原设备验证", "用原设备扫码", "已登录设备扫码",
                      "请选择验证方式", "选择验证方式", "扫脸验证", "验证登录密码",
                      "完成身份验证")
QR_EXPIRED_WORDS = ("已失效", "已过期", "点击刷新", "刷新二维码")
SMS_BAD_WORDS = ("验证码错误", "验证码不正确", "验证码有误", "验证码已过期", "验证码失效",
                 "验证码无效", "验证失败", "验证码次数", "不正确", "已过期", "失效",
                 "操作频繁", "太过频繁", "请稍后再试", "请重试", "网络异常", "错误")
QR_MIN_SIDE = 100        # 小于这个尺寸的图不当二维码
QR_FIRST_WAIT = 45       # 等二维码出现最多等这么久，超了就说"取不到，用手动截图"
QR_REFRESH_AFTER = 100   # 二维码超过这么久没换新，就去找页面上的「点击刷新」
SMS_AFTER_QR_GONE = 2.0  # 二维码消失后等这么久，才认为"扫过了"
SMS_SETTLE = 3.0         # 点完「验证」等这么久，再看抖音的结果

# 抖音弹「身份验证」让你选怎么验证时，手机号/短信是优先选项。
SMS_CHOICE_TEXTS = VERIFY_PHONE_PICK_TEXTS
# 万一还要再点一下才发短信（按钮文字完全一致才点，不能是那个行的名字）
SMS_SEND_TEXTS = ("获取验证码", "发送验证码", "获取短信验证码")
SMS_CHOICE_EVERY = 8.0   # 秒：同一次授权里最快多久去点一次
SMS_SEND_EVERY = 30.0    # 秒：自动点「获取验证码」的间隔（不能狂发短信）
SMS_BEFORE_SUBMIT_WAIT = 3.5  # 秒：验证码敲进去后先等几秒，让抖音认到，再点「验证」
SMS_TYPE_DELAY = 120          # 毫秒：一个数字一个数字敲（跟真人一样），敲得太快页面会丢
# 二级验证是「验证登录密码」时，用户填的是抖音登录密码：长度上限比验证码宽松，
# 而且**绝不做数字过滤**（密码里有字母和符号）。密码只在内存里过一手，
# 不写日志、不进状态、不落盘（详见 _submit_pwd）。
SMS_PWD_MAX = 64

# 通用输入框（主界面「浏览器画面」下面那个框）：用户自己打什么都有可能，
# 所以上限给得比密码还宽一点，只防手滑粘进一整篇文章。字符一律不过滤。
# 它**不做任何识别**，只把内容粘到抖音页面当前光标处（详见 _submit_text / _paste_text）。
TEXT_MAX = 200

# 手机号登录（面板主路径）：先切到这个标签，再填号码
PHONE_TAB_TEXTS = ("验证码登录", "手机号登录", "接收短信验证码")
# 手机号登录那一步的提交按钮：抖音那个弹窗写的是「登录」，不是「验证」
PHONE_SUBMIT_TEXTS = ("登录", "立即登录")

# 把文字对应的元素找出来，返回屏幕坐标 —— 点击由外面用真鼠标点
_JS_FIND_TEXT_CENTER = """(texts) => {
  const wanted = (texts || []).map((t) => String(t || "").trim()).filter(Boolean);
  if (!wanted.length) return {ok: false};
  const phone = document.querySelector("input#normal-input");
  const code = document.querySelector("input#button-input");
  const containerOf = (el) => {
    let node = el;
    while (node && node.parentElement && node !== document.body) {
      node = node.parentElement;
      if (code && node.contains(code) && phone && node.contains(phone)) return node;
    }
    return null;
  };
  const root = (phone && containerOf(phone)) || document.body;
  const vis = (el) => {
    const st = getComputedStyle(el);
    if (st.display === "none" || st.visibility === "hidden" || st.opacity === "0") return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const match = (scope) => [...scope.querySelectorAll("span,div,button,a")].filter((el) => {
    if (!vis(el)) return false;
    if (el.disabled === true || String(el.getAttribute("aria-disabled") || "") === "true") return false;
    return wanted.indexOf(el.textContent.trim()) >= 0;
  });
  let nodes = [];
  let scope = root;
  for (let i = 0; i < 3 && scope && scope !== document.body; i += 1) {
    nodes = match(scope);
    if (nodes.length) break;
    scope = scope.parentElement;
  }
  if (!nodes.length) nodes = match(document.body);
  if (!nodes.length) return {ok: false};
  nodes.sort((a, b) => {
    const ra = a.getBoundingClientRect();
    const rb = b.getBoundingClientRect();
    return ra.width * ra.height - rb.width * rb.height;
  });
  const el = nodes[0];
  const r = el.getBoundingClientRect();
  let x = r.left + r.width / 2;
  let y = r.top + r.height / 2;
  const top = document.elementFromPoint(x, y);
  if (top && top !== el && !el.contains(top) && !top.contains(el)) {
    const hr = top.getBoundingClientRect();
    if (hr.width > 0 && hr.height > 0) { x = hr.left + hr.width / 2; y = hr.top + hr.height / 2; }
  }
  return {ok: true, x: x, y: y, text: el.textContent.trim().slice(0, 20)};
}"""

# 受控输入框：直接改 value 会被 React 抹掉，得走原生 setter 再补事件
_JS_FILL_REACT_INPUT = """(args) => {
  const sel = String((args && args.selector) || "");
  const value = String((args && args.value) || "");
  let el = sel ? document.querySelector(sel) : null;
  if (!el) {
    const key = sel === "input#button-input" ? "验证码" : "手机号";
    el = [...document.querySelectorAll("input")].find((n) => {
      const text = String(n.placeholder || "") + String(n.getAttribute("aria-label") || "");
      return text.indexOf(key) >= 0;
    });
  }
  if (!el) return {ok: false, value: ""};
  const proto = window.HTMLInputElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(proto, "value").set;
  try { el.focus(); } catch (e) {}
  setter.call(el, "");
  el.dispatchEvent(new Event("input", {bubbles: true}));
  setter.call(el, value);
  el.dispatchEvent(new Event("input", {bubbles: true}));
  el.dispatchEvent(new Event("change", {bubbles: true}));
  return {ok: true, value: String(el.value || "")};
}"""


def judge_send_response(body: str):
    """看抖音「发验证码」接口的原话：("ok"|"bad"|"", 说明)。"""
    text = str(body or "").strip()
    if not text:
        return "", ""
    try:
        data = json.loads(text)
    except Exception:
        return "", ""
    inner = data.get("data") if isinstance(data.get("data"), dict) else data
    code = inner.get("error_code", inner.get("status_code"))
    detail = str(inner.get("description") or inner.get("message") or "")[:120]
    if code in (0, "0"):
        return "ok", detail
    if code is None:
        return "", detail
    return "bad", detail


VIEWPORT = {"width": 1280, "height": 860}
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
)
DEFAULT_TEMPLATE = "[盖瑞]今日火花[加一]\\n—— [右边] 每日一言 [左边] ——\\n[API]"


# --------------------------------------------------------------------------
# .env 读写（就地覆盖写入，保证容器内单文件挂载始终看到最新内容）
# --------------------------------------------------------------------------
def parse_env() -> dict:
    if not ENV_PATH.exists():
        return {}
    try:
        values = dotenv_values(str(ENV_PATH))
    except Exception:
        return {}
    return {k: v for k, v in values.items() if v is not None}


def env_quote(value) -> str:
    value = "" if value is None else str(value)
    if re.fullmatch(r"[A-Za-z0-9_./:@+%-]+", value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return '"' + escaped + '"'


def write_env_text(text: str) -> None:
    """原子写：先写同目录临时文件，再 os.replace 顶上，读到的一半内容不可能出现。"""
    atomic_write(ENV_PATH, text)


# .env 里的 TZ 缓存一份：now_text 每秒被调很多次，不值得每次都去读盘
_TZ_CACHE = {"value": ""}


def update_env(mapping: dict) -> None:
    """只更新指定键，保留原有注释、顺序和其它配置。整段加锁，不会互相覆盖。"""
    with _ENV_LOCK:
        lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
        out = []
        written = set()
        for line in lines:
            match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
            if match and match.group(1) in mapping:
                key = match.group(1)
                written.add(key)
                if mapping[key] is None:
                    continue  # 传 None 表示删掉这个键
                out.append(key + "=" + env_quote(mapping[key]))
            else:
                out.append(line)
        for key, value in mapping.items():
            if key not in written and value is not None:
                out.append(key + "=" + env_quote(value))
        write_env_text("\n".join(out).rstrip("\n") + "\n")
        _TZ_CACHE["value"] = ""  # TZ 可能刚被改过，下次取时间重新读


# --------------------------------------------------------------------------
# 抖音号配置：存在 /app/config/accounts.json，
# 结构：{抖音号: {"username": 显示名, "targets": [...], "times": [...],
#                 "cookies": "加密后的 Cookie"}}
# .env 里只留 TZ / MESSAGE_TEMPLATE / RANDOM_DELAY_* / HITOKOTO_TYPES 这类全局简单键。
# --------------------------------------------------------------------------
def _clean_cookie_list(data) -> list:
    if not isinstance(data, list):
        return []
    result = []
    for item in data:
        if not isinstance(item, dict) or not item.get("name") or not item.get("domain"):
            continue
        result.append({k: v for k, v in item.items() if k != "sameSite" and v is not None})
    return result


def cookie_key(unique_id: str) -> str:
    return ("COOKIES_" + str(unique_id)).upper()


def _legacy_accounts_from_env() -> dict:
    """老格式：账号在 .env 的 TASKS + COOKIES_XXX 里。只在第一次迁移时读一下。"""
    values = parse_env()
    try:
        tasks = json.loads(values.get("TASKS") or "[]")
    except Exception:
        tasks = []
    if not isinstance(tasks, list):
        return {}
    accounts = {}
    for task in tasks:
        if not isinstance(task, dict):
            continue
        uid = str(task.get("unique_id") or "").strip()
        if not uid:
            continue
        try:
            raw = json.loads(values.get(cookie_key(uid)) or "[]")
        except Exception:
            raw = []
        cookies = _clean_cookie_list(raw)
        accounts[uid] = {
            "username": str(task.get("username") or uid),
            "targets": [str(x) for x in (task.get("targets") or [])],
            "cookies": (
                encrypt_text(json.dumps(cookies, ensure_ascii=False, separators=(",", ":")))
                if cookies
                else ""
            ),
        }
    return accounts


def migrate_legacy_accounts() -> bool:
    """把 .env 里的老账号数据搬进 accounts.json，搬完就把 .env 里那份删掉。"""
    if ACCOUNTS_PATH.exists():
        return False
    accounts = _legacy_accounts_from_env()
    if not accounts:
        return False
    ACCOUNTS_STORE.write(accounts)
    try:
        update_env({"TASKS": None})
        for key in list(parse_env()):
            if key.upper().startswith("COOKIES_"):
                update_env({key: None})
    except Exception:
        pass
    log_force("配置迁移", "已把 %d 个抖音号搬进 accounts.json（Cookie 已加密）" % len(accounts))
    return True


def load_accounts() -> dict:
    accounts = ACCOUNTS_STORE.read()
    if not accounts and not ACCOUNTS_PATH.exists():
        migrate_legacy_accounts()
        accounts = ACCOUNTS_STORE.read()
    if not isinstance(accounts, dict):
        return {}
    return {str(k): v for k, v in accounts.items() if isinstance(v, dict) and str(k)}


def save_account(unique_id: str, **fields) -> None:
    """只改一个抖音号的若干字段，其它账号原样保留。"""
    uid = str(unique_id or "").strip()
    if not uid:
        return

    def _apply(accounts):
        if not isinstance(accounts, dict):
            accounts = {}
        item = accounts.get(uid)
        if not isinstance(item, dict):
            item = {}
        item.update({k: v for k, v in fields.items() if v is not None})
        accounts[uid] = item
        return accounts

    ACCOUNTS_STORE.update(_apply)


def drop_account(unique_id: str) -> None:
    """彻底删掉一个抖音号（配置 + Cookie）。"""
    uid = str(unique_id or "").strip()
    if not uid:
        return

    def _apply(accounts):
        if isinstance(accounts, dict):
            accounts.pop(uid, None)
            return accounts
        return {}

    ACCOUNTS_STORE.update(_apply)


def load_tasks() -> list:
    """抖音号列表：名称、目标好友和账号自己的发送设置，不含 Cookie。"""
    tasks = []
    for uid, item in load_accounts().items():
        settings = item.get("settings")
        tasks.append(
            {
                "username": str(item.get("username") or uid),
                "unique_id": uid,
                "targets": [str(x) for x in (item.get("targets") or [])],
                "times": [str(x) for x in (item.get("times") or []) if str(x).strip()],
                "settings": dict(settings) if isinstance(settings, dict) else {},
            }
        )
    return tasks


def account_settings(task: dict) -> dict:
    """一个抖音号自己的设置（消息模板 / 发送间隔 / 一言类型）。

    只挑「用户真的填过」的那几项：没填的留给前端回落到全局默认，
    这样界面上看到的就是「这个号实际会用到的值」。
    """
    own = task.get("settings") if isinstance(task.get("settings"), dict) else {}
    out = {}
    template = str(own.get("template") or "").strip()
    if template:
        out["template"] = template
    for key in ("delay_min", "delay_max"):
        value = own.get(key)
        if value is None or str(value).strip() == "":
            continue
        try:
            out[key] = str(int(float(str(value))))
        except ValueError:
            continue
    kinds = own.get("hitokoto_types")
    if isinstance(kinds, list) and kinds:
        out["hitokoto_types"] = json.dumps(kinds, ensure_ascii=False)
    return out


def _cookie_list_from_blob(data) -> list:
    """把两种凭据格式都摊成一份 Cookie 数组。

    v1：直接存 Cookie 数组；
    v2：存 {"version":2,"storage_state":{"cookies":[...],"origins":[...]}}
        （新版扫码存的是这个，除了 Cookie 还带 localStorage）。
    以前只认 v1，于是所有用 v2 存的号（ddy 等）在这里**静默返回空** ——
    「一键复制 Cookie」和一切依赖它的功能都拿不到东西，界面上只显示"没保存过 Cookie"，
    查不出原因。这里把 v2 也接上。
    """
    if isinstance(data, dict):
        state = data.get("storage_state")
        if isinstance(state, dict) and isinstance(state.get("cookies"), list):
            return _clean_cookie_list(state["cookies"])
        if isinstance(data.get("cookies"), list):
            return _clean_cookie_list(data["cookies"])
        return []
    return _clean_cookie_list(data)


def load_storage_state(unique_id: str):
    """取这个号的完整浏览器凭据（能直接喂给 Playwright 的 storage_state）。

    v2 的号带着 localStorage，必须整份用上；v1 的号只有 Cookie，包成 storage_state 形状。
    返回 None 表示这个号还没凭据 / 解不开。
    """
    uid = str(unique_id or "").strip()
    if not uid:
        return None
    item = load_accounts().get(uid) or {}
    blob = str(item.get("cookies") or "")
    if not blob:
        return None
    try:
        data = json.loads(decrypt_text(blob))
    except Exception as error:
        _warn_decrypt(uid, error)
        return None
    if isinstance(data, dict) and data.get("version") == 2 and isinstance(data.get("storage_state"), dict):
        return data["storage_state"]
    if isinstance(data, dict) and isinstance(data.get("cookies"), list) and isinstance(data.get("origins"), list):
        return data
    cookies = _cookie_list_from_blob(data)
    if not cookies:
        return None
    return {"cookies": cookies, "origins": []}


def load_cookies(unique_id: str) -> list:
    """读取已保存的 Cookie（加密存的，这里解回来），顺手去掉 Playwright 不支持的字段。"""
    uid = str(unique_id or "").strip()
    if not uid:
        return []
    item = load_accounts().get(uid) or {}
    blob = str(item.get("cookies") or "")
    if not blob:
        return []
    try:
        return _cookie_list_from_blob(json.loads(decrypt_text(blob)))
    except Exception as error:
        # 以前这里静默返回 []：界面上只看到"未授权"，根本查不出是密钥变了还是 Cookie 坏了
        _warn_decrypt(uid, error)
        return []


def save_cookies(unique_id: str, cookies: list) -> None:
    """Cookie 加密后存进 accounts.json —— 直接 cat 那个文件看不到 sessionid。"""
    uid = str(unique_id or "").strip()
    if not uid:
        return
    cleaned = _clean_cookie_list(cookies)
    text = json.dumps(cleaned, ensure_ascii=False, separators=(",", ":"))
    save_account(uid, cookies=encrypt_text(text))
    invalidate_shot_cache()


def save_account_list(tasks: list, old_unique_id: str = "", new_unique_id: str = "") -> None:
    """把整份账号列表写回 accounts.json；Cookie 原样保留（改抖音号时跟着搬过去）。"""
    old_uid = str(old_unique_id or "").strip()
    new_uid = str(new_unique_id or "").strip()

    def _apply(accounts):
        if not isinstance(accounts, dict):
            accounts = {}
        out = {}
        for task in tasks:
            if not isinstance(task, dict):
                continue
            uid = str(task.get("unique_id") or "").strip()
            if not uid:
                continue
            item = accounts.get(uid)
            cookies = item.get("cookies") if isinstance(item, dict) else ""
            if not cookies and old_uid and new_uid and uid == new_uid:
                source = accounts.get(old_uid)
                if isinstance(source, dict):
                    cookies = source.get("cookies") or ""
            # 这个号自己的消息模板/发送间隔（改别的号时原样带过去，别被抹掉）
            settings = task.get("settings")
            if not isinstance(settings, dict):
                settings = (item.get("settings") if isinstance(item, dict) else None) or {}
            out[uid] = {
                "username": str(task.get("username") or uid),
                "targets": [str(x) for x in (task.get("targets") or [])],
                "times": [str(x) for x in (task.get("times") if isinstance(task.get("times"), list)
                                            else (item.get("times") if isinstance(item, dict) else []) or [])
                          if str(x).strip()],
                "settings": dict(settings),
                "cookies": cookies or "",
            }
        return out

    ACCOUNTS_STORE.update(_apply)


def _tz_text() -> str:
    """.env 里的时区（缓存一份，别每次取时间都读盘）。"""
    if not _TZ_CACHE["value"]:
        _TZ_CACHE["value"] = (parse_env().get("TZ") or "Asia/Shanghai").strip() or "Asia/Shanghai"
    return _TZ_CACHE["value"]


def now_text() -> str:
    """按 .env 里的 TZ 返回本地时间文本（面板容器默认是 UTC）。"""
    wanted = _tz_text()
    if wanted and os.environ.get("TZ") != wanted:
        os.environ["TZ"] = wanted
        try:
            time.tzset()
        except Exception:
            pass
    return time.strftime("%Y-%m-%d %H:%M:%S")


def load_sends(limit: int = 2) -> list:
    """读取程序写下的发送结果记录。"""
    try:
        data = json.loads(SEND_LOG.read_text(encoding="utf-8"))
    except Exception:
        return []
    runs = data.get("runs") if isinstance(data, dict) else None
    return runs[:limit] if isinstance(runs, list) else []


def schedule_spec_canon(text) -> str:
    """Normalize a daily send-time rule; blank or invalid input returns an empty string."""
    raw = str(text or "").strip().replace("：", ":")
    if not raw:
        return ""
    offset = re.fullmatch(r"(\d{1,2}:\d{1,2})(?:\s*)(?:±|\+/-)(\d{1,3})", raw)
    window = re.fullmatch(r"(\d{1,2}:\d{1,2})\s*-\s*(\d{1,2}:\d{1,2})", raw)

    def clock(value):
        parts = value.split(":")
        if len(parts) != 2 or not all(x.isdigit() for x in parts):
            return None
        hour, minute = map(int, parts)
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        return hour * 60 + minute

    if offset:
        center = clock(offset.group(1))
        delta = int(offset.group(2))
        if center is None or not (1 <= delta <= 720):
            return ""
        return "%02d:%02d±%d" % (center // 60, center % 60, delta)
    if window:
        start, end = clock(window.group(1)), clock(window.group(2))
        if start is None or end is None or end <= start:
            return ""
        return "%02d:%02d-%02d:%02d" % (start // 60, start % 60, end // 60, end % 60)

    parts = raw.split(":")
    if len(parts) == 1 and parts[0].isdigit():
        parts.append("0")
    if len(parts) not in (2, 3) or not all(x.isdigit() for x in parts):
        return ""
    nums = [int(x) for x in parts]
    if not (0 <= nums[0] <= 23 and 0 <= nums[1] <= 59 and
            (len(nums) == 2 or 0 <= nums[2] <= 59)):
        return ""
    return "%02d:%02d" % (nums[0], nums[1]) + (":%02d" % nums[2] if len(nums) == 3 and nums[2] else "")


def _parse_schedule_specs(raw, label="发送时间") -> tuple:
    specs = []
    for chunk in re.split(r"[\r\n,，;；]+", str(raw or "")):
        text = chunk.strip()
        if not text:
            continue
        item = schedule_spec_canon(text)
        if not item:
            return [], "%s「%s」格式不正确；可填 09:00、09:00-11:00 或 09:00±30" % (label, text)
        if item not in specs:
            specs.append(item)
    return sorted(specs), ""


def _clock_minute(text) -> int:
    match = re.match(r"^(\d{2}):(\d{2})", str(text or ""))
    return int(match.group(1)) * 60 + int(match.group(2)) if match else None


def schedule_spec_start_minutes(text):
    """Earliest minute a rule can resolve to; used by the availability checker."""
    canon = schedule_spec_canon(text)
    if not canon:
        return None
    offset = re.fullmatch(r"(\d{2}:\d{2})±(\d+)", canon)
    if offset:
        return max(0, _clock_minute(offset.group(1)) - int(offset.group(2)))
    window = re.fullmatch(r"(\d{2}:\d{2})-(\d{2}:\d{2})", canon)
    return _clock_minute(window.group(1)) if window else _clock_minute(canon)


def schedule_spec_is_random(text) -> bool:
    canon = schedule_spec_canon(text)
    return bool(re.search(r"(?:-|±|\+/-)", canon))


def _schedule_resolve_minute(text, date_str: str) -> int:
    """Resolve random rules deterministically so every scheduler process agrees for a day."""
    canon = schedule_spec_canon(text)
    if not canon:
        return None
    offset = re.fullmatch(r"(\d{2}:\d{2})±(\d+)", canon)
    window = re.fullmatch(r"(\d{2}:\d{2})-(\d{2}:\d{2})", canon)
    if offset:
        center, delta = _clock_minute(offset.group(1)), int(offset.group(2))
        start, end = max(0, center - delta), min(1439, center + delta)
    elif window:
        start, end = _clock_minute(window.group(1)), _clock_minute(window.group(2)) - 1
    else:
        return _clock_minute(canon)
    digest = hashlib.sha256((str(date_str) + "\x00" + canon).encode("utf-8")).digest()
    return start + (int.from_bytes(digest[:8], "big") % (end - start + 1))


def schedule_specs_today(specs, date_str="") -> list:
    date_str = str(date_str or now_text()[:10])
    out = []
    for spec in specs or []:
        minute = _schedule_resolve_minute(spec, date_str)
        out.append("%02d:%02d" % divmod(minute, 60) if minute is not None else "")
    return out


def _schedule_owner_map(users=None) -> dict:
    users = load_users() if users is None else users
    owners = {}
    for name, item in (users or {}).items():
        if isinstance(item, dict):
            for uid in item.get("accounts") or []:
                owners[str(uid)] = str(name)
    for uid in admin_owned():
        owners[str(uid)] = PANEL_USERNAME
    return owners


def _schedule_account_active(uid: str, owners=None, users=None) -> bool:
    owner = (owners or {}).get(str(uid or ""), "")
    if not owner:
        return False
    if owner == PANEL_USERNAME:
        return True
    users = load_users() if users is None else users
    return bool(_access_record(users.get(owner) or {}).get("active"))


def _estimate_task_minutes(task: dict, env=None) -> int:
    """Conservative display estimate based on target count and configured pauses."""
    targets = [x for x in (task.get("targets") or []) if str(x).strip()]
    count = max(1, len(targets))
    settings = task.get("settings") if isinstance(task.get("settings"), dict) else {}
    env = parse_env() if env is None else env
    try:
        delay_max = float(settings.get("delay_max") if settings.get("delay_max") not in (None, "")
                          else env.get("RANDOM_DELAY_MAX", 0))
    except (TypeError, ValueError):
        delay_max = 0
    return min(240, max(5, int(2 + count * 1.5 + max(0, delay_max) * max(0, count - 1) / 60 + 0.999)))


def _schedule_rows(tasks=None, date_str="", owners=None, exclude_uid="") -> list:
    tasks = load_tasks() if tasks is None else tasks
    date_str = str(date_str or now_text()[:10])
    users = load_users()
    owners = _schedule_owner_map(users) if owners is None else owners
    env = parse_env()
    rows = []
    for task in tasks:
        uid = str(task.get("unique_id") or "")
        targets = [x for x in (task.get("targets") or []) if str(x).strip()]
        specs = [schedule_spec_canon(x) for x in (task.get("times") or [])]
        specs = [x for x in specs if x]
        if uid == str(exclude_uid or "") or not targets or not specs or not _schedule_account_active(uid, owners, users):
            continue
        estimate = _estimate_task_minutes(task, env)
        for spec, today in zip(specs, schedule_specs_today(specs, date_str)):
            minute = _clock_minute(today)
            if minute is None:
                continue
            rows.append({
                "time": today,
                "minute": minute,
                "unique_id": uid,
                "username": str(task.get("username") or uid),
                "estimate_minutes": estimate,
                "spec": spec,
            })
    return rows


def _intervals_overlap(start_a, duration_a, start_b, duration_b) -> bool:
    # Compare neighbouring days as a task can run across midnight.
    for shift in (-1440, 0, 1440):
        if start_a < start_b + shift + duration_b + 5 and start_b + shift < start_a + duration_a + 5:
            return True
    return False


def _schedule_conflicts(specs, uid="", targets=None, delay_max=None, tasks=None, date_str="") -> list:
    tasks = load_tasks() if tasks is None else tasks
    date_str = str(date_str or now_text()[:10])
    rows = _schedule_rows(tasks, date_str, exclude_uid=uid)
    targets = [x for x in (targets or []) if str(x).strip()]
    estimate = _estimate_task_minutes({"targets": targets, "settings": {"delay_max": delay_max}})
    candidate_today = schedule_specs_today(specs, date_str)
    conflicts = []
    for proposed in candidate_today:
        start = _clock_minute(proposed)
        if start is None:
            continue
        for row in rows:
            if _intervals_overlap(start, estimate, row["minute"], row["estimate_minutes"]):
                conflicts.append({
                    "time": proposed,
                    "account": row["username"],
                    "unique_id": row["unique_id"],
                    "other_time": row["time"],
                    "estimate_minutes": row["estimate_minutes"],
                })
    return conflicts


def _recommended_schedule_time(tasks=None, uid="", targets=None, delay_max=None, rows=None) -> str:
    tasks = load_tasks() if tasks is None else tasks
    date_str = now_text()[:10]
    now_minute = _clock_minute(now_text()[11:16])
    estimate = _estimate_task_minutes({"targets": targets or [], "settings": {"delay_max": delay_max}})
    # Start ten minutes from now, use five-minute boundaries, and stay in daytime when possible.
    first = max(6 * 60, ((now_minute + 14) // 5) * 5)
    candidates = list(range(first, 23 * 60 + 1, 5)) + list(range(6 * 60, first, 5))
    rows = _schedule_rows(tasks, date_str, exclude_uid=uid) if rows is None else rows
    for candidate in candidates:
        if all(not _intervals_overlap(candidate, estimate, row["minute"], row["estimate_minutes"])
               for row in rows):
            return "%02d:%02d" % divmod(candidate, 60)
    return ""


def schedule_check_payload(payload: dict) -> dict:
    uid = str(payload.get("unique_id") or "").strip()
    exclude_uid = str(payload.get("orig_unique_id") or uid).strip()
    specs, error = _parse_schedule_specs(payload.get("schedule_times"), "发送时间")
    if error:
        return {"ok": False, "error": error}
    targets = re.split(r"[\r\n,]+", str(payload.get("targets") or ""))
    try:
        delay_max = float(str(payload.get("delay_max") or 0))
    except (TypeError, ValueError):
        delay_max = 0
    tasks = load_tasks()
    conflicts = _schedule_conflicts(specs, exclude_uid, targets, delay_max, tasks)
    rows = _schedule_rows(tasks, exclude_uid=exclude_uid)
    return {
        "ok": True,
        "conflicts": conflicts,
        "occupied": [{k: row[k] for k in ("time", "username", "unique_id", "estimate_minutes")} for row in rows],
        "estimate_minutes": _estimate_task_minutes({"targets": targets, "settings": {"delay_max": delay_max}}),
        "recommended": _recommended_schedule_time(tasks, exclude_uid, targets, delay_max, rows=rows),
    }


# --------------------------------------------------------------------------
# 全局默认发送内容 / 一键推平到所有账号
# --------------------------------------------------------------------------
def _parse_delay_pair(payload: dict):
    """发送间隔：返回 (min, max, 错误)。"""
    try:
        low = int(float(str(payload.get("delay_min", 0) or 0)))
        high = int(float(str(payload.get("delay_max", 0) or 0)))
    except (TypeError, ValueError):
        return 0, 0, "发送间隔必须是数字（秒）"
    if not (0 <= low <= 600 and 0 <= high <= 600):
        return 0, 0, "发送间隔请填 0-600 秒"
    if high < low:
        low, high = high, low
    return low, high, ""


def _parse_hitokoto(text) -> tuple:
    """一言类型：返回 (列表, 错误)。空串 = 不设置（用内置默认）。"""
    raw = str(text or "").strip()
    if not raw:
        return [], ""
    try:
        parsed = json.loads(raw)
    except Exception:
        return [], "一言类型必须是 JSON 数组"
    if not isinstance(parsed, list) or not parsed:
        return [], "一言类型必须是非空 JSON 数组"
    return [str(x) for x in parsed], ""


def save_global_defaults(payload: dict) -> dict:
    """保存「全局默认发送内容」：消息模板 / 一言类型 / 默认间隔。

    只影响**没有单独配置过**的抖音号 —— 那些号在 accounts.json 里 template 等字段是空的，
    发送端会回落到 .env 里的这份全局值。已经单独配过的号不受影响。
    """
    template = str(payload.get("message_template", "") or "").strip()
    kinds, error = _parse_hitokoto(payload.get("hitokoto_types"))
    if error:
        return {"ok": False, "error": error}
    low, high, error = _parse_delay_pair(payload)
    if error:
        return {"ok": False, "error": error}

    mapping = {
        "MESSAGE_TEMPLATE": template or DEFAULT_TEMPLATE,
        "RANDOM_DELAY_MIN": str(low),
        "RANDOM_DELAY_MAX": str(high),
    }
    if kinds:
        # 存成紧凑 JSON：.env 里带引号的长 JSON 容易让 dotenv 解析出岔子，这里不换行
        mapping["HITOKOTO_TYPES"] = json.dumps(kinds, ensure_ascii=False, separators=(",", ":"))
    update_env(mapping)
    log_force("全局默认", "更新全局发送内容")
    return {
        "ok": True,
        "message": "全局默认已保存（没单独配置过的抖音号立即生效）",
    }


def push_global_to_accounts(payload: dict) -> dict:
    """把全局默认值「推平」到所有抖音号自己的配置上（按勾选项覆盖）。

    这是破坏性操作：会覆盖掉每个号自己的设置，所以前端会先弹确认框。
    只写 accounts.json，不动 .env。
    """
    want_template = bool(payload.get("template"))
    want_hitokoto = bool(payload.get("hitokoto"))
    want_delay = bool(payload.get("delay"))
    picked = [n for n, w in (("消息模板", want_template), ("一言类型", want_hitokoto),
                             ("发送间隔", want_delay)) if w]
    if not picked:
        return {"ok": False, "error": "先勾选要推平的项目（消息模板 / 一言类型 / 发送间隔）"}

    values = parse_env()
    template = str(values.get("MESSAGE_TEMPLATE", DEFAULT_TEMPLATE) or DEFAULT_TEMPLATE).strip() \
        or DEFAULT_TEMPLATE
    kinds, _error = _parse_hitokoto(values.get("HITOKOTO_TYPES"))
    try:
        glo = int(float(str(values.get("RANDOM_DELAY_MIN", 0) or 0)))
        ghi = int(float(str(values.get("RANDOM_DELAY_MAX", 0) or 0)))
    except (TypeError, ValueError):
        glo, ghi = 0, 0
    tasks = load_tasks()
    changed = 0
    for task in tasks:
        if not isinstance(task, dict):
            continue
        settings = task.get("settings")
        if not isinstance(settings, dict):
            settings = {}
        else:
            settings = dict(settings)
        if want_template:
            settings["template"] = template
        if want_hitokoto:
            settings["hitokoto_types"] = list(kinds)
        if want_delay:
            settings["delay_min"] = glo
            settings["delay_max"] = ghi
        task["settings"] = settings
        changed += 1

    save_account_list(tasks)   # Cookie 原样保留
    invalidate_shot_cache()
    log_force("推平全局", "把全局默认的「%s」覆盖到 %d 个抖音号" % ("、".join(picked), changed))
    return {
        "ok": True,
        "changed": changed,
        "message": "已把全局默认的「%s」覆盖到 %d 个抖音号" % ("、".join(picked), changed),
    }


# --------------------------------------------------------------------------
# 公告 + 管理员联系方式（显示在所有用户的控制台最上方）
#
# 存的是一份独立的小 JSON：这东西只有面板展示用，调度侧完全不需要，
# 所以不进 .env（免得 worker 也跟着多背一份和自己无关的配置）。
# 读坏了就当"没有公告"处理，绝不能让一条坏 JSON 把整个面板拦在启动阶段。
# --------------------------------------------------------------------------
NOTICE_MAX_TEXT = 3000
NOTICE_MAX_CONTACT = 600


def load_notice() -> dict:
    """读公告。任何异常都退化成"没有公告"，不往外抛。"""
    empty = {"contact": "", "text": "", "updated_at": ""}
    try:
        data = json.loads(NOTICE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return dict(empty)
    if not isinstance(data, dict):
        return dict(empty)
    return {
        "contact": str(data.get("contact") or ""),
        "text": str(data.get("text") or ""),
        "updated_at": str(data.get("updated_at") or ""),
    }


def notice_version(contact: str, text: str) -> str:
    """内容指纹：前端拿它判断"公告是不是换过了"。

    用户点过「知道了」之后本地记的是这个值，内容一变指纹就变，
    公告会重新出现 —— 否则管理员改了公告，老用户永远看不到。
    """
    return hashlib.md5(("%s\x00%s" % (contact, text)).encode("utf-8")).hexdigest()[:10]


def notice_payload() -> dict:
    notice = load_notice()
    return {
        "contact": notice["contact"],
        "text": notice["text"],
        "updated_at": notice["updated_at"],
        "version": notice_version(notice["contact"], notice["text"]),
    }


def save_notice(payload: dict) -> dict:
    """保存公告与联系方式（只有管理员能调到，路由那头已经拦过一道）。"""
    text = str(payload.get("text") or "").strip()
    contact = str(payload.get("contact") or "").strip()
    if len(text) > NOTICE_MAX_TEXT:
        return {"ok": False, "error": "公告正文最多 %d 字，现在 %d 字" % (NOTICE_MAX_TEXT, len(text))}
    if len(contact) > NOTICE_MAX_CONTACT:
        return {"ok": False, "error": "联系方式最多 %d 字，现在 %d 字" % (NOTICE_MAX_CONTACT, len(contact))}
    stamp = now_text()
    atomic_write(
        NOTICE_PATH,
        json.dumps(
            {"text": text, "contact": contact, "updated_at": stamp},
            ensure_ascii=False,
            indent=2,
        ),
        0o600,
    )
    if text or contact:
        log_force("公告", "更新公告/联系方式（%d 字正文）" % len(text))
    else:
        log_force("公告", "清空公告/联系方式")
    return {
        "ok": True,
        "message": "公告已保存" if (text or contact) else "公告已清空（用户控制台不再显示公告条）",
        "version": notice_version(contact, text),
        "updated_at": stamp,
    }


# --------------------------------------------------------------------------
# 定向消息（管理员单独发给指定用户的站内通知）
#
# 和公告的区别：公告是"写给全体"的，全站只有一份内容；定向消息是一条一条的，
# 每条自带收件人名单，所以必须存成列表。同样是一份独立小 JSON，不进 .env
# （调度侧完全用不到）。读坏了就当"没有消息"，绝不让一份坏 JSON 把面板拦在启动阶段。
#
# 已读回执：每条记录里的 read 是 {用户名: 首次已读时间}。
# 用户控制台把消息**铺出来**的那一刻就会回传「我看过了」，不靠用户点不点按钮；
# 点「知道了」只是把它从自己界面上收起来，那个状态存在浏览器本地（localStorage），
# 所以换台设备/换个浏览器还会再看到一次 —— 服务端的 read 才是「谁真的看过」的答案。
#
# 硬边界：普通用户的 /api/status 只会拿到**自己收件箱**里的那些，
# targets / sent 这两个字段只给管理员。绝不能出现"别人的消息"。
# --------------------------------------------------------------------------
MESSAGE_MAX_TEXT = 1000  # 单条正文上限（比公告短：这是点名推送，不是公告板）
MESSAGE_MAX_RECIPIENTS = 200  # 一条最多发给多少人
MESSAGE_KEEP = 200  # 最多留多少条历史（超了就丢最老的）
MESSAGE_INBOX_MAX = 50  # 每次最多下发多少条收件箱（status 每 2 秒轮询一次，得有上限）


def load_messages() -> list:
    """读所有定向消息。任何异常都退化成空列表，不往外抛。"""
    try:
        data = json.loads(MESSAGES_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    raw = data.get("messages")
    if not isinstance(raw, list):
        return []
    items = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        rid = str(row.get("id") or "").strip()
        text = str(row.get("text") or "")
        if not rid or not text:
            continue  # 没有 id 或没有正文的残条一律跳过，不让它污染下发内容
        recipients = [str(x) for x in (row.get("recipients") or []) if str(x).strip()]
        read = row.get("read")
        if not isinstance(read, dict):
            read = {}
        items.append(
            {
                "id": rid,
                "from": str(row.get("from") or ""),
                "text": text,
                "recipients": recipients,
                "created_at": str(row.get("created_at") or ""),
                "read": {str(k): str(v) for k, v in read.items()},
            }
        )
    return items


def _save_messages(items: list) -> None:
    atomic_write(
        MESSAGES_PATH,
        json.dumps({"messages": items}, ensure_ascii=False, indent=2),
        0o600,
    )


def _new_message_id(text: str, recipients: list, stamp: str) -> str:
    """短 id。掺了随机数，同内容同秒也不会撞；前端拿它当"这条消息唯一性"的凭据。"""
    seed = "%s\x00%s\x00%s\x00%s" % (text, ",".join(recipients), stamp, os.urandom(8).hex())
    return "m" + hashlib.md5(seed.encode("utf-8")).hexdigest()[:12]


def message_targets() -> list:
    """可以接收定向消息的人：所有注册用户 + 管理员自己（去重、排序）。"""
    names = set()
    for name in load_users().keys():
        name = str(name or "").strip()
        if name:
            names.add(name)
    if PANEL_USERNAME:
        names.add(str(PANEL_USERNAME))
    return sorted(names)


def inbox_for(user: str) -> list:
    """这个人的收件箱：只挑把他列进 recipients 的那些，按时间倒序。"""
    user = str(user or "")
    if not user:
        return []
    box = []
    for item in load_messages():
        if user not in item["recipients"]:
            continue
        box.append(
            {
                "id": item["id"],
                "from": item["from"],
                "text": item["text"],
                "created_at": item["created_at"],
                "read": bool(item["read"].get(user)),
                "read_at": item["read"].get(user, ""),
            }
        )
    box.sort(key=lambda row: (row["created_at"], row["id"]), reverse=True)
    return box[:MESSAGE_INBOX_MAX]


def message_payload(user: str, is_admin: bool) -> dict:
    """下发给人看的那份。

    普通用户只拿到自己的收件箱 —— **绝不下发别人的消息**（硬边界）。
    管理员额外拿到「发件箱」：每条消息的收件人名单 + 谁看了谁没看。
    """
    box = inbox_for(user)
    payload = {
        "inbox": box,
        "unread": len([row for row in box if not row["read"]]),
        "sent": [],
        "targets": message_targets() if is_admin else [],
        "max_text": MESSAGE_MAX_TEXT,
    }
    if not is_admin:
        return payload
    sent = []
    for item in load_messages():
        read = item["read"]
        detail = [
            {"name": name, "read": bool(read.get(name)), "read_at": read.get(name, "")}
            for name in item["recipients"]
        ]
        sent.append(
            {
                "id": item["id"],
                "from": item["from"],
                "text": item["text"],
                "recipients": list(item["recipients"]),
                "created_at": item["created_at"],
                "read_count": len([row for row in detail if row["read"]]),
                "read_detail": detail,
            }
        )
    sent.sort(key=lambda row: (row["created_at"], row["id"]), reverse=True)
    payload["sent"] = sent
    return payload


def send_message(payload: dict, sender: str) -> dict:
    """发一条定向消息（只有管理员能调到，路由那头已经拦过一道）。"""
    text = str(payload.get("text") or "").strip()
    if not text:
        return {"ok": False, "error": "消息内容不能为空"}
    if len(text) > MESSAGE_MAX_TEXT:
        return {
            "ok": False,
            "error": "消息最多 %d 字，现在 %d 字" % (MESSAGE_MAX_TEXT, len(text)),
        }
    allowed = message_targets()
    picked = []
    for raw in payload.get("recipients") or []:
        name = str(raw or "").strip()
        if not name or name in picked:
            continue
        if name not in allowed:
            return {"ok": False, "error": "没有这个用户：%s" % name}
        picked.append(name)
    if not picked:
        return {"ok": False, "error": "至少要选一个收件人"}
    if len(picked) > MESSAGE_MAX_RECIPIENTS:
        return {"ok": False, "error": "一条消息最多发给 %d 个人" % MESSAGE_MAX_RECIPIENTS}
    stamp = now_text()
    item = {
        "id": _new_message_id(text, picked, stamp),
        "from": str(sender or ""),
        "text": text,
        "recipients": picked,
        "created_at": stamp,
        "read": {},
    }
    items = load_messages()
    items.insert(0, item)
    del items[MESSAGE_KEEP:]
    _save_messages(items)
    log_force("定向消息", "发给 %d 人（%s）：%s" % (len(picked), "、".join(picked), text[:40]))
    return {
        "ok": True,
        "message": "已发给 %d 个用户" % len(picked),
        "id": item["id"],
        "created_at": stamp,
        "recipients": picked,
    }


def delete_message(payload: dict) -> dict:
    """删掉一条定向消息（所有收件人的控制台上同时消失）。"""
    mid = str(payload.get("id") or "").strip()
    if not mid:
        return {"ok": False, "error": "没指定要删哪条消息"}
    items = load_messages()
    kept = [item for item in items if item["id"] != mid]
    if len(kept) == len(items):
        return {"ok": False, "error": "这条消息已经不在了（可能刚被别人删过）"}
    _save_messages(kept)
    log_force("定向消息", "删除一条消息（%s）" % mid)
    return {"ok": True, "message": "已删除这条消息"}


def mark_message_read(payload: dict, user: str) -> dict:
    """把「我」收件箱里的消息标成已读。

    只能标自己的：名单从请求体来的 id 只是"挑哪几条"，
    「是谁在读」永远取自会话，所以改不到别人的已读状态。
    传 ids 为空 = 把我收到的全部标成已读。
    已经标过的保持**首次**已读时间不动（不会因为再刷一次页面就刷新时间）。
    """
    user = str(user or "")
    if not user:
        return {"ok": False, "error": "未登录"}
    ids = payload.get("ids")
    if isinstance(ids, str):
        ids = [ids]
    if not isinstance(ids, list):
        ids = []
    wanted = set(str(x) for x in ids if str(x).strip())
    items = load_messages()
    stamp = now_text()
    changed = 0
    for item in items:
        if wanted and item["id"] not in wanted:
            continue
        if user in item["recipients"] and not item["read"].get(user):
            item["read"][user] = stamp
            changed += 1
    if changed:
        _save_messages(items)
    box = inbox_for(user)
    return {
        "ok": True,
        "changed": changed,
        "unread": len([row for row in box if not row["read"]]),
    }


def load_relogin() -> dict:
    """程序发现登录失效时会留一个标记文件。"""
    try:
        data = json.loads(RELOGIN_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def task_login_failures() -> dict:
    """执行任务途中程序自己发现登录失效的号（按抖音号索引，每个号只留最近一次）。

    「检测」按钮的结果是上一次点击的快照，可能已经过去很久；真正每天都在跑、
    每天都在看登录态的，是发送任务本身。所以账号徽章必须同时看这里，
    否则会出现「发送记录写着登录已失效、账号那一栏却还是登录正常」。
    """
    out = {}
    for run in load_sends(SEND_MAX_ADMIN):
        if not isinstance(run, dict):
            continue
        if str(run.get("status") or "") != "no_login":
            continue
        uid = str(run.get("unique_id") or run.get("account") or "").strip()
        if not uid or uid in out:
            continue  # runs 是新的在前，第一个命中的就是最近一次
        out[uid] = {
            "at": str(run.get("at") or ""),
            "detail": str(run.get("detail") or ""),
            "account": str(run.get("account") or ""),
        }
    return out


def real_check(checks: dict, uid: str):
    """从 checks 里取出「真正的登录检测结果」；扫码授权留下的记录不算检测。

    checks[uid] 这个格子被两拨人写过：
      * 登录检测（LoginChecker._finish）—— ok / message / at，外加会话数、好友匹配情况
      * 扫码授权成功（BrowserSession._store）—— 带 source="qr"
    授权成功只说明「Cookie 存下来了」，不代表这个号还能发消息。以前两者混在一个格子里、
    界面又只看 ok，于是刚扫码完的号立刻顶着绿色的「登录正常」，等真去发才发现早掉线了。
    所以读的时候统一走这里：source=="qr" 一律当作「还没检测过」。
    """
    item = (checks or {}).get(str(uid))
    if not isinstance(item, dict):
        return None
    if str(item.get("source") or "") == "qr":
        return None
    return item


def task_login_failure_for(uid: str, failures: dict, checks: dict, saved_at: dict) -> dict:
    """任务报过登录失效、且之后没有再成功登录/检测通过 —— 返回那条失败记录，否则 {}。

    时间戳都是 "YYYY-MM-DD HH:MM:SS"，可以直接比字符串大小。
    判定用严格大于：只有**晚于**失败时间的成功记录才算恢复，
    宁可多提示一次登录失效，也不要再出现「明明失效了却显示正常」。
    """
    fail = (failures or {}).get(str(uid))
    if not fail:
        return {}
    fail_at = str(fail.get("at") or "")
    recovered = ""
    # 只认真正的登录检测（扫码授权不算，见 real_check）
    check = real_check(checks, uid)
    if isinstance(check, dict) and check.get("ok"):
        recovered = str(check.get("at") or "")
    saved = str((saved_at or {}).get(str(uid)) or "")
    if saved > recovered:
        recovered = saved
    if recovered and recovered > fail_at:
        return {}  # 之后已经恢复过了
    return fail


def register_allowed() -> bool:
    """别人能不能自己点「注册一个」建账号（管理员可在「用户管理」里关掉）。"""
    try:
        return bool(load_state().get("allow_register", True))
    except Exception:
        return True


def load_state() -> dict:
    data = STATE_STORE.read()
    return data if isinstance(data, dict) else {}


def save_state(patch: dict) -> dict:
    """合并写入面板状态。整段在锁里完成，20 个请求同时写也不会互相覆盖。"""

    def _merge(state):
        if not isinstance(state, dict):
            state = {}
        for key, value in patch.items():
            if isinstance(value, dict) and isinstance(state.get(key), dict):
                state[key].update(value)
            else:
                state[key] = value
        return state

    return STATE_STORE.update(_merge)


def record_admin_login(ip: str) -> None:
    """记下管理员这次从哪登录的。

    管理员不是注册用户，没有 panel-users.json 里的记录可写，
    所以单独存在面板状态里（管理界面会显示）。
    """
    ip = str(ip or "").strip()
    if not ip:
        return
    try:
        save_state({"admin_login": {"at": now_text(), "ip": ip}})
    except Exception:
        pass


def admin_owned() -> list:
    """管理员自己名下的抖音号（存在 panel-state.json 里；管理员不是注册用户）"""
    try:
        return [str(x) for x in (load_state().get("admin_accounts") or []) if str(x).strip()]
    except Exception:
        return []


def _save_admin_owned(items) -> None:
    seen = []
    for item in items:
        text = str(item or "").strip()
        if text and text not in seen:
            seen.append(text)
    save_state({"admin_accounts": seen})


def bind_admin_account(unique_id: str) -> dict:
    """把抖音号绑到管理员自己名下（普通用户依然动不了，只是不再是"没人认领"）"""
    unique_id = str(unique_id or "").strip()
    if not unique_id:
        return {"ok": False, "error": "请填写要绑定的抖音号"}
    if unique_id not in {str(t.get("unique_id") or "") for t in load_tasks()}:
        return {"ok": False, "error": "还没有「%s」这个抖音号，先在左边「1 账号」里把它建出来" % unique_id}
    def _apply(users):
        for item in users.values():
            if isinstance(item, dict) and unique_id in [str(x) for x in (item.get("accounts") or [])]:
                item["accounts"] = [x for x in (item.get("accounts") or []) if str(x) != unique_id]
        return users

    update_users(_apply)
    _save_admin_owned(admin_owned() + [unique_id])
    log_force("绑定抖音号", "%s → %s（管理员自己）" % (unique_id, PANEL_USERNAME))
    return {"ok": True, "message": "已把 %s 绑到管理员（你自己）名下" % unique_id}


def drop_account_from_owners(unique_id: str) -> None:
    """账号被删掉后，把它从所有人（含管理员）名下摘掉，别留悬空的号"""
    uid = str(unique_id or "").strip()
    if not uid:
        return

    def _apply(users):
        for item in users.values():
            if isinstance(item, dict) and uid in [str(x) for x in (item.get("accounts") or [])]:
                item["accounts"] = [x for x in (item.get("accounts") or []) if str(x) != uid]
        return users

    update_users(_apply)
    if uid in admin_owned():
        _save_admin_owned([x for x in admin_owned() if x != uid])


# 判断抖音页面处于什么状态：有没有好友列表、弹没弹登录框
PAGE_STATE_JS = """() => {
  const count = (sel) => document.querySelectorAll(sel).length;
  const text = document.body ? (document.body.innerText || "") : "";
  return {
    conversations: count(".conversationConversationItemwrapper"),
    titles: Array.from(document.querySelectorAll(".conversationConversationItemtitle"))
              .map(function(e){ return (e.innerText || "").trim(); }),
    loginDialog: /扫码登录/.test(text) && /验证码登录|密码登录/.test(text)
  };
}"""

SCROLL_JS = """(sel) => { const el = document.querySelector(sel); if (el) { el.scrollTop = el.scrollTop + el.clientHeight; } }"""


# --------------------------------------------------------------------------
# 会话
# --------------------------------------------------------------------------
def make_token(user: str = "admin", scope: str = "", admin: bool = True, key: str = "") -> str:
    """登录令牌：只记「是谁、是不是管理员」，能管哪些抖音号每次现查（绑号后不用重登）。"""
    payload = {
        "u": str(user or "admin"),
        "a": 1 if admin else 0,
        "k": str(key or ""),
        "n": secrets.token_urlsafe(9),
        "iat": int(time.time()),
    }
    raw = base64.urlsafe_b64encode(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).decode("ascii").rstrip("=")
    sig = hmac.new(SESSION_SECRET, raw.encode("utf-8"), hashlib.sha256).hexdigest()[:32]
    return raw + "." + sig


def token_claims(token: str) -> dict:
    """校验签名并取回登录信息：{u: 登录名, a: 是不是管理员}"""
    if not token or "." not in token:
        return {}
    raw, sig = token.rsplit(".", 1)
    expect = hmac.new(SESSION_SECRET, raw.encode("utf-8"), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(sig, expect):
        return {}
    try:
        data = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    try:
        issued = int(data.get("iat") or 0)
    except (TypeError, ValueError):
        return {}
    # 过期的、或者签发时间明显不对的令牌，一律不认
    if not issued or abs(time.time() - issued) > SESSION_TTL:
        return {}
    # 退出登录时拉黑的令牌：签名再对也不认（防“把令牌拄走后还能用”）
    if token_id(token) in revoked_tokens():
        return {}
    return data


def token_ok(token: str) -> bool:
    return bool(token_claims(token))


# --------------------------------------------------------------------------
# 控制台用户：谁都能自己注册；注册后在面板里绑自己的抖音号
#   /app/config/panel-users.json 结构：
#   {"users": {"alice": {"salt": "...", "hash": "...", "at": "注册时间",
#                        "last_login": "最近登录", "accounts": ["抖音号"]}}}
# --------------------------------------------------------------------------
MAX_ACCOUNTS_PER_USER = 1  # 除管理员外，每个控制台用户只能绑 1 个抖音号
USERNAME_RE = r"[A-Za-z0-9_.@-]{2,32}"
# 注册要填的手机号：只要中国大陆 11 位手机号。不要验证码、不校验唯一，仅作联系方式。
PHONE_RE = r"1[3-9][0-9]{9}"

# 账号时长：老用户没有 access 字段，按历史约定永久有效；新注册用户从创建时起试用 3 天。
TRIAL_DAYS = 3
REDEEM_DURATIONS = {
    "7": {"days": 7, "label": "一星期"},
    "14": {"days": 14, "label": "两星期"},
    "30": {"days": 30, "label": "一个月"},
    "60": {"days": 60, "label": "两个月"},
}
REDEEM_CODE_RE = r"DSF-[A-Z0-9]{4}(?:-[A-Z0-9]{4}){2}"


def _epoch(value, default=0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return int(default)


def _format_epoch(value) -> str:
    stamp = _epoch(value)
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(stamp)) if stamp else ""


def _access_record(item: dict) -> dict:
    """统一读取账号时长；缺少 access 的历史记录保持永久，不误伤现有用户。"""
    raw = item.get("access") if isinstance(item, dict) else None
    if not isinstance(raw, dict) or not raw:
        return {"unlimited": True, "active": True, "trial": False, "expires_at": 0}
    unlimited = bool(raw.get("unlimited"))
    expires = _epoch(raw.get("expires_at"))
    return {
        "unlimited": unlimited,
        "active": unlimited or expires > int(time.time()),
        "trial": bool(raw.get("trial")),
        "expires_at": expires,
        "trial_expires_at": _epoch(raw.get("trial_expires_at")),
        "created_at": _epoch(raw.get("created_at")),
    }


def subscription_info(name: str) -> dict:
    """给用户界面和发送端看的订阅状态，不返回任何密码或内部字段。"""
    item = load_users().get(str(name or ""))
    access = _access_record(item or {})
    now = int(time.time())
    remaining = 0 if access["unlimited"] else max(0, access["expires_at"] - now)
    days = remaining // 86400
    hours = (remaining % 86400) // 3600
    if access["unlimited"]:
        status, text = "unlimited", "永久有效"
    elif remaining > 0:
        status = "trial" if access.get("trial") and access["expires_at"] <= access.get("trial_expires_at", 0) else "active"
        text = "试用期还剩 %d 天 %d 小时" % (days, hours) if status == "trial" else "还剩 %d 天 %d 小时" % (days, hours)
    else:
        status, text = "expired", "已到期"
    return {
        "status": status,
        "active": bool(access["active"]),
        "unlimited": bool(access["unlimited"]),
        "trial": bool(access.get("trial")),
        "expires_at": access["expires_at"],
        "expires_text": _format_epoch(access["expires_at"]),
        "remaining_seconds": remaining,
        "remaining_text": text,
    }


def _new_trial_access(created_at: int = None) -> dict:
    created = int(created_at or time.time())
    expires = created + TRIAL_DAYS * 86400
    return {
        "unlimited": False,
        "trial": True,
        "created_at": created,
        "trial_expires_at": expires,
        "expires_at": expires,
        "updated_at": created,
    }


def _pass_hash(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 60000).hex()


def _norm_users(raw) -> dict:
    if not isinstance(raw, dict):
        return {}
    users = {}
    for name, item in raw.items():
        if not isinstance(item, dict):
            continue
        record = dict(item)
        owned = [str(x) for x in (record.get("accounts") or []) if str(x).strip()]
        legacy = str(record.pop("unique_id", "") or "")  # 兼容更早的"一个登录名绑一个号"格式
        if legacy and legacy not in owned:
            owned.append(legacy)
        record["accounts"] = owned
        users[str(name)] = record
    return users


def load_users() -> dict:
    data = USERS_STORE.read()
    return _norm_users(data.get("users") if isinstance(data, dict) else None)


def save_users(users: dict) -> None:
    USERS_STORE.write({"users": users})


def update_users(fn) -> dict:
    """原子的"读 → 改 → 写"：两个请求同时给同一个用户加号时，不会丢更新。"""

    def _apply(data):
        users = _norm_users(data.get("users") if isinstance(data, dict) else None)
        result = fn(users)
        return {"users": result if isinstance(result, dict) else users}

    written = USERS_STORE.update(_apply)
    return _norm_users(written.get("users") if isinstance(written, dict) else None)


def user_accounts(name: str) -> list:
    """这个用户名下绑了哪些抖音号"""
    item = load_users().get(str(name or ""))
    if not isinstance(item, dict):
        return []
    return [str(x) for x in (item.get("accounts") or []) if str(x).strip()]


def owner_of(unique_id: str, users: dict = None) -> str:
    """这个抖音号现在绑在谁名下（没人绑就返回空字符串）"""
    target = str(unique_id or "").strip()
    if not target:
        return ""
    table = load_users() if users is None else users
    for name, item in table.items():
        if isinstance(item, dict) and target in [str(x) for x in (item.get("accounts") or [])]:
            return str(name)
    if target in admin_owned():
        return PANEL_USERNAME
    return ""


def _name_error(name: str) -> str:
    if not re.fullmatch(USERNAME_RE, str(name or "")):
        return "登录名请用 2-32 位字母、数字或 _ . @ -"
    if str(name) == PANEL_USERNAME:
        return "这个登录名已经被管理员占用了，换一个"
    return ""


def _phone_error(phone: str, required: bool = True) -> str:
    """手机号校验。required=False 时允许留空（管理员手动建号不必填）。"""
    phone = str(phone or "").strip()
    if not phone:
        return "请填手机号" if required else ""
    if not re.fullmatch(PHONE_RE, phone):
        return "手机号要填 11 位数字（比如 13812345678）"
    return ""


def create_user(name: str, password: str, again=None, phone: str = "",
                require_phone: bool = False) -> dict:
    """注册新用户。

    phone 是手机号：自注册必须填（require_phone=True）；
    管理员手动建号不传，保持原样不强制。
    """
    name = str(name or "").strip()
    password = str(password or "")
    phone = str(phone or "").strip()
    error = _name_error(name)
    if error:
        return {"ok": False, "error": error}
    phone_error = _phone_error(phone, required=require_phone)
    if phone_error:
        return {"ok": False, "error": phone_error}
    if again not in (None, "") and str(again) != password:
        return {"ok": False, "error": "两次输入的密码不一样"}
    if len(password) < 6:
        return {"ok": False, "error": "密码至少 6 位"}
    if name in load_users():
        return {"ok": False, "error": "这个登录名已经有人用了，换一个"}
    salt = secrets.token_hex(16)
    made = {}
    created_at = int(time.time())

    def _apply(users):
        if name in users:
            return users
        users[name] = {
            "salt": salt,
            "hash": _pass_hash(password, salt),
            "at": now_text(),
            "last_login": now_text(),
            "phone": phone,
            "accounts": [],
            "key": secrets.token_hex(8),  # 令牌里带上它：用户被删掉后，旧登录立刻失效
            "access": _new_trial_access(created_at),
        }
        made["key"] = users[name]["key"]
        return users

    update_users(_apply)
    if not made:
        return {"ok": False, "error": "这个登录名已经有人用了，换一个"}
    log_force("新用户注册", name)
    return {"ok": True, "user": name, "key": made["key"], "message": "注册成功"}


def verify_user(name: str, password: str, ip: str = "") -> dict:
    """校验注册用户登录，成功返回 {"user": 登录名}，失败返回 {}"""
    name = str(name or "")
    item = load_users().get(name)
    if not isinstance(item, dict) or not password:
        return {}
    salt = str(item.get("salt") or "")
    expect = str(item.get("hash") or "")
    if not salt or not expect:
        return {}
    if not hmac.compare_digest(_pass_hash(password, salt), expect):
        return {}
    def _touch(users):
        # 记一下「最近登录」和来源 IP，管理员界面要看
        if name in users:
            users[name]["last_login"] = now_text()
            # ip 为空说明这次不是登录调用（例如改密码时校验旧密码），
            # 那就别把已经记下来的来源覆盖掉
            if ip:
                users[name]["last_ip"] = str(ip)
        return users

    try:
        update_users(_touch)
    except Exception:
        pass
    return {"user": name, "key": str(item.get("key") or "")}


def _code_hash(code: str) -> str:
    return hashlib.sha256(str(code or "").encode("ascii", "ignore")).hexdigest()


def _new_redeem_code() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    raw = "".join(secrets.choice(alphabet) for _ in range(12))
    return "DSF-%s-%s-%s" % (raw[:4], raw[4:8], raw[8:])


def generate_redeem_codes(days_key: str, count=1, actor: str = "") -> dict:
    option = REDEEM_DURATIONS.get(str(days_key or ""))
    if not option:
        return {"ok": False, "error": "请选择 7 天、14 天、30 天或 60 天"}
    try:
        count = int(count)
    except (TypeError, ValueError):
        count = 1
    if not (1 <= count <= 100):
        return {"ok": False, "error": "一次最多生成 100 个兑换码"}
    made = []
    now = int(time.time())

    def _apply(data):
        codes = data.get("codes") if isinstance(data, dict) else None
        if not isinstance(codes, list):
            codes = []
        hashes = {str(item.get("hash") or "") for item in codes if isinstance(item, dict)}
        while len(made) < count:
            code = _new_redeem_code()
            digest = _code_hash(code)
            if digest in hashes:
                continue
            hashes.add(digest)
            made.append(code)
            codes.insert(0, {
                "hash": digest,
                "days": option["days"],
                "label": option["label"],
                "created_at": now,
                "created_by": str(actor or PANEL_USERNAME),
                "used_at": 0,
                "used_by": "",
            })
        return {"codes": codes[:5000]}

    CODES_STORE.update(_apply)
    log_force("生成兑换码", "%s：%d 个" % (option["label"], len(made)))
    return {"ok": True, "codes": made, "days": option["days"], "label": option["label"], "message": "已生成 %d 个%s兑换码（明文只显示这一次）" % (len(made), option["label"])}


def redeem_code(name: str, raw_code: str) -> dict:
    name = str(name or "").strip()
    normalized = re.sub(r"\s+", "", str(raw_code or "").upper())
    if not re.fullmatch(REDEEM_CODE_RE, normalized):
        return {"ok": False, "error": "兑换码格式不正确，请粘贴 DSF-XXXX-XXXX-XXXX 格式的完整兑换码"}
    if name not in load_users():
        return {"ok": False, "error": "没有这个用户"}
    digest = _code_hash(normalized)
    result = {"ok": False, "error": "兑换码无效或已经使用"}

    def _consume(data):
        codes = data.get("codes") if isinstance(data, dict) else None
        if not isinstance(codes, list):
            codes = []
        hit = next((item for item in codes if isinstance(item, dict) and str(item.get("hash") or "") == digest), None)
        if not hit or _epoch(hit.get("used_at")):
            return data
        days = max(1, _epoch(hit.get("days"), 0))
        now = int(time.time())

        def _extend(users):
            item = users.get(name)
            if not isinstance(item, dict):
                return users
            access = item.get("access") if isinstance(item.get("access"), dict) else None
            if not access or bool(access.get("unlimited")):
                result.update({"ok": True, "unlimited": True, "message": "这是永久账号，无需兑换时长"})
                return users
            base = max(now, _epoch(access.get("expires_at")))
            access = dict(access)
            access.update({"unlimited": False, "trial": False, "expires_at": base + days * 86400, "updated_at": now})
            item["access"] = access
            result.update({"ok": True, "days": days, "expires_at": access["expires_at"], "message": "兑换成功，已增加 %d 天使用时长" % days})
            return users

        update_users(_extend)
        # 永久账号无需兑换时长，也不应消耗一次性兑换码。
        if result.get("ok") and not result.get("unlimited"):
            hit["used_at"] = now
            hit["used_by"] = name
            hit["redeemed_days"] = days
        return {"codes": codes}

    CODES_STORE.update(_consume)
    if result.get("ok"):
        result["subscription"] = subscription_info(name)
        log_force("兑换时长", "%s：%s" % (name, result.get("message") or "成功"))
    return result


def grant_user_duration(name: str, days_key: str, actor: str = "") -> dict:
    name = str(name or "").strip()
    option = REDEEM_DURATIONS.get(str(days_key or ""))
    if not option:
        return {"ok": False, "error": "请选择有效的时长"}
    result = {"ok": False, "error": "没有这个用户"}
    now = int(time.time())

    def _apply(users):
        item = users.get(name)
        if not isinstance(item, dict):
            return users
        access = item.get("access") if isinstance(item.get("access"), dict) else None
        if not access or bool(access.get("unlimited")):
            result.update({"ok": True, "message": "这个账号本来就是永久有效，无需增加时长", "unlimited": True})
            return users
        access = dict(access)
        base = max(now, _epoch(access.get("expires_at")))
        access.update({"unlimited": False, "trial": False, "expires_at": base + option["days"] * 86400, "updated_at": now})
        item["access"] = access
        result.update({"ok": True, "message": "已给 %s 增加 %s" % (name, option["label"]), "expires_at": access["expires_at"]})
        return users

    update_users(_apply)
    if result.get("ok"):
        result["subscription"] = subscription_info(name)
        log_force("管理员授予时长", "%s → %s（%s）" % (actor or PANEL_USERNAME, name, option["label"]))
    return result


def redeem_code_overview(limit: int = 100) -> list:
    raw = CODES_STORE.read()
    codes = raw.get("codes") if isinstance(raw, dict) else []
    if not isinstance(codes, list):
        return []
    out = []
    for item in codes[:max(1, min(500, int(limit or 100)))]:
        if not isinstance(item, dict):
            continue
        out.append({
            "label": str(item.get("label") or ""),
            "days": _epoch(item.get("days")),
            "created_at": _format_epoch(item.get("created_at")),
            "created_by": str(item.get("created_by") or ""),
            "used_at": _format_epoch(item.get("used_at")),
            "used_by": str(item.get("used_by") or ""),
            "used": bool(_epoch(item.get("used_at"))),
        })
    return out


def set_user_password(name: str, password: str) -> dict:
    name = str(name or "").strip()
    password = str(password or "")
    if len(password) < 6:
        return {"ok": False, "error": "密码至少 6 位"}
    if name not in load_users():
        return {"ok": False, "error": "没有这个用户"}
    salt = secrets.token_hex(16)

    def _apply(users):
        if name in users:
            users[name]["salt"] = salt
            users[name]["hash"] = _pass_hash(password, salt)
        return users

    update_users(_apply)
    log_force("重设密码", name)
    # 新密码只在这一次响应里回给你，服务器上不再留任何明文副本
    return {
        "ok": True,
        "password": password,
        "message": "已把 %s 的密码改好：%s（只显示这一次，请私下发给他）" % (name, password),
    }


def change_own_password(name: str, old: str, new: str, again=None) -> dict:
    if not verify_user(name, old):
        return {"ok": False, "error": "现在的密码不对"}
    if again not in (None, "") and str(again) != str(new):
        return {"ok": False, "error": "两次输入的新密码不一样"}
    return set_user_password(name, new)


def bind_account(name: str, unique_id: str, force: bool = False, silent: bool = False) -> dict:
    """把某个抖音号绑到用户名下。force=True 是管理员操作，可以从别人名下拿过来。"""
    name = str(name or "").strip()
    unique_id = str(unique_id or "").strip()
    if name == PANEL_USERNAME:
        return bind_admin_account(unique_id)
    users = load_users()
    if name not in users:
        return {"ok": False, "error": "没有这个用户"}
    if not unique_id:
        return {"ok": False, "error": "请填写要绑定的抖音号"}
    if unique_id not in {str(t.get("unique_id") or "") for t in load_tasks()}:
        return {"ok": False, "error": "还没有「%s」这个抖音号，先在账号区把它建出来" % unique_id}
    owner = owner_of(unique_id, users)
    if owner and owner != name and not force:
        return {"ok": False, "error": "这个抖音号现在绑在 %s 名下" % owner}
    if unique_id in admin_owned():
        _save_admin_owned([x for x in admin_owned() if x != unique_id])

    def _apply(table):
        if name not in table:
            return table
        holder = owner_of(unique_id, table)
        if holder and holder != name and not force:
            return table
        if holder and holder != name and holder in table:
            table[holder]["accounts"] = [
                x for x in (table[holder].get("accounts") or []) if str(x) != unique_id
            ]
        owned = [str(x) for x in (table[name].get("accounts") or []) if str(x) != unique_id]
        owned.append(unique_id)
        table[name]["accounts"] = owned
        return table

    update_users(_apply)
    if not silent:
        log_force("绑定抖音号", "%s → %s" % (name, unique_id))
    return {"ok": True, "message": "已把 %s 绑给 %s" % (unique_id, name)}


def unbind_account(name: str, unique_id: str) -> dict:
    name = str(name or "").strip()
    unique_id = str(unique_id or "").strip()
    if name == PANEL_USERNAME:
        if unique_id not in admin_owned():
            return {"ok": False, "error": "管理员名下没有这个抖音号"}
        _save_admin_owned([x for x in admin_owned() if x != unique_id])
        log_force("解绑抖音号", "%s ← %s（管理员自己）" % (unique_id, PANEL_USERNAME))
        return {"ok": True, "message": "已从管理员名下解绑 %s" % unique_id}
    users = load_users()
    if name not in users:
        return {"ok": False, "error": "没有这个用户"}
    before = [str(x) for x in (users[name].get("accounts") or [])]
    if unique_id not in before:
        return {"ok": False, "error": "%s 名下没有这个抖音号" % name}

    def _apply(table):
        if name in table:
            table[name]["accounts"] = [
                x for x in (table[name].get("accounts") or []) if str(x) != unique_id
            ]
        return table

    update_users(_apply)
    log_force("解绑抖音号", "%s ← %s" % (name, unique_id))
    return {"ok": True, "message": "已解绑 %s" % unique_id}


def delete_user(name: str) -> dict:
    name = str(name or "").strip()
    if name not in load_users():
        return {"ok": False, "error": "没有这个用户"}

    def _apply(users):
        users.pop(name, None)
        return users

    update_users(_apply)

    def _remove_push_devices(data):
        subscriptions = _clean_push_subscriptions(data)["users"]
        subscriptions.pop(name, None)
        return {"users": subscriptions}

    WEBPUSH_SUBSCRIPTIONS_STORE.update(_remove_push_devices)
    log_force("删除用户", name)
    return {"ok": True, "message": "已删除用户 %s（它的抖音号配置还在账号区，可以再删掉）" % name}


def user_overview() -> list:
    """管理员界面用：所有注册用户 + 他们绑的抖音号 + 状态"""
    users = load_users()
    tasks = {str(t.get("unique_id") or ""): t for t in load_tasks()}
    state = load_state()
    saved_at = state.get("saved_at") or {}
    checks = state.get("checks") or {}
    login_failures = task_login_failures()
    rows = []
    for name, item in users.items():
        if not isinstance(item, dict):
            continue
        accounts = []
        for uid in [str(x) for x in (item.get("accounts") or [])]:
            task = tasks.get(uid) or {}
            check = real_check(checks, uid)
            accounts.append(
                {
                    "unique_id": uid,
                    "username": str(task.get("username") or ""),
                    "exists": uid in tasks,
                    "targets": [str(t) for t in (task.get("targets") or [])],
                                        "has_cookie": bool(task) and bool(load_cookies(uid)),
                    "saved_at": saved_at.get(uid, ""),
                    "check_ok": check.get("ok") if isinstance(check, dict) else None,
                    "check_at": check.get("at") if isinstance(check, dict) else "",
                    "task_login_failed": bool(
                        task_login_failure_for(uid, login_failures, checks, saved_at)
                    ),
                }
            )
        rows.append(
            {
                "name": str(name),
                "at": str(item.get("at") or ""),
                "last_login": str(item.get("last_login") or ""),
                "last_ip": str(item.get("last_ip") or ""),
                "phone": str(item.get("phone") or ""),
                "accounts": accounts,
                "subscription": subscription_info(name),
            }
        )
    rows.sort(key=lambda row: (row["at"], row["name"]))
    return rows


def unowned_accounts() -> list:
    """还没有绑给任何用户的抖音号（管理员可以按需分配）"""
    users = load_users()
    free = []
    for task in load_tasks():
        uid = str(task.get("unique_id") or "")
        if uid and not owner_of(uid, users):
            free.append(uid)
    return free


# --------------------------------------------------------------------------
# 强制操作：卡死时用（结束卡住的浏览器 / 重启面板进程）
# --------------------------------------------------------------------------
def _proc_cmdline(pid: int) -> str:
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as handle:
            return handle.read().decode("utf-8", "replace").replace("\x00", " ").strip()
    except Exception:
        return ""


def chromium_pids() -> list:
    """容器里所有浏览器进程（授权浏览器、发送任务、登录检测都算）。"""
    pids = []
    try:
        entries = os.listdir("/proc")
    except Exception:
        return pids
    me = os.getpid()
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid == me:
            continue
        command = _proc_cmdline(pid).lower()
        if not command or "panel.py" in command:
            continue
        if "chrome" in command or "chromium" in command or "headless_shell" in command:
            pids.append(pid)
    return pids


def kill_chromium() -> int:
    killed = 0
    for pid in chromium_pids():
        try:
            os.kill(pid, signal.SIGKILL)
            killed += 1
        except Exception:
            pass
    return killed


def log_force(action: str, detail: str) -> None:
    try:
        append_text(FORCE_LOG, "%s  %s  %s\n" % (now_text(), action, detail))
    except Exception:
        pass


def load_force_log(limit: int = 6) -> list:
    try:
        text = FORCE_LOG.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []
    rows = [line for line in text.splitlines() if line.strip()]
    return rows[-limit:][::-1]


_DECRYPT_WARNED = {}


def _warn_decrypt(uid: str, error) -> None:
    """Cookie 解不开时留个痕（同一个账号 5 分钟只记一次，免得刷屏）。"""
    now = time.time()
    if now - float(_DECRYPT_WARNED.get(uid) or 0) < 300:
        return
    _DECRYPT_WARNED[uid] = now
    try:
        log_force("Cookie 解密失败", "账号 %s：%s（面板密钥变过？需要重新扫码授权）" % (uid, error))
    except Exception:
        pass


# 强制停止要不要再收一个请求进来干这活 —— 已经在清的时候就别再排队了
_FORCE_STOP_LOCK = threading.Lock()
_FORCE_STOP_BUSY = {"on": False}


def force_stop(reason: str = "面板按钮") -> dict:
    """让请求线程立刻返回，清理交给后台 —— 点一下要等 10 秒才回话，前端会以为死掉了。"""
    with _FORCE_STOP_LOCK:
        if _FORCE_STOP_BUSY["on"]:
            return {"ok": True, "message": "上一次清理还在收尾，几秒后就好（不用再点）"}
        _FORCE_STOP_BUSY["on"] = True
    threading.Thread(target=_force_stop_worker, args=(reason,), name="force-stop", daemon=True).start()
    return {"ok": True, "message": "已经开始清理（授权浏览器 / 登录检测 / 发送任务），几秒后自动收尾"}


def _force_stop_worker(reason: str) -> None:
    """真正干活的清理：先好好通知、等线程收尾，最后才动刀杀进程。"""
    try:
        _force_stop_now(reason)
    finally:
        with _FORCE_STOP_LOCK:
            _FORCE_STOP_BUSY["on"] = False


def _force_stop_now(reason: str) -> None:
    details = []
    try:
        browser.force_stop()
        details.append("已通知授权浏览器退出")
    except Exception as error:
        details.append("通知授权浏览器失败：%s" % error)

    try:
        checker.force_stop()
        details.append("已通知登录检测退出")
    except Exception as error:
        details.append("通知登录检测失败：%s" % error)

    # 先给两个后台线程一点时间自己收尾（各最多 8 秒），再去杀浏览器进程。
    # 一上来就杀，它们会在半路撞上"浏览器已经关了"，日志里全是没用的报错堆栈。
    for label, worker in (("授权浏览器", browser), ("登录检测", checker)):
        if not _join_thread(worker, 8.0):
            details.append("%s没在 8 秒内收尾，下面直接清进程" % label)

    try:
        if runner.kill():
            details.append("已结束面板里正在跑的发送任务")
    except Exception as error:
        details.append("结束发送任务失败：%s" % error)

    killed = kill_chromium()
    if killed:
        details.append("已强制结束 %d 个浏览器进程" % killed)

    try:
        atomic_write(
            ABORT_PATH, json.dumps({"at": now_text(), "reason": reason}, ensure_ascii=False)
        )
        details.append("已请求当前发送任务尽快停止")
    except Exception:
        pass

    try:
        save_state({"auto_auth_for": "", "auto_auth_try": 0})
    except Exception:
        pass

    try:
        browser._set(state="idle", message="已被强制停止", error="")
    except Exception:
        pass

    try:
        with checker.lock:
            if not checker.running():
                checker.state = "idle"
                checker.message = "已被强制停止（浏览器已结束）"
    except Exception:
        pass

    detail = "；".join(details) if details else "没有发现正在运行的东西"
    log_force("强制停止", detail)


def delete_account(unique_id: str) -> dict:
    """删掉一个账号：配置、Cookie、检测记录一起清掉，其它账号不受影响。"""
    unique_id = str(unique_id or "").strip()
    if not unique_id:
        return {"ok": False, "error": "没指定要删哪个账号"}
    try:
        drop_account(unique_id)
    except Exception as error:
        return {"ok": False, "error": "删除失败：" + str(error)}
    except Exception:
        pass
    try:
        def _clean(state):
            if isinstance(state, dict):
                for key in ("checks", "saved_at"):
                    if isinstance(state.get(key), dict):
                        state[key].pop(unique_id, None)
            return state

        STATE_STORE.update(_clean)
    except Exception:
        pass
    try:
        def _unbind(users):
            for item in users.values():
                if isinstance(item, dict):
                    owned = [str(x) for x in (item.get("accounts") or [])]
                    kept = [x for x in owned if x != unique_id]
                    if len(kept) != len(owned):
                        item["accounts"] = kept
            return users

        update_users(_unbind)
    except Exception:
        pass
    invalidate_shot_cache()
    log_force("删除账号", unique_id)
    return {"ok": True, "message": "已删除账号 %s（连同它的 Cookie）" % unique_id}


def force_restart() -> dict:
    """清理干净后让面板进程退出，容器由 Docker 自动拉起（约 10 秒后可用）。"""
    force_stop("重启前清理")
    try:
        atomic_write(RESTART_FLAG, json.dumps({"at": now_text()}, ensure_ascii=False))
    except Exception:
        pass
    log_force("强制重启", "面板进程即将退出，容器会自动重新拉起")
    _graceful_exit()
    return {"ok": True, "message": "正在重启面板后端，约 10 秒后自动恢复（页面会自己刷新）"}


def _join_thread(worker, timeout: float) -> bool:
    """等后台线程自己收尾（带超时，多个会话一起等）。返回 True 表示都结束了。"""
    threads = None
    getter = getattr(worker, "threads", None)
    if callable(getter):
        try:
            threads = list(getter())
        except Exception:
            threads = []
    if threads is None:
        one = getattr(worker, "thread", None)
        threads = [one] if one is not None else []
    deadline = time.time() + timeout
    ok = True
    for thread in threads:
        try:
            if thread.is_alive():
                thread.join(max(0.05, deadline - time.time()))
            if thread.is_alive():
                ok = False
        except Exception:
            ok = False
    return ok


def _graceful_exit() -> None:
    """先把这次响应发完，再让服务器正常退出。

    以前是直接 os._exit，运气不好会把还没吐出去的 {"ok": true} 掐掉，
    前端就以为重启失败了。现在先 shutdown（serve_forever 会正常返回），
    万一卡住再用 os._exit 兜底。
    """

    def _stop():
        time.sleep(0.5)  # 等这一次 HTTP 响应写完
        try:
            if SERVER is not None:
                SERVER.shutdown()
        except Exception:
            pass
        time.sleep(1.0)
        os._exit(3)  # 正常路径下走不到这：shutdown 之后主线程已经退出进程了

    threading.Thread(target=_stop, name="graceful-exit", daemon=True).start()


# --------------------------------------------------------------------------
# 浏览器会话：截图 / 点击 / 输入，登录成功自动导出 Cookies
# --------------------------------------------------------------------------
# ---- 在抖音页面里找东西用的脚本（全部按文字 + 尺寸找，不写死类名）----
_JS_HAS_WORDS = """(words) => {
  for (const el of document.querySelectorAll('div,span,p,button,a,label')) {
    const t = (el.innerText || el.textContent || '').trim();
    if (!t || t.length > 30) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 4 || r.height < 4) continue;
    const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden') continue;
    for (const w of words) { if (t.indexOf(w) >= 0) { return w; } }
  }
  return '';
}"""

_JS_CLICK_LOGIN = """(texts) => {
  for (const el of document.querySelectorAll('button,a,div,span')) {
    const t = (el.innerText || el.textContent || '').trim();
    if (texts.indexOf(t) < 0) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 30 || r.height < 20) continue;
    const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden' || st.opacity === '0') continue;
    el.click();
    return {ok: true, text: t};
  }
  return {ok: false};
}"""

_JS_FIND_QR = """() => {
  const cand = [];
  const push = (el, kind, src) => {
    const r = el.getBoundingClientRect();
    if (r.width < 100 || r.height < 100) return;
    const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden' || st.opacity === '0') return;
    if (Math.abs(r.width - r.height) > Math.max(24, r.width * 0.25)) return;
    cand.push({kind: kind, src: src, w: Math.round(r.width), h: Math.round(r.height),
               x: Math.round(r.x), y: Math.round(r.y)});
  };
  document.querySelectorAll('img').forEach(el => {
    const s = String(el.getAttribute('src') || '');
    push(el, s.indexOf('data:image') === 0 ? 'dataimg' : 'img', s);
  });
  document.querySelectorAll('canvas').forEach(el => {
    let s = '';
    try { s = el.toDataURL('image/png'); } catch (e) { s = ''; }
    if (s) { push(el, 'canvas', s); }
  });
  cand.sort((a, b) => (b.w * b.h) - (a.w * a.h));
  return cand.length ? cand[0] : null;
}"""

_JS_QR_REFRESH = """() => {
  const words = ['点击刷新', '刷新二维码', '刷新'];
  for (const el of document.querySelectorAll('button,a,div,span,p')) {
    const t = (el.innerText || el.textContent || '').trim();
    if (words.indexOf(t) < 0) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 20 || r.height < 12) continue;
    const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden') continue;
    el.click();
    return {ok: true, text: t};
  }
  return {ok: false};
}"""

# 验证码输入框：按 placeholder / aria-label / name 里带"验证码"找
# 按“文字完全一致”点元素：避开“顺带包含这几个字”的大容器，避免点到整个弹窗
_JS_CLICK_BY_TEXT = """(texts) => {
  const want = texts.map(t => String(t).replace(/\\s+/g, '')).filter(Boolean);
  const hit = [];
  for (const el of document.querySelectorAll('div,span,p,li,a,button,label')) {
    const t = (el.innerText || el.textContent || '').trim().replace(/\\s+/g, '');
    if (!t || !want.some(w => t === w || t.indexOf(w) >= 0)) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 24 || r.height < 10) continue;
    const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden' || st.opacity === '0') continue;
    const native = el.tagName === 'BUTTON' || el.tagName === 'A' ||
      el.getAttribute('role') === 'button' || st.cursor === 'pointer';
    hit.push({el: el, area: r.width * r.height, native: native});
  }
  if (!hit.length) { return {ok: false}; }
  hit.sort((a, b) => (a.native !== b.native) ? (a.native ? -1 : 1) : a.area - b.area);
  // 就点最里层那个字：点击会自己往上冒泡，行上的点击事件照样收得到。
  // 不能往上点父节点：弹窗里只剩这一个按钮时，外层的字一模一样，点了外层等于没点。
  const target = hit[0].el;
  try { target.scrollIntoView({block: 'center'}); } catch (e) {}
  target.click();
  return {ok: true, text: (hit[0].el.innerText || '').trim(), tag: target.tagName.toLowerCase()};
}"""

_JS_SMS_BOX = """() => {
  const re = /(验证码|动态码|校验码)/;
  // 自己和所有祖先都不能是藏着的（祖先 opacity:0 也算藏）
  const shown = (el) => {
    let node = el;
    while (node && node.nodeType === 1) {
      const st = getComputedStyle(node);
      if (st.display === 'none' || st.visibility === 'hidden') return false;
      if (parseFloat(st.opacity || '1') < 0.05) return false;
      node = node.parentElement;
    }
    const r = el.getBoundingClientRect();
    return r.width >= 4 && r.height >= 8;
  };
  // 真鼠标点在框的正中间，会点到它自己吗？点到别的就说明它被盖住了 / 根本看不见
  const onTop = (el) => {
    const r = el.getBoundingClientRect();
    const cx = r.x + r.width / 2, cy = r.y + r.height / 2;
    if (cx < 0 || cy < 0 || cx > window.innerWidth || cy > window.innerHeight) return null;
    try {
      const top = document.elementFromPoint(cx, cy);
      return !!(top && (top === el || el.contains(top)));
    } catch (e) { return null; }
  };
  // 「验证」按钮在哪：验证码框永远挨着它，用它挑出真正的那一个
  const words = ['验证', '确认', '确定', '提交', '下一步', '完成验证', '确认登录'];
  const norm = (v) => String(v || '').replace(/\\s+/g, '');
  const button = () => {
    const out = [];
    for (const el of document.querySelectorAll('button,a,[role="button"],div,span')) {
      const t = norm(el.innerText || el.textContent);
      if (!t || words.indexOf(t) < 0) continue;
      const st = getComputedStyle(el);
      if (st.display === 'none' || st.visibility === 'hidden' || st.opacity === '0') continue;
      const r = el.getBoundingClientRect();
      if (r.width < 24 || r.height < 14) continue;
      const native = (el.tagName === 'BUTTON' || el.tagName === 'A'
                      || String(el.getAttribute('role') || '') === 'button'
                      || st.cursor === 'pointer');
      out.push({x: r.x + r.width / 2, y: r.y + r.height / 2,
                area: Math.round(r.width * r.height), native: native});
    }
    if (!out.length) return null;
    out.sort((a, b) => (a.native !== b.native) ? (a.native ? -1 : 1) : (a.area - b.area));
    return out[0];
  };
  const clear = () => {
    for (const old of document.querySelectorAll('[data-wz-sms]')) {
      old.removeAttribute('data-wz-sms');
    }
  };
  const all = Array.from(document.querySelectorAll('input'));
  const btn = button();
  const dist = (el) => {
    if (!btn) return 0;
    const r = el.getBoundingClientRect();
    return Math.round(Math.abs(r.x + r.width / 2 - btn.x) + Math.abs(r.y + r.height / 2 - btn.y));
  };
  const info = (o) => {
    const r = o.el.getBoundingClientRect();
    return {idx: o.idx, ph: String(o.el.getAttribute('placeholder') || '').slice(0, 20),
            ml: String(o.el.getAttribute('maxlength') || ''),
            x: Math.round(r.x), y: Math.round(r.y),
            w: Math.round(r.width), h: Math.round(r.height),
            hit: o.hit, d: dist(o.el)};
  };
  // 1) 带「验证码」字样的输入框
  const code = [];
  for (let i = 0; i < all.length; i++) {
    const el = all[i];
    if (!shown(el)) continue;
    const ph = String(el.getAttribute('placeholder') || '');
    const al = String(el.getAttribute('aria-label') || '');
    const nm = String(el.getAttribute('name') || '');
    if (!(re.test(ph) || re.test(al) || re.test(nm))) continue;
    code.push({idx: i, el: el, hit: onTop(el)});
  }
  if (code.length) {
    // 点得着的优先（抖音会留一个看不见的同名框在前面）；
    // 都点得着就挑离「验证」按钮最近的那个
    const pick = code.slice().sort((a, b) => {
      const av = (a.hit === true) ? 0 : 1, bv = (b.hit === true) ? 0 : 1;
      if (av !== bv) return av - bv;
      return dist(a.el) - dist(b.el);
    })[0];
    clear();
    pick.el.setAttribute('data-wz-sms', '1');
    const r = pick.el.getBoundingClientRect();
    return {idx: pick.idx, kind: 'code', multi: 1,
            ph: String(pick.el.getAttribute('placeholder') || '').slice(0, 20),
            x: Math.round(r.x), y: Math.round(r.y),
            w: Math.round(r.width), h: Math.round(r.height),
            onTop: pick.hit, d: dist(pick.el), cands: code.map(info)};
  }
  // 1b) 二级验证弹的是「验证登录密码」：页面上是个密码框，一个「验证」按钮，没有验证码。
  //     只在页面上确实没有验证码框时才走到这里（上面那段已经 return 了），所以
  //     登录弹窗里那个「密码登录」的框不会被误判 —— 何况那边还有 MODAL_WORDS 挡着。
  const pwd = [];
  for (let i = 0; i < all.length; i++) {
    const el = all[i];
    if (!shown(el)) continue;
    const typ = String(el.getAttribute('type') || '').toLowerCase();
    const ph = String(el.getAttribute('placeholder') || '');
    const al = String(el.getAttribute('aria-label') || '');
    const nm = String(el.getAttribute('name') || '');
    const looks = (typ === 'password') || /(登录密码|输入密码)/.test(ph + ' ' + al + ' ' + nm);
    if (!looks) continue;
    if (String(el.getAttribute('maxlength') || '') === '1') continue;
    pwd.push({idx: i, el: el, hit: onTop(el)});
  }
  if (pwd.length) {
    const pick = pwd.slice().sort((a, b) => {
      const av = (a.hit === true) ? 0 : 1, bv = (b.hit === true) ? 0 : 1;
      if (av !== bv) return av - bv;
      return dist(a.el) - dist(b.el);
    })[0];
    clear();
    pick.el.setAttribute('data-wz-sms', '1');
    const r = pick.el.getBoundingClientRect();
    return {idx: pick.idx, kind: 'pwd', multi: 1,
            ph: String(pick.el.getAttribute('placeholder') || '').slice(0, 20),
            x: Math.round(r.x), y: Math.round(r.y),
            w: Math.round(r.width), h: Math.round(r.height),
            onTop: pick.hit, d: dist(pick.el), cands: pwd.map(info)};
  }
  // 2) 一格一个数字的那种：把同一行的几格认出来
  const seg = [];
  for (let i = 0; i < all.length; i++) {
    const el = all[i];
    if (!shown(el)) continue;
    const r = el.getBoundingClientRect();
    const ml = String(el.getAttribute('maxlength') || '');
    if (ml !== '1' && r.width > 64) continue;
    seg.push({idx: i, el: el, x: r.x, y: r.y, w: r.width, h: r.height, hit: onTop(el)});
  }
  if (seg.length >= 2 && seg.length <= 8) {
    const y0 = seg[0].y, h0 = seg[0].h;
    const row = seg.filter(o => Math.abs(o.y - y0) < Math.max(6, h0 * 0.6));
    if (row.length >= 2) {
      row.sort((a, b) => a.x - b.x);
      clear();
      for (const o of row) { o.el.setAttribute('data-wz-sms', '1'); }
      return {idx: row[0].idx, idxs: row.map(o => o.idx), kind: 'seg', multi: row.length, ph: '',
              x: Math.round(row[0].x), y: Math.round(row[0].y),
              w: Math.round(row[0].w), h: Math.round(row[0].h),
              onTop: row[0].hit, d: dist(row[0].el), cands: row.map(info)};
    }
  }
  return null;
}"""

_JS_ACTIVE_VALUE = """() => {
  // 「内容到底有没有粘进去」靠它核对：读当前有光标的那个元素里的内容。
  // 光标不在输入框里（比如落在 body 上）就返回空串 —— 那等于没粘进去。
  const el = document.activeElement;
  if (!el) return "";
  const tag = String(el.tagName || '').toLowerCase();
  if (tag === 'input' || tag === 'textarea') return String(el.value || '');
  if (el.isContentEditable) return String(el.textContent || '');
  return "";
}"""

_JS_SMS_FOCUS = """() => {
  // 只认上一步挑好、已经标记的那个框：保证“找框”“填框”“读框”是同一个框
  const el = document.querySelector('[data-wz-sms="1"]');
  if (!el) return false;
  try { el.scrollIntoView({block: 'center'}); } catch (e) {}
  try { el.focus(); } catch (e) {}
  return document.activeElement === el;
}"""

_JS_SMS_TYPE = """(text) => {
  // React 受控输入框：用原生 setter + 补发事件，直接赋 value 页面不一定认
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
  const put = (el, v) => {
    setter.call(el, v);
    el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
  };
  const marked = Array.from(document.querySelectorAll('[data-wz-sms="1"]'));
  if (!marked.length) return null;
  const chars = String(text || '').split('');
  if (marked.length > 1) {
    for (let i = 0; i < marked.length; i++) { put(marked[i], chars[i] || ''); }
    return marked.map(e => String(e.value || '')).join('');
  }
  put(marked[0], text);
  return String(marked[0].value || '');
}"""

_JS_SMS_FOCUSED = """(i) => {
  const all = document.querySelectorAll('[data-wz-sms=\"1\"]');
  const el = all[i];
  return !!el && document.activeElement === el;
}"""

_JS_SMS_VALUE = """() => {
  // 先读上一步标记的那个框（跟填的是同一个）；没标记才退回去盲找
  const marked = Array.from(document.querySelectorAll('[data-wz-sms="1"]'));
  if (marked.length) return marked.map(e => String(e.value || '')).join('');
  const re = /(验证码|动态码|校验码)/;
  const vis = (el) => {
    const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden') return false;
    const r = el.getBoundingClientRect();
    return r.width >= 4 && r.height >= 8;
  };
  const all = Array.from(document.querySelectorAll('input'));
  const hit = [];
  for (let i = 0; i < all.length; i++) {
    const el = all[i];
    if (!vis(el)) continue;
    const ph = String(el.getAttribute('placeholder') || '');
    const al = String(el.getAttribute('aria-label') || '');
    if (!(re.test(ph) || re.test(al))) continue;
    hit.push(String(el.value || ''));
  }
  if (hit.length) return hit.join('|');
  const seg = [];
  for (let i = 0; i < all.length; i++) {
    const el = all[i];
    if (!vis(el)) continue;
    const r = el.getBoundingClientRect();
    const ml = String(el.getAttribute('maxlength') || '');
    if (ml !== '1' && r.width > 64) continue;
    seg.push(String(el.value || ''));
  }
  return seg.join('');
}"""

_JS_SMS_SUBMIT = """() => {
  // 找「验证 / 确认」类按钮：挑一个最像真按钮的，并告诉外面它画在屏幕的哪个位置。
  // 这里只负责“找到”，真正的点击交给外面用真鼠标点（跟人点的一样）。
  const words = ['验证', '确认', '确定', '提交', '下一步', '完成验证', '确认登录'];
  const re = /(验证码|动态码|校验码)/;
  const vis = (el) => {
    const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden' || st.opacity === '0') return false;
    const r = el.getBoundingClientRect();
    return r.width >= 4 && r.height >= 8;
  };
  let bx = 0, by = 0;
  for (const el of document.querySelectorAll('input')) {
    if (!vis(el)) continue;
    const r = el.getBoundingClientRect();
    const ph = String(el.getAttribute('placeholder') || '');
    const al = String(el.getAttribute('aria-label') || '');
    const ml = String(el.getAttribute('maxlength') || '');
    if (re.test(ph) || re.test(al) || ml === '1' || r.width <= 64) {
      bx = r.x; by = r.y; break;
    }
  }
  const out = [];
  for (const el of document.querySelectorAll('button,a,div,span')) {
    const t = (el.innerText || el.textContent || '').trim();
    if (words.indexOf(t) < 0) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 30 || r.height < 18) continue;
    const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden' || st.opacity === '0') continue;
    const off = el.disabled === true
      || String(el.getAttribute('aria-disabled') || '') === 'true'
      || st.pointerEvents === 'none';
    const tag = String(el.tagName || '');
    const native = (tag === 'BUTTON' || tag === 'A'
                    || String(el.getAttribute('role') || '') === 'button'
                    || st.cursor === 'pointer');
    out.push({el: el, text: t, off: off, native: native,
              area: Math.round(r.width * r.height),
              d: Math.round(Math.abs(r.x - bx) + Math.abs(r.y - by))});
  }
  out.sort((a, b) => {
    if (a.off !== b.off) return a.off ? 1 : -1;           // 点得动的排前面
    if (a.native !== b.native) return a.native ? -1 : 1;  // 本来就是按钮的优先
    if (a.area !== b.area) return a.area - b.area;        // 同样文字时取最里层的，别点外层的大框
    return a.d - b.d;                                     // 再挑离验证码框近的
  });
  if (!out.length) { return null; }
  const best = out[0];
  if (best.off) { return {ok: false, off: true, text: best.text, d: best.d}; }
  const el = best.el;
  try { el.scrollIntoView({block: 'center', inline: 'center'}); } catch (e) {}
  const r = el.getBoundingClientRect();
  const x = r.x + r.width / 2, y = r.y + r.height / 2;
  let hit = false;
  try {
    const top = document.elementFromPoint(x, y);
    hit = !!(top && (top === el || el.contains(top)
                     || (top.closest ? top.closest('button, a') === el : false)));
  } catch (e) {}
  return {ok: true, text: best.text, x: x, y: y,
          w: Math.round(r.width), h: Math.round(r.height),
          tag: String(el.tagName || '').toLowerCase(), hit: hit, d: best.d};
}"""

# 真鼠标点不着（被盖住了）时：页内脚本点一下同名的最里层元素
_JS_SMS_CLICK_TEXT = """(want) => {
  const norm = (v) => String(v || '').replace(/\\s+/g, '');
  const w = norm(want);
  const hit = [];
  for (const el of document.querySelectorAll('button,a,div,span')) {
    if (norm(el.innerText || el.textContent) !== w) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 30 || r.height < 18) continue;
    const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden' || st.opacity === '0') continue;
    hit.push({el: el, area: r.width * r.height});
  }
  if (!hit.length) { return false; }
  hit.sort((a, b) => a.area - b.area);
  try { hit[0].el.scrollIntoView({block: 'center'}); } catch (e) {}
  hit[0].el.click();
  return true;
}"""

# \u8bca\u65ad\u7528\uff1a\u628a\u9a8c\u8bc1\u7801\u9875\u7684\u7ed3\u6784\u63cf\u4e00\u4efd\uff08\u4e0d\u5305\u62ec\u8f93\u5165\u7684\u5185\u5bb9\uff0c\u53ea\u8bb0\u957f\u5ea6\uff09\uff0c\u65b9\u4fbf\u4e0b\u6b21\u51fa\u95ee\u9898\u65f6\u67e5
_JS_SMS_PROBE = """() => {
  const vis = (el) => {
    const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden') return false;
    const r = el.getBoundingClientRect();
    return r.width >= 4 && r.height >= 8;
  };
  const out = {url: String(location.href).slice(0, 110), frames: document.querySelectorAll('iframe').length,
               editable: document.querySelectorAll('[contenteditable="true"]').length, inputs: []};
  // 真鼠标点在框的正中间会点到谁：点到别的就说明这个框被盖住 / 根本看不见。
  // 抖音页面上同时有好几个「请输入验证码」的框，就靠这个分清哪个是真的。
  const onTop = (el) => {
    const r = el.getBoundingClientRect();
    const cx = r.x + r.width / 2, cy = r.y + r.height / 2;
    if (cx < 0 || cy < 0 || cx > window.innerWidth || cy > window.innerHeight) return null;
    try {
      const top = document.elementFromPoint(cx, cy);
      return (top === el || el.contains(top)) ? 'self' : String(top.tagName || '').toLowerCase();
    } catch (e) { return 'err'; }
  };
  const all = Array.from(document.querySelectorAll('input'));
  for (let i = 0; i < all.length && out.inputs.length < 10; i++) {
    const el = all[i];
    const r = el.getBoundingClientRect();
    out.inputs.push({i: i, vis: vis(el),
                     ph: String(el.getAttribute('placeholder') || '').slice(0, 22),
                     al: String(el.getAttribute('aria-label') || '').slice(0, 16),
                     nm: String(el.getAttribute('name') || '').slice(0, 16),
                     ml: String(el.getAttribute('maxlength') || ''),
                     im: String(el.getAttribute('inputmode') || ''),
                     len: String(el.value || '').length,
                     hit: onTop(el),
                     wh: [Math.round(r.width), Math.round(r.height)]});
  }
  out.picked = document.querySelectorAll('[data-wz-sms="1"]').length;
  return JSON.stringify(out);
}"""

# 页面上像"提示 / 报错"的短文字，交给 Python 判断是哪一类
_JS_PAGE_NOTES = """() => {
  const words = ['错误', '不正确', '失败', '过期', '失效', '频繁', '稍后', '重试', '已发送',
                 '发送至', '安全验证', '身份验证', '异常', '请重新', '已锁定', '受限',
                 // 二级验证页的说明文字：拿到原话就能直接告诉用户该干什么
                 '短信验证', '账号安全', '请完成', '完成验证', '验证码已'];
  const out = [];
  for (const el of document.querySelectorAll('div,span,p,label,button')) {
    const t = (el.innerText || el.textContent || '').trim().replace(/\\s+/g, ' ');
    if (!t || t.length > 60) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 6 || r.height < 6) continue;
    const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden') continue;
    for (const w of words) { if (t.indexOf(w) >= 0) { out.push(t); break; } }
  }
  return Array.from(new Set(out)).slice(0, 25);
}"""


def _png_side(raw) -> int:
    """PNG 头里的宽度（顺便确认这真是一张 PNG）。"""
    try:
        if not raw or raw[:8] != b"\x89PNG\r\n\x1a\n":
            return 0
        return int.from_bytes(raw[16:20], "big")
    except Exception:
        return 0


def _data_url_bytes(src) -> bytes:
    text = str(src or "")
    if not text.startswith("data:"):
        return b""
    head, _, payload = text.partition(",")
    if "base64" not in head:
        return b""
    try:
        return base64.b64decode(payload)
    except Exception:
        return b""


def _pick_note(notes, words) -> str:
    """按词的"具体程度"先后找一个页面提示：先找"验证码错误"，再找泛泛的"错误"。"""
    for word in words:
        for note in notes or []:
            if word in str(note):
                return str(note)
    return ""


class BrowserSession:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.commands = queue.Queue()
        self.thread = None
        self.state = "idle"
        self.message = "尚未启动"
        self.error = ""
        self.saved = ""
        self.png = None
        self.unique_id = ""
        self.username = ""
        # 这台授权浏览器是谁的（哪个抖音号）；和 unique_id 分开记，
        # 免得别的流程（比如登录检测）推画面时把归属改掉，让别人看到你的画面。
        self.owner = ""
        self.forced = False
        self.last_frame_at = 0.0
        # ---- 扫码登录流程的状态 ----
        self.qr_png = None           # 最新二维码原图（PNG 字节）
        self.qr_hash = ""            # 二维码指纹：变了说明换新了
        self.qr_at = 0.0             # 这张二维码是什么时候取到的
        self.qr_seen_at = 0.0        # 第一次看到二维码的时刻（"扫过了"要靠它判断）
        self.phone_login = False     # 这次授权走的是「手机号 + 验证码」而不是扫码
        self.phone_digits = 0        # 手机号填进去几位（只存长度，不存号码）
        self.verify_qr = False       # 抖音弹了二级验证，而且屏幕上有二维码可扫
        self.verify_hint = ""        # 二级验证时给用户看的那句话
        # 「手动模式」：进了二级验证就把**可操作画面**交给用户 ——
        # 主界面那块画面会自动摊开并写明"可以直接点"，通用输入框就在它下面。
        # 自动那套（自动点「用原设备扫码」+ 抓二维码摆出来）**并列保留**，两边不冲突。
        self.verify_manual = False
        self._verify_pick_at = 0.0
        self._verify_pick_method = ""  # phone / device / face / manual
        self._verify_face_hint_shown = False
        self.qr_gone_at = 0.0        # 二维码消失的时刻
        self.phase = "idle"          # idle/opening/qr/scanned/sms/manual/done
        self.page_hint = ""          # 页面上抓到的提示或报错原文
        self.sms_hint = ""           # "验证码已发送至…"之类
        # 二级验证要填哪种东西：code=手机验证码 / pwd=抖音登录密码
        self.sms_kind = "code"
        self.sms_result = {}         # 最近一次提交验证码的结果
        # 通用输入框那条通道的结果：跟验证码/密码分开存，
        # 免得「密码提交成功」被拿去做通用框的解释（也免得反过来）。
        self.text_result = {}
        self.started_at = 0.0
        # 最后一次"有人真的在弄这台浏览器"的时刻：扫码扫到了、点了画面、填了手机号/验证码
        # 都算。一直没动静的话，AUTH_IDLE_STOP 秒后自动关掉，把浏览器让给别人。
        self.last_progress_at = 0.0
        self._last_login_click = 0.0
        self._sms_submitted_at = 0.0
        self._sms_fill = None        # 已经敲进去、等着点验证的验证码
        self.sms_box_len = None      # 最近一次看到「抖音的框里有几位数字」
        self._sms_error = ""        # 最近一次填码没填进去的原因（给用户看原话）
        self.sms_box_at = 0.0        # 那次是什么时候看的
        # 验证码敲进抖音那一瞬间的定格画面：抖音的框填满就自己往下走，
        # 不留一张的话，实时画面十有八九拍到的是「填之前」或者「已经过了这一步」。
        self.sms_proof = None
        self.sms_proof_at = 0.0
        self._cdp = None             # 主循环的 CDP 通道，用来「立刻抓一张」
        self._sms_choice_at = 0.0
        self._sms_send_at = 0.0
        self._notes_at = 0.0
        self._qr_wait_hint_at = 0.0

    def running(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def _progress_at(self) -> float:
        """上一次"有动静"的时刻；没记过就退回本次启动时间，免得刚开就被判超时。"""
        with self.lock:
            return self.last_progress_at or self.started_at or time.time()

    def _touch(self) -> None:
        """记一次"有人在弄这台浏览器"（点了画面、填了手机号/验证码、扫到码…）"""
        with self.lock:
            self.last_progress_at = time.time()

    def idle_left(self):
        """距离"没动静自动关闭"还有多少秒；没在跑就是 None。"""
        if not self.running():
            return None
        left = AUTH_IDLE_STOP - (time.time() - self._progress_at())
        return int(max(0, left))

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "state": self.state,
                "message": self.message,
                "error": self.error,
                "saved": self.saved,
                "running": bool(self.thread and self.thread.is_alive()),
                "has_image": self.png is not None,
                "live": bool(
                    self.png is not None
                    and self.last_frame_at
                    and (time.time() - self.last_frame_at) < 20
                ),
                "frame_age": int(time.time() - self.last_frame_at) if self.last_frame_at else None,
                # 还有多久没动静就自动关（前端拿它显示倒计时）。
                # 这里不能用 idle_left()：那会再抢一次 self.lock，而 snapshot 是拿着锁进来的。
                "idle_left": (
                    int(max(0, AUTH_IDLE_STOP - (time.time() - (self.last_progress_at or self.started_at or time.time()))))
                    if (self.thread and self.thread.is_alive())
                    else None
                ),
                "stuck": bool(
                    self.thread
                    and self.thread.is_alive()
                    and self.last_frame_at
                    and (time.time() - self.last_frame_at) > STUCK_AFTER
                ),
                # ---- 扫码登录 ----
                "phase": self.phase,
                "has_qr": self.qr_png is not None,
                "qr_hash": self.qr_hash,
                "qr_age": int(time.time() - self.qr_at) if self.qr_at else None,
                "verify_qr": self.verify_qr,
                "verify_hint": self.verify_hint,
                # 手动模式：前端据此把「浏览器画面」摊开、显示"这块可以直接点"的提示条
                "manual": self.verify_manual,
                "sms_hint": self.sms_hint,
                "sms_kind": self.sms_kind,
                "sms_result": self.sms_result,
                "text_result": self.text_result,
                "page_hint": self.page_hint,
                # 给授权弹窗显示倒计时用：还要等几秒点「验证」、点完等几秒看结果
                "sms_click_left": self._sms_click_left(),
                "sms_click_total": SMS_BEFORE_SUBMIT_WAIT,
                "sms_settle_left": self._sms_settle_left(),
                "sms_settle_total": SMS_SETTLE,
                "qr_ttl": QR_REFRESH_AFTER,
                "sms_box_len": self.sms_box_len,
                "has_sms_proof": self.sms_proof is not None,
                "sms_proof_at": self.sms_proof_at or None,
                "sms_box_age": (int(time.time() - self.sms_box_at) if self.sms_box_at else None),
            }

    def _sms_click_left(self):
        """还要等几秒才替用户点「验证」；没在等就是 None"""
        fill = self._sms_fill
        if not isinstance(fill, dict):
            return None
        left = float(fill.get("at") or 0) + SMS_BEFORE_SUBMIT_WAIT - time.time()
        return round(max(0.0, left), 1)

    def _sms_settle_left(self):
        """「验证」点完了，还要等几秒才去看抖音的结果；没在等就是 None"""
        if not self._sms_submitted_at:
            return None
        left = self._sms_submitted_at + SMS_SETTLE - time.time()
        return round(max(0.0, left), 1)

    def _set(self, **kwargs) -> None:
        with self.lock:
            for key, value in kwargs.items():
                setattr(self, key, value)

    def image(self):
        with self.lock:
            return self.png

    def start(self, unique_id: str, username: str):
        # 「检查别人在不在跑 + 标记自己在跑 + 起线程」整段上锁：
        # 两个页面同时点「开始授权」时，只会有一个真的启动。
        with _ENGINE_LOCK:
            if checker.running():
                return False, "正在检测登录状态，请稍候再试"
            # [本地增强] 发送任务在跑也允许开授权浏览器。
            # 一轮发送可能要跑十几分钟，全锁着的话"想临时扫码加个号"就得干等；
            # 内存不够由 BrowserPool.start 里的可用内存检查兜底，不会无限叠浏览器。
            with self.lock:
                if self.thread and self.thread.is_alive():
                    return False, "授权浏览器已经在运行了"
                self.unique_id = str(unique_id or "").strip()
                self.owner = self.unique_id
                self.username = str(username or "").strip() or "账号1"
                self.error = ""
                self.saved = ""
                self.png = None
                self.forced = False
                self.last_frame_at = 0.0
                self.last_progress_at = time.time()   # 3 分钟没动静的倒计时从这一刻开始
                self.qr_png = None
                self.qr_hash = ""
                self.qr_at = 0.0
                self.qr_seen_at = 0.0
                self.qr_gone_at = 0.0
                # 上一轮留下的二级验证状态必须清掉：不清的话新一次授权一开跑
                # 就会带着「手动模式」和上一张二级验证二维码，用户会看懵
                self.verify_qr = False
                self.verify_hint = ""
                self.verify_manual = False
                self._verify_pick_at = 0.0
                self._verify_pick_method = ""
                self._verify_face_hint_shown = False
                self.sms_proof = None
                self.sms_proof_at = 0.0
                self.phase = "opening"
                self.phone_login = False
                self.phone_digits = 0
                self.page_hint = ""
                self.sms_hint = ""
                self.sms_kind = "code"
                self.sms_result = {}
                self.text_result = {}
                self.started_at = time.time()
                self._last_login_click = 0.0
                self._sms_submitted_at = 0.0
                self._sms_fill = None
                self._sms_choice_at = 0.0
                self._sms_send_at = 0.0
                self._notes_at = 0.0
                self._qr_wait_hint_at = 0.0
                self.state = "starting"
                self.message = "正在启动浏览器…"
            # 清掉上一次遗留的「停止」等指令：不然刚点开始授权，浏览器会被旧指令立刻关掉
            while True:
                try:
                    self.commands.get_nowait()
                except queue.Empty:
                    break
            self.thread = threading.Thread(target=self._run, name="browser", daemon=True)
            self.thread.start()
            return True, "已启动"

    def stop(self) -> None:
        self.commands.put({"name": "stop"})

    def force_stop(self) -> None:
        """标记强制退出：即使循环卡在一次长时间操作里，回到循环也会立刻结束。"""
        self.forced = True
        self.commands.put({"name": "stop"})

    def send(self, name: str, **payload) -> None:
        self.commands.put({"name": name, **payload})

    def _run(self) -> None:
        playwright = browser = context = page = None
        try:
            from playwright.sync_api import sync_playwright

            playwright = sync_playwright().start()
            browser = playwright.chromium.launch(
                headless=True,
                args=[
                    "--disable-dev-shm-usage",
                    "--no-sandbox",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            context = browser.new_context(viewport=VIEWPORT, locale="zh-CN", user_agent=USER_AGENT)
            page = context.new_page()
            cdp = context.new_cdp_session(page)
            self._cdp = cdp          # 留着：验证码填进去那一瞬间要当场抓一张
            self._set(message="正在打开抖音聊天页…")
            page.goto(CHAT_URL, wait_until="domcontentloaded", timeout=120000)
            self._set(state="waiting", message="正在打开抖音登录页…")

            last_shot = 0.0
            last_check = 0.0
            started_at = time.time()
            phase_seen = self.phase
            verify_seen = self.verify_qr
            running = True
            while running:
                if self.forced:
                    running = False
                    self._set(state="idle", message="已被强制停止")
                    break
                while True:
                    try:
                        command = self.commands.get_nowait()
                    except queue.Empty:
                        break
                    name = command.get("name")
                    try:
                        if name == "stop":
                            running = False
                            self._set(state="idle", message="已停止授权浏览器")
                            break
                        # 有人动过这台浏览器（点画面、滚、按键、填手机号/验证码）就算有动静，
                        # 3 分钟没动静的倒计时从这里重新开始
                        self._touch()
                        if name == "click":
                            page.mouse.click(float(command.get("x", 0)), float(command.get("y", 0)))
                        elif name == "wheel":
                            page.mouse.wheel(0, float(command.get("dy", 0)))
                        elif name == "type":
                            page.keyboard.type(str(command.get("text", "")))
                        elif name == "press":
                            page.keyboard.press(str(command.get("key", "Enter")))
                        elif name == "goto":
                            page.goto(str(command.get("url", CHAT_URL)), wait_until="domcontentloaded", timeout=120000)
                        elif name == "phone":
                            self._submit_phone(page, str(command.get("phone") or ""))
                        elif name == "sms":
                            if self.phone_login:
                                self._submit_phone_code(page, str(command.get("code") or ""))
                            else:
                                self._submit_sms(page, str(command.get("code") or ""))
                        elif name == "pwd":
                            # 二级验证弹密码框：走独立通道，不跟"手机号登录"那条岔路混
                            # （那条是给验证码用的，会去填 input#button-input）
                            self._submit_pwd(page, str(command.get("password") or ""))
                        elif name == "text":
                            # 通用输入框：用户自己打的内容，敲进去就停，不排队点「验证」
                            self._submit_text(page, str(command.get("text") or ""))
                    except Exception as error:
                        self._set(message="操作失败：" + str(error))
                    time.sleep(0.05)

                if not running:
                    break

                now = time.time()
                if now - started_at > AUTO_AUTH_SECONDS:
                    running = False
                    self._set(
                        state="idle",
                        message="这次授权开太久了（超过 20 分钟），已自动关闭浏览器（需要时再点「开始授权」）",
                    )
                    break
                # 3 分钟没动静（没扫码、没人操作画面）就自动关掉：
                # 这台机器只有一个浏览器引擎，一直开着别人就都用不了。
                idle_left = AUTH_IDLE_STOP - (now - self._progress_at())
                if idle_left <= 0:
                    running = False
                    self._set(
                        state="idle",
                        message="%d 分钟没人操作，已自动关闭授权浏览器（别人可以用了；需要时再点「开始授权」）"
                        % (AUTH_IDLE_STOP // 60),
                    )
                    break
                if now - last_shot > 1.2:
                    try:
                        self._set(png=self._capture(cdp), last_frame_at=now)
                    except Exception:
                        pass
                    last_shot = now

                if now - last_check > 1.5:
                    last_check = now
                    try:
                        if self._login_step(page, context, cdp):
                            running = False
                        else:
                            self._settle_sms(page, context)
                    except Exception:
                        pass
                    # 阶段往前走（扫到了 / 进到验证码 / 登录成功）也算"有动静"：
                    # 扫码是在手机上完成的，面板这边只有阶段变了才知道人来了。
                    # 二级验证弹出来时也要重算：那得去拿另一台已登录的手机来扫。
                    if self.phase != phase_seen or self.verify_qr != verify_seen:
                        phase_seen = self.phase
                        verify_seen = self.verify_qr
                        self._touch()

                time.sleep(0.25)
        except Exception as error:
            self._set(state="error", error=str(error), message="浏览器启动失败：" + str(error))
        finally:
            for resource in (context, browser):
                if resource is not None:
                    try:
                        resource.close()
                    except Exception:
                        pass
            if playwright is not None:
                try:
                    playwright.stop()
                except Exception:
                    pass
            with self.lock:
                # 检测结束后把画面交还出去：不然实时画面窗一直挂着别人的最后一张图
                self.png = None
                self.last_frame_at = 0.0
                # 二维码是一次性的：关掉浏览器就别留着，免得别人看到你账号的登录码
                self.qr_png = None
                self.qr_hash = ""
                self.qr_at = 0.0
                self.qr_seen_at = 0.0
                self.qr_gone_at = 0.0
                self.verify_qr = False
                self.verify_hint = ""
                self.verify_manual = False
                self.sms_proof = None
                self.sms_proof_at = 0.0
                self._cdp = None
                self.sms_hint = ""
                self.page_hint = ""
                self._sms_fill = None
                if self.phase != "done":
                    self.phase = "idle"
                # 收工：把「这台浏览器是谁的」也清掉，面板不再显示「被某某占用」；
                # 号已经存好了，留着归属只会让下一个用的人以为这台浏览器还是别人的
                self.owner = ""
                self.unique_id = ""
                self.thread = None

    @staticmethod
    def _capture(cdp) -> bytes:
        # 用 CDP 直接取图，不走截图前等待页面稳定的逻辑（有动画时 Playwright 会一直等到超时）
        result = cdp.send("Page.captureScreenshot", {"format": "png"})
        return base64.b64decode(result["data"])

    def _douyin_cookies(self, context) -> list:
        cookies = context.cookies()
        return [c for c in cookies if "douyin.com" in (c.get("domain") or "")]

    def _logged_in(self, cookies: list) -> bool:
        return any(c.get("name") in ("sessionid", "sessionid_ss") and c.get("value") for c in cookies)

    def _detect_login(self, context) -> bool:
        douyin = self._douyin_cookies(context)
        if not self._logged_in(douyin):
            return False
        if not (self.owner or self.unique_id):
            self._set(
                state="need_id",
                message="已检测到登录成功，但还没填「抖音号」。请在上方填写并保存配置，再点「导出 Cookies」",
            )
            return False
        self._store(douyin)
        return True

    def _store(self, cookies: list) -> None:
        cleaned = []
        for cookie in cookies:
            item = {k: v for k, v in cookie.items() if k != "sameSite" and v is not None}
            cleaned.append(item)
        # 存到哪个账号以 owner 为准：unique_id 会被「保存配置」顺手改，owner 不会
        uid = str(self.owner or self.unique_id or "").strip()
        if not uid:
            self._set(message="还没给这台浏览器指定抖音号，先在上面的配置里填好再试")
            return
        key = cookie_key(uid)

        existing = load_accounts().get(uid)
        if isinstance(existing, dict):
            save_cookies(uid, cleaned)
        else:
            save_account(
                uid,
                username=self.username or uid,
                targets=[],
                cookies=encrypt_text(
                    json.dumps(_clean_cookie_list(cleaned), ensure_ascii=False, separators=(",", ":"))
                ),
            )
        try:
            RELOGIN_PATH.unlink()
        except Exception:
            pass
        now = now_text()
        # 只记「这个号什么时候授权的」，**不要**往 checks 里写。
        # checks 是「登录检测」的结果，授权成功 != 检测通过：授权只说明 Cookie 存下来了，
        # 这个号还能不能发消息得等检测跑一遍。以前在这里写一条 ok=True，
        # 界面上刚授权的号立刻变成绿色的「登录正常」，看着全绿，真去发才发现早掉线了。
        # 想让新授权的号显示正常？点「检测所有账号」跑一遍。
        save_state({"saved_at": {uid: now}})
        self._set(
            state="authorized",
            saved=key,
            phase="done",
            message="登录成功，已保存 %d 个 Cookie 项到 %s" % (len(cleaned), key),
        )

    # ------------------------------------------------------------------
    # 扫码登录流程
    # ------------------------------------------------------------------
    def qr_image(self):
        with self.lock:
            return self.qr_png

    def sms_proof_image(self):
        with self.lock:
            return self.sms_proof

    @staticmethod
    def _has_words(page, words) -> str:
        try:
            got = page.evaluate(_JS_HAS_WORDS, list(words))
        except Exception:
            return ""
        return str(got or "")

    def _has_words_any(self, page, words) -> str:
        """主页面和各个 iframe 都找一遍：二级验证的弹窗常常在 iframe 里"""
        hit = self._has_words(page, words)
        if hit:
            return hit
        for frame in self._sms_frames(page):
            if frame is page:
                continue
            got = self._has_words(frame, words)
            if got:
                return got
        return ""

    def _find_qr(self, page):
        try:
            got = page.evaluate(_JS_FIND_QR)
        except Exception:
            got = None
        if isinstance(got, dict):
            return got
        # 二级验证的弹窗常常在 iframe 里，主页面找不到就挨个 iframe 找一遍；
        # iframe 里的坐标是相对它自己的，得加上 iframe 在整页里的位置，否则裁出来的图是错的
        for frame in self._sms_frames(page):
            if frame is page:
                continue
            try:
                inner = frame.evaluate(_JS_FIND_QR)
            except Exception:
                continue
            if not isinstance(inner, dict):
                continue
            try:
                box = frame.frame_element().bounding_box()
            except Exception:
                box = None
            if not box:
                continue
            moved = dict(inner)
            moved["x"] = float(inner.get("x") or 0) + float(box.get("x") or 0)
            moved["y"] = float(inner.get("y") or 0) + float(box.get("y") or 0)
            return moved
        return None

    def _qr_png_of(self, qr, cdp) -> bytes:
        """把二维码弄成 PNG 字节：能直接读原图就读原图，否则按元素位置裁一张。"""
        raw = _data_url_bytes(qr.get("src"))
        if _png_side(raw) >= QR_MIN_SIDE:
            return raw
        try:
            clip = {
                "x": max(0, int(qr.get("x") or 0)),
                "y": max(0, int(qr.get("y") or 0)),
                "width": max(1, int(qr.get("w") or 0)),
                "height": max(1, int(qr.get("h") or 0)),
                "scale": 3,
            }
            shot = cdp.send("Page.captureScreenshot", {"format": "png", "clip": clip})
            raw = base64.b64decode(shot["data"])
        except Exception:
            return b""
        return raw if _png_side(raw) >= QR_MIN_SIDE else b""

    def _sms_frames(self, page):
        """主页面 + 各个 iframe 都算上（抖音的弹窗有时候在 iframe 里）"""
        frames = [page]
        try:
            for frame in page.frames:
                if frame is page.main_frame:
                    continue
                frames.append(frame)
        except Exception:
            pass
        return frames

    def _note_sms_kind(self, kind) -> None:
        """记下这一步要填的是「验证码」还是「登录密码」——弹窗照它换控件。

        换种的时候顺手把上一条结果清掉：不然会拿"验证码提交成功"去解释密码这一步。
        """
        kind = "pwd" if str(kind or "") == "pwd" else "code"
        with self.lock:
            changed = (self.sms_kind != kind)
            self.sms_kind = kind
        if changed:
            self._set(sms_result={})

    def _sms_target(self, page):
        """找验证码框：找到就给 (哪个页面帧, 框的信息)

        手机号登录那一栏也有验证码框，区分办法：看到“如何扫码 / 验证码登录”
        这种选项卡的字（说明还在登录弹窗里）但没有“安全验证 / 已发送”这种二级验证的字，就不算。
        """
        for frame in self._sms_frames(page):
            try:
                box = frame.evaluate(_JS_SMS_BOX)
            except Exception:
                continue
            if not isinstance(box, dict):
                continue
            modal = self._has_words(frame, MODAL_WORDS)
            strong = self._has_words(frame, SMS_STRONG_WORDS)
            if modal and not strong:
                return None
            self._note_sms_kind(box.get("kind"))
            return (frame, box)
        return None

    def _sms_box(self, page):
        """只要框本身（给状态机判断用），没有就返 None"""
        target = self._sms_target(page)
        return target[1] if target else None

    def _sms_mouse(self, frame):
        """拿到能用真鼠标/真键盘的那个对象（frame 可能是 iframe，也可能是主页面）"""
        return getattr(frame, "page", None) or frame

    def _sms_focused(self, frame, pos: int) -> bool:
        """光标是不是真的落在标记的那个验证码框里"""
        try:
            return bool(frame.evaluate(_JS_SMS_FOCUSED, int(pos)))
        except Exception:
            return False

    def _type_sms(self, frame, digits: str, multi: bool, clear: bool = False) -> bool:
        """真鼠标点框 + 真键盘一个个数字敲，敲完先核对焦点。

        返回 True 只代表“每一格都真的点进去、真的敲了”，不代表页面接受。
        """
        try:
            fields = frame.locator('[data-wz-sms="1"]')
            count = fields.count()
        except Exception as error:
            self._sms_error = str(error)[:160]
            return False
        if count < 1:
            self._sms_error = "页面上没标记到验证码框"
            return False
        host = self._sms_mouse(frame)
        chunks = list(digits) if multi else [digits]
        for pos in range(min(count, len(chunks))):
            field = fields.nth(pos)
            try:
                spot = field.bounding_box()
            except Exception as error:
                self._sms_error = str(error)[:160]
                return False
            if not spot:
                self._sms_error = "验证码框画在屏幕外面，量不到位置"
                return False
            try:
                host.mouse.click(spot["x"] + spot["width"] / 2,
                                 spot["y"] + spot["height"] / 2)
            except Exception as error:
                self._sms_error = "鼠标点不到验证码框：" + str(error)[:120]
                return False
            if not self._sms_focused(frame, pos):
                # 真鼠标没把焦点送进去：再用页内脚本 focus 一次，还是要核对
                try:
                    frame.evaluate(_JS_SMS_FOCUS)
                except Exception:
                    pass
                if not self._sms_focused(frame, pos):
                    self._sms_error = "鼠标点下去了，但光标没进验证码框"
                    return False
            if clear:
                try:
                    host.keyboard.press("Control+a")
                    host.keyboard.press("Backspace")
                except Exception:
                    pass
            try:
                host.keyboard.type(chunks[pos], delay=SMS_TYPE_DELAY)
            except Exception as error:
                self._sms_error = str(error)[:160]
                return False
        return True

    def _fill_sms(self, page, digits: str) -> str:
        """真正把验证码敲进抖音的框里

        跟人手动填用的是同一套动作：真鼠标点在框上（点完先核对光标真的进了那个框），
        再用真键盘一个数字一个数字敲。抖音页面上常留着一个看不见的同名框，
        “挑哪个框”在 _JS_SMS_BOX 里定（真鼠标点得着 + 离「验证」按钮最近），
        并且挑好的框会打上标记，后面填、读都只认这一个，不会再各找各的。
        """
        self._sms_error = ""
        target = self._sms_target(page)
        if target is None:
            return ""
        frame, box = target
        idxs = box.get("idxs") or [box.get("idx")]
        idxs = [int(i) for i in idxs if i is not None]
        multi = len(idxs) > 1
        for attempt in (0, 1):
            if self._type_sms(frame, digits, multi, clear=(attempt == 1)):
                break
        got = self._sms_value(page, frame)
        self._note_sms_box(got)
        if digits in got:
            return got
        # 最后一招：用 React 原生 setter 写进去（写不进去也不会谎报）
        try:
            frame.evaluate(_JS_SMS_TYPE, digits)
        except Exception as error:
            self._sms_error = self._sms_error or str(error)[:160]
        got = self._sms_value(page, frame)
        self._note_sms_box(got)
        return got

    def _snap_now(self, proof: bool = False) -> None:
        """立刻抓一张，别等主循环那 1.2 秒一张的节奏。

        proof=True 时顺手留一份「填码那一刻」的定格图给弹窗看。
        """
        cdp = self._cdp
        if cdp is None or self.forced:
            return
        try:
            png = self._capture(cdp)
        except Exception:
            return
        if not png:
            return
        stamp = time.time()
        if proof:
            self._set(png=png, last_frame_at=stamp, sms_proof=png, sms_proof_at=stamp)
        else:
            self._set(png=png, last_frame_at=stamp)

    def _note_sms_box(self, got) -> None:
        """记下「抖音的框里现在有几位」——弹窗就靠它告诉用户码到底进没进去"""
        with self.lock:
            self.sms_box_len = len(str(got or ""))
            self.sms_box_at = time.time()

    def _log_sms_probe(self, page) -> None:
        """把验证码页的结构记一条日志（不记输入的内容，只记长度），方便以后查"""
        try:
            for frame in self._sms_frames(page):
                probe = frame.evaluate(_JS_SMS_PROBE)
                if probe:
                    append_text(SMS_PROBE_LOG, "%s  %s\n" % (now_text(), str(probe)[:900]))
                    break
        except Exception:
            pass


    # ---- 手机号登录：面板的主路径（扫码退到备用）--------------------------------
    def _fill_input(self, page, selector: str, value: str) -> str:
        """把值写进抖音的输入框，返回框里实际剩下的内容。

        先用 Playwright 的 fill（它发出的 input 事件 React 认），不行再用
        原生 setter 兜一次 —— 抖音的输入框是受控组件，直接改 value 会被抹掉。
        """
        try:
            page.fill(selector, "", timeout=4000)
            page.fill(selector, value, timeout=4000)
            return str(page.input_value(selector, timeout=2000) or "")
        except Exception:
            pass
        try:
            got = page.evaluate(_JS_FILL_REACT_INPUT, {"selector": selector, "value": value})
        except Exception:
            return ""
        return str(got.get("value") or "") if isinstance(got, dict) else ""

    def _click_text_mouse(self, page, texts) -> str:
        """按文字找到元素，用真鼠标点它，返回点到的文字。

        抖音的发验证码/登录按钮用页内合成 click 点不动（试过，接口根本不发），
        所以这里只算坐标，点击交给 page.mouse。
        """
        try:
            hit = page.evaluate(_JS_FIND_TEXT_CENTER, list(texts))
        except Exception:
            return ""
        if not (isinstance(hit, dict) and hit.get("ok")):
            return ""
        x = float(hit.get("x") or 0)
        y = float(hit.get("y") or 0)
        try:
            page.mouse.move(x, y)
            page.wait_for_timeout(60)
            page.mouse.click(x, y)
        except Exception:
            return ""
        return str(hit.get("text") or "")

    def _click_text_any(self, page, texts):
        """主页面和各个 iframe 依次找那个字去点，点到就返回点到的那个元素信息。

        二级验证的弹窗经常在 iframe 里，只点主页面会点空。这里用**页内合成 click**
        （不是真鼠标）：选择页那两个按钮是普通 div，合成 click 收得到；而真鼠标得先拿坐标，
        iframe 里的坐标跟主页面不是一个坐标系，反而更容易点歪。
        """
        for frame in self._sms_frames(page):
            try:
                got = frame.evaluate(_JS_CLICK_BY_TEXT, list(texts))
            except Exception:
                continue
            if isinstance(got, dict) and got.get("ok"):
                return got
        return None

    def _submit_phone(self, page, phone: str) -> None:
        """手机号登录第一步：切到「验证码登录」-> 填手机号 -> 点「获取验证码」。"""
        digits = re.sub(r"\D", "", phone or "")
        if not re.fullmatch(r"1\d{10}", digits):
            self._set(message="手机号要填 11 位数字，检查一下")
            return

        with self.lock:
            self.phone_login = True
        self._snap_now()

        # 1) 登录弹窗没开就先点开
        if not self._has_words(page, MODAL_WORDS):
            try:
                page.evaluate(_JS_CLICK_LOGIN, LOGIN_ENTRY_TEXTS)
            except Exception:
                pass
            page.wait_for_timeout(1800)

        # 2) 切到「验证码登录」：弹窗默认停在扫码，不切进去就没有手机号那个框
        picked = self._click_text_mouse(page, PHONE_TAB_TEXTS)
        if picked:
            self._set(message="已切到「%s」" % picked)
            page.wait_for_timeout(1500)

        # 3) 填手机号（只回长度，手机号本身不写日志）
        got = re.sub(r"\D", "", self._fill_input(page, "input#normal-input", digits))
        with self.lock:
            self.phone_digits = len(got)
        if not got:
            self._set(
                message="没找到抖音的手机号输入框。请在下面截图里点一下手机号那一栏，"
                        "再用上面那行「输入并发送」把号码填进去。",
                page_hint="请手动点手机号输入框",
            )
            return
        if got != digits:
            self._set(message="手机号只填进去 %d 位，请检查一下（也可以手动补全）" % len(got))
            return
        page.wait_for_timeout(900)

        # 4) 点「获取验证码」，同时看一眼抖音接口的返回 —— 光看页面文字看不出成败
        clicked = ""
        verdict = ""
        detail = ""
        try:
            with page.expect_response(
                lambda r: "/passport/web/" in r.url
                and not re.search(
                    r"challenge|qrcode|qrconnect|ticket_guard|login_guiding|ttwid",
                    r.url,
                    re.I,
                ),
                timeout=9000,
            ) as caught:
                clicked = self._click_text_mouse(page, SMS_SEND_TEXTS)
            try:
                raw = caught.value.text()[:400]
            except Exception:
                raw = ""
            verdict, detail = judge_send_response(raw)
        except Exception:
            clicked = clicked or ""
            page.wait_for_timeout(300)

        with self.lock:
            self.phase = "sms"

        if clicked and verdict != "bad":
            self._sms_send_at = time.time()
            self._set(
                message="手机号已经填进抖音并点了「%s」，短信到了就把验证码填在下面。" % clicked,
                sms_hint="验证码已经发到你手机上：填在下面，我替你填进抖音并点「登录」。",
                sms_result={"ok": True, "at": now_text(), "message": "已请求发送短信验证码"},
            )
            return
        if verdict == "bad":
            self._set(
                message="抖音没接受这次发送（接口返回：%s）。检查一下号码，"
                        "或者到下面截图里手动试一次。想自己操作画面就切到上面的「手动授权」页签。" % (detail or "未说明原因"),
                sms_hint="抖音拒绝了这次验证码发送，看上面的提示。",
                sms_result={"ok": False, "at": now_text(), "message": detail or "抖音拒绝了这次发送"},
            )
            return
        self._set(
            message="手机号填好了，但没能确认抖音发出验证码。请在下面截图里手动点一次「获取验证码」。",
            page_hint="请手动点「获取验证码」",
        )

    def _submit_phone_code(self, page, code: str) -> None:
        """手机号登录第二步：把验证码填进抖音的框，再用真鼠标点「登录」。"""
        digits = re.sub(r"\D", "", code or "")
        if not digits:
            return
        got = re.sub(r"\D", "", self._fill_input(page, "input#button-input", digits))
        with self.lock:
            self.sms_box_len = len(got)
            self.sms_box_at = time.time()
        if not got:
            self._set(
                phase="sms",
                sms_result={"ok": False, "at": now_text(),
                            "message": "验证码没写进抖音的框里，请在截图里点一下那个框再填一次"},
            )
            return
        if got != digits:
            self._set(
                phase="sms",
                sms_result={"ok": False, "at": now_text(),
                            "message": "验证码只进去 %d 位，再填一次" % len(got)},
            )
            return

        # 抖音认到码之后「登录」才会亮，等一会儿再点
        page.wait_for_timeout(int(SMS_BEFORE_SUBMIT_WAIT * 1000))
        clicked = ""
        for _ in range(3):
            clicked = self._click_text_mouse(page, PHONE_SUBMIT_TEXTS)
            if clicked:
                break
            page.wait_for_timeout(1500)
        with self.lock:
            self._sms_submitted_at = time.time()
        if clicked:
            self._set(
                phase="scanned",
                sms_result={"ok": True, "at": now_text(),
                            "message": "验证码已经填进抖音并点了「%s」，等结果…" % clicked},
            )
        else:
            self._set(
                phase="sms",
                sms_result={"ok": False, "at": now_text(),
                            "message": "验证码填好了，但没找到抖音的「登录」按钮，"
                                       "请在截图里手动点一下登录"},
            )

    def _click_verify_method(self, page, methods):
        """按调用方给出的优先顺序，在主页面和 iframe 中尝试验证入口。"""
        text_by_method = {
            "phone": SMS_CHOICE_TEXTS,
            "device": VERIFY_DEVICE_PICK_TEXTS,
            "face": VERIFY_FACE_PICK_TEXTS,
        }
        for method in methods:
            picked = self._click_text_any(page, text_by_method[method])
            if picked:
                return method, picked
        return "", None

    def _is_verify_choice_page(self, page) -> bool:
        """只把明确的方式选择页当作选择页；单独的人脸/扫码页不触发点选。"""
        prompt = self._has_words_any(page, VERIFY_CHOICE_PROMPT_WORDS)
        phone = self._has_words_any(page, VERIFY_PHONE_WORDS)
        device = self._has_words_any(page, VERIFY_DEVICE_WORDS)
        face = self._has_words_any(page, VERIFY_FACE_WORDS)
        options = sum(bool(x) for x in (phone, device, face))
        # 某些抖音版本只显示“身份验证”和按钮文字，不显示“选择验证方式”提示。
        # 直接命中三类可选入口也视为选择页，避免把旧登录二维码继续当二级二维码。
        direct_words = (
            "接收短信验证码", "通过短信验证", "使用短信验证", "手机短信验证",
            "手机号验证", "手机号码验证", "发送短信验证", "发送短信验证码",
        ) + VERIFY_DEVICE_PICK_TEXTS + VERIFY_FACE_PICK_TEXTS
        direct_option = self._has_words_any(page, direct_words)
        return bool((prompt and options >= 1) or options >= 2 or direct_option)

    def _login_step(self, page, context, cdp) -> bool:
        """授权流程的大脑：自动点开登录 -> 扒二维码 -> 扫完进二级验证。

        返回 True 表示已经登录成功（Cookie 存好了）。
        """
        if self._detect_login(context):
            self._sms_fill = None
            with self.lock:
                # 登录成功就退出「手动模式」：前端据此把手动提示条收掉
                self.verify_manual = False
            self._set(phase="done", sms_hint="")
            return True

        now = time.time()

        # ---- 00) 进二级验证 → 切到「手动模式」，把可操作画面交给用户 ----
        # 用户要的是：二级验证一来就把实时画面摆出来让人自己操作（那块画面可以点），
        # 输入用那个通用输入框。验证方式选择页按手机号、原设备、人脸的顺序尝试；
        # 页面直接给二维码时不点击，直接展示给用户扫码。
        # 判据三条，命中一条就算进了二级验证：
        #   ① 已经抓到二级验证的二维码   ② 已经提交过验证码（抖音正在要下一关）
        #   ③ 页面上出现二级验证的强特征词
        # 一旦进来就不再退出去：中间状态会闪，而画面框架反复摊开收起会让人没法操作。
        if not self.verify_manual:
            if (self.verify_qr
                    or self._sms_submitted_at
                    or (self.qr_seen_at and self._is_verify_choice_page(page))
                    or self._has_words_any(page, VERIFY_STAGE_WORDS)):
                with self.lock:
                    self.verify_manual = True
                self._set(
                    message="抖音要求二级验证 —— 下面那块画面可以直接点，"
                            "也可以打字用下面那个通用输入框；如果出现方式选择，会先试手机号，"
                            "再试原设备，最后才试人脸。身份核验请在抖音 App 中完成。"
                )

        # ---- 0a) 验证码已经敲进抖音的框里了：等几秒，再替用户点「验证」 ----
        fill = self._sms_fill
        if fill and now - float(fill.get("at") or 0) >= SMS_BEFORE_SUBMIT_WAIT:
            self._sms_fill = None
            self._click_sms_verify(
                page, str(fill.get("code") or ""), int(fill.get("tries") or 0)
            )
            return False

        # ---- 1) 弹窗还没出来就自己点开「登录」----
        if not self.qr_seen_at and now - self.started_at > 2.0:
            if not self._has_words(page, MODAL_WORDS) and now - self._last_login_click > 5.0:
                self._last_login_click = now
                try:
                    hit = page.evaluate(_JS_CLICK_LOGIN, LOGIN_ENTRY_TEXTS)
                except Exception:
                    hit = None
                if isinstance(hit, dict) and hit.get("ok"):
                    self._set(message="已自动打开抖音登录页：请用抖音 App 扫码；遇到验证时按页面提示完成")

        qr = self._find_qr(page)

        # 方式选择页没有二级二维码时，不能沿用登录阶段缓存的旧二维码。
        # 抖音有时会把登录二维码节点留在 DOM 里，用户已经进入身份验证
        # 选择页后仍会被误判成二级二维码；此时清掉旧图，只处理选择按钮。
        sms_box_now = self._sms_box(page)
        choice_page_now = self._is_verify_choice_page(page)
        if not choice_page_now and not sms_box_now:
            identity_hint = self._has_words_any(
                page, ("身份验证", "安全验证", "双重验证", "登录双重验证")
            )
            option_hint = self._has_words_any(
                page, (
                    "接收短信验证码", "通过短信验证", "使用短信验证",
                    "手机短信验证", "手机号验证", "手机号码验证",
                    "发送短信验证", "发送短信验证码",
                ) + VERIFY_DEVICE_PICK_TEXTS + VERIFY_FACE_PICK_TEXTS
            )
            choice_page_now = bool(identity_hint and option_hint)
        # 方式选择页优先于任何残留的二维码节点；这里的二维码一定是旧登录码，
        # 只有没有方式选择页时才允许二维码进入二级验证展示。
        if choice_page_now:
            with self.lock:
                self.verify_manual = True
                self.qr_png = None
                self.qr_hash = ""
                self.qr_at = 0.0
                self.verify_qr = False
                self.verify_hint = ""
            qr = None

        # 已选择手机号验证、但页面还需要再点一次「获取验证码」时，补点一次。
        # 二维码已直接出现时不碰页面上的任何验证选项，只负责把二维码展示出来。
        if (
            not qr
            and self.qr_seen_at
            and self._sms_choice_at
            and now - self._sms_choice_at > 8.0
            and now - self._sms_send_at > SMS_SEND_EVERY
            and not self._sms_box(page)
            and self._has_words_any(page, SMS_SEND_TEXTS)
        ):
            self._sms_send_at = now
            sent = self._click_text_any(page, SMS_SEND_TEXTS)
            if sent:
                self._set(
                    message="现在帮你点了「%s」，短信马上到。"
                            % str(sent.get("text") or SMS_SEND_TEXTS[0]),
                    sms_hint="已经帮你发了一条短信验证码，收到后填在下面。",
                )

        # ---- 1c) 只有出现验证选项时才按手机号 -> 原设备 -> 人脸选择 ----
        # 直接出现二维码时绝不点任何选项，只把码展示出来；直接进入人脸验证页也不当作选择页。
        sms_box = self._sms_box(page)
        # 选择页可能还留着登录弹窗的隐藏验证码输入框；不能因此跳过方式选择。
        # 只有明确出现二级验证标题/提示和验证入口时才会进入这里。
        choice_page = bool(choice_page_now or self._is_verify_choice_page(page))
        if not choice_page and self._verify_pick_method:
            # 上一项已离开选择页，视为抖音已经切换到下一步；后续若再弹新选择页重新优先扫码。
            self._verify_pick_method = ""
            self._verify_pick_at = 0.0
        if not qr and self.qr_seen_at and self.verify_manual and choice_page:
            method = self._verify_pick_method
            if not method:
                method, picked = self._click_verify_method(page, ("phone", "device", "face"))
                if method == "phone":
                    self._verify_pick_method = method
                    self._verify_pick_at = now
                    self._sms_choice_at = now
                    self._sms_send_at = 0.0
                    label = str(picked.get("text") or "手机号/短信验证")
                    self._set(
                        phase="sms",
                        message="抖音提供手机号验证，已优先选择「%s」。收到短信后把验证码填在下方。"
                                % label,
                        sms_hint="已选择手机号/短信验证。把抖音发送的验证码填在下面；如果此方式无法继续，会再尝试原设备扫码。",
                    )
                elif method == "device":
                    self._verify_pick_method = method
                    self._verify_pick_at = now
                    self._sms_choice_at = 0.0
                    self._sms_send_at = 0.0
                    label = str(picked.get("text") or "原设备扫码")
                    self._set(
                        phase="scanned",
                        sms_hint="",
                        message="当前没有可用的手机号验证入口，已选择「%s」。如果页面仍无法继续，会再尝试人脸验证。"
                                % label,
                        page_hint="已选择原设备验证；请用已登录的抖音 App 扫描页面二维码。",
                    )
                elif method == "face":
                    self._verify_pick_method = method
                    self._verify_pick_at = now
                    self._sms_choice_at = 0.0
                    self._sms_send_at = 0.0
                    label = str(picked.get("text") or "人脸验证")
                    self._set(
                        phase="scanned",
                        sms_hint="",
                        message="手机号和原设备验证入口均不可用，已回退到「%s」。请在抖音 App 中按提示完成人脸核验。"
                                % label,
                        page_hint="人脸验证需要你在抖音 App 中亲自完成。",
                    )
                else:
                    self._verify_pick_method = "manual"
                    self._verify_pick_at = now
                    self._set(
                        phase="scanned",
                        sms_hint="",
                        message="没能识别验证方式按钮。请在下方可点击画面里按顺序选择手机号验证、原设备验证，最后再选人脸验证。",
                        page_hint="可以直接点击下方抖音画面选择验证方式。",
                    )
                return False

            # 手机号方式已经出现验证码输入框时，保持手机号流程，不回退到手动操作。
            if method == "phone" and not sms_box and now - self._verify_pick_at >= VERIFY_PHONE_FALLBACK_AFTER:
                next_method, picked = self._click_verify_method(page, ("device", "face"))
                self._sms_choice_at = 0.0
                self._sms_send_at = 0.0
                if next_method == "device":
                    self._verify_pick_method = next_method
                    self._verify_pick_at = now
                    label = str(picked.get("text") or "原设备扫码")
                    self._set(
                        phase="scanned",
                        sms_hint="",
                        message="手机号验证没有继续，已回退到「%s」。如果页面仍无法继续，会再尝试人脸验证。"
                                % label,
                        page_hint="请用已登录的抖音 App 扫描页面二维码。",
                    )
                elif next_method == "face":
                    self._verify_pick_method = next_method
                    self._verify_pick_at = now
                    label = str(picked.get("text") or "人脸验证")
                    self._set(
                        phase="scanned",
                        sms_hint="",
                        message="手机号验证无法继续，且没有可用的原设备入口，已回退到「%s」。请在抖音 App 中按提示完成人脸核验。"
                                % label,
                        page_hint="人脸验证需要你在抖音 App 中亲自完成。",
                    )
                else:
                    self._verify_pick_method = "manual"
                    self._verify_pick_at = now
                    self._set(
                        phase="scanned",
                        sms_hint="",
                        message="手机号验证无法继续，也没有识别到原设备或人脸选项。请在下方可点击画面中手动选择。",
                        page_hint="验证画面可点击；实际核验请在抖音 App 中完成。",
                    )
                return False

            if method == "device" and now - self._verify_pick_at >= VERIFY_DEVICE_FALLBACK_AFTER:
                picked = self._click_text_any(page, VERIFY_FACE_PICK_TEXTS)
                if picked:
                    self._verify_pick_method = "face"
                    self._verify_pick_at = now
                    label = str(picked.get("text") or "人脸验证")
                    self._set(
                        phase="scanned",
                        sms_hint="",
                        message="原设备扫码入口没有继续，已回退到「%s」。请在抖音 App 中按提示完成人脸验证。"
                                % label,
                        page_hint="人脸验证需要你在抖音 App 中亲自完成。",
                    )
                else:
                    self._verify_pick_method = "manual"
                    self._verify_pick_at = now
                    self._set(
                        phase="scanned",
                        sms_hint="",
                        message="原设备扫码入口没有继续，且没能自动选择人脸验证。请在下方可点击画面里选择人脸验证。",
                        page_hint="验证画面可点击；请在抖音 App 中完成后续核验。",
                    )
                return False

            if method == "face" and now - self._verify_pick_at >= VERIFY_FACE_MANUAL_AFTER:
                self._verify_pick_method = "manual"
                self._set(
                    message="已尝试打开人脸验证，但页面仍停在方式选择。请在下方可点击画面里选择验证方式，并按抖音 App 提示完成。",
                    page_hint="验证画面可点击；人脸核验由你在抖音 App 中完成。",
                )
                return False
            return False

        # 二级验证直接进入人脸页时不替用户采集或提交人脸，只提示下一步由抖音 App 完成。
        face_stage = bool(not qr and self.verify_manual
                          and self._has_words_any(page, VERIFY_FACE_STAGE_WORDS))
        if face_stage and not self._verify_face_hint_shown:
            self._verify_face_hint_shown = True
            self._set(
                message="抖音正在进行人脸验证，请在手机抖音 App 中按页面提示完成。下方画面可以点击查看；网页不会代替你完成人脸核验。",
                page_hint="请在抖音 App 中完成身份验证。",
            )
        elif not face_stage:
            self._verify_face_hint_shown = False

        if not qr and self.verify_qr:
            # 二维码没了（扫过了 / 验证过了）就把二级验证的提示收掉
            with self.lock:
                self.verify_qr = False
                self.verify_hint = ""

        # ---- 2) 有二维码就抓下来给用户扫 ----
        if qr:
            raw = self._qr_png_of(qr, cdp)
            if raw:
                digest = hashlib.sha256(raw).hexdigest()[:16]
                # 页面上写着「用已登录的设备扫码」这类字 = 这是二级验证的码，不是登录码
                verify_words = self._has_words_any(page, VERIFY_QR_WORDS)
                is_verify_qr = bool(verify_words or (self.verify_manual and not choice_page_now))
                with self.lock:
                    fresh = digest != self.qr_hash
                    self.qr_png = raw
                    self.qr_hash = digest
                    if fresh:
                        self.qr_at = now
                    if not self.qr_seen_at:
                        self.qr_seen_at = now
                        self.phase = "qr"
                        self.message = "请用抖音 App 扫描页面上的二维码并确认登录；如果二维码无法识别，可在「手动授权」中操作画面"
                    elif self.phase in ("idle", "opening", "manual"):
                        self.phase = "qr"
                    self.qr_gone_at = 0.0
                    if is_verify_qr:
                        self.verify_qr = True
                        self.verify_hint = ("抖音要求二级验证：用你手机上已登录的抖音 App " +
                                            "扫下面这个二维码" +
                                            "（如果要扫脸，就在 App 里按提示做）")
                        self.message = self.verify_hint
                    else:
                        # 验证页换了二维码但没有可识别的提示词时，也要把当前二维码显示出来。
                        self.phase = "qr"
                        self.message = "抖音显示了新的二维码，请用抖音 App 扫描并按页面提示完成验证。"
                return False

        # ---- 3) 一直没二维码：别让用户干等，直接说清楚 ----
        if not self.qr_seen_at and now - self.started_at > QR_FIRST_WAIT:
            with self.lock:
                if self.phase in ("opening", "idle"):
                    self.phase = "manual"
            self._set(
                message="没能自动取出二维码（抖音可能改版了）：请用下面的截图手动点「登录」，"
                        "再点这个页面里的二维码"
            )
            return False

        # ---- 4) 二维码过期了就替用户点「刷新」----
        if self.qr_seen_at and self.qr_at and now - self.qr_at > QR_REFRESH_AFTER:
            if self._has_words(page, QR_EXPIRED_WORDS):
                try:
                    page.evaluate(_JS_QR_REFRESH)
                except Exception:
                    pass

        # ---- 5) 二维码不见了：可能扫过了，进入二级验证 ----
        if self.qr_seen_at:
            if not self.qr_gone_at:
                self.qr_gone_at = now
            if now - self.qr_gone_at >= SMS_AFTER_QR_GONE:
                box = self._sms_box(page)
                if box:
                    with self.lock:
                        if self.phase != "sms":
                            self.phase = "sms"
                            self.message = "需要手机验证码"
                    self._set(sms_hint=self._sent_hint(page))
                else:
                    with self.lock:
                        if self.phase in ("qr",):
                            self.phase = "scanned"
                            self.message = "已经扫到二维码了，正在等抖音确认…"

        # ---- 6) 顺手把页面上的提示/报错抓回来 ----
        if now - self._notes_at > 3.0:
            self._notes_at = now
            notes = self._page_notes(page)
            if notes:
                hint = _pick_note(notes, SMS_BAD_WORDS) or ""
                if hint:
                    self._set(page_hint="抖音说：" + hint)
                else:
                    sent = _pick_note(notes, ("已发送", "发送至"))
                    self._set(page_hint=sent or "")
        return False

    def _page_notes(self, page) -> list:
        try:
            got = page.evaluate(_JS_PAGE_NOTES)
        except Exception:
            return []
        return [str(x) for x in got] if isinstance(got, list) else []

    def _sent_hint(self, page) -> str:
        """二级验证页上的说明："验证码已发送至 138****8888"、"为了你的账号安全…"。

        能拿到原话就直接给用户看原话：比我们自己编一句更准确。
        """
        sent = _pick_note(
            self._page_notes(page),
            ("已发送", "发送至", "验证码已", "短信验证", "账号安全", "请完成", "完成验证",
             "登录密码"),
        )
        if sent:
            return sent
        if self.sms_kind == "pwd":
            return "抖音要你做一次安全验证：这次要的是你的抖音登录密码，填在下面"
        return "抖音要你做一次安全验证：把手机收到的验证码填到下面"

    def _submit_sms(self, page, code: str) -> None:
        """用户把手机上收到的验证码填进来：先真的敲进抖音的框里，

        敲进去之后先不点验证，等几秒让抖音反应过来，再由主循环去点（见 _click_sms_verify）。
        """
        digits = "".join(ch for ch in str(code or "") if ch.isdigit())
        if not digits:
            self._set(sms_result={"ok": False, "at": now_text(),
                                  "message": "验证码应该是手机收到的那串数字"})
            return
        box = self._sms_box(page)
        if box is None:
            raw = None
            try:
                raw = page.evaluate(_JS_SMS_BOX)
            except Exception:
                raw = None
            if isinstance(raw, dict):
                msg = ("现在页面上那个是「手机号登录」用的验证码框，"
                       "还没到二级验证这一步")
            else:
                msg = ("抖音页面上没找到验证码输入框（可能已经验证过了，"
                       "或者这一步不需要验证码）。状态没有变化")
            self._set(sms_result={"ok": False, "at": now_text(), "message": msg})
            return

        self._log_sms_probe(page)
        got = self._fill_sms(page, digits)
        self._note_sms_box(got)
        if digits not in got:
            why = self._sms_error or "页面上的框可能换样了"
            self._set(
                phase="sms",
                sms_result={"ok": False, "at": now_text(),
                            "message": "验证码没能敲进抖音的框里（%s）。"
                                       "你可以直接点截图里那个输入框手动填，或把截图发我" % why},
            )
            return

        # 当场抓一张留证：抖音的框填满几位就自己往下走，等主循环那 1.2 秒的
        # 节奏，拍到的框里已经空了 —— 用户就以为码没填进去。
        self._snap_now(proof=True)
        with self.lock:
            self._sms_fill = {"code": digits, "at": time.time(), "tries": 0}
        self._set(
            phase="sms",
            sms_result={"ok": True, "at": now_text(),
                        "message": "验证码已经敲进抖音的框里了。"
                                   "等 %.0f 秒让抖音反应过来，然后我替你点「验证」。"
                                   % SMS_BEFORE_SUBMIT_WAIT},
        )

    def _submit_pwd(self, page, password: str) -> None:
        """二级验证弹的是「验证登录密码」：把用户填的密码真的敲进抖音那个框里。

        和验证码走同一套动作（真鼠标点框 + 真键盘敲），也走同一个"等几秒再点验证"
        的节奏（见 _click_sms_verify）—— 区别只有两处：不过滤字符、文案说的是密码。

        密码纪律：只在这一刻用一次。不写日志（_log_sms_probe 只记长度）、
        不进状态（sms_result 里只报位数）、不落盘；内存里留到"点完验证"为止。
        """
        pwd = str(password or "").replace("\r", "").replace("\n", "")
        if not pwd:
            self._set(sms_result={"ok": False, "at": now_text(),
                                  "message": "密码是空的，检查一下再填"})
            return
        if len(pwd) > SMS_PWD_MAX:
            self._set(sms_result={"ok": False, "at": now_text(),
                                  "message": "密码有 %d 位，太长了，检查一下是不是粘错了" % len(pwd)})
            return
        box = self._sms_box(page)
        if box is None:
            self._set(sms_result={"ok": False, "at": now_text(),
                                  "message": "抖音页面上现在没有密码框（可能验证已经过了，"
                                             "或者页面换了样）。状态没有变化"})
            return

        self._log_sms_probe(page)
        got = self._fill_sms(page, pwd)
        self._note_sms_box(got)
        if pwd not in got:
            why = self._sms_error or "页面上的框可能换样了"
            self._set(
                phase="sms",
                sms_result={"ok": False, "at": now_text(),
                            "message": "密码没能敲进抖音的框里（%s）。"
                                       "你可以直接点截图里那个输入框手动填，或把截图发我" % why},
            )
            return

        # 跟验证码一样：先定格抓一张，再等几秒让抖音认到，然后由主循环点「验证」
        self._snap_now(proof=True)
        with self.lock:
            self._sms_fill = {"code": pwd, "at": time.time(), "tries": 0}
        self._set(
            phase="sms",
            sms_result={"ok": True, "at": now_text(),
                        "message": "密码已经敲进抖音的框里了（%d 位）。"
                                   "等 %.0f 秒让抖音反应过来，然后我替你点「验证」。"
                                   % (len(pwd), SMS_BEFORE_SUBMIT_WAIT)},
        )

    def _active_value(self, page) -> str:
        """读回「当前有光标那个元素」里的内容 —— 用来核对到底粘进去没有。

        主页面和各个 iframe 都看一眼（抖音的弹窗有时候在 iframe 里）。
        """
        for frame in self._sms_frames(page):
            try:
                got = frame.evaluate(_JS_ACTIVE_VALUE)
            except Exception:
                continue
            if got:
                return str(got)
        return ""

    def _paste_text(self, page, body: str) -> str:
        """把内容一次性「粘」进抖音页面**当前光标处**，返回粘完之后那个位置的内容。

        用户的要求是「别管有没有能输入的框，直接粘贴」—— 所以这里**一条识别都不做**：
        不找框、不挑框、不套「还在登录弹窗里就不算」那层保护，光标在哪就粘到哪。
        没有光标就什么都不发生，由调用方如实告诉用户。

        两条路都试，两条都只发「粘贴」级别的动作（不一个个敲键，字符不会丢）：
          ① CDP 的 insert_text：只发一个 input 事件，React 的受控输入也认；
          ② 真·剪贴板 + Ctrl+V：有些页面只认 paste 事件。
             这条只在①没成功、且真的把原文写进剪贴板之后才按 Ctrl+V。
        """
        if not body:
            return ""
        self._sms_error = ""

        # ① insert_text（等价于粘贴：只发 input 事件，不敲键）
        try:
            page.keyboard.insert_text(body)
        except Exception as error:
            self._sms_error = str(error)[:160]
        got = self._active_value(page)
        if body in got:
            return got

        # ② 真剪贴板 + Ctrl+V。**只有真的把原文写进剪贴板了才按 Ctrl+V** ——
        #    否则按下去粘上来的是用户自己剪贴板里原有的东西，比粘不上更糟。
        #    这条兜底自己失败不往外报：真正的原因（多半是「页面上现在没有光标」）
        #    由调用方如实说清楚，再把 Playwright 的内部报错糊上去只会干扰判断。
        try:
            context = getattr(page, "context", None)
            if context is not None:
                try:
                    context.grant_permissions(["clipboard-read", "clipboard-write"])
                except Exception:
                    pass
            page.evaluate("(t) => navigator.clipboard.writeText(String(t))", body)
        except Exception:
            pass
        else:
            try:
                page.keyboard.press("Control+V")
            except Exception:
                pass
        return self._active_value(page)

    def _submit_text(self, page, text: str) -> None:
        """通用输入框：不做任何识别，直接把用户打的内容粘到抖音页面当前光标处。

        跟验证码 / 密码那两条通道的区别有两个：

          ① **不管页面上有没有输入框**（用户明确要求）：不找框、不挑框、不套登录弹窗保护，
             光标在哪就粘到哪；没有光标就如实说「没粘进去」，并告诉他怎么把光标放进去。
          ② **不排队点「验证」**：这里不写 self._sms_fill，主循环那份
             `fill and now - at >= SMS_BEFORE_SUBMIT_WAIT` 就不会被触发，
             「验证」完全由用户自己在画面里点。

        内容纪律：不写日志正文（_log_sms_probe 只记页面结构）、不落盘、不进状态，
        text_result 里只说「几个字」。
        """
        body = str(text or "").replace("\r", "").replace("\n", "")
        if not body:
            self._set(text_result={"ok": False, "at": now_text(),
                                   "message": "框里是空的，先打字再点「提交」"})
            return
        if len(body) > TEXT_MAX:
            self._set(text_result={"ok": False, "at": now_text(),
                                   "message": "内容有 %d 个字，太长了（上限 %d），"
                                              "检查一下是不是粘错了" % (len(body), TEXT_MAX)})
            return

        self._log_sms_probe(page)
        got = self._paste_text(page, body)
        if body not in got:
            why = ("（%s）" % self._sms_error) if self._sms_error else ""
            if got:
                tail = ("没能原样粘进去：抖音那个框里现在别的内容（%d 个字符）——"
                        "可能是框有长度限制，或者光标不在你想填的那个框里。"
                        "把光标点进那个框再试一次。" % len(got))
            else:
                tail = ("没能粘进去：抖音页面上现在没有光标。"
                        "先在画面里点一下那个输入框、把光标放进去，再点「提交」。")
            self._set(text_result={"ok": False, "at": now_text(), "message": tail + why})
            return

        # 只定格抓一张留证；**不**设 _sms_fill，所以不会替你点「验证」
        self._snap_now(proof=True)
        self._set(
            text_result={"ok": True, "at": now_text(),
                         "message": "已经粘进抖音页面了（%d 个字）。"
                                    "接下来请你自己在画面里点「验证」。" % len(body)},
        )

    def _click_sms_verify(self, page, digits: str, tries: int = 0) -> None:
        """等够了再点「验证」；点之前先看一眼码还在不在框里"""
        target = self._sms_target(page)
        got = self._sms_value(page, target[0] if target else None)
        self._note_sms_box(got)
        if digits and digits not in got:
            if not self._sms_box(page):
                # 框都没了：多半抖音已经过了这一步（或者用户自己走开了）。
                # 这时候报“验证码被清掉了”是冤枉用户，交给 _settle_sms 看结果。
                self._sms_submitted_at = time.time()
                self._set(phase="scanned",
                          sms_result={"ok": True, "at": now_text(),
                                      "message": "验证码框已经不见了（抖音好像自己往下走了），等结果…"})
                return
            self._set(phase="sms",
                      sms_result={"ok": False, "at": now_text(),
                                  "message": "验证码没留在抖音的框里（可能没敲进去，或者被清掉了）。"
                                             "请再填一次"})
            return
        target = self._sms_target(page)
        frame = target[0] if target else page
        clicked = None
        try:
            clicked = frame.evaluate(_JS_SMS_SUBMIT)
        except Exception:
            clicked = None
        if isinstance(clicked, dict) and clicked.get("off"):
            # 「验证」按钮还是灰的：抖音没认到验证码，再等一会儿试
            if tries < 2:
                with self.lock:
                    self._sms_fill = {"code": digits, "at": time.time(), "tries": tries + 1}
                self._set(phase="sms",
                          sms_result={"ok": True, "at": now_text(),
                                      "message": "抖音的「%s」按钮还是灰的，"
                                                 "再等 %.0f 秒我重新点一次。"
                                                 % (str(clicked.get("text") or "验证"), SMS_BEFORE_SUBMIT_WAIT)})
                return
            self._set(phase="sms",
                      sms_result={"ok": False, "at": now_text(),
                                  "message": "验证码填进去了，但抖音的「验证」按钮一直是灰的"
                                             "（它没认到验证码）。请点截图里那个框重填一次"})
            return
        how = ""
        if isinstance(clicked, dict) and clicked.get("ok"):
            how = self._press_verify(page, frame, clicked)
        if not how:
            # 页面上找不到「验证」按钮、或者按钮点不动：拿回车兜底
            if self._press_enter(frame):
                how = "回车"
        if not how:
            self._set(phase="sms",
                      sms_result={"ok": False, "at": now_text(),
                                  "message": "验证码敲好了，但没找到「验证」按钮，也没能按回车。"
                                             "请点截图里的「验证」"})
            return
        self._sms_submitted_at = time.time()
        self._set(
            phase="scanned",
            sms_result={"ok": True, "at": now_text(),
                        "message": "验证码填好了，也点了「%s」。等抖音的结果…" % how},
        )

    def _press_verify(self, page, frame, info) -> str:
        """点「验证」：优先用真鼠标点（跟人点的一模一样），点不到才退回页内脚本点。

        返回点的是哪个按钮（按钮上的字）；没点上给空字符串。
        """
        text = str(info.get("text") or "验证")
        if frame is page and info.get("hit"):
            try:
                page.mouse.click(float(info.get("x") or 0), float(info.get("y") or 0), delay=60)
                return text
            except Exception:
                pass
        try:
            frame.evaluate(_JS_SMS_CLICK_TEXT, text)
            return text
        except Exception:
            return ""

    @staticmethod
    def _press_enter(frame) -> bool:
        """回车兜底：焦点还在验证码框上，回车等于点「验证」。

        frame 可能是主页面（Page），也可能是弹窗所在的 iframe（Frame）：
        Page 没有 .page 属性，不能写死 frame.page，否则直接抛异常、兜底永远走不到。
        """
        try:
            host = getattr(frame, "page", None) or frame
            host.keyboard.press("Enter")
            return True
        except Exception:
            return False

    def _sms_value(self, page, frame=None) -> str:
        """读回框里的数字。给了 frame 就只读那一个（保证跟填的是同一个框）。"""
        frames = [frame] if frame is not None else self._sms_frames(page)
        for fr in frames:
            try:
                got = fr.evaluate(_JS_SMS_VALUE)
            except Exception:
                continue
            text = str(got or "")
            if text:
                return text
        return ""


    def _settle_sms(self, page, context) -> None:
        """提交完验证码等几秒再看结果：成功去登录，失败把抖音给的原因带回来。"""
        if not self._sms_submitted_at:
            return
        if time.time() - self._sms_submitted_at < SMS_SETTLE:
            return
        self._sms_submitted_at = 0.0
        if self._detect_login(context):
            return
        # 不能因为状态被轮询改回 "sms" 就不报错：
        # 输错验证码时页面会停在验证码步骤，正是需要把原因告诉用户的时候。
        notes = self._page_notes(page)
        bad = _pick_note(notes, SMS_BAD_WORDS)
        if bad:
            self._set(
                phase="sms",
                page_hint="抖音说：" + bad,
                sms_result={"ok": False, "at": now_text(),
                            "message": "验证没通过 —— 抖音说：%s" % bad},
            )
            return
        box = self._sms_box(page)
        if box:
            self._set(phase="sms")
            self._set(sms_result={"ok": False, "at": now_text(),
                                  "message": "验证码好像没被接受（页面上还停在验证码这一步）："
                                             "看看手机上的验证码是不是最新的，再试一次"})
            return
        self._set(sms_result={"ok": True, "at": now_text(),
                              "message": "验证码已提交，抖音还在确认；稍等几秒，"
                                         "如果一直没变化就重新点一次「开始授权」"})


# --------------------------------------------------------------------------
# 登录状态检测：用已保存的 Cookie 打开一次聊天页，确认还能不能发（不发消息）
# --------------------------------------------------------------------------
class LoginChecker:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.thread = None
        self.state = "idle"
        self.message = "尚未检测"
        self.unique_id = ""
        # 检测自己的画面单独存一份：以前推到共享的那份里，会把
        # "实时画面属于哪个抖音号"标记改掉，导致别人的截图接口看到你的画面。
        self.png = None
        self.last_frame_at = 0.0
        self.forced = False
        self.started_at = 0.0
        self.ok = None
        # ---- 批量检测（管理页「检测所有账号」）----
        # 一台机器同时只能跑一个检测：检测要开 Chromium，内存只够一个（_ENGINE_LOCK 也是这个意思）。
        # 所以「批量」不是并发，而是排一个队、拿一个后台线程挨个跑完。
        self.batch = []              # 还没轮到的抖音号
        self.batch_current = ""      # 正在检测的那个
        self.batch_total = 0
        self.batch_done = 0
        self.batch_ok = 0
        self.batch_bad = 0
        self.batch_skipped = []      # [{"unique_id": x, "reason": "..."}]
        self.batch_cancel = False
        self.batch_running = False
        self.batch_thread = None
        self.batch_started_at = 0.0
        self.batch_finished_at = 0.0

    def running(self) -> bool:
        # 批量检测期间，两个号之间的空档 self.thread 会是 None，
        # 但对外仍然要算「正在检测」——
        # 不然这空档里授权浏览器 / 发送任务会挤进来，跟批量检测抢内存。
        if self.batch_running:
            return True
        return bool(self.thread and self.thread.is_alive())

    def _set(self, **kwargs) -> None:
        with self.lock:
            for key, value in kwargs.items():
                setattr(self, key, value)

    def force_stop(self) -> None:
        """让正在跑的检测尽快收尾（配合 kill_chromium 使用）。"""
        self.forced = True
        with self.lock:
            # 批量检测要一起停：只置 forced 的话，本轮跑完队列还会接着跑下一个
            self.batch_cancel = True

    def stop(self) -> None:
        """用户自己点「停止检测」：只置标志，让检测好好收尾（不强杀浏览器）。"""
        self.forced = True
        with self.lock:
            if self.batch_running:
                # 批量：当前这个跑完就收工，不再开下一个
                self.batch_cancel = True
            if self.state == "running":
                self.message = "已请求停止检测，正在收尾…"

    def snapshot(self) -> dict:
        with self.lock:
            alive = bool(self.thread and self.thread.is_alive())
            age = int(time.time() - self.last_frame_at) if self.last_frame_at else None
            batch = {
                "running": bool(self.batch_running),
                "total": int(self.batch_total),
                "done": int(self.batch_done),
                "ok": int(self.batch_ok),
                "bad": int(self.batch_bad),
                "current": str(self.batch_current or ""),
                "left": len(self.batch),
                "skipped": list(self.batch_skipped),
                "cancel": bool(self.batch_cancel),
                "started_at": self.batch_started_at,
                "finished_at": self.batch_finished_at,
            }
            return {
                "state": self.state,
                "message": self.message,
                "unique_id": self.unique_id,
                # 批量检测的两个号之间也算「在跑」，否则界面会闪一下「空闲」，
                # 按钮跟着亮起来，用户正好点下去就会被服务端挡回来，莫名其妙。
                "running": bool(alive or self.batch_running),
                "ok": self.ok,
                "frame_age": age,
                "stuck": bool(alive and age is not None and age > STUCK_AFTER),
                "batch": batch,
            }

    def _blocked_reason(self) -> str:
        """现在能不能开一轮检测？不能就返回原因，能就返回空串。

        单次检测和批量检测共用这一套判断，免得两边规则不一样：
        单点检测被拦住了，批量却不拦，一路把内存耗干。
        """
        if browser.running():
            return "请先停止上面的授权浏览器，再检测（这台机器内存有限）"
        # 发送任务在跑也允许检测：检测本身很快，没必要等任务结束。
        # 但检测同样要开 Chromium，所以内存不够时还是拦住，免得把两边都拖卡。
        left = memory_available_mb()
        need = max(MIN_FREE_MB, SESSION_COST_MB + (TASK_RESERVE_MB if runner.running() else 0))
        if left is not None and left < need:
            who = "发送任务" if runner.running() else "别的授权"
            return "内存不够了（只剩 %d MB，检测要 %d MB），等%s结束再检测" % (left, need, who)
        return ""

    def _begin(self, unique_id: str, message: str = ""):
        """真正开一轮检测：落状态 + 起线程，返回那个线程（批量检测要靠它 join）。"""
        with self.lock:
            self.unique_id = unique_id
            self.state = "running"
            self.message = message or "正在用已保存的 Cookie 打开抖音聊天页…"
            self.png = None
            self.last_frame_at = 0.0
            self.forced = False
            self.ok = None
            self.started_at = time.time()
        thread = threading.Thread(target=self._run, args=(unique_id,), daemon=True)
        self.thread = thread
        thread.start()
        return thread

    def start(self, unique_id: str):
        unique_id = str(unique_id or "").strip()
        # 和授权浏览器共用一把引擎锁：同时点只会有一个真的跑起来
        with _ENGINE_LOCK:
            if self.running():
                if self.batch_running:
                    return False, "正在批量检测中，等它跑完（或点「停止检测」提前收工）"
                return False, "正在检测中，请稍候"
            if not unique_id:
                return False, "请先填写并保存「抖音号」"
            blocked = self._blocked_reason()
            if blocked:
                return False, blocked
            self._begin(unique_id)
            return True, "开始检测，请稍候（大约 20-60 秒）"

    # ------------------------------------------------------------------
    # 一键检测所有账号
    # ------------------------------------------------------------------
    BATCH_GAP_SECONDS = 2.0      # 两个号中间喘一口：等上一个浏览器彻底退掉再开下一个
    BATCH_WAIT_BROWSER = 120.0   # 开跑前授权浏览器还占着，最多等它这么久

    def start_all(self, unique_ids):
        """把所有账号排进队列，后台逐个检测。不会给任何人发消息。"""
        uids = []
        for item in (unique_ids or []):
            uid = str(item or "").strip()
            if uid and uid not in uids:
                uids.append(uid)
        with _ENGINE_LOCK:
            if self.running():
                if self.batch_running:
                    return False, "批量检测已经在跑了，等它结束（或点「停止检测」提前收工）"
                return False, "正在检测中，等它跑完再点「检测所有账号」"
            if not uids:
                return False, "还没有抖音号可以检测"
            with self.lock:
                self.batch = list(uids)
                self.batch_current = ""
                self.batch_total = len(uids)
                self.batch_done = 0
                self.batch_ok = 0
                self.batch_bad = 0
                self.batch_skipped = []
                self.batch_cancel = False
                self.batch_running = True
                self.batch_started_at = time.time()
                self.batch_finished_at = 0.0
                self.state = "running"
                self.message = "准备逐个检测 %d 个抖音号…" % len(uids)
                self.ok = None
            self.batch_thread = threading.Thread(target=self._batch_run, daemon=True)
            self.batch_thread.start()
            return True, "开始逐个检测 %d 个抖音号（每个约 20-60 秒，总共可能要几分钟）" % len(uids)

    def _batch_canceled(self) -> bool:
        with self.lock:
            return bool(self.batch_cancel)

    def _batch_run(self) -> None:
        # note 用来解释「为什么一个都没检测」；正常跑完是空串
        note = ""
        try:
            # 开跑前先等授权浏览器让位：批量检测要开十几次浏览器，
            # 跟一个正在跑的授权会话抢内存，两边都会很慢。
            waited = 0.0
            while browser.running() and waited < self.BATCH_WAIT_BROWSER and not self._batch_canceled():
                with self.lock:
                    self.message = "等授权浏览器结束再开始批量检测…（已等 %d 秒）" % int(waited)
                time.sleep(2.0)
                waited += 2.0
            if browser.running():
                with self.lock:
                    pending = [x for x in ([self.batch_current] + list(self.batch)) if x]
                    self.batch_skipped = self.batch_skipped + [
                        {"unique_id": x, "reason": "授权浏览器一直开着"} for x in pending
                    ]
                    self.batch = []
                    self.batch_current = ""
                note = "授权浏览器一直开着，一个都没检测"

            while not note:
                with self.lock:
                    if self.batch_cancel:
                        # 取消：队列里剩下的明说「没检测」，别让人以为它们也是正常的
                        if self.batch:
                            self.batch_skipped = self.batch_skipped + [
                                {"unique_id": x, "reason": "已取消"} for x in list(self.batch)
                            ]
                            self.batch = []
                        break
                    if not self.batch:
                        break
                    uid = self.batch.pop(0)
                    self.batch_current = uid
                # 上一个检测的浏览器可能还没退干净，先等一下再开下一个
                time.sleep(self.BATCH_GAP_SECONDS)
                if self._batch_canceled():
                    with self.lock:
                        self.batch_skipped = self.batch_skipped + [
                            {"unique_id": x, "reason": "已取消"} for x in ([uid] + list(self.batch))
                        ]
                        self.batch = []
                        self.batch_current = ""
                    break
                # 这个空档里浏览器理论上开不起来（BrowserSession.start 也看 checker.running），
                # 但真出现了就跳过这一号，别跟它抢内存
                if browser.running():
                    with self.lock:
                        self.batch_skipped.append({"unique_id": uid, "reason": "授权浏览器正在跑"})
                        self.batch_done += 1
                        self.batch_current = ""
                    continue
                blocked = self._blocked_reason()
                if blocked:
                    with self.lock:
                        self.batch_skipped.append({"unique_id": uid, "reason": blocked})
                        self.batch_done += 1
                        self.batch_current = ""
                    continue
                try:
                    thread = self._begin(uid, "批量检测：正在检测 %s…" % uid)
                except Exception as error:
                    with self.lock:
                        self.batch_skipped.append(
                            {"unique_id": uid, "reason": "开不起来：" + str(error)}
                        )
                        self.batch_done += 1
                        self.batch_current = ""
                    continue
                # 等这一轮真的跑完再开下一个：同一时刻只允许一个 Chromium
                try:
                    thread.join(max(30.0, CHECK_TIMEOUT + 60))
                except Exception:
                    pass
                with self.lock:
                    self.batch_done += 1
                    if self.ok is True:
                        self.batch_ok += 1
                    elif self.ok is False:
                        self.batch_bad += 1
                    self.batch_current = ""
        except Exception as error:  # 兜底：批量线程绝不能把面板带崩
            note = "批量检测中断：" + str(error)
        finally:
            with self.lock:
                summary = {
                    "at": now_text(),
                    "total": int(self.batch_total),
                    "done": int(self.batch_done),
                    "ok": int(self.batch_ok),
                    "bad": int(self.batch_bad),
                    "skipped": list(self.batch_skipped),
                    "stopped": bool(self.batch_cancel),
                    "note": note,
                }
                self.batch_running = False
                self.batch_finished_at = time.time()
                self.batch_current = ""
                self.batch = []
                self.state = "done"
                if note:
                    self.message = "批量检测没跑成：%s" % note
                elif self.batch_cancel:
                    self.message = ("批量检测已停止：共 %d 个，已检测 %d 个（正常 %d，异常 %d），跳过 %d 个"
                                    % (summary["total"], summary["done"], summary["ok"],
                                       summary["bad"], len(summary["skipped"])))
                else:
                    self.message = ("批量检测完成：共 %d 个 · 正常 %d 个 · 异常 %d 个 · 跳过 %d 个"
                                    % (summary["total"], summary["ok"], summary["bad"],
                                       len(summary["skipped"])))
            # 落一份「上次批量检测是什么时候、结果如何」，面板重启后界面上还看得到
            try:
                save_state({"check_all": summary})
            except Exception:
                pass

    def _probe(self, page) -> dict:
        try:
            info = page.evaluate(PAGE_STATE_JS)
            return info if isinstance(info, dict) else {}
        except Exception:
            return {}

    def _finish(self, unique_id: str, ok: bool, message: str, extra: dict = None, keep_old: bool = False) -> None:
        record = {"ok": bool(ok), "message": message, "at": now_text()}
        if extra:
            record.update(extra)
        if not keep_old:
            save_state({"checks": {unique_id: record}})
        with self.lock:
            self.state = "done"
            self.ok = bool(ok)
            self.message = message

    def _targets_of(self, unique_id: str) -> list:
        for task in load_tasks():
            if str(task.get("unique_id") or "") == unique_id:
                return [str(item) for item in (task.get("targets") or [])]
        return []

    def _run(self, unique_id: str) -> None:
        playwright = instance = context = page = None
        watchdog = None
        stop_watchdog = threading.Event()
        deadline = time.time() + CHECK_TIMEOUT
        try:
            cookies = load_cookies(unique_id)
            if not cookies:
                self._finish(unique_id, False, "还没有保存 Cookie，请先授权登录", {"reason": "no_cookie"})
                return
            has_session = any(
                c.get("name") in ("sessionid", "sessionid_ss") and c.get("value") for c in cookies
            )
            from playwright.sync_api import sync_playwright

            playwright = sync_playwright().start()
            instance = playwright.chromium.launch(
                headless=True,
                args=[
                    "--disable-dev-shm-usage",
                    "--no-sandbox",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            context = instance.new_context(viewport=VIEWPORT, locale="zh-CN", user_agent=USER_AGENT)
            context.add_cookies(cookies)
            page = context.new_page()
            cdp = context.new_cdp_session(page)

            def keep_frame():
                # 检测期间把画面存进自己这一份，实时画面窗显示的就是它
                if self.forced:
                    return
                try:
                    result = cdp.send("Page.captureScreenshot", {"format": "png"})
                    png = base64.b64decode(result["data"])
                except Exception:
                    return
                if png:
                    with self.lock:
                        self.png = png
                        self.last_frame_at = time.time()

            # 看门狗：整轮检测超过上限就强杀浏览器，保证不会一直卡在加载或截图上
            def watch():
                while not stop_watchdog.wait(1.0):
                    if self.forced:
                        return
                    if time.time() > deadline:
                        self.forced = True
                        self._set(message="检测超过 %d 秒还没有结果，已自动结束（多半是页面加载太慢）" % CHECK_TIMEOUT)
                        kill_chromium()
                        return

            watchdog = threading.Thread(target=watch, daemon=True)
            watchdog.start()

            # 不等整页加载完（抖音首页很重），页面一开始导航就先把画面推出来
            try:
                page.goto(CHAT_URL, wait_until="commit", timeout=60000)
            except Exception:
                pass
            keep_frame()

            targets = self._targets_of(unique_id)
            titles = []
            info = {}
            conversations = 0
            while time.time() < deadline:
                if self.forced:
                    self._finish(unique_id, False, "检测已中断（超时或被强制停止）", {"reason": "aborted"}, keep_old=True)
                    return
                page.wait_for_timeout(1000)
                keep_frame()
                info = self._probe(page)
                conversations = int(info.get("conversations") or 0)
                titles.extend([t for t in (info.get("titles") or []) if t and t not in titles])
                if conversations:
                    break
                with self.lock:
                    self.message = "正在等待聊天页加载（如果弹了登录框，说明 Cookie 已失效）…"

            if conversations:
                # 往下翻几屏，尽量把好友列表看全
                for _ in range(6):
                    if self.forced or (targets and all(name in titles for name in targets)):
                        break
                    try:
                        page.evaluate(SCROLL_JS, ".conversationConversationListwrapper")
                    except Exception:
                        pass
                    page.wait_for_timeout(1500)
                    keep_frame()
                    info = self._probe(page)
                    titles.extend([t for t in (info.get("titles") or []) if t and t not in titles])

            if self.forced:
                self._finish(unique_id, False, "检测已中断（超时或被强制停止）", {"reason": "aborted"}, keep_old=True)
                return

            if not conversations:
                reason = "页面被登录弹窗挡住了" if info.get("loginDialog") else "页面里没有出现好友列表"
                hint = "，Cookie 可能已过期，请重新授权登录" if has_session else "，请点「开始授权」用手机号登录"
                self._finish(
                    unique_id,
                    False,
                    "未登录：" + reason + hint,
                    {"conversations": 0, "has_session": has_session, "titles": titles[:40]},
                )
                return

            matched = [name for name in targets if any(name == t or name in t for t in titles)]
            missing = [name for name in targets if name not in matched]
            message = "已登录，好友列表正常（看到 %d 个会话）" % conversations
            if targets:
                message += "；目标好友找到 %d/%d" % (len(matched), len(targets))
            self._finish(
                unique_id,
                True,
                message,
                {
                    "conversations": conversations,
                    "titles": titles[:40],
                    "matched": matched,
                    "missing": missing,
                    "has_session": has_session,
                },
            )
        except Exception as error:
            if self.forced:
                self._finish(unique_id, False, "检测已被强制停止（浏览器已结束，保留上一次的检测结果）", {"reason": "aborted"}, keep_old=True)
            else:
                self._finish(unique_id, False, "检测失败：" + str(error), {"error": str(error)})
        finally:
            stop_watchdog.set()
            for resource in (context, instance):
                if resource is not None:
                    try:
                        resource.close()
                    except Exception:
                        pass
            if playwright is not None:
                try:
                    playwright.stop()
                except Exception:
                    pass
            with self.lock:
                self.thread = None


# --------------------------------------------------------------------------
# 手动执行一次
# --------------------------------------------------------------------------
class TaskRunner:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.process = None
        self._task_env_path = ""
        self.started_at = None
        self.handle = None
        self._lock_handle = None
        self.only = ""   # 这次只跑哪个抖音号（空 = 全部）

    def _take_shared_lock(self) -> bool:
        """抢共享发送锁，避免多个进程同时运行并向同一账号重复发送。
        """
        if fcntl is None:
            return True
        self._release_shared_lock()
        handle = None
        try:
            SEND_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
            handle = os.open(SEND_LOCK_PATH, os.O_WRONLY | os.O_CREAT, 0o600)
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except Exception:
            if handle is not None:
                try:
                    os.close(handle)
                except Exception:
                    pass
            return False
        self._lock_handle = handle
        return True

    def _release_shared_lock(self) -> None:
        handle = self._lock_handle
        self._lock_handle = None
        if handle is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(handle, fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            os.close(handle)
        except Exception:
            pass

    def _reap(self) -> None:
        """发送任务退出后释放共享锁，供下一次手动运行使用。"""
        process = self.process
        if process is not None and process.poll() is not None:
            self._release_shared_lock()
            self._cleanup_task_env_file()

    def _cleanup_task_env_file(self) -> None:
        path = self._task_env_path
        self._task_env_path = ""
        if path:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
            except Exception:
                pass

    def start(self, only: str = "", source: str = "手动"):
        """only 传抖音号时，只跑这一个账号（账号级登录的用户只能跑自己的）。"""
        # 同样共用引擎锁：不会出现"检测刚起来，发送任务也起来了"
        with _ENGINE_LOCK:
            if browser.running():
                return False, "授权浏览器还开着，请先点「停止」再运行，避免内存不够"
            if checker.running():
                return False, "正在检测登录状态，请稍候"
            if self.process is not None and self.process.poll() is None:
                return False, "已经有一个任务在运行了"
            if not self._take_shared_lock():
                return False, "已有发送任务正在运行，等它结束后再试，避免同一个账号重复发送"
            environment = os.environ.copy()
            for key in tuple(environment):
                if key.upper() == "TASKS" or key.upper().startswith("COOKIES_"):
                    environment.pop(key, None)
            # Linux 对单个 execve 环境项有 128 KiB 上限。较大的抖音 Cookie
            # 会让 Popen 直接报 E2BIG，worker 连启动都做不到。把 TASKS / COOKIES
            # 单独通过匿名文件描述符传给 worker，普通配置仍留在环境变量里。
            task_values = {}
            for key, value in parse_env().items():
                if value is None:
                    continue
                if key.upper() == "TASKS" or key.upper().startswith("COOKIES_"):
                    task_values[key] = value
                else:
                    environment[key] = value
            # 账号数据现在存在 accounts.json；它覆盖同名旧格式变量。
            task_values.update(accounts_env())
            task_env_bytes = json.dumps(
                task_values, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
            task_env_fd = None
            task_env_path = ""
            task_env_pass_fds = ()
            if hasattr(os, "memfd_create"):
                task_env_fd = os.memfd_create("douyinsparkflow-task-env")
                try:
                    view = memoryview(task_env_bytes)
                    while view:
                        written = os.write(task_env_fd, view)
                        if written <= 0:
                            raise OSError("写入发送配置失败")
                        view = view[written:]
                    os.lseek(task_env_fd, 0, os.SEEK_SET)
                except Exception:
                    os.close(task_env_fd)
                    raise
                environment["TASK_ENV_FD"] = str(task_env_fd)
                task_env_pass_fds = (task_env_fd,)
            else:
                LOG_DIR.mkdir(parents=True, exist_ok=True)
                task_env_fd, task_env_path = tempfile.mkstemp(
                    prefix=".send-task-env-", suffix=".json", dir=str(LOG_DIR)
                )
                try:
                    os.fchmod(task_env_fd, 0o600)
                    with os.fdopen(task_env_fd, "wb") as handle:
                        handle.write(task_env_bytes)
                    task_env_fd = None
                except Exception:
                    if task_env_fd is not None:
                        os.close(task_env_fd)
                    try:
                        os.unlink(task_env_path)
                    except Exception:
                        pass
                    raise
                environment["TASK_ENV_FILE"] = task_env_path
            if only:
                environment["RUN_ONLY_ACCOUNTS"] = str(only)
            else:
                environment.pop("RUN_ONLY_ACCOUNTS", None)
            self.only = str(only or "")
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            self.handle = os.fdopen(
                os.open(RUN_LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "a", encoding="utf-8"
            )
            self.handle.write(
                "\n===== %s %s运行（%s）=====\n" % (now_text(), source, only or "全部账号")
            )
            self.handle.flush()
            try:
                self.process = subprocess.Popen(
                    [sys.executable, "main.py"],
                    cwd=str(BASE_DIR),
                    env=environment,
                    stdout=self.handle,
                    stderr=subprocess.STDOUT,
                    text=True,
                    **({"pass_fds": task_env_pass_fds} if task_env_pass_fds else {}),
                )
            except Exception:
                self._release_shared_lock()
                if task_env_path:
                    try:
                        os.unlink(task_env_path)
                    except Exception:
                        pass
                raise
            finally:
                if task_env_fd is not None:
                    try:
                        os.close(task_env_fd)
                    except Exception:
                        pass
            self._task_env_path = task_env_path
            self.started_at = time.time()
            return True, "任务已启动"

    def snapshot(self) -> dict:
        with self.lock:
            self._reap()
            running = self.process is not None and self.process.poll() is None
            stale = None
            if running:
                try:
                    stale = time.time() - RUN_LOG.stat().st_mtime
                except Exception:
                    stale = None
            return {
                "running": running,
                "returncode": None if (running or self.process is None) else self.process.returncode,
                "started_at": self.started_at,
                "stale": stale,
                "stuck": bool(running and stale is not None and stale > RUN_STUCK_AFTER),
                "only": self.only,
            }

    def kill(self) -> bool:
        """强杀面板里正在跑的发送任务（连同它的浏览器进程）。"""
        with self.lock:
            self._release_shared_lock()
            process = self.process
            if process is None or process.poll() is not None:
                return False
            try:
                process.kill()
            except Exception:
                pass
            self.started_at = None
            return True

    def running(self) -> bool:
        with self.lock:
            self._reap()
            return self.process is not None and self.process.poll() is None


def tail(path: Path, limit: int = 15000) -> str:
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-limit:]
    except Exception as error:
        return "读取日志失败：" + str(error)


# 好友列表收集用的选择器：刻意与 core/tasks.py 里发送时用的那套保持一致，
# 这样"这里列出来的名字"就是"发送时列表里找得到的名字"，两边不会打架。
_FRIEND_CHAT_URL = "https://www.douyin.com/chat"
_FRIEND_ITEM_SELECTOR = ".conversationConversationItemwrapper"
_FRIEND_TITLE_SELECTOR = ".conversationConversationItemtitle"
_FRIEND_LIST_SELECTOR = ".conversationConversationListwrapper"
# 抖音的会话列表是**虚拟列表**（同时只有十来个节点），而且刚渲染那一瞬间，
# 个人会话的标题位置放的是数字 uid，几秒后才换成昵称。
# 数字标题是临时占位，绝不能当成好友名收进去 —— 否则列表里会混进
# 3933396975227929 这类长数字，而且一旦记进 seen，同一行的真名就再也收不到。
_FRIEND_PLACEHOLDER_TITLE = re.compile(r"^\d{5,}$")
# 一次拉取的总时限。抖音偶尔会让导航/列表卡很久（实测出现过卡在 goto 好几分钟），
# 没有总时限的话这个后台线程会一直占着"正在拉取"，后面谁都拉不了、界面一直转圈。
_FRIEND_SCAN_MAX_SECONDS = 300


class FriendScanner:
    """把某个抖音号的好友列表拉下来（后台线程跑，前端轮询进度）。

    只读：打开这个号自己的聊天页，把左边会话列表翻到底，收集标题。
    不发送任何消息，也不改动账号配置 —— 勾选结果由前端写进「目标好友」后
    走原有的保存配置流程落地。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.unique_id = ""
        self.thread = None
        self.error = ""
        self.friends = []
        self.targets = []
        self.progress = ""
        self.started_at = 0.0
        self.finished_at = 0.0

    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def snapshot(self) -> dict:
        return {
            "unique_id": self.unique_id,
            "running": self.running(),
            "error": self.error,
            "friends": list(self.friends),
            "targets": list(self.targets),
            "progress": self.progress,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }

    def start(self, unique_id: str) -> tuple:
        uid = str(unique_id or "").strip()
        if not uid:
            return False, "先填好抖音号再来拉好友"
        if self.running():
            if self.unique_id == uid:
                return True, "正在拉取，稍等"
            # 浏览器卡住时线程会一直挂在同步调用里，位子不能让永远占着 ——
            # 超过总时限还有余量就作废旧任务，放行新的一次。
            if time.time() - float(self.started_at or 0) > _FRIEND_SCAN_MAX_SECONDS + 120:
                log_force("好友列表", "上一次拉取（%s）超时未结束，作废后重新开始" % self.unique_id)
                self.error = "上一次拉取超时了，已作废"
                self.finished_at = time.time()
                self.thread = None
            else:
                return False, "已经有一个号在拉好友了，等它跑完再来"
        busy = False
        try:
            busy = bool(runner.running())
        except Exception:
            busy = False
        if busy:
            # 2G 内存的小机器上两个 Chromium 会互相拖，别跟发送任务抢
            return False, "发送任务正在跑，等它结束再拉好友"
        storage = load_storage_state(uid)
        if not storage:
            return False, "这个号还没保存过可用的登录凭据，先扫码登录成功再拉"
        with self._lock:
            self.unique_id = uid
            self.error = ""
            self.friends = []
            self.targets = []
            self.progress = "正在打开抖音…"
            self.started_at = time.time()
            self.finished_at = 0.0
        self.thread = threading.Thread(target=self._work, args=(uid, storage), daemon=True)
        self.thread.start()
        return True, "已开始拉取好友列表"

    def _work(self, uid: str, storage: dict) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except Exception as error:
            self.error = "拉取组件不可用：%s" % error
            self.finished_at = time.time()
            return
        pw = None
        browser = None
        context = None
        try:
            pw = sync_playwright().start()
            # 必须和发送/登录检测用同一套启动参数：容器里跑的是 root，
            # 不带 --no-sandbox 时 Chromium 会直接拒绝启动（"Running as root
            # without --no-sandbox is not supported"），功能当场失效。
            browser = pw.chromium.launch(
                headless=True,
                args=[
                    "--disable-dev-shm-usage",
                    "--no-sandbox",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            context = browser.new_context(storage_state=storage)
            context.set_default_navigation_timeout(120000)
            context.set_default_timeout(120000)
            page = context.new_page()
            # goto 本身最多要等 120 秒，这段时间界面上只显示"正在打开抖音…"
            # 太含糊，这里把话说清楚。
            self.progress = "正在打开抖音聊天页…"
            page.goto(_FRIEND_CHAT_URL, wait_until="domcontentloaded")

            deadline = min(time.time() + 120, float(self.started_at or time.time()) + _FRIEND_SCAN_MAX_SECONDS)
            ready = False
            while time.time() < deadline:
                try:
                    if page.locator(_FRIEND_ITEM_SELECTOR).count() > 0:
                        ready = True
                        break
                except Exception:
                    pass
                self.progress = "正在等待聊天页加载…"
                time.sleep(1.0)
            if not ready:
                self.error = "聊天页 2 分钟内没渲染出会话列表，确认这个号还能正常登录"
                return

            names = self._collect(page, uid, deadline=float(self.started_at or time.time()) + _FRIEND_SCAN_MAX_SECONDS)
            self.friends = names
            account = load_accounts().get(uid) or {}
            self.targets = [str(x) for x in (account.get("targets") or [])]
            if names:
                save_account(uid, friend_list=list(names), friend_list_at=now_text())
            self.progress = ""
        except Exception as error:
            self.error = "拉取好友列表失败：%s" % error
            try:
                log_force(
                    "好友列表",
                    "账号 %s 拉取失败：%s\n%s" % (uid, error, traceback.format_exc()),
                )
            except Exception:
                pass
        finally:
            self.finished_at = time.time()
            for closer in (context, browser):
                try:
                    if closer is not None:
                        closer.close()
                except Exception:
                    pass
            try:
                if pw is not None:
                    pw.stop()
            except Exception:
                pass

    def _collect(self, page, uid: str, deadline: float = 0.0) -> list:
        names = []
        seen = set()

        def out_of_time():
            """超过总时限就收工，别让界面无限转圈。"""
            return bool(deadline) and time.time() > deadline

        def titles_now():
            """当前可见项的标题。抖音的会话列表是虚拟列表，同时只有十来个节点。"""
            out = []
            try:
                rows = page.locator(_FRIEND_ITEM_SELECTOR).all()
            except Exception:
                return out
            for row in rows:
                try:
                    title = row.locator(_FRIEND_TITLE_SELECTOR).inner_text().strip()
                except Exception:
                    continue
                if title:
                    out.append(title)
            return out

        def unfinished(titles):
            """这一屏里还有没有"数字占位" —— 有就说明名字还没渲染完。"""
            return any(_FRIEND_PLACEHOLDER_TITLE.match(t) for t in titles)

        def settle_and_harvest(rounds=3, pause=0.6):
            """等这一屏把昵称渲染出来再收；等不到也只收已经渲染好的部分。

            数字占位一律跳过：它是抖音给个人会话用的临时 uid。
            实测（ddy）：直接开滚会把 uid 当成好友名收进来，结果 49 条里混着
            十几条 3933396975227929 这样的数字；先等名字渲染好再滚，
            拿到的是干净的昵称、0 个数字。
            """
            titles = titles_now()
            for _ in range(rounds):
                if titles and not unfinished(titles):
                    break
                time.sleep(pause)
                titles = titles_now()
            for title in titles:
                if _FRIEND_PLACEHOLDER_TITLE.match(title) or title in seen:
                    continue
                seen.add(title)
                names.append(title)

        def metrics():
            """列表容器的 [scrollTop, scrollHeight, clientHeight]；取不到返回 None。"""
            try:
                return page.evaluate(
                    """(sel) => {
                        const box = document.querySelector(sel);
                        if (!box) return null;
                        return [box.scrollTop, box.scrollHeight, box.clientHeight];
                    }""",
                    _FRIEND_LIST_SELECTOR,
                )
            except Exception:
                return None

        def scroll_step():
            """往下滚一屏，返回"这次是不是真的滚动了"。"""
            try:
                return bool(
                    page.evaluate(
                        """(sel) => {
                            const box = document.querySelector(sel);
                            if (!box) return false;
                            const before = box.scrollTop;
                            box.scrollTop = before + Math.max(200, box.clientHeight * 0.85);
                            return box.scrollTop > before;
                        }""",
                        _FRIEND_LIST_SELECTOR,
                    )
                )
            except Exception:
                return False

        # 先等第一屏把昵称渲染出来再开始滚，否则滚太快会把整屏 uid 收走
        first_deadline = time.time() + 20
        while time.time() < first_deadline and not out_of_time():
            titles = titles_now()
            if titles and not unfinished(titles):
                break
            self.progress = "正在等会话列表显示名字…"
            time.sleep(0.6)

        settle_and_harvest()
        last = len(names)
        stable = 0
        for _ in range(300):
            if out_of_time():
                self.error = "拉取超时（抖音响应太慢），下面是已经拿到的 %d 个，可稍后重试" % len(names)
                break
            moved = scroll_step()
            time.sleep(0.9)
            settle_and_harvest()
            self.progress = "已找到 %d 个好友…" % len(names)
            if len(names) == last:
                stable += 1
            else:
                stable = 0
                last = len(names)
            if stable < 3 or moved:
                continue
            # 看起来到底了。抖音是"滚到接近底部才继续加载/追加"，实测同一个号
            # 有时拿到 45 个、有时 57 个 —— 少的那次就是在追加内容之前收工了。
            # 所以这里不立刻结束，而是再盯一段时间：只要"名字变多"或者
            # "列表整体变长"就继续滚，两边都没动静才算真的到底。
            grew = False
            for _ in range(15):
                if out_of_time():
                    break
                time.sleep(1.0)
                before_len = len(names)
                before_metrics = metrics()
                settle_and_harvest()
                after_metrics = metrics()
                longer = bool(
                    before_metrics
                    and after_metrics
                    and len(after_metrics) == 3
                    and len(before_metrics) == 3
                    and after_metrics[1] > before_metrics[1]
                )
                if len(names) > before_len or longer:
                    grew = True
                    break
            if not grew:
                break
            stable = 0
            last = len(names)
        return names
class BrowserPool:
    """授权浏览器会话池：最多同时开 MAX_AUTH_SESSIONS 个，每个号一个独立会话。

    以前全站只有一个浏览器引擎，两个人不能同时扫码。现在按"槽位"分：
      * 每个会话仍然是原来那套 BrowserSession（自己的二维码 / 画面 / 线程 / 归属）；
      * 谁只能看/操作自己名下的那个会话（管理员看全部）；
      * 发送任务 / 登录检测可以并存，但开新会话前会按 SESSION_COST_MB + TASK_RESERVE_MB
        算一次内存账（见 start）：不够就明确告诉用户还差多少，而不是把整机拖进 swap。
    """

    def __init__(self, size: int = 2) -> None:
        self.size = max(1, int(size))
        self.lock = threading.RLock()  # 兼容老代码里的 with browser.lock
        self.sessions = [BrowserSession() for _ in range(self.size)]
        # 一个永远不启动的"空会话"，用来给"你这会儿没有在跑的东西"提供一份干净的状态
        self.spare = BrowserSession()

    # ---------------- 查询 ----------------
    def all(self) -> list:
        with self.lock:
            return list(self.sessions)

    def running(self) -> bool:
        return any(s.running() for s in self.all())

    def threads(self) -> list:
        return [s.thread for s in self.all() if getattr(s, "thread", None) is not None]

    def find_owner(self, unique_id: str):
        uid = str(unique_id or "")
        if not uid:
            return None
        for s in self.all():
            if s.running() and str(getattr(s, "owner", "") or "") == uid:
                return s
        return None

    def pick_for(self, scopes, want: str = ""):
        """这次请求该用哪个会话。scopes=None 表示管理员（都能看）。

        1) 指定了抖音号：只认那个号，而且必须是我名下的；
        2) 没指定：优先正在跑的、我名下的；
        3) 都没有：给最后一张我名下的画面（让人知道刚才是什么状态）；
        4) 还是没有：None。
        """
        want = str(want or "").strip()

        def mine(owner: str) -> bool:
            return scopes is None or (bool(owner) and owner in scopes)

        with self.lock:
            if want:
                for s in self.sessions:
                    if str(getattr(s, "owner", "") or "") == want or str(getattr(s, "unique_id", "") or "") == want:
                        return s if mine(want) else None
                return None
            for s in self.sessions:
                if s.running() and mine(str(getattr(s, "owner", "") or "")):
                    return s
            for s in self.sessions:
                if s.image() is not None and mine(str(getattr(s, "owner", "") or "")):
                    return s
            return None

    def snapshot_for(self, scopes, want: str = "") -> dict:
        """给某个请求看的授权状态。不是自己的就回一份干净的空状态，别泄露别人的二维码。"""
        s = self.pick_for(scopes, want)
        return (s or self.spare).snapshot()

    def list_for(self, scopes) -> list:
        """正在跑的会话列表（管理台用来展示"现在有几个人在授权"）。

        普通用户只看得到自己名下的，管理员看全部。
        """
        out = []
        for s in self.all():
            owner = str(getattr(s, "owner", "") or "")
            if scopes is not None and owner not in scopes:
                continue
            if not s.running() and not owner:
                continue
            snap = s.snapshot()
            out.append(
                {
                    "owner": owner,
                    "username": str(getattr(s, "username", "") or ""),
                    "running": bool(snap.get("running")),
                    "phase": snap.get("phase"),
                    "message": snap.get("message"),
                    "idle_left": snap.get("idle_left"),
                    "has_image": bool(snap.get("has_image")),
                    "mine": scopes is None or (bool(owner) and owner in scopes),
                }
            )
        return out

    def used(self) -> int:
        """现在有几个授权会话在跑。"""
        return sum(1 for s in self.all() if s.running())

    def wait_seconds(self):
        """最快多久会空出一个槽位。

        每个会话有两个上限：3 分钟没操作自动关、20 分钟绝对上限。
        取所有在跑会话里最小的那个，就是"运气最好"的等待时间。
        返回 None 表示现在没有会话在跑（不用等）。
        """
        waits = []
        now = time.time()
        for s in self.all():
            if not s.running():
                continue
            left = s.idle_left()
            if left is None:
                left = AUTH_IDLE_STOP
            started = float(getattr(s, "started_at", 0) or 0)
            if started:
                left = min(left, max(0.0, AUTO_AUTH_SECONDS - (now - started)))
            waits.append(max(0, int(left)))
        if not waits:
            return None
        return min(waits)

    # ---------------- 兼容老写法（不区分会话时用"第一个在跑的"）----------------
    def _first(self):
        for s in self.all():
            if s.running():
                return s
        return self.sessions[0] if self.sessions else self.spare

    @property
    def owner(self) -> str:
        return str(getattr(self._first(), "owner", "") or "")

    @property
    def unique_id(self) -> str:
        return str(getattr(self._first(), "unique_id", "") or "")

    @property
    def username(self) -> str:
        return str(getattr(self._first(), "username", "") or "")

    def image(self):
        return self._first().image()

    def snapshot(self) -> dict:
        return self._first().snapshot()

    def qr_image(self, unique_id: str = "", scopes=None):
        s = self.pick_for(scopes, unique_id)
        if s is None:
            return None
        return s.qr_image()

    def sms_proof_image(self, unique_id: str = "", scopes=None):
        s = self.pick_for(scopes, unique_id)
        if s is None:
            return None
        return s.sms_proof_image()

    def _set(self, **kwargs) -> None:
        for s in self.all():
            try:
                s._set(**kwargs)
            except Exception:
                pass

    def force_stop(self) -> None:
        for s in self.all():
            try:
                s.force_stop()
            except Exception:
                pass

    def stop(self, unique_id: str = "", scopes=None) -> None:
        s = self.pick_for(scopes, unique_id)
        if s is None and scopes is None:
            s = self._first()
        if s is not None:
            s.stop()

    def send(self, name: str, unique_id: str = "", scopes=None, **payload) -> None:
        s = self.pick_for(scopes, unique_id)
        if s is None and scopes is None:
            s = self._first()
        if s is not None:
            s.send(name, **payload)

    # ---------------- 开一个会话 ----------------
    def start(self, unique_id: str, username: str):
        uid = str(unique_id or "").strip()
        with _ENGINE_LOCK:
            if checker.running():
                return False, "正在检测登录状态，请稍候再试"
            # [本地增强] 这里原来挡着 runner.running()（发送任务在跑就不给开授权）。
            # 发送任务动辄十几分钟，挡着会让"扫码加个号"干等；现在放开，
            # 靠下面的可用内存检查保证不会跟发送任务抢爆内存。
            with self.lock:
                if uid:
                    same = self.find_owner(uid)
                    if same is not None:
                        return False, "「%s」的授权浏览器已经在运行了" % uid
                free = [s for s in self.sessions if not s.running()]
                if not free:
                    wait = self.wait_seconds()
                    hint = ("最快约 %s后空出" % fmt_wait(wait)) if wait is not None else "等其中一个结束"
                    return False, "现在有 %d 个授权在同时进行，%s（对方点「停止」会立刻空出来）" % (self.size, hint)
                left = memory_available_mb()
                # 开一个新会话的账：先保证够它自己吃（SESSION_COST_MB）；
                # 如果这会儿还有发送任务 / 检测在跑，再多留 TASK_RESERVE_MB，别把它们挤死。
                # 没人和你抢的时候不留额外余量 —— 否则阈值虚高，多出来的那个槽位永远开不出来。
                busy = runner.running() or checker.running()
                need = max(MIN_FREE_MB, SESSION_COST_MB + (TASK_RESERVE_MB if busy else 0))
                if left is not None and left < need:
                    if runner.running():
                        who = "发送任务"
                    elif checker.running():
                        who = "登录检测"
                    else:
                        who = "别的授权"
                    return False, ("内存不够了（只剩 %d MB，开一个授权要 %d MB），等%s结束再试"
                                   % (left, need, who))
                return free[0].start(uid, username)


def fmt_wait(seconds) -> str:
    """把"还要等多少秒"说成人话：45 秒 / 2 分 10 秒。"""
    try:
        total = int(max(0, float(seconds)))
    except Exception:
        return "一会儿"
    if total < 60:
        return "%d 秒" % total
    return "%d 分 %d 秒" % (total // 60, total % 60)


def memory_available_mb():
    """可用内存（MB）。读不到就返回 None（不因为读不到就拦人）。"""
    try:
        with open("/proc/meminfo", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except Exception:
        pass
    return None


browser = BrowserPool(MAX_AUTH_SESSIONS)
runner = TaskRunner()
checker = LoginChecker()
friend_scanner = FriendScanner()

# 面板的 HTTP 服务器对象（优雅重启时要用它来 shutdown）
SERVER = None


class BodyTooLarge(Exception):
    """请求体太大，直接回 413，不解析。"""


def accounts_env() -> dict:
    """把 accounts.json 摊成 main.py 认识的 TASKS / COOKIES_XXX 环境变量。

    发送任务的代码没动，还是从环境变量读账号 —— 我们只是在启动它的地方现拼一份。
    """
    tasks = []
    cookies = {}
    for uid, item in load_accounts().items():
        settings = item.get("settings") if isinstance(item.get("settings"), dict) else {}
        tasks.append(
            {
                "username": str(item.get("username") or uid),
                "unique_id": uid,
                "targets": [str(x) for x in (item.get("targets") or [])],
                # 这个号自己的消息模板 / 发送间隔 / 一言类型：发送端优先用它，
                # 没配才回落到 .env 里的全局值
                "template": str(settings.get("template") or ""),
                "delay_min": settings.get("delay_min"),
                "delay_max": settings.get("delay_max"),
                "hitokoto_types": settings.get("hitokoto_types") or [],
            }
        )
        blob = str(item.get("cookies") or "")
        if not blob:
            continue
        try:
            cookies[cookie_key(uid)] = decrypt_text(blob)
        except Exception:
            continue
    env = {"TASKS": json.dumps(tasks, ensure_ascii=False, separators=(",", ":"))}
    env.update(cookies)
    return env


# --------------------------------------------------------------------------
# 实时画面：授权浏览器和登录检测各存各的，这里只负责"现在该看谁的那张"。
# 归属跟着画面走，不看别的字段 —— 这样别人点检测时不会看到你的画面。
# --------------------------------------------------------------------------
def current_frame():
    """返回 (图片, 归属抖音号)。没画面时返回 (None, "")。"""
    if browser.running():
        return browser.image(), str(getattr(browser, "owner", "") or "")
    if checker.running():
        with checker.lock:
            return checker.png, str(checker.unique_id or "")
    # 两边都没在跑：留着最后一张，让人知道刚才是什么状态
    image = browser.image()
    if image:
        return image, str(getattr(browser, "owner", "") or "")
    with checker.lock:
        return checker.png, str(checker.unique_id or "")


# 截图文件名 → 抖音号 的映射（按 30 秒缓存，压测不会每次都翻盘）
_SHOT_UID_CACHE = {"at": 0.0, "map": {}, "mtime": None}
_SHOT_CACHE_TTL = 5.0


def invalidate_shot_cache() -> None:
    _SHOT_UID_CACHE["at"] = 0.0
    _SHOT_UID_CACHE["map"] = {}
    _SHOT_UID_CACHE["mtime"] = None


def shot_uid_map() -> dict:
    """从发送记录里还原"这张截图是哪个抖音号发的"。"""
    now = time.time()
    mtime = None
    try:
        mtime = SEND_LOG.stat().st_mtime
    except Exception:
        mtime = None
    if (_SHOT_UID_CACHE["map"] and (now - _SHOT_UID_CACHE["at"]) < _SHOT_CACHE_TTL
            and _SHOT_UID_CACHE.get("mtime") == mtime):
        return _SHOT_UID_CACHE["map"]
    mapping = {}
    try:
        for run in load_sends(20):
            uid = str(run.get("unique_id") or "")
            if not uid:
                continue
            for item in (run.get("friends") or []):
                shot = str((item or {}).get("shot") or "")
                if shot:
                    mapping[shot] = uid
    except Exception:
        mapping = {}
    _SHOT_UID_CACHE["at"] = now
    _SHOT_UID_CACHE["map"] = mapping
    # 发送记录一变（刚跑完一轮）就立刻重算：否则新截图会在 5 秒里被判成"不是你的"
    _SHOT_UID_CACHE["mtime"] = mtime
    return mapping

# --------------------------------------------------------------------------
# 页面
# --------------------------------------------------------------------------
INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>DouYinSparkFlow 控制台</title>
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<link rel="manifest" href="/manifest.webmanifest"><link rel="apple-touch-icon" href="/apple-touch-icon.png"><meta name="theme-color" content="#ffffff">
<meta name="apple-mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-title" content="续火花"><meta name="apple-mobile-web-app-status-bar-style" content="default">
<script>(function(){var t="dark";try{var saved=localStorage.getItem("dsh-theme");if(saved==="dark"||saved==="light")t=saved;}catch(e){}document.documentElement.setAttribute("data-theme",t);})();</script>
<style>
:root{
  --brand:#5b6cff;--brand-dark:#4351e0;--brand-soft:#eef0ff;--brand-line:#c9cfff;
  --ink:#111827;--ink2:#374151;--muted:#6b7280;--line:#e5e7eb;--line2:#f1f3f9;
  --bg:#f5f6fb;--card:#fff;
  --ok:#0b7a44;--ok-bg:#e7f7ee;--ok-line:#b3e2c6;
  --warn:#8a5a00;--warn-bg:#fdf5da;--warn-line:#eedca2;
  --bad:#b3261e;--bad-bg:#fdeceb;--bad-line:#f2bcb8;
  --neu:#475569;--neu-bg:#eef1f6;--neu-line:#d7dee9;
  --r:10px;--head-h:56px;
  --side:#0f172a;--side2:#1b2540;--sideink:#c3cee2;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);line-height:1.55;-webkit-text-size-adjust:100%;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,"PingFang SC","Microsoft YaHei",sans-serif}
header{background:#0f172a;color:#fff;padding:10px 18px;display:flex;justify-content:space-between;align-items:center;
  gap:8px 14px;flex-wrap:wrap;min-height:var(--head-h);position:sticky;top:0;z-index:130}
h1{font-size:16px;margin:0;display:flex;align-items:center;gap:8px;white-space:nowrap}
h1 .logo{width:22px;height:22px;border-radius:7px;flex:none;background:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cpath fill='%23fff' fill-rule='evenodd' d='M12 2.6C14.6 6.8 18.4 9.4 18.4 13.8A6.4 6.4 0 0 1 5.6 13.8C5.6 11.2 7.2 9.4 8.6 7.4C8.9 9 9.7 10.2 10.8 11C10.4 8 10.9 5 12 2.6ZM12 12.4C13.4 14 14.4 15.3 14.4 16.8A2.4 2.4 0 0 1 9.6 16.8C9.6 15.3 10.6 14 12 12.4Z'/%3E%3C/svg%3E") center/54% no-repeat,linear-gradient(135deg,#fb4857,#b7192d)}
.head-mid{display:flex;align-items:center;gap:8px;min-width:0;font-size:13px;color:#94a3b8;flex:1 1 auto}
.head-mid b{color:#fff;font-size:14px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:min(38vw,260px)}
.header-right{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.header-right .lnk{color:#cbd5e1;text-decoration:none;font-size:13px;padding:6px 8px;border-radius:6px}
.header-right .lnk:hover{color:#fff;background:rgba(255,255,255,.09)}
.opensource-promo{display:flex;align-items:center;gap:12px;background:var(--brand-soft);border:1px solid var(--brand-line)}
.opensource-promo .promo-mark{display:grid;place-items:center;width:38px;height:38px;flex:none;border-radius:11px;background:linear-gradient(135deg,#f04452,#a91d2d);color:#fff;font-size:12px;font-weight:800;letter-spacing:-.4px}
.opensource-promo .promo-copy{flex:1;min-width:0}
.opensource-promo h2{margin:0 0 2px;font-size:14px}
.opensource-promo p{margin:0;color:var(--muted);font-size:12.5px}
.opensource-promo a{display:inline-flex;align-items:center;gap:5px;min-height:38px;padding:7px 12px;border:1px solid var(--brand-line);border-radius:9px;background:var(--card);color:var(--brand);font-size:13px;font-weight:700;text-decoration:none;white-space:nowrap}
.opensource-promo a:hover{background:var(--brand-soft);text-decoration:underline}
@media(max-width:560px){.opensource-promo{align-items:flex-start;flex-wrap:wrap;gap:9px}.opensource-promo .promo-copy{flex:1 1 calc(100% - 52px)}.opensource-promo a{margin-left:47px}}
main{max-width:1420px;margin:16px auto;padding:0 16px}
section{background:var(--card);border:1px solid var(--line);border-radius:var(--r);padding:14px 16px;margin-bottom:16px}
h2{font-size:15px;margin:0 0 10px;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.step{display:inline-flex;align-items:center;justify-content:center;width:21px;height:21px;border-radius:50%;
  background:linear-gradient(135deg,#fb4857,#b7192d);color:#fff;font-size:12px;font-weight:700;flex:none;border:0;
  box-shadow:0 2px 6px rgba(255,90,43,.3)}
label{display:block;font-weight:600;margin:10px 0 5px;font-size:14px}
.hint{font-weight:400;color:var(--muted);font-size:12px}
input,textarea,select{width:100%;padding:9px;border:1px solid #cbd5e1;border-radius:8px;font:inherit;background:var(--card);color:inherit}
input:focus,textarea:focus,select:focus{outline:2px solid var(--brand-line);outline-offset:1px;border-color:var(--brand)}
input:disabled,textarea:disabled{background:var(--line2);color:var(--muted)}
textarea{min-height:80px;resize:vertical}
button{background:var(--brand);color:#fff;border:0;border-radius:8px;padding:9px 15px;cursor:pointer;font:inherit;
  display:inline-flex;align-items:center;justify-content:center;gap:6px;transition:background .15s,transform .06s}
button:hover:not(:disabled){background:var(--brand-dark)}
button:active:not(:disabled){transform:translateY(1px)}
button:focus-visible{outline:2px solid var(--brand-line);outline-offset:2px}
button.sec{background:var(--neu)}
button.sec:hover:not(:disabled){background:#334155}
button.danger{background:var(--bad)}
button.danger:hover:not(:disabled){background:#8f1d17}
button.danger-ghost{background:var(--card);color:var(--bad);border:1px solid var(--bad-line)}
button.danger-ghost:hover:not(:disabled){background:var(--bad-bg)}
button.ghost{background:var(--card);color:var(--ink2);border:1px solid var(--line)}
button.ghost:hover:not(:disabled){background:var(--line2)}
button.sm{padding:6px 10px;font-size:13px;margin:2px 2px 2px 0}
button:disabled{opacity:.5;cursor:not-allowed}
button svg{flex:none}
.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}
.grid3{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px}
.grid>div,.grid3>div{display:flex;flex-direction:column}
.grid>div>label,.grid3>div>label{flex:1 1 auto}
.muted{color:var(--muted);font-size:13px}
.row{margin-top:8px;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.row[hidden]{display:none}
.row>button,.row>a{flex:none}
.row>input{flex:1 1 200px;width:auto;min-width:0}
.row>img{max-width:100%}
.row.space{justify-content:space-between}
.row .push{margin-left:auto}
.flash{padding:9px 12px;border-radius:8px;margin-bottom:10px;font-size:14px;border:1px solid transparent;
  display:flex;align-items:flex-start;gap:10px}
.flash .close{flex:none}
.ok{background:var(--ok-bg);color:var(--ok);border-color:var(--ok-line)}
.bad{background:var(--bad-bg);color:var(--bad);border-color:var(--bad-line)}
pre{max-height:340px;overflow:auto;background:#0f172a;color:#dbeafe;padding:12px;border-radius:8px;
  white-space:pre-wrap;font-size:12px;margin:0}
#shot{display:block;max-width:100%;max-height:min(62vh,470px);object-fit:contain;border:1px solid var(--line);
  border-radius:8px;cursor:crosshair;background:var(--card)}
#shot[hidden]{display:none}
.badge{display:inline-flex;align-items:center;gap:7px;padding:6px 13px;border-radius:999px;font-weight:600;
  font-size:13px;border:1px solid transparent;background:var(--neu-bg);color:var(--neu)}
.badge .dot{width:8px;height:8px;border-radius:50%;background:currentColor;flex:none}
.badge.g{background:var(--ok-bg);color:var(--ok);border-color:var(--ok-line)}
.badge.r{background:var(--bad-bg);color:var(--bad);border-color:var(--bad-line)}
.badge.y{background:var(--warn-bg);color:var(--warn);border-color:var(--warn-line)}
.badge.n{background:var(--neu-bg);color:var(--neu);border-color:var(--neu-line)}
#headbadge{padding:4px 10px;font-size:12px;white-space:nowrap}
.kv{font-weight:600;margin-top:2px;word-break:break-word;overflow-wrap:anywhere}
#checkresult{font-size:13px;line-height:1.7}
.facts{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.facts>div{background:var(--line2);border-radius:8px;padding:9px 11px}
.empty{text-align:center;color:var(--muted);border:1px dashed #cbd5e1;border-radius:8px;padding:16px;
  font-size:13px;background:var(--card)}
.empty b{display:block;color:var(--ink2);font-size:14px;margin-bottom:4px}
.cnt{font-size:12px;color:var(--muted);margin:5px 0 0}
.cnt b{color:var(--ink2);font-weight:600}
.checkline{display:flex;align-items:flex-start;gap:9px;margin-top:10px;font-size:14px}
.checkline input[type=checkbox]{width:auto;flex:none;margin:3px 0 0}
.checkline label{font-weight:400;margin:0}
#gglobalwrap[hidden]{display:none}
.progress{height:6px;background:var(--neu-bg);border-radius:999px;overflow:hidden;margin-top:8px;max-width:420px}
.progress i{display:block;height:100%;width:0;background:var(--brand);border-radius:999px;transition:width .5s linear}
.progress.done i{background:var(--ok)}
.progress.bad i{background:var(--bad)}
.shots{display:flex;gap:10px;overflow-x:auto;padding:8px 2px 10px}
.shots a{flex:none}
.shots img{height:150px;width:auto;max-width:280px;object-fit:cover;border:1px solid var(--line);border-radius:8px;display:block}
#shotview{position:fixed;inset:0;z-index:200;background:rgba(15,23,42,.86);display:flex;align-items:center;justify-content:center;padding:3vh 3vw}
#shotview[hidden]{display:none}
#shotview img{max-width:100%;max-height:86vh;border-radius:10px;background:#fff;box-shadow:0 12px 40px rgba(0,0,0,.55);display:block}
#shotviewclose{position:absolute;top:16px;right:18px}
#shotviewtip{position:absolute;bottom:12px;left:0;right:0;padding:0 12px;text-align:center;color:#cbd5e1;font-size:12px;word-break:break-all}
.shots img{cursor:zoom-in}
#authstate{display:flex;align-items:center;gap:7px;flex-wrap:wrap}
#acctabs{display:flex;flex-wrap:wrap;gap:8px}
#acctabs button{background:var(--line2);color:var(--ink);padding:7px 12px;font-size:13px;max-width:100%;
  border:1px solid var(--line);font-weight:500}
#acctabs button .nm{display:inline-block;max-width:min(46vw,240px);overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap;vertical-align:bottom}
#acctabs button .dot,#authstate .dot{width:8px;height:8px;border-radius:50%;background:#94a3b8;flex:none}
#acctabs button .dot.g,#authstate .dot.g{background:var(--ok)}
#acctabs button .dot.y,#authstate .dot.y{background:#d97706}
#acctabs button .dot.r,#authstate .dot.r{background:var(--bad)}
#acctabs button.on{background:var(--brand);color:#fff;border-color:var(--brand)}
#acctabs button.on .dot{background:#fff}
details.fold{background:var(--card);border:1px solid var(--line);border-radius:var(--r);padding:12px 16px}
details.fold>summary{cursor:pointer;font-weight:600;font-size:15px;list-style:none;display:flex;
  align-items:center;gap:8px;min-height:26px}
details.fold>summary::-webkit-details-marker{display:none}
details.fold>summary::after{content:'展开';margin-left:auto;color:var(--muted);font-size:12px;font-weight:400}
details.fold[open]>summary::after{content:'收起'}
details.fold>.body{padding-top:6px}
details.adv{margin-top:14px;border:1px solid var(--line);border-radius:8px;background:var(--card)}
details.adv>summary{cursor:pointer;font-weight:600;font-size:13.5px;color:var(--ink2);list-style:none;
  display:flex;align-items:center;gap:8px;padding:11px 13px;border-radius:8px}
details.adv>summary::-webkit-details-marker{display:none}
details.adv>summary::before{content:'';width:0;height:0;border-left:5px solid currentColor;
  border-top:4px solid transparent;border-bottom:4px solid transparent;transition:transform .15s;flex:none}
details.adv[open]>summary::before{transform:rotate(90deg)}
details.adv>summary:hover{background:var(--line2)}
details.adv>.advbody{padding:0 13px 13px}
@media(hover:none){
  button:not(:disabled):active{background:var(--brand-dark);transform:translateY(1px)}
  button.sec:not(:disabled):active{background:#334155}
  button.danger:not(:disabled):active{background:#8f1d17}
  button.ghost:not(:disabled):active,button.danger-ghost:not(:disabled):active{background:var(--line2)}
}
#shotwrap{margin-top:10px}
#shotwrap[hidden]{display:none}
#shotframe{position:relative;display:inline-block;max-width:100%}
#shotframe .livetag{position:absolute;left:8px;top:8px;background:rgba(15,23,42,.72);color:#fff;font-size:12px;
  padding:3px 9px;border-radius:999px;pointer-events:none}
#flash{position:fixed;top:calc(var(--head-h) + 10px);left:50%;transform:translateX(-50%);z-index:210;
  width:min(92vw,660px);pointer-events:none}
#flash .flash{margin:0 0 8px;box-shadow:0 10px 26px rgba(15,23,42,.22);pointer-events:auto}
#flash .flash span{flex:1}
#flash .close{background:transparent;border:0;color:inherit;opacity:.6;padding:0 3px;font-size:17px;
  line-height:1.1;cursor:pointer}
#flash .close:hover{opacity:1;background:transparent;transform:none}
#starterr{color:var(--bad);font-size:13px;margin:8px 0 0;min-height:18px}
.cols{display:grid;grid-template-columns:minmax(0,1.08fr) minmax(0,0.92fr);gap:16px;align-items:start}
.col{display:flex;flex-direction:column;gap:16px;min-width:0}
.col>section,.col>details{margin-bottom:0}
@media(max-width:1240px){main{max-width:1080px}.cols{grid-template-columns:minmax(0,1fr) minmax(0,1fr)}}
@media(max-width:1080px){.cols{grid-template-columns:1fr}}
/* ---- 左侧导航 + 内容面板（和管理控制台同一套观感）---- */
.app{display:grid;grid-template-columns:230px minmax(0,1fr);min-height:100vh;align-items:start}
.side{background:var(--side);color:var(--sideink);display:flex;flex-direction:column;gap:14px;
  padding:18px 12px;position:sticky;top:0;height:100vh}
.brand{display:flex;align-items:center;gap:10px;padding:2px 6px 12px;border-bottom:1px solid rgba(255,255,255,.09)}
.brand .logo{width:26px;height:26px;border-radius:8px;flex:none;background:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cpath fill='%23fff' fill-rule='evenodd' d='M12 2.6C14.6 6.8 18.4 9.4 18.4 13.8A6.4 6.4 0 0 1 5.6 13.8C5.6 11.2 7.2 9.4 8.6 7.4C8.9 9 9.7 10.2 10.8 11C10.4 8 10.9 5 12 2.6ZM12 12.4C13.4 14 14.4 15.3 14.4 16.8A2.4 2.4 0 0 1 9.6 16.8C9.6 15.3 10.6 14 12 12.4Z'/%3E%3C/svg%3E") center/58% no-repeat,linear-gradient(135deg,#fb4857,#b7192d)}
.brand b{display:block;color:#fff;font-size:14px;letter-spacing:.2px}
.brand i{display:block;font-style:normal;font-size:11.5px;color:#8fa0bd}
#nav{display:flex;flex-direction:column;gap:3px}
.nav{appearance:none;border:0;background:transparent;color:var(--sideink);text-align:left;font:inherit;
  padding:9px 11px;border-radius:9px;cursor:pointer;display:flex;align-items:center;gap:9px}
.nav:hover:not(:disabled){background:var(--side2);color:#fff}
.nav.on{background:linear-gradient(90deg,var(--brand),#6d5cf0);color:#fff;font-weight:600}
.nav .pill{margin-left:auto;background:rgba(255,255,255,.16);color:#fff;border-radius:99px;
  padding:1px 8px;font-size:11.5px;font-weight:600}
.nav.on .pill{background:rgba(255,255,255,.28)}
.side-foot{margin-top:auto;font-size:12.5px;display:flex;flex-direction:column;gap:6px;padding:12px 6px 0;
  border-top:1px solid rgba(255,255,255,.09)}
.side-foot .who{color:#fff;font-weight:600;overflow:hidden;text-overflow:ellipsis}
.side-foot a{color:var(--sideink);text-decoration:none;padding:2px 0}
.side-foot a:hover{color:#fff;text-decoration:underline}
main.main{max-width:none;margin:0;padding:18px 22px 40px;min-width:0}
.top{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:14px}
.top h1{margin:0;font-size:19px;letter-spacing:.2px}
.top .sp{flex:1 1 auto}
.top .head-mid{display:flex;align-items:center;gap:8px;min-width:0;font-size:13px;color:var(--muted);flex:0 1 auto}
.top .head-mid b{color:var(--ink);font-size:14px;font-weight:600;white-space:nowrap;
  overflow:hidden;text-overflow:ellipsis;max-width:min(34vw,240px)}
section.panel{display:none;background:none;border:0;padding:0;margin:0}
section.panel.on{display:block}
/* 登录方式：三个并列页签 */
.authtabs{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:6px;margin:12px 0 0;background:var(--line2);border:1px solid var(--line);
  border-radius:10px;padding:4px}
.authtab{flex:1 1 0;appearance:none;border:0;background:transparent;color:var(--ink2);font:inherit;
  font-size:13.5px;padding:7px 10px;border-radius:7px;cursor:pointer}
.authtab:hover:not(.on){background:#e8ecf6}
.authtab.on{background:var(--card);color:var(--brand-dark);font-weight:600;box-shadow:0 1px 3px rgba(15,23,42,.12)}
.authtab{display:flex;align-items:center;justify-content:center;gap:4px;white-space:nowrap}
.auth-rec-badge{display:inline-flex;align-items:center;white-space:nowrap;padding:1px 5px;border-radius:999px;background:#c9233b;color:#fff;font-size:9px;line-height:1.5;font-weight:800;letter-spacing:.2px}
.auth-recommend{display:flex;align-items:flex-start;gap:11px;margin:10px 0 13px;padding:13px 14px;border:1px solid var(--brand-line);border-left:5px solid #c9233b;border-radius:12px;background:var(--brand-soft);color:var(--ink)}
.auth-recommend .ar-icon{display:grid;place-items:center;flex:none;width:30px;height:30px;border-radius:9px;background:#c9233b;color:#fff;font-size:17px;font-weight:800}
.auth-recommend strong{display:block;font-size:14px;line-height:1.4}
.auth-recommend p{margin:3px 0 0;color:var(--ink2);font-size:13px;line-height:1.6}
.manual-screen-tip{display:flex;align-items:flex-start;gap:10px;margin:10px 0;padding:12px 14px;border:2px solid #c9233b;border-radius:12px;background:var(--brand-soft);color:var(--ink)}
.manual-screen-tip .tapmark{display:grid;place-items:center;flex:none;width:32px;height:32px;border-radius:10px;background:#c9233b;color:#fff;font-size:18px}
.manual-screen-tip p{margin:0}
.manual-screen-tip strong{display:block;font-size:14px;line-height:1.35}
.manual-screen-tip p>span{display:block;margin-top:3px;color:var(--ink2);font-size:13px;line-height:1.55}
@media(max-width:560px){.auth-rec-badge{font-size:8.5px;padding:1px 4px}.auth-recommend,.manual-screen-tip{padding:11px 12px;gap:8px}}
.auth-download{display:inline-flex;align-items:center;justify-content:center;padding:7px 11px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--ink2);font-size:13px;text-decoration:none}
.auth-download:hover{border-color:var(--brand-line);background:var(--brand-soft);color:var(--brand)}
@media(max-width:900px){
  .app{grid-template-columns:1fr}
  .side{position:static;height:auto;flex-direction:row;flex-wrap:wrap;align-items:center;gap:8px;padding:10px 12px}
  .brand{border:0;padding:0 8px 0 0}
  #nav{flex-direction:row;flex-wrap:wrap}
  .side-foot{flex-direction:row;gap:12px;margin:0 0 0 auto;border:0;padding:0}
  main.main{padding:14px 14px 32px}
}
@media(max-width:900px){
  .grid3{grid-template-columns:repeat(2,minmax(0,1fr))}
  #guide ol{grid-template-columns:1fr}
}
@media(max-width:680px){
  .grid,.grid3{grid-template-columns:1fr}
  .facts{grid-template-columns:repeat(2,minmax(0,1fr))}
  .facts>div:last-child:nth-child(odd){grid-column:1/-1}
  main{padding:0 10px;margin:12px auto}
  header{padding:9px 12px;gap:6px 10px}
  h1{font-size:15px}
  .head-mid{order:3;flex-basis:100%}
  .head-mid b{max-width:100%}
  .header-right{margin-left:auto}
  #flash{top:calc(var(--head-h) + 6px);width:96vw}
  #acctabs button .nm{max-width:58vw}
}
/* ---- 授权向导弹窗：只是 api/status 的投影，关掉不影响后台授权 ---- */
#authwiz{position:fixed;inset:0;z-index:300;display:flex;align-items:center;justify-content:center}
#authwiz[hidden]{display:none}
#wzbarp[hidden],#wzforce[hidden],#authbubble[hidden]{display:none}
#authwizmask{position:absolute;inset:0;background:rgba(15,23,42,.68)}
#authwizbox{position:relative;width:min(92vw,520px);max-height:92vh;overflow:auto;background:var(--card);
  border-radius:16px;box-shadow:0 24px 60px rgba(0,0,0,.42);display:flex;flex-direction:column}
.wz-head{display:flex;align-items:center;gap:10px;padding:12px 16px;border-bottom:1px solid var(--line)}
.wz-head b{font-size:15px;flex:1 1 auto}
.wz-body{padding:18px 16px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:12px;min-height:236px}
.wz-act{display:flex;flex-direction:column;align-items:center;gap:12px;width:100%}
.wz-act[hidden]{display:none}
.wz-tip{margin:0;text-align:center;color:var(--ink2);font-size:14px}
.wz-tip.muted{color:var(--muted);font-size:12px}
.wz-spin{width:46px;height:46px;border-radius:50%;border:4px solid var(--brand-soft);border-top-color:var(--brand);
  animation:wzspin .9s linear infinite}
@keyframes wzspin{to{transform:rotate(360deg)}}
.wz-code{display:flex;gap:8px;width:100%;max-width:360px}
.wz-code[hidden]{display:none}
.wz-code input{flex:1 1 auto;min-width:0;font-size:18px;letter-spacing:4px;text-align:center;padding:11px 8px}
.wz-code button{flex:0 0 auto;white-space:nowrap;padding:11px 16px}
/* 通用输入框：挂在「浏览器画面」下面（不是授权弹窗里）。
   填完点提交，验证按钮就在上面那一张画面上，直接点 —— 所以它必须跟画面贴在一起。 */
#anybox{margin-top:12px;padding-top:12px;border-top:1px dashed var(--line)}
#anytext{letter-spacing:normal;text-align:left;font-size:14px}
#anyresult{margin:8px 0 0;font-size:13px}
#anyresult[hidden]{display:none}
#anyresult.ok{color:#166534}
#anyresult.bad{color:#b91c1c}
.wz-live{width:100%;max-width:360px;display:flex;flex-direction:column;align-items:center;gap:5px}
#wzlivbox[hidden]{display:none}
#wzsmslive{max-width:100%;max-height:190px;object-fit:contain;border:1px solid var(--line);border-radius:10px;background:#fff}
.wz-tip.ok{color:var(--ok)}
.wz-bar{width:100%;max-width:360px;height:8px;border-radius:99px;background:var(--line2);overflow:hidden}
.wz-bar[hidden]{display:none}
.wz-bar i{display:block;height:100%;width:0;background:var(--brand);border-radius:99px;transition:width .25s linear}
.wz-ok{width:64px;height:64px;border-radius:50%;background:var(--ok-bg);color:var(--ok);font-size:34px;
  display:flex;align-items:center;justify-content:center;animation:wzpop .35s cubic-bezier(.2,1.4,.5,1)}
@keyframes wzpop{from{transform:scale(.4);opacity:0}to{transform:scale(1);opacity:1}}
.wz-acts{display:flex;gap:8px;flex-wrap:wrap;justify-content:center}
.wz-steps{display:flex;align-items:center;gap:6px;padding:10px 16px;border-top:1px solid var(--line);
  justify-content:center;font-size:12px;color:var(--muted)}
.wz-dot{width:8px;height:8px;border-radius:50%;background:var(--line);flex:none}
.wz-dot.on{background:linear-gradient(160deg,#ffb257,#ff7a2f)}
.wz-dot.cur{background:linear-gradient(160deg,#ff8a2b,#ff3d2e);box-shadow:0 0 0 3px rgba(255,122,47,.2)}
#authbubble{position:fixed;right:16px;bottom:16px;z-index:290;box-shadow:0 8px 24px rgba(0,0,0,.22)}
@media (max-width:640px){
  #authwiz{align-items:flex-end}
  #authwizbox{width:100vw;max-width:100vw;border-radius:16px 16px 0 0;max-height:96vh}
  #authbubble{left:2vw;right:2vw;bottom:2vw}
}

/* ---- 新手引导：一条一条按真实状态点亮，不再是一段让人读完就忘的文字 ---- */
#guide{background:var(--card);border:1px solid var(--line);border-radius:var(--r);padding:12px 16px;margin-bottom:16px}
#guide .g-head{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
#guide .g-head b{font-size:15px}
#guide .g-head .sp{flex:1 1 auto}
#guide .g-sum{font-size:13px;color:var(--muted)}
#guide ol{list-style:none;margin:10px 0 0;padding:0;display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:8px}
#guide li{display:flex;align-items:flex-start;gap:10px;border:1px solid var(--line);border-radius:9px;padding:9px 11px;
  background:var(--card);cursor:pointer}
#guide li:hover{background:var(--line2)}
#guide li .gdot{flex:none;width:21px;height:21px;border-radius:50%;display:flex;align-items:center;justify-content:center;
  font-size:12px;font-weight:700;background:linear-gradient(135deg,#fb4857,#b7192d);color:#fff;border:0;margin-top:1px;
  box-shadow:0 2px 6px rgba(255,90,43,.26)}
#guide li .gtxt{min-width:0;flex:1 1 auto}
#guide li .gtxt b{display:block;font-size:13.5px;font-weight:600}
#guide li .gtxt span{display:block;font-size:12px;color:var(--muted);line-height:1.6}
#guide li .gact{flex:none;align-self:center}
#guide li.done{background:var(--ok-bg);border-color:var(--ok-line)}
#guide li.done .gdot{background:var(--ok);color:#fff;border-color:var(--ok)}
#guide li.done .gtxt b{color:var(--ok)}
#guide li.cur{background:var(--brand-soft);border-color:var(--brand-line);box-shadow:0 0 0 1px var(--brand-line) inset}
#guide li.cur .gdot{background:linear-gradient(160deg,#ff8a2b,#ff3d2e);color:#fff;border-color:transparent}
#guide li.cur .gtxt b{color:var(--brand-dark)}
#guide.min ol{display:none}
.sec-hl{animation:sechl 1.6s ease-out}
@keyframes sechl{0%{box-shadow:0 0 0 3px var(--brand-line)}100%{box-shadow:0 0 0 3px transparent}}
/* ---- 深浅色：浅色是默认，[data-theme=dark] 是深色；按钮在顶栏 ---- */
#themebtn{background:transparent;border:0;color:#cbd5e1;font-size:15px;line-height:1;padding:5px 9px;
  border-radius:6px;cursor:pointer}
#themebtn:hover{background:rgba(255,255,255,.09);color:#fff}
html[data-theme="dark"]{
  --brand:#7f8cff;--brand-dark:#6b7bff;--brand-soft:#1d2550;--brand-line:#313c78;
  --ink:#e8ecf7;--ink2:#c5cee2;--muted:#8c98b6;--line:#26314b;--line2:#1a2338;
  --bg:#0b1220;--card:#141d33;
  --ok:#4ade80;--ok-bg:#102a1e;--ok-line:#1f5138;
  --warn:#fbbf24;--warn-bg:#2b2410;--warn-line:#57451a;
  --bad:#f87171;--bad-bg:#2d1517;--bad-line:#5e2a2d;
  --neu:#93a1bb;--neu-bg:#1e293b;--neu-line:#334155;
}
html[data-theme="dark"] select option{background:#141d33;color:#e8ecf7}
html[data-theme="dark"] #shotview{background:rgba(0,0,0,.9)}
html[data-theme="dark"] pre{background:#080e1c}
/* ---- 火花图标：一套遮罩 + 暖色渐变，导航/主题/徽标共用 ---- */
.ni{display:inline-block;flex:none;width:16px;height:16px;vertical-align:-3px;
  background:linear-gradient(160deg,#ffd27a,#ff7a2f);
  -webkit-mask-repeat:no-repeat;mask-repeat:no-repeat;
  -webkit-mask-position:center;mask-position:center;
  -webkit-mask-size:contain;mask-size:contain}
.nav .ni{opacity:.92}
.nav:hover .ni,.nav.on .ni{opacity:1}
.nav.on .ni{background:linear-gradient(160deg,#fff2cd,#ffbb63)}
#themebtn .ni{width:15px;height:15px}
html[data-theme="dark"] #themebtn .ni{background:linear-gradient(160deg,#9a8b78,#5c5348)}
.badge .ni{width:11px;height:11px;vertical-align:-1px}
.ni-flame{-webkit-mask-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cpath fill='%23000' fill-rule='evenodd' d='M12 2.6C14.6 6.8 18.4 9.4 18.4 13.8A6.4 6.4 0 0 1 5.6 13.8C5.6 11.2 7.2 9.4 8.6 7.4C8.9 9 9.7 10.2 10.8 11C10.4 8 10.9 5 12 2.6ZM12 12.4C13.4 14 14.4 15.3 14.4 16.8A2.4 2.4 0 0 1 9.6 16.8C9.6 15.3 10.6 14 12 12.4Z'/%3E%3C/svg%3E");mask-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cpath fill='%23000' fill-rule='evenodd' d='M12 2.6C14.6 6.8 18.4 9.4 18.4 13.8A6.4 6.4 0 0 1 5.6 13.8C5.6 11.2 7.2 9.4 8.6 7.4C8.9 9 9.7 10.2 10.8 11C10.4 8 10.9 5 12 2.6ZM12 12.4C13.4 14 14.4 15.3 14.4 16.8A2.4 2.4 0 0 1 9.6 16.8C9.6 15.3 10.6 14 12 12.4Z'/%3E%3C/svg%3E")}
.ni-accounts{-webkit-mask-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cg fill='none' stroke='%23000' stroke-width='1.9' stroke-linecap='round' stroke-linejoin='round'%3E%3Ccircle cx='12' cy='8.2' r='3.6'/%3E%3Cpath d='M5 20.2c0-3.6 3.1-6.4 7-6.4s7 2.8 7 6.4'/%3E%3C/g%3E%3C/svg%3E");mask-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cg fill='none' stroke='%23000' stroke-width='1.9' stroke-linecap='round' stroke-linejoin='round'%3E%3Ccircle cx='12' cy='8.2' r='3.6'/%3E%3Cpath d='M5 20.2c0-3.6 3.1-6.4 7-6.4s7 2.8 7 6.4'/%3E%3C/g%3E%3C/svg%3E")}
.ni-admin{-webkit-mask-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cg fill='none' stroke='%23000' stroke-width='1.9' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M12 2.9 4.8 5.8v5.5c0 4.5 3 8.3 7.2 9.7 4.2-1.4 7.2-5.2 7.2-9.7V5.8z'/%3E%3Cpath d='m9.2 11.9 2.2 2.2 4.2-4.4'/%3E%3C/g%3E%3C/svg%3E");mask-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cg fill='none' stroke='%23000' stroke-width='1.9' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M12 2.9 4.8 5.8v5.5c0 4.5 3 8.3 7.2 9.7 4.2-1.4 7.2-5.2 7.2-9.7V5.8z'/%3E%3Cpath d='m9.2 11.9 2.2 2.2 4.2-4.4'/%3E%3C/g%3E%3C/svg%3E")}
.ni-logs{-webkit-mask-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cg fill='none' stroke='%23000' stroke-width='1.9' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M6.4 3h7.2l4.8 4.8V21H6.4z'/%3E%3Cpath d='M13.6 3v4.8h4.8'/%3E%3Cpath d='M9.2 12.4h5.6M9.2 16h4'/%3E%3C/g%3E%3C/svg%3E");mask-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cg fill='none' stroke='%23000' stroke-width='1.9' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M6.4 3h7.2l4.8 4.8V21H6.4z'/%3E%3Cpath d='M13.6 3v4.8h4.8'/%3E%3Cpath d='M9.2 12.4h5.6M9.2 16h4'/%3E%3C/g%3E%3C/svg%3E")}
.ni-me{-webkit-mask-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cg fill='none' stroke='%23000' stroke-width='1.9' stroke-linecap='round' stroke-linejoin='round'%3E%3Crect x='3' y='4.6' width='18' height='14.8' rx='2.6'/%3E%3Ccircle cx='8.8' cy='11' r='2.5'/%3E%3Cpath d='M5.4 16.8c.6-1.7 1.9-2.6 3.4-2.6s2.8.9 3.4 2.6'/%3E%3Cpath d='M15.4 10.2h3.4M15.4 13.8h3.4'/%3E%3C/g%3E%3C/svg%3E");mask-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cg fill='none' stroke='%23000' stroke-width='1.9' stroke-linecap='round' stroke-linejoin='round'%3E%3Crect x='3' y='4.6' width='18' height='14.8' rx='2.6'/%3E%3Ccircle cx='8.8' cy='11' r='2.5'/%3E%3Cpath d='M5.4 16.8c.6-1.7 1.9-2.6 3.4-2.6s2.8.9 3.4 2.6'/%3E%3Cpath d='M15.4 10.2h3.4M15.4 13.8h3.4'/%3E%3C/g%3E%3C/svg%3E")}
.ni-clock{-webkit-mask-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cg fill='none' stroke='%23000' stroke-width='1.9' stroke-linecap='round' stroke-linejoin='round'%3E%3Ccircle cx='12' cy='12' r='8.5'/%3E%3Cpath d='M12 7v5l3.4 2'/%3E%3C/g%3E%3C/svg%3E");mask-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cg fill='none' stroke='%23000' stroke-width='1.9' stroke-linecap='round' stroke-linejoin='round'%3E%3Ccircle cx='12' cy='12' r='8.5'/%3E%3Cpath d='M12 7v5l3.4 2'/%3E%3C/g%3E%3C/svg%3E")}
.ni-overview{-webkit-mask-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cg fill='none' stroke='%23000' stroke-width='1.9' stroke-linecap='round' stroke-linejoin='round'%3E%3Crect x='3.4' y='3.4' width='7.4' height='7.4' rx='1.7'/%3E%3Crect x='13.2' y='3.4' width='7.4' height='7.4' rx='1.7'/%3E%3Crect x='3.4' y='13.2' width='7.4' height='7.4' rx='1.7'/%3E%3Crect x='13.2' y='13.2' width='7.4' height='7.4' rx='1.7'/%3E%3C/g%3E%3C/svg%3E");mask-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cg fill='none' stroke='%23000' stroke-width='1.9' stroke-linecap='round' stroke-linejoin='round'%3E%3Crect x='3.4' y='3.4' width='7.4' height='7.4' rx='1.7'/%3E%3Crect x='13.2' y='3.4' width='7.4' height='7.4' rx='1.7'/%3E%3Crect x='3.4' y='13.2' width='7.4' height='7.4' rx='1.7'/%3E%3Crect x='13.2' y='13.2' width='7.4' height='7.4' rx='1.7'/%3E%3C/g%3E%3C/svg%3E")}
.ni-records{-webkit-mask-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cg fill='none' stroke='%23000' stroke-width='1.9' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M21.2 2.8 3 9.9l7.1 3 3 7.1z'/%3E%3Cpath d='M21.2 2.8 10.1 12.9'/%3E%3C/g%3E%3C/svg%3E");mask-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cg fill='none' stroke='%23000' stroke-width='1.9' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M21.2 2.8 3 9.9l7.1 3 3 7.1z'/%3E%3Cpath d='M21.2 2.8 10.1 12.9'/%3E%3C/g%3E%3C/svg%3E")}
.ni-system{-webkit-mask-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cg fill='none' stroke='%23000' stroke-width='1.9' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M4 7h10M18 7h2M4 17h2M10 17h10'/%3E%3Ccircle cx='16' cy='7' r='2.3'/%3E%3Ccircle cx='8' cy='17' r='2.3'/%3E%3C/g%3E%3C/svg%3E");mask-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cg fill='none' stroke='%23000' stroke-width='1.9' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M4 7h10M18 7h2M4 17h2M10 17h10'/%3E%3Ccircle cx='16' cy='7' r='2.3'/%3E%3Ccircle cx='8' cy='17' r='2.3'/%3E%3C/g%3E%3C/svg%3E")}
.ni-clock{-webkit-mask-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cg fill='none' stroke='%23000' stroke-width='1.9' stroke-linecap='round' stroke-linejoin='round'%3E%3Ccircle cx='12' cy='12' r='8.5'/%3E%3Cpath d='M12 7v5l3.4 2'/%3E%3C/g%3E%3C/svg%3E");mask-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cg fill='none' stroke='%23000' stroke-width='1.9' stroke-linecap='round' stroke-linejoin='round'%3E%3Ccircle cx='12' cy='12' r='8.5'/%3E%3Cpath d='M12 7v5l3.4 2'/%3E%3C/g%3E%3C/svg%3E")}
.ni-users{-webkit-mask-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cg fill='none' stroke='%23000' stroke-width='1.9' stroke-linecap='round' stroke-linejoin='round'%3E%3Ccircle cx='9' cy='8' r='3.4'/%3E%3Cpath d='M2.8 20.2c0-3.5 2.8-6.1 6.2-6.1s6.2 2.6 6.2 6.1'/%3E%3Cpath d='M16.6 5.4a3.4 3.4 0 0 1 0 6.5'/%3E%3Cpath d='M17.6 20.2c0-2.2-.6-3.9-1.7-5.1'/%3E%3C/g%3E%3C/svg%3E");mask-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cg fill='none' stroke='%23000' stroke-width='1.9' stroke-linecap='round' stroke-linejoin='round'%3E%3Ccircle cx='9' cy='8' r='3.4'/%3E%3Cpath d='M2.8 20.2c0-3.5 2.8-6.1 6.2-6.1s6.2 2.6 6.2 6.1'/%3E%3Cpath d='M16.6 5.4a3.4 3.4 0 0 1 0 6.5'/%3E%3Cpath d='M17.6 20.2c0-2.2-.6-3.9-1.7-5.1'/%3E%3C/g%3E%3C/svg%3E")}
/* ---- 公告条（主界面最上方；正文 + 管理员联系方式）---- */
#notice{background:var(--brand-soft);border:1px solid var(--brand-line);border-radius:var(--r);
  padding:12px 16px;margin-bottom:16px}
#notice .n-head{display:flex;align-items:center;gap:8px;margin-bottom:4px}
#notice .n-head b{font-size:14px;color:var(--brand-dark);letter-spacing:.5px}
#notice .n-head .sp{flex:1}
#notice .n-body{white-space:pre-wrap;word-break:break-word;font-size:14px;line-height:1.75;color:var(--ink)}
#notice .n-contact{margin-top:10px;padding-top:10px;border-top:1px dashed var(--brand-line);
  white-space:pre-wrap;word-break:break-word;font-size:13px;line-height:1.7;color:var(--ink2)}
#notice .n-contact b{color:var(--brand-dark)}
#notice .n-foot{margin-top:8px;font-size:12px;color:var(--muted)}
/* ---- 定向消息（和公告条同一位置，一条一个卡片；只发给管理员勾选的人）---- */
#msgs{margin-bottom:16px}
#msgs .m-cap{font-size:12px;color:var(--muted);margin:0 0 6px}
.msg{background:var(--card);border:1px solid var(--brand-line);border-left:3px solid var(--brand);
  border-radius:var(--r);padding:12px 16px;margin-bottom:10px}
.msg .m-head{display:flex;align-items:center;gap:8px;margin-bottom:4px;flex-wrap:wrap}
.msg .m-head b{font-size:14px;color:var(--brand-dark);letter-spacing:.5px}
.msg .m-head .sp{flex:1}
.msg .m-head .m-at{font-size:12px;color:var(--muted)}
.msg .m-body{white-space:pre-wrap;word-break:break-word;font-size:14px;line-height:1.75;color:var(--ink)}
.msg .m-dot{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--bad);flex:none}
.msg .m-foot{margin-top:8px;font-size:12px;color:var(--muted)}

/* ---- visual refresh ---- */
:root{
  --brand:#2563eb;--brand-dark:#1d4ed8;--brand-soft:#eff6ff;--brand-line:#bfdbfe;
  --ink:#172033;--ink2:#334155;--muted:#64748b;--line:#dfe6ef;--line2:#f3f6fa;
  --bg:#f3f6fa;--card:#ffffff;--r:12px;
  --side:#172238;--side2:#243452;--sideink:#b9c6dc;
}
body{background:var(--bg);color:var(--ink)}
.side{background:var(--side);padding:20px 14px;box-shadow:10px 0 28px rgba(25,42,70,.08)}
.brand{padding:0 8px 16px;border-bottom-color:rgba(255,255,255,.12)}
.brand b{letter-spacing:0;font-size:14px}
.nav{padding:10px 12px;border-radius:8px;color:#b9c6dc}
.nav:hover:not(:disabled){background:var(--side2)}
.nav.on{background:#2d6cdf;box-shadow:0 5px 14px rgba(37,99,235,.22)}
main.main{padding:24px 28px 48px}
.top{margin-bottom:20px}
.top h1{font-size:21px;letter-spacing:0}
section,.cols>section,.col>section{border-color:var(--line);box-shadow:0 4px 18px rgba(31,49,79,.045)}
section.panel{box-shadow:none}
#guide{padding:15px 18px;margin-bottom:20px;box-shadow:0 4px 18px rgba(31,49,79,.045)}
#guide li{border-color:#e5ebf3;background:#fbfcfe;transition:border-color .15s,background .15s,transform .15s}
#guide li:hover{background:#f5f8fd;border-color:#cbd9ec;transform:translateY(-1px)}
#guide li.cur{background:var(--brand-soft);border-color:var(--brand-line);box-shadow:0 0 0 1px var(--brand-line) inset}
.step,#guide li .gdot{background:#f97316;box-shadow:0 3px 8px rgba(249,115,22,.22)}
.step{width:24px;height:24px}
button{border-radius:8px;box-shadow:0 2px 5px rgba(37,99,235,.12)}
button:hover:not(:disabled){box-shadow:0 4px 10px rgba(37,99,235,.18)}
button.ghost,button.danger-ghost{box-shadow:none}
input,textarea,select{border-color:#cbd5e1;border-radius:8px;background:#fff;transition:border-color .15s,box-shadow .15s}
input:focus,textarea:focus,select:focus{box-shadow:0 0 0 3px rgba(37,99,235,.13)}
.facts>div{background:#f7f9fc;border:1px solid #edf1f6}
.empty{background:#fbfcfe;border-color:#cbd5e1}
.badge{padding:5px 11px}
.authtabs{background:#f5f7fb;border-color:#e4eaf2}
.authtab.on{color:var(--brand-dark)}
details.fold{box-shadow:0 4px 18px rgba(31,49,79,.045)}
details.adv{border-color:#e3e9f2}
#notice{box-shadow:0 4px 16px rgba(37,99,235,.06)}
.msg{box-shadow:0 4px 16px rgba(37,99,235,.05)}
html[data-theme="dark"]{
  --brand:#6ea0ff;--brand-dark:#8bb4ff;--brand-soft:#172b4e;--brand-line:#2e5a9d;
  --ink:#edf3ff;--ink2:#c9d6eb;--muted:#91a3bf;--line:#293a56;--line2:#1b2a42;
  --bg:#0f1929;--card:#16243a;--side:#0e1829;--side2:#213453;
}
html[data-theme="dark"] .facts>div,html[data-theme="dark"] #guide li{background:#1b2a42;border-color:#293a56}
html[data-theme="dark"] input,html[data-theme="dark"] textarea,html[data-theme="dark"] select{background:#101c30;border-color:#354967}
html[data-theme="dark"] .empty{background:#16243a}
@media(max-width:900px){.side{box-shadow:0 4px 16px rgba(25,42,70,.12)}main.main{padding:18px 16px 36px}}
@media(max-width:680px){main.main{padding:14px 12px 30px}.top h1{font-size:19px}}
/* 用户端视觉重构 */
:root{--brand:#087f8c;--brand-dark:#076b76;--brand-soft:#e6f6f4;--brand-line:#9ddbd5;--ink:#172b32;--ink2:#3c555d;--muted:#6a8187;--line:#dce9e9;--line2:#f0f6f5;--bg:#f2f7f6;--card:#fff;--r:16px;--side:#102d34;--side2:#19434a;--sideink:#c4dcdd}
body{background:radial-gradient(ellipse at 78% -15%,#dff2ed 0,transparent 40%),var(--bg);color:var(--ink)}.side{background:linear-gradient(180deg,#123840,#102b33);box-shadow:8px 0 26px #13373d22}.brand{padding:4px 9px 17px}.nav{min-height:44px;border:1px solid transparent;transition:.18s}.nav.on{background:linear-gradient(110deg,#0c8b91,#087f8c);box-shadow:0 6px 16px #087f8c38}.top{background:#f2f7f6e0;backdrop-filter:blur(14px);padding:9px 0}.top h1{font-size:22px;letter-spacing:-.3px}section{border-radius:16px;box-shadow:0 5px 20px #1a484a0b}input,textarea,select{border-color:#cbdcdd;border-radius:10px}button{border-radius:10px;min-height:40px;transition:.15s}.badge{border-radius:999px}
.today-status{display:flex;align-items:center;gap:14px;padding:15px 18px;margin:0 0 15px;border:1px solid #b9e1d7;border-radius:16px;background:linear-gradient(115deg,#e9f8f1,#f3faf8);box-shadow:0 5px 18px #1877610e}.today-mark{width:38px;height:38px;display:grid;place-items:center;border-radius:13px;background:#d4f0e4;color:#148264;font-size:21px;flex:none}.today-copy{min-width:0;flex:1;display:flex;flex-direction:column;gap:2px}.today-state{font-size:15px;color:#334b52}.today-state.success{color:#087957}.today-state.partial{color:#986600}.today-state.failed{color:#ad3434}.today-state.pending{color:#8a6500}#todaySendMeta{color:var(--muted);font-size:12.5px;overflow-wrap:anywhere}
html[data-theme="dark"]{--brand:#59c7c2;--brand-dark:#83ded4;--brand-soft:#123f42;--brand-line:#286b68;--ink:#e7f2f1;--ink2:#c2d6d5;--muted:#93acab;--line:#2a4649;--line2:#192f33;--bg:#102126;--card:#172d32;--side:#091d22;--side2:#183b40}html[data-theme="dark"] body{background:#102126}html[data-theme="dark"] .today-status{background:#173c34;border-color:#2c6356}html[data-theme="dark"] .today-state{color:#d6e7e5}html[data-theme="dark"] input,html[data-theme="dark"] textarea,html[data-theme="dark"] select{background:#10252a;border-color:#38575a}
@media(max-width:900px){.app{display:block}.side{position:fixed;inset:auto 0 0;height:auto;min-height:63px;width:100%;padding:4px 7px calc(5px + env(safe-area-inset-bottom));z-index:150;box-shadow:0 -8px 26px #102d3429}.brand,.side-foot{display:none}nav{height:54px;display:flex;flex-direction:row;overflow-x:auto;overscroll-behavior-x:contain;scrollbar-width:none;gap:3px}nav::-webkit-scrollbar{display:none}.nav{flex:1 0 61px;min-width:61px;min-height:50px;padding:5px 4px;display:flex;flex-direction:column;justify-content:center;gap:3px;font-size:10.5px;line-height:1.1;text-align:center;white-space:nowrap}.nav .ni{width:18px;height:18px}main.main{padding:12px 14px calc(90px + env(safe-area-inset-bottom));margin:0}.top{position:sticky;top:0;z-index:90;margin:-12px -14px 12px;padding:10px 14px;background:#f2f7f6f2;border-bottom:1px solid #dce9e9}.today-status{padding:13px;gap:10px}.cols{grid-template-columns:1fr}.col{min-width:0}}
@media(max-width:560px){main.main{padding-left:11px;padding-right:11px}.top{margin-left:-11px;margin-right:-11px;padding:10px 11px;gap:7px}.top .head-mid{order:3;flex-basis:100%;max-width:100%}.top #hbstart{margin-left:auto}.today-status{align-items:flex-start}section{padding:12px}.row>button{flex:1 1 auto}input,textarea,select{font-size:16px}button{min-height:44px}.authtab{font-size:12px;padding:8px 4px}.tblwrap,.reclist{max-width:100%;overscroll-behavior-x:contain}}

/* Red and black by default; the theme button switches to white and red. */
html[data-theme="dark"]{color-scheme:dark;--brand:#f04452;--brand-dark:#d92e3e;--brand-soft:#311519;--brand-line:#79333d;--ink:#f5f2f3;--ink2:#ded6d8;--muted:#a49a9d;--line:#393336;--line2:#211d20;--bg:#0b0a0b;--card:#151214;--side:#070607;--side2:#1b1518;--sideink:#d8cfd2;--ok:#5bd59e;--ok-bg:#12271f;--ok-line:#2a5841;--warn:#f4c35d;--warn-bg:#2b2112;--warn-line:#6e5221;--bad:#ff7d85;--bad-bg:#311519;--bad-line:#79333d;--neu:#cbd0d8;--neu-bg:#202126;--neu-line:#3a3c43}
html[data-theme="light"]{color-scheme:light;--brand:#ca2638;--brand-dark:#a91d2d;--brand-soft:#fff0f2;--brand-line:#efb5bc;--ink:#241b1d;--ink2:#57474a;--muted:#806f72;--line:#eadcdf;--line2:#fff3f4;--bg:#fff9f9;--card:#fff;--side:#fff;--side2:#fff0f2;--sideink:#58494c;--ok:#087a50;--ok-bg:#e7f7ee;--ok-line:#b3e2c6;--warn:#8a5a00;--warn-bg:#fdf5da;--warn-line:#eedca2;--bad:#a91d2d;--bad-bg:#fdebed;--bad-line:#efb5bc;--neu:#475569;--neu-bg:#f2f3f5;--neu-line:#d7dee9}
html[data-theme="dark"] body{background:radial-gradient(ellipse at 78% -15%,#35151b 0,transparent 38%),var(--bg);color:var(--ink)}
html[data-theme="light"] body{background:radial-gradient(ellipse at 78% -15%,#fff0f1 0,transparent 38%),var(--bg);color:var(--ink)}
html[data-theme] .side{background:var(--side);box-shadow:0 8px 24px rgba(30,8,12,.12)}
html[data-theme="dark"] .side{box-shadow:8px 0 26px rgba(0,0,0,.28)}
html[data-theme="light"] .brand b{color:var(--ink)}
html[data-theme="light"] .brand i{color:var(--muted)}
html[data-theme] .nav:hover:not(:disabled){background:var(--side2);color:var(--brand)}
html[data-theme] .nav.on{background:linear-gradient(110deg,var(--brand),var(--brand-dark));color:#fff;box-shadow:0 6px 16px rgba(190,25,44,.24)}
html[data-theme] .top{background:var(--bg);border-color:var(--line)}
html[data-theme] section{border-color:var(--line)}
html[data-theme] input,html[data-theme] textarea,html[data-theme] select{background:var(--card);border-color:var(--line);color:var(--ink)}
html[data-theme] input:focus,html[data-theme] textarea:focus,html[data-theme] select:focus{outline-color:var(--brand-line);border-color:var(--brand)}
html[data-theme] .step{background:linear-gradient(135deg,#f45563,#b7192d);box-shadow:0 2px 6px rgba(190,25,44,.25)}
html[data-theme] .brand .logo{background:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cpath fill='%23fff' fill-rule='evenodd' d='M12 2.6C14.6 6.8 18.4 9.4 18.4 13.8A6.4 6.4 0 0 1 5.6 13.8C5.6 11.2 7.2 9.4 8.6 7.4C8.9 9 9.7 10.2 10.8 11C10.4 8 10.9 5 12 2.6ZM12 12.4C13.4 14 14.4 15.3 14.4 16.8A2.4 2.4 0 0 1 9.6 16.8C9.6 15.3 10.6 14 12 12.4Z'/%3E%3C/svg%3E") center/58% no-repeat,linear-gradient(135deg,#f45563,#b7192d)}
html[data-theme="dark"] .today-status{background:linear-gradient(115deg,#14261e,#171b18);border-color:#315b43}
html[data-theme="light"] .today-status{background:linear-gradient(115deg,#f0fbf4,#fff);border-color:#b9dfc8}
html[data-theme="dark"] input,html[data-theme="dark"] textarea,html[data-theme="dark"] select{background:#191516;border-color:#514448}
html[data-theme="dark"] .empty,html[data-theme="dark"] .facts>div{background:#1a1719;border-color:var(--line)}
html[data-theme="light"] .empty,html[data-theme="light"] .facts>div{background:#fff;border-color:var(--line)}
.account-menu{position:relative;z-index:200;flex:none;color:var(--ink)}
.account-menu>summary{list-style:none;display:inline-flex;align-items:center;justify-content:center;gap:6px;min-height:40px;padding:6px 10px;border:1px solid var(--line);border-radius:10px;background:var(--card);color:var(--ink2);font-size:13px;font-weight:600;cursor:pointer;white-space:nowrap}
.account-menu>summary::-webkit-details-marker{display:none}
.account-menu>summary:hover,.account-menu[open]>summary{border-color:var(--brand-line);color:var(--brand);background:var(--brand-soft)}
.account-panel{position:absolute;right:0;top:calc(100% + 8px);width:246px;padding:8px;border:1px solid var(--line);border-radius:14px;background:var(--card);box-shadow:0 16px 44px rgba(20,8,10,.24);display:grid;gap:3px;z-index:220}
.account-id{display:grid;gap:2px;padding:10px 11px 11px;margin-bottom:3px;border-bottom:1px solid var(--line)}
.account-id b{font-size:13px;color:var(--ink);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.account-id span{font-size:11.5px;color:var(--muted)}
.account-panel a{display:flex;align-items:center;min-height:40px;padding:8px 11px;border-radius:9px;color:var(--ink2);font-size:13px;text-decoration:none}
.account-panel a:hover{background:var(--brand-soft);color:var(--brand)}
.account-panel a.account-exit{color:var(--bad)}
.account-panel a.account-exit:hover{background:var(--bad-bg)}
.theme-label{font-size:12px;white-space:nowrap}
html[data-theme] #themebtn{gap:5px;border-color:var(--line);color:var(--ink2);background:var(--card)}
html[data-theme] #themebtn:hover{border-color:var(--brand-line);color:var(--brand)}
@media(max-width:900px){html[data-theme] .side{box-shadow:0 -8px 26px rgba(30,8,12,.18)}html[data-theme] .top{background:var(--bg);border-bottom-color:var(--line);z-index:180}}
@media(max-width:560px){.account-panel{width:min(270px,calc(100vw - 22px))}}

/* DouYinSparkFlow visual refresh: a calm operations desk with the spark red reserved for action. */
html[data-theme="dark"]{
  color-scheme:dark;--brand:#e84d5b;--brand-dark:#cc3546;--brand-soft:#2d191e;--brand-line:#65313a;
  --ink:#f5edef;--ink2:#d8c8cc;--muted:#a58f95;--line:#3b2b30;--line2:#261b1f;
  --bg:#100d0f;--card:#191416;--side:#130f11;--side2:#29191e;--sideink:#cdbdc1;
  --ok:#68d8a1;--ok-bg:#14251e;--ok-line:#2d5c45;--warn:#f1c469;--warn-bg:#2b2114;--warn-line:#6c5428;
  --bad:#ff8790;--bad-bg:#30191e;--bad-line:#6b343c;--neu:#c5c8ce;--neu-bg:#222126;--neu-line:#414047;
}
html[data-theme="light"]{
  color-scheme:light;--brand:#bd263c;--brand-dark:#981b31;--brand-soft:#fbe9ec;--brand-line:#ecc2c8;
  --ink:#2b1a1e;--ink2:#5d454b;--muted:#806a70;--line:#e8d8db;--line2:#f8edef;
  --bg:#fff8f8;--card:#fff;--side:#fffdfd;--side2:#f9ecee;--sideink:#60484e;
  --ok:#08794e;--ok-bg:#e8f6ef;--ok-line:#b5dfc8;--warn:#845900;--warn-bg:#fff5dc;--warn-line:#ead9a9;
  --bad:#a82034;--bad-bg:#fdebed;--bad-line:#efbdc4;--neu:#4c515c;--neu-bg:#f1f2f5;--neu-line:#dce0e7;
}
html[data-theme] body{font-family:Inter,"Aptos","PingFang SC","Microsoft YaHei","Noto Sans CJK SC",sans-serif;font-size:14px;font-variant-numeric:tabular-nums;background:var(--bg);color:var(--ink)}
html[data-theme] .app{grid-template-columns:224px minmax(0,1fr);min-height:100vh}
html[data-theme] .side{gap:20px;padding:22px 15px;background:var(--side);border-right:1px solid var(--line);box-shadow:none}
html[data-theme] .brand{gap:11px;padding:4px 8px 17px;border-bottom:1px solid var(--line)}
html[data-theme] .brand .logo{width:34px;height:34px;border-radius:11px;box-shadow:none;background-size:58%,100%}
html[data-theme] .brand b{font-size:14px;letter-spacing:.1px}
html[data-theme="dark"] .brand b{color:#fff}
html[data-theme] .brand i{margin-top:2px;color:var(--muted);font-size:11px;letter-spacing:.1px}
html[data-theme] nav{gap:5px}
html[data-theme] .nav{min-height:42px;padding:9px 11px;border:1px solid transparent;border-radius:9px;color:var(--sideink);font-size:13px;transition:background .16s,color .16s,border-color .16s}
html[data-theme] .nav:hover{background:var(--side2);color:var(--brand)}
html[data-theme] .nav.on{background:var(--brand-soft);border-color:var(--brand-line);color:var(--brand);box-shadow:none;font-weight:700}
html[data-theme] .nav.on .ni{background:var(--brand)}
html[data-theme] .side-foot{gap:8px;padding:14px 8px 0;border-top-color:var(--line)}
html[data-theme] .side-foot .who{color:var(--ink);overflow-wrap:anywhere}
html[data-theme] .side-foot a{color:var(--muted);text-decoration:none}
html[data-theme] .side-foot a:hover{color:var(--brand)}
html[data-theme] main.main{width:100%;max-width:1600px;margin:0 auto;padding:24px clamp(18px,3vw,42px) 56px}
html[data-theme] .top{position:sticky;top:0;z-index:120;min-height:58px;margin:-24px calc(-1 * clamp(18px,3vw,42px)) 22px;padding:10px clamp(18px,3vw,42px);background:var(--bg);border:0;border-bottom:1px solid var(--line)}
html[data-theme] .top h1{font-size:20px;letter-spacing:-.35px}
html[data-theme] .head-mid{color:var(--muted)}
html[data-theme] .head-mid b{color:var(--ink);font-size:13px}
html[data-theme] .panel{margin:0;padding:0;border:0;border-radius:0;background:transparent;box-shadow:none}
html[data-theme] .panel>section,html[data-theme] .panel .col>section,html[data-theme] .overview-layout>section{margin:0 0 14px;padding:19px 20px;border:1px solid var(--line);border-radius:13px;background:var(--card);box-shadow:none}
html[data-theme] .panel>section h2,html[data-theme] .panel .col>section h2,html[data-theme] .overview-layout>section h2{font-size:15px;letter-spacing:-.1px}
html[data-theme] .overview-heading{margin:0 0 16px}
html[data-theme] .overview-heading h2{font-size:23px;letter-spacing:-.55px}
html[data-theme] .overview-heading p{font-size:13px;color:var(--muted)}
html[data-theme] .today-status{min-height:82px;margin-bottom:13px;padding:18px 20px;border:1px solid var(--brand-line);border-left:4px solid var(--brand);border-radius:12px;background:var(--brand-soft);box-shadow:none}
html[data-theme] .today-mark{width:30px;height:30px;display:grid;place-items:center;border-radius:9px;background:var(--brand);color:#fff;font-size:17px}
html[data-theme] .today-copy b{font-size:16px;letter-spacing:-.2px;color:var(--ink)}
html[data-theme] .today-copy b.success{color:var(--ok)}
html[data-theme] .today-copy b.partial,html[data-theme] .today-copy b.pending{color:var(--warn)}
html[data-theme] .today-copy b.failed{color:var(--bad)}
html[data-theme] .today-copy span{font-size:12.5px;color:var(--muted)}
html[data-theme] .overview-stats{grid-template-columns:repeat(4,minmax(0,1fr));gap:0;margin:0 0 17px;padding:12px 0;border:1px solid var(--line);border-radius:12px;background:var(--card)}
html[data-theme] .overview-stat{padding:5px 18px;border:0;border-right:1px solid var(--line);border-radius:0;background:transparent}
html[data-theme] .overview-stat:last-child{border-right:0}
html[data-theme] .overview-stat span{font-size:12px;color:var(--muted)}
html[data-theme] .overview-stat b{margin-top:5px;font-size:25px;letter-spacing:-.5px;color:var(--ink)}
html[data-theme] #todaySuccessCount{color:var(--ok)}
html[data-theme] .overview-layout{grid-template-columns:minmax(0,1.55fr) minmax(260px,.8fr);gap:14px}
html[data-theme] .overview-layout>section{margin:0;padding:18px 20px}
html[data-theme] .overview-layout>section:last-child{background:var(--line2)}
html[data-theme] .overview-run{padding:12px 2px;border-top-color:var(--line)}
html[data-theme] .overview-run-main b{color:var(--ink);font-size:13.5px}
html[data-theme] .overview-run-main span{color:var(--muted)}
html[data-theme] .overview-shortcut{padding:13px 0;border-top-color:var(--line)}
html[data-theme] .overview-shortcut b{font-size:13px}
html[data-theme] .overview-shortcut span{font-size:12px;color:var(--muted)}
html[data-theme] .opensource-promo{margin-bottom:14px;padding:11px 14px;border-color:var(--line);border-radius:11px;background:var(--card)}
html[data-theme] .opensource-promo .promo-mark{width:34px;height:34px;border-radius:9px;background:var(--brand);font-size:11px}
html[data-theme] .opensource-promo h2{margin:0 0 1px;font-size:13px}
html[data-theme] .opensource-promo p{font-size:12px;color:var(--muted)}
html[data-theme] .opensource-promo a{min-height:34px;border-color:var(--brand-line);border-radius:8px;background:var(--brand-soft);color:var(--brand);font-size:12px}
html[data-theme] button{min-height:38px;border-radius:8px;background:var(--brand);color:#fff;font-weight:600;box-shadow:none;transition:background .16s,border-color .16s,color .16s}
html[data-theme] button:hover:not(:disabled){background:var(--brand-dark)}
html[data-theme] button.sm{min-height:32px}
html[data-theme] button.ghost,html[data-theme] button.sec{border:1px solid var(--line);background:var(--card);color:var(--ink2)}
html[data-theme] button.ghost:hover:not(:disabled),html[data-theme] button.sec:hover:not(:disabled){border-color:var(--brand-line);background:var(--brand-soft);color:var(--brand)}
html[data-theme] button.danger-ghost{border-color:var(--bad-line);background:transparent;color:var(--bad)}
html[data-theme] input,html[data-theme] textarea,html[data-theme] select{border-color:var(--line);border-radius:8px;background:var(--card);color:var(--ink)}
html[data-theme] input:focus,html[data-theme] textarea:focus,html[data-theme] select:focus{outline:3px solid rgba(232,77,91,.22);outline-offset:1px;border-color:var(--brand)}
html[data-theme] label{color:var(--ink2)}
html[data-theme] .badge{border-radius:7px;font-size:12px}
html[data-theme] .authtabs{border-color:var(--line);background:var(--line2)}
html[data-theme] button.authtab{border:1px solid transparent;background:transparent;color:var(--ink2);font-weight:500}
html[data-theme] button.authtab:hover:not(.on){background:var(--line2);color:var(--ink)}
html[data-theme] button.authtab.on{border-color:var(--brand);background:var(--brand);color:#fff;font-weight:700}
html[data-theme] .auth-recommend,html[data-theme] .manual-screen-tip{border-color:var(--brand-line);border-radius:10px;background:var(--brand-soft)}
html[data-theme] .progress{background:var(--line2)}
html[data-theme] pre{border:1px solid var(--line);border-radius:10px;background:#120f11;color:#eadfe2}
html[data-theme] a{color:var(--brand)}
html[data-theme] .side-foot a{color:var(--muted)}
html[data-theme] .account-panel a:not(.account-exit){color:var(--ink2)}
html[data-theme] .account-panel a.account-exit{color:var(--bad)}
html[data-theme] :focus-visible{outline:3px solid rgba(232,77,91,.48);outline-offset:2px}
@media(max-width:900px){html[data-theme] .app{display:block}html[data-theme] .side{position:fixed;inset:auto 0 0;height:auto;min-height:62px;width:100%;padding:4px 8px calc(5px + env(safe-area-inset-bottom));z-index:150;border:0;border-top:1px solid var(--line);box-shadow:none}html[data-theme] nav{height:54px;gap:4px}html[data-theme] .nav{min-height:50px;border-radius:8px}html[data-theme] main.main{padding:12px 18px calc(92px + env(safe-area-inset-bottom))}html[data-theme] .top{margin:-12px -18px 17px;padding:9px 18px}}
@media(max-width:720px){html[data-theme] .overview-layout{grid-template-columns:1fr}}
@media(max-width:560px){html[data-theme] main.main{padding-right:12px;padding-left:12px}html[data-theme] .top{margin-right:-12px;margin-left:-12px;padding-right:12px;padding-left:12px;gap:7px}html[data-theme] .overview-heading h2{font-size:21px}html[data-theme] .overview-stats{padding:7px 0}html[data-theme] .overview-stat{padding:7px 12px}html[data-theme] .overview-stat:nth-child(2){border-right:0}html[data-theme] .overview-stat:nth-child(n+3){border-top:1px solid var(--line)}html[data-theme] .panel>section,html[data-theme] .panel .col>section,html[data-theme] .overview-layout>section{padding:15px 14px}html[data-theme] .today-status{align-items:flex-start;padding:14px}html[data-theme] .opensource-promo{align-items:flex-start}html[data-theme] .opensource-promo a{margin-left:44px}}
/* A calm, monochrome workspace with clear page separation. */
html[data-theme]{color-scheme:light;--brand:#171717;--brand-dark:#333;--brand-soft:#f5f5f5;--brand-line:#dedede;--brand2:#333;--soft:#f5f5f5;--softline:#dedede;--ink:#171717;--ink2:#404040;--muted:#737373;--line:#e5e5e5;--line2:#f7f7f7;--bg:#fff;--card:#fff;--side:#fff;--side2:#f5f5f5;--sideink:#404040;--ok:#167344;--ok-bg:#eef7f1;--okbg:#eef7f1;--ok-line:#c7e6d1;--okline:#c7e6d1;--warn:#805700;--warn-bg:#fbf5e8;--warnbg:#fbf5e8;--warn-line:#ead6a8;--warnline:#ead6a8;--bad:#b4232f;--bad-bg:#fff1f1;--badbg:#fff1f1;--bad-line:#f0c4c7;--badline:#f0c4c7;--neu:#525252;--neu-bg:#f4f4f4;--neu-line:#e5e5e5}
html{scroll-padding-top:78px;scroll-behavior:smooth}
html[data-theme] body{background:#fff;color:#171717;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,"PingFang SC","Microsoft YaHei",sans-serif;font-size:14px;line-height:1.55;-webkit-text-size-adjust:100%;font-variant-numeric:tabular-nums}
html[data-theme] .app{grid-template-columns:244px minmax(0,1fr);min-height:100vh;background:#fff}
html[data-theme] .side{position:sticky;top:0;display:flex;flex-direction:column;gap:18px;height:100vh;min-height:100vh;padding:24px 16px;background:#fff;border-right:1px solid #e8e8e8;box-shadow:none}
html[data-theme] .brand{gap:11px;padding:2px 9px 19px;border-bottom:1px solid #ededed}
html[data-theme] .brand .logo{width:34px;height:34px;border-radius:10px;filter:grayscale(1);box-shadow:none}
html[data-theme] .brand b{color:#171717;font-size:14px;letter-spacing:.1px}html[data-theme] .brand i{margin-top:2px;color:#737373;font-size:11px}
html[data-theme] nav{display:flex;flex-direction:column;gap:5px}
html[data-theme] .nav{display:flex;align-items:center;gap:10px;min-height:43px;padding:9px 11px;border:1px solid transparent;border-radius:8px;background:transparent;color:#525252;font-size:13px;text-align:left;transition:background-color .16s,border-color .16s,color .16s}
html[data-theme] .nav:hover:not(:disabled){background:#f7f7f7;color:#171717}html[data-theme] .nav.on{background:#f1f1f1;border-color:#e5e5e5;color:#171717;box-shadow:none;font-weight:700}html[data-theme] .nav.on .ni{background:#171717}
html[data-theme] .side-foot{display:flex;flex-direction:column;align-items:flex-start;gap:7px;margin-top:auto;padding:14px 9px 0;border-top:1px solid #ededed}
html[data-theme] .side-foot .who{max-width:100%;color:#171717;font-size:12px;font-weight:600;overflow-wrap:anywhere}
html[data-theme] .side-foot a{padding:3px 0;color:#737373;font-size:12px;text-decoration:none}html[data-theme] .side-foot a:hover{color:#171717;text-decoration:underline}
html[data-theme] main.main{width:100%;max-width:1500px;margin:0 auto;padding:26px clamp(22px,4vw,58px) 60px}
html[data-theme] .top{position:sticky;top:0;z-index:120;display:flex;align-items:center;min-height:60px;gap:12px;margin:-26px calc(-1 * clamp(22px,4vw,58px)) 26px;padding:10px clamp(22px,4vw,58px);background:#fff;border:0;border-bottom:1px solid #ededed;backdrop-filter:none}
html[data-theme] .top h1{margin:0;font-size:20px;letter-spacing:-.35px;text-wrap:balance}html[data-theme] .head-mid{color:#737373;font-size:12px}html[data-theme] .head-mid b{color:#171717}
html[data-theme] #main-content{scroll-margin-top:72px}
html[data-theme] .panel{margin:0;padding:0;border:0;border-radius:0;background:transparent;box-shadow:none}
html[data-theme] .panel>section,html[data-theme] .panel .col>section{margin:0 0 14px;padding:20px;border:1px solid #e5e5e5;border-radius:12px;background:#fff;box-shadow:none}
html[data-theme] .panel h2{margin-top:0;color:#171717;font-size:15px;letter-spacing:-.15px;text-wrap:balance}
html[data-theme] .view-heading,.overview-heading{margin:0 0 17px}.view-heading h2,.overview-heading h2{margin:0;font-size:24px;letter-spacing:-.65px}.view-heading p,.overview-heading p{margin:5px 0 0;color:#737373;font-size:13px}.view-heading .eyebrow,.overview-heading .eyebrow{margin:0 0 5px;color:#737373;font-size:11px;font-weight:700;letter-spacing:.08em;text-transform:uppercase}.task-view-heading{display:none}#p-accounts.task-mode .account-view-heading{display:none}#p-accounts.task-mode .task-view-heading{display:block}
html[data-theme] .today-status{display:flex;align-items:center;gap:14px;min-height:86px;margin:0 0 17px;padding:18px 20px;border:1px solid #e5e5e5;border-left:3px solid #171717;border-radius:11px;background:#fff;box-shadow:none}
html[data-theme] .today-mark{display:grid;place-items:center;width:34px;height:34px;border-radius:9px;background:#171717;color:#fff;font-size:17px;flex:none}
html[data-theme] .today-copy{display:flex;min-width:0;flex:1;flex-direction:column;gap:3px}.today-copy b{color:#171717;font-size:16px;text-wrap:balance}.today-copy span{color:#737373;font-size:12px;overflow-wrap:anywhere}
html[data-theme] .overview-shortcuts{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}
html[data-theme] .overview-shortcuts button{display:grid;grid-template-columns:34px minmax(0,1fr) 16px;align-items:center;gap:11px;min-height:100px;padding:16px;border:1px solid #e5e5e5;border-radius:11px;background:#fff;color:#171717;text-align:left;transition:background-color .16s,border-color .16s,transform .16s}
html[data-theme] .overview-shortcuts button:hover{transform:translateY(-1px);border-color:#bdbdbd;background:#fafafa}
html[data-theme] .shortcut-icon{display:grid;place-items:center;width:32px;height:32px;border-radius:8px;background:#f1f1f1;color:#404040;font-size:11px;font-weight:700}
html[data-theme] .overview-shortcuts button>span:nth-child(2){display:flex;min-width:0;flex-direction:column;gap:4px}.overview-shortcuts b{font-size:13px}.overview-shortcuts small{color:#737373;font-size:11.5px;font-weight:400;line-height:1.45}
html[data-theme] #p-accounts:not(.task-mode) .cols{display:block}html[data-theme] #p-accounts:not(.task-mode) .cols>.col:first-child{display:flex;flex-direction:column;gap:14px}html[data-theme] #p-accounts:not(.task-mode) .cols>.col:last-child{display:none}
html[data-theme] #p-accounts.task-mode .cols{display:block}html[data-theme] #p-accounts.task-mode .cols>.col{display:contents}html[data-theme] #p-accounts.task-mode #authbox,html[data-theme] #p-accounts.task-mode #statusbox{display:none}html[data-theme] #p-accounts.task-mode #acctbox{margin-bottom:14px;padding:11px 15px}html[data-theme] #p-accounts.task-mode #acctbox h2,html[data-theme] #p-accounts.task-mode #acctbox .row.space,html[data-theme] #p-accounts.task-mode #acctbox .grid{display:none}
html[data-theme] #guide,html[data-theme] #themebtn,html[data-theme] #mobileapp,html[data-theme] #opensource-promo{display:none!important}html[data-theme] details.adv{display:none!important}
html[data-theme] #statusbox .facts>div:not(:last-child){display:none}html[data-theme] #statusbox .row:has(#bcopycookie){display:none}
html[data-theme] .frp{margin:9px 0 6px;padding:12px;border:1px solid #e5e5e5;border-radius:11px;background:#fafafa}
html[data-theme] .frp button{min-height:34px;padding:7px 13px;background:#fff;color:#171717;border:1px solid #d4d4d4}
html[data-theme] .frp button:hover:not(:disabled){background:#f1f1f1;border-color:#bdbdbd}
html[data-theme] .frp .cnt{display:inline-block;margin:0 0 0 10px;vertical-align:middle}
html[data-theme] .frp .cnt.frp-err{color:#b3261e}
html[data-theme] .frp-list{display:grid;grid-template-columns:repeat(auto-fill,minmax(158px,1fr));gap:6px;margin-top:11px;max-height:264px;overflow:auto}
html[data-theme] .frp-item{display:flex;align-items:center;gap:8px;padding:7px 9px;border:1px solid #ececec;border-radius:8px;background:#fff;font-size:12.5px;color:#171717;cursor:pointer}
html[data-theme] .frp-item:hover{border-color:#bdbdbd}
html[data-theme] .frp-item.on{background:#f1f1f1;border-color:#cfcfcf}
html[data-theme] .frp-item span{min-width:0;overflow-wrap:anywhere}
html[data-theme] .frp-item input[type=checkbox]{flex:none;width:16px;height:16px;min-height:0;margin:0;padding:0;border:1px solid #cfcfcf;border-radius:4px;background:#fff;accent-color:#171717}
html[data-theme] .authtabs{gap:5px;padding:5px;border:1px solid #e5e5e5;border-radius:9px;background:#f7f7f7}
html[data-theme] button{min-height:40px;border:1px solid #171717;border-radius:8px;background:#171717;color:#fff;font:inherit;font-weight:600;box-shadow:none;touch-action:manipulation;transition:background-color .16s,border-color .16s,color .16s,transform .16s}
html[data-theme] button:hover:not(:disabled){background:#333;border-color:#333}html[data-theme] button:active:not(:disabled){transform:translateY(1px)}html[data-theme] button.sm{min-height:34px;padding:7px 11px}
html[data-theme] button.ghost,html[data-theme] button.sec{border-color:#dedede;background:#fff;color:#262626}html[data-theme] button.ghost:hover:not(:disabled),html[data-theme] button.sec:hover:not(:disabled){border-color:#bdbdbd;background:#f7f7f7;color:#111}
html[data-theme] button.danger,html[data-theme] button.danger-ghost,html[data-theme] button.dghost{border-color:#b4232f;background:#fff;color:#a82034}html[data-theme] button.danger:hover:not(:disabled),html[data-theme] button.danger-ghost:hover:not(:disabled),html[data-theme] button.dghost:hover:not(:disabled){background:#fff1f1}
html[data-theme] button.authtab{min-height:36px;border:1px solid transparent;background:transparent;color:#404040}html[data-theme] button.authtab.on{border-color:#171717;background:#171717;color:#fff}
html[data-theme] input,html[data-theme] textarea,html[data-theme] select{min-height:40px;border:1px solid #dedede;border-radius:8px;background:#fff;color:#171717;font:inherit;touch-action:manipulation}html[data-theme] input:focus,html[data-theme] textarea:focus,html[data-theme] select:focus{outline:3px solid #e8e8e8;outline-offset:1px;border-color:#888}html[data-theme] select{color-scheme:light}
html[data-theme] label{color:#404040}html[data-theme] .muted,html[data-theme] .hint,html[data-theme] .sub{color:#737373}
html[data-theme] .badge,html[data-theme] .chip{border-radius:7px;font-variant-numeric:tabular-nums}
html[data-theme] .facts>div{min-width:0;border-color:#ededed;background:#fafafa}html[data-theme] .kv,.account-menu,.overview-shortcuts{min-width:0}html[data-theme] .kv,html[data-theme] td,html[data-theme] pre{overflow-wrap:anywhere;word-break:break-word}
html[data-theme] .tblwrap,html[data-theme] .reclist{max-width:100%;overscroll-behavior:contain}html[data-theme] #shotview,html[data-theme] #authwiz{overscroll-behavior:contain}html[data-theme] #shot{width:100%;height:auto;object-fit:contain}
html[data-theme] .account-menu>summary{min-height:38px;border:1px solid #dedede;border-radius:8px;background:#fff;color:#404040}html[data-theme] .account-menu>summary:hover,html[data-theme] .account-menu[open]>summary{background:#f7f7f7;color:#171717}
html[data-theme] .account-panel{border-color:#e5e5e5;border-radius:10px;background:#fff;box-shadow:0 14px 36px rgba(0,0,0,.12)}html[data-theme] .account-panel a{min-height:40px;color:#404040}html[data-theme] .account-panel a:hover{background:#f5f5f5;color:#171717}
html[data-theme] #flash{z-index:200}html[data-theme] #flash .flash{border-radius:9px;box-shadow:0 8px 24px rgba(0,0,0,.1)}
html[data-theme] a{color:#262626}html[data-theme] a:hover{color:#000}html[data-theme] :focus-visible{outline:3px solid #777!important;outline-offset:3px!important}
html[data-theme] h1,html[data-theme] h2,html[data-theme] h3{scroll-margin-top:76px}
.skip-link{position:fixed;top:8px;left:8px;z-index:500;transform:translateY(-160%);padding:9px 12px;border-radius:7px;background:#171717;color:#fff}.skip-link:focus{transform:translateY(0)}
@media(max-width:900px){html[data-theme] .app{display:block}html[data-theme] .side{position:fixed;inset:auto 0 0;z-index:150;display:flex;flex-direction:column;gap:4px;width:100%;height:auto;min-height:0;padding:5px 8px calc(7px + env(safe-area-inset-bottom));border:0;border-top:1px solid #e8e8e8;background:#fff;box-shadow:0 -5px 18px rgba(0,0,0,.04)}html[data-theme] .brand{display:none}html[data-theme] nav{height:54px;flex-direction:row;gap:3px;overflow-x:auto;overscroll-behavior-x:contain;scrollbar-width:none}html[data-theme] nav::-webkit-scrollbar{display:none}html[data-theme] .nav{display:flex;flex:1 0 62px;flex-direction:column;justify-content:center;gap:3px;min-width:62px;min-height:49px;padding:4px 3px;border-radius:7px;font-size:10px;line-height:1.1;text-align:center;white-space:nowrap}html[data-theme] .side-foot{display:flex;flex-direction:row;justify-content:center;gap:20px;margin:0;padding:3px 0 0;border:0}html[data-theme] .side-foot .who{display:none}html[data-theme] .side-foot a{padding:3px 8px;font-size:11px}html[data-theme] main.main{padding:12px 18px calc(130px + env(safe-area-inset-bottom))}html[data-theme] .top{margin:-12px -18px 18px;padding:9px 18px}}
@media(max-width:640px){html[data-theme] .overview-shortcuts{grid-template-columns:1fr}html[data-theme] .overview-shortcuts button{min-height:72px;padding:13px}html[data-theme] .overview-heading h2,html[data-theme] .view-heading h2{font-size:22px}html[data-theme] .today-status{align-items:flex-start;flex-wrap:wrap;padding:14px}html[data-theme] .today-status>button{margin-left:48px}html[data-theme] .panel>section,html[data-theme] .panel .col>section{padding:15px 14px}html[data-theme] .top{gap:8px}html[data-theme] .head-mid{display:none}html[data-theme] input,html[data-theme] textarea,html[data-theme] select{font-size:16px}}
@media(prefers-reduced-motion:reduce){html{scroll-behavior:auto}html[data-theme] *,html[data-theme] *::before,html[data-theme] *::after{scroll-behavior:auto!important;animation-duration:.01ms!important;animation-iteration-count:1!important;transition-duration:.01ms!important}}
</style></head><body>
<a class="skip-link" href="#main-content">跳到主要内容</a>
<div class="app">
<aside class="side">
<div class="brand"><span class="logo"></span><div><b>DouYinSparkFlow</b><i>续火花控制台</i></div></div>
<nav id="nav" aria-label="主菜单">
<button class="nav on" type="button" data-go="overview"><i class="ni ni-overview" aria-hidden="true"></i>概览</button>
<button class="nav" type="button" data-go="accounts"><i class="ni ni-accounts" aria-hidden="true"></i>抖音账户</button>
<button class="nav" type="button" data-go="tasks"><i class="ni ni-system" aria-hidden="true"></i>任务配置</button>
<button class="nav" type="button" data-go="records"><i class="ni ni-records" aria-hidden="true"></i>发送记录</button>
<button class="nav" type="button" data-go="me"><i class="ni ni-me" aria-hidden="true"></i>我的账号</button>
<button class="nav" type="button" data-go="admin" id="navadmin" hidden><i class="ni ni-admin" aria-hidden="true"></i>管理</button>
</nav>
<div class="side-foot">
<span class="who" id="whoami">—</span>
<a href="https://github.com/BARONCMH/DouYinSparkFlow-OpenSource" target="_blank" rel="noopener noreferrer">GitHub 仓库 ↗</a>
<a href="/logout">切换账号</a>
</div>
</aside>
<main class="main" id="main-content" tabindex="-1">
<div class="top">
<h1 id="pageTitle">概览</h1>
<span class="sp"></span>
<span class="head-mid"><b id="headacct">—</b><span id="headbadge" class="badge n" role="status" aria-live="polite">读取中…</span></span>
<button id="hbstart" class="sm" type="button" aria-label="开始授权登录">开始授权</button>
<button id="themebtn" class="ghost sm" type="button" aria-label="切换主题"><i class="ni ni-flame"></i><span class="theme-label"></span></button>
<details class="account-menu" id="accountMenu">
  <summary aria-label="打开账号菜单" aria-expanded="false"><span>账号</span><span aria-hidden="true">⌄</span></summary>
  <div class="account-panel">
    <div class="account-id"><b id="accountWho">—</b><span id="accountRole">普通用户</span></div>
    <a id="accountAdminLink" href="/admin">管理员登录</a>
    <a class="account-exit" href="/logout">切换账号 / 退出登录</a>
  </div>
</details>
</div>
<div id="flash" role="status" aria-live="polite"></div>
<div id="notice" hidden>
<div class="n-head"><b>公告</b><span class="sp"></span><button id="notice_x" class="ghost sm" type="button" aria-label="关闭公告">知道了</button></div>
<div class="n-body" id="notice_text" hidden></div>
<div class="n-contact" id="notice_contact" hidden></div>
<div class="n-foot" id="notice_foot" hidden></div>
</div>
<div id="msgs" hidden></div>
<div id="relogin"></div>

<!-- ===== 概览 ===== -->
<section class="panel on" id="p-overview" aria-labelledby="overview-title">
<div class="overview-heading"><p class="eyebrow">今日状态</p><h2 id="overview-title">今天的发送情况</h2><p>查看任务结果，或直接进入账号与发送配置。</p></div>
<section class="today-status" aria-live="polite" aria-label="今天的发送状态">
  <span class="today-mark" aria-hidden="true">✦</span>
  <div class="today-copy"><b id="todaySendState">正在读取今天的发送状态…</b><span id="todaySendMeta">发送结果会自动更新</span></div>
  <button class="ghost sm" type="button" data-go="records">查看发送记录</button>
</section>
<div class="overview-shortcuts" aria-label="常用操作">
  <button type="button" data-go="accounts"><span class="shortcut-icon" aria-hidden="true">01</span><span><b>抖音账户配置</b><small>授权登录并查看登录状态</small></span><span aria-hidden="true">→</span></button>
  <button type="button" data-go="tasks"><span class="shortcut-icon" aria-hidden="true">02</span><span><b>任务配置</b><small>设置好友、发送时间与消息</small></span><span aria-hidden="true">→</span></button>
  <button type="button" data-go="records"><span class="shortcut-icon" aria-hidden="true">03</span><span><b>发送记录</b><small>查看最近任务结果</small></span><span aria-hidden="true">→</span></button>
</div>
</section>

<!-- ===== 抖音账户 ===== -->
<section class="panel" id="p-accounts">
<div class="view-heading account-view-heading"><p class="eyebrow">账号</p><h2>抖音账户配置</h2><p>管理账号授权与登录状态。</p></div>
<div class="view-heading task-view-heading"><p class="eyebrow">任务</p><h2>任务配置</h2><p>选择抖音号，设置目标好友和发送时间。</p></div>
<section id="guide" aria-label="新手引导">
<div class="g-head"><b>新手引导</b>
<span class="sp"></span><button id="gtoggle" class="ghost sm" type="button" aria-label="收起或展开新手引导">收起</button></div>
<ol id="glist">
<li id="g1" data-goto="acctbox"><span class="gdot">1</span><span class="gtxt"><b>建一个账号</b><span>只填一个「抖音号」当名字（例如 myspark），别的都能先空着</span></span><button class="sm gact" id="gact1" type="button">去填写</button></li>
<li id="g2" data-goto="authbox"><span class="gdot">2</span><span class="gtxt"><b>授权登录</b><span>选择一种方式完成登录</span></span><button class="sm gact" id="gact2" type="button">去授权</button></li>
<li id="g3" data-goto="statusbox"><span class="gdot">3</span><span class="gtxt"><b>确认登录成功</b><span>点「检测登录状态」，确认 Cookie 还能看到好友列表</span></span><button class="sm gact" id="gact3" type="button">去检测</button></li>
<li id="g4" data-goto="cfgbox"><span class="gdot">4</span><span class="gtxt"><b>填目标好友</b><span>每行一个好友昵称；每行填写一个好友，运行时发送</span></span><button class="sm gact" id="gact4" type="button">去填写</button></li>
<li id="g5" data-goto="runbox"><span class="gdot">5</span><span class="gtxt"><b>跑一次看看</b><span>点击运行开始发送，并查看本次结果</span></span><button class="sm gact" id="gact5" type="button">去运行</button></li>
</ol>
</section>

<div class="cols">
<div class="col">

<section id="acctbox"><h2><span class="step">1</span>账号</h2>
<div id="acctabs"></div>
<div class="row space">
<button type="button" id="baddacct" class="sec">＋ 新增账号</button>
<button type="button" id="bdelacct" class="danger-ghost push" aria-label="删除当前账号">删除当前账号</button>
</div>
<div class="grid" style="margin-top:12px">
<div><label for="f_uid">抖音号 <span class="hint">自己起个标识就行，例如 myspark</span></label>
<input id="f_uid" name="unique_id" form="cfg" required pattern="[A-Za-z0-9_-]{1,40}" maxlength="40" title="1~40 位，只能用字母、数字、下划线 _ 或短横线 -" placeholder="例如 myspark" autocomplete="off"></div>
<div><label for="f_uname">账号名称 <span class="hint">可以不填，默认用抖音号</span></label>
<input id="f_uname" name="username" form="cfg" maxlength="40" placeholder="例如 小明的小号" autocomplete="off"></div>
</div>
</section>

<section id="authbox"><h2><span class="step">2</span>授权登录</h2>
<div class="authtabs" role="tablist" aria-label="选择登录方式">
<button class="authtab on" id="tabqr" type="button" role="tab" aria-selected="true" aria-label="二维码登录，推荐方式">二维码登录 <span class="auth-rec-badge">推荐</span></button>
<button class="authtab" id="tabmanual" type="button" role="tab" aria-selected="false">手动授权</button>
<button class="authtab" id="tabcookie" type="button" role="tab" aria-selected="false">备用导入</button>
</div>
<div id="browserauthpane">
<div id="manualauthnotice" class="auth-recommend" role="note" hidden>
<span class="ar-icon" aria-hidden="true">✓</span>
<div><strong>手动授权备用方式</strong><p>如果二维码登录无法继续，可在下方画面中直接操作抖音登录页。登录成功后会自动加密保存并检查状态，无需复制 Cookie。</p></div>
</div>
<div class="row">
<button id="bstart" type="button">开始授权</button>
<button id="bauthrestart" class="sec" type="button" title="关闭当前授权并重新获取二维码">重启授权</button>
<button id="bstop" class="danger" type="button">停止</button>
<button class="sm sec" id="bshot" type="button" aria-label="显示或收起浏览器画面">显示画面</button>
<span class="muted" id="startnow"></span>
</div>
<p id="starterr" role="alert"></p>
<p id="authstate" class="muted">尚未启动</p>
<p class="muted" id="qrowner" hidden><span id="qrownertext"></span><button class="sm sec" id="bswitchwho" type="button" hidden>切到这个账号</button></p>
<p class="muted" id="qrdiag" role="status"></p>
<div id="qrarea" hidden style="margin-top:14px">
<div class="row" style="align-items:flex-start">
<img id="qrimg" alt="抖音登录二维码：用手机抖音 App 扫一扫" width="220" height="220" style="border:1px solid #e5e7eb;border-radius:10px;background:#fff;flex:0 0 auto">
</div>
</div>
<div id="verifyqr" hidden style="margin-top:14px;padding:14px;border:1px dashed #94a3b8;border-radius:12px;text-align:center">
<p class="muted" id="verifyhint" role="status" style="margin:0 0 10px"></p>
<img id="verifyqrimg" alt="抖音二级验证二维码：用已登录的抖音 App 扫一扫" width="220" height="220" style="background:#fff;border:1px solid #e5e7eb;border-radius:10px">
</div>
<div id="shotwrap" hidden>
<div id="manualscreentip" class="manual-screen-tip" role="note" hidden><span class="tapmark" aria-hidden="true">👆</span><p><strong>下方画面可以直接点击</strong><span>点画面里的输入框、登录或验证按钮，就能直接操作抖音页面。</span></p></div>
<div class="row"><div id="shotframe"><img id="shot" alt="点击这里操作抖音页面" hidden><span class="livetag" id="livetag" hidden></span></div></div>
<p class="muted" id="shotempty" hidden>当前没有正在运行的浏览器画面：点「开始授权」或「检测登录状态」后，这里会实时显示。</p>
<div id="anybox">
<div class="row">
<input id="anytext" type="text" autocomplete="off" maxlength="200" placeholder="在这里打要粘给抖音的内容" aria-label="通用输入框" style="flex:1 1 auto;min-width:160px">
<button class="sm sec" id="anyclear" type="button">清空</button>
<button class="sm" id="anygo" type="button">提交</button>
</div>
<p id="anyresult" role="status" hidden></p>
</div>
<div class="row">
<button class="sm sec" data-wheel="-400" type="button" aria-label="画面向上滚动">向上滚动</button>
<button class="sm sec" data-wheel="400" type="button" aria-label="画面向下滚动">向下滚动</button>
<button class="sm sec" data-press="Enter" type="button" aria-label="在抖音页面按回车">回车</button>
<button class="sm sec" data-press="Tab" type="button" aria-label="在抖音页面按 Tab 键">Tab</button>
<button class="sm sec" data-press="Backspace" type="button" aria-label="在抖音页面按退格键">退格</button>
<button class="sm sec" data-goto="1" type="button" aria-label="回到抖音聊天页">回到聊天页</button>
</div>
</div>
</div>
<div id="cookieauthpane" hidden>
<label for="f_ck">从 Cookie 工具导入（备用方式）</label>
<textarea id="f_ck" name="cookie_json" form="cfg" placeholder="仅在自动授权失败时，粘贴工具导出的 Cookie JSON" autocomplete="off" spellcheck="false"></textarea>
<div class="row">
<button type="submit" form="cfg" id="bcookieimport">保存并导入 Cookie</button>
<a class="auth-download" href="/downloads/Get-Douyin-Cookies.exe" download>下载 Cookie 获取工具（EXE）</a>
<a class="auth-download" href="/downloads/Get-Douyin-Cookies.exe.sha256" download>查看 SHA-256</a>
</div>
<p class="muted">Cookie 等同登录凭证，请只导入自己的账号。通常无需使用此备用方式。</p>
</div>
</section>

<section id="statusbox"><h2><span class="step">3</span>登录状态</h2>
<div id="badge" class="badge n">正在读取…</div>
<div class="facts" style="margin-top:12px">
<div><div class="muted">账号</div><div class="kv" id="st_account">—</div></div>
<div><div class="muted">抖音号</div><div class="kv" id="st_uid">—</div></div>
<div><div class="muted">目标好友</div><div class="kv" id="st_targets">—</div></div>
<div><div class="muted">Cookie</div><div class="kv" id="st_cookie">—</div></div>
<div><div class="muted">登录成功时间</div><div class="kv" id="st_saved">—</div></div>
</div>
<div class="row"><button class="sm" id="bcopycookie" type="button">复制该抖音号 Cookie</button><span class="muted" id="copystate" role="status"></span></div>
<div class="row"><button id="bcheck" type="button">检测登录状态</button><span class="muted" id="checkstate" role="status"></span></div>
<div class="progress" id="checkbar" hidden><i id="checkfill"></i></div>
<p class="muted" id="checktime" role="status" hidden></p>
<div id="checkresult" class="muted"></div>
</section>

</div>
<div class="col">

<section id="cfgbox"><h2><span class="step">4</span>发送配置</h2>
<form id="cfg">
<input type="hidden" name="orig_unique_id">
<label for="f_targets">目标好友 <span class="hint">每行一个，支持备注 / 昵称 / 抖音号；可以先空着，登录成功后再填</span></label>
<textarea id="f_targets" name="targets" placeholder="每行写一个好友，例如：小明、老王" autocomplete="off"></textarea>
<p class="cnt" id="cnt_targets">已填 0 个好友</p>
<div class="frp" id="frpBox">
<button type="button" class="sm" id="frpLoad">拉取好友</button>
<span class="cnt" id="frpState" role="status">点「拉取好友」获取这个号的好友，勾选后自动填入上方</span>
<div class="frp-list" id="frpList" hidden></div>
</div>
<label for="f_times">每天发送时间 <span class="hint">每行一个：09:00 固定；09:00-11:00 随机；09:00±30 前后随机</span></label>
<textarea id="f_times" name="schedule_times" placeholder="09:00" style="min-height:64px" autocomplete="off"></textarea>
<div class="facts" style="margin-top:8px">
<div><div class="muted">预计完成时间</div><div class="kv" id="schedule_estimate">填写目标好友后计算</div></div>
<div><div class="muted">推荐空闲时间</div><div class="kv" id="schedule_recommended">—</div></div>
</div>
<p class="cnt" id="schedule_occupied">正在读取其他账号的发送时间…</p>
<p class="cnt" id="schedule_hint" role="status"></p>
<label for="f_msg">消息模板 <span class="hint">只对当前这个抖音号生效；换行直接按回车，[API] 会替换成每日一句</span></label>
<textarea id="f_msg" name="message_template" autocomplete="off"></textarea>
<details class="adv">
<summary>高级设置（发送间隔 / 时区）</summary>
<div class="advbody">
<p class="muted" id="gnote">发送间隔 / 一言类型只对当前账号生效；时区 / 日志级别所有账号共用，只有管理员能改。</p>
<div class="grid">
<div><label for="f_dmin">发送间隔下限 <span class="hint">秒，0=不等，范围 0~600</span></label><input id="f_dmin" name="delay_min" type="number" min="0" max="600" step="1" placeholder="0"></div>
<div><label for="f_dmax">发送间隔上限 <span class="hint">秒，0=不等，范围 0~600</span></label><input id="f_dmax" name="delay_max" type="number" min="0" max="600" step="1" placeholder="0"></div>
</div>
<div class="grid">
<div><label for="f_tz">时区 <span class="hint">所有账号共用，仅管理员可改</span></label><input id="f_tz" name="tz" placeholder="Asia/Shanghai" autocomplete="off"></div>
<div><label for="f_ll">日志级别 <span class="hint">所有账号共用，仅管理员可改</span></label><select id="f_ll" name="log_level">
<option>DEBUG</option><option>INFO</option><option>WARNING</option><option>ERROR</option>
</select></div>
</div>
<label for="f_hito">一言类型 <span class="hint">JSON 数组，不填就用默认；只对当前这个号生效</span></label><input id="f_hito" name="hitokoto_types" placeholder="留空即可" autocomplete="off">
<div class="checkline" id="gglobalwrap" hidden>
<input type="checkbox" id="f_global">
<label for="f_global">顺便把「消息模板 / 发送间隔 / 一言类型」也设为<b>新账号的默认值</b> <span class="muted">（不勾就只改当前这个号；这一项只有管理员看得到）</span></label>
</div>
</div>
</details>
<div class="row"><button type="submit" id="bsubmit">保存配置</button>
<span class="muted" id="cfgstate" role="status"></span></div>
</form></section>

<section id="runbox"><h2><span class="step">5</span>立即运行</h2>
<div class="row"><button id="brun" type="button">立即运行一次</button><span class="muted" id="runstate" role="status"></span></div></section>

</div><!-- /col -->
</div><!-- /cols -->
</section><!-- /p-accounts -->

<!-- ===== 发送记录 ===== -->
<section class="panel" id="p-records">
<section id="sendsbox"><h2>发送记录 <span class="muted" id="sendssum"></span></h2>
<div id="sends" class="muted">加载中…</div>
<p class="muted" id="sendnote">每个好友的成败都有记录和截图，点缩略图放大。这里只显示最近 2 次。</p>
<p class="muted" id="sendmore" hidden><a href="/admin#records">去管理控制台看最近 100 条完整记录 →</a></p>
</section>
</section><!-- /p-records -->

<!-- ===== 我的账号 ===== -->
<section class="panel" id="p-me">
<section id="meinfo">
<h2>账号信息</h2>
<div class="facts">
<div><div class="muted">登录名</div><div class="kv" id="me_name">—</div></div>
<div><div class="muted">角色</div><div class="kv" id="me_role">—</div></div>
<div><div class="muted">名下的抖音号</div><div class="kv" id="me_ids">—</div></div>
<div><div class="muted">登录状态</div><div class="kv" id="me_badge">—</div></div>
</div>
</section>

<section id="subscriptionbox">
<h2>时长服务</h2>
<p class="muted">查看账户剩余时长，或兑换时长码。</p>
<div class="facts" style="margin-top:12px">
<div><div class="muted">当前状态</div><div class="kv" id="sub_status">读取中…</div></div>
<div><div class="muted">剩余时长</div><div class="kv" id="sub_remaining">—</div></div>
<div><div class="muted">到期时间</div><div class="kv" id="sub_expires">—</div></div>
 </div>
<div class="row" style="margin-top:14px">
<input id="sub_code" maxlength="19" autocomplete="off" placeholder="例如 DSF-XXXX-XXXX-XXXX…" aria-label="兑换码" inputmode="text" spellcheck="false">
<button id="sub_redeem" type="button">兑换时长</button>
 </div>
<p class="muted" id="sub_result" role="status" aria-live="polite"></p>
</section>


<section id="mebox" hidden>
<h2>修改登录密码</h2>
<p class="muted" id="mehint"></p>
<div class="grid3">
<div><label for="mp_old">现在的密码</label><input id="mp_old" type="password" autocomplete="current-password"></div>
<div><label for="mp_new">新密码 <span class="hint">至少 6 位</span></label><input id="mp_new" type="password" minlength="6" title="至少 6 位" autocomplete="new-password"></div>
<div><label for="mp_new2">再输一次新密码</label><input id="mp_new2" type="password" minlength="6" title="至少 6 位" autocomplete="new-password"></div>
</div>
<div class="row"><button id="mp_save" type="button">修改我的登录密码</button><span class="muted" id="mp_state" role="status"></span></div>
</section>
</section><!-- /p-me -->

<!-- ===== 管理（仅管理员可见）===== -->
<section class="panel" id="p-admin">
<section id="adminbox" hidden>
<h2>用户管理</h2>
<p class="muted">所有注册用户、绑定的抖音号、登录状态和剩余时长；可以改密码、分配抖音号、删用户。</p>
<div id="userlist" class="muted">加载中…</div>
<div class="checkline">
<input type="checkbox" id="regenable">
<label for="regenable">允许别人自己点「注册一个」建账号 <span class="muted">（关掉后只能由你在下面手动建号）</span></label>
</div>
<div class="grid3" style="margin-top:12px">
<div><label for="nu_name">给朋友开个账号：登录名 <span class="hint">2~32 位</span></label><input id="nu_name" placeholder="例如 xiaoming" minlength="2" maxlength="32" title="2~32 位" autocomplete="off"></div>
<div><label for="nu_pw">初始密码 <span class="hint">至少 6 位</span></label><input id="nu_pw" type="password" placeholder="例如 Spark123456" minlength="6" title="至少 6 位" autocomplete="new-password"></div>
<div><label>&nbsp;</label><button id="nu_btn" type="button">创建账号</button></div>
</div>
<p class="muted" id="nu_state" role="status"></p>
<p class="muted" style="margin-top:14px">改我（管理员）的登录密码</p>
<div class="grid3" style="margin-top:8px">
<div><label for="adm_old">现在的密码</label><input type="password" id="adm_old" autocomplete="current-password"></div>
<div><label for="adm_new">新密码 <span class="hint">至少 6 位</span></label><input type="password" id="adm_new" minlength="6" title="至少 6 位" autocomplete="new-password"></div>
<div><label for="adm_again">再输一次新密码</label><input type="password" id="adm_again" minlength="6" title="至少 6 位" autocomplete="new-password"></div>
</div>
<div class="row" style="margin-top:8px"><button id="adm_btn" type="button">改管理员密码</button><span class="muted" id="adm_state" role="status"></span></div>
<div class="grid3" style="margin-top:12px">
<div><label for="ub_uid">把抖音号分配给某个用户</label><input id="ub_uid" placeholder="抖音号，例如 dingdingya1216" autocomplete="off"></div>
<div><label for="ub_user">给谁</label><select id="ub_user"></select></div>
<div><label>&nbsp;</label><button id="ub_save" type="button">分配</button></div>
</div>
<p class="muted" id="ub_state" role="status">抖音号要先在左边建出来才能分配；没分配的号只有管理员看得到。</p>
<hr>
<h2>时长与兑换码</h2>
<div class="grid3">
<div><label for="redeem_days">兑换码时长</label><select id="redeem_days"><option value="7">一星期</option><option value="14">两星期</option><option value="30">一个月</option><option value="60">两个月</option></select></div>
<div><label for="redeem_count">生成数量</label><input id="redeem_count" type="number" min="1" max="100" value="1"></div>
<div><label>&nbsp;</label><button id="redeem_generate" type="button">生成兑换码</button></div>
</div>
<p class="muted" id="redeem_generate_state" role="status"></p>
<pre id="redeem_generated" hidden style="margin-top:8px;max-height:180px"></pre>
<div class="grid3" style="margin-top:10px">
<div><label for="grant_user">直接授予用户</label><select id="grant_user"></select></div>
<div><label for="grant_days">授予时长</label><select id="grant_days"><option value="7">一星期</option><option value="14">两星期</option><option value="30">一个月</option><option value="60">两个月</option></select></div>
<div><label>&nbsp;</label><button id="grant_duration" type="button">授予时长</button></div>
</div>
<p class="muted" id="grant_state" role="status"></p>
<div id="redeem_history" class="muted" style="margin-top:10px">兑换码历史读取中…</div>
</section>

<section id="forcebox" hidden>
<h2>卡住了？紧急操作</h2>
<p class="muted">如果「停止」点了没反应（浏览器卡在加载页面时就会这样），用下面这两个。</p>
<div class="row">
<button id="bforce" class="danger" type="button">强制停止（结束卡住的浏览器和任务）</button>
<button id="brestart" class="danger-ghost" type="button">强制重启后端</button>
</div>
<p id="forcestate" class="muted" role="status"></p>
<p class="muted">「强制停止」只结束卡住的浏览器和发送任务，面板本身不动，登录状态和配置都保留；「强制重启后端」会把面板进程整个重新拉起，约 10 秒后自动恢复，页面会自己刷新。</p>
<div id="forcelog" class="muted"></div>
</section>
</section><!-- /p-admin -->
</main>
</div><!-- /app -->

<div id="shotview" hidden role="dialog" aria-modal="true" aria-label="查看截图大图">
<button id="shotviewclose" class="danger-ghost" type="button" aria-label="关闭截图窗口">关闭</button>
<img id="shotviewimg" alt="放大查看的发送截图">
<div id="shotviewtip"></div>
</div>
<script>
var $ = function(id){ return document.getElementById(id); };
// 每个普通用户最多能绑几个抖音号（由服务端注入，改后端常量这里跟着变）
var MAX_PER_USER = __MAX_ACCOUNTS__;
// 「还要等多久」说成人话（授权位被占满时用）
function fmtWait(sec){
  if(sec === null || sec === undefined){ return '一会儿'; }
  sec = Math.max(0, Math.round(sec));
  if(sec < 60){ return sec + ' 秒'; }
  return Math.floor(sec / 60) + ' 分 ' + (sec % 60) + ' 秒';
}
var FLASH_SEQ = 0, FLASH_MAX = 3;
function closeFlash(id){
  var el = $(id);
  if(el && el.parentNode && el.parentNode.removeChild){ el.parentNode.removeChild(el); }
}
// 提示做成一条条往下堆的列表：新结果不会把上一条顶掉，能同时看到「保存成功」和「检测失败」
function flash(text, ok){
  var box = $('flash');
  if(!box){ return; }
  if(!text){ box.innerHTML = ''; return; }
  FLASH_SEQ += 1;
  var id = 'f' + FLASH_SEQ;
  var item = document.createElement('div');
  item.id = id;
  item.className = 'flash ' + (ok ? 'ok' : 'bad');
  item.innerHTML = '<span>' + esc(text) + '</span>'
    + '<button class="close" type="button" data-flash="' + id + '" aria-label="关闭这条提示">×</button>';
  if(box.appendChild){ box.appendChild(item); }
  while(box.children && box.children.length > FLASH_MAX && box.removeChild){ box.removeChild(box.children[0]); }
  setTimeout(function(){ closeFlash(id); }, 8000);
}
(function(){
  var box = $('flash');
  if(box && box.addEventListener){
    box.addEventListener('click', function(e){
      var t = e.target;
      var id = (t && t.getAttribute) ? t.getAttribute('data-flash') : null;
      if(id){ closeFlash(id); }
    });
  }
})();
// 除了屏幕上的浮层提示，还把结果固定写在按钮下面：浮层会自己消失，很容易漏看
function note(text, ok){
  flash(text, ok);
  var box = $('starterr');
  if(box){
    box.style.color = ok ? '#166534' : '#b91c1c';
    box.textContent = text || '';
  }
}
// 后端出错分两类：一类是它自己回的 JSON（带 error 字段），
// 一类是网关/反代回的 HTML（502、504 之类，Playwright 挂掉时也常见）。
// 后者以前会 r.json() 抛错，被吞成 {}，界面上就只剩「保存失败」四个字，什么原因都看不出来。
function post(url, data){
  return fetch(url, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(data || {})})
    .then(function(r){
      return r.text().then(function(t){
        var body = null;
        try { body = JSON.parse(t); } catch(e){ body = null; }
        if(!body || typeof body !== 'object'){
          body = {ok: false, error: 'HTTP ' + r.status + '（后端没返回正常内容，可能正在重启）'};
        }
        if(body.httpStatus === undefined){ body.httpStatus = r.status; }
        return body;
      });
    })
    .catch(function(){
      return {ok: false, httpStatus: 0, error: '网络断了或后端没响应，稍后再试'};
    });
}
// ---- 按钮防抖：点一下先锁住，等服务端状态回来再校正 ----
function lockButton(btn){
  if(!btn){ return true; }
  var now = Date.now();
  if(btn._lockUntil && btn._lockUntil > now){ return false; }
  btn._lockUntil = now + 5000;
  btn.disabled = true;
  setTimeout(function(){
    btn._lockUntil = 0;
    // 解锁后按服务端状态校正一次，别把其实还不能点的按钮点亮
    if(typeof refresh === 'function'){ refresh(); } else { btn.disabled = false; }
  }, 5200);
  return true;
}
function btnLocked(id){
  var b = $(id);
  return !!(b && b._lockUntil && b._lockUntil > Date.now());
}
// 按钮灰不灰一律以服务端状态为准；正锁着的按钮先别动
function setBtn(id, disabled, title){
  var b = $(id);
  if(!b){ return; }
  b.disabled = !!disabled || btnLocked(id);
  if(title !== undefined){ b.title = title || ''; }
}
// ---- 两个多行输入框：随时告诉用户填了几条，不用等提交才发现少填了 ----
function countsOf(text){
  var n = 0;
  String(text || '').split(NL).forEach(function(line){
    line.split(/[,，;；]/).forEach(function(piece){ if(piece.trim()){ n += 1; } });
  });
  return n;
}
function updateCounts(){
  var f = $('cfg');
  if(!f){ return; }
  var tn = countsOf(f.targets ? f.targets.value : '');
  var ct = $('cnt_targets');
  if(ct){ ct.textContent = tn ? ('已填 ' + tn + ' 个好友') : '还没填好友（可以先授权登录，之后回来补）'; }
}
(function(){
  var f = $('cfg');
  if(!f){ return; }
  var el = f.targets;
  if(el && el.addEventListener){ el.addEventListener('input', updateCounts); }
})();
// ---- 多账号：每个账号一份配置，互不影响 ----
var ACCOUNTS = [], CUR_ACCT = null, GLOBALCFG = {}, FORM_UID = null, LAST_CHECKER = {}, ADDING = false;
var NL = String.fromCharCode(10);
// checks 里的记录可能是「扫码授权成功」留下的（带 source='qr'），那**不是**检测结果。
// 服务端已经过滤掉了，这里再挡一道，保证所有地方读到的语义一致。
function realCheck(c){
  return (c && c.source !== 'qr') ? c : null;
}
function acctStateInfo(a){
  // 千万别写成 `a.check || {}`。空对象是 truthy，`!undefined` 也是 true，
  // 于是"一次检测都没跑过"会被下面 `ck && !ck.ok` 判成"检测没过"，
  // 直接把账号标成红色的「未登录」——2026-09-26 线上就是这么把 9 个号全渲染成失效的。
  // 统一用 null 表示"没有检测结果"，走最后那条"待检测"分支。
  var ck = realCheck(a.check);
  if(LAST_CHECKER.running && (!LAST_CHECKER.unique_id || LAST_CHECKER.unique_id === a.unique_id)){ return {c:'y', t:'检测中'}; }
  // 执行任务途中程序自己发现的登录失效，比「检测」按钮的结果更新，必须优先显示
  if(a.task_login_failed){ return a.has_cookie ? {c:'r', t:'登录已失效'} : {c:'n', t:'未授权'}; }
  if(ck && ck.ok && !a.ready){ return {c:'y', t:'已登录（待填目标好友）'}; }
  if(ck && ck.ok){ return {c:'g', t:'已登录'}; }
  if(ck && !ck.ok){ return a.has_cookie ? {c:'r', t:'未登录'} : {c:'n', t:'未授权'}; }
  // 有 Cookie 但一次真检测都没跑过：说"待检测"，既不要用红色吓人，也不能报成绿色
  return a.has_cookie ? {c:'n', t:'待检测'} : {c:'n', t:'未授权'};
}
function acctState(a){
  return acctStateInfo(a).t;
}
function renderAcctTabs(){
  var box = $('acctabs');
  box.innerHTML = '';
  var del = $('bdelacct');
  if(del){ del.disabled = !CUR_ACCT; }
  if(!ACCOUNTS.length){
    box.innerHTML = '<div class="empty"><b>还没有账号</b>点右边的「＋ 新增账号」，只填一个「抖音号」就能开始。</div>';
    return;
  }
  if(ADDING){
    var nb = document.createElement('button');
    nb.type = 'button';
    nb.className = 'on';
    nb.textContent = '＋ 新账号（还没保存）';
    nb.title = '填好下面的内容，点「保存配置」就会加进来';
    box.appendChild(nb);
  }
  ACCOUNTS.forEach(function(a){
    var b = document.createElement('button');
    var info = acctStateInfo(a);
    // 名称和抖音号一样时不再重复一遍：标签短一点，账号多了才排得下
    var nm = a.username || a.unique_id;
    var head = (nm && nm !== a.unique_id) ? (nm + '（' + a.unique_id + '）') : a.unique_id;
    var label = head + ' · ' + info.t;
    b.type = 'button';
    if(!ADDING && CUR_ACCT && a.unique_id === CUR_ACCT.unique_id){ b.className = 'on'; }
    // 名字太长就省略号收尾，别把整行撑破；完整内容放在悬停提示和无障碍标签里
    b.innerHTML = '<span class="dot ' + info.c + '"></span><span class="nm">' + esc(label) + '</span>';
    b.title = label;
    if(b.setAttribute){ b.setAttribute('aria-label', '切换到账号：' + label); }
    b.onclick = function(){ selectAccount(a.unique_id); };
    box.appendChild(b);
  });
}
function fillForm(a){
  a = a || {};
  var f = $('cfg');
  if($('f_ck')){ $('f_ck').value = ''; }
  FORM_UID = a.unique_id || null;
  f.username.value = a.username || '';
  f.unique_id.value = a.unique_id || '';
  f.targets.value = (a.targets || []).join(NL);
  f.schedule_times.value = (a.times || []).join(NL);
  f.orig_unique_id.value = a.unique_id || '';
  // 这三项跟着抖音号走：这个号自己设过就用它自己的，没设过就显示全局默认值
  var st = a.settings || {};
  f.message_template.value = (st.template !== undefined) ? st.template : (GLOBALCFG.message_template || '');
  f.delay_min.value = (st.delay_min !== undefined) ? st.delay_min : (GLOBALCFG.delay_min === undefined ? '0' : GLOBALCFG.delay_min);
  f.delay_max.value = (st.delay_max !== undefined) ? st.delay_max : (GLOBALCFG.delay_max === undefined ? '0' : GLOBALCFG.delay_max);
  f.hitokoto_types.value = (st.hitokoto_types !== undefined) ? st.hitokoto_types : (GLOBALCFG.hitokoto_types || '');
  f.tz.value = GLOBALCFG.tz || 'Asia/Shanghai';
  f.log_level.value = GLOBALCFG.log_level || 'INFO';
  // 「顺便设为新账号默认值」每次切号都复位，免得手一抖把默认值也改了
  if(f.save_global){ f.save_global.checked = false; }
  $('cfgstate').textContent = a.has_cookie
    ? ('已保存 ' + a.cookie_count + ' 个 Cookie 项' + (a.saved_at ? '（' + a.saved_at + '）' : ''))
    : '这个账号还没有 Cookie，用下面的「开始授权」登录后会自动写入';
  updateCounts();
  scheduleCheckLater();
}
function selectAccount(uid){
  var found = ACCOUNTS.filter(function(a){ return a.unique_id === uid; })[0];
  ADDING = false;
  CUR_ACCT = found || null;
  if(found){ fillForm(found); }
  renderAcctTabs();
}
function applyAccounts(s){
  ACCOUNTS = s.accounts || [];
  GLOBALCFG = s.global || {};
  LAST_CHECKER = s.checker || {};
  var keep = CUR_ACCT ? ACCOUNTS.filter(function(a){ return a.unique_id === CUR_ACCT.unique_id; })[0] : null;
  if(keep){ CUR_ACCT = keep; }
  else if(ADDING){
    // 正在填新账号：轮询不要把它切走。若草稿的抖音号已经进了列表（比如刚扫码授权成功），就接管它，但不覆盖已填内容
    var draft = ($('cfg').unique_id.value || '').trim();
    var hit = draft ? ACCOUNTS.filter(function(a){ return a.unique_id === draft; })[0] : null;
    if(hit){ ADDING = false; CUR_ACCT = hit; }
  }
  else if(ACCOUNTS.length){ CUR_ACCT = ACCOUNTS[0]; fillForm(ACCOUNTS[0]); }
  else {
    // 一个账号都没有（新注册的用户就是这种）：直接进入"填新账号"状态，
    // 这样轮询不会每 2 秒清空他正在输入的内容。
    CUR_ACCT = null;
    if(!ADDING){ ADDING = true; fillForm({}); }
  }
  renderAcctTabs();
}
// ---- 角色：管理员看全部用户；注册用户只看自己绑的抖音号 ----
var IM_ADMIN = true;
function applyRole(s){
  var admin = !!s.is_admin;
  var roleChanged = (IM_ADMIN !== admin);
  IM_ADMIN = admin;
  if(roleChanged){
    // 角色变了：发送记录要按新角色的条数重渲染一次
    // （refreshSends 有「内容没变就不重画」的去重缓存，先把它清掉）
    lastSends = '';
    if(typeof refreshSends === 'function'){ refreshSends(); }
  }
  // 「消息模板 / 发送间隔 / 一言类型」跟着抖音号走：普通用户改也只影响自己名下的号，
  // 所以不锁。只有「时区 / 日志级别」是全体共用的一份，锁给管理员。
  ['f_tz', 'f_ll'].forEach(function(id){
    var el = $(id);
    if(el){ el.disabled = !admin; el.title = admin ? '' : '所有账号共用的设置，只有管理员能改'; }
  });
  if($('gglobalwrap')){ $('gglobalwrap').hidden = !admin; }
  // 管理员才看得到进「管理控制台」的入口
  if($('adminlink')){ $('adminlink').hidden = !admin; }
  if($('navadmin')){ $('navadmin').hidden = !admin; }
  if(!admin && typeof showPanel === 'function' && $('p-admin') && $('p-admin').className.indexOf('on') >= 0){
    // 换了个人登录 / 权限被收回：别把人晾在一个自己看不见的「管理」页上
    showPanel('accounts');
  }
  if($('sendmore')){ $('sendmore').hidden = !admin; }
  if($('sub_redeem')){
    $('sub_redeem').disabled = admin;
    $('sub_redeem').title = admin ? '管理员账号永久有效，请在管理页生成或授予时长' : '';
  }
  if($('sendnote')){
    // 条数跟着角色走，说明文字也得跟着改，免得写着「2 次」却列出了 50 条
    $('sendnote').textContent = '每个好友的成败都有记录和截图，点缩略图放大。'
      + (admin ? '这里显示最近 ' + SEND_VIEW_MAX_ADMIN + ' 次。' : '这里只显示最近 ' + SEND_VIEW_MAX_USER + ' 次。');
  }
  $('adminbox').hidden = !admin;
  $('forcebox').hidden = !admin;
  var re = $('regenable');
  if(re && document.activeElement !== re){ re.checked = !!s.allow_register; }
  // 「我的账号」页：账号信息给所有人看；改密码也放开（改的都是自己的密码）
  $('mebox').hidden = false;
  if($('whoami')){ $('whoami').textContent = String(s.me || '') || '—'; }
  if($('accountWho')){ $('accountWho').textContent = String(s.me || '') || '—'; }
  if($('accountRole')){ $('accountRole').textContent = admin ? '管理员账号' : '普通用户'; }
  if($('accountAdminLink')){
    $('accountAdminLink').hidden = false;
    $('accountAdminLink').textContent = admin ? '管理控制台' : '管理员登录';
    $('accountAdminLink').href = '/admin';
  }
  if($('me_name')){ $('me_name').textContent = String(s.me || '') || '—'; }
  if($('me_role')){ $('me_role').textContent = admin ? '管理员（可见全部抖音号）' : '普通用户'; }
  if($('me_ids')){
    $('me_ids').textContent = admin
      ? '全部（管理权限）'
      : ((s.my_ids || []).length ? (s.my_ids || []).join('、') : '还没绑定，去「抖音账户」新建一个');
  }
  if($('me_badge')){
    var mbd = $('me_badge');
    mbd.textContent = s.has_cookie ? '已授权（Cookie 可用）' : '未授权';
    mbd.style.color = s.has_cookie ? 'var(--ok)' : 'var(--muted)';
  }
  var mine = (s.my_ids || []).length;
  // 普通用户只能有 1 个抖音号：已经有一个了就别再让他点「新增账号」
  var addBtn = $('baddacct');
  if(addBtn){
    var atLimit = !admin && mine >= MAX_PER_USER;
    addBtn.disabled = atLimit;
    addBtn.title = atLimit ? ('每个普通用户只能绑 ' + MAX_PER_USER + ' 个抖音号：想换号就先「删除当前账号」，再建新的') : '';
  }
  $('mehint').textContent = admin
    ? '这里改的是你自己的面板登录密码。要改别人的密码、分配抖音号，去左边「管理」页。'
    : (mine
      ? ('你名下的抖音号：' + (s.my_ids || []).join('、') + '（每个普通用户只能绑 1 个，想换号就先删掉再建新的）。')
      : '还没绑定抖音号：去「抖音账户」点「＋ 新增账号」，只填一个自己起的标识（例如 myspark），再用手机号授权登录。');
  var f = $('cfg');
  f.username.readOnly = false;
  f.unique_id.readOnly = false;
}
var USER_LIST = [], USERS_AT = 0, USERS_FORCE = false, LAST_STATUS = null;
function usersPanelOpen(){
  // 「用户管理」现在是「管理」页里的卡片（不再是折叠块），
  // 所以拿"管理页当前是否显示"当开关：看不见就别白拉用户列表。
  var p = $('p-admin');
  return !!(p && p.className.indexOf('on') >= 0);
}
function loadRedeemCodes(){
  if(!IM_ADMIN || !usersPanelOpen()){ return; }
  fetch('api/redeem/codes').then(function(r){ return r.json(); }).then(function(d){
    var box = $('redeem_history');
    if(!box){ return; }
    var rows = d.codes || [];
    box.innerHTML = rows.length ? ('<div style="overflow:auto"><table style="width:100%;min-width:620px;border-collapse:collapse;font-size:13px">'
      + '<tr style="text-align:left;color:#64748b"><th style="padding:6px 4px">时长</th><th style="padding:6px 4px">生成时间</th><th style="padding:6px 4px">状态</th><th style="padding:6px 4px">使用者</th></tr>'
      + rows.map(function(x){ return '<tr style="border-top:1px solid #eef2f7"><td style="padding:7px 4px">' + esc(x.label || (x.days + ' 天')) + '</td><td style="padding:7px 4px">' + esc(x.created_at || '—') + '</td><td style="padding:7px 4px">' + (x.used ? '<span class="badge n">已使用</span>' : '<span class="badge g">未使用</span>') + '</td><td style="padding:7px 4px">' + esc(x.used_by || '—') + '</td></tr>'; }).join('')
      + '</table></div>') : '还没有生成过兑换码';
  }).catch(function(){});
}
function renderUsers(s){
  if(!(s && s.is_admin)){ return; }
  // 用户列表只有管理员用得到：收起「用户管理」时一次都不拉；展开时立刻拉一次，之后 8 秒兜底
  if(!usersPanelOpen()){ return; }
  if(!USERS_FORCE && (Date.now() - USERS_AT) < 8000){ return; }
  USERS_FORCE = false;
  USERS_AT = Date.now();
  fetch('api/users').then(function(r){ return r.json(); }).then(function(d){
    USER_LIST = d.users || [];
    var box = $('userlist');
    {
      var html = '';
      if(!USER_LIST.length){
        html += '<div style="margin-bottom:8px">还没有人注册。把登录地址发给朋友，让他们自己点「注册一个」就行。</div>';
      }
      html += '<div style="overflow-x:auto">'
        + '<table style="width:100%;min-width:780px;border-collapse:collapse;font-size:14px">'
        + '<tr style="text-align:left;color:#64748b"><th style="padding:6px 4px">登录名</th>'
        + '<th style="padding:6px 4px">手机号</th>'
        + '<th style="padding:6px 4px">注册时间</th><th style="padding:6px 4px">最近登录</th>'
        + '<th style="padding:6px 4px">登录 IP</th>'
        + '<th style="padding:6px 4px">绑定的抖音号 / 状态</th><th style="padding:6px 4px">使用时长</th><th style="padding:6px 4px">操作</th></tr>';
      if(IM_ADMIN && s && s.me){
        var mine = (ACCOUNTS || []).filter(function(a){ return a.login_user === s.me; });
        var mcell = mine.length ? mine.map(function(a){
          return '<div><b>' + esc(a.unique_id) + '</b>'
            + (a.username && a.username !== a.unique_id ? '（' + esc(a.username) + '）' : '')
            + ' · ' + acctState(a)
            + ' <button class="sm sec" type="button" aria-label="把这个抖音号从我的名下解绑" data-unbind="' + esc(a.unique_id) + '" data-user="' + esc(s.me) + '">解绑</button></div>';
        }).join('') : '<span style="color:#94a3b8">还没绑</span>';
        var al = s.admin_login || {};
        var sub = u.subscription || {};
        var subText = sub.unlimited ? '永久有效' : (sub.remaining_text || (sub.active ? '有效' : '已到期'));
        html += '<tr style="border-top:1px solid #eef2f7">'
          + '<td style="padding:8px 4px;white-space:nowrap"><b>' + esc(s.me) + '</b> <span class="muted">（管理员，我自己）</span>'
          + '</td>'
          + '<td style="padding:8px 4px"><span class="muted">—</span></td>'
          + '<td style="padding:8px 4px">—</td>'
          + '<td style="padding:8px 4px">' + (al.at ? esc(al.at) : '—') + '</td>'
          + '<td style="padding:8px 4px">' + (al.ip ? esc(al.ip) : '—') + '</td>'
          + '<td style="padding:8px 4px">' + mcell + '</td>'
          + '<td style="padding:8px 4px">永久有效</td>'
          + '<td style="padding:8px 4px"><span class="muted">—</span></td></tr>';
      }
    USER_LIST.forEach(function(u){
      var acc = u.accounts || [];
        var cell = acc.length ? acc.map(function(a){
          var tag = !a.exists ? '配置已删除'
            : (a.check_ok === true ? '登录正常'
            : (!a.has_cookie ? '未授权'
            : (a.check_ok === false ? '登录失效' : '未检测')));
          return '<div><b>' + esc(a.unique_id) + '</b>'
            + (a.username ? '（' + esc(a.username) + '）' : '')
            + ' · ' + tag
            + ' <button class="sm sec" type="button" aria-label="解绑这个抖音号" data-unbind="' + esc(a.unique_id) + '" data-user="' + esc(u.name) + '">解绑</button></div>';
        }).join('') : '<span style="color:#94a3b8">还没绑</span>';
        html += '<tr style="border-top:1px solid #eef2f7">'
          + '<td style="padding:8px 4px"><b>' + esc(u.name) + '</b></td>'
          + '<td style="padding:8px 4px">' + (u.phone ? esc(u.phone) : '<span style="color:#94a3b8">—</span>') + '</td>'
          + '<td style="padding:8px 4px">' + esc(u.at) + '</td>'
          + '<td style="padding:8px 4px">' + (u.last_login ? esc(u.last_login) : '<span style="color:#94a3b8">从未</span>') + '</td>'
          + '<td style="padding:8px 4px">' + (u.last_ip ? esc(u.last_ip) : '<span style="color:#94a3b8">—</span>') + '</td>'
          + '<td style="padding:8px 4px">' + cell + '</td>'
          + '<td style="padding:8px 4px">' + esc(subText) + (sub.expires_text ? '<br><span class="muted small">至 ' + esc(sub.expires_text) + '</span>' : '') + '</td>'
          + '<td style="padding:8px 4px;white-space:nowrap">'
          + '<button class="sm" type="button" aria-label="给这个用户重设密码" data-pass="' + esc(u.name) + '">改密码</button>'
          + '<button class="sm danger" type="button" aria-label="删除这个用户" data-del="' + esc(u.name) + '">删除</button>'
          + '</td></tr>';
      });
      html += '</table></div>';
      var free = (d.free || []);
      html += '<div style="margin-top:8px">'
        + (free.length
            ? ('还没有分配给任何人的抖音号：' + free.map(function(x){ return '<b>' + esc(x) + '</b>'; }).join('、') + '（普通用户动不了这些号；想给谁用就在下面分配，也可以选「管理员（我自己）」）')
            : '所有抖音号都已经分配给某个用户了')
        + '</div>';
      box.innerHTML = html;
      Array.prototype.forEach.call(box.querySelectorAll('[data-del]'), function(b){
        b.onclick = function(){
          var name = b.dataset.del;
          if(!window.confirm('删除用户「' + name + '」？他会立刻登不上控制台；他绑的抖音号配置会保留，可以再分配给别人。')){ return; }
          post('api/user/delete', {name: name}).then(function(r){ flash(r.message || r.error || '', !!r.ok); refresh(); });
        };
      });
      Array.prototype.forEach.call(box.querySelectorAll('[data-pass]'), function(b){
        b.onclick = function(){
          var name = b.dataset.pass;
          var pass = window.prompt('给「' + name + '」设置一个新密码（至少 6 位）：');
          if(!pass){ return; }
          post('api/user/password', {name: name, password: pass}).then(function(r){
            flash(r.ok ? ('新密码：' + (r.password || pass) + '（只显示这一次，请私下发给他）') : (r.error || ''), !!r.ok);
            refresh();
          });
        };
      });
      Array.prototype.forEach.call(box.querySelectorAll('[data-unbind]'), function(b){
        b.onclick = function(){
          var uid = b.dataset.unbind, user = b.dataset.user;
          if(!window.confirm('把「' + uid + '」从 ' + user + ' 名下解绑？解绑后他看不到这个抖音号，配置和 Cookie 还在。')){ return; }
          post('api/user/unbind', {name: user, unique_id: uid}).then(function(r){ flash(r.message || r.error || '', !!r.ok); refresh(); });
        };
      });
    }
    var sel = $('ub_user'), keep = sel.value;
    sel.innerHTML = USER_LIST.map(function(u){
      return '<option value="' + esc(u.name) + '">' + esc(u.name) + '</option>';
    }).join('');
    if(IM_ADMIN && s && s.me){
      sel.innerHTML = '<option value="' + esc(s.me) + '">管理员（我自己）</option>' + sel.innerHTML;
    }
    if(keep){ sel.value = keep; }
    var grant = $('grant_user');
    if(grant){
      var oldGrant = grant.value;
      grant.innerHTML = USER_LIST.map(function(u){ return '<option value="' + esc(u.name) + '">' + esc(u.name) + '</option>'; }).join('');
      if(oldGrant){ grant.value = oldGrant; }
    }
    loadRedeemCodes();
  }).catch(function(){});
}
$('regenable').onchange = function(){
  var on = this.checked;
  post('api/register/allow', {allow: on}).then(function(r){
    flash(r.message || r.error || '', !!r.ok);
    if(!r.ok){ refresh(); }
  });
};
$('adm_btn').onclick = function(){
  var o = $('adm_old').value, n = $('adm_new').value, a = $('adm_again').value;
  if(!o || !n){ $('adm_state').textContent = '现在的密码和新密码都要填'; return; }
  if(n !== a){ $('adm_state').textContent = '两次输入的新密码不一样'; return; }
  post('api/admin/password', {old: o, password: n, again: a}).then(function(r){
    $('adm_state').textContent = r.message || r.error || '';
    flash(r.message || r.error || '', !!r.ok);
    if(r.ok){ $('adm_old').value = ''; $('adm_new').value = ''; $('adm_again').value = ''; }
  });
};
$('nu_btn').onclick = function(){
  var n = $('nu_name').value.trim(), p = $('nu_pw').value;
  if(!n || !p){ $('nu_state').textContent = '登录名和密码都要填'; return; }
  post('api/user/create', {name: n, password: p}).then(function(r){
    $('nu_state').textContent = r.message || r.error || '';
    flash(r.message || r.error || '', !!r.ok);
    if(r.ok){ $('nu_name').value = ''; $('nu_pw').value = ''; refresh(); }
  });
};
$('ub_save').onclick = function(){
  var uid = $('ub_uid').value.trim(), name = $('ub_user').value;
  if(!uid){ flash('请填写要分配的抖音号', false); return; }
  if(!name){ flash('还没有注册用户可以分配', false); return; }
  post('api/user/bind', {name: name, unique_id: uid}).then(function(r){
    $('ub_state').textContent = r.message || r.error || '';
    flash(r.message || r.error || '', !!r.ok);
    if(r.ok){ $('ub_uid').value = ''; refresh(); }
  });
};
if($('redeem_generate')){
  $('redeem_generate').onclick = function(){
    var days = $('redeem_days').value, count = parseInt($('redeem_count').value || '1', 10);
    $('redeem_generate').disabled = true;
    post('api/redeem/generate', {days: days, count: count}).then(function(r){
      $('redeem_generate_state').textContent = r.message || r.error || '';
      $('redeem_generate_state').style.color = r.ok ? 'var(--ok)' : 'var(--bad)';
      var out = $('redeem_generated');
      if(out){ out.hidden = !r.ok; out.textContent = r.ok ? (r.codes || []).join('\n') : ''; }
      if(r.ok){ loadRedeemCodes(); }
    }).finally(function(){ $('redeem_generate').disabled = false; });
  };
}
if($('grant_duration')){
  $('grant_duration').onclick = function(){
    var name = $('grant_user').value, days = $('grant_days').value;
    if(!name){ $('grant_state').textContent = '还没有可授予的普通用户'; return; }
    $('grant_duration').disabled = true;
    post('api/user/grant', {name: name, days: days}).then(function(r){
      $('grant_state').textContent = r.message || r.error || '';
      $('grant_state').style.color = r.ok ? 'var(--ok)' : 'var(--bad)';
      if(r.ok){ refresh(); }
    }).finally(function(){ $('grant_duration').disabled = false; });
  };
}
$('mp_save').onclick = function(){
  var old = $('mp_old').value, a = $('mp_new').value, b = $('mp_new2').value;
  if(!old || !a){ flash('请填写现在和新密码', false); return; }
  if(a !== b){ flash('两次输入的新密码不一样', false); return; }
  post('api/me/password', {old: old, password: a, again: b}).then(function(r){
    $('mp_state').textContent = r.message || r.error || '';
    flash(r.message || r.error || '', !!r.ok);
    if(r.ok){ $('mp_old').value = ''; $('mp_new').value = ''; $('mp_new2').value = ''; }
  });
};
var SCHEDULE_CONFLICTS = [], scheduleCheckTimer = null, scheduleCheckAt = 0, scheduleCheckSignature = '', SCHEDULE_AUTOFILL = false;
function scheduleCheckNow(force){
  var f = $('cfg');
  if(!f || !f.schedule_times){ return Promise.resolve(null); }
  var signature = [f.unique_id.value, f.schedule_times.value, f.targets.value, f.delay_max.value].join('|');
  if(!force && signature === scheduleCheckSignature && Date.now() - scheduleCheckAt < 7000){ return Promise.resolve(null); }
  scheduleCheckSignature = signature; scheduleCheckAt = Date.now();
  return post('api/schedule/check', {
    orig_unique_id: f.orig_unique_id.value || '', unique_id: f.unique_id.value || '',
    schedule_times: f.schedule_times.value || '', targets: f.targets.value || '', delay_max: f.delay_max.value || '0'
  }).then(function(r){
    if(!r || !r.ok){ return r || null; }
    SCHEDULE_CONFLICTS = r.conflicts || [];
    if(SCHEDULE_AUTOFILL && !f.schedule_times.value.trim() && r.recommended){
      f.schedule_times.value = r.recommended;
      SCHEDULE_AUTOFILL = false;
    }
    if($('schedule_estimate')){ $('schedule_estimate').textContent = '约 ' + (r.estimate_minutes || 5) + ' 分钟'; }
    if($('schedule_recommended')){ $('schedule_recommended').textContent = r.recommended || '暂无可用时段'; }
    if($('schedule_occupied')){
      var occupied = r.occupied || [];
      $('schedule_occupied').textContent = occupied.length
        ? '其他账号已安排：' + occupied.slice(0, 12).map(function(x){ return x.time + '（约 ' + (x.estimate_minutes || 0) + ' 分钟）'; }).join('、')
        : '目前没有其他账号安排发送任务。';
    }
    if($('schedule_hint')){
      if(SCHEDULE_CONFLICTS.length){
        $('schedule_hint').textContent = '这个时间段与其他发送任务重叠：' + SCHEDULE_CONFLICTS.map(function(x){ return x.time + '（已有任务 ' + x.other_time + '）'; }).join('、');
        $('schedule_hint').style.color = 'var(--warn)';
      } else {
        $('schedule_hint').textContent = f.schedule_times.value.trim() ? '当前时间段没有检测到重叠。' : '请设置每天发送时间以开启自动发送。';
        $('schedule_hint').style.color = 'var(--muted)';
      }
    }
    return r;
  });
}
function scheduleCheckLater(){
  if(scheduleCheckTimer){ clearTimeout(scheduleCheckTimer); }
  scheduleCheckTimer = setTimeout(function(){ scheduleCheckNow(true).catch(function(){}); }, 450);
}
['f_times','f_targets','f_dmax'].forEach(function(id){ var el = $(id); if(el){ el.addEventListener('input', function(){ if(id === 'f_times'){ SCHEDULE_AUTOFILL = false; } scheduleCheckLater(); }); } });

$('cfg').addEventListener('submit', function(e){
  e.preventDefault();
  var f = e.target, data = {};
  var sb = $('bsubmit');
  if(sb && sb.disabled){ return; }
  var oldText = (sb && sb.textContent) || '保存配置';
  if(sb){ sb.disabled = true; sb.textContent = '保存中…'; }
  var done = function(){ if(sb){ sb.disabled = false; sb.textContent = oldText; } };
  ['orig_unique_id','username','unique_id','targets','schedule_times','message_template','delay_min','delay_max','tz','log_level','hitokoto_types'].forEach(function(k){ data[k] = f[k].value; });
  data.cookie_json = $('f_ck') ? $('f_ck').value : '';
  data.save_global = (f.save_global && f.save_global.checked) ? '1' : '';
  scheduleCheckNow(true).then(function(sr){
    if(sr && !sr.ok){ flash(sr.error || '发送时间检查失败', false); return null; }
    if(sr && sr.conflicts && sr.conflicts.length && !window.confirm('发送时间与其他账号的任务重叠：\n\n'
      + sr.conflicts.map(function(x){ return x.time + '（已有任务 ' + x.other_time + '）'; }).join('\n')
      + '\n\n仍然保存吗？')){ return null; }
    return post('api/config', data);
  }).then(function(r){
    if(!r){ done(); return; }
    if(r.ok){
      flash(r.message || '配置已保存', true);
      if($('f_ck')){ $('f_ck').value = ''; }
      var want = r.unique_id || f.unique_id.value;
      ADDING = false;
      CUR_ACCT = null; FORM_UID = null;
      refresh();
      setTimeout(function(){ selectAccount(want); }, 500);
    } else { flash(r.error || '保存失败', false); }
    done();
  }).catch(function(){ flash('保存失败：网络断了或后端没响应，稍后再试', false); done(); });
});
$('baddacct').onclick = function(){
  var f = $('cfg');
  var draft = (f.unique_id.value || '').trim();
  var dirty = !!(draft || (f.username.value || '').trim() || (f.targets.value || '').trim() || (f.schedule_times.value || '').trim());
  // 正在填的内容不会自己保存：要点「保存配置」才算数，所以清空前先问一句
  if(dirty && !window.confirm('「' + (draft || '这个账号') + '」里还有没保存的内容，点「新增账号」会把它们清空。确定要开始填新账号吗？\n（点「取消」就什么都不动）')){
    return;
  }
  ADDING = true; CUR_ACCT = null; FORM_UID = null;
  SCHEDULE_AUTOFILL = true;
  fillForm({});
  renderAcctTabs();
  f.unique_id.focus();
  flash('新账号：先填一个「抖音号」，点「保存配置」或直接点「开始授权」都行', true);
};
$('bdelacct').onclick = function(){
  var uid = (CUR_ACCT && CUR_ACCT.unique_id) || $('cfg').unique_id.value.trim();
  if(!uid){ flash('当前没有可删除的账号', false); return; }
  if(!window.confirm('确定删除账号「' + uid + '」？它的 Cookie 和检测记录会一起删掉，其它账号不受影响。')){ return; }
  post('api/account/delete', {unique_id: uid}).then(function(r){
    flash(r.message || r.error || '', !!r.ok);
    CUR_ACCT = null; FORM_UID = null;
    refresh();
  });
};
function startAuth(uid, name){
  post('api/browser/start', {unique_id: uid, username: name}).then(function(r){
    note(r.ok ? (r.message || '已启动授权浏览器') : (r.error || '启动失败'), !!r.ok);
    // 点了「开始授权」不再自动弹窗、也不再自动开实时画面：
    // 手机号和验证码直接填在第 2 步里，想看抖音那边的画面就自己点「实时画面」
  });
}
function startFlow(){
  var f = $('cfg');
  var uid = (f.unique_id.value || '').trim();
  var saved = ACCOUNTS.some(function(a){ return a.unique_id === uid; });
  if(!uid){ note('请先在上面第 1 步「账号」里填一个「抖音号」（自己起个名字，例如 myspark），再点开始授权', false); return; }
  if(saved){ startAuth(uid, f.username.value); return; }
  // 新填的抖音号还没保存：先自动保存一次（顺便绑到你名下），再开扫码
  note('先把「' + uid + '」保存下来（会自动绑到你名下）…', true);
  var data = {};
  ['orig_unique_id','username','unique_id','targets','schedule_times','message_template','delay_min','delay_max','tz','log_level','hitokoto_types'].forEach(function(k){ data[k] = f[k].value; });
  data.cookie_json = $('f_ck') ? $('f_ck').value : '';
  data.save_global = (f.save_global && f.save_global.checked) ? '1' : '';
  post('api/config', data).then(function(r){
    if(!r.ok){
      note('保存失败：' + (r.error || '请检查「抖音号」是否填对'), false);
      return;
    }
    ADDING = false;
    refresh();
    setTimeout(function(){ startAuth(r.unique_id || uid, f.username.value); }, 600);
  });
}
function restartAuthFlow(){
  if(authRestarting){ return; }
  var f = $('cfg');
  var uid = (f.unique_id.value || '').trim();
  var name = (f.username.value || '').trim();
  if(!uid){ note('请先选择或填写一个抖音号，再重启授权', false); return; }
  authRestarting = true;
  note('正在检查当前授权，随后会重新打开二维码…', true);
  refresh();
  fetch('api/state?unique_id=' + encodeURIComponent(uid), {cache:'no-store'})
    .then(function(response){ if(!response.ok){ throw new Error('state'); } return response.json(); })
    .then(function(s){
      if(!s.browser || !s.browser.running){
        authRestarting = false;
        refresh();
        if(ACCOUNTS.some(function(a){ return a.unique_id === uid; })){
          startAuth(uid, name);
        } else {
          startFlow();
        }
        return;
      }
      note('正在关闭旧授权，稍后会重新打开二维码…', true);
      post('api/browser/stop', {unique_id: uid}).then(function(r){
        if(!r.ok){
          authRestarting = false;
          note(r.error || '停止当前授权失败，没有启动新的授权', false);
          refresh();
          return;
        }
        waitAuthStopped(uid, name, 0);
      });
    })
    .catch(function(){
      authRestarting = false;
      note('无法确认当前授权状态，没有启动或停止授权。请稍后重试。', false);
      refresh();
    });
}
function waitAuthStopped(uid, name, attempt){
  fetch('api/state?unique_id=' + encodeURIComponent(uid), {cache:'no-store'})
    .then(function(r){ if(!r.ok){ throw new Error('state'); } return r.json(); })
    .then(function(s){
      if(!s.browser || !s.browser.running){
        authRestarting = false;
        refresh();
        if(ACCOUNTS.some(function(a){ return a.unique_id === uid; })){
          startAuth(uid, name);
        } else {
          startFlow();
        }
        return;
      }
      if(attempt >= 39){
        authRestarting = false;
        note('旧授权还没有完全关闭，因此没有启动新授权。请稍后点「停止」后再试。', false);
        refresh();
        return;
      }
      setTimeout(function(){ waitAuthStopped(uid, name, attempt + 1); }, 500);
    })
    .catch(function(){
      if(attempt >= 39){
        authRestarting = false;
        note('暂时无法确认授权是否已关闭，没有启动新授权。请稍后重试。', false);
        refresh();
        return;
      }
      setTimeout(function(){ waitAuthStopped(uid, name, attempt + 1); }, 500);
    });
}
function lockPeer(id, btn){
  // 顶栏和左栏是两个按钮：锁要一起锁，不然连点两下会起两次
  var b = $(id);
  if(b && btn){ b._lockUntil = btn._lockUntil; b.disabled = true; }
}
$('bstart').onclick = function(){ if(!lockButton(this)){ return; } lockPeer('hbstart', this); startFlow(); };
if($('hbstart')){ $('hbstart').onclick = function(){ if(!lockButton(this)){ return; } lockPeer('bstart', this); startFlow(); }; }
function setShot(open){
  shotOpen = !!open;
  $('shotwrap').hidden = !shotOpen;
  $('bshot').textContent = shotOpen ? '收起画面' : '显示画面';
  $('bshot').setAttribute('aria-label', shotOpen ? '收起浏览器画面' : '显示浏览器画面');
}
$('bshot').onclick = function(){
  var open = !shotOpen;
  // 手动收起之后就别再自动弹回来，否则用户刚关掉、2 秒后又被顶开
  shotManual = !open;
  setShot(open);
};
$('bstop').onclick = function(){
  if(!lockButton(this)){ return; }
  post('api/browser/stop', {unique_id: curUid()}).then(function(){ flash('已请求停止', true); });
};
if($('bauthrestart')){ $('bauthrestart').onclick = function(){
  if(!lockButton(this)){ return; }
  lockPeer('bstart', this); lockPeer('hbstart', this); lockPeer('bstop', this);
  restartAuthFlow();
}; }
Array.prototype.forEach.call(document.querySelectorAll('[data-wheel]'), function(b){
  b.onclick = function(){ post('api/browser/wheel', {dy: parseFloat(b.dataset.wheel), unique_id: curUid()}); };
});
Array.prototype.forEach.call(document.querySelectorAll('[data-press]'), function(b){
  b.onclick = function(){ post('api/browser/press', {key: b.dataset.press, unique_id: curUid()}); };
});
Array.prototype.forEach.call(document.querySelectorAll('[data-goto]'), function(b){
  b.onclick = function(){ post('api/browser/goto', {unique_id: curUid()}); };
});
$('bforce').onclick = function(){
  $('forcestate').textContent = '正在强制停止…';
  post('api/force/stop', {reason: '面板按钮'}).then(function(r){
    $('forcestate').textContent = r.message || r.error || '';
    flash(r.message || r.error || '', !!r.ok);
    refresh();
  }).catch(function(){ $('forcestate').textContent = '请求失败，请刷新页面重试'; });
};
$('brestart').onclick = function(){
  if(!window.confirm('确定强制重启面板后端？大约 10 秒后自动恢复。')){ return; }
  $('forcestate').textContent = '正在重启后端，页面会在 15 秒后自动刷新…';
  post('api/force/restart', {}).then(function(r){
    $('forcestate').textContent = r.message || r.error || '';
  }).catch(function(){}).then(function(){
    setTimeout(function(){ location.reload(); }, 15000);
  });
};
$('brun').onclick = function(){
  if(!lockButton(this)){ return; }
  var who = (CUR_ACCT && (CUR_ACCT.username || CUR_ACCT.unique_id)) || ($('cfg').unique_id.value || '').trim() || '当前账号';
  // 手动运行是真发消息，问一句再动手；点取消就把按钮放开，别白等 5 秒
  if(!window.confirm('立即运行一次：会给「' + who + '」的目标好友真实发送消息。确定现在发吗？')){
    this._lockUntil = 0; this.disabled = false;
    return;
  }
  post('api/run').then(function(r){ flash(r.message || r.error || '', !!r.ok); });
};
// 检测要跑十几秒到两分钟：给条进度和秒数，别让人干等着不知道跑到哪了
var CHECK_TOTAL = 120, checkTimer = null, checkStart = 0, checkWasRunning = false;
function startCheckTimer(){
  var bar = $('checkbar'), tm = $('checktime'), fill = $('checkfill');
  if(checkTimer){ clearInterval(checkTimer); }
  checkStart = Date.now();
  if(bar){ bar.hidden = false; if(bar.classList){ bar.classList.remove('done'); bar.classList.remove('bad'); } }
  if(tm){ tm.hidden = false; }
  var tickFn = function(){
    var sec = checkStart ? Math.round((Date.now() - checkStart) / 1000) : 0;
    var pct = Math.max(2, Math.min(100, Math.round(sec * 100 / CHECK_TOTAL)));
    if(fill && fill.style){ fill.style.width = pct + '%'; }
    if(tm){ tm.textContent = '检测中…已经等了 ' + sec + ' 秒（最多等 ' + CHECK_TOTAL + ' 秒）'; }
  };
  tickFn();
  checkTimer = setInterval(tickFn, 1000);
}
function stopCheckTimer(){
  if(checkTimer){ clearInterval(checkTimer); checkTimer = null; }
  var bar = $('checkbar'), tm = $('checktime'), fill = $('checkfill');
  if(bar){ bar.hidden = true; if(bar.classList){ bar.classList.remove('done'); bar.classList.remove('bad'); } }
  if(tm){ tm.hidden = true; tm.textContent = ''; }
  if(fill && fill.style){ fill.style.width = '0%'; }
}
function finishCheck(){
  var sec = checkStart ? Math.round((Date.now() - checkStart) / 1000) : 0;
  var bar = $('checkbar'), tm = $('checktime'), fill = $('checkfill');
  if(bar && bar.classList){ bar.classList.add('done'); }
  if(fill && fill.style){ fill.style.width = '100%'; }
  if(tm){ tm.hidden = false; tm.textContent = '检测结束，一共用了 ' + sec + ' 秒'; }
  setTimeout(stopCheckTimer, 3000);
}
// ---- 一键复制当前抖音号的 Cookie ----
// 面板是明文 HTTP：navigator.clipboard 在非安全上下文里基本没有，
// 所以先用几十年的老办法 document.execCommand('copy') 兜底，都不行才让人手动复制。
function copyViaTextarea(text){
  var ta = document.createElement('textarea');
  ta.value = text;
  ta.setAttribute('readonly', 'readonly');
  ta.style.position = 'fixed';
  ta.style.top = '-1000px';
  ta.style.left = '-1000px';
  document.body.appendChild(ta);
  var ok = false;
  try {
    ta.focus();
    ta.select();
    try { ta.setSelectionRange(0, ta.value.length); } catch(e){}
    ok = document.execCommand('copy');
  } catch(e){ ok = false; }
  if(ta.parentNode && ta.parentNode.removeChild){ ta.parentNode.removeChild(ta); }
  return ok;
}
function copyToClipboard(text){
  if(window.isSecureContext && navigator.clipboard && navigator.clipboard.writeText){
    return navigator.clipboard.writeText(text).then(
      function(){ return true; },
      function(){ return copyViaTextarea(text); }
    );
  }
  return Promise.resolve(copyViaTextarea(text));
}
// 复制哪个号：正在新增就跟着表单填的抖音号走，否则跟着当前选中的账号
function copyCookieUid(){
  if(ADDING){
    var f = $('cfg');
    return (f && f.unique_id) ? String(f.unique_id.value || '').trim() : '';
  }
  return String((CUR_ACCT || {}).unique_id || '').trim();
}
function copyCookie(){
  var btn = $('bcopycookie'), box = $('copystate');
  if(!btn || btn._copying){ return; }
  // 自己管锁：lockButton 那套要靠 refresh() 解锁，这里没人会给这个按钮解锁，会一直灰着
  btn._copying = true;
  btn.disabled = true;
  if(box){ box.textContent = '正在读取 Cookie…'; }
  function done(){
    btn._copying = false;
    btn.disabled = false;
    if(box){ box.textContent = ''; }
  }
  function fail(text){
    done();
    note(text || '复制失败，稍后再试', false);
  }
  var uid = copyCookieUid();
  fetch('/api/cookie' + (uid ? ('?unique_id=' + encodeURIComponent(uid)) : ''))
    .then(function(r){
      return r.text().then(function(t){
        var body = null;
        try { body = JSON.parse(t); } catch(e){ body = null; }
        if(!body || typeof body !== 'object'){
          body = {ok: false, error: 'HTTP ' + r.status + '（后端没返回正常内容，可能正在重启）'};
        }
        return body;
      });
    })
    .catch(function(){ return {ok: false, error: '网络断了或后端没响应，稍后再试'}; })
    .then(function(body){
      if(!body.ok){ fail(body.error); return; }
      return copyToClipboard(String(body.cookie_text || '')).then(function(ok){
        if(ok){
          done();
          note('已复制「' + (body.username || body.unique_id) + '」的 Cookie（' + body.cookie_count + ' 项），别转发给别人', true);
        } else {
          // 自动复制被浏览器挡了，就把原文给用户手动选，别假装复制成功了
          done();
          window.prompt('浏览器不让自动复制：下面这串就是 Cookie，按 Ctrl+A 全选再按 Ctrl+C', body.cookie_text);
        }
      }, function(){ fail('复制出错了，稍后再试'); });
    })
    .catch(function(){ fail('复制出错了，稍后再试'); });
}
// ---- 手机验证码只有一个入口：授权弹窗里的 wzcode 框（见 wzSubmit） ----
var lastQrHash = '', lastWantSms = false, qrFailedHash = '';
var wantManual = false, wantQr = true;
function setAuthMethod(method){
  if(['manual','qr','cookie'].indexOf(method) < 0){ return; }
  wantManual = method === 'manual';
  wantQr = method === 'qr';
  [['tabmanual','manual'],['tabqr','qr'],['tabcookie','cookie']].forEach(function(item){
    var tab = $(item[0]), selected = method === item[1];
    if(tab){
      tab.className = 'authtab' + (selected ? ' on' : '');
      tab.setAttribute('aria-selected', selected ? 'true' : 'false');
    }
  });
  if($('browserauthpane')){ $('browserauthpane').hidden = method === 'cookie'; }
  if($('cookieauthpane')){ $('cookieauthpane').hidden = method !== 'cookie'; }
  if($('manualauthnotice')){ $('manualauthnotice').hidden = !wantManual; }
  if($('manualscreentip')){ $('manualscreentip').hidden = true; }
  if(wantManual){
    shotManual = false;
    setShot(true);
  } else if(wantQr){
    shotManual = false;
    setShot(false);
  }
  if(typeof refresh === 'function'){ refresh(); }
}
if($('tabmanual')){ $('tabmanual').onclick = function(){ setAuthMethod('manual'); }; }
if($('tabqr')){ $('tabqr').onclick = function(){ setAuthMethod('qr'); }; }
if($('tabcookie')){ $('tabcookie').onclick = function(){ setAuthMethod('cookie'); }; }

// 二维码取不下来（比如刚好在换新、或这张码不属于我）时把整块收起来，
// 不给用户留一个带 alt 文字的破图
(function(){
  var img = $('qrimg');
  if(img){
    img.onerror = function(){
      qrFailedHash = lastQrHash;
      this.hidden = true;
      this.removeAttribute('src');
      var area = $('qrarea');
      if(area){ area.hidden = true; }
    };
  }
})();
$('bcopycookie').onclick = copyCookie;
$('bcheck').onclick = function(){
  if(!lockButton(this)){ return; }
  var ck = LAST_CHECKER || {};
  var me = (CUR_ACCT && CUR_ACCT.unique_id) || '';
  // 正在检测我自己的号：这个按钮已经变成「停止检测」
  if(ck.running && (IM_ADMIN || !ck.unique_id || ck.unique_id === me)){
    post('api/check/stop', {}).then(function(r){ flash(r.message || r.error || '', !!r.ok); refresh(); });
    return;
  }
  var uid = me || $('cfg').unique_id.value.trim();
  if(!uid){ flash('请先选择或新增一个账号', false); return; }
  $('checkstate').textContent = '正在启动检测…';
  checkWasRunning = true;
  startCheckTimer();
  post('api/check', {unique_id: uid}).then(function(r){
    if(!r.ok){ flash(r.error || '检测启动失败', false); checkWasRunning = false; stopCheckTimer(); refresh(); }
  }).catch(function(){ flash('检测没能启动，请稍后再试', false); checkWasRunning = false; stopCheckTimer(); });
};
var shot = $('shot');
function bindShot(img){
  if(!img){ return; }
  img.addEventListener('click', function(e){
    if(!img.naturalWidth){ return; }
    var rect = img.getBoundingClientRect();
    var x = Math.round((e.clientX - rect.left) * img.naturalWidth / rect.width);
    var y = Math.round((e.clientY - rect.top) * img.naturalHeight / rect.height);
    post('api/browser/click', {x: x, y: y, unique_id: curUid()});
  });
}
bindShot(shot);

// ---- 画面只有一个：第 2 步里的实时截图（框内点击＝操作抖音页面） ----
// 以前还有一扇"实时画面"悬浮窗和这里显示同一帧，两套开关互相打架，
// 现在只留这一处，悬浮窗整个删掉了。
var shotOpen = false, shotAutoDone = false, shotManual = false, shotDoneClosed = false, busyLast = false;
var authRestarting = false;
// 二级验证手动模式：只在**刚进入**那一下强制摊开画面（进这一阶段是一次性事件）
var manualOpened = false;
var SHOT_EMPTY_HINT = '当前没有正在运行的浏览器画面：点「开始授权」或「检测登录状态」后，这里会实时显示。';
var SHOT_STALE_HINT = '画面已经不动了：最后一张是刚才那一下的。重新点「开始授权」或「检测登录状态」会继续刷新。';
function showShotHint(text){
  var el = $('shotempty');
  if(!el){ return; }
  el.textContent = text || SHOT_EMPTY_HINT;
  el.hidden = false;
}
function setShotTag(age, fresh){
  var tag = $('livetag');
  if(!tag){ return; }
  if(!fresh){ tag.hidden = true; return; }
  tag.hidden = false;
  tag.textContent = (typeof age === 'number') ? ('实时画面 · ' + age + ' 秒前') : '实时画面';
}
var lastUrl = null;
// 当前界面上选中的抖音号：所有浏览器指令/画面请求都带上它，多会话时才不会串
function curUid(){ try { return String((CUR_ACCT && CUR_ACCT.unique_id) || ($('cfg') ? $('cfg').unique_id.value : '') || '').trim(); } catch(e){ return ''; } }
function withUid(url){
  var uid = curUid();
  if(!uid){ return url; }
  return url + (url.indexOf('?') >= 0 ? '&' : '?') + 'unique_id=' + encodeURIComponent(uid);
}
function esc(t){
  return String(t === null || t === undefined ? '' : t)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
function badge(text, cls){
  var b = $('badge');
  if(!b){ return; }
  // 状态用一个跟着文字一起变色的圆点表示，比 emoji 在各平台一致得多
  b.className = 'badge ' + cls;
  b.innerHTML = '<span class="dot"></span><span>' + esc(text) + '</span>';
}
function headBadge(text, cls){
  // 顶部常驻的那一条：不滚屏也能看到当前账号是不是登录着
  var h = $('headbadge');
  if(!h){ return; }
  h.className = 'badge ' + cls;
  h.innerHTML = '<span class="dot"></span><span>' + esc(text) + '</span>';
}
// 记「已经画过哪一条失效提示」：用 时间+账号 做 key；授权成功后清空，下次再失效还能弹
var RELOGIN_KEY = '';
function renderRelogin(info){
  var box = $('relogin');
  if(!info){ box.innerHTML = ''; RELOGIN_KEY = ''; return; }
  var key = String(info.at || '') + '|' + String(info.account || '');
  if(RELOGIN_KEY === key){ return; }
  RELOGIN_KEY = key;
  box.innerHTML = '<div class="flash bad" style="padding:14px;font-size:14px;line-height:1.8">'
    + '<b>抖音登录已失效</b>（' + esc(info.at || '') + '，账号 ' + esc(info.account || '') + '）<br>'
    + esc(info.detail || '') + '<br>'
    + '<button id="breauth" type="button" style="margin-top:8px">重新授权登录</button>'
    + '<span class="muted" style="margin-left:8px">点它打开抖音登录页，用手机号收验证码登录即可（也可以切「手动授权」自己在画面里操作）；3 分钟没人操作会自动关掉浏览器</span>'
    + '</div>';
  var btn = $('breauth');
  if(btn){
    btn.onclick = function(){
      // 失效的是哪个号就授权哪个号。以前这里拿的是"当前选中的账号"，
      // 失效的号不是当前选中的那个时，会去给另一个号开浏览器。
      var want = String(info.account || '').trim();
      var hit = null;
      if(want){
        hit = ACCOUNTS.filter(function(a){
          return a.unique_id === want || a.username === want;
        })[0] || null;
      }
      var uid = hit ? hit.unique_id : want;
      if(!uid){ flash('先在「1 账号」里选好要授权的账号，再点「开始授权」', false); return; }
      if(hit){ selectAccount(hit.unique_id); }
      post('api/browser/start', {unique_id: uid, username: hit ? hit.username : ''}).then(function(r){
        flash(r.ok ? ('已给「' + uid + '」启动授权，往下滚到第 2 步填手机号') : (r.error || '启动失败'), !!r.ok);
        if(r.ok){ $('authbox').scrollIntoView({behavior:'smooth', block:'start'}); }
      });
    };
  }
}
var REASON = {
  sent:['已发送','g'],
  sent_unverified:['已发送（未校验）','y'],
  stuck:['没发出去','r'],
  no_editor:['聊天窗口没打开','r'],
  type_failed:['输入失败','r'],
  send_error:['发送出错','r'],
  not_delivered:['消息未送达','r'],
  no_login:['登录失效','r'],
  no_friend:['没找到好友','n'],
  not_found:['好友列表里没找到','r'],
  slow_page:['页面加载慢','y'],
  aborted:['被强制停止','n']
};
function tagHtml(text, cls){
  return '<span class="badge ' + cls + '" style="padding:2px 9px;font-size:12px"><span class="dot"></span>' + esc(text) + '</span>';
}
function renderStatus(s){
  var cur;
  if(ADDING && !CUR_ACCT){
    cur = {
      username: ($('cfg').username.value || '').trim(),
      unique_id: ($('cfg').unique_id.value || '').trim(),
      targets: [], has_cookie: false, check: null, _new: true, ready: false,
    };
  } else {
    cur = CUR_ACCT || s;
  }
  var chk = realCheck(cur.check), ck = s.checker || {}, au = s.auth || {};
  // 二级验证：抖音要求用已登录的设备扫码时，把抓到的二维码直接摆出来
  var vq = !!au.verify_qr, vqh = String(au.qr_hash || "");
  var vqBox = $("verifyqr");
  if(vqBox){
    vqBox.hidden = !vq;
    if(vq){
      var vh = $("verifyhint");
      if(vh){ vh.textContent = au.verify_hint || "抖音要求二级验证：用你手机上已登录的抖音 App 扫下面这个二维码。"; }
      var vi = $("verifyqrimg");
      if(vi && vqh && vi.getAttribute("data-qh") !== vqh){
        vi.setAttribute("data-qh", vqh);
        vi.src = withUid("api/qr?h=" + encodeURIComponent(vqh));
      }
    }
  }
  var ready = !!(cur.ready || (cur.targets && cur.targets.length));
  var sameAcct = !ck.unique_id || ck.unique_id === cur.unique_id;
  var runningThis = ck.running && sameAcct;
  $('st_account').textContent = cur.username || '—';
  $('st_uid').textContent = cur.unique_id || '—';
  $('st_targets').textContent = (cur.targets && cur.targets.length) ? cur.targets.join('、') : '未填写';
  $('st_cookie').textContent = cur.has_cookie
    ? ('已保存 ' + cur.cookie_count + ' 项' + (cur.saved_at ? '（' + cur.saved_at + '）' : ''))
    : '未保存';
  if($('st_saved')){ $('st_saved').textContent = cur.saved_at || '—'; }
  if($('savedat')){
    $('savedat').textContent = cur.saved_at ? ('登录成功时间：' + cur.saved_at) : '';
  }


  if(cur._new){ badge('新账号：填个「抖音号」，点下面「开始授权」就能用手机号登录', 'n'); }
  else if(runningThis){ badge('正在检测登录状态…', 'y'); }
  else if(chk && chk.ok && !ready){ badge('已登录（还没填「目标好友」，填完点「保存配置」后可以手动运行）', 'y'); }
  else if(chk && chk.ok){ badge('已登录 - ' + chk.at, 'g'); }
  else if(chk && !chk.ok){ badge('未登录 / Cookie 失效 - ' + chk.at, 'r'); }
  else if(cur.has_cookie){ badge('已保存 Cookie，建议点下面「检测登录状态」确认一下', 'n'); }
  else { badge('尚未授权（还没有登录过）', 'n'); }
  // 顶部常驻的一句话状态：不滚屏也能看到当前账号登录没登录
  var hs = '未授权', hc = 'n';
  if(runningThis){ hs = '检测中'; hc = 'y'; }
  else if(chk && chk.ok){ hs = ready ? '已登录' : '已登录 · 待填好友'; hc = ready ? 'g' : 'y'; }
  else if(chk && !chk.ok){ hs = '未登录'; hc = 'r'; }
  else if(cur.has_cookie){ hs = '待检测'; }
  else if(cur._new){ hs = '新账号'; }
  headBadge(hs, hc);

  // 检测开始/结束：把进度条和秒数开关一次
  if(runningThis && !checkWasRunning){ checkWasRunning = true; startCheckTimer(); }
  else if(!runningThis && checkWasRunning){ checkWasRunning = false; finishCheck(); }
  if(runningThis && ck.stuck){
    $('checkstate').textContent = (ck.message || '') + '　超过 ' + Math.round((ck.frame_age||0)) + ' 秒没有新画面，可能卡住了，可以点下面「强制停止」';
  } else {
    $('checkstate').textContent = runningThis ? ck.message : '';
  }
  // 按钮灰不灰，一律以服务端状态为准（本地锁只是防连点）
  var ckWhy = '';
  if(au.running){ ckWhy = '授权浏览器开着，先点「停止」再检测'; }
  else if(ck.running && !runningThis){ ckWhy = '另一个账号正在检测，等它跑完'; }
  setBtn('bcheck', !!ckWhy, ckWhy ? ('暂时点不了：' + ckWhy) : '');
  $('bcheck').textContent = runningThis ? '停止检测' : '检测登录状态';
  var noAccount = !s.is_admin && !(s.my_ids && s.my_ids.length);
  var runWhy = '';
  if(s.runner.running){ runWhy = '正在执行发送任务，等它跑完'; }
  else if(au.running){ runWhy = '授权浏览器还开着，先点「停止」再运行'; }
  else if(ck.running){ runWhy = '正在检测登录状态，等它跑完'; }
  else if(noAccount){ runWhy = '你还没有绑定抖音号：先在「1 账号」里建一个并授权登录'; }
  setBtn('brun', !!runWhy, runWhy ? ('暂时点不了：' + runWhy) : '');

  var html = '';
  if(chk){
    html += '<div>' + tagHtml(chk.ok ? '通过' : '没通过', chk.ok ? 'g' : 'r') + ' ' + esc(chk.message) + '　<span class="muted">' + esc(chk.at) + '</span></div>';
    if(chk.titles && chk.titles.length){
      html += '<div class="muted">聊天列表里看到的会话：' + chk.titles.slice(0, 12).map(esc).join('、') + (chk.titles.length > 12 ? ' …' : '') + '</div>';
    }
    if(chk.matched && chk.matched.length){
      html += '<div style="color:#166534">已找到的目标好友：' + chk.matched.map(esc).join('、') + '</div>';
    }
    if(chk.missing && chk.missing.length){
      html += '<div style="color:#b91c1c">没在前几屏看到的目标好友：' + chk.missing.map(esc).join('、') + '（可能需要手动往上/下翻一下）</div>';
    }
  } else if(cur.has_cookie){
    // 有 Cookie、但从没真检测过：这是常态（扫码授权不等于是验过），
    // 说清楚 + 指个按钮，别让人以为号坏了
    html += '<div class="muted">这个号还没检测过。点上面的「检测登录状态」跑一遍，'
         + '确认保存的 Cookie 现在还能不能发消息（大约 20-60 秒）。</div>';
  }
  $('checkresult').innerHTML = html;
  renderGuide(s, cur);
}
function renderSubscription(s){
  var sub = (s && s.subscription) || {};
  var statusText = sub.status === 'unlimited' ? '永久有效'
    : (sub.status === 'trial' ? '试用期' : (sub.status === 'active' ? '有效' : (sub.status === 'expired' ? '已到期' : '读取中')));
  var statusEl = $('sub_status');
  if(statusEl){ statusEl.textContent = statusText; }
  if($('sub_remaining')){ $('sub_remaining').textContent = sub.remaining_text || '—'; }
  if($('sub_expires')){ $('sub_expires').textContent = sub.unlimited ? '不会到期' : (sub.expires_text || '—'); }
}
function renderTodaySend(s){
  var state = $('todaySendState'), meta = $('todaySendMeta');
  if(!state || !meta){ return; }
  if(s && s.is_admin){
    state.textContent = '今天的全站发送状态';
    meta.textContent = '请在管理控制台的发送记录中查看';
    return;
  }
  var summary = s && s.today_send || {}, run = summary.record || null;
  var date = String(summary.date || '今天');
  state.className = 'today-state';
  if(!run){
    state.textContent = '今天还没有发送记录';
    meta.textContent = date + ' · 手动运行发送任务后会更新';
    return;
  }
  var labels = {
    ok:['今天发送成功','success'], partial:['今天部分发送成功','partial'],
    failed:['今天发送失败','failed'], no_login:['今天未发送：登录已失效','failed'],
    error:['今天任务出错','failed'], queued:['发送任务排队中','pending'],
    queue_timeout:['发送排队超时','failed'], skipped:['今天的发送已跳过','pending'],
    running:['发送任务进行中','pending'], no_friend:['未找到目标好友','failed']
  };
  var result = labels[run.status] || ['今天任务状态：' + (run.status || '未知'),'pending'];
  state.textContent = result[0];
  state.classList.add(result[1]);
  meta.textContent = [run.account, run.at, run.detail].filter(Boolean).join(' · ');
}
if($('sub_redeem')){
  $('sub_redeem').onclick = function(){
    var code = ($('sub_code').value || '').trim();
    if(!code){ $('sub_result').textContent = '请先输入兑换码'; return; }
    this.disabled = true;
    post('api/redeem', {code: code}).then(function(r){
      $('sub_result').textContent = r.message || r.error || '';
      $('sub_result').style.color = r.ok ? 'var(--ok)' : 'var(--bad)';
      if(r.ok){ $('sub_code').value = ''; refresh(); }
    }).finally(function(){ $('sub_redeem').disabled = false; });
  };
}
// ---- 新手引导：按真实状态点亮每一步，点一下就把人送到该去的地方 ----
var GUIDE_OPEN_KEY = 'guide:open';
function guideSetOpen(open){
  var g = $('guide');
  if(!g){ return; }
  if(open){ g.classList.remove('min'); } else { g.classList.add('min'); }
  var b = $('gtoggle');
  if(b){
    b.textContent = open ? '收起' : '展开';
    b.setAttribute('aria-label', open ? '收起新手引导' : '展开新手引导');
  }
  try { window.localStorage.setItem(GUIDE_OPEN_KEY, open ? '1' : '0'); } catch(e){}
}
// ---- 左侧导航：切换内容面板 ----
var PANELS = {overview:'概览', accounts:'抖音账户配置', tasks:'任务配置', records:'发送记录', me:'我的账号', admin:'管理'};
var PANEL_ORDER = ['overview', 'accounts', 'records', 'me', 'admin'];
function showPanel(go){
  if(go === 'logs'){ go = 'records'; }
  if(go === 'subscription'){ go = 'me'; }
  if(PANELS[go] === undefined){ go = 'overview'; }
  var panelName = go === 'tasks' ? 'accounts' : go;
  PANEL_ORDER.forEach(function(k){
    var p = $('p-' + k);
    if(p){ if(k === panelName){ p.classList.add('on'); } else { p.classList.remove('on'); } }
  });
  var accountPanel = $('p-accounts');
  if(accountPanel){ accountPanel.classList.toggle('task-mode', go === 'tasks'); }
  Array.prototype.forEach.call(document.querySelectorAll('#nav .nav'), function(b){
    if(b.getAttribute('data-go') === go){ b.classList.add('on'); } else { b.classList.remove('on'); }
  });
  var t = $('pageTitle');
  if(t){ t.textContent = PANELS[go]; }
  try { window.localStorage.setItem('panel:go', go); } catch(e){}
  // 切到「日志」页时直接翻到最下面（默认就要看到最新几行）
  if(go === 'logs' && typeof refreshLogs === 'function'){ refreshLogs(true); }
  // 切到「管理」页时立刻拉一次用户列表（原先是展开折叠块触发的）
  if(go === 'admin' && LAST_STATUS){
    USERS_FORCE = true;
    if(typeof renderUsers === 'function'){ renderUsers(LAST_STATUS); }
  }
}
function initNav(){
  Array.prototype.forEach.call(document.querySelectorAll('[data-go]'), function(b){
    b.addEventListener('click', function(){ showPanel(b.getAttribute('data-go')); });
  });
  var saved = '';
  try { saved = window.localStorage.getItem('panel:go') || ''; } catch(e){}
  if(saved === 'logs'){ saved = 'records'; }
  if(saved === 'subscription'){ saved = 'me'; }
  // 「管理」对普通用户是隐藏的：上次退出时停在那一页的话，回落到抖音账户
  if(saved === 'admin' && $('navadmin') && $('navadmin').hidden){ saved = 'overview'; }
  showPanel(saved || 'overview');
}
function guideScrollTo(id){
  var el = $(id);
  if(!el){ return; }
  // 目标可能在别的面板里：先切过去，不然 scrollIntoView 打在 display:none 上没反应
  var node = el, host = null;
  while(node && node !== document.body){
    if(node.id && node.className && String(node.className).indexOf('panel') >= 0){ host = node; break; }
    node = node.parentNode;
  }
  if(host && String(host.className).indexOf('on') < 0){
    showPanel(String(host.id).replace(/^p-/, ''));
    setTimeout(function(){ guideScrollTo(id); }, 60);
    return;
  }
  if(el.scrollIntoView){ el.scrollIntoView({behavior:'smooth', block:'start'}); }
  if(el.classList){
    el.classList.remove('sec-hl');
    void el.offsetWidth;                 // 强制回流，动画才会重新播一次
    el.classList.add('sec-hl');
    setTimeout(function(){ el.classList.remove('sec-hl'); }, 1800);
  }
}
function renderGuide(s, cur){
  if(!$('glist')){ return; }
  cur = cur || {};
  var chk = realCheck(cur.check);
  var hasUid = !!cur.unique_id;
  var authed = hasUid && !!cur.has_cookie && (!chk || chk.ok !== false);
  var checked = !!(chk && chk.ok);
  var ready = !!(cur.ready || (cur.targets && cur.targets.length));
  var ran = !!(s && s.runner && s.runner.returncode !== null && s.runner.returncode !== undefined);
  var done = [hasUid, authed, checked, ready, ran];
  var current = -1;
  for(var i = 0; i < done.length; i++){ if(!done[i]){ current = i; break; } }
  for(var k = 0; k < done.length; k++){
    var li = $('g' + (k + 1));
    if(li){
      li.className = done[k] ? 'done' : (k === current ? 'cur' : '');
      var dot = li.querySelector ? li.querySelector('.gdot') : null;
      if(dot){ dot.textContent = done[k] ? '✓' : String(k + 1); }
    }
    var act = $('gact' + (k + 1));
    if(act){ act.hidden = (k !== current); }
  }
}
function guideAction(step){
  if(step === 1 || step === 4){
    var target = (step === 1) ? $('f_uid') : $('f_targets');
    guideScrollTo(step === 1 ? 'acctbox' : 'cfgbox');
    if(target && target.focus){ setTimeout(function(){ target.focus(); }, 400); }
    return;
  }
  if(step === 2){ guideScrollTo('authbox'); if($('bstart') && !$('bstart').disabled){ $('bstart').click(); } return; }
  if(step === 3){ guideScrollTo('statusbox'); if($('bcheck') && !$('bcheck').disabled){ $('bcheck').click(); } return; }
  if(step === 5){ guideScrollTo('runbox'); if($('brun') && !$('brun').disabled){ $('brun').click(); } return; }
}
(function(){
  var g = $('guide');
  if(!g){ return; }
  var saved = null;
  try { saved = window.localStorage.getItem(GUIDE_OPEN_KEY); } catch(e){}
  guideSetOpen(saved !== '0');
  if($('gtoggle')){ $('gtoggle').onclick = function(){ guideSetOpen(g.classList.contains('min')); }; }
  if($('glist') && $('glist').addEventListener){
    $('glist').addEventListener('click', function(ev){
      var node = ev.target;
      while(node && node !== $('glist')){
        if(node.id && /^gact[1-5]$/.test(node.id)){ guideAction(parseInt(node.id.slice(4), 10)); return; }
        node = node.parentNode;
      }
      var li = ev.target;
      while(li && li !== $('glist') && !(li.getAttribute && li.getAttribute('data-goto'))){ li = li.parentNode; }
      if(li && li !== $('glist') && li.getAttribute){ guideScrollTo(li.getAttribute('data-goto')); }
    });
  }
})();
var AUTH_COLOR = {idle:'n', starting:'y', waiting:'y', need_id:'y', authorized:'g', error:'r'};
// ---- 授权向导弹窗：只把 api/status 投影成一幕一幕，自己不做任何授权逻辑 ----
// 二维码不在这个弹窗里：它直接显示在第 2 步的页面上（扫码是主路径，不该藏在弹窗后面）
var WZ = {open:false, act:'', sent:'', sending:false, doneAt:0, dismissed:false, kind:'code', auto:false};
// 「通用输入框」搬到了浏览器画面下面，不归授权弹窗管，所以自己一个状态
var ANY = {sending:false};
var WZ_ACTS = ['scan', 'sms', 'done', 'err'];
var WZ_STEP = {scan:0, sms:1, err:2, done:3};
var WZ_NAME = {scan:'确认', sms:'输验证码', done:'完成', err:'出错了'};
function wzDigits(v){
  return String(v === null || v === undefined ? '' : v).replace(/[^0-9]/g, '').slice(0, 8);
}
function wzPickAct(au, lv){
  au = au || {}; lv = lv || {};
  var ph = String(au.phase || '');
  if(lv.mine === false){ return 'err'; }                     // 别人账号在跑：画面不归我
  if(au.state === 'authorized' || ph === 'done'){ return 'done'; }
  if(au.stuck || au.error || au.state === 'error'){ return 'err'; }
  if(ph === 'sms'){ return 'sms'; }
  if(ph === 'scanned' || ph === 'manual'){ return 'scan'; }
  return '';
}
function wizardShow(){
  WZ.open = true;
  WZ.dismissed = false;
  // 用户自己点开的（气泡 / 按钮）：不算"自动弹出来"，
  // 所以后面「验证码提交后自动收起」那套不该把他这条撵走
  WZ.auto = false;
  var w = $('authwiz');
  if(w){ w.hidden = false; }
  var b = $('authbubble');
  if(b){ b.hidden = true; }
  if(WZ.act === 'sms'){
    var c = (WZ.kind === 'pwd') ? $('wzpwd') : $('wzcode');
    if(c && c.focus){ c.focus(); }
  }
}
function wizardHide(dismissed){
  WZ.open = false;
  var w = $('authwiz');
  if(w){ w.hidden = true; }
  // 用户自己点「收起」才算拒绝；换幕/没内容时的自动收起不算
  if(dismissed){ WZ.dismissed = true; }
}
function wzSetAct(act){
  var changed = (WZ.act !== act);   // 只有「换幕」才做一次性动作，反复同步是安全的
  WZ.act = act;
  WZ_ACTS.forEach(function(a){
    var el = $('wzact-' + a);
    if(el){ el.hidden = (a !== act); }
  });
  var cur = WZ_STEP[act];
  for(var i = 0; i < 4; i++){
    var d = $('wzdot' + i);
    if(d){ d.className = 'wz-dot' + (i < cur ? ' on' : (i === cur ? ' cur' : '')); }
  }
  var nm = $('wzstepname');
  if(nm){ nm.textContent = (act === 'sms' && WZ.kind === 'pwd') ? '输密码' : (WZ_NAME[act] || ''); }
  if(changed){
    // 换到新的一幕：把上一次「收起」的记录清掉，不然该提示时弹不出来
    WZ.dismissed = false;
    if(act === 'sms'){
      var c = $('wzcode'), pw0 = $('wzpwd');
      if(c){ c.value = ''; }
      if(pw0){ pw0.value = ''; pw0.type = 'password'; }
      var fb = (WZ.kind === 'pwd') ? pw0 : c;      // 聚焦用户该填的那个
      if(fb && fb.focus){ fb.focus(); }
      WZ.sent = '';
      var sres = $('wzsmsresult');
      if(sres){ sres.textContent = ''; }
    }
    if(act === 'done'){ WZ.doneAt = 0; }
  }
}
function wzSubmit(){
  // 二级验证可能是「填验证码」，也可能是「填登录密码」——两条通道分开走：
  // 验证码只留数字、后端按数字校验；密码原样送，后端不过滤字符。
  if(WZ.kind === 'pwd'){
    var pe = $('wzpwd');
    var pwd = pe ? String(pe.value || '') : '';
    if(!pwd){
      note('请填抖音的登录密码', false);
      if(pe && pe.focus){ pe.focus(); }
      return;
    }
    if(WZ.sending){ return; }
    WZ.sending = true;
    var ps = $('wzsmsstate');
    if(ps){ ps.textContent = '正在提交…'; }
    post('api/auth/pwd', {password: pwd, unique_id: curUid()}).then(function(r){
      WZ.sending = false;
      var ps2 = $('wzsmsstate');
      if(ps2){ ps2.textContent = ''; }
      // 送出去了就把输入框清掉：密码不在页面上多留一秒。
      // 失败（比如浏览器没在跑）不清 —— 那等于没送出去，别逼用户白打一遍。
      if(r.ok && pe){ pe.value = ''; pe.type = 'password'; }
      if(!r.ok){ note(r.error || '提交失败，稍后再试', false); }
    }, function(){
      WZ.sending = false;
      var ps3 = $('wzsmsstate');
      if(ps3){ ps3.textContent = ''; }
      note('提交失败，稍后再试', false);
    });
    return;
  }
  var el = $('wzcode');
  var code = wzDigits(el && el.value);
  if(!code){
    note('请填手机上收到的那串数字验证码', false);
    if(el && el.focus){ el.focus(); }
    return;
  }
  if(WZ.sending){ return; }
  WZ.sending = true;
  var st = $('wzsmsstate');
  if(st){ st.textContent = '正在提交…'; }
  post('api/auth/sms', {code: code, unique_id: curUid()}).then(function(r){
    WZ.sending = false;
    var st2 = $('wzsmsstate');
    if(st2){ st2.textContent = ''; }
    if(!r.ok){ note(r.error || '提交失败，稍后再试', false); }
  }, function(){
    WZ.sending = false;
    var st3 = $('wzsmsstate');
    if(st3){ st3.textContent = ''; }
    note('提交失败，稍后再试', false);
  });
}
// 「通用输入框」挂在浏览器画面下面，不属于授权弹窗。
// 内容原样送（后端也不过滤）；后端敲进抖音之后**不**自动点「验证」，
// 所以提交成功也**不**清空输入框 —— 用户可能还要照着抖音的提示改一改再送一次。
function anySubmit(){
  var el = $('anytext');
  var body = el ? String(el.value || '') : '';
  if(!body){
    note('先在通用输入框里打点东西', false);
    if(el && el.focus){ el.focus(); }
    return;
  }
  if(ANY.sending){ return; }
  ANY.sending = true;
  var rs = $('anyresult');
  if(rs){ rs.hidden = false; rs.className = ''; rs.textContent = '正在提交…'; }
  var done = function(r){
    ANY.sending = false;
    if(r && r.ok){ return; }          // 成功的文案由 refresh() 里的 renderAnyBox 统一渲染
    var r2 = $('anyresult');
    if(r2){ r2.hidden = false; r2.className = 'bad'; r2.textContent = (r && r.error) || '提交失败，稍后再试'; }
  };
  post('api/auth/text', {text: body, unique_id: curUid()}).then(function(r){
    done(r);
  }, function(){
    done(null);
  });
}
function renderAnyBox(au){
  // 通用输入框那条通道的结果：跟授权弹窗无关，跟着刷新自己渲染
  var rs = $('anyresult');
  if(!rs){ return; }
  var tr = (au && au.text_result) || {};
  var msg = String(tr.message || '');
  rs.hidden = !msg;
  rs.textContent = msg;
  rs.className = msg ? (tr.ok ? 'ok' : 'bad') : '';
}
function renderWizard(au, lv, sr){
  au = au || {}; lv = lv || {};
  var act = wzPickAct(au, lv);
  if(!act){
    wizardHide();                       // 没有一幕可放，弹窗就必须不在屏幕上
    var b0 = $('authbubble');
    if(b0){ b0.hidden = true; }
    WZ.act = '';
    WZ.qrHash = '';
    WZ.doneAt = 0;
    return;
  }
  // 验证码这一幕必须用户动手（页面上那个框被弹窗挡着），没人手动收起过就自动弹出来
  if(act === 'sms' && !WZ.open && !WZ.dismissed){ wizardShow(); WZ.auto = true; }

  // 「填完验证码以后那个窗口就自己关掉」：验证码一提交，阶段就从 sms 往前走，
  // 这时候如果这个弹窗是**自动弹出来的**，就自动收起来（用户自己点开的不动）。
  // 特意放过 done 那一幕：登录成功要让他看到"收 Cookie"的反馈，那一幕自己会收。
  if(WZ.open && WZ.auto && act !== 'sms' && act !== 'done'){ wizardHide(); }
  if(act === 'scan'){
    var stip = $('wzscantip');
    // 二级验证这一档是「手动验证」：面板不替你操作，得你自己在那块可点画面里弄。
    // 画面和通用输入框都在主界面上（被这个弹窗盖着看不见），所以这里给说明 + 一个按钮。
    // 手动模式下 #wzmanualtip 已经把话说全了，上面那行状态就别再重复一遍（两段几乎一样的
    // 话叠在一起，用户会以为出了什么事）。
    var mn = $('wzmanual');
    if(stip){
      if(au.manual){ stip.hidden = true; }
      else{
        stip.hidden = false;
        stip.textContent = au.message || '已经扫到了，请在手机上点「确认登录」…';
      }
    }
    if(mn){ mn.hidden = !au.manual; }
    if(au.manual){
      var mnt = $('wzmanualtip');
      if(mnt){
        mnt.textContent = '抖音要求二级验证，这一步得你自己来：上面那块画面可以直接点 —— '
          + '点「用原设备扫码」或者人脸都行；要打字就用主界面画面下面那个通用输入框，'
          + '填完点「提交」。我同时会帮你点「用原设备扫码」、把二维码摆出来；'
          + '登录成功我会自动把它收起来。';
      }
    }
  }
  if(act === 'sms'){
    // 二级验证有「验证码」和「登录密码」两种：换种时把另一套控件清掉、藏起来
    var kind = (au.sms_kind === 'pwd') ? 'pwd' : 'code';
    if(WZ.kind !== kind){
      WZ.kind = kind;
      WZ.sent = '';
      var c1 = $('wzcode'); if(c1){ c1.value = ''; }
      var p1 = $('wzpwd'); if(p1){ p1.value = ''; p1.type = 'password'; }
    }
    var crow = $('wzcoderow'), prow = $('wzpwdrow');
    if(crow){ crow.hidden = (kind !== 'code'); }
    if(prow){ prow.hidden = (kind !== 'pwd'); }
    var chint = $('wzcodehint');
    if(chint){
      chint.textContent = (kind === 'pwd')
        ? '提交后我替你填进抖音并点「验证」；密码只在这一刻用一次，不写日志、不落盘。下面的画面就是抖音那边的样子。'
        : '填满 6 位会自动提交；提交后我替你填进抖音并点「验证」，下面的画面就是抖音那边的样子。';
    }
    var hint = $('wzsmshint');
    if(hint){
      hint.textContent = au.sms_hint || ((kind === 'pwd')
        ? '抖音要你做一次安全验证：这次要的是抖音登录密码，填在下面，我替你填进抖音并点「验证」。'
        : '抖音要你做一次安全验证：把手机收到的验证码填在下面，我替你填进抖音并点「验证」。');
    }
    var wait = (typeof au.sms_click_left === 'number') ? au.sms_click_left : null;
    var settle = (typeof au.sms_settle_left === 'number') ? au.sms_settle_left : null;
    var bar = $('wzbar'), fill = $('wzbarfill'), bp = $('wzbarp');
    var pct = null, line = '';
    if(wait !== null && wait > 0){
      var wt = Number(au.sms_click_total || 3.5) || 3.5;
      pct = (1 - wait / wt) * 100;
      line = '已经替你敲进抖音了，' + Math.max(1, Math.ceil(wait)) + ' 秒后点「验证」…';
    } else if(settle !== null && settle > 0){
      var stt = Number(au.sms_settle_total || 3) || 3;
      pct = (1 - settle / stt) * 100;
      line = '已经点了「验证」，等抖音的结果…';
    }
    if(bar){ bar.hidden = (pct === null); }
    if(bp){ bp.hidden = (pct === null); bp.textContent = line; }
    if(fill && pct !== null){ fill.style.width = Math.max(0, Math.min(100, pct)) + '%'; }
    var sres = $('wzsmsresult');
    if(sres){
      if(sr && sr.message){ sres.textContent = String(sr.message); sres.style.color = sr.ok ? '#166534' : '#b91c1c'; }
    }
    // 抖音那边的画面：填过码就把「填码那一刻」定格的那张摆出来，没填过才看实时画面。
    // 原因是实时画面 2 秒才一张，而抖音的框一填满就自己往下走，很容易正好拍空。
    var lvb = $('wzlivbox'), lvi = $('wzsmslive'), lvc = $('wzlivcap');
    if(lvb){
      var pat = (typeof au.sms_proof_at === 'number' && au.sms_proof_at > 0) ? au.sms_proof_at : null;
      var hasImg = !!lv.has_image && lv.mine !== false;
      lvb.hidden = !(pat !== null || hasImg);
      if(pat !== null && lvi){
        var pmk = 'p' + Math.round(pat);
        if(lvi.getAttribute('data-pmk') !== pmk){
          lvi.setAttribute('data-pmk', pmk);
          lvi.src = withUid('api/smsproof?t=' + pmk);
        }
        if(lvc){
          var pag = Math.round(Date.now() / 1000 - pat);
          lvc.textContent = (pag >= 0)
            ? ('这是我往抖音的框里填码那一刻的画面（' + pag + ' 秒前）')
            : '这是我往抖音的框里填码那一刻的画面';
        }
      } else if(hasImg && lvi && WZ.open){
        lvi.removeAttribute('data-pmk');
        lvi.src = withUid('api/screenshot?t=' + Date.now());
        if(lvc){
          lvc.textContent = (typeof lv.frame_age === 'number')
            ? ('这是抖音那边的实时画面（约 ' + lv.frame_age + ' 秒前的样子）')
            : '这是抖音那边的实时画面';
        }
      }
    }
    var lve = $('wzsmslen');
    if(lve){
      var bl = (typeof au.sms_box_len === 'number') ? au.sms_box_len : null;
      var ba = (typeof au.sms_box_age === 'number') ? au.sms_box_age : null;
      var unit = (WZ.kind === 'pwd') ? '位密码' : '位数字';
      if(bl === null || ba === null || ba > 30){
        lve.className = 'wz-tip muted';
        lve.textContent = '还没往抖音的框里填 —— 填进去后这里会立刻显示进去了几位。';
      } else if(bl > 0){
        lve.className = 'wz-tip ok';
        lve.textContent = '抖音的框里现在有 ' + bl + ' ' + unit + '。';
      } else {
        lve.className = 'wz-tip muted';
        lve.textContent = '抖音的框里现在还是空的（没进去）。';
      }
    }
  }
  if(act === 'err'){
    var et = $('wzerrtip');
    var msg = (lv.mine === false)
      ? '另一个账号正在使用授权浏览器，画面只对管理员开放。等它跑完再试。'
      : (au.stuck ? '画面 90 秒没更新，好像卡住了：点「强制停止」，再重新来一遍。'
        : String(au.error || au.message || (sr && sr.message) || '授权出了点问题，重试一下。'));
    if(et){ et.textContent = msg; }
    var fb = $('wzforce');
    if(fb){ fb.hidden = !au.stuck; }
  }
  if(act === 'done' && !WZ.doneAt){
    WZ.doneAt = Date.now();
    setTimeout(function(){
      wizardHide();
      var b1 = $('authbubble');
      if(b1){ b1.hidden = true; }
      if(typeof refresh === 'function'){ refresh(); }
    }, 1500);
  }
  var bub = $('authbubble');
  if(bub){
    var needMe = (act === 'sms' || act === 'scan');
    bub.hidden = WZ.open || !needMe;
    bub.className = (act === 'sms') ? 'sm danger' : 'sm';
    bub.textContent = (act === 'sms')
      ? ((WZ.kind === 'pwd') ? '该填抖音的登录密码了 —— 点这里打开密码窗口'
                             : '该填抖音的验证码了 —— 点这里打开验证码窗口')
      : '已经扫到了 —— 点这里看确认进度';
  }
  wzSetAct(act);
}
// 弹窗的按钮绑定必须等 DOM 解析完：这块 HTML 写在 <script> 后面，
// 之前在脚本里直接绑定，元素还不存在，于是「收起 / 重试 / 强制停止 / 授权气泡」全是死的。
function bindAuthWizard(){
  if($('wzclose2')){ $('wzclose2').onclick = function(){ wizardHide(true); }; }
  if($('wzclose')){ $('wzclose').onclick = function(){ wizardHide(true); }; }
  if($('authbubble')){ $('authbubble').onclick = wizardShow; }
  // 「收起弹窗，去画面里操作」：二级验证手动模式用 —— 收起弹窗 + 把可点画面摊开
  if($('wzgotoshot')){
    $('wzgotoshot').onclick = function(){
      wizardHide(true);
      shotManual = false;
      setShot(true);
    };
  }
  if($('wzsubmit')){ $('wzsubmit').onclick = wzSubmit; }
  if($('wzcode')){
    $('wzcode').addEventListener('input', function(){
      this.value = wzDigits(this.value);
      if(this.value.length >= 6 && this.value !== WZ.sent){ WZ.sent = this.value; wzSubmit(); }
    });
    $('wzcode').addEventListener('keydown', function(e){
      if(e.key === 'Enter'){ e.preventDefault(); wzSubmit(); }
    });
  }
  if($('wzpwdsubmit')){ $('wzpwdsubmit').onclick = wzSubmit; }
  if($('wzpwd')){
    $('wzpwd').addEventListener('keydown', function(e){
      if(e.key === 'Enter'){ e.preventDefault(); wzSubmit(); }
    });
  }
  if($('wzpwdeye')){
    $('wzpwdeye').onclick = function(){
      var p = $('wzpwd');
      if(!p){ return; }
      var show = (p.type === 'password');
      p.type = show ? 'text' : 'password';
      this.textContent = show ? '隐藏' : '显示';
      this.setAttribute('aria-pressed', show ? 'true' : 'false');
      this.setAttribute('aria-label', show ? '隐藏密码' : '显示密码');
    };
  }
  if($('wzretry')){
    $('wzretry').onclick = function(){
      var uid = (CUR_ACCT && CUR_ACCT.unique_id) || '';
      if(!uid && $('cfg')){ uid = String($('cfg').unique_id.value || '').trim(); }
      var name = ($('cfg') && $('cfg').username) ? $('cfg').username.value : '';
      if(!uid){ note('先选好账号再重试', false); return; }
      post('api/browser/stop', {unique_id: curUid()}).then(function(){
        setTimeout(function(){ wizardHide(); startAuth(uid, name); }, 1200);
      });
    };
  }
  if($('wzforce')){
    $('wzforce').onclick = function(){ post('api/browser/stop', {unique_id: curUid()}).then(function(r){ flash(r.message || r.error || '', !!r.ok); }); };
  }
}
if(typeof document !== 'undefined' && document.addEventListener){
  document.addEventListener('keydown', function(e){
    if((e.key === 'Escape' || e.keyCode === 27) && WZ.open){ wizardHide(true); }
  });
}
// ---- 公告条：管理员在 /admin 里写的正文 + 联系方式，给全体用户看 ----
// 关掉之后记的是**内容指纹**而不是"关过"这个事实：
// 管理员把公告改了，指纹跟着变，公告会重新出现，不会出现改了但没人看得到。
var NOTICE_SEEN = '';
var noticeVer = '';
try { NOTICE_SEEN = window.localStorage.getItem('notice:seen') || ''; } catch(e){ NOTICE_SEEN = ''; }
function noticeEsc(t){
  return esc(String(t === null || t === undefined ? '' : t)).replace(/\r\n|\r|\n/g, '<br>');
}
function renderNotice(n){
  var el = $('notice');
  if(!el){ return; }
  n = n || {};
  var text = String(n.text || ''), contact = String(n.contact || '');
  var ver = String(n.version || '');
  if(!text && !contact){ el.hidden = true; return; }
  if(ver && ver === NOTICE_SEEN){ el.hidden = true; return; }
  noticeVer = ver;
  var tb = $('notice_text');
  tb.innerHTML = noticeEsc(text);
  tb.hidden = !text;
  var cb = $('notice_contact');
  cb.innerHTML = contact ? ('<b>管理员联系方式</b><br>' + noticeEsc(contact)) : '';
  cb.hidden = !contact;
  var fb = $('notice_foot');
  var at = String(n.updated_at || '');
  fb.textContent = at ? ('公告更新时间：' + at) : '';
  fb.hidden = !at;
  el.hidden = false;
}
function noticeClose(){
  var el = $('notice');
  if(el){ el.hidden = true; }
  if(noticeVer){
    NOTICE_SEEN = noticeVer;
    try { window.localStorage.setItem('notice:seen', noticeVer); } catch(e){}
  }
}
$('notice_x').onclick = noticeClose;
// ---- 定向消息：管理员单独发给我的一条条通知（和公告条同一位置，在它下面）----
// 「知道了」关掉之后把这条消息的 id 记在浏览器本地。消息本身发出去就不可改，
// 所以 id 就是天然的内容指纹：同一台设备不会再看到第二次；
// 但服务端的「已读」是另一回事 —— 换台设备/换个浏览器还会看到（那时它已经是「已读」状态）。
var MSG_SEEN = {};
var MSG_SEEN_MAX = 400;   // localStorage 里最多记多少个 id，别让它无限涨
var MSG_SHOW_MAX = 20;    // 一次最多铺多少条，超出的提示"还有几条，收起来就会接着显示"
var MSG_READ_SENT = {};   // 本次会话已回传过「已读」的 id（status 每 2 秒刷一次，不能反复提交）
var MSG_SIG = '';         // 当前渲染内容的指纹：没变就不重建 DOM（免得闪、免得选中被清掉）
try {
  var MSG_RAW = window.localStorage.getItem('msg:seen');
  var MSG_ARR = MSG_RAW ? JSON.parse(MSG_RAW) : [];
  if(MSG_ARR && MSG_ARR.length){
    MSG_ARR.forEach(function(x){ MSG_SEEN[String(x)] = 1; });
  }
} catch(e){ MSG_SEEN = {}; }
function msgSaveSeen(){
  try {
    var ids = Object.keys(MSG_SEEN);
    if(ids.length > MSG_SEEN_MAX){ ids = ids.slice(ids.length - MSG_SEEN_MAX); }
    window.localStorage.setItem('msg:seen', JSON.stringify(ids));
  } catch(e){}
}
function msgEsc(t){
  return esc(String(t === null || t === undefined ? '' : t)).replace(/\r\n|\r|\n/g, '<br>');
}
function msgMarkRead(ids, done){
  var fresh = (ids || []).filter(function(x){ return x && !MSG_READ_SENT[x]; });
  if(!fresh.length){ if(done){ done(0); } return; }
  fresh.forEach(function(x){ MSG_READ_SENT[x] = 1; });
  post('api/message/read', { ids: fresh }).then(function(r){
    // 回传失败就把本地标记撤掉，下一轮刷新再试；否则这条会永远停在"未读"上
    if(!(r && r.ok)){ fresh.forEach(function(x){ delete MSG_READ_SENT[x]; }); }
    if(done){ done(r && r.ok ? (r.changed || 0) : 0); }
  });
}
function renderMessages(m){
  var box = $('msgs');
  if(!box){ return; }
  m = m || {};
  var show = [], older = 0;
  (m.inbox || []).forEach(function(row){
    if(MSG_SEEN[String(row.id)]){ return; }   // 我已经点过「知道了」的，不再显示
    show.push(row);
  });
  if(show.length > MSG_SHOW_MAX){
    older = show.length - MSG_SHOW_MAX;
    show = show.slice(0, MSG_SHOW_MAX);
  }
  if(!show.length){
    MSG_SIG = '';
    box.hidden = true;
    box.innerHTML = '';
    return;
  }
  var unread = Number(m.unread || 0);
  var sig = show.map(function(row){ return row.id + (row.read ? '1' : '0'); }).join(',') + '|' + older;
  if(sig !== MSG_SIG){
    MSG_SIG = sig;
    var html = '<div class="m-cap">我的消息' + (unread > 0 ? ('（' + unread + ' 条未读）') : '') + '</div>';
    html += show.map(function(row){
      var at = String(row.created_at || '');
      var from = String(row.from || '管理员') || '管理员';
      return '<div class="msg" data-mid="' + esc(row.id) + '">'
        + '<div class="m-head">'
        + '<b>' + (row.read ? '消息' : '新消息') + '</b>'
        + (row.read ? '' : '<span class="m-dot" aria-hidden="true"></span>')
        + '<span class="sp"></span>'
        + '<span class="m-at">' + esc(from) + (at ? ('　' + esc(at)) : '') + '</span>'
        + '<button class="ghost sm" type="button" data-mclose="' + esc(row.id) + '">知道了</button>'
        + '</div>'
        + '<div class="m-body">' + msgEsc(row.text) + '</div>'
        + '</div>';
    }).join('');
    if(older){
      html += '<div class="m-cap">还有 ' + older + ' 条更早的消息，点「知道了」把上面这些收起来就会露出来</div>';
    }
    box.innerHTML = html;
  }
  box.hidden = false;
  // 铺出来就算看过了：把**还没读过的**回传已读（已经是已读的不用再报一次）。
  // 后端只认自己收件箱里的那些，所以这里报什么都改不到别人。
  msgMarkRead(show.filter(function(row){ return !row.read; }).map(function(row){ return row.id; }));
}
function msgDismiss(mid){
  if(!mid){ return; }
  MSG_SEEN[String(mid)] = 1;
  msgSaveSeen();
  msgMarkRead([mid]);
  MSG_SIG = '';
  if(LAST_STATUS){ renderMessages(LAST_STATUS.messages); }
}
if($('msgs')){
  // 用事件委托：每 2 秒可能会重建一次内容，直接绑在按钮上会被重渲染冲掉
  $('msgs').addEventListener('click', function(e){
    var el = e.target;
    while(el && el !== this && !(el.getAttribute && el.getAttribute('data-mclose'))){ el = el.parentNode; }
    if(el && el !== this && el.getAttribute){ msgDismiss(el.getAttribute('data-mclose')); }
  });
}
function refresh(){
  fetch('api/status').then(function(r){ return r.json(); }).then(function(s){
    LAST_STATUS = s;
    applyAccounts(s);
    renderStatus(s);
    renderTodaySend(s);
    renderSubscription(s);
    applyRole(s);
    renderUsers(s);
    renderRelogin(s.relogin);
    renderNotice(s.notice);
    renderMessages(s.messages);
    var au = s.auth || {}, lv = s.live || {}, ck2 = s.checker || {};
    // 授权浏览器闲着多久会自动关：写在状态行里，别让人以为它会一直开着
    var idleText = '';
    if(au.running && typeof au.idle_left === 'number' && au.idle_left > 0){
      var il = au.idle_left;
      idleText = '　' + (il >= 60 ? (Math.ceil(il / 60) + ' 分钟') : (il + ' 秒')) + '没人操作就自动关掉浏览器';
    }
    // 状态行跟着当前页签说话：二维码模式引导用户用抖音 App 确认，手动模式引导用户操作画面
    var auMsg = String(au.message || '');
    if(String(au.phase || '') === 'qr' && !au.error){
      auMsg = wantManual
        ? '手动授权模式：抖音登录页已经打开了，在下面的画面里自己操作就行。'
        : (wantQr ? '请用抖音 App 扫描上方二维码，并在手机上确认登录。' : auMsg);
    }
    $('authstate').innerHTML = '<span class="dot ' + (AUTH_COLOR[au.state] || 'n') + '"></span><span>'
      + esc(auMsg) + (au.error ? esc('（' + au.error + '）') : '')
      + (au.stuck ? esc('　画面 90 秒没更新，好像卡住了，请点下面的「强制停止」') : '')
      + esc(idleText) + '</span>';

    // ---- 二维码：手动授权和扫码授权都展示 ----
    var qh = String(au.qr_hash || '');
    var ph = String(au.phase || '');
    var wantSms = (ph === 'sms');
    var qrFallback = wantQr && ph === 'manual';
    // 没二维码 / 不是我的浏览器 / 这张图刚才没取下来，都不摆这个块（宁可没有，也不留一张破图）
    var showQr = (wantManual || wantQr) && ph === 'qr' && !au.verify_qr && lv.mine !== false && !!qh && qh !== qrFailedHash;
    $('qrarea').hidden = !showQr;
    if(qh && qh !== lastQrHash){
      lastQrHash = qh;
      $('qrimg').hidden = false;
      $('qrimg').src = withUid('api/qr?h=' + encodeURIComponent(qh));
    } else if(!qh && lastQrHash){
      // 浏览器关了就把旧二维码从页面上抹掉，别留着别人账号的登录码
      lastQrHash = '';
      $('qrimg').removeAttribute('src');
    }
    if($('manualscreentip')){
      // 先隐藏提示，等下面确实拿到一张实时画面后再显示，避免空画面时提前弹出。
      $('manualscreentip').hidden = true;
      var manualTipTitle = $('manualscreentip').querySelector('strong');
      if(manualTipTitle){
        manualTipTitle.textContent = au.manual
          ? '二级验证画面可以直接点击'
          : (qrFallback ? '二维码没识别到，可以点下面画面继续' : '下方画面可以直接点击');
      }
    }
    if(qrFallback && !shotManual){ setShot(true); }
    lastWantSms = wantSms;
    // 这台浏览器正在给谁授权：和上面选中的账号不是同一个时，说清楚并给个一键切换
    var qo = $('qrowner'), qot = $('qrownertext'), sw = $('bswitchwho');
    if(qo){
      var ownerUid = String(lv.owner || '');
      var curUid = String((CUR_ACCT && CUR_ACCT.unique_id) || '');
      if(!curUid && $('cfg') && $('cfg').unique_id){ curUid = String($('cfg').unique_id.value || '').trim(); }
      var otherOwner = (au.running && ownerUid && ownerUid !== '__other__' && ownerUid !== curUid) ? ownerUid : '';
      qo.hidden = !otherOwner;
      if(otherOwner){
        if(qot){ qot.textContent = '浏览器现在给「' + otherOwner + '」授权，和上面选中的账号不是同一个。'; }
        if(sw){
          sw.hidden = false;
          sw.textContent = '切到「' + otherOwner + '」';
          sw.onclick = function(){ selectAccount(otherOwner); };
        }
      } else if(sw){
        sw.hidden = true;
      }
    }
    var diag = '';
    if(au.page_hint){ diag = String(au.page_hint); }
    else if(ph === 'scanned'){ diag = au.message || '已经扫到了，正在等抖音确认…'; }
    else if(ph === 'manual'){ diag = au.message || ''; }
    $('qrdiag').textContent = diag;
    // 验证码只留一个入口：授权弹窗里的输入框（见 wzSubmit）
    renderWizard(au, lv, au.sms_result || null);
    // 通用输入框在浏览器画面下面（不在弹窗里），跟着同一份状态自己刷新
    renderAnyBox(au);

    // ---- 二级验证的「手动模式」----
    // 抖音这一步要用户自己操作：把「浏览器画面」摊开、显示"这块可以直接点"的提示条。
    // 只在**刚进入**手动模式那一下强制摊开（进这一阶段是一次性事件，不是反复弹），
    // 之后用户自己收起就不会再被顶开；退出手动模式（登录成功）时提示条自己收掉。
    var manual = !!au.manual;
    if(manual && !manualOpened){
      manualOpened = true;
      shotManual = false;       // 进了新阶段，重置"用户手动收起过"
      shotAutoDone = true;      // 别让下面那段通用自动摊开再抢一次
      setShot(true);
    }
    if(!manual){ manualOpened = false; }
    // 扫码/检测一开跑就把画面推出来；检测到登录成功，自动收起来。
    // 只认「我自己的」浏览器/检测：别人账号开跑时，不该把我看的画面收走
    var myUid = (CUR_ACCT && CUR_ACCT.unique_id) || '';
    var busyMine = (!!au.running && lv.mine !== false)
      || (!!ck2.running && (IM_ADMIN || ck2.unique_id === myUid));
    if(busyMine && !busyLast){
      shotAutoDone = false; shotManual = false; shotDoneClosed = false; manualOpened = false;
    }
    busyLast = busyMine;
    var loginOk = (au.state === 'authorized') || (ck2.state === 'done' && ck2.ok === true);
    if(loginOk && !shotDoneClosed){
      shotDoneClosed = true;
      if(shotOpen){ setShot(false); }
    }
    var rs = $('runstate');
    rs.textContent = s.runner.running
      ? ('运行中…' + (s.runner.stuck ? '　超过 3 分钟没有新日志，可能卡住了，可以点下面的「强制停止」' : ''))
      : (s.runner.returncode === null ? '尚未运行' : ('上次退出码 ' + s.runner.returncode));
  setBtn('bstart', au.running || !!ck2.running);
  if($('hbstart')){ $('hbstart').disabled = $('bstart').disabled; $('hbstart').title = $('bstart').title; }
  var ha = $('headacct');
  if(ha){ ha.textContent = $('st_account').textContent || '—'; }
    // 按钮点不动的时候要说清楚为什么，不然用户只会觉得"坏了"
    var pool = s.pool || {};
    var poolFull = !!pool.size && (pool.used || 0) >= pool.size;
    var busy = '';
    if(au.running){ busy = '你的授权浏览器已经开着，可以点「重启授权」重新获取二维码'; }
    else if(s.checker && s.checker.running){ busy = '正在检测登录状态，等它跑完'; }
    else if(poolFull){
      busy = '现在 ' + pool.used + '/' + pool.size + ' 个授权都有人在用，最快约 '
           + (pool.wait_text || fmtWait(pool.wait_seconds)) + '后空出（对方点「停止」会立刻空出）';
    }
    setBtn('bstart', au.running || !!(s.checker && s.checker.running) || poolFull);
    if($('hbstart')){ $('hbstart').disabled = $('bstart').disabled; }
    $('bstart').title = busy ? ('暂时点不了：' + busy) : '';
    $('startnow').textContent = busy ? ('（暂时点不了：' + busy + '）') : '';
    setBtn('bstop', !au.running && !(s.runner && s.runner.running),
      (!au.running && !(s.runner && s.runner.running)) ? '现在没有在跑的东西，不用停' : '');
    var restartUid = String((CUR_ACCT && CUR_ACCT.unique_id) || ($('cfg').unique_id.value || '')).trim();
    setBtn('bauthrestart', authRestarting || !restartUid || !!(s.checker && s.checker.running),
      authRestarting ? '正在关闭旧授权并重新启动…' : (!restartUid ? '先选择或填写抖音号' : '关闭当前授权并重新获取二维码'));
    if(authRestarting){
      setBtn('bstart', true);
      setBtn('bstop', true);
      if($('hbstart')){ $('hbstart').disabled = true; }
    }
    var fl = $('forcelog');
    var rows = s.force_log || [];
    fl.innerHTML = rows.length
      ? ('<div style="margin-bottom:4px">最近的强制操作：</div>' + rows.map(function(t){ return '<div>' + esc(t) + '</div>'; }).join(''))
      : '';
    if(lv.has_image || au.has_image){
      // 浏览器一开跑就自动把画面显示出来（用户手动收起的除外）。
      // 手机号模式下不摊开：不然第 2 步会被画面挤下去。
      // 手动授权模式下**一定摊开** —— 那个模式的重点就是这块能点的画面。
      if(!shotAutoDone && !shotManual){
        shotAutoDone = true;
        setShot(wantManual || qrFallback);
      }
      if(shotOpen){
        $('shotempty').hidden = true;
        fetch(withUid('api/screenshot?t=' + Date.now())).then(function(r){
          if(r.status === 204 || !r.ok){ return null; }
          return r.blob();
        }).then(function(b){
          if(!b || !b.size){
            shot.hidden = true;
            setShotTag(null, false);
            showShotHint(SHOT_EMPTY_HINT);
            return;
          }
          var url = URL.createObjectURL(b);
          shot.src = url;
          shot.hidden = false;
          if($('manualscreentip')){
            $('manualscreentip').hidden = !(wantManual || qrFallback || !!au.manual);
          }
          var fresh = lv.live || au.live;
          var age = (lv.frame_age === null || lv.frame_age === undefined) ? au.frame_age : lv.frame_age;
          setShotTag(age, !!fresh);
          if(!fresh){ showShotHint(SHOT_STALE_HINT); }
          if(lastUrl){ URL.revokeObjectURL(lastUrl); }
          lastUrl = url;
        }).catch(function(){});
      } else {
        shot.hidden = true;
      }
    } else {
      shot.hidden = true;
      setShotTag(null, false);
      if(shotOpen){ showShotHint(SHOT_EMPTY_HINT); }
    }
  }).catch(function(){});
}
var SEND_BADGE = {
  ok:['全部发送成功','g'],
  partial:['部分发送成功','y'],
  failed:['发送失败','r'],
  no_friend:['没找到目标好友','n'],
  no_login:['登录已失效','r'],
  error:['运行出错','r'],
  running:['本次记录未正常结束','y'],
  skipped:['上一轮还没跑完，本轮跳过','y'],
  queued:['排队中：等上一轮跑完接着发','y'],
  queue_timeout:['排队超时，本轮没发成','r']
};
// 主控制台「发送记录」显示几条：管理员看最近 50 次，普通用户还是只看自己名下最近 2 次。
// 完整记录留在管理控制台（那里管理员能看最近 100 次）。
var SEND_VIEW_MAX_ADMIN = 50, SEND_VIEW_MAX_USER = 2;
function sendViewMax(){ return IM_ADMIN ? SEND_VIEW_MAX_ADMIN : SEND_VIEW_MAX_USER; }
function renderSends(runs){
  var box = $('sends');
  var sm = $('sendssum');
  if(!runs || !runs.length){ box.innerHTML = '<div class="muted">还没有发送记录（还没有运行过）</div>'; if(sm){ sm.textContent = ''; } return; }
  if(sm){
    var info0 = SEND_BADGE[runs[0].status] || [(runs[0].status || ''), 'n'];
    sm.textContent = '（最近一次：' + info0[0] + '）';
  }
  var html = '';
  runs.slice(0, sendViewMax()).forEach(function(run){
    var info = SEND_BADGE[run.status] || [(run.status || ''), 'n'];
    html += '<div style="border-top:1px solid #eef2f7;padding:12px 0">';
    html += '<div><span class="badge ' + info[1] + '" style="padding:4px 10px;font-size:13px"><span class="dot"></span>' + esc(info[0]) + '</span>'
         + ' <b style="margin-left:6px">' + esc(run.account) + '</b>'
         + ' <span class="muted">' + esc(run.at) + '</span></div>';
    if(run.detail){ html += '<div class="muted" style="margin-top:4px">' + esc(run.detail) + '</div>'; }
    var friends = run.friends || [], shotList = [];
    friends.forEach(function(f){
      var why = REASON[f.reason] ? REASON[f.reason] : [(f.ok ? '已发送' : '没发出去'), (f.ok ? 'g' : 'r')];
      html += '<div style="margin-top:8px">' + tagHtml(why[0], why[1]) + ' <b>' + esc(f.name) + '</b>'
           + ' <span class="muted">' + esc(f.detail) + '</span>'
           + (f.shot ? ' <a href="api/shot?name=' + encodeURIComponent(f.shot) + '" data-shot="' + esc(f.shot) + '" target="_blank" rel="noopener">看大图</a>' : '')
           + '</div>';
      if(f.shot){ shotList.push(f.shot); }
    });
    if(shotList.length){
      // 截图排成一条能左右滑的缩略图带：好友多的时候页面不会被图片撑成一条长龙
      html += '<div class="shots">' + shotList.map(function(nm){
        var u = 'api/shot?name=' + encodeURIComponent(nm);
        return '<a href="' + u + '" data-shot="' + esc(nm) + '" target="_blank" rel="noopener">'
             + '<img src="' + u + '" loading="lazy" alt="发送后的截图，点击看大图"></a>';
      }).join('') + '</div>';
    }
    if(!friends.length && run.targets && run.targets.length){
      html += '<div class="muted" style="margin-top:6px">目标好友：' + run.targets.map(esc).join('、') + '</div>';
    }
    html += '</div>';
  });
  box.innerHTML = html;
}
// 截图窗口：点缩略图或「看截图」直接在页面上放大看，不用另开标签页
function closeShotWin(){
  var v = $('shotview'), img = $('shotviewimg');
  if(v){ v.hidden = true; }
  if(img && img.removeAttribute){ img.removeAttribute('src'); }
}
function openShotWin(name){
  var v = $('shotview'), img = $('shotviewimg'), tip = $('shotviewtip');
  if(!v || !img || !name){ return; }
  img.src = 'api/shot?name=' + encodeURIComponent(name);
  if(tip){ tip.textContent = name; }
  v.hidden = false;
  var btn = $('shotviewclose');
  if(btn && btn.focus){ btn.focus(); }
}
(function(){
  var box = $('sends');
  if(box && box.addEventListener){
    box.addEventListener('click', function(ev){
      var el = ev.target;
      while(el && el !== box && !(el.getAttribute && el.getAttribute('data-shot'))){ el = el.parentNode; }
      if(!el || el === box || !el.getAttribute){ return; }
      var nm = el.getAttribute('data-shot');
      if(!nm){ return; }
      if(ev.preventDefault){ ev.preventDefault(); }
      openShotWin(nm);
    });
  }
  var win = $('shotview');
  if(win && win.addEventListener){
    win.addEventListener('click', function(ev){
      var t = ev.target;
      if(t === win || (t && t.id === 'shotviewclose')){ closeShotWin(); }
    });
  }
  if(window.addEventListener){
    window.addEventListener('keydown', function(ev){ if(ev.key === 'Escape'){ closeShotWin(); } });
  }
})();
var lastSends = '';
function refreshSends(){
  fetch('api/sends?limit=' + SEND_VIEW_MAX_ADMIN).then(function(r){ return r.text(); }).then(function(t){
    if(t === lastSends){ return; }
    lastSends = t;
    try { renderSends(JSON.parse(t).runs); } catch(e){}
  }).catch(function(){});
}
function refreshLogs(force){
  var el = $('logs');
  if(!el){ return; }
  fetch('api/logs').then(function(r){ return r.text(); }).then(function(t){
    // 只有用户本来就在底部时才自动跟着滚：人家翻上去看旧日志时别把他拽下来
    // nearBottom 改成取回内容之后再算：判断的是"此刻"的滚动位置，不会被别的请求插队冲掉
    var nearBottom = true;
    if(el.scrollHeight && el.clientHeight !== undefined){
      nearBottom = (el.scrollHeight - el.scrollTop - el.clientHeight) < 40;
    }
    el.textContent = t || '暂无日志';
    // force=true 表示「日志面板刚打开」：不管以前滚到哪儿，默认翻到最下面
    if((force || nearBottom) && el.scrollHeight){ el.scrollTop = el.scrollHeight; }
  }).catch(function(){});
}
// ---- 轮询：页面切到后台就停，回到前台立刻补一次再继续（手机锁屏时不再空转） ----
var POLL = { timers: [], on: false };
function pollOnce(){ refresh(); refreshSends(); refreshLogs(); }
function startPolling(){
  if(POLL.on){ return; }
  POLL.on = true;
  POLL.timers = [
    setInterval(refresh, 2000),
    setInterval(refreshSends, 4000),
    setInterval(refreshLogs, 4000)
  ];
}
function stopPolling(){
  POLL.on = false;
  POLL.timers.forEach(function(t){ clearInterval(t); });
  POLL.timers = [];
}
document.addEventListener('visibilitychange', function(){
  if(document.hidden){ stopPolling(); }
  else { pollOnce(); startPolling(); }
});

// ---- 折叠面板记住开合；展开「用户管理」时立刻拉一次用户列表 ----
Array.prototype.forEach.call(document.querySelectorAll('details.fold[id]'), function(d){
  var key = 'fold:' + d.id;
  try {
    var saved = window.localStorage.getItem(key);
    if(saved === '1'){ d.open = true; }
    else if(saved === '0'){ d.open = false; }
  } catch(e){}
  d.addEventListener('toggle', function(){
    try { window.localStorage.setItem(key, d.open ? '1' : '0'); } catch(e){}
    if(d.open && d.id === 'adminbox'){
      USERS_FORCE = true;
      if(LAST_STATUS){ renderUsers(LAST_STATUS); }
    }
  });
});

// 通用输入框的按钮绑定：它挂在浏览器画面下面，跟授权弹窗分家，所以单独一个函数
function bindAnyBox(){
  if($('anygo')){ $('anygo').onclick = anySubmit; }
  if($('anyclear')){
    $('anyclear').onclick = function(){
      var a = $('anytext');
      if(a){ a.value = ''; a.focus(); }
      var r0 = $('anyresult');
      if(r0){ r0.hidden = true; r0.textContent = ''; r0.className = ''; }
    };
  }
  if($('anytext')){
    $('anytext').addEventListener('keydown', function(e){
      if(e.key === 'Enter'){ e.preventDefault(); anySubmit(); }
    });
  }
}

function initWebPush(){
  var enable = $('webpush-enable'), disable = $('webpush-disable'), state = $('webpush-state');
  if(!enable || !disable || !state){ return; }
  var ua = navigator.userAgent || '';
  var ios = /iPhone|iPad|iPod/i.test(ua) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  var standalone = !!(navigator.standalone || (window.matchMedia && window.matchMedia('(display-mode: standalone)').matches));
  var supported = !!(window.Notification && navigator.serviceWorker && ('PushManager' in window));
  function say(text){ state.textContent = text; }
  function setSubscribed(active){
    enable.hidden = !!active;
    disable.hidden = !active;
    if(active){ say('此设备已开启发送结果推送通知。'); }
  }
  if(navigator.serviceWorker){
    navigator.serviceWorker.register('/service-worker.js', {scope:'/'}).catch(function(){
      if(supported){ say('通知组件暂时无法加载，请刷新页面后重试。'); }
    });
  }
  if(ios && !standalone){
    enable.disabled = true;
    enable.textContent = '先添加到主屏幕';
    say('iPhone / iPad 的系统通知需要 iOS 16.4+：先用 Safari“添加到主屏幕”，再从主屏幕图标打开此页面。');
    return;
  }
  if(!supported){
    enable.disabled = true;
    say('当前浏览器不支持网页推送。iPhone / iPad 请更新到 iOS 16.4+ 并从主屏幕图标打开。');
    return;
  }
  if(window.Notification.permission === 'denied'){
    enable.disabled = true;
    say('系统已禁止通知，请在设备设置中允许“续火花”发送通知。');
    return;
  }
  navigator.serviceWorker.ready.then(function(registration){
    return registration.pushManager.getSubscription();
  }).then(function(subscription){
    if(!subscription){ setSubscribed(false); say('尚未开启通知；点“开启发送通知”完成设置。'); return null; }
    return fetch('/api/webpush/status', {
      method:'POST', credentials:'same-origin', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({endpoint:subscription.endpoint})
    }).then(function(response){
      return response.json().then(function(data){ if(!response.ok || !data.ok){ throw new Error('无法读取这台设备的通知状态。'); } return data; });
    });
  }).then(function(data){
    if(!data){ return; }
    if(data.active){ setSubscribed(true); }
    else { setSubscribed(false); say('此设备尚未绑定到当前账号；点“开启发送通知”即可绑定。'); }
  }).catch(function(){ say('还没有开启通知；点“开启发送通知”完成设置。'); });

  function toApplicationServerKey(value){
    var base64 = String(value || '').replace(/-/g, '+').replace(/_/g, '/');
    while(base64.length % 4){ base64 += '='; }
    var raw = window.atob(base64), result = new Uint8Array(raw.length);
    for(var i = 0; i < raw.length; i++){ result[i] = raw.charCodeAt(i); }
    return result;
  }
  enable.onclick = function(){
    if(enable.disabled){ return; }
    enable.disabled = true;
    say('正在请求系统通知权限…');
    var permission;
    try { permission = window.Notification.requestPermission(); }
    catch(e){ enable.disabled = false; say('系统没有接受通知授权请求，请重试。'); return; }
    Promise.resolve(permission).then(function(granted){
      if(granted !== 'granted'){ throw new Error(granted === 'denied' ? '系统已拒绝通知权限，请到设备设置中开启。' : '你还没有允许通知。'); }
      say('正在登记这台设备…');
      return navigator.serviceWorker.ready.then(function(registration){
        return fetch('/api/webpush/vapid-key', {credentials:'same-origin'}).then(function(response){
          return response.json().then(function(data){
            if(!response.ok || !data.ok){ throw new Error(data.error || '服务器暂时无法开启推送。'); }
            return registration.pushManager.getSubscription().then(function(existing){
              return existing || registration.pushManager.subscribe({userVisibleOnly:true, applicationServerKey:toApplicationServerKey(data.public_key)});
            });
          });
        });
      });
    }).then(function(subscription){
      return fetch('/api/webpush/subscription', {
        method:'POST', credentials:'same-origin', headers:{'Content-Type':'application/json'},
        body:JSON.stringify(subscription.toJSON())
      }).then(function(response){
        return response.json().then(function(data){ if(!response.ok || !data.ok){ throw new Error(data.error || '设备登记失败。'); } });
      }).then(function(){ setSubscribed(true); });
    }).catch(function(error){
      enable.disabled = false;
      say(error && error.message ? error.message : '开启通知失败，请检查网络后重试。');
    });
  };
  disable.onclick = function(){
    disable.disabled = true;
    navigator.serviceWorker.ready.then(function(registration){ return registration.pushManager.getSubscription(); }).then(function(subscription){
      if(!subscription){ return null; }
      return fetch('/api/webpush/unsubscribe', {
        method:'POST', credentials:'same-origin', headers:{'Content-Type':'application/json'},
        body:JSON.stringify({endpoint:subscription.endpoint})
      }).then(function(response){
        return response.json().then(function(data){ if(!response.ok || !data.ok){ throw new Error(data.error || '关闭通知失败。'); } return subscription; });
      });
    }).then(function(subscription){ return subscription ? subscription.unsubscribe() : true; }).then(function(){
      disable.disabled = false;
      setSubscribed(false);
      say('此设备已关闭发送结果通知。');
    }).catch(function(error){
      disable.disabled = false;
      say(error && error.message ? error.message : '关闭通知失败，请检查网络后重试。');
    });
  };
}

// 顶部提示条要贴在顶栏下面：手机上顶栏会折成两行、高度变高，这里跟着量一次
function syncHeadHeight(){
  // 顶栏换成了侧边栏布局里的 .main .top：量它的高度，顶部提示条才会正好落在标题栏下面
  var h = (document.querySelector ? document.querySelector('.main .top') : null);
  var root = document.documentElement;
  if(h && h.offsetHeight && root && root.style && root.style.setProperty){
    root.style.setProperty('--head-h', h.offsetHeight + 'px');
  }
}
syncHeadHeight();
if(window.addEventListener){ window.addEventListener('resize', syncHeadHeight); }

// 授权弹窗那块 HTML 排在 <script> 后面，必须等解析完再绑事件（否则按钮点了没反应）
// 侧边栏导航也一起等：面板和按钮都是解析完才存在的
function bootChrome(){
  bindAuthWizard();
  bindAnyBox();
  initWebPush();
  initNav();
}
if(document.readyState === 'loading'){ document.addEventListener('DOMContentLoaded', bootChrome); }
else { bootChrome(); }

pollOnce();
startPolling();

// ---- 红黑 / 白红主题切换：保持用户选择 ----
(function(){
  var btn = $('themebtn');
  function paint(){
    var dark = document.documentElement.getAttribute('data-theme') === 'dark';
    var label = btn && btn.querySelector('.theme-label');
    var nextTheme = dark ? '白红' : '红黑';
    var action = dark ? '切换到白红主题' : '切换到红黑主题';
    if(btn){
      btn.title = action;
      btn.setAttribute('aria-label', action);
      if(label){ label.textContent = nextTheme; }
    }
    var meta = document.querySelector('meta[name="theme-color"]');
    if(meta){ meta.setAttribute('content', dark ? '#0b0a0b' : '#fff9f9'); }
  }
  if(btn){
    btn.onclick = function(){
      var next = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
      document.documentElement.setAttribute('data-theme', next);
      try { window.localStorage.setItem('dsh-theme', next); } catch(e){}
      paint();
    };
  }
  paint();
})();

  (function(){
    var menu = $('accountMenu');
    if(!menu){ return; }
    var summary = menu.querySelector('summary');
    function sync(){ if(summary){ summary.setAttribute('aria-expanded', menu.open ? 'true' : 'false'); } }
    menu.addEventListener('toggle', sync);
    document.addEventListener('click', function(event){ if(menu.open && !menu.contains(event.target)){ menu.open = false; } });
    document.addEventListener('keydown', function(event){
      if(event.key === 'Escape' && menu.open){ menu.open = false; if(summary){ summary.focus(); } }
    });
    sync();
  })();


/* ===== 好友选择器：拉取抖音好友 -> 勾选 -> 自动写入「目标好友」 ===== */
(function(){
  var btn = $('frpLoad'), st = $('frpState'), list = $('frpList');
  if(!btn || !st || !list){ return; }
  var timer = null, deadline = 0, POLL = 2000, LIMIT = 330000, lastUid = null;

  function setState(text, isErr){
    st.textContent = text || '';
    st.className = isErr ? 'cnt frp-err' : 'cnt';
  }
  function reset(){
    if(timer){ clearInterval(timer); timer = null; }
    btn.disabled = false;
    list.innerHTML = '';
    list.hidden = true;
  }
  // 当前选中的抖音号：优先用面板自己的 curUid()（任务配置模式下 #f_uid 是隐藏的）
  function pickUid(){
    try{ if(typeof curUid === 'function'){ return String(curUid() || '').trim(); } }catch(e){}
    var el = $('f_uid');
    return el ? String(el.value || '').trim() : '';
  }
  function syncUid(){
    var uid = pickUid();
    if(uid !== lastUid){
      lastUid = uid;
      reset();
      setState(uid ? '点「拉取好友」获取这个号的好友' : '先在上面选一个已登录的抖音号', false);
    }
    return uid;
  }
  function clean(name){ return String(name == null ? '' : name).replace(/\u00a0/g, ' ').trim(); }
  function readTargets(){
    var ta = $('f_targets'), out = [];
    if(ta){ String(ta.value || '').split('\n').forEach(function(x){ x = x.trim(); if(x){ out.push(x); } }); }
    return out;
  }
  function writeTargets(names){
    var ta = $('f_targets'); if(!ta){ return; }
    ta.value = names.join('\n');
    try{ ta.dispatchEvent(new Event('input', {bubbles:true})); }catch(e){}
  }
  function boxes(){ return Array.prototype.slice.call(list.querySelectorAll('input[type=checkbox]')); }
  function commit(){
    var known = {};
    boxes().forEach(function(cb){ known[cb.getAttribute('data-name')] = 1; });
    var keep = readTargets().filter(function(x){ return !known[x]; });
    var picked = boxes().filter(function(cb){ return cb.checked; }).map(function(cb){ return cb.getAttribute('data-name'); });
    writeTargets(picked.concat(keep));
  }
  function render(friends){
    var sel = {};
    readTargets().forEach(function(x){ sel[x] = 1; });
    list.innerHTML = '';
    friends.forEach(function(raw){
      var name = clean(raw);
      if(!name){ return; }
      var lab = document.createElement('label');
      lab.className = 'frp-item' + (sel[name] ? ' on' : '');
      var cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.setAttribute('data-name', name);
      cb.checked = !!sel[name];
      cb.addEventListener('change', function(){ lab.classList.toggle('on', cb.checked); commit(); });
      var sp = document.createElement('span');
      sp.textContent = name;
      lab.appendChild(cb);
      lab.appendChild(sp);
      list.appendChild(lab);
    });
    list.hidden = list.children.length === 0;
  }
  function finish(text, isErr){
    if(timer){ clearInterval(timer); timer = null; }
    btn.disabled = false;
    setState(text, isErr);
  }
  function poll(uid){
    fetch('/api/friends?unique_id=' + encodeURIComponent(uid), {credentials:'same-origin'})
      .then(function(r){ return r.json(); })
      .then(function(d){
        if(!d || !d.ok){ finish((d && d.error) || '拉取失败', true); return; }
        if(d.running){
          setState(d.progress || '正在拉取好友，请稍候…', false);
          if(Date.now() > deadline){ finish('拉取超时了，稍后重试', true); }
          return;
        }
        var names = (d.friends || []).map(clean).filter(function(x){ return x; });
        render(names);
        if(names.length){
          finish('共 ' + names.length + ' 个好友；勾选即填入上方「目标好友」，别忘了保存', false);
        } else {
          finish(d.error || '没有拉到好友，稍后重试', true);
        }
      })
      .catch(function(){ /* 网络抖动：等下一轮 */ });
  }
  btn.addEventListener('click', function(){
    var uid = syncUid();
    if(!uid){ setState('先在上面选一个已登录的抖音号', true); return; }
    btn.disabled = true;
    setState('正在开始拉取…', false);
    post('/api/friends/refresh', {unique_id: uid}).then(function(d){
      if(!d || !d.ok){ finish((d && d.error) || '开始拉取失败', true); return; }
      deadline = Date.now() + LIMIT;
      if(timer){ clearInterval(timer); }
      timer = setInterval(function(){ poll(uid); }, POLL);
      poll(uid);
    });
  });
  // 切换账号时清掉上一次的结果，别把 A 号的好友填到 B 号身上
  try{
    if(typeof selectAccount === 'function'){
      var _select = selectAccount;
      selectAccount = function(uid){ _select(uid); lastUid = null; syncUid(); };
    }
  }catch(e){}
})();
</script><!-- 授权向导弹窗：只是 api/status 的投影；点「收起」只是藏起来，后台授权照跑 -->
<div id="authwiz" hidden role="dialog" aria-modal="true" aria-labelledby="wztitle">
<div id="authwizmask"></div>
<div id="authwizbox">
<div class="wz-head"><b id="wztitle">登录抖音</b>
<button class="ghost sm" id="wzclose2" type="button" aria-label="收起弹窗，授权继续在后台跑">收起</button>
</div>
<div class="wz-body">
<div class="wz-act" id="wzact-scan" hidden>
<div class="wz-spin" aria-hidden="true"></div>
<p class="wz-tip" id="wzscantip">已经扫到了，请在手机上点「确认登录」…</p>
<!-- 二级验证这一档改成「手动验证」：面板不替你操作，把可点的实时画面摆给你。
     画面和通用输入框都在主界面上（被这个弹窗盖着看不见），所以这里只放说明 + 一个按钮：
     点它就收起弹窗、把画面摊开。 -->
<div id="wzmanual" hidden>
<p class="wz-tip" id="wzmanualtip"></p>
<div class="wz-acts">
<button class="sm" id="wzgotoshot" type="button">收起弹窗，去画面里操作</button>
</div>
</div>
</div>
<div class="wz-act" id="wzact-sms" hidden>
<p class="wz-tip" id="wzsmshint"></p>
<div class="wz-code" id="wzcoderow">
<input id="wzcode" inputmode="numeric" autocomplete="one-time-code" maxlength="8" placeholder="手机收到的验证码" aria-label="手机收到的验证码">
<button class="sm" id="wzsubmit" type="button">提交</button>
</div>
<div class="wz-code" id="wzpwdrow" hidden>
<input id="wzpwd" type="password" autocomplete="off" maxlength="64" placeholder="抖音登录密码" aria-label="抖音登录密码">
<button class="sm ghost" id="wzpwdeye" type="button" aria-pressed="false" aria-label="显示密码" tabindex="-1">显示</button>
<button class="sm" id="wzpwdsubmit" type="button">提交</button>
</div>
<p class="wz-tip muted" id="wzcodehint">填满 6 位会自动提交；提交后我替你填进抖音并点「验证」，下面的画面就是抖音那边的样子。</p>
<div class="wz-bar" id="wzbar" hidden><i id="wzbarfill"></i></div>
<p class="wz-tip muted" id="wzbarp" hidden></p>
<div class="wz-live" id="wzlivbox" hidden>
<img id="wzsmslive" alt="抖音那边的画面：你填进去的验证码会出现在这里">
<p class="wz-tip muted" id="wzlivcap"></p>
</div>
<p class="wz-tip muted" id="wzsmslen"></p>
<p class="wz-tip" id="wzsmsresult" role="status"></p>
<p class="wz-tip muted" id="wzsmsstate" role="status"></p>
</div>
<div class="wz-act" id="wzact-done" hidden>
<div class="wz-ok" aria-hidden="true">✓</div>
<p class="wz-tip">登录成功，正在把 Cookie 收好…</p>
</div>
<div class="wz-act" id="wzact-err" hidden>
<p class="wz-tip" id="wzerrtip"></p>
<div class="wz-acts">
<button class="sm" id="wzretry" type="button">重试</button>
<button class="sm ghost" id="wzclose" type="button">关闭</button>
<button class="sm danger-ghost" id="wzforce" type="button" hidden>强制停止</button>
</div>
</div>
</div>
<div class="wz-steps"><span class="wz-dot" id="wzdot0"></span><span class="wz-dot" id="wzdot1"></span><span class="wz-dot" id="wzdot2"></span><span class="wz-dot" id="wzdot3"></span><span id="wzstepname">登录</span></div>
</div>
</div>
<button id="authbubble" type="button" hidden>授权进行中，点这里回到弹窗</button>
</body></html>

"""

ADMIN_HTML = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>管理控制台 · DouYinSparkFlow</title>
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<script>(function(){var t="dark";try{var saved=localStorage.getItem("dsh-theme");if(saved==="dark"||saved==="light")t=saved;}catch(e){}document.documentElement.setAttribute("data-theme",t);})();</script>
<style>
:root{
  --brand:#2563eb;--brand2:#1d4ed8;--soft:#eff6ff;--softline:#bfdbfe;
  --ink:#0f172a;--ink2:#334155;--muted:#6b7280;--line:#e6e9f2;--bg:#f4f6fb;--card:#fff;
  --ok:#0b7a44;--okbg:#e7f7ee;--okline:#b3e2c6;
  --warn:#8a5a00;--warnbg:#fdf5da;--warnline:#eedca2;
  --bad:#b3261e;--badbg:#fdeceb;--badline:#f2bcb8;
  --side:#172238;--side2:#243452;--sideink:#b9c6dc;--r:12px;
}
*{box-sizing:border-box}
html,body{margin:0}
body{background:var(--bg);color:var(--ink);line-height:1.6;-webkit-text-size-adjust:100%;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,"PingFang SC","Microsoft YaHei",sans-serif}
a{color:var(--brand2)}
.app{display:grid;grid-template-columns:238px minmax(0,1fr);min-height:100vh}

/* ---- 左侧导航 ---- */
.side{background:var(--side);color:var(--sideink);display:flex;flex-direction:column;gap:14px;
  padding:20px 14px;position:sticky;top:0;height:100vh;box-shadow:10px 0 28px rgba(25,42,70,.08)}
.brand{display:flex;align-items:center;gap:10px;padding:2px 6px 12px;border-bottom:1px solid rgba(255,255,255,.09)}
.brand .logo{width:26px;height:26px;border-radius:8px;flex:none;background:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cpath fill='%23fff' fill-rule='evenodd' d='M12 2.6C14.6 6.8 18.4 9.4 18.4 13.8A6.4 6.4 0 0 1 5.6 13.8C5.6 11.2 7.2 9.4 8.6 7.4C8.9 9 9.7 10.2 10.8 11C10.4 8 10.9 5 12 2.6ZM12 12.4C13.4 14 14.4 15.3 14.4 16.8A2.4 2.4 0 0 1 9.6 16.8C9.6 15.3 10.6 14 12 12.4Z'/%3E%3C/svg%3E") center/58% no-repeat,linear-gradient(135deg,#fb4857,#b7192d)}
.brand b{display:block;color:#fff;font-size:14px;letter-spacing:.2px}
.brand i{display:block;font-style:normal;font-size:11.5px;color:#8fa0bd}
nav{display:flex;flex-direction:column;gap:3px}
.nav{appearance:none;border:0;background:transparent;color:var(--sideink);text-align:left;font:inherit;
  padding:9px 11px;border-radius:9px;cursor:pointer;display:flex;align-items:center;gap:9px}
.nav:hover{background:var(--side2);color:#fff}
.nav.on{background:#2d6cdf;color:#fff;font-weight:600;box-shadow:0 5px 14px rgba(37,99,235,.22)}
.nav .pill{margin-left:auto;background:rgba(255,255,255,.16);color:#fff;border-radius:99px;padding:1px 8px;font-size:11.5px;font-weight:600}
.nav.on .pill{background:rgba(255,255,255,.28)}
.side-foot{margin-top:auto;font-size:12.5px;display:flex;flex-direction:column;gap:6px;padding:12px 6px 0;
  border-top:1px solid rgba(255,255,255,.09)}
.side-foot .who{color:#fff;font-weight:600}
.side-foot a{color:var(--sideink);text-decoration:none}
.side-foot a:hover{color:#fff;text-decoration:underline}

/* ---- 主区 ---- */
.main{padding:24px 28px 48px;min-width:0}
.top{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:16px}
.top h1{margin:0;font-size:19px;letter-spacing:.2px}
.top .sp{flex:1 1 auto}
.chip{display:inline-flex;align-items:center;gap:7px;background:var(--card);border:1px solid var(--line);
  border-radius:99px;padding:5px 12px;font-size:12.5px;color:var(--ink2)}
.chip .dot{width:8px;height:8px;border-radius:50%;background:#94a3b8;flex:none}
.chip.g .dot{background:var(--ok)} .chip.y .dot{background:#d97706} .chip.r .dot{background:var(--bad)}
section.panel{display:none}
section.panel.on{display:block}
.card{background:var(--card);border:1px solid var(--line);border-radius:var(--r);padding:16px 18px;margin-bottom:16px;
  box-shadow:0 4px 18px rgba(31,49,79,.045)}
.card>h2{margin:0 0 4px;font-size:15px}
.card>p.sub{margin:0 0 12px;color:var(--muted);font-size:12.5px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:16px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:var(--r);padding:14px 16px}
.kpi b{display:block;font-size:26px;line-height:1.25;letter-spacing:.5px}
.kpi span{color:var(--muted);font-size:12.5px}
.kpi.g b{color:var(--ok)} .kpi.y b{color:#b45309} .kpi.r b{color:var(--bad)} .kpi.b b{color:var(--brand2)}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.row.tight{gap:6px}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px}
label{display:block;font-weight:600;font-size:13px;margin:10px 0 5px}
label .hint{font-weight:400;color:var(--muted);font-size:12px}
input,textarea,select{width:100%;padding:8px 10px;border:1px solid #cbd5e1;border-radius:8px;font:inherit;background:var(--card);color:inherit}
input:focus,textarea:focus,select:focus{outline:2px solid var(--softline);outline-offset:1px;border-color:var(--brand)}
textarea{min-height:74px;resize:vertical}
button{font:inherit;cursor:pointer;border:0;border-radius:8px;padding:8px 14px;background:var(--brand);color:#fff;
  display:inline-flex;align-items:center;justify-content:center;gap:6px}
button:hover:not(:disabled){background:var(--brand2)}
button:disabled{opacity:.5;cursor:not-allowed}
button.sm{padding:5px 10px;font-size:12.5px}
button.ghost{background:var(--card);color:var(--ink2);border:1px solid var(--line)}
button.ghost:hover:not(:disabled){background:#f1f4fa}
button.danger{background:var(--bad)}
button.danger:hover:not(:disabled){background:#8f1d17}
button.dghost{background:var(--card);color:var(--bad);border:1px solid var(--badline)}
button.dghost:hover:not(:disabled){background:var(--badbg)}
button.link{background:transparent;color:var(--brand2);padding:2px 4px}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;color:var(--muted);font-weight:600;font-size:12px;padding:8px 8px;border-bottom:1px solid var(--line);
  white-space:nowrap;position:sticky;top:0;background:var(--card)}
td{padding:9px 8px;border-bottom:1px solid #f1f3f9;vertical-align:middle}
tr:last-child td{border-bottom:0}
tbody tr:hover{background:#fafbff}
.tblwrap{overflow:auto;max-height:62vh}
/* 操作列不要换行：按钮挤成两行甚至字被拆行都很难看 */
.acts{display:flex;gap:6px;flex-wrap:nowrap;align-items:center}
.acts button{white-space:nowrap;flex:none}
/* 新建 / 编辑抖音号卡片里的实时校验提示 */
.nufb{margin:4px 0 0;font-size:12px;color:var(--muted);min-height:15px;line-height:1.35}
.nufb.bad{color:var(--bad)}
.nufb.warn{color:var(--warn)}
.nufb.good{color:var(--ok)}
#accNew input[readonly]{background:var(--soft);color:var(--ink2);cursor:not-allowed}
#p-accounts .tblwrap table{min-width:1210px}
/* 目标好友可能很长：截断显示，鼠标悬停看全文，别把操作列挤没了 */
td.tgt{max-width:250px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.badge{display:inline-flex;align-items:center;gap:6px;border-radius:99px;padding:2px 10px;font-size:12px;
  font-weight:600;background:#eef1f6;color:#475569;border:1px solid #d7dee9;white-space:nowrap}
.badge .dot{width:7px;height:7px;border-radius:50%;background:currentColor;flex:none}
.badge.g{background:var(--okbg);color:var(--ok);border-color:var(--okline)}
.badge.y{background:var(--warnbg);color:var(--warn);border-color:var(--warnline)}
.badge.r{background:var(--badbg);color:var(--bad);border-color:var(--badline)}
.badge.n{background:#eef1f6;color:#475569;border-color:#d7dee9}
.mono{font-family:Consolas,ui-monospace,monospace}
.muted{color:var(--muted)}
.small{font-size:12.5px}
pre{background:#0f172a;color:#dbeafe;border-radius:10px;padding:12px;font-size:12px;max-height:420px;overflow:auto;
  white-space:pre-wrap;margin:0}
.empty{text-align:center;color:var(--muted);border:1px dashed var(--line);border-radius:10px;padding:18px;background:var(--card);font-size:13px}
.shots{display:flex;gap:8px;overflow-x:auto;padding:6px 0}
.shots img{height:120px;border:1px solid var(--line);border-radius:8px;cursor:zoom-in;display:block}
#toast{position:fixed;right:18px;bottom:18px;z-index:200;display:flex;flex-direction:column;gap:8px;align-items:flex-end}
#toast .t{background:#0f172a;color:#fff;border-radius:10px;padding:10px 14px;font-size:13px;max-width:min(70vw,420px);
  box-shadow:0 10px 30px rgba(15,23,42,.28);animation:pop .18s ease-out}
#toast .t.ok{background:#0b7a44} #toast .t.bad{background:var(--bad)}
@keyframes pop{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
#shotview{position:fixed;inset:0;z-index:210;background:rgba(15,23,42,.88);display:flex;align-items:center;justify-content:center;padding:3vh 3vw}
#shotview[hidden]{display:none}
#shotview img{max-width:100%;max-height:88vh;border-radius:10px;background:#fff}
#shotview .x{position:absolute;top:16px;right:18px}
.search{max-width:220px}
.split{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
/* 发送记录：左边 100 条列表，右边那一次的明细 */
.reclayout{display:grid;grid-template-columns:minmax(230px,320px) minmax(0,1fr);gap:14px;align-items:start}
.reclist{border:1px solid var(--line);border-radius:10px;max-height:66vh;overflow:auto}
.reclist .item{display:flex;gap:9px;align-items:center;padding:9px 11px;border-bottom:1px solid #f1f3f9;cursor:pointer}
.reclist .item:last-child{border-bottom:0}
.reclist .item:hover{background:#fafbff}
.reclist .item.on{background:var(--soft);box-shadow:inset 3px 0 0 var(--brand)}
.reclist .item .meta{min-width:0;flex:1}
.reclist .item b{display:block;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.reclist .item span{font-size:11.5px;color:var(--muted)}
.recdetail{min-width:0}
@media(max-width:900px){.reclayout{grid-template-columns:1fr}.reclist{max-height:40vh}}
hr{border:0;border-top:1px solid var(--line);margin:16px 0}
.card h3{font-size:14px;margin:0 0 6px}
label.cb{display:inline-flex;align-items:center;gap:6px;margin:0;font-weight:500;font-size:13px;cursor:pointer}
.grisk{color:var(--bad)}
.note{background:var(--soft);border:1px solid var(--softline);color:#3b3fa8;border-radius:10px;padding:10px 13px;font-size:12.5px}
/* ---- 定向消息（/admin「系统」页里发给指定用户）---- */
#mcfg .pick{max-height:220px;overflow:auto;border:1px solid var(--line);border-radius:10px;
  padding:8px 10px;margin-top:8px;background:var(--card)}
#mcfg .pick label{display:flex;align-items:center;gap:8px;font-weight:500;font-size:13.5px;
  margin:0;padding:5px 7px;border-radius:8px;cursor:pointer}
#mcfg .pick label:hover{background:var(--soft)}
#mcfg .pick input[type=checkbox]{width:auto;flex:none;margin:0;accent-color:var(--brand)}
#mcfg .pick .pn{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#mcfg .pick .pmeta{color:var(--muted);font-size:12px;flex:none}
#mcfg .pick .empty{border:0;background:transparent;padding:10px 4px}
#mcfg .msgprev .m-cap{font-size:12px;color:var(--muted);margin:0 0 6px}
#mcfg .msgprev .msg{border:1px solid var(--softline);border-left:3px solid var(--brand);
  border-radius:10px;padding:11px 14px;background:var(--soft)}
#mcfg .msgprev .m-head{display:flex;align-items:center;gap:8px;margin-bottom:4px;font-size:12.5px;color:#3b3fa8}
#mcfg .msgprev .m-head b{font-size:13.5px}
#mcfg .msgprev .m-head .sp{flex:1}
#mcfg .msgprev .m-head .m-dot{display:inline-block;width:7px;height:7px;border-radius:50%;
  background:var(--bad);flex:none}
#mcfg .msgprev .m-body{white-space:pre-wrap;word-break:break-word;font-size:13px;line-height:1.7;color:var(--ink2)}
#mcfg .sitem{display:flex;align-items:flex-start;gap:10px;border:1px solid var(--line);
  border-radius:10px;padding:10px 13px;margin-bottom:8px;background:var(--card)}
#mcfg .sitem .grow{flex:1;min-width:0}
#mcfg .sitem .sbod{white-space:pre-wrap;word-break:break-word;font-size:13px;line-height:1.65;
  margin:4px 0 6px;color:var(--ink)}
#mcfg .sitem .smeta{font-size:12px;color:var(--muted);line-height:1.9;word-break:break-word}
#mcfg .sitem .rd{display:inline-block;margin:0 4px 0 0;padding:1px 8px;border-radius:99px;
  font-size:11.5px;border:1px solid var(--line);color:var(--muted);background:var(--card)}
#mcfg .sitem .rd.on{border-color:var(--okline);color:var(--ok);background:var(--okbg)}
#mcfg .sitem .rd.no{border-color:var(--warnline);color:var(--warn);background:var(--warnbg)}
.warnbox{background:var(--warnbg);border:1px solid var(--warnline);color:var(--warn);border-radius:10px;padding:10px 13px;font-size:12.5px}
@media(max-width:900px){
  .app{grid-template-columns:1fr}
  .side{position:static;height:auto}
  nav{flex-direction:row;overflow-x:auto;gap:6px}
  .nav{white-space:nowrap}
  .side-foot{flex-direction:row;gap:14px;align-items:center;flex-wrap:wrap}
  .main{padding:14px}
}

/* ---- 深浅色：深色只换变量，侧栏本来就深 ---- */
html[data-theme="dark"]{
  --ink:#e8ecf7;--ink2:#c5cee2;--muted:#8c98b6;--line:#26314b;--bg:#0b1220;--card:#141d33;
  --soft:#1d2550;--softline:#313c78;
  --ok:#4ade80;--okbg:#102a1e;--okline:#1f5138;
  --warn:#fbbf24;--warnbg:#2b2410;--warnline:#57451a;
  --bad:#f87171;--badbg:#2d1517;--badline:#5e2a2d;
  --side:#070c17;--side2:#141d33;--sideink:#c3cee2;
}
html[data-theme="dark"] .badge,html[data-theme="dark"] .badge.n{background:#1e293b;color:#94a3b8;border-color:#334155}
html[data-theme="dark"] tbody tr:hover,html[data-theme="dark"] .reclist .item:hover{background:#182240}
html[data-theme="dark"] td{border-bottom-color:#1e2740}
html[data-theme="dark"] .reclist .item{border-bottom-color:#1e2740}
html[data-theme="dark"] pre{background:#080e1c}
html[data-theme="dark"] #toast .t{background:#080e1c}
/* ---- 火花图标：一套遮罩 + 暖色渐变，导航/主题/徽标共用 ---- */
.ni{display:inline-block;flex:none;width:16px;height:16px;vertical-align:-3px;
  background:linear-gradient(160deg,#ffd27a,#ff7a2f);
  -webkit-mask-repeat:no-repeat;mask-repeat:no-repeat;
  -webkit-mask-position:center;mask-position:center;
  -webkit-mask-size:contain;mask-size:contain}
.nav .ni{opacity:.92}
.nav:hover .ni,.nav.on .ni{opacity:1}
.nav.on .ni{background:linear-gradient(160deg,#fff2cd,#ffbb63)}
#themebtn .ni{width:15px;height:15px}
html[data-theme="dark"] #themebtn .ni{background:linear-gradient(160deg,#9a8b78,#5c5348)}
.badge .ni{width:11px;height:11px;vertical-align:-1px}
.ni-flame{-webkit-mask-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cpath fill='%23000' fill-rule='evenodd' d='M12 2.6C14.6 6.8 18.4 9.4 18.4 13.8A6.4 6.4 0 0 1 5.6 13.8C5.6 11.2 7.2 9.4 8.6 7.4C8.9 9 9.7 10.2 10.8 11C10.4 8 10.9 5 12 2.6ZM12 12.4C13.4 14 14.4 15.3 14.4 16.8A2.4 2.4 0 0 1 9.6 16.8C9.6 15.3 10.6 14 12 12.4Z'/%3E%3C/svg%3E");mask-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cpath fill='%23000' fill-rule='evenodd' d='M12 2.6C14.6 6.8 18.4 9.4 18.4 13.8A6.4 6.4 0 0 1 5.6 13.8C5.6 11.2 7.2 9.4 8.6 7.4C8.9 9 9.7 10.2 10.8 11C10.4 8 10.9 5 12 2.6ZM12 12.4C13.4 14 14.4 15.3 14.4 16.8A2.4 2.4 0 0 1 9.6 16.8C9.6 15.3 10.6 14 12 12.4Z'/%3E%3C/svg%3E")}
.ni-accounts{-webkit-mask-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cg fill='none' stroke='%23000' stroke-width='1.9' stroke-linecap='round' stroke-linejoin='round'%3E%3Ccircle cx='12' cy='8.2' r='3.6'/%3E%3Cpath d='M5 20.2c0-3.6 3.1-6.4 7-6.4s7 2.8 7 6.4'/%3E%3C/g%3E%3C/svg%3E");mask-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cg fill='none' stroke='%23000' stroke-width='1.9' stroke-linecap='round' stroke-linejoin='round'%3E%3Ccircle cx='12' cy='8.2' r='3.6'/%3E%3Cpath d='M5 20.2c0-3.6 3.1-6.4 7-6.4s7 2.8 7 6.4'/%3E%3C/g%3E%3C/svg%3E")}
.ni-admin{-webkit-mask-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cg fill='none' stroke='%23000' stroke-width='1.9' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M12 2.9 4.8 5.8v5.5c0 4.5 3 8.3 7.2 9.7 4.2-1.4 7.2-5.2 7.2-9.7V5.8z'/%3E%3Cpath d='m9.2 11.9 2.2 2.2 4.2-4.4'/%3E%3C/g%3E%3C/svg%3E");mask-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cg fill='none' stroke='%23000' stroke-width='1.9' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M12 2.9 4.8 5.8v5.5c0 4.5 3 8.3 7.2 9.7 4.2-1.4 7.2-5.2 7.2-9.7V5.8z'/%3E%3Cpath d='m9.2 11.9 2.2 2.2 4.2-4.4'/%3E%3C/g%3E%3C/svg%3E")}
.ni-logs{-webkit-mask-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cg fill='none' stroke='%23000' stroke-width='1.9' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M6.4 3h7.2l4.8 4.8V21H6.4z'/%3E%3Cpath d='M13.6 3v4.8h4.8'/%3E%3Cpath d='M9.2 12.4h5.6M9.2 16h4'/%3E%3C/g%3E%3C/svg%3E");mask-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cg fill='none' stroke='%23000' stroke-width='1.9' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M6.4 3h7.2l4.8 4.8V21H6.4z'/%3E%3Cpath d='M13.6 3v4.8h4.8'/%3E%3Cpath d='M9.2 12.4h5.6M9.2 16h4'/%3E%3C/g%3E%3C/svg%3E")}
.ni-me{-webkit-mask-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cg fill='none' stroke='%23000' stroke-width='1.9' stroke-linecap='round' stroke-linejoin='round'%3E%3Crect x='3' y='4.6' width='18' height='14.8' rx='2.6'/%3E%3Ccircle cx='8.8' cy='11' r='2.5'/%3E%3Cpath d='M5.4 16.8c.6-1.7 1.9-2.6 3.4-2.6s2.8.9 3.4 2.6'/%3E%3Cpath d='M15.4 10.2h3.4M15.4 13.8h3.4'/%3E%3C/g%3E%3C/svg%3E");mask-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cg fill='none' stroke='%23000' stroke-width='1.9' stroke-linecap='round' stroke-linejoin='round'%3E%3Crect x='3' y='4.6' width='18' height='14.8' rx='2.6'/%3E%3Ccircle cx='8.8' cy='11' r='2.5'/%3E%3Cpath d='M5.4 16.8c.6-1.7 1.9-2.6 3.4-2.6s2.8.9 3.4 2.6'/%3E%3Cpath d='M15.4 10.2h3.4M15.4 13.8h3.4'/%3E%3C/g%3E%3C/svg%3E")}
.ni-overview{-webkit-mask-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cg fill='none' stroke='%23000' stroke-width='1.9' stroke-linecap='round' stroke-linejoin='round'%3E%3Crect x='3.4' y='3.4' width='7.4' height='7.4' rx='1.7'/%3E%3Crect x='13.2' y='3.4' width='7.4' height='7.4' rx='1.7'/%3E%3Crect x='3.4' y='13.2' width='7.4' height='7.4' rx='1.7'/%3E%3Crect x='13.2' y='13.2' width='7.4' height='7.4' rx='1.7'/%3E%3C/g%3E%3C/svg%3E");mask-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cg fill='none' stroke='%23000' stroke-width='1.9' stroke-linecap='round' stroke-linejoin='round'%3E%3Crect x='3.4' y='3.4' width='7.4' height='7.4' rx='1.7'/%3E%3Crect x='13.2' y='3.4' width='7.4' height='7.4' rx='1.7'/%3E%3Crect x='3.4' y='13.2' width='7.4' height='7.4' rx='1.7'/%3E%3Crect x='13.2' y='13.2' width='7.4' height='7.4' rx='1.7'/%3E%3C/g%3E%3C/svg%3E")}
.ni-records{-webkit-mask-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cg fill='none' stroke='%23000' stroke-width='1.9' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M21.2 2.8 3 9.9l7.1 3 3 7.1z'/%3E%3Cpath d='M21.2 2.8 10.1 12.9'/%3E%3C/g%3E%3C/svg%3E");mask-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cg fill='none' stroke='%23000' stroke-width='1.9' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M21.2 2.8 3 9.9l7.1 3 3 7.1z'/%3E%3Cpath d='M21.2 2.8 10.1 12.9'/%3E%3C/g%3E%3C/svg%3E")}
.ni-system{-webkit-mask-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cg fill='none' stroke='%23000' stroke-width='1.9' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M4 7h10M18 7h2M4 17h2M10 17h10'/%3E%3Ccircle cx='16' cy='7' r='2.3'/%3E%3Ccircle cx='8' cy='17' r='2.3'/%3E%3C/g%3E%3C/svg%3E");mask-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cg fill='none' stroke='%23000' stroke-width='1.9' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M4 7h10M18 7h2M4 17h2M10 17h10'/%3E%3Ccircle cx='16' cy='7' r='2.3'/%3E%3Ccircle cx='8' cy='17' r='2.3'/%3E%3C/g%3E%3C/svg%3E")}
.ni-users{-webkit-mask-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cg fill='none' stroke='%23000' stroke-width='1.9' stroke-linecap='round' stroke-linejoin='round'%3E%3Ccircle cx='9' cy='8' r='3.4'/%3E%3Cpath d='M2.8 20.2c0-3.5 2.8-6.1 6.2-6.1s6.2 2.6 6.2 6.1'/%3E%3Cpath d='M16.6 5.4a3.4 3.4 0 0 1 0 6.5'/%3E%3Cpath d='M17.6 20.2c0-2.2-.6-3.9-1.7-5.1'/%3E%3C/g%3E%3C/svg%3E");mask-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cg fill='none' stroke='%23000' stroke-width='1.9' stroke-linecap='round' stroke-linejoin='round'%3E%3Ccircle cx='9' cy='8' r='3.4'/%3E%3Cpath d='M2.8 20.2c0-3.5 2.8-6.1 6.2-6.1s6.2 2.6 6.2 6.1'/%3E%3Cpath d='M16.6 5.4a3.4 3.4 0 0 1 0 6.5'/%3E%3Cpath d='M17.6 20.2c0-2.2-.6-3.9-1.7-5.1'/%3E%3C/g%3E%3C/svg%3E")}
/* 管理端视觉重构 */
:root{--brand:#256b62;--brand2:#17584f;--soft:#e7f4f0;--softline:#b9ddd2;--ink:#172b2d;--ink2:#405759;--muted:#6d8182;--line:#dce8e5;--bg:#f2f7f5;--card:#fff;--ok:#087958;--okbg:#e7f6ef;--okline:#b8dfcb;--r:16px;--side:#102d32;--side2:#19454a;--sideink:#c4d9d8}
body{background:radial-gradient(ellipse at 92% -18%,#deeee8 0,transparent 38%),var(--bg);color:var(--ink)}.side{background:linear-gradient(180deg,#13373d,#102b32);box-shadow:8px 0 24px #1034361f}.brand{padding:7px 9px 17px}.nav{min-height:44px;border:1px solid transparent;transition:.18s}.nav.on{background:linear-gradient(110deg,#218878,#176b60);box-shadow:0 6px 16px #197d6a38}.main{padding:28px 32px 52px}.top{position:sticky;top:0;z-index:80;padding:9px 0 13px;background:#f2f7f5e8;backdrop-filter:blur(14px)}.top h1{font-size:22px;letter-spacing:-.3px}.card,.kpi{border-radius:16px;box-shadow:0 5px 20px #1a48410b}.card{padding:18px 20px}button{min-height:40px;border-radius:10px}input,textarea,select{border-radius:10px;border-color:#c9d9d6}.kpi{padding:16px 17px}.tblwrap,.reclist{border-radius:12px}

html[data-theme="dark"]{--brand:#64cdb8;--brand2:#7fddc9;--soft:#183e39;--softline:#28665b;--ink:#e8f1f0;--ink2:#c7d8d6;--muted:#94aaa7;--line:#2a4542;--bg:#102120;--card:#172e2c;--ok:#68ddb4;--okbg:#183d33;--okline:#2d6956;--side:#0a1c1c;--side2:#1b3c38}html[data-theme="dark"] body{background:#102120}html[data-theme="dark"] .top{background:#102120e8}html[data-theme="dark"] html[data-theme="dark"] input,html[data-theme="dark"] textarea,html[data-theme="dark"] select{background:#102522;border-color:#385752}
@media(max-width:900px){.app{display:block}.side{position:fixed;inset:auto 0 0;height:auto;width:100%;padding:4px 7px calc(5px + env(safe-area-inset-bottom));z-index:150;box-shadow:0 -8px 26px #0c2b2a2e}.brand,.side-foot{display:none}nav{height:55px;display:flex;flex-direction:row;overflow-x:auto;overscroll-behavior-x:contain;scrollbar-width:none;gap:3px}nav::-webkit-scrollbar{display:none}.nav{flex:1 0 62px;min-width:62px;min-height:51px;padding:5px 4px;display:flex;flex-direction:column;justify-content:center;gap:3px;font-size:10.5px;line-height:1.1;text-align:center;white-space:nowrap}.nav .ni{width:18px;height:18px}.nav .pill{display:none}.main{padding:12px 14px calc(92px + env(safe-area-inset-bottom))}.top{position:sticky;top:0;margin:-12px -14px 12px;padding:10px 14px;background:#f2f7f5f2;border-bottom:1px solid var(--line)}.grid2{grid-template-columns:1fr}.kpis{grid-template-columns:repeat(2,minmax(0,1fr))}.tblwrap{max-width:calc(100vw - 56px);overscroll-behavior-x:contain}}
@media(max-width:560px){.main{padding-left:11px;padding-right:11px}.top{margin-left:-11px;margin-right:-11px;padding:10px 11px}.card{padding:14px}input,textarea,select{font-size:16px}button{min-height:44px}.kpis{gap:8px}.kpi{padding:12px}.kpi b{font-size:23px}.row>button{flex:1 1 auto}.tblwrap{max-width:calc(100vw - 44px)}}

/* Red and black by default; the theme button switches to white and red. */
html[data-theme="dark"]{color-scheme:dark;--brand:#f04452;--brand2:#d92e3e;--soft:#311519;--softline:#79333d;--ink:#f5f2f3;--ink2:#ded6d8;--muted:#a49a9d;--line:#393336;--bg:#0b0a0b;--card:#151214;--side:#070607;--side2:#1b1518;--sideink:#d8cfd2;--ok:#5bd59e;--okbg:#12271f;--okline:#2a5841;--warn:#f4c35d;--warnbg:#2b2112;--warnline:#6e5221;--bad:#ff7d85;--badbg:#311519;--badline:#79333d}
html[data-theme="light"]{color-scheme:light;--brand:#ca2638;--brand2:#a91d2d;--soft:#fff0f2;--softline:#efb5bc;--ink:#241b1d;--ink2:#57474a;--muted:#806f72;--line:#eadcdf;--bg:#fff9f9;--card:#fff;--side:#fff;--side2:#fff0f2;--sideink:#58494c;--ok:#087a50;--okbg:#e7f7ee;--okline:#b3e2c6;--warn:#8a5a00;--warnbg:#fdf5da;--warnline:#eedca2;--bad:#a91d2d;--badbg:#fdebed;--badline:#efb5bc}
html[data-theme="dark"] body{background:radial-gradient(ellipse at 92% -18%,#35151b 0,transparent 38%),var(--bg);color:var(--ink)}
html[data-theme="light"] body{background:radial-gradient(ellipse at 92% -18%,#fff0f1 0,transparent 38%),var(--bg);color:var(--ink)}
html[data-theme] .side{background:var(--side)}
html[data-theme="light"] .brand b{color:var(--ink)}
html[data-theme="light"] .brand i{color:var(--muted)}
html[data-theme] .nav:hover{background:var(--side2);color:var(--brand)}
html[data-theme] .nav.on{background:linear-gradient(110deg,var(--brand),var(--brand2));color:#fff;box-shadow:0 6px 16px rgba(190,25,44,.24)}
html[data-theme] .top{background:var(--bg);border-color:var(--line)}
html[data-theme] .card,html[data-theme] .kpi,html[data-theme] 
html[data-theme] .note{background:var(--soft);border-color:var(--softline);color:var(--ink2)}
html[data-theme] input,html[data-theme] textarea,html[data-theme] select{background:var(--card);border-color:var(--line);color:var(--ink)}
html[data-theme="dark"] input,html[data-theme="dark"] textarea,html[data-theme="dark"] select{background:#191516;border-color:#514448}
html[data-theme] 
html[data-theme="dark"] 
html[data-theme="light"] 
html[data-theme] .nav.on .ni{background:linear-gradient(160deg,#fff,#ffe5e7)}
.account-menu{position:relative;z-index:200;flex:none;color:var(--ink)}
.account-menu>summary{list-style:none;display:inline-flex;align-items:center;justify-content:center;gap:6px;min-height:40px;padding:6px 10px;border:1px solid var(--line);border-radius:10px;background:var(--card);color:var(--ink2);font-size:13px;font-weight:600;cursor:pointer;white-space:nowrap}
.account-menu>summary::-webkit-details-marker{display:none}
.account-menu>summary:hover,.account-menu[open]>summary{border-color:var(--softline);color:var(--brand);background:var(--soft)}
.account-panel{position:absolute;right:0;top:calc(100% + 8px);width:246px;padding:8px;border:1px solid var(--line);border-radius:14px;background:var(--card);box-shadow:0 16px 44px rgba(20,8,10,.24);display:grid;gap:3px;z-index:220}
.account-id{display:grid;gap:2px;padding:10px 11px 11px;margin-bottom:3px;border-bottom:1px solid var(--line)}
.account-id b{font-size:13px;color:var(--ink);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.account-id span{font-size:11.5px;color:var(--muted)}
.account-panel a{display:flex;align-items:center;min-height:40px;padding:8px 11px;border-radius:9px;color:var(--ink2);font-size:13px;text-decoration:none}
.account-panel a:hover{background:var(--soft);color:var(--brand)}
.account-panel a.account-exit{color:var(--bad)}
.account-panel a.account-exit:hover{background:var(--badbg)}
.theme-label{font-size:12px;white-space:nowrap}
html[data-theme] #themebtn{gap:5px;border-color:var(--line);color:var(--ink2);background:var(--card)}
html[data-theme] #themebtn:hover{border-color:var(--softline);color:var(--brand)}
@media(max-width:900px){html[data-theme] .side{box-shadow:0 -8px 26px rgba(30,8,12,.18)}html[data-theme] .top{background:var(--bg);border-bottom-color:var(--line);z-index:180}}
@media(max-width:560px){.account-panel{width:min(270px,calc(100vw - 22px))}}

/* Admin visual refresh: the same spark red system, tuned for dense operational work. */
html[data-theme="dark"]{
  color-scheme:dark;--brand:#e84d5b;--brand2:#cc3546;--soft:#2d191e;--softline:#65313a;
  --ink:#f5edef;--ink2:#d8c8cc;--muted:#a58f95;--line:#3b2b30;--bg:#100d0f;--card:#191416;
  --ok:#68d8a1;--okbg:#14251e;--okline:#2d5c45;--warn:#f1c469;--warnbg:#2b2114;--warnline:#6c5428;
  --bad:#ff8790;--badbg:#30191e;--badline:#6b343c;--side:#130f11;--side2:#29191e;--sideink:#cdbdc1;
}
html[data-theme="light"]{
  color-scheme:light;--brand:#bd263c;--brand2:#981b31;--soft:#fbe9ec;--softline:#ecc2c8;
  --ink:#2b1a1e;--ink2:#5d454b;--muted:#806a70;--line:#e8d8db;--bg:#fff8f8;--card:#fff;
  --ok:#08794e;--okbg:#e8f6ef;--okline:#b5dfc8;--warn:#845900;--warnbg:#fff5dc;--warnline:#ead9a9;
  --bad:#a82034;--badbg:#fdebed;--badline:#efbdc4;--side:#fffdfd;--side2:#f9ecee;--sideink:#60484e;
}
html[data-theme] body{font-family:Inter,"Aptos","PingFang SC","Microsoft YaHei","Noto Sans CJK SC",sans-serif;font-size:13.5px;font-variant-numeric:tabular-nums;background:var(--bg);color:var(--ink)}
html[data-theme] .app{grid-template-columns:224px minmax(0,1fr)}
html[data-theme] .side{gap:20px;padding:22px 15px;background:var(--side);border-right:1px solid var(--line);box-shadow:none}
html[data-theme] .brand{gap:11px;padding:4px 8px 17px;border-bottom:1px solid var(--line)}
html[data-theme] .brand .logo{width:34px;height:34px;border-radius:11px;box-shadow:none;background-size:58%,100%}
html[data-theme] .brand b{font-size:14px;color:var(--ink);letter-spacing:.1px}
html[data-theme] .brand i{margin-top:2px;color:var(--muted);font-size:11px;letter-spacing:.1px}
html[data-theme] nav{gap:5px}
html[data-theme] .nav{min-height:42px;padding:9px 11px;border:1px solid transparent;border-radius:9px;color:var(--sideink);font-size:13px;transition:background .16s,color .16s,border-color .16s}
html[data-theme] .nav:hover{background:var(--side2);color:var(--brand)}
html[data-theme] .nav.on{background:var(--soft);border-color:var(--softline);color:var(--brand);box-shadow:none;font-weight:700}
html[data-theme] .nav.on .pill{background:var(--brand);color:#fff}
html[data-theme] .nav.on .ni{background:var(--brand)}
html[data-theme] .side-foot{gap:8px;padding:14px 8px 0;border-top-color:var(--line)}
html[data-theme] .side-foot .who{color:var(--ink)}
html[data-theme] .side-foot a{color:var(--muted)}
html[data-theme] .side-foot a:hover{color:var(--brand)}
html[data-theme] .main{max-width:1600px;margin:0 auto;padding:24px clamp(18px,3vw,42px) 56px}
html[data-theme] .top{position:sticky;top:0;z-index:120;min-height:58px;margin:-24px calc(-1 * clamp(18px,3vw,42px)) 22px;padding:10px clamp(18px,3vw,42px);background:var(--bg);border:0;border-bottom:1px solid var(--line)}
html[data-theme] .top h1{font-size:20px;letter-spacing:-.35px}
html[data-theme] .card{border:1px solid var(--line);border-radius:13px;background:var(--card);box-shadow:none;padding:19px 20px}
html[data-theme] .card>h2{font-size:15px;letter-spacing:-.1px}
html[data-theme] .card>p.sub{font-size:12.5px;color:var(--muted)}
html[data-theme] .kpis{gap:0;margin-bottom:16px;border:1px solid var(--line);border-radius:12px;background:var(--card);overflow:hidden}
html[data-theme] .kpi{min-width:0;padding:14px 16px;border:0;border-right:1px solid var(--line);border-radius:0;background:transparent}
html[data-theme] .kpi:last-child{border-right:0}
html[data-theme] .kpi b{font-size:25px;letter-spacing:-.35px}
html[data-theme] .kpi span{font-size:12px;color:var(--muted)}
html[data-theme] .kpi.b b{color:var(--brand)}
html[data-theme] .kpi.g b{color:var(--ok)}
html[data-theme] .kpi.y b{color:var(--warn)}
html[data-theme] .kpi.r b{color:var(--bad)}
html[data-theme] .grid2{gap:14px}
html[data-theme] button{min-height:38px;border-radius:8px;background:var(--brand);font-weight:600;box-shadow:none;transition:background .16s,border-color .16s,color .16s}
html[data-theme] button:hover:not(:disabled){background:var(--brand2)}
html[data-theme] button.sm{min-height:32px}
html[data-theme] button.ghost,html[data-theme] button.sec{border:1px solid var(--line);background:var(--card);color:var(--ink2)}
html[data-theme] button.ghost:hover:not(:disabled),html[data-theme] button.sec:hover:not(:disabled){border-color:var(--softline);background:var(--soft);color:var(--brand)}
html[data-theme] button.dghost{border-color:var(--badline);background:transparent;color:var(--bad)}
html[data-theme] input,html[data-theme] textarea,html[data-theme] select{min-height:40px;border-color:var(--line);border-radius:8px;background:var(--card);color:var(--ink)}
html[data-theme] input:focus,html[data-theme] textarea:focus,html[data-theme] select:focus{outline:3px solid rgba(232,77,91,.22);outline-offset:1px;border-color:var(--brand)}
html[data-theme] label{color:var(--ink2)}
html[data-theme] table{font-size:12.5px}
html[data-theme] th{z-index:2;border-bottom-color:var(--line);background:var(--card);color:var(--muted);font-size:11.5px}
html[data-theme] td{border-bottom-color:var(--line);color:var(--ink2)}
html[data-theme] tbody tr:hover{background:var(--soft)}
html[data-theme] .badge{border-radius:7px}
html[data-theme] .chip{border-color:var(--line);border-radius:8px;background:var(--card);color:var(--ink2)}
html[data-theme] .account-menu>summary{border-radius:8px;background:var(--card);color:var(--ink2)}
html[data-theme] .account-menu>summary:hover,html[data-theme] .account-menu[open]>summary{border-color:var(--softline);background:var(--soft);color:var(--brand)}
html[data-theme] .account-panel{border-color:var(--line);border-radius:12px;background:var(--card);box-shadow:0 16px 36px rgba(0,0,0,.2)}
html[data-theme] #toast .t{border:1px solid var(--line);border-radius:10px;background:var(--card);color:var(--ink)}
html[data-theme] :focus-visible{outline:3px solid rgba(232,77,91,.48);outline-offset:2px}
@media(max-width:900px){html[data-theme] .app{display:block}html[data-theme] .side{position:fixed;inset:auto 0 0;height:auto;width:100%;padding:4px 8px calc(5px + env(safe-area-inset-bottom));z-index:150;border:0;border-top:1px solid var(--line);box-shadow:none}html[data-theme] nav{height:54px;gap:4px}html[data-theme] .nav{min-height:50px;border-radius:8px}html[data-theme] .main{padding:12px 18px calc(92px + env(safe-area-inset-bottom))}html[data-theme] .top{margin:-12px -18px 17px;padding:9px 18px}}
@media(max-width:700px){html[data-theme] .kpis{grid-template-columns:repeat(3,minmax(0,1fr))}html[data-theme] .kpi:nth-child(3){border-right:0}html[data-theme] .kpi:nth-child(n+4){border-top:1px solid var(--line)}}
@media(max-width:560px){html[data-theme] .main{padding-right:12px;padding-left:12px}html[data-theme] .top{margin-right:-12px;margin-left:-12px;padding-right:12px;padding-left:12px}html[data-theme] .card{padding:15px 14px}html[data-theme] .kpi{padding:11px 10px}html[data-theme] .kpi b{font-size:22px}}
/* 2026-10-07 restore the clean white admin console without changing behavior. */
html[data-theme]{color-scheme:light;--brand:#171717;--brand2:#333;--soft:#f5f5f5;--softline:#dedede;--ink:#171717;--ink2:#404040;--muted:#737373;--line:#e5e5e5;--bg:#fff;--card:#fff;--side:#fff;--side2:#f4f4f4;--sideink:#404040;--ok:#167344;--okbg:#eef7f1;--okline:#c7e6d1;--warn:#805700;--warnbg:#fbf5e8;--warnline:#ead6a8;--bad:#b4232f;--badbg:#fff1f1;--badline:#f0c4c7}
html[data-theme] body{background:#fff;color:#171717}html[data-theme] .side{background:#fff;border-right:1px solid #e8e8e8;box-shadow:none}html[data-theme] .brand{border-bottom-color:#ededed}html[data-theme] .brand b,html[data-theme] .side-foot .who{color:#171717}html[data-theme] .brand i,html[data-theme] .side-foot a{color:#737373}
html[data-theme] .nav{background:transparent;color:#525252;border-color:transparent}html[data-theme] .nav:hover{background:#f7f7f7;color:#171717}html[data-theme] .nav.on{background:#f1f1f1;border-color:#dedede;color:#171717;box-shadow:none}html[data-theme] .nav.on .ni{background:#171717}
html[data-theme] .top{background:#fff;border-bottom:1px solid #ededed;backdrop-filter:none}html[data-theme] .card,html[data-theme] .kpi{border-color:#e5e5e5;border-radius:12px;background:#fff;box-shadow:none}html[data-theme] .card>p.sub,html[data-theme] .kpi span{color:#737373}
html[data-theme] button{background:#171717;color:#fff;border-radius:8px;box-shadow:none}html[data-theme] button:hover:not(:disabled){background:#333}html[data-theme] button.ghost,html[data-theme] button.sec{background:#fff;color:#262626;border-color:#dedede}html[data-theme] button.ghost:hover:not(:disabled),html[data-theme] button.sec:hover:not(:disabled){background:#f7f7f7;color:#111;border-color:#bdbdbd}
html[data-theme] input,html[data-theme] textarea,html[data-theme] select{background:#fff;border-color:#dedede;color:#171717}html[data-theme] input:focus,html[data-theme] textarea:focus,html[data-theme] select:focus{outline-color:#e8e8e8;border-color:#999}html[data-theme] th{background:#fff;color:#737373}html[data-theme] td{border-bottom-color:#e5e5e5;color:#404040}html[data-theme] tbody tr:hover{background:#fafafa}
html[data-theme] .chip,html[data-theme] .account-menu>summary,html[data-theme] .account-panel{background:#fff;border-color:#e5e5e5;color:#404040}html[data-theme] .account-panel{box-shadow:0 16px 36px rgba(0,0,0,.12)}html[data-theme] #toast .t{background:#fff;border-color:#e5e5e5;color:#171717}html[data-theme] #themebtn{display:none!important}
@media(max-width:900px){html[data-theme] .side{border-top:1px solid #e8e8e8;box-shadow:0 -5px 18px rgba(0,0,0,.04)}html[data-theme] .top{background:#fff}}
/* Keyboard access and neutral icon treatment for the refreshed admin UI. */
html[data-theme] .ni{background:#737373}
html[data-theme] .nav.on .ni{background:#171717}
html[data-theme] :focus-visible{outline:3px solid #666!important;outline-offset:3px!important}
.skip-link{position:fixed;top:8px;left:8px;z-index:500;transform:translateY(-160%);padding:9px 12px;border:1px solid #171717;border-radius:7px;background:#171717;color:#fff;text-decoration:none}
.skip-link:focus{transform:translateY(0)}

</style></head><body>
<div id="toast" aria-live="polite"></div>
<div class="app">
<aside class="side">
  <div class="brand"><span class="logo"></span><div><b>DouYinSparkFlow</b><i>管理控制台</i></div></div>
  <nav id="nav" aria-label="&#31649;&#29702;&#33756;&#21333;">
    <button class="nav on" type="button" data-go="overview"><i class="ni ni-overview" aria-hidden="true"></i>概览</button>
    <button class="nav" type="button" data-go="accounts"><i class="ni ni-accounts" aria-hidden="true"></i>抖音号 <span class="pill" id="nAcc">0</span></button>
    <button class="nav" type="button" data-go="users"><i class="ni ni-users" aria-hidden="true"></i>用户 <span class="pill" id="nUser">0</span></button>
    <button class="nav" type="button" data-go="subscription"><i class="ni ni-clock" aria-hidden="true"></i>时长服务</button>
    <button class="nav" type="button" data-go="records"><i class="ni ni-records" aria-hidden="true"></i>发送记录</button>
    <button class="nav" type="button" data-go="logs"><i class="ni ni-logs" aria-hidden="true"></i>运行日志</button>
    <button class="nav" type="button" data-go="system"><i class="ni ni-system" aria-hidden="true"></i>系统 / 应急</button>
  </nav>
  <div class="side-foot">
    <span class="who" id="whoami">管理员</span>
    <a href="/">我的控制台</a>
    <a href="/logout">退出登录</a>
  </div>
</aside>

<a class="skip-link" href="#main-content">&#36339;&#21040;&#20027;&#35201;&#20869;&#23481;</a>
<main class="main" id="main-content" tabindex="-1">
  <div class="top">
    <h1 id="pageTitle">概览</h1>
    <span class="sp"></span>
    <span class="chip" id="chipAuth"><span class="dot"></span><span>浏览器状态读取中…</span></span>
    <span class="chip" id="chipRun"><span class="dot"></span><span>任务状态读取中…</span></span>
    <button class="ghost sm" id="themebtn" type="button" aria-label="切换主题"><i class="ni ni-flame"></i><span class="theme-label"></span></button>
    <details class="account-menu" id="accountMenu">
      <summary aria-label="打开账号菜单" aria-expanded="false"><span>账号</span><span aria-hidden="true">⌄</span></summary>
      <div class="account-panel">
        <div class="account-id"><b id="accountWho">管理员</b><span id="accountRole">管理员账号</span></div>
        <a href="/">我的控制台</a>
        <a class="account-exit" href="/logout">切换账号 / 退出登录</a>
      </div>
    </details>
  </div>

  <!-- ============ 概览 ============ -->
  <section class="panel on" id="p-overview">
    <div class="kpis">
      <div class="kpi b"><b id="kAcc">–</b><span>抖音号总数</span></div>
      <div class="kpi g"><b id="kOk">–</b><span>登录正常</span></div>
      <div class="kpi n"><b id="kNo">–</b><span>未授权</span></div>
      <div class="kpi y"><b id="kWait">–</b><span>待填目标好友</span></div>
      <div class="kpi b"><b id="kUser">–</b><span>控制台用户</span></div>
      <div class="kpi y"><b id="kFree">–</b><span>未分配抖音号</span></div>
    </div>

    <div class="grid2">
      <div class="card">
        <h2>浏览器 / 登录检测</h2>
        <p class="sub">最多同时 <b>2 个</b>授权（每个号一个独立会话，各扫各的）；发送任务和登录检测仍然独占。</p>
        <div class="row" style="margin-bottom:10px">
          <span class="badge n" id="brBadge"><span class="dot"></span>空闲</span>
          <span class="muted small" id="brWho"></span>
        </div>
        <p class="small" id="brMsg">—</p>
        <p class="small muted" id="brLeft"></p>
        <div id="brList" class="small" style="margin:8px 0 10px"></div>
        <div class="row tight">
          <button class="sm ghost" type="button" data-act="check-all">检测所有账号</button>
          <button class="sm ghost" type="button" data-act="force-stop">强制停止浏览器和任务</button>
          <button class="sm ghost" type="button" data-act="run">跑一轮全部账号</button>
        </div>
        <p class="small muted" id="chkAll">检测所有账号：逐个打开聊天页确认还能不能发，不会给谁发消息。</p>
      </div>

      <div class="card">
        <h2>最近一次发送</h2>
        <p class="sub">每次运行结束后的结果，含每个好友的成败和截图。</p>
        <div id="lastRun" class="small muted">读取中…</div>
      </div>
    </div>

    <div class="card">
      <h2>需要关注的账号</h2>
      <p class="sub">没登录、登录失效、或者还没填目标好友的号，都会列在这里。</p>
      <div id="needAttention" class="small muted">读取中…</div>
    </div>
  </section>

  <!-- ============ 抖音号 ============ -->
  <section class="panel" id="p-accounts">
    <div class="card">
      <h2>全部抖音号</h2>
      <p class="sub">可以在这里检测登录、开授权浏览器、导出 Cookie 或删除账号。</p>
      <div class="split" style="margin-bottom:10px">
        <input class="search" id="accFilter" placeholder="搜索抖音号 / 名称 / 用户" autocomplete="off">
        <button class="sm ghost" type="button" id="accRefresh">刷新</button>
        <button class="sm ghost" type="button" data-act="check-all">检测所有账号</button>
        <span class="sp" style="flex:1"></span>
        <button class="sm" type="button" id="accNewBtn">＋ 新建抖音号</button>
      </div>
      <div class="tblwrap"><table>
        <thead><tr><th>抖音号</th><th>名称</th><th>归属</th><th>登录状态</th><th>Cookie</th><th>目标好友</th><th>操作</th></tr></thead>
        <tbody id="accBody"><tr><td colspan="7" class="muted">读取中…</td></tr></tbody>
      </table></div>
    </div>

    <div class="card" id="accNew" hidden>
      <h2 id="nu_title">新建抖音号</h2>
      <p class="sub" id="nu_sub">填写「抖音号」和「名称」即可创建，授权登录后再补目标好友。</p>
      <div class="grid2">
        <div>
          <label for="nu_uid">抖音号 <span class="hint" id="nu_uid_hint">自己起个标识，例如 myspark</span></label>
          <input id="nu_uid" placeholder="例如 myspark" autocomplete="off">
          <label for="nu_name">名称 <span class="hint">不填就用抖音号；名称用来区分截图和记录，不能和别的号重名</span></label>
          <input id="nu_name" placeholder="例如 小明的小号" autocomplete="off">
          <p class="nufb" id="nu_fb_name"></p>
          <label for="nu_targets">目标好友 <span class="hint">每行一个，支持备注 / 昵称 / 抖音号；可以先空着</span></label>
          <textarea id="nu_targets" placeholder="小明&#10;老王"></textarea>
          <p class="nufb" id="nu_fb_targets"></p>
          <label for="nu_times">每天发送时间 <span class="hint">每行一个：09:00；09:00-11:00 随机；09:00±30 前后随机</span></label>
          <textarea id="nu_times" placeholder="留空会建议空闲时间"></textarea>
          <p class="nufb" id="nu_fb_times"></p>
        </div>
        <div>
          <label for="nu_msg">消息模板 <span class="hint">只对这个抖音号生效；[API] 会替换成每日一句</span></label>
          <textarea id="nu_msg" placeholder="留空就用全局模板" autocomplete="off"></textarea>
          <p class="nufb" id="nu_fb_msg"></p>
        </div>
      </div>
      <div class="grid2" style="margin-top:2px">
        <div>
          <label for="nu_dmin">发送间隔 <span class="hint">秒，0=不等；给两个好友之间加的随机等待</span></label>
          <div class="row tight">
            <input id="nu_dmin" type="number" min="0" max="600" step="1" placeholder="0" style="max-width:110px">
            <span class="muted small">~</span>
            <input id="nu_dmax" type="number" min="0" max="600" step="1" placeholder="0" style="max-width:110px">
          </div>
          <p class="nufb" id="nu_fb_delay"></p>
        </div>
        <div>
          <label for="nu_hito">一言类型 <span class="hint">JSON 数组，例如 [&quot;a&quot;,&quot;b&quot;]；留空就用全局默认</span></label>
          <input id="nu_hito" placeholder="留空即可" autocomplete="off">
          <p class="nufb" id="nu_fb_hito"></p>
        </div>
      </div>
      <div class="row" style="margin-top:12px">
        <button class="sm" type="button" id="nu_save">保存</button>
        <button class="sm ghost" type="button" id="nu_cancel">取消</button>
        <span class="muted small" id="nu_state"></span>
      </div>
    </div>

    <div class="card" id="gcfg">
      <h2>全局发送设置 <span class="hint">所有「没单独配过」的抖音号都用这一份</span></h2>
      <p class="sub">只影响账号里对应项为空的号；已经单独配过的号不受影响。改完点「保存全局默认」立即生效，不用重启。</p>
      <div class="grid2">
        <div>
          <label for="gc_msg">全局消息模板 <span class="hint">[API] 会替换成每日一句</span></label>
          <textarea id="gc_msg" placeholder="留空就用内置默认模板" autocomplete="off"></textarea>
          <p class="nufb" id="gc_fb_msg"></p>
        </div>
        <div>
          <label for="gc_hito">全局一言类型 <span class="hint">JSON 数组，例如 [&quot;a&quot;,&quot;b&quot;]；留空用内置默认</span></label>
          <input id="gc_hito" placeholder="留空即可" autocomplete="off">
          <p class="nufb" id="gc_fb_hito"></p>
          <label for="gc_dmin">全局默认发送间隔 <span class="hint">秒，0=不等；两个好友之间的随机等待</span></label>
          <div class="row tight">
            <input id="gc_dmin" type="number" min="0" max="600" step="1" placeholder="0" style="max-width:110px">
            <span class="muted small">~</span>
            <input id="gc_dmax" type="number" min="0" max="600" step="1" placeholder="0" style="max-width:110px">
          </div>
          <p class="nufb" id="gc_fb_delay"></p>
        </div>
      </div>
      <div class="row" style="margin-top:12px">
        <button class="sm" type="button" id="gc_save">保存全局默认</button>
        <span class="muted small" id="gc_state"></span>
      </div>

      <hr>
      <h3>一键推平到所有账号</h3>
      <p class="sub">把上面的全局值<strong class="grisk">覆盖</strong>到每个抖音号自己的配置上（勾了哪项就推哪项）。
        这会改掉账号自己原本的设置，且没有一键还原。</p>
      <div class="row" style="flex-wrap:wrap;gap:16px">
        <label class="cb"><input type="checkbox" id="gp_msg" checked> 消息模板</label>
        <label class="cb"><input type="checkbox" id="gp_hito" checked> 一言类型</label>
        <label class="cb"><input type="checkbox" id="gp_delay"> 发送间隔</label>
      </div>
      <div class="row" style="margin-top:10px">
        <button class="sm dghost" type="button" id="gp_go">应用到全部账号</button>
        <span class="muted small" id="gp_state"></span>
      </div>
    </div>
  </section>

  <!-- ============ 用户 ============ -->
  <section class="panel" id="p-users">
    <div class="card">
      <h2>控制台用户</h2>
      <p class="sub">谁可以登录这个面板、名下有哪些抖音号。</p>
      <div class="split" style="margin-bottom:10px">
        <label style="margin:0;display:inline-flex;align-items:center;gap:8px;font-weight:400">
          <input type="checkbox" id="regAllow" style="width:auto"> 允许别人自己注册
        </label>
        <span class="muted small" id="regState"></span>
        <span class="sp" style="flex:1"></span>
        <button class="sm ghost" type="button" id="usersRefresh">刷新</button>
      </div>
      <div class="tblwrap"><table>
        <thead><tr><th>登录名</th><th>手机号</th><th>注册时间</th><th>最近登录</th><th>登录 IP</th><th>名下的抖音号</th><th>使用时长</th><th>操作</th></tr></thead>
        <tbody id="userBody"><tr><td colspan="8" class="muted">读取中…</td></tr></tbody>
      </table></div>
      <p class="small muted" id="freeIds" style="margin:10px 0 0"></p>
    </div>

    <div class="grid2">
      <div class="card">
        <h2>开一个新账号</h2>
        <p class="sub">给朋友开控制台账号，他登录后自己绑抖音号。</p>
        <label for="cu_name">登录名 <span class="hint">2~32 位</span></label>
        <input id="cu_name" placeholder="例如 xiaoming" autocomplete="off">
        <label for="cu_pw">初始密码 <span class="hint">至少 6 位</span></label>
        <input id="cu_pw" type="password" placeholder="例如 Spark123456" autocomplete="new-password">
        <div class="row" style="margin-top:12px">
          <button class="sm" type="button" id="cu_btn">创建账号</button>
          <span class="muted small" id="cu_state"></span>
        </div>
      </div>
      <div class="card">
        <h2>把抖音号分配给某人</h2>
        <p class="sub">没分配给任何人的抖音号，普通用户在面板里看不到。每人只能有 1 个：分配新的会自动解绑旧的。</p>
        <label for="bd_uid">抖音号</label>
        <input id="bd_uid" placeholder="例如 dingdingya1216" autocomplete="off">
        <label for="bd_user">给谁</label>
        <select id="bd_user"></select>
        <div class="row" style="margin-top:12px">
          <button class="sm" type="button" id="bd_btn">分配</button>
          <span class="muted small" id="bd_state"></span>
        </div>
      </div>
    </div>
  </section>

  <!-- ============ 时长服务 ============ -->
  <section class="panel" id="p-subscription">
    <div class="card">
      <h2>兑换码生成器</h2>
      <p class="sub">生成一次性时长兑换码。明文只在生成后显示一次，请及时复制保存。</p>
      <div class="grid2">
        <div><label for="sc_days">时长</label><select id="sc_days"><option value="7">一星期</option><option value="14">两星期</option><option value="30">一个月</option><option value="60">两个月</option></select></div>
        <div><label for="sc_count">数量</label><input id="sc_count" type="number" min="1" max="100" value="1"></div>
      </div>
      <div class="row" style="margin-top:12px"><button class="sm" type="button" id="sc_generate">生成兑换码</button><span class="muted small" id="sc_state"></span></div>
      <pre id="sc_output" hidden style="margin-top:10px;max-height:180px"></pre>
    </div>
    <div class="card">
      <h2>直接授予普通用户</h2>
      <p class="sub">管理员可直接给指定用户增加时长，不需要兑换码。</p>
      <div class="grid2">
        <div><label for="sc_user">用户</label><select id="sc_user"></select></div>
        <div><label for="sc_grant_days">时长</label><select id="sc_grant_days"><option value="7">一星期</option><option value="14">两星期</option><option value="30">一个月</option><option value="60">两个月</option></select></div>
      </div>
      <div class="row" style="margin-top:12px"><button class="sm" type="button" id="sc_grant">授予时长</button><span class="muted small" id="sc_grant_state"></span></div>
    </div>
    <div class="card">
      <h2>兑换码历史</h2>
      <div class="tblwrap"><table><thead><tr><th>时长</th><th>生成时间</th><th>状态</th><th>使用者</th></tr></thead><tbody id="sc_history"><tr><td colspan="4" class="muted">读取中…</td></tr></tbody></table></div>
    </div>
  </section>

  <!-- ============ 发送记录 ============ -->
  <section class="panel" id="p-records">
    <div class="card">
      <h2>发送记录</h2>
      <p class="sub">保留最近 100 次运行（含很久以前的）；左边点一次，右边看这一次每个好友的成败和截图。普通用户只能看到自己名下最近的 2 条。</p>
      <div class="split" style="margin-bottom:10px">
        <input class="search" id="recFilter" placeholder="按账号 / 状态筛选" autocomplete="off">
        <button class="sm ghost" type="button" id="recRefresh">刷新</button>
        <span class="muted small" id="recCount"></span>
      </div>
      <div class="reclayout">
        <div class="reclist" id="recList"></div>
        <div class="recdetail" id="recDetail"></div>
      </div>
    </div>
  </section>

  <!-- ============ 日志 ============ -->
  <section class="panel" id="p-logs">
    <div class="card">
      <h2>运行日志</h2>
      <p class="sub">发送引擎和手动运行日志的最新内容。</p>
      <div class="row" style="margin-bottom:10px">
        <label style="margin:0;display:inline-flex;align-items:center;gap:8px;font-weight:400">
          <input type="checkbox" id="logFollow" checked style="width:auto"> 自动滚到最新
        </label>
        <button class="sm ghost" type="button" id="logRefresh">刷新</button>
      </div>
      <pre id="logBox">读取中…</pre>
    </div>
    <div class="card">
      <h2>最近的强制操作 / 记录</h2>
      <div id="forceLog" class="small muted">读取中…</div>
    </div>
  </section>

  <!-- ============ 系统 ============ -->
  <section class="panel" id="p-system">
    <div class="grid2">
      <div class="card">
        <h2>应急操作</h2>
        <p class="sub">浏览器卡住时用；不会影响已保存的登录状态和配置。</p>
        <div class="row tight">
          <button class="sm ghost" type="button" data-act="force-stop">强制停止（浏览器 + 任务）</button>
          <button class="sm dghost" type="button" data-act="force-restart">强制重启面板后端</button>
        </div>
        <p class="muted small" style="margin:10px 0 0">强制重启大约 10 秒后自动恢复，页面会自己刷新。</p>
      </div>
      <div class="card">
        <h2>改管理员密码</h2>
        <p class="sub">改完当前浏览器会自动换一张新通行证，其他设备上的旧登录立即失效。</p>
        <label for="ap_old">现在的密码</label>
        <input id="ap_old" type="password" autocomplete="current-password">
        <label for="ap_new">新密码 <span class="hint">至少 6 位</span></label>
        <input id="ap_new" type="password" autocomplete="new-password">
        <label for="ap_again">再输一次</label>
        <input id="ap_again" type="password" autocomplete="new-password">
        <div class="row" style="margin-top:12px">
          <button class="sm" type="button" id="ap_btn">保存新密码</button>
          <span class="muted small" id="ap_state"></span>
        </div>
      </div>
    </div>
    <div class="card" id="ncfg">
      <h2>公告与管理员联系方式 <span class="hint">显示在用户控制台最上方，所有登录用户都能看到</span></h2>
      <p class="sub">写一段给全体同志的公告，外加怎么联系你。两块都留空 = 用户那边不出现公告条；
        改完点「保存公告」立即生效，不用重启。用户点过「知道了」之后就不再烦他，但<strong>内容一改就会重新出现</strong>。</p>
      <label for="nc_text">公告正文 <span class="hint">留空则不显示正文</span></label>
      <textarea id="nc_text" placeholder="例如：各位同志，本周火花维护时段调整，具体见群通知。" style="min-height:90px" autocomplete="off"></textarea>
      <p class="nufb" id="nc_fb_text"></p>
      <label for="nc_contact">管理员联系方式 <span class="hint">可以多行，例如 微信：xxx / QQ：xxx / 邮箱：xxx</span></label>
      <textarea id="nc_contact" placeholder="微信：&#10;QQ：&#10;邮箱：" style="min-height:92px" autocomplete="off"></textarea>
      <p class="nufb" id="nc_fb_contact"></p>
      <div class="row" style="margin-top:12px">
        <button class="sm" type="button" id="nc_save">保存公告</button>
        <button class="sm dghost" type="button" id="nc_clear">清空输入框</button>
        <span class="muted small" id="nc_state"></span>
      </div>
      <p class="sub" style="margin-top:14px">用户在控制台看到的效果：</p>
      <div class="note" id="nc_prev"></div>
    </div>
    <div class="card" id="mcfg">
      <h2>定向消息 <span class="hint">只发给勾选的用户，显示在他们控制台最上方（和公告条同一位置）</span></h2>
      <p class="sub">和公告的区别：公告是给<b>全体</b>看的，定向消息只有你勾的那几个人看得到。
        发完可以在下面看到<b>谁看了、谁还没看</b>；删掉的消息所有收件人那边同时消失。</p>

      <label for="mc_filter">收件人 <span class="hint">勾几个就发几个，可以多选</span></label>
      <input id="mc_filter" placeholder="输入名字过滤，例如 zhang" autocomplete="off">
      <div class="row" style="margin-top:8px">
        <button class="sm ghost" type="button" id="mc_all">全选（当前筛选出的）</button>
        <button class="sm ghost" type="button" id="mc_none">清空选择</button>
        <span class="muted small" id="mc_count">已选 0 人</span>
      </div>
      <div class="pick" id="mc_pick"></div>
      <p class="nufb" id="mc_fb_pick"></p>

      <label for="mc_text">消息内容 <span class="hint">换行会原样保留</span></label>
      <textarea id="mc_text" placeholder="例如：你的火花任务今天提示登录失效了，麻烦回来重新授权一下。" style="min-height:104px" autocomplete="off"></textarea>
      <p class="nufb" id="mc_fb_text"></p>

      <div class="row" style="margin-top:12px">
        <button class="sm" type="button" id="mc_send">发送消息</button>
        <button class="sm dghost" type="button" id="mc_reset">清空输入</button>
        <span class="muted small" id="mc_state"></span>
      </div>

      <p class="sub" style="margin-top:14px">用户在控制台看到的效果：</p>
      <div class="msgprev" id="mc_prev"></div>

      <label style="margin-top:16px">已发出的消息 <span class="hint" id="mc_listnote"></span></label>
      <div class="sentlist" id="mc_list"></div>
    </div>
    <div class="card">
      <h2>说明</h2>
      <div class="note">
        · 每个普通用户只能绑 <b>__MAX_ACCOUNTS__ 个</b>抖音号（管理员不受限）；没分配给任何人的号只有管理员看得到，分配新号时会自动解绑旧的。<br>
        · 「抖音号」那一页每行都有 <b>运行</b>：只给这一个号跑一轮；「概览」里的「跑一轮全部账号」才是全部跑。<br>
        · 授权浏览器 3 分钟没人操作会自动关闭，把引擎让给别人。<br>
        · 管理员入口是 <b>/admin</b>，普通用户访问会回到自己的控制台。
      </div>
    </div>
  </section>
</main>
</div>

<div id="shotview" hidden>
  <button class="ghost sm x" type="button" id="shotClose">关闭</button>
  <img id="shotImg" alt="发送截图">
</div>

<script>
var $ = function(id){ return document.getElementById(id); };
function esc(t){
  return String(t === null || t === undefined ? '' : t)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
function toast(text, ok){
  if(!text){ return; }
  var box = $('toast');
  var el = document.createElement('div');
  el.className = 't ' + (ok ? 'ok' : 'bad');
  el.textContent = text;
  box.appendChild(el);
  setTimeout(function(){ if(el.parentNode){ el.parentNode.removeChild(el); } }, 6000);
}
function post(url, data){
  return fetch(url, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(data || {})})
    .then(function(r){
      return r.text().then(function(t){
        var body = null;
        try { body = JSON.parse(t); } catch(e){ body = null; }
        if(!body || typeof body !== 'object'){ body = {ok:false, error:'HTTP ' + r.status}; }
        return body;
      });
    })
    .catch(function(){ return {ok:false, error:'网络断了或后端没响应'}; });
}
function getText(url){
  return fetch(url).then(function(r){ return r.text(); }).catch(function(){ return ''; });
}
function getJson(url){
  return fetch(url).then(function(r){ return r.json(); }).catch(function(){ return null; });
}
function fmtLeft(sec){
  if(sec === null || sec === undefined){ return ''; }
  return sec >= 60 ? (Math.ceil(sec / 60) + ' 分钟') : (sec + ' 秒');
}
// 「还要等多久」说成人话
function fmtWait(sec){
  if(sec === null || sec === undefined){ return '一会儿'; }
  sec = Math.max(0, Math.round(sec));
  if(sec < 60){ return sec + ' 秒'; }
  return Math.floor(sec / 60) + ' 分 ' + (sec % 60) + ' 秒';
}
// 返回 [色调, 文案, 是否需要关注]
// 注意：checks 里的记录有可能是扫码授权留下的（source='qr'），那**不是**检测结果。
// 服务端已经把它滤掉了，这里再挡一道：万一拿到旧数据/旧缓存，
// 也不会把「刚扫码授权」渲染成绿色的「登录正常」。
function acctBadge(a){
  var ck = (a.check && a.check.source !== 'qr') ? a.check : null;
  var hasCookie = !!a.has_cookie;
  // 执行任务途中程序自己发现的登录失效，比「检测」按钮的结果更新，必须优先显示
  if(a.task_login_failed){ return [hasCookie ? 'r' : 'n', hasCookie ? '登录已失效' : '未授权', true]; }
  if(ck && ck.ok && !a.ready){ return ['y', '已登录 · 待填好友', true]; }
  if(ck && ck.ok){ return ['g', '登录正常', false]; }
  if(ck && !ck.ok){ return [hasCookie ? 'r' : 'n', hasCookie ? '登录失效' : '未授权', true]; }
  // 有 Cookie、但一次真检测都没跑过：「待检测」——
  // 它不算「要处理的问题」（别把「需要关注的账号」刷屏），但也不该显示成绿色
  if(!hasCookie){ return ['n', '未授权', true]; }
  if(!a.ready){ return ['y', '待填好友', true]; }
  return ['n', '待检测', false];
}

// ---- 面板切换 ----
var PANELS = ['overview','accounts','users','subscription','records','logs','system'];
var TITLES = {overview:'概览', accounts:'抖音号', users:'用户', subscription:'时长服务', records:'发送记录', logs:'运行日志', system:'系统 / 应急'};
var CURRENT = 'overview';
function showPanel(name){
  if(PANELS.indexOf(name) < 0){ name = 'overview'; }
  CURRENT = name;
  PANELS.forEach(function(p){
    var el = $('p-' + p);
    if(el){ el.className = 'panel' + (p === name ? ' on' : ''); }
  });
  Array.prototype.forEach.call(document.querySelectorAll('#nav .nav'), function(b){
    b.className = 'nav' + (b.getAttribute('data-go') === name ? ' on' : '');
  });
  $('pageTitle').textContent = TITLES[name] || '概览';
  if(name === 'users'){ loadUsers(true); }
  if(name === 'subscription'){ loadUsers(true); loadSubscriptionAdmin(); }
  if(name === 'records'){ loadSends(); }
  if(name === 'logs'){ loadLogs(true); }
  if(name === 'accounts'){ loadStatus(); }
}
Array.prototype.forEach.call(document.querySelectorAll('#nav .nav'), function(b){
  b.onclick = function(){
    var name = b.getAttribute('data-go');
    if(location.hash !== '#' + name){ location.hash = name; } else { showPanel(name); }
  };
});
window.addEventListener('hashchange', function(){ showPanel((location.hash || '').replace('#',''));
 });

// ---- 状态 ----
var STATUS = null, USERS = null, RUNS = [];
var SEND_TONE = {ok:'g', partial:'y', failed:'r', no_friend:'n', no_login:'r', error:'r', running:'y', skipped:'y', queued:'y', queue_timeout:'r'};
var REC_SEL = 0, REC_MAX = 100, REC_STORED = 0, REC_CAP = 2;
// ---- 公告与管理员联系方式（/admin「系统」页里改）----
// 用户端每次 /api/status 都会拿到这份内容，所以保存完直接 loadStatus 回读，
// 保证显示的就是后端真正存下来的那份（而不是输入框里的文本）。
var NC_LOADED = false;
var NC_MAX_TEXT = 3000, NC_MAX_CONTACT = 600;
function ncPrevHtml(){
  var t = ($('nc_text').value || '').trim();
  var c = ($('nc_contact').value || '').trim();
  if(!t && !c){ return '<span class="muted small">两块都留空：用户控制台不会出现公告条</span>'; }
  return (t ? noticePreview(t) : '')
    + (c ? '<br><b>管理员联系方式</b><br>' + noticePreview(c) : '');
}
function noticePreview(t){
  return esc(t).replace(/\r\n|\r|\n/g, '<br>');
}
function ncCheck(){
  var hard = [];
  var t = ($('nc_text').value || '').trim();
  var c = ($('nc_contact').value || '').trim();
  if(t.length > NC_MAX_TEXT){ hard.push('公告正文太长了（最多 ' + NC_MAX_TEXT + ' 字，现在 ' + t.length + ' 字）'); }
  if(c.length > NC_MAX_CONTACT){ hard.push('联系方式太长了（最多 ' + NC_MAX_CONTACT + ' 字，现在 ' + c.length + ' 字）'); }
  nuSetFb('nc_fb_text', t ? ('正文：' + t.length + ' 字 / ' + t.split(/\r?\n/).length + ' 行') : '留空 = 不显示正文', '');
  nuSetFb('nc_fb_contact', c ? ('联系方式：' + c.length + ' 字 / ' + c.split(/\r?\n/).length + ' 行') : '留空 = 不显示联系方式', '');
  $('nc_prev').innerHTML = ncPrevHtml();
  var btn = $('nc_save');
  if(btn){ btn.disabled = hard.length > 0; }
  $('nc_state').textContent = hard.length ? ('还不能保存：' + hard[0]) : '✓ 可以保存';
  return hard;
}
function ncLoad(){
  var n = (STATUS && STATUS.notice) || {};
  $('nc_text').value = n.text || '';
  $('nc_contact').value = n.contact || '';
  ncCheck();
}
Array.prototype.forEach.call(['nc_text', 'nc_contact'],
  function(id){ var el = $(id); if(el){ el.addEventListener('input', function(){ ncCheck(); }); } });

$('nc_save').onclick = function(){
  var hard = ncCheck();
  if(hard.length){ toast(hard[0], false); return; }
  $('nc_state').textContent = '保存中…';
  $('nc_save').disabled = true;
  post('api/notice/save', { text: $('nc_text').value || '', contact: $('nc_contact').value || '' })
    .then(function(r){
      $('nc_save').disabled = false;
      $('nc_state').textContent = (r && (r.message || r.error)) || '';
      toast((r && (r.message || r.error)) || '', !!(r && r.ok));
      if(r && r.ok){ loadStatus(); }
    });
};
$('nc_clear').onclick = function(){
  if(!$('nc_text').value && !$('nc_contact').value){ toast('输入框本来就是空的', false); return; }
  if(!window.confirm('清空公告和联系方式？\n\n确认后主界面上的公告条会消失（还要点一次「保存公告」才生效）。')){ return; }
  $('nc_text').value = '';
  $('nc_contact').value = '';
  ncCheck();
  $('nc_state').textContent = '已清空输入框 —— 点「保存公告」才会真正生效';
};
// ---- 定向消息（/admin「系统」页里改）----
// 收件人名单由后端算好跟着 /api/status 下发（messages.targets，只有管理员拿得到），
// 因此这里不依赖 USERS 是否已经拉过。勾选状态只存在内存里：
// 4 秒一次的轮询只刷新「已发出的消息」那块，绝不冲掉正在勾的选择。
var MC_LOADED = false;
var MC_SIG = '';        // 名单指纹（含 USERS 是否到位）：变了才重建勾选框
var MC_PICK = {};       // 已勾选：{用户名: true}
var MC_MAX_TEXT = 1000;
var MC_SHOW = 60;       // 「已发出的消息」最多铺多少条
function mcMessages(){
  return (STATUS && STATUS.messages) || {};
}
function mcTargets(){
  return (mcMessages().targets || []).slice();
}
function mcSyncPicker(){
  // max_text 以后端为准（避免前后端两处硬编码漂移）
  var mt = Number(mcMessages().max_text || 0);
  if(mt > 0){ MC_MAX_TEXT = mt; }
  var targets = mcTargets();
  var sig = targets.join('\u0000') + '|' + (USERS ? '1' : '0');
  if(sig === MC_SIG){ return; }
  MC_SIG = sig;
  // 名单里已经没有了的人（号被删了）就别再留着勾选状态
  var keep = {};
  targets.forEach(function(n){ if(MC_PICK[n]){ keep[n] = true; } });
  MC_PICK = keep;
  mcRenderPicker();
}
function mcRenderPicker(){
  var box = $('mc_pick');
  if(!box){ return; }
  var kw = (($('mc_filter') && $('mc_filter').value) || '').trim().toLowerCase();
  var byName = {};
  ((USERS && USERS.users) || []).forEach(function(u){ byName[String(u.name)] = u; });
  var all = mcTargets();
  var list = all.filter(function(n){ return !kw || n.toLowerCase().indexOf(kw) >= 0; });
  if(!list.length){
    box.innerHTML = '<div class="empty">'
      + (all.length ? '没有匹配的用户名' : '还没有别人注册 —— 现在只发得给自己')
      + '</div>';
  } else {
    box.innerHTML = list.map(function(name){
      var u = byName[name];
      var meta = u ? ((u.accounts || []).length + ' 个号') : '';
      if(name === String((STATUS && STATUS.me) || '')){
        meta = meta ? (meta + ' · 我自己') : '我自己';
      }
      return '<label>'
        + '<input type="checkbox" data-pick="' + esc(name) + '"' + (MC_PICK[name] ? ' checked' : '') + '>'
        + '<span class="pn">' + esc(name) + '</span>'
        + (meta ? ('<span class="pmeta">' + esc(meta) + '</span>') : '')
        + '</label>';
    }).join('');
  }
  mcCount();
}
function mcCount(){
  var el = $('mc_count');
  if(el){ el.textContent = '已选 ' + Object.keys(MC_PICK).length + ' 人'; }
}
function msgPrev(t){
  return esc(t).replace(/\r\n|\r|\n/g, '<br>');
}
function mcPrevHtml(){
  var t = (($('mc_text') && $('mc_text').value) || '').trim();
  var names = Object.keys(MC_PICK);
  var who = names.length
    ? ('发给 ' + names.length + ' 人：' + names.slice(0, 6).join('、') + (names.length > 6 ? ' 等' : ''))
    : '还没勾收件人 —— 现在发不出去';
  if(!t){
    return '<div class="m-cap">' + esc(who) + '　（还没写内容）</div>';
  }
  return '<div class="m-cap">' + esc(who) + '</div>'
    + '<div class="msg"><div class="m-head"><b>新消息</b><span class="m-dot"></span><span class="sp"></span>'
    + '<span class="m-at">管理员　现在</span></div>'
    + '<div class="m-body">' + msgPrev(t) + '</div></div>';
}
function mcCheck(){
  var hard = [];
  var t = (($('mc_text') && $('mc_text').value) || '').trim();
  var names = Object.keys(MC_PICK);
  if(!t){ hard.push('还没写消息内容'); }
  else if(t.length > MC_MAX_TEXT){
    hard.push('消息太长了（最多 ' + MC_MAX_TEXT + ' 字，现在 ' + t.length + ' 字）');
  }
  if(!names.length){ hard.push('还没勾收件人'); }
  nuSetFb('mc_fb_pick',
    names.length
      ? ('已选 ' + names.length + ' 人：' + names.slice(0, 8).join('、') + (names.length > 8 ? ' 等' : ''))
      : '一个都没选 —— 消息发不出去',
    names.length ? 'good' : 'warn');
  nuSetFb('mc_fb_text',
    t ? ('内容：' + t.length + ' / ' + MC_MAX_TEXT + ' 字 / ' + t.split(/\r?\n/).length + ' 行')
      : '留空 = 不能发送',
    (t && t.length > MC_MAX_TEXT) ? 'bad' : '');
  if($('mc_prev')){ $('mc_prev').innerHTML = mcPrevHtml(); }
  if($('mc_send')){ $('mc_send').disabled = hard.length > 0; }
  if($('mc_state')){ $('mc_state').textContent = hard.length ? ('还不能发送：' + hard[0]) : '✓ 可以发送'; }
  return hard;
}
function mcRenderList(){
  var box = $('mc_list');
  if(!box){ return; }
  var sent = (mcMessages().sent || []);
  if($('mc_listnote')){
    $('mc_listnote').textContent = sent.length
      ? ('最多显示最近 ' + MC_SHOW + ' 条，服务器共留 ' + sent.length + ' 条')
      : '';
  }
  var html = sent.slice(0, MC_SHOW).map(function(row){
    var total = (row.recipients || []).length;
    var seen = Number(row.read_count || 0);
    var all = total > 0 && seen >= total;
    var detail = (row.read_detail || []).map(function(r){
      return '<span class="rd ' + (r.read ? 'on' : 'no') + '">'
        + esc(r.name) + (r.read ? ('（' + esc(r.read_at || '已读') + '）') : '（未读）')
        + '</span>';
    }).join('');
    return '<div class="sitem">'
      + '<div class="grow">'
      + '<div class="row" style="margin:0 0 2px;gap:6px">'
      + '<span class="badge ' + (all ? 'g' : 'y') + '"><span class="dot"></span>'
      + (total ? (all ? '全部已读' : ('已读 ' + seen + '/' + total)) : '没有收件人') + '</span>'
      + '<span class="muted small">' + esc(row.created_at || '') + '</span>'
      + '</div>'
      + '<div class="sbod">' + msgPrev(String(row.text || '')) + '</div>'
      + '<div class="smeta">'
      + (detail || '<span class="muted">这条没有收件人</span>')
      + '</div>'
      + '</div>'
      + '<button class="sm dghost" type="button" data-mdel="' + esc(row.id) + '">删除</button>'
      + '</div>';
  }).join('');
  box.innerHTML = sent.length ? html : '<div class="empty">还没发过定向消息</div>';
}
function mcLoad(){
  MC_SIG = '';            // 强制按最新名单重建一次
  mcSyncPicker();
  mcCheck();
  mcRenderList();
}
Array.prototype.forEach.call(['mc_text'],
  function(id){ var el = $(id); if(el){ el.addEventListener('input', function(){ mcCheck(); }); } });
if($('mc_filter')){ $('mc_filter').addEventListener('input', function(){ mcRenderPicker(); }); }
if($('mc_pick')){
  $('mc_pick').addEventListener('change', function(e){
    var el = e.target;
    if(!(el && el.getAttribute && el.getAttribute('data-pick') !== null)){ return; }
    var name = el.getAttribute('data-pick');
    if(el.checked){ MC_PICK[name] = true; } else { delete MC_PICK[name]; }
    mcCount();
    mcCheck();
  });
}
if($('mc_all')){
  $('mc_all').onclick = function(){
    var kw = (($('mc_filter') && $('mc_filter').value) || '').trim().toLowerCase();
    mcTargets().forEach(function(n){
      if(!kw || n.toLowerCase().indexOf(kw) >= 0){ MC_PICK[n] = true; }
    });
    mcRenderPicker();
    mcCheck();
  };
}
if($('mc_none')){
  $('mc_none').onclick = function(){
    MC_PICK = {};
    mcRenderPicker();
    mcCheck();
  };
}
if($('mc_reset')){
  $('mc_reset').onclick = function(){
    if(!$('mc_text').value && !Object.keys(MC_PICK).length){ toast('本来就是空的', false); return; }
    $('mc_text').value = '';
    MC_PICK = {};
    mcRenderPicker();
    mcCheck();
    $('mc_state').textContent = '已清空输入（还没发出任何东西）';
  };
}
if($('mc_send')){
  $('mc_send').onclick = function(){
    var hard = mcCheck();
    if(hard.length){ toast(hard[0], false); return; }
    var payload = { text: $('mc_text').value || '', recipients: Object.keys(MC_PICK) };
    $('mc_send').disabled = true;
    $('mc_state').textContent = '发送中…';
    post('api/message/send', payload).then(function(r){
      $('mc_send').disabled = false;
      $('mc_state').textContent = (r && (r.message || r.error)) || '';
      toast((r && (r.message || r.error)) || '', !!(r && r.ok));
      if(r && r.ok){
        $('mc_text').value = '';
        MC_PICK = {};
        mcRenderPicker();
        mcCheck();
        loadStatus();   // 回读一遍：拿到真正的 id / 时间 / 收件人，而不是输入框里的东西
      } else {
        mcCheck();
      }
    });
  };
}
if($('mc_list')){
  // 事件委托：这块每次 status 都会重渲染
  $('mc_list').addEventListener('click', function(e){
    var el = e.target;
    while(el && el !== this && !(el.getAttribute && el.getAttribute('data-mdel'))){ el = el.parentNode; }
    if(!el || el === this || !el.getAttribute){ return; }
    var mid = el.getAttribute('data-mdel');
    var info = null;
    (mcMessages().sent || []).forEach(function(x){ if(x.id === mid){ info = x; } });
    var names = info ? (info.recipients || []) : [];
    if(!window.confirm('删除这条消息？\n\n它已经发给 ' + names.length + ' 个人（'
      + (names.slice(0, 6).join('、') || '无') + (names.length > 6 ? ' 等' : '')
      + '），删掉之后他们控制台上的这条消息会同时消失，已读记录也一起没了。')){ return; }
    el.disabled = true;
    post('api/message/delete', { id: mid }).then(function(r){
      el.disabled = false;
      toast((r && (r.message || r.error)) || '', !!(r && r.ok));
      if(r && r.ok){ loadStatus(); }
    });
  });
}
function loadStatus(){
  getJson('api/status').then(function(s){
    if(!s){ return; }
    STATUS = s;
    renderOverview(s);
    renderAccounts(s);
    renderForcelog(s);
    // 全局设置只在第一次拿到 status 时回填：之后不再动，
    // 免得管理员正在改的时候被 4 秒轮询把输入框冲掉
    if(!GC_LOADED){ GC_LOADED = true; gcLoad(); }
    if(!NC_LOADED){ NC_LOADED = true; ncLoad(); }
    mcSyncPicker();   // 名单变了（有人新注册）就重建勾选框；没变什么都不做
    mcRenderList();   // 「已发出的消息」每次回读，已读回执才跟得上
    if(!MC_LOADED){ MC_LOADED = true; mcCheck(); }
  });
}
function loadUsers(force){
  if(USERS && !force){ renderUsers(); return; }
  getJson('api/users').then(function(d){
    if(!d){ return; }
    USERS = d;
    renderUsers();
    renderOverview(STATUS);
  });
}
function loadSends(){
  getJson('api/sends?limit=' + REC_MAX).then(function(d){
    if(!d){ return; }
    RUNS = d.runs || [];
    REC_CAP = d.max || 2;
    REC_STORED = d.stored || RUNS.length;
    if(REC_SEL >= RUNS.length){ REC_SEL = 0; }
    renderRuns();
    renderOverview(STATUS);
  });
}
function loadLogs(force){
  getText('api/logs').then(function(t){
    var el = $('logBox');
    if(!el){ return; }
    var follow = $('logFollow').checked;
    var nearBottom = (el.scrollHeight - el.scrollTop - el.clientHeight) < 60;
    el.textContent = t || '暂无日志';
    // force=true 表示「日志面板刚打开」：不管以前滚到哪儿，默认翻到最下面
    if(follow && (force || nearBottom)){ el.scrollTop = el.scrollHeight; }
  });
}

function renderOverview(s){
  if(!s){ return; }
  var accs = s.accounts || [];
  var ok = 0, no = 0, wait = 0;
  accs.forEach(function(a){
    var st = acctBadge(a)[0];
    if(st === 'g'){ ok += 1; }
    if(!a.has_cookie){ no += 1; }
    if(a.has_cookie && !a.ready){ wait += 1; }
  });
  $('kAcc').textContent = accs.length;
  $('kOk').textContent = ok;
  $('kNo').textContent = no;
  $('kWait').textContent = wait;
  if(USERS){
    $('kUser').textContent = (USERS.users || []).length;
    $('kFree').textContent = (USERS.free || []).length;
    $('nUser').textContent = (USERS.users || []).length;
  }
  $('nAcc').textContent = accs.length;
  if(s.me){ $('whoami').textContent = s.me + '（管理员）'; }
  if($('accountWho') && s.me){ $('accountWho').textContent = s.me; }
  // 「允许别人自己注册」是个开关：必须按服务端状态回显，不然刷新一下就"自己变回去"了。
  // 用户正在点它的时候不要抢（鼠标按下去那一下会被轮询覆盖）。
  var ra = $('regAllow');
  if(ra && document.activeElement !== ra){ ra.checked = !!s.allow_register; }
  var rs = $('regState');
  if(rs){ rs.textContent = s.allow_register ? '当前：允许（注册页开着）' : '当前：已关闭（只能由你手动开号）'; }

  var au = s.auth || {}, ck = s.checker || {}, rn = s.runner || {};
  var live = s.live || {};
  var chip = $('chipAuth'), txt;
  if(au.running){ chip.className = 'chip y'; txt = '授权浏览器运行中'; }
  else if(ck.running){ chip.className = 'chip y'; txt = '登录检测中'; }
  else { chip.className = 'chip'; txt = '浏览器空闲'; }
  chip.innerHTML = '<span class="dot"></span><span>' + esc(txt) + '</span>';

  var rc = $('chipRun');
  if(rn.running){
    rc.className = 'chip y';
    rc.innerHTML = '<span class="dot"></span><span>正在发送' + (rn.only ? ('：' + esc(rn.only)) : '（全部账号）') + '</span>';
  }
  else if(rn.returncode === null || rn.returncode === undefined){ rc.className = 'chip'; rc.innerHTML = '<span class="dot"></span><span>尚未运行</span>'; }
  else { rc.className = (rn.returncode === 0 ? 'chip g' : 'chip r'); rc.innerHTML = '<span class="dot"></span><span>上次退出码 ' + esc(rn.returncode) + '</span>'; }

  var who = '';
  if(live.mine === false){ who = '（另一个账号的会话，画面只对号主开放）'; }
  else if(live.owner){ who = '当前：' + live.owner; }
  $('brWho').textContent = who;
  var msg = au.message || (ck.running ? ck.message : '') || '空闲';
  $('brMsg').textContent = msg;
  var left = (typeof au.idle_left === 'number') ? au.idle_left : null;
  $('brLeft').textContent = (au.running && left !== null)
    ? ('还有 ' + fmtLeft(left) + '没人操作就会自动关掉浏览器')
    : '';
  // 「检测所有账号」的进度与结果
  var ca = $('chkAll');
  if(ca){
    var bt = ck.batch || null;
    if(bt && bt.running){
      ca.textContent = '批量检测中：已检测 ' + bt.done + '/' + bt.total
        + (bt.current ? ('　正在检测 ' + bt.current) : '')
        + (bt.left ? ('　还剩 ' + bt.left + ' 个') : '')
        + (bt.skipped && bt.skipped.length ? ('　已跳过 ' + bt.skipped.length + ' 个') : '')
        + (bt.cancel ? '　（收到停止请求，本轮跑完就收工）' : '');
    } else {
      var bl = ck.batch_last || {};
      if(bt && bt.total && bt.finished_at){
        ca.textContent = '上次批量检测：共 ' + bt.total + ' 个 · 正常 ' + bt.ok
          + ' · 异常 ' + bt.bad + ' · 跳过 ' + (bt.skipped || []).length;
      } else if(bl.at){
        ca.textContent = '上次批量检测（' + bl.at + '）：共 ' + bl.total + ' 个 · 正常 ' + bl.ok
          + ' · 异常 ' + bl.bad + ' · 跳过 ' + ((bl.skipped || []).length)
          + (bl.stopped ? '　（中途停止）' : '');
      } else {
        ca.textContent = '检测所有账号：逐个打开聊天页确认还能不能发，不会给谁发消息。';
      }
      if(bt && bt.skipped && bt.skipped.length){
        ca.title = bt.skipped.map(function(x){ return x.unique_id + '：' + x.reason; }).join('\n');
      }
    }
  }
  // 有东西在跑的时候「检测所有账号」也点不动，别让人点了被服务端挡回来
  Array.prototype.forEach.call(document.querySelectorAll('[data-act="check-all"]'), function(b){
    var busyAll = !!(ck.running || au.running || rn.running);
    b.disabled = busyAll;
    b.title = busyAll ? '有任务或浏览器正在跑，等它结束' : '逐个检测每个抖音号（不会发消息）';
  });

  var b = $('brBadge');
  var tone = au.running ? 'y' : (ck.running ? 'y' : 'n');
  b.className = 'badge ' + tone;
  // 所有授权会话（最多 max_sessions 个）。谁在用、到哪一步、还有多久自动关。
  var list = $('brList');
  if(list){
    var sess = s.sessions || [];
    var pool = s.pool || {};
    var html = '';
    if(sess.length){
      var full = (pool.size || 2) <= (pool.used || 0);
      html = '<div style="margin-bottom:6px"><b>正在授权（' + sess.length + '/' + (s.max_sessions || 2) + '）</b>'
        + (full ? '<span class="muted small">　最快约 ' + fmtWait(pool.wait_seconds) + '后空出一个位</span>' : '')
        + '</div>' + sess.map(function(x){
        var t = x.phase === 'sms' ? 'y' : (x.running ? 'g' : 'n');
        return '<div class="row tight" style="margin-bottom:4px">'
          + '<span class="badge ' + (x.running ? t : 'n') + '"><span class="dot"></span>' + esc(x.running ? (x.phase || '运行中') : '已结束') + '</span>'
          + '<b class="mono">' + esc(x.owner || '—') + '</b>'
          + (x.idle_left !== null && x.idle_left !== undefined && x.running ? '<span class="muted small">还有 ' + fmtLeft(x.idle_left) + '自动关</span>' : '')
          + '<span class="muted small">' + esc((x.message || '').slice(0, 40)) + '</span>'
          + '</div>';
      }).join('');
    } else {
      html = '<span class="muted">现在没有授权浏览器在跑' +
        (s.mem_available_mb ? ('（可用内存 ' + s.mem_available_mb + ' MB）') : '') + '</span>';
    }
    list.innerHTML = html;
  }
  b.innerHTML = '<span class="dot"></span>' + (au.running ? '授权中' : (ck.running ? '检测中' : '空闲'));

  // 最近一次发送
  var box = $('lastRun');
  if(!RUNS.length){ box.innerHTML = '<span class="muted">还没有发送记录</span>'; }
  else {
    var r = RUNS[0];
    var tone2 = r.status === 'ok' ? 'g' : (r.status === 'partial' ? 'y' : (r.status === 'no_friend' ? 'n' : 'r'));
    var sent = 0, failed = 0;
    (r.friends || []).forEach(function(f){ if(f.ok){ sent += 1; } else { failed += 1; } });
    box.innerHTML = '<div class="row tight" style="margin-bottom:6px">'
      + '<span class="badge ' + tone2 + '"><span class="dot"></span>' + esc(r.status || '') + '</span>'
      + '<b>' + esc(r.account || '') + '</b><span class="muted">' + esc(r.at || '') + '</span></div>'
      + '<div>成功 ' + sent + ' 个 · 失败/跳过 ' + failed + ' 个'
      + ((r.targets || []).length ? ('（目标 ' + (r.targets || []).length + ' 个）') : '') + '</div>'
      + (r.detail ? ('<div class="muted">' + esc(r.detail) + '</div>') : '');
  }

  // 需要关注
  var need = [];
  accs.forEach(function(a){
    var st = acctBadge(a);
    if(st[2]){
      need.push('<div class="row tight" style="margin-bottom:4px">'
        + '<span class="badge ' + st[0] + '"><span class="dot"></span>' + esc(st[1]) + '</span>'
        + '<b class="mono">' + esc(a.unique_id) + '</b>'
        + (a.username && a.username !== a.unique_id ? ('<span class="muted">' + esc(a.username) + '</span>') : '')
        + (a.login_user ? ('<span class="muted small">归属：' + esc(a.login_user) + '</span>') : '')
        + '</div>');
    }
  });
  $('needAttention').innerHTML = need.length ? need.join('') : '<span class="muted">都正常，没有要处理的。</span>';
}

function renderAccounts(s){
  if(!s){ return; }
  var kw = ($('accFilter').value || '').trim().toLowerCase();
  var rows = (s.accounts || []).filter(function(a){
    if(!kw){ return true; }
    return (String(a.unique_id || '') + ' ' + String(a.username || '') + ' ' + String(a.login_user || '')).toLowerCase().indexOf(kw) >= 0;
  });
  var body = $('accBody');
  if(!rows.length){
    body.innerHTML = '<tr><td colspan="7" class="muted">没有匹配的抖音号</td></tr>';
    return;
  }
  body.innerHTML = rows.map(function(a){
    var st = acctBadge(a);
    // 和 acctBadge 同一套判断：扫码授权留下的记录不是检测结果
    var ck = (a.check && a.check.source !== 'qr') ? a.check : null;
    var cookie = a.has_cookie ? (a.cookie_count + ' 项') : '—';
    var targets = (a.targets && a.targets.length) ? esc(a.targets.join('、')) : '<span class="muted">未填</span>';
    // 有东西在跑（浏览器 / 检测 / 发送任务）时不让点运行，服务端还会再挡一次
    var busy = !!(s && ((s.runner || {}).running || (s.auth || {}).running || (s.checker || {}).running));
    var runBtn = '<button class="sm" type="button" data-run="' + esc(a.unique_id) + '"'
      + (busy ? ' disabled title="有任务或浏览器正在跑，等它结束"' : ' title="只给这一个号跑一轮"') + '>运行</button>';
    return '<tr>'
      + '<td class="mono"><b>' + esc(a.unique_id) + '</b></td>'
      + '<td>' + (a.username && a.username !== a.unique_id ? esc(a.username) : '<span class="muted">—</span>') + '</td>'
      + '<td>' + (a.login_user ? esc(a.login_user) : '<span class="muted">未分配</span>') + '</td>'
      + '<td><span class="badge ' + st[0] + '"><span class="dot"></span>' + esc(st[1]) + '</span>'
      + (ck && ck.at
          ? ('<div class="muted small" title="上次检测时间">' + esc(ck.at) + '</div>')
          : (a.saved_at ? ('<div class="muted small" title="上次授权成功时间">授权 ' + esc(a.saved_at) + '</div>') : ''))
      + '</td>'
      + '<td>' + esc(cookie) + '</td>'
      + '<td class="tgt" title="' + (a.targets && a.targets.length ? esc(a.targets.join('、')) : '') + '">' + targets
      + ((a.times_today || []).filter(Boolean).length ? '<div class="small muted">每天 ' + esc((a.times_today || []).filter(Boolean).join('、')) + ' · 约 ' + esc(a.estimate_minutes || 5) + ' 分钟</div>' : '<div class="small muted">未设置自动发送时间</div>') + '</td>'
      + '<td><div class="acts">'
      + runBtn
      + '<button class="sm" type="button" data-edit="' + esc(a.unique_id) + '">编辑</button>'
      + '<button class="sm ghost" type="button" data-check="' + esc(a.unique_id) + '">检测</button>'
      + '<button class="sm ghost" type="button" data-start="' + esc(a.unique_id) + '">授权</button>'
      + '<button class="sm ghost" type="button" data-cookie="' + esc(a.unique_id) + '">Cookie</button>'
      + '<button class="sm dghost" type="button" data-del="' + esc(a.unique_id) + '">删除</button>'
      + '</div></td></tr>';
  }).join('');
}

function renderUsers(){
  if(!USERS){ return; }
  var list = USERS.users || [];
  if(list.length){ $('nUser').textContent = list.length; }
  var body = $('userBody');
  if(!list.length){
    body.innerHTML = '<tr><td colspan="8" class="muted">还没有别人注册</td></tr>';
  } else {
    body.innerHTML = list.map(function(u){
      var accs = (u.accounts || []).map(function(a){
        var tag = !a.exists ? ['n', '配置已删除']
          : (a.task_login_failed ? (a.has_cookie ? ['r', '登录已失效'] : ['n', '未授权'])
          : (a.check_ok === true ? ['g', '登录正常']
          : (!a.has_cookie ? ['n', '未授权']
          : (a.check_ok === false ? ['r', '登录失效'] : ['n', '未检测']))));
        return '<div class="row tight" style="margin-bottom:3px">'
          + '<b class="mono">' + esc(a.unique_id) + '</b>'
          + '<span class="badge ' + tag[0] + '"><span class="dot"></span>' + tag[1] + '</span>'
          + '<button class="sm link" type="button" data-unbind="' + esc(a.unique_id) + '" data-user="' + esc(u.name) + '">解绑</button>'
          + '</div>';
      }).join('') || '<span class="muted">还没绑</span>';
      return '<tr>'
        + '<td><b>' + esc(u.name) + '</b></td>'
        + '<td class="muted small mono">' + (u.phone ? esc(u.phone) : '—') + '</td>'
        + '<td class="muted small">' + esc(u.at || '—') + '</td>'
        + '<td class="muted small">' + (u.last_login ? esc(u.last_login) : '从未') + '</td>'
        + '<td class="muted small mono">' + (u.last_ip ? esc(u.last_ip) : '—') + '</td>'
        + '<td>' + accs + '</td>'
        + '<td>' + esc((u.subscription || {}).unlimited ? '永久有效' : ((u.subscription || {}).remaining_text || '已到期'))
        + ((u.subscription || {}).expires_text ? '<br><span class="muted small">至 ' + esc((u.subscription || {}).expires_text) + '</span>' : '') + '</td>'
        + '<td><div class="acts">'
        + '<button class="sm ghost" type="button" data-pass="' + esc(u.name) + '">改密码</button>'
        + '<button class="sm dghost" type="button" data-deluser="' + esc(u.name) + '">删除</button>'
        + '</div></td></tr>';
    }).join('');
  }
  var free = USERS.free || [];
  $('freeIds').innerHTML = free.length
    ? ('未分配给任何人的抖音号：' + free.map(function(x){ return '<b class="mono">' + esc(x) + '</b>'; }).join('、'))
    : '所有抖音号都已经分配出去了';
  var sel = $('bd_user'), keep = sel.value;
  sel.innerHTML = list.map(function(u){ return '<option value="' + esc(u.name) + '">' + esc(u.name) + '</option>'; }).join('');
  if(keep){ sel.value = keep; }
  var ss = $('sc_user');
  if(ss){ var old = ss.value; ss.innerHTML = list.map(function(u){ return '<option value="' + esc(u.name) + '">' + esc(u.name) + '</option>'; }).join(''); if(old){ ss.value = old; } }
}
function loadSubscriptionAdmin(){
  getJson('api/redeem/codes').then(function(d){
    var body = $('sc_history');
    if(!body){ return; }
    var rows = d.codes || [];
    body.innerHTML = rows.length ? rows.map(function(x){ return '<tr><td>' + esc(x.label || (x.days + ' 天')) + '</td><td class="muted small">' + esc(x.created_at || '—') + '</td><td><span class="badge ' + (x.used ? 'n' : 'g') + '"><span class="dot"></span>' + (x.used ? '已使用' : '未使用') + '</span></td><td>' + esc(x.used_by || '—') + '</td></tr>'; }).join('') : '<tr><td colspan="4" class="muted">还没有生成过兑换码</td></tr>';
  });
}

function renderRuns(){
  var list = $('recList'), detail = $('recDetail');
  var kw = ($('recFilter').value || '').trim().toLowerCase();
  $('recCount').textContent = RUNS.length
    ? ('当前能看 ' + RUNS.length + ' 条（上限 ' + REC_CAP + ' 条，服务器共留 ' + REC_STORED + ' 条）')
    : '';
  if(!RUNS.length){
    list.innerHTML = '<div class="empty">还没有发送记录</div>';
    detail.innerHTML = '';
    return;
  }
  var rows = [];
  RUNS.forEach(function(r, i){
    var text = (String(r.account || '') + ' ' + String(r.status || '') + ' ' + String(r.at || '')).toLowerCase();
    if(kw && text.indexOf(kw) < 0){ return; }
    rows.push(i);
  });
  if(!rows.length){
    list.innerHTML = '<div class="empty">没有匹配的记录</div>';
    detail.innerHTML = '';
    return;
  }
  if(rows.indexOf(REC_SEL) < 0){ REC_SEL = rows[0]; }
  list.innerHTML = rows.map(function(i){
    var r = RUNS[i], tone = SEND_TONE[r.status] || 'n';
    var sent = 0, bad = 0;
    (r.friends || []).forEach(function(f){ if(f.ok){ sent += 1; } else { bad += 1; } });
    return '<div class="item' + (i === REC_SEL ? ' on' : '') + '" data-rec="' + i + '">'
      + '<span class="badge ' + tone + '"><span class="dot"></span>' + esc(r.status || '') + '</span>'
      + '<div class="meta"><b>' + esc(r.account || '') + '</b>'
      + '<span>' + esc(r.at || '') + '　成功 ' + sent + ' · 未成功 ' + bad + '</span></div>'
      + '</div>';
  }).join('');
  renderRecDetail();
}
function renderRecDetail(){
  var detail = $('recDetail');
  var r = RUNS[REC_SEL];
  if(!r){ detail.innerHTML = '<div class="empty">左边选一条记录</div>'; return; }
  var tone = SEND_TONE[r.status] || 'n';
  var html = '<div class="row tight" style="margin-bottom:8px">'
    + '<span class="badge ' + tone + '"><span class="dot"></span>' + esc(r.status || '') + '</span>'
    + '<b>' + esc(r.account || '') + '</b><span class="muted">' + esc(r.at || '') + '</span>'
    + (r.unique_id ? '<span class="mono muted small">' + esc(r.unique_id) + '</span>' : '') + '</div>';
  if(r.detail){ html += '<p class="muted small" style="margin:0 0 8px">' + esc(r.detail) + '</p>'; }
  var friends = r.friends || [];
  if(!friends.length && (r.targets || []).length){
    html += '<p class="small">目标好友：' + r.targets.map(esc).join('、') + '</p>';
  }
  if(friends.length){
    html += '<div class="tblwrap"><table><thead><tr><th>好友</th><th>结果</th><th>说明</th><th>截图</th></tr></thead><tbody>'
      + friends.map(function(f){
        return '<tr><td><b>' + esc(f.name) + '</b></td>'
          + '<td><span class="badge ' + (f.ok ? 'g' : 'r') + '"><span class="dot"></span>' + (f.ok ? '已发送' : '没发出去') + '</span></td>'
          + '<td class="muted small">' + esc(f.detail || '') + '</td>'
          + '<td>' + (f.shot ? ('<button class="sm link" type="button" data-shot="' + esc(f.shot) + '">看大图</button>') : '<span class="muted">—</span>') + '</td></tr>';
      }).join('') + '</tbody></table></div>';
  }
  var shots = friends.filter(function(f){ return f.shot; }).map(function(f){ return f.shot; });
  if(shots.length){
    html += '<div class="shots">' + shots.map(function(n){
      return '<img src="api/shot?name=' + encodeURIComponent(n) + '" data-shot="' + esc(n) + '" loading="lazy" alt="发送截图">';
    }).join('') + '</div>';
  }
  detail.innerHTML = html;
}

function renderForcelog(s){
  if(!s){ return; }
  var rows = s.force_log || [];
  var box = $('forceLog');
  box.innerHTML = rows.length
    ? rows.map(function(t){ return '<div class="mono small">' + esc(t) + '</div>'; }).join('')
    : '<span class="muted">最近没有强制操作</span>';
  var br = $('brMsg');
  if(br && s.auth && s.auth.force_hint){ br.textContent = s.auth.force_hint; }
}

// ---- 操作 ----
function act(url, data, okMsg){
  return post(url, data).then(function(r){
    toast(r.message || r.error || (r.ok ? (okMsg || '完成') : '失败'), !!r.ok);
    loadStatus();
    return r;
  });
}
document.addEventListener('click', function(ev){
  var el = ev.target;
  while(el && el !== document.body && !(el.getAttribute && (el.getAttribute('data-check') || el.getAttribute('data-start')
    || el.getAttribute('data-cookie') || el.getAttribute('data-del') || el.getAttribute('data-unbind')
    || el.getAttribute('data-pass') || el.getAttribute('data-deluser') || el.getAttribute('data-act')
    || el.getAttribute('data-shot') || el.getAttribute('data-run') || el.getAttribute('data-edit')
    || el.getAttribute('data-rec') !== null))){
    el = el.parentNode;
  }
  if(!el || el === document.body || !el.getAttribute){ return; }
  var uid, user;
  if((uid = el.getAttribute('data-edit'))){
    nuEdit(uid);
  } else if((uid = el.getAttribute('data-run'))){
    if(!window.confirm('只给「' + uid + '」的目标好友真实发送一轮消息？\n（不会动其他账号）')){ return; }
    act('api/run', {unique_id: uid}, '已开始运行 ' + uid);
  } else if((uid = el.getAttribute('data-check'))){
    act('api/check', {unique_id: uid}, '已开始检测 ' + uid);
  } else if((uid = el.getAttribute('data-start'))){
    act('api/browser/start', {unique_id: uid, username: ''}, '已给 ' + uid + ' 开授权浏览器');
  } else if((uid = el.getAttribute('data-cookie'))){
    getJson('api/cookie?unique_id=' + encodeURIComponent(uid)).then(function(d){
      if(!d || !d.ok){ toast((d && d.error) || '读不到 Cookie', false); return; }
      var ta = document.createElement('textarea');
      ta.value = d.cookie_text || '';
      ta.style.position = 'fixed'; ta.style.top = '-1000px';
      document.body.appendChild(ta);
      ta.select();
      var ok = false;
      try { ok = document.execCommand('copy'); } catch(e){ ok = false; }
      document.body.removeChild(ta);
      toast(ok ? ('已复制「' + (d.username || uid) + '」的 Cookie（' + d.cookie_count + ' 项）')
               : '浏览器不让自动复制，请手动复制', ok);
      if(!ok){ window.prompt('手动复制这串 Cookie：', d.cookie_text || ''); }
    });
  } else if((uid = el.getAttribute('data-del'))){
    if(!window.confirm('删除抖音号「' + uid + '」？它的 Cookie 和检测记录会一起删掉。')){ return; }
    act('api/account/delete', {unique_id: uid}, '已删除 ' + uid);
  } else if((uid = el.getAttribute('data-unbind'))){
    user = el.getAttribute('data-user');
    if(!window.confirm('把「' + uid + '」从 ' + user + ' 名下解绑？解绑后他看不到这个号，配置还在。')){ return; }
    act('api/user/unbind', {name: user, unique_id: uid}, '已解绑');
  } else if((user = el.getAttribute('data-pass'))){
    var pw = window.prompt('给「' + user + '」设置新密码（至少 6 位）：');
    if(!pw){ return; }
    post('api/user/password', {name: user, password: pw}).then(function(r){
      toast(r.ok ? ('新密码：' + (r.password || pw) + '（只显示这一次）') : (r.error || '失败'), !!r.ok);
      loadUsers(true);
    });
  } else if((user = el.getAttribute('data-deluser'))){
    if(!window.confirm('删除用户「' + user + '」？他立刻登不上控制台；他绑的抖音号配置会保留。')){ return; }
    act('api/user/delete', {name: user}, '已删除用户 ' + user).then(function(){ loadUsers(true); });
  } else if(el.getAttribute('data-rec') !== null){
    var idx = parseInt(el.getAttribute('data-rec'), 10);
    REC_SEL = isNaN(idx) ? 0 : idx;
    renderRuns();
  } else if((uid = el.getAttribute('data-shot'))){
    $('shotImg').src = 'api/shot?name=' + encodeURIComponent(uid);
    $('shotview').hidden = false;
  } else if((uid = el.getAttribute('data-act'))){
    if(uid === 'force-stop'){
      act('api/force/stop', {reason: '管理控制台'}, '已强制停止');
    } else if(uid === 'force-restart'){
      if(!window.confirm('确定强制重启面板后端？大约 10 秒后自动恢复。')){ return; }
      post('api/force/restart', {}).then(function(r){
        toast(r.message || r.error || '正在重启', !!r.ok);
        setTimeout(function(){ location.reload(); }, 15000);
      });
    } else if(uid === 'check-all'){
      if(!window.confirm('逐个检测所有抖音号？\n只打开聊天页确认还能不能发，不会给谁发消息。\n每个号大约 20-60 秒，期间会占用浏览器和内存。')){ return; }
      act('api/check/all', {}, '已开始逐个检测');
    } else if(uid === 'run'){
      if(!window.confirm('立即给全部账号的目标好友真实发送一轮消息，确定吗？')){ return; }
      act('api/run', {}, '已开始运行');
    }
  }
});

$('shotClose').onclick = function(){ $('shotview').hidden = true; $('shotImg').removeAttribute('src'); };
$('shotview').onclick = function(ev){ if(ev.target === this){ $('shotClose').onclick(); } };
document.addEventListener('keydown', function(ev){ if(ev.key === 'Escape'){ $('shotview').hidden = true; } });

$('accFilter').addEventListener('input', function(){ renderAccounts(STATUS); });
$('recFilter').addEventListener('input', function(){ renderRuns(); });
$('recRefresh').onclick = function(){ loadSends(); toast('已刷新', true); };
$('accRefresh').onclick = function(){ loadStatus(); toast('已刷新', true); };
$('usersRefresh').onclick = function(){ loadUsers(true); toast('已刷新', true); };
$('logRefresh').onclick = function(){ loadLogs(); toast('已刷新', true); };
// ---- 新建 / 编辑抖音号：两个入口共用同一张卡片 ----
// 注意：后端 save_config 是「整体覆盖」（entry["settings"] = {...}），
// 所以编辑时必须把字段全部回填、全部提交，漏一个就会把那个号的值清掉。
var NU_EDITING = '';          // 非空 = 正在编辑这个抖音号
function nuLines(text){
  // 和后端一致：按换行 / 逗号 / 分号拆，去空项
  return String(text || '').split(/[\r\n,，;；]+/).map(function(x){ return x.trim(); })
    .filter(function(x){ return x; });
}
function nuPad2(n){ n = String(n); return n.length < 2 ? ('0' + n) : n; }
function nuTimeNorm(line){
  // 返回规整后的 'HH:MM[:SS]'；false = 非法；null = 空行。规则和后端 save_config 一一对应
  var t = String(line || '').replace(/：/g, ':').trim();
  if(!t){ return null; }
  var parts = t.split(':').filter(function(p){ return p !== ''; });
  var nums = [];
  for(var i = 0; i < parts.length; i++){
    // 逐段 trim：后端是 int(p)，Python 的 int(" 9") 也认，别让前端比后端更严
    var seg = parts[i].trim();
    if(!/^\d+$/.test(seg)){ return false; }
    nums.push(parseInt(seg, 10));
  }
  if(nums.length === 1){ nums = [nums[0], 0, 0]; }
  else if(nums.length === 2){ nums = [nums[0], nums[1], 0]; }
  else if(nums.length !== 3){ return false; }
  var h = nums[0], mi = nums[1], se = nums[2];
  if(!(h >= 0 && h <= 23 && mi >= 0 && mi <= 59 && se >= 0 && se <= 59)){ return false; }
  return nuPad2(h) + ':' + nuPad2(mi) + (se ? (':' + nuPad2(se)) : '');
}
function nuShotName(text){
  // 和后端 shot_name_of 同一套算法：用它提前发现「名称重名」
  return String(text || '').replace(/[^0-9A-Za-z\u4e00-\u9fff_-]+/g, '_')
    .replace(/^_+|_+$/g, '').slice(0, 24);
}
function nuAcctOf(uid){
  var list = (STATUS && STATUS.accounts) || [];
  for(var i = 0; i < list.length; i++){
    if(list[i].unique_id === uid){ return list[i]; }
  }
  return null;
}
function nuSetFb(id, text, tone){
  var el = $(id);
  if(!el){ return; }
  el.textContent = text || '';
  el.className = 'nufb' + (tone ? (' ' + tone) : '');
}
var nuScheduleTimer = null, NU_SCHEDULE_AUTOFILL = false;
function nuScheduleCheckNow(){
  var uid = ($('nu_uid').value || '').trim();
  return post('api/schedule/check', {
    orig_unique_id: NU_EDITING || '', unique_id: uid,
    schedule_times: $('nu_times').value || '', targets: $('nu_targets').value || '',
    delay_max: $('nu_dmax').value || '0'
  }).then(function(r){
    if(!r || !r.ok){ nuSetFb('nu_fb_times', (r && r.error) || '发送时间检查失败', 'bad'); return r; }
    if(NU_SCHEDULE_AUTOFILL && !$('nu_times').value.trim() && r.recommended){
      $('nu_times').value = r.recommended;
      NU_SCHEDULE_AUTOFILL = false;
    }
    var specs = nuLines($('nu_times').value);
    if(r.conflicts && r.conflicts.length){
      nuSetFb('nu_fb_times', '与其他账号的任务时间重叠：' + r.conflicts.map(function(x){ return x.time + '（已有 ' + x.other_time + '）'; }).join('、'), 'warn');
    } else if(specs.length){
      nuSetFb('nu_fb_times', '预计约 ' + (r.estimate_minutes || 5) + ' 分钟完成；当前时段没有重叠。', 'good');
    } else {
      nuSetFb('nu_fb_times', '自动发送时间未设置。推荐空闲时间：' + (r.recommended || '暂无'), 'warn');
    }
    return r;
  });
}
function nuScheduleCheckLater(){
  if(nuScheduleTimer){ clearTimeout(nuScheduleTimer); }
  nuScheduleTimer = setTimeout(function(){ nuScheduleCheckNow().catch(function(){}); }, 450);
}
function nuNum(raw){
  // 后端是 int(float(str(x)))，这里只认纯数字（可带小数、允许 "6." 这种写法），
  // 并且和后端一样**截断取整**（"6.5" 两边都算 6），
  // 挡住 0x10 / 1e3 / -5 这类前端放行、后端必报错的输入
  var s = String(raw || '').trim();
  if(!/^\d+\.?\d*$/.test(s)){ return null; }
  return Math.trunc(Number(s));
}
function nuCheck(){
  // 硬错误 = 和后端规则完全一致、必然被拒的项（挡住保存）
  // 软提示 = 后端允许、但值得提前提醒的项
  var uid = ($('nu_uid').value || '').trim();
  var name = ($('nu_name').value || '').trim();
  var g = (STATUS && STATUS.global) || {};
  var hard = [];
  var accounts = (STATUS && STATUS.accounts) || [];

  // 1) 抖音号
  if(!uid){
    hard.push('抖音号必须填');
  } else if(!/^[A-Za-z0-9_-]+$/.test(uid)){
    hard.push('抖音号只能用字母、数字、下划线或短横线');
  } else if(!NU_EDITING){
    var hitUid = accounts.filter(function(x){ return x.unique_id === uid; })[0];
    if(hitUid){ hard.push('抖音号「' + uid + '」已经存在了；要改它的配置请点那一行的「编辑」'); }
  }

  // 2) 名称（重名会让截图串号，后端是硬拦的）
  var effName = name || uid;
  var myKey = nuShotName(effName);
  if(myKey){
    var dupName = accounts.filter(function(x){
      return x.unique_id !== NU_EDITING && nuShotName(x.username || x.unique_id) === myKey;
    })[0];
    if(dupName){
      hard.push('名称「' + effName + '」已经被「' + dupName.unique_id + '」用了，换一个（名称用来区分截图，不能重名）');
      nuSetFb('nu_fb_name', '和「' + dupName.unique_id + '」的截图名撞了', 'bad');
    } else {
      nuSetFb('nu_fb_name', name ? ('截图 / 记录里显示为「' + effName + '」') : '不填就用抖音号当名称', '');
    }
  } else {
    nuSetFb('nu_fb_name', '', '');
  }

  // 3) 目标好友
  var targets = nuLines($('nu_targets').value);
  if(!targets.length){
    nuSetFb('nu_fb_targets', '还没填目标好友：手动运行时会跳过这个号', 'warn');
  } else {
    var seen = {}, repeats = [];
    targets.forEach(function(t){ if(seen[t]){ repeats.push(t); } seen[t] = 1; });
    nuSetFb('nu_fb_targets', '已填 ' + targets.length + ' 个好友'
      + (repeats.length ? ('（重复：' + repeats.join('、') + '）') : ''), repeats.length ? 'warn' : 'good');
  }

  // 5) 消息模板
  var msg = $('nu_msg').value || '';
  if(!msg.trim()){
    nuSetFb('nu_fb_msg', '留空 = 用全局模板', 'warn');
  } else {
    nuSetFb('nu_fb_msg', '自定义模板：' + msg.trim().length + ' 字 / ' + msg.split(/\r?\n/).length + ' 行'
      + (msg.indexOf('[API]') < 0 ? '（不含 [API]，不会插入每日一句）' : ''), '');
  }

  // 6) 发送间隔
  var dmin = nuNum($('nu_dmin').value), dmax = nuNum($('nu_dmax').value);
  if(dmin === null || dmax === null || !(dmin >= 0 && dmin <= 600 && dmax >= 0 && dmax <= 600)){
    hard.push('发送间隔要填 0~600 的数字（秒），两个框都要填');
    nuSetFb('nu_fb_delay', '两个框都要填 0~600 的数字', 'bad');
  } else if(dmax < dmin){
    nuSetFb('nu_fb_delay', '上限比下限小，保存时会自动对调', 'warn');
  } else {
    nuSetFb('nu_fb_delay', dmin === dmax ? ('固定 ' + dmin + ' 秒') : (dmin + '~' + dmax + ' 秒之间随机'), 'good');
  }

  // 7) 一言类型
  var hito = ($('nu_hito').value || '').trim();
  if(!hito){
    nuSetFb('nu_fb_hito', '留空 = 用全局默认', '');
  } else {
    var kinds = null;
    try { kinds = JSON.parse(hito); } catch(e){ kinds = null; }
    if(!(kinds instanceof Array) || !kinds.length){
      hard.push('一言类型必须是 JSON 数组，例如 [\"a\",\"b\"]');
      nuSetFb('nu_fb_hito', '这串不是合法的非空 JSON 数组', 'bad');
    } else {
      nuSetFb('nu_fb_hito', '已填 ' + kinds.length + ' 个类型', 'good');
    }
  }

  var btn = $('nu_save');
  if(btn){ btn.disabled = hard.length > 0; }
  $('nu_state').textContent = hard.length ? ('还不能保存：' + hard[0])
    : (NU_EDITING ? '✓ 可以保存（只改这一个号）' : '✓ 可以保存');
  return hard;
}
function nuReset(show){
  NU_EDITING = '';
  var g = (STATUS && STATUS.global) || {};
  $('nu_title').textContent = '新建抖音号';
  $('nu_sub').textContent = '填写「抖音号」和「名称」即可创建，授权登录后再补目标好友。';
  $('nu_uid').value = '';
  $('nu_uid').readOnly = false;
  $('nu_uid_hint').textContent = '自己起个标识，例如 myspark';
  $('nu_name').value = '';
  $('nu_targets').value = '';
  $('nu_times').value = '';
  NU_SCHEDULE_AUTOFILL = !!show;
  $('nu_msg').value = g.message_template || '';
  $('nu_dmin').value = (g.delay_min === undefined ? '0' : g.delay_min);
  $('nu_dmax').value = (g.delay_max === undefined ? '0' : g.delay_max);
  $('nu_hito').value = g.hitokoto_types || '';
  if(show){ $('accNew').hidden = false; }
  nuCheck();
  nuScheduleCheckLater();
}
function nuEdit(uid){
  var a = nuAcctOf(uid);
  if(!a){ toast('没找到「' + uid + '」，先刷新一下', false); return; }
  var g = (STATUS && STATUS.global) || {};
  var st = a.settings || {};
  NU_EDITING = uid;
  NU_SCHEDULE_AUTOFILL = false;
  $('nu_title').textContent = '编辑抖音号：' + uid;
  $('nu_sub').textContent = '改完点「保存」立刻生效；正在跑的任务不受影响，下一轮用新配置。';
  $('nu_uid').value = uid;
  $('nu_uid').readOnly = true;
  $('nu_uid_hint').textContent = '抖音号是账号标识，建好后不支持改名；要换号请新建一个';
  $('nu_name').value = a.username || '';
  $('nu_targets').value = (a.targets || []).join('\n');
  $('nu_times').value = (a.times || []).join('\n');
  $('nu_msg').value = (st.template !== undefined) ? st.template : (g.message_template || '');
  $('nu_dmin').value = (st.delay_min !== undefined) ? st.delay_min : (g.delay_min === undefined ? '0' : g.delay_min);
  $('nu_dmax').value = (st.delay_max !== undefined) ? st.delay_max : (g.delay_max === undefined ? '0' : g.delay_max);
  $('nu_hito').value = (st.hitokoto_types !== undefined) ? st.hitokoto_types : (g.hitokoto_types || '');
  $('accNew').hidden = false;
  nuCheck();
  nuScheduleCheckLater();
  if($('accNew').scrollIntoView){ $('accNew').scrollIntoView({block: 'nearest'}); }
  if($('nu_name').focus){ $('nu_name').focus(); }
}
Array.prototype.forEach.call(['nu_uid', 'nu_name', 'nu_targets', 'nu_times', 'nu_msg', 'nu_dmin', 'nu_dmax', 'nu_hito'],
  function(id){ var el = $(id); if(el){ el.addEventListener('input', function(){ nuCheck(); }); } });
['nu_times','nu_targets','nu_dmax'].forEach(function(id){ var el = $(id); if(el){ el.addEventListener('input', function(){ if(id === 'nu_times'){ NU_SCHEDULE_AUTOFILL = false; } nuScheduleCheckLater(); }); } });

$('accNewBtn').onclick = function(){
  if($('accNew').hidden){ nuReset(true); } else { $('accNew').hidden = true; }
};
$('nu_cancel').onclick = function(){ $('accNew').hidden = true; NU_EDITING = ''; };
$('nu_save').onclick = function(){
  var hard = nuCheck();
  if(hard.length){ toast(hard[0], false); return; }
  var g = (STATUS && STATUS.global) || {};
  var wasEditing = NU_EDITING;
  $('nu_state').textContent = '保存中…';
  $('nu_save').disabled = true;
  nuScheduleCheckNow().then(function(sr){
    if(sr && !sr.ok){ return sr; }
    if(sr && sr.conflicts && sr.conflicts.length && !window.confirm('发送时间与其他账号的任务重叠：\n\n'
      + sr.conflicts.map(function(x){ return x.time + '（已有任务 ' + x.other_time + '）'; }).join('\n')
      + '\n\n仍然保存吗？')){ return null; }
    return post('api/config', {
    orig_unique_id: wasEditing || '',
    unique_id: ($('nu_uid').value || '').trim(),
    username: ($('nu_name').value || '').trim(),
    targets: $('nu_targets').value || '',
    schedule_times: $('nu_times').value || '',
    message_template: $('nu_msg').value || '',
    delay_min: ($('nu_dmin').value || '').trim(),
    delay_max: ($('nu_dmax').value || '').trim(),
    tz: g.tz || '', log_level: g.log_level || '',
    hitokoto_types: ($('nu_hito').value || '').trim(),
    cookie_json: '',
    save_global: ''
    });
  }).then(function(r){
    if(!r){ $('nu_save').disabled = false; return; }
    $('nu_save').disabled = false;
    if(r.ok){
      $('accNew').hidden = true;
      nuReset(false);
      loadStatus();
      toast(wasEditing ? ('已更新「' + wasEditing + '」的发送配置') : (r.message || '已保存'), true);
    } else {
      $('nu_state').textContent = r.error || '保存失败';
      toast(r.error || '保存失败', false);
      nuCheck();
      $('nu_state').textContent = r.error || '保存失败';
    }
  }).catch(function(){
    $('nu_save').disabled = false;
    $('nu_state').textContent = '保存失败：网络断了或后端没响应';
    toast('保存失败：网络断了或后端没响应', false);
  });
};

// ================= 全局发送设置 =================
function gcCheck(){
  var hard = [];
  var dm = nuNum($('gc_dmin').value), dx = nuNum($('gc_dmax').value);
  if(dm === null || dx === null || !(dm >= 0 && dm <= 600 && dx >= 0 && dx <= 600)){
    hard.push('全局发送间隔要填 0~600 的数字，两个框都要填');
    nuSetFb('gc_fb_delay', '两个框都要填 0~600 的数字', 'bad');
  } else if(dx < dm){
    nuSetFb('gc_fb_delay', '上限比下限小，保存时会自动对调', 'warn');
  } else {
    nuSetFb('gc_fb_delay', dm === dx ? ('固定 ' + dm + ' 秒') : (dm + '~' + dx + ' 秒之间随机'), 'good');
  }

  var hito = ($('gc_hito').value || '').trim();
  if(!hito){
    nuSetFb('gc_fb_hito', '留空 = 用内置默认一言', '');
  } else {
    var kinds = null;
    try { kinds = JSON.parse(hito); } catch(e){ kinds = null; }
    if(!(kinds instanceof Array) || !kinds.length){
      hard.push('全局一言类型必须是 JSON 数组，例如 ["a","b"]');
      nuSetFb('gc_fb_hito', '这串不是合法的非空 JSON 数组', 'bad');
    } else {
      nuSetFb('gc_fb_hito', '已填 ' + kinds.length + ' 个类型', 'good');
    }
  }

  var msg = $('gc_msg').value || '';
  if(!msg.trim()){
    nuSetFb('gc_fb_msg', '留空 = 用内置默认模板', 'warn');
  } else {
    nuSetFb('gc_fb_msg', '全局模板：' + msg.trim().length + ' 字 / ' + msg.split(/\r?\n/).length + ' 行'
      + (msg.indexOf('[API]') < 0 ? '（不含 [API]，不会插入每日一句）' : ''), '');
  }

  var btn = $('gc_save');
  if(btn){ btn.disabled = hard.length > 0; }
  $('gc_state').textContent = hard.length ? ('还不能保存：' + hard[0]) : '✓ 可以保存';
  return hard;
}
function gcPayload(){
  return {
    message_template: $('gc_msg').value || '',
    hitokoto_types: ($('gc_hito').value || '').trim(),
    delay_min: ($('gc_dmin').value || '').trim(),
    delay_max: ($('gc_dmax').value || '').trim()
  };
}
var GC_LOADED = false;
function gcLoad(){
  var g = (STATUS && STATUS.global) || {};
  $('gc_msg').value = g.message_template || '';
  $('gc_hito').value = g.hitokoto_types || '';
  $('gc_dmin').value = (g.delay_min === undefined ? '0' : g.delay_min);
  $('gc_dmax').value = (g.delay_max === undefined ? '0' : g.delay_max);
  gcCheck();
}
Array.prototype.forEach.call(['gc_msg', 'gc_hito', 'gc_dmin', 'gc_dmax'],
  function(id){ var el = $(id); if(el){ el.addEventListener('input', function(){ gcCheck(); }); } });

$('gc_save').onclick = function(){
  var hard = gcCheck();
  if(hard.length){ toast(hard[0], false); return; }
  $('gc_state').textContent = '保存中…';
  $('gc_save').disabled = true;
  post('api/global/save', gcPayload()).then(function(r){
    $('gc_save').disabled = false;
    $('gc_state').textContent = r.message || r.error || '';
    toast(r.message || r.error || '', !!r.ok);
    if(r.ok){ loadStatus(); }
  });
};

$('gp_go').onclick = function(){
  var pick = [];
  if($('gp_msg').checked){ pick.push('消息模板'); }
  if($('gp_hito').checked){ pick.push('一言类型'); }
  if($('gp_delay').checked){ pick.push('发送间隔'); }
  if(!pick.length){ toast('先勾选要推平的项目', false); return; }
  var n = ((STATUS && STATUS.accounts) || []).length;
  if(!n){ toast('还没有抖音号', false); return; }
  if(!window.confirm('把全局默认的「' + pick.join('、') + '」覆盖到全部 ' + n + ' 个抖音号？\n\n'
      + '⚠ 会改掉这些号自己原本的配置，没有一键还原。\n'
      + '（会先把上面这份全局默认保存下来，再推平）')){ return; }
  var hard = gcCheck();
  if(hard.length){ toast(hard[0], false); return; }
  $('gp_state').textContent = '正在保存全局默认…';
  // 先保存全局默认，再推平 —— 否则用户改了输入框没点保存，推下去的会是旧值
  post('api/global/save', gcPayload()).then(function(sv){
    if(!sv || !sv.ok){
      $('gp_state').textContent = (sv && sv.error) || '保存全局默认失败';
      toast((sv && sv.error) || '保存全局默认失败', false);
      return null;
    }
    $('gp_state').textContent = '正在推平到 ' + n + ' 个账号…';
    return post('api/global/apply', {
      template: $('gp_msg').checked, hitokoto: $('gp_hito').checked,
      delay: $('gp_delay').checked
    });
  }).then(function(r){
    if(!r){ return; }
    $('gp_state').textContent = r.message || r.error || '';
    toast(r.message || r.error || '', !!r.ok);
    if(r.ok){ loadStatus(); }
  });
};

$('regAllow').onchange = function(){
  var on = this.checked;
  act('api/register/allow', {allow: on}, on ? '已允许注册' : '已关闭注册');
};
$('cu_btn').onclick = function(){
  var n = ($('cu_name').value || '').trim(), p = $('cu_pw').value;
  if(!n || !p){ $('cu_state').textContent = '登录名和密码都要填'; return; }
  post('api/user/create', {name: n, password: p}).then(function(r){
    $('cu_state').textContent = r.message || r.error || '';
    toast(r.message || r.error || '', !!r.ok);
    if(r.ok){ $('cu_name').value = ''; $('cu_pw').value = ''; loadUsers(true); }
  });
};
$('bd_btn').onclick = function(){
  var uid = ($('bd_uid').value || '').trim(), name = $('bd_user').value;
  if(!uid){ $('bd_state').textContent = '要填抖音号'; return; }
  if(!name){ $('bd_state').textContent = '还没有可以分配的用户'; return; }
  post('api/user/bind', {name: name, unique_id: uid}).then(function(r){
    $('bd_state').textContent = r.message || r.error || '';
    toast(r.message || r.error || '', !!r.ok);
    if(r.ok){ $('bd_uid').value = ''; loadUsers(true); loadStatus(); }
  });
};
if($('sc_generate')){
  $('sc_generate').onclick = function(){
    var btn = this;
    btn.disabled = true;
    post('api/redeem/generate', {days: $('sc_days').value, count: parseInt($('sc_count').value || '1', 10)}).then(function(r){
      $('sc_state').textContent = r.message || r.error || '';
      $('sc_state').style.color = r.ok ? 'var(--ok)' : 'var(--bad)';
      $('sc_output').hidden = !r.ok;
      $('sc_output').textContent = r.ok ? (r.codes || []).join('\n') : '';
      if(r.ok){ loadSubscriptionAdmin(); }
    }).finally(function(){ btn.disabled = false; });
  };
}
if($('sc_grant')){
  $('sc_grant').onclick = function(){
    var name = $('sc_user').value;
    if(!name){ $('sc_grant_state').textContent = '还没有可授予的普通用户'; return; }
    var btn = this;
    btn.disabled = true;
    post('api/user/grant', {name: name, days: $('sc_grant_days').value}).then(function(r){
      $('sc_grant_state').textContent = r.message || r.error || '';
      $('sc_grant_state').style.color = r.ok ? 'var(--ok)' : 'var(--bad)';
      if(r.ok){ loadUsers(true); loadStatus(); }
    }).finally(function(){ btn.disabled = false; });
  };
}
$('ap_btn').onclick = function(){
  var o = $('ap_old').value, n = $('ap_new').value, a = $('ap_again').value;
  if(!o || !n){ $('ap_state').textContent = '现在和新密码都要填'; return; }
  if(n !== a){ $('ap_state').textContent = '两次新密码不一样'; return; }
  post('api/admin/password', {old: o, password: n, again: a}).then(function(r){
    $('ap_state').textContent = r.message || r.error || '';
    toast(r.message || r.error || '', !!r.ok);
    if(r.ok){ $('ap_old').value = ''; $('ap_new').value = ''; $('ap_again').value = ''; }
  });
};

// ---- 轮询：页面在后台就停 ----
var TIMERS = [];
function tick(){
  if(document.hidden){ return; }
  loadStatus();
  if(CURRENT === 'records'){ loadSends(); }
  if(CURRENT === 'logs'){ loadLogs(); }
}
function startPoll(){
  if(TIMERS.length){ return; }
  TIMERS.push(setInterval(tick, 4000));
}
function stopPoll(){
  TIMERS.forEach(function(t){ clearInterval(t); });
  TIMERS = [];
}
document.addEventListener('visibilitychange', function(){
  if(document.hidden){ stopPoll(); } else { tick(); startPoll(); }
});

showPanel((location.hash || '').replace('#',''));
loadStatus();
loadUsers(false);
loadSends();
loadLogs();
startPoll();

// ---- 红黑 / 白红主题切换：保持用户选择 ----
(function(){
  var btn = $('themebtn');
  function paint(){
    var dark = document.documentElement.getAttribute('data-theme') === 'dark';
    var label = btn && btn.querySelector('.theme-label');
    var nextTheme = dark ? '白红' : '红黑';
    var action = dark ? '切换到白红主题' : '切换到红黑主题';
    if(btn){
      btn.title = action;
      btn.setAttribute('aria-label', action);
      if(label){ label.textContent = nextTheme; }
    }
    var meta = document.querySelector('meta[name="theme-color"]');
    if(meta){ meta.setAttribute('content', dark ? '#0b0a0b' : '#fff9f9'); }
  }
  if(btn){
    btn.onclick = function(){
      var next = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
      document.documentElement.setAttribute('data-theme', next);
      try { window.localStorage.setItem('dsh-theme', next); } catch(e){}
      paint();
    };
  }
  paint();
})();

  (function(){
    var menu = $('accountMenu');
    if(!menu){ return; }
    var summary = menu.querySelector('summary');
    function sync(){ if(summary){ summary.setAttribute('aria-expanded', menu.open ? 'true' : 'false'); } }
    menu.addEventListener('toggle', sync);
    document.addEventListener('click', function(event){ if(menu.open && !menu.contains(event.target)){ menu.open = false; } });
    document.addEventListener('keydown', function(event){
      if(event.key === 'Escape' && menu.open){ menu.open = false; if(summary){ summary.focus(); } }
    });
    sync();
  })();

</script>
</body></html>

"""


# 网页标签页上的小图标：品牌渐变圆角方块 + 一道火花
FAVICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
<defs><linearGradient id="g" x1="0" y1="0" x2="64" y2="64" gradientUnits="userSpaceOnUse">
<stop stop-color="#ff9a2e"/><stop offset="1" stop-color="#ff3d2e"/></linearGradient></defs>
<rect width="64" height="64" rx="15" fill="url(#g)"/>
<g transform="translate(12 12) scale(1.6667)"><path fill="#fff" fill-rule="evenodd" d="M12 2.6C14.6 6.8 18.4 9.4 18.4 13.8A6.4 6.4 0 0 1 5.6 13.8C5.6 11.2 7.2 9.4 8.6 7.4C8.9 9 9.7 10.2 10.8 11C10.4 8 10.9 5 12 2.6ZM12 12.4C13.4 14 14.4 15.3 14.4 16.8A2.4 2.4 0 0 1 9.6 16.8C9.6 15.3 10.6 14 12 12.4Z"/></g>
</svg>
"""

APPLE_TOUCH_ICON_PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAALQAAAC0CAIAAACyr5FlAAAb90lEQVR42u2deZQc1X3vP/dWdff07ItGMxLaR6AFGKwFrWhBGGMWgdmeEIhFNkZ2HMA2xsbOCxDMQSc+MTiQxMkJCU843h422M6L7ZjtEGPElmCEkYyQDIIR2qVZe6269/1R1dPToxnNol6qe+r+QwFz7ty59avf7/tbv0L/Uyu9Sx/3kK3nrG/onzb3pzX96/BPO9iGpn8d/mkH21wy2DqZXzbkyvrm/mlzsLnpf53+aX3M4Z/Wxxz+abO3uelfhy8ZQ5kV/zr80x73bPrX4Z92sGfpOVct/36gf1p8zOHrEh9z+Kf1MYd/2pxvbvrX4Z82S5jDf3lj6bTSz1H5p/Uxh39aH3P4p/Uxh39aj2GOXJzYW3ctELJ4TpvzzWVRvbwcS4a2SURBlIISysbmsnTQ/slsLgRWjPopnLORWDfS8JMyaGSRG4IsnVYaxCPM/xQX3UlNM8n4wPpjbBlZH3MAQpCM0zCVhVciJJffRyLqgg8fc4x1zCEk0S6WXkdFPXaSuWuYvZpoB9LwMUep+IGj3FyQjFM/haXXgkZI0Fx+L+FabMs1LmPAgowEc4yd6xAGsW6WXUdZNUohDZRNwxQu+BLdxzKUR6nricExR6kizSGeBVaMhiksvgY0UrrgVCsWXs70BSQiA4CPEtUTx59Wlo6rNorNpUGsm6XXUV6LUikPRaA1oQrWfp1EDCHGbP5vLGMOgRWnfjKLr0GrtIZwlIeymXE2S9YR6UAYPuYYY5hDGsS6WLqB8lq0djVEGosIhOSC2ymrxLayoD98zFE01yEEVpy6KSxeh9YZaqPXv1U2tRO46E6inSeFTIsbc4xBV00YRLtYdh0VdWjVX224dyPRmiVXM+l0Yj2jRKbFfLdyTLpqgkSM+sksuWZgtdH7Y1phhrjqPrR20epYSuvLsVg/Jwzi3Sxz0MYgaiONTC2mL2DVRrqOIM3SyKgNc/OxhzkcJ6VhCkvWo1OxjRMsYaA1qzbSMJVkYjTI1MccRXMdUhLrYul1lNegVUb2dWDhEGhFTROXfIXIMYThY44SxRyiN5OyPpVJGcaSEmVz1ic5fQ3RrhEg09LBHGOhsrI3kxKuQZ0QbfRDpkIQCLH2TrQaO/UuYylx4KiNupTakMNTG33DHpPPZM3N9LQjzLEQOZRjJNiXUhtdabWBYERLSLRmzWepacKKI0TJo3hZksWPg9ZtNExm6YljG0Mh06oGrvgL4j3D2qHIIwJjA3PoXrUxjNjGiZCpgbJZsJb5a91qDx9zFD3mcIrLGyazZN0o1UbfrYAL/oxwFUr5mKP4MYcwiPWwbAPldaNXG2lkanHKbM7flBEz9TFHcapNp25j0lCZlOHLh4FWnLOBSXPdhJzfmlCsalM6CdiTQxv9LYumooar784aYCy+1oQSmEghBMkYDVNG76QMpjyUxezlLFxL9zEMs7gyaj7myAyJZkttZIQ9FBffTk0TyVjGzj7mKIbEQSq2sWwkmZQRCAc0TOLKr5OIlaTfV9KYQ0hi3SzbMMJMyvDDHhJls+hTtJxNtOtkSwn9dsg8XocgmaBhMsuuzb7a6AdO192DGcS20/Lnt0N62qBKg7iTSanOidpIhz0Uk+Zw8W1EOgaY3eC3JnjvOgRWjHHTsuykDGZctGL1DTS1EI+U0uwGWZoz3qVBtJv5aymvRdu5UhvpOmRNKMz6e9N1hCUREShVzKEwQkxudXApuV7SwLaZu4JVG+g+hjR9zOFVzAEoRSDIxDluEVceltPkcvmdNM8kPlT7tY85CncdAivO+BYq6gboc8yVbRFoRbiST24iET1RkbrfDlnI6xAC26K8hrLKPJmVvtUey69m3gVEOtPt135rgpfUpkApJs4eMXDLTthUs/4eyqrSg4GKuDWh9Ga8I7Atmk8l/9LhGJf6iZz/aTfsoYv4bksOc5CyKZPPdD9l8q88FBd+jpkLB26/9jFHwa5Dg1KUVVI9noIsIdAaM8D6e7CTRV2kXnKYQ0oSUSbOpaIWZefJVRkQmU5vZeV6Oo9gBvx2SO+oTUX9pAIYlOPBx5Vf4ZRZ9HRkbSqh3w55shvaNjMXUdjliGZFLXdsoWka8egAfQyl35rgtaSM1gSCBMIUfDkJ2/oJ3PE4ZZUk4ydbipz3uy0xzOG4KnXMWFAYV+X4mLptUT+B6+8lGc+oHPAxR75FTQjsJPWTMINojReWYWLbnH0xmx4i3uPWD/iYoxAj7gXJGONnEAxnuZz4pOTDwLZYvJZbHiTWM3r94bdDnvR1SKaehdeWYWJbLLmUWx5Mj3/x2yELIGrjp+PB5cjH0ktZtY72Q0PPnvNbE4bIvAs5spFOVoLaZibNdcGg15ZTE7TxAVc++rVCFXdrAvnK+vTS8cW7hy7J7PtsW1TUEa7Cm0sIpATNpr9h1dV0HMqYEOS9aR+exByOZATCrPoMyh4B4VIixoyFGAGUnb8yjhG3Mgi0YtO3WXE1Pe0YZv8yNh9znOg6DJNoJ0vXs/YuJs4hER22hhMEQnh8ufKh2Xg/TdOIdLr2xcccQ4uaYdJ1mDPO54Lb0JqPfz5NWjDEJhopmH0O3l9O5jZUxh2PUj+BaFd//eG3Qw5wHUIS76FxOlfeh5SgmHsuM5cQ6xo6dqQVoQqqG13D5PElJUozYTpfe5y6ZiJdWUi+lH47pNJcfg91E92hSsCaW/oP/xswNpqI0zCVU+Z41FUZrNW2eRp3PU5dM4kYUvqM1INFAoJ0HuITtzJ3NcpCGu4MnTkraVnsKo8Tt00nosR7ClAdeJKVH83T+Mb3MAMkrSxQypUgI7U06TrEvEs4b5MrGe7/1QjJeSnlMegmmkCIw3v48G23b6VYliMfTVO5/i+J9YB2i8d8zJGGGskYDVP51P/GCCBk2og4XI2zVzBzEbGhDLOGZJyiW9LAtlh1FbdspqfTDej5mCON3hMxrr6f+kkou3+q3aFxPG/TUIcR2BZ/eJ5iXE5w/dx1fHYzkc5UsMdvh5QB2g9wyVeZvQK7j0Hp5/i1LGbcZJKxgbWdTlX6FAUUPYF8rFnHzQ/Q3ZFxD2O0HVIY9Bxl3iWs2oiyB5CM3nrMUJhl609U7O/AjrY/Yidd1vEilY/zrmH5pRw7NNw5dKXZmoDATlLTzFV/hRlEDN707LzspesYNw0rlqFydZ+tDZODezxUyTFqfPqFb7P8EjqPDa0/SpaR2pnZdf1D1DQPADX6KQ+lKK9m5Qai3QMrGJ0iRklEKd7l8NlKgy8+Qus56eDY2GKkdnzXtXfRsnhQg3L8jINl1zBu6gAs4g7gMEMcaeO93xeZNzvglyAN1n3Z/bvGFuaQBrFOzvgE596MVsNDkc6MgypWXu8yWgx22mSCYl9OZeGpH+OSz9B5NJ2ZK33MIQRWkop61n8rpTDEsJtBNOesZ9xUEvFBZmBo3nqeEliOprz404ybSDIxqGdbWu2QAgSJCDc+QlXDEFBjQH1bVsnKDcS6B24j07oo/ZTB2l7qxrPhLrrbM6J/ulRbE6Qk0s5Fd9CyaFhQY8Dvafk6GqeSiKdVjk79IxBi324SMXdURrEbF6VY8knOWunC8FJmpJYGsS5mreD8P0OpUTUdOcijmlU30JXqUdZ9MrpGkIN7sBJF7M32W4EQN34DZaeJKUuQkVpIknEqGrjhYZcafpRcWhJlc+5NrNlI+/6MMm5SQdJkrEQkw/ljp5/ORTfR3TkwcVgpYA6tsC2u/w4VtSc3b1ogDaTBdQ+wcgMdfSKJWmMGad/Pu6+7v7EUwIdAa678c2obsRKDBACLGnNIg1g3H/88py5B29npYtWKDZtZeR0dx7WB2AlKZjmjgqpqufEbxCJpt79E2iGFQbSLliVc+EWUnR6zl5Uy7utT8tGrP4RRIt5s35i6Vqy4lJYziXTnrWAsL60JTrzyvE1Zdh8c+VCK6zezqo/+0LqIw6ODqkmNEKz/cjYlo/CtCU4C5dQlzD4H9Ih916EzEZn6QxoEQhzcQ6y7FLzZfgm5eatYciGdoyIO8yQjtcCKs+aWXL2qXvtyw2ZWXUf7QYLlHNxDrKd0vNm+McBrv8y4CW7MtLgZqaVBtIOzr2DWCrTKsto43r7csJlV19JxkGAZVoISW072oGkyF9+YUQ1UnJhDoGyCFVz0pZwXaPXalxs2c95NfPjHkvJmM0YF2VxyE3MXudn8YsUcUhLpZOFlNEwZWQ7lJO3Lhm+ycj1H9lKCSyAEwTKu/sJoUvkeaodUNuFqVn/GLbfPZxvq5/+exskoVcQlpSdWHgvPZfUVdBwZQSmhh1oTpEl3OwsupXHaaNMoJ2FfpMHiy5CyCPoiRx0zvepzNDRhJXNHHJYbRmohSCaoa2bNZ/PHeNLvWFpRqktKlGLidK74HB1HMQyKiZEaSayTC25l3BS0KsTIR1H4OZM5XYZE2XxiHWcsJtIzrAmFnsAcSBIxmk9l0ZVonZ1gub+Ol34gXMG1X+xfFulpzCEkiR5W3pia9+i/yNzFTBXzVjB/FZGeExWpe6UdUgiSUSbMYtEVRdl/VlxIxTntGYtIxjParz3aDikMYj0svIxQBdouPmehuFrlHFw1dyGBYDrXmPPWhFFDXGVTVsXpa3JIH5+jnCfQeYjnt7iB3WLxaYFJLTRMyOA7zlIpYdYxhwYoqyjKAEMgxC++zTsvuSnQYgl41NQzucXt1fAu5kCQjDHhNKoaCxTeOLllhghX8d1b2PsO0kAXg3woG61ZuBrbzqjF9xzmEAJlUd1IMJxXQtdsmZX3f09PB8kYP7qbWDdaF0E5iJQIwceWEyp3YYdX2yElVpKZS9LXXTzSAdB+gGgnlQ3seJFfPIg0i8C4OJZl3AQm9mkh9mI7pNYYQeonFmvkwAwgJFaCmvE8v4XnHsPwvnwItCZcQctcEons1mBksTVBYFuEq5k+3xMsSaM2Lu5I0zBPfYu27UUATp0O0NPPxrYH6R/2AiO1VoTKi7g4Ly0cGmkiJQ/fyJG9bvG3l2EHgtnzqKjKgKUeYqR2CF1PmUPVuIIRup68WekbfAyE6DrCj+4mGUMr76IoB3aMP4XGiSQSx/UPewdzNE6neNfOrRky7RB0vPErnv5nr4NTpQhXsGgN8ShSeLId0ra8SKE1qLE8bh3d1/+/2Elqmvj37/DSE0UATucsyIJk5KAdUmBbVDbQ3JIO63o20z0cs9LXvgTL+Olm9u/2LvhwEpyzWqmuJ5nMFsGgzFr3vlKYQRomeZe0QOtU970eApD2+49mgESER24i0uHRRimnpaWmnskziceH67Pki5FaYCdpnIo0vHh3jjn4w3P86N4TBuj0oBY9WM6RNn54N7aFsr2YuXWE+NQz3NkkHmKkdhgtJszyFqFr7xGFwLZ46lu8+jOO7Rv065fm4PJhUVnHS/+Xf3/Io+DDufKFKxES5SnMoTXBMLtfJx7xnOJ1at+//3UO/AkryY4X07qkLzFDpIM9fyAYHvTwtkVtE888yis/Q5ooy4tQ+/cvu4NxPIQ5lHbptxIRb6kNZ+zYf/8HL/6YcDVC8N//MUBjplMu330UaZ5IsrXGMPn+Nzj0PtL0XC+/1hxoQwuvtUM6wnGU9970ULGd1kiD9gM8cT9llSibskreeYkPt7sIrj+vpzmETXYmB6H5u89w+AO3AdMjf6lhEI+w8y3CYZTyWDukU0B1pM1DelYrlM1PH6B9H8Eyt0kiEU+NdlEj8nPTqigY5sButtyZ0pGesaHHjrhzwzzXmuBE0He95i2D8txjbP0JVQ3YFoC2CYV545fEe/pbFq2wksOyibZFZT27XucHf4GQngCnjhbc9TbthzH6WEbtEUZqJ19/dC9xDwzG0AphsO9d/t93qB6HlcwAzh9uZ9frGbWiWhOqoHHKEJWYfeWjvJrnt/Dqzz0RWXeO/P67CGMQvV5YRmpnLv2B97zisAjBD//SnVbb70qMAL/9Qbq924EOZRU0z3Br/If5sVaNY8ud7HjRK2n93dtHEBvNbzukRkjiEZe0oIDC4WCLp/6ana8Qrur/2rQiGGbnKxz9yB3U11cfjEDnpUap/vheuo+6qdGCjQuTdB7jg10EywYdzVDgdkgna999tJAwzZGMD7fzzKOEqwf4oB069J5jbP9t/35r2xqxpS+rpG0Hv/1hfzkrQPjcpuMIhoHSnmyH1BojwJ63C5l7cz7fF3+ElRh0ypTWBMt44d/c5s3eL75m/Ghgb1kFf3jenU6jC/cn79pONIow0u/DY+2QGiE58KcCBzYSUd7ZSrB80N4CrQiWs3cHT252v3jnfmcvH7Fp0IpAmLbt7N9VsLCHc+Z9HxBNjbD1IiO1UgTD7NtN+4HCqFnnN/7pDQ6+RyB0ojftTB369Xf56QPpRPzoQKWUxHrY9mzBjKmjpLe9hhk40Z0XnpFaGkQ66DpaSNC+7RnsYUQslE1NoysfZnD0o20dN+2t50aIZ7O6rCSH9rvhL++OYHAaqXe9CoUYI+x0VbXtcF/2cMIV1Y38+rs88U2EcBkqR6GunNjJ/t0F0JcOCdqRg7y/k1BZxp17bgSDAzuO7nPn3uXfTzn4Pnu2ESwfLnpQFtXj+M0/8bNv0XoedRNGw9UiDeI9bHumAD6805Z35ADxWAbjrhdHMDhFdW8+h3Jq5HW+cdm251yGr+H/amVT3civ/4Ef3k0gNJpPXyvMYNpnyf9M9N89TTQyBMlo4dkhtSYQZv9uXvi+O9csn8MqknG2PYMZHPELVjbltfzux3QexgiMxmcJlvPhDj7ckVefRSkMk717+M8nqaxG2Z5nh3SK6p56MK8Vuc4H1NPB/j9hjvbrL6scvSk0DGJdtG3Pb8WCxkryd3/FsUMEAgOHvzzGSK0xTOIxvvsFkvE8OXhOiLB2PIsvI9bVn9sr1zOfhCAeYeJpzL8QrRFmPgTDtpAGW/6Wl56mpg7LLhJGaqfHpu2P/Pw7LgVm3lKT8y8cpeY4qd9skIwxdyXhqjzNyFM2hsn2N3jiUWrHYVm5YI3MGSO1bVFey2/+lZ2v5SlpKSVaMWM+MxcS687jiEtn/n85qzbkKW/gQKJkgofvdq+7+BipHW/2+/ekiuV1nvKT8y9CWflrnXFsysyzaczXRF6njul7j/DONsKVWR4Vlyd2SK0oK+fDP/KD+/IUHXJezOLLqD8lf9SyQqCSLF83RHFydj2Urc/yxKNU1WHbRcsOadlU1PLs47z+S3eoah5gaaiSloUujVceJMNKUN3IrMX5mEriOGXHDvPwPe6cLa2LmZHaIZD+wf10HMpTDEAIllyBGcrHdywkiQit51NZj8p9N5cz2+LRv2b/XsLZHgJWAEZqrQmEOLqPH29GiAxfPFctxZq5K2magRXPPQOQBMmyK/MRDnY8lBd/wy9/QnW966EUPSO1bVFVz++e5LVfYeTacxEohYBlV9F5GGnm8Gs2g3QcZMY8pp7lJsByjbU7jvH391GWKlUpEUZqZVNezb98lY92ucgg17B0xXrOuYbuwy62z7rlMkw6DnHKLC7/WqpVJJc2xZn3df/tHNpPMDRiDi/vMlI7/ZLSJBHj8bsRMreRD4fMK1TBzY/w6Ycwg0Q6MQJZpivoPMx5G/nqk8w8Oz0hI0fZZtvGNHlyC6/9VzZzKENhCePepU055x3uCz4OfsCBPZy6gLIKF0/lSu1rlGLSXGYvY/8u2rYTrsrCrkaASDtlFaz/Jhd+wc3i5g7Z2DbSQEqeepzHH8YwhzteIRsvzrh3aVPueYcze013v8Grv6KuiUmnuSYmJ5crEBLbom4CCy8hEWPXawg5mqRrr7WSkq7DzFzIxgc5c41blpAjydDKJfBue48HvsyTj2GYgxaX5+bFCf3F1jxZlt5nJy2XiHLOlfyvO6kd7zqBOVIhvV/2jhfZ8hXaDxCuSpU+DDkjUCBTjJPJGIkYl93BJzZhBrEtl5kxRwrDYW77+fd47CG6OqmqRqmsScbwXpxx75KmvEqGG+MzCIZ593947T+pbWTyrByqECFcEzN+GvMu4NhHHNxDtJNkHCExDKTR3xF1+CWFwLaJR4hHQDF+Ghu/zfJ1bh1Cjti1nTiQlHy0hwe+xE/+FTNAWTjrtRrD+YGU5sgD5jj+WZrEoyTjLL+cdV+hrjm3KqTXbdm/m7YdvP0CO7fSdZR4D4EyAiFXSrSNbblNnVX1nDKLMz/O5DnMmI/hlHfn7IS9CuMX/8ZjD9LVSWWNy4uQd8lICUdBJEOnDLkQdB2jcTLr7mTp2oy3mJNoQR+fM9LJvp28+Sw7fsvhD4h2oWxCFVTVc9oSPnYBMxdSWZ+h82TuEIZACNre4x++ydbnqKxOt2jnTan3xxy3t+ZVGgZ8NkwSURIJll/G+q9RO941MblDIQ7U6BVBO8mRj9j+Ap2HOONcxk+nsq5PBa9Kg49cI4z/8xBdHVTVYts5zZsM57mPcORfMnS/yJWg+xiNk1h/F0suThc75S7E6YyUd/rkBhxAmDsB7RU751fs2cU/PsDLz1FZjWHmNNc6/OeUcBTQsvR9NkziURJxVlzOtXdRM67/JeZwDFAv9Y7IIe5Jq6KU3lKKn3+Pxx+mqz1DYeROT48Ac9ze6hXJ6KWlFYKuozRP5+KbaV1B05TMT00UK1+HVmjS7OKd7Wx7hV//hN89TWUVZiBrdaBZwxy3eQBzDODIGCTixHqoqqflLOav4awVNE8rPinppyeASDdvvsyLv+HNl9m/Fykpr0LnO4YxPMxxmzcwx4CDb6UkaRGPYiepqqel1ZWSCdO9LiVpGJs6WE8Xb73KS0/zP1s50IbWhMIEQm4MptAWZBDMcVtr4fXEiQdnS4EQKSmxqKqj5UxXSibOSL8MpZCFlpLjZaK7g22vsvVZ3tzK/jZsRVk5wZCLNrLoj+TgxQl9W6uHJYP+k4MQWCkpqaylpZUF5zJvNROmZTggiD6hcZERnDhJpNmX6iu9f8rL7d28s523XmXrM2x7hf199IRIyYT2nJ4YCHPc6knMMQIpSVJZx8xWFn6cBasZP3lYUxnTr1kP6gf1/mRv3GxIL6bzGNte4eVn2fYqB/aiFGWO7eijJzx+txmY41avYo6RSomVpLqBaXOQkmScmWfRPBVlEwrTuhwpsS3qmwYmVRn+OrwPpRGC999h7/sEg8SivP4CtkIIDuxl7/sAZWHMUFrBFMtX1x9z3NpaXCceWkqcZVmpehFJVS1CYiWZOJ2GZpQiVMb81ZgBknGmnEbzFLQiEMQIEOtBSGIR3n7NrfZ+9y3a3iMQJBFj5zaUBkG0h3gUBAjMgJu5M4P98USRfnVp4Sh2yUg/CKRIPzt1MVqnxgQKEnHspDva2omBKkVFNeFKly40VE77YQwDy+LY4YzorfMQCqedbRf86rRu0Lp49cTxz2ZxyfJQm2vsgaIFQqAFaEJhRHnKVe6lEbKIdoMg0oNSmKZrOGoaQGfW0mlU6t+VSk9P98SHkf1ns5j1xIietYsA1EA/I000mKnhJ0aq3beYPozsb2gWs57I4oapeENpfhheYKQe9YzKHFat+qcd/eZm6cm7f1ofc/inzfmG/x+r1Ez+Ml4dsQAAAABJRU5ErkJggg==")
PWA_ICON_512_PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAgAAAAIACAIAAAB7GkOtAAAXFUlEQVR42u3dWXYcxxWE4WAfrIdYia3Btjxbsq3j40kSKHkFFkWCsyQPmufZ017KG4IfeEBTBNBsoKurMut+8cr/gX07K+7NrOzApaN3L+co67T+XwEAAADQJ7Di/gAAAFATWKkLAAAA1ARW6gIAAAA1gZW6AAAAQE1gpS4AAABQE1ipCwAAANQEVuoCAAAANYGVugAAAEBNYKUuAAAAUBNYqQsAAADUBFbqAgAAACkaBaEuAAAAEFEQ6gIAAAARBaFwAAAAEFEQAAAAAIgoCAAAAACIggAAAABAREEAAAAAIKIgAAAAABBREAAAAACIKAgAAAAARBQEAAAAACIKAgAAAAARBQEAAACAiIIAAAAAQERBAAAAACCiIAAAAAAgCgIAAAAAEQUBAAAAgIiCAAAAAIAoCAAAAACIggAAAACAKAgAAACIKAh1AQAAgIiCUBcAAACIKAiFAwAAgIiCAAAAAEBEQQAAAAAgoiAAAAAAEFEQAAAAABAFAQAAAICIggAAAABAREEAAAAAIKIgAAAAABBREAAAAACIKAgAAAAARBQEAAAAACIKAgAAAAARBQEAAAAAURAAAAAAiCgIAAAAAEQUBAAAAABEQQAAAABAFAQAAAAAREEAAABAREGoCwAAAEQUhLoAAABAREEoHAAAAEQUBAAAAAAiCgIAAAAAEQUBAAAAgIiCAAAAAIAoCACgE+DPgzoAIgpCXQBFgVcHdQBEFIS6AGoBrw7qAIgoCHUBlAZOdgKFAkQUhMIB4uQHAIgoCABgucCDfqBQgIiCUDjAgoFTx/9XB4UCRBSEugAqAlEHQERBqAtg0cCa0/+rXgwAIgpC4QBmf4UCRBQEAJAl3/h87CZAJQERBQEAFJn9H+4BCgWIKAgAYAHAY8d/hQJEFITCAYqf+191JRQQURAKB1gKcN7x/zVJQYCIggAAit35iUIBIgpC4QBZWtLD9psApQZEFAQAYPYHACIKAgBoGbjY+H9yE6CSgIiCAABKzf73e4BKAiIKAgDoC9hm/FdJQERBAACVz/2TXHMlFBBREABAV8Ao479KAiIKAgCovDm45kooIKIgAIBOgKvDbg+ClBoQURAAgB8EAAARBQEANAJcHXb4NlipAREFAQCY/QGAiIIAANoBru7y8s91V0IBEQUBANT7QcD6HqDUgIiCAABmBK7u/u6/UgNEQQAA5Wb/szYBSg2IKAgAYF5glvHfdwGIKAgAoMLs/8gmQKkBEQUBAMwOTD/+Xx98F4CIggAACs3+cTQEiCgIAKAZYK7LP4euhAJEQQAAZWf/kz3AdwGIKAgAYBpgrvHfdwEQBQEAVD/3f7AJ8F0AIgoCAJgMaGf8910AIgoCACh45+fQlVBAREEAABMCTZ3+3xh8WYCIggAACs3+8V0AIgoCAJgWaGr8P2sT4MsCRBQEAODHwABAREEAANsDr7U3/j+yCfBlASIKAgCoNvvfGHwXgIiCAAB2AjQ7/sfRECCiIACA2peCbroSCogoCABgbKDr8d+3CYgoCABg8T8IuOlKKCCiIACA8YC+xv+He4BvExBREACAHwMDABEFAQCcC+jx9P+mK6GAiIIAAErO/jYHgIiCAAC2BPq9/HPLlVBAREEAAFVn/1N7gG8TEFEQAMBjgd5/+uvbBEQUBABQ9tz/liuhgIiCAADOCSxm/L/fA3zdgIiCAADc+QEAIgoCAMjSx//7uj34ugERBQEA1Jr9bQ4AEQUBAGwOLO/yz1mbAF83IKIgAIAKeqQH+LoBEQUBACx+/Pd1AyIKAgAovjm47UooIKIgAIDTgGsFxn9fNyCiIACAmi8G7rgSCogoCADg28C1Gqf/a3qA9QCIKAgAIH4QAABEFASgBlBn/D91E2A9ACIKAgAo2AN83YCIggCUBaqN/9YDIKIgAIDYBPi6AREFAagKVB7/rQdAREEAAGV115VQgCgIQE3A+H9qD7BgABEFATD7x8kPABBREIDFAcb/k5sACwYQURAAs3/BHmA9ACIKAlABMP7H0RAgoiAAZn9KktwbLBhAREEAFg4Y/83+gIiCAJj9af0mwIIBRBQEYDGA8X/zHmDBACIKAmD2j5MfACCiIAA9A8b/DTcBFgwgoiAANgc19bo/HQyIKAjAgoDrxv84GgJEFAQAQBfZBFgwgIiCAHQHGP/N/oCIggAA6AKbAAsGEFEQgB4B4/+WPcCKAkQUBMDsHyc/AEBEQQC4fw294UooIKIgANxfD7CiABEFAegH8AIgjoYAEQUBMPvTSJsAKwoQURAA7l+wB1hRgIiCAHD/OPkBACIKAsD9y98IsuQAoiAADQHeANscACIKAmD2p1H0piuhAFEQAO1BD7CiABEFAeD+cfIDAEQUBID7V9oEWFGAiIIAcP+CPcCKAkQUBKBN4NAVoDgaAkQUBABA4+svgyUHiCgIAPcnSw4QURAA7l96E2DJAURBALh/xR5gyQFEQQDmB7wBjpMfQERBAMz+tPtNgCUHiCgIAPevqb/608GAiIIAcH+yJgGiIADag02AJQcQBQGYFLjhDbDZHxBREAAAzbQJsOQAoiAA3L9iD7DkAKIgANw/Tn4AgIiCAHD/IvqbK6EAURCAWQBvgFvuARZtREGoC8DsX7AHWJMRBaEuAO5fsAdYkwBREADuX7EHWJMAURAA7u8syKKNKAh1AXD/Qvq7Px4JEAUB2DXgClD8YgAQURAAsz+1vQmwaCMKQl0A2kPBHmBNRhSEugC4f8EeYE1GFIS6ALi/syCLNqIgFA6wPXDTG+BO9JZLQREFoS4As78eYNFGFITCAbh/7R5g0UYUhMIBuH/BHmDRRhSEwgG4f8EeYNFGFITCAbYBvAHuV2/7AzKiIBQOYPbXA6zqiIIAALg/WdURBQEAcP+CmwCrOqIgAADuX7AHWNURBQEAbAh4A7ykHmBVRxQEAGBzUFPvuBQkCgIA4P56gFUdURAAAPfXA6zqiIIAALh/wR5gVUcUBABwErjlDfDSe4BlH1EQgBaAa4PZnxp6J+zZjCgIAPenBetdf0AmoiAAbbj/9YH7UxM9wLMZURAAAEVSECCiIAC7AR6Z+q8P3J/m3AR4NiMKAjCL+zf1n3QFqGAP8GxGFARgXve/Ppj9qa0Xwh7eiIIAxLk/LVrvuRQUURCA3QPrD38OB+2BGuoBHt6IggBM4/42B9RUD/DwRhQEYGL3P5x1ELvtDbAe4OGNKAhAM2O12Z8m7gGezYiCAIwInOvw53Dg/jSn3vcHZERBAGZxf7M/tdwDPN0RBQHYtfsfDtyfmusBnu6IggC0YKzcnybuAR7eiIIAnAvY5t7njTl+m/OnfaZHHt6IggDM6f5mf2pnE+DpjigIwPTuf2Pg/jS/PnApSBQEoMK5v/ZA5+oBnu6IggAcZV24/yibAO5PrfUAj39EQQB25/5mf2q2B3j8IwoCMIH735g2rv0VF4HocT3A4x9REIAWpmazPzX1TtjjH1EQNYHrO4vSvDlwf2pIH/oDMhEFAZjE/c3+1EsP4A8RBcH9uT8V7AH8IaIgANn9KRD3p9Z6gMc/oiBqAhOP/9yfIikIIAqimvvfnCqV5WU3QWmMF8L8IaIguL/NAS1bH7kUFFEQce6/c90auD910wP4Q0RBLBVoavzn/tRaD2AgEQXB/SfYBHB/aq0HMJCIguD+Zn8q2AMYSERBxLn/7jcB3J8a1Mf+gIwoiKUC7Yz/O/2YboKSXwxEFASgTfe/NZj9qbNNAAOJKAju78UAmf0BEQUR5/5b6LaMaOpkE8BAIgqia6CX8Z/7U2s9gIFEFAT3n2ATwP3JyU9EQaiL2X974IqLQLS1PnElVBREnPvveBOgDmRzEFEQ6jIu0MX4z/2pu00Ah4koCO4/iu4M3J966gEcJqIguH8XmwMiJz8RBQHobhPA/am1TQADiSiI9oHD/sd/LwbI7B9REApXxP3v7OCHlwdugtJ4+tSVUFEQ3N/sT3oAC4ooiDj3H3sToA5kZIkoCIW7AHA4eNKIJt0EsKCIguD+Y+nuwP3J7B9REApXdPa/60oo9bAJYEERBRHn/rvvAdyf+roRxIIiCmIyYJFH/3dH+sPxboJSHA1FFAT377AH2CRRs/rMldCIguD+u9Q9OXFk9o8oCEBVU7s3KBT1sQlgQREFMRmw+PF/TQ/g/tRaD+BREQXB/SfoAdyf4uQnoiC4f70ecIFKvuQiEM3xNpiJRRREnPtP9U5YocjmIKIglg3UHP8f6PWB+1Nz+tyV0IiCMPvP1wMUisz+EQWxbKD4+H9WD+D+1NomgIlFFITZf4IeoJLUWg/gUREFMTpg/D/ZA7g/xclPREGY/Uvqjc2yIl50E5RmfRvMxERBbAUY/8/bA/RRsjmIKAizf8EeoJI0l75wJTSiIEYFjP/n6gHcn1rrAUxMFITZf4oeoJIUJz8RBbEUwPg/1jthouk3AUxMFAT3n05vqhiZ/SMKIk5+9ID/6wU3QamNt8FcThTERoDxf6weoNHSLPpy4P4RBWFzMGcPUEmKo6GIgugKuGH8H6MHcH9qbRPA5URBmP0n0l/0UTL7RxREV4DxXw+g5W0CuJwoCLP/bD3ARSCasQcwMVEQGwHGf/sAiqOhiIIw+5MeQIvQV66ERhTE4wDjP5HZP6IgzP5EtOxNgCgI7m/8J6rYA8y4oiCIKE5+IgqieF2M/0RFNgHcXxQE9yciURD2RERUSV+7EhpREMZ/Ij1AFITZn4ji5CeiIIrVxfhPZBMgCsLsT0Rm/4iCqFIX4z9R8U2AKAizPxGRKIhK7m/8J6JvBlEQZn8iIlEQRdzf+E9EoiC8DSciEgVR5mPfNP4TUU55DSAKwuxPRGb/iILg/kTE/SMKYiGA8x96WFcuqwGJgjD7kx5AVfWPQRQE9yc9gEgUBPcnIhIFwf3JJoC4f0RBcH/SAygVXgOIgljgx3YFiPQAMvuXjoIg0gPIldCKURBERDnjFEgUBPen6sO+TQCJgrA5IL2BSBTEIoBb3gCTHkDcXxQEEdFJ/XMQBcH9iWwCSBQE9yc9gLh/REFwf9IDiPtHFER3gDfApAdQNn4NIArC7E9EZv+IguD+ZBOgBiQKgvuTHkDcP6IguD/pAbQ4/WsQBcH9ifQAEgXRO+AKEBFx/6JREEQ2AcT9IwqCSA+gbPwaQBQE9yfSA8z+EQXB/Yl9E/ePKIg+gNveAJMuQufXvwdREGZ/Ij2AREFwfyI9gERBcH8iPYD7RxQE9yeiVHgNIAqiM8AbYLIJILN/6SgIIj2AuL8oCCI9gLh/mSgIIj2AsptfA4iC4P7ET8nmIKIgtAfi/poW948oiPmBO64AaQP+z8T9RUEQ3/f/p4vpP4MoCO5Ps7qnsyAy+0cUBPcnIu4fURDc3+GPz0LcP6IgTgKv7Fuxi3LMEZ10YlPWA7rW0/uiIMz+xCt9LrN/REFwfyI9gPtHFAT3pwktkoeS9hBRENzfgOwzEvePKIg1wMveA/NQ/3/aQk/ti4KwOSC26MOSKAjuTwzRR+b+EQXB/Yl76gHcP6IguD/xQeL+oiDaArwH5v4+PmWMN8CiIMz+1IT9LcY69QCzf0RBcH/SCIn7RxQE9+d6EkOJ+0cUhPbA/Ym4f0RBXBi44j0wfVv3/qspUh77BvhIFITZn5p1uoWZph5g9o8oCO7P/dWHtAdRENyfyjmmHsD9IwqC+zNrIu4fURDbA94Dc3+1os315L4oCLM/9eBoi7RLPcDsH1EQ3J+IuH9EQWgPhll1I+4fURDcn3glcf+IguD+rJmI+0cUxBrgwEUgncaHopzjCpAoCLM/RRwQmf0jCoL7U8sDrHmZuH9EQXB/LccnIu4vCoL0ACLtoXYURLwHrmTEegBdWE/s19kcrKpteUj78VnI7F8vCoL76wFE3D+iIKiI+eoBxP1FQZBW5FMQ9y8fBfGS98B6AFE2fQMsCsLsT3oAmf0jCoL7U69u23sP0MO4f0RBcH8ay0PFAZEoCO5PdidEoiBqfOwXvQeu7bD99gDdK7t/A3wkCsKLAdKlyOwfURDcn/QA4v4RBbEYwCkQV+20B+hbO9J390VBmP2JiERBLN79bQKIaJPxXxTEMgE9IE4n1Jn775e1QVEQRESiIGq+DX/BJoDI+C8KouxdKD1gLElWUGfuH1EQ3QF6ABH3FwXhSigZTlU4zv0jCqJUXWwCiIz/oiDq1kUPIOL+oiDUhZxRqG2c/EQURCngjzYBRPXG/yNRENw/eoBBVVW5f0RBOPkhojj5EQVRD7AJIKow/h+JguD+0QPivEI9uX9EQagLEcXJjyiI6oX7g02AoVUllzj+czlREBsBegAR948oiLKAHkDE/SMKAkDOLtQwzv0jCqISYBNA1Pv4z+VEQVwc0AOIuH9EQQAoTjBUL05+IgqiEPB7mwCi3sZ/JiYKYjRADzDGqhv3jygIABHFyU9EQVQCbAKI2h//mVhEQewI0APiNEPFuH9EQcTJDxHFyU9EQdQBfmcTYKRVq/bGfx4VURDTAHoAEfePKIiygB5gsFUl7t8zsKcu8WJgene7clklWH+c+0cURNeATQCnU5N59Z19HnVh4NLRmllM4TYE3ho8hheUrQDr5/6iIOJoiPepAMXRUERB9Ab81kEQB/TZGxv/edQGwGlHQOpyYeBtZ0FxHMT6nfxEFERFwFaAJ/qk3L8f4Ns7AHUZC7AVsA/g/nHfv3XgoQagLuMC7+gB2gDr5/5NA8cNQF12BGgDegD3l/HZKnDp6MplddktoAcUbwOsn/u3Clw6OrisLlMA2kDNHsD9/WXHiIIA/MYFoXoeyv25fxqPgjgQBTEt8K6tQIGtAOvf3voZSERBLA+wFVi8q3J/7t8JcMYOQOEmAGwFFrYb4PtjWT9/yGRREAeiIOYD9IDemwHT5/49AycagLpMD7ynDfTWCfj+jqyfP2TiKIgDURANAHpA+82A6XP/LC8K4kAURDOANtBgJ+D701i/xz+zREEciIJoCdADGukEfJ/7p0IUxIEoiPaA97WBOZoB05/F+j3+mTEK4iVREE0CesBknYDvc/+qwOMagMLNC2gDtGDr9/jPDazUpWngeT8bJu4P2BWwpy6tA8/v5yj5wFaAFmT9nu42gJW69AH82laAuD9gZGBPXboB7vcAWwHq2vo93S0BK3XpDLAVIO4PGAnYU5f+AFsBalBP+KPt/QErdekV+JWtAHF/wFbAnrp0DDzoAR/aDdBMvu/Z7BnYU5clADoBzeL7Hr3OgT11WRTwy+Mn8yOdgHbs+x69/oE9dVkmoBPQTn3fo7cIYE9dFg7oBDS673uyIgoC0Bfw3PHz/LFOQGfryX0PTh1gT13KAToBneX7HpyIglCXIsBzx7PeJzoB3/dcVAT21AWQZ49dQCco6/uei4iCUJfigE5Q0/c9F1WBPXUBnKJnj3/Z/6lO0L+ektMAEAUBuADwi+OZUSfo0fetaoAoCMAIgE7Qne9btABREICRgZ8f+8tnOkHDvm/RAiIKArA7QCdo1vctWkBEQQCmAXSCpnzfmgREFARgeuBna13pc+3hPHraaA+IKAjAUoCfrm0PX9RrD99zag+IKAgAIPnJ2vbwZZ/t4fsu5AAiCgIA2A748dr28NV87eEH7toDIgoCAJgP+NHa9vD1du3hGT+jBUQUBADQJ/DDte3hmyHPSMgBFAVW6gIoDXB/QGFgpS4AAABQE1ipCwAAANQEVuoCAAAANYGVugAAAEBNYKUuAAAAUBNYqQsAAADUBFbqAgAAADWBlboAAABATWClLgAAAFATWKkLAAAARBSEugAAAEBEQSgcAAAARBQEAAAAACIKAgAAAAARBQEAAACAiIIAAAAAQERBAAAAAEAUBAAAAAAaBf4He4gIjiwvVgwAAAAASUVORK5CYII=")

AUTH_CSS = """
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:grid;place-items:center;padding:24px;color:#111827;
  background:
    radial-gradient(900px 420px at 12% -10%, #e7eaff 0%, rgba(231,234,255,0) 62%),
    radial-gradient(720px 380px at 102% -4%, #f4e9ff 0%, rgba(244,233,255,0) 58%),
    #f6f7fc;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,"PingFang SC","Microsoft YaHei",sans-serif}
.box{width:min(404px,100%);background:#fff;border:1px solid #e8ebf5;border-radius:18px;
  padding:28px 26px 22px;box-shadow:0 18px 50px rgba(15,23,42,.09)}
.brand{display:flex;align-items:center;gap:11px;margin-bottom:18px}
.brand .logo{width:34px;height:34px;border-radius:11px;flex:none;
  background:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cpath fill='%23fff' fill-rule='evenodd' d='M12 2.6C14.6 6.8 18.4 9.4 18.4 13.8A6.4 6.4 0 0 1 5.6 13.8C5.6 11.2 7.2 9.4 8.6 7.4C8.9 9 9.7 10.2 10.8 11C10.4 8 10.9 5 12 2.6ZM12 12.4C13.4 14 14.4 15.3 14.4 16.8A2.4 2.4 0 0 1 9.6 16.8C9.6 15.3 10.6 14 12 12.4Z'/%3E%3C/svg%3E") center/58% no-repeat,linear-gradient(135deg,#fb4857,#b7192d);
  box-shadow:0 6px 16px rgba(255,110,40,.35)}
.brand b{display:block;font-size:15px;letter-spacing:.2px}
.brand i{display:block;font-style:normal;font-size:11.5px;color:#8b93a7;letter-spacing:.4px}
h2{margin:0 0 6px;font-size:20px;letter-spacing:.2px}
p.tip{color:#6b7280;font-size:13px;margin:0;line-height:1.7}
label{display:block;font-size:13px;font-weight:600;color:#374151;margin-top:14px}
input{width:100%;padding:11px 12px;margin-top:7px;border:1px solid #cfd6e4;border-radius:10px;font:inherit;
  background:#fff;color:inherit;transition:border-color .15s,box-shadow .15s}
input:focus{outline:none;border-color:#5b6cff;box-shadow:0 0 0 3px rgba(91,108,255,.18)}
button{width:100%;margin-top:18px;padding:11px;border:0;border-radius:10px;font:inherit;font-weight:600;color:#fff;
  cursor:pointer;background:linear-gradient(135deg,#5b6cff,#7a5cf0);box-shadow:0 8px 20px rgba(91,108,255,.28)}
button:hover{filter:brightness(1.06)}
.error{margin-top:13px;color:#b3261e;font-size:13px;background:#fdeceb;border:1px solid #f2bcb8;
  border-radius:10px;padding:9px 11px}
.foot{margin-top:16px;padding-top:14px;border-top:1px solid #eef1f7;font-size:12.5px;color:#8b93a7;text-align:center}
.foot a{color:#5b6cff;text-decoration:none;font-weight:600}
.foot a:hover{text-decoration:underline}

/* ---- 深色（登录/注册页）：跟随系统或用户在控制台里的选择 ---- */
html[data-theme="dark"] body{color:#e8ecf7;
  background:radial-gradient(900px 420px at 12% -10%, #1a2246 0%, rgba(26,34,70,0) 62%),
    radial-gradient(720px 380px at 102% -4%, #2a1c46 0%, rgba(42,28,70,0) 58%), #0b1220}
html[data-theme="dark"] .box{background:#141d33;border-color:#26314b;box-shadow:0 18px 50px rgba(0,0,0,.45)}
html[data-theme="dark"] h2{color:#fff}
html[data-theme="dark"] p.tip{color:#8c98b6}
html[data-theme="dark"] label{color:#c5cee2}
html[data-theme="dark"] input{background:#0f1830;color:#e8ecf7;border-color:#33415f}
html[data-theme="dark"] .error{background:#2d1517;border-color:#5e2a2d;color:#fca5a5}
html[data-theme="dark"] .foot{border-top-color:#26314b;color:#8c98b6}
html[data-theme="dark"] .foot a{color:#9aa8ff}

/* ---- 密码框：右边挂一个「看一眼」按钮 ---- */
.pw{position:relative;display:block}
.pw input{padding-right:44px}
.pw .eye{position:absolute;right:7px;top:8px;width:30px;height:30px;margin:0;padding:0;border:0;
  border-radius:8px;background:transparent;box-shadow:none;cursor:pointer;color:#e2761f;
  display:grid;place-items:center}
.pw .eye:hover{background:#f1f3fa;filter:none}
.pw .eye svg{width:18px;height:18px;display:block}
html[data-theme="dark"] .pw .eye{color:#ffa85c}
html[data-theme="dark"] .pw .eye:hover{background:#1c2742}

/* ---- 「记住账号」那一行 ---- */
.keep{display:flex;align-items:center;gap:7px;margin-top:12px;font-size:12.5px;font-weight:400;color:#6b7280}
.keep input{width:auto;margin:0;padding:0;accent-color:#5b6cff}
html[data-theme="dark"] .keep{color:#8c98b6}

/* ---- 提交按钮的「正在登录」状态：点了就先禁掉，免得连点两次 ---- */
button[disabled]{opacity:.72;cursor:progress;filter:none}
.hint{margin-top:10px;font-size:12.5px;color:#8b93a7;text-align:center}
.hint.bad{color:#b3261e}
html[data-theme="dark"] .hint{color:#8c98b6}
html[data-theme="dark"] .hint.bad{color:#fca5a5}

/* 登录页与控制台使用同一套蓝色品牌语言 */
body{background:#f3f6fa}
.box{border-color:#dfe6ef;border-radius:14px;box-shadow:0 20px 55px rgba(31,49,79,.11)}
.brand .logo{box-shadow:0 6px 15px rgba(249,115,22,.22)}
input:focus{border-color:#2563eb;box-shadow:0 0 0 3px rgba(37,99,235,.14)}
form>button:not(.eye){background:#2563eb;box-shadow:0 8px 18px rgba(37,99,235,.2)}
form>button:not(.eye):hover{background:#1d4ed8;filter:none}
.foot a{color:#2563eb}
html[data-theme="dark"] body{background:#0f1929}
html[data-theme="dark"] .box{border-color:#293a56}
html[data-theme="dark"] form>button:not(.eye){background:#4d83e8}
html[data-theme="dark"] .foot a{color:#8bb4ff}

.auth-theme{position:fixed;top:calc(12px + env(safe-area-inset-top));right:max(12px,env(safe-area-inset-right));z-index:20;width:auto;margin:0;min-height:40px;padding:8px 13px;border:1px solid #efb5bc;border-radius:999px;background:#fff;color:#a91d2d;font:inherit;font-size:13px;font-weight:600;box-shadow:0 6px 18px rgba(120,20,34,.12);cursor:pointer}
.auth-theme:hover{background:#fff0f2}
html[data-theme="dark"] body{color:#f5f2f3;background:radial-gradient(900px 420px at 12% -10%,#35151b 0,transparent 62%),#0b0a0b}
html[data-theme="dark"] .box{background:#151214;border-color:#393336;box-shadow:0 20px 55px rgba(0,0,0,.42)}
html[data-theme="dark"] h2{color:#fff}
html[data-theme="dark"] p.tip,html[data-theme="dark"] .foot{color:#a49a9d}
html[data-theme="dark"] label{color:#ded6d8}
html[data-theme="dark"] input{background:#191516;color:#f5f2f3;border-color:#514448}
html[data-theme="dark"] input:focus{border-color:#f04452;box-shadow:0 0 0 3px rgba(240,68,82,.18)}
html[data-theme="dark"] form>button:not(.eye){background:#d92e3e;box-shadow:0 8px 18px rgba(190,25,44,.22)}
html[data-theme="dark"] form>button:not(.eye):hover{background:#b91f31}
html[data-theme="dark"] .foot a{color:#ff8790}
html[data-theme="dark"] .auth-theme{background:#1a1517;color:#ffd7db;border-color:#573038}
html[data-theme="dark"] .auth-theme:hover{background:#311519}
html[data-theme="light"] body{color:#241b1d;background:radial-gradient(900px 420px at 12% -10%,#fff0f1 0,transparent 62%),#fff9f9}
html[data-theme="light"] .box{background:#fff;border-color:#eadcdf;box-shadow:0 20px 55px rgba(80,20,30,.12)}
html[data-theme="light"] h2{color:#241b1d}
html[data-theme="light"] p.tip,html[data-theme="light"] .foot{color:#806f72}
html[data-theme="light"] label{color:#57474a}
html[data-theme="light"] input{background:#fff;color:#241b1d;border-color:#eadcdf}
html[data-theme="light"] input:focus{border-color:#ca2638;box-shadow:0 0 0 3px rgba(202,38,56,.14)}
html[data-theme="light"] form>button:not(.eye){background:#ca2638;box-shadow:0 8px 18px rgba(190,25,44,.18)}
html[data-theme="light"] form>button:not(.eye):hover{background:#a91d2d}
html[data-theme="light"] .foot a{color:#a91d2d}
@media(max-width:480px){.auth-theme{top:calc(10px + env(safe-area-inset-top));right:10px}.box{width:calc(100% - 28px);margin:58px auto 20px}}
/* Auth pages share the same quiet red/black language as the console. */
html[data-theme="dark"]{color-scheme:dark;--auth-bg:#100d0f;--auth-card:#191416;--auth-ink:#f5edef;--auth-muted:#a58f95;--auth-line:#3b2b30;--auth-red:#e84d5b;--auth-soft:#2d191e}
html[data-theme="light"]{color-scheme:light;--auth-bg:#fff8f8;--auth-card:#fff;--auth-ink:#2b1a1e;--auth-muted:#806a70;--auth-line:#e8d8db;--auth-red:#bd263c;--auth-soft:#fbe9ec}
html[data-theme] body{font-family:Inter,"Aptos","PingFang SC","Microsoft YaHei","Noto Sans CJK SC",sans-serif;color:var(--auth-ink);background:var(--auth-bg)}
html[data-theme] .box{position:relative;overflow:hidden;width:min(430px,calc(100% - 32px));padding:32px 30px 24px;border:1px solid var(--auth-line);border-radius:15px;background:var(--auth-card);box-shadow:0 22px 56px rgba(25,8,12,.18)}
html[data-theme] .box::before{content:"";position:absolute;inset:0 0 auto;height:4px;background:var(--auth-red)}
html[data-theme] .brand{gap:12px;margin-bottom:24px}
html[data-theme] .brand .logo{width:42px;height:42px;border-radius:12px;box-shadow:none}
html[data-theme] .brand b{font-size:15px;letter-spacing:.1px;color:var(--auth-ink)}
html[data-theme] .brand i{margin-top:3px;color:var(--auth-muted);font-size:11px;letter-spacing:.1px}
html[data-theme] h2{margin:0 0 7px;color:var(--auth-ink);font-size:24px;letter-spacing:-.6px}
html[data-theme] p.tip{margin:0 0 20px;color:var(--auth-muted);font-size:13px;line-height:1.65}
html[data-theme] label{margin-top:14px;color:var(--auth-ink);font-size:12.5px}
html[data-theme] input{min-height:44px;margin-top:7px;padding:10px 12px;border:1px solid var(--auth-line);border-radius:8px;background:var(--auth-card);color:var(--auth-ink)}
html[data-theme] input:focus{outline:3px solid rgba(232,77,91,.2);border-color:var(--auth-red);box-shadow:none}
html[data-theme] form>button:not(.eye){min-height:46px;margin-top:20px;border-radius:8px;background:var(--auth-red);box-shadow:none;font-weight:700}
html[data-theme] form>button:not(.eye):hover{background:#9f2034;filter:none}
html[data-theme] .error{border-color:var(--auth-line);border-radius:8px}
html[data-theme] .foot{margin-top:20px;padding-top:15px;border-top:1px solid var(--auth-line);color:var(--auth-muted)}
html[data-theme] .foot a{color:var(--auth-red)}
html[data-theme] .auth-theme{min-height:38px;padding:7px 12px;border:1px solid var(--auth-line);border-radius:8px;background:var(--auth-card);color:var(--auth-ink);box-shadow:none}
html[data-theme] .auth-theme:hover{border-color:var(--auth-red);background:var(--auth-soft);color:var(--auth-red)}
html[data-theme] :focus-visible{outline:3px solid rgba(232,77,91,.48);outline-offset:2px}
@media(max-width:480px){html[data-theme] .box{width:calc(100% - 28px);margin:58px auto 20px;padding:28px 21px 21px}}
@media(prefers-reduced-motion:reduce){html[data-theme] *,html[data-theme] *::before,html[data-theme] *::after{scroll-behavior:auto!important;transition:none!important}}

/* White, monochrome auth pages to match the redesigned console. */
html[data-theme]{color-scheme:light;--auth-ink:#171717;--auth-muted:#737373;--auth-line:#e5e5e5;--auth-surface:#fff;--auth-soft:#f5f5f5}
html[data-theme="dark"],html[data-theme="light"]{color-scheme:light}
html[data-theme="dark"] body,html[data-theme="light"] body{min-height:100svh;padding:max(20px,env(safe-area-inset-top)) max(20px,env(safe-area-inset-right)) max(20px,env(safe-area-inset-bottom)) max(20px,env(safe-area-inset-left));color:#171717;background:#fff}
html[data-theme="dark"] .box,html[data-theme="light"] .box{width:min(420px,100%);padding:30px 28px 24px;border:1px solid #e5e5e5;border-radius:12px;background:#fff;box-shadow:0 12px 36px rgba(0,0,0,.06)}
html[data-theme="dark"] .box::before,html[data-theme="light"] .box::before{height:0;background:transparent}
html[data-theme="dark"] .brand .logo,html[data-theme="light"] .brand .logo{filter:grayscale(1);box-shadow:none}
html[data-theme="dark"] .brand b,html[data-theme="light"] .brand b,html[data-theme="dark"] h2,html[data-theme="light"] h2{color:#171717}
html[data-theme="dark"] .brand i,html[data-theme="light"] .brand i,html[data-theme="dark"] p.tip,html[data-theme="light"] p.tip,html[data-theme="dark"] .foot,html[data-theme="light"] .foot{color:#737373}
html[data-theme="dark"] label,html[data-theme="light"] label{color:#404040}
html[data-theme="dark"] input,html[data-theme="light"] input{min-height:44px;background:#fff;color:#171717;border-color:#dedede}
html[data-theme="dark"] input:focus,html[data-theme="light"] input:focus{outline:3px solid #e8e8e8;outline-offset:1px;border-color:#888;box-shadow:none}
html[data-theme="dark"] form>button:not(.eye),html[data-theme="light"] form>button:not(.eye){min-height:46px;border:1px solid #171717;border-radius:8px;background:#171717;color:#fff;box-shadow:none}
html[data-theme="dark"] form>button:not(.eye):hover,html[data-theme="light"] form>button:not(.eye):hover{background:#333;border-color:#333}
html[data-theme="dark"] .foot,html[data-theme="light"] .foot{border-top-color:#ededed}
html[data-theme="dark"] .foot a,html[data-theme="light"] .foot a{color:#262626}
html[data-theme="dark"] .badge,html[data-theme="light"] .badge{border-color:#e5e5e5;border-radius:7px;background:#f7f7f7;color:#404040}
html[data-theme="dark"] .error,html[data-theme="light"] .error{border-color:#f0c4c7;border-radius:8px}
html[data-theme="dark"] .auth-theme,html[data-theme="light"] .auth-theme{display:none!important}
html[data-theme="dark"] :focus-visible,html[data-theme="light"] :focus-visible{outline:3px solid #777;outline-offset:3px}
@media(max-width:480px){html[data-theme="dark"] .box,html[data-theme="light"] .box{width:100%;padding:26px 21px 21px}}
@media(prefers-reduced-motion:reduce){html[data-theme] *,html[data-theme] *::before,html[data-theme] *::after{scroll-behavior:auto!important;animation-duration:.01ms!important;animation-iteration-count:1!important;transition-duration:.01ms!important}}

html[data-theme="dark"] h1,html[data-theme="light"] h1{margin:0 0 7px;color:#171717;font-size:24px;letter-spacing:-.6px;text-wrap:balance}
.skip-link{position:fixed;top:8px;left:8px;z-index:500;transform:translateY(-160%);padding:9px 12px;border:1px solid #171717;border-radius:7px;background:#171717;color:#fff;text-decoration:none}
.skip-link:focus{transform:translateY(0)}

html[data-theme="dark"] .pw .eye,html[data-theme="light"] .pw .eye{color:#525252}
html[data-theme="dark"] .pw .eye:hover,html[data-theme="light"] .pw .eye:hover{background:#f5f5f5;color:#171717}


"""

ADMIN_LOGIN_CSS = """
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:grid;place-items:center;color:#e5e7eb;
  background:radial-gradient(1100px 560px at 18% -12%,#26325a 0%,#101a30 52%,#0a1020 100%);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,"PingFang SC","Microsoft YaHei",sans-serif}
.box{width:min(400px,calc(100% - 40px));background:rgba(23,32,54,.94);border:1px solid #28324e;border-radius:16px;
  padding:26px 24px;box-shadow:0 26px 70px rgba(3,7,18,.6)}
.brand{display:flex;align-items:center;gap:11px;margin-bottom:14px}
.brand .logo{width:30px;height:30px;border-radius:9px;flex:none;background:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cpath fill='%23fff' fill-rule='evenodd' d='M12 2.6C14.6 6.8 18.4 9.4 18.4 13.8A6.4 6.4 0 0 1 5.6 13.8C5.6 11.2 7.2 9.4 8.6 7.4C8.9 9 9.7 10.2 10.8 11C10.4 8 10.9 5 12 2.6ZM12 12.4C13.4 14 14.4 15.3 14.4 16.8A2.4 2.4 0 0 1 9.6 16.8C9.6 15.3 10.6 14 12 12.4Z'/%3E%3C/svg%3E") center/58% no-repeat,linear-gradient(135deg,#fb4857,#b7192d)}
.brand b{display:block;font-size:15px;color:#fff}
.brand i{display:block;font-style:normal;font-size:11px;color:#8fa0c4;letter-spacing:1.4px}
.badge{display:inline-flex;align-items:center;gap:6px;font-size:11.5px;color:#c7d2fe;background:rgba(91,108,255,.16);
  border:1px solid rgba(91,108,255,.45);border-radius:99px;padding:3px 10px;margin-bottom:12px}
h2{margin:0 0 6px;font-size:19px;color:#fff}
p.tip{color:#94a3c2;font-size:13px;margin:0;line-height:1.7}
label{display:block;font-size:13px;color:#b9c4dc;margin-top:14px}
input{width:100%;padding:11px 12px;margin-top:7px;border-radius:9px;border:1px solid #33415f;background:#0f1830;
  color:#eef2ff;font:inherit}
input:focus{outline:2px solid rgba(91,108,255,.55);outline-offset:1px;border-color:#5b6cff}
button{width:100%;margin-top:18px;padding:11px;border:0;border-radius:9px;cursor:pointer;font:inherit;font-weight:600;
  color:#fff;background:linear-gradient(135deg,#5b6cff,#7a5cf0)}
button:hover{filter:brightness(1.09)}
.error{margin-top:13px;color:#fca5a5;font-size:13px;background:rgba(220,38,38,.13);
  border:1px solid rgba(220,38,38,.36);border-radius:9px;padding:9px 11px}
.foot{margin-top:18px;padding-top:14px;border-top:1px solid #26314e;font-size:12px;color:#7f8db0;text-align:center}
.foot a{color:#a5b4fc;text-decoration:none}
.foot a:hover{text-decoration:underline}

/* ---- 密码框「看一眼」按钮（和普通登录页同一套）---- */
.pw{position:relative;display:block}
.pw input{padding-right:44px}
.pw .eye{position:absolute;right:7px;top:8px;width:30px;height:30px;margin:0;padding:0;border:0;
  border-radius:8px;background:transparent;box-shadow:none;cursor:pointer;color:#ffa85c;
  display:grid;place-items:center}
.pw .eye:hover{background:rgba(255,138,43,.16);filter:none}
.pw .eye svg{width:18px;height:18px;display:block}
button[disabled]{opacity:.72;cursor:progress;filter:none}
/* ---- 火花图标：一套遮罩 + 暖色渐变，导航/主题/徽标共用 ---- */
.ni{display:inline-block;flex:none;width:16px;height:16px;vertical-align:-3px;
  background:linear-gradient(160deg,#ffd27a,#ff7a2f);
  -webkit-mask-repeat:no-repeat;mask-repeat:no-repeat;
  -webkit-mask-position:center;mask-position:center;
  -webkit-mask-size:contain;mask-size:contain}
.nav .ni{opacity:.92}
.nav:hover .ni,.nav.on .ni{opacity:1}
.nav.on .ni{background:linear-gradient(160deg,#fff2cd,#ffbb63)}
#themebtn .ni{width:15px;height:15px}
html[data-theme="dark"] #themebtn .ni{background:linear-gradient(160deg,#9a8b78,#5c5348)}
.badge .ni{width:11px;height:11px;vertical-align:-1px}
.ni-flame{-webkit-mask-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cpath fill='%23000' fill-rule='evenodd' d='M12 2.6C14.6 6.8 18.4 9.4 18.4 13.8A6.4 6.4 0 0 1 5.6 13.8C5.6 11.2 7.2 9.4 8.6 7.4C8.9 9 9.7 10.2 10.8 11C10.4 8 10.9 5 12 2.6ZM12 12.4C13.4 14 14.4 15.3 14.4 16.8A2.4 2.4 0 0 1 9.6 16.8C9.6 15.3 10.6 14 12 12.4Z'/%3E%3C/svg%3E");mask-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cpath fill='%23000' fill-rule='evenodd' d='M12 2.6C14.6 6.8 18.4 9.4 18.4 13.8A6.4 6.4 0 0 1 5.6 13.8C5.6 11.2 7.2 9.4 8.6 7.4C8.9 9 9.7 10.2 10.8 11C10.4 8 10.9 5 12 2.6ZM12 12.4C13.4 14 14.4 15.3 14.4 16.8A2.4 2.4 0 0 1 9.6 16.8C9.6 15.3 10.6 14 12 12.4Z'/%3E%3C/svg%3E")}
.ni-accounts{-webkit-mask-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cg fill='none' stroke='%23000' stroke-width='1.9' stroke-linecap='round' stroke-linejoin='round'%3E%3Ccircle cx='12' cy='8.2' r='3.6'/%3E%3Cpath d='M5 20.2c0-3.6 3.1-6.4 7-6.4s7 2.8 7 6.4'/%3E%3C/g%3E%3C/svg%3E");mask-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cg fill='none' stroke='%23000' stroke-width='1.9' stroke-linecap='round' stroke-linejoin='round'%3E%3Ccircle cx='12' cy='8.2' r='3.6'/%3E%3Cpath d='M5 20.2c0-3.6 3.1-6.4 7-6.4s7 2.8 7 6.4'/%3E%3C/g%3E%3C/svg%3E")}
.ni-admin{-webkit-mask-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cg fill='none' stroke='%23000' stroke-width='1.9' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M12 2.9 4.8 5.8v5.5c0 4.5 3 8.3 7.2 9.7 4.2-1.4 7.2-5.2 7.2-9.7V5.8z'/%3E%3Cpath d='m9.2 11.9 2.2 2.2 4.2-4.4'/%3E%3C/g%3E%3C/svg%3E");mask-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cg fill='none' stroke='%23000' stroke-width='1.9' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M12 2.9 4.8 5.8v5.5c0 4.5 3 8.3 7.2 9.7 4.2-1.4 7.2-5.2 7.2-9.7V5.8z'/%3E%3Cpath d='m9.2 11.9 2.2 2.2 4.2-4.4'/%3E%3C/g%3E%3C/svg%3E")}
.ni-logs{-webkit-mask-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cg fill='none' stroke='%23000' stroke-width='1.9' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M6.4 3h7.2l4.8 4.8V21H6.4z'/%3E%3Cpath d='M13.6 3v4.8h4.8'/%3E%3Cpath d='M9.2 12.4h5.6M9.2 16h4'/%3E%3C/g%3E%3C/svg%3E");mask-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cg fill='none' stroke='%23000' stroke-width='1.9' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M6.4 3h7.2l4.8 4.8V21H6.4z'/%3E%3Cpath d='M13.6 3v4.8h4.8'/%3E%3Cpath d='M9.2 12.4h5.6M9.2 16h4'/%3E%3C/g%3E%3C/svg%3E")}
.ni-me{-webkit-mask-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cg fill='none' stroke='%23000' stroke-width='1.9' stroke-linecap='round' stroke-linejoin='round'%3E%3Crect x='3' y='4.6' width='18' height='14.8' rx='2.6'/%3E%3Ccircle cx='8.8' cy='11' r='2.5'/%3E%3Cpath d='M5.4 16.8c.6-1.7 1.9-2.6 3.4-2.6s2.8.9 3.4 2.6'/%3E%3Cpath d='M15.4 10.2h3.4M15.4 13.8h3.4'/%3E%3C/g%3E%3C/svg%3E");mask-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cg fill='none' stroke='%23000' stroke-width='1.9' stroke-linecap='round' stroke-linejoin='round'%3E%3Crect x='3' y='4.6' width='18' height='14.8' rx='2.6'/%3E%3Ccircle cx='8.8' cy='11' r='2.5'/%3E%3Cpath d='M5.4 16.8c.6-1.7 1.9-2.6 3.4-2.6s2.8.9 3.4 2.6'/%3E%3Cpath d='M15.4 10.2h3.4M15.4 13.8h3.4'/%3E%3C/g%3E%3C/svg%3E")}
.ni-overview{-webkit-mask-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cg fill='none' stroke='%23000' stroke-width='1.9' stroke-linecap='round' stroke-linejoin='round'%3E%3Crect x='3.4' y='3.4' width='7.4' height='7.4' rx='1.7'/%3E%3Crect x='13.2' y='3.4' width='7.4' height='7.4' rx='1.7'/%3E%3Crect x='3.4' y='13.2' width='7.4' height='7.4' rx='1.7'/%3E%3Crect x='13.2' y='13.2' width='7.4' height='7.4' rx='1.7'/%3E%3C/g%3E%3C/svg%3E");mask-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cg fill='none' stroke='%23000' stroke-width='1.9' stroke-linecap='round' stroke-linejoin='round'%3E%3Crect x='3.4' y='3.4' width='7.4' height='7.4' rx='1.7'/%3E%3Crect x='13.2' y='3.4' width='7.4' height='7.4' rx='1.7'/%3E%3Crect x='3.4' y='13.2' width='7.4' height='7.4' rx='1.7'/%3E%3Crect x='13.2' y='13.2' width='7.4' height='7.4' rx='1.7'/%3E%3C/g%3E%3C/svg%3E")}
.ni-records{-webkit-mask-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cg fill='none' stroke='%23000' stroke-width='1.9' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M21.2 2.8 3 9.9l7.1 3 3 7.1z'/%3E%3Cpath d='M21.2 2.8 10.1 12.9'/%3E%3C/g%3E%3C/svg%3E");mask-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cg fill='none' stroke='%23000' stroke-width='1.9' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M21.2 2.8 3 9.9l7.1 3 3 7.1z'/%3E%3Cpath d='M21.2 2.8 10.1 12.9'/%3E%3C/g%3E%3C/svg%3E")}
.ni-system{-webkit-mask-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cg fill='none' stroke='%23000' stroke-width='1.9' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M4 7h10M18 7h2M4 17h2M10 17h10'/%3E%3Ccircle cx='16' cy='7' r='2.3'/%3E%3Ccircle cx='8' cy='17' r='2.3'/%3E%3C/g%3E%3C/svg%3E");mask-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cg fill='none' stroke='%23000' stroke-width='1.9' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M4 7h10M18 7h2M4 17h2M10 17h10'/%3E%3Ccircle cx='16' cy='7' r='2.3'/%3E%3Ccircle cx='8' cy='17' r='2.3'/%3E%3C/g%3E%3C/svg%3E")}
.ni-users{-webkit-mask-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cg fill='none' stroke='%23000' stroke-width='1.9' stroke-linecap='round' stroke-linejoin='round'%3E%3Ccircle cx='9' cy='8' r='3.4'/%3E%3Cpath d='M2.8 20.2c0-3.5 2.8-6.1 6.2-6.1s6.2 2.6 6.2 6.1'/%3E%3Cpath d='M16.6 5.4a3.4 3.4 0 0 1 0 6.5'/%3E%3Cpath d='M17.6 20.2c0-2.2-.6-3.9-1.7-5.1'/%3E%3C/g%3E%3C/svg%3E");mask-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cg fill='none' stroke='%23000' stroke-width='1.9' stroke-linecap='round' stroke-linejoin='round'%3E%3Ccircle cx='9' cy='8' r='3.4'/%3E%3Cpath d='M2.8 20.2c0-3.5 2.8-6.1 6.2-6.1s6.2 2.6 6.2 6.1'/%3E%3Cpath d='M16.6 5.4a3.4 3.4 0 0 1 0 6.5'/%3E%3Cpath d='M17.6 20.2c0-2.2-.6-3.9-1.7-5.1'/%3E%3C/g%3E%3C/svg%3E")}

.auth-theme{position:fixed;top:calc(12px + env(safe-area-inset-top));right:max(12px,env(safe-area-inset-right));z-index:20;width:auto;margin:0;min-height:40px;padding:8px 13px;border:1px solid #573038;border-radius:999px;background:#1a1517;color:#ffd7db;font:inherit;font-size:13px;font-weight:600;cursor:pointer}
html[data-theme="dark"] body{background:radial-gradient(900px 500px at 15% -12%,#35151b 0,#171013 46%,#0b0a0b 100%);color:#f5f2f3}
html[data-theme="dark"] .box{background:rgba(21,18,20,.96);border-color:#393336}
html[data-theme="dark"] .badge{color:#ffc1c7;background:rgba(240,68,82,.14);border-color:rgba(240,68,82,.38)}
html[data-theme="dark"] input{background:#191516;color:#f5f2f3;border-color:#514448}
html[data-theme="dark"] input:focus{outline-color:rgba(240,68,82,.5);border-color:#f04452}
html[data-theme="dark"] button:not(.eye){background:linear-gradient(135deg,#f04452,#b7192d)}
html[data-theme="dark"] .foot a{color:#ff8790}
html[data-theme="light"] body{background:radial-gradient(900px 500px at 15% -12%,#fff0f1 0,#fff6f6 46%,#fff 100%);color:#241b1d}
html[data-theme="light"] .box{background:#fff;border-color:#eadcdf;box-shadow:0 24px 64px rgba(80,20,30,.15)}
html[data-theme="light"] .brand i,html[data-theme="light"] p.tip,html[data-theme="light"] .foot{color:#806f72}
html[data-theme="light"] .badge{color:#a91d2d;background:#fff0f2;border-color:#efb5bc}
html[data-theme="light"] h2{color:#241b1d}
html[data-theme="light"] label{color:#57474a}
html[data-theme="light"] input{background:#fff;color:#241b1d;border-color:#eadcdf}
html[data-theme="light"] input:focus{outline-color:rgba(202,38,56,.25);border-color:#ca2638}
html[data-theme="light"] button:not(.eye){background:linear-gradient(135deg,#ca2638,#a91d2d)}
html[data-theme="light"] .foot{border-top-color:#eadcdf}
html[data-theme="light"] .foot a{color:#a91d2d}
html[data-theme="light"] .auth-theme{background:#fff;color:#a91d2d;border-color:#efb5bc}
@media(max-width:480px){.auth-theme{top:calc(10px + env(safe-area-inset-top));right:10px}.box{width:calc(100% - 28px);margin:58px auto 20px}}
/* Admin sign-in uses the same red/black identity as the rest of the console. */
html[data-theme="dark"]{color-scheme:dark;--auth-bg:#100d0f;--auth-card:#191416;--auth-ink:#f5edef;--auth-muted:#a58f95;--auth-line:#3b2b30;--auth-red:#e84d5b;--auth-soft:#2d191e}
html[data-theme="light"]{color-scheme:light;--auth-bg:#fff8f8;--auth-card:#fff;--auth-ink:#2b1a1e;--auth-muted:#806a70;--auth-line:#e8d8db;--auth-red:#bd263c;--auth-soft:#fbe9ec}
html[data-theme] body{font-family:Inter,"Aptos","PingFang SC","Microsoft YaHei","Noto Sans CJK SC",sans-serif;color:var(--auth-ink);background:var(--auth-bg)}
html[data-theme] .box{position:relative;overflow:hidden;width:min(430px,calc(100% - 32px));padding:32px 30px 24px;border:1px solid var(--auth-line);border-radius:15px;background:var(--auth-card);box-shadow:0 22px 56px rgba(25,8,12,.18)}
html[data-theme] .box::before{content:"";position:absolute;inset:0 0 auto;height:4px;background:var(--auth-red)}
html[data-theme] .brand{gap:12px;margin-bottom:21px}
html[data-theme] .brand .logo{width:42px;height:42px;border-radius:12px;box-shadow:none}
html[data-theme] .brand b{font-size:15px;color:var(--auth-ink)}
html[data-theme] .brand i{margin-top:3px;color:var(--auth-muted);font-size:11px;letter-spacing:.2px}
html[data-theme] .badge{margin-bottom:12px;border-color:var(--auth-line);border-radius:7px;background:var(--auth-soft);color:var(--auth-red)}
html[data-theme] h2{margin:0 0 7px;color:var(--auth-ink);font-size:24px;letter-spacing:-.6px}
html[data-theme] p.tip{margin:0 0 20px;color:var(--auth-muted);font-size:13px;line-height:1.65}
html[data-theme] label{margin-top:14px;color:var(--auth-ink);font-size:12.5px}
html[data-theme] input{min-height:44px;margin-top:7px;padding:10px 12px;border:1px solid var(--auth-line);border-radius:8px;background:var(--auth-card);color:var(--auth-ink)}
html[data-theme] input:focus{outline:3px solid rgba(232,77,91,.2);border-color:var(--auth-red);box-shadow:none}
html[data-theme] form>button:not(.eye){min-height:46px;margin-top:20px;border-radius:8px;background:var(--auth-red);box-shadow:none;font-weight:700}
html[data-theme] form>button:not(.eye):hover{background:#9f2034;filter:none}
html[data-theme] .error{border-color:var(--auth-line);border-radius:8px}
html[data-theme] .foot{margin-top:20px;padding-top:15px;border-top:1px solid var(--auth-line);color:var(--auth-muted)}
html[data-theme] .foot a{color:var(--auth-red)}
html[data-theme] .auth-theme{min-height:38px;padding:7px 12px;border:1px solid var(--auth-line);border-radius:8px;background:var(--auth-card);color:var(--auth-ink);box-shadow:none}
html[data-theme] .auth-theme:hover{border-color:var(--auth-red);background:var(--auth-soft);color:var(--auth-red)}
html[data-theme] :focus-visible{outline:3px solid rgba(232,77,91,.48);outline-offset:2px}
@media(max-width:480px){html[data-theme] .box{width:calc(100% - 28px);margin:58px auto 20px;padding:28px 21px 21px}}
@media(prefers-reduced-motion:reduce){html[data-theme] *,html[data-theme] *::before,html[data-theme] *::after{scroll-behavior:auto!important;transition:none!important}}

/* Match the public login page and the white admin workspace. */
html[data-theme]{color-scheme:light;--auth-ink:#171717;--auth-muted:#737373;--auth-line:#e5e5e5;--auth-surface:#fff;--auth-soft:#f5f5f5}
html[data-theme="dark"],html[data-theme="light"]{color-scheme:light}
html[data-theme="dark"] body,html[data-theme="light"] body{min-height:100svh;padding:max(20px,env(safe-area-inset-top)) max(20px,env(safe-area-inset-right)) max(20px,env(safe-area-inset-bottom)) max(20px,env(safe-area-inset-left));color:#171717;background:#fff}
html[data-theme="dark"] .box,html[data-theme="light"] .box{width:min(420px,100%);padding:30px 28px 24px;border:1px solid #e5e5e5;border-radius:12px;background:#fff;box-shadow:0 12px 36px rgba(0,0,0,.06)}
html[data-theme="dark"] .brand .logo,html[data-theme="light"] .brand .logo{filter:grayscale(1);box-shadow:none}
html[data-theme="dark"] .box::before,html[data-theme="light"] .box::before{display:none;height:0;background:transparent}
html[data-theme] .ni{background:#737373}
html[data-theme="dark"] .brand b,html[data-theme="light"] .brand b,html[data-theme="dark"] h2,html[data-theme="light"] h2{color:#171717}
html[data-theme="dark"] .brand i,html[data-theme="light"] .brand i,html[data-theme="dark"] p.tip,html[data-theme="light"] p.tip,html[data-theme="dark"] .foot,html[data-theme="light"] .foot{color:#737373}
html[data-theme="dark"] label,html[data-theme="light"] label{color:#404040}
html[data-theme="dark"] .badge,html[data-theme="light"] .badge{border-color:#e5e5e5;border-radius:7px;background:#f7f7f7;color:#404040}
html[data-theme="dark"] input,html[data-theme="light"] input{min-height:44px;background:#fff;color:#171717;border-color:#dedede}
html[data-theme="dark"] input:focus,html[data-theme="light"] input:focus{outline:3px solid #e8e8e8;outline-offset:1px;border-color:#888}
html[data-theme="dark"] form>button:not(.eye),html[data-theme="light"] form>button:not(.eye){min-height:46px;border:1px solid #171717;border-radius:8px;background:#171717;color:#fff;box-shadow:none}
html[data-theme="dark"] form>button:not(.eye):hover,html[data-theme="light"] form>button:not(.eye):hover{background:#333;border-color:#333;filter:none}
html[data-theme="dark"] .foot,html[data-theme="light"] .foot{border-top-color:#ededed}
html[data-theme="dark"] .foot a,html[data-theme="light"] .foot a{color:#262626}
html[data-theme="dark"] .auth-theme,html[data-theme="light"] .auth-theme{display:none!important}
html[data-theme="dark"] :focus-visible,html[data-theme="light"] :focus-visible{outline:3px solid #777;outline-offset:3px}
@media(max-width:480px){html[data-theme="dark"] .box,html[data-theme="light"] .box{width:100%;padding:26px 21px 21px}}
@media(prefers-reduced-motion:reduce){html[data-theme] *,html[data-theme] *::before,html[data-theme] *::after{scroll-behavior:auto!important;animation-duration:.01ms!important;animation-iteration-count:1!important;transition-duration:.01ms!important}}

html[data-theme="dark"] h1,html[data-theme="light"] h1{margin:0 0 7px;color:#171717;font-size:24px;letter-spacing:-.6px;text-wrap:balance}
.skip-link{position:fixed;top:8px;left:8px;z-index:500;transform:translateY(-160%);padding:9px 12px;border:1px solid #171717;border-radius:7px;background:#171717;color:#fff;text-decoration:none}
.skip-link:focus{transform:translateY(0)}

html[data-theme="dark"] .pw .eye,html[data-theme="light"] .pw .eye{color:#525252}
html[data-theme="dark"] .pw .eye:hover,html[data-theme="light"] .pw .eye:hover{background:#f5f5f5;color:#171717}


"""

ADMIN_LOGIN_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#ffffff">
<title>管理员登录 · DouYinSparkFlow</title>
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<script>(function(){var t="dark";try{var saved=localStorage.getItem("dsh-theme");if(saved==="dark"||saved==="light")t=saved;}catch(e){}document.documentElement.setAttribute("data-theme",t);})();</script>
<style>__CSS__</style></head><body><a class="skip-link" href="#auth-main">&#36339;&#21040;&#20027;&#35201;&#20869;&#23481;</a><button class="auth-theme" id="auth-theme" type="button" aria-label="切换到白红主题">白红</button><main class="box" id="auth-main" tabindex="-1">
<div class="brand"><span class="logo" aria-hidden="true"></span><div><b>DouYinSparkFlow</b><i>ADMIN CONSOLE</i></div></div>
<span class="badge"><i class="ni ni-flame" aria-hidden="true"></i>管理员入口</span>
<h1>管理员登录</h1>
<p class="tip">这里只给管理员用：用管理员账号密码进来，管理所有用户的抖音号、发送记录和系统开关。</p>
<form method="post" action="/login">
<input type="hidden" name="next" value="__NEXT__">
<label for="ad-user">管理员账号</label>
<input id="ad-user" name="username" value="__USER__" maxlength="64" autocomplete="username"
  autocapitalize="off" autocorrect="off" spellcheck="false" required>
<label for="ad-pass">密码</label>
<span class="pw"><input id="ad-pass" name="password" type="password" autocomplete="current-password" required>
<button type="button" class="eye" id="ad-eye" aria-label="显示密码" aria-pressed="false"><svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12s3.6-6.5 10-6.5S22 12 22 12s-3.6 6.5-10 6.5S2 12 2 12Z"/><circle cx="12" cy="12" r="2.8"/></svg></button></span>
<button id="ad-go">进入管理控制台</button></form>
__ERROR__
<p class="foot">不是管理员？<a href="/login">返回普通登录</a><br><a href="https://github.com/BARONCMH/DouYinSparkFlow-OpenSource" target="_blank" rel="noopener noreferrer">项目已开源 · 查看 GitHub</a></p>
</main>
<script>
(function(){
  var box=document.getElementById("ad-user"), pw=document.getElementById("ad-pass");
  var go=document.getElementById("ad-go"), eye=document.getElementById("ad-eye");
  if(!box||!pw) return;
  if(box.value) pw.focus(); else box.focus();
  if(eye){ eye.onclick=function(){ var show=pw.type==="password"; pw.type=show?"text":"password";
    eye.setAttribute("aria-pressed", show?"true":"false"); eye.setAttribute("aria-label", show?"隐藏密码":"显示密码"); pw.focus(); }; }
  var form=box.form;
  if(form){ form.addEventListener("submit", function(){ if(!box.value.trim()||!pw.value) return;
    if(go){ go.disabled=true; go.textContent="正在验证…"; } }); }
})();
</script>
<script>(function(){var b=document.getElementById("auth-theme");function paint(){var dark=document.documentElement.getAttribute("data-theme")==="dark";var label=dark?"白红":"红黑";var action=dark?"切换到白红主题":"切换到红黑主题";if(b){b.textContent=label;b.title=action;b.setAttribute("aria-label",action);}var m=document.querySelector('meta[name="theme-color"]');if(m)m.setAttribute("content","#ffffff");}if(b)b.onclick=function(){var next=document.documentElement.getAttribute("data-theme")==="dark"?"light":"dark";document.documentElement.setAttribute("data-theme",next);try{localStorage.setItem("dsh-theme",next);}catch(e){}paint();};paint();})();</script></body></html>

"""

LOGIN_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#ffffff">
<title>登录 · DouYinSparkFlow</title>
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<script>(function(){var t="dark";try{var saved=localStorage.getItem("dsh-theme");if(saved==="dark"||saved==="light")t=saved;}catch(e){}document.documentElement.setAttribute("data-theme",t);})();</script>
<style>__CSS__</style></head><body><a class="skip-link" href="#auth-main">&#36339;&#21040;&#20027;&#35201;&#20869;&#23481;</a><button class="auth-theme" id="auth-theme" type="button" aria-label="切换到白红主题">白红</button><main class="box" id="auth-main" tabindex="-1">
<div class="brand"><span class="logo" aria-hidden="true"></span><div><b>DouYinSparkFlow</b><i>抖音火花助手 · 控制台</i></div></div>
<h1>登录控制台</h1>
<p class="tip">用你的控制台账号登录，管理自己的抖音号、目标好友和手动发送配置。</p>
<form method="post" action="/login">
<input type="hidden" name="next" value="__NEXT__">
<label for="lg-user">账号</label>
<input id="lg-user" name="username" value="__USER__" maxlength="64" autocomplete="username"
  autocapitalize="off" autocorrect="off" spellcheck="false" enterkeyhint="next"
  placeholder="你的控制台登录名" required>
<label for="lg-pass">密码</label>
<span class="pw"><input id="lg-pass" name="password" type="password" autocomplete="current-password" enterkeyhint="go" required>
<button type="button" class="eye" id="lg-eye" aria-label="显示密码" aria-pressed="false"><svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12s3.6-6.5 10-6.5S22 12 22 12s-3.6 6.5-10 6.5S2 12 2 12Z"/><circle cx="12" cy="12" r="2.8"/></svg></button></span>
<label class="keep" for="lg-keep"><input type="checkbox" id="lg-keep"> 记住账号（下次自动填好）</label>
<button id="lg-go">登录</button></form>
__ERROR__
<p class="foot">__REGOFFER__<br><a href="https://github.com/BARONCMH/DouYinSparkFlow-OpenSource" target="_blank" rel="noopener noreferrer">项目已开源 · 查看 GitHub</a><br><span style="color:#a3aab8">忘了密码？找管理员重置 · </span><a href="/login?next=/admin" style="font-weight:400;color:#a3aab8">管理员入口</a></p>
</main>
<script>
(function(){
  var form=document.querySelector("form[action='/login']");
  var box=document.getElementById("lg-user"), pw=document.getElementById("lg-pass");
  var go=document.getElementById("lg-go"), eye=document.getElementById("lg-eye"), keep=document.getElementById("lg-keep");
  var UKEY="dsh-last-user", KKEY="dsh-keep-user";
  function get(k){ try{ return localStorage.getItem(k)||""; }catch(e){ return ""; } }
  function put(k,v){ try{ if(v) localStorage.setItem(k,v); else localStorage.removeItem(k); }catch(e){} }
  if(box&&!box.value){ var saved=get(UKEY); if(saved) box.value=saved; }
  /* 默认记住登录名（只在本地存个名字，不碰密码）；用户取消过一次就一直不记 */
  if(keep) keep.checked = get(KKEY)!=="0";
  if(box&&pw){ if(box.value) pw.focus(); else box.focus(); }
  if(eye&&pw){ eye.onclick=function(){ var show=pw.type==="password"; pw.type=show?"text":"password";
    eye.setAttribute("aria-pressed", show?"true":"false"); eye.setAttribute("aria-label", show?"隐藏密码":"显示密码"); pw.focus(); }; }
  if(form){ form.addEventListener("submit", function(){
    if(!box||!pw||!box.value.trim()||!pw.value) return;
    if(keep&&!keep.checked){ put(UKEY,""); put(KKEY,"0"); }
    else { put(UKEY, box.value.trim()); put(KKEY,"1"); }
    if(go){ go.disabled=true; go.textContent="登录中…"; }
  }); }
})();
</script>
<script>(function(){var b=document.getElementById("auth-theme");function paint(){var dark=document.documentElement.getAttribute("data-theme")==="dark";var label=dark?"白红":"红黑";var action=dark?"切换到白红主题":"切换到红黑主题";if(b){b.textContent=label;b.title=action;b.setAttribute("aria-label",action);}var m=document.querySelector('meta[name="theme-color"]');if(m)m.setAttribute("content","#ffffff");}if(b)b.onclick=function(){var next=document.documentElement.getAttribute("data-theme")==="dark"?"light":"dark";document.documentElement.setAttribute("data-theme",next);try{localStorage.setItem("dsh-theme",next);}catch(e){}paint();};paint();})();</script></body></html>

"""

REGISTER_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#ffffff">
<title>注册 · DouYinSparkFlow</title>
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<script>(function(){var t="dark";try{var saved=localStorage.getItem("dsh-theme");if(saved==="dark"||saved==="light")t=saved;}catch(e){}document.documentElement.setAttribute("data-theme",t);})();</script>
<style>__CSS__</style></head><body><a class="skip-link" href="#auth-main">&#36339;&#21040;&#20027;&#35201;&#20869;&#23481;</a><button class="auth-theme" id="auth-theme" type="button" aria-label="切换到白红主题">白红</button><main class="box" id="auth-main" tabindex="-1">
<div class="brand"><span class="logo" aria-hidden="true"></span><div><b>DouYinSparkFlow</b><i>抖音火花助手 · 控制台</i></div></div>
<h1>注册一个账号</h1>
<p class="tip">注册后登录控制台，绑定你自己的抖音号（手机号登录或扫码授权），就能自己设目标好友和发送配置。别人的账号互相看不到。</p>
<form method="post" action="/register">
<label for="rg-user">登录名（2-32 位字母、数字或 _ . @ -）</label>
<input id="rg-user" name="username" maxlength="32" autocomplete="username"
  autocapitalize="off" autocorrect="off" spellcheck="false" enterkeyhint="next" required>
<label for="rg-phone">手机号（11 位，不需要验证码）</label>
<input id="rg-phone" name="phone" type="tel" inputmode="numeric" maxlength="11"
  pattern="1[3-9][0-9]{9}" autocomplete="tel" enterkeyhint="next" required
  placeholder="13812345678">
<label for="rg-pass">密码（至少 6 位）</label>
<span class="pw"><input id="rg-pass" name="password" type="password" autocomplete="new-password" minlength="6" enterkeyhint="next" required>
<button type="button" class="eye" id="rg-eye" aria-label="显示密码" aria-pressed="false"><svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12s3.6-6.5 10-6.5S22 12 22 12s-3.6 6.5-10 6.5S2 12 2 12Z"/><circle cx="12" cy="12" r="2.8"/></svg></button></span>
<label for="rg-pass2">再输一次密码</label>
<input id="rg-pass2" name="password2" type="password" autocomplete="new-password" minlength="6" enterkeyhint="go" required>
<p class="hint bad" id="rg-hint" role="alert" hidden></p>
<button id="rg-go">注册并登录</button></form>
__ERROR__
<p class="foot">已经有账号了？<a href="/login">去登录</a><br><a href="https://github.com/BARONCMH/DouYinSparkFlow-OpenSource" target="_blank" rel="noopener noreferrer">项目已开源 · 查看 GitHub</a></p>
</main>
<script>
(function(){
  var form=document.querySelector("form[action='/register']");
  var u=document.getElementById("rg-user"), ph=document.getElementById("rg-phone"), p1=document.getElementById("rg-pass"), p2=document.getElementById("rg-pass2");
  var go=document.getElementById("rg-go"), eye=document.getElementById("rg-eye"), hint=document.getElementById("rg-hint");
  if(u) u.focus();
  if(eye&&p1&&p2){ eye.onclick=function(){ var show=p1.type==="password"; p1.type=p2.type=show?"text":"password";
    eye.setAttribute("aria-pressed", show?"true":"false"); eye.setAttribute("aria-label", show?"隐藏密码":"显示密码"); }; }
  var PHONE_RE_JS=/^1[3-9][0-9]{9}$/, bad=null;
  function check(){
    if(!hint||!p1||!p2) return true;
    bad=null;
    if(ph){
      var pv=ph.value.trim();
      if(!pv){ bad=ph; hint.textContent="请填手机号"; hint.hidden=false; return false; }
      if(!PHONE_RE_JS.test(pv)){ bad=ph; hint.textContent="手机号要填 11 位数字（比如 13812345678）"; hint.hidden=false; return false; }
    }
    if(p2.value && p1.value!==p2.value){ bad=p2; hint.textContent="两次输入的密码不一样"; hint.hidden=false; return false; }
    hint.hidden=true; return true;
  }
  if(ph) ph.addEventListener("blur", function(){ if(ph.value.trim()) check(); });
  if(ph) ph.addEventListener("input", function(){ if(hint&&!hint.hidden) check(); });
  if(p2) p2.addEventListener("input", check);
  if(p1) p1.addEventListener("input", function(){ if(hint&&!hint.hidden) check(); });
  if(form){ form.addEventListener("submit", function(e){
    if(!check()){ e.preventDefault(); if(bad){ bad.focus(); if(bad.select) bad.select(); } return; }
    if(go){ go.disabled=true; go.textContent="注册中…"; }
  }); }
})();
</script>
<script>(function(){var b=document.getElementById("auth-theme");function paint(){var dark=document.documentElement.getAttribute("data-theme")==="dark";var label=dark?"白红":"红黑";var action=dark?"切换到白红主题":"切换到红黑主题";if(b){b.textContent=label;b.title=action;b.setAttribute("aria-label",action);}var m=document.querySelector('meta[name="theme-color"]');if(m)m.setAttribute("content","#ffffff");}if(b)b.onclick=function(){var next=document.documentElement.getAttribute("data-theme")==="dark"?"light":"dark";document.documentElement.setAttribute("data-theme",next);try{localStorage.setItem("dsh-theme",next);}catch(e){}paint();};paint();})();</script></body></html>

"""


def html_escape(text) -> str:
    return (
        str(text if text is not None else "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def image_mime(data: bytes) -> str:
    """按文件头判断图片类型。
    以前发送截图一律回 image/jpeg：文件名虽然是 .jpg，但内容一旦不是图片，
    浏览器会按内容嗅探，同源就能当页面跑脚本 —— 所以这里按真实字节定类型，
    顺便让所有响应都带上统一的安全头。
    """
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "application/octet-stream"


def page_with_limits(html: str) -> str:
    """把"每个普通用户最多几个抖音号"注进页面，前端文案就不会和后台常量说两套话。"""
    return html.replace("__MAX_ACCOUNTS__", str(MAX_ACCOUNTS_PER_USER))


def safe_next(value: str) -> str:
    """登录成功后往哪跳。只认站内几个固定页面，免得变成开放重定向。"""
    text = str(value or "").strip()
    if text in ("/admin", "/admin/", "/"):
        return text
    return "/"


def _short_user(value, limit: int = 64) -> str:
    """登录页回填用的登录名：截短一点，别让它变成超长 URL、也别灌进页面。"""
    return str(value or "").strip()[:limit]


DOWNLOAD_MANIFEST = json.dumps(
    {
        "id": "/",
        "name": "DouYinSparkFlow",
        "short_name": "续火花",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "background_color": "#f2f7f6",
        "theme_color": "#087f8c",
        "icons": [
            {"src": "/app-icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
            {"src": "/apple-touch-icon.png", "sizes": "180x180", "type": "image/png", "purpose": "any"},
            {"src": "/favicon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any maskable"}
        ],
    },
    ensure_ascii=False,
    separators=(",", ":"),
)

WEBPUSH_SERVICE_WORKER_JS = r"""'use strict';
self.addEventListener('push', function (event) {
  var data = {};
  try { data = event.data ? event.data.json() : {}; } catch (e) {}
  var title = String(data.title || '发送任务已完成').slice(0, 80);
  var options = {
    body: String(data.body || '打开续火花控制台查看发送结果').slice(0, 180),
    icon: '/app-icon-512.png',
    badge: '/apple-touch-icon.png',
    tag: String(data.tag || 'sparkflow-send').slice(0, 80),
    renotify: false,
    data: { url: '/' }
  };
  event.waitUntil(self.registration.showNotification(title, options));
});
self.addEventListener('notificationclick', function (event) {
  event.notification.close();
  event.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(function (clients) {
    for (var i = 0; i < clients.length; i++) {
      var client = clients[i];
      if (client.url.indexOf(self.location.origin + '/') === 0 && 'focus' in client) {
        return client.focus();
      }
    }
    return self.clients.openWindow('/');
  }));
});
"""

DOWNLOAD_PAGE_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#087f8c"><meta name="apple-mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-title" content="续火花"><meta name="apple-mobile-web-app-status-bar-style" content="default">
<link rel="manifest" href="/manifest.webmanifest"><link rel="icon" href="/favicon.svg" type="image/svg+xml"><link rel="apple-touch-icon" href="/apple-touch-icon.png">
<title>下载手机应用 · DouYinSparkFlow</title><style>
*{box-sizing:border-box}body{margin:0;min-height:100vh;background:radial-gradient(ellipse at 80% -10%,#dff2ed,transparent 42%),#f2f7f6;color:#172b32;font:16px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,"PingFang SC","Microsoft YaHei",sans-serif}
main{width:min(740px,100% - 32px);margin:42px auto;padding-bottom:40px}.brand{display:flex;align-items:center;gap:12px;margin-bottom:24px}.logo{width:46px;height:46px;border-radius:15px;background:linear-gradient(135deg,#fb4857,#b7192d);display:grid;place-items:center;color:white;font-size:26px;box-shadow:0 7px 18px #ff6b3438}.brand b{display:block;font-size:19px}.brand span{display:block;color:#6a8187;font-size:13px}
.card{background:#fff;border:1px solid #dce9e9;border-radius:20px;padding:24px;margin:14px 0;box-shadow:0 8px 28px #1a484a0d}.card h1{font-size:24px;line-height:1.25;margin:0 0 8px}.card h2{font-size:17px;margin:0 0 8px}.muted{color:#6a8187;font-size:14px}.download{display:flex;justify-content:center;align-items:center;min-height:54px;border-radius:12px;background:#087f8c;color:white;text-decoration:none;font-weight:700;margin:18px 0 8px}.download:hover{background:#076b76}.download.disabled{background:#82989a;pointer-events:none}.hash{font:12px/1.6 ui-monospace,Consolas,monospace;overflow-wrap:anywhere;background:#f0f6f5;border-radius:10px;padding:10px;color:#405759}.steps{padding-left:22px}.steps li{padding:3px 0}.badge{display:inline-block;background:#e6f6f4;color:#076b76;border-radius:99px;padding:3px 9px;font-size:12px;font-weight:700}a{color:#087f8c}details{padding:13px 0;border-top:1px solid #dce9e9}summary{cursor:pointer;font-weight:700}.foot{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;font-size:14px}
@media(max-width:600px){main{margin:20px auto;width:calc(100% - 22px)}.card{padding:18px;border-radius:17px}.card h1{font-size:22px}}
</style></head><body><main><div class="brand"><div class="logo">✦</div><div><b>DouYinSparkFlow</b><span>手机应用与安装说明</span></div></div>
<section class="card"><span class="badge">Android 安装包</span><h1>把续火花管理放进口袋</h1><p>登录或注册原有网站账号，查看今天的发送状态，管理自己的账号配置。开启系统提醒后，发送成功、部分成功或失败都会显示通知；应用打开时检查更及时，后台通知可能受 Android 省电影响而延迟。</p>
__APK_BUTTON__<div class="muted">安装包大小：__APK_SIZE__ · SHA-256</div><div class="hash">__APK_SHA__</div>
<p class="muted">首次安装时 Android 可能要求允许浏览器或文件管理器安装此来源的应用。已安装旧版的设备需要先卸载旧版再安装本版，并重新登录；抖音账号配置和发送记录保存在服务器，不受卸载影响。安装后请在应用内开启系统通知。</p></section>
<section class="card"><h2>iPhone / iPad 主屏幕版</h2><p>这是可安装的网页应用，复用网站账号和普通用户功能，不需要 App Store 安装：</p><ol class="steps"><li>用 Safari 打开 <a href="/login">登录页</a>并登录。</li><li>点分享按钮，选择“添加到主屏幕”。</li><li>确认名称后添加，从主屏幕图标进入，开启“发送结果通知”。</li></ol><p class="muted">iOS 16.4 及以上版本支持网页推送。通知只在添加到主屏幕并从图标打开后开启；普通 Safari 标签页没有系统推送权限。授权后，发送成功或失败会由服务器推送，即使网页关闭也能收到。</p></section>
__COOKIE_TOOL_SECTION__
<section class="card"><h2>安全与校验</h2><p>应用只连接本网站的 HTTPS 地址；TLS 校验失败时会停止连接，不会绕过证书检查。应用不请求通讯录、短信或定位权限。Android 通知需单独授权；用于检查发送结果的会话 Cookie 在设备上用 Android Keystore 加密保存。</p><p class="muted">直接下载适用于网站分发测试，不代表已通过 Google Play 商店审核。安装前可核对上方 SHA-256。</p></section>
<div class="foot"><a href="/login">返回登录</a><a href="/">打开控制台</a></div></main></body></html>"""


def render_download_page() -> str:
    button = '<div class="download disabled">Android 安装包准备中</div>'
    size_text = "尚未发布"
    digest = "安装包准备中"
    try:
        if ANDROID_APK_PATH.is_file() and ANDROID_APK_PATH.resolve().parent == DOWNLOAD_DIR.resolve():
            button = '<a class="download" href="/downloads/DouYinSparkFlow.apk">下载 Android 安装包</a>'
            size = ANDROID_APK_PATH.stat().st_size
            size_text = "%.1f MB" % (size / (1024 * 1024))
            try:
                sidecar = ANDROID_APK_SHA_PATH.read_text(encoding="ascii").split()[0]
                digest = sidecar if re.fullmatch(r"[0-9a-fA-F]{64}", sidecar) else ""
            except Exception:
                digest = ""
            if not digest:
                sha = hashlib.sha256()
                with ANDROID_APK_PATH.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        sha.update(chunk)
                digest = sha.hexdigest()
    except Exception:
        button = '<div class="download disabled">Android 安装包准备中</div>'
    cookie_section = '<section class="card"><h2>抖音 Cookie 获取工具</h2><div class="download disabled">Windows EXE 准备中</div></section>'
    try:
        if COOKIE_TOOL_EXE_PATH.is_file() and COOKIE_TOOL_EXE_PATH.resolve().parent == DOWNLOAD_DIR.resolve():
            cookie_size = "%.1f MB" % (COOKIE_TOOL_EXE_PATH.stat().st_size / (1024 * 1024))
            cookie_digest = ""
            try:
                sidecar = COOKIE_TOOL_SHA_PATH.read_text(encoding="ascii").split()[0]
                cookie_digest = sidecar if re.fullmatch(r"[0-9a-fA-F]{64}", sidecar) else ""
            except Exception:
                pass
            if not cookie_digest:
                sha = hashlib.sha256()
                with COOKIE_TOOL_EXE_PATH.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        sha.update(chunk)
                cookie_digest = sha.hexdigest()
            cookie_section = (
                '<section class="card"><h2>抖音 Cookie 获取工具</h2>'
                '<p>下载后双击 EXE，在独立的 Microsoft Edge 窗口登录自己的抖音账号；登录成功后，Cookie JSON 会保存在 EXE 所在文件夹。</p>'
                '<a class="download" href="/downloads/Get-Douyin-Cookies.exe">下载 Windows EXE（%s）</a>'
                '<div class="muted">SHA-256</div><div class="hash">%s</div>'
                '<p class="muted">无需安装 Python；需要 Microsoft Edge。Cookie 是登录凭证，工具只在本机保存、不上传。导入后请删除 JSON 文件。程序未做代码签名，Windows 可能提示未知发布者，可核对 SHA-256。</p></section>'
            ) % (cookie_size, cookie_digest)
    except Exception:
        cookie_section = '<section class="card"><h2>抖音 Cookie 获取工具</h2><div class="download disabled">Windows EXE 准备中</div></section>'
    return (DOWNLOAD_PAGE_HTML.replace("__APK_BUTTON__", button)
            .replace("__APK_SIZE__", size_text).replace("__APK_SHA__", digest)
            .replace("__COOKIE_TOOL_SECTION__", cookie_section))


def render_login(error: str = "", nxt: str = "", user: str = "") -> str:
    """普通登录页 / 管理员登录页：从管理员入口进来的，给一张明显不一样的脸。

    user 是"上一次输的登录名"：密码打错了回来，账号框里还是原来那个，
    不用重新打一遍（手机上这一步最烦）。
    """
    target = safe_next(nxt)
    error_box = '<div class="error" role="alert">%s</div>' % html_escape(error) if error else ""
    filled = html_escape(_short_user(user))
    if target in ("/admin", "/admin/"):
        html = ADMIN_LOGIN_HTML.replace("__CSS__", ADMIN_LOGIN_CSS)
        return (
            html.replace("__NEXT__", html_escape(target))
            .replace("__USER__", filled)
            .replace("__ERROR__", error_box)
        )
    html = LOGIN_HTML.replace("__CSS__", AUTH_CSS)
    # 这一行会塞进页脚，所以只给行内片段，不带 <p>
    if register_allowed():
        offer = '还没有账号？<a href="/register">注册一个</a>'
    else:
        offer = '需要账号？<a href="/login">找管理员开一个</a>'
    return (
        html.replace("__NEXT__", html_escape(target))
        .replace("__USER__", filled)
        .replace("__REGOFFER__", offer)
        .replace("__ERROR__", error_box)
    )


def render_register(error: str = "") -> str:
    html = REGISTER_HTML.replace("__CSS__", AUTH_CSS)
    body = '<div class="error" role="alert">%s</div>' % html_escape(error) if error else ""
    return html.replace("__ERROR__", body)


class Handler(BaseHTTPRequestHandler):
    server_version = "SparkFlowPanel/1.0"
    # 单个连接的 socket 超时：请求行/响应写完不再无限挂着，慢连接也不会占死线程
    timeout = 30

    def log_message(self, fmt, *args):
        return

    def _claims(self) -> dict:
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        morsel = cookie.get("sid")
        claims = token_claims(morsel.value) if morsel else {}
        if not claims:
            return {}
        if claims.get("a"):
            # 管理员会话要和当前面板密码绑定：改过密码，旧登录一律失效
            if str(claims.get("k") or "") != admin_key():
                return {}
            return claims
        # 注册用户：对一下令牌里的 key，用户被删掉或重建过就让旧登录失效
        item = load_users().get(str(claims.get("u") or ""))
        if not isinstance(item, dict):
            return {}
        if str(item.get("key") or "") != str(claims.get("k") or ""):
            return {}
        return claims

    def _authed(self) -> bool:
        return bool(self._claims())

    def _is_admin(self) -> bool:
        return bool(self._claims().get("a"))

    def _user(self) -> str:
        return str(self._claims().get("u") or "")

    def _scopes(self):
        """我能管的抖音号：管理员返回 None（=全部），普通用户返回自己绑的那些"""
        if self._is_admin():
            return None
        return user_accounts(self._user())

    def _deny(self) -> bool:
        """只有管理员能做的事：非管理员直接回 403"""
        if self._is_admin():
            return False
        self._json({"ok": False, "error": "这个操作只有管理员可以执行"}, 403)
        return True

    def _same_origin_request(self) -> bool:
        """Require browser-origin proof for push-subscription changes."""
        source = str(self.headers.get("Origin") or self.headers.get("Referer") or "").strip()
        host = str(self.headers.get("Host") or "").strip().lower()
        try:
            parsed = urlsplit(source)
            return bool(
                self._https() and parsed.scheme == "https" and parsed.netloc.lower() == host
                and not parsed.username and not parsed.password
            )
        except Exception:
            return False

    def _owns(self, unique_id: str) -> bool:
        """这个抖音号是不是我绑的（管理员都算）"""
        scopes = self._scopes()
        if scopes is None:
            return True
        return str(unique_id or "") in scopes

    def _client_ip(self) -> str:
        """访客真实 IP：本机代理转发时优先看它填的来源头。"""
        return pick_client_ip(
            str(self.client_address[0] if self.client_address else ""),
            self.headers.get("X-Real-IP") or "",
            self.headers.get("X-Forwarded-For") or "",
        )

    def _deny_other(self, unique_id: str) -> bool:
        if self._owns(unique_id):
            return False
        self._json({"ok": False, "error": "这个抖音号不在你的名下"}, 403)
        return True

    def _want_uid(self, payload=None, query=None) -> str:
        """这次请求明确指定了哪个抖音号（没指定就是空字符串）。"""
        if isinstance(payload, dict):
            uid = str(payload.get("unique_id") or "").strip()
            if uid:
                return uid
        if query:
            return str((query.get("unique_id") or [""])[0]).strip()
        return ""

    def _my_session(self, payload=None, query=None):
        """这次请求该操作哪个授权会话：优先客户端指定的抖音号，其次按归属我名下的号。"""
        want = self._want_uid(payload, query)
        sess = browser.pick_for(self._scopes(), want)
        # 指定的号不属于我 → 直接当没有（绝不回落到别人的会话上）
        if sess is None and want and not self._owns(want):
            return None
        return sess

    def _shot_allowed(self, payload=None, query=None) -> bool:
        """当前实时画面/授权浏览器是不是我名下的号（防止看到/操作别人扫码的画面）

        普通用户这里必须"查得到归属才算我的"：浏览器收尾的那一瞬间 owner 会被清空，
        但 Playwright 还在跑、命令队列还在收，以前这种空归属会被当成"谁都可以"，
        等于给了一个可以往别人会话里点鼠标的窗口。
        """
        if self._is_admin():
            return True
        want = self._want_uid(payload, query)
        if want and not self._owns(want):
            return False                     # 指名要动别人的号：直接拒，不许回落到自己那个会话
        sess = self._my_session(payload, query)
        if sess is not None:
            owner = str(getattr(sess, "owner", "") or "")
            return bool(owner) and self._owns(owner)
        # 没有授权会话时，登录检测的画面还可能是我的
        _image, owner = current_frame()
        return bool(owner) and self._owns(owner)

    def _browser_owner_ok(self, payload=None, query=None) -> bool:
        """当前这台授权浏览器归不归我。

        归属为空＝浏览器正在收尾或状态不完整。这种时候对普通用户一律不放行：
        以前是"空的就算谁的都行"，于是收尾的那一瞬间，谁都能把验证码、鼠标键盘
        塞进别人的会话里。
        """
        if self._is_admin():
            return True                      # 管理员本来就能管所有号
        want = self._want_uid(payload, query)
        if want and not self._owns(want):
            return False
        sess = self._my_session(payload, query)
        if sess is None:
            return False
        owner = str(getattr(sess, "owner", "") or "")
        if owner:
            return self._owns(owner)
        return False                          # 归属为空＝正在收尾，普通用户不放行

    def _shot_is_mine(self, name: str) -> bool:
        """发送截图只放行自己账号的：归属直接查「文件名 → 抖音号」，不靠名字猜。"""
        if self._is_admin():
            return True
        uid = shot_uid_map().get(str(name or ""))
        return bool(uid) and self._owns(uid)

    def my_accounts(self) -> list:
        """我名下的抖音号（列表）。管理员＝全部。"""
        scopes = self._scopes()
        tasks = load_tasks()
        if scopes is None:
            return tasks
        return [t for t in tasks if str(t.get("unique_id") or "") in scopes]

    def my_account(self) -> dict:
        accounts = self.my_accounts()
        return accounts[0] if accounts else {}

    def visible_sends(self, limit: int = 0) -> list:
        """发送记录：管理员最多看最近 100 次，普通用户只看自己名下最近的 2 次。

        limit 是本次想要的条数，最终还要受角色上限约束（普通用户传 100 也只能拿到 2）。
        """
        cap = SEND_MAX_ADMIN if self._is_admin() else SEND_MAX_USER
        if limit and limit > 0:
            cap = min(cap, limit)
        runs = load_sends(max(cap, SEND_MAX_USER))
        if self._is_admin():
            return runs[:cap]
        scopes = set(self._scopes() or [])
        names = {str(t.get("username") or "") for t in self.my_accounts()}
        names.discard("")
        mine = []
        for run in runs:
            if not isinstance(run, dict):
                continue
            uid = str(run.get("unique_id") or "")
            if (uid and uid in scopes) or (not uid and str(run.get("account") or "") in names):
                mine.append(run)
        return mine[:cap]

    def visible_notification_runs(self) -> list:
        """Small, account-scoped send events for mobile notification polling."""
        runs = load_sends(SEND_STORE_MAX)
        if not self._is_admin():
            scopes = set(self._scopes() or [])
            names = {str(t.get("username") or "") for t in self.my_accounts()}
            names.discard("")
            runs = [
                run for run in runs
                if isinstance(run, dict)
                and (
                    (str(run.get("unique_id") or "") and str(run.get("unique_id") or "") in scopes)
                    or (not str(run.get("unique_id") or "") and str(run.get("account") or "") in names)
                )
            ]
        events = []
        for run in runs:
            if not isinstance(run, dict):
                continue
            status = str(run.get("status") or "")
            if status in ("running", "queued"):
                continue
            stable = json.dumps(run, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
            event_id = hashlib.sha256(stable.encode("utf-8")).hexdigest()[:32]
            events.append({
                "event_id": event_id,
                "at": str(run.get("at") or "")[:40],
                "account": str(run.get("account") or "")[:80],
                "unique_id": str(run.get("unique_id") or "")[:80],
                "status": status[:24],
            })
        return events

    def visible_logs(self) -> str:
        parts = []
        run_log = tail(RUN_LOG)
        app_log = tail(APP_LOG)
        if run_log:
            parts.append("----- 手动运行日志 -----\n" + run_log)
        if app_log:
            parts.append("----- 发送引擎日志 -----\n" + app_log)
        text = "\n\n".join(parts) if parts else "暂无日志"
        if self._is_admin():
            return text
        keys = [str(x) for x in (self._scopes() or [])]
        keys += [str(t.get("username") or "") for t in self.my_accounts()]
        keys = [k for k in keys if k]
        if not keys:
            return "（你还没有绑定抖音号，这里没有你的日志）"
        kept = [line for line in text.splitlines() if any(k in line for k in keys)]
        if not kept:
            return "（你的账号暂时没有新日志）"
        return "（这里只显示你自己账号的日志）\n" + "\n".join(kept)

    def _config_guard(self, payload: dict) -> str:
        """用户改配置前的检查：只能改自己名下的号，想换号只能用没人占用的。"""
        if self._is_admin():
            return ""
        mine = set(self._scopes() or [])
        orig = str(payload.get("orig_unique_id") or "").strip()
        uid = str(payload.get("unique_id") or "").strip()
        tasks = load_tasks()
        if orig:
            if orig not in mine:
                return "你只能修改自己绑定的抖音号"
        # 这次到底是不是"新建一个号"：改个名字重命名不算，所以限额只在真新建时卡。
        # 以前只在 orig 为空的那个分支里卡限额，orig 指向一个已经被删掉的号时
        # 会走到"新增"分支却绕开限额，一个人就能一直建下去。
        is_new = (not orig) or not any(str(t.get("unique_id") or "") == orig for t in tasks)
        if is_new and uid not in mine and len(mine) >= MAX_ACCOUNTS_PER_USER:
            return "每个控制台用户最多绑 %d 个抖音号：想换号就先把现在的删掉" % MAX_ACCOUNTS_PER_USER
        if uid and uid not in mine:
            taken = uid in {str(t.get("unique_id") or "") for t in tasks} or owner_of(uid)
            if taken:
                return "抖音号「%s」已经被占用了，换一个名字" % uid
        return ""

    def _https(self) -> bool:
        """这次请求是不是走 HTTPS 进来的（自己起的 TLS，或者前面挂了反代）。"""
        if str(self.headers.get("X-Forwarded-Proto") or "").strip().lower() == "https":
            return True
        return os.getenv("PANEL_COOKIE_SECURE", "").strip().lower() in ("1", "true", "yes", "on")

    def _sid_cookie(self, token: str) -> str:
        """登录 Cookie。走 HTTPS 时加 Secure：以后浏览器不会把它发到明文 HTTP 上。"""
        cookie = "sid=" + str(token) + "; Path=/; HttpOnly; SameSite=Lax; Max-Age=604800"
        return cookie + "; Secure" if self._https() else cookie

    def _sec_headers(self) -> None:
        """统一的安全响应头：不给点别的站点的页面里嵌、不允许乱猜类型、不带 Referer"""
        if self._https():
            # 既然这次是 https 进来的，就让浏览器以后只走 https
            self.send_header("Strict-Transport-Security", "max-age=31536000")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' blob: data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; "
            "base-uri 'none'; form-action 'self'",
        )

    def _json(self, payload: dict, status: int = 200, cookie=None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._sec_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def _html(self, text: str, status: int = 200, cookie=None) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self._sec_headers()
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def _text(self, text: str, status: int = 200) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self._sec_headers()
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        if length > MAX_BODY_BYTES:
            # 别再把那 2MB 读进来占着请求线程：直接挂断连接回 413
            self.close_connection = True
            raise BodyTooLarge()
        raw = self._read_exact(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def _form(self) -> dict:
        """普通 HTML 表单提交（登录页 / 注册页），不是 JSON"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        try:
            raw = parse_qs(self._read_exact(length).decode("utf-8"))
        except Exception:
            return {}
        return {key: (value or [""])[0] for key, value in raw.items()}

    def _read_exact(self, length: int) -> bytes:
        """按 Content-Length 读请求体，但绝不无限等。

        以前这里直接 rfile.read(length)：客户端只要报一个大长度却不发内容，
        这个请求线程就永远停在那儿。POST /login、/register 是在鉴权之前读表单的，
        所以几十个空连接就能把面板拖死（不登录也能打）。
        """
        chunks = []
        left = length
        deadline = time.time() + BODY_READ_TIMEOUT
        try:
            self.connection.settimeout(2.0)
        except Exception:
            pass
        try:
            while left > 0:
                if time.time() > deadline:
                    self.close_connection = True
                    return b""
                try:
                    chunk = self.rfile.read1(left)
                except Exception:
                    # 超时或被中断：再看一眼总时限，没到就继续等
                    if time.time() > deadline:
                        self.close_connection = True
                        return b""
                    continue
                if not chunk:
                    return b""
                chunks.append(chunk)
                left -= len(chunk)
        finally:
            try:
                self.connection.settimeout(self.timeout)
            except Exception:
                pass
        return b"".join(chunks)

    def _redirect(self, location: str, cookie=None) -> None:
        self.send_response(HTTPStatus.FOUND)
        self._sec_headers()
        self.send_header("Location", location)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _favicon(self) -> None:
        """标签页小图标。故意放在鉴权之前：登录页也要有图标，浏览器默认还会去要 /favicon.ico。"""
        body = FAVICON_SVG.encode("utf-8")
        self.send_response(200)
        self._sec_headers()
        self.send_header("Content-Type", "image/svg+xml; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(body)

    def _png_icon(self, body: bytes) -> None:
        self.send_response(200)
        self._sec_headers()
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=2592000, immutable")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/service-worker.js":
            body = WEBPUSH_SERVICE_WORKER_JS.encode("utf-8")
            self.send_response(200)
            self._sec_headers()
            self.send_header("Content-Type", "application/javascript; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Service-Worker-Allowed", "/")
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/download":
            self._html(render_download_page())
            return
        if path in ("/downloads/Get-Douyin-Cookies.exe", "/downloads/Get-Douyin-Cookies.exe.sha256"):
            target = COOKIE_TOOL_EXE_PATH if path.endswith(".exe") else COOKIE_TOOL_SHA_PATH
            if not target.is_file() or target.resolve().parent != DOWNLOAD_DIR.resolve():
                self._text("Not Found", 404)
                return
            try:
                size = target.stat().st_size
                self.send_response(200)
                self._sec_headers()
                self.send_header(
                    "Content-Type",
                    "application/vnd.microsoft.portable-executable" if path.endswith(".exe") else "text/plain; charset=utf-8",
                )
                if path.endswith(".exe"):
                    self.send_header("Content-Disposition", 'attachment; filename="Get-Douyin-Cookies.exe"')
                self.send_header("Content-Length", str(size))
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                with target.open("rb") as handle:
                    while True:
                        chunk = handle.read(256 * 1024)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        if path == "/manifest.webmanifest":
            body = DOWNLOAD_MANIFEST.encode("utf-8")
            self.send_response(200)
            self._sec_headers()
            self.send_header("Content-Type", "application/manifest+json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "public, max-age=3600")
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/apple-touch-icon.png":
            self._png_icon(APPLE_TOUCH_ICON_PNG)
            return
        if path == "/app-icon-512.png":
            self._png_icon(PWA_ICON_512_PNG)
            return
        if path in ("/downloads/DouYinSparkFlow.apk", "/downloads/DouYinSparkFlow.apk.sha256"):
            target = ANDROID_APK_PATH if path.endswith(".apk") else ANDROID_APK_SHA_PATH
            if not target.is_file() or target.resolve().parent != DOWNLOAD_DIR.resolve():
                self._text("Not Found", 404)
                return
            try:
                size = target.stat().st_size
                self.send_response(200)
                self._sec_headers()
                self.send_header(
                    "Content-Type",
                    "application/vnd.android.package-archive" if path.endswith(".apk") else "text/plain; charset=utf-8",
                )
                if path.endswith(".apk"):
                    self.send_header("Content-Disposition", 'attachment; filename="DouYinSparkFlow.apk"')
                self.send_header("Content-Length", str(size))
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                with target.open("rb") as handle:
                    while True:
                        chunk = handle.read(256 * 1024)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        if path in ("/favicon.ico", "/favicon.svg"):
            self._favicon()
            return
        if path == "/logout":
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            morsel = cookie.get("sid")
            if morsel:
                revoke_token(morsel.value)
            query = parse_qs(urlparse(self.path).query)
            requested_next = safe_next((query.get("next") or [""])[0]) if query.get("next") else ""
            self._redirect(requested_next or "/login", "sid=; Path=/; Max-Age=0")
            return
        if path == "/login":
            query = parse_qs(urlparse(self.path).query)
            nxt = safe_next((query.get("next") or [""])[0])
            # 登录失败/被限流时不直接回 200 页面，而是 303 回登录页并带个错误码：
            # 这样地址栏里 next 还在，刷新不会突然变回另一套登录页
            code = str((query.get("e") or [""])[0])
            error = ""
            if code == "bad":
                error = "账号或密码不正确"
                # k = 还能再错几次。剩一两次就提前说一声，
                # 免得用户下一次输错突然被锁 10 分钟、不知道发生了什么。
                try:
                    left = int((query.get("k") or [""])[0])
                except ValueError:
                    left = -1
                if left == 1:
                    error += "，还可以再试 1 次，再错就要等 10 分钟"
                elif left == 2:
                    error += "，还可以再试 2 次"
            elif code == "blocked":
                try:
                    minutes = max(1, min(60, int((query.get("m") or ["1"])[0])))
                except ValueError:
                    minutes = 1
                error = "登录失败次数太多了，请等 %d 分钟后再试" % minutes
            elif code == "regclosed":
                error = "注册已经关闭了，找管理员开一个账号"
            # u = 上一次输的登录名，回填进表单
            self._html(render_login(error, nxt, (query.get("u") or [""])[0]))
            return
        if path == "/register":
            if not register_allowed():
                # 地址栏要跟着真实状态走：注册关了就回登录页，别停在 /register 上
                self._redirect("/login?e=regclosed")
                return
            self._html(render_register())
            return
        if not self._authed():
            # 直接访问 /admin 的话，把人送到"管理员登录页"而不是普通登录页
            self._redirect("/login?next=/admin" if path in ("/admin", "/admin/") else "/login")
            return
        if path in ("/", "/index.html"):
            self._html(page_with_limits(INDEX_HTML))
        elif path in ("/admin", "/admin/"):
            # 新的管理员入口：只有管理员进得来；普通用户回自己的控制台
            if not self._is_admin():
                cookie = SimpleCookie(self.headers.get("Cookie", ""))
                morsel = cookie.get("sid")
                if morsel:
                    revoke_token(morsel.value)
                self._redirect("/login?next=/admin", "sid=; Path=/; Max-Age=0")
                return
            self._html(page_with_limits(ADMIN_HTML))
        elif path == "/api/config":
            self._json(self.current_config())
        elif path == "/api/state":
            if self._deny():
                return
            query = parse_qs(urlparse(self.path).query)
            uid = str((query.get("unique_id") or [""])[0])
            self._json({"browser": browser.snapshot_for(self._scopes(), uid), "run": runner.snapshot()})
        elif path == "/api/status":
            self._json(self.status_payload())
        elif path == "/api/cookie":
            # 「一键复制 Cookie」按钮点下去才按需读一次：
            # 不塞进 /api/config 那种每秒轮询的接口里，少一分凭证被反复搬运的风险。
            query = parse_qs(urlparse(self.path).query)
            unique_id = str((query.get("unique_id") or [""])[0]).strip()
            if not unique_id:
                unique_id = str(self.my_account().get("unique_id") or "")
            if not unique_id:
                self._json({"ok": False, "error": "先填好「抖音号」并保存，再来复制 Cookie"}, 403)
                return
            # _owns() 对管理员恒为真，普通用户只能拿自己名下的号
            if self._deny_other(unique_id):
                return
            cookies = load_cookies(unique_id)
            if not cookies:
                self._json(
                    {
                        "ok": False,
                        "error": "「%s」还没保存过 Cookie：先点「开始授权」用手机号登录" % unique_id,
                    },
                    404,
                )
                return
            account = next(
                (t for t in load_tasks() if str(t.get("unique_id") or "") == unique_id), {}
            )
            # 存的本来就是 Cookie-Editor 那种 JSON 数组，原样导出就能再粘回「Cookie JSON」框
            self._json(
                {
                    "ok": True,
                    "unique_id": unique_id,
                    "username": str(account.get("username") or ""),
                    "cookie_count": len(cookies),
                    "cookie_text": json.dumps(cookies, ensure_ascii=False, separators=(",", ":")),
                }
            )
        elif path == "/api/friends":
            query = parse_qs(urlparse(self.path).query)
            uid = str((query.get("unique_id") or [""])[0]).strip()
            if uid and self._deny_other(uid):
                return
            self._json(self.friend_payload(uid))
        elif path == "/api/users":
            if self._deny():
                return
            self._json({"users": user_overview(), "free": unowned_accounts()})
        elif path == "/api/subscription":
            self._json(subscription_info(self._user()))
        elif path == "/api/redeem/codes":
            if self._deny():
                return
            self._json({"codes": redeem_code_overview()})
        elif path == "/api/mobile/notifications":
            self._json({"runs": self.visible_notification_runs()})
        elif path == "/api/webpush/vapid-key":
            try:
                self._json({"ok": True, "public_key": webpush_vapid_keys()["public_key"]})
            except RuntimeError as error:
                self._json({"ok": False, "error": str(error)}, 503)
        elif path == "/api/sends":
            # limit 只是"这次想要几条"，真正的上限由角色决定：
            # 管理员最多 100 条，普通用户最多 2 条。不传 limit 就只回 2 条 ——
            # 主控制台每 4 秒轮询一次，不能每次都把 100 条的包拖下来。
            query = parse_qs(urlparse(self.path).query)
            try:
                want = int((query.get("limit") or ["0"])[0])
            except ValueError:
                want = 0
            role_cap = SEND_MAX_ADMIN if self._is_admin() else SEND_MAX_USER
            cap = min(role_cap, want) if want > 0 else SEND_MAX_USER
            try:
                stamp = SEND_LOG.stat().st_mtime
            except Exception:
                stamp = 0
            self._json(
                {
                    "runs": self.visible_sends(cap),
                    "mtime": stamp,
                    "max": role_cap,
                    "stored": len(self.visible_sends(SEND_STORE_MAX)),
                }
            )
        elif path == "/api/shot":
            query = parse_qs(urlparse(self.path).query)
            name = (query.get("name") or [""])[0]
            if not re.fullmatch(r"[0-9A-Za-z_\u4e00-\u9fff.-]{1,80}", name or ""):
                self._text("Not Found", 404)
                return
            if not self._is_admin() and not self._shot_is_mine(name):
                self._text("Not Found", 404)
                return
            shot_path = SHOT_DIR / name
            if not shot_path.is_file() or shot_path.resolve().parent != SHOT_DIR.resolve():
                self._text("Not Found", 404)
                return
            data = shot_path.read_bytes()
            self.send_response(200)
            self._sec_headers()
            self.send_header("Content-Type", image_mime(data))
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        elif path == "/api/qr":
            # 授权浏览器的二维码。归属按"这台浏览器是给哪个号授权的"判断，
            # 不是按当前画面归属：二维码是这台浏览器产出的
            query = parse_qs(urlparse(self.path).query)
            uid = str((query.get("unique_id") or [""])[0])
            if not self._browser_owner_ok(None, query):
                self._json({"ok": False, "error": "当前二维码不属于你名下的抖音号"}, 403)
                return
            image = browser.qr_image(uid, self._scopes())
            if not image:
                self._text("还没有二维码", 404)
                return
            self.send_response(200)
            self._sec_headers()
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(image)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(image)
        elif path == "/api/smsproof":
            # 「把码填进抖音那一刻」的定格图。归属判断和二维码一样：
            # 这台授权浏览器归谁的，画面就归谁。
            query = parse_qs(urlparse(self.path).query)
            uid = str((query.get("unique_id") or [""])[0])
            if not self._browser_owner_ok(None, query):
                self._json({"ok": False, "error": "这张画面不属于你名下的抖音号"}, 403)
                return
            image = browser.sms_proof_image(uid, self._scopes())
            if not image:
                self._text("还没有填码那一刻的画面", 404)
                return
            self.send_response(200)
            self._sec_headers()
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(image)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(image)
        elif path == "/api/logs":
            self._text(self.visible_logs())
        elif path == "/api/screenshot":
            query = parse_qs(urlparse(self.path).query)
            if not self._shot_allowed(None, query):
                self.send_response(HTTPStatus.NO_CONTENT)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            sess = self._my_session(None, query)
            image = sess.image() if sess is not None else current_frame()[0]
            if not image:
                self.send_response(HTTPStatus.NO_CONTENT)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(image)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(image)
        else:
            self._text("Not Found", 404)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/login":
            form = self._form()
            user = str(form.get("username", "")).strip()
            password = str(form.get("password", ""))
            nxt = safe_next(form.get("next") or "")
            ip = self._client_ip()
            # 失败/被挡住时都要回到登录页：把登录名带回去回填，省得用户再打一遍
            back = "/login?next=%s&u=%s" % (quote(nxt, safe=""), quote(_short_user(user), safe=""))
            left = login_block_left(ip, user)
            if left:
                self._redirect("%s&e=blocked&m=%d" % (back, max(1, left // 60 + 1)))
                return
            token = ""
            admin_login = nxt in ("/admin", "/admin/")
            if verify_admin(user, password):
                token = make_token(user, "", True, admin_key())
                # 管理员不是注册用户，自己的来源 IP 单独记
                record_admin_login(ip)
            elif not admin_login:
                found = verify_user(user, password, ip)
                if found:
                    token = make_token(found["user"], "", False, found.get("key", ""))
            if token:
                login_ok(ip, user)
                # 从「管理控制台」进来的就直接回管理控制台
                self._redirect(nxt, self._sid_cookie(token))
            else:
                login_failed(ip, user)
                time.sleep(LOGIN_FAIL_DELAY)  # 让暴力破解变慢
                # 这一次刚好踩到上限（第 5 次错）就直接说「等 M 分钟」，
                # 别先回一句「密码不正确」、下一次才告诉人已经被锁了
                wait = login_block_left(ip, user)
                if wait:
                    self._redirect("%s&e=blocked&m=%d" % (back, max(1, wait // 60 + 1)))
                else:
                    self._redirect("%s&e=bad&k=%d" % (back, login_tries_left(ip, user)))
            return
        if path == "/register":
            if not register_allowed():
                self._redirect("/login?e=regclosed")
                return
            # 面板是公网的：注册也限速，不然别人可以拿脚本成批建号
            reg_ip = self._client_ip()
            wait = register_block_left(reg_ip)
            if wait:
                self._html(render_register("注册请求太频繁了，请等 %d 分钟后再试" % max(1, wait // 60 + 1)))
                return
            note_register_attempt(reg_ip)
            form = self._form()
            result = create_user(
                form.get("username", ""), form.get("password", ""), form.get("password2"),
                form.get("phone", ""), require_phone=True,
            )
            if not result.get("ok"):
                self._html(render_register(result.get("error", "注册失败")))
                return
            token = make_token(result["user"], "", False, result.get("key", ""))
            self._redirect("/", self._sid_cookie(token))
            return
        if not self._authed():
            self._json({"ok": False, "error": "未登录"}, 401)
            return
        try:
            self._dispatch(path)
        except BodyTooLarge:
            self._json({"ok": False, "error": "请求内容太大了，已被拒绝"}, 413)
        except Exception as error:
            # 完整堆栈写进日志，回给客户端的只说"出错了"，不泄露内部细节
            log_force("未捕获异常", "%s %s\n%s" % (path, error, traceback.format_exc()))
            self._json({"ok": False, "error": "服务器内部出错了（详情已记到日志）"}, 500)

    def _reply(self, ok: bool, message: str, **extra) -> None:
        payload = {"ok": ok}
        payload["message" if ok else "error"] = message
        payload.update(extra)
        self._json(payload)

    def _dispatch(self, path: str) -> None:
        if path == "/api/webpush/status":
            if not self._same_origin_request():
                self._json({"ok": False, "error": "请求来源无效，请刷新后再试"}, 403)
                return
            try:
                payload = self._body()
                endpoint = str(payload.get("endpoint") or "")
                _webpush_endpoint_origin(endpoint)
                subscriptions = _clean_push_subscriptions(WEBPUSH_SUBSCRIPTIONS_STORE.read())["users"]
                active = any(
                    item.get("endpoint") == endpoint
                    for item in subscriptions.get(self._user(), [])
                )
            except ValueError as error:
                self._json({"ok": False, "error": str(error)}, 400)
                return
            self._json({"ok": True, "active": active})
        elif path == "/api/webpush/subscription":
            if not self._same_origin_request():
                self._json({"ok": False, "error": "请求来源无效，请刷新后再试"}, 403)
                return
            try:
                payload = self._body()
                webpush_vapid_keys()
                result = save_webpush_subscription(self._user(), payload)
            except RuntimeError as error:
                self._json({"ok": False, "error": str(error)}, 503)
                return
            except (ValueError, TypeError, KeyError) as error:
                self._json({"ok": False, "error": str(error) or "设备订阅信息无效"}, 400)
                return
            self._json(result, 200 if result.get("ok") else 409)
        elif path == "/api/webpush/unsubscribe":
            if not self._same_origin_request():
                self._json({"ok": False, "error": "请求来源无效，请刷新后再试"}, 403)
                return
            try:
                payload = self._body()
                endpoint = str(payload.get("endpoint") or "")
                if not endpoint:
                    raise ValueError("设备订阅地址为空")
                remove_webpush_subscription(self._user(), endpoint)
            except ValueError as error:
                self._json({"ok": False, "error": str(error)}, 400)
                return
            self._json({"ok": True})
        elif path == "/api/schedule/check":
            payload = self._body()
            error = self._config_guard(payload)
            if error:
                self._json({"ok": False, "error": error}, 403)
                return
            result = schedule_check_payload(payload)
            if not self._is_admin():
                result["conflicts"] = [
                    {"time": x["time"], "other_time": x["other_time"],
                     "estimate_minutes": x["estimate_minutes"]}
                    for x in result.get("conflicts", [])
                ]
                result["occupied"] = [
                    {"time": x["time"], "estimate_minutes": x["estimate_minutes"]}
                    for x in result.get("occupied", [])
                ]
            self._json(result)
        elif path == "/api/friends/refresh":
            payload = self._body()
            unique_id = str(payload.get("unique_id") or "").strip()
            if not unique_id:
                self._json({"ok": False, "error": "先填好抖音号再来拉好友"}, 400)
                return
            if self._deny_other(unique_id):
                return
            ok, message = friend_scanner.start(unique_id)
            self._reply(ok, message)
        elif path == "/api/config":
            payload = self._body()
            error = self._config_guard(payload)
            if error:
                self._json({"ok": False, "error": error}, 403)
                return
            result = self.save_config(payload)
            if result.get("ok") and not self._is_admin():
                # 普通用户自己建的抖音号，保存成功就自动绑到自己名下
                uid = str(result.get("unique_id") or "").strip()
                old = str(payload.get("orig_unique_id") or "").strip()
                if old and old != uid:
                    unbind_account(self._user(), old)
                if uid:
                    bind_account(self._user(), uid, silent=True)
            self._json(result)
        elif path == "/api/account/delete":
            payload = self._body()
            unique_id = str(payload.get("unique_id") or "")
            if self._deny_other(unique_id):
                return
            result = delete_account(unique_id)
            if result.get("ok"):
                drop_account_from_owners(unique_id)
            self._json(result)
        elif path == "/api/users":
            if self._deny():
                return
            self._json({"users": user_overview(), "free": unowned_accounts()})
        elif path == "/api/user/bind":
            if self._deny():
                return
            payload = self._body()
            name = str(payload.get("name") or "")
            uid = str(payload.get("unique_id") or "")
            result = bind_account(name, uid, force=True)
            # 普通用户只能有一个抖音号：分配新的时候自动把旧的解绑，
            # 不然管理员一分配就变成了"名下两个号"，跟限额自相矛盾。
            if result.get("ok") and name and name != PANEL_USERNAME:
                for other in list(user_accounts(name)):
                    if str(other) != uid:
                        unbind_account(name, other)
                        result["message"] = (result.get("message") or "已分配") + "；同时解绑了旧号「%s」" % other
            self._json(result)
        elif path == "/api/redeem":
            payload = self._body()
            self._json(redeem_code(self._user(), str(payload.get("code") or "")))
        elif path == "/api/redeem/generate":
            if self._deny():
                return
            payload = self._body()
            self._json(generate_redeem_codes(str(payload.get("days") or ""), payload.get("count", 1), self._user()))
        elif path == "/api/user/grant":
            if self._deny():
                return
            payload = self._body()
            self._json(grant_user_duration(str(payload.get("name") or ""), str(payload.get("days") or ""), self._user()))
        elif path == "/api/user/unbind":
            if self._deny():
                return
            payload = self._body()
            self._json(
                unbind_account(str(payload.get("name") or ""), str(payload.get("unique_id") or ""))
            )
        elif path == "/api/user/password":
            if self._deny():
                return
            payload = self._body()
            self._json(
                set_user_password(str(payload.get("name") or ""), str(payload.get("password") or ""))
            )
        elif path == "/api/user/delete":
            if self._deny():
                return
            payload = self._body()
            self._json(delete_user(str(payload.get("name") or "")))
        elif path == "/api/global/save":
            if self._deny():
                return
            self._json(save_global_defaults(self._body()))
        elif path == "/api/global/apply":
            if self._deny():
                return
            self._json(push_global_to_accounts(self._body()))
        elif path == "/api/notice/save":
            if self._deny():
                return
            self._json(save_notice(self._body()))
        elif path == "/api/message/send":
            if self._deny():
                return
            self._json(send_message(self._body(), self._user()))
        elif path == "/api/message/delete":
            if self._deny():
                return
            self._json(delete_message(self._body()))
        elif path == "/api/message/read":
            # 任何登录用户都能调；「我是谁」只认会话，请求体里只管挑哪几条
            self._json(mark_message_read(self._body(), self._user()))
        elif path == "/api/register/allow":
            if self._deny():
                return
            payload = self._body()
            allow = bool(payload.get("allow"))
            save_state({"allow_register": allow})
            log_force("注册开关", "打开" if allow else "关闭")
            self._json(
                {
                    "ok": True,
                    "allow": allow,
                    "message": "已%s别人自己注册" % ("打开" if allow else "关闭"),
                }
            )
        elif path == "/api/user/create":
            if self._deny():
                return
            payload = self._body()
            self._json(
                create_user(str(payload.get("name") or ""), str(payload.get("password") or ""))
            )
        elif path == "/api/admin/password":
            if self._deny():
                return
            payload = self._body()
            if not verify_admin(self._user(), str(payload.get("old") or "")):
                self._json({"ok": False, "error": "现在的密码不对"}, 403)
                return
            result = set_admin_password(
                str(payload.get("password") or ""), str(payload.get("again") or "")
            )
            if result.get("ok"):
                ip = self._client_ip()
                login_ok(ip, self._user())
                # 密码换了，旧登录态作废：直接给当前浏览器换一张新的通行证
                token = make_token(PANEL_USERNAME, "", True, admin_key())
                self._json(
                    result, 200, self._sid_cookie(token)
                )
            else:
                self._json(result)
        elif path == "/api/me/password":
            payload = self._body()
            self._json(
                change_own_password(
                    self._user(),
                    str(payload.get("old") or ""),
                    str(payload.get("password") or ""),
                    str(payload.get("again") or ""),
                )
            )
        elif path == "/api/run":
            payload = self._body()
            want = str(payload.get("unique_id") or "").strip()
            scopes = self._scopes()
            if scopes is not None and not scopes:
                self._json({"ok": False, "error": "你还没有绑定抖音号，先建一个并授权登录"}, 403)
                return
            if want:
                # 只跑指定的这一个号（管理员在管理控制台里点某一行「运行」）
                if self._deny_other(want):
                    return
                if want not in {str(t.get("unique_id") or "") for t in load_tasks()}:
                    self._json({"ok": False, "error": "没有「%s」这个抖音号" % want}, 404)
                    return
                ok, message = runner.start(want)
            else:
                ok, message = runner.start("" if scopes is None else ",".join(scopes))
            self._reply(ok, message)
        elif path == "/api/check/all":
            # 一键检测所有账号：服务端排队逐个跑（并发开浏览器内存扛不住）。
            # 账号范围跟着调用者走：管理员 = 全部，普通用户 = 自己名下的。
            scopes = self._scopes()
            uids = []
            for task in load_tasks():
                uid = str(task.get("unique_id") or "")
                if not uid:
                    continue
                if scopes is not None and uid not in scopes:
                    continue
                uids.append(uid)
            ok, message = checker.start_all(uids)
            self._reply(ok, message)
        elif path == "/api/check":
            payload = self._body()
            unique_id = str(payload.get("unique_id") or "").strip()
            if not unique_id:
                # 以前这里会偷偷挑一个账号去检测；现在必须明确说检测哪个，
                # 免得点错页面顺手把别人的号打开了。
                self._json({"ok": False, "error": "请先选好要检测的抖音号（unique_id 不能为空）"}, 400)
                return
            if self._deny_other(unique_id):
                return
            ok, message = checker.start(unique_id)
            self._reply(ok, message)
        elif path == "/api/check/stop":
            # 检测没在跑的时候 unique_id 是空的：这时谁点都算「没东西可停」，直接回 OK。
            # 拿空串去做归属判断的话，普通用户点一下会收到「这个抖音号不在你的名下」的怪报错
            was_batch = bool((checker.snapshot().get("batch") or {}).get("running"))
            running_uid = str(checker.unique_id or "")
            # 批量检测正在跑的时候 unique_id 可能落在别的号上（刚跑完上一个，
            # 队列里排的还有别人的号）—— 只按"当前这个号"判归属，管理员之外的
            # 人也能停掉自己触发的批量
            if running_uid and not was_batch and self._deny_other(running_uid):
                return
            checker.stop()
            self._reply(True, "已请求停止批量检测，本轮跑完就收工" if was_batch else "已请求停止检测")
        elif path == "/api/browser/start":
            payload = self._body()
            unique_id = str(payload.get("unique_id") or "").strip()
            if not self._is_admin():
                # 前端没指定就用名下第一个；无论哪种情况，username 都从
                # 「真正要授权的那个号」取，不能拿名下第一个账号的名字硬套到别的号上，
                # 否则扫码成功后 Cookie 会被安上一个错的名字
                if not unique_id:
                    unique_id = str(self.my_account().get("unique_id") or "")
                target = next(
                    (t for t in self.my_accounts() if str(t.get("unique_id") or "") == unique_id),
                    {},
                )
                payload["username"] = str(target.get("username") or unique_id)
            if not unique_id:
                self._json({"ok": False, "error": "请先填写并保存「抖音号」，再点开始授权"}, 403)
                return
            if not self._is_admin() and not self._owns(unique_id):
                # 以前这里会把"已经存在、还没人认领"的抖音号自动绑给点按钮的人，
                # 结果任何注册用户只要猜到号名就能把别人的号抢到自己名下（连带能开他的浏览器）。
                # 现在只能由管理员在「用户管理」里分配。
                known = {str(t.get("unique_id") or "") for t in load_tasks()}
                holder = owner_of(unique_id)
                if holder:
                    hint = "它现在绑在「%s」名下，让管理员改绑" % holder
                elif unique_id in known:
                    hint = "它已经在系统里了，但还没分配给你。让管理员在「用户管理」里分配给你，不能自己抢"
                else:
                    hint = "先点上面的「保存配置」把它建出来（要填目标好友）"
                self._json(
                    {"ok": False, "error": "「%s」还不在你名下：%s" % (unique_id, hint)}, 403
                )
                return
            payload["unique_id"] = unique_id
            ok, message = browser.start(unique_id, payload.get("username", ""))
            self._reply(ok, message)
        elif path.startswith("/api/browser/"):
            payload = self._body()
            sess = self._my_session(payload)
            if not self._shot_allowed(payload):
                self._json({"ok": False, "error": "当前画面不属于你名下的抖音号"}, 403)
                return
            self._browser_command(path, payload, sess)
        elif path == "/api/auth/phone":
            payload = self._body()
            if not self._browser_owner_ok(payload):
                self._json({"ok": False, "error": "当前授权浏览器不属于你名下的抖音号"}, 403)
                return
            sess = self._my_session(payload)
            if sess is None or not sess.running():
                self._json({"ok": False, "error": "授权浏览器没在运行：先点「开始授权」"}, 409)
                return
            phone = re.sub(r"\D", "", str(payload.get("phone") or ""))
            if not re.fullmatch(r"1\d{10}", phone):
                self._json({"ok": False, "error": "请填 11 位手机号"}, 400)
                return
            browser.send("phone", unique_id=str(payload.get("unique_id") or ""), scopes=self._scopes(), phone=phone)
            self._reply(True, "手机号已提交，正在填进抖音页面…")
        elif path == "/api/auth/sms":
            payload = self._body()
            if not self._browser_owner_ok(payload):
                self._json({"ok": False, "error": "当前授权浏览器不属于你名下的抖音号"}, 403)
                return
            sess = self._my_session(payload)
            if sess is None or not sess.running():
                self._json({"ok": False, "error": "授权浏览器没在运行：先点「开始授权」"}, 409)
                return
            code = str(payload.get("code") or "").strip()
            if not any(ch.isdigit() for ch in code):
                self._json({"ok": False, "error": "请填手机上收到的那串数字验证码"}, 400)
                return
            if len(code) > 12:
                self._json({"ok": False, "error": "验证码没这么长，检查一下是不是粘错了"}, 400)
                return
            browser.send("sms", unique_id=str(payload.get("unique_id") or ""), scopes=self._scopes(), code=code)
            self._reply(True, "验证码已提交，正在填进抖音页面…")
        elif path == "/api/auth/pwd":
            # 二级验证是「验证登录密码」时用：密码只在内存/网络上过一手，
            # 这里不做任何记录 —— 不写日志、不进状态、不回显。
            payload = self._body()
            if not self._browser_owner_ok(payload):
                self._json({"ok": False, "error": "当前授权浏览器不属于你名下的抖音号"}, 403)
                return
            sess = self._my_session(payload)
            if sess is None or not sess.running():
                self._json({"ok": False, "error": "授权浏览器没在运行：先点「开始授权」"}, 409)
                return
            pwd = str(payload.get("password") or "").replace("\r", "").replace("\n", "")
            if not pwd:
                self._json({"ok": False, "error": "密码是空的，检查一下再填"}, 400)
                return
            if len(pwd) > SMS_PWD_MAX:
                self._json({"ok": False, "error": "密码太长了，检查一下是不是粘错了"}, 400)
                return
            browser.send("pwd", unique_id=str(payload.get("unique_id") or ""),
                         scopes=self._scopes(), password=pwd)
            self._reply(True, "密码已提交，正在填进抖音页面…")
        elif path == "/api/auth/text":
            # 通用输入框：用户自己决定填什么。跟验证码/密码通道同样的鉴权，
            # 也同样的「不留痕」—— 不写日志正文、不落盘、不回显。
            payload = self._body()
            if not self._browser_owner_ok(payload):
                self._json({"ok": False, "error": "当前授权浏览器不属于你名下的抖音号"}, 403)
                return
            sess = self._my_session(payload)
            if sess is None or not sess.running():
                self._json({"ok": False, "error": "授权浏览器没在运行：先点「开始授权」"}, 409)
                return
            anytext = str(payload.get("text") or "").replace("\r", "").replace("\n", "")
            if not anytext:
                self._json({"ok": False, "error": "框里是空的，先打字再点「提交」"}, 400)
                return
            if len(anytext) > TEXT_MAX:
                self._json({"ok": False, "error": "内容太长了（上限 %d 个字）" % TEXT_MAX}, 400)
                return
            browser.send("text", unique_id=str(payload.get("unique_id") or ""),
                         scopes=self._scopes(), text=anytext)
            self._reply(True, "已提交，正在敲进抖音页面…")
        elif path == "/api/force/stop":
            if self._deny():
                return
            payload = self._body()
            self._json(force_stop(str(payload.get("reason") or "面板按钮")))
        elif path == "/api/force/restart":
            if self._deny():
                return
            self._json(force_restart())
        else:
            self._json({"ok": False, "error": "未知接口"}, 404)

    def _browser_command(self, path: str, payload: dict = None, sess=None) -> None:
        payload = payload if isinstance(payload, dict) else {}
        uid = str(payload.get("unique_id") or "")
        if path == "/api/browser/stop":
            if sess is not None:
                sess.stop()
            else:
                browser.stop(uid, self._scopes())
            self._reply(True, "已请求停止")
        elif path == "/api/browser/click":
            browser.send("click", unique_id=uid, scopes=self._scopes(), x=payload.get("x", 0), y=payload.get("y", 0))
            self._reply(True, "已点击")
        elif path == "/api/browser/wheel":
            browser.send("wheel", unique_id=uid, scopes=self._scopes(), dy=payload.get("dy", 0))
            self._reply(True, "已滚动")
        elif path == "/api/browser/type":
            browser.send("type", unique_id=uid, scopes=self._scopes(), text=payload.get("text", ""))
            self._reply(True, "已输入")
        elif path == "/api/browser/press":
            browser.send("press", unique_id=uid, scopes=self._scopes(), key=payload.get("key", "Enter"))
            self._reply(True, "已按键")
        elif path == "/api/browser/goto":
            browser.send("goto", unique_id=uid, scopes=self._scopes(), url=CHAT_URL)
            self._reply(True, "已返回聊天页")
        else:
            self._json({"ok": False, "error": "未知接口"}, 404)

    def friend_payload(self, unique_id: str) -> dict:
        """当前这个号的好友列表：正在拉就报进度，拉完了给最新结果。

        内存里的结果优先（刚拉完还没落盘的也在），否则回落到上次存下来的那份。
        """
        uid = str(unique_id or "").strip()
        snap = friend_scanner.snapshot()
        if uid and snap.get("unique_id") == uid and (snap.get("running") or snap.get("finished_at")):
            return {
                "ok": True,
                "unique_id": uid,
                "running": bool(snap.get("running")),
                "error": str(snap.get("error") or ""),
                "progress": str(snap.get("progress") or ""),
                "friends": list(snap.get("friends") or []),
                "targets": list(snap.get("targets") or []),
                "at": "",
            }
        account = load_accounts().get(uid) or {}
        saved = account.get("friend_list")
        return {
            "ok": True,
            "unique_id": uid,
            "running": False,
            "error": "",
            "progress": "",
            "friends": [str(x) for x in (saved if isinstance(saved, list) else [])],
            "targets": [str(x) for x in (account.get("targets") or [])],
            "at": str(account.get("friend_list_at") or ""),
        }

    def status_payload(self) -> dict:
        # 自动准备二维码已经挪到 _auto_auth_loop 后台心跳里做了；
        # 这里不再顺手调 _scopes()，省得每次轮询都白读一次用户表
        state = load_state()
        saved_at = state.get("saved_at") or {}
        checks = state.get("checks") or {}
        # 执行任务途中发现的登录失效（每次轮询都读一遍，任务一失效徽章就跟着变）
        login_failures = task_login_failures()
        tasks = load_tasks()
        is_admin = self._is_admin()
        me = self._user()
        access = subscription_info(me)
        # 普通用户首页直接展示今天自己的发送结果；管理员继续在完整记录页查看全站记录。
        today_send = None
        if not is_admin:
            today = now_text()[:10]
            today_runs = [
                run for run in self.visible_sends(SEND_MAX_USER)
                if str(run.get("at") or "").startswith(today)
            ]
            latest = today_runs[0] if today_runs else None
            today_send = {
                "date": today,
                "record": ({
                    "at": str(latest.get("at") or ""),
                    "account": str(latest.get("account") or ""),
                    "status": str(latest.get("status") or ""),
                    "detail": str(latest.get("detail") or ""),
                } if latest else None),
            }
        scopes = self._scopes()  # None = 管理员（全部）
        login_of = {}
        for name, item in load_users().items():
            if isinstance(item, dict):
                for uid in (item.get("accounts") or []):
                    login_of[str(uid)] = str(name)
        for uid in admin_owned():
            login_of[uid] = PANEL_USERNAME
        if scopes is not None:
            tasks = [t for t in tasks if str(t.get("unique_id") or "") in scopes]
        primary = tasks[0] if tasks else {}
        unique_id = str(primary.get("unique_id", "") or "")
        cookies = load_cookies(unique_id) if unique_id else []

        values = parse_env()

        accounts = []
        for task in tasks:
            uid = str(task.get("unique_id") or "")
            item = load_cookies(uid)
            targets = [str(t) for t in (task.get("targets") or [])]
            times = [schedule_spec_canon(t) for t in (task.get("times") or [])]
            times = [t for t in times if t]
            accounts.append(
                {
                    "username": str(task.get("username") or ""),
                    "unique_id": uid,
                    "targets": targets,
                    "times": times,
                    "own_times": bool(times),
                    "times_today": schedule_specs_today(times),
                    "times_random": any(schedule_spec_is_random(t) for t in times),
                    "estimate_minutes": _estimate_task_minutes(task, values),
                    "ready": bool([t for t in targets if t.strip()]),
                    "has_cookie": bool(item),
                    "cookie_count": len(item),
                    "saved_at": saved_at.get(uid, ""),
                    "check": real_check(checks, uid),
                    "login_user": login_of.get(uid, ""),
                    "mine": True if scopes is None else (uid in scopes),
                    "task_login_failed": bool(
                        task_login_failure_for(uid, login_failures, checks, saved_at)
                    ),
                    "task_login_at": str(
                        (task_login_failure_for(uid, login_failures, checks, saved_at) or {}).get("at") or ""
                    ),
                    "task_login_detail": str(
                        (task_login_failure_for(uid, login_failures, checks, saved_at) or {}).get("detail") or ""
                    ),
                    # 这个号自己的消息模板/发送间隔/一言类型（空 = 用全局默认）
                    "settings": account_settings(task),
                }
            )

        # 授权状态按"发起这次请求的人"给：看自己那个会话；没有自己的会话就给一份干净的空状态
        # （绝不能让普通用户看到别人的二维码/画面）。管理员看的是第一个在跑的会话。
        auth_state = browser.snapshot_for(scopes)
        shot_owner = str(getattr(browser.pick_for(scopes) or browser.spare, "owner", "") or "")
        shot_mine = is_admin or not shot_owner or (shot_owner in (scopes or []))
        if not shot_mine:
            # 别人的浏览器：画面、二维码、二级验证码一律不下发。
            # 以前只清了画面，二维码字段还照发，界面上就出现一张 403 打不开的图，
            # 旁边还写着「用手机抖音扫下面这张二维码」—— 自相矛盾，直接全清掉。
            auth_state = dict(
                auth_state,
                has_image=False,
                live=False,
                has_qr=False,
                qr_hash="",
                verify_qr=False,
                verify_hint="",
                message="另一个账号正在使用这台浏览器，画面暂时看不到",
            )
        elif not auth_state.get("has_image"):
            auth_state = dict(auth_state, has_image=False, live=False)
        sessions = browser.list_for(scopes)
        checker_state = checker.snapshot()
        # 批量检测的明细里带着抖音号列表（current / skipped），
        # 一并写进快照方便界面显示；普通用户看不到别人的号，所以要按归属清掉。
        try:
            checker_state["batch_last"] = state.get("check_all") or {}
        except Exception:
            checker_state["batch_last"] = {}
        if not is_admin and str(checker_state.get("unique_id") or "") not in (scopes or []):
            # 别的账号正在检测，只保留"有人在跑"，不暴露是谁
            checker_state = dict(checker_state, unique_id="__other__", message="",
                                 state="idle", batch={}, batch_last={})
        relogin = load_relogin() or None
        if relogin and not is_admin:
            name = str(primary.get("username") or "")
            if name and str(relogin.get("account") or "") != name:
                relogin = None
        runner_state = runner.snapshot()
        if not is_admin:
            # The runner is shared by the panel process. A user's dashboard must
            # not display another account's result or the panel's global history.
            visible_ids = set(str(x) for x in (scopes or []) if str(x))
            run_ids = set(x.strip() for x in str(runner_state.get("only") or "").split(",") if x.strip())
            if not visible_ids.intersection(run_ids):
                runner_state = {
                    "running": False,
                    "returncode": None,
                    "started_at": None,
                    "stale": None,
                    "stuck": False,
                    "only": "",
                }

        occupied_rows = [row for row in _schedule_rows() if row["unique_id"] != unique_id]
        schedule_recommended = _recommended_schedule_time(
            load_tasks(), unique_id,
            primary.get("targets") if isinstance(primary, dict) else [],
            (primary.get("settings") or {}).get("delay_max", 0)
            if isinstance(primary, dict) else 0,
            rows=occupied_rows,
        )
        return {
            "me": me,
            "is_admin": is_admin,
            "subscription": access,
            "today_send": today_send,
            # 管理员自己的「最近从哪登录」；普通用户不需要，别白给
            "admin_login": (load_state().get("admin_login") or {}) if is_admin else {},
            "allow_register": register_allowed(),
            "scope": "" if is_admin else (scopes[0] if scopes else ""),
            "my_ids": [] if is_admin else list(scopes or []),
            "username": str(primary.get("username") or ""),
            "unique_id": unique_id,
            "targets": [str(t) for t in (primary.get("targets") or [])],
            "has_cookie": bool(cookies),
            "cookie_count": len(cookies),
            "saved_at": saved_at.get(unique_id, ""),
            "check": real_check(checks, unique_id),
            "accounts": accounts,
            "schedule": {
                "timezone": values.get("TZ", "Asia/Shanghai"),
                "today": now_text()[:10],
                "occupied": [
                    {"time": row["time"], "estimate_minutes": row["estimate_minutes"]}
                    if not is_admin else {k: row[k] for k in ("time", "username", "unique_id", "estimate_minutes")}
                    for row in occupied_rows
                ],
                "recommended": {"time": schedule_recommended},
            },
            "global": {
                "message_template": values.get("MESSAGE_TEMPLATE", DEFAULT_TEMPLATE),
                "delay_min": values.get("RANDOM_DELAY_MIN", "0"),
                "delay_max": values.get("RANDOM_DELAY_MAX", "0"),
                "tz": values.get("TZ", "Asia/Shanghai"),
                "log_level": values.get("LOG_LEVEL", "INFO"),
                "hitokoto_types": values.get("HITOKOTO_TYPES", ""),
                            },
            # 公告条（管理员可编辑；普通用户只读）
            "notice": notice_payload(),
            # 定向消息：普通用户只拿到自己的收件箱；管理员额外拿到发件箱和已读回执
            "messages": message_payload(me, is_admin),
            "relogin": relogin,
            "auth": auth_state,
            "live": {
                "has_image": bool(auth_state["has_image"]),
                "live": bool(auth_state["live"]),
                "frame_age": auth_state["frame_age"] if shot_mine else None,
                "mine": shot_mine,
                "owner": shot_owner if shot_mine else "__other__",
            },
            "checker": checker_state,
            "runner": runner_state,
            "sessions": sessions,
            "max_sessions": browser.size,
            # Host memory is an operational detail; only the admin panel needs it.
            "mem_available_mb": memory_available_mb() if is_admin else None,
            # 授权槽位占用情况：谁都能看（不含别人的账号名），池满时前端据此提示"还要等多久"
            "pool": {
                "size": browser.size,
                "used": browser.used(),
                "wait_seconds": browser.wait_seconds(),
                "wait_text": fmt_wait(browser.wait_seconds()) if browser.wait_seconds() is not None else "",
            },
            "force_log": load_force_log() if is_admin else [],
        }

    def current_config(self) -> dict:
        values = parse_env()
        tasks = load_tasks()
        if self._is_admin():
            task = tasks[0] if tasks else {}
        else:
            task = self.my_account()
        unique_id = str(task.get("unique_id", "") or "")
        cookies = load_cookies(unique_id) if unique_id else []
        own = account_settings(task)
        return {
            "username": str(task.get("username", "") or ""),
            "unique_id": unique_id,
            "targets": "\n".join(str(t) for t in (task.get("targets") or [])),
            "schedule_times": "\n".join(str(t) for t in (task.get("times") or [])),
            "message_template": own.get("template") or values.get("MESSAGE_TEMPLATE", DEFAULT_TEMPLATE),
            "delay_min": own.get("delay_min") or values.get("RANDOM_DELAY_MIN", "0"),
            "delay_max": own.get("delay_max") or values.get("RANDOM_DELAY_MAX", "0"),
            "tz": values.get("TZ", "Asia/Shanghai"),
            "log_level": values.get("LOG_LEVEL", "INFO"),
            "hitokoto_types": own.get("hitokoto_types") or values.get("HITOKOTO_TYPES", ""),
            "cookie_count": len(cookies) if isinstance(cookies, list) else 0,
        }

    def save_config(self, payload: dict) -> dict:
        unique_id = str(payload.get("unique_id", "")).strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]+", unique_id or ""):
            return {"ok": False, "error": "抖音号只能包含字母、数字、下划线或短横线"}
        # [本地增强] 账号名称可以先不填，默认用抖音号
        username = str(payload.get("username", "")).strip() or unique_id or "账号1"
        # 支持先授权登录、再填写目标好友；没有目标好友的账号不会发送（tasks.py 会跳过）。
        targets = [t.strip() for t in re.split(r"[\r\n,]+", str(payload.get("targets", ""))) if t.strip()]
        try:
            delay_min = int(float(str(payload.get("delay_min", 0) or 0)))
            delay_max = int(float(str(payload.get("delay_max", 0) or 0)))
        except (TypeError, ValueError):
            return {"ok": False, "error": "发送间隔必须是数字（秒）"}
        if not (0 <= delay_min <= 600 and 0 <= delay_max <= 600):
            return {"ok": False, "error": "发送间隔请填 0-600 秒"}
        if delay_max < delay_min:
            delay_min, delay_max = delay_max, delay_min

        # 多账号：这次只改当前选中的账号，其它账号原样保留。
        tasks = load_tasks()
        orig_unique_id = str(payload.get("orig_unique_id", "") or "").strip()
        entry = None
        if orig_unique_id:
            entry = next((t for t in tasks if str(t.get("unique_id") or "") == orig_unique_id), None)
        if entry is None:
            entry = next((t for t in tasks if str(t.get("unique_id") or "") == unique_id), None)
        old_unique_id = str(entry.get("unique_id") or "") if entry else ""
        # 账号名称不能重名：发送记录/截图的归属是按名称区分出来的，重名会串号（看到别人的截图）
        my_shot_name = shot_name_of(username)
        for task in tasks:
            if task is entry:
                continue
            if my_shot_name and my_shot_name == shot_name_of(task.get("username") or task.get("unique_id")):
                return {
                    "ok": False,
                    "error": "账号名称「%s」已经被另一个抖音号用了（名称用来区分截图和记录，不能重名），换一个，或者留空直接用抖音号"
                    % username,
                }
        if entry is None:
            entry = {}
            tasks.append(entry)
        entry["username"] = username
        entry["unique_id"] = unique_id
        entry["targets"] = targets
        if "schedule_times" in payload:
            times, error = _parse_schedule_specs(payload.get("schedule_times"), "发送时间")
            if error:
                return {"ok": False, "error": error}
        else:
            times = [schedule_spec_canon(x) for x in (entry.get("times") or [])]
            times = [x for x in times if x]
        entry["times"] = times

        # 「消息模板 / 发送间隔 / 一言类型」跟着**这个抖音号**走：改哪个号就只影响哪个号。
        # 普通用户只能改自己名下的号（_config_guard 已经拦过），所以怎么改都动不到别人。
        template = str(payload.get("message_template", "")).strip()
        hitokoto = str(payload.get("hitokoto_types", "")).strip()
        kinds = []
        if hitokoto:
            try:
                parsed = json.loads(hitokoto)
            except Exception:
                return {"ok": False, "error": "一言类型必须是 JSON 数组"}
            if not isinstance(parsed, list) or not parsed:
                return {"ok": False, "error": "一言类型必须是非空 JSON 数组"}
            kinds = [str(x) for x in parsed]
        entry["settings"] = {
            "template": template,
            "delay_min": delay_min,
            "delay_max": delay_max,
            "hitokoto_types": kinds,
        }

        mapping = {}
        # 时区和日志级别是整台容器共用的一份（改它会动到所有人的日志设置），
        # 所以照旧只有管理员能改；普通用户提交上来的值直接忽略，.env 原值不动。
        if self._is_admin():
            mapping.update({
                "TZ": str(payload.get("tz", "")).strip() or "Asia/Shanghai",
                "LOG_LEVEL": (str(payload.get("log_level", "")).strip() or "INFO").upper(),
            })
            # 「新账号的默认值」：管理员勾上这个才会把消息模板/发送间隔/一言类型
            # 也写进全局 .env（那是给还没单独设过的号兜底用的）。不勾就只改当前这个号。
            if str(payload.get("save_global", "")).strip().lower() in ("1", "true", "on", "yes"):
                mapping.update({
                    "RANDOM_DELAY_MIN": str(delay_min),
                    "RANDOM_DELAY_MAX": str(delay_max),
                    "MESSAGE_TEMPLATE": template or DEFAULT_TEMPLATE,
                })
                if kinds:
                    mapping["HITOKOTO_TYPES"] = json.dumps(
                        kinds, ensure_ascii=False, separators=(",", ":")
                    )

        cookie_text = str(payload.get("cookie_json", "")).strip()
        imported = None
        if cookie_text:
            try:
                cookies = json.loads(cookie_text)
            except Exception:
                return {"ok": False, "error": "Cookie 必须是合法的 JSON 数组"}
            if not isinstance(cookies, list):
                return {"ok": False, "error": "Cookie 必须是合法的 JSON 数组"}
            # 抖音会给一条 name 为空的垃圾 Cookie，不能因为它把整份 Cookie 判成非法
            cookies = [c for c in cookies if isinstance(c, dict) and str(c.get("name") or "").strip()]
            if not cookies:
                return {"ok": False, "error": "Cookie 数组格式不对，请用 Cookie-Editor 导出的 JSON"}
            imported = cookies

        # 前面全部检查通过，才开始真正落盘（免得报错了却已经把一半配置写进去）
        # 账号列表写回 accounts.json（改了抖音号的话，Cookie 跟着搬，不用重新扫码）
        save_account_list(tasks, old_unique_id, unique_id)
        if imported is not None:
            save_cookies(unique_id, imported)

        update_env(mapping)
        invalidate_shot_cache()
        # 授权浏览器的"我在给哪个账号授权"和"画面归谁"必须是一份数据：
        #   * 这台浏览器就是刚改名的那个账号 -> unique_id 和 owner 一起改成新号。
        #     只改 unique_id 的话，画面归属还挂在老号上，号主自己会被挡在截图外面；
        #   * 它正在给别的账号授权 -> 一个字都不动，否则扫码成功的 Cookie 会存到别人名下；
        #   * 它没在跑 -> 只更新"当前选中的账号"，不动上一张图的归属。
        # 现在可能有多个会话，所以逐个看：给这个号在用的那个会话改名。
        with browser.lock:
            for sess in browser.all():
                with sess.lock:
                    if old_unique_id and sess.unique_id == old_unique_id:
                        sess.unique_id = unique_id
                        sess.owner = unique_id
                        sess.username = username
                    elif not sess.running() and not sess.unique_id:
                        sess.unique_id = unique_id
                        sess.username = username
        # 普通用户提交的「时区 / 日志级别」被忽略了（那两项是整台容器共用的），
        # 这里明确说一句，免得他改了没生效还以为面板坏了
        note = "" if self._is_admin() else "（时区 / 日志级别是所有账号共用的，只有管理员能改，这次没动）"
        return {
            "ok": True,
            "message": "配置已保存（当前共 %d 个账号）%s" % (len(tasks), note),
            "unique_id": unique_id,
            "accounts": len(tasks),
        }



def maybe_auto_authorize(allowed_ids=None) -> None:
    """登录失效后自动把扫码页开好，打开面板就能直接扫。

    allowed_ids 不为 None 时只处理名单里的抖音号（普通用户不该被别人的号
    自动占住浏览器，否则他自己的「开始授权」会一直点不动）。
    """
    # 整段上锁：几个页面同时刷新时，只会有一次真的去准备二维码
    with _AUTO_AUTH_LOCK:
        info = load_relogin()
        if not info:
            return
        marker = str(info.get("at") or "")
        if not marker:
            return
        state = load_state()
        if state.get("auto_auth_for") == marker:
            return
        last = state.get("auto_auth_try") or {}
        try:
            if time.time() - float(last.get("at_ts") or 0) < 60:
                return
        except Exception:
            pass
        if browser.running() or checker.running() or runner.running():
            return
        tasks = load_tasks()
        if allowed_ids is not None:
            tasks = [t for t in tasks if str(t.get("unique_id") or "") in allowed_ids]
        if not tasks:
            return
        # 只认"失效的那个账号"：以前直接拿第一个，多账号时会开错别人的浏览器
        wanted = str(info.get("account") or "").strip()
        picked = None
        if wanted:
            picked = next(
                (
                    t
                    for t in tasks
                    if str(t.get("username") or "") == wanted
                    or str(t.get("unique_id") or "") == wanted
                ),
                None,
            )
            if picked is None:
                return
        else:
            picked = tasks[0]
        unique_id = str(picked.get("unique_id") or "")
        if not unique_id:
            return
        save_state({"auto_auth_try": {"at_ts": time.time(), "at": now_text()}})
        ok, message = browser.start(unique_id, str(picked.get("username") or ""))
        if ok:
            save_state({"auto_auth_for": marker, "auto_auth": {"at": now_text(), "message": "登录失效，已自动准备好二维码"}})


def _auto_auth_loop(stop_event: threading.Event) -> None:
    """后台心跳：定时看看有没有账号登录失效，有就把扫码页准备好。

    以前这事挂在 /api/status 里，谁刷新页面谁顺手干一遍；人一多，几个请求线程
    就一起堵在「开浏览器」上，状态接口越刷越慢。现在统一交给这一个心跳线程。
    """
    while not stop_event.wait(AUTO_AUTH_INTERVAL):
        try:
            maybe_auto_authorize()
        except Exception:
            try:
                log_force("自动准备二维码失败", traceback.format_exc())
            except Exception:
                pass


def _lock_reaper_loop(stop_event: threading.Event) -> None:
    """后台心跳：定时回收「正在发送」这把共享锁。

    以前这把锁只在 /api/status 被轮询时顺带释放（snapshot 里调 _reap）。
    网页一关就没人轮询了，手动运行结束后锁还攥在面板进程手里，
    后续手动运行就会被误判为已有任务。历史上这曾导致一次发送无法启动。
    现在交给一个后台线程定期回收，不依赖有没有人开着页面。
    """
    while not stop_event.wait(LOCK_REAP_INTERVAL):
        try:
            runner.snapshot()   # snapshot 内部会 _reap()，进程结束就把锁还回去
        except Exception:
            try:
                log_force("回收发送锁失败", traceback.format_exc())
            except Exception:
                pass


def _schedule_state_read() -> dict:
    try:
        raw = json.loads(SCHEDULE_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    return {
        "seen": raw.get("seen") if isinstance(raw.get("seen"), dict) else {},
        "pending": raw.get("pending") if isinstance(raw.get("pending"), list) else [],
        "last": raw.get("last") if isinstance(raw.get("last"), dict) else {},
    }


def _schedule_state_write(state: dict) -> None:
    atomic_write(SCHEDULE_STATE_PATH, json.dumps(state, ensure_ascii=False, separators=(",", ":")))


def _schedule_queue_due(day: str, minute: str) -> None:
    tasks = load_tasks()
    owners = _schedule_owner_map()
    due = []
    for row in _schedule_rows(tasks, day, owners):
        if row["time"] == minute:
            due.append({"unique_id": row["unique_id"], "time": minute, "day": day})
    if not due:
        return
    with _SCHEDULE_LOCK:
        state = _schedule_state_read()
        seen = state["seen"]
        # Keep a short duplicate-prevention ledger; older days can no longer be retried.
        cutoff = time.strftime("%Y-%m-%d", time.localtime(time.time() - 3 * 86400))
        state["seen"] = {key: value for key, value in seen.items()
                          if str(value or "")[:10] >= cutoff}
        pending = state["pending"]
        queued = {(str(x.get("day") or ""), str(x.get("time") or ""), str(x.get("unique_id") or ""))
                  for x in pending if isinstance(x, dict)}
        for item in due:
            key = "%s|%s|%s" % (item["day"], item["time"], item["unique_id"])
            tuple_key = (item["day"], item["time"], item["unique_id"])
            if key in state["seen"] or tuple_key in queued:
                continue
            state["seen"][key] = item["day"]
            pending.append(item)
            queued.add(tuple_key)
        # A corrupt or hand-edited file should not grow the queue without a bound.
        state["pending"] = pending[-500:]
        _schedule_state_write(state)


def _pending_schedule_is_current(item: dict, tasks: list, owners: dict) -> bool:
    uid, day = str(item.get("unique_id") or ""), str(item.get("day") or "")
    if not uid or not day or not _schedule_account_active(uid, owners):
        return False
    task = next((x for x in tasks if str(x.get("unique_id") or "") == uid), None)
    if not task or not [x for x in (task.get("targets") or []) if str(x).strip()]:
        return False
    return str(item.get("time") or "") in schedule_specs_today(task.get("times") or [], day)


def _schedule_dispatch_one() -> None:
    # Manual runs, login checks and authorization sessions retain their existing priority.
    if runner.running() or browser.running() or checker.running():
        return
    with _SCHEDULE_LOCK:
        state = _schedule_state_read()
        pending = list(state["pending"])
    if not pending:
        return
    item = pending[0] if isinstance(pending[0], dict) else {}
    tasks = load_tasks()
    owners = _schedule_owner_map()
    if not _pending_schedule_is_current(item, tasks, owners):
        with _SCHEDULE_LOCK:
            state = _schedule_state_read()
            state["pending"] = [x for x in state["pending"] if x != item]
            _schedule_state_write(state)
        return
    uid = str(item.get("unique_id") or "")
    try:
        ok, message = runner.start(uid, source="定时")
    except Exception:
        ok, message = False, "启动定时任务时出错：" + traceback.format_exc()
    if ok:
        with _SCHEDULE_LOCK:
            state = _schedule_state_read()
            state["pending"] = [x for x in state["pending"] if x != item]
            state["last"] = {"unique_id": uid, "time": item.get("time"), "day": item.get("day"),
                              "started_at": now_text(), "message": message}
            _schedule_state_write(state)
        return
    # A manual task or another engine may have won the race; keep this due job queued.
    if any(text in str(message) for text in ("正在", "已有发送", "已经有一个任务")):
        return
    with _SCHEDULE_LOCK:
        state = _schedule_state_read()
        state["pending"] = [x for x in state["pending"] if x != item]
        state["last"] = {"unique_id": uid, "time": item.get("time"), "day": item.get("day"),
                          "started_at": now_text(), "message": str(message)[:300]}
        _schedule_state_write(state)
    try:
        log_force("定时发送启动失败", "%s：%s" % (uid, message))
    except Exception:
        pass


def _schedule_loop(stop_event: threading.Event) -> None:
    """Minute scheduler; due work shares TaskRunner and the interprocess send lock."""
    last_minute = ""
    while not stop_event.wait(SCHEDULE_INTERVAL):
        try:
            current = now_text()
            day, minute = current[:10], current[11:16]
            marker = day + " " + minute
            if marker != last_minute:
                _schedule_queue_due(day, minute)
                last_minute = marker
            _schedule_dispatch_one()
        except Exception:
            try:
                log_force("定时发送调度异常", traceback.format_exc())
            except Exception:
                pass


def ensure_link() -> None:
    try:
        if LINK_PATH.is_symlink():
            return
        if LINK_PATH.exists():
            LINK_PATH.unlink()
        os.symlink(ENV_PATH, LINK_PATH)
    except Exception:
        pass


def main() -> int:
    global SERVER
    # 这个面板手里全是密码和 Cookie：把默认权限收紧成"只有自己能看"，
    # 后面不管谁新写了什么文件，都不会是 -rw-r--r--。
    try:
        os.umask(0o077)
    except Exception:
        pass
    ensure_link()
    if not PANEL_PASSWORD:
        print("PANEL_PASSWORD 未设置，拒绝启动", file=sys.stderr)
        return 1
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    _tighten_log_perms()
    _cleanup_plaintext_leftovers()
    try:
        migrate_legacy_accounts()
    except Exception:
        pass
    try:
        save_account_list(load_tasks())
        old = {k: None for k in ("SCHEDULE_TIMES", "CRON_HOUR", "CRON_MINUTE", "CRON_SECOND", "SEND_LOCK_WAIT", "SEND_AUTH_WAIT") if k in parse_env()}
        if old:
            update_env(old)
    except Exception as error:
        log_force("清理旧自动发送配置失败", type(error).__name__)
    # 后台心跳：登录失效时自动把二维码准备好（不再占用 /api/status 请求线程）
    heartbeat_stop = threading.Event()
    threading.Thread(
        target=_auto_auth_loop, args=(heartbeat_stop,), name="auto-auth", daemon=True
    ).start()
    # 后台回收发送锁：不依赖网页轮询，避免锁长期占用导致后续手动运行被拦截
    threading.Thread(
        target=_lock_reaper_loop, args=(heartbeat_stop,), name="lock-reaper", daemon=True
    ).start()
    threading.Thread(
        target=_schedule_loop, args=(heartbeat_stop,), name="daily-scheduler", daemon=True
    ).start()
    if _webpush_crypto():
        threading.Thread(
            target=_webpush_notify_loop, args=(heartbeat_stop,), name="webpush-notify", daemon=True
        ).start()
    else:
        print("[panel] Web Push 暂不可用：未安装 cryptography 依赖", flush=True)
    SERVER = ThreadingHTTPServer((PANEL_HOST, PANEL_PORT), Handler)
    print("[panel] listening on %s:%d" % (PANEL_HOST, PANEL_PORT), flush=True)
    try:
        SERVER.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        heartbeat_stop.set()
        SERVER.server_close()
    return 0


def _tighten_log_perms() -> None:
    """把日志目录里已经存在的文件统一改成 0600（只有自己可读）。

    【为什么不能无脑 chmod】
    LOG_DIR / SHOT_DIR 实际挂在网络文件系统（COS）上。实测这个挂载：
      - stat / readdir 很便宜：遍历 729 个文件只要 0.22 秒（约 0.3ms/个）；
      - chmod 是一次远程元数据调用，约 60ms/个。
    send-shots 已经攒了 700 多张截图，一次全量 chmod 要 44 秒以上。
    而看门狗判定"连续 3 次 /login 不通就 force-recreate"，窗口只有约 16 秒，
    于是容器每次都在启动途中被杀，陷入"重建 -> 又来不及启动 -> 再重建"的死循环，
    面板再也起不来（只能人工停掉看门狗）。
    所以这里改成：只对权限确实不是 0600 的文件发 chmod。
    进程 umask 是 0o77，新写的文件天生就是 0600，正常启动下这里一个远程写都不会发；
    第一遍只在真的存在历史遗留（比如别人手动拷进来的 0644 文件）时做少量修正。
    """
    for base in (LOG_DIR, SHOT_DIR):
        try:
            entries = os.scandir(base)
        except Exception:
            continue
        with entries:
            for entry in entries:
                try:
                    # 用 lstat，跳过符号链接（原逻辑就是 not p.is_symlink()）
                    info = entry.stat(follow_symlinks=False)
                    # 位运算显式加括号：Python 里 & 的优先级低于 ==，不括会算错
                    if (info.st_mode & 0o170000) != 0o100000:
                        continue
                    if (info.st_mode & 0o777) == 0o600:
                        continue
                    os.chmod(entry.path, 0o600)
                except Exception:
                    continue


def _cleanup_plaintext_leftovers() -> None:
    """启动时清理历史遗留：删掉以前的明文密码文件、抹掉配置里的 pw 字段。

    密码副本功能已经整个删掉了，这里只是把老版本留下的残留清干净。
    """
    legacy = BASE_DIR / "config" / ("panel-" + "passwords.json")
    try:
        if legacy.exists():
            legacy.unlink()
    except Exception:
        pass
    try:
        item = load_admin()
        if isinstance(item, dict) and "pw" in item:
            item.pop("pw", None)
            atomic_write(ADMIN_PATH, json.dumps(item, ensure_ascii=False, indent=2))
    except Exception:
        pass
    try:
        def _drop(state):
            if isinstance(state, dict):
                state.pop("show_passwords", None)
            return state

        STATE_STORE.update(_drop)
    except Exception:
        pass


if __name__ == "__main__":
    raise SystemExit(main())
