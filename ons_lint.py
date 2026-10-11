#!/usr/bin/env python3
"""Small static check for the generated ONScripter script.

Reports duplicate labels, jumps to undefined labels, user commands (defsub) without a label,
and a summary of the commands that are used. Usage: python ons_lint.py [0.txt]
"""
import collections
import os
import re
import sys

path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), '..', 'onscripter', '0.txt')
raw = open(path, 'rb').read()
try:
    text = raw.decode('utf-8').split('\n')
except UnicodeDecodeError:
    text = raw.decode('cp932').split('\n')

defined = collections.Counter()
used = collections.defaultdict(list)
defsubs = []
cmds = collections.Counter()
for n, line in enumerate(text, 1):
    line = line.rstrip('\r')
    if not line or line.startswith(';'):
        continue
    m = re.match(r'\*([A-Za-z0-9_]+)\s*$', line)
    if m:
        defined[m.group(1).lower()] += 1
        continue
    m = re.match(r'defsub\s+(\w+)', line)
    if m:
        defsubs.append(m.group(1))
    for t in re.findall(r'(?:goto|gosub|,)\s*\*([A-Za-z0-9_]+)', line):
        used[t.lower()].append(n)
    first = line.split()[0]
    cmds['<text>' if not re.match(r'[A-Za-z_]', first) else first] += 1

bad = 0
for k, v in defined.items():
    if v > 1:
        print('duplicate label', k)
        bad += 1
for k, lines in used.items():
    if k not in defined:
        print('undefined label', k, 'used at line', lines[:3])
        bad += 1
for d in defsubs:
    if d.lower() not in defined:
        print('defsub without label', d)
        bad += 1
print('labels: %d, jumps: %d, problems: %d' % (len(defined), sum(len(v) for v in used.values()), bad))
print('statements:', ', '.join('%s=%d' % kv for kv in cmds.most_common(30)))
