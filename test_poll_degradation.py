# -*- coding: utf-8 -*-
"""R1/R2 连续轮询失败修复 —— RED 负测（ChatGPT 裁定 6 场景，先红后绿）。

当前代码缺陷（本文件断言修复后的行为，故在当前代码上应红）：
  1. 单批次持续失败 → 无 critical 告警、无降级标记
  2. 新信号未被拦（execute_signal 不读降级标志，真实新信号仍继续）
  3. 多批次一成一败 → 成功批次会替失败批次解锁
  4. 恢复过早（fetch 成功即宣称恢复，未等成交识别 + 保护处理完成）
  5. 通知异常可杀监控（告警未在 try/except 内）—— 守卫测，当前代码无告警故空过
  6. 暂停逻辑会触碰存量 SL/TP 或监控

用法：`.venv\\Scripts\\python.exe test_poll_degradation.py`（rc=0 即全过）
"""
import copy
import json
import os
import sys
import tempfile
import threading
import time
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import trader_260725
from trader_260725 import CryptoTrader
import test_monitor_poll_recovery as PR

SYMBOL = PR.SYMBOL
REAL_OID = 'REAL_EXCHANGE_OID_1'
BATCH = PR.BATCH
ENTRY_ID = PR.ENTRY_ID

RESULTS = []


def report(name, passed, detail=""):
    RESULTS.append((name, passed))
    print(f"{'[PASS]' if passed else '[FAIL]'} {name} {detail}")


def _criticals(fake):
    return [m for lvl, m in fake.sent if lvl == 'critical']


def _init_poll_tracking(fake):
    """初始化 R1/R2 降级跟踪变量为真实类型（fake 不跑 __init__）。"""
    fake._poll_fail_streak = {}
    fake._poll_first_fail_time = {}
    fake._poll_last_success_time = {}          # 逐批：batch_id → 最后完整成功时间
    fake._poll_degraded_batches = set()
    fake._unresolved_intent_batches = set()   # 创建结果未决（与轮询降级分开管理）
    fake._poll_alert_lock = threading.Lock()
    fake._poll_alert_active = False
    fake._poll_alert_attempted = {}
    fake._ready = True
    fake._not_ready_reason = ""
    # 绑定新增告警方法（MagicMock 不会自动绑未在 REAL_HELPERS 中的新方法）
    fake._alert_poll_degraded = (
        lambda streak, stale, batch_id=None:
        CryptoTrader._alert_poll_degraded(fake, streak, stale, batch_id))
    # 绑定启动重证所需 helper（recover_active_batches 会调用）。
    # 不用 setattr 逐个挂 —— MagicMock 的 __setattr__ 与 getattr 互调会递归爆栈。
    fake._run_position_census = lambda: []
    fake._get_current_position_amt = lambda *a, **k: 0.0
    for _h in ('_start_monitoring', '_converge_batch_orders_before_clear',
               '_collect_batch_order_ids', '_verify_order_created',
               '_prune_pending_sl_by_registry', '_handle_limit_close_on_recovery',
               '_handle_partial_close_on_recovery', '_finalize_limit_full_fill',
               '_rebuild_unresolved_entry_orders'):
        if hasattr(CryptoTrader, _h):
            mock.patch.object(
                fake, _h,
                (lambda n: lambda *a, **k: getattr(CryptoTrader, n)(fake, *a, **k))(_h)
            ).start()
    fake.clear_batch_state = lambda *a, **k: True


def _realistic_seed(sp, **over):
    """按建批真实语义播种：pending_sl_orders 含**全部入场层**（含未成交层预备项）。

    另补 recover_active_batches 必需的完整批次字段（L3491-3507 直接下标访问）。
    """
    PR._seed(sp, **over)
    b = json.load(open(sp, encoding='utf-8'))[SYMBOL][BATCH]
    b['pending_sl_orders'] = [0]          # L7733-7735: last_filled_count=0 → 全层待挂
    b.update({
        'batch_total_amount': 0.43,
        'is_hedge_mode': True,
        'params_base': {'positionSide': 'LONG', 'leverage': 100},
        'prepared_tp_params': {},
        'layer_sl_params': [],
        'filled_details': [58000.0],
        'total_entry_fee': 0.15,
    })
    b.update(over)
    with open(sp, 'w', encoding='utf-8') as f:
        json.dump({SYMBOL: {BATCH: b}}, f, ensure_ascii=False, indent=2)


def _drive_failing(fake, rounds=5):
    """驱动监控，fetch_open_orders 持续失败。返回 (rounds, err)。"""
    fake.exchange.fetch_open_orders.side_effect = RuntimeError('持续失败')
    return PR._drive(fake, None, max_rounds=rounds)


# ── T1: 单批次持续失败 → critical 告警 + 该批次标记降级 ────────────────────
def test_single_batch_persistent_failure_alerts_and_marks_degraded():
    d = tempfile.mkdtemp(prefix='pd_t1_')
    sp = os.path.join(d, 'trade_state.json')
    trader_260725.STATE_FILE = sp
    try:
        PR._seed(sp)
        states = PR._disk(sp)
        fake = PR._make_fake(sp, states)
        _init_poll_tracking(fake)   # 真实 set，非 MagicMock
        _drive_failing(fake, 5)
        crit = _criticals(fake)
        report('T1a 持续失败 → 发出 critical 告警',
               len(crit) >= 1, f'critical 数={len(crit)}')
        report('T1b 持续失败 → 该批次标记降级',
               BATCH in fake._poll_degraded_batches,
               f'degraded={fake._poll_degraded_batches}')
    finally:
        trader_260725.STATE_FILE = PR._real_state_file


# ── T2: 新信号实际被拦（execute_signal → ENTRY create_order 拒绝）──────────
class _EntryFake:
    """execute_signal fake：通过所有门闸直到 ENTRY 创建前。"""

    def __init__(self):
        self._ready = True
        self._not_ready_reason = ""
        self.tg_sent = []
        self.create_calls = []
        self._poll_degraded_batches = set()
        self._registry_log = []            # (state, identity) —— 核对磁盘 registry 证据
        self.is_active_seen = None         # save_batch_state 观测到的 is_active
        self.persisted = {}                # 最终落盘视图
        # 🔥 历史踩坑：新增跟踪变量必须显式初始化，否则 MagicMock/缺属性会在
        #    线程或门禁路径抛 AttributeError，制造假绿/假红。
        self._poll_alert_lock = threading.RLock()
        self._poll_fail_streak = {}
        self._unresolved_intent_batches = set()

    def _update_registry(self, symbol, batch_id, identity, state=None, **kw):
        # 🔥 保真度（ChatGPT 前两轮复审）：必须**真的写进 registry**，
        # 只记录调用无法证明 ABSENT 落盘，也无法让下游 registry 判定生效。
        self._registry_log.append((state, str(identity)))   # 记录原始 identity
        if getattr(self, 'persist_ok', True):
            b = self.persisted.setdefault(batch_id, {})
            b.setdefault('protection_registry', {})[str(identity)] = {
                'role': 'ENTRY', 'state': state, 'order_id': kw.get('order_id')}

    def _update_registry_checked(self, symbol, batch_id, identity, state=None, **kw):
        # 返回 True ⟺ 写盘确认（替身按 self.persist_ok 模拟可注入的写盘结果）
        self._update_registry(symbol, batch_id, identity, state=state, **kw)
        return getattr(self, 'persist_ok', True)

    def _registry_has_unresolved_entries(self, b_data):
        """与真实实现同口径（L7118）：任一 registry 条目处于未决态 → True。"""
        reg = (b_data or {}).get('protection_registry') or {}
        for e in reg.values():
            if isinstance(e, dict) and e.get('state') in (
                    'PENDING_CREATE', 'PENDING_VERIFY', 'NOT_CONFIRMED', 'HARD_LOCK'):
                return True
        return False

    def _self_heal_no_id(self, symbol, batch_id):
        self.heal_calls = getattr(self, 'heal_calls', 0) + 1

    def _recheck_registry_self_heal(self, symbol, batch_id):
        pass

    def _rebuild_entry_orders_from_registry(self, symbol, batch_id):
        """替身：默认对账**未收编**（结果仍未知），可注入 (orders, ok) 反例。"""
        return list(getattr(self, 'rebuild_result', ([], False))[0]), \
            bool(getattr(self, 'rebuild_result', ([], False))[1])

    def save_batch_state(self, symbol, batch_id, data):
        self.is_active_seen = data.get('is_active')
        self.persisted[batch_id] = copy.deepcopy(data)
        return True

    def _persist_states(self, all_states):
        if not getattr(self, 'persist_ok', True):
            return False          # 注入写盘失败
        # all_states 是 {symbol: {batch: data}}；替身内部按 {batch: data} 存，
        # 必须**摊平**，否则 load_all_states 再包一层会把账本视图打翻。
        self.persisted = {}
        for _bs in (all_states or {}).values():
            if isinstance(_bs, dict):
                for _bid, _b in _bs.items():
                    self.persisted[_bid] = copy.deepcopy(_b)
        return True

    def load_all_states(self):
        return {SYMBOL: {bid: copy.deepcopy(b) for bid, b in self.persisted.items()}}

    def _safe_api_call(self, fn, *a, **k):
        return fn(*a, **k)

    def _check_account_risk(self, all_states, signal, stats_file=None):
        return CryptoTrader._check_account_risk(self, all_states, signal, stats_file)

    def _count_active_batches(self, all_states):
        return CryptoTrader._count_active_batches(self, all_states)

    def _get_today_realized_pnl(self, stats_file=None):
        return CryptoTrader._get_today_realized_pnl(self, stats_file)

    def _compute_signal_fingerprint(self, signal):
        return "fp-test"

    def _check_existing_conflicts(self, symbol, batch_id, all_states, fp):
        return False
    def _get_current_position_amt(self, *a, **k):
        return 0.0

    def _validate_stop_losses(self, signal, mark_price):
        return True, "ok"

    def _validate_take_profit(self, signal, mark_price):
        return True, "ok"

    def _protection_identity(self, batch_id, role, layer, side):
        return f"{batch_id}|{role}|L{layer}|{side}"

    def _build_intent(self, **k):
        return dict(k)

    def save_batch_state(self, symbol, batch_id, data):
        self.is_active_seen = data.get('is_active')
        self.persisted[batch_id] = copy.deepcopy(data)
        return True

    def send_tg_notification(self, text, **k):
        self.tg_sent.append((k.get('level', 'info'), str(text)))

    def _send_email_alert(self, *a, **k):
        pass

    def _start_monitoring(self, *a, **k):
        return None          # 哨兵：不得真起监控线程（否则 AttributeError 被当拒绝）


class _FakeSignal:
    def __init__(self):
        self.symbol = SYMBOL
        self.batch_id = "batch_new_001"
        self.side = "BUY"
        self.leverage = 20
        self.entries = [(77000.0, 0.43)]
        self.stop_loss_steps = [75000.0]
        self.take_profit = 60000.0


def _entry_fake_with_exchange():
    fake = _EntryFake()
    ex = mock.MagicMock()
    ex.fapiPrivateGetPositionSideDual.return_value = {'dualSidePosition': True}
    ex.fetch_ticker.return_value = {'last': 76500.0, 'close': 76500.0}
    ex.fetch_balance.return_value = {'USDT': {'free': 10000.0}}
    ex.amount_to_precision.side_effect = lambda s, v: v
    ex.price_to_precision.side_effect = lambda s, v: v
    ex.set_leverage.return_value = {}

    def _create(**k):
        fake.create_calls.append(k)
        return {'id': 'x', 'status': 'open'}
    ex.create_order.side_effect = _create
    fake.exchange = ex
    return fake


def test_new_signal_blocked_when_degraded():
    # 阳性对照：健康时同一替身确实能创建 ENTRY（否则 T2b/T2c 是假绿）
    healthy = _entry_fake_with_exchange()
    CryptoTrader.execute_signal(healthy, _FakeSignal())
    report('T2a-健康 同一替身确实能创建 ENTRY（阳性对照）',
           len(healthy.create_calls) >= 1,
           f'健康时 create 次数={len(healthy.create_calls)}（应 ≥1）')

    fake = _entry_fake_with_exchange()
    fake._poll_degraded_batches.add(BATCH)   # 模拟监控线程已报告降级
    ret = CryptoTrader.execute_signal(fake, _FakeSignal())
    report('T2b 降级时新信号被拒绝（返回 None）',
           ret is None, f'返回={ret!r}')
    report('T2c 降级时未创建 ENTRY 单',
           len(fake.create_calls) == 0,
           f'create 次数={len(fake.create_calls)}')


# ── T3: 多批次一成一败 → 成功批次不得替失败批次解锁 ────────────────────────
def test_multi_batch_one_success_one_failure():
    d = tempfile.mkdtemp(prefix='pd_t3_')
    sp = os.path.join(d, 'trade_state.json')
    trader_260725.STATE_FILE = sp
    try:
        PR._seed(sp)
        states = PR._disk(sp)
        fake = PR._make_fake(sp, states)
        _init_poll_tracking(fake)
        # 模拟两个批次同时降级；本批次需"订单全部判定 + 无待处理"才能恢复
        fake._poll_degraded_batches.add(BATCH)
        fake._poll_degraded_batches.add('batch_other')
        fake._poll_fail_streak[BATCH] = 3
        fake._poll_fail_streak['batch_other'] = 3
        # 本批次查询成功 + 订单状态已知（无新成交、无待补挂）→ 完整恢复
        fake.exchange.fetch_open_orders.side_effect = None
        fake.exchange.fetch_open_orders.return_value = [
            {'id': ENTRY_ID, 'status': 'open'}]     # 仍在挂单中 → 明确存活
        PR._drive(fake, None, max_rounds=3)
        degraded = fake._poll_degraded_batches
        report('T3a 本批次恢复后被移除',
               BATCH not in degraded, f'degraded={degraded}')
        report('T3b 另一批次仍降级（不被解锁）',
               'batch_other' in degraded, f'degraded={degraded}')
    finally:
        trader_260725.STATE_FILE = PR._real_state_file


# ── T4: 失败后的完整恢复（成交识别 + 保护处理完成）────────────────────────
def test_complete_recovery_after_failure():
    d = tempfile.mkdtemp(prefix='pd_t4_')
    sp = os.path.join(d, 'trade_state.json')
    trader_260725.STATE_FILE = sp
    try:
        PR._seed(sp)
        states = PR._disk(sp)
        fake = PR._make_fake(sp, states)
        _init_poll_tracking(fake)
        # 第一阶段：3 轮失败 → 降级
        fake.exchange.fetch_open_orders.side_effect = [
            RuntimeError('失败1'), RuntimeError('失败2'), RuntimeError('失败3')]
        PR._drive(fake, None, max_rounds=3)
        report('T4a 失败期间批次处于降级',
               BATCH in fake._poll_degraded_batches,
               f'失败后 degraded={fake._poll_degraded_batches}')
        # 第二阶段 B1 对照：识别到成交但 SL 未创建 → 必须保持暂停
        fake.exchange.fetch_open_orders.side_effect = None
        fake.exchange.fetch_open_orders.return_value = []
        fake.exchange.fetch_order.return_value = {
            'id': ENTRY_ID, 'status': 'closed', 'average': 58000.0,
            'info': {'cumQuote': '24940', 'executedQty': '0.43', 'updateTime': 1},
        }
        PR._drive(fake, None, max_rounds=2)
        report('T4b 识别到成交但保护未完成 → 仍保持暂停',
               BATCH in fake._poll_degraded_batches,
               f'成交后 degraded={fake._poll_degraded_batches}')
    finally:
        trader_260725.STATE_FILE = PR._real_state_file


# ── T5: 通知异常不得杀监控（守卫测）────────────────────────────────────────
def test_notification_exception_kills_nothing():
    d = tempfile.mkdtemp(prefix='pd_t5_')
    sp = os.path.join(d, 'trade_state.json')
    trader_260725.STATE_FILE = sp
    try:
        PR._seed(sp)
        states = PR._disk(sp)
        fake = PR._make_fake(sp, states)
        _init_poll_tracking(fake)

        def _boom(text, **k):
            raise RuntimeError('TG 发送失败')
        fake.send_tg_notification = _boom
        fake._send_email_alert = mock.MagicMock(
            side_effect=RuntimeError('邮件失败'))
        rounds, err = _drive_failing(fake, 5)
        report('T5 通知异常未杀监控（驱动未因告警异常中断）',
               rounds >= 3 and err is None, f'驱动轮次={rounds} err={err!r}')
    finally:
        trader_260725.STATE_FILE = PR._real_state_file


# ── T6: 已有 SL/TP 和监控不被暂停逻辑触碰 ────────────────────────────────
def test_existing_sl_tp_and_monitor_not_touched():
    d = tempfile.mkdtemp(prefix='pd_t6_')
    sp = os.path.join(d, 'trade_state.json')
    trader_260725.STATE_FILE = sp
    try:
        PR._seed(sp)
        states = PR._disk(sp)
        states[SYMBOL][BATCH]['current_sl_id'] = 'S1'
        states[SYMBOL][BATCH]['tp_order_id'] = 'T1'
        fake = PR._make_fake(sp, states)
        _init_poll_tracking(fake)
        fake.cancel_calls = 0
        _orig_cancel = fake.exchange.cancel_order
        fake.fetch_positions_calls = 0
        _orig_fp = fake.exchange.fetch_positions

        def _counting_fp(*a, **k):
            fake.fetch_positions_calls += 1
            return _orig_fp(*a, **k)
        fake.exchange.fetch_positions.side_effect = _counting_fp

        def _counting_cancel(*a, **k):
            fake.cancel_calls += 1
            return _orig_cancel(*a, **k)
        fake.exchange.cancel_order.side_effect = _counting_cancel
        # 阳性对照：健康轮确实调 fetch_positions（证明计数探针本身有效，T6b 非假绿）
        # 阳性对照：健康轮确实走到「批次存活核对」（证明 T6b 判据本身可观测）
        fake.exchange.fetch_open_orders.side_effect = None
        fake.exchange.fetch_open_orders.return_value = [
            {'id': ENTRY_ID, 'status': 'open'}]
        PR._drive(fake, None, max_rounds=1)
        report('T6a-健康 健康轮确实完成核对（阳性对照）',
               fake._poll_fail_streak.get(BATCH, 0) == 0
               and BATCH not in fake._poll_degraded_batches,
               f"健康轮后 streak={fake._poll_fail_streak.get(BATCH, 0)}（=0 且未降级）")
        # 降级轮：open_orders 失败 → 不应再走到 fetch_positions（业务暂停但线程存活）
        fake.fetch_positions_calls = 0
        _drive_failing(fake, 5)
        report('T6a 暂停期间未撤存量保护单',
               fake.cancel_calls == 0, f'cancel={fake.cancel_calls}')
        report('T6b 暂停期间监控线程未退出（仍有轮次推进）',
               fake._poll_fail_streak.get(BATCH, 0) >= 3,
               f"降级计数={fake._poll_fail_streak.get(BATCH)}"
               f"（≥3 → 线程在持续轮询而非死亡）")
    finally:
        trader_260725.STATE_FILE = PR._real_state_file


# ══════════════════════════════════════════════════════════════════════════
# ChatGPT 第二轮复审（97af6e3 不可合入）：4 个阻断项负测
# ══════════════════════════════════════════════════════════════════════════

# ── 阻断1：成交识别失败（fetch_order 失败）不得报成"恢复" ──────────────────
def test_block1_fetch_order_failure_keeps_degraded():
    d = tempfile.mkdtemp(prefix='blk1_')
    sp = os.path.join(d, 'trade_state.json')
    trader_260725.STATE_FILE = sp
    try:
        PR._seed(sp)
        fake = PR._make_fake(sp, PR._disk(sp))
        _init_poll_tracking(fake)
        ex = fake.exchange
        ex.fetch_open_orders.side_effect = [
            RuntimeError('f1'), RuntimeError('f2'), RuntimeError('f3')]
        PR._drive(fake, None, max_rounds=3)
        report('B1a 三轮失败后处于降级',
               BATCH in fake._poll_degraded_batches,
               f'degraded={fake._poll_degraded_batches}')
        # open_orders 成功但 fetch_order 失败 → 不得解除降级
        ex.fetch_open_orders.side_effect = None
        ex.fetch_open_orders.return_value = []
        ex.fetch_order.side_effect = RuntimeError('fetch_order 失败')
        PR._drive(fake, None, max_rounds=3)
        report('B1b 成交识别失败时不得报成恢复',
               BATCH in fake._poll_degraded_batches,
               f'识别失败后 degraded={fake._poll_degraded_batches}')
    finally:
        trader_260725.STATE_FILE = PR._real_state_file


# ── 阻断2：暂停检查后、第一层 ENTRY 创建前才降级 → 必须拦住 ────────────────
def test_block2_degrade_after_check_blocks_entry_create():
    fake = _entry_fake_with_exchange()
    # 在骨架落盘后、第一层 create 之前才置降级（模拟另一线程刚好报故障）
    _orig_save = fake.save_batch_state

    def _save_then_degrade(*a, **k):
        r = _orig_save(*a, **k)
        fake._poll_degraded_batches.add(BATCH)   # 检查已过，此刻才降级
        return r
    fake.save_batch_state = _save_then_degrade
    ret = CryptoTrader.execute_signal(fake, _FakeSignal())
    report('B2a 检查后才降级 → 仍不得创建 ENTRY',
           len(fake.create_calls) == 0,
           f'create 次数={len(fake.create_calls)}，返回={ret!r}')


# ── 阻断3：A 的陈旧不得被 B 的成功遮住（逐批计算）──────────────────────────
def test_block3_per_batch_stale_not_masked_by_other_batch():
    d = tempfile.mkdtemp(prefix='blk3_')
    sp = os.path.join(d, 'trade_state.json')
    trader_260725.STATE_FILE = sp
    try:
        PR._seed(sp)
        fake = PR._make_fake(sp, PR._disk(sp))
        _init_poll_tracking(fake)
        # 模拟：A 早已失明，但别的批次刚成功过一次（全局 last_success_time=现在）
        fake._poll_last_success_time = time.time()
        fake._poll_first_fail_time[BATCH] = time.time() - 9999.0
        fake._poll_fail_streak[BATCH] = 1
        fake.exchange.fetch_open_orders.side_effect = RuntimeError('A 失败')
        PR._drive(fake, None, max_rounds=1)
        report('B3a 别的批次成功不遮住 A 的陈旧（A 应降级）',
               BATCH in fake._poll_degraded_batches,
               f'A degraded={fake._poll_degraded_batches}；'
               f'全局 last_success_time 为"现在"')
    finally:
        trader_260725.STATE_FILE = PR._real_state_file


# ── 阻断4：通知返回 False（未抛异常）后仍应有限重试 ────────────────────────
def test_block4_alert_retry_on_notify_false():
    d = tempfile.mkdtemp(prefix='blk4_')
    sp = os.path.join(d, 'trade_state.json')
    trader_260725.STATE_FILE = sp
    try:
        PR._seed(sp)
        fake = PR._make_fake(sp, PR._disk(sp))
        _init_poll_tracking(fake)
        calls = []

        def _returns_false(text, **k):
            calls.append(k.get('level'))
            return False      # 通知失败但不抛异常
        fake.send_tg_notification = _returns_false
        fake.exchange.fetch_open_orders.side_effect = RuntimeError('持续失败')
        PR._drive(fake, None, max_rounds=6)
        report('B4a 通知返回 False 后仍有有限重试',
               len(calls) >= 2,
               f'6 轮失败内告警尝试次数={len(calls)}（应 ≥2 → 有重试）')
    finally:
        trader_260725.STATE_FILE = PR._real_state_file


# ══════════════════════════════════════════════════════════════════════════
# ChatGPT 第三轮复审（4edd39a 不可合入）：3 阻断 + 2 口径
# ══════════════════════════════════════════════════════════════════════════

# ── 阻断5：未成交层的预备项不得让降级永远不解锁 ──────────────────────────
def test_block5_unfilled_entry_does_not_block_recovery():
    d = tempfile.mkdtemp(prefix='blk5_')
    sp = os.path.join(d, 'trade_state.json')
    trader_260725.STATE_FILE = sp
    try:
        _realistic_seed(sp)          # pending_sl_orders=[0]（未成交层预备项）
        fake = PR._make_fake(sp, PR._disk(sp))
        _init_poll_tracking(fake)
        ex = fake.exchange
        ex.fetch_open_orders.side_effect = [
            RuntimeError('f1'), RuntimeError('f2'), RuntimeError('f3')]
        PR._drive(fake, None, max_rounds=3)
        report('B5a 三轮失败后处于降级',
               BATCH in fake._poll_degraded_batches,
               f'degraded={fake._poll_degraded_batches}')
        # 查询恢复：入场单仍在挂单中（未成交）→ 预备项不构成"确需保护" → 应解锁
        ex.fetch_open_orders.side_effect = None
        ex.fetch_open_orders.return_value = [{'id': ENTRY_ID, 'status': 'open'}]
        PR._drive(fake, None, max_rounds=3)
        report('B5b 未成交 ENTRY 恢复正常后应解锁',
               BATCH not in fake._poll_degraded_batches,
               f'恢复后 degraded={fake._poll_degraded_batches}'
               f'（pending_sl_orders=[0] 为未成交层预备项，不得阻止解锁）')
    finally:
        trader_260725.STATE_FILE = PR._real_state_file


# ── 阻断6：部分层已创建时不得返回 CLEAN_REJECT ───────────────────────────
def test_block6_partial_create_not_clean_reject():
    class _TwoLayerSignal(_FakeSignal):
        def __init__(self):
            super().__init__()
            self.entries = [(77000.0, 0.43), (78000.0, 0.43)]
            self.stop_loss_steps = [75000.0, 76000.0]

    # 6a 零创建：先过前置暂停门 → 在**骨架落盘后、首层创建前**才降级
    #     （这才是阻断2 的真实时序）；未尝试层应落 ABSENT，允许 CLEAN_REJECT。
    #     注意 create_order 计数器只统计**真正发出的调用**，不能用它判"零副作用"。
    f0 = _entry_fake_with_exchange()
    f0.registry_layers = []          # 实际进入 create 的层
    f0._state_lock = threading.RLock()
    _orig_create0 = f0.exchange.create_order

    def _spy_create0(**k):
        f0.registry_layers.append(len(f0.registry_layers))
        return _orig_create0(**k)
    f0.exchange.create_order.side_effect = _spy_create0
    _orig_save0 = f0.save_batch_state

    def _save_then_pause0(*a, **k):
        r = _orig_save0(*a, **k)
        f0._poll_degraded_batches.add(BATCH)   # 骨架已落，此刻才降级
        return r
    f0.save_batch_state = _save_then_pause0
    r0 = CryptoTrader.execute_signal(f0, _TwoLayerSignal())
    reg0 = f0._registry_log
    # identity 形如 batch|ENTRY|L0|LONG；用正则取层号（替身 parser 口径）
    import re as _re
    absent_layers = sorted(
        int(_re.search(r'\|L(\d+)\|', ident).group(1))
        for st, ident in reg0 if st == 'ABSENT' and _re.search(r'\|L(\d+)\|', ident))
    report('B6a 零创建：未尝试层落 ABSENT 且可 CLEAN_REJECT',
           r0 == 'CLEAN_REJECT'
           and len(f0.registry_layers) == 0
           and absent_layers == [0, 1],
           f'返回={r0!r}，实际 create 层={f0.registry_layers}（应空），'
           f'ABSENT 层={absent_layers}（应 [0,1]），原始 registry={reg0}')

    # 6b 部分创建：第 1 层成功后第 2 层才被暂停 → 不得 CLEAN_REJECT
    f1 = _entry_fake_with_exchange()
    f1.registry_layers = []
    f1._state_lock = threading.RLock()     # 空骨架停用路径需要
    n = {'c': 0}
    _orig_create = f1.exchange.create_order

    def _create_then_pause(**k):
        f1.registry_layers.append(n['c'])
        n['c'] += 1
        r = _orig_create(**k)
        if n['c'] >= 1:
            f1._poll_degraded_batches.add(BATCH)   # 第 2 层前降级
        return r
    f1.exchange.create_order.side_effect = _create_then_pause
    r1 = CryptoTrader.execute_signal(f1, _TwoLayerSignal())
    reg1 = f1._registry_log
    report('B6b 部分创建：不得返回 CLEAN_REJECT（须保留证据）',
           r1 != 'CLEAN_REJECT' and len(f1.registry_layers) >= 1,
           f'返回={r1!r}（应非 CLEAN_REJECT），实际 create 层={f1.registry_layers}，'
           f'registry={reg1}')
    # B6c 用替身可注入的写盘结果核对 ABSENT 落盘确认路径
    report('B6c ABSENT 落盘确认路径可被注入（替身 persist_ok=True 时返回 CLEAN_REJECT）',
           r0 == 'CLEAN_REJECT' and getattr(f0, 'persist_ok', True) is True,
           f'返回={r0!r}（persist_ok=True → ABSENT 落盘确认 → CLEAN_REJECT）')
    report('B6d 部分创建：返回 None 后批次保持 active 交监控',
           f1.is_active_seen is True,
           f'is_active 落盘观测={f1.is_active_seen}')


# ── 阻断7：降级后重启，启动重证未过 → 禁止新 ENTRY ───────────────────────
def test_block7_restart_reverify_blocks_new_entry():
    d = tempfile.mkdtemp(prefix='blk7_')
    sp = os.path.join(d, 'trade_state.json')
    trader_260725.STATE_FILE = sp
    try:
        _realistic_seed(sp)
        # 模拟重启后：内存降级集合为空（丢失），但订单查询失败
        fake = PR._make_fake(sp, PR._disk(sp))
        _init_poll_tracking(fake)
        fake.exchange.fetch_open_orders.side_effect = RuntimeError('查询失败')
        ok = CryptoTrader.recover_active_batches(fake)
        report('B7a 重启后查询失败 → recover 返回 False（保持未就绪）',
               ok is False, f'返回={ok!r}')
        report('B7b 重证未过 → _ready 保持 False（禁止新 ENTRY）',
               fake._ready is False,
               f'_ready={fake._ready}，reason={fake._not_ready_reason!r}')
        # 证明 _ready=False 确实拦住新 ENTRY（SG1 门）
        ef = _entry_fake_with_exchange()
        ef._ready = False
        ef._not_ready_reason = '启动重证未通过'
        ret = CryptoTrader.execute_signal(ef, _FakeSignal())
        report('B7c 未就绪时新 ENTRY 被拦（create=0）',
               len(ef.create_calls) == 0,
               f'create 次数={len(ef.create_calls)}，返回={ret!r}')
    finally:
        trader_260725.STATE_FILE = PR._real_state_file


# ── 口径：send_tg_notification 返回 None（未配 TG）不得记成已送达 ────────
def test_block8_notify_none_is_not_delivered():
    d = tempfile.mkdtemp(prefix='blk8_')
    sp = os.path.join(d, 'trade_state.json')
    trader_260725.STATE_FILE = sp
    try:
        _realistic_seed(sp)
        fake = PR._make_fake(sp, PR._disk(sp))
        _init_poll_tracking(fake)
        calls = []
        fake.send_tg_notification = (
            lambda text, **k: (calls.append(k.get('level')), None)[1])  # 返回 None
        fake.exchange.fetch_open_orders.side_effect = RuntimeError('持续失败')
        PR._drive(fake, None, max_rounds=6)
        report('B8a 通知返回 None → 不得记成已送达（保留重试）',
               fake._poll_alert_active is False,
               f'_poll_alert_active={fake._poll_alert_active}'
               f'（应 False → 保留重试资格），尝试次数={len(calls)}')
        report('B8b 通知返回 None 时仍有有限重试',
               len(calls) >= 2, f'6 轮内尝试={len(calls)} 次（应 ≥2）')
    finally:
        trader_260725.STATE_FILE = PR._real_state_file


# ── 口径：真实双批次时序（A 失败、B 成功）不得被 B 遮住 A 的陈旧 ────────
def test_block9_real_dual_batch_stale():
    d = tempfile.mkdtemp(prefix='blk9_')
    sp = os.path.join(d, 'trade_state.json')
    trader_260725.STATE_FILE = sp
    try:
        _realistic_seed(sp)
        fake = PR._make_fake(sp, PR._disk(sp))
        _init_poll_tracking(fake)
        BATCH_B = 'batch_pollrec_002'
        # 真实驱动：A 连续失败 ≥3 轮 → 应降级（与 B 无关）
        fake.exchange.fetch_open_orders.side_effect = RuntimeError('A 失败')
        PR._drive(fake, None, max_rounds=3)
        report('B9a A 连续失败后降级',
               BATCH in fake._poll_degraded_batches,
               f'A degraded={fake._poll_degraded_batches}')
        # B 成功一次 → 更新 **B 自己** 的 last_success_time，不得覆盖 A 的判据
        fake._poll_last_success_time[BATCH_B] = time.time()
        fake._poll_degraded_batches.discard(BATCH)      # 复位
        fake._poll_fail_streak[BATCH] = 1
        fake._poll_first_fail_time[BATCH] = time.time() - 9999.0  # A 早已失明
        fake.exchange.fetch_open_orders.side_effect = RuntimeError('A 失败')
        PR._drive(fake, None, max_rounds=1)
        report('B9b B 成功不遮住 A 的陈旧（A 应仍降级）',
               BATCH in fake._poll_degraded_batches,
               f'A degraded={fake._poll_degraded_batches}；'
               f'last_success_time={fake._poll_last_success_time}')
    finally:
        trader_260725.STATE_FILE = PR._real_state_file


# ── B10/B11：CLEAN_REJECT 的落盘与接管（第四轮复审阻断3）─────────────────
def _two_layer_fake():
    class _TwoLayerSignal(_FakeSignal):
        def __init__(self):
            super().__init__()
            self.entries = [(77000.0, 0.43), (78000.0, 0.43)]
            self.stop_loss_steps = [75000.0, 76000.0]
    f = _entry_fake_with_exchange()
    f.registry_layers = []
    f._state_lock = threading.RLock()
    f.monitoring_started = []
    f.monitoring_started_ids = []
    f._start_monitoring = (
        lambda *a, **k: (f.monitoring_started.append(k.get('batch_id')),
                         f.monitoring_started_ids.extend(
                             k.get('entry_orders') or [])))
    return f, _TwoLayerSignal()


def _zero_create_fake():
    """零创建替身：骨架落盘后才降级 → 首层创建前被暂停门拦截 → 进入 PROVEN-CLEAN。"""
    f, sig = _two_layer_fake()
    f.registry_layers = []
    _orig_create = f.exchange.create_order

    def _spy(**k):
        f.registry_layers.append(len(f.registry_layers))
        return _orig_create(**k)
    f.exchange.create_order.side_effect = _spy
    _orig_save = f.save_batch_state

    def _save_then_pause(*a, **k):
        r = _orig_save(*a, **k)
        f._poll_degraded_batches.add('batch_new_001')   # 骨架已落，此刻才降级
        return r
    f.save_batch_state = _save_then_pause
    return f, sig


# ── B18：CLEAN_REJECT 假出口——并发变化 / 骨架缺失 / 残留未决意图 ─────────
def test_block18_clean_reject_no_fake_exit():
    # 并发注入点用 `_update_registry`：新实现走 `_update_registry_checked`
    # （替身内部转调 `_update_registry`），旧实现直接走 `_update_registry` ——
    # 挂在这一层才能对**两个版本都**真正注入并发变化（挂 checked 则旧版收不到）。
    def _after_absent(f, mutate):
        _orig = f._update_registry

        def _hook(symbol, batch_id, identity, state=None, **kw):
            r = _orig(symbol, batch_id, identity, state=state, **kw)
            if state == 'ABSENT':
                mutate(f)
            return r
        f._update_registry = _hook
        return f

    # 18a 并发变化：落 ABSENT 期间骨架被写入 entry_orders → 不得 CLEAN_REJECT
    f, sig = _zero_create_fake()
    _after_absent(f, lambda x: x.persisted.setdefault('batch_new_001', {})
                  .__setitem__('entry_orders', ['RACE']))
    r = CryptoTrader.execute_signal(f, sig)
    report('B18a 重读发现 entry_orders（并发变化）→ 不得 CLEAN_REJECT',
           r != 'CLEAN_REJECT', f'返回={r!r}（应非 CLEAN_REJECT）')

    # 18b 骨架缺失：落 ABSENT 后批次被移除 → 重读不到 → 不得 CLEAN_REJECT
    f2, sig2 = _zero_create_fake()
    _after_absent(f2, lambda x: x.persisted.pop('batch_new_001', None))
    r2 = CryptoTrader.execute_signal(f2, sig2)
    report('B18b 重读不到预期骨架批次 → 不得 CLEAN_REJECT',
           r2 != 'CLEAN_REJECT', f'返回={r2!r}（应非 CLEAN_REJECT）')

    # 18c 未决意图仍在 registry → 不得 CLEAN_REJECT
    def _add_pending(x):
        x.persisted.setdefault('batch_new_001', {}).setdefault(
            'protection_registry', {})['X|ENTRY|L9|LONG'] = {
                'role': 'ENTRY', 'state': 'PENDING_CREATE', 'order_id': None}
    f3, sig3 = _zero_create_fake()
    _after_absent(f3, _add_pending)
    r3 = CryptoTrader.execute_signal(f3, sig3)
    report('B18c 重读发现未决 ENTRY 意图 → 不得 CLEAN_REJECT',
           r3 != 'CLEAN_REJECT', f'返回={r3!r}（应非 CLEAN_REJECT）')


# ── B19：对账结果决定接管是否用真实 ID / 是否降级告警 ────────────────────
def test_block19_reconcile_drives_takeover():
    f, sig = _two_layer_fake()
    _bind_real_reconcile(f)
    f._state_lock = threading.RLock()
    f.exchange.fetch_open_orders.side_effect = None
    f.exchange.fetch_open_orders.return_value = [{
        'id': REAL_OID, 'symbol': SYMBOL, 'type': 'STOP_MARKET', 'side': 'buy',
        'price': 77000.0, 'amount': 0.43, 'status': 'open'}]
    f.exchange.create_order.side_effect = RuntimeError('创建结果未知')
    CryptoTrader.execute_signal(f, sig)
    time.sleep(0.4)
    report('B19a 对账收编成功 → 接管按真实订单 ID 监控',
           REAL_OID in f.monitoring_started_ids,
           f'接管传入 entry_orders={f.monitoring_started_ids}（应含 REAL_OID）')
    report('B19b 对账成功 → 不误置降级',
           'batch_new_001' not in f._poll_degraded_batches,
           f'degraded={f._poll_degraded_batches}（对账已判明，不应降级）')

    f2, sig2 = _two_layer_fake()
    f2.rebuild_result = ([], False)          # 未收编 → 结果仍未知
    f2.exchange.create_order.side_effect = RuntimeError('创建结果未知')
    CryptoTrader.execute_signal(f2, sig2)
    time.sleep(0.3)
    report('B19c 对账仍未知 → 持续禁止新增风险（置入降级集合）',
           'batch_new_001' in f2._poll_degraded_batches,
           f'degraded={f2._poll_degraded_batches}（应含本批次）')
    report('B19d 对账仍未知 → 必须发 critical 告警要求人工核对',
           any(lv == 'critical' for lv, _ in f2.tg_sent),
           f'告警级别={[lv for lv, _ in f2.tg_sent]}')


def test_block10_write_false_no_clean_reject():
    f, sig = _two_layer_fake()
    f.persist_ok = False                     # 注入：写盘返回 False
    _orig = f.save_batch_state

    def _save_then_pause(*a, **k):
        r = _orig(*a, **k)
        f._poll_degraded_batches.add(BATCH)
        return r
    f.save_batch_state = _save_then_pause
    r = CryptoTrader.execute_signal(f, sig)
    report('B10 写盘返回 False → 不得 CLEAN_REJECT',
           r != 'CLEAN_REJECT',
           f'返回={r!r}（应非 CLEAN_REJECT）')


def test_block11_unknown_create_has_takeover():
    f, sig = _two_layer_fake()
    # 第 1 层已尝试创建，但 create 抛未知异常（结果未知）
    _orig = f.exchange.create_order

    def _unknown_create(**k):
        raise RuntimeError('create 结果未知（网络中断）')
    f.exchange.create_order.side_effect = _unknown_create
    r = CryptoTrader.execute_signal(f, sig)
    report('B11a 创建结果未知 → 不得 CLEAN_REJECT',
           r != 'CLEAN_REJECT', f'返回={r!r}（应非 CLEAN_REJECT）')
    time.sleep(0.3)   # 接管在线程中启动
    report('B11b 创建结果未知 → 必须实际启动监控接管',
           len(f.monitoring_started) >= 1,
           f'已启动接管批次={f.monitoring_started}（应 ≥1）')


# ── B12：启动重证"查询成功但事实不一致"（第四轮复审阻断2）────────────────
def test_block12_reverify_inconsistent_success():
    d = tempfile.mkdtemp(prefix='blk12_')
    sp = os.path.join(d, 'trade_state.json')
    trader_260725.STATE_FILE = sp
    try:
        _realistic_seed(sp)
        fake = PR._make_fake(sp, PR._disk(sp))
        _init_poll_tracking(fake)
        # 查询"成功"但返回空列表；本地 ENTRY 不在其中，且 fetch_order 状态未知
        fake.exchange.fetch_open_orders.side_effect = None
        fake.exchange.fetch_open_orders.return_value = []
        fake.exchange.fetch_order.side_effect = RuntimeError('状态未知')
        fake._get_current_position_amt = lambda *a, **k: 0.0
        ok = CryptoTrader.recover_active_batches(fake)
        report('B12a 查询成功但 ENTRY 事实不一致 → 不得 READY',
               ok is False and fake._ready is False,
               f'recover={ok!r}，_ready={fake._ready}')
    finally:
        trader_260725.STATE_FILE = PR._real_state_file


# ── B13：有持仓但无有效 SL 锚点 → 重证不通过（第四轮复审阻断2）──────────
def test_block13_reverify_position_without_sl():
    d = tempfile.mkdtemp(prefix='blk13_')
    sp = os.path.join(d, 'trade_state.json')
    trader_260725.STATE_FILE = sp
    try:
        # registry 只有 ENTRY 记录（非空但不含 CONFIRMED SL）——旧逻辑会误判为有锚点
        _realistic_seed(sp, current_sl_id=None,
                        protection_registry={'X|ENTRY|L0|LONG': {
                            'role': 'ENTRY', 'state': 'CONFIRMED', 'order_id': 'e1'}})
        fake = PR._make_fake(sp, PR._disk(sp))
        _init_poll_tracking(fake)
        fake.exchange.fetch_open_orders.side_effect = None
        fake.exchange.fetch_open_orders.return_value = []
        fake.exchange.fetch_order.return_value = {'status': 'closed'}
        fake._get_current_position_amt = lambda *a, **k: 0.002   # 有持仓
        ok = CryptoTrader.recover_active_batches(fake)
        report('B13 有持仓但 registry 只有 ENTRY（无有效 SL）→ 不得 READY',
               ok is False and fake._ready is False,
               f'recover={ok!r}，_ready={fake._ready}')
    finally:
        trader_260725.STATE_FILE = PR._real_state_file


# ── B14：止损失败 → 保持暂停（第四轮复审阻断1）──────────────────────────
def test_block14_sl_failure_keeps_paused():
    d = tempfile.mkdtemp(prefix='blk14_')
    sp = os.path.join(d, 'trade_state.json')
    trader_260725.STATE_FILE = sp
    try:
        # 已成交一层，但无 SL 锚点（current_sl_id=None）→ 保护未确认
        _realistic_seed(sp, current_sl_id=None, last_filled_count=1,
                        pending_sl_orders=[0], filled_details=[58000.0],
                        protection_registry={})
        fake = PR._make_fake(sp, PR._disk(sp))
        _init_poll_tracking(fake)
        fake._poll_fail_streak[BATCH] = 3
        fake._poll_degraded_batches.add(BATCH)
        fake.exchange.fetch_open_orders.side_effect = None
        fake.exchange.fetch_open_orders.return_value = []
        fake.exchange.fetch_order.return_value = {
            'id': ENTRY_ID, 'status': 'closed', 'average': 58000.0,
            'info': {'cumQuote': '24940', 'executedQty': '0.43', 'updateTime': 1}}
        # SL 创建失败（保护维护失败）
        fake.exchange.create_order.side_effect = RuntimeError('SL 创建失败')
        PR._drive(fake, None, max_rounds=3)
        report('B14 订单可读但 SL 维护失败 → 保持暂停',
               BATCH in fake._poll_degraded_batches,
               f'degraded={fake._poll_degraded_batches}（应仍含本批次）')
    finally:
        trader_260725.STATE_FILE = PR._real_state_file


def _bind_real_reconcile(f):
    """把**真实**的无 ID 意图对账实现绑到 execute_signal 替身上。

    ChatGPT 第五轮复审：B11b 的桩只证明「调用过 _start_monitoring」，
    证明不了「无 ID 的 ENTRY 真能被收编并按真实 ID 交监控」。"""
    for name in ('_order_matches_intent', '_commit_registry_txn',
                 '_self_heal_no_id', '_recheck_registry_self_heal',
                 '_rebuild_entry_orders_from_registry',
                 '_registry_has_unresolved_entries'):
        setattr(f, name, (lambda n=name: lambda *a, **k: getattr(CryptoTrader, n)(f, *a, **k))())
    return f


# ── B15：创建返回异常但交易所实际有单并成交（第五轮复审阻断1）────────────
def test_block15_unknown_create_real_monitor_takeover():
    """真实对账 + 真实监控控制流：create 抛未知异常，交易所**实际有单且已成交**。

    旧实现用 entry_orders=[] 启动监控 → 监控只遍历传入 ID → 永远不会对该 ENTRY
    调 fetch_order → 创建实际成功并成交时无人补挂保护。"""
    f, sig = _two_layer_fake()
    _bind_real_reconcile(f)
    f._state_lock = threading.RLock()
    # 交易所实况：第 1 层的开仓条件单**真实存在**（尽管 create 抛了异常）
    real_order = {'id': REAL_OID, 'symbol': SYMBOL, 'type': 'STOP_MARKET',
                  'side': 'buy', 'price': 77000.0, 'amount': 0.43, 'status': 'open'}
    f.exchange.fetch_open_orders.side_effect = None
    f.exchange.fetch_open_orders.return_value = [real_order]
    f.exchange.create_order.side_effect = RuntimeError('创建结果未知（网络中断）')
    CryptoTrader.execute_signal(f, sig)
    time.sleep(0.4)   # 接管在线程中启动

    report('B15a 真实对账从交易所收编无 ID ENTRY → 接管按真实 ID 监控',
           REAL_OID in f.monitoring_started_ids,
           f'接管传入 entry_orders={f.monitoring_started_ids}（应含 {REAL_OID}）')
    report('B15b 对账已判明 → 不误置降级',
           'batch_new_001' not in f._poll_degraded_batches,
           f'degraded={f._poll_degraded_batches}')

    # 真实监控控制流：用接管实际拿到的 ID 驱动 → 必须识别该 ENTRY 成交
    if REAL_OID in f.monitoring_started_ids:
        d = tempfile.mkdtemp(prefix='blk15m_')
        sp = os.path.join(d, 'trade_state.json')
        trader_260725.STATE_FILE = sp
        try:
            _realistic_seed(sp, entry_orders=[REAL_OID], current_sl_id=None,
                            last_filled_count=0, pending_sl_orders=[0],
                            protection_registry={
                                f'{BATCH}|ENTRY|L0|LONG': {
                                    'role': 'ENTRY', 'state': 'CONFIRMED',
                                    'order_id': REAL_OID, 'id_known': True}})
            mf = PR._make_fake(sp, PR._disk(sp))
            _init_poll_tracking(mf)
            mf.exchange.fetch_open_orders.side_effect = None
            mf.exchange.fetch_open_orders.return_value = []      # 已不在未结 = 已成交
            mf.exchange.fetch_order.return_value = {
                'id': REAL_OID, 'status': 'closed', 'average': 58000.0,
                'info': {'cumQuote': '24940', 'executedQty': '0.43', 'updateTime': 1}}
            mf._get_current_position_amt = lambda *a, **k: 0.43
            PR._drive(mf, None, max_rounds=3)
            report('B15c 真实监控按真实 ID 识别该 ENTRY 成交并进入补挂保护路径',
                   mf.exchange.fetch_order.call_count >= 1
                   and len(mf.sl_place_calls) >= 1,
                   f'fetch_order={mf.exchange.fetch_order.call_count} 次，'
                   f'补挂路径进入={len(mf.sl_place_calls)} 次'
                   f'（fetch_order=0 即监控认不出该 ENTRY）')
        finally:
            trader_260725.STATE_FILE = PR._real_state_file


# ── B16：SL 在交易所消失但本地 ID 未清（第五轮复审阻断2）─────────────────
def test_block16_sl_vanished_on_exchange():
    d = tempfile.mkdtemp(prefix='blk16_')
    sp = os.path.join(d, 'trade_state.json')
    trader_260725.STATE_FILE = sp
    try:
        _realistic_seed(sp, current_sl_id='SL_GONE')
        fake = PR._make_fake(sp, PR._disk(sp))
        _init_poll_tracking(fake)
        fake.exchange.fetch_open_orders.side_effect = None
        fake.exchange.fetch_open_orders.return_value = []
        # ⚠️ 关键：SL 回查必须返回 open，否则监控线程会**先**把 current_sl_id 清成
        # None，重证就会因「无 SL 锚点」而红 —— 打在另一个原因上，失去区分度。
        def _fo(oid, sym=None, *a, **k):
            if str(oid) == 'SL_GONE':
                return {'id': 'SL_GONE', 'status': 'open', 'amount': 0.43,
                        'side': 'sell', 'info': {'stopPrice': 55000.0}}
            return {'id': str(oid), 'status': 'closed', 'average': 58000.0,
                    'info': {'cumQuote': '24940', 'executedQty': '0.43'}}
        fake.exchange.fetch_order.side_effect = _fo
        fake._get_current_position_amt = lambda *a, **k: 0.43
        ok = CryptoTrader.recover_active_batches(fake)
        report('B16 本地 SL ID 非空但交易所已无该止损单 → 不得 READY',
               ok is False and fake._ready is False,
               f'recover={ok!r}，_ready={fake._ready}')
    finally:
        trader_260725.STATE_FILE = PR._real_state_file


# ── B17：SL 方向错 / 覆盖不足 / 覆盖量不明（第五轮复审阻断2）─────────────
def test_block17_sl_direction_and_coverage():
    for label, order in (
            ('方向错', {'id': 'SL1', 'side': 'BUY', 'amount': 0.43}),
            ('覆盖不足', {'id': 'SL1', 'side': 'SELL', 'amount': 0.1}),
            ('覆盖量不明', {'id': 'SL1', 'side': 'SELL', 'amount': None})):
        d = tempfile.mkdtemp(prefix='blk17_')
        sp = os.path.join(d, 'trade_state.json')
        trader_260725.STATE_FILE = sp
        try:
            _realistic_seed(sp, current_sl_id='SL1')
            fake = PR._make_fake(sp, PR._disk(sp))
            _init_poll_tracking(fake)
            fake.exchange.fetch_open_orders.side_effect = None
            fake.exchange.fetch_open_orders.return_value = [order]
            # ⚠️ 只有 SL 回查返回 open（否则监控会先清掉 current_sl_id）；
            #    ENTRY 必须返回 closed，否则 ENTRY 判明那一步就先红了，
            #    测试会打在另一个原因上、失去区分度。
            def _fo(oid, sym=None, *a, **k):
                if str(oid) == 'SL1':
                    return dict(order, id='SL1', status='open')
                return {'id': str(oid), 'status': 'closed', 'average': 58000.0,
                        'info': {'cumQuote': '24940', 'executedQty': '0.43'}}
            fake.exchange.fetch_order.side_effect = _fo
            fake._get_current_position_amt = lambda *a, **k: 0.43
            ok = CryptoTrader.recover_active_batches(fake)
            report(f'B17 SL {label} → 不得 READY',
                   ok is False and fake._ready is False,
                   f'recover={ok!r}，_ready={fake._ready}')
        finally:
            trader_260725.STATE_FILE = PR._real_state_file


# ── B20/B21/B22：接管参数逐层一致 + 连续时序（第六轮复审阻断1）───────────
def _reconciled_run():
    """跑到「创建抛未知异常 → 真实对账」这一步的替身。"""
    f, sig = _two_layer_fake()
    _bind_real_reconcile(f)
    f._state_lock = threading.RLock()
    f.monitor_kwargs = []
    f._start_monitoring = lambda *a, **k: f.monitor_kwargs.append(k)
    f.exchange.fetch_open_orders.side_effect = None
    f.exchange.fetch_open_orders.return_value = [{
        'id': REAL_OID, 'symbol': SYMBOL, 'type': 'STOP_MARKET', 'side': 'buy',
        'price': 77000.0, 'amount': 0.43, 'status': 'open'}]
    f.exchange.create_order.side_effect = RuntimeError('创建结果未知')
    CryptoTrader.execute_signal(f, sig)
    time.sleep(0.4)
    return f, sig


def test_block20_takeover_params_layer_consistent():
    f, _ = _reconciled_run()
    report('B20a 收编成功 → 确实启动了监控接管',
           len(f.monitor_kwargs) == 1, f'启动次数={len(f.monitor_kwargs)}（应 1）')
    if not f.monitor_kwargs:
        return
    k = f.monitor_kwargs[0]
    n = len(k.get('entry_orders') or [])
    ta, ss = k.get('target_amounts') or [], k.get('stop_steps') or []
    report('B20b 接管数量/止损参数与订单逐层一致（非空且不短于订单数）',
           n >= 1 and len(ta) >= n and len(ss) >= n,
           f'订单={n}，target_amounts={len(ta)}，stop_steps={len(ss)}'
           f'（旧实现传创建异常前的空值 → 监控按层访问越界）')
    report('B20c 接管不得用 prepared_tp_params={} / layer_sl_params=[] 空壳',
           bool(k.get('prepared_tp_params')) and bool(k.get('layer_sl_params')),
           f'tp_params={bool(k.get("prepared_tp_params"))}，'
           f'layer_sl_params={len(k.get("layer_sl_params") or [])}')


def test_block21_continuous_timeline_fill_then_protection():
    """连续时序：创建抛异常 → 收编 → 交易所实际成交 → 保护处理。

    ChatGPT 第六轮复审：B15c「另建了一个已填好状态的替身，未驱动这条实际接管线程」。
    这里用**接管实际传入的 kwargs** 驱动真实监控，不另建替身。"""
    f, _ = _reconciled_run()
    if not f.monitor_kwargs:
        report('B21a 连续时序：收编后启动监控', False, '接管未启动，前置不成立')
        return
    k = dict(f.monitor_kwargs[0])
    d = tempfile.mkdtemp(prefix='blk21_')
    sp = os.path.join(d, 'trade_state.json')
    trader_260725.STATE_FILE = sp
    try:
        _realistic_seed(sp, entry_orders=list(k.get('entry_orders') or []),
                        stop_steps=list(k.get('stop_steps') or [55000.0]),
                        target_amounts=list(k.get('target_amounts') or [0.43]),
                        current_sl_id=None, last_filled_count=0,
                        pending_sl_orders=[0], protection_registry={})
        mf = PR._make_fake(sp, PR._disk(sp))
        _init_poll_tracking(mf)
        mf.exchange.fetch_open_orders.side_effect = None
        mf.exchange.fetch_open_orders.return_value = []      # 已不在未结 = 已成交
        mf.exchange.fetch_order.return_value = {
            'id': REAL_OID, 'status': 'closed', 'average': 58000.0,
            'info': {'cumQuote': '24940', 'executedQty': '0.43', 'updateTime': 1}}
        mf._get_current_position_amt = lambda *a, **k2: 0.43
        PR._drive(mf, None, max_rounds=3)
        report('B21a 连续时序：收编 ID → 成交识别 → 进入补挂保护路径',
               mf.exchange.fetch_order.call_count >= 1 and len(mf.sl_place_calls) >= 1,
               f'fetch_order={mf.exchange.fetch_order.call_count}，'
               f'补挂路径={len(mf.sl_place_calls)}')
        me = PR._disk(sp).get(SYMBOL, {}).get(BATCH, {}).get('monitor_error')
        report('B21b 连续时序：监控未因越界异常退出（未写 monitor_error）',
               not me, f'monitor_error={me}')
    finally:
        trader_260725.STATE_FILE = PR._real_state_file


def test_block22_takeover_refused_when_persist_unconfirmed():
    """收编写盘**未确认** → 收编不算数 → 不得启动监控接管，且仍须保持暂停。"""
    f, sig = _two_layer_fake()
    _bind_real_reconcile(f)
    f._state_lock = threading.RLock()
    f.monitor_kwargs = []
    f._start_monitoring = lambda *a, **k: f.monitor_kwargs.append(k)
    f.exchange.fetch_open_orders.side_effect = None
    f.exchange.fetch_open_orders.return_value = [{
        'id': REAL_OID, 'symbol': SYMBOL, 'type': 'STOP_MARKET', 'side': 'buy',
        'price': 77000.0, 'amount': 0.43, 'status': 'open'}]
    f.exchange.create_order.side_effect = RuntimeError('创建结果未知')
    f.persist_ok = False                    # 注入：写盘返回 False
    CryptoTrader.execute_signal(f, sig)
    time.sleep(0.3)
    report('B22 收编落盘未确认 → 不得启动监控接管',
           len(f.monitor_kwargs) == 0, f'启动次数={len(f.monitor_kwargs)}（应 0）')
    report('B22b 收编落盘未确认 → 仍置未决意图集合（持续禁止新增风险）',
           'batch_new_001' in f._unresolved_intent_batches,
           f'未决意图={f._unresolved_intent_batches}')


# ── B23/B24：「持续禁止新增风险」必须真的持续（第六轮复审阻断2）──────────
def test_block23_unresolved_survives_poll_recovery():
    f, sig = _two_layer_fake()
    _bind_real_reconcile(f)
    f._state_lock = threading.RLock()
    f._start_monitoring = lambda *a, **k: None
    f.exchange.fetch_open_orders.side_effect = RuntimeError('交易所不可达')
    f.exchange.create_order.side_effect = RuntimeError('创建结果未知')
    CryptoTrader.execute_signal(f, sig)
    time.sleep(0.3)
    in_set = 'batch_new_001' in f._unresolved_intent_batches
    # 模拟「一次正常轮询后」通用恢复分支把降级标记洗掉
    with f._poll_alert_lock:
        f._poll_degraded_batches.discard('batch_new_001')
        f._poll_fail_streak['batch_new_001'] = 0
    report('B23a 正常轮询洗掉降级标记后，未决意图标记仍在',
           in_set and 'batch_new_001' in f._unresolved_intent_batches,
           f'未决意图={f._unresolved_intent_batches}（不得被通用恢复分支清掉）')
    created = []
    f.exchange.create_order.side_effect = (
        lambda **kk: created.append(kk) or {'id': 'X'})
    # ⚠️ 不得手工补降级标记 —— 那样旧实现也会被降级闸门拦住、测试失去区分度。
    # 这里保持降级为空：旧实现只看降级集合 → 会真的发出 create；
    # 新实现读未决意图集合 → 同样阻断。
    CryptoTrader.execute_signal(f, sig)
    time.sleep(0.3)
    report('B23b 未决意图未清除时，新 ENTRY 创建仍被阻断（降级已清空）',
           len(created) == 0, f'本轮 create 调用={len(created)}（应 0）')


def test_block24_restart_rejects_unresolved_skeleton():
    """重启：未决骨架（无本地订单 + 查询仓位为零）也必须拒绝 READY。"""
    d = tempfile.mkdtemp(prefix='blk24_')
    sp = os.path.join(d, 'trade_state.json')
    trader_260725.STATE_FILE = sp
    try:
        _realistic_seed(sp, entry_orders=[], current_sl_id=None,
                        last_filled_count=0, pending_sl_orders=[],
                        protection_registry={
                            f'{BATCH}|ENTRY|L0|LONG': {
                                'role': 'ENTRY', 'state': 'PENDING_CREATE',
                                'order_id': None, 'id_known': False}})
        fake = PR._make_fake(sp, PR._disk(sp))
        _init_poll_tracking(fake)
        fake.exchange.fetch_open_orders.side_effect = None
        fake.exchange.fetch_open_orders.return_value = []
        fake.exchange.fetch_order.return_value = {'status': 'closed'}
        fake._get_current_position_amt = lambda *a, **k: 0.0   # 仓位为零
        ok = CryptoTrader.recover_active_batches(fake)
        report('B24 重启后未决骨架（无本地订单 + 零仓位）→ 不得 READY',
               ok is False and fake._ready is False,
               f'recover={ok!r}，_ready={fake._ready}')
        report('B24b 重启后由账本重建未决意图集合（不依赖内存态）',
               BATCH in fake._unresolved_intent_batches,
               f'未决意图={fake._unresolved_intent_batches}')
    finally:
        trader_260725.STATE_FILE = PR._real_state_file


# ── B25：Hedge Mode SL 反例——错 positionSide / 错类型 / NaN / inf 覆盖量 ──
def test_block25_hedge_sl_variants():
    for label, over in (
            ('positionSide 错', {'id': 'SL1', 'type': 'STOP_MARKET', 'side': 'SELL',
                                 'amount': 0.43, 'positionSide': 'SHORT'}),
            ('非止损类型', {'id': 'SL1', 'type': 'LIMIT', 'side': 'SELL',
                            'amount': 0.43, 'positionSide': 'LONG'}),
            ('覆盖量 NaN', {'id': 'SL1', 'type': 'STOP_MARKET', 'side': 'SELL',
                            'amount': float('nan'), 'positionSide': 'LONG'}),
            ('覆盖量 inf', {'id': 'SL1', 'type': 'STOP_MARKET', 'side': 'SELL',
                            'amount': float('inf'), 'positionSide': 'LONG'})):
        d = tempfile.mkdtemp(prefix='blk25_')
        sp = os.path.join(d, 'trade_state.json')
        trader_260725.STATE_FILE = sp
        try:
            _realistic_seed(sp, current_sl_id='SL1', is_hedge_mode=True)
            fake = PR._make_fake(sp, PR._disk(sp))
            _init_poll_tracking(fake)
            fake.exchange.fetch_open_orders.side_effect = None
            fake.exchange.fetch_open_orders.return_value = [over]
            # ⚠️ 只有 SL1 回查返回 open；ENTRY 必须 closed，否则 ENTRY 判明那一步
            # 就先红了，测试会打在另一个原因上、失去区分度。
            def _fo25(oid, sym=None, *a, **kk):
                if str(oid) == 'SL1':
                    return dict(over, id='SL1', status='open')
                return {'id': str(oid), 'status': 'closed', 'average': 58000.0,
                        'info': {'cumQuote': '24940', 'executedQty': '0.43'}}
            fake.exchange.fetch_order.side_effect = _fo25
            fake._get_current_position_amt = lambda *a, **k: 0.43
            ok = CryptoTrader.recover_active_batches(fake)
            report(f'B25 Hedge SL {label} → 不得 READY',
                   ok is False and fake._ready is False,
                   f'recover={ok!r}，_ready={fake._ready}')
        finally:
            trader_260725.STATE_FILE = PR._real_state_file


def main():
    tests = [
        test_single_batch_persistent_failure_alerts_and_marks_degraded,
        test_new_signal_blocked_when_degraded,
        test_multi_batch_one_success_one_failure,
        test_complete_recovery_after_failure,
        test_notification_exception_kills_nothing,
        test_existing_sl_tp_and_monitor_not_touched,
        # ═══ ChatGPT 第三轮复审（4edd39a 不可合入）═══
        test_block5_unfilled_entry_does_not_block_recovery,
        test_block6_partial_create_not_clean_reject,
        test_block7_restart_reverify_blocks_new_entry,
        test_block8_notify_none_is_not_delivered,
        test_block9_real_dual_batch_stale,
        test_block10_write_false_no_clean_reject,
        test_block11_unknown_create_has_takeover,
        test_block12_reverify_inconsistent_success,
        test_block13_reverify_position_without_sl,
        test_block14_sl_failure_keeps_paused,
        test_block15_unknown_create_real_monitor_takeover,
        test_block16_sl_vanished_on_exchange,
        test_block17_sl_direction_and_coverage,
        test_block18_clean_reject_no_fake_exit,
        test_block19_reconcile_drives_takeover,
        test_block20_takeover_params_layer_consistent,
        test_block21_continuous_timeline_fill_then_protection,
        test_block22_takeover_refused_when_persist_unconfirmed,
        test_block23_unresolved_survives_poll_recovery,
        test_block24_restart_rejects_unresolved_skeleton,
        test_block25_hedge_sl_variants,
        test_block1_fetch_order_failure_keeps_degraded,
        test_block2_degrade_after_check_blocks_entry_create,
        test_block3_per_batch_stale_not_masked_by_other_batch,
        test_block4_alert_retry_on_notify_false,
    ]
    for fn in tests:
        try:
            fn()
        except Exception as e:
            report(f'{fn.__name__} 异常', False, f'{type(e).__name__}: {e}')
    passed = sum(1 for _, p in RESULTS if p)
    print(f'\nGREEN: {passed}/{len(RESULTS)}')
    return 0 if passed == len(RESULTS) else 1


if __name__ == '__main__':
    raise SystemExit(main())
