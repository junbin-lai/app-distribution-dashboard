"""飞书群机器人（自建应用）：发布完成通知 + @机器人查状态。

为什么是自建应用而不是群里的「自定义机器人 webhook」：
- webhook 机器人只能往群里发文本/卡片，**收不到 @消息**（无法实现"@机器人查状态"）
- webhook 机器人**不能上传图片**（图片要先调开放平台接口拿 image_key，需要应用凭证）
- 所以走自建应用：开放平台创建应用 → 开机器人能力 → 长连接接收事件（无需公网 IP）。

能力：
1. 发布任务结束（成功/失败/手动停止）→ 往通知群发卡片（结果摘要 + 状态分享图）
2. 群里 @机器人（或私聊机器人）：
   - 「查询 <应用名>」→ 查各平台状态，回复状态分享图
   - 「查询 全部」→ 逐个应用查询并回复
   - 「绑定通知群」/「解绑通知群」→ 设置/取消发布通知的目标群（写回 board.json）
   - 无参数 → 回复用法 + 应用列表

依赖 lark-oapi（官方 SDK）与 Pillow（分享图渲染），缺失时飞书功能自动关闭。
"""
from __future__ import annotations

import io
import json
import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from . import board_config
from .base import StoreError

try:
    import lark_oapi as lark  # noqa: N813
    _LARK_OK = True
except ImportError:
    _LARK_OK = False

# web.py 注入的运行环境（避免 feishu_bot 反向 import web 造成循环依赖）
_ENV: Dict[str, Any] = {
    "board_config_path": "config/board.json",
    # query_status(app_id) -> (statuses, errors)；icon(app_id) -> bytes 或 None
    "query_status": None,
    "icon": None,
    # apps() -> 应用目录列表（[{id,name,package_name,...}]）
    "apps": None,
    "board_url": "http://127.0.0.1:8090/",
    # 日志回调 log(msg)（写看板日志区/服务端日志）
    "log": None,
}

# 连接状态（供 /api/config 展示）
_STATE: Dict[str, Any] = {
    "enabled": False,      # 配置了 app_id/app_secret
    "connected": False,    # 长连接已建立
    "bound_chat": False,   # 已绑定通知群
    "last_error": "",
    "started": False,
}

_MENTION_TOKEN = re.compile(r"@_user_\d+\s*")
_ALL_WORDS = ("全部", "所有", "all", "全部应用")

PLATFORM_NAMES = {
    "huawei": "华为", "oppo": "OPPO", "vivo": "VIVO", "xiaomi": "小米",
    "honor": "荣耀", "google": "Google Play", "apple": "Apple", "qq": "应用宝",
}


def configure(board_config_path: str, query_status: Callable, icon: Callable,
              apps: Callable, board_url: str, log: Optional[Callable] = None) -> None:
    """由 web.run_server 注入运行环境（在启动时调用一次）。"""
    _ENV["board_config_path"] = board_config_path
    _ENV["query_status"] = query_status
    _ENV["icon"] = icon
    _ENV["apps"] = apps
    _ENV["board_url"] = board_url or "http://127.0.0.1:8090/"
    _ENV["log"] = log


def state() -> Dict[str, Any]:
    """连接/绑定状态快照（/api/config 用）。"""
    path = _ENV["board_config_path"]
    fs = board_config.feishu_config(path)
    ready = board_config.feishu_ready(path)
    _STATE["enabled"] = ready
    _STATE["bound_chat"] = bool(fs.get("notify_chat_id"))
    return dict(_STATE)


def _log(msg: str) -> None:
    fn = _ENV.get("log")
    if fn:
        try:
            fn(msg)
        except Exception:
            pass
    else:
        print(f"[飞书] {msg}", flush=True)


# ---- 消息 API 封装 ----

def _client(app_id: str, app_secret: str):
    return lark.Client.builder().app_id(app_id).app_secret(app_secret).build()


def _feishu_call(resp) -> None:
    """统一响应校验：非 0 码抛 StoreError。"""
    if resp is None or not resp.success():
        code = getattr(resp, "code", "?")
        msg = getattr(resp, "msg", "") or ""
        raise StoreError(f"飞书接口失败 code={code} msg={msg}")


def upload_image(app_id: str, app_secret: str, png: bytes) -> str:
    """上传 PNG 拿 image_key（im/v1/images，image_type=message）。"""
    req = (
        lark.im.v1.CreateImageRequest.builder()
        .request_body(
            lark.im.v1.CreateImageRequestBody.builder()
            .image_type("message")
            .image(io.BytesIO(png))
            .build()
        )
        .build()
    )
    resp = _client(app_id, app_secret).im.v1.image.create(req)
    _feishu_call(resp)
    image_key = getattr(getattr(resp, "data", None), "image_key", "")
    if not image_key:
        raise StoreError("飞书未返回 image_key")
    return image_key


def send_message(app_id: str, app_secret: str, chat_id: str,
                 msg_type: str, content: str) -> None:
    """主动发消息到群（receive_id_type=chat_id）。"""
    req = (
        lark.im.v1.CreateMessageRequest.builder()
        .receive_id_type("chat_id")
        .request_body(
            lark.im.v1.CreateMessageRequestBody.builder()
            .receive_id(chat_id)
            .msg_type(msg_type)
            .content(content)
            .build()
        )
        .build()
    )
    resp = _client(app_id, app_secret).im.v1.message.create(req)
    _feishu_call(resp)


def reply_message(app_id: str, app_secret: str, message_id: str,
                  msg_type: str, content: str) -> None:
    """回复某条消息（@指令的应答走这里，天然形成话题串）。"""
    req = (
        lark.im.v1.ReplyMessageRequest.builder()
        .message_id(message_id)
        .request_body(
            lark.im.v1.ReplyMessageRequestBody.builder()
            .msg_type(msg_type)
            .content(content)
            .build()
        )
        .build()
    )
    resp = _client(app_id, app_secret).im.v1.message.reply(req)
    _feishu_call(resp)


# ---- 卡片/文本构造 ----

def _clip(s: str, n: int = 90) -> str:
    s = str(s or "").replace("\n", " ").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def build_publish_card(task: Dict[str, Any], app_name: str, image_key: str = "") -> Dict[str, Any]:
    """发布结束通知卡片（interactive card JSON）。"""
    status = task.get("status")
    version = task.get("version_name") or ""
    results = task.get("results") or []
    errors = task.get("errors") or []
    ok_plats = [PLATFORM_NAMES.get(r.get("platform"), r.get("platform")) for r in results if r.get("ok")]
    bad = list(errors)
    bad += [{"platform": r.get("platform"), "error": r.get("message") or "未成功"} for r in results if not r.get("ok")]

    if status == "done":
        template, icon, word = "green", "✅", "发布完成"
    elif status == "killed":
        template, icon, word = "grey", "⏹", "发布已手动停止"
    else:
        template, icon, word = "red", "❌", "发布失败"

    title = f"{icon} {word} · {app_name}" + (f" v{version}" if version else "")
    fields = [
        {"is_short": True, "text": {"tag": "lark_md", "content": f"**应用**\n{app_name}"}},
        {"is_short": True, "text": {"tag": "lark_md", "content": f"**平台**\n{', '.join(ok_plats) if ok_plats else '—'}"}},
        {"is_short": True, "text": {"tag": "lark_md", "content": f"**操作者**\n{task.get('user') or '—'}"}},
        {"is_short": True, "text": {"tag": "lark_md", "content": f"**结束时间**\n{task.get('finished_at') or '—'}"}},
    ]
    elements: List[Dict[str, Any]] = [{"tag": "div", "fields": fields}]
    if bad:
        lines = "\n".join(
            f"❌ {PLATFORM_NAMES.get(b.get('platform'), b.get('platform'))}：{_clip(b.get('error'))}"
            for b in bad[:6]
        )
        more = f"\n…等共 {len(bad)} 个问题" if len(bad) > 6 else ""
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": f"**失败明细**\n{lines}{more}"}})
    if image_key:
        elements.append({
            "tag": "img",
            "img_key": image_key,
            "alt": {"tag": "plain_text", "content": "各平台当前状态分享图"},
        })
    url = str(_ENV.get("board_url") or "")
    if url:
        elements.append({
            "tag": "action",
            "actions": [{
                "tag": "button",
                "text": {"tag": "plain_text", "content": "打开看板"},
                "type": "primary",
                "url": url,
            }],
        })
    elements.append({"tag": "hr"})
    elements.append({"tag": "note", "elements": [
        {"tag": "plain_text", "content": "App 发布看板 · 发布结束自动通知"},
    ]})
    return {"config": {"wide_screen_mode": True}, "header": {"template": template, "title": {"tag": "plain_text", "content": title}}, "elements": elements}


def _at_prefix(at_user_id: str) -> str:
    """文字消息里的 @某人 前缀（飞书文本消息语法：<at user_id="ou_xxx"></at>）。"""
    uid = str(at_user_id or "").strip()
    return f'<at user_id="{uid}"></at> ' if uid else ""


def build_result_post(app_name: str, image_key: str, at_user_id: str = "") -> Dict[str, Any]:
    """查询结果的富文本（post）消息：标题下方 @回提问的人 + 状态分享图。

    飞书的图片消息（msg_type=image）本身不能带 @，所以结果改用富文本发，
    这样「被@的人收到提醒」和「看到状态图」在同一气泡里。
    富文本的 title 只能放纯文本（放不了可点击的 @），因此 @ 放在标题正下方的
    第一行（紧跟标题、视觉上属于标题区），正文不再重复应用名那句文案。
    """
    uid = str(at_user_id or "").strip()
    content: List[List[Dict[str, Any]]] = []
    if uid:
        content.append([{"tag": "at", "user_id": uid}])  # 标题下方 @提问者
    content.append([{"tag": "img", "image_key": image_key}])
    return {
        "zh_cn": {
            "title": f"{app_name} 发布状态",
            "content": content,
        }
    }


def build_help_text(apps: List[Dict[str, Any]], bound: bool) -> str:
    names = "、".join(str(a.get("name") or a.get("id")) for a in apps[:20]) or "（目录为空）"
    bind_line = "已绑定本群" if bound else "未绑定（@我 发送「绑定通知群」开启发布通知）"
    return (
        "我是 App 发布看板机器人 🤖\n"
        "用法：\n"
        "• @我 查询 <应用名> — 查该应用各平台状态，回复状态分享图\n"
        "• @我 查询 全部 — 逐个应用查询（较慢，耐心等）\n"
        f"• 发布通知群：{bind_line}\n"
        f"可查询应用：{names}"
    )


# ---- @指令处理 ----

def strip_mentions(text: str) -> str:
    """去掉 @_user_N 占位符，留下真正的指令文本。"""
    return _MENTION_TOKEN.sub("", str(text or "")).strip()


# 口语指令里的填充词：先剥掉再做应用匹配
# （「凯迪仕智能发布状态」→「凯迪仕智能」；「查询凯迪仕app版本」→「凯迪仕」；
#   「凯迪仕目前发布版本是？」→「凯迪仕」）
# 注意长词在前（循环按序替换，先吃掉长词避免留下残字）
_FILLER_WORDS = (
    "发布状态", "上架状态", "审核状态", "应用状态", "查询状态",
    "发布版本", "app版本", "版本号", "的版本", "是多少",
    "状态", "进度", "版本", "安卓", "android", "app", "appstore",
    "查询", "查一下", "查查", "看看", "看下", "帮我", "麻烦", "请", "一下",
    "怎么样", "咋样", "多少", "什么", "当前", "现在", "目前", "最新", "显示",
    "上线", "上了", "发布", "是",
    "的", "了", "呢", "吧", "吗", "？", "?", "。", "，", ",", "！",
)


def clean_command_keyword(cmd: str) -> str:
    """剥掉动词前缀与填充词，留下用于匹配应用名的关键词。"""
    s = strip_mentions(cmd)
    s = re.sub(r"^(?:帮我|麻烦|请)\s*(?:查询|查一下|查查|查|看看|看下|query|status)\s*", "", s, flags=re.IGNORECASE)
    s = re.sub(r"^(?:查询|查一下|查查|查|看看|看下|query|status)\s*", "", s, flags=re.IGNORECASE)
    changed = True
    while changed:
        changed = False
        for w in _FILLER_WORDS:
            if w and w in s:
                s = s.replace(w, " ")
                changed = True
    return " ".join(s.split()).strip()


def _name_tokens(name: str) -> List[str]:
    """应用名拆成可匹配的词：中文整段 + 英文单词。

    「凯迪仕智能 (Kaadas Smart)」→ [凯迪仕智能, Kaadas, Smart]
    """
    parts = re.split(r"[\s()（）\-_/]+", str(name or ""))
    return [p for p in parts if len(p) >= 2]


def match_apps(apps: List[Dict[str, Any]], keyword: str) -> List[Dict[str, Any]]:
    """关键词 → 应用匹配（大小写不敏感），四档优先级：

    1. 精确：关键词 == 名称/ID/包名
    2. 模糊：关键词是名称/ID/包名的子串（凯迪、迪仕智能、homeacc 都在这档命中）
    3. 反向：关键词里包含应用名的核心词（「凯迪仕智能发布状态」含「凯迪仕智能」；
       或英数段出现在包名/ID 里，如 kaidishi）
    4. 分词：关键词按空白拆段后每段都出现在名称/ID/包名里（「easy key」→ EasyKey）

    多个命中时返回全部，由调用方提示用户说得更具体。
    """
    kw = str(keyword or "").strip().lower()
    if not kw:
        return []
    exact, fuzzy, reverse, tokened = [], [], [], []
    for a in apps:
        name = str(a.get("name") or "")
        app_id = str(a.get("id") or "")
        pkg = str(a.get("package_name") or "")
        nl, il, pl = name.lower(), app_id.lower(), pkg.lower()
        if kw == nl or kw == il or kw == pl:
            exact.append(a)
            continue
        if kw in nl or kw in il or kw in pl:
            fuzzy.append(a)
            continue
        hit = False
        # 反向：关键词里包含应用名的核心词
        # 中文词按子串（凯迪仕智能 ⊂ 凯迪仕智能发布状态）；英文词按整词边界，
        # 避免 Motorola Home Secure 的「Home」误命中「homeaccess」
        for tok in _name_tokens(name):
            t = tok.lower()
            if re.search(r"[\u4e00-\u9fff]", t):
                if t in kw:
                    reverse.append(a)
                    hit = True
                    break
            else:
                if re.search(r"(?<![a-z0-9])" + re.escape(t) + r"(?![a-z0-9])", kw):
                    reverse.append(a)
                    hit = True
                    break
        if not hit:
            # 包名/ID 反向：关键词里的英数段（≥4 位）出现在包名或 ID 里（如 kaidishi）
            for seg in re.findall(r"[a-z0-9]{4,}", kw):
                if seg in pl or seg in il:
                    reverse.append(a)
                    hit = True
                    break
        if not hit:
            # 分词：每一段都出现在名称/ID/包名里（easy key → EasyKey）
            parts = kw.split()
            if parts and all((p in nl or p in il or p in pl) for p in parts):
                tokened.append(a)
    for lst in (exact, fuzzy, reverse, tokened):
        if lst:
            return lst
    # 兜底1：截尾回退——填充词表没料到的口语尾巴（「凯迪仕是」「凯迪仕哈」），
    # 从结尾逐字截短重试，取能命中应用的最长前缀（只对含中文的关键词做）
    if re.search(r"[\u4e00-\u9fff]", kw):
        for end in range(len(kw), 1, -1):
            prefix = kw[:end]
            hits = [a for a in apps
                    if prefix in str(a.get("name") or "").lower()
                    or prefix in str(a.get("id") or "").lower()
                    or prefix in str(a.get("package_name") or "").lower()]
            if hits:
                return hits
    # 兜底2：错别字相似度（「KKHOEM」→KKhome、「凯迪士智能」→凯迪仕智能）
    # 与名称/ID/包名/名称分词逐一算相似度，最高分 ≥0.72 才算命中；
    # 有多个分数接近的候选时全部返回，让用户选
    if len(kw) >= 4:
        import difflib
        scored = []
        for a in apps:
            name = str(a.get("name") or "")
            cands = [name.lower(), str(a.get("id") or "").lower(),
                     str(a.get("package_name") or "").lower()]
            cands += [t.lower() for t in _name_tokens(name)]
            best = max((difflib.SequenceMatcher(None, kw, c).ratio() for c in cands if c), default=0.0)
            scored.append((best, a))
        if scored:
            scored.sort(key=lambda x: -x[0])
            if scored[0][0] >= 0.72:
                top = scored[0][0]
                return [a for s, a in scored if s >= top - 0.08]
    return []


def _list_apps() -> List[Dict[str, Any]]:
    """取应用目录（由 web.run_server 注入的 apps() 读取，避免循环 import）。"""
    fn = _ENV.get("apps")
    if fn is None:
        return []
    try:
        return fn() or []
    except Exception:
        return []


def handle_command_text(
    text: str,
    chat_id: str,
    message_id: str,
    reply,  # callable(msg_type, content) — 测试时可注入
    config_path: str,
    at_user_id: str = "",  # 提问者 open_id：群里回复时 @回他（私聊传空）
) -> None:
    """解析并执行一条指令文本（@占位符已剥离或原样均可）。

    reply(msg_type, content) 用于回消息；拆出来是为了可单测。
    at_user_id 非空时所有文字回复都会 @ 回提问的人。
    """
    cmd = strip_mentions(text)
    apps = _list_apps()
    fs = board_config.feishu_config(config_path)
    bound = bool(fs.get("notify_chat_id"))

    def _say(msg: str) -> None:
        """群聊里回话时带上 @提问者（私聊 at_user_id 为空则不带）。"""
        reply("text", json.dumps({"text": _at_prefix(at_user_id) + msg}, ensure_ascii=False))

    if not cmd or cmd in ("帮助", "help", "?", "？", "用法"):
        _say(build_help_text(apps, bound))
        return

    if cmd in ("绑定通知群", "绑定群", "设为通知群"):
        ok = board_config.save_feishu_fields(config_path, {"notify_chat_id": chat_id})
        if ok:
            _log("通知群已绑定: " + chat_id)
            _say(f"✅ 已把本群绑定为发布通知群（chat_id={chat_id}），之后发布结束会在这里通知。")
        else:
            _say("❌ 写入配置失败，请检查 config/board.json 是否可写。")
        return

    if cmd in ("解绑通知群", "解绑群"):
        ok = board_config.save_feishu_fields(config_path, {"notify_chat_id": ""})
        if ok:
            _log("通知群已解绑")
            _say("已取消本群的通知绑定。")
        else:
            _say("❌ 写入配置失败。")
        return

    # 查询指令：剥掉「查询/帮我/发布状态」等口语词后按应用名匹配
    keyword = clean_command_keyword(cmd)

    if not keyword:
        _say(build_help_text(apps, bound))
        return
    if keyword.lower() in _ALL_WORDS:
        targets = apps
        if not targets:
            _say("应用目录为空。")
            return
        _say(f"开始查询全部 {len(targets)} 个应用，逐个回复，请稍候…")
        for a in targets:
            _run_query_and_reply(a, reply, config_path, at_user_id)
        return

    hits = match_apps(apps, keyword)
    if not hits:
        names = "、".join(str(a.get("name") or a.get("id")) for a in apps[:20])
        _say(f"没找到「{keyword}」对应的应用。可查询：{names}")
        return
    if len(hits) > 1:
        names = "、".join(str(a.get("name") or a.get("id")) for a in hits[:10])
        _say(f"「{keyword}」匹配到多个应用：{names}，请说得更具体一点。")
        return
    _run_query_and_reply(hits[0], reply, config_path, at_user_id)


def _run_query_and_reply(app: Dict[str, Any], reply, config_path: str, at_user_id: str = "") -> None:
    """查询单个应用状态 → 渲染分享图 → 回复（群里 @回提问的人）。"""
    app_id = app.get("id")
    name = str(app.get("name") or app_id)
    query = _ENV.get("query_status")
    icon_fn = _ENV.get("icon")
    fs = board_config.feishu_config(config_path)
    app_id_key, app_secret = fs.get("app_id", ""), fs.get("app_secret", "")

    def _say(msg: str) -> None:
        reply("text", json.dumps({"text": _at_prefix(at_user_id) + msg}, ensure_ascii=False))

    from .share_image import render_status_image
    try:
        _say(f"正在查询 {name} 各平台状态…")
        statuses, errors = query(app_id) if query else ([], [])
        # 不是每个应用都上架所有平台——没配凭证的平台查不到属正常，
        # 不在群里报「部分平台查询失败」，只展示已支持平台（分享图里就只有它们）；
        # 全部平台都没结果时才提示
        if not statuses:
            _say(f"{name} 没有查到任何平台状态（或各平台均未配置该应用凭证）。")
            return
        checked_at = next((s.get("checked_at") or "" for s in statuses), "")
        icon_bytes = None
        if icon_fn:
            try:
                icon_bytes = icon_fn(app_id)
            except Exception:
                icon_bytes = None
        png = render_status_image(name, statuses, icon_bytes=icon_bytes, checked_at=checked_at)
        if not png:
            _say(f"{name} 分享图生成失败（Pillow 未安装？）")
            return
        image_key = upload_image(app_id_key, app_secret, png)
        # 结果用富文本消息：同一条里 @回提问者 + 状态分享图（图片消息带不了 @）
        try:
            reply("post", json.dumps(build_result_post(name, image_key, at_user_id), ensure_ascii=False))
        except Exception as e:
            # 富文本发送失败兜底：先补一条带 @ 的文字，再发普通图片
            _log(f"富文本结果发送失败，回退文字+图片: {e}")
            _say(f"{name} 各平台状态 👇")
            reply("image", json.dumps({"image_key": image_key}))
    except StoreError as e:
        _say(f"❌ 查询/发送失败：{_clip(str(e), 120)}")
    except Exception as e:  # 兜底：机器人线程不能因单条指令崩掉
        _say(f"❌ 异常：{type(e).__name__}: {_clip(str(e), 120)}")


# ---- 长连接事件入口 ----

# @所有人的判定：文本里的 @_all 占位符，或 mentions 里 key='@_all'
_ALL_MENTION_KEYS = ("@_all", "_all")


def _mentions_all(text: str, mentions) -> bool:
    """消息是否 @了所有人（@所有人 不触发机器人）。

    飞书 @所有人 的可靠标记是文本里的 @_all 占位符，以及 mentions 里对应项的
    key='@_all'（该项 id 为空）。只认这两个标记、不按 name 判断，
    避免把名字恰好叫「所有人」的同事误判成 @所有人。
    """
    if text and "@_all" in text:
        return True
    for m in (mentions or []):
        try:
            key = str(getattr(m, "key", "") or "").strip().lower()
        except Exception:
            continue
        if key in _ALL_MENTION_KEYS:
            return True
    return False


def _on_message(data) -> None:
    """im.message.receive_v1 事件回调（群 @ 与私聊都会进这里）。"""
    try:
        msg = getattr(data, "event", None) and data.event.message
        if msg is None:
            return
        chat_id = msg.chat_id or ""
        message_id = msg.message_id or ""
        chat_type = msg.chat_type or ""
        if (msg.message_type or "") != "text":
            # 非文本消息（表情/图片等）只在群里回应，私聊也提示
            if chat_id:
                pass  # 静默忽略，避免打扰
            return
        content = msg.content or "{}"
        try:
            text = str(json.loads(content).get("text") or "")
        except ValueError:
            text = content
        # @所有人 不触发机器人：飞书 @所有人 会以 @_all 占位符出现在文本里，
        # mentions 里对应项的 key 也是 @_all（name 通常为「所有人」）
        if _mentions_all(text, getattr(msg, "mentions", None)):
            _log(f"忽略 @所有人 消息（不触发机器人）chat={chat_id}")
            return
        # 提问者 open_id：群聊里回复时 @回他（私聊不需要 @）
        at_user_id = ""
        if chat_type and chat_type != "p2p":
            try:
                at_user_id = str(getattr(getattr(data.event.sender, "sender_id", None),
                                         "open_id", "") or "")
            except Exception:
                at_user_id = ""
        _log(f"收到指令 chat={chat_id} type={chat_type} text={text!r} 提问者={at_user_id or '-'}")
        # 指令处理可能很慢（查询全平台状态），放线程里跑，不阻塞事件循环
        fs = board_config.feishu_config(_ENV["board_config_path"])
        app_id_key, app_secret = fs.get("app_id", ""), fs.get("app_secret", "")

        def reply(msg_type: str, content_json: str) -> None:
            if not message_id:
                send_message(app_id_key, app_secret, chat_id, msg_type, content_json)
            else:
                reply_message(app_id_key, app_secret, message_id, msg_type, content_json)

        threading.Thread(
            target=handle_command_text,
            args=(text, chat_id, message_id, reply, _ENV["board_config_path"], at_user_id),
            daemon=True, name="feishu-cmd",
        ).start()
    except Exception as e:
        _log(f"事件处理异常: {e}")


def start() -> bool:
    """启动长连接（守护线程）。未配置/缺依赖返回 False。"""
    if _STATE.get("started"):
        return True
    if not _LARK_OK:
        _STATE["last_error"] = "未安装 lark-oapi（pip install lark-oapi）"
        _log(_STATE["last_error"])
        return False
    path = _ENV["board_config_path"]
    fs = board_config.feishu_config(path)
    app_id_key, app_secret = fs.get("app_id", ""), fs.get("app_secret", "")
    if not (app_id_key and app_secret):
        _STATE["last_error"] = "board.json 未配置 feishu.app_id/app_secret"
        return False
    _STATE["started"] = True
    _STATE["enabled"] = True

    def _ws_conn_dead(cli) -> bool:
        """连接是否已断（供看门狗判断；websockets 新旧版本属性兼容）。"""
        conn = getattr(cli, "_conn", None)
        if conn is None:
            return True
        try:
            state = getattr(conn, "state", None)
            if state is not None:
                return str(getattr(state, "name", state)) != "OPEN"
        except Exception:
            pass
        try:
            return bool(getattr(conn, "closed"))  # 旧版 websockets 属性
        except Exception:
            return False

    def _run() -> None:
        import asyncio
        # ⚠️ 关键修复（2026-09-23 实测踩坑）：lark_oapi.ws 模块 import 时在【主线程】
        # 抓了事件循环（模块级 loop = get_event_loop()），而长连接跑在本守护线程——
        # 平时没事，一旦断线重连，任务会绑到不同的 loop 上直接崩：
        #   RuntimeError: Task got Future attached to a different loop
        # 之后 start() 还阻塞在 _select() 里，连接死了也永远不会再重连。
        # 修复：给本线程建专属事件循环，并把 ws 模块的模块级 loop 改指过来，
        # 让 connect / ping / 断线重连全部落在同一个循环、同一个线程上。
        while True:
            bot_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(bot_loop)
            lark.ws.loop = bot_loop
            dispatcher = (
                lark.EventDispatcherHandler.builder("", "")
                .register_p2_im_message_receive_v1(_on_message)
                .build()
            )
            cli = lark.ws.Client(
                app_id_key, app_secret,
                event_handler=dispatcher,
                log_level=lark.LogLevel.INFO,
                auto_reconnect=True,
            )

            def _on_reconnected():
                _STATE.update({"connected": True, "last_error": ""})
                _log("长连接已建立")

            def _on_reconnecting():
                _STATE.update({"connected": False})
                _log("长连接断开，SDK 自动重连中…")

            cli.on_reconnected = _on_reconnected
            cli.on_reconnecting = _on_reconnecting
            _STATE.update({"connected": True, "last_error": ""})
            _log(f"飞书机器人已启动（app_id={app_id_key[:8]}…），等待 @指令")

            # 看门狗：SDK 自身重连失灵（连接持续断开 ~5 分钟）时强制重建客户端，
            # 保证机器人不会悄悄死掉
            stop_watchdog = threading.Event()

            def _watchdog():
                dead_checks = 0
                while not stop_watchdog.wait(15):
                    if _ws_conn_dead(cli):
                        dead_checks += 1
                    else:
                        dead_checks = 0
                    if dead_checks >= 20:  # 20 次 × 15s ≈ 5 分钟
                        _log("长连接持续中断约 5 分钟，强制重建机器人客户端…")
                        _STATE["connected"] = False
                        try:
                            bot_loop.call_soon_threadsafe(bot_loop.stop)
                        except RuntimeError:
                            pass
                        return

            threading.Thread(target=_watchdog, daemon=True, name="feishu-ws-watchdog").start()
            try:
                cli.start()  # 阻塞；正常断线由 SDK 在同一循环内自动重连
            except Exception as e:
                _STATE["connected"] = False
                _STATE["last_error"] = str(e)
                _log(f"长连接异常退出，10s 后重建: {e}")
            finally:
                stop_watchdog.set()
                _STATE["connected"] = False
            time.sleep(10)  # 重建间隔，避免疯狂重试

    threading.Thread(target=_run, daemon=True, name="feishu-ws").start()
    return True


# ---- 发布结束通知（web.py 任务完成钩子调用） ----

def classify_publish_outcome(task: Dict[str, Any]) -> str:
    """把发布任务终态归类成通知场景。

    判定口径（2026-09-23 与产品确认）：
    - 勾选的平台全部「真实送审成功」→ all_success（国内平台提交即提审；
      Google 必须勾了「自动送审」且提交成功才算送审，只存草稿不算）
    - 部分平台成功 → partial_success
    - 全部失败 → failed
    - 被手动停止 → killed
    - 全部平台都只是草稿/未送审（如只发了 Google 且没勾自动送审）→ draft_only
    dry-run 任务不进这里（调用方已过滤）。
    """
    if task.get("status") == "killed":
        return "killed"
    results = task.get("results") or []
    errors = task.get("errors") or []
    ok_plats = [r.get("platform") for r in results if r.get("ok")]
    bad_plats = [e.get("platform") for e in errors]
    bad_plats += [r.get("platform") for r in results if not r.get("ok")]
    # Google 未自动送审=只存草稿：从"成功"里剔除，归入草稿类
    auto_review = bool(task.get("auto_review"))
    draft_plats = []
    if "google" in ok_plats and not auto_review:
        ok_plats.remove("google")
        draft_plats.append("google")
    if not ok_plats and not bad_plats and draft_plats:
        return "draft_only"  # 只有草稿，没有任何平台送审
    if bad_plats or draft_plats:
        return "partial_success" if ok_plats else "failed"
    return "all_success"


# 通知场景开关：默认只在「勾选平台全部送审成功」时通知（2026-09-23 产品要求），
# 其余场景预留字段，以后要开改成 true 重启即可
_NOTIFY_SCENARIOS_DEFAULT = {
    "all_success": True,      # 正式发布·勾选平台全部提审成功
    "partial_success": False,  # 部分平台成功（其余失败/仅草稿）
    "failed": False,           # 全部平台失败
    "killed": False,           # 被手动停止
    "draft_only": False,       # 只存了草稿未送审（如 Google 未勾自动送审）
}


def notify_publish_finished(task: Dict[str, Any], app_name: str) -> None:
    """发布任务结束后发通知（应在独立线程调用，本函数不再起线程）。

    是否通知由 board.json feishu.notify_on 按场景控制（默认只开 all_success：
    勾选的平台全部真实送审成功才发）。通知内容：结果卡片（含摘要+分享图）。
    """
    path = _ENV["board_config_path"]
    fs = board_config.feishu_config(path)
    chat_id = fs.get("notify_chat_id") or ""
    app_id_key, app_secret = fs.get("app_id", ""), fs.get("app_secret", "")
    if not (app_id_key and app_secret):
        return
    if not _LARK_OK:
        _log("跳过飞书通知：未安装 lark-oapi")
        return
    if fs.get("notify_on_publish") is False:
        return
    if not chat_id:
        _log("跳过飞书通知：未绑定通知群（群里 @机器人 发送「绑定通知群」）")
        return
    if task.get("dry_run"):
        return
    # 按场景过滤（notify_on 未配置时用默认值：只有 all_success 通知）
    scenario = classify_publish_outcome(task)
    notify_on = fs.get("notify_on") if isinstance(fs.get("notify_on"), dict) else {}
    enabled = {**_NOTIFY_SCENARIOS_DEFAULT, **{k: v for k, v in notify_on.items()
                                               if k in _NOTIFY_SCENARIOS_DEFAULT}}
    if not enabled.get(scenario, False):
        _log(f"跳过飞书通知：场景 {scenario} 未开启（board.json feishu.notify_on.{scenario}）")
        return
    try:
        time.sleep(5)  # 等平台状态接口稍缓一口气，刚提交的版本能查到
        image_key = ""
        query = _ENV.get("query_status")
        icon_fn = _ENV.get("icon")
        if query:
            try:
                statuses, _errors = query(task.get("app_id"))
                if statuses:
                    from .share_image import render_status_image
                    checked_at = next((s.get("checked_at") or "" for s in statuses), "")
                    icon_bytes = None
                    if icon_fn:
                        try:
                            icon_bytes = icon_fn(task.get("app_id"))
                        except Exception:
                            icon_bytes = None
                    png = render_status_image(app_name, statuses, icon_bytes=icon_bytes, checked_at=checked_at)
                    if png:
                        image_key = upload_image(app_id_key, app_secret, png)
            except Exception as e:
                _log(f"通知分享图生成失败（不影响卡片发送）: {e}")
        card = build_publish_card(task, app_name, image_key)
        send_message(app_id_key, app_secret, chat_id, "interactive", json.dumps(card, ensure_ascii=False))
        _log(f"已发送发布通知到群 {chat_id}")
    except Exception as e:
        _log(f"飞书通知发送失败: {e}")
