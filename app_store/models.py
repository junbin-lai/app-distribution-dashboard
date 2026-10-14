"""统一数据模型：发行清单、提交结果、商店状态。

各商店字段千差万别，这里收敛成几个稳定的 dataclass，
适配器内部负责与各平台字段互相转换。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, List

import re


class Platform(str, Enum):
    HUAWEI = "huawei"
    OPPO = "oppo"
    VIVO = "vivo"
    XIAOMI = "xiaomi"
    HONOR = "honor"
    GOOGLE = "google"
    APPLE = "apple"
    QQ = "qq"

    @property
    def display_name(self) -> str:
        return {
            Platform.HUAWEI: "华为 AppGallery",
            Platform.OPPO: "OPPO 软件商店",
            Platform.VIVO: "vivo 应用商店",
            Platform.XIAOMI: "小米应用商店",
            Platform.HONOR: "荣耀应用市场",
            Platform.GOOGLE: "Google Play",
            Platform.APPLE: "App Store",
            Platform.QQ: "腾讯应用宝",
        }[self]


class AuditState(str, Enum):
    """各商店的审核/上架状态统一映射。"""

    DRAFT = "draft"  # 草稿/未提交
    SUBMITTED = "submitted"  # 已提交
    REVIEWING = "reviewing"  # 审核中
    PENDING = "pending"  # 审核通过，待发布/定时发布
    REJECTED = "rejected"  # 被驳回
    PUBLISHED = "published"  # 已上架
    UNKNOWN = "unknown"  # 未知/未接入

    @property
    def display_name(self) -> str:
        return {
            AuditState.DRAFT: "草稿",
            AuditState.SUBMITTED: "已提交",
            AuditState.REVIEWING: "审核中",
            AuditState.PENDING: "待发布",
            AuditState.REJECTED: "被驳回",
            AuditState.PUBLISHED: "已上架",
            AuditState.UNKNOWN: "未知",
        }[self]


@dataclass
class Release:
    """一次待发布的发行描述（不同商店取各自需要的字段）。"""

    package_name: str
    version_name: str
    version_code: int
    apk_path: str = ""
    aab_path: str = ""
    release_notes: str = ""
    track: str = "production"  # Google Play: production/beta/alpha/internal
    title: str = ""
    description: str = ""
    whatsnew: str = ""  # 各商店的"更新说明"，优先于 release_notes
    metadata: dict = field(default_factory=dict)

    def artifact_path(self) -> str:
        return self.aab_path or self.apk_path


@dataclass
class SubmitResult:
    platform: Platform
    ok: bool
    message: str = ""
    remote_reference: str = ""  # 商店返回的任务/编辑 id
    state: AuditState = AuditState.UNKNOWN
    raw: Any = None

    @property
    def summary(self) -> str:
        mark = "✔" if self.ok else "✘"
        text = f"[{self.platform.display_name}] {mark} {self.message}"
        if self.remote_reference:
            text += f" (ref={self.remote_reference})"
        return text


@dataclass
class StoreStatus:
    platform: Platform
    package_name: str
    state: AuditState = AuditState.UNKNOWN
    live_version_codes: List[int] = field(default_factory=list)
    live_version_names: List[str] = field(default_factory=list)
    draft_version_names: List[str] = field(default_factory=list)  # 草稿/未送审
    reviewing_version_names: List[str] = field(default_factory=list)  # 审核中/审核通过未发布版本
    beta_version_names: List[str] = field(default_factory=list)  # Google beta 轨道
    alpha_version_names: List[str] = field(default_factory=list)  # Google alpha 轨道
    internal_version_names: List[str] = field(default_factory=list)  # Google internal 轨道
    audit_note: str = ""  # 审核状态文字（标准化）：审核中 / 审核通过，待发布 / 审核通过，定时发布（时间）/ 审核未通过
    google_release_now: bool = False  # Google：production 轨道存在可「全量发布」的 inProgress 分阶段版本
    review_message: str = ""  # 平台附加信息（更新说明/releaseId 等，不在状态卡直接展示）
    checked_at: str = ""
    raw: Any = None

    @property
    def summary(self) -> str:
        versions = ", ".join(self.live_version_names) or ", ".join(str(c) for c in self.live_version_codes)
        base = f"[{self.platform.display_name}] {self.package_name} —— {self.state.display_name}"
        if versions:
            base += f" | 已上架版本: {versions}"
        if self.draft_version_names:
            base += f" | 草稿未送审: {', '.join(self.draft_version_names)}"
        if self.reviewing_version_names:
            base += f" | 审核中: {', '.join(self.reviewing_version_names)}"
        if self.review_message:
            base += f" | {self.review_message}"
        return base


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# 「2026-10-15 09:34:00」/「2026-10-15T09:34」/「2026-10-15T09:34:00+0800」
_SCHEDULE_RE = re.compile(r"^\d{4}-(\d{1,2})-(\d{1,2})[T ](\d{1,2}):(\d{2})")


def short_schedule(value) -> str:
    """定时时间缩写：'2026-10-15 09:34:00' / '2026-10-15T09:34' / 时间戳 → '10-15 09:34'。

    用于「待发布（定时 10-15 09:00）」样式的时间部分；
    时间戳按东八区换算，无法识别的格式原样返回。
    """
    if value is None:
        return ""
    s = str(value).strip()
    if not s:
        return ""
    m = _SCHEDULE_RE.match(s)
    if m:
        return f"{int(m[1]):02d}-{int(m[2]):02d} {int(m[3]):02d}:{m[4]}"
    if s.isdigit():  # 毫秒/秒级时间戳 → 东八区
        try:
            ts = int(s)
            if ts > 10 ** 12:
                ts /= 1000
            dt = datetime.fromtimestamp(ts, tz=timezone(timedelta(hours=8)))
            return dt.strftime("%m-%d %H:%M")
        except (ValueError, OSError, OverflowError):
            pass
    return s
