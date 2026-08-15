# trader_260725.py
import json
import os
import random
import tempfile
import time
import ccxt
import threading
import asyncio
import re
from datetime import datetime, timedelta
import pytz
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

# 北京时间时区（与 watchdog.py 保持一致，日报/盈亏记录统一使用）
BEIJING_TZ = pytz.timezone('Asia/Shanghai')

# QQ 邮箱发送串行锁（限制同时最多 1 个 SMTP 连接）
EMAIL_SEND_LOCK = threading.Lock()

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

        self.proxy_url = proxy_url  # 保存供 IP 检测使用

        self.exchange = ccxt.binanceusdm(exchange_config)

        self.tg_bot = tg_bot
        self.chat_id = chat_id
        self.loop = loop
        self.verbose = verbose

        # 🔥 IP 监控相关
        self.ip_file = os.path.join(os.path.dirname(__file__), ".last_ip.txt")
        self._load_last_ip()
        self.last_ip_check_time = 0
        self.IP_CHECK_INTERVAL = 600  # 5 分钟

        # 🔥 是否启用 IP 检测（云服务器有固定 IP 时可以禁用）
        self.IP_CHECK_ENABLED = os.getenv("IP_CHECK_ENABLED", "true").lower() == "true"
        if not self.IP_CHECK_ENABLED and self.verbose:
            print("ℹ️ IP 检测已禁用（IP_CHECK_ENABLED=false）")

        # 🔥 记录最近一次币安报告的 IP（用于调试）
        self._last_reported_ip = None

        # 🔥 全局 API 请求限流
        self._api_lock = threading.Lock()
        self._last_api_call_time = 0
        self._min_api_interval = 0.2  # 200ms 最小间隔

        # 🔥 全局 API 信号量（串行化所有 API 请求）
        self._api_semaphore = threading.Semaphore(1)

        # 🔥 状态文件读写锁（保护 read-modify-write 原子性）
        self._state_lock = threading.Lock()

        # 🔥 全局 API 熔断器（统一冷却时间）
        self.api_cooldown_until = 0
        self.api_cooldown_lock = threading.Lock()

        # 🔥 监控线程去重
        self._active_monitors = set()
        self._active_monitors_lock = threading.Lock()

        if verbose:
            print("正在连接交易所并同步服务器时间/加载元数据...")
        self._safe_api_call(self.exchange.load_time_difference)
        self._safe_api_call(self.exchange.load_markets, True)

        # 🔥 强制同步服务器时间
        self._safe_api_call(self.exchange.fetch_time)
        self._safe_api_call(self.exchange.load_time_difference)

        self.last_time_sync = time.time()

        # 🔥 每日结算日报线程（daemon，每天 08:05 发送昨日结算）
        self._last_daily_report_date = None
        threading.Thread(target=self._daily_report_loop, daemon=True).start()

    # ==================== IP 监控方法 ====================

    def _load_last_ip(self):
        """加载上次记录的 IP"""
        self.last_known_ip = None
        if os.path.exists(self.ip_file):
            try:
                with open(self.ip_file, 'r', encoding='utf-8') as f:
                    self.last_known_ip = f.read().strip()
            except Exception as e:
                import traceback
                print(f"⚠️ [异常] 加载 IP 文件失败: {e}")
                traceback.print_exc()

    def _save_last_ip(self, ip: str):
        """保存当前 IP"""
        try:
            with open(self.ip_file, 'w', encoding='utf-8') as f:
                f.write(ip)
            self.last_known_ip = ip
        except Exception as e:
            print(f"⚠️ 保存 IP 失败: {e}")

    def _get_public_ip(self) -> str | None:
        """主动获取当前公网 IP（走代理时查币安实际看到的出口 IP，静默失败）"""
        if not self.IP_CHECK_ENABLED:
            return self.last_known_ip

        try:
            import urllib.request

            # 如果配置了代理，走代理查询（与币安 API 同一出口 IP）
            if getattr(self, 'proxy_url', None):
                proxy_handler = urllib.request.ProxyHandler({
                    'http': self.proxy_url,
                    'https': self.proxy_url,
                })
                opener = urllib.request.build_opener(proxy_handler)
            else:
                opener = urllib.request.build_opener()

            req = urllib.request.Request(
                'https://api.ipify.org',
                headers={'User-Agent': 'Mozilla/5.0'}
            )
            with opener.open(req, timeout=5) as response:
                ip = response.read().decode('utf-8').strip()
                if ip and '.' in ip and len(ip) < 20:
                    return ip
        except Exception:
            pass
        return None

    def _record_ip_change(self, ip: str, source: str = "binance_error"):
        """记录 IP 变化并发送通知"""
        if not ip:
            return

        self._last_reported_ip = ip

        if ip == self.last_known_ip:
            print(f"ℹ️ [IP重复] 币安报告 IP: {ip} (与上次记录相同，已忽略)")
            return

        self._save_last_ip(ip)

        msg = (
            f"⚠️ IP 地址已变化！\n"
            f"📌 新 IP: {ip}\n"
            f"📂 来源: {source}\n"
            f"⏰ 时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"💡 请将新 IP 添加到币安 API 白名单！"
        )

        print(f"\n{'=' * 60}")
        print(f"🔔 [{time.strftime('%Y-%m-%d %H:%M:%S')}]")
        print(msg)
        print(f"{'=' * 60}\n")

        sent = self._try_async_send(msg)

        if not sent:
            self._fallback_notify_file(ip, source)

    def get_last_reported_ip(self) -> str | None:
        """获取最近一次币安报告的 IP（用于调试）"""
        return self._last_reported_ip

    def _try_async_send(self, text: str) -> bool:
        """尝试通过异步方式发送，返回是否成功"""
        if not self.tg_bot or not self.chat_id or not self.loop:
            print(f"⚠️ [异步TG] 缺少必要参数")
            return False

        try:
            future = asyncio.run_coroutine_threadsafe(
                self.tg_bot.send_message(
                    chat_id=self.chat_id,
                    text=text
                    # 纯文本，不使用 Markdown
                ),
                self.loop
            )
            future.result(timeout=5)
            print(f"📤 [异步TG] 通知发送成功")
            return True
        except asyncio.TimeoutError:
            print(f"⚠️ [异步TG] 发送超时 (5秒)")
            return False
        except Exception as e:
            print(f"⚠️ [异步TG] 发送失败: {e}")
            return False

    def _fallback_notify_file(self, ip: str, source: str = "binance_error"):
        """备用方式：写入 .notify 文件，由 bot_runner 在启动后发送"""
        try:
            # 构建纯文本消息（去掉 Markdown 特殊字符）
            plain_msg = (
                f"⚠️ IP 地址已变化！\n"
                f"新 IP: {ip}\n"
                f"来源: {source}\n"
                f"时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"请将新 IP 添加到币安 API 白名单！"
            )

            base_dir = os.path.dirname(os.path.abspath(__file__))
            notify_file = os.path.join(base_dir, ".notify")
            content = f"ip_notify|{plain_msg}"

            with tempfile.NamedTemporaryFile("w", dir=base_dir, delete=False, encoding="utf-8") as tf:
                tf.write(content)
                tmp_name = tf.name
            os.replace(tmp_name, notify_file)
            print(f"📝 [备用通知] 已写入 .notify 文件: {notify_file}")
        except Exception as e:
            import traceback
            print(f"⚠️ [备用通知] 写入失败: {e}")
            traceback.print_exc()

    def _extract_ip_from_error(self, error_msg: str) -> str | None:
        """从币安错误信息中提取 IP 地址"""
        match = re.search(r'request ip:\s*([\d.]+)', error_msg, re.IGNORECASE)
        if match:
            return match.group(1)
        return None

    def _check_ip_periodically(self):
        """定期主动检测 IP 变化"""
        if not self.IP_CHECK_ENABLED:
            return False

        try:
            ip = self._get_public_ip()
            if ip:
                if ip != self.last_known_ip:
                    self._record_ip_change(ip, source="periodic_check")
                return True
            return False
        except Exception:
            return False

    def send_tg_notification(self, text: str, reply_markup=None, level: str = 'info'):
        """
        发送 Telegram 通知（异步方式）
        用于正常运行期间的通知发送
        level: 'info' 普通通知 | 'critical' 资金安全告警（自动加醒目前缀）
        """
        if level == 'critical':
            text = f"🚨【资金安全】\n{text}"
            # 🔥 资金安全告警同步推送 QQ 邮箱（兜底通道，独立线程异步发送，失败不影响 TG）
            self._send_email_alert(text, subject="🚨 资金安全告警")
        if self.tg_bot and self.chat_id and self.loop:
            try:
                future = asyncio.run_coroutine_threadsafe(
                    self.tg_bot.send_message(
                        chat_id=self.chat_id,
                        text=text,
                        parse_mode='Markdown',
                        reply_markup=reply_markup
                    ),
                    self.loop
                )
                # 等待发送完成，避免静默失败
                future.result(timeout=5)
            except asyncio.TimeoutError:
                print(f"⚠️ [TG通知] 发送超时 (5秒)")
            except Exception as e:
                print(f"⚠️ [TG通知] 发送失败: {e}")
        else:
            print(f"⚠️ [TG通知] 缺少必要参数，无法发送")

    # ==================== QQ 邮箱告警（兜底通道） ====================

    def _send_email_alert(self, text: str, subject: str = "交易告警") -> None:
        """发送 QQ 邮箱告警（独立线程异步发送，失败静默，未配置自动跳过）
        .env 需配置：QQ_MAIL_USER / QQ_MAIL_AUTH_CODE（QQ邮箱授权码）/ QQ_MAIL_TO（可选，默认=发件人）
        """
        mail_user = os.getenv("QQ_MAIL_USER", "").strip()
        mail_code = os.getenv("QQ_MAIL_AUTH_CODE", "").strip()
        mail_to = os.getenv("QQ_MAIL_TO", "").strip() or mail_user
        if not (mail_user and mail_code and mail_to):
            print("⚠️ [邮件] 未配置 QQ_MAIL_USER/AUTH_CODE，跳过邮件发送")
            return

        def _do_send():
            with EMAIL_SEND_LOCK:
                try:
                    import smtplib
                    from email.mime.text import MIMEText
                    from email.header import Header
                    # 清理 Telegram Markdown 符号，邮件按纯文本显示
                    plain = re.sub(r'[*`]', '', text)
                    msg = MIMEText(plain, "plain", "utf-8")
                    msg["Subject"] = Header(subject, "utf-8")
                    msg["From"] = mail_user
                    msg["To"] = mail_to
                    with smtplib.SMTP_SSL("smtp.qq.com", 465, timeout=10) as server:
                        server.login(mail_user, mail_code)
                        server.sendmail(mail_user, [mail_to], msg.as_string())
                    print(f"📧 [邮件] 已发送: {subject}")
                except Exception as e:
                    print(f"⚠️ [邮件] 发送失败: {e}")

        threading.Thread(target=_do_send, daemon=True).start()

    # ==================== 盈亏记录 / 持仓快照 / 每日日报 ====================

    def _record_realized_pnl(self, batch_id: str, symbol: str, side: str, amount: float,
                             avg_price: float, exit_price: float, net_pnl: float,
                             mode: str) -> None:
        """记录一笔已实现盈亏到 trade_stats.json（原子写入，失败静默）"""
        try:
            base_dir = os.path.dirname(os.path.abspath(__file__))
            stats_file = os.path.join(base_dir, "trade_stats.json")
            with self._state_lock:
                stats = {}
                if os.path.exists(stats_file):
                    try:
                        with open(stats_file, "r", encoding="utf-8") as f:
                            stats = json.load(f)
                    except Exception:
                        stats = {}
                record = {
                    "time": datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S"),
                    "batch_id": batch_id,
                    "symbol": symbol,
                    "side": side,
                    "amount": round(float(amount), 6),
                    "avg_price": round(float(avg_price), 4),
                    "exit_price": round(float(exit_price), 4),
                    "net_pnl": round(float(net_pnl), 4),
                    "mode": mode,
                }
                stats.setdefault("trades", []).append(record)
                with tempfile.NamedTemporaryFile("w", dir=base_dir, delete=False, encoding="utf-8") as tf:
                    json.dump(stats, tf, ensure_ascii=False, indent=2)
                    temp_name = tf.name
                os.replace(temp_name, stats_file)
        except Exception as e:
            print(f"⚠️ [盈亏记录] 写入失败: {e}")

    def _build_position_snapshot(self, exclude_batch_id: str = None) -> str:
        """构建当前所有活跃批次的持仓快照（仅读状态文件，零 API 开销）
        exclude_batch_id: 排除指定批次（用于平仓后显示"剩余"批次）"""
        try:
            all_states = self.load_all_states()
            lines = []
            total_batches = 0
            for symbol, symbol_batches in all_states.items():
                for batch_id, b_data in symbol_batches.items():
                    if not b_data.get('is_active'):
                        continue
                    if batch_id == exclude_batch_id:
                        continue
                    total_batches += 1
                    side = b_data.get('side', 'BUY')
                    emoji = "📈" if side == 'BUY' else "📉"
                    last_filled_count = b_data.get('last_filled_count', 0)
                    target_amounts = b_data.get('target_amounts', [])
                    filled_amount = sum(target_amounts[:last_filled_count])
                    entry_count = len(b_data.get('entry_orders', []))
                    sl_txt = "SL✔️" if b_data.get('current_sl_id') else "SL❌"
                    tp_txt = "TP✔️" if b_data.get('tp_order_id') else "TP❌"
                    lines.append(
                        f"{emoji} `{symbol}` {side} | 持仓 `{filled_amount:.4f}` "
                        f"({last_filled_count}/{entry_count}层) | {sl_txt} {tp_txt}"
                    )
            if not lines:
                return ""
            return f"📋 **剩余活跃批次 ({total_batches})：**\n" + "\n".join(lines)
        except Exception as e:
            print(f"⚠️ [快照] 构建失败: {e}")
            return ""

    def _notify_snapshot(self, exclude_batch_id: str = None):
        """平仓后附带发送剩余活跃批次快照（无剩余批次则静默）"""
        try:
            snapshot_msg = self._build_position_snapshot(exclude_batch_id)
            if snapshot_msg:
                self.send_tg_notification(snapshot_msg)
        except Exception:
            pass

    def _send_daily_report(self):
        """发送每日结算报告（昨日已实现盈亏 + 余额 + 持仓快照）"""
        try:
            report_date = (datetime.now(BEIJING_TZ) - timedelta(days=1)).strftime("%Y-%m-%d")
            base_dir = os.path.dirname(os.path.abspath(__file__))
            stats_file = os.path.join(base_dir, "trade_stats.json")
            today_trades = []
            total_pnl = 0.0
            if os.path.exists(stats_file):
                try:
                    with open(stats_file, "r", encoding="utf-8") as f:
                        stats = json.load(f)
                    for t in stats.get("trades", []):
                        if str(t.get("time", "")).startswith(report_date):
                            today_trades.append(t)
                            total_pnl += float(t.get("net_pnl", 0.0))
                except Exception:
                    pass

            snapshot = self._build_position_snapshot()

            balance_txt = ""
            try:
                balance = self._safe_api_call(self.exchange.fetch_balance)
                usdt = balance.get('USDT', {})
                free = float(usdt.get('free', 0.0) or 0.0)
                balance_txt = f"💰 可用余额: `{free:.2f}` USDT\n"
            except Exception:
                balance_txt = ""

            msg = (
                f"📅 **每日结算报告** `{report_date}`\n\n"
                f"📊 昨日平仓: `{len(today_trades)}` 笔\n"
            )
            if today_trades:
                msg += f"💰 昨日已实现盈亏: `{total_pnl:+.2f}` USDT\n"
                msg += "📝 明细（最近5笔）：\n"
                for t in today_trades[-5:]:
                    emoji = "🟢" if t['net_pnl'] >= 0 else "🔴"
                    msg += f"  {emoji} `{t['symbol']}` {t['side']} {t['mode']} | `{t['net_pnl']:+.2f}` USDT\n"
            else:
                msg += f"💰 昨日已实现盈亏: `0.00` USDT（无平仓）\n"

            msg += f"\n{balance_txt}"
            if snapshot:
                msg += f"\n{snapshot}"

            self.send_tg_notification(msg)
            # 🔥 日报同步推送 QQ 邮箱（留档）
            self._send_email_alert(msg, subject=f"每日结算报告 {report_date}")
        except Exception as e:
            print(f"⚠️ [日报] 发送失败: {e}")

    def _daily_report_loop(self):
        """每日 08:05（北京时间）自动发送昨日结算日报（daemon 线程）"""
        self._last_daily_report_date = None
        while True:
            try:
                now = datetime.now(BEIJING_TZ)
                today = now.strftime("%Y-%m-%d")
                if (now.hour == 8 and now.minute == 5
                        and self._last_daily_report_date != today):
                    self._send_daily_report()
                    self._last_daily_report_date = today
                    time.sleep(90)  # 避免同一窗口重复发送
                else:
                    time.sleep(30)
            except Exception as e:
                print(f"⚠️ [日报] 循环异常: {e}")
                time.sleep(300)

    def _health_check(self) -> bool:
        """交易所健康检查：时间 + 账户 + 持仓"""
        try:
            print("🔍 [健康检查] 检查时间同步...")
            self._safe_api_call(self.exchange.fetch_time)
            print("✅ [健康检查] 时间同步正常")
            print("🔍 [健康检查] 检查账户余额...")
            self._safe_api_call(self.exchange.fetch_balance)
            print("✅ [健康检查] 账户余额正常")
            print("🔍 [健康检查] 检查持仓...")
            self._safe_api_call(self.exchange.fetch_positions)
            print("✅ [健康检查] 持仓查询正常")
            return True
        except Exception as e:
            import traceback
            print(f"⚠️ [健康检查失败] {e}")
            traceback.print_exc()
            return False

    def _wait_for_api_cooldown(self):
        """等待全局 API 熔断结束"""
        while True:
            with self.api_cooldown_lock:
                wait_time = self.api_cooldown_until - time.time()

            if wait_time <= 0:
                return

            print(f"🚫 [API熔断] 等待 {wait_time:.1f} 秒...")
            time.sleep(min(wait_time, 5.0))

    def _parse_binance_ban_time(self, error_msg: str) -> float:
        """从币安错误信息中解析封禁时间，返回需要等待的秒数，默认 300 秒"""
        match = re.search(r'banned until (\d+)', str(error_msg), re.IGNORECASE)
        if match:
            try:
                ban_timestamp_ms = int(match.group(1))
                ban_timestamp_s = ban_timestamp_ms / 1000.0
                wait_time = ban_timestamp_s - time.time()
                if wait_time > 0:
                    print(f"🔍 [封禁解析] Binance 指定封禁至 {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ban_timestamp_s))}")
                    return wait_time + 5
            except Exception:
                pass
        return 300.0

    def _safe_api_call(self, func, *args, retries=5, delay=2, **kwargs):
        for i in range(retries):
            # 🔥 每次重试都检查全局熔断（感知其他线程设置的冷却）
            self._wait_for_api_cooldown()
            try:
                # 🔥 单次 API 请求才占用信号量
                with self._api_semaphore:
                    with self._api_lock:
                        now = time.time()
                        wait_time = self._min_api_interval - (now - self._last_api_call_time)
                        if wait_time > 0:
                            time.sleep(wait_time)
                        self._last_api_call_time = time.time()
                    return func(*args, **kwargs)

            except Exception as e:
                err_str = str(e).lower()

                # 🔥 检测到 IP 相关错误
                if "-2015" in err_str or "invalid api-key" in err_str or "permissions" in err_str:
                    ip = self._extract_ip_from_error(str(e))
                    if ip:
                        print(f"🔍 [IP检测] 币安报告的 IP: {ip} (当前记录的 IP: {self.last_known_ip})")
                        self._record_ip_change(ip, source="binance_error")
                    if i == retries - 1:
                        raise e
                    time.sleep(delay)
                    continue

                # 🔥 -1021 时间戳错误特殊处理
                if "-1021" in err_str or "recvwindow" in err_str:
                    try:
                        self.exchange.load_time_difference()
                        self.last_time_sync = time.time()
                        print(f"🔄 [时间同步] 已重新同步服务器时间")
                        time.sleep(2)  # 等待同步生效
                    except Exception as sync_e:
                        print(f"⚠️ 时间同步失败: {sync_e}")
                    if i == retries - 1:
                        raise e
                    time.sleep(1)
                    continue

                # 🔥 429/IP封禁熔断处理
                if (isinstance(e, ccxt.RateLimitExceeded) or
                        "429" in err_str or "-1003" in err_str or
                        "too many requests" in err_str or
                        "banned" in err_str or "418" in err_str or
                        "way too many requests" in err_str):

                    # 🔥 检测到 IP 封禁 → 触发全局熔断
                    if "banned" in err_str or "418" in err_str or "way too many requests" in err_str:
                        ban_seconds = self._parse_binance_ban_time(str(e))
                        with self.api_cooldown_lock:
                            self.api_cooldown_until = max(
                                self.api_cooldown_until,
                                time.time() + ban_seconds
                            )
                        print(f"🚫 [全局熔断] IP 被封禁，统一冷却 {ban_seconds:.0f} 秒")
                        import traceback
                        traceback.print_exc()
                        # 🔥 等待冷却后重试
                        self._wait_for_api_cooldown()
                        if i == retries - 1:
                            raise e
                        continue

                    # 普通 429：指数退避 + 随机抖动
                    base_wait = 15 * (i + 1)
                    jitter = random.uniform(0, 5)
                    wait_time = base_wait + jitter
                    print(f"🛑 [429限频] 休眠 {wait_time:.1f} 秒 (第 {i + 1} 次重试)...")
                    time.sleep(wait_time)
                    if i == retries - 1:
                        raise e
                    continue

                # 🔥 网络抖动/超时处理
                if isinstance(e, (ccxt.NetworkError, ccxt.ExchangeNotAvailable, ccxt.RequestTimeout)):
                    print(f"⚠️ 网络抖动/超时: {e}，正在第 {i + 1} 次重试...")
                    if i == retries - 1:
                        raise e
                    time.sleep(delay * (i + 1))
                    continue

                # 🔥 OrderNotFound：订单不存在（已被交易所清除/从未存在），重试无意义，直接抛出（S44）
                # 调用方据此区分"单子已丢失需补挂"与"网络抖动下轮重试"
                if isinstance(e, ccxt.OrderNotFound):
                    raise e

                # 🔥 交易所维护处理
                if isinstance(e, ccxt.ExchangeError):
                    if "system maintenance" in err_str or "503" in err_str:
                        print(f"🚧 交易所维护中，休眠 30 秒后重试...")
                        time.sleep(30)
                        if i == retries - 1:
                            raise e
                        continue
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
        with self._state_lock:
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
        with self._state_lock:
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
                    import traceback
                    print(f"⚠️ 清理批次状态失败: {e}")
                    traceback.print_exc()

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
        # 🔥 健康检查：确认交易所 API 可用
        print("🔍 [恢复前健康检查] 正在验证交易所连接...")
        try:
            self._safe_api_call(self.exchange.fetch_time)
            print("✅ [恢复前健康检查] 通过")
        except Exception as e:
            print(f"⚠️ [恢复中断] 交易所 API 不可用: {e}，等待 60 秒后重试")
            time.sleep(60)
            return False

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

                    # 🔥 验证止损单是否存在
                    if b_data.get('current_sl_id'):
                        try:
                            sl_order = self._safe_api_call(
                                self.exchange.fetch_order,
                                b_data['current_sl_id'],
                                symbol,
                                params={'stop': True}
                            )
                            valid_statuses = ['new', 'open', 'active']
                            status = sl_order.get('status', '').lower()
                            if status not in valid_statuses:
                                print(f"  └─ ⚠️ 止损单状态异常: {status}，将重新挂单")
                                b_data['current_sl_id'] = None
                            else:
                                print(f"  └─ ✅ 止损单验证通过: {b_data['current_sl_id']}")
                        except Exception as e:
                            print(f"  └─ ⚠️ 无法验证止损单: {e}，将重新挂单")
                            b_data['current_sl_id'] = None

                    # 🔥 清理可能残留的监控标记，然后启动监控线程
                    with self._active_monitors_lock:
                        if batch_id in self._active_monitors:
                            print(f"  └─ 🧹 清理残留监控标记: {batch_id}")
                            self._active_monitors.discard(batch_id)
                        self._active_monitors.add(batch_id)

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

    def _update_sl_no_validation(self, symbol: str, batch_id: str, b_data: dict, sl_price: float, mode: str = "",
                                 mark_modified: bool = True) -> tuple[bool, str]:
        """
        内部方法：直接更新止损，跳过价格校验（用于保本损）
        此方法不检查止损价是否合理，直接挂单
        mark_modified: 是否将 user_modified 置 True（§7.2 修复）
            - 手动路径（/be、/sl 等用户主动操作）：默认 True，保持存量行为
            - 自动路径（D-001 第 3K 自动保本等）：传 False，避免 KAMA 被误判为"手动修改"而暂停
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
            if mark_modified:
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

    def _get_active_batch_count(self) -> int:
        """获取当前活跃批次数量"""
        all_states = self.load_all_states()
        count = 0
        for symbol, symbol_batches in all_states.items():
            for b_id, b_data in symbol_batches.items():
                if b_data.get('is_active'):
                    count += 1
        return count

    def _calculate_monitoring_interval(self) -> float:
        """
        根据活跃批次数量动态计算轮询间隔
        4H级别交易：间隔更宽松，避免429
        """
        active_count = self._get_active_batch_count()

        if active_count <= 2:
            base_interval = 30.0
            jitter_range = 10.0
        elif active_count <= 4:
            base_interval = 45.0
            jitter_range = 15.0
        elif active_count <= 6:
            base_interval = 60.0
            jitter_range = 20.0
        else:
            base_interval = 90.0
            jitter_range = 30.0

        return random.uniform(base_interval, base_interval + jitter_range)

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
        只校验：做多时止损价 < 入场价，做空时止损价 > 入场价
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
                self.send_tg_notification(f"🚨 **挂单被阻断！**\n{msg}", level='critical')
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
            # 同一 symbol 只请求一次 ticker
            ticker = self._safe_api_call(self.exchange.fetch_ticker, symbol) if symbol_state else None
            current_price = 0.0
            if ticker:
                current_price = float(ticker.get('last') or ticker.get('close') or 0.0)
            for b_id, b_data in symbol_state.items():
                if b_data.get('is_active') and b_data.get('entry_orders'):
                    target_amounts = b_data.get('target_amounts', [])
                    leverage = b_data.get('params_base', {}).get('leverage', 100)
                    for amount in target_amounts:
                        if current_price > 0:
                            used_margin += (amount * current_price) / leverage

            print(f"💰 账户可用 USDT 余额: {usdt_free:.2f} USDT")
            print(f"📊 当前批次所需保证金: {total_required_margin:.2f} USDT")
            print(f"📊 已有活跃批次占用保证金: {used_margin:.2f} USDT")
            print(f"📊 总需求: {total_required_margin + used_margin:.2f} USDT")
            usage_rate = (total_required_margin + used_margin) / usdt_free * 100 if usdt_free > 0 else 0
            print(f"📊 保证金使用率: {usage_rate:.1f}%")

            if usdt_free < (total_required_margin + used_margin):
                print(
                    f"❌ 【余额不足阻断】账户可用余额 ({usdt_free:.2f} USDT) 不足以支付总需求 ({total_required_margin + used_margin:.2f} USDT)！")
                self.send_tg_notification(
                    f"🚨 **挂单被阻断！**\n"
                    f"❌ 账户可用余额 `{usdt_free:.2f}` USDT\n"
                    f"📊 当前批次需 `{total_required_margin:.2f}` USDT\n"
                    f"📊 已有批次占用 `{used_margin:.2f}` USDT\n"
                    f"📊 总需求 `{total_required_margin + used_margin:.2f}` USDT\n"
                    f"💡 请撤销部分挂单或增加保证金后再试。",
                    level='critical'
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
                'layer_sl_params': layer_sl_params,
                'layer_tp_params': layer_tp_params,
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

            # 🔥 启动监控线程（去重检查交给 _start_monitoring 自己处理）
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
                    'layer_sl_params': layer_sl_params,
                    'layer_tp_params': layer_tp_params,
                },
                daemon=True
            )
            monitor_thread.start()

            return batch_id

        except Exception as e:
            print(f"\n⚠️ 执行异常: {e}")
            import traceback
            traceback.print_exc()
            return None

    def _start_monitoring(self, symbol: str, batch_id: str, entry_orders: list, stop_steps: list,
                          take_profit_price: float,
                          current_sl_id: str, tp_order_id: str, batch_total_amount: float, target_amounts: list,
                          params_base: dict, is_hedge_mode: bool, side: str, last_filled_count: int = 0,
                          filled_details: list = None, total_entry_fee: float = 0.0,
                          pending_sl_orders: list = None,
                          prepared_tp_params: dict = None,
                          layer_sl_params: list = None,
                          layer_tp_params: list = None):

        # 🔥 检查并清理可能残留的监控标记
        with self._active_monitors_lock:
            if batch_id in self._active_monitors:
                print(f"  └─ ⚠️ 批次 [{batch_id}] 监控标记残留，自动清理 (当前监控集合: {self._active_monitors})")
                self._active_monitors.discard(batch_id)
            self._active_monitors.add(batch_id)
            print(f"👀 批次 [{batch_id}] 监控已注册 (活跃监控数: {len(self._active_monitors)})")

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
            if 'layer_sl_params' in latest_b_data:
                layer_sl_params = latest_b_data.get('layer_sl_params', [])
            if 'layer_tp_params' in latest_b_data:
                layer_tp_params = latest_b_data.get('layer_tp_params', [])

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
                # 🔥 根据活跃批次数量动态计算轮询间隔
                sleep_interval = self._calculate_monitoring_interval()
                if fast_poll_count > 0:
                    sleep_interval = min(sleep_interval, 3.0)
                    fast_poll_count -= 1

                time.sleep(sleep_interval)
                self._sync_time_if_needed()

                # 🔥 定期主动检测 IP（每 5 分钟）
                now = time.time()
                if now - self.last_ip_check_time > self.IP_CHECK_INTERVAL:
                    self._check_ip_periodically()
                    self.last_ip_check_time = now

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

                if manual_canceled_detected:
                    if batch_filled_count == 0:
                        # 无成交：全部撤单，终止批次
                        print(f"🚨 [批次终止] 本批次未建仓且开仓挂单被撤销，正在退出...")
                        self._cancel_remaining_entries(symbol, entry_orders, filled_layers)
                        self.clear_batch_state(symbol, batch_id)
                        self.send_tg_notification(
                            f"🧹 **[批次终止]** 批次 `{batch_id}` 在建仓前挂单已全撤，后台监控退出。")
                        break
                    else:
                        # 🔥 有已成交层：只取消未成交的挂单，保留已成交层继续监控
                        print(f"⚠️ [手动撤单] 批次 [{batch_id}] 已有 {batch_filled_count} 层成交，仅取消剩余挂单")

                        cancelled_count = 0
                        for idx, order_id in enumerate(entry_orders):
                            if not filled_layers[idx] and not canceled_layers[idx]:
                                try:
                                    self._safe_api_call(self.exchange.cancel_order, order_id, symbol,
                                                        params={'stop': True})
                                    canceled_layers[idx] = True
                                    cancelled_count += 1
                                    print(f"  └─ 已撤销第 {idx + 1} 层挂单: {order_id}")
                                except Exception as e:
                                    print(f"  └─ ⚠️ 撤销第 {idx + 1} 层挂单失败: {e}")

                        # 🔥 更新状态：移除已撤销的订单
                        latest_all = self.load_all_states()
                        latest_b_data = latest_all.get(symbol, {}).get(batch_id, {})
                        if latest_b_data:
                            # 只保留已成交的订单
                            remaining_orders = [entry_orders[i] for i in range(len(entry_orders)) if filled_layers[i]]
                            latest_b_data['entry_orders'] = remaining_orders
                            # 标记为程序主动操作，防止重复提醒
                            latest_b_data['is_programmatic_cancel'] = True
                            self.save_batch_state(symbol, batch_id, latest_b_data)

                        if cancelled_count > 0:
                            self.send_tg_notification(
                                f"🗑️ **[手动撤单处理]** 批次 `{batch_id}`\n"
                                f"📊 已成交 {batch_filled_count} 层，已取消 {cancelled_count} 层剩余挂单\n"
                                f"💡 已成交仓位继续运行止盈止损"
                            )

                        # 重置标记，防止重复触发
                        manual_canceled_detected = False

                # 🔥 程序撤单/pending_close 且无成交：退出监控（finally 块会清理状态）
                if batch_filled_count == 0:
                    latest_all_pc = self.load_all_states()
                    latest_b_data_pc = latest_all_pc.get(symbol, {}).get(batch_id, {})
                    if latest_b_data_pc and (latest_b_data_pc.get('is_programmatic_cancel', False) or
                                              latest_b_data_pc.get('pending_close', False)):
                        print(f"🚨 [批次终止] 本批次未建仓，程序撤单已完成，正在退出监控...")
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
                        # 🔥 A1/N8：持仓归零路径同样撤销限价平仓单（补全清理覆盖）
                        self._cancel_limit_close_order(symbol, batch_id)
                        self.clear_batch_state(symbol, batch_id)
                        break

                # ==================== 部分减仓检测（自动更新止盈止损单） ====================
                _fb_other = 0
                if current_actual_position is not None and has_entered_position and current_actual_position < batch_filled_amount:
                    _fb_states = self.load_all_states()
                    _fb_sym = _fb_states.get(symbol, {})
                    _fb_other = sum(1 for b, d in _fb_sym.items() if d.get('is_active', False) and b != batch_id)
                    if _fb_other > 0:
                        print(f"  ┏━ ⏭️ [多批次] 跳过部分减仓检测 (同symbol活跃批次: {_fb_other + 1})")

                if _fb_other == 0 and current_actual_position is not None and has_entered_position and current_actual_position < batch_filled_amount:
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

                        # 🔥 更新 batch_filled_amount 为新值
                        batch_filled_amount = new_amount

                        # 🔄 M1 修复：先挂新、再撤旧（消除空窗期；挂新失败则保留旧单+告警，下轮重试）
                        # 双单并存窗口极小；reduceOnly 触发最多平掉全部持仓，符合结算语义，无超量风险
                        if batch_filled_amount > 0:
                            # —— 先挂新止损单（不再受 current_sl_id is None 限制）——
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
                                new_sl_id = new_sl_order['id']
                                print(f"  └─ ✅ 新止损单已挂: {formatted_sl_price} (数量: {batch_filled_amount}, ID: {new_sl_id})")
                                # 再撤旧止损单（撤旧失败仅打日志；延迟清理机制由 D-001 §8 pending_cancel_sl_ids 接管）
                                if current_sl_id:
                                    try:
                                        self._safe_api_call(self.exchange.cancel_order, current_sl_id, symbol,
                                                            params={'stop': True})
                                        print(f"  └─ 已撤销旧止损单: {current_sl_id}")
                                    except Exception as e:
                                        if "Unknown order" in str(e) or "-2011" in str(e):
                                            print(f"  └─ 旧止损单 {current_sl_id} 已不存在")
                                        else:
                                            print(f"  └─ ⚠️ 撤销旧止损单失败（旧单可能仍在场）: {current_sl_id} ({e})")
                                current_sl_id = new_sl_id
                            except Exception as e:
                                # 挂新失败：保留旧单（保护仍在），告警，下轮重试
                                print(f"  └─ ❌ 更新止损单失败（旧单保留）: {e}")
                                self.send_tg_notification(
                                    f"⚠️ 部分减仓后止损单更新失败（旧单保留），批次 {batch_id} {symbol}",
                                    level='warning'
                                )

                            # —— 先挂新止盈单（不再受 tp_order_id is None 限制）——
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
                                new_tp_id = new_tp_order['id']
                                print(f"  └─ ✅ 新止盈单已挂: {formatted_tp_price} (数量: {batch_filled_amount}, ID: {new_tp_id})")
                                # 再撤旧止盈单（撤旧失败仅打日志；延迟清理机制由 D-001 §8 pending_cancel_sl_ids 接管）
                                if tp_order_id:
                                    try:
                                        self._safe_api_call(self.exchange.cancel_order, tp_order_id, symbol,
                                                            params={'stop': True})
                                        print(f"  └─ 已撤销旧止盈单: {tp_order_id}")
                                    except Exception as e:
                                        if "Unknown order" in str(e) or "-2011" in str(e):
                                            print(f"  └─ 旧止盈单 {tp_order_id} 已不存在")
                                        else:
                                            print(f"  └─ ⚠️ 撤销旧止盈单失败（旧单可能仍在场）: {tp_order_id} ({e})")
                                tp_order_id = new_tp_id
                            except Exception as e:
                                # 挂新失败：保留旧单（保护仍在），告警，下轮重试
                                print(f"  └─ ❌ 更新止盈单失败（旧单保留）: {e}")
                                self.send_tg_notification(
                                    f"⚠️ 部分减仓后止盈单更新失败（旧单保留），批次 {batch_id} {symbol}",
                                    level='warning'
                                )

                        # 🔥 清理无效的 pending_sl_orders（超过实际成交层数的）
                        pending_sl_orders = [idx for idx in pending_sl_orders if idx < batch_filled_count]
                        print(f"  └─ 📝 清理待挂列表: {pending_sl_orders}")

                        # 保存状态（M2 修复：增量更新，基于 latest_b_data 复制只写本段变化的字段，
                        # 避免整对象重建静默清空 D-001 未来新增的状态字段，如 KAMA/自动保本相关字段）
                        batch_state_data = latest_b_data.copy() if latest_b_data else {}
                        batch_state_data.update({
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
                            'layer_sl_params': layer_sl_params,
                            'layer_tp_params': layer_tp_params,
                            'sl_fail_count': sl_fail_count,
                        })
                        batch_state_data.setdefault('sl_failed_layers', [])
                        self.save_batch_state(symbol, batch_id, batch_state_data)
                        print(f"  └─ ✅ 状态已保存")

                # 更新 VWAP（如果持仓有变化）
                if current_actual_position is not None and has_entered_position and current_actual_position < batch_filled_amount:
                    # 🔥 检查同symbol是否有其他活跃批次
                    _check_states = self.load_all_states()
                    _symbol_state = _check_states.get(symbol, {})
                    _other_active = sum(1 for bid, bdata in _symbol_state.items()
                                        if bdata.get('is_active', False) and bid != batch_id)
                    if _other_active == 0:
                        # 单批次：总持仓 == 本批次持仓，可以安全更新
                        if current_actual_position == 0:
                            batch_filled_amount = 0.0
                        else:
                            batch_filled_amount = float(self.exchange.amount_to_precision(symbol, current_actual_position))
                    else:
                        # 多批次：总持仓 != 本批次持仓，跳过覆盖
                        print(f"  └─ ⏭️ [多批次] 跳过持仓量覆盖 (同symbol活跃批次: {_other_active + 1})")

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

                # 🔥 兜底：有持仓但无止损单时，触发恢复（覆盖重启后SL丢失场景）
                if not current_sl_id and has_entered_position and batch_filled_amount > 0:
                    need_recover_sl = True

                if current_sl_id and (str(current_sl_id) not in open_orders_map) and has_entered_position:
                    sl_id_str = str(current_sl_id)
                    if sl_id_str not in terminal_orders:
                        sl_status = None
                        try:
                            sl_detail = self._safe_api_call(self.exchange.fetch_order, current_sl_id, symbol,
                                                            retries=2, params={'stop': True})
                            sl_status = sl_detail.get('status')
                        except ccxt.OrderNotFound:
                            # S33/S44：单子已被交易所清除（不存在）→ 视同 canceled，走下方 canceled 分支
                            # （修复：旧代码任何异常都只"下轮重试"，单子被清除则永久卡死不补挂）
                            sl_status = 'canceled'
                            print(f"⚠️ [S33] 止损单 {sl_id_str} 不存在（OrderNotFound），视同已取消")
                        except Exception as e:
                            print(f"⚠️ 无法拉取止损单 {current_sl_id} 状态 ({e})，下轮重试...")
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

                    # 🔥 记录已实现盈亏 + 附带剩余持仓快照
                    self._record_realized_pnl(batch_id, symbol, side, batch_filled_amount,
                                              batch_entry_vwap, sl_exit_price, net_pnl, "止损")
                    self._notify_snapshot(batch_id)

                    self._cancel_remaining_entries(symbol, entry_orders, filled_layers)
                    if tp_order_id:
                        try:
                            self._safe_api_call(self.exchange.cancel_order, tp_order_id, symbol, params={'stop': True})
                        except Exception:
                            pass

                    # 🔥 A1：撤销限价平仓单，防孤儿单 + 幽灵线程
                    self._cancel_limit_close_order(symbol, batch_id)

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

                    # 🔥 记录已实现盈亏 + 附带剩余持仓快照
                    self._record_realized_pnl(batch_id, symbol, side, batch_filled_amount,
                                              batch_entry_vwap, tp_exit_price, net_pnl, "止盈")
                    self._notify_snapshot(batch_id)

                    self._cancel_remaining_entries(symbol, entry_orders, filled_layers)
                    if current_sl_id:
                        try:
                            self._safe_api_call(self.exchange.cancel_order, current_sl_id, symbol,
                                                params={'stop': True})
                        except Exception:
                            pass

                    # 🔥 A1/N8：TP 结算路径同样撤销限价平仓单（补全清理覆盖）
                    self._cancel_limit_close_order(symbol, batch_id)

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
                                    sl_error_count += 1
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
                                            f"💡 请立即手动检查持仓并设置止损！",
                                            level='critical'
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
                                                f"请立即手动检查持仓并设置止损！",
                                                level='critical'
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

                    # 保存状态（M3 修复：增量更新，基于已有状态复制只写本段变化的字段，
                    # 避免整对象重建静默清空 D-001 未来新增的状态字段，如 KAMA/自动保本相关字段）
                    batch_state_data = latest_b_data.copy() if latest_b_data else {}
                    batch_state_data.update({
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
                        'layer_sl_params': layer_sl_params,
                        'layer_tp_params': layer_tp_params,
                        'sl_fail_count': sl_fail_count,
                        'sl_failed_layers': sl_failed_layers,
                    })
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
            # 🔥 从活跃监控集合中移除
            with self._active_monitors_lock:
                self._active_monitors.discard(batch_id)
                print(f"👀 批次 [{batch_id}] 监控已移除 (剩余活跃监控数: {len(self._active_monitors)})")

            # 🔥 S32/A1：异常退出或程序撤单路径兜底撤销限价平仓单（防 finally clear 时残留孤儿）
            # 主路径已 clear 的批次这里 load 到空，自动跳过，不会重复撤销
            try:
                all_states_tmp = self.load_all_states()
                if all_states_tmp.get(symbol, {}).get(batch_id, {}):
                    self._cancel_limit_close_order(symbol, batch_id)
            except Exception:
                pass

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
                        f"⚠️ 程序将重试，请关注后续通知！",
                        level='critical'
                    )
            else:
                raw_sl_price = stop_steps[idx] if idx < len(stop_steps) else stop_steps[-1]
                formatted_sl_price = float(self.exchange.price_to_precision(symbol, raw_sl_price))
                sl_params = params_base.copy()
                sl_params['stopPrice'] = formatted_sl_price
                if not is_hedge_mode:
                    sl_params['reduceOnly'] = True
                # 从已有参数推导止损方向（side 不在方法参数中）
                if len(layer_sl_params) > 0:
                    sl_side = layer_sl_params[0]['side']
                elif params_base.get('positionSide') == 'LONG':
                    sl_side = 'sell'
                elif params_base.get('positionSide') == 'SHORT':
                    sl_side = 'buy'
                else:
                    print(f"  └─ ⚠️ 无法确定止损方向 (layer_sl_params 为空且无 positionSide)，跳过本层止损")
                    return
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
                        f"⚠️ 程序将重试，请关注后续通知！",
                        level='critical'
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

            # 🔥 A1：市价平仓前撤销限价平仓单（场景C：已挂限价单 → 用户 /close）
            self._cancel_limit_close_order(target_symbol, batch_id)

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

            # 🔥 记录已实现盈亏 + 附带剩余持仓快照
            self._record_realized_pnl(batch_id, target_symbol, side, current_filled_amount,
                                      avg_price, actual_price, actual_net_pnl, "市价平仓")
            self._notify_snapshot(batch_id)

            return True, result_msg

        except Exception as e:
            return False, f"❌ 市价平仓失败: {e}"

    # ==================== 新增：限价平仓（支持最优价和自定义价） ====================

    def _cancel_limit_close_order(self, symbol: str, batch_id: str) -> None:
        """撤销限价平仓单（所有结算/平仓路径 clear_batch_state 前调用）
        修复 A1：原代码只有限价单自身被取消（_monitor_limit_close）一条清理路径，
        SL 结算 / TP 结算 / 市价平仓 / 持仓归零 均不撤 limit_close_order_id →
        限价单孤儿（无持仓 reduceOnly 永不成交）+ _monitor_limit_close 幽灵线程。
        注意：调用点必须紧接着 clear_batch_state，状态字段由 clear 一并删除。
        """
        try:
            all_states = self.load_all_states()
            b_data = all_states.get(symbol, {}).get(batch_id, {})
            limit_id = b_data.get('limit_close_order_id') if b_data else None
            if limit_id:
                try:
                    self._safe_api_call(self.exchange.cancel_order, limit_id, symbol)
                    print(f"  └─ 已撤销限价平仓单: {limit_id}")
                except Exception as e:
                    if "Unknown order" in str(e) or "-2011" in str(e):
                        print(f"  └─ 限价平仓单 {limit_id} 已不存在，无需撤销")
                    else:
                        print(f"  └─ ⚠️ 撤销限价平仓单失败: {e}（限价单可能残留，持仓归零后将自动失效）")
        except Exception as e:
            print(f"  └─ ⚠️ 清理限价平仓单异常: {e}")

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
                # 🔥 N14：撤 TP 后必须清空 tp_order_id，否则主循环判定"TP丢失"
                # （is_programmatic_cancel 已置 True 不会误补挂，但残留 id 会导致每轮多余 fetch_order）
                target_b_data['tp_order_id'] = None

            # 挂限价平仓单
            close_side = 'sell' if side == 'BUY' else 'buy'
            order_params = target_b_data['params_base'].copy()
            if target_b_data.get('is_hedge_mode', False):
                order_params['positionSide'] = 'LONG' if side == 'BUY' else 'SHORT'
            else:
                order_params['reduceOnly'] = True

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

                # 🔥 N11：批次存活检查（防幽灵线程）
                # 场景：限价单挂着时 SL 触发 / 用户市价平仓 → 主循环已 clear_batch_state
                # → 本线程若无此检查将永久每 3s 轮询，持续占用全局 API 信号量
                try:
                    _alive_states = self.load_all_states()
                    _alive_b = _alive_states.get(symbol, {}).get(batch_id, {})
                except Exception:
                    _alive_b = {}
                if not _alive_b or not _alive_b.get('is_active', True):
                    print(f"🧹 [限价平仓监控] 批次 {batch_id} 已不存在，监控退出（防幽灵线程）")
                    break

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

                    # 🔥 记录已实现盈亏 + 附带剩余持仓快照
                    self._record_realized_pnl(batch_id, symbol, side, current_filled_amount,
                                              avg_price, actual_price, net_pnl, "限价平仓")
                    self._notify_snapshot(batch_id)
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