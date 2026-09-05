import pandas as pd

df_mark = pd.read_parquet("cache/BTCUSDTUSDT_4h_mark.parquet")
df_mark["open_time"] = pd.to_datetime(df_mark["open_time_ms"], unit="ms", utc=True)

csv = pd.read_csv("results/final_trades_E_prime.csv")

# 取第一行，信号时间应该是UTC+8
print("CSV前3行信号时间:", csv['信号时间'].head(3).tolist())

# 找到对应的UTC时间
for i in range(3):
    bj_time = csv['信号时间'].iloc[i]
    # 找到CSV中第一行的sig_idx对应的数据
    # 第一行成交价64705.7
    pass

# 直接看：UTC 2026-08-19 12:00 → UTC+8 2026-08-19 20:00?
# CSV第一行信号时间是 2026-08-19 20:00
# 查找UTC 2026-08-19 12:00 的K线
ts_utc = df_mark['open_time']
target = ts_utc[ts_utc == pd.Timestamp("2026-08-19 12:00", tz='UTC')].index
if len(target) > 0:
    idx = target[0]
    print(f"\nUTC 2026-08-19 12:00 → 北京时间 {df_mark['open_time'].iloc[idx].tz_convert('Asia/Shanghai')}")
    print(f"  close={df_mark['close'].iloc[idx]:.1f}")

# CSV第一行成交价64705.7，找到对应的K线
match = df_mark[df_mark['close'].between(64705, 64706)]
for idx in match.index:
    utc_t = df_mark['open_time'].iloc[idx]
    bj_t = utc_t.tz_convert('Asia/Shanghai')
    print(f"\n成交价64705.7对应: UTC={utc_t} → 北京时间={bj_t} close={df_mark['close'].iloc[idx]:.1f}")
