"""Fetch a pinned portable metadata reader into the checkout, without system installation."""
import hashlib
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tarfile
import tempfile
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
VERSION = "13.59"
DOWNLOADS = {
    "posix": ("https://codeload.github.com/exiftool/exiftool/tar.gz/refs/tags/13.59",
              "87d3317882fdae9cb4dcfe57a96a378d0132ffc02c731315bf128b19ddcf7aac"),
    "nt": ("https://downloads.sourceforge.net/project/exiftool/exiftool-13.59_64.zip",
           "44b512b25af500724ba579d0a53c8fc5851628b692dd5e5d94ae4a15c2cba9ec"),
}


def safe_directory(path):
    path = Path(os.path.abspath(path))
    for part in (*reversed(path.parents), path):
        if part.exists() or part.is_symlink():
            info = part.lstat()
            if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("Linked or non-directory metadata tool destination")
    return path


def member_path(name):
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name or len(path.parts) < 2:
        raise ValueError("Unsafe tool archive member")
    return Path(*path.parts[1:])


def regular_file(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISREG(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400


def unpack(archive, unpacked, platform):
    total = 0
    if platform == "nt":
        with zipfile.ZipFile(archive) as package:
            for item in package.infolist():
                if item.is_dir() and len(PurePosixPath(item.filename).parts) == 1:
                    continue
                relative = member_path(item.filename)
                if stat.S_ISLNK(item.external_attr >> 16):
                    raise ValueError("Linked metadata archive member")
                total += item.file_size
                if total > 128 * 1024 * 1024:
                    raise ValueError("Metadata archive exceeds extraction limit")
                destination = unpacked / relative
                if item.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                else:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with package.open(item) as source, destination.open("xb") as out:
                        shutil.copyfileobj(source, out)
        (unpacked / "exiftool(-k).exe").rename(unpacked / "exiftool.exe")
    else:
        with tarfile.open(archive) as package:
            for item in package:
                if item.isdir() and len(PurePosixPath(item.name).parts) == 1:
                    continue
                relative = member_path(item.name)
                if not (item.isdir() or item.isfile()):
                    raise ValueError("Linked or special metadata archive member")
                total += item.size
                if total > 128 * 1024 * 1024:
                    raise ValueError("Metadata archive exceeds extraction limit")
                destination = unpacked / relative
                if item.isdir():
                    destination.mkdir(parents=True, exist_ok=True)
                else:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with package.extractfile(item) as source, destination.open("xb") as out:
                        shutil.copyfileobj(source, out)


def enable_execution(path):
    if os.name == "posix":
        # Grant owner execution without restoring permissions from the archive.
        path.chmod(path.stat().st_mode | stat.S_IXUSR)


def setup():
    parent = safe_directory(ROOT / (".windows-sandbox" if os.name == "nt" else ".tools"))
    target = parent / "exiftool"
    exe = target / ("exiftool.exe" if os.name == "nt" else "exiftool")
    marker = target / "iarchive-version.txt"
    if target.exists():
        safe_directory(target)
        if regular_file(exe) and regular_file(marker) and marker.read_text() == VERSION:
            enable_execution(exe)
            print(f"Metadata reader ready: {exe}")
            return
        raise ValueError("Existing metadata tool directory is incomplete; inspect it before retrying")
    parent.mkdir(exist_ok=True)
    url, expected = DOWNLOADS[os.name]
    with tempfile.TemporaryDirectory(prefix="metadata-", dir=parent) as temporary:
        stage = Path(temporary)
        archive = stage / "download"
        with urllib.request.urlopen(url, timeout=60) as response, archive.open("xb") as stream:
            total = 0
            while data := response.read(1024 * 1024):
                total += len(data)
                if total > 32 * 1024 * 1024:
                    raise ValueError("Metadata distribution exceeds download limit")
                stream.write(data)
        if hashlib.sha256(archive.read_bytes()).hexdigest() != expected:
            raise ValueError("Metadata distribution checksum mismatch")
        unpacked = stage / "unpacked"
        unpacked.mkdir()
        unpack(archive, unpacked, os.name)
        if not regular_file(unpacked / exe.name):
            raise ValueError("Metadata archive has no executable")
        enable_execution(unpacked / exe.name)
        (unpacked / "iarchive-version.txt").write_text(VERSION)
        unpacked.rename(target)
    print(f"Metadata reader ready: {exe}")


if __name__ == "__main__":
    setup()
