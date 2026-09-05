"""回测"第N根弱涨主动平仓"策略：如果第N根时涨幅<阈值，主动平仓。"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, pandas as pd
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

def simulate(signal, tp_l=0.18, tp_s=0.12, early_bar=3, early_thresh=0.005):
    """early_bar: 在第几根检查涨幅; early_thresh: 涨幅低于此值则主动平仓"""
    i = signal.idx
    if i + 1 >= n: return None
    bt = signal.breakthrough; d = signal.direction; is_long = (d == "long")
    av = atr_vals[i-1] if i-1 >= 0 else np.nan
    if np.isnan(av) or av <= 0: return None
    if is_long:
        init_stop = bt - av; tp_price = bt * (1 + tp_l) if tp_l > 0 else float("inf")
    else:
        init_stop = bt + av; tp_price = bt * (1 - tp_s) if tp_s > 0 else 0.0
    max_exc = -np.inf; exit_idx = exit_price = exit_reason = None
    early_exited = False
    for j in range(i+1, n):
        ks = kama_vals[j-1]
        if np.isnan(ks): continue
        exc = (highs[j]-bt)/bt if is_long else (bt-lows[j])/bt
        if exc > max_exc: max_exc = exc
        bh = j - i
        if bh <= 3: stop_line = init_stop; s_type = "atr_stop"
        else:
            if is_long: stop_line = max(ks, bt); s_type = "breakeven" if ks < bt else "kama_trail"
            else: stop_line = min(ks, bt); s_type = "breakeven" if ks > bt else "kama_trail"
        # 先检查止损
        if is_long and lows[j] < stop_line: exit_idx, exit_price, exit_reason = j, float(stop_line), s_type; break
        if not is_long and highs[j] > stop_line: exit_idx, exit_price, exit_reason = j, float(stop_line), s_type; break
        # 检查止盈
        if tp_l > 0 and is_long and highs[j] >= tp_price: exit_idx, exit_price, exit_reason = j, float(tp_price), "take_profit"; break
        if tp_s > 0 and not is_long and lows[j] <= tp_price: exit_idx, exit_price, exit_reason = j, float(tp_price), "take_profit"; break
        # 主动平仓检查：在第early_bar根收盘时，如果涨幅<阈值则平仓
        if bh == early_bar:
            close_exc = (closes[j]-bt)/bt if is_long else (bt-closes[j])/bt
            if close_exc < early_thresh:
                exit_idx, exit_price, exit_reason = j, float(closes[j]), "early_exit"
                early_exited = True
                break
    if exit_idx is None: exit_idx = n-1; exit_price = float(closes[-1]); exit_reason = "end"
    r = (exit_price-bt)/av if is_long else (bt-exit_price)/av
    ret = (exit_price-bt)/bt if is_long else (bt-exit_price)/bt
    return {"direction": d, "r_value": r, "final_return": ret,
            "mfe": float(max_exc) if max_exc != -np.inf else 0.0,
            "exit_reason": exit_reason, "exit_idx": exit_idx,
            "hold_bars": exit_idx - i, "early_exited": early_exited, "sig_idx": i}

def run_backtest(tp_l=0.18, tp_s=0.12, early_bar=3, early_thresh=0.005):
    results = []; last_exit = -1
    for s in signals:
        if s.idx <= last_exit: continue
        r = simulate(s, tp_l, tp_s, early_bar, early_thresh)
        if r: results.append(r); last_exit = r["exit_idx"]
    return pd.DataFrame(results)

print("=" * 110)
print("第N根弱涨主动平仓策略回测")
print("=" * 110)

# 基准
base = run_backtest(early_bar=999, early_thresh=-1)  # 不触发主动平仓
print(f"\n基准（不主动平仓）: {len(base)}单, 累计R={base['r_value'].sum():.1f}, 均R={base['r_value'].mean():.2f}, 收益={base['final_return'].sum():.1%}")

# 扫描 early_bar × early_thresh
print(f"\n{'='*110}")
print("扫描：第N根 × 涨幅阈值")
print(f"{'='*110}")

for early_bar in [3, 5, 8]:
    print(f"\n—— 第{early_bar}根主动平仓 ——")
    print(f"{'阈值':>8} {'总单数':>6} {'主动平仓':>8} {'累计R':>8} {'均R':>6} {'胜率':>6} {'收益':>9} {'vs基准':>8} {'主动平仓R':>10} {'主动均R':>8} {'原均R':>8}")
    print("-" * 100)
    for thresh in [0.001, 0.003, 0.005, 0.008, 0.01, 0.015, 0.02, 0.03]:
        rdf = run_backtest(early_bar=early_bar, early_thresh=thresh)
        early = rdf[rdf["early_exited"]]
        non_early = rdf[~rdf["early_exited"]]
        # 找这些主动平仓单在基准中的表现（用sig_idx匹配）
        early_sig_indices = set(early["sig_idx"])
        base_early = base[base["sig_idx"].isin(early_sig_indices)]
        sr = rdf["r_value"]
        vs = sr.sum() - base["r_value"].sum()
        print(f"{thresh:>7.1%} {len(rdf):>6} {len(early):>8} {sr.sum():>8.1f} {sr.mean():>6.2f} {(sr>0).mean():>5.0%} {rdf['final_return'].sum():>9.1%} {vs:>+8.1f} {early['r_value'].sum():>10.1f} {early['r_value'].mean():>8.2f} {base_early['r_value'].mean():>8.2f}" if len(early) > 0 else f"{thresh:>7.1%} {len(rdf):>6} {0:>8} {sr.sum():>8.1f} {sr.mean():>6.2f} {(sr>0).mean():>5.0%} {rdf['final_return'].sum():>9.1%} {vs:>+8.1f} {'—':>10} {'—':>8} {'—':>8}")

# 详细对比最优参数
print(f"\n{'='*110}")
print("详细对比：第3根涨幅<0.5%主动平仓")
print(f"{'='*110}")

rdf_early = run_backtest(early_bar=3, early_thresh=0.005)
early = rdf_early[rdf_early["early_exited"]]
non_early = rdf_early[~rdf_early["early_exited"]]

print(f"\n  基准: {len(base)}单, 累计R={base['r_value'].sum():.1f}, 均R={base['r_value'].mean():.2f}")
print(f"  主动平仓策略: {len(rdf_early)}单, 累计R={rdf_early['r_value'].sum():.1f}, 均R={rdf_early['r_value'].mean():.2f}")
print(f"  差异: {rdf_early['r_value'].sum() - base['r_value'].sum():+.1f}R")

print(f"\n  主动平仓的单子（{len(early)}单）:")
print(f"    主动平仓后: 均R={early['r_value'].mean():.2f}, 累计R={early['r_value'].sum():.1f}, 持仓中位={early['hold_bars'].median():.0f}根")
# 找这些单在基准中的表现
early_indices = list(early.index)
base_early = base[base["sig_idx"].isin(set(early["sig_idx"]))]
print(f"    基准中同样单: 均R={base_early['r_value'].mean():.2f}, 累计R={base_early['r_value'].sum():.1f}, 持仓中位={base_early['hold_bars'].median():.0f}根")
print(f"    → 主动平仓节省持仓: {base_early['hold_bars'].median() - early['hold_bars'].median():.0f}根")
print(f"    → R值变化: {early['r_value'].sum() - base_early['r_value'].sum():+.1f}R")

# 出场原因分布对比
print(f"\n  出场原因分布对比:")
print(f"  {'原因':>10} {'基准单数':>8} {'基准累计R':>10} {'策略单数':>8} {'策略累计R':>10}")
print("-" * 55)
reason_map = {"atr_stop": "ATR止损", "breakeven": "保本平仓", "kama_trail": "KAMA跟踪", "take_profit": "固定止盈", "early_exit": "主动平仓", "end": "未平仓"}
for reason, label in reason_map.items():
    b_sub = base[base["exit_reason"] == reason]
    e_sub = rdf_early[rdf_early["exit_reason"] == reason]
    if b_sub.empty and e_sub.empty: continue
    print(f"  {label:>10} {len(b_sub):>8} {b_sub['r_value'].sum():>10.1f} {len(e_sub):>8} {e_sub['r_value'].sum():>10.1f}")

# 多空分离
print(f"\n  多空分离:")
for d, lbl in [("long", "做多"), ("short", "做空")]:
    b_sub = base[base["direction"] == d]
    e_sub = rdf_early[rdf_early["direction"] == d]
    print(f"  {lbl}: 基准{len(b_sub)}单/R={b_sub['r_value'].sum():.1f}, 策略{len(e_sub)}单/R={e_sub['r_value'].sum():.1f}, 差异{e_sub['r_value'].sum()-b_sub['r_value'].sum():+.1f}R")

# 年度对比
print(f"\n  年度对比:")
rdf_early["year"] = pd.to_datetime([times[i] for i in rdf_early["exit_idx"]], utc=True).year
base["year"] = pd.to_datetime([times[i] for i in base["exit_idx"]], utc=True).year
print(f"  {'年份':>6} {'基准单数':>8} {'基准R':>8} {'策略单数':>8} {'策略R':>8} {'差异':>8}")
print("-" * 45)
for yr in sorted(base["year"].unique()):
    b_yr = base[base["year"] == yr]
    e_yr = rdf_early[rdf_early["year"] == yr]
    print(f"  {yr:>6} {len(b_yr):>8} {b_yr['r_value'].sum():>8.1f} {len(e_yr):>8} {e_yr['r_value'].sum():>8.1f} {e_yr['r_value'].sum()-b_yr['r_value'].sum():>+8.1f}")

print(f"\n{'='*110}")
