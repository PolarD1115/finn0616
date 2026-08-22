# -*- coding: utf-8 -*-
import re
src = open('server.py', encoding='utf-8').read()
for pat in ['tarot', '塔罗', 'question']:
    idxs = [m.start() for m in re.finditer(re.escape(pat), src)]
    lines = [src[:i].count('\n') + 1 for i in idxs]
    print('%s -> %s' % (pat, lines[:20]))
