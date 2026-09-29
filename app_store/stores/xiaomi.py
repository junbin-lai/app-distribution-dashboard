"""小米应用商店适配器（基于小米官方 Example.py 完整实现）。

凭证字段（config/credentials.json -> "xiaomi"）：
  email          小米开放平台账号邮箱
  password       账号密码或私钥
  public_key     小米开放平台下载的 X509 公钥证书(.pem)路径

接口（官方示例确认）：
  POST https://api.developer.xiaomi.com/devupload/dev/push   推送/更新应用
  POST https://api.developer.xiaomi.com/devupload/dev/query  查询应用信息
  POST https://api.developer.xiaomi.com/devupload/dev/category 查询分类（公开）
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Dict, List, Optional

from ..base import StoreAdapter, StoreError
from ..models import AuditState, Platform, Release, SubmitResult, StoreStatus, utcnow_iso
from ..upload_progress import make_multipart_monitor

# 官方示例常量
DOMAIN = "https://api.developer.xiaomi.com/devupload"
PUSH_URL = DOMAIN + "/dev/push"
QUERY_URL = DOMAIN + "/dev/query"
CATEGORY_URL = DOMAIN + "/dev/category"

GROUP_SIZE = 128
ENCRYPT_GROUP_SIZE = GROUP_SIZE - 11  # PKCS1v1.5 RSA-1024 每段最大 117 字节


def _array_copy(src, src_pos: int, dst, dst_pos: int, length: int) -> None:
    for i in range(length):
        dst[dst_pos + i] = src[src_pos + i]


def _file_md5(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


class XiaomiAdapter(StoreAdapter):
    platform = Platform.XIAOMI
    display_name = "小米应用商店"
    availability = "ready"
    required_credential_fields = ()  # 支持 apps 多应用映射，在 _cred_for 校验

    def check(self) -> List[str]:
        problems: List[str] = []
        for mod in ("requests", "Crypto", "cryptography"):
            try:
                __import__(mod)
            except ImportError:
                problems.append(f"缺少依赖 {mod}（pip install requests pycryptodome cryptography）")
        return problems

    def _cred_for(self, pkg: str) -> dict:
        """按包名取 {email,password,public_key}；无 apps 时用顶层凭证。"""
        apps = self.credentials.get("apps") or {}
        if apps:
            c = apps.get(pkg) or {}
            if not c.get("email"):
                raise StoreError(f"小米凭证 apps 中没有 {pkg} 的 email/password/public_key")
            return c
        return self.credentials

    def _cert_path(self, pkg: str = "") -> str:
        cred = self._cred_for(pkg)
        p = os.path.expanduser(cred.get("public_key") or "")
        if not p or not os.path.isfile(p):
            raise StoreError(f"小米公钥证书不存在: {p!r}（小米开放平台下载 X509 公钥）")
        return p

    def _encrypt_by_public_key(self, text: str, pkg: str = "") -> str:
        """对签名 JSON 用 X509 公钥做 RSA-1024 分段加密，返回 hex。"""
        from cryptography.hazmat.backends import default_backend
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
        from cryptography.x509 import load_pem_x509_certificate
        from Crypto.Cipher import PKCS1_v1_5
        from Crypto.PublicKey import RSA

        with open(self._cert_path(pkg), "rb") as f:
            buff = f.read()
        cert = load_pem_x509_certificate(buff, default_backend())
        public_key = cert.public_key()
        pem = public_key.public_bytes(
            encoding=Encoding.PEM, format=PublicFormat.PKCS1
        )
        cipher = PKCS1_v1_5.new(RSA.import_key(pem))

        data = text.encode("utf-8")
        n = len(data)
        i = 0
        out = bytearray()
        while i < n:
            segsize = ENCRYPT_GROUP_SIZE if (n - i) > ENCRYPT_GROUP_SIZE else (n - i)
            segment = bytearray(segsize)
            _array_copy(data, i, segment, 0, segsize)
            out += cipher.encrypt(bytes(segment))
            i += segsize
        return out.hex()

    def _post(self, url: str, data: Dict[str, str], files: Any = None) -> Dict[str, Any]:
        import requests
        try:
            resp = requests.post(url, data=data, files=files, timeout=300)
        except (requests.ConnectionError, requests.Timeout) as e:
            raise StoreError(f"小米接口网络错误 {url}: {e}")
        try:
            payload = resp.json()
        except Exception:
            raise StoreError(f"小米接口返回非 JSON (HTTP {resp.status_code}): {resp.text[:300]}")
        return payload

    def publish(self, release: Release, dry_run: bool = False) -> SubmitResult:
        scb = (release.metadata or {}).get("_step_cb")
        if dry_run:
            # dry-run 本地校验：凭证字段齐全（含公钥证书）+ 安装包存在
            cred = self._cred_for(release.package_name)
            if not cred.get("email") or not cred.get("password"):
                raise StoreError(f"小米 dry-run：{release.package_name} 缺 email/password")
            self._cert_path(release.package_name)
            apk = release.apk_path
            if not apk or not os.path.isfile(apk):
                raise StoreError(f"小米 dry-run：APK 不存在: {apk}")
            return SubmitResult(
                platform=self.platform, ok=True,
                message="小米: dry-run 校验通过（未真实调用）",
                state=AuditState.DRAFT,
            )
        if not release.artifact_path():
            raise StoreError("小米发布需要 apk_path（小米仅支持 APK）")
        apk = release.apk_path
        if not apk or not os.path.isfile(apk):
            raise StoreError(f"小米 APK 不存在: {apk}")

        cred = self._cred_for(release.package_name)
        email = cred.get("email") or ""
        password = cred.get("password") or ""

        try:
            synchro_type = int(release.metadata.get("synchroType", 1)) if release.metadata else 1
        except (TypeError, ValueError) as e:
            raise StoreError(
                f"小米 synchroType 无法转为整数: {release.metadata.get('synchroType')!r} ({e})")
        app_detail: Dict[str, Any] = release.metadata.get("appDetail", {}) if release.metadata else {}
        app_detail.setdefault("appName", (release.metadata or {}).get("store_name_cn") or release.title or release.package_name)
        app_detail.setdefault("packageName", release.package_name)
        app_detail.setdefault("versionName", release.version_name or "")
        if release.release_notes:
            app_detail["updateDesc"] = release.release_notes
        # 定时上线（支持毫秒时间戳或 'YYYY-MM-DDTHH:MM'/'YYYY-MM-DD HH:MM' 字符串，按东八区解析）
        import datetime as _dt
        ot = (release.metadata or {}).get("online_time") or (release.metadata or {}).get("onlineTime")
        if ot:
            try:
                ot_ms = int(ot)
            except (ValueError, TypeError):
                try:
                    parsed = _dt.datetime.strptime(str(ot).replace("T", " ")[:16], "%Y-%m-%d %H:%M")
                    parsed = parsed.replace(tzinfo=_dt.timezone(_dt.timedelta(hours=8)))
                    ot_ms = int(parsed.timestamp() * 1000)
                except (ValueError, TypeError):
                    raise StoreError(f"online_time 无法解析（毫秒或 YYYY-MM-DD[THH:MM]）：{ot!r}")
            app_detail["onlineTime"] = ot_ms

        request_data = {
            "userName": email,
            "appInfo": json.dumps(app_detail, ensure_ascii=False),
            "synchroType": synchro_type,
        }

        sig_json: Dict[str, Any] = {
            "sig": [],
            "password": password,
        }
        sig_json["sig"].append({
            "name": "RequestData",
            "hash": hashlib.md5(json.dumps(request_data, ensure_ascii=False).encode("utf-8")).hexdigest(),
        })
        sig_json["sig"].append({"name": "apk", "hash": _file_md5(apk)})

        pc = (release.metadata or {}).get("_progress_cb")
        if scb: scb("签名并上传 APK 到小米…")
        encrypted = self._encrypt_by_public_key(json.dumps(sig_json, ensure_ascii=False), release.package_name)
        req_data = {
            "RequestData": json.dumps(request_data, ensure_ascii=False),
            "SIG": encrypted,
        }
        file_size = os.path.getsize(apk)
        if pc:
            import requests as _req
            f = open(apk, "rb")
            try:
                fields = list(req_data.items()) + [
                    ("apk", (os.path.basename(apk), f, "application/octet-stream")),
                ]
                body = make_multipart_monitor(fields, file_size, pc)
                try:
                    resp = _req.post(PUSH_URL, data=body, headers={"Content-Type": body.content_type}, timeout=300)
                except (_req.ConnectionError, _req.Timeout) as e:
                    raise StoreError(f"小米上传网络错误: {e}")
            finally:
                f.close()
            try:
                payload = resp.json()
            except Exception:
                raise StoreError(f"小米上传返回非 JSON (HTTP {resp.status_code}): {resp.text[:300]}")
        else:
            with open(apk, "rb") as f:
                files: Dict[str, Any] = {"apk": (os.path.basename(apk), f)}
                payload = self._post(PUSH_URL, data=req_data, files=files)

        if scb: scb("小米发布完成")
        # 小米 push 成功码以文档明确的 result=0 为准（其余历史兼容码已收窄，避免误判成功）
        ok = payload.get("result") in (0, "0")
        return SubmitResult(
            platform=self.platform,
            ok=ok,
            message=f"小米: {payload.get('msg') or payload}",
            state=AuditState.SUBMITTED if ok else AuditState.UNKNOWN,
            raw=payload,
        )

    def query_status(self, package_name: str) -> StoreStatus:
        cred = self._cred_for(package_name)
        email = cred.get("email") or ""
        password = cred.get("password") or ""
        request_data = {"packageName": package_name, "userName": email}
        sig_json = {
            "sig": [{
                "name": "RequestData",
                "hash": hashlib.md5(json.dumps(request_data, ensure_ascii=False).encode("utf-8")).hexdigest(),
            }],
            "password": password,
        }
        encrypted = self._encrypt_by_public_key(json.dumps(sig_json, ensure_ascii=False), package_name)
        payload = self._post(
            QUERY_URL,
            data={"RequestData": json.dumps(request_data, ensure_ascii=False), "SIG": encrypted},
        )
        # 小米 dev/query 返回 packageInfo：versionName/versionCode 是「最新（提审）版本」，
        # onlineVersionCode 是「线上版本号」（商店已上架）。接口没有审核状态字段。
        ok = payload.get("result") in (0, "0")
        info = payload.get("packageInfo") or {}
        version = info.get("versionName") or ""
        submitted_code = info.get("versionCode")
        online_code = info.get("onlineVersionCode")

        def _as_int(v):
            try:
                return int(v)
            except (TypeError, ValueError):
                return None

        submitted = _as_int(submitted_code)
        online = _as_int(online_code)

        # 小米没有审核状态/拒绝字段，只能按版本号大小推断：
        #   线上版本号 < 最新版本号  → 提审版本还没上线 → 审核中
        #   线上版本号 == 最新版本号 → 已上架
        # 这是近似处理：被拒时接口也不会告诉我们（仍会显示成「审核中」），
        # 等小米后续开放审核状态/拒绝字段后再改成准确归类。
        reviewing_names: List[str] = []
        live_names: List[str] = []
        live_codes: List[int] = []
        audit_note = ""
        if submitted is not None and online is not None and submitted > online:
            state = AuditState.REVIEWING
            if version:
                reviewing_names = [str(version)]
            audit_note = f"{version} 审核中" if version else "审核中"
            # 接口不返回线上版本名，只有线上版本号，补进备注便于确认商店当前还是哪个版本
            audit_note += f"（线上版本号 {online}）"
        elif version:
            state = AuditState.PUBLISHED
            live_names = [str(version)]
            code = online if online is not None else submitted
            if code is not None:
                live_codes = [code]
        else:
            state = AuditState.UNKNOWN
        # "查询成功"等操作信息不写入状态
        msg = payload.get("message") or ""
        if msg in ("查询成功", "成功", ""):
            msg = ""
        return StoreStatus(
            platform=self.platform,
            package_name=package_name,
            state=state,
            live_version_codes=live_codes,
            live_version_names=live_names,
            reviewing_version_names=reviewing_names,
            audit_note=audit_note,
            review_message=msg,
            raw=payload,
            checked_at=utcnow_iso(),
        )
