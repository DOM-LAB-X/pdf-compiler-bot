"""Photos -> letter-size PDF. Python port of photos_to_pdf.html (no Discord code in here).
Works entirely on in-memory bytes; nothing is written to disk."""
import io
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PIL import ExifTags, Image, ImageDraw, ImageFont, ImageOps
from reportlab import rl_config
from reportlab.lib.pagesizes import landscape, letter
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

try:
    import pillow_heif
    pillow_heif.register_heif_opener()  # HEIC / HEIF
except ImportError:
    pass

try:
    import pytesseract
    # Windows: Tesseract's installer doesn't add itself to PATH
    _win_tess = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")
    if _win_tess.exists():
        pytesseract.pytesseract.tesseract_cmd = str(_win_tess)
except ImportError:
    pytesseract = None

Image.MAX_IMAGE_PIXELS = 200_000_000  # allow big panoramas / scans
rl_config.useA85 = 0  # embed JPEGs raw instead of ASCII85 (+25% size)

# ─────────────────────────────────────────────
#   Options (same presets as the HTML version)
# ─────────────────────────────────────────────
SIZE_PRESETS = {
    "small":    (1200, 45),   # max px, JPEG quality
    "balanced": (1800, 75),
    "original": (None, 95),
}
SIZE_ORDER = ["original", "balanced", "small"]
COLORS = {"red": "#cc0000", "white": "#ffffff", "black": "#111111", "yellow": "#ffdd00"}
POSITIONS = ("tl", "tr", "ml", "mr", "bl", "br")
MARGIN_PT = 18  # 0.25 in, used by "fit"


@dataclass
class Options:
    orientation: str = "portrait"   # portrait | landscape
    fit: str = "fit"                # fit | fill | stretch
    size: str = "balanced"          # small | balanced | original
    label: str = ""                 # lines separated by "|" or newlines
    label_position: str = "tl"
    label_color: str = "red"
    label_font: str = "sans"        # sans | serif


# ─────────────────────────────────────────────
#   Supported formats
# ─────────────────────────────────────────────
IMAGE_EXTS = re.compile(r"\.(jpe?g|jfif|png|gif|webp|bmp|tiff?|avif|heic|heif|ico|tga|ppm|pgm)$", re.I)


def looks_like_image(filename: str, content_type: str | None) -> bool:
    return bool((content_type or "").startswith("image/") or IMAGE_EXTS.search(filename or ""))


def can_open(data: bytes) -> bool:
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.size
        return True
    except Exception:
        return False


def open_rgb(data: bytes) -> Image.Image:
    """Open any supported image upright (EXIF rotation applied), flattened onto white."""
    with Image.open(io.BytesIO(data)) as im:
        im.seek(0)  # first frame of GIF / multi-page TIFF
        im = ImageOps.exif_transpose(im)
        if im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info):
            im = im.convert("RGBA")
            bg = Image.new("RGB", im.size, "white")
            bg.paste(im, mask=im.getchannel("A"))
            return bg
        return im.convert("RGB")


# ─────────────────────────────────────────────
#   Dates: Timemark OCR -> EXIF -> fallback
# ─────────────────────────────────────────────
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def ocr_available() -> bool:
    if pytesseract is None:
        return False
    try:
        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False


def _parse_date(t: str) -> datetime | None:
    hour = minute = 0
    mt = re.search(r"\b(\d{1,2}):(\d{2})(?:\s*(AM|PM)\b)?", t, re.I)
    if mt:
        hour, minute = int(mt[1]), int(mt[2])
        if mt[3]:
            hour = hour % 12 + (12 if mt[3].upper() == "PM" else 0)

    ma = re.search(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})\b", t, re.I)
    if ma:
        y, mo, d = int(ma[3]), MONTHS[ma[1].lower()], int(ma[2])
    else:
        mb = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b", t)
        if not mb:
            return None
        mo, d, y = int(mb[1]), int(mb[2]), int(mb[3])
        if y < 100:
            y += 2000
    try:
        return datetime(y, mo, d, hour if hour < 24 else 0, minute if minute < 60 else 0)
    except ValueError:
        return None


def parse_timemark_text(text: str) -> datetime | None:
    # Try the raw OCR text, then with common OCR mix-ups fixed (l/I/| vs 1, O vs 0)
    letters = re.sub(r"(?<=[A-Za-z])1", "l", re.sub(r"(?<=[A-Za-z])0|\b0(?=ct)", "o", text))
    digits = re.sub(r"(?<=\d)[|Il]|[|Il](?=\d)", "1", re.sub(r"(?<=\d)O|O(?=\d)", "0", letters))
    return _parse_date(digits) or _parse_date(text)


def timemark_date(im: Image.Image) -> datetime | None:
    """Timemark puts white text in a dark box bottom-left: crop it, upscale, threshold, OCR."""
    if pytesseract is None:
        return None
    w, h = im.size
    crop = im.crop((0, int(h * 0.55), int(w * 0.60), h))
    scale = min(2.0, 2400 / max(crop.width, 1))  # keep it light enough for a Pi
    crop = crop.resize((max(1, int(crop.width * scale)), max(1, int(crop.height * scale))))
    bw = crop.convert("L").point(lambda v: 0 if v > 150 else 255)  # light text -> black on white
    try:
        return parse_timemark_text(pytesseract.image_to_string(bw))
    except Exception:
        return None


def exif_date(data: bytes) -> datetime | None:
    try:
        with Image.open(io.BytesIO(data)) as im:
            exif = im.getexif()
            ifd = exif.get_ifd(ExifTags.IFD.Exif)
            raw = ifd.get(36867) or ifd.get(36868) or exif.get(306)  # DateTimeOriginal, Digitized, DateTime
            if raw:
                return datetime.strptime(str(raw).strip()[:19], "%Y:%m:%d %H:%M:%S")
    except Exception:
        pass
    return None


def find_date(data: bytes, use_ocr: bool = True) -> tuple[datetime | None, str | None]:
    """Returns (date, source) where source is 'timemark', 'exif' or None."""
    if use_ocr:
        try:
            d = timemark_date(open_rgb(data))
            if d:
                return d, "timemark"
        except Exception:
            pass
    d = exif_date(data)
    return (d, "exif") if d else (None, None)


# ─────────────────────────────────────────────
#   Label / watermark
# ─────────────────────────────────────────────
FONT_CANDIDATES = {
    "sans": [
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",   # Arial-metric clone (Debian/Pi)
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",              # macOS
        "C:/Windows/Fonts/arialbd.ttf",
    ],
    "serif": [
        "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf",  # Times-metric clone
        "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
        "/System/Library/Fonts/Supplemental/Times New Roman Bold.ttf",
        "C:/Windows/Fonts/timesbd.ttf",
    ],
}


def _font(kind: str, size: int):
    for p in FONT_CANDIDATES.get(kind, FONT_CANDIDATES["sans"]):
        if Path(p).exists():
            return ImageFont.truetype(p, size)
    return ImageFont.load_default(size)


def label_lines(text: str) -> list[str]:
    return [l.strip() for l in re.split(r"\||\\n|\n", text or "") if l.strip()]


def burn_label(im: Image.Image, lines: list[str], position: str, color: str, font_kind: str) -> Image.Image:
    if not lines:
        return im
    W, H = im.size
    font_size = max(16, round(W * 0.024))
    font = _font(font_kind, font_size)
    draw = ImageDraw.Draw(im)

    lh = round(font_size * 1.3)
    pad = round(font_size * 0.5)
    box_w = max(draw.textlength(l, font=font) for l in lines) + pad * 2
    box_h = len(lines) * lh + pad * 1.2
    margin = round(W * 0.022)

    bx = margin if position in ("tl", "ml", "bl") else W - box_w - margin
    by = {"t": margin, "m": (H - box_h) / 2, "b": H - box_h - margin}[position[0]]

    overlay = Image.new("RGBA", im.size, (0, 0, 0, 0))
    ImageDraw.Draw(overlay).rounded_rectangle(
        (bx, by, bx + box_w, by + box_h), radius=round(font_size * 0.25), fill=(255, 255, 255, 235))
    im = Image.alpha_composite(im.convert("RGBA"), overlay).convert("RGB")

    draw = ImageDraw.Draw(im)
    fill = COLORS.get(color, color)
    cx = bx + box_w / 2
    for i, line in enumerate(lines):
        draw.text((cx, by + pad + (i + 1) * lh - lh * 0.15), line, font=font, fill=fill, anchor="ms")
    return im


# ─────────────────────────────────────────────
#   Pages + PDF
# ─────────────────────────────────────────────
@dataclass
class Page:
    jpeg: bytes
    width: int
    height: int


def render_page(data: bytes, opts: Options) -> Page:
    max_dim, quality = SIZE_PRESETS[opts.size]
    im = open_rgb(data)
    if max_dim and max(im.size) > max_dim:
        im.thumbnail((max_dim, max_dim), Image.LANCZOS)
    im = burn_label(im, label_lines(opts.label), opts.label_position, opts.label_color, opts.label_font)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=quality, optimize=True)
    return Page(buf.getvalue(), im.width, im.height)


def pages_to_pdf(pages: list[Page], opts: Options, title: str = "photos") -> bytes:
    page_w, page_h = landscape(letter) if opts.orientation == "landscape" else letter
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(page_w, page_h))
    c.setTitle(title)
    for pg in pages:
        ratio = pg.width / pg.height
        if opts.fit == "fill":
            if ratio > page_w / page_h:
                dh, dw = page_h, page_h * ratio
            else:
                dw, dh = page_w, page_w / ratio
        elif opts.fit == "stretch":
            dw, dh = page_w, page_h
        else:
            s = min((page_w - 2 * MARGIN_PT) / pg.width, (page_h - 2 * MARGIN_PT) / pg.height)
            dw, dh = pg.width * s, pg.height * s
        # JPEG bytes are embedded as-is (no re-encode)
        c.drawImage(ImageReader(io.BytesIO(pg.jpeg)), (page_w - dw) / 2, (page_h - dh) / 2, dw, dh)
        c.showPage()
    c.save()
    return buf.getvalue()


def safe_filename(name: str) -> str:
    name = re.sub(r"\.pdf$", "", (name or "").strip(), flags=re.I)
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name)
    return (name or "photos")[:100] + ".pdf"
