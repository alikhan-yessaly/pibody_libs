#!/usr/bin/env python3
"""Lossless PNG shrinker for the board's display images (no dependencies).

    python3 tools/pngshrink.py demo/module_pngs/*.png [--check]

Keeps every pixel's RGBA value exactly; transparent pixels (alpha 0) are
merged into one colour, which no viewer and not the st7789 driver can see.
Drops metadata chunks (colour profiles, gamma, text, EXIF, physical size),
writes the smallest of: palette PNG at the lowest bit depth that fits, or
RGB/RGBA, each with several filter choices at zlib level 9.
--check only reports sizes.
"""
import struct
import sys
import zlib


def _chunks(data):
    i = 8
    while i < len(data):
        n = struct.unpack(">I", data[i:i + 4])[0]
        yield data[i + 4:i + 8], data[i + 8:i + 8 + n]
        i += 12 + n


def _paeth(a, b, c):
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def decode(data):
    """-> width, height, list of rows of (r, g, b, a)."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    idat = b""
    plte = trns = None
    for t, d in _chunks(data):
        if t == b"IHDR":
            w, h, bd, ct, _, _, il = struct.unpack(">IIBBBBB", d)
        elif t == b"PLTE":
            plte = [tuple(d[i:i + 3]) for i in range(0, len(d), 3)]
        elif t == b"tRNS":
            trns = d
        elif t == b"IDAT":
            idat += d
    assert il == 0, "interlaced PNG not supported"
    ch = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[ct]
    bpp_bits = ch * bd
    stride = (w * bpp_bits + 7) // 8
    bpp = max(1, bpp_bits // 8)
    raw = zlib.decompress(idat)
    rows = []
    prev = bytearray(stride)
    p = 0
    for _ in range(h):
        f = raw[p]
        line = bytearray(raw[p + 1:p + 1 + stride])
        p += 1 + stride
        for i in range(stride):
            a = line[i - bpp] if i >= bpp else 0
            b = prev[i]
            c = prev[i - bpp] if i >= bpp else 0
            line[i] = (line[i] + (0, a, b, (a + b) // 2, _paeth(a, b, c))[f]) & 0xFF
        prev = line
        px = []
        for x in range(w):
            if bd < 8:
                bit = x * bd
                v = (line[bit // 8] >> (8 - bd - bit % 8)) & ((1 << bd) - 1)
                vals = (v,)
            else:
                assert bd == 8, "16-bit PNG not supported"
                vals = tuple(line[x * ch:(x + 1) * ch])
            if ct == 3:
                r, g, bb = plte[vals[0]]
                al = trns[vals[0]] if trns and vals[0] < len(trns) else 255
                px.append((r, g, bb, al))
            elif ct == 6:
                px.append(vals)
            elif ct == 2:
                px.append(vals + (255,))
            elif ct == 0:
                px.append((vals[0] * 255 // ((1 << bd) - 1),) * 3 + (255,))
            else:
                px.append((vals[0],) * 3 + (vals[1],))
        rows.append(px)
    return w, h, rows


def _filter(lines, bpp, mode):
    out = bytearray()
    prev = bytes(len(lines[0]))
    for line in lines:
        cands = []
        for f in ((0,) if mode == "none" else (0, 1, 2, 3, 4)):
            enc = bytearray([f])
            for i in range(len(line)):
                a = line[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                pred = (0, a, b, (a + b) // 2, _paeth(a, b, c))[f]
                enc.append((line[i] - pred) & 0xFF)
            cands.append(enc)
        if mode == "none":
            out += cands[0]
        else:  # minimum sum of absolute differences
            out += min(cands, key=lambda e: sum(v if v < 128 else 256 - v for v in e[1:]))
        prev = line
    return bytes(out)


def _chunk(t, d):
    return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))


def _z(data):
    best = None
    for strategy in (zlib.Z_DEFAULT_STRATEGY, zlib.Z_FILTERED):
        c = zlib.compressobj(9, zlib.DEFLATED, 15, 9, strategy)
        z = c.compress(data) + c.flush()
        if best is None or len(z) < len(best):
            best = z
    return best


def encode(w, h, rows):
    rows = [[p if p[3] else (0, 0, 0, 0) for p in r] for r in rows]
    colors = sorted({p for r in rows for p in r}, key=lambda p: (p[3] == 255, p))
    results = []
    if len(colors) <= 256:
        idx = {c: i for i, c in enumerate(colors)}
        bd = next(b for b in (1, 2, 4, 8) if len(colors) <= 1 << b)
        lines = []
        for r in rows:
            bits = bytearray((w * bd + 7) // 8)
            for x, p in enumerate(r):
                bit = x * bd
                bits[bit // 8] |= idx[p] << (8 - bd - bit % 8)
            lines.append(bytes(bits))
        plte = b"".join(bytes(c[:3]) for c in colors)
        alphas = bytes(c[3] for c in colors)
        trns = alphas.rstrip(b"\xff")
        head = _chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, bd, 3, 0, 0, 0)) + _chunk(b"PLTE", plte)
        if trns:
            head += _chunk(b"tRNS", trns)
        for mode in ("none", "adaptive"):
            results.append(head + _chunk(b"IDAT", _z(_filter(lines, 1, mode))))
    alpha = any(p[3] != 255 for r in rows for p in r)
    ch = 4 if alpha else 3
    lines = [bytes(v for p in r for v in p[:ch]) for r in rows]
    head = _chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6 if alpha else 2, 0, 0, 0))
    for mode in ("none", "adaptive"):
        results.append(head + _chunk(b"IDAT", _z(_filter(lines, ch, mode))))
    best = min(results, key=len)
    return b"\x89PNG\r\n\x1a\n" + best + _chunk(b"IEND", b"")


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    check = "--check" in sys.argv
    before = after = 0
    for path in args:
        data = open(path, "rb").read()
        w, h, rows = decode(data)
        new = encode(w, h, rows)
        w2, h2, rows2 = decode(new)
        same = (w2, h2) == (w, h) and all(
            (a if a[3] else (0, 0, 0, 0)) == b for ra, rb in zip(rows, rows2) for a, b in zip(ra, rb))
        assert same, path + ": round trip differs"
        before += len(data)
        after += min(len(new), len(data))
        print("%-40s %6d -> %6d" % (path, len(data), len(new)))
        if not check and len(new) < len(data):
            open(path, "wb").write(new)
    print("total %d -> %d bytes (%d%%)" % (before, after, 100 * after // max(1, before)))


if __name__ == "__main__":
    main()
