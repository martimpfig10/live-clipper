"""Final video: live clip on top, chosen photo below, hook title. 1080x1920."""
from __future__ import annotations

import textwrap
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

from .config import Settings
from .media import run

try:  # iPhone photos
    from pillow_heif import register_heif_opener

    register_heif_opener()
except Exception:  # pragma: no cover - optional
    pass


@dataclass
class Crop:
    cx: float = 0.5
    cy: float = 0.5
    zoom: float = 1.0

    @classmethod
    def from_dict(cls, d: dict | None) -> "Crop":
        d = d or {}
        return cls(
            cx=min(1.0, max(0.0, float(d.get("cx", 0.5)))),
            cy=min(1.0, max(0.0, float(d.get("cy", 0.5)))),
            zoom=min(4.0, max(1.0, float(d.get("zoom", 1.0)))),
        )


def _even(x: float) -> int:
    return max(2, int(round(x / 2)) * 2)


def crop_box(src_w: int, src_h: int, out_w: int, out_h: int, crop: Crop) -> tuple[int, int, int, int]:
    """Largest window with the output aspect ratio, shrunk by zoom, centred at (cx, cy)."""
    aspect = out_w / out_h
    if src_w / src_h > aspect:
        h = src_h
        w = src_h * aspect
    else:
        w = src_w
        h = src_w / aspect
    w, h = w / crop.zoom, h / crop.zoom
    w, h = min(_even(w), src_w - src_w % 2), min(_even(h), src_h - src_h % 2)
    x = min(max(0, crop.cx * src_w - w / 2), src_w - w)
    y = min(max(0, crop.cy * src_h - h / 2), src_h - h)
    return w, h, int(x), int(y)


def _font(s: Settings, size: int):
    path = s.font_path
    if path:
        return ImageFont.truetype(path, size)
    return ImageFont.load_default(size=size)


def hook_overlay(s: Settings, hook: str, out: Path) -> Path:
    cfg = s.section("render")
    W, H = int(cfg.get("width", 1080)), int(cfg.get("height", 1920))
    split = float(cfg.get("split", 0.5))
    size = int(cfg.get("hook_font_size", 64))
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    max_w = int(W * 0.86)

    # shrink until it fits in 3 lines
    for _ in range(12):
        font = _font(s, size)
        avg = max(1, draw.textlength("abcdefghijklmnopqrstuvwxyz", font=font) / 26)
        lines = textwrap.wrap(hook.upper(), width=max(8, int(max_w / avg)))
        widest = max((draw.textlength(line, font=font) for line in lines), default=0)
        if len(lines) <= 3 and widest <= max_w:
            break
        size = int(size * 0.9)

    line_h = int(size * 1.18)
    pad_x, pad_y = int(size * 0.55), int(size * 0.35)
    box_w = int(widest) + 2 * pad_x
    box_h = line_h * len(lines) + 2 * pad_y
    if cfg.get("hook_position", "seam") == "top":
        top = int(H * 0.04)
    else:
        top = int(H * split - box_h / 2)
    left = (W - box_w) // 2
    draw.rounded_rectangle(
        [left, top, left + box_w, top + box_h], radius=int(size * 0.35), fill=(255, 255, 255, 245)
    )
    for i, line in enumerate(lines):
        lw = draw.textlength(line, font=font)
        draw.text(((W - lw) / 2, top + pad_y + i * line_h), line, font=font, fill=(10, 10, 10, 255))
    img.save(out)
    return out


def prepare_photo(src: Path, w: int, h: int, out: Path) -> Path:
    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        im = ImageOps.fit(im, (w, h), method=Image.LANCZOS, centering=(0.5, 0.4))
        im.save(out, quality=92)
    return out


def placeholder_photo(w: int, h: int, out: Path) -> Path:
    Image.new("RGB", (w, h), (24, 24, 28)).save(out, quality=90)
    return out


def render_post(
    s: Settings,
    src: Path,
    src_w: int,
    src_h: int,
    start: float,
    end: float,
    hook: str,
    photo: Path | None,
    crop: Crop,
    out: Path,
) -> Path:
    cfg = s.section("render")
    W, H = int(cfg.get("width", 1080)), int(cfg.get("height", 1920))
    top_h = _even(H * float(cfg.get("split", 0.5)))
    bot_h = H - top_h
    tmp = out.parent
    tmp.mkdir(parents=True, exist_ok=True)
    stem = out.stem
    photo_img = tmp / f"{stem}_photo.jpg"
    if photo and Path(photo).exists():
        prepare_photo(Path(photo), W, bot_h, photo_img)
    else:
        placeholder_photo(W, bot_h, photo_img)
    hook_png = hook_overlay(s, hook, tmp / f"{stem}_hook.png")

    cw, ch, cx, cy = crop_box(src_w, src_h, W, top_h, crop)
    fps = int(cfg.get("fps", 30))
    dur = max(0.5, end - start)
    graph = (
        f"[0:v]crop={cw}:{ch}:{cx}:{cy},scale={W}:{top_h}:flags=lanczos,setsar=1,fps={fps}[top];"
        f"[1:v]scale={W}:{bot_h},setsar=1,fps={fps}[bot];"
        f"[top][bot]vstack=inputs=2[stk];"
        f"[stk][2:v]overlay=0:0,format=yuv420p[v]"
    )
    part = out.with_suffix(".part.mp4")
    run([
        "ffmpeg", "-nostdin", "-y", "-v", "error",
        "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(src),
        "-loop", "1", "-framerate", str(fps), "-i", str(photo_img),
        "-i", str(hook_png),
        "-filter_complex", graph,
        "-map", "[v]", "-map", "0:a?", "-t", f"{dur:.3f}",
        "-c:v", "libx264", "-preset", str(cfg.get("preset", "veryfast")), "-crf", str(cfg.get("crf", 21)),
        "-profile:v", "high", "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
        "-movflags", "+faststart", str(part),
    ])
    part.replace(out)
    for f in (photo_img, hook_png):
        f.unlink(missing_ok=True)
    return out
