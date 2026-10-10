#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""监控循环在 `fetch_open_orders` 失败后的恢复行为（第二十轮 T1/T2）。

## 要回答的两个问题

> ⚠️ **本文件全绿 ≠ 保护链通过验收。** T1/T2 是对**当前行为**的表征，
> 钉住的是「轮询失败分支静默」这一缺陷；不是止损保护已验收。

- **T1**：某轮 `fetch_open_orders` 失败（该分支只 `print` + 计数 + `continue`），
  **窗口内已成交**的入场单，在**下一次成功轮**是否会**进入补挂止损路径**？
  （⚠️ 只断言「进入该路径」，**未**断言 SL 真的被 `create_order` 挂出、也**未**断言
  交易所确认——替身保真度不足，见文末 NOT VERIFIED-1。）
- **T2**：这条失败分支上，**该监控线程**有没有发 TG，以及有没有设置本地「停止新风险」标志？
  （⚠️ 只覆盖该分支。**未**驱动新开仓信号验证账户级风险闸门、**未**覆盖独立巡检
  `健康巡检.py` —— **全系统告警结论未验证**。）

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

import ast
import contextlib
import io
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
    "_batch_has_active_exposure", "_collect_batch_order_ids", "_verify_clear_proof",
    "clear_batch_state",
    # S6f（第八轮复审阻断1）：有仓时校验运行期 SL 判据 —— 未绑定则拿到 MagicMock，
    # 解包 2 元组抛 ValueError 被循环外层 except 吞掉，"类型"这一维的反例会假绿。
    "_check_protection_order_validity",
    # 恢复链同一路径：返回 (verdict, found_id) 二元组，未绑定 → 解包 0 元组同样炸。
    "_adjudicate_recreate_before_repair",
    # S6b 证据升级：真实 save_batch_state 内部会做字段级 merge，未绑定 →
    # MagicMock 混进 json.dump → 落盘失败 → `is True` 恒假，磁盘证据拿不到。
    "_merge_batch_state",
    "_order_matches_intent", "_assert_create_allowed", "_final_pre_create_check",
    "_commit_protection_with_g3", "_g3a_converge_race_order", "_verify_order_created",
    "_classify_create_exception", "_verify_and_update_registry",
    "_recheck_registry_self_heal", "_is_stale_pre_launch_entry",
    "_place_prepared_orders_immediately", "_monitor_lifecycle_check",
    "_calculate_monitoring_interval", "_get_active_batch_count",
    "_alert_poll_degraded",
    "_monitor_takeover_handoff", "_monitor_terminal_evidence",
    "_finish_monitor_takeover", "_run_monitor_takeover",
    # R1/R2 修复后，恢复判定会走到本轮"所需保护已确认"这一步；
    # `_batch_net_position` 未绑定时返回 None → 解包 2 元组抛 ValueError，
    # 使监控线程在判定前就异常退出（W1），导致 T3/T4 假绿。显式绑定真实实现。
    "_batch_net_position",
    # R1/R2 第五轮复审：接管路径要用**真实**的无 ID 意图对账
    # （_self_heal_no_id / _rebuild_entry_orders_from_registry）——用桩只能证明
    # 「调用过」，证明不了「无 ID 的 ENTRY 真能被收编并按真实 ID 交监控」。
    "_self_heal_no_id", "_rebuild_entry_orders_from_registry",
    "_registry_has_unresolved_entries",
    # 第十轮复审阻断3：monitor_error 落账改走「判定 + 写盘同临界区」的方法。
    # 未绑定 → MagicMock，verdict 恒非字符串 → 落账被静默跳过，S6i-3/S6j-2 假红、
    # 且看不出是替身问题（打印的是 `写入失败（<MagicMock ...>）`）。
    "_write_monitor_error_if_owner",
    # ── F2/F3（真实成交价入账 + 成本待补证）新增的 6 个辅助 ─────────────────
    # 它们在**成交分支内**被调用；未绑定 → 返回 MagicMock → 解包 4 元组抛
    # "not enough values to unpack (expected 4, got 0)" → 被既有 except 吞成
    # 「补查开仓订单状态失败」→ 成交识别静默中止 → T1b 假红。绑真实实现后
    # 语义与生产一致（本文件 seed 无 cost_pending → 补证调用零请求、零网络）。
    "_resolve_entry_fill_evidence", "_record_fill_evidence",
    "_backfill_entry_costs", "_settlement_cost_gate",
    "_defer_settlement_for_cost", "_finalize_cost_pending_settlement",
    # ── R3 禁止清账内存锁（S6g-1 保真度修复）────────────────────────────────
    # 未绑定 → 自动 mock：收尾段读锁恒 truthy → 误判超限跳过清账（S6g-1 clear=0
    # 假红）。绑定真实 get/set/drop + `_no_clear_latches`（底层字典访问器），
    # 配合 `_make_fake` 里显式置空的 `_monitor_resume_no_clear_latches`。
    "_no_clear_latches", "_no_clear_latch_get", "_no_clear_latch_set",
    "_no_clear_latch_drop",
    # ── R5 复审打回修复（2026-10-08）新增的两个自包含方法辅助 ─────────────────
    # 同型保真度坑（本文件第七/第八个）：未绑定 → 裸 MagicMock ——
    # `_persist_guard_arrays()` 返回 MagicMock 混进 S6b 真实落盘的
    # json.dump → `Object of type MagicMock is not JSON serializable`（落盘 0 次、
    # handoff=False）；`_fill_window_cover()` 返回 MagicMock 解包 2 元组抛
    # ValueError → classify_failed → S6f-3/S6f-5「恢复中止」同源。
    # 绑真实实现 = S6g-1 同型修复，判据零改动。
    "_fill_window_cover", "_persist_guard_arrays",
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
    # ⚠️ 保真度：_fee_float 是 @staticmethod（REAL_HELPERS 的「传 self」形态不适用）。
    # 漏绑 → 自动 mock → 返回 MagicMock → `f > 0` 抛 TypeError → 被成交分支的
    # except 吞成「补查开仓订单状态失败」→ 路径静默不执行（F2/F3 在填充链引入了
    # 这一依赖，故此处显式绑真实实现）。
    fake._fee_float = CryptoTrader._fee_float

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
    fake._active_monitor_generations = {}
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
    fake._unresolved_intent_batches = set()
    fake._poll_degraded_batches = set()
    # ── R3 禁止清账内存锁（S6g-1 保真度修复，2026-10-08）────────────────────
    # fake 是裸 MagicMock：`_no_clear_latch_get` 未绑定 → 自动 mock 返回 truthy
    # 对象 → 收尾段 `_fin_no_clear_latch` 恒真 → 误判「续跑已耗尽」跳过清账
    # → S6g-1 阳性对照 clear=0 假红（本文件实测第六个同型坑，与前五个坑同族）。
    # 锁字典必须显式置空（真实实现里 `or {}` 挡不住 getattr 自动 mock），方法绑
    # 真实实现；S6g 判据（decision/cancel_limit/converge/clear ≥1）原样保留。
    fake._monitor_resume_no_clear_latches = {}
    fake._poll_fail_streak = {}
    fake._poll_first_fail_time = {}
    fake._poll_alert_lock = threading.RLock()
    fake._poll_alert_active = False

    fake.load_all_states = lambda: states
    # G3 已改用「本次读取」三元组接口（ChatGPT 复审⑤）：(数据, 是否损坏, 详情)
    # 与上面的 load_all_states 桩同源：默认**本次读取可信、未损坏**。
    fake._load_all_states_ex = lambda: (states, False, "")
    # 本文件不测持久化（T3/T4 已用真实落盘覆盖），故 save_batch_state 用**记录式桩**：
    # 真实实现会走 merge + json.dump，循环里若有未绑定的辅助把 MagicMock 写进批次对象，
    # 就会抛「Object of type MagicMock is not JSON serializable」并让落盘失败，
    # 那是**测试替身保真度问题、不是产品行为**（第五个坑）。真实落盘语义见另一份文件。
    fake.saved = []
    def _record_save(s, b, d, **_k):
        # 写盘契约（save_batch_state docstring）：仅返回 True 表示已持久化。
        # 记录式桩沿用 append，但必须回 True —— 回 None 会被 `is True` 判成
        # 落盘失败，那是**桩的保真度问题**，不是被测行为。
        fake.saved.append((s, b, dict(d)))
        return True
    fake.save_batch_state = _record_save
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
        # R5门禁（漏项⑦）：按已定方案补真实夹具字段 side（方向观测证据，
        # LONG 批次入场单 = buy）——side 缺失会被 side_evidence_missing 正确拒绝。
        ex.fetch_order.return_value = {
            "id": ENTRY_ID, "status": "closed", "average": 58000.0,
            "side": "buy",
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
# T2：持续失败是否触发 critical 告警（R1/R2 修复后）
#
# 修复前：失败分支静默（无告警、无升级、无上限）—— 缺陷。
# 修复后：连续 3 轮失败 → critical 告警 + 该批次标记降级。
#
# ⚠️ 口径边界（勿扩大）：只覆盖**这一个监控分支**。本文件**未**驱动新的开仓信号去
# 验证账户级风险闸门（RISK_MAX_ACTIVE_BATCHES / 余额阻断等）在该场景下是否有反应，
# 也**未**覆盖独立巡检 `健康巡检.py`。因此「全系统零告警」是**未验证**的推论，
# 本文件不得被引用为该结论的证据。
# --------------------------------------------------------------------------

def check_t2_persistent_failure_alerts():
    d = tempfile.mkdtemp(prefix="pollrec_")
    state_path = os.path.join(d, "trade_state.json")
    trader_260725.STATE_FILE = state_path
    try:
        _seed(state_path)
        states = _disk(state_path)
        fake = _make_fake(state_path, states)
        ex = fake.exchange

        # 每一轮都失败 → 第 3 轮起应触发 critical 告警
        ex.fetch_open_orders.side_effect = RuntimeError("持续失败")

        rounds, err = _drive(fake, None, max_rounds=3)

        report(
            "T2 持续失败达阈值 → 发出 critical 告警",
            len(fake.sent) >= 1,
            f"驱动轮次={rounds}，send_tg_notification 记录={fake.sent}"
            f"（应 ≥1 → 有告警）")

        report(
            "T2b 连续失败只累加计数，成交识别被跳过",
            ex.fetch_order.call_count == 0,
            f"失败 {rounds} 轮内 fetch_order 调用={ex.fetch_order.call_count} 次"
            f"（0 = 成交识别整段被 continue 跳过）；"
            f"fetch_open_orders 调用={ex.fetch_open_orders.call_count} 次")
    finally:
        trader_260725.STATE_FILE = _real_state_file


def _takeover_lifecycle(fake, generation="gen-1"):
    control = {
        "batch_id": BATCH, "symbol": SYMBOL, "generation": generation,
        "reconciled": True, "phase": "starting", "lock": threading.Lock(),
        "handoff_event": threading.Event(), "failure_alerted": False,
    }
    fake._active_monitor_generations[BATCH] = generation
    fake._active_monitors.add(BATCH)
    fake._unresolved_intent_batches.add(BATCH)
    fake._poll_degraded_batches.add(BATCH)
    fake._poll_fail_streak[BATCH] = 1
    fake._start_monitoring = lambda **kw: CryptoTrader._start_monitoring(fake, **kw)
    return control


def _monitor_kwargs():
    return dict(
        symbol=SYMBOL, batch_id=BATCH, entry_orders=[ENTRY_ID],
        stop_steps=[55000.0], take_profit_price=60000.0,
        current_sl_id=None, tp_order_id=None, batch_total_amount=0.43,
        target_amounts=[0.43], params_base={"leverage": 100},
        is_hedge_mode=False, side="BUY", last_filled_count=0,
        filled_details=[0.0], total_entry_fee=0.0, pending_sl_orders=[0],
        prepared_tp_params={}, layer_sl_params=_layer_sl_params())


def check_s6_first_poll_failure_keeps_gate():
    """Drive the actual loop: first poll fails, then thread exits before a good cycle."""
    d = tempfile.mkdtemp(prefix="s6_first_fail_")
    state_path = os.path.join(d, "trade_state.json")
    trader_260725.STATE_FILE = state_path
    try:
        _seed(state_path)
        states = _disk(state_path)
        fake = _make_fake(state_path, states)
        life = _takeover_lifecycle(fake)
        fake.exchange.fetch_open_orders.side_effect = RuntimeError("first poll unavailable")
        sleep_calls = {"n": 0}

        def _sleep(sec):
            sleep_calls["n"] += 1
            if sleep_calls["n"] == 1:
                return
            raise _StopLoop("stop after failed first poll")

        caught = None
        with mock.patch.object(trader_260725.time, "sleep", _sleep), \
                mock.patch.object(trader_260725, "current_instance_id", return_value="s6-test"), \
                mock.patch.object(trader_260725, "write_progress", return_value=None):
            try:
                fake._run_monitor_takeover(life, **_monitor_kwargs())
            except _StopLoop:
                caught = True
        report("S6a 实际监控首轮轮询失败后退出 → 从未业务接管、闸门保持",
               caught is True and not life["handoff_event"].is_set()
               and BATCH in fake._unresolved_intent_batches,
               f"handoff={life['handoff_event'].is_set()}，phase={life['phase']}，"
               f"unresolved={fake._unresolved_intent_batches}")
    finally:
        trader_260725.STATE_FILE = _real_state_file


def check_s6_complete_first_business_cycle():
    """The actual loop releases only after known order/position facts + durable save."""
    d = tempfile.mkdtemp(prefix="s6_first_ok_")
    state_path = os.path.join(d, "trade_state.json")
    trader_260725.STATE_FILE = state_path
    try:
        _seed(state_path)
        states = _disk(state_path)
        fake = _make_fake(state_path, states)
        life = _takeover_lifecycle(fake)
        fake.exchange.fetch_open_orders.return_value = [{"id": ENTRY_ID, "status": "open"}]
        # 🔥 证据升级（第八轮复审「durable_save 只是测试内存替身」）：
        #   1) 绑**真实** save_batch_state —— 内部经 _persist_states 真写临时 STATE_FILE；
        #   2) 把 load_all_states 换成**读磁盘** —— 放闸前的 `_cycle_batch` 必须来自
        #      磁盘重读，否则"状态落盘成功 + 批次可重读"只是测试内存里的自说自话。
        saves = []
        _real_save_batch_state = CryptoTrader.save_batch_state

        def durable_save(symbol, batch_id, data, **_k):
            ok = _real_save_batch_state(fake, symbol, batch_id, data, **_k)
            if ok is True:
                saves.append(dict(_disk(state_path).get(symbol, {}).get(batch_id, {}) or {}))
            return ok

        fake.save_batch_state = durable_save
        fake.load_all_states = lambda: _disk(state_path)
        fake._load_all_states_ex = lambda: (_disk(state_path), False, "")
        sleep_calls = {"n": 0}

        def _sleep(sec):
            sleep_calls["n"] += 1
            if sleep_calls["n"] == 1:
                return
            raise _StopLoop("stop after one complete business cycle")

        with mock.patch.object(trader_260725.time, "sleep", _sleep), \
                mock.patch.object(trader_260725, "current_instance_id", return_value="s6-test"), \
                mock.patch.object(trader_260725, "write_progress", return_value=None):
            try:
                fake._run_monitor_takeover(life, **_monitor_kwargs())
            except _StopLoop:
                pass
        on_disk = _disk(state_path).get(SYMBOL, {}).get(BATCH, {})
        report("S6b 实际首轮完成：订单 open、持仓零、未决 registry 空、状态落盘成功"
               "（真实 _persist_states 写盘 + 磁盘重读作放闸证据）",
               bool(saves) and on_disk.get("is_active") is True
               and life["handoff_event"].is_set()
               and life["phase"] == "unverified_exit"
               and BATCH in fake._unresolved_intent_batches
               and BATCH in fake._poll_degraded_batches,
               f"磁盘落盘次数={len(saves)}，磁盘 is_active={on_disk.get('is_active')}，"
               f"handoff曾完成={life['handoff_event'].is_set()}，"
               f"非终态测试退出后未决闸门={fake._unresolved_intent_batches}，"
               f"降级闸门={fake._poll_degraded_batches}")
    finally:
        trader_260725.STATE_FILE = _real_state_file


def check_s6_normal_terminal_requires_durable_proof():
    """Produce tombstone through real clear gate, then drive actual G1 monitor exit."""
    d = tempfile.mkdtemp(prefix="s6_terminal_")
    state_path = os.path.join(d, "trade_state.json")
    trader_260725.STATE_FILE = state_path
    try:
        _seed(state_path)
        states = _disk(state_path)
        fake = _make_fake(state_path, states)
        # 🔥 证据升级（第八轮复审「墓碑写入是内存替身」）：改用**真实读写 + 临时文件**。
        #    _monitor_terminal_evidence 拿到的是从磁盘重读的墓碑，
        #    不再由测试内存 dict 直接投喂——"正常终结 = 持久化账本 + 交易所 clear proof"
        #    才是一条可独立复现的证据链。
        tomb_path = os.path.join(d, "trade_tombstones.json")
        fake.tombstone_file = tomb_path
        fake._load_tombstones = lambda: CryptoTrader._load_tombstones(fake)
        fake._persist_tombstones = lambda data: CryptoTrader._persist_tombstones(fake, data)
        proof = {
            "batch_id": BATCH, "symbol": SYMBOL, "scope": "PRE_ENTRY",
            "position_zero": True, "state_ids_resolved": [ENTRY_ID],
            "exchange_scan": "zero", "l1_canceled": [ENTRY_ID],
            "l2_canceled": [],
        }
        cleared = fake.clear_batch_state(SYMBOL, BATCH, proof=proof)
        # 磁盘墓碑（供断言"确实写盘"），与终结证据读取同源
        try:
            with open(tomb_path, encoding="utf-8") as _tf:
                tomb_on_disk = (json.load(_tf) or {}).get(BATCH)
        except (OSError, ValueError):
            tomb_on_disk = None
        # The actual durable state reader sees the deletion written by clear_batch_state.
        fake.load_all_states = lambda: _disk(state_path)
        fake._load_all_states_ex = lambda: (_disk(state_path), False, "")
        life = _takeover_lifecycle(fake)
        with mock.patch.object(trader_260725.time, "sleep", lambda sec: None), \
                mock.patch.object(trader_260725, "current_instance_id", return_value="s6-test"), \
                mock.patch.object(trader_260725, "write_progress", return_value=None):
            fake._run_monitor_takeover(life, **_monitor_kwargs())
    finally:
        trader_260725.STATE_FILE = _real_state_file
    report("S6c 实际生命周期正常退出 + durable tombstone proof（真实落盘+磁盘重读）→ 解除闸门",
           cleared is True and tomb_on_disk is not None
           and life["phase"] == "terminal"
           and BATCH not in fake._unresolved_intent_batches
           and BATCH not in fake._poll_degraded_batches,
           f"clear={cleared!r}，phase={life['phase']}，"
           f"磁盘墓碑 evidence={(tomb_on_disk or {}).get('clear_evidence')}，"
           f"unresolved={fake._unresolved_intent_batches}")


def check_s6_unclassified_exception_keeps_gate():
    """An actual monitor-loop exception is not treated as a successful handoff/terminal."""
    d = tempfile.mkdtemp(prefix="s6_exception_")
    state_path = os.path.join(d, "trade_state.json")
    trader_260725.STATE_FILE = state_path
    try:
        _seed(state_path)
        fake = _make_fake(state_path, _disk(state_path))
        life = _takeover_lifecycle(fake)
        fake._calculate_monitoring_interval = lambda: (_ for _ in ()).throw(
            RuntimeError("injected real-loop failure"))
        with mock.patch.object(trader_260725, "current_instance_id", return_value="s6-test"), \
                mock.patch.object(trader_260725, "write_progress", return_value=None):
            fake._run_monitor_takeover(life, **_monitor_kwargs())
        report("S6d 实际监控循环异常退出 → 保持闸门并告警",
               life["phase"] == "unverified_exit"
               and BATCH in fake._unresolved_intent_batches
               and any(level == "critical" for level, _ in fake.sent),
               f"phase={life['phase']}，unresolved={fake._unresolved_intent_batches}，"
               f"alerts={[level for level, _ in fake.sent]}")
    finally:
        trader_260725.STATE_FILE = _real_state_file


def check_s6_old_generation_exit_is_ignored():
    """Run the real old monitor loop; a late exit must not remove the new generation."""
    d = tempfile.mkdtemp(prefix="s6_generation_")
    state_path = os.path.join(d, "trade_state.json")
    trader_260725.STATE_FILE = state_path
    try:
        _seed(state_path)
        fake = _make_fake(state_path, _disk(state_path))
        old = _takeover_lifecycle(fake, "old-generation")
        entered_sleep = threading.Event()
        release_old = threading.Event()

        def _blocked_sleep(sec):
            entered_sleep.set()
            release_old.wait(timeout=3)

        errors = []
        def run_old():
            try:
                fake._run_monitor_takeover(old, **_monitor_kwargs())
            except BaseException as exc:
                errors.append(exc)
        with mock.patch.object(trader_260725.time, "sleep", _blocked_sleep), \
                mock.patch.object(trader_260725, "current_instance_id", return_value="s6-test"), \
                mock.patch.object(trader_260725, "write_progress", return_value=None):
            t = threading.Thread(target=run_old, daemon=True)
            t.start()
            assert entered_sleep.wait(timeout=2), "old monitor did not enter real loop"
            fake._active_monitor_generations[BATCH] = "new-generation"
            fake._active_monitors.add(BATCH)
            before_unresolved = set(fake._unresolved_intent_batches)
            before_degraded = set(fake._poll_degraded_batches)
            release_old.set()
            t.join(timeout=3)
        report("S6e 旧代次在新代次接管后退出 → 不得删新登记或改闸门",
               not t.is_alive() and not errors
               and fake._active_monitor_generations.get(BATCH) == "new-generation"
               and BATCH in fake._active_monitors
               and fake._unresolved_intent_batches == before_unresolved
               and fake._poll_degraded_batches == before_degraded,
               f"alive={t.is_alive()}，errors={errors!r}，owner="
               f"{fake._active_monitor_generations.get(BATCH)!r}，"
               f"unresolved={fake._unresolved_intent_batches}")
    finally:
        trader_260725.STATE_FILE = _real_state_file


def _s6f_kwargs():
    """有仓时序：本批次已成交 0.43、本地 SL 锚点指向 SL1、无待补挂层。"""
    kw = _monitor_kwargs()
    kw.update(current_sl_id="SL1", last_filled_count=1,
              filled_details=[58000.0], pending_sl_orders=[])
    return kw


def _s6f_drive(sl_type, *, amount=0.43, close_position="false"):
    """跑一轮**真实**监控：仓位 0.43、filled_layers 已置位、current_sl_id=SL1
    就是交易所那张条件单。返回 (fake, life, 磁盘落盘次数, 驱动异常)。

    `amount` / `close_position` 用来构造第九轮复审阻断1那一对反例
    （`reduceOnly=true, closePosition=false, amount=None` vs 真正的全仓平单）。
    """
    d = tempfile.mkdtemp(prefix="s6f_%s_" % sl_type[:5].lower())
    state_path = os.path.join(d, "trade_state.json")
    trader_260725.STATE_FILE = state_path
    fake = life = None
    try:
        _seed(state_path, current_sl_id="SL1", last_filled_count=1,
              filled_details=[58000.0], pending_sl_orders=[])
        states = _disk(state_path)
        fake = _make_fake(state_path, states)
        life = _takeover_lifecycle(fake)
        fake._get_current_position_amt = lambda *a, **k: 0.43
        sl_order = {
            "id": "SL1", "side": "SELL", "amount": amount, "status": "open",
            "type": "market",
            # 顶层 type 被 ccxt 归一化为 market；类型判据只认 info.type
            "info": {"type": sl_type, "positionSide": "BOTH",
                     "reduceOnly": "true", "closePosition": close_position,
                     "stopPrice": 55000.0},
        }
        # 入场单**不在**未结列表（filled_layers 已置位 → 循环直接跳过成交识别）
        fake.exchange.fetch_open_orders.return_value = [sl_order]
        # 补挂必须失败：否则错类型那一轮会在同一轮挂出真止损并顺带放闸，
        # A/B 就区分不出"类型判据"这一个变量。
        fake.exchange.create_order.side_effect = RuntimeError("模拟补挂止损失败")

        saves = []

        def durable_save(symbol, batch_id, data, **_k):
            states.setdefault(symbol, {})[batch_id] = dict(data)
            saves.append(dict(data))
            return True
        fake.save_batch_state = durable_save

        sleep_calls = {"n": 0}

        def _sleep(sec):
            sleep_calls["n"] += 1
            if sleep_calls["n"] == 1:
                return
            raise _StopLoop("stop after one real cycle")
        err = None
        with mock.patch.object(trader_260725.time, "sleep", _sleep), \
                mock.patch.object(trader_260725, "current_instance_id", return_value="s6-test"), \
                mock.patch.object(trader_260725, "write_progress", return_value=None):
            try:
                fake._run_monitor_takeover(life, **_s6f_kwargs())
            except _StopLoop:
                pass
            except Exception as e:      # noqa: BLE001 - 记录下来，交给断言暴露
                err = e
        return fake, life, saves, err
    finally:
        trader_260725.STATE_FILE = _real_state_file


def _s6f_fetch_order_ids(fake, oid="SL1"):
    return [str(c.args[0]) for c in fake.exchange.fetch_order.call_args_list
            if c.args and str(c.args[0]) == oid]


def check_s6_wrong_stop_type_blocks_takeover():
    """第八轮复审阻断1：有仓时 `current_sl_id` 指向**止盈类型**条件单 → 不得放闸。

    运行期 `_check_protection_order_validity` 不判类型、不排 NaN，而放闸判据
    `_poll_sl_validated` 正是它给的 → `TAKE_PROFIT_MARKET`（info 同样带 stopPrice）
    会被当作止损完成首轮接管。这里用同一替身、同一时序的 A/B 把"类型"这一个变量
    单独钉死；启动重证一侧的同口径反例已由 B25/B27 覆盖。
    """
    neg, neg_life, neg_saves, neg_err = _s6f_drive("TAKE_PROFIT_MARKET")
    report("S6f-1 有仓 + current_sl_id 指向止盈类型单 → 首轮不得放闸、闸门保持",
           neg_err is None and not neg_life["handoff_event"].is_set()
           and bool(neg_saves) and BATCH in neg._unresolved_intent_batches,
           f"handoff={neg_life['handoff_event'].is_set()}，"
           f"磁盘落盘={len(neg_saves)}（落盘本身必须成功，缺的只是保护判据），"
           f"未决闸门={neg._unresolved_intent_batches}，异常={neg_err!r}")
    report("S6f-2 拒绝理由是类型这一维（SG3 告警带白名单事实）且已进入撤旧回查",
           any("非止损类型" in t for _, t in neg.sent)
           and len(_s6f_fetch_order_ids(neg)) >= 1,
           f"类型告警={any('非止损类型' in t for _, t in neg.sent)}，"
           f"撤旧回查 SL1 次数={len(_s6f_fetch_order_ids(neg))}")

    pos, pos_life, pos_saves, pos_err = _s6f_drive("STOP_MARKET")
    report("S6f-3 阳性对照：同一时序、只把 info.type 换成 STOP_MARKET → 正常放闸",
           pos_err is None and pos_life["handoff_event"].is_set()
           and bool(pos_saves)
           and not any("非止损类型" in t for _, t in pos.sent)
           and not _s6f_fetch_order_ids(pos),
           f"handoff={pos_life['handoff_event'].is_set()}，"
           f"类型告警={any('非止损类型' in t for _, t in pos.sent)}，"
           f"撤旧回查 SL1 次数={len(_s6f_fetch_order_ids(pos))}（0 = 未判为无效），"
           f"异常={pos_err!r}")


def _s6g_bind_side_effect_spy(fake):
    """把 finally 收尾能碰到的**真实副作用面**全部换成计数探针。

    S6e 把 `_finally_cleanup_decision` 桩成了 'skip'，因此那段生产收尾根本没被执行过；
    这里改绑**真实**实现（返回 'allow'），再在其下游每一个写动作上放探针。
    """
    hits = {"decision": 0, "cancel_limit": 0, "converge": 0, "clear": 0, "finalize": 0}
    _real_decision = CryptoTrader._finally_cleanup_decision

    def decision(s, b):
        hits["decision"] += 1
        return _real_decision(fake, s, b)

    fake._finally_cleanup_decision = decision
    fake._cancel_limit_close_order = (
        lambda s, b: hits.__setitem__("cancel_limit", hits["cancel_limit"] + 1))

    def converge(s, b):
        hits["converge"] += 1
        return {"batch_id": b, "symbol": s}   # 让下游 clear 能被调用到

    fake._converge_batch_orders_before_clear = converge

    def clear(s, b, **k):
        hits["clear"] += 1
        return True
    fake.clear_batch_state = clear
    fake._finalize_limit_full_fill = (
        lambda s, b, oid: hits.__setitem__("finalize", hits["finalize"] + 1)
        or (True, "spy"))
    fake._cleanup_authorization_still_valid = lambda s, b, snap: True
    return hits


def _s6g_run_owner():
    """阳性对照：**所有者代次**退出 → 生产收尾真的会跑到这些写动作。"""
    d = tempfile.mkdtemp(prefix="s6g_owner_")
    state_path = os.path.join(d, "trade_state.json")
    trader_260725.STATE_FILE = state_path
    try:
        _seed(state_path)
        fake = _make_fake(state_path, _disk(state_path))
        life = _takeover_lifecycle(fake, "owner-generation")
        fake.exchange.fetch_open_orders.return_value = [{"id": ENTRY_ID, "status": "open"}]
        hits = _s6g_bind_side_effect_spy(fake)
        sleep_calls = {"n": 0}

        def _sleep(sec):
            sleep_calls["n"] += 1
            if sleep_calls["n"] == 1:
                return
            raise _StopLoop("stop after one real cycle")
        err = None
        with mock.patch.object(trader_260725.time, "sleep", _sleep), \
                mock.patch.object(trader_260725, "current_instance_id", return_value="s6-test"), \
                mock.patch.object(trader_260725, "write_progress", return_value=None):
            try:
                fake._run_monitor_takeover(life, **_monitor_kwargs())
            except _StopLoop:
                pass
            except Exception as e:      # noqa: BLE001
                err = e
        return hits, err
    finally:
        trader_260725.STATE_FILE = _real_state_file


def _s6g_run_old_generation():
    """旧代次：跑满一轮 → 新代次接管 → 旧代次醒来 break 出循环。返回 (hits, 口志, 线程, 错误)。"""
    d = tempfile.mkdtemp(prefix="s6g_old_")
    state_path = os.path.join(d, "trade_state.json")
    trader_260725.STATE_FILE = state_path
    try:
        _seed(state_path)
        fake = _make_fake(state_path, _disk(state_path))
        old = _takeover_lifecycle(fake, "old-generation")
        fake.exchange.fetch_open_orders.return_value = [{"id": ENTRY_ID, "status": "open"}]
        hits = _s6g_bind_side_effect_spy(fake)

        sleep_calls = {"n": 0}
        entered_sleep = threading.Event()
        release_old = threading.Event()

        def _sleep(sec):
            sleep_calls["n"] += 1
            if sleep_calls["n"] == 1:
                return                      # 让旧代次完整跑完第一轮
            entered_sleep.set()
            release_old.wait(timeout=3)     # 卡在下一轮醒来处
        errors = []

        def run_old():
            try:
                fake._run_monitor_takeover(old, **_monitor_kwargs())
            except BaseException as exc:    # noqa: BLE001
                errors.append(exc)
        buf = io.StringIO()
        with mock.patch.object(trader_260725.time, "sleep", _sleep), \
                mock.patch.object(trader_260725, "current_instance_id", return_value="s6-test"), \
                mock.patch.object(trader_260725, "write_progress", return_value=None), \
                contextlib.redirect_stdout(buf):
            t = threading.Thread(target=run_old, daemon=True)
            t.start()
            assert entered_sleep.wait(timeout=3), "旧代次未进入真实监控循环"
            fake._active_monitor_generations[BATCH] = "new-generation"
            fake._active_monitors.add(BATCH)
            release_old.set()
            t.join(timeout=5)
        return hits, buf.getvalue(), t, errors, fake
    finally:
        trader_260725.STATE_FILE = _real_state_file


def check_s6_old_generation_exit_runs_no_cleanup_side_effects():
    """第八轮复审阻断2：旧代次在整个退出收尾中失去副作用权限。

    S6e 只证明了"旧代次不删新登记"，且把 `_finally_cleanup_decision` 桩成 'skip'，
    于是下面这段真实收尾（finalizer / 撤限价平仓单 / converge 撤单 / clear 删状态）
    从未被执行过。这里改绑**真实**判定 + 在每个写动作上放探针，并给出阳性对照——
    没有对照就无法区分"旧代次被拦住"和"这段路径本来就不会跑"。
    """
    owner_hits, owner_err = _s6g_run_owner()
    report("S6g-1 阳性对照：所有者代次退出 → 真实收尾确实跑到撤单/收敛/清理",
           owner_err is None and owner_hits["decision"] >= 1
           and owner_hits["cancel_limit"] >= 1
           and owner_hits["converge"] >= 1 and owner_hits["clear"] >= 1,
           f"decision={owner_hits['decision']}，cancel_limit={owner_hits['cancel_limit']}，"
           f"converge={owner_hits['converge']}，clear={owner_hits['clear']}，"
           f"finalize={owner_hits['finalize']}，异常={owner_err!r}")

    hits, log, t, errors, fake = _s6g_run_old_generation()
    report("S6g-2 旧代次在新代次接管后退出 → 收尾判定被跳过、零交易所/账本副作用",
           not t.is_alive() and not errors
           and hits["decision"] == 0 and hits["cancel_limit"] == 0
           and hits["converge"] == 0 and hits["clear"] == 0
           and hits["finalize"] == 0,
           f"alive={t.is_alive()}，errors={errors!r}，hits={hits}")
    report("S6g-3 确认走到了非所有者分支（而非测试没跑到 finally）",
           "已非登记所有者" in log,
           f"日志含非所有者标记={'已非登记所有者' in log}；"
           f"owner={fake._active_monitor_generations.get(BATCH)!r}；"
           f"日志尾部={log.strip().splitlines()[-2:]}")


def check_s6_amount_none_needs_close_position():
    """第九轮复审阻断1：`amount=None` 只有订单自己声明 `closePosition=true` 才有效。

    反例 `reduceOnly=true, closePosition=false, amount=None`：既无全仓平语义、
    又无可核实覆盖量。SG3 的 ②判据只看 reduceOnly/closePosition 任一为 true，
    会先放过它；若 `_sl_order_verdict` 仅凭调用方传 `allow_amount_none=True`
    就返回 True，这张单会让**有仓批次**完成首轮接管放闸（S6 的闸门被空手套）。
    同时给真正全仓平单阳性对照，否则分不清"判据拒了 None"和"这条路径根本走不通"。
    """
    neg, neg_life, neg_saves, neg_err = _s6f_drive(
        "STOP_MARKET", amount=None, close_position="false")
    report("S6f-4 reduceOnly=true + closePosition=false + amount=None → 不得放闸",
           neg_err is None and not neg_life["handoff_event"].is_set()
           and bool(neg_saves) and BATCH in neg._unresolved_intent_batches
           and any("closePosition 非 true" in t for _, t in neg.sent),
           f"handoff={neg_life['handoff_event'].is_set()}，"
           f"磁盘落盘={len(neg_saves)}，未决闸门={neg._unresolved_intent_batches}，"
           f"覆盖量告警={any('closePosition 非 true' in t for _, t in neg.sent)}，"
           f"异常={neg_err!r}")

    pos, pos_life, pos_saves, pos_err = _s6f_drive(
        "STOP_MARKET", amount=None, close_position="true")
    report("S6f-5 阳性对照：真正 closePosition=true 的全仓平单 → 正常放闸",
           pos_err is None and pos_life["handoff_event"].is_set()
           and bool(pos_saves)
           and not any("closePosition 非 true" in t for _, t in pos.sent),
           f"handoff={pos_life['handoff_event'].is_set()}，"
           f"磁盘落盘={len(pos_saves)}，"
           f"覆盖量告警={any('closePosition 非 true' in t for _, t in pos.sent)}，"
           f"异常={pos_err!r}")


def _s6i_run(replace_generation):
    """真实监控：第一轮完整跑完后，下一次 `sleep` 抛**普通异常**。

    `replace_generation=True` → 在抛异常**之前**把登记换成新代次，
    也就是"旧代次被新代次替换后才炸"这一半时序（S6g 只覆盖正常醒来 break）。
    返回 (states, saves, 日志, 错误列表, fake, lifecycle)。
    """
    tag = "old" if replace_generation else "owner"
    d = tempfile.mkdtemp(prefix="s6i_%s_" % tag)
    state_path = os.path.join(d, "trade_state.json")
    trader_260725.STATE_FILE = state_path
    try:
        _seed(state_path)
        states = _disk(state_path)
        fake = _make_fake(state_path, states)
        life = _takeover_lifecycle(fake, "gen-s6i-old")
        fake.exchange.fetch_open_orders.return_value = [
            {"id": ENTRY_ID, "status": "open"}]
        saves = []

        def durable_save(symbol, batch_id, data, **_k):
            states.setdefault(symbol, {})[batch_id] = dict(data)
            saves.append(dict(data))
            return True
        fake.save_batch_state = durable_save

        n = {"i": 0}

        def _sleep(sec):
            n["i"] += 1
            if n["i"] == 1:
                return                      # 第一轮完整跑完 → 进入下一次 sleep
            if replace_generation:
                fake._active_monitor_generations[BATCH] = "gen-s6i-new"
                fake._active_monitors.add(BATCH)
            raise RuntimeError("模拟：旧代次从 sleep 抛出普通异常")

        errors = []
        buf = io.StringIO()
        with mock.patch.object(trader_260725.time, "sleep", _sleep), \
                mock.patch.object(trader_260725, "current_instance_id",
                                  return_value="s6-test"), \
                mock.patch.object(trader_260725, "write_progress",
                                  return_value=None), \
                contextlib.redirect_stdout(buf):
            try:
                fake._run_monitor_takeover(life, **_monitor_kwargs())
            except BaseException as e:      # noqa: BLE001 - 记录下来，交给断言暴露
                errors.append(e)
        return states, saves, buf.getvalue(), errors, fake, life
    finally:
        trader_260725.STATE_FILE = _real_state_file


def check_s6_exception_side_effects_are_generation_gated():
    """第九轮复审阻断2：旧代次**异常**退出时，落账与告警同样要先看代次。

    S6g 覆盖的是旧代次"正常醒来后 break 出循环"；这条覆盖另一半：旧代次被新代次
    替换后从 `sleep` 抛普通异常 → 先进 `except Exception`，那里两步都以 batch_id
    为键写**共享**状态（`monitor_error` 落账 + critical 退出告警），
    发生在 finally 的所有权判断**之前**。少了这次核对，旧代次会把 `monitor_error`
    写进新代次正在用的账本 —— recover_active_batches 见到它会跳过恢复并要求人工清理。
    """
    states, saves, log, errors, fake, life = _s6i_run(replace_generation=True)
    batch = states.get(SYMBOL, {}).get(BATCH, {})
    report("S6i-1 旧代次异常退出 → 不写 monitor_error、不发终止告警、登记仍属新代次",
           batch.get("monitor_error") is not True
           and not any(s.get("monitor_error") for s in saves)
           and not any("监控线程异常退出" in t for _, t in fake.sent)
           and fake._active_monitor_generations.get(BATCH) == "gen-s6i-new",
           f"账本 monitor_error={batch.get('monitor_error')!r}，"
           f"落盘含 monitor_error 的次数={sum(1 for s in saves if s.get('monitor_error'))}，"
           f"退出告警={any('监控线程异常退出' in t for _, t in fake.sent)}，"
           f"登记={fake._active_monitor_generations.get(BATCH)!r}，"
           f"退出原因={life.get('exit_reason')!r}")
    # 注意：`errors` 恒为空不是断言失效 —— `_start_monitoring` 的 `except Exception`
    # 本来就不 re-raise（吞掉后 finally 走完即正常返回），所以这里用**日志**证明
    # 确实进过该分支、且进的是非所有者那一路。
    report("S6i-2 确认真的进了 except 并走到非所有者分支（而非根本没抛异常）",
           "监控循环内部异常" in log
           and "跳过 monitor_error 落账与终止告警" in log,
           f"异常分支日志={'监控循环内部异常' in log}，"
           f"门控日志={'跳过 monitor_error 落账与终止告警' in log}，"
           f"errors={errors!r}（预期为空：except 不 re-raise）")

    states2, saves2, log2, errors2, fake2, life2 = _s6i_run(replace_generation=False)
    batch2 = states2.get(SYMBOL, {}).get(BATCH, {})
    report("S6i-3 阳性对照：**所有者**代次异常退出 → 确实写 monitor_error + 发终止告警",
           batch2.get("monitor_error") is True
           and any("监控线程异常退出" in t for _, t in fake2.sent)
           and "监控循环内部异常" in log2
           and "跳过 monitor_error 落账与终止告警" not in log2,
           f"账本 monitor_error={batch2.get('monitor_error')!r}，"
           f"退出告警={any('监控线程异常退出' in t for _, t in fake2.sent)}，"
           f"异常分支日志={'监控循环内部异常' in log2}，"
           f"被门控={'跳过 monitor_error 落账与终止告警' in log2}")




def _s6j_run(register_during_notify):
    """受控并发：旧代次**通过所有权判定后停在通知处**，此刻登记新代次，再放行。

    S6i-1 覆盖的是"替换发生在所有权检查**之前**"；第十轮复审点名的是下一段窗口：
    判定为真 → 发网络通知（可挂数百 ms～5s）→ 读账本 → 写盘。真正的竞态发生在
    这中间，S6i 那条时序根本碰不到它。这里用一个受控卡点把它钉死：
      * 卡在 `send_tg_notification`（"监控线程异常退出"那条）里 —— 此刻代次判定已经
        通过、`monitor_error` 还没写；
      * 主测试线程做**忠实登记**（同样先抢 `_active_monitors_lock`，与生产一致）；
      * 再放行旧代次，看它还写不写。

    `register_during_notify=False` 是同一时序的阳性对照：卡点仍在，但没有第三者接管，
    用来证明"绿"不是因为卡点本身把写入路径掐断了。

    返回 (states, saves, 日志, fake, lifecycle, 时序字典, 线程内异常)。
    """
    tag = "takeover" if register_during_notify else "control"
    d = tempfile.mkdtemp(prefix="s6j_%s_" % tag)
    state_path = os.path.join(d, "trade_state.json")
    trader_260725.STATE_FILE = state_path
    try:
        _seed(state_path)
        states = _disk(state_path)
        fake = _make_fake(state_path, states)
        life = _takeover_lifecycle(fake, "gen-s6j-old")
        fake.exchange.fetch_open_orders.return_value = [
            {"id": ENTRY_ID, "status": "open"}]
        saves = []

        def durable_save(symbol, batch_id, data, **_k):
            states.setdefault(symbol, {})[batch_id] = dict(data)
            saves.append(dict(data))
            return True
        fake.save_batch_state = durable_save

        reached, release = threading.Event(), threading.Event()
        _real_send = fake.send_tg_notification

        def send(*a, **kw):
            text = str(a[0]) if a else ""
            if "监控线程异常退出" in text:
                reached.set()          # ← 卡点：所有权判定已通过、写盘尚未发生
                release.wait(10)       # ← 等主测试线程登记完新代次
            return _real_send(*a, **kw)
        fake.send_tg_notification = send

        n = {"i": 0}

        def _sleep(sec):
            n["i"] += 1
            if n["i"] == 1:
                return                 # 第一轮完整跑完
            raise RuntimeError("模拟：旧代次从 sleep 抛出普通异常")

        buf = io.StringIO()
        seq = {}
        errors = []
        with mock.patch.object(trader_260725.time, "sleep", _sleep), \
                mock.patch.object(trader_260725, "current_instance_id",
                                  return_value="s6-test"), \
                mock.patch.object(trader_260725, "write_progress",
                                  return_value=None), \
                contextlib.redirect_stdout(buf):
            def _run():
                try:
                    fake._run_monitor_takeover(life, **_monitor_kwargs())
                except BaseException as e:     # noqa: BLE001 - 交给断言暴露
                    errors.append(e)

            t = threading.Thread(target=_run, daemon=True)
            t.start()
            seq["reached_notify"] = reached.wait(10)
            if seq["reached_notify"] and register_during_notify:
                # 忠实登记：生产里登记方也要先抢到代次锁，故这里同样带超时去抢，
                # 抢不到 = 有人把网络调用（或别的慢操作）留在了锁内。
                seq["reg_lock_acquired"] = fake._active_monitors_lock.acquire(timeout=5)
                if seq["reg_lock_acquired"]:
                    fake._active_monitor_generations[BATCH] = "gen-s6j-new"
                    fake._active_monitors.add(BATCH)
                    fake._active_monitors_lock.release()
            release.set()
            t.join(30)
            seq["thread_still_alive"] = t.is_alive()
        return states, saves, buf.getvalue(), fake, life, seq, errors
    finally:
        trader_260725.STATE_FILE = _real_state_file


def _s6j_source_tree():
    path = os.path.abspath(trader_260725.__file__)
    with io.open(path, encoding="utf-8") as f:
        src = f.read()
    return src, ast.parse(src)


def check_s6_monitor_error_write_is_atomic():
    """第十轮复审阻断3：`monitor_error` 落账必须与代次判定在同一段临界区内。

    三条各管一段，缺一条都还能被绕过去：
      S6j-1 动态时序 —— 判定通过后停在通知处、期间完成登记 → 放行后**不得**写入；
      S6j-2 阳性对照 —— 同一卡点但无人接管 → 仍要写入（证明卡点没掐断正常路径）；
      S6j-3 结构断言 —— 判定与 save_batch_state 必须同在 `_active_monitors_lock`
            内、告警发送必须在锁外。S6j-1 单独挡不住"写之前再做一次**无锁**复核"
            那种改法（复核发生在通知之后，照样能判出非所有者），只有结构断言能。
    """
    # ---- S6j-1：判定通过 → 通知期间被替换 → 放行后不得落账
    states, saves, log, fake, life, seq, errors = _s6j_run(register_during_notify=True)
    batch = states.get(SYMBOL, {}).get(BATCH, {})
    report("S6j-1 通知期间完成新代次登记 → 旧代次放行后不写 monitor_error",
           seq.get("reached_notify") is True
           and batch.get("monitor_error") is not True
           and not any(s.get("monitor_error") for s in saves)
           and "放弃 monitor_error 写入" in log
           and fake._active_monitor_generations.get(BATCH) == "gen-s6j-new"
           and seq.get("thread_still_alive") is False,
           f"卡到通知={seq.get('reached_notify')}，账本 monitor_error="
           f"{batch.get('monitor_error')!r}，"
           f"含 monitor_error 的落盘={sum(1 for s in saves if s.get('monitor_error'))}，"
           f"放弃写入日志={'放弃 monitor_error 写入' in log}，"
           f"登记={fake._active_monitor_generations.get(BATCH)!r}，"
           f"线程残留={seq.get('thread_still_alive')}，errors={errors!r}")

    # ---- 同一时序的附带判据：通知期间代次锁**必须是空闲的**（网络不占锁）
    report("S6j-1b 通知（网络）期间登记方能立刻拿到代次锁 → 通知没有占着锁",
           seq.get("reg_lock_acquired") is True,
           f"5s 内抢到代次锁={seq.get('reg_lock_acquired')}；"
           f"抢不到 = 有网络调用留在了 _active_monitors_lock 里")

    # ---- S6j-2：同一卡点、无人接管 → 必须照常写入
    states2, saves2, log2, fake2, life2, seq2, errors2 = _s6j_run(
        register_during_notify=False)
    batch2 = states2.get(SYMBOL, {}).get(BATCH, {})
    report("S6j-2 阳性对照：同一卡点但无人接管 → monitor_error 照常写入",
           seq2.get("reached_notify") is True
           and batch2.get("monitor_error") is True
           and any("已写入 monitor_error 标记" in l for l in log2.splitlines())
           and not any("放弃 monitor_error 写入" in l for l in log2.splitlines()),
           f"卡到通知={seq2.get('reached_notify')}，"
           f"账本 monitor_error={batch2.get('monitor_error')!r}，"
           f"已写入日志={'已写入 monitor_error 标记' in log2}，"
           f"放弃写入日志={'放弃 monitor_error 写入' in log2}，errors={errors2!r}")

    # ---- S6j-3：结构 —— 判定与写盘同锁，且告警在锁外
    # ---- S6j-1c：退出告警必须按**代次**陈述（第十一轮复审口径2）
    # 新代次已在本条发送期间完成接管，旧文案"该批次监控已终止"会把"旧代次已死"
    # 说成"新代次也停了"，误导人工处置。
    exit_msgs = [t for _, t in fake.sent if "监控线程异常退出" in t]
    report("S6j-1c 退出告警按代次陈述：不说'该批次监控已终止'，要求按当前登记代次核实",
           bool(exit_msgs)
           and not any("该批次监控已终止" in t for t in exit_msgs)
           and all("退出代次" in t and "本代次" in t and "当前登记代次" in t
                   for t in exit_msgs),
           f"退出告警条数={len(exit_msgs)}，"
           f"仍含旧文案'该批次监控已终止'="
           f"{any('该批次监控已终止' in t for t in exit_msgs)}，"
           f"含'退出代次'={any('退出代次' in t for t in exit_msgs)}，"
           f"含'本代次'={any('本代次' in t for t in exit_msgs)}，"
           f"含'当前登记代次'={any('当前登记代次' in t for t in exit_msgs)}")

    src, tree = _s6j_source_tree()
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef)
               and n.name == "_write_monitor_error_if_owner"), None)
    locked = []
    if fn is not None:
        locked = [n for n in fn.body
                  if isinstance(n, ast.With)
                  and any("'_active_monitors_lock'" in ast.dump(item.context_expr)
                          for item in n.items)]
    guard_lines = [n.lineno for n in ast.walk(locked[0]) if isinstance(n, ast.Return)] \
        if locked else []
    save_calls = [n for n in ast.walk(locked[0]) if isinstance(n, ast.Call)
                  and isinstance(n.func, ast.Attribute)
                  and n.func.attr == "save_batch_state"] if locked else []
    gen_reads = [n for n in ast.walk(locked[0]) if isinstance(n, ast.Attribute)
                 and n.attr == "_active_monitor_generations"] if locked else []
    order_ok = bool(guard_lines and save_calls
                    and min(guard_lines) < min(c.lineno for c in save_calls)
                    and gen_reads)
    # 告警必须在临界区外：整个方法体内不该有 send_tg_notification
    no_net_in_helper = not any(isinstance(n, ast.Call)
                               and isinstance(n.func, ast.Attribute)
                               and n.func.attr == "send_tg_notification"
                               for n in ast.walk(fn)) if fn else False
    # except 分支里的告警不得被任何 _active_monitors_lock 包住
    handler_ok = False
    for n in ast.walk(tree):
        if not isinstance(n, ast.Try):
            continue
        for h in n.handlers:
            seg = ast.get_source_segment(src, h) or ""
            if "监控线程异常退出" not in seg:
                continue
            locked_h = [w for w in ast.walk(h)
                        if isinstance(w, ast.With)
                        and any("'_active_monitors_lock'" in ast.dump(i.context_expr)
                                for i in w.items)]
            handler_ok = not locked_h
    report("S6j-3 结构：判定与 save_batch_state 同在代次锁内、且锁内零网络",
           len(locked) == 1 and order_ok and no_net_in_helper and handler_ok,
           f"方法内 _active_monitors_lock 临界区数={len(locked)}，"
           f"判定在写盘之前且读了代次登记={order_ok}，"
           f"锁外告警（helper 无 send_tg_notification）={no_net_in_helper}，"
           f"except 内告警未被代次锁包住={handler_ok}")




def _s6k_run(send_return, save_return=True, finish_return=True):
    """所有者代次异常退出；critical 返回值与写盘返回值都**可指定**。

    第十一轮复审要钉的两条"成功判定"口径：
      · 告警 —— 只有 `send_tg_notification` 返回 `True` 才算确认送达，才能记
        `failure_alerted`；返回 `False`（超时/请求失败）或 `None`（未配置 TG）
        都必须保留 `_finish_monitor_takeover` 的补充告警资格。
      · 写盘 —— 只有 `save_batch_state` 返回 `True` 才算落盘成功。
      · finish_return —— 收尾补发（第 2 次尝试）的返回值也可指定，用于钉死
        "两次都没送达时只有终局日志、没有第 3 次"这条口径（S6k-5）。
    返回 (states, saves, 日志, fake, lifecycle, errors)。
    """
    tag = "%s_s%s" % (str(send_return).lower(), str(save_return).lower())
    d = tempfile.mkdtemp(prefix="s6k_%s_" % tag)
    state_path = os.path.join(d, "trade_state.json")
    trader_260725.STATE_FILE = state_path
    try:
        _seed(state_path)
        states = _disk(state_path)
        fake = _make_fake(state_path, states)
        # ?????load_all_states ????????**???**????????
        # ???????helper ??????????????? ?? ??
        # ???????????????????????????
        fake.load_all_states = lambda: {s: {b: dict(v) for b, v in m.items()}
                                        for s, m in states.items()}
        life = _takeover_lifecycle(fake, "gen-s6k")
        fake.exchange.fetch_open_orders.return_value = [
            {"id": ENTRY_ID, "status": "open"}]

        saves = []

        def durable_save(symbol, batch_id, data, **_k):
            if save_return is not True:
                return save_return        # 未确认落盘：连记录都不该有
            states.setdefault(symbol, {})[batch_id] = dict(data)
            saves.append(dict(data))
            return save_return
        fake.save_batch_state = durable_save

        _real_send = fake.send_tg_notification

        def send(*a, **kw):
            r = _real_send(*a, **kw)      # 照常记录，便于断言"确实尝试发送"
            _t = str(a[0]) if a else ""
            if "监控线程异常退出" in _t:
                return send_return
            if "未能证明安全终结" in _t:
                return finish_return       # 收尾补发（第 2 次尝试）可控
            return r
        fake.send_tg_notification = send

        n = {"i": 0}

        def _sleep(sec):
            n["i"] += 1
            if n["i"] == 1:
                return                    # 第一轮完整跑完
            raise RuntimeError("模拟：所有者代次从 sleep 抛出普通异常")

        buf = io.StringIO()
        errors = []
        with mock.patch.object(trader_260725.time, "sleep", _sleep), \
                mock.patch.object(trader_260725, "current_instance_id",
                                  return_value="s6-test"), \
                mock.patch.object(trader_260725, "write_progress",
                                  return_value=None), \
                contextlib.redirect_stdout(buf):
            try:
                fake._run_monitor_takeover(life, **_monitor_kwargs())
            except BaseException as e:    # noqa: BLE001 - 交给断言暴露
                errors.append(e)
        return states, saves, buf.getvalue(), fake, life, errors
    finally:
        trader_260725.STATE_FILE = _real_state_file


def check_s6_alert_and_write_confirm_use_strict_success():
    """第十一轮复审口径1+3：告警与写盘的"成功"都只认 `True`。

    告警侧的危害是**沉默丢告警**：`failure_alerted` 被记成 True 后，
    `_finish_monitor_takeover` 的 `should_alert = not failure_alerted` 会跳过补充告警，
    于是 `send_tg_notification` 返回 False/None 的那一轮 critical 就没有任何兜底。
    写盘侧同理：`save_batch_state` 契约写明仅返回 True 表示已持久化，
    把 None 当成功会让"落盘失败"看起来像"已写入 monitor_error"。
    """
    # ---- 告警返回 False / None / True 三档
    detail = {}
    for ret in (False, None, True):
        _, saves_r, log_r, fake_r, life_r, err_r = _s6k_run(send_return=ret)
        alerted = life_r.get("failure_alerted")
        finish = any("未能证明安全终结" in t for _, t in fake_r.sent)
        undelivered = "未确认送达" in log_r
        exit_sent = any("监控线程异常退出" in t for _, t in fake_r.sent)
        detail[ret] = (alerted, finish, undelivered, exit_sent, err_r)
        if ret is True:
            ok = (alerted is True and not finish and not undelivered
                  and exit_sent and not err_r)
            report("S6k-3 阳性对照：send 返回 True → 记 failure_alerted、"
                   "接管收尾不再补发",
                   ok,
                   f"failure_alerted={alerted!r}，补发告警={finish}，"
                   f"未确认送达日志={undelivered}，退出告警已发={exit_sent}，"
                   f"errors={err_r!r}")
        else:
            ok = (alerted is not True and finish and undelivered
                  and exit_sent and not err_r)
            name = "S6k-1" if ret is False else "S6k-2"
            report(f"{name} send 返回 {ret!r} → 不记 failure_alerted、"
                   f"保留接管收尾补发资格",
                   ok,
                   f"failure_alerted={alerted!r}，补发告警={finish}，"
                   f"未确认送达日志={undelivered}，退出告警已尝试={exit_sent}，"
                   f"errors={err_r!r}")

    # ---- 写盘返回非 True → 不得记成"已写入 monitor_error"
    states_n, saves_n, log_n, fake_n, life_n, err_n = _s6k_run(
        send_return=True, save_return=None)
    batch_n = states_n.get(SYMBOL, {}).get(BATCH, {})
    report("S6k-4 save_batch_state 返回 None → 判 persist_failed、"
           "不记'已写入'、账本无 monitor_error",
           batch_n.get("monitor_error") is not True
           and not saves_n
           and not any("已写入 monitor_error 标记" in l for l in log_n.splitlines())
           and any("落盘被拒" in l for l in log_n.splitlines())
           and not err_n,
           f"账本 monitor_error={batch_n.get('monitor_error')!r}，"
           f"已确认落盘次数={len(saves_n)}，"
           f"'已写入'日志={'已写入 monitor_error 标记' in log_n}，"
           f"'落盘被拒'日志={'落盘被拒' in log_n}，errors={err_n!r}")

    # ---- S6k-5：两次 critical 都没确认送达时的**终局口径**
    # 链路只有两次尝试：异常分支退出告警（第 1 次）+ _finish_monitor_takeover
    # 补发（第 2 次）。补发仍失败则**没有**第 3 次、也没有持久化重试队列 ——
    # 因此必须留下一条把该事实写死的日志，否则"已经补发过"会被读成"已送达"。
    _, _, log5, fake5, life5, err5 = _s6k_run(send_return=False,
                                              finish_return=False)
    exit_n = sum(1 for _, m in fake5.sent if "监控线程异常退出" in m)
    fin_n = sum(1 for _, m in fake5.sent if "未能证明安全终结" in m)
    terminal = [l for l in log5.splitlines()
                if "仍未确认送达" in l and "此后无自动重试" in l]
    report("S6k-5 两次 critical 均未确认送达 → 恰好 2 次尝试 + 终局日志"
           "（明确无第 3 次重试）",
           life5.get("failure_alerted") is not True
           and exit_n == 1 and fin_n == 1 and len(terminal) == 1 and not err5,
           f"failure_alerted={life5.get('failure_alerted')!r}，"
           f"退出告警={exit_n} 次，收尾告警={fin_n} 次，"
           f"终局日志={len(terminal)} 条，errors={err5!r}")


CHECKS = [
    check_t1_recovers_and_places_sl_after_failed_round,
    check_t2_persistent_failure_alerts,
    check_s6_first_poll_failure_keeps_gate,
    check_s6_complete_first_business_cycle,
    check_s6_normal_terminal_requires_durable_proof,
    check_s6_unclassified_exception_keeps_gate,
    check_s6_old_generation_exit_is_ignored,
    check_s6_wrong_stop_type_blocks_takeover,
    check_s6_amount_none_needs_close_position,
    check_s6_old_generation_exit_runs_no_cleanup_side_effects,
    check_s6_exception_side_effects_are_generation_gated,
    check_s6_monitor_error_write_is_atomic,
    check_s6_alert_and_write_confirm_use_strict_success,
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
