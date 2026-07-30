import json
import os
import random
import tempfile
import time
import ccxt
import threading
import asyncio
from dotenv import load_dotenv
from parser import TradeSignal, parse_signal_from_json

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

load_dotenv()
STATE_FILE = "trade_state.json"

TAKER_FEE_RATE = 0.0005
MAKER_FEE_RATE = 0.0002
SLIPPAGE_BUFFER = 0.0002


class CryptoTrader:
    def __init__(self, api_key: str, secret: str, is_demo: bool = False, proxy_url: str = None,
                 tg_bot=None, chat_id=None, loop=None, verbose: bool = True):
        exchange_config = {
            'apiKey': api_key,
            'secret': secret,
            'enableRateLimit': True,
            'timeout': 30000,
            'options': {
                'defaultType': 'future',
                'fetchCurrencies': False,
                'adjustForTimeDifference': True,
                'recvWindow': 10000,
            }
        }

        if is_demo:
            exchange_config['urls'] = {
                'api': {
                    'fapiPublic': 'https://testnet.binancefuture.com/fapi/v1',
                    'fapiPrivate': 'https://testnet.binancefuture.com/fapi/v1',
                    'fapiPrivateV2': 'https://testnet.binancefuture.com/fapi/v2',
                }
            }

        if proxy_url:
            exchange_config['proxies'] = {
                'http': proxy_url,
                'https': proxy_url,
            }

        self.exchange = ccxt.binanceusdm(exchange_config)

        self.tg_bot = tg_bot
        self.chat_id = chat_id
        self.loop = loop
        self.verbose = verbose

        if verbose:
            print("正在连接交易所并同步服务器时间/加载元数据...")
        self._safe_api_call(self.exchange.load_time_difference)
        self._safe_api_call(self.exchange.load_markets, True)

        self.last_time_sync = time.time()

    def send_tg_notification(self, text: str, reply_markup=None):
        if self.tg_bot and self.chat_id and self.loop:
            try:
                asyncio.run_coroutine_threadsafe(
                    self.tg_bot.send_message(
                        chat_id=self.chat_id,
                        text=text,
                        parse_mode='Markdown',
                        reply_markup=reply_markup
                    ),
                    self.loop
                )
            except Exception as e:
                print(f"⚠️ 线程跨界发送 Telegram 推送失败: {e}")

    def _safe_api_call(self, func, *args, retries=5, delay=2, **kwargs):
        for i in range(retries):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                err_str = str(e).lower()

                if (isinstance(e, ccxt.RateLimitExceeded) or
                        "429" in err_str or "-1003" in err_str or "too many requests" in err_str):
                    wait_time = 15 * (i + 1)
                    print(f"🛑 [触发交易所频次限制 429] 正在强制休眠避让 {wait_time} 秒 (第 {i + 1} 次重试)...")
                    time.sleep(wait_time)
                    if i == retries - 1:
                        raise e
                    continue

                if isinstance(e, (ccxt.NetworkError, ccxt.ExchangeNotAvailable, ccxt.RequestTimeout)):
                    print(f"⚠️ 网络抖动/超时: {e}，正在第 {i + 1} 次重试...")
                    if i == retries - 1:
                        raise e
                    time.sleep(delay * (i + 1))
                    continue

                if isinstance(e, ccxt.ExchangeError):
                    if "-1021" in err_str or "recvwindow" in err_str:
                        try:
                            self.exchange.load_time_difference()
                            self.last_time_sync = time.time()
                        except Exception:
                            pass
                        if i == retries - 1:
                            raise e
                        time.sleep(1)
                    elif "system maintenance" in err_str or "503" in err_str:
                        print(f"🚧 遇到交易所维护中，休眠 30 秒后重试... ({e})")
                        time.sleep(30)
                    else:
                        if i == retries - 1:
                            raise e
                        time.sleep(delay)
                else:
                    if i == retries - 1:
                        raise e
                    time.sleep(delay)

    def _sync_time_if_needed(self):
        if time.time() - self.last_time_sync > 300:
            try:
                self._safe_api_call(self.exchange.load_time_difference)
                self.last_time_sync = time.time()
            except Exception as e:
                print(f"⚠️ 时间同步微调失败: {e}")

    def load_all_states(self) -> dict:
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                print(f"⚠️ 读取状态文件失败: {e}")
        return {}

    def save_batch_state(self, symbol: str, batch_id: str, batch_data: dict):
        all_states = self.load_all_states()
        if symbol not in all_states:
            all_states[symbol] = {}
        all_states[symbol][batch_id] = batch_data

        dir_name = os.path.dirname(STATE_FILE) or "."
        try:
            with tempfile.NamedTemporaryFile("w", dir=dir_name, delete=False, encoding="utf-8") as tf:
                json.dump(all_states, tf, indent=4, ensure_ascii=False)
                temp_name = tf.name
            os.replace(temp_name, STATE_FILE)
        except Exception as e:
            print(f"⚠️ 保存状态文件失败: {e}")

    def clear_batch_state(self, symbol: str, batch_id: str):
        all_states = self.load_all_states()
        if symbol in all_states and batch_id in all_states[symbol]:
            del all_states[symbol][batch_id]
            if not all_states[symbol]:
                del all_states[symbol]

            dir_name = os.path.dirname(STATE_FILE) or "."
            try:
                with tempfile.NamedTemporaryFile("w", dir=dir_name, delete=False, encoding="utf-8") as tf:
                    json.dump(all_states, tf, indent=4, ensure_ascii=False)
                    temp_name = tf.name
                os.replace(temp_name, STATE_FILE)
                print(f"🧹 批次 [{batch_id}] 状态归档/清理完毕。")
            except Exception as e:
                print(f"⚠️ 清理批次状态失败: {e}")

    def get_batch_summary(self, batch_id: str) -> dict | None:
        """
        获取单个批次的详细汇总信息
        返回: {
            'symbol': str,
            'side': str,
            'leverage': int,
            'filled_amount': float,
            'avg_price': float,
            'current_price': float,
            'unrealized_pnl': float,
            'unrealized_pnl_pct': float,
            'take_profit': float,
            'stop_loss': float,
            'entry_count': int,
            'filled_count': int,
        }
        """
        all_states = self.load_all_states()

        for symbol, symbol_batches in all_states.items():
            if batch_id in symbol_batches:
                b_data = symbol_batches[batch_id]
                if not b_data.get('is_active'):
                    continue

                last_filled_count = b_data.get('last_filled_count', 0)
                target_amounts = b_data.get('target_amounts', [])
                filled_details = b_data.get('filled_details', [])
                total_entry_fee = b_data.get('total_entry_fee', 0.0)

                filled_amount = sum(target_amounts[:last_filled_count])
                if filled_amount <= 0:
                    return None

                total_cost = sum([target_amounts[i] * filled_details[i] for i in range(last_filled_count)])
                avg_price = (total_cost + total_entry_fee) / filled_amount

                try:
                    ticker = self._safe_api_call(self.exchange.fetch_ticker, symbol)
                    current_price = float(ticker.get('last') or ticker.get('close') or 0.0)
                except Exception:
                    current_price = avg_price

                side = b_data.get('side', 'BUY')
                if side == 'BUY':
                    unrealized_pnl = (current_price - avg_price) * filled_amount
                else:
                    unrealized_pnl = (avg_price - current_price) * filled_amount

                capital_base = avg_price * filled_amount
                unrealized_pnl_pct = (unrealized_pnl / capital_base * 100) if capital_base > 0 else 0.0

                stop_steps = b_data.get('stop_steps', [])
                if last_filled_count > 0 and stop_steps:
                    current_sl = stop_steps[last_filled_count - 1] if last_filled_count - 1 < len(stop_steps) else \
                        stop_steps[-1]
                else:
                    current_sl = stop_steps[-1] if stop_steps else 0.0

                return {
                    'symbol': symbol,
                    'batch_id': batch_id,
                    'side': side,
                    'leverage': b_data.get('params_base', {}).get('leverage', 100),
                    'filled_amount': filled_amount,
                    'avg_price': avg_price,
                    'current_price': current_price,
                    'unrealized_pnl': unrealized_pnl,
                    'unrealized_pnl_pct': unrealized_pnl_pct,
                    'take_profit': b_data.get('take_profit_price', 0.0),
                    'stop_loss': current_sl,
                    'entry_count': len(target_amounts),
                    'filled_count': last_filled_count,
                }

        return None

    def get_all_batches_summary(self) -> list:
        """获取所有活跃批次的汇总信息"""
        all_states = self.load_all_states()
        summaries = []

        for symbol, symbol_batches in all_states.items():
            for batch_id, b_data in symbol_batches.items():
                if b_data.get('is_active'):
                    summary = self.get_batch_summary(batch_id)
                    if summary:
                        summaries.append(summary)

        return summaries

    def recover_active_batches(self) -> bool:
        all_states = self.load_all_states()
        has_recovered = False
        stale_batches = []

        for symbol, symbol_batches in all_states.items():
            for batch_id, b_data in symbol_batches.items():
                if b_data.get('is_active'):
                    print(f"\n🔄 [状态恢复] 识别到未完成的历史活跃任务 [{batch_id}] ({symbol})，正在检查...")

                    # 🔥 检查是否有错误标记（之前监控线程崩溃）
                    if b_data.get('monitor_error', False):
                        print(f"  └─ ⚠️ 批次 [{batch_id}] 有错误标记，跳过恢复并清理")
                        stale_batches.append((symbol, batch_id))
                        continue

                    # 🔥 验证批次是否真的还有挂单或持仓
                    entry_orders = b_data.get('entry_orders', [])
                    last_filled_count = b_data.get('last_filled_count', 0)

                    # 检查是否有未成交的挂单
                    has_pending_orders = len(entry_orders) > last_filled_count

                    # 检查是否有持仓
                    try:
                        positions = self._safe_api_call(self.exchange.fetch_positions, [symbol])
                        current_pos = 0.0
                        for pos in positions:
                            if pos.get('symbol') == symbol or pos.get('info', {}).get('symbol') == \
                                    symbol.replace('/', '').split(':')[0]:
                                current_pos = abs(float(pos.get('contracts', 0) or pos.get('positionAmt', 0)))
                                break
                    except Exception:
                        current_pos = 0.0

                    has_position = current_pos > 0

                    # 🔥 如果既没有挂单也没有持仓，清理这个批次
                    if not has_pending_orders and not has_position:
                        print(f"  └─ 🧹 批次 [{batch_id}] 无挂单且无持仓，自动清理")
                        stale_batches.append((symbol, batch_id))
                        continue

                    # 有挂单或持仓，正常恢复
                    print(f"  └─ ✅ 批次 [{batch_id}] 有效，正在接管监控...")
                    has_recovered = True

                    try:
                        leverage = b_data.get('params_base', {}).get('leverage', 100)
                        self._safe_api_call(self.exchange.set_leverage, leverage, symbol)
                        print(f"  └─ ✅ 杠杆已重新设置为: {leverage}x")
                    except Exception as e:
                        print(f"  └─ ⚠️ 设置杠杆失败: {e}")

                    t = threading.Thread(
                        target=self._start_monitoring,
                        kwargs={
                            'symbol': b_data['symbol'],
                            'batch_id': batch_id,
                            'entry_orders': b_data['entry_orders'],
                            'stop_steps': b_data['stop_steps'],
                            'take_profit_price': b_data['take_profit_price'],
                            'current_sl_id': b_data.get('current_sl_id'),
                            'tp_order_id': b_data.get('tp_order_id'),
                            'batch_total_amount': b_data['batch_total_amount'],
                            'target_amounts': b_data.get('target_amounts', []),
                            'params_base': b_data['params_base'],
                            'is_hedge_mode': b_data['is_hedge_mode'],
                            'side': b_data.get('side', 'BUY'),
                            'last_filled_count': b_data.get('last_filled_count', 0),
                            'filled_details': b_data.get('filled_details', None),
                            'total_entry_fee': b_data.get('total_entry_fee', 0.0),
                            'pending_sl_orders': b_data.get('pending_sl_orders', []),
                            'prepared_sl_params': b_data.get('prepared_sl_params', {}),
                            'prepared_tp_params': b_data.get('prepared_tp_params', {}),
                        },
                        daemon=True
                    )
                    t.start()

        # 🔥 清理无效的批次
        for symbol, batch_id in stale_batches:
            self.clear_batch_state(symbol, batch_id)
            print(f"  └─ 🧹 已清理无效批次 [{batch_id}]")

        return has_recovered

    def update_batch_tp(self, batch_id: str, new_tp_price: float) -> tuple[bool, str]:
        all_states = self.load_all_states()
        target_symbol = None
        target_b_data = None

        for symbol, symbol_batches in all_states.items():
            if batch_id in symbol_batches and symbol_batches[batch_id].get('is_active'):
                target_symbol = symbol
                target_b_data = symbol_batches[batch_id]
                break

        if not target_b_data:
            return False, f"❌ 未找到处于活跃状态的批次号 `{batch_id}`"

        filled_details = target_b_data.get('filled_details', [])
        last_filled_count = target_b_data.get('last_filled_count', 0)
        target_amounts = target_b_data.get('target_amounts', [])
        current_filled_amount = sum(target_amounts[:last_filled_count])
        side = target_b_data.get('side', 'BUY')

        formatted_tp_price = float(self.exchange.price_to_precision(target_symbol, new_tp_price))

        if current_filled_amount <= 0:
            target_b_data['take_profit_price'] = formatted_tp_price
            target_b_data['user_modified'] = True
            target_b_data = self._update_prepared_tp_params(target_b_data, target_symbol, formatted_tp_price)
            self.save_batch_state(target_symbol, batch_id, target_b_data)
            print(f"📝 [无持仓预更新] 批次 {batch_id} 止盈已预更新为 {formatted_tp_price} (等待成交后生效)")
            self.send_tg_notification(
                f"📝 批次 `{batch_id}` 止盈已预更新为 `{formatted_tp_price}`\n"
                f"💡 将在首层成交后自动生效，程序不会覆盖此设置。"
            )
            return True, f"✅ 批次 `{batch_id}` 止盈目标已预更新为 `{formatted_tp_price}`（等待首层成交后自动生效）"

        filled_costs = sum([target_amounts[i] * filled_details[i] for i in range(last_filled_count)])
        vwap = filled_costs / current_filled_amount if current_filled_amount > 0 else 0.0

        if side == 'BUY':
            min_profit_price = vwap * (1 + TAKER_FEE_RATE + MAKER_FEE_RATE + SLIPPAGE_BUFFER)
            if formatted_tp_price <= min_profit_price:
                return False, (
                    f"❌ 校验拒绝：新止盈价 (`{formatted_tp_price}`) 过低！\n"
                    f"📊 持仓均价: `{vwap:.2f}`\n"
                    f"📈 最低盈利价: `{min_profit_price:.2f}` (含手续费+滑点缓冲)"
                )
        else:
            max_profit_price = vwap * (1 - TAKER_FEE_RATE - MAKER_FEE_RATE - SLIPPAGE_BUFFER)
            if formatted_tp_price >= max_profit_price:
                return False, (
                    f"❌ 校验拒绝：新止盈价 (`{formatted_tp_price}`) 过高！\n"
                    f"📊 持仓均价: `{vwap:.2f}`\n"
                    f"📉 最高盈利价: `{max_profit_price:.2f}` (含手续费+滑点缓冲)"
                )

        old_tp_id = target_b_data.get('tp_order_id')
        if old_tp_id:
            try:
                self._safe_api_call(self.exchange.cancel_order, old_tp_id, target_symbol, params={'stop': True})
            except Exception as e:
                if "Unknown order" in str(e) or "-2011" in str(e):
                    print(f"ℹ️ 旧止盈单 {old_tp_id} 已不存在，跳过撤销")
                else:
                    print(f"⚠️ 撤销旧止盈单失败: {e}")
                    return False, f"❌ 撤销旧止盈单失败: {e}"

        tp_params = target_b_data['params_base'].copy()
        tp_params['stopPrice'] = formatted_tp_price
        if not target_b_data['is_hedge_mode']:
            tp_params['reduceOnly'] = True

        tp_side = 'sell' if side == 'BUY' else 'buy'

        try:
            new_tp_order = self._safe_api_call(
                self.exchange.create_order,
                symbol=target_symbol,
                type='TAKE_PROFIT_MARKET',
                side=tp_side,
                amount=current_filled_amount,
                params=tp_params
            )
            new_tp_id = new_tp_order['id']

            target_b_data['take_profit_price'] = formatted_tp_price
            target_b_data['tp_order_id'] = new_tp_id
            target_b_data['user_modified'] = True
            self.save_batch_state(target_symbol, batch_id, target_b_data)

            self.send_tg_notification(
                f"✅ 批次 `{batch_id}` 止盈已修改为 `{formatted_tp_price}`\n"
                f"💡 程序已记录您的修改，不会自动覆盖此设置。"
            )

            return True, f"✅ 批次 `{batch_id}` 止盈单已成功修改为 `{formatted_tp_price}` USDT (ID: `{new_tp_id}`)"
        except Exception as e:
            return False, f"❌ 挂出新止盈单失败: {e}"

    def _update_prepared_tp_params(self, batch_data: dict, symbol: str, tp_price: float) -> dict:
        """更新预生成的止盈参数"""
        side = batch_data.get('side', 'BUY')
        prepared_tp_params = batch_data.get('prepared_tp_params', {})

        tp_side = 'sell' if side == 'BUY' else 'buy'
        tp_params = batch_data['params_base'].copy()
        tp_params['stopPrice'] = tp_price
        if not batch_data['is_hedge_mode']:
            tp_params['reduceOnly'] = True

        prepared_tp_params = {
            'symbol': symbol,
            'type': 'TAKE_PROFIT_MARKET',
            'side': tp_side,
            'params': tp_params
        }

        batch_data['prepared_tp_params'] = prepared_tp_params
        return batch_data

    def _update_prepared_sl_params(self, batch_data: dict, symbol: str, sl_price: float) -> dict:
        """更新预生成的止损参数"""
        side = batch_data.get('side', 'BUY')
        prepared_sl_params = batch_data.get('prepared_sl_params', {})

        sl_side = 'sell' if side == 'BUY' else 'buy'
        sl_params = batch_data['params_base'].copy()
        sl_params['stopPrice'] = sl_price
        if not batch_data['is_hedge_mode']:
            sl_params['reduceOnly'] = True

        prepared_sl_params = {
            'symbol': symbol,
            'type': 'STOP_MARKET',
            'side': sl_side,
            'params': sl_params
        }

        batch_data['prepared_sl_params'] = prepared_sl_params
        return batch_data

    def update_batch_sl(self, batch_id: str, new_sl_price: float) -> tuple[bool, str]:
        all_states = self.load_all_states()
        target_symbol = None
        target_b_data = None

        for symbol, symbol_batches in all_states.items():
            if batch_id in symbol_batches and symbol_batches[batch_id].get('is_active'):
                target_symbol = symbol
                target_b_data = symbol_batches[batch_id]
                break

        if not target_b_data:
            return False, f"❌ 未找到处于活跃状态的批次号 `{batch_id}`"

        last_filled_count = target_b_data.get('last_filled_count', 0)
        target_amounts = target_b_data.get('target_amounts', [])
        current_filled_amount = sum(target_amounts[:last_filled_count])
        side = target_b_data.get('side', 'BUY')

        formatted_sl_price = float(self.exchange.price_to_precision(target_symbol, new_sl_price))

        if current_filled_amount <= 0:
            stop_steps = target_b_data.get('stop_steps', [])
            if stop_steps:
                stop_steps[-1] = formatted_sl_price
                target_b_data['stop_steps'] = stop_steps
                target_b_data['user_modified'] = True
                target_b_data = self._update_prepared_sl_params(target_b_data, target_symbol, formatted_sl_price)
                self.save_batch_state(target_symbol, batch_id, target_b_data)
                print(f"📝 [无持仓预更新] 批次 {batch_id} 止损已预更新为 {formatted_sl_price} (等待成交后生效)")
                self.send_tg_notification(
                    f"📝 批次 `{batch_id}` 止损已预更新为 `{formatted_sl_price}`\n"
                    f"💡 将在首层成交后自动生效，程序不会覆盖此设置。"
                )
                return True, f"✅ 批次 `{batch_id}` 阶梯止损目标已预更新为 `{formatted_sl_price}`（等待首层成交后自动生效）"
            else:
                return False, f"❌ 批次 `{batch_id}` 未找到止损阶梯配置"

        ticker = self._safe_api_call(self.exchange.fetch_ticker, target_symbol)
        current_mark_price = float(ticker.get('last') or ticker.get('close') or 0.0)

        if side == 'BUY':
            if formatted_sl_price >= current_mark_price:
                return False, f"❌ 校验拒绝：新止损价 (`{formatted_sl_price}`) 不得高于或等于当前市价 (`{current_mark_price}`)，否则会立即触发！"
        else:
            if formatted_sl_price <= current_mark_price:
                return False, f"❌ 校验拒绝：新止损价 (`{formatted_sl_price}`) 不得低于或等于当前市价 (`{current_mark_price}`)，否则会立即触发！"

        old_sl_id = target_b_data.get('current_sl_id')
        if old_sl_id:
            try:
                self._safe_api_call(self.exchange.cancel_order, old_sl_id, target_symbol, params={'stop': True})
            except Exception as e:
                if "Unknown order" in str(e) or "-2011" in str(e):
                    print(f"ℹ️ 旧止损单 {old_sl_id} 已不存在，跳过撤销")
                else:
                    print(f"⚠️ 撤销旧止损单失败: {e}")
                    return False, f"❌ 撤销旧止损单失败: {e}"

        sl_params = target_b_data['params_base'].copy()
        sl_params['stopPrice'] = formatted_sl_price
        if not target_b_data['is_hedge_mode']:
            sl_params['reduceOnly'] = True

        sl_side = 'sell' if side == 'BUY' else 'buy'

        try:
            new_sl_order = self._safe_api_call(
                self.exchange.create_order,
                symbol=target_symbol,
                type='STOP_MARKET',
                side=sl_side,
                amount=current_filled_amount,
                params=sl_params
            )
            new_sl_id = new_sl_order['id']

            stop_steps = target_b_data.get('stop_steps', [])
            if last_filled_count - 1 < len(stop_steps):
                stop_steps[last_filled_count - 1] = formatted_sl_price
            target_b_data['stop_steps'] = stop_steps
            target_b_data['current_sl_id'] = new_sl_id
            target_b_data['user_modified'] = True
            self.save_batch_state(target_symbol, batch_id, target_b_data)

            self.send_tg_notification(
                f"🛡️ 批次 `{batch_id}` 止损已修改为 `{formatted_sl_price}`\n"
                f"💡 程序已记录您的修改，不会自动覆盖此设置。"
            )

            return True, f"🛡️ 批次 `{batch_id}` 止损单已成功修改为 `{formatted_sl_price}` USDT (ID: `{new_sl_id}`)"
        except Exception as e:
            return False, f"❌ 挂出新止损单失败: {e}"

    def set_breakeven_sl(self, batch_id: str) -> tuple[bool, str]:
        """
        设置保本损，自动选择最优模式
        模式1: 名义保本（不含手续费）- 止损价 = 入场均价
        模式2: 实际保本（含手续费）- 止损价 = 入场均价 + 手续费成本
        """
        all_states = self.load_all_states()
        target_b_data = None
        target_symbol = None

        for symbol, symbol_batches in all_states.items():
            if batch_id in symbol_batches and symbol_batches[batch_id].get('is_active'):
                target_symbol = symbol
                target_b_data = symbol_batches[batch_id]
                break

        if not target_b_data:
            return False, f"❌ 未找到处于活跃状态的批次号 `{batch_id}`"

        last_filled_count = target_b_data.get('last_filled_count', 0)
        target_amounts = target_b_data.get('target_amounts', [])
        filled_details = target_b_data.get('filled_details', [])
        total_entry_fee = target_b_data.get('total_entry_fee', 0.0)
        current_filled_amount = sum(target_amounts[:last_filled_count])

        if current_filled_amount <= 0:
            return False, f"⚠️ 批次 `{batch_id}` 尚未建仓，无法计算保本价！"

        # 计算名义均价（不含手续费）
        filled_costs = sum([target_amounts[i] * filled_details[i] for i in range(last_filled_count)])
        nominal_avg = filled_costs / current_filled_amount

        # 计算含费均价（实际保本价）
        actual_avg = (filled_costs + total_entry_fee) / current_filled_amount

        # 获取当前市价
        try:
            ticker = self._safe_api_call(self.exchange.fetch_ticker, target_symbol)
            current_price = float(ticker.get('last') or ticker.get('close') or 0.0)
        except Exception:
            return False, f"⚠️ 无法获取 `{target_symbol}` 的当前市价"

        side = target_b_data.get('side', 'BUY')
        fee_amount = total_entry_fee
        fee_percent = (fee_amount / (
                nominal_avg * current_filled_amount)) * 100 if current_filled_amount > 0 and nominal_avg > 0 else 0

        # 判断选择哪种保本模式
        if side == 'BUY':
            if current_price >= actual_avg:
                target_price = actual_avg
                mode = "✅ 实际保本（含手续费）"
                mode_desc = "当前市价已覆盖所有成本，使用含费保本价"
            elif current_price >= nominal_avg:
                target_price = nominal_avg
                mode = "⚠️ 名义保本（不含手续费）"
                mode_desc = f"当前市价低于实际保本价 `{actual_avg:.2f}`，扣除手续费后仍亏损，使用名义保本"
            else:
                error_msg = (
                    f"❌ **无法设置保本损**\n\n"
                    f"📊 当前市价：`{current_price:.2f}`\n"
                    f"📊 名义均价：`{nominal_avg:.2f}`\n"
                    f"📊 实际保本价：`{actual_avg:.2f}`\n"
                    f"💸 手续费：`{fee_amount:.4f}` USDT (`{fee_percent:.3f}%`)\n\n"
                    f"⚠️ 当前价格低于名义均价，即使不含手续费也无法保本！\n"
                    f"💡 建议等待价格回升至 `{nominal_avg:.2f}` 以上再尝试。"
                )
                self.send_tg_notification(error_msg)
                return False, "当前价格低于成本价，无法设置保本损"
        else:  # SELL
            if current_price <= actual_avg:
                target_price = actual_avg
                mode = "✅ 实际保本（含手续费）"
                mode_desc = "当前市价已覆盖所有成本，使用含费保本价"
            elif current_price <= nominal_avg:
                target_price = nominal_avg
                mode = "⚠️ 名义保本（不含手续费）"
                mode_desc = f"当前市价高于实际保本价 `{actual_avg:.2f}`，扣除手续费后仍亏损，使用名义保本"
            else:
                error_msg = (
                    f"❌ **无法设置保本损**\n\n"
                    f"📊 当前市价：`{current_price:.2f}`\n"
                    f"📊 名义均价：`{nominal_avg:.2f}`\n"
                    f"📊 实际保本价：`{actual_avg:.2f}`\n"
                    f"💸 手续费：`{fee_amount:.4f}` USDT (`{fee_percent:.3f}%`)\n\n"
                    f"⚠️ 当前价格高于名义均价，即使不含手续费也无法保本！\n"
                    f"💡 建议等待价格回落至 `{nominal_avg:.2f}` 以下再尝试。"
                )
                self.send_tg_notification(error_msg)
                return False, "当前价格高于成本价，无法设置保本损"

        # 构建详细通知
        info_msg = (
            f"🔒 **保本损设置**\n"
            f"🆔 批次：`{batch_id}`\n"
            f"📈 方向：`{side}`\n"
            f"├─ 名义均价：`{nominal_avg:.2f}`\n"
            f"├─ 手续费：`{fee_amount:.4f}` USDT (`{fee_percent:.3f}%`)\n"
            f"├─ 实际保本价：`{actual_avg:.2f}`\n"
            f"├─ 当前市价：`{current_price:.2f}`\n"
            f"├─ 保本模式：{mode}\n"
            f"└─ 说明：{mode_desc}\n\n"
            f"🛡️ 止损将设置为：`{target_price:.2f}`"
        )

        self.send_tg_notification(info_msg)

        # 执行保本损设置（跳过校验）
        return self._update_sl_no_validation(target_symbol, batch_id, target_b_data, target_price, mode)

    def _update_sl_no_validation(self, symbol: str, batch_id: str, b_data: dict, sl_price: float, mode: str = "") -> \
            tuple[bool, str]:
        """
        内部方法：直接更新止损，跳过价格校验（用于保本损）
        此方法不检查止损价是否合理，直接挂单
        """
        formatted_sl_price = float(self.exchange.price_to_precision(symbol, sl_price))

        sl_params = b_data['params_base'].copy()
        sl_params['stopPrice'] = formatted_sl_price
        if not b_data['is_hedge_mode']:
            sl_params['reduceOnly'] = True

        side = b_data.get('side', 'BUY')
        sl_side = 'sell' if side == 'BUY' else 'buy'

        last_filled_count = b_data.get('last_filled_count', 0)
        target_amounts = b_data.get('target_amounts', [])
        current_filled_amount = sum(target_amounts[:last_filled_count])

        try:
            # 撤销旧止损单
            old_sl_id = b_data.get('current_sl_id')
            if old_sl_id:
                try:
                    self._safe_api_call(self.exchange.cancel_order, old_sl_id, symbol, params={'stop': True})
                    print(f"  └─ 已撤销旧止损单: {old_sl_id}")
                except Exception as e:
                    if "Unknown order" in str(e) or "-2011" in str(e):
                        print(f"  └─ 旧止损单 {old_sl_id} 已不存在，跳过")
                    else:
                        print(f"  └─ 撤销旧止损单失败: {e}")

            # 创建新止损单
            new_sl_order = self._safe_api_call(
                self.exchange.create_order,
                symbol=symbol,
                type='STOP_MARKET',
                side=sl_side,
                amount=current_filled_amount,
                params=sl_params
            )
            new_sl_id = new_sl_order['id']

            # 更新状态
            b_data['current_sl_id'] = new_sl_id
            b_data['user_modified'] = True

            # 更新止损阶梯
            stop_steps = b_data.get('stop_steps', [])
            if last_filled_count - 1 < len(stop_steps):
                stop_steps[last_filled_count - 1] = formatted_sl_price
            b_data['stop_steps'] = stop_steps

            self.save_batch_state(symbol, batch_id, b_data)

            result_msg = f"🔒 批次 `{batch_id}` 保本损已设置！\n🛡️ 止损价：`{formatted_sl_price}`"
            if mode:
                result_msg += f"\n📊 模式：{mode}"

            print(f"🔒 [保本损] 批次 {batch_id} 止损已设置为 {formatted_sl_price} (ID: {new_sl_id})")

            return True, result_msg
        except Exception as e:
            return False, f"❌ 设置保本损失败: {e}"

    def update_take_profit(self, batch_id: str, new_price: float) -> bool:
        success, _ = self.update_batch_tp(batch_id, new_price)
        return success

    def update_stop_loss(self, batch_id: str, new_price: float) -> bool:
        success, _ = self.update_batch_sl(batch_id, new_price)
        return success

    def set_breakeven_stop_loss(self, batch_id: str) -> float | None:
        success, msg = self.set_breakeven_sl(batch_id)
        if success:
            import re
            match = re.search(r'`([\d.]+)`', msg)
            if match:
                return float(match.group(1))
            all_states = self.load_all_states()
            for symbol, symbol_batches in all_states.items():
                if batch_id in symbol_batches and symbol_batches[batch_id].get('is_active'):
                    b_data = symbol_batches[batch_id]
                    stop_steps = b_data.get('stop_steps', [])
                    if stop_steps:
                        return stop_steps[-1]
            return None
        else:
            return None

    def _cancel_remaining_entries(self, symbol: str, entry_orders: list, filled_layers: list = None):
        print(f"🧹 正在清理本批次残余开仓挂单...")
        for idx, order_id in enumerate(entry_orders):
            if filled_layers and filled_layers[idx]:
                continue
            try:
                self._safe_api_call(self.exchange.cancel_order, order_id, symbol, params={'stop': True})
                print(f"  └─ 已成功撤销开仓挂单: {order_id}")
            except Exception:
                pass

    def _get_current_position_amt(self, symbol: str, is_hedge_mode: bool, side: str = 'BUY',
                                  retries: int = 3) -> float | None:
        for attempt in range(retries):
            try:
                positions = self._safe_api_call(self.exchange.fetch_positions, [symbol])
                for pos in positions:
                    if pos.get('symbol') == symbol or pos.get('info', {}).get('symbol') == \
                            symbol.replace('/', '').split(':')[0]:
                        if is_hedge_mode:
                            target_side = 'long' if side == 'BUY' else 'short'
                            if pos.get('side') == target_side:
                                return abs(float(pos.get('contracts', 0) or pos.get('positionAmt', 0)))
                        else:
                            return abs(float(pos.get('contracts', 0) or pos.get('positionAmt', 0)))
                return 0.0
            except Exception as e:
                print(f"⚠️ 查询持仓信息失败 (尝试 {attempt + 1}/{retries}): {e}")
                if attempt < retries - 1:
                    time.sleep(1)
        print(f"❌ 查询持仓失败，已重试 {retries} 次")
        return None

    def _get_current_vwap_from_position(self, symbol: str) -> float | None:
        try:
            positions = self._safe_api_call(self.exchange.fetch_positions, [symbol])
            for pos in positions:
                if pos.get('symbol') == symbol or pos.get('info', {}).get('symbol') == \
                        symbol.replace('/', '').split(':')[0]:
                    entry_price = pos.get('entryPrice') or pos.get('info', {}).get('entryPrice')
                    if entry_price:
                        return float(entry_price)
        except Exception as e:
            print(f"⚠️ 查询持仓均价失败: {e}")
        return None

    def _check_existing_conflicts(self, symbol: str, batch_id: str, all_states: dict) -> bool:
        print(f"\n🔍 正在针对批次 [{batch_id}] 进行防冲突扫描...")

        symbol_state = all_states.get(symbol, {})

        if batch_id in symbol_state and symbol_state[batch_id].get('is_active'):
            print(f"❌ 【批次冲突】批次 [{batch_id}] 目前已在运行中！请勿重复执行。")
            return True

        known_order_ids = set()
        for b_id, b_data in symbol_state.items():
            if not b_data.get('is_active'):
                continue
            for order_id in b_data.get('entry_orders', []):
                known_order_ids.add(str(order_id))
            if b_data.get('tp_order_id'):
                known_order_ids.add(str(b_data['tp_order_id']))
            if b_data.get('current_sl_id'):
                known_order_ids.add(str(b_data['current_sl_id']))

        try:
            open_orders = self._safe_api_call(self.exchange.fetch_open_orders, symbol)
        except Exception as e:
            print(f"⚠️ 获取未结订单失败: {e}")
            return False

        unknown_orders = []
        for ord in open_orders:
            ord_id = str(ord['id'])
            if ord_id not in known_order_ids:
                unknown_orders.append(ord)

        if unknown_orders:
            print(f"⚠️ 【未识别挂单提醒】检测到交易所存在 {len(unknown_orders)} 个不受代码管理的“孤儿挂单”！")
            for ord in unknown_orders:
                print(
                    f"   └─ Order ID: {ord['id']} | 类型: {ord['type']} | 方向: {ord['side']} | 触发/委托价: {ord.get('stopPrice') or ord.get('price')}")

            print("🧹 自动清理孤儿挂单中...")
            cleaned_count = 0
            for ord in unknown_orders:
                try:
                    self._safe_api_call(self.exchange.cancel_order, ord['id'], symbol, params={'stop': True})
                    print(f"  └─ ✅ 已撤销: {ord['id']}")
                    cleaned_count += 1
                except Exception as e:
                    print(f"  └─ ⚠️ 撤销失败: {ord['id']} - {e}")

            if cleaned_count > 0:
                print(f"🧹 孤儿挂单已清理完毕 (共清理 {cleaned_count} 个)！")
                time.sleep(0.5)

            return False

        print("✅ 防冲突校验通过：当前批次无重复，其他已存在批次运行正常。")
        return False

    def _validate_stop_losses(self, signal, current_mark_price: float) -> tuple[bool, str]:
        """
        校验所有止损价是否合理
        返回: (是否通过, 错误信息)
        """
        side = signal.side.upper()

        for idx, (trigger_price, amount) in enumerate(signal.entries, 1):
            raw_sl_price = signal.stop_loss_steps[idx - 1] if idx - 1 < len(
                signal.stop_loss_steps) else signal.initial_stop_loss

            if side == 'BUY':
                # 做多：止损价必须低于入场价
                if raw_sl_price >= trigger_price:
                    error_msg = (
                        f"❌ 第 {idx} 层止损价不合理！\n"
                        f"   ├─ 入场价: {trigger_price}\n"
                        f"   ├─ 止损价: {raw_sl_price}\n"
                        f"   └─ 做多时止损价必须 < 入场价（当前 {raw_sl_price} >= {trigger_price}）"
                    )
                    return False, error_msg
                # 额外检查：止损价是否高于当前市价（会立即触发）
                if raw_sl_price >= current_mark_price:
                    warning_msg = (
                        f"⚠️ 第 {idx} 层止损价 {raw_sl_price} 高于当前市价 {current_mark_price}，做多止损单会立即触发！\n"
                        f"   💡 建议将止损价设置在当前市价以下"
                    )
                    print(warning_msg)
                    # 不阻断，只警告
            else:  # SELL
                # 做空：止损价必须高于入场价
                if raw_sl_price <= trigger_price:
                    error_msg = (
                        f"❌ 第 {idx} 层止损价不合理！\n"
                        f"   ├─ 入场价: {trigger_price}\n"
                        f"   ├─ 止损价: {raw_sl_price}\n"
                        f"   └─ 做空时止损价必须 > 入场价（当前 {raw_sl_price} <= {trigger_price}）"
                    )
                    return False, error_msg
                # 额外检查：止损价是否低于当前市价（会立即触发）
                if raw_sl_price <= current_mark_price:
                    warning_msg = (
                        f"⚠️ 第 {idx} 层止损价 {raw_sl_price} 低于当前市价 {current_mark_price}，做空止损单会立即触发！\n"
                        f"   💡 建议将止损价设置在当前市价以上"
                    )
                    print(warning_msg)
                    # 不阻断，只警告

        return True, "✅ 所有止损价合理性校验通过！"

    def execute_signal(self, signal):
        symbol = signal.symbol
        batch_id = signal.batch_id
        all_states = self.load_all_states()
        side = signal.side.upper()

        if self._check_existing_conflicts(symbol, batch_id, all_states):
            return None

        base_currency = symbol.split('/')[0] if '/' in symbol else symbol.replace('USDT', '')

        current_pos = self._get_current_position_amt(symbol, is_hedge_mode=False, side=side)
        if current_pos is None:
            print(f"❌ 无法查询当前持仓，已重试失败，请检查网络后重试")
            return None

        if current_pos > 0:
            print(
                f"📈 【加仓模式】检测到当前已有 {side} 方向基础持仓 {current_pos} {base_currency}，本批次 [{batch_id}] 将独立挂单与独立计算风控！")
        else:
            print(f"🚀 【首仓模式】本批次 [{batch_id}] 为 {side} 方向底仓进场。")

        print(f"👉 开始为交易对 [{symbol}] 执行策略指令 (批次: {batch_id})...")

        try:
            self._safe_api_call(self.exchange.set_leverage, signal.leverage, symbol)
            print(f"✅ 杠杆成功设置为: {signal.leverage}x")

            # 🔥 获取当前市价用于止损价校验
            ticker = self._safe_api_call(self.exchange.fetch_ticker, symbol)
            current_mark_price = float(ticker.get('last') or ticker.get('close') or 0.0)
            print(f"🌐 当前最新市场价格: {current_mark_price} USDT")

            # 🔥 止损价合理性校验（在挂单前拦截不合理数据）
            print("\n🔍 [止损价合理性校验中...]")
            is_valid, msg = self._validate_stop_losses(signal, current_mark_price)
            if not is_valid:
                print(msg)
                self.send_tg_notification(f"🚨 **挂单被阻断！**\n{msg}")
                return None
            print(msg)

            params_base = {}
            is_hedge_mode = False
            try:
                res = self._safe_api_call(self.exchange.fapiPrivateGetPositionSideDual)
                if res and res.get('dualSidePosition'):
                    params_base['positionSide'] = 'LONG' if side == 'BUY' else 'SHORT'
                    is_hedge_mode = True
                    print(f"💡 检测到账户为 [双向持仓模式]，方向: {params_base['positionSide']}")
                else:
                    print("💡 检测到账户为 [单向持仓模式]")
            except Exception as e:
                print(f"⚠️ 获取持仓模式状态失败，默认单向持仓: {e}")

            params_base['workingType'] = 'MARK_PRICE'
            params_base['leverage'] = signal.leverage

            total_required_margin = 0.0
            print("\n📏 [数量、价格精度与保证金预算校验中...]")
            for idx, (raw_trigger_price, raw_amount) in enumerate(signal.entries, 1):
                formatted_amount = float(self.exchange.amount_to_precision(symbol, raw_amount))
                formatted_price = float(self.exchange.price_to_precision(symbol, raw_trigger_price))
                notional = formatted_amount * formatted_price
                if notional < 5.0:
                    print(f"❌ 第 {idx} 层订单名义价值 ({notional:.2f} USDT) 低于币安限制 5 USDT，程序终止！")
                    return None
                total_required_margin += (notional / signal.leverage)

            balance = self._safe_api_call(self.exchange.fetch_balance)
            usdt_free = float(balance.get('USDT', {}).get('free', 0.0) or balance.get('free', {}).get('USDT', 0.0))

            used_margin = 0.0
            all_states = self.load_all_states()
            symbol_state = all_states.get(symbol, {})
            for b_id, b_data in symbol_state.items():
                if b_data.get('is_active') and b_data.get('entry_orders'):
                    target_amounts = b_data.get('target_amounts', [])
                    leverage = b_data.get('params_base', {}).get('leverage', 100)
                    ticker = self._safe_api_call(self.exchange.fetch_ticker, symbol)
                    current_price = float(ticker.get('last') or ticker.get('close') or 0.0)
                    for amount in target_amounts:
                        if current_price > 0:
                            used_margin += (amount * current_price) / leverage

            print(f"💰 账户可用 USDT 余额: {usdt_free:.2f} USDT")
            print(f"📊 当前批次所需保证金: {total_required_margin:.2f} USDT")
            print(f"📊 已有活跃批次占用保证金: {used_margin:.2f} USDT")
            print(f"📊 总需求: {total_required_margin + used_margin:.2f} USDT")

            if usdt_free < (total_required_margin + used_margin):
                print(
                    f"❌ 【余额不足阻断】账户可用余额 ({usdt_free:.2f} USDT) 不足以支付总需求 ({total_required_margin + used_margin:.2f} USDT)！")
                self.send_tg_notification(
                    f"🚨 **挂单被阻断！**\n"
                    f"❌ 账户可用余额 `{usdt_free:.2f}` USDT\n"
                    f"📊 当前批次需 `{total_required_margin:.2f}` USDT\n"
                    f"📊 已有批次占用 `{used_margin:.2f}` USDT\n"
                    f"📊 总需求 `{total_required_margin + used_margin:.2f}` USDT\n"
                    f"💡 请撤销部分挂单或增加保证金后再试。"
                )
                return None
            else:
                print("✅ 资金校验通过，余额充裕，开始发布条件挂单...\n")

            entry_orders = []
            target_amounts = []
            active_stop_steps = []
            batch_total_amount = 0.0

            order_side = 'buy' if side == 'BUY' else 'sell'

            layer_sl_params = []
            layer_tp_params = []

            for idx, (raw_trigger_price, raw_amount) in enumerate(signal.entries):
                formatted_amount = float(self.exchange.amount_to_precision(symbol, raw_amount))
                formatted_price = float(self.exchange.price_to_precision(symbol, raw_trigger_price))

                raw_sl_price = signal.stop_loss_steps[idx] if idx < len(signal.stop_loss_steps) else 0.0
                formatted_sl_price = float(self.exchange.price_to_precision(symbol, raw_sl_price))

                if side == 'BUY':
                    if formatted_price <= current_mark_price:
                        print(
                            f"⚠️ [跳过第 {idx + 1} 层] 触发买价 ({formatted_price}) <= 当前市价 ({current_mark_price})，挂单会立即触发！")
                        continue
                else:
                    if formatted_price >= current_mark_price:
                        print(
                            f"⚠️ [跳过第 {idx + 1} 层] 触发卖价 ({formatted_price}) >= 当前市价 ({current_mark_price})，挂单会立即触发！")
                        continue

                order_params = params_base.copy()
                order_params['stopPrice'] = formatted_price

                try:
                    order = self._safe_api_call(
                        self.exchange.create_order,
                        symbol=symbol,
                        type='STOP_MARKET',
                        side=order_side,
                        amount=formatted_amount,
                        params=order_params
                    )
                    entry_orders.append(order['id'])
                    target_amounts.append(formatted_amount)
                    active_stop_steps.append(signal.stop_loss_steps[idx])
                    batch_total_amount += formatted_amount

                    print(
                        f"  └─ 第 {idx + 1} 层条件{'买' if side == 'BUY' else '卖'}单已挂出: 触发价 {formatted_price} | 数量 {formatted_amount} (预设止损价: {formatted_sl_price}) (ID: {order['id']})")

                    sl_params = params_base.copy()
                    sl_params['stopPrice'] = formatted_sl_price
                    if not is_hedge_mode:
                        sl_params['reduceOnly'] = True
                    sl_side = 'sell' if side == 'BUY' else 'buy'

                    layer_sl_params.append({
                        'symbol': symbol,
                        'type': 'STOP_MARKET',
                        'side': sl_side,
                        'amount': formatted_amount,
                        'params': sl_params
                    })

                    formatted_tp_price = float(self.exchange.price_to_precision(symbol, signal.take_profit))
                    tp_params = params_base.copy()
                    tp_params['stopPrice'] = formatted_tp_price
                    if not is_hedge_mode:
                        tp_params['reduceOnly'] = True
                    tp_side = 'sell' if side == 'BUY' else 'buy'

                    layer_tp_params.append({
                        'symbol': symbol,
                        'type': 'TAKE_PROFIT_MARKET',
                        'side': tp_side,
                        'amount': formatted_amount,
                        'params': tp_params
                    })

                except ccxt.ExchangeError as e:
                    if "-2021" in str(e):
                        print(
                            f"⚠️ [挂单失败] 第 {idx + 1} 层触发价 {formatted_price} 不满足{'高于' if side == 'BUY' else '低于'}市价条件，已自动跳过。")
                    else:
                        raise e

            if not entry_orders:
                print("❌ 没有成功挂出任何有效开仓条件单（触发价均不符合逻辑），程序安全退出。")
                return None

            batch_total_amount = float(self.exchange.amount_to_precision(symbol, batch_total_amount))

            formatted_tp_price = float(self.exchange.price_to_precision(symbol, signal.take_profit))
            tp_params = params_base.copy()
            tp_params['stopPrice'] = formatted_tp_price
            if not is_hedge_mode:
                tp_params['reduceOnly'] = True
            tp_side = 'sell' if side == 'BUY' else 'buy'

            prepared_tp_params = {
                'symbol': symbol,
                'type': 'TAKE_PROFIT_MARKET',
                'side': tp_side,
                'params': tp_params
            }

            prepared_sl_template = {
                'symbol': symbol,
                'type': 'STOP_MARKET',
                'side': 'sell' if side == 'BUY' else 'buy',
                'params_base': params_base.copy(),
                'is_hedge_mode': is_hedge_mode,
            }

            initial_pending = list(range(len(entry_orders)))

            batch_state_data = {
                'is_active': True,
                'batch_id': batch_id,
                'symbol': symbol,
                'side': side,
                'entry_orders': entry_orders,
                'stop_steps': active_stop_steps,
                'take_profit_price': signal.take_profit,
                'current_sl_id': None,
                'tp_order_id': None,
                'batch_total_amount': batch_total_amount,
                'target_amounts': target_amounts,
                'params_base': params_base,
                'is_hedge_mode': is_hedge_mode,
                'last_filled_count': 0,
                'filled_details': [0.0] * len(entry_orders),
                'total_entry_fee': 0.0,
                'user_modified': False,
                'pending_sl_orders': initial_pending,
                'prepared_tp_params': prepared_tp_params,
                'prepared_sl_template': prepared_sl_template,
                'layer_sl_params': layer_sl_params,
                'layer_tp_params': layer_tp_params,
                # 🔥 新增：记录止损单失败的层，用于熔断
                'sl_fail_count': {},
                'sl_failed_layers': [],
            }
            self.save_batch_state(symbol, batch_id, batch_state_data)

            print(f"\n📊 {len(entry_orders)} 层开仓条件单布置完毕，本批次总配额数量: {batch_total_amount}")
            print("💡 说明：止盈与止损挂单参数已预生成，成交后立即挂出（1秒内）。\n")

            remaining_margin = usdt_free - total_required_margin - used_margin
            margin_usage_ratio = (total_required_margin + used_margin) / usdt_free * 100 if usdt_free > 0 else 0

            if margin_usage_ratio > 80:
                warning_msg = (
                    f"⚠️ **保证金使用率过高提醒**\n"
                    f"🆔 批次 `{batch_id}` 已成功挂单！\n"
                    f"💰 账户余额: `{usdt_free:.2f}` USDT\n"
                    f"📊 总保证金需求: `{total_required_margin + used_margin:.2f}` USDT\n"
                    f"📊 使用率: `{margin_usage_ratio:.1f}%`\n"
                    f"📊 剩余可用: `{remaining_margin:.2f}` USDT\n"
                    f"💡 建议：价格波动可能导致强平，请密切关注！"
                )
                print(f"⚠️ {warning_msg}")
                self.send_tg_notification(warning_msg)

            self._safe_api_call(self.exchange.load_time_difference)
            self.last_time_sync = time.time()

            print(f"🚀 批次 [{batch_id}] 所有条件订单布置完毕，正在后台静默监控独立风控状态...\n")

            monitor_thread = threading.Thread(
                target=self._start_monitoring,
                kwargs={
                    'symbol': symbol,
                    'batch_id': batch_id,
                    'entry_orders': entry_orders,
                    'stop_steps': active_stop_steps,
                    'take_profit_price': signal.take_profit,
                    'current_sl_id': None,
                    'tp_order_id': None,
                    'batch_total_amount': batch_total_amount,
                    'target_amounts': target_amounts,
                    'params_base': params_base,
                    'is_hedge_mode': is_hedge_mode,
                    'side': side,
                    'last_filled_count': 0,
                    'filled_details': [0.0] * len(entry_orders),
                    'total_entry_fee': 0.0,
                    'pending_sl_orders': initial_pending,
                    'prepared_tp_params': prepared_tp_params,
                    'prepared_sl_template': prepared_sl_template,
                    'layer_sl_params': layer_sl_params,
                    'layer_tp_params': layer_tp_params,
                },
                daemon=True
            )
            monitor_thread.start()

            return batch_id

        except Exception as e:
            print(f"\n⚠️ 执行异常: {e}")
            return None

    def _start_monitoring(self, symbol: str, batch_id: str, entry_orders: list, stop_steps: list,
                          take_profit_price: float,
                          current_sl_id: str, tp_order_id: str, batch_total_amount: float, target_amounts: list,
                          params_base: dict, is_hedge_mode: bool, side: str, last_filled_count: int = 0,
                          filled_details: list = None, total_entry_fee: float = 0.0,
                          pending_sl_orders: list = None,
                          prepared_tp_params: dict = None,
                          prepared_sl_template: dict = None,
                          layer_sl_params: list = None,
                          layer_tp_params: list = None,
                          prepared_sl_params: dict = None):

        has_entered_position = False
        filled_layers = [False] * len(entry_orders)
        canceled_layers = [False] * len(entry_orders)

        terminal_orders = set()
        fast_poll_count = 0

        if filled_details is None or len(filled_details) != len(entry_orders):
            filled_details = [0.0] * len(entry_orders)

        if layer_sl_params is None:
            layer_sl_params = []
        if layer_tp_params is None:
            layer_tp_params = []

        for i in range(last_filled_count):
            if i < len(filled_layers):
                filled_layers[i] = True

        if pending_sl_orders is None:
            pending_sl_orders = []

        latest_all = self.load_all_states()
        latest_b_data = latest_all.get(symbol, {}).get(batch_id, {})
        if latest_b_data:
            if 'pending_sl_orders' in latest_b_data:
                pending_sl_orders = latest_b_data.get('pending_sl_orders', [])
            if 'prepared_tp_params' in latest_b_data:
                prepared_tp_params = latest_b_data.get('prepared_tp_params', {})
            if 'prepared_sl_template' in latest_b_data:
                prepared_sl_template = latest_b_data.get('prepared_sl_template', {})
            if 'layer_sl_params' in latest_b_data:
                layer_sl_params = latest_b_data.get('layer_sl_params', [])
            if 'layer_tp_params' in latest_b_data:
                layer_tp_params = latest_b_data.get('layer_tp_params', [])

        # 兼容处理：prepared_sl_params → prepared_sl_template
        if prepared_sl_params and not prepared_sl_template:
            prepared_sl_template = prepared_sl_params
            print(f"  └─ 🔄 兼容处理：prepared_sl_params → prepared_sl_template")

        print(f"👀 批次 [{batch_id}] 启动【批次独立隔离】实时风控监控...")
        if pending_sl_orders:
            print(f"  └─ ⚠️ 有待补挂止损的层: {pending_sl_orders}")

        # 🔥 熔断计数器
        sl_error_count = 0
        MAX_SL_ERRORS = 10
        SL_COOLDOWN_SECONDS = 60

        # 🔥 部分减仓标记，避免重复打印
        last_partial_reduce_log_time = 0

        # 🔥 加载已有的失败计数
        sl_fail_count = latest_b_data.get('sl_fail_count', {}) if latest_b_data else {}
        MAX_SL_FAILS_PER_LAYER = 5

        # ================================================================
        # 🔥 主监控循环
        # ================================================================
        try:
            while True:
                if fast_poll_count > 0:
                    sleep_interval = 3.0
                    fast_poll_count -= 1
                else:
                    sleep_interval = random.uniform(10.0, 12.0)

                time.sleep(sleep_interval)
                self._sync_time_if_needed()

                open_orders_map = {}
                try:
                    open_orders = self._safe_api_call(self.exchange.fetch_open_orders, symbol)
                    open_orders_map = {str(ord['id']): ord for ord in open_orders}
                except Exception as e:
                    print(f"⚠️ 获取未结订单失败，等待下一次轮询: {e}")
                    continue

                batch_filled_count = 0
                batch_filled_amount = 0.0
                total_cost = 0.0
                manual_canceled_detected = False

                # 🔥 收集本次轮询中新成交的层
                newly_filled_layers = []

                for idx, order_id_raw in enumerate(entry_orders):
                    order_id = str(order_id_raw)

                    if filled_layers[idx]:
                        batch_filled_count += 1
                        batch_filled_amount += target_amounts[idx]
                        total_cost += target_amounts[idx] * filled_details[idx]
                        continue

                    if canceled_layers[idx]:
                        continue

                    if order_id not in open_orders_map:
                        if order_id in terminal_orders:
                            continue

                        try:
                            ord_detail = self._safe_api_call(self.exchange.fetch_order, order_id_raw, symbol,
                                                             retries=2, params={'stop': True})
                            ord_status = ord_detail.get('status')

                            if ord_status in ['closed', 'filled']:
                                filled_layers[idx] = True
                                terminal_orders.add(order_id)
                                fast_poll_count = 3

                                batch_filled_count += 1
                                batch_filled_amount += target_amounts[idx]

                                executed_price = float(ord_detail.get('average') or 0.0)
                                if executed_price == 0.0:
                                    info = ord_detail.get('info', {})
                                    cum_quote = float(info.get('cumQuote', 0.0))
                                    executed_qty = float(info.get('executedQty', 0.0))
                                    if cum_quote > 0 and executed_qty > 0:
                                        executed_price = cum_quote / executed_qty
                                    else:
                                        executed_price = float(ord_detail.get('price') or 0.0)

                                trigger_price = float(ord_detail.get('stopPrice') or 0.0)
                                if executed_price == 0.0:
                                    executed_price = trigger_price

                                slippage = executed_price - trigger_price if trigger_price > 0 else 0.0
                                slippage_pct = (slippage / trigger_price * 100) if trigger_price > 0 else 0.0

                                executed_price = float(self.exchange.price_to_precision(symbol, executed_price))
                                filled_details[idx] = executed_price
                                total_cost += target_amounts[idx] * executed_price

                                layer_entry_fee = executed_price * target_amounts[idx] * TAKER_FEE_RATE
                                total_entry_fee += layer_entry_fee

                                # 🔥 收集新成交层
                                newly_filled_layers.append({
                                    'idx': idx,
                                    'executed_price': executed_price,
                                    'amount': target_amounts[idx],
                                    'fee': layer_entry_fee,
                                    'slippage': slippage,
                                    'slippage_pct': slippage_pct,
                                })

                                print(
                                    f"🎯 [批次 {batch_id}] 第 {idx + 1} 层{'买' if side == 'BUY' else '卖'}单成交！实际成交价: {executed_price}")

                                if idx not in pending_sl_orders:
                                    pending_sl_orders.append(idx)
                                    latest_all = self.load_all_states()
                                    latest_b_data = latest_all.get(symbol, {}).get(batch_id, {})
                                    if latest_b_data:
                                        latest_b_data['pending_sl_orders'] = pending_sl_orders
                                        self.save_batch_state(symbol, batch_id, latest_b_data)
                                    print(f"  └─ 📝 第 {idx + 1} 层加入待挂止损队列")

                                # 🔥 尝试预挂止损单（只有当前没有止损单时才挂）
                                if current_sl_id is None:
                                    self._place_prepared_orders_immediately(
                                        symbol, batch_id, idx, batch_filled_amount,
                                        prepared_tp_params, layer_sl_params, layer_tp_params,
                                        is_hedge_mode, params_base, stop_steps
                                    )
                                else:
                                    print(f"  └─ ⚡ 已存在止损单，等待主循环合并更新")

                            # ========== 🔥 修复：正确的 elif 分支 ==========
                            elif ord_status in ['canceled', 'expired', 'rejected']:
                                canceled_layers[idx] = True
                                terminal_orders.add(order_id)

                                # 🔥 检查是否是程序主动撤单
                                latest_all_check = self.load_all_states()
                                latest_b_data_check = latest_all_check.get(symbol, {}).get(batch_id, {})
                                is_programmatic = latest_b_data_check.get('is_programmatic_cancel', False)

                                if is_programmatic:
                                    # 程序主动撤单，不触发手动撤单逻辑
                                    print(f"ℹ️ [程序撤单] 第 {idx + 1} 层开仓条件单已被程序撤销 (ID: {order_id})")
                                    # 不设置 manual_canceled_detected
                                else:
                                    manual_canceled_detected = True
                                    print(f"⚠️ 🛑 [手动撤单提醒] 第 {idx + 1} 层开仓条件单被撤销 (ID: {order_id})")
                                    self.send_tg_notification(
                                        f"⚠️ 🛑 **[撤单提醒]** 批次 `{batch_id}` 第 {idx + 1} 层条件单已被手动撤销/失效。"
                                    )

                        except Exception as e:
                            print(f"⚠️ 补查开仓订单 {order_id_raw} 状态失败 ({e})，将在下一轮重试...")

                # 🔥 如果有新成交的层，发送合并通知
                if newly_filled_layers:
                    notification_lines = [
                        f"🎯 **{'买' if side == 'BUY' else '卖'}单成交提醒**",
                        f"🆔 **批次号**：`{batch_id}`",
                        f"🪙 **标的**：`{symbol}`",
                        f"📊 **本次成交层数**：`{len(newly_filled_layers)}` 层\n"
                    ]

                    total_layer_fee = 0.0
                    for layer in newly_filled_layers:
                        idx = layer['idx']
                        executed_price = layer['executed_price']
                        amount = layer['amount']
                        fee = layer['fee']
                        slippage = layer['slippage']
                        slippage_pct = layer['slippage_pct']
                        total_layer_fee += fee

                        slippage_str = f"+{slippage:.2f}" if slippage >= 0 else f"{slippage:.2f}"
                        slippage_pct_str = f"+{slippage_pct:.3f}%" if slippage_pct >= 0 else f"{slippage_pct:.3f}%"

                        notification_lines.append(
                            f"📌 **第 {idx + 1} 层**：`{executed_price}` USDT | 数量 `{amount}` | 滑点 `{slippage_str}` (`{slippage_pct_str}`)"
                        )

                    notification_lines.append(f"\n💸 **预估总手续费**：`{total_layer_fee:.4f}` USDT")

                    combined_msg = "\n".join(notification_lines)

                    # 🔥 硬编码按钮（不依赖外部函数）
                    keyboard = [
                        [
                            InlineKeyboardButton("🔒 保本", callback_data=f"be_{batch_id}"),
                            InlineKeyboardButton("💰 平仓", callback_data=f"close_{batch_id}"),
                            InlineKeyboardButton("🗑️ 撤单", callback_data=f"cancel_{batch_id}"),
                        ]
                    ]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    self.send_tg_notification(combined_msg, reply_markup=reply_markup)

                if manual_canceled_detected and batch_filled_count == 0:
                    print(f"🚨 [批次终止] 本批次未建仓且开仓挂单被撤销，正在退出...")
                    self._cancel_remaining_entries(symbol, entry_orders, filled_layers)
                    self.clear_batch_state(symbol, batch_id)
                    self.send_tg_notification(f"🧹 **[批次终止]** 批次 `{batch_id}` 在建仓前挂单已全撤，后台监控退出。")
                    break

                if batch_filled_amount > 0:
                    batch_filled_amount = float(self.exchange.amount_to_precision(symbol, batch_filled_amount))
                    has_entered_position = True

                current_actual_position = self._get_current_position_amt(symbol, is_hedge_mode, side=side)

                # ==================== 持仓归零检测 ====================
                if current_actual_position is not None and has_entered_position and batch_filled_amount > 0:
                    if current_actual_position == 0:
                        latest_all = self.load_all_states()
                        latest_b_data = latest_all.get(symbol, {}).get(batch_id, {})

                        # 🔥 如果已被限价平仓监控处理，跳过重复结算
                        if latest_b_data.get('settled_by_limit_close', False):
                            print(f"ℹ️ [限价平仓已处理] 批次 [{batch_id}] 跳过重复结算")
                            self._cancel_remaining_entries(symbol, entry_orders, filled_layers)
                            self.clear_batch_state(symbol, batch_id)
                            break

                        # 🔥 如果是程序平仓，跳过结算
                        if latest_b_data.get('pending_close', False) or latest_b_data.get('is_programmatic_cancel',
                                                                                          False):
                            print(f"ℹ️ [程序平仓] 批次 [{batch_id}] 由程序触发平仓，跳过结算")
                            self._cancel_remaining_entries(symbol, entry_orders, filled_layers)
                            self.clear_batch_state(symbol, batch_id)
                            break

                        print(f"🛑 [持仓归零检测] 批次 [{batch_id}] 实际持仓已归零，正在安全退出监控...")

                        # 🔥 计算实际盈亏
                        if batch_filled_amount > 0:
                            # 计算持仓均价（含手续费）
                            filled_costs = sum(
                                [target_amounts[i] * filled_details[i] for i in range(batch_filled_count)])
                            total_cost_with_fee = filled_costs + total_entry_fee
                            avg_price_with_fee = total_cost_with_fee / batch_filled_amount if batch_filled_amount > 0 else 0

                            # 获取当前市价（平仓价格）
                            try:
                                ticker = self._safe_api_call(self.exchange.fetch_ticker, symbol)
                                exit_price = float(ticker.get('last') or ticker.get('close') or 0.0)
                            except Exception:
                                exit_price = avg_price_with_fee

                            # 计算盈亏
                            if side == 'BUY':
                                gross_pnl = (exit_price - avg_price_with_fee) * batch_filled_amount
                            else:
                                gross_pnl = (avg_price_with_fee - exit_price) * batch_filled_amount

                            # 估算平仓手续费（市价平仓用 TAKER_FEE_RATE）
                            exit_fee = exit_price * batch_filled_amount * TAKER_FEE_RATE
                            total_fees = total_entry_fee + exit_fee
                            net_pnl = gross_pnl - total_fees

                            capital_base = avg_price_with_fee * batch_filled_amount if batch_filled_amount > 0 else 1
                            net_pnl_pct = (net_pnl / capital_base) * 100 if capital_base > 0 else 0.0

                            # 构建盈亏报告
                            pnl_emoji = "🟢" if net_pnl >= 0 else "🔴"
                            pnl_msg = (
                                f"📊 **[平仓结算]**\n\n"
                                f"🆔 **批次号**：`{batch_id}`\n"
                                f"🪙 **标的**：`{symbol}`\n"
                                f"📊 **方向**：`{side}`\n"
                                f"📊 **平仓模式**：未知\n"
                                f"📊 **已成交层数**：`{batch_filled_count}/{len(entry_orders)}`\n"
                                f"📈 **持仓均价**：`{avg_price_with_fee:.2f}` USDT\n"
                                f"💵 **平仓价格**：`{exit_price:.2f}` USDT\n"
                                f"🔢 **平仓数量**：`{batch_filled_amount}`\n"
                                f"📊 **名义盈亏**：`{gross_pnl:+.2f}` USDT\n"
                                f"💸 **总手续费**：`{total_fees:.4f}` USDT\n"
                                f"{pnl_emoji} **最终净盈亏**：`{net_pnl:+.2f}` USDT (`{net_pnl_pct:+.2f}%`)"
                            )

                            print(f"\n{pnl_msg}")
                            self.send_tg_notification(pnl_msg)

                        self._cancel_remaining_entries(symbol, entry_orders, filled_layers)
                        if tp_order_id:
                            try:
                                self._safe_api_call(self.exchange.cancel_order, tp_order_id, symbol,
                                                    params={'stop': True})
                            except Exception:
                                pass
                        if current_sl_id:
                            try:
                                self._safe_api_call(self.exchange.cancel_order, current_sl_id, symbol,
                                                    params={'stop': True})
                            except Exception:
                                pass
                        self.clear_batch_state(symbol, batch_id)
                        break

                # ==================== 部分减仓检测（自动更新止盈止损单） ====================
                if current_actual_position is not None and has_entered_position and current_actual_position < batch_filled_amount:
                    # 避免频繁打印
                    current_time = time.time()
                    if current_time - last_partial_reduce_log_time > 5:
                        print(
                            f"⚠️ [部分减仓检测] 批次 [{batch_id}] 实际持仓 {current_actual_position} < 程序记录 {batch_filled_amount}")
                        last_partial_reduce_log_time = current_time

                    # 🔥 更新实际持仓数量
                    old_amount = batch_filled_amount
                    new_amount = float(self.exchange.amount_to_precision(symbol, current_actual_position))

                    # 只有当变化超过 0.5% 时才触发更新，避免频繁操作
                    if new_amount > 0 and abs(new_amount - old_amount) / old_amount > 0.005:
                        print(f"  └─ 🔄 更新止盈止损单数量: {old_amount:.4f} → {new_amount:.4f}")

                        # 保存旧订单ID用于日志
                        old_sl_id = current_sl_id
                        old_tp_id = tp_order_id

                        # 撤销旧止损单
                        if current_sl_id:
                            try:
                                self._safe_api_call(self.exchange.cancel_order, current_sl_id, symbol,
                                                    params={'stop': True})
                                current_sl_id = None
                                print(f"  └─ 已撤销旧止损单: {old_sl_id}")
                            except Exception as e:
                                if "Unknown order" in str(e) or "-2011" in str(e):
                                    print(f"  └─ 旧止损单 {old_sl_id} 已不存在")
                                    current_sl_id = None
                                else:
                                    print(f"  └─ ⚠️ 撤销旧止损单失败: {e}")

                        # 撤销旧止盈单
                        if tp_order_id:
                            try:
                                self._safe_api_call(self.exchange.cancel_order, tp_order_id, symbol,
                                                    params={'stop': True})
                                tp_order_id = None
                                print(f"  └─ 已撤销旧止盈单: {old_tp_id}")
                            except Exception as e:
                                if "Unknown order" in str(e) or "-2011" in str(e):
                                    print(f"  └─ 旧止盈单 {old_tp_id} 已不存在")
                                    tp_order_id = None
                                else:
                                    print(f"  └─ ⚠️ 撤销旧止盈单失败: {e}")

                        # 🔥 更新 batch_filled_amount 为新值
                        batch_filled_amount = new_amount

                        # 挂新的止损单
                        if batch_filled_amount > 0 and current_sl_id is None:
                            sl_idx = batch_filled_count - 1
                            if sl_idx < 0:
                                sl_idx = 0
                            raw_sl_price = stop_steps[sl_idx] if sl_idx < len(stop_steps) else stop_steps[-1]
                            formatted_sl_price = float(self.exchange.price_to_precision(symbol, raw_sl_price))
                            sl_params = params_base.copy()
                            sl_params['stopPrice'] = formatted_sl_price
                            if not is_hedge_mode:
                                sl_params['reduceOnly'] = True

                            try:
                                new_sl_order = self._safe_api_call(
                                    self.exchange.create_order,
                                    symbol=symbol,
                                    type='STOP_MARKET',
                                    side='sell' if side == 'BUY' else 'buy',
                                    amount=batch_filled_amount,
                                    params=sl_params
                                )
                                current_sl_id = new_sl_order['id']
                                print(f"  └─ ✅ 止损单已更新: {formatted_sl_price} (数量: {batch_filled_amount})")
                            except Exception as e:
                                print(f"  └─ ❌ 更新止损单失败: {e}")

                        # 挂新的止盈单
                        if batch_filled_amount > 0 and tp_order_id is None:
                            formatted_tp_price = float(self.exchange.price_to_precision(symbol, take_profit_price))
                            tp_params = params_base.copy()
                            tp_params['stopPrice'] = formatted_tp_price
                            if not is_hedge_mode:
                                tp_params['reduceOnly'] = True

                            try:
                                new_tp_order = self._safe_api_call(
                                    self.exchange.create_order,
                                    symbol=symbol,
                                    type='TAKE_PROFIT_MARKET',
                                    side='sell' if side == 'BUY' else 'buy',
                                    amount=batch_filled_amount,
                                    params=tp_params
                                )
                                tp_order_id = new_tp_order['id']
                                print(f"  └─ ✅ 止盈单已更新: {formatted_tp_price} (数量: {batch_filled_amount})")
                            except Exception as e:
                                print(f"  └─ ❌ 更新止盈单失败: {e}")

                        # 🔥 清理无效的 pending_sl_orders（超过实际成交层数的）
                        pending_sl_orders = [idx for idx in pending_sl_orders if idx < batch_filled_count]
                        print(f"  └─ 📝 清理待挂列表: {pending_sl_orders}")

                        # 保存状态
                        batch_state_data = {
                            'is_active': True,
                            'batch_id': batch_id,
                            'symbol': symbol,
                            'side': side,
                            'entry_orders': entry_orders,
                            'stop_steps': stop_steps,
                            'take_profit_price': take_profit_price,
                            'current_sl_id': current_sl_id,
                            'tp_order_id': tp_order_id,
                            'batch_total_amount': batch_total_amount,
                            'target_amounts': target_amounts,
                            'params_base': params_base,
                            'is_hedge_mode': is_hedge_mode,
                            'last_filled_count': last_filled_count,
                            'filled_details': filled_details,
                            'total_entry_fee': total_entry_fee,
                            'user_modified': False,
                            'pending_sl_orders': pending_sl_orders,
                            'prepared_tp_params': prepared_tp_params,
                            'prepared_sl_template': prepared_sl_template,
                            'layer_sl_params': layer_sl_params,
                            'layer_tp_params': layer_tp_params,
                            'sl_fail_count': sl_fail_count,
                            'sl_failed_layers': latest_b_data.get('sl_failed_layers', []) if latest_b_data else [],
                        }
                        self.save_batch_state(symbol, batch_id, batch_state_data)
                        print(f"  └─ ✅ 状态已保存")

                # 更新 VWAP（如果持仓有变化）
                if current_actual_position is not None and has_entered_position and current_actual_position < batch_filled_amount:
                    current_vwap = self._get_current_vwap_from_position(symbol)
                    if current_vwap is not None:
                        batch_entry_vwap = current_vwap
                        print(f"  └─ 更新 VWAP: {batch_entry_vwap:.2f}")
                    if current_actual_position == 0:
                        batch_filled_amount = 0.0
                    else:
                        batch_filled_amount = float(self.exchange.amount_to_precision(symbol, current_actual_position))

                batch_entry_vwap = (total_cost / batch_filled_amount) if batch_filled_amount > 0 else 0.0

                latest_all = self.load_all_states()
                latest_b_data = latest_all.get(symbol, {}).get(batch_id, {})

                if latest_b_data and 'pending_sl_orders' in latest_b_data:
                    pending_sl_orders = latest_b_data.get('pending_sl_orders', [])

                if latest_b_data:
                    stop_steps = latest_b_data.get('stop_steps', stop_steps)
                    take_profit_price = latest_b_data.get('take_profit_price', take_profit_price)
                    current_sl_id = latest_b_data.get('current_sl_id', current_sl_id)
                    tp_order_id = latest_b_data.get('tp_order_id', tp_order_id)
                    user_modified = latest_b_data.get('user_modified', False)
                    # 加载失败计数
                    sl_fail_count = latest_b_data.get('sl_fail_count', {})
                    sl_failed_layers = latest_b_data.get('sl_failed_layers', [])
                else:
                    user_modified = False
                    sl_failed_layers = []

                sl_triggered = False
                sl_detail = None
                need_recover_sl = False

                if current_sl_id and (str(current_sl_id) not in open_orders_map) and has_entered_position:
                    sl_id_str = str(current_sl_id)
                    if sl_id_str not in terminal_orders:
                        try:
                            sl_detail = self._safe_api_call(self.exchange.fetch_order, current_sl_id, symbol,
                                                            retries=2, params={'stop': True})
                            sl_status = sl_detail.get('status')
                            if sl_status in ['closed', 'filled']:
                                sl_triggered = True
                                terminal_orders.add(sl_id_str)
                            elif sl_status in ['canceled', 'expired']:
                                terminal_orders.add(sl_id_str)
                                # 🔥 检查是否是程序主动撤单（平仓时撤销）
                                latest_all_check = self.load_all_states()
                                latest_b_data_check = latest_all_check.get(symbol, {}).get(batch_id, {})
                                is_programmatic = latest_b_data_check.get('is_programmatic_cancel', False)
                                if is_programmatic:
                                    print(f"ℹ️ [程序撤单] 批次 {batch_id} 止损单已被程序撤销 (ID: {current_sl_id})")
                                    current_sl_id = None
                                elif user_modified:
                                    print(f"ℹ️ [用户主动修改] 批次 {batch_id} 止损单已被用户撤销，不再自动补挂")
                                    current_sl_id = None
                                else:
                                    print(f"⚠️ ⚠️ [风控异常] 止损单已在外部撤销，准备按策略自动补挂...")
                                    current_sl_id = None
                                    need_recover_sl = True
                        except Exception as e:
                            print(f"⚠️ 无法拉取止损单 {current_sl_id} 状态 ({e})，下轮重试...")

                if sl_triggered and sl_detail:
                    sl_exit_price = float(sl_detail.get('average') or 0.0)
                    if sl_exit_price == 0.0:
                        info = sl_detail.get('info', {})
                        cum_quote = float(info.get('cumQuote', 0.0))
                        executed_qty = float(info.get('executedQty', 0.0))
                        if cum_quote > 0 and executed_qty > 0:
                            sl_exit_price = cum_quote / executed_qty
                        else:
                            sl_exit_price = float(sl_detail.get('stopPrice') or sl_detail.get('price') or 0.0)

                    sl_exit_price = float(self.exchange.price_to_precision(symbol, sl_exit_price))

                    if side == 'BUY':
                        gross_pnl = (sl_exit_price - batch_entry_vwap) * batch_filled_amount
                    else:
                        gross_pnl = (batch_entry_vwap - sl_exit_price) * batch_filled_amount

                    exit_fee = sl_exit_price * batch_filled_amount * TAKER_FEE_RATE
                    total_fees = total_entry_fee + exit_fee
                    net_pnl = gross_pnl - total_fees

                    capital_base = batch_entry_vwap * batch_filled_amount
                    net_pnl_pct = (net_pnl / capital_base) * 100 if capital_base > 0 else 0.0

                    sl_msg = (
                        f"🚨 **[止损平仓结算提醒]**\n\n"
                        f"🆔 **批次号**：`{batch_id}`\n"
                        f"🪙 **标的**：`{symbol}`\n"
                        f"📊 **方向**：`{side}`\n"
                        f"📊 **平仓模式**：止损单 (Taker {TAKER_FEE_RATE * 100:.2f}%)\n"
                        f"持仓均价：`{batch_entry_vwap:.2f}` USDT\n"
                        f"平仓均价：`{sl_exit_price:.2f}` USDT\n"
                        f"平仓数量：`{batch_filled_amount}`\n"
                        f"名义盈亏：`{gross_pnl:+.2f}` USDT\n"
                        f"扣除手续费：`{total_fees:.4f}` USDT\n"
                        f"💰 **最终净盈亏**：`{net_pnl:+.2f}` USDT (`{net_pnl_pct:+.2f}%`)"
                    )
                    print(f"\n🚨 [风控触发] 批次 [{batch_id}] 专属止损单已触发成交！净盈亏: {net_pnl:+.2f} USDT")
                    self.send_tg_notification(sl_msg)

                    self._cancel_remaining_entries(symbol, entry_orders, filled_layers)
                    if tp_order_id:
                        try:
                            self._safe_api_call(self.exchange.cancel_order, tp_order_id, symbol, params={'stop': True})
                        except Exception:
                            pass

                    self.clear_batch_state(symbol, batch_id)
                    break

                tp_triggered = False
                tp_detail = None
                need_recover_tp = False

                if tp_order_id and (str(tp_order_id) not in open_orders_map) and has_entered_position:
                    tp_id_str = str(tp_order_id)
                    if tp_id_str not in terminal_orders:
                        try:
                            tp_detail = self._safe_api_call(self.exchange.fetch_order, tp_order_id, symbol,
                                                            retries=2, params={'stop': True})
                            tp_status = tp_detail.get('status')
                            if tp_status in ['closed', 'filled']:
                                tp_triggered = True
                                terminal_orders.add(tp_id_str)
                            elif tp_status in ['canceled', 'expired']:
                                terminal_orders.add(tp_id_str)
                                # 🔥 检查是否是程序主动撤单（平仓时撤销）
                                latest_all_check = self.load_all_states()
                                latest_b_data_check = latest_all_check.get(symbol, {}).get(batch_id, {})
                                is_programmatic = latest_b_data_check.get('is_programmatic_cancel', False)
                                if is_programmatic:
                                    print(f"ℹ️ [程序撤单] 批次 {batch_id} 止盈单已被程序撤销 (ID: {tp_order_id})")
                                    tp_order_id = None
                                elif user_modified:
                                    print(f"ℹ️ [用户主动修改] 批次 {batch_id} 止盈单已被用户撤销，不再自动补挂")
                                    tp_order_id = None
                                else:
                                    print(f"⚠️ ⚠️ [风控异常] 止盈单已在外部撤销，准备按策略自动补挂...")
                                    tp_order_id = None
                                    need_recover_tp = True
                        except Exception as e:
                            print(f"⚠️ 无法拉取止盈单 {tp_order_id} 状态 ({e})，下轮重试...")

                if tp_triggered and tp_detail:
                    tp_exit_price = float(tp_detail.get('average') or 0.0)
                    if tp_exit_price == 0.0:
                        info = tp_detail.get('info', {})
                        cum_quote = float(info.get('cumQuote', 0.0))
                        executed_qty = float(info.get('executedQty', 0.0))
                        if cum_quote > 0 and executed_qty > 0:
                            tp_exit_price = cum_quote / executed_qty
                        else:
                            tp_exit_price = float(tp_detail.get('stopPrice') or tp_detail.get('price') or 0.0)

                    tp_exit_price = float(self.exchange.price_to_precision(symbol, tp_exit_price))

                    if side == 'BUY':
                        gross_pnl = (tp_exit_price - batch_entry_vwap) * batch_filled_amount
                    else:
                        gross_pnl = (batch_entry_vwap - tp_exit_price) * batch_filled_amount

                    exit_fee = tp_exit_price * batch_filled_amount * MAKER_FEE_RATE
                    total_fees = total_entry_fee + exit_fee
                    net_pnl = gross_pnl - total_fees

                    capital_base = batch_entry_vwap * batch_filled_amount
                    net_pnl_pct = (net_pnl / capital_base) * 100 if capital_base > 0 else 0.0

                    tp_msg = (
                        f"🎉 **[止盈平仓结算提醒]**\n\n"
                        f"🆔 **批次号**：`{batch_id}`\n"
                        f"🪙 **标的**：`{symbol}`\n"
                        f"📊 **方向**：`{side}`\n"
                        f"📊 **平仓模式**：止盈单 (Maker {MAKER_FEE_RATE * 100:.2f}%)\n"
                        f"持仓均价：`{batch_entry_vwap:.2f}` USDT\n"
                        f"平仓均价：`{tp_exit_price:.2f}` USDT\n"
                        f"平仓数量：`{batch_filled_amount}`\n"
                        f"名义盈亏：`{gross_pnl:+.2f}` USDT\n"
                        f"扣除手续费：`{total_fees:.4f}` USDT\n"
                        f"💰 **最终净盈亏**：`{net_pnl:+.2f}` USDT (`{net_pnl_pct:+.2f}%`)"
                    )
                    print(f"\n🎉 [止盈触发] 批次 [{batch_id}] 专属止盈单已触发成交！净盈亏: {net_pnl:+.2f} USDT")
                    self.send_tg_notification(tp_msg)

                    self._cancel_remaining_entries(symbol, entry_orders, filled_layers)
                    if current_sl_id:
                        try:
                            self._safe_api_call(self.exchange.cancel_order, current_sl_id, symbol,
                                                params={'stop': True})
                        except Exception:
                            pass

                    self.clear_batch_state(symbol, batch_id)
                    break

                # ==================== 处理待补挂止损 ====================
                if pending_sl_orders and has_entered_position and batch_filled_amount > 0:
                    all_processed = True
                    for layer_idx in pending_sl_orders:
                        if layer_idx < len(filled_layers) and filled_layers[layer_idx]:
                            all_processed = False
                            break

                    if not all_processed:
                        print(f"\n⚡ [批次 {batch_id}] 处理待补挂止损，等待主循环更新...")
                        need_recover_sl = True

                need_update_sl = (batch_filled_count > last_filled_count) or need_recover_sl
                need_update_tp = (batch_filled_count > last_filled_count) or need_recover_tp

                if need_update_sl and pending_sl_orders and batch_filled_amount > 0:
                    print(f"  └─ 🔧 补挂待处理止损层: {pending_sl_orders}")

                if batch_filled_count > last_filled_count and user_modified:
                    print(f"ℹ️ [新层成交] 批次 {batch_id} 新层成交，重置用户修改标志")
                    latest_b_data['user_modified'] = False
                    self.save_batch_state(symbol, batch_id, latest_b_data)
                    user_modified = False

                if user_modified and not (batch_filled_count > last_filled_count):
                    if need_recover_sl or need_recover_tp:
                        print(f"ℹ️ [用户主动修改后补挂] 批次 {batch_id} 使用用户设置的价格补挂")
                    else:
                        pass

                # ==================== 风控更新：止损 + 止盈 ====================
                if (need_update_sl or need_update_tp) and batch_filled_amount > 0:
                    raw_new_sl_price = stop_steps[batch_filled_count - 1] if batch_filled_count - 1 < len(
                        stop_steps) else \
                        stop_steps[-1]
                    formatted_new_sl_price = float(self.exchange.price_to_precision(symbol, raw_new_sl_price))
                    formatted_tp_price = float(self.exchange.price_to_precision(symbol, take_profit_price))

                    print(f"\n⚡ [批次 {batch_id}] 同步维护独立风控...")

                    sl_side = 'sell' if side == 'BUY' else 'buy'
                    tp_side = 'sell' if side == 'BUY' else 'buy'

                    sl_success = False

                    # ========== 止损更新（带降级保护） ==========
                    if need_update_sl:
                        old_sl_id = current_sl_id
                        old_sl_price = None
                        old_sl_amount = None

                        if old_sl_id:
                            try:
                                old_order = self._safe_api_call(self.exchange.fetch_order, old_sl_id, symbol,
                                                                retries=2, params={'stop': True})
                                old_sl_price = float(old_order.get('stopPrice', 0.0))
                                old_sl_amount = float(old_order.get('amount', 0.0))
                            except Exception:
                                pass

                        if old_sl_id:
                            try:
                                self._safe_api_call(self.exchange.cancel_order, old_sl_id, symbol,
                                                    params={'stop': True})
                                print(f"  └─ 已撤销旧止损单: {old_sl_id}")
                                old_sl_id = None
                            except Exception as e:
                                if "Unknown order" in str(e) or "-2011" in str(e):
                                    print(f"  └─ 旧止损单 {old_sl_id} 已不存在，跳过")
                                    old_sl_id = None
                                else:
                                    print(f"  └─ ⚠️ 撤销旧止损单失败: {e}")
                                    sl_error_count = 0
                                    continue

                        if old_sl_id is None:
                            sl_params = params_base.copy()
                            sl_params['stopPrice'] = formatted_new_sl_price
                            if not is_hedge_mode:
                                sl_params['reduceOnly'] = True

                            # 🔥 检查该层是否已被标记为"失败层"（熔断）
                            layer_failed = False
                            if str(batch_filled_count - 1) in sl_fail_count:
                                if sl_fail_count[str(batch_filled_count - 1)] >= MAX_SL_FAILS_PER_LAYER:
                                    layer_failed = True
                                    print(
                                        f"  └─ 🔥 [熔断保护] 第 {batch_filled_count} 层止损单已连续失败 {MAX_SL_FAILS_PER_LAYER} 次，跳过重试")

                            if not layer_failed:
                                try:
                                    new_sl_order = self._safe_api_call(
                                        self.exchange.create_order,
                                        symbol=symbol,
                                        type='STOP_MARKET',
                                        side=sl_side,
                                        amount=batch_filled_amount,
                                        params=sl_params
                                    )
                                    current_sl_id = new_sl_order['id']
                                    sl_success = True
                                    print(f"  └─ ✅ 止损单已挂出: {formatted_new_sl_price} (ID: {current_sl_id})")

                                    # 🔥 安全移除已处理的 pending_sl_orders
                                    if pending_sl_orders:
                                        removed = []
                                        for idx in list(pending_sl_orders):
                                            if idx < batch_filled_count:
                                                pending_sl_orders.remove(idx)
                                                removed.append(idx)
                                        if removed:
                                            print(f"  └─ 📝 已补挂层 {removed}，从待挂列表中移除")
                                        latest_all = self.load_all_states()
                                        latest_b_data = latest_all.get(symbol, {}).get(batch_id, {})
                                        if latest_b_data:
                                            latest_b_data['pending_sl_orders'] = pending_sl_orders
                                            self.save_batch_state(symbol, batch_id, latest_b_data)

                                    sl_error_count = 0
                                    # 重置该层的失败计数
                                    layer_key = str(batch_filled_count - 1)
                                    if layer_key in sl_fail_count:
                                        sl_fail_count[layer_key] = 0

                                except Exception as e:
                                    print(f"  └─ ❌ 挂出止损单失败: {e}")
                                    current_sl_id = None
                                    sl_success = False

                                    # 🔥 记录失败次数
                                    layer_key = str(batch_filled_count - 1)
                                    sl_fail_count[layer_key] = sl_fail_count.get(layer_key, 0) + 1
                                    print(
                                        f"  └─ ⚠️ 第 {batch_filled_count} 层止损单失败次数: {sl_fail_count[layer_key]}/{MAX_SL_FAILS_PER_LAYER}")

                                    # 如果达到熔断阈值，发送告警
                                    if sl_fail_count[layer_key] >= MAX_SL_FAILS_PER_LAYER:
                                        self.send_tg_notification(
                                            f"🚨 **止损单熔断触发！**\n"
                                            f"🆔 批次：`{batch_id}`\n"
                                            f"📊 第 {batch_filled_count} 层\n"
                                            f"⚠️ 止损单连续失败 {MAX_SL_FAILS_PER_LAYER} 次，已停止自动重试\n"
                                            f"💡 请立即手动检查持仓并设置止损！"
                                        )

                                    if old_sl_price and old_sl_amount and old_sl_amount > 0:
                                        try:
                                            print(f"  └─ 🔄 降级保护：尝试用旧止损价 {old_sl_price} 恢复...")
                                            recovery_params = params_base.copy()
                                            recovery_params['stopPrice'] = old_sl_price
                                            if not is_hedge_mode:
                                                recovery_params['reduceOnly'] = True

                                            recovery_order = self._safe_api_call(
                                                self.exchange.create_order,
                                                symbol=symbol,
                                                type='STOP_MARKET',
                                                side=sl_side,
                                                amount=old_sl_amount,
                                                params=recovery_params
                                            )
                                            current_sl_id = recovery_order['id']
                                            sl_success = True
                                            print(
                                                f"  └─ 🔄 降级保护成功：已用旧止损价恢复: {old_sl_price} (ID: {current_sl_id})")
                                            self.send_tg_notification(
                                                f"⚠️ **降级保护触发**\n"
                                                f"🆔 批次 `{batch_id}` 新止损单挂单失败，已自动恢复为旧止损价\n"
                                                f"🛡️ 止损价：`{old_sl_price}`\n"
                                                f"🔢 数量：`{old_sl_amount}`"
                                            )
                                            sl_error_count = 0
                                        except Exception as recovery_e:
                                            print(f"  └─ ❌ 降级保护失败: {recovery_e}")
                                            self.send_tg_notification(
                                                f"🚨 **紧急：批次 `{batch_id}` 止损保护丢失！**\n"
                                                f"旧止损单已撤销，新止损单挂单失败，且恢复失败！\n"
                                                f"请立即手动检查持仓并设置止损！"
                                            )
                                            latest_all = self.load_all_states()
                                            latest_b_data = latest_all.get(symbol, {}).get(batch_id, {})
                                            if latest_b_data:
                                                latest_b_data['sl_error'] = True
                                                latest_b_data['sl_error_time'] = time.time()
                                                self.save_batch_state(symbol, batch_id, latest_b_data)

                                            sl_error_count += 1
                                            if sl_error_count >= MAX_SL_ERRORS:
                                                print(
                                                    f"🚨 [熔断触发] 批次 {batch_id} 止损更新连续失败 {sl_error_count} 次，暂停 60 秒")
                                                time.sleep(SL_COOLDOWN_SECONDS)
                                                sl_error_count = 0
                                    else:
                                        print(f"  └─ ⚠️ 无旧止损信息，无法降级恢复")
                                        sl_error_count += 1
                                        if sl_error_count >= MAX_SL_ERRORS:
                                            print(
                                                f"🚨 [熔断触发] 批次 {batch_id} 止损更新连续失败 {sl_error_count} 次，暂停 60 秒")
                                            time.sleep(SL_COOLDOWN_SECONDS)
                                            sl_error_count = 0
                            else:
                                # 该层已被熔断，从待挂列表中移除
                                if pending_sl_orders and batch_filled_count - 1 in pending_sl_orders:
                                    pending_sl_orders.remove(batch_filled_count - 1)
                                    latest_all = self.load_all_states()
                                    latest_b_data = latest_all.get(symbol, {}).get(batch_id, {})
                                    if latest_b_data:
                                        latest_b_data['pending_sl_orders'] = pending_sl_orders
                                        latest_b_data['sl_failed_layers'] = latest_b_data.get('sl_failed_layers', [])
                                        if batch_filled_count - 1 not in latest_b_data['sl_failed_layers']:
                                            latest_b_data['sl_failed_layers'].append(batch_filled_count - 1)
                                        self.save_batch_state(symbol, batch_id, latest_b_data)

                    # ========== 止盈更新 ==========
                    if need_update_tp:
                        if tp_order_id:
                            try:
                                self._safe_api_call(self.exchange.cancel_order, tp_order_id, symbol,
                                                    params={'stop': True})
                            except Exception:
                                pass

                        tp_params = params_base.copy()
                        tp_params['stopPrice'] = formatted_tp_price
                        if not is_hedge_mode:
                            tp_params['reduceOnly'] = True

                        try:
                            new_tp_order = self._safe_api_call(
                                self.exchange.create_order,
                                symbol=symbol,
                                type='TAKE_PROFIT_MARKET',
                                side=tp_side,
                                amount=batch_filled_amount,
                                params=tp_params
                            )
                            tp_order_id = new_tp_order['id']
                            print(f"  └─ ✅ 止盈单已挂出: {formatted_tp_price} (ID: {tp_order_id})")
                        except Exception as e:
                            print(f"  └─ ❌ 挂出止盈单失败: {e}")
                            tp_order_id = None

                    if sl_success or tp_order_id:
                        risk_update_msg = (
                            f"⚡ **[风控阶梯同步更新/重新挂单]**\n"
                            f"🆔 **批次号**：`{batch_id}`\n"
                            f"🪙 **标的**：`{symbol}`\n"
                            f"📊 **方向**：`{side}`\n"
                            f"📊 **当前已成交层数**：`{batch_filled_count}/{len(entry_orders)}`\n"
                            f"📈 **当前持仓均价**：`{batch_entry_vwap:.2f}` USDT\n"
                            f"🛡️ **最新阶梯止损价**：`{formatted_new_sl_price}` USDT\n"
                            f"🎯 **目标止盈价**：`{formatted_tp_price}` USDT\n"
                            f"🔢 **风控覆盖数量**：`{batch_filled_amount}`"
                        )

                        # 🔥 硬编码按钮（不依赖外部函数）
                        keyboard = [
                            [
                                InlineKeyboardButton("🔒 保本", callback_data=f"be_{batch_id}"),
                                InlineKeyboardButton("💰 平仓", callback_data=f"close_{batch_id}"),
                                InlineKeyboardButton("🗑️ 撤单", callback_data=f"cancel_{batch_id}"),
                            ]
                        ]
                        reply_markup = InlineKeyboardMarkup(keyboard)
                        self.send_tg_notification(risk_update_msg, reply_markup=reply_markup)

                    last_filled_count = batch_filled_count

                    batch_state_data = {
                        'is_active': True,
                        'batch_id': batch_id,
                        'symbol': symbol,
                        'side': side,
                        'entry_orders': entry_orders,
                        'stop_steps': stop_steps,
                        'take_profit_price': take_profit_price,
                        'current_sl_id': current_sl_id,
                        'tp_order_id': tp_order_id,
                        'batch_total_amount': batch_total_amount,
                        'target_amounts': target_amounts,
                        'params_base': params_base,
                        'is_hedge_mode': is_hedge_mode,
                        'last_filled_count': last_filled_count,
                        'filled_details': filled_details,
                        'total_entry_fee': total_entry_fee,
                        'user_modified': False,
                        'pending_sl_orders': pending_sl_orders,
                        'prepared_tp_params': prepared_tp_params,
                        'prepared_sl_template': prepared_sl_template,
                        'layer_sl_params': layer_sl_params,
                        'layer_tp_params': layer_tp_params,
                        'sl_fail_count': sl_fail_count,
                        'sl_failed_layers': sl_failed_layers,
                    }
                    self.save_batch_state(symbol, batch_id, batch_state_data)

                elif pending_sl_orders and has_entered_position and batch_filled_amount > 0:
                    still_pending = []
                    for idx in pending_sl_orders:
                        if idx < len(filled_layers) and filled_layers[idx]:
                            still_pending.append(idx)

                    if still_pending:
                        print(f"⚠️ [批次 {batch_id}] 待补挂层 {still_pending} 未能处理，等待下一轮轮询")

        # ================================================================
        # 🔥 异常捕获 - 监控循环内部异常
        # ================================================================
        except Exception as inner_e:
            print(f"⚠️ 监控循环内部异常: {inner_e}")
            import traceback
            traceback.print_exc()

        # ================================================================
        # 🔥 finally 块 - 确保清理工作始终执行
        # ================================================================
        finally:
            # 清理程序撤单标记和批次状态（如果是程序撤单导致的退出）
            try:
                all_states = self.load_all_states()
                b_data = all_states.get(symbol, {}).get(batch_id, {})
                if b_data:
                    # 如果是程序撤单或 pending_close 标记，清理批次
                    if b_data.get('is_programmatic_cancel') or b_data.get('pending_close'):
                        self.clear_batch_state(symbol, batch_id)
                        print(f"  └─ 🧹 程序撤单，批次状态已清理")
            except Exception as e:
                print(f"  └─ ⚠️ 清理程序撤单标记失败: {e}")

            # 检查是否有持仓，如果没有则清理批次状态
            try:
                positions = self._safe_api_call(self.exchange.fetch_positions, [symbol])
                current_pos = 0.0
                for pos in positions:
                    if pos.get('symbol') == symbol or pos.get('info', {}).get('symbol') == \
                            symbol.replace('/', '').split(':')[0]:
                        current_pos = abs(float(pos.get('contracts', 0) or pos.get('positionAmt', 0)))
                        break
            except Exception:
                current_pos = 0.0

            # 如果无持仓，清理批次
            if current_pos == 0:
                all_states = self.load_all_states()
                b_data = all_states.get(symbol, {}).get(batch_id, {})
                if b_data:
                    self.clear_batch_state(symbol, batch_id)
                    print(f"  └─ 🧹 无持仓，已清理批次状态")
            else:
                print(f"  └─ 📌 有持仓 {current_pos}，保留批次状态")

            print(f"🧹 批次 [{batch_id}] 监控线程已退出")

    def _place_prepared_orders_immediately(self, symbol, batch_id, idx, batch_filled_amount,
                                           prepared_tp_params, layer_sl_params, layer_tp_params,
                                           is_hedge_mode, params_base, stop_steps):
        """🔥 成交后立即使用预生成的参数挂止盈和止损单（1秒内完成）
        注意：此方法只在 current_sl_id 为 None 时调用，即首次成交时
        """
        latest_all = self.load_all_states()
        latest_b_data = latest_all.get(symbol, {}).get(batch_id, {})

        # 🔥 只在没有止损单时才挂（首次成交）
        if latest_b_data.get('current_sl_id') is None:
            if idx < len(layer_sl_params):
                sl_params = layer_sl_params[idx].copy()
                sl_params['amount'] = batch_filled_amount
                if not is_hedge_mode:
                    sl_params['params']['reduceOnly'] = True

                try:
                    new_sl_order = self._safe_api_call(
                        self.exchange.create_order,
                        symbol=sl_params['symbol'],
                        type=sl_params['type'],
                        side=sl_params['side'],
                        amount=sl_params['amount'],
                        params=sl_params['params']
                    )
                    if latest_b_data:
                        sl_price = sl_params['params']['stopPrice']
                        latest_b_data['current_sl_id'] = new_sl_order['id']
                        # 从待挂列表中移除当前层
                        pending = latest_b_data.get('pending_sl_orders', [])
                        if idx in pending:
                            pending.remove(idx)
                        latest_b_data['pending_sl_orders'] = pending
                        self.save_batch_state(symbol, batch_id, latest_b_data)
                        print(f"  └─ ⚡ 预生成止损单已挂出: {sl_price} (ID: {new_sl_order['id']})")
                except Exception as e:
                    print(f"  └─ ⚡ 预生成止损单挂出失败: {e}")
                    # 🔥 记录失败，发送告警
                    if latest_b_data:
                        sl_fail_count = latest_b_data.get('sl_fail_count', {})
                        layer_key = str(idx)
                        sl_fail_count[layer_key] = sl_fail_count.get(layer_key, 0) + 1
                        latest_b_data['sl_fail_count'] = sl_fail_count
                        self.save_batch_state(symbol, batch_id, latest_b_data)
                    self.send_tg_notification(
                        f"🚨 **止损单预生成挂单失败！**\n"
                        f"🆔 批次：`{batch_id}`\n"
                        f"📊 第 {idx + 1} 层\n"
                        f"💡 原因：{str(e)[:100]}\n"
                        f"⚠️ 程序将重试，请关注后续通知！"
                    )
            else:
                raw_sl_price = stop_steps[idx] if idx < len(stop_steps) else stop_steps[-1]
                formatted_sl_price = float(self.exchange.price_to_precision(symbol, raw_sl_price))
                sl_params = params_base.copy()
                sl_params['stopPrice'] = formatted_sl_price
                if not is_hedge_mode:
                    sl_params['reduceOnly'] = True
                sl_side = 'sell' if side == 'BUY' else 'buy'
                try:
                    new_sl_order = self._safe_api_call(
                        self.exchange.create_order,
                        symbol=symbol,
                        type='STOP_MARKET',
                        side=sl_side,
                        amount=batch_filled_amount,
                        params=sl_params
                    )
                    latest_all = self.load_all_states()
                    latest_b_data = latest_all.get(symbol, {}).get(batch_id, {})
                    if latest_b_data:
                        latest_b_data['current_sl_id'] = new_sl_order['id']
                        pending = latest_b_data.get('pending_sl_orders', [])
                        if idx in pending:
                            pending.remove(idx)
                        latest_b_data['pending_sl_orders'] = pending
                        self.save_batch_state(symbol, batch_id, latest_b_data)
                        print(f"  └─ ⚡ 止损单已挂出(兜底): {formatted_sl_price} (ID: {new_sl_order['id']})")
                except Exception as e:
                    print(f"  └─ ⚡ 止损单挂出失败(兜底): {e}")
                    if latest_b_data:
                        sl_fail_count = latest_b_data.get('sl_fail_count', {})
                        layer_key = str(idx)
                        sl_fail_count[layer_key] = sl_fail_count.get(layer_key, 0) + 1
                        latest_b_data['sl_fail_count'] = sl_fail_count
                        self.save_batch_state(symbol, batch_id, latest_b_data)
                    self.send_tg_notification(
                        f"🚨 **止损单挂出失败(兜底)！**\n"
                        f"🆔 批次：`{batch_id}`\n"
                        f"📊 第 {idx + 1} 层\n"
                        f"💡 原因：{str(e)[:100]}\n"
                        f"⚠️ 程序将重试，请关注后续通知！"
                    )
        else:
            print(f"  └─ ⚡ 已存在止损单，等待主循环合并更新")

        # 挂止盈单（首次成交时挂，后续不重复挂）
        if latest_b_data and latest_b_data.get('tp_order_id') is None:
            try:
                tp_params = prepared_tp_params.copy()
                tp_params['amount'] = batch_filled_amount
                if not is_hedge_mode:
                    tp_params['params']['reduceOnly'] = True

                new_tp_order = self._safe_api_call(
                    self.exchange.create_order,
                    symbol=tp_params['symbol'],
                    type=tp_params['type'],
                    side=tp_params['side'],
                    amount=tp_params['amount'],
                    params=tp_params['params']
                )
                latest_all = self.load_all_states()
                latest_b_data = latest_all.get(symbol, {}).get(batch_id, {})
                if latest_b_data:
                    latest_b_data['tp_order_id'] = new_tp_order['id']
                    self.save_batch_state(symbol, batch_id, latest_b_data)
                    print(f"  └─ ⚡ 预生成止盈单已挂出: {tp_params['params']['stopPrice']} (ID: {new_tp_order['id']})")
            except Exception as e:
                print(f"  └─ ⚡ 预生成止盈单挂出失败: {e}")
        else:
            print(f"  └─ ⚡ 已存在止盈单，等待主循环合并更新")

    # ==================== 新增：取消挂单 ====================

    def cancel_open_orders(self, batch_id: str) -> tuple[bool, str]:
        """
        取消指定批次的所有未成交开仓条件单
        已成交的层保留持仓，止盈止损单不受影响
        """
        all_states = self.load_all_states()
        target_symbol = None
        target_b_data = None

        for symbol, symbol_batches in all_states.items():
            if batch_id in symbol_batches and symbol_batches[batch_id].get('is_active'):
                target_symbol = symbol
                target_b_data = symbol_batches[batch_id]
                break

        if not target_b_data:
            return False, f"❌ 未找到处于活跃状态的批次号 `{batch_id}`"

        entry_orders = target_b_data.get('entry_orders', [])
        last_filled_count = target_b_data.get('last_filled_count', 0)
        pending_count = len(entry_orders) - last_filled_count

        if pending_count <= 0:
            return False, f"ℹ️ 批次 `{batch_id}` 没有未成交的挂单"

        # 🔥 设置标记：这是程序主动撤单
        target_b_data['is_programmatic_cancel'] = True
        self.save_batch_state(target_symbol, batch_id, target_b_data)

        # 记录要取消的订单ID
        cancelled_ids = []
        cancelled_layers = []

        for idx in range(last_filled_count, len(entry_orders)):
            order_id = entry_orders[idx]
            try:
                self._safe_api_call(self.exchange.cancel_order, order_id, target_symbol, params={'stop': True})
                cancelled_ids.append(order_id)
                cancelled_layers.append(idx + 1)
                print(f"  └─ 已撤销第 {idx + 1} 层挂单: {order_id}")
            except Exception as e:
                print(f"  └─ ⚠️ 撤销第 {idx + 1} 层挂单失败: {e}")

        if not cancelled_ids:
            # 撤销失败，清除标记
            target_b_data.pop('is_programmatic_cancel', None)
            self.save_batch_state(target_symbol, batch_id, target_b_data)
            return False, f"⚠️ 批次 `{batch_id}` 挂单撤销失败，请检查订单状态"

        # 从状态中移除已撤销的订单
        remaining_orders = entry_orders[:last_filled_count]
        target_b_data['entry_orders'] = remaining_orders

        # 更新 pending_sl_orders
        pending_sl = target_b_data.get('pending_sl_orders', [])
        pending_sl = [idx for idx in pending_sl if idx < last_filled_count]
        target_b_data['pending_sl_orders'] = pending_sl

        current_持仓 = sum(target_b_data.get('target_amounts', [])[:last_filled_count])

        # 🔥 根据是否有已成交层，决定如何处理
        if last_filled_count > 0:
            # 有已成交层：部分撤单，监控继续
            self.save_batch_state(target_symbol, batch_id, target_b_data)
            result_msg = (
                f"🗑️ **撤单完成**\n\n"
                f"🆔 批次：`{batch_id}`\n"
                f"🪙 标的：`{target_symbol}`\n"
                f"📊 已撤销：{len(cancelled_ids)} 个挂单\n"
                f"├─ 层数：{cancelled_layers}\n"
                f"├─ 订单ID：{cancelled_ids}\n"
                f"📊 当前持仓：{current_持仓}\n"
                f"📊 剩余待成交层数：{len(remaining_orders) - last_filled_count}\n\n"
                f"💡 {last_filled_count} 层已成交，止盈止损单已保留，监控继续运行"
            )
        else:
            # 🔥 无已成交层：全部撤单
            # 保留状态，让监控线程检测到 is_programmatic_cancel 后自然退出
            # 标记批次为"即将终止"状态，让监控线程自己清理
            target_b_data['entry_orders'] = []  # 清空订单列表
            target_b_data['pending_sl_orders'] = []
            target_b_data['pending_close'] = True  # 🔥 标记：批次即将关闭
            self.save_batch_state(target_symbol, batch_id, target_b_data)

            result_msg = (
                f"🗑️ **撤单完成**\n\n"
                f"🆔 批次：`{batch_id}`\n"
                f"🪙 标的：`{target_symbol}`\n"
                f"📊 已撤销：{len(cancelled_ids)} 个挂单\n"
                f"├─ 层数：{cancelled_layers}\n"
                f"├─ 订单ID：{cancelled_ids}\n"
                f"📊 当前持仓：0\n"
                f"📊 剩余待成交层数：0\n\n"
                f"💡 批次已无挂单，监控已自动退出"
            )

        return True, result_msg

    # ==================== 新增：市价平仓 ====================

    def close_position_market(self, batch_id: str) -> tuple[bool, str]:
        """
        市价平仓 - 立即以市价平掉该批次全部持仓
        """
        all_states = self.load_all_states()
        target_symbol = None
        target_b_data = None

        for symbol, symbol_batches in all_states.items():
            if batch_id in symbol_batches and symbol_batches[batch_id].get('is_active'):
                target_symbol = symbol
                target_b_data = symbol_batches[batch_id]
                break

        if not target_b_data:
            return False, f"❌ 未找到处于活跃状态的批次号 `{batch_id}`"

        last_filled_count = target_b_data.get('last_filled_count', 0)
        target_amounts = target_b_data.get('target_amounts', [])
        current_filled_amount = sum(target_amounts[:last_filled_count])
        side = target_b_data.get('side', 'BUY')

        if current_filled_amount <= 0:
            return False, f"⚠️ 批次 `{batch_id}` 尚未建仓，无需平仓"

        # 🔥 标记这是程序主动撤单，监控线程将静默退出
        target_b_data['is_programmatic_cancel'] = True
        target_b_data['pending_close'] = True
        self.save_batch_state(target_symbol, batch_id, target_b_data)

        # 获取当前市价
        try:
            ticker = self._safe_api_call(self.exchange.fetch_ticker, target_symbol)
            current_price = float(ticker.get('last') or ticker.get('close') or 0.0)
        except Exception as e:
            return False, f"❌ 获取市价失败: {e}"

        # 计算均价和预估盈亏
        filled_details = target_b_data.get('filled_details', [])
        total_cost = sum([target_amounts[i] * filled_details[i] for i in range(last_filled_count)])
        total_entry_fee = target_b_data.get('total_entry_fee', 0.0)
        avg_price = (total_cost + total_entry_fee) / current_filled_amount if current_filled_amount > 0 else 0

        if side == 'BUY':
            gross_pnl = (current_price - avg_price) * current_filled_amount
        else:
            gross_pnl = (avg_price - current_price) * current_filled_amount

        # 估算平仓手续费（市价 = Taker）
        exit_fee = current_price * current_filled_amount * TAKER_FEE_RATE
        total_fees = total_entry_fee + exit_fee
        net_pnl = gross_pnl - total_fees

        # 执行市价平仓
        try:
            # 先撤销所有未成交的开仓条件单
            entry_orders = target_b_data.get('entry_orders', [])
            for idx, order_id in enumerate(entry_orders):
                if idx >= last_filled_count:
                    try:
                        self._safe_api_call(self.exchange.cancel_order, order_id, target_symbol, params={'stop': True})
                        print(f"  └─ 已撤销开仓挂单: {order_id}")
                    except Exception:
                        pass

            # 撤销止盈止损单
            if target_b_data.get('tp_order_id'):
                try:
                    self._safe_api_call(self.exchange.cancel_order, target_b_data['tp_order_id'], target_symbol,
                                        params={'stop': True})
                    print(f"  └─ 已撤销止盈单: {target_b_data['tp_order_id']}")
                except Exception:
                    pass

            if target_b_data.get('current_sl_id'):
                try:
                    self._safe_api_call(self.exchange.cancel_order, target_b_data['current_sl_id'], target_symbol,
                                        params={'stop': True})
                    print(f"  └─ 已撤销止损单: {target_b_data['current_sl_id']}")
                except Exception:
                    pass

            # 市价平仓
            close_side = 'sell' if side == 'BUY' else 'buy'
            order = self._safe_api_call(
                self.exchange.create_order,
                symbol=target_symbol,
                type='MARKET',
                side=close_side,
                amount=current_filled_amount,
                params={'reduceOnly': True}
            )

            # 获取实际成交价格
            actual_price = float(order.get('average') or order.get('price') or current_price)

            # 重新计算实际盈亏
            if side == 'BUY':
                actual_gross_pnl = (actual_price - avg_price) * current_filled_amount
            else:
                actual_gross_pnl = (avg_price - actual_price) * current_filled_amount

            actual_exit_fee = actual_price * current_filled_amount * TAKER_FEE_RATE
            actual_total_fees = total_entry_fee + actual_exit_fee
            actual_net_pnl = actual_gross_pnl - actual_total_fees

            capital_base = avg_price * current_filled_amount if current_filled_amount > 0 else 1
            net_pnl_pct = (actual_net_pnl / capital_base) * 100 if capital_base > 0 else 0.0

            pnl_emoji = "🟢" if actual_net_pnl >= 0 else "🔴"

            # 清理批次状态
            self.clear_batch_state(target_symbol, batch_id)

            result_msg = (
                f"📊 **[市价平仓结算]**\n\n"
                f"🆔 **批次号**：`{batch_id}`\n"
                f"🪙 **标的**：`{target_symbol}`\n"
                f"📊 **方向**：`{side}`\n"
                f"📊 **平仓模式**：市价单 (Taker {TAKER_FEE_RATE * 100:.2f}%)\n"
                f"📊 **持仓**：`{current_filled_amount}` ({last_filled_count}层)\n"
                f"📈 **持仓均价**：`{avg_price:.2f}` USDT\n"
                f"💵 **平仓均价**：`{actual_price:.2f}` USDT\n"
                f"📊 **名义盈亏**：`{actual_gross_pnl:+.2f}` USDT\n"
                f"💸 **总手续费**：`{actual_total_fees:.4f}` USDT\n"
                f"{pnl_emoji} **最终净盈亏**：`{actual_net_pnl:+.2f}` USDT (`{net_pnl_pct:+.2f}%`)"
            )

            print(f"\n{result_msg}")
            return True, result_msg

        except Exception as e:
            return False, f"❌ 市价平仓失败: {e}"

    # ==================== 新增：限价平仓（支持最优价和自定义价） ====================

    def close_position_limit(self, batch_id: str, price: float = None) -> tuple[bool, str]:
        """
        限价平仓
        - price=None: 最优价挂单（当前对手价，Maker费率）
        - price=数值: 用户指定价格
        """
        all_states = self.load_all_states()
        target_symbol = None
        target_b_data = None

        for symbol, symbol_batches in all_states.items():
            if batch_id in symbol_batches and symbol_batches[batch_id].get('is_active'):
                target_symbol = symbol
                target_b_data = symbol_batches[batch_id]
                break

        if not target_b_data:
            return False, f"❌ 未找到处于活跃状态的批次号 `{batch_id}`"

        last_filled_count = target_b_data.get('last_filled_count', 0)
        target_amounts = target_b_data.get('target_amounts', [])
        current_filled_amount = sum(target_amounts[:last_filled_count])
        side = target_b_data.get('side', 'BUY')

        if current_filled_amount <= 0:
            return False, f"⚠️ 批次 `{batch_id}` 尚未建仓，无需平仓"

        # 🔥 标记这是程序主动撤单，监控线程将静默退出
        target_b_data['is_programmatic_cancel'] = True
        target_b_data['pending_close'] = True
        self.save_batch_state(target_symbol, batch_id, target_b_data)

        # 获取当前市价
        try:
            ticker = self._safe_api_call(self.exchange.fetch_ticker, target_symbol)
            current_price = float(ticker.get('last') or ticker.get('close') or 0.0)
            bid = float(ticker.get('bid') or current_price)
            ask = float(ticker.get('ask') or current_price)
        except Exception as e:
            return False, f"❌ 获取市价失败: {e}"

        # 确定挂单价格
        if price is None:
            # 最优价：做多平仓用卖一，做空平仓用买一
            if side == 'BUY':
                limit_price = ask
            else:
                limit_price = bid
            price_mode = "💎 最优价挂单"
        else:
            limit_price = float(self.exchange.price_to_precision(target_symbol, price))
            price_mode = f"✏️ 自定义价格 {limit_price}"

        # 检查价格是否合理（做多平仓价应高于成本价，做空平仓价应低于成本价）
        # 但只是警告，不阻止
        filled_details = target_b_data.get('filled_details', [])
        total_cost = sum([target_amounts[i] * filled_details[i] for i in range(last_filled_count)])
        total_entry_fee = target_b_data.get('total_entry_fee', 0.0)
        avg_price = (total_cost + total_entry_fee) / current_filled_amount if current_filled_amount > 0 else 0

        if side == 'BUY' and limit_price <= avg_price:
            print(f"⚠️ 警告：平仓价 {limit_price} 不高于均价 {avg_price}，可能亏损")
        elif side == 'SELL' and limit_price >= avg_price:
            print(f"⚠️ 警告：平仓价 {limit_price} 不低于均价 {avg_price}，可能亏损")

        # 执行限价平仓
        try:
            # 先撤销所有未成交的开仓条件单
            entry_orders = target_b_data.get('entry_orders', [])
            for idx, order_id in enumerate(entry_orders):
                if idx >= last_filled_count:
                    try:
                        self._safe_api_call(self.exchange.cancel_order, order_id, target_symbol, params={'stop': True})
                        print(f"  └─ 已撤销开仓挂单: {order_id}")
                    except Exception:
                        pass

            # 撤销原有止盈单（避免冲突）
            if target_b_data.get('tp_order_id'):
                try:
                    self._safe_api_call(self.exchange.cancel_order, target_b_data['tp_order_id'], target_symbol,
                                        params={'stop': True})
                    print(f"  └─ 已撤销旧止盈单: {target_b_data['tp_order_id']}")
                except Exception:
                    pass

            # 挂限价平仓单
            close_side = 'sell' if side == 'BUY' else 'buy'
            order_params = target_b_data['params_base'].copy()
            # 🔥 限价单不支持 reduceOnly，用 positionSide 替代
            if target_b_data.get('is_hedge_mode', False):
                order_params['positionSide'] = 'LONG' if side == 'BUY' else 'SHORT'

            order = self._safe_api_call(
                self.exchange.create_order,
                symbol=target_symbol,
                type='LIMIT',
                side=close_side,
                amount=current_filled_amount,
                price=limit_price,
                params=order_params
            )

            order_id = order['id']

            # 保存限价单ID到状态
            target_b_data['limit_close_order_id'] = order_id
            target_b_data['limit_close_price'] = limit_price
            target_b_data['limit_close_mode'] = price_mode
            self.save_batch_state(target_symbol, batch_id, target_b_data)

            # 计算预计盈亏
            if side == 'BUY':
                est_gross_pnl = (limit_price - avg_price) * current_filled_amount
            else:
                est_gross_pnl = (avg_price - limit_price) * current_filled_amount

            est_exit_fee = limit_price * current_filled_amount * MAKER_FEE_RATE
            est_total_fees = total_entry_fee + est_exit_fee
            est_net_pnl = est_gross_pnl - est_total_fees

            pnl_emoji = "🟢" if est_net_pnl >= 0 else "🔴"

            result_msg = (
                f"💰 **限价平仓单已挂出**\n\n"
                f"🆔 **批次号**：`{batch_id}`\n"
                f"🪙 **标的**：`{target_symbol}`\n"
                f"📊 **方向**：`{side}`\n"
                f"📊 **持仓**：`{current_filled_amount}` ({last_filled_count}层)\n"
                f"📈 **持仓均价**：`{avg_price:.2f}` USDT\n"
                f"📊 **挂单价**：`{limit_price:.2f}` USDT\n"
                f"📊 **模式**：{price_mode}\n"
                f"📊 **预计盈亏**：{pnl_emoji} `{est_net_pnl:+.2f}` USDT\n\n"
                f"🛡️ **止损单仍保留作为保护**\n"
                f"💡 限价单成交后，批次将自动结算"
            )

            print(f"\n{result_msg}")

            # 🔥 启动一个后台线程监控限价单成交
            monitor_thread = threading.Thread(
                target=self._monitor_limit_close,
                args=(target_symbol, batch_id, order_id, current_filled_amount, avg_price, total_entry_fee, side,
                      last_filled_count, target_amounts, filled_details),
                daemon=True
            )
            monitor_thread.start()

            return True, result_msg

        except Exception as e:
            return False, f"❌ 挂限价平仓单失败: {e}"

    # ==================== 新增：监控限价平仓单 ====================

    def _monitor_limit_close(self, symbol: str, batch_id: str, order_id: str,
                             current_filled_amount: float, avg_price: float, total_entry_fee: float,
                             side: str, last_filled_count: int, target_amounts: list, filled_details: list):
        """
        后台监控限价平仓单是否成交
        """
        print(f"👀 [限价平仓监控] 批次 {batch_id} 订单 {order_id} 监控启动...")

        try:
            while True:
                time.sleep(3)

                try:
                    order = self._safe_api_call(self.exchange.fetch_order, order_id, symbol)
                    status = order.get('status')
                except Exception as e:
                    print(f"⚠️ [限价平仓监控] 查询订单状态失败: {e}")
                    continue

                if status == 'closed' or status == 'filled':
                    actual_price = float(order.get('average') or order.get('price') or 0.0)
                    if actual_price == 0.0:
                        info = order.get('info', {})
                        cum_quote = float(info.get('cumQuote', 0.0))
                        executed_qty = float(info.get('executedQty', 0.0))
                        if cum_quote > 0 and executed_qty > 0:
                            actual_price = cum_quote / executed_qty
                        else:
                            actual_price = float(order.get('price') or 0.0)

                    print(f"✅ [限价平仓监控] 批次 {batch_id} 限价单已成交，价格: {actual_price}")

                    # 计算盈亏
                    if side == 'BUY':
                        gross_pnl = (actual_price - avg_price) * current_filled_amount
                    else:
                        gross_pnl = (avg_price - actual_price) * current_filled_amount

                    exit_fee = actual_price * current_filled_amount * MAKER_FEE_RATE
                    total_fees = total_entry_fee + exit_fee
                    net_pnl = gross_pnl - total_fees

                    capital_base = avg_price * current_filled_amount if current_filled_amount > 0 else 1
                    net_pnl_pct = (net_pnl / capital_base) * 100 if capital_base > 0 else 0.0
                    pnl_emoji = "🟢" if net_pnl >= 0 else "🔴"

                    # 🔥 先设置标记，防止主循环重复结算
                    all_states = self.load_all_states()
                    b_data = all_states.get(symbol, {}).get(batch_id, {})
                    if b_data:
                        b_data['settled_by_limit_close'] = True
                        # 🔥 保留 is_programmatic_cancel，防止撤单提醒
                        b_data['is_programmatic_cancel'] = True
                        self.save_batch_state(symbol, batch_id, b_data)

                    # 🔥 撤销止损单
                    if b_data.get('current_sl_id'):
                        try:
                            self._safe_api_call(self.exchange.cancel_order, b_data['current_sl_id'], symbol,
                                                params={'stop': True})
                            print(f"  └─ 已撤销止损单: {b_data['current_sl_id']}")
                        except Exception:
                            pass

                    # 🔥 不调用 clear_batch_state，让主循环的 finally 块清理

                    result_msg = (
                        f"🎉 **[限价平仓结算]**\n\n"
                        f"🆔 **批次号**：`{batch_id}`\n"
                        f"🪙 **标的**：`{symbol}`\n"
                        f"📊 **方向**：`{side}`\n"
                        f"📊 **平仓模式**：限价单 (Maker {MAKER_FEE_RATE * 100:.2f}%)\n"
                        f"📊 **持仓**：`{current_filled_amount}` ({last_filled_count}层)\n"
                        f"📈 **持仓均价**：`{avg_price:.2f}` USDT\n"
                        f"💵 **平仓均价**：`{actual_price:.2f}` USDT\n"
                        f"📊 **名义盈亏**：`{gross_pnl:+.2f}` USDT\n"
                        f"💸 **总手续费**：`{total_fees:.4f}` USDT\n"
                        f"{pnl_emoji} **最终净盈亏**：`{net_pnl:+.2f}` USDT (`{net_pnl_pct:+.2f}%`)"
                    )

                    print(f"\n{result_msg}")
                    self.send_tg_notification(result_msg)
                    break

                elif status == 'canceled' or status == 'expired':
                    print(f"⚠️ [限价平仓监控] 批次 {batch_id} 限价单已取消/过期")
                    all_states = self.load_all_states()
                    b_data = all_states.get(symbol, {}).get(batch_id, {})
                    if b_data:
                        b_data.pop('limit_close_order_id', None)
                        b_data.pop('limit_close_price', None)
                        b_data.pop('limit_close_mode', None)
                        self.save_batch_state(symbol, batch_id, b_data)
                    break

        except Exception as e:
            print(f"❌ [限价平仓监控] 批次 {batch_id} 异常: {e}")
            import traceback
            traceback.print_exc()

        print(f"🧹 [限价平仓监控] 批次 {batch_id} 监控线程已退出")


if __name__ == "__main__":
    print("⚠️ 请通过 bot_runner.py 启动完整的交易系统")
    print("🔧 trader_260725.py 仅供导入使用")