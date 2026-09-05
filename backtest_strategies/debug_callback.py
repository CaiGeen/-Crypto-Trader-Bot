"""调试回调基准搜索方向：验证CSV第1771行。"""
import pandas as pd

df = pd.read_csv('results/新入场逻辑v2_回调基准_ATR偏移.csv', encoding='utf-8-sig')
i = 1770
highs = df['最高价'].to_numpy()
lows = df['最低价'].to_numpy()

win_start = i - 6
win_end = i  # 不含i本身
segment_high = highs[win_start:win_end]
print(f'信号idx={i}, 方向=做多')
print(f'6K极值: {df["6K极值"].iloc[i]}')
print(f'窗口区间: [{win_start}, {win_end}) 即K线 {win_start}..{win_end-1}')
print(f'箱内最高: {segment_high.max()}, 最低: {lows[win_start:win_end].min()}')
print(f'从左往右找2K不创新高的回调高点:')
pts = []
for j in range(win_start, win_end - 2):
    if j + 2 >= win_end:
        continue
    if highs[j+1] <= highs[j] and highs[j+2] <= highs[j]:
        pts.append((j, highs[j]))
print(f'  {pts if pts else "无"}')
if pts:
    print(f'  最早(最左): {pts[0]}, 最晚(最右): {pts[-1]}}')
else:
    print('未找到回调基准')
print(f'  回调基准列: {df["回调基准"].iloc[i]}, 6K极值: {df["6K极值"].iloc[i]}')