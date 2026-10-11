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
from poll_alert_state import PollAlertBudget
import test_monitor_poll_recovery as PR

SYMBOL = PR.SYMBOL
REAL_OID = 'REAL_EXCHANGE_OID_1'
# 🔥 收敛审查 S6：模拟真实 _start_monitoring 的"无限循环存活"。
# 永不置位；daemon 线程随进程退出回收。
_BLOCK = threading.Event()
BATCH = PR.BATCH
ENTRY_ID = PR.ENTRY_ID

# R5门禁（漏项⑦）状态账本**会话级恢复目标**：下面 27 处 finally 原本恢复到
# `PR._real_state_file`（解释器启动时捕获的**默认** `trade_state.json`，相对
# cwd = 仓库根）。部分用例经 takeover 路径（trader_260725.py L4239/L7905）把
# **真实** `_start_monitoring` 落在 daemon 线程里持续跑（「进程退出收尸」的
# 既有设计），该线程下一轮 `_persist_states`（REAL_HELPERS 真绑定，见
# test_monitor_poll_recovery.py L64/L233）读的是**当下**全局 STATE_FILE——
# 测试 finally 已把全局恢复成默认 → 线程越界写仓库根账本。
# 脚本模式窗口仅毫秒（一直靠运气干净）；pytest 收集段整会话常驻 → 必然越界：
#   门禁哨兵实录 trade_state.json 1916→1917 字节、tmp_pytest_watch.out
#   26 处 os.replace/copy2 栈全部同源（本文件 L77 → _start_monitoring →
#   _record_fill_evidence → _persist_states）。
# 恢复目标改为会话级 temp：越界线程与后续用例永远够不到默认账本。
# 判据零改动（27 处仅换恢复目标；每用例开头仍各自 mkdtemp 指向独立 sp）。
_SESSION_STATE = os.path.join(
    tempfile.mkdtemp(prefix='pd_session_'), 'trade_state.json')

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
    fake._poll_alert_budget = PollAlertBudget(os.path.join(
        tempfile.mkdtemp(prefix='pd_notify_'), '.poll_alert.state.json'))
    fake._poll_email_calls = []
    fake._send_email_alert = lambda *a, **k: (fake._poll_email_calls.append(k) or True)
    fake._send_poll_degraded_tg = lambda text: (
        fake.sent.append(('critical', str(text))) or 'ACCEPTED')
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
        trader_260725.STATE_FILE = _SESSION_STATE


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
        self._active_monitors = set()
        self._active_monitors_lock = threading.RLock()
        self._active_monitor_generations = {}
        self._poll_alert_active = False
        self._finish_monitor_takeover = lambda life: CryptoTrader._finish_monitor_takeover(self, life)
        self._run_monitor_takeover = lambda lifecycle, **kw: CryptoTrader._run_monitor_takeover(
            self, lifecycle, **kw)
        self._monitor_terminal_evidence = lambda s, b: CryptoTrader._monitor_terminal_evidence(
            self, s, b)

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

    def _persist_states(self, all_states, **_k):
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

    def _load_all_states_ex(self):
        # per-read 三元组（外部复审第 6 轮迁移）：与 load_all_states 同源，
        # 损坏恒 False（本文件账本由用例直写，不存在损坏态）。
        return self.load_all_states(), False, ""

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
        trader_260725.STATE_FILE = _SESSION_STATE


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
        trader_260725.STATE_FILE = _SESSION_STATE


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

        def _boom(text):
            raise RuntimeError('TG 发送失败')
        fake._send_poll_degraded_tg = _boom
        fake._send_email_alert = mock.MagicMock(return_value=True)
        rounds, err = _drive_failing(fake, 5)
        report('T5 通知异常未杀监控（驱动未因告警异常中断）',
               rounds >= 3 and err is None, f'驱动轮次={rounds} err={err!r}')
    finally:
        trader_260725.STATE_FILE = _SESSION_STATE


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
        trader_260725.STATE_FILE = _SESSION_STATE


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
        trader_260725.STATE_FILE = _SESSION_STATE


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
        trader_260725.STATE_FILE = _SESSION_STATE


# ── 阻断4：明确失败受冷却和事件预算约束 ───────────────────────────────────
def test_block4_alert_retry_on_notify_false():
    d = tempfile.mkdtemp(prefix='blk4_')
    sp = os.path.join(d, 'trade_state.json')
    trader_260725.STATE_FILE = sp
    try:
        PR._seed(sp)
        fake = PR._make_fake(sp, PR._disk(sp))
        _init_poll_tracking(fake)
        calls = []

        def _returns_failed(text):
            calls.append('critical')
            return 'FAILED'
        fake._send_poll_degraded_tg = _returns_failed
        fake.exchange.fetch_open_orders.side_effect = RuntimeError('持续失败')
        PR._drive(fake, None, max_rounds=6)
        event_id = fake._poll_alert_budget.open_event_id()
        _last = fake._poll_alert_budget.attempts(event_id, 'tg')[-1]['at']
        with mock.patch.object(trader_260725.time, 'time', return_value=_last + 899):
            CryptoTrader._alert_poll_degraded(fake, 6, 200, BATCH)
        report('B4a 900秒冷却内不重复尝试TG', len(calls) == 1,
               f'冷却前告警尝试次数={len(calls)}（应为1）')
        with mock.patch.object(trader_260725.time, 'time', return_value=_last + 900):
            CryptoTrader._alert_poll_degraded(fake, 9, 300, BATCH)
        report('B4b 明确失败且冷却到期可重试一次', len(calls) == 2,
               f'冷却到期告警尝试次数={len(calls)}（应为2）')
        report('B4c 重试不重复发送邮件', len(fake._poll_email_calls) == 1,
               f'邮件发送路径次数={len(fake._poll_email_calls)}（应为1）')
    finally:
        trader_260725.STATE_FILE = _SESSION_STATE


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
        trader_260725.STATE_FILE = _SESSION_STATE


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
        trader_260725.STATE_FILE = _SESSION_STATE


# ── 口径：结果未知不得重发；保留额度证据 ──────────────────────────────────
def test_block8_notify_none_is_not_delivered():
    d = tempfile.mkdtemp(prefix='blk8_')
    sp = os.path.join(d, 'trade_state.json')
    trader_260725.STATE_FILE = sp
    try:
        _realistic_seed(sp)
        fake = PR._make_fake(sp, PR._disk(sp))
        _init_poll_tracking(fake)
        calls = []
        fake._send_poll_degraded_tg = lambda text: (calls.append(text) or 'UNKNOWN')
        fake.exchange.fetch_open_orders.side_effect = RuntimeError('持续失败')
        PR._drive(fake, None, max_rounds=6)
        event_id = fake._poll_alert_budget.open_event_id()
        _history = fake._poll_alert_budget.attempts(event_id, 'tg')
        report('B8a UNKNOWN 不冒充送达且保留结果态',
               len(_history) == 1 and _history[0]['status'] == 'UNKNOWN',
               f'tg_history={_history!r}')
        report('B8b UNKNOWN 不自动重复发送避免重复投递',
               len(calls) == 1, f'6轮内TG发送尝试={len(calls)}（应为1）')
    finally:
        trader_260725.STATE_FILE = _SESSION_STATE


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
        trader_260725.STATE_FILE = _SESSION_STATE


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
    # 🔥 收敛审查 S6：替身必须模拟真实监控的**存活**（_start_monitoring 是无限
    # 循环）。若替身立即返回，接管代码的 is_alive() 会判"进入主体后已退出"，
    # 未决闸门不解除 → B15b/B19b 假红。用永不置位的事件阻塞（daemon 线程，
    # 进程退出时自动回收）。
    def _start_stub(*a, **k):
        f.monitoring_started.append(k.get('batch_id'))
        f.monitoring_started_ids.extend(k.get('entry_orders') or [])
        _BLOCK.wait()
    f._start_monitoring = _start_stub
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
    report('B19b 只有收编、未完成业务轮询 → 闸门保持',
           'batch_new_001' in f._unresolved_intent_batches
           and 'batch_new_001' in f._poll_degraded_batches,
           f'unresolved={f._unresolved_intent_batches}, degraded={f._poll_degraded_batches}')

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
        trader_260725.STATE_FILE = _SESSION_STATE


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
        trader_260725.STATE_FILE = _SESSION_STATE


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
        trader_260725.STATE_FILE = _SESSION_STATE


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
    report('B15b 替身未运行首轮业务循环 → 闸门必须保持',
           'batch_new_001' in f._unresolved_intent_batches
           and 'batch_new_001' in f._poll_degraded_batches,
           f'unresolved={f._unresolved_intent_batches}, degraded={f._poll_degraded_batches}')

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
                'side': 'buy',   # R5门禁：方向观测证据（LONG 批次入场单=buy）
                'info': {'cumQuote': '24940', 'executedQty': '0.43', 'updateTime': 1}}
            mf._get_current_position_amt = lambda *a, **k: 0.43
            _drive_monitor_with_reconciled_ids(
                mf, [REAL_OID], [55000.0], [0.43], PR._layer_sl_params(),
                max_rounds=3)
            report('B15c 真实监控按真实 ID 识别该 ENTRY 成交并进入补挂保护路径',
                   mf.exchange.fetch_order.call_count >= 1
                   and len(mf.sl_place_calls) >= 1,
                   f'fetch_order={mf.exchange.fetch_order.call_count} 次，'
                   f'补挂路径进入={len(mf.sl_place_calls)} 次'
                   f'（fetch_order=0 即监控认不出该 ENTRY）')
        finally:
            trader_260725.STATE_FILE = _SESSION_STATE


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
        trader_260725.STATE_FILE = _SESSION_STATE


# ── B17：SL 方向错 / 覆盖不足 / 覆盖量不明（第五轮复审阻断2）─────────────
def test_block17_sl_direction_and_coverage():
    # ⚠️ 夹具带 **info.type=STOP_MARKET + info.positionSide=LONG**（S6 收敛后
    #    运行期/启动重证共用类型白名单与 positionSide 判据；本文件 `_realistic_seed`
    #    是 Hedge + LONG）。缺这两个字段的话三个反例会**全部**先在类型/仓位方向
    #    这两维被拒，B17 名义上要区分的方向/覆盖/覆盖量就失去区分度（红的不是它测的那条）。
    for label, order in (
            ('方向错', {'id': 'SL1', 'side': 'BUY', 'amount': 0.43,
                     'info': {'type': 'STOP_MARKET', 'positionSide': 'LONG'}}),
            ('覆盖不足', {'id': 'SL1', 'side': 'SELL', 'amount': 0.1,
                     'info': {'type': 'STOP_MARKET', 'positionSide': 'LONG'}}),
            ('覆盖量不明', {'id': 'SL1', 'side': 'SELL', 'amount': None,
                     'info': {'type': 'STOP_MARKET', 'positionSide': 'LONG'}})):
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
            trader_260725.STATE_FILE = _SESSION_STATE


# ── B20/B21/B22：接管参数逐层一致 + 连续时序（第六轮复审阻断1）───────────
def _drive_monitor_with_reconciled_ids(fake, entry_orders, stop_steps,
                                       target_amounts, layer_sl_params,
                                       max_rounds=3):
    """用接管的真实 ENTRY ID 驱动监控，不退回 PR._drive 的固定 e1 fixture。

    F2/F3 会严格校验响应 ID 与本层账本 ID。旧 _drive 固定传 ENTRY_ID='e1'，
    而 B15c/B21 的响应来自真实收编 ID；因此旧驱动制造 identity_mismatch，
    是 harness 输入不一致，不是生产拒绝有效成交证据。
    """
    calls = {'n': 0}

    def _sleep(_sec):
        calls['n'] += 1
        if calls['n'] > max_rounds:
            raise PR._StopLoop()

    with mock.patch.object(trader_260725.time, 'sleep', _sleep):
        try:
            CryptoTrader._start_monitoring(
                fake, SYMBOL, BATCH, list(entry_orders), list(stop_steps),
                60000.0, None, None, sum(target_amounts), list(target_amounts),
                {'positionSide': 'LONG', 'leverage': 100}, True, 'BUY',
                0, None, 0.0, None, {}, list(layer_sl_params))
        except PR._StopLoop:
            pass
    return calls['n']


def _reconciled_run():
    """跑到「创建抛未知异常 → 真实对账」这一步的替身。"""
    f, sig = _two_layer_fake()
    _bind_real_reconcile(f)
    f._state_lock = threading.RLock()
    f.monitor_kwargs = []
    # 🔥 收敛审查 S6：替身必须模拟真实监控的存活 —— 真实 _start_monitoring
    # 是无限循环，线程进入后 is_alive() 恒为 True。若替身立即返回或 sleep
    # 太短，is_alive() 竞态会让 B15b/B19b 假红（线程已退出 → 闸门不解除）。
    # 用永不置位的事件阻塞（daemon 线程，进程退出时自动回收）。
    _block = threading.Event()
    def _alive_stub(*a, **k):
        f.monitor_kwargs.append(k)
        _block.wait()         # 模拟监控主循环：永不返回
    f._start_monitoring = _alive_stub
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
            'side': 'buy',   # R5门禁：方向观测证据（LONG 批次入场单=buy）
            'info': {'cumQuote': '24940', 'executedQty': '0.43', 'updateTime': 1}}
        mf._get_current_position_amt = lambda *a, **k2: 0.43
        _drive_monitor_with_reconciled_ids(
            mf, list(k.get('entry_orders') or []),
            list(k.get('stop_steps') or [55000.0]),
            list(k.get('target_amounts') or [0.43]),
            list(k.get('layer_sl_params') or []), max_rounds=3)
        report('B21a 连续时序：收编 ID → 成交识别 → 进入补挂保护路径',
               mf.exchange.fetch_order.call_count >= 1 and len(mf.sl_place_calls) >= 1,
               f'fetch_order={mf.exchange.fetch_order.call_count}，'
               f'补挂路径={len(mf.sl_place_calls)}')
        me = PR._disk(sp).get(SYMBOL, {}).get(BATCH, {}).get('monitor_error')
        report('B21b 连续时序：监控未因越界异常退出（未写 monitor_error）',
               not me, f'monitor_error={me}')
    finally:
        trader_260725.STATE_FILE = _SESSION_STATE


def test_block22_takeover_refused_when_persist_unconfirmed():
    """收编写盘**未确认** → 收编不算数 → 不得启动监控接管，且仍须保持暂停。"""
    f, sig = _two_layer_fake()
    _bind_real_reconcile(f)
    f._state_lock = threading.RLock()
    f.monitor_kwargs = []
    # 🔥 收敛审查 S6：替身必须模拟真实监控的存活 —— 真实 _start_monitoring
    # 是无限循环，线程进入后 is_alive() 恒为 True。若替身立即返回或 sleep
    # 太短，is_alive() 竞态会让 B15b/B19b 假红（线程已退出 → 闸门不解除）。
    # 用永不置位的事件阻塞（daemon 线程，进程退出时自动回收）。
    _block = threading.Event()
    def _alive_stub(*a, **k):
        f.monitor_kwargs.append(k)
        _block.wait()         # 模拟监控主循环：永不返回
    f._start_monitoring = _alive_stub
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
        trader_260725.STATE_FILE = _SESSION_STATE


# ── B25：Hedge Mode SL 反例——错 positionSide / 错类型 / NaN / inf 覆盖量 ──
def test_block25_hedge_sl_variants():
    for label, over in (
            ('positionSide 错', _ccxt_order(position_side='SHORT')),
            ('非止损类型', _ccxt_order(kind='LIMIT', top_type='limit',
                                       stop_price=None)),
            ('覆盖量 NaN', _ccxt_order(amount=float('nan'))),
            ('覆盖量 inf', _ccxt_order(amount=float('inf')))):
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
            trader_260725.STATE_FILE = _SESSION_STATE


# ── B26：收编成功但参数不完整 → 无监控时仍禁止新 ENTRY（第七轮复审阻断1）──
def test_block26_refused_takeover_still_blocks_new_entry():
    # B26a 收编已确认落盘，但重建出的**参数无效**（数量/止损价为 0、layer_sl_params 空）
    # → 旧自检只看列表长度会放行；新自检必须拒绝接管，且**仍置未决闸门**。
    f, sig = _two_layer_fake()
    _bind_real_reconcile(f)
    f._state_lock = threading.RLock()
    f.monitor_kwargs = []
    # 🔥 收敛审查 S6：替身必须模拟真实监控的存活 —— 真实 _start_monitoring
    # 是无限循环，线程进入后 is_alive() 恒为 True。若替身立即返回或 sleep
    # 太短，is_alive() 竞态会让 B15b/B19b 假红（线程已退出 → 闸门不解除）。
    # 用永不置位的事件阻塞（daemon 线程，进程退出时自动回收）。
    _block = threading.Event()
    def _alive_stub(*a, **k):
        f.monitor_kwargs.append(k)
        _block.wait()         # 模拟监控主循环：永不返回
    f._start_monitoring = _alive_stub
    f.exchange.fetch_open_orders.side_effect = None
    f.exchange.fetch_open_orders.return_value = [{
        'id': REAL_OID, 'symbol': SYMBOL, 'type': 'STOP_MARKET', 'side': 'buy',
        'price': 77000.0, 'amount': 0.43, 'status': 'open'}]
    f.exchange.create_order.side_effect = RuntimeError('创建结果未知')
    _real_rebuild = f._rebuild_entry_orders_from_registry

    def _poisoned_rebuild(*a, **k):
        orders, ok = _real_rebuild(*a, **k)
        b = f.persisted.get('batch_new_001', {})     # 收编后重读到的就是这份
        b['target_amounts'] = [0.0]
        b['stop_steps'] = [0.0]
        b['layer_sl_params'] = []
        return orders, ok
    f._rebuild_entry_orders_from_registry = _poisoned_rebuild
    CryptoTrader.execute_signal(f, sig)
    time.sleep(0.4)
    report('B26a 数量/止损价为 0 或 layer_sl_params 为空 → 不得放行接管',
           len(f.monitor_kwargs) == 0,
           f'启动次数={len(f.monitor_kwargs)}（应 0；旧自检只看长度会误放行）')
    report('B26a2 收编成功但参数无效 → 仍置未决闸门（无监控也禁止新风险）',
           'batch_new_001' in f._unresolved_intent_batches,
           f'未决意图={f._unresolved_intent_batches}')
    created = []
    f.exchange.create_order.side_effect = (
        lambda **kk: created.append(kk) or {'id': 'Y'})
    f._poll_degraded_batches.discard('batch_new_001')
    CryptoTrader.execute_signal(f, sig)
    time.sleep(0.3)
    report('B26a3 闸门在位时新 ENTRY 创建被阻断',
           len(created) == 0, f'本轮 create 调用={len(created)}（应 0）')

    # B26b 线程启动失败 → 同一个出口，同样必须置未决闸门
    f2, sig2 = _two_layer_fake()
    f2._start_monitoring = lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError('线程启动失败'))
    f2.exchange.create_order.side_effect = RuntimeError('创建结果未知')
    CryptoTrader.execute_signal(f2, sig2)
    time.sleep(0.3)
    report('B26b 线程启动失败 → 未决闸门仍置位（无监控也禁止新风险）',
           'batch_new_001' in f2._unresolved_intent_batches,
           f'未决意图={f2._unresolved_intent_batches}')


# ── B27：止盈单 / positionSide 缺失不得认作 SL（第七轮复审阻断2）──────────
def test_block27_non_stop_and_missing_pside():
    for label, order in (
            ('TAKE_PROFIT_MARKET 带 stopPrice',
             _ccxt_order(kind='TAKE_PROFIT_MARKET')),
            ('positionSide 缺失', _ccxt_order(position_side='')),
            ('positionSide=BOTH', _ccxt_order(position_side='BOTH'))):
        d = tempfile.mkdtemp(prefix='blk27_')
        sp = os.path.join(d, 'trade_state.json')
        trader_260725.STATE_FILE = sp
        try:
            _realistic_seed(sp, current_sl_id='SL1', is_hedge_mode=True)
            fake = PR._make_fake(sp, PR._disk(sp))
            _init_poll_tracking(fake)
            fake.exchange.fetch_open_orders.side_effect = None
            fake.exchange.fetch_open_orders.return_value = [order]
            fake.exchange.fetch_order.side_effect = _sl_open_then_entry_closed(order)
            fake._get_current_position_amt = lambda *a, **k: 0.43
            ok = CryptoTrader.recover_active_batches(fake)
            report(f'B27 Hedge {label} → 不得 READY',
                   ok is False and fake._ready is False,
                   f'recover={ok!r}，_ready={fake._ready}')
        finally:
            trader_260725.STATE_FILE = _SESSION_STATE


def _ccxt_order(kind='STOP_MARKET', side='SELL', amount=0.43,
                position_side='LONG', stop_price='55000.0',
                top_type='market', oid='SL1', status='open',
                reduce_only='false', close_position='false'):
    """手工构造的、模拟预期 Binance USD-M ccxt 字段布局的测试夹具。

    注意：未关联任何实际脱敏载荷或 ccxt fixture 文件；仅用于按仓库既有
    `info` 字段契约检查代码对顶层归一化类型的处理，不构成实测 ccxt 校准证据。
    """
    return {
        'id': oid, 'status': status, 'type': top_type, 'side': side,
        'amount': amount, 'price': None,
        'info': {
            'type': kind, 'positionSide': position_side,
            'stopPrice': stop_price, 'reduceOnly': reduce_only,
            'closePosition': close_position,
            'origQty': str(amount), 'side': str(side).upper(),
        },
    }


def _sl_open_then_entry_closed(order):
    def _fo(oid, sym=None, *a, **k):
        if str(oid) == 'SL1':
            return dict(order, id='SL1', status='open')
        return {'id': str(oid), 'status': 'closed', 'average': 58000.0,
                'info': {'cumQuote': '24940', 'executedQty': '0.43'}}
    return _fo


# ── B28：未决闸门必须有经核实后的解除路径，且普通轮询不得解除 ────────────
def test_block28_unresolved_gate_release_path():
    d = tempfile.mkdtemp(prefix='blk28_')
    sp = os.path.join(d, 'trade_state.json')
    trader_260725.STATE_FILE = sp
    try:
        # 已核实：registry 全部 CONFIRMED，且账本有真实 order_id
        _realistic_seed(sp, current_sl_id='SL1', protection_registry={
            f'{BATCH}|ENTRY|L0|LONG': {
                'role': 'ENTRY', 'state': 'CONFIRMED',
                'order_id': ENTRY_ID, 'id_known': True}})
        fake = PR._make_fake(sp, PR._disk(sp))
        _init_poll_tracking(fake)
        fake._unresolved_intent_batches.add(BATCH)     # 之前置入
        fake.exchange.fetch_open_orders.side_effect = None
        _sl_rv = _ccxt_order()
        fake.exchange.fetch_open_orders.return_value = [_sl_rv]
        fake.exchange.fetch_order.side_effect = _sl_open_then_entry_closed(_sl_rv)
        fake._get_current_position_amt = lambda *a, **k: 0.43
        CryptoTrader.recover_active_batches(fake)
        report('B28 意图已核实且账本无未决 → 解除新增风险闸门',
               BATCH not in fake._unresolved_intent_batches,
               f'未决意图={fake._unresolved_intent_batches}（应为空）')
    finally:
        trader_260725.STATE_FILE = _SESSION_STATE


# ── B29：重证必须读恢复后最新账本（刚收编的批次不得仍判未决）──────────
def test_block29_reverify_uses_post_recovery_ledger():
    d = tempfile.mkdtemp(prefix='blk29_')
    sp = os.path.join(d, 'trade_state.json')
    trader_260725.STATE_FILE = sp
    try:
        _realistic_seed(sp, entry_orders=[ENTRY_ID], current_sl_id='SL1',
                        protection_registry={
                            f'{BATCH}|ENTRY|L0|LONG': {
                                'role': 'ENTRY', 'state': 'CONFIRMED',
                                'order_id': ENTRY_ID, 'id_known': True}})
        fake = PR._make_fake(sp, PR._disk(sp))
        _init_poll_tracking(fake)
        # 恢复前 all_states 快照里 registry 是 PENDING_CREATE（未收编）；
        # 恢复流程收编后会写回 CONFIRMED —— 重证必须读**恢复后**的账本。
        pre = PR._disk(sp)
        pre[SYMBOL][BATCH]['protection_registry'] = {
            f'{BATCH}|ENTRY|L0|LONG': {
                'role': 'ENTRY', 'state': 'PENDING_CREATE',
                'order_id': None, 'id_known': False}}
        orig_load = fake.load_all_states
        seq = {'n': 0}

        def _load():
            seq['n'] += 1
            return copy.deepcopy(pre) if seq['n'] <= 1 else orig_load()
        fake.load_all_states = _load
        fake.exchange.fetch_open_orders.side_effect = None
        _sl_rv = _ccxt_order()
        fake.exchange.fetch_open_orders.return_value = [_sl_rv]
        fake.exchange.fetch_order.side_effect = _sl_open_then_entry_closed(_sl_rv)
        fake._get_current_position_amt = lambda *a, **k: 0.43
        ok = CryptoTrader.recover_active_batches(fake)
        # ⚠️ 如实收窄：READY 轴本用例**不**断言（recover 另有其他检查未过，
        #    混在一起会掩盖本条要验的判据）。这里只断言「不得仍判未决」——
        #    旧实现读恢复前 all_states，会把刚收编的批次判为未决。
        report('B29 恢复流程已收编 → 重证不得仍判未决（须读恢复后账本）',
               BATCH not in fake._unresolved_intent_batches,
               f'未决意图={fake._unresolved_intent_batches}（应为空），'
               f'load_all_states 调用={seq["n"]} 次，recover={ok!r}（仅作参考）')
    finally:
        trader_260725.STATE_FILE = _SESSION_STATE


# ── B30：未知结果期间先置闸门；监控延迟退出后恢复闸门（S6）──────────────
def test_block30_thread_exits_immediately():
    f, sig = _two_layer_fake()
    _bind_real_reconcile(f)
    f._state_lock = threading.RLock()
    f.monitor_started = []
    # 此替身刻意立即返回，不制造“业务接管完成”信号。
    def _return_without_business_cycle(*a, **k):
        f.monitor_started.append(k.get('batch_id'))
    f._start_monitoring = _return_without_business_cycle
    f.exchange.fetch_open_orders.side_effect = None
    f.exchange.fetch_open_orders.return_value = [{'id': REAL_OID, 'symbol': SYMBOL, 'type': 'STOP_MARKET',
          'side': 'buy', 'price': 77000.0, 'amount': 0.43, 'status': 'open'}]
    f.exchange.create_order.side_effect = RuntimeError('创建结果未知')
    CryptoTrader.execute_signal(f, sig)
    time.sleep(0.1)
    report('B30a 替身线程立即返回且未完成首轮 → 未决闸门保持',
           'batch_new_001' in f._unresolved_intent_batches
           and len(f.monitor_started) == 1,
           f'未决意图={f._unresolved_intent_batches}，监控调用={f.monitor_started}')
    # 无业务接管证据时，新信号仍必须被闸门挡住。
    created_during_takeover = []
    f.exchange.create_order.side_effect = lambda **kk: created_during_takeover.append(kk) or {'id': 'Z'}
    CryptoTrader.execute_signal(f, sig)
    report('B30b 接管期间未决闸门 → 并发新 ENTRY 被阻断',
           len(created_during_takeover) == 0,
           f'本轮 create 调用={len(created_during_takeover)}')


# ── B33：放闸之后监控退出 → 再次发起 ENTRY 必须被重新拦住（S6 收口）────────
def test_block33_release_then_exit_blocks_entry():
    """第八轮复审收口要求③：把「放闸 → 退出 → 再发 ENTRY」这条链**实际**跑一遍。

    B30 只覆盖「接管未完成时闸门保持」；S6b 只覆盖「首轮完成即放闸」。中间缺的是
    **放闸之后**的一次真实退出：线程退出却拿不出安全终结证据 → 闸门必须**重新封上**，
    再来的 ENTRY 不得下单。缺了这一条，S6b 的"放闸"就等价于永久解封。

    三段都走生产实现：`_monitor_takeover_handoff`（放闸）、
    `_finish_monitor_takeover`（退出分类 + 闸门回封）、`execute_signal`（ENTRY 门闸）。
    首尾各带阳性对照，否则"被拦住"可能只是这个替身根本下不了单。
    """
    f, sig = _two_layer_fake()
    _bind_real_reconcile(f)
    # _EntryFake 不走 __init__，只补退出分类真正要读的逐批跟踪变量
    # （不整体调 _init_poll_tracking：它会 patch 该类根本没有的 helper，直接 AttributeError）
    f._poll_first_fail_time = {}
    f._poll_last_success_time = {}
    f._poll_alert_active = False
    f._ready = True
    f._not_ready_reason = ""
    f._state_lock = threading.RLock()
    f.monitor_started = []
    f._start_monitoring = lambda *a, **k: f.monitor_started.append(k.get('batch_id'))
    f.exchange.fetch_open_orders.side_effect = None
    f.exchange.fetch_open_orders.return_value = [{'id': REAL_OID, 'symbol': SYMBOL,
          'type': 'STOP_MARKET', 'side': 'buy', 'price': 77000.0, 'amount': 0.43,
          'status': 'open'}]
    f.exchange.create_order.side_effect = RuntimeError('创建结果未知')
    created = []

    def _create(**kk):
        created.append(kk)
        return {'id': 'Z', 'status': 'open'}

    def _life(gen):
        return {'batch_id': 'batch_new_001', 'symbol': SYMBOL, 'generation': gen,
                'phase': 'starting', 'lock': threading.RLock(),
                'handoff_event': threading.Event(), 'failure_alerted': False,
                'reconciled': True, 'exit_reason': None}

    # ① 首次 ENTRY 创建结果未知 → 意图未决，闸门先封（B30a 的时序起点）
    CryptoTrader.execute_signal(f, sig)
    time.sleep(0.1)
    report('B33a 创建结果未知 → 未决闸门先封（时序起点）',
           'batch_new_001' in f._unresolved_intent_batches and len(f.monitor_started) == 1,
           f'未决意图={f._unresolved_intent_batches}，监控调用={f.monitor_started}')

    # ② 当前代次完成首轮业务轮询 → 真实放闸（S6 解除路径①）
    life = _life('gen-s6h-1')
    f._active_monitor_generations['batch_new_001'] = 'gen-s6h-1'
    f._active_monitors.add('batch_new_001')
    handed = CryptoTrader._monitor_takeover_handoff(f, life)
    report('B33b 真实放闸：handoff 完成且未决闸门解除（S6b 同一条解除路径）',
           handed is True and life['handoff_event'].is_set()
           and 'batch_new_001' not in f._unresolved_intent_batches,
           f'handed={handed}，handoff={life["handoff_event"].is_set()}，'
           f'未决意图={f._unresolved_intent_batches}，'
           f'降级={sorted(f._poll_degraded_batches)}')

    # ③ 放闸后监控线程退出，却拿不出安全终结证据（账本里批次还活着、无墓碑）
    #    → 真实退出分类必须重新封闸 + critical 告警（S6 解除条件②）
    CryptoTrader._finish_monitor_takeover(f, life)
    resealed = (life['phase'] == 'unverified_exit'
                and 'batch_new_001' in f._unresolved_intent_batches
                and 'batch_new_001' in f._poll_degraded_batches)
    report('B33c 放闸后无终结证据的退出 → 闸门重新封上（phase=unverified_exit）',
           resealed,
           f'phase={life["phase"]}，未决意图={f._unresolved_intent_batches}，'
           f'降级={sorted(f._poll_degraded_batches)}')

    # ④ 此时再实际发起 ENTRY：必须被拦住，不得落 create_order
    f.exchange.create_order.side_effect = None
    f.exchange.create_order.side_effect = _create
    ret = CryptoTrader.execute_signal(f, sig)
    time.sleep(0.1)
    report('B33d 放闸后退出 → 再次发起 ENTRY 被拦截（零下单）',
           ret is None and len(created) == 0,
           f'返回={ret!r}，create 调用={len(created)}，'
           f'未决意图={f._unresolved_intent_batches}')

    # ⑤ 阳性对照：同一替身重新完成一轮真实放闸后，同一 ENTRY 必须能落单，
    #    否则④可能只是这个替身根本下不了单（假绿）
    life2 = _life('gen-s6h-2')
    f._active_monitor_generations['batch_new_001'] = 'gen-s6h-2'
    CryptoTrader._monitor_takeover_handoff(f, life2)
    ret2 = CryptoTrader.execute_signal(f, sig)
    time.sleep(0.1)
    report('B33e 阳性对照：重新放闸后同一 ENTRY 确实能落单（证明④不是假绿）',
           life2['handoff_event'].is_set() and len(created) >= 1,
           f'handoff={life2["handoff_event"].is_set()}，ret2={ret2!r}，'
           f'create 调用={len(created)}，未决意图={f._unresolved_intent_batches}')


# ── B31：已收编成功但账本字段不可解析 → 不得抛异常、不得放行（S5）──────
def test_block31_unparsable_ledger_value():
    f, sig = _two_layer_fake()
    _bind_real_reconcile(f)
    f._state_lock = threading.RLock()
    f.monitor_kwargs = []
    f._start_monitoring = lambda *a, **k: f.monitor_kwargs.append(k)
    f.exchange.fetch_open_orders.side_effect = None
    f.exchange.fetch_open_orders.return_value = [{'id': REAL_OID, 'symbol': SYMBOL, 'type': 'STOP_MARKET',
          'side': 'buy', 'price': 77000.0, 'amount': 0.43, 'status': 'open'}]
    f.exchange.create_order.side_effect = RuntimeError('创建结果未知')
    _real_rebuild = f._rebuild_entry_orders_from_registry

    def _poisoned(*a, **k):
        orders, ok = _real_rebuild(*a, **k)
        b = f.persisted.get('batch_new_001', {})
        b['target_amounts'] = ['not-a-number', None]   # 不可解析
        b['stop_steps'] = ['oops', '']
        return orders, ok
    f._rebuild_entry_orders_from_registry = _poisoned
    raised = None
    try:
        CryptoTrader.execute_signal(f, sig)
    except Exception as e:                                  # noqa: BLE001
        raised = e
    time.sleep(0.3)
    report('B31a 账本数量/止损值不可解析 → 不得抛异常',
           raised is None, f'异常={raised!r}（旧实现 float(a) 会直接抛 TypeError）')
    report('B31b 不可解析 → 拒绝启动监控（不得放行）',
           len(f.monitor_kwargs) == 0, f'启动次数={len(f.monitor_kwargs)}（应 0）')
    report('B31c 不可解析 → 未决闸门保持',
           'batch_new_001' in f._unresolved_intent_batches,
           f'未决意图={f._unresolved_intent_batches}')


# ── B32：手工 info 字段夹具契约检查（S7；非实际载荷校准）──────────────────
def test_block32_info_contract_fixture():
    # 32a 按预期 info 字段布局构造的合法 SL → 必须 READY（不得误报）
    d = tempfile.mkdtemp(prefix='blk32a_')
    sp = os.path.join(d, 'trade_state.json')
    trader_260725.STATE_FILE = sp
    try:
        _realistic_seed(sp, current_sl_id='SL1', is_hedge_mode=True)
        fake = PR._make_fake(sp, PR._disk(sp))
        _init_poll_tracking(fake)
        good = _ccxt_order()          # 手工夹具：info.type=STOP_MARKET, top type=market
        fake.exchange.fetch_open_orders.side_effect = None
        fake.exchange.fetch_open_orders.return_value = [good]
        fake.exchange.fetch_order.side_effect = _sl_open_then_entry_closed(good)
        fake._get_current_position_amt = lambda *a, **k: 0.43
        ok = CryptoTrader.recover_active_batches(fake)
        report('B32a 手工 info 契约夹具（STOP_MARKET，顶层 market）→ READY',
               ok is True and fake._ready is True,
               f'recover={ok!r}，_ready={fake._ready}，'
               f'顶层 type={good.get("type")!r}，info.type={good["info"]["type"]!r}')
    finally:
        trader_260725.STATE_FILE = _SESSION_STATE

    # 32b 手工止盈夹具（TAKE_PROFIT_MARKET + stopPrice + positionSide 正确）→ 不得 READY
    for label, order in (
            ('TAKE_PROFIT_MARKET', _ccxt_order(kind='TAKE_PROFIT_MARKET')),
            ('TRAILING_STOP_MARKET 以外的普通 LIMIT',
             _ccxt_order(kind='LIMIT', top_type='limit', stop_price=None))):
        d = tempfile.mkdtemp(prefix='blk32b_')
        sp = os.path.join(d, 'trade_state.json')
        trader_260725.STATE_FILE = sp
        try:
            _realistic_seed(sp, current_sl_id='SL1', is_hedge_mode=True)
            fake = PR._make_fake(sp, PR._disk(sp))
            _init_poll_tracking(fake)
            fake.exchange.fetch_open_orders.side_effect = None
            fake.exchange.fetch_open_orders.return_value = [order]
            fake.exchange.fetch_order.side_effect = _sl_open_then_entry_closed(order)
            fake._get_current_position_amt = lambda *a, **k: 0.43
            ok = CryptoTrader.recover_active_batches(fake)
            report(f'B32b 手工 info 契约夹具 {label}（带 stopPrice）→ 不得 READY',
                   ok is False and fake._ready is False,
                   f'recover={ok!r}，_ready={fake._ready}')
        finally:
            trader_260725.STATE_FILE = _SESSION_STATE


# ── B34：收编+参数有效+线程存活，但首轮业务核验未完成 → 闸门不得解除 ──────
def test_block34_takeover_alive_before_first_round_keeps_gate():
    """ChatGPT 第十二轮复审阻断①：`is_alive()` 不是放闸的充分条件。

    真实 `_start_monitoring` 进入主体后是无限循环，**首轮订单/持仓/止损核验
    尚未完成**时线程必然 alive。若以「已进入主体且还活着」作为放闸依据，
    新 ENTRY 就会在「批次事实未核实」的窗口内被放行。

    既有用例为何没钉住它：B30a 的替身**立即返回**（线程随即死亡 →
    is_alive=False → 闸门自然保持），B19/B15 的 fake 账本 `layer_sl_params`
    为空 → 参数自检不通过 → 接管根本没起。两条路各自绕开了窗口。
    本条同时满足「参数自检通过」与「线程仍在循环内」，把它钉死。
    """
    f, sig = _two_layer_fake()
    _bind_real_reconcile(f)
    f._state_lock = threading.RLock()
    f.monitor_kwargs = []
    f.monitoring_started_ids = []
    _block = threading.Event()      # 永不置位：模拟首轮核验尚未完成的监控主循环
    in_loop = []

    def _alive_stub(*a, **k):
        f.monitor_kwargs.append(k)
        f.monitoring_started_ids.extend(k.get('entry_orders') or [])
        in_loop.append('entered')
        try:
            _block.wait()
        finally:
            in_loop.append('returned')
    f._start_monitoring = _alive_stub
    f.exchange.fetch_open_orders.side_effect = None
    f.exchange.fetch_open_orders.return_value = [{
        'id': REAL_OID, 'symbol': SYMBOL, 'type': 'STOP_MARKET', 'side': 'buy',
        'price': 77000.0, 'amount': 0.43, 'status': 'open'}]
    f.exchange.create_order.side_effect = RuntimeError('创建结果未知')
    CryptoTrader.execute_signal(f, sig)
    time.sleep(0.4)

    k = f.monitor_kwargs[0] if f.monitor_kwargs else {}
    n = len(k.get('entry_orders') or [])
    # 与生产 `_tk_ok` 同构的自检（trader ~L6687-6692）：只有它为真，
    # 下面"闸门保持"才是**因为首轮未核验**，而不是因为参数无效。
    params_ok = (n >= 1
                 and len(k.get('target_amounts') or []) >= n
                 and len(k.get('stop_steps') or []) >= n
                 and bool(k.get('layer_sl_params'))
                 and bool(k.get('prepared_tp_params')))
    report('B34a 前提：收编成功 + 参数自检有效 + 线程已进入主体且仍在循环内',
           bool(f.monitor_kwargs) and in_loop == ['entered'] and params_ok,
           f'接管启动={len(f.monitor_kwargs)} 次，订单={n}，'
           f'layer_sl={len(k.get("layer_sl_params") or [])}，'
           f'tp_params={bool(k.get("prepared_tp_params"))}，'
           f'循环状态={in_loop}（须为 [entered]）')
    report('B34b 首轮业务核验未完成 → 未决闸门必须保持',
           'batch_new_001' in f._unresolved_intent_batches,
           f'unresolved={f._unresolved_intent_batches}，'
           f'degraded={f._poll_degraded_batches}')
    # 窗口期内的并发新 ENTRY 必须仍被挡住 —— 闸门被提前解除的直接资金后果。
    created = []
    f.exchange.create_order.side_effect = lambda **kk: created.append(kk) or {'id': 'Z'}
    CryptoTrader.execute_signal(f, sig)
    report('B34c 该窗口内并发新 ENTRY 必须仍被阻断',
           len(created) == 0,
           f'本轮 create 调用={len(created)}（应 0）')


# ── B35：普通轮询恢复分支不得在「持仓 UNKNOWN」时解除降级（第十四轮阻断）────
def test_block35_position_unknown_keeps_degraded_gate():
    """第十四轮复审阻断：解除 `_poll_degraded_batches` 必须**同时**知道持仓真相。

    缺陷（第十三轮走查遗漏、ChatGPT 指出并已由本用例实证）：
    普通恢复分支的解除条件只要
        `not _poll_orders_unresolved and _poll_protection_confirmed`
    **没有** `current_actual_position is not None`。于是：
      · 订单查询已恢复、账本尚无已识别成交 → `batch_filled_amount == 0`
      · `_poll_needs_protection = False` → 保护判据被「空过」判成已确认
      · 此时持仓查询返回 None（UNKNOWN ≠ EMPTY），旧实现照样打印
        「✅ 全部批次监控恢复」并把降级闸门清空
    而 S6 首轮接管判据（trader `:10752`）本就要求 `current_actual_position
    is not None` —— 普通恢复分支必须同款 Fail-Closed，不能让 S6 兜底。

    B35c 的信号时刻把持仓查询恢复成 0.0：这样 `execute_signal` 自己那道
    「持仓 UNKNOWN → 拒绝」（`:6261`）不会替本用例挡单，**闸门成为唯一拦截**。
    否则「零下单」会是假绿（异常/前置拒绝也能得到 0）。
    """
    d = tempfile.mkdtemp(prefix='pd_b35_')
    sp = os.path.join(d, 'trade_state.json')
    trader_260725.STATE_FILE = sp
    try:
        PR._seed(sp)
        fake = PR._make_fake(sp, PR._disk(sp))
        _init_poll_tracking(fake)
        # 让同一替身既能跑监控轮询、又能跑真实 execute_signal 落单。
        # 这些桩只覆盖与被测闸门无关的判据（风控/指纹/活跃数），缺了会在
        # `risk_allowed, risk_reason = ...` 解包处抛异常 → 「零下单」成假绿。
        fake._check_account_risk = lambda *a, **k: (True, 'ok')
        fake._count_active_batches = lambda *a, **k: 0
        fake._compute_signal_fingerprint = lambda *a, **k: 'fp-b35'
        fake._check_existing_conflicts = lambda *a, **k: False
        fake._validate_stop_losses = lambda *a, **k: (True, 'ok')
        fake._validate_take_profit = lambda *a, **k: (True, 'ok')
        fake._get_today_realized_pnl = lambda *a, **k: 0.0
        fake._start_monitoring = lambda *a, **k: None   # 成功路径不起真线程
        ex = fake.exchange
        ex.fetch_balance.return_value = {'USDT': {'free': 10000.0}}
        ex.fetch_ticker.return_value = {'last': 76500.0, 'close': 76500.0}
        ex.fapiPrivateGetPositionSideDual.return_value = {'dualSidePosition': True}
        ex.set_leverage.return_value = {}
        created = []
        ex.create_order.side_effect = (
            lambda **kk: created.append(kk) or {'id': 'B35', 'status': 'open'})

        # ① 连续失败 → 降级闸门置位
        ex.fetch_open_orders.side_effect = RuntimeError('持续失败')
        PR._drive(fake, None, max_rounds=3)
        report('B35a 连续失败 → 降级闸门置位',
               BATCH in fake._poll_degraded_batches,
               f'degraded={sorted(fake._poll_degraded_batches)}')

        # ② 订单查询恢复，但持仓查询返回 None（UNKNOWN ≠ 零仓）
        ex.fetch_open_orders.side_effect = None
        ex.fetch_open_orders.return_value = [{'id': ENTRY_ID, 'status': 'open'}]
        fake._get_current_position_amt = lambda *a, **k: None     # UNKNOWN
        PR._drive(fake, None, max_rounds=2)
        report('B35b 持仓 UNKNOWN → 不得解除降级闸门',
               BATCH in fake._poll_degraded_batches,
               f'degraded={sorted(fake._poll_degraded_batches)}，'
               f'streak={fake._poll_fail_streak.get(BATCH)}')

        # ③ 此时实际发起新 ENTRY：必须零下单（闸门是唯一拦截，见方法 docstring）
        fake._get_current_position_amt = lambda *a, **k: 0.0     # 信号时刻查询已恢复
        ret = CryptoTrader.execute_signal(fake, _FakeSignal())
        time.sleep(0.1)
        report('B35c 持仓未被监控核实过 → 新 ENTRY 零下单',
               ret is None and len(created) == 0,
               f'返回={ret!r}，create 调用={len(created)}，'
               f'degraded={sorted(fake._poll_degraded_batches)}')

        # ④ 阳性对照：持仓核实为零 → 同一条恢复路径确实放闸
        PR._drive(fake, None, max_rounds=2)
        report('B35d 阳性对照：持仓核实为零 → 恢复并解除闸门',
               BATCH not in fake._poll_degraded_batches,
               f'degraded={sorted(fake._poll_degraded_batches)}，'
               f'streak={fake._poll_fail_streak.get(BATCH)}')

        # ⑤ 阳性对照：放闸后同一替身确实能落单（证明 ③ 不是替身根本下不了单）
        ret2 = CryptoTrader.execute_signal(fake, _FakeSignal())
        time.sleep(0.1)
        report('B35e 阳性对照：恢复后同一 ENTRY 确实能落单（③ 非假绿）',
               len(created) >= 1,
               f'返回={ret2!r}，create 调用={len(created)}')
    finally:
        trader_260725.STATE_FILE = _SESSION_STATE


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
        test_block26_refused_takeover_still_blocks_new_entry,
        test_block27_non_stop_and_missing_pside,
        test_block28_unresolved_gate_release_path,
        test_block29_reverify_uses_post_recovery_ledger,
        test_block30_thread_exits_immediately,
        test_block31_unparsable_ledger_value,
        test_block32_info_contract_fixture,
        test_block33_release_then_exit_blocks_entry,
        test_block34_takeover_alive_before_first_round_keeps_gate,
        test_block35_position_unknown_keeps_degraded_gate,
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
