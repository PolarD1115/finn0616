# -*- coding: utf-8 -*-
"""扫描 agent_outbound 在代码库中的消费约定"""
import os, re
root = '.'
for fn in ['gateway.py', 'server.py', 'napcat.py', 'background.py', 'heartbeat.py', 'home_system.py', 'aggregator.py', 'desire_engine.py', 'desire_bridge.py', 'emotion_engine.py']:
    if not os.path.exists(fn):
        continue
    src = open(fn, encoding='utf-8').read()
    hits = [src[:m.start()].count('\n') + 1 for m in re.finditer(r'agent_outbound', src)]
    if hits:
        print('%s -> %s' % (fn, hits[:20]))
