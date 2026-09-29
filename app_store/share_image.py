"""服务端渲染「发布状态分享图」（Pillow），与前端 drawShareCard 同款设计。

前端用 canvas 画（复制到剪贴板分享），飞书机器人需要在服务端出同一张图：
640 宽白卡片、圆角 16、2x 超采样后缩到 1x 保证清晰、应用图标圆裁剪、
平台行 = 名称 + 状态徽章 + 已上架/审核中/草稿明细行。

坐标/字号/颜色与前端 index.html 的 drawShareCard() 保持一致；
文字一律用基线锚点（anchor="ls"）对齐 canvas fillText 的默认基线语义。

字体优先 macOS 自带 Hiragino Sans GB（常规=ttc 索引 0，粗体=索引 2），
探测不到时逐级回退，最后退到 PIL 默认位图字体（仅保底）。
"""
from __future__ import annotations

import io
import os
import re
from typing import Any, Dict, List, Optional, Tuple

try:
    from PIL import Image, ImageDraw, ImageFont
    _PIL_OK = True
except ImportError:  # Pillow 未安装时分享图功能不可用，但不影响看板其他功能
    _PIL_OK = False

# 与前端 drawShareCard 一致的设计常量
W = 640
PX = 34
HEAD = 104
FOOT = 44
LINE_H = 19
SCALE = 2  # 2x 超采样

# 状态徽章: 文案 / 底色 / 字色（与前端 bd 映射一致）
BADGES = {
    "published": ("已上架", "#e8f6ec", "#157034"),
    "reviewing": ("审核中", "#fdf0e7", "#c93400"),
    "pending": ("待发布", "#e8f1fd", "#0066cc"),
    "draft": ("草稿", "#f0f4f8", "#43688c"),
    "submitted": ("已提交", "#f0f4f8", "#43688c"),
    "rejected": ("已拒绝", "#fdeceb", "#d70015"),
    "killed": ("已停止", "#f0f4f8", "#43688c"),
    "unknown": ("未知", "#f2f2f7", "#86868b"),
}

PLATFORM_NAMES = {
    "huawei": "华为", "oppo": "OPPO", "vivo": "VIVO", "xiaomi": "小米",
    "honor": "荣耀", "google": "Google Play", "apple": "Apple", "qq": "应用宝",
}

STATE_LABELS = {k: v[0] for k, v in BADGES.items()}

# 版本号样式（6.19.2 / 2.1.0），用于判断审核备注是否只是重复上面已列的版本
_VERSION_RE = re.compile(r"\d+(?:\.\d+)+")

# 中文字体探测链（Hiragino 索引来自实测：0=常规 2=粗体）
_FONT_CANDIDATES = [
    ("/System/Library/Fonts/PingFang.ttc", 0),        # macOS 新系统（若存在）
    ("/System/Library/Fonts/Hiragino Sans GB.ttc", 0),
    ("/System/Library/Fonts/STHeiti Light.ttc", 0),
    ("/System/Library/Fonts/Supplemental/Songti.ttc", 0),
    ("/System/Library/Fonts/Supplemental/Arial Unicode.ttf", 0),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 0),  # Linux 兜底
]
_BOLD_CANDIDATES = [
    ("/System/Library/Fonts/PingFang.ttc", 1),
    ("/System/Library/Fonts/Hiragino Sans GB.ttc", 2),
    ("/System/Library/Fonts/STHeiti Medium.ttc", 0),
    ("/System/Library/Fonts/Supplemental/Songti.ttc", 1),
    ("/System/Library/Fonts/Supplemental/Arial Unicode.ttf", 0),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 0),
]

_font_cache: Dict[Tuple[int, bool], Any] = {}


def _load_font(size: int, bold: bool) -> Any:
    """按字号/字重加载字体（带缓存）；无可用 TrueType 时退位图默认字体。"""
    if not _PIL_OK:
        return None
    key = (size, bold)
    if key in _font_cache:
        return _font_cache[key]
    font = None
    for path, idx in (_BOLD_CANDIDATES if bold else _FONT_CANDIDATES):
        try:
            font = ImageFont.truetype(path, size, index=idx)
            break
        except OSError:
            continue
    if font is None:
        try:
            font = ImageFont.load_default()
        except Exception:
            font = None
    _font_cache[key] = font
    return font


def _hex(color: str) -> Tuple[int, int, int]:
    c = color.lstrip("#")
    return (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16))


def _fit_text(draw: Any, text: str, font: Any, max_w: float) -> str:
    """超宽截断加省略号（与前端 fit() 行为一致）；max_w 为 1x 坐标。"""
    s = str(text)
    try:
        if draw.textlength(s, font=font) <= max_w * SCALE:
            return s
        while len(s) > 1 and draw.textlength(s + "…", font=font) > max_w * SCALE:
            s = s[:-1]
        return s + "…"
    except Exception:
        return s


def _build_rows(statuses: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """把 /api/status 的 statuses 转成渲染行（与前端 rows 构造一致）。"""
    rows = []
    for x in statuses or []:
        names = x.get("live_version_names") or []
        live = ", ".join(names or [str(c) for c in (x.get("live_version_codes") or [])])
        rev = ", ".join(x.get("reviewing_version_names") or [])
        draft = ", ".join(x.get("draft_version_names") or [])
        note = str(x.get("audit_note") or "").strip()
        state = str(x.get("state") or "unknown")
        lines = []
        if live:
            lines.append(("已上架", live))
        if rev:
            lines.append(("审核中", rev))
        if draft:
            lines.append(("草稿", draft))
        if note and note != STATE_LABELS.get(state):
            # 备注里提到的版本号若已在上面几行列过（如华为备注「6.19.2 审核中（Android）」
            # 与「审核中 6.19.2（Android）」重复），就不再重复显示；
            # 而「production:审核通过，待上架」这类不带版本号的备注属于额外信息，保留
            note_versions = set(_VERSION_RE.findall(note))
            shown = " ".join(v for _, v in lines)
            if not note_versions or not all(v in shown for v in note_versions):
                lines.append(("", note))
        if not lines:
            lines.append(("", "—"))
        rows.append({
            "name": PLATFORM_NAMES.get(x.get("platform"), str(x.get("platform"))),
            "badge": BADGES.get(state, BADGES["unknown"]),
            "lines": lines,
        })
    return rows


def _format_beijing_time(checked_at: str) -> str:
    """把平台返回的 UTC ISO 时间（2026-09-23T06:39:09+00:00）转成北京时间「YYYY-MM-DD HH:MM」。

    与前端 canvas 分享图行为一致（浏览器 new Date() 解析后取本地时间）；
    解析失败或为空时回退到当前北京时间。
    """
    from datetime import datetime, timedelta, timezone
    tz_bj = timezone(timedelta(hours=8))
    ts = str(checked_at or "").strip()
    if ts:
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)  # 无时区标记按 UTC 处理
            return dt.astimezone(tz_bj).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            pass
    return datetime.now(tz_bj).strftime("%Y-%m-%d %H:%M")


def render_status_image(
    app_name: str,
    statuses: List[Dict[str, Any]],
    icon_bytes: Optional[bytes] = None,
    checked_at: str = "",
) -> Optional[bytes]:
    """渲染 PNG（字节）。Pillow 不可用或无任何状态行时返回 None。

    checked_at 传 /api/status 里任一平台的 checked_at（前端取第一条非空的）。
    """
    if not _PIL_OK:
        return None
    rows = _build_rows(statuses)
    if not rows:
        return None

    ts = _format_beijing_time(checked_at)

    # 字号按 2x 加载（画布是 W*SCALE 宽，字体也要等比放大，否则文字相对布局只有一半大）
    f_title = _load_font(23 * SCALE, True)
    f_sub = _load_font(13 * SCALE, False)
    f_row = _load_font(16 * SCALE, True)
    f_badge = _load_font(12 * SCALE, True)
    f_line = _load_font(13 * SCALE, False)
    f_foot = _load_font(11 * SCALE, False)

    row_heights = [42 + len(r["lines"]) * LINE_H + 8 for r in rows]
    height = HEAD + sum(row_heights) + FOOT

    img = Image.new("RGB", (W * SCALE, height * SCALE), _hex("#f5f5f7"))
    draw = ImageDraw.Draw(img)

    def rr(x, y, w, h, r, fill=None, outline=None, width=1):
        draw.rounded_rectangle(
            [x * SCALE, y * SCALE, (x + w) * SCALE, (y + h) * SCALE],
            radius=r * SCALE, fill=fill, outline=outline, width=width * SCALE)

    # 白色圆角卡片 + 细描边（rgba(0,0,0,.06) ≈ #f0f0f0）
    rr(10, 10, W - 20, height - 20, 16, fill=(255, 255, 255),
       outline=(240, 240, 240), width=1)

    def text(x, y, s, font, fill):
        """基线锚点绘制（canvas fillText 默认基线语义，y 即基线）。"""
        draw.text((x * SCALE, y * SCALE), str(s), font=font, fill=fill, anchor="ls")

    # 头部：图标（圆裁剪）+ 标题 + 副标题 + 蓝色小条
    icon_img = None
    if icon_bytes:
        try:
            icon_img = Image.open(io.BytesIO(icon_bytes)).convert("RGBA")
        except Exception:
            icon_img = None
    tx = PX
    if icon_img is not None:
        r = 26
        cx, cy = PX + r, 50
        s = max(2 * r / icon_img.width, 2 * r / icon_img.height)
        dw, dh = icon_img.width * s, icon_img.height * s
        icon_img = icon_img.resize(
            (max(1, int(dw * SCALE)), max(1, int(dh * SCALE))), Image.LANCZOS)
        mask = Image.new("L", icon_img.size, 0)
        ImageDraw.Draw(mask).ellipse(
            [0, 0, icon_img.size[0] - 1, icon_img.size[1] - 1], fill=255)
        img.paste(icon_img, (int((cx - dw / 2) * SCALE), int((cy - dh / 2) * SCALE)), mask)
        draw.ellipse(
            [(cx - r) * SCALE, (cy - r) * SCALE, (cx + r) * SCALE, (cy + r) * SCALE],
            outline=(230, 230, 230), width=SCALE)  # rgba(0,0,0,.1)≈#e6e6e6
        tx = PX + 66

    title = _fit_text(draw, app_name, f_title, W - tx - PX)
    text(tx, 44 if icon_img is not None else 56, title, f_title, _hex("#1d1d1f"))
    text(tx, 68 if icon_img is not None else 80,
         f"App 发布状态 · 查询于 {ts}", f_sub, _hex("#86868b"))
    rr(tx, 82 if icon_img is not None else 92, 34, 3, 1.5, fill=_hex("#0071e3"))

    # 平台行
    y = HEAD
    for idx, row in enumerate(rows):
        name = _fit_text(draw, row["name"], f_row, 220)
        text(PX, y + 24, name, f_row, _hex("#1d1d1f"))
        # 状态徽章（右侧圆角胶囊）
        label, bg, fg = row["badge"]
        tw = draw.textlength(label, font=f_badge) / SCALE
        bx, by, bh = W - PX - tw - 16, y + 9, 21
        rr(bx, by, tw + 16, bh, 10.5, fill=_hex(bg))
        text(bx + 8, by + 15, label, f_badge, _hex(fg))
        # 明细行
        ly = y + 44
        for lab, val in row["lines"]:
            lx = PX
            if lab:
                text(PX, ly, lab, f_line, _hex("#86868b"))
                lx = PX + draw.textlength(lab, font=f_line) / SCALE + 12
            val = _fit_text(draw, val, f_line, W - PX - lx)
            text(lx, ly, val, f_line, _hex("#3a3a3c"))
            ly += LINE_H
        if idx < len(rows) - 1:
            draw.line([(PX * SCALE, (y + row_heights[idx] - 5) * SCALE),
                       ((W - PX) * SCALE, (y + row_heights[idx] - 5) * SCALE)],
                      fill=(235, 235, 235), width=SCALE)  # rgba(0,0,0,.08)≈#ebebeb
        y += row_heights[idx]

    # 页脚
    foot = "— 由 App 发布看板生成 —"
    fw = draw.textlength(foot, font=f_foot) / SCALE
    text((W - fw) / 2, height - 18, foot, f_foot, _hex("#86868b"))

    # 与前端 canvas 一致：直接输出 2x 分辨率（W*SCALE 宽，如 1280），
    # 不缩回 1x——前端分享图就是 2x 输出的，聊天窗口/飞书里看才是清晰的
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
