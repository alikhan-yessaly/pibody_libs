# Small QR code encoder: byte mode, error correction M (L for the longest
# texts), versions 1-6 (up to 134 bytes). Enough for a Wi-Fi join code.
# Follows ISO/IEC 18004 (layout as in Project Nayuki's reference encoder).
#
#   m = encode("WIFI:T:WPA;S:name;P:password;;")
#   m.size, m.get(x, y) -> True for a dark module

# version: (L: ec per block, blocks), (M: ec per block, blocks); total codewords
_TOTAL = (0, 26, 44, 70, 100, 134, 172)
_EC = {
    "L": (0, (7, 1), (10, 1), (15, 1), (20, 1), (26, 1), (18, 2)),
    "M": (0, (10, 1), (16, 1), (26, 1), (18, 2), (24, 2), (16, 4)),
}
_FORMAT = {"L": 1, "M": 0}
_ALIGN = (0, 0, 18, 22, 26, 30, 34)


def _gf_mul(x, y):
    z = 0
    for i in range(7, -1, -1):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z & 0xFF


def _rs_divisor(degree):
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _gf_mul(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _gf_mul(root, 2)
    return result


def _rs_remainder(data, divisor):
    result = [0] * len(divisor)
    for b in data:
        factor = b ^ result.pop(0)
        result.append(0)
        for i, c in enumerate(divisor):
            result[i] ^= _gf_mul(c, factor)
    return result


class QR:
    def __init__(self, version, level):
        self.version = version
        self.level = level
        self.size = 17 + 4 * version
        n = self.size
        self._m = bytearray(n * n)     # 1 = dark
        self._fn = bytearray(n * n)    # 1 = function module

    def get(self, x, y):
        return self._m[y * self.size + x] == 1

    def _set(self, x, y, dark, fn=True):
        i = y * self.size + x
        self._m[i] = 1 if dark else 0
        if fn:
            self._fn[i] = 1

    def _function_patterns(self):
        n = self.size
        for i in range(n):
            self._set(6, i, i % 2 == 0)
            self._set(i, 6, i % 2 == 0)
        for cx, cy in ((3, 3), (n - 4, 3), (3, n - 4)):
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    x, y = cx + dx, cy + dy
                    if 0 <= x < n and 0 <= y < n:
                        d = max(abs(dx), abs(dy))
                        self._set(x, y, d != 2 and d != 4)
        a = _ALIGN[self.version]
        if a:
            for dy in range(-2, 3):
                for dx in range(-2, 3):
                    self._set(a + dx, a + dy, max(abs(dx), abs(dy)) != 1)
        self._format_bits(0)

    def _format_bits(self, mask):
        n = self.size
        data = _FORMAT[self.level] << 3 | mask
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = (data << 10 | rem) ^ 0x5412
        bit = lambda i: (bits >> i) & 1
        for i in range(6):
            self._set(8, i, bit(i))
        self._set(8, 7, bit(6))
        self._set(8, 8, bit(7))
        self._set(7, 8, bit(8))
        for i in range(9, 15):
            self._set(14 - i, 8, bit(i))
        for i in range(8):
            self._set(n - 1 - i, 8, bit(i))
        for i in range(8, 15):
            self._set(8, n - 15 + i, bit(i))
        self._set(8, n - 8, True)

    def _codewords(self, text):
        v, ec_len_blocks = self.version, _EC[self.level][self.version]
        ec_len, blocks = ec_len_blocks
        total = _TOTAL[v]
        data_len = total - ec_len * blocks
        bits = []

        def put(val, n):
            for i in range(n - 1, -1, -1):
                bits.append((val >> i) & 1)
        put(4, 4)
        put(len(text), 8)
        for b in text:
            put(b, 8)
        put(0, min(4, data_len * 8 - len(bits)))
        put(0, -len(bits) % 8)
        data = []
        for i in range(0, len(bits), 8):
            byte = 0
            for b in bits[i:i + 8]:
                byte = byte << 1 | b
            data.append(byte)
        pad = 0xEC
        while len(data) < data_len:
            data.append(pad)
            pad ^= 0xEC ^ 0x11
        # versions 1-6: all blocks the same length
        per = data_len // blocks
        div = _rs_divisor(ec_len)
        dblocks = [data[i * per:(i + 1) * per] for i in range(blocks)]
        eblocks = [_rs_remainder(b, div) for b in dblocks]
        out = []
        for i in range(per):
            for b in dblocks:
                out.append(b[i])
        for i in range(ec_len):
            for b in eblocks:
                out.append(b[i])
        return out

    def _place(self, codewords):
        n = self.size
        total_bits = len(codewords) * 8
        i = 0
        right = n - 1
        while right >= 1:
            if right == 6:
                right = 5
            upward = ((right + 1) & 2) == 0
            for vert in range(n):
                y = n - 1 - vert if upward else vert
                for j in range(2):
                    x = right - j
                    k = y * n + x
                    if not self._fn[k] and i < total_bits:
                        self._m[k] = (codewords[i >> 3] >> (7 - (i & 7))) & 1
                        i += 1
            right -= 2

    def _mask(self, mask):
        n = self.size
        for y in range(n):
            for x in range(n):
                k = y * n + x
                if self._fn[k]:
                    continue
                if mask == 0:
                    inv = (x + y) % 2 == 0
                elif mask == 1:
                    inv = y % 2 == 0
                elif mask == 2:
                    inv = x % 3 == 0
                elif mask == 3:
                    inv = (x + y) % 3 == 0
                elif mask == 4:
                    inv = (x // 3 + y // 2) % 2 == 0
                elif mask == 5:
                    inv = x * y % 2 + x * y % 3 == 0
                elif mask == 6:
                    inv = (x * y % 2 + x * y % 3) % 2 == 0
                else:
                    inv = ((x + y) % 2 + x * y % 3) % 2 == 0
                if inv:
                    self._m[k] ^= 1

    def _penalty(self):
        n, m = self.size, self._m
        score = 0
        lines = []
        for y in range(n):
            lines.append(bytes(m[y * n:(y + 1) * n]))
        for x in range(n):
            lines.append(bytes(m[y * n + x] for y in range(n)))
        finder_a = b"\x01\x00\x01\x01\x01\x00\x01\x00\x00\x00\x00"
        finder_b = b"\x00\x00\x00\x00\x01\x00\x01\x01\x01\x00\x01"
        for line in lines:
            run, prev = 0, 2
            for c in line:
                if c == prev:
                    run += 1
                else:
                    if run >= 5:
                        score += run - 2
                    run, prev = 1, c
            if run >= 5:
                score += run - 2
            padded = b"\x00" * 4 + line + b"\x00" * 4
            for pat in (finder_a, finder_b):
                k = padded.find(pat)
                while k >= 0:
                    score += 40
                    k = padded.find(pat, k + 1)
        for y in range(n - 1):
            for x in range(n - 1):
                c = m[y * n + x]
                if c == m[y * n + x + 1] == m[(y + 1) * n + x] == m[(y + 1) * n + x + 1]:
                    score += 3
        dark = sum(m)
        k = (abs(dark * 20 - n * n * 10) + n * n - 1) // (n * n) - 1
        score += max(k, 0) * 10
        return score


def encode(text):
    """QR code for text (str or bytes, up to 134 bytes)."""
    if isinstance(text, str):
        text = text.encode()
    for v in range(1, 7):
        for level in ("M", "L"):
            ec_len, blocks = _EC[level][v]
            if len(text) * 8 + 12 <= (_TOTAL[v] - ec_len * blocks) * 8:
                return _build(text, v, level)
    raise ValueError("text too long for a QR code here (134 bytes max)")


def _build(text, version, level):
    q = QR(version, level)
    q._function_patterns()
    q._place(q._codewords(text))
    data = bytes(q._m)
    best, best_mask = None, 0
    for mask in range(8):
        q._m[:] = data
        q._mask(mask)
        q._format_bits(mask)
        p = q._penalty()
        if best is None or p < best:
            best, best_mask = p, mask
    q._m[:] = data
    q._mask(best_mask)
    q._format_bits(best_mask)
    return q


def wifi_text(ssid, password):
    """The text phones read as "join this Wi-Fi" (WPA)."""
    def esc(s):
        for c in "\\;,\":":
            s = s.replace(c, "\\" + c)
        return s
    return "WIFI:T:WPA;S:%s;P:%s;;" % (esc(ssid), esc(password))


def draw(display, q, x, y, scale, dark=0x0000, light=0xFFFF, quiet=4):
    """Draw q with its quiet zone at x, y: (size + 2*quiet) * scale pixels square."""
    n = q.size
    side = (n + 2 * quiet) * scale
    display.fill_rect(x, y, side, side, light)
    ox, oy = x + quiet * scale, y + quiet * scale
    for r in range(n):
        c = 0
        while c < n:
            if q.get(c, r):
                s = c
                while c < n and q.get(c, r):
                    c += 1
                display.fill_rect(ox + s * scale, oy + r * scale, (c - s) * scale, scale, dark)
            else:
                c += 1
    return side
