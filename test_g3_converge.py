# -*- coding: utf-8 -*-
"""
P0 Batch A 专项测试：G1/G2/G3b/G3a/N14-守卫 直接驱动验证（2026-08-28）

背景：回放测试（test_close_race_replay.py）的冻结层在 R14 之前拦截，
G1/G2/G3b/G3a 的执行路径（create 已发出后的竞态收敛）在回放中【从未触达】——
本文件直接驱动六个新 helper + 终态守卫 + F3 早返回，补齐 Batch A 的行为覆盖。

覆盖矩阵（对应规格 v3 §9 终审硬约束）：
  G3b _commit_protection_with_g3（硬约束① 锁内重读）：
    C1 正常 commit / C2 close_phase=1 拒绝 / C3 close_phase=2 拒绝 / C4 pending_close belt
    / C5 批次缺失 / C6 TOCTOU 封死（gate 过后磁盘被并发改写）/ C7 锁真实持有（B 阻塞至 A 释放）
  G3a _g3a_converge_race_order（硬约束② filled+amount+status 联合判定）：
    A1 FILLED / A2 冲突：canceled 但有成交事实 → filled（数量事实第一优先级）
    / A3 PARTIALLY_FILLED 撤余量 / A4 终态无成交 / A5 open 无成交 → cancel
    / A6 fetch UNKNOWN → PENDING_VERIFY+hard_locked+critical / A7 撤单失败 → HARD_LOCK
    / A8 -2011/Unknown order 视同已收敛
  G2 _final_pre_create_check：T1 允许 / T2 批次缺失 / T3 close_phase / T4 pending_close
  G1 _assert_create_allowed：D1 批次缺失(require_live_batch) / D2 平仓流程 / D3 PROGRAMMATIC_CANCELED
    禁建（含 replace 豁免不可用）/ D4 FAILED 仍放行（既有语义无回归）/ D5 无条目放行
  终态守卫 _update_registry：E1 PROGRAMMATIC_CANCELED 拒绝转出 / E2 同态回写（改 reason）放行
  F3 _adjudicate_recreate_before_repair：H1 PROGRAMMATIC_CANCELED → ('hold', None)

运行：.venv/Scripts/python.exe test_g3_converge.py（ccxt 只在项目 .venv）
预期：GREEN——全部 PASS 退出码 0。任何 FAIL = Batch A 实现回退/偏离规格，退出码 1。
测试基建惯例：unbound-method + fake self；MagicMock 数值比较必炸 → 数值属性绑定真实值。
"""
import os
import sys
import tempfile
import threading
import time
import traceback
from unittest import mock

import ccxt

import trader_260725
from trader_260725 import CryptoTrader

SYMBOL = "BTC/USDT:USDT"
BATCH = "batch_g3"
IDENT = f"{BATCH}|TP|L1|LONG"

RESULTS = []


def check(name, passed, detail=""):
    RESULTS.append((name, bool(passed), str(detail)))
    print(f"  [{'PASS' if passed else 'FAIL'}] {name}" + (f"\n        → {detail}" if detail else ""))
    return bool(passed)


# =====================================================================
# 基建：MemStateStore / FakeExchange / make_fake（沿回放测试惯例，精简版）
# =====================================================================
class MemStateStore:
    def __init__(self, initial):
        self._data = initial
        self._lock = threading.Lock()

    def load(self):
        with self._lock:
            return {s: {b: dict(d) for b, d in bs.items()} for s, bs in self._data.items()}

    def persist(self, all_states):
        with self._lock:
            self._data = dict(all_states)


class FakeExchange:
    """可编程交易所桩：fetch_order 返回预设订单或抛预设异常；cancel 同理。"""

    def __init__(self):
        self.fetch_order_result = None      # dict 或 Exception
        self.cancel_exc = None              # None=成功 / Exception
        self.cancel_calls = []
        self.fetch_calls = []
        self.position_amt = 0.0

    def fetch_order(self, order_id, symbol=None, params=None, **kw):
        self.fetch_calls.append(str(order_id))
        if isinstance(self.fetch_order_result, Exception):
            raise self.fetch_order_result
        r = dict(self.fetch_order_result)
        r.setdefault('id', str(order_id))
        return r

    def cancel_order(self, order_id, symbol=None, params=None, **kw):
        self.cancel_calls.append(str(order_id))
        if self.cancel_exc is not None:
            raise self.cancel_exc
        return {'id': str(order_id), 'status': 'canceled'}

    def fetch_positions(self, symbols=None, params=None, **kw):
        return [{'symbol': SYMBOL, 'side': 'long', 'contracts': self.position_amt,
                 'positionAmt': self.position_amt, 'info': {'symbol': 'BTCUSDT'}}]


def _bind_real(fake, name):
    fn = getattr(CryptoTrader, name)
    setattr(fake, name, lambda *a, **k: fn(fake, *a, **k))


def make_batch(**overrides):
    b = {
        'is_active': True, 'batch_id': BATCH, 'symbol': SYMBOL, 'side': 'BUY',
        'close_phase': 0, 'pending_close': False, 'user_modified': False,
        'protection_registry': {},
    }
    b.update(overrides)
    return {SYMBOL: {BATCH: b}}


def make_fake(store, ex):
    fake = mock.MagicMock()
    fake._safe_api_call = lambda fn, *a, **k: fn(*a, **k)   # 直通
    fake._api_cooldown_until = 0                             # 数值比较必炸教训
    fake.exchange = ex
    fake.sent = []
    fake.send_tg_notification = lambda text, **kw: fake.sent.append(
        (kw.get('level', 'info'), str(text)[:80]))
    fake._state_lock = threading.Lock()                      # 生产同款非重入锁
    fake.load_all_states = lambda: store.load()
    fake._persist_states = lambda all_s: store.persist(all_s)
    fake.save_batch_state = lambda s, b, d: CryptoTrader.save_batch_state(fake, s, b, d)
    # P0 Batch C（2026-08-29）：save/clear 重写后新增 helper —— 未绑定时
    # _merge_batch_state 返回 MagicMock 污染 store（batch 落盘为空 dict，registry
    # 断言全空 = 假回归第 5 次实证）；_load_tombstones 未绑定时返回 MagicMock
    # 恰好无害（isinstance dict=False）但语义不可靠 → 全量绑定真实实现。
    fake.tombstone_file = os.path.join(tempfile.gettempdir(),
                                       f"tomb_g3_{os.getpid()}_{id(fake)}.json")
    for name in ('_load_tombstones', '_persist_tombstones', '_prune_tombstones',
                 '_collect_batch_order_ids', '_merge_batch_state'):
        _bind_real(fake, name)
    for name in ('_update_registry', '_assert_create_allowed', '_final_pre_create_check',
                 '_commit_protection_with_g3', '_g3a_converge_race_order',
                 '_g3_cancel_race_order', '_g3_log_position_recheck',
                 '_find_registry_identity_by_order_id', '_adjudicate_recreate_before_repair',
                 '_get_current_position_amt'):
        _bind_real(fake, name)
    fake._gate_alert_notify = lambda *a, **k: None
    fake._gate_alert_clear = lambda *a, **k: None
    return fake


def reg_entry(store, identity=IDENT):
    b = store.load().get(SYMBOL, {}).get(BATCH) or {}
    return dict((b.get('protection_registry') or {}).get(identity) or {})


# =====================================================================
# G3b _commit_protection_with_g3
# =====================================================================
def test_g3b():
    print("\n" + "=" * 70)
    print("G3b _commit_protection_with_g3（硬约束①：锁内重读，禁调用方旧快照）")
    print("=" * 70)

    # C1 正常 commit：registry 写 CONFIRMED + order_id + id_known
    store = MemStateStore(make_batch())
    fake = make_fake(store, FakeExchange())
    r = CryptoTrader._commit_protection_with_g3(fake, SYMBOL, BATCH, IDENT, 'ord_c1')
    e = reg_entry(store)
    check("C1 正常批次 commit → 'committed' + registry CONFIRMED/order_id/id_known 落盘",
          r == 'committed' and e.get('state') == 'CONFIRMED' and e.get('order_id') == 'ord_c1'
          and e.get('id_known') is True,
          f"r={r!r}, registry={e}")

    # C2 close_phase=1 → g3_triggered，registry 不写
    store = MemStateStore(make_batch(close_phase=1))
    fake = make_fake(store, FakeExchange())
    r = CryptoTrader._commit_protection_with_g3(fake, SYMBOL, BATCH, IDENT, 'ord_c2')
    check("C2 close_phase=1 → 'g3_triggered'，未写 CONFIRMED",
          r == 'g3_triggered' and reg_entry(store).get('state') is None,
          f"r={r!r}, registry={reg_entry(store)}")

    # C3 close_phase=2（结算中）→ g3_triggered
    store = MemStateStore(make_batch(close_phase=2))
    fake = make_fake(store, FakeExchange())
    r = CryptoTrader._commit_protection_with_g3(fake, SYMBOL, BATCH, IDENT, 'ord_c3')
    check("C3 close_phase=2（CLOSE_SETTLING）→ 'g3_triggered'",
          r == 'g3_triggered', f"r={r!r}")

    # C4 legacy pending_close belt
    store = MemStateStore(make_batch(pending_close=True))
    fake = make_fake(store, FakeExchange())
    r = CryptoTrader._commit_protection_with_g3(fake, SYMBOL, BATCH, IDENT, 'ord_c4')
    check("C4 legacy pending_close=True（belt）→ 'g3_triggered'",
          r == 'g3_triggered', f"r={r!r}")

    # C5 批次缺失（已 clear）→ g3_triggered
    store = MemStateStore({SYMBOL: {}})
    fake = make_fake(store, FakeExchange())
    r = CryptoTrader._commit_protection_with_g3(fake, SYMBOL, BATCH, IDENT, 'ord_c5')
    check("C5 批次缺失（已清理）→ 'g3_triggered'", r == 'g3_triggered', f"r={r!r}")

    # C6 TOCTOU 封死：调用前（gate 已过之后）磁盘被并发改写 close_phase=1
    #    ——helper 签名不接收快照、锁内 load_all_states 重读 → 必然读到最新 → 拒绝
    store = MemStateStore(make_batch())
    fake = make_fake(store, FakeExchange())
    # 模拟另一线程（close 入口）在 create 已发出后、commit 前落盘 close_phase=1
    _snap = store.load()                    # 调用方旧快照（close_phase=0）
    _snap[SYMBOL][BATCH]['close_phase'] = 1
    store.persist(_snap)
    r = CryptoTrader._commit_protection_with_g3(fake, SYMBOL, BATCH, IDENT, 'ord_c6')
    check("C6 TOCTOU 封死（gate 后磁盘并发改写 close_phase=1 → commit 拒绝）",
          r == 'g3_triggered' and reg_entry(store).get('state') is None,
          f"r={r!r}（锁内重读禁旧快照 → 陈旧 gate 放行无法穿透 commit）")

    # C7 锁真实持有：线程 A 持 _state_lock → B 的 commit 阻塞；A 释放后 B 完成
    store = MemStateStore(make_batch())
    fake = make_fake(store, FakeExchange())
    fake._state_lock.acquire()
    out = {}

    def _worker():
        out['r'] = CryptoTrader._commit_protection_with_g3(fake, SYMBOL, BATCH, IDENT, 'ord_c7')

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    time.sleep(0.5)
    blocked = t.is_alive() and 'r' not in out
    fake._state_lock.release()
    t.join(5)
    check("C7 _state_lock 真实持有（持锁期间 commit 阻塞，释放后完成 'committed'）",
          blocked and out.get('r') == 'committed'
          and reg_entry(store).get('order_id') == 'ord_c7',
          f"阻塞期存活={blocked}, r={out.get('r')!r}")


# =====================================================================
# G3a _g3a_converge_race_order
# =====================================================================
def _g3a_case(name, order_result, cancel_exc=None, expect=None,
              expect_state=None, expect_reason_prefix=None, expect_critical=False,
              expect_hard_lock=False, position_amt=0.0):
    store = MemStateStore(make_batch(close_phase=1))   # 竞态背景：批次已进入平仓
    ex = FakeExchange()
    ex.fetch_order_result = order_result
    ex.cancel_exc = cancel_exc
    ex.position_amt = position_amt
    fake = make_fake(store, ex)
    r = CryptoTrader._g3a_converge_race_order(fake, SYMBOL, BATCH, IDENT, 'ord_race')
    e = reg_entry(store)
    criticals = [s for lvl, s in fake.sent if lvl == 'critical']
    ok = (r == expect
          and (expect_state is None or e.get('state') == expect_state)
          and (expect_reason_prefix is None
               or str(e.get('terminated_reason', '')).startswith(expect_reason_prefix))
          and (not expect_critical or len(criticals) >= 1)
          and (expect_hard_lock is False or e.get('hard_locked') is True))
    check(name, ok,
          f"r={r!r}（期望 {expect!r}）, registry state={e.get('state')!r}, "
          f"reason={e.get('terminated_reason')!r}, hard_locked={e.get('hard_locked')}, "
          f"critical={len(criticals)}, cancel_calls={ex.cancel_calls}")


def test_g3a():
    print("\n" + "=" * 70)
    print("G3a _g3a_converge_race_order（硬约束②：filled+amount+status 联合判定）")
    print("=" * 70)

    # A1 FILLED：closed + filled==amount → filled，核账，非 HARD_LOCK
    _g3a_case("A1 FILLED（closed+filled=amount）→ 'filled' + PROGRAMMATIC_CANCELED(g3_race_filled)",
              {'status': 'closed', 'filled': 0.002, 'amount': 0.002},
              expect='filled', expect_state='PROGRAMMATIC_CANCELED',
              expect_reason_prefix='g3_race_filled@', position_amt=0.0)

    # A2 硬约束② 冲突：status=canceled 但 filled=amount>0 → 按成交事实处理 → filled
    _g3a_case("A2 冲突（status=canceled 但有成交事实）→ 'filled'（数量事实第一优先级）",
              {'status': 'canceled', 'filled': 0.002, 'amount': 0.002},
              expect='filled', expect_state='PROGRAMMATIC_CANCELED',
              expect_reason_prefix='g3_race_filled@')

    # A3 PARTIALLY_FILLED：open + 0<filled<amount → 撤余量 → partial
    _g3a_case("A3 PARTIALLY_FILLED（open+filled<amount）→ 'partial' + 撤余量",
              {'status': 'open', 'filled': 0.001, 'amount': 0.002},
              expect='partial', expect_state='PROGRAMMATIC_CANCELED',
              expect_reason_prefix='g3_race_partial_filled@')

    # A4 终态无成交：canceled + filled=0 → terminal
    _g3a_case("A4 终态无成交（canceled+filled=0）→ 'terminal'",
              {'status': 'canceled', 'filled': 0.0, 'amount': 0.002},
              expect='terminal', expect_state='PROGRAMMATIC_CANCELED',
              expect_reason_prefix='g3_race_terminal_canceled')

    # A5 open 无成交 → cancel → canceled
    _g3a_case("A5 open 无成交 → 'canceled'（撤单收敛）",
              {'status': 'open', 'filled': 0.0, 'amount': 0.002},
              expect='canceled', expect_state='PROGRAMMATIC_CANCELED',
              expect_reason_prefix='g3_race_canceled')

    # A6 fetch UNKNOWN（NetworkError，非 ExchangeError）→ unknown + PENDING_VERIFY + hard_lock + critical
    _g3a_case("A6 fetch UNKNOWN（NetworkError）→ 'unknown' + PENDING_VERIFY + hard_locked + critical",
              ccxt.NetworkError('connect timeout'),
              expect='unknown', expect_state='PENDING_VERIFY',
              expect_reason_prefix='g3_race_fetch_unknown',
              expect_critical=True, expect_hard_lock=True)

    # A7 撤单失败（非 -2011 ExchangeError）→ cancel_failed + HARD_LOCK + critical
    _g3a_case("A7 撤单失败（ExchangeError 非-2011）→ 'cancel_failed' + HARD_LOCK + critical",
              {'status': 'open', 'filled': 0.0, 'amount': 0.002},
              cancel_exc=ccxt.ExchangeError('order reject boom'),
              expect='cancel_failed', expect_state='HARD_LOCK',
              expect_reason_prefix='g3_race_cancel_failed',
              expect_critical=True, expect_hard_lock=True)

    # A8 -2011/Unknown order 视同已收敛
    _g3a_case("A8 cancel 撞 Unknown order（-2011 语义）→ 视同已收敛 'canceled'",
              {'status': 'open', 'filled': 0.0, 'amount': 0.002},
              cancel_exc=ccxt.OrderNotFound('Unknown order sent ord_race'),
              expect='canceled', expect_state='PROGRAMMATIC_CANCELED',
              expect_reason_prefix='g3_race_canceled')

    # A9 部分成交撤余量失败 → cancel_failed + HARD_LOCK
    _g3a_case("A9 PARTIALLY_FILLED 撤余量失败 → 'cancel_failed' + HARD_LOCK + critical",
              {'status': 'open', 'filled': 0.001, 'amount': 0.002},
              cancel_exc=ccxt.ExchangeError('cancel reject'),
              expect='cancel_failed', expect_state='HARD_LOCK',
              expect_reason_prefix='g3_race_partial_cancel_failed@',
              expect_critical=True, expect_hard_lock=True)


# =====================================================================
# G2 _final_pre_create_check / G1 _assert_create_allowed
# =====================================================================
def test_gates():
    print("\n" + "=" * 70)
    print("G2 _final_pre_create_check / G1 _assert_create_allowed")
    print("=" * 70)

    # G2 T1-T4
    store = MemStateStore(make_batch())
    fake = make_fake(store, FakeExchange())
    ok, reason = CryptoTrader._final_pre_create_check(fake, SYMBOL, BATCH, IDENT)
    check("T1 G2 正常批次 → 允许", ok and reason == '', f"ok={ok}, reason={reason!r}")

    store = MemStateStore({SYMBOL: {}})
    fake = make_fake(store, FakeExchange())
    ok, reason = CryptoTrader._final_pre_create_check(fake, SYMBOL, BATCH, IDENT)
    check("T2 G2 批次缺失 → 拒绝（require_live_batch）",
          (not ok) and 'require_live_batch' in reason, f"ok={ok}, reason={reason!r}")

    store = MemStateStore(make_batch(close_phase=1))
    fake = make_fake(store, FakeExchange())
    ok, reason = CryptoTrader._final_pre_create_check(fake, SYMBOL, BATCH, IDENT)
    check("T3 G2 close_phase=1 → 拒绝（平仓流程）",
          (not ok) and '平仓流程' in reason, f"ok={ok}, reason={reason!r}")

    store = MemStateStore(make_batch(pending_close=True))
    fake = make_fake(store, FakeExchange())
    ok, reason = CryptoTrader._final_pre_create_check(fake, SYMBOL, BATCH, IDENT)
    check("T4 G2 legacy pending_close → 拒绝（belt）",
          (not ok) and '平仓流程' in reason, f"ok={ok}, reason={reason!r}")

    # G1 D1-D5
    store = MemStateStore({SYMBOL: {}})
    fake = make_fake(store, FakeExchange())
    ok, reason = CryptoTrader._assert_create_allowed(fake, SYMBOL, BATCH, IDENT)
    check("D1 G1 批次缺失 → 拒绝（require_live_batch，封死场景 B 孤儿通道）",
          (not ok) and 'require_live_batch' in reason, f"ok={ok}, reason={reason!r}")

    store = MemStateStore(make_batch(close_phase=1))
    fake = make_fake(store, FakeExchange())
    ok, reason = CryptoTrader._assert_create_allowed(fake, SYMBOL, BATCH, IDENT)
    check("D2 G1 close_phase=1 → 拒绝",
          (not ok) and '平仓流程' in reason, f"ok={ok}, reason={reason!r}")

    def _with_entry(entry):
        return make_batch(protection_registry={IDENT: entry})

    store = MemStateStore(_with_entry({'state': 'PROGRAMMATIC_CANCELED',
                                       'order_id': 'tp_old', 'id_known': True,
                                       'terminated_reason': 'close_requested_canceled'}))
    fake = make_fake(store, FakeExchange())
    ok, reason = CryptoTrader._assert_create_allowed(fake, SYMBOL, BATCH, IDENT,
                                                     replace_order_id='tp_old')
    check("D3 G1 PROGRAMMATIC_CANCELED → 拒绝（且 replace 豁免不可用）",
          (not ok) and '程序终结' in reason, f"ok={ok}, reason={reason!r}")

    store = MemStateStore(_with_entry({'state': 'FAILED', 'fail_count': 1, 'order_id': 'x'}))
    fake = make_fake(store, FakeExchange())
    ok, reason = CryptoTrader._assert_create_allowed(fake, SYMBOL, BATCH, IDENT)
    check("D4 G1 FAILED → 仍放行（既有语义无回归）", ok, f"ok={ok}, reason={reason!r}")

    store = MemStateStore(make_batch())
    fake = make_fake(store, FakeExchange())
    ok, reason = CryptoTrader._assert_create_allowed(fake, SYMBOL, BATCH, IDENT)
    check("D5 G1 无条目（首次创建）→ 放行", ok, f"ok={ok}, reason={reason!r}")


# =====================================================================
# 终态守卫 _update_registry / F3 早返回
# =====================================================================
def test_guards():
    print("\n" + "=" * 70)
    print("终态守卫 _update_registry / F3 _adjudicate_recreate_before_repair")
    print("=" * 70)

    # E1 PROGRAMMATIC_CANCELED 拒绝转出
    store = MemStateStore(make_batch(protection_registry={
        IDENT: {'state': 'PROGRAMMATIC_CANCELED', 'order_id': 'tp_old', 'id_known': True,
                'terminated_reason': 'close_requested_canceled'}}))
    fake = make_fake(store, FakeExchange())
    r = CryptoTrader._update_registry(fake, SYMBOL, BATCH, IDENT, state='CONFIRMED',
                                      order_id='tp_new')
    e = reg_entry(store)
    check("E1 终态守卫：PROGRAMMATIC_CANCELED → CONFIRMED 被拒（返回 None，状态不变）",
          r is None and e.get('state') == 'PROGRAMMATIC_CANCELED'
          and e.get('order_id') == 'tp_old',
          f"r={r!r}, state={e.get('state')!r}, order_id={e.get('order_id')!r}")

    # E2 同态回写（reason 更新）放行
    store = MemStateStore(make_batch(protection_registry={
        IDENT: {'state': 'PROGRAMMATIC_CANCELED', 'order_id': 'tp_old', 'id_known': True,
                'terminated_reason': 'close_requested_canceled'}}))
    fake = make_fake(store, FakeExchange())
    r = CryptoTrader._update_registry(fake, SYMBOL, BATCH, IDENT,
                                      state='PROGRAMMATIC_CANCELED',
                                      terminated_reason='g3_race_canceled')
    e = reg_entry(store)
    check("E2 同态回写放行（reason 可更新，状态保持终态）",
          e.get('state') == 'PROGRAMMATIC_CANCELED'
          and e.get('terminated_reason') == 'g3_race_canceled',
          f"state={e.get('state')!r}, reason={e.get('terminated_reason')!r}")

    # H1 F3 早返回：PROGRAMMATIC_CANCELED → ('hold', None) 永不补挂/收养
    store = MemStateStore(make_batch(protection_registry={
        IDENT: {'state': 'PROGRAMMATIC_CANCELED', 'order_id': 'tp_old', 'id_known': True}}))
    fake = make_fake(store, FakeExchange())
    verdict, oid = CryptoTrader._adjudicate_recreate_before_repair(fake, SYMBOL, BATCH, IDENT)
    check("H1 F3 裁决：PROGRAMMATIC_CANCELED → ('hold', None)（双保险之一）",
          verdict == 'hold' and oid is None, f"verdict={verdict!r}, oid={oid!r}")


# =====================================================================
# 主入口
# =====================================================================
def main():
    print("=" * 70)
    print("P0 Batch A 专项：G1/G2/G3b/G3a/N14-守卫 直接驱动（GREEN 基线）")
    print(f"被测源码：trader_260725.py（2026-08-28 Batch A 工作树，+466/-70 未 commit）")
    print("=" * 70)
    for fn in (test_g3b, test_g3a, test_gates, test_guards):
        try:
            fn()
        except Exception as e:
            print(f"❌ {fn.__name__} 异常: {e}")
            traceback.print_exc()
            RESULTS.append((f"{fn.__name__} 异常", False, str(e)))

    n_pass = sum(1 for r in RESULTS if r[1])
    n_fail = sum(1 for r in RESULTS if not r[1])
    print("\n" + "=" * 70)
    print(f"汇总：PASS={n_pass}  FAIL={n_fail}  （共 {len(RESULTS)} 项）")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  ❗ FAIL: {name} | {detail}")
    # 退出码判失败（勿 grep 关键字——8-28 回归教训）
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
