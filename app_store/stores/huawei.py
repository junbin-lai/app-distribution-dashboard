# -*- coding: utf-8 -*-
"""华为 AppGallery 适配器（Publishing API v2，Android）。

发布流程（基于华为 AppGallery Publishing API v2 官方接口行为）：
1. appid-list -> 获取 appId
2. upload-url -> 获取上传地址 + authCode
3. multipart 直传 APK 到 FileServer
4. PUT app-file-info -> 绑定上传文件到版本（fileType=5 APK）
5. PUT app-info -> 更新更新说明（可选）
6. app-submit -> 提交发布（APK 解析中自动轮询重试）
7. app-info -> 查询已上架版本
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import urllib.parse
from typing import Any, Dict, List, Tuple

from ..base import StoreAdapter, StoreError
from ..models import AuditState, Platform, Release, SubmitResult, StoreStatus, short_schedule, utcnow_iso
from ..upload_progress import ProgressFile, make_multipart_monitor

_DOMAIN = "https://connect-api.cloud.huawei.com"


class HuaweiAdapter(StoreAdapter):
    platform = Platform.HUAWEI
    display_name = "华为 AppGallery"
    availability = "ready"
    required_credential_fields = ()

    def __init__(self, credentials: Dict[str, Any]) -> None:
        super().__init__(credentials)
        self._apps = self.credentials.get("apps") or {}
        self._cid = self.credentials.get("client_id") or ""
        self._csec = self.credentials.get("client_secret") or ""
        # OAuth token 缓存：pkg -> (token, 过期时间戳)，加锁保证并发安全
        self._token_cache: Dict[str, Tuple[str, float]] = {}
        self._token_lock = threading.Lock()

    def _cred_for(self, pkg: str) -> dict:
        if self._apps:
            c = self._apps.get(pkg) or {}
            # 兼容 apps[pkg] 直接写 client_id 字符串的简写形式
            if isinstance(c, str):
                c = {"client_id": c, "client_secret": self._csec}
            if not c.get("client_id"):
                raise StoreError(f"华为凭证 apps 中没有 {pkg}")
            return c
        return {"client_id": self._cid, "client_secret": self._csec}

    # token 过期前提前续期的余量（秒）
    _TOKEN_REFRESH_MARGIN = 60

    def _token(self, pkg: str, force_refresh: bool = False) -> str:
        import requests
        now = time.time()
        with self._token_lock:
            if not force_refresh:
                cached = self._token_cache.get(pkg)
                # 命中缓存且距过期还有余量时直接复用
                if cached and cached[1] - now > self._TOKEN_REFRESH_MARGIN:
                    return cached[0]
            cred = self._cred_for(pkg)
            r = requests.post(
                f"{_DOMAIN}/api/oauth2/v1/token",
                json={
                    "grant_type": "client_credentials",
                    "client_id": cred.get("client_id"),
                    "client_secret": cred.get("client_secret"),
                },
                timeout=30,
            )
            try:
                d = r.json()
            except Exception:
                raise StoreError(f"华为 OAuth 返回非JSON: HTTP {r.status_code} {r.text[:200]}")
            tok = d.get("access_token")
            if not tok:
                # 只保留错误码/描述，不把整个响应体（可能含敏感字段）写进异常
                err = d.get("error") or d.get("ret", {}).get("code") or f"HTTP {r.status_code}"
                desc = d.get("error_description") or d.get("sub_error") or ""
                raise StoreError(f"华为 OAuth 失败: [{err}] {desc}".strip())
            # 华为 token 默认 1 小时有效，按返回的 expires_in 记录过期时间
            try:
                expires_in = int(d.get("expires_in") or 3600)
            except (ValueError, TypeError):
                expires_in = 3600
            self._token_cache[pkg] = (tok, now + expires_in)
            return tok

    def _headers(self, pkg: str, force_refresh: bool = False) -> dict:
        return {
            "client_id": self._cred_for(pkg)["client_id"],
            "Authorization": "Bearer " + self._token(pkg, force_refresh=force_refresh),
        }

    def _get(self, path: str, params: dict, pkg: str) -> dict:
        import requests
        r = requests.get(_DOMAIN + path, params=params, headers=self._headers(pkg), timeout=30)
        if r.status_code == 401:
            # token 失效：强制刷新后重试一次
            r = requests.get(_DOMAIN + path, params=params,
                             headers=self._headers(pkg, force_refresh=True), timeout=30)
        d = self._parse_json(r, path)
        if d.get("ret", {}).get("code") != 0:
            raise StoreError("华为 " + path + ": " + str(d))
        return d

    def _post(self, path: str, pkg: str, query: dict = None, body: dict = None) -> dict:
        import requests
        url = _DOMAIN + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        r = requests.post(url, json=body, headers=self._headers(pkg), timeout=60)
        if r.status_code == 401:
            r = requests.post(url, json=body,
                              headers=self._headers(pkg, force_refresh=True), timeout=60)
        d = self._parse_json(r, path)
        if d.get("ret", {}).get("code") != 0:
            raise StoreError("华为 " + path + ": " + str(d))
        return d

    def _put(self, path: str, pkg: str, query: dict = None, body: dict = None) -> dict:
        """PUT 请求（app-file-info / app-info 用 PUT）。"""
        import requests
        url = _DOMAIN + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        r = requests.put(url, json=body, headers=self._headers(pkg), timeout=60)
        if r.status_code == 401:
            r = requests.put(url, json=body,
                             headers=self._headers(pkg, force_refresh=True), timeout=60)
        d = self._parse_json(r, path)
        if d.get("ret", {}).get("code") != 0:
            raise StoreError("华为 " + path + ": " + str(d))
        return d

    @staticmethod
    def _parse_json(r, path: str) -> dict:
        """校验 HTTP 状态码并解析 JSON，失败统一包装为 StoreError。"""
        if r.status_code >= 400:
            raise StoreError(f"华为 {path}: HTTP {r.status_code} {r.text[:200]}")
        try:
            return r.json()
        except Exception:
            raise StoreError(f"华为 {path} 返回非JSON: HTTP {r.status_code} {r.text[:200]}")

    def _submit_with_retry(self, path: str, pkg: str, query: dict = None,
                           max_attempts: int = 8, interval: int = 30) -> dict:
        """提交发布，APK 解析中(204144660 parsing / 204144727 compiling)时轮询重试。

        华为解析 APK 需 1-2 分钟（大包更久），报解析中错误应重试
        而非直接失败。每次失败都提示去 AGC 后台完成。
        """
        import time as _t
        import requests
        last_payload: dict = {}
        for attempt in range(max_attempts):
            url = _DOMAIN + path
            if query:
                url += "?" + urllib.parse.urlencode(query)
            r = requests.post(url, json={}, headers=self._headers(pkg), timeout=60)
            try:
                d = r.json()
            except Exception:
                raise StoreError(f"华为提交返回非JSON: {r.status_code} {r.text[:200]}")
            last_payload = d
            ret = d.get("ret") or {}
            code = ret.get("code")
            if code == 0:
                return d
            msg = str(ret.get("msg", "") or ret.get("message", "")).lower()
            # 解析中/编译中 → 重试；msg 为空时仅按错误码兜底判定（华为偶发不带 msg）
            parsing = code in (204144660, 204144727) and (
                not msg or
                "parsing" in msg or "parse" in msg or "解析" in msg or
                "compil" in msg or "编译" in msg or "try again" in msg
            )
            if not parsing:
                raise StoreError(
                    f"华为 {path}: [{code}] {ret.get('msg', ret)}"
                    f"（APK 已上传成功，若需手动可在 AGC 后台完成：https://developer.huawei.com/consumer/cn/console）"
                )
            if attempt < max_attempts - 1:
                _t.sleep(interval)
        raise StoreError(
            f"华为提交超时（APK 仍在处理，可在 AGC 后台完成：https://developer.huawei.com/consumer/cn/console）"
        )

    # ---------- 查询 ----------
    def query_status(self, package_name: str, harmony_package: str = "") -> StoreStatus:
        """查询华为应用状态。

        harmony_package 非空时（如凯迪仕 Android 应用传入 com.kaadas.lock），
        同时查询 Android（v2）与 HarmonyOS（v3）版本并合并到同一状态卡片。
        """
        pk = package_name
        cred = self._cred_for(pk)
        app_kind = cred.get("app_kind", "android")

        # HarmonyOS 应用：直接用 v3 查询
        if app_kind == "harmony":
            return self._query_harmony(pk, cred.get("app_id", ""))

        # Android：v2 接口（appid-list → app-info）
        d = self._get("/api/publish/v2/appid-list", {"packageName": pk}, pk)
        appids = d.get("appids") or []
        app_id = None
        for a in appids:
            if a.get("value"):
                app_id = a["value"]
                break
        if not app_id:
            raise StoreError(f"华为未找到 {pk} 的 appId（需先在 AGC 创建应用）")

        s = self._query_android(pk, app_id)

        # 该应用还关联了 Harmony 包名 → 一并查询并合并展示
        if harmony_package:
            try:
                hcred = self._apps.get(harmony_package) or {}
                h_app_id = hcred.get("app_id") or ""
                if h_app_id:
                    hs = self._query_harmony(harmony_package, h_app_id)
                    # 已上架版本（标注平台）
                    android_names = [str(n) + "（Android）" for n in s.live_version_names]
                    hnames = [str(n) + "（Harmony）" for n in hs.live_version_names]
                    s.live_version_names = android_names + hnames
                    s.live_version_codes = list(s.live_version_codes) + list(hs.live_version_codes)
                    # 审核中/草稿版本（标注平台，与已上架版本一致）
                    s.reviewing_version_names = ([str(n) + "（Android）" for n in s.reviewing_version_names]
                                                 + [str(n) + "（Harmony）" for n in hs.reviewing_version_names])
                    s.draft_version_names = ([str(n) + "（Android）" for n in s.draft_version_names]
                                             + [str(n) + "（Harmony）" for n in hs.draft_version_names])
                    # 审核状态文字（两平台都保留，标注平台）
                    notes = []
                    if s.audit_note:
                        notes.append(s.audit_note + "（Android）")
                    if hs.audit_note:
                        notes.append(hs.audit_note + "（Harmony）")
                    s.audit_note = "；".join(notes)
                    # 状态合并：任一审核中→审核中；任一待发布→待发布；均通过才已上架
                    if AuditState.REVIEWING in (s.state, hs.state):
                        s.state = AuditState.REVIEWING
                    elif AuditState.PENDING in (s.state, hs.state):
                        s.state = AuditState.PENDING
                    elif AuditState.REJECTED in (s.state, hs.state):
                        s.state = AuditState.REJECTED
                    s.review_message = (s.review_message + "；" if s.review_message else "") + hs.review_message
                    s.raw = {"android": s.raw, "harmony": hs.raw}
                else:
                    s.review_message = (s.review_message + "；" if s.review_message else "") + f"Harmony({harmony_package}) 凭证缺 app_id"
            except StoreError as e:
                s.review_message = (s.review_message + "；" if s.review_message else "") + f"Harmony 查询失败: {e}"
        return s

    def _query_android(self, pkg: str, app_id: str) -> StoreStatus:
        """Android：v2 app-info 查询版本状态。"""
        dd = self._get("/api/publish/v2/app-info", {"appId": app_id, "lang": "zh-CN"}, pkg)
        ai = dd.get("appInfo") or {}
        live_version = ai.get("onShelfVersionNumber") or ""
        live_vcode = ai.get("onShelfVersionCode") or None
        curr_version = ai.get("versionNumber") or ""
        curr_vcode = ai.get("versionCode") or None
        release_state = ai.get("releaseState")
        # releaseState: 0=已上架 1=审核不通过 2=已下架 3=待上架 4=审核中
        # 5=升级审核中 6=申请下架 7=草稿 8=升级审核不通过 12=预审中
        if release_state in (4, 5, 12):
            state = AuditState.REVIEWING
        elif release_state == 3:
            state = AuditState.PENDING  # 审核通过待上架
        elif release_state == 0:
            state = AuditState.PUBLISHED
        elif release_state in (1, 8):
            state = AuditState.REJECTED
        elif release_state == 7:
            state = AuditState.DRAFT
        else:
            state = AuditState.UNKNOWN
        # 已上架版本
        live_names = [str(live_version)] if live_version else []
        live_codes = [int(live_vcode)] if live_vcode else []
        # 版本归类按 releaseState：仅真正"审核中"(4/5/12)才进 reviewing；
        # 待上架(3)/拒绝(1,8)的版本由 audit_note 文字表达（不再误标"审核中"），草稿(7)进 draft
        reviewing_names = []
        draft_names = []
        if curr_version and curr_version != live_version:
            if release_state in (4, 5, 12):
                reviewing_names = [str(curr_version)]
            elif release_state == 7:
                draft_names = [str(curr_version)]
        # 审核状态文字（标准化）
        note = ""
        if release_state in (4, 5, 12):
            note = f"{curr_version} 审核中" if curr_version else "审核中"
        elif release_state in (1, 8):
            note = f"{curr_version} 审核未通过" if curr_version else "审核未通过"
        elif release_state == 3:
            # 待上架：app-info 带 releaseTime（定时上架时间）时展示定时时间
            release_time = ai.get("releaseTime") or ""
            if release_time:
                note = f"待发布（定时 {short_schedule(release_time)}）"
            else:
                note = f"{curr_version} 审核通过" if curr_version else "审核通过"
        elif release_state == 0:
            note = ""  # 已上架由徽章/已上架版本行表达，不进审核状态
        return StoreStatus(
            self.platform, pkg, state,
            live_version_names=live_names, live_version_codes=live_codes,
            reviewing_version_names=reviewing_names,
            draft_version_names=draft_names,
            audit_note=note,
            review_message=f"releaseState={release_state}",
            raw=dd, checked_at=utcnow_iso(),
        )

    def _query_harmony(self, pkg: str, app_id: str) -> StoreStatus:
        """HarmonyOS：v3 app-info 直接按 appId 查询。"""
        dd = self._get("/api/publish/v3/app-info", {"appId": app_id}, pkg)
        ai = dd.get("appInfo") or {}
        live_version = ai.get("onShelfVersionNumber") or ""
        curr_version = ai.get("versionNumber") or ""
        vcode = ai.get("onShelfVersionCode") or ai.get("versionCode") or 0
        release_state = ai.get("releaseState")
        live_names = [str(live_version)] if live_version else []
        codes = [int(vcode)] if vcode else []
        if release_state in (4, 5, 12):
            state = AuditState.REVIEWING
        elif release_state == 3:
            state = AuditState.PENDING
        elif release_state == 0:
            state = AuditState.PUBLISHED
        elif release_state in (1, 8):
            state = AuditState.REJECTED
        elif release_state == 7:
            state = AuditState.DRAFT
        else:
            state = AuditState.UNKNOWN
        # 同 _query_android：仅真正"审核中"才进 reviewing，草稿进 draft，其余由 note 表达
        reviewing_names = []
        draft_names = []
        if curr_version and curr_version != live_version:
            if release_state in (4, 5, 12):
                reviewing_names = [str(curr_version)]
            elif release_state == 7:
                draft_names = [str(curr_version)]
        note = ""
        if release_state in (4, 5, 12):
            note = f"{curr_version} 审核中" if curr_version else "审核中"
        elif release_state in (1, 8):
            note = f"{curr_version} 审核未通过" if curr_version else "审核未通过"
        elif release_state == 3:
            # 待上架：app-info 带 releaseTime（定时上架时间）时展示定时时间
            release_time = ai.get("releaseTime") or ""
            if release_time:
                note = f"待发布（定时 {short_schedule(release_time)}）"
            else:
                note = f"{curr_version} 审核通过" if curr_version else "审核通过"
        elif release_state == 0:
            note = ""  # 已上架由徽章/已上架版本行表达，不进审核状态
        return StoreStatus(
            self.platform, pkg, state,
            live_version_names=live_names, live_version_codes=codes,
            reviewing_version_names=reviewing_names,
            draft_version_names=draft_names,
            audit_note=note,
            review_message=f"releaseState={release_state}",
            raw=dd, checked_at=utcnow_iso(),
        )

    # ---------- 发布 ----------
    def publish(self, release: Release, dry_run: bool = False) -> SubmitResult:
        scb = (release.metadata or {}).get("_step_cb")
        # HarmonyOS 应用当前仅支持查询版本（发布需在 AGC 控制台手动操作）
        cred = self._cred_for(release.package_name)
        if cred.get("app_kind", "android") == "harmony":
            raise StoreError("华为 Harmony 仅支持查询版本；发布请在 AGC 控制台操作")
        if dry_run:
            # dry-run 本地校验：凭证字段齐全 + 安装包存在（与其他国内平台一致）
            if not cred.get("client_id") or not cred.get("client_secret"):
                raise StoreError("华为 dry-run：缺少 client_id/client_secret")
            if not release.apk_path or not os.path.isfile(release.apk_path):
                raise StoreError(f"华为 dry-run：APK 不存在: {release.apk_path or '(未提供)'}")
            return SubmitResult(self.platform, True, "华为: dry-run 通过", state=AuditState.DRAFT)

        pkg = release.package_name
        apk = release.apk_path
        if not apk or not os.path.isfile(apk):
            raise StoreError(f"华为 APK 不存在: {apk}")

        if scb: scb("获取华为 appId…")
        # 1) appid-list -> appId
        d = self._get("/api/publish/v2/appid-list", {"packageName": pkg}, pkg)
        appids = d.get("appids") or []
        app_id = None
        for a in appids:
            if a.get("value"):
                app_id = a["value"]
                break
        if not app_id:
            raise StoreError(f"华为未找到 {pkg} 的 appId")

        if scb: scb("获取华为上传地址…")
        # 2) upload-url（GET 返回 uploadUrl+authCode）
        import requests
        file_size = os.path.getsize(apk)
        up = self._get(
            "/api/publish/v2/upload-url",
            {"appId": app_id, "releaseType": 1, "suffix": "apk"},
            pkg,
        )
        upload_url = up.get("uploadUrl") or ""
        auth_code = up.get("authCode") or ""
        if not upload_url:
            raise StoreError(f"华为未返回上传地址: {up}")

        if scb: scb("上传 APK 到华为…")
        # 3) multipart 直传（fields: authCode/fileCount/name/parseType + file）
        pc = (release.metadata or {}).get("_progress_cb")
        apk_name = os.path.basename(apk)
        if pc:
            f = open(apk, "rb")
            try:
                fields = [
                    ("authCode", auth_code),
                    ("fileCount", "1"),
                    ("name", apk_name),
                    ("parseType", "0"),
                ]
                # 有进度回调时用 Monitor 流式
                body = make_multipart_monitor(fields + [("file", (apk_name, f, "application/octet-stream"))], file_size, pc)
                r_up = requests.post(upload_url, data=body, headers={"Content-Type": body.content_type}, timeout=600)
            finally:
                f.close()
        else:
            with open(apk, "rb") as f:
                r_up = requests.post(upload_url, data={"authCode": auth_code, "fileCount": "1", "name": apk_name, "parseType": "0"}, files={"file": (apk_name, f)}, timeout=600)
        up_json = {}
        try:
            up_json = r_up.json()
        except Exception:
            raise StoreError(f"华为上传返回非JSON: {r_up.status_code} {r_up.text[:200]}")
        file_rsp = (up_json.get("result") or {}).get("UploadFileRsp") or {}
        if file_rsp.get("ifSuccess") != 1 and up_json.get("result", {}).get("resultCode") != "0":
            raise StoreError(f"华为上传失败: {r_up.text[:300]}")
        file_list = file_rsp.get("fileInfoList") or []
        if not file_list:
            raise StoreError(f"华为上传未返回文件信息: {r_up.text[:300]}")
        file_dest_url = file_list[0].get("fileDestUlr") or file_list[0].get("fileDestUrl") or ""
        if not file_dest_url:
            raise StoreError(f"华为上传缺 fileDestUrl: {r_up.text[:300]}")

        # 4) 绑定文件到版本（PUT app-file-info, fileType=5 APK）
        if scb: scb("绑定 APK 文件到版本…")
        self._put("/api/publish/v2/app-file-info", pkg, query={"appId": app_id, "releaseType": 1}, body={
            "fileType": 5,
            "files": [{"fileName": apk_name, "fileDestUrl": file_dest_url}],
        })

        # 5) 更新更新说明（可选）
        if release.release_notes:
            self._put("/api/publish/v2/app-info", pkg, query={"appId": app_id, "releaseType": 1}, body={"newFeatures": release.release_notes})

        # 6) 提交发布（支持定时）+ 解析中轮询
        if scb: scb("提交发布到华为 AppGallery…")
        meta = release.metadata or {}
        submit_query = {"appId": app_id, "releaseType": 1}
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
            # 华为 releaseTime 要求北京时间(+0800)，显式指定 UTC+8 时区，不依赖本地时区
            _tz8 = _dt.timezone(_dt.timedelta(hours=8))
            submit_query["releaseTime"] = _dt.datetime.fromtimestamp(ot_int / 1000, tz=_tz8).strftime(
                "%Y-%m-%dT%H:%M:%S+0800"
            )
        payload = self._submit_with_retry("/api/publish/v2/app-submit", pkg, query=submit_query)
        return SubmitResult(
            self.platform,
            True,
            f"华为: {payload.get('ret', {}).get('msg', '提交成功')}",
            remote_reference=str(file_dest_url),
            state=AuditState.SUBMITTED,
            raw=payload,
        )

    # ---------- 立即上线 ----------
    def release_now(self, package_name: str) -> dict:
        """审核通过但未到定时上架时间(releaseState=3 待上架)的版本改为立即上架。

        使用华为「更新版本上架时间」接口：
        PUT /api/publish/v2/on-shelf-time
          changeType=2  指定时间上架改为审核通过立即上架
          changeType=3  变更定时上架时间（releaseTime 必填）
        releaseType 目前仅支持 1=全网。
        """
        pkg = package_name
        cred = self._cred_for(pkg)
        if cred.get("app_kind", "android") == "harmony":
            raise StoreError("华为 Harmony 应用请在 AGC 控制台操作立即上架")

        d = self._get("/api/publish/v2/appid-list", {"packageName": pkg}, pkg)
        appids = d.get("appids") or []
        app_id = None
        for a in appids:
            if a.get("value"):
                app_id = a["value"]
                break
        if not app_id:
            raise StoreError(f"华为未找到 {pkg} 的 appId")

        import datetime as _dt
        # releaseTime 用北京时间(+0800)，显式指定时区，不依赖本地时区
        _tz8 = _dt.timezone(_dt.timedelta(hours=8))
        body = {
            "changeType": 2,  # 指定时间上架 → 审核通过立即上架
            # releaseTime 标记必选但 changeType=2 时以官方说明"changeType为3时有效"为准，
            # 这里传当前时间占位，避免部分网关强校验缺参
            "releaseTime": _dt.datetime.now(tz=_tz8).strftime("%Y-%m-%dT%H:%M:%S+0800"),
            "releaseType": 1,  # 全网（目前仅支持全网）
        }
        self._put("/api/publish/v2/on-shelf-time", pkg, query={"appId": app_id}, body=body)
        return {
            "app_id": app_id,
            "changeType": 2,
            "message": "已请求改为审核通过立即上架（原定时时间作废）",
        }