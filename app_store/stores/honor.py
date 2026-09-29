# -*- coding: utf-8 -*-
"""荣耀应用市场适配器（基于荣耀官方 API 传包服务指引，doc 101359）。

凭证：client_id / client_secret（管理中心>开放能力>凭证）
官方文档：https://developer.honor.com/cn/doc/guides/101359
本地存档：docs/honor_api_guide.txt

发布流程（官方时序）：
1. get-app-id              根据包名查 APPID
2. get-app-detail          查应用详细信息（更新时复用现网资料）
3. get-file-upload-url     获取文件上传路径（URL 带 ?appId=，Body 为 List<UploadFile>）
4. file-upload             上传文件（multipart，?appId=&objectId=）
5. update-file-info        绑定文件（bindingFileList）
6. update-app-info         更新应用信息（复用现网 basicInfo）
7. update-language-info    更新多语言信息（复用现网 languageInfo）
8. submit-audit            提交审核（releaseType）

关键字段：fileType=100(APK应用包)；APK 包名须与应用绑定包名一致，版本 >= 已上架。
"""
# flake8: noqa
import datetime
import hashlib
import json
import os
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from ..base import StoreAdapter, StoreError
from ..models import AuditState, Platform, Release, SubmitResult, StoreStatus, utcnow_iso
from ..upload_progress import make_multipart_monitor

_IAM_URL = "https://iam.developer.honor.com/auth/token"
_OPENAPI = "https://appmarket-openapi-drcn.cloud.honor.com/openapi/v1/publish"

# 文件类型：100=APK 应用包（其余：1=图标 3=应用介绍截图 等，详见文档文件类型表）
_FILE_TYPE_APK = 100


def _parse_release_time(ts: str) -> Optional[datetime.datetime]:
    """解析荣耀 publishInfo.releaseTime（如 2026-09-14T09:27:49+0800）为带时区时间；失败返回 None。"""
    if not ts:
        return None
    try:
        return datetime.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S%z")
    except ValueError:
        try:
            return datetime.datetime.fromisoformat(ts.replace("+0800", "+08:00"))
        except ValueError:
            return None


class HonorAdapter(StoreAdapter):
    platform = Platform.HONOR
    display_name = "荣耀应用市场"
    availability = "ready"
    required_credential_fields = ()

    def __init__(self, credentials: Dict[str, Any]) -> None:
        super().__init__(credentials)
        self._apps = self.credentials.get("apps") or {}
        self._cid = self.credentials.get("client_id") or ""
        self._csecret = self.credentials.get("client_secret") or ""
        # token 缓存：client_id -> (token, 过期时间戳)，加锁保护并发
        self._tokens: Dict[str, Tuple[str, float]] = {}
        self._token_lock = threading.Lock()

    def check(self) -> List[str]:
        try:
            import requests
            return []
        except ImportError:
            return ["缺少 requests"]

    def _cred_for(self, pkg: str) -> tuple:
        if self._apps:
            c = self._apps.get(pkg) or {}
            if not c.get("client_id"):
                raise StoreError(f"荣耀凭证 apps 中没有 {pkg}")
            return c.get("client_id", ""), c.get("client_secret", "")
        return self._cid, self._csecret

    def _token(self, pkg: str) -> str:
        """获取 token（按 client_id 缓存，按 expires_in 过期，提前 5 分钟刷新）。"""
        import requests as req
        cid, sec = self._cred_for(pkg)
        with self._token_lock:
            cached = self._tokens.get(cid)
            if cached and time.time() < cached[1]:
                return cached[0]
        # 锁外请求，避免长时间持锁；并发下可能重复请求 token，无副作用
        try:
            resp = req.post(_IAM_URL, data={
                "grant_type": "client_credentials",
                "client_id": cid,
                "client_secret": sec,
            }, timeout=30)
        except (req.ConnectionError, req.Timeout) as e:
            raise StoreError(f"荣耀获取 token 网络错误: {e}")
        try:
            d = resp.json()
        except Exception:
            raise StoreError(f"荣耀获取 token 非JSON (HTTP {resp.status_code}): {resp.text[:300]}")
        tok = d.get("access_token")
        if not tok:
            raise StoreError(f"荣耀获取 token 失败: {d}")
        try:
            ttl = int(d.get("expires_in") or 3600)
        except (TypeError, ValueError):
            ttl = 3600
        with self._token_lock:
            self._tokens[cid] = (tok, time.time() + max(ttl - 300, 60))
        return tok

    def _get(self, pkg: str, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        import requests as req
        tok = self._token(pkg)
        url = _OPENAPI + path
        try:
            resp = req.get(url, params=params, headers={"Authorization": f"Bearer {tok}"}, timeout=60)
        except (req.ConnectionError, req.Timeout) as e:
            raise StoreError(f"荣耀 {path} 网络错误: {e}")
        try:
            d = resp.json()
        except Exception:
            raise StoreError(f"荣耀 {path} 非JSON (HTTP {resp.status_code}): {resp.text[:300]}")
        if d.get("code") != 0:
            raise StoreError(f"荣耀 {path}: {d.get('msg', d)}")
        return d

    def _post(self, pkg: str, path: str, body: Any, query: Dict[str, Any] = None) -> Dict[str, Any]:
        """POST JSON。query: 附加到 URL 的 query 参数（如 appId）。"""
        import requests as req
        tok = self._token(pkg)
        url = _OPENAPI + path
        try:
            resp = req.post(url, json=body, params=query,
                            headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}, timeout=120)
        except (req.ConnectionError, req.Timeout) as e:
            raise StoreError(f"荣耀 {path} 网络错误: {e}")
        try:
            d = resp.json()
        except Exception:
            raise StoreError(f"荣耀 {path} 非JSON (HTTP {resp.status_code}): {resp.text[:300]}")
        if d.get("code") != 0:
            raise StoreError(f"荣耀 {path}: {d.get('msg', d)}")
        return d

    def _get_app_id(self, pkg: str) -> int:
        d = self._get(pkg, "/get-app-id", {"pkgName": pkg})
        apps = d.get("data") or []
        for a in apps:
            if a.get("packageName") == pkg:
                return int(a["appId"])
        raise StoreError(f"荣耀未找到 {pkg} 的 appId（需先在平台创建并绑定包名）")

    def _get_app_detail(self, pkg: str, app_id: int) -> Dict[str, Any]:
        """查询应用详细信息（更新时复用现网资料）。"""
        d = self._get(pkg, "/get-app-detail", {"appId": app_id})
        return d.get("data") or {}

    def _file_sha256(self, path: str) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for c in iter(lambda: f.read(65536), b""):
                h.update(c)
        return h.hexdigest()

    def publish(self, release: Release, dry_run: bool = False) -> SubmitResult:
        if dry_run:
            # dry-run 本地校验：凭证字段齐全 + 安装包存在
            cid, sec = self._cred_for(release.package_name)
            if not cid or not sec:
                raise StoreError(f"荣耀 dry-run：{release.package_name} 缺 client_id/client_secret")
            apk = release.apk_path
            if not apk or not os.path.isfile(apk):
                raise StoreError(f"荣耀 dry-run：APK 不存在: {apk}")
            return SubmitResult(self.platform, True, "荣耀: dry-run 通过", state=AuditState.DRAFT)

        scb = (release.metadata or {}).get("_step_cb")
        pc = (release.metadata or {}).get("_progress_cb")
        apk = release.apk_path
        if not apk or not os.path.isfile(apk):
            raise StoreError(f"荣耀 APK 不存在: {apk}")
        pkg = release.package_name
        fs = os.path.getsize(apk)
        apk_name = os.path.basename(apk)

        # 0) 获取 APPID + 现网资料（更新应用复用）
        if scb: scb("获取荣耀 appId…")
        app_id = self._get_app_id(pkg)
        if scb: scb("获取荣耀现网应用资料…")
        detail = self._get_app_detail(pkg, app_id)
        basic = detail.get("basicInfo") or {}
        langs = detail.get("languageInfo") or []
        old_publish = detail.get("publishInfo") or {}

        # 1) 获取文件上传路径（URL 带 ?appId=，Body 为 List<UploadFile>）
        if scb: scb("获取荣耀上传 URL…")
        up = self._post(pkg, "/get-file-upload-url", [{
            "fileName": apk_name,
            "fileType": _FILE_TYPE_APK,
            "fileSize": fs,
            "fileSha256": self._file_sha256(apk),
        }], query={"appId": app_id})
        uploads = up.get("data") or []
        if not uploads:
            raise StoreError(f"荣耀未返回上传路径: {up}")
        first = uploads[0] if isinstance(uploads, list) else uploads
        upload_url = first.get("uploadUrl") or ""
        object_id = first.get("objectId") or ""
        if not upload_url or object_id is None:
            raise StoreError(f"荣耀上传配置缺 uploadUrl/objectId: {first}")

        # 2) 上传文件（uploadUrl 已含 appId+objectId，multipart file）
        if scb: scb("上传 APK 到荣耀…")
        import requests as req
        tok = self._token(pkg)
        # 上传自动重试：仅连接中断/超时重试（每次重新打开文件、从 0 重传，上传 URL 不变）；
        # 拿到任何 HTTP 响应（成功或业务报错）都不重试，与应用宝 COS 直传的重连策略一致。
        uploaded = False
        last_err = None
        for attempt in range(1, 4):
            try:
                if pc:
                    f = open(apk, "rb")
                    try:
                        fields = [("file", (apk_name, f, "application/vnd.android.package-archive"))]
                        body = make_multipart_monitor(fields, fs, pc)
                        up_resp = req.post(upload_url, data=body,
                                           headers={"Authorization": f"Bearer {tok}", "Content-Type": body.content_type}, timeout=600)
                    finally:
                        f.close()
                else:
                    with open(apk, "rb") as f:
                        up_resp = req.post(upload_url, files={"file": (apk_name, f, "application/vnd.android.package-archive")},
                                           headers={"Authorization": f"Bearer {tok}"}, timeout=600)
                try:
                    up_d = up_resp.json()
                except Exception:
                    raise StoreError(f"荣耀文件上传返回非JSON (HTTP {up_resp.status_code}): {up_resp.text[:300]}")
                if up_d.get("code") != 0:
                    raise StoreError(f"荣耀文件上传失败: {up_d.get('msg', up_d)}")
                uploaded = True
                break
            except StoreError:
                raise  # HTTP 层/业务错误不重试，保持原有行为
            except (req.ConnectionError, req.Timeout) as e:
                last_err = e
                if attempt < 3:
                    if scb:
                        scb(f"上传中断（{type(e).__name__}），{2 ** attempt} 秒后第 {attempt + 1} 次重试…")
                    time.sleep(2 ** attempt)
        if not uploaded:
            raise StoreError(
                f"荣耀文件上传失败（{attempt} 次尝试均连接中断，多为网络不稳）: {last_err}")

        # 3) 更新文件信息（bindingFileList 绑定 APK 到版本）
        if scb: scb("绑定 APK 文件…")
        self._post(pkg, "/update-file-info", {
            "bindingFileList": [{"objectId": object_id}],
        }, query={"appId": app_id})

        # 4) 更新应用信息（复用现网 basicInfo）
        if scb: scb("更新应用信息…")
        app_info: Dict[str, Any] = {
            "appClassification": basic.get("appClassification", ""),
            "gameType": basic.get("gameType"),
            "supplyName": basic.get("supplyName", ""),
            "supplyNameEn": basic.get("supplyNameEn", ""),
            "devName": basic.get("devName", ""),
            "devNameEn": basic.get("devNameEn", ""),
            "webUrl": basic.get("webUrl", ""),
            "customerServiceEmail": basic.get("customerServiceEmail", ""),
            "customerServiceTel": basic.get("customerServiceTel", ""),
            "defaultLanguage": basic.get("defaultLanguage", "zh-CN"),
            "releaseCountry": basic.get("releaseCountry", "CN"),
            "paymentInfo": basic.get("paymentInfo", 1),
            "inAppPayment": basic.get("inAppPayment", ""),
            "ratingId": basic.get("ratingId", 3),
            "privacyPolicyUrl": basic.get("privacyPolicyUrl", ""),
            "publicationNumber": basic.get("publicationNumber"),
            "appRegistrationEntityStatus": basic.get("appRegistrationEntityStatus"),
            "appRegistrationNumber": basic.get("appRegistrationNumber"),
            "appRegistrationEntityName": basic.get("appRegistrationEntityName", ""),
            "unifiedSocialCreditId": basic.get("unifiedSocialCreditId", ""),
        }
        app_info = {k: (v if v is not None else "") for k, v in app_info.items()}
        self._post(pkg, "/update-app-info", app_info, query={"appId": app_id})

        # 5) 更新多语言信息（复用现网 languageInfo，替换 newFeature 为本次更新说明）
        if scb: scb("更新多语言信息…")
        lang_list = []
        if langs:
            lang_list = [{
                "languageId": lang.get("languageId", "zh-CN"),
                "appName": lang.get("appName", ""),
                "intro": lang.get("intro", ""),
                "briefIntro": lang.get("briefIntro", ""),
                "newFeature": release.release_notes or lang.get("newFeature", ""),
            } for lang in langs]
        else:
            lang_list = [{
                "languageId": "zh-CN",
                "appName": (release.metadata or {}).get("store_name_cn") or release.title or pkg,
                "intro": "",
                "briefIntro": "",
                "newFeature": release.release_notes or "",
            }]
        self._post(pkg, "/update-language-info", {
            "languageInfoList": lang_list,
            "setAll": 0,
        }, query={"appId": app_id})

        # 6) 提交审核
        if scb: scb("提交审核到荣耀…")
        meta = release.metadata or {}
        audit_body: Dict[str, Any] = {"releaseType": 1}
        ot = meta.get("online_time") or meta.get("onlineTime")
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
            audit_body["releaseType"] = 2
            # 定时时间按东八区格式化（不依赖服务器本地时区）
            audit_body["releaseTime"] = _dt.datetime.fromtimestamp(
                ot_int / 1000, tz=_dt.timezone(_dt.timedelta(hours=8))).strftime("%Y-%m-%dT%H:%M:%S+0800")
        if meta.get("force_update") or meta.get("forceUpdate"):
            audit_body["forceUpdate"] = 1
        audit = self._post(pkg, "/submit-audit", audit_body, query={"appId": app_id})

        release_id = audit.get("data", {}).get("releaseId", "") if isinstance(audit.get("data"), dict) else ""
        return SubmitResult(self.platform, True, "荣耀: 提交审核成功",
                            remote_reference=str(release_id), state=AuditState.SUBMITTED, raw=audit)

    def query_status(self, package_name: str) -> StoreStatus:
        app_id = self._get_app_id(package_name)
        d = self._get(package_name, "/get-app-current-release", {"appId": app_id})
        data = d.get("data") or {}
        if isinstance(data, list):
            data = data[0] if data else {}
        version = data.get("versionName") or ""
        vcode = data.get("versionCode") or ""
        audit = data.get("auditResult")
        audit_msg = data.get("auditMessage") or ""
        release_id = data.get("releaseId") or ""

        # 0审核中 1通过 2不通过 3其他 4编辑未提交
        # 已上架 vs 待发布：auditResult=1(通过)时查 publishInfo.releaseType
        # releaseType=1(立即) → 已上架；=2(定时) → 定时时间未到=待发布、已到=已生效上架
        # （上架后 auditResult/releaseType 不会变化，定时发布只能按 releaseTime 推断是否生效）
        release_type = None
        release_time = ""
        detail_ok = True
        try:
            det = self._get_app_detail(package_name, app_id)
            pi = det.get("publishInfo") or {}
            release_type = pi.get("releaseType")
            release_time = pi.get("releaseTime") or ""
        except Exception as e:
            # 详情获取失败不静默：记录原因；下方状态判定按"无法确认"处理
            detail_ok = False
            print(f"荣耀: 获取应用详情失败，无法判定发布方式（按未知处理）: {e}", file=sys.stderr)

        state = AuditState.UNKNOWN
        if audit == 1:
            if not detail_ok:
                # 详情获取失败：无法区分立即/定时发布，定时发布不能误判为已上架
                state = AuditState.UNKNOWN
            elif release_type == 2:
                # 定时发布：定时时间已过 → 版本已生效上架；未到 → 待发布
                scheduled = _parse_release_time(release_time)
                state = AuditState.PENDING if scheduled is None or scheduled > datetime.datetime.now().astimezone() else AuditState.PUBLISHED
            else:
                state = AuditState.PUBLISHED
        elif audit == 0:
            state = AuditState.REVIEWING
        elif audit == 4:
            state = AuditState.DRAFT
        elif audit == 2:
            state = AuditState.REJECTED

        # 已上架 vs 审核中/待发布分离
        # 版本归类按真实审核状态：仅"审核中"才进 reviewing；
        # 待发布/审核拒绝由审核状态文字表达，草稿进 draft（不再误标"审核中"）
        live_names = [str(version)] if version and state == AuditState.PUBLISHED else []
        reviewing_names = [str(version)] if version and state == AuditState.REVIEWING else []
        draft_names = [str(version)] if version and state == AuditState.DRAFT else []

        # 审核状态文字（标准化，剥离 HTML）
        note = ""
        if state == AuditState.PENDING:
            if release_time:
                # "2026-09-14T13:00:00+0800" → 转可读
                rt = release_time.replace("T", " ").replace("+0800", "")
                note = f"{version} 审核通过，定时发布（{rt}）"
            else:
                note = f"{version} 审核通过"
        elif state == AuditState.REVIEWING:
            note = "审核中"
        elif state == AuditState.REJECTED:
            note = "审核未通过"
        elif state == AuditState.DRAFT:
            note = "草稿"
        elif state == AuditState.PUBLISHED:
            note = ""  # 已上架由徽章/已上架版本行表达，不进审核状态

        msgs = []
        if release_id: msgs.append(f"releaseId: {release_id}")
        return StoreStatus(
            self.platform, package_name, state,
            live_version_names=live_names,
            live_version_codes=[int(vcode)] if str(vcode).isdigit() and state == AuditState.PUBLISHED else [],
            reviewing_version_names=reviewing_names,
            draft_version_names=draft_names,
            audit_note=note,
            review_message="；".join(msgs),
            raw=d, checked_at=utcnow_iso(),
        )
