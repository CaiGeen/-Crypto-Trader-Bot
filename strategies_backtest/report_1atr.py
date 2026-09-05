"""1ATR止损完整报告：最优配置(回调基准+0.5ATR确认+1ATR止损+1.5ATR加仓+22%/12%止盈) + 1min消歧。

输出：终端完整统计 + 明细CSV（中文列名）
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

# 1min数据
df_1m = pd.read_parquet(cfg.base_dir / "cache" / "BTCUSDTUSDT_1m.parquet")
ms_1m = df_1m["open_time_ms"].to_numpy()
o1m = df_1m["open"].to_numpy(); h1m = df_1m["high"].to_numpy()
l1m = df_1m["low"].to_numpy(); c1m = df_1m["close"].to_numpy()
n1m = len(df_1m)
ms_4h = df_4h["open_time_ms"].to_numpy()
four_h_ms = 4 * 60 * 60 * 1000
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
STOP_MULT = 1.0  # 1ATR止损

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
    s = min_start_idx[j_4h]; e = min_end_idx[j_4h]
    if s >= e or s >= n1m: return None
    for k in range(s, min(e, n1m)):
        hit_stop = (l1m[k] < sl) if is_long else (h1m[k] > sl)
        hit_tp = (h1m[k] >= tp_price) if is_long else (l1m[k] <= tp_price)
        if hit_stop and hit_tp:
            if is_long:
                if o1m[k] <= sl: return "stop", float(o1m[k]), k - s
                elif o1m[k] >= tp_price: return "take_profit", float(tp_price), k - s
                else: return "stop", float(sl), k - s
            else:
                if o1m[k] >= sl: return "stop", float(o1m[k]), k - s
                elif o1m[k] <= tp_price: return "take_profit", float(tp_price), k - s
                else: return "stop", float(sl), k - s
        elif hit_stop:
            if is_long: ep = float(o1m[k]) if o1m[k] < sl else float(sl)
            else: ep = float(o1m[k]) if o1m[k] > sl else float(sl)
            return "stop", ep, k - s
        elif hit_tp:
            return "take_profit", float(tp_price), k - s
    return None

def simulate_if_held(i, entry_base, av, d, is_long):
    """模拟弱突破单如果继续持有的结果（用同样止损止盈逻辑）。"""
    if is_long: init_stop = entry_base - STOP_MULT*av; tp_price = entry_base*(1+TP_LONG)
    else: init_stop = entry_base + STOP_MULT*av; tp_price = entry_base*(1-TP_SHORT)
    mfe = -np.inf if is_long else np.inf
    exit_idx = exit_price = exit_reason = None
    for j in range(i+1, n4h):
        ks = kama_vals[j-1]
        if np.isnan(ks): continue
        bh = j - i
        if bh <= 3: sl = init_stop
        else: sl = max(ks, entry_base) if is_long else min(ks, entry_base)
        if is_long: mfe = max(mfe, highs[j])
        else: mfe = min(mfe, lows[j])
        hit_stop = (lows[j] < sl) if is_long else (highs[j] > sl)
        hit_tp = (highs[j] >= tp_price) if is_long else (lows[j] <= tp_price)
        if hit_stop and hit_tp:
            result = disambiguate_1m(j, sl, tp_price, is_long)
            if result is not None:
                reason_1m, ep, _ = result
                if reason_1m == "stop":
                    exit_idx = j; exit_price = ep; exit_reason = "止损"
                else:
                    exit_idx = j; exit_price = ep; exit_reason = "止盈"
                break
            if is_long: ep = float(opens[j]) if opens[j] < sl else float(sl)
            else: ep = float(opens[j]) if opens[j] > sl else float(sl)
            exit_idx = j; exit_price = ep; exit_reason = "止损"; break
        elif hit_stop:
            if is_long: ep = float(opens[j]) if opens[j] < sl else float(sl)
            else: ep = float(opens[j]) if opens[j] > sl else float(sl)
            exit_idx = j; exit_price = ep; exit_reason = "止损"; break
        elif hit_tp:
            exit_idx = j; exit_price = float(tp_price); exit_reason = "止盈"; break
    if exit_idx is None: exit_idx = n4h-1; exit_price = float(closes[-1]); exit_reason = "未平仓"
    mfe_pct = (mfe - entry_base)/entry_base*100 if is_long else (entry_base-mfe)/entry_base*100
    r_val = (exit_price-entry_base)/av if is_long else (entry_base-exit_price)/av
    return {"missed_mfe_pct": mfe_pct, "missed_r": r_val, "missed_reason": exit_reason,
            "missed_hold": exit_idx - i}


def simulate(signal):
    i = signal.idx
    if i + 1 >= n4h: return None
    bt_6k = signal.breakthrough; d = signal.direction; is_long = (d == "long")
    av = atr_vals[i-1] if i-1 >= 0 else np.nan
    if np.isnan(av) or av <= 0: return None
    cb_extreme, cb_idx = find_callback_extreme(i, d, cfg.window)
    has_cb = cb_extreme is not None
    entry_base = cb_extreme if has_cb else bt_6k
    filled = (lows[i] <= entry_base) if is_long else (highs[i] >= entry_base)
    confirmed = (closes[i] >= entry_base + 0.5*av) if is_long else (closes[i] <= entry_base - 0.5*av)
    # 成交价=基准线（限价单挂单价，不计跳空）
    fill_price = float(entry_base) if filled else 0.0

    if not filled:
        return {"dir": d, "exit_idx": i, "filled": filled, "has_cb": has_cb,
                "entry_base": entry_base, "bt_6k": bt_6k, "atr": av,
                "confirmed": confirmed, "r": 0.0, "hold": 0, "exit_reason": "限价未成交",
                "exit_price": 0.0, "fill_price": 0.0,
                "addon": False, "addon_price": 0, "ambig": False,
                "mfe_pct": 0.0, "mae_pct": 0.0, "year": int(years[i]), "sig_idx": i,
                "init_stop": 0, "tp_price": 0,
                "missed_mfe_pct": 0.0, "missed_r": 0.0, "missed_reason": "", "missed_hold": 0}

    if not confirmed:
        ep = float(closes[i])
        r1 = (ep-fill_price)/av if is_long else (fill_price-ep)/av
        # 模拟如果继续持有
        held = simulate_if_held(i, entry_base, av, d, is_long)
        return {"dir": d, "exit_idx": i, "filled": filled, "has_cb": has_cb,
                "entry_base": entry_base, "bt_6k": bt_6k, "atr": av,
                "confirmed": False, "r": r1, "hold": 0, "exit_reason": "弱突破平仓",
                "exit_price": ep, "fill_price": fill_price,
                "addon": False, "addon_price": 0, "ambig": False,
                "mfe_pct": 0.0, "mae_pct": 0.0, "year": int(years[i]), "sig_idx": i,
                "init_stop": 0, "tp_price": 0,
                "missed_mfe_pct": held["missed_mfe_pct"], "missed_r": held["missed_r"],
                "missed_reason": held["missed_reason"], "missed_hold": held["missed_hold"]}

    if is_long: init_stop = fill_price - STOP_MULT*av; tp_price = fill_price*(1+TP_LONG)
    else: init_stop = fill_price + STOP_MULT*av; tp_price = fill_price*(1-TP_SHORT)

    addon_done = False; addon_price = 0.0
    exit_idx = exit_price = exit_reason = None; ambig = False
    mfe = -np.inf if is_long else np.inf; mae = np.inf if is_long else -np.inf

    for j in range(i+1, n4h):
        ks = kama_vals[j-1]
        if np.isnan(ks): continue
        bh = j - i
        if bh <= 3: sl = init_stop
        else: sl = max(ks, fill_price) if is_long else min(ks, fill_price)

        if not addon_done:
            cg_abs = (closes[j]-fill_price) if is_long else (fill_price-closes[j])
            if cg_abs >= 1.5*av: addon_done = True; addon_price = float(closes[j])

        if is_long: mfe = max(mfe, highs[j]); mae = min(mae, lows[j])
        else: mfe = min(mfe, lows[j]); mae = max(mae, highs[j])

        hit_stop = (lows[j] < sl) if is_long else (highs[j] > sl)
        hit_tp = (highs[j] >= tp_price) if is_long else (lows[j] <= tp_price)

        if hit_stop and hit_tp:
            ambig = True
            result = disambiguate_1m(j, sl, tp_price, is_long)
            if result is not None:
                reason_1m, ep, _ = result
                if reason_1m == "stop":
                    exit_idx = j; exit_price = ep
                    exit_reason = "前3K止损" if bh <= 3 else "KAMA跟踪止损"
                else:
                    exit_idx = j; exit_price = ep; exit_reason = "止盈"
                break
            if is_long: ep = float(sl)
            else: ep = float(sl)
            exit_idx = j; exit_price = ep
            exit_reason = "前3K止损" if bh <= 3 else "KAMA跟踪止损"; break
        elif hit_stop:
            ep = float(sl)
            exit_idx = j; exit_price = ep
            exit_reason = "前3K止损" if bh <= 3 else "KAMA跟踪止损"; break
        elif hit_tp:
            exit_idx = j; exit_price = float(tp_price); exit_reason = "止盈"; break

    if exit_idx is None: exit_idx = n4h-1; exit_price = float(closes[-1]); exit_reason = "未平仓"

    mfe_pct = (mfe - fill_price)/fill_price*100 if is_long else (fill_price-mfe)/fill_price*100
    mae_pct = (mae - fill_price)/fill_price*100 if is_long else (fill_price-mae)/fill_price*100
    r1 = (exit_price-fill_price)/av if is_long else (fill_price-exit_price)/av
    r2 = ((exit_price-addon_price)/av if is_long else (addon_price-exit_price)/av) if addon_done else 0.0
    return {"dir": d, "exit_idx": exit_idx, "filled": filled, "has_cb": has_cb,
            "entry_base": entry_base, "bt_6k": bt_6k, "atr": av,
            "confirmed": True, "r": r1+r2, "hold": exit_idx-i, "exit_reason": exit_reason,
            "exit_price": exit_price, "addon": addon_done, "addon_price": addon_price,
            "ambig": ambig, "mfe_pct": mfe_pct, "mae_pct": mae_pct,
            "year": int(years[i]), "sig_idx": i, "init_stop": init_stop, "tp_price": tp_price,
            "fill_price": fill_price,
            "missed_mfe_pct": 0.0, "missed_r": 0.0, "missed_reason": "", "missed_hold": 0}

# 运行：未成交单记入结果但不占用last_exit（方案B）
results = []; last_exit = -1
for s in signals:
    if s.idx <= last_exit: continue
    r = simulate(s)
    if r:
        results.append(r)
        if r["filled"]:
            last_exit = r["exit_idx"]

rdf = pd.DataFrame(results)
filled = rdf[rdf["filled"]].copy()

print("=" * 110)
print("1ATR止损完整报告")
print("=" * 110)
print(f"配置：回调基准+0.5ATR确认+1ATR止损+1.5ATR加仓+做多22%/做空12%止盈+1min消歧")
print(f"数据：{df_4h['open_time'].iloc[0]} ~ {df_4h['open_time'].iloc[-1]}，{n4h}根4H K线")
print(f"原始信号：{len(signals)}，总波段：{len(rdf)}，成交：{len(filled)}，未成交：{len(rdf)-len(filled)}（不占用last_exit）")
print()

sr = filled["r"]
w = sr[sr > 0]; l = sr[sr <= 0]
pf = w.sum() / abs(l.sum()) if len(l) and l.sum() != 0 else float("inf")
pf_str = f"{pf:.1f}" if pf < 1000 else "inf"

# 1. 总览
print(f"{'━' * 110}")
print("一、总览")
print(f"{'━' * 110}")
print(f"  累计R={sr.sum():.1f}  均R={sr.mean():.2f}  胜率={(sr>0).mean():.0%}  PF={pf_str}  累计收益={sr.sum():.1f}R")
print(f"  歧义波段: {filled['ambig'].sum()} 个")

# 2. 多空分开
print(f"\n{'━' * 110}")
print("二、多空分开")
print(f"{'━' * 110}")
print(f"{'方向':>6} {'单数':>6} {'累计R':>8} {'均R':>6} {'胜率':>6} {'大赚(≥5R)':>10} {'大亏(≤-1R)':>10}")
print("-" * 60)
for d, lbl in [("long", "做多"), ("short", "做空")]:
    sub = filled[filled["dir"] == d]
    if sub.empty: continue
    sr2 = sub["r"]
    big_win = (sr2 >= 5).sum()
    big_loss = (sr2 <= -1).sum()
    print(f"{lbl:>6} {len(sub):>6} {sr2.sum():>8.1f} {sr2.mean():>6.2f} {(sr2>0).mean():>5.0%} {big_win:>10} {big_loss:>10}")

# 3. 出场方式
print(f"\n{'━' * 110}")
print("三、出场方式分布")
print(f"{'━' * 110}")
vc = filled["exit_reason"].value_counts()
for reason, cnt in vc.items():
    sub = filled[filled["exit_reason"] == reason]
    print(f"  {reason:>10}: {cnt:>5}单 ({cnt/len(filled):.0%})  累计R={sub['r'].sum():>7.1f}  均R={sub['r'].mean():>6.2f}")

# 4. 回调基准 vs 回退6K
print(f"\n{'━' * 110}")
print("四、入场基准类型")
print(f"{'━' * 110}")
for has_cb, lbl in [(True, "回调基准"), (False, "回退6K极值")]:
    sub = filled[filled["has_cb"] == has_cb]
    if sub.empty: continue
    print(f"  {lbl:>10}: {len(sub):>5}单 ({len(sub)/len(filled):.0%})  累计R={sub['r'].sum():>7.1f}  均R={sub['r'].mean():>6.2f}  胜率={(sub['r']>0).mean():.0%}")

# 4.5 弱突破错过分析
weak = filled[~filled["confirmed"]]
print(f"\n{'━' * 110}")
print(f"四(补)、弱突破平仓单'如果继续持有'分析（{len(weak)}单）")
print(f"{'━' * 110}")
if not weak.empty:
    missed_big = weak[weak["missed_mfe_pct"] >= 10]
    print(f"  弱突破平仓R: {weak['r'].sum():.1f}（均{weak['r'].mean():.2f}）")
    print(f"  如果继续持有累计R: {weak['missed_r'].sum():.1f}（均{weak['missed_r'].mean():.2f}）")
    print(f"  净差R: {weak['missed_r'].sum()-weak['r'].sum():+.1f}")
    print(f"  错过大行情(继续持有MFE≥10%): {len(missed_big)}单 ({len(missed_big)/len(weak):.0%})")
    if not missed_big.empty:
        print(f"    这些单继续持有累计R={missed_big['missed_r'].sum():.1f}，弱突破平仓仅赚R={missed_big['r'].sum():.1f}")
        print(f"    错过R={missed_big['missed_r'].sum()-missed_big['r'].sum():.1f}")
    print(f"\n  {'可能错过MFE':>12} {'单数':>6} {'弱突破R':>8} {'继续持有R':>10} {'差R':>8}")
    print("  " + "-" * 50)
    for lo, hi in [(0,2),(2,6),(6,10),(10,22),(22,100)]:
        sub = weak[(weak["missed_mfe_pct"] >= lo) & (weak["missed_mfe_pct"] < hi)]
        if sub.empty: continue
        print(f"  [{lo}%,{hi}%):{'':>3} {len(sub):>6} {sub['r'].sum():>8.1f} {sub['missed_r'].sum():>10.1f} {sub['missed_r'].sum()-sub['r'].sum():>+8.1f}")

# 5. 加仓统计
print(f"\n{'━' * 110}")
print("五、加仓统计")
print(f"{'━' * 110}")
addon_sub = filled[filled["addon"]]
no_addon = filled[~filled["addon"]]
print(f"  加仓单: {len(addon_sub):>5} ({len(addon_sub)/len(filled):.0%})  基础R={no_addon['r'].sum():.1f}  加仓贡献R={addon_sub['r'].sum()-no_addon['r'].sum():+.1f}")

# 6. 年度分解
print(f"\n{'━' * 110}")
print("六、年度分解")
print(f"{'━' * 110}")
print(f"{'年份':>6} {'单数':>6} {'累计R':>8} {'均R':>6} {'胜率':>6} {'做多R':>8} {'做空R':>8} {'最大回撤R':>10}")
print("-" * 65)
for yr in sorted(filled["year"].unique()):
    sub = filled[filled["year"] == yr]
    sr2 = sub["r"]
    lr = sub[sub["dir"]=="long"]["r"].sum()
    srr = sub[sub["dir"]=="short"]["r"].sum()
    cum = sr2.cumsum()
    max_dd = (cum - cum.cummax()).min()
    print(f"{yr:>6} {len(sub):>6} {sr2.sum():>8.1f} {sr2.mean():>6.2f} {(sr2>0).mean():>5.0%} {lr:>8.1f} {srr:>8.1f} {max_dd:>10.1f}")
print(f"{'合计':>6} {len(filled):>6} {sr.sum():>8.1f} {sr.mean():>6.2f} {(sr>0).mean():>5.0%} {filled[filled['dir']=='long']['r'].sum():>8.1f} {filled[filled['dir']=='short']['r'].sum():>8.1f}")

# 7. R值分布
print(f"\n{'━' * 110}")
print("七、R值分布")
print(f"{'━' * 110}")
print(f"{'R区间':>12} {'单数':>6} {'占比':>6} {'累计R':>8} {'均R':>6}")
print("-" * 45)
for lo, hi in [(-3,-2),(-2,-1),(-1,0),(0,1),(1,2),(2,5),(5,10),(10,100)]:
    sub = filled[(filled["r"] >= lo) & (filled["r"] < hi)]
    if sub.empty: continue
    print(f"[{lo}, {hi}):{'':>4} {len(sub):>6} {len(sub)/len(filled):>5.0%} {sub['r'].sum():>8.1f} {sub['r'].mean():>6.2f}")

# 8. MFE分布
print(f"\n{'━' * 110}")
print("八、MFE分布（最大有利偏移）")
print(f"{'━' * 110}")
print(f"{'MFE区间':>12} {'单数':>6} {'占比':>6} {'累计R':>8} {'均R':>6}")
print("-" * 45)
for lo, hi in [(0,2),(2,6),(6,10),(10,15),(15,22),(22,100)]:
    sub = filled[(filled["mfe_pct"] >= lo) & (filled["mfe_pct"] < hi)]
    if sub.empty: continue
    print(f"[{lo}%,{hi}%):{'':>3} {len(sub):>6} {len(sub)/len(filled):>5.0%} {sub['r'].sum():>8.1f} {sub['r'].mean():>6.2f}")

# 9. ATR%分布
print(f"\n{'━' * 110}")
print("九、ATR%分布")
print(f"{'━' * 110}")
ap = filled["atr"] / filled["entry_base"]
print(f"  中位={ap.median():.2%}  均值={ap.mean():.2%}  范围=[{ap.min():.2%}, {ap.max():.2%}]")
print(f"{'ATR%区间':>12} {'单数':>6} {'累计R':>8} {'均R':>6} {'胜率':>6}")
print("-" * 45)
for lo, hi in [(0,0.01),(0.01,0.015),(0.015,0.02),(0.02,0.03),(0.03,0.05),(0.05,1)]:
    sub = filled[(ap >= lo) & (ap < hi)]
    if sub.empty: continue
    print(f"[{lo:.1%},{hi:.1%}){'':>2} {len(sub):>6} {sub['r'].sum():>8.1f} {sub['r'].mean():>6.2f} {(sub['r']>0).mean():>5.0%}")

# 10. 持仓时间
print(f"\n{'━' * 110}")
print("十、持仓时间")
print(f"{'━' * 110}")
print(f"  中位={filled['hold'].median():.0f}根  均值={filled['hold'].mean():.1f}根  范围=[{filled['hold'].min()},{filled['hold'].max()}]")
print(f"{'持仓K数':>10} {'单数':>6} {'占比':>6} {'累计R':>8} {'均R':>6}")
print("-" * 40)
for lo, hi in [(0,1),(1,3),(3,5),(5,8),(8,12),(12,20),(20,999)]:
    sub = filled[(filled["hold"] >= lo) & (filled["hold"] < hi)]
    if sub.empty: continue
    print(f"[{lo},{hi}):{'':>4} {len(sub):>6} {len(sub)/len(filled):>5.0%} {sub['r'].sum():>8.1f} {sub['r'].mean():>6.2f}")

# 导出CSV（含未成交单，标注"限价未成交"）
rows = []
for idx, r in rdf.reset_index(drop=True).iterrows():
    i = int(r["sig_idx"])
    sig_t = pd.Timestamp(times_4h[i]).tz_convert("Asia/Shanghai").strftime("%Y-%m-%d %H:%M")
    is_unfilled = not r["filled"]
    if is_unfilled:
        rows.append({
            "序号": idx+1, "信号时间": sig_t, "方向": "做多" if r["dir"]=="long" else "做空",
            "6K极值": round(r["bt_6k"],2), "入场基准": round(r["entry_base"],2),
            "成交价": "",
            "基准类型": "回调基准" if r["has_cb"] else "回退6K极值",
            "回调偏移%": round((r["bt_6k"]-r["entry_base"])/r["bt_6k"]*100,3) if r["has_cb"] else "",
            "ATR": round(r["atr"],2), "ATR%": round(r["atr"]/r["entry_base"]*100,3),
            "是否成交": "限价未成交",
            "是否确认": "确认" if r["confirmed"] else "弱突破",
            "初始止损": "", "止盈目标": "",
            "出场时间": "", "出场价": "",
            "持仓K数": "", "R值": "",
            "出场原因": "限价未成交", "是否加仓": "",
            "加仓价": "",
            "MFE%": "", "MAE%": "",
            "是否歧义": "",
            "可能错过MFE%": "", "可能错过R值": "", "可能错过出场": "", "可能错过持仓K数": "",
            "是否错过大行情": "",
            "年份": int(r["year"]),
        })
        continue
    exit_t = pd.Timestamp(times_4h[int(r["exit_idx"])]).tz_convert("Asia/Shanghai").strftime("%Y-%m-%d %H:%M")
    cb_off = (r["bt_6k"]-r["entry_base"])/r["bt_6k"]*100 if r["dir"]=="long" else (r["entry_base"]-r["bt_6k"])/r["bt_6k"]*100
    is_weak = not r["confirmed"]
    missed_big = "是" if (is_weak and r["missed_mfe_pct"] >= 10) else ""
    rows.append({
        "序号": idx+1, "信号时间": sig_t, "方向": "做多" if r["dir"]=="long" else "做空",
        "6K极值": round(r["bt_6k"],2), "入场基准": round(r["entry_base"],2),
        "成交价": round(r["fill_price"],2),
        "基准类型": "回调基准" if r["has_cb"] else "回退6K极值",
        "回调偏移%": round(cb_off,3) if r["has_cb"] else "",
        "ATR": round(r["atr"],2), "ATR%": round(r["atr"]/r["entry_base"]*100,3),
        "是否成交": "成交",
        "是否确认": "确认" if r["confirmed"] else "弱突破",
        "初始止损": round(r["init_stop"],2) if r["confirmed"] else "",
        "止盈目标": round(r["tp_price"],2) if r["confirmed"] else "",
        "出场时间": exit_t, "出场价": round(r["exit_price"],2),
        "持仓K数": int(r["hold"]), "R值": round(r["r"],3),
        "出场原因": r["exit_reason"], "是否加仓": "是" if r["addon"] else "否",
        "加仓价": round(r["addon_price"],2) if r["addon"] else "",
        "MFE%": round(r["mfe_pct"],2), "MAE%": round(r["mae_pct"],2),
        "是否歧义": "是" if r["ambig"] else "否",
        "可能错过MFE%": round(r["missed_mfe_pct"],2) if is_weak else "",
        "可能错过R值": round(r["missed_r"],3) if is_weak else "",
        "可能错过出场": r["missed_reason"] if is_weak else "",
        "可能错过持仓K数": int(r["missed_hold"]) if is_weak else "",
        "是否错过大行情": missed_big,
        "年份": int(r["year"]),
    })
out_df = pd.DataFrame(rows)
out_df = out_df.sort_values("信号时间", ascending=False).reset_index(drop=True)
out_df["序号"] = range(1, len(out_df) + 1)
csv_path = cfg.results_path / "1ATR止损_完整报告v4.csv"
out_df.to_csv(csv_path, index=False, encoding="utf-8-sig")
print(f"\n{'=' * 110}")
print(f"明细CSV已导出: {csv_path}")
print(f"共 {len(out_df)} 行 × {len(out_df.columns)} 列")
print(f"列名: {list(out_df.columns)}")
print(f"{'=' * 110}")
