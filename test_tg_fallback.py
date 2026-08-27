# -*- coding: utf-8 -*-
"""
TG 通知 Markdown 降级机制离线验收测试（不连真实 Telegram、不连交易所）

背景（10:33 生产实测发现）：批次号 batch_20260819_103231_8dd23c 含 3 个下划线（奇数），
Telegram parse_mode='Markdown' 解析失败整条丢弃 → SG2 拒绝通知 Fail-Silent（违反不变量⑧）。

修复设计：send_tg_notification 捕获 telegram.error.BadRequest（类型判定，非字符串匹配）
→ 同一消息降级纯文本重发一次 → 纯文本也失败只记录，绝不影响交易控制流。

覆盖场景（ChatGPT 验收标准）：
  1: 正常 Markdown 文本            -> 1 次发送，Markdown 模式
  2: 含奇数下划线（批次号）         -> Markdown 失败 -> 自动纯文本重发成功
  3: 两次发送都失败                -> 只记录异常，不向上抛出
  4: execute_signal 集成            -> SG2 拒绝仍 return None + 零 create_order + 纯文本通知送达

用法: .venv\\Scripts\\python.exe test_tg_fallback.py
"""
import asyncio
import sys
import threading

from telegram.error import BadRequest

from trader_260725 import CryptoTrader

SYMBOL = "BTC/USDT:USDT"
RESULTS = []


def report(name, passed, detail=""):
    RESULTS.append((name, passed))
    print(f"\n{'=' * 60}\n[{'PASS' if passed else 'FAIL'}] {name} {detail}\n{'=' * 60}")


# 后台事件循环（send_tg_notification 用 run_coroutine_threadsafe 提交）
LOOP = asyncio.new_event_loop()
threading.Thread(target=LOOP.run_forever, daemon=True).start()


class FakeTgBot:
    """模拟 Telegram：Markdown 模式下文本含奇数个下划线 -> BadRequest（真实行为）；
    always_fail 模式下任何发送都失败"""

    def __init__(self, always_fail=False):
        self.sent = []          # (text, parse_mode)
        self.always_fail = always_fail

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        if self.always_fail:
            raise BadRequest("模拟：发送通道不可用")
        if parse_mode == 'Markdown' and text.count('_') % 2 == 1:
            raise BadRequest("Can't parse entities: can't find end of the entity")
        self.sent.append((text, parse_mode))


class NotifyFake:
    def __init__(self, tg_bot):
        self.tg_bot = tg_bot
        self.chat_id = 12345
        self.loop = LOOP

    def _send_email_alert(self, text, subject=""):
        pass  # critical 分支兜底 stub


def notify(fake, text, level='warning'):
    CryptoTrader.send_tg_notification(fake, text, level=level)


def scenario_1():
    """1: 正常 Markdown 文本 -> 1 次发送，Markdown 模式"""
    bot = FakeTgBot()
    notify(NotifyFake(bot), "✅ 测试通知：熔断已解除")
    ok = len(bot.sent) == 1 and bot.sent[0][1] == 'Markdown'
    report("场景1: 正常Markdown -> 1次发送", ok, f"(发送: {len(bot.sent)}, 模式: {bot.sent[0][1] if bot.sent else None})")


def scenario_2():
    """2: 奇数下划线批次号 -> Markdown 失败 -> 纯文本重发成功"""
    bot = FakeTgBot()
    text = "⚠️【加仓信号被拒】批次 batch_20260819_103231_8dd23c (BTC/USDT:USDT)\n原因: 存在未归属仓位"
    notify(NotifyFake(bot), text, level='warning')
    ok = (len(bot.sent) == 1                       # Markdown 失败未入列，纯文本成功入列
          and bot.sent[0][1] is None               # 降级为纯文本
          and text in bot.sent[0][0])              # 同一消息（warning 级会加前缀，用包含判定）
    report("场景2: 下划线批次号 -> 纯文本重发成功", ok,
           f"(发送: {len(bot.sent)}, 模式: {bot.sent[0][1] if bot.sent else None})")


def scenario_3():
    """3: 两次都失败 -> 只记录，不抛异常"""
    bot = FakeTgBot(always_fail=True)
    raised = None
    try:
        notify(NotifyFake(bot), "🚨 任何消息", level='warning')
    except Exception as e:
        raised = e
    ok = raised is None and len(bot.sent) == 0
    report("场景3: 两次失败 -> 不抛异常不影响控制流", ok, f"(异常: {raised!r})")


def scenario_4():
    """4: execute_signal 集成 -> SG2 拒绝 return None + 零下单 + 纯文本通知送达"""
    bot = FakeTgBot()
    states = {SYMBOL: {'b1': {'is_active': True, 'last_filled_count': 0,
                              'target_amounts': [0.01], 'current_sl_id': None}}}

    class Exchange:
        def __init__(self):
            self.create_order_calls = 0
            self.set_leverage_calls = 0
        def fetch_open_orders(self, symbol):
            return []
        def set_leverage(self, lv, symbol):
            self.set_leverage_calls += 1
        def fetch_ticker(self, symbol):
            raise RuntimeError("不应到达此处")
        def create_order(self, **kw):
            self.create_order_calls += 1
            return {'id': 'x'}

    ex = Exchange()
    fake = NotifyFake(bot)
    fake._ready = True
    fake._not_ready_reason = ""
    fake.load_all_states = lambda: states
    fake._check_existing_conflicts = lambda s, b, a: False
    fake._get_current_position_amt = lambda *a, **k: 0.03      # delta=+0.03 手工仓
    fake._safe_api_call = lambda fn, *a, **k: fn(*a, **k)
    fake._check_sl_coverage = lambda sym, st, pos: CryptoTrader._check_sl_coverage(fake, sym, st, pos)
    # D-006（2026-08-28）：绑定真实账户风控闸门三件套（execute_signal 新前置依赖，防假回归）
    fake._check_account_risk = lambda st, sig, stats_file=None: CryptoTrader._check_account_risk(fake, st, sig, stats_file)
    fake._count_active_batches = lambda st: CryptoTrader._count_active_batches(fake, st)
    fake._get_today_realized_pnl = lambda stats_file=None: CryptoTrader._get_today_realized_pnl(fake, stats_file)
    fake.send_tg_notification = lambda text, **k: CryptoTrader.send_tg_notification(fake, text, **k)
    fake.exchange = ex

    class Sig:
        symbol = SYMBOL
        batch_id = "batch_20260819_103231_8dd23c"   # 3 个下划线
        side = "BUY"
        leverage = 20

    ret = CryptoTrader.execute_signal(fake, Sig())
    ok = (ret is None
          and ex.create_order_calls == 0        # 硬断言：零下单
          and ex.set_leverage_calls == 0
          and len(bot.sent) == 1 and bot.sent[0][1] is None)  # 纯文本通知送达
    report("场景4: SG2拒绝+零下单+纯文本通知送达", ok,
           f"(返回: {ret!r}, 下单: {ex.create_order_calls}, 通知: {len(bot.sent)}, 模式: {bot.sent[0][1] if bot.sent else None})")


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
    print("TG 通知降级机制验收完成")
