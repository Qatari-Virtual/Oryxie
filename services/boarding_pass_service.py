"""Luxury 1920x750 Pillow PNG Reward Ticket generator for QRV Christmas Operations 2026."""

from __future__ import annotations

import asyncio
import functools
import hashlib
import io
import json
import logging
import math
import os
import random
import threading
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional, Tuple

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

log = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = os.path.join("assets", "christmas_2026_config.json")

# ── Canvas & Layout Constants ─────────────────────────────────────────────────
W, H = 1920, 750
STUB_X = 1400
CORNER_R = 28
MAIN_LX = 125
MAIN_RX = 1345
LOGO_PATH = os.path.join("assets", "Qatar_virtual_logo_white.PNG")

# ── Palette ───────────────────────────────────────────────────────────────────
GOLD_LABEL = (224, 178, 72)
GOLD_BRIGHT = (250, 218, 120)
GOLD_VALUE = (238, 200, 108)
WHITE = (255, 255, 255)

# ── Font Loading & Caching ────────────────────────────────────────────────────
_FONT_DIRS = ("C:/Windows/Fonts", "/usr/share/fonts/truetype/dejavu")
_BASE_LOCK = threading.Lock()


@functools.lru_cache(maxsize=32)
def _font(name: str, size: int) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    """Load TrueType font by filename with cross-platform fallbacks."""
    candidates = [name, "segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf"]
    for directory in _FONT_DIRS:
        for candidate in candidates:
            path = os.path.join(directory, candidate)
            if os.path.exists(path):
                try:
                    return ImageFont.truetype(path, size=size)
                except OSError:
                    continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _fit_font(
    draw: ImageDraw.ImageDraw,
    text: str,
    font_name: str,
    max_size: int,
    min_size: int,
    max_width: int,
) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    """Shrink font size gracefully until text fits within max_width."""
    for sz in range(max_size, min_size - 1, -2):
        f = _font(font_name, sz)
        bb = draw.textbbox((0, 0), text, font=f)
        if (bb[2] - bb[0]) <= max_width:
            return f
    return _font(font_name, min_size)


# ═════════════════════════════════════════════════════════════════════════════
#  VECTORIZED ATMOSPHERE & BACKGROUND
# ═════════════════════════════════════════════════════════════════════════════


def _build_card_mask(w: int, h: int, r: int) -> Image.Image:
    """Create a 2x supersampled anti-aliased rounded rectangle alpha mask."""
    mask_2x = Image.new("L", (w * 2, h * 2), 0)
    md = ImageDraw.Draw(mask_2x)
    md.rounded_rectangle([0, 0, w * 2 - 1, h * 2 - 1], radius=r * 2, fill=255)
    return mask_2x.resize((w, h), Image.LANCZOS)


def _build_velvet_background(w: int, h: int) -> Image.Image:
    """Generate deep crimson-burgundy velvet radial background + golden ambient glows."""
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)

    cx, cy = w * 0.36, h * 0.48
    dist = np.sqrt(((xx - cx) / (w * 0.56)) ** 2 + ((yy - cy) / (h * 0.68)) ** 2)
    t = np.clip(dist, 0.0, 1.35) / 1.35

    r_ch = 112.0 * (1.0 - t ** 1.35) + 42.0 * (t ** 1.35)
    g_ch = 13.0 * (1.0 - t ** 1.25) + 3.0 * (t ** 1.25)
    b_ch = 44.0 * (1.0 - t ** 1.25) + 17.0 * (t ** 1.25)

    stub_shade = np.clip((xx - STUB_X) / 520.0, 0.0, 1.0)
    r_ch -= stub_shade * 12.0
    b_ch -= stub_shade * 4.0

    # Warm golden ambient shimmer along header gold rule
    hdr_glow = np.exp(-(((xx - 860.0) / 360.0) ** 2 + ((yy - 132.0) / 88.0) ** 2))
    r_ch += hdr_glow * 56.0
    g_ch += hdr_glow * 34.0
    b_ch += hdr_glow * 8.0

    # Warm golden bokeh glow behind tree & bottom-right of stub
    stub_glow = np.exp(-(((xx - 1745.0) / 210.0) ** 2 + ((yy - 635.0) / 150.0) ** 2))
    tree_glow = np.exp(-(((xx - 1742.0) / 150.0) ** 2 + ((yy - 415.0) / 120.0) ** 2))
    warm = stub_glow * 0.85 + tree_glow * 0.45
    r_ch += warm * 78.0
    g_ch += warm * 48.0
    b_ch += warm * 10.0

    # Faint illumination behind bottom-left ornaments
    bl_glow = np.exp(-(((xx - 72.0) / 140.0) ** 2 + ((yy - 670.0) / 120.0) ** 2))
    r_ch += bl_glow * 24.0
    g_ch += bl_glow * 12.0

    rgb = np.stack(
        [
            np.clip(r_ch, 0, 255),
            np.clip(g_ch, 0, 255),
            np.clip(b_ch, 0, 255),
        ],
        axis=-1,
    ).astype(np.uint8)
    alpha = np.full((h, w, 1), 255, dtype=np.uint8)
    return Image.fromarray(np.concatenate([rgb, alpha], axis=-1), mode="RGBA")


# ═════════════════════════════════════════════════════════════════════════════
#  FESTIVE VECTOR & 3D ARTWORK HELPERS
# ═════════════════════════════════════════════════════════════════════════════


def _draw_snowflake(
    draw: ImageDraw.ImageDraw,
    cx: float,
    cy: float,
    radius: float,
    color: Tuple[int, int, int, int],
    rotation_deg: float = 0.0,
    width: int = 2,
):
    """Draw a 6-fold branched crystalline snowflake."""
    base_rad = math.radians(rotation_deg)
    for k in range(6):
        ang = base_rad + k * (math.pi / 3.0)
        ca, sa = math.cos(ang), math.sin(ang)
        draw.line([(cx, cy), (cx + radius * ca, cy + radius * sa)], fill=color, width=width)

        for frac, barb_scale in ((0.44, 0.28), (0.72, 0.20)):
            bx, by = cx + radius * frac * ca, cy + radius * frac * sa
            blen = radius * barb_scale
            for side in (-1, 1):
                b_ang = ang + side * math.radians(52)
                draw.line(
                    [(bx, by), (bx + blen * math.cos(b_ang), by + blen * math.sin(b_ang))],
                    fill=color,
                    width=max(1, width - 1),
                )

    hr = radius * 0.16
    if hr >= 3:
        hex_pts = [
            (
                cx + hr * math.cos(base_rad + i * math.pi / 3.0),
                cy + hr * math.sin(base_rad + i * math.pi / 3.0),
            )
            for i in range(6)
        ]
        draw.polygon(hex_pts, outline=color)


def _draw_sparkle_flare(
    glow_layer: ImageDraw.ImageDraw,
    sharp_layer: ImageDraw.ImageDraw,
    cx: float,
    cy: float,
    size: float,
    color: Tuple[int, int, int] = (255, 224, 128),
    alpha: int = 235,
):
    """Draw a warm 4-point lens flare sparkle + radial halo."""
    hr = max(4, int(size * 0.85))
    glow_layer.ellipse(
        [cx - hr, cy - hr, cx + hr, cy + hr],
        fill=(color[0], color[1], color[2], int(alpha * 0.42)),
    )

    outer = size
    inner = max(1.2, size * 0.14)
    pts = []
    for i in range(8):
        ang = i * (math.pi / 4.0)
        r = outer if i % 2 == 0 else inner
        pts.append((cx + r * math.cos(ang), cy + r * math.sin(ang)))
    sharp_layer.polygon(pts, fill=(255, 245, 200, alpha))

    if size >= 11:
        diag = size * 0.36
        for ang_deg in (45, 135):
            a = math.radians(ang_deg)
            dx, dy = diag * math.cos(a), diag * math.sin(a)
            sharp_layer.line(
                [(cx - dx, cy - dy), (cx + dx, cy + dy)],
                fill=(color[0], color[1], color[2], int(alpha * 0.65)),
                width=1,
            )


def _draw_pine_sprig(
    draw: ImageDraw.ImageDraw,
    pts: Tuple[Tuple[float, float], Tuple[float, float], Tuple[float, float]],
    needle_len: float,
    density: int,
    seed: int,
    scale: float = 1.0,
):
    """Draw a layered spruce/pine twig along a quadratic Bezier spline."""
    rng = random.Random(seed)
    p0, p1, p2 = pts

    def _bez(t: float) -> Tuple[float, float]:
        mt = 1.0 - t
        return (
            mt * mt * p0[0] + 2 * mt * t * p1[0] + t * t * p2[0],
            mt * mt * p0[1] + 2 * mt * t * p1[1] + t * t * p2[1],
        )

    def _tan(t: float) -> Tuple[float, float]:
        dx = 2 * (1 - t) * (p1[0] - p0[0]) + 2 * t * (p2[0] - p1[0])
        dy = 2 * (1 - t) * (p1[1] - p0[1]) + 2 * t * (p2[1] - p1[1])
        mag = math.hypot(dx, dy) or 1.0
        return dx / mag, dy / mag

    layers = [
        ((14, 46, 20, 235), int(3 * scale), 1.06, 0.30),
        ((26, 86, 36, 235), int(2 * scale), 0.96, 0.42),
        ((56, 132, 58, 200), max(1, int(1.5 * scale)), 0.78, 0.50),
    ]

    for col, n_width, len_mult, fwd_bias in layers:
        for i in range(density):
            t = max(0.0, min(1.0, (i + rng.uniform(-0.25, 0.25)) / max(1, density - 1)))
            bx, by = _bez(t)
            tx, ty = _tan(t)
            px, py = -ty, tx

            taper = 0.55 + 0.45 * math.sin(math.pi * (0.15 + 0.80 * (1.0 - t)))
            for side in (-1, 1):
                jitter = rng.uniform(0.78, 1.18)
                nl = needle_len * len_mult * taper * jitter
                fwd = fwd_bias + rng.uniform(-0.12, 0.14)
                nx = side * px * 0.86 + tx * fwd
                ny = side * py * 0.86 + ty * fwd
                nmag = math.hypot(nx, ny) or 1.0
                ex = bx + (nx / nmag) * nl
                ey = by + (ny / nmag) * nl
                draw.line([(bx, by), (ex, ey)], fill=col, width=max(1, n_width))

    stem_pts = [_bez(i / 18.0) for i in range(19)]
    draw.line(stem_pts, fill=(46, 32, 20, 240), width=max(2, int(3 * scale)))


def _draw_holly_cluster(
    canvas: Image.Image,
    cx: float,
    cy: float,
    scale: float = 1.0,
    angles: Iterable[float] = (-140.0, -25.0, 75.0),
):
    """Render jagged shaded holly leaves + glossy 3D crimson berries."""
    size = int(170 * scale)
    patch = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    pc = size / 2.0
    pd = ImageDraw.Draw(patch, "RGBA")

    def _leaf_poly(length: float, width: float, ang_rad: float):
        raw_upper = [
            (0.00, 0.00),
            (0.20, -0.34),
            (0.32, -0.22),
            (0.50, -0.44),
            (0.64, -0.26),
            (0.82, -0.35),
            (1.00, 0.00),
        ]
        raw_lower = [(ux, -uy) for (ux, uy) in reversed(raw_upper[1:-1])]
        ca, sa = math.cos(ang_rad), math.sin(ang_rad)
        out = []
        for rx, ry in raw_upper + raw_lower:
            px, py = rx * length, ry * width
            out.append((pc + px * ca - py * sa, pc + px * sa + py * ca))
        return out

    shadow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow, "RGBA")
    for idx, deg in enumerate(angles):
        rad = math.radians(deg)
        slen = (44 + (idx % 2) * 8) * scale
        swid = (24 + (idx % 2) * 4) * scale
        sd.polygon(_leaf_poly(slen, swid, rad), fill=(8, 2, 5, 135))
    shadow = shadow.filter(ImageFilter.GaussianBlur(radius=3.5 * scale))
    canvas.alpha_composite(shadow, (int(cx - pc + 2), int(cy - pc + 4)))

    for idx, deg in enumerate(angles):
        rad = math.radians(deg)
        slen = (44 + (idx % 2) * 8) * scale
        swid = (24 + (idx % 2) * 4) * scale
        poly = _leaf_poly(slen, swid, rad)
        pd.polygon(poly, fill=(22, 92, 36, 255), outline=(14, 62, 24, 255))
        pd.polygon(poly[: len(poly) // 2 + 1], fill=(44, 128, 52, 165))
        ca, sa = math.cos(rad), math.sin(rad)
        pd.line(
            [(pc + 3 * ca, pc + 3 * sa), (pc + slen * 0.88 * ca, pc + slen * 0.88 * sa)],
            fill=(112, 168, 86, 175),
            width=max(1, int(1.6 * scale)),
        )

    berry_offsets = [(-6, -5), (6, -3), (0, 6)]
    br = max(5, int(7.5 * scale))
    for bx_o, by_o in berry_offsets:
        bx = pc + bx_o * scale
        by = pc + by_o * scale
        pd.ellipse([bx - br + 1, by - br + 2, bx + br + 1, by + br + 2], fill=(25, 2, 6, 140))
        pd.ellipse([bx - br, by - br, bx + br, by + br], fill=(165, 12, 20, 255))
        pd.ellipse(
            [bx - br * 0.82, by - br * 0.85, bx + br * 0.65, by + br * 0.65],
            fill=(224, 34, 42, 255),
        )
        sr = max(2, br * 0.34)
        pd.ellipse(
            [bx - br * 0.48, by - br * 0.52, bx - br * 0.48 + sr, by - br * 0.52 + sr],
            fill=(255, 215, 215, 230),
        )

    canvas.alpha_composite(patch, (int(cx - pc), int(cy - pc)))


def _draw_bauble_3d(
    canvas: Image.Image,
    cx: int,
    cy: int,
    radius: int,
    kind: str = "red",
    string_top_y: int | None = None,
):
    """Render a 3D spherical-shaded glossy Christmas bauble with gold cap and string."""
    if string_top_y is not None and string_top_y < cy - radius - 8:
        sd = ImageDraw.Draw(canvas, "RGBA")
        sd.line([(cx, string_top_y), (cx, cy - radius - 8)], fill=(228, 192, 92, 215), width=2)

    pad = 16
    sz = radius * 2 + pad * 2
    yy, xx = np.mgrid[0:sz, 0:sz].astype(np.float32)
    c = sz / 2.0
    dx = (xx - c) / float(radius)
    dy = (yy - c) / float(radius)
    r2 = dx * dx + dy * dy
    inside = r2 <= 1.0
    nz = np.sqrt(np.clip(1.0 - r2, 0.0, 1.0))

    lx, ly, lz = -0.38, -0.46, 0.80
    diff = np.clip(dx * lx + dy * ly + nz * lz, 0.0, 1.0)
    hx, hy, hz = -0.22, -0.26, 0.94
    spec = np.clip(dx * hx + dy * hy + nz * hz, 0.0, 1.0) ** 34
    rim = np.clip(1.0 - nz, 0.0, 1.0) ** 2.6 * np.clip(dx * 0.5 + dy * 0.6 + 0.3, 0.0, 1.0)

    if kind == "gold":
        base = np.array([150.0, 102.0, 26.0])
        lit = np.array([250.0, 214.0, 112.0])
        rim_col = np.array([255.0, 224.0, 130.0])
        hi_col = np.array([255.0, 252.0, 232.0])
    else:
        base = np.array([82.0, 6.0, 14.0])
        lit = np.array([218.0, 28.0, 42.0])
        rim_col = np.array([210.0, 145.0, 65.0])
        hi_col = np.array([255.0, 238.0, 238.0])

    col = (
        base[None, None, :] * (1.0 - diff[:, :, None] * 0.85)
        + lit[None, None, :] * (diff[:, :, None] * 0.95)
        + rim_col[None, None, :] * (rim[:, :, None] * 0.38)
        + hi_col[None, None, :] * (spec[:, :, None] * 0.92)
    )

    dist_px = np.sqrt(r2) * radius
    edge_alpha = np.clip((radius + 0.7 - dist_px) / 1.4, 0.0, 1.0) * 255.0
    edge_alpha = np.where(inside | (edge_alpha > 0), edge_alpha, 0.0)

    rgba = np.zeros((sz, sz, 4), dtype=np.uint8)
    rgba[:, :, :3] = np.clip(col, 0, 255).astype(np.uint8)
    rgba[:, :, 3] = np.clip(edge_alpha, 0, 255).astype(np.uint8)
    sphere_img = Image.fromarray(rgba, mode="RGBA")

    sh = Image.new("RGBA", (sz, sz), (0, 0, 0, 0))
    sh_d = ImageDraw.Draw(sh, "RGBA")
    sh_d.ellipse(
        [pad + 4, pad + 7, pad + radius * 2 + 4, pad + radius * 2 + 7],
        fill=(12, 2, 6, 155),
    )
    sh = sh.filter(ImageFilter.GaussianBlur(radius=5.5))
    canvas.alpha_composite(sh, (int(cx - c), int(cy - c)))
    canvas.alpha_composite(sphere_img, (int(cx - c), int(cy - c)))

    cd = ImageDraw.Draw(canvas, "RGBA")
    cw = max(8, int(radius * 0.36))
    ch = max(7, int(radius * 0.25))
    cap_y0 = cy - radius - ch + 3
    cap_y1 = cy - radius + 4
    cd.rounded_rectangle(
        [cx - cw, cap_y0, cx + cw, cap_y1],
        radius=3,
        fill=(212, 168, 62, 255),
        outline=(135, 96, 26, 255),
        width=1,
    )
    cd.rectangle(
        [cx - cw + 2, cap_y0 + 1, cx - cw // 4, cap_y1 - 1],
        fill=(252, 224, 130, 220),
    )
    cd.ellipse(
        [cx - 4, cap_y0 - 7, cx + 4, cap_y0 + 1],
        outline=(236, 198, 92, 245),
        width=2,
    )


def _draw_gold_tree(canvas: Image.Image, cx: int, top_y: int, height: int):
    """Draw an ornate glowing metallic-gold stylized Christmas tree on the stub."""
    w_box = int(height * 1.15)
    h_box = height + 40
    ox, oy = cx - w_box // 2, top_y - 20

    glow = Image.new("RGBA", (w_box, h_box), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow, "RGBA")
    sharp = Image.new("RGBA", (w_box, h_box), (0, 0, 0, 0))
    sd = ImageDraw.Draw(sharp, "RGBA")

    lx = w_box / 2.0
    ly_top = 28.0
    body_h = height * 0.84

    tiers = [
        (0.00, 0.35, 0.16),
        (0.24, 0.64, 0.28),
        (0.50, 0.94, 0.39),
    ]
    for y0_f, y1_f, half_w_f in tiers:
        y_a = ly_top + body_h * y0_f
        y_b = ly_top + body_h * y1_f
        hw = height * half_w_f
        pts = [(lx, y_a), (lx + hw, y_b), (lx - hw, y_b)]
        gd.polygon(pts, fill=(245, 196, 78, 55), outline=(255, 215, 105, 185))
        sd.polygon(pts, fill=(224, 174, 60, 32), outline=(248, 212, 112, 245), width=2)

    garlands = [
        ((lx - height * 0.09, ly_top + body_h * 0.22), (lx + height * 0.12, ly_top + body_h * 0.31)),
        ((lx - height * 0.21, ly_top + body_h * 0.50), (lx + height * 0.24, ly_top + body_h * 0.60)),
        ((lx - height * 0.31, ly_top + body_h * 0.78), (lx + height * 0.34, ly_top + body_h * 0.89)),
    ]
    for p_a, p_b in garlands:
        sd.line([p_a, p_b], fill=(255, 228, 145, 215), width=2)

    dots = [
        (lx - height * 0.08, ly_top + body_h * 0.26),
        (lx + height * 0.09, ly_top + body_h * 0.30),
        (lx - height * 0.16, ly_top + body_h * 0.53),
        (lx + height * 0.14, ly_top + body_h * 0.56),
        (lx - height * 0.02, ly_top + body_h * 0.44),
        (lx - height * 0.24, ly_top + body_h * 0.80),
        (lx + height * 0.20, ly_top + body_h * 0.84),
        (lx - height * 0.04, ly_top + body_h * 0.72),
    ]
    for dx, dy in dots:
        gd.ellipse([dx - 5, dy - 5, dx + 5, dy + 5], fill=(255, 224, 120, 175))
        sd.ellipse([dx - 2.5, dy - 2.5, dx + 2.5, dy + 2.5], fill=(255, 244, 195, 245))

    tw = max(4, int(height * 0.045))
    sd.rectangle(
        [lx - tw, ly_top + body_h * 0.94, lx + tw, ly_top + height * 0.94],
        fill=(214, 168, 64, 225),
    )

    star_r = height * 0.095
    star_ir = star_r * 0.42
    star_pts = []
    for k in range(10):
        ang = math.radians(-90 + k * 36)
        r = star_r if k % 2 == 0 else star_ir
        star_pts.append((lx + r * math.cos(ang), ly_top + r * math.sin(ang)))
    gd.ellipse(
        [lx - star_r * 1.4, ly_top - star_r * 1.4, lx + star_r * 1.4, ly_top + star_r * 1.4],
        fill=(255, 224, 120, 160),
    )
    sd.polygon(star_pts, fill=(255, 232, 145, 255))

    glow = glow.filter(ImageFilter.GaussianBlur(radius=6.0))
    canvas.alpha_composite(glow, (ox, oy))
    canvas.alpha_composite(sharp, (ox, oy))


def _draw_vertical_barcode(
    canvas: Image.Image,
    x: int,
    y: int,
    w: int,
    h: int,
    payload: str,
):
    """Draw a crisp white rounded-corner vertical barcode strip on the stub."""
    d = ImageDraw.Draw(canvas, "RGBA")
    d.rounded_rectangle([x, y, x + w, y + h], radius=12, fill=(252, 250, 250, 246))

    digest = hashlib.sha256(payload.encode("utf-8")).digest()
    rng = random.Random(int.from_bytes(digest[:8], "big"))

    qx, qy = 14, 18
    cy = y + qy
    y_limit = y + h - qy

    bars = [5, 3, 2, 4, 2, 6, 3, 2]
    while len(bars) < 95:
        bars.append(rng.choice([2, 3, 4, 6, 7, 2, 3, 5]))
    bars.extend([3, 2, 5, 2, 4, 6])

    is_black = True
    idx = 0
    while cy < y_limit:
        bh = min(bars[idx % len(bars)], y_limit - cy)
        if is_black and bh > 0:
            d.rectangle([x + qx, cy, x + w - qx, cy + bh], fill=(22, 16, 18, 255))
        cy += bh
        is_black = not is_black
        idx += 1


def _draw_qr_block(
    canvas: Image.Image,
    x: int,
    y: int,
    size: int,
    payload: str,
):
    """Draw a razor-sharp symmetric rounded-corner decorative QR badge."""
    d = ImageDraw.Draw(canvas, "RGBA")
    d.rounded_rectangle([x, y, x + size, y + size], radius=14, fill=(252, 250, 250, 248))

    modules = 21
    m = (size - 26) // modules
    ox = x + (size - modules * m) // 2
    oy = y + (size - modules * m) // 2

    dark = (22, 16, 18, 255)
    white = (252, 250, 250, 255)

    def _finder(fx: int, fy: int):
        d.rectangle([ox + fx * m, oy + fy * m, ox + (fx + 7) * m - 1, oy + (fy + 7) * m - 1], fill=dark)
        d.rectangle([ox + (fx + 1) * m, oy + (fy + 1) * m, ox + (fx + 6) * m - 1, oy + (fy + 6) * m - 1], fill=white)
        d.rectangle([ox + (fx + 2) * m, oy + (fy + 2) * m, ox + (fx + 5) * m - 1, oy + (fy + 5) * m - 1], fill=dark)

    _finder(0, 0)
    _finder(modules - 7, 0)
    _finder(0, modules - 7)

    ax, ay = modules - 7, modules - 7
    d.rectangle([ox + ax * m, oy + ay * m, ox + (ax + 5) * m - 1, oy + (ay + 5) * m - 1], fill=dark)
    d.rectangle([ox + (ax + 1) * m, oy + (ay + 1) * m, ox + (ax + 4) * m - 1, oy + (ay + 4) * m - 1], fill=white)
    d.rectangle([ox + (ax + 2) * m, oy + (ay + 2) * m, ox + (ax + 3) * m - 1, oy + (ay + 3) * m - 1], fill=dark)

    digest = hashlib.sha256(payload.encode("utf-8")).digest()
    rng = random.Random(int.from_bytes(digest[8:16], "big"))

    def _reserved(r: int, c: int) -> bool:
        if (r < 8 and c < 8) or (r < 8 and c >= modules - 8) or (r >= modules - 8 and c < 8):
            return True
        if ax <= r < ax + 5 and ay <= c < ay + 5:
            return True
        return r == 6 or c == 6

    for k in range(8, modules - 8):
        if k % 2 == 0:
            d.rectangle([ox + k * m, oy + 6 * m, ox + (k + 1) * m - 1, oy + 7 * m - 1], fill=dark)
            d.rectangle([ox + 6 * m, oy + k * m, ox + 7 * m - 1, oy + (k + 1) * m - 1], fill=dark)

    for r in range(modules):
        for c in range(modules):
            if not _reserved(r, c) and rng.random() > 0.46:
                d.rectangle([ox + c * m, oy + r * m, ox + (c + 1) * m - 1, oy + (r + 1) * m - 1], fill=dark)


def _draw_gold_gradient_text(
    canvas: Image.Image,
    x: int,
    y: int,
    text: str,
    font: ImageFont.ImageFont,
    top_rgb: Tuple[int, int, int] = (252, 224, 128),
    bot_rgb: Tuple[int, int, int] = (206, 152, 54),
    anchor: str | None = None,
) -> Tuple[int, int, int, int]:
    """Draw bold text with a smooth vertical metallic-gold gradient + soft drop shadow."""
    pad = 24
    probe = Image.new("RGBA", (10, 10), (0, 0, 0, 0))
    pd = ImageDraw.Draw(probe)
    bb0 = pd.textbbox((0, 0), text, font=font, anchor=anchor)
    tw = max(4, bb0[2] - bb0[0] + pad * 2)
    th = max(4, bb0[3] - bb0[1] + pad * 2)
    draw_x = pad - bb0[0]
    draw_y = pad - bb0[1]

    mask_img = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
    md = ImageDraw.Draw(mask_img, "RGBA")
    md.text((draw_x, draw_y), text, font=font, fill=(255, 255, 255, 255), anchor=anchor)
    bb = md.textbbox((draw_x, draw_y), text, font=font, anchor=anchor)

    sh = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
    sd = ImageDraw.Draw(sh, "RGBA")
    sd.text((draw_x + 2, draw_y + 3), text, font=font, fill=(20, 2, 8, 170), anchor=anchor)
    sh = sh.filter(ImageFilter.GaussianBlur(radius=2.5))
    paste_x, paste_y = x - draw_x, y - draw_y
    canvas.alpha_composite(sh, (paste_x, paste_y))

    y_top, y_bot = max(0, bb[1]), max(bb[1] + 1, bb[3])
    t_col = np.clip((np.arange(th, dtype=np.float32) - y_top) / float(y_bot - y_top), 0.0, 1.0)[:, None]
    grad = np.zeros((th, tw, 4), dtype=np.uint8)
    grad[:, :, 0] = (top_rgb[0] * (1.0 - t_col) + bot_rgb[0] * t_col).astype(np.uint8)
    grad[:, :, 1] = (top_rgb[1] * (1.0 - t_col) + bot_rgb[1] * t_col).astype(np.uint8)
    grad[:, :, 2] = (top_rgb[2] * (1.0 - t_col) + bot_rgb[2] * t_col).astype(np.uint8)
    grad[:, :, 3] = np.array(mask_img.split()[3], dtype=np.uint8)
    canvas.alpha_composite(Image.fromarray(grad, mode="RGBA"), (paste_x, paste_y))
    return (
        x + bb0[0],
        y + bb0[1],
        x + bb0[2],
        y + bb0[3],
    )


def _clean_rank_prefix(name: str, prefixes: Tuple[str, ...], default_prefix: str) -> str:
    """Avoid duplicating Capt. / CPT or FO. / F/O if caller passed one already."""
    stripped = (name or "").strip()
    upper = stripped.upper()
    for p in prefixes:
        if upper.startswith(p.upper()):
            return stripped
    return f"{default_prefix} {stripped}"


def _draw_mini_star(draw: ImageDraw.ImageDraw, cx: float, cy: float, outer_r: float, color: Tuple[int, int, int, int]):
    """Draw a crisp 5-point vector star without relying on font glyph availability."""
    inner_r = outer_r * 0.42
    pts = []
    for k in range(10):
        ang = math.radians(-90 + k * 36)
        r = outer_r if k % 2 == 0 else inner_r
        pts.append((cx + r * math.cos(ang), cy + r * math.sin(ang)))
    draw.polygon(pts, fill=color)


def _draw_approved_stamp(
    canvas: Image.Image,
    cx: int = 1178,
    cy: int = 80,
    angle_deg: float = 7.5,
):
    """Draw a tilted metallic-emerald 'PIREP VERIFIED / APPROVED' aviation stamp."""
    sw, sh = 560, 184  # 2x supersampled before downscaling for crisp tilted edges
    stamp = Image.new("RGBA", (sw, sh), (0, 0, 0, 0))
    sd = ImageDraw.Draw(stamp, "RGBA")

    sd.rounded_rectangle(
        [16, 16, sw - 16, sh - 16],
        radius=22,
        fill=(14, 74, 42, 115),
        outline=(74, 228, 146, 242),
        width=7,
    )
    sd.rounded_rectangle(
        [28, 28, sw - 28, sh - 28],
        radius=14,
        outline=(140, 248, 188, 188),
        width=3,
    )

    f_top = _font("seguisb.ttf", 20)
    top_txt = "PIREP VERIFIED"
    sd.text((sw // 2, 50), top_txt, font=f_top, fill=(198, 252, 220, 236), anchor="mm")
    tb = sd.textbbox((sw // 2, 50), top_txt, font=f_top, anchor="mm")
    sd.line([(56, 50), (tb[0] - 28, 50)], fill=(116, 242, 172, 175), width=2)
    sd.line([(tb[2] + 28, 50), (sw - 56, 50)], fill=(116, 242, 172, 175), width=2)
    _draw_mini_star(sd, tb[0] - 14, 50, 7.5, (158, 250, 198, 230))
    _draw_mini_star(sd, tb[2] + 14, 50, 7.5, (158, 250, 198, 230))

    f_app = _font("seguibl.ttf", 54)
    _draw_gold_gradient_text(
        stamp,
        sw // 2,
        96,
        "APPROVED",
        f_app,
        top_rgb=(232, 255, 242),
        bot_rgb=(116, 238, 172),
        anchor="mm",
    )
    sd = ImageDraw.Draw(stamp, "RGBA")

    f_bot = _font("seguisb.ttf", 17)
    sd.text(
        (sw // 2, 136),
        "CREWCENTER  \u00b7  QRV 2026",
        font=f_bot,
        fill=(180, 245, 208, 214),
        anchor="mm",
    )

    # Stamped ink grain texture
    rng_np = np.random.default_rng(202612)
    grain = rng_np.uniform(0.80, 1.0, (sh // 2, sw // 2)).astype(np.float32)
    grain = np.where(rng_np.random((sh // 2, sw // 2)) < 0.045, rng_np.uniform(0.45, 0.75, (sh // 2, sw // 2)), grain)
    grain_2x = np.repeat(np.repeat(grain, 2, axis=0), 2, axis=1)
    arr = np.array(stamp, dtype=np.float32)
    arr[:, :, 3] *= grain_2x
    stamp = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), mode="RGBA")

    rot_2x = stamp.rotate(angle_deg, resample=Image.BICUBIC, expand=True)
    final_stamp = rot_2x.resize((rot_2x.width // 2, rot_2x.height // 2), Image.LANCZOS)

    sh_alpha = final_stamp.split()[3].point(lambda p: int(p * 0.45))
    sh_img = Image.new("RGBA", final_stamp.size, (6, 18, 10, 255))
    sh_img.putalpha(sh_alpha)
    sh_img = sh_img.filter(ImageFilter.GaussianBlur(radius=3.5))

    px = cx - final_stamp.width // 2
    py = cy - final_stamp.height // 2
    canvas.alpha_composite(sh_img, (px + 2, py + 4))
    canvas.alpha_composite(final_stamp, (px, py))


def _draw_ticket_reward_pill(
    canvas: Image.Image,
    x0: int,
    y0: int,
    x1: int,
    y1: int,
    tickets_earned: int,
):
    """Draw an emerald-and-gold ChristmasTickets reward badge pill inside PAX card."""
    pad = 12
    pw, ph = (x1 - x0) + pad * 2, (y1 - y0) + pad * 2
    pill_layer = Image.new("RGBA", (pw, ph), (0, 0, 0, 0))
    pd = ImageDraw.Draw(pill_layer, "RGBA")

    glow = Image.new("RGBA", (pw, ph), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow, "RGBA")
    gd.rounded_rectangle([pad, pad, pw - pad, ph - pad], radius=12, outline=(76, 224, 146, 120), width=5)
    glow = glow.filter(ImageFilter.GaussianBlur(radius=3.5))
    pill_layer.alpha_composite(glow)

    pd = ImageDraw.Draw(pill_layer, "RGBA")
    pd.rounded_rectangle(
        [pad, pad, pw - pad, ph - pad],
        radius=12,
        fill=(14, 68, 38, 228),
        outline=(92, 228, 152, 232),
        width=2,
    )
    pd.rounded_rectangle(
        [pad + 3, pad + 3, pw - pad - 3, ph - pad - 3],
        radius=9,
        outline=(210, 255, 226, 48),
        width=1,
    )

    count_str = f"+{tickets_earned:,}" if tickets_earned > 0 else "0"
    unit_str = "TICKETS"
    inner_max_w = (x1 - x0) - 24
    icon_w, icon_h, icon_gap = 28, 18, 11

    f_cnt = _font("segoeuib.ttf", 23)
    f_unit = _font("seguisb.ttf", 19)
    cb = (0, 0, 0, 0)
    ub = (0, 0, 0, 0)
    for sz in (23, 21, 19, 17):
        f_cnt = _font("segoeuib.ttf", sz)
        f_unit = _font("seguisb.ttf", max(14, sz - 4))
        cb = pd.textbbox((0, 0), count_str, font=f_cnt, anchor="ls")
        ub = pd.textbbox((0, 0), unit_str, font=f_unit, anchor="ls")
        tot = icon_w + icon_gap + (cb[2] - cb[0]) + 7 + (ub[2] - ub[0])
        if tot <= inner_max_w:
            break

    tot_w = icon_w + icon_gap + (cb[2] - cb[0]) + 7 + (ub[2] - ub[0])
    start_x = (pw - tot_w) / 2.0
    cy_mid = ph / 2.0

    tx0, ty0 = start_x, cy_mid - icon_h / 2.0
    tx1, ty1 = tx0 + icon_w, ty0 + icon_h
    pd.rounded_rectangle([tx0, ty0, tx1, ty1], radius=3, fill=(246, 208, 98, 245))
    notch_r = 3.2
    pd.ellipse([tx0 - notch_r, cy_mid - notch_r, tx0 + notch_r, cy_mid + notch_r], fill=(14, 68, 38, 255))
    pd.ellipse([tx1 - notch_r, cy_mid - notch_r, tx1 + notch_r, cy_mid + notch_r], fill=(14, 68, 38, 255))
    perf_x = tx0 + icon_w * 0.68
    for py_off in (-4.5, 0.0, 4.5):
        pd.ellipse([perf_x - 1.2, cy_mid + py_off - 1.2, perf_x + 1.2, cy_mid + py_off + 1.2], fill=(14, 68, 38, 220))

    txt_base_y = cy_mid + 8
    cnt_x = tx1 + icon_gap - cb[0]
    pd.text((cnt_x, txt_base_y), count_str, font=f_cnt, fill=(238, 255, 244, 255), anchor="ls")
    cb_real = pd.textbbox((cnt_x, txt_base_y), count_str, font=f_cnt, anchor="ls")
    pd.text((cb_real[2] + 7 - ub[0], txt_base_y), unit_str, font=f_unit, fill=(248, 216, 118, 255), anchor="ls")

    canvas.alpha_composite(pill_layer, (x0 - pad, y0 - pad))


@functools.lru_cache(maxsize=2)
def _build_static_template(logo_path: str = LOGO_PATH) -> Tuple[Image.Image, Image.Image]:
    """Build and cache the static card artwork + rounded clip mask."""
    img = _build_velvet_background(W, H)

    wm_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    wm_draw = ImageDraw.Draw(wm_layer, "RGBA")
    wm_specs = [
        (330, 88, 52, 15, 22),
        (560, 260, 68, 8, 16),
        (1060, 118, 56, 28, 22),
        (1245, 295, 74, 12, 16),
        (225, 425, 62, 22, 16),
        (765, 480, 72, 19, 15),
        (1135, 575, 58, 35, 18),
        (395, 645, 64, 10, 16),
        (1505, 140, 58, 25, 18),
        (1655, 510, 62, 14, 16),
    ]
    for sx, sy, sr, rot, alp in wm_specs:
        _draw_snowflake(wm_draw, sx, sy, sr, (175, 72, 104, alp), rotation_deg=rot, width=2)
    img.alpha_composite(wm_layer)

    draw = ImageDraw.Draw(img, "RGBA")
    logo_h = 88
    logo_y = 35
    logo_right = MAIN_LX + 290
    if logo_path and os.path.exists(logo_path):
        try:
            with Image.open(logo_path) as raw_logo:
                logo = raw_logo.convert("RGBA")
                scale_w = int(logo.width * logo_h / max(1, logo.height))
                logo = logo.resize((scale_w, logo_h), Image.LANCZOS)
            img.alpha_composite(logo, (MAIN_LX, logo_y))
            logo_right = MAIN_LX + logo.width
        except Exception:
            draw.text((MAIN_LX, logo_y + 10), "QATARI\nVIRTUAL", font=_font("segoeuib.ttf", 30), fill=WHITE)
    else:
        draw.text((MAIN_LX, logo_y + 10), "QATARI\nVIRTUAL", font=_font("segoeuib.ttf", 30), fill=WHITE)

    sep_x = logo_right + 30
    draw.line([(sep_x, 38), (sep_x, 124)], fill=(*GOLD_BRIGHT, 210), width=3)

    f_qrv = _font("seguibl.ttf", 84)
    qrv_x = sep_x + 30
    qrv_bb = _draw_gold_gradient_text(img, qrv_x, 28, "QRV", f_qrv)
    qrv_w = qrv_bb[2] - qrv_bb[0]
    draw = ImageDraw.Draw(img, "RGBA")

    ev_x = qrv_x + qrv_w + 26
    draw.text((ev_x, 34), "CHRISTMAS", font=_font("segoeuib.ttf", 40), fill=WHITE)
    draw.text((ev_x + 2, 82), "OPERATIONS 2026", font=_font("seguisb.ttf", 25), fill=GOLD_BRIGHT)

    hdr_rule_y = 152
    rule_glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    rg_d = ImageDraw.Draw(rule_glow, "RGBA")
    rg_d.line([(95, hdr_rule_y), (MAIN_RX, hdr_rule_y)], fill=(255, 206, 92, 165), width=8)
    rule_glow = rule_glow.filter(ImageFilter.GaussianBlur(radius=4.5))
    img.alpha_composite(rule_glow)

    draw = ImageDraw.Draw(img, "RGBA")
    draw.line([(95, hdr_rule_y), (MAIN_RX, hdr_rule_y)], fill=(242, 204, 108, 245), width=3)

    hero_glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    hg_d = ImageDraw.Draw(hero_glow, "RGBA")
    hg_d.rounded_rectangle([984, 184, MAIN_RX, 378], radius=18, outline=(255, 208, 92, 135), width=7)
    hg_d.ellipse([1045, 206, 1285, 354], fill=(228, 162, 52, 48))
    hero_glow = hero_glow.filter(ImageFilter.GaussianBlur(radius=7.5))
    img.alpha_composite(hero_glow)

    hero_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    hd = ImageDraw.Draw(hero_layer, "RGBA")
    hd.rounded_rectangle(
        [MAIN_LX, 184, 960, 378],
        radius=18,
        fill=(44, 3, 18, 135),
        outline=(232, 188, 86, 150),
        width=2,
    )
    hd.rounded_rectangle(
        [984, 184, MAIN_RX, 378],
        radius=18,
        fill=(52, 4, 20, 185),
        outline=(248, 212, 112, 240),
        width=2,
    )
    hd.rounded_rectangle(
        [990, 190, MAIN_RX - 6, 372],
        radius=14,
        outline=(242, 200, 96, 75),
        width=1,
    )
    hd.line([(MAIN_LX, 504), (MAIN_RX, 504)], fill=(216, 172, 74, 90), width=1)
    img.alpha_composite(hero_layer)

    _draw_gold_tree(img, 1742, 314, 162)

    od = ImageDraw.Draw(img, "RGBA")
    y_perf = 20
    while y_perf < H - 24:
        od.line(
            [(STUB_X, y_perf), (STUB_X, min(y_perf + 12, H - 22))],
            fill=(228, 186, 86, 210),
            width=3,
        )
        y_perf += 22

    od.rounded_rectangle(
        [4, 4, W - 5, H - 5],
        radius=CORNER_R - 2,
        outline=(232, 188, 86, 195),
        width=2,
    )
    od.rounded_rectangle(
        [10, 10, W - 11, H - 11],
        radius=CORNER_R - 6,
        outline=(195, 150, 62, 82),
        width=1,
    )

    pine_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    pd = ImageDraw.Draw(pine_layer, "RGBA")

    stub_boughs = [
        (((1515, -10), (1605, 42), (1685, 118)), 34, 48, 101, 1.0),
        (((1585, -14), (1685, 58), (1765, 145)), 38, 54, 102, 1.05),
        (((1675, -12), (1770, 55), (1875, 132)), 40, 56, 103, 1.1),
        (((1935, 18), (1865, 92), (1780, 185)), 38, 52, 104, 1.05),
        (((1935, 95), (1882, 175), (1835, 265)), 34, 46, 105, 0.95),
        (((1935, 185), (1895, 248), (1862, 322)), 28, 36, 106, 0.85),
    ]
    for b_pts, nlen, dens, sd, sc in stub_boughs:
        _draw_pine_sprig(pd, b_pts, nlen, dens, sd, sc)

    bl_boughs = [
        (((-10, 625), (32, 578), (68, 505)), 32, 42, 201, 0.95),
        (((-10, 685), (48, 638), (96, 565)), 34, 46, 202, 1.0),
        (((18, 760), (62, 705), (120, 650)), 32, 42, 203, 0.95),
        (((-8, 755), (56, 735), (134, 712)), 28, 38, 204, 0.9),
        (((-6, 170), (22, 176), (48, 184)), 14, 14, 205, 0.72),
    ]
    for b_pts, nlen, dens, sd, sc in bl_boughs:
        _draw_pine_sprig(pd, b_pts, nlen, dens, sd, sc)

    pine_shadow = pine_layer.split()[3].point(lambda p: int(p * 0.45))
    sh_img = Image.new("RGBA", (W, H), (10, 1, 5, 255))
    sh_img.putalpha(pine_shadow)
    sh_img = sh_img.filter(ImageFilter.GaussianBlur(radius=5.0))
    img.alpha_composite(sh_img, (3, 5))
    img.alpha_composite(pine_layer)

    _draw_holly_cluster(img, 54, 186, scale=0.88, angles=(-115, -35, 45))
    _draw_holly_cluster(img, 1336, hdr_rule_y - 6, scale=0.86, angles=(-160, -80, 150))
    _draw_holly_cluster(img, 64, 705, scale=0.95, angles=(-135, -55, 18))

    _draw_bauble_3d(img, 1858, 138, 21, kind="red", string_top_y=62)
    _draw_bauble_3d(img, 1742, 110, 25, kind="gold", string_top_y=28)
    _draw_bauble_3d(img, 1648, 206, 33, kind="red", string_top_y=78)
    _draw_bauble_3d(img, 1785, 246, 28, kind="gold", string_top_y=158)

    _draw_bauble_3d(img, 36, 578, 17, kind="gold", string_top_y=520)
    _draw_bauble_3d(img, 62, 638, 22, kind="red", string_top_y=574)

    glow_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sharp_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow_layer, "RGBA")
    sd = ImageDraw.Draw(sharp_layer, "RGBA")

    hero_sparkles = [
        (465, 152, 14, 225),
        (590, 24, 12, 210),
        (685, 152, 22, 245),
        (780, 146, 13, 210),
        (1065, 48, 11, 185),
        (1170, 152, 18, 235),
        (1245, 76, 14, 200),
        (1532, 148, 20, 245),
        (1588, 238, 12, 205),
        (1698, 286, 14, 220),
        (1838, 220, 13, 215),
        (1655, 365, 12, 215),
        (1826, 382, 15, 225),
        (1672, 468, 16, 230),
        (1842, 476, 13, 210),
        (1614, 615, 14, 220),
        (1632, 706, 18, 235),
        (1858, 675, 16, 230),
        (1874, 540, 13, 215),
        (1712, 718, 12, 200),
    ]
    for sx, sy, ssz, salp in hero_sparkles:
        _draw_sparkle_flare(gd, sd, sx, sy, ssz, alpha=salp)

    gold_flakes = [
        (1602, 278, 14, 15, 175),
        (1866, 318, 16, 28, 185),
        (1485, 88, 13, 10, 145),
        (1635, 492, 12, 20, 160),
    ]
    for fx, fy, fr, frot, falp in gold_flakes:
        _draw_snowflake(sd, fx, fy, fr, (250, 215, 120, falp), rotation_deg=frot, width=1)

    rng = random.Random(2026)
    dust_zones = [
        (1040, 1350, 20, 148, 28),
        (450, 1340, 136, 160, 18),
        (1430, 1900, 320, 730, 56),
        (1425, 1610, 20, 230, 20),
        (1180, 1380, 680, 732, 8),
        (145, 295, 680, 732, 8),
    ]
    for x0, x1, y0, y1, count in dust_zones:
        for _ in range(count):
            px = rng.randint(x0, x1)
            py = rng.randint(y0, y1)
            pr = rng.choice([1, 1, 2, 2, 3])
            pa = rng.randint(95, 225)
            gd.ellipse([px - pr * 2, py - pr * 2, px + pr * 2, py + pr * 2], fill=(255, 214, 105, pa // 2))
            sd.ellipse([px - pr, py - pr, px + pr, py + pr], fill=(255, 238, 170, pa))

    glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(radius=4.0))
    img.alpha_composite(glow_layer)
    img.alpha_composite(sharp_layer)

    _draw_approved_stamp(img, cx=1182, cy=80, angle_deg=7.5)

    card_mask = _build_card_mask(W, H, CORNER_R)
    return img, card_mask


def render_reward_ticket_png(
    dispatch_code: str,
    flight_number: str,
    origin: str,
    destination: str,
    origin_city: str,
    destination_city: str,
    registration: str,
    aircraft_model: str,
    pilot_name: str,
    copilot_name: Optional[str],
    allocated_pax: int,
    flight_date: Optional[str] = None,
    tickets_earned: Optional[int] = None,
    logo_path: str = LOGO_PATH,
) -> bytes:
    """Render a 1920x750 PIREP-Approved Christmas Reward Ticket PNG."""
    with _BASE_LOCK:
        base_img, card_mask = _build_static_template(logo_path)

    img = base_img.copy()
    draw = ImageDraw.Draw(img, "RGBA")

    date_str = (flight_date or datetime.now(timezone.utc).strftime("%d %b %Y")).strip().upper()
    origin_u = origin.strip().upper()
    dest_u = destination.strip().upper()
    dcode_u = dispatch_code.strip().upper()
    flight_u = flight_number.strip().upper()
    reg_u = registration.strip().upper()
    pax_val = max(0, int(allocated_pax))
    earned_tickets = pax_val if tickets_earned is None else max(0, int(tickets_earned))
    payload_key = f"{dcode_u}|{flight_u}|{origin_u}-{dest_u}|{reg_u}|{date_str}|{earned_tickets}"

    f_lbl = _font("seguisb.ttf", 19)
    f_foot = _font("seguisb.ttf", 16)

    route_x0, route_x1 = MAIN_LX, 960
    pax_x0, pax_x1 = 984, MAIN_RX

    draw.text((route_x0 + 26, 200), "ROUTE:", font=f_lbl, fill=GOLD_LABEL)

    arr_len, arr_gap = 104, 34
    f_icao = _font("seguibl.ttf", 84)
    o_w0 = (0, 0, 0, 0)
    d_w0 = (0, 0, 0, 0)
    for icao_sz in (84, 78, 72):
        f_icao = _font("seguibl.ttf", icao_sz)
        o_w0 = draw.textbbox((0, 0), origin_u, font=f_icao)
        d_w0 = draw.textbbox((0, 0), dest_u, font=f_icao)
        total_rt_w = (o_w0[2] - o_w0[0]) + arr_len + arr_gap * 2 + (d_w0[2] - d_w0[0])
        if total_rt_w <= (route_x1 - route_x0 - 84):
            break

    o_w = o_w0[2] - o_w0[0]
    d_w = d_w0[2] - d_w0[0]
    rt_start_x = (route_x0 + route_x1 - (o_w + arr_len + arr_gap * 2 + d_w)) // 2
    orig_x = rt_start_x - o_w0[0]
    icao_draw_y = 228

    draw.text((orig_x + 3, icao_draw_y + 4), origin_u, font=f_icao, fill=(14, 1, 6, 165))
    draw.text((orig_x, icao_draw_y), origin_u, font=f_icao, fill=WHITE)
    o_bb = draw.textbbox((orig_x, icao_draw_y), origin_u, font=f_icao)

    dest_draw_x = o_bb[2] + arr_gap * 2 + arr_len - d_w0[0]
    draw.text((dest_draw_x + 3, icao_draw_y + 4), dest_u, font=f_icao, fill=(14, 1, 6, 165))
    draw.text((dest_draw_x, icao_draw_y), dest_u, font=f_icao, fill=WHITE)
    d_bb = draw.textbbox((dest_draw_x, icao_draw_y), dest_u, font=f_icao)

    arr_cy = (o_bb[1] + o_bb[3]) // 2
    arr_x0 = o_bb[2] + arr_gap
    arr_x1 = arr_x0 + arr_len
    shaft_half, head_len, head_half = 5, 26, 16
    draw.polygon(
        [
            (arr_x0, arr_cy - shaft_half),
            (arr_x1 - head_len, arr_cy - shaft_half),
            (arr_x1 - head_len, arr_cy - head_half),
            (arr_x1, arr_cy),
            (arr_x1 - head_len, arr_cy + head_half),
            (arr_x1 - head_len, arr_cy + shaft_half),
            (arr_x0, arr_cy + shaft_half),
        ],
        fill=GOLD_BRIGHT,
    )

    city_y = max(o_bb[3], d_bb[3]) + 11
    f_orig_city = _fit_font(draw, origin_city.upper(), "seguisb.ttf", 21, 14, 280)
    f_dest_city = _fit_font(draw, destination_city.upper(), "seguisb.ttf", 21, 14, 340)
    draw.text(((o_bb[0] + o_bb[2]) // 2, city_y), origin_city.upper(), font=f_orig_city, fill=GOLD_VALUE, anchor="mt")
    draw.text(((d_bb[0] + d_bb[2]) // 2, city_y), destination_city.upper(), font=f_dest_city, fill=GOLD_VALUE, anchor="mt")

    pax_cx = (pax_x0 + pax_x1) // 2
    draw.text((pax_cx, 198), "PASSENGERS", font=f_lbl, fill=GOLD_BRIGHT, anchor="mt")
    draw.line([(pax_cx - 46, 224), (pax_cx + 46, 224)], fill=(*GOLD_BRIGHT, 140), width=1)

    pax_num_str = f"{pax_val:,}"
    pax_unit_str = "PAX"
    f_pax_num = _font("seguibl.ttf", 74)
    f_pax_unit = _font("segoeuib.ttf", 28)
    nb = (0, 0, 0, 0)
    ub = (0, 0, 0, 0)
    for p_sz in (74, 68, 60):
        f_pax_num = _font("seguibl.ttf", p_sz)
        nb = draw.textbbox((0, 0), pax_num_str, font=f_pax_num, anchor="ls")
        ub = draw.textbbox((0, 0), pax_unit_str, font=f_pax_unit, anchor="ls")
        if (nb[2] - nb[0]) + 13 + (ub[2] - ub[0]) <= (pax_x1 - pax_x0 - 44):
            break

    total_pax_w = (nb[2] - nb[0]) + 13 + (ub[2] - ub[0])
    pax_start_x = pax_cx - total_pax_w // 2
    pax_base_y = 298
    num_bb = _draw_gold_gradient_text(
        img,
        pax_start_x - nb[0],
        pax_base_y,
        pax_num_str,
        f_pax_num,
        top_rgb=(255, 242, 180),
        bot_rgb=(228, 176, 68),
        anchor="ls",
    )
    draw = ImageDraw.Draw(img, "RGBA")
    draw.text(
        (num_bb[2] + 13 - ub[0], pax_base_y),
        pax_unit_str,
        font=f_pax_unit,
        fill=(255, 243, 212),
        anchor="ls",
    )

    _draw_ticket_reward_pill(img, pax_x0 + 20, 314, pax_x1 - 20, 362, earned_tickets)
    draw = ImageDraw.Draw(img, "RGBA")

    r2_y = 406
    tier2_cols = [
        (MAIN_LX, "DISPATCH CODE:", dcode_u, 250),
        (398, "FLIGHT:", flight_u, 165),
        (582, "AIRCRAFT:", aircraft_model, 378),
        (pax_x0, "REGISTRATION:", reg_u, MAIN_RX - pax_x0),
    ]
    for cx, lbl, val, max_cw in tier2_cols:
        draw.text((cx, r2_y), lbl, font=f_lbl, fill=GOLD_LABEL)
        f_c2 = _fit_font(draw, val, "segoeuib.ttf", 35, 24, max_cw)
        draw.text((cx, r2_y + 26), val, font=f_c2, fill=WHITE)

    r3_y = 530
    cap_display = _clean_rank_prefix(pilot_name, ("Capt.", "CPT "), "Capt.")
    has_copilot = bool(copilot_name and copilot_name.strip())
    cap_max_w = 432 if has_copilot else (pax_x0 - MAIN_LX - 30)
    f_cap = _fit_font(draw, cap_display, "segoeuib.ttf", 35, 24, cap_max_w)
    draw.text((MAIN_LX, r3_y), "PILOT IN COMMAND:", font=f_lbl, fill=GOLD_LABEL)
    draw.text((MAIN_LX, r3_y + 26), cap_display, font=f_cap, fill=WHITE)

    if has_copilot:
        co_x = 582
        co_display = _clean_rank_prefix(copilot_name or "", ("FO.", "F/O "), "FO.")
        f_co = _fit_font(draw, co_display, "segoeuib.ttf", 35, 24, pax_x0 - co_x - 24)
        draw.text((co_x, r3_y), "CO-PILOT:", font=f_lbl, fill=GOLD_LABEL)
        draw.text((co_x, r3_y + 26), co_display, font=f_co, fill=WHITE)

    f_dt = _fit_font(draw, date_str, "segoeuib.ttf", 35, 24, MAIN_RX - pax_x0)
    draw.text((pax_x0, r3_y), "DATE:", font=f_lbl, fill=GOLD_LABEL)
    draw.text((pax_x0, r3_y + 26), date_str, font=f_dt, fill=WHITE)

    foot_text = "SIMULATION DISCLAIMER: FOR FLIGHT SIMULATION USE ONLY  \u00b7  QATARI VIRTUAL CHRISTMAS OPERATIONS 2026"
    draw.text(((MAIN_LX + MAIN_RX) // 2, 674), foot_text, font=f_foot, fill=(*GOLD_LABEL, 225), anchor="mt")

    _draw_vertical_barcode(img, 1438, 248, 132, 452, payload_key)
    _draw_qr_block(img, 1650, 516, 184, payload_key)

    img.putalpha(card_mask)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf.read()


class BoardingPassService:
    """Renders 1920x750 PIREP-Approved Christmas Reward Ticket PNGs off the event loop."""

    WIDTH = W
    HEIGHT = H

    def __init__(self, config_path: str = DEFAULT_CONFIG_PATH):
        self.config_path = config_path

    def _load_branding(self) -> Dict[str, Any]:
        try:
            if os.path.exists(self.config_path):
                with open(self.config_path, "r", encoding="utf-8") as handle:
                    data = json.load(handle)
                return dict((data.get("ui_branding") or {}).get("boarding_pass") or {})
        except Exception as exc:
            log.warning("Could not load boarding pass branding from %s: %s", self.config_path, exc)
        return {}

    async def render_boarding_pass(self, pass_data: Dict[str, Any]) -> bytes:
        """Render a PIREP-Approved Reward Ticket PNG asynchronously in a worker thread."""
        return await asyncio.to_thread(self._render_sync, pass_data)

    def _render_sync(self, pass_data: Dict[str, Any]) -> bytes:
        branding = {**self._load_branding(), **(pass_data.get("branding") or {})}
        configured_logo = str(branding.get("logo_path") or "").strip()
        logo_path = configured_logo if configured_logo and os.path.exists(configured_logo) else LOGO_PATH

        dispatch_code = str(pass_data.get("dispatch_code") or "XMAS-0001").strip().upper()
        origin = str(
            pass_data.get("origin")
            or pass_data.get("origin_icao")
            or pass_data.get("hub_icao")
            or "OTHH"
        ).strip().upper()
        destination = str(
            pass_data.get("destination")
            or pass_data.get("destination_icao")
            or "DEST"
        ).strip().upper()
        origin_city = str(
            pass_data.get("origin_city")
            or ("Doha" if origin == "OTHH" else origin)
        ).strip()
        destination_city = str(
            pass_data.get("destination_city")
            or ("Doha" if destination == "OTHH" else destination)
        ).strip()
        flight_number = str(
            pass_data.get("flight_number")
            or pass_data.get("leg2_callsign")
            or pass_data.get("leg1_callsign")
            or dispatch_code
        ).strip().upper()
        registration = str(pass_data.get("registration") or "A7-XXX").strip().upper()
        aircraft_model = str(pass_data.get("aircraft_model") or "Qatar Passenger").strip()
        pilot_name = str(
            pass_data.get("pilot_name")
            or pass_data.get("pilot_callsign")
            or "PILOT"
        ).strip()
        raw_copilot = pass_data.get("copilot_name") or pass_data.get("copilot_callsign")
        copilot_name = str(raw_copilot).strip() if raw_copilot and str(raw_copilot).strip() else None
        allocated_pax = max(0, int(pass_data.get("allocated_pax") or 0))
        flight_date = pass_data.get("flight_date")

        if pass_data.get("tickets_earned") is not None:
            tickets_earned = max(0, int(pass_data["tickets_earned"]))
        elif pass_data.get("ticket_reward") is not None:
            tickets_earned = max(0, int(pass_data["ticket_reward"]))
        else:
            tickets_earned = allocated_pax

        return render_reward_ticket_png(
            dispatch_code=dispatch_code,
            flight_number=flight_number,
            origin=origin,
            destination=destination,
            origin_city=origin_city,
            destination_city=destination_city,
            registration=registration,
            aircraft_model=aircraft_model,
            pilot_name=pilot_name,
            copilot_name=copilot_name,
            allocated_pax=allocated_pax,
            flight_date=flight_date,
            tickets_earned=tickets_earned,
            logo_path=logo_path,
        )
