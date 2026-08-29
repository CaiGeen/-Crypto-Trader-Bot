# -*- coding: utf-8 -*-
"""
P0 事故回放测试：限价平仓竞态 → 孤儿 TP 订单（2026-08-28 实盘事故，订单 3000002162416909）

ChatGPT P0 指令（2026-08-28）：只做"事故回放测试"，暂不修改任何生产代码。
本测试完整复现真实事故生命周期：
  2 层成交 → TP/SL 在场 → /close 最优价 → close_position_limit 撤 TP+清 tp_order_id(N14)
  → pending_close=True → 限价平仓单挂出未成交 → 监控线程执行一轮风控维护
  → 限价单成交 + 持仓归零 → _monitor_limit_close 结算 → 主循环持仓归零分支清理 state。

两种竞态顺序：
  场景 A：监控维护发生在限价成交【之前】（事故原始时序）
  场景 B：限价成交/结算发生在监控维护 create_order 【执行过程中】（更窄窗口）

断言纪律（ChatGPT 七项要求；2026-08-28 Batch A 实施后翻转为 GREEN 回归锁）：
  ① CLOSE_REQUESTED（pending_close）窗口内禁止补挂 TP      —— RED 基线实证 → 现已封死（冻结层）
  ② 窗口内禁止补挂 SL                                      —— PASS（文档化）
  ③ 禁止风控更新重建保护单                                  —— RED 基线实证 → 现已封死
  ④ 平仓成交后该批次交易所残单（TP/SL/entry）最终必须为 0   —— RED 基线实证 → 现已封死
  ⑤ state/registry 最终清理                                 —— PASS
  ⑥ 不允许"state 已清除但交易所仍有该批次残单"              —— RED 基线实证 → 现已封死
  ⑦ 最危险断言：即使错误 TP 在结算前被重建，最终结算/清理
     逻辑必须收敛它，不能形成 orphan TP                      —— RED 基线实证 → 现已封死

GREEN 正向断言（Batch A 修复行为锁定，防回退）：
  G1/ 冻结触发：close 后监控轮必须打出 🧊[P0 冻结](close_phase=1)——封死机制直接证据
  G2/ N14 终态落库：结算后（清理前）IDENT_TP/IDENT_SL registry 必须为
      PROGRAMMATIC_CANCELED（close_requested_canceled / close_settled_canceled）
  G3/ close_phase 生命周期：结算后（清理前）必须为 2（CLOSE_SETTLING）
  G4/ 零 Mock 泄漏：log 中不得出现 'MagicMock'（未绑定 helper 污染自动报警——
      2026-08-28 探针实证：_find_registry_identity_by_order_id 未绑定时 N14 写到
      MagicMock key 上，真实 identity 未落终态，GREEN 断言无从验证）
  G5/（场景 B）冻结先于 create：竞态窗口内 create_call 不得发起（create_call_seen=False）

运行：.venv/Scripts/python.exe test_close_race_replay.py（ccxt 只在项目 .venv）
预期：Batch A 实施后（2026-08-28 工作树）GREEN 基线——全部 PASS，退出码 0；
任何 FAIL = 回退（RED 复活）或测试基建损坏，退出码 1。
禁止为了让本测试通过修改 trader_260725.py / bot_runner.py。

测试基建惯例（沿用项目既有 unbound-method + fake self 惯例）：
  - CryptoTrader._start_monitoring(fake, ...)：真实监控主循环，跑在独立后台线程
  - CryptoTrader.close_position_limit(fake, BATCH, price=None)：真实平仓链路
    （内部 threading.Thread(target=self._monitor_limit_close) 从 fake 取属性 →
     fake._monitor_limit_close 必须绑定为真实实现 wrapper，否则线程跑 MagicMock）
  - gated time.sleep：mock.patch('trader_260725.time.sleep') 三线程身份分流——
    主线程 sleep 直通 0；监控线程 park 在 loop-top sleep 等 monitor_gate（一轮一放行，
    parked 事件标记一轮完成）；限价平仓线程用独立 limit_gate
  - MagicMock 数值比较必炸教训：所有数值属性绑定真实值
"""
import io
import threading
import time
import traceback
from contextlib import redirect_stdout
from unittest import mock

import ccxt

import trader_260725
from trader_260725 import CryptoTrader

SYMBOL = "BTC/USDT:USDT"
BATCH = "batch_race"
REAL_SLEEP = time.sleep          # patch 前捕获，主线程轮询用
MAIN_T = threading.current_thread()

# 场景数值（贴近实盘事故）：2 层各 0.001 BTC，入场 79400/79420，TP 85000，SL 75002
ENTRY_PRICES = [79400.0, 79420.0]
AMOUNTS = [0.001, 0.001]
TP_PRICE = 85000.0
SL_PRICE = 75002.0
TICKER = {'last': 79393.0, 'bid': 79392.8, 'ask': 79402.4}

IDENT_TP = f"{BATCH}|TP|L1|LONG"   # layer = batch_filled_count-1 = 1
IDENT_SL = f"{BATCH}|SL|L1|LONG"

RESULTS = []


def report(scenario, name, passed, expect_red, detail=""):
    """expect_red=True 的断言：FAIL = RED-CONFIRMED（当前代码缺陷实证）；
    PASS = NOT-RED（竞态未复现，需检查测试基建）。
    expect_red=False 的断言：FAIL = UNEXPECTED-FAIL（测试基建问题，非目标缺陷）。"""
    RESULTS.append((scenario, name, passed, expect_red, detail))
    if passed:
        tag = "PASS" if not expect_red else "NOT-RED(竞态未复现?)"
    else:
        tag = "RED-CONFIRMED(缺陷实证)" if expect_red else "UNEXPECTED-FAIL(基建问题)"
    print(f"  [{scenario}] [{'PASS' if passed else 'FAIL'}] {name}")
    print(f"        → {tag}" + (f" | {detail}" if detail else ""))
    return passed


# =====================================================================
# FakeExchange：双 dict（open/archive）+ 完整事件时间线
# =====================================================================
class FakeExchange:
    """记录所有 create/cancel/fetch 调用的交易所仿真（ccxt 归一化订单格式）。
    events: [(seq, thread_name, label, detail)]，按发生顺序即事件时间线。
    cancel → 移入 archive（fetch_order 返回 canceled，供 F2/F3 裁决）。
    hold_create + create_gate：场景 B 在 TAKE_PROFIT_MARKET create 中途挂起。"""

    def __init__(self):
        self.orders = {}      # id -> open order
        self.archive = {}     # id -> terminal order (canceled/filled)
        self.events = []
        self._ev_seq = 0
        self._lock = threading.Lock()
        self.ticker = dict(TICKER)
        self.position_amt = sum(AMOUNTS)
        self._id_seq = 0
        self.hold_create = False
        self.create_gate = threading.Event()

    # ---------- 事件 ----------
    def _ev(self, label, detail=""):
        with self._lock:
            self._ev_seq += 1
            self.events.append((self._ev_seq, threading.current_thread().name, label, str(detail)))

    def labels(self):
        return [e[2] for e in self.events]

    # ---------- 行情/持仓 ----------
    def fetch_ticker(self, symbol, params=None):
        self._ev('fetch_ticker', f"last={self.ticker.get('last')}")
        return dict(self.ticker)

    def fetch_positions(self, symbols=None, params=None):
        self._ev('fetch_positions', f"contracts={self.position_amt}")
        return [{'symbol': SYMBOL, 'side': 'long', 'contracts': self.position_amt,
                 'positionAmt': self.position_amt, 'info': {'symbol': 'BTCUSDT'}}]

    # ---------- 订单 ----------
    def fetch_open_orders(self, symbol, params=None, **kw):
        ids = [o['id'] for o in self.orders.values()]
        self._ev('fetch_open_orders', f"ids={ids}")
        return [dict(o) for o in self.orders.values()]

    def fetch_order(self, order_id, symbol=None, params=None, **kw):
        oid = str(order_id)
        if oid in self.orders:
            self._ev('fetch_order', f"id={oid} status=open")
            return dict(self.orders[oid])
        if oid in self.archive:
            o = self.archive[oid]
            self._ev('fetch_order', f"id={oid} status={o.get('status')}")
            return dict(o)
        self._ev('fetch_order', f"id={oid} OrderNotFound")
        raise ccxt.OrderNotFound(f"Unknown order sent {oid}")

    def create_order(self, symbol=None, type=None, side=None, amount=None,
                     price=None, params=None, **kw):
        self._id_seq += 1
        oid = f"ord{self._id_seq}"
        if self.hold_create and type == 'TAKE_PROFIT_MARKET':
            # 场景 B：记录"create 已发起"（下单意图已发生），挂起等主线程闸门
            self._ev('create_call_BLOCKED', f"type={type} side={side} amount={amount} "
                                           f"stopPrice={(params or {}).get('stopPrice')}")
            self.create_gate.wait()
            self._ev('create_gate_RELEASED', f"type={type} id={oid}")
        info = {'type': str(type)}
        p = params or {}
        if p.get('stopPrice') is not None:
            info['stopPrice'] = p['stopPrice']
        if p.get('reduceOnly'):
            info['reduceOnly'] = 'true'
        if p.get('positionSide'):
            info['positionSide'] = p['positionSide']
        order = {'id': oid, 'symbol': symbol, 'status': 'open', 'type': type, 'side': side,
                 'amount': float(amount) if amount is not None else None,
                 'price': float(price) if price is not None else None,
                 'stopPrice': p.get('stopPrice'), 'params': dict(p), 'info': info,
                 'average': None, 'filled': 0.0}
        self.orders[oid] = order
        self._ev('create_order', f"id={oid} type={type} side={side} amount={amount} price={price}")
        return dict(order)

    def cancel_order(self, order_id, symbol=None, params=None, **kw):
        oid = str(order_id)
        if oid in self.orders:
            o = self.orders.pop(oid)
            o['status'] = 'canceled'
            self.archive[oid] = o
            self._ev('cancel_order', f"id={oid} type={o['info'].get('type')} → canceled")
            return dict(o)
        self._ev('cancel_order', f"id={oid} → Unknown order")
        raise ccxt.OrderNotFound(f"Unknown order sent {oid}")

    # ---------- 精度（直通） ----------
    def price_to_precision(self, symbol, value):
        return float(value)

    def amount_to_precision(self, symbol, value):
        return float(value)

    def load_time_difference(self):
        return 0

    # ---------- 测试驱动钩子 ----------
    def seed_order(self, oid, otype, side, amount, stop_price=None):
        order = {'id': oid, 'symbol': SYMBOL, 'status': 'open', 'type': otype, 'side': side,
                 'amount': float(amount), 'price': None, 'stopPrice': stop_price,
                 'params': {}, 'info': {'type': otype, 'stopPrice': stop_price, 'reduceOnly': 'true'},
                 'average': None, 'filled': 0.0}
        self.orders[oid] = order
        self._ev('seed_order', f"id={oid} type={otype} stop={stop_price}")
        return order

    def seed_filled(self, oid, otype, side, amount, avg):
        order = {'id': oid, 'symbol': SYMBOL, 'status': 'closed', 'type': otype, 'side': side,
                 'amount': float(amount), 'price': avg, 'stopPrice': None, 'params': {},
                 'info': {'type': otype}, 'average': avg, 'filled': float(amount)}
        self.archive[oid] = order
        self._ev('seed_filled', f"id={oid} type={otype} avg={avg}")

    def fill_order(self, oid, average):
        o = self.orders.pop(oid)
        o['status'] = 'closed'
        o['average'] = float(average)
        o['filled'] = float(o['amount'])
        self.archive[oid] = o
        self._ev('FILL_order', f"id={oid} avg={average} → 持仓归零")


# =====================================================================
# MemStateStore：生产磁盘语义仿真
# =====================================================================
class MemStateStore:
    """模拟 trade_state.json 的读写语义（防 Mock 盲区，见《ccxt实盘归一化与Mock盲区.md》）：
    - load_all_states() 生产从磁盘 json.load → 每次返回【全新 dict】，跨线程不共享引用；
      测试若返回同一对象，结算线程的原地改写会直接对监控线程的旧引用可见，
      掩盖"L4361 陈旧快照 → L5335 整批保存覆盖"的真实竞态（B5 现象）。
    - _persist_states() 生产为 os.replace 原子写 → 这里用锁内引用替换等价。
    - 批次层浅拷贝（嵌套对象共享）：registry 等嵌套结构全部走 _update_registry 的
      load→改→save 纪律，共享引用不改变结果。"""

    def __init__(self, initial):
        self._data = initial
        self._lock = threading.Lock()

    def load(self):
        with self._lock:
            return {s: {b: dict(d) for b, d in bs.items()} for s, bs in self._data.items()}

    def persist(self, all_states):
        with self._lock:
            self._data = dict(all_states)


# =====================================================================
# fake trader：真实 helper 绑定清单（沿项目 make_fake 惯例）
# =====================================================================
def _bind_real(fake, name):
    fn = getattr(CryptoTrader, name)
    setattr(fake, name, lambda *a, **k: fn(fake, *a, **k))


def make_fake(store, ex):
    fake = mock.MagicMock()
    fake._safe_api_call = lambda fn, *a, **k: fn(*a, **k)   # 直通（绕过鉴权/限流机器）
    fake._api_cooldown_until = 0                             # MagicMock 数值比较必炸教训
    fake.exchange = ex
    fake.sent = []
    fake.send_tg_notification = lambda text, **kw: fake.sent.append(
        (kw.get('level', 'info'), str(text)[:60]))
    # 状态持久化：生产磁盘语义（load 全新 dict / persist 原子替换），真实 save/clear 实现
    # _state_lock 用生产同款【非重入 Lock】（L153）——RLock 会静默放过"锁内再取锁"
    # 死锁违规（如 _commit_protection_with_g3 锁内调 _update_registry→save_batch_state）
    fake._state_lock = threading.Lock()
    fake.load_all_states = lambda: store.load()
    fake.save_batch_state = lambda s, b, d: CryptoTrader.save_batch_state(fake, s, b, d)
    fake.clear_batch_state = lambda s, b: CryptoTrader.clear_batch_state(fake, s, b)
    fake._persist_states = lambda all_s: store.persist(all_s)
    # registry 状态机 / 仲裁闸门 / F1-F3 裁决 / verify：全部真实实现
    for name in ('_update_registry', '_assert_create_allowed', '_adjudicate_recreate_before_repair',
                 '_build_intent', '_order_matches_intent', '_verify_and_update_registry',
                 '_verify_order_created', '_reconcile_stale_protection_layers',
                 '_prune_pending_sl_by_registry', '_classify_create_exception',
                 '_protection_identity', '_gate_alert_notify', '_gate_alert_clear',
                 '_tp_update_blocked', '_check_tp_viability', '_mark_tp_param_invalid',
                 '_clear_tp_param_invalid', '_check_protection_order_validity',
                 '_get_current_position_amt', '_cancel_remaining_entries',
                 '_cancel_limit_close_order',
                 # P0 Batch A 新 helper 六件套（未绑定 → MagicMock 吸收调用，
                 # N14 终态会写到 MagicMock key 上，GREEN 断言失效——G4 断言防此坑）
                 '_final_pre_create_check', '_commit_protection_with_g3',
                 '_g3a_converge_race_order', '_g3_cancel_race_order',
                 '_g3_log_position_recheck', '_find_registry_identity_by_order_id'):
        _bind_real(fake, name)
    # 数值/容器属性（MagicMock 数值比较必炸教训）
    fake._active_monitors = set()
    fake._active_monitors_lock = threading.Lock()
    fake._tp_breaker_alerted = {}
    fake._gate_alert_counts = {}
    fake._gate_alert_lock = threading.Lock()
    fake._sg3_alerted = set()
    fake.registry_self_heal_interval = 1e18    # 守卫不可达（R-B 自愈重查）
    fake.last_ip_check_time = time.time()
    fake.IP_CHECK_INTERVAL = 1e9               # IP 检查守卫不可达
    fake.last_time_sync = time.time()
    # no-op 绑定（守卫不可达 / 避免 Mock 假路径 / 避免文件 IO）
    fake._sync_time_if_needed = lambda: None
    fake._recheck_registry_self_heal = lambda s, b: None
    fake._calculate_monitoring_interval = lambda: 60.0
    fake._record_realized_pnl = lambda *a, **k: None   # 写 trade_stats.json，测试避免
    fake._notify_snapshot = lambda *a, **k: None
    fake._verify_failure_msg = lambda *a, **k: 'verify_failure(测试桩)'
    fake._place_prepared_orders_immediately = lambda *a, **k: None  # 本回放无新成交层
    # ⚠️ 关键绑定：close_position_limit 内 threading.Thread(target=self._monitor_limit_close)
    # 从 fake 取属性——必须绑定为真实实现 wrapper，否则线程跑 MagicMock
    fake._monitor_limit_close = lambda *a, **k: CryptoTrader._monitor_limit_close(fake, *a, **k)
    return fake


def make_initial_state(ex):
    """实盘事故前的批次状态：2 层已成交、TP/SL 在场、registry CONFIRMED。"""
    b = {
        'is_active': True, 'batch_id': BATCH, 'symbol': SYMBOL, 'side': 'BUY',
        'entry_orders': ['e1', 'e2'],
        'stop_steps': [74900.0, SL_PRICE],
        'take_profit_price': TP_PRICE,
        'current_sl_id': 'sl_orig', 'tp_order_id': 'tp_orig',
        'batch_total_amount': sum(AMOUNTS), 'target_amounts': list(AMOUNTS),
        'params_base': {}, 'is_hedge_mode': False,
        'last_filled_count': 2, 'filled_details': list(ENTRY_PRICES),
        'total_entry_fee': 0.0, 'user_modified': False, 'pending_sl_orders': [],
        'protection_registry': {
            IDENT_TP: {'state': 'CONFIRMED', 'order_id': 'tp_orig', 'id_known': True,
                       'order_kind': 'conditional', 'role': 'TP', 'layer': 1, 'side': 'LONG',
                       'intent': {'symbol': 'BTC/USDT:USDT', 'side': 'sell', 'qty': sum(AMOUNTS),
                                  'order_type': 'TAKE_PROFIT_MARKET', 'stop_price': TP_PRICE,
                                  'reduce_only': True},
                       'updated_at': time.time()},
            IDENT_SL: {'state': 'CONFIRMED', 'order_id': 'sl_orig', 'id_known': True,
                       'order_kind': 'conditional', 'role': 'SL', 'layer': 1, 'side': 'LONG',
                       'intent': {'symbol': 'BTC/USDT:USDT', 'side': 'sell', 'qty': sum(AMOUNTS),
                                  'order_type': 'STOP_MARKET', 'stop_price': SL_PRICE,
                                  'reduce_only': True},
                       'updated_at': time.time()},
        },
    }
    return {SYMBOL: {BATCH: b}}


# =====================================================================
# 三线程 gated-sleep 驱动器
# =====================================================================
class RaceHarness:
    """监控线程 park 在每轮 loop-top sleep；主线程一轮一放行。
    限价平仓线程（close_position_limit 派生）用独立 limit_gate。"""

    def __init__(self, fake):
        self.fake = fake
        self.MONITOR_T = [None]
        self.parked = threading.Event()
        self.monitor_gate = threading.Event()
        self.monitor_finished = threading.Event()
        self.limit_gate = threading.Event()
        self.limit_threads = []

    def gated_sleep(self, sec):
        t = threading.current_thread()
        if t is MAIN_T:
            return 0                      # 主线程 sleep 直通（真实等待用 REAL_SLEEP 轮询）
        if self.MONITOR_T[0] is not None and t is self.MONITOR_T[0]:
            self.parked.set()             # 一轮结束，park 等放行
            self.monitor_gate.wait()
            self.monitor_gate.clear()
            return 0
        # 限价平仓监控线程（或其他派生后台线程）
        if t not in self.limit_threads:
            self.limit_threads.append(t)
        self.limit_gate.wait()
        self.limit_gate.clear()
        return 0

    def monitor_main(self):
        """包装真实 _start_monitoring：finally 同时置 monitor_finished + parked 解阻塞。"""
        f = self.fake
        try:
            CryptoTrader._start_monitoring(
                f, SYMBOL, BATCH, ['e1', 'e2'], [74900.0, SL_PRICE], TP_PRICE,
                'sl_orig', 'tp_orig', sum(AMOUNTS), list(AMOUNTS), {}, False, 'BUY',
                last_filled_count=2, filled_details=list(ENTRY_PRICES), total_entry_fee=0.0,
                pending_sl_orders=[], prepared_tp_params=None, layer_sl_params=None)
        except Exception as e:
            print(f"❌ [回放测试] _start_monitoring 异常: {e}")
            traceback.print_exc()
        finally:
            self.monitor_finished.set()
            self.parked.set()

    def start_monitor(self):
        t = threading.Thread(target=self.monitor_main, name='race-monitor', daemon=True)
        self.MONITOR_T[0] = t
        t.start()
        if not self.parked.wait(30):
            raise TimeoutError('监控线程首次 park 超时')

    def release_round(self):
        """放行监控线程执行一轮（非阻塞；调用前线程必须处于 park 态）。"""
        self.parked.clear()
        self.monitor_gate.set()

    def wait_round_done(self, timeout=60):
        """等待一轮完成（park）或线程退出（finished）。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.monitor_finished.is_set():
                return 'finished'
            if self.parked.wait(0.2):
                return 'parked'
        raise TimeoutError('监控线程一轮超时未 park')

    def run_round(self, timeout=60):
        self.release_round()
        return self.wait_round_done(timeout)

    def wait_create_call(self, ex, timeout=3):
        """等待监控线程在 create_order 处挂起（场景 B）。
        Batch A 后冻结先于 create 拦截 → create 不会发起，3s 足够判定（G5 断言）。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if any(e[2] == 'create_call_BLOCKED' for e in ex.events):
                return True
            REAL_SLEEP(0.05)
        return False

    def wait_limit_exit(self, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if all(not t.is_alive() for t in self.limit_threads):
                return True
            REAL_SLEEP(0.05)
        return False


# =====================================================================
# 证据提取
# =====================================================================
def tp_creates(evts):
    """所有 TAKE_PROFIT_MARKET create（含挂起后完成的）——即错误补挂。"""
    ids = []
    for e in evts:
        if e[2] == 'create_order' and 'type=TAKE_PROFIT_MARKET' in e[3]:
            ids.append(e[3].split()[0].split('=')[1])
    return ids


def sl_creates(evts):
    return [e for e in evts if e[2] == 'create_order' and 'type=STOP_MARKET' in e[3]]


def residual_batch_orders(ex):
    """终态该批次交易所残单：TP/SL 条件单 + entry 挂单。"""
    res = []
    for o in ex.orders.values():
        t = (o.get('info') or {}).get('type', '')
        if t in ('TAKE_PROFIT_MARKET', 'STOP_MARKET') or o['id'] in ('e1', 'e2'):
            res.append(o)
    return res


def has_cancel_event(evts, oid):
    return any(e[2] == 'cancel_order' and f"id={oid} " in e[3] + ' ' for e in evts) or \
        any(e[2] == 'cancel_order' and e[3].startswith(f"id={oid} ") for e in evts)


def idx_of(evts, label, contains=None):
    for e in evts:
        if e[2] == label and (contains is None or contains in e[3]):
            return e[0]
    return None


# =====================================================================
# 场景驱动
# =====================================================================
def drive_scenario(mode):
    """mode='A'：监控维护在限价成交之前（事故原始时序）
       mode='B'：限价成交/结算发生在监控 create 执行过程中（更窄窗口）"""
    ex = FakeExchange()
    ex.seed_filled('e1', 'STOP_MARKET', 'buy', AMOUNTS[0], ENTRY_PRICES[0])
    ex.seed_filled('e2', 'STOP_MARKET', 'buy', AMOUNTS[1], ENTRY_PRICES[1])
    ex.seed_order('tp_orig', 'TAKE_PROFIT_MARKET', 'sell', sum(AMOUNTS), TP_PRICE)
    ex.seed_order('sl_orig', 'STOP_MARKET', 'sell', sum(AMOUNTS), SL_PRICE)
    store = MemStateStore(make_initial_state(ex))
    fake = make_fake(store, ex)
    h = RaceHarness(fake)
    if mode == 'B':
        ex.hold_create = True

    log = io.StringIO()
    close_ok = None
    round1 = None
    create_call_seen = None
    settled_flag_alive = None
    with redirect_stdout(log), mock.patch.object(trader_260725.time, 'sleep',
                                                 side_effect=h.gated_sleep):
        # 1. 监控已在运行（对应实盘：批次 2 层成交、TP/SL 在场）
        h.start_monitor()

        # 2. TG /close 最优价平仓（close_position_limit 全真实链路）
        close_ok, _msg = CryptoTrader.close_position_limit(fake, BATCH, price=None)

        # 3. 竞态窗口
        if mode == 'A':
            # 场景 A：限价成交【之前】跑一轮监控风控维护 → R14 补挂窗口
            round1 = h.run_round()
        else:
            # 场景 B：放行监控 → 在 create_order 中途挂起（结算尚未发生）
            h.release_round()
            create_call_seen = h.wait_create_call(ex)

        # 4. 限价平仓单成交 + 持仓归零（交易所侧事实）
        limit_id = next(oid for oid, o in ex.orders.items()
                        if (o.get('info') or {}).get('type') == 'LIMIT')
        ex.fill_order(limit_id, TICKER['ask'])
        ex.position_amt = 0.0

        # 5. 放行限价平仓监控线程（结算：置 settled 旗标 + 只撤 SL + N14-SL 终态）
        h.limit_gate.set()
        limit_exited = h.wait_limit_exit()

        # G2/G3 证据点：结算完成后、主循环清理【之前】抓 registry 终态 + close_phase
        # （清理后批次删除无从断言——N14 终态是本时刻的唯一可观测窗口）
        b_mid = store.load().get(SYMBOL, {}).get(BATCH) or {}
        _reg_mid = b_mid.get('protection_registry') or {}
        reg_tp_mid = dict(_reg_mid.get(IDENT_TP) or {})
        reg_sl_mid = dict(_reg_mid.get(IDENT_SL) or {})
        phase_after_settle = int(b_mid.get('close_phase', -1) or -1)

        if mode == 'A':
            # 6a. 限价成交【之后】再跑一轮监控（归零检测 → 跳过结算 → clear → break）
            round2 = h.run_round()
            _ = round2
        else:
            if create_call_seen:
                # 6b. 结算已完成后释放 create 闸门 → create 完成 → 保存 state（陈旧快照覆盖）
                ex.create_gate.set()
                h.wait_round_done()
                # B5 证据点：结算写入的 settled_by_limit_close 是否被风控保存覆盖丢失
                b = store.load().get(SYMBOL, {}).get(BATCH)
                settled_flag_alive = bool(b and b.get('settled_by_limit_close'))
                # 再跑一轮（归零检测 → 复活 state 靠 pending_close 分支清除）
                h.run_round()
            else:
                # 自适应分支：修复后 create 被抑制 → 直接驱动至线程退出
                ex.create_gate.set()
                for _ in range(5):
                    if h.monitor_finished.is_set():
                        break
                    h.run_round()

    monitor_exited = h.monitor_finished.is_set()
    return {
        'ex': ex, 'store': store, 'fake': fake, 'log': log.getvalue(),
        'close_ok': close_ok, 'mode': mode, 'monitor_exited': monitor_exited,
        'limit_exited': limit_exited, 'round1': round1,
        'create_call_seen': create_call_seen, 'settled_flag_alive': settled_flag_alive,
        'reg_tp_mid': reg_tp_mid, 'reg_sl_mid': reg_sl_mid,
        'phase_after_settle': phase_after_settle,
    }


# =====================================================================
# 断言（含 RED 预期标注）
# =====================================================================
def assert_scenario(ev, tag):
    ex, mode = ev['ex'], ev['mode']
    evts = ex.events
    states = ev['store'].load()   # 终态快照（断言时读取）

    print(f"\n{'=' * 78}\n场景 {tag}（mode={mode}）断言\n{'=' * 78}")

    # --- 0. 中性 sanity（预期 PASS；防 false GREEN：监控内部异常被 W1 except 吞掉）---
    tp_cancelled = has_cancel_event(evts, 'tp_orig')
    report(tag, '0/sanity：close 成功+撤旧TP+挂限价单+双线程正常退出',
           bool(ev['close_ok']) and tp_cancelled
           and any('type=LIMIT' in e[3] for e in evts if e[2] == 'create_order')
           and ev['monitor_exited'] and ev['limit_exited'],
           expect_red=False,
           detail=f"close={ev['close_ok']}, 撤tp_orig={tp_cancelled}, "
                  f"monitor退出={ev['monitor_exited']}, limit线程退出={ev['limit_exited']}")

    # --- 1. CLOSE_REQUESTED 窗口内禁止补挂 TP（RED 基线已实证 → GREEN 回归锁）---
    # 说明：本回放全程即事故链（close → 竞态窗口 → 成交 → 清理），
    # 窗口内任何 TAKE_PROFIT_MARKET create 都是错误补挂
    tps = tp_creates(evts)
    fill_idx = idx_of(evts, 'FILL_order')
    report(tag, '1/窗口内禁止补挂 TP（pending_close 期间无新 TAKE_PROFIT_MARKET create）',
           len(tps) == 0, expect_red=False,
           detail=f"错误补挂 TP create={len(tps)} 次, ids={tps}（fill 事件 seq={fill_idx}）"
                  f"（冻结层在 R14 之前拦截）")

    # --- 1b. 窗口内禁止补挂 SL（当前代码 PASS，文档化）---
    sls = sl_creates(evts)
    report(tag, '1b/窗口内禁止补挂 SL（无新 STOP_MARKET create）',
           len(sls) == 0, expect_red=False,
           detail=f"SL create 次数={len(sls)}")

    # --- 3. 禁止风控更新重建保护单（RED 基线已实证 → GREEN 回归锁）---
    tp_orders_total = (sum(1 for o in ex.orders.values()
                           if (o.get('info') or {}).get('type') == 'TAKE_PROFIT_MARKET')
                       + sum(1 for o in ex.archive.values()
                             if (o.get('info') or {}).get('type') == 'TAKE_PROFIT_MARKET'))
    report(tag, '3/风控维护不得重建保护单（交易所 TP 单总数不得 > 初始 1 张）',
           tp_orders_total <= 1, expect_red=False,
           detail=f"TP 单总数（open+archive）={tp_orders_total}（tp_orig 撤销后本应为 0/1 终态）")

    # --- 4 + 6. 终态：交易所该批次残单必须为 0（RED 基线孤儿 TP 已实证 → GREEN 回归锁）---
    residual = residual_batch_orders(ex)
    state_cleared = BATCH not in states.get(SYMBOL, {})
    res_str = ', '.join("{}:{}@{}".format(o['id'], o['info'].get('type'), o.get('stopPrice'))
                        for o in residual)
    viol = ' → 违反【state 已清除但交易所仍有该批次残单】禁令' if residual else ''
    report(tag, '4/平仓成交后该批次交易所残单必须归零（TP/SL/entry）',
           len(residual) == 0, expect_red=False,
           detail=f"残单=[{res_str}] | state 已清除={state_cleared}{viol}")

    # --- 5. state/registry 最终清理（预期 PASS）---
    report(tag, '5/state 与 registry 最终清理（BATCH 从 states 移除）',
           state_cleared, expect_red=False,
           detail=f"states[{SYMBOL!r}] keys={list(states.get(SYMBOL, {}).keys())}")

    # --- 5b. 结算撤 SL（中性 sanity，预期 PASS）---
    report(tag, '5b/限价平仓结算撤销 SL（_monitor_limit_close 只撤 current_sl_id）',
           has_cancel_event(evts, 'sl_orig'), expect_red=False,
           detail=f"撤 sl_orig={has_cancel_event(evts, 'sl_orig')}")

    # --- 7. 最危险断言：错误重建的 TP 必须被最终清理收敛（RED 基线已实证 → GREEN 回归锁）---
    tps = tp_creates(evts)
    converged = []
    for oid in tps:
        archived = ex.archive.get(oid)
        converged.append(bool(archived and archived.get('status') == 'canceled')
                         or has_cancel_event(evts, oid))
    report(tag, '7/孤儿 TP 收敛（错误重建的 TP 必须被结算/清理撤销，不能失联）',
           len(tps) == 0 or all(converged), expect_red=False,
           detail=f"错误重建 TP ids={tps}, 各自被撤销收敛={converged}"
                  f"（冻结层封死后 ids 应恒为空）")

    # ===== Batch A GREEN 正向断言（修复行为锁定，防回退）=====

    # --- G1/ 冻结触发（封死机制直接证据：log 中必须出现 P0 冻结 print）---
    freeze_seen = 'P0 冻结' in ev['log']
    report(tag, 'G1/冻结触发（close 后监控轮打出 🧊[P0 冻结] close_phase=1）',
           freeze_seen, expect_red=False,
           detail=f"冻结 print 出现={freeze_seen}（close_phase=1 每轮重读生效）")

    # --- G2/ N14 终态落库（结算后、清理前 registry 必须为 PROGRAMMATIC_CANCELED）---
    _tp_mid = ev.get('reg_tp_mid') or {}
    _sl_mid = ev.get('reg_sl_mid') or {}
    tp_state = _tp_mid.get('state')
    tp_reason = _tp_mid.get('terminated_reason')
    sl_state = _sl_mid.get('state')
    sl_reason = _sl_mid.get('terminated_reason')
    report(tag, 'G2/N14 TP 终态落库（IDENT_TP → PROGRAMMATIC_CANCELED/close_requested_canceled）',
           tp_state == 'PROGRAMMATIC_CANCELED' and tp_reason == 'close_requested_canceled',
           expect_red=False,
           detail=f"IDENT_TP registry state={tp_state!r}, reason={tp_reason!r}")
    report(tag, 'G2b/N14 SL 终态落库（IDENT_SL → PROGRAMMATIC_CANCELED/close_settled_canceled）',
           sl_state == 'PROGRAMMATIC_CANCELED' and sl_reason == 'close_settled_canceled',
           expect_red=False,
           detail=f"IDENT_SL registry state={sl_state!r}, reason={sl_reason!r}")

    # --- G3/ close_phase 生命周期（结算后、清理前必须为 2 = CLOSE_SETTLING）---
    report(tag, 'G3/close_phase 结算相位（结算后清理前 close_phase == 2）',
           ev.get('phase_after_settle') == 2, expect_red=False,
           detail=f"phase_after_settle={ev.get('phase_after_settle')}"
                  f"（1=请求挂单→2=结算中→清理随批次删除）")

    # --- G4/ 零 Mock 泄漏（未绑定 helper 污染自动报警）---
    mock_leak = 'MagicMock' in ev['log']
    report(tag, 'G4/零 Mock 泄漏（log 不得出现 MagicMock 字样）',
           not mock_leak, expect_red=False,
           detail=f"MagicMock 泄漏={mock_leak}（未绑定 helper → 调用被 MagicMock 吸收，"
                  f"如 N14 写到 mock key 上）")

    if mode == 'B':
        # --- G5/（场景 B）冻结先于 create：竞态窗口内 create_call 不得发起 ---
        report(tag, 'G5/场景B 冻结先于 create（create_call_seen 必须为 False）',
               ev['create_call_seen'] is False, expect_red=False,
               detail=f"create_call_seen={ev['create_call_seen']}"
                      f"（冻结层在 R14 create 之前拦截 → 最窄竞态窗口也不进入）")
        # --- B5. settled 旗标存活（RED 基线已实证陈旧快照覆盖 → 现冻结下 create 被抑制，
        #     陈旧保存窗口不再存在；以 G2b/G3 终态与相位证据代替）---
        report(tag, 'B5/结算旗标 settled_by_limit_close 不被覆盖（冻结抑制 create 后无陈旧保存窗口）',
               bool(ev['settled_flag_alive']) or ev['create_call_seen'] is False,
               expect_red=False,
               detail=f"round1 完成后旗标存活={ev['settled_flag_alive']}"
                      f"（create 被冻结抑制 → 快照覆盖通道不存在）")


# =====================================================================
# 事件时间线 + 关键路径证据
# =====================================================================
def print_timeline(ev, tag):
    ex = ev['ex']
    print(f"\n{'=' * 78}\n场景 {tag} 完整事件时间线（FakeExchange 全调用记录）\n{'=' * 78}")
    for seq, tname, label, detail in ex.events:
        print(f"  #{seq:03d} [{tname}] {label}: {detail}")

    # 关键日志行（事故特征路径证据 + Batch A 修复行为特征行）
    keys = ('[TP 补挂]', '止盈单已挂出', 'F3 收养', 'F3 裁决', '[仲裁]', '[限价平仓监控]',
            '已撤销旧止盈单', '已撤销止损单', '[限价平仓已处理]', '[程序平仓]',
            '批次状态已清理', '同步维护独立风控', '监控线程已退出', '限价平仓单已挂出',
            'P0 冻结', '[N14]', '[G3a]', '终态守卫')
    print(f"\n--- 场景 {tag} 关键路径日志（事故特征行）---")
    for line in ev['log'].splitlines():
        if any(k in line for k in keys):
            print(f"  | {line.strip()[:150]}")


# =====================================================================
# 主入口
# =====================================================================
def main():
    print("=" * 78)
    print("P0 事故回放测试：限价平仓竞态 → 孤儿 TP（Batch A 实施后 GREEN 基线）")
    print(f"被测源码：trader_260725.py（2026-08-28 Batch A 工作树，+466/-70 未 commit）")
    print("RED 基线（c147543）：9 RED-CONFIRMED / 8 PASS / 退出码 1（缺陷实证，已归档")
    print("  close_race_RED_20260828.log）；本测试锁定修复行为，任何 FAIL = 回退或基建损坏。")
    print("=" * 78)

    for tag, mode in (('A: 监控维护先于限价成交', 'A'), ('B: 限价成交先于监控维护完成', 'B')):
        print(f"\n{'#' * 78}\n# 场景 {tag}\n{'#' * 78}")
        try:
            ev = drive_scenario(mode)
            assert_scenario(ev, tag.split(':')[0])
            print_timeline(ev, tag.split(':')[0])
        except Exception as e:
            print(f"❌ 场景 {tag} 驱动异常: {e}")
            traceback.print_exc()
            RESULTS.append((tag, '驱动异常', False, False, str(e)))

    # ===== 汇总 =====
    print("\n" + "=" * 78)
    print("汇总（GREEN 基线：全部应为 PASS；FAIL = 回退（RED 复活）或测试基建损坏）")
    print("=" * 78)
    n_red = sum(1 for r in RESULTS if not r[2] and r[3])
    n_pass = sum(1 for r in RESULTS if r[2] and not r[3])
    n_unexpected = sum(1 for r in RESULTS if not r[2] and not r[3])
    n_notred = sum(1 for r in RESULTS if r[2] and r[3])
    for sc, name, passed, red, detail in RESULTS:
        status = ('PASS' if passed and not red else
                  'RED-CONFIRMED' if not passed and red else
                  'NOT-RED' if passed else 'UNEXPECTED-FAIL')
        print(f"  [{status:16s}] {sc:2s} | {name}")
    print(f"\n  PASS(预期)={n_pass}  RED-CONFIRMED(回退!)={n_red}  "
          f"NOT-RED={n_notred}  UNEXPECTED-FAIL(基建)={n_unexpected}")
    if n_red:
        print("\n  ❗ 出现 RED-CONFIRMED：修复回退（孤儿 TP 补挂通道复活），立即停止灰度并回滚审查！")
    if n_unexpected:
        print("\n  ❗ 出现 UNEXPECTED-FAIL：测试基建损坏（非缺陷），先修测试再判定。")
    # GREEN 基线：任何 FAIL（含基建失败）→ 退出码 1（区分"场景全过"与"退出码"，
    # 见 8-28 回归教训：批量回归判失败必须用退出码）
    return 1 if (n_red or n_unexpected or n_notred) else 0


if __name__ == "__main__":
    raise SystemExit(main())
