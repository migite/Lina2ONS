#!/usr/bin/env python3
"""Build an ONScripter (default) or Ponscripter game folder from the extracted data.

Usage: python convert_to_ons.py [--engine onscripter|ponscripter] [--root DIR] [--out DIR]
                                [--encoding cp932|utf-8] [--no-assets] [--copy]

  --engine        target engine (default: onscripter, the original ogapee interpreter)
  --root DIR      game folder that contains unpacked\\ (default: the folder above Converter)
  --out DIR       output folder (default: <root>\\onscripter or <root>\\ponscripter)
  --encoding      script encoding (default: cp932 / Shift-JIS for onscripter, utf-8 for ponscripter)
  --no-assets     only write 0.txt
  --copy          copy the assets instead of making hard links (links need the same drive)

Output
  0.txt                    the converted game script (see svs_to_ons.py)
  *.bmp                    background / event pictures that the script uses
  t_*.png window.png       sprites and message window converted from .kbp (kbp_to_png.py)
  sel\\sel_NN.png          choice buttons cut out of select_mes.kbp (normal | highlighted)
  music\\*.ogg voice\\*.ogg wav\\*.wav   sounds that the script uses

The engine also needs a font named default.ttf in the same folder (not included).
"""
import argparse
import os
import re
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import kbp_to_png  # noqa: E402
import svs_to_ons  # noqa: E402

ROOT = os.path.dirname(HERE)
WINDOW_SIZE = (768, 200)        # the message window part of window.kbp (buttons lie below it)
SEL_CELL = (204, 34)            # one choice label in select_mes.kbp, 22 rows per column pair


def link_or_copy(src, dst, copy):
    if os.path.exists(dst):
        if os.path.getsize(dst) == os.path.getsize(src):
            return
        os.remove(dst)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if not copy:
        try:
            os.link(src, dst)
            return
        except OSError:
            pass
    shutil.copy2(src, dst)


def crop(w, px, x, y, cw, ch):
    out = bytearray()
    for r in range(y, y + ch):
        out += px[(r * w + x) * 4:(r * w + x + cw) * 4]
    return bytes(out)


def convert_assets(text, unpacked, out, copy):
    missing = []
    dat = os.path.join(unpacked, 'dat')

    def need(src, dst):
        if os.path.exists(src):
            link_or_copy(src, os.path.join(out, dst), copy)
        else:
            missing.append(src)

    names = set(re.findall(r'"(?::[^";]*;)?([^"\\:;]+\.(?:bmp|png))"', text))
    for n in sorted(names):
        if n.lower().endswith('.bmp'):
            need(os.path.join(dat, n), n)
    for n in sorted(names):
        if n.startswith('t_') and n.endswith('.png'):
            src = os.path.join(dat, n[:-4] + '.kbp')
            if os.path.exists(src):
                kbp_to_png.convert(src, os.path.join(out, n))
            else:
                missing.append(src)

    w, h, px = kbp_to_png.read_kbp(os.path.join(dat, 'window.kbp'))
    rgba = kbp_to_png.bgra_to_rgba(px)
    kbp_to_png.write_png(os.path.join(out, 'window.png'), WINDOW_SIZE[0], WINDOW_SIZE[1],
                         crop(w, rgba, 0, 0, WINDOW_SIZE[0], WINDOW_SIZE[1]))

    w, h, px = kbp_to_png.read_kbp(os.path.join(dat, 'select_mes.kbp'))
    rgba = kbp_to_png.bgra_to_rgba(px)
    cw, ch = SEL_CELL
    os.makedirs(os.path.join(out, 'sel'), exist_ok=True)
    for k in range(44):
        g, row = divmod(k, 22)
        kbp_to_png.write_png(os.path.join(out, 'sel', 'sel_%02d.png' % k), cw * 2, ch,
                             crop(w, rgba, g * cw * 2, row * ch, cw * 2, ch))

    for folder, src_dir, pattern in (('voice', 'voice_ogg', r'voice\\([^"]+)"'),
                                     ('music', 'music_ogg', r'music\\([^"]+)"'),
                                     ('wav', 'wav', r'wav\\([^"]+)"')):
        for n in sorted(set(re.findall(pattern, text))):
            need(os.path.join(unpacked, src_dir, n), os.path.join(folder, n))
    return missing


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--root', default=ROOT)
    ap.add_argument('--engine', choices=('onscripter', 'ponscripter'), default='onscripter')
    ap.add_argument('--out')
    ap.add_argument('--encoding', help='script encoding (default: cp932 for onscripter, utf-8 for ponscripter)')
    ap.add_argument('--no-assets', action='store_true')
    ap.add_argument('--copy', action='store_true')
    a = ap.parse_args()

    unpacked = os.path.join(a.root, 'unpacked')
    encoding = a.encoding or ('cp932' if a.engine == 'onscripter' else 'utf-8')
    out = a.out or os.path.join(a.root, a.engine)
    os.makedirs(out, exist_ok=True)

    def listing(folder, strip_ext):
        d = os.path.join(unpacked, folder)
        if not os.path.isdir(d):
            return None
        return set((os.path.splitext(n)[0] if strip_ext else n).lower() for n in os.listdir(d))

    text, warnings, _ = svs_to_ons.build(os.path.join(unpacked, 'script_decoded'),
                                         voices=listing('voice_ogg', True), sounds=listing('wav', False))
    try:
        data = text.replace('\n', '\r\n').encode(encoding)
    except UnicodeEncodeError as ex:
        sys.exit('cannot encode the script as %s: %s' % (encoding, ex))
    with open(os.path.join(out, '0.txt'), 'wb') as f:
        f.write(data)
    print('0.txt: %d lines (%s)' % (text.count('\n'), encoding))
    for w in warnings:
        print('warning:', w)
    if a.no_assets:
        return
    missing = convert_assets(text, unpacked, out, a.copy)
    for m in missing:
        print('missing asset:', m)
    print('done -> %s' % out)
    sys.exit(1 if missing else 0)


if __name__ == '__main__':
    main()
