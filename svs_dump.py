"""List the statements of a decoded SVS1 script (*.spr) in a readable form (reverse engineering aid).

Usage: python svs_dump.py <file.spr> [start_offset [end_offset]]

By default only the script's own code is listed (the shared library that precedes it is skipped);
pass 0x1020 as start offset to see the library too. Arguments that were pushed for a command or call
are shown inside the parentheses, labels of the label table are printed as L<n>:.
"""
import sys

from svs_disasm import Script, fmt_expr


def dump(sc, start=None, end=None, out=sys.stdout):
    labels = {}
    for i, off in enumerate(sc.labels):
        if off:
            labels.setdefault(off, []).append(i)
    lo = sc.code_mid if start is None else start
    hi = sc.pool if end is None else end
    pend = []
    for ins in sc.disasm():
        if ins.off < lo or ins.off >= hi:
            continue
        if ins.off in labels:
            out.write('L%s:\n' % ','.join(map(str, labels[ins.off])))
        if ins.kind == 'expr':
            pend.append(fmt_expr(sc, ins.a))
        elif ins.kind == 'push':
            continue
        elif ins.kind in ('cmd', 'calll'):
            name = 'CMD%d' % ins.a if ins.kind == 'cmd' else 'call L%d' % ins.a
            out.write('%x: %s(%s)\n' % (ins.off, name, ' ; '.join(pend)))
            pend = []
        else:
            if pend:
                out.write('%x:   expr %s\n' % (ins.off, ' ; '.join(pend)))
                pend = []
            out.write('%x: %s %s %s\n' % (ins.off, ins.kind, ins.a, '' if ins.b is None else ins.b))


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    sc = Script(open(sys.argv[1], 'rb').read())
    start = int(sys.argv[2], 0) if len(sys.argv) > 2 else None
    end = int(sys.argv[3], 0) if len(sys.argv) > 3 else None
    sys.stdout.reconfigure(encoding='utf-8')
    dump(sc, start, end)


if __name__ == '__main__':
    main()
