"""检查CSV第1771行回调基准是否正确。"""
import pandas as pd

df = pd.read_csv('results/新入场逻辑v2_回调基准_ATR偏移.csv', encoding='utf-8-sig')
r = df.iloc[1770]

print(f"信号时间: {r['信号时间']}")
print(f"方向: {r['方向']}")
print(f"信号收盘价: {r['信号收盘价']}")
print(f"6K极值: {r['6K极值']}")
print(f"回调基准: {r['回调基准']}")
print(f"ATR: {r['ATR']}")
print()

# 找到信号前的6K箱体内的价格行为
# 信号idx=1770，信号收盘价64169.9，6K极值63753
# 检查在6K箱体内是否有被2K确认的回调高点