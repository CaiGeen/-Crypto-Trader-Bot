"""订单状态机回测（2026-08-23 用户规则v2）：挂单生命周期 + 三止损 + J4开仓规则。

多头状态机（空头镜像）：
  武装：某K高点H后连续2根不创新高（收盘确认）→ 基准线=H，挂单=H+ATR(H当根)
  挂单期间（无有效期，仅被形态约束）：
    a. 价格触及挂单价 → 市价成交（该K收盘是否突破6K极值不影响成交，B1）
    b. 出现高于H的新高点（未及挂单价）且其后连续2K不创新高 → 更新基准线与挂单价（E2.b）
    c. 收盘突破6K极值但未触发挂单，其后连续3根收盘跌破基准线 → 撤单重观察（E2.c）
    d. 自基准K起 区间最高−最低 > 3×ATR(最新收盘K) → 撤单，等新基准线（E6）
  持仓三止损：
    ① 初始止损=基准线（买入后第1-3K，盘中按线价）
    ② 保护性平仓：成交当根及第1-3K，每次收盘 < 基准线+1.5×ATR → 按收盘价平仓（标注第几K）
    ③ 第3K收盘仍持仓 → 止损上移成本线；此后止损=max(KAMA[前K], 成本线)，按线价成交
       （仅当挂线价在开盘时已高于现价时按开盘价成交——物理上无法按线价成交）
    止盈：基准线×1.22/0.88，止损优先
  开新仓（J4）：无持仓 或 全部持仓止损已≥成本线；保护性平仓后立即可再开
  成交当根的盘中先后顺序用1分钟K线消歧；成交K曾低于基准线的标注"1m消歧"
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, pandas as pd
import callback_atr_entry as m
from intrabar_1h import resolve_fill_bar_tf

highs, lows, closes, opens = m.highs, m.lows, m.closes, m.opens
atr, kama = m.atr_vals, m.kama_vals
n = m.n
ms = m.df["open_time_ms"].to_numpy()
times = m.df["open_time"].dt.tz_convert("Asia/Shanghai").dt.strftime("%Y-%m-%d %H:%M").to_numpy()
years = m.df["open_time"].dt.year.to_numpy()

# 滚动6K极值（不含当前根）
ext6 = np.full(n, np.nan); ext6s = np.full(n, np.nan)
for t in range(6, n):
    ext6[t] = highs[t-6:t].max(); ext6s[t] = lows[t-6:t].min()

machines = {"long": {"order": None, "positions": [], "regime": False},
            "short": {"order": None, "positions": [], "regime": False}}
bands = []      # 全波段记录（含未成交）
trades = []     # 成交明细

_BAND_SEQ = [0]
def new_order(d, base_idx, confirm_t):
    is_long = (d == "long")
    base = float(highs[base_idx]) if is_long else float(lows[base_idx])
    a = float(atr[base_idx])
    trig = base + a if is_long else base - a
    _BAND_SEQ[0] += 1
    return {"dir": d, "base": base, "base_idx": base_idx, "confirm_t": confirm_t,
            "atr": a, "trig": trig, "breakout_seen": False, "below_cnt": 0,
            "band_id": _BAND_SEQ[0], "trade": None}

def all_protected(t):
    for mc in machines.values():
        for p in mc["positions"]:
            if t - p["fill_bar"] < 4:   # 第1-3K内止损=基准线<成本 → 未保护
                return False
    return True

def close_position(p, t, price, reason, extra=""):
    is_long = (p["dir"] == "long")
    r = (price - p["entry"]) / p["atr0"] if is_long else (p["entry"] - price) / p["atr0"]
    p.update(exit_t=t, exit_price=price, reason=reason, extra=extra, r=r,
             hold=t - p["fill_bar"])
    trades.append(p)

def process_fill_bar(p, t):
    """成交当根：1m消歧止损/止盈，否则收盘保护性检查（第0K）。"""
    is_long = (p["dir"] == "long")
    stopped, tp_hit = resolve_fill_bar_tf(int(ms[t]), is_long, p["trig"], p["base"], p["tp"], tf="1m")
    p["disamb"] = bool(stopped or (lows[t] < p["base"] if is_long else highs[t] > p["base"]))
    if stopped:
        close_position(p, t, p["base"], "ATR止损", "成交当根(1m消歧)")
    elif tp_hit:
        close_position(p, t, p["tp"], "固定止盈", "成交当根")
    else:
        conf = p["base"] + 1.5 * p["atr0"] if is_long else p["base"] - 1.5 * p["atr0"]
        if (closes[t] < conf) if is_long else (closes[t] > conf):
            if (closes[t] < p["base"]) if is_long else (closes[t] > p["base"]):
                close_position(p, t, p["base"], "ATR止损", "收盘穿线(止损优先)")
            else:
                close_position(p, t, float(closes[t]), "保护性平仓", "第0K(成交当根)")

# ============ 主循环 ============
for t in range(6, n):
    for d, mc in machines.items():
        is_long = (d == "long")
        o = mc["order"]

        # —— 挂单成交判定（B1：不看收盘是否突破）——
        if o is not None:
            trig_hit = highs[t] >= o["trig"] if is_long else lows[t] <= o["trig"]
            if trig_hit and all_protected(t):
                p = {"dir": d, "base": o["base"], "base_idx": o["base_idx"], "atr0": o["atr"],
                     "trig": o["trig"], "entry": o["trig"], "fill_bar": t,
                     "tp": o["base"] * (1.22 if is_long else 0.88),
                     "ext6": ext6[o["confirm_t"]], "band_id": o["band_id"]}
                mc["positions"].append(p)
                o["trade"] = p
                o["end_t"] = t
                o["status"] = "成交"
                bands.append(o)      # 该基准线已成交入账（出场细节由trade携带）
                mc["order"] = None   # 新基准线需重新形成
                process_fill_bar(p, t)

        # —— 持仓管理 ——
        for p in list(mc["positions"]):
            if "exit_t" in p:
                mc["positions"].remove(p); continue
            bh = t - p["fill_bar"]
            if bh == 0:
                continue  # 成交当根已在 process_fill_bar 处理
            if bh <= 3:
                # 规则①：盘中止损=基准线（止损优先）
                if (lows[t] < p["base"]) if is_long else (highs[t] > p["base"]):
                    close_position(p, t, p["base"], "ATR止损", f"第{bh}K"); continue
                # 规则②：第1-3K收盘保护性检查
                conf = p["base"] + 1.5 * p["atr0"] if is_long else p["base"] - 1.5 * p["atr0"]
                if (closes[t] < conf) if is_long else (closes[t] > conf):
                    close_position(p, t, float(closes[t]), "保护性平仓", f"第{bh}K"); continue
                # bh==3 收盘通过 → 成本线上移（第4K起生效）
            else:
                # 规则③：止损=max/min(KAMA[前K], 成本线)，按线价；开盘已越过线则按开盘价
                cost = p["entry"]
                sl = max(kama[t-1], cost) if is_long else min(kama[t-1], cost)
                if is_long:
                    if lows[t] < sl:
                        px = float(opens[t]) if opens[t] < sl else sl
                        close_position(p, t, px, "成本线止损" if sl == cost else "KAMA跟踪",
                                       "跳空按开盘价" if px != sl else ""); continue
                    if highs[t] >= p["tp"]:
                        close_position(p, t, p["tp"], "固定止盈", ""); continue
                else:
                    if highs[t] > sl:
                        px = float(opens[t]) if opens[t] > sl else sl
                        close_position(p, t, px, "成本线止损" if sl == cost else "KAMA跟踪",
                                       "跳空按开盘价" if px != sl else ""); continue
                    if lows[t] <= p["tp"]:
                        close_position(p, t, p["tp"], "固定止盈", ""); continue
        # 数据末尾强制平仓
        if t == n - 1:
            for p in list(mc["positions"]):
                if "exit_t" not in p:
                    close_position(p, t, float(closes[t]), "数据末尾", "")
                    mc["positions"].remove(p)

        # —— 6K突破环境（收盘判定）：收盘突破6K极值激活；最新低价跌破前K KAMA失效 ——
        if mc["regime"]:
            if (lows[t] < kama[t-1]) if is_long else (highs[t] > kama[t-1]):
                mc["regime"] = False
                if mc["order"] is not None:
                    mc["order"]["end_t"] = t; mc["order"]["status"] = "跌破KAMA撤单"
                    bands.append(mc["order"]); mc["order"] = None
        else:
            if (closes[t] > ext6[t]) if is_long else (closes[t] < ext6s[t]):
                mc["regime"] = True

        # —— 挂单形态维护（未成交时，收盘后处理）——
        o = mc["order"]
        if o is not None:
            # E2.b：新基准线（高于旧基准且2K确认）
            if t >= 2 and mc["regime"]:
                if is_long:
                    new_swing = highs[t-1] <= highs[t-2] and highs[t] <= highs[t-2]
                else:
                    new_swing = lows[t-1] >= lows[t-2] and lows[t] >= lows[t-2]
                if new_swing:
                    o["end_t"] = t; o["status"] = "被新基准线替换"
                    bands.append(o)
                    mc["order"] = new_order(d, t-2, t)
                    o = mc["order"]
            if o is None:
                pass
            else:
                # E2.c：收盘突破6K极值未触发 → 连续3根收盘破基准线则撤单
                if not o.get("filled_by"):
                    if (closes[t] > ext6[t]) if is_long else (closes[t] < ext6s[t]):
                        o["breakout_seen"] = True; o["below_cnt"] = 0
                    elif o["breakout_seen"]:
                        if (closes[t] < o["base"]) if is_long else (closes[t] > o["base"]):
                            o["below_cnt"] += 1
                            if o["below_cnt"] >= 3:
                                o["end_t"] = t; o["status"] = "收盘破线3根撤单"
                                bands.append(o); mc["order"] = None; o = None
                        else:
                            o["below_cnt"] = 0
                # E6：区间>3×ATR(最新收盘K)撤单
                if o is not None:
                    seg_h = highs[o["confirm_t"]:t+1].max(); seg_l = lows[o["confirm_t"]:t+1].min()
                    if (seg_h - seg_l) > 3 * atr[t]:
                        o["end_t"] = t; o["status"] = "3ATR区间撤单"
                        bands.append(o); mc["order"] = None
        else:
            # IDLE：新武装（须6K突破环境开启）
            if t >= 2 and mc["regime"]:
                if is_long:
                    swing = highs[t-1] <= highs[t-2] and highs[t] <= highs[t-2]
                else:
                    swing = lows[t-1] >= lows[t-2] and lows[t] >= lows[t-2]
                if swing:
                    mc["order"] = new_order(d, t-2, t)

# 收尾：仍在场的挂单
for d, mc in machines.items():
    if mc["order"] is not None:
        o = mc["order"]; o["end_t"] = n - 1; o["status"] = "未触发至数据末尾"
        bands.append(o)


# ============ 输出 ============
def band_rows(only_trade=False):
    rows = []
    for b in bands:
        d = b["dir"]; is_long = (d == "long")
        base_idx = b["base_idx"]; end_t = b["end_t"]
        peak = float(highs[base_idx:end_t+1].max()) if is_long else float(lows[base_idx:end_t+1].min())
        pk = base_idx + (int(np.argmax(highs[base_idx:end_t+1])) if is_long else int(np.argmin(lows[base_idx:end_t+1])))
        ext = ext6[b.get("confirm_t", base_idx)] if not np.isnan(ext6[b.get("confirm_t", base_idx)]) else highs[base_idx]
        tr = b.get("trade")
        if only_trade and tr is None:
            continue
        if is_long:
            miss = max((highs[base_idx:end_t+1].max() - b["trig"]) / b["trig"] * 100, 0.0)
            amp_b = (peak - b["base"]) / b["base"] * 100
            amp_e = (peak - ext) / ext * 100
        else:
            miss = max((b["trig"] - lows[base_idx:end_t+1].min()) / b["trig"] * 100, 0.0)
            amp_b = (b["base"] - peak) / b["base"] * 100
            amp_e = (ext - peak) / ext * 100
        row = {
            "方向": "做多" if is_long else "做空",
            "基准线K时间": times[base_idx], "确认K时间": times[b["confirm_t"]],
            "基准线": round(b["base"], 2), "ATR": round(b["atr"], 2), "触发线": round(b["trig"], 2),
            "6K极值": round(float(ext), 2),
            "状态": b["status"],
            "波段涨跌幅%_基准线": round(amp_b, 3), "波段涨跌幅%_极值基准": round(amp_e, 3),
            "峰值价": round(peak, 2), "峰值K时间": times[pk],
            "未成交错过%": round(miss, 3) if b["status"] != "成交" else "",
            "年份": int(years[base_idx]),
        }
        if tr is not None:
            row.update({
                "成交时间": times[tr["fill_bar"]], "入场价": round(tr["entry"], 2),
                "初始止损": round(tr["base"], 2),
                "保护线": round(tr["base"] + 1.5*tr["atr0"] if is_long else tr["base"] - 1.5*tr["atr0"], 2),
                "止盈目标": round(tr["tp"], 2),
                "出场时间": times[tr["exit_t"]], "出场价": round(tr["exit_price"], 2),
                "出场原因": tr["reason"], "平仓细节": tr["extra"],
                "保护性平仓第几K": tr["extra"].replace("第", "").replace("K(成交当根)", "").replace("K", "") if tr["reason"] == "保护性平仓" else "",
                "持仓K数": tr["hold"], "R值": round(tr["r"], 4),
                "1m消歧": "是" if tr.get("disamb") else "否",
            })
        rows.append(row)
    df = pd.DataFrame(rows)
    return df.sort_values("基准线K时间", ascending=False).reset_index(drop=True)

full = band_rows(False)
trade_df = band_rows(True)
full.insert(0, "序号", range(1, len(full) + 1))
trade_df.insert(0, "序号", range(1, len(trade_df) + 1))
full.to_csv("results/全波段明细_订单状态机.csv", index=False, encoding="utf-8-sig")
trade_df.to_csv("results/成交明细_订单状态机.csv", index=False, encoding="utf-8-sig")

print(f"全波段: {len(full)}条  状态分布: {dict(full['状态'].value_counts())}")
r = trade_df["R值"]
print(f"成交: {len(trade_df)}单 累计R={r.sum():.1f} 均R={r.mean():.2f} 胜率={(r>0).mean():.0%} 最大亏={r.min():.2f}R")
print(f"出场: {dict(trade_df['出场原因'].value_counts())}")
print(f"保护性平仓第几K分布: {dict(trade_df[trade_df['出场原因']=='保护性平仓']['平仓细节'].value_counts())}")
print(f"多空: 做多{len(trade_df[trade_df['方向']=='做多'])}单/{trade_df[trade_df['方向']=='做多']['R值'].sum():.1f}R  "
      f"做空{len(trade_df[trade_df['方向']=='做空'])}单/{trade_df[trade_df['方向']=='做空']['R值'].sum():.1f}R")
yr = trade_df.groupby(trade_df['成交时间'].str[:4])["R值"].agg(["count", "sum"])
print(yr.to_string())
