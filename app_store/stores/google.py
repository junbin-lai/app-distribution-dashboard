"""Google Play 适配器（androidpublisher v3）--- 完整可用。

凭证：service_account_json —— Google Play Console 创建的服务账号 JSON
（文件路径或内联 dict）。要求该账号有对应应用的发布权限。

发布流程（官方 edits API）：
  edits.insert -> bundles.upload(AAB) / apks.upload(APK) -> tracks.update -> edits.commit

上传超时已调大（默认 600s），适合大 AAB。
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..base import StoreAdapter, StoreError, TaskKilledError
from ..models import AuditState, Platform, Release, SubmitResult, StoreStatus, utcnow_iso

_ANDROIDPUBLISHER_SCOPE = "https://www.googleapis.com/auth/androidpublisher"
_UPLOAD_TIMEOUT = 300  # 秒，单次 socket 读/写超时（按次生效：健康上传每次 send 都很快完成不会触发；
                     # 只在网络完全卡死时触发断开重试；过长会让卡死的上传挂半小时，按需在凭证里覆盖）
_UPLOAD_MAX_RETRIES = 5  # 上传失败自动重试次数

# 商店页面「真正已上架版本」探测结果缓存：避免看板/飞书反复查询时每次抓页面。
# key=package_name, value=(expire_monotonic, version)。失败结果也缓存（短 TTL），
# 防止网络抖动时每次都等 30s 超时。
_PAGE_VERSION_CACHE: Dict[str, tuple] = {}
_PAGE_VERSION_TTL_SECONDS = 300
_PAGE_VERSION_FAIL_TTL_SECONDS = 60


class _ProgressFile:
    """文件包装：httplib2 分块读文件时回调当前已传大小 → 看板实时显示上传进度。"""

    def __init__(self, path: str, total: int, callback):
        self._f = open(path, "rb")
        self._total = total
        self._sent = 0
        self._cb = callback
        self._last = -1

    def read(self, n: int = -1) -> bytes:
        data = self._f.read(n)
        if data:
            self._sent += len(data)
            cur = self._sent // (512 * 1024)  # 每 512KB 汇报一次
            if cur > self._last:
                self._last = cur
                if self._cb:
                    self._cb(self._sent, self._total)
        return data

    def seek(self, offset: int, whence: int = 0) -> int:
        return self._f.seek(offset, whence)

    def tell(self) -> int:
        return self._f.tell()

    def close(self) -> None:
        self._f.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


# 上传 AAB 后是否需要等 processing 完成再 commit（大包 1-3 分钟）
_PROCESSING_POLL_SECONDS = 5
_PROCESSING_MAX_WAIT = 300


class GoogleAdapter(StoreAdapter):
    platform = Platform.GOOGLE
    display_name = "Google Play"
    availability = "ready"
    required_credential_fields = ()  # 支持 apps 多账号映射

    def __init__(self, credentials: Dict[str, Any]) -> None:
        super().__init__(credentials)
        self._upload_timeout = int(self.credentials.get("upload_timeout", _UPLOAD_TIMEOUT))
        self._upload_retries = int(self.credentials.get("upload_max_retries", _UPLOAD_MAX_RETRIES))
        self._apps = self.credentials.get("apps") or {}

    def _cred_for(self, pkg: str) -> Dict[str, Any]:
        """按包名取 service_account_json；无 apps 时用顶层。"""
        if self._apps:
            c = self._apps.get(pkg)
            if not c:
                raise StoreError(f"Google 凭证 apps 中没有 {pkg} 的 service_account_json")
            if isinstance(c, str):
                return {"service_account_json": c}
            return c
        return self.credentials

    def _play_page_version(self, package_name: str) -> str:
        """从 Google Play 商店页面抓取当前已上架版本（What's New 版本）。

        API 无审核状态字段，页面显示的版本号才是真正已上架版本；据此区分
        「已上架」与「API completed 但实际未生效/审核中」。结果按包名缓存，
        失败/超时返回空串（不阻塞查询）。
        """
        now = time.monotonic()
        hit = _PAGE_VERSION_CACHE.get(package_name)
        if hit is not None and hit[0] > now:
            return hit[1]

        value = ""
        try:
            import subprocess
            import sys as _sys
            probe = Path(__file__).resolve().parent.parent / "play_version_probe.py"
            if probe.is_file():
                py = getattr(_sys, "executable", "python3")
                proc = subprocess.run(
                    [py, "-B", str(probe), package_name],
                    capture_output=True, text=True, timeout=30,
                )
                if proc.returncode != 0:
                    # 子进程失败输出原因到 stderr（不静默吞掉，便于排查网络/解析问题）
                    err = (proc.stderr or "").strip()
                    print(f"Google Play 页面版本探测失败 (exit {proc.returncode}): {err[:300]}",
                          file=sys.stderr)
                else:
                    value = (proc.stdout or "").strip()
        except Exception as e:
            print(f"Google Play 页面版本探测异常: {e}", file=sys.stderr)

        ttl = _PAGE_VERSION_TTL_SECONDS if value else _PAGE_VERSION_FAIL_TTL_SECONDS
        _PAGE_VERSION_CACHE[package_name] = (now + ttl, value)
        return value

    def _load_creds(self, pkg: str = "") -> Dict[str, Any]:
        cred = self._cred_for(pkg) if pkg else self.credentials
        value = cred.get("service_account_json") or ""
        if isinstance(value, dict):
            return value
        p = Path(value).expanduser()
        if not p.is_file():
            raise StoreError(f"Google 服务账号 JSON 不存在: {p}")
        return json.loads(p.read_text(encoding="utf-8"))

    def check(self) -> List[str]:
        problems: List[str] = []
        try:
            import googleapiclient  # noqa: F401
        except ImportError:
            problems.append(
                "缺少 Google API 依赖，请安装: pip install app-store-publisher[google]"
                "（google-api-python-client / google-auth / google-auth-httplib2）"
            )
        return problems

    def _service(self, pkg: str = ""):
        try:
            from google.auth.transport.requests import Request  # noqa: F401
            from google.oauth2 import service_account
            from googleapiclient.discovery import build
            import googleapiclient.http
            import socket
        except ImportError as e:
            raise StoreError(
                f"缺少 Google API 依赖: {e}。安装: pip install app-store-publisher[google]"
            )
        creds = self._load_creds(pkg)
        scoped = service_account.Credentials.from_service_account_info(
            creds, scopes=[_ANDROIDPUBLISHER_SCOPE]
        )
        # 超时加大：上传大 AAB/APK 需要
        import httplib2
        from google_auth_httplib2 import AuthorizedHttp
        # httplib2 0.32 不支持 block_size 参数，仅加大 timeout
        http = httplib2.Http(timeout=self._upload_timeout)
        auth_http = AuthorizedHttp(scoped, http=http)
        return build("androidpublisher", "v3", http=auth_http, cache_discovery=False)

    def publish(self, release: Release, dry_run: bool = False) -> SubmitResult:
        if not release.aab_path and not release.apk_path:
            raise StoreError("发布前请提供 aab_path（Google 推荐）或 apk_path")
        # Google 优先 AAB
        path = Path(release.aab_path) if release.aab_path else Path(release.apk_path)
        if not path.is_file():
            raise StoreError(f"安装包不存在: {path}")
        if release.track not in ("production", "beta", "alpha", "internal"):
            raise StoreError(f"无效轨道: {release.track}")

        service = self._service(release.package_name)
        package = release.package_name
        edit = service.edits().insert(body={}, packageName=package).execute()
        edit_id = edit["id"]
        release_notes = release.whatsnew or release.release_notes or ""

        try:
            if dry_run:
                # dry-run 只做轻量校验：本地前置检查已过 + 凭证能创建 edit（对该应用有权限）。
                # 不真实上传安装包——edit 立即删除，零字节传输（原实现先整包上传再删 edit）。
                service.edits().delete(packageName=package, editId=edit_id).execute()
                return SubmitResult(
                    platform=self.platform,
                    ok=True,
                    message=f"dry-run 校验通过（{path.name}，未上传）",
                    remote_reference=edit_id,
                    state=AuditState.DRAFT,
                    raw={"edit_id": edit_id, "artifact": str(path)},
                )
            is_aab = path.suffix.lower() == ".aab"
            mime = "application/octet-stream" if is_aab else "application/vnd.android.package-archive"
            pc = (release.metadata or {}).get("_progress_cb")
            scb = (release.metadata or {}).get("_step_cb")
            fs = path.stat().st_size
            if scb:
                scb("开始上传安装包 " + path.name)

            if pc:
                # == httplib2 直接流式 POST 文件对象 ==
                # httplib2 分块 read+send 交替（实测 5MB→641 次），_ProgressFile 回调实时触发
                # 单连接（不分块续传），避免 resumable 第二个 PUT 被中间件挂起
                upload_path = "bundles" if is_aab else "apks"
                upload_url = (f"https://androidpublisher.googleapis.com/upload/androidpublisher/v3/"
                              f"applications/{package}/edits/{edit_id}/{upload_path}?uploadType=media")
                import httplib2 as _hb2
                # 手写上传路径不走 googleapiclient 的 num_retries，补有限重试（3 次指数退避）；
                # 每次重试重建 _ProgressFile（文件偏移需归零）
                uploaded = None
                last_err: Optional[Exception] = None
                for attempt in range(3):
                    pf = _ProgressFile(str(path), fs, pc)
                    try:
                        resp, content = service._http.request(
                            upload_url, method="POST", body=pf,
                            headers={"Content-Type": mime, "Content-Length": str(fs)},
                        )
                        if resp.status != 200:
                            raise StoreError(
                                f"Google Play 上传失败: HTTP {resp.status}: {content[:300]}")
                        uploaded = json.loads(content)
                        break
                    except StoreError:
                        raise
                    except TaskKilledError:
                        # 停止发布：不能被下面的重试逻辑吞掉，必须立刻向上传播中断线程
                        raise
                    except Exception as e:
                        last_err = e
                        print(f"Google Play 上传第 {attempt + 1} 次失败: {e}", file=sys.stderr)
                        if attempt < 2:
                            time.sleep(2 ** attempt)
                    finally:
                        pf.close()
                if uploaded is None:
                    raise StoreError(f"Google Play 上传失败（重试 3 次仍失败）: {last_err}")
            else:
                # 无进度回调时走 googleapiclient 标准 upload
                if is_aab:
                    uploaded = (
                        service.edits().bundles().upload(
                            packageName=package, editId=edit_id,
                            media_body=str(path), media_mime_type=mime,
                        ).execute(num_retries=self._upload_retries)
                    )
                else:
                    uploaded = (
                        service.edits().apks().upload(
                            packageName=package, editId=edit_id,
                            media_body=str(path), media_mime_type=mime,
                        ).execute(num_retries=self._upload_retries)
                    )
            if scb:
                scb("上传完成 (versionCode " + str(uploaded.get("versionCode") or "") + ")")
            version_code = uploaded.get("versionCode") or release.version_code

            # 大 AAB 上传后需等待 processing 完成
            if is_aab:
                if scb:
                    scb("等待 Google 处理（processing）…")
                self._wait_processing(service, package, edit_id, version_code)

            if scb:
                scb("提交发布（tracks.update → commit）…")

            auto_review = bool((release.metadata or {}).get("auto_review")) or bool(self.credentials.get("auto_review"))
            release_mode = str((release.metadata or {}).get("release_mode") or "staged").lower()
            # 发布方式（勾选「自动送审」时生效）：
            #   staged = 分阶段发布：inProgress + 百万分之一，审核通过后先放极小比例，再在看板「全量发布」
            #   full   = 全量发布：completed，审核通过后 Google 自动 100% 上线
            # 不自动送审时仍存草稿（status=draft，需到 Play Console 手动送审）
            if auto_review:
                if release_mode == "full":
                    release_body = {
                        "name": f"{version_code} ({release.version_name})" if release.version_name else str(version_code),
                        "versionCodes": [str(version_code)],
                        "status": "completed",
                        "releaseNotes": (
                            [{"language": "zh-CN", "text": release_notes}] if release_notes else []
                        ),
                    }
                else:
                    release_body = {
                        "name": f"{version_code} ({release.version_name})" if release.version_name else str(version_code),
                        "versionCodes": [str(version_code)],
                        "status": "inProgress",
                        "userFraction": 0.000001,  # 百万分之一，审核通过后几乎无人收到更新
                        "releaseNotes": (
                            [{"language": "zh-CN", "text": release_notes}] if release_notes else []
                        ),
                    }
            else:
                release_body = {
                    "name": f"{version_code} ({release.version_name})" if release.version_name else str(version_code),
                    "versionCodes": [str(version_code)],
                    "status": "draft",
                    "releaseNotes": (
                        [{"language": "zh-CN", "text": release_notes}] if release_notes else []
                    ),
                }
            track_body = {"track": release.track, "releases": [release_body]}
            service.edits().tracks().update(
                packageName=package, editId=edit_id, track=release.track, body=track_body
            ).execute()

            if auto_review:
                # 自动送审：提交 edit 让 Google 开始审核（审核通过后按所选发布方式上线）
                service.edits().commit(packageName=package, editId=edit_id).execute()
            else:
                # 存草稿不送审
                service.edits().commit(
                    packageName=package, editId=edit_id, changesNotSentForReview=True
                ).execute()
            mode_note = (
                "全量发布，审核通过后自动 100% 上线"
                if auto_review and release_mode == "full"
                else "分阶段发布，审核通过后按百万分之一比例自动上线，需后续手动调至 100%"
            )
            return SubmitResult(
                platform=self.platform,
                ok=True,
                message=(
                    f"已提交到 {release.track} 轨道（{path.name}，versionCode {version_code}）"
                    + (f"，已自动送审（{mode_note}）" if auto_review else "，已保存为草稿（需到 Play Console 手动送审）")
                ),
                remote_reference=edit_id,
                state=AuditState.SUBMITTED,
                raw={"edit_id": edit_id, "version_code": version_code, "is_aab": is_aab, "release_mode": release_mode},
            )
        except Exception as e:
            # StoreError 路径同样清理已创建的 edit；删除失败只记录，不掩盖原始错误
            try:
                service.edits().delete(packageName=package, editId=edit_id).execute()
            except Exception as de:
                print(f"Google Play 清理 edit {edit_id} 失败: {de}", file=sys.stderr)
            if isinstance(e, StoreError):
                raise
            raise StoreError(f"Google Play 发布失败: {e}")

    def _wait_processing(self, service, package: str, edit_id: str, version_code: Any) -> None:
        """等待大 AAB processing 完成后再 commit（避免 500）。不阻塞太久。"""
        import time
        waited = 0
        while waited < _PROCESSING_MAX_WAIT:
            try:
                bundles = (
                    service.edits()
                    .bundles()
                    .list(packageName=package, editId=edit_id)
                    .execute()
                )
                for b in bundles.get("bundles", []):
                    if b.get("versionCode") == str(version_code) and b.get("status") in ("ready", ""):
                        return
                time.sleep(_PROCESSING_POLL_SECONDS)
                waited += _PROCESSING_POLL_SECONDS
            except Exception as e:
                # 轮询异常不静默：记录到 stderr 后继续等待（偶发查询失败不影响最终结果）
                print(f"Google Play 等待 processing 轮询异常（继续等待）: {e}", file=sys.stderr)
                time.sleep(_PROCESSING_POLL_SECONDS)
                waited += _PROCESSING_POLL_SECONDS

    @staticmethod
    def _explain_error(e: Exception) -> str:
        """把 Google API 异常归类成人话提示：网络超时 / 账号权限 / 包名。

        网络类（socket 超时 Errno 60、连接被重置、DNS 失败等都是 OSError 子类）
        与权限/包名无关，国内访问 Google 需 VPN/代理，分开提示才不会误导排查方向。
        """
        if isinstance(e, OSError):
            return "网络连接失败，国内访问 Google 需 VPN/代理"
        status = getattr(e, "status_code", None)
        if status is None:
            status = getattr(getattr(e, "resp", None), "status", None)
        if status == 403:
            return "服务账号无权访问该应用，请到 Play Console 检查权限"
        if status == 404:
            return "包名不存在或账号无权访问"
        return "可能是包名/服务账号权限问题"

    def query_status(self, package_name: str) -> StoreStatus:
        service = self._service(package_name)
        try:
            edit = service.edits().insert(body={}, packageName=package_name).execute()
        except Exception as e:
            raise StoreError(f"Google Play 查询失败（{self._explain_error(e)}）: {e}")
        edit_id = edit["id"]
        try:
            resp = (
                service.edits()
                .tracks()
                .list(packageName=package_name, editId=edit_id)
                .execute()
            )
        except Exception as e:
            raise StoreError(f"Google Play 查询失败: {e}")
        finally:
            try:
                service.edits().delete(packageName=package_name, editId=edit_id).execute()
            except Exception:
                pass

        raw_tracks = resp.get("tracks", [])
        live_codes: List[int] = []
        live_names: List[str] = []
        draft_names: List[str] = []
        reviewing_names: List[str] = []
        pending_names: List[str] = []
        review_msgs: List[str] = []
        audit_note: str = ""
        release_now = False
        prod_completed: List[Dict[str, Any]] = []

        beta_names: List[str] = []
        alpha_names: List[str] = []
        internal_names: List[str] = []

        def _semver(text: str):
            m = re.search(r"(\d+\.\d+\.\d+)", text or "")
            if not m:
                return None
            return tuple(int(x) for x in m.group(1).split("."))

        def _ver_text(text: str) -> str:
            # 展示时只保留版本号，去掉 Google 自带的 versionCode 前缀
            v = _semver(text)
            return ".".join(str(x) for x in v) if v else (text or "")

        for track in raw_tracks:
            track_name = track.get("track", "?")
            for rel in track.get("releases", []):
                codes = [c for c in rel.get("versionCodes", [])]
                name = rel.get("name") or ""
                nm = _ver_text(name)
                status = rel.get("status", "")
                if status == "inProgress":
                    # 分阶段发布（inProgress + userFraction）：
                    #   · production 轨道 → 显示为「待发布」而非「审核中」。
                    #     Google Play API 没有审核状态字段，inProgress 既可能是「审核中」，
                    #     也可能是「审核通过后按比例灰度中」；我们用的比例是百万分之一，
                    #     约等于没人收得到更新，语义上更接近「还没正式发布」，故归为待发布。
                    #     （Google 后续若提供真实的待发布/拒绝字段，再改成准确归类。）
                    #   · 其他轨道（beta/alpha/internal）保持「审核中」，不参与全量发布。
                    if track_name == "production":
                        release_now = True
                        review_msgs.append(f"{track_name}:分阶段发布中 ({nm})")
                        if nm and nm not in pending_names:
                            pending_names.append(nm)
                    else:
                        review_msgs.append(f"{track_name}:审核中 ({nm})")
                        if nm and nm not in reviewing_names:
                            reviewing_names.append(nm)
                elif status == "draft":
                    review_msgs.append(f"{track_name}:草稿未送审 ({nm})")
                    if nm and nm not in draft_names:
                        draft_names.append(nm)
                elif status == "completed":
                    if track_name == "production":
                        prod_completed.append(
                            {"name": nm, "codes": codes, "semver": _semver(name)}
                        )
                    else:
                        # 内测/测试轨道单独收集
                        if track_name == "beta" and nm and nm not in beta_names:
                            beta_names.append(nm)
                        elif track_name == "alpha" and nm and nm not in alpha_names:
                            alpha_names.append(nm)
                        elif track_name == "internal" and nm and nm not in internal_names:
                            internal_names.append(nm)
                elif status == "halted":
                    review_msgs.append(f"{track_name}:已暂停 ({nm})")

        # API 的 status=completed 不代表审核通过/真正上架：审核被拒或尚未生效时，
        # Play API 也可能返回 completed。商店页面显示的版本才是真正已上架版本，
        # 因此用页面版本对 production 的 completed 发布做复核。
        page_ver = ""
        page_semver = None
        # 只要有正式版发布（completed 或分阶段 inProgress），就用商店页面版本复核，
        # 保证「已上架」始终显示真正在商店生效的版本。
        if prod_completed or release_now:
            page_ver = self._play_page_version(package_name)
            page_semver = _semver(page_ver)

        if page_ver:
            for cand in prod_completed:
                nm = cand["name"]
                sv = cand["semver"]
                if sv is not None and page_semver is not None and sv == page_semver:
                    if nm and nm not in live_names:
                        live_names.append(nm)
                    for c in cand["codes"]:
                        try:
                            live_codes.append(int(c))
                        except (TypeError, ValueError):
                            pass
                elif sv is not None and page_semver is not None and sv > page_semver:
                    # 比页面版本新但还没在商店生效：按审核中展示
                    review_msgs.append(f"production:审核中 ({nm})")
                    if nm and nm not in reviewing_names:
                        reviewing_names.append(nm)
                # 比页面版本旧的 completed 属于已被替换的旧发布，不展示
            if not live_names:
                # 轨道里没有与页面版本一致的 completed（例如只有被拒的新版本），
                # 页面版本才是真正已上架版本。
                live_names = [page_ver]
        else:
            # 拿不到商店页面版本时退回旧逻辑：completed 一律按已上架处理。
            for cand in prod_completed:
                nm = cand["name"]
                if nm and nm not in live_names:
                    live_names.append(nm)
                for c in cand["codes"]:
                    try:
                        live_codes.append(int(c))
                    except (TypeError, ValueError):
                        pass
            if prod_completed:
                audit_note = "未能核对商店页面版本，已上架按提交状态显示"

        # 状态优先级：分阶段发布中（待发布）> 审核中 > 已上架 > 草稿 > 未知
        if pending_names:
            state = AuditState.PENDING
        elif reviewing_names:
            state = AuditState.REVIEWING
        elif live_names:
            state = AuditState.PUBLISHED
        elif draft_names:
            state = AuditState.DRAFT
        elif raw_tracks:
            state = AuditState.DRAFT
        else:
            state = AuditState.UNKNOWN

        # 分阶段发布中 → 「待发布」的说明文字：
        # 原因见上面 inProgress 分支注释（API 无法区分审核中/灰度中，百万分之一≈未发布）。
        # 版本号只进 audit_note（审核状态行展示），不进 reviewing_names，避免又显示成「审核中」。
        if pending_names:
            audit_note = "；".join(
                f"{nm} 分阶段发布中（百万分之一，点「全量发布」正式上线）" for nm in pending_names
            )

        # 自管式发布（Managed Publishing）：审核通过后 Google 不会自动上架，需手动发布。
        if self.credentials.get("managed_publishing") and (reviewing_names or pending_names):
            audit_note = "自管式发布，审核通过后需在 Play Console 手动发布"

        extra = {}
        if beta_names:
            extra["beta_version_names"] = beta_names
        if alpha_names:
            extra["alpha_version_names"] = alpha_names
        if internal_names:
            extra["internal_version_names"] = internal_names
        return StoreStatus(
            platform=self.platform,
            package_name=package_name,
            state=state,
            live_version_codes=sorted(live_codes),
            live_version_names=live_names,
            draft_version_names=draft_names,
            reviewing_version_names=reviewing_names,
            audit_note=audit_note,
            google_release_now=release_now,
            review_message="；".join(review_msgs) or ("各轨道当前无发布记录" if not raw_tracks else ""),
            checked_at=utcnow_iso(),
            raw=raw_tracks,
            **extra,
        )

    def release_now(self, package_name: str) -> dict:
        """分阶段发布的版本改为全量发布（userFraction 从百万分之一调到 100%）。

        适用于「自动送审」时用 inProgress + 极低比例提交的版本：
        审核通过后 Google 自动按极低比例上线，调此接口把比例改到 100% 全量发布。
        """
        service = self._service(package_name)
        edit = service.edits().insert(body={}, packageName=package_name).execute()
        edit_id = edit["id"]
        try:
            tracks = service.edits().tracks().list(
                editId=edit_id, packageName=package_name
            ).execute()
            updated = False
            for track in tracks.get("tracks", []):
                # 只处理正式发布轨道，避免误把 beta/internal 的分阶段发布也转成全量
                if track.get("track") != "production":
                    continue
                rels = track.get("releases", [])
                changed = False
                for rel in rels:
                    if rel.get("status") == "inProgress":
                        rel["status"] = "completed"
                        rel.pop("userFraction", None)
                        changed = True
                if changed:
                    service.edits().tracks().update(
                        packageName=package_name, editId=edit_id,
                        track=track["track"],
                        body={"track": track["track"], "releases": rels},
                    ).execute()
                    updated = True
            if not updated:
                raise StoreError("没有处于分阶段发布（inProgress）的版本，无需全量发布")
            service.edits().commit(
                packageName=package_name, editId=edit_id
            ).execute()
            return {"message": "已全量发布（userFraction → 100%）"}
        except StoreError:
            raise
        except Exception as e:
            try:
                service.edits().delete(
                    packageName=package_name, editId=edit_id
                ).execute()
            except Exception:
                pass
            raise StoreError(f"Google 全量发布失败: {self._explain_error(e)}")

    # ---- 签名 APK 下载（generatedapks）----

    def list_bundle_versions(self, package_name: str) -> List[Dict[str, Any]]:
        """列出该应用已上传的全部 App Bundle 版本（供看板选择下载签名 APK）。

        临时 edit 内 bundles.list + tracks.list 合并出每个 versionCode 的轨道/发布状态，
        完成后立即删除 edit（不留痕）。Google API 不提供上传时间与安装人数，
        前端对应列显示占位符；版本名称解析自轨道 release name
        （本看板发布时写入 "versionCode (versionName)"，Google 自动生成时通常也含版本信息）。
        """
        service = self._service(package_name)
        try:
            edit = service.edits().insert(body={}, packageName=package_name).execute()
        except Exception as e:
            raise StoreError(f"Google Play 拉取版本失败（{self._explain_error(e)}）: {e}")
        edit_id = edit["id"]
        try:
            try:
                bundles = (
                    service.edits()
                    .bundles()
                    .list(packageName=package_name, editId=edit_id)
                    .execute()
                )
                tracks = (
                    service.edits()
                    .tracks()
                    .list(packageName=package_name, editId=edit_id)
                    .execute()
                )
            except Exception as e:
                raise StoreError(f"Google Play 拉取版本列表失败: {e}")
        finally:
            try:
                service.edits().delete(packageName=package_name, editId=edit_id).execute()
            except Exception:
                pass

        # versionCode → 所在轨道的发布信息（同一版本可能出现在多个轨道）
        rel_map: Dict[int, List[Dict[str, Any]]] = {}
        for track in tracks.get("tracks", []):
            track_name = track.get("track", "?")
            for rel in track.get("releases", []):
                for c in rel.get("versionCodes", []):
                    try:
                        vc = int(c)
                    except (TypeError, ValueError):
                        continue
                    rel_map.setdefault(vc, []).append(
                        {
                            "track": track_name,
                            "name": rel.get("name") or "",
                            "status": rel.get("status") or "",
                        }
                    )
        status_map = {
            "completed": "已发布",
            "inProgress": "发布中",
            "draft": "草稿",
            "halted": "已暂停",
        }
        out: List[Dict[str, Any]] = []
        for b in bundles.get("bundles", []):
            try:
                vc = int(b.get("versionCode"))
            except (TypeError, ValueError):
                continue
            rels = rel_map.get(vc) or []
            if rels:
                status_txt = "；".join(
                    "{}·{}".format(r["track"], status_map.get(r["status"], r["status"] or "未知"))
                    for r in rels
                )
                rel_name = next((r["name"] for r in rels if r.get("name")), "")
            else:
                status_txt = "未加入任何发布"
                rel_name = ""
            # 版本名称：优先从 "versionCode (versionName)" 形态解析
            version_name = ""
            if rel_name:
                m = re.match(r"^\d+\s+\((.+)\)$", rel_name)
                version_name = m.group(1) if m else rel_name
            out.append(
                {
                    "version_code": vc,
                    "version_name": version_name,
                    "release_status": status_txt,
                }
            )
        out.sort(key=lambda x: -x["version_code"])
        return out

    def _universal_apk_download_id(self, package_name: str, version_code: int) -> str:
        """查询该版本的 universal 整包 APK downloadId（generatedapks.list）。

        409 = 该版本的 APK 还在生成中（刚上传），稍后重试即可。
        """
        from googleapiclient.errors import HttpError

        service = self._service(package_name)
        try:
            resp = (
                service.generatedapks()
                .list(packageName=package_name, versionCode=version_code)
                .execute()
            )
        except HttpError as e:
            if getattr(e.resp, "status", None) == 409:
                raise StoreError("该版本的 APK 还在生成中（Google 需要少量时间），请稍后重试")
            if getattr(e.resp, "status", None) == 404:
                raise StoreError("Google 未找到该版本（确认 versionCode 为已上传的 App Bundle）")
            raise StoreError(f"Google 拉取签名 APK 信息失败: {e}")
        except Exception as e:
            raise StoreError(f"Google 拉取签名 APK 信息失败: {e}")
        for key in resp.get("generatedApks", []) or []:
            uni = key.get("generatedUniversalApk") or {}
            if uni.get("downloadId"):
                return uni["downloadId"]
        raise StoreError("该版本未生成 universal 整包 APK，无法下载（可重试或换版本）")

    def stream_signed_apk(self, package_name: str, version_code: int, part_path: str):
        """流式拉取签名 APK（生成器）：先 yield 总字节数，再逐块 yield 数据。

        总大小要等第一块数据到达才知道（yield 给调用方发 Content-Length 用，
        浏览器下载条才能显示百分比）；数据块同时写入 part_path。
        生成器自然结束 = 文件完整；中途异常/关闭由调用方负责清理 part 文件。
        """
        import googleapiclient.http

        download_id = self._universal_apk_download_id(package_name, version_code)
        service = self._service(package_name)
        req = service.generatedapks().download_media(
            packageName=package_name, versionCode=version_code, downloadId=download_id
        )
        with open(part_path, "wb") as fh:
            captured = []

            class _Capture:
                """MediaIoBaseDownload 的写入目标：数据落盘同时暂存，供生成器逐块 yield。"""

                def write(self, data):
                    fh.write(data)
                    captured.append(data)

            downloader = googleapiclient.http.MediaIoBaseDownload(
                _Capture(), req, chunksize=8 * 1024 * 1024
            )
            done = False
            first = True
            while not done:
                del captured[:]
                status, done = downloader.next_chunk()
                if first:
                    first = False
                    yield int(status.total_size or 0)
                if captured:
                    yield b"".join(captured)

    def download_signed_apk(self, package_name: str, version_code: int, dest_path: str) -> str:
        """下载指定 Bundle 版本的签名 APK（universal 整包）到 dest_path。

        generatedapks.list 拿 generatedUniversalApk 的 downloadId，
        再 generatedapks.download_media 流式写盘（8MB 分块）。
        下载的 APK 由 Play App Signing 的应用签名密钥签名，可直接覆盖安装做升级测试。
        """
        import googleapiclient.http
        from googleapiclient.errors import HttpError

        download_id = self._universal_apk_download_id(package_name, version_code)
        service = self._service(package_name)

        dest = Path(dest_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            req = service.generatedapks().download_media(
                packageName=package_name, versionCode=version_code, downloadId=download_id
            )
            with open(dest, "wb") as fh:
                downloader = googleapiclient.http.MediaIoBaseDownload(
                    fh, req, chunksize=8 * 1024 * 1024
                )
                done = False
                while not done:
                    _st, done = downloader.next_chunk()
        except HttpError as e:
            if dest.exists():
                try:
                    dest.unlink()
                except OSError:
                    pass
            if getattr(e.resp, "status", None) == 409:
                raise StoreError("该版本的 APK 还在生成中（Google 需要少量时间），请稍后重试")
            raise StoreError(f"下载签名 APK 失败: {e}")
        except Exception as e:
            if dest.exists():
                try:
                    dest.unlink()
                except OSError:
                    pass
            raise StoreError(f"下载签名 APK 失败: {e}")
        if not dest.is_file() or dest.stat().st_size < 1024:
            raise StoreError("下载结果异常（文件不存在或过小），请重试")
        return str(dest)
