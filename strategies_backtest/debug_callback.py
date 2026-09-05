"""调试第1771行回调基准为何等于6K极值。"""
import pandas as pd

df = pd.read_csv('results/新入场逻辑v2_回调基准_ATR偏移.csv', encoding='utf-8-sig')
i = 1770
s_close = df['信号收盘价'].iloc[i]
bt = df['6K极值'].iloc[i]
print(f'信号idx={i}, 方向=做多, close={s_close}, 6K极值={bt}')
highs = df['最高价'].to_numpy()
lows = df['最低价'].to_numpy()

win_start = i - 6
win_end = i
seg_highs = highs[win_start:win_end]
seg_lows = lows[win_start:win_end]
print(f'6K箱体范围: 索引[{win_start}, {win_end}), 共{len(seg_highs)}根')
print(f'箱内最高价: {seg_highs.max()}, 最低价: {seg_lows.min()}')
print(f'回调基准={df["回调基准"].iloc[i]}, 6K极值={bt}')

points = []
for j in range(win_start, win_end - 2):
    if j < 0 or j + 2 >= win_end:
        continue
    if highs[j+1] <= highs[j] and highs[j+2] <= highs[j]:
        points.append((j, highs[j]))

print(f'找到的2K不新高回调点(从左往右): {points}')
print(f'最早(最左)的: {points[0] if points else None}')
print(f'最晚(最右)的: {points[-1] if points else None}')