import sys
fn, frames = sys.argv[1], set(map(int, sys.argv[2].split(',')))
for l in open(fn):
    if not l.startswith('C '):
        continue
    h, b = l.split('|')
    hs = h.split()
    if int(hs[1]) not in frames:
        continue
    s = ''
    for t in b.split():
        if t == '/':
            s += '/'
            continue
        c = int(t.split(':')[0])
        s += '0' if c == 0 else 'N' if c == 13 else 'I' if c >= 14 else format(c, 'x')
    print(f"fr{hs[1]} ch{hs[2]} ws{hs[4]} | {s}")
