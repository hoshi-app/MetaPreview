from __future__ import annotations

from enum import Enum
import logging
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from selector_cards import (
    OUTPUT_SIZE as SELECTOR_OUTPUT_SIZE,
    POSTER_SOURCE,
    apply_opacity_mask,
    list_local_posters,
    make_background,
    setup_logging,
)

ROOT_DIR = Path(__file__).parent
SOURCES_DIR = ROOT_DIR / "sources"
LOGO_SVG_PATH = SOURCES_DIR / "logo.svg"
LOGO_PNG_PATH = SOURCES_DIR / "logo.png"
logger = logging.getLogger("metapreview")
FONT_SEMIBOLD = SOURCES_DIR / "Inter_18pt-SemiBold.ttf"
FONT_REGULAR = SOURCES_DIR / "Inter_18pt-Regular.ttf"

CANVAS_SIZE = (1200, 630)
BG_COLOR = (0x17, 0x17, 0x17)

MARGIN_TOP = 76
MARGIN_BOTTOM = 76
MARGIN_LEFT = 90
MARGIN_RIGHT = 90

NAME_FONT_SIZE = 64
NAME_COLOR = (0xF1, 0xF1, 0xF1)

DESCRIPTION_GAP = 30
DESCRIPTION_FONT_SIZE = 24
DESCRIPTION_COLOR = (0xD9, 0xD9, 0xD9)
DESCRIPTION_MAX_WIDTH = 510
DESCRIPTION_MAX_HEIGHT = 165

LOGO_SIZE = (122, 87)
LOGO_COLOR = (0xFF, 0xFF, 0xFF)
LOGO_RENDER_SCALE = 2
POSTER_LAYER_SCALE = 2
ELLIPSIS = "..."


class PreviewMode(str, Enum):
    RELEASE = "ReleasePreview"
    SELECTOR = "SelectorPreview"


def _load_truetype(path: Path, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    if path.is_file():
        return ImageFont.truetype(str(path), size=size)
    return ImageFont.load_default()


def load_inter_semibold(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    return _load_truetype(FONT_SEMIBOLD, size)


def load_inter_regular(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    return _load_truetype(FONT_REGULAR, size)


def apply_logo_color(
    logo: Image.Image,
    color: tuple[int, int, int] = LOGO_COLOR,
) -> Image.Image:
    logo = logo.convert("RGBA")
    alpha = logo.getchannel("A")
    white = Image.new("L", logo.size, color[0])
    return Image.merge("RGBA", (white, white, white, alpha))


def load_logo_rgba(size: tuple[int, int] = LOGO_SIZE) -> Image.Image | None:
    render_w = size[0] * LOGO_RENDER_SCALE
    render_h = size[1] * LOGO_RENDER_SCALE

    if LOGO_PNG_PATH.is_file():
        logo = Image.open(LOGO_PNG_PATH).convert("RGBA")
        if logo.size != (render_w, render_h):
            logo = logo.resize((render_w, render_h), Image.Resampling.LANCZOS)
        logo = apply_logo_color(logo)
        if LOGO_RENDER_SCALE != 1:
            logo = logo.resize(size, Image.Resampling.LANCZOS)
        return logo

    if not LOGO_SVG_PATH.is_file():
        return None

    try:
        import fitz
    except ImportError:
        logger.warning("PyMuPDF is not installed; logo.svg will be skipped")
        return None

    try:
        doc = fitz.open(str(LOGO_SVG_PATH))
        page = doc[0]
        rect = page.rect
        if rect.width <= 0 or rect.height <= 0:
            doc.close()
            return None
        matrix = fitz.Matrix(render_w / rect.width, render_h / rect.height)
        pix = page.get_pixmap(matrix=matrix, alpha=True)
        doc.close()
        logo = Image.frombytes("RGBA", (pix.width, pix.height), pix.samples)
        logo = apply_logo_color(logo)
        if LOGO_RENDER_SCALE != 1:
            logo = logo.resize(size, Image.Resampling.LANCZOS)
        return logo
    except Exception:
        logger.exception("Failed to render logo from %s", LOGO_SVG_PATH)
        return None


def _line_height(font: ImageFont.FreeTypeFont | ImageFont.ImageFont) -> int:
    ascent, descent = font.getmetrics()
    return ascent + descent


def _text_width(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
) -> float:
    if hasattr(draw, "textlength"):
        return draw.textlength(text, font=font)
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0]


def ellipsize_line(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    max_width: int,
) -> str:
    text = text.rstrip()
    if _text_width(draw, text, font) <= max_width:
        return text

    trimmed = text
    while trimmed and _text_width(draw, trimmed + ELLIPSIS, font) > max_width:
        trimmed = trimmed[:-1].rstrip()
    return (trimmed + ELLIPSIS) if trimmed else ELLIPSIS


def wrap_text_lines(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    max_width: int,
) -> list[str]:
    words = text.split()
    if not words:
        return [""]

    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        if _text_width(draw, candidate, font) <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def fit_description_lines(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    max_width: int,
    max_height: int,
) -> list[str]:
    lines = wrap_text_lines(draw, text, font, max_width)
    line_height = _line_height(font)

    if len(lines) * line_height <= max_height:
        return lines

    max_lines = max(1, max_height // line_height)
    lines = lines[:max_lines]
    lines[-1] = ellipsize_line(draw, lines[-1], font, max_width)
    return lines


def draw_description(
    draw: ImageDraw.ImageDraw,
    text: str,
    xy: tuple[int, int],
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    color: tuple[int, int, int],
    max_width: int,
    max_height: int,
) -> None:
    x, y = xy
    line_height = _line_height(font)
    for line in fit_description_lines(draw, text, font, max_width, max_height):
        draw.text((x, y), line, font=font, fill=color)
        y += line_height


def render_selector_poster_layer(
    posters: list,
    *,
    poster_source: str = POSTER_SOURCE,
    perspective_supersample: int = POSTER_LAYER_SCALE,
) -> Image.Image:
    output_size = SELECTOR_OUTPUT_SIZE

    grid = make_background(
        output_size=output_size,
        poster_source=poster_source,
        sources=posters,
        perspective_supersample=max(1, perspective_supersample),
    )
    return apply_opacity_mask(grid, output_size=output_size)


def paste_layer_right_full_height(canvas: Image.Image, layer: Image.Image) -> None:
    canvas_w, canvas_h = canvas.size
    layer_w, layer_h = layer.size
    target_h = canvas_h
    target_w = max(1, round(layer_w * target_h / layer_h))
    if (target_w, target_h) != layer.size:
        layer = layer.resize((target_w, target_h), Image.Resampling.LANCZOS)

    x = canvas_w - target_w
    if layer.mode == "RGBA":
        canvas.paste(layer, (x, 0), layer)
    else:
        canvas.paste(layer, (x, 0))


def generate_selector_preview(
    name: str,
    description: str,
    posters: list,
    *,
    poster_source: str = POSTER_SOURCE,
) -> Image.Image:
    canvas = Image.new("RGB", CANVAS_SIZE, BG_COLOR)
    draw = ImageDraw.Draw(canvas)

    name_font = load_inter_semibold(NAME_FONT_SIZE)
    description_font = load_inter_regular(DESCRIPTION_FONT_SIZE)

    name_x = MARGIN_LEFT
    name_y = MARGIN_TOP
    draw.text((name_x, name_y), name, font=name_font, fill=NAME_COLOR)

    description_y = name_y + _line_height(name_font) + DESCRIPTION_GAP
    draw_description(
        draw,
        description,
        (name_x, description_y),
        description_font,
        DESCRIPTION_COLOR,
        DESCRIPTION_MAX_WIDTH,
        DESCRIPTION_MAX_HEIGHT,
    )

    logo = load_logo_rgba(LOGO_SIZE)
    if logo is not None:
        logo_x = MARGIN_LEFT
        logo_y = CANVAS_SIZE[1] - MARGIN_BOTTOM - LOGO_SIZE[1]
        canvas.paste(logo, (logo_x, logo_y), logo)

    poster_layer = render_selector_poster_layer(posters, poster_source=poster_source)
    paste_layer_right_full_height(canvas, poster_layer)
    return canvas


def generate_preview(
    mode: PreviewMode,
    name: str,
    description: str,
    posters: list,
    *,
    poster_source: str = POSTER_SOURCE,
) -> Image.Image:
    if mode == PreviewMode.SELECTOR:
        return generate_selector_preview(
            name,
            description,
            posters,
            poster_source=poster_source,
        )
    raise NotImplementedError(f"{mode.value} is not implemented yet")


if __name__ == "__main__":
    setup_logging()

    name = "Название селектора"
    description = (
        "Короткое описание селектора для превью. Текст автоматически переносится "
        "по ширине блока и обрезается многоточием, если не помещается по высоте."
    )
    posters = list_local_posters()

    image = generate_preview(
        PreviewMode.SELECTOR,
        name,
        description,
        posters,
        poster_source="local",
    )
    output_path = ROOT_DIR / "selector_preview.png"
    image.save(output_path)
    print(f"Saved -> {output_path.resolve()}")
