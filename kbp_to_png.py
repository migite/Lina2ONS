"""Convert Sigma KBP1 images (*.kbp) to PNG (RGBA).

KBP1 layout (little endian): "KBP1", u32 0, u32 width, u32 height, u32 header size (0x14),
followed by width*height*4 bytes of BGRA pixels, top row first.

Usage: python kbp_to_png.py <input_dir_or_file> <output_dir>
"""
import os
import struct
import sys
import zlib


def read_kbp(path):
    with open(path, "rb") as f:
        b = f.read()
    if b[:4] != b"KBP1":
        raise ValueError("not KBP1: " + path)
    w, h, hdr = struct.unpack_from("<3I", b, 8)
    px = b[hdr:hdr + w * h * 4]
    if len(px) != w * h * 4:
        raise ValueError("truncated: " + path)
    return w, h, px


def bgra_to_rgba(px):
    out = bytearray(px)
    out[0::4] = px[2::4]
    out[2::4] = px[0::4]
    alpha = px[3::4]
    if max(alpha, default=0) <= 63:
        # the game stores a 6-bit alpha (0-63)
        out[3::4] = bytes((a * 255 + 31) // 63 for a in alpha)
    return bytes(out)


def write_png(path, w, h, rgba):
    raw = bytearray()
    stride = w * 4
    for y in range(h):
        raw.append(0)
        raw += rgba[y * stride:(y + 1) * stride]

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        f.write(chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)))
        f.write(chunk(b"IDAT", zlib.compress(bytes(raw), 6)))
        f.write(chunk(b"IEND", b""))


def convert(src, dst):
    w, h, px = read_kbp(src)
    write_png(dst, w, h, bgra_to_rgba(px))
    return w, h


def main():
    if len(sys.argv) != 3:
        sys.exit("usage: python kbp_to_png.py <input_dir_or_file> <output_dir>")
    src, out = sys.argv[1:3]
    os.makedirs(out, exist_ok=True)
    files = [src] if os.path.isfile(src) else [os.path.join(src, n) for n in sorted(os.listdir(src))
                                              if n.lower().endswith(".kbp")]
    for p in files:
        name = os.path.splitext(os.path.basename(p))[0] + ".png"
        w, h = convert(p, os.path.join(out, name))
        print("%s %dx%d" % (name, w, h))


if __name__ == "__main__":
    main()
