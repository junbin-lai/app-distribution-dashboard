"""看板自身配置（登录账号/权限 + 飞书机器人）的加载与写回。

配置文件 config/board.json（本地私有，gitignore 排除，模板见 example.board.json）：

{
  "users": [
    {"username": "laijunbin", "password": "改成你的密码", "role": "admin"},
    {"username": "xiaowang",  "password": "改成同事的密码", "role": "viewer"}
  ],
  "session_days": 14,
  "feishu": {
    "app_id": "cli_xxx",
    "app_secret": "xxx",
    "notify_chat_id": "oc_xxx",
    "notify_on_publish": true
  }
}

- users 非空 = 开启登录鉴权；role 三种：owner（超级管理员，可管理用户账号）/ admin（可发布）/ viewer（只读+查询）
- feishu 段缺省时飞书功能整体关闭，不影响看板其他功能
- 每次请求都重新读盘，改配置无需重启（飞书长连接除外，需重启生效）
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from typing import Any, Dict, List, Optional

_LOCK = threading.Lock()

ROLES = ("owner", "admin", "viewer")


def _read_json(path: str) -> Dict[str, Any]:
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def load_board_config(path: str) -> Dict[str, Any]:
    """读取看板配置（文件缺失/损坏时返回空 dict = 全部功能关闭）。"""
    return _read_json(path)


def list_users(path: str) -> List[Dict[str, str]]:
    """返回 [{"username","password","password_sha256","role"}]；users 为空列表 = 未开启鉴权。"""
    cfg = load_board_config(path)
    users = cfg.get("users")
    if not isinstance(users, list):
        return []
    out = []
    for u in users:
        if not isinstance(u, dict):
            continue
        name = str(u.get("username") or "").strip()
        if not name:
            continue
        role = str(u.get("role") or "").strip().lower()
        out.append({
            "username": name,
            "password": str(u.get("password") or ""),
            "password_sha256": str(u.get("password_sha256") or ""),
            # 未知角色一律按最低权限 viewer 处理
            "role": role if role in ROLES else "viewer",
        })
    return out


def auth_enabled(path: str) -> bool:
    return bool(list_users(path))


def session_days(path: str) -> int:
    """会话有效期（天）。默认 90 天上限，且每次请求滑动续期——
    多平台发布耗时长（可能几十分钟）不受影响，发布期间前端持续轮询会一直续期。"""
    cfg = load_board_config(path)
    try:
        days = int(cfg.get("session_days") or 90)
    except (TypeError, ValueError):
        days = 90
    return max(1, min(365, days))


def check_login(path: str, username: str, password: str) -> Optional[Dict[str, str]]:
    """校验用户名/密码；成功返回 {"username","role"}，失败返回 None。

    password_sha256 存在时优先校验哈希（sha256(明文)），否则比对明文。
    """
    for u in list_users(path):
        if u["username"] != username:
            continue
        ok = False
        if u["password_sha256"]:
            ok = hashlib.sha256((password or "").encode("utf-8")).hexdigest() == u["password_sha256"]
        else:
            ok = bool(password) and u["password"] == password
        if ok:
            return {"username": u["username"], "role": u["role"]}
        return None  # 用户名对了密码不对，直接失败（不继续匹配同名用户）
    return None


def feishu_config(path: str) -> Dict[str, Any]:
    """返回飞书机器人配置（未配置时为空 dict）。"""
    cfg = load_board_config(path)
    fs = cfg.get("feishu")
    return fs if isinstance(fs, dict) else {}


def feishu_ready(path: str) -> bool:
    fs = feishu_config(path)
    return bool(fs.get("app_id") and fs.get("app_secret"))


def security_alert_chat_id(path: str) -> str:
    """安全告警的接收会话（飞书私聊 chat_id）。与发布通知群（notify_chat_id）分开：
    账号增删改/越权等敏感告警只发私聊，不进公共发布群。空串 = 未绑定。"""
    fs = feishu_config(path)
    return str(fs.get("security_chat_id") or "")


def _atomic_write_json(path: str, cfg: Dict[str, Any]) -> None:
    """原子写 JSON。写完对 tmp 强制 chmod 0o600 再 replace。

    open(tmp,"w") 按默认 umask 创建（通常 644），os.replace 后目标文件继承 tmp
    的权限——board.json 含飞书 app_secret 和密码哈希，不处理的话每次写都会把
    手工收紧的权限（chmod 600）静默回退成 644。
    """
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def save_feishu_fields(path: str, fields: Dict[str, Any]) -> bool:
    """把若干字段合并写回 board.json 的 feishu 段（原子替换，保留其余配置）。

    供「@机器人 绑定通知群」等指令在运行期回写 notify/security 配置使用。
    """
    with _LOCK:
        cfg = _read_json(path)
        fs = cfg.get("feishu")
        if not isinstance(fs, dict):
            fs = {}
        fs.update(fields)
        cfg["feishu"] = fs
        try:
            _atomic_write_json(path, cfg)
            return True
        except OSError:
            return False


def save_users(path: str, users: List[Dict[str, str]]) -> bool:
    """整体替换 board.json 的 users 段（原子写，保留 feishu/session_days 等其余配置）。

    供用户管理页面增删改账号使用；users 内的密码只写 password_sha256（哈希），
    不接受明文落盘。写失败返回 False（调用方应提示用户）。
    """
    with _LOCK:
        cfg = _read_json(path)
        cfg["users"] = users
        try:
            _atomic_write_json(path, cfg)
            return True
        except OSError:
            return False
