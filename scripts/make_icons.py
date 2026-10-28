"""Draw RAVAN's launcher icon (a cyan holographic orb) at every density.

The icon is generated rather than hand-drawn so it stays identical to the
in-app orb: run `python scripts/make_icons.py` after changing the palette.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
RES = ROOT / "android" / "app" / "src" / "main" / "res"
S = 2048

CYAN = (0, 212, 255)
VIOLET = (111, 125, 255)


def _ramp(t: np.ndarray, stops: list[tuple[float, tuple[int, int, int]]]) -> np.ndarray:
    out = np.zeros(t.shape + (3,), np.float32)
    for (p0, c0), (p1, c1) in zip(stops, stops[1:]):
        mask = (t >= p0) & (t <= p1)
        f = ((t - p0) / max(1e-6, p1 - p0))[mask][:, None]
        for k in range(3):
            out[..., k][mask] = c0[k] + (c1[k] - c0[k]) * f[:, 0]
    return out


def _layer(rgb, alpha: np.ndarray) -> Image.Image:
    a = np.clip(alpha, 0, 255).astype(np.uint8)
    rgba = np.zeros((S, S, 4), np.uint8)
    rgba[..., 0], rgba[..., 1], rgba[..., 2] = rgb
    rgba[..., 3] = a
    return Image.fromarray(rgba, "RGBA")


def draw_orb(sphere_r: float, ring_scale: float, with_background: bool) -> Image.Image:
    yy, xx = np.mgrid[0:S, 0:S].astype(np.float32)
    cx = cy = S / 2.0
    d = np.hypot(xx - cx, yy - cy)

    base = Image.new("RGBA", (S, S), (2, 7, 12, 255) if with_background else (0, 0, 0, 0))

    # Outer holographic glow.
    glow = np.clip(1.0 - d / (sphere_r * 1.65), 0.0, 1.0) ** 2 * 110
    base = Image.alpha_composite(base, _layer(CYAN, glow))

    # Lit sphere, offset highlight.
    ox, oy = cx - sphere_r * 0.30, cy - sphere_r * 0.34
    sd = np.hypot(xx - ox, yy - oy)
    t = np.clip(sd / (sphere_r * 1.42), 0.0, 1.0)
    colours = _ramp(t, [
        (0.00, (185, 244, 255)),
        (0.42, (40, 180, 214)),
        (0.78, (8, 92, 118)),
        (1.00, (4, 23, 34)),
    ])
    edge = np.clip((sphere_r - d) / 2.0 + 0.5, 0.0, 1.0)
    rgba = np.zeros((S, S, 4), np.uint8)
    rgba[..., :3] = np.clip(colours, 0, 255).astype(np.uint8)
    rgba[..., 3] = (edge * 255).astype(np.uint8)
    base = Image.alpha_composite(base, Image.fromarray(rgba, "RGBA"))

    # Scan band, clipped to the sphere.
    band_y = cy + sphere_r * 0.16
    band = np.exp(-((yy - band_y) / (sphere_r * 0.10)) ** 2) * 70 * edge
    base = Image.alpha_composite(base, _layer((255, 255, 255), band))

    # Two orbital rings.
    for colour, width_scale, rot, squash in (
        (CYAN + (150,), 1.22, 16, 0.34),
        (VIOLET + (95,), 0.92, -28, 0.92),
    ):
        ring = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        radius = sphere_r * ring_scale * width_scale
        box = (cx - radius, cy - radius * squash, cx + radius, cy + radius * squash)
        ImageDraw.Draw(ring).ellipse(box, outline=colour, width=int(S * 0.009))
        ring = ring.rotate(rot, resample=Image.BICUBIC, center=(cx, cy))
        base = Image.alpha_composite(base, ring)

    # Bright core and a soft specular spot.
    core = np.exp(-(d / (sphere_r * 0.13)) ** 2) * 255
    base = Image.alpha_composite(base, _layer((232, 252, 255), core))
    hx, hy = cx - sphere_r * 0.32, cy - sphere_r * 0.40
    hd = np.hypot(xx - hx, yy - hy)
    gloss = np.exp(-(hd / (sphere_r * 0.30)) ** 2) * 55 * edge
    base = Image.alpha_composite(base, _layer((255, 255, 255), gloss))

    return base


def save(img: Image.Image, path: Path, px: int, round_mask: bool = False) -> None:
    out = img.resize((px, px), Image.LANCZOS)
    if round_mask:
        mask = Image.new("L", (px * 4, px * 4), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, px * 4 - 1, px * 4 - 1), fill=255)
        out.putalpha(mask.resize((px, px), Image.LANCZOS))
    path.parent.mkdir(parents=True, exist_ok=True)
    out.save(path)


def main() -> None:
    full = draw_orb(sphere_r=S * 0.205, ring_scale=1.18, with_background=True)
    foreground = draw_orb(sphere_r=S * 0.150, ring_scale=1.18, with_background=False)

    legacy = {"mdpi": 48, "hdpi": 72, "xhdpi": 96, "xxhdpi": 144, "xxxhdpi": 192}
    adaptive = {"mdpi": 108, "hdpi": 162, "xhdpi": 216, "xxhdpi": 324, "xxxhdpi": 432}

    for name, px in legacy.items():
        save(full, RES / f"mipmap-{name}" / "ic_launcher.png", px)
        save(full, RES / f"mipmap-{name}" / "ic_launcher_round.png", px, round_mask=True)
    for name, px in adaptive.items():
        save(foreground, RES / f"mipmap-{name}" / "ic_launcher_foreground.png", px)

    full.resize((512, 512), Image.LANCZOS).save(ROOT / "web" / "icons" / "app-icon-512.png")
    print("Wrote launcher icons and web/icons/app-icon-512.png")


if __name__ == "__main__":
    main()
