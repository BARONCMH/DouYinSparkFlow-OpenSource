import base64
import json
import os
import random
import re
import signal
import subprocess
import sys
import traceback
from uuid import uuid4
from utils.logger import setup_logger
from utils.config import get_config, get_userData
from core.douyin_im import match_contact_name, norm
from core.msg_builder import build_message, build_message_with_openai
from core.browser import get_browser
from playwright.sync_api import Response
import time

config = get_config()
userData = get_userData()

# 这个进程手里有账号 Cookie：默认权限收紧成"只有自己能看"，
# 之后它写出来的日志 / 截图 / 发送记录都会是 0600。
try:
    os.umask(0o077)
except Exception:
    pass

# 多账号：面板手动运行时可通过 RUN_ONLY_ACCOUNTS 指定要运行的账号；
# 没传就按老样子把所有账号都跑一遍。
_only_accounts = (os.environ.get("RUN_ONLY_ACCOUNTS") or "").strip()
if _only_accounts:
    _only_ids = {x.strip() for x in _only_accounts.split(",") if x.strip()}
    _picked = [u for u in userData if str(u.get("unique_id") or "") in _only_ids]
    _missing = sorted(_only_ids - {str(u.get("unique_id") or "") for u in _picked})
    if _missing:
        # 这些账号没配好（多半是还没有 Cookie），本次不跑，也绝不连累别的账号
        print("[douyin] 这些账号本次跳过（缺少 Cookie 或配置）：%s" % ", ".join(_missing))
    userData = _picked

# [本地增强] 只填了抖音号、还没填「目标好友」的账号，是在等用户扫码登录，直接跳过，
# 别让它们去动浏览器、也别在发送记录里写一条"啥也没发"的假成功。
_no_target = sorted({str(u.get("unique_id") or "") for u in userData
                     if not [t for t in (u.get("targets") or []) if str(t).strip()]})
if _no_target:
    print("[douyin] 这些账号还没填目标好友，本次不发送：%s" % ", ".join(_no_target))
    userData = [u for u in userData if str(u.get("unique_id") or "") not in set(_no_target)]

logger = setup_logger(level=config.get("logLevel", "Info"))
userIDDict = {}

# [本地增强] 每个抖音号可以有自己的「消息模板 / 发送间隔 / 一言类型」：
# 这些设置跟着账号存在 accounts.json 里，面板启动发送引擎时会把它们塞进 TASKS。
# utils.config.get_userData() 只取了账号名和目标好友，所以这里按 unique_id 补一张表。
# 没单独配的账号就沿用 .env 里的全局值 —— 别人的号怎么改都影响不到你。
_ACCOUNT_SETTINGS = {}
try:
    for _item in json.loads(os.getenv("TASKS", "[]") or "[]"):
        if not isinstance(_item, dict):
            continue
        _uid = str(_item.get("unique_id") or "")
        if not _uid:
            continue
        _ACCOUNT_SETTINGS[_uid] = {
            "template": str(_item.get("template") or ""),
            "hitokoto_types": _item.get("hitokoto_types") or [],
            "delay_min": _item.get("delay_min"),
            "delay_max": _item.get("delay_max"),
        }
except Exception:
    _ACCOUNT_SETTINGS = {}

_GLOBAL_TEMPLATE = config.get("messageTemplate", "续火花")
_GLOBAL_HITOKOTO = list(config.get("hitokotoTypes") or [])
# 当前正在跑的那个号自己的发送间隔（None = 用全局）
_CUR_DELAY = {"min": None, "max": None}


def apply_account_settings(unique_id):
    """切到某个账号前，把"这个号自己的"消息模板 / 一言类型 / 发送间隔套上去。

    config 就是 get_config() 返回的那一份（msg_builder 读的也是它），
    所以这里改完，下面发消息用的就是改过的值。
    """
    item = _ACCOUNT_SETTINGS.get(str(unique_id or "")) or {}
    template = str(item.get("template") or "").strip()
    config["messageTemplate"] = template or _GLOBAL_TEMPLATE
    kinds = item.get("hitokoto_types")
    if isinstance(kinds, list) and kinds:
        config["hitokotoTypes"] = [str(x) for x in kinds]
    else:
        config["hitokotoTypes"] = list(_GLOBAL_HITOKOTO)
    _CUR_DELAY["min"] = item.get("delay_min")
    _CUR_DELAY["max"] = item.get("delay_max")


# [本地增强] 发送结果记录：控制台会读这些文件来显示"到底发没发出去"
SEND_LOG = "/app/logs/send-status.json"
SHOT_DIR = "/app/logs/send-shots"
# 按「每个抖音号各留 2 条」来留记录。以前是全局只留 2 条：三个账号同一分钟跑完，
# 前面账号的记录会被后面的直接顶掉，控制台又只看得到最近 2 条 ——
# 结果就是别人账号把位置占满，自己的发送结果永远看不到。
KEEP_RUNS_PER_ACCOUNT = 25
KEEP_RUNS_MAX = 100  # 兜底上限，账号再多也不会无限涨


def _safe_name(text):
    cleaned = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_-]+", "_", str(text)).strip("_")
    return cleaned[:24] or "friend"


def capture_shot(session, account, target):
    """截一张发送后的画面，作为"确实发出去了"的证据。"""
    if session is None:
        return ""
    try:
        os.makedirs(SHOT_DIR, exist_ok=True)
        name = "%s-%s-%s.jpg" % (
            time.strftime("%Y%m%d-%H%M%S"),
            _safe_name(account),
            _safe_name(target),
        )
        result = session.send("Page.captureScreenshot", {"format": "jpeg", "quality": 55})
        with open(os.path.join(SHOT_DIR, name), "wb") as handle:
            handle.write(base64.b64decode(result["data"]))
        return name
    except Exception as error:
        print("截图失败:", error)
        return ""


REL_LOGIN_FLAG = "/app/logs/need-relogin.json"
ABORT_FLAG = "/app/logs/abort-run.json"


def abort_requested():
    """控制台上点了「强制停止」后会留下这个标记，任务看到就停手。"""
    return os.path.exists(ABORT_FLAG)


def clear_abort_flag():
    """每次任务开始时清掉旧标记，避免影响下一轮的发送。"""
    try:
        os.remove(ABORT_FLAG)
    except Exception:
        pass


def mark_need_relogin(account, detail):
    """登录失效时留个标记，控制台看到就会提示重新扫码。"""
    try:
        os.makedirs(os.path.dirname(REL_LOGIN_FLAG), exist_ok=True)
        with open(REL_LOGIN_FLAG, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "account": account,
                    "detail": detail,
                },
                handle,
                ensure_ascii=False,
            )
    except Exception as error:
        print("写入登录失效标记失败:", error)


def clear_relogin_marker():
    try:
        if os.path.exists(REL_LOGIN_FLAG):
            os.remove(REL_LOGIN_FLAG)
    except Exception:
        pass


def delay_range():
    """读取"每个好友之间随机等多久"的配置（秒）。

    优先用「当前这个号自己的」设置；没设过才回落到 .env 里的全局值。
    两处都没有就按 0 算（= 好友之间不等待）。
    """
    def _num(raw, fallback):
        if raw is None or str(raw).strip() == "":
            return float(fallback)
        try:
            return float(str(raw).strip())
        except ValueError:
            return float(fallback)
    low = max(0.0, _num(_CUR_DELAY.get("min"), _num(os.getenv("RANDOM_DELAY_MIN"), 0)))
    high = max(0.0, _num(_CUR_DELAY.get("max"), _num(os.getenv("RANDOM_DELAY_MAX"), 0)))
    if high < low:
        low, high = high, low
    return low, high


def delay_seconds():
    """本次要等多久：在配置的区间里随机取一个秒数。"""
    low, high = delay_range()
    if high <= 0:
        return 0.0
    return random.uniform(low, high)


def wait_with_abort(seconds):
    """等待指定秒数；期间每一秒检查一次"强制停止"，被打断就返回 False。"""
    if seconds <= 0:
        return True
    deadline = time.time() + seconds
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            return True
        if abort_requested():
            return False
        time.sleep(min(1.0, remaining))


def record_run(entry, cleanup_shots=True):
    """把一次发送的结果写进 send-status.json，控制台据此显示发送记录。"""
    try:
        os.makedirs(os.path.dirname(SEND_LOG), exist_ok=True)
        data = {"runs": []}
        if os.path.exists(SEND_LOG):
            try:
                with open(SEND_LOG, encoding="utf-8") as handle:
                    loaded = json.load(handle)
                if isinstance(loaded, dict) and isinstance(loaded.get("runs"), list):
                    data = loaded
            except Exception:
                pass
        run_id = str(entry.get("run_id") or "")
        existing_index = next(
            (
                index for index, run in enumerate(data["runs"])
                if run_id and isinstance(run, dict) and str(run.get("run_id") or "") == run_id
            ),
            None,
        )
        if existing_index is None:
            data["runs"].insert(0, entry)
        else:
            data["runs"][existing_index] = entry
        kept, seen = [], {}
        for run in data["runs"]:
            key = str(run.get("unique_id") or run.get("account") or "")
            seen[key] = seen.get(key, 0) + 1
            if seen[key] <= KEEP_RUNS_PER_ACCOUNT and len(kept) < KEEP_RUNS_MAX:
                kept.append(run)
        data["runs"] = kept
        tmp = SEND_LOG + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=1)
        os.replace(tmp, SEND_LOG)
        if cleanup_shots:
            keep = {
                item.get("shot")
                for run in data["runs"]
                for item in run.get("friends", [])
                if item.get("shot")
            }
            try:
                for name in os.listdir(SHOT_DIR):
                    if name not in keep:
                        os.remove(os.path.join(SHOT_DIR, name))
            except Exception:
                pass
    except Exception as error:
        print("写入发送记录失败:", error)


# [新增] 左上角搜索框：翻列表没找到的好友，靠它按名字直接找人
SEARCH_INPUT_SELECTOR = "input[placeholder*='搜索']"
SEARCH_RESULT_BOX_SELECTOR = "[class*='SearchPanelitembox']"
SEARCH_RESULT_TITLE_SELECTOR = "[class*='SearchPanelitemtitle']"
SEARCH_RESULT_BUTTON_SELECTOR = "[class*='SearchPanelitemchat_btn']"
CHAT_HEADER_TITLE_SELECTOR = ".RightPanelHeadertitle, .RightPanelHeaderTitle, [data-e2e='chat-title']"
SEARCH_RESULT_TIMEOUT = 15  # 等搜索结果出来
SEARCH_OPEN_TIMEOUT = 45  # 点完"发消息"之后，等右边聊天窗口确认切过去
CONVERSATION_ITEM_SELECTOR = '[data-e2e="conversation-item"], .conversationConversationItemwrapper'
CONVERSATION_TITLE_SELECTOR = ".conversationConversationItemtitle"
CONVERSATION_LIST_SELECTOR = ".conversationConversationListwrapper"
# 抖音输入区选择真实可编辑的 contenteditable 节点，避免匹配到外层容器。
CHAT_EDITOR_SELECTOR = (
    # New Douyin chat uses a Slate editor (data-placeholder="发送消息") inside
    # the msg-input container. Select the editable node itself; the old
    # messageEditorimChatEditorContainer is only a wrapper and Playwright rejects
    # typing into it as non-editable.
    '[data-e2e="msg-input"] [contenteditable="true"], '
    '.DraftEditor-root [contenteditable="true"]'
)
CHAT_SEND_BUTTON_SELECTOR = '[class~="e2e-send-msg-btn"]'

# [修复] 判断"好友列表到底加载出来没有"用的阈值
LIST_READY_MIN_ITEMS = 5  # 会话条目到这么多，就认为列表已经画出来了
LIST_READY_CONFIRM = 2  # 上面这个信号连续出现两次才算数，避开渲染中途的抖动
MIN_PLAUSIBLE_CONVERSATIONS = 5  # 少于这么多会话还"翻到底"，基本是列表没加载出来


def handle_response(response: Response):
    """
    只监听你要的那个接口响应
    """
    global userIDDict
    # 精准匹配目标接口 URL
    if "aweme/v1/web/im/user/info" in response.url:
        # print(f"URL: {response.url}")
        # print(f"状态码: {response.status}")
        try:
            # 获取接口返回的 JSON 数据
            json_data = response.json()
            # print("\n📦 响应 JSON 数据：")
            # print(json.dumps(json_data, indent=4, ensure_ascii=False))
            for item in json_data.get("data", []):
                short_id = item.get("short_id")  # short_id
                unique_id = item.get("unique_id")  # unique_id
                sec_uid = item.get("sec_uid", "")  # sec_uid 可能不存在，提供默认值为空字符串
                # [修复] 这两个字段都可能是 null：以前 norm(None) 会直接抛异常，
                # 整个响应被丢掉，那一批好友就都进不了 userIDDict。
                nickname = norm(item.get("nickname") or "")  # 昵称
                remark_name = norm(item.get("remark_name") or nickname)  #  备注名，如果没有则使用昵称
                userIDDict[remark_name] = [short_id, unique_id, sec_uid, nickname, remark_name]
        except Exception as e:
            tb = traceback.extract_tb(e.__traceback__)
            last = tb[-1]
            print(f"解析响应失败: {e}")
            print(f"文件: {last.filename}, 行号: {last.lineno}, 函数: {last.name}")


def retry_operation(name, operation, retries=3, delay=2, *args, **kwargs):
    """
    通用的重试逻辑
    :param name: 操作名称（用于日志记录）
    :param operation: 要执行的异步操作
    :param retries: 最大重试次数
    :param delay: 每次重试之间的延迟（秒）
    :param args: 传递给操作的参数
    :param kwargs: 传递给操作的关键字参数
    """
    for attempt in range(retries):
        try:
            return operation(*args, **kwargs)
        except Exception as e:
            if attempt < retries - 1:
                logger.warning(f"{name} 失败，正在重试第 {attempt + 1} 次，错误：{e}")
                time.sleep(delay)
            else:
                logger.error(f"{name} 失败，已达到最大重试次数，错误：{e}")
                raise

def checkTargetName(targetName, targets):
    displayed = norm(targetName)
    return match_contact_name(
        displayed,
        targets,
        aliases=userIDDict.get(displayed, []),
    )


def conversation_count(page) -> int:
    """当前页面上渲染出来的会话条目数（读不到就当 0）。"""
    try:
        return page.locator(CONVERSATION_ITEM_SELECTOR).count()
    except Exception:
        return 0


def list_is_scrollable(page) -> bool:
    """会话列表容器是否已经溢出（说明内容不止一屏）。"""
    try:
        handle = page.locator(CONVERSATION_LIST_SELECTOR).element_handle(timeout=3000)
        if handle is None:
            return False
        dims = page.evaluate(
            "(element) => [element.scrollHeight, element.clientHeight]", handle
        )
        return bool(dims) and dims[0] > dims[1] + 4
    except Exception:
        return False


def list_ready_signal(page) -> bool:
    """好友列表看起来已经画出来了？

    实测：页面刚打开时容器是空的（一条会话都没有、也滚不动），要几十秒后才一次性把
    会话画出来。所以这里用"真的画出来了"这种正向信号：
      - 会话条目已经够多，或者
      - 列表容器已经溢出可滚动
    光看"条目数 > 0"不行 —— 只画出第一个会话时就会放行（2026-09-13 20:37 那次就是
    这么丢掉两个好友的）；光看"条目数不再变化"也不行 —— 卡在 1 个会话时它同样"稳定"。
    """
    count = conversation_count(page)
    if count <= 0:
        return False
    if count >= LIST_READY_MIN_ITEMS:
        return True
    return list_is_scrollable(page)


def wait_for_conversation_list(page, timeout=120, abort_check=None):
    """等好友列表真正加载出来。

    abort_check：每次轮询调用一次，返回 True 就中断（比如发现登录弹窗）。
    返回 "ready" / "abort" / "timeout"。
    """
    deadline = time.time() + timeout
    hits = 0
    while time.time() < deadline:
        if list_ready_signal(page):
            hits += 1
            if hits >= LIST_READY_CONFIRM:
                return "ready"
        else:
            hits = 0
        if abort_check is not None and abort_check():
            return "abort"
        time.sleep(2)
    return "timeout"


def _name_matches(displayed, target):
    """屏幕上的名字和名单上的名字，是不是同一个人。

    列表里会带角标后缀（比如 "王宇轩 (2)"），所以允许"名字 + 空格/括号开头的后缀"；
    但一个字的名单（"单"、"贾"）如果显示成"单眼皮小明"就不算 —— 那是另一个人，
    宁可不发，也不能发错人。
    """
    d = norm(displayed or "")
    t = norm(target or "")
    if not d or not t:
        return False
    if d == t:
        return True
    if not d.startswith(t):
        return False
    rest = d[len(t):]
    if len(rest) > 8:
        return False
    return rest[0] in " ([({·-—_"


def _find_search_hit(page, name):
    """在搜索结果里找这个名字那一行，返回对应元素（没有就 None）。"""
    try:
        boxes = page.locator(SEARCH_RESULT_BOX_SELECTOR)
        total = boxes.count()
    except Exception:
        return None
    for index in range(total):
        box = boxes.nth(index)
        try:
            title = box.locator(SEARCH_RESULT_TITLE_SELECTOR).inner_text()
        except Exception:
            continue
        if _name_matches(title, name):
            return box
    return None


def _conversation_row_is_active(page, name):
    """标题栏还没渲染时，用列表选中态辅助确认当前会话。"""
    try:
        rows = page.locator(CONVERSATION_ITEM_SELECTOR)
        for index in range(rows.count()):
            row = rows.nth(index)
            try:
                title = row.locator(CONVERSATION_TITLE_SELECTOR).inner_text()
            except Exception:
                continue
            if not _name_matches(title, name):
                continue
            cls = row.get_attribute("class") or ""
            if "curConversation" in cls or row.get_attribute("aria-selected") == "true":
                return True
    except Exception:
        pass
    return False


def _chat_is_open_for(page, name):
    """确认右侧聊天标题或列表选中态对应目标好友。

    这一步是安全闸：如果点完搜索结果其实没切过去（聊天客户端还没连上时就会这样），
    右边标题还是上一个人的名字，这里会判 False，宁可这个好友不发，也不能把消息发错人。
    """
    try:
        titles = page.locator(CHAT_HEADER_TITLE_SELECTOR).all_inner_texts()
    except Exception:
        titles = []
    visible_titles = [str(title or "").strip() for title in titles if str(title or "").strip()]
    if visible_titles:
        return any(_name_matches(title, name) for title in visible_titles)
    return _conversation_row_is_active(page, name)


def _activate_chat_by_row(page, row, username, name, timeout=10):
    """用抖音当前聊天界面能处理的事件序列打开已匹配的会话。

    在线上页面，Playwright locator/mouse/CDP 点击可能只留下列表项、没有切换右侧聊天。
    对准确的 data-e2e 会话项派发鼠标事件后才会触发聊天详情请求。确认标题或选中态后，
    调用方才允许输入消息。
    """
    dispatched = False
    try:
        row.evaluate(
            """el => {
              for (const type of ['mousedown', 'mouseup', 'click']) {
                el.dispatchEvent(new MouseEvent(type, {
                  bubbles: true, cancelable: true, view: window
                }));
              }
            }"""
        )
        dispatched = True
    except Exception as error:
        logger.warning(
            f"账号 {username} 触发好友 {name} 的会话点击失败：{type(error).__name__}"
        )

    if dispatched:
        deadline = time.monotonic() + max(1, timeout)
        while time.monotonic() < deadline:
            if _chat_is_open_for(page, name):
                logger.debug(f"账号 {username} 已确认打开好友 {name} 的聊天窗口")
                return True
            try:
                page.wait_for_timeout(200)
            except Exception:
                time.sleep(0.2)

    # 兼容兜底：后续页面若不再响应合成事件，再试普通点击。
    try:
        row.click(timeout=5000)
    except Exception:
        pass
    deadline = time.monotonic() + max(1, timeout)
    while time.monotonic() < deadline:
        if _chat_is_open_for(page, name):
            logger.debug(f"账号 {username} 通过兼容点击确认打开好友 {name} 的聊天窗口")
            return True
        try:
            page.wait_for_timeout(200)
        except Exception:
            time.sleep(0.2)
    return False


def open_chat_by_search(page, username, name):
    """用左上角搜索框按名字找人，并点开和这个好友的聊天窗口。

    实测（2026-09-13）：搜索框输名字会出"联系人"一行，右边带「发消息」按钮，
    点它就切到这个好友的聊天窗口。但页面刚打开、聊天客户端还没连上的时候，点了没反应，
    所以要重试，而且点完必须核对右边标题栏的名字才算成功。
    """
    for attempt in range(2):
        try:
            box = page.locator(SEARCH_INPUT_SELECTOR).first
            box.click(timeout=10000)
            try:
                box.fill("")
            except Exception:
                pass
            box.type(name, delay=80)
        except Exception as error:
            logger.warning(f"账号 {username} 打不开搜索框，放弃用搜索兜底：{error}")
            return False

        hit = None
        deadline = time.time() + SEARCH_RESULT_TIMEOUT
        while time.time() < deadline:
            hit = _find_search_hit(page, name)
            if hit is not None:
                break
            time.sleep(0.6)

        if hit is None:
            logger.debug(f"账号 {username} 搜索框第 {attempt + 1} 次没搜到 {name}")
            time.sleep(2)
            continue

        try:
            button = hit.locator(SEARCH_RESULT_BUTTON_SELECTOR)
            if button.count() > 0:
                button.first.click(timeout=8000)
            else:
                hit.click(timeout=8000)
        except Exception as error:
            logger.warning(f"账号 {username} 点搜索结果里的 {name} 失败：{error}")
            continue

        deadline = time.time() + SEARCH_OPEN_TIMEOUT
        while time.time() < deadline:
            if _chat_is_open_for(page, name):
                logger.info(f"账号 {username} 用搜索框找到并打开了好友 {name}")
                return True
            time.sleep(0.5)
        logger.warning(
            f"账号 {username} 搜索里找到了 {name}，但聊天窗口没切过去，跳过（不冒发错人的风险）"
        )
        time.sleep(2)
    return False


# ---------------------------------------------------------------------------
# [修复·误报] "消息到底发出去没有" 的判定。
#
# 2026-09-26 翻车记录：上一版**只信 DOM，而且类名是猜的**
# （`[class*='box-item-']` + /is-me|isMe|is_me|self|mine/）。
# 抖音真实类名是 `.MessageBoxContentisFromMe` —— 既不含 is-me 也不含 isMe，
# 于是"自己发的消息"永远匹配 0 条，8 条真发出去的消息全被判成"未送达"，
# 还在控制台弹了个假的"抖音登录已失效"。
#
# 两条教训直接写进逻辑：
#   ① 选择器必须来自实测。这里改用抖音自己的挂点（data-e2e 属性），
#      权威来源是 `_upstream/core/douyin_im.py` 里从真实 HAR 提取的那份。
#   ② **绝不把"没验出来"当成"失败"**。服务端回执优先；回执通道不可用时
#      宁可报"已发送（未校验）"，也不能冤枉一条真发出去的消息。
# ---------------------------------------------------------------------------

# 抖音网页版 IM 的挂点（data-e2e / 稳定类名，不依赖构建哈希）
MSG_ITEM_SELECTOR = '[data-e2e="msg-item-content"]'
MSG_FROM_ME_SELECTOR = ".MessageBoxContentisFromMe"
MSG_LIST_SELECTOR = '[data-e2e="message-list"]'
IM_SEND_PATH = "/v1/message/send"

# 发送失败时消息区里会出现的字样。**只在消息区里找**，而且收得很窄：
# 上一版拿一串词（含"重发"）去搜整页 innerText，页面上别处只要出现一次就误报失败。
_SEND_FAIL_WORDS = ("发送失败", "未送达")

# 登录失效时回执 / 页面痕迹里常见的字样（只有命中这些才敢说"疑似登录失效"，
# 否则限流、风控之类的失败也会被说成登录过期，又是一次假警报）。
_AUTH_HINTS = (
    "登录", "登陆", "log in", "login", "token", "auth", "expired",
    "过期", "失效", "认证", "鉴权", "未授权",
)

_JS_SCAN_MESSAGES = """
() => {
    const SEL_ITEM = '__MSG_ITEM__';
    const SEL_ME = '__MSG_ME__';
    const isMine = (el) => {
        if (!el) return false;
        if (el.matches && el.matches(SEL_ME)) return true;             // 就是它自己
        if (el.closest && el.closest(SEL_ME)) return true;             // 祖先
        if (el.querySelector && el.querySelector(SEL_ME)) return true; // 子孙
        return false;
    };
    let items = document.querySelectorAll(SEL_ITEM);
    if (!items.length) items = document.querySelectorAll(SEL_ME);
    const mine = [];
    const all = [];
    for (const node of items) {
        const rect = node.getBoundingClientRect();
        if (!(rect.width > 0 && rect.height > 0)) continue;
        const text = String(node.innerText || node.textContent || "").trim();
        if (!text) continue;
        all.push(text);
        if (isMine(node)) mine.push(text);
    }
    // 自己那侧优先；标记一个都没命中（抖音改类名了）就退回全部 ——
    // 宁可放宽，也绝不能再出现"选择器不匹配 → 真发出去的消息被判成没发出去"。
    return {
        texts: (mine.length ? mine : all).slice(-60),
        items: all.length,
        own: mine.length,
    };
}
"""

_JS_FAILURE_SIGNATURE = """
() => {
    const scope = document.querySelector('__MSG_LIST__')
               || document.querySelector('.RightPanelBody')
               || document.body;
    if (!scope) return "";
    const icons = scope.querySelectorAll(
        "svg[class*='fail'], svg[class*='Fail'], svg[class*='error'],"
        + " svg[class*='Error'], [class*='exclamation'], [class*='Exclamation'],"
        + " [class*='failIcon'], [class*='retry'], [class*='Retry'],"
        + " [class*='resend'], [class*='Resend']"
    );
    for (const el of icons) {
        const r = el.getBoundingClientRect();
        if (r.width > 0 && r.height > 0) {
            return "fail_icon(" + String(el.className || "").slice(0, 48) + ")";
        }
    }
    const text = String(scope.innerText || "");
    const words = __FAIL_WORDS__;
    for (const kw of words) {
        if (text.includes(kw)) return "fail_text=" + kw;
    }
    return "";
}
"""


def _scan_message_list(page):
    """扫一遍消息区，返回 {texts, items, own, ok}。

    texts = 用来比对的候选文本（自己的那侧优先，退路是全部条目）
    items = 消息条目总数（0 说明聊天区压根没渲染出来，是会话有问题的强信号）
    """
    try:
        data = page.evaluate(
            _JS_SCAN_MESSAGES.replace("__MSG_ITEM__", MSG_ITEM_SELECTOR).replace(
                "__MSG_ME__", MSG_FROM_ME_SELECTOR
            )
        )
    except Exception:
        return {"texts": [], "items": 0, "own": 0, "ok": False}
    if not isinstance(data, dict):
        return {"texts": [], "items": 0, "own": 0, "ok": False}
    data["ok"] = True
    return data


def _send_failure_signature(page):
    """消息区里有没有"发送失败"的痕迹，返回一段描述（没有就是空串）。

    只在消息区里找，不扫整页 —— 整页里随便哪个角落有个"重发"字样就会误报。
    """
    try:
        words = json.dumps(list(_SEND_FAIL_WORDS), ensure_ascii=False)
        return (
            page.evaluate(
                _JS_FAILURE_SIGNATURE.replace("__MSG_LIST__", MSG_LIST_SELECTOR).replace(
                    "__FAIL_WORDS__", words
                )
            )
            or ""
        )
    except Exception:
        return ""


def message_reached_bubble(page, message, scan=None):
    """刚发的那条消息，有没有真的出现在聊天记录里（DOM 旁证）。

    比对前把空白都去掉，避免换行 / 零宽字符造成假不匹配。
    **注意：返回 False 不代表没发出去** —— 真正的判据是服务端回执。
    """
    wanted = norm(message or "").strip()
    if not wanted:
        return False
    data = scan if isinstance(scan, dict) else _scan_message_list(page)
    for shown in data.get("texts") or []:
        if wanted in norm(shown):
            return True
    return False


def _looks_like_auth_failure(text):
    """这句话看起来像不像"登录/鉴权"的问题。"""
    low = str(text or "").lower()
    return any(hint in low for hint in _AUTH_HINTS)


def _json_send_success(text):
    """抖音发送回执的 JSON 是成功还是失败。True / False / None（None = 看不出来）。"""
    try:
        data = json.loads(text)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    seen_code = False
    code_ok = True
    error_seen = False
    stack = [data]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            # status_code / err_no 这类是"错误码"：0 = 没错 → 明确成功
            for key in ("status_code", "err_no", "errno", "error_code"):
                if key in current:
                    seen_code = True
                    if current.get(key) not in (0, "0"):
                        code_ok = False
            # `code` 有歧义：见过 code=0，也见过 code=200 表示成功。
            # 所以只在它明显不是成功值时才当失败；成功值**不**用于确认成功。
            # （否则抖音哪天真返 {"code":200} 就会被我们判成失败 —— 又是一次假警报，
            #   正是这次要消灭的那类错误。）
            if "code" in current:
                value = current.get("code")
                if value not in (0, "0", 200, "200"):
                    seen_code = True
                    code_ok = False
            message = str(
                current.get("message") or current.get("status_msg") or ""
            ).lower()
            if message and message not in ("success", "ok", "0"):
                error_seen = True
            for key in ("error", "error_msg", "err_msg"):
                if current.get(key):
                    error_seen = True
            stack.extend(
                value for value in current.values() if isinstance(value, (dict, list))
            )
        elif isinstance(current, list):
            stack.extend(
                value for value in current if isinstance(value, (dict, list))
            )
    if seen_code:
        return code_ok and not error_seen
    if error_seen:
        return False
    return None


def _json_send_error(text):
    """从回执里抠一句给人看的原因。"""
    fallback = (text or "")[:200].strip()
    try:
        data = json.loads(text)
    except Exception:
        return fallback
    if not isinstance(data, dict):
        return fallback
    stack = [data]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            for key in ("status_msg", "message", "msg", "err_msg", "error", "reason"):
                value = current.get(key)
                if value:
                    return str(value)[:200]
            stack.extend(
                value for value in current.values() if isinstance(value, (dict, list))
            )
        elif isinstance(current, list):
            stack.extend(
                value for value in current if isinstance(value, (dict, list))
            )
    return fallback


def decide_send_outcome(
    receipt, landed, leftover, failure_sign, scan_ok, scan_items, read_error=""
):
    """把"这次到底发出去没有"收成一个**纯函数**，方便离线把所有分支都跑一遍。

    返回 (ok, reason, detail, relogin_detail)；relogin_detail 非空 = 要报登录失效。

    证据强弱：服务端回执 > 聊天记录 > 页面失败痕迹。
    最关键的一条：**"没看到回执" ≠ "没发出去"** —— 这时只能报 sent_unverified，
    上一版就是在这里把 8 条真发出去的消息全冤成了"未送达"。
    """
    if receipt.get("ok") is True:
        return (
            True,
            "sent",
            "已发送（服务端回执正常：%s）" % receipt.get("reason"),
            "",
        )
    if receipt.get("ok") is False:
        # 服务端明确拒绝 / 网络层就断了：这才是真的没发出去
        relogin = ""
        if _looks_like_auth_failure(receipt.get("reason")):
            relogin = (
                "消息发不出去（%s），疑似登录已失效，请重新扫码" % receipt.get("reason")
            )
        return False, "not_delivered", "消息没有送达：%s" % receipt.get("reason"), relogin
    if landed:
        # 没抓到回执（或回执看不清），但聊天记录里确实有这条消息
        return True, "sent", "已发送（消息已出现在聊天记录里）", ""
    if leftover:
        return (
            False,
            "stuck",
            "消息没发出去：输入框里还残留 %d 个字" % len(leftover),
            "",
        )
    if failure_sign:
        return (
            False,
            "not_delivered",
            "消息没有送达（页面出现发送失败痕迹：%s）" % failure_sign,
            "消息发不出去（页面显示发送失败），疑似登录已失效，请重新扫码",
        )
    if not receipt.get("enabled") or not scan_ok:
        # 回执通道没起来 / 消息区读不到，页面也看不出结果 ——
        # 这是"没验出来"，不是"失败"。绝不冤枉一条真发出去的消息。
        return (
            True,
            "sent_unverified",
            "已发送（未能校验：拿不到服务端回执，页面也没留下痕迹）",
            "",
        )
    if not scan_items:
        return (
            False,
            "not_delivered",
            "消息没有送达：聊天区一条消息都没有渲染出来",
            "消息发不出去（聊天区没渲染出来），疑似登录已失效，请重新扫码",
        )
    return (
        False,
        "not_delivered",
        "消息没有送达：输入框已清空，但没看到发送请求、聊天记录里也没有这条"
        + ("（%s）" % read_error if read_error else ""),
        "消息发不出去（没有发送请求也没有聊天记录），疑似登录已失效，请重新扫码",
    )


class ImSendWatcher:
    """抓抖音 /v1/message/send 的真实回执（走 CDP 网络层）。

    为什么必须看服务端：发送失败时输入框照样清空，DOM 类名还会随版本变。
    "这条消息有没有被抖音接受"只有服务端回执说了算。

    重要：CDP 不可用时 `enabled=False`，这时**绝对不能**把"没看到回执"
    当成"没发出去" —— 那是"没验出来"，不是"没发出去"。上一版就是栽在这。
    """

    def __init__(self, page):
        self.session = None
        self.enabled = False
        self.error = ""
        self.rows = {}
        try:
            self.session = page.context.new_cdp_session(page)
            self.session.send("Network.enable")
            self.session.on("Network.requestWillBeSent", self._on_request)
            self.session.on("Network.responseReceived", self._on_response)
            self.session.on("Network.loadingFinished", self._on_finished)
            self.session.on("Network.loadingFailed", self._on_failed)
            self.enabled = True
        except Exception as error:
            self.error = str(error)
            self.session = None
            logger.warning("抓不到发送回执（CDP 不可用），本次只能退回看页面：%s" % error)

    # --- CDP 回调（同步 API 里这些回调在 wait_for_timeout 期间被派发）---

    def _on_request(self, params):
        try:
            request = params.get("request") or {}
            url = str(request.get("url") or "")
            if IM_SEND_PATH not in url:
                return
            if str(request.get("method") or "").upper() != "POST":
                return
            self.rows[params.get("requestId")] = {
                "url": url.split("?", 1)[0],
                "post": str(request.get("postData") or ""),
                "status": None,
                "headers": {},
                "finished": False,
                "failed": "",
                "body": "",
            }
        except Exception:
            pass

    def _on_response(self, params):
        row = self.rows.get(params.get("requestId"))
        if not row:
            return
        response = params.get("response") or {}
        row["status"] = response.get("status")
        row["headers"] = response.get("headers") or {}

    def _on_finished(self, params):
        row = self.rows.get(params.get("requestId"))
        if row:
            row["finished"] = True

    def _on_failed(self, params):
        row = self.rows.get(params.get("requestId"))
        if row:
            row["failed"] = str(params.get("errorText") or "loading_failed")

    # --- 主线程侧 ---

    @staticmethod
    def _header(headers, name):
        target = name.lower()
        for key, value in (headers or {}).items():
            if str(key).lower() == target:
                return str(value)
        return ""

    def reset(self):
        """给下一个好友发送前清一次，免得上一轮的回执混进来。"""
        self.rows.clear()

    def collect(self, page, message, wait_ms=2500):
        """等回执落地 + 取回响应体，返回一条汇总。

        同步 Playwright 的 CDP 事件必须靠阻塞调用"泵"出来，所以这里必须
        wait_for_timeout（已在 test_cdp_mech.py 实测过）；而 getResponseBody
        只能在主线程调，不能在 CDP 回调里调（会死锁）。
        """
        if not self.enabled:
            return {
                "enabled": False,
                "seen": False,
                "ok": None,
                "httpStatus": None,
                "logid": "",
                "reason": self.error or "CDP 不可用",
                "matched": False,
                "url": "",
                "bodyOk": None,
            }
        try:
            page.wait_for_timeout(wait_ms)
        except Exception:
            time.sleep(max(wait_ms, 0) / 1000.0)
        for request_id, row in list(self.rows.items()):
            if row.get("finished") and not row.get("body"):
                row["body"] = self._fetch_body(request_id)
        return self._summarize(message)

    def _fetch_body(self, request_id):
        try:
            body = self.session.send("Network.getResponseBody", {"requestId": request_id})
        except Exception:
            # 实测响应体可能已经过期（No resource with given identifier found），
            # 那就当"看不到内容"，不能因此判失败。
            return ""
        raw = body.get("body") or ""
        if body.get("base64Encoded"):
            try:
                return base64.b64decode(raw).decode("utf-8", errors="replace")
            except Exception:
                return ""
        return raw

    def _summarize(self, message):
        wanted = norm(message or "")
        rows = list(self.rows.values())
        receipt = {
            "enabled": True,
            "seen": bool(rows),
            "ok": None,
            "httpStatus": None,
            "logid": "",
            "reason": "",
            "matched": False,
            "url": "",
            "bodyOk": None,
        }
        if not rows:
            receipt["reason"] = "没看到发送请求（可能压根没发出去）"
            return receipt

        # 优先挑"请求体里就是我们这句话"的那条，免得抓到别人的 / 别的消息
        chosen = None
        for row in rows:
            if wanted and wanted in norm(row.get("post")):
                chosen = row
                receipt["matched"] = True
        if chosen is None:
            chosen = rows[-1]

        receipt["url"] = chosen.get("url") or ""
        receipt["httpStatus"] = chosen.get("status")
        receipt["logid"] = (
            self._header(chosen.get("headers"), "x-tt-logid")
            or self._header(chosen.get("headers"), "x-tt-trace-log")
            or self._header(chosen.get("headers"), "x-tt-trace-id")
        )

        if chosen.get("failed"):
            receipt["ok"] = False
            receipt["reason"] = "网络层就失败了：%s" % chosen.get("failed")
            return receipt

        status = chosen.get("status")
        if not isinstance(status, int):
            receipt["reason"] = "响应还没回来"
            return receipt
        if not (200 <= status < 300):
            receipt["ok"] = False
            receipt["reason"] = "服务端返回 HTTP %s" % status
            return receipt

        body = chosen.get("body") or ""
        json_ok = _json_send_success(body)
        receipt["bodyOk"] = json_ok
        if json_ok is True:
            receipt["ok"] = True
            receipt["reason"] = "服务端已接受（HTTP %s）" % status
        elif json_ok is False:
            receipt["ok"] = False
            receipt["reason"] = "服务端拒绝：%s" % (
                _json_send_error(body) or "未知原因"
            )
        else:
            # HTTP 通了但回执内容认不出来 —— 这是"没验出来"，不是"失败"
            receipt["ok"] = None
            receipt["reason"] = "HTTP %s 但回执内容看不出来" % status
        return receipt

    def close(self):
        try:
            if self.session is not None:
                self.session.detach()
        except Exception:
            pass


def close_search_panel(page):
    """搜索用完，把左边的搜索状态收回去，别影响后面的操作和现场截图。"""
    try:
        if page.locator(SEARCH_INPUT_SELECTOR).count():
            cancel = page.get_by_text("取消", exact=True)
            if cancel.count():
                cancel.first.click(timeout=5000)
                time.sleep(1)
    except Exception:
        pass


def scroll_and_select_user(page, username, targets, skip=None):
    """尝试滚动并查找用户名

    skip：本次已经发过消息的好友。列表没加载好、重扫时会传进来，避免重复发送。
    """
    # [修复] skip 是调用方手里的"本次已经发过的好友"集合，而且是发完一个才往里加。
    # 以前这里复制了一份快照，调用方后来加进去的好友这边看不到 —— 页面一旦重扫，
    # 同一个好友就可能被再发一次。这里改成保留引用，每次读最新的那份。
    skip_live = skip if isinstance(skip, set) else set(skip or ())
    # 定义目标元素和滚动容器的选择器
    target_selector = CONVERSATION_ITEM_SELECTOR
    scrollable_friends_selector = CONVERSATION_LIST_SELECTOR

    # [修复] 使用模糊匹配 no-more-tip- 前缀，不再依赖精确哈希后缀
    # 同时增加文本匹配作为兜底
    # no_more_selector = 'xpath=//div[contains(@class, "no-more-tip-")]'
    # loading_selector = 'xpath=//div[contains(@class, "semi-spin")]'

    logger.debug(f"账号 {username} 开始查找目标好友列表")
    logger.debug(f"账号 {username} 目标好友列表: {targets}")

    found_targets = set()
    # [修改] 复制一份目标列表用于追踪进度
    remaining_targets = set(targets)

    # [修复] 新增：连续空滚动计数器（滚动后没有发现新好友的次数）
    empty_scroll_count = 0
    # [修复] 真正用来判断"到底了"的计数器：滚不动、并且列表也没变长，才算一次。
    # 单独放一个计数器是因为"没找到新好友"和"滚不动"在到底时同时成立，加在一起会让
    # 判定来得太早 —— 会话列表是分页加载的，滚到底之后下一页往往还要几秒才回来。
    bottom_hits = 0
    MAX_EMPTY_SCROLLS = 10  # 连续10次（约半分钟）都滚不动、列表也不变长，才算到底
    # [修复] 用来区分"真的翻到底"和"列表压根没渲染出来"：后者滚不动，表面看一模一样
    scrolled_once = False
    reloaded = False

    while True:
        # 查找所有目标元素
        target_elements = page.locator(target_selector).all()

        # [修复] 记录本轮循环前已发现的好友数，用于判断是否有新发现
        prev_found_count = len(found_targets)

        for element in target_elements:
            try:
                # 查找子元素 span，模糊匹配 class
                span = element.locator(CONVERSATION_TITLE_SELECTOR)
                targetName = norm(span.inner_text())
                if not targetName:
                    continue

                # [修复] found_targets 现在只用来统计"这次一共扫到多少个会话"，
                # 不再当"这个标题处理过了"的挡箭牌：会话列表是虚拟滚动、而且会被新消息
                # 重新排序，标题第一次画出来时可能还不是备注名。那一刻判成"不是目标"
                # 就把它永久跳过，这个好友后面再也不会被认出来 —— 这就是"有时候找不到"的来源之一。
                found_targets.add(targetName)

                logger.debug(f"账号 {username} 找到好友 {targetName}")
                
                targetSymbol = checkTargetName(targetName, targets)

                if targetSymbol and targetSymbol in skip_live:
                    # 重扫时遇到本次已经发过的好友，跳过，别重复发一遍
                    logger.debug(f"账号 {username} 好友 {targetName} 本次已发送过，跳过")
                    continue

                if targetSymbol:
                    chat_opened = _activate_chat_by_row(
                        page, element, username, targetSymbol
                    )
                    yield (targetSymbol, chat_opened)

                    # [修改] 标记已找到，如果全找到了直接退出
                    if targetSymbol in remaining_targets:
                        remaining_targets.remove(targetSymbol)
                    if len(remaining_targets) == 0:
                        logger.debug(f"账号 {username} 所有目标好友均已找到，停止搜索")
                        return
                    break
            except Exception as e:
                traceback.print_exc()
        else:
            # [修复] 检查本轮是否有新好友被发现
            new_found = len(found_targets) > prev_found_count
            if new_found:
                empty_scroll_count = 0  # 有新发现，重置计数器
                bottom_hits = 0
            else:
                empty_scroll_count += 1  # 无新发现，递增计数器

            # [修复] 状态检测逻辑（多重兜底）

            # # 1. 检查是否到底（"没有更多了" —— 使用模糊类名匹配）
            # if page.locator(no_more_selector).count() > 0:
            #     logger.info(f"账号 {username} 检测到'没有更多了'标志，已到达底部")
            #     if len(remaining_targets) > 0:
            #         logger.warning(
            #             f"账号 {username} 搜索结束，仍有以下好友未找到: {remaining_targets}"
            #         )
            #     break

            # 2. [修复] 检查连续空滚动次数，防止死循环
            # 到底 = 连续很多次滚不动且列表没变长；后面那个 empty_scroll_count 是兜底，
            # 防止"能滚但一条新会话都不出现"这种诡异情况下死循环。
            if bottom_hits >= MAX_EMPTY_SCROLLS or empty_scroll_count >= MAX_EMPTY_SCROLLS * 4:
                # [修复] 先别急着宣布"翻到底"：列表只渲染出一两个会话、滚动条根本不动时，
                # 看起来和"到底"完全一样，其实是页面没加载出来 —— 以前这里直接放弃，就把
                # 好友误报成"不在这个号的好友里"。现在重新加载页面再扫一遍（发过的不重发）。
                if not reloaded and (
                    not scrolled_once
                    or len(found_targets) < MIN_PLAUSIBLE_CONVERSATIONS
                    or remaining_targets
                ):
                    # 三种情况都算"列表没加载全"：压根没滚动过（整屏都没填满）、
                    # 会话数少得不像话、或者翻到底了还有好友没出现。
                    # 反正只重来这一次，把已经发过的好友跳过，不会再发第二遍。
                    reloaded = True
                    logger.warning(
                        f"账号 {username} 翻到底只看到 {len(found_targets)} 个会话"
                        f"（{'一次都没滚动动' if not scrolled_once else '数量偏少'}），"
                        f"还有 {len(remaining_targets)} 个好友没找到，"
                        f"判定是好友列表没加载全，重新加载页面再扫一遍"
                    )
                    try:
                        retry_operation(
                            "重新打开抖音网页聊天页面",
                            page.goto,
                            retries=1,
                            delay=3,
                            url="https://www.douyin.com/chat",
                            wait_until="domcontentloaded",
                        )
                        time.sleep(3)
                        if wait_for_conversation_list(page, timeout=120) == "ready":
                            logger.info(f"账号 {username} 页面已重新加载，继续查找好友")
                        else:
                            logger.warning(f"账号 {username} 页面重新加载后列表仍不稳定，继续尽力查找")
                    except Exception:
                        traceback.print_exc()
                    found_targets.clear()
                    empty_scroll_count = 0
                    bottom_hits = 0
                    continue
                logger.warning(
                    f"账号 {username} 连续 {MAX_EMPTY_SCROLLS} 次滚不动、列表也不再变长，判定已到达底部"
                    f"（本次一共扫到 {len(found_targets)} 个会话）"
                )
                if len(remaining_targets) > 0:
                    logger.warning(
                        f"账号 {username} 搜索结束，仍有以下好友未找到: {remaining_targets}"
                    )
                break

            # 3. 检查是否正在加载
            # if page.locator(loading_selector).count() > 0:
            #     logger.debug(f"账号 {username} 列表正在加载中 (Loading)...")
            #     time.sleep(1.5)  # 给加载留点时间
            #     # 不 break，继续去滚动以触发后续内容

            # 4. 滚动容器
            try:
                scrollable_element = page.locator(
                    scrollable_friends_selector
                ).element_handle(timeout=10000)
            except Exception:
                scrollable_element = None

            if scrollable_element:
                # [修复] 会话列表是"虚拟滚动"：屏幕上永远只挂着十几个条目（实测条目高约 67px）。
                # 以前一次跳 800px，屏幕上挂的条目少一点就会整段跳过去（漏看好友）；
                # 而且列表是分页加载的，滚到底的那一瞬间下一页往往还在路上，看起来跟"真的到底"
                # 一模一样 —— 于是好好的好友被报成"找不到"。
                # 现在：按可视高度的一半滚（保证和上一屏一定重叠），并且只有"列表确实不再变长"
                # 才记账算到底。
                metrics_before = page.evaluate(
                    "(element) => [element.scrollTop, element.scrollHeight, element.clientHeight]",
                    scrollable_element,
                )
                scroll_top_before, height_before, view_height = metrics_before
                step = max(240, int(view_height * 0.5))

                # 注意：Playwright 的 evaluate 只接受一个参数，所以步长直接写进 JS 里
                page.evaluate(
                    "(element) => { element.scrollTop += %d; }" % step,
                    scrollable_element,
                )
                time.sleep(0.4)
                scroll_top_after, height_after, _ = page.evaluate(
                    "(element) => [element.scrollTop, element.scrollHeight, element.clientHeight]",
                    scrollable_element,
                )

                if height_after > height_before:
                    # 列表又长出来了 —— 分页还在往下加载，绝不能算"到底"
                    empty_scroll_count = 0
                    bottom_hits = 0
                    scrolled_once = True
                    logger.debug(
                        f"账号 {username} 列表又加载出新会话（{height_before} -> {height_after}），继续往下翻"
                    )
                elif scroll_top_before == scroll_top_after:
                    # 滚不动了。先别急着记一笔"到底"：滚到底会触发下一页加载，
                    # 等一两秒列表往往会突然变长。
                    time.sleep(1.2)
                    height_later = page.evaluate(
                        "(element) => element.scrollHeight", scrollable_element
                    )
                    if height_later > height_before:
                        empty_scroll_count = 0
                        bottom_hits = 0
                        scrolled_once = True
                        logger.debug(
                            f"账号 {username} 滚到底后列表又加载出新会话（{height_before} -> {height_later}），继续往下翻"
                        )
                    else:
                        # 真的滚不动、列表也没变长，这才记一笔"疑似到底"
                        bottom_hits += 1
                        logger.debug(
                            f"账号 {username} scrollTop 未变化 ({scroll_top_before})，可能已到底 (到底计数: {bottom_hits}/{MAX_EMPTY_SCROLLS})"
                        )
                else:
                    scrolled_once = True
                    empty_scroll_count = 0
                    bottom_hits = 0
                    logger.debug(
                        f"账号 {username} 滚动好友列表以加载更多好友 (scrollTop: {scroll_top_before} -> {scroll_top_after})"
                    )

                time.sleep(1.2)
            else:
                logger.error(f"账号 {username} 未找到滚动容器，退出")
                break

    # [新增] 列表翻到底还有没找到的好友：再拿左上角搜索框按名字找一遍。
    # 会话列表是"虚拟滚动 + 分页"的，屏幕上永远只挂着十几个条目，个别好友（被挤到很下面、
    # 或者分页还没把那一页加载出来）翻列表就是看不到；搜索框是直接按名字找人，
    # 搜到的"联系人"行右边有「发消息」按钮，点开就是这个好友的聊天窗口，比翻列表更彻底。
    if remaining_targets:
        logger.warning(
            f"账号 {username} 翻列表没找到 {sorted(remaining_targets)}，改用左上角搜索框再找一遍"
        )
        for name in sorted(remaining_targets):
            if abort_requested():
                logger.warning(f"账号 {username} 收到强制停止指令，搜索兜底也停下")
                break
            if name in skip_live:
                continue
            if open_chat_by_search(page, username, name):
                yield (name, _chat_is_open_for(page, name))
        close_search_panel(page)


def do_user_task(browser, username, cookies, targets, unique_id=""):
    account = username
    storage_state = None
    if isinstance(cookies, dict):
        if cookies.get("version") == 2 and isinstance(cookies.get("storage_state"), dict):
            storage_state = cookies["storage_state"]
        elif isinstance(cookies.get("cookies"), list) and isinstance(cookies.get("origins"), list):
            storage_state = cookies
        if storage_state is None:
            logger.error("账号 %s 的浏览器凭据格式无效，跳过本次发送", account)
            return
        cookies = storage_state.get("cookies")
        if not isinstance(cookies, list) or not cookies:
            logger.error("账号 %s 的 storage_state 没有 Cookie，跳过本次发送", account)
            return
    # 切到这个号自己的消息模板 / 一言类型 / 发送间隔（没单独配就用全局的）
    apply_account_settings(unique_id)
    # 好友映射是全局 dict：不清理的话，上一个账号抓到的好友会留到下一个账号，
    # 万一重名就可能匹配到别的账号的会话（现在靠 checkTargetName 兜着，但别留这个隐患）
    userIDDict.clear()
    context = browser.new_context(storage_state=storage_state) if storage_state else browser.new_context()
    context.set_default_navigation_timeout(
        config["browserActionTimeout"]
    )  # 设置导航超时时间为 120 秒
    context.set_default_timeout(
        config["browserActionTimeout"]
    )  # 设置所有操作的默认超时时间为 120 秒

    page = context.new_page()

    page.on("response", handle_response)  # 监听响应，收集好友完整信息用于匹配

    # 新凭据恢复完整 storage_state（含 Cookie 和 localStorage）；旧账号继续注入 Cookie。
    if not storage_state:
        context.add_cookies(cookies)

    # [本地增强] 记录本次发送结果，结束后写进 send-status.json 供控制台展示
    entry = {
        "run_id": "%s:%s" % (os.getenv("PANEL_RUN_ID", "") or uuid4().hex, str(unique_id or "")),
        "runner_id": str(os.getenv("PANEL_RUN_ID") or ""),
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "account": account,
        "unique_id": str(unique_id or ""),
        "targets": list(targets),
        "friends": [],
        "status": "running",
        "detail": "",
    }
    # 先落一条进行中记录。若这个抖音号超过独立的 10 分钟上限，监督进程
    # 会只把当前账号改成超时；概览和邮箱通知仍能显示本账号未完成。
    record_run(entry)
    try:
        shot_session = context.new_cdp_session(page)
    except Exception:
        shot_session = None

    # [修复·误报] 同时挂一个"发送回执"观察器：只有它才知道抖音服务端到底
    # 收下没有。上一版只信 DOM（还是猜的类名），8 条真发出去的消息全被冤成"未送达"。
    watcher = None

    try:
        watcher = ImSendWatcher(page)
        # 打开抖音网页聊天页面
        retry_operation(
            "打开抖音网页聊天页面",
            page.goto,
            retries=config["taskRetryTimes"],
            delay=5,
            url="https://www.douyin.com/chat",
            wait_until="domcontentloaded",
        )

        time.sleep(5)  # 等待5秒让过可能存在的弹窗

        # [本地增强] 原来是等 5 秒就直接开始找好友，页面没加载完就会误判成"找不到好友"，
        # 登录失效时也会静默跳过。这里改成：等到好友列表出现，或者发现登录弹窗就明确报错。
        def _login_dialog_visible():
            try:
                return bool(
                    page.evaluate(
                        """() => {
                            const text = document.body ? (document.body.innerText || "") : "";
                            return /扫码登录/.test(text) && /验证码登录|密码登录/.test(text);
                        }"""
                    )
                )
            except Exception:
                return False

        # [修复] 以前"条目数 > 0"就算就绪：列表是异步渲染的，偶尔只画出第一个会话就放行，
        # 后面就会把"还没加载完"当成"已经翻到底"，把好友报成找不到（2026-09-13 20:37 那次
        # 就是这么丢掉两个好友的）。现在等"列表真的画出来了"再开扫。
        list_state = wait_for_conversation_list(page, timeout=120, abort_check=_login_dialog_visible)
        if list_state == "abort":
            entry["status"] = "no_login"
            entry["reason"] = "no_login"
            entry["detail"] = "登录已失效（页面弹出了登录框），控制台会自动准备好二维码，扫码即可恢复"
            logger.error(f"账号 {account} 登录已失效，本次不发送")
            mark_need_relogin(account, entry["detail"])
            return
        if list_state == "timeout":
            entry["reason"] = "slow_page"
            logger.warning(
                f"账号 {account} 等待好友列表超时（只看到 {conversation_count(page)} 个会话），仍然继续尝试查找"
            )
        else:
            # [修复·红色感叹号] 以前只要"能打开列表"就无条件清掉登录失效标记。
            # 但"登录失效"不只表现为弹登录框：Cookie 半失效时页面照常渲染，只是消息发不出去
            # （气泡上挂红色感叹号）。这种情况下清标记正好把唯一的警报也抹掉了。
            # 所以只在"本轮确实发成功过"时才清；否则留着，等控制台提示重新扫码。
            pass


        logger.debug(f"账号 {account} 开始发送消息")
        low, high = delay_range()
        if high > 0:
            logger.info(f"账号 {account} 发送间隔：每个好友之间随机等 {low:.0f}-{high:.0f} 秒")
        # 滚动并选择用户
        handled = 0
        seen_targets = set()  # 记下真正在列表里匹配上的好友，循环完就知道谁没找到
        for selection in scroll_and_select_user(page, account, targets, skip=seen_targets):
            if isinstance(selection, (tuple, list)) and len(selection) == 2:
                target, chat_opened = selection
            else:
                # 兼容仍只返回好友名称的旧选择器。
                target, chat_opened = selection, True
            seen_targets.add(target)
            if abort_requested():
                logger.warning(f"账号 {account} 收到强制停止指令，放弃给剩余好友发送")
                entry["friends"].append(
                    {
                        "name": target,
                        "ok": False,
                        "reason": "aborted",
                        "detail": "被控制台的「强制停止」叫停，这个好友没有发送",
                        "shot": "",
                        "message": "",
                    }
                )
                record_run(entry, cleanup_shots=False)
                break
            if handled:
                wait = delay_seconds()
                if wait > 0:
                    logger.info(f"账号 {account} 等 {wait:.0f} 秒再发下一个好友")
                    if not wait_with_abort(wait):
                        logger.warning(f"账号 {account} 等待期间收到强制停止指令，放弃给剩余好友发送")
                        entry["friends"].append(
                            {
                                "name": target,
                                "ok": False,
                                "reason": "aborted",
                                "detail": "被控制台的「强制停止」叫停，这个好友没有发送",
                                "shot": "",
                                "message": "",
                            }
                        )
                        record_run(entry, cleanup_shots=False)
                        break
            handled += 1
            item = {
                "name": target,
                "ok": False,
                "reason": "",
                "detail": "",
                "shot": "",
                "message": "",
            }
            logger.debug(f"账号 {account} 已选中好友 {target} 发送消息")

            if not chat_opened:
                item["reason"] = "chat_not_open"
                item["detail"] = "好友已在列表中找到，但抖音没有确认切换到该聊天窗口；消息未发送"
                item["shot"] = capture_shot(shot_session, account, target)
                logger.error(f"账号 {account} 给 {target} 发送失败：{item['detail']}")
                entry["friends"].append(item)
                record_run(entry, cleanup_shots=False)
                continue

            # 第一步：点开好友后，聊天输入框有没有出来
            try:
                page.wait_for_selector(CHAT_EDITOR_SELECTOR, timeout=45000)
            except Exception:
                item["reason"] = "no_editor"
                item["detail"] = "好友已在列表中找到并点击，但聊天输入框 45 秒内仍未加载出来，消息没发出去"
                item["shot"] = capture_shot(shot_session, account, target)
                logger.error(f"账号 {account} 给 {target} 发送失败：{item['detail']}")
                entry["friends"].append(item)
                record_run(entry, cleanup_shots=False)
                continue

            chat_input = page.locator(CHAT_EDITOR_SELECTOR)
            message = build_message()
            item["message"] = message

            # 第二步：输入内容
            try:
                # 换行两种写法都认：控制台里直接按回车（真换行），
                # 或者老写法 \n（字面量反斜杠+n，默认模板就是这种）
                lines = re.split(r"\\n|\r?\n", message)
                for index, line in enumerate(lines):
                    chat_input.type(line)
                    if index != len(lines) - 1:
                        chat_input.press("Shift+Enter")
            except Exception as error:
                item["reason"] = "type_failed"
                item["detail"] = "在输入框里打字失败：%s" % error
                item["shot"] = capture_shot(shot_session, account, target)
                logger.error(f"账号 {account} 给 {target} 发送失败：{item['detail']}")
                traceback.print_exc()
                entry["friends"].append(item)
                record_run(entry, cleanup_shots=False)
                continue

            # 第三步：按回车发送，然后校验"到底发出去了没有"。
            #
            # [修复·误报] 上一版只看 DOM，而且类名是猜的，于是 8 条真发出去的消息
            # 全被判成"未送达"。现在改成**三级证据，从强到弱**：
            #   ① 服务端回执（CDP 抓 /v1/message/send）—— 抖音收下没有，这是权威
            #   ② 聊天记录里有没有这条消息（DOM 旁证，用抖音自己的 data-e2e 挂点）
            #   ③ 页面上有没有"发送失败"痕迹
            # 而且："没看到回执" ≠ "没发出去"。回执通道不可用时只能报"未校验"，
            # 绝不能反过来冤枉一条真发出去的消息。
            try:
                logger.debug(f"账号 {account} 准备发送消息给好友 {target}")
                watcher.reset()
                send_button = page.locator(CHAT_SEND_BUTTON_SELECTOR).filter(
                    visible=True
                ).last
                if send_button.count() > 0:
                    send_button.click(timeout=5000)
                else:
                    # 旧版页面可能没有稳定的发送按钮挂点，再回退到 Enter。
                    chat_input.press("Enter")
                receipt = watcher.collect(page, message, wait_ms=2500)

                leftover = None
                read_error = ""
                try:
                    # 空的输入框里也有零宽字符，必须用 norm 清掉，否则会误判成"没发出去"
                    leftover = norm(chat_input.inner_text())
                except Exception as error:
                    read_error = str(error)

                scan = _scan_message_list(page)
                landed = message_reached_bubble(page, message, scan)
                failure_sign = _send_failure_signature(page)
                # 回执摘要留在记录里，出问题时不用猜（面板不认这个字段，不影响展示）
                item["receipt"] = "http=%s ok=%s logid=%s matched=%s" % (
                    receipt.get("httpStatus"),
                    receipt.get("ok"),
                    receipt.get("logid") or "-",
                    receipt.get("matched"),
                )

                # 判定逻辑抽成了纯函数 decide_send_outcome()：
                # 里面的分支比现场能造的测试场景还多，写在流程里根本没法逐个验。
                ok, reason, detail, relogin_detail = decide_send_outcome(
                    receipt,
                    landed,
                    leftover,
                    failure_sign,
                    scan.get("ok"),
                    scan.get("items"),
                    read_error,
                )
                item["ok"] = ok
                item["reason"] = reason
                item["detail"] = detail
                if relogin_detail:
                    mark_need_relogin(account, relogin_detail)

                logger.info(
                    f"账号 {account} 给好友 {target} " + ("发送成功" if item["ok"] else "发送失败")
                )
            except Exception as error:
                item["reason"] = "send_error"
                item["detail"] = "发送过程中出错：%s" % error
                logger.error(f"账号 {account} 给 {target} 发送失败：{item['detail']}")
                traceback.print_exc()

            time.sleep(1)
            item["shot"] = capture_shot(shot_session, account, target)
            entry["friends"].append(item)
            record_run(entry, cleanup_shots=False)
        # [本地增强] 名单上还有没匹配上的好友：列表滚到底也没出现这个名字。
        # 截一张当时的画面存进发送记录，点开就知道是「列表压根没加载出来」，
        # 还是「这个人不在这个号的好友里 / 昵称对不上」。
        if not abort_requested():
            missing = [t for t in targets if t not in seen_targets]
            if missing:
                miss_shot = capture_shot(shot_session, account, "、".join(missing))
                for name in missing:
                    entry["friends"].append({
                        "name": name,
                        "ok": False,
                        "reason": "not_found",
                        "detail": "好友列表翻到底也没看到这个名字（不在这个号的好友里，或昵称对不上）",
                        "shot": miss_shot,
                        "message": "",
                    })
                record_run(entry, cleanup_shots=False)
                logger.warning(
                    f"账号 {account} 没找到的好友：{'、'.join(missing)}（已附现场截图）"
                )
    except Exception as error:
        entry["status"] = "error"
        entry["detail"] = "任务出错：%s" % error
        traceback.print_exc()
    finally:
        if entry["status"] == "running":
            if not entry["friends"]:
                entry["status"] = "no_friend"
                entry["reason"] = entry.get("reason") or "no_friend"
                entry["detail"] = "没有匹配到任何目标好友，本次没有发送"
            elif all(item.get("reason") == "not_found" for item in entry["friends"]):
                entry["status"] = "no_friend"
                entry["reason"] = "no_friend"
                entry["detail"] = "名单上的好友一个都没在列表里找到，已附现场截图"
            elif all(item["ok"] for item in entry["friends"]):
                entry["status"] = "ok"
            elif any(item["ok"] for item in entry["friends"]):
                entry["status"] = "partial"
            else:
                entry["status"] = "failed"
        # [修复·红色感叹号] 只有"这一轮真的把消息发出去过"才清登录失效标记。
        # 以前是"能打开好友列表"就清，于是"页面正常但消息发不出去"的半失效状态
        # 每轮都会把自己的警报抹掉，控制台永远只显示"已发送"。
        if any(
            item.get("ok") and item.get("reason") == "sent"
            for item in entry["friends"]
        ):
            had_failure = entry.get("status") in ("no_login",)
            if not had_failure:
                clear_relogin_marker()
        entry["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        record_run(entry)
        if watcher is not None:
            watcher.close()
        context.close()


def runTasks():
    if os.getenv("PANEL_ACCOUNT_CHILD") != "1" and os.getenv("PANEL_RUN_ID"):
        _run_panel_accounts_separately()
        return
    _run_tasks_in_current_process()


def _run_tasks_in_current_process():
    if not userData:
        logger.warning("没有可执行的账号，本次跳过（可能是账号缺少 Cookie）")
        return
    # core.browser.get_browser returns the CloakBrowser Browser instance
    # directly; it does not return a (Playwright, Browser) tuple.
    browser = get_browser()
    try:
        # 检查是否启用多任务和任务数量
        # 创建信号量以限制并发任务数量
        clear_abort_flag()
        logger.info("开始执行任务")
        logger.debug(f"当前配置如下：")
        logger.debug(f"消息模板: {config.get('messageTemplate', '未找到消息模板')}")
        logger.debug(f"一言类型: {config['hitokotoTypes']}")
        for user in userData:
            logger.debug(
                f"用户: {user.get('username', '未知用户')}, 目标好友: {user['targets']}"
            )

        for user in userData:
            cookies = user["cookies"]
            targets = user["targets"]
            username = user.get("username", "未知用户")
            logger.info(f"开始处理账号 {username}")
            # 创建任务
            do_user_task(browser, username, cookies, targets, user.get("unique_id", ""))
            logger.info(f"账号 {username} 任务完成")
    finally:
        # 关闭浏览器实例
        browser.close()



def _terminate_account_process(process):
    """Stop only one timed-out account worker and the browser it launched."""
    if process is None or process.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except Exception:
        try:
            process.kill()
        except Exception:
            pass
    try:
        process.wait(timeout=5)
    except Exception:
        try:
            process.kill()
        except Exception:
            pass
        try:
            process.wait(timeout=2)
        except Exception:
            pass


def _record_account_timeout(user, started_at, timeout_seconds):
    """Mark only the account that exceeded its own time limit."""
    uid = str(user.get("unique_id") or "")
    runner_id = str(os.getenv("PANEL_RUN_ID") or "")
    run_id = "%s:%s" % (runner_id or "", uid)
    account = str(user.get("username") or uid or "抖音账号")
    started_text = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started_at))
    finished_text = time.strftime("%Y-%m-%d %H:%M:%S")
    detail = (
        "此抖音号发送超过 %d 分钟，系统已停止该账号及其浏览器；"
        "本账号未完成，后续账号会继续执行。" % max(1, timeout_seconds // 60)
    )
    try:
        data = {"runs": []}
        if os.path.exists(SEND_LOG):
            try:
                with open(SEND_LOG, encoding="utf-8") as handle:
                    loaded = json.load(handle)
                if isinstance(loaded, dict) and isinstance(loaded.get("runs"), list):
                    data = loaded
            except Exception:
                pass
        runs = data["runs"]
        entry = next(
            (
                item for item in runs
                if isinstance(item, dict)
                and (
                    str(item.get("run_id") or "") == run_id
                    or (runner_id and str(item.get("runner_id") or "") == runner_id
                        and str(item.get("unique_id") or "") == uid)
                )
            ),
            None,
        )
        if entry is not None and str(entry.get("status") or "") != "running":
            return
        if entry is None:
            entry = {
                "run_id": run_id,
                "runner_id": runner_id,
                "at": started_text,
                "account": account,
                "unique_id": uid,
                "targets": list(user.get("targets") or []),
                "friends": [],
            }
            runs.insert(0, entry)
        entry.update({
            "status": "timed_out",
            "reason": "timeout",
            "detail": detail,
            "finished_at": finished_text,
            "timed_out": True,
        })
        runs[:] = runs[:KEEP_RUNS_MAX]
        tmp = SEND_LOG + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=1)
        os.replace(tmp, SEND_LOG)
    except Exception as error:
        logger.error("记录账号 %s 的超时结果失败：%s", account, type(error).__name__)


def _record_account_process_failure(user, started_at, returncode=None, detail=""):
    """Record worker crashes that happen before do_user_task can create a result."""
    uid = str(user.get("unique_id") or "")
    runner_id = str(os.getenv("PANEL_RUN_ID") or "")
    run_id = "%s:%s" % (runner_id, uid)
    account = str(user.get("username") or uid or "抖音账号")
    finished_text = time.strftime("%Y-%m-%d %H:%M:%S")
    if not detail:
        if returncode is not None and returncode < 0 and os.name != "nt":
            try:
                signal_name = signal.Signals(-returncode).name
            except (ValueError, AttributeError):
                signal_name = "信号 %d" % -returncode
            detail = "发送进程被 %s 中断（退出码 %s）" % (signal_name, returncode)
        else:
            detail = "发送进程异常退出（退出码 %s）" % returncode
    try:
        data = {"runs": []}
        if os.path.exists(SEND_LOG):
            try:
                with open(SEND_LOG, encoding="utf-8") as handle:
                    loaded = json.load(handle)
                if isinstance(loaded, dict) and isinstance(loaded.get("runs"), list):
                    data = loaded
            except Exception:
                pass
        runs = data["runs"]
        entry = next(
            (
                item for item in runs
                if isinstance(item, dict)
                and str(item.get("run_id") or "") == run_id
            ),
            None,
        )
        if entry is not None and str(entry.get("status") or "") != "running":
            return
        if entry is None:
            entry = {
                "run_id": run_id,
                "runner_id": runner_id,
                "at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started_at)),
                "account": account,
                "unique_id": uid,
                "targets": list(user.get("targets") or []),
                "friends": [],
            }
            runs.insert(0, entry)
        entry.update({
            "status": "error",
            "reason": "process_error",
            "detail": detail,
            "finished_at": finished_text,
        })
        runs[:] = runs[:KEEP_RUNS_MAX]
        tmp = SEND_LOG + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=1)
        os.replace(tmp, SEND_LOG)
    except Exception as error:
        logger.error("记录账号 %s 的进程错误失败：%s", account, type(error).__name__)


def _run_panel_accounts_separately():
    """Give each panel-run account its own process and independent timeout."""
    if not userData:
        logger.warning("没有可执行的账号，本次跳过（可能是账号缺少 Cookie）")
        return
    try:
        timeout_seconds = max(1, int(os.getenv("PANEL_ACCOUNT_TIMEOUT_SECONDS", "600")))
    except (TypeError, ValueError):
        timeout_seconds = 600

    clear_abort_flag()
    logger.info("开始执行任务；每个抖音号独立计时，上限 %d 秒", timeout_seconds)
    any_failure = False
    for user in userData:
        uid = str(user.get("unique_id") or "").strip()
        account = str(user.get("username") or uid or "抖音账号")
        if not uid:
            logger.warning("账号 %s 缺少 unique_id，本次跳过", account)
            any_failure = True
            continue
        logger.info("开始处理账号 %s（独立上限 %d 秒）", account, timeout_seconds)
        child_environment = os.environ.copy()
        child_environment["RUN_ONLY_ACCOUNTS"] = uid
        child_environment["PANEL_ACCOUNT_CHILD"] = "1"
        options = {}
        if os.name == "nt":
            options["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            options["start_new_session"] = True
        started_at = time.time()
        try:
            process = subprocess.Popen(
                [sys.executable, "main.py"],
                cwd=os.getcwd(),
                env=child_environment,
                **options,
            )
        except Exception as error:
            logger.error("账号 %s 的发送进程启动失败：%s", account, error)
            any_failure = True
            _record_account_process_failure(
                user, started_at, detail="发送进程启动失败，请查看运行日志。"
            )
            continue
        try:
            process.wait(timeout=timeout_seconds)
            logger.info("账号 %s 任务完成（退出码 %s）", account, process.returncode)
            if process.returncode != 0:
                any_failure = True
                _record_account_process_failure(user, started_at, process.returncode)
        except subprocess.TimeoutExpired:
            # If the worker exited right at the deadline, don't convert a completed
            # account run into a timeout.
            if process.poll() is not None:
                logger.info("账号 %s 在超时检查前已完成（退出码 %s）", account, process.returncode)
                if process.returncode != 0:
                    any_failure = True
                    _record_account_process_failure(user, started_at, process.returncode)
                continue
            logger.error("账号 %s 发送超过 %d 秒，停止该账号并继续后续账号", account, timeout_seconds)
            _terminate_account_process(process)
            _record_account_timeout(user, started_at, timeout_seconds)
            any_failure = True
    if any_failure:
        logger.error("本轮至少有一个抖音账号未正常完成；请检查发送记录和运行日志")
        raise SystemExit(1)
