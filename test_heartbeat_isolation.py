# -*- coding: utf-8 -*-
"""验收 E：心跳写盘失败就地隔离（事故修复 2026-10-06）。

背景（实盘事故，已取证）
================================================================
`health_progress._atomic_write` 的 `os.replace` 抛 PermissionError
→ 逃出 `_start_monitoring` 主循环（候选 :9582 / :11811 两处 `write_progress`）
→ 监控线程死亡 → finally 清理 → converge 撤单 + clear（4 张入场单、墓碑）。

修复形状（转审第三轮边界 3：只恢复已明确分类的路径）
================================================================
  * 只隔离 `OSError`（含 PermissionError），其余异常照常外抛；
  * 失败不刷新成功心跳、不伪造健康；**不写 `monitor_error`**、不阻塞轮询；
  * 首报一次（走**非阻塞事件队列**，零网络）+ 每 300s 限频提醒 + 恢复通知；
  * 不调用 `recover_active_batches`、不改全局 `monitor_error` 语义。

用例
================================================================
  E1 持续 PermissionError → 隔离、返回 False、首报打印 + 事件入队，不外抛
  E2 限频 → 300s 内不重复打印；超过 300s 才提醒
  E3 恢复 → 返回 True、状态清空、打印恢复通知
  E4 **降级通知本身失败** → 依然不外抛（告警通道不是第二个杀监控入口）
  E5 非 OSError（ValueError）→ **照常外抛**（不把未知异常变成自动重试）
  E6 集成：真实 `_start_monitoring` 在心跳持续失败下**监控继续**——
     不出现「监控循环内部异常」、不写 monitor_error，且首报可见、线程正常退出

运行：G:\my-crypto-bot\.venv\Scripts\python.exe test_heartbeat_isolation.py
预期：GREEN: 6/6，退出码 0。零网络、零外发、不碰生产账本。
"""
import contextlib
import hashlib
import io
import json
import os
import sys
import tempfile
import threading
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import trader_260725
from trader_260725 import CryptoTrader

SYM, BID = 'BTCUSDT', 'batch_hb'
SRC_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        'trader_260725.py')
RESULTS = []

_PROD_FILES = ['trade_state.json', 'trade_tombstones.json', 'trade_stats.json',
               'auth_blocked.json', 'signal.json', 'signal_dedup.json']
_PROD_DIR = os.path.dirname(os.path.abspath(__file__))


def _prod_snapshot():
    snap = {}
    for _n in _PROD_FILES:
        _p = os.path.join(_PROD_DIR, _n)
        try:
            with open(_p, 'rb') as _f:
                _d = _f.read()
            snap[_n] = (hashlib.sha256(_d).hexdigest(), len(_d),
                        os.stat(_p).st_mtime_ns)
        except FileNotFoundError:
            snap[_n] = ('<missing>', 0, 0)
    return snap


_SNAP0 = _prod_snapshot()


def check(name, passed, detail=''):
    RESULTS.append((name, bool(passed)))
    print('  [%s] %s' % ('PASS' if passed else 'FAIL', name)
          + (('\n        → ' + str(detail)) if detail and not passed else ''))
    return bool(passed)


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
            status='open', filled=0.0, side='sell'):
        o = {'id': oid, 'status': status, 'filled': filled, 'amount': amount,
             'type': otype, 'stopPrice': stop, 'side': side,
             'average': stop, 'price': stop}
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

    def price_to_precision(self, symbol, price):
        return price

    def create_order(self, symbol, otype, side, amount, price=None,
                     params=None, **k):
        nid = 'N%d' % (len(self.create_calls) + 1)
        self.create_calls.append((otype, side, float(amount)))
        stop = float((params or {}).get('stopPrice') or 0)
        o = self._mk(nid, otype=otype, amount=float(amount), stop=stop, side=side)
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


def make_trader(tmp=None):
    tmp = str(tmp or tempfile.mkdtemp(prefix='hb_'))
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
    t.send_tg_notification = lambda text, **k: t.sent_tg.append(str(text))
    t.IP_CHECK_ENABLED = False
    t._get_public_ip = lambda: None
    t._send_email_alert = lambda *a, **k: False
    t.enqueued = []
    t._enqueue_notify_event = lambda kind, msg: (
        t.enqueued.append((kind, str(msg))) or 'EVT1')
    return t, ex


def _capture(fn):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        r = fn()
    return r, buf.getvalue()


def _boom(*a, **k):
    raise PermissionError("[WinError 5] 拒绝访问。: '.bot_health\\.x.tmp'")


# ───────────────────── E1 持续失败被隔离 ─────────────────────
def e1_persistent_permission_error_isolated():
    t, ex = make_trader()
    with mock.patch.object(trader_260725, 'write_progress', _boom):
        r, out = _capture(lambda: t._safe_write_progress('i1', BID, SYM))
    check('E1 失败返回 False 而非外抛', r is False, r)
    check('E1 首报打印「心跳降级」', '心跳降级' in out, out[:300])
    check('E1 首报进非阻塞事件队列（零网络）',
          any(k == 'heartbeat_degraded' for k, _ in t.enqueued), t.enqueued)
    check('E1 未写 monitor_error 标记（真 W1 文案）',
          '已写入 monitor_error 标记' not in out and
          '[W1]' not in out, out[:300])
    check('E1 失败状态已记录', BID in getattr(t, '_hp_fail_state', {}))


# ───────────────────── E2 限频 ─────────────────────
def e2_rate_limited_reminder():
    t, ex = make_trader()
    with mock.patch.object(trader_260725, 'write_progress', _boom):
        _capture(lambda: t._safe_write_progress('i1', BID, SYM))
        _, out2 = _capture(lambda: t._safe_write_progress('i1', BID, SYM))
        check('E2 300s 内不重复打印提醒', '持续失败' not in out2, out2[:300])
        t._hp_fail_state[BID]['last'] -= 301          # 模拟超过 300s
        _, out3 = _capture(lambda: t._safe_write_progress('i1', BID, SYM))
        check('E2 超过 300s 打印限频提醒', '持续失败' in out3 and '300s' in out3,
              out3[:300])
    check('E2 首报事件仅入队一次（不刷屏）',
          len(t.enqueued) == 1, t.enqueued)


# ───────────────────── E3 恢复通知 ─────────────────────
def e3_recovery_notice():
    t, ex = make_trader()
    with mock.patch.object(trader_260725, 'write_progress', _boom):
        _capture(lambda: t._safe_write_progress('i1', BID, SYM))
    r, out = _capture(lambda: t._safe_write_progress('i1', BID, SYM))
    check('E3 恢复后返回 True', r is True, r)
    check('E3 打印恢复通知', '心跳恢复' in out, out[:300])
    check('E3 失败状态已清空', BID not in getattr(t, '_hp_fail_state', {}))


# ───────────────────── E4 通知本身失败 ─────────────────────
def e4_notify_failure_still_isolated():
    t, ex = make_trader()

    def _bad_enqueue(*a, **k):
        raise RuntimeError('notify queue down')

    t._enqueue_notify_event = _bad_enqueue
    with mock.patch.object(trader_260725, 'write_progress', _boom):
        try:
            r, out = _capture(lambda: t._safe_write_progress('i1', BID, SYM))
        except Exception as e:                       # 通知异常若外抛即失败
            check('E4 通知失败不得外抛', False, '%s: %s' % (type(e).__name__, e))
            return
    check('E4 通知失败仍返回 False（被隔离）', r is False, r)
    check('E4 通知失败仍打印降级首报', '心跳降级' in out, out[:300])


def e4b_enqueue_returns_none():
    """E4b：`_enqueue_notify_event` **返回 None**（入队失败）与抛异常同效。

    转审点名：确认"通知入队返回 None"和"抛异常"都不会终止监控；
    并且"入队成功"绝不能被写成"通知已送达"（入队≠送达）。
    """
    t, ex = make_trader()
    t._enqueue_notify_event = lambda *a, **k: None      # 入队失败：返回 None
    with mock.patch.object(trader_260725, 'write_progress', _boom):
        try:
            r, out = _capture(lambda: t._safe_write_progress('i1', BID, SYM))
        except Exception as e:
            check('E4b 入队返回 None 不得外抛', False,
                  '%s: %s' % (type(e).__name__, e))
            return
    check('E4b 入队返回 None 仍被隔离（返回 False、不外抛）', r is False, r)
    check('E4b 返回 None 仍打印降级首报', '心跳降级' in out, out[:300])
    check('E4b 未把入队成功写成"已送达"（入队≠送达）',
          '已送达' not in out and '通知已发送' not in out
          and '发送成功' not in out, out[:300])
    check('E4b 失败状态仍被记录（可等恢复通知）',
          BID in getattr(t, '_hp_fail_state', {}))


# ───────────────────── E5 非 OSError 照常外抛 ─────────────────────
def e5_non_oserror_propagates():
    t, ex = make_trader()

    def _value_err(*a, **k):
        raise ValueError('not an I/O error')

    with mock.patch.object(trader_260725, 'write_progress', _value_err):
        try:
            t._safe_write_progress('i1', BID, SYM)
        except ValueError:
            check('E5 非 OSError 照常外抛（分类恢复边界）', True)
            return
        except Exception as e:
            check('E5 非 OSError 照常外抛（分类恢复边界）', False,
                  '被 %s 吞掉了' % type(e).__name__)
            return
    check('E5 非 OSError 照常外抛（分类恢复边界）', False, '未抛出任何异常')


# ───────────────────── E6 集成：监控不因心跳失败而死 ─────────────────────
def e6_monitor_survives_heartbeat_failure():
    t, ex = make_trader()
    st = {'checks': 0, 'hb': 0}

    def _wp_boom(*a, **k):
        st['hb'] += 1
        raise PermissionError("[WinError 5] 拒绝访问。: '.bot_health\\x.tmp'")

    def _lifecycle(_all, _b, **_k):
        # 心跳写入真正发生过（st['hb']>=1）之后才放行退出，
        # 保证被测的"心跳失败"路径确实先被执行到。
        st['checks'] += 1
        return 'exit' if (st['hb'] >= 1 or st['checks'] > 50) else 'ok'

    batch = {'is_active': True, 'symbol': SYM, 'side': 'BUY',
             'is_hedge_mode': True, 'entry_orders': ['E1'],
             'target_amounts': [1.0], 'filled_details': [0.0],
             'last_filled_count': 0, 'current_sl_id': None,
             'params_base': {}, 'batch_total_amount': 1.0,
             'protection_registry': {}}
    with open(trader_260725.STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump({SYM: {BID: batch}}, f, ensure_ascii=False)
    ex.positions = []
    # 让补查路径能拿到订单（否则 fetch_order -2011 → `continue`
    # 直接跳过本轮末尾的心跳写入，被测路径永远执行不到）
    ex._mk('E1', otype='LIMIT', amount=1.0, stop=76620.0, side='buy')
    ex.open_orders = [ex.orders['E1']]
    ex._mk('SL1', otype='STOP_MARKET', amount=1.0, stop=75001.0, side='sell')

    with mock.patch.object(trader_260725, 'write_progress', _wp_boom), \
            mock.patch.object(trader_260725, 'current_instance_id',
                              lambda: 'inst-hb-test'), \
            mock.patch.object(trader_260725.time, 'sleep', lambda s: None), \
            mock.patch.object(t, '_monitor_lifecycle_check', _lifecycle):
        th = threading.Thread(
            target=t._start_monitoring, args=(SYM, BID),
            kwargs=dict(entry_orders=['E1'], stop_steps=[75001.0],
                        take_profit_price=80000.0, current_sl_id=None,
                        tp_order_id=None, batch_total_amount=1.0,
                        target_amounts=[1.0], params_base={},
                        is_hedge_mode=True, side='BUY',
                        last_filled_count=0, filled_details=[0.0],
                        total_entry_fee=0.0),
            daemon=True)
        _, out = _capture(lambda: (th.start(), th.join(timeout=30)))

    with open(trader_260725.STATE_FILE, encoding='utf-8') as f:
        b = json.load(f).get(SYM, {}).get(BID) or {}
    check('E6 监控未因心跳失败而异常退出',
          '监控循环内部异常' not in out, out[:600])
    check('E6 未写 monitor_error 标记（真 W1 文案）',
          '已写入 monitor_error 标记' not in out and '[W1]' not in out, out[:600])
    check('E6 心跳降级首报可见', '心跳降级' in out, out[:600])
    check('E6 生命周期正常退出（线程结束）', not th.is_alive(),
          'thread alive=%s' % th.is_alive())
    check('E6 生命周期检查确实被调用（说明跑进了真实主循环）',
          st['checks'] >= 1, st)
    check('E6 心跳写入确实被调用过（被测路径已执行）',
          st['hb'] >= 1, st)


def main():
    print('== 验收 E：心跳写盘失败就地隔离 ==')
    e1_persistent_permission_error_isolated()
    e2_rate_limited_reminder()
    e3_recovery_notice()
    e4_notify_failure_still_isolated()
    e4b_enqueue_returns_none()
    e5_non_oserror_propagates()
    e6_monitor_survives_heartbeat_failure()
    dirty = [k for k in _PROD_FILES
             if _prod_snapshot().get(k) != _SNAP0.get(k)]
    check('T99 生产/候选账本免疫快照未变', not dirty, dirty)
    npass = sum(1 for _, ok in RESULTS if ok)
    print('\nGREEN: %d/%d' % (npass, len(RESULTS)))
    return 0 if npass == len(RESULTS) else 1


if __name__ == '__main__':
    sys.exit(main())
