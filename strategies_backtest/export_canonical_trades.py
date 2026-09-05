"""导出统一口径完整字段自查报告（逐字段可验证）。

策略 = 触发线市价入场 + 三止损规则（用户2026-08-22确认的统一口径）·B合并
输出：results/最优配置_统一口径_完整字段_v5.csv（北京时间，时间降序）

字段算法说明（供逐字段核对）：
  6K极值            max(high[i-6..i-1]) 做多 / min(low) 做空；收盘超极值%>0 即信号成立
  回调突破线        6K窗口从左到右第一个 j 满足 high[j+1]<=high[j] 且 high[j+2]<=high[j]（做多）
  回调线所在K/确认完成K  cb_idx 与 cb_idx+2 的时间（后者收盘后回调线成立，最晚=信号K前一根）
  ATR               atr_vals[cb_idx]（RMA20，回调线当根收盘值）
  入场触发线        回调线±1×ATR（挂单自信号K开盘生效）
  入场价            触发线（价格触及触发线即市价买入，成交价=触发线）
  成交方式          信号K触发=信号K盘中触及；挂单触发=其后最多6根K内触及；未触及=不成交（不在表内）
  初始止损          =回调线（规则1：盘中触及按线价市价出场）
  保护线            回调线±1.5×ATR（规则2：成交当K收盘未超过→按收盘价保护性平仓）
  成交当K收盘价     与保护线对照核对规则2
  止盈目标          回调线×(1+22%) / ×(1-12%)
  出场前KAMA        kama_vals[出场K-1]（规则3：第3K收盘后止损=max/min(KAMA,成本线)）
  出场时止损线      出场当根生效的止损线（1-3K=回调线；第3K收盘后=max/min(KAMA,入场价)）
  出场K开盘价       动态线（成本线/KAMA）跳空穿越时按开盘价成交
  R值               (出场价-入场价)/ATR（做空镜像），初始止损对应-1R，成本线≈0R
  峰值价/波段涨跌幅  信号K..出场K最高/低相对回调线（含信号K）
  入场后涨跌幅       成交K..出场K最高/低相对入场价
  MAE%              成交K..出场K最不利幅度（相对入场价，负值）
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, pandas as pd
import callback_atr_entry as m

res, nocb, nofill = m.run(m.simulate_new, "b", confirm_atr=1.5, same_bar_exit="1m")
times = m.df["open_time"].dt.tz_convert("Asia/Shanghai").dt.strftime("%Y-%m-%d %H:%M").to_numpy()
to_cn = {"atr_stop": "ATR止损", "breakeven": "成本线止损", "kama_trail": "KAMA跟踪",
         "take_profit": "固定止盈", "confirm_exit": "保护性平仓", "end": "数据末尾"}

rows = []
for t in res:
    i = t["signal_idx"]; j = t["exit_idx"]; eb = t["entry_bar"]; cb_idx = m.find_callback_line(i, t["dir"], m.cfg.window)[1]
    is_long = (t["dir"] == "long")
    cb = t["cb"]; bt = t["bt"]; av = t["atr"]; ep = t["exit_price"]; ent = t["entry_price"]
    conf_line = cb + 1.5 * av if is_long else cb - 1.5 * av
    if is_long:
        off = (bt - cb) / bt * 100
        beyond = (m.closes[i] - bt) / bt * 100
        seg = m.highs[i:j+1]; pk = int(np.argmax(seg)) + i; peak = float(seg.max())
        post = float(m.highs[eb:j+1].max()) if j >= eb else peak
        amp = (peak - cb) / cb * 100; amp_post = (post - ent) / ent * 100
        mae = (float(m.lows[eb:j+1].min()) - ent) / ent * 100 if j >= eb else 0.0
    else:
        off = (cb - bt) / bt * 100
        beyond = (bt - m.closes[i]) / bt * 100
        seg = m.lows[i:j+1]; pk = int(np.argmin(seg)) + i; peak = float(seg.min())
        post = float(m.lows[eb:j+1].min()) if j >= eb else peak
        amp = (cb - peak) / cb * 100; amp_post = (ent - post) / ent * 100
        mae = (ent - float(m.highs[eb:j+1].max())) / ent * 100 if j >= eb else 0.0
    rows.append({
        "序号": 0,
        "信号时间": times[i], "成交时间": times[eb], "成交方式": t["fill"],
        "方向": "做多" if is_long else "做空",
        "突破K开盘价": round(float(m.opens[i]), 2), "突破K最高价": round(float(m.highs[i]), 2),
        "突破K最低价": round(float(m.lows[i]), 2), "突破K收盘价": round(float(m.closes[i]), 2),
        "收盘超极值%": round(beyond, 3), "6K极值": round(bt, 2),
        "回调突破线": round(cb, 2), "回调线所在K时间": times[cb_idx],
        "回调线确认完成K时间": times[min(cb_idx + 2, m.n - 1)],
        "是否有回调线": "是", "回调偏移%": round(off, 3),
        "ATR": round(av, 2), "ATR%": round(av / cb * 100, 3),
        "入场触发线": round(t["trig"], 2), "入场价": round(ent, 2),
        "初始止损": round(cb, 2), "保护线(线±1.5ATR)": round(conf_line, 2),
        "成交当K收盘价": round(float(m.closes[eb]), 2),
        "止盈目标": round(cb * (1 + m.TP_LONG) if is_long else cb * (1 - m.TP_SHORT), 2),
        "出场时间": times[j], "出场K开盘价": round(t["exit_open"], 2),
        "出场前KAMA": round(t["exit_kama_prev"], 2), "出场时止损线": round(t["exit_stop_line"], 2),
        "出场价": round(ep, 2),
        "出场原因": to_cn[t["exit_reason"]],
        "持仓K数": t["hold"], "R值": round(t["r"], 4), "收益率%": round(t["ret"] * 100, 3),
        "峰值价": round(peak, 2), "峰值K时间": times[pk],
        "波段涨跌幅%": round(amp, 3), "入场后涨跌幅%": round(amp_post, 3),
        "MAE%": round(mae, 3),
        "前3K止损": "是" if (t["exit_reason"] == "atr_stop" and t["hold"] <= 3) else "否",
        "年份": t["year"],
    })

df = pd.DataFrame(rows)
df = df.sort_values("信号时间", ascending=False).reset_index(drop=True)
df["序号"] = range(1, len(df) + 1)

out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results", "最优配置_统一口径_完整字段_v5.csv")
df.to_csv(out_path, index=False, encoding="utf-8-sig")

print(f"统一口径完整字段自查报告已导出: {out_path}")
print(f"共{len(df)}行×{len(df.columns)}列（触发线市价入场+三止损规则·B合并）")
print(f"成交方式: {dict(df['成交方式'].value_counts())}")
print(f"出场原因分布: {dict(df['出场原因'].value_counts())}")
print(f"累计R={df['R值'].sum():.1f} 胜率={(df['R值']>0).mean():.0%} 中位波段涨跌幅={df['波段涨跌幅%'].median():.2f}%")
