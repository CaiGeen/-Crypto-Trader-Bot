# -*- coding: utf-8 -*-
"""
D-006: 账户层风控闸门 —— 专项测试（2026-08-28）

已批准限额（用户 2026-08-28 确认）：
  RISK_MAX_ACTIVE_BATCHES = 3（活跃批次总数达 3 拒新批次）
  RISK_MAX_ACTIVE_SYMBOLS = 1（新交易对使带仓交易对数超 1 拒；同 symbol 加仓放行）
  RISK_DAILY_REALIZED_LOSS_LIMIT = 暂不启用（0；机制保留，env 即时开关）
  MAX_LEVERAGE = 100（trader 层硬闸门——此前现役代码无强制，本批补齐）
  RISK_MAX_MARGIN_USAGE_PCT：用户裁决暂不实现

语义约定：
  - 只拦新开仓路径；存量批次止盈止损/平仓/监控不受影响
  - trade_stats.json 损坏 → Fail-Closed（与 D-005 补丁同哲学：未知状态 ≠ 允许）
  - /force 不绕过 D-006（闸门在 trader execute_signal 内，/force 只绕 D-005 去重）
  - 限额调用时读 env 不缓存（<=0 视为禁用）

MagicMock 纪律：本测试用 SimpleNamespace + 真实数值绑定（helper 不依赖 self 状态，
unbound 调用 CryptoTrader._method(fake, ...)）；env 用 os.environ 显式设置并 finally 恢复。
"""
import ast
import json
import os
import sys
import tempfile
from datetime import datetime

import pytz

import trader_260725
from trader_260725 import CryptoTrader

RESULTS = []
BEIJING = pytz.timezone('Asia/Shanghai')
TODAY = datetime.now(BEIJING).strftime("%Y-%m-%d")
SYMBOL = "BTC/USDT:USDT"
SYMBOL2 = "ETH/USDT:USDT"
ENV_KEYS = ("RISK_MAX_ACTIVE_BATCHES", "RISK_MAX_ACTIVE_SYMBOLS",
            "RISK_DAILY_REALIZED_LOSS_LIMIT", "MAX_LEVERAGE")


def report(name, passed, detail=""):
    RESULTS.append((name, passed))
    print(f"{'[PASS]' if passed else '[FAIL]'} {name} {detail}")


class _FakeTrader:
    """helper 均不依赖 self 状态（stats_file 显式传参），unbound 调用即可"""


def _make_fake():
    ft = _FakeTrader()
    if hasattr(CryptoTrader, '_count_active_batches'):
        ft._count_active_batches = lambda states: CryptoTrader._count_active_batches(ft, states)
    if hasattr(CryptoTrader, '_get_today_realized_pnl'):
        ft._get_today_realized_pnl = lambda stats_file=None: CryptoTrader._get_today_realized_pnl(ft, stats_file)
    return ft


def _sig(symbol=SYMBOL, leverage=20):
    return type('S', (), {'symbol': symbol, 'leverage': leverage})()


def _states(symbols_batches):
    """symbols_batches: {symbol: 批次数或批次id列表} → {symbol: {bid: {'is_active': True}}}"""
    return {sym: {f"batch_{sym.split('/')[0]}_{i}": {'is_active': True}
                 for i in range(n if isinstance(n, int) else len(n))}
            for sym, n in symbols_batches.items()}


def _stats_file(trades):
    tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
    json.dump({"trades": trades}, tmp, ensure_ascii=False)
    tmp.close()
    return tmp.name


def _env(**over):
    """显式设置 D-006 相关 env（未给的键删除以测默认值）"""
    for k in ENV_KEYS:
        if k in over:
            os.environ[k] = str(over[k])
        else:
            os.environ.pop(k, None)


def main():
    saved_env = {k: os.environ.get(k) for k in ENV_KEYS}
    tmp_files = []
    try:
        fake = _make_fake()

        # ---------- 语义存在性 ----------
        report("T00 helper 三件套存在（hasattr 纪律）",
               all(hasattr(CryptoTrader, m) for m in
                   ('_count_active_batches', '_get_today_realized_pnl', '_check_account_risk')))

        # ---------- T01/T02 活跃批次上限（默认 3） ----------
        _env()  # 全部用默认值：批次3 / 交易对1 / 日亏0 / 杠杆100
        a1, r1 = CryptoTrader._check_account_risk(fake, _states({SYMBOL: [1, 2]}), _sig())
        a2, r2 = CryptoTrader._check_account_risk(fake, _states({SYMBOL: [1, 2, 3]}), _sig())
        report("T01 批次数 2<3 放行 / 3 达上限拒绝",
               a1 is True and a2 is False and "RISK_MAX_ACTIVE_BATCHES" in r2 and "3" in r2)

        # ---------- T03/T04 交易对上限（默认 1，同 symbol 加仓放行） ----------
        a3, r3 = CryptoTrader._check_account_risk(fake, _states({SYMBOL: [1]}), _sig(symbol=SYMBOL))
        a4, r4 = CryptoTrader._check_account_risk(fake, _states({SYMBOL: [1]}), _sig(symbol=SYMBOL2))
        report("T03 同 symbol 加仓放行 / 新 symbol 超限拒绝",
               a3 is True and a4 is False and "RISK_MAX_ACTIVE_SYMBOLS" in r4)
        # 非活跃批次不计入
        st_inactive = {SYMBOL: {"b_old": {'is_active': False}}}
        a5, _ = CryptoTrader._check_account_risk(fake, st_inactive, _sig(symbol=SYMBOL2))
        report("T04 is_active=False 批次不计入交易对/批次计数", a5 is True)

        # ---------- T05 杠杆硬闸门（默认 100） ----------
        a6, r6 = CryptoTrader._check_account_risk(fake, _states({SYMBOL: [1]}), _sig(leverage=100))
        a7, r7 = CryptoTrader._check_account_risk(fake, _states({SYMBOL: [1]}), _sig(leverage=101))
        a8, r8 = CryptoTrader._check_account_risk(fake, _states({SYMBOL: [1]}), _sig(leverage=0))
        a9, r9 = CryptoTrader._check_account_risk(fake, _states({SYMBOL: [1]}), _sig(leverage=-5))
        bad_lev = type('S', (), {'symbol': SYMBOL, 'leverage': '20x'})()
        a10, r10 = CryptoTrader._check_account_risk(fake, _states({SYMBOL: [1]}), bad_lev)
        report("T05 杠杆 100 放行 / 101 拒 / 0 拒 / 负值拒 / 非法值拒",
               a6 and not a7 and "MAX_LEVERAGE" in r7 and not a8 and not a9
               and not a10 and "非法" in r10)

        # ---------- T06 日亏损门默认禁用（limit=0 不读 stats） ----------
        # 无 stats 文件场景下放行即证明未触发该门（损坏文件也放行 = 门禁用）
        corrupt = os.path.join(tempfile.gettempdir(), "d006_corrupt_no_read.json")
        with open(corrupt, "w", encoding="utf-8") as f:
            f.write("{corrupted !!!")
        tmp_files.append(corrupt)
        a11, _ = CryptoTrader._check_account_risk(fake, _states({SYMBOL: [1]}), _sig(), stats_file=corrupt)
        report("T06 日亏门默认禁用：损坏 stats 也不阻断（未启用不读表）", a11 is True)

        # ---------- T07-T09 日亏损门启用语义 ----------
        _env(RISK_DAILY_REALIZED_LOSS_LIMIT=500)
        f_loss = _stats_file([{"time": f"{TODAY} 10:00:00", "net_pnl": -600.0}])
        f_small = _stats_file([{"time": f"{TODAY} 10:00:00", "net_pnl": -400.0}])
        f_yday = _stats_file([{"time": "2020-01-01 10:00:00", "net_pnl": -900.0}])
        f_mixed = _stats_file([{"time": f"{TODAY} 10:00:00", "net_pnl": -350.0},
                               {"time": f"{TODAY} 11:00:00", "net_pnl": 100.0}])
        tmp_files += [f_loss, f_small, f_yday, f_mixed]
        a12, r12 = CryptoTrader._check_account_risk(fake, _states({SYMBOL: [1]}), _sig(), stats_file=f_loss)
        a13, _ = CryptoTrader._check_account_risk(fake, _states({SYMBOL: [1]}), _sig(), stats_file=f_small)
        a14, _ = CryptoTrader._check_account_risk(fake, _states({SYMBOL: [1]}), _sig(), stats_file=f_yday)
        a15, _ = CryptoTrader._check_account_risk(fake, _states({SYMBOL: [1]}), _sig(), stats_file=f_mixed)
        report("T07 当日亏 600>限 500 拒 / 亏 400 放行",
               a12 is False and "RISK_DAILY_REALIZED_LOSS" in r12 and a13 is True)
        report("T08 昨日亏损不计入（日切边界，北京时间）", a14 is True)
        report("T09 当日多笔求和判定（-350+100=-250 < 500 放行）", a15 is True)

        # ---------- T10/T11 stats 损坏 Fail-Closed（门启用时） ----------
        before = open(corrupt, encoding="utf-8").read()
        a16, r16 = CryptoTrader._check_account_risk(fake, _states({SYMBOL: [1]}), _sig(), stats_file=corrupt)
        after = open(corrupt, encoding="utf-8").read()
        # 根节点非 dict
        f_root = os.path.join(tempfile.gettempdir(), "d006_root_list.json")
        with open(f_root, "w", encoding="utf-8") as f:
            json.dump([1, 2, 3], f)
        tmp_files.append(f_root)
        a17, r17 = CryptoTrader._check_account_risk(fake, _states({SYMBOL: [1]}), _sig(), stats_file=f_root)
        # 文件不存在 = 正常
        a18, _ = CryptoTrader._check_account_risk(
            fake, _states({SYMBOL: [1]}), _sig(),
            stats_file=os.path.join(tempfile.gettempdir(), "d006_not_exist_xxx.json"))
        report("T10 损坏 → 拒且文件不被覆盖 / 根节点非 dict → 拒 / 文件不存在 → 放行",
               a16 is False and "Fail-Closed" in r16 and before == after
               and a17 is False and a18 is True)

        # ---------- T12 禁用语义（显式 0） ----------
        _env(RISK_MAX_ACTIVE_BATCHES=0, RISK_MAX_ACTIVE_SYMBOLS=0, MAX_LEVERAGE=0)
        a19, _ = CryptoTrader._check_account_risk(fake, _states({SYMBOL: [1, 2, 3, 4, 5]}), _sig(symbol=SYMBOL2, leverage=999))
        report("T12 全部限额显式置 0 = 全禁用（含杠杆门）", a19 is True)

        # ---------- T13/T14 源码锚点（AST 核实闸门位置） ----------
        src = open('trader_260725.py', encoding='utf-8').read()
        tree = ast.parse(src)
        gate_line = conflict_line = lev_line = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == 'execute_signal':
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Call):
                        fn = sub.func
                        nm = fn.attr if isinstance(fn, ast.Attribute) else (fn.id if isinstance(fn, ast.Name) else None)
                        if nm == '_check_account_risk' and gate_line is None:
                            gate_line = sub.lineno
                        if nm == '_check_existing_conflicts' and conflict_line is None:
                            conflict_line = sub.lineno
                    if isinstance(sub, ast.Attribute) and sub.attr == 'set_leverage' and lev_line is None:
                        lev_line = sub.lineno
        notify_gate = "账户层风控拦截" in src and "level='warning')" in src
        # /force 不绕过：force 命令在 bot_runner，闸门在 trader execute_signal 内——
        # 验证 bot_runner 的 force 路径仍经 run_trader_execution → execute_signal（源码锚点）
        br_src = open('bot_runner.py', encoding='utf-8').read()
        force_no_bypass = ("_check_account_risk" not in br_src) and ("run_trader_execution" in br_src)
        report("T13 闸门位于 execute_signal 内且先于冲突检查与 set_leverage",
               gate_line is not None and conflict_line is not None and lev_line is not None
               and gate_line < conflict_line < lev_line,
               f"(gate={gate_line} conflict={conflict_line} lev={lev_line})")
        report("T14 拦截含 TG warning 通知 + /force 不绕过（闸门在 trader 层）",
               notify_gate and force_no_bypass)

    finally:
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        for p in tmp_files:
            try:
                os.remove(p)
            except OSError:
                pass

    total = len(RESULTS)
    passed = sum(1 for _, p in RESULTS if p)
    print(f"\n{'=' * 60}\nD-006 账户层风控: {passed}/{total}")
    if passed < total:
        for n, p in RESULTS:
            if not p:
                print(f"  FAILED: {n}")
        sys.exit(1)
    print("ALL GREEN")


if __name__ == "__main__":
    main()
