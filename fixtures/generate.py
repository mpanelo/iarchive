"""Generate tiny synthetic fixtures using only Python's standard library.

Never overwrites an existing output directory. No personal media is read.
"""
import argparse
from pathlib import Path
import struct
import zlib


def chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def png(width, height, pixel, compression=6):
    rows = bytearray()
    for y in range(height):
        rows.append(0)
        for x in range(width):
            rows.extend(pixel(x, y))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(rows, compression)) + chunk(b"IEND", b"")


def generate(output, clean=False, heic=False):
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)

    def scene(x, y):
        if 10 < x < 50 and 10 < y < 70:
            return 160, 200, 30
        if (x - 100) ** 2 + (y - 45) ** 2 < 400:
            return 240, 80, 120
        return (x * 2) % 100, (y * 3) % 100, 80

    original = png(160, 100, scene)
    (output / "scene.PNG").write_bytes(original)
    (output / "exact copy 旅行😀.png").write_bytes(original)
    (output / "recompressed.png").write_bytes(png(160, 100, scene, compression=1))
    (output / "black.png").write_bytes(png(128, 128, lambda x, y: (0, 0, 0)))
    (output / "night.png").write_bytes(png(128, 128, lambda x, y: (200, 200, 160) if x % 16 < 3 and 16 < y < 20 else (3, 3, 8)))
    (output / "nested").mkdir()
    (output / "nested" / "scene.PNG").write_bytes(png(64, 64, lambda x, y: (x * 3, y * 3, 120)))
    if heic:
        from PIL import Image
        from pillow_heif import register_heif_opener
        register_heif_opener()
        with Image.open(output / "scene.PNG") as image:
            image.save(output / "scene.heic", format="HEIF")
            image.save(output / "scene.jpg", quality=90)
            exif = Image.Exif()
            exif[274] = 6
            image.save(output / "rotated.heic", format="HEIF", exif=exif.tobytes())
    if not clean:
        (output / "corrupted.heic").write_bytes(b"intentionally invalid HEIC")
        (output / "unsupported.dng").write_bytes(b"synthetic unsupported RAW")
        (output / "scene.MOV").write_bytes(b"synthetic video bytes; not a playable MOV")
        (output / "scene.xmp").write_text("<xmp>synthetic</xmp>", encoding="utf-8")
        (output / "empty.jpg").touch()
        (output / "copy.partial").write_bytes(b"interrupted copy")
        (output / "unknown.xyz").write_bytes(b"unknown bytes still belong in raw backup")
    print(f"Synthetic fixtures: {output}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--clean", action="store_true", help="Only valid images; suitable for a successful visual smoke test")
    parser.add_argument("--with-heic", action="store_true", help="Requires the uv worker environment; adds synthetic HEIC/JPEG and rotated HEIC")
    args = parser.parse_args()
    generate(args.output, args.clean, args.with_heic)
