"""Only previews are transformed; original bytes are never written."""

import warnings
from PIL import Image, ImageOps, ImageFile
from pillow_heif import register_heif_opener

MAX_IMAGE_PIXELS = 64_000_000
Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
ImageFile.LOAD_TRUNCATED_IMAGES = False
register_heif_opener(thumbnails=False)


def decode(path):
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(path) as source:
            if getattr(source, "n_frames", 1) > 1:
                raise ValueError("multi-frame image is not fully visually audited")
            if source.width * source.height > MAX_IMAGE_PIXELS:
                raise ValueError("image exceeds safe decode pixel limit")
            source.load()
            oriented = ImageOps.exif_transpose(source)
            # Transparent pixels preview against white, rather than generating spurious black candidates.
            if oriented.mode in {"RGBA", "LA"} or "transparency" in oriented.info:
                rgba = oriented.convert("RGBA")
                rgb = Image.new("RGB", rgba.size, "white")
                rgb.paste(rgba, mask=rgba.getchannel("A"))
                return rgb
            return oriented.convert("RGB")
