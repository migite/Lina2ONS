"""Structural disassembler for decoded SVS1 scripts (*.spr).

The layout was derived from the interpreter in sviu.exe.

File layout (little endian)
  +0x04 start of the script's own code (everything before it is the shared library)
  +0x08 start of the string pool, +0x0c file size, +0x14 header size (= start of code)
  +0x20 table of 1024 label offsets (label n -> absolute code offset)

Statement words (u16)
  0x0200 numeric expression, 0x0201 string expression; both are RPN and end with 0x019f
  0x0314 push the last expression result on the argument stack
  0x1000-0x17ff command number (word & 0x7ff); its arguments are the pushed values
  0x0300 jmp abs32          0x0301 jump if false abs32     0x0302 jump if true abs32
  0x0303 case imm32,abs32   (compares with the last expression result)
  0x0305 jmp label(u16)     0x0310 call label(u16)         0x0311/0x0317 return(u16)
  0x0312 u16,u32   0x0318 u16   0x0319 u16   0x031a u16,u32   (frame bookkeeping)

Expression tokens (u16)
  0x100 integer constant (imm32)      0x105 string constant (imm32 = pool offset, string exprs)
  0x101 / 0x108 variable indexed by a nested expression
  0x102 nested numeric expression in string exprs (skipped imm32 in numeric exprs)
  0x10b / 0x10e array base imm32 + nested index expression
  0x10c inline function call (not supported here)
  0x180-0x192 operators (no operand), 0x200 nested numeric expression,
  any other token is followed by an imm32 (variable address / constant)
"""
import struct
from collections import namedtuple

Ins = namedtuple("Ins", "off kind a b c")

OPS = {
    0x180: '*', 0x181: '/', 0x182: '%', 0x183: '+', 0x184: '-', 0x185: '&', 0x186: '|',
    0x187: '^', 0x188: '<<', 0x189: '>>', 0x18a: '>', 0x18b: '>=', 0x18c: '<', 0x18d: '<=',
    0x18e: '&&', 0x18f: '||', 0x190: '=', 0x191: '==', 0x192: '!=',
}


class Script:
    def __init__(self, data):
        if data[:4] != b"SVS1":
            raise ValueError("not SVS1")
        self.b = data
        self.code_mid, self.pool, self.total = struct.unpack_from("<3I", data, 4)
        self.hdr = struct.unpack_from("<I", data, 0x14)[0]
        self.labels = list(struct.unpack_from("<1024I", data, 0x20))

    def string(self, rel):
        s = self.pool + rel
        return self.b[s:self.b.find(b"\0", s)].decode("cp932", "replace")

    def u16(self, o):
        return struct.unpack_from("<H", self.b, o)[0]

    def u32(self, o):
        return struct.unpack_from("<I", self.b, o)[0]

    def expr(self, o):
        """Parse the expression at o (a 0x200/0x201 word). Returns ((kind, items), next_offset)."""
        kind = self.u16(o)
        o += 2
        items = []
        while True:
            t = self.u16(o)
            if t == 0x19f:
                return (kind, items), o + 2
            if t in (0x101, 0x108) or (t == 0x102 and kind == 0x201):
                sub, o = self.expr(o + 2)
                items.append(('vi', t, sub))
            elif t in (0x10b, 0x10e) and kind == 0x200:
                imm = self.u32(o + 2)
                sub, o = self.expr(o + 6)
                items.append(('vk', t, imm, sub))
            elif t == 0x10c and kind == 0x200:
                raise ValueError("inline call at 0x%x" % o)
            elif t in OPS:
                items.append(('op', t))
                o += 2
            elif t == 0x200:
                sub, o = self.expr(o)
                items.append(('sub', sub))
            else:
                items.append(('t', t, self.u32(o + 2)))
                o += 6

    def disasm(self, start=None, end=None):
        o = self.hdr if start is None else start
        end = self.pool if end is None else end
        out = []
        while o < end:
            w = self.u16(o)
            if w in (0x200, 0x201):
                e, n = self.expr(o)
                out.append(Ins(o, 'expr', e, None, None))
                o = n
            elif w == 0x314:
                out.append(Ins(o, 'push', None, None, None))
                o += 2
            elif 0x1000 <= w <= 0x17ff:
                out.append(Ins(o, 'cmd', w & 0x7ff, None, None))
                o += 2
            elif w in (0x300, 0x301, 0x302):
                out.append(Ins(o, {0x300: 'jmp', 0x301: 'jf', 0x302: 'jt'}[w], self.u32(o + 2), None, None))
                o += 6
            elif w == 0x303:
                out.append(Ins(o, 'case', self.u32(o + 2), self.u32(o + 6), None))
                o += 10
            elif w in (0x305, 0x310):
                out.append(Ins(o, 'jmpl' if w == 0x305 else 'calll', self.u16(o + 2), None, None))
                o += 4
            elif w in (0x311, 0x317):
                out.append(Ins(o, 'ret', w, self.u16(o + 2), None))
                o += 4
            elif w in (0x312, 0x31a):
                out.append(Ins(o, 'frame', self.u16(o + 2), self.u32(o + 4), None))
                o += 8
            elif w in (0x318, 0x319):
                out.append(Ins(o, 'frame', self.u16(o + 2), None, None))
                o += 4
            else:
                raise ValueError("unknown word 0x%x at 0x%x" % (w, o))
        return out


def fmt_expr(sc, e):
    """Readable RPN dump of an expression (debugging aid)."""
    kind, items = e
    parts = []
    for it in items:
        k = it[0]
        if k == 't':
            t, v = it[1], it[2]
            sv = v if v < 0x80000000 else v - (1 << 32)
            if t == 0x105 and kind == 0x201:
                parts.append('"%s"' % sc.string(v))
            elif t == 0x100:
                parts.append(str(sv))
            else:
                parts.append('T%x(%d)' % (t, sv))
        elif k == 'vi':
            parts.append('V%x[%s]' % (it[1], fmt_expr(sc, it[2])))
        elif k == 'vk':
            parts.append('V%x(%d)[%s]' % (it[1], it[2], fmt_expr(sc, it[3])))
        elif k == 'op':
            parts.append(OPS[it[1]])
        else:
            parts.append('{%s}' % fmt_expr(sc, it[1]))
    return ' '.join(parts)
