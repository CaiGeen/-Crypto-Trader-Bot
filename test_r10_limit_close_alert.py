# -*- coding: utf-8 -*-
"""
C2/R10 限价平仓监控异常退出告警离线验收测试（不连交易所/Telegram）

契约（ChatGPT 锁定）：
  1: 异常退出 -> critical TG 发送，消息含 batch_id / order_id
  2: 异常退出 -> 绝不写 monitor_error 标记（该标记语义=重启时跳过恢复并清理批次；
     限价线程死亡时主监控仍健在，照抄会导致健康批次被误清——R11 模式）
  3: 正常退出（批次消亡）-> 零告警（不增加噪音）
  4: critical TG 自身抛异常 -> 不得再次抛出（真因不得被 TG 异常覆盖）

用法: .venv\\Scripts\\python.exe test_r10_limit_close_alert.py
"""
import sys
from unittest import mock

import trader_260725
from trader_260725 import CryptoTrader

SYMBOL = "BTC/USDT:USDT"
BATCH = "batch_r10_001"
ORDER = "limit_ord_001"
RESULTS = []


def report(name, passed, detail=""):
    RESULTS.append((name, passed))
    print(f"\n{'=' * 60}\n[{'PASS' if passed else 'FAIL'}] {name} {detail}\n{'=' * 60}")


class FakeExchange:
    def fetch_order(self, order_id, symbol, **kw):
        return {'status': 'closed', 'average': 100.0}  # 触发结算路径


def make_fake(states, tg_raises=False, pnl_raises=False):
    fake = mock.MagicMock()
    fake.load_all_states = lambda: states
    fake._safe_api_call = lambda fn, *a, **k: fn(*a, **k)
    fake.exchange = FakeExchange()
    fake.sent = []       # (level, text)
    fake.saved = []      # save_batch_state 记录

    def _tg(text, **kw):
        if tg_raises:
            raise RuntimeError("模拟 TG 通道故障")
        fake.sent.append((kw.get('level', 'info'), text))

    fake.send_tg_notification = _tg
    fake.save_batch_state = lambda s, b, d: fake.saved.append(dict(d))

    def _pnl(*a, **k):
        if pnl_raises:
            raise RuntimeError("模拟结算内部异常")  # 触发外层 except
        return None

    fake._record_realized_pnl = _pnl
    fake._notify_snapshot = lambda *a, **k: None
    return fake


ALIVE_STATES = {SYMBOL: {BATCH: {'is_active': True, 'current_sl_id': None}}}


def run(fake):
    with mock.patch.object(trader_260725.time, 'sleep'):
        CryptoTrader._monitor_limit_close(
            fake, SYMBOL, BATCH, ORDER, 0.01, 100.0, 0.0, 'BUY', 1, [0.01], [100.0])


def scenario_1():
    """1: 异常退出 -> critical TG 含 batch_id / order_id"""
    fake = make_fake(ALIVE_STATES, pnl_raises=True)
    run(fake)
    crits = [t for lv, t in fake.sent if lv == 'critical' and BATCH in t and ORDER in t]
    report("场景1: 异常退出发 critical TG(含批次/订单号)", len(crits) == 1,
           f"(critical 匹配: {len(crits)})")


def scenario_2():
    """2: 异常退出 -> 绝不写 monitor_error"""
    fake = make_fake(ALIVE_STATES, pnl_raises=True)
    run(fake)
    bad = [d for d in fake.saved if d.get('monitor_error')]
    report("场景2: 异常退出零 monitor_error 写入", not bad,
           f"(含 monitor_error 的保存: {len(bad)})")


def scenario_3():
    """3: 正常退出（批次消亡防幽灵）-> 零 critical"""
    ghost_states = {SYMBOL: {}}   # 批次已不存在
    fake = make_fake(ghost_states)
    run(fake)
    crits = [t for lv, t in fake.sent if lv == 'critical']
    report("场景3: 批次消亡正常退出零告警", len(crits) == 0,
           f"(critical: {len(crits)}, 全部消息: {len(fake.sent)})")


def scenario_4():
    """4: TG 自身故障 -> 异常处理不得再次抛出（真因不被覆盖）"""
    fake = make_fake(ALIVE_STATES, tg_raises=True, pnl_raises=False)
    # 结算卡片 TG(L3768 无包裹)抛异常 -> 外层 except -> 新告警代码再调 TG 再抛 -> 必须吞掉
    raised = None
    try:
        run(fake)
    except Exception as e:
        raised = e
    report("场景4: TG 故障不二次抛出", raised is None, f"(异常: {raised!r})")


if __name__ == '__main__':
    scenario_1()
    scenario_2()
    scenario_3()
    scenario_4()
    print("\n" + "#" * 60)
    failed = [n for n, p in RESULTS if not p]
    if failed:
        print(f"❌ {len(failed)}/{len(RESULTS)} 个场景失败: {failed}")
        sys.exit(1)
    print(f"✅ 全部 {len(RESULTS)} 个场景通过")
    print("C2/R10 限价平仓监控告警验收完成")
