# -*- coding: utf-8 -*-
"""验收 D 正式用例：监控异常恢复（ChatGPT 第三轮裁决 · 事故修复 3）。

三组覆盖：
  A. `_monitor_resume_class` 三态分类 + 不续跑集合——只读、fail-closed、零副作用；
  B. 续跑语义（真实线程端到端）：三态各自能接续、**不写** monitor_error、
     不删代次登记、续跑通知是 warning 不是 critical、
     成交进度**单调**（账本滞后不得把内存已识别成交打回未入场）、有效 SL 不被撤；
  C. 超限四条（有限重试 3 次耗尽）：残余风险登记 + critical + **禁止自动清账**
     （闸门钉在唯一清账入口 clear_batch_state，分型/迁移照跑）+ 有效 SL 保留 +
     monitor_error 落盘（人工处置）。

与 tmp_clear_race_probe2.py（条件1 原始探针，送审证据）互为对照：本文件是可重复
回归用例，探针原样保留为原始验收输出来源。

运行：G:\\my-crypto-bot\\.venv\\Scripts\\python.exe test_monitor_resume.py
零网络、零外发（send_tg_notification 打桩入内存）、不碰生产账本。
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import time as _time
import traceback
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import trader_260725
from trader_260725 import CryptoTrader

# 🔥 事故修复 4（验收 F 的绊线）：验收 D 全程**零外发**。
#    必须在任何被测对象构造**之前**装上（覆盖构造阶段 + 后台线程）。
#    R5门禁（漏项⑦）：安装**仅在脚本模式执行** —— 原本无条件安装，pytest
#    收集（import 本模块）即泄漏到整个测试进程，实测掐断
#    test_egress_ip_check 的 127.0.0.1 本地 HTTP 客户端连接 → 5 个
#    RealHttpErrorTests 假红（`'error' != 'ok'/'rejected'`）。判据零放宽：
#    - 本文件脚本运行（`python test_monitor_resume.py`）行为逐字不变：
#      导入即装、z0 断言 `_tw.snapshot()['on'] is True` 照样成立；
#    - test_egress_tripwire 的 f6/f8/f10 各自在用例内显式 `tw.install`，
#      不依赖本行的被动安装；
#    - `_tw` 模块名保持模块级可见（z0 等用例引用）。
import egress_tripwire as _tw
if __name__ == '__main__':
    _tw.install(raise_on_hit=True)

SYM = 'BTCUSDT'
BID = 'batch_mres'

# 离线无 .bot_health/control.json → 打桩实例 id（本仓库既有约定，
# 见 test_f2f3_fill_cost.py:1425）；HEALTH_DIR 指向临时目录，不在仓库留文件。
import health_progress
health_progress.HEALTH_DIR = tempfile.mkdtemp(prefix='mres_health_')
trader_260725.current_instance_id = lambda: 'mres-offline-instance'


# ───────────────────── 夹具（沿用 tmp_clear_race_probe2 的 fake 交易所） ─────────────────────

class Ex:
    def __init__(self):
        self.last_response_headers = {}
        self.markets = {}
        self.orders = {}
        self.cancel_calls = []
        self.create_calls = []
        self.positions = []
        self.open_orders = []

    def _mk(self, oid, otype='STOP_MARKET', amount=1.0, stop=75001.0,
            status='open', filled=0.0, side='sell', **k):
        o = {'id': oid, 'status': status, 'filled': filled, 'amount': amount,
             'type': otype, 'stopPrice': stop, 'side': side,
             'average': stop, 'price': stop,
             # SG3-P1 校验走 info.positionSide（hedge 模式）
             'info': {'positionSide': 'LONG', 'symbol': SYM}}
        self.orders[oid] = o
        return o

    def fetch_order(self, oid, symbol=None, params=None, **k):
        o = self.orders.get(oid)
        if o is None:
            raise Exception('binanceusdm -2011 Unknown order')
        return dict(o)

    def cancel_order(self, oid, symbol=None, params=None, **k):
        self.cancel_calls.append(oid)
        o = self.orders.get(oid)
        if o is None or str(o.get('status') or '').lower() in ('closed', 'filled'):
            raise Exception('binanceusdm -2011 Unknown order')
        o['status'] = 'canceled'
        return {'id': oid}

    def amount_to_precision(self, symbol, amount):
        return amount

    def fetch_ticker(self, symbol=None, params=None, **k):
        # c25（外部复审第 7 轮）：/be 取价路径的可编程桩。
        return {'last': float(getattr(self, 'ticker_price', 0.0) or 0.0)}

    def price_to_precision(self, symbol, price):
        return price

    def create_order(self, symbol=None, otype=None, side=None, amount=None,
                     price=None, params=None, type=None, **k):
        if otype is None:            # 生产调用走 create_order(type=...) 关键字
            otype = type
        nid = 'N%d' % (len(self.create_calls) + 1)
        self.create_calls.append((otype, side, float(amount or 0)))
        stop = float((params or {}).get('stopPrice') or 0)
        o = self._mk(nid, otype=otype, amount=float(amount or 0), stop=stop,
                     side=side)
        self.open_orders.append(o)
        return {'id': nid}

    def fetch_positions(self, symbols=None):
        return self.positions

    def fetch_open_orders(self, symbol=None, params=None, **k):
        return [o for o in self.open_orders
                if str(o.get('status') or 'open').lower() == 'open']

    def fetch_balance(self):
        return {'USDT': {'total': 16000}}

    def set_leverage(self, *a, **k):
        return {}

    def load_time_difference(self):
        return True

    def load_markets(self, *a, **k):
        return {}

    def fetch_time(self):
        return 1234567890


def make_trader(tmp):
    tmp = str(tmp)
    trader_260725.STATE_FILE = os.path.join(tmp, 'trade_state.json')
    trader_260725.AUTH_BLOCKED_FILE = os.path.join(tmp, 'auth_blocked.json')
    trader_260725.NOTIFY_QUEUE_DIR_TRADER = os.path.join(tmp, '.notify_queue')
    trader_260725.TOMBSTONE_FILE = os.path.join(tmp, 'trade_tombstones.json')
    ex = Ex()
    with mock.patch.object(CryptoTrader, '_daily_report_loop', lambda self: None):
        with mock.patch.object(trader_260725.ccxt, 'binanceusdm') as mk:
            mk.return_value = ex
            t = CryptoTrader('k', 's')
    t._min_api_interval = 0
    t.ip_file = os.path.join(tmp, 'last_ip.txt')
    t.sent_tg = []
    t.sent_tg_levels = []

    def _send(text, **k):
        t.sent_tg.append(str(text))
        t.sent_tg_levels.append(k.get('level'))

    t.send_tg_notification = _send
    t.IP_CHECK_ENABLED = False
    t._get_public_ip = lambda: None
    t._send_email_alert = lambda *a, **k: False
    return t, ex


def _registry(bid=BID):
    return {
        '%s|ENTRY|L0|LONG' % bid: {
            'state': 'PLACED', 'order_id': 'E1',
            'intent': {'qty': 1.0, 'price': '76620.0'}},
        '%s|SL|L0|LONG' % bid: {
            'state': 'CONFIRMED', 'order_id': 'SL1',
            'intent': {'qty': 1.0, 'stop_price': '75001.0'}},
    }


def _batch(**over):
    """默认：普通活跃批次（已入场 1 层、SL1 有效、无平仓事务）。"""
    b = {
        'is_active': True, 'symbol': SYM, 'side': 'BUY',
        'is_hedge_mode': True,
        'entry_orders': ['E1'], 'target_amounts': [1.0],
        'filled_details': [76620.0], 'last_filled_count': 1,
        'total_entry_fee': 0.15, 'current_sl_id': 'SL1',
        'take_profit_price': 80000.0, 'tp_order_id': 'TP1',
        'stop_steps': [75001.0],
        'params_base': {'positionSide': 'LONG', 'leverage': 100},
        'realized_reduce_amount': 0.0, 'realized_reduce_cost': 0.0,
        'batch_total_amount': 1.0,
        'protection_registry': _registry(),
    }
    b.update(over)
    return b


def _seed(t, batch, bid=BID):
    with open(trader_260725.STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump({SYM: {bid: batch}}, f, ensure_ascii=False)


def _ledger(t, bid=BID):
    try:
        with open(trader_260725.STATE_FILE, encoding='utf-8') as f:
            return (json.load(f).get(SYM, {}) or {}).get(bid)
    except (OSError, ValueError):
        return None


def _mon_kwargs(**over):
    kw = dict(entry_orders=['E1'], stop_steps=[75001.0],
              take_profit_price=80000.0, current_sl_id='SL1',
              tp_order_id='TP1', batch_total_amount=1.0,
              target_amounts=[1.0],
              params_base={'positionSide': 'LONG', 'leverage': 100},
              is_hedge_mode=True, side='BUY',
              last_filled_count=1, filled_details=[76620.0],
              total_entry_fee=0.15)
    kw.update(over)
    return kw


def _run_monitor(t, mode='once', join_s=4.0, bid=BID, max_kill_raises=4, **mon_kw):
    """起真实监视线程 → 断言窗口内 join → kill 收尸（保证不留后台线程）。

    mode='once'  ：第 3 次小睡崩溃一次，之后放行（模拟一次抖动 → 应续跑）；
    mode='once_first'：第 1 次小睡崩溃一次，之后放行（R3/C2 对照用——线程可能
                   在第 3 次睡眠前就走完，'once' 未必崩得到）；
    mode='always'：每次都崩（→ 3 次续跑耗尽 → 走超限收尾）。
    kill 开关在断言完成后置位，让线程在有限次数续跑后必然退出，
    避免上一个用例的线程在 STATE_FILE 被重定向后污染下一个用例。
    max_kill_raises：收尸开关最多制造几次崩溃（默认 4 = 恰好推到耗尽）。
    **R3/C2 对照组用 0** —— 否则收尸本身会把「未耗尽」的对照推回耗尽态。
    """
    # 收尸/崩溃注入必须**有上限**：收尾段（converge 重试）里也有 time.sleep(2)，
    # 无上限注入会把收尾本身打断 —— 那样测的就不是「超限闸门」而是测试噪音。
    # 上限 4 次 = 恰好 3 次续跑 + 第 4 次触发超限；之后一律放行。
    st = {'n': 0, 'kill': False, 'crashed': False, 'kill_raises': 0}
    _real = _time.sleep
    buf = io.StringIO()

    def _sleep(s):
        if st['kill']:
            if st['kill_raises'] < max_kill_raises:
                st['kill_raises'] += 1
                raise RuntimeError('kill-switch（用例收尸）')
            _real(min(s, 0.02))
            return
        st['n'] += 1
        if mode == 'always' and st['n'] <= 4:
            raise RuntimeError('injected monitor crash（always）')
        if mode == 'once_first' and st['n'] == 1 and not st['crashed']:
            # R3/C2 对照组专用：**第一次**睡眠就崩一次 → 第 1/3 次续跑（未耗尽），
            # 之后一律放行，让线程照常走完收敛/清账并自然退出。
            st['crashed'] = True
            raise RuntimeError('injected monitor crash（once_first）')
        if mode == 'once' and st['n'] >= 3 and not st['crashed']:
            st['crashed'] = True      # 'once'：只崩一次，之后放行（模拟单次抖动）
            raise RuntimeError('injected monitor crash（once）')
        _real(min(s, 0.02))

    with mock.patch.object(trader_260725.time, 'sleep', _sleep):
        with contextlib.redirect_stdout(buf):
            th = threading.Thread(target=t._start_monitoring, args=(SYM, bid),
                                  kwargs=_mon_kwargs(**mon_kw), daemon=True)
            th.start()
            th.join(timeout=join_s)
            alive = th.is_alive()
            # ⚠️ 必须在**收尸之前**取账本快照：kill 会故意把线程推到「续跑耗尽」
            # 退出，那条路径本来就会写 monitor_error + 超限标志，
            # 拿收尸之后的账本去断言「续跑没写 monitor_error」必然假红。
            b_at_assert = _ledger(t, bid)
            # 代次登记同样在收尸前快照（收尾会正常移除登记）
            mon_at_assert = set(t._active_monitors)
            gen_at_assert = dict(t._active_monitor_generations)
            st['kill'] = True
            th.join(timeout=15)
            exited = not th.is_alive()
    return {'out': buf.getvalue(), 'alive': alive, 'exited': exited,
            'n_sleep': st['n'], 'b': b_at_assert, 'monitors': mon_at_assert,
            'gens': gen_at_assert}


# ───────────────────── A 组：三态分类（只读） ─────────────────────

def a1_active():
    t, ex = make_trader(tempfile.mkdtemp(prefix='mres_'))
    _seed(t, _batch())
    cls, why = t._monitor_resume_class(SYM, BID)
    assert cls == 'active', (cls, why)
    assert why == 'normal_active', why


def a2_close_in_flight():
    t, ex = make_trader(tempfile.mkdtemp(prefix='mres_'))
    _seed(t, _batch(close_phase=1, pending_close=True,
                    close_reason='limit_pending_normal',
                    limit_close_order_id='L1'))
    cls, why = t._monitor_resume_class(SYM, BID)
    assert cls == 'close_in_flight', (cls, why)
    assert why.startswith('limit_close_transaction_in_flight(phase=1)'), why


def a3_stale_pending_close():
    t, ex = make_trader(tempfile.mkdtemp(prefix='mres_'))
    _seed(t, _batch(pending_close=True))
    cls, why = t._monitor_resume_class(SYM, BID)
    assert cls == 'stale_pending_close', (cls, why)
    assert why == 'pending_close_without_limit_close_order', why


def a4_frozen_manual_review():
    t, ex = make_trader(tempfile.mkdtemp(prefix='mres_'))
    _seed(t, _batch(close_reason='limit_cancel_manual_review'))
    cls, why = t._monitor_resume_class(SYM, BID)
    assert cls == '' and 'frozen_by_existing_state' in why, (cls, why)


def a5_frozen_cost_pending():
    t, ex = make_trader(tempfile.mkdtemp(prefix='mres_'))
    _seed(t, _batch(close_reason='cost_pending_settling'))
    cls, why = t._monitor_resume_class(SYM, BID)
    assert cls == '' and 'frozen_by_existing_state' in why, (cls, why)


def a6_frozen_qty_conflict():
    t, ex = make_trader(tempfile.mkdtemp(prefix='mres_'))
    _seed(t, _batch(close_reason='qty_conflict_settling'))
    cls, why = t._monitor_resume_class(SYM, BID)
    assert cls == '' and 'frozen_by_existing_state' in why, (cls, why)


def a7_settled_finalizer_owns():
    t, ex = make_trader(tempfile.mkdtemp(prefix='mres_'))
    _seed(t, _batch(settled_by_limit_close=True, close_phase=2,
                    pending_close=True, limit_close_order_id='L1'))
    cls, why = t._monitor_resume_class(SYM, BID)
    assert cls == '' and why == 'settled_finalizer_owns', (cls, why)


def a8_missing_batch():
    t, ex = make_trader(tempfile.mkdtemp(prefix='mres_'))
    _seed(t, _batch())
    cls, why = t._monitor_resume_class(SYM, 'batch_never_existed')
    assert cls == '' and why == 'batch_missing_or_inactive', (cls, why)


def a9_inactive_batch():
    t, ex = make_trader(tempfile.mkdtemp(prefix='mres_'))
    _seed(t, _batch(is_active=False))
    cls, why = t._monitor_resume_class(SYM, BID)
    assert cls == '' and why == 'batch_missing_or_inactive', (cls, why)


def a10_unreadable_ledger_fail_closed():
    """账本读不出来 = UNKNOWN ≠ 可恢复 → 必须 fail-closed 返回 ''。"""
    t, ex = make_trader(tempfile.mkdtemp(prefix='mres_'))
    _seed(t, _batch())

    def _boom(*a, **k):
        raise RuntimeError('simulated ledger io failure')

    real = t.load_all_states
    t.load_all_states = _boom
    try:
        cls, why = t._monitor_resume_class(SYM, BID)
    finally:
        t.load_all_states = real
    assert cls == '', (cls, why)
    assert why.startswith('ledger_unreadable'), why


def a11_read_only_zero_side_effect():
    """分类必须只读：不改账本字节、不发通知、不碰交易所。"""
    t, ex = make_trader(tempfile.mkdtemp(prefix='mres_'))
    _seed(t, _batch(pending_close=True))
    with open(trader_260725.STATE_FILE, 'rb') as f:
        before = f.read()
    for bid in (BID, 'batch_never_existed'):
        t._monitor_resume_class(SYM, bid)
    with open(trader_260725.STATE_FILE, 'rb') as f:
        after = f.read()
    assert before == after, '分类改动了账本字节'
    assert t.sent_tg == [], t.sent_tg
    assert ex.cancel_calls == [] and ex.create_calls == [], \
        (ex.cancel_calls, ex.create_calls)


# ───────────────────── B 组：续跑语义（真实线程） ─────────────────────

def b1_active_resumes_without_monitor_error():
    """三态①：普通活跃 → 续跑；不写 monitor_error、代次登记保留。"""
    t, ex = make_trader(tempfile.mkdtemp(prefix='mres_'))
    _seed(t, _batch())
    ex._mk('SL1', otype='STOP_MARKET', amount=1.0, stop=75001.0, side='sell')
    ex._mk('TP1', otype='TAKE_PROFIT_MARKET', amount=1.0, stop=80000.0,
           side='sell')
    ex._mk('E1', otype='LIMIT', amount=1.0, stop=76620.0, side='buy',
           status='filled', filled=1.0)
    ex.open_orders = [ex.orders['SL1'], ex.orders['TP1']]
    ex.positions = [{'symbol': SYM, 'contracts': 1.0, 'positionAmt': 1.0,
                     'side': 'long', 'positionSide': 'LONG'}]

    r = _run_monitor(t, mode='once', join_s=4.0)
    b = r['b']                       # 收尸**前**的账本快照（见 _run_monitor 注释）
    assert '监控恢复续跑' in r['out'], r['out'][-2500:]
    assert '第 1/3 次续跑' in r['out'], r['out'][-2500:]
    assert '类别=active' in r['out'], r['out'][-2500:]
    assert r['alive'], '续跑后线程应存活:\n' + r['out'][-2500:]
    assert r['exited'], '用例收尸失败（后台线程泄漏会污染后续用例）'
    assert b and b.get('monitor_error') in (None, ''), \
        '续跑路径写了 monitor_error（必须由退出路径才允许写）: %r' % b
    assert not b.get('monitor_resume_exhausted'), b
    assert BID in r['monitors'], '代次登记被删: %r' % (r['monitors'],)
    assert r['gens'].get(BID), '代次登记丢失'
    assert '监控终止' not in r['out'], r['out'][-2500:]
    # 续跑通知必须是 warning：critical 只允许留给「监控真的死了」的终局告警
    pairs = [(x, lv) for x, lv in zip(t.sent_tg, t.sent_tg_levels)
             if '监控恢复续跑' in x]
    assert pairs, '没抓到续跑通知（应由本地队列以 warning 发出）: %r' % (
        list(zip(t.sent_tg, t.sent_tg_levels)),)
    assert all(lv == 'warning' for _, lv in pairs), \
        '续跑通知级别不对（必须 warning 不是 critical）: %r' % pairs


def b2_close_in_flight_resumes():
    """三态②：真实平仓在途 → 续跑（不写 monitor_error、线程存活）。"""
    t, ex = make_trader(tempfile.mkdtemp(prefix='mres_'))
    _seed(t, _batch(close_phase=1, pending_close=True,
                    close_reason='limit_pending_normal',
                    limit_close_order_id='L1'))
    ex._mk('SL1', otype='STOP_MARKET', amount=1.0, stop=75001.0, side='sell')
    ex._mk('L1', otype='LIMIT', amount=1.0, stop=76500.0, side='sell')
    ex._mk('E1', otype='LIMIT', amount=1.0, stop=76620.0, side='buy',
           status='filled', filled=1.0)
    ex.open_orders = [ex.orders['SL1'], ex.orders['L1']]
    ex.positions = [{'symbol': SYM, 'contracts': 1.0, 'positionAmt': 1.0,
                     'side': 'long', 'positionSide': 'LONG'}]

    r = _run_monitor(t, mode='once', join_s=4.0,
                     current_sl_id='SL1', tp_order_id=None)
    b = r['b']
    assert '监控恢复续跑' in r['out'], r['out'][-2500:]
    assert '类别=close_in_flight' in r['out'], r['out'][-2500:]
    assert r['alive'], '续跑后线程应存活:\n' + r['out'][-2500:]
    assert r['exited'], '用例收尸失败（后台线程泄漏）'
    assert b and b.get('monitor_error') in (None, ''), \
        '续跑路径写了 monitor_error: %r' % b
    assert not b.get('monitor_resume_exhausted'), b
    assert BID in r['monitors'], r['monitors']


def b3_stale_pending_close_fill_lagging():
    """三态③ + 成交进度单调：账本滞后（last_filled_count=0）但交易所已成交。

    续跑同步**绝不允许**用落后账本把内存已识别成交打回未入场——那会让监控
    走「本批次未建仓 → 撤单退出」，成交从此没人管（probe2 首版回归即此）。
    三条原始验收：成交入账 / 有效 SL 保留 / 监控由明确路径接续。
    """
    t, ex = make_trader(tempfile.mkdtemp(prefix='mres_'))
    _seed(t, _batch(pending_close=True, filled_details=[0.0],
                    last_filled_count=0))
    ex._mk('SL1', otype='STOP_MARKET', amount=1.0, stop=75001.0, side='sell')
    ex._mk('E1', otype='LIMIT', amount=1.0, stop=76620.0, side='buy',
           status='filled', filled=1.0)
    ex.open_orders = [ex.orders['SL1']]
    ex.positions = [{'symbol': SYM, 'contracts': 1.0, 'positionAmt': 1.0,
                     'side': 'long', 'positionSide': 'LONG'}]

    r = _run_monitor(t, mode='once', join_s=4.0,
                     last_filled_count=0, filled_details=[0.0])
    b = r['b']                       # 收尸前快照（收尾会写 monitor_error + 超限标志）
    sl = [o for o in ex.fetch_open_orders(SYM)
          if str(o.get('type') or '').upper().startswith('STOP')]
    c1 = bool(b) and int(b.get('last_filled_count') or 0) > 0
    c2 = bool(sl) and bool(b and b.get('current_sl_id')) and not ex.cancel_calls
    c3 = r['alive'] and bool(b) and b.get('monitor_error') in (None, '') \
        and not b.get('monitor_resume_exhausted')
    assert c1, '成交未入账: last_filled_count=%r' % (b or {}).get('last_filled_count')
    assert c2, '有效 SL 被撤/缺失: sl=%r cancel=%r' % ([o.get('id') for o in sl],
                                                      ex.cancel_calls)
    assert c3, '监控未接续: alive=%s err=%r\n%s' % (
        r['alive'], (b or {}).get('monitor_error'), r['out'][-1500:])
    assert '本批次未建仓' not in r['out'], '续跑后仍按零成交退出:\n' + r['out'][-1500:]


# ───────────────────── C 组：超限四条 ─────────────────────

def _exhaust(t, r, bid=BID):
    """断言 3 次续跑用尽 + 残余风险登记 + critical 告警。"""
    for i in (1, 2, 3):
        assert ('第 %d/3 次续跑' % i) in r['out'], \
            '续跑次数不足 %d:\n%s' % (i, r['out'][-2500:])
    assert 'retry_exhausted' in r['out'] or '恢复超限' in r['out'], \
        r['out'][-2500:]
    b = _ledger(t, bid)
    assert b, '批次从账本消失（应被保留交人工处置）'
    assert b.get('monitor_resume_exhausted') is True, b
    res = b.get('monitor_resume_residual') or {}
    assert res.get('policy') == ['keep_effective_sl', 'manual_disposition',
                                 'no_auto_clear',
                                 'residual_risk_registered'], res
    assert r['exited'], '超限后线程应退出'
    alert = [x for x in t.sent_tg if '监控续跑已用尽' in x]
    assert alert, t.sent_tg
    return b


def _program_cancel_batch(**over):
    """c1/c2 夹具：程序撤单 + pending_close（decision='allow'，收尾本该 converge→clear）。

    口径必须是**干净的已成交已平仓**：fix2 的 G1 账实不符门会拦「有 fee/成交迹象
    但 last_filled_count=0」的批次（ledger_fill_mismatch），那会让 c2 对照组
    无论有没有超限闸门都 clear 不掉 —— 测出来的就不是闸门了。
    """
    b = _batch(pending_close=True, is_programmatic_cancel=True,
               close_reason='', close_phase=0, last_filled_count=1,
               filled_details=[76620.0], total_entry_fee=0.15,
               current_sl_id=None, tp_order_id=None,
               protection_registry={
                   '%s|ENTRY|L0|LONG' % BID: {
                       'state': 'FILLED', 'order_id': 'E1',
                       'intent': {'qty': 1.0, 'price': '76620.0'}},
               })
    b.update(over)
    return b


def c1_exhaust_keeps_state_and_blocks_auto_clear():
    """超限四条之③：禁止自动清账——闸门钉在唯一清账入口。

    批次是普通「程序撤单 + pending_close」形态（decision='allow'，收尾本应
    converge→clear），耗尽后必须保留账本；对照组 c2 用同样崩溃但不登记超限，
    同一路径必须能正常 clear —— 二者唯一差别就是超限标志。
    """
    t, ex = make_trader(tempfile.mkdtemp(prefix='mres_'))
    _seed(t, _program_cancel_batch())
    ex._mk('E1', otype='LIMIT', amount=1.0, stop=76620.0, side='buy',
           status='filled', filled=1.0)
    ex.positions = []

    r = _run_monitor(t, mode='always', join_s=4.0,
                     last_filled_count=1, filled_details=[76620.0],
                     current_sl_id=None, tp_order_id=None)
    b = _exhaust(t, r)
    assert '⛔ [超限] 跳过清账' in r['out'], \
        '收尾没有拦下自动清账:\n' + r['out'][-2500:]
    # 关键：converge 必须**已经收敛出 proof**——否则"没 clear"可能只是因为
    # 收敛本身失败，测的就不是超限闸门了（对照见 c2 同夹具能 clear）。
    assert 'proof=已收敛' in r['out'], \
        'converge 未产出 proof（本用例就没走到闸门）:\n' + r['out'][-2500:]
    assert b.get('is_active') is True, b


def c2_control_unexhausted_retries_still_clears():
    """对照组（ChatGPT §6.1 C2 裁决）：**未耗尽重试**时同一路径必须照旧清账。

    旧对照是「同样崩溃 + 把 ④登记函数打成 lambda」—— 那等于**手工关掉闸门的
    来源**，证不了「闸门之外的清账路径本身没被改坏」。按裁决改成真正未耗尽的
    对照：只注入 1 次崩溃 → `第 1/3 次续跑`（重试计数从未触顶）→ 收尾/循环内
    正常收敛并清账。`max_kill_raises=0` 保证收尸开关不再额外制造崩溃，否则
    收尸本身会把对照推回耗尽态。
    """
    t, ex = make_trader(tempfile.mkdtemp(prefix='mres_'))
    _seed(t, _program_cancel_batch())
    ex._mk('E1', otype='LIMIT', amount=1.0, stop=76620.0, side='buy',
           status='filled', filled=1.0)
    ex.positions = []

    r = _run_monitor(t, mode='once_first', join_s=6.0, max_kill_raises=0,
                     last_filled_count=1, filled_details=[76620.0],
                     current_sl_id=None, tp_order_id=None)
    out, b = r['out'], _ledger(t)
    assert '第 1/3 次续跑' in out, \
        '对照组应真的崩过并续跑过:\n' + out[-2500:]
    assert '第 2/3 次续跑' not in out and '第 3/3 次续跑' not in out, \
        '对照组必须是**未耗尽**重试（不得触顶）:\n' + out[-2500:]
    assert 'retry_exhausted' not in out and '恢复超限' not in out, \
        '未耗尽却走了超限处置:\n' + out[-2500:]
    assert '⛔ [超限] 跳过清账' not in out, \
        '未耗尽却拦了清账:\n' + out[-2500:]
    assert b is None, \
        '对照组应当正常清账（批次仍在账本）: %r\n——— 输出 ———\n%s' % (
            b, out[-3000:])
    assert r['exited'], '对照组线程应自然退出:\n' + out[-2000:]


def c6_exhaust_but_ledger_write_fails_still_blocks_clear():
    """C2 负向用例（ChatGPT §6.1 裁决）：**已耗尽 + 登记写盘失败 → 必须保留账本**。

    R3：超限发生时**先在当前代次内**锁定禁止清账，再尝试落盘；**落盘失败不得
    恢复普通清理授权**。
    夹具只让「带超限标志的载荷」写盘失败（其余写盘照常），所以本用例与 c1 的
    唯一差别就是**那次登记没落盘**：
      · 修前：账本里没有标志 → 收尾闸门打开 → 批次被删（违反裁决）；
      · 修后：当前代次内存锁兜住 → `⛔ [超限] 跳过清账` → 账本保留。
    """
    t, ex = make_trader(tempfile.mkdtemp(prefix='mres_'))
    _seed(t, _program_cancel_batch())
    ex._mk('E1', otype='LIMIT', amount=1.0, stop=76620.0, side='buy',
           status='filled', filled=1.0)
    ex.positions = []
    _real_persist = t._persist_states
    _fail = {'n': 0}

    def _persist_fail_on_exhausted(states, **_k):
        for _sb in (states or {}).values():
            for _bb in (_sb or {}).values():
                if isinstance(_bb, dict) and _bb.get('monitor_resume_exhausted'):
                    _fail['n'] += 1
                    return False
        return _real_persist(states, **_k)

    t._persist_states = _persist_fail_on_exhausted   # 只掐「超限登记」那一次写盘

    r = _run_monitor(t, mode='always', join_s=4.0,
                     last_filled_count=1, filled_details=[76620.0],
                     current_sl_id=None, tp_order_id=None)
    out, b = r['out'], _ledger(t)
    assert _fail['n'] >= 1, '夹具未命中登记写盘失败（场景没构造出来）'
    assert 'retry_exhausted' in out or '第 3/3 次续跑' in out, \
        '本用例必须是**已耗尽**:\n' + out[-2500:]
    assert '残余风险落盘被拒' in out, \
        '登记写盘失败未被报告:\n' + out[-2500:]
    assert '当前代次内已锁定禁止清账' in out, \
        'R3：落盘失败时应报告已在当前代次内锁定禁止清账:\n' + out[-2500:]
    assert '⛔ [超限] 跳过清账' in out, \
        '落盘失败 → 普通清理授权被恢复（违反 R3）:\n' + out[-3000:]
    assert b is not None, \
        '已耗尽 + 登记写盘失败 → 必须保留账本（被删了）: %r\n%s' % (b, out[-3000:])
    assert b.get('monitor_resume_exhausted') is not True, \
        '登记确实没落盘（否则就不是本用例了）: %r' % (b,)
    assert r['exited'], out[-2000:]
    alert = [x for x in t.sent_tg if '监控续跑已用尽' in x]
    assert alert, t.sent_tg
    assert '登记未落盘' in alert[0], \
        '落盘失败时四条里的 critical 也必须如实报告「登记未落盘」:\n' + alert[0]


def c7_direct_clear_blocked_by_inprocess_latch():
    """R3b（ChatGPT 复审 2026-10-08 漏项⑤）：**同进程绕过已复现** —— 超限登记
    落盘失败（c6 场景）后：内存锁在位、持久化标志缺位；此时另一调用方 proof
    到手后**直调唯一清账入口** `clear_batch_state` 也必须拒绝删账：
      · 内存锁（当前代次）与持久化标志两源任一命中即拒；
      · 只阻止删账（converge/proof 校验照跑，不加 sidecar、不动保护维护）；
      · 诚实边界：内存锁不跨进程/重启 —— 跨重启由持久化标志兜底。
    对照：锁回收（人工处置完成）后同一 proof 正常清账 —— 闸门不误伤。
    """
    t, ex = make_trader(tempfile.mkdtemp(prefix='mres_'))
    _seed(t, _program_cancel_batch())
    ex._mk('E1', otype='LIMIT', amount=1.0, stop=76620.0, side='buy',
           status='filled', filled=1.0)
    ex.positions = []
    proof = t._converge_batch_orders_before_clear(SYM, BID)
    assert isinstance(proof, dict), '前置失败：proof 未产出 %r' % (proof,)
    # 前置：账本在册且两个持久化标志均缺位（复现 c6 登记写盘失败后的磁盘态）
    _b = _ledger(t)
    assert _b and not _b.get('monitor_resume_exhausted') \
        and not _b.get('monitor_resume_residual'), _b
    # 只置内存锁（当前代次超限锁定，持久化标志缺位 —— 绕过复现态）
    t._no_clear_latch_set(SYM, BID, '登记写盘失败 → 代次内锁定')
    assert t._no_clear_latch_get(SYM, BID), '前置失败：内存锁未置位'
    ret = t.clear_batch_state(SYM, BID, proof=proof)
    assert ret is False, 'R3b 违规：内存锁在位仍允许删账 ret=%r' % (ret,)
    assert _ledger(t) is not None, 'R3b 违规：账本被删'
    assert any('内存锁在位' in m for m in t.sent_tg), \
        'R3b 拒绝未留档告警: %r' % (t.sent_tg[-3:],)
    # 对照：锁回收后同一 proof 允许删账 —— 闸门不误伤正常清理
    t._no_clear_latch_drop(SYM, BID)
    ret2 = t.clear_batch_state(SYM, BID, proof=proof)
    assert ret2 is True, '对照失败：锁回收后应正常清账 ret=%r msgs=%r' % (
        ret2, t.sent_tg[-3:])
    assert _ledger(t) is None, '对照失败：锁回收后账本应已删除'


def c8_hot_collect_books_bound_entry_fill():
    """R1c 监控侧（ChatGPT 复审 2026-10-08 漏项②）：收敛把 intent 匹配确认的
    入场单**订单与层绑定**进账本后，运行中的监控线程必须 **append-only 热
    收编** —— 否则骨架代次快照 entry_orders=[] 时，撤单期间的成交在本代次
    内永不入账（成交层数恒 0、保护管理不接续）。

    时序：monitor 启动快照 entry_orders=[]（绑定前）→ 收敛侧已把 E9 绑进
    账本（registry order_id + entry_orders）→ 本代次第一轮热收编
    entry_orders=['E9'] → fill 循环入账 E9 成交。
    收编守卫：前缀一致 + 各并行数组长度对齐才扩展；不成立只跳过本轮，绝不
    中断监控、绝不改已有层状态。

    **原场景输入保持不变**（ChatGPT 复审打回② 2026-10-08）：启动快照是
    `entry_orders=[]、target_amounts=[1.0]`——「订单尚未绑定、计划数量已
    存在」的骨架代次。此前用例曾把 target_amounts 改成空列表绕开长度守卫，
    属漏测；现按原输入验收：热收编、成交入账、保护维护、同代次监控继续。
    """
    t, ex = make_trader(tempfile.mkdtemp(prefix='mres_'))
    _seed(t, _batch(entry_orders=['E9'], target_amounts=[1.0],
                    filled_details=[0.0], last_filled_count=0,
                    total_entry_fee=0.0,
                    protection_registry={
                        '%s|ENTRY|L0|LONG' % BID: {
                            'state': 'PROGRAMMATIC_CANCELED', 'order_id': 'E9',
                            'id_known': True, 'role': 'ENTRY', 'layer': 0,
                            'intent': {'qty': 1.0, 'price': '76620.0'}},
                        '%s|SL|L0|LONG' % BID: {
                            'state': 'CONFIRMED', 'order_id': 'SL1',
                            'id_known': True, 'role': 'SL', 'layer': 0,
                            'intent': {'qty': 1.0, 'stop_price': '75001.0'}},
                    }))
    ex._mk('SL1', otype='STOP_MARKET', amount=1.0, stop=75001.0, side='sell')
    ex._mk('TP1', otype='TAKE_PROFIT_MARKET', amount=1.0, stop=80000.0,
           side='sell')
    ex._mk('E9', otype='LIMIT', amount=1.0, stop=76620.0, side='buy',
           status='filled', filled=1.0)
    ex.open_orders = [ex.orders['SL1'], ex.orders['TP1']]
    ex.positions = [{'symbol': SYM, 'contracts': 1.0, 'positionAmt': 1.0,
                     'side': 'long', 'positionSide': 'LONG'}]

    r = _run_monitor(t, mode='once', join_s=4.0,
                     entry_orders=[],                # 绑定前的过期快照
                     target_amounts=[1.0],           # 原场景：计划已存在（复审打回②还原）
                     last_filled_count=0,
                     filled_details=[0.0])
    out, b = r['out'], r['b']
    assert '[R1c] 批次 %s 监控热收编' % BID in out, \
        '热收编未发生（过期快照未收编账本绑定单）:\n' + out[-2500:]
    assert int((b or {}).get('last_filled_count') or 0) > 0, \
        '绑定单成交未入账: last_filled_count=%r\n%s' % (
            (b or {}).get('last_filled_count'), out[-2000:])
    assert [str(x) for x in ((b or {}).get('entry_orders') or [])] == ['E9'], \
        '账本绑定链被改动: %r' % ((b or {}).get('entry_orders'),)
    sl = [o for o in ex.fetch_open_orders(SYM)
          if str(o.get('type') or '').upper().startswith('STOP')]
    assert sl, '有效 SL 被撤/缺失: cancel=%r' % (ex.cancel_calls,)
    assert r['alive'] and b and b.get('monitor_error') in (None, '') \
        and not b.get('monitor_resume_exhausted'), \
        '监控未接续: err=%r\n%s' % ((b or {}).get('monitor_error'), out[-1500:])
    assert '本批次未建仓' not in out, '热收编后仍按零成交退出:\n' + out[-1500:]
    assert r['exited'], '用例收尸失败（后台线程泄漏会污染后续用例）'


def c3_exhaust_keeps_effective_sl_and_position():
    """超限四条之①②④：有持仓 + 有效 SL 的批次耗尽后——SL 不被撤、账本保留、
    monitor_error 落盘（转人工处置）。"""
    t, ex = make_trader(tempfile.mkdtemp(prefix='mres_'))
    _seed(t, _batch(pending_close=True, last_filled_count=1,
                    filled_details=[76620.0]))
    ex._mk('SL1', otype='STOP_MARKET', amount=1.0, stop=75001.0, side='sell')
    ex._mk('E1', otype='LIMIT', amount=1.0, stop=76620.0, side='buy',
           status='filled', filled=1.0)
    ex.open_orders = [ex.orders['SL1']]
    ex.positions = [{'symbol': SYM, 'contracts': 1.0, 'positionAmt': 1.0,
                     'side': 'long', 'positionSide': 'LONG'}]

    r = _run_monitor(t, mode='once', join_s=4.0)
    b = _exhaust(t, r)
    sl = [o for o in ex.fetch_open_orders(SYM)
          if str(o.get('type') or '').upper().startswith('STOP')]
    assert sl, '有效 SL 被撤了（超限四条之①）: %r' % (ex.cancel_calls,)
    assert not ex.cancel_calls, '超限收尾不得撤单: %r' % ex.cancel_calls
    assert b.get('monitor_error'), 'monitor_error 未写（人工处置缺登记）: %r' % b
    assert int(b.get('last_filled_count') or 0) > 0, b


def c4_classify_still_runs_when_exhausted():
    """超限四条之②③ 并存：分型（R26 语义）必须照跑——闸门只拦清账不拦分型。

    早期版本把闸门放在 _finally_cleanup_decision 顶部，会连 'classify' 一起吞掉，
    导致 test_p5_closecancel r26/r27 双红；此用例把该回归钉死。
    """
    t, ex = make_trader(tempfile.mkdtemp(prefix='mres_'))
    _seed(t, _batch(close_phase=1, pending_close=True,
                    close_reason='limit_pending_normal',
                    limit_close_order_id='L1', last_filled_count=1,
                    filled_details=[76620.0],
                    current_sl_id='S1', tp_order_id='T1',
                    protection_registry={
                        '%s|SL|L0|LONG' % BID: {
                            'state': 'CONFIRMED', 'order_id': 'S1',
                            'intent': {'qty': 1.0, 'stop_price': '75001.0'}},
                        '%s|TP|L0|LONG' % BID: {
                            'state': 'PROGRAMMATIC_CANCELED', 'order_id': 'T1',
                            'terminated_reason': 'close_requested_canceled',
                            'intent': {'qty': 1.0, 'stop_price': '80000.0'}},
                    }))
    ex._mk('S1', otype='STOP_MARKET', amount=1.0, stop=75001.0, side='sell')
    ex._mk('T1', otype='TAKE_PROFIT_MARKET', amount=1.0, stop=80000.0,
           side='sell', status='canceled', _gone=True)
    ex._mk('L1', otype='LIMIT', amount=1.0, stop=76500.0, status='canceled',
           filled=0.0, side='sell')
    ex.positions = []
    t._close_amount_guard = lambda s, sd, ih, nq, bid: (None, 'stub：fail-closed')

    r = _run_monitor(t, mode='always', join_s=4.0, current_sl_id='S1',
                     tp_order_id='T1')
    b = _exhaust(t, r)
    assert b.get('close_reason') == 'limit_cancel_manual_review', \
        '超限后统一分型没跑: close_reason=%r\n%s' % (
            b.get('close_reason'), r['out'][-2500:])
    assert '跳过清账' in r['out'] or b.get('is_active') is True, r['out'][-1500:]


# ───────────────────── 结构性断言（无法动态驱动的部分） ─────────────────────

def c5_retry_gates_present():
    """次数闸动态覆盖见 c1~c4（恰好 3 次续跑）；间隔闸无法离线加速 600s，
    这里做源码级在场断言，避免有人把间隔闸删掉而用例仍绿。"""
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'trader_260725.py'), encoding='utf-8').read()
    assert '_MAX_MONITOR_RESUME_ATTEMPTS = 3' in src, '次数闸被删'
    assert '_RESUME_RESET_SECONDS = 600' in src, '间隔闸常量被删'
    assert 'retry_exhausted' in src, '超限分支被删'
    i_reset = src.find('_resume_attempts = 0')
    i_ts = src.find('_last_resume_ts')
    i_gate = src.find(
        'if (time.time() - _last_resume_ts) > _RESUME_RESET_SECONDS:')
    assert i_reset > 0 and i_ts > 0, '续跑计数初始化被删'
    assert i_gate > 0, '间隔闸判定被删'
    assert '_resume_attempts = 0' in src[i_gate:i_gate + 400], \
        '间隔闸不再重置计数（长跑期偶发抖动会被 3 次判死）'


def z0_zero_egress_overall():
    """验收 D 全程零外发（修复 4 双轨的 B 轨断言）。

    绊线在模块导入时就装上 → 覆盖全部 23 条用例的**构造阶段**与**后台线程**；
    A 轨（EgressBlocked）会在发生点直接炸掉用例，B 轨在这里兜底：
    即使某次外发被被测代码的 except 吞掉，计数也赖不掉。
    """
    _tw.assert_zero('验收 D（23 条用例 + 构造阶段 + 监视线程）')
    assert _tw.snapshot()['on'] is True, '跑测期间绊线被关掉了'


# ───────────── R4：单一外层 try/finally（替换收尾块与 _reraise 桥接） ─────────────

def c9_persist_guard_never_writes_misaligned_arrays():
    """R5 复审（第 2 轮）缺陷修复：落盘守卫用账本链覆盖 entry_orders 时，必须整组替换
    并行数组（filled_details / target_amounts）并保证与之对齐；凑不齐则返回 None，
    调用方本轮跳过落盘（fail-closed）。否则会落盘 entry_orders=['E9'] 配 filled_details=[]，
    与 R1c 守卫要防的坐标错位同构。

    单元级判据（直接调用落盘守卫）：账本侧不对齐 → None；账本对齐 → 整组以账本为准；
    快照与账本一致（非替换路径）→ 原样返回，正常路径行为不变。
    注：真实监控路径下该分支的可达性未在本文件内复现（本用例不驱动落盘分支）。"""
    t, ex = make_trader(tempfile.mkdtemp(prefix='mres_'))
    g = t._persist_guard_arrays([], [], [], {
        'entry_orders': ['E9'], 'filled_details': [0.0], 'target_amounts': []})
    assert g is None, '账本侧不对齐（target_amounts 缺）仍放行落盘: %r' % (g,)
    g = t._persist_guard_arrays([], [], [], {
        'entry_orders': ['E9'], 'filled_details': [0.0], 'target_amounts': [1.0]})
    assert g is not None and g['entry_orders'] == ['E9'] \
        and len(g['filled_details']) == 1 and len(g['target_amounts']) == 1, \
        '账本对齐时未整组以账本为准: %r' % (g,)
    g = t._persist_guard_arrays(['E9'], [0.0], [1.0], {
        'entry_orders': ['E9'], 'filled_details': [0.0], 'target_amounts': [1.0]})
    assert g == {'entry_orders': ['E9'], 'filled_details': [0.0],
                 'target_amounts': [1.0]}, '非替换路径被改写: %r' % (g,)
    g = t._persist_guard_arrays(['X7'], [0.0], [1.0], {
        'entry_orders': ['E9', 'E10'], 'filled_details': [0.0, 0.0],
        'target_amounts': [1.0, 1.0]})
    assert g is None or g['entry_orders'] == ['X7'], \
        '快照与账本分歧（非真前缀）却改写绑定链: %r' % (g,)


def c10_guard_binding_race_no_loss():
    """复审 P1-1（第 3 轮）：守卫检查后、实际落盘前的并发绑定不得丢失，
    三数组保持对应。真实路径：真实 _start_monitoring + 真实 _persist_guard_arrays +
    真实 save_batch_state + 真实 _bind_converged_entry_order（隔离交易所替身）。

    注入点 = `_persist_guard_arrays` **返回后**（即"检查后、保存前"），用真实绑定方法
    把 E9 绑进账本（链 [E1]→[E1,E9]、ta 两项、fd 补齐两项）。断言：保存后绑定仍在、
    entry_orders/target_amounts/filled_details 三数组等长。"""
    t, ex = make_trader(tempfile.mkdtemp(prefix='mres_'))
    _seed(t, _batch(entry_orders=['E1'], target_amounts=[1.0],
                    filled_details=[0.0], last_filled_count=0,
                    total_entry_fee=0.0,
                    protection_registry={
                        '%s|ENTRY|L0|LONG' % BID: {
                            'state': 'CONFIRMED', 'order_id': 'E1',
                            'id_known': True, 'role': 'ENTRY', 'layer': 0,
                            'intent': {'qty': 1.0, 'price': '76620.0'}},
                        '%s|ENTRY|L1|LONG' % BID: {
                            'state': 'CONFIRMED', 'order_id': 'E9',
                            'id_known': True, 'role': 'ENTRY', 'layer': 1,
                            'intent': {'qty': 1.0, 'price': '76630.0'}},
                        '%s|SL|L0|LONG' % BID: {
                            'state': 'CONFIRMED', 'order_id': 'SL1',
                            'id_known': True, 'role': 'SL', 'layer': 0,
                            'intent': {'qty': 1.0, 'stop_price': '75001.0'}},
                    }))
    ex._mk('SL1', otype='STOP_MARKET', amount=1.0, stop=75001.0, side='sell')
    ex._mk('TP1', otype='TAKE_PROFIT_MARKET', amount=1.0, stop=80000.0,
           side='sell')
    ex._mk('E1', otype='LIMIT', amount=1.0, stop=76620.0, side='buy',
           status='filled', filled=1.0)
    # E9 进替身订单簿：否则收编后每轮补挂前状态查询都走 -2011×retries 重试，
    # 周期变慢会把收尸窗口挤到边界（实测满载时 15s 不够）。open 不入成交断言。
    ex._mk('E9', otype='LIMIT', amount=1.0, stop=76630.0, side='buy',
           status='open', filled=0.0)
    ex.open_orders = [ex.orders['SL1'], ex.orders['TP1'], ex.orders['E9']]
    ex.positions = [{'symbol': SYM, 'contracts': 1.0, 'positionAmt': 1.0,
                     'side': 'long', 'positionSide': 'LONG'}]

    # 注入：守卫检查（对绑定前账本）完成后、真实落盘前，并发绑定 E9
    _real_guard = t._persist_guard_arrays
    _st = {'injected': 0}

    def _hooked_guard(snap_eo, snap_fd, snap_ta, ledger_b):
        res = _real_guard(snap_eo, snap_fd, snap_ta, ledger_b)   # 检查（绑定前）
        # 避开持锁期注入（第5轮守卫默认化后锁内也调守卫；同 c13 注）
        if _st['injected'] == 0 and not t._state_lock.locked():
            _st['injected'] += 1
            ok = t._bind_converged_entry_order(
                SYM, BID, 'E9', '%s|ENTRY|L1|LONG' % BID)
            assert ok, '并发绑定注入本身失败（前置不成立）'
        return res

    t._persist_guard_arrays = _hooked_guard
    r = _run_monitor(t, mode='once', join_s=4.0,
                     entry_orders=['E1'], target_amounts=[1.0],
                     last_filled_count=0, filled_details=[0.0],
                     total_entry_fee=0.0)
    out, b = r['out'], r['b']
    assert _st['injected'] >= 1, '守卫检查后窗口未发生（注入未触发）'
    _eo = [str(x) for x in ((b or {}).get('entry_orders') or [])]
    _ta = list((b or {}).get('target_amounts') or [])
    _fd = list((b or {}).get('filled_details') or [])
    assert _eo == ['E1', 'E9'], \
        '守卫与落盘之间的并发绑定丢失: %r\n%s' % (_eo, out[-1500:])
    assert len(_ta) == len(_eo) and len(_fd) == len(_eo), \
        '三数组不对应: eo=%r ta=%r fd=%r\n%s' % (_eo, _ta, _fd, out[-1500:])
    assert r['exited'], '用例收尸失败（后台线程泄漏会污染后续用例）'


def c11_adoption_keeps_recorded_fee_once():
    """复审 P1-2（第 3 轮）：
    (a) 账本已记账的成交层（fee 38.31、lfc=1）被旧快照收编后，手续费**不得重复
        累计**（历史实测 38.31→76.62）、层数不变、成交价不丢；
    (b) 真正的新成交只累计一次（= 单层 taker 费）。
    补证与重启接续的不重复计费由既有套件回归覆盖（cost_pending/恢复段用例）。"""
    fee_one = 76620.0 * 1.0 * trader_260725.TAKER_FEE_RATE
    # (a) 已记账成交层 + 空订单快照收编
    t, ex = make_trader(tempfile.mkdtemp(prefix='mres_'))
    _seed(t, _batch(entry_orders=['E9'], target_amounts=[1.0],
                    filled_details=[76620.0], last_filled_count=1,
                    total_entry_fee=fee_one,
                    protection_registry={
                        '%s|ENTRY|L0|LONG' % BID: {
                            'state': 'PROGRAMMATIC_CANCELED', 'order_id': 'E9',
                            'id_known': True, 'role': 'ENTRY', 'layer': 0,
                            'intent': {'qty': 1.0, 'price': '76620.0'}},
                        '%s|SL|L0|LONG' % BID: {
                            'state': 'CONFIRMED', 'order_id': 'SL1',
                            'id_known': True, 'role': 'SL', 'layer': 0,
                            'intent': {'qty': 1.0, 'stop_price': '75001.0'}},
                    }))
    ex._mk('SL1', otype='STOP_MARKET', amount=1.0, stop=75001.0, side='sell')
    ex._mk('TP1', otype='TAKE_PROFIT_MARKET', amount=1.0, stop=80000.0,
           side='sell')
    ex._mk('E9', otype='LIMIT', amount=1.0, stop=76620.0, side='buy',
           status='filled', filled=1.0)
    ex.open_orders = [ex.orders['SL1'], ex.orders['TP1']]
    ex.positions = [{'symbol': SYM, 'contracts': 1.0, 'positionAmt': 1.0,
                     'side': 'long', 'positionSide': 'LONG'}]

    r = _run_monitor(t, mode='once', join_s=4.0,
                     entry_orders=[], target_amounts=[1.0],
                     last_filled_count=1, filled_details=[76620.0],
                     total_entry_fee=fee_one)
    out, b = r['out'], r['b']
    assert '[R1c] 批次 %s 监控热收编' % BID in out, \
        '前置失败：热收编未发生（本用例未测到收编路径）:\n' + out[-1500:]
    fee = float((b or {}).get('total_entry_fee') or 0.0)
    assert abs(fee - fee_one) < 1e-6, \
        '已记账成交层收编后手续费被重复累计: got %r want %r\n%s' % (
            fee, fee_one, out[-1500:])
    assert int((b or {}).get('last_filled_count') or 0) == 1, \
        '成交层数变化: %r\n%s' % ((b or {}).get('last_filled_count'), out[-1500:])
    _fd = list((b or {}).get('filled_details') or [])
    assert _fd and abs(float(_fd[0]) - 76620.0) < 1e-6, \
        '成交价丢失: %r\n%s' % (_fd, out[-1500:])
    assert r['exited'], '用例收尸失败'

    # (b) 真正的新成交只累计一次（账本未记账 → 首次发现入账恰一次）
    t2, ex2 = make_trader(tempfile.mkdtemp(prefix='mres_'))
    _seed(t2, _batch(entry_orders=['E9'], target_amounts=[1.0],
                     filled_details=[0.0], last_filled_count=0,
                     total_entry_fee=0.0,
                     protection_registry={
                         '%s|ENTRY|L0|LONG' % BID: {
                             'state': 'PROGRAMMATIC_CANCELED', 'order_id': 'E9',
                             'id_known': True, 'role': 'ENTRY', 'layer': 0,
                             'intent': {'qty': 1.0, 'price': '76620.0'}},
                         '%s|SL|L0|LONG' % BID: {
                             'state': 'CONFIRMED', 'order_id': 'SL1',
                             'id_known': True, 'role': 'SL', 'layer': 0,
                             'intent': {'qty': 1.0, 'stop_price': '75001.0'}},
                     }))
    ex2._mk('SL1', otype='STOP_MARKET', amount=1.0, stop=75001.0, side='sell')
    ex2._mk('TP1', otype='TAKE_PROFIT_MARKET', amount=1.0, stop=80000.0,
            side='sell')
    ex2._mk('E9', otype='LIMIT', amount=1.0, stop=76620.0, side='buy',
            status='filled', filled=1.0)
    ex2.open_orders = [ex2.orders['SL1'], ex2.orders['TP1']]
    ex2.positions = [{'symbol': SYM, 'contracts': 1.0, 'positionAmt': 1.0,
                      'side': 'long', 'positionSide': 'LONG'}]
    r2 = _run_monitor(t2, mode='once', join_s=4.0,
                      entry_orders=[], target_amounts=[1.0],
                      last_filled_count=0, filled_details=[0.0],
                      total_entry_fee=0.0)
    fee2 = float((r2['b'] or {}).get('total_entry_fee') or 0.0)
    assert abs(fee2 - fee_one) < 1e-6, \
        '真正新成交未恰好累计一次: got %r want %r\n%s' % (
            fee2, fee_one, r2['out'][-1500:])
    assert r2['exited'], '用例收尸失败'


def c12_skeleton_plan_qty_never_overwrites_ledger_binding():
    """复审 P1 残项（第 4 轮）验收 A：**空订单快照带计划数量** —— 并发绑定后，
    锁内保存不得用快照里「尚未绑定订单的计划占位」覆盖账本确认的绑定数量。

    真实路径：真实 _persist_guard_arrays 检查（绑定前账本）→ 真实
    _bind_converged_entry_order 绑 E9（账本确认数量 0.5）→ 真实
    save_batch_state(align_bindings=True) 锁内重读合并落盘。"""
    t, ex = make_trader(tempfile.mkdtemp(prefix='mres_'))
    _seed(t, _batch(entry_orders=[], target_amounts=[],
                    filled_details=[], last_filled_count=0, total_entry_fee=0.0,
                    protection_registry={
                        '%s|ENTRY|L0|LONG' % BID: {
                            'state': 'CONFIRMED', 'order_id': 'E9',
                            'id_known': True, 'role': 'ENTRY', 'layer': 0,
                            'intent': {'qty': 0.5, 'price': '76620.0'}},
                    }))
    # 监控快照：空订单 + 未绑定的计划数量占位 [1.0]
    snap_eo, snap_ta, snap_fd = [], [1.0], [0.0]
    # 1) 守卫检查（对绑定前账本）—— 与监控股号点同调用
    data = {
        'is_active': True, 'batch_id': BID, 'symbol': SYM, 'side': 'BUY',
        'entry_orders': list(snap_eo), 'target_amounts': list(snap_ta),
        'filled_details': list(snap_fd), 'last_filled_count': 0,
        'total_entry_fee': 0.0, 'stop_steps': [75001.0],
        'take_profit_price': 60000.0, 'current_sl_id': None,
        'tp_order_id': None, 'batch_total_amount': 1.0,
        'params_base': {'positionSide': 'LONG', 'leverage': 100},
        'is_hedge_mode': True, 'pending_sl_orders': [],
        'prepared_tp_params': None, 'layer_sl_params': [], 'sl_fail_count': {},
    }
    _pg = t._persist_guard_arrays(snap_eo, snap_fd, snap_ta, _ledger(t) or {})
    if _pg is not None:
        data.update(_pg)
    # 2) 检查后、保存前：真实绑定 E9（账本确认数量来自 intent qty=0.5）
    ok = t._bind_converged_entry_order(SYM, BID, 'E9', '%s|ENTRY|L0|LONG' % BID)
    assert ok, '前置失败：真实绑定 E9 未成功'
    b1 = _ledger(t) or {}
    assert [str(x) for x in (b1.get('entry_orders') or [])] == ['E9']
    assert [float(x) for x in (b1.get('target_amounts') or [])] == [0.5], \
        '前置失败：绑定后账本数量应为 [0.5]，实得 %r' % (b1.get('target_amounts'),)
    # 3) 真实保存（锁内重读→身份合并→校验→落盘；身份对齐自第5轮起为默认行为）
    saved = t.save_batch_state(SYM, BID, data)
    assert saved is True, '真实保存未成功'
    b2 = _ledger(t) or {}
    _eo = [str(x) for x in (b2.get('entry_orders') or [])]
    _ta = [float(x) for x in (b2.get('target_amounts') or [])]
    assert _eo == ['E9'], '绑定丢失: %r' % (_eo,)
    assert _ta == [0.5], \
        '空快照的计划占位覆盖了账本确认数量: got %r want [0.5]' % (_ta,)


def c13_prefix_keeps_fill_and_tail_qty_from_ledger():
    """复审 P1 残项（第 4 轮）验收 B：**已有订单前缀带额外计划数量** —— 并发绑定后：
    ① 新绑定订单（尾部层）的数量取账本确认值 0.5，不得被快照未绑定占位 1.0 改写；
    ② 原前缀（E1）本代次新入账的成交价 76620.0 不得丢失。
    真实监控 + 真实绑定 + 真实保存（与 c10 同注入机制）。"""
    t, ex = make_trader(tempfile.mkdtemp(prefix='mres_'))
    _seed(t, _batch(entry_orders=['E1'], target_amounts=[1.0],
                    filled_details=[0.0], last_filled_count=0,
                    total_entry_fee=0.0,
                    protection_registry={
                        '%s|ENTRY|L0|LONG' % BID: {
                            'state': 'CONFIRMED', 'order_id': 'E1',
                            'id_known': True, 'role': 'ENTRY', 'layer': 0,
                            'intent': {'qty': 1.0, 'price': '76620.0'}},
                        '%s|ENTRY|L1|LONG' % BID: {
                            'state': 'CONFIRMED', 'order_id': 'E9',
                            'id_known': True, 'role': 'ENTRY', 'layer': 1,
                            'intent': {'qty': 0.5, 'price': '76630.0'}},
                        '%s|SL|L0|LONG' % BID: {
                            'state': 'CONFIRMED', 'order_id': 'SL1',
                            'id_known': True, 'role': 'SL', 'layer': 0,
                            'intent': {'qty': 1.0, 'stop_price': '75001.0'}},
                    }))
    ex._mk('SL1', otype='STOP_MARKET', amount=1.0, stop=75001.0, side='sell')
    ex._mk('TP1', otype='TAKE_PROFIT_MARKET', amount=1.0, stop=80000.0,
           side='sell')
    ex._mk('E1', otype='LIMIT', amount=1.0, stop=76620.0, side='buy',
           status='filled', filled=1.0)
    ex._mk('E9', otype='LIMIT', amount=0.5, stop=76630.0, side='buy',
           status='open', filled=0.0)
    ex.open_orders = [ex.orders['SL1'], ex.orders['TP1'], ex.orders['E9']]
    ex.positions = [{'symbol': SYM, 'contracts': 1.0, 'positionAmt': 1.0,
                     'side': 'long', 'positionSide': 'LONG'}]

    # 注入：守卫检查（对绑定前账本）完成后、真实落盘前，并发绑定 E9
    #（钩子避开持锁期 —— 第5轮守卫默认化后未带参写盘在锁内也调守卫，同 c10 注）
    _real_guard = t._persist_guard_arrays
    _st = {'injected': 0}

    def _hooked_guard(snap_eo, snap_fd, snap_ta, ledger_b):
        res = _real_guard(snap_eo, snap_fd, snap_ta, ledger_b)
        if _st['injected'] == 0 and not t._state_lock.locked():
            _st['injected'] += 1
            ok = t._bind_converged_entry_order(
                SYM, BID, 'E9', '%s|ENTRY|L1|LONG' % BID)
            assert ok, '并发绑定注入本身失败（前置不成立）'
        return res

    t._persist_guard_arrays = _hooked_guard
    r = _run_monitor(t, mode='once', join_s=4.0,
                     entry_orders=['E1'],
                     target_amounts=[1.0, 1.0],   # 已绑定 E1 + 额外计划占位
                     last_filled_count=0, filled_details=[0.0],
                     total_entry_fee=0.0)
    out, b = r['out'], r['b']
    assert _st['injected'] >= 1, '守卫检查后窗口未发生（注入未触发）'
    _eo = [str(x) for x in ((b or {}).get('entry_orders') or [])]
    _ta = [float(x) for x in ((b or {}).get('target_amounts') or [])]
    _fd = [float(x or 0) for x in ((b or {}).get('filled_details') or [])]
    assert _eo == ['E1', 'E9'], '并发绑定丢失: %r\n%s' % (_eo, out[-1200:])
    assert _ta == [1.0, 0.5], \
        '新绑定订单数量被快照计划占位改写（或前缀量被改）: got %r want [1.0, 0.5]\n%s' % (
            _ta, out[-1200:])
    assert _fd and abs(_fd[0] - 76620.0) < 1e-6, \
        '原前缀本代次新入账成交价丢失: %r\n%s' % (_fd, out[-1200:])
    assert r['exited'], '用例收尸失败（后台线程泄漏会污染后续用例）'


def c14_plain_save_keeps_ledger_binding():
    """外部复审第 1 项（TOCTOU 残留）：**凡携带 entry_orders 键的写盘**都必须在锁内
    做身份对齐——原来仅两处监控落盘点 opt-in，其余「读全量 dict → 改一个字段 → 回写」
    的窗口（pending_sl / 冻结入账 / 收养 / user_modified / sl_error / tp_fail_count
    等 10 处）同样会把并发绑定覆盖回旧快照。

    用例形状 = 复审建议的最小反例：快照 `entry_orders=[]`（陈旧读数）、账本已绑定
    `['E1']` → 普通（不带任何对齐参数的）save_batch_state 落盘后绑定不得丢、三数组
    保持对应，同时本盘变更字段（pending_sl_orders）照常写入。"""
    t, ex = make_trader(tempfile.mkdtemp(prefix='mres_'))
    _seed(t, _batch(entry_orders=['E1'], target_amounts=[1.0],
                    filled_details=[0.0], last_filled_count=0, total_entry_fee=0.0,
                    protection_registry={
                        '%s|ENTRY|L0|LONG' % BID: {
                            'state': 'CONFIRMED', 'order_id': 'E1',
                            'id_known': True, 'role': 'ENTRY', 'layer': 0,
                            'intent': {'qty': 1.0, 'price': '76620.0'}},
                        '%s|SL|L0|LONG' % BID: {
                            'state': 'CONFIRMED', 'order_id': 'SL1',
                            'id_known': True, 'role': 'SL', 'layer': 0,
                            'intent': {'qty': 1.0, 'stop_price': '75001.0'}},
                    }))
    # 模拟10处调用点的陈旧全量快照：读到 entry_orders=[] 之后只改 pending 字段就回写
    stale = _batch(entry_orders=[], target_amounts=[1.0],
                   filled_details=[0.0], last_filled_count=0, total_entry_fee=0.0,
                   pending_sl_orders=[0])
    ok = t.save_batch_state(SYM, BID, stale)        # 普通保存，无任何对齐参数
    assert ok is True, '普通保存被拒（应成功且保住绑定）'
    b = _ledger(t) or {}
    _eo = [str(x) for x in (b.get('entry_orders') or [])]
    _ta = list(b.get('target_amounts') or [])
    _fd = list(b.get('filled_details') or [])
    assert _eo == ['E1'], \
        '读-改-写窗口的陈旧快照覆盖了并发绑定: %r' % (_eo,)
    assert len(_ta) == len(_eo) and len(_fd) == len(_eo), \
        '三数组不对应: eo=%r ta=%r fd=%r' % (_eo, _ta, _fd)
    assert list(b.get('pending_sl_orders') or []) == [0], \
        '本盘变更字段未写入（保存未生效）: %r' % (b.get('pending_sl_orders'),)


def c15_chain_shrink_optout_still_allowed():
    """外部复审第 1 项裁决的**唯一例外**：有意收缩（10357「只保留已成交的订单」）
    必须经 `allow_chain_shrink=True` 显式跳过默认对齐，否则默认 append-only 会把
    被程序撤掉的未成交单保下来（合法收缩被误伤）。"""
    t, ex = make_trader(tempfile.mkdtemp(prefix='mres_'))
    _seed(t, _batch(entry_orders=['E1', 'E9'], target_amounts=[1.0, 0.5],
                    filled_details=[76620.0, 0.0], last_filled_count=1,
                    total_entry_fee=38.31,
                    protection_registry={
                        '%s|ENTRY|L0|LONG' % BID: {
                            'state': 'FILLED', 'order_id': 'E1',
                            'id_known': True, 'role': 'ENTRY', 'layer': 0,
                            'intent': {'qty': 1.0, 'price': '76620.0'}},
                        '%s|ENTRY|L1|LONG' % BID: {
                            'state': 'PROGRAMMATIC_CANCELED', 'order_id': 'E9',
                            'id_known': True, 'role': 'ENTRY', 'layer': 1,
                            'intent': {'qty': 0.5, 'price': '76630.0'}},
                    }))
    shrink = _batch(entry_orders=['E1'], target_amounts=[1.0],
                    filled_details=[76620.0], last_filled_count=1,
                    total_entry_fee=38.31,
                    is_programmatic_cancel=True)
    ok = t.save_batch_state(SYM, BID, shrink, allow_chain_shrink=True)
    assert ok is True, '有意收缩保存被拒'
    b = _ledger(t) or {}
    _eo = [str(x) for x in (b.get('entry_orders') or [])]
    assert _eo == ['E1'], \
        '有意收缩未生效（默认对齐误保被撤单）: %r' % (_eo,)


def c16_missing_filled_details_selfheals():
    """外部复审第 2 项（活性缺口）：账本 `filled_details` 缺失时不得永久拒写——
    (a) 守卫对缺键按链长补零（原来 `None → [] → 长度不等 → None` 永久拒绝）；
    (b) 真实 save（快照链是账本的扩展前缀场景）落盘成功且磁盘补齐该键；
    (c) 真实绑定扩展链时同样补齐（原来仅 `isinstance(list) 且更短` 才补）。"""
    t, ex = make_trader(tempfile.mkdtemp(prefix='mres_'))
    # (a) 守卫：快照空链、账本 [E9] 但缺 filled_details 键
    g = t._persist_guard_arrays([], [], [],
                                {'entry_orders': ['E9'], 'target_amounts': [1.0]})
    assert g is not None and g['entry_orders'] == ['E9'] \
        and [float(x) for x in g['filled_details']] == [0.0] \
        and [float(x) for x in g['target_amounts']] == [1.0], \
        '缺键场景守卫仍拒绝（永久拒写活性缺口）: %r' % (g,)
    # (b) 真实 save：陈旧空快照 + 账本缺键 → 成功且自愈
    bd = _batch(entry_orders=['E9'], target_amounts=[1.0],
                last_filled_count=0, total_entry_fee=0.0,
                protection_registry={
                    '%s|ENTRY|L0|LONG' % BID: {
                        'state': 'CONFIRMED', 'order_id': 'E9',
                        'id_known': True, 'role': 'ENTRY', 'layer': 0,
                        'intent': {'qty': 1.0, 'price': '76620.0'}}})
    bd.pop('filled_details', None)                # 账本缺该键
    _seed(t, bd)
    stale = _batch(entry_orders=[], target_amounts=[1.0],
                   filled_details=[0.0], last_filled_count=0, total_entry_fee=0.0)
    ok = t.save_batch_state(SYM, BID, stale)
    assert ok is True, '缺键账本被永久拒写（活性缺口）'
    b = _ledger(t) or {}
    assert 'filled_details' in b and len(b['filled_details']) == 1, \
        '缺键未被自愈: %r' % (b.get('filled_details'),)
    assert [str(x) for x in (b.get('entry_orders') or [])] == ['E9']
    # (c) 真实绑定：扩展链时补齐
    bd2 = _batch(entry_orders=[], target_amounts=[],
                 last_filled_count=0, total_entry_fee=0.0,
                 protection_registry={
                     '%s|ENTRY|L0|LONG' % BID: {
                         'state': 'CONFIRMED', 'order_id': 'E9',
                         'id_known': True, 'role': 'ENTRY', 'layer': 0,
                         'intent': {'qty': 1.0, 'price': '76620.0'}}})
    bd2.pop('filled_details', None)
    _seed(t, bd2)
    ok_bind = t._bind_converged_entry_order(
        SYM, BID, 'E9', '%s|ENTRY|L0|LONG' % BID)
    assert ok_bind, '前置失败：绑定未成功'
    b2 = _ledger(t) or {}
    assert isinstance(b2.get('filled_details'), list) \
        and len(b2['filled_details']) == len(b2.get('entry_orders') or []), \
        '绑定扩展后未补齐 filled_details: %r' % (b2.get('filled_details'),)


def c17_missing_target_amounts_rejects_with_alert():
    """外部复审第 2 轮·第 1 项：账本 `target_amounts` 缺失（前缀场景）时，默认对齐
    会拒绝本轮全部链相关写入——fail-closed 之外必须**显式告警**，不得静默卡死
    保护维护。断言：保存被拒、账本原状未动、critical 告警已发。"""
    t, ex = make_trader(tempfile.mkdtemp(prefix='mres_'))
    bd = _batch(entry_orders=['E9'], target_amounts=[1.0], filled_details=[0.0],
                last_filled_count=0, total_entry_fee=0.0,
                protection_registry={
                    '%s|ENTRY|L0|LONG' % BID: {
                        'state': 'CONFIRMED', 'order_id': 'E9',
                        'id_known': True, 'role': 'ENTRY', 'layer': 0,
                        'intent': {'qty': 1.0, 'price': '76620.0'}}})
    bd.pop('target_amounts', None)             # 账本缺 ta 键
    _seed(t, bd)
    stale = _batch(entry_orders=[], target_amounts=[], filled_details=[0.0],
                   last_filled_count=0, total_entry_fee=0.0,
                   user_modified=True)
    ok = t.save_batch_state(SYM, BID, stale)
    assert ok is False, '缺 ta 的账本未被拒写（应 fail-closed）'
    b = _ledger(t) or {}
    assert 'target_amounts' not in b and not b.get('user_modified'), \
        '拒写后账本仍被改动（应保持原状）: %r' % (b,)
    assert any('对齐校验拒绝写入' in m and '不会自动恢复' in m and lv == 'critical'
               for lv, m in zip(t.sent_tg_levels + [None] * len(t.sent_tg),
                                t.sent_tg)), \
        '拒写未告警/缺人工处置提示（静默卡死保护维护）: %r' % (t.sent_tg[-3:],)


def c18_shrink_then_rebuild_restores_binding():
    """外部复审第 2 轮·第 1 项兜底论证的**测试证明**：`allow_chain_shrink` 的有意
    收缩把未成交层从链上摘掉后，registry（CONFIRMED + order_id 仍在）经**真实**
    `_rebuild_entry_orders_from_registry`（锁内直写 `_commit_registry_txn`）把被
    回退的绑定找回来。"""
    t, ex = make_trader(tempfile.mkdtemp(prefix='mres_'))
    _seed(t, _batch(entry_orders=['E1', 'E9'], target_amounts=[1.0, 0.5],
                    filled_details=[76620.0, 0.0], last_filled_count=1,
                    total_entry_fee=38.31,
                    protection_registry={
                        '%s|ENTRY|L0|LONG' % BID: {
                            'state': 'CONFIRMED', 'order_id': 'E1',
                            'id_known': True, 'role': 'ENTRY', 'layer': 0,
                            'intent': {'qty': 1.0, 'price': '76620.0'}},
                        '%s|ENTRY|L1|LONG' % BID: {
                            'state': 'CONFIRMED', 'order_id': 'E9',
                            'id_known': True, 'role': 'ENTRY', 'layer': 1,
                            'intent': {'qty': 0.5, 'price': '76630.0'}},
                    }))
    shrink = _batch(entry_orders=['E1'], target_amounts=[1.0],
                    filled_details=[76620.0], last_filled_count=1,
                    total_entry_fee=38.31, is_programmatic_cancel=True)
    ok = t.save_batch_state(SYM, BID, shrink, allow_chain_shrink=True)
    assert ok is True, '有意收缩保存被拒'
    b0 = _ledger(t) or {}
    assert [str(x) for x in (b0.get('entry_orders') or [])] == ['E1'], \
        '收缩未生效: %r' % (b0.get('entry_orders'),)
    # 真实链重建：registry 两单均 CONFIRMED+order_id → 绑定找回
    orders, rebuilt = t._rebuild_entry_orders_from_registry(SYM, BID)
    assert rebuilt is True, '链重建未提交'
    b1 = _ledger(t) or {}
    assert [str(x) for x in (b1.get('entry_orders') or [])] == ['E1', 'E9'], \
        '收缩后重建未找回绑定: %r' % (b1.get('entry_orders'),)
    assert len(b1.get('target_amounts') or []) == 2, \
        '重建后数量层未对齐: %r' % (b1.get('target_amounts'),)


def c19_ta_misalign_blocks_all_chain_writes():
    """外部复审第 2 轮·第 1 项爆炸半径**如实验证**：账本 ta 短于链（长度错乱）时，
    默认对齐拒绝**所有**带 entry_orders 的写入——包括与成交无关的字段
    （user_modified）——并且必须告警。该批次在此状态下保护维护停摆是已知代价，
    由告警引导人工/重建路径恢复，不允许静默。"""
    t, ex = make_trader(tempfile.mkdtemp(prefix='mres_'))
    _seed(t, _batch(entry_orders=['E1', 'E9'], target_amounts=[1.0],
                    filled_details=[76620.0, 0.0], last_filled_count=1,
                    total_entry_fee=38.31,
                    protection_registry={
                        '%s|ENTRY|L0|LONG' % BID: {
                            'state': 'CONFIRMED', 'order_id': 'E1',
                            'id_known': True, 'role': 'ENTRY', 'layer': 0,
                            'intent': {'qty': 1.0, 'price': '76620.0'}},
                        '%s|ENTRY|L1|LONG' % BID: {
                            'state': 'CONFIRMED', 'order_id': 'E9',
                            'id_known': True, 'role': 'ENTRY', 'layer': 1,
                            'intent': {'qty': 0.5, 'price': '76630.0'}},
                    }))
    snap = _batch(entry_orders=['E1'], target_amounts=[1.0],
                  filled_details=[76620.0], last_filled_count=1,
                  total_entry_fee=38.31, user_modified=True)
    ok = t.save_batch_state(SYM, BID, snap)
    assert ok is False, '错乱账本未被拒写（应 fail-closed）'
    b = _ledger(t) or {}
    assert [str(x) for x in (b.get('entry_orders') or [])] == ['E1', 'E9'], \
        '拒写后链被改动: %r' % (b.get('entry_orders'),)
    assert not b.get('user_modified'), \
        '错乱账本上非链字段写入仍落盘（应一并拒绝）'
    assert any('对齐校验拒绝写入' in m and '不会自动恢复' in m and lv == 'critical'
               for lv, m in zip(t.sent_tg_levels + [None] * len(t.sent_tg),
                                t.sent_tg)), \
        '拒写未告警/缺人工处置提示（静默）: %r' % (t.sent_tg[-3:],)


def c20_lifecycle_per_read_corruption_verdict():
    """外部复审第 3 轮·第 1 项：损坏判定必须取**本次读取**三元组。确定性复现共享
    标志竞态——本线程读失败（占位 {} + 标志置位）后，并发的成功读把共享标志冲成
    False，旧口径随即把占位 {} 判成「批次已清理」→ 'exit'（保护停摆、僵尸批次）。
    修后：调用方把 per-read corrupt 传入守卫 → 'unknown'（跳过副作用、继续等）。
    另附源码钉：G1/G3 生命周期点必须经 `_load_all_states_ex` 取三元组。"""
    t, ex = make_trader(tempfile.mkdtemp(prefix='mres_'))
    _seed(t, _batch(entry_orders=['E1'], target_amounts=[1.0],
                    filled_details=[0.0], last_filled_count=0, total_entry_fee=0.0,
                    protection_registry={
                        '%s|ENTRY|L0|LONG' % BID: {
                            'state': 'CONFIRMED', 'order_id': 'E1',
                            'id_known': True, 'role': 'ENTRY', 'layer': 0,
                            'intent': {'qty': 1.0, 'price': '76620.0'}}}))
    _real_ex = t._load_all_states_ex
    calls = {'n': 0}

    def fail_once_then_ok():
        calls['n'] += 1
        if calls['n'] == 1:
            t._state_corrupted = True
            t._state_corruption_detail = (
                'sim: transient read failure (os.replace/外部锁交错)')
            return {}, True, t._state_corruption_detail
        return _real_ex()

    t._load_all_states_ex = fail_once_then_ok
    _all_a = t.load_all_states()                       # 线程 A：本次读失败
    assert _all_a == {} and t._state_corrupted is True, '前置失败'
    _all_b = t.load_all_states()                       # 并发竞争者：成功读
    assert t._state_corrupted is False, \
        '前置失败：共享标志应已被并发成功读复位（竞态前提不成立）'
    st = t._monitor_lifecycle_check(_all_a, (_all_a.get(SYM) or {}).get(BID, {}),
                                    read_corrupt=True)
    assert st == 'unknown', \
        '损坏读被当成批次不存在（exit）→ 保护停摆、僵尸批次: %r' % (st,)
    # 对照：干净读语义不变
    _all_c, _c, _d = _real_ex()
    st2 = t._monitor_lifecycle_check(_all_c, (_all_c.get(SYM) or {}).get(BID, {}),
                                     read_corrupt=_c)
    assert st2 == 'ok', '干净读回归破坏: %r' % (st2,)
    # 源码钉：G1/G3 两处生命周期点 + 恢复重读共 3 处必须取 per-read 三元组
    _src = open(trader_260725.__file__, encoding='utf-8').read()
    assert _src.count('read_corrupt=') >= 3, \
        '生命周期/恢复重读点未接 per-read 三元组（共享标志竞态回归）'


def c21_g2_zero_settlement_guard_per_read():
    """外部复审第 4 轮·第 1 项：**G2（归零结算前 TOCTOU 守卫）**同样必须用本次
    读取的损坏三元组——共享标志被并发成功读冲掉后，占位 {} 会被判 'exit' →
    break → 持仓批次失去保护（比 G1/G3 后果更重：结算窗口直接退出）。
    机制复现 = G2 站点的精确调用形态（三元组 + 中途并发成功读）；源码钉 =
    G2 站点接入 `_g2_corrupt` 且全库 `read_corrupt=` 接入点 ≥4（G1/G2/G3/恢复）。"""
    t, ex = make_trader(tempfile.mkdtemp(prefix='mres_'))
    _seed(t, _batch(entry_orders=['E1'], target_amounts=[1.0],
                    filled_details=[0.0], last_filled_count=0, total_entry_fee=0.0,
                    protection_registry={
                        '%s|ENTRY|L0|LONG' % BID: {
                            'state': 'CONFIRMED', 'order_id': 'E1',
                            'id_known': True, 'role': 'ENTRY', 'layer': 0,
                            'intent': {'qty': 1.0, 'price': '76620.0'}}}))
    _real_ex = t._load_all_states_ex
    calls = {'n': 0}

    def fail_once_then_ok():
        calls['n'] += 1
        if calls['n'] == 1:
            t._state_corrupted = True
            t._state_corruption_detail = 'sim: G2 transient read failure'
            return {}, True, 'sim: G2 transient read failure'
        return _real_ex()

    t._load_all_states_ex = fail_once_then_ok
    # G2 站点形态：本次读（失败）→ 守卫判定前的并发成功读（共享标志被复位）
    _all, _corrupt, _detail = t._load_all_states_ex()
    _b = (_all.get(SYM) or {}).get(BID, {})
    _all2, _c2, _d2 = t._load_all_states_ex()
    assert t._state_corrupted is False, '竞态前提不成立（共享标志未被复位）'
    st = t._monitor_lifecycle_check(_all, _b, read_corrupt=_corrupt)
    assert st == 'unknown', \
        'G2 损坏读被判 exit → 持仓批次在结算窗口失去保护: %r' % (st,)
    # 源码钉：G2 站点必须取 per-read 三元组；全库接入点 ≥4
    _src = open(trader_260725.__file__, encoding='utf-8').read()
    assert '_g2_corrupt' in _src, 'G2 归零结算守卫未接 per-read 三元组'
    assert _src.count('read_corrupt=') >= 4, \
        'per-read 接入点不足（应为 G1/G2/G3/恢复重读共 4 处）'


def c22_terminal_evidence_never_true_on_unreadable_ledger():
    """外部复审第 5 轮：`_monitor_terminal_evidence` 在「账本不可读（占位 {}）+
    并发成功读冲掉共享标志 + 磁盘恰有有效墓碑」的组合下，也必须返回 False
    （UNKNOWN ≠ EMPTY——账本不可读时判「已终结」就是放行）。
    确定性复现：读失败之后、守卫判定之前插入一次成功读（共享标志复位）。
    对照：干净读 + 批次已删 + 有效墓碑 → True（证明墓碑路径本身活着）。"""
    t, ex = make_trader(tempfile.mkdtemp(prefix='mres_'))
    bd = _batch(entry_orders=['E1'], target_amounts=[1.0], filled_details=[0.0],
                last_filled_count=0, total_entry_fee=0.0,
                protection_registry={
                    '%s|ENTRY|L0|LONG' % BID: {
                        'state': 'CONFIRMED', 'order_id': 'E1',
                        'id_known': True, 'role': 'ENTRY', 'layer': 0,
                        'intent': {'qty': 1.0, 'price': '76620.0'}}})
    _seed(t, bd)
    # 磁盘上写入**有效墓碑**（与 clear_batch_state 落盘同形状）
    with open(t.tombstone_file, 'w', encoding='utf-8') as f:
        json.dump({BID: {
            'batch_id': BID, 'symbol': SYM, 'cleared_at': _time.time(),
            'close_phase': 3,
            'known_order_ids': ['E1'], 'converged_order_ids': [],
            'clear_evidence': {
                'batch_id': BID, 'symbol': SYM, 'position_zero': True,
                'exchange_scan': 'zero', 'scope': 'PRE_ENTRY',
                'state_ids_resolved': ['E1'],
            }}}, f)
    # 对照 1：批次仍在账本 → 不是终结
    assert t._monitor_terminal_evidence(SYM, BID) is False, '前置失败: 账本在场仍判终结'
    # 对照 2：干净读 + 批次已删 + 有效墓碑 → True（墓碑路径活着）
    with open(trader_260725.STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump({SYM: {}}, f)
    assert t._monitor_terminal_evidence(SYM, BID) is True, \
        '前置失败: 干净读下墓碑终结路径不通（对照失效）'
    # 竞态：批次仍在账本 + A 收到占位 {} + B 的成功读已在 A 判定前把共享标志复位
    _seed(t, bd)
    _real_load = t.load_all_states
    calls = {'n': 0}

    def load_fail_then_race_reset():
        calls['n'] += 1
        if calls['n'] == 1:
            # 模拟交错结果：A 的读失败（占位 {}）……
            t._state_corrupted = True
            t._state_corruption_detail = 'sim: transient ledger read failure'
            # ……且 B 的成功读已在 A 判定之前把共享标志冲回 False
            t._state_corrupted = False
            return {}
        return _real_load()

    t.load_all_states = load_fail_then_race_reset
    st = t._monitor_terminal_evidence(SYM, BID)
    assert st is False, \
        '账本不可读仍判「已终结」（True）→ UNKNOWN ≠ EMPTY 被违反: %r' % (st,)
    # 源码钉：该函数不得再依赖共享标志
    _src = open(trader_260725.__file__, encoding='utf-8').read()
    _mte = _src.split('def _monitor_terminal_evidence(')[1].split('\n    def ')[0]
    assert '_state_corrupted' not in _mte, \
        '_monitor_terminal_evidence 仍依赖共享标志（竞态回归）'


def c23_persist_gate_uses_per_read_corruption():
    """外部复审第 6 轮：写盘闸门必须用**调用方本次读取**的损坏三元组。竞态复现：
    锁内读失败（占位 {}）→ 并发成功读把共享标志复位 → 旧闸门失守 → 占位载荷
    覆盖磁盘、销毁其他批次（证据灭失 —— D-009 P0-B 要防的正是这个）。
    载荷启发式不可用（空载荷 ≠ 损坏：合法清掉最后一个批次时载荷同样为空），
    判据只能来自读取来源本身。"""
    t, ex = make_trader(tempfile.mkdtemp(prefix='mres_'))
    bd_a = _batch(entry_orders=['E1'], target_amounts=[1.0], filled_details=[0.0],
                  last_filled_count=0, total_entry_fee=0.0,
                  protection_registry={
                      '%s|ENTRY|L0|LONG' % BID: {
                          'state': 'CONFIRMED', 'order_id': 'E1',
                          'id_known': True, 'role': 'ENTRY', 'layer': 0,
                          'intent': {'qty': 1.0, 'price': '76620.0'}}})
    bd_b = _batch(entry_orders=['E2'], target_amounts=[1.0], filled_details=[0.0],
                  last_filled_count=0, total_entry_fee=0.0,
                  protection_registry={
                      '%s|ENTRY|L0|LONG' % 'batch_other_2': {
                          'state': 'CONFIRMED', 'order_id': 'E2',
                          'id_known': True, 'role': 'ENTRY', 'layer': 0,
                          'intent': {'qty': 1.0, 'price': '76620.0'}}})
    _state = {SYM: {BID: bd_a, 'batch_other_2': bd_b}}
    with open(trader_260725.STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(_state, f, ensure_ascii=False)
    _real_ex = t._load_all_states_ex
    _real_load = t.load_all_states
    calls = {'n': 0}

    def ex_fail_once():
        calls['n'] += 1
        if calls['n'] == 1:
            t._state_corrupted = True
            t._state_corruption_detail = 'sim: transient ledger read failure'
            return {}, True, 'sim: transient ledger read failure'
        return _real_ex()

    t._load_all_states_ex = ex_fail_once

    def load_then_race_reset():
        r = _real_load()                 # 经 ex_fail_once → 占位 {} + 标志 True
        if calls['n'] == 1:
            t._state_corrupted = False   # 并发成功读：判定前复位共享标志
        return r

    t.load_all_states = load_then_race_reset
    snap = _batch(entry_orders=['E1'], target_amounts=[1.0], filled_details=[0.0],
                  last_filled_count=0, total_entry_fee=0.0,
                  protection_registry={
                      '%s|ENTRY|L0|LONG' % BID: {
                          'state': 'CONFIRMED', 'order_id': 'E1',
                          'id_known': True, 'role': 'ENTRY', 'layer': 0,
                          'intent': {'qty': 1.0, 'price': '76620.0'}}},
                  pending_sl_orders=[0])
    ok = t.save_batch_state(SYM, BID, snap)
    assert ok is False, '占位 {} 仍完成落盘（写盘闸门失守 → 证据灭失）'
    with open(trader_260725.STATE_FILE, encoding='utf-8') as f:
        data = json.load(f)
    assert 'batch_other_2' in (data.get(SYM) or {}), \
        '占位载荷销毁了其他批次（batch_other_2 已从账本消失）'


def c24_clear_unreadable_uses_per_read_triple():
    """外部复审第 7 轮·①：清账入口的不可读判断必须用**本次读取**三元组——
    失败读被并发成功读复位后，旧口径把「未知」当「已清理」直接 return True
    （无删除、无告警）。断言：race 下返回 False、账本保留、critical 告警；
    对照：可信空账本（批次不存在）仍幂等 True。"""
    t, ex = make_trader(tempfile.mkdtemp(prefix='mres_'))
    _seed(t, _batch(entry_orders=['E1'], target_amounts=[1.0],
                    filled_details=[0.0], last_filled_count=0,
                    total_entry_fee=0.0,
                    protection_registry={
                        '%s|ENTRY|L0|LONG' % BID: {
                            'state': 'CONFIRMED', 'order_id': 'E1',
                            'id_known': True, 'role': 'ENTRY', 'layer': 0,
                            'intent': {'qty': 1.0, 'price': '76620.0'}}}))
    proof = {'batch_id': BID, 'symbol': SYM, 'scope': 'PRE_ENTRY',
             'position_zero': True, 'exchange_scan': 'zero',
             'state_ids_resolved': ['E1'], 'l1_canceled': ['E1'],
             'l2_canceled': [], 'l3_orphans': []}
    _real_ex = t._load_all_states_ex
    calls = {'n': 0}

    def ex_fail_then_race_reset():
        calls['n'] += 1
        if calls['n'] == 1:
            t._state_corrupted = True
            t._state_corruption_detail = 'sim: transient ledger read failure'
            t._state_corrupted = False          # 并发成功读在判定前复位共享标志
            return {}, True, 'sim: transient ledger read failure'
        return _real_ex()

    t._load_all_states_ex = ex_fail_then_race_reset
    ret = t.clear_batch_state(SYM, BID, proof=proof)
    assert ret is False, '不可读被误报为已清理（return True）: %r' % (ret,)
    with open(trader_260725.STATE_FILE, encoding='utf-8') as f:
        data = json.load(f)
    assert BID in (data.get(SYM) or {}), '账本被改动/删除（应保留）'
    assert any('不可读' in m and lv == 'critical'
               for lv, m in zip(t.sent_tg_levels + [None] * len(t.sent_tg),
                                t.sent_tg)), \
        '不可读拒绝未告警: %r' % (t.sent_tg[-3:],)
    # 对照：可信空账本 → 批次不存在 → 幂等 True（既有语义保留）
    t2, ex2 = make_trader(tempfile.mkdtemp(prefix='mres_'))
    with open(trader_260725.STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump({SYM: {}}, f)
    ret2 = t2.clear_batch_state(SYM, BID, proof=proof)
    assert ret2 is True, '可信空账本的幂等成功被破坏: %r' % (ret2,)


def c25_filled_layer_missing_price_guard_and_be():
    """外部复审第 7 轮·②：已成交层缺价绝不静默补零当已知成本——
    (a) 无可信证据 → save 后该层登记 cost_pending（blocked 依赖成本的 /be/结算）；
    (b) 有 fill_evidence.price → 恢复进 filled_details；
    (c) 已成交层 fd=0 时 `/be` 必须在**撤旧止损之前**拒绝（False、SL1 原样、
        零 cancel/零 create、critical 告警）。"""
    _reg = {
        '%s|ENTRY|L0|LONG' % BID: {
            'state': 'CONFIRMED', 'order_id': 'E1',
            'id_known': True, 'role': 'ENTRY', 'layer': 0,
            'intent': {'qty': 1.0, 'price': '76620.0'}},
        '%s|SL|L0|LONG' % BID: {
            'state': 'CONFIRMED', 'order_id': 'SL1',
            'id_known': True, 'role': 'SL', 'layer': 0,
            'intent': {'qty': 1.0, 'stop_price': '75001.0'}},
    }
    # (a) 缺价且无可信证据 → 登记 cost_pending
    t, ex = make_trader(tempfile.mkdtemp(prefix='mres_'))
    bd = _batch(entry_orders=['E1'], target_amounts=[1.0],
                last_filled_count=1, total_entry_fee=0.0,
                protection_registry=_reg)
    bd.pop('filled_details', None)
    _seed(t, bd)
    snap = _batch(entry_orders=['E1'], target_amounts=[1.0],
                  filled_details=[0.0], last_filled_count=1,
                  total_entry_fee=0.0, pending_sl_orders=[],
                  protection_registry=_reg)
    ok = t.save_batch_state(SYM, BID, snap)
    assert ok is True, '缺价保存被拒（应可写并登记待确认）: %r' % (ok,)
    with open(trader_260725.STATE_FILE, encoding='utf-8') as f:
        b = json.load(f).get(SYM, {}).get(BID, {})
    assert 0 in [int(x) for x in (b.get('cost_pending_layers') or [])], \
        '已成交层缺价未登记 cost_pending（被静默当已知成本）: %r' % (
            b.get('cost_pending_layers'),)
    # (b) 有可信证据 → 恢复到 filled_details
    t2, ex2 = make_trader(tempfile.mkdtemp(prefix='mres_'))
    bd2 = _batch(entry_orders=['E1'], target_amounts=[1.0],
                 last_filled_count=1, total_entry_fee=0.0,
                 fill_evidence={'0': {'status': 'cost_pending', 'idx': 0,
                                      'order_id': 'E1', 'price': 76620.0}},
                 protection_registry=_reg)
    bd2.pop('filled_details', None)
    _seed(t2, bd2)
    snap2 = _batch(entry_orders=['E1'], target_amounts=[1.0],
                   filled_details=[0.0], last_filled_count=1,
                   total_entry_fee=0.0, pending_sl_orders=[],
                   protection_registry=_reg)
    ok2 = t2.save_batch_state(SYM, BID, snap2)
    assert ok2 is True
    with open(trader_260725.STATE_FILE, encoding='utf-8') as f:
        b2 = json.load(f).get(SYM, {}).get(BID, {})
    _fd2 = list(b2.get('filled_details') or [])
    assert _fd2 and abs(float(_fd2[0]) - 76620.0) < 1e-6, \
        '可信成交证据未恢复到 filled_details: %r' % (_fd2,)
    # (c) /be 撤旧前拒绝
    t3, ex3 = make_trader(tempfile.mkdtemp(prefix='mres_'))
    _seed(t3, _batch(entry_orders=['E1'], target_amounts=[1.0],
                     filled_details=[0.0], last_filled_count=1,
                     total_entry_fee=0.0, current_sl_id='SL1',
                     protection_registry=_reg))
    ex3._mk('SL1', otype='STOP_MARKET', amount=1.0, stop=75001.0, side='sell')
    ex3.open_orders = [ex3.orders['SL1']]
    ex3.ticker_price = 0.0
    ret = t3.set_breakeven_sl(BID)
    assert ret[0] is False, '缺价仍放行 /be: %r' % (ret,)
    assert ex3.cancel_calls == [] and ex3.create_calls == [], \
        '缺价时发生撤旧/建新: cancel=%r create=%r' % (
            ex3.cancel_calls, ex3.create_calls)
    assert (ex3.orders.get('SL1') or {}).get('status') == 'open', \
        '现有 SL 被撤销（应保持有效）'
    assert any('缺有效成交价' in m and lv == 'critical'
               for lv, m in zip(t3.sent_tg_levels + [None] * len(t3.sent_tg),
                                t3.sent_tg)), \
        '/be 缺价拒绝未告警: %r' % (t3.sent_tg[-3:],)


def c26_tp_refuses_when_cost_not_ready():
    """复审（外审 7 同类路径，阻塞项）：`/tp` 与 `/be` 同属「成本依赖 + 撤旧保护」，
    成本未就绪时必须在**任何校验/撤单之前**拒绝——否则被 fd=0 拉低的 VWAP 会放行
    低于真实保本的止盈，随后撤旧挂新、触发即亏。断言：cost_pending 批 与
    已成交层缺价批 调用 update_batch_tp 均返回 False、TP1 原样、零 cancel/零 create、
    critical 告警。"""
    _reg = {
        '%s|ENTRY|L0|LONG' % BID: {
            'state': 'CONFIRMED', 'order_id': 'E1',
            'id_known': True, 'role': 'ENTRY', 'layer': 0,
            'intent': {'qty': 1.0, 'price': '76620.0'}},
        '%s|TP|L0|LONG' % BID: {
            'state': 'CONFIRMED', 'order_id': 'TP1',
            'id_known': True, 'role': 'TP', 'layer': 0,
            'intent': {'qty': 1.0, 'stop_price': '80000.0'}},
    }
    # (a) 已登记 cost_pending
    t, ex = make_trader(tempfile.mkdtemp(prefix='mres_'))
    _seed(t, _batch(filled_details=[0.0], cost_pending_layers=[0],
                    protection_registry=_reg))
    ex._mk('TP1', otype='TAKE_PROFIT_MARKET', amount=1.0, stop=80000.0, side='sell')
    ex.open_orders = [ex.orders['TP1']]
    ret = t.update_batch_tp(BID, 1.0)   # 远低于真实保本（≈76620）
    assert ret[0] is False, 'cost_pending 批仍放行 /tp: %r' % (ret,)
    assert ex.cancel_calls == [] and ex.create_calls == [], \
        'cost_pending 时发生撤旧/建新: cancel=%r create=%r' % (
            ex.cancel_calls, ex.create_calls)
    assert (ex.orders.get('TP1') or {}).get('status') == 'open', \
        '现有 TP 被撤销（应保持有效）'
    assert any('成本未就绪' in m and lv == 'critical'
               for lv, m in zip(t.sent_tg_levels + [None] * len(t.sent_tg),
                                t.sent_tg)), \
        '/tp cost_pending 拒绝未告警: %r' % (t.sent_tg[-3:],)
    # (b) 无 cost_pending 但已成交层缺价
    t2, ex2 = make_trader(tempfile.mkdtemp(prefix='mres_'))
    _seed(t2, _batch(filled_details=[0.0], protection_registry=_reg))
    ex2._mk('TP1', otype='TAKE_PROFIT_MARKET', amount=1.0, stop=80000.0, side='sell')
    ex2.open_orders = [ex2.orders['TP1']]
    ret2 = t2.update_batch_tp(BID, 1.0)
    assert ret2[0] is False, '已成交层缺价仍放行 /tp: %r' % (ret2,)
    assert ex2.cancel_calls == [] and ex2.create_calls == [], \
        '缺价时发生撤旧/建新: cancel=%r create=%r' % (
            ex2.cancel_calls, ex2.create_calls)
    assert (ex2.orders.get('TP1') or {}).get('status') == 'open', \
        '现有 TP 被撤销（应保持有效）'


def c27_save_protection_block_exception_is_fail_closed():
    """复审（外审 7 非阻塞项）：save 侧「已成交层缺价保护」块若自身异常，必须
    **fail-closed 拒绝落盘并告警**——否则已成交层的伪造 0 成本会原样落盘（fail-open）。
    以病态 `last_filled_count` 确定性触发（`int()` 抛错）。断言：save 返回 False、
    磁盘未写入伪造 0 成本、critical 告警含异常原因。"""
    t, ex = make_trader(tempfile.mkdtemp(prefix='mres_'))
    seed = _batch(last_filled_count='abc')
    seed.pop('filled_details', None)
    _seed(t, seed)
    snap = _batch(filled_details=[0.0], last_filled_count='abc',
                  pending_sl_orders=[])
    ok = t.save_batch_state(SYM, BID, snap)
    assert ok is False, '保护块异常仍完成落盘（fail-open）: %r' % (ok,)
    disc = _ledger(t)
    assert disc is not None, '账本被删（应保留）'
    _fd_disc = disc.get('filled_details')
    assert not (isinstance(_fd_disc, list) and _fd_disc
                and float(_fd_disc[0]) == 0.0), \
        '伪造 0 成本被落盘: %r' % (_fd_disc,)
    assert any('保护块异常' in m and lv == 'critical'
               for lv, m in zip(t.sent_tg_levels + [None] * len(t.sent_tg),
                                t.sent_tg)), \
        'fail-closed 未告警: %r' % (t.sent_tg[-3:],)


def r4a_single_outer_finally_replaces_reraise():
    """R4 结构钉住（源码判据）：**替换**而非叠加。

    ① `_reraise` 桥接与 `except BaseException` 处理器一并撤掉；
    ② 主循环整体下移一级、被**单一**外层 `try:` 包住；
    ③ 原收尾块（含最后那句「监控线程已退出」）落在配对的 `finally:` 内，
       且收尾之后不得再有任何再抛逻辑。
    """
    src = open(trader_260725.__file__, encoding='utf-8', errors='replace').read()
    assert 'def _start_monitoring(' in src, '被测函数不存在'
    body = src.split('def _start_monitoring(', 1)[1].split('\n    def ', 1)[0]
    ls = body.splitlines()
    # 判据看**代码行**，不看注释：R4 的注释里必须保留对 `_reraise` 桥接的历史说明
    code = [l.strip() for l in ls if l.strip() and not l.strip().startswith('#')]
    assert not any(l.startswith('_reraise') for l in code), \
        'R4：_reraise 赋值应被撤掉（源码里仍在）'
    assert not any(l.startswith('raise _reraise') for l in code), \
        'R4：函数末尾的 _reraise 再抛应被撤掉（源码里仍在）'
    assert not any(l.startswith('except BaseException') for l in code), \
        'R4：except BaseException 处理器应被撤掉（异常须自然穿出 → finally → 传播）'
    iw = next((i for i, l in enumerate(ls) if l.strip() == 'while True:'), -1)
    assert iw > 0, '主循环 while True 未找到'
    assert ls[iw].rstrip() == '            while True:', \
        'R4：主循环应整体下移一级（现: %r）' % ls[iw]
    it = next((i for i in range(iw - 1, -1, -1)
               if ls[i].rstrip() == '        try:'), -1)
    assert 0 <= it < iw, 'R4：缩进=8 的外层 try 未包住主循环'
    fin = next((i for i in range(iw + 1, len(ls))
                if ls[i].rstrip() == '        finally:'), -1)
    assert fin > iw, 'R4：外层 finally 未找到'
    tail = '\n'.join(ls[fin:])
    assert '收尾块（事故修复 3' in tail, 'R4：收尾块未迁入 finally 内'
    assert '监控线程已退出' in tail, 'R4：收尾块的退出打印不在 finally 内'
    assert 'raise' not in tail.split('监控线程已退出', 1)[-1], \
        'R4：finally 收尾之后不应再有 _reraise 式再抛'


def r4b_handler_base_exception_still_runs_finally_cleanup():
    """R4 行为：异常发生在**处置层（except Exception 处理体）内**时收尾仍必须执行。

    修前：`except BaseException → _reraise → break` 只覆盖**循环体 try 内**的异常；
    处理体内抛出的 BaseException（本用例注入点：W1 写入处置层内抛 KeyboardInterrupt，
    内层 `except Exception` 接不住 BaseException）会**直接穿出 while** → 循环外的
    收尾块整段被跳过 → 代次登记泄漏、线程以 traceback 终结，所谓「先收尾后传播」
    的桥接根本没生效。
    修后：收尾在**外层 finally**，穿出路径照样执行；随后异常原样传播（不再靠
    `_reraise`，也没有任何捕获）。
    """
    t, ex = make_trader(tempfile.mkdtemp(prefix='mres_'))
    _seed(t, _program_cancel_batch())
    ex._mk('E1', otype='LIMIT', amount=1.0, stop=76620.0, side='buy',
           status='filled', filled=1.0)
    ex.positions = []
    _real_w1 = t._write_monitor_error_if_owner
    _hits = {'n': 0}

    def _probe(*a, **k):
        _hits['n'] += 1
        if _hits['n'] == 1:        # 只炸第一次：那一次正是处置层的 W1 写入
            raise KeyboardInterrupt('R4 probe（处置层内 BaseException）')
        return _real_w1(*a, **k)

    t._write_monitor_error_if_owner = _probe
    excs = []
    _real_hook = threading.excepthook
    threading.excepthook = lambda args: excs.append(args.exc_value)
    try:
        r = _run_monitor(t, mode='always', join_s=4.0,
                         last_filled_count=1, filled_details=[76620.0],
                         current_sl_id=None, tp_order_id=None)
    finally:
        threading.excepthook = _real_hook
    out = r['out']
    assert _hits['n'] >= 1, '探针未命中（处置层没走到 W1 写入）:\n' + out[-2000:]
    assert any(isinstance(x, KeyboardInterrupt) for x in excs), \
        '异常必须原样传播到线程之外（被吞成正常退出了）: %r\n%s' % (excs, out[-1500:])
    assert '监控线程已退出' in out, \
        'R4：穿出主循环的异常把 finally 收尾块跳过了:\n' + out[-3000:]
    assert BID not in t._active_monitors, \
        'R4：收尾未清理代次登记（泄漏）: %r' % (t._active_monitors,)


CASES = [
    a1_active, a2_close_in_flight, a3_stale_pending_close,
    a4_frozen_manual_review, a5_frozen_cost_pending, a6_frozen_qty_conflict,
    a7_settled_finalizer_owns, a8_missing_batch, a9_inactive_batch,
    a10_unreadable_ledger_fail_closed, a11_read_only_zero_side_effect,
    b1_active_resumes_without_monitor_error,
    b2_close_in_flight_resumes,
    b3_stale_pending_close_fill_lagging,
    c1_exhaust_keeps_state_and_blocks_auto_clear,
    c2_control_unexhausted_retries_still_clears,
    c3_exhaust_keeps_effective_sl_and_position,
    c4_classify_still_runs_when_exhausted,
    c5_retry_gates_present,
    c6_exhaust_but_ledger_write_fails_still_blocks_clear,
    c7_direct_clear_blocked_by_inprocess_latch,
    c8_hot_collect_books_bound_entry_fill,
    c9_persist_guard_never_writes_misaligned_arrays,
    c10_guard_binding_race_no_loss,
    c11_adoption_keeps_recorded_fee_once,
    c12_skeleton_plan_qty_never_overwrites_ledger_binding,
    c13_prefix_keeps_fill_and_tail_qty_from_ledger,
    c14_plain_save_keeps_ledger_binding,
    c15_chain_shrink_optout_still_allowed,
    c16_missing_filled_details_selfheals,
    c17_missing_target_amounts_rejects_with_alert,
    c18_shrink_then_rebuild_restores_binding,
    c19_ta_misalign_blocks_all_chain_writes,
    c20_lifecycle_per_read_corruption_verdict,
    c21_g2_zero_settlement_guard_per_read,
    c22_terminal_evidence_never_true_on_unreadable_ledger,
    c23_persist_gate_uses_per_read_corruption,
    c24_clear_unreadable_uses_per_read_triple,
    c25_filled_layer_missing_price_guard_and_be,
    c26_tp_refuses_when_cost_not_ready,
    c27_save_protection_block_exception_is_fail_closed,
    r4a_single_outer_finally_replaces_reraise,
    r4b_handler_base_exception_still_runs_finally_cleanup,
    z0_zero_egress_overall,
]


def main():
    ok, failed = 0, []
    try:
        for fn in CASES:
            try:
                fn()
                ok += 1
                print('✅ %s' % fn.__name__)
            except Exception as e:
                failed.append(fn.__name__)
                print('❌ %s: %s' % (fn.__name__, e))
                traceback.print_exc()
    finally:
        _tw.uninstall()                  # 还原解释器状态（修复 4 校准项）
    print('=' * 74)
    if failed:
        print('RED: %d/%d  失败=%s' % (ok, len(CASES), failed))
        return 1
    print('GREEN: %d/%d' % (ok, len(CASES)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
