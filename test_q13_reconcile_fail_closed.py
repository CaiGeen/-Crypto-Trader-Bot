#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Q13 reconcile_pre_launch.py 三个 fail-open —— 反例 / 修后 / 健康阳性对照（v1.2 P1）。

## 缺陷（报告 Q13 `:31,133,163-170,267-294`；两个反例已复现）

三个 fail-open 叠加，可打印「✅ 对账通过」且 rc=0（错误安全结论源）：

1. **无「交易所有持仓但本地无批次」反向检查**——4c/4d 只遍历 `local_batches`，
   本地空/漏时恒静默（反例①：交易所裸仓 + 本地无记录 → rc=0 无告警）；
2. **拉单/拉持仓失败只 print 不进 issues**（`185-189/206-207`）——全部失败仍
   rc=0「✅ 对账通过」（反例②：错误安全结论）；部分失败还会让 4d 产出误导性的
   「已手动平仓」明细（持仓拉取失败被当成无持仓）；
3. **SL 无有效性/方向/覆盖量核验**——4b 只验 id 存在；4c 方向检查是死分支
   （`pos 'LONG'` vs `batch 'BUY'` 直比恒不等 → `pass`，`275-277`）；
4. **STATE_FILE 为 CWD 相对**（`:31`）——跑错目录读到空账本 = 反例①的成因之一。

## 修法（报告第 54 行）

UNKNOWN（拉取失败）不得通过一律 rc≠0、补交易所→账本反向核对、核验实际 SL
有效性/方向/覆盖量；STATE_FILE 改脚本相对（与标准启动链 CWD 钉死一致）。

## 分组

- **反例（修前红 → 修后绿）**：
  CE1 交易所有持仓+本地无批次 → rc=1 反向核对告警；
  CE2 拉单+拉持仓全失败 → rc=1 且 UNKNOWN 进结论、不得出「✅ 对账通过」；
  CE2b 仅持仓拉取失败（本地有成交批次）→ UNKNOWN，不得出误导性「已手动平仓」；
  CE3 SL 方向错误（long 持仓挂 buy SL）→ rc=1 方向告警；
  CE4 SL 覆盖不足（SL amount < 已成交）→ rc=1 覆盖告警；
  CE5 本地 symbol 之外孤儿单（163-170 只查本地 symbol → 4a 盲区）→ rc=1；
  S0 STATE_FILE 绝对路径；S1 结构锚点。
- **健康阳性对照（恒绿）**：
  C1 正常对账（方向/覆盖/类型全对）→ rc=0 通过；
  C2 空账本 + 空交易所 → rc=0 通过（防过度告警）。

跑法：`.venv\\Scripts\\python.exe test_q13_reconcile_fail_closed.py`（rc=0 即全过）
"""
import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

import reconcile_pre_launch as rec  # noqa: E402

RESULTS = []


def report(name, passed, detail=""):
    RESULTS.append((name, passed))
    print(f"[{'PASS' if passed else 'FAIL'}] {name}\n        {detail}")


# ---------------------------------------------------------------------------
# 夹具：假交易所（离线，零网络）+ 临时账本
# ---------------------------------------------------------------------------

class _FakeExchange:
    def __init__(self, orders=None, positions=None, orders_exc=None, pos_exc=None):
        self._orders = list(orders or [])
        self._positions = list(positions or [])
        self._orders_exc = orders_exc
        self._pos_exc = pos_exc

    def fetch_open_orders(self, *a, **k):
        if self._orders_exc is not None:
            raise self._orders_exc
        # 按 symbol 过滤（如实模拟交易所）：带 symbol 参数只回该 symbol；
        # 不带 = 全量扫描
        symbol = a[0] if a and isinstance(a[0], str) else None
        if symbol is None:
            return list(self._orders)
        return [o for o in self._orders if o.get('symbol') == symbol]

    def fetch_positions(self):
        if self._pos_exc is not None:
            raise self._pos_exc
        return list(self._positions)


def _run(local_state, orders=None, positions=None, orders_exc=None, pos_exc=None):
    """跑一次 main()，返回 (rc, stdout)。local_state=None → 账本文件不存在。"""
    d = tempfile.mkdtemp(prefix='q13_')
    sp = os.path.join(d, 'trade_state.json')
    if local_state is not None:
        with open(sp, 'w', encoding='utf-8') as f:
            json.dump(local_state, f, ensure_ascii=False)
    rec.STATE_FILE = sp
    ex = _FakeExchange(orders, positions, orders_exc, pos_exc)
    rec.create_exchange = lambda: ex
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = rec.main()
    return rc, buf.getvalue()


# 健康本地批次（1 层成交，SL/TP 在场）
_LOCAL_BATCH = {
    'BTC/USDT:USDT': {
        'batch_q13': {
            'is_active': True,
            'side': 'BUY',
            'last_filled_count': 1,
            'batch_total_amount': 0.01,
            'target_amounts': [0.01],
            'current_sl_id': 'sl1',
            'tp_order_id': 'tp1',
            'entry_orders': ['e1'],
            'protection_registry': {},
            'pending_sl_orders': [],
            'sl_fail_count': {},
        }
    }
}

_POS_LONG = [{'symbol': 'BTC/USDT:USDT', 'side': 'long', 'contracts': 0.01,
              'entryPrice': 50000.0, 'unrealizedPnl': 0.0}]


def _orders(sl_side='sell', sl_amount=0.01, sl_type='STOP_MARKET'):
    return [
        {'id': 'sl1', 'symbol': 'BTC/USDT:USDT', 'type': sl_type,
         'side': sl_side, 'amount': sl_amount, 'status': 'open'},
        {'id': 'tp1', 'symbol': 'BTC/USDT:USDT', 'type': 'TAKE_PROFIT_MARKET',
         'side': 'buy', 'amount': 0.01, 'status': 'open'},
    ]


# ---------------------------------------------------------------------------
# 反例①：交易所有持仓 + 本地无批次（无反向核对 → 修前 rc=0 静默通过）
# ---------------------------------------------------------------------------

def check_ce1_bare_position_reverse():
    rc, out = _run(local_state={}, orders=[], positions=_POS_LONG)
    report(
        "CE1 裸仓反查：交易所有持仓+本地无批次 → rc=1 且『反向核对』告警"
        "（修前 rc=0「✅ 对账通过」）",
        rc == 1 and '反向核对' in out and '✅ 对账通过' not in out,
        f"rc={rc}；含『反向核对』={('反向核对' in out)}；含✅通过={('✅ 对账通过' in out)}")


# ---------------------------------------------------------------------------
# 反例②：拉单+拉持仓全失败 → 修前只 print，rc=0 错误安全结论
# ---------------------------------------------------------------------------

def check_ce2_all_fetches_fail():
    rc, out = _run(local_state={}, orders_exc=RuntimeError('rate limited'),
                   pos_exc=RuntimeError('timeout waiting'))
    report(
        "CE2 全拉取失败 → rc=1 且 UNKNOWN 进结论（修前 rc=0「✅ 对账通过」）",
        rc == 1 and 'UNKNOWN' in out and '✅ 对账通过' not in out,
        f"rc={rc}；含 UNKNOWN={('UNKNOWN' in out)}；含✅通过={('✅ 对账通过' in out)}")


# ---------------------------------------------------------------------------
# 反例②b：仅持仓拉取失败（本地有成交批次）→ 修前 4d 产出误导性「已手动平仓」
# ---------------------------------------------------------------------------

def check_ce2b_pos_fetch_fail_no_misleading_detail():
    rc, out = _run(local_state=_LOCAL_BATCH, orders=_orders(),
                   pos_exc=RuntimeError('timeout waiting'))
    report(
        "CE2b 持仓拉取失败 → UNKNOWN 结论、不得出误导性『已手动平仓』"
        "（修前 rc=1 但原因为假：把拉取失败当成无持仓）",
        rc == 1 and 'UNKNOWN' in out and '已手动平仓' not in out,
        f"rc={rc}；含 UNKNOWN={('UNKNOWN' in out)}；含『已手动平仓』={('已手动平仓' in out)}")


# ---------------------------------------------------------------------------
# 反例③：SL 方向错误（long 持仓的 SL 挂成 buy = 触发会反向开仓）→ 修前不查
# ---------------------------------------------------------------------------

def check_ce3_sl_direction():
    rc, out = _run(local_state=_LOCAL_BATCH, orders=_orders(sl_side='buy'),
                   positions=_POS_LONG)
    report(
        "CE3 SL 方向错误（long 持仓 + buy SL）→ rc=1『方向』告警"
        "（修前 rc=0：只验 id 存在）",
        rc == 1 and '方向' in out and '✅ 对账通过' not in out,
        f"rc={rc}；含『方向』={('方向' in out)}；含✅通过={('✅ 对账通过' in out)}")


# ---------------------------------------------------------------------------
# 反例④：SL 覆盖不足（SL amount=0.001 < 已成交 0.01）→ 修前不查
# ---------------------------------------------------------------------------

def check_ce4_sl_coverage():
    rc, out = _run(local_state=_LOCAL_BATCH, orders=_orders(sl_amount=0.001),
                   positions=_POS_LONG)
    report(
        "CE4 SL 覆盖不足（amount 0.001 < 已成交 0.01）→ rc=1『覆盖』告警"
        "（修前 rc=0）",
        rc == 1 and '覆盖' in out and '✅ 对账通过' not in out,
        f"rc={rc}；含『覆盖』={('覆盖' in out)}；含✅通过={('✅ 对账通过' in out)}")


# ---------------------------------------------------------------------------
# 反例⑤：本地 symbol 之外的孤儿单（163-170：symbols_to_query 只查本地 → 4a 盲区；
# 注释承诺「额外全量扫描」但未实现）
# ---------------------------------------------------------------------------

def check_ce5_orphan_outside_local_symbols():
    orders = _orders() + [
        {'id': 'orphan_eth', 'symbol': 'ETH/USDT:USDT', 'type': 'LIMIT',
         'side': 'buy', 'amount': 1.0, 'status': 'open'}]
    rc, out = _run(local_state=_LOCAL_BATCH, orders=orders, positions=_POS_LONG)
    report(
        "CE5 本地 symbol 外孤儿单 → rc=1『孤儿单』（修前只查本地 symbol → "
        "4a 盲区 rc=0「✅ 对账通过」）",
        rc == 1 and '孤儿单' in out and '✅ 对账通过' not in out,
        f"rc={rc}；含『孤儿单』={('孤儿单' in out)}；含✅通过={('✅ 对账通过' in out)}")


# ---------------------------------------------------------------------------
# 结构锚点：STATE_FILE 绝对 + 三修法关键词在场
# ---------------------------------------------------------------------------

def check_s0_state_file_absolute():
    spec = importlib.util.spec_from_file_location(
        'q13_rec_fresh', os.path.join(ROOT, 'reconcile_pre_launch.py'))
    fresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh)
    report(
        "S0 STATE_FILE 为绝对路径（修前=CWD 相对，跑错目录读空账本）",
        os.path.isabs(fresh.STATE_FILE),
        f"STATE_FILE={fresh.STATE_FILE!r}")


def check_s1_source_anchors():
    with open(os.path.join(ROOT, 'reconcile_pre_launch.py'), encoding='utf-8') as f:
        src = f.read()
    ok = ('UNKNOWN' in src and '反向核对' in src and 'target_amounts' in src
          and '_norm_side' in src)
    report(
        "S1 结构：UNKNOWN 进结论 + 反向核对 + target_amounts 覆盖量 + _norm_side 归一",
        ok,
        f"UNKNOWN={('UNKNOWN' in src)} 反向核对={('反向核对' in src)} "
        f"target_amounts={('target_amounts' in src)} _norm_side={('_norm_side' in src)}")


# ---------------------------------------------------------------------------
# 健康阳性对照
# ---------------------------------------------------------------------------

def check_c1_happy_path_still_passes():
    rc, out = _run(local_state=_LOCAL_BATCH, orders=_orders(sl_side='sell'),
                   positions=_POS_LONG)
    report(
        "C1 健康对账（方向/覆盖/类型全对）→ rc=0「✅ 对账通过」恒绿",
        rc == 0 and '✅ 对账通过' in out,
        f"rc={rc}；含✅通过={('✅ 对账通过' in out)}")


def check_c2_empty_still_passes():
    rc, out = _run(local_state={}, orders=[], positions=[])
    report(
        "C2 空账本+空交易所 → rc=0 通过（防过度告警）恒绿",
        rc == 0 and '✅ 对账通过' in out,
        f"rc={rc}；含✅通过={('✅ 对账通过' in out)}")


CHECKS = [
    check_ce1_bare_position_reverse,
    check_ce2_all_fetches_fail,
    check_ce2b_pos_fetch_fail_no_misleading_detail,
    check_ce3_sl_direction,
    check_ce4_sl_coverage,
    check_ce5_orphan_outside_local_symbols,
    check_s0_state_file_absolute,
    check_s1_source_anchors,
    check_c1_happy_path_still_passes,
    check_c2_empty_still_passes,
]


def main():
    for fn in CHECKS:
        try:
            fn()
        except Exception as e:
            import traceback
            traceback.print_exc()
            report(fn.__name__, False, f"测试自身异常: {type(e).__name__}: {e}")
    ok = sum(1 for _, p in RESULTS if p)
    print("\n" + "=" * 68)
    print(f"Q13 对账工具 fail-open 测试: {ok}/{len(RESULTS)} 通过")
    print("=" * 68)
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
