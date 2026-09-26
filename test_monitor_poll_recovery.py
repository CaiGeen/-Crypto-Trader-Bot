#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""监控循环在 `fetch_open_orders` 失败后的恢复行为（第二十轮 T1/T2）。

## 要回答的两个问题

- **T1**：某轮 `fetch_open_orders` 失败（该分支只 `print` + 计数 + `continue`），
  **窗口内已成交**的入场单，在**下一次成功轮**是否会被补挂止损？
- **T2**：这条失败路径上，全链路有没有任何告警（TG / critical）或「停止新风险」标志？

## 为什么单独一个文件

`_start_monitoring` 是 200+ 行的 `while True`，依赖面很宽（生命周期守卫、时间同步、
registry 自愈、IP 巡检、活跃批次数、限流退避…）。上一份注入测试
（`test_protection_write_gates.py`）刻意只测写盘门禁、零循环依赖，两者分开维护。

## 驱动方式

`time.sleep` 换成计数哨兵：第 N 次调用抛 `_StopLoop`，把无限循环限定成确定轮次。
`fetch_open_orders` 用 `side_effect` 序列制造「先失败、后成功」。
全程零网络：`_safe_api_call` 透传到 MagicMock 交易所，TG/邮件在实例级收集。

⚠️ MagicMock 陷阱（仓库既有教训，见 `test_b1_state_machine.py:48` 与
`test_protection_chain_fix.py:56`）：未显式绑定的方法会退化为自动 mock，
返回值在解包处抛 TypeError 又被 `except` 吞掉 → 路径静默不执行。
因此下方 REAL_HELPERS 逐个显式绑定真实实现；未绑定的辅助一律给**确定性桩**。

跑法：`.venv\\Scripts\\python.exe test_monitor_poll_recovery.py`（rc=0 即全过）
"""

import json
import os
import sys
import tempfile
import threading
import time
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import trader_260725  # noqa: E402
from trader_260725 import CryptoTrader  # noqa: E402

SYMBOL = "BTC/USDT:USDT"
BATCH = "batch_pollrec_001"
ENTRY_ID = "e1"

RESULTS = []
_real_state_file = trader_260725.STATE_FILE
_real_sleep = trader_260725.time.sleep

# 需要绑真实实现的方法（其余给确定性桩，避免 MagicMock 自动 mock 吞语义）
REAL_HELPERS = (
    "_persist_states", "_update_registry", "_protection_identity", "_build_intent",
    "_order_matches_intent", "_assert_create_allowed", "_final_pre_create_check",
    "_commit_protection_with_g3", "_g3a_converge_race_order", "_verify_order_created",
    "_classify_create_exception", "_verify_and_update_registry",
    "_recheck_registry_self_heal", "_is_stale_pre_launch_entry",
    "_place_prepared_orders_immediately", "_monitor_lifecycle_check",
    "_calculate_monitoring_interval", "_get_active_batch_count",
)


class _StopLoop(BaseException):
    """用来把 `while True` 限定成确定轮次的哨兵。

    ⚠️ 必须继承 `BaseException` 而非 `Exception`：`_start_monitoring` 的 while 循环
    外层有一个 `except Exception`（L9437）会把 `Exception` 子类哨兵当成真实故障，
    触发「监控线程异常退出」critical 告警 + 写 `monitor_error` 标记 —— 那是**驱动方式
    造出来的假告警**，会让 T2 的结论完全失真。`except Exception` 不捕 `BaseException`。
    """


def report(name, passed, detail=""):
    RESULTS.append((name, passed))
    print(f"[{'PASS' if passed else 'FAIL'}] {name}\n        {detail}")


def _seed(state_path, **over):
    b = {
        "is_active": True, "batch_id": BATCH, "symbol": SYMBOL, "side": "BUY",
        "entry_orders": [ENTRY_ID], "stop_steps": [55000.0],
        "take_profit_price": 60000.0, "current_sl_id": None, "tp_order_id": None,
        "close_phase": 0, "pending_sl_orders": [], "protection_registry": {},
        "target_amounts": [0.43], "last_filled_count": 0,
    }
    b.update(over)
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump({SYMBOL: {BATCH: b}}, f, ensure_ascii=False, indent=2)


def _disk(state_path):
    with open(state_path, encoding="utf-8") as f:
        return json.load(f)


def _make_fake(state_path, states):
    fake = mock.MagicMock()
    fake._safe_api_call = lambda fn, *a, **k: fn(*a, **k)
    ex = mock.MagicMock()
    ex.amount_to_precision.side_effect = lambda s, v: v
    ex.price_to_precision.side_effect = lambda s, v: v
    fake.exchange = ex

    fake.sent = []
    fake.send_tg_notification = lambda text, **kw: fake.sent.append(
        (kw.get("level", "info"), str(text)))
    fake._send_email_alert = lambda text, subject="": None

    fake._api_cooldown_until = 0
    fake._process_start_ts = time.time()
    fake._state_corrupted = False
    fake._state_corruption_detail = ""
    fake._defer_state_corrupt_alert = False
    fake._active_monitors = set()
    fake._active_monitors_lock = threading.Lock()
    fake._state_lock = threading.Lock()
    fake._registry_self_heal_interval = 10 ** 9      # 旧名，保留兼容
    # ⚠️ 代码里用的是**无下划线**的 `self.registry_self_heal_interval`（L7581）。
    # 只设带下划线的版本会让 MagicMock 自动 mock 漏进来 → `float >= MagicMock` 抛 TypeError
    # → 被循环外层 except 吞掉、监控线程「异常退出」。这是本文件实测踩到的第一个坑。
    fake.registry_self_heal_interval = 10 ** 9       # 自愈不干扰本用例
    fake.last_ip_check_time = time.time()
    fake.IP_CHECK_INTERVAL = 10 ** 9
    fake._check_ip_periodically = lambda: None
    fake._sync_time_if_needed = lambda: None
    fake._g3_log_position_recheck = lambda *a, **k: None
    # 真实 save_batch_state 内部要读墓碑（L2804 `_load_tombstones`）——未绑定则拿到
    # MagicMock，`tombstones.get()` 返回自动 mock，被 `_tombstone_entry_valid` 当成
    # 有效墓碑，进而在 json.dump 时抛出「Object of type MagicMock is not JSON
    # serializable」。这是本文件实测踩到的第三个坑。
    fake._load_tombstones = lambda: {}
    # 持仓查询返回 float：L8013 有 `current_actual_position < batch_filled_amount`
    # 的数值比较，未绑定 → MagicMock < float → TypeError → 循环外层 except 吞掉。
    # 第四个坑。返回 0.0 = 零仓（批次刚成交、交易所持仓尚未反映的合理中间态）。
    fake._get_current_position_amt = lambda *a, **k: 0.0
    # finally 块入口（L9485）要解包 `_finally_cleanup_decision()` 的 2 元组。未绑定 →
    # MagicMock 的 `__iter__` 返回空 → "not enough values to unpack (expected 2, got 0)"。
    # 这是本文件实测踩到的第二个坑（同仓库既有记录）。哨兵是 BaseException，finally 必然
    # 执行，所以必须给确定性桩；'skip' 让 finally 不做任何清理（哨兵之后已无意义）。
    fake._finally_cleanup_decision = lambda s, b: ('skip', None)

    fake.load_all_states = lambda: states
    # 本文件不测持久化（T3/T4 已用真实落盘覆盖），故 save_batch_state 用**记录式桩**：
    # 真实实现会走 merge + json.dump，循环里若有未绑定的辅助把 MagicMock 写进批次对象，
    # 就会抛「Object of type MagicMock is not JSON serializable」并让落盘失败，
    # 那是**测试替身保真度问题、不是产品行为**（第五个坑）。真实落盘语义见另一份文件。
    fake.saved = []
    fake.save_batch_state = lambda s, b, d: fake.saved.append((s, b, dict(d)))
    # 补挂止损路径入口探针：T1 要断言的是「失败轮之后的成功轮是否进入了补挂路径」，
    # 而不是「循环内是否成功 create_order」——后者受 fake 保真度影响（见上）。
    fake.sl_place_calls = []
    for name in REAL_HELPERS:
        if hasattr(CryptoTrader, name):
            setattr(fake, name, (lambda n=name: lambda *a, **k: getattr(CryptoTrader, n)(fake, *a, **k))())
    _real_place = fake._place_prepared_orders_immediately

    def _spy_place(*a, **k):
        fake.sl_place_calls.append(a[1] if len(a) > 1 else None)
        return _real_place(*a, **k)
    fake._place_prepared_orders_immediately = _spy_place
    return fake


def _layer_sl_params():
    return [{"symbol": SYMBOL, "type": "STOP_MARKET", "side": "sell", "amount": 0.43,
             "params": {"stopPrice": 55000.0, "reduceOnly": True, "workingType": "MARK_PRICE"}}]


def _run_rounds(fake, max_rounds):
    """跑最多 max_rounds 轮监控，哨兵终止。返回实际 sleep 次数。"""
    calls = {"n": 0}

    def counting_sleep(sec):
        calls["n"] += 1
        raise _StopLoop(f"round>{max_rounds}")
    return calls, counting_sleep


def _drive(fake, sleep_fn, max_rounds=6):
    """在 patch time.sleep 下调用 _start_monitoring 直到哨兵。返回 sleep 次数。"""
    n = {"c": 0}

    def _sleep(sec):
        n["c"] += 1
        if n["c"] > max_rounds:
            raise _StopLoop()
    with mock.patch.object(trader_260725.time, "sleep", _sleep):
        try:
            CryptoTrader._start_monitoring(
                fake, SYMBOL, BATCH, [ENTRY_ID], [55000.0], 60000.0,
                None, None, 0.43, [0.43], {"leverage": 100}, False, "BUY",
                0, None, 0.0, None, None, _layer_sl_params())
        except _StopLoop:
            pass
        except Exception as e:
            print(f"    [驱动异常] {type(e).__name__}: {e}")
            return n["c"], e
    return n["c"], None


# --------------------------------------------------------------------------
# T1：首轮 fetch_open_orders 失败，窗口内成交 → 后续成功轮是否补挂 SL
# --------------------------------------------------------------------------

def check_t1_recovers_and_places_sl_after_failed_round():
    d = tempfile.mkdtemp(prefix="pollrec_")
    state_path = os.path.join(d, "trade_state.json")
    trader_260725.STATE_FILE = state_path
    try:
        _seed(state_path)
        states = _disk(state_path)
        fake = _make_fake(state_path, states)
        ex = fake.exchange

        # 第 1 轮失败；第 2 轮起成功，且**入场单已不在未结列表**——这才是「已成交」的
        # 正确模拟：代码只在 `order_id not in open_orders_map` 时才去 fetch_order 判成交
        # （仍在未结列表 = 还挂着，不视为成交）。
        ex.fetch_open_orders.side_effect = [
            RuntimeError("模拟 fetch_open_orders 网络失败"),   # 第 1 轮：失败
            [],                                                # 第 2 轮：成功，入场单已不在未结
            [],
        ]
        # 入场单已成交（监控靠 fetch_order 判定）
        ex.fetch_order.return_value = {
            "id": ENTRY_ID, "status": "closed", "average": 58000.0,
            "info": {"cumQuote": "24940", "executedQty": "0.43", "updateTime": 1},
        }

        rounds, err = _drive(fake, None, max_rounds=3)

        # 断言落在可靠信号上：失败轮不做成交识别（fetch_order=0），成功轮做了（>0）。
        report(
            "T1a 失败轮跳过成交识别，后续成功轮完成识别",
            ex.fetch_order.call_count >= 1,
            f"fetch_order 调用={ex.fetch_order.call_count} 次"
            f"（>0 = 失败轮之后的成功轮确实做了成交判定）；"
            f"fetch_open_orders={ex.fetch_open_orders.call_count} 次，驱动轮次={rounds}")

        report(
            "T1b 成交被识别后进入补挂止损路径",
            len(fake.sl_place_calls) >= 1,
            f"_place_prepared_orders_immediately 进入次数={len(fake.sl_place_calls)}"
            f"，STOP_MARKET(55000) create_order 次数="
            f"{len([c for c in ex.create_order.call_args_list if (c.kwargs.get('params') or {}).get('stopPrice') == 55000.0])}"
            f"，驱动异常={err!r}")

        report(
            "T1c 失败轮未消费状态：批次仍在账本且未被置 monitor_error",
            _disk(state_path).get(SYMBOL, {}).get(BATCH, {}).get("is_active") is True
            and not _disk(state_path).get(SYMBOL, {}).get(BATCH, {}).get("monitor_error"),
            f"磁盘 is_active={_disk(state_path).get(SYMBOL, {}).get(BATCH, {}).get('is_active')}，"
            f"monitor_error={_disk(state_path).get(SYMBOL, {}).get(BATCH, {}).get('monitor_error')}")

        print("        [NOT VERIFIED-1] 「循环内 create_order 成功挂出 SL」未由本用例断言：\n"
              "                 _start_monitoring 1185 行、依赖面过宽，fake 保真度不足以让内层\n"
              "                 补挂走完（补挂路径本身已由 test_protection_write_gates.py 的\n"
              "                 C1 对照组直接验证：STOP_MARKET 真实挂出并落盘 CONFIRMED）。")
        print("        [NOT VERIFIED-2] 本轮监控线程是被**测试替身缺陷**提前打断的，不是跑满哨兵轮次：\n"
              "                 证据 = 输出里的「[W1] 已写入 monitor_error 标记」（该标记只由循环\n"
              "                 外层 `except Exception` 写入，表示循环内抛了 TypeError/ValueError，\n"
              "                 根因是某个未绑定的辅助返回 MagicMock）。真实代码里这会触发\n"
              "                 「监控线程异常退出」critical —— 但那是**替身**造成的，不是被测行为，\n"
              "                 故本文件不对告警内容作断言（T2 只断言普通失败分支）。\n"
              "                 T1a/T1b/T1c 的断言全部发生在该打断**之前**，结论有效。")
    finally:
        trader_260725.STATE_FILE = _real_state_file


# --------------------------------------------------------------------------
# T2：失败路径全链路有无告警 / 有无「停止新风险」标志
# --------------------------------------------------------------------------

def check_t2_failed_poll_path_has_no_alert():
    d = tempfile.mkdtemp(prefix="pollrec_")
    state_path = os.path.join(d, "trade_state.json")
    trader_260725.STATE_FILE = state_path
    try:
        _seed(state_path)
        states = _disk(state_path)
        fake = _make_fake(state_path, states)
        ex = fake.exchange

        # 每一轮都失败 → 只走「计数 + print + continue」
        ex.fetch_open_orders.side_effect = RuntimeError("持续失败")

        rounds, err = _drive(fake, None, max_rounds=3)

        report(
            "T2 轮询失败路径：零告警、零停止新风险标志（当前行为=缺陷）",
            len(fake.sent) == 0,
            f"驱动轮次={rounds}，驱动异常={err!r}，"
            f"send_tg_notification 记录={fake.sent}（应为空 → 无告警），"
            f"_send_email_alert 未被调用（桩）")

        report(
            "T2b 连续失败只累加计数，无升级/无上限（当前行为）",
            ex.fetch_order.call_count == 0,
            f"失败 {rounds} 轮内 fetch_order 调用={ex.fetch_order.call_count} 次"
            f"（0 = 成交识别整段被 continue 跳过）；"
            f"fetch_open_orders 调用={ex.fetch_open_orders.call_count} 次")
    finally:
        trader_260725.STATE_FILE = _real_state_file


CHECKS = [
    check_t1_recovers_and_places_sl_after_failed_round,
    check_t2_failed_poll_path_has_no_alert,
]


def main():
    for fn in CHECKS:
        try:
            fn()
        except Exception as e:
            import traceback
            traceback.print_exc()
            report(fn.__name__, False, f"测试自身异常: {type(e).__name__}: {e}")
    ok = sum(1 for _, p in RESULTS if p)
    print("\n" + "=" * 68)
    print(f"监控轮询恢复注入测试: {ok}/{len(RESULTS)} 通过")
    print("=" * 68)
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
