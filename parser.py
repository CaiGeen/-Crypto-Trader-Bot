import json
from typing import List, Tuple
from dataclasses import dataclass


@dataclass
class TradeSignal:
    symbol: str
    side: str
    leverage: int
    entries: List[Tuple[float, float]]
    stop_loss_steps: List[float]
    take_profit: float
    initial_stop_loss: float
    batch_id: str = None


def parse_signal_from_json(file_path: str) -> TradeSignal:
    """从 JSON 文件解析交易信号（支持大小写不敏感）"""
    with open(file_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    # 🔥 大小写不敏感的字段获取函数
    def get_field(key: str, default=None):
        # 先尝试原样
        if key in data:
            return data[key]
        # 再尝试大写
        if key.upper() in data:
            return data[key.upper()]
        # 再尝试小写
        if key.lower() in data:
            return data[key.lower()]
        return default

    symbol = get_field("symbol")
    if symbol is None:
        raise KeyError("缺少必填字段: symbol (或 SYMBOL)")

    side = get_field("side", "BUY").upper()
    leverage = get_field("leverage")
    if leverage is None:
        raise KeyError("缺少必填字段: leverage (或 LEVERAGE)")

    take_profit = get_field("take_profit")
    if take_profit is None:
        raise KeyError("缺少必填字段: take_profit (或 TAKE_PROFIT)")

    initial_stop_loss = get_field("initial_stop_loss", 0.0)

    # 🔥 获取 entries（支持大小写）
    entries_data = get_field("entries")
    if entries_data is None:
        raise KeyError("缺少必填字段: entries (或 ENTRIES)")

    entries = []
    stop_loss_steps = []

    for item in entries_data:
        # 每个 entry 内部也支持大小写
        def get_entry_field(key: str, default=None):
            if key in item:
                return item[key]
            if key.upper() in item:
                return item[key.upper()]
            if key.lower() in item:
                return item[key.lower()]
            return default

        trigger_price = get_entry_field("trigger_price")
        if trigger_price is None:
            raise KeyError(f"entry 缺少 trigger_price (或 TRIGGER_PRICE): {item}")

        amount = get_entry_field("amount")
        if amount is None:
            raise KeyError(f"entry 缺少 amount (或 AMOUNT): {item}")

        stop_loss = get_entry_field("stop_loss", initial_stop_loss)

        entries.append((trigger_price, amount))
        stop_loss_steps.append(stop_loss)

    # 如果 stop_loss_steps 数量少于 entries，用 initial_stop_loss 补齐
    while len(stop_loss_steps) < len(entries):
        stop_loss_steps.append(initial_stop_loss)

    # 生成 batch_id
    from datetime import datetime
    batch_id = f"batch_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    return TradeSignal(
        symbol=symbol,
        side=side,
        leverage=leverage,
        entries=entries,
        stop_loss_steps=stop_loss_steps,
        take_profit=take_profit,
        initial_stop_loss=initial_stop_loss,
        batch_id=batch_id,
    )