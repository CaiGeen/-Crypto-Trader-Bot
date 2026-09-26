#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""保护单写盘门禁的离线注入测试（第二十轮）。

## 背景

已登记缺陷（一项根因、两处门禁）：**保护单路径的「Intent Before Side Effect」
不变量只有注释、没有代码强制**，而入场路径有（L6012 `save_batch_state(...) is not True`
→ Fail-Closed、零 `create_order`）。

- **T3 / 首次+兜底 SL**：挂止损前先 `_update_registry(state='PENDING_CREATE', intent=...)`，
  但 `_update_registry` 内部**不检查** `_persist_states()` 返回值，调用方也不检查
  → 意图写盘失败时**仍然 `create_order`**，磁盘上没有可靠锚点。
  ✅ **C1/G1 已落地**：7 个 SL 创建入口改走 `_update_registry_checked`，见下 N1/N1a。
- **T4 → ✅ N2 / N2a / N3（C2/G2 已落地）**：`_commit_protection_with_g3` 在持锁段内直写
  `entry['state']='CONFIRMED'` 后 `self._persist_states(all_states)` **原先不检查返回** →
  写盘失败仍 `return 'committed'`。✅ **C2 已落地**：写盘未确认返回第三态
  `('persist_failed', order_id)`，**四个消费点逐一显式接管**（见下 N2/N2a）。

## 本文件的性质：G1、G2 **均已落地**——全部为负测（全绿 = 两处缺陷已修）

### ✅ N1 / N1a：G1 负测（检验**期望行为**，全绿 = G1 生效）

- **N1** 注入意图写盘失败 → **零 STOP_MARKET `create_order`** + 锁外 critical + 磁盘无 `PENDING_CREATE` 锚点。
  ⚠️ 计数**只看 STOP_MARKET**：TP 创建走的是未门禁的独立路径（契约 §24.2 登记为相邻缺陷），
  本测会把「意图写盘失败但 TP 仍被创建」如实打印出来，但不作为 N1 的失败依据。
- **N1a** 结构断言：**7 个 SL 创建入口全部**走 `_update_registry_checked`（防后续新增入口绕过门禁）。

### ✅ N2 / N2a / N3：G2 负测（检验**期望行为**，全绿 = G2 生效）

- **N2** 注入**确认**写盘失败 → 返回**第三态**且**携带 `order_id`**；上层
  `_verify_and_update_registry`（**锁外**）发 critical、措辞含「交易所可能已有该单」；磁盘**无** `CONFIRMED`。
- **N2a** 四个 G3 消费点**分别**注入 → 统一入口**不**返回 `'success'`；首次 SL / 兜底 SL / TP 三处
  **不落入 `else`**（不写 `current_sl_id`/`tp_order_id`、**不调 `_gate_alert_clear`**、不打印成功文案）。
- **N3** N2 之后再创建同一 identity → 既有 `_assert_create_allowed` **拒绝**，理由含 `PENDING_CREATE`。

> ⚠️ N2 的注入**不能**沿用 N1 的 `os.replace` 全量失败——那会把**意图**写盘一起拦掉，
> 根本走不到 create 与 commit。故改用**按内容判别**：只有 payload 含 `CONFIRMED` 才失败。
>
> 📌 **报告行编号约定**：契约中 **N1、N1a、N2、N2a** 是四个**各自独立**的验收项。
> 为免把 N1 的子条件误读成 N1a：N1 的三个期望条件报为 `N1-1`（零下单）/
> `N1-2`（锁外 critical）/ `N1-3`（磁盘无锚点），契约 N1a 报为 `N1a`（结构断言）；
> N2 的三个期望条件报为 `N2-1`/`N2-2`/`N2-3`，契约 N2a 的四个消费点报为
> `N2a-1`..`N2a-4`。**连字符 = 「该项的第几个条件」；`N2a-N` = 「四个消费点的第几个」。**

### ⏳ T3d 仍为表征测试（属 §24.6「重启收编未获保证」，**不在本批次**）

T3d 钉住「磁盘无锚点 → 重启后无法收编交易所已存在的单」，该口径须单独负测。

> ⚠️ **不能只凭本文件 rc=0 判定「止损写盘安全已验证」**——TP 的**创建**仍走未门禁路径
> （§24.2 相邻缺陷），本测只覆盖 G1 的 SL 创建门禁 + G2 的确认写盘。

## 注入方式

`_persist_states` 的写盘链是 `NamedTemporaryFile → flush → os.fsync → os.replace`（L2519-2524），
任何异常 → 打印并 `return False`（L2527-2529）。故 patch `os.replace` 抛错即可**确定性**注入
写盘失败，无需真的损坏磁盘。`STATE_FILE` 全程重定向到临时目录，**不触碰生产 `trade_state.json`**。

## 脚手架来源

helper 绑定清单沿用 `test_b1_state_machine.py` 的 `_bind_helpers`（含它记录的
「MagicMock 未绑定 → 自动 mock 返回值 → 路径被 except 吞」陷阱），但**必须绑真实
`_persist_states` / `_update_registry` / `_state_lock`**——现有 fake 都把持久化桩掉了
（`test_b1_state_machine.py:74`「states 为共享引用，无需落盘」），那样注入不到写盘失败。

零网络：`_safe_api_call` 透传到 MagicMock 交易所；不连 Telegram、不连交易所。

跑法：`.venv\\Scripts\\python.exe test_protection_write_gates.py`（rc=0 即全过）
"""

import contextlib
import io
import json
import os
import re
import sys
import tempfile
import threading
import time
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import trader_260725  # noqa: E402
from trader_260725 import CryptoTrader  # noqa: E402

SYMBOL = "BTC/USDT:USDT"
BATCH = "batch_wgate_001"
IDENT_SL = f"{BATCH}|SL|L0|LONG"

RESULTS = []
_real_state_file = trader_260725.STATE_FILE


def report(name, passed, detail=""):
    RESULTS.append((name, passed))
    print(f"[{'PASS' if passed else 'FAIL'}] {name}\n        {detail}")


# --------------------------------------------------------------------------
# 脚手架
# --------------------------------------------------------------------------

def _fresh_state_file():
    """把 STATE_FILE 重定向到全新临时目录，返回 (dir, path)。"""
    d = tempfile.mkdtemp(prefix="wgate_")
    path = os.path.join(d, "trade_state.json")
    trader_260725.STATE_FILE = path
    return d, path


def _restore_state_file():
    trader_260725.STATE_FILE = _real_state_file


def _seed_batch(state_path, **over):
    b = {
        "is_active": True,
        "batch_id": BATCH,
        "symbol": SYMBOL,
        "side": "BUY",
        "entry_orders": ["e1"],
        "stop_steps": [55000.0],
        "take_profit_price": 60000.0,
        "current_sl_id": None,
        "tp_order_id": None,
        "close_phase": 0,
        "pending_sl_orders": [],
        "protection_registry": {},
    }
    b.update(over)
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump({SYMBOL: {BATCH: b}}, f, ensure_ascii=False, indent=2)
    return b


def _read_disk(state_path):
    with open(state_path, encoding="utf-8") as f:
        return json.load(f)


def _make_fake(state_path, states):
    """MagicMock 假实例 + 真实持久化/锁/registry 语义。"""
    fake = mock.MagicMock()
    fake._safe_api_call = lambda fn, *a, **k: fn(*a, **k)
    ex = mock.MagicMock()
    ex.amount_to_precision.side_effect = lambda s, v: v
    ex.price_to_precision.side_effect = lambda s, v: v
    ex.fetch_order.return_value = {"id": "sl_ex_1", "status": "NEW"}
    ex.create_order.return_value = {"id": "sl_ex_1"}
    ex.fetch_open_orders.return_value = [{"id": "sl_ex_1", "status": "NEW"}]
    fake.exchange = ex

    fake.sent = []
    fake.send_tg_notification = lambda text, **kw: fake.sent.append(
        (kw.get("level", "info"), str(text)))

    # ⚠️ MagicMock 陷阱（见 test_b1_state_machine.py:48）：不显式置 0 会被自动 mock 吞掉
    fake._api_cooldown_until = 0
    fake._process_start_ts = time.time()
    fake._state_corrupted = False
    fake._state_corruption_detail = ""
    fake._defer_state_corrupt_alert = False

    # 真实锁 + 真实落盘（本文件与既有 fake 的唯一区别）
    fake._state_lock = threading.Lock()
    fake.load_all_states = lambda: states
    fake._persist_states = lambda all_s: CryptoTrader._persist_states(fake, all_s)
    fake._update_registry = lambda s, b, i, **f: CryptoTrader._update_registry(fake, s, b, i, **f)
    # ⚠️ C1/G1 三条包装**必须**绑真实实现（第 8 次 MagicMock 陷阱）：
    # 漏绑 → MagicMock 返回值不是 True → `is not True` 恒成立 → 门禁在**无注入**时也拦下单，
    # 对照组会假红、负测会假绿。_converge_alert 是告警唯一出口，不绑则 critical 记不进 fake.sent。
    fake._update_registry_locked = (
        lambda s, b, i, **f: CryptoTrader._update_registry_locked(fake, s, b, i, **f))
    fake._update_registry_checked = (
        lambda s, b, i, **f: CryptoTrader._update_registry_checked(fake, s, b, i, **f))
    fake._converge_alert = (
        lambda key, msg, level='critical': CryptoTrader._converge_alert(fake, key, msg, level=level))

    for name in ("_protection_identity", "_build_intent", "_order_matches_intent",
                 "_assert_create_allowed", "_final_pre_create_check",
                 "_commit_protection_with_g3", "_g3a_converge_race_order",
                 "_verify_order_created", "_classify_create_exception",
                 "_verify_and_update_registry", "_recheck_registry_self_heal",
                 "_is_stale_pre_launch_entry"):
        if hasattr(CryptoTrader, name):
            setattr(fake, name, (lambda n=name: lambda *a, **k: getattr(CryptoTrader, n)(fake, *a, **k))())
    return fake


def _layer_sl_params(amount=0.43, stop=55000.0):
    return [{
        "symbol": SYMBOL,
        "type": "STOP_MARKET",
        "side": "sell",
        "amount": amount,
        "params": {"stopPrice": stop, "reduceOnly": True, "workingType": "MARK_PRICE"},
    }]


def _tp_params():
    return {"symbol": SYMBOL, "type": "TAKE_PROFIT_MARKET", "side": "sell",
            "params": {"stopPrice": 60000.0, "reduceOnly": True}}


def _params_base():
    return {"leverage": 100}


# --------------------------------------------------------------------------
# N1（G1 负测）：意图写盘失败 → 零 STOP_MARKET 下单 / 锁外 critical / 磁盘无锚点
# --------------------------------------------------------------------------

def check_n1_intent_write_failure_blocks_create():
    """N1（契约 §24.4）：注入意图写盘失败，期望 G1 门禁 Fail-Closed 零下单。"""
    d, state_path = _fresh_state_file()
    try:
        _seed_batch(state_path)
        states = _read_disk(state_path)
        fake = _make_fake(state_path, states)

        with mock.patch("os.replace", side_effect=OSError(13, "Permission denied")):
            CryptoTrader._place_prepared_orders_immediately(
                fake, SYMBOL, BATCH, 0, 0.43, _tp_params(), _layer_sl_params(),
                False, _params_base(), [55000.0])

        # ⚠️ 只统计止损单：TP 创建走未门禁的独立路径（§24.2 相邻缺陷），不计入本测
        sl_creates = [c for c in fake.exchange.create_order.call_args_list
                      if c.kwargs.get("type") == "STOP_MARKET"]
        all_types = [c.kwargs.get("type") for c in fake.exchange.create_order.call_args_list]
        disk = _read_disk(state_path)
        reg = disk.get(SYMBOL, {}).get(BATCH, {}).get("protection_registry", {})
        anchored = any(v.get("state") == "PENDING_CREATE" for v in reg.values())
        crit = [m for lvl, m in fake.sent if lvl == "critical"]
        gate_ran = any("意图未写入磁盘" in m for m in crit)

        report(
            "N1-1 意图写盘失败 → 零 STOP_MARKET create_order（G1 Fail-Closed）",
            len(sl_creates) == 0,
            f"STOP_MARKET 下单={len(sl_creates)} 次；全部下单类型={all_types}。"
            f"⚠️ 若出现 TAKE_PROFIT_MARKET，属 §24.2 已登记的 TP 相邻缺陷，不计入本测")

        report(
            "N1-2 意图写盘失败 → 已发锁外 critical（且证明门禁确实执行到）",
            gate_ran,
            f"critical 记录={crit}（须含『意图未写入磁盘』——否则是门禁根本没跑到的假绿）")

        report(
            "N1-3 意图写盘失败 → 磁盘无 PENDING_CREATE 锚点（写入确实失败）",
            anchored is False,
            f"protection_registry={ {k: v.get('state') for k, v in reg.items()} }")
    finally:
        _restore_state_file()


# --------------------------------------------------------------------------
# N1a（结构）：7 个 SL 创建入口必须全部走 checked，防止新增入口绕过门禁
# --------------------------------------------------------------------------

def check_n1a_all_sl_entrances_gated():
    """静态断言：所有 `role='SL'` 的 `state='PENDING_CREATE'` 写入都必须经
    `_update_registry_checked`。逐点驱动 `_start_monitoring` 深层分支成本极高
    （该路径有 5 个已记录的 MagicMock 陷阱），故用结构断言覆盖全部 7 处，
    行为正确性由 N1（首次/兜底）实证。"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trader_260725.py")
    lines = open(path, encoding="utf-8").read().split("\n")
    gated, bad = 0, []
    for i, s in enumerate(lines, 1):
        if "state='PENDING_CREATE'" not in s:
            continue
        block = "\n".join(lines[i - 1:i + 12])
        if "role='SL'" not in block:
            continue
        caller = None
        for j in range(i - 1, max(i - 8, 0), -1):
            m = re.search(r"self\._update_registry(_checked)?\(", lines[j])
            if m:
                caller = m.group(0)
                break
        if caller == "self._update_registry_checked(":
            gated += 1
        else:
            bad.append("L%d(%s)" % (i, caller))

    report(
        "N1a 全部 role='SL' 意图写入都走 _update_registry_checked（当前应 7 处）",
        not bad and gated == 7,
        f"已门禁={gated}；未门禁={bad}。任一 SL 入口绕过门禁即失败；"
        f"新增 SL 入口也会因 gated != 7 而被逼审")


# --------------------------------------------------------------------------
# T3b：重启后能否识别交易所已存在的止损单
# --------------------------------------------------------------------------

def check_t3_restart_cannot_reconcile_orphan_sl():
    d, state_path = _fresh_state_file()
    try:
        _seed_batch(state_path)
        states = _read_disk(state_path)
        fake = _make_fake(state_path, states)
        with mock.patch("os.replace", side_effect=OSError(13, "Permission denied")):
            CryptoTrader._place_prepared_orders_immediately(
                fake, SYMBOL, BATCH, 0, 0.43, _tp_params(), _layer_sl_params(),
                False, _params_base(), [55000.0])

        # 模拟重启：新实例读同一份磁盘（磁盘上没有 PENDING_CREATE / CONFIRMED），
        # 交易所上那张单存在。
        states2 = _read_disk(state_path)
        fake2 = _make_fake(state_path, states2)
        reg2 = states2.get(SYMBOL, {}).get(BATCH, {}).get("protection_registry", {})
        try:
            CryptoTrader._recheck_registry_self_heal(fake2, SYMBOL, BATCH)
            healed = True
        except Exception as e:
            healed = False
            heal_err = f"{type(e).__name__}: {e}"
        after = _read_disk(state_path)
        reg_after = after.get(SYMBOL, {}).get(BATCH, {}).get("protection_registry", {})

        report(
            "T3d 重启后自愈：磁盘无锚点 → 无法收编交易所已存在的 SL（当前行为）",
            len(reg_after) == len(reg2),
            f"自愈前 registry 条目={len(reg2)}，自愈后={len(reg_after)}，"
            f"调用未抛异常={healed}。交易所侧 fetch_order 已被调用="
            f"{fake2.exchange.fetch_order.call_count} 次。"
            f"结论：没有 identity/intent 锚点，重启后**无机制**把这张单收编回账本")
    finally:
        _restore_state_file()


# --------------------------------------------------------------------------
# N2 / N2a / N3（G2 负测，契约 §24.4）：注入**确认**写盘失败
# --------------------------------------------------------------------------
#
# 与 N1 的注入区别：N1 打断的是**意图**写盘（`os.replace` 全量失败 → 门禁把单拦在
# create **之前**）。N2 必须让 create **真的发生**、只让**确认**那一次写盘失败，
# 否则走不到 commit，测的是 G1 而不是 G2。故按 payload **内容**判别而非调用序号——
# 序号依赖路径顺序，路径一改就漂移成假绿。


def _inject_confirm_write_failure(fake):
    """只让含 CONFIRMED 的落盘失败；意图写盘（PENDING_CREATE 等）照常成功。"""
    real = fake._persist_states

    def _persist(all_s):
        for bs in (all_s or {}).values():
            for b in bs.values():
                if not isinstance(b, dict):
                    continue
                for e in (b.get("protection_registry") or {}).values():
                    if isinstance(e, dict) and e.get("state") == "CONFIRMED":
                        return False
        return real(all_s)

    fake._persist_states = _persist


def _seed_pending_registry(state_path, **over):
    reg = {IDENT_SL: {"state": "PENDING_CREATE", "id_known": False,
                      "order_kind": "conditional", "role": "SL", "layer": 0,
                      "side": "LONG", "intent": None}}
    reg.update(over.pop("protection_registry", {}))
    return _seed_batch(state_path, protection_registry=reg, **over)


def _run_consumer(key, seed_over, idx, inject, bind_tp=False):
    """跑一次完整下单并返回观察结果（N2a 成对跑的共用驱动）。

    `inject` 只让**确认**那次写盘失败（`_inject_confirm_write_failure`），
    意图写盘照常 → create 真能发生 → 才走到 G3 commit。
    """
    d, state_path = _fresh_state_file()
    try:
        _seed_batch(state_path, protection_registry={}, **seed_over)
        states = _read_disk(state_path)
        fake = _make_fake(state_path, states)
        if bind_tp:
            fake._tp_update_blocked = lambda *a, **k: False
        if inject:
            _inject_confirm_write_failure(fake)

        g3_calls = []
        real_commit = CryptoTrader._commit_protection_with_g3

        def _spy(*a, **k):
            r = real_commit(fake, *a, **k)
            g3_calls.append(r)
            return r
        fake._commit_protection_with_g3 = _spy

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            CryptoTrader._place_prepared_orders_immediately(
                fake, SYMBOL, BATCH, idx, 0.43, _tp_params(), _layer_sl_params(),
                False, _params_base(), [55000.0])
        return {
            "g3": g3_calls,
            "out": buf.getvalue(),
            "saved": _saved_field(fake, key),
            "clears": fake._gate_alert_clear.call_count,
            "crit": sum(1 for lvl, _ in fake.sent if lvl == "critical"),
        }
    finally:
        _restore_state_file()


def _saved_field(fake, key):
    """save_batch_state 被调用时写入的该字段值（fake 未绑 → 自动 mock 记录参数）。"""
    vals = []
    for c in fake.save_batch_state.call_args_list:
        data = c.args[2] if len(c.args) > 2 else None
        if isinstance(data, dict) and data.get(key):
            vals.append(data.get(key))
    return vals


def check_n2_confirm_write_failure_returns_third_state():
    """N2：确认写盘失败 → 第三态 + 携带 order_id + 磁盘无 CONFIRMED + 锁外 critical。"""
    d, state_path = _fresh_state_file()
    try:
        _seed_pending_registry(state_path)
        states = _read_disk(state_path)
        fake = _make_fake(state_path, states)
        _inject_confirm_write_failure(fake)

        ret = CryptoTrader._commit_protection_with_g3(
            fake, SYMBOL, BATCH, IDENT_SL, "sl_ex_1", order_kind="conditional")
        disk = _read_disk(state_path)
        on_disk = disk.get(SYMBOL, {}).get(BATCH, {}).get(
            "protection_registry", {}).get(IDENT_SL, {}).get("state")

        third = isinstance(ret, tuple) and len(ret) == 2 and ret[0] == "persist_failed"
        report(
            "N2-1 确认写盘失败 → 第三态（非 'committed'）且携带 order_id",
            third and ret != "committed" and "sl_ex_1" in ret,
            f"返回={ret!r}（期望 ('persist_failed', 'sl_ex_1')）；"
            f"type={type(ret).__name__}")

        report(
            "N2-2 第三态后磁盘无 CONFIRMED（账本仍停在 PENDING_CREATE）",
            on_disk != "CONFIRMED",
            f"磁盘 state={on_disk!r}；交易所侧订单 id=sl_ex_1 已 verify 成功 → "
            f"『交易所有单、账本无 CONFIRMED』窗口被如实暴露")

        # 上层（锁外）critical：_commit_protection_with_g3 在 _state_lock 内，禁止网络 IO
        fake.sent.clear()
        vret = CryptoTrader._verify_and_update_registry(
            fake, SYMBOL, BATCH, IDENT_SL, "sl_ex_1", desc="首次止损单")
        crit = [m for lvl, m in fake.sent if lvl == "critical"]
        report(
            "N2-3 上层 _verify_and_update_registry 不返回 'success'，锁外 critical 含 order_id 与「交易所可能已有该单」",
            vret != "success" and vret == "persist_failed"
            and any("sl_ex_1" in m for m in crit)
            and any("交易所可能已有该单" in m for m in crit),
            f"返回={vret!r}；critical {len(crit)} 条；"
            f"含 order_id={any('sl_ex_1' in m for m in crit)}；"
            f"含『交易所可能已有该单』={any('交易所可能已有该单' in m for m in crit)}")
    finally:
        _restore_state_file()


def _check_g3_consumer(site, key, seed_over, idx, expect_clear_drop, bind_tp=False):
    """N2a 消费点②③④：**对照 + 注入**成对跑同一个 setup。

    为什么必须带对照组：注入组的『没有成功簿记』只说明 else 没跑——但它**本来可能
    就没跑到**（闸门拦下、条件不满足、TP 链被自动 mock 跳过…），那样注入组再绿也是假绿。
    对照组先证明「该 setup 确实走得到成功 else」，注入组的阴性结果才有意义。

    `_gate_alert_clear` 为什么用**下降**而不是 =0 判：`_assert_create_allowed` 返回 True
    时会自动同 identity 清零一次（L6801/6817/6823），那一次与 G3 的 else 无关，
    断言 =0 在正确实现上也会红。else 那一次是否被跳过，靠『注入 < 对照』来判。
    """
    ctl = _run_consumer(key, seed_over, idx, inject=False, bind_tp=bind_tp)
    inj = _run_consumer(key, seed_over, idx, inject=True, bind_tp=bind_tp)
    third = (len(inj["g3"]) == 1 and isinstance(inj["g3"][0], tuple)
             and len(inj["g3"][0]) == 2 and inj["g3"][0][0] == "persist_failed")

    report(
        f"N2a-{site} 对照组确实走通成功 else（防假绿：否则注入组的『无成功簿记』无意义）",
        ctl["g3"] == ["committed"] and bool(ctl["saved"]) and "已挂出" in ctl["out"],
        f"对照 G3={ctl['g3']}（期望恰为 ['committed']）；save 写入 {key}={ctl['saved']}；"
        f"stdout 含『已挂出』={'已挂出' in ctl['out']}；critical={ctl['crit']}（对照不应有）")

    report(
        f"N2a-{site} 注入后第三态且**不落入 else**（不写 {key} / 不打印成功文案 / critical 已发）",
        third and not inj["saved"] and "已挂出" not in inj["out"] and inj["crit"] > 0,
        f"注入 G3={inj['g3']}（期望 ('persist_failed', <order_id>)）；"
        f"save 写入 {key}={inj['saved']}（期望空，否则 else 簿记仍执行）；"
        f"stdout 含『已挂出』={'已挂出' in inj['out']}（期望 False）；critical={inj['crit']}")

    if expect_clear_drop:
        report(
            f"N2a-{site} else 内的 _gate_alert_clear 未执行"
            f"（注入 {inj['clears']} 次 < 对照 {ctl['clears']} 次）",
            inj["clears"] < ctl["clears"],
            f"对照={ctl['clears']}（闸门自动清零 1 次 + else 清零 1 次）；"
            f"注入={inj['clears']}（应只剩闸门那 1 次）。"
            f"两者相等 = else 照样跑了，**既有 FAILED 告警额度仍会被清掉**")


def check_n2a_1_unified_entry_consumer():
    """消费点① L6321 `_verify_and_update_registry`：第三态**不得** return 'success'。"""
    d, state_path = _fresh_state_file()
    try:
        _seed_pending_registry(state_path)
        states = _read_disk(state_path)
        fake = _make_fake(state_path, states)
        _inject_confirm_write_failure(fake)
        ret = CryptoTrader._verify_and_update_registry(
            fake, SYMBOL, BATCH, IDENT_SL, "sl_ex_1", desc="首次止损单")
        crit = [m for lvl, m in fake.sent if lvl == "critical"]
        report(
            "N2a-1 统一入口 _verify_and_update_registry（if 在 L6332）不返回 'success'，且锁外 critical 已发",
            ret != "success" and ret == "persist_failed" and bool(crit),
            f"返回={ret!r}；critical={len(crit)} 条")
    finally:
        _restore_state_file()


def check_n2a_2_first_sl_consumer():
    """消费点② 首次/预生成 SL：`current_sl_id is None` + `idx < len(layer_sl_params)`。
    本消费点的 else **本来就不调** `_gate_alert_clear`（契约 §24.3 表 #2），故不查下降。"""
    _check_g3_consumer(2, "current_sl_id",
                       dict(current_sl_id=None, tp_order_id=None),
                       idx=0, expect_clear_drop=False)


def check_n2a_3_fallback_sl_consumer():
    """消费点③ 兜底 SL：`current_sl_id is None` + L9917 `else:`（`idx >= len(layer_sl_params)`）。

    ⚠️ 触发条件是 **idx 越过层参数表**，不是「current_sl_id 已有」——设了 current_sl_id
    会走 L10097 的最外层 else，只打印「已存在止损单，等待主循环合并更新」就返回，
    兜底路径根本不会创建（探针实测坐实过这一版错误 setup）。
    """
    _check_g3_consumer(3, "current_sl_id",
                       dict(current_sl_id=None, tp_order_id=None),
                       idx=1, expect_clear_drop=True)


def check_n2a_4_tp_consumer():
    """消费点④ 预生成 TP：`current_sl_id` 已有 → SL 走最外层 else 跳过，本例只打到 TP。

    ⚠️ 必须绑 `_tp_update_blocked=False`（`test_sg3_p1.py` 已记录的 MagicMock 陷阱）：
    未绑 → 自动 mock 返回 truthy → TP 恢复链整体跳过，本消费点**触达不到**（假绿）。
    """
    _check_g3_consumer(4, "tp_order_id",
                       dict(current_sl_id="sl_ex_0", tp_order_id=None),
                       idx=0, expect_clear_drop=True, bind_tp=True)


def check_n3_gate_blocks_recreate_after_persist_failure():
    """N3：N2 之后再创建同一 identity → 既有闸门拒绝，理由含 PENDING_CREATE（磁盘锚点仍在）。"""
    d, state_path = _fresh_state_file()
    try:
        _seed_pending_registry(state_path)
        states = _read_disk(state_path)
        fake = _make_fake(state_path, states)
        _inject_confirm_write_failure(fake)
        CryptoTrader._commit_protection_with_g3(
            fake, SYMBOL, BATCH, IDENT_SL, "sl_ex_1", order_kind="conditional")

        # ⚠️ 从**磁盘**重建实例再判闸门：本 fake 的 load_all_states 返回同一份内存 dict，
        # 上一步已把它原地改成 CONFIRMED（磁盘并未落）。用内存态会把「磁盘锚点仍在」
        # 误读成 CONFIRMED，测的就不是 N3 要的那件事了。
        disk = _read_disk(state_path)
        on_disk = disk.get(SYMBOL, {}).get(BATCH, {}).get(
            "protection_registry", {}).get(IDENT_SL, {}).get("state")
        fake2 = _make_fake(state_path, disk)
        allowed, reason = CryptoTrader._assert_create_allowed(fake2, SYMBOL, BATCH, IDENT_SL)

        report(
            "N3 确认写盘失败后再次创建同一 identity → 闸门拒绝且理由含 PENDING_CREATE",
            on_disk == "PENDING_CREATE" and allowed is False
            and "PENDING_CREATE" in str(reason),
            f"磁盘 state={on_disk!r}；闸门 allowed={allowed}；reason={reason!r}")
    finally:
        _restore_state_file()


# --------------------------------------------------------------------------
# 对照组：写盘正常时，两处门禁都应正常落盘（证明注入本身有效）
# --------------------------------------------------------------------------

def check_control_persist_works_without_injection():
    d, state_path = _fresh_state_file()
    try:
        _seed_batch(state_path)
        states = _read_disk(state_path)
        fake = _make_fake(state_path, states)
        CryptoTrader._place_prepared_orders_immediately(
            fake, SYMBOL, BATCH, 0, 0.43, _tp_params(), _layer_sl_params(),
            False, _params_base(), [55000.0])
        disk = _read_disk(state_path)
        reg = disk.get(SYMBOL, {}).get(BATCH, {}).get("protection_registry", {})
        states_seen = [v.get("state") for v in reg.values()]
        # 严格钉住「PENDING_CREATE → CONFIRMED」完整链：registry 每个 identity 只有一条、
        # 原地覆盖（L6619 只刷新 updated_at），所以成功链的**终态**必须是 CONFIRMED。
        # 早期版本写成 `PENDING_CREATE or CONFIRMED`，那太松——停在 PENDING_CREATE
        # 也算过，而那正是 T4 注入后的状态。
        report(
            "C1 对照组：无注入时 PENDING_CREATE → CONFIRMED 完整链落盘（证明注入有效）",
            states_seen == ["CONFIRMED"],
            f"registry 各条 state={states_seen}（期望恰为 ['CONFIRMED']：意图已推进到确认），"
            f"create_order 调用={fake.exchange.create_order.call_count} 次")
    finally:
        _restore_state_file()


CHECKS = [
    check_control_persist_works_without_injection,
    check_n1_intent_write_failure_blocks_create,
    check_n1a_all_sl_entrances_gated,
    check_t3_restart_cannot_reconcile_orphan_sl,
    check_n2_confirm_write_failure_returns_third_state,
    check_n2a_1_unified_entry_consumer,
    check_n2a_2_first_sl_consumer,
    check_n2a_3_fallback_sl_consumer,
    check_n2a_4_tp_consumer,
    check_n3_gate_blocks_recreate_after_persist_failure,
]


def main():
    failed = 0
    for fn in CHECKS:
        try:
            fn()
        except Exception as e:
            failed += 1
            import traceback
            traceback.print_exc()
            report(fn.__name__, False, f"测试自身异常: {type(e).__name__}: {e}")
    ok = sum(1 for _, p in RESULTS if p)
    print("\n" + "=" * 68)
    print(f"保护单写盘门禁注入测试: {ok}/{len(RESULTS)} 通过")
    print("=" * 68)
    return 0 if failed == 0 and ok == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
