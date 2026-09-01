# -*- coding: utf-8 -*-
"""v6.2 上线前一次性 legacy 只读检查（不修改任何状态，零 API）。

裁定来源（ChatGPT r6 交叉审核）：
  运行时不做自动兜底；部署条件 = LEGACY_UNRECOVERABLE == 0。

判据（与 r6 §6 / _pending_entry_ids_for_gate 契约一致）：
  对每个 is_active 批次，若同时满足：
    A. len(target_amounts) > last_filled_count   （存在未成交计划层）
    B. len(entry_orders) == last_filled_count    （🗑️ 截断签名）
    C. registry 无法恢复完整 ENTRY 链             （r6 helper 判定）
  → 计为 LEGACY_UNRECOVERABLE 命中。

只读：本脚本绝不写 trade_state.json / 任何状态文件 / 任何交易所 API。
"""
import ast
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, 'trade_state.json')
HELPER = os.path.join(HERE, '送审附件_v6.2', 'new_helpers_v62.py')


def load_recoverable_impl():
    """AST 提取 _pending_entry_ids_for_gate（与 production 同一实现，零 import 副作用）。"""
    import textwrap
    tree = ast.parse(open(HELPER, encoding='utf-8').read())
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == '_pending_entry_ids_for_gate':
            seg = textwrap.dedent(ast.get_source_segment(
                open(HELPER, encoding='utf-8').read(), n))
            ns = {}
            exec(compile(seg, HELPER, 'exec'), ns)
            return ns['_pending_entry_ids_for_gate']
    raise LookupError('_pending_entry_ids_for_gate not found')


def main():
    if not os.path.exists(STATE):
        print(f'ℹ️ {STATE} 不存在 → 无任何批次 → LEGACY_UNRECOVERABLE = 0（可上线）')
        return 0
    try:
        with open(STATE, encoding='utf-8') as f:
            data = json.load(f)
    except Exception as e:
        print(f'❌ trade_state.json 读取失败（账本损坏，人工处置后再上线）: {e}')
        return 1
    if not isinstance(data, dict):
        print('❌ trade_state.json 根节点非 dict（账本损坏，人工处置后再上线）')
        return 1

    recoverable_impl = load_recoverable_impl()

    # FakeSelf 最小桩：_pending_entry_ids_for_gate 是纯函数（零 API、零锁）
    class _Stub:
        pass

    hits = []
    total_active = 0
    for symbol, batches in (data or {}).items():
        if not isinstance(batches, dict):
            continue
        for batch_id, b in batches.items():
            if not isinstance(b, dict) or not b.get('is_active'):
                continue
            total_active += 1
            ta = b.get('target_amounts') or []
            eo = b.get('entry_orders') or []
            lfc = int(b.get('last_filled_count') or 0)
            cond_a = len(ta) > lfc
            cond_b = len(eo) == lfc
            if not (cond_a and cond_b):
                continue                      # 不满足 🗑️ 截断签名 → 非 legacy 命中
            stub = _Stub()
            _, rec, _chain = recoverable_impl(stub, symbol, batch_id, b, lfc)
            if not rec:
                hits.append((symbol, batch_id, len(ta), lfc, len(eo)))

    print('=' * 66)
    print('v6.2 部署前 legacy 只读检查（LEGACY_UNRECOVERABLE）')
    print('=' * 66)
    print(f'  active 批次总数: {total_active}')
    print(f'  命中 LEGACY_UNRECOVERABLE: {len(hits)}')
    for sym, bid, nta, lfc, neo in hits:
        print(f'    - {sym} / {bid}: target={nta} 层, lfc={lfc}, '
              f'entry_orders={neo} 张, registry 不可恢复')
    print('-' * 66)
    if hits:
        print('❌ 部署条件不满足（LEGACY_UNRECOVERABLE != 0）')
        print('   处置（人工三选一）：')
        print('   ① 等该批次自然结束后再升级')
        print('   ② 人工在交易所核对并撤掉/确认剩余 ENTRY')
        print('   ③ 人工提供精确 order-id ↔ layer 映射做一次性 registry backfill')
        return 1
    print('✅ 部署条件满足：LEGACY_UNRECOVERABLE == 0（可上线）')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
