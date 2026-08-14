import json
import os
import uuid
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
        if key in data:
            return data[key]
        if key.upper() in data:
            return data[key.upper()]
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

    # 🔥 杠杆限制（从环境变量读取，与 bot_runner 保持一致）
    MAX_LEVERAGE = int(os.getenv("MAX_LEVERAGE", "100"))

    if leverage > MAX_LEVERAGE:
        raise ValueError(
            f"❌ 信号杠杆 {leverage}x 超过最大允许值 {MAX_LEVERAGE}x\n"
            f"💡 请调整 .env 中的 MAX_LEVERAGE（当前 {MAX_LEVERAGE}）"
        )
    if leverage < 1:
        raise ValueError(f"❌ 杠杆 {leverage}x 必须大于 0")
    if not isinstance(leverage, int) or leverage <= 0:
        raise ValueError(f"❌ 杠杆 {leverage} 必须是正整数")

    take_profit = get_field("take_profit")
    if take_profit is None:
        raise KeyError("缺少必填字段: take_profit (或 TAKE_PROFIT)")

    # 🔥 检查止盈价是否为正数
    if take_profit <= 0:
        raise ValueError(f"❌ 止盈价 {take_profit} 必须大于 0")

    initial_stop_loss = get_field("initial_stop_loss", 0.0)
    if initial_stop_loss <= 0:
        raise ValueError(f"❌ 初始止损价 {initial_stop_loss} 必须大于 0")

    # 🔥 获取 entries（支持大小写）
    entries_data = get_field("entries")
    if entries_data is None:
        raise KeyError("缺少必填字段: entries (或 ENTRIES)")

    if not isinstance(entries_data, list):
        raise ValueError("❌ entries 必须是数组")
    if len(entries_data) == 0:
        raise ValueError("❌ entries 不能为空")

    entries = []
    stop_loss_steps = []

    for idx, item in enumerate(entries_data):
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

        # 🔥 检查触发价是否为正数
        if trigger_price <= 0:
            raise ValueError(f"❌ 第 {idx + 1} 层触发价 {trigger_price} 必须大于 0")

        amount = get_entry_field("amount")
        if amount is None:
            raise KeyError(f"entry 缺少 amount (或 AMOUNT): {item}")

        # 🔥 数量检查
        if amount <= 0:
            raise ValueError(f"❌ 第 {idx + 1} 层数量 {amount} 必须大于 0")
        if amount > 1000:  # 防止误操作
            raise ValueError(f"❌ 第 {idx + 1} 层数量 {amount} 超过最大允许值 1000")

        stop_loss = get_entry_field("stop_loss", initial_stop_loss)
        if stop_loss <= 0:
            raise ValueError(f"❌ 第 {idx + 1} 层止损价 {stop_loss} 必须大于 0")

        entries.append((trigger_price, amount))
        stop_loss_steps.append(stop_loss)

    # 如果 stop_loss_steps 数量少于 entries，用 initial_stop_loss 补齐
    while len(stop_loss_steps) < len(entries):
        stop_loss_steps.append(initial_stop_loss)

    # 🔥 校验：所有止损价必须为正数
    for idx, sl in enumerate(stop_loss_steps):
        if sl <= 0:
            raise ValueError(f"❌ 第 {idx + 1} 层止损价 {sl} 必须大于 0")

    # 🔥 生成 batch_id（使用 UUID 确保唯一性，避免同一秒内重复执行导致冲突）
    from datetime import datetime
    batch_id = f"batch_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"

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