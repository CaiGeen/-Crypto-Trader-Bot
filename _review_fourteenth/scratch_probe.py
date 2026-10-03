# -*- coding: utf-8 -*-
"""探针：能否用同一个 PR 替身既驱动监控轮询（降级→恢复），又跑真实 execute_signal
统计 create_order 次数。用于给 B35 定点反例选型，不是交付物。"""
import os
import sys
import tempfile
import traceback

BASE = r"G:\my-crypto-bot-wt"
os.chdir(BASE)
sys.path.insert(0, BASE)

import test_poll_degradation as T            # noqa: E402
import test_monitor_poll_recovery as PR      # noqa: E402
import trader_260725                         # noqa: E402
from trader_260725 import CryptoTrader       # noqa: E402

d = tempfile.mkdtemp(prefix='probe_')
sp = os.path.join(d, 'trade_state.json')
trader_260725.STATE_FILE = sp
try:
    PR._seed(sp)
    fake = PR._make_fake(sp, PR._disk(sp))
    T._init_poll_tracking(fake)
    fake._get_today_realized_pnl = lambda *a, **k: 0.0
    # monitor 替身跑 execute_signal 所需返回值桩（与 _EntryFake 同款语义）
    fake._check_account_risk = lambda *a, **k: (True, 'ok')
    fake._count_active_batches = lambda *a, **k: 0
    fake._compute_signal_fingerprint = lambda *a, **k: 'fp-b35'
    fake._check_existing_conflicts = lambda *a, **k: False
    fake._validate_stop_losses = lambda *a, **k: (True, 'ok')
    fake._validate_take_profit = lambda *a, **k: (True, 'ok')
    fake._start_monitoring = lambda *a, **k: None
    ex = fake.exchange
    ex.fetch_balance.return_value = {'USDT': {'free': 10000.0}}
    ex.fetch_ticker.return_value = {'last': 76500.0, 'close': 76500.0}
    ex.fapiPrivateGetPositionSideDual.return_value = {'dualSidePosition': True}
    ex.set_leverage.return_value = {}
    created = []
    ex.create_order.side_effect = lambda **k: created.append(k) or {'id': 'x', 'status': 'open'}

    # ① 连续失败 → 降级
    ex.fetch_open_orders.side_effect = RuntimeError('持续失败')
    PR._drive(fake, None, max_rounds=3)
    print('① 失败后 degraded =', sorted(fake._poll_degraded_batches))

    # ② 订单查询恢复、持仓 UNKNOWN(None)
    ex.fetch_open_orders.side_effect = None
    ex.fetch_open_orders.return_value = [{'id': T.ENTRY_ID, 'status': 'open'}]
    fake._get_current_position_amt = lambda *a, **k: None
    n, err = PR._drive(fake, None, max_rounds=2)
    print('② UNKNOWN 轮后 degraded =', sorted(fake._poll_degraded_batches),
          'streak =', fake._poll_fail_streak, 'err =', err)

    # ③ 此时实际发起新 ENTRY
    # ③ 信号时刻持仓查询已恢复（transient 失败已过），但监控尚未用已知持仓重新核实
    fake._get_current_position_amt = lambda *a, **k: 0.0
    try:
        ret = CryptoTrader.execute_signal(fake, T._FakeSignal())
    except Exception:
        traceback.print_exc()
        ret = 'EXC'
    print('③ execute_signal ret =', repr(ret), 'create 调用 =', len(created),
          'degraded =', sorted(fake._poll_degraded_batches))

    # ④ 阳性对照：清闸后再来（证明 ③ 不是替身根本下不了单）
    fake._poll_degraded_batches.clear()
    try:
        ret2 = CryptoTrader.execute_signal(fake, T._FakeSignal())
    except Exception:
        traceback.print_exc()
        ret2 = 'EXC'
    print('④ 清闸后 ret =', repr(ret2), 'create 调用 =', len(created))
finally:
    trader_260725.STATE_FILE = PR._real_state_file
