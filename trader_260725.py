# trader_260725.py
import json
import os
import random
import shutil
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
from telegram.error import BadRequest
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

# -1021 时间戳错误重同步冷却（秒）：窗口内不重复调 load_time_difference（P0-1，堵放大器 A）
TIME_SYNC_COOLDOWN = 60


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
                'recvWindow': 20000,  # P1-1: 10000→20000，减少网络抖动导致的 -1021
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
        self.IP_CHECK_INTERVAL = 600  # 10 分钟

        # R-B（事件3根因B）：运行期 registry 自愈重查周期 + 持续未确认升级告警阈值。
        # 原 _recheck_registry_self_heal 只在启动恢复调用一次 → NOT_CONFIRMED 永久卡死。
        # 主循环每 registry_self_heal_interval 秒重查一次；连续 self_heal_escalate_rounds 轮
        # 仍查不到 → critical 告警一次（L1 生命周期不变量：失败状态通知 + 人工接管入口）。
        self.registry_self_heal_interval = 30  # 秒
        self.self_heal_escalate_rounds = 10    # 连续未确认轮数（约 5 分钟）
        self._self_heal_unconfirmed_rounds = {}  # (symbol, batch_id, identity) → 连续未确认轮次
        # F4b（事件3通知风暴，2026-08-21）：进程启动时刻——自愈 MISMATCH 针对启动前历史条目
        # （updated_at < _process_start_ts）降级为 info 不告警：重启时的状态同步 ≠ 新资金风险。
        # 边界：升级告警（连续 10 轮 ≈5 分钟仍查不到）不降级——持续查不到是真实异常，须人工核实。
        self._process_start_ts = time.time()

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
        self._cooldown_active = False   # R1: 当前熔断周期是否已发过进入告警
        self._cooldown_gen = 0          # R1: 熔断周期代数，防跨周期解除通知错配
        self._ready = False                              # SG1: READY 门控，默认 Fail-Closed
        self._not_ready_reason = "启动恢复中（历史批次接管未完成）"  # SG1: 仅诊断展示，永不参与安全判断

        # 🔥 监控线程去重
        self._active_monitors = set()
        self._active_monitors_lock = threading.Lock()

        # 🔥 SG3-P1: 保护单无效告警节流（键=(batch_id, order_id, reason)，防告警风暴）
        self._sg3_alerted = set()

        # 🔥 运行时安全补丁：gate 拒绝告警去重（键=(identity, reason_cat)，最多3次TG）
        self._gate_alert_counts = {}
        self._gate_alert_lock = threading.Lock()

        # 🔥 运行时安全补丁 v2：TP 参数无效 critical 告警去重（键=batch_id，60 分钟窗口）
        self._tp_invalid_alerted = {}
        # 🔥 运行时安全补丁 v3（ChatGPT 终审 2026-08-20）：TP 补挂熔断告警去重（键=(batch_id, layer)，
        # 熔断持续期间仅 1 次 critical，成功挂出时清除 → 下次熔断可再提醒）
        self._tp_breaker_alerted = {}

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
        level: 'info' 普通通知 | 'warning' 需关注（自动加 ⚠️ 前缀） | 'critical' 资金安全告警（自动加 🚨 前缀 + 邮箱兜底）
        """
        if level == 'critical':
            text = f"🚨【资金安全】\n{text}"
            # 🔥 资金安全告警同步推送 QQ 邮箱（兜底通道，独立线程异步发送，失败不影响 TG）
            self._send_email_alert(text, subject="🚨 资金安全告警")
        elif level == 'warning':
            text = f"⚠️【需关注】\n{text}"
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
            except BadRequest as e:
                # 🔥 Markdown/Entity 解析失败等请求级错误（如批次号奇数下划线）→ 同一消息降级纯文本重发
                # 一次，保证告警必达（不变量⑧ Fail-not-Silent）。类型判定，不依赖错误文案。
                try:
                    future = asyncio.run_coroutine_threadsafe(
                        self.tg_bot.send_message(
                            chat_id=self.chat_id,
                            text=text,
                            reply_markup=reply_markup
                        ),
                        self.loop
                    )
                    future.result(timeout=5)
                    print(f"ℹ️ [TG通知] Markdown 解析失败({str(e)[:60]})，已降级纯文本发送")
                except Exception as e2:
                    print(f"⚠️ [TG通知] 纯文本重发失败: {e2}")
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

    def _wait_for_api_cooldown(self):
        """等待全局 API 熔断结束。
        运行时安全补丁：终端输出限频——进入时打印 1 次 + 每 60 秒 1 次进度提示，
        消除多线程同时等待时的终端刷屏放大器。"""
        _printed_enter = False
        _last_progress = 0.0
        while True:
            with self.api_cooldown_lock:
                wait_time = self.api_cooldown_until - time.time()

            if wait_time <= 0:
                break

            if not _printed_enter:
                _printed_enter = True
                _last_progress = time.time()
                print(f"🚫 [API熔断] 进入冷却，预计等待 {wait_time:.0f} 秒...")
            elif time.time() - _last_progress >= 60:
                _last_progress = time.time()
                print(f"🚫 [API熔断] 仍在冷却，剩余 {wait_time:.0f} 秒")
            time.sleep(min(wait_time, 5.0))

        # R1: 熔断解除通知（状态转换式，每个周期最多 1 条；多线程下仅 1 个线程能翻转 active）
        gen_snapshot = None
        with self.api_cooldown_lock:
            if self._cooldown_active and time.time() >= self.api_cooldown_until:
                self._cooldown_active = False
                gen_snapshot = self._cooldown_gen
        if gen_snapshot is not None:
            try:
                with self.api_cooldown_lock:
                    # 发送前二次确认：期间没有新熔断周期开始、冷却未被延长
                    still_valid = (self._cooldown_gen == gen_snapshot
                                   and time.time() >= self.api_cooldown_until)
                if still_valid:
                    self.send_tg_notification("✅ API 熔断已解除，恢复正常交易监控", level='info')
                else:
                    print("ℹ️ [R1] 跳过解除通知：已检测到新的熔断周期/冷却延长")
            except Exception as e:
                print(f"⚠️ [R1] 熔断解除通知发送失败: {e}")

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

    def _alert_cooldown_start(self, reason: str, seconds: float):
        """R1: 熔断进入告警（状态转换式，每个熔断周期只发一次 critical，防告警风暴）
        同一周期内再次 429/418 只延长冷却（调用方已 max() 更新），不重复告警"""
        with self.api_cooldown_lock:
            if self._cooldown_active:
                return  # 本周期已告警
            self._cooldown_active = True
            self._cooldown_gen += 1
        # 锁外构建与发送：active_cnt 走本地状态文件，不持熔断锁、不发交易所请求
        try:
            expire_str = datetime.fromtimestamp(time.time() + seconds, BEIJING_TZ).strftime('%H:%M:%S')
            active_cnt = 0
            try:
                # 状态结构: {symbol: {batch_id: batch_data}}，无包装键
                for state in self.load_all_states().values():
                    for b in state.values():
                        if isinstance(b, dict) and b.get('is_active'):
                            active_cnt += 1
            except Exception:
                active_cnt = -1
            cnt_str = str(active_cnt) if active_cnt >= 0 else '未知(状态读取失败)'
            self.send_tg_notification(
                f"API 全局熔断已触发\n原因: {reason}\n"
                f"预计冷却: {seconds:.0f} 秒（至北京时间 {expire_str}）\n"
                f"受影响活跃批次: {cnt_str} 个\n"
                f"熔断期间暂停新的交易所 API 请求；已存在于交易所的 SL/TP 条件单仍由交易所维护，"
                f"但程序暂时无法补挂、修改或撤销订单。",
                level='critical'
            )
        except Exception as e:
            print(f"⚠️ [R1] 熔断告警发送失败: {e}")

    def _gate_alert_notify(self, identity, gate_reason, msg, level='warning'):
        """运行时安全补丁：gate 拒绝告警去重——
        同一 identity + 同一拒绝类别，最多 3 次 TG；第 4 次起静默（print only）。
        状态变化时由 _assert_create_allowed 返回 True 自动清除计数（_gate_alert_clear）。"""
        if gate_reason.startswith('HARD_LOCK'):
            return  # 硬锁已由调用方静默处理
        # 提取拒绝类别
        reason_cat = 'cooldown' if 'cooldown' in gate_reason.lower() else 'unknown'
        for state_key in ('PENDING_CREATE', 'PENDING_VERIFY', 'NOT_CONFIRMED', 'CONFIRMED', 'MISMATCH', 'FAILED'):
            if state_key in gate_reason:
                reason_cat = state_key
                break
        key = (identity, reason_cat)
        with self._gate_alert_lock:
            count = self._gate_alert_counts.get(key, 0)
            if count >= 3:
                print(f"  └─ 🤫 [告警去重] `{identity}` ({reason_cat}) 已告警 {count} 次，本次静默")
                return
            self._gate_alert_counts[key] = count + 1
            current = count + 1
        suffix = f"（第{current}次/共3次）" if current < 3 else "（第3次/共3次，后续将静默）"
        try:
            self.send_tg_notification(msg + "\n📢 " + suffix, level=level)
        except Exception as e:
            print(f"⚠️ [告警去重] TG 发送失败: {e}")

    def _gate_alert_clear(self, identity):
        """运行时安全补丁：状态变化时清除该 identity 的告警计数（gate 通过时调用）"""
        with self._gate_alert_lock:
            keys_to_remove = [k for k in self._gate_alert_counts if k[0] == identity]
            for k in keys_to_remove:
                del self._gate_alert_counts[k]

    def _check_tp_viability(self, side, tp_price, cost_price, mark_price) -> tuple[bool, str]:
        """R2 成交后止盈价可行性校验（ChatGPT 终审 2026-08-20 v2 修正边界）：
        BUY: TP > 现价 且 TP >= 持仓成本；SELL: TP < 现价 且 TP <= 持仓成本。
        现价维度防币安 -2021（"Order would immediately trigger"判定基准是现价）；
        成本维度防无意义止盈（TP 低于成本）。TP == 成本 = 合法"保本退出"（放行）。
        返回 (valid, reason)。"""
        try:
            tp_price = float(tp_price)
            cost_price = float(cost_price or 0.0)
            mark_price = float(mark_price or 0.0)
        except (TypeError, ValueError):
            return False, f"止盈价/成本/现价存在非法值（TP={tp_price}, cost={cost_price}, mark={mark_price}）"
        if tp_price <= 0:
            return False, f"止盈价无效（{tp_price}）"
        if side == 'BUY':
            if tp_price <= mark_price or tp_price < cost_price:
                return False, (f"BUY 止盈价 {tp_price} 无效：需 > 现价 {mark_price}（防-2021 立即触发）"
                               f"且 >= 持仓成本 {cost_price}（允许保本退出）")
        else:
            if tp_price >= mark_price or tp_price > cost_price:
                return False, (f"SELL 止盈价 {tp_price} 无效：需 < 现价 {mark_price}（防-2021 立即触发）"
                               f"且 <= 持仓成本 {cost_price}（允许保本退出）")
        return True, ''

    def _mark_tp_param_invalid(self, symbol, batch_id, reason):
        """R2/R3: 止盈价确定性错误 → 写批次标记（补挂循环短路，不再打 Binance API）
        + critical 告警（60 分钟窗口去重，键=batch_id）。"""
        try:
            latest_all = self.load_all_states()
            b = latest_all.get(symbol, {}).get(batch_id)
            if b is not None:
                b['tp_param_invalid'] = {'reason': reason, 'ts': time.time()}
                self.save_batch_state(symbol, batch_id, b)
        except Exception as e:
            print(f"⚠️ [TP参数无效] 写标记失败: {e}")
        now = time.time()
        if now - self._tp_invalid_alerted.get(batch_id, 0) >= 3600:
            self._tp_invalid_alerted[batch_id] = now
            try:
                self.send_tg_notification(
                    f"🚨 **止盈价不合理，挂单被跳过！**\n"
                    f"🆔 批次：`{batch_id}`\n"
                    f"📌 {reason}\n"
                    f"💡 程序【不调用交易所】【不自动重试】。请用用户命令修正止盈价后，程序将自动恢复挂单。",
                    level='critical'
                )
            except Exception as e:
                print(f"⚠️ [TP参数无效] TG 发送失败: {e}")
        else:
            print(f"  └─ 🤫 [TP参数无效] 批次 {batch_id} 60 分钟内已告警，不重复")

    def _clear_tp_param_invalid(self, symbol, batch_id):
        """R2: 校验通过 → 清除批次标记（用户改价后自动恢复挂单）"""
        try:
            latest_all = self.load_all_states()
            b = latest_all.get(symbol, {}).get(batch_id)
            if b is not None and 'tp_param_invalid' in b:
                del b['tp_param_invalid']
                self.save_batch_state(symbol, batch_id, b)
        except Exception:
            pass

    def _tp_update_blocked(self, symbol, batch_id, side, layer, tp_price, cost_price,
                           mark_price=None, max_tp_fails=5) -> bool:
        """补挂止盈前综合预检（ChatGPT 终审 2026-08-20）：
        返回 True = 应跳过本轮止盈更新（不打 Binance create API）。三种短路：
          a) 熔断短路：tp_fail_count 连续确定失败 ≥ max_tp_fails（对称 SL 的 MAX_SL_FAILS_PER_LAYER）
          b) R2 可行性校验：BUY 需 TP > max(现价, 成本)；SELL 需 TP < min(现价, 成本)
             —— 失败 = 确定性错误，不打 API + critical（60min 去重）+ 写 tp_param_invalid 标记
          c) 已标记且仍不合理 → 静默跳过（不重复告警）；已标记但现合理（用户改价）→ 清标记放行
        关键：标记不短路校验（否则用户改价后永远无法自愈），只短路"告警与 create"。"""
        latest_all = self.load_all_states()
        b = latest_all.get(symbol, {}).get(batch_id)
        was_invalid = bool(b and b.get('tp_param_invalid'))
        if b:
            _tf = b.get('tp_fail_count') or {}
            if _tf.get(str(layer), 0) >= max_tp_fails:
                print(f"  └─ 🔥 [熔断保护] 批次 {batch_id} 第 {layer + 1} 层止盈单连续失败 ≥{max_tp_fails} 次，跳过自动重试")
                # ChatGPT 终审（2026-08-20）：熔断时 1 次 critical（此后静默；
                # 成功挂出时清 key 恢复 → 下次熔断可再提醒）。用户改价/成功恢复前不再打扰。
                _bk = (batch_id, layer)
                if _bk not in self._tp_breaker_alerted:
                    self._tp_breaker_alerted[_bk] = time.time()
                    try:
                        self.send_tg_notification(
                            f"🚨 **止盈补挂已熔断！**\n"
                            f"🆔 批次：`{batch_id}`\n"
                            f"📊 第 {layer + 1} 层\n"
                            f"⚠️ 该层止盈单连续失败 ≥{max_tp_fails} 次，程序已停止自动重试\n"
                            f"💡 请人工核查 TP 参数/持仓。修正后程序将自动恢复（成功挂出后熔断自动解除）",
                            level='critical'
                        )
                    except Exception as e:
                        print(f"⚠️ [TP熔断] TG 发送失败: {e}")
                return True
        if mark_price is None:
            try:
                ticker = self._safe_api_call(self.exchange.fetch_ticker, symbol)
                mark_price = float(ticker.get('last') or ticker.get('close') or 0.0)
            except Exception:
                mark_price = 0.0
        valid, reason = self._check_tp_viability(side, tp_price, cost_price, mark_price)
        if not valid:
            if was_invalid:
                print(f"  └─ 🤫 [TP参数无效] 批次 {batch_id} 止盈价仍不合理，静默跳过挂单（已有标记，等待用户修正）")
                return True
            print(f"  └─ ❌ [R2 预检] 止盈更新被跳过: {reason}")
            self._mark_tp_param_invalid(symbol, batch_id, reason)
            return True
        self._clear_tp_param_invalid(symbol, batch_id)
        return False

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
                    # 🔥 重同步加冷却：TIME_SYNC_COOLDOWN 秒窗口内不重复调 load_time_difference（P0-1）
                    if time.time() - self.last_time_sync > TIME_SYNC_COOLDOWN:
                        try:
                            # R6: 收编信号量限速保护。不套 _safe_api_call——此处位于其 except 分支内，
                            # 嵌套调用在 sync 自身再抛 -1021 时有递归风险；重试职责由外层循环 continue 承担
                            with self._api_semaphore:
                                self.exchange.load_time_difference()
                            self.last_time_sync = time.time()
                            print(f"🔄 [时间同步] 已重新同步服务器时间")
                            time.sleep(2)  # 等待同步生效
                        except Exception as sync_e:
                            print(f"⚠️ 时间同步失败: {sync_e}")
                    else:
                        print(f"🔄 [时间同步] 冷却期内跳过重同步（上次同步 {time.time() - self.last_time_sync:.0f} 秒前）")
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
                        self._alert_cooldown_start("IP 被 Binance 封禁(418/banned)", ban_seconds)  # R1
                        import traceback
                        traceback.print_exc()
                        # 🔥 等待冷却后重试
                        self._wait_for_api_cooldown()
                        if i == retries - 1:
                            raise e
                        continue

                    # 普通 429：触发全局熔断，所有线程一起降速（P0-2，堵恶化器 C）
                    global_cooldown = 30 + random.uniform(0, 30)  # 30-60 秒全局冷却
                    with self.api_cooldown_lock:
                        self.api_cooldown_until = max(
                            self.api_cooldown_until,
                            time.time() + global_cooldown
                        )
                    print(f"🛑 [429限频] 触发全局熔断 {global_cooldown:.1f} 秒 (第 {i + 1} 次重试)...")
                    self._alert_cooldown_start("429 限频", global_cooldown)  # R1
                    self._wait_for_api_cooldown()
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

    def _persist_states(self, all_states: dict) -> None:
        """R12: 状态持久化唯一入口（调用方必须已持有 _state_lock）。
        备份 last-known-good 到 .bak 后原子写入新状态。
        边界：首次保存无文件则跳过备份；备份失败仅警告绝不阻断主保存
        （C3 是恢复纵深，不改变既有保存契约）。"""
        try:
            if os.path.exists(STATE_FILE):
                shutil.copy2(STATE_FILE, STATE_FILE + '.bak')
        except Exception as bak_e:
            print(f"⚠️ [R12] 状态备份失败（不阻断保存）: {bak_e}")
        dir_name = os.path.dirname(STATE_FILE) or "."
        try:
            with tempfile.NamedTemporaryFile("w", dir=dir_name, delete=False, encoding="utf-8") as tf:
                json.dump(all_states, tf, indent=4, ensure_ascii=False)
                temp_name = tf.name
            os.replace(temp_name, STATE_FILE)
        except Exception as e:
            print(f"⚠️ 保存状态文件失败: {e}")

    def save_batch_state(self, symbol: str, batch_id: str, batch_data: dict):
        with self._state_lock:
            all_states = self.load_all_states()
            if symbol not in all_states:
                all_states[symbol] = {}
            all_states[symbol][batch_id] = batch_data
            self._persist_states(all_states)

    def clear_batch_state(self, symbol: str, batch_id: str):
        if getattr(self, '_tp_breaker_alerted', None):  # 终态清理熔断告警键（ChatGPT 终审 2026-08-20，长期运行内存管理）
            self._tp_breaker_alerted = {k: v for k, v in self._tp_breaker_alerted.items() if k[0] != batch_id}
        with self._state_lock:
            all_states = self.load_all_states()
            if symbol in all_states and batch_id in all_states[symbol]:
                del all_states[symbol][batch_id]
                if not all_states[symbol]:
                    del all_states[symbol]
                self._persist_states(all_states)
                print(f"🧹 批次 [{batch_id}] 状态归档/清理完毕。")

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
        recovered_count = 0  # R3-v2: 计数器与成败分离，返回值只表达"流程成功/失败"
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
                        current_pos = None  # R11: UNKNOWN ≠ EMPTY，查询失败不得当作无持仓

                    has_position = current_pos is not None and current_pos > 0

                    # 🔥 如果既没有挂单也没有持仓(已确认)，清理这个批次
                    if not has_pending_orders and not has_position and current_pos is not None:
                        # B2-5 恢复护栏：骨架批次（entry_orders=[] 但 registry 有未决 ENTRY，
                        # 如开仓前置落盘后崩溃）→ 旧行为会误清理毁证据；B2-6 升级为对账自愈：
                        #   无 ID（PENDING_CREATE/PENDING_VERIFY id_unknown）→ §6.3 身份签名匹配
                        #   有 ID（PENDING_VERIFY/NOT_CONFIRMED）→ verify 自愈
                        #   匹配收编 CONFIRMED → 重建 entry_orders → 正常接管监控（禁止补挂任何层）
                        #   仍无法确认 → 保留证据待人工对账，不清理不接管
                        if self._registry_has_unresolved_entries(b_data):
                            print(f"  └─ ⚠️ 批次 [{batch_id}] 存在未决 ENTRY registry 记录"
                                  f"（开仓骨架/崩溃窗口），尝试身份匹配自愈对账...")
                            try:
                                self._self_heal_no_id(symbol, batch_id)
                                self._recheck_registry_self_heal(symbol, batch_id)
                                rebuilt_orders, _rebuilt = self._rebuild_entry_orders_from_registry(
                                    symbol, batch_id)
                            except Exception as e:
                                rebuilt_orders, _rebuilt = [], False
                                print(f"  └─ ⚠️ 批次 [{batch_id}] 自愈对账异常: {e}")
                            if _rebuilt and rebuilt_orders:
                                # 收编成功 → 刷新本地视图，落入正常接管路径（下方"有挂单或持仓"）
                                b_data = self.load_all_states().get(symbol, {}).get(batch_id, {})
                                entry_orders = b_data.get('entry_orders', [])
                                last_filled_count = b_data.get('last_filled_count', 0)
                                has_pending_orders = len(entry_orders) > last_filled_count
                                print(f"  └─ ✅ 批次 [{batch_id}] 身份匹配收编 "
                                      f"{len(rebuilt_orders)} 层 ENTRY（零二次 Create），正常接管监控")
                            else:
                                print(f"  └─ ⚠️ 批次 [{batch_id}] 未决 ENTRY 无法确认"
                                      f"（无匹配单/快照失败），保留证据待人工对账，不清理不接管")
                                continue
                        else:
                            print(f"  └─ 🧹 批次 [{batch_id}] 无挂单且无持仓，自动清理")
                            stale_batches.append((symbol, batch_id))
                            continue
                    elif current_pos is None:
                        print(f"  └─ ⚠️ 批次 [{batch_id}] 持仓查询失败(UNKNOWN)，保留批次不清理")

                    # 有挂单或持仓，正常恢复
                    print(f"  └─ ✅ 批次 [{batch_id}] 有效，正在接管监控...")
                    recovered_count += 1

                    try:
                        leverage = b_data.get('params_base', {}).get('leverage', 100)
                        self._safe_api_call(self.exchange.set_leverage, leverage, symbol)
                        print(f"  └─ ✅ 杠杆已重新设置为: {leverage}x")
                    except Exception as e:
                        print(f"  └─ ⚠️ 设置杠杆失败: {e}")

                    # 🔥 验证止损单是否存在
                    # P0-F2 前置修复（ChatGPT 终审前置问题2）：UNKNOWN ≠ None
                    #   旧代码 except Exception → current_sl_id=None → 监控线程误判"无SL"
                    #   → 进入补挂链 → 双SL风险。修复：区分 OrderNotFound(确实不存在→清None)
                    #   vs NetworkError/其他(UNKNOWN→保留ID+critical告警+不修改)
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
                        except ccxt.OrderNotFound:
                            # 订单确实不存在（已撤销/已成交/已过期）→ 安全清除
                            print(f"  └─ ℹ️ 止损单 {b_data['current_sl_id']} 已不存在(已撤销/成交)，清除 ID")
                            b_data['current_sl_id'] = None
                        except Exception as e:
                            # UNKNOWN：网络异常等 → 保留 current_sl_id，不转为 None
                            # 防止监控线程误判"无SL"→ 补挂链 → 双SL 风险（UNKNOWN ≠ EMPTY）
                            print(f"  └─ ⚠️ 止损单验证失败(UNKNOWN): {e}，保留 SL ID 不清除")
                            self.send_tg_notification(
                                f"🚨【资金安全】止损单验证失败(UNKNOWN)\n"
                                f"标的: `{symbol}` | 批次: `{batch_id}`\n"
                                f"SL ID: `{b_data['current_sl_id']}`\n"
                                f"错误: {e}\n"
                                f"⚠️ 保留 SL ID 未清除，监控不会自动补挂。请人工核实。",
                                level='critical')
                            # 不修改 b_data['current_sl_id'] — 保持 UNKNOWN 语义

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

        print(f"✅ [状态恢复] 恢复流程完成，共接管 {recovered_count} 个历史活跃批次")
        return True  # R3-v2: 返回值表达"流程成功/失败"而非"是否恢复过批次"；唯一失败路径=健康检查不通过(已提前 return False)

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

        # B2-8（§5.7 #1）：用户改 TP 纳入仲裁——identity = TP/L{主层}/{持仓方向}
        tp_side_ident = 'LONG' if side == 'BUY' else 'SHORT'
        tp_layer = max(0, (int(target_b_data.get('last_filled_count', 0) or 0) - 1))
        tp_identity = self._protection_identity(batch_id, 'TP', tp_layer, tp_side_ident)

        old_tp_id = target_b_data.get('tp_order_id')
        if old_tp_id:
            try:
                self._safe_api_call(self.exchange.cancel_order, old_tp_id, target_symbol, params={'stop': True})
                # B2-8: 撤旧成功 = 程序确认旧单已取消 → 旧 identity 置 ABSENT（gate 放行重建）
                self._update_registry(target_symbol, batch_id, tp_identity, state='ABSENT')
            except Exception as e:
                if "Unknown order" in str(e) or "-2011" in str(e):
                    print(f"ℹ️ 旧止盈单 {old_tp_id} 已不存在，跳过撤销")
                    # B2-8: 确认不存在同样置 ABSENT（gate 放行重建）
                    self._update_registry(target_symbol, batch_id, tp_identity, state='ABSENT')
                else:
                    print(f"⚠️ 撤销旧止盈单失败: {e}")
                    return False, f"❌ 撤销旧止盈单失败: {e}"

        tp_params = target_b_data['params_base'].copy()
        tp_params['stopPrice'] = formatted_tp_price
        if not target_b_data['is_hedge_mode']:
            tp_params['reduceOnly'] = True

        tp_side = 'sell' if side == 'BUY' else 'buy'

        try:
            # B2-8: Create 仲裁闸门（撤旧后 registry=ABSENT → 放行；未决/硬锁 → 拒绝）
            allowed, gate_reason = self._assert_create_allowed(
                target_symbol, batch_id, tp_identity, desc='用户修改止盈')
            if not allowed:
                print(f"  └─ 🚫 [仲裁] 跳过用户改止盈: {gate_reason}")
                return False, f"🚫 止盈单创建被仲裁拦截：{gate_reason}"
            # B2-2: 意图先落盘（崩溃安全 Create）+ intent 指纹
            self._update_registry(target_symbol, batch_id, tp_identity, state='PENDING_CREATE',
                                  id_known=False, order_kind='conditional', role='TP',
                                  layer=tp_layer, side=tp_side_ident,
                                  intent=self._build_intent(
                                      symbol=target_symbol, side=tp_side,
                                      qty=current_filled_amount,
                                      order_type='TAKE_PROFIT_MARKET',
                                      stop_price=formatted_tp_price,
                                      reduce_only=tp_params.get('reduceOnly')))
            new_tp_order = self._safe_api_call(
                self.exchange.create_order,
                symbol=target_symbol,
                type='TAKE_PROFIT_MARKET',
                side=tp_side,
                amount=current_filled_amount,
                params=tp_params,
                retries=1
            )
            # B2-0 Verify 统一入口：success→CONFIRMED；not_found→NOT_CONFIRMED；unknown→PENDING_VERIFY
            verify_result = self._verify_and_update_registry(
                target_symbol, batch_id, tp_identity, new_tp_order['id'], desc='用户修改止盈')
            if verify_result != 'success':
                self.send_tg_notification(
                    self._verify_failure_msg("新止盈单", new_tp_order['id'], target_symbol, verify_result),
                    level='critical' if verify_result == 'unknown' else 'warning')
                return False, f"❌ 新止盈单创建验证失败({verify_result})，未记录订单"
            new_tp_id = new_tp_order['id']

            # B2-8: 重新加载最新状态合并（防旧快照覆盖 registry 的 ABSENT/CONFIRMED 更新）
            latest_b = self.load_all_states().get(target_symbol, {}).get(batch_id, {})
            if latest_b:
                target_b_data.update(latest_b)
            # ChatGPT 终审（2026-08-20）：人工修改止盈成功 = 真正恢复 → 恢复 FAILED 告警额度
            self._gate_alert_clear(tp_identity)
            target_b_data['take_profit_price'] = formatted_tp_price
            target_b_data['tp_order_id'] = new_tp_id
            target_b_data['user_modified'] = True
            # 第二轮审查（2026-08-21）：成功改价 = 参数已恢复有效 → 清 tp_param_invalid 脏标记
            # （此前 _clear 只在自动补挂预检调用，用户命令路径从未清理 → 状态文件长期带脏标记）
            self._clear_tp_param_invalid(target_symbol, batch_id)
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
        # 🔥 修复漏洞4：ticker 异常返回空时市价为 0，后续方向性校验失效
        if current_mark_price <= 0:
            return False, f"❌ 获取市价异常（返回 {current_mark_price}），无法校验止损价方向"

        if side == 'BUY':
            if formatted_sl_price >= current_mark_price:
                return False, f"❌ 校验拒绝：新止损价 (`{formatted_sl_price}`) 不得高于或等于当前市价 (`{current_mark_price}`)，否则会立即触发！"
        else:
            if formatted_sl_price <= current_mark_price:
                return False, f"❌ 校验拒绝：新止损价 (`{formatted_sl_price}`) 不得低于或等于当前市价 (`{current_mark_price}`)，否则会立即触发！"

        # B2-8（§5.7 #2）：用户改 SL 纳入仲裁——identity = SL/L{主层}/{持仓方向}
        sl_side_ident = 'LONG' if side == 'BUY' else 'SHORT'
        sl_layer = max(0, (int(target_b_data.get('last_filled_count', 0) or 0) - 1))
        sl_identity = self._protection_identity(batch_id, 'SL', sl_layer, sl_side_ident)

        old_sl_id = target_b_data.get('current_sl_id')
        if old_sl_id:
            try:
                self._safe_api_call(self.exchange.cancel_order, old_sl_id, target_symbol, params={'stop': True})
                # B2-8: 撤旧成功 = 程序确认旧单已取消 → 旧 identity 置 ABSENT（gate 放行重建）
                self._update_registry(target_symbol, batch_id, sl_identity, state='ABSENT')
            except Exception as e:
                if "Unknown order" in str(e) or "-2011" in str(e):
                    print(f"ℹ️ 旧止损单 {old_sl_id} 已不存在，跳过撤销")
                    # B2-8: 确认不存在同样置 ABSENT（gate 放行重建）
                    self._update_registry(target_symbol, batch_id, sl_identity, state='ABSENT')
                else:
                    print(f"⚠️ 撤销旧止损单失败: {e}")
                    return False, f"❌ 撤销旧止损单失败: {e}"

        sl_params = target_b_data['params_base'].copy()
        sl_params['stopPrice'] = formatted_sl_price
        if not target_b_data['is_hedge_mode']:
            sl_params['reduceOnly'] = True

        sl_side = 'sell' if side == 'BUY' else 'buy'

        try:
            # B2-8: Create 仲裁闸门（撤旧后 registry=ABSENT → 放行；未决/硬锁 → 拒绝）
            allowed, gate_reason = self._assert_create_allowed(
                target_symbol, batch_id, sl_identity, desc='用户修改止损')
            if not allowed:
                print(f"  └─ 🚫 [仲裁] 跳过用户改止损: {gate_reason}")
                return False, f"🚫 止损单创建被仲裁拦截：{gate_reason}"
            # B2-2: 意图先落盘（崩溃安全 Create）+ intent 指纹
            self._update_registry(target_symbol, batch_id, sl_identity, state='PENDING_CREATE',
                                  id_known=False, order_kind='conditional', role='SL',
                                  layer=sl_layer, side=sl_side_ident,
                                  intent=self._build_intent(
                                      symbol=target_symbol, side=sl_side,
                                      qty=current_filled_amount,
                                      order_type='STOP_MARKET',
                                      stop_price=formatted_sl_price,
                                      reduce_only=sl_params.get('reduceOnly')))
            new_sl_order = self._safe_api_call(
                self.exchange.create_order,
                symbol=target_symbol,
                type='STOP_MARKET',
                side=sl_side,
                amount=current_filled_amount,
                params=sl_params,
                retries=1
            )
            # B2-0 Verify 统一入口：success→CONFIRMED；not_found→NOT_CONFIRMED；unknown→PENDING_VERIFY
            verify_result = self._verify_and_update_registry(
                target_symbol, batch_id, sl_identity, new_sl_order['id'], desc='用户修改止损')
            if verify_result != 'success':
                self.send_tg_notification(
                    self._verify_failure_msg("新止损单", new_sl_order['id'], target_symbol, verify_result),
                    level='critical' if verify_result == 'unknown' else 'warning')
                return False, f"❌ 新止损单创建验证失败({verify_result})，未记录订单"
            new_sl_id = new_sl_order['id']

            # B2-8: 重新加载最新状态合并（防旧快照覆盖 registry 的 ABSENT/CONFIRMED 更新）
            latest_b = self.load_all_states().get(target_symbol, {}).get(batch_id, {})
            if latest_b:
                target_b_data.update(latest_b)
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
                self.send_tg_notification(error_msg, level='warning')
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
                self.send_tg_notification(error_msg, level='warning')
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

        # B2-8（§5.7 #3）：保本损换挂纳入仲裁——identity = SL/L{主层}/{持仓方向}
        sl_side_ident = 'LONG' if side == 'BUY' else 'SHORT'
        sl_layer = max(0, (int(b_data.get('last_filled_count', 0) or 0) - 1))
        sl_identity = self._protection_identity(batch_id, 'SL', sl_layer, sl_side_ident)

        try:
            # 撤销旧止损单
            old_sl_id = b_data.get('current_sl_id')
            if old_sl_id:
                try:
                    self._safe_api_call(self.exchange.cancel_order, old_sl_id, symbol, params={'stop': True})
                    print(f"  └─ 已撤销旧止损单: {old_sl_id}")
                    # B2-8: 撤旧成功 = 程序确认旧单已取消 → 旧 identity 置 ABSENT（gate 放行重建）
                    self._update_registry(symbol, batch_id, sl_identity, state='ABSENT')
                except Exception as e:
                    if "Unknown order" in str(e) or "-2011" in str(e):
                        print(f"  └─ 旧止损单 {old_sl_id} 已不存在，跳过")
                        # B2-8: 确认不存在同样置 ABSENT（gate 放行重建）
                        self._update_registry(symbol, batch_id, sl_identity, state='ABSENT')
                    else:
                        # B2-8: 撤旧异常阻断——先撤后挂语义，撤旧失败绝不 create（双单窗口封死）
                        print(f"  └─ ❌ 撤销旧止损单失败，阻断保本损重建: {e}")
                        return False, f"❌ 撤销旧止损单失败（保本损重建已阻断，避免双单）: {e}"

            # B2-8: Create 仲裁闸门（撤旧后 registry=ABSENT → 放行；未决/硬锁 → 拒绝）
            allowed, gate_reason = self._assert_create_allowed(symbol, batch_id, sl_identity, desc='保本损')
            if not allowed:
                print(f"  └─ 🚫 [仲裁] 跳过保本损: {gate_reason}")
                return False, f"🚫 保本损止损单创建被仲裁拦截：{gate_reason}"
            # B2-2: 意图先落盘（崩溃安全 Create）+ intent 指纹
            self._update_registry(symbol, batch_id, sl_identity, state='PENDING_CREATE',
                                  id_known=False, order_kind='conditional', role='SL',
                                  layer=sl_layer, side=sl_side_ident,
                                  intent=self._build_intent(
                                      symbol=symbol, side=sl_side,
                                      qty=current_filled_amount,
                                      order_type='STOP_MARKET',
                                      stop_price=formatted_sl_price,
                                      reduce_only=sl_params.get('reduceOnly')))

            # 创建新止损单
            new_sl_order = self._safe_api_call(
                self.exchange.create_order,
                symbol=symbol,
                type='STOP_MARKET',
                side=sl_side,
                amount=current_filled_amount,
                params=sl_params,
                retries=1
            )
            # B2-0 Verify 统一入口：success→CONFIRMED；not_found→NOT_CONFIRMED；unknown→PENDING_VERIFY
            verify_result = self._verify_and_update_registry(
                symbol, batch_id, sl_identity, new_sl_order['id'], desc='保本损')
            if verify_result != 'success':
                self.send_tg_notification(
                    self._verify_failure_msg("保本损止损单", new_sl_order['id'], symbol, verify_result),
                    level='critical' if verify_result == 'unknown' else 'warning')
                return False, f"❌ 保本损止损单创建验证失败({verify_result})，未记录订单"
            new_sl_id = new_sl_order['id']

            # B2-8: 重新加载最新状态合并（防旧快照覆盖 registry 的 ABSENT/CONFIRMED 更新）
            latest_b = self.load_all_states().get(symbol, {}).get(batch_id, {})
            if latest_b:
                b_data.update(latest_b)

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
        if not success:
            return None
        # 从状态中读取保本损价格（set_breakeven_sl 已更新 stop_steps）
        all_states = self.load_all_states()
        for symbol, symbol_batches in all_states.items():
            if batch_id in symbol_batches and symbol_batches[batch_id].get('is_active'):
                b_data = symbol_batches[batch_id]
                stop_steps = b_data.get('stop_steps', [])
                last_filled_count = b_data.get('last_filled_count', 0)
                if stop_steps and last_filled_count > 0:
                    return stop_steps[last_filled_count - 1]
                elif stop_steps:
                    return stop_steps[-1]
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

        # P1-2: 基准间隔整体上调（4H 级别交易无需 30 秒轮询），减少 API 权重消耗
        if active_count <= 2:
            base_interval = 60.0
            jitter_range = 20.0
        elif active_count <= 4:
            base_interval = 75.0
            jitter_range = 25.0
        elif active_count <= 6:
            base_interval = 90.0
            jitter_range = 30.0
        else:
            base_interval = 120.0
            jitter_range = 40.0

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

    def _check_sl_coverage(self, symbol: str, all_states: dict, current_pos: float) -> tuple[bool, str]:
        """SG2: 加仓前风险闸门——任何已有仓位，只要系统无法证明其全部由有效 SL 覆盖，
        就禁止创建新的风险仓位（Fail-Closed）。
        - 只做判定不发通知（通知由调用方负责）；current_pos 由调用方单次快照传入，避免二次查询状态漂移
        - 有效 SL = current_sl_id 存在且该 id 在交易所 open_orders 中；查询失败 = UNKNOWN = 拒绝
        - 未成交批次（last_filled_count=0）无 SL 不算裸仓
        - delta≠0（含负值，名义台账与交易所不一致）一律拒绝
        返回 (allowed, reason)。"""
        EPS = 1e-9
        # ① 程序台账：已成交 active 批次的名义仓位与 SL id
        program_position = 0.0
        filled_batches = []  # [(batch_id, sl_id)]
        for batch_id, b_data in (all_states.get(symbol, {}) or {}).items():
            if not (isinstance(b_data, dict) and b_data.get('is_active')):
                continue
            last_filled = int(b_data.get('last_filled_count', 0) or 0)
            if last_filled <= 0:
                continue  # 未成交批次无需 SL
            target_amounts = b_data.get('target_amounts', []) or []
            program_position += sum(target_amounts[:last_filled])
            filled_batches.append((batch_id, b_data.get('current_sl_id')))

        # ② 差额判定（方案 A 严格）：正差=未归属手工仓位，负差=台账与交易所不一致
        # 注意：未归属仓位即使手工设置了 SL 也无法确认归属（有 SL ≠ delta 归零），指引应为平仓而非设 SL
        delta = current_pos - program_position
        if delta > EPS:
            return False, (f"存在未归属仓位 {delta:.6f}"
                           f"（交易所仓位 {current_pos:.6f} > 程序台账 {program_position:.6f}，"
                           f"未纳入程序批次管理）")
        if delta < -EPS:
            return False, (f"仓位与程序台账不一致"
                           f"（台账 {program_position:.6f} > 交易所 {current_pos:.6f}），"
                           f"无法确认保护状态，请人工核对")

        # ③ SL 有效性校验：一次 fetch_open_orders，失败 = UNKNOWN = 拒绝
        try:
            open_orders = self._safe_api_call(self.exchange.fetch_open_orders, symbol)
        except Exception as e:
            return False, f"SL 状态查询失败（{str(e)[:80]}），无法确认保护状态"
        open_ids = {str(o.get('id')) for o in open_orders}
        missing = [bid for bid, sl_id in filled_batches
                   if not sl_id or str(sl_id) not in open_ids]
        if missing:
            return False, f"批次 {', '.join(missing)} 缺少有效止损单（无 SL 或已被交易所撤除）"
        return True, ""

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

        # P0-F1 前置修复（ChatGPT 终审前置问题1）：双通道扫描 + Fail-Closed
        #   单通道 fetch_open_orders 看不到 algo 条件单（SL/TP），异常时 return False（放行）→ Fail-Open
        #   修复：normal + stop=True 双通道合并去重，任一通道异常 → return True（阻断开仓）+ critical
        try:
            normal_orders = self._safe_api_call(self.exchange.fetch_open_orders, symbol)
        except Exception as e:
            print(f"⚠️ 获取未结订单(普通通道)失败: {e}")
            self.send_tg_notification(
                f"🚨【资金安全】防冲突扫描失败(普通通道)，已阻断开仓\n"
                f"标的: `{symbol}`\n错误: {e}",
                level='critical')
            return True  # Fail-Closed：无法确认安全 → 阻断

        try:
            stop_orders = self._safe_api_call(
                self.exchange.fetch_open_orders, symbol, params={'stop': True})
        except Exception as e:
            print(f"⚠️ 获取未结订单(条件单通道)失败: {e}")
            self.send_tg_notification(
                f"🚨【资金安全】防冲突扫描失败(条件单通道)，已阻断开仓\n"
                f"标的: `{symbol}`\n错误: {e}",
                level='critical')
            return True  # Fail-Closed

        # 合并双通道结果，按订单 ID 去重
        open_orders = {}
        for ord in normal_orders + stop_orders:
            open_orders[str(ord['id'])] = ord
        open_orders = list(open_orders.values())

        unknown_orders = []
        for ord in open_orders:
            ord_id = str(ord['id'])
            if ord_id not in known_order_ids:
                unknown_orders.append(ord)

        if unknown_orders:
            print(f"⚠️ 【未识别挂单提醒】检测到交易所存在 {len(unknown_orders)} 个不受代码管理的“孤儿挂单”！")
            self.send_tg_notification(
                f"⚠️ **孤儿挂单检测**\n"
                f"🆔 标的：`{symbol}`\n"
                f"🔢 发现 {len(unknown_orders)} 个不受代码管理的挂单\n"
                f"🧹 程序将自动清理这些孤儿挂单。",
                level='warning'
            )
            for ord in unknown_orders:
                print(
                    f"   └─ Order ID: {ord['id']} | 类型: {ord['type']} | 方向: {ord['side']} | 触发/委托价: {ord.get('stopPrice') or ord.get('price')}")

            print("🧹 自动清理孤儿挂单中...")
            cleaned_count = 0
            failed_count = 0
            failed_ids = []
            for ord in unknown_orders:
                try:
                    self._safe_api_call(self.exchange.cancel_order, ord['id'], symbol, params={'stop': True})
                    print(f"  └─ ✅ 已撤销: {ord['id']}")
                    cleaned_count += 1
                except Exception as e:
                    print(f"  └─ ⚠️ 撤销失败: {ord['id']} - {e}")
                    failed_count += 1
                    failed_ids.append(str(ord['id']))

            if cleaned_count > 0:
                print(f"🧹 孤儿挂单已清理完毕 (共清理 {cleaned_count} 个)！")
                time.sleep(0.5)

            # 🔥 修复漏洞3：撤销失败时 Fail-Closed（原代码撤失败只 print 不阻断 → 孤儿单仍在场却放行开仓）
            if failed_count > 0:
                self.send_tg_notification(
                    f"🚨【资金安全】孤儿挂单撤销失败，已阻断开仓\n"
                    f"标的: `{symbol}`\n"
                    f"失败 {failed_count} 个: {', '.join(failed_ids)}\n"
                    f"请手动撤销后重试",
                    level='critical')
                return True  # Fail-Closed：孤儿单仍在场 → 阻断

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

    def _validate_take_profit(self, signal, current_mark_price: float) -> tuple[bool, str]:
        """
        R1 开仓前止盈价方向性校验（ChatGPT 终审 2026-08-20 补充）：
        只拦"方向性错误"——BUY 时止盈价 ≤ 当前市价、SELL 时止盈价 ≥ 当前市价。
        依据：条件单触发价必 > 市价（否则被跳过），成交价 ≥ 触发价 > 市价，
        TP ≤ 市价 ⇒ TP 必 < 成交价 ⇒ TAKE_PROFIT_MARKET 卖单触发价 ≤ 现价 ⇒ 币安确定性 -2021。
        不用 TP > max(所有层入场价)：阶梯入场场景 TP 低于未成交层触发价是合法的（ChatGPT 结论）。
        返回: (是否通过, 错误信息)
        """
        side = signal.side.upper()
        try:
            tp_price = float(signal.take_profit or 0.0)
        except (TypeError, ValueError):
            tp_price = 0.0
        if tp_price <= 0:
            return False, f"❌ 止盈价无效（{tp_price}），无法校验"

        if side == 'BUY':
            # 做多：止盈价必须高于当前市价（否则卖单触发价 ≤ 现价 → -2021 确定性拒绝）
            if tp_price <= current_mark_price:
                return False, (
                    f"❌ 止盈价方向错误！\n"
                    f"   ├─ 方向: BUY（做多）\n"
                    f"   ├─ 止盈价: {tp_price}\n"
                    f"   ├─ 当前市价: {current_mark_price}\n"
                    f"   └─ 做多时止盈价必须 > 当前市价（当前 {tp_price} <= {current_mark_price}），"
                    f"币安将确定性拒绝（-2021 Order would immediately trigger）"
                )
        else:  # SELL
            # 做空：止盈价必须低于当前市价（否则买单触发价 ≥ 现价 → -2021 确定性拒绝）
            if tp_price >= current_mark_price:
                return False, (
                    f"❌ 止盈价方向错误！\n"
                    f"   ├─ 方向: SELL（做空）\n"
                    f"   ├─ 止盈价: {tp_price}\n"
                    f"   ├─ 当前市价: {current_mark_price}\n"
                    f"   └─ 做空时止盈价必须 < 当前市价（当前 {tp_price} >= {current_mark_price}），"
                    f"币安将确定性拒绝（-2021 Order would immediately trigger）"
                )
        return True, "✅ 止盈价方向性校验通过！"

    def execute_signal(self, signal):
        symbol = signal.symbol
        batch_id = signal.batch_id
        # SG1: READY 门控——启动恢复未完成前禁止任何新风险（最终安全边界，任何调用路径必经）
        # 注：只 print 不发 TG（防告警风暴，安全 Gate ≠ 通知系统），用户提示由 bot_runner B 层负责
        if not self._ready:
            print(f"🚫 [SG1] 系统未就绪，拒绝新信号 [{batch_id}] ({symbol}): {self._not_ready_reason}")
            return None
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
            # SG2: 加仓前风险闸门——无法证明全部已有仓位（程序批次+未归属仓位）受有效 SL
            # 保护时拒绝新批次（Fail-Closed，不变量②）。helper 只判定，此处负责告知用户。
            allowed, sg2_reason = self._check_sl_coverage(symbol, all_states, current_pos)
            if not allowed:
                print(f"🚫 [SG2] 拒绝加仓信号 [{batch_id}]: {sg2_reason}")
                try:
                    self.send_tg_notification(
                        f"⚠️【加仓信号被拒】批次 `{batch_id}` ({symbol})\n"
                        f"原因: {sg2_reason}\n"
                        f"未执行任何下单。如需程序继续开仓，请先平掉未归属仓位；"
                        f"程序无法为未归属仓位确认保护状态。",
                        level='warning')
                except Exception:
                    pass
                return None
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
            # 🔥 修复漏洞4：ticker 异常返回空时市价为 0，后续校验（tp > 0 即通过）会失效
            if current_mark_price <= 0:
                msg = f"❌ 获取市价异常（返回 {current_mark_price}），已阻断挂单"
                print(msg)
                self.send_tg_notification(f"🚨 **挂单被阻断！**\n标的: `{symbol}`\n{msg}", level='critical')
                return None
            print(f"🌐 当前最新市场价格: {current_mark_price} USDT")

            # 🔥 止损价合理性校验（在挂单前拦截不合理数据）
            print("\n🔍 [止损价合理性校验中...]")
            is_valid, msg = self._validate_stop_losses(signal, current_mark_price)
            if not is_valid:
                print(msg)
                self.send_tg_notification(f"🚨 **挂单被阻断！**\n{msg}", level='critical')
                return None
            print(msg)

            # R1: 止盈价方向性校验（ChatGPT 终审 2026-08-20）——开仓前拦截确定性错误：
            # BUY 需 TP > 现价、SELL 需 TP < 现价（币安 -2021 判定基准是现价）。
            # 校验失败 = 参数确定错误，任何重试必失败 → 直接阻断整批，不挂任何开仓单。
            print("\n🔍 [止盈价合理性校验中...]")
            tp_is_valid, tp_msg = self._validate_take_profit(signal, current_mark_price)
            if not tp_is_valid:
                print(tp_msg)
                self.send_tg_notification(f"🚨 **挂单被阻断！**\n{tp_msg}", level='critical')
                return None
            print(tp_msg)

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

            # ── B2-5（§5.6 + Case F）：进入开仓循环前，先落批次骨架 + 全部将尝试层 ENTRY 意图 ──
            # 崩溃安全 Create：中途崩溃 → 交易所可能已有挂单，本地保留 identity 证据可对账/自愈。
            # 价格过滤与主循环同规则：被跳过层不预写（不残留 PENDING_CREATE）。
            # 恢复护栏（_registry_has_unresolved_entries）保证骨架批次不被自动清理。
            position_side = 'LONG' if side == 'BUY' else 'SHORT'
            skeleton_entry_layers = []
            for _idx, (_raw_tp, _raw_amt) in enumerate(signal.entries):
                _fp = float(self.exchange.price_to_precision(symbol, _raw_tp))
                if side == 'BUY':
                    if _fp <= current_mark_price:
                        continue
                else:
                    if _fp >= current_mark_price:
                        continue
                skeleton_entry_layers.append(_idx)
            skeleton_registry = {}
            for _idx in skeleton_entry_layers:
                _raw_tp, _raw_amt = signal.entries[_idx]
                _fp = float(self.exchange.price_to_precision(symbol, _raw_tp))
                skeleton_registry[self._protection_identity(batch_id, 'ENTRY', _idx, position_side)] = {
                    'state': 'PENDING_CREATE',  # 意图先落盘：create 可能已发出 → 恢复时仲裁闸门禁重挂
                    'id_known': False,
                    'order_kind': 'conditional',
                    'role': 'ENTRY',
                    'layer': _idx,
                    'side': position_side,
                    'intent': self._build_intent(
                        symbol=symbol, side=order_side,
                        qty=float(self.exchange.amount_to_precision(symbol, _raw_amt)),
                        order_type='STOP_MARKET', stop_price=_fp),
                    'updated_at': time.time(),
                }
            # B2-6（§6.3 + Case F）：骨架持久化 entry_layers/entry_stop_steps 元数据——
            # 崩溃恢复收编 ENTRY 后重建 entry_orders/stop_steps/layer_sl_params 的权威映射
            # （registry 条目只含 layer 序号，不含 SL 价格；此处补全层→SL 映射）
            skeleton = {
                'is_active': True,
                'batch_id': batch_id,
                'symbol': symbol,
                'side': side,
                'entry_layers': list(skeleton_entry_layers),
                'entry_stop_steps': [signal.stop_loss_steps[i] if i < len(signal.stop_loss_steps) else 0.0
                                     for i in skeleton_entry_layers],
                'entry_orders': [],
                'stop_steps': [],
                'take_profit_price': signal.take_profit,
                'current_sl_id': None,
                'tp_order_id': None,
                'batch_total_amount': 0.0,
                'target_amounts': [],
                'params_base': params_base,
                'is_hedge_mode': is_hedge_mode,
                'last_filled_count': 0,
                'filled_details': [],
                'total_entry_fee': 0.0,
                'user_modified': False,
                'pending_sl_orders': [],
                'prepared_tp_params': {},
                'layer_sl_params': [],
                'sl_fail_count': {},
                'sl_failed_layers': [],
                'protection_registry': skeleton_registry,
            }
            self.save_batch_state(symbol, batch_id, skeleton)

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
                        params=order_params,
                        retries=1
                    )
                    entry_orders.append(order['id'])
                    target_amounts.append(formatted_amount)
                    active_stop_steps.append(signal.stop_loss_steps[idx])
                    batch_total_amount += formatted_amount

                    # B2-5（§5.6 T2c）：create 成功 → PENDING_VERIFY + order_id + id_known=true
                    self._update_registry(
                        symbol, batch_id,
                        self._protection_identity(batch_id, 'ENTRY', idx, position_side),
                        state='PENDING_VERIFY', order_id=order['id'], id_known=True)

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

                except ccxt.ExchangeError as e:
                    if "-2021" in str(e):
                        print(
                            f"⚠️ [挂单失败] 第 {idx + 1} 层触发价 {formatted_price} 不满足{'高于' if side == 'BUY' else '低于'}市价条件，已自动跳过。")
                        # B2-5（§5.6）：-2021 确定拒绝 → 该层 ABSENT（不残留 PENDING_CREATE、不计 FAILED）
                        self._update_registry(
                            symbol, batch_id,
                            self._protection_identity(batch_id, 'ENTRY', idx, position_side),
                            state='ABSENT')
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
                'sl_fail_count': {},
                'sl_failed_layers': [],
            }
            # B2-5（§5.6 T4）：完整批次状态落盘前，合并 registry 并把全部成功层 ENTRY → CONFIRMED
            # （业务 Commit）。ABSENT（-2021）层保持不动；跳过滤层本就不在骨架中。
            _latest_b = self.load_all_states().get(symbol, {}).get(batch_id, {})
            _merged_registry = dict(_latest_b.get('protection_registry') or {})
            for _identity, _entry in _merged_registry.items():
                if _entry.get('role') == 'ENTRY' and _entry.get('state') in ('PENDING_CREATE', 'PENDING_VERIFY'):
                    _entry['state'] = 'CONFIRMED'
                    _entry['updated_at'] = time.time()
            batch_state_data['protection_registry'] = _merged_registry
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
                self.send_tg_notification(warning_msg, level='warning')

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

    # ==================== SG3-P1: 保护单有效性校验 ====================

    def _check_protection_order_validity(self, ord, expected_side, is_hedge_mode,
                                         position_side, required_amount):
        """SG3-P1: 校验保护单（SL/TP）有效性。返回 (valid, reason)。

        纯读快照判断器——只用 open_orders_map 已拉取的订单数据，零新增 API。
        只做事实判断，不触发任何策略动作（user_modified/need_recover/TG/下单全在监控循环决策）。
        三项校验（ChatGPT 审定）：
          ① 方向 side：SL/TP 必须与仓位反向（BUY 仓 → sell 保护单）
          ② 保护语义：单向=reduceOnly/closePosition 任一 true；Hedge=side 已匹配 且 positionSide 匹配（LONG+BUY=加仓非保护）
          ③ 数量：amount 非 None 时 ≥ required*(1-0.001)-1e-9（0.1% 容差）；None（closePosition 全仓平）跳过
        明确不校验：stopPrice（策略参数，用户可改）、type（ccxt 归一化为 market，校验必误报）。"""
        # ① 方向
        if str(ord.get('side', '')).lower() != expected_side:
            return False, f"方向错误(期望{expected_side}，实际{ord.get('side')})"
        # ② 保护语义
        info = ord.get('info', {}) or {}
        if is_hedge_mode:
            if str(info.get('positionSide', '')).upper() != str(position_side).upper():
                return False, f"positionSide 不匹配(期望{position_side}，实际{info.get('positionSide')})"
        else:
            ro = str(info.get('reduceOnly') or '').lower()
            cp = str(info.get('closePosition') or '').lower()
            if ro != 'true' and cp != 'true':
                return False, "缺少保护语义(reduceOnly/closePosition 均非 true)"
        # ③ 数量（closePosition 单 amount=None → 跳过，全仓平语义天然覆盖）
        amount = ord.get('amount')
        if amount is not None:
            try:
                amount = float(amount)
            except (TypeError, ValueError):
                return False, f"数量字段异常({amount!r})"
            if amount < required_amount * (1 - 0.001) - 1e-9:
                return False, f"覆盖数量不足({amount} < {required_amount})"
        return True, ""

    # ==================== C5/SG4: Create→Verify→Commit ====================

    def _verify_order_created(self, order_id, symbol, order_kind='conditional'):
        """C5/SG4 + B1(P0-3): 写侧三态验证——create_order 返回的 id 在交易所是否真实存在。

        Verify 必须用 fetch_order（事务确认点），不用 open_orders 快照（周期监控数据，
        不承担事务语义，且新单可能尚未进入本轮快照）。返回三态（禁止退化为 True/False）：
          'success'   → fetch_order 成功，订单真实存在 → 调用方才可 Commit
          'not_found' → OrderNotFound，订单确实不存在 → 不 Commit + NOT_CONFIRMED（禁重试禁补单）
          'unknown'   → 其他异常（NetworkError 等）→ 不 Commit + critical + 不自动补单
                        关键：UNKNOWN ≠ NOT_FOUND —— 网络未知不能被当成"不存在"（UNKNOWN ≠ EMPTY）。
        order_kind（B1/P0-3，默认 'conditional' 兼容 C5 既有 2 参调用）：
          'conditional' → STOP/TAKE_PROFIT 条件单，fetch_order 必须带 params={'stop': True} 走
                          algo 端点；不带 stop=True 查条件单会命中普通端点 → 恒 not_found →
                          假阴性（C5 实盘事故根因：12 处全误判 not_found → 无限重挂 24 孤儿单）。
          'normal'      → 普通限价/市价单，走默认端点（不带 params）。
        """
        try:
            if order_kind == 'conditional':
                self._safe_api_call(self.exchange.fetch_order, order_id, symbol,
                                    params={'stop': True}, retries=1)
            else:
                self._safe_api_call(self.exchange.fetch_order, order_id, symbol, retries=1)
            return 'success'
        except ccxt.OrderNotFound:
            # R-A（事件3根因A）：create 后立即 fetch 命中 Binance algo 端点可见性延迟
            #（事件3实证：4/4 单 create 成功但 0 秒 verify 全部 OrderNotFound 假阴性）。
            # OrderNotFound 短窗口重试（2s × 3）：仍查不到才返回 not_found；
            # 重试期网络异常 → unknown（结果未知 ≠ 不存在，UNKNOWN ≠ EMPTY）。
            for _attempt in range(3):
                time.sleep(2)
                try:
                    if order_kind == 'conditional':
                        self._safe_api_call(self.exchange.fetch_order, order_id, symbol,
                                            params={'stop': True}, retries=1)
                    else:
                        self._safe_api_call(self.exchange.fetch_order, order_id, symbol, retries=1)
                    return 'success'
                except ccxt.OrderNotFound:
                    continue
                except Exception:
                    return 'unknown'
            return 'not_found'
        except Exception:
            return 'unknown'

    def _verify_and_update_registry(self, symbol, batch_id, identity, order_id, desc='保护单',
                                    order_kind='conditional'):
        """B2-0: create 成功后 verify 统一入口——按操作阶段区分异常语义（ChatGPT 评审①）：
        verify 阶段 OrderNotFound ≠ create 阶段 ExchangeError（可能是查询延迟/路由参数错误/
        交易所暂时不可见/订单已状态变化），因此：
          success    → registry CONFIRMED，返回 'success'（调用方才可 Commit）
          not_found  → registry NOT_CONFIRMED（不 Commit、不计数、不自动重挂），返回 'not_found'
          unknown    → registry PENDING_VERIFY（结果未知，不计数不补单），返回 'unknown'
        调用方只按返回值执行副作用；verify 分支内禁止 raise/计数/自动重挂（C5 事故模式）。"""
        verify_result = self._verify_order_created(order_id, symbol, order_kind)
        if verify_result == 'success':
            self._update_registry(symbol, batch_id, identity, state='CONFIRMED',
                                  order_id=order_id, id_known=True)
        elif verify_result == 'not_found':
            self._update_registry(symbol, batch_id, identity, state='NOT_CONFIRMED',
                                  order_id=order_id, id_known=True)
        else:
            self._update_registry(symbol, batch_id, identity, state='PENDING_VERIFY',
                                  order_id=order_id, id_known=True)
        return verify_result

    # ==================== B1/P0-2: create 异常精确分类 + 幂等键 + registry ====================

    def _classify_create_exception(self, e):
        """B1/P0-2: create_order 异常精确分类（规格 §3.2 FAILED 精确分类）：
          'failed'  → ExchangeError 及其子类（确定拒绝：余额不足/无效订单/权限/OrderNotFound 等）
                      → 可计数、可安全重试（FAILED 是唯一允许再次 Create 的自动路径）
          'unknown' → NetworkError/RequestTimeout/RateLimitExceeded/其他 → 结果未知（id_unknown）
                      关键：网络未知 ≠ 创建失败 —— 禁止计数、禁止自动补单（UNKNOWN ≠ EMPTY）。
        ccxt 异常层次：ExchangeError 与 NetworkError 是两个独立分支（errors.py L70/L182 实证），
        因此 isinstance(e, ccxt.ExchangeError) 为 False 即落入 unknown。"""
        if isinstance(e, ccxt.ExchangeError):
            return 'failed'
        return 'unknown'

    def _protection_identity(self, batch_id, role, layer, side):
        """B1/P0-2: 保护单幂等身份键（规格 §5.1）—— batch_id|role|L{layer}|side。
        role ∈ {SL, TP, ENTRY}；side 为持仓方向（LONG/SHORT）。
        含 batch_id → 旧批次/新批次同层不互认，杜绝跨批次收编错单（§13 场景⑨）。"""
        return f"{batch_id}|{role}|L{layer}|{side}"

    def _registry_has_unresolved_entries(self, b_data):
        """B2-5: registry 存在任一未决 ENTRY（PENDING_CREATE/PENDING_VERIFY/NOT_CONFIRMED/HARD_LOCK）
        → True。恢复护栏：前置落盘骨架/崩溃窗口批次（entry_orders=[]）不得被自动清理，
        保留证据待对账（旧行为 entry_orders=[] 且无持仓 → 自动清理 → 证据被毁，前置落盘失去意义）。
        终态（CONFIRMED/ABSENT/FAILED/MISMATCH）→ False，照常清理，无回归。"""
        reg = (b_data or {}).get('protection_registry') or {}
        for entry in reg.values():
            if not isinstance(entry, dict):
                continue
            if entry.get('state') in ('PENDING_CREATE', 'PENDING_VERIFY', 'NOT_CONFIRMED', 'HARD_LOCK'):
                return True
        return False

    def _update_registry(self, symbol, batch_id, identity, state=None, order_id=None,
                         id_known=None, order_kind=None, role=None, layer=None, side=None,
                         intent=None, fail_count_incr=None, hard_locked=None,
                         terminated_reason=None):
        """B1/P0-2: 保护单 registry 落盘（规格 §5.2）—— protection_registry[identity] 状态条目。
        每次更新刷新 updated_at；load → modify → save（读最新状态再写，防覆盖并发修改）。
        调用方必须持有监控线程/写侧锁（仲裁需持锁，§13 推论④）。
        B2-2: intent 不可变（ChatGPT③）——首次写入后不覆盖，防后期参数漂移
        导致自愈匹配失败/错收编。
        B2-4: fail_count_incr 递增条目级 fail_count 并返回新值（HARD_LOCK 判定源，§5.4）；
        hard_locked 落盘硬锁标记。"""
        latest_all = self.load_all_states()
        b = latest_all.get(symbol, {}).get(batch_id)
        if b is None:
            return None
        reg = b.setdefault('protection_registry', {})
        entry = reg.setdefault(identity, {})
        if state is not None:
            entry['state'] = state
        if order_id is not None:
            entry['order_id'] = order_id
        if id_known is not None:
            entry['id_known'] = id_known
        if order_kind is not None:
            entry['order_kind'] = order_kind
        if role is not None:
            entry['role'] = role
        if layer is not None:
            entry['layer'] = layer
        if side is not None:
            entry['side'] = side
        if intent is not None:
            entry.setdefault('intent', intent)
        new_fail_count = None
        if fail_count_incr is not None:
            new_fail_count = entry.get('fail_count', 0) + fail_count_incr
            entry['fail_count'] = new_fail_count
        if hard_locked is not None:
            entry['hard_locked'] = hard_locked
        if terminated_reason is not None:
            entry['terminated_reason'] = terminated_reason
        entry['updated_at'] = time.time()
        self.save_batch_state(symbol, batch_id, b)
        return new_fail_count

    def _assert_create_allowed(self, symbol, batch_id, identity, desc='保护单', replace_order_id=None):
        """B2-3: Create 仲裁闸门（规格 §5.3 + §10.1 最小联动）——
        同 identity 存在未终结/已确认状态时禁止新 create（C5 重挂变体最后防线）：
          禁止 = {PENDING_CREATE, PENDING_VERIFY, NOT_CONFIRMED, CONFIRMED, MISMATCH}
            PENDING_CREATE  意图已落盘，create 可能已发出 → 再 create = 双单
            PENDING_VERIFY  结果未知（网络异常）→ 再 create = 双单风险
            NOT_CONFIRMED   查询不到 ≠ 不存在（algo 延迟/路由错误）→ 禁自动重挂（C5 根因）
            CONFIRMED       已确认有单 → 再 create = 双单
            MISMATCH        订单与意图不符（错单嫌疑）→ 需人工，禁自动 create
          允许 = FAILED（确定拒绝，唯一允许再次 Create 的自动路径，§8 转移表）
                / ABSENT（人工核实确无此单后重建）/ 无条目（首次创建）
        B2-8: replace_order_id（换挂语义，§5.7 #1/#2/#3/#5/#6 收编）——
          CONFIRMED 且 replace_order_id == entry.order_id → 允许（调用方声明：确认的旧单
          将被撤销替换——先撤后挂或先挂后撤，旧单物理离开 → 无双单）
          未决态（PENDING_CREATE/PENDING_VERIFY/NOT_CONFIRMED）/错单（MISMATCH）/硬锁
          → 一律拒绝换挂（结果未知时禁止以"替换"名义 create——攻击点③闭环保持）
        全局 cooldown（§10.1）：_api_cooldown_until 未到期 → 拒绝，封禁期不发请求（避免撞 418）。
        返回 (allowed: bool, reason: str)。调用点必须持有写侧锁（§13 推论④）。"""
        cooldown_until = getattr(self, '_api_cooldown_until', 0) or 0
        if cooldown_until and time.time() < cooldown_until:
            return False, (f"全局 cooldown 未到期（剩余 {int(cooldown_until - time.time())}s），"
                           f"封禁期禁止 Create")
        latest_all = self.load_all_states()
        b = latest_all.get(symbol, {}).get(batch_id)
        if b is None:
            self._gate_alert_clear(identity)
            return True, ''
        entry = b.get('protection_registry', {}).get(identity)
        if entry is None:
            self._gate_alert_clear(identity)
            return True, ''
        state = entry.get('state')
        # B2-4: HARD_LOCK 真熔断（§5.4）——置于状态检查最前；
        # reason 以 'HARD_LOCK' 开头供调用点识别静默（进入时已 critical，此后静默）。
        if state == 'HARD_LOCK' or entry.get('hard_locked'):
            return False, (f"HARD_LOCK: identity `{identity}` 已硬锁"
                           f"（fail_count={entry.get('fail_count', 0)}），等待人工解锁")
        # 防御（不变量①⑧）：fail_count≥5 却未置锁 = 置锁写盘失败/旧数据 → 保守拒绝，
        # 宁可不做不可错做；启动校验（_validate_registry_locks_on_startup）会补置锁。
        if (entry.get('fail_count') or 0) >= 5:
            return False, (f"HARD_LOCK: identity `{identity}` fail_count≥5 但未置硬锁"
                           f"（异常数据/置锁写盘失败），保守禁止 Create，需人工核实")
        if state == 'FAILED':
            return True, ''  # 确定拒绝 → 允许经闸门重试（计数延续不清零，§8）——runtime补丁已移除误清
        if state == 'ABSENT':
            self._gate_alert_clear(identity)
            return True, ''  # 人工核实确无此单 → 允许重建
        if state in ('PENDING_CREATE', 'PENDING_VERIFY', 'NOT_CONFIRMED', 'CONFIRMED', 'MISMATCH'):
            if state == 'CONFIRMED' and replace_order_id and entry.get('order_id') == replace_order_id:
                # B2-8 换挂语义：确认的旧单将被撤销替换（先撤后挂/先挂后撤，旧单物理离开）
                self._gate_alert_clear(identity)
                return True, ''
            return False, (f"identity `{identity}` 状态 `{state}` "
                           f"未终结/已确认/错单嫌疑，禁止再次 Create")
        # 未知状态（防御）→ 保守禁止：宁可不做，不可错做（不变量①⑧）
        return False, f"identity `{identity}` 状态 `{state}` 未知，保守禁止 Create（需人工核实）"

    def _validate_registry_locks_on_startup(self):
        """B2-4: 启动校验全部 protection_registry 的硬锁与解锁审计（规格 §5.5 + 重启恢复表 §6.2）。
        规则：
          1. state=HARD_LOCK 且 hard_locked=false：无审计三字段 → 非法解锁，回滚 hard_locked=true + critical；
             有审计三字段（unlock_reason/unlock_time/unlock_operator）→ 合法解锁（用户已核实），不干预
          2. state=FAILED 且 fail_count>=5 未置锁（旧数据/崩溃窗口）→ 补置 hard_locked + critical
          3. state=HARD_LOCK 且 hard_locked=true → 维持锁定（静默，等待人工）
        返回 (rolled_back: int, alerted: int)。调用点：bot 启动恢复路径（run_trader_recovery_on_startup），
        必须在 recover_active_batches 之前执行——恢复逻辑读 registry，需先保证硬锁状态正确。"""
        all_states = self.load_all_states()
        rolled_back = 0
        alerted = 0
        for symbol, sym_states in all_states.items():
            if not isinstance(sym_states, dict):
                continue
            for batch_id, b in sym_states.items():
                if not isinstance(b, dict):
                    continue
                reg = b.get('protection_registry') or {}
                if not reg:
                    continue
                dirty = False
                for identity, entry in reg.items():
                    if not isinstance(entry, dict):
                        continue
                    state = entry.get('state')
                    hard_locked = entry.get('hard_locked')
                    if state == 'HARD_LOCK' and not hard_locked:
                        audit_ok = all(entry.get(k) for k in
                                       ('unlock_reason', 'unlock_time', 'unlock_operator'))
                        if not audit_ok:
                            # 非法解锁（§5.5）：防"不知道为什么直接改 false"回到不可审计状态
                            entry['hard_locked'] = True
                            entry['updated_at'] = time.time()
                            dirty = True
                            rolled_back += 1
                            self.send_tg_notification(
                                f"🚨 **检测到非法解锁，已回滚为硬锁**\n"
                                f"🆔 批次：`{batch_id}`\n"
                                f"📌 identity：`{identity}`\n"
                                f"⚠️ hard_locked=false 但缺少审计三字段"
                                f"（unlock_reason / unlock_time / unlock_operator）\n"
                                f"🔒 已回滚 hard_locked=true。如需解锁：\n"
                                f"1. 到交易所核实该 identity 订单实际状态\n"
                                f"2. 手改 trade_state.json 对应条目：state → ABSENT 或 CONFIRMED，"
                                f"并同时写入 unlock_reason/unlock_time/unlock_operator",
                                level='critical')
                            alerted += 1
                        # 有审计三字段 → 合法解锁，不干预（state 应已离开 HARD_LOCK）
                    elif state == 'FAILED' and (entry.get('fail_count') or 0) >= 5 and not hard_locked:
                        # 旧数据/崩溃窗口：fail_count≥5 未置锁 → 补置硬锁（重启恢复表 §6.2）
                        entry['hard_locked'] = True
                        entry['state'] = 'HARD_LOCK'
                        entry['updated_at'] = time.time()
                        dirty = True
                        rolled_back += 1
                        self.send_tg_notification(
                            f"🚨 **检测到未置锁的 FAILED 记录，已补置硬锁**\n"
                            f"🆔 批次：`{batch_id}`\n"
                            f"📌 identity：`{identity}`\n"
                            f"⚠️ fail_count={entry.get('fail_count')} ≥ 5 但未硬锁"
                            f"（旧数据/置锁写盘崩溃窗口）\n"
                            f"🔒 已补置 HARD_LOCK。请人工核实后按 §5.5 规范解锁",
                            level='critical')
                        alerted += 1
                    # state=HARD_LOCK 且 hard_locked=true → 维持锁定（静默，等待人工）
                    # 其他状态 → 不干预
                if dirty:
                    self.save_batch_state(symbol, batch_id, b)
        return rolled_back, alerted

    def _build_intent(self, symbol, side, qty, order_type, stop_price=None, reduce_only=None):
        """B2-2: 不可变 intent 指纹（ChatGPT③）—— identity 回答"是不是同一个逻辑订单"，
        intent 回答"这个逻辑订单具体要下什么"（symbol/side/qty/order_type/stop_price/reduce_only）。
        自愈匹配（_order_matches_intent）与 Create 仲裁（B2-3）均以此为准；
        qty/stop_price 统一转 float，数值比较容忍交易所精度往返。"""
        try:
            qty = float(qty) if qty is not None else None
        except (TypeError, ValueError):
            qty = None
        try:
            stop_price = float(stop_price) if stop_price is not None else None
        except (TypeError, ValueError):
            stop_price = None
        return {
            'symbol': symbol,
            'side': side,
            'qty': qty,
            'order_type': order_type,
            'stop_price': stop_price,
            'reduce_only': bool(reduce_only) if reduce_only is not None else None,
        }

    def _order_matches_intent(self, order, intent, symbol):
        """B2-1 + F1（事件3通知风暴根因，2026-08-21）：fetch 返回订单字段与 registry intent 完整比对——
        FOUND ≠ CONFIRMED；FOUND + intent 完整匹配 = CONFIRMED。
        比对 symbol/side/order_type/reduceOnly/stopPrice/amount；缺失字段跳过（软检查），
        明确不匹配即返回 False（宁可不收编，不可错收编）。
        F1 现实映射修正——ccxt 归一化产物 vs Binance 原始字段（实证：4 条有效保护单全部被误判 MISMATCH）：
          a) symbol：ccxt 统一格式 'BTC/USDT:USDT'，本地 intent 存 'BTCUSDT' → 去分隔符归一化再比
          b) order_type：ccxt 顶层 type 对条件单归一化为 'market'（SG3-P1 共识：type 校验必误报）
             → 条件单优先用 info.type（Binance 原始 STOP_MARKET/TAKE_PROFIT_MARKET）还原；
             顶层 market 且 info.type 缺失 → 跳过 type 比对（软检查，防误杀有效单）
          c) reduceOnly：info 里可能是字符串 'true'/'false'（Binance 原始）→ 统一转 bool"""
        try:
            # a) symbol 归一化（防 'BTCUSDT' vs 'BTC/USDT:USDT' 误判）：
            #    ccxt 统一格式 'BASE/QUOTE:SETTLE' → 取 BASE+QUOTE（'BTC/USDT:USDT'→'BTCUSDT'）
            def _norm_sym(s):
                s = str(s or '').upper()
                if '/' in s:
                    base, rest = s.split('/', 1)
                    quote = rest.split(':', 1)[0]
                    return (base + quote).replace('_', '')
                return s.replace('/', '').replace(':', '').replace('_', '')
            if _norm_sym(order.get('symbol')) != _norm_sym(intent.get('symbol')):
                return False
            o_side = str(order.get('side', '')).lower()
            i_side = str(intent.get('side', '')).lower() if intent.get('side') else ''
            if o_side and i_side and o_side != i_side:
                return False
            # b) order_type：顶层 type 对条件单是 'market'（ccxt 归一化）→ info.type 还原
            o_type = str(order.get('type', '')).upper().replace('_', '')
            i_type = str(intent.get('order_type', '')).upper().replace('_', '')
            if i_type:
                info_type = None
                if isinstance(order.get('info'), dict):
                    info_type = str(order['info'].get('type', '')).upper().replace('_', '')
                if info_type:
                    o_type = info_type  # 条件单：优先用 Binance 原始类型还原
                elif o_type == 'MARKET':
                    o_type = ''  # 顶层 market 且无 info.type → 跳过 type 比对（软检查，SG3-P1 共识）
                if o_type and o_type != i_type:
                    return False
            # c) reduceOnly：顶层 or info 兜底；Binance info 里是字符串 'true'/'false' → 统一 bool
            ro = order.get('reduceOnly')
            if ro is None and isinstance(order.get('info'), dict):
                ro = order['info'].get('reduceOnly')
            if ro is not None and intent.get('reduce_only') is not None:
                def _as_bool(v):
                    if isinstance(v, str):
                        return v.strip().lower() == 'true'
                    return bool(v)
                if _as_bool(ro) != _as_bool(intent['reduce_only']):
                    return False
            # stopPrice：顶层 or info 兜底
            sp = order.get('stopPrice')
            if sp is None and isinstance(order.get('info'), dict):
                sp = order['info'].get('stopPrice')
            if sp is not None and intent.get('stop_price') is not None:
                if abs(float(sp) - float(intent['stop_price'])) > max(
                        1e-8, abs(float(intent['stop_price'])) * 1e-6):
                    return False
            # amount（qty）
            amt = order.get('amount')
            if amt is not None and intent.get('qty') is not None:
                if abs(float(amt) - float(intent['qty'])) > max(
                        1e-8, abs(float(intent['qty'])) * 1e-6):
                    return False
            return True
        except Exception:
            return False  # 解析失败 → 保守不匹配

    def _adjudicate_recreate_before_repair(self, symbol, batch_id, identity):
        """F3（2026-08-21 事件4）：补挂前的 registry 实况裁决——治愈"批次级 id 缺失 + registry
        CONFIRMED"死锁态（R14 每轮补挂 → 闸门永久拦截，registry 永不终结）。
        返回 (verdict, order_id)：
          ('allow', None)    → registry 无条目 / 已终结(ABSENT/FAILED) → 允许补挂
          ('adopt', id)      → CONFIRMED/未决 且 fetch 在场且 intent 匹配 → 收养已有订单（防双挂）
          ('mismatch', None) → 在场但 intent 不匹配 → 已 critical 告警，禁止自动处理
          ('hold', None)     → 网络异常 / 结果未知 → 保守保留下轮（不补挂不收养，防双单）
        防双单核心：任何未终结状态在 fetch 确认终结前，一律不自动补挂（对齐 B2-3 仲裁 §5.3）。"""
        try:
            latest_all = self.load_all_states()
            entry = (latest_all.get(symbol, {}).get(batch_id, {})
                     .get('protection_registry', {}).get(identity))
            if not entry:
                return 'allow', None
            state = entry.get('state')
            order_id = entry.get('order_id')
            if state in ('ABSENT', 'FAILED'):
                return 'allow', None
            if state == 'MISMATCH':
                return 'mismatch', None
            if state in ('CONFIRMED', 'PENDING_VERIFY', 'NOT_CONFIRMED') and order_id:
                try:
                    order = self._safe_api_call(self.exchange.fetch_order, order_id, symbol,
                                                retries=1, params={'stop': True})
                except ccxt.OrderNotFound:
                    # 交易所已清除 → 视同终结 → ABSENT 放行补挂（对齐 S33 语义）
                    self._update_registry(symbol, batch_id, identity, state='ABSENT',
                                          terminated_reason='f3_adjudicate_order_not_found')
                    return 'allow', None
                except Exception:
                    return 'hold', None  # 网络异常 → 结果未知，保留下轮
                valid_statuses = ('new', 'open', 'active')
                status = str(order.get('status', '')).lower()
                if status and status not in valid_statuses:
                    # 已终结（canceled/expired/closed/triggered）→ ABSENT 放行补挂
                    self._update_registry(symbol, batch_id, identity, state='ABSENT',
                                          terminated_reason=f'f3_adjudicate_status_{status}')
                    return 'allow', None
                # 在场：intent 匹配 → 收养（补 Commit，绝不再 create——防双单复活）
                intent = entry.get('intent')
                if intent and self._order_matches_intent(order, intent, symbol):
                    if state != 'CONFIRMED':
                        self._update_registry(symbol, batch_id, identity, state='CONFIRMED')
                    return 'adopt', order_id
                if state == 'CONFIRMED':
                    # 在场但与记录意图不符 → 错单/旧单嫌疑：critical 告警，禁止自动处理
                    self.send_tg_notification(
                        f"🚨 **保护单补挂前置裁决：订单与意图不匹配**\n"
                        f"🆔 批次：`{batch_id}`\n"
                        f"📌 身份：`{identity}`\n"
                        f"📌 订单：`{order_id}`\n"
                        f"⚠️ registry 确认有单但在场订单与记录意图不符（错单/旧单嫌疑）\n"
                        f"💡 程序不自动处理，请人工核实后手动清理",
                        level='critical')
                    return 'mismatch', None
                return 'hold', None  # 未决态在场但不匹配 → 保守
            # PENDING_CREATE（意图已落盘，create 可能已发出）→ 结果未知，保守
            return 'hold', None
        except Exception:
            return 'hold', None  # 任何异常 → 保守保留下轮

    def _is_stale_pre_launch_entry(self, entry):
        """F4b（2026-08-21）：启动窗口降级判断——registry 条目 updated_at 早于本进程启动时刻
        = 启动前历史条目（重启时的状态同步，非本轮新创建）→ 自愈告警降级为 info。
        返回 False 时照常 critical。防御性：_process_start_ts 缺失/非数值（测试 MagicMock 基座）
        → 一律不降级（保守：宁多告警，不漏告警）。"""
        try:
            start_ts = getattr(self, '_process_start_ts', 0)
            if not isinstance(start_ts, (int, float)) or not start_ts:
                return False
            ua = entry.get('updated_at') or 0
            if not isinstance(ua, (int, float)) or not ua:
                return False
            return ua < start_ts
        except Exception:
            return False

    def _recheck_registry_self_heal(self, symbol, batch_id):
        """B1/P0-2 + B2-1: registry 重查自愈（规格 §6.3）—— PENDING_VERIFY/NOT_CONFIRMED 条目重新 fetch：
          FOUND + intent 完整匹配 → CONFIRMED + 收编（补 Commit current_sl_id/tp_order_id，绝不新建）
          FOUND 但 intent 不匹配 → MISMATCH + critical + 不收编（错单/旧单/其他批次单，ChatGPT②）
          FOUND 无 intent       → 保守维持原状态，不收编（旧条目等待人工核实）
          OrderNotFound         → NOT_CONFIRMED（维持，静默）
          其他异常              → 维持 PENDING_VERIFY（结果未知，静默等下一轮重查）
        只补 Commit 不 Create —— 防双单复活（§13 场景⑦：成功场景必须收编已存在订单）。"""
        latest_all = self.load_all_states()
        b = latest_all.get(symbol, {}).get(batch_id)
        if b is None:
            return
        reg = b.get('protection_registry', {})
        changed = False
        for identity, entry in reg.items():
            if entry.get('state') not in ('PENDING_VERIFY', 'NOT_CONFIRMED'):
                # R-B: 条目已终结/确认 → 清理累计未确认轮次（防内存膨胀）
                rounds = getattr(self, '_self_heal_unconfirmed_rounds', None)
                if isinstance(rounds, dict):
                    rounds.pop((symbol, batch_id, identity), None)
                continue
            order_id = entry.get('order_id')
            if not order_id:
                continue
            order_kind = entry.get('order_kind', 'conditional')
            try:
                if order_kind == 'conditional':
                    order = self._safe_api_call(self.exchange.fetch_order, order_id, symbol,
                                                params={'stop': True}, retries=1)
                else:
                    order = self._safe_api_call(self.exchange.fetch_order, order_id, symbol, retries=1)
            except ccxt.OrderNotFound:
                if entry.get('state') != 'NOT_CONFIRMED':
                    entry['state'] = 'NOT_CONFIRMED'
                    entry['updated_at'] = time.time()
                    changed = True
                # R-B: 持续未确认升级告警（L1 生命周期不变量：失败状态通知 + 人工接管入口）——
                # 连续 N 轮仍查不到 → critical 一次（不刷屏；成功/终结后计数自动清零）。
                # 触发一次即止，避免告警风暴（若此后成功又再度失败，可再次触发）。
                rounds = getattr(self, '_self_heal_unconfirmed_rounds', None)
                if not isinstance(rounds, dict):
                    rounds = {}
                    self._self_heal_unconfirmed_rounds = rounds
                key = (symbol, batch_id, identity)
                rounds[key] = rounds.get(key, 0) + 1
                threshold = getattr(self, '_self_heal_escalate_rounds', 10)
                if rounds[key] == threshold:
                    self.send_tg_notification(
                        f"🚨 **保护单持续无法确认（请人工核实）**\n"
                        f"🆔 批次：`{batch_id}`\n"
                        f"📌 身份：`{identity}`\n"
                        f"📌 订单：`{order_id}`\n"
                        f"⚠️ 程序连续 {rounds[key]} 轮自愈重查仍查不到该订单\n"
                        f"💡 订单可能未真正创建、已触发或已被撤销。请到交易所核实持仓保护状态！",
                        level='critical')
                continue
            except Exception:
                continue  # 结果未知 → 维持现状静默，等待下一轮重查
            # F2（事件3通知风暴根因，2026-08-21）：订单生命周期分层——fetch 到 ≠ 订单有效。
            # 已终结订单（canceled/expired/rejected/closed/triggered）→ 视为不存在，标 ABSENT，
            # 不进 intent 比对、不告警（对齐启动恢复 L1155 valid_statuses 先例）。
            # 实证：自愈 fetch 已撤销订单返回 status=canceled 对象（不抛 OrderNotFound）→
            # 旧代码当 FOUND 进 intent 比对 → symbol/type 格式差异 → 误判 MISMATCH + critical。
            valid_statuses = ('new', 'open', 'active')
            _status = str(order.get('status', '')).lower()
            if _status and _status not in valid_statuses:
                entry['state'] = 'ABSENT'
                entry['updated_at'] = time.time()
                entry['terminated_reason'] = f'lifecycle_ended_status_{_status}'
                changed = True
                rounds = getattr(self, '_self_heal_unconfirmed_rounds', None)
                if isinstance(rounds, dict):
                    rounds.pop((symbol, batch_id, identity), None)
                print(f"  └─ ℹ️ [自愈] 条目 {identity} 订单 {order_id} 已终结(status={_status})，标 ABSENT")
                continue
            # B2-1: FOUND ≠ CONFIRMED —— FOUND + intent 完整匹配 = CONFIRMED（ChatGPT②）
            intent = entry.get('intent')
            if not intent:
                # 无 intent（旧条目/未落盘）：保守不收编，维持原状态等待人工核实
                print(f"  └─ ⚠️ [自愈] 条目 {identity} 无 intent 指纹，保守不收编 (order_id={order_id})")
                continue
            if not self._order_matches_intent(order, intent, symbol):
                # F4b（2026-08-21）：先判断是否启动前历史条目（用原 updated_at，勿在状态更新后判断）
                is_legacy_entry = self._is_stale_pre_launch_entry(entry)
                # FOUND 但字段不匹配 → 错误订单/旧订单/其他批次订单：MISMATCH + critical + 不收编
                entry['state'] = 'MISMATCH'
                entry['updated_at'] = time.time()
                changed = True
                if is_legacy_entry:
                    # F4b：启动前历史条目的状态同步 ≠ 新资金风险 → 降级为 info，不 TG/邮件
                    print(f"  └─ ℹ️ [自愈] 历史条目 {identity} 与交易所实况不符，启动窗口降级（不告警）")
                else:
                    self.send_tg_notification(
                        f"🚨 **自愈检测到订单意图不匹配（MISMATCH）**\n"
                        f"🆔 批次：`{batch_id}` 身份：`{identity}`\n"
                        f"📌 订单 `{order_id}` 与 registry 意图不符，【不收编】\n"
                        f"🛠️ 请到交易所人工核实该订单！",
                        level='critical')
                continue
            # FOUND + intent 完整匹配 → CONFIRMED + 收编（只补 Commit 不新建）
            role = entry.get('role')
            if role == 'SL' and b.get('current_sl_id') is None:
                b['current_sl_id'] = order_id
            elif role == 'TP' and b.get('tp_order_id') is None:
                b['tp_order_id'] = order_id
            entry['state'] = 'CONFIRMED'
            entry['updated_at'] = time.time()
            changed = True
            # R-B: 确认成功 → 清零持续未确认轮次
            rounds = getattr(self, '_self_heal_unconfirmed_rounds', None)
            if isinstance(rounds, dict):
                rounds.pop((symbol, batch_id, identity), None)
        if changed:
            self.save_batch_state(symbol, batch_id, b)

    def _reconcile_stale_protection_layers(self, symbol, batch_id, role, keep_order_id=None):
        """R-C（事件3根因C）：滚动撤销链补强 —— 新层汇总保护单已确认后，撤销 registry 中
        同 role 旧层带 order_id 的单（防层叠重复：多张旧层单叠加理论平仓量 > 实际持仓）。
        事件3实证：verify 假阴性 → current_sl_id 恒 null → 旧层单永不撤销 → L0(0.43)+L1(0.817)
        层叠。本方法只撤销不创建（新单已确认，撤销旧单无空窗）：
          fetch 存在       → cancel + 条目终结（ABSENT）
          OrderNotFound    → 已不存在（被撤/已触发/从未成功）→ 条目终结（ABSENT）
          网络异常         → 结果未知，保留下轮重试（未知 ≠ 不存在，不误撤）
        keep_order_id：新挂汇总单 ID，跳过（不得撤销自己）。
        调用点：主循环补挂 SL/TP 成功段 + 预生成 SL/TP 成功段（均为新单已 Commit 后）。"""
        latest_all = self.load_all_states()
        b = latest_all.get(symbol, {}).get(batch_id)
        if b is None:
            return
        reg = b.get('protection_registry', {})
        changed = False
        for identity, entry in reg.items():
            if not isinstance(entry, dict):
                continue
            if entry.get('role') != role:
                continue
            order_id = entry.get('order_id')
            if not order_id or order_id == keep_order_id:
                continue
            # 只处理带 order_id 的条目（PENDING_CREATE 无 id 跳过；其余状态均以实况裁决）
            try:
                order = self._safe_api_call(self.exchange.fetch_order, order_id, symbol,
                                            params={'stop': True}, retries=1)
            except ccxt.OrderNotFound:
                entry['state'] = 'ABSENT'
                entry['updated_at'] = time.time()
                entry['terminated_reason'] = 'stale_layer_reconcile_not_found'
                changed = True
                print(f"  └─ 🧹 [滚动撤销] {role} 旧层 {identity} 已不存在(order_id={order_id})，条目终结")
                continue
            except Exception as e:
                # 网络异常 → 结果未知：保留下轮重试，不撤销（未知 ≠ 不存在）
                print(f"  └─ ⚠️ [滚动撤销] {role} 旧层 {identity} 查询失败，保留下轮: {e}")
                continue
            # F2（2026-08-21）：已终结订单（canceled/expired 等）→ 直接终结，不 cancel。
            # 实证：R-C fetch 已撤销订单返回 status=canceled（不抛异常）→ 旧代码进 cancel 分支
            # → cancel 已撤单抛 "Unknown order" → 跳过且不标 ABSENT（脏数据残留，如 TP L2 案例）。
            valid_statuses = ('new', 'open', 'active')
            _status = str(order.get('status', '')).lower()
            if _status and _status not in valid_statuses:
                entry['state'] = 'ABSENT'
                entry['updated_at'] = time.time()
                entry['terminated_reason'] = f'stale_layer_reconcile_status_{_status}'
                changed = True
                print(f"  └─ 🧹 [滚动撤销] {role} 旧层 {identity} 已终结(status={_status})，条目终结")
                continue
            # 订单真实存在 → 撤销（旧层单被新汇总单替代）
            try:
                self._safe_api_call(self.exchange.cancel_order, order_id, symbol,
                                    params={'stop': True}, retries=1)
                print(f"  └─ 🧹 [滚动撤销] 已撤销 {role} 旧层单 {identity} (order_id={order_id})")
            except Exception as e:
                if "Unknown order" in str(e) or "-2011" in str(e):
                    print(f"  └─ 🧹 [滚动撤销] {role} 旧层单 {order_id} 已不存在，跳过")
                else:
                    print(f"  └─ ⚠️ [滚动撤销] {role} 旧层单撤销失败，保留下轮: {e}")
                    continue
            entry['state'] = 'ABSENT'
            entry['updated_at'] = time.time()
            entry['terminated_reason'] = 'stale_layer_reconcile_cancelled'
            changed = True
        if changed:
            self.save_batch_state(symbol, batch_id, b)

    def _prune_pending_sl_by_registry(self, symbol, batch_id, pending_sl_orders):
        """R-D（事件3根因D）：registry 已有 order_id 的层无论 verify 结果都移出待挂列表。
        语义澄清：pending_sl_orders = "需要创建保护单"；create 已返回 id = 创建已发生 →
        不再待创建；NOT_CONFIRMED/PENDING_VERIFY 的确认/收编由 R-B 运行期自愈负责。
        事件3实证：NOT_CONFIRMED 层永不从 pending 移除 → 每轮补挂尝试被仲裁闸门拦截 →
        无限循环（gate 告警 3 次后静默，但层叠未解）。本方法按 registry 实况裁决：
          条目 role=SL 且 layer=idx 且 order_id 非空 → 移出 pending（无论状态）。
        直接原地修改调用方 list 并落盘；返回是否移除了任何层。"""
        if not pending_sl_orders:
            return False
        latest_all = self.load_all_states()
        b = latest_all.get(symbol, {}).get(batch_id)
        if not b:
            return False
        reg = b.get('protection_registry', {})
        removed = []
        for idx in list(pending_sl_orders):
            for entry in reg.values():
                if (isinstance(entry, dict) and entry.get('role') == 'SL'
                        and entry.get('layer') == idx and entry.get('order_id')):
                    pending_sl_orders.remove(idx)
                    removed.append(idx)
                    break
        if removed:
            b['pending_sl_orders'] = pending_sl_orders
            self.save_batch_state(symbol, batch_id, b)
            print(f"  └─ 📝 [R-D] registry 已有 order_id 的层 {removed} 移出待挂列表"
                  f"（确认由运行期自愈负责）")
        return bool(removed)

    def _self_heal_no_id(self, symbol, batch_id):
        """B2-6（规格 §6.3）：无 ID 身份签名匹配自愈——处理 PENDING_CREATE / PENDING_VERIFY(id_unknown)
        且无 order_id 的 ENTRY 条目。签名 = registry intent（B2-2 已落盘 6 字段）：
          拉双通道 open orders 快照（normal + params={'stop':True}）合并成统一视图（§8 OrderSnapshot 语义：
          任一通道失败 → view INVALID）：
            命中且唯一   → CONFIRMED + 记录真实 order_id + id_known=True（收编，绝不 Create）
            命中多条     → NOT_CONFIRMED + critical（人工裁决，禁止自动收编多条）
            未命中（快照 VALID）→ NOT_CONFIRMED（缺席≠从未存在：单可能已触发终结，不变量①）
            快照 INVALID → 维持 PENDING_VERIFY(id_unknown)（结果未知，静默下轮再试，不误判无单）
        恢复路径只 Commit 不 Create（§6 恢复总原则：恢复不扩大风险）。"""
        latest_all = self.load_all_states()
        b = latest_all.get(symbol, {}).get(batch_id)
        if b is None:
            return
        reg = b.get('protection_registry', {})
        # 收集需无 ID 自愈的目标（ENTRY + 无 order_id + 未决态 + 有签名）
        targets = []
        for identity, entry in reg.items():
            if not isinstance(entry, dict):
                continue
            if entry.get('role') != 'ENTRY' or entry.get('order_id'):
                continue  # 已有 ID → 走 verify 路径（_recheck_registry_self_heal）
            if entry.get('state') not in ('PENDING_CREATE', 'PENDING_VERIFY'):
                continue
            if not entry.get('intent'):
                continue  # 无签名（旧数据）→ 保守维持，等人工
            targets.append((identity, entry))
        if not targets:
            return
        # 双通道快照（§8 OrderSnapshot）：任一通道失败 → INVALID
        try:
            normal = self._safe_api_call(self.exchange.fetch_open_orders, symbol, retries=1)
        except Exception:
            normal = None
        try:
            conditional = self._safe_api_call(self.exchange.fetch_open_orders, symbol,
                                              params={'stop': True}, retries=1)
        except Exception:
            conditional = None
        if normal is None or conditional is None:
            # 快照 INVALID → 全部目标转 PENDING_VERIFY(id_unknown)，静默下轮再试（不误判无单）
            for _, entry in targets:
                if entry.get('state') != 'PENDING_VERIFY' or entry.get('id_known'):
                    entry['state'] = 'PENDING_VERIFY'
                    entry['id_known'] = False
                    entry['updated_at'] = time.time()
            self.save_batch_state(symbol, batch_id, b)
            return
        # 合并统一视图（双通道按 id 去重，normal 优先保留字段）
        orders_by_id = {}
        for o in (normal or []):
            if isinstance(o, dict) and o.get('id'):
                orders_by_id.setdefault(o['id'], o)
        for o in (conditional or []):
            if isinstance(o, dict) and o.get('id'):
                orders_by_id.setdefault(o['id'], o)
        changed = False
        for identity, entry in targets:
            intent = entry.get('intent')
            matches = [oid for oid, o in orders_by_id.items()
                       if self._order_matches_intent(o, intent, symbol)]
            if len(matches) == 1:
                # 命中且唯一 → 收编 CONFIRMED + 记录真实 order_id（24 孤儿单通用防线）
                entry['state'] = 'CONFIRMED'
                entry['order_id'] = matches[0]
                entry['id_known'] = True
                entry['updated_at'] = time.time()
                changed = True
            elif len(matches) > 1:
                # 命中多条 → NOT_CONFIRMED + critical（人工裁决，禁止自动收编多条）
                entry['state'] = 'NOT_CONFIRMED'
                entry['updated_at'] = time.time()
                changed = True
                self.send_tg_notification(
                    f"🚨 **身份签名匹配命中多条订单（禁止自动收编）**\n"
                    f"🆔 批次：`{batch_id}`\n"
                    f"📌 身份：`{identity}`\n"
                    f"⚠️ 快照中 {len(matches)} 条订单签名相同，【未收编】\n"
                    f"🛠️ 请到交易所人工核实后按 §5.5 规范处理！",
                    level='critical')
            else:
                # 快照 VALID 但未命中 → NOT_CONFIRMED（缺席≠从未存在：单可能已触发终结）
                entry['state'] = 'NOT_CONFIRMED'
                entry['updated_at'] = time.time()
                changed = True
        if changed:
            self.save_batch_state(symbol, batch_id, b)

    def _rebuild_entry_orders_from_registry(self, symbol, batch_id):
        """B2-6（§6.3 + Case F）：从 protection_registry 重建 ENTRY 链——扫描全部
        state=CONFIRMED 且带 order_id 的 ENTRY 条目，按 layer 升序收编真实 order_id → entry_orders；
        同时用骨架元数据（entry_layers/entry_stop_steps/params_base/is_hedge_mode/side）重建
        stop_steps / target_amounts / batch_total_amount / layer_sl_params / prepared_tp_params /
        pending_sl_orders（恢复接管监控必需——否则层成交后 SL/TP 无参数可挂，违反不变量②）。
        返回 (entry_orders: list, rebuilt: bool)。恢复路径只 Commit 不 Create。"""
        latest_all = self.load_all_states()
        b = latest_all.get(symbol, {}).get(batch_id)
        if b is None:
            return [], False
        reg = b.get('protection_registry', {})
        confirmed = []
        for identity, entry in reg.items():
            if not isinstance(entry, dict):
                continue
            if entry.get('role') != 'ENTRY' or entry.get('state') != 'CONFIRMED':
                continue
            oid = entry.get('order_id')
            if not oid:
                continue
            confirmed.append((int(entry.get('layer', 0)), oid, entry))
        if not confirmed:
            return [], False
        confirmed.sort(key=lambda x: x[0])
        entry_orders = [oid for _, oid, _ in confirmed]
        b['entry_orders'] = entry_orders
        # 骨架元数据重建（entry_layers 提供 layer→序号映射）
        entry_layers = b.get('entry_layers') or []
        entry_stop_steps = b.get('entry_stop_steps') or []
        params_base = b.get('params_base') or {}
        is_hedge_mode = bool(b.get('is_hedge_mode', False))
        side = b.get('side', 'BUY')
        sl_side = 'sell' if side == 'BUY' else 'buy'
        stop_steps = []
        target_amounts = []
        layer_sl_params = []
        batch_total_amount = 0.0
        for layer, oid, entry in confirmed:
            qty = (entry.get('intent') or {}).get('qty')
            try:
                qty = float(qty) if qty is not None else 0.0
            except (TypeError, ValueError):
                qty = 0.0
            target_amounts.append(qty)
            batch_total_amount += qty
            if layer in entry_layers:
                idx_in_skeleton = entry_layers.index(layer)
                if idx_in_skeleton < len(entry_stop_steps):
                    raw_sl_price = entry_stop_steps[idx_in_skeleton]
                else:
                    raw_sl_price = 0.0
            else:
                raw_sl_price = 0.0
            try:
                formatted_sl_price = float(self.exchange.price_to_precision(symbol, raw_sl_price))
            except Exception:
                formatted_sl_price = float(raw_sl_price or 0.0)
            stop_steps.append(raw_sl_price)
            sl_params = dict(params_base)
            sl_params['stopPrice'] = formatted_sl_price
            if not is_hedge_mode:
                sl_params['reduceOnly'] = True
            layer_sl_params.append({
                'symbol': symbol,
                'type': 'STOP_MARKET',
                'side': sl_side,
                'amount': qty,
                'params': sl_params,
            })
        if stop_steps:
            b['stop_steps'] = stop_steps
        b['target_amounts'] = target_amounts
        b['batch_total_amount'] = float(self.exchange.amount_to_precision(symbol, batch_total_amount))
        b['layer_sl_params'] = layer_sl_params
        # prepared_tp_params（止盈首次成交时挂，_place_prepared_orders_immediately 依赖）
        try:
            formatted_tp_price = float(self.exchange.price_to_precision(symbol, b.get('take_profit_price')))
        except Exception:
            formatted_tp_price = float(b.get('take_profit_price') or 0.0)
        tp_params = dict(params_base)
        tp_params['stopPrice'] = formatted_tp_price
        if not is_hedge_mode:
            tp_params['reduceOnly'] = True
        b['prepared_tp_params'] = {
            'symbol': symbol,
            'type': 'TAKE_PROFIT_MARKET',
            'side': sl_side,
            'params': tp_params,
        }
        # pending_sl_orders：骨架阶段 last_filled_count=0（无成交）→ 全部层待挂 SL
        if not b.get('last_filled_count'):
            b['pending_sl_orders'] = list(range(len(entry_orders)))
        b['updated_at'] = time.time()
        self.save_batch_state(symbol, batch_id, b)
        return entry_orders, True

    def _verify_failure_msg(self, desc, order_id, symbol, verify_result):
        """C5/SG4: Verify 失败统一消息（unknown → 人工核实，防双单复活）。"""
        if verify_result == 'unknown':
            return (f"🚨 **订单创建结果未知（UNKNOWN）**\n"
                    f"📌 {desc} ID `{order_id}` ({symbol})\n"
                    f"⚠️ 网络异常，无法确认该订单是否已在交易所创建\n"
                    f"💡 程序【未记录】此订单（不 Commit），也不会自动补单\n"
                    f"🛠️ 请到交易所核实是否存在该订单，避免重复挂单！")
        return (f"❌ **订单创建验证失败（NOT_FOUND）**\n"
                f"📌 {desc} ID `{order_id}` ({symbol})\n"
                f"⚠️ 交易所返回订单不存在，程序【未记录】此订单（不 Commit）。")

    def _start_monitoring(self, symbol: str, batch_id: str, entry_orders: list, stop_steps: list,
                          take_profit_price: float,
                          current_sl_id: str, tp_order_id: str, batch_total_amount: float, target_amounts: list,
                          params_base: dict, is_hedge_mode: bool, side: str, last_filled_count: int = 0,
                          filled_details: list = None, total_entry_fee: float = 0.0,
                          pending_sl_orders: list = None,
                          prepared_tp_params: dict = None,
                          layer_sl_params: list = None):

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
        # R-B: 运行期周期自愈重查时间戳（每 registry_self_heal_interval 秒一次）
        last_registry_self_heal_time = 0
        # P1-2: 连续网络错误计数器（用于动态降速，避免加重限流）
        consecutive_network_errors = 0

        if filled_details is None or len(filled_details) != len(entry_orders):
            filled_details = [0.0] * len(entry_orders)

        if layer_sl_params is None:
            layer_sl_params = []

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
                if consecutive_network_errors > 0:
                    # 🔥 连续网络错误 → 动态 ×3 降速（封顶 5 分钟），避免错误重试加重限流（P1-2）
                    sleep_interval = min(sleep_interval * 3, 300.0)
                if fast_poll_count > 0:
                    sleep_interval = min(sleep_interval, 3.0)
                    fast_poll_count -= 1

                time.sleep(sleep_interval)
                self._sync_time_if_needed()

                # 🔥 R-B: 运行期周期自愈重查（事件3根因B）——每 ~30s 重查一次 registry 未决条目
                #（PENDING_VERIFY/NOT_CONFIRMED）：FOUND+intent 匹配 → CONFIRMED + 收编 Commit，
                # 解开"verify 假阴性 → 永久卡死"（原自愈只在启动恢复调用一次，运行期零机制）。
                now = time.time()
                if now - last_registry_self_heal_time >= self.registry_self_heal_interval:
                    last_registry_self_heal_time = now
                    try:
                        self._recheck_registry_self_heal(symbol, batch_id)
                    except Exception as e:
                        print(f"  └─ ⚠️ [自愈] registry 周期重查异常: {e}")

                # 🔥 定期主动检测 IP（每 5 分钟）
                now = time.time()
                if now - self.last_ip_check_time > self.IP_CHECK_INTERVAL:
                    self._check_ip_periodically()
                    self.last_ip_check_time = now

                open_orders_map = {}
                try:
                    open_orders = self._safe_api_call(self.exchange.fetch_open_orders, symbol)
                    open_orders_map = {str(ord['id']): ord for ord in open_orders}
                    consecutive_network_errors = 0
                except Exception as e:
                    consecutive_network_errors += 1
                    print(f"⚠️ 获取未结订单失败 (连续 {consecutive_network_errors} 次，已降速)，等待下一次轮询: {e}")
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
                                        prepared_tp_params, layer_sl_params,
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

                            sl_side_ident = 'LONG' if side == 'BUY' else 'SHORT'
                            sl_identity = self._protection_identity(batch_id, 'SL', sl_idx, sl_side_ident)
                            try:
                                # B2-8: Create 仲裁闸门（§5.7 #5 换挂语义）——旧单 CONFIRMED + replace_order_id
                                # 匹配 → 放行（确认旧单将物理离开）；未决态/硬锁/错单 → 拒绝，保留旧单保护
                                allowed, gate_reason = self._assert_create_allowed(
                                    symbol, batch_id, sl_identity, desc='部分减仓换挂止损',
                                    replace_order_id=current_sl_id)
                                if not allowed:
                                    print(f"  └─ 🚫 [仲裁] 跳过部分减仓换挂止损: {gate_reason}")
                                    self._gate_alert_notify(
                                        sl_identity, gate_reason,
                                        f"⚠️ 部分减仓后止损换挂被仲裁拦截（旧单保留）\n"
                                        f"🆔 批次：`{batch_id}`\n📌 {gate_reason}",
                                        level='warning')
                                else:
                                    # B2-2: 意图先落盘（崩溃安全 Create）+ intent 指纹
                                    self._update_registry(symbol, batch_id, sl_identity, state='PENDING_CREATE',
                                                          id_known=False, order_kind='conditional', role='SL',
                                                          layer=sl_idx, side=sl_side_ident,
                                                          intent=self._build_intent(
                                                              symbol=symbol,
                                                              side='sell' if side == 'BUY' else 'buy',
                                                              qty=batch_filled_amount,
                                                              order_type='STOP_MARKET',
                                                              stop_price=formatted_sl_price,
                                                              reduce_only=sl_params.get('reduceOnly')))
                                    new_sl_order = self._safe_api_call(
                                        self.exchange.create_order,
                                        symbol=symbol,
                                        type='STOP_MARKET',
                                        side='sell' if side == 'BUY' else 'buy',
                                        amount=batch_filled_amount,
                                        params=sl_params,
                                        retries=1
                                    )
                                    # B2-0 Verify 统一入口：success→CONFIRMED；not_found→NOT_CONFIRMED；unknown→PENDING_VERIFY
                                    verify_result = self._verify_and_update_registry(
                                        symbol, batch_id, sl_identity, new_sl_order['id'], desc='部分减仓换挂止损')
                                    if verify_result != 'success':
                                        print(f"  └─ ❌ 新止损单验证失败({verify_result})，不 Commit/不撤旧: {new_sl_order['id']}")
                                        self.send_tg_notification(
                                            self._verify_failure_msg("止损更新单", new_sl_order['id'], symbol, verify_result),
                                            level='critical' if verify_result == 'unknown' else 'warning')
                                    else:
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

                            tp_side_ident = 'LONG' if side == 'BUY' else 'SHORT'
                            tp_identity = self._protection_identity(batch_id, 'TP', sl_idx, tp_side_ident)
                            try:
                                # B2-8: Create 仲裁闸门（§5.7 #6 换挂语义）——旧单 CONFIRMED + replace_order_id
                                # 匹配 → 放行（确认旧单将物理离开）；未决态/硬锁/错单 → 拒绝，保留旧单保护
                                allowed, gate_reason = self._assert_create_allowed(
                                    symbol, batch_id, tp_identity, desc='部分减仓换挂止盈',
                                    replace_order_id=tp_order_id)
                                if not allowed:
                                    print(f"  └─ 🚫 [仲裁] 跳过部分减仓换挂止盈: {gate_reason}")
                                    self._gate_alert_notify(
                                        tp_identity, gate_reason,
                                        f"⚠️ 部分减仓后止盈换挂被仲裁拦截（旧单保留）\n"
                                        f"🆔 批次：`{batch_id}`\n📌 {gate_reason}",
                                        level='warning')
                                else:
                                    # B2-2: 意图先落盘（崩溃安全 Create）+ intent 指纹
                                    self._update_registry(symbol, batch_id, tp_identity, state='PENDING_CREATE',
                                                          id_known=False, order_kind='conditional', role='TP',
                                                          layer=sl_idx, side=tp_side_ident,
                                                          intent=self._build_intent(
                                                              symbol=symbol,
                                                              side='sell' if side == 'BUY' else 'buy',
                                                              qty=batch_filled_amount,
                                                              order_type='TAKE_PROFIT_MARKET',
                                                              stop_price=formatted_tp_price,
                                                              reduce_only=tp_params.get('reduceOnly')))
                                    new_tp_order = self._safe_api_call(
                                        self.exchange.create_order,
                                        symbol=symbol,
                                        type='TAKE_PROFIT_MARKET',
                                        side='sell' if side == 'BUY' else 'buy',
                                        amount=batch_filled_amount,
                                        params=tp_params,
                                        retries=1
                                    )
                                    # B2-0 Verify 统一入口：success→CONFIRMED；not_found→NOT_CONFIRMED；unknown→PENDING_VERIFY
                                    verify_result = self._verify_and_update_registry(
                                        symbol, batch_id, tp_identity, new_tp_order['id'], desc='部分减仓换挂止盈')
                                    if verify_result != 'success':
                                        print(f"  └─ ❌ 新止盈单验证失败({verify_result})，不 Commit/不撤旧: {new_tp_order['id']}")
                                        self.send_tg_notification(
                                            self._verify_failure_msg("止盈更新单", new_tp_order['id'], symbol, verify_result),
                                            level='critical' if verify_result == 'unknown' else 'warning')
                                    else:
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
                            'user_modified': latest_b_data.get('user_modified', False) if latest_b_data else False,  # R13-B: 保留现有值，不得硬编码覆盖
                            'pending_sl_orders': pending_sl_orders,
                            'prepared_tp_params': prepared_tp_params,
                            'layer_sl_params': layer_sl_params,
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
                # F3（2026-08-21 事件4）：与 TP R14 对称——补挂前先裁决 registry 实况，
                # 防"registry CONFIRMED + current_sl_id 丢失"死锁（闸门永久拦截补挂）。
                if not current_sl_id and has_entered_position and batch_filled_amount > 0:
                    sl_identity_r14 = self._protection_identity(
                        batch_id, 'SL', batch_filled_count - 1,
                        params_base.get('positionSide', 'LONG' if side == 'BUY' else 'SHORT'))
                    verdict, found_id = self._adjudicate_recreate_before_repair(
                        symbol, batch_id, sl_identity_r14)
                    if verdict == 'allow':
                        need_recover_sl = True
                        print(f"⚠️ [SL 补挂] 批次 {batch_id} 止损单缺失(未创建或创建失败)，准备补挂...")
                    elif verdict == 'adopt' and found_id:
                        current_sl_id = found_id
                        print(f"✅ [F3 收养] 批次 {batch_id} 止损单实为在场 ({found_id})，收养防双挂")
                        try:
                            _lb = self.load_all_states().get(symbol, {}).get(batch_id, {})
                            if _lb:
                                _lb['current_sl_id'] = found_id
                                self.save_batch_state(symbol, batch_id, _lb)
                        except Exception:
                            pass
                    elif verdict == 'mismatch':
                        print(f"🚫 [F3 裁决] 批次 {batch_id} 止损单在场但不匹配，已 critical 告警，不自动处理")
                    else:
                        print(f"⏸️ [F3 裁决] 批次 {batch_id} 止损单结果未知，保守保留下轮")

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
                            # F2（2026-08-21 事件4）：物理单已终结 → registry 同步终结为 ABSENT。
                            # 否则 CONFIRMED 条目永不终结 → 后续补挂被闸门永久拦截（死锁根因）。
                            # 遍历 registry 按 order_id 精确匹配 identity（防 layer 漂移），找不到再回退最新层。
                            _latest_check = self.load_all_states().get(symbol, {}).get(batch_id, {})
                            _reg_target = None
                            _reg_fallback = False
                            for _k, _v in (_latest_check.get('protection_registry') or {}).items():
                                if str(_v.get('order_id', '')) == str(sl_id_str):
                                    _reg_target = _k
                                    break
                            if _reg_target is None:
                                # 第二轮审查（2026-08-21）：fallback 用独立 reason 落盘，审计可区分
                                # "精确匹配终结" 与 "回退猜测终结"（误终结由 F3 adopt/mismatch 兜底）
                                _reg_fallback = True
                                _reg_target = self._protection_identity(
                                    batch_id, 'SL', batch_filled_count - 1,
                                    params_base.get('positionSide',
                                                    'LONG' if side == 'BUY' else 'SHORT'))
                            self._update_registry(symbol, batch_id, _reg_target,
                                                  state='ABSENT',
                                                  terminated_reason=(f'terminal_status_{sl_status}_fallback'
                                                                     if _reg_fallback
                                                                     else f'terminal_status_{sl_status}'))
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

                # SG3-P1: 订单存在 ≠ 保护有效——SL 在 open_orders_map 中时校验方向/保护语义/数量
                if current_sl_id and (str(current_sl_id) in open_orders_map) and has_entered_position and batch_filled_amount > 0:
                    sl_ord = open_orders_map.get(str(current_sl_id))
                    if sl_ord is not None:
                        expected_side = 'sell' if side == 'BUY' else 'buy'
                        position_side = (params_base or {}).get('positionSide', 'BOTH')
                        valid, reason = self._check_protection_order_validity(
                            sl_ord, expected_side, is_hedge_mode, position_side, batch_filled_amount)
                        if not valid:
                            dedup_key = (batch_id, str(current_sl_id), reason)
                            if dedup_key not in self._sg3_alerted:
                                self._sg3_alerted.add(dedup_key)
                                self.send_tg_notification(
                                    f"⚠️ [SG3-P1] 批次 {batch_id} 止损单异常（{reason}），"
                                    f"{'已通知用户，不自动修改（用户已接管）' if user_modified else '程序将自动撤销重挂'}",
                                    level='critical')
                            if user_modified:
                                print(f"ℹ️ [SG3-P1] 批次 {batch_id} 止损单无效({reason})，用户已接管，仅告警不自动修复")
                            else:
                                print(f"⚠️ [SG3-P1] 批次 {batch_id} 止损单无效({reason})，准备撤销重挂...")
                                need_recover_sl = True
                        else:
                            # 订单已恢复有效 → 清理该订单节流记录，允许下次异常再报
                            self._sg3_alerted = {
                                k for k in self._sg3_alerted
                                if not (k[0] == batch_id and k[1] == str(current_sl_id))}

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
                                # F2（2026-08-21 事件4）：物理单已终结 → registry 同步终结为 ABSENT。
                                # 否则 CONFIRMED 条目永不终结 → 后续补挂被闸门永久拦截（死锁根因）。
                                _latest_check = self.load_all_states().get(symbol, {}).get(batch_id, {})
                                _reg_target = None
                                _reg_fallback = False
                                for _k, _v in (_latest_check.get('protection_registry') or {}).items():
                                    if str(_v.get('order_id', '')) == str(tp_id_str):
                                        _reg_target = _k
                                        break
                                if _reg_target is None:
                                    # 第二轮审查（2026-08-21）：fallback 用独立 reason 落盘（同 SL 段）
                                    _reg_fallback = True
                                    _reg_target = self._protection_identity(
                                        batch_id, 'TP', batch_filled_count - 1,
                                        params_base.get('positionSide',
                                                        'LONG' if side == 'BUY' else 'SHORT'))
                                self._update_registry(symbol, batch_id, _reg_target,
                                                      state='ABSENT',
                                                      terminated_reason=(f'terminal_status_{tp_status}_fallback'
                                                                         if _reg_fallback
                                                                         else f'terminal_status_{tp_status}'))
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

                # SG3-P1: 订单存在 ≠ 保护有效——TP 在 open_orders_map 中时校验（与 SL 对称）
                if tp_order_id and (str(tp_order_id) in open_orders_map) and has_entered_position and batch_filled_amount > 0:
                    tp_ord = open_orders_map.get(str(tp_order_id))
                    if tp_ord is not None:
                        expected_side = 'sell' if side == 'BUY' else 'buy'
                        position_side = (params_base or {}).get('positionSide', 'BOTH')
                        valid, reason = self._check_protection_order_validity(
                            tp_ord, expected_side, is_hedge_mode, position_side, batch_filled_amount)
                        if not valid:
                            dedup_key = (batch_id, str(tp_order_id), reason)
                            if dedup_key not in self._sg3_alerted:
                                self._sg3_alerted.add(dedup_key)
                                self.send_tg_notification(
                                    f"⚠️ [SG3-P1] 批次 {batch_id} 止盈单异常（{reason}），"
                                    f"{'已通知用户，不自动修改（用户已接管）' if user_modified else '程序将自动撤销重挂'}",
                                    level='critical')
                            if user_modified:
                                print(f"ℹ️ [SG3-P1] 批次 {batch_id} 止盈单无效({reason})，用户已接管，仅告警不自动修复")
                            else:
                                print(f"⚠️ [SG3-P1] 批次 {batch_id} 止盈单无效({reason})，准备撤销重挂...")
                                need_recover_tp = True
                        else:
                            # 订单已恢复有效 → 清理该订单节流记录，允许下次异常再报
                            self._sg3_alerted = {
                                k for k in self._sg3_alerted
                                if not (k[0] == batch_id and k[1] == str(tp_order_id))}

                # R14 + F3: TP 从未创建成功(tp_order_id is None)时，如果有持仓且未用户修改，标记需要补挂
                # F3（2026-08-21 事件4）：补挂前先裁决 registry 实况——治愈"registry CONFIRMED +
                # 批次级 id 丢失"死锁态：物理单已终结 → 放行补挂；仍在场 → 收养防双挂；不匹配 → 告警。
                if tp_order_id is None and has_entered_position and batch_filled_amount > 0 and not user_modified:
                    tp_identity_r14 = self._protection_identity(
                        batch_id, 'TP', batch_filled_count - 1,
                        params_base.get('positionSide', 'LONG' if side == 'BUY' else 'SHORT'))
                    verdict, found_id = self._adjudicate_recreate_before_repair(
                        symbol, batch_id, tp_identity_r14)
                    if verdict == 'allow':
                        need_recover_tp = True
                        print(f"⚠️ [TP 补挂] 批次 {batch_id} 止盈单缺失(未创建或创建失败)，准备补挂...")
                    elif verdict == 'adopt' and found_id:
                        tp_order_id = found_id
                        print(f"✅ [F3 收养] 批次 {batch_id} 止盈单实为在场 ({found_id})，收养防双挂")
                        # 补批次级 id 落盘（收养后 R14 不再触发，且风控段可直接复用）
                        try:
                            _lb = self.load_all_states().get(symbol, {}).get(batch_id, {})
                            if _lb:
                                _lb['tp_order_id'] = found_id
                                self.save_batch_state(symbol, batch_id, _lb)
                        except Exception:
                            pass
                    elif verdict == 'mismatch':
                        print(f"🚫 [F3 裁决] 批次 {batch_id} 止盈单在场但不匹配，已 critical 告警，不自动处理")
                    else:
                        print(f"⏸️ [F3 裁决] 批次 {batch_id} 止盈单结果未知，保守保留下轮")

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
                    # R-D（事件3根因D）：registry 已有 order_id 的层无论 verify 结果都移出待挂列表
                    #（create 已返回 id = 创建已发生；NOT_CONFIRMED/PENDING_VERIFY 由 R-B 运行期
                    # 自愈重查确认/收编）→ 防"闸门拦截 + pending 永不清空"的无限循环
                    self._prune_pending_sl_by_registry(symbol, batch_id, pending_sl_orders)
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
                            # F1（2026-08-21 事件4）：替换旧单前先过仲裁闸门（replace 语义）——
                            # CONFIRMED + replace_order_id==entry.order_id → 放行先撤后建；
                            # 未决态/硬锁 → 拒绝替换（保留原单、不撤销、不创建，等自愈/人工）。
                            # 原结构"先撤销再闸门检查（未传 replace_order_id）"→ CONFIRMED 拦截 →
                            # current_sl_id=None 落盘 → 下轮缺失检测又补挂 → 闸门永久拦截（死锁）。
                            sl_identity_pre = self._protection_identity(
                                batch_id, 'SL', batch_filled_count - 1,
                                params_base.get('positionSide', 'LONG' if side == 'BUY' else 'SHORT'))
                            allowed_r, reason_r = self._assert_create_allowed(
                                symbol, batch_id, sl_identity_pre, desc='替换止损单',
                                replace_order_id=old_sl_id)
                            if not allowed_r:
                                # 拒绝替换 → 保留原单（old_sl_id 不清空 → 下方创建分支自然跳过）
                                print(f"  └─ 🚫 [仲裁] 跳过替换止损单（保留原单）: {reason_r}")
                                self._gate_alert_notify(
                                    sl_identity_pre, reason_r,
                                    f"⚠️ **止损单替换被仲裁拦截**\n"
                                    f"🆔 批次：`{batch_id}`\n"
                                    f"📌 {reason_r}\n"
                                    f"💡 程序保留原单不重复挂单，等待自愈重查确认",
                                    level='warning')
                            else:
                                try:
                                    self._safe_api_call(self.exchange.cancel_order, old_sl_id, symbol,
                                                        params={'stop': True})
                                    print(f"  └─ 已撤销旧止损单: {old_sl_id} → registry ABSENT")
                                    # F1: 撤销确认 → registry 终结为 ABSENT（旧单物理离开 → 允许安全重建）
                                    self._update_registry(symbol, batch_id, sl_identity_pre,
                                                          state='ABSENT',
                                                          terminated_reason='canceled_by_update_replace')
                                    old_sl_id = None
                                except Exception as e:
                                    if "Unknown order" in str(e) or "-2011" in str(e):
                                        print(f"  └─ 旧止损单 {old_sl_id} 已不存在 → registry ABSENT")
                                        self._update_registry(symbol, batch_id, sl_identity_pre,
                                                              state='ABSENT',
                                                              terminated_reason='order_not_found_on_replace')
                                        old_sl_id = None
                                    else:
                                        # F1: 网络异常 fail-closed——不清 id、不创建，保留下轮（防双单）
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
                                sl_identity = self._protection_identity(
                                    batch_id, 'SL', batch_filled_count - 1,
                                    params_base.get('positionSide', 'LONG' if side == 'BUY' else 'SHORT'))
                                try:
                                    # B2-3: Create 仲裁闸门（§5.3）—— 同 identity 未决/已确认 → 禁新 create
                                    #（NOT_CONFIRMED/PENDING_VERIFY 残留时不得再次 create：C5 重挂变体封堵）
                                    allowed, gate_reason = self._assert_create_allowed(
                                        symbol, batch_id, sl_identity, desc='补挂止损单')
                                    if not allowed:
                                        if gate_reason.startswith('HARD_LOCK'):
                                            # B2-4: 硬锁静默（进入时已 critical，此后不重复告警）
                                            print(f"  └─ 🔒 [硬锁] 跳过补挂止损单: {gate_reason}")
                                        else:
                                            print(f"  └─ 🚫 [仲裁] 跳过补挂止损单: {gate_reason}")
                                            self._gate_alert_notify(
                                                sl_identity, gate_reason,
                                                f"⚠️ **止损单创建被仲裁拦截**\n"
                                                f"🆔 批次：`{batch_id}`\n"
                                                f"📌 {gate_reason}\n"
                                                f"💡 程序不重复挂单，等待自愈重查确认",
                                                level='warning')
                                        current_sl_id = None
                                        sl_success = False
                                    else:
                                        # B2-2: 意图先落盘（崩溃安全）+ intent 指纹
                                        self._update_registry(symbol, batch_id, sl_identity,
                                                              state='PENDING_CREATE', id_known=False,
                                                              order_kind='conditional', role='SL',
                                                              layer=batch_filled_count - 1,
                                                              side=params_base.get('positionSide',
                                                                                   'LONG' if side == 'BUY' else 'SHORT'),
                                                              intent=self._build_intent(
                                                                  symbol=symbol, side=sl_side,
                                                                  qty=batch_filled_amount,
                                                                  order_type='STOP_MARKET',
                                                                  stop_price=sl_params.get('stopPrice'),
                                                                  reduce_only=sl_params.get('reduceOnly')))
                                        new_sl_order = self._safe_api_call(
                                            self.exchange.create_order,
                                            symbol=symbol,
                                            type='STOP_MARKET',
                                            side=sl_side,
                                            amount=batch_filled_amount,
                                            params=sl_params,
                                            retries=1
                                        )
                                        # B2-0 Verify 统一入口：success→CONFIRMED；not_found→NOT_CONFIRMED
                                        #（不 raise/不计数/不自动重挂）；unknown→PENDING_VERIFY（不计数不补单）
                                        verify_result = self._verify_and_update_registry(
                                            symbol, batch_id, sl_identity, new_sl_order['id'], desc='补挂止损单')
                                        if verify_result != 'success':
                                            current_sl_id = None
                                            sl_success = False
                                            print(f"  └─ ❌ 止损单验证失败({verify_result})，不 Commit/不补单/不重挂: {new_sl_order['id']}")
                                            self.send_tg_notification(
                                                self._verify_failure_msg("止损单", new_sl_order['id'], symbol, verify_result),
                                                level='critical' if verify_result == 'unknown' else 'warning')
                                        else:
                                            current_sl_id = new_sl_order['id']
                                            sl_success = True
                                            print(f"  └─ ✅ 止损单已挂出: {formatted_new_sl_price} (ID: {current_sl_id})")
                                            # R-C（事件3根因C）：滚动撤销链补强——新汇总单已确认，
                                            # 撤销 registry 中旧层同 role 单（防层叠重复：理论平仓量 > 实际持仓）
                                            self._reconcile_stale_protection_layers(
                                                symbol, batch_id, 'SL', keep_order_id=current_sl_id)

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

                                    # B2-0: create 异常按操作阶段分流（ChatGPT①）——
                                    # unknown（NetworkError 等）→ PENDING_VERIFY(id_unknown) 不计数不熔断
                                    # 不降级恢复（可能已创建→再补=双单）；failed → 原计数+熔断+降级恢复
                                    create_unknown = (self._classify_create_exception(e) == 'unknown')
                                    if create_unknown:
                                        sl_identity = self._protection_identity(
                                            batch_id, 'SL', batch_filled_count - 1,
                                            params_base.get('positionSide', 'LONG' if side == 'BUY' else 'SHORT'))
                                        self._update_registry(symbol, batch_id, sl_identity,
                                                              state='PENDING_VERIFY', id_known=False,
                                                              order_kind='conditional')
                                        self.send_tg_notification(
                                            f"🚨 **止损单创建结果未知（UNKNOWN）**\n"
                                            f"🆔 批次：`{batch_id}`\n"
                                            f"📊 第 {batch_filled_count} 层\n"
                                            f"⚠️ 网络异常，无法确认止损单是否已创建\n"
                                            f"💡 程序【不计数】【不自动补单】，请到交易所核实！",
                                            level='critical'
                                        )
                                        old_sl_price = None
                                        old_sl_amount = None
                                    else:
                                        # 🔥 记录失败次数（仅确定拒绝 failed → FAILED 允许再次 Create）
                                        sl_identity = self._protection_identity(
                                            batch_id, 'SL', batch_filled_count - 1,
                                            params_base.get('positionSide', 'LONG' if side == 'BUY' else 'SHORT'))
                                        new_fc = self._update_registry(symbol, batch_id, sl_identity,
                                                                       state='FAILED', id_known=False,
                                                                       order_kind='conditional',
                                                                       fail_count_incr=1)
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
                                        # B2-4: registry fail_count≥5 → HARD_LOCK（§5.4）——
                                        # 落盘硬锁标记 + 进入时 1 次 critical，此后闸门拦截静默
                                        if new_fc is not None and new_fc >= 5:
                                            self._update_registry(symbol, batch_id, sl_identity,
                                                                  hard_locked=True)
                                            self.send_tg_notification(
                                                f"🚨 **HARD_LOCK 硬锁触发**\n"
                                                f"🆔 批次：`{batch_id}`\n"
                                                f"📊 第 {batch_filled_count} 层（identity：`{sl_identity}`）\n"
                                                f"⚠️ 该 identity 连续确定失败 {new_fc} 次（≥5），已硬锁\n"
                                                f"💡 程序不再自动重挂。请人工核实持仓后按 §5.5 规范解锁"
                                                f"（写 unlock_reason/unlock_time/unlock_operator）",
                                                level='critical'
                                            )

                                    if old_sl_price and old_sl_amount and old_sl_amount > 0:
                                        recovery_identity = self._protection_identity(
                                            batch_id, 'SL', batch_filled_count - 1,
                                            params_base.get('positionSide', 'LONG' if side == 'BUY' else 'SHORT'))
                                        try:
                                            print(f"  └─ 🔄 降级保护：尝试用旧止损价 {old_sl_price} 恢复...")
                                            recovery_params = params_base.copy()
                                            recovery_params['stopPrice'] = old_sl_price
                                            if not is_hedge_mode:
                                                recovery_params['reduceOnly'] = True

                                            # B2-3: Create 仲裁闸门（§5.3）—— 同 identity 未决/已确认 → 禁新 create
                                            allowed, gate_reason = self._assert_create_allowed(
                                                symbol, batch_id, recovery_identity, desc='降级恢复止损单')
                                            if not allowed:
                                                if gate_reason.startswith('HARD_LOCK'):
                                                    # B2-4: 硬锁静默（进入时已 critical，此后不重复告警）
                                                    print(f"  └─ 🔒 [硬锁] 跳过降级恢复: {gate_reason}")
                                                else:
                                                    print(f"  └─ 🚫 [仲裁] 跳过降级恢复: {gate_reason}")
                                                    self._gate_alert_notify(
                                                        recovery_identity, gate_reason,
                                                        f"🚨 **降级恢复被仲裁拦截**\n"
                                                        f"🆔 批次：`{batch_id}`\n"
                                                        f"📌 {gate_reason}\n"
                                                        f"💡 程序不重复挂单，等待自愈重查确认；请关注持仓保护状态！",
                                                        level='critical')
                                                sl_success = False
                                            else:
                                                # B2-2: 意图先落盘（崩溃安全）+ intent 指纹
                                                self._update_registry(symbol, batch_id, recovery_identity,
                                                                      state='PENDING_CREATE', id_known=False,
                                                                      order_kind='conditional', role='SL',
                                                                      layer=batch_filled_count - 1,
                                                                      side=params_base.get('positionSide',
                                                                                           'LONG' if side == 'BUY' else 'SHORT'),
                                                                      intent=self._build_intent(
                                                                          symbol=symbol, side=sl_side,
                                                                          qty=old_sl_amount,
                                                                          order_type='STOP_MARKET',
                                                                          stop_price=recovery_params.get('stopPrice'),
                                                                          reduce_only=recovery_params.get('reduceOnly')))
                                                recovery_order = self._safe_api_call(
                                                    self.exchange.create_order,
                                                    symbol=symbol,
                                                    type='STOP_MARKET',
                                                    side=sl_side,
                                                    amount=old_sl_amount,
                                                    params=recovery_params,
                                                    retries=1
                                                )
                                                # B2-0 Verify 统一入口：not_found→NOT_CONFIRMED 不 raise；unknown→PENDING_VERIFY
                                                verify_result = self._verify_and_update_registry(
                                                    symbol, batch_id, recovery_identity, recovery_order['id'],
                                                    desc='降级恢复止损单')
                                                if verify_result != 'success':
                                                    print(f"  └─ ❌ 降级恢复单验证失败({verify_result})，不 Commit/不补单: {recovery_order['id']}")
                                                    self.send_tg_notification(
                                                        self._verify_failure_msg("降级恢复止损单", recovery_order['id'],
                                                                                  symbol, verify_result),
                                                        level='critical' if verify_result == 'unknown' else 'warning')
                                                    sl_success = False
                                                else:
                                                    current_sl_id = recovery_order['id']
                                                    sl_success = True
                                                    print(
                                                        f"  └─ 🔄 降级保护成功：已用旧止损价恢复: {old_sl_price} (ID: {current_sl_id})")
                                                    self.send_tg_notification(
                                                        f"⚠️ **降级保护触发**\n"
                                                        f"🆔 批次 `{batch_id}` 新止损单挂单失败，已自动恢复为旧止损价\n"
                                                        f"🛡️ 止损价：`{old_sl_price}`\n"
                                                        f"🔢 数量：`{old_sl_amount}`",
                                                        level='warning'
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
                                        if not create_unknown:
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
                    # R2/R3: 补挂前综合预检（ChatGPT 终审 2026-08-20）——
                    # 标记短路 / 层熔断短路 / 可行性校验（确定性错误不打 API + critical + 标记）
                    if need_update_tp and not self._tp_update_blocked(
                            symbol, batch_id, side, batch_filled_count - 1,
                            formatted_tp_price, batch_entry_vwap):
                        # B2-2: 意图先落盘（崩溃安全）+ intent 指纹（F1: identity 上移供撤销前闸门复用）
                        tp_identity = self._protection_identity(
                            batch_id, 'TP', batch_filled_count - 1,
                            params_base.get('positionSide', 'LONG' if side == 'BUY' else 'SHORT'))

                        # F1（2026-08-21 事件4）：替换旧单前先过仲裁闸门（replace 语义）——
                        # 原结构"先撤销再闸门检查（未传 replace_order_id）"→ CONFIRMED 拦截 →
                        # tp_order_id=None 落盘 → R14 每轮补挂 → 闸门永久拦截（registry 永不终结 = 死锁）。
                        # 拒绝/网络异常 → 保留原单（tp_skip_create=True → 下方闸门走 F1 分支不清 id）。
                        tp_skip_create = False
                        if tp_order_id:
                            allowed_r, reason_r = self._assert_create_allowed(
                                symbol, batch_id, tp_identity, desc='替换止盈单',
                                replace_order_id=tp_order_id)
                            if not allowed_r:
                                print(f"  └─ 🚫 [仲裁] 跳过替换止盈单（保留原单）: {reason_r}")
                                self._gate_alert_notify(
                                    tp_identity, reason_r,
                                    f"⚠️ **止盈单替换被仲裁拦截**\n"
                                    f"🆔 批次：`{batch_id}`\n"
                                    f"📌 {reason_r}\n"
                                    f"💡 程序保留原单不重复挂单，等待自愈重查确认",
                                    level='warning')
                                tp_skip_create = True
                            else:
                                try:
                                    self._safe_api_call(self.exchange.cancel_order, tp_order_id, symbol,
                                                        params={'stop': True})
                                    print(f"  └─ 已撤销旧止盈单: {tp_order_id} → registry ABSENT")
                                    # F1: 撤销确认 → registry 终结为 ABSENT（旧单物理离开 → 允许安全重建）
                                    self._update_registry(symbol, batch_id, tp_identity,
                                                          state='ABSENT',
                                                          terminated_reason='canceled_by_update_replace')
                                    tp_order_id = None
                                except Exception as e:
                                    if "Unknown order" in str(e) or "-2011" in str(e):
                                        print(f"  └─ 旧止盈单 {tp_order_id} 已不存在 → registry ABSENT")
                                        self._update_registry(symbol, batch_id, tp_identity,
                                                              state='ABSENT',
                                                              terminated_reason='order_not_found_on_replace')
                                        tp_order_id = None
                                    else:
                                        # F1: 网络异常 fail-closed——不清 id、不创建，保留下轮（防双单）
                                        print(f"  └─ ⚠️ 撤销旧止盈单失败: {e}，保留原单下轮再试")
                                        tp_skip_create = True

                        tp_params = params_base.copy()
                        tp_params['stopPrice'] = formatted_tp_price
                        if not is_hedge_mode:
                            tp_params['reduceOnly'] = True

                        try:
                            # B2-3: Create 仲裁闸门（§5.3）—— 同 identity 未决/已确认 → 禁新 create
                            if tp_skip_create:
                                allowed, gate_reason = False, 'F1_replace_blocked_skip_create'
                            else:
                                allowed, gate_reason = self._assert_create_allowed(
                                    symbol, batch_id, tp_identity, desc='补挂止盈单')
                            if not allowed:
                                if gate_reason == 'F1_replace_blocked_skip_create':
                                    # F1: 替换被阻断 → 保留原单（不清 id → 落盘保持 → R14 不触发）
                                    print(f"  └─ ⏭️ [F1] 替换被阻断，保留原止盈单 (id={tp_order_id})")
                                else:
                                    if gate_reason.startswith('HARD_LOCK'):
                                        # B2-4: 硬锁静默（进入时已 critical，此后不重复告警）
                                        print(f"  └─ 🔒 [硬锁] 跳过补挂止盈单: {gate_reason}")
                                    else:
                                        print(f"  └─ 🚫 [仲裁] 跳过补挂止盈单: {gate_reason}")
                                        self._gate_alert_notify(
                                            tp_identity, gate_reason,
                                            f"⚠️ **止盈单创建被仲裁拦截**\n"
                                            f"🆔 批次：`{batch_id}`\n"
                                            f"📌 {gate_reason}\n"
                                            f"💡 程序不重复挂单，等待自愈重查确认",
                                            level='warning')
                                    tp_order_id = None
                            else:
                                # B2-2: 崩溃安全——create 前先落盘 PENDING_CREATE + 不可变 intent 指纹
                                self._update_registry(symbol, batch_id, tp_identity,
                                                      state='PENDING_CREATE', id_known=False,
                                                      order_kind='conditional', role='TP',
                                                      layer=batch_filled_count - 1,
                                                      side=params_base.get('positionSide',
                                                                           'LONG' if side == 'BUY' else 'SHORT'),
                                                      intent=self._build_intent(
                                                          symbol=symbol, side=tp_side,
                                                          qty=batch_filled_amount,
                                                          order_type='TAKE_PROFIT_MARKET',
                                                          stop_price=tp_params.get('stopPrice'),
                                                          reduce_only=tp_params.get('reduceOnly')))
                                new_tp_order = self._safe_api_call(
                                    self.exchange.create_order,
                                    symbol=symbol,
                                    type='TAKE_PROFIT_MARKET',
                                    side=tp_side,
                                    amount=batch_filled_amount,
                                    params=tp_params,
                                    retries=1
                                )
                                # B2-0 Verify 统一入口：not_found→NOT_CONFIRMED；unknown→PENDING_VERIFY
                                verify_result = self._verify_and_update_registry(
                                    symbol, batch_id, tp_identity, new_tp_order['id'], desc='补挂止盈单')
                                if verify_result != 'success':
                                    print(f"  └─ ❌ 止盈单验证失败({verify_result})，不 Commit/不补单: {new_tp_order['id']}")
                                    self.send_tg_notification(
                                        self._verify_failure_msg("止盈单", new_tp_order['id'], symbol, verify_result),
                                        level='critical' if verify_result == 'unknown' else 'warning')
                                    tp_order_id = None
                                else:
                                    tp_order_id = new_tp_order['id']
                                    print(f"  └─ ✅ 止盈单已挂出: {formatted_tp_price} (ID: {tp_order_id})")
                                    # R-C（事件3根因C）：滚动撤销链补强——撤销 registry 旧层 TP 单
                                    self._reconcile_stale_protection_layers(
                                        symbol, batch_id, 'TP', keep_order_id=tp_order_id)
                                    # 补挂 TP 成功 → 清零该层层级熔断计数（对称 SL L3951-3955 语义）
                                    try:
                                        _b2 = self.load_all_states().get(symbol, {}).get(batch_id, {})
                                        if _b2 and _b2.get('tp_fail_count'):
                                            _b2['tp_fail_count'].pop(str(batch_filled_count - 1), None)
                                            self.save_batch_state(symbol, batch_id, _b2)
                                    except Exception:
                                        pass
                                    # ChatGPT 终审（2026-08-20）：成功挂出 = 真正恢复 →
                                    # 解除熔断告警去重（下次熔断可再提醒）+ 恢复 FAILED 告警 3 次额度（不永久吃掉）
                                    self._tp_breaker_alerted.pop((batch_id, batch_filled_count - 1), None)
                                    self._gate_alert_clear(tp_identity)
                        except Exception as e:
                            print(f"  └─ ❌ 挂出止盈单失败: {e}")
                            tp_order_id = None
                            # B2-0/B2-2: create 异常按操作阶段分流（与补挂 SL 段一致）——
                            # unknown（NetworkError 等）→ PENDING_VERIFY(id_unknown) 不计数不补单
                            #（可能已创建=再补=双单风险，等自愈按 intent 确认）；failed → FAILED 允许再次 Create
                            create_unknown = (self._classify_create_exception(e) == 'unknown')
                            if create_unknown:
                                self._update_registry(symbol, batch_id, tp_identity,
                                                      state='PENDING_VERIFY', id_known=False,
                                                      order_kind='conditional')
                                self.send_tg_notification(
                                    f"🚨 **止盈单创建结果未知（UNKNOWN）**\n"
                                    f"🆔 批次：`{batch_id}`\n"
                                    f"📊 第 {batch_filled_count} 层\n"
                                    f"⚠️ 网络异常，无法确认止盈单是否已创建\n"
                                    f"💡 程序【不计数】【不自动补单】，请到交易所核实！",
                                    level='critical'
                                )
                            else:
                                new_fc = self._update_registry(symbol, batch_id, tp_identity,
                                                               state='FAILED', id_known=False,
                                                               order_kind='conditional',
                                                               fail_count_incr=1)
                                # 补挂 TP 层级别熔断计数（对称 SL 的 sl_fail_count，ChatGPT 终审 2026-08-20）
                                try:
                                    _b = self.load_all_states().get(symbol, {}).get(batch_id, {})
                                    if _b:
                                        _tf = _b.get('tp_fail_count') or {}
                                        _tf[str(batch_filled_count - 1)] = _tf.get(str(batch_filled_count - 1), 0) + 1
                                        _b['tp_fail_count'] = _tf
                                        self.save_batch_state(symbol, batch_id, _b)
                                except Exception:
                                    pass
                                # 告警去重：同一 identity + FAILED 类别最多 3 次 TG（与 gate 拒绝路径一致）
                                self._gate_alert_notify(
                                    tp_identity, 'FAILED',
                                    f"⚠️ **止盈单创建失败（FAILED）**\n"
                                    f"🆔 批次：`{batch_id}`\n"
                                    f"📊 第 {batch_filled_count} 层\n"
                                    f"⚠️ 交易所明确拒绝（余额不足/无效参数等），允许后续重试\n"
                                    f"💡 请关注下一次风控更新是否重新挂单",
                                    level='warning'
                                )
                                # B2-4: registry fail_count≥5 → HARD_LOCK（§5.4）
                                if new_fc is not None and new_fc >= 5:
                                    self._update_registry(symbol, batch_id, tp_identity,
                                                          hard_locked=True)
                                    self.send_tg_notification(
                                        f"🚨 **HARD_LOCK 硬锁触发**\n"
                                        f"🆔 批次：`{batch_id}`\n"
                                        f"📊 第 {batch_filled_count} 层（identity：`{tp_identity}`）\n"
                                        f"⚠️ 该 identity 连续确定失败 {new_fc} 次（≥5），已硬锁\n"
                                        f"💡 程序不再自动重挂。请人工核实持仓后按 §5.5 规范解锁"
                                        f"（写 unlock_reason/unlock_time/unlock_operator）",
                                        level='critical'
                                    )

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
                        'user_modified': latest_b_data.get('user_modified', False) if latest_b_data else False,  # R13-B: 保留现有值，不得硬编码覆盖
                        'pending_sl_orders': pending_sl_orders,
                        'prepared_tp_params': prepared_tp_params,
                        'layer_sl_params': layer_sl_params,
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
            # 🔥 该异常位于 while 循环外层：监控线程将因此退出且无自动重生机制。
            # 若批次已有持仓，将不再自动补挂止损/止盈，属资金安全事件 → critical
            self.send_tg_notification(
                f"🚨 **监控线程异常退出**\n"
                f"🆔 批次：`{batch_id}`\n"
                f"💡 原因：`{str(inner_e)[:200]}`\n"
                f"⚠️ 该批次监控已终止，如有持仓将不再自动补挂止损/止盈\n"
                f"💡 请检查仓位，必要时重启程序恢复监控。",
                level='critical'
            )

            # 🔥 W1 修复（D-002）：补写 monitor_error 标记
            # recover_active_batches（L800）按此标记识别"监控线程曾崩溃"的批次，
            # 设计意图是跳过自动恢复并清理（需人工确认），而非按正常批次逻辑恢复。
            # 修复前全项目无任何位置写入该标记，设计意图落空。
            # 注意：save_batch_state 是整对象替换，必须先 load 现有数据再只改此字段，
            # 避免清空批次其他状态字段。
            try:
                all_states_w1 = self.load_all_states()
                b_data_w1 = all_states_w1.get(symbol, {}).get(batch_id, {})
                if b_data_w1:
                    b_data_w1['monitor_error'] = True
                    self.save_batch_state(symbol, batch_id, b_data_w1)
                    print(f"  └─ 📝 [W1] 已写入 monitor_error 标记（重启时将跳过恢复并清理）")
            except Exception as save_e:
                print(f"  └─ ⚠️ [W1] 写入 monitor_error 标记失败: {save_e}")

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
                current_pos = None  # R11: UNKNOWN ≠ EMPTY，查询失败不得当作无持仓

            # 如果确认无持仓，清理批次
            if current_pos is not None and current_pos == 0:
                all_states = self.load_all_states()
                b_data = all_states.get(symbol, {}).get(batch_id, {})
                if b_data:
                    self.clear_batch_state(symbol, batch_id)
                    print(f"  └─ 🧹 无持仓，已清理批次状态")
            elif current_pos is None:
                print(f"  └─ ⚠️ 持仓查询失败(UNKNOWN)，保留批次状态不清理")
            else:
                print(f"  └─ 📌 有持仓 {current_pos}，保留批次状态")

            print(f"🧹 批次 [{batch_id}] 监控线程已退出")

    def _place_prepared_orders_immediately(self, symbol, batch_id, idx, batch_filled_amount,
                                           prepared_tp_params, layer_sl_params,
                                           is_hedge_mode, params_base, stop_steps):
        """🔥 成交后立即使用预生成的参数挂止盈和止损单（1秒内完成）
        注意：此方法只在 current_sl_id 为 None 时调用，即首次成交时
        B1/P0-2 语义（规格 §3.2/§5.1/§6.3/§13）：意图先落盘 → create → verify(kind) → 状态迁移：
          verify success → CONFIRMED + Commit（current_sl_id/tp_order_id）
          verify not_found → NOT_CONFIRMED（不 Commit/不计数/不补单 + critical）
          verify unknown   → PENDING_VERIFY(id_known=True)（不 Commit/不计数/不补单 + critical）
          create 抛 ExchangeError → 'failed' → FAILED（计数，既有失败路径，允许重试）
          create 抛 NetworkError 等 → 'unknown' → PENDING_VERIFY(id_unknown)（不计数/不补单 + critical）
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
                position_side = 'LONG' if sl_params['side'] == 'sell' else 'SHORT'
                identity = self._protection_identity(batch_id, 'SL', idx, position_side)

                try:
                    # B2-3: Create 仲裁闸门（§5.3）—— 同 identity 未决/已确认 → 禁新 create
                    allowed, gate_reason = self._assert_create_allowed(
                        symbol, batch_id, identity, desc='预生成止损单')
                    if not allowed:
                        if gate_reason.startswith('HARD_LOCK'):
                            # B2-4: 硬锁静默（进入时已 critical，此后不重复告警）
                            print(f"  └─ 🔒 [硬锁] 跳过预生成止损单: {gate_reason}")
                        else:
                            print(f"  └─ 🚫 [仲裁] 跳过预生成止损单: {gate_reason}")
                            self._gate_alert_notify(
                                identity, gate_reason,
                                f"⚠️ **预生成止损单被仲裁拦截**\n"
                                f"🆔 批次：`{batch_id}`\n"
                                f"📊 第 {idx + 1} 层\n"
                                f"📌 {gate_reason}\n"
                                f"💡 程序不重复挂单，等待自愈重查确认",
                                level='warning')
                    else:
                        # B1: 意图先落盘（崩溃安全 Create）—— 订单 ID 未知 + intent 指纹（B2-2）
                        self._update_registry(symbol, batch_id, identity, state='PENDING_CREATE',
                                              id_known=False, order_kind='conditional', role='SL',
                                              layer=idx, side=position_side,
                                              intent=self._build_intent(
                                                  symbol=sl_params['symbol'],
                                                  side=sl_params['side'],
                                                  qty=sl_params['amount'],
                                                  order_type=sl_params['type'],
                                                  stop_price=sl_params['params'].get('stopPrice'),
                                                  reduce_only=sl_params['params'].get('reduceOnly')))
                        new_sl_order = self._safe_api_call(
                            self.exchange.create_order,
                            symbol=sl_params['symbol'],
                            type=sl_params['type'],
                            side=sl_params['side'],
                            amount=sl_params['amount'],
                            params=sl_params['params'],
                            retries=1
                        )
                        # C5/SG4 + B1 Verify(kind)：unknown→PENDING_VERIFY；not_found→NOT_CONFIRMED
                        verify_result = self._verify_order_created(new_sl_order['id'], sl_params['symbol'], 'conditional')
                        if verify_result == 'unknown':
                            self._update_registry(symbol, batch_id, identity, state='PENDING_VERIFY',
                                                  order_id=new_sl_order['id'], id_known=True)
                            print(f"  └─ ⚡ 预生成止损单创建结果未知(UNKNOWN)，不 Commit/不补单: {new_sl_order['id']}")
                            self.send_tg_notification(
                                f"🚨 **预生成止损单创建结果未知（UNKNOWN）**\n"
                                f"🆔 批次：`{batch_id}`\n"
                                f"📊 第 {idx + 1} 层\n"
                                f"⚠️ 网络异常，无法确认该订单是否已创建\n"
                                f"💡 程序【未记录】此订单（不 Commit），不会自动补单\n"
                                f"🛠️ 请到交易所核实是否存在该订单！",
                                level='critical')
                        elif verify_result == 'not_found':
                            # 确定不存在 → NOT_CONFIRMED：禁重试禁补单，不计数（NOT_CONFIRMED ≠ FAILED）
                            self._update_registry(symbol, batch_id, identity, state='NOT_CONFIRMED',
                                                  order_id=new_sl_order['id'], id_known=True)
                            print(f"  └─ ⚡ 预生成止损单创建未确认(NOT_CONFIRMED)，不 Commit/不补单: {new_sl_order['id']}")
                            self.send_tg_notification(
                                f"🚨 **预生成止损单创建未确认（NOT_CONFIRMED）**\n"
                                f"🆔 批次：`{batch_id}`\n"
                                f"📊 第 {idx + 1} 层\n"
                                f"⚠️ 交易所查询不到该订单（可能不存在或已被触发）\n"
                                f"💡 程序【未记录】此订单（不 Commit），也不会自动补单\n"
                                f"🛠️ 请到交易所核实持仓保护状态！",
                                level='critical')
                        elif latest_b_data:
                            # CONFIRMED → Commit
                            self._update_registry(symbol, batch_id, identity, state='CONFIRMED',
                                                  order_id=new_sl_order['id'], id_known=True)
                            sl_price = sl_params['params']['stopPrice']
                            latest_b_data['current_sl_id'] = new_sl_order['id']
                            # 从待挂列表中移除当前层
                            pending = latest_b_data.get('pending_sl_orders', [])
                            if idx in pending:
                                pending.remove(idx)
                            latest_b_data['pending_sl_orders'] = pending
                            self.save_batch_state(symbol, batch_id, latest_b_data)
                            print(f"  └─ ⚡ 预生成止损单已挂出: {sl_price} (ID: {new_sl_order['id']})")
                            # R-C（事件3根因C）：滚动撤销链补强——预生成路径可能因 current_sl_id
                            # 为 None 而对后续层补挂（verify 假阴性未 Commit 时），须撤销 registry
                            # 旧层 SL 单（防层叠重复）
                            self._reconcile_stale_protection_layers(
                                symbol, batch_id, 'SL', keep_order_id=new_sl_order['id'])
                except Exception as e:
                    cls = self._classify_create_exception(e)
                    if cls == 'unknown':
                        # 结果未知（NetworkError/超时/限流等）→ PENDING_VERIFY(id_unknown)：不计数不补单
                        self._update_registry(symbol, batch_id, identity, state='PENDING_VERIFY', id_known=False)
                        print(f"  └─ ⚡ 预生成止损单创建结果未知(UNKNOWN)，不计数/不补单: {e}")
                        self.send_tg_notification(
                            f"🚨 **预生成止损单创建结果未知（UNKNOWN）**\n"
                            f"🆔 批次：`{batch_id}`\n"
                            f"📊 第 {idx + 1} 层\n"
                            f"⚠️ 网络异常，无法确认订单是否已创建（订单 ID 未知）\n"
                            f"💡 程序【未记录】此订单（不 Commit），不会自动补单\n"
                            f"🛠️ 请到交易所核实是否存在该订单！",
                            level='critical')
                    else:
                        # 确定拒绝（ExchangeError）→ FAILED：既有失败路径（计数 + 告警 + 允许重试）
                        new_fc = self._update_registry(symbol, batch_id, identity, state='FAILED',
                                                       fail_count_incr=1)
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
                        # B2-4: registry fail_count≥5 → HARD_LOCK（§5.4）
                        if new_fc is not None and new_fc >= 5:
                            self._update_registry(symbol, batch_id, identity, hard_locked=True)
                            self.send_tg_notification(
                                f"🚨 **HARD_LOCK 硬锁触发**\n"
                                f"🆔 批次：`{batch_id}`\n"
                                f"📊 第 {idx + 1} 层（identity：`{identity}`）\n"
                                f"⚠️ 该 identity 连续确定失败 {new_fc} 次（≥5），已硬锁\n"
                                f"💡 程序不再自动重挂。请人工核实持仓后按 §5.5 规范解锁"
                                f"（写 unlock_reason/unlock_time/unlock_operator）",
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
                position_side = 'LONG' if sl_side == 'sell' else 'SHORT'
                identity = self._protection_identity(batch_id, 'SL', idx, position_side)
                try:
                    # B2-3: Create 仲裁闸门（§5.3）—— 同 identity 未决/已确认 → 禁新 create
                    allowed, gate_reason = self._assert_create_allowed(
                        symbol, batch_id, identity, desc='兜底止损单')
                    if not allowed:
                        if gate_reason.startswith('HARD_LOCK'):
                            # B2-4: 硬锁静默（进入时已 critical，此后不重复告警）
                            print(f"  └─ 🔒 [硬锁] 跳过兜底止损单: {gate_reason}")
                        else:
                            print(f"  └─ 🚫 [仲裁] 跳过兜底止损单: {gate_reason}")
                            self._gate_alert_notify(
                                identity, gate_reason,
                                f"⚠️ **兜底止损单被仲裁拦截**\n"
                                f"🆔 批次：`{batch_id}`\n"
                                f"📊 第 {idx + 1} 层\n"
                                f"📌 {gate_reason}\n"
                                f"💡 程序不重复挂单，等待自愈重查确认",
                                level='warning')
                    else:
                        # B1: 意图先落盘（崩溃安全 Create）—— 订单 ID 未知 + intent 指纹（B2-2）
                        self._update_registry(symbol, batch_id, identity, state='PENDING_CREATE',
                                              id_known=False, order_kind='conditional', role='SL',
                                              layer=idx, side=position_side,
                                              intent=self._build_intent(
                                                  symbol=symbol,
                                                  side=sl_side,
                                                  qty=batch_filled_amount,
                                                  order_type='STOP_MARKET',
                                                  stop_price=sl_params.get('stopPrice'),
                                                  reduce_only=sl_params.get('reduceOnly')))
                        new_sl_order = self._safe_api_call(
                            self.exchange.create_order,
                            symbol=symbol,
                            type='STOP_MARKET',
                            side=sl_side,
                            amount=batch_filled_amount,
                            params=sl_params,
                            retries=1
                        )
                        # C5/SG4 + B1 Verify(kind)：unknown→PENDING_VERIFY；not_found→NOT_CONFIRMED
                        verify_result = self._verify_order_created(new_sl_order['id'], symbol, 'conditional')
                        if verify_result == 'unknown':
                            self._update_registry(symbol, batch_id, identity, state='PENDING_VERIFY',
                                                  order_id=new_sl_order['id'], id_known=True)
                            print(f"  └─ ⚡ 兜底止损单创建结果未知(UNKNOWN)，不 Commit/不补单: {new_sl_order['id']}")
                            self.send_tg_notification(
                                f"🚨 **兜底止损单创建结果未知（UNKNOWN）**\n"
                                f"🆔 批次：`{batch_id}`\n"
                                f"📊 第 {idx + 1} 层\n"
                                f"⚠️ 网络异常，无法确认该订单是否已创建\n"
                                f"💡 程序【未记录】此订单（不 Commit），不会自动补单\n"
                                f"🛠️ 请到交易所核实是否存在该订单！",
                                level='critical')
                        elif verify_result == 'not_found':
                            # 确定不存在 → NOT_CONFIRMED：禁重试禁补单，不计数（NOT_CONFIRMED ≠ FAILED）
                            self._update_registry(symbol, batch_id, identity, state='NOT_CONFIRMED',
                                                  order_id=new_sl_order['id'], id_known=True)
                            print(f"  └─ ⚡ 兜底止损单创建未确认(NOT_CONFIRMED)，不 Commit/不补单: {new_sl_order['id']}")
                            self.send_tg_notification(
                                f"🚨 **兜底止损单创建未确认（NOT_CONFIRMED）**\n"
                                f"🆔 批次：`{batch_id}`\n"
                                f"📊 第 {idx + 1} 层\n"
                                f"⚠️ 交易所查询不到该订单（可能不存在或已被触发）\n"
                                f"💡 程序【未记录】此订单（不 Commit），也不会自动补单\n"
                                f"🛠️ 请到交易所核实持仓保护状态！",
                                level='critical')
                        else:
                            # CONFIRMED → Commit
                            self._update_registry(symbol, batch_id, identity, state='CONFIRMED',
                                                  order_id=new_sl_order['id'], id_known=True)
                            # ChatGPT 终审（2026-08-20）：兜底 SL 成功挂出 = 真正恢复 →
                            # 恢复 FAILED 告警 3 次额度（L4885 直发点同 identity 去重计数）
                            self._gate_alert_clear(identity)
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
                                # R-C（事件3根因C）：滚动撤销链补强——撤销 registry 旧层 SL 单
                                self._reconcile_stale_protection_layers(
                                    symbol, batch_id, 'SL', keep_order_id=new_sl_order['id'])
                except Exception as e:
                    cls = self._classify_create_exception(e)
                    if cls == 'unknown':
                        # 结果未知（NetworkError/超时/限流等）→ PENDING_VERIFY(id_unknown)：不计数不补单
                        self._update_registry(symbol, batch_id, identity, state='PENDING_VERIFY', id_known=False)
                        print(f"  └─ ⚡ 兜底止损单创建结果未知(UNKNOWN)，不计数/不补单: {e}")
                        self.send_tg_notification(
                            f"🚨 **兜底止损单创建结果未知（UNKNOWN）**\n"
                            f"🆔 批次：`{batch_id}`\n"
                            f"📊 第 {idx + 1} 层\n"
                            f"⚠️ 网络异常，无法确认订单是否已创建（订单 ID 未知）\n"
                            f"💡 程序【未记录】此订单（不 Commit），不会自动补单\n"
                            f"🛠️ 请到交易所核实是否存在该订单！",
                            level='critical')
                    else:
                        # 确定拒绝（ExchangeError）→ FAILED：既有失败路径（计数 + 告警 + 允许重试）
                        new_fc = self._update_registry(symbol, batch_id, identity, state='FAILED',
                                                       fail_count_incr=1)
                        if latest_b_data:
                            sl_fail_count = latest_b_data.get('sl_fail_count', {})
                            layer_key = str(idx)
                            sl_fail_count[layer_key] = sl_fail_count.get(layer_key, 0) + 1
                            latest_b_data['sl_fail_count'] = sl_fail_count
                            self.save_batch_state(symbol, batch_id, latest_b_data)
                        # 告警去重：同一 identity + FAILED 类别最多 3 次 TG（与 gate 拒绝路径一致）
                        self._gate_alert_notify(
                            identity, 'FAILED',
                            f"🚨 **止损单挂出失败(兜底)！**\n"
                            f"🆔 批次：`{batch_id}`\n"
                            f"📊 第 {idx + 1} 层\n"
                            f"💡 原因：{str(e)[:100]}\n"
                            f"⚠️ 程序将重试，请关注后续通知！",
                            level='critical'
                        )
                        # B2-4: registry fail_count≥5 → HARD_LOCK（§5.4）
                        if new_fc is not None and new_fc >= 5:
                            self._update_registry(symbol, batch_id, identity, hard_locked=True)
                            self.send_tg_notification(
                                f"🚨 **HARD_LOCK 硬锁触发**\n"
                                f"🆔 批次：`{batch_id}`\n"
                                f"📊 第 {idx + 1} 层（identity：`{identity}`）\n"
                                f"⚠️ 该 identity 连续确定失败 {new_fc} 次（≥5），已硬锁\n"
                                f"💡 程序不再自动重挂。请人工核实持仓后按 §5.5 规范解锁"
                                f"（写 unlock_reason/unlock_time/unlock_operator）",
                                level='critical'
                            )
        else:
            print(f"  └─ ⚡ 已存在止损单，等待主循环合并更新")

        # 挂止盈单（首次成交时挂，后续不重复挂）
        if latest_b_data and latest_b_data.get('tp_order_id') is None:
            position_side = 'LONG' if prepared_tp_params['side'] == 'sell' else 'SHORT'
            identity = self._protection_identity(batch_id, 'TP', idx, position_side)
            # R2/R3: 成交后止盈价可行性预检（ChatGPT 终审 2026-08-20；确定性错误：不打 API、
            # 1 次 critical、写 tp_param_invalid 标记）。预生成阶段无真实成本：cost=0 仅校验现价
            # 方向 → 只防 -2021、不承担成本保护（cost=0 ≠ 无限制 = 尚未产生成本）；成交后 R2 双校验。
            _tp_side = 'BUY' if prepared_tp_params['side'] == 'sell' else 'SELL'
            if self._tp_update_blocked(symbol, batch_id, _tp_side, idx,
                                       prepared_tp_params['params'].get('stopPrice'), 0.0):
                return
            try:
                # B2-3: Create 仲裁闸门（§5.3）—— 同 identity 未决/已确认 → 禁新 create
                allowed, gate_reason = self._assert_create_allowed(
                    symbol, batch_id, identity, desc='预生成止盈单')
                if not allowed:
                    if gate_reason.startswith('HARD_LOCK'):
                        # B2-4: 硬锁静默（进入时已 critical，此后不重复告警）
                        print(f"  └─ 🔒 [硬锁] 跳过预生成止盈单: {gate_reason}")
                    else:
                        print(f"  └─ 🚫 [仲裁] 跳过预生成止盈单: {gate_reason}")
                        self._gate_alert_notify(
                            identity, gate_reason,
                            f"⚠️ **预生成止盈单被仲裁拦截**\n"
                            f"🆔 批次：`{batch_id}`\n"
                            f"📊 第 {idx + 1} 层\n"
                            f"📌 {gate_reason}\n"
                            f"💡 程序不重复挂单，等待自愈重查确认",
                            level='warning')
                else:
                    # B1: 意图先落盘（崩溃安全 Create）—— 订单 ID 未知 + intent 指纹（B2-2）
                    self._update_registry(symbol, batch_id, identity, state='PENDING_CREATE',
                                          id_known=False, order_kind='conditional', role='TP',
                                          layer=idx, side=position_side,
                                          intent=self._build_intent(
                                              symbol=prepared_tp_params['symbol'],
                                              side=prepared_tp_params['side'],
                                              qty=batch_filled_amount,
                                              order_type=prepared_tp_params['type'],
                                              stop_price=prepared_tp_params['params'].get('stopPrice'),
                                              reduce_only=(True if not is_hedge_mode
                                                           else prepared_tp_params['params'].get('reduceOnly'))))
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
                        params=tp_params['params'],
                        retries=1
                    )
                    # C5/SG4 + B1 Verify(kind)：unknown→PENDING_VERIFY；not_found→NOT_CONFIRMED
                    verify_result = self._verify_order_created(new_tp_order['id'], tp_params['symbol'], 'conditional')
                    if verify_result == 'unknown':
                        self._update_registry(symbol, batch_id, identity, state='PENDING_VERIFY',
                                              order_id=new_tp_order['id'], id_known=True)
                        print(f"  └─ ⚡ 预生成止盈单创建结果未知(UNKNOWN)，不 Commit/不补单: {new_tp_order['id']}")
                        self.send_tg_notification(
                            f"🚨 **预生成止盈单创建结果未知（UNKNOWN）**\n"
                            f"🆔 批次：`{batch_id}`\n"
                            f"⚠️ 网络异常，无法确认该订单是否已创建\n"
                            f"💡 程序【未记录】此订单（不 Commit），不会自动补单\n"
                            f"🛠️ 请到交易所核实是否存在该订单！",
                            level='critical')
                    elif verify_result == 'not_found':
                        # 确定不存在 → NOT_CONFIRMED：禁重试禁补单，不计数（NOT_CONFIRMED ≠ FAILED）
                        self._update_registry(symbol, batch_id, identity, state='NOT_CONFIRMED',
                                              order_id=new_tp_order['id'], id_known=True)
                        print(f"  └─ ⚡ 预生成止盈单创建未确认(NOT_CONFIRMED)，不 Commit/不补单: {new_tp_order['id']}")
                        self.send_tg_notification(
                            f"🚨 **预生成止盈单创建未确认（NOT_CONFIRMED）**\n"
                            f"🆔 批次：`{batch_id}`\n"
                            f"⚠️ 交易所查询不到该订单（可能不存在或已被触发）\n"
                            f"💡 程序【未记录】此订单（不 Commit），也不会自动补单\n"
                            f"🛠️ 请到交易所核实持仓保护状态！",
                            level='critical')
                    else:
                        # CONFIRMED → Commit
                        self._update_registry(symbol, batch_id, identity, state='CONFIRMED',
                                              order_id=new_tp_order['id'], id_known=True)
                        # ChatGPT 终审（2026-08-20）：预生成 TP 成功挂出 = 真正恢复 → 恢复 FAILED 告警额度
                        self._gate_alert_clear(identity)
                        latest_all = self.load_all_states()
                        latest_b_data = latest_all.get(symbol, {}).get(batch_id, {})
                        if latest_b_data:
                            latest_b_data['tp_order_id'] = new_tp_order['id']
                            self.save_batch_state(symbol, batch_id, latest_b_data)
                            print(f"  └─ ⚡ 预生成止盈单已挂出: {tp_params['params']['stopPrice']} (ID: {new_tp_order['id']})")
                            # R-C（事件3根因C）：滚动撤销链补强——撤销 registry 旧层 TP 单
                            self._reconcile_stale_protection_layers(
                                symbol, batch_id, 'TP', keep_order_id=new_tp_order['id'])
            except Exception as e:
                cls = self._classify_create_exception(e)
                if cls == 'unknown':
                    # 结果未知（NetworkError/超时/限流等）→ PENDING_VERIFY(id_unknown)：不计数不补单
                    self._update_registry(symbol, batch_id, identity, state='PENDING_VERIFY', id_known=False)
                    print(f"  └─ ⚡ 预生成止盈单创建结果未知(UNKNOWN)，不计数/不补单: {e}")
                    self.send_tg_notification(
                        f"🚨 **预生成止盈单创建结果未知（UNKNOWN）**\n"
                        f"🆔 批次：`{batch_id}`\n"
                        f"⚠️ 网络异常，无法确认订单是否已创建（订单 ID 未知）\n"
                        f"💡 程序【未记录】此订单（不 Commit），不会自动补单\n"
                        f"🛠️ 请到交易所核实是否存在该订单！",
                        level='critical')
                else:
                    # B2-4: 确定拒绝（ExchangeError）→ FAILED：终结悬空的 PENDING_CREATE，
                    # 允许经闸门重试（此前缺此分支 → registry 停在 PENDING_CREATE 被闸门永久拦截）
                    new_fc = self._update_registry(symbol, batch_id, identity, state='FAILED',
                                                   fail_count_incr=1)
                    print(f"  └─ ⚡ 预生成止盈单挂出失败(FAILED)，可经闸门重试: {e}")
                    # B2-4: registry fail_count≥5 → HARD_LOCK（§5.4）
                    if new_fc is not None and new_fc >= 5:
                        self._update_registry(symbol, batch_id, identity, hard_locked=True)
                        self.send_tg_notification(
                            f"🚨 **HARD_LOCK 硬锁触发**\n"
                            f"🆔 批次：`{batch_id}`\n"
                            f"📊 identity：`{identity}`\n"
                            f"⚠️ 该 identity 连续确定失败 {new_fc} 次（≥5），已硬锁\n"
                            f"💡 程序不再自动重挂。请人工核实持仓后按 §5.5 规范解锁"
                            f"（写 unlock_reason/unlock_time/unlock_operator）",
                            level='critical'
                        )
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

        # 🔥 修复漏洞1b：先获取市价，成功后再设 flags（原代码先设 flags 再取 ticker，
        # ticker 失败时 flags 已落盘 → 监控线程误判"程序平仓中"不恢复 SL）
        try:
            ticker = self._safe_api_call(self.exchange.fetch_ticker, target_symbol)
            current_price = float(ticker.get('last') or ticker.get('close') or 0.0)
        except Exception as e:
            return False, f"❌ 获取市价失败: {e}"

        # 标记程序主动平仓，监控线程将静默退出（ticker 已成功，安全设 flags）
        target_b_data['is_programmatic_cancel'] = True
        target_b_data['pending_close'] = True
        self.save_batch_state(target_symbol, batch_id, target_b_data)

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
            # 先撤销所有未成交的开仓条件单（保护单不撤，仍在位保护仓位）
            entry_orders = target_b_data.get('entry_orders', [])
            for idx, order_id in enumerate(entry_orders):
                if idx >= last_filled_count:
                    try:
                        self._safe_api_call(self.exchange.cancel_order, order_id, target_symbol, params={'stop': True})
                        print(f"  └─ 已撤销开仓挂单: {order_id}")
                    except Exception:
                        pass

            # 🔥 修复漏洞1：先市价平仓，成功后再撤 SL/TP（原代码先撤 SL/TP 再平仓，
            # 若平仓失败则裸仓无保护且监控线程因 is_programmatic_cancel 不补挂）
            # reduceOnly 平仓后 SL/TP 即使短暂存在也不会反向开仓，风险远低于先撤保护再赌平仓
            close_side = 'sell' if side == 'BUY' else 'buy'
            order = self._safe_api_call(
                self.exchange.create_order,
                symbol=target_symbol,
                type='MARKET',
                side=close_side,
                amount=current_filled_amount,
                params={'reduceOnly': True},
                retries=1
            )

            # 平仓成功 — 现在安全撤销保护单
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
            # 🔥 修复漏洞1b：失败回滚 — 清除 flags，恢复监控线程保护能力
            # （SL/TP 未被撤销仍在交易所保护仓位，清除 flags 后监控线程恢复正常补挂）
            try:
                rollback_states = self.load_all_states()
                rollback_b_data = rollback_states.get(target_symbol, {}).get(batch_id, {})
                if rollback_b_data:
                    rollback_b_data['is_programmatic_cancel'] = False
                    rollback_b_data['pending_close'] = False
                    self.save_batch_state(target_symbol, batch_id, rollback_b_data)
                    print(f"  └─ 🔄 平仓失败回滚：已清除 is_programmatic_cancel/pending_close，监控线程恢复保护")
            except Exception as rollback_err:
                print(f"  └─ ⚠️ 回滚失败: {rollback_err}（需人工检查批次状态）")
                self.send_tg_notification(
                    f"🚨【资金安全】市价平仓失败且回滚异常！\n批次: {batch_id}\n"
                    f"请立即检查仓位是否仍有 SL 保护！",
                    level='critical')
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

        # 🔥 修复漏洞1b：先获取市价，成功后再设 flags（与市价平仓对称修复）
        try:
            ticker = self._safe_api_call(self.exchange.fetch_ticker, target_symbol)
            current_price = float(ticker.get('last') or ticker.get('close') or 0.0)
            bid = float(ticker.get('bid') or current_price)
            ask = float(ticker.get('ask') or current_price)
        except Exception as e:
            return False, f"❌ 获取市价失败: {e}"

        # 标记程序主动平仓，监控线程将静默退出（ticker 已成功，安全设 flags）
        target_b_data['is_programmatic_cancel'] = True
        target_b_data['pending_close'] = True
        self.save_batch_state(target_symbol, batch_id, target_b_data)

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
            # 限价平仓（C5：create_order 非幂等，禁止盲重；reduceOnly 仅保证业务结果不超仓）
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
                params=order_params,
                retries=1
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
            # 🔥 修复漏洞1b：失败回滚 — 清除 flags，恢复监控线程保护能力
            # （限价平仓不撤 SL，仓位仍受保护，但 flags 残留会导致监控线程不补挂）
            try:
                rollback_states = self.load_all_states()
                rollback_b_data = rollback_states.get(target_symbol, {}).get(batch_id, {})
                if rollback_b_data:
                    rollback_b_data['is_programmatic_cancel'] = False
                    rollback_b_data['pending_close'] = False
                    self.save_batch_state(target_symbol, batch_id, rollback_b_data)
                    print(f"  └─ 🔄 挂限价单失败回滚：已清除 is_programmatic_cancel/pending_close")
            except Exception as rollback_err:
                print(f"  └─ ⚠️ 回滚失败: {rollback_err}")
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
            # R10: 对齐主监控的 Fail-not-Silent（不变量⑧）
            # 刻意不写 monitor_error 标记——该标记语义是"重启时跳过恢复并清理批次"，
            # 而本线程异常时主监控仍健在，照抄会导致健康批次在重启时被误清（R11 模式）
            try:
                self.send_tg_notification(
                    f"🚨 **限价平仓监控线程异常退出**\n"
                    f"🆔 批次：`{batch_id}`\n"
                    f"📋 限价单：`{order_id}`（仍挂在交易所，已无人跟踪）\n"
                    f"💡 原因：`{str(e)[:200]}`\n"
                    f"⚠️ 若限价单成交，主监控将按持仓归零兜底结算（费用口径可能异常）\n"
                    f"💡 请检查限价单状态，必要时重启程序。",
                    level='critical'
                )
            except Exception as tg_e:
                print(f"⚠️ [R10] 告警发送失败: {tg_e}")  # 吞掉 TG 异常，不覆盖真因

        print(f"🧹 [限价平仓监控] 批次 {batch_id} 监控线程已退出")


if __name__ == "__main__":
    print("⚠️ 请通过 bot_runner.py 启动完整的交易系统")
    print("🔧 trader_260725.py 仅供导入使用")