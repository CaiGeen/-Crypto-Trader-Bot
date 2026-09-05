"""确认式入场变体 v2：入场=回调线±1×ATR，止损=回调线。

口径（2026-08-22 与用户确认）：
  ATR = 回调突破线那根K收盘后的ATR值 atr_vals[cb_idx]（RMA20，对齐TradingView）
  成交：信号K收盘未到触发线 → 挂单等触发按E成交；已越过触发线 → 下一根开盘价成交
        （敏感性：已越过触发线的信号直接跳过 fill_mode="skip"）
  止损：初始=回调线（限价成交时初始风险=1×ATR）；第3K后止损=max(KAMA, 实际成交价)
  止盈：回调线×(1+22%)/×(1-12%)，止损优先于止盈；开盘价出场
  挂单6根K未成交作废；无回调线不开仓（严格模式）
参照组（回调线入场）同ATR口径重算，保证公平。
"""
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
closes = df["close"].to_numpy(); opens = df["open"].to_numpy(); n = len(df)
dates = df['open_time'].dt.year.to_numpy()

def calc_atr(h, l, c, length=20):
    m = len(c); tr = np.zeros(m); tr[0] = h[0] - l[0]
    for i in range(1, m):
        tr[i] = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
    av = np.zeros(m); av[0] = tr[0]; a = 1.0/length
    for i in range(1, m): av[i] = a*tr[i] + (1-a)*av[i-1]
    return av

atr_vals = calc_atr(highs, lows, closes, 20)
TP_LONG, TP_SHORT = 0.22, 0.12
MAX_WAIT = 6  # 限价单有效期（根）

def find_callback_line(i, direction, window=6):
    start = i - window; end = i - 1
    for j in range(start, end - 1):
        if j < 0 or j + 2 >= n: continue
        if direction == "long":
            if highs[j+1] <= highs[j] and highs[j+2] <= highs[j]:
                return highs[j], j
        else:
            if lows[j+1] >= lows[j] and lows[j+2] >= lows[j]:
                return lows[j], j
    return None, None

def simulate_ref(signal, be_from=3):
    """参照组：回调线入场，初始止损=回调线∓1ATR；第be_from根K起止损=max/min(KAMA,回调线)。ATR=cb当根。

    be_from=3（用户口径）：前2根K用初始ATR止损，第3根K开盘起保本/KAMA止损单已挂在场内，
    盘中按线价成交（无"线挂上去时价格已越过"的跳空市价单问题）。
    """
    i = signal.idx
    d = signal.direction; is_long = (d == "long")
    cb, cb_idx = find_callback_line(i, d, cfg.window)
    if cb is None: return "nocb", None
    av = atr_vals[cb_idx]
    if np.isnan(av) or av <= 0: return "nocb", None
    if i + 1 >= n: return "nocb", None
    init_stop = cb - av if is_long else cb + av
    tp_price = cb * (1 + TP_LONG) if is_long else cb * (1 - TP_SHORT)
    exit_idx = None; ep = None; reason = None
    sl_last = float("nan"); ks_last = float("nan")
    for j in range(i + 1, n):
        ks = kama_vals[j-1]
        if np.isnan(ks): continue
        bh = j - i
        ks_last = float(ks)
        if bh < be_from:
            sl = init_stop; st = "atr_stop"
            if is_long and lows[j] < sl:
                sl_last = float(sl); ep = float(sl); reason = st; exit_idx = j; break
            if not is_long and highs[j] > sl:
                sl_last = float(sl); ep = float(sl); reason = st; exit_idx = j; break
        else:
            if is_long: sl = max(ks, cb); st = "breakeven" if ks < cb else "kama_trail"
            else: sl = min(ks, cb); st = "breakeven" if ks > cb else "kama_trail"
            if is_long and lows[j] < sl:
                sl_last = float(sl)
                ep = float(opens[j]) if opens[j] < sl else float(sl); reason = st; exit_idx = j; break
            if not is_long and highs[j] > sl:
                sl_last = float(sl)
                ep = float(opens[j]) if opens[j] > sl else float(sl); reason = st; exit_idx = j; break
        sl_last = float(sl)
        if is_long and highs[j] >= tp_price:
            ep = float(tp_price); reason = "take_profit"; exit_idx = j; break
        if not is_long and lows[j] <= tp_price:
            ep = float(tp_price); reason = "take_profit"; exit_idx = j; break
    if exit_idx is None: exit_idx = n-1; ep = float(closes[-1]); reason = "end"
    r = (ep - cb)/av if is_long else (cb - ep)/av
    ret = (ep - cb)/cb if is_long else (cb - ep)/cb
    return "ok", {"r": r, "ret": ret, "signal_idx": i, "entry_bar": i,
                  "entry_price": float(cb), "fill": "回调线入场", "trig": float(cb),
                  "cb": float(cb), "bt": float(signal.breakthrough), "atr": float(av),
                  "cb_idx": cb_idx, "init_stop": float(init_stop), "tp_price": float(tp_price),
                  "exit_idx": exit_idx, "hold": exit_idx - i,
                  "year": int(dates[exit_idx]), "dir": d, "exit_reason": reason,
                  "mfe": 0.0, "exit_price": float(ep),
                  "exit_stop_line": sl_last, "exit_kama_prev": ks_last,
                  "exit_open": float(opens[exit_idx])}

def simulate_new(signal, confirm_atr=1.5, same_bar_exit="1h"):
    # confirm_atr: 入场当K收盘须超过 回调线±confirm_atr×ATR，否则当K收盘平仓；None=无确认规则
    # same_bar_exit: "stop"=当K必查止损(保守) / "none"=当K不查(乐观) /
    #                "strict"=开盘越过触发线才查 / "1h"=用1H K线消解路径歧义
    """新变体：挂单在触发线（回调线±1ATR），自信号K开盘起生效（cb在前一根收盘已知），
    价格触及即市价买入（成交价=触发线）；止损挂在回调线按线价成交。

    成交当K顺序：盘中触发成交 → 1H消歧判当根止损/止盈 → 收盘确认检查（不足confirm_atr则收盘平仓）。
    """
    i = signal.idx
    d = signal.direction; is_long = (d == "long")
    cb, cb_idx = find_callback_line(i, d, cfg.window)
    if cb is None: return "nocb", None
    av = atr_vals[cb_idx]
    if np.isnan(av) or av <= 0: return "nocb", None

    trig = cb + av if is_long else cb - av
    tp_price = cb * (1 + TP_LONG) if is_long else cb * (1 - TP_SHORT)

    # 挂单自信号K开盘起生效，含信号K当根（如信号K影线触及触发线即成交）
    entry_bar = None
    for j in range(i, min(i + MAX_WAIT, n)):
        if (is_long and highs[j] >= trig) or (not is_long and lows[j] <= trig):
            entry_bar = j; entry_price = float(trig)
            fill = "信号K触发" if j == i else "挂单触发"
            break
    if entry_bar is None: return "nofill", None

    # —— 成交当根处理 ——
    from intrabar_1h import resolve_fill_bar_tf
    exit_idx = None; ep = None; reason = None
    if same_bar_exit in ("1h", "3m", "5m"):
        bar_ms = int(df["open_time_ms"].iloc[entry_bar])
        stopped, tp_hit = resolve_fill_bar_tf(bar_ms, is_long, trig, float(cb), tp_price, tf=same_bar_exit)
    elif same_bar_exit == "stop":
        stopped = (lows[entry_bar] < cb) if is_long else (highs[entry_bar] > cb)
        tp_hit = (highs[entry_bar] >= tp_price) if is_long else (lows[entry_bar] <= tp_price)
    else:  # none / strict 简化：不查当根
        stopped = tp_hit = False
    if stopped:
        exit_idx = entry_bar; ep = float(cb); reason = "atr_stop"
    elif tp_hit:
        exit_idx = entry_bar; ep = float(tp_price); reason = "take_profit"
    elif confirm_atr is not None:
        conf_line = cb + confirm_atr * av if is_long else cb - confirm_atr * av
        if (is_long and closes[entry_bar] < conf_line) or (not is_long and closes[entry_bar] > conf_line):
            # 收盘已穿越止损线（成交在触发线、收盘在线外 → 成交后必然穿越过止损线）→ 止损优先按线价
            if (is_long and closes[entry_bar] < cb) or (not is_long and closes[entry_bar] > cb):
                exit_idx = entry_bar; ep = float(cb); reason = "atr_stop"
            else:
                exit_idx = entry_bar; ep = float(closes[entry_bar]); reason = "confirm_exit"

    # —— 后续K线管理（用户三规则：1-3K初始止损=回调线；第3K收盘后止损上移到成本线，
    #     此后止损=max/min(KAMA, 成本线)，KAMA跟踪接管；跳空穿越按开盘价）——
    sl_last = float(cb); ks_last = float("nan")
    if exit_idx is None:
        for j in range(entry_bar + 1, n):
            ks = kama_vals[j-1]
            if np.isnan(ks): continue
            bh = j - entry_bar
            ks_last = float(ks)
            if bh <= 3:
                sl = float(cb); st = "atr_stop"; sl_last = float(sl)
                if is_long and lows[j] < sl:
                    ep = sl; reason = st; exit_idx = j; break
                if not is_long and highs[j] > sl:
                    ep = sl; reason = st; exit_idx = j; break
            else:
                sl = max(ks, entry_price) if is_long else min(ks, entry_price)
                st = "breakeven" if sl == entry_price else "kama_trail"
                sl_last = float(sl)
                if is_long and lows[j] < sl:
                    ep = float(opens[j]) if opens[j] < sl else sl; reason = st; exit_idx = j; break
                if not is_long and highs[j] > sl:
                    ep = float(opens[j]) if opens[j] > sl else sl; reason = st; exit_idx = j; break
            if is_long and highs[j] >= tp_price:
                ep = float(tp_price); reason = "take_profit"; exit_idx = j; break
            if not is_long and lows[j] <= tp_price:
                ep = float(tp_price); reason = "take_profit"; exit_idx = j; break
    if exit_idx is None: exit_idx = n-1; ep = float(closes[-1]); reason = "end"
    # 出场诊断字段：止损线上限（成交当根出场时为cb或保护线场景）
    if exit_idx == entry_bar and reason == "atr_stop":
        sl_last = float(cb)
    elif exit_idx == entry_bar and reason == "confirm_exit":
        sl_last = float(conf_line)

    r = (ep - entry_price)/av if is_long else (entry_price - ep)/av
    ret = (ep - entry_price)/entry_price if is_long else (entry_price - ep)/entry_price
    mfe = -np.inf
    for j in range(entry_bar, exit_idx + 1):
        if is_long: mfe = max(mfe, (highs[j] - entry_price)/entry_price)
        else: mfe = max(mfe, (entry_price - lows[j])/entry_price)
    if mfe == -np.inf: mfe = 0.0
    return "ok", {"r": r, "ret": ret, "signal_idx": i, "entry_bar": entry_bar,
                  "entry_price": entry_price, "fill": fill, "trig": float(trig),
                  "cb": float(cb), "bt": float(signal.breakthrough), "atr": float(av),
                  "exit_idx": exit_idx, "hold": exit_idx - entry_bar,
                  "year": int(dates[exit_idx]), "dir": d, "exit_reason": reason,
                  "mfe": float(mfe), "exit_price": float(ep),
                  "exit_stop_line": sl_last, "exit_kama_prev": ks_last,
                  "exit_open": float(opens[exit_idx])}

def run(sim_fn, merge="b", **kwargs):
    results = []; last_exit = -1; gate = 0
    nofill = nocb = 0
    for s in signals:
        if merge == "b" and s.idx <= last_exit: continue
        if merge == "c" and s.idx < gate: continue
        status, r = sim_fn(s, **kwargs)
        if status == "nocb": nocb += 1; continue
        if status == "nofill": nofill += 1; continue
        results.append(r)
        if merge == "b":
            last_exit = r["exit_idx"]
        else:
            anchor = r["entry_bar"] if sim_fn is simulate_new else r["signal_idx"]
            j = r["exit_idx"]
            if r["exit_reason"] == "atr_stop" and j + 1 < anchor + 4: gate = j + 1
            else: gate = anchor + 4
    return results, nocb, nofill

def show(label, results, nocb, nofill):
    rdf = pd.DataFrame(results)
    r = rdf["r"]
    print(f"\n【{label}】 成交={len(rdf)} 未成交作废={nofill} 无回调线={nocb}")
    print(f"  累计R={r.sum():.1f} 均R={r.mean():.2f} 胜率={(r>0).mean():.0%} "
          f"累计收益={rdf['ret'].sum():.1%} 持仓中位={rdf['hold'].median():.0f}根 "
          f"最大单笔亏损={r.min():.2f}R")
    if "开盘成交" in set(rdf["fill"]):
        vc = rdf["fill"].value_counts()
        print("  成交方式:", "  ".join(f"{k}={v}({v/len(rdf):.0%})" for k, v in vc.items()))
    for d, lbl in [("long", "做多"), ("short", "做空")]:
        sub = rdf[rdf["dir"] == d]
        if sub.empty: continue
        print(f"  {lbl}: {len(sub)}单 累计R={sub['r'].sum():.1f} 均R={sub['r'].mean():.2f} 胜率={(sub['r']>0).mean():.0%}")
    vc = rdf["exit_reason"].value_counts()
    print("  出场:", "  ".join(f"{k}={v}({v/len(rdf):.0%})" for k, v in vc.items()))
    return rdf

if __name__ == "__main__":
    print("=" * 100)
    print("确认式入场（ATR=回调线当根K收盘值）：入场=线±1ATR，止损=线 vs 参照：线入场，止损=线∓1ATR")
    print("=" * 100)

    ref_b, nb, nf = run(simulate_ref, "b"); ref_c, nb, nf = run(simulate_ref, "c")
    show("参照 · 回调线入场 · B合并", ref_b, nb, nf)
    show("参照 · 回调线入场 · C合并", ref_c, nb, nf)
    # 主口径：挂单自信号K生效 + 入场当K收盘须超回调线+1.5ATR（否则收盘平仓）+ 1H消歧
    new_b, nb1, nf1 = run(simulate_new, "b", confirm_atr=1.5, same_bar_exit="1h")
    new_c, nb2, nf2 = run(simulate_new, "c", confirm_atr=1.5, same_bar_exit="1h")
    bdf = show("新入场·1.5ATR确认 · B合并", new_b, nb1, nf1)
    cdf = show("新入场·1.5ATR确认 · C合并", new_c, nb2, nf2)

    # 确认阈值敏感性（含无确认）
    print(f"\n—— 确认阈值敏感性（挂单自信号K生效，1H消歧；累计R / 均R / 胜率）——")
    print(f"{'确认阈值':>16} {'B合并':>24} {'C合并':>24}")
    for ca, lbl in [(None, "无确认"), (0.5, "0.5ATR"), (1.0, "1.0ATR"), (1.5, "1.5ATR(用户)")]:
        rb2, _, _ = run(simulate_new, "b", confirm_atr=ca, same_bar_exit="1h")
        rc2, _, _ = run(simulate_new, "c", confirm_atr=ca, same_bar_exit="1h")
        b2 = pd.DataFrame(rb2); c2 = pd.DataFrame(rc2)
        print(f"{lbl:>16} {b2['r'].sum():>8.1f}/{b2['r'].mean():>5.2f}/{(b2['r']>0).mean():>4.0%} "
              f"{c2['r'].sum():>8.1f}/{c2['r'].mean():>5.2f}/{(c2['r']>0).mean():>4.0%}")

    # 年度
    print(f"\n{'年份':>6} {'参照B':>8} {'新B':>8} {'参照C':>8} {'新C':>8}")
    rb = pd.DataFrame(ref_b); rc = pd.DataFrame(ref_c)
    for yr in sorted(set(bdf["year"]) | set(cdf["year"])):
        print(f"{yr:>6} {rb[rb['year']==yr]['r'].sum():>8.1f} {bdf[bdf['year']==yr]['r'].sum():>8.1f} "
              f"{rc[rc['year']==yr]['r'].sum():>8.1f} {cdf[cdf['year']==yr]['r'].sum():>8.1f}")
    print(f"{'合计':>6} {rb['r'].sum():>8.1f} {bdf['r'].sum():>8.1f} {rc['r'].sum():>8.1f} {cdf['r'].sum():>8.1f}")

    # 导出CSV（主口径：挂单自信号K生效 + 1.5ATR确认 + 1H消歧）
    times = df["open_time"].dt.tz_convert("Asia/Shanghai").dt.strftime("%Y-%m-%d %H:%M").to_numpy()
    def to_cn(reason):
        return {"atr_stop": "ATR止损", "breakeven": "保本平仓", "kama_trail": "KAMA跟踪",
                "take_profit": "固定止盈", "confirm_exit": "确认失败平仓",
                "end": "数据末尾"}.get(reason, reason)
    rows = []
    for label, rdf in [("参照_回调线入场_B", rb), ("参照_回调线入场_C", rc),
                       ("新入场_ATR触发_B", bdf), ("新入场_ATR触发_C", cdf)]:
        for k, (_, t) in enumerate(rdf.iterrows(), start=1):
            i = int(t["signal_idx"]); eb = int(t["entry_bar"])
            is_long = (t["dir"] == "long")
            cb = t["cb"]; bt = t["bt"]
            off = (bt - cb)/bt*100 if is_long else (cb - bt)/bt*100
            rows.append({
                "序号": k, "组别": label,
                "信号时间": times[i], "成交时间": times[eb], "方向": "做多" if is_long else "做空",
                "6K极值": round(bt, 2), "回调突破线": round(cb, 2), "回调偏移%": round(off, 3),
                "ATR": round(t["atr"], 2), "入场触发线": round(t["trig"], 2),
                "成交方式": t["fill"], "入场价": round(t["entry_price"], 2),
                "初始止损": round(cb - t["atr"] if (label.startswith("参照") and is_long) else
                                  cb + t["atr"] if (label.startswith("参照") and not is_long) else
                                  cb, 2),
                "止盈目标": round(cb*(1+TP_LONG) if is_long else cb*(1-TP_SHORT), 2),
                "出场时间": times[int(t["exit_idx"])], "出场价": round(t["exit_price"], 2),
                "持仓K数": t["hold"], "R值": round(t["r"], 4), "收益率%": round(t["ret"]*100, 3),
                "MFE%": round(t["mfe"]*100, 3), "出场原因": to_cn(t["exit_reason"]), "年份": t["year"],
            })
    out = pd.DataFrame(rows)
    out = out.sort_values(["组别", "信号时间"], ascending=[True, False]).reset_index(drop=True)
    out["序号"] = range(1, len(out) + 1)
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results", "回调线_ATR入场确认_对比_v3.csv")
    out.to_csv(out_path, index=False, encoding="utf-8-sig")
    print(f"\n自查明细已导出: {out_path}（{len(out)}行）")
