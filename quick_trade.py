# quick_trade.py
"""
================================================================================
📋 quick_trade.py - 快速挂单工具

功能：读取 signal.json 并执行挂单，然后退出。

⚠️ 重要：不要与 watchdog 同时运行！

================================================================================
✅ 正确的使用流程（方案A - 推荐）：

  1. 停止 watchdog
     └─ 在终端按 Ctrl+C，或点击 IDE 的停止按钮

  2. 修改 signal.json
     └─ 编辑信号参数（symbol, side, leverage, entries, take_profit, initial_stop_loss）

  3. 运行 quick_trade.py 执行挂单
     └─ python quick_trade.py
     └─ 挂单成功后会显示批次号

  4. 立即重启 watchdog
     └─ python watchdog.py
     └─ watchdog 启动后会接管监控，恢复活跃批次

⚠️ 注意事项：
  - quick_trade.py 运行时会短暂连接交易所，挂单后退出
  - watchdog 重启后会自动恢复该批次的监控
  - 挂单到 watchdog 启动之间有约 3-5 秒的空窗期（条件单远离市价，风险可控）

❌ 错误的用法：
  - 不要与 watchdog 同时运行
  - 不要在 watchdog 运行时运行 quick_trade.py

================================================================================
📋 其他使用方式：

  # 使用自定义信号文件
  python quick_trade.py my_signal.json

  # 查看帮助
  python quick_trade.py --help

================================================================================
"""

import os
import sys
from dotenv import load_dotenv
from trader_260725 import CryptoTrader
from parser import parse_signal_from_json

load_dotenv()


def main():
    signal_file = "signal.json"
    if len(sys.argv) > 1:
        signal_file = sys.argv[1]

    # 🔥 获取代理配置（从 .env 读取）
    proxy_url = os.getenv("BINANCE_PROXY")

    trader = CryptoTrader(
        api_key=os.getenv("BINANCE_API_KEY"),
        secret=os.getenv("BINANCE_SECRET"),
        is_demo=False,
        proxy_url=proxy_url,
        verbose=True
    )

    signal = parse_signal_from_json(signal_file)
    batch_id = trader.execute_signal(signal)

    if batch_id:
        print(f"\n✅ 挂单成功！批次号: {batch_id}")
        print(f"📌 请立即重启 watchdog: python watchdog.py")
    else:
        print("\n❌ 挂单失败，请检查上方日志")


if __name__ == "__main__":
    main()