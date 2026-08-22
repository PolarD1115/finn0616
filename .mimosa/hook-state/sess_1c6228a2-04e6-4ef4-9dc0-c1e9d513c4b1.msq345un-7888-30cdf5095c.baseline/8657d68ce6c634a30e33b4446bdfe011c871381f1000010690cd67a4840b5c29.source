# -*- coding: utf-8 -*-
"""临时扫描脚本：定位 server.py 关键符号行号（阶段7审查用，审查后删除）"""
import re

src = open('server.py', encoding='utf-8').read()
lines_all = src.splitlines()
out = ['total lines: %d' % len(lines_all)]
pats = [
    'get_latest_diary', 'manage_memory_house', 'save_expense',
    'check_expense_report', 'manage_piggy_bank', 'mcp_error_handler',
    'supabase =', 'def wallet_', 'def house_', 'def cat_',
    'cat_tick', 'to_thread', 'rpc_wallet', 'rpc_house', 'rpc_cat',
    'agent_outbound', 'async def', 'PET_HOUSE',
]
for pat in pats:
    idxs = [m.start() for m in re.finditer(re.escape(pat), src)]
    lines = [src[:i].count('\n') + 1 for i in idxs]
    out.append('%s -> %s' % (pat, str(lines[:30])))
open('_scan_out.txt', 'w', encoding='utf-8').write('\n'.join(out))
print('scan done')
