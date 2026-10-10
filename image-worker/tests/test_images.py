import hashlib
from pathlib import Path

import pytest
from PIL import Image, ImageChops, ImageDraw, ImageStat
from pillow_heif import register_heif_opener

from iarchive_worker.__main__ import handle
from iarchive_worker.decoder import decode
from iarchive_worker.features import measure

register_heif_opener(thumbnails=False)


def request(root, cache, name, file_id=1):
    return {"protocol_version": 1, "request_id": str(file_id), "file_id": file_id, "archive_root": str(root.resolve()), "input_path": str((root / name).resolve()), "cache_root": str(cache.resolve()), "algorithms": ["phash", "dark", "sharpness", "thumbnail"], "max_preview_pixels": 128}


def scene(size=(160, 100)):
    image = Image.new("RGB", size, (10, 30, 80))
    draw = ImageDraw.Draw(image)
    draw.rectangle((15, 10, 60, 80), fill=(160, 200, 30))
    draw.ellipse((80, 20, 140, 70), fill=(240, 80, 120))
    draw.line((5, 95, 150, 5), fill="white", width=5)
    return image


@pytest.mark.parametrize("extension,format", [("JPEG", "JPEG"), ("png", "PNG"), ("heic", "HEIF")])
def test_decode_preview_phash_and_original_preserved(tmp_path, extension, format):
    root = tmp_path / "photos 旅行😀"
    root.mkdir()
    path = root / f"photo.{extension}"
    scene().save(path, format=format)
    before = (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
    features = handle(request(root, tmp_path / "cache", path.name))
    assert len(features["phash"]) == 16
    assert features["width"] == 160
    assert Path(features["thumbnail"]).is_file()
    with Image.open(features["thumbnail"]) as preview:
        assert max(preview.size) <= 128
    assert (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns) == before
    assert len(list(root.iterdir())) == 1


@pytest.mark.parametrize("format,extension", [("JPEG", "jpg"), ("HEIF", "heic")])
@pytest.mark.parametrize("orientation", range(1, 9))
def test_exif_orientation(tmp_path, format, extension, orientation):
    image = scene((160, 100))
    exif = Image.Exif()
    exif[274] = orientation
    path = tmp_path / f"rotated.{extension}"
    # HEIF needs serialized EXIF to encode the container's rotation property.
    image.save(path, format=format, exif=exif.tobytes(), quality=95)
    before = (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
    transformations = {
        2: Image.Transpose.FLIP_LEFT_RIGHT,
        3: Image.Transpose.ROTATE_180,
        4: Image.Transpose.FLIP_TOP_BOTTOM,
        5: Image.Transpose.TRANSPOSE,
        6: Image.Transpose.ROTATE_270,
        7: Image.Transpose.TRANSVERSE,
        8: Image.Transpose.ROTATE_90,
    }
    expected = image.transpose(transformations[orientation]) if orientation != 1 else image
    corrected = decode(path)
    assert corrected.size == expected.size
    difference = ImageStat.Stat(ImageChops.difference(corrected, expected)).mean
    assert max(difference) < 12
    assert (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns) == before
    corrected.close()


def test_decompression_pixel_limit(tmp_path, monkeypatch):
    import iarchive_worker.decoder as decoder
    path=tmp_path / "large.png"
    scene().save(path)
    monkeypatch.setattr(decoder, "MAX_IMAGE_PIXELS", 100)
    with pytest.raises(ValueError, match="pixel limit"):
        decoder.decode(path)


def test_animation_does_not_claim_whole_file_audited(tmp_path):
    path=tmp_path / "animated.gif"
    first=Image.new("RGB", (32,32), "black")
    second=Image.new("RGB", (32,32), "white")
    first.save(path, save_all=True, append_images=[second], duration=100, loop=0)
    with pytest.raises(ValueError, match="multi-frame"):
        decode(path)


def test_near_duplicates_and_dark_review_only(tmp_path):
    image = scene()
    a = tmp_path / "original.jpg"
    b = tmp_path / "smaller.jpg"
    image.save(a, quality=95)
    image.resize((120, 75)).save(b, quality=80)
    first, second = measure(decode(a)), measure(decode(b))
    distance = (int(first["phash"], 16) ^ int(second["phash"], 16)).bit_count()
    assert distance <= 6
    black = measure(Image.new("RGB", (128, 128), "black"))
    assert black["dark_candidate_score"] >= 0.8
    assert "review only" in black["dark_reason"]
    night = Image.new("RGB", (128, 128), (3, 3, 8))
    draw = ImageDraw.Draw(night)
    for x in range(4, 128, 16):
        draw.ellipse((x, 16, x + 3, 19), fill=(180, 180, 180))
    meaningful = measure(night)
    assert "review only" in meaningful["dark_reason"]
    assert "delete" not in meaningful


def test_corrupt_heic_is_not_black_candidate(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "bad.heic").write_bytes(b"not a valid HEIC")
    with pytest.raises(Exception):
        handle(request(root, tmp_path / "cache", "bad.heic"))
    assert not list((tmp_path / "cache").glob("*.jpg"))


def test_symlink_input_is_refused(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    outside = tmp_path / "outside.png"
    scene().save(outside)
    link = root / "link.png"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("Windows symlink privilege unavailable; junction test runs in Rust")
    req = request(root, tmp_path / "cache", "link.png")
    req["input_path"] = str(link.absolute())
    with pytest.raises(ValueError):
        handle(req)
