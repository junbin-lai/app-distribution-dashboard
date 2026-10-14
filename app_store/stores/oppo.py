# -*- coding: utf-8 -*-
"""OPPO 软件商店适配器（完整实现）。

凭证字段：client_id / client_secret
接口：GET /developer/v1/token(获取token)
      GET /resource/v1/upload/get-upload-url(获取上传配置)
      POST upload_url(上传文件)
      POST /resource/v1/app/upd(发布版本，支持online_type=1/2定时)
      GET /resource/v1/app/info(查询)
"""
# flake8: noqa
import hashlib, hmac, json, os, time, datetime as _dt
from typing import Any, Dict, List, Optional

from ..base import StoreAdapter, StoreError
from ..models import AuditState, Platform, Release, SubmitResult, StoreStatus, short_schedule, utcnow_iso
from ..upload_progress import make_multipart_monitor

_DOMAIN = "https://oop-openapi-cn.heytapmobi.com"


class OPPOAdapter(StoreAdapter):
    platform = Platform.OPPO
    display_name = "OPPO 软件商店"
    availability = "ready"
    required_credential_fields = ()  # 已在 __init__ / _cred_for 中校验

    def __init__(self, credentials: Dict[str, Any]) -> None:
        super().__init__(credentials)
        self._domain = self.credentials.get("domain", _DOMAIN).rstrip("/")
        self._client_id = self.credentials.get("client_id") or ""
        self._client_secret = self.credentials.get("client_secret") or ""
        self._token: str = ""
        self._token_for: str = ""
        self._token_expire: float = 0.0  # token 过期时间戳（0 表示无有效缓存）

    def check(self) -> List[str]:
        try:
            import requests
            return []
        except ImportError:
            return ["缺少 requests 依赖"]

    @staticmethod
    def _sign(secret: str, data: Dict[str, Any]) -> str:
        items = sorted(data.items())
        sign_str = "&".join(f"{k}={v}" for k, v in items)
        return hmac.new(secret.encode(), sign_str.encode(), hashlib.sha256).hexdigest()

    def _cred_for(self, pkg: str) -> tuple:
        """按包名取 client_id/client_secret；无 apps 时用顶层凭证。"""
        apps = self.credentials.get("apps") or {}
        if apps:
            c = apps.get(pkg)
            if not c:
                raise StoreError(f"OPPO 凭证 apps 中没有 {pkg} 的 client_id/client_secret")
            return c.get("client_id", ""), c.get("client_secret", "")
        return self._client_id, self._client_secret

    def _get_token(self, cid: str, csecret: str) -> str:
        """请求新 token 并记录过期时间（expires_in 秒，未返回时按 1 小时并留 5 分钟余量，待确认）。"""
        import requests as req
        try:
            resp = req.get(f"{self._domain}/developer/v1/token", params={
                "client_id": cid, "client_secret": csecret, "grant_type": "client_credentials",
            }, timeout=30)
        except (req.ConnectionError, req.Timeout) as e:
            raise StoreError(f"OPPO 获取 token 网络错误: {e}")
        try:
            d = resp.json()
        except Exception:
            raise StoreError(f"OPPO 获取 token 非JSON (HTTP {resp.status_code}): {resp.text[:300]}")
        if d.get("errno") != 0 or not d.get("data", {}).get("access_token"):
            raise StoreError(f"OPPO 获取 token 失败: {d}")
        data = d["data"]
        try:
            ttl = int(data.get("expires_in") or 3600)
        except (TypeError, ValueError):
            ttl = 3600
        self._token_expire = time.time() + max(ttl - 300, 60)
        return data["access_token"]

    def _request(self, method: str, path: str, data: Dict[str, Any] = None, files: Any = None,
                 pkg: str = "", _retried: bool = False) -> Dict[str, Any]:
        import requests as req
        cid, csecret = self._cred_for(pkg)
        if not self._token or self._token_for != cid or time.time() >= self._token_expire:
            self._token = self._get_token(cid, csecret)
            self._token_for = cid
        params = dict(data or {})
        # 清洗 None 值（requests form 编码对 None 与签名串不一致会导致签名失败）
        params = {k: (v if v is not None else "") for k, v in params.items()}
        params["access_token"] = self._token
        params["timestamp"] = int(time.time())
        params["api_sign"] = self._sign(csecret, params)
        try:
            if method.upper() == "GET":
                resp = req.get(self._domain + path, params=params, timeout=60)
            else:
                resp = req.post(self._domain + path, data=params, files=files, timeout=120)
        except (req.ConnectionError, req.Timeout) as e:
            raise StoreError(f"OPPO {path} 网络错误: {e}")
        try:
            payload = resp.json()
        except Exception:
            raise StoreError(f"OPPO {path} 非JSON (HTTP {resp.status_code}): {resp.text[:300]}")
        if payload.get("errno") != 0:
            # token 失效（errmsg 含 token 字样）时强制刷新并重试一次
            msg = str(payload.get("errmsg", ""))
            if not _retried and "token" in msg.lower():
                self._token = self._get_token(cid, csecret)
                self._token_for = cid
                return self._request(method, path, data=data, files=files, pkg=pkg, _retried=True)
            raise StoreError(f"OPPO {path}: {payload.get('errmsg', payload)}")
        return payload

    def _upload_file(self, file_path: str, cb=None, pkg: str = "", file_type: str = "apk") -> str:
        """上传文件并返回文件 url。file_type 按用途区分（apk/icon/pic 等）；
        图标与截图的正确 type 取值待确认（官方文档未明确，先按用途区分传入）。"""
        import requests as req
        cfg = self._request("GET", "/resource/v1/upload/get-upload-url", pkg=pkg)
        upload_url = cfg["data"]["upload_url"]
        upload_sign = cfg["data"]["sign"]
        file_size = os.path.getsize(file_path)
        try:
            if cb:
                with open(file_path, "rb") as f:
                    fields = [
                        ("sign", upload_sign),
                        ("type", file_type),
                        ("file", (os.path.basename(file_path), f, "application/octet-stream")),
                    ]
                    body = make_multipart_monitor(fields, file_size, cb)
                    resp = req.post(upload_url, data=body, headers={"Content-Type": body.content_type}, timeout=600)
            else:
                with open(file_path, "rb") as f:
                    resp = req.post(upload_url, data={"sign": upload_sign, "type": file_type},
                                    files={"file": (os.path.basename(file_path), f)}, timeout=600)
        except (req.ConnectionError, req.Timeout) as e:
            raise StoreError(f"OPPO 文件上传网络错误: {e}")
        try:
            r = resp.json()
        except Exception:
            raise StoreError(f"OPPO 上传返回非JSON (HTTP {resp.status_code}): {resp.text[:300]}")
        if r.get("errno") != 0:
            raise StoreError(f"OPPO 上传错误: {r}")
        return r["data"]["url"] if isinstance(r.get("data"), dict) else r["data"]

    def publish(self, release: Release, dry_run: bool = False) -> SubmitResult:
        scb = (release.metadata or {}).get("_step_cb")
        if dry_run:
            # dry-run 本地校验：凭证字段齐全 + 安装包存在
            cid, csecret = self._cred_for(release.package_name)
            if not cid or not csecret:
                raise StoreError(f"OPPO dry-run：{release.package_name} 缺 client_id/client_secret")
            apk = release.apk_path
            if not apk or not os.path.isfile(apk):
                raise StoreError(f"OPPO dry-run：APK 不存在: {apk}")
            return SubmitResult(self.platform, True, "OPPO: dry-run 通过", state=AuditState.DRAFT)
        apk = release.apk_path
        if not apk or not os.path.isfile(apk):
            raise StoreError(f"OPPO APK 不存在: {apk}")

        if scb: scb("获取 OPPO 上传地址…")
        # 读取 OPPO 现网资料（更新时自动复用，避免重复填）
        existing = self._request("GET", "/resource/v1/app/info", data={"pkg_name": release.package_name}, pkg=release.package_name).get("data") or {}
        meta = release.metadata or {}
        md5 = self._file_md5(apk)
        if scb: scb("上传 APK 到 OPPO…")
        pc = (release.metadata or {}).get("_progress_cb")
        apk_url = self._upload_file(apk, cb=pc, pkg=release.package_name)

        # 以 OPPO 现网资料为基底（更新已有应用：分类/版权/商务联系人/简介等
        # 全部沿用现网值，避免每次提交缺字段），再覆盖版本相关字段
        params = dict(existing)
        # 移除只读/管理字段（不允许回传）
        for _ro in ("app_id", "dev_id", "version_id", "app_key", "sign", "apk_full_url",
                    "apk_md5", "apk_size", "app_create_time", "create_time", "update_time",
                    "audit_status", "audit_status_name", "change_state", "old_audit_status",
                    "state", "type", "level", "reserve_state", "transfer_state",
                    "refuse_reason", "refuse_reason_with_sugg", "business_refuse_reason",
                    "online_info_offline_apply", "freeze_advice", "freeze_file",
                    "freeze_reason", "freeze_reason_with_sugg", "offline_info", "offline_time",
                    "online_time", "sche_online_time", "pic_url_material",
                    "landscape_pic_url_material", "video_url_material", "special_file_url",
                    "electronic_cert_url", "icp_url", "cover_url", "header_md5", "icon_md5",
            # 适配/设备/版本类字段由新 APK 决定，不应从旧版本继承
            "adaptive_equipment", "adaptive_type", "version_device", "version_type",
            "version_name", "resolution", "app_real_type"):
            params.pop(_ro, None)

        # 覆盖版本相关字段
        params.update({
            "pkg_name": release.package_name,
            "version_code": str(release.version_code or meta.get("version_code") or existing.get("version_code", "")),
            "version_name": release.version_name or meta.get("version_name") or existing.get("version_name", ""),
            "apk_url": json.dumps([{"url": apk_url, "md5": md5, "cpu_code": meta.get("cpu_code", 0)}], ensure_ascii=False),
            "app_name": meta.get("store_name_cn") or release.title or meta.get("appName") or existing.get("app_name", release.package_name),
            "second_category_id": meta.get("second_category_id") or existing.get("second_category_id", 0),
            "third_category_id": meta.get("third_category_id") or existing.get("third_category_id", 0),
            "summary": meta.get("summary") or existing.get("summary", ""),
            "detail_desc": meta.get("detail_desc") or existing.get("detail_desc", ""),
            "update_desc": release.release_notes or existing.get("update_desc", ""),
            "privacy_source_url": meta.get("privacy_source_url") or existing.get("privacy_source_url", ""),
            # icon/截图上传需带 pkg（多应用凭证按包名取），type 取值待确认（见 _upload_file 注释）
            "icon_url": (self._upload_file(meta["icon"], pkg=release.package_name, file_type="icon")
                         if meta.get("icon") else existing.get("icon_url", "")),
            "pic_url": (self._upload_images(meta["screenshots"], pkg=release.package_name, file_type="pic")
                        if meta.get("screenshots") else existing.get("pic_url", "")),
            "test_desc": meta.get("test_desc") or existing.get("test_desc", ""),
            # 商务联系人（优先 meta 配置，其次复用现网资料）
            "business_username": meta.get("business_username") or existing.get("business_username", ""),
            "business_email": meta.get("business_email") or existing.get("business_email", ""),
            "business_mobile": meta.get("business_mobile") or existing.get("business_mobile", ""),
            "business_qq": meta.get("business_qq") or existing.get("business_qq", ""),
            "business_wx": meta.get("business_wx") or existing.get("business_wx", ""),
            "business_address": meta.get("business_address") or existing.get("business_address", ""),
            "copyright_url": meta.get("copyright_url") or existing.get("copyright_url", ""),
        })

        # 清洗 None → 空串（现网资料部分字段为 null，None 会导致签名串
        # 出现 business_qq=None 而与 requests 实际发送值不一致 → 签名校验失败）
        params = {k: (v if v is not None else "") for k, v in params.items()}

        if scb: scb("提交资料到 OPPO…")
        # 定时发布
        import datetime as _dt
        ot = meta.get("online_time") or meta.get("onlineTime") or (release.metadata or {}).get("online_time")
        if ot:
            params["online_type"] = 2
            try:
                ot_int = int(ot)
            except (ValueError, TypeError):
                try:
                    dt = _dt.datetime.strptime(str(ot).replace("T", " ")[:16], "%Y-%m-%d %H:%M")
                    ot_int = int(dt.timestamp() * 1000)
                except (ValueError, TypeError):
                    raise StoreError(f"online_time 格式错误: {ot!r}")
            # 定时时间按东八区格式化（不依赖服务器本地时区）
            params["sche_online_time"] = _dt.datetime.fromtimestamp(
                ot_int / 1000, tz=_dt.timezone(_dt.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
        else:
            params["online_type"] = 1

        payload = self._request("POST", "/resource/v1/app/upd", data=params, pkg=release.package_name)
        return SubmitResult(self.platform, True, f"OPPO: {payload.get('errmsg', '提交成功')}",
                            remote_reference=str(payload.get("data", {}).get("task_id", "")),
                            state=AuditState.SUBMITTED, raw=payload)

    def query_status(self, package_name: str) -> StoreStatus:
        payload = self._request("GET", "/resource/v1/app/info", data={"pkg_name": package_name}, pkg=package_name)
        data = payload.get("data") or {}
        version = data.get("version_name") or ""
        vcode = data.get("version_code") or 0
        codes = [int(vcode)] if vcode else []

        audit_status = data.get("audit_status")
        audit_name = data.get("audit_status_name", "")
        change_state = data.get("change_state")
        update_desc = data.get("update_desc", "")
        online_type = data.get("online_type")
        sche_time = data.get("sche_online_time", "")
        # 状态映射（OPPO 审核状态：111=上线）
        def _to_int(v):
            try:
                return int(v)
            except (ValueError, TypeError):
                return None
        audit_i = _to_int(audit_status)
        change_i = _to_int(change_state)
        # OPPO 状态文字含明确的「不通过/驳回」时优先判为驳回（audit_status=444 已实测，
        # 其他驳回码可能 >111，不能只靠数值区间判断，否则会被误归为草稿）
        rejected_by_name = bool(audit_name and ("不通过" in audit_name or "驳回" in audit_name))
        state = AuditState.UNKNOWN
        if rejected_by_name or audit_i == 444 or change_i == 444:
            state = AuditState.REJECTED
        elif audit_i == 111 or change_i == 111:
            state = AuditState.PUBLISHED
        elif audit_i is not None and 0 < audit_i < 111:
            # 审核中；若已审核通过则进入"待发布"
            if audit_name and ("通过" in audit_name):
                state = AuditState.PENDING
            else:
                state = AuditState.REVIEWING
        elif audit_i == 0:
            state = AuditState.DRAFT
        # 其余未知数值码（含 >111 的未文档化状态）保持 UNKNOWN，不再落入草稿

        # 已上架版本：仅 state=PUBLISHED 时当前版本才是线上
        # 版本归类按真实审核状态：仅"审核中"才进 reviewing；
        # 待发布(PENDING)/审核拒绝由审核状态文字表达，草稿进 draft（不再误标"审核中"）
        live_names = [str(version)] if version and state == AuditState.PUBLISHED else []
        live_codes = [int(vcode)] if vcode and state == AuditState.PUBLISHED else []
        reviewing_names = [str(version)] if version and state == AuditState.REVIEWING else []
        draft_names = [str(version)] if version and state == AuditState.DRAFT else []

        # 审核状态文字（标准化；待发布已由右上角徽章表达，此处不带"待发布"字样）
        note = ""
        online_i = _to_int(online_type)
        if state == AuditState.PENDING:
            if online_i == 2 and sche_time:
                note = f"待发布（定时 {short_schedule(sche_time)}）"
            else:
                note = f"{version} 审核通过"
        elif state == AuditState.REVIEWING:
            note = "审核中"
        elif state == AuditState.REJECTED:
            note = f"{version} 审核未通过" if version else "审核未通过"
        elif state == AuditState.DRAFT:
            note = "草稿"
        elif state == AuditState.PUBLISHED:
            note = ""  # 已上架由徽章/已上架版本行表达，不进审核状态

        # 驳回时把平台驳回原因带给前端（hover/详情可见）
        review_msg = audit_name or ""
        if state == AuditState.REJECTED:
            reason = str(data.get("refuse_reason") or "").strip().replace("\r", "").replace("\n", " ")
            if reason:
                review_msg = f"{audit_name}：{reason}" if audit_name else reason

        return StoreStatus(self.platform, package_name, state,
                           live_version_names=live_names,
                           live_version_codes=live_codes,
                           reviewing_version_names=reviewing_names,
                           draft_version_names=draft_names,
                           audit_note=note,
                           review_message=review_msg,
                           raw=payload, checked_at=utcnow_iso())

    @staticmethod
    def _file_md5(path: str) -> str:
        h = hashlib.md5()
        with open(path, "rb") as f:
            for c in iter(lambda: f.read(65536), b""):
                h.update(c)
        return h.hexdigest()

    def _upload_images(self, paths: List[str], pkg: str = "", file_type: str = "pic") -> str:
        # 截图上传 type 取值待确认（官方文档未明确），先与 apk 区分传入
        return ",".join(self._upload_file(p, pkg=pkg, file_type=file_type) for p in paths)
