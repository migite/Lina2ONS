#!/usr/bin/env python3
"""Convert the decoded SVS1 scripts (*.spr) of ハヤシ迷作劇場 into an ONScripter/Ponscripter script.

Usage:
  python svs_to_ons.py [--src DIR] [--out FILE] [--scripts NAME ...] [--encoding utf-8|cp932]

  --src      folder with the decoded .spr files (default: ..\\unpacked\\script_decoded)
  --out      output script (default: ..\\onscripter\\0.txt)
  --scripts  scripts to convert (default: mei*, eye, ed1-ed5)
  --encoding output encoding: cp932 (Shift-JIS) for ONScripter (default), utf-8 for Ponscripter

How it works
  * Every .spr carries a copy of the shared library (menus, message window, sprite handling ...).
    The tool reads the tables out of that library (background list, BGM list, sprite files and
    widths, choice layouts) and translates calls into the library as high level commands that are
    implemented by the runtime in ons_runtime.txt.
  * The script's own code is decompiled into events (assignments, jumps, calls, messages),
    unreachable code is dropped, and every event is written as ONScripter code.
  * Unknown commands are kept as ';' comments so that nothing is silently lost.
"""
import argparse
import bisect
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from svs_disasm import OPS, Script  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


class Unsupported(Exception):
    pass


def sgn(v):
    return v - (1 << 32) if v >= 0x80000000 else v


# ---------------------------------------------------------------- expressions
def parse_expr(sc, e):
    """RPN expression -> tree.

    ('c', int) ('s', str) ('v', token, address) ('vi', token, index) ('vk', token, base, index)
    ('bin', op, l, r) ('asg', lhs, rhs)
    """
    kind, items = e
    st = []
    for it in items:
        k = it[0]
        if k == 't':
            t, v = it[1], it[2]
            if t == 0x100:
                st.append(('c', sgn(v)))
            elif t == 0x105 and kind == 0x201:
                st.append(('s', sc.string(v)))
            else:
                st.append(('v', t, sgn(v)))
        elif k == 'vi':
            st.append(('vi', it[1], parse_expr(sc, it[2])))
        elif k == 'vk':
            st.append(('vk', it[1], it[2], parse_expr(sc, it[3])))
        elif k == 'sub':
            st.append(parse_expr(sc, it[1]))
        else:
            op = OPS[it[1]]
            r = st.pop()
            l = st.pop()
            st.append(('asg', l, r) if op == '=' else ('bin', op, l, r))
    return st[-1] if st else ('c', 0)


CONST_OPS = {
    '+': lambda a, b: a + b, '-': lambda a, b: a - b, '*': lambda a, b: a * b,
    '/': lambda a, b: int(a / b) if b else 0, '%': lambda a, b: a % b if b else 0,
    '&': lambda a, b: a & b, '|': lambda a, b: a | b, '^': lambda a, b: a ^ b,
    '<<': lambda a, b: a << b, '>>': lambda a, b: a >> b,
    '==': lambda a, b: int(a == b), '!=': lambda a, b: int(a != b), '<': lambda a, b: int(a < b),
    '>': lambda a, b: int(a > b), '<=': lambda a, b: int(a <= b), '>=': lambda a, b: int(a >= b),
    '&&': lambda a, b: int(bool(a) and bool(b)), '||': lambda a, b: int(bool(a) or bool(b)),
}


def fold(ast, consts):
    k = ast[0]
    if k == 'v':
        key = (ast[1], ast[2])
        return ('c', consts[key]) if key in consts else ast
    if k == 'vi':
        idx = fold(ast[2], consts)
        if idx[0] == 'c' and (ast[1], idx[1]) in consts:
            return ('c', consts[(ast[1], idx[1])])
        return ('vi', ast[1], idx)
    if k == 'vk':
        return ('vk', ast[1], ast[2], fold(ast[3], consts))
    if k == 'bin':
        l, r = fold(ast[2], consts), fold(ast[3], consts)
        if l[0] == 'c' and r[0] == 'c' and ast[1] in CONST_OPS:
            return ('c', CONST_OPS[ast[1]](l[1], r[1]))
        return ('bin', ast[1], l, r)
    if k == 'asg':
        return ('asg', ast[1], fold(ast[2], consts))
    return ast


def fmt_ast(ast):
    k = ast[0]
    if k == 'c':
        return str(ast[1])
    if k == 's':
        return '"%s"' % ast[1]
    if k == 'v':
        return 'T%x(%d)' % (ast[1], ast[2])
    if k == 'vi':
        return 'V%x[%s]' % (ast[1], fmt_ast(ast[2]))
    if k == 'vk':
        return 'V%x(%d)[%s]' % (ast[1], ast[2], fmt_ast(ast[3]))
    if k == 'bin':
        return '(%s %s %s)' % (fmt_ast(ast[2]), ast[1], fmt_ast(ast[3]))
    return '%s = %s' % (fmt_ast(ast[1]), fmt_ast(ast[2]))


# ------------------------------------------------------------------ variables
class VarMap:
    """Maps game variables onto ONScripter variables.

    V101[n] (n < 175)  -> %n            story flags, saved with the savegame
    V101[n] (n >= 175) -> %300...       persistent (global) variables
    V108[1500 + k]     -> %(400 + k)    CG / scene unlock flags (global)
    everything else    -> %500...       scratch values
    """
    LOCAL_LIMIT = 175

    def __init__(self):
        self.g = {}
        self.misc = {}

    def v101(self, n):
        if 0 <= n < self.LOCAL_LIMIT:
            return n
        return self.g.setdefault(('v101', n), 300 + len([k for k in self.g if k[0] == 'v101']))

    def v108(self, n):
        if 1500 <= n < 1600:
            return 400 + n - 1500
        return self.misc_var(('v108', n))

    def misc_var(self, key):
        return self.misc.setdefault(key, 500 + len(self.misc))

    def num(self, ast):
        k = ast[0]
        if k == 'vi' and ast[2][0] == 'c':
            n = ast[2][1]
            if ast[1] == 0x101:
                return '%%%d' % self.v101(n)
            if ast[1] == 0x108:
                return '%%%d' % self.v108(n)
            if ast[1] == 0x102:
                return '$%d' % n
        if k == 'v':
            return '%%%d' % self.misc_var(('v', ast[1], ast[2]))
        if k == 'vk' and ast[3][0] == 'c':
            return '%%%d' % self.misc_var(('vk', ast[1], ast[2], ast[3][1]))
        raise Unsupported('variable ' + fmt_ast(ast))


# -------------------------------------------------------------------- library
class Library:
    """Reads the lookup tables out of the shared library code."""

    def __init__(self, sc):
        self.sc = sc
        self.ins = sc.disasm(sc.hdr, sc.code_mid)
        self.idx = {i.off: k for k, i in enumerate(self.ins)}

    def func(self, label):
        k = self.idx[self.sc.labels[label]]
        e = k
        while self.ins[e].kind != 'ret':
            e += 1
        return k, e

    def first_string(self, k, limit=60):
        for i in self.ins[k:k + limit]:
            if i.kind == 'expr' and i.a[0] == 0x201:
                for it in i.a[1]:
                    if it[0] == 't' and it[1] == 0x105:
                        return self.sc.string(it[2])
            if i.kind in ('jmp', 'ret'):
                break
        return None

    def first_call(self, k, limit=20):
        for i in self.ins[k:k + limit]:
            if i.kind == 'calll':
                return i.a
        return None

    def case_group(self, k, e):
        res = []
        for i in self.ins[k:e]:
            if i.kind == 'case':
                res.append((i.a, i.b))
            elif res:
                break
        return res

    def table(self, label, getter):
        k, e = self.func(label)
        return {v: getter(self.idx[t]) for v, t in self.case_group(k, e)}

    def bg(self):
        return self.table(15, self.first_string)

    def bgm(self):
        return {v: s for v, s in self.table(31, self.first_string).items() if s}

    def chars(self):
        """char id -> (width, [file per expression])"""
        out = {}
        for ch, lab in self.table(18, self.first_call).items():
            k, e = self.func(lab)
            files = {}
            width = None
            for v, t in self.case_group(k, e):
                files[v] = self.first_string(self.idx[t])
                for i in self.ins[self.idx[t]:self.idx[t] + 12]:
                    if i.kind == 'expr':
                        a = parse_expr(self.sc, i.a)
                        if a[0] == 'asg' and a[1] == ('v', 0x103, 291) and a[2][0] == 'c':
                            width = a[2][1]
            out[ch] = (width, [files[v] for v in sorted(files)])
        return out

    def selects(self):
        """choice set id -> list of (row, column group) cells of select_mes"""
        k, e = self.func(9)
        res = {}
        for sid, t in self.case_group(k, e):
            n = None
            rows = {}
            cols = {}
            for i in self.ins[self.idx[t]:e]:
                if i.kind == 'jmp':
                    break
                if i.kind != 'expr':
                    continue
                a = fold(parse_expr(self.sc, i.a), {})
                if a[0] != 'asg':
                    continue
                lhs, rhs = a[1], a[2]
                if lhs == ('v', 0x10d, 28) and rhs[0] == 'c':
                    n = rhs[1]
                elif lhs[0] == 'vk' and lhs[2] in (32, 44) and lhs[3][0] == 'c':
                    (rows if lhs[2] == 32 else cols)[lhs[3][1]] = rhs
            if n is None:
                continue
            cells = []
            for j in range(n):
                r, c = rows.get(j), cols.get(j)
                if r is None or r[0] != 'c':
                    raise Unsupported('select layout %d' % sid)
                cells.append((r[1] // 34, c[1] // 204 if c and c[0] == 'c' else 0))
            res[sid] = cells
        return res


# ------------------------------------------------------------------ converter
CMP = {'==': '==', '!=': '!=', '<': '<', '>': '>', '<=': '<=', '>=': '>='}
CMP_NOT = {'==': '!=', '!=': '==', '<': '>=', '>=': '<', '>': '<=', '<=': '>'}
ARITH = {'+': 'add', '-': 'sub', '*': 'mul', '/': 'div', '%': 'mod'}
TEMP0 = 190
CONSTS = {
    (0x101, 80): 0,     # V101[80] = scene replay mode, never active in a normal playthrough
    (0x109, 0): 0,      # T109(0) = movie / key state; the wait loops after a movie start are skipped, because
                        # s_movie (mpegplay) returns only when the movie has ended or was cancelled
}
SEL_RESULT = 179                # runtime register holding the choice result
MOVIE_CMD = 435                 # starts a movie (the wait loop with CMD433 / CMD226 / CMD434 follows)
IGNORED_CMDS = {0, 8, 17, 18, 45, 51, 82, 130, 143, 433, 434}   # frame waits, progress saves, movie polling ...
CM_SLOT = 126                   # V101[126]: the CM that cm.spr plays between the two eye catches
CM_REGISTER = 186               # runtime register: the CM number that s_cm_play plays
OPENING_MOVIE = 'mei_demo'      # op.spr, played when a new game starts
ENDING_MOVIE = 'ed_demo'        # ed.spr, played after the last chapter
# cm.spr: CM number -> movies (one is picked at random). The numbers 2, 3, 4 and 13 are slide shows without a movie.
CM_MOVIES = {
    1: ['pure_cm'], 5: ['ayakasi_cm'], 6: ['demo_m004fix'], 7: ['paru_cm1', 'paru_cm2'], 8: ['silk_cm'],
    9: ['of'], 10: ['uta'], 11: ['giri_cm'], 12: ['TG1', 'TG2'], 14: ['youko', 'o_love'],
}


class Event:
    __slots__ = ('off', 'kind', 'ast', 'args', 'n', 'target', 'val', 'cond')

    def __init__(self, off, kind, **kw):
        self.off, self.kind = off, kind
        self.ast = self.args = self.n = self.target = self.val = self.cond = None
        for k, v in kw.items():
            setattr(self, k, v)


class Converter:
    def __init__(self, lib, vars_, converted, voices=None, sounds=None, movies=None):
        self.lib = lib
        self.vars = vars_
        self.converted = converted
        self.voices = voices        # lower case voice names that exist (None: do not check)
        self.sounds = sounds        # lower case sound effect file names that exist
        self.movies = movies or set()   # lower case movie names (no extension) that exist
        self.bg = lib.bg()
        self.bgm = lib.bgm()
        self.chars = lib.chars()
        self.stubs = set()
        self.warnings = []

    # ---- decoding into events ---------------------------------------------
    def events(self, sc):
        ins = sc.disasm(sc.code_mid)
        evs = []
        pend = []          # (kind, off, ast) kind: 'arg' | 'eval'
        i = 0

        def flush_evals(keep_args=False):
            nonlocal pend
            for k, off, a in pend:
                if k == 'eval' or not keep_args:
                    evs.append(Event(off, 'eval', ast=a))
            pend = []

        while i < len(ins):
            x = ins[i]
            if x.kind == 'expr':
                a = parse_expr(sc, x.a)
                if i + 1 < len(ins) and ins[i + 1].kind == 'push':
                    pend.append(('arg', x.off, a))
                    i += 2
                else:
                    pend.append(('eval', x.off, a))
                    i += 1
                continue
            if x.kind in ('cmd', 'calll'):
                args = []
                first = x.off
                for k, off, a in pend:
                    if k == 'eval':
                        evs.append(Event(off, 'eval', ast=a))
                    else:
                        args.append(a)
                        first = min(first, off)
                evs.append(Event(first if args else x.off, x.kind, n=x.a, args=args))
                pend = []
            elif x.kind in ('jf', 'jt'):
                cond = None
                if pend and pend[-1][0] == 'eval':
                    cond = pend.pop()
                flush_evals()
                evs.append(Event(cond[1] if cond else x.off, x.kind, target=x.a, cond=cond[2] if cond else None))
            elif x.kind == 'case':
                cond = None
                if pend and pend[-1][0] == 'eval':
                    cond = pend.pop()
                flush_evals()
                evs.append(Event(cond[1] if cond else x.off, 'case', val=sgn(x.a), target=x.b,
                                 cond=cond[2] if cond else None))
            elif x.kind in ('jmp', 'jmpl', 'ret'):
                flush_evals()
                evs.append(Event(x.off, x.kind, target=x.a if x.kind != 'ret' else None))
            else:
                flush_evals()
            i += 1
        flush_evals()
        return evs

    def successors(self, sc, ev):
        """Returns (jump targets, falls through to the next event)."""
        if ev.kind == 'jmp':
            return [ev.target], False
        if ev.kind == 'jmpl':
            return [self.norm(sc.labels[ev.target])], False
        if ev.kind == 'ret':
            return [], False
        if ev.kind in ('jf', 'jt'):
            c = fold(ev.cond, CONSTS) if ev.cond else None
            if c is not None and c[0] == 'c':
                taken = (c[1] == 0) if ev.kind == 'jf' else (c[1] != 0)
                return ([ev.target], False) if taken else ([], True)
            return [ev.target], True
        if ev.kind == 'case':
            return [ev.target], True
        if ev.kind == 'cmd' and ev.n == 48:
            tgt = self.script_target(ev)
            if tgt != 'next':
                return [], False
        return [], True

    def script_target(self, ev):
        for a in reversed(ev.args):
            if a[0] == 's':
                return os.path.splitext(a[1])[0].lower()
        return None

    # ---- ONS emission helpers -------------------------------------------------
    def tmp(self):
        t = TEMP0 + self.ntmp
        self.ntmp += 1
        if t > 199:
            raise Unsupported('expression too deep')
        return '%%%d' % t

    def esc(self, s):
        return s.replace('"', "'")

    def opnd(self, ast):
        ast = fold(ast, CONSTS)
        k = ast[0]
        if k == 'c':
            return str(ast[1])
        if k == 's':
            return '"%s"' % self.esc(ast[1])
        if k == 'asg':
            self.assign(ast[1], ast[2])
            return self.opnd(ast[1])
        if k == 'bin':
            return self.binop(ast)
        return self.vars.num(ast)

    def binop(self, ast):
        op, l, r = ast[1], ast[2], ast[3]
        t = self.tmp()
        if op in ARITH:
            lo = self.opnd(l)
            self.out('mov %s,%s' % (t, lo))
            ro = self.opnd(r)
            self.out('%s %s,%s' % (ARITH[op], t, ro))
            return t
        if op in CMP:
            lo, ro = self.opnd(l), self.opnd(r)
            self.out('mov %s,0' % t)
            self.out('if %s %s %s mov %s,1' % (lo, CMP[op], ro, t))
            return t
        if op == '<<' and r[0] == 'c':
            lo = self.opnd(l)
            self.out('mov %s,%s' % (t, lo))
            self.out('mul %s,%d' % (t, 1 << r[1]))
            return t
        raise Unsupported('operator %s in %s' % (op, fmt_ast(ast)))

    def assign(self, lhs, rhs):
        rhs = fold(rhs, CONSTS)
        if (rhs[0] == 'bin' and rhs[1] == '|' and rhs[2] == fold(lhs, CONSTS) and rhs[3][0] == 'c'
                and rhs[3][1] > 0 and rhs[3][1] & (rhs[3][1] - 1) == 0):
            # "flags |= bit": every bit becomes its own persistent variable
            self.out('mov %%%d,1' % self.vars.misc_var(('bit', fmt_ast(rhs[2]), rhs[3][1])))
            return
        dst = self.vars.num(fold(lhs, CONSTS))
        if rhs[0] == 'bin' and rhs[1] in ARITH:
            lo = self.opnd(rhs[2])
            ro = self.opnd(rhs[3])
            if lo == dst:
                self.out('%s %s,%s' % (ARITH[rhs[1]], dst, ro))
            else:
                t = self.tmp()
                self.out('mov %s,%s' % (t, lo))
                self.out('%s %s,%s' % (ARITH[rhs[1]], t, ro))
                self.out('mov %s,%s' % (dst, t))
            return
        src = self.opnd(rhs)
        if src != dst:
            self.out('mov %s,%s' % (dst, src))

    def jump_if(self, ast, target, want):
        ast = fold(ast, CONSTS)
        if ast[0] == 'c':
            if bool(ast[1]) == want:
                self.out('goto *%s' % target)
            return
        if ast[0] == 'bin' and ast[1] in CMP:
            lo, ro = self.opnd(ast[2]), self.opnd(ast[3])
            self.out('if %s %s %s goto *%s' % (lo, CMP[ast[1]] if want else CMP_NOT[ast[1]], ro, target))
            return
        if ast[0] == 'bin' and ast[1] in ('&&', '||'):
            both = (ast[1] == '&&') != want      # a && b jumping on false / a || b jumping on true
            if both:
                self.jump_if(ast[2], target, want)
                self.jump_if(ast[3], target, want)
            else:
                skip = self.newlabel()
                self.jump_if(ast[2], skip, not want)
                self.jump_if(ast[3], target, want)
                self.label(skip)
            return
        v = self.opnd(ast)
        self.out('if %s %s 0 goto *%s' % (v, '!=' if want else '==', target))

    def newlabel(self):
        self.nlabel += 1
        return '%s_c%d' % (self.base, self.nlabel)

    def out(self, line):
        self.lines.append(line)

    def label(self, name):
        self.lines.append('*' + name)
        self.cur_name = None

    def comment(self, text):
        self.lines.append('; ' + text)

    # ---- messages -----------------------------------------------------------------
    def message(self, args):
        if len(args) < 2 or args[0][0] != 's':
            raise Unsupported('dynamic message')
        text = args[0][1]
        voice = []
        name_ast = args[1]
        if name_ast[0] == 's':
            nm = name_ast[1]
            for m in re.finditer(r"\\pw'([^']*)'", nm):
                voice.append(m.group(1))
            nm = re.sub(r"\\p[ws]'[^']*'", '', nm)
            name_cmd = 's_nm "%s"' % self.esc(nm)
        else:
            name_cmd = 's_nm %s' % self.vars.num(fold(name_ast, CONSTS))
        if name_cmd != self.cur_name:
            self.out(name_cmd)
            self.cur_name = name_cmd
        se = []
        pieces = []
        for m in re.finditer(r"\\pw'([^']*)'|\\ps'([^']*)'|\\k|\\c|\\n|[^\\]+|\\", text):
            tok = m.group(0)
            if m.group(1) is not None:
                voice.append(m.group(1))
            elif m.group(2) is not None:
                se.append(m.group(2))
            else:
                pieces.append(tok)
        for v in voice:
            base = os.path.splitext(v)[0]
            if self.voices is not None and base.lower() not in self.voices:
                self.comment('missing voice %s' % v)
            else:
                self.out('dwave 0,"voice\\%s.ogg"' % base)
        for s in se:
            if self.sounds is not None and s.lower() not in self.sounds:
                self.comment('missing sound %s' % s)
            else:
                self.out('dwave 1,"wav\\%s"' % s)
        line = ''
        lines = []
        j = 0
        while j < len(pieces):
            p = pieces[j]
            if p == '\\k':
                if j + 1 < len(pieces) and pieces[j + 1] == '\\c':
                    line += '\\'
                    j += 1
                else:
                    line += '@'
            elif p == '\\c':
                line += '\\'
            elif p == '\\n':
                lines.append(line)
                line = ''
            else:
                line += p
            j += 1
        if line:
            lines.append(line)
        for l in lines:
            self.out(l)

    # ---- library calls ----------------------------------------------------------
    def const_arg(self, args, i):
        a = fold(args[i], CONSTS)
        if a[0] != 'c':
            raise Unsupported('non constant argument')
        return a[1]

    def lib_call(self, n, args):
        for a in args:
            if a[0] == 'asg':
                self.assign(a[1], a[2])
        args = [a for a in args if a[0] != 'asg']
        try:
            h = getattr(self, 'L%d' % n, None)
            if h is None:
                raise Unsupported('library function %d' % n)
            h(args)
        except (Unsupported, IndexError) as ex:
            self.comment('UNSUPPORTED calll %d(%s) %s' % (n, ', '.join(fmt_ast(a) for a in args), ex))
            self.warnings.append('%s: calll %d: %s' % (self.base, n, ex))

    def L1(self, args):
        if args[0][0] == 's':
            self.out('caption "ハヤシ迷作劇場　%s"' % self.esc(args[0][1].replace('～', '').strip().replace('　', ' ')))

    def L2(self, args):
        self.out('s_chr_reset')

    def L3(self, args):
        pass

    def L4(self, args):
        self.out('s_quake')

    def L5(self, args):
        self.out('s_flash')

    def L9(self, args):
        try:
            ms = self.const_arg(args, 0)
        except Unsupported:
            ms = 3000
        self.out('s_select %d,%d' % (self.const_arg(args, 2), ms))
        self.out('mov %s,%%%d' % (self.vars.num(('v', 0x103, 279)), SEL_RESULT))

    def L10(self, args):
        if self.const_arg(args, 0):
            self.out('texton')
        else:
            self.out('s_textoff')
            self.cur_name = None

    def L12(self, args):
        self.message(args)

    def L13(self, args):
        self.out('texton')

    def L15(self, args):
        n = self.const_arg(args, 3)
        f = self.bg.get(n)
        if not f:
            raise Unsupported('background %d' % n)
        t = self.const_arg(args, 0)
        eff = self.const_arg(args, 1)
        ms = 0 if eff < 0 else (t if t > 0 else 750)
        if eff >= 64:                       # effects 64 and up also remove the characters
            self.out('mov %6,-1')
            self.out('mov %7,-1')
        self.out('s_bg "%s",%d' % (f, ms))

    def L17(self, args):
        self.out('s_chr %s' % ','.join(self.opnd(a) for a in args))

    def L20(self, args):
        self.out('s_chr_clear %s' % self.opnd(args[0]))

    def L21(self, args):
        self.out('s_chr2 %s' % ','.join(self.opnd(a) for a in args))

    def L31(self, args):
        f = self.bgm.get(self.const_arg(args, 2))
        if not f:
            raise Unsupported('bgm')
        self.out('bgm "music\\%s.ogg"' % os.path.splitext(f)[0])

    def L32(self, args):
        self.out('bgmstop')

    def L33(self, args):
        self.out('bgmstop')

    def L34(self, args):
        self.out('s_fadein %s,%s' % (self.opnd(args[0]), self.opnd(args[1])))

    def L35(self, args):
        self.out('s_fadeout %s,%s' % (self.opnd(args[0]), self.opnd(args[1])))

    L36 = L34
    L37 = L35

    def L38(self, args):
        self.comment('snow effect on (not implemented)')

    def L40(self, args):
        self.comment('snow effect off (not implemented)')

    def L41(self, args):
        pass

    # ---- commands ---------------------------------------------------------------------
    def command(self, ev):
        n, args = ev.n, ev.args
        for a in args:
            if a[0] == 'asg' and a[1][0] != 'v':
                try:
                    self.assign(a[1], a[2])
                except Unsupported as ex:
                    self.comment('UNSUPPORTED assignment %s (%s)' % (fmt_ast(a), ex))
        if n == 48:
            tgt = self.script_target(ev)
            if tgt in self.converted:
                self.out('goto *%s_start' % tgt)
            elif tgt == 'next':
                self.out('gosub *s_next_preview')
                self.cur_name = None
            elif tgt == 'cm':
                self.out('mov %%%d,%%%d' % (CM_REGISTER, CM_SLOT))
                self.out('gosub *s_cm_play')
                self.out('goto *%s_start' % self.base)       # cm.spr hands over to eye.spr again
                self.cur_name = None
            else:
                self.stubs.add(tgt)
                self.out('goto *stub_%s' % tgt)
            return
        if n == MOVIE_CMD:
            self.movie(args)
            return
        if n in IGNORED_CMDS:
            return
        self.comment('CMD%d(%s)' % (n, ', '.join(fmt_ast(a) for a in args if a[0] != 'asg')))

    def movie(self, args):
        name = next((a[1] for a in args if a[0] == 's'), None)
        if name is None:
            raise Unsupported('movie without a file name')
        base = os.path.splitext(name)[0]
        if base.lower() not in self.movies:
            self.comment('missing movie %s' % name)
            return
        self.out('s_movie "movie\\%s.mpg"' % base)
        self.cur_name = None            # s_movie removes the name plate

    # ---- driver -------------------------------------------------------------------------
    def reachable(self, sc, evs, off2k, entry):
        seen = set()
        funcs = {}
        work = [entry]
        while work:
            o = work.pop()
            k = off2k.get(o)
            if k is None:
                self.warnings.append('%s: jump into the middle of a statement at 0x%x' % (self.base, o))
                continue
            while k < len(evs) and evs[k].off not in seen:
                e = evs[k]
                seen.add(e.off)
                if e.kind == 'calll' and sc.labels[e.n] >= sc.code_mid:
                    f = self.norm(sc.labels[e.n])
                    funcs[f] = e.n
                    work.append(f)
                jumps, falls = self.successors(sc, e)
                work.extend(jumps)
                if not falls:
                    break
                k += 1
        return seen, funcs

    def convert(self, name, sc):
        self.base = name
        self.lines = []
        self.nlabel = 0
        self.cur_name = None
        self.ntmp = 0
        self.case_op = None
        evs = self.events(sc)
        starts = sorted(set(e.off for e in evs))
        startset = set(starts)

        def norm(o):
            # frame bookkeeping words produce no event; continue at the next real statement
            if o in startset:
                return o
            i = bisect.bisect_right(starts, o)
            return starts[i] if i < len(starts) else o

        self.norm = norm
        for e in evs:
            if e.kind in ('jmp', 'jf', 'jt', 'case'):
                e.target = norm(e.target)
        off2k = {}
        for k, e in enumerate(evs):
            off2k.setdefault(e.off, k)
        self.evs, self.off2k = evs, off2k
        entry = norm(sc.code_mid)
        seen, funcs = self.reachable(sc, evs, off2k, entry)
        targets = set()
        for e in evs:
            if e.off in seen:
                targets.update(self.successors(sc, e)[0])
        fstarts = sorted(set(norm(o) for o in sc.labels if o >= sc.code_mid) | {entry})
        names = {entry: '%s_start' % name}
        for o, n in funcs.items():
            names[o] = '%s_L%d' % (name, n)
        self.entry = entry
        self.lines.append('; ---- %s.spr ' % name + '-' * 40)
        for e in evs:
            if e.off not in seen:
                continue
            if e.off in names:
                self.label(names[e.off])
            elif e.off in targets:
                self.label('%s_%x' % (name, e.off))
            self.ntmp = 0
            try:
                self.emit(sc, e, names, fstarts)
            except Unsupported as ex:
                self.comment('UNSUPPORTED %s %s: %s' % (e.kind, e.n if e.n is not None else '', ex))
                self.warnings.append('%s: %s: %s' % (name, e.kind, ex))
        return self.lines

    def is_idle_loop(self, e):
        """A backward jump over statements that do nothing: the script's final "wait forever" loop."""
        kt = self.off2k.get(e.target)
        ke = self.evs.index(e)
        if kt is None or kt > ke:
            return False
        for x in self.evs[kt:ke]:
            if x.kind == 'cmd' and x.n in IGNORED_CMDS:
                continue
            if x.kind == 'eval' and fold(x.ast, CONSTS)[0] != 'asg':
                continue
            if x.kind in ('jf', 'jt') and x.cond is not None:
                c = fold(x.cond, CONSTS)
                if c[0] == 'c' and ((c[1] != 0) == (x.kind == 'jf')):
                    continue                          # condition is constant and never jumps
            return False
        return True

    def target_name(self, off, names):
        return names.get(off) or '%s_%x' % (self.base, off)

    def emit(self, sc, e, names, fstarts):
        k = e.kind
        if k == 'eval':
            a = fold(e.ast, CONSTS)
            if a[0] == 'asg' and not (a[1][0] == 'v' and a[1][1] in (0x109, 0x10a, 0x10d)):
                self.assign(a[1], a[2])
        elif k == 'cmd':
            self.command(e)
        elif k == 'calll':
            if sc.labels[e.n] >= sc.code_mid:
                for a in e.args:
                    if a[0] == 'asg':
                        self.assign(a[1], a[2])
                self.out('gosub *%s' % self.target_name(self.norm(sc.labels[e.n]), names))
                self.cur_name = None
            else:
                self.lib_call(e.n, e.args)
        elif k == 'jmp':
            if self.is_idle_loop(e):
                self.out('goto *s_chapter')          # idle loop at the end of a script
            else:
                self.out('goto *%s' % self.target_name(e.target, names))
        elif k == 'jmpl':
            self.out('goto *%s' % self.target_name(self.norm(sc.labels[e.target]), names))
        elif k in ('jf', 'jt'):
            if e.cond is None:
                raise Unsupported('condition missing')
            self.jump_if(e.cond, self.target_name(e.target, names), k == 'jt')
        elif k == 'case':
            if e.cond is not None:
                self.case_op = self.opnd(e.cond)
            if self.case_op is None:
                raise Unsupported('case without value')
            self.out('if %s == %d goto *%s' % (self.case_op, e.val, self.target_name(e.target, names)))
        elif k == 'ret':
            owner = max([o for o in fstarts if o <= e.off], default=None)
            self.out('goto *s_chapter' if owner == self.entry else 'return')


# ---------------------------------------------------------------- runtime
# Character pictures are shown as plain sprites (not the native "ld"/"cl" tachie layer, which
# does not mix well with mpegplay, see s_movie). *s_chrfile below reproduces "ld"'s own layout:
# x = (slot+1)*200 - picture_width/2 (slot 0/1/2 = left/center/right of the 800 wide screen),
# y = 0 (every picture is 600 px tall, the full screen height). The width comes from the game's
# own library code (same value the original engine itself used to centre the picture).
def chr_table(chars):
    lines = []
    for ch in sorted(chars):
        width, files = chars[ch]
        if width is None:
            continue
        for ex, f in enumerate(files):
            name = ':a;' + os.path.splitext(f)[0] + '.png'
            for slot in range(3):
                x = (slot + 1) * 200 - width // 2
                lines.append('if %%190==%d && %%191==%d && %%193==%d mov $90,"%s":mov %%192,%d'
                              % (ch, ex, slot, name, x))
    return '\n'.join(lines)


def select_code(sels):
    """One block per choice set; every option is a two cell image cut from select_mes.png."""
    out = []
    for sid in sorted(sels):
        cells = sels[sid]
        out.append('*s_sel_%d' % sid)
        n = len(cells)
        y0 = 300 - n * 46 // 2
        for j, (row, col) in enumerate(cells):
            out.append('lsp %d,":a/2,0,3;sel\\sel_%02d.png",298,%d' % (301 + j, row + 22 * (col // 2), y0 + 46 * j))
            out.append('spbtn %d,%d' % (301 + j, j + 1))
        out.append('mov %%177,%d' % n)
        out.append('goto *s_sel_wait')
    return '\n'.join(out)


def movie_entries(movies):
    """CMs that can be played: CM number order, every entry lists the movies of one CM (one is picked at random)."""
    entries = []
    for n in sorted(CM_MOVIES):
        files = [f for f in CM_MOVIES[n] if f.lower() in movies]
        if files:
            entries.append(files)
    return entries


def cm_table(entries):
    out = []
    for i, files in enumerate(entries, 1):
        if len(files) > 1:
            out.append('if %%%d==%d rnd %%187,%d' % (CM_REGISTER, i, len(files)))
        for j, f in enumerate(files):
            cond = '%%%d==%d' % (CM_REGISTER, i) + (' && %%187==%d' % j if len(files) > 1 else '')
            out.append('if %s mov $92,"movie\\%s.mpg"' % (cond, f))
    return '\n'.join(out)


def movie_play(name, movies):
    return 's_movie "movie\\%s.mpg"' % name if name.lower() in movies else ''


def build_runtime(lib, conv):
    with open(os.path.join(HERE, 'ons_runtime.txt'), encoding='utf-8') as f:
        rt = f.read().replace('\r\n', '\n')
    sels = lib.selects()
    entries = movie_entries(conv.movies)
    rt = rt.replace(';@@CHRFILE@@', chr_table(conv.chars))
    rt = rt.replace(';@@SELECTJUMP@@', '\n'.join('if %%178==%d goto *s_sel_%d' % (s, s) for s in sorted(sels)))
    rt = rt.replace(';@@SELECT@@', select_code(sels))
    rt = rt.replace(';@@CMFILE@@', cm_table(entries))
    rt = rt.replace(';@@CMCOUNT@@', 'mov %%188,%d' % len(entries))
    rt = rt.replace(';@@MOVIE_OP@@', movie_play(OPENING_MOVIE, conv.movies))
    rt = rt.replace(';@@MOVIE_ED@@', movie_play(ENDING_MOVIE, conv.movies))
    return rt


def default_scripts(src):
    names = []
    for fn in sorted(os.listdir(src)):
        b, ext = os.path.splitext(fn)
        if ext.lower() == '.spr' and (b.startswith('mei') or b == 'eye' or re.fullmatch(r'ed[1-5]', b)):
            names.append(b)
    return names


def build(src, names=None, voices=None, sounds=None, movies=None):
    """Convert the scripts in src. Returns (script text with \\n line ends, warnings, converter).

    voices / sounds: optional sets of existing file names (lower case, voices without extension);
    references to anything else are written as comments.
    movies: lower case names (no extension) of the converted movies; movies that are not listed are not played."""
    names = names or default_scripts(src)
    scripts = {n: Script(open(os.path.join(src, n + '.spr'), 'rb').read()) for n in names}
    ref = scripts.get('mei01') or next(iter(scripts.values()))
    lib = Library(ref)
    conv = Converter(lib, VarMap(), set(n.lower() for n in names), voices, sounds, movies)
    body = []
    for n in names:
        body.extend(conv.convert(n, scripts[n]))
        body.append('')
    stubs = ['; ---- scripts that are not converted (menus, movies ...) ----']
    for s in sorted(conv.stubs):
        stubs += ['*stub_%s' % s, 'goto *title', '']
    text = build_runtime(lib, conv) + '\n' + '\n'.join(body) + '\n' + '\n'.join(stubs) + '\n'
    return text, conv.warnings, conv


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--src', default=os.path.join(ROOT, 'unpacked', 'script_decoded'))
    ap.add_argument('--out', default=os.path.join(ROOT, 'onscripter', '0.txt'))
    ap.add_argument('--scripts', nargs='*')
    ap.add_argument('--encoding', default='cp932')
    a = ap.parse_args()

    mdir = os.path.join(os.path.dirname(os.path.abspath(a.src)), 'movie')
    movies = set(os.path.splitext(n)[0].lower() for n in os.listdir(mdir)) if os.path.isdir(mdir) else None
    text, warnings, _ = build(a.src, a.scripts, movies=movies)
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, 'w', encoding=a.encoding, newline='') as f:
        f.write(text.replace('\n', '\r\n'))
    print('%d lines -> %s' % (text.count('\n'), a.out))
    for w in warnings:
        print('warning:', w)


if __name__ == '__main__':
    main()
