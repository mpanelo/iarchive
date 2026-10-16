"""Create fresh synthetic relationship fixtures; never accept an existing output tree."""
import argparse
from pathlib import Path
import struct
import sys

from PIL import Image
from pillow_heif import register_heif_opener

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from setup_metadata import safe_directory


def box(name, data):
    return struct.pack(">I", len(data) + 8) + name + data


def maker(content, image):
    values = [(17, content), (21, image)] if content else [(21, image)]
    data = b"Apple iOS\x00\x00\x01MM" + struct.pack(">H", len(values))
    offset = 14 + 2 + 12 * len(values) + 4
    tail = b""
    for tag, value in values:
        text = value.encode() + b"\x00"
        data += struct.pack(">HHII", tag, 2, len(text), offset + len(tail))
        tail += text
    return data + b"\x00" * 4 + tail


def movie(content):
    # One RGB24 frame in a QuickTime container, with an embedded content identifier.
    pixels = bytes([40, 80, 120]) * 16 * 16
    ftyp = box(b"ftyp", b"qt  " + b"\x00" * 4 + b"qt  ")
    mdat = box(b"mdat", pixels)
    matrix = struct.pack(">9I", 65536, 0, 0, 0, 65536, 0, 0, 0, 0x40000000)
    mvhd = box(b"mvhd", struct.pack(">6I", 0, 0, 0, 600, 600, 65536) + struct.pack(">H", 256) + b"\x00" * 10 + matrix + b"\x00" * 24 + struct.pack(">I", 2))
    tkhd = box(b"tkhd", struct.pack(">7I", 7, 0, 0, 1, 0, 600, 0) + b"\x00" * 12 + matrix + struct.pack(">2I", 16 << 16, 16 << 16))
    mdhd = box(b"mdhd", struct.pack(">5IHH", 0, 0, 0, 600, 600, 0, 0))
    hdlr = box(b"hdlr", struct.pack(">I", 0) + b"mhlrvide" + b"\x00" * 12 + b"Video\x00")
    sample = box(b"raw ", b"\x00" * 6 + struct.pack(">H", 1) + b"\x00" * 16 + struct.pack(">HHIIIH", 16, 16, 72 << 16, 72 << 16, 0, 1) + b"\x00" * 32 + struct.pack(">Hh", 24, -1))
    stsd = box(b"stsd", struct.pack(">II", 0, 1) + sample)
    stts = box(b"stts", struct.pack(">4I", 0, 1, 1, 600))
    stsc = box(b"stsc", struct.pack(">5I", 0, 1, 1, 1, 1))
    stsz = box(b"stsz", struct.pack(">3I", 0, len(pixels), 1))
    stco = box(b"stco", struct.pack(">3I", 0, 1, len(ftyp) + 8))
    url = box(b"url ", struct.pack(">I", 1))
    dinf = box(b"dinf", box(b"dref", struct.pack(">II", 0, 1) + url))
    minf = box(b"minf", box(b"vmhd", struct.pack(">I4H", 1, 0, 0, 0, 0)) + dinf + box(b"stbl", stsd + stts + stsc + stsz + stco))
    trak = box(b"trak", tkhd + box(b"mdia", mdhd + hdlr + minf))
    key = box(b"mdta", b"com.apple.quicktime.content.identifier")
    keys = box(b"keys", struct.pack(">II", 0, 1) + key)
    ilst = box(b"ilst", box(struct.pack(">I", 1), box(b"data", struct.pack(">II", 1, 0) + content.encode())))
    meta = box(b"meta", box(b"hdlr", b"\x00" * 8 + b"mdta" + b"\x00" * 12) + keys + ilst)
    return ftyp + mdat + box(b"moov", mvhd + trak + meta)


def generate(output):
    output = safe_directory(output)
    output.mkdir(parents=True, exist_ok=False)
    register_heif_opener()
    for name, content, identity in [
        ("live.heic", "synthetic-live-content", "synthetic-live-image"),
        ("portrait.jpg", None, "synthetic-portrait-image"),
    ]:
        exif = Image.Exif()
        exif[271] = "Apple"
        exif[34665] = {37500: maker(content, identity)}
        Image.new("RGB", (64,64), (0,0,0)).save(output / name, format="HEIF" if name.endswith("heic") else "JPEG", exif=exif)
    (output / "unrelated-name.mov").write_bytes(movie("synthetic-live-content"))
    # XMP explicitly identifies the original document and an adjusted descendant.
    for name, identifier, derived in [("original.jpg", "xmp.did:synthetic-original", None),
                                      ("cropped.jpg", "xmp.did:synthetic-crop", "xmp.did:synthetic-original")]:
        Image.new("RGB", (64,64), (0,0,0)).save(output / name)
        xmp = ('<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
               '<rdf:Description rdf:about="" xmlns:xmpMM="http://ns.adobe.com/xap/1.0/mm/" xmlns:stRef="http://ns.adobe.com/xap/1.0/sType/ResourceRef#" '
               f'xmpMM:DocumentID="{identifier}">')
        if derived:
            xmp += f'<xmpMM:DerivedFrom rdf:parseType="Resource"><stRef:documentID>{derived}</stRef:documentID></xmpMM:DerivedFrom>'
        xmp += '</rdf:Description></rdf:RDF></x:xmpmeta>'
        path = output / name
        jpeg = path.read_bytes()
        payload = b"http://ns.adobe.com/xap/1.0/\x00" + xmp.encode()
        path.write_bytes(jpeg[:2] + b"\xff\xe1" + struct.pack(">H",len(payload)+2) + payload + jpeg[2:])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    generate(parser.parse_args().output)
