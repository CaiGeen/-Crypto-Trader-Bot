"""1min消歧回测：用1分钟K线消除4H级别止损/止盈触发歧义。

策略：回调基准+0.5ATR确认+0.3ATR止损+1.5ATR加仓+22%/12%止盈+KAMA跟踪保本
消歧逻辑：
  - 4H级别：low[j] < stop 且 high[j] >= tp → 同时触及，有歧义
  - 1min级别：逐根1min扫描，第一个触及的事件决定出场
  - 同一根1min内同时触及：看开盘价穿哪条线，止损优先（保守）
出场价：市价成交，触及线成交；跳空用1min开盘价
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, pandas as pd
from config import BacktestConfig
from data_loader import load_klines
from indicators import kama
from signal_detector import detect_breakout

cfg = BacktestConfig()
df_4h = load_klines(cfg)
kama_vals = kama(df_4h["close"].to_numpy(), cfg.kama_er_len, cfg.kama_fast, cfg.kama_slow)
signals = detect_breakout(df_4h, cfg.window)
highs = df_4h["high"].to_numpy(); lows = df_4h["low"].to_numpy()
closes = df_4h["close"].to_numpy(); opens = df_4h["open"].to_numpy()
times_4h = df_4h["open_time"].tolist()
n4h = len(df_4h)
years = df_4h["open_time"].dt.year.to_numpy()

# 加载1min数据
df_1m = pd.read_parquet(cfg.base_dir / "cache" / "BTCUSDTUSDT_1m.parquet")
t1m = pd.to_datetime(df_1m["open_time_ms"], unit="ms", utc=True)
ms_1m = df_1m["open_time_ms"].to_numpy()
o1m = df_1m["open"].to_numpy(); h1m = df_1m["high"].to_numpy()
l1m = df_1m["low"].to_numpy(); c1m = df_1m["close"].to_numpy()
n1m = len(df_1m)
print(f"1min数据: {n1m}行, {t1m.iloc[0]} ~ {t1m.iloc[-1]}")

# 建立4H→1min索引映射（每根4H对应1min的起止索引）
ms_4h = df_4h["open_time_ms"].to_numpy()
four_h_ms = 4 * 60 * 60 * 1000
# 对每根4H，1min区间 [ms_4h, ms_4h + four_h_ms)
min_start_idx = np.searchsorted(ms_1m, ms_4h)
min_end_idx = np.searchsorted(ms_1m, ms_4h + four_h_ms)


def calc_atr(h, l, c, length=20):
    m = len(c); tr = np.zeros(m); tr[0] = h[0] - l[0]
    for i in range(1, m): tr[i] = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
    av = np.zeros(m); av[0] = tr[0]; a = 1.0/length
    for i in range(1, m): av[i] = a*tr[i] + (1-a)*av[i-1]
    return av

atr_vals = calc_atr(highs, lows, closes, 20)
TP_LONG = 0.22; TP_SHORT = 0.12


def find_callback_extreme(i, direction, window=6):
    start = i - window; end = i - 1
    for j in range(start, end - 1):
        if j < 0 or j + 2 >= n4h: continue
        if direction == "long":
            if highs[j+1] <= highs[j] and highs[j+2] <= highs[j]: return highs[j], j
        else:
            if lows[j+1] >= lows[j] and lows[j+2] >= lows[j]: return lows[j], j
    return None, None


def disambiguate_1m(j_4h, sl, tp_price, is_long):
    """用1min数据消歧：同一根4H内同时触及止损止盈时，判断先后。
    返回 (exit_reason, exit_price, min_bar_offset)
    exit_reason: 'stop' / 'take_profit'
    min_bar_offset: 在4H K线内的1min偏移（0-239）
    """
    s = min_start_idx[j_4h]
    e = min_end_idx[j_4h]
    if s >= e or s >= n1m:
        return None  # 无1min数据

    for k in range(s, min(e, n1m)):
        hit_stop = (l1m[k] < sl) if is_long else (h1m[k] > sl)
        hit_tp = (h1m[k] >= tp_price) if is_long else (l1m[k] <= tp_price)

        if hit_stop and hit_tp:
            # 同一根1min内同时触及：看开盘价
            if is_long:
                if o1m[k] <= sl:
                    ep = float(o1m[k])
                    return "stop", ep, k - s
                elif o1m[k] >= tp_price:
                    return "take_profit", float(tp_price), k - s
                else:
                    # 盘中先穿哪条？保守取止损
                    return "stop", float(sl), k - s
            else:
                if o1m[k] >= sl:
                    ep = float(o1m[k])
                    return "stop", ep, k - s
                elif o1m[k] <= tp_price:
                    return "take_profit", float(tp_price), k - s
                else:
                    return "stop", float(sl), k - s
        elif hit_stop:
            if is_long:
                ep = float(o1m[k]) if o1m[k] < sl else float(sl)
            else:
                ep = float(o1m[k]) if o1m[k] > sl else float(sl)
            return "stop", ep, k - s
        elif hit_tp:
            return "take_profit", float(tp_price), k - s

    return None  # 1min内未触及（数据不完整）


def simulate(signal, use_1m=False):
    """回测。use_1m=True时用1min消歧，False用4H止损优先。"""
    i = signal.idx
    if i + 1 >= n4h: return None
    bt_6k = signal.breakthrough; d = signal.direction; is_long = (d == "long")
    av = atr_vals[i-1] if i-1 >= 0 else np.nan
    if np.isnan(av) or av <= 0: return None
    cb_extreme, _ = find_callback_extreme(i, d, cfg.window)
    entry_base = cb_extreme if cb_extreme is not None else bt_6k
    filled = (lows[i] <= entry_base) if is_long else (highs[i] >= entry_base)
    confirmed = (closes[i] >= entry_base + 0.5*av) if is_long else (closes[i] <= entry_base - 0.5*av)

    if not confirmed:
        ep = float(closes[i])
        r1 = (ep-entry_base)/av if is_long else (entry_base-ep)/av
        return {"dir": d, "exit_idx": i, "filled": filled, "r": r1, "hold": 0,
                "exit_reason": "弱突破平仓", "year": int(years[i]), "ambig": False}

    if is_long: init_stop = entry_base - 0.3*av; tp_price = entry_base*(1+TP_LONG)
    else: init_stop = entry_base + 0.3*av; tp_price = entry_base*(1-TP_SHORT)

    addon_done = False; addon_price = 0.0
    exit_idx = exit_price = exit_reason = None
    ambig_count = 0

    for j in range(i+1, n4h):
        ks = kama_vals[j-1]
        if np.isnan(ks): continue
        bh = j - i
        if bh <= 3: sl = init_stop
        else: sl = max(ks, entry_base) if is_long else min(ks, entry_base)

        # 加仓（4H收盘价条件，无歧义）
        if not addon_done:
            cg_abs = (closes[j]-entry_base) if is_long else (entry_base-closes[j])
            if cg_abs >= 1.5*av: addon_done = True; addon_price = float(closes[j])

        hit_stop = (lows[j] < sl) if is_long else (highs[j] > sl)
        hit_tp = (highs[j] >= tp_price) if is_long else (lows[j] <= tp_price)

        if hit_stop and hit_tp:
            # 歧义：同时触及止损和止盈
            ambig_count += 1
            if use_1m:
                result = disambiguate_1m(j, sl, tp_price, is_long)
                if result is not None:
                    reason_1m, ep, _ = result
                    if reason_1m == "stop":
                        exit_idx = j; exit_price = ep
                        exit_reason = "前3K止损" if bh <= 3 else "KAMA跟踪止损"
                    else:
                        exit_idx = j; exit_price = ep; exit_reason = "止盈"
                    break
                # 1min数据不完整，回退到止损优先
            # 4H级别：止损优先
            if is_long:
                ep = float(opens[j]) if opens[j] < sl else float(sl)
            else:
                ep = float(opens[j]) if opens[j] > sl else float(sl)
            exit_idx = j; exit_price = ep
            exit_reason = "前3K止损" if bh <= 3 else "KAMA跟踪止损"
            break
        elif hit_stop:
            if is_long:
                ep = float(opens[j]) if opens[j] < sl else float(sl)
            else:
                ep = float(opens[j]) if opens[j] > sl else float(sl)
            exit_idx = j; exit_price = ep
            exit_reason = "前3K止损" if bh <= 3 else "KAMA跟踪止损"
            break
        elif hit_tp:
            exit_idx = j; exit_price = float(tp_price); exit_reason = "止盈"
            break

    if exit_idx is None: exit_idx = n4h-1; exit_price = float(closes[-1]); exit_reason = "未平仓"
    r1 = (exit_price-entry_base)/av if is_long else (entry_base-exit_price)/av
    r2 = ((exit_price-addon_price)/av if is_long else (addon_price-exit_price)/av) if addon_done else 0.0
    return {"dir": d, "exit_idx": exit_idx, "filled": filled, "r": r1+r2,
            "hold": exit_idx-i, "exit_reason": exit_reason, "year": int(years[i]),
            "addon": addon_done, "ambig": ambig_count > 0, "ambig_count": ambig_count}


def run(use_1m=False):
    results = []; last_exit = -1
    for s in signals:
        if s.idx <= last_exit: continue
        r = simulate(s, use_1m)
        if r: results.append(r); last_exit = r["exit_idx"]
    return pd.DataFrame(results)


print("=" * 110)
print("1min消歧回测：4H止损优先 vs 1min精确消歧")
print("=" * 110)
print(f"策略：回调基准+0.5ATR确认+0.3ATR止损+1.5ATR加仓+22%/12%止盈")
print(f"歧义条件：同一根4H内 low<stop 且 high>=tp")
print()

rdf_4h = run(use_1m=False)
rdf_1m = run(use_1m=True)

# 歧义统计
filled_4h = rdf_4h[rdf_4h["filled"]]
ambig_sigs = filled_4h[filled_4h["ambig"]]
print(f"—— 歧义统计 ——")
print(f"  成交单总数: {len(filled_4h)}")
print(f"  涉及歧义的波段: {len(ambig_sigs)} ({len(ambig_sigs)/len(filled_4h):.1%})")
print(f"  歧义触发总次数: {filled_4h['ambig_count'].sum()}")
print(f"  → {'大部分波段无歧义，4H级别足够' if len(ambig_sigs)/len(filled_4h) < 0.1 else '歧义频繁，1min消歧有必要'}")

# 回测对比
print(f"\n{'=' * 110}")
print("回测对比")
print(f"{'=' * 110}")
modes = [("4H止损优先", rdf_4h), ("1min消歧", rdf_1m)]

print(f"\n{'方案':>12} {'成交':>6} {'累计R':>8} {'均R':>6} {'胜率':>6} {'前3K止损':>8} {'KAMA跟踪':>8} {'止盈':>6} {'弱突破':>6}")
print("-" * 70)
for label, rdf in modes:
    filled = rdf[rdf["filled"]]
    sr = filled["r"]
    pre3k = filled[filled["exit_reason"] == "前3K止损"]
    kama_t = filled[filled["exit_reason"] == "KAMA跟踪止损"]
    tp = filled[filled["exit_reason"] == "止盈"]
    weak = filled[filled["exit_reason"] == "弱突破平仓"]
    print(f"{label:>12} {len(filled):>6} {sr.sum():>8.1f} {sr.mean():>6.2f} "
          f"{(sr>0).mean():>5.0%} {len(pre3k):>8} {len(kama_t):>8} {len(tp):>6} {len(weak):>6}")

print(f"\n{'方案':>12}  {'做多累计R':>10} {'做多均R':>8}  {'做空累计R':>10} {'做空均R':>8}")
print("-" * 55)
for label, rdf in modes:
    filled = rdf[rdf["filled"]]
    for d in ["long", "short"]:
        sub = filled[filled["dir"] == d]
        if d == "long": lr, lm = sub["r"].sum(), sub["r"].mean() if not sub.empty else 0
        else: sr2, sm = sub["r"].sum(), sub["r"].mean() if not sub.empty else 0
    print(f"{label:>12}  {lr:>10.1f} {lm:>8.2f}  {sr2:>10.1f} {sm:>8.2f}")

# 歧义波段明细
print(f"\n{'=' * 110}")
print(f"歧义波段明细（{len(ambig_sigs)}个波段，4H止损优先 vs 1min消歧）")
print(f"{'=' * 110}")
if len(ambig_sigs) > 0:
    diff_count = 0
    same_count = 0
    for idx in ambig_sigs.index:
        r4h = rdf_4h.loc[idx]
        r1m = rdf_1m.loc[idx]
        if r4h["exit_reason"] != r1m["exit_reason"]:
            diff_count += 1
        else:
            same_count += 1
    print(f"  1min改变结论: {diff_count} 个（止损→止盈 或 止盈→止损）")
    print(f"  1min维持结论: {same_count} 个（4H止损优先恰好正确）")

    # 改变结论的波段
    print(f"\n  改变结论的波段:")
    print(f"  {'序号':>4} {'方向':>4} {'4H出场':>10} {'4H R值':>8} {'1min出场':>10} {'1min R值':>8} {'差R':>8}")
    cnt = 0
    for idx in ambig_sigs.index:
        r4h = rdf_4h.loc[idx]
        r1m = rdf_1m.loc[idx]
        if r4h["exit_reason"] != r1m["exit_reason"]:
            cnt += 1
            if cnt <= 30:
                d_str = "多" if r4h["dir"] == "long" else "空"
                print(f"  {cnt:>4} {d_str:>4} {r4h['exit_reason']:>10} {r4h['r']:>8.2f} {r1m['exit_reason']:>10} {r1m['r']:>8.2f} {r1m['r']-r4h['r']:>+8.2f}")
    if diff_count > 30:
        print(f"  ... 共{diff_count}个")

# 年度分解
print(f"\n{'=' * 110}")
print("年度分解")
print(f"{'=' * 110}")
print(f"\n{'年份':>6}  {'4H累计R':>10} {'4H单数':>8}  {'1min累计R':>10} {'1min单数':>8}  {'差R':>8}")
print("-" * 60)
all_years = sorted(set(rdf_4h["year"]).union(set(rdf_1m["year"])))
for yr in all_years:
    c = rdf_4h[(rdf_4h["filled"]) & (rdf_4h["year"]==yr)]
    b = rdf_1m[(rdf_1m["filled"]) & (rdf_1m["year"]==yr)]
    print(f"{yr:>6}  {c['r'].sum():>10.1f} {len(c):>8}  {b['r'].sum():>10.1f} {len(b):>8}  {b['r'].sum()-c['r'].sum():>+8.1f}")
tc = rdf_4h[rdf_4h["filled"]]["r"].sum(); tb = rdf_1m[rdf_1m["filled"]]["r"].sum()
print(f"{'合计':>6}  {tc:>10.1f} {len(rdf_4h[rdf_4h['filled']]):>8}  {tb:>10.1f} {len(rdf_1m[rdf_1m['filled']]):>8}  {tb-tc:>+8.1f}")

print(f"\n{'=' * 110}")
