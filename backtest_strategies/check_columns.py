"""打印CSV实际列名，检查编码问题。"""
import pandas as pd

df = pd.read_csv('results/新入场逻辑v2_回调基准_ATR偏移.csv', encoding='utf-8-sig')
for idx, c in enumerate(df.columns):
    print(idx, repr(c))