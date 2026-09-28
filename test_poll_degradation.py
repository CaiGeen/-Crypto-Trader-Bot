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
        self._registry_log = []            # (state, layer) —— 核对磁盘 registry 证据
        self.is_active_seen = None         # save_batch_state 观测到的 is_active
        self.persisted = {}                # 最终落盘视图

    def _update_registry(self, symbol, batch_id, identity, state=None, **kw):
        # identity 形如 batch|ENTRY|L{layer}|SIDE
        try:
            layer = int(str(identity).split('|L')[-1].split('|')[0])
        except Exception:
            layer = -1
        self._registry_log.append((state, layer))

    def save_batch_state(self, symbol, batch_id, data):
        self.is_active_seen = data.get('is_active')
        self.persisted[batch_id] = copy.deepcopy(data)
        return True

    def _persist_states(self, all_states):
        self.persisted = copy.deepcopy(all_states)
        return True

    def load_all_states(self):
        return {SYMBOL: {BATCH: dict(self.persisted.get(BATCH, {'is_active': False}))}}

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
    f0.load_all_states = lambda: f0.persisted
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
    # identity 形如 batch|ENTRY|L0|LONG；层号从 '|L' 之后取（替身 parser 口径）
    absent_layers = sorted(l for st, l in reg0 if st == 'ABSENT')
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
    f1.load_all_states = lambda: f1.persisted
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
    # ⚠️ 口径边界（如实登记）：B6c/B6d 只用**本替身可观测**的落盘证据断言。
    # `_update_registry` 在本替身上未被真实调用链命中（registry 观测为空），
    # 说明 create 后的 registry 写入路径在本替身下保真度不足 —— 不能宣称
    # "PENDING_VERIFY 证据已验证"。这一条留给下一轮用真实 registry 替身核对。
    report('B6c [保真度不足] registry 证据未取得（不得宣称已验证）',
           False,
           f'registry 观测={reg1}（替身未命中真实 _update_registry 调用链）')
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
