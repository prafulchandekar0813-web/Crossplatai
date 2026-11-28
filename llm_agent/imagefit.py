"""Pictures people upload, made to fit what the system needs instead of being refused.

Any ordinary photo (PNG, JPG, WEBP, GIF; up to MAX_INPUT) is accepted. The result is re-encoded from the pixels, which also drops
hidden data such as the GPS position a phone puts in a photo. Phone photos that are stored sideways are turned upright.
"""
import io
from typing import Tuple

from PIL import Image, ImageOps, UnidentifiedImageError

MB = 1024 * 1024
MAX_INPUT = 10 * MB
MAX_PIXELS = 50_000_000          # decompression-bomb guard: a small file can claim to be a huge picture
MIN_SIDE = 32
ITEM_EDGE = 1200                 # longest side of a stored product / design picture
MAX_RATIO = 3.0                  # a strip longer than 3:1 (either way) is cropped to 3:1 so it cannot break a layout
LOGO_SIZE = 512
FORMATS = {"PNG", "JPEG", "WEBP", "GIF"}


class ImageError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message, self.status = message, status


def _open(data: bytes) -> Image.Image:
    if len(data) > MAX_INPUT:
        raise ImageError(f"That picture is {len(data) // MB} MB. Please use one under {MAX_INPUT // MB} MB.", 413)
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    try:
        img = Image.open(io.BytesIO(data))
        if img.format not in FORMATS:
            raise ImageError("Pictures must be PNG, JPG, WEBP or GIF.")
        img.load()                                   # first frame of an animated GIF
        img = ImageOps.exif_transpose(img)
    except ImageError:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError, SyntaxError):
        raise ImageError("That file could not be read as a picture. Use a PNG, JPG, WEBP or GIF.")
    if min(img.size) < MIN_SIDE:
        raise ImageError(f"That picture is only {img.width} × {img.height} pixels. Please use one at least {MIN_SIDE} pixels on each side.")
    return img


def _mode(img: Image.Image) -> Image.Image:
    return img.convert("RGBA" if "A" in img.getbands() or "transparency" in img.info or img.mode == "P" else "RGB")


def _crop_to_ratio(img: Image.Image) -> Image.Image:
    w, h = img.size
    if w > h * MAX_RATIO:
        left = (w - int(h * MAX_RATIO)) // 2
        return img.crop((left, 0, left + int(h * MAX_RATIO), h))
    if h > w * MAX_RATIO:
        top = (h - int(w * MAX_RATIO)) // 2
        return img.crop((0, top, w, top + int(w * MAX_RATIO)))
    return img


def photo(data: bytes) -> Tuple[bytes, str]:
    """A product / design picture: upright, no longer than ITEM_EDGE on its long side, strips trimmed to 3:1, saved as WEBP."""
    img = _crop_to_ratio(_mode(_open(data)))
    img.thumbnail((ITEM_EDGE, ITEM_EDGE), Image.LANCZOS)             # only ever shrinks
    out = io.BytesIO()
    img.save(out, "WEBP", quality=82, method=4)
    return out.getvalue(), "webp"


def logo(data: bytes, whole: bool = False, size: int = LOGO_SIZE) -> bytes:
    """An app logo: a square PNG. By default the middle of the picture is cropped to a square; whole=True keeps the entire picture
    and pads it (transparent if the picture has transparency, white otherwise)."""
    img = _mode(_open(data))
    if not whole:
        img = ImageOps.fit(img, (size, size), Image.LANCZOS, centering=(0.5, 0.5))
    else:
        img.thumbnail((size, size), Image.LANCZOS)
        pad = (255, 255, 255, 0) if img.mode == "RGBA" else (255, 255, 255)
        canvas = Image.new(img.mode, (size, size), pad)
        canvas.paste(img, ((size - img.width) // 2, (size - img.height) // 2))
        img = canvas
    out = io.BytesIO()
    img.save(out, "PNG", optimize=True)
    return out.getvalue()
