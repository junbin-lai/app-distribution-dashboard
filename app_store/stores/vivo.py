"""vivo 应用商店适配器（完整实现，基于官方文档+签名示例）。

凭证（config/credentials.json）：
  "vivo": {
    "access_key": "申请API传包服务后分配的 access_key",
    "access_secret": "access_key 配对的密钥"
  }

接口：
  POST https://developer-api.vivo.com.cn/router/rest （正式环境）
  - app.query.details    查询应用详情（已上架版本）
  - app.upload.apk.app   上传 APK（返回流水号）
  - app.sync.update.app  应用更新（支持 onlineType=2 + scheOnlineTime 定时）

签名：HMAC-SHA256，按参数 ASCII 排序拼接 key=value& 后加密
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import urllib.parse
from typing import Any, Dict, List

from ..base import StoreAdapter, StoreError
from ..models import AuditState, Platform, Release, SubmitResult, StoreStatus, short_schedule, utcnow_iso
from ..upload_progress import make_multipart_monitor

_DOMAIN = "https://developer-api.vivo.com.cn/router/rest"


class VivoAdapter(StoreAdapter):
    platform = Platform.VIVO
    display_name = "vivo 应用商店"
    availability = "ready"
    required_credential_fields = ()  # 已支持 apps 多应用映射，在 _cred_for 校验

    def __init__(self, credentials: Dict[str, Any]) -> None:
        super().__init__(credentials)
        self._apps = self.credentials.get("apps") or {}
        self._key = self.credentials.get("access_key") or ""
        self._secret = self.credentials.get("access_secret") or ""

    def check(self) -> List[str]:
        return []

    def _cred_for(self, pkg: str) -> tuple:
        """按包名取 access_key/access_secret；无 apps 时用顶层凭证。"""
        if self._apps:
            c = self._apps.get(pkg)
            if not c:
                raise StoreError(f"vivo 凭证 apps 中没有 {pkg} 的 access_key/access_secret")
            return c.get("access_key", ""), c.get("access_secret", "")
        return self._key, self._secret

    # ---- 签名 ----
    @staticmethod
    def _sign(params: Dict[str, Any], secret: str) -> str:
        items = sorted(params.items())
        s = "&".join(f"{k}={v}" for k, v in items)
        return hmac.new(secret.encode(), s.encode(), hashlib.sha256).hexdigest()

    @staticmethod
    def _md5(path: str) -> str:
        h = hashlib.md5()
        with open(path, "rb") as f:
            for c in iter(lambda: f.read(65536), b""):
                h.update(c)
        return h.hexdigest()

    def _call(self, method: str, biz: Dict[str, Any], files: Any = None, pkg: str = "") -> Dict[str, Any]:
        """组装公共参数+业务参数，签名后 POST。"""
        import requests as req
        key, secret = self._cred_for(pkg)

        params: Dict[str, Any] = {
            "access_key": key,
            "timestamp": str(int(time.time() * 1000)),
            "method": method,
            "v": "1.0",
            "sign_method": "hmac",
            "format": "json",
            "target_app_key": "developer",
        }
        params.update(biz)

        # 对除了文件之外的所有参数签名
        if files:
            sign_params = dict(params)
            params["sign"] = self._sign(sign_params, secret)
        else:
            params["sign"] = self._sign(params, secret)

        # POST body 为 form
        data = urllib.parse.urlencode(params).encode("utf-8")
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        if files:
            # multipart：参数里去掉签名、二进制单独传
            # VIVO 的 API：上传文件接口一般先把文件上传获得流水号（服务端拉取）？
            raise StoreError("VIVO 文件上传走专门接口（见 _get_apk_token），此处不适用")
        try:
            resp = req.post(_DOMAIN, data=data, headers=headers, timeout=120)
        except req.exceptions.ConnectionError as e:
            raise StoreError(f"vivo 网络连接失败: {e}")
        except req.exceptions.Timeout as e:
            raise StoreError(f"vivo 请求超时: {e}")
        try:
            payload = resp.json()
        except Exception:
            raise StoreError(f"vivo 非JSON: {resp.text[:300]}")
        if payload.get("code") != 0:
            raise StoreError(f"vivo {method}: {payload.get('msg', payload)}")
        return payload

    def publish(self, release: Release, dry_run: bool = False) -> SubmitResult:
        scb = (release.metadata or {}).get("_step_cb")
        if dry_run:
            # dry-run 也做本地校验：凭证字段非空、安装包存在
            key, secret = self._cred_for(release.package_name)
            if not key or not secret:
                raise StoreError("vivo 凭证缺少 access_key/access_secret")
            if not release.apk_path or not os.path.isfile(release.apk_path):
                raise StoreError(f"vivo APK 不存在: {release.apk_path}")
            return SubmitResult(self.platform, True, "vivo: dry-run 通过", state=AuditState.DRAFT)

        apk = release.apk_path
        if not apk or not os.path.isfile(apk):
            raise StoreError(f"vivo APK 不存在: {apk}")

        if scb: scb("上传 APK 到 vivo…")
        # 1) 上传 APK 拿流水号（multipart）: app.upload.apk.app
        pc = (release.metadata or {}).get("_progress_cb")
        up_resp = self._upload_apk(apk, release.package_name, cb=pc)
        serial = up_resp.get("serialnumber") or ""
        if not serial:
            raise StoreError(f"vivo 上传 APK 未返回 serialnumber: {up_resp}")

        if scb: scb("提交更新到 vivo…")
        # 2) 应用更新（同步）: app.sync.update.app
        meta = release.metadata or {}
        params = {
            "packageName": release.package_name,
            "versionCode": str(release.version_code or meta.get("version_code", up_resp.get("versionCode", ""))),
            "apk": serial,
            "fileMd5": self._md5(apk),
            "onlineType": 1,
            "updateDesc": release.release_notes or "",
        }
        # 定时
        ot = meta.get("online_time")
        if ot:
            import datetime as _dt
            try:
                ot_int = int(ot)
            except (ValueError, TypeError):
                try:
                    dt = _dt.datetime.strptime(str(ot).replace("T", " ")[:16], "%Y-%m-%d %H:%M")
                    ot_int = int(dt.timestamp() * 1000)
                except (ValueError, TypeError):
                    raise StoreError(f"online_time 格式错误: {ot!r}")
            params["onlineType"] = 2
            # vivo 定时时间按北京时间(UTC+8)格式化，显式指定时区，不依赖本地时区
            _tz8 = _dt.timezone(_dt.timedelta(hours=8))
            params["scheOnlineTime"] = _dt.datetime.fromtimestamp(ot_int / 1000, tz=_tz8).strftime("%Y-%m-%d %H:%M:%S")
        else:
            params["onlineType"] = 1

        payload = self._call("app.sync.update.app", params, pkg=release.package_name)
        return SubmitResult(self.platform, True, f"vivo: {payload.get('msg', '提交成功')}",
                            remote_reference=payload.get("data", {}).get("task_id", "") or serial,
                            state=AuditState.SUBMITTED, raw=payload)

    def _upload_apk(self, apk_path: str, package_name: str, cb=None) -> dict:
        """通过 multipart 上传 APK 到 vivo，返回 serialnumber 等。"""
        import requests as req
        import time
        md5 = self._md5(apk_path)
        key, secret = self._cred_for(package_name)
        params: dict = {
            "access_key": key,
            "timestamp": str(int(time.time() * 1000)),
            "method": "app.upload.apk.app",
            "v": "1.0",
            "sign_method": "hmac",
            "format": "json",
            "target_app_key": "developer",
            "packageName": package_name,
            "fileMd5": md5,
        }
        params["sign"] = self._sign(params, secret)
        file_size = os.path.getsize(apk_path)
        try:
            if cb:
                # multipart 用 MultipartEncoderMonitor 流式 → 实时进度
                f = open(apk_path, "rb")
                try:
                    fields = list(params.items()) + [
                        ("file", (apk_path.split("/")[-1], f, "application/vnd.android.package-archive")),
                    ]
                    body = make_multipart_monitor(fields, file_size, cb)
                    resp = req.post(_DOMAIN, data=body, headers={"Content-Type": body.content_type}, timeout=600)
                finally:
                    f.close()
            else:
                with open(apk_path, "rb") as f:
                    files = {"file": (apk_path.split("/")[-1], f, "application/vnd.android.package-archive")}
                    resp = req.post(_DOMAIN, data=params, files=files, timeout=600)
        except req.exceptions.ConnectionError as e:
            raise StoreError(f"vivo 上传网络连接失败: {e}")
        except req.exceptions.Timeout as e:
            raise StoreError(f"vivo 上传请求超时: {e}")
        try:
            payload = resp.json()
        except Exception:
            raise StoreError(f"vivo 上传错误(非JSON): {resp.text[:300]}")
        if payload.get("code") != 0:
            raise StoreError(f"vivo APK 上传失败: {payload.get('msg', payload)}")
        return payload.get("data", {})

    def query_status(self, package_name: str) -> StoreStatus:
        payload = self._call("app.query.details", {"packageName": package_name}, pkg=package_name)
        data = payload.get("data") or {}
        version = data.get("versionName") or ""
        vcode = data.get("versionCode") or ""
        status = data.get("status")  # 3=已上架? saleStatus=1?
        sale = data.get("saleStatus")
        update_desc = data.get("updateDesc") or ""
        online_type = data.get("onlineType")

        state = AuditState.UNKNOWN
        # status: 0=草稿/空 1=审核中 2=审核中 3=审核通过; saleStatus=0 不可售 1=可售(已上架)
        sche_val = data.get("scheOnlineTime") or data.get("sche_online_time") or ""
        if sale == 1:
            state = AuditState.PUBLISHED  # 已上架（可售）
        elif status in (1, 2):
            state = AuditState.REVIEWING
        elif status == 3:
            state = AuditState.PENDING  # 审核通过未上架（待发布/定时）
        elif status == 0:
            state = AuditState.DRAFT
        if state in (AuditState.PUBLISHED, AuditState.REVIEWING, AuditState.PENDING) and not version:
            state = AuditState.UNKNOWN

        # 已上架 vs 审核中/待发布分离
        # 版本归类按真实审核状态：仅"审核中"(status 1/2)才进 reviewing；
        # 待发布(审核通过未上架)由审核状态文字表达，草稿进 draft（不再误标"审核中"）
        live_names = [str(version)] if version and state == AuditState.PUBLISHED else []
        reviewing_names = [str(version)] if version and state == AuditState.REVIEWING else []
        draft_names = [str(version)] if version and state == AuditState.DRAFT else []

        # 审核状态文字（标准化）
        note = ""
        if state == AuditState.PENDING:
            if online_type == 2 and sche_val:
                note = f"待发布（定时 {short_schedule(sche_val)}）"
            else:
                note = f"{version} 审核通过"
        elif state == AuditState.REVIEWING:
            note = "审核中"
        elif state == AuditState.PUBLISHED:
            note = ""  # 已上架由徽章/已上架版本行表达，不进审核状态
        elif state == AuditState.DRAFT:
            note = "草稿"
        msgs = []
        if update_desc:
            msgs.append(f"更新说明: {update_desc}")
        return StoreStatus(self.platform, package_name, state,
                           live_version_names=live_names,
                           live_version_codes=[int(vcode)] if str(vcode).isdigit() and state == AuditState.PUBLISHED else [],
                           reviewing_version_names=reviewing_names,
                           draft_version_names=draft_names,
                           audit_note=note,
                           review_message="；".join(msgs),
                           raw=payload, checked_at=utcnow_iso())
