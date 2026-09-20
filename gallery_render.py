"""
Рендер .tgs (Lottie) в PNG + сборка коллажа в стиле emoji-галереи.
БЕЗ КЭША — каждый раз рендерит заново, чтобы превью совпадало с выдачей.
"""

import os
import gzip
import json
import tempfile
import logging
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

try:
    from rlottie_python import LottieAnimation
    _HAS_RLOTTIE = True
except Exception as e:
    LottieAnimation = None
    _HAS_RLOTTIE = False
    logger.warning("rlottie_python не установлен: %s", e)


# ─── Настройки ──────────────────────────────────────────────────────────────

CELL_PX      = 200
PAD_PX       = 12
MARGIN_PX    = 20
RADIUS       = 18

BG_COLOR     = (255, 255, 255)
CELL_BG      = (168, 168, 168)
BADGE_BG     = (30, 30, 30)
BADGE_TEXT   = (255, 255, 255)
BADGE_R      = 22

COLS = 4
ROWS = 3
MAX_PER_PAGE = COLS * ROWS   # 12


# ─── Шрифт ──────────────────────────────────────────────────────────────────

def _load_label_font(size: int = 22):
    candidates = [
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/segoeuib.ttf",
        "arial.ttf",
    ]
    for c in candidates:
        try:
            return ImageFont.truetype(c, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _placeholder_cell(size: int = CELL_PX) -> Image.Image:
    img = Image.new("RGB", (size, size), CELL_BG)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, size - 1, size - 1], outline=(120, 120, 120), width=3)
    d.line([0, 0, size, size], fill=(120, 120, 120), width=3)
    d.line([0, size, size, 0], fill=(120, 120, 120), width=3)
    return img


# ─── Рендер одного .tgs в PNG (без кэша) ────────────────────────────────────

def render_tgs_frame(tgs_path: Path, out_png: Path,
                     size: int = CELL_PX, frame_index: int = 0) -> bool:
    if not _HAS_RLOTTIE:
        return False

    try:
        with open(tgs_path, "rb") as f:
            head = f.read(4)
        is_gzip = head[:2] == b"\x1f\x8b"

        if is_gzip:
            with gzip.open(str(tgs_path), "rb") as f:
                raw = f.read()
        else:
            with open(tgs_path, "rb") as f:
                raw = f.read()

        try:
            json.loads(raw)
        except Exception:
            return False

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
            tmp.write(raw)
            tmp_json = tmp.name

        try:
            anim = LottieAnimation.from_file(tmp_json)
            total = anim.lottie_animation_get_totalframe()
            if total <= 0:
                return False
            fi = min(frame_index, max(0, int(total) - 1))

            img = None
            try:
                img = anim.render_pillow_frame(frame_num=fi)
            except Exception:
                pass
            if img is None:
                return False

            img = img.convert("RGBA")
            img.thumbnail((size, size), Image.LANCZOS)

            canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            ox = (size - img.width) // 2
            oy = (size - img.height) // 2
            canvas.paste(img, (ox, oy), img)

            canvas.save(out_png, "PNG", optimize=True)
            return True
        finally:
            try:
                os.unlink(tmp_json)
            except OSError:
                pass
    except Exception as e:
        logger.warning("render %s: %s", tgs_path.name, e)
        return False


# ─── Сборка коллажа (БЕЗ КЭША) ──────────────────────────────────────────────

def _rounded_rect(draw, box, radius, fill):
    x1, y1, x2, y2 = box
    r = radius
    draw.rectangle([x1 + r, y1, x2 - r, y2], fill=fill)
    draw.rectangle([x1, y1 + r, x2, y2 - r], fill=fill)
    draw.pieslice([x1, y1, x1 + 2*r, y1 + 2*r], 180, 270, fill=fill)
    draw.pieslice([x2 - 2*r, y1, x2, y1 + 2*r], 270, 360, fill=fill)
    draw.pieslice([x1, y2 - 2*r, x1 + 2*r, y2], 90, 180, fill=fill)
    draw.pieslice([x2 - 2*r, y2 - 2*r, x2, y2], 0, 90, fill=fill)


def make_gallery_image(tgs_paths: list,
                       page_number: int = 1,
                       total_pages: int = 1,
                       out_path: Path | None = None) -> Path | None:
    """
    Собирает коллаж. Кэш НЕ используется — всегда рендерит заново.
    """
    if out_path is None:
        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        out_path = Path(tmp.name)
        tmp.close()

    W = MARGIN_PX * 2 + COLS * CELL_PX + (COLS - 1) * PAD_PX
    H = MARGIN_PX * 2 + ROWS * CELL_PX + (ROWS - 1) * PAD_PX

    canvas = Image.new("RGB", (W, H), BG_COLOR)
    draw = ImageDraw.Draw(canvas)

    label_font = _load_label_font(22)

    for i in range(MAX_PER_PAGE):
        row = i // COLS
        col = i % COLS

        x = MARGIN_PX + col * (CELL_PX + PAD_PX)
        y = MARGIN_PX + row * (CELL_PX + PAD_PX)

        _rounded_rect(draw, [x, y, x + CELL_PX, y + CELL_PX], RADIUS, CELL_BG)

        if i < len(tgs_paths):
            tmp_cell = out_path.parent / f"_cell_{os.getpid()}_{i}.png"
            ok = render_tgs_frame(tgs_paths[i], tmp_cell, size=int(CELL_PX * 0.75))
            if ok and tmp_cell.exists():
                cell_img = Image.open(tmp_cell).convert("RGBA")
                cw, ch = cell_img.size
                ox = x + (CELL_PX - cw) // 2
                oy = y + (CELL_PX - ch) // 2
                canvas.paste(cell_img, (ox, oy), cell_img)
                try:
                    tmp_cell.unlink()
                except OSError:
                    pass
            else:
                canvas.paste(_placeholder_cell(CELL_PX).resize(
                    (int(CELL_PX * 0.75), int(CELL_PX * 0.75))),
                    (x + CELL_PX // 8, y + CELL_PX // 8))
                try:
                    tmp_cell.unlink(missing_ok=True)
                except OSError:
                    pass

        badge_cx = x + BADGE_R + 4
        badge_cy = y + BADGE_R + 4
        draw.ellipse(
            [badge_cx - BADGE_R, badge_cy - BADGE_R,
             badge_cx + BADGE_R, badge_cy + BADGE_R],
            fill=BADGE_BG
        )
        num = str(i + 1)
        bbox = draw.textbbox((0, 0), num, font=label_font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        draw.text(
            (badge_cx - tw // 2, badge_cy - th // 2 - 2),
            num, fill=BADGE_TEXT, font=label_font
        )

    canvas.save(out_path, "PNG", optimize=True)
    return out_path


# ─── Тест ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    shared = Path(__file__).parent / "templates" / "shared" / "main1"
    files = sorted(shared.glob("*.tgs"))[:12]
    if not files:
        print("Нет .tgs в templates/shared/main1")
        raise SystemExit(1)
    out = make_gallery_image(files, page_number=1, total_pages=1)
    print("Готово:", out, "размер:", out.stat().st_size, "байт")