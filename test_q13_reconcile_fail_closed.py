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


def _orders(sl_side='sell', sl_amount=0.01, sl_type='STOP_MARKET', sl_info=None):
    # R7（外部评审）：SL 夹具带平仓语义（单向单 reduceOnly=true，与生产创建
    # 路径一致——trader 4172/6686 单向 sl_params['reduceOnly']=True）。
    return [
        {'id': 'sl1', 'symbol': 'BTC/USDT:USDT', 'type': sl_type,
         'side': sl_side, 'amount': sl_amount, 'status': 'open',
         'info': dict(sl_info if sl_info is not None else {'reduceOnly': 'true'})},
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


# ---------------------------------------------------------------------------
# R2（独立复审）：SL 身份/覆盖——id 命中但 symbol 错配、或 SL 仅覆盖账本
# 目标量而实际持仓更大 → 修前 rc=0（及修后 CE9 补强），修后必红
# ---------------------------------------------------------------------------

def check_r2_sl_symbol_mismatch():
    orders = [dict(_orders()[0], symbol='ETH/USDT:USDT'), _orders()[1]]
    rc, out = _run(local_state=_LOCAL_BATCH, orders=orders, positions=_POS_LONG)
    report(
        "R2a SL 单挂错 symbol → rc=1『身份错配』（修前按 id 命中即通过）",
        rc == 1 and '身份错配' in out and '✅ 对账通过' not in out,
        f"rc={rc}；含『身份错配』={('身份错配' in out)}；含✅通过={('✅ 对账通过' in out)}")


def check_r2_sl_undercovers_real_position():
    # 账本目标 0.01 & SL 0.01 都“合法”，但交易所实际持仓 0.1 —— 90% 无保护
    positions = [dict(_POS_LONG[0], contracts=0.1)]
    rc, out = _run(local_state=_LOCAL_BATCH, orders=_orders(), positions=positions)
    report(
        "R2b SL 只覆盖账本量、实际持仓 10 倍 → rc=1『覆盖不足』（修前恒过）",
        rc == 1 and '覆盖不足' in out and '✅ 对账通过' not in out,
        f"rc={rc}；含『覆盖不足』={('覆盖不足' in out)}；含✅通过={('✅ 对账通过' in out)}")


# ---------------------------------------------------------------------------
# R3（独立复审）：方向/数量字段缺失或非法 → UNKNOWN 必须进结论，不得静默放行
# ---------------------------------------------------------------------------

def check_r3_sl_side_missing():
    orders = [dict(_orders()[0], side=''), _orders()[1]]
    rc, out = _run(local_state=_LOCAL_BATCH, orders=orders, positions=_POS_LONG)
    report(
        "R3a SL 缺 side 字段 → rc=1『缺 side』（修前方向检查恒真，静默过）",
        rc == 1 and '缺 side' in out and '✅ 对账通过' not in out,
        f"rc={rc}；含『缺 side』={('缺 side' in out)}；含✅通过={('✅ 对账通过' in out)}")


def check_r3_sl_amount_nan():
    orders = [dict(_orders()[0], amount=float('nan')), _orders()[1]]
    rc, out = _run(local_state=_LOCAL_BATCH, orders=orders, positions=_POS_LONG)
    report(
        "R3b SL amount=NaN → rc=1『非法』（修前 NaN 比较恒假，静默过）",
        rc == 1 and '非法' in out and '✅ 对账通过' not in out,
        f"rc={rc}；含『非法』={('非法' in out)}；含✅通过={('✅ 对账通过' in out)}")


# ---------------------------------------------------------------------------
# R4（独立复审）：hedge 同 symbol 双持仓 —— 旧“首个 symbol 匹配+break”会把
# long 持仓施加到 SELL 批次报方向冲突（健康假阳性）；修后同向匹配 → rc=0
# ---------------------------------------------------------------------------

def check_r4_healthy_hedge_no_false_positive():
    local = {
        'BTC/USDT:USDT': {
            'batch_L': dict(_LOCAL_BATCH['BTC/USDT:USDT']['batch_q13'],
                            side='BUY', current_sl_id='sl_l', tp_order_id='tp_l',
                            entry_orders=['e1']),
            'batch_S': dict(_LOCAL_BATCH['BTC/USDT:USDT']['batch_q13'],
                            side='SELL', current_sl_id='sl_s', tp_order_id='tp_s',
                            entry_orders=['e2']),
        }
    }
    positions = [
        {'symbol': 'BTC/USDT:USDT', 'side': 'long', 'contracts': 0.01,
         'entryPrice': 50000.0, 'unrealizedPnl': 0.0},
        {'symbol': 'BTC/USDT:USDT', 'side': 'short', 'contracts': 0.01,
         'entryPrice': 50000.0, 'unrealizedPnl': 0.0},
    ]
    orders = [
        # R7：hedge 单带 positionSide（生产对冲创建路径 13279/14060）
        {'id': 'sl_l', 'symbol': 'BTC/USDT:USDT', 'type': 'STOP_MARKET',
         'side': 'sell', 'amount': 0.01, 'status': 'open',
         'info': {'positionSide': 'LONG'}},
        {'id': 'tp_l', 'symbol': 'BTC/USDT:USDT', 'type': 'TAKE_PROFIT_MARKET',
         'side': 'buy', 'amount': 0.01, 'status': 'open'},
        {'id': 'sl_s', 'symbol': 'BTC/USDT:USDT', 'type': 'STOP_MARKET',
         'side': 'buy', 'amount': 0.01, 'status': 'open',
         'info': {'positionSide': 'SHORT'}},
        {'id': 'tp_s', 'symbol': 'BTC/USDT:USDT', 'type': 'TAKE_PROFIT_MARKET',
         'side': 'sell', 'amount': 0.01, 'status': 'open'},
    ]
    rc, out = _run(local_state=local, orders=orders, positions=positions)
    report(
        "R4 健康 hedge（同 symbol 双向持仓、各自保护齐全）→ rc=0（修前误报 rc=1）",
        rc == 0 and '✅ 对账通过' in out,
        f"rc={rc}；含✅通过={('✅ 对账通过' in out)}")


def check_r2c_same_direction_multibatch_aggregate_ok():
    # ChatGPT 复核要点：A 批持仓 0.01、B 批持仓 0.02（同方向），各自 SL 各自覆盖 → 总仓 0.03
    # 必须判健康；逐批 SL>=总仓 的口径会把两个健康批次全部误判。
    local = {
        'BTC/USDT:USDT': {
            'batch_A': dict(_LOCAL_BATCH['BTC/USDT:USDT']['batch_q13'],
                            side='BUY', current_sl_id='sl_a', tp_order_id='tp_a',
                            entry_orders=['e1'], target_amounts=[0.01], last_filled_count=1),
            'batch_B': dict(_LOCAL_BATCH['BTC/USDT:USDT']['batch_q13'],
                            side='BUY', current_sl_id='sl_b', tp_order_id='tp_b',
                            entry_orders=['e2'], target_amounts=[0.02], last_filled_count=1),
        }
    }
    positions = [{'symbol': 'BTC/USDT:USDT', 'side': 'long', 'contracts': 0.03,
                  'entryPrice': 50000.0, 'unrealizedPnl': 0.0}]
    orders = [
        {'id': 'sl_a', 'symbol': 'BTC/USDT:USDT', 'type': 'STOP_MARKET',
         'side': 'sell', 'amount': 0.01, 'status': 'open',
         'info': {'reduceOnly': 'true'}},
        {'id': 'tp_a', 'symbol': 'BTC/USDT:USDT', 'type': 'TAKE_PROFIT_MARKET',
         'side': 'buy', 'amount': 0.01, 'status': 'open'},
        {'id': 'sl_b', 'symbol': 'BTC/USDT:USDT', 'type': 'STOP_MARKET',
         'side': 'sell', 'amount': 0.02, 'status': 'open',
         'info': {'reduceOnly': 'true'}},
        {'id': 'tp_b', 'symbol': 'BTC/USDT:USDT', 'type': 'TAKE_PROFIT_MARKET',
         'side': 'buy', 'amount': 0.02, 'status': 'open'},
    ]
    rc, out = _run(local_state=local, orders=orders, positions=positions)
    report(
        "R2c 同向双批各自 SL 聚合覆盖总仓 → rc=0（逐批 SL>=总仓 的口径会误判健康）",
        rc == 0 and '✅ 对账通过' in out,
        f"rc={rc}；含✅通过={('✅ 对账通过' in out)}")


# ---------------------------------------------------------------------------
# R7（外部评审反例）：positionSide + 平仓语义——与仓库现有止损判据对齐
# （trader _check_protection_order_validity 7188-7200：hedge 看 positionSide，
#   单向看 reduceOnly/closePosition）。修前 4c 完全不看 info → 反例 rc=0。
# ---------------------------------------------------------------------------

def check_r7_sl_position_side_mismatch():
    # 外部评审实测反例：long 持仓 + sell SL，但 SL info.positionSide=SHORT——
    # 该单平的是空仓、对本仓零保护，修前仍 rc=0「✅ 对账通过」。
    orders = [dict(_orders()[0], info={'positionSide': 'SHORT', 'reduceOnly': 'false'}),
              _orders()[1]]
    rc, out = _run(local_state=_LOCAL_BATCH, orders=orders, positions=_POS_LONG)
    report(
        "R7a SL positionSide=SHORT 在 long 持仓 → rc=1『positionSide』"
        "（修前 rc=0「✅ 对账通过」，错误 Hedge 保护放行）",
        rc == 1 and 'positionSide' in out and '✅ 对账通过' not in out,
        f"rc={rc}；含『positionSide』={('positionSide' in out)}；含✅通过={('✅ 对账通过' in out)}")


def check_r7_sl_close_semantics_missing():
    # 平仓语义缺失：无 positionSide 且 reduceOnly/closePosition 均非 true
    # = 开仓单不是保护单（与 7196-7200 判据一致），修前不查。
    orders = [dict(_orders()[0], info={'reduceOnly': 'false'}), _orders()[1]]
    rc, out = _run(local_state=_LOCAL_BATCH, orders=orders, positions=_POS_LONG)
    report(
        "R7b SL 缺平仓语义（reduceOnly/closePosition 均非 true）→ rc=1『平仓语义』"
        "（修前 rc=0）",
        rc == 1 and '平仓语义' in out and '✅ 对账通过' not in out,
        f"rc={rc}；含『平仓语义』={('平仓语义' in out)}；含✅通过={('✅ 对账通过' in out)}")


def check_r7c_healthy_position_side_long():
    # 健康对照：long 持仓 + sell SL + positionSide=LONG → 必须保持 rc=0
    orders = [dict(_orders()[0], info={'positionSide': 'LONG', 'reduceOnly': 'false'}),
              _orders()[1]]
    rc, out = _run(local_state=_LOCAL_BATCH, orders=orders, positions=_POS_LONG)
    report(
        "R7c 健康 positionSide=LONG 在 long 持仓 → rc=0（防过度告警）恒绿",
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
    check_r2_sl_symbol_mismatch,
    check_r2_sl_undercovers_real_position,
    check_r3_sl_side_missing,
    check_r3_sl_amount_nan,
    check_r4_healthy_hedge_no_false_positive,
    check_r2c_same_direction_multibatch_aggregate_ok,
    check_r7_sl_position_side_mismatch,
    check_r7_sl_close_semantics_missing,
    check_r7c_healthy_position_side_long,
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
