"""Web 可视化看板：应用分类 / 一键发布 / 状态查询。

纯标准库实现（http.server），不依赖 Flask/FastAPI。
启动：appstore web --port 8090 [--host 0.0.0.0] [--board-config config/board.json]
API：
  GET  /                      看板页面（未登录时前端显示登录层）
  GET  /api/apps              应用目录（分类+应用）
  GET  /api/platforms         平台注册表
  GET  /api/config            看板配置信息（含登录/飞书状态）
  GET  /api/session           当前登录会话（无需登录）
  GET  /api/share-image       状态分享图 PNG（登录后可用）
  GET  /api/validate          校验凭证（返回 ok 列表）
  POST /api/login             {username, password} → 会话 Cookie
  POST /api/logout            退出登录
  POST /api/publish           {app_id, platform|all, dry_run}（admin）
  POST /api/status            {app_id, platform?}（viewer 可用）
"""
from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
import urllib.request
import uuid
import webbrowser
from http import cookies as http_cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from .apk_meta import extract_apk_icon, parse_build, parse_apk
from .base import StoreError, TaskKilledError
from .catalog import get_catalog
from .config import load_credentials
from .models import Platform
from .registry import get_adapter, list_platforms
from . import board_config
from . import feishu_bot

PLATFORM_VALUES = {p.value for p in Platform}

PUBLISH_PLATFORMS = {p for p in PLATFORM_VALUES if p != "apple"}

# ---- 任务持久化 ----
# APPSTORE_HISTORY_FILE 供测试隔离：跑测试服务/模拟脚本时指到临时文件，
# 避免误写误删真实发布历史（config/tasks_history.json）
_HISTORY_FILE = os.environ.get("APPSTORE_HISTORY_FILE") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "config", "tasks_history.json")
_HISTORY_MAX = 50

_TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")


def _load_index_html() -> str:
    """优先读外部模板文件；缺失回退内嵌。"""
    p = os.path.join(_TEMPLATE_DIR, "index.html")
    try:
        return open(p, encoding="utf-8").read()
    except OSError:
        return INDEX_HTML_FALLBACK


INDEX_HTML_FALLBACK = r"""<html><body><h1>模板文件缺失</h1><p>请创建 app_store/templates/index.html</p></body></html>
"""

INDEX_HTML = _load_index_html()


def _json_response(handler: BaseHTTPRequestHandler, payload: Any, status: int = 200) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _html_response(handler: BaseHTTPRequestHandler, html: str, status: int = 200) -> None:
    body = html.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


_ICON_EXTS = ((".png", "image/png"), (".webp", "image/webp"), (".jpg", "image/jpeg"), (".jpeg", "image/jpeg"))


def _bytes_response(handler: BaseHTTPRequestHandler, body: bytes, ctype: str, status: int = 200) -> None:
    handler.send_response(status)
    handler.send_header("Content-Type", ctype)
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "public, max-age=86400")
    handler.end_headers()
    handler.wfile.write(body)


def _http_get_bytes(url: str, timeout: int = 30) -> tuple:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read()
    ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip() or "application/octet-stream"
    return data, ctype


def _play_store_icon(package_name: str) -> tuple:
    """Google Play 商店页抓应用图标（read-only，页面服务端渲染含图标 img）。"""
    from urllib.parse import quote
    url = "https://play.google.com/store/apps/details?id=" + quote(package_name)
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9"})
    with urllib.request.urlopen(req, timeout=25) as resp:
        html = resp.read().decode("utf-8", "replace")
    m = re.search(r'https://play-lh\.googleusercontent\.com/[A-Za-z0-9_-]+', html)
    if not m:
        raise StoreError("Play 商店页未找到图标 URL")
    return _http_get_bytes(m.group(0))


_BUILDS_GRACE_SECS = 24 * 3600  # 刚上传尚未绑定的文件保护期


def _builds_root() -> str:
    """安装包落盘根目录（上传与清理共用，避免两处路径不一致）。"""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "builds")


def _cleanup_unreferenced_builds(catalog) -> Dict[str, Any]:
    """删除 builds/{apk,aab}/ 下未被任何应用引用的安装包。

    只扫 builds/ 两个子目录（catalog 里可能引用 Downloads 等外部路径，绝不碰）；
    修改时间在保护期内的文件跳过（刚上传、尚未绑定到应用的新包不误删）。
    任何失败只跳过该文件，不影响调用方。
    相对路径基准与 catalog.json 所在目录（catalog.base_dir）保持一致，
    避免服务器从其他工作目录启动时误判引用、误删在用安装包。
    """
    base_dir = str(getattr(catalog, "base_dir", "") or os.path.dirname(os.path.abspath(Handler.catalog_path)))
    builds_root = _builds_root()
    refs = set()
    for a in catalog.all_apps():
        for f in ("apk_build", "aab_build"):
            v = str(a.get(f) or "").strip()
            if v:
                refs.add(os.path.normpath(v if os.path.isabs(v) else os.path.join(base_dir, v)))
    removed, freed = [], 0
    now = time.time()
    for sub in ("apk", "aab"):
        d = os.path.join(builds_root, sub)
        if not os.path.isdir(d):
            continue
        for fn in os.listdir(d):
            if not fn.lower().endswith("." + sub):
                continue
            p = os.path.normpath(os.path.join(d, fn))
            if p in refs:
                continue
            try:
                if now - os.path.getmtime(p) < _BUILDS_GRACE_SECS:
                    continue  # 保护期内的新文件
                freed += os.path.getsize(p)
                os.remove(p)
                removed.append(fn)
            except OSError:
                pass
    return {"removed": removed, "freed": freed}


_BODY_MAX_BYTES = 10 * 1024 * 1024  # JSON 请求体上限（上传接口走 multipart，不受此限）


def _read_body(handler: BaseHTTPRequestHandler) -> Dict[str, Any]:
    """读取 JSON 请求体；格式非法时抛 StoreError（由 do_POST 统一转成 400 JSON）。"""
    raw_len = handler.headers.get("Content-Length") or "0"
    try:
        length = int(raw_len)
    except (TypeError, ValueError):
        raise StoreError(f"非法的 Content-Length: {raw_len}")
    if length < 0:
        raise StoreError("非法的 Content-Length")
    if length > _BODY_MAX_BYTES:
        raise StoreError(f"请求体过大（上限 {_BODY_MAX_BYTES // 1024 // 1024}MB）")
    if length == 0:
        return {}
    try:
        data = json.loads(handler.rfile.read(length).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise StoreError(f"请求体不是合法 JSON: {e}")
    if not isinstance(data, dict):
        raise StoreError("请求体须为 JSON 对象")
    return data


_UPLOAD_MAX_BYTES = 1024 * 1024 * 1024  # 单文件上传上限 1GB（email 解析需整包读入内存）


def _parse_multipart_upload(handler: BaseHTTPRequestHandler) -> tuple:
    """解析 multipart/form-data 上传（cgi 模块已在 Python 3.13 移除，改用 email 解析）。

    返回 (ftype, filename, file_bytes)；非法输入抛 StoreError。
    """
    from email.parser import BytesParser
    from email.policy import default as email_policy

    ctype = handler.headers.get("Content-Type", "")
    if "multipart/form-data" not in ctype:
        raise StoreError("Content-Type 须为 multipart/form-data")
    raw_len = handler.headers.get("Content-Length") or "0"
    try:
        length = int(raw_len)
    except (TypeError, ValueError):
        raise StoreError(f"非法的 Content-Length: {raw_len}")
    if length <= 0:
        raise StoreError("上传内容为空")
    if length > _UPLOAD_MAX_BYTES:
        raise StoreError(f"文件过大（上限 {_UPLOAD_MAX_BYTES // 1024 // 1024}MB）")
    body = handler.rfile.read(length)
    # email 解析器需要完整报文：把 Content-Type 头拼回请求体前再解析
    raw = b"Content-Type: " + ctype.encode("latin-1") + b"\r\n\r\n" + body
    try:
        msg = BytesParser(policy=email_policy).parsebytes(raw)
    except Exception as e:
        raise StoreError(f"上传解析失败: {e}")
    ftype, filename, data = "", "", b""
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition") or ""
        if name == "type":
            ftype = (part.get_payload(decode=True) or b"").decode("utf-8", "replace").strip().lower()
        elif name == "file":
            filename = part.get_filename() or ""
            data = part.get_payload(decode=True) or b""
    if ftype not in ("aab", "apk"):
        raise StoreError("缺少或错误的 type 参数（aab|apk）")
    if not filename or not data:
        raise StoreError("未收到文件")
    return ftype, filename, data




# ---- 任务系统（异步发布 + 进度）----
_task_lock = threading.Lock()
_TASKS: dict = {}
_PENDING_PATHS: dict = {}
_FINISHED_TASKS_MAX = 200  # 内存中最多保留的已完成任务条数（超出按完成时间淘汰最旧）


def _history_path():
    p = os.path.abspath(_HISTORY_FILE)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return p


def _load_history():
    """启动时把 config/tasks_history.json 里的历史任务加载进 _TASKS（供前端展示）。"""
    try:
        with open(_history_path(), "r", encoding="utf-8") as f:
            items = json.load(f)
    except Exception:
        return
    if not isinstance(items, list):
        return
    with _task_lock:
        for it in items:
            if not isinstance(it, dict) or not it.get("id") or it.get("dry_run"):
                continue
            tid = it["id"]
            if tid not in _TASKS:
                _TASKS[tid] = it


def _save_history():
    """把最近的稳定任务（done/error/已取消）写盘，最多保留 _HISTORY_MAX 条；dry-run 校验任务不入历史。"""
    try:
        with _task_lock:
            stable = [
                v for v in _TASKS.values()
                if v.get("status") in ("done", "error", "killed")
                and not v.get("dry_run")
            ]
            stable.sort(key=lambda x: x.get("finished_at", ""), reverse=True)
            items = stable[:_HISTORY_MAX]
        tmp = _history_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=1)
        os.replace(tmp, _history_path())
    except Exception:
        pass


def _new_task(app_id, platform, dry_run, apk_path="", aab_path="", version_name=""):
    tid = uuid.uuid4().hex[:12]
    with _task_lock:
        _TASKS[tid] = {
            "id": tid, "app_id": app_id, "platform": platform, "dry_run": dry_run,
            "version_name": version_name,
            "status": "running", "progress": 0, "stage": "准备中",
            "steps": [], "results": [], "errors": [],
            "upload": None,
            # 并行发布：按平台分组的步骤/状态/上传进度（前端点击芯片切换查看）
            "psteps": {}, "pstate": {}, "uploads": {}, "kill_req": False,
        }
        _PENDING_PATHS[tid] = {"apk": apk_path, "aab": aab_path}
    return tid


def _step(tid, msg, level="info"):
    ts = time.strftime("%H:%M:%S")
    with _task_lock:
        if tid in _TASKS:
            _TASKS[tid]["steps"].append(f"[{ts}] {msg}")


def _update(tid, **kw):
    with _task_lock:
        if tid in _TASKS:
            _TASKS[tid].update(kw)
            if kw.get("status") in ("done", "error", "killed"):
                if not _TASKS[tid].get("finished_at"):
                    _TASKS[tid]["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                # 任务结束后参数已无用处，及时释放引用
                _PENDING_PATHS.pop(tid, None)
                # 已完成任务数量超上限时淘汰最旧的，避免内存无限增长
                finished = [k for k, v in _TASKS.items()
                            if v.get("status") in ("done", "error", "killed")]
                if len(finished) > _FINISHED_TASKS_MAX:
                    finished.sort(key=lambda k: _TASKS[k].get("finished_at", ""))
                    for k in finished[: len(finished) - _FINISHED_TASKS_MAX]:
                        _TASKS.pop(k, None)
    # _save_history 有自己的锁，在外部调用避免死锁
    if kw.get("status") in ("done", "error", "killed"):
        _save_history()
        # 飞书群通知（异步线程，失败不影响任务结果；dry-run 不通知）
        if not kw.get("_no_notify") and not _TASKS.get(tid, {}).get("notify_started"):
            with _task_lock:
                t = _TASKS.get(tid)
                if t is not None and not t.get("dry_run"):
                    t["notify_started"] = True
                    notify_needed = True
                else:
                    notify_needed = False
            if notify_needed:
                threading.Thread(target=_notify_feishu_publish, args=(tid,), daemon=True).start()


def _pstep(tid: str, plat: str, msg: str, level: str = "info"):
    """按平台分组的步骤日志（并行发布时前端切换查看对应平台）。"""
    ts = time.strftime("%H:%M:%S")
    with _task_lock:
        t = _TASKS.get(tid)
        if t is not None:
            t.setdefault("psteps", {}).setdefault(plat, []).append(f"[{ts}] {msg}")


def _pstate(tid: str, plat: str, st: str):
    """平台级状态：pending / running / done / error。"""
    with _task_lock:
        t = _TASKS.get(tid)
        if t is not None:
            t.setdefault("pstate", {})[plat] = st


def _pupload(tid: str, plat: str, sent: int, total: int):
    """按平台的上传字节进度。"""
    with _task_lock:
        t = _TASKS.get(tid)
        if t is not None:
            t.setdefault("uploads", {})[plat] = {
                "sent": sent, "total": total, "pct": f"{100*sent/(total or 1):.0f}%"}


def _check_kill(tid: str):
    '''停止发布：任务被标记 kill_req 后，由各平台线程的回调调用并抛异常，中断上传/流程。'''
    with _task_lock:
        t = _TASKS.get(tid)
        if t is not None and t.get("kill_req"):
            raise TaskKilledError()


def _record_error(tid: str, plat: str, targets, msg: str, label: str = "错误"):
    """平台线程失败：记录错误 + 推进整体进度（锁外再写状态，Lock 不可重入）。
    label 用于区分普通错误与手动停止（传空串则只显示 msg 本身）。"""
    with _task_lock:
        t = _TASKS.get(tid)
        if t is not None:
            t.setdefault("errors", []).append({"platform": plat, "error": msg})
            done_n = len(t.get("results") or []) + len(t.get("errors") or [])
            t["progress"] = min(95, 15 + int(75 * done_n / max(1, len(targets))))
    _pstate(tid, plat, "error")
    text = f"{label} {msg}" if label else msg
    _step(tid, f"  {plat}: {text}", "error")
    _pstep(tid, plat, text, "error")


def _publish_worker(tid: str):
    from .catalog import get_catalog
    from .config import load_credentials
    from .registry import get_adapter
    from .models import Platform

    with _task_lock:
        t = _TASKS.get(tid)
        if not t:
            return
        app_id, platform, dry_run = t["app_id"], t["platform"], t["dry_run"]
        params = dict(_PENDING_PATHS.get(tid, {}))
    try:
        catalog = get_catalog(Handler.catalog_path)
        creds = load_credentials(Handler.credentials_path)
        release = catalog.to_release(
            app_id=app_id,
            version_name=params.get("version_name", ""),
            version_code=params.get("version_code"),
            platform="google" if platform == "google" else "cn",
            release_notes=params.get("release_notes", ""),
            track=params.get("track", ""),
            apk_path=params.get("apk", ""),
            aab_path=params.get("aab", ""),
            online_time=params.get("online_time"),
        )
        release.metadata["auto_review"] = bool(params.get("auto_review"))
        release.metadata["release_mode"] = params.get("release_mode") or "staged"
        release.metadata["release_notes_l10n"] = params.get("release_notes_l10n") or {}
        _step(tid, f"包名: {release.package_name} v{release.version_name or '?'}")
        if release.version_name:
            _update(tid, version_name=release.version_name)  # 实际发布的版本号（含 catalog 回落）
        _update(tid, progress=8, stage="读取配置")
        if platform == "all":
            targets = [k for k in creds if k in PUBLISH_PLATFORMS]
            display = "全部已配平台"
        elif "," in platform:
            parts = [p.strip() for p in platform.split(",") if p.strip() in PUBLISH_PLATFORMS]
            targets = [p for p in parts if p in creds]
            display = ", ".join(targets)
        else:
            targets = [platform] if platform in PUBLISH_PLATFORMS else [p for p in [platform] if p in creds]
            display = platform
        _step(tid, f"目标平台: {display}")
        # 回写解析后的平台列表（"all" 时前端芯片/标题能显示具体平台）
        _update(tid, platform=",".join(targets))
        # 并行发布：每个平台一个线程同时执行各自的发布流程，互不阻塞
        with _task_lock:
            if tid in _TASKS:
                _TASKS[tid]["psteps"] = {k: [] for k in targets}
                _TASKS[tid]["pstate"] = {k: "pending" for k in targets}
                _TASKS[tid]["uploads"] = {}
        _update(tid, progress=15, stage="并行发布中" if len(targets) > 1 else (f"发布 {targets[0]}" if targets else "无目标平台"))

        import copy as _copy
        from concurrent.futures import ThreadPoolExecutor

        def _run_one(key: str):
            # 每平台独立 Release 副本：上传进度/步骤回调互不覆盖（共享对象在并行下会竞争）
            rel = _copy.deepcopy(release)
            _pstate(tid, key, "running")
            _step(tid, f"→ {key}: 开始")
            _pstep(tid, key, "开始发布")
            try:
                adapter = get_adapter(key, creds)
                problems = adapter.check()
                if problems:
                    raise StoreError("；".join(problems))
                _step(tid, f"  {key}: 凭证校验通过")
                _pstep(tid, key, "凭证校验通过")
                # 上传进度/步骤回调：写入按平台分组的字段（前端按选中平台展示）；
                # 回调前先检查停止请求（kill_req），被停止时抛 TaskKilledError 中断当前平台线程
                def _progress_cb(sent, total):
                    _check_kill(tid)
                    _pupload(tid, key, sent, total)

                def _step_cb(msg):
                    _check_kill(tid)
                    _pstep(tid, key, msg)
                    _step(tid, f"  {key}: {msg}")
                rel.metadata["_progress_cb"] = _progress_cb
                rel.metadata["_step_cb"] = _step_cb
                res = adapter.publish(rel, dry_run=dry_run)
                ok = bool(res.ok)
                item = {"platform": key, "ok": ok, "message": res.message,
                        "remote_reference": res.remote_reference, "state": res.state.value}
                with _task_lock:
                    t = _TASKS.get(tid)
                    if t is not None:
                        t.setdefault("results", []).append(item)
                        done_n = len(t.get("results") or []) + len(t.get("errors") or [])
                        t["progress"] = min(95, 15 + int(75 * done_n / max(1, len(targets))))
                _pstate(tid, key, "done" if ok else "error")
                _step(tid, f"  {key}: {'完成' if ok else '失败'} - {res.message}", "ok" if ok else "error")
                _pstep(tid, key, f"{'完成' if ok else '失败'} - {res.message}", "ok" if ok else "error")
            except TaskKilledError:
                _record_error(tid, key, targets, "已手动停止", label="")
            except StoreError as e:
                _record_error(tid, key, targets, str(e))
            except Exception as e:  # 线程内兜底，单平台异常不影响其他平台线程
                # 适配器可能把回调抛出的 TaskKilledError 包装成普通异常，这里按停止请求归一处理
                if _TASKS.get(tid, {}).get("kill_req"):
                    _record_error(tid, key, targets, "已手动停止", label="")
                else:
                    _record_error(tid, key, targets, f"异常: {e}")

        if targets:
            with ThreadPoolExecutor(max_workers=len(targets)) as pool:
                futures = [pool.submit(_run_one, key) for key in targets]
                for f in futures:
                    f.result()  # 等全部平台结束后再汇总
        with _task_lock:
            t = _TASKS.get(tid) or {}
            results = list(t.get("results") or [])
            errors = list(t.get("errors") or [])
            kill_req = bool(t.get("kill_req"))
        # 汇总：errors 非空或任一平台返回 ok=False 都视为失败（含部分失败），
        # 不能无条件标 done，否则前端会误显示「发布成功」；被手动停止的任务统一标 killed
        failed_n = len(errors) + sum(1 for r in results if not r.get("ok"))
        ok_n = sum(1 for r in results if r.get("ok"))
        if kill_req:
            final_status, final_stage = "killed", "已手动停止"
        elif failed_n == 0:
            final_status, final_stage = "done", "全部完成"
        elif ok_n > 0:
            final_status, final_stage = "error", "部分失败"
        else:
            final_status, final_stage = "error", "发布失败"
        _update(tid, status=final_status, progress=100, stage=final_stage,
                results=results, errors=errors)
        if kill_req:
            _step(tid, "发布已手动停止", "error")
        elif failed_n:
            _step(tid, f"共 {failed_n} 个平台失败", "error")
        else:
            _step(tid, "发布流程全部完成")
    except Exception as e:
        _step(tid, f"异常: {e}", "error")
        _update(tid, status="error", stage="失败", progress=100)

# ---- 登录会话（内存态；重启后需重新登录）----
_session_lock = threading.Lock()
_SESSIONS: Dict[str, Dict[str, Any]] = {}  # token -> {username, role, expires_at}
_SESSION_COOKIE = "board_session"

# viewer 也能调用的 POST 接口（只读性质）；其余 POST 一律要求 admin
_READ_ONLY_POST_PATHS = {"/api/login", "/api/logout", "/api/status", "/api/apk/meta"}


def _now_ts() -> float:
    return time.time()


def _session_clean() -> None:
    """清掉过期会话（在锁内调用）。"""
    now = _now_ts()
    for k in [k for k, v in _SESSIONS.items() if v.get("expires_at", 0) < now]:
        _SESSIONS.pop(k, None)


def _session_get(token: str) -> Optional[Dict[str, Any]]:
    if not token:
        return None
    with _session_lock:
        _session_clean()
        s = _SESSIONS.get(token)
        if not s:
            return None
        # 滑动续期：访问即刷新过期时间（有效期由 board.json session_days 控制）
        days = board_config.session_days(Handler.board_config_path)
        s["expires_at"] = _now_ts() + days * 86400
        return {"username": s["username"], "role": s["role"]}


def _session_create(username: str, role: str) -> str:
    token = secrets.token_urlsafe(32)
    days = board_config.session_days(Handler.board_config_path)
    with _session_lock:
        _session_clean()
        _SESSIONS[token] = {
            "username": username, "role": role,
            "expires_at": _now_ts() + days * 86400,
        }
        # 同一用户旧会话收敛（防无限累积；允许双端并存，只保留最近两个）
        mine = [k for k, v in _SESSIONS.items() if v["username"] == username]
        if len(mine) > 2:
            for k in sorted(mine)[:-2]:
                _SESSIONS.pop(k, None)
    return token


def _session_drop(token: str) -> None:
    with _session_lock:
        _SESSIONS.pop(token, None)


def _read_cookie(handler: BaseHTTPRequestHandler, name: str) -> str:
    raw = handler.headers.get("Cookie") or ""
    try:
        jar = http_cookies.SimpleCookie()
        jar.load(raw)
        morsel = jar.get(name)
        return morsel.value if morsel else ""
    except http_cookies.CookieError:
        return ""


def _set_cookie(handler: BaseHTTPRequestHandler, name: str, value: str, max_age: int) -> None:
    """写入会话 Cookie（HttpOnly + SameSite=Lax；HTTP 内网环境不加 Secure）。"""
    handler.send_header(
        "Set-Cookie",
        f"{name}={value}; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}")


def _auth_enabled() -> bool:
    return board_config.auth_enabled(Handler.board_config_path)


def _request_user(handler: BaseHTTPRequestHandler) -> Optional[Dict[str, str]]:
    """从 Cookie 会话取当前用户；未开启鉴权时视为本机 admin 模式。"""
    if not _auth_enabled():
        return {"username": "", "role": "admin"}
    return _session_get(_read_cookie(handler, _SESSION_COOKIE))


def _same_origin(handler: BaseHTTPRequestHandler) -> bool:
    """写操作 POST 的 CSRF 加固：Origin 存在时必须与 Host 同源。

    SameSite=Lax Cookie 已挡跨站 POST；这里对带 Origin 的请求再做一道校验，
    防御不支持 SameSite 的老浏览器/WebView。
    """
    origin = handler.headers.get("Origin")
    if not origin:
        return True
    host = handler.headers.get("Host") or ""
    try:
        o = urlparse(origin)
        return (o.netloc or "") == host
    except Exception:
        return False


# ---- 状态查询 / 图标解析（HTTP 接口与飞书机器人共用）----

def _is_transient_query_error(msg: str) -> bool:
    """判断查询报错是否是网络类瞬时错误（并发查询时偶发 HTTPSConnectionPool 连接失败）。

    只有这类错误才重试一次；凭证缺失/包名不存在等业务错误不重试。
    """
    m = msg or ""
    for kw in ("网络", "连接", "超时", "Connection", "Timeout", "DNS", "timed out", "resolve"):
        if kw in m:
            return True
    return False


def query_app_status(app_id: str, platform: str = "all") -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """查询某应用在各平台的状态（/api/status 与飞书机器人共用这套实现）。

    返回 (statuses, errors)，字段结构与 /api/status 响应一致。
    """
    catalog = get_catalog(Handler.catalog_path)
    app = catalog.get_app(app_id)
    package = app.get("package_name") or ""
    if not package:
        raise StoreError(f"应用 {app_id} 尚未配置 package_name")
    creds = load_credentials(Handler.credentials_path)
    if platform == "all":
        targets = [k for k in creds if k in PLATFORM_VALUES]
    else:
        parts = [p.strip() for p in platform.split(",") if p.strip()]
        targets = [p for p in parts if p in creds] or [platform]

    huawei_harmony_pkg = app.get("huawei_harmony_package") or ""

    def query_one(key: str) -> Dict[str, Any]:
        # 网络类瞬时错误重试一次（并发查询时偶发连接失败，重试后大多能恢复）；
        # 凭证/包名等业务错误直接抛出，不重试。
        last_err: Exception = StoreError(f"查询 {key} 失败")
        for attempt in (1, 2):
            try:
                adapter = get_adapter(key, creds)
                if key == "huawei" and huawei_harmony_pkg:
                    s = adapter.query_status(package, huawei_harmony_pkg)
                else:
                    s = adapter.query_status(package)
                return {
                    "platform": key, "state": s.state.value,
                    "live_version_codes": s.live_version_codes,
                    "live_version_names": s.live_version_names,
                    "draft_version_names": s.draft_version_names,
                    "reviewing_version_names": s.reviewing_version_names,
                    "beta_version_names": list(getattr(s, "beta_version_names", [])),
                    "alpha_version_names": list(getattr(s, "alpha_version_names", [])),
                    "internal_version_names": list(getattr(s, "internal_version_names", [])),
                    "audit_note": s.audit_note,
                    "google_release_now": bool(getattr(s, "google_release_now", False)),
                    "review_message": s.review_message,
                    "checked_at": s.checked_at,
                }
            except StoreError as e:
                last_err = e
                if attempt == 1 and _is_transient_query_error(str(e)):
                    time.sleep(0.5)
                    continue
                raise
            except Exception as e:
                last_err = e
                if attempt == 1 and _is_transient_query_error(str(e)):
                    time.sleep(0.5)
                    continue
                raise
        raise last_err

    statuses, errors = [], []
    # 多个平台并发查询：各平台接口互相独立，串行时总耗时等于各平台之和。
    # 并发上限压到 4，避免一次 8 个连接把网络压出 HTTPSConnectionPool 连接失败
    # （实测全并发时 OPPO/华为/vivo/荣耀偶发连接错误导致漏平台）。
    if len(targets) > 1:
        from concurrent.futures import ThreadPoolExecutor
        workers = min(len(targets), 4)
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(query_one, key): key for key in targets}
            for fut in futures:
                key = futures[fut]
                try:
                    statuses.append(fut.result())
                except StoreError as e:
                    errors.append({"platform": key, "error": str(e)})
                except Exception as e:  # 单平台未预期异常不丢已查到的其他平台结果
                    errors.append({"platform": key, "error": f"异常: {e}"})
    else:
        for key in targets:
            try:
                statuses.append(query_one(key))
            except StoreError as e:
                errors.append({"platform": key, "error": str(e)})
            except Exception as e:
                errors.append({"platform": key, "error": f"异常: {e}"})
    return statuses, errors


def resolve_app_icon(app_id: str) -> Optional[bytes]:
    """解析应用图标字节（catalog.icon 显式配置 > APK 提取 > Apple artwork > Play 商店页）。

    从 /api/app/icon 抽出来的共用逻辑：HTTP 接口与飞书分享图都用。
    结果缓存到 <catalog目录>/icons/{app_id}.{ext}（与原实现一致）。
    """
    app = get_catalog(Handler.catalog_path).get_app(app_id)
    icons_dir = os.path.join(os.path.dirname(os.path.abspath(Handler.catalog_path)), "icons")
    os.makedirs(icons_dir, exist_ok=True)
    icon_ref = str(app.get("icon") or "").strip()
    src_marker = os.path.join(icons_dir, app_id + ".src")
    try:
        with open(src_marker, "r", encoding="utf-8") as f:
            cached_src = f.read().strip()
    except OSError:
        cached_src = None
    if cached_src is not None and cached_src != icon_ref:
        # icon 配置已变更：清除旧缓存与标记，走重新获取
        for _e, _c in _ICON_EXTS:
            try:
                os.remove(os.path.join(icons_dir, app_id + _e))
            except OSError:
                pass
        try:
            os.remove(src_marker)
        except OSError:
            pass
        cached_src = None
    if cached_src is not None:
        for ext, ct in _ICON_EXTS:
            p = os.path.join(icons_dir, app_id + ext)
            if os.path.isfile(p):
                with open(p, "rb") as f:
                    return f.read()
    # 逐来源尝试
    errors: List[str] = []
    data, ctype = b"", ""
    if icon_ref:  # 显式配置优先
        try:
            if icon_ref.startswith(("http://", "https://")):
                data, ctype = _http_get_bytes(icon_ref)
            elif os.path.isfile(icon_ref):
                with open(icon_ref, "rb") as f:
                    data = f.read()
                ctype = dict(_ICON_EXTS).get(os.path.splitext(icon_ref)[1].lower(), "image/png")
            else:
                errors.append("catalog.icon 路径不存在")
        except Exception as e:
            errors.append(f"catalog.icon: {e}")
    package = app.get("package_name") or ""
    if not data:
        apk_path = str(app.get("apk_build") or "")
        if apk_path and os.path.isfile(apk_path):
            try:
                data, ctype = extract_apk_icon(apk_path)
            except Exception as e:
                errors.append(f"APK: {e}")
        else:
            errors.append("APK 未配置或不存在")
    if not data and package:
        try:
            creds = load_credentials(Handler.credentials_path)
            if creds.get("apple"):
                adapter = get_adapter("apple", creds)
                data, ctype = adapter.fetch_icon(package)
        except Exception as e:
            errors.append(f"Apple: {e}")
    if not data and package:
        try:
            data, ctype = _play_store_icon(package)
        except Exception as e:
            errors.append(f"Play: {e}")
    if not data:
        return None  # 分享图场景：没图标就画无图标版，不报错
    # 写缓存（先写临时文件再 os.replace 原子替换，避免并发读到半截文件）
    ext = next((e for e, t in _ICON_EXTS if t == ctype), ".png")
    cache_path = os.path.join(icons_dir, app_id + ext)
    try:
        tmp_path = cache_path + ".tmp"
        with open(tmp_path, "wb") as f:
            f.write(data)
        os.replace(tmp_path, cache_path)
        tmp_src = src_marker + ".tmp"
        with open(tmp_src, "w", encoding="utf-8") as f:
            f.write(icon_ref)
        os.replace(tmp_src, src_marker)
    except OSError:
        pass  # 缓存写失败不影响本次结果
    return data


def _notify_feishu_publish(tid: str) -> None:
    """任务结束后发飞书群通知（在独立线程里调用，失败不影响任务结果）。"""
    with _task_lock:
        t = _TASKS.get(tid) or {}
        snapshot = {
            "app_id": t.get("app_id"), "platform": t.get("platform"),
            "version_name": t.get("version_name"), "status": t.get("status"),
            "stage": t.get("stage"), "results": list(t.get("results") or []),
            "errors": list(t.get("errors") or []), "dry_run": t.get("dry_run"),
            "user": t.get("user"), "finished_at": t.get("finished_at"),
            "auto_review": bool(t.get("auto_review")),
        }
    if snapshot.get("dry_run"):
        return
    app_name = str(snapshot.get("app_id") or "")
    try:
        app = get_catalog(Handler.catalog_path).get_app(snapshot.get("app_id"))
        app_name = str(app.get("name") or snapshot.get("app_id"))
    except Exception:
        pass
    try:
        _step(tid, "飞书通知: 发送中…")
        feishu_bot.notify_publish_finished(snapshot, app_name)
        _step(tid, "飞书通知: 已处理", "ok")
    except Exception as e:
        _step(tid, f"飞书通知: 失败 {e}", "error")


# ---- 登录/操作审计日志（config/login_log.jsonl，一行一条 JSON） ----
_AUDIT_LOG_FILE = os.environ.get("APPSTORE_AUDIT_LOG") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "config", "login_log.jsonl")
_audit_lock = threading.Lock()

# 只记录这些路径（登录/登出/写操作/越权尝试）；普通浏览查询不记，避免日志噪音
_AUDIT_PATHS = {
    "/api/login", "/api/logout", "/api/publish", "/api/build/upload",
    "/api/apps/update", "/api/release-now", "/api/tasks/stop", "/api/tasks/clear",
}
_AUDIT_ACTION_NAMES = {
    "/api/login": "登录", "/api/logout": "退出登录", "/api/publish": "发布应用",
    "/api/build/upload": "上传安装包", "/api/apps/update": "修改应用配置",
    "/api/release-now": "华为立即上线", "/api/tasks/stop": "停止发布任务",
    "/api/tasks/clear": "清空发布历史",
}


def _audit_log(action: str, user: str, ip: str, ok: bool, detail: str = "") -> None:
    """追加一条审计日志（JSONL：时间/IP/账号/动作/结果/说明）。写失败不影响业务。"""
    line = json.dumps({
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "ip": ip or "-", "user": user or "-",
        "action": action, "ok": bool(ok), "detail": detail or "",
    }, ensure_ascii=False)
    try:
        with _audit_lock:
            os.makedirs(os.path.dirname(os.path.abspath(_AUDIT_LOG_FILE)) or ".", exist_ok=True)
            with open(_AUDIT_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except OSError:
        pass


def _client_ip(handler: BaseHTTPRequestHandler) -> str:
    try:
        return str(handler.client_address[0]) if handler.client_address else ""
    except Exception:
        return ""


def _lan_addresses() -> List[str]:
    """本机局域网 IPv4 列表（启动横幅展示用）。"""
    import socket
    ips: List[str] = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("10.255.255.255", 1))  # 不会真的发包，只为让路由选源地址
            ip = s.getsockname()[0]
            if ip and not ip.startswith("127."):
                ips.append(ip)
        finally:
            s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except OSError:
        pass
    return ips


class Handler(BaseHTTPRequestHandler):
    server_version = "AppStoreBoard/0.2"
    credentials_path = "config/credentials.json"
    catalog_path = "apps/catalog.json"
    board_config_path = "config/board.json"

    def log_message(self, fmt, *args):  # 静默访问日志
        pass

    def _catalog(self):
        return get_catalog(self.catalog_path)

    # ---- 鉴权 ----
    def _check_auth(self, path: str, write: bool) -> Optional[Tuple[int, str]]:
        """返回 (HTTP 状态码, 原因)（None = 放行）。

        - 未开启鉴权（board.json 无 users）：全部放行（本机模式）
        - 页面 / 与 /api/session、/api/login、/api/logout 不需要登录
        - 其余 API 需要登录会话；写操作额外要求 admin 角色 + 同源校验
        - 敏感写操作与越权/跨站尝试写入审计日志 config/login_log.jsonl
        """
        if not _auth_enabled():
            return None
        # 页面本身与登录态查询/登录接口不需要会话
        if path == "/" or path == "/api/session" or path in _READ_ONLY_POST_PATHS:
            return None
        user = _request_user(self)
        if user is None:
            return (401, "login")
        if write:
            if not _same_origin(self):
                if path in _AUDIT_PATHS:
                    _audit_log(_AUDIT_ACTION_NAMES.get(path, path),
                               user.get("username") or "?", _client_ip(self), False,
                               "跨站请求被拒绝")
                return (403, "origin")
            if user.get("role") != "admin":
                if path in _AUDIT_PATHS:
                    _audit_log(_AUDIT_ACTION_NAMES.get(path, path),
                               user.get("username") or "?", _client_ip(self), False,
                               "viewer 只读账号尝试写操作被拒绝")
                return (403, "role")
            # admin 的敏感写操作 → 记审计日志
            # （登录/登出在各自接口里记；发布在 _api_publish 里记更细的参数）
            if path in _AUDIT_PATHS and path not in ("/api/login", "/api/logout", "/api/publish"):
                _audit_log(_AUDIT_ACTION_NAMES.get(path, path),
                           user.get("username") or "?", _client_ip(self), True, "")
        return None

    def _auth_error_response(self, status: int, reason: str = "") -> None:
        if status == 401:
            _json_response(self, {"ok": False, "error": "未登录或会话已过期，请先登录"}, 401)
        elif reason == "origin":
            _json_response(self, {"ok": False, "error": "跨站请求被拒绝（Origin 校验未通过）"}, 403)
        else:
            _json_response(self, {"ok": False, "error": "无权执行该操作（发布类操作需要 admin 账号，当前为只读 viewer）"}, 403)

    # ---- GET ----
    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path
        try:
            denied = self._check_auth(path, write=False)
            if denied:
                return self._auth_error_response(denied[0], denied[1])
            if path == "/":
                return _html_response(self, INDEX_HTML)
            if path == "/api/apps":
                catalog = self._catalog()
                payload = {
                    "categories": [{"name": c} for c in catalog.categories()],
                    "apps": [catalog.status_payload(a["id"]) for a in catalog.all_apps()],
                }
                return _json_response(self, payload)
            if path == "/api/app/icon":
                return self._api_app_icon()
            if path == "/api/google/bundles":
                return self._api_google_bundles()
            if path == "/api/google/apk":
                return self._api_google_apk()
            if path == "/api/platforms":
                return _json_response(self, list_platforms())
            if path == "/api/session":
                return self._api_session()
            if path == "/api/share-image":
                return self._api_share_image()
            if path == "/api/config":
                return self._api_config()
            if path == "/api/validate":
                return self._api_validate()
            if path == "/api/files":
                return self._api_files()
            if path.startswith("/api/tasks/"):
                tid = path.split("/")[-1]
                import copy as _c
                with _task_lock:
                    # 并行发布时工作线程会持续 append 各平台字段，必须快照后再序列化
                    task = _c.deepcopy(_TASKS.get(tid, {"error": "not found"}))
                return _json_response(self, task)
            if path == "/api/tasks":
                import copy as _c2
                with _task_lock:
                    running = [_c2.deepcopy(v) for v in _TASKS.values() if v.get("status") in ("running",)]
                    # dry-run 校验任务不进历史列表
                    history = [_c2.deepcopy(v) for v in _TASKS.values() if v.get("status") in ("done", "error", "killed") and not v.get("dry_run")]
                history.sort(key=lambda x: x.get("finished_at", ""), reverse=True)
                return _json_response(self, {"running": running, "history": history[:_HISTORY_MAX]})
            return _json_response(self, {"error": "not found"}, 404)
        except Exception as e:
            return _json_response(self, {"error": str(e)}, 500)

    # ---- POST ----
    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        try:
            denied = self._check_auth(path, write=True)
            if denied:
                return self._auth_error_response(denied[0], denied[1])
            if path == "/api/build/upload":
                # 上传解析/校验失败也要返回 JSON 错误，不能让连接直接断开
                return self._api_build_upload()
            body = _read_body(self)
            if path == "/api/login":
                return self._api_login(body)
            if path == "/api/logout":
                return self._api_logout()
            if path == "/api/publish":
                return self._api_publish(body)
            if path == "/api/status":
                return self._api_status(body)
            if path == "/api/apps/update":
                return self._api_update_app(body)
            if path == "/api/apk/meta":
                return self._api_apk_meta(body)
            if path == "/api/release-now":
                return self._api_release_now(body)
            if path == "/api/tasks/stop":
                return self._api_tasks_stop(body)
            if path == "/api/tasks/clear":
                return self._api_tasks_clear(body)
            return _json_response(self, {"error": "not found"}, 404)
        except StoreError as e:
            return _json_response(self, {"ok": False, "error": str(e)}, 400)
        except Exception as e:
            return _json_response(self, {"ok": False, "error": str(e)}, 500)

    # ---- 鉴权接口 ----
    def _api_session(self):
        user = _request_user(self)
        if user is None:
            return _json_response(self, {"authenticated": False, "auth_enabled": _auth_enabled()})
        return _json_response(self, {
            "authenticated": True, "auth_enabled": _auth_enabled(),
            "username": user.get("username") or "本机模式", "role": user.get("role", "admin"),
        })

    def _api_login(self, body: Dict[str, Any]):
        username = str(body.get("username") or "").strip()
        password = str(body.get("password") or "")
        if not username or not password:
            raise StoreError("请输入用户名和密码")
        found = board_config.check_login(self.board_config_path, username, password)
        if not found:
            time.sleep(0.6)  # 失败延迟，增加暴力尝试成本
            _audit_log("登录", username or "?", _client_ip(self), False, "用户名或密码错误")
            return _json_response(self, {"ok": False, "error": "用户名或密码错误"}, 401)
        _audit_log("登录", found["username"], _client_ip(self), True,
                   "角色 " + found["role"])
        token = _session_create(found["username"], found["role"])
        days = board_config.session_days(self.board_config_path)
        body_resp = {"ok": True, "username": found["username"], "role": found["role"]}
        body_bytes = json.dumps(body_resp, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body_bytes)))
        _set_cookie(self, _SESSION_COOKIE, token, days * 86400)
        self.end_headers()
        self.wfile.write(body_bytes)

    def _api_logout(self):
        user = _request_user(self)
        _audit_log("退出登录", (user or {}).get("username") or "?", _client_ip(self), True)
        _session_drop(_read_cookie(self, _SESSION_COOKIE))
        body_bytes = json.dumps({"ok": True}, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body_bytes)))
        _set_cookie(self, _SESSION_COOKIE, "", 0)  # 立即过期
        self.end_headers()
        self.wfile.write(body_bytes)

    def _api_config(self):
        user = _request_user(self)
        fs_state = feishu_bot.state()
        return _json_response(self, {
            "credentials": self.credentials_path,
            "catalog": self.catalog_path,
            "board_config": self.board_config_path,
            "auth_enabled": _auth_enabled(),
            "username": (user or {}).get("username") or "",
            "role": (user or {}).get("role") or "admin",
            "feishu": fs_state,
        })

    def _api_share_image(self):
        """服务端渲染的状态分享图 PNG（与前端「分享」按钮出的图同款设计）。

        用途：飞书机器人发图、脚本/CI 直接取图、前端可选统一改走这张。
        """
        qs = parse_qs(urlparse(self.path).query)
        app_id = (qs.get("app_id") or [""])[0].strip()
        if not app_id:
            raise StoreError("缺少 app_id")
        from .share_image import render_status_image
        app = self._catalog().get_app(app_id)
        name = str(app.get("name") or app_id)
        statuses, errors = query_app_status(app_id)
        if not statuses:
            raise StoreError("没有查到任何平台状态" + (f"（{errors[0]['error']}）" if errors else ""))
        checked_at = next((s.get("checked_at") or "" for s in statuses), "")
        icon_bytes = None
        try:
            icon_bytes = resolve_app_icon(app_id)
        except Exception:
            icon_bytes = None
        png = render_status_image(name, statuses, icon_bytes=icon_bytes, checked_at=checked_at)
        if not png:
            raise StoreError("分享图生成失败（Pillow 未安装？pip install Pillow）")
        return _bytes_response(self, png, "image/png")

    # ---- 实现 ----
    def _api_validate(self):
        creds = load_credentials(self.credentials_path)
        rows = []
        for key in creds:
            if key not in PLATFORM_VALUES:
                continue
            try:
                adapter = get_adapter(key, creds)
                problems = adapter.check()
                rows.append({"platform": key, "ok": not problems, "message": "；".join(problems) if problems else ""})
            except StoreError as e:
                rows.append({"platform": key, "ok": False, "message": str(e)})
        return _json_response(self, {"validated": rows})


    def _api_files(self):
        """返回本机可选的 AAB/APK 列表，供前端选择。"""
        catalog = self._catalog()
        return _json_response(self, catalog.detect_local_builds())

    def _api_build_upload(self):
        """接收前端选择的 AAB/APK 文件并保存到工作区 builds/ 目录。

        浏览器 file input 拿不到本地绝对路径，所以通过上传方式把文件
        落到后端（127.0.0.1 本地，大文件也快），返回后端真实路径。
        multipart 表单字段：type = aab|apk, file = 文件
        multipart 解析基于 email 模块（cgi 已在 Python 3.13 移除）；
        需整包读入内存，因此有单文件大小上限（_UPLOAD_MAX_BYTES）。
        """
        ftype, filename, data = _parse_multipart_upload(self)

        ext = os.path.splitext(filename)[1].lower()
        expected_ext = "." + ftype
        if ext != expected_ext:
            raise StoreError(
                f"文件类型不匹配：type={ftype} 要求 {expected_ext} 文件，收到 {ext or '无扩展名'}"
            )

        # 安全文件名：取 basename，加时间戳防重名
        safe_name = os.path.basename(filename or f"upload.{ftype}")
        ts = time.strftime("%Y%m%d%H%M%S")
        stem, ext = os.path.splitext(safe_name)
        save_name = f"{stem}-{ts}{ext}" if ts else safe_name

        save_dir = os.path.join(_builds_root(), ftype)
        os.makedirs(save_dir, exist_ok=True)
        save_path = os.path.abspath(os.path.join(save_dir, save_name))

        with open(save_path, "wb") as out:
            out.write(data)

        return _json_response(self, {"ok": True, "type": ftype, "path": save_path, "name": save_name})

    def _api_update_app(self, body: Dict[str, Any]):
        app_id = body.get("app_id") or ""
        if not app_id:
            raise StoreError("缺少 app_id")
        fields = {k: v for k, v in body.items() if k != "app_id"}
        catalog = self._catalog()
        catalog.update_app(app_id, fields)
        # 绑定/更换安装包时，自动回收 builds/ 下未被任何应用引用的旧包
        cleanup = None
        if "apk_build" in fields or "aab_build" in fields:
            try:
                cleanup = _cleanup_unreferenced_builds(catalog)
            except Exception:
                cleanup = None
        return _json_response(self, {"ok": True, "app": catalog.status_payload(app_id), "cleanup": cleanup})

    def _api_apk_meta(self, body: Dict[str, Any]):
        """解析 APK 元数据（package/versionName/versionCode/label）。"""
        path = body.get("path") or ""
        if not path:
            raise StoreError("缺少 path")
        info = parse_build(path)
        return _json_response(self, {"ok": True, **info})

    def _api_release_now(self, body: Dict[str, Any]):
        """审核通过待上架（定时未到时间）的版本改为立即上架。

        目前仅华为提供官方接口（on-shelf-time changeType=2）；
        其余平台适配器 release_now 默认抛 StoreError，前端只在
        华为"待发布"状态卡上展示按钮。
        """
        app_id = body.get("app_id") or ""
        if not app_id:
            raise StoreError("缺少 app_id")
        app = self._catalog().get_app(app_id)
        package = app.get("package_name") or ""
        if not package:
            raise StoreError(f"应用 {app_id} 尚未配置 package_name")
        platform = (body.get("platform") or "").lower()
        if not platform:
            raise StoreError("缺少 platform（如 huawei / google）")
        creds = load_credentials(self.credentials_path)
        if platform not in creds:
            raise StoreError(f"未配置 {platform} 凭证")
        adapter = get_adapter(platform, creds)
        result = adapter.release_now(package)
        return _json_response(self, {
            "ok": True, "platform": platform, "package": package, "result": result,
        })

    def _api_app_icon(self):
        """应用图标（供前端分享图）：解析逻辑在共用函数 resolve_app_icon 里。

        结果缓存到 <catalog目录>/icons/{app_id}.{ext}，浏览器侧另有 max-age 缓存。
        """
        qs = parse_qs(urlparse(self.path).query)
        app_id = (qs.get("app_id") or [""])[0].strip()
        if not app_id:
            raise StoreError("缺少 app_id")
        data = resolve_app_icon(app_id)
        if data is None:
            raise StoreError(
                "未找到应用图标；可在 catalog 为应用配置 icon 字段（本地路径或 URL）")
        # 缓存里必有扩展名信息，直接按缓存文件类型回
        icons_dir = os.path.join(os.path.dirname(os.path.abspath(self.catalog_path)), "icons")
        for ext, ct in _ICON_EXTS:
            p = os.path.join(icons_dir, app_id + ext)
            if os.path.isfile(p):
                return _bytes_response(self, data, ct)
        return _bytes_response(self, data, "image/png")

    def _api_google_bundles(self):
        """Google Play 已上传的 App Bundle 版本列表（供下载面板选择版本）。

        返回 [{version_code, version_name, release_status}]，按版本号倒序。
        Google API 不提供上传时间/安装人数，由前端展示占位符。
        """
        qs = parse_qs(urlparse(self.path).query)
        app_id = (qs.get("app_id") or [""])[0].strip()
        if not app_id:
            raise StoreError("缺少 app_id")
        app = self._catalog().get_app(app_id)
        package = str(app.get("package_name") or "").strip()
        if not package:
            raise StoreError("该应用未配置包名（package_name）")
        creds = load_credentials(self.credentials_path)
        if not creds.get("google"):
            raise StoreError("未配置 Google 凭证（credentials.json google 段）")
        adapter = get_adapter("google", creds)
        rows = adapter.list_bundle_versions(package)
        # Google API 不提供未发布/历史版本的 versionName（Play Console 网页是内部数据源），
        # 合并本地缓存：下载过的版本会从 APK 清单解析出真实版本名
        cached = self._google_cached_version_names()
        pkg_names = cached.get(package) or {}
        for row in rows:
            if not row.get("version_name"):
                vname = pkg_names.get(str(row.get("version_code")))
                if vname:
                    row["version_name"] = vname
        return _json_response(self, {"ok": True, "package_name": package, "bundles": rows})

    def _downloads_dir(self) -> str:
        return os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(self.catalog_path))), "downloads"
        )

    def _google_version_names_path(self) -> str:
        return os.path.join(self._downloads_dir(), "version-names.json")

    def _google_cached_version_names(self) -> Dict[str, Dict[str, str]]:
        """读本地版本名称缓存 {package: {version_code: version_name}}。"""
        path = self._google_version_names_path()
        if not os.path.isfile(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _remember_google_version_name(self, package: str, version_code: int, apk_path: str) -> None:
        """从已下载的签名 APK 解析 versionName 落盘缓存（供版本列表补全名称列）。"""
        names = self._google_cached_version_names()
        pkg_map = names.get(package) or {}
        if str(version_code) in pkg_map:
            return  # 已解析过
        try:
            meta = parse_apk(apk_path)
        except Exception:
            return  # 解析失败不影响下载本身
        vname = str(meta.get("version_name") or "").strip()
        if not vname:
            return
        pkg_map[str(version_code)] = vname
        names[package] = pkg_map
        path = self._google_version_names_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(names, f, ensure_ascii=False, indent=1)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass

    def _api_google_apk(self):
        """下载指定 Bundle 版本的签名 APK（universal 整包，Play App Signing 密钥签名）。

        已缓存（downloads/{包名}-{versionCode}.apk）直接从本地发；未缓存则流式透传：
        边从 Google 拉边转发给浏览器（下载条立即出现并显示进度），完成后落盘缓存。
        """
        qs = parse_qs(urlparse(self.path).query)
        app_id = (qs.get("app_id") or [""])[0].strip()
        try:
            version_code = int((qs.get("version_code") or [""])[0])
        except ValueError:
            raise StoreError("version_code 必须是整数")
        if not app_id:
            raise StoreError("缺少 app_id")
        app = self._catalog().get_app(app_id)
        package = str(app.get("package_name") or "").strip()
        if not package:
            raise StoreError("该应用未配置包名（package_name）")
        # 文件名只允许包名字符（Android 包名规则），防路径注入
        safe_pkg = re.sub(r"[^A-Za-z0-9._-]", "_", package)
        fname = f"{safe_pkg}-{version_code}.apk"
        dest = os.path.join(self._downloads_dir(), fname)
        if not os.path.isfile(dest):
            creds = load_credentials(self.credentials_path)
            if not creds.get("google"):
                raise StoreError("未配置 Google 凭证（credentials.json google 段）")
            adapter = get_adapter("google", creds)
            # 首次拉取：流式透传给浏览器——先从 Google 读到第一块拿到总大小再发响应头，
            # 浏览器下载条立即出现（有总大小即显示百分比进度），之后边从 Google 拉边转发；
            # 同时写独立命名的 .part 临时文件（并发拉取互不干扰），全部完成后原子改名入缓存，
            # 下载中断不会留下半成品被后续请求命中
            part = dest + ".part-" + uuid.uuid4().hex[:8]
            stream = None
            try:
                stream = adapter.stream_signed_apk(package, version_code, part)
                total = next(stream)
                self.send_response(200)
                self.send_header("Content-Type", "application/vnd.android.package-archive")
                if total:
                    self.send_header("Content-Length", str(total))
                self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                try:
                    for chunk in stream:
                        self.wfile.write(chunk)
                except (ConnectionError, BrokenPipeError):
                    # 浏览器取消/断开：正常现象，停止拉取（finally 清理临时文件）
                    return
                os.replace(part, dest)
                self._remember_google_version_name(package, version_code, dest)
                return
            finally:
                if stream is not None:
                    stream.close()
                if os.path.isfile(part):
                    try:
                        os.remove(part)
                    except OSError:
                        pass
        # 顺带把该版本的真实 versionName 解析进本地缓存（版本列表显示用）
        self._remember_google_version_name(package, version_code, dest)
        size = os.path.getsize(dest)
        self.send_response(200)
        self.send_header("Content-Type", "application/vnd.android.package-archive")
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with open(dest, "rb") as f:
            while True:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def _api_tasks_stop(self, body: Dict[str, Any]):
        '''停止运行中的发布任务：置 kill_req 标记，各平台线程在下一次进度/步骤回调处中断。'''
        tid = str(body.get("task_id") or "").strip()
        if not tid:
            raise StoreError("缺少 task_id")
        with _task_lock:
            t = _TASKS.get(tid)
            if t is None:
                return _json_response(self, {"ok": False, "error": "任务不存在"}, 404)
            if t.get("status") != "running":
                return _json_response(self, {"ok": False, "error": "任务已结束，无需停止"}, 400)
            t["kill_req"] = True
            t["stage"] = "正在停止…"
        _step(tid, "收到停止请求，正在中断各平台上传…")
        return _json_response(self, {"ok": True})

    def _api_tasks_clear(self, body: Dict[str, Any]):
        """清空发布历史：删除落盘文件 + 清理内存中的稳定任务。

        两道防护（2026-09-23 起）：
        1. 必须显式传 {"confirm": true} 才执行——防止随手/脚本误调（前端没有清空按钮，
           这个接口只给脚本用，多一步确认不影响日常使用）
        2. 清空前自动备份：tasks_history.json.bak（最近一次）+ 带时间戳的
           tasks_history.json.bak-YYYYmmdd-HHMMSS（保留最近 5 份，多次清空也不会
           把上一次的备份覆盖掉），随时可找回。
        """
        # 第一道闸：必须显式确认（{"confirm": true}），防止随手调用误清历史
        if not (isinstance(body, dict) and body.get("confirm")):
            raise StoreError(
                "清空发布历史需要显式确认：请求体传 {\"confirm\": true}；"
                "执行前会自动备份到 config/tasks_history.json.bak-<时间戳>")
        # 第二道闸：先备份再删除（防误清不可恢复）
        try:
            if os.path.isfile(_HISTORY_FILE):
                import glob
                import shutil
                shutil.copyfile(_HISTORY_FILE, _HISTORY_FILE + ".bak")
                stamp = time.strftime("%Y%m%d-%H%M%S")
                shutil.copyfile(_HISTORY_FILE, f"{_HISTORY_FILE}.bak-{stamp}")
                # 只保留最近 5 份时间戳备份（只清理 .bak-<数字> 形式，不动手工备份）
                olds = sorted(glob.glob(_HISTORY_FILE + ".bak-[0-9]*"))
                for p in olds[:-5]:
                    try:
                        os.remove(p)
                    except OSError:
                        pass
        except Exception:
            pass
        # 删除落盘历史文件
        try:
            if os.path.isfile(_HISTORY_FILE):
                os.remove(_HISTORY_FILE)
        except Exception:
            pass
        # 清理内存：只保留运行中的任务
        with _task_lock:
            keep = {k: v for k, v in _TASKS.items() if v.get("status") == "running"}
            for k in list(_TASKS.keys()):
                if k not in keep:
                    _TASKS.pop(k, None)
                    _PENDING_PATHS.pop(k, None)
        return _json_response(self, {"ok": True, "cleared": True})

    def _api_publish(self, body: Dict[str, Any]):
        app_id = body.get("app_id") or ""
        if not app_id:
            raise StoreError("缺少 app_id")
        platform = (body.get("platform") or "all").lower()
        dry_run = bool(body.get("dry_run"))
        version_name = body.get("version_name") or ""
        # 审计日志先记（应用校验失败也能留下"谁在什么时候试图发布什么"）
        _pub_user = (_request_user(self) or {}).get("username") or "?"
        try:
            _audit_log("发布应用", _pub_user, _client_ip(self), True,
                       f"应用 {app_id} · 平台 {platform} · v{version_name or '?'}"
                       + (" · dry-run 仅校验" if dry_run else ""))
        except Exception:
            pass
        # 仅查询应用不允许发布
        app = self._catalog().get_app(app_id)
        if app.get("query_only"):
            raise StoreError("应用 {id} 标记为仅查询（query_only），不支持发布操作".format(id=app_id))
        platform = (body.get("platform") or "all").lower()
        dry_run = bool(body.get("dry_run"))
        version_name = body.get("version_name") or ""
        version_code = body.get("version_code")
        release_notes = body.get("release_notes") or ""
        release_notes_l10n = body.get("release_notes_l10n") or {}
        track = body.get("track") or ""
        apk_path = body.get("apk_path") or ""
        aab_path = body.get("aab_path") or ""
        online_time = body.get("online_time") or None
        auto_review = bool(body.get("auto_review"))
        release_mode = body.get("release_mode") or "staged"

        # 前置校验：APK/AAB 包名必须与应用目录中的 package_name 一致
        for label, p in (("AAB", aab_path), ("APK", apk_path)):
            if not p:
                continue
            try:
                meta = parse_build(p)
            except StoreError as e:
                raise StoreError(f"{label} 解析失败: {e}")
            real = meta.get("package_name") or ""
            expected = app.get("package_name") or ""
            if expected and real and real != expected:
                raise StoreError(
                    f"安装包包名与当前应用不一致！{label} 内包名={real}，"
                    f"应用 {app_id} 配置包名={expected}。请选择正确的安装包"
                )

        # 创建异步任务（记录操作者，供发布历史与飞书通知展示）
        user = _request_user(self) or {}
        tid = _new_task(app_id, platform, dry_run, apk_path=apk_path, aab_path=aab_path,
                        version_name=version_name)
        with _task_lock:
            _TASKS[tid]["user"] = user.get("username") or ""
            # auto_review 同时存进任务本身：任务结束时 _PENDING_PATHS 会被清掉，
            # 飞书通知要靠它判断 Google 是否真的送审（勾了自动送审才算送审成功）
            _TASKS[tid]["auto_review"] = auto_review
            _TASKS[tid]["release_mode"] = release_mode
            _PENDING_PATHS[tid].update({
                "version_name": version_name, "version_code": version_code,
                "release_notes": release_notes, "release_notes_l10n": release_notes_l10n, "track": track,
                "online_time": online_time,
                "auto_review": auto_review,
                "release_mode": release_mode,
            })
        _step(tid, "任务已创建，后台发布中...")

        t = threading.Thread(target=_publish_worker, args=(tid,), daemon=True)
        t.start()
        _step(tid, "后台线程启动")

        return _json_response(self, {"ok": True, "task_id": tid})

    def _api_status(self, body: Dict[str, Any]):
        app_id = body.get("app_id") or ""
        if not app_id:
            raise StoreError("缺少 app_id")
        app = self._catalog().get_app(app_id)
        package = app.get("package_name") or ""
        statuses, errors = query_app_status(app_id, (body.get("platform") or "all").lower())
        return _json_response(self, {"ok": not errors or bool(statuses), "app_id": app_id, "package": package, "statuses": statuses, "errors": errors})


def run_server(
    host: str = "127.0.0.1",
    port: int = 8090,
    credentials_path: str = "config/credentials.json",
    catalog_path: str = "apps/catalog.json",
    open_browser: bool = False,
    board_config_path: str = "config/board.json",
) -> int:
    Handler.credentials_path = credentials_path
    Handler.catalog_path = catalog_path
    Handler.board_config_path = board_config_path

    auth_on = board_config.auth_enabled(board_config_path)
    is_loopback = host in ("127.0.0.1", "localhost", "::1", "")
    if not is_loopback and not auth_on:
        raise StoreError(
            "局域网/公网访问必须先开启登录鉴权：请在 config/board.json 配置 users"
            "（模板见 config/example.board.json，说明见 docs/WEB_GUIDE.md），"
            "再绑定非 127.0.0.1 地址。看板能执行真实发布，不能不开鉴权暴露到网内。"
        )

    # 通知卡片「打开看板」按钮用实际可达地址（绑 0.0.0.0 时取局域网 IP）
    lan_ips = _lan_addresses()
    display_host = host if host not in ("0.0.0.0", "::", "") else (lan_ips[0] if lan_ips else "127.0.0.1")
    board_url = f"http://{display_host}:{port}/"

    # 给飞书机器人注入运行环境（状态查询/图标/应用列表/看板地址）
    feishu_bot.configure(
        board_config_path=board_config_path,
        query_status=query_app_status,
        icon=resolve_app_icon,
        apps=lambda: get_catalog(catalog_path).all_apps(),
        board_url=board_url,
        log=lambda msg: print(f"[飞书] {msg}", flush=True),
    )

    _load_history()
    httpd = ThreadingHTTPServer((host, port), Handler)
    # 启动横幅全部带 flush=True：后台(nohup)运行时日志文件能立刻看到
    if is_loopback:
        print(f"🖥  AppStore 发布看板已启动: http://127.0.0.1:{port}/", flush=True)
    else:
        print(f"🖥  AppStore 发布看板已启动（局域网模式，监听 {host}:{port}）", flush=True)
        for ip in lan_ips:
            print(f"   同网段访问: http://{ip}:{port}/", flush=True)
        if not lan_ips:
            print(f"   未探测到局域网 IP，请用本机 IP 访问 {port} 端口", flush=True)
    print(f"   凭证: {credentials_path}", flush=True)
    print(f"   目录: {catalog_path}", flush=True)
    print(f"   看板配置: {board_config_path}（登录鉴权: {'已开启' if auth_on else '未开启（本机直连模式）'}）", flush=True)
    if auth_on:
        names = ", ".join(u["username"] + "(" + u["role"] + ")" for u in board_config.list_users(board_config_path))
        print(f"   账号: {names}", flush=True)
    # 飞书机器人（board.json 配了 feishu.app_id/app_secret 才启动长连接）
    fs_state = feishu_bot.state()
    if fs_state["enabled"]:
        if feishu_bot.start():
            bind = "已绑定" if fs_state.get("bound_chat") else "未绑定（在群里 @机器人 发送「绑定通知群」）"
            print(f"   飞书机器人: 长连接启动中；发布通知群{bind}", flush=True)
    else:
        print("   飞书机器人: 未配置（board.json feishu 段，可选功能）", flush=True)
    print("   按 Ctrl+C 停止", flush=True)
    if open_browser:
        try:
            webbrowser.open(f"http://127.0.0.1:{port}/")
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(run_server())
