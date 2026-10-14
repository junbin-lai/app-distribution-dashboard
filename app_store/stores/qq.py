# -*- coding: utf-8 -*-
"""腾讯应用宝（腾讯开放平台）适配器。

凭证字段（config/credentials.json）：
  "qq": {
    "user_id": "...",             # 开发者 UserID（管理中心-账户信息）
    "access_secret": "...",       # API 接入密钥（管理中心-账户管理-API发布接口-申请开通后分配）
    "apps": {"com.xxx": "110..."} # 包名 -> 应用宝 app_id（安卓应用管理-应用首页查看）
  }

接口（正式环境 https://p.open.qq.com/open_file/developer_api，全部 POST 表单）：
  /query_app_detail         查询应用详情（含线上 VersionCode/VersionName）
  /get_file_upload_info     获取腾讯云 COS 预签名 URL + 上传流水号（每天限 100 次）
  PUT <pre_sign_url>        APK 原始字节直传 COS（Content-Type: application/octet-stream）
  /update_app               应用更新提审（APK 流水号+MD5+版本特性说明+发布类型，每天限 50 次，超时建议 60s+；
                            按 APK ABI 自动选槽位：纯 arm64 走 apk64_*，双架构/32 位/无 lib 走 apk32_*（兼容包））
  /query_app_update_status  审核状态（1 审核中 / 2 驳回 / 3 通过 / 8 开发者撤销）

签名：全部参数（公共 user_id/timestamp + 业务参数）按 ASCII 升序拼 k1=v1&k2=v2
（值为 null 不参与、参数名值不做 URL 编码）→ HmacSHA256(key=access_secret) → 小写 hex，追加 sign。
发布类型（update_app 的 deploy_type，必填）：
  1 = 审核通过后立即发布；2 = 定时发布（deploy_time 秒级时间戳，北京时间）。
  定时限制：至少提交审核后 6 小时、仅 30 天内（平台规则，适配器前置校验）。
限制：仅主账号调用；仅支持已上线应用的更新（不支持新应用首发）。
"""
import hashlib
import hmac
import os
import time
import zipfile
from typing import Any, Dict, List

from ..base import StoreAdapter, StoreError
from ..models import AuditState, Platform, Release, SubmitResult, StoreStatus, short_schedule, utcnow_iso
from ..upload_progress import ProgressFile

_DOMAIN = "https://p.open.qq.com/open_file/developer_api"


class QQAdapter(StoreAdapter):
    platform = Platform.QQ
    display_name = "腾讯应用宝"
    availability = "ready"
    required_credential_fields = ("user_id", "access_secret")

    def __init__(self, credentials: Dict[str, Any]) -> None:
        super().__init__(credentials)
        self._user_id = str(self.credentials.get("user_id") or "")
        self._secret = str(self.credentials.get("access_secret") or "")

    def check(self) -> List[str]:
        try:
            import requests  # noqa: F401
            return []
        except ImportError:
            return ["缺少 requests 依赖"]

    # ---------- 基础 ----------
    def _app_id(self, pkg: str) -> str:
        apps = self.credentials.get("apps") or {}
        aid = apps.get(pkg) or self.credentials.get("app_id") or ""
        if isinstance(aid, dict):  # 容错：apps 值为 dict 时取其 app_id 键
            aid = aid.get("app_id") or ""
        if not aid:
            raise StoreError(f"应用宝凭证 apps 中没有 {pkg} 的 app_id")
        return str(aid)

    def _sign(self, params: Dict[str, str]) -> str:
        items = sorted((k, v) for k, v in params.items() if v is not None)
        sign_str = "&".join(f"{k}={v}" for k, v in items)
        return hmac.new(self._secret.encode(), sign_str.encode(), hashlib.sha256).hexdigest()

    def _api(self, path: str, params: Dict[str, Any], timeout: int = 60) -> Dict[str, Any]:
        import requests as req
        # None 参数不参与签名也不发送（与头部签名规则"值为 null 不参与"一致），而非转空串
        data = {k: str(v) for k, v in params.items() if v is not None}
        data["user_id"] = self._user_id
        data["timestamp"] = str(int(time.time()))
        data["sign"] = self._sign(data)
        resp = req.post(
            _DOMAIN + path, data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=timeout,
        )
        try:
            payload = resp.json()
        except Exception:
            raise StoreError(f"应用宝 {path} 非JSON: {resp.text[:300]}")
        if payload.get("ret") != 0:
            raise StoreError(f"应用宝 {path}: ret={payload.get('ret')} {payload.get('msg', '')}")
        return payload

    @staticmethod
    def _file_md5(path: str) -> str:
        h = hashlib.md5()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()

    @staticmethod
    def _apk_is_pure_64bit(path: str) -> bool:
        """判断 APK 是否为纯 64 位安装包：lib/ 仅含 64 位 ABI（arm64-v8a / x86_64 等）。

        含 32 位 ABI（armeabi-v7a 等）或无 lib 目录（纯 Java 应用，全架构兼容）都按
        「32 位或 32&64 位兼容包」处理——双架构包若走 apk64_* 槽位，平台会按纯 64 位
        解析校验并拒绝（ret=4000046）。"""
        bit64 = {"arm64-v8a", "x86_64", "mips64", "mips64r2"}
        found = set()
        with zipfile.ZipFile(path) as z:
            for name in z.namelist():
                if name.startswith("lib/"):
                    parts = name.split("/")
                    if len(parts) >= 3 and parts[2]:
                        found.add(parts[1])
        if not found:
            return False
        return found.issubset(bit64)

    # ---------- 发布 ----------
    def publish(self, release: Release, dry_run: bool = False) -> SubmitResult:
        import requests as req
        scb = (release.metadata or {}).get("_step_cb")
        pc = (release.metadata or {}).get("_progress_cb")
        pkg = release.package_name
        apk = release.apk_path
        if not apk or not os.path.isfile(apk):
            raise StoreError(f"应用宝 APK 不存在: {apk}")
        app_id = self._app_id(pkg)

        # 只读校验：详情线上版本号（更新的 APK 版本号不能小于线上版本号）
        detail = self._api("/query_app_detail", {"pkg_name": pkg, "app_id": app_id})
        online_vc = detail.get("VersionCode") or detail.get("version_code") or 0
        try:
            online_vc = int(online_vc)
        except (TypeError, ValueError):
            online_vc = 0
        if release.version_code and online_vc and int(release.version_code) < online_vc:
            raise StoreError(
                f"应用宝: 待发布 versionCode({release.version_code}) 小于线上版本号({online_vc})，平台不允许更新")

        if dry_run:
            # dry-run 在一切副作用之前返回（不取上传凭证、不上传 COS、不提审）
            return SubmitResult(
                self.platform, True,
                f"应用宝: dry-run 通过（线上版本 code={online_vc}，未上传）",
                state=AuditState.DRAFT, raw={"online_version_code": online_vc},
            )

        if scb:
            scb("获取应用宝上传凭证…")
        info = self._api("/get_file_upload_info", {
            "pkg_name": pkg, "app_id": app_id,
            "file_type": "apk", "file_name": os.path.basename(apk),
        })
        pre_sign_url = info.get("pre_sign_url") or ""
        serial = info.get("serial_number") or ""
        if not pre_sign_url or not serial:
            raise StoreError(f"应用宝获取上传信息失败: {info}")

        if scb:
            scb("上传 APK 到应用宝（腾讯云 COS）…")
        fs = os.path.getsize(apk)
        # COS 直传自动重试：仅连接中断/超时重试（每次重新打开文件、从 0 重传，预签名 URL 不变）；
        # 拿到任何 HTTP 响应（成功或报错）都不重试，与原有行为一致
        uploaded = False
        last_err = None
        for attempt in range(1, 4):
            try:
                if pc:
                    body = ProgressFile(apk, fs, pc)
                    try:
                        # 显式 Content-Length：ProgressFile 无 __len__，requests 无法自动推断长度
                        resp = req.put(pre_sign_url, data=body,
                                       headers={"Content-Type": "application/octet-stream",
                                                "Content-Length": str(fs)}, timeout=1800)
                    finally:
                        body.close()
                else:
                    with open(apk, "rb") as f:
                        resp = req.put(pre_sign_url, data=f,
                                       headers={"Content-Type": "application/octet-stream",
                                                "Content-Length": str(fs)}, timeout=1800)
                if resp.status_code != 200:
                    raise StoreError(f"应用宝 COS 上传失败: HTTP {resp.status_code} {resp.text[:200]}")
                uploaded = True
                break
            except StoreError:
                raise  # HTTP 层错误不重试，保持原有行为
            except (req.exceptions.ConnectionError, req.exceptions.Timeout) as e:
                last_err = e
                if attempt < 3:
                    if scb:
                        scb(f"上传中断（{type(e).__name__}），{2 ** attempt} 秒后第 {attempt + 1} 次重试…")
                    time.sleep(2 ** attempt)
        if not uploaded:
            raise StoreError(
                f"应用宝 COS 上传失败（{attempt} 次尝试均连接中断，多为网络不稳或带宽被占用）: {last_err}")

        if scb:
            scb("提交应用宝更新（提审）…")
        meta = release.metadata or {}
        apk_md5 = self._file_md5(apk)
        params: Dict[str, Any] = {
            "pkg_name": pkg, "app_id": app_id,
        }
        # 提审槽位按 ABI 构成选择（双架构/32 位/无 lib → 兼容包 apk32_*；纯 64 位 → apk64_*）
        if self._apk_is_pure_64bit(apk):
            params["apk64_flag"] = 1
            params["apk64_file_serial_number"] = serial
            params["apk64_file_md5"] = apk_md5
        else:
            params["apk32_flag"] = 1
            params["apk32_file_serial_number"] = serial
            params["apk32_file_md5"] = apk_md5
        notes = release.whatsnew or release.release_notes or ""
        if notes:
            params["feature"] = notes  # 版本特性说明（更新说明）
        # 应用名：国内专用名（不带英文后缀，避免审核驳回）
        store_cn = meta.get("store_name_cn") or ""
        if store_cn:
            params["app_name"] = store_cn
        # 发布类型：定时上线（online_time）→ deploy_type=2 + deploy_time；否则审核通过后立即发布
        import datetime as _dt
        ot = meta.get("online_time")
        if ot:
            try:
                ot_sec = int(ot) // (1000 if int(ot) > 10 ** 11 else 1)
            except (ValueError, TypeError):
                try:
                    parsed = _dt.datetime.strptime(str(ot).replace("T", " ")[:16], "%Y-%m-%d %H:%M")
                    ot_sec = int(parsed.timestamp())
                except (ValueError, TypeError):
                    raise StoreError(f"应用宝: online_time 无法解析（时间戳或 YYYY-MM-DD[THH:MM]）: {ot!r}")
            now = _dt.datetime.now().timestamp()
            if ot_sec <= now:
                raise StoreError("应用宝: 定时发布时间必须晚于当前时间")
            if ot_sec - now < 6 * 3600:
                raise StoreError("应用宝: 定时发布时间至少需在提交审核 6 小时后（平台规则）")
            if ot_sec - now > 30 * 86400:
                raise StoreError("应用宝: 定时发布仅可选择 30 天内的时间（平台规则）")
            params["deploy_type"] = 2
            params["deploy_time"] = ot_sec
        else:
            params["deploy_type"] = 1
        payload = self._api("/update_app", params, timeout=90)
        return SubmitResult(
            self.platform, True,
            f"应用宝: {payload.get('msg') or '更新已提交审核'}",
            remote_reference=str(serial),
            state=AuditState.SUBMITTED, raw=payload,
        )

    # ---------- 查询 ----------
    def query_status(self, package_name: str, local_release: Dict[str, Any] = None) -> StoreStatus:
        """local_release：本地最近一次成功提审任务的 {version_code, online_time}（可选）。

        应用宝 /query_app_update_status 只有 1 审核中/2 驳回/3 通过/8 撤销，
        没有「待发布」状态：定时发布(deploy_type=2)审核通过后仍返回 3，
        与已真正上架无法区分。传入本地提审记录后，用「线上版本号仍小于
        最近提审版本号」推断定时未到、版本未上线 → 待发布（定时 时间）。
        """
        app_id = self._app_id(package_name)
        detail = self._api("/query_app_detail", {"pkg_name": package_name, "app_id": app_id})
        vname = str(detail.get("VersionName") or detail.get("version_name") or "")
        vcode = detail.get("VersionCode") or detail.get("version_code") or 0
        try:
            vcode_i = int(vcode)
        except (TypeError, ValueError):
            vcode_i = 0

        state = AuditState.PUBLISHED
        note = ""
        review_msg = ""
        audit = None
        try:
            st = self._api("/query_app_update_status", {"pkg_name": package_name, "app_id": app_id})
            audit = st.get("audit_status")
            reason = st.get("audit_reason") or ""
            try:
                audit = int(audit)
            except (TypeError, ValueError):
                audit = None
            if audit == 1:
                # 仅真正审核中才进 REVIEWING（统一版本归类约定；
                # 该接口不返回待审版本号，故 reviewing 列表为空，由审核状态文字表达）
                state = AuditState.REVIEWING
                note = "应用更新审核中"
            elif audit == 2:
                state = AuditState.REJECTED
                note = ("审核未通过：" + reason) if reason else "审核未通过"
            elif audit == 8:
                # 开发者主动撤销更新，线上老版本仍在架
                state = AuditState.PUBLISHED
                note = "上次更新已撤销（开发者撤回）"
            # audit == 3 审核通过（更新自动发布），保持 PUBLISHED
        except StoreError as e:
            review_msg = f"审核状态查询失败: {e}"

        # 已上架版本 = 详情返回的线上版本（仅 PUBLISHED 时；
        # REVIEWING/REJECTED 期间详情版本语义不明，不放已上架列表）
        live_names = [vname] if vname and state == AuditState.PUBLISHED else []
        live_codes = [vcode_i] if vcode_i and state == AuditState.PUBLISHED else []

        # 定时发布推断：审核通过(3)但线上版本号仍小于本地最近提审版本号
        # → 定时时间未到、版本未上线。仅 audit==3 时推断（撤销(8)/查询失败不猜）。
        if (local_release and audit == 3 and state == AuditState.PUBLISHED
                and local_release.get("online_time") and vcode_i):
            try:
                local_vc = int(local_release.get("version_code") or 0)
            except (TypeError, ValueError):
                local_vc = 0
            if local_vc > vcode_i:
                state = AuditState.PENDING
                note = f"待发布（定时 {short_schedule(local_release['online_time'])}）"
                # 已上架版本仍是线上老版本（上面已按 PUBLISHED 算好，保留展示）

        return StoreStatus(
            self.platform, package_name, state,
            live_version_names=live_names,
            live_version_codes=live_codes,
            audit_note=note,
            review_message=review_msg,
            checked_at=utcnow_iso(),
            raw={"detail": detail},
        )
