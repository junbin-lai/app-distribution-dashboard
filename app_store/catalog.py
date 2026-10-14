"""应用目录：分类、包名、构建产物路径的管理。

catalog.json 是唯一数据源；CLI 和 Web 都从这里读应用信息，
发布时把应用元数据并入 Release（package_name / 包路径 / 版本）。

每个应用可配置双构建产物：
  - aab_build : Google Play 用（AAB，Google 专用包）
  - apk_build : 其他平台用（华为/OPPO/vivo/小米/荣耀 的 APK）
  - latest_build : 兼容旧字段（单包时使用；若同时配置 aab/apk 则忽略）
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .base import StoreError
from .models import Platform, Release

DEFAULT_CATALOG = str(Path(__file__).resolve().parent.parent / "apps" / "catalog.json")

_CN_PLATFORMS = {"huawei", "oppo", "vivo", "xiaomi", "honor"}


def _expand(p: str) -> str:
    """显式传入的路径（如 CLI 参数）：相对路径以当前工作目录为基准。"""
    return os.path.abspath(os.path.expanduser(p))


def _resolve(base: Path, p: str) -> str:
    """catalog.json 内配置的路径：相对路径以 catalog 所在目录为基准，绝对路径原样。"""
    p = os.path.expanduser(p)
    if not os.path.isabs(p):
        p = str(base / p)
    return os.path.abspath(p)


class AppCatalog:
    def __init__(self, path: str = DEFAULT_CATALOG) -> None:
        self.path = Path(path).expanduser()
        if not self.path.is_file():
            raise StoreError(f"应用目录不存在: {self.path}")
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise StoreError(f"应用目录 JSON 解析失败: {e}")
        self.base_dir = self.path.resolve().parent

    # ---- 查询 ----
    def categories(self) -> List[str]:
        return [c["name"] for c in self.data.get("categories", [])]

    def all_apps(self) -> List[Dict[str, Any]]:
        apps: List[Dict[str, Any]] = []
        for cat in self.data.get("categories", []):
            for app in cat.get("apps", []):
                item = dict(app)
                item["category"] = cat["name"]
                item["category_display"] = cat["name"] if "display_name" not in cat else (cat["display_name"] or "")
                apps.append(item)
        return apps

    def get_app(self, app_id: str) -> Dict[str, Any]:
        for app in self.all_apps():
            if app["id"] == app_id:
                return app
        raise StoreError(f"目录中找不到应用: {app_id}（可用 appstore apps 查看）")

    # ---- 构建产物 ----
    def _build_paths(self, app: Dict[str, Any]) -> Dict[str, str]:
        """返回 {aab: 绝对路径或'', apk: 绝对路径或''}。"""
        aab = app.get("aab_build") or ""
        apk = app.get("apk_build") or ""
        latest = app.get("latest_build") or ""
        # 兼容：latest_build 按后缀分流
        if not aab and not apk and latest:
            l_abs = _resolve(self.base_dir, latest)
            if l_abs.lower().endswith(".apk"):
                apk = latest
            else:
                aab = latest
        return {"aab": _resolve(self.base_dir, aab) if aab else "",
                "apk": _resolve(self.base_dir, apk) if apk else ""}

    def artifact_for(self, app: Dict[str, Any], platform: str) -> str:
        """按平台选包：Google→AAB(缺则APK)，国内→APK(缺则AAB)。"""
        b = self._build_paths(app)
        if platform == "google":
            return b["aab"] or b["apk"]
        return b["apk"] or b["aab"]

    # ---- 转 Release ----
    def to_release(
        self,
        app_id: str,
        version_name: str = "",
        version_code: Optional[int] = None,
        platform: str = "google",
        release_notes: str = "",
        track: str = "",
        apk_path: str = "",
        aab_path: str = "",
        online_time: Optional[int] = None,
        catalog_fallback: bool = True,
    ) -> Release:
        """把应用元数据转成 Release。platform 决定默认选哪个包。

        显式传 apk_path/aab_path 可覆盖目录配置；version/release_notes 可显式提供。
        catalog_fallback=False 时不回落到目录里配置的 aab_build/apk_build
        （调用方已按目标平台自行解析路径时使用，避免无关平台的旧路径阻塞发布）。
        """
        app = self.get_app(app_id)
        package = app.get("package_name") or ""
        if not package:
            raise StoreError(f"应用 {app_id} 尚未配置 package_name（包名）")

        b = self._build_paths(app)
        use_aab = _expand(aab_path) if aab_path else (b["aab"] if catalog_fallback else "")
        use_apk = _expand(apk_path) if apk_path else (b["apk"] if catalog_fallback else "")

        # 校验（仅校验实际提供的路径存在）
        for label, p in (("AAB", use_aab), ("APK", use_apk)):
            if p and not Path(p).is_file():
                raise StoreError(f"应用 {app_id} 配置的{label}包不存在: {p}")

        vname = version_name or app.get("version_name") or ""
        vcode = version_code if version_code is not None else app.get("version_code")
        notes = release_notes or app.get("release_notes") or ""
        t = track or app.get("track") or "production"
        otime = online_time if online_time is not None else app.get("online_time")

        return Release(
            package_name=package,
            version_name=vname,
            version_code=vcode if vcode is not None else 0,
            apk_path=use_apk,
            aab_path=use_aab,
            release_notes=notes,
            track=t,
            title=app.get("name") or "",
            metadata={"app_id": app_id, "category": app.get("category", ""), "online_time": otime,
                      "store_name_cn": app.get("store_name_cn") or ""},
        )

    # ---- 更新 ----
    def update_app(self, app_id: str, fields: Dict[str, Any]) -> Dict[str, Any]:
        """更新目录中的应用字段（如 aab_build/apk_build/package_name/version_name），写回 JSON。"""
        allowed = {"package_name", "aab_build", "apk_build", "latest_build", "version_name", "version_code", "track", "notes", "name", "online_time", "icon", "store_name_cn"}
        unknown = set(fields) - allowed
        if unknown:
            raise StoreError(f"不支持的字段: {', '.join(sorted(unknown))}")
        # 读-改-写整体加锁，避免多线程（如 Web 并发请求）交错写坏 catalog
        with _catalog_lock:
            for cat in self.data.get("categories", []):
                for app in cat.get("apps", []):
                    if app.get("id") == app_id:
                        for k, v in fields.items():
                            if v == "" or v is None:
                                app.pop(k, None)
                            else:
                                app[k] = v
                        self._save()
                        return self.get_app(app_id)
        raise StoreError(f"目录中找不到应用: {app_id}")

    def _save(self) -> None:
        # 同目录唯一临时文件 + os.replace 原子替换；异常时尽力清理临时文件
        fd, tmp_name = tempfile.mkstemp(prefix=self.path.name + ".", suffix=".tmp", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json.dumps(self.data, ensure_ascii=False, indent=2))
            os.replace(tmp_name, self.path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def detect_local_builds(self, paths: Optional[List[str]] = None) -> Dict[str, List[Dict[str, Any]]]:
        """扫描常见目录找出可用的 AAB/APK（用于前端文件选择器）。"""
        base = [self.base_dir, Path.home() / "Downloads"]
        found: Dict[str, List[Dict[str, Any]]] = {"aab": [], "apk": []}
        for d in base:
            if not d.is_dir():
                continue
            for p in sorted(d.glob("*.aab")) + sorted(d.glob("*.apk")):
                try:
                    size = p.stat().st_size
                except OSError:
                    continue
                entry = {"path": str(p), "name": p.name, "size": size, "mtime": p.stat().st_mtime}
                key = "aab" if p.suffix.lower() == ".aab" else "apk"
                found[key].append(entry)
        for key in found:
            found[key].sort(key=lambda e: e["mtime"], reverse=True)
        return found

    def status_payload(self, app_id: str) -> Dict[str, Any]:
        app = self.get_app(app_id)
        b = self._build_paths(app)
        return {
            "id": app["id"],
            "name": app.get("name", ""),
            "category": app.get("category", ""),
            "category_display": app.get("category_display", ""),
            "package_name": app.get("package_name") or "",
            "aab_build": b["aab"],
            "apk_build": b["apk"],
            "aab_exists": bool(b["aab"]) and Path(b["aab"]).is_file(),
            "apk_exists": bool(b["apk"]) and Path(b["apk"]).is_file(),
            "latest_build": app.get("latest_build") or "",
            "version_name": app.get("version_name") or "",
            "version_code": app.get("version_code"),
            "release_notes": app.get("release_notes") or "",
            "track": app.get("track") or "production",
            "notes": app.get("notes", ""),
            "query_only": bool(app.get("query_only", False)),
            "huawei_harmony_package": app.get("huawei_harmony_package") or "",
        }


_cached: Optional[AppCatalog] = None
_catalog_lock = threading.Lock()


def get_catalog(path: str = DEFAULT_CATALOG) -> AppCatalog:
    global _cached
    # 单例的惰性初始化加锁，避免多线程下重复构造/覆盖
    with _catalog_lock:
        if _cached is None or str(_cached.path) != str(Path(path).expanduser()):
            _cached = AppCatalog(path)
        return _cached
