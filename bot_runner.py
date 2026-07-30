# bot_runner.py
import os
import sys
import json
import logging
import asyncio
from datetime import datetime
from dotenv import load_dotenv

# 1. 强制将当前文件所在目录加入 sys.path
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)
from telegram.request import HTTPXRequest
from telegram.error import NetworkError, TimedOut, RetryAfter, Conflict

# 导入交易核心与解析器
from parser import parse_signal_from_json
from trader_260725 import CryptoTrader

# 2. 加载环境变量
load_dotenv()

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

BOT_TOKEN = os.getenv("TG_BOT_TOKEN")
ALLOWED_USER_ID = os.getenv("TG_ALLOWED_USER_ID")

if not BOT_TOKEN or not ALLOWED_USER_ID:
    raise ValueError("❌ 请先在 .env 文件中配置 TG_BOT_TOKEN 和 TG_ALLOWED_USER_ID！")

ALLOWED_USER_ID = int(ALLOWED_USER_ID.strip())

# 全局异步互斥锁
TRADER_LOCK = asyncio.Lock()

# 💡 用于记录用户的临时交互状态
USER_PENDING_INPUTS = {}

# 🔥 测试命令的确认状态
TEST_CONFIRM_STATE = {}

# 🔥 预设策略模板（支持多空双向）
STRATEGY_TEMPLATES = {
    "btc_100x": {
        "symbol": "BTCUSDT",
        "side": "BUY",
        "leverage": 100,
        "entry_prices": "64650,64800,64900,64999",
        "amounts": "0.001,0.001,0.001,0.001",
        "stop_losses": "64600,64601,64602,64603",
        "tp": 66000,
        "sl": 64000,
    },
    "btc_50x": {
        "symbol": "BTCUSDT",
        "side": "BUY",
        "leverage": 50,
        "entry_prices": "64650,64800,64900,64999",
        "amounts": "0.002,0.002,0.002,0.002",
        "stop_losses": "64600,64601,64602,64603",
        "tp": 66000,
        "sl": 64000,
    },
    "eth_50x": {
        "symbol": "ETHUSDT",
        "side": "BUY",
        "leverage": 50,
        "entry_prices": "3200,3250,3300,3350",
        "amounts": "0.01,0.01,0.01,0.01",
        "stop_losses": "3180,3200,3220,3240",
        "tp": 3500,
        "sl": 3100,
    },
    "btc_100x_short": {
        "symbol": "BTCUSDT",
        "side": "SELL",
        "leverage": 100,
        "entry_prices": "65100,65000,64900,64800",
        "amounts": "0.001,0.001,0.001,0.001",
        "stop_losses": "65500,65450,65400,65350",
        "tp": 64000,
        "sl": 65700,
    },
}


# ==================== Watchdog 支持 ====================
def check_watchdog_notify():
    """检查 watchdog 是否有通知需要发送"""
    try:
        notify_file = os.path.join(BASE_DIR, ".notify")
        if os.path.exists(notify_file):
            with open(notify_file, "r", encoding="utf-8") as f:
                content = f.read().strip()
            os.remove(notify_file)

            if '|' in content:
                parts = content.split('|')
                if len(parts) >= 2:
                    return {'type': parts[1], 'msg': parts[1]}
            return {'type': 'unknown', 'msg': content}
    except Exception:
        pass
    return None


def is_authorized(user_id: int) -> bool:
    if int(user_id) != ALLOWED_USER_ID:
        logging.warning(f"🚫 拦截到未经授权的访问请求，User ID: {user_id}")
        return False
    return True


async def safe_reply(update: Update, text: str, parse_mode=None, reply_markup=None):
    """带网络超时重试保护的回复函数"""
    try:
        if update.message:
            return await update.message.reply_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
        elif update.callback_query and update.callback_query.message:
            return await update.callback_query.message.reply_text(text, parse_mode=parse_mode,
                                                                  reply_markup=reply_markup)
    except Exception as e:
        logging.error(f"⚠️ 回复消息给 Telegram 失败: {e}")


def get_standard_markup(batch_id: str) -> InlineKeyboardMarkup:
    """
    生成批次卡片的交互按钮（3个核心按钮）
    🔒 保本 - 点击后弹出确认对话框
    💰 平仓 - 点击后弹出模式选择
    🗑️ 撤单 - 点击后弹出确认对话框
    """
    keyboard = [
        [
            InlineKeyboardButton("🔒 保本", callback_data=f"be_{batch_id}"),
            InlineKeyboardButton("💰 平仓", callback_data=f"close_{batch_id}"),
            InlineKeyboardButton("🗑️ 撤单", callback_data=f"cancel_{batch_id}"),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """启动命令 - 简洁版"""
    if not is_authorized(update.effective_user.id):
        await safe_reply(update, "🚫 未授权的访问！")
        return

    welcome_msg = (
        "🤖 **加密货币自动化交易 Bot**\n\n"
        "输入 `/help` 查看所有可用指令和示例。\n"
        "输入 `/status` 查看当前批次状态。"
    )
    await safe_reply(update, welcome_msg, parse_mode='Markdown')


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """显示所有可用指令（纯文本模式）"""
    if not is_authorized(update.effective_user.id):
        await safe_reply(update, "🚫 未授权的访问！")
        return

    help_msg = (
        "🤖 Bot 可用指令\n\n"
        "📊 查询类\n"
        "• /status - 查看所有活跃批次状态\n"
        "• /summary - 查看持仓汇总（含浮动盈亏）\n"
        "• /list_templates - 查看所有预设模板\n\n"

        "📝 下单类\n"
        "• /signal [Symbol] [Side] [Leverage] [EntryPrices] [Amounts] [StopLosses] [TP] [InitialSL]\n"
        "  示例: /signal BTCUSDT SELL 100 64230,64228,64160,64100 0.001,0.001,0.001,0.001 64800,64700,64600,64500 64000 64800\n"
        "• /signal_template [模板名] - 使用预设模板下单\n"
        "  示例: /signal_template btc_100x_short\n"
        "• /test [Symbol] [Side] [Leverage] - 生成远离市价的测试挂单\n"
        "  示例: /test BTCUSDT SELL 100\n\n"

        "🎯 风控/管理\n"
        "• /be [BatchID] - 一键保本损\n"
        "  示例: /be batch_20260729_210945\n"
        "• /close [BatchID] - 平仓（交互式选择模式）\n"
        "  示例: /close batch_20260729_210945\n"
        "• /cancel [BatchID] - 取消该批次所有未成交挂单\n"
        "  示例: /cancel batch_20260729_210945\n\n"

        "📁 文件/JSON\n"
        "• 发送 JSON 文本 - 覆盖 signal.json 并开仓\n"
        "• 上传 signal.json 文件 - 覆盖并开仓\n\n"

        "📋 可用模板\n"
        f"• {', '.join(STRATEGY_TEMPLATES.keys())}\n\n"

        "💡 提示：发送 /list_templates 查看模板详情\n"
        "💡 批次卡片上的 [保本] [平仓] [撤单] 按钮可快速操作"
    )
    await safe_reply(update, help_msg, parse_mode=None)


async def list_templates_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """列出所有预设模板"""
    if not is_authorized(update.effective_user.id):
        await safe_reply(update, "🚫 未授权的访问！")
        return

    if not STRATEGY_TEMPLATES:
        await safe_reply(update, "📭 当前没有预设模板。")
        return

    msg = "📋 **可用预设模板：**\n\n"
    for name, config in STRATEGY_TEMPLATES.items():
        msg += (
            f"🔹 `{name}`\n"
            f"  ├─ 标的: {config['symbol']} {config['side']} {config['leverage']}x\n"
            f"  ├─ 入场: {config['entry_prices']}\n"
            f"  ├─ 数量: {config['amounts']}\n"
            f"  ├─ 止损: {config['stop_losses']}\n"
            f"  ├─ 止盈: {config['tp']} | 初始止损: {config['sl']}\n\n"
        )

    await safe_reply(update, msg, parse_mode='Markdown')


async def summary_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """查看持仓汇总（含浮动盈亏）"""
    if not is_authorized(update.effective_user.id):
        await safe_reply(update, "🚫 未授权的访问！")
        return

    trader = context.bot_data.get('global_trader')
    if trader is None:
        await safe_reply(update, "❌ 交易引擎尚未初始化，请稍后再试。")
        return

    try:
        summaries = trader.get_all_batches_summary()

        if not summaries:
            await safe_reply(update, "📊 当前没有持仓。")
            return

        total_pnl = 0.0
        total_amount = 0.0

        msg = "📊 **持仓汇总**\n"
        msg += f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"

        for s in summaries:
            emoji = "📈" if s['side'] == 'BUY' else "📉"
            pnl_emoji = "🟢" if s['unrealized_pnl'] >= 0 else "🔴"

            msg += (
                f"{emoji} **{s['symbol']}** | {s['side']} | {s['leverage']}x\n"
                f"├─ 持仓: `{s['filled_amount']:.4f}` ({s['filled_count']}/{s['entry_count']}层)\n"
                f"├─ 均价: `{s['avg_price']:.2f}` | 市价: `{s['current_price']:.2f}`\n"
                f"├─ 浮动盈亏: {pnl_emoji} `{s['unrealized_pnl']:+.2f}` USDT (`{s['unrealized_pnl_pct']:+.2f}%`)\n"
                f"├─ 止盈: `{s['take_profit']:.2f}` | 止损: `{s['stop_loss']:.2f}`\n"
                f"└─ 批次号: `{s['batch_id']}`\n\n"
            )

            total_pnl += s['unrealized_pnl']
            total_amount += s['filled_amount']

        msg += f"📊 **总览**\n"
        msg += f"├─ 总持仓: `{total_amount:.4f}`\n"
        msg += f"└─ 总浮动盈亏: `{total_pnl:+.2f}` USDT"

        await safe_reply(update, msg, parse_mode='Markdown')

    except Exception as e:
        await safe_reply(update, f"⚠️ 获取汇总失败: {e}")


async def test_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /test 命令 - 生成远离市价的测试挂单
    格式: /test BTCUSDT BUY 100
    或: /test BTCUSDT SELL 50
    """
    if not is_authorized(update.effective_user.id):
        await safe_reply(update, "🚫 未授权的访问！")
        return

    try:
        args = context.args
        if len(args) < 3:
            await safe_reply(
                update,
                "❌ 参数不足！标准格式：\n"
                "`/test [Symbol] [Side] [Leverage]`\n\n"
                "示例：\n"
                "`/test BTCUSDT BUY 100` - 生成做多测试挂单\n"
                "`/test BTCUSDT SELL 50` - 生成做空测试挂单\n\n"
                "💡 测试挂单价格远离当前市价，不会意外触发。",
                parse_mode='Markdown'
            )
            return

        symbol = args[0].upper()
        side = args[1].upper()
        if side not in ["BUY", "SELL"]:
            await safe_reply(update, f"❌ 方向 `{side}` 无效，必须为 BUY 或 SELL", parse_mode='Markdown')
            return

        leverage = int(args[2])
        if leverage <= 0:
            await safe_reply(update, f"❌ 杠杆 `{leverage}` 必须大于 0", parse_mode='Markdown')
            return

        trader = context.bot_data.get('global_trader')
        if trader is None:
            await safe_reply(update, "❌ 交易引擎尚未初始化，请稍后再试。")
            return

        try:
            ticker = trader.exchange.fetch_ticker(symbol)
            current_price = float(ticker.get('last') or ticker.get('close') or 0.0)
            if current_price <= 0:
                await safe_reply(update, f"❌ 无法获取 `{symbol}` 的当前市价，请检查交易对是否正确。",
                                 parse_mode='Markdown')
                return
        except Exception as e:
            await safe_reply(update, f"❌ 获取市价失败: {e}", parse_mode='Markdown')
            return

        if side == 'BUY':
            entry_prices = [
                round(current_price * 1.20, 1),
                round(current_price * 1.40, 1),
                round(current_price * 1.60, 1),
                round(current_price * 1.80, 1),
            ]
            stop_losses = [
                round(current_price * 0.80, 1),
                round(current_price * 0.85, 1),
                round(current_price * 0.90, 1),
                round(current_price * 0.95, 1),
            ]
            tp = round(current_price * 2.00, 1)
            sl = round(current_price * 0.50, 1)
            direction_emoji = "📈"
            direction_text = "做多"
        else:
            entry_prices = [
                round(current_price * 0.80, 1),
                round(current_price * 0.60, 1),
                round(current_price * 0.40, 1),
                round(current_price * 0.20, 1),
            ]
            stop_losses = [
                round(current_price * 1.20, 1),
                round(current_price * 1.15, 1),
                round(current_price * 1.10, 1),
                round(current_price * 1.05, 1),
            ]
            tp = round(current_price * 0.50, 1)
            sl = round(current_price * 2.00, 1)
            direction_emoji = "📉"
            direction_text = "做空"

        test_amount = 0.001
        amounts = [str(test_amount), str(test_amount), str(test_amount), str(test_amount)]

        entries = []
        for p, a, sl_stop in zip(entry_prices, amounts, stop_losses):
            entries.append({"trigger_price": p, "amount": float(a), "stop_loss": sl_stop})

        signal_data = {
            "symbol": symbol,
            "side": side,
            "leverage": leverage,
            "entries": entries,
            "take_profit": tp,
            "initial_stop_loss": sl,
        }

        confirm_msg = (
            f"{direction_emoji} **测试挂单确认**\n\n"
            f"🪙 标的: `{symbol}`\n"
            f"📊 方向: `{direction_text}`\n"
            f"⚡ 杠杆: `{leverage}x`\n"
            f"💰 当前市价: `{current_price:.1f}` USDT\n\n"
            f"📋 **4层挂单详情：**\n"
        )
        for i, (p, sl_stop) in enumerate(zip(entry_prices, stop_losses), 1):
            confirm_msg += f"  │ 第{i}层: 入场 `{p:.1f}` | 数量 `{test_amount}` | 止损 `{sl_stop:.1f}`\n"
        confirm_msg += f"\n🎯 止盈目标: `{tp:.1f}`\n"
        confirm_msg += f"🛡️ 初始止损: `{sl:.1f}`\n\n"
        confirm_msg += (
            "⚠️ **所有价格远离当前市价，不会意外触发！**\n"
            "✅ 确认请回复: `YES`\n"
            "❌ 取消请回复: `NO`"
        )

        chat_id = update.effective_chat.id
        TEST_CONFIRM_STATE[chat_id] = {
            "action": "test_confirm",
            "signal_data": signal_data,
            "confirm_msg": confirm_msg,
        }

        await safe_reply(update, confirm_msg, parse_mode='Markdown')

    except ValueError as e:
        await safe_reply(update, f"❌ 数字格式错误: {e}", parse_mode='Markdown')
    except Exception as e:
        await safe_reply(update, f"❌ 生成测试挂单失败: {e}", parse_mode='Markdown')


async def signal_template_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """使用预设模板下单"""
    if not is_authorized(update.effective_user.id):
        await safe_reply(update, "🚫 未授权的访问！")
        return

    try:
        args = context.args
        if len(args) < 1:
            await safe_reply(
                update,
                "❌ 请指定模板名称！\n"
                "可用模板：" + ", ".join(STRATEGY_TEMPLATES.keys()) + "\n"
                                                                     "示例：/signal_template btc_100x",
                parse_mode='Markdown'
            )
            return

        template_name = args[0].lower()
        if template_name not in STRATEGY_TEMPLATES:
            await safe_reply(
                update,
                f"❌ 未找到模板 `{template_name}`\n"
                "可用模板：" + ", ".join(STRATEGY_TEMPLATES.keys()),
                parse_mode='Markdown'
            )
            return

        config = STRATEGY_TEMPLATES[template_name]

        if len(args) >= 2:
            override_entry_prices = args[1]
            try:
                override_prices = [float(p) for p in override_entry_prices.split(',')]
                if len(override_prices) != 4:
                    await safe_reply(
                        update,
                        f"❌ 覆盖参数需要 4 个价格，当前为 {len(override_prices)} 个",
                        parse_mode='Markdown'
                    )
                    return
                config = config.copy()
                config['entry_prices'] = override_entry_prices
            except ValueError:
                await safe_reply(update, "❌ 覆盖价格格式错误，请用逗号分隔 4 个数字", parse_mode='Markdown')
                return

        entry_prices = [float(x) for x in config["entry_prices"].split(',')]
        amounts = [float(x) for x in config["amounts"].split(',')]
        stop_losses = [float(x) for x in config["stop_losses"].split(',')]

        signal_data = {
            "symbol": config["symbol"],
            "side": config["side"],
            "leverage": config["leverage"],
            "entries": [
                {"trigger_price": p, "amount": a, "stop_loss": sl}
                for p, a, sl in zip(entry_prices, amounts, stop_losses)
            ],
            "take_profit": config["tp"],
            "initial_stop_loss": config["sl"],
        }

        signal_file = os.path.join(BASE_DIR, "signal.json")
        with open(signal_file, "w", encoding="utf-8") as f:
            json.dump(signal_data, f, indent=4, ensure_ascii=False)

        await safe_reply(
            update,
            f"🎯 模板 `{template_name}` 已生效！\n"
            f"📊 {config['symbol']} {config['side']} {config['leverage']}x\n"
            f"📈 入场价: {config['entry_prices']}\n"
            f"🛡️ 止损: {config['stop_losses']}\n"
            f"🎯 止盈: {config['tp']} | 初始止损: {config['sl']}\n\n"
            f"⏳ 正在唤起交易引擎...",
            parse_mode='Markdown'
        )
        asyncio.create_task(run_trader_execution(update, context))

    except Exception as e:
        await safe_reply(update, f"❌ 执行模板失败: {e}")


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """查看当前活跃交易批次状态并附带快捷按钮"""
    if not is_authorized(update.effective_user.id):
        await safe_reply(update, "🚫 未授权的访问！")
        return

    trader = context.bot_data.get('global_trader')
    if trader is None:
        await safe_reply(update, "❌ 交易引擎尚未初始化，请稍后再试。")
        return

    try:
        # ================================================================
        # 🔥 被动清理：显示前验证批次是否真的有效
        # ================================================================
        all_states = trader.load_all_states()
        cleaned_count = 0
        for symbol, symbol_batches in list(all_states.items()):
            for batch_id, b_data in list(symbol_batches.items()):
                if b_data.get('is_active'):
                    entry_orders = b_data.get('entry_orders', [])
                    last_filled_count = b_data.get('last_filled_count', 0)
                    has_pending = len(entry_orders) > last_filled_count

                    # 检查是否有持仓
                    try:
                        positions = trader._safe_api_call(trader.exchange.fetch_positions, [symbol])
                        current_pos = 0.0
                        for pos in positions:
                            if pos.get('symbol') == symbol or pos.get('info', {}).get('symbol') == \
                                    symbol.replace('/', '').split(':')[0]:
                                current_pos = abs(float(pos.get('contracts', 0) or pos.get('positionAmt', 0)))
                                break
                    except Exception:
                        current_pos = 0.0

                    # 🔥 如果既没有挂单也没有持仓，清理这个无效批次
                    if not has_pending and current_pos == 0:
                        trader.clear_batch_state(symbol, batch_id)
                        cleaned_count += 1
                        print(f"🧹 [被动清理] 清理无效批次 {batch_id}")

        if cleaned_count > 0:
            print(f"🧹 [被动清理] 共清理 {cleaned_count} 个无效批次")
            # 重新加载状态
            all_states = trader.load_all_states()

        # ================================================================
        # 🔥 显示活跃批次
        # ================================================================
        active_summary = []
        total_margin_used = 0.0
        symbol = None
        current_price = 0.0

        for sym, symbol_batches in all_states.items():
            symbol = sym
            try:
                ticker = trader._safe_api_call(trader.exchange.fetch_ticker, symbol)
                current_price = float(ticker.get('last') or ticker.get('close') or 0.0)
            except Exception:
                current_price = 0.0

            for b_id, b_data in symbol_batches.items():
                if b_data.get('is_active'):
                    markup = get_standard_markup(b_id)

                    last_filled = b_data.get('last_filled_count', 0)
                    stop_steps = b_data.get('stop_steps', [])
                    side = b_data.get('side', 'BUY')

                    if last_filled > 0 and stop_steps:
                        if last_filled - 1 < len(stop_steps):
                            current_sl = stop_steps[last_filled - 1]
                        else:
                            current_sl = stop_steps[-1] if stop_steps else 'N/A'
                    else:
                        current_sl = stop_steps[-1] if stop_steps else 'N/A'

                    # 计算待成交层数
                    entry_count = len(b_data.get('entry_orders', []))
                    filled_count = b_data.get('last_filled_count', 0)
                    pending_count = entry_count - filled_count

                    text = (
                        f"📌 **批次** `{b_id}` ({symbol})\n"
                        f"  └─ 方向: `{side}`\n"
                        f"  └─ 成交层数: `{filled_count}/{entry_count}`"
                    )
                    if pending_count > 0:
                        text += f" (待成交: {pending_count}层)"
                    text += (
                        f"\n  └─ 当前止盈: `{b_data.get('take_profit_price')}`\n"
                        f"  └─ 当前止损: `{current_sl}`"
                    )
                    await safe_reply(update, text, parse_mode='Markdown', reply_markup=markup)
                    active_summary.append(b_id)

                    target_amounts = b_data.get('target_amounts', [])
                    leverage = b_data.get('params_base', {}).get('leverage', 100)
                    if current_price > 0:
                        for amount in target_amounts:
                            total_margin_used += (amount * current_price) / leverage

        # 显示保证金总览
        try:
            balance = trader._safe_api_call(trader.exchange.fetch_balance)
            usdt_free = float(balance.get('USDT', {}).get('free', 0.0))

            if total_margin_used > 0 and usdt_free > 0:
                usage_rate = (total_margin_used / (usdt_free + total_margin_used)) * 100
                summary_msg = (
                    f"\n📊 **账户保证金总览**\n"
                    f"💰 可用余额: `{usdt_free:.2f}` USDT\n"
                    f"📊 已用保证金: `{total_margin_used:.2f}` USDT\n"
                    f"📊 使用率: `{usage_rate:.1f}%`"
                )
                await safe_reply(update, summary_msg, parse_mode='Markdown')
        except Exception:
            pass

        if not active_summary:
            await safe_reply(update, "📊 当前没有正在运行的活跃批次。", parse_mode='Markdown')

    except Exception as e:
        await safe_reply(update, f"⚠️ 获取状态失败: {e}")


# ==================== 风控/管理命令 ====================

async def be_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """手动指令: /be B1 (保本损)"""
    if not is_authorized(update.effective_user.id):
        await safe_reply(update, "🚫 未授权的访问！")
        return

    trader = context.bot_data.get('global_trader')
    if trader is None:
        await safe_reply(update, "❌ 交易引擎尚未初始化，请稍后再试。")
        return

    try:
        args = context.args
        if len(args) < 1:
            await safe_reply(update, "❌ 格式错误！正确格式：`/be <BatchID>`", parse_mode='Markdown')
            return

        batch_id = args[0]

        loop = asyncio.get_running_loop()
        new_sl = await loop.run_in_executor(None, trader.set_breakeven_stop_loss, batch_id)

        if new_sl:
            await safe_reply(update, f"🔒 批次 `{batch_id}` 已成功一键保本！止损已拉至持仓均价：`{new_sl}`",
                             parse_mode='Markdown')
        else:
            await safe_reply(update, f"⚠️ 保本损设置失败，批次 `{batch_id}` 可能没有持仓或均价计算出错。",
                             parse_mode='Markdown')
    except Exception as e:
        await safe_reply(update, f"❌ 执行 `/be` 异常: {e}")


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    手动指令: /cancel <BatchID>
    取消该批次所有未成交的开仓条件单
    """
    if not is_authorized(update.effective_user.id):
        await safe_reply(update, "🚫 未授权的访问！")
        return

    trader = context.bot_data.get('global_trader')
    if trader is None:
        await safe_reply(update, "❌ 交易引擎尚未初始化，请稍后再试。")
        return

    try:
        args = context.args
        if len(args) < 1:
            await safe_reply(update, "❌ 格式错误！正确格式：`/cancel <BatchID>`", parse_mode='Markdown')
            return

        batch_id = args[0]

        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, trader.cancel_open_orders, batch_id)

        if result[0]:
            await safe_reply(update, result[1], parse_mode='Markdown')
        else:
            await safe_reply(update, result[1], parse_mode='Markdown')

    except Exception as e:
        await safe_reply(update, f"❌ 执行 `/cancel` 异常: {e}", parse_mode='Markdown')


async def close_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    手动指令: /close <BatchID>
    交互式平仓，用户选择模式
    """
    if not is_authorized(update.effective_user.id):
        await safe_reply(update, "🚫 未授权的访问！")
        return

    trader = context.bot_data.get('global_trader')
    if trader is None:
        await safe_reply(update, "❌ 交易引擎尚未初始化，请稍后再试。")
        return

    try:
        args = context.args
        if len(args) < 1:
            await safe_reply(update, "❌ 格式错误！正确格式：`/close <BatchID>`", parse_mode='Markdown')
            return

        batch_id = args[0]

        # 检查批次是否存在且活跃
        all_states = trader.load_all_states()
        target_b_data = None
        target_symbol = None
        for symbol, symbol_batches in all_states.items():
            if batch_id in symbol_batches and symbol_batches[batch_id].get('is_active'):
                target_symbol = symbol
                target_b_data = symbol_batches[batch_id]
                break

        if not target_b_data:
            await safe_reply(update, f"❌ 未找到活跃批次 `{batch_id}`", parse_mode='Markdown')
            return

        # 检查是否有持仓
        last_filled_count = target_b_data.get('last_filled_count', 0)
        target_amounts = target_b_data.get('target_amounts', [])
        current_filled_amount = sum(target_amounts[:last_filled_count])

        if current_filled_amount <= 0:
            await safe_reply(update, f"⚠️ 批次 `{batch_id}` 尚未建仓，无需平仓。", parse_mode='Markdown')
            return

        # 获取当前市价和盈亏信息
        try:
            ticker = trader.exchange.fetch_ticker(target_symbol)
            current_price = float(ticker.get('last') or ticker.get('close') or 0.0)
        except Exception:
            current_price = 0.0

        # 计算均价
        filled_details = target_b_data.get('filled_details', [])
        total_cost = sum([target_amounts[i] * filled_details[i] for i in range(last_filled_count)])
        total_entry_fee = target_b_data.get('total_entry_fee', 0.0)
        avg_price = (total_cost + total_entry_fee) / current_filled_amount if current_filled_amount > 0 else 0

        # 计算盈亏
        side = target_b_data.get('side', 'BUY')
        if side == 'BUY':
            unrealized_pnl = (current_price - avg_price) * current_filled_amount
        else:
            unrealized_pnl = (avg_price - current_price) * current_filled_amount

        pnl_emoji = "🟢" if unrealized_pnl >= 0 else "🔴"

        # 构建平仓选项消息（使用按钮）
        close_msg = (
            f"💰 **选择平仓方式**\n\n"
            f"🆔 批次：`{batch_id}`\n"
            f"🪙 标的：`{target_symbol}`\n"
            f"📊 方向：`{side}`\n"
            f"📊 持仓：`{current_filled_amount}` ({last_filled_count}层)\n"
            f"📈 均价：`{avg_price:.2f}` USDT\n"
            f"💰 当前市价：`{current_price:.2f}` USDT\n"
            f"{pnl_emoji} 浮动盈亏：`{unrealized_pnl:+.2f}` USDT\n\n"
            f"请点击下方按钮选择平仓方式："
        )

        keyboard = [
            [
                InlineKeyboardButton("🚀 市价平仓", callback_data=f"close_market_{batch_id}"),
                InlineKeyboardButton("💎 最优价挂单", callback_data=f"close_limit_{batch_id}"),
            ],
            [
                InlineKeyboardButton("✏️ 自定义价格", callback_data=f"close_custom_{batch_id}"),
                InlineKeyboardButton("❌ 取消", callback_data=f"close_cancel_{batch_id}"),
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await safe_reply(update, close_msg, parse_mode='Markdown', reply_markup=reply_markup)

    except Exception as e:
        await safe_reply(update, f"❌ 执行 `/close` 异常: {e}", parse_mode='Markdown')


# ==================== 保留但隐藏的旧命令（向后兼容） ====================

async def tp_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """手动指令: /tp B1 68800 (已隐藏，但保留功能)"""
    if not is_authorized(update.effective_user.id):
        await safe_reply(update, "🚫 未授权的访问！")
        return

    trader = context.bot_data.get('global_trader')
    if trader is None:
        await safe_reply(update, "❌ 交易引擎尚未初始化，请稍后再试。")
        return

    try:
        args = context.args
        if len(args) < 2:
            await safe_reply(update, "❌ 格式错误！正确格式：`/tp <BatchID> <Price>`", parse_mode='Markdown')
            return

        batch_id = args[0]
        new_tp = float(args[1])

        loop = asyncio.get_running_loop()
        success = await loop.run_in_executor(None, trader.update_take_profit, batch_id, new_tp)

        if success:
            await safe_reply(update, f"✅ 批次 `{batch_id}` 止盈已成功更新为：`{new_tp}`", parse_mode='Markdown')
        else:
            await safe_reply(update, f"⚠️ 更新止盈失败，未找到活跃批次 `{batch_id}` 或订单异常。", parse_mode='Markdown')
    except Exception as e:
        await safe_reply(update, f"❌ 执行 `/tp` 异常: {e}")


async def sl_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """手动指令: /sl B1 62500 (已隐藏，但保留功能)"""
    if not is_authorized(update.effective_user.id):
        await safe_reply(update, "🚫 未授权的访问！")
        return

    trader = context.bot_data.get('global_trader')
    if trader is None:
        await safe_reply(update, "❌ 交易引擎尚未初始化，请稍后再试。")
        return

    try:
        args = context.args
        if len(args) < 2:
            await safe_reply(update, "❌ 格式错误！正确格式：`/sl <BatchID> <Price>`", parse_mode='Markdown')
            return

        batch_id = args[0]
        new_sl = float(args[1])

        loop = asyncio.get_running_loop()
        success = await loop.run_in_executor(None, trader.update_stop_loss, batch_id, new_sl)

        if success:
            await safe_reply(update, f"✅ 批次 `{batch_id}` 止损已成功更新为：`{new_sl}`", parse_mode='Markdown')
        else:
            await safe_reply(update, f"⚠️ 更新止损失败，未找到活跃批次 `{batch_id}` 或订单异常。", parse_mode='Markdown')
    except Exception as e:
        await safe_reply(update, f"❌ 执行 `/sl` 异常: {e}")


# ==================== 按钮回调处理器 ====================

async def button_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理消息下方内联按钮点击交互"""
    try:
        await update.callback_query.answer()
    except Exception as e:
        logging.warning(f"⚠️ 应答 callback_query 失败: {e}")

    if not is_authorized(update.effective_user.id):
        return

    trader = context.bot_data.get('global_trader')
    if trader is None:
        await update.callback_query.message.reply_text("❌ 交易引擎尚未初始化，请稍后再试。")
        return

    data = update.callback_query.data
    chat_id = update.callback_query.message.chat_id
    loop = asyncio.get_running_loop()

    # ========== 辅助函数：从 callback_data 中提取 batch_id ==========
    def extract_batch_id(data: str, prefix: str) -> tuple:
        """
        从 callback_data 中提取 action_type 和 batch_id
        返回: (action_type, batch_id)
        """
        parts = data.split("_")
        # 去掉前缀后的部分
        remaining = parts[1:] if len(parts) > 1 else []

        # 检查是否有 action_type (如 market, limit, custom, cancel, confirm)
        if remaining and remaining[0] in ["market", "limit", "custom", "cancel", "confirm"]:
            action_type = remaining[0]
            batch_id = "_".join(remaining[1:]) if len(remaining) > 1 else ""
        else:
            action_type = None
            batch_id = "_".join(remaining) if remaining else ""

        return action_type, batch_id

    # ========== 保本 ==========
    if data.startswith("be_"):
        # 🔥 清理可能残留的旧状态
        USER_PENDING_INPUTS.pop(chat_id, None)

        batch_id = data.replace("be_", "")

        # 获取批次状态，计算保本价
        all_states = trader.load_all_states()
        target_b_data = None
        target_symbol = None

        for symbol, symbol_batches in all_states.items():
            if batch_id in symbol_batches and symbol_batches[batch_id].get('is_active'):
                target_symbol = symbol
                target_b_data = symbol_batches[batch_id]
                break

        if not target_b_data:
            await update.callback_query.message.reply_text(
                f"❌ 未找到活跃批次 `{batch_id}`",
                parse_mode='Markdown'
            )
            return

        # 计算保本信息
        last_filled_count = target_b_data.get('last_filled_count', 0)
        target_amounts = target_b_data.get('target_amounts', [])
        filled_details = target_b_data.get('filled_details', [])
        total_entry_fee = target_b_data.get('total_entry_fee', 0.0)
        current_filled_amount = sum(target_amounts[:last_filled_count])

        if current_filled_amount <= 0:
            await update.callback_query.message.reply_text(
                f"⚠️ 批次 `{batch_id}` 尚未建仓，无法计算保本价！",
                parse_mode='Markdown'
            )
            return

        filled_costs = sum([target_amounts[i] * filled_details[i] for i in range(last_filled_count)])
        nominal_avg = filled_costs / current_filled_amount
        actual_avg = (filled_costs + total_entry_fee) / current_filled_amount

        try:
            ticker = trader._safe_api_call(trader.exchange.fetch_ticker, target_symbol)
            current_price = float(ticker.get('last') or ticker.get('close') or 0.0)
        except Exception:
            current_price = 0.0

        side = target_b_data.get('side', 'BUY')
        fee_amount = total_entry_fee
        fee_percent = (fee_amount / (
                    nominal_avg * current_filled_amount)) * 100 if current_filled_amount > 0 and nominal_avg > 0 else 0

        # 确定保本模式和价格
        if side == 'BUY':
            if current_price >= actual_avg:
                target_price = actual_avg
                mode = "✅ 实际保本（含手续费）"
                mode_desc = "当前市价已覆盖所有成本，使用含费保本价"
            elif current_price >= nominal_avg:
                target_price = nominal_avg
                mode = "⚠️ 名义保本（不含手续费）"
                mode_desc = f"当前市价低于实际保本价 {actual_avg:.2f}，扣除手续费后仍亏损，使用名义保本"
            else:
                await update.callback_query.message.reply_text(
                    f"❌ **无法设置保本损**\n\n"
                    f"📊 当前市价：{current_price:.2f}\n"
                    f"📊 名义均价：{nominal_avg:.2f}\n"
                    f"📊 实际保本价：{actual_avg:.2f}\n"
                    f"💡 当前价格低于成本价，无法保本！",
                    parse_mode='Markdown'
                )
                return
        else:  # SELL
            if current_price <= actual_avg:
                target_price = actual_avg
                mode = "✅ 实际保本（含手续费）"
                mode_desc = "当前市价已覆盖所有成本，使用含费保本价"
            elif current_price <= nominal_avg:
                target_price = nominal_avg
                mode = "⚠️ 名义保本（不含手续费）"
                mode_desc = f"当前市价高于实际保本价 {actual_avg:.2f}，扣除手续费后仍亏损，使用名义保本"
            else:
                await update.callback_query.message.reply_text(
                    f"❌ **无法设置保本损**\n\n"
                    f"📊 当前市价：{current_price:.2f}\n"
                    f"📊 名义均价：{nominal_avg:.2f}\n"
                    f"📊 实际保本价：{actual_avg:.2f}\n"
                    f"💡 当前价格高于成本价，无法保本！",
                    parse_mode='Markdown'
                )
                return

        # 🔥 显示确认对话框
        confirm_msg = (
            f"🔒 **保本损确认**\n\n"
            f"🆔 批次：`{batch_id}`\n"
            f"📈 方向：`{side}`\n"
            f"├─ 名义均价：`{nominal_avg:.2f}`\n"
            f"├─ 手续费：`{fee_amount:.4f}` USDT (`{fee_percent:.3f}%`)\n"
            f"├─ 实际保本价：`{actual_avg:.2f}`\n"
            f"├─ 当前市价：`{current_price:.2f}`\n"
            f"├─ 保本模式：{mode}\n"
            f"└─ 说明：{mode_desc}\n\n"
            f"🛡️ 将设置止损为：`{target_price:.2f}`\n\n"
            f"⚠️ 确认后将立即执行保本损设置！"
        )

        # 保存状态，等待用户确认
        USER_PENDING_INPUTS[chat_id] = {
            "action": "be_confirm",
            "batch_id": batch_id,
            "target_symbol": target_symbol,
            "target_price": target_price,
            "mode": mode,
        }

        keyboard = [
            [
                InlineKeyboardButton("✅ 确认保本", callback_data=f"be_confirm_{batch_id}"),
                InlineKeyboardButton("❌ 取消", callback_data=f"be_cancel_{batch_id}"),
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.callback_query.message.reply_text(confirm_msg, parse_mode='Markdown', reply_markup=reply_markup)

    # ========== 保本确认/取消 ==========
    elif data.startswith("be_confirm_"):
        batch_id = data.replace("be_confirm_", "")
        pending = USER_PENDING_INPUTS.pop(chat_id, {})

        if pending.get("action") != "be_confirm" or pending.get("batch_id") != batch_id:
            await update.callback_query.message.reply_text("⚠️ 状态已过期，请重新操作。")
            return

        target_price = pending["target_price"]
        target_symbol = pending["target_symbol"]
        mode = pending.get("mode", "")

        # 重新验证批次状态
        all_states = trader.load_all_states()
        b_data = all_states.get(target_symbol, {}).get(batch_id, {})
        if not b_data or not b_data.get('is_active'):
            await update.callback_query.message.reply_text(f"❌ 批次 `{batch_id}` 已不存在或已过期")
            return

        # 执行保本损
        success, msg = await loop.run_in_executor(
            None,
            trader._update_sl_no_validation,
            target_symbol, batch_id, b_data, target_price, mode
        )

        if success:
            await update.callback_query.message.reply_text(
                f"🔒 批次 `{batch_id}` 保本损设置成功！\n🛡️ 止损价：`{target_price:.2f}`",
                parse_mode='Markdown'
            )
        else:
            await update.callback_query.message.reply_text(
                f"❌ 保本损设置失败: {msg}",
                parse_mode='Markdown'
            )

    elif data.startswith("be_cancel_"):
        batch_id = data.replace("be_cancel_", "")
        USER_PENDING_INPUTS.pop(chat_id, {})
        await update.callback_query.message.reply_text("❌ 已取消保本损操作。")

    # ========== 平仓 ==========
    elif data.startswith("close_"):
        # 🔥 清理可能残留的旧状态
        USER_PENDING_INPUTS.pop(chat_id, None)

        # 🔥 提取 action_type 和 batch_id
        action_type, batch_id = extract_batch_id(data, "close_")

        # 如果没有 batch_id，尝试从 data 中直接提取
        if not batch_id:
            # 兼容最旧格式: close_batch_xxx
            parts = data.split("_", 1)
            if len(parts) > 1:
                batch_id = parts[1]
            else:
                await update.callback_query.message.reply_text("❌ 无效的平仓请求")
                return

        # 如果是取消操作
        if action_type == "cancel":
            await update.callback_query.message.reply_text("❌ 已取消平仓操作。")
            return

        # 如果没有 action_type，显示模式选择
        if action_type is None:
            # 兼容旧格式: close_batch_xxx
            await _show_close_options(update, context, batch_id)
            return

        # 重新验证批次状态
        all_states = trader.load_all_states()
        target_b_data = None
        target_symbol = None
        for symbol, symbol_batches in all_states.items():
            if batch_id in symbol_batches and symbol_batches[batch_id].get('is_active'):
                target_symbol = symbol
                target_b_data = symbol_batches[batch_id]
                break

        if not target_b_data:
            await update.callback_query.message.reply_text(f"❌ 批次 `{batch_id}` 已不存在或已过期")
            return

        # 检查是否有持仓
        last_filled_count = target_b_data.get('last_filled_count', 0)
        target_amounts = target_b_data.get('target_amounts', [])
        current_filled_amount = sum(target_amounts[:last_filled_count])

        if current_filled_amount <= 0:
            await update.callback_query.message.reply_text(f"⚠️ 批次 `{batch_id}` 尚未建仓，无需平仓。")
            return

        if action_type == "market":
            # 市价平仓
            result = await loop.run_in_executor(
                None,
                trader.close_position_market,
                batch_id
            )
            if result[0]:
                await update.callback_query.message.reply_text(result[1], parse_mode='Markdown')
            else:
                await update.callback_query.message.reply_text(result[1], parse_mode='Markdown')

        elif action_type == "limit":
            # 最优价挂单平仓
            result = await loop.run_in_executor(
                None,
                trader.close_position_limit,
                batch_id,
                None  # price=None 表示最优价
            )
            if result[0]:
                await update.callback_query.message.reply_text(result[1], parse_mode='Markdown')
            else:
                await update.callback_query.message.reply_text(result[1], parse_mode='Markdown')

        elif action_type == "custom":
            # 自定义价格 - 等待用户输入
            USER_PENDING_INPUTS[chat_id] = {
                "action": "close_custom_price",
                "batch_id": batch_id,
            }
            await update.callback_query.message.reply_text(
                f"✏️ 请输入你的目标平仓价格（例如 `64100`）：",
                parse_mode='Markdown'
            )

    # ========== 撤单 ==========
    elif data.startswith("cancel_"):
        # 🔥 清理可能残留的旧状态
        USER_PENDING_INPUTS.pop(chat_id, None)

        # 🔥 提取 action_type 和 batch_id
        action_type, batch_id = extract_batch_id(data, "cancel_")

        # 如果没有 batch_id，尝试从 data 中直接提取
        if not batch_id:
            parts = data.split("_", 1)
            if len(parts) > 1:
                batch_id = parts[1]
            else:
                await update.callback_query.message.reply_text("❌ 无效的撤单请求")
                return

        # 如果是取消操作
        if action_type == "cancel":
            await update.callback_query.message.reply_text("❌ 已取消撤单操作。")
            return

        # 如果没有 action_type，显示确认对话框
        if action_type is None:
            await _show_cancel_confirmation(update, context, batch_id)
            return

        if action_type == "confirm":
            # 用户确认撤单
            # 重新验证批次状态
            all_states = trader.load_all_states()
            target_b_data = None
            target_symbol = None
            for symbol, symbol_batches in all_states.items():
                if batch_id in symbol_batches and symbol_batches[batch_id].get('is_active'):
                    target_symbol = symbol
                    target_b_data = symbol_batches[batch_id]
                    break

            if not target_b_data:
                await update.callback_query.message.reply_text(f"❌ 批次 `{batch_id}` 已不存在或已过期")
                return

            result = await loop.run_in_executor(
                None,
                trader.cancel_open_orders,
                batch_id
            )
            if result[0]:
                await update.callback_query.message.reply_text(result[1], parse_mode='Markdown')
            else:
                await update.callback_query.message.reply_text(result[1], parse_mode='Markdown')

    # ========== 保留但隐藏：改止盈/改止损（回调仍可用） ==========
    elif data.startswith("tp_"):
        batch_id = data.replace("tp_", "")
        USER_PENDING_INPUTS[chat_id] = {"action": "wait_tp", "batch_id": batch_id}
        await update.callback_query.message.reply_text(
            f"🎯 请直接回复针对批次 `{batch_id}` 的新止盈价（例如输入纯数字 `69000` 发送即可）：",
            parse_mode='Markdown'
        )

    elif data.startswith("sl_"):
        batch_id = data.replace("sl_", "")
        USER_PENDING_INPUTS[chat_id] = {"action": "wait_sl", "batch_id": batch_id}
        await update.callback_query.message.reply_text(
            f"🛡️ 请直接回复针对批次 `{batch_id}` 的新止损价（例如输入纯数字 `62500` 发送即可）：",
            parse_mode='Markdown'
        )


async def _show_close_options(update: Update, context: ContextTypes.DEFAULT_TYPE, batch_id: str):
    """显示平仓模式选择按钮（辅助函数）"""
    trader = context.bot_data.get('global_trader')
    if trader is None:
        await update.callback_query.message.reply_text("❌ 交易引擎尚未初始化。")
        return

    all_states = trader.load_all_states()
    target_b_data = None
    target_symbol = None
    for symbol, symbol_batches in all_states.items():
        if batch_id in symbol_batches and symbol_batches[batch_id].get('is_active'):
            target_symbol = symbol
            target_b_data = symbol_batches[batch_id]
            break

    if not target_b_data:
        await update.callback_query.message.reply_text(f"❌ 未找到活跃批次 `{batch_id}`")
        return

    last_filled_count = target_b_data.get('last_filled_count', 0)
    target_amounts = target_b_data.get('target_amounts', [])
    current_filled_amount = sum(target_amounts[:last_filled_count])

    if current_filled_amount <= 0:
        await update.callback_query.message.reply_text(f"⚠️ 批次 `{batch_id}` 尚未建仓，无需平仓。")
        return

    try:
        ticker = trader._safe_api_call(trader.exchange.fetch_ticker, target_symbol)
        current_price = float(ticker.get('last') or ticker.get('close') or 0.0)
    except Exception:
        current_price = 0.0

    filled_details = target_b_data.get('filled_details', [])
    total_cost = sum([target_amounts[i] * filled_details[i] for i in range(last_filled_count)])
    total_entry_fee = target_b_data.get('total_entry_fee', 0.0)
    avg_price = (total_cost + total_entry_fee) / current_filled_amount if current_filled_amount > 0 else 0

    side = target_b_data.get('side', 'BUY')
    if side == 'BUY':
        unrealized_pnl = (current_price - avg_price) * current_filled_amount
    else:
        unrealized_pnl = (avg_price - current_price) * current_filled_amount

    pnl_emoji = "🟢" if unrealized_pnl >= 0 else "🔴"

    close_msg = (
        f"💰 **选择平仓方式**\n\n"
        f"🆔 批次：`{batch_id}`\n"
        f"🪙 标的：`{target_symbol}`\n"
        f"📊 方向：`{side}`\n"
        f"📊 持仓：`{current_filled_amount}` ({last_filled_count}层)\n"
        f"📈 均价：`{avg_price:.2f}` USDT\n"
        f"💰 当前市价：`{current_price:.2f}` USDT\n"
        f"{pnl_emoji} 浮动盈亏：`{unrealized_pnl:+.2f}` USDT\n\n"
        f"请点击下方按钮选择平仓方式："
    )

    keyboard = [
        [
            InlineKeyboardButton("🚀 市价平仓", callback_data=f"close_market_{batch_id}"),
            InlineKeyboardButton("💎 最优价挂单", callback_data=f"close_limit_{batch_id}"),
        ],
        [
            InlineKeyboardButton("✏️ 自定义价格", callback_data=f"close_custom_{batch_id}"),
            InlineKeyboardButton("❌ 取消", callback_data=f"close_cancel_{batch_id}"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.callback_query.message.reply_text(close_msg, parse_mode='Markdown', reply_markup=reply_markup)


async def _show_cancel_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE, batch_id: str):
    """显示撤单确认对话框（辅助函数）"""
    trader = context.bot_data.get('global_trader')
    if trader is None:
        await update.callback_query.message.reply_text("❌ 交易引擎尚未初始化。")
        return

    all_states = trader.load_all_states()
    target_b_data = None
    target_symbol = None
    for symbol, symbol_batches in all_states.items():
        if batch_id in symbol_batches and symbol_batches[batch_id].get('is_active'):
            target_symbol = symbol
            target_b_data = symbol_batches[batch_id]
            break

    if not target_b_data:
        await update.callback_query.message.reply_text(f"❌ 未找到活跃批次 `{batch_id}`")
        return

    entry_orders = target_b_data.get('entry_orders', [])
    last_filled_count = target_b_data.get('last_filled_count', 0)
    pending_count = len(entry_orders) - last_filled_count

    if pending_count <= 0:
        await update.callback_query.message.reply_text(f"ℹ️ 批次 `{batch_id}` 没有未成交的挂单。")
        return

    current_持仓 = sum(target_b_data.get('target_amounts', [])[:last_filled_count])

    pending_layers = list(range(last_filled_count + 1, len(entry_orders) + 1))

    confirm_msg = (
        f"🗑️ **取消挂单确认**\n\n"
        f"🆔 批次：`{batch_id}`\n"
        f"🪙 标的：`{target_symbol}`\n"
        f"📊 当前持仓：`{current_持仓}` ({last_filled_count}层已成交)\n"
        f"📊 待取消挂单：{pending_count}层（第{', '.join(map(str, pending_layers))}层）\n\n"
        f"⚠️ 确认后将撤销所有未成交挂单！\n"
        f"💡 已成交的层将保留持仓，止盈止损单不受影响。"
    )

    keyboard = [
        [
            InlineKeyboardButton("✅ 确认撤单", callback_data=f"cancel_confirm_{batch_id}"),
            InlineKeyboardButton("❌ 取消", callback_data=f"cancel_cancel_{batch_id}"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.callback_query.message.reply_text(confirm_msg, parse_mode='Markdown', reply_markup=reply_markup)


# ==================== 消息处理器 ====================

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理用户上传的 signal.json 文件"""
    if not is_authorized(update.effective_user.id):
        await safe_reply(update, "🚫 未授权的访问！")
        return

    document = update.message.document
    if not document.file_name.endswith(".json"):
        await safe_reply(update, "❌ 请上传 JSON 格式的文件（文件后缀必须为 .json）")
        return

    try:
        file = await document.get_file()
        content = await file.download_as_bytearray()
        signal_data = json.loads(content.decode('utf-8'))

        required_fields = ["symbol", "side", "leverage", "entries", "take_profit", "initial_stop_loss"]
        for field in required_fields:
            if field not in signal_data:
                await safe_reply(
                    update,
                    f"❌ JSON 缺少必填字段 `{field}`\n"
                    f"必填字段：{', '.join(required_fields)}",
                    parse_mode='Markdown'
                )
                return

        signal_file = os.path.join(BASE_DIR, "signal.json")
        with open(signal_file, "w", encoding="utf-8") as f:
            json.dump(signal_data, f, indent=4, ensure_ascii=False)

        await safe_reply(
            update,
            f"✅ `signal.json` 已更新！\n"
            f"📊 {signal_data.get('symbol')} {signal_data.get('side')} {signal_data.get('leverage')}x\n"
            f"⏳ 正在唤起交易引擎...",
            parse_mode='Markdown'
        )
        asyncio.create_task(run_trader_execution(update, context))

    except json.JSONDecodeError as e:
        await safe_reply(update, f"❌ JSON 解析失败: {e}")
    except Exception as e:
        await safe_reply(update, f"❌ 处理文件失败: {e}")


async def handle_json_or_pending_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """拦截普通文本：检查是否为按钮触发后的等待输入，否则按 JSON 信号解析处理"""
    if not is_authorized(update.effective_user.id):
        await safe_reply(update, "🚫 未授权的访问！")
        return

    chat_id = update.effective_chat.id
    text = update.message.text.strip()

    # ========== 检查是否为测试确认 ==========
    if chat_id in TEST_CONFIRM_STATE:
        state = TEST_CONFIRM_STATE.pop(chat_id)
        if state["action"] == "test_confirm":
            upper_text = text.upper()
            if upper_text == "YES":
                signal_data = state["signal_data"]
                signal_file = os.path.join(BASE_DIR, "signal.json")
                with open(signal_file, "w", encoding="utf-8") as f:
                    json.dump(signal_data, f, indent=4, ensure_ascii=False)

                await safe_reply(
                    update,
                    f"✅ 测试挂单已确认！正在执行...\n"
                    f"📊 {signal_data['symbol']} {signal_data['side']} {signal_data['leverage']}x",
                    parse_mode='Markdown'
                )
                asyncio.create_task(run_trader_execution(update, context))
                return
            elif upper_text == "NO":
                await safe_reply(update, "❌ 测试挂单已取消。")
                return
            else:
                # 检查是否发送了新的 JSON 信号
                json_text = text
                if text.startswith("```"):
                    lines = text.split("\n")
                    if len(lines) >= 3:
                        json_lines = lines[1:-1]
                        json_text = "\n".join(json_lines)

                try:
                    signal_data = json.loads(json_text)
                    if all(k in signal_data for k in ["symbol", "side", "leverage", "entries"]):
                        await safe_reply(
                            update,
                            f"ℹ️ 检测到新的 JSON 信号，已自动取消之前的测试挂单。\n"
                            f"✅ 正在执行新的交易信号...",
                            parse_mode='Markdown'
                        )
                        signal_file = os.path.join(BASE_DIR, "signal.json")
                        with open(signal_file, "w", encoding="utf-8") as f:
                            json.dump(signal_data, f, indent=4, ensure_ascii=False)
                        asyncio.create_task(run_trader_execution(update, context))
                        return
                except (json.JSONDecodeError, KeyError, ValueError):
                    pass

                await safe_reply(
                    update,
                    f"❌ 请输入 `YES` 确认或 `NO` 取消。\n\n"
                    f"{state['confirm_msg']}",
                    parse_mode='Markdown'
                )
                TEST_CONFIRM_STATE[chat_id] = state
                return

    # ========== 检查是否为自定义价格输入 ==========
    if chat_id in USER_PENDING_INPUTS:
        pending = USER_PENDING_INPUTS.get(chat_id)
        if pending.get("action") == "close_custom_price":
            try:
                price = float(text)
                if price <= 0:
                    await safe_reply(update, "❌ 价格必须大于 0", parse_mode='Markdown')
                    return

                batch_id = pending["batch_id"]
                loop = asyncio.get_running_loop()
                trader = context.bot_data.get('global_trader')
                if trader is None:
                    await safe_reply(update, "❌ 交易引擎尚未初始化，请稍后再试。")
                    return

                result = await loop.run_in_executor(
                    None,
                    trader.close_position_limit,
                    batch_id,
                    price
                )
                if result[0]:
                    await safe_reply(update, result[1], parse_mode='Markdown')
                else:
                    await safe_reply(update, result[1], parse_mode='Markdown')

                USER_PENDING_INPUTS.pop(chat_id, None)
            except ValueError:
                await safe_reply(update, "❌ 请输入有效的数字价格。", parse_mode='Markdown')
            return

    # ========== 检查是否为改止盈/改止损的等待输入（保留） ==========
    if chat_id in USER_PENDING_INPUTS:
        pending = USER_PENDING_INPUTS.pop(chat_id)
        action = pending.get("action")
        batch_id = pending.get("batch_id")

        if action in ["wait_tp", "wait_sl"]:
            try:
                val = float(text)
                trader = context.bot_data.get('global_trader')
                if trader is None:
                    await safe_reply(update, "❌ 交易引擎尚未初始化，请稍后再试。")
                    return
                loop = asyncio.get_running_loop()

                if action == "wait_tp":
                    success = await loop.run_in_executor(None, trader.update_take_profit, batch_id, val)
                    if success:
                        await safe_reply(update, f"✅ 批次 `{batch_id}` 止盈已成功更新为：`{val}`",
                                         parse_mode='Markdown')
                    else:
                        await safe_reply(update, f"⚠️ 更新批次 `{batch_id}` 止盈失败。", parse_mode='Markdown')

                elif action == "wait_sl":
                    success = await loop.run_in_executor(None, trader.update_stop_loss, batch_id, val)
                    if success:
                        await safe_reply(update, f"✅ 批次 `{batch_id}` 止损已成功更新为：`{val}`",
                                         parse_mode='Markdown')
                    else:
                        await safe_reply(update, f"⚠️ 更新批次 `{batch_id}` 止损失败。", parse_mode='Markdown')
                return
            except ValueError:
                await safe_reply(update, "❌ 输入格式错误，请输入有效的数字价格。操作已取消。")
                return

    # ========== 检查是否为 JSON ==========
    json_text = text
    if text.startswith("```"):
        lines = text.split("\n")
        if len(lines) >= 3:
            json_lines = lines[1:-1]
            json_text = "\n".join(json_lines)
        else:
            await safe_reply(update, "❌ JSON 代码块格式不完整。")
            return
    elif not (text.startswith("{") and text.endswith("}")):
        await safe_reply(update, "❌ 输入格式无效：JSON 信号必须以 `{` 开头并以 `}` 结尾。\n"
                                 "或使用 `/signal`、`/signal_template` 等指令。")
        return

    try:
        signal_data = json.loads(json_text)

        required_fields = ["symbol", "side", "leverage", "entries", "take_profit", "initial_stop_loss"]
        missing_fields = [f for f in required_fields if f not in signal_data]
        if missing_fields:
            await safe_reply(
                update,
                f"❌ JSON 缺少必填字段: {', '.join(missing_fields)}",
                parse_mode='Markdown'
            )
            return

        if not isinstance(signal_data["entries"], list) or len(signal_data["entries"]) == 0:
            await safe_reply(update, "❌ `entries` 必须是非空数组", parse_mode='Markdown')
            return

        for i, entry in enumerate(signal_data["entries"]):
            if not isinstance(entry, dict):
                await safe_reply(update, f"❌ entries[{i}] 必须是对象", parse_mode='Markdown')
                return
            if "trigger_price" not in entry or "amount" not in entry:
                await safe_reply(
                    update,
                    f"❌ entries[{i}] 缺少 `trigger_price` 或 `amount` 字段",
                    parse_mode='Markdown'
                )
                return

        signal_file = os.path.join(BASE_DIR, "signal.json")
        with open(signal_file, "w", encoding="utf-8") as f:
            json.dump(signal_data, f, indent=4, ensure_ascii=False)

        await safe_reply(
            update,
            f"✅ 已成功接收 JSON 信号并更新 `signal.json`！\n"
            f"📊 {signal_data.get('symbol')} {signal_data.get('side')} {signal_data.get('leverage')}x\n"
            f"📈 共 {len(signal_data.get('entries', []))} 层\n"
            f"⏳ 正在唤起交易引擎...",
            parse_mode='Markdown'
        )
        asyncio.create_task(run_trader_execution(update, context))

    except json.JSONDecodeError as e:
        await safe_reply(update, f"❌ JSON 解析失败: {e}\n\n请检查 JSON 格式。")
    except Exception as e:
        await safe_reply(update, f"⚠️ 处理信号失败: {e}")


async def handle_quick_signal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /signal 快捷指令（完整参数，支持多空双向）"""
    if not is_authorized(update.effective_user.id):
        await safe_reply(update, "🚫 未授权的访问！")
        return

    try:
        args = context.args
        if len(args) < 8:
            await safe_reply(
                update,
                "❌ 参数不足！标准格式：\n"
                "`/signal [Symbol] [Side] [Leverage] [EntryPrices] [Amounts] [StopLosses] [TP] [InitialSL]`\n\n"
                "做多示例：\n"
                "`/signal BTCUSDT BUY 100 64650,64800,64900,64999 0.001,0.001,0.001,0.001 64600,64601,64602,64603 66000 64000`\n\n"
                "做空示例：\n"
                "`/signal BTCUSDT SELL 100 65100,65000,64900,64800 0.001,0.001,0.001,0.001 65500,65450,65400,65350 64000 65700`\n\n"
                "💡 提示：使用 `/signal_template` 可快速调用预设模板。",
                parse_mode='Markdown'
            )
            return

        symbol = args[0].upper()
        side = args[1].upper()
        if side not in ["BUY", "SELL"]:
            await safe_reply(update, f"❌ 方向 `{side}` 无效，必须为 BUY 或 SELL", parse_mode='Markdown')
            return

        leverage = int(args[2])

        entry_prices = [float(p) for p in args[3].split(',')]
        amounts = [float(a) for a in args[4].split(',')]
        stop_losses = [float(sl) for sl in args[5].split(',')]
        take_profit = float(args[6])
        initial_sl = float(args[7])

        if len(entry_prices) != len(amounts):
            await safe_reply(
                update,
                f"❌ 价格层级数量 ({len(entry_prices)}) 与金额层级数量 ({len(amounts)}) 不匹配！",
                parse_mode='Markdown'
            )
            return

        if len(entry_prices) != len(stop_losses):
            await safe_reply(
                update,
                f"❌ 价格层级数量 ({len(entry_prices)}) 与止损层级数量 ({len(stop_losses)}) 不匹配！",
                parse_mode='Markdown'
            )
            return

        entries = []
        for p, a, sl in zip(entry_prices, amounts, stop_losses):
            entries.append({"trigger_price": p, "amount": a, "stop_loss": sl})

        signal_data = {
            "symbol": symbol,
            "side": side,
            "leverage": leverage,
            "entries": entries,
            "take_profit": take_profit,
            "initial_stop_loss": initial_sl,
        }

        signal_file = os.path.join(BASE_DIR, "signal.json")
        with open(signal_file, "w", encoding="utf-8") as f:
            json.dump(signal_data, f, indent=4, ensure_ascii=False)

        await safe_reply(
            update,
            f"🎯 快捷信号生效！已更新 `signal.json` ({symbol} {side} {leverage}x)，唤起交易中...",
            parse_mode='Markdown'
        )
        asyncio.create_task(run_trader_execution(update, context))

    except ValueError as e:
        await safe_reply(update, f"❌ 数字格式错误: {e}")
    except Exception as e:
        await safe_reply(update, f"❌ 解析快捷指令失败: {e}")


async def run_trader_execution(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """后台独立运行交易引擎"""
    async with TRADER_LOCK:
        try:
            api_key = os.getenv("BINANCE_API_KEY")
            secret = os.getenv("BINANCE_SECRET")
            proxy_url = os.getenv("BINANCE_PROXY")
            loop = asyncio.get_running_loop()

            trader = CryptoTrader(
                api_key=api_key,
                secret=secret,
                is_demo=False,
                proxy_url=proxy_url,
                tg_bot=context.bot,
                chat_id=update.effective_chat.id,
                loop=loop,
                verbose=False
            )

            signal_file = os.path.join(BASE_DIR, "signal.json")
            signal = parse_signal_from_json(signal_file)

            print(f"\n📋 信号解析完成:")
            print(f"   ├─ 标的: {signal.symbol}")
            print(f"   ├─ 方向: {signal.side}")
            print(f"   ├─ 杠杆: {signal.leverage}x")
            print(f"   ├─ 止盈: {signal.take_profit}")
            print(f"   ├─ 初始止损: {signal.initial_stop_loss}")
            print(f"   ├─ 阶梯层数: {len(signal.entries)}")
            for i, (price, amount) in enumerate(signal.entries, 1):
                sl = signal.stop_loss_steps[i - 1] if i - 1 < len(signal.stop_loss_steps) else 'N/A'
                print(f"   │  └─ 第{i}层: 入场 {price} | 数量 {amount} | 止损 {sl}")
            print(f"   └─ 止盈目标: {signal.take_profit}")

            batch_id = await loop.run_in_executor(None, trader.execute_signal, signal)

            if batch_id:
                # 🔥 获取实际挂单层数
                all_states = trader.load_all_states()
                actual_orders = []
                for symbol, symbol_batches in all_states.items():
                    if batch_id in symbol_batches:
                        actual_orders = symbol_batches[batch_id].get('entry_orders', [])
                        break
                actual_layers = len(actual_orders)
                original_layers = len(signal.entries)
                skipped_layers = original_layers - actual_layers

                markup = get_standard_markup(batch_id)

                if actual_layers == 0:
                    await safe_reply(
                        update,
                        f"⚠️ **所有挂单均被跳过！**\n\n"
                        f"🆔 **批次号**：`{batch_id}`\n"
                        f"🪙 **标的**：`{signal.symbol}`\n"
                        f"📊 所有 {original_layers} 层触发价均不符合逻辑（已低于/高于市价）\n"
                        f"💡 请检查信号参数后重试。",
                        parse_mode='Markdown'
                    )
                    return

                success_msg = (
                    f"✅ **挂单下发成功，监控已就绪！**\n\n"
                    f"🆔 **批次号**：`{batch_id}`\n"
                    f"🪙 **标的**：`{signal.symbol}`\n"
                    f"📈 **方向**：`{signal.side}` ({signal.leverage}x)\n"
                    f"🔢 **实际挂单层数**：{actual_layers} 层"
                )

                if skipped_layers > 0:
                    success_msg += f"\n⚠️ **有 {skipped_layers} 层因价格不合理被跳过**"
                    success_msg += f"\n💡 触发价需{'高于' if signal.side == 'BUY' else '低于'}当前市价才能挂单"

                success_msg += (
                    f"\n🎯 **止盈目标**：`{signal.take_profit}`\n"
                    f"🛡️ **初始止损**：`{signal.initial_stop_loss}`\n\n"
                    f"👀 *交易引擎已进入独立隔离实时风控状态...*"
                )

                await safe_reply(update, success_msg, parse_mode='Markdown', reply_markup=markup)
            else:
                print(f"\n❌ execute_signal 返回 None，请检查上方日志定位失败原因")
                await safe_reply(update, "⚠️ **挂单未执行**（可能触发防冲突机制或资金校验未通过）。",
                                 parse_mode='Markdown')

        except Exception as e:
            logging.exception("🚨 交易引擎运行崩溃:")
            print(f"\n❌ 异常详情: {e}")
            await safe_reply(update, f"🚨 **挂单失败/引擎异常**:\n`{str(e)}`", parse_mode='Markdown')


async def run_trader_recovery_on_startup(trader: CryptoTrader):
    """启动时在后台独立检查并自动恢复历史活跃任务"""
    async with TRADER_LOCK:
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, trader.recover_active_batches)
            print("✅ [启动检测] 历史任务恢复校验完成！")
        except Exception as e:
            logging.exception(f"⚠️ 检查历史活跃任务失败: {e}")


async def send_summary_notification(app: Application):
    """发送持仓汇总到 Telegram"""
    try:
        trader = app.bot_data.get('global_trader')
        if trader is None:
            logging.warning("⚠️ trader 未初始化，无法发送汇总")
            return

        summaries = trader.get_all_batches_summary()
        if not summaries:
            logging.info("📊 启动后无持仓，跳过汇总发送")
            return

        total_pnl = 0.0
        total_amount = 0.0
        msg = "📊 **启动后持仓汇总**\n"
        msg += f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"

        for s in summaries:
            emoji = "📈" if s['side'] == 'BUY' else "📉"
            pnl_emoji = "🟢" if s['unrealized_pnl'] >= 0 else "🔴"
            msg += (
                f"{emoji} **{s['symbol']}** | {s['side']} | {s['leverage']}x\n"
                f"├─ 持仓: `{s['filled_amount']:.4f}` ({s['filled_count']}/{s['entry_count']}层)\n"
                f"├─ 均价: `{s['avg_price']:.2f}` | 市价: `{s['current_price']:.2f}`\n"
                f"├─ 浮动盈亏: {pnl_emoji} `{s['unrealized_pnl']:+.2f}` USDT (`{s['unrealized_pnl_pct']:+.2f}%`)\n"
                f"├─ 止盈: `{s['take_profit']:.2f}` | 止损: `{s['stop_loss']:.2f}`\n"
                f"└─ 批次号: `{s['batch_id']}`\n\n"
            )
            total_pnl += s['unrealized_pnl']
            total_amount += s['filled_amount']

        msg += f"📊 **总浮动盈亏**: `{total_pnl:+.2f}` USDT"

        await app.bot.send_message(
            chat_id=ALLOWED_USER_ID,
            text=msg,
            parse_mode='Markdown'
        )
        logging.info("📊 启动后持仓汇总已发送")
    except Exception as e:
        logging.warning(f"⚠️ 发送启动汇总失败: {e}")


async def on_post_init(app: Application):
    """Telegram Bot 异步事件循环就绪后的回调钩子"""
    try:
        api_key = os.getenv("BINANCE_API_KEY")
        secret = os.getenv("BINANCE_SECRET")
        proxy_url = os.getenv("BINANCE_PROXY")
        loop = asyncio.get_running_loop()

        global_trader = CryptoTrader(
            api_key=api_key,
            secret=secret,
            is_demo=False,
            proxy_url=proxy_url,
            tg_bot=app.bot,
            chat_id=ALLOWED_USER_ID,
            loop=loop,
            verbose=True
        )
        app.bot_data['global_trader'] = global_trader

        asyncio.create_task(run_trader_recovery_on_startup(global_trader))

        async def delayed_summary():
            await asyncio.sleep(5)
            notify_content = check_watchdog_notify()
            if notify_content and notify_content.get('type') == 'summary_restart':
                logging.info("📊 Watchdog 重启，发送持仓汇总")
                await send_summary_notification(app)
            else:
                await send_summary_notification(app)

        asyncio.create_task(delayed_summary())

        try:
            notify_content = check_watchdog_notify()
            if notify_content and notify_content.get('type') != 'summary_restart':
                await app.bot.send_message(
                    chat_id=ALLOWED_USER_ID,
                    text=f"🔄 **Watchdog 通知**\n{notify_content.get('msg', '重启')}",
                    parse_mode='Markdown'
                )
        except Exception as e:
            logging.warning(f"⚠️ 发送启动通知失败: {e}")

    except Exception as e:
        logging.error(f"⚠️ 启动时初始化全局 trader 失败: {e}")


# ==================== 全局错误处理器 ====================
async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """全局错误处理 - 过滤掉正常的网络断开错误"""
    error = context.error

    if isinstance(error, NetworkError):
        error_str = str(error).lower()
        if "disconnected" in error_str or "remoteprotocolerror" in error_str:
            logging.debug(f"ℹ️ Telegram 连接断开（正常）: {error}")
            return

    if isinstance(error, TimedOut):
        logging.debug(f"ℹ️ Telegram 请求超时（正常）: {error}")
        return

    if isinstance(error, Conflict):
        logging.warning(f"⚠️ Telegram Bot 冲突（可能有多个实例）: {error}")
        return

    logging.error("❌ 未处理的异常:", exc_info=error)
    try:
        if update:
            await safe_reply(update, f"⚠️ **系统异常**\n{str(error)[:300]}")
        else:
            await context.bot.send_message(
                chat_id=ALLOWED_USER_ID,
                text=f"⚠️ **系统异常**\n{str(error)[:300]}",
                parse_mode='Markdown'
            )
    except Exception:
        pass


def main():
    proxy_url = os.getenv("BINANCE_PROXY")

    request_kwargs = {"connect_timeout": 30.0, "read_timeout": 30.0, "write_timeout": 30.0}
    if proxy_url:
        request_kwargs["proxy"] = proxy_url
    req = HTTPXRequest(**request_kwargs)

    get_updates_kwargs = {"connect_timeout": 30.0, "read_timeout": 120.0, "write_timeout": 30.0}
    if proxy_url:
        get_updates_kwargs["proxy"] = proxy_url
    get_updates_req = HTTPXRequest(**get_updates_kwargs)

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .request(req)
        .get_updates_request(get_updates_req)
        .post_init(on_post_init)
        .build()
    )

    app.add_error_handler(error_handler)

    # 命令处理器
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("test", test_command))
    app.add_handler(CommandHandler("signal", handle_quick_signal))
    app.add_handler(CommandHandler("signal_template", signal_template_command))
    app.add_handler(CommandHandler("list_templates", list_templates_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("summary", summary_command))

    # 🔥 核心管理命令
    app.add_handler(CommandHandler("be", be_command))
    app.add_handler(CommandHandler("close", close_command))
    app.add_handler(CommandHandler("cancel", cancel_command))

    # 🔥 保留但隐藏的旧命令（向后兼容）
    app.add_handler(CommandHandler("tp", tp_command))
    app.add_handler(CommandHandler("sl", sl_command))

    app.add_handler(CallbackQueryHandler(button_callback_handler))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_json_or_pending_input))

    print("🚀 Telegram Bot 监听服务已启动（已支持完整手动指令与交互按钮）...")
    app.run_polling(bootstrap_retries=-1)


if __name__ == "__main__":
    main()