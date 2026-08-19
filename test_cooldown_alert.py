# -*- coding: utf-8 -*-
"""
B1/R1 熔断告警状态机离线竞态测试（不连交易所、不碰真实状态文件、不影响运行中的 Bot）

覆盖 4 个场景（ChatGPT 验收标准）：
  1: 同一熔断周期多次 429        -> 仅 1 条进入 critical（不重复告警）
  2: 多线程同时解除              -> 仅 1 条解除通知
  3: 解除竞争期间出现新熔断       -> 不发送错误的"已解除"（generation 校验）
  4: 解除后再次熔断              -> 重新发送 1 条进入 critical（周期可重启）

用法: .venv\\Scripts\\python.exe test_cooldown_alert.py
"""
import sys
import threading
import time
from unittest import mock

import trader_260725
from trader_260725 import CryptoTrader

SYMBOL = "BTC/USDT:USDT"
RESULTS = []


def report(name, passed, detail=""):
    RESULTS.append((name, passed))
    print(f"\n{'=' * 60}\n[{'PASS' if passed else 'FAIL'}] {name} {detail}\n{'=' * 60}")


def make_fake_self():
    """最小 fake self：只提供 R1 熔断告警所需的属性/方法"""
    fake = mock.MagicMock()
    fake.api_cooldown_lock = threading.Lock()
    fake.api_cooldown_until = 0.0
    fake._cooldown_active = False
    fake._cooldown_gen = 0
    fake.sent = []  # (level, text) 记录所有告警
    fake.load_all_states = lambda: {SYMBOL: {'batch_001': {'is_active': True}}}

    def _record_tg(text, reply_markup=None, level='info'):
        fake.sent.append((level, text))

    fake.send_tg_notification = _record_tg
    return fake


def count_alerts(fake):
    entry = [t for lv, t in fake.sent if lv == 'critical' and '熔断已触发' in t]
    recover = [t for lv, t in fake.sent if '熔断已解除' in t]
    return entry, recover


class InjectingLock:
    """竞态注入锁：当观察到 'active 已翻 False 且 gen 仍是 1' 的第一次获取时，
    在获取真实锁之前注入一个新 429 熔断周期（模拟 T2 恰在 T1 二次确认前进入）。
    注入点不持有真实锁，_alert_cooldown_start 可正常加锁，无死锁。"""

    def __init__(self, real_lock, fake):
        self.real = real_lock
        self.fake = fake
        self.injected = False

    def __enter__(self):
        if (not self.injected and self.fake._cooldown_gen == 1
                and self.fake._cooldown_active is False):
            self.injected = True  # 先置位防递归重入
            CryptoTrader._alert_cooldown_start(self.fake, "429 限频", 30.0)
        return self.real.__enter__()

    def __exit__(self, *args):
        return self.real.__exit__(*args)


def scenario_1():
    """1: 同一熔断周期多次 429 -> 仅 1 条进入 critical"""
    fake = make_fake_self()
    CryptoTrader._alert_cooldown_start(fake, "429 限频", 45.0)
    CryptoTrader._alert_cooldown_start(fake, "429 限频", 60.0)  # 延长冷却，不重复告警
    entry, _ = count_alerts(fake)
    ok = (len(entry) == 1 and fake._cooldown_gen == 1 and fake._cooldown_active is True
          and '受影响活跃批次: 1 个' in entry[0])  # 计数正确（结构 {symbol:{batch_id:...}}）
    report("场景1: 同一周期多次429 -> 仅1条进入告警", ok,
           f"(critical进入: {len(entry)}, gen: {fake._cooldown_gen})")


def scenario_2():
    """2: 多线程同时解除 -> 仅 1 条解除通知"""
    fake = make_fake_self()
    CryptoTrader._alert_cooldown_start(fake, "429 限频", 0.05)
    fake.api_cooldown_until = time.time() + 0.05

    threads = [threading.Thread(target=CryptoTrader._wait_for_api_cooldown, args=(fake,))
               for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    _, recover = count_alerts(fake)
    ok = len(recover) == 1 and fake._cooldown_active is False
    report("场景2: 8线程同时解除 -> 仅1条解除通知", ok,
           f"(解除通知: {len(recover)}, active: {fake._cooldown_active})")


def scenario_3():
    """3: 解除竞争期间出现新熔断 -> 不发送错误的'已解除'（generation 校验）"""
    fake = make_fake_self()
    CryptoTrader._alert_cooldown_start(fake, "429 限频", 0.05)
    fake.api_cooldown_until = time.time() + 0.05

    injecting = InjectingLock(threading.Lock(), fake)
    fake.api_cooldown_lock = injecting

    CryptoTrader._wait_for_api_cooldown(fake)  # 单线程走完整解除路径，注入点在二次确认前

    entry, recover = count_alerts(fake)
    ok = (len(recover) == 0                # 没有错误的解除通知
          and len(entry) == 2               # gen1 进入 + 注入的 gen2 进入
          and fake._cooldown_gen == 2       # 新周期确实开始
          and fake._cooldown_active is True
          and injecting.injected)           # 注入确实发生了（测试有效性）
    report("场景3: 解除瞬间新429 -> 不发错误解除", ok,
           f"(解除: {len(recover)}, 进入: {len(entry)}, gen: {fake._cooldown_gen}, 注入: {injecting.injected})")


def scenario_4():
    """4: 解除后再次熔断 -> 重新发送 1 条进入 critical（周期可重启）"""
    fake = make_fake_self()
    CryptoTrader._alert_cooldown_start(fake, "429 限频", 0.05)
    fake.api_cooldown_until = time.time() + 0.05
    CryptoTrader._wait_for_api_cooldown(fake)  # 完整走完：进入 -> 解除

    CryptoTrader._alert_cooldown_start(fake, "IP 被 Binance 封禁(418/banned)", 300.0)  # 再熔断

    entry, recover = count_alerts(fake)
    ok = (len(entry) == 2 and len(recover) == 1
          and fake._cooldown_gen == 2 and fake._cooldown_active is True)
    report("场景4: 解除后再次熔断 -> 再发1条进入", ok,
           f"(进入: {len(entry)}, 解除: {len(recover)}, gen: {fake._cooldown_gen})")


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
    print("B1/R1 熔断告警状态机竞态验收完成")
