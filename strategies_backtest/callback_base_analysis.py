"""反哺分析：用6K内回调高点作为波段涨幅计算起点，对比原突破线起点。

对每个6K突破信号，在6K箱体内从右往左找最新的"2K不创新高回调"高点：
  做多：highs[j+1] <= highs[j] 且 highs[j+2] <= highs[j]
  做空：lows[j+1] >= lows[j] 且 lows[j+2] >= lows[j]
用该高点作为新起点计算MFE和涨幅，对比分布变化。
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import pandas as pd
from config import BacktestConfig
from data_loader import load_klines
from indicators import kama
from signal_detector import detect_breakout

cfg = BacktestConfig()
df = load_klines(cfg)
kama_vals = kama(df["close"].to_numpy(), cfg.kama_er_len, cfg.kama_fast, cfg.kama_slow)
signals = detect_breakout(df, cfg.window)
highs = df["high"].to_numpy(); lows = df["low"].to_numpy()
closes = df["close"].to_numpy(); times = df["open_time"].tolist()
n = len(df)


def calc_atr(h, l, c, length=20):
    m = len(c); tr = np.zeros(m); tr[0] = h[0] - l[0]
    for i in range(1, m):
        tr[i] = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
    av = np.zeros(m); av[0] = tr[0]; a = 1.0/length
    for i in range(1, m): av[i] = a*tr[i] + (1-a)*av[i-1]
    return av

atr_vals = calc_atr(highs, lows, closes, 20)
TP_LONG = 0.22; TP_SHORT = 0.12


def find_callback_extreme(i, direction, window=6):
    """在6K箱体内从右往左找最新的2K不创新高/低回调的极值点。

    做多：找highs[j]，使得highs[j+1]<=highs[j]且highs[j+2]<=highs[j]
    做空：找lows[j]，使得lows[j+1]>=lows[j]且lows[j+2]>=lows[j]
    返回(极值, j)，找不到返回(None, None)。
    """
    start = i - window  # 6K箱体起始
    end = i - 1         # 6K箱体结束
    # j从end-2到start（需要j+2<=end）
    for j in range(start, end - 1):
        if j < 0 or j + 2 >= n: continue
        if direction == "long":
            if highs[j + 1] <= highs[j] and highs[j + 2] <= highs[j]:
                return highs[j], j
        else:
            if lows[j + 1] >= lows[j] and lows[j + 2] >= lows[j]:
                return lows[j], j
    return None, None


def simulate(signal):
    """回测单波段，同时用突破线和回调高点两种起点计算MFE。"""
    i = signal.idx
    if i + 1 >= n: return None
    bt = signal.breakthrough; d = signal.direction; is_long = (d == "long")
    av = atr_vals[i - 1] if i - 1 >= 0 else np.nan
    if np.isnan(av) or av <= 0: return None

    # 找回调高点
    cb_extreme, cb_idx = find_callback_extreme(i, d, cfg.window)
    if cb_extreme is None:
        cb_extreme = bt  # 找不到用突破线
        cb_idx = i

    if is_long:
        init_stop = bt - av; tp_price = bt * (1 + TP_LONG)
    else:
        init_stop = bt + av; tp_price = bt * (1 - TP_SHORT)

    max_exc_bt = -np.inf   # 从突破线算的MFE
    max_exc_cb = -np.inf   # 从回调高点算的MFE
    peak_price = -np.inf if is_long else np.inf
    exit_idx = exit_price = exit_reason = None

    for j in range(i + 1, n):
        ks = kama_vals[j - 1]
        if np.isnan(ks): continue

        # MFE（两种起点）
        if is_long:
            exc_bt = (highs[j] - bt) / bt
            exc_cb = (highs[j] - cb_extreme) / cb_extreme
            if highs[j] > peak_price: peak_price = highs[j]
        else:
            exc_bt = (bt - lows[j]) / bt
            exc_cb = (cb_extreme - lows[j]) / cb_extreme
            if lows[j] < peak_price: peak_price = lows[j]
        if exc_bt > max_exc_bt: max_exc_bt = exc_bt
        if exc_cb > max_exc_cb: max_exc_cb = exc_cb

        bh = j - i
        if bh <= 3: stop_line = init_stop; s_type = "atr_stop"
        else:
            if is_long:
                stop_line = max(ks, bt); s_type = "breakeven" if ks < bt else "kama_trail"
            else:
                stop_line = min(ks, bt); s_type = "breakeven" if ks > bt else "kama_trail"

        if is_long and lows[j] < stop_line: exit_idx, exit_price, exit_reason = j, float(stop_line), s_type; break
        if not is_long and highs[j] > stop_line: exit_idx, exit_price, exit_reason = j, float(stop_line), s_type; break
        if is_long and highs[j] >= tp_price: exit_idx, exit_price, exit_reason = j, float(tp_price), "take_profit"; break
        if not is_long and lows[j] <= tp_price: exit_idx, exit_price, exit_reason = j, float(tp_price), "take_profit"; break

    if exit_idx is None: exit_idx = n - 1; exit_price = float(closes[-1]); exit_reason = "end"

    if is_long:
        ret_bt = (exit_price - bt) / bt
        ret_cb = (exit_price - cb_extreme) / cb_extreme
        r_value = (exit_price - bt) / av
    else:
        ret_bt = (bt - exit_price) / bt
        ret_cb = (cb_extreme - exit_price) / cb_extreme
        r_value = (bt - exit_price) / av

    return {
        "direction": d, "signal_idx": i, "exit_idx": exit_idx,
        "breakthrough": bt, "callback_extreme": cb_extreme,
        "cb_offset_pct": (bt - cb_extreme) / bt if is_long else (cb_extreme - bt) / bt,
        "has_callback": cb_idx != i,
        "r_value": r_value, "final_return": ret_bt, "final_return_cb": ret_cb,
        "mfe_bt": float(max_exc_bt) if max_exc_bt != -np.inf else 0.0,
        "mfe_cb": float(max_exc_cb) if max_exc_cb != -np.inf else 0.0,
        "atr_pct": av / bt, "exit_reason": exit_reason,
    }


# 运行回测
results = []; last_exit_idx = None
for s in signals:
    if last_exit_idx is not None and s.idx <= last_exit_idx: continue
    r = simulate(s)
    if r: results.append(r); last_exit_idx = r["exit_idx"]

rdf = pd.DataFrame(results)
f = rdf[rdf["mfe_bt"].abs() >= 0.10].reset_index(drop=True)

print("=" * 100)
print("反哺分析：6K内回调高点 vs 突破线 作为波段涨幅起点")
print("=" * 100)

# 1. 回调高点统计
print(f"\n—— 回调高点统计 ——")
print(f"  有回调高点的信号: {rdf['has_callback'].sum()}/{len(rdf)} ({rdf['has_callback'].mean():.0%})")
print(f"  回调高点偏移(突破线-回调高点)/突破线: 中位{rdf['cb_offset_pct'].median():.2%}, 均值{rdf['cb_offset_pct'].mean():.2%}")
print(f"  偏移范围: [{rdf['cb_offset_pct'].min():.2%}, {rdf['cb_offset_pct'].max():.2%}]")

# 2. MFE分布对比
print(f"\n—— MFE分布对比（全量{len(rdf)}单）——")
print(f"{'指标':>15} {'突破线起点':>12} {'回调高点起点':>12} {'差异':>10}")
print("-" * 55)
for label, col_bt, col_cb in [
    ("MFE中位", "mfe_bt", "mfe_cb"),
    ("MFE均值", "mfe_bt", "mfe_cb"),
    ("MFE P25", "mfe_bt", "mfe_cb"),
    ("MFE P75", "mfe_bt", "mfe_cb"),
]:
    bt_val = rdf[col_bt].quantile(0.5) if "中位" in label else rdf[col_bt].quantile(0.25) if "P25" in label else rdf[col_bt].quantile(0.75) if "P75" in label else rdf[col_bt].mean()
    cb_val = rdf[col_cb].quantile(0.5) if "中位" in label else rdf[col_cb].quantile(0.25) if "P25" in label else rdf[col_cb].quantile(0.75) if "P75" in label else rdf[col_cb].mean()
    print(f"{label:>15} {bt_val:>12.1%} {cb_val:>12.1%} {cb_val-bt_val:>10.1%}")

# 3. MFE区间分布对比
print(f"\n—— MFE区间分布对比 ——")
print(f"{'MFE区间':>12} {'突破线起点':>15} {'回调高点起点':>15}")
print("-" * 45)
for lo, hi in [(0, 0.02), (0.02, 0.06), (0.06, 0.10), (0.10, 0.15), (0.15, 0.22), (0.22, 1)]:
    c_bt = ((rdf["mfe_bt"] >= lo) & (rdf["mfe_bt"] < hi)).sum()
    c_cb = ((rdf["mfe_cb"] >= lo) & (rdf["mfe_cb"] < hi)).sum()
    print(f"[{lo:.0%},{hi:.0%}) {c_bt:>10} ({c_bt/len(rdf):>3.0%}) {c_cb:>10} ({c_cb/len(rdf):>3.0%})")

# 4. ≥10%过滤后的对比
print(f"\n—— MFE≥10%过滤后对比（突破线起点，{len(f)}单）——")
print(f"{'指标':>15} {'突破线起点':>12} {'回调高点起点':>12}")
print("-" * 42)
print(f"{'MFE中位':>15} {f['mfe_bt'].median():>12.1%} {f['mfe_cb'].median():>12.1%}")
print(f"{'MFE均值':>15} {f['mfe_bt'].mean():>12.1%} {f['mfe_cb'].mean():>12.1%}")
print(f"{'收益中位(突破线)':>15} {f['final_return'].median():>12.1%} {f['final_return_cb'].median():>12.1%}")
print(f"{'收益均值(突破线)':>15} {f['final_return'].mean():>12.1%} {f['final_return_cb'].mean():>12.1%}")

# 5. 用回调高点作为止盈基准的回测
print(f"\n{'='*100}")
print("用回调高点作为止盈基准的回测对比")
print(f"{'='*100}")

def simulate_with_tp_base(signal, use_callback_base=False):
    """用回调高点或突破线作为止盈基准。"""
    i = signal.idx
    if i + 1 >= n: return None
    bt = signal.breakthrough; d = signal.direction; is_long = (d == "long")
    av = atr_vals[i - 1] if i - 1 >= 0 else np.nan
    if np.isnan(av) or av <= 0: return None

    cb_extreme, _ = find_callback_extreme(i, d, cfg.window)
    if cb_extreme is None: cb_extreme = bt

    tp_base = cb_extreme if use_callback_base else bt
    tp = TP_LONG if is_long else TP_SHORT
    if is_long:
        init_stop = bt - av; tp_price = tp_base * (1 + tp)
    else:
        init_stop = bt + av; tp_price = tp_base * (1 - tp)

    max_exc = -np.inf; exit_idx = exit_price = exit_reason = None
    for j in range(i + 1, n):
        ks = kama_vals[j - 1]
        if np.isnan(ks): continue
        if is_long: exc = (highs[j] - bt) / bt
        else: exc = (bt - lows[j]) / bt
        if exc > max_exc: max_exc = exc
        bh = j - i
        if bh <= 3: stop_line = init_stop; s_type = "atr_stop"
        else:
            if is_long: stop_line = max(ks, bt); s_type = "breakeven" if ks < bt else "kama_trail"
            else: stop_line = min(ks, bt); s_type = "breakeven" if ks > bt else "kama_trail"
        if is_long and lows[j] < stop_line: exit_idx, exit_price, exit_reason = j, float(stop_line), s_type; break
        if not is_long and highs[j] > stop_line: exit_idx, exit_price, exit_reason = j, float(stop_line), s_type; break
        if is_long and highs[j] >= tp_price: exit_idx, exit_price, exit_reason = j, float(tp_price), "take_profit"; break
        if not is_long and lows[j] <= tp_price: exit_idx, exit_price, exit_reason = j, float(tp_price), "take_profit"; break
    if exit_idx is None: exit_idx = n - 1; exit_price = float(closes[-1]); exit_reason = "end"
    if is_long: r = (exit_price - bt) / av; ret = (exit_price - bt) / bt
    else: r = (bt - exit_price) / av; ret = (bt - exit_price) / bt
    return {"direction": d, "r_value": r, "final_return": ret,
            "mfe": float(max_exc) if max_exc != -np.inf else 0.0,
            "exit_reason": exit_reason, "exit_idx": exit_idx}

for label, use_cb in [("突破线止盈基准", False), ("回调高点止盈基准", True)]:
    res = []; last_ex = None
    for s in signals:
        if last_ex is not None and s.idx <= last_ex: continue
        r = simulate_with_tp_base(s, use_cb)
        if r: res.append(r); last_ex = r["exit_idx"]
    rdf_tp = pd.DataFrame(res)
    ff = rdf_tp[rdf_tp["mfe"].abs() >= 0.10].reset_index(drop=True)
    sr = ff["r_value"]; w = sr[sr > 0]; l = sr[sr <= 0]
    pf = w.sum() / abs(l.sum()) if len(l) and l.sum() != 0 else float("inf")
    pf_str = f"{pf:.1f}" if pf < 1000 else "inf"
    print(f"\n{label}（MFE≥10%: {len(ff)}单）")
    for d2, lbl2 in [("long", "做多"), ("short", "做空")]:
        sub = ff[ff["direction"] == d2]
        if sub.empty: continue
        sr2 = sub["r_value"]
        print(f"  {lbl2:>4} {len(sub):>5} 累计R={sr2.sum():>7.1f} 均R={sr2.mean():>6.2f} 胜率={(sr2>0).mean():>5.0%} 收益={sub['final_return'].sum():>8.1%}")
    print(f"  {'合并':>4} {len(ff):>5} 累计R={sr.sum():>7.1f} 均R={sr.mean():>6.2f} 胜率={(sr>0).mean():>5.0%} 收益={ff['final_return'].sum():>8.1%} PF={pf_str}")

    vc = ff["exit_reason"].value_counts()
    print(f"  出场: ", end="")
    for reason, cnt in vc.items():
        print(f"{reason}={cnt}({cnt/len(ff):.0%}) ", end="")
    print()

# 6. 回调高点偏移 vs MFE关系
print(f"\n{'='*100}")
print("回调高点偏移 vs 波段质量")
print(f"{'='*100}")
print(f"{'偏移%':>10} {'单数':>5} {'MFE_bt中位':>11} {'MFE_cb中位':>11} {'≥10%概率':>10}")
print("-" * 55)
for lo, hi in [(0, 0.005), (0.005, 0.01), (0.01, 0.02), (0.02, 0.04), (0.04, 1)]:
    sub = rdf[(rdf["cb_offset_pct"] >= lo) & (rdf["cb_offset_pct"] < hi)]
    if sub.empty: continue
    print(f"[{lo:.1%},{hi:.1%}) {len(sub):>5} {sub['mfe_bt'].median():>11.1%} {sub['mfe_cb'].median():>11.1%} {(sub['mfe_bt']>=0.10).mean():>10.0%}")

print(f"\n{'='*100}")
