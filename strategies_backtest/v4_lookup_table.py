"""展示v4 E[R]联合查表的完整42格结构，标出哪些格子被过滤（E[R]<0）。"""
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

daily_df = pd.read_parquet(cfg.base_dir / "cache" / "BTCUSDTUSDT_1d.parquet")
k_ema = 2.0 / 71; de70 = np.zeros(len(daily_df)); de70[0] = daily_df["close"].iloc[0]
for i in range(1, len(daily_df)):
    de70[i] = daily_df["close"].iloc[i]*k_ema + de70[i-1]*(1-k_ema)
daily_df["date"] = daily_df["open_time"].dt.date
date_to_ema = dict(zip(daily_df["date"], de70))

def get_prev_dev(i):
    if i - 1 < 0: return np.nan
    prev_date = pd.to_datetime(times[i-1], utc=True).date()
    ema_prev = date_to_ema.get(prev_date, np.nan)
    if np.isnan(ema_prev): return np.nan
    return (closes[i-1] - ema_prev) / ema_prev * 100

def simulate(signal, tp_l=0.18, tp_s=0.12):
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
        if is_long and lows[j] < stop_line: exit_idx, exit_price, exit_reason = j, float(stop_line), s_type; break
        if not is_long and highs[j] > stop_line: exit_idx, exit_price, exit_reason = j, float(stop_line), s_type; break
        if tp_l > 0 and is_long and highs[j] >= tp_price: exit_idx, exit_price, exit_reason = j, float(tp_price), "take_profit"; break
        if tp_s > 0 and not is_long and lows[j] <= tp_price: exit_idx, exit_price, exit_reason = j, float(tp_price), "take_profit"; break
    if exit_idx is None: exit_idx = n-1; exit_price = float(closes[-1]); exit_reason = "end"
    r = (exit_price-bt)/av if is_long else (bt-exit_price)/av
    ret = (exit_price-bt)/bt if is_long else (bt-exit_price)/bt
    return {"direction": d, "r_value": r, "final_return": ret,
            "exit_reason": exit_reason, "exit_idx": exit_idx,
            "atr_pct": av / bt, "dev_prev": get_prev_dev(i)}

all_results = []; last_exit = -1
for s in signals:
    if s.idx <= last_exit: continue
    r = simulate(s)
    if r: all_results.append(r); last_exit = r["exit_idx"]
rdf = pd.DataFrame(all_results)

atr_bins = [(0, 0.01), (0.01, 0.015), (0.015, 0.02), (0.02, 0.03), (0.03, 0.05), (0.05, 1.0)]
dev_bins = [(-999, -10), (-10, -5), (-5, 0), (0, 5), (5, 10), (10, 20), (20, 999)]

print("=" * 120)
print("v4 E[R]联合查表完整结构（6个ATR%区间 × 7个偏离度区间 = 42格）")
print("=" * 120)
print(f"\n全量基准: {len(rdf)}单, 累计R={rdf['r_value'].sum():.1f}")
print(f"\n两个维度:")
print(f"  维度1 - ATR%: 突破K前一根的ATR / 突破线（衡量波动率）")
print(f"  维度2 - 偏离度: 突破K前一根收盘价 相对 日线EMA70 的偏离（衡量价格位置）")
print(f"\n查表规则: 每格至少3单才建表, E[R]<0的格子的信号被过滤")

# 构建查表并输出完整矩阵
print(f"\n{'='*120}")
print("完整42格查表矩阵")
print(f"{'='*120}")

# 表头
hdr = f"{'ATR%区间':>14} │"
for dlo, dhi in dev_bins:
    if dlo == -999: lbl = "(-∞,-10%)"
    elif dhi == 999: lbl = "(20%,+∞)"
    else: lbl = f"[{dlo:.0f}%,{dhi:.0f}%)"
    hdr += f" {lbl:>12} │"
print(hdr)
print("─" * len(hdr))

filtered_cells = []
for alo, ahi in atr_bins:
    row = f"[{alo:.1%},{ahi:.1%})".rjust(14) + " │"
    for dlo, dhi in dev_bins:
        sub = rdf[(rdf["atr_pct"] >= alo) & (rdf["atr_pct"] < ahi) &
                  (rdf["dev_prev"] >= dlo) & (rdf["dev_prev"] < dhi)]
        if len(sub) < 3:
            row += f" {'(不足3单)':>12} │"
        else:
            er = sub["r_value"].mean()
            win_rate = (sub["r_value"] > 0).mean()
            if er < 0:
                row += f" {'★'+f'{er:.2f}':>12} │"
                filtered_cells.append((alo, ahi, dlo, dhi, len(sub), er, win_rate))
            else:
                row += f" {f'{er:.2f}':>12} │"
    print(row)

print(f"\n{'='*120}")
print(f"被过滤格子（E[R]<0, 共{len(filtered_cells)}格）")
print(f"{'='*120}")
print(f"\n{'ATR%区间':>14} {'偏离度区间':>14} {'单数':>5} {'E[R]':>7} {'胜率':>6} {'累计R':>8} {'其中打损':>8} {'其中盈利':>8}")
print("-" * 80)
total_filtered = 0; total_loss = 0; total_win = 0
for alo, ahi, dlo, dhi, cnt, er, wr in filtered_cells:
    sub = rdf[(rdf["atr_pct"] >= alo) & (rdf["atr_pct"] < ahi) &
              (rdf["dev_prev"] >= dlo) & (rdf["dev_prev"] < dhi)]
    loss_cnt = (sub["r_value"] <= 0).sum()
    win_cnt = (sub["r_value"] > 0).sum()
    total_filtered += cnt; total_loss += loss_cnt; total_win += win_cnt
    if dlo == -999: dlbl = "(-∞,-10%)"
    elif dhi == 999: dlbl = "(20%,+∞)"
    else: dlbl = f"[{dlo:.0f}%,{dhi:.0f}%)"
    print(f"[{alo:.1%},{ahi:.1%})".rjust(14) + f" {dlbl:>14} {cnt:>5} {er:>7.2f} {wr:>5.0%} {sub['r_value'].sum():>8.1f} {loss_cnt:>8} {win_cnt:>8}")

print(f"\n  合计: 过滤{total_filtered}单, 其中打损{total_loss}单({total_loss/total_filtered:.0%}), 盈利{total_win}单({total_win/total_filtered:.0%})")

# 多空分离看被过滤格子
print(f"\n{'='*120}")
print("被过滤格子的多空分离")
print(f"{'='*120}")
print(f"\n{'ATR%区间':>14} {'偏离度区间':>14} {'方向':>5} {'单数':>5} {'累计R':>8} {'打损':>5} {'盈利':>5}")
print("-" * 65)
for alo, ahi, dlo, dhi, cnt, er, wr in filtered_cells:
    sub = rdf[(rdf["atr_pct"] >= alo) & (rdf["atr_pct"] < ahi) &
              (rdf["dev_prev"] >= dlo) & (rdf["dev_prev"] < dhi)]
    if dlo == -999: dlbl = "(-∞,-10%)"
    elif dhi == 999: dlbl = "(20%,+∞)"
    else: dlbl = f"[{dlo:.0f}%,{dhi:.0f}%)"
    for d, dlbl2 in [("long", "多"), ("short", "空")]:
        dsub = sub[sub["direction"] == d]
        if dsub.empty: continue
        lc = (dsub["r_value"] <= 0).sum()
        wc = (dsub["r_value"] > 0).sum()
        print(f"[{alo:.1%},{ahi:.1%})".rjust(14) + f" {dlbl:>14} {dlbl2:>5} {len(dsub):>5} {dsub['r_value'].sum():>8.1f} {lc:>5} {wc:>5}")

print(f"\n{'='*120}")
