
"""App Store（苹果）适配器：查询 + 发布（通过 App Store Connect API）。

查询：iTunes Lookup 公开接口为主，版本号再抓 apps.apple.com 页面校准（Lookup CDN 缓存可能滞后）。
发布（提交审核）：使用 App Store Connect API（基于 .p8 私钥 JWT 认证），
参考 GitHub 上普遍使用的 ES256 JWT + REST API 方案。

凭证配置（config/credentials.json）：
{
  "apple": {
    "country": "cn",
    "apps": {
      "com.conex.philips": 1671595666
    },
    "api_key": {
      "issuer_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
      "key_id": "XXXXXXXXXX",
      "private_key_path": "/path/to/AuthKey_XXXXXXXXXX.p8"
    }
  }
}

发布流程（提交已上传的构建版本供审核）：
1. 创建 App Store 版本（POST /v1/apps/{id}/appStoreVersions）
2. 查找构建并关联到版本（PATCH …/relationships/build）
3. 创建本地化信息（版本描述、更新说明）
4. 创建审核提交（POST /v1/reviewSubmissions）
5. 添加版本项目（POST /v1/reviewSubmissions/{id}/items）
"""
from __future__ import annotations

import base64
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..base import StoreAdapter, StoreError
from ..models import AuditState, Platform, Release, SubmitResult, StoreStatus, utcnow_iso

_BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

# App Store Connect API 端点
_ASC_BASE = "https://api.appstoreconnect.apple.com/v1"

# apps.apple.com 页面版本解析缓存：看板/飞书反复查询时避免每次抓大页面。
# key=package_name, value=(expire_monotonic, version)。失败结果也缓存（短 TTL），
# 防止网络抖动时每次都等 30s 超时。
_PAGE_VERSION_CACHE: Dict[str, tuple] = {}
_PAGE_VERSION_TTL_SECONDS = 300
_PAGE_VERSION_FAIL_TTL_SECONDS = 60


class AppleAdapter(StoreAdapter):
    platform = Platform.APPLE
    display_name = "Apple App Store"
    availability = "ready"
    required_credential_fields = ("country",)  # 查询只需 country；发布额外需要 api_key

    def __init__(self, credentials: Dict[str, Any]) -> None:
        super().__init__(credentials)
        self._api_key = credentials.get("api_key") or {}
        self._apps_map = self.credentials.get("apps") or {}

    def _country(self):
        return self.credentials.get("country") or "cn"

    def _app_id(self, package_name: str = "") -> str:
        """返回 Apple 数字 App ID（如 1671595666）或 bundleId。"""
        if package_name:
            aid = self._apps_map.get(package_name) or ""
            if aid: return str(aid)
        aid = self.credentials.get("app_id") or ""
        if aid: return str(aid)
        if package_name:
            return package_name
        raise StoreError("苹果商店缺少 app_id 或 apps 映射")

    def _apple_app_id(self, package_name: str) -> str:
        """获取 Apple 数字 App ID（必须为数字，用于 ASC API）。"""
        sid = self._app_id(package_name).strip()
        if sid.isdigit():
            return sid
        raise StoreError(f"Apple 需要数字 App Store ID（当前: {sid}）。请在 credentials 中配置数字 app_id")

    # ========== 查询（网页解析，已稳定） ==========

    def _http_get(self, url: str, headers: Dict[str, str] = None, timeout: int = 30) -> str:
        hdrs = {"User-Agent": _BROWSER_UA}
        if headers:
            hdrs.update(headers)
        req = urllib.request.Request(url, headers=hdrs)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")

    def _lookup(self, package_name: str) -> Dict[str, Any]:
        param = {"country": self._country(), "entity": "software"}
        appid_ = self._app_id(package_name)
        sid = appid_.strip()
        param["id" if sid.isdigit() else "bundleId"] = sid
        url = "https://itunes.apple.com/lookup?" + urllib.parse.urlencode(param)
        data = json.loads(self._http_get(url, headers={"User-Agent": ""}))
        if data.get("resultCount") != 1 or not data.get("results"):
            raise StoreError(f"Apple 查询无结果: 包 {package_name!r} id {appid_!r}")
        return data["results"][0]

    def fetch_icon(self, package_name: str) -> Tuple[bytes, str]:
        """iTunes Lookup 的 artworkUrl 取应用图标（PNG），供分享图使用。"""
        r0 = self._lookup(package_name)
        url = r0.get("artworkUrl512") or r0.get("artworkUrl100") or r0.get("artworkUrl60") or ""
        if not url:
            raise StoreError("iTunes Lookup 无 artwork 字段")
        req = urllib.request.Request(url, headers={"User-Agent": ""})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
        if not data:
            raise StoreError("artwork 下载为空")
        return data, "image/png"

    def _parse_page_version(self, html: str) -> str:
        m = re.search(r'"primarySubtitle":"版本\s*([0-9]+(?:\.[0-9]+){1,3})"', html)
        return m.group(1) if m else ""

    def _page_version_cached(self, package_name: str, track_url: str) -> str:
        """带缓存地抓 apps.apple.com 页面版本。

        Lookup 的 CDN 缓存可能滞后，页面版本更准；但页面抓取较慢，按包名缓存，
        成功 5 分钟、失败 1 分钟，避免看板/飞书反复查询时每次都等页面。
        """
        now = time.monotonic()
        hit = _PAGE_VERSION_CACHE.get(package_name)
        if hit is not None and hit[0] > now:
            return hit[1]
        value = ""
        if track_url:
            try:
                html = self._http_get(track_url)
                value = self._parse_page_version(html)
            except Exception:
                value = ""
        ttl = _PAGE_VERSION_TTL_SECONDS if value else _PAGE_VERSION_FAIL_TTL_SECONDS
        _PAGE_VERSION_CACHE[package_name] = (now + ttl, value)
        return value

    def query_status(self, package_name: str) -> StoreStatus:
        r0 = self._lookup(package_name)
        version = ""
        page_used = False
        track_url = r0.get("trackViewUrl") or ""
        v = self._page_version_cached(package_name, track_url)
        if v:
            version = v
            page_used = True
        if not version:
            version = r0.get("version") or ""
        state = AuditState.PUBLISHED if version else AuditState.UNKNOWN
        note = ""  # 已上架由徽章/已上架版本行表达，不进审核状态
        return StoreStatus(
            platform=self.platform, package_name=package_name, state=state,
            live_version_names=[version] if version else [],
            audit_note=note,
            review_message="",
            raw={"lookup": r0, "page_used": page_used},
            checked_at=utcnow_iso(),
        )

    # ========== 发布（App Store Connect API，框架待测） ==========

    def check(self) -> List[str]:
        """检查发布依赖。有 api_key 时校验私钥文件是否存在。"""
        problems: List[str] = []
        key_info = self._api_key
        if key_info:
            p8_path = key_info.get("private_key_path") or ""
            if p8_path and not Path(p8_path).expanduser().is_file():
                problems.append(f"Apple API 私钥 .p8 文件不存在: {p8_path}")
        return problems

    def _jwt_token(self) -> str:
        """生成 App Store Connect API JWT 令牌（ES256 签名）。

        参考 GitHub 普遍使用的 JWT + ECDSA 方案：
        - 用 cryptography 的 ECDSA 签名
        - 有效期 20 分钟（Apple 最大限制）
        - kid header 指向生成的 API Key ID
        """
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.backends import default_backend

        issuer_id = (self._api_key or {}).get("issuer_id") or ""
        key_id = (self._api_key or {}).get("key_id") or ""
        p8_path = (self._api_key or {}).get("private_key_path") or ""

        if not issuer_id or not key_id or not p8_path:
            raise StoreError("Apple 发布需要配置 api_key.issuer_id / key_id / private_key_path")

        p8_path = Path(p8_path).expanduser()
        if not p8_path.is_file():
            raise StoreError(f"Apple API 私钥文件不存在: {p8_path}")

        # 加载 .p8 私钥（PKCS#8 格式）
        p8_bytes = p8_path.read_bytes()
        private_key = serialization.load_pem_private_key(
            p8_bytes, password=None, backend=default_backend(),
        )

        now = int(time.time())
        payload = {
            "iss": issuer_id,
            "iat": now,
            "exp": now + 1200,  # 20 分钟
            "aud": "appstoreconnect-v1",
        }
        header = {"alg": "ES256", "kid": key_id, "typ": "JWT"}

        # JWT base64url 编码
        def _b64(data: bytes) -> str:
            return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

        header_b64 = _b64(json.dumps(header, separators=(",", ":")).encode())
        payload_b64 = _b64(json.dumps(payload, separators=(",", ":")).encode())
        message = (header_b64 + "." + payload_b64).encode()

        # ES256 签名（P-256 + SHA-256）
        signature = private_key.sign(message, ec.ECDSA(hashes.SHA256()))
        sig_b64 = _b64(signature)
        return f"{header_b64}.{payload_b64}.{sig_b64}"

    def _asc_request(self, method: str, path: str, body: Any = None) -> Dict[str, Any]:
        """发送 App Store Connect API 请求。

        Args:
            method: GET / POST / PATCH
            path: 如 /apps/123/appStoreVersions
            body: 请求体 dict

        Returns:
            解析后的 JSON 响应 data
        """
        token = self._jwt_token()
        url = _ASC_BASE + (path if path.startswith("/") else "/" + path)
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        data_bytes = json.dumps(body, ensure_ascii=False).encode() if body else None

        req = urllib.request.Request(url, data=data_bytes, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw)
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = json.loads(e.read().decode())
            except Exception:
                pass
            raise StoreError(
                f"App Store Connect API {method} {path} 失败: HTTP {e.code} {detail}",
            )
        except Exception as e:
            raise StoreError(f"App Store Connect API 请求失败: {e}")

    def _get_app_store_version_id(self, apple_app_id: str, version_string: str) -> Optional[str]:
        """查找已有 App Store 版本（用于更新已有版本而非创建新版本）。

        分页跟随 links.next 翻页（limit=200）；确实不存在返回 None，
        StoreError / 网络错误向上抛，不静默吞掉。
        """
        path: Optional[str] = f"/apps/{apple_app_id}/appStoreVersions?limit=200"
        while path:
            data = self._asc_request("GET", path)
            for v in data.get("data", []):
                attrs = v.get("attributes", {})
                if attrs.get("versionString") == version_string:
                    return v["id"]
            nxt = (data.get("links") or {}).get("next") or ""
            # links.next 为绝对 URL，转回 _asc_request 接受的相对路径
            path = nxt[len(_ASC_BASE):] if nxt.startswith(_ASC_BASE) else None
        return None

    def publish(self, release: Release, dry_run: bool = False) -> SubmitResult:
        """提交已上传的构建版本以进行审核（App Store Connect API）。

        前提：
        1. 构建已通过 Xcode / Transporter 上传至 App Store Connect
        2. 构建在 TestFlight 中已处理出口合规
        3. 凭证配置了 api_key（issuer_id, key_id, private_key_path）

        流程（参考 GitHub 主流实现）：
        1. 创建 App Store 版本 -> 2. 关联构建 -> 3. 填本地化信息（更新说明） -> 4. 提交审核
        """
        if not self._api_key:
            raise StoreError(
                "Apple 发布需要配置 api_key（issuer_id / key_id / private_key_path）。"
                "生成路径：App Store Connect → 用户和访问 → 集成 → App Store Connect API"
            )

        apple_app_id = self._apple_app_id(release.package_name)
        version_str = release.version_name or ""
        if not version_str:
            raise StoreError("Apple 发布需要提供 version_name（版本号，如 4.19.1）")
        release_notes = release.release_notes or ""

        if dry_run:
            return SubmitResult(self.platform, True,
                                f"Apple: dry-run 通过（将发布 {version_str} 给 App ID {apple_app_id}）",
                                state=AuditState.DRAFT)

        step_cb = (release.metadata or {}).get("_step_cb")

        # 1) 创建或获取 App Store 版本
        if step_cb: step_cb("创建 App Store 版本…")
        existing = self._get_app_store_version_id(apple_app_id, version_str)
        if existing:
            version_id = existing
            if step_cb: step_cb(f"版本 {version_str} 已存在，复用 ID: {version_id[:8]}…")
        else:
            body = {
                "data": {
                    "type": "appStoreVersions",
                    "attributes": {
                        "platform": "IOS",
                        "versionString": version_str,
                    },
                    "relationships": {
                        "app": {"data": {"type": "apps", "id": apple_app_id}},
                    },
                }
            }
            resp = self._asc_request("POST", "/appStoreVersions", body=body)
            version_id = resp.get("data", {}).get("id", "")
            if step_cb: step_cb(f"版本 {version_str} 已创建 ({version_id[:8]}…)")
            time.sleep(0.5)

        if not version_id:
            raise StoreError(f"Apple 版本创建失败（versionString: {version_str}）")

        # 2) 查找构建并关联到版本
        if step_cb: step_cb("查找匹配的构建版本…")
        build_id = self._find_build(apple_app_id, version_str)
        if not build_id:
            raise StoreError(
                f"Apple 未找到版本 {version_str} 的构建。请先用 Xcode / Transporter 上传构建，"
                f"并在 TestFlight 中完成出口合规处理"
            )
        # 关联构建
        self._asc_request("PATCH", f"/appStoreVersions/{version_id}/relationships/build", body={
            "data": {"type": "builds", "id": build_id},
        })
        if step_cb: step_cb(f"构建 {build_id[:8]}… 已关联到版本")

        # 3) 填写本地化信息（至少一条）
        if step_cb: step_cb("填写版本本地化（更新说明）…")
        self._set_version_localization(version_id, release_notes)

        # 4) 提交审核
        if step_cb: step_cb("创建审核提交…")
        self._submit_for_review(apple_app_id, version_id, version_str)

        if step_cb: step_cb("审核已提交")
        return SubmitResult(
            platform=self.platform, ok=True,
            message=f"Apple: 已提交 {version_str} 审核（App ID {apple_app_id}）",
            remote_reference=version_id,
            state=AuditState.SUBMITTED,
            raw={"version_id": version_id, "build_id": build_id, "version": version_str},
        )

    # ---- ASC API 辅助方法 ----

    def _find_build(self, apple_app_id: str, version_str: str) -> Optional[str]:
        """按版本号查找匹配的构建。

        先精确匹配 versionString；失败则尝试 preReleaseVersion 匹配。
        仅「确实不存在」返回 None；StoreError / 网络错误向上抛，不静默吞掉。
        """
        # 精确匹配
        build_data = self._asc_request(
            "GET",
            f"/builds?filter[app]={apple_app_id}&filter[version]={version_str}"
            f"&filter[processingState]=VALID&limit=5",
        )
        builds = build_data.get("data", [])
        if builds:
            return builds[0]["id"]
        # 试试前缀匹配（如 "4.19" 匹配 "4.19.1"）
        data2 = self._asc_request(
            "GET",
            f"/builds?filter[app]={apple_app_id}&filter[preReleaseVersion.version]={version_str}"
            f"&limit=5",
        )
        builds = data2.get("data", [])
        if builds:
            return builds[0]["id"]
        return None

    def _set_version_localization(self, version_id: str, whats_new: str) -> None:
        """设置版本本地化信息（描述、更新说明等）。至少需要一条本地化记录。"""
        if whats_new:
            body = {
                "data": {
                    "type": "appStoreVersionLocalizations",
                    "attributes": {
                        "locale": "zh-Hans",
                        "whatsNew": whats_new,
                    },
                    "relationships": {
                        "appStoreVersion": {"data": {"type": "appStoreVersions", "id": version_id}},
                    },
                }
            }
            self._asc_request("POST", "/appStoreVersionLocalizations", body=body)

    def _submit_for_review(self, apple_app_id: str, version_id: str, version_str: str) -> None:
        """创建审核提交（ReviewSubmission）并添加版本项目。

        流程（Apple 现行方案，GitHub 主流实现参考）：
        1. POST /v1/reviewSubmissions - 创建审核提交
        2. POST /v1/reviewSubmissions/{id}/items - 添加 APP_STORE_VERSION 项目
        """
        # 创建审核提交
        sub_body = {
            "data": {
                "type": "reviewSubmissions",
                "attributes": {"platform": "IOS"},
                "relationships": {
                    "app": {"data": {"type": "apps", "id": apple_app_id}},
                },
            }
        }
        sub_resp = self._asc_request("POST", "/reviewSubmissions", body=sub_body)
        sub_id = sub_resp.get("data", {}).get("id", "")
        if not sub_id:
            raise StoreError("Apple 审核提交创建失败")
        time.sleep(0.3)

        # 添加版本项目
        item_body = {
            "data": {
                "type": "reviewSubmissionItems",
                "relationships": {
                    "reviewSubmission": {"data": {"type": "reviewSubmissions", "id": sub_id}},
                    "appStoreVersion": {"data": {"type": "appStoreVersions", "id": version_id}},
                },
            }
        }
        try:
            self._asc_request("POST", "/reviewSubmissionItems", body=item_body)
        except Exception as e:
            # 添加审核项目失败：尝试删除已创建的 reviewSubmission，避免平台侧残留空提交
            try:
                self._asc_request("DELETE", f"/reviewSubmissions/{sub_id}")
            except Exception as de:
                print(f"Apple 清理 reviewSubmission {sub_id} 失败: {de}", file=sys.stderr)
            # 有些情况下必须先用 PATCH 设置 appStoreVersion 的 earliestReleaseDate / releaseType
            raise StoreError(f"Apple 添加审核项目失败: {e}")
