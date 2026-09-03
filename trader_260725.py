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
import uuid
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

# ==================== P0 Batch C（防回退，v2 §3/§5 + v3 §5/§6） ====================
# C2 墓碑：独立于 trade_state.json（clear 本身从 state 删键，墓碑必须独立持久化
# 才能抵抗"删记忆"式复活）。TTL=7 天（三轮裁定 D3：覆盖长假/停机重启窗口；
# parser batch_id 带 uuid4 后缀 → 复用概率≈0，超期后使命完成）。
TOMBSTONE_FILE = "trade_tombstones.json"
TOMBSTONE_TTL_SECONDS = 7 * 24 * 3600
# C1 字段级 merge 分类字段表（v2 §5.1 + v3 §5 七类）：
#   A 棘轮（close_phase 专列 int max）/ G user_modified OR / B 单调账本 /
#   C registry 逐 identity / D id 镜像 / E 静态幂等 / F 簿记最新者胜（默认）。
_MERGE_RATCHET_BOOL_FIELDS = ('pending_close', 'is_programmatic_cancel',
                              'settled_by_limit_close', 'settlement_reported')


def _partial_resize_owner_ok(b, owner_op_id):
    """🔥 v6.4-P0 + P5：G1/G2/G3b 极窄 owner exception 判据。

    仅当批次处于 partial_resize_pending（净账本已 durable、只差保护单 resize）
    或 limit_cancel_restore_pending（P5：closecancel 归属已 durable、只差保护单
    恢复）且调用方持有匹配的 close_op_id 时，允许在 close_phase>=1 冻结期
    创建/替换保护单（= 事务自身的 resize/re-arm）。默认 gate 行为零变化，
    严禁泛化成 allow_during_close——否则等于重新打开 close 竞态封印。"""
    return (owner_op_id is not None
            and b.get('close_reason') in ('partial_resize_pending',
                                          'limit_cancel_restore_pending')
            and str(owner_op_id) == str(b.get('close_op_id') or ''))
_MERGE_ID_MIRROR_FIELDS = ('tp_order_id', 'current_sl_id', 'limit_close_order_id')
# C 类保护集：磁盘条目处于这些 state → 保留磁盘（未决/已锁/终态不许被旧快照降级）
_MERGE_REGISTRY_PROTECTED_STATES = ('PENDING_CREATE', 'PENDING_VERIFY', 'NOT_CONFIRMED',
                                    'CONFIRMED', 'MISMATCH', 'HARD_LOCK',
                                    'PROGRAMMATIC_CANCELED')
_REGISTRY_TERMINAL_STATES = ('PROGRAMMATIC_CANCELED', 'ABSENT', 'FAILED')

# 北京时间时区（与 watchdog.py 保持一致，日报/盈亏记录统一使用）
BEIJING_TZ = pytz.timezone('Asia/Shanghai')

# QQ 邮箱发送串行锁（限制同时最多 1 个 SMTP 连接）
EMAIL_SEND_LOCK = threading.Lock()


class ApiMetrics:
    """🔥 v6.4-P4b（Phase 2 观测层数据模型修正）：_safe_api_call 单点极薄计数。

    只观测、绝不改变任何限频/冷却/重试策略；所有内部异常静默吞掉（观测故障
    零影响交易）。事件模型 (ts, endpoint, ok/fail, used_weight_1m,
    order_count_10s, order_count_1m)——三指标 latest=时间序最后有效 header、
    peak=60s 窗口内最大值（绝不用进程生命周期 max 冒充「最新」，ChatGPT 复审
    Blocker 2）。所有 _safe_api_call 调用尝试均入账（成功与失败），若本次响应
    提供 header 则同步记录（header 由 _api_semaphore 临界区内 dict copy 快照，
    ownership 与产生它的事务绑定，P4c）。
    429/418 事发时输出前 window 秒调用面 + 三指标窗口轨迹 + 本次错误 header——
    「IP weight 飙升」vs「order-count 爆了」vs「两者都低却拿到 418（外部嫌疑
    大幅上升）」三种归因方向当场分开。"""

    # 静态 weight 估值（辅助口径；2026-09 官方文档实锤：v3 positionRisk/account=5，
    # openOrders 带 symbol=1，order 查询=1，ticker(symbol)=1，time=1，
    # DELETE order=1（P4b 修正，原误写 0），New Order IP=0（order 计数由真实 header 记录））
    WEIGHT_ESTIMATES = {
        'fetch_open_orders': 1, 'fetch_order': 1, 'fetch_positions': 5,
        'fetch_balance': 5, 'fetch_ticker': 1, 'fetch_time': 1,
        'load_time_difference': 1, 'set_leverage': 1,
        'create_order': 0, 'cancel_order': 1,
    }

    def __init__(self, window=60.0, time_fn=time.time):
        self.window = float(window)
        self._time_fn = time_fn
        self._lock = threading.Lock()
        self._events = []            # [ts, endpoint, ok/fail, uw, oc10, oc1]（缺 header 为 None）
        self._last_summary_ts = 0.0

    @staticmethod
    def _endpoint_of(func):
        return getattr(func, '__name__', str(func))

    @staticmethod
    def _hdr_val(headers, key):
        for k, v in (headers or {}).items():
            if key in str(k).lower():
                try:
                    return float(v)
                except (TypeError, ValueError):
                    return None
        return None

    def _window_stats(self, now, seconds=None):
        """窗口内 (counts, est_weight, 各指标 latest/peak60s)。事件按时间序追加，
        「最新」= 窗口内最后一个有效 header 值。"""
        sec = self.window if seconds is None else float(seconds)
        counts = {}
        uw_l = uw_p = o10_l = o10_p = o1_l = o1_p = None
        for ts, ep, _st, uw, oc10, oc1 in self._events:
            if ts < now - sec:
                continue
            counts[ep] = counts.get(ep, 0) + 1
            if uw is not None:
                uw_l = uw
                uw_p = uw if uw_p is None else max(uw_p, uw)
            if oc10 is not None:
                o10_l = oc10
                o10_p = oc10 if o10_p is None else max(o10_p, oc10)
            if oc1 is not None:
                o1_l = oc1
                o1_p = oc1 if o1_p is None else max(o1_p, oc1)
        est = sum(self.WEIGHT_ESTIMATES.get(e, 1) * n for e, n in counts.items())
        return counts, est, uw_l, uw_p, o10_l, o10_p, o1_l, o1_p

    @staticmethod
    def _seg(label, latest, peak):
        if latest is None:
            return ''
        return f" {label} 最新={latest:.0f} 峰值60s={peak:.0f}"

    def format_summary(self):
        """当前窗口汇总行（无节流；record() 内部按 60s 节流打印同一内容）。"""
        try:
            now = self._time_fn()
            with self._lock:
                counts, est, uw_l, uw_p, o10_l, o10_p, o1_l, o1_p = self._window_stats(now)
            detail = (', '.join(f'{e}×{n}' for e, n in
                                sorted(counts.items(), key=lambda kv: -kv[1]))
                      or '无')
            return (f"📊 [限流观测] 近{self.window:.0f}s 调用: {detail} | 估算weight≈{est}"
                    + self._seg('USED-WEIGHT', uw_l, uw_p)
                    + self._seg('ORDER-10S', o10_l, o10_p)
                    + self._seg('ORDER-1M', o1_l, o1_p))
        except Exception:
            return None  # 观测故障绝不外泄

    def record(self, func, headers=None, ok=True):
        """入账一次响应（成功或失败均可，header 缺失记 None）；
        跨过汇总窗口时返回汇总日志行（由调用方 print），否则 None。"""
        try:
            ep = self._endpoint_of(func)
            now = self._time_fn()
            line = None
            with self._lock:
                self._events.append((now, ep, 'ok' if ok else 'fail',
                                     self._hdr_val(headers, 'used-weight-1m'),
                                     self._hdr_val(headers, 'order-count-10s'),
                                     self._hdr_val(headers, 'order-count-1m')))
                cutoff = now - self.window * 3   # 事件保留 3 个窗口
                while self._events and self._events[0][0] < cutoff:
                    self._events.pop(0)
                if now - self._last_summary_ts >= self.window:
                    self._last_summary_ts = now
                    counts, est, uw_l, uw_p, o10_l, o10_p, o1_l, o1_p = \
                        self._window_stats(now)
                    detail = (', '.join(f'{e}×{n}' for e, n in
                                        sorted(counts.items(), key=lambda kv: -kv[1]))
                              or '无')
                    line = (f"📊 [限流观测] 近{self.window:.0f}s 调用: {detail}"
                            f" | 估算weight≈{est}"
                            + self._seg('USED-WEIGHT', uw_l, uw_p)
                            + self._seg('ORDER-10S', o10_l, o10_p)
                            + self._seg('ORDER-1M', o1_l, o1_p))
            return line
        except Exception:
            return None  # 观测故障绝不外泄

    def snapshot_last(self, seconds=None):
        """窗口内各 endpoint 调用次数（429/418 归因核心证据）。"""
        try:
            now = self._time_fn()
            sec = self.window if seconds is None else float(seconds)
            with self._lock:
                counts = {}
                for ts, e, _st, _uw, _o10, _o1 in self._events:
                    if ts >= now - sec:
                        counts[e] = counts.get(e, 0) + 1
            return counts
        except Exception:
            return {}

    def format_incident(self, func, headers, err, seconds=None):
        """429/418 事发快照：前 window 秒调用面 + 三指标窗口轨迹 + 本次错误 header。"""
        try:
            ep = self._endpoint_of(func)
            now = self._time_fn()
            sec = self.window if seconds is None else float(seconds)
            with self._lock:
                counts, est, uw_l, uw_p, o10_l, o10_p, o1_l, o1_p = \
                    self._window_stats(now, seconds)
            detail = (', '.join(f'{k}×{v}' for k, v in
                                sorted(counts.items(), key=lambda kv: -kv[1]))
                      or '无')

            def _v(x):
                return 'N/A' if x is None else f'{x:.0f}'
            parts = [f"🔬 [限流观测·事发快照] {ep} 触发限流（{str(err)[:120]}）",
                     f"前{sec:.0f}s 本程序调用面: {detail} | 估算weight≈{est}"]
            if uw_l is not None:
                parts.append(f"USED-WEIGHT 窗口最新={uw_l:.0f} 峰值60s={uw_p:.0f}")
            if o10_l is not None:
                parts.append(f"ORDER-10S 窗口最新={o10_l:.0f} 峰值60s={o10_p:.0f}")
            if o1_l is not None:
                parts.append(f"ORDER-1M 窗口最新={o1_l:.0f} 峰值60s={o1_p:.0f}")
            parts.append(f"本次错误 header: USED-WEIGHT={_v(self._hdr_val(headers, 'used-weight-1m'))}"
                         f" | ORDER-10S={_v(self._hdr_val(headers, 'order-count-10s'))}"
                         f" | ORDER-1M={_v(self._hdr_val(headers, 'order-count-1m'))}")
            return parts[0] + '\n  └─ ' + '\n  └─ '.join(parts[1:])
        except Exception as e:
            return f"🔬 [限流观测·事发快照] 采集失败: {e}"


# 模块级单例：_safe_api_call 直连（绕开实例初始化顺序问题；观测数据跨实例共享无害）
_API_METRICS = ApiMetrics()

TAKER_FEE_RATE = 0.0005
MAKER_FEE_RATE = 0.0002
# 🔥 v6.4-P6（设计 v1.3，ChatGPT FULLY ALIGNED）：守恒分级观察器宽限窗——
# 仅当存在「有效在途平仓事务」（五条件合取）时，瞬时归档滞后豁免 300s；
# 无可解释事务的守恒冲突立即 critical（f1e135 场景零延迟）。
CONSERVATION_GRACE_S = 300
SLIPPAGE_BUFFER = 0.0002

# -1021 时间戳错误重同步冷却（秒）：窗口内不重复调 load_time_difference（P0-1，堵放大器 A）
TIME_SYNC_COOLDOWN = 60

# ==================== D-010 Batch 2：AUTH_BLOCKED 鉴权熔断（盲区安全模式） ====================
# 设计依据：discussions/D-010_通知链路加固与2015分流_设计确认稿_v3.md + Batch2_实施方案_改动点清单.md
# ChatGPT 终审 2026-08-28 三条钉死约束：
#   1. 闸门位于 _safe_api_call 的 retry loop 与 try 之前，raise 于 try 外
#   2. L920 load_time_difference 直连点必须 AST + 动态 mock 零网络调用双验证
#   3. BLIND-SAFE 恢复顺序锁死：probe → RECOVERING → reconcile → clear（Fail-Closed，绝不半恢复）

# 鉴权失败白名单（ChatGPT 终审裁定：白名单式分类，不做模糊关键词猜测。
# -2015 Invalid API-key/IP/permissions、-2014 API-key format invalid、-1022 Signature 不合法。
# 注意：原实现的裸 "permissions" 模糊匹配已移除——普通业务参数错误/订单状态错误/余额不足
# 等一律不得进入 AUTH_BLOCKED，仍走原有重试路径）
AUTH_BLOCKED_ERROR_PATTERNS = ("-2015", "-2014", "-1022", "invalid api-key")

AUTH_BLOCKED_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "auth_blocked.json")
NOTIFY_QUEUE_DIR_TRADER = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".notify_queue")

# T4：盲区休眠（监控线程收到 AuthBlockedError → 300s 纯本地等待，零 API）
AUTH_BLIND_SLEEP_SECONDS = 300


class AuthBlockedError(Exception):
    """D-010 不变量⑨：AUTH_BLOCKED（盲区安全模式）下普通 Binance API 调用被拒绝。
    唯一放行路径 = _safe_api_call(..., auth_probe=True)，且该参数仅允许出现在
    _attempt_auth_recovery（探活，唯一触发点 = bot_runner 启动探活 + /auth_reset 命令）。"""
    pass


def _generate_notify_event_id() -> str:
    """D-010 T1：事件实例身份（文件名/计数键/审计引用）。
    与 bot_runner._generate_notify_event_id 格式完全一致（写入端-消费端契约）："""
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f") + "_" + uuid.uuid4().hex[:8]


def _fsync_dir(dir_path: str) -> bool:
    """D-009 P0-A：目录项 fsync（best-effort，降级绝不等于写入失败）——ChatGPT R1 批准。

    作用：os.replace 原子替换后，rename 的目录项本身也需落盘，否则断电可能
    出现"新文件内容已写入、目录项仍指向旧 inode"的撕裂。

    平台现实（本机实测 2026-08-29）：
      POSIX   —— os.open(dir, O_DIRECTORY) 可行，真落盘，返回 True
      Windows —— 三种 os.open 方式全部 PermissionError（无 O_DIRECTORY 语义）
                 → 捕获后返回 False，由调用方降级

    安全方向（ChatGPT R1 明确裁定）：Windows 下 _fsync_dir 失败**不得**被当作
    写入失败。否则会出现"状态其实已安全写入，仅因平台不支持目录 fsync 就判定
    整个保存失败"的错误安全方向。返回值仅用于诊断，绝不参与写入成败判断。
    """
    try:
        fd = os.open(dir_path, getattr(os, 'O_DIRECTORY', os.O_RDONLY))
    except (PermissionError, OSError, AttributeError):
        return False          # Windows 常态路径：静默降级，不刷屏
    try:
        os.fsync(fd)
        return True
    except (PermissionError, OSError):
        return False
    finally:
        try:
            os.close(fd)
        except Exception:
            pass


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
        # 🔥 v6.4-P6：守恒分级观察器事件存储（键=(symbol, side_upper) →
        # {'first_seen','warning_sent','critical_count'}）。事件随收敛/批次<2
        # 整份删除（≤3 critical 为单事件上限）；内存态，重启清零。
        self._conservation_events = {}
        self._conservation_event_lock = threading.Lock()

        # 🔥 P5j/P5k：限价监控所有权登记（键=(symbol, batch_id, close_op_id)
        # → thread）。判据「已预留/已运行」，检查+登记在锁内原子完成，线程退出
        # 条件释放；登记前任何失败都保持「可续跑」——绝不把「尝试过」当成
        # 「已接管」。
        self._limit_close_monitor_threads = {}
        self._limit_close_monitor_lock = threading.Lock()

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
        # 🔥 v6.2-r6（P0）：平仓流程卡死告警去重（键=batch_id，60 分钟窗口）
        # 必须在 __init__ 初始化：restart 后新进程对象不会重放 BEGIN，
        # monitor 消费侧需要该 dict 已存在（否则 AttributeError）。
        self._freeze_alerted = {}
        # 🔥 v6.4-P1：partial resize 并发在途簿记（monitor 自愈线程 vs /partial 事务线程
        # 防双 create）+ 运行期自愈节流/失败计数簿记
        self._resize_inflight = set()
        self._resize_inflight_lock = threading.Lock()
        self._partial_resume_state = {}
        self._freeze_print_state = {}  # 🔥 v6.4-P2（Fix C）：冻结 console 提示节流簿记

        # 🔥 D-010 Batch 2：AUTH_BLOCKED 盲区安全模式（auth_blocked.json 持久化，损坏 Fail-Closed）
        self.auth_blocked_file = AUTH_BLOCKED_FILE        # 测试可重定向
        self.notify_queue_dir = NOTIFY_QUEUE_DIR_TRADER   # T1 写入端队列目录（测试可重定向）
        self._auth_corruption_alerted = False             # 锁文件损坏告警去重（进程内一次）
        self._auth_recovering = False                     # T6：RECOVERING 期间仅恢复链线程放行
        self._auth_recovery_thread = None                 # T6：恢复链线程身份（threading.current_thread()）
        self._auth_recovery_lock = threading.Lock()       # B1：恢复链单飞（/auth_reset 与启动探活共用）

        if verbose:
            print("正在连接交易所并同步服务器时间/加载元数据...")
        try:
            self._safe_api_call(self.exchange.load_time_difference)
            self._safe_api_call(self.exchange.load_markets, True)

            # 🔥 强制同步服务器时间
            self._safe_api_call(self.exchange.fetch_time)
            self._safe_api_call(self.exchange.load_time_difference)
        except AuthBlockedError as e:
            # 🔥 D-010 B3 前置：持久锁存在时跳过启动初始化 API（零网络请求），
            # 探活与恢复交给 bot_runner 启动探活 / /auth_reset 命令（避免构造崩溃导致命令入口不可用）
            print(f"🔒 [D-010] 检测到 AUTH_BLOCKED 持久锁，跳过启动初始化 API 调用: {e}")

        self.last_time_sync = time.time()

        # 🔥 每日结算日报线程（daemon，每天 08:05 发送昨日结算）
        self._last_daily_report_date = None
        threading.Thread(target=self._daily_report_loop, daemon=True).start()

        # 🔥 D-009 P0（ChatGPT R2/R3 批准）：账本完整性状态位（默认"可信"，Fail-Closed 起手）
        # _state_corrupted=True  → 核心账本不可信 → 禁止恢复/接管/新建（SG1 _ready 恒 False）
        # _tombstones_degraded=True → 历史复活防线不可信 → 已存在批次放行，全新批次拒绝
        self._state_corrupted = False
        self._state_corruption_detail = ""
        self._tombstones_degraded = False

        # 🔥 P0 Batch C：墓碑文件路径（测试可重定向）+ 复活告警去重（进程内每批次一次）
        # 启动时 prune 过期墓碑（v2 §3：启动一次 + 日报顺带；文件极小无性能面）
        self.tombstone_file = TOMBSTONE_FILE
        self._tombstone_alerted = set()
        try:
            self._prune_tombstones()
        except Exception as _tomb_e:
            print(f"⚠️ [C2] 启动墓碑清理异常（不阻断启动）: {_tomb_e}")

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

        # 🔥 D-010 T5：IP 变更三通道——Email 独立并行发送（不依赖 TG 成败；
        # 8-28 事故"IP 变更无邮件提醒"根因补齐。critical 语义对齐：邮箱兜底通道）
        try:
            self._send_email_alert(msg, subject="⚠️ IP 地址变化告警")
        except Exception as e:
            print(f"⚠️ [IP告警] Email 发送失败: {e}")

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
        """备用方式：D-010 Batch 2 起写入 .notify_queue/{event_id}.notify 事件队列
        （原单槽 .notify 互相覆盖已淘汰），由 bot_runner 消费循环送达。
        写入端不写 state——C3 语义：queue 有 state 无 → 消费端新建 ACTIVE 正常发送。"""
        try:
            # 构建纯文本消息（去掉 Markdown 特殊字符）
            plain_msg = (
                f"⚠️ IP 地址已变化！\n"
                f"新 IP: {ip}\n"
                f"来源: {source}\n"
                f"时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"请将新 IP 添加到币安 API 白名单！"
            )
            event_id = self._enqueue_notify_event("ip_notify", plain_msg)
            if event_id:
                print(f"📝 [备用通知] 已入队事件 {event_id} → {self.notify_queue_dir}")
        except Exception as e:
            import traceback
            print(f"⚠️ [备用通知] 写入失败: {e}")
            traceback.print_exc()

    def _enqueue_notify_event(self, notify_type: str, plain_msg: str) -> str | None:
        """D-010 T1/三通道：进程级原子入队（C1：tmp → flush → os.replace）。
        落点 .notify_queue/{event_id}.notify，内容 `type|msg`（写入-消费端共用契约）。
        type 只描述事件来源/语义，不承担去重职责。失败返回 None（调用方自行降级）。"""
        qdir = self.notify_queue_dir
        tmp_path = None
        try:
            os.makedirs(qdir, exist_ok=True)
            event_id = _generate_notify_event_id()
            final_path = os.path.join(qdir, f"{event_id}.notify")
            fd, tmp_path = tempfile.mkstemp(dir=qdir, prefix=".tmp_", suffix=".notify")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(f"{notify_type}|{plain_msg}")
                f.flush()
            os.replace(tmp_path, final_path)
            return event_id
        except Exception as e:
            print(f"⚠️ [D-010] 通知事件入队失败: {e}")
            if tmp_path:
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
            return None

    # ==================== D-010 Batch 2：AUTH_BLOCKED 状态管理 ====================

    def _load_auth_state(self) -> dict:
        """读取 auth_blocked.json。返回 {'locked': bool, 'state': str, 'reason': str}。
        文件不存在 → 未锁。存在但无法可靠解析 → 状态未知 = Fail-Closed 按 BLOCKED 处理
        （ChatGPT 终审批定：程序无法证明"当前没有 AUTH_BLOCKED"时继续访问 API 不安全），
        且必须显式告警，不伪装成正常运行。告警进程内去重（每次损坏进程生命周期仅 1 次）。"""
        path = self.auth_blocked_file
        if not os.path.exists(path):
            return {'locked': False, 'state': 'UNBLOCKED', 'reason': ''}
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, dict):
                state = str(data.get('state', 'BLOCKED')).upper()
                locked = bool(data.get('blocked', True)) and state != 'UNBLOCKED'
                return {'locked': locked, 'state': state,
                        'reason': str(data.get('reason', ''))}
            raise ValueError(f"非 dict 结构: {type(data).__name__}")
        except Exception as e:
            if not self._auth_corruption_alerted:
                self._auth_corruption_alerted = True
                self._alert_auth_state_corrupted(e)
            return {'locked': True, 'state': 'BLOCKED',
                    'reason': f'AUTH_BLOCKED 状态文件损坏，系统进入 Fail-Closed 盲区安全模式（{e}）'}

    def _alert_auth_state_corrupted(self, exc: Exception) -> None:
        """锁文件损坏告警（三通道 + 含 Fail-Closed/盲区字样，不伪装正常运行）"""
        msg = (f"🚨 AUTH_BLOCKED 状态文件损坏（auth_blocked.json 无法解析）\n"
               f"系统进入 Fail-Closed 盲区安全模式（BLIND-SAFE）：无法证明当前没有鉴权封锁，"
               f"按已封锁处理，全部 Binance API 已停摆。\n"
               f"解析错误: {exc}\n"
               f"处理方式：人工检查/修复或删除 auth_blocked.json 后重启（重启会走启动探活恢复链）\n"
               f"时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"\n{'=' * 60}\n🔒 {msg}\n{'=' * 60}")
        try:
            self.send_tg_notification(msg, level='critical')   # critical → 自动 Email 兜底
        except Exception as e:
            print(f"⚠️ [D-010] 锁损坏告警 TG 发送失败: {e}")
        try:
            self._enqueue_notify_event("auth_blocked", msg)    # 队列事件（消费端安全网）
        except Exception as e:
            print(f"⚠️ [D-010] 锁损坏告警入队失败: {e}")

    def _save_auth_state(self, state: str, reason: str) -> None:
        """原子写 auth_blocked.json（tmp → flush → os.replace，项目惯例）。
        state ∈ {BLOCKED, RECOVERING, UNBLOCKED}；UNBLOCKED 保留在文件中作审计痕迹（blocked=false）。"""
        path = self.auth_blocked_file
        data = {'blocked': state in ('BLOCKED', 'RECOVERING'), 'state': state,
                'reason': reason[:500],
                'updated_at': time.strftime('%Y-%m-%d %H:%M:%S')}
        tmp_path = None
        try:
            d = os.path.dirname(path) or '.'
            fd, tmp_path = tempfile.mkstemp(dir=d, prefix='.tmp_', suffix='.json')
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
                f.flush()
            os.replace(tmp_path, path)
        except Exception as e:
            print(f"⚠️ [D-010] auth_blocked.json 写入失败: {e}")
            if tmp_path:
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass

    def _enter_auth_blocked(self, reason: str) -> None:
        """进入 AUTH_BLOCKED 盲区安全模式：写锁 + 三通道告警（TG + Email + 队列事件）。
        告警文本必须包含"盲区安全模式"字样与已知盲区声明（设计稿 §3.3，用户知情）。"""
        try:
            self._save_auth_state('BLOCKED', reason)
        except Exception:
            pass  # _save_auth_state 内部已打印告警；闸门在后续调用重读文件兜底
        msg = (f"鉴权失败，程序已进入盲区安全模式（BLIND-SAFE）\n"
               f"原因: {reason[:300]}\n"
               f"已知盲区：仓位可能已被 SL/TP 平掉而程序不知；无法撤改剩余条件单；无法同步仓位状态\n"
               f"安全依赖：交易所侧既有 SL/TP 条件单继续生效\n"
               f"全部 Binance API 调用已停摆（普通调用 0 次网络请求）\n"
               f"恢复方式：修复 IP 白名单/API 权限后发送 /auth_reset（探活→对账→自动解锁）\n"
               f"时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"\n{'=' * 60}\n🔒 [D-010] {msg}\n{'=' * 60}")
        try:
            self.send_tg_notification(msg, level='critical')   # TG + Email（critical 自动邮箱兜底）
        except Exception as e:
            print(f"⚠️ [D-010] AUTH_BLOCKED TG 告警失败: {e}")
        try:
            self._enqueue_notify_event("auth_blocked", msg)    # 队列事件（消费循环安全网）
        except Exception as e:
            print(f"⚠️ [D-010] AUTH_BLOCKED 队列事件入队失败: {e}")

    def _attempt_auth_recovery(self) -> tuple:
        """BLIND-SAFE 恢复链（ChatGPT 钉死约束 3，顺序锁死 Fail-Closed）：
        auth_probe 探活 → 成功 → RECOVERING（锁保持，仅恢复链线程可用 API）
        → reconcile（复用 recover_active_batches，R-A/B/C/D 自愈家族）
        → reconcile 成功 → clear（UNBLOCKED）→ 普通监控恢复。
        任一步失败 → 保持锁（绝不半恢复，绝不"probe 成功直接 clear"）。
        单飞：_auth_recovery_lock 非阻塞获取（/auth_reset 与启动探活共用，防并发探活）。"""
        if not self._auth_recovery_lock.acquire(blocking=False):
            return False, "已有恢复链在运行中，请稍候再试（防并发探活）"
        try:
            # Step 1：探活（唯一放行路径：auth_probe=True → fetch_balance，1 次受控网络请求）
            try:
                self._safe_api_call(self.exchange.fetch_balance, auth_probe=True)
            except Exception as e:
                self._save_auth_state('BLOCKED', f'探活失败，保持锁定: {e}')
                return False, f"探活失败（保持 AUTH_BLOCKED 盲区安全模式）: {e}"
            # Step 2：RECOVERING（锁保持；仅本恢复链线程的 API 调用放行）
            self._save_auth_state('RECOVERING', '探活成功，reconcile 进行中（锁保持）')
            self._auth_recovering = True
            self._auth_recovery_thread = threading.current_thread()
            try:
                # Step 3：reconcile——复用既有启动恢复链（向交易所现实收敛，盲区期不一致由其兜住）
                try:
                    ok = self.recover_active_batches()
                except Exception as e:
                    ok = False
                    print(f"🚨 [D-010] 恢复期 reconcile 异常: {e}")
                if not ok:
                    self._save_auth_state('BLOCKED', '恢复期 reconcile 失败，重新锁定（Fail-Closed）')
                    return False, "reconcile 失败，保持 AUTH_BLOCKED（盲区安全模式），可稍后重试 /auth_reset"
                # Step 4：clear（顺序锁死：先 reconcile 后 clear——绝不半恢复）
                self._save_auth_state('UNBLOCKED', '探活 + reconcile 均成功，鉴权已恢复')
                msg = ("鉴权已恢复（盲区安全模式解除）\n"
                       f"探活与对账均成功，普通监控已恢复。\n"
                       f"时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
                try:
                    self.send_tg_notification(msg, level='info')
                except Exception as e:
                    print(f"⚠️ [D-010] 恢复成功 TG 通知失败: {e}")
                return True, "恢复完成：探活 + reconcile 均成功，已解锁"
            finally:
                self._auth_recovering = False
                self._auth_recovery_thread = None
        finally:
            self._auth_recovery_lock.release()

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
                             mode: str, pnl_partial: bool = False,
                             dedup_key: str | None = None,
                             stats_file: str | None = None) -> None:
        """记录一笔已实现盈亏到 trade_stats.json（原子写入，失败静默）

        dedup_key —— P5：同一 close 订单的 PnL 幂等记录（/closecancel 与
        _monitor_limit_close 共享 finalizer，双线程/崩溃重试只记一次）。
        pnl_partial=True —— 本次实际成交**小于台账**（此前存在未被跟踪的减仓：
        手动减仓 / ADL / 他方平仓）。此时 net_pnl 仅覆盖本次成交部分，**不是该
        批次的完整已实现盈亏**；落 `prior_reduction_unknown` 标记供日报/汇总识别。
        返回 bool：True=已记录（含 dedup 命中=幂等成功），False=写盘失败
        （P5 finalizer 依赖此返回值：失败必须保持 phase=2，绝不 clear——否则
        成交记录永久丢失）。"""
        try:
            base_dir = os.path.dirname(os.path.abspath(__file__))
            stats_file = stats_file or os.path.join(base_dir, "trade_stats.json")
            with self._state_lock:
                stats = {}
                if os.path.exists(stats_file):
                    try:
                        with open(stats_file, "r", encoding="utf-8") as f:
                            stats = json.load(f)
                    except Exception:
                        stats = {}
                if dedup_key and any(r.get('dedup_key') == dedup_key
                                     for r in stats.get('trades', []) or []):
                    return True  # 幂等：同订单 PnL 已记录（finalizer 接管/重试语义）
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
                if dedup_key:
                    record['dedup_key'] = dedup_key
                stats.setdefault("trades", []).append(record)
                with tempfile.NamedTemporaryFile("w", dir=os.path.dirname(stats_file), delete=False, encoding="utf-8") as tf:
                    json.dump(stats, tf, ensure_ascii=False, indent=2)
                    temp_name = tf.name
                os.replace(temp_name, stats_file)
            return True
        except Exception as e:
            print(f"⚠️ [盈亏记录] 写入失败: {e}")
            return False

    def _count_active_batches(self, all_states):
        """D-006: 统计全账户活跃批次数与带活跃批次的交易对集合（零 API，只读本地状态文件）"""
        total = 0
        symbols = set()
        for sym, symbol_batches in (all_states or {}).items():
            if not isinstance(symbol_batches, dict):
                continue
            for b_id, b_data in symbol_batches.items():
                if isinstance(b_data, dict) and b_data.get('is_active'):
                    total += 1
                    symbols.add(sym)
        return total, symbols

    def _get_today_realized_pnl(self, stats_file=None):
        """D-006: 求当日（北京时间）已实现盈亏总和，数据源 trade_stats.json。
        返回 (pnl_sum, ok)：
        - 文件不存在 = 无历史 = (0.0, True)
        - 文件存在但读不出 / JSON 非法 / 根节点非 dict = (0.0, False) → 调用方 Fail-Closed
        （与 D-005 去重表损坏 Fail-Closed 同哲学：未知状态 ≠ 允许）"""
        if stats_file is None:
            stats_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trade_stats.json")
        if not os.path.exists(stats_file):
            return 0.0, True
        try:
            with open(stats_file, "r", encoding="utf-8") as f:
                stats = json.load(f)
        except Exception:
            return 0.0, False
        if not isinstance(stats, dict) or not isinstance(stats.get("trades", []), list):
            return 0.0, False
        today = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")
        total = 0.0
        for t in stats["trades"]:
            if not isinstance(t, dict):
                continue
            t_time = t.get("time")
            if not isinstance(t_time, str) or not t_time.startswith(today):
                continue
            try:
                total += float(t.get("net_pnl", 0.0))
            except (TypeError, ValueError):
                continue
        return round(total, 4), True

    def _check_account_risk(self, all_states, signal, stats_file=None):
        """D-006: 账户层风控闸门（只判定不通知——通知由 execute_signal 调用方负责，沿用 SG 门风格）。
        限额调用时读 env 不缓存（改 .env 即时生效，无需重启）；限额 <=0 视为禁用。
        已批准限额（2026-08-28）：批次 3 / 交易对 1 / 日亏损暂不启用（0）/ MAX_LEVERAGE 100。
        返回 (allowed, reason)。"""
        # RISK_MAX_ACTIVE_BATCHES: 活跃批次总数达到上限即拒绝新批次
        try:
            max_batches = int(os.getenv("RISK_MAX_ACTIVE_BATCHES", "3"))
        except (TypeError, ValueError):
            max_batches = 3
        # RISK_MAX_ACTIVE_SYMBOLS: 新交易对会使带仓交易对数超上限即拒绝（同 symbol 加仓放行）
        try:
            max_symbols = int(os.getenv("RISK_MAX_ACTIVE_SYMBOLS", "1"))
        except (TypeError, ValueError):
            max_symbols = 1
        # RISK_DAILY_REALIZED_LOSS_LIMIT: 当日已实现亏损上限 USDT（0 = 暂不启用）
        try:
            daily_loss_limit = float(os.getenv("RISK_DAILY_REALIZED_LOSS_LIMIT", "0") or 0)
        except (TypeError, ValueError):
            daily_loss_limit = 0.0
        # MAX_LEVERAGE: 杠杆上限（与 bot_runner 统一配置；现役代码此前无 trader 层强制，本闸门补齐）
        try:
            max_leverage = int(os.getenv("MAX_LEVERAGE", "100"))
        except (TypeError, ValueError):
            max_leverage = 100

        total_batches, active_symbols = self._count_active_batches(all_states)
        if max_batches > 0 and total_batches >= max_batches:
            return False, (f"活跃批次总数 {total_batches} 已达上限 {max_batches}"
                           f"（RISK_MAX_ACTIVE_BATCHES）")
        if max_symbols > 0:
            new_symbol = signal.symbol not in active_symbols
            effective_symbols = len(active_symbols) + (1 if new_symbol else 0)
            if effective_symbols > max_symbols:
                return False, (f"带活跃批次的交易对数将达 {effective_symbols}，超过上限 {max_symbols}"
                               f"（RISK_MAX_ACTIVE_SYMBOLS，当前: {sorted(active_symbols)}）")
        try:
            leverage = int(signal.leverage)
        except (TypeError, ValueError):
            return False, f"信号杠杆值非法: {signal.leverage!r}"
        if leverage <= 0:
            return False, f"信号杠杆 {leverage}x 非正值"
        if max_leverage > 0 and leverage > max_leverage:
            return False, f"信号杠杆 {leverage}x 超过上限 {max_leverage}x（MAX_LEVERAGE）"

        if daily_loss_limit > 0:
            pnl_today, stats_ok = self._get_today_realized_pnl(stats_file)
            if not stats_ok:
                return False, ("trade_stats.json 损坏，无法评估当日盈亏（Fail-Closed）。"
                               "修复或删除该文件后下一条信号自动恢复，无需重启；"
                               "期间存量批次的止盈止损/平仓/监控不受影响")
            if pnl_today <= -daily_loss_limit:
                return False, (f"当日已实现亏损 {pnl_today:.2f} USDT 已达上限 {daily_loss_limit:.2f}"
                               f"（RISK_DAILY_REALIZED_LOSS_LIMIT，北京时间次日自动重置）")
        return True, ""

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
                    filled_amount, _ = self._batch_net_position(b_data)  # v6.4：净仓位
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
            # P0 Batch C（v2 §3）：日报顺带 prune 过期墓碑（TTL 7 天）
            self._prune_tombstones()
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

    @staticmethod
    def _format_429_diagnostics(func, headers, err):
        """🔥 v6.4（429 诊断增强）：只采集证据——原始错误 + Retry-After +
        X-MBX-USED-WEIGHT-* + X-MBX-ORDER-COUNT-*（429 也可能来自 order rate
        limit，缺后者时 order-count 型 429 会查不出真因）+ endpoint。
        不改变任何限频/冷却/重试策略。"""
        try:
            fname = getattr(func, '__name__', str(func))
            h = headers or {}
            retry_after = h.get('Retry-After') or h.get('retry-after')
            used = {k: v for k, v in h.items() if 'used-weight' in str(k).lower()}
            order_count = {k: v for k, v in h.items()
                           if 'order-count' in str(k).lower()}
            return (f"🔬 [429诊断] endpoint={fname} | Retry-After={retry_after} | "
                    f"used-weight={used} | order-count={order_count} | err={err}")
        except Exception as diag_e:
            return f"🔬 [429诊断] 证据采集失败: {diag_e}"

    @staticmethod
    def _effective_429_cooldown(base_cooldown, retry_after):
        """🔥 v6.4（429 协议缺口修复）：普通 429 冷却 = max(基础随机值, Retry-After + 1s)。

        官方语义：429 的 Retry-After 是避免升级 418 封禁的应等时长；无视会累积
        strike。无有效 Retry-After → 保持原 30-60s 逻辑。不改重试次数/418 逻辑。"""
        try:
            ra = float(retry_after)
            if ra > 0:
                return max(base_cooldown, ra + 1.0)
        except (TypeError, ValueError):
            pass
        return base_cooldown

    def _safe_api_call(self, func, *args, retries=5, delay=2, auth_probe=False, **kwargs):
        # 🔥 D-010 Batch 2（ChatGPT 钉死约束 1）：AUTH_BLOCKED 入口闸门——
        # 位于 for retry loop 与 try 之前，raise 于 try 外。锁定期内下方 -1021 分支的
        # load_time_difference() 直连物理不可达（AST + 动态 mock 双验证，test S1/S5）。
        # 白名单仅两条放行：① auth_probe=True（探活，全库仅 _attempt_auth_recovery 一处出现）
        # ② RECOVERING 态且调用线程 == 恢复链线程（reconcile 期间仅恢复链可用 API）
        if not auth_probe:
            auth = self._load_auth_state()
            if auth['locked'] and not (
                auth['state'] == 'RECOVERING'
                and self._auth_recovering
                and threading.current_thread() is self._auth_recovery_thread
            ):
                raise AuthBlockedError(
                    f"[盲区安全模式] API 调用被拒绝（状态={auth['state']}）：{auth['reason'][:200]}")
        for i in range(retries):
            # 🔥 每次重试都检查全局熔断（感知其他线程设置的冷却）
            self._wait_for_api_cooldown()
            # 🔥 v6.4-P4c：本次尝试的 frozen headers（在 semaphore 临界区内快照；
            # 请求未达 Binance/本地抛错时为空 dict——口径：所有 _safe_api_call 调用
            # 尝试均入账，若本次响应提供 header 则同步记录）
            _attempt_headers = {}
            try:
                # 🔥 单次 API 请求才占用信号量
                with self._api_semaphore:
                    with self._api_lock:
                        now = time.time()
                        wait_time = self._min_api_interval - (now - self._last_api_call_time)
                        if wait_time > 0:
                            time.sleep(wait_time)
                        self._last_api_call_time = time.time()
                    # 🔥 v6.4-P4c（ChatGPT PROVEN race 收口）：request → header
                    # snapshot(dict copy) → metrics record 三件事为同一观测临界区——
                    # 必须在持有 _api_semaphore 生命周期内完成，否则 semaphore 释放后
                    # 共享 exchange.last_response_headers 会被并发请求覆盖，失败证据
                    # 归属错误（A 的 900 被记成 B 的 50）。成功/失败都在此入账。
                    # 🔥 v6.4-P4d：调用前清空共享 header 栏——「线程安全读取共享 header」
                    # ≠「header 属于本次 attempt」；本地 pre-network 抛错未产生新响应时，
                    # 上一笔遗留值绝不归属本次（R16）。有响应时 ccxt 会重写该栏。
                    self.exchange.last_response_headers = {}
                    try:
                        _r = func(*args, **kwargs)
                    except Exception:
                        _attempt_headers = dict(getattr(self.exchange, 'last_response_headers',
                                                        None) or {})
                        _API_METRICS.record(func, _attempt_headers, ok=False)
                        raise
                    _attempt_headers = dict(getattr(self.exchange, 'last_response_headers',
                                                    None) or {})
                    _sum = _API_METRICS.record(func, _attempt_headers, ok=True)
                    if _sum:
                        print(_sum)
                    return _r

            except Exception as e:
                err_str = str(e).lower()

                # 🔥 D-010 Batch 2：明确鉴权失败白名单分流（ChatGPT 终审：白名单式分类，
                # 不做模糊关键词猜测——普通业务参数错误/订单状态错误/余额不足不进 AUTH_BLOCKED，
                # 仍走原有重试路径。原实现的裸 "permissions" 匹配已移除，-2015 错误文本自身含
                # "permissions" 字样，码匹配已覆盖）
                if any(p in err_str for p in AUTH_BLOCKED_ERROR_PATTERNS):
                    ip = self._extract_ip_from_error(str(e))
                    if ip:
                        print(f"🔍 [IP检测] 币安报告的 IP: {ip} (当前记录的 IP: {self.last_known_ip})")
                        self._record_ip_change(ip, source="binance_error")
                    # 立即进入 AUTH_BLOCKED 盲区安全模式 + 三通道告警，停止无意义重试
                    # （旧行为 sleep+continue 最多 5 次重试 → 已删除；8-28 事故刷屏根因之一）
                    self._enter_auth_blocked(err_str)
                    raise AuthBlockedError(
                        f"鉴权失败已进入盲区安全模式（BLIND-SAFE），API 已全部停摆。"
                        f"原始错误: {str(e)[:300]}")

                # 🔥 -1021 时间戳错误特殊处理
                if "-1021" in err_str or "recvwindow" in err_str:
                    # 🔥 重同步加冷却：TIME_SYNC_COOLDOWN 秒窗口内不重复调 load_time_difference（P0-1）
                    if time.time() - self.last_time_sync > TIME_SYNC_COOLDOWN:
                        try:
                            # R6: 收编信号量限速保护。不套 _safe_api_call——此处位于其 except 分支内，
                            # 嵌套调用在 sync 自身再抛 -1021 时有递归风险；重试职责由外层循环 continue 承担
                            # 🔥 v6.4-P4c：snapshot+record 同样收进 semaphore 临界区（同一 ownership 不变量）
                            # 🔥 v6.4-P4d：成功与失败都必须在临界区内完成 snapshot+record
                            # （失败路径原先在 semaphore 释放后才 record——同一 race）；
                            # 调用前清空共享 header 栏（同 R16 语义）
                            with self._api_semaphore:
                                self.exchange.last_response_headers = {}
                                try:
                                    self.exchange.load_time_difference()
                                except Exception:
                                    _API_METRICS.record(
                                        self.exchange.load_time_difference,
                                        dict(getattr(self.exchange, 'last_response_headers',
                                                     None) or {}), ok=False)
                                    raise
                                self.last_time_sync = time.time()
                                _API_METRICS.record(self.exchange.load_time_difference,
                                                    dict(getattr(self.exchange,
                                                                 'last_response_headers',
                                                                 None) or {}), ok=True)
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

                    # 🔥 v6.4（429 诊断增强）：只采集证据，不改任何限频/冷却/重试策略
                    # 🔥 v6.4-P4c：只消费本次尝试的 frozen _attempt_headers——
                    # 绝不重读共享 exchange.last_response_headers（可能已被并发请求覆盖，
                    # 429 归因会得出相反结论）
                    print(self._format_429_diagnostics(func, _attempt_headers, e))
                    # 🔥 v6.4-P4/P4b：事发快照（失败调用已在 semaphore 临界区内入账）——
                    # 三指标窗口轨迹 + 本次错误 header 归因证据链
                    print(_API_METRICS.format_incident(func, _attempt_headers, e))

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
                    # 🔥 v6.4（429 协议缺口修复）：冷却 = max(原随机值, Retry-After + 1s)——
                    # 官方要求 429 后按 Retry-After back off，无视会累积升级 418 封禁。
                    # 无有效值保持原 30-60s 逻辑；不改重试次数/418 逻辑。
                    _ra_hdrs = getattr(self.exchange, 'last_response_headers', None) or {}
                    _ra = _ra_hdrs.get('Retry-After') or _ra_hdrs.get('retry-after')
                    global_cooldown = self._effective_429_cooldown(
                        30 + random.uniform(0, 30), _ra)
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
        """D-009 P0-A（ChatGPT R2 批准）：读取状态账本——三态分离，杜绝"损坏=空"的致命误读。

        三种"空"的安全含义完全不同，旧实现把它们全部塌缩成 {}：
          ① 文件不存在    → 首次启动，确实没有历史批次        （可正常 READY）
          ② 合法 {} / dict → 所有批次已清理完毕               （可正常 READY）
          ③ 读取失败/根非 dict → 账本损坏                     （Fail-Closed，禁止 READY）

        ③ 的正确语义是"不知道有哪些批次"，绝不是"没有批次"。若按 ① 处理，
        进程会以空账本启动 → 不接管任何历史批次 → 交易所上的真实持仓变成
        无人监控的孤儿仓；随后新开仓还会把损坏账本覆盖掉，证据彻底灭失。

        损坏时置位 self._state_corrupted=True 并返回 {}（占位，返回值在损坏态
        下无意义）。调用方读取返回值前必须先判 _state_corrupted。
        """
        self._state_corrupted = False
        self._state_corruption_detail = ""
        if not os.path.exists(STATE_FILE):
            return {}
        try:
            with open(STATE_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except Exception as e:
            self._state_corrupted = True
            self._state_corruption_detail = f"{type(e).__name__}: {e}"
            print(f"🚨 [D-009] trade_state.json 读取失败（账本损坏，Fail-Closed）: {e}")
            return {}
        if not isinstance(data, dict):
            self._state_corrupted = True
            self._state_corruption_detail = f"根节点类型非法: {type(data).__name__}"
            print(f"🚨 [D-009] trade_state.json 根节点非 dict（账本损坏，Fail-Closed）")
            return {}
        return data

    def _persist_states(self, all_states: dict) -> bool:
        """R12: 状态持久化唯一入口（调用方必须已持有 _state_lock）。
        备份 last-known-good 到 .bak 后原子写入新状态。
        边界：首次保存无文件则跳过备份；备份失败仅警告绝不阻断主保存
        （C3 是恢复纵深，不改变既有保存契约）。

        D-009 P0-A（ChatGPT R1 批准）：补全持久化链
            json.dump → flush → os.fsync(file) → os.replace → _fsync_dir(尽力降级)
        原子写(os.replace)只保证"不会读到半写文件"，不保证"已落盘"；断电场景下
        缺 fsync 会产生 0 字节或截断文件（实证：.notify.state.json，2026-08-29）。
        ⚠️ fsync 是降概率器，不是安全边界——真正的安全边界是下文的损坏 Fail-Closed。

        D-009 P0-B：账本已损坏时拒绝覆盖写入（Fail-Closed）。
        损坏态下 load_all_states 返回的是 {}，若照常写回，会把磁盘上残留的其余
        批次一次性抹掉，把"读失败"升级成"证据灭失"。宁可停止写入，不可毁证据。
        """
        # D-009 P0-B：损坏账本禁止覆盖（未知 ≠ 空账本）
        if getattr(self, '_state_corrupted', False) is True:
            print(f"🚫 [D-009] 拒绝覆盖写入：trade_state.json 已损坏，"
                  f"账本内容不可信（保护现场待人工恢复）")
            try:
                self.send_tg_notification(
                    f"🚨【资金安全】状态写入被拒绝（账本已损坏）\n"
                    f"trade_state.json 读取失败，程序拒绝覆盖写入以保护现场。\n"
                    f"错误: `{self._state_corruption_detail[:150]}`\n"
                    f"⚠️ 系统已停止交易（READY=False）。请人工修复或重命名该文件后重启。",
                    level='critical')
            except Exception:
                pass
            return False
        try:
            if os.path.exists(STATE_FILE):
                shutil.copy2(STATE_FILE, STATE_FILE + '.bak')
        except Exception as bak_e:
            print(f"⚠️ [R12] 状态备份失败（不阻断保存）: {bak_e}")
        dir_name = os.path.dirname(STATE_FILE) or "."
        try:
            with tempfile.NamedTemporaryFile("w", dir=dir_name, delete=False, encoding="utf-8") as tf:
                json.dump(all_states, tf, indent=4, ensure_ascii=False)
                tf.flush()                 # D-009 P0-A：先刷 Python/ libc 缓冲
                os.fsync(tf.fileno())      # D-009 P0-A：再落盘，然后才允许 replace
                temp_name = tf.name
            os.replace(temp_name, STATE_FILE)
            _fsync_dir(dir_name)           # D-009 P0-A：目录项尽力落盘（Windows 降级返回 False）
            return True
        except Exception as e:
            print(f"⚠️ 保存状态文件失败: {e}")
            return False

    def save_batch_state(self, symbol: str, batch_id: str, batch_data: dict):
        """P0 Batch C（v2 §5 + v3 §5/§6）：状态落盘单咽喉 = 墓碑检查 + 字段级 merge。
        C2：见墓碑（TTL 内）→ 拒绝写入（Fail-Closed，已清理批次复活通道封死）+
            🚨 critical 告警（锁外发送，防持锁 5s TG 超时；进程内每批次一次去重）。
        C1：磁盘既有批次按七类规则 merge（A 棘轮 / G user_modified OR / B 单调账本 /
            C registry 逐 identity / D id 镜像 / E 静态 / F 簿记最新者胜），
            旧快照不得降级安全面（B5 陈旧覆盖的语义级修复）。
        merge 后字段集合 = 磁盘 ∪ 快照（快照新增字段正常写入，磁盘独有字段补回）。"""
        _tomb_alert = False
        _tomb_degraded_reject = False
        with self._state_lock:
            tombstones = self._load_tombstones()
            # D-009 Q3：墓碑损坏 → DEGRADED（每次 _load_tombstones 重新判定，非粘性）
            _degraded = getattr(self, '_tombstones_degraded', False) is True
            t_entry = tombstones.get(batch_id)
            if isinstance(t_entry, dict):
                try:
                    _age = time.time() - float(t_entry.get('cleared_at', 0) or 0)
                except (TypeError, ValueError):
                    _age = 0.0
                if _age < TOMBSTONE_TTL_SECONDS:
                    _tomb_alert = True
            if not _tomb_alert:
                all_states = self.load_all_states()
                # D-009 Q3 分治（ChatGPT R3 批准）：墓碑证明的是"该批次曾存在且已结束，
                # 不得复活"，不是"该批次存在"。因此存在性由 trade_state 自己证明：
                #   已存在 batch_id → trade_state 已证明其存在，墓碑非必要条件 → 放行
                #   全新 batch_id   → 必须排除"是已清理批次复活"；墓碑损坏无法排除 → 拒绝
                # 准则：未知状态 ≠ 允许。
                if _degraded and not isinstance((all_states.get(symbol) or {}).get(batch_id), dict):
                    _tomb_degraded_reject = True
                else:
                    if symbol not in all_states:
                        all_states[symbol] = {}
                    existing = all_states[symbol].get(batch_id)
                    if isinstance(existing, dict) and existing:
                        batch_data = self._merge_batch_state(existing, batch_data)
                    all_states[symbol][batch_id] = batch_data
                    self._persist_states(all_states)
        if _tomb_degraded_reject:
            _dkey = ('tombstone_degraded', batch_id)
            if _dkey not in getattr(self, '_tombstone_alerted', set()):
                try:
                    self._tombstone_alerted.add(_dkey)
                except Exception:
                    pass
                self.send_tg_notification(
                    f"🚨【资金安全】墓碑不可信，拒绝创建全新批次\n"
                    f"批次 `{batch_id}` ({symbol}) 在本地账本中不存在，且墓碑文件 "
                    f"trade_tombstones.json 读取失败。\n"
                    f"无法排除该批次是已清理批次的复活（陈旧线程/旧快照回写）。\n"
                    f"⚠️ 已存在批次的更新不受影响，仅阻断新建。请人工修复墓碑文件。",
                    level='critical')
            else:
                print(f"🪦 [D-009] 墓碑 DEGRADED 拦截新建批次 {batch_id}（告警已去重）")
            return
        if _tomb_alert:
            if batch_id not in getattr(self, '_tombstone_alerted', set()):
                try:
                    self._tombstone_alerted.add(batch_id)
                except Exception:
                    pass
                self.send_tg_notification(
                    f"🚨【资金安全】已清理批次复活尝试被阻断\n"
                    f"批次 `{batch_id}` 已在墓碑登记（7 天 TTL 内），save_batch_state 拒绝写入。\n"
                    f"疑似陈旧线程/旧快照回写。请人工核查 trade_tombstones.json 与该批次来源。",
                    level='critical')
            else:
                print(f"🪦 [C2] 墓碑拦截 save（批次 {batch_id}，复活告警已去重）")

    def clear_batch_state(self, symbol: str, batch_id: str, proof=None,
                          authorization=None) -> bool:
        """P0 Batch B（proof 门，ChatGPT APPROVED 2026-08-29）：close 清理从
        「状态删除动作」升级为「经过证明的状态迁移」。唯一入口
        clear_batch_state(symbol, batch_id, proof=<converge 返回的 proof dict>)；
        禁止任何 force/skip_verify/proof_required 布尔逃生门（G-B9 签名守卫）。
        Fail-Closed：proof 缺失/非法 → 拒绝（不删 state、不写墓碑、不发
        close_phase=3，锁外 critical 告警后 return False，批次保留待下轮 converge）。
        close_phase=3（CLOSED）唯一写入点在本函数墓碑落盘内（G-B9 正则锚定）。
        批次已不存在 → 幂等 return True（无状态可保护）。
        Batch C（v2 §3）：清理即写墓碑；converged_order_ids = registry 已终态条目
        的 order_id ∪ proof.l1_canceled ∪ proof.l2_canceled（Batch B 升级）；
        幂等：墓碑已存在（重复 clear）不覆盖 cleared_at。"""
        if getattr(self, '_tp_breaker_alerted', None):  # 终态清理熔断告警键（ChatGPT 终审 2026-08-20，长期运行内存管理）
            self._tp_breaker_alerted = {k: v for k, v in self._tp_breaker_alerted.items() if k[0] != batch_id}
        # converge 告警计数随批次清理一并修剪（镜像 _tp_breaker_alerted 惯例，防长期运行内存增长）
        _counts = getattr(self, '_converge_alert_counts', None)
        if isinstance(_counts, dict):
            self._converge_alert_counts = {k: v for k, v in _counts.items()
                                           if not (isinstance(k, tuple) and batch_id in k)}
        _reject = None
        with self._state_lock:
            all_states = self.load_all_states()
            b_data = (all_states.get(symbol) or {}).get(batch_id)
            if b_data is None:
                return True  # 已清理/不存在 → 幂等成功（无状态可保护）
            _reject = self._verify_clear_proof(symbol, batch_id, proof, b_data)
            # 🔥 P5h（ChatGPT 七复审 P0-2）：删除授权与删除在同一 _state_lock 内
            # 原子绑定——授权校验若发生在锁外，"校验通过 → 取锁 → 删除"之间
            # 仍可发生 settled/manual_review/op 迁移，旧线程会删掉新状态。
            # authorization=(close_op_id, close_reason, settled, limit_close_order_id)
            if _reject is None and authorization is not None:
                _cur_snap = (b_data.get('close_op_id') or '',
                             b_data.get('close_reason') or '',
                             bool(b_data.get('settled_by_limit_close')),
                             b_data.get('limit_close_order_id') or '')
                if _cur_snap != tuple(authorization):
                    _reject = (f'清理授权失效：批次状态已迁移'
                               f'({tuple(authorization)} → {_cur_snap})，拒绝删除')
            if _reject is None:
                # C2：先落墓碑再删 state（删记忆后墓碑是唯一防线，顺序不可倒）
                try:
                    tombstones = self._load_tombstones()
                    if not isinstance(tombstones.get(batch_id), dict):
                        _converged = sorted(
                            {str(ent.get('order_id'))
                             for ent in (b_data.get('protection_registry') or {}).values()
                             if isinstance(ent, dict) and ent.get('order_id')
                             and ent.get('state') in _REGISTRY_TERMINAL_STATES}
                            | {str(_i) for _i in (proof.get('l1_canceled') or [])}
                            | {str(_i) for _i in (proof.get('l2_canceled') or [])})
                        _t_entry = {
                            'symbol': symbol,
                            'side': b_data.get('side'),
                            'cleared_at': time.time(),
                            'converged_order_ids': _converged,
                            'known_order_ids': self._collect_batch_order_ids(b_data),
                        }
                        # P0 Batch B：close_phase=3（CLOSED）唯一写入点（G-B9 正则锚定，
                        # 全库仅此一处对 close_phase 赋值 3）
                        _t_entry['close_phase'] = 3
                        tombstones[batch_id] = _t_entry
                        self._persist_tombstones(tombstones)
                except Exception as tomb_e:
                    print(f"⚠️ [C2] 墓碑落盘失败（不阻断清理，但请人工检查）: {tomb_e}")
                del all_states[symbol][batch_id]
                if not all_states[symbol]:
                    del all_states[symbol]
                self._persist_states(all_states)
                print(f"🧹 批次 [{batch_id}] 状态归档/清理完毕（proof 门通过，"
                      f"墓碑已登记 close_phase=3，7 天防复活）。")
                return True
        # 锁外拒绝告警（TG I/O 不进 _state_lock；同键 3 轮去重防刷屏）
        self._converge_alert(('clear_rejected', symbol, batch_id),
                             f"🚨【资金安全】批次 `{batch_id}`({symbol}) 清理被 proof 门拒绝"
                             f"（{_reject}）。状态保留，待下轮 converge 生成收敛证明后重试"
                             f"（Fail-Closed，无程序侧逃生门）。", level='critical')
        return False

    # ==================== P0 Batch C：墓碑 / 字段级 merge ====================

    def _load_tombstones(self) -> dict:
        """C2：墓碑读取。缺文件/损坏 → 空 dict + 置位 _tombstones_degraded（D-009 Q3）。

        与 trade_state 的处理刻意不同，原因是两者的安全角色不同：
          trade_state 损坏 → 核心账本不可信 → _state_corrupted → _ready=False（全面停摆）
          墓碑损坏        → 仅"反复活防线"不可信 → _tombstones_degraded → 系统保持 READY，
                            但**全新批次**因无法排除复活而被拒绝（save_batch_state 闸门）

        损坏态返回 {} 意味着"无法证明任何批次是已清理的"，故绝不可用于放行新建。
        """
        path = getattr(self, 'tombstone_file', TOMBSTONE_FILE)
        self._tombstones_degraded = False
        if not os.path.exists(path):
            return {}
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except Exception as e:
            self._tombstones_degraded = True
            print(f"⚠️ [D-009] 墓碑文件读取失败（复活防护降级，禁止新建批次）: {e}")
            return {}
        if not isinstance(data, dict):
            self._tombstones_degraded = True
            print(f"⚠️ [D-009] 墓碑根节点非 dict（复活防护降级，禁止新建批次）")
            return {}
        return data

    def _persist_tombstones(self, tombstones: dict) -> bool:
        """C2：墓碑原子写（调用方必须已持有 _state_lock，与 _persist_states 同范式）。
        D-009 P0-A（ChatGPT R1 批准）：json.dump → flush → os.fsync → os.replace
        → _fsync_dir(尽力降级)。墓碑是"反复活"的唯一防线，其完整性优先于主账本。"""
        path = getattr(self, 'tombstone_file', TOMBSTONE_FILE)
        dir_name = os.path.dirname(path) or "."
        try:
            with tempfile.NamedTemporaryFile("w", dir=dir_name, delete=False,
                                             encoding="utf-8") as tf:
                json.dump(tombstones, tf, indent=4, ensure_ascii=False)
                tf.flush()                 # D-009 P0-A
                os.fsync(tf.fileno())      # D-009 P0-A
                temp_name = tf.name
            os.replace(temp_name, path)
            _fsync_dir(dir_name)           # D-009 P0-A：Windows 降级返回 False，不阻断
            return True
        except Exception as e:
            print(f"⚠️ [C2] 保存墓碑文件失败: {e}")
            return False

    def _prune_tombstones(self) -> None:
        """C2：TTL 过期清理（启动 + 日报线程顺带）。持锁 prune，异常不外溢。"""
        try:
            with self._state_lock:
                tombstones = self._load_tombstones()
                if not tombstones:
                    return
                now = time.time()
                pruned = {}
                for bid, entry in tombstones.items():
                    try:
                        age = now - float((entry or {}).get('cleared_at', 0) or 0)
                    except (TypeError, ValueError):
                        age = 0.0
                    if age < TOMBSTONE_TTL_SECONDS:
                        pruned[bid] = entry
                if len(pruned) != len(tombstones):
                    self._persist_tombstones(pruned)
                    print(f"🪦 [C2] 墓碑 TTL 清理：{len(tombstones) - len(pruned)} 条过期移除，"
                          f"剩余 {len(pruned)} 条")
        except Exception as e:
            print(f"⚠️ [C2] 墓碑清理异常: {e}")

    def _collect_batch_order_ids(self, b_data: dict) -> list:
        """C2：收集批次已知订单 id 全集（镜像字段 + registry），供墓碑溯源。"""
        ids = []
        for key in _MERGE_ID_MIRROR_FIELDS:
            v = (b_data or {}).get(key)
            if v:
                ids.append(str(v))
        for ent in ((b_data or {}).get('protection_registry') or {}).values():
            if isinstance(ent, dict) and ent.get('order_id'):
                ids.append(str(ent['order_id']))
        seen, out = set(), []
        for i in ids:
            if i not in seen:
                seen.add(i)
                out.append(i)
        return out

    def _merge_batch_state(self, disk: dict, snap: dict) -> dict:
        """C1 字段级 merge（v2 §5.1 七类 + v3 §5：user_modified 移出棘轮归 G 类）。
        disk = 磁盘现状（较新事实的载体），snap = 调用方快照（可能陈旧）。
        返回合并后的新 dict；磁盘独有字段补回（union 语义）。"""
        merged = dict(snap)  # E/F/未知字段默认最新者胜（快照覆盖）
        # —— G 类（v3 §5）：user_modified 事实字段取 OR，绝不参与安全判定 ——
        merged['user_modified'] = bool(disk.get('user_modified')) or bool(snap.get('user_modified'))
        # —— A 类棘轮：close_phase int max + Boolean False→True 单向 ——
        try:
            merged['close_phase'] = max(int(disk.get('close_phase', 0) or 0),
                                        int(snap.get('close_phase', 0) or 0))
        except (TypeError, ValueError):
            pass
        for f in _MERGE_RATCHET_BOOL_FIELDS:
            if disk.get(f) and not snap.get(f):
                merged[f] = disk[f]  # 磁盘 True 快照 False → 保留 True
        # —— B 类单调账本：结算线程已计的成交/费用不被旧快照抹掉 ——
        try:
            merged['last_filled_count'] = max(int(disk.get('last_filled_count', 0) or 0),
                                              int(snap.get('last_filled_count', 0) or 0))
        except (TypeError, ValueError):
            pass
        try:
            merged['total_entry_fee'] = max(float(disk.get('total_entry_fee', 0.0) or 0.0),
                                            float(snap.get('total_entry_fee', 0.0) or 0.0))
        except (TypeError, ValueError):
            pass
        _fd, _fs = disk.get('filled_details') or [], snap.get('filled_details') or []
        if isinstance(_fd, list) and isinstance(_fs, list):
            _fd_out = []
            for i in range(max(len(_fd), len(_fs))):
                a = _fd[i] if i < len(_fd) else None
                b = _fs[i] if i < len(_fs) else None
                if a is None:
                    _fd_out.append(b)
                elif b is None:
                    _fd_out.append(a)
                else:
                    try:
                        _fd_out.append(a if float(a) >= float(b) else b)
                    except (TypeError, ValueError):
                        _fd_out.append(b)
            merged['filled_details'] = _fd_out
        # —— C 类 registry 逐 identity merge ——
        _dr = disk.get('protection_registry') or {}
        _sr = snap.get('protection_registry') or {}
        if _dr or _sr:
            merged_reg = dict(_sr)
            for ident, d_ent in _dr.items():
                s_ent = _sr.get(ident)
                if s_ent is None:
                    merged_reg[ident] = d_ent  # 磁盘独有 identity 补回（防丢更新）
                    continue
                if not (isinstance(d_ent, dict) and isinstance(s_ent, dict)):
                    continue
                if d_ent.get('state') in _MERGE_REGISTRY_PROTECTED_STATES:
                    merged_reg[ident] = dict(d_ent)  # 未决/已锁/终态 → 磁盘为准
                else:
                    # 磁盘 FAILED/ABSENT → updated_at 新者胜（允许后续重建翻正）
                    try:
                        d_newer = (float(d_ent.get('updated_at', 0) or 0)
                                   >= float(s_ent.get('updated_at', 0) or 0))
                    except (TypeError, ValueError):
                        d_newer = False
                    merged_reg[ident] = dict(d_ent) if d_newer else dict(s_ent)
            merged['protection_registry'] = merged_reg
        # —— D 类 id 镜像：磁盘有 id 快照清空 → 仅活单且未进结算才保留 ——
        _phase = 0
        try:
            _phase = int(merged.get('close_phase', 0) or 0)
        except (TypeError, ValueError):
            _phase = 0
        _reg_now = merged.get('protection_registry') or {}
        for f in _MERGE_ID_MIRROR_FIELDS:
            d_v, s_v = disk.get(f), snap.get(f)
            if d_v is not None and s_v is None and _phase < 2:
                _id_live = any(
                    isinstance(e, dict) and str(e.get('order_id', '')) == str(d_v)
                    and e.get('state') not in _REGISTRY_TERMINAL_STATES
                    for e in _reg_now.values())
                if _id_live:
                    merged[f] = d_v
        # —— 磁盘独有字段补回（union；快照新增字段已在 dict(snap) 中） ——
        for k, v in disk.items():
            if k not in merged:
                merged[k] = v
        return merged

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

                filled_amount, net_cost = self._batch_net_position(b_data)  # v6.4：净仓位/净成本
                if filled_amount <= 0:
                    return None

                # v6.4：剩余 fee 按 cost 比例分摊（fee 模型 = price×amount×r ∝ 成本 notional，
                # 非 qty 比例；ChatGPT 反例验算：partial→不同价新层后 qty 比例会低估剩余 fee）
                _rr_cost = float(b_data.get('realized_reduce_cost', 0.0) or 0.0)
                _gross_cost = net_cost + _rr_cost
                fee_rem = float(total_entry_fee or 0.0) * net_cost / _gross_cost \
                    if _gross_cost > 0 else 0.0
                avg_price = (net_cost + fee_rem) / filled_amount

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

    def _run_position_census(self) -> list:
        """D-009 P0-C（ChatGPT Q2 阶段一最小版）：交易所持仓普查（反向对账的只读半边）。

        触发场景：本地账本损坏 → 唯一可信的仓位来源是交易所。此时若不普查，
        运维只能看到"账本空"，会误判为无持仓。

        严格只读：不撤单、不平仓、不改单、不写任何本地状态。完整的自动
        reconciliation 属阶段二；本阶段只负责"把真相摆到人类面前"。

        返回 [(symbol, side, contracts), ...]（仅含非零仓位）；
        查询失败返回 None —— UNKNOWN ≠ EMPTY，绝不可退化成空列表。
        """
        try:
            positions = self._safe_api_call(self.exchange.fetch_positions)
        except Exception as e:
            print(f"⚠️ [D-009] 交易所持仓普查失败（UNKNOWN，绝不当作无持仓）: {e}")
            return None
        found = []
        for pos in (positions or []):
            if not isinstance(pos, dict):
                continue
            try:
                amt = float(pos.get('contracts', 0) or pos.get('positionAmt', 0) or 0)
            except (TypeError, ValueError):
                continue
            if abs(amt) <= 0:
                continue
            _info = pos.get('info') if isinstance(pos.get('info'), dict) else {}
            _sym = pos.get('symbol') or _info.get('symbol') or '<unknown>'
            _side = pos.get('side') or ('long' if amt > 0 else 'short')
            found.append((str(_sym), str(_side), abs(amt)))
        return found

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

        # D-009 P0-A/P0-B（ChatGPT R2/R3 批准）：账本损坏 → Fail-Closed。
        # 损坏读出的 {} 只表示"不知道有哪些批次"，绝不表示"没有批次"。此时接管任何
        # 批次都可能把交易所上的真实持仓变成无人监控的孤儿仓，因此：
        # 禁止恢复、禁止接管、禁止置位 READY（SG1 随即封死全部风险增加入口）。
        # 刻意**不退出进程**：退出 → watchdog 重启 → 账本仍坏 → 再次退出 = 无限重启，
        # 不增加任何安全性；存活则可保住 TG 告警 / 查询命令 / 人工介入通道。
        if getattr(self, '_state_corrupted', False) is True:
            _detail = getattr(self, '_state_corruption_detail', '') or '未知原因'
            self._not_ready_reason = (f"trade_state.json 损坏，账本不可信"
                                      f"（{_detail[:80]}），已停止交易待人工恢复")
            print(f"🚨 [D-009] trade_state.json 损坏（{_detail}）")
            print(f"   └─ 禁止接管任何历史批次，READY 保持 False，进程存活等待人工介入")
            # .bak 仅做**存在性诊断**，绝不 open / 装载其内容（ChatGPT R3：
            # .bak 是 last-state-before-this-write 而非 last-known-good，
            # 实测其内含 is_active=True 幽灵批次，静默恢复 = 复活已清理批次）。
            _bak_path = STATE_FILE + '.bak'
            try:
                if not os.path.exists(_bak_path):
                    _bak_note = f"{_bak_path} 不存在，无恢复候选"
                elif os.path.getsize(_bak_path) > 0:
                    _bak_note = (f"{_bak_path} 存在（{os.path.getsize(_bak_path)} 字节），"
                                 f"仅可作为人工恢复候选证据，程序绝不自动装载")
                else:
                    _bak_note = f"{_bak_path} 存在但为空，无恢复价值"
            except OSError:
                _bak_note = f"{_bak_path} 状态不可读取"
            # D-009 P0-C：账本不可信 → 唯一可信来源是交易所（只读普查）
            _census = self._run_position_census()
            if _census is None:
                _pos_note = "⚠️ 普查失败（UNKNOWN）——绝不可当作无持仓"
            elif _census:
                _pos_note = "\n".join(f"   • {_s} {_sd} {_a}" for _s, _sd, _a in _census)
            else:
                _pos_note = "交易所当前无持仓（普查成功，0 个仓位）"
            print(f"🔍 [D-009] 交易所持仓普查（只读）：{_pos_note}")
            try:
                self.send_tg_notification(
                    f"🚨【资金安全】本地账本损坏，已停止交易\n"
                    f"trade_state.json 读取失败: `{_detail[:150]}`\n"
                    f"已禁止接管任何历史批次，READY=False（进程存活，TG 与查询命令可用）。\n"
                    f"备份诊断: {_bak_note}\n"
                    f"交易所持仓普查（只读，未执行任何自动操作）:\n{_pos_note}\n"
                    f"⚠️ 请人工核对后修复或重命名该文件，再重启程序。",
                    level='critical')
            except Exception:
                pass
            return False

        for symbol, symbol_batches in all_states.items():
            for batch_id, b_data in symbol_batches.items():
                if b_data.get('is_active'):
                    print(f"\n🔄 [状态恢复] 识别到未完成的历史活跃任务 [{batch_id}] ({symbol})，正在检查...")

                    # 🔥 检查是否有错误标记（之前监控线程崩溃）
                    # 🔥 P5f（ChatGPT 五复审 P0-1）：monitor_error 分支位于所有守卫
                    # 之前——冻结态/PnL 守卫批次不得经此被静默清理：
                    #   manual_review → 保持冻结 + 告警；
                    #   settled+phase2 → 路由 finalizer（PnL 门是唯一清批守卫）；
                    #   限价事务在途 → 交恢复分型（FULL_FILL 结算 / 否则冻结）。
                    if b_data.get('monitor_error', False):
                        _me_takeover = False
                        _me_reason = (b_data.get('close_reason') or '')
                        # 🔥 P5g（ChatGPT 六复审 P0-3）：优先级 settled > manual_review
                        # ——settled 已认领结算的批次必须先续跑 finalizer，绝不能
                        # 因 manual_review 判断在前而永久冻结
                        if b_data.get('settled_by_limit_close') \
                                and b_data.get('limit_close_order_id'):
                            self._finalize_limit_full_fill(
                                symbol, batch_id, b_data['limit_close_order_id'])
                            continue
                        if _me_reason == 'limit_cancel_manual_review':
                            print(f"  └─ 🧊 批次 [{batch_id}] 有错误标记但处于人工核对"
                                  f"冻结（归属未明确），保留不清理")
                            self.send_tg_notification(
                                f"🚨【资金安全】批次 `{batch_id}` 监控异常退出且处于"
                                f"人工核对冻结（限价撤单 + 仓位已被止损归零）。\n"
                                f"💡 请核对交易所成交记录后人工处理（未被自动清理）。",
                                level='critical')
                            continue
                        if b_data.get('settled_by_limit_close') \
                                and b_data.get('limit_close_order_id'):
                            self._finalize_limit_full_fill(
                                symbol, batch_id, b_data['limit_close_order_id'])
                            continue
                        if _me_reason in ('limit_pending_normal',
                                          'limit_cancel_restore_pending'):
                            self._handle_limit_close_on_recovery(symbol, batch_id)
                            _me_after = ((self.load_all_states().get(symbol, {}) or {})
                                         .get(batch_id) or {})
                            if not _me_after.get('is_active'):
                                continue  # 已结算归档
                            # 🔥 P5h（ChatGPT 七复审 P0-3）：批次仍在（限价在途 →
                            # PENDING 仅重建限价监控）→ 必须继续走下方正常接管，
                            # 否则限价监控退出后该批次失去主监控所有权（裸仓风险）
                            print(f"  └─ 👁️ 批次 [{batch_id}] 错误标记批次恢复后仍活跃，"
                                  f"继续正常接管监控")
                            # b_data 刷新为磁盘最新视图，供下方接管流程使用
                            # （monitor_error 保留为崩溃证据：限价理由批次已不再
                            #  因该标记被无条件清理，且强行清除会被下游陈旧快照回写）
                            b_data = _me_after
                            # 不 continue：落回下方「有挂单或持仓 → 正常恢复」接管流程
                            _me_takeover = True
                        if not _me_takeover:
                            print(f"  └─ ⚠️ 批次 [{batch_id}] 有错误标记，跳过恢复并清理")
                            stale_batches.append((symbol, batch_id))
                            continue

                    # 🔥 验证批次是否真的还有挂单或持仓
                    entry_orders = b_data.get('entry_orders', [])
                    last_filled_count = b_data.get('last_filled_count', 0)

                    # 检查是否有未成交的挂单
                    has_pending_orders = len(entry_orders) > last_filled_count

                    # 检查是否有持仓
                    # 🔥 P5i（ChatGPT 八复审 P0）：Hedge Mode 下同一 symbol 有
                    # LONG/SHORT 双记录——必须按本批次 side 取方向感知净仓。
                    # 旧实现取首个同 symbol 记录即 break，若先读到对侧零仓位
                    # 会误判"无持仓" → 落入 stale/恢复分支 → 主监控永不接管。
                    try:
                        current_pos = self._get_current_position_amt(
                            symbol, bool(b_data.get('is_hedge_mode', False)),
                            b_data.get('side') or 'BUY')
                    except Exception:
                        current_pos = None  # R11: UNKNOWN ≠ EMPTY，查询失败不得当作无持仓

                    has_position = current_pos is not None and current_pos > 0

                    # 🔥 如果既没有挂单也没有持仓(已确认)，清理这个批次
                    if not has_pending_orders and not has_position and current_pos is not None:
                        # 🔥 P5e（ChatGPT 四复审 P0）：限价平仓事务在途/人工核对冻结
                        # 且仓位已归零——绝不 stale-clear（R18 组合竞态的命令路径变体
                        # 与重启场景）→ 交恢复分型（FULL_FILL→finalizer 结算；
                        # 零/部分成交→原子写 manual_review 持久冻结）
                        if (b_data.get('close_reason') in (
                                'limit_pending_normal',
                                'limit_cancel_restore_pending',
                                'limit_cancel_manual_review')):
                            print(f"  └─ 🧊 批次 [{batch_id}] 限价平仓事务在途/"
                                  f"人工核对冻结且仓位归零，交恢复分型（不清理）")
                            self._handle_limit_close_on_recovery(symbol, batch_id)
                            continue
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

                    # 🔥 v6.4-P0：partial 崩溃分型续跑（生产调用点——重启看到
                    # partial_resize_pending 只续 resize；partial_closing loud 拒续跑）
                    _pc_reason = (b_data.get('close_reason') or '')
                    if _pc_reason in ('partial_closing', 'partial_resize_pending'):
                        self._handle_partial_close_on_recovery(symbol, batch_id)
                    # 🔥 P5：限价平仓分型（restore_pending 自动续跑 / limit_pending_normal
                    # 重拉监控或终态裁决）——重启后限价监控不自动重生的既有缺口闭环
                    if _pc_reason in ('limit_pending_normal',
                                      'limit_cancel_restore_pending'):
                        self._handle_limit_close_on_recovery(symbol, batch_id)

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

        # 🔥 清理无效的批次（P0 Batch B：converge 收敛证明后才 clear，Fail-Closed；
        # G-B7 锁定语义：monitor_error 批次也必须先撤本批次交易所残单才能清理）
        for symbol, batch_id in stale_batches:
            _proof = self._converge_batch_orders_before_clear(symbol, batch_id)
            if _proof is not None and self.clear_batch_state(symbol, batch_id, proof=_proof):
                print(f"  └─ 🧹 已清理无效批次 [{batch_id}]（proof 收敛通过）")
            else:
                print(f"  └─ ⚠️ [B] 无效批次 [{batch_id}] 本轮未收敛"
                      f"（UNKNOWN/撤单失败），保留状态待下轮恢复重试")

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
        current_filled_amount, tp_net_cost = self._batch_net_position(target_b_data)  # v6.4：净仓位
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

        # v6.4：TP vwap 用净成本（partial 后 gross vwap 会虚高）
        vwap = tp_net_cost / current_filled_amount if current_filled_amount > 0 else 0.0

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
            # G2（P0 Batch A）：create 紧前关闭态复核——失败流入下方既有 not-allowed 分支（零重缩进）
            if allowed:
                _g2_ok, _g2_reason = self._final_pre_create_check(
                    target_symbol, batch_id, tp_identity, desc='用户修改止盈')
                if not _g2_ok:
                    allowed, gate_reason = False, _g2_reason
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
        current_filled_amount, _ = self._batch_net_position(target_b_data)  # v6.4：净仓位
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
            # G2（P0 Batch A）：create 紧前关闭态复核——失败流入下方既有 not-allowed 分支（零重缩进）
            if allowed:
                _g2_ok, _g2_reason = self._final_pre_create_check(
                    target_symbol, batch_id, sl_identity, desc='用户修改止损')
                if not _g2_ok:
                    allowed, gate_reason = False, _g2_reason
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
        current_filled_amount, net_cost = self._batch_net_position(target_b_data)  # v6.4：净仓位/净成本

        if current_filled_amount <= 0:
            return False, f"⚠️ 批次 `{batch_id}` 尚未建仓，无法计算保本价！"

        # v6.4：名义/含费均价均基于净仓位/净成本；剩余 fee 按 cost 比例分摊（fee ∝ notional）
        _rr_cost = float(target_b_data.get('realized_reduce_cost', 0.0) or 0.0)
        _gross_cost = net_cost + _rr_cost
        fee_rem = float(total_entry_fee or 0.0) * net_cost / _gross_cost \
            if _gross_cost > 0 else 0.0

        # 计算名义均价（不含手续费）
        nominal_avg = net_cost / current_filled_amount

        # 计算含费均价（实际保本价）
        actual_avg = (net_cost + fee_rem) / current_filled_amount

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
        current_filled_amount, _ = self._batch_net_position(b_data)  # v6.4：净仓位

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
            # G2（P0 Batch A）：create 紧前关闭态复核——失败流入下方既有 not-allowed 分支（零重缩进）
            if allowed:
                _g2_ok, _g2_reason = self._final_pre_create_check(
                    symbol, batch_id, sl_identity, desc='保本损')
                if not _g2_ok:
                    allowed, gate_reason = False, _g2_reason
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
            # 🔥 v6.2-P0-1（实盘 2026-09-01 17:4x）：100x 下成交到发现 SL/TP 缺失的
            # 裸仓窗口最长 ~80s 不可接受 → 压到 5~15s 级。fast_poll 机制保留
            # （检测到成交后仍加速到 3s），API 权重增量可接受（远低于 1~3s 轮询）。
            base_interval = 10.0
            jitter_range = 5.0
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
            # 🔥 v6.4（consumer 补漏）：SG2 按净仓位累计——partial 后 gross > net，
            # 会假触发「台账 > 交易所」分支（变量名 last_filled 曾致清单漏网）
            program_position += self._batch_net_position(b_data)[0]
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

        # ③ SL 有效性校验：双通道（普通 + stop=True 条件单）——SL 是条件单，
        # 普通通道看不到（P0-F1 同族：单通道 = 假"缺少有效止损单"，
        # 2026-09-02 实盘首曝于已成交批次+活 SL+再加仓场景）；
        # 任一通道失败 = UNKNOWN = 拒绝（Fail-Closed，与 conflict scan 同款）
        try:
            open_orders = self._safe_api_call(self.exchange.fetch_open_orders, symbol)
        except Exception as e:
            return False, f"SL 状态查询失败（{str(e)[:80]}），无法确认保护状态"
        try:
            stop_orders = self._safe_api_call(self.exchange.fetch_open_orders, symbol,
                                              params={'stop': True})
        except Exception as e:
            return False, f"SL 状态查询失败-条件单通道（{str(e)[:80]}），无法确认保护状态"
        open_ids = {str(o.get('id')) for o in list(open_orders) + list(stop_orders)}
        missing = [bid for bid, sl_id in filled_batches
                   if not sl_id or str(sl_id) not in open_ids]
        if missing:
            return False, f"批次 {', '.join(missing)} 缺少有效止损单（无 SL 或已被交易所撤除）"
        return True, ""

    def _compute_signal_fingerprint(self, signal) -> str:
        """🔥 v6.3（D-005 开仓幂等）：规范化原始信号意图指纹。

        组件 = symbol|side|entries(price,amount)|stop_steps|TP，数值全部过
        price_to_precision/amount_to_precision（77880/77880.0 不漂移）。
        描述的是【用户原始交易意图】：与当前市价无关、不使用 post-skip 层集合
        （重发时价格穿层会改变 skip 集，post-skip 指纹会在最需要拦截时漂移）。
        stop_steps 直接使用实际列表逐项规范化，不人为补层——幂等层不负责
        修复/解释缺失 stop step（异常输入由既有合法性校验负责）。
        不含 batch_id/时间戳/文案/uuid——同一意图必须得到同一指纹。
        """
        parts = [f"sym={signal.symbol}", f"side={signal.side.upper()}"]
        parts.append("entries=" + ";".join(
            f"{self.exchange.price_to_precision(signal.symbol, p)},"
            f"{self.exchange.amount_to_precision(signal.symbol, a)}"
            for p, a in signal.entries))
        parts.append("stops=" + ";".join(
            self.exchange.price_to_precision(signal.symbol, s)
            for s in signal.stop_loss_steps))
        parts.append(f"tp={self.exchange.price_to_precision(signal.symbol, signal.take_profit)}")
        return "|".join(parts)

    def _batch_net_position(self, b_data):
        """🔥 v6.4-P0：批次净仓位/净成本二元组（durable partial ledger）。

        净仓位 = Σ target_amounts[:lfc] − realized_reduce_amount
        净成本 = Σ (target_amounts×filled_details)[:lfc] − realized_reduce_cost
        realized_reduce_* 唯一写者 = PARTIAL COMMIT（_execute_partial_close）；
        fill 路径零改动（新层成交只动 target_amounts/lfc，净量随分子自动增长）。
        旧批次无字段 → get(...,0.0) → 净=gross，向后兼容零迁移。
        """
        lfc = int(b_data.get('last_filled_count', 0) or 0)
        ta = b_data.get('target_amounts') or []
        fd = b_data.get('filled_details') or []
        gross_qty = float(sum(ta[:lfc]))
        gross_cost = float(sum((ta[i] * fd[i]) for i in range(min(lfc, len(fd)))))
        return (gross_qty - float(b_data.get('realized_reduce_amount', 0.0) or 0.0),
                gross_cost - float(b_data.get('realized_reduce_cost', 0.0) or 0.0))

    def _check_conservation_conflict(self, symbol, all_states, actual, side='', tol=0.0005):
        """🔥 v6.4-P0：守恒破坏检测——actual < Σnet − tol ⇒ 归属不可知（ATTRIBUTION_CONFLICT）。

        tol 默认半档 amount 精度（0.0005），防精度抖动误报；真实发散 ≥0.001 必触发。
        多批次 + App 模糊减仓场景（f1e135 教训的一般化）：检测器，不是修复——
        触发后由人工 reconcile，不做自动归属猜测。
        🔥 v6.4-P6（设计 v1.3）：计量边界收窄为 (symbol, side)——Hedge 模式下
        LONG/SHORT 批次可并存（same_side_close_inflight 只禁同方向并行 close），
        Σnet 只累加同方向批次，杜绝「单方向 actual vs 双方向台账」误判。"""
        symbol_state = all_states.get(symbol, {}) or {}
        side_u = str(side or '').upper()
        net_sum = 0.0
        for _b in symbol_state.values():
            if not isinstance(_b, dict) or not _b.get('is_active'):
                continue
            if side_u and str(_b.get('side') or '').upper() != side_u:
                continue
            _q, _c = self._batch_net_position(_b)
            net_sum += max(_q, 0.0)
        return actual < net_sum - tol

    def _is_valid_inflight_close_txn(self, b):
        """🔥 v6.4-P6（v1.2 阻断 1 收口）：「有效在途平仓事务」五条件合取。

        回滚（L9117-9119 同型）只复位 close_phase/pending_close，保留
        close_reason/close_op_id 作审计——单看 reason 会把已回滚批次的陈旧
        标记当成在途事务，给真实外部减仓错误宽限。五条件合取下陈旧 reason
        被结构性排除（回滚后 close_phase>=1 ∧ pending_close=True 必不成立）。"""
        if not isinstance(b, dict) or not b.get('is_active'):
            return False
        if int(b.get('close_phase', 0) or 0) < 1:
            return False
        if b.get('pending_close') is not True:
            return False
        if not (b.get('close_op_id') or ''):
            return False
        reason = b.get('close_reason') or ''
        if reason not in ('market_confirming', 'limit_pending_normal',
                          'partial_closing'):
            return False
        if reason == 'limit_pending_normal' and not (b.get('limit_close_order_id') or ''):
            return False
        return True

    def _maybe_report_conservation_conflict(self, symbol, side, actual_position):
        """🔥 v6.4-P6（设计 v1.3，ChatGPT FULLY ALIGNED）：守恒分级观察器——每轮无条件调用。

        - 计量边界 (symbol, side)：Σnet 只累加同方向 active 批次；
        - 同方向活跃批次 <2 → 删除整份事件记录并 return（单批次无归属歧义，
          观察器只负责清理历史事件，绝不产生守恒告警）；
        - 事件模型：每 (symbol, side) 至多一份
          {'first_seen', 'warning_sent', 'critical_count'}；
          守恒恢复（本轮无冲突，显式收敛）或批次<2 → 整份删除；
          ≤3 次 critical 为单事件上限（取代旧 _conservation_alert_count 的
          进程生命周期上限——旧语义达 3 后未来真实事故永久静默）；
        - 分级：无「有效在途平仓事务」（五条件合取，见
          _is_valid_inflight_close_txn）→ 立即 critical（外部减仓零延迟）；
          有 → 首见 warning + CONSERVATION_GRACE_S 后升级 critical；
        - 事件内单调棘轮：critical_count>0 后不得降级回 warning、不得重新
          获得宽限；
        - 零新增 API（actual_position 由调用方传入）；锁内认领、锁外通知。"""
        try:
            all_states = self.load_all_states()
            side_u = str(side or '').upper()
            key = (symbol, side_u)
            same_side = [b for b in ((all_states.get(symbol) or {}).values())
                         if isinstance(b, dict) and b.get('is_active')
                         and str(b.get('side') or '').upper() == side_u]
            with self._conservation_event_lock:
                if len(same_side) < 2:
                    self._conservation_events.pop(key, None)
                    return
                if actual_position is None:
                    return                      # 仓位不可判定 → 不观察不改状态
                conflict = self._check_conservation_conflict(
                    symbol, all_states, actual_position, side=side_u)
                if not conflict:
                    self._conservation_events.pop(key, None)   # 显式收敛
                    return
                ev = self._conservation_events.get(key)
                # 🔥 v1.3 实施自检（R43）：事件绑定其发生时的批次集——不同批次集
                # 不可能是同一次冲突。防「全部监控线程退出 → 观察器停调 → 删除
                # 路径不可达」窗口内旧事件滞留，污染新批次集的新事件（旧
                # critical 状态经单调棘轮使新事件跳过 warning 直接 critical）。
                _cur_ids = frozenset(b.get('batch_id') or '' for b in same_side)
                if ev is not None and ev.get('_batch_ids') != _cur_ids:
                    ev = None
                if ev is None:
                    ev = self._conservation_events[key] = {
                        'first_seen': time.time(), 'warning_sent': False,
                        'critical_count': 0, '_batch_ids': _cur_ids}
                else:
                    ev['_batch_ids'] = _cur_ids
                has_inflight = any(self._is_valid_inflight_close_txn(b)
                                   for b in same_side)
                escalate = ((not has_inflight) or ev['critical_count'] > 0
                            or (time.time() - ev['first_seen']) >= CONSERVATION_GRACE_S)
                critical_msg = None
                warning_msg = None
                if escalate:
                    if ev['critical_count'] < 3:
                        ev['critical_count'] += 1
                        _ctx = ' | '.join(
                            f"{b.get('batch_id')}/net{self._batch_net_position(b)[0]:.4f}"
                            f"/ph{int(b.get('close_phase', 0) or 0)}"
                            f"/{b.get('close_reason') or '-'}" for b in same_side)
                        critical_msg = (
                            f"🚨【资金安全】仓位守恒破坏（ATTRIBUTION_CONFLICT）\n"
                            f"🧭 {symbol} {side_u}：交易所实际持仓 {actual_position} "
                            f"< Σ批次净仓位\n"
                            f"📊 同方向批次: {_ctx}\n"
                            f"💡 外部减仓归属不可知，各批次保护单数量不可信。"
                            f"请人工核对后修正台账（本事件第 {ev['critical_count']}/3 次告警）。")
                elif not ev['warning_sent']:
                    ev['warning_sent'] = True
                    warning_msg = (
                        f"⚠️【守恒观测】{symbol} {side_u} 实际持仓 < Σ批次净仓位，"
                        f"但存在在途平仓事务（归档滞后豁免窗 {CONSERVATION_GRACE_S}s）。\n"
                        f"💡 若 {CONSERVATION_GRACE_S}s 后仍发散将升级 critical；"
                        f"若为程序平仓滞后可忽略本条。")
            # 锁外通知（防持锁 I/O）
            if critical_msg:
                print(f"  └─ 🚨 [守恒检测] {symbol} {side_u} ATTRIBUTION_CONFLICT")
                self.send_tg_notification(critical_msg, level='critical')
            elif warning_msg:
                print(f"  └─ ⚠️ [守恒观测] {symbol} {side_u} 在途平仓豁免窗内（warning）")
                self.send_tg_notification(warning_msg, level='warning')
        except Exception as e:
            print(f"  └─ ⚠️ [守恒检测] 异常: {e}")

    def _handle_partial_close_on_recovery(self, symbol, batch_id):
        """🔥 v6.4-P0：恢复路径的 partial 分型接线（真实生产调用点，重启续跑入口）。

        partial_resize_pending → _resume_partial_resize 只续 resize，绝不重发 MARKET；
        partial_closing → loud critical（副作用未知），拒绝自动续跑。"""
        try:
            b = (self.load_all_states().get(symbol, {}) or {}).get(batch_id) or {}
            ok, msg = self._resume_partial_resize(symbol, batch_id,
                                                  b.get('close_op_id'))
            print(f"  └─ {'✅' if ok else '⚠️'} [partial 恢复] 批次 [{batch_id}]: {msg}")
        except Exception as e:
            print(f"  └─ ⚠️ [partial 恢复] 批次 [{batch_id}] 异常: {e}")
            self.send_tg_notification(
                f"🚨【资金安全】partial 恢复异常 批次 `{batch_id}`: {e}",
                level='critical')

    def _handle_limit_close_on_recovery(self, symbol, batch_id):
        """🔥 P5：重启后限价平仓分型（接线契约①/⑦）。

        limit_cancel_restore_pending → 自动续跑保护恢复（绝不再发 close order）；
        limit_pending_normal → 权威裁决：
          终态（TERMINAL_ZERO/PARTIAL/CONFIRMED_FULL）→ 四态分型（含 finalizer）；
          仍 live（PENDING）→ 重拉 _monitor_limit_close 线程——重启后限价监控
          不自动重生的既有缺口（否则成交只剩「持仓归零兜底结算」，Maker 口径丢失）；
          不可证明 → 保持冻结 + loud。"""
        try:
            b = (self.load_all_states().get(symbol, {}) or {}).get(batch_id) or {}
            op = b.get('close_op_id') or ''
            oid = b.get('limit_close_order_id') or ''
            reason = b.get('close_reason') or ''
            # 🔥 P5f（ChatGPT 五复审 P0-3）：结算优先级最高——settled+phase=2
            # 必须先走 finalizer 续跑，绝不能因 manual_review 判断在前而永久冻结
            if b.get('settled_by_limit_close') and oid:
                ok_s, msg_s = self._finalize_limit_full_fill(symbol, batch_id, oid)
                print(f"  └─ {'✅' if ok_s else '⚠️'} [限价平仓恢复] 批次 [{batch_id}]: {msg_s}")
                return
            # 🔥 P5e（ChatGPT 四复审 P0）：人工核对冻结态——只告警，不裁决不恢复
            if reason == 'limit_cancel_manual_review':
                self.send_tg_notification(
                    f"🚨【资金安全】批次 `{batch_id}` 处于人工核对冻结"
                    f"（限价撤单 + 仓位已被止损归零，归属未明确）。\n"
                    f"💡 请核对交易所成交记录后人工处理（重启后保持冻结）。",
                    level='critical')
                print(f"  └─ 🧊 [限价平仓恢复] 批次 [{batch_id}]: "
                      f"manual_review 冻结保持，等待人工")
                return
            # 🔥 P5e：重启时仓位已归零（SL 窗口触发）——恢复/续跑无意义，
            # 与主循环同款分型：FULL_FILL→finalizer；零/部分成交→原子写 manual_review
            if reason in ('limit_pending_normal', 'limit_cancel_restore_pending'):
                try:
                    # 🔥 P5f：Hedge Mode 下同一 symbol 有 LONG/SHORT 双记录——
                    # 必须按本批次 side 取方向感知净仓，不得取首个匹配（否则归零误判）
                    _cur = self._get_current_position_amt(
                        symbol, bool(b.get('is_hedge_mode', False)),
                        b.get('side') or 'BUY')
                    if _cur == 0.0 and oid:
                        try:
                            self._safe_api_call(self.exchange.cancel_order, oid, symbol)
                        except Exception as _ce:
                            if '-2011' not in str(_ce) and 'Unknown order' not in str(_ce):
                                print(f"  └─ ⚠️ [限价平仓恢复] 撤在途限价单失败: {_ce}")
                        _pre_net0, _ = self._batch_net_position(b)
                        _verd, _det, _fill0 = self._confirm_close_filled(
                            symbol, b.get('side') or 'BUY',
                            bool(b.get('is_hedge_mode', False)), oid,
                            expected=_pre_net0, attempts=2, delay=0.5,
                            order_kind='normal')
                        if _fill0 and float(_fill0) >= _pre_net0 - max(1e-8, _pre_net0 * 1e-6):
                            ok_ff, msg_ff = self._finalize_limit_full_fill(symbol, batch_id, oid)
                            print(f"  └─ {'✅' if ok_ff else '⚠️'} [限价平仓恢复] "
                                  f"批次 [{batch_id}]: {msg_ff}")
                            return
                        ok_mr, msg_mr = self._mark_limit_cancel_manual_review(
                            symbol, batch_id, close_op_id=op, order_id=oid)
                        print(f"  └─ {'🧊' if ok_mr else '⚠️'} [限价平仓恢复] "
                              f"批次 [{batch_id}] 仓位已归零（filled={_fill0}）: {msg_mr}")
                        return
                except Exception as _rz:
                    print(f"  └─ ⚠️ [限价平仓恢复] 仓位归零分型异常: {_rz}（按原流程继续）")
            if reason == 'limit_cancel_restore_pending':
                ok, msg = self._resume_closecancel_restore(symbol, batch_id, op)
                print(f"  └─ {'✅' if ok else '⚠️'} [限价平仓恢复] 批次 [{batch_id}]: {msg}")
                return
            if not oid:
                return
            pre_net, _c = self._batch_net_position(b)
            verdict, detail, _filled = self._confirm_close_filled(
                symbol, b.get('side') or 'BUY', bool(b.get('is_hedge_mode', False)),
                oid, expected=pre_net, attempts=2, delay=0.5, order_kind='normal')
            if verdict in ('TERMINAL_ZERO', 'PARTIAL', 'CONFIRMED_FULL'):
                ok, msg = self._adjudicate_closed_limit_close(symbol, batch_id, oid)
                print(f"  └─ {'✅' if ok else '⚠️'} [限价平仓恢复] 批次 [{batch_id}]: {msg}")
                return
            if verdict == 'PENDING':
                # 限价单仍 live → 重拉监控线程（durable 派生 monitor 入参）
                # 🔥 P5k（ChatGPT 十复审 P0）：与正常挂单路径**共用**同一加锁
                # helper——两个入口各自「检查→登记→启动」在并发下会同时看到空位
                # （或看到对方已登记但未 start、is_alive()==False 而覆盖），
                # 导致同订单两个监控；且无条件 pop 会删掉仍存活线程的所有权。
                _guard_key = (symbol, batch_id, op)
                lfc = int(b.get('last_filled_count', 0) or 0)
                ta = list(b.get('target_amounts') or [])
                fd = list(b.get('filled_details') or [])
                gross = float(sum(ta[:lfc]))
                gross_cost = float(sum(ta[i] * fd[i] for i in range(min(lfc, len(fd)))))
                avg = (gross_cost / gross) if gross > 0 else 0.0
                if not self._start_limit_close_monitor_once(
                        _guard_key, (symbol, batch_id, oid, gross, avg,
                                     float(b.get('total_entry_fee', 0.0) or 0.0),
                                     b.get('side') or 'BUY', lfc, ta, fd)):
                    print(f"  └─ 👁️ [限价平仓恢复] 批次 [{batch_id}] "
                          f"限价监控已预留/在运行，跳过重复启动")
                    return
                print(f"  └─ 👁️ [限价平仓恢复] 批次 [{batch_id}] 限价单在途，监控线程已重生")
                return
            self.send_tg_notification(
                f"🚨【资金安全】限价平仓单状态不可证明 批次 `{batch_id}`\n"
                f"💡 {verdict}: {detail[:200]}\n"
                f"⚠️ 保持冻结，请人工核对交易所限价单后处理（或 /closecancel）。",
                level='critical')
            print(f"  └─ ⚠️ [限价平仓恢复] 批次 [{batch_id}]: {verdict}（{detail[:120]}）")
        except Exception as e:
            print(f"  └─ ⚠️ [限价平仓恢复] 批次 [{batch_id}] 异常: {e}")

    def _start_limit_close_monitor_once(self, guard_key, monitor_args):
        """🔥 P5k：限价监控「启动一次」——正常挂单与重启恢复两个入口**共用**。

        判据：`guard_key` 已存在 = 线程已成功启动（见下方不变量）。
        🔒 P5l（ChatGPT 十一复审 P0）：「预留」≠「保证启动」——旧版先登记后
        锁外 start()，并发方在窗口内拿到 False（误以为已有存活 owner），随后
        start() 失败清表 → 双方均返回但零 owner 零监控（R33b 确定复现）。
        必要不变量：**本 helper 返回 False 时，key 对应线程必然已成功 start()**。
        实现：start() + 登记全部在锁内完成，且仅在 start() 成功后登记：
        - 并发方在锁上排队，要么看到已登记（=已成功启动），要么自己启动；
        - start() 失败 → 零登记零留痕，异常原样上抛（调用方走既有失败路径）；
        - 锁内 start() 无死锁：新线程仅在退出时才回调
          _release_limit_monitor_ownership 抢同一把锁，而 start() 的返回只依赖
          bootstrap 的 _started 事件（先于线程体执行），不依赖该回调。
        返回 True=本次启动；False=已有已启动的 owner，本次跳过。"""
        with self._limit_close_monitor_lock:
            if guard_key in self._limit_close_monitor_threads:
                return False
            th = threading.Thread(target=self._monitor_limit_close_owned,
                                  args=(guard_key,) + tuple(monitor_args),
                                  daemon=True)
            th.start()
            self._limit_close_monitor_threads[guard_key] = th
        return True

    def _release_limit_monitor_ownership(self, guard_key, owner):
        """仅当登记项确实属于 owner 时才释放——防止旧线程退出抹掉新 owner。"""
        with self._limit_close_monitor_lock:
            if self._limit_close_monitor_threads.get(guard_key) is owner:
                self._limit_close_monitor_threads.pop(guard_key, None)

    def _monitor_limit_close_owned(self, guard_key, *args, **kwargs):
        """🔥 P5j：限价监控所有权包裹——线程退出（正常成交收敛 / 异常）时释放
        `_limit_close_monitor_threads` 登记，使下一次恢复可重新接管。"""
        try:
            self._monitor_limit_close(*args, **kwargs)
        finally:
            self._release_limit_monitor_ownership(guard_key,
                                                  threading.current_thread())

    def _execute_partial_close(self, symbol, batch_id, amount):
        """🔥 v6.4-P0：批次指定部分平仓——intent-before-effect 纪律的平仓侧复用。

        事务：BEGIN(partial_closing, transient) → 市价减单 → exact confirm
        → CAS 持久化净账本 + reason→partial_resize_pending（同一次原子提交）
        → 保护单 resize（撤旧挂净量；G1/G2/G3b owner exception 窄放行）
        → 最终 CAS 回 ACTIVE。pending ENTRY 原样保留。
        崩溃语义：partial_closing 阶段崩溃 = loud/人工；partial_resize_pending =
        _resume_partial_resize 续跑，绝不重发 MARKET。
        """
        all_states = self.load_all_states()
        b = (all_states.get(symbol, {}) or {}).get(batch_id)
        if not isinstance(b, dict) or not b.get('is_active'):
            return False, 'batch_missing_or_inactive'
        net_qty, _net_cost = self._batch_net_position(b)
        # 🔥 v6.4 三审（P0-2）：/partial 必须严格部分平仓——amount == net 全平后
        # post_net=0 仍进 resize（create amount=0）且 pending ENTRY 保留却无保护维护。
        # 全量平仓请走 /close（完整 ENTRY 撤除 + 结算 + 归档链）。
        if amount <= 0 or amount >= net_qty - 1e-12:
            return False, (f'invalid_amount（净仓位 {net_qty}；/partial 只接受严格小于'
                           f'净仓位的数量，全量平仓请使用 /close）')
        ok, close_op_id, reason, snapshot = self._begin_close_request_if_active(
            symbol, batch_id, 'partial_closing')
        if not ok:
            return False, f'begin_failed（{reason}）'
        side = ((snapshot or b).get('side') or 'BUY').upper()
        params_base = dict((snapshot or b).get('params_base') or {})
        # 🔥 v6.4 三审（P0-1）：BEGIN 后先过生产 coverage/守恒 guard——多批次
        # actual < Σnet（归属冲突）在此 Fail-Closed，绝不带冲突继续减 aggregate
        # 重演 wrong-close。此时尚无交易所副作用 → 冲突即 CAS 回滚 BEGIN。
        is_hedge_g = bool((snapshot or b).get('is_hedge_mode', False))
        guard_amount, guard_detail = self._close_amount_guard(
            symbol, side, is_hedge_g, amount, batch_id)
        if guard_amount is None or abs(float(guard_amount) - amount) > 1e-9:
            self._rollback_close_request_if_current(symbol, batch_id, close_op_id)
            return False, f'partial_guard_rejected（{guard_detail}）'
        reduce_side = 'sell' if side == 'BUY' else 'buy'
        try:
            order = self._safe_api_call(self.exchange.create_order,
                                        symbol, 'market', reduce_side, amount, None,
                                        params_base)
            order_id = str((order or {}).get('id') or '')
            if not order_id:
                raise ValueError('empty order id')
        except Exception as e:
            self._set_close_reason_if_current(symbol, batch_id, close_op_id,
                                              f'partial_error_order（{e}）')
            return False, f'partial_order_failed（{e}）'
        # 🔥 v6.4-P0 rework（P0-1）：复用生产六态确认器——订单存在 ≠ 成交。
        # 只有被 _confirm_close_filled 证明的 filled 才可进 durable COMMIT；
        # PENDING/UNKNOWN/NOT_CONFIRMED 绝不 COMMIT（防台账凭空减仓 + 保护欠覆盖）。
        is_hedge = bool((snapshot or b).get('is_hedge_mode', False))
        verdict, detail, filled = self._confirm_close_filled(
            symbol, side, is_hedge, order_id, amount, pos_before=None,
            order_kind='normal')
        if verdict in ('CONFIRMED_FULL', 'PARTIAL') and filled:
            confirmed_amount = float(filled)
        elif verdict == 'TERMINAL_ZERO':
            # 权威 filled==0 且终态 → 唯一可回滚分支（v5 语义）：安全撤销 BEGIN
            self._rollback_close_request_if_current(symbol, batch_id, close_op_id)
            return False, 'partial_zero_fill_rolled_back'
        else:
            # PENDING/UNKNOWN/NOT_CONFIRMED → 保持冻结（partial_closing=loud），人工接管
            self.send_tg_notification(
                f"🚨【资金安全】partial 确认失败（{verdict}）批次 `{batch_id}`\n"
                f"💡 {detail}\n请核对交易所实际仓位与挂单后人工处理。",
                level='critical')
            return False, f'partial_confirm_{verdict}'
        # CAS 持久化净账本 + reason→partial_resize_pending（rollback 事务模板：
        # 锁内重读 → 校验 op_id/phase → 同一锁段写账本 → _persist_states）
        with self._state_lock:
            latest = self.load_all_states()
            b2 = (latest.get(symbol, {}) or {}).get(batch_id)
            if not isinstance(b2, dict) or (b2.get('close_op_id') or '') != close_op_id \
                    or int(b2.get('close_phase', 0) or 0) != 1 \
                    or b2.get('close_reason') != 'partial_closing':
                return False, 'state_changed（重启恢复路径接管）'
            pre_net_qty, pre_net_cost = self._batch_net_position(b2)
            if confirmed_amount > pre_net_qty + 1e-12:
                return False, f'confirm_exceeds_net（{confirmed_amount} > {pre_net_qty}）'
            reduce_cost_delta = confirmed_amount * pre_net_cost / pre_net_qty
            b2['realized_reduce_amount'] = float(
                b2.get('realized_reduce_amount', 0.0) or 0.0) + confirmed_amount
            b2['realized_reduce_cost'] = float(
                b2.get('realized_reduce_cost', 0.0) or 0.0) + reduce_cost_delta
            b2['close_reason'] = 'partial_resize_pending'
            b2['partial_resize_stage'] = 0  # 🔥 v6.4-P1：分腿进度（0=未开始/1=SL 完成/2=TP 完成）
            if not self._persist_states(latest):
                return False, 'persist_failed'
            # 🔥 resize 必须用 commit 后的净量（旧净量会重开 wrong-close 窗口）
            post_net_qty, _post_net_cost = self._batch_net_position(b2)
        # resize：撤旧挂净量（owner exception 窄放行）。失败 → 保持冻结，恢复续跑。
        # 🔥 v6.4-P1：in-flight 防护——monitor 运行期自愈与本事务线程互斥（防双 create）
        if not self._try_acquire_resize_inflight(batch_id):
            return False, 'resize_inflight（并发续跑防护）'
        try:
            ok_r, msg_r = self._resize_protection_after_partial(
                symbol, batch_id, close_op_id, post_net_qty)
        finally:
            self._release_resize_inflight(batch_id)
        if not ok_r:
            return False, msg_r
        return True, 'partial_committed'

    def _resize_protection_after_partial(self, symbol, batch_id, close_op_id, net_qty,
                                         adopt_matching_sl=False):
        """🔥 v6.4-P1：partial commit 后按净量重挂 SL/TP（owner exception 窄放行）。

        v6.4-P1 实盘事故收敛（2026-09-02 14:27 resize_cancel_unverified_SL 假阴性 +
        运行期无自愈 + TOCTOU），四轮 ChatGPT 收敛冻结设计：
        - durable 取价：SL=stop_steps[last_filled_count-1]（缺层回退末位）、
          TP=take_profit_price——绝不再依赖即将被撤销的旧交易所订单取价
          （OrderNotFound ≠ 未成交，恢复时旧单可能已查不到）。
        - 撤旧后有界确认（4×0.5s）：单次查询会被条件单通道传播延迟打假阴性；
          全败仍 Fail-Closed。OrderNotFound 只证明「不用再撤」，绝不单独视为未成交证明。
        - partial_resize_stage 0/1/2 分腿 durable：每腿 verify 后同锁提交新 id+stage；
          resume 按 stage 跳过已完成腿（防「SL 成功后 TP 失败 → 重试撞 CONFIRMED 门」冻结）。
        - 守恒门双置位：每腿「撤旧终态确认后、create 前」权威复检 _close_amount_guard
          且返回量 ≈ net_qty（1e-9）——撤单窗口内旧保护单可能触发成交使入口证明过期。
        - 收编路径：上一轮 verify 成功但 stage 提交前崩溃 → registry CONFIRMED 新 id ≠
          账本 id → 直接收编（不重发 create）。该 CONFIRMED 只能来自本事务
          （close_phase=1 期间其他创建者全被 G1/G2 owner 闸门拒绝）。
        - 🔥 P5（adopt_matching_sl=True）：limit_cancel_restore_pending 恢复时 SL 未被
          撤——live 且数量/目标价与当前净量匹配则收编（stage=1，不重建）；不匹配
          （PARTIAL 净量已变/丢失/已触发）才走 resize。TP 已撤 → 专用窄 re-arm
          （_rearm_tp_registry_for_closecancel 五条件）后按净量重建。
        失败 → 保持冻结（stage 已持久化进度），恢复/monitor 60s 续跑，绝不重发 MARKET。
        """
        latest = self.load_all_states()
        b = (latest.get(symbol, {}) or {}).get(batch_id)
        if not isinstance(b, dict):
            return False, 'batch_missing'
        side = (b.get('side') or 'BUY').upper()
        pos_side = 'LONG' if side == 'BUY' else 'SHORT'
        reduce_side = 'sell' if side == 'BUY' else 'buy'
        is_hedge = bool(b.get('is_hedge_mode', False))
        lfc = int(b.get('last_filled_count', 0) or 0)
        stage = int(b.get('partial_resize_stage', 0) or 0)
        is_p5_restore = b.get('close_reason') == 'limit_cancel_restore_pending'
        # 🔥 v6.4-P1 durable 目标价（用户改单已同步维护这两处：/sl → stop_steps、/tp → take_profit_price）
        stops = b.get('stop_steps') or []
        sl_idx = max(lfc - 1, 0)
        sl_price = (float(stops[sl_idx]) if sl_idx < len(stops) else float(stops[-1])) if stops else 0.0
        tp_price = float(b.get('take_profit_price') or 0)
        for kind, old_id in (('SL', b.get('current_sl_id')),
                             ('TP', b.get('tp_order_id'))):
            if kind == 'SL' and stage >= 1:
                continue  # 该腿已 durable 完成（stage 幂等，绝不重撤/重挂）
            if kind == 'TP' and stage >= 2:
                continue
            price = sl_price if kind == 'SL' else tp_price
            if price <= 0:
                # durable 目标价缺失 → 不猜测，保持冻结（恢复续跑），绝不以劣化参数重挂
                return False, f'resize_price_unknown_{kind}（durable 目标价缺失）'
            # 🔥 P5：PURE_CANCEL/PARTIAL 恢复时 TP 已被 close 启动撤掉（registry
            # PROGRAMMATIC_CANCELED/close_requested_canceled）→ 专用窄 re-arm 开启
            # 新一代订单生命周期（五条件，generic 终态守卫零改动）
            if kind == 'TP' and is_p5_restore:
                ok_rm, why_rm = self._rearm_tp_registry_for_closecancel(
                    symbol, batch_id, close_op_id)
                if not ok_rm:
                    return False, f'tp_rearm_failed（{why_rm}）'
            identity = self._protection_identity(batch_id, kind, max(lfc - 1, 0), pos_side)
            # 🔥 P5：SL 收编判定——live 且数量/目标价与当前净量匹配 → 不重建
            if adopt_matching_sl and kind == 'SL' and old_id:
                _ad, _adopt_id = self._try_adopt_live_sl(symbol, batch_id, old_id,
                                                         net_qty, price)
                if _ad == 'adopted':
                    with self._state_lock:
                        latest_a = self.load_all_states()
                        b_a = (latest_a.get(symbol, {}) or {}).get(batch_id)
                        if not isinstance(b_a, dict) \
                                or (b_a.get('close_op_id') or '') != close_op_id \
                                or b_a.get('close_reason') not in (
                                    'partial_resize_pending', 'limit_cancel_restore_pending'):
                            return False, 'state_changed'
                        b_a['current_sl_id'] = _adopt_id
                        b_a['partial_resize_stage'] = 1
                        if not self._persist_states(latest_a):
                            return False, 'persist_failed'
                    continue
            # 收编判定：registry CONFIRMED 新 id ≠ 账本 id = 上一轮 verify 后 stage 提交前崩溃
            reg_entry = (b.get('protection_registry') or {}).get(identity) or {}
            adopted = (reg_entry.get('state') == 'CONFIRMED'
                       and reg_entry.get('order_id')
                       and str(reg_entry.get('order_id')) != str(old_id))
            if adopted:
                new_id = str(reg_entry['order_id'])
            else:
                allowed, r = self._assert_create_allowed(
                    symbol, batch_id, identity, desc=f'partial resize {kind}',
                    replace_order_id=old_id, owner_op_id=close_op_id)
                if allowed:
                    allowed, r = self._final_pre_create_check(
                        symbol, batch_id, identity, desc=f'partial resize {kind}',
                        owner_op_id=close_op_id)
                if not allowed:
                    return False, f'resize_blocked_{kind}（{r}）'
            if old_id:
                # 🔥 v6.4-P0 rework（P0-3）：cancel 必须证明旧单已物理离场，未证明不得继续
                # （旧 oversized SL + 新净量 SL 同时在场 = 正在修的 wrong-close 本身）
                try:
                    self._safe_api_call(self.exchange.cancel_order, old_id, symbol,
                                        params={'stop': True})
                except Exception as e:
                    if '-2011' not in str(e) and 'Unknown order' not in str(e) \
                            and 'does not exist' not in str(e):
                        return False, f'resize_cancel_failed_{kind}（{e}）'
                    # -2011 = 旧单已不存在（对「撤单」目标幂等；「未成交」由下方守恒门兜底）
                else:
                    # 🔥 v6.4-P1：撤单确认传播延迟假阴性（2026-09-02 实盘 status=open）→ 有界重试
                    _st = ''
                    for _i in range(4):
                        try:
                            _chk = self._safe_api_call(self.exchange.fetch_order, old_id,
                                                       symbol, params={'stop': True})
                            _st = str((_chk or {}).get('status') or '').lower()
                            if _st in ('canceled', 'expired', 'rejected'):
                                break
                        except Exception as e:
                            if '-2011' in str(e) or 'Unknown order' in str(e):
                                _st = 'canceled'
                                break
                            return False, f'resize_cancel_verify_failed_{kind}（{e}）'
                        time.sleep(0.5)
                    if _st not in ('canceled', 'expired', 'rejected'):
                        return False, f'resize_cancel_unverified_{kind}（status={_st}）'
            # 🔥 v6.4-P1 TOCTOU 守恒门：每腿撤旧终态确认后、create/commit 前权威复检——
            # 撤单窗口内旧保护单可能触发成交使入口守恒证明过期（R3b）；收编路径同门兜底
            g_qty, g_detail = self._close_amount_guard(symbol, side, is_hedge, net_qty, batch_id)
            if g_qty is None or abs(float(g_qty) - net_qty) > 1e-9:
                self.send_tg_notification(
                    f"🚨【资金安全】partial resize 守恒门拦截（{kind}）批次 `{batch_id}`\n"
                    f"💡 {g_detail}\n"
                    f"⚠️ 拒绝按净量 {net_qty} 重挂（撤单窗口内仓位可能已变化）。\n"
                    f"批次保持冻结并停止自动续跑——请核对交易所实际仓位与挂单后人工处理。",
                    level='critical')
                return False, f'resize_guard_rejected_{kind}（{g_detail}）'
            if not adopted:
                otype = 'STOP_MARKET' if kind == 'SL' else 'TAKE_PROFIT_MARKET'
                # 🔥 v6.4-P0 rework（P0-3）：复用 crash-safe Create 链——
                # B2-2 intent 先落盘（PENDING_CREATE）→ create → verify → registry CONFIRMED；
                # 禁止「create 返回 ID 就直接 CONFIRMED」的孤儿窗口模式。
                self._update_registry(symbol, batch_id, identity, state='PENDING_CREATE',
                                      id_known=False, order_kind='conditional', role=kind,
                                      layer=max(int(b.get('last_filled_count', 0) or 0) - 1, 0),
                                      side=reduce_side,
                                      intent=self._build_intent(
                                          symbol=symbol, side=reduce_side, qty=net_qty,
                                          order_type=otype, stop_price=str(price),
                                          reduce_only=True))
                params = dict(b.get('params_base') or {})
                params['stopPrice'] = price
                try:
                    new_order = self._safe_api_call(self.exchange.create_order,
                                                    symbol, otype, reduce_side, net_qty, None,
                                                    params)
                except Exception as e:
                    # create 异常 ≠ 未发出（可能已在途）→ registry 保持 PENDING_CREATE
                    # Fail-Closed 等人工/裁决，绝不盲目重发（C5 纪律）
                    return False, f'resize_create_failed_{kind}（{e}）'
                new_id = str((new_order or {}).get('id') or '')
                if not new_id:
                    return False, f'resize_create_failed_{kind}'
                vres = self._verify_and_update_registry(
                    symbol, batch_id, identity, new_id,
                    desc=f'partial resize {kind}', owner_op_id=close_op_id)
                if vres != 'success':
                    # not_found→NOT_CONFIRMED / unknown→PENDING_VERIFY（registry 已记录真相）；
                    # 保持冻结（恢复/人工接管），绝不把未验证 ID 标 CONFIRMED
                    return False, f'resize_verify_failed_{kind}（{vres}）'
            # 🔥 v6.4-P1 分腿 durable commit：新 id + stage 同一次锁内持久化
            # 🔥 P5：reason 校验统一接受 limit_cancel_restore_pending（接线契约①）
            with self._state_lock:
                latest2 = self.load_all_states()
                b2 = (latest2.get(symbol, {}) or {}).get(batch_id)
                if not isinstance(b2, dict) \
                        or (b2.get('close_op_id') or '') != close_op_id \
                        or b2.get('close_reason') not in (
                            'partial_resize_pending', 'limit_cancel_restore_pending'):
                    return False, 'state_changed'
                if kind == 'SL':
                    b2['current_sl_id'] = new_id
                    b2['partial_resize_stage'] = 1
                else:
                    b2['tp_order_id'] = new_id
                    b2['partial_resize_stage'] = 2
                if not self._persist_states(latest2):
                    return False, 'persist_failed'
        # 最终 CAS → ACTIVE（两腿 id 已分腿 durable；此处只切事务态并清 stage）
        # 🔥 P5（接线契约③）：restore_pending 时原子清理 limit_close_* 三字段——
        # 绝不让 ACTIVE 批次携带一张已终态的「活跃限价平仓单」镜像
        with self._state_lock:
            latest = self.load_all_states()
            b3 = (latest.get(symbol, {}) or {}).get(batch_id)
            if not isinstance(b3, dict) or (b3.get('close_op_id') or '') != close_op_id \
                    or b3.get('close_reason') not in (
                        'partial_resize_pending', 'limit_cancel_restore_pending'):
                return False, 'state_changed'
            if int(b3.get('partial_resize_stage', 0) or 0) != 2:
                return False, 'state_changed（resize stage 未就绪）'
            b3['close_phase'] = 0
            b3['pending_close'] = False
            b3['is_programmatic_cancel'] = False
            b3['close_reason'] = ''
            b3.pop('partial_resize_stage', None)
            if b3.get('close_op_id') == close_op_id and \
                    b3.pop('limit_close_order_id', None) is not None:
                b3.pop('limit_close_price', None)
                b3.pop('limit_close_mode', None)
            if not self._persist_states(latest):
                return False, 'persist_failed'
        return True, 'partial_active'

    def _resume_partial_resize(self, symbol, batch_id, owner_op_id):
        """🔥 v6.4-P1：分型续跑（重启恢复 + monitor 运行期自愈共用入口）。

        partial_resize_pending → 只续 resize（按持久化净 qty + stage 进度），
        绝不重发 MARKET；partial_closing → loud critical + 人工核实（副作用未知）。
        🔥 v6.4-P1：入口快速守恒门——actual 必须覆盖 Σnet 且 guard 返回量 ≈ 本批净量
        （每腿 create 前还有权威门，见 _resize_protection_after_partial）。"""
        all_states = self.load_all_states()
        b = (all_states.get(symbol, {}) or {}).get(batch_id)
        if not isinstance(b, dict):
            return False, 'batch_missing'
        reason = b.get('close_reason')
        if reason == 'partial_closing':
            self.send_tg_notification(
                f"🚨【资金安全】批次 `{batch_id}` 停留在 partial_closing（部分平仓副作用未知），"
                f"已拒绝自动续跑——请核对交易所实际仓位与挂单后人工处理。",
                level='critical')
            return False, 'partial_closing_requires_manual_review'
        if reason != 'partial_resize_pending':
            if reason == 'limit_cancel_restore_pending':
                # 🔥 P5：closecancel 归属已 durable 的确定性恢复态 → 续保护单恢复
                return self._resume_closecancel_restore(symbol, batch_id, owner_op_id)
            return False, f'nothing_to_resume（{reason}）'
        if (b.get('close_op_id') or '') != (owner_op_id or ''):
            return False, 'op_id_mismatch'
        net_qty, _c = self._batch_net_position(b)
        side = (b.get('side') or 'BUY').upper()
        is_hedge = bool(b.get('is_hedge_mode', False))
        g_qty, g_detail = self._close_amount_guard(symbol, side, is_hedge, net_qty, batch_id)
        if g_qty is None or abs(float(g_qty) - net_qty) > 1e-9:
            self.send_tg_notification(
                f"🚨【资金安全】partial resume 守恒门拦截 批次 `{batch_id}`\n"
                f"💡 {g_detail}\n"
                f"⚠️ 账本净量 {net_qty} 与交易所实际已漂移，拒绝自动续跑——请人工核对后处理。",
                level='critical')
            return False, f'resume_guard_rejected（{g_detail}）'
        return self._resize_protection_after_partial(symbol, batch_id, owner_op_id, net_qty)

    # ==================== P5：/closecancel（限价平仓撤销与恢复） ====================

    def _try_adopt_live_sl(self, symbol, batch_id, sl_id, net_qty, target_price):
        """🔥 P5：SL 收编判定——旧 SL 未被 close 撤（限价路径全程不撤 SL），
        live 且数量≈当前净量、目标价≈durable stop_steps → 收编（不重建）。
        返回 ('adopted', sl_id) / ('resize', None)。查询异常按 resize 处理
        （cancel 对已消失订单 -2011 幂等，守恒门兜底）。"""
        try:
            o = self._safe_api_call(self.exchange.fetch_order, sl_id, symbol,
                                    params={'stop': True})
            if not isinstance(o, dict):
                return 'resize', None
            status = str(o.get('status') or '').lower()
            filled = float(o.get('filled') or 0.0)
            amount = float(o.get('amount') or 0.0)
            stop = float(o.get('stopPrice') or 0.0)
            if status in ('open', 'new', 'active', 'partially_filled') and filled <= 1e-12 \
                    and abs(amount - net_qty) <= max(1e-8, net_qty * 1e-6) \
                    and abs(stop - target_price) <= 1e-6:
                return 'adopted', sl_id
        except Exception:
            pass
        return 'resize', None

    def _rearm_tp_registry_for_closecancel(self, symbol, batch_id, close_op_id):
        """🔥 P5：TP registry 专用窄 re-arm 事务（ChatGPT v2 §5.1 裁定）。

        五条件全满足才允许该 TP slot 开启新一代订单生命周期：
          close_reason==limit_cancel_restore_pending / close_phase==1 /
          close_op_id==owner / 旧 TP registry state==PROGRAMMATIC_CANCELED /
          terminated_reason==close_requested_canceled
        旧条目快照入 rearm_audit 后移除（新代由 fresh create 建立全新 intent，
        按当前 durable net_qty）；generic _update_registry 终态守卫零改动；
        普通 PROGRAMMATIC_CANCELED 不获得任何复活能力。
        🔥 复审 Blocker 3（P0）：crash 幂等——re-arm pop 后、create 前崩溃重启，
        registry 条目已缺 + rearm_audit 已有本 op 记录 → 幂等放行（返回
        already_rearmed），绝不 tp_rearm_failed 冻结。"""
        with self._state_lock:
            try:
                latest = self.load_all_states()
            except Exception as e:
                return False, f'state_unreadable（{e}）'
            b = (latest.get(symbol, {}) or {}).get(batch_id)
            if not isinstance(b, dict):
                return False, 'batch_missing'
            if b.get('close_reason') != 'limit_cancel_restore_pending' \
                    or int(b.get('close_phase', 0) or 0) != 1 \
                    or (b.get('close_op_id') or '') != (close_op_id or ''):
                return False, 'not_in_restore'
            side = (b.get('side') or 'BUY').upper()
            pos_side = 'LONG' if side == 'BUY' else 'SHORT'
            lfc = int(b.get('last_filled_count', 0) or 0)
            identity = self._protection_identity(batch_id, 'TP', max(lfc - 1, 0), pos_side)
            # 🔥 P5c（ChatGPT 二复审 Blocker 1，P0）：crash 幂等前置——本 op 已
            # re-arm（审计在册）→ 无论条目缺失（pop 后崩溃）还是新代已进入
            # PENDING_CREATE/CONFIRMED（create 后、stage commit 前崩溃），一律幂等
            # 放行续跑；新代 CONFIRMED 由恢复链的收编路径接管（绝不重发 create）
            for _aud in b.get('rearm_audit') or []:
                if _aud.get('op') == (close_op_id or '') \
                        and _aud.get('identity') == identity:
                    return True, 'already_rearmed'
            entry = (b.get('protection_registry') or {}).get(identity) or {}
            if entry.get('state') == 'PROGRAMMATIC_CANCELED' \
                    and entry.get('terminated_reason') == 'close_requested_canceled':
                b.setdefault('rearm_audit', []).append({
                    'time': time.time(), 'identity': identity,
                    'prev_state': entry.get('state'),
                    'prev_order_id': entry.get('order_id'),
                    'prev_terminated_reason': entry.get('terminated_reason'),
                    'op': close_op_id})
                b['protection_registry'].pop(identity, None)
                if not self._persist_states(latest):
                    return False, 'persist_failed'
                return True, 'rearmed'
            return False, f'rearm_conditions_not_met（state={entry.get("state")}）'

    def _commit_closecancel_attribution(self, symbol, batch_id, close_op_id,
                                        confirmed_filled):
        """🔥 P5：PARTIAL_FILL_CANCEL 成交量归属——原子 CAS（防双计用状态单向迁移）。

        只有 close_reason==limit_pending_normal 才能提交；首提改变 close_reason →
        并发线程/崩溃重试的 CAS 立即失败（替代 accounted 字段，ChatGPT v3 裁定）。
        成本口径与 /partial L3219 完全一致：净成本比例分摊。"""
        with self._state_lock:
            try:
                latest = self.load_all_states()
            except Exception as e:
                return False, f'state_unreadable（{e}）'
            b = (latest.get(symbol, {}) or {}).get(batch_id)
            if not isinstance(b, dict):
                return False, 'batch_missing'
            if (b.get('close_op_id') or '') != (close_op_id or '') \
                    or int(b.get('close_phase', 0) or 0) != 1 \
                    or b.get('close_reason') != 'limit_pending_normal':
                return False, 'state_changed（已归属/已迁移，绝不双计）'
            pre_net_qty, pre_net_cost = self._batch_net_position(b)
            if confirmed_filled > pre_net_qty + 1e-12:
                return False, f'confirm_exceeds_net（{confirmed_filled} > {pre_net_qty}）'
            reduce_cost_delta = (confirmed_filled * pre_net_cost / pre_net_qty
                                 if pre_net_qty > 0 else 0.0)
            b['realized_reduce_amount'] = float(
                b.get('realized_reduce_amount', 0.0) or 0.0) + confirmed_filled
            b['realized_reduce_cost'] = float(
                b.get('realized_reduce_cost', 0.0) or 0.0) + reduce_cost_delta
            b['close_reason'] = 'limit_cancel_restore_pending'
            if not self._persist_states(latest):
                return False, 'persist_failed'
            return True, 'attributed'

    def _resume_closecancel_restore(self, symbol, batch_id, owner_op_id):
        """🔥 P5：limit_cancel_restore_pending 确定态恢复——只续保护单恢复，
        绝不再发 close order（与 partial_resume 分型对称：副作用已知→自动续跑）。"""
        all_states = self.load_all_states()
        b = (all_states.get(symbol, {}) or {}).get(batch_id)
        if not isinstance(b, dict):
            return False, 'batch_missing'
        if b.get('close_reason') != 'limit_cancel_restore_pending':
            return False, f'nothing_to_resume（{b.get("close_reason")}）'
        if (b.get('close_op_id') or '') != (owner_op_id or ''):
            return False, 'op_id_mismatch'
        net_qty, _c = self._batch_net_position(b)
        side = (b.get('side') or 'BUY').upper()
        is_hedge = bool(b.get('is_hedge_mode', False))
        g_qty, g_detail = self._close_amount_guard(symbol, side, is_hedge, net_qty, batch_id)
        if g_qty is None or abs(float(g_qty) - net_qty) > 1e-9:
            self.send_tg_notification(
                f"🚨【资金安全】closecancel 恢复守恒门拦截 批次 `{batch_id}`\n"
                f"💡 {g_detail}\n"
                f"⚠️ 账本净量 {net_qty} 与交易所实际已漂移，拒绝恢复——请人工核对后处理。",
                level='critical')
            return False, f'resume_guard_rejected（{g_detail}）'
        return self._resize_protection_after_partial(symbol, batch_id, owner_op_id,
                                                     net_qty, adopt_matching_sl=True)

    def _route_zero_position_limit_close(self, symbol, batch_id, position_zero=None):
        """🔥 P5g/P5h：限价事务在途 + **仓位已归零** 的统一分型路由
        ——主循环归零分支与 finally 清理边界共用同一实现。

        position_zero：调用方已确认的归零证据（0.0/False 均可）；None = 未确认
        → 本函数自行做**方向感知**查询（Hedge Mode LONG/SHORT 双记录）。
        🔥 P5h（ChatGPT 七复审 P0-1）：仓位非归零或 UNKNOWN 一律拒绝路由——
        否则 monitor 在非零持仓下异常退出，finally 会撤掉正常在途的限价单并把
        批次写成永久冻结（真实后果：交易中断 + 错误冻结）。

        归零确认后：撤在途限价单（-2011 幂等，防价格回落对零仓位开反向仓）
          → 权威 fetch_order → 按成交量覆盖度分型：
            FULL_FILL（含 canceled+filled=全量 退化形态）→ finalizer 正确结算；
            零成交/部分成交 → 原子写 manual_review 冻结（SL 成交归属不可知）；
        返回 (ok, msg)：ok=True 表示已路由终结（结算或冻结），调用方一律
        **不得**再走普通 converge+clear；分类不可证明 → ok=False 且调用方
        同样必须保持 Fail-Closed 不清理。"""
        try:
            b = (self.load_all_states().get(symbol, {}) or {}).get(batch_id) or {}
            oid = str(b.get('limit_close_order_id') or '')
            if not oid:
                return False, 'no_limit_order'
            if position_zero is None:
                position_zero = self._get_current_position_amt(
                    symbol, bool(b.get('is_hedge_mode', False)),
                    b.get('side') or 'BUY')
            if position_zero is None:
                return False, 'position_unknown（归零证据不可得，拒绝归零分型）'
            if float(position_zero) > 1e-9:
                return False, f'position_not_zero（仓位 {position_zero}，拒绝归零分型）'
            try:
                self._safe_api_call(self.exchange.cancel_order, oid, symbol)
            except Exception as _ce:
                if '-2011' not in str(_ce) and 'Unknown order' not in str(_ce):
                    return False, f'cancel_failed（{str(_ce)[:120]}）'
            try:
                ord2 = self._safe_api_call(self.exchange.fetch_order, oid, symbol)
            except Exception:
                ord2 = None
            filled = float((ord2 or {}).get('filled') or 0.0)
            pre_net, _c = self._batch_net_position(b)
            if filled >= pre_net - max(1e-8, pre_net * 1e-6):
                return self._finalize_limit_full_fill(symbol, batch_id, oid, order=ord2)
            return self._mark_limit_cancel_manual_review(
                symbol, batch_id, close_op_id=b.get('close_op_id'), order_id=oid)
        except Exception as e:
            return False, f'route_failed（{str(e)[:120]}）'

    def _finally_cleanup_decision(self, symbol, batch_id):
        """🔥 P5f（ChatGPT 五复审 P0-2）：finally 清理授权（fail-closed + 状态绑定）。

        返回 (decision, snapshot)：
          'finalizer' settled（有订单 ID）→ 由 finalizer 独占清理生命周期；
          'skip'     manual_review 冻结 / 批次已不存在 / **读取异常**——
                     读失败绝不沿用旧 fail-open 默认（否则异常退出即旁路 PnL 门）；
          'allow'    普通批次 → 允许旧的两段清理（执行前须二次校验 snapshot）。
        snapshot=(close_op_id, close_reason, settled, limit_close_order_id)。"""
        try:
            with self._state_lock:
                b = (self.load_all_states().get(symbol, {}) or {}).get(batch_id) or {}
                if not isinstance(b, dict) or not b.get('is_active'):
                    return 'skip', None
                snap = (b.get('close_op_id') or '', b.get('close_reason') or '',
                        bool(b.get('settled_by_limit_close')),
                        b.get('limit_close_order_id') or '')
                if snap[2] and snap[3]:
                    return 'finalizer', snap
                if snap[1] == 'limit_cancel_manual_review':
                    return 'skip', snap
                # 🔥 P5g（ChatGPT 六复审 P0-1）：限价事务在途（reason=limit_pending_normal /
                # restore_pending）也绝不放行普通清理——monitor 可能在写入 manual_review
                # 之前异常退出（crash-before-marker），此时 pending_close=True，
                # 旧清理会 converge+clear 丢失 ZERO/PARTIAL 归属。统一交分型路由。
                if snap[1] in ('limit_pending_normal', 'limit_cancel_restore_pending') \
                        and snap[3]:
                    return 'classify', snap
                return 'allow', snap
        except Exception as e:
            print(f"  └─ ⚠️ [P5] finally 清理授权读取失败（fail-closed，不清理）: {e}")
            return 'skip', None

    def _cleanup_authorization_still_valid(self, symbol, batch_id, snapshot):
        """清理执行前二次校验：授权快照必须与**当前**账本一致（TOCTOU 收口）。

        授权后、clear 前若另一线程推进了 close 事务（settled/phase=2/reason
        迁移），旧授权立即失效——清理不得凭陈旧判断执行（R23）。"""
        if not snapshot:
            return False
        try:
            with self._state_lock:
                b = (self.load_all_states().get(symbol, {}) or {}).get(batch_id) or {}
                if not isinstance(b, dict) or not b.get('is_active'):
                    return False
                cur = (b.get('close_op_id') or '', b.get('close_reason') or '',
                       bool(b.get('settled_by_limit_close')),
                       b.get('limit_close_order_id') or '')
                return cur == tuple(snapshot)
        except Exception:
            return False

    def _mark_limit_cancel_manual_review(self, symbol, batch_id,
                                         close_op_id=None, order_id=None):
        """🔥 P5e/P5f：SL 归零 × 限价撤单（零/部分成交）→ 原子落盘人工核对冻结态。

        专用 close_reason=limit_cancel_manual_review（不再伪装成普通
        limit_pending_normal）——主循环、finally 清理、启动 stale/monitor_error
        清理、恢复分发统一识别：不恢复、不清理、只告警。
        🔥 P5f（ChatGPT 五复审 P0-3）CAS 收紧：
          - 代际隔离：close_op_id / limit_close_order_id 必须与调用方预期一致
            （旧 L1/OP1 线程绝不能把新 L2/OP2 标成冻结）；
          - 拒绝 settled（settled+phase=2 优先级最高 → finalizer 结算）；
          - 仅 close_phase==1 可迁移（防与恢复链/新事务并发覆盖）。"""
        with self._state_lock:
            try:
                latest = self.load_all_states()
            except Exception as e:
                return False, f'state_unreadable（{e}）'
            b = (latest.get(symbol, {}) or {}).get(batch_id)
            if not isinstance(b, dict) or not b.get('is_active'):
                return False, 'batch_missing'
            if close_op_id is not None \
                    and str(b.get('close_op_id') or '') != str(close_op_id):
                return False, ('generation_mismatch（op %s ≠ 当前事务 %s）'
                               % (close_op_id, b.get('close_op_id')))
            if order_id is not None \
                    and str(b.get('limit_close_order_id') or '') != str(order_id):
                return False, ('generation_mismatch（order %s ≠ 当前事务 %s）'
                               % (order_id, b.get('limit_close_order_id')))
            if b.get('settled_by_limit_close'):
                return False, 'settled_already（结算优先，不得改冻结）'
            if int(b.get('close_phase', 0) or 0) != 1:
                return False, f'phase_mismatch（close_phase={b.get("close_phase")}）'
            if b.get('close_reason') not in ('limit_pending_normal',
                                             'limit_cancel_restore_pending'):
                return False, f'not_markable（reason={b.get("close_reason")}）'
            b['close_reason'] = 'limit_cancel_manual_review'
            if not self._persist_states(latest):
                return False, 'persist_failed'
        if time.time() - self._freeze_alerted.get(batch_id, 0) >= 3600:
            self._freeze_alerted[batch_id] = time.time()
            self.send_tg_notification(
                f"🚨【资金安全】批次 `{batch_id}` 限价平仓单已撤销但仓位已被止损归零"
                f"——余量平仓归属未明确，已转入人工核对冻结。\n"
                f"💡 请核对交易所成交记录后人工处理（本批次不会被自动清理/恢复，"
                f"重启后保持冻结）。",
                level='critical')
        return True, 'manual_review'

    def _adjudicate_closed_limit_close(self, symbol, batch_id, order_id):
        """🔥 P5：终态限价平仓单的四态裁决（命令与 monitor canceled 分支共用）。

        按成交量分型（v3.1 冻结）：TERMINAL_ZERO→PURE_CANCEL 恢复原净仓；
        PARTIAL→durable 归属已成交部分→恢复剩余净量；CONFIRMED_FULL→共享幂等
        finalizer（绝不恢复 ACTIVE）；PENDING/UNKNOWN/NOT_CONFIRMED→Fail-Closed。"""
        all_states = self.load_all_states()
        b = (all_states.get(symbol, {}) or {}).get(batch_id)
        if not isinstance(b, dict) or not b.get('is_active'):
            return False, 'batch_missing'
        close_op_id = b.get('close_op_id') or ''
        if not close_op_id:
            return False, 'no_close_inflight'
        # 🔥 P5 复审 Blocker 1（P0）：代际隔离——旧事务的监控/命令不得裁决当前
        # 新一代事务的订单（旧 L1/OP1 绝不能把新 L2/OP2 标 settled 并触发 clear）
        if str(b.get('limit_close_order_id') or '') != str(order_id):
            return False, ('order_generation_mismatch（裁决订单 %s ≠ 当前事务订单 %s，'
                           '旧事务线程已失效）' % (order_id, b.get('limit_close_order_id')))
        if b.get('close_reason') not in ('limit_pending_normal',
                                         'limit_cancel_restore_pending'):
            return False, f'not_cancellable（reason={b.get("close_reason")}）'
        if b.get('settled_by_limit_close'):
            # 🔥 P5 复审 Blocker 2（P0）：phase=2 接管契约——已认领结算的批次
            # 必须由任何看到 settled 的调用方续跑幂等 finalizer（PnL 去重 +
            # converge+clear），绝不直接退回（否则 CAS 后崩溃无人续跑）
            return self._finalize_limit_full_fill(symbol, batch_id, order_id)
        pre_net_qty, _c = self._batch_net_position(b)
        verdict, detail, filled = self._confirm_close_filled(
            symbol, b.get('side') or 'BUY', bool(b.get('is_hedge_mode', False)),
            order_id, expected=pre_net_qty, attempts=3, delay=0.4,
            order_kind='normal')
        if verdict == 'TERMINAL_ZERO':
            ok_a, msg_a = self._commit_closecancel_attribution(
                symbol, batch_id, close_op_id, 0.0)
            if not ok_a:
                return False, msg_a
            return self._resume_closecancel_restore(symbol, batch_id, close_op_id)
        if verdict == 'PARTIAL':
            if not filled or filled <= 0:
                return False, f'partial_fill_unknown（{detail}）'
            # 🔥 覆盖度兜底：已全量成交后再撤（status 退化为 canceled + filled==全量）
            # → 本质是 FULL_FILL，绝不按「部分成交」恢复（恢复会让净量归零并撞守恒门）
            if filled >= pre_net_qty - max(1e-8, pre_net_qty * 1e-6):
                return self._finalize_limit_full_fill(symbol, batch_id, order_id)
            ok_a, msg_a = self._commit_closecancel_attribution(
                symbol, batch_id, close_op_id, float(filled))
            if not ok_a:
                return False, msg_a
            return self._resume_closecancel_restore(symbol, batch_id, close_op_id)
        if verdict == 'CONFIRMED_FULL':
            return self._finalize_limit_full_fill(symbol, batch_id, order_id)
        # PENDING / UNKNOWN / NOT_CONFIRMED → Fail-Closed
        self.send_tg_notification(
            f"🚨【资金安全】限价平仓单终态无法证明 批次 `{batch_id}`\n"
            f"💡 {verdict}: {detail[:200]}\n"
            f"⚠️ 保持冻结（单飞占用中）。请核对交易所限价单实际状态后重试 /closecancel 或人工处理。",
            level='critical')
        return False, f'closecancel_{verdict}（{detail[:120]}）'

    def _submit_closecancel(self, symbol, batch_id):
        """🔥 P5：/closecancel 命令入口——撤销在途限价平仓并恢复批次控制权。

        资格 → cancel → 四态裁决（TERMINAL_ZERO/PARTIAL→恢复；CONFIRMED_FULL→
        共享 finalizer；其余→Fail-Closed）。inflight 与恢复调度互斥。"""
        all_states = self.load_all_states()
        b = (all_states.get(symbol, {}) or {}).get(batch_id)
        if not isinstance(b, dict) or not b.get('is_active'):
            return False, f'batch_missing（{batch_id}）'
        if not b.get('close_op_id'):
            return False, 'no_close_inflight（该批次没有在途平仓事务）'
        if b.get('settled_by_limit_close'):
            # 🔥 P5c（ChatGPT 二复审 Blocker 3）：settled + close_phase=2 = finalizer
            # 已认领但未完成（PnL 门/converge 断点）→ 命令接管共享 finalizer 续跑
            # （CAS 只确认事实不授独占权——任何调用方都可幂等续跑，PnL dedup 防双记）
            if int(b.get('close_phase', 0) or 0) == 2 and b.get('limit_close_order_id'):
                if not self._try_acquire_resize_inflight(batch_id):
                    return False, 'closecancel_inflight（已有撤销/恢复在途）'
                try:
                    return self._finalize_limit_full_fill(
                        symbol, batch_id, b['limit_close_order_id'])
                finally:
                    self._release_resize_inflight(batch_id)
            return False, 'already_settled（限价平仓已成交结算，不可撤销）'
        if int(b.get('close_phase', 0) or 0) == 0 and not b.get('pending_close'):
            return False, 'no_close_inflight（批次处于 ACTIVE，无在途平仓事务）'
        if b.get('close_reason') != 'limit_pending_normal':
            return False, (f'not_cancellable（reason={b.get("close_reason")}——'
                           f'仅 limit_pending_normal 可撤销）')
        order_id = b.get('limit_close_order_id')
        if not order_id:
            return False, 'no_limit_order（限价平仓单 ID 缺失，请人工核对）'
        if not self._try_acquire_resize_inflight(batch_id):
            return False, 'closecancel_inflight（已有撤销/恢复在途）'
        try:
            try:
                self._safe_api_call(self.exchange.cancel_order, order_id, symbol)
            except Exception as e:
                if '-2011' not in str(e) and 'Unknown order' not in str(e) \
                        and 'does not exist' not in str(e):
                    return False, f'cancel_failed（{e}）'
                # -2011：订单已离场 → 进入裁决（ filled 事实由 fetch_order 权威判定）
            return self._adjudicate_closed_limit_close(symbol, batch_id, order_id)
        finally:
            self._release_resize_inflight(batch_id)

    def _finalize_limit_full_fill(self, symbol, batch_id, order_id, order=None):
        """🔥 P5：FULL_FILL 共享幂等 finalizer（/closecancel 与 _monitor_limit_close
        共用；ChatGPT v3 裁定）。CAS 认领只确认事实、不授独占权——任何看到
        settled_by_limit_close=True 的调用方都必须继续执行幂等收敛；
        PnL 以 (symbol, order_id) 去重（崩溃/接管/重试只记一次）。
        🔥 复审 4 Blocker 收口：
          B1(P0) CAS 校验 close_op_id + limit_close_order_id==order_id（代际隔离）；
          B2(P0) settled=True 由裁决器路由至此 → 接管续跑；
          B4(P0) PnL 落盘失败 → 保持 close_phase=2 等待续跑，绝不 clear；
          fetch_order 失败无成交价 → 保持 phase=2 重试，绝不 exit_price=0 结算；
          converge None / clear 失败 → 如实返回（不谎报成功）。"""
        # ① 成交价：调用方已持有 order（monitor）直接复用；否则 fetch，失败保持
        # phase=2 等待续跑（exit_price=0 结算会把 PnL 算成 -100%，属数据污染）
        claimed = False
        avg_price = 0.0
        if order is None:
            try:
                order = self._safe_api_call(self.exchange.fetch_order, order_id, symbol)
            except Exception as e:
                print(f"⚠️ [finalizer] fetch_order 失败，保持 phase=2 待重试: {e}")
                return False, f'order_unavailable（{str(e)[:120]}）'
        if not isinstance(order, dict):
            return False, 'order_structure_unknown（保持 phase=2）'
        info = order.get('info', {}) or {}
        avg_price = float(order.get('average') or order.get('price') or 0.0)
        if avg_price == 0.0:
            cum_quote = float(info.get('cumQuote', 0.0))
            executed_qty = float(info.get('executedQty', 0.0))
            if cum_quote > 0 and executed_qty > 0:
                avg_price = cum_quote / executed_qty
        if avg_price <= 0.0:
            return False, 'fill_price_unavailable（保持 phase=2）'
        with self._state_lock:
            latest = self.load_all_states()
            b = (latest.get(symbol, {}) or {}).get(batch_id)
            if not isinstance(b, dict) or not b.get('is_active'):
                return True, 'already_cleared'
            # 🔥 Blocker 1（P0）：代际隔离——只允许当前事务（close_op 在册且
            # durable limit_close_order_id == 本订单）触发结算；旧线程的 L1
            # 绝不能结算新一代 L2/OP2
            if not b.get('close_op_id') \
                    or str(b.get('limit_close_order_id') or '') != str(order_id):
                return False, ('order_generation_mismatch（结算订单 %s ≠ 当前事务'
                               '订单 %s）' % (order_id, b.get('limit_close_order_id')))
            if not b.get('settled_by_limit_close'):
                b['settled_by_limit_close'] = True
                b['is_programmatic_cancel'] = True
                b['close_phase'] = 2
                if not self._persist_states(latest):
                    return False, 'persist_failed'
                claimed = True
        # ② 结算数据（从 durable ledger 取，与 monitor 快照口径一致）
        b = (self.load_all_states().get(symbol, {}) or {}).get(batch_id) or {}
        side = (b.get('side') or 'BUY').upper()
        net_qty, net_cost = self._batch_net_position(b)
        gross = float(sum((b.get('target_amounts') or [])[:int(b.get('last_filled_count', 0) or 0)]))
        total_entry_fee = float(b.get('total_entry_fee', 0.0) or 0.0)
        fee_rem = total_entry_fee * (net_qty / gross) if gross > 0 else 0.0
        total_cost_with_fee = net_cost + fee_rem
        avg_entry = total_cost_with_fee / net_qty if net_qty > 0 else 0.0
        exit_price = avg_price
        if side == 'BUY':
            gross_pnl = (exit_price - avg_entry) * net_qty
        else:
            gross_pnl = (avg_entry - exit_price) * net_qty
        exit_fee = exit_price * net_qty * MAKER_FEE_RATE
        total_fees = fee_rem + exit_fee
        net_pnl = gross_pnl - total_fees
        # ③ PnL 幂等记录（dedup 键 = symbol:order_id）
        # 🔥 Blocker 4（P0）：PnL 必须 durable 成功才允许继续收敛/清理——落盘失败
        # 保持 close_phase=2（claim 已完成），由恢复/接管续跑重试（dedup 防双记）
        if not self._record_realized_pnl(batch_id, symbol, side, net_qty, avg_entry,
                                         exit_price, net_pnl, '限价平仓',
                                         dedup_key=f'{symbol}:{order_id}'):
            return False, 'pnl_persist_failed（保持 close_phase=2 待续跑重试）'
        # ④ 撤 SL（N14）+ 残余 TP（B0）——同 monitor 结算惯例
        if b.get('current_sl_id'):
            _sl_id = b['current_sl_id']
            try:
                self._safe_api_call(self.exchange.cancel_order, _sl_id, symbol,
                                    params={'stop': True})
                _sl_identity = self._find_registry_identity_by_order_id(symbol, batch_id, _sl_id)
                if _sl_identity:
                    self._update_registry(symbol, batch_id, _sl_identity,
                                          state='PROGRAMMATIC_CANCELED',
                                          order_id=_sl_id, id_known=True,
                                          terminated_reason='close_settled_canceled')
            except Exception:
                pass
        _tp_id = b.get('tp_order_id')
        if _tp_id:
            try:
                self._safe_api_call(self.exchange.cancel_order, _tp_id, symbol,
                                    params={'stop': True})
            except Exception:
                pass  # -2011 幂等 / 其余交由 converge 收敛
            try:
                _tp_identity = self._find_registry_identity_by_order_id(symbol, batch_id, _tp_id)
                if _tp_identity:
                    self._update_registry(symbol, batch_id, _tp_identity,
                                          state='PROGRAMMATIC_CANCELED',
                                          order_id=_tp_id, id_known=True,
                                          terminated_reason='close_settled_canceled_tp')
            except Exception:
                pass
        # ⑤ 结算 TG（一次）+ 记录
        if claimed and self._claim_settlement_reported(symbol, batch_id):
            capital_base = avg_entry * net_qty if net_qty > 0 else 1
            net_pnl_pct = (net_pnl / capital_base) * 100 if capital_base > 0 else 0.0
            self.send_tg_notification(
                f"🎉 **[限价平仓结算]**\n\n"
                f"🆔 **批次号**：`{batch_id}`\n"
                f"🪙 **标的**：`{symbol}`\n"
                f"📊 **方向**：`{side}`\n"
                f"📊 **平仓模式**：限价单 (Maker {MAKER_FEE_RATE * 100:.2f}%)\n"
                f"📊 **持仓**：`{net_qty}`\n"
                f"📈 **持仓均价**：`{avg_entry:.2f}` USDT\n"
                f"💵 **平仓均价**：`{exit_price:.2f}` USDT\n"
                f"📊 **名义盈亏**：`{gross_pnl:+.2f}` USDT\n"
                f"💸 **总手续费**：`{total_fees:.4f}` USDT\n"
                f"{'🟢' if net_pnl >= 0 else '🔴'} **最终净盈亏**：`{net_pnl:+.2f}` USDT (`{net_pnl_pct:+.2f}%`)"
            )
            self._notify_snapshot(batch_id)
        # ⑥ converge + clear（幂等；如实返回：proof 未过 / clear 失败不得谎报成功）
        _proof = self._converge_batch_orders_before_clear(symbol, batch_id)
        if _proof is None:
            return False, 'converge_pending（保持 close_phase=2，重启/接管续跑）'
        if not self.clear_batch_state(symbol, batch_id, proof=_proof):
            return False, 'clear_failed（保持 close_phase=2，重启/接管续跑）'
        return True, ('finalized' if claimed else 'finalized_takeover')

    def _try_acquire_resize_inflight(self, batch_id):
        """🔥 v6.4-P1：resize 在途互斥（monitor 自愈线程 vs /partial 事务线程防双 create）。"""
        with self._resize_inflight_lock:
            if batch_id in self._resize_inflight:
                return False
            self._resize_inflight.add(batch_id)
            return True

    def _release_resize_inflight(self, batch_id):
        with self._resize_inflight_lock:
            self._resize_inflight.discard(batch_id)

    def _monitor_lifecycle_check(self, latest_all, latest_b_data):
        """🔥 v6.4-P3：monitor 生命周期守卫（每轮/每分支重证生存资格，ChatGPT 冻结规格）。

        三态返回：
          'ok'      账本可信 + batch 仍 active → 继续本轮工作
          'exit'    账本可信 + batch 缺失/非 active → 线程必须立即退出
                    （磁盘生命周期是唯一权威：clear_batch_state 迁移到 CLOSED 后，
                    任何旧内存线程立即失去产生交易副作用与终端报告的资格）
          'unknown' 账本损坏（D-009：load_all_states 返回 {} 且 _state_corrupted=True）
                    → 本轮跳过全部副作用。UNKNOWN ≠ EMPTY——损坏的 {} 绝不解释为
                    「batch 已被清理」，防止为修僵尸线程重新引入 UNKNOWN→误清 的旧错误。
        调用方约定：必须在任何交易所 API / 结算 / 补挂 / converge 之前调用。"""
        if getattr(self, '_state_corrupted', False):
            return 'unknown'
        if not isinstance(latest_b_data, dict) or not latest_b_data.get('is_active'):
            return 'exit'
        return 'ok'

    def _claim_settlement_reported(self, symbol, batch_id):
        """🔥 v6.4-P3（Fix④）：terminal 结算报告原子认领（_state_lock 内 CAS）。

        唯一 owner 才允许打印/发送 [平仓结算] 报告（at-most-once）：
          锁内重读 → batch 仍 active + settlement_reported 非 True
          → 写 True + durable persist → 当前线程取得 ownership。
        persist 失败 / 已被认领 / 批次已消失 → False（不发送）。
        权衡：persist-先于-发送，极端崩溃窗口允许少发一次通知（墓碑仍在），
        绝不允许重复发送几十次假结算（2026-09-02 18:45 实盘事故）。"""
        with self._state_lock:
            latest_all = self.load_all_states()
            b = (latest_all.get(symbol, {}) or {}).get(batch_id)
            if not isinstance(b, dict) or not b.get('is_active'):
                return False
            if b.get('settlement_reported'):
                return False
            b['settlement_reported'] = True
            if not self._persist_states(latest_all):
                return False
            return True

    def _maybe_runtime_resume_partial(self, symbol, batch_id, close_op_id):
        """🔥 v6.4-P1：monitor 冻结分支的 partial 运行期自愈调度（60s 节流）。

        - R8：首次看到某 op 的 partial_resize_pending 只登记时间不执行——给 /partial
          事务线程完整 60s 先自行完成 resize（否则首见立即抢跑 + inflight 撞车 →
          用户对已成功的减仓收到假失败，可能重发 /partial 造成二次真实减仓）。
          状态按 close_op_id 绑定：同批次新事务自动重新登记，杜绝陈旧 ts 立即放行。
        - R7：守恒门拒绝（resume_guard_rejected* / resize_guard_rejected_*）=
          terminal safety conflict——callee 已 critical 一次，置 halted 停止本进程
          自动续跑等人工；其余暂态失败静默下轮重试，连续 3 轮一次 critical。"""
        _prs = dict(self._partial_resume_state.get(batch_id) or {})
        if _prs.get('op') != (close_op_id or ''):
            self._partial_resume_state[batch_id] = {'ts': time.time(), 'fails': 0,
                                                    'op': close_op_id or ''}
            return
        if _prs.get('halted') or time.time() - _prs['ts'] < 60:
            return
        _prs['ts'] = time.time()
        if self._try_acquire_resize_inflight(batch_id):
            try:
                ok_r, msg_r = self._resume_partial_resize(symbol, batch_id, close_op_id)
            finally:
                self._release_resize_inflight(batch_id)
        else:
            ok_r, msg_r = False, 'resize_inflight'
        if ok_r:
            self._partial_resume_state.pop(batch_id, None)
            print(f"  └─ ✅ [partial 自愈] 批次 {batch_id}: {msg_r}")
            self.send_tg_notification(
                f"✅【partial 自愈】批次 `{batch_id}` 保护单已按净量重挂，恢复 ACTIVE。",
                level='info')
            return
        if msg_r == 'resize_inflight':
            self._partial_resume_state[batch_id] = _prs  # /partial 事务在途，不计数不告警
            return
        _prs['fails'] = int(_prs.get('fails', 0) or 0) + 1
        print(f"  └─ ⏳ [partial 自愈] 批次 {batch_id} 本轮未续跑: {msg_r}")
        if msg_r.startswith('resume_guard_rejected') or msg_r.startswith('resize_guard_rejected_'):
            _prs['halted'] = True  # 守恒冲突 = terminal，等人工（callee 已 critical）
        elif _prs['fails'] == 3:
            self.send_tg_notification(
                f"🚨【资金安全】批次 `{batch_id}` partial resize 连续 3 轮续跑失败\n"
                f"💡 最近原因: {msg_r}\n"
                f"批次保持冻结并将继续自动重试；请核对实际仓位与挂单。",
                level='critical')
        self._partial_resume_state[batch_id] = _prs

    def _maybe_runtime_finalize_limit(self, symbol, batch_id, close_op_id):
        """🔥 P5c（ChatGPT 二复审 Blocker 3）：monitor 冻结分支的 phase=2 finalizer
        运行期接管调度（60s 节流，与 partial 自愈共用状态簿记）。

        settled_by_limit_close=True + close_phase=2 = finalizer 已认领未完成
        （PnL 落盘门/converge 断点）→ 定期接管续跑（dedup 防双记）；
        不再依赖重启才有恢复链。失败静默下轮重试（finalizer 自身幂等）。"""
        _prs = dict(self._partial_resume_state.get(batch_id) or {})
        if _prs.get('op') != (close_op_id or ''):
            self._partial_resume_state[batch_id] = {'ts': time.time(), 'fails': 0,
                                                    'op': close_op_id or ''}
            return
        if _prs.get('halted') or time.time() - _prs['ts'] < 60:
            return
        _prs['ts'] = time.time()
        with self._state_lock:
            b = (self.load_all_states().get(symbol, {}) or {}).get(batch_id) or {}
            order_id = str(b.get('limit_close_order_id') or '')
        if not order_id:
            return
        if self._try_acquire_resize_inflight(batch_id):
            try:
                ok_r, msg_r = self._finalize_limit_full_fill(symbol, batch_id, order_id)
            finally:
                self._release_resize_inflight(batch_id)
        else:
            ok_r, msg_r = False, 'resize_inflight'
        if ok_r:
            self._partial_resume_state.pop(batch_id, None)
            print(f"  └─ ✅ [限价结算接管] 批次 {batch_id}: {msg_r}")
            return
        if msg_r == 'resize_inflight':
            self._partial_resume_state[batch_id] = _prs
            return
        _prs['fails'] = int(_prs.get('fails', 0) or 0) + 1
        print(f"  └─ ⏳ [限价结算接管] 批次 {batch_id} 本轮未完成: {msg_r}")
        if _prs['fails'] == 3:
            self.send_tg_notification(
                f"🚨【资金安全】批次 `{batch_id}` 限价平仓结算连续 3 轮接管失败\n"
                f"💡 最近原因: {msg_r}\n"
                f"批次保持 phase=2 并将继续自动重试；请核对实际仓位与挂单。",
                level='critical')
        self._partial_resume_state[batch_id] = _prs

    def _check_existing_conflicts(self, symbol: str, batch_id: str, all_states: dict,
                                  signal_fingerprint: str = None) -> bool:
        print(f"\n🔍 正在针对批次 [{batch_id}] 进行防冲突扫描...")

        symbol_state = all_states.get(symbol, {})

        if batch_id in symbol_state and symbol_state[batch_id].get('is_active'):
            print(f"❌ 【批次冲突】批次 [{batch_id}] 目前已在运行中！请勿重复执行。")
            return True

        # 🔥 v6.3（D-005 开仓幂等）：active 同指纹 → 拒绝第二批次。
        # entry_orders=[] 不豁免——PENDING_CREATE 语义 = create 可能已发出，
        # 空 skeleton 无法证明未发单；对账/收编由既有恢复机制负责（只 Commit 不 Create）。
        # 纯本地判据前置 → 拒绝路径零 API 权重。
        if signal_fingerprint:
            for _bid, _b in symbol_state.items():
                if _bid == batch_id or not isinstance(_b, dict) or not _b.get('is_active'):
                    continue
                if _b.get('signal_fingerprint') == signal_fingerprint:
                    print(f"❌ 【开仓幂等】已存在相同交易意图的活跃批次 [{_bid}]，拒绝重复开仓。")
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

        # D-006: 账户层风控闸门（2026-08-28）——策略限额 Fail-Closed，只拦新开仓路径。
        # 存量批次的止盈止损/平仓/监控不受影响；/force 不绕过本闸门（只绕 D-005 去重）。
        risk_allowed, risk_reason = self._check_account_risk(all_states, signal)
        if not risk_allowed:
            print(f"🚫 [D-006] 账户层风控拒绝信号 [{batch_id}] ({symbol}): {risk_reason}")
            try:
                self.send_tg_notification(
                    f"⚠️【账户层风控拦截】批次 `{batch_id}` ({symbol})\n"
                    f"原因: {risk_reason}\n"
                    f"未执行任何下单。存量批次的止盈/止损/平仓/监控不受影响。\n"
                    f"如确需开仓：调整 .env 限额（即时生效无需重启）或先平掉部分仓位。",
                    level='warning')
            except Exception:
                pass
            return None

        # 🔥 v6.3（D-005）：指纹在冲突扫描前计算——描述原始意图，与市价/跳层无关
        _signal_fp = self._compute_signal_fingerprint(signal)
        if self._check_existing_conflicts(symbol, batch_id, all_states, _signal_fp):
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
                'signal_fingerprint': _signal_fp,  # 🔥 v6.3（D-005）：开仓幂等指纹
                'entry_layers': list(skeleton_entry_layers),
                'entry_stop_steps': [signal.stop_loss_steps[i] if i < len(signal.stop_loss_steps) else 0.0
                                     for i in skeleton_entry_layers],
                'entry_orders': [],
                'stop_steps': [],
                'take_profit_price': signal.take_profit,
                'current_sl_id': None,
                'tp_order_id': None,
                'close_phase': 0,  # P0 Batch A（P0-1）：0=ACTIVE 唯一权威
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
                # 🔥 v6.4（PROVEN-CLEAN 收口）：到达此处 = 所有层均被本地价格过滤
                # 或 -2021 确定拒绝（registry 已标 ABSENT，不残留 PENDING_CREATE）——
                # 0 create_order、0 交易所副作用，数学上可证明干净。空骨架立即终止
                # active：不再伪装成风险批次参与幂等/统计。不调 clear_batch_state
                # （其 convergence proof/tombstone 契约不适用于零副作用场景）。
                try:
                    with self._state_lock:
                        latest = self.load_all_states()
                        _b = (latest.get(symbol, {}) or {}).get(batch_id)
                        if isinstance(_b, dict) and not _b.get('entry_orders'):
                            _b['is_active'] = False
                            self._persist_states(latest)
                except Exception as _e:
                    print(f"⚠️ [PROVEN-CLEAN] 空骨架停用失败（不影响拒绝结果）: {_e}")
                print("❌ 没有成功挂出任何有效开仓条件单（触发价均不符合逻辑），程序安全退出。")
                # 🔥 哨兵（Fix D）：bot_runner 据此释放 D-005 记录，允许立即修正重发；
                # 其余 None 路径（异常/未知）保持 EXECUTING 10 分钟 Fail-Closed 不变
                return 'CLEAN_REJECT'

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
                'close_phase': 0,  # P0 Batch A（P0-1）：0=ACTIVE 唯一权威；存量无字段读取侧视同 0
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
                                    order_kind='conditional', owner_op_id=None):
        """B2-0: create 成功后 verify 统一入口——按操作阶段区分异常语义（ChatGPT 评审①）：
        verify 阶段 OrderNotFound ≠ create 阶段 ExchangeError（可能是查询延迟/路由参数错误/
        交易所暂时不可见/订单已状态变化），因此：
          success    → G3b Commit 原子边界（P0 Batch A）→ registry CONFIRMED，返回 'success'
          not_found  → registry NOT_CONFIRMED（不 Commit、不计数、不自动重挂），返回 'not_found'
          unknown    → registry PENDING_VERIFY（结果未知，不计数不补单），返回 'unknown'
        P0 Batch A（G3 集成）：verify success 后经 _commit_protection_with_g3 持锁复核
        close_phase——若批次已进入平仓/已清理（g3_triggered），转 G3a 收敛后【仍返回
        'success'】：registry 已记录 PROGRAMMATIC_CANCELED 收敛真相，调用方侧效在
        批次冻结（监控循环 close_phase 冻结）下无害（v3 §1.2 裁定语义）。
        调用方只按返回值执行副作用；verify 分支内禁止 raise/计数/自动重挂（C5 事故模式）。"""
        verify_result = self._verify_order_created(order_id, symbol, order_kind)
        if verify_result == 'success':
            g3 = self._commit_protection_with_g3(symbol, batch_id, identity, order_id,
                                                 order_kind=order_kind, desc=desc,
                                                 owner_op_id=owner_op_id)
            if g3 == 'g3_triggered':
                # create 已发生但批次已进入平仓/已清理 → G3a 收敛（锁外，可发 API）
                self._g3a_converge_race_order(symbol, batch_id, identity, order_id,
                                              order_kind=order_kind, desc=desc)
            return 'success'
        elif verify_result == 'not_found':
            self._update_registry(symbol, batch_id, identity, state='NOT_CONFIRMED',
                                  order_id=order_id, id_known=True)
        else:
            self._update_registry(symbol, batch_id, identity, state='PENDING_VERIFY',
                                  order_id=order_id, id_known=True)
        return verify_result

    # ==================== P0 Batch A（2026-08-28 限价平仓竞态）: G2 / G3 / N14 ====================

    def _final_pre_create_check(self, symbol, batch_id, identity, desc='保护单',
                                owner_op_id=None):
        """G2（P0 Batch A，规格 v3 §1.1）：create 紧前最终复核——仲裁闸门通过后、
        PENDING_CREATE 意图落盘前的关闭态复检。与 G1（_assert_create_allowed）分工：
        G1 = registry 状态机仲裁（含 PROGRAMMATIC_CANCELED 禁建）；G2 = close_phase
        关闭态复核（P0-1：只读 close_phase 唯一权威，legacy pending_close 保守兼容 belt，
        Boolean 不参与判定表达式）。user_modified 绝不作为授权条件（终审硬约束③）。
        实施落点 = gate 调用后、PENDING_CREATE 写入之前（偏离诚实记载见规格 v3 §9.5：
        create 之后到 G3a/G3b 的残余窗口由 G3 全覆盖——这正是 G3 存在的理由）。
        返回 (allowed: bool, reason: str)。"""
        latest_all = self.load_all_states()
        b = latest_all.get(symbol, {}).get(batch_id)
        if b is None:
            return False, (f"批次 {batch_id} 状态不存在（G2 require_live_batch），"
                           f"禁止为缺失批次 Create [{desc}]")
        close_phase = int(b.get('close_phase', 0) or 0)
        if close_phase >= 1 or b.get('pending_close'):
            if _partial_resize_owner_ok(b, owner_op_id):
                pass  # 🔥 v6.4-P0：partial 事务自身 resize（极窄 owner exception）
            else:
                return False, (f"批次 {batch_id} 已进入平仓流程(close_phase={close_phase})，"
                               f"G2 禁止创建/替换保护单 [{desc}]")
        return True, ''

    def _commit_protection_with_g3(self, symbol, batch_id, identity, order_id,
                                   order_kind='conditional', desc='保护单',
                                   owner_op_id=None):
        """G3b（P0-3 + 终审硬约束①）：保护单 Commit 原子提交边界。
        持 _state_lock 且锁内 load_all_states() 重新读取最新磁盘（禁用 G3a/verify
        阶段任何旧快照）；同一持锁段内完成"关闭态复核 + CONFIRMED Commit"——
        关闭线程写 close_phase=1 同样必须先拿 _state_lock 才能落盘 → 二者串行化，
        复核与 Commit 之间无线程穿插点（现状 _update_registry 锁外复核 TOCTOU 终结）。
        返回：
          'committed'    → 批次存活且未进入平仓 → registry 已写 CONFIRMED 并落盘
          'g3_triggered' → 批次缺失/已进入平仓（close_phase≥1 或 legacy pending_close）
                           → 未写 CONFIRMED；调用方须转 _g3a_converge_race_order 收敛
        边界：锁内零交易所 API；_state_lock 非重入 → 禁止调用 save_batch_state/
        _update_registry（内部再取锁会死锁），直接操作 dict + _persist_states（L1249 契约）。"""
        with self._state_lock:
            all_states = self.load_all_states()  # 硬约束①：锁内重读，禁旧快照
            b = all_states.get(symbol, {}).get(batch_id)
            if b is None:
                return 'g3_triggered'
            _close_phase = int(b.get('close_phase', 0) or 0)
            if _close_phase >= 1 or b.get('pending_close'):
                if _partial_resize_owner_ok(b, owner_op_id):
                    pass  # 🔥 v6.4-P0：partial 事务自身 resize（极窄 owner exception）
                else:
                    return 'g3_triggered'
            entry = b.setdefault('protection_registry', {}).setdefault(identity, {})
            entry['state'] = 'CONFIRMED'
            entry['order_id'] = order_id
            entry['id_known'] = True
            if order_kind:
                entry['order_kind'] = order_kind
            entry['updated_at'] = time.time()
            self._persist_states(all_states)
            return 'committed'

    def _g3_cancel_race_order(self, symbol, order_id, order_kind='conditional') -> bool:
        """G3a 内部：撤销竞态订单（条件单走 stop=True algo 端点）。
        -2011/Unknown order 视同成功（订单已不存在 = 已收敛）。撤销失败 → critical
        + 返回 False（调用方转 HARD_LOCK，Fail-Closed 绝不 clear）。"""
        try:
            if order_kind == 'conditional':
                self._safe_api_call(self.exchange.cancel_order, order_id, symbol, params={'stop': True})
            else:
                self._safe_api_call(self.exchange.cancel_order, order_id, symbol)
            return True
        except Exception as e:
            if 'Unknown order' in str(e) or '-2011' in str(e):
                return True  # 已不存在 = 已收敛
            self.send_tg_notification(
                f"🚨【资金安全】G3a 竞态订单撤销失败（Fail-Closed）\n"
                f"🪙 标的：`{symbol}`\n📋 订单：`{order_id}`\n"
                f"💡 原因：`{str(e)[:120]}`\n"
                f"⚠️ 已 HARD_LOCK + critical，绝不自动 clear，请人工核实残单！",
                level='critical')
            return False

    def _g3_log_position_recheck(self, symbol, batch_id, order_id, filled_amount):
        """G3a（核账）：FILLED/PARTIALLY_FILLED 收敛后重核 position（风险减少方向）。
        仅日志 + 复用 _get_current_position_amt，不驱动任何状态变更（终审正交不变量：
        订单终态不反向驱动 batch close_phase）。"""
        try:
            pos_amt = self._get_current_position_amt(symbol, False) or 0.0
            print(f"  └─ [G3a 核账] {symbol} 竞态单 {order_id} 成交 {filled_amount} 后持仓 ≈ {pos_amt}（结算对账参考）")
        except Exception as e:
            print(f"  └─ ⚠️ [G3a 核账] position 重核失败（仅日志）: {e}")

    def _g3a_converge_race_order(self, symbol, batch_id, identity, order_id,
                                 order_kind='conditional', desc='保护单'):
        """G3a（P0-2 + 终审硬约束②）：create 竞态订单收敛（锁外，可发 API）。
        触发：G3b 返回 g3_triggered（create 已发生，但批次已进入平仓/已清理）。
        订单已在交易所，不得简单 cancel——先 fetch 最终状态，按"数量事实
        （filled/amount）第一优先级、status 第二"联合判定分支收敛（冲突不按未成交处理）：
          filled≥amount 或 已终结但 filled>0  → FILLED：风险已减少，非异常不 HARD_LOCK，
              重核 position 核账，registry=PROGRAMMATIC_CANCELED(g3_race_filled@数量)
          0<filled<amount                    → PARTIALLY_FILLED：撤余量+重核
              （g3_race_partial_filled@数量，撤余量失败→HARD_LOCK+critical）
          canceled/expired/rejected 无成交   → 已收敛（g3_race_terminal_*）
          其余（open/new/active/未知）        → cancel（g3_race_canceled；失败→HARD_LOCK）
        fetch 异常 → UNKNOWN ≠ EMPTY（P0-5 同哲学）：PENDING_VERIFY + hard_locked +
        critical，交 Batch B 两源扫描兜底（id_known=True → L1 精确归属）。
        返回收敛结果（'filled'/'partial'/'terminal'/'canceled'/'cancel_failed'/'unknown'）。"""
        try:
            if order_kind == 'conditional':
                order = self._safe_api_call(self.exchange.fetch_order, order_id, symbol,
                                            params={'stop': True}, retries=1)
            else:
                order = self._safe_api_call(self.exchange.fetch_order, order_id, symbol, retries=1)
        except Exception as e:
            self._update_registry(symbol, batch_id, identity, state='PENDING_VERIFY',
                                  order_id=order_id, id_known=True, hard_locked=True,
                                  terminated_reason='g3_race_fetch_unknown')
            self.send_tg_notification(
                f"🚨【资金安全】G3a 竞态订单状态未知（UNKNOWN ≠ 不存在）\n"
                f"🆔 批次：`{batch_id}` / `{symbol}`\n📌 {desc}（identity `{identity}`）\n"
                f"📋 订单：`{order_id}`\n💡 原因：`{str(e)[:120]}`\n"
                f"⚠️ 已硬锁 + PENDING_VERIFY，交两源扫描兜底，绝不自动 clear！",
                level='critical')
            return 'unknown'
        status = str(order.get('status') or '').lower()
        try:
            filled = float(order.get('filled') or 0.0)
        except (TypeError, ValueError):
            filled = 0.0
        try:
            amount = float(order.get('amount') or 0.0)
        except (TypeError, ValueError):
            amount = 0.0
        terminal_status = status in ('canceled', 'expired', 'rejected', 'closed', 'filled')
        # —— 硬约束②：数量事实第一优先级，status 第二；冲突按"已有成交事实"处理 ——
        if (filled > 0 and amount > 0 and filled >= amount) or (filled > 0 and terminal_status):
            self._g3_log_position_recheck(symbol, batch_id, order_id, filled)
            self._update_registry(symbol, batch_id, identity, state='PROGRAMMATIC_CANCELED',
                                  order_id=order_id, id_known=True,
                                  terminated_reason=f'g3_race_filled@{filled:.8f}')
            print(f"  └─ ⚡ [G3a] 竞态单已成交（{filled}/{amount}, status={status}）——风险减少非异常，"
                  f"registry=PROGRAMMATIC_CANCELED(g3_race_filled)")
            return 'filled'
        if filled > 0:
            # PARTIALLY_FILLED：撤余量（Binance 撤单即撤未成交部分）+ position 重核
            if not self._g3_cancel_race_order(symbol, order_id, order_kind):
                self._update_registry(symbol, batch_id, identity, state='HARD_LOCK',
                                      order_id=order_id, id_known=True, hard_locked=True,
                                      terminated_reason=f'g3_race_partial_cancel_failed@{filled:.8f}')
                return 'cancel_failed'
            self._g3_log_position_recheck(symbol, batch_id, order_id, filled)
            self._update_registry(symbol, batch_id, identity, state='PROGRAMMATIC_CANCELED',
                                  order_id=order_id, id_known=True,
                                  terminated_reason=f'g3_race_partial_filled@{filled:.8f}')
            print(f"  └─ ⚡ [G3a] 竞态单部分成交（{filled}/{amount}）已撤余量，registry=PROGRAMMATIC_CANCELED")
            return 'partial'
        if status in ('canceled', 'expired', 'rejected'):
            self._update_registry(symbol, batch_id, identity, state='PROGRAMMATIC_CANCELED',
                                  order_id=order_id, id_known=True,
                                  terminated_reason=f'g3_race_terminal_{status}')
            print(f"  └─ ⚡ [G3a] 竞态单已终态（{status}），无需撤销，registry=PROGRAMMATIC_CANCELED")
            return 'terminal'
        # OPEN / new / active / status 未知且无成交 → 撤单
        if self._g3_cancel_race_order(symbol, order_id, order_kind):
            self._update_registry(symbol, batch_id, identity, state='PROGRAMMATIC_CANCELED',
                                  order_id=order_id, id_known=True,
                                  terminated_reason='g3_race_canceled')
            print(f"  └─ ⚡ [G3a] 竞态单未成交已撤销，registry=PROGRAMMATIC_CANCELED(g3_race_canceled)")
            return 'canceled'
        self._update_registry(symbol, batch_id, identity, state='HARD_LOCK',
                              order_id=order_id, id_known=True, hard_locked=True,
                              terminated_reason='g3_race_cancel_failed')
        return 'cancel_failed'

    def _find_registry_identity_by_order_id(self, symbol, batch_id, order_id):
        """N14（P0 Batch A）：按 order_id 反查 registry identity。
        close_position_limit 撤 TP 后须把对应 registry 条目写 PROGRAMMATIC_CANCELED 终态
        （"程序主动终结"）；TP identity 由 positionSide/layer 推导在多路径下易错，
        按 order_id 反查最稳。返回 identity 或 None。"""
        try:
            latest_all = self.load_all_states()
        except Exception:
            return None
        b = latest_all.get(symbol, {}).get(batch_id)
        if not b:
            return None
        oid = str(order_id)
        for _identity, _entry in (b.get('protection_registry') or {}).items():
            if _entry.get('order_id') is not None and str(_entry.get('order_id')) == oid:
                return _identity
        return None

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
        每次更新刷新 updated_at。
        B2-2: intent 不可变（ChatGPT③）——首次写入后不覆盖，防后期参数漂移
        导致自愈匹配失败/错收编。
        B2-4: fail_count_incr 递增条目级 fail_count 并返回新值（HARD_LOCK 判定源，§5.4）；
        hard_locked 落盘硬锁标记。
        P0 Batch A（§1.4 终态守卫）：PROGRAMMATIC_CANCELED 是订单生命周期终态，
        不可转出（状态机闭环最后防线——防止任何旧路径把它改回 CONFIRMED/ABSENT
        而复活补挂通道）。同态回写（reason 更新）放行。
        P0 Batch C 方案 A（ChatGPT 批准 2026-08-29）：**锁内化事务路径**——
        with _state_lock: load 最新 → modify → _persist_states 直写。
        不走普通 save_batch_state 的 C 类 merge：本函数是明确的"读最新→修改→写回"
        收敛事务（R2c 回滚/R3 收编/G3 终态写），merge 无法区分它与 B5 陈旧快照，
        会把合法收敛写吞掉（test_b2_restart_semantics 三态复现实证）。锁内直写后
        与 clear_batch_state/clear 持锁互斥，无 clear 竞态窗口；AST 已排查 62 个
        调用点零持锁进入，无自嵌死锁。merge/tombstone 语义不变（普通 save 调用面
        收窄为监控线程快照写——B5 目标不变）。"""
        with self._state_lock:
            latest_all = self.load_all_states()
            b = latest_all.get(symbol, {}).get(batch_id)
            if b is None:
                return None
            reg = b.setdefault('protection_registry', {})
            entry = reg.setdefault(identity, {})
            if (entry.get('state') == 'PROGRAMMATIC_CANCELED'
                    and state is not None and state != 'PROGRAMMATIC_CANCELED'):
                print(f"  └─ 🚫 [终态守卫] identity `{identity}` 已 PROGRAMMATIC_CANCELED，"
                      f"拒绝回写 state={state}（订单终态不可转出）")
                return None
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
            # 直写持锁持久化（绕过 C 类 merge；批次被 clear 则上面 b is None 已拦截）
            self._persist_states(latest_all)
        return new_fail_count

    def _commit_registry_txn(self, symbol, batch_id, reg_entries=None, batch_fields=None):
        """P0 Batch C 选项1（ChatGPT 批准 2026-08-29，严格限定 4 函数调用面）：
        读-改-写事务锁内提交——与 _update_registry 方案 A 同范式，专供
        "决策在锁外计算（含交易所 API / TG 告警）、提交在锁内直写"的收敛路径
        （启动校验回滚 R2c / 自愈收编 R3 / entry 链重建 R3b）。
        不走普通 save_batch_state 的 C 类 merge：merge 无法区分 B5 陈旧快照与
        合法收敛写，磁盘条目 ∈ 保护集时会把回滚/收编吞掉（b2_restart 三态复现实证）。
        reg_entries: {identity: entry 快照}——逐条应用：
          终态守卫：磁盘 PROGRAMMATIC_CANCELED 不可被非同态快照覆盖（同 _update_registry）；
          intent 不可变（B2-2）：磁盘已有 intent 则保留磁盘值（防自愈指纹漂移）。
        batch_fields: {batch 级字段: 新值}——None 值跳过（id 镜像只补不清）。
        批次已不存在（被 clear）→ 放弃写入返回 False（不复活，墓碑精神 C-I）。
        ⚠️ 调用方不得持有 _state_lock（AST 已排查全库调用点零锁内进入，无自嵌死锁）。"""
        if not reg_entries and not batch_fields:
            return True
        with self._state_lock:
            latest_all = self.load_all_states()
            latest_b = latest_all.get(symbol, {}).get(batch_id)
            if latest_b is None:
                return False
            if reg_entries:
                reg = latest_b.setdefault('protection_registry', {})
                for identity, snap in reg_entries.items():
                    if not isinstance(snap, dict):
                        continue
                    disk = reg.get(identity)
                    if (isinstance(disk, dict) and disk.get('state') == 'PROGRAMMATIC_CANCELED'
                            and snap.get('state') != 'PROGRAMMATIC_CANCELED'):
                        print(f"  └─ 🚫 [终态守卫] identity `{identity}` 已 PROGRAMMATIC_CANCELED，"
                              f"锁内直写拒绝覆盖（订单终态不可转出）")
                        continue
                    if isinstance(disk, dict) and disk.get('intent') is not None:
                        snap = dict(snap)
                        snap['intent'] = disk['intent']
                    reg[identity] = snap
            for k, v in (batch_fields or {}).items():
                if v is None:
                    continue
                latest_b[k] = v
            self._persist_states(latest_all)
        return True

    def _assert_create_allowed(self, symbol, batch_id, identity, desc='保护单', replace_order_id=None,
                               owner_op_id=None):
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
            # P0 Batch A（D5 裁定 require_live_batch 语义默认全拒）：批次缺失 = 已清理或
            # 从未落盘 → 禁止为其创建保护单（封死场景 B：旧线程为已 clear 批次补挂
            # → 孤儿 TP 事故通道）。入场单路径不经本闸门（骨架先落盘），不受影响。
            return False, (f"批次 {symbol}/{batch_id} 状态不存在（require_live_batch），"
                           f"禁止为缺失批次 Create [{desc}]")
        # P0-1（G1 关闭态检查）：只读 close_phase 唯一权威（int 单调），legacy
        # pending_close 作保守兼容 belt；Boolean 不参与判定表达式（v3 §2.2 硬规则 1/2）
        _close_phase = int(b.get('close_phase', 0) or 0)
        if _close_phase >= 1 or b.get('pending_close'):
            if _partial_resize_owner_ok(b, owner_op_id):
                pass  # 🔥 v6.4-P0：partial 事务自身 resize（极窄 owner exception）
            else:
                return False, (f"批次 {batch_id} 已进入平仓流程(close_phase={_close_phase})，"
                               f"禁止创建/替换保护单 [{desc}]")
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
        if state in ('PENDING_CREATE', 'PENDING_VERIFY', 'NOT_CONFIRMED', 'CONFIRMED', 'MISMATCH',
                     'PROGRAMMATIC_CANCELED'):
            if state == 'CONFIRMED' and replace_order_id and entry.get('order_id') == replace_order_id:
                # B2-8 换挂语义：确认的旧单将被撤销替换（先撤后挂/先挂后撤，旧单物理离开）
                self._gate_alert_clear(identity)
                return True, ''
            if state == 'PROGRAMMATIC_CANCELED':
                # P0 Batch A（§1.4）：程序主动终结的订单绝不再 create（无 replace 豁免）
                return False, (f"identity `{identity}` 已程序终结(PROGRAMMATIC_CANCELED"
                               f"{', reason=' + entry['terminated_reason'] if entry.get('terminated_reason') else ''})，"
                               f"禁止再次 Create")
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
                txn_reg = {}  # P0 Batch C 选项1：收集实际改动 identity 快照（锁内直写提交）
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
                            txn_reg[identity] = dict(entry)
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
                        txn_reg[identity] = dict(entry)
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
                    # P0 Batch C 选项1（ChatGPT 批准 2026-08-29）：锁内直写——绕过
                    # save_batch_state 的 C 类 merge（磁盘 ∈ 保护集 → 保留磁盘会吞掉
                    # 本函数的合法回滚/补锁写，R2c 实证）。TG 告警已在上方锁外发送，
                    # 持锁区仅做磁盘事务。
                    self._commit_registry_txn(symbol, batch_id, reg_entries=txn_reg)
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
            if state == 'PROGRAMMATIC_CANCELED':
                # P0 Batch A（§1.4 + §9.4 正交不变量）：程序主动终结的订单
                # （close_requested_canceled / g3_race_*）绝不再补挂/收养——
                # 双保险之一（另一处：_update_registry 终态守卫拒绝转出）
                return 'hold', None
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
        txn_reg = {}     # P0 Batch C 选项1：实际改动 identity 快照（锁内直写提交）
        txn_fields = {}  # id 镜像 Commit（只补不清）
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
                    txn_reg[identity] = dict(entry)
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
                txn_reg[identity] = dict(entry)
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
                txn_reg[identity] = dict(entry)
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
                txn_fields['current_sl_id'] = order_id
            elif role == 'TP' and b.get('tp_order_id') is None:
                b['tp_order_id'] = order_id
                txn_fields['tp_order_id'] = order_id
            entry['state'] = 'CONFIRMED'
            entry['updated_at'] = time.time()
            txn_reg[identity] = dict(entry)
            changed = True
            # R-B: 确认成功 → 清零持续未确认轮次
            rounds = getattr(self, '_self_heal_unconfirmed_rounds', None)
            if isinstance(rounds, dict):
                rounds.pop((symbol, batch_id, identity), None)
        if changed:
            # P0 Batch C 选项1（ChatGPT 批准 2026-08-29）：锁内直写——R3 收编
            # （PENDING_VERIFY→CONFIRMED + id 镜像 Commit）不再被 C 类 merge 吞掉。
            # API fetch / 告警已在上方锁外完成，持锁区仅做磁盘事务。
            self._commit_registry_txn(symbol, batch_id, reg_entries=txn_reg,
                                      batch_fields=txn_fields)

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
            txn_reg = {}  # P0 Batch C 选项1：锁内直写提交收集
            for identity, entry in targets:
                if entry.get('state') != 'PENDING_VERIFY' or entry.get('id_known'):
                    entry['state'] = 'PENDING_VERIFY'
                    entry['id_known'] = False
                    entry['updated_at'] = time.time()
                    txn_reg[identity] = dict(entry)
            # P0 Batch C 选项1：锁内直写（绕过 C 类 merge，同 _update_registry 方案 A）
            self._commit_registry_txn(symbol, batch_id, reg_entries=txn_reg)
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
        txn_reg = {}  # P0 Batch C 选项1：锁内直写提交收集
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
                txn_reg[identity] = dict(entry)
                changed = True
            elif len(matches) > 1:
                # 命中多条 → NOT_CONFIRMED + critical（人工裁决，禁止自动收编多条）
                entry['state'] = 'NOT_CONFIRMED'
                entry['updated_at'] = time.time()
                txn_reg[identity] = dict(entry)
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
                txn_reg[identity] = dict(entry)
                changed = True
        if changed:
            # P0 Batch C 选项1（ChatGPT 批准 2026-08-29）：锁内直写（绕过 C 类 merge）。
            # 快照 API / critical 告警已在上方锁外完成，持锁区仅做磁盘事务。
            self._commit_registry_txn(symbol, batch_id, reg_entries=txn_reg)

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
        # P0 Batch C 选项1（ChatGPT 批准 2026-08-29）：锁内直写——R3b entry 链重建
        # 只提交本函数重建的骨架字段（registry 是重建的输入，保留磁盘最新值不动）；
        # 绕过 C 类 merge，防止收编后的 CONFIRMED 条目把重建写吞掉。
        txn_fields = {
            'entry_orders': entry_orders,
            'target_amounts': target_amounts,
            'batch_total_amount': b.get('batch_total_amount'),
            'layer_sl_params': layer_sl_params,
            'prepared_tp_params': b.get('prepared_tp_params'),
            'updated_at': b.get('updated_at'),
        }
        if stop_steps:
            txn_fields['stop_steps'] = stop_steps
        if not b.get('last_filled_count'):
            txn_fields['pending_sl_orders'] = list(range(len(entry_orders)))
        self._commit_registry_txn(symbol, batch_id, batch_fields=txn_fields)
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
                # 🔥 v6.4-P3（G1）：生命周期守卫——醒来后先重证生存资格，再允许任何 API。
                # 磁盘生命周期是唯一权威：批次已被对账/清理（如 /auth_reset）→ 线程立即退出
                # （零 API/零结算/零补挂）。UNKNOWN ≠ EMPTY：账本损坏绝不解释为「已清理」。
                _g1_all = self.load_all_states()
                _g1_state = self._monitor_lifecycle_check(
                    _g1_all, _g1_all.get(symbol, {}).get(batch_id, {}))
                if _g1_state == 'exit':
                    print(f"  └─ 🛬 [生命周期] 批次 {batch_id} 已从账本消失/停用，监控线程正常退出（零 API 零副作用）")
                    break
                if _g1_state == 'unknown':
                    print(f"  └─ ⏸️ [生命周期] 账本 UNKNOWN（损坏），本轮跳过全部轮询副作用")
                    continue
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
                except AuthBlockedError as abe:
                    # 🔥 D-010 T4：盲区休眠——300s 纯本地等待（零 API），醒来重读锁文件，
                    # 仍锁继续睡（闸门在 _safe_api_call 入口本地读 auth_blocked.json，无网络请求）
                    print(f"🔒 [盲区安全模式] 监控轮询跳过（{AUTH_BLIND_SLEEP_SECONDS}s 后重查锁状态）: {abe}")
                    time.sleep(AUTH_BLIND_SLEEP_SECONDS)
                    continue
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
                                if not is_programmatic:
                                    # v6.2 ΔE1 后 🗑️ 不写批量级 flag；程序撤单事实按 ID 存于 registry
                                    for _pc_e in (latest_b_data_check.get('protection_registry') or {}).values():
                                        if (isinstance(_pc_e, dict)
                                                and str(_pc_e.get('order_id')) == str(order_id)
                                                and _pc_e.get('state') == 'PROGRAMMATIC_CANCELED'):
                                            is_programmatic = True
                                            break

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
                        # P0 Batch B：converge 证明后才 clear；未收敛不 break，下轮重试
                        _proof = self._converge_batch_orders_before_clear(symbol, batch_id)
                        if _proof is not None and self.clear_batch_state(symbol, batch_id, proof=_proof):
                            self.send_tg_notification(
                                f"🧹 **[批次终止]** 批次 `{batch_id}` 在建仓前挂单已全撤，后台监控退出。")
                            break
                        print(f"  └─ ⚠️ [B] 批次 {batch_id} 本轮未收敛（UNKNOWN/撤单失败），保留待下轮重试")
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

                # 🔥 v6.4-P6（v1.3 契约）：守恒观察器每轮无条件调用——收敛与
                # 「同方向批次<2」的清理依赖持续观察；观察器内部按 (symbol, side)
                # 认领事件（锁内）+ 幂等去重，多批次监控线程并发安全；复用本轮
                # 已取得的方向仓位，零新增 API。
                self._maybe_report_conservation_conflict(symbol, side, current_actual_position)

                # ==================== 持仓归零检测 ====================
                if current_actual_position is not None and has_entered_position and batch_filled_amount > 0:
                    if current_actual_position == 0:
                        latest_all = self.load_all_states()
                        latest_b_data = latest_all.get(symbol, {}).get(batch_id, {})
                        # 🔥 v6.4-P3（G2）：TOCTOU 二次守卫——G1 通过后本周期内批次可能已被
                        # /auth_reset 对账清理；结算/撤单/converge 前必须重证生存资格。
                        # （2026-09-02 18:45 实盘：僵尸监控对已墓碑批次反复发假结算报告）
                        _lc2 = self._monitor_lifecycle_check(latest_all, latest_b_data)
                        if _lc2 == 'exit':
                            print(f"  └─ 🛬 [生命周期] 批次 {batch_id} 已被对账/外部归档，监控线程退出（零结算零副作用）")
                            break
                        if _lc2 == 'unknown':
                            print(f"  └─ ⏸️ [生命周期] 账本 UNKNOWN（损坏），本轮跳过归零结算")
                            continue

                        # 🔥 P5c（ChatGPT 二复审 Blocker 2，P0）：已被限价平仓 finalizer
                        # 认领（settled=True）→ 统一路由共享 finalizer 接管续跑，
                        # **禁止本分支自行 converge+clear**——finalizer 的 PnL 落盘门
                        # 是批次清理的唯一守卫，旁路 clear 会永久丢失成交记录。
                        # finalizer 可重试失败 → 保持 phase=2 下轮重试（dedup 防双记）。
                        if latest_b_data.get('settled_by_limit_close', False):
                            _fid = (latest_b_data.get('limit_close_order_id') or '')
                            if _fid:
                                _ok_f, _msg_f = self._finalize_limit_full_fill(
                                    symbol, batch_id, _fid)
                                if _ok_f:
                                    break
                                print(f"  └─ ⚠️ [P5 finalizer] 批次 {batch_id} "
                                      f"本轮未完成（{_msg_f}），保持 phase=2 下轮重试")
                                continue
                            print(f"🚨【资金安全】批次 `{batch_id}` settled=True 但"
                                  f"限价订单 ID 缺失（异常态），已拒绝自行清理，请人工核对。")
                            continue

                        # 🔥 P5e：人工核对冻结态（持久化）——无副作用可见冻结，
                        # 节流 critical（首报由 _mark 时发送，此处 1h 重复提醒）
                        if latest_b_data.get('close_reason') == 'limit_cancel_manual_review':
                            if time.time() - self._freeze_alerted.get(batch_id, 0) >= 3600:
                                self._freeze_alerted[batch_id] = time.time()
                                self.send_tg_notification(
                                    f"🚨【资金安全】批次 `{batch_id}` 仍处于人工核对冻结"
                                    f"（限价撤单 + 仓位已被止损归零，归属未明确）。\n"
                                    f"💡 请核对交易所成交记录后人工处理。",
                                    level='critical')
                            print(f"  └─ 🧊 [P5] 批次 {batch_id} 人工核对冻结中"
                                  f"（manual_review），跳过保护单维护")
                            continue
                        # 🔥 P5c（同 Blocker 2）：限价平仓事务在途（未 settled）而仓位
                        # 已归零（如 SL 窗口触发）——在途限价单若不撤，价格回落可能
                        # 对零仓位开反向仓。撤单（-2011 幂等）后按成交量分型。
                        # 🔥 P5e（ChatGPT 四复审 P0）：仓位已归零时恢复 ACTIVE 无意义
                        # （守恒门必拒）——除 FULL_FILL（走 finalizer 正确结算）外
                        # 一律原子写入 manual_review 持久冻结，绝不恢复、绝不静默
                        # clear（R18a/R18b）。覆盖度兜底含 canceled+filled=全量
                        # 退化形态（R21）。
                        # 🔥 P5g：统一分型路由（与 finally 清理边界共用实现）
                        if (latest_b_data.get('close_reason') in (
                                'limit_pending_normal', 'limit_cancel_restore_pending')
                                and latest_b_data.get('limit_close_order_id')):
                            _ok_r0, _msg_r0 = self._route_zero_position_limit_close(
                                symbol, batch_id, position_zero=0.0)
                            print(f"  └─ {'✅' if _ok_r0 else '⚠️'} [P5] 批次 {batch_id} "
                                  f"限价在途+仓位归零分型: {_msg_r0}")
                            continue

                        # 🔥 如果是程序平仓，跳过结算
                        if latest_b_data.get('pending_close', False) or latest_b_data.get('is_programmatic_cancel',
                                                                                          False):
                            print(f"ℹ️ [程序平仓] 批次 [{batch_id}] 由程序触发平仓，跳过结算")
                            self._cancel_remaining_entries(symbol, entry_orders, filled_layers)
                            # P0 Batch B：converge 证明后才 clear；未收敛不 break，下轮重试
                            _proof = self._converge_batch_orders_before_clear(symbol, batch_id)
                            if _proof is not None and self.clear_batch_state(symbol, batch_id, proof=_proof):
                                break
                            print(f"  └─ ⚠️ [B] 批次 {batch_id} 本轮未收敛（UNKNOWN/撤单失败），保留待下轮重试")

                        print(f"🛑 [持仓归零检测] 批次 [{batch_id}] 实际持仓已归零，正在安全退出监控...")

                        # 🔥 计算实际盈亏（v6.4：净仓位/净成本基准；剩余 fee 按 cost 比例分摊）
                        # 🔥 v6.4-P2（Fix A）：此处曾引用未绑定的 b_data（变量早已改名
                        # latest_b_data；finally 区才赋值 b_data → 局部变量未绑定）
                        # → 外部（app 手动）全平场景监控线程崩溃，批次失去 SL/TP 维护
                        # （2026-09-02 16:30 实盘事故，ChatGPT 终审 P0 批准修复）
                        # 🔥 v6.4-P3（Fix④）：结算报告原子认领——_state_lock 内 CAS，
                        # 唯一 owner 才打印/发送（at-most-once；persist 失败/已认领/批次
                        # 消失 → 不发送）。converge UNKNOWN 重试轮绝不重复发报告。
                        if batch_filled_amount > 0 and self._claim_settlement_reported(symbol, batch_id):
                            # 计算持仓均价（含手续费）
                            _net_qty, _net_cost = self._batch_net_position(latest_b_data)
                            _rr_cost = float(latest_b_data.get('realized_reduce_cost', 0.0) or 0.0)
                            _gross_cost = _net_cost + _rr_cost
                            _fee_rem = float(total_entry_fee or 0.0) * _net_cost / _gross_cost \
                                if _gross_cost > 0 else 0.0
                            total_cost_with_fee = _net_cost + _fee_rem
                            avg_price_with_fee = total_cost_with_fee / _net_qty if _net_qty > 0 else 0
                            # 🔥 v6.4-P3（R2 修正）：结算数量统一用 durable 净量——
                            # partial 后内存 gross（batch_filled_amount）不再是剩余仓位，
                            # PnL/手续费/报告数量全部按 _net_qty（2026-09-02 实盘「数量也不对」）
                            settlement_qty = _net_qty

                            # 获取当前市价（平仓价格）
                            try:
                                ticker = self._safe_api_call(self.exchange.fetch_ticker, symbol)
                                exit_price = float(ticker.get('last') or ticker.get('close') or 0.0)
                            except Exception:
                                exit_price = avg_price_with_fee

                            # 计算盈亏
                            if side == 'BUY':
                                gross_pnl = (exit_price - avg_price_with_fee) * settlement_qty
                            else:
                                gross_pnl = (avg_price_with_fee - exit_price) * settlement_qty

                            # 估算平仓手续费（市价平仓用 TAKER_FEE_RATE）
                            exit_fee = exit_price * settlement_qty * TAKER_FEE_RATE
                            total_fees = total_entry_fee + exit_fee
                            net_pnl = gross_pnl - total_fees

                            capital_base = avg_price_with_fee * settlement_qty if settlement_qty > 0 else 1
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
                                f"🔢 **平仓数量**：`{settlement_qty}`\n"
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
                        # P0 Batch B：converge 证明后才 clear；未收敛不 break，下轮重试
                        _proof = self._converge_batch_orders_before_clear(symbol, batch_id)
                        if _proof is not None and self.clear_batch_state(symbol, batch_id, proof=_proof):
                            break
                        print(f"  └─ ⚠️ [B] 批次 {batch_id} 本轮未收敛（UNKNOWN/撤单失败），保留待下轮重试")

                # ==================== 部分减仓检测（自动更新止盈止损单） ====================
                _fb_other = 0
                if current_actual_position is not None and has_entered_position and current_actual_position < batch_filled_amount:
                    _fb_states = self.load_all_states()
                    _fb_sym = _fb_states.get(symbol, {})
                    _fb_other = sum(1 for b, d in _fb_sym.items() if d.get('is_active', False) and b != batch_id)
                    if _fb_other > 0:
                        print(f"  ┏━ ⏭️ [多批次] 跳过部分减仓检测 (同symbol活跃批次: {_fb_other + 1})")
                        # 🔥 v6.4-P6：守恒检测已外提为每轮无条件观察器（本分支
                        # 不再调用）——v1.3 契约：收敛/批次<2 清理依赖持续观察。

                if _fb_other == 0 and current_actual_position is not None and has_entered_position and 0 < current_actual_position < batch_filled_amount:
                    # 🔥 v6.2（实盘 2026-09-01 17:2x）：持仓归零（0.0）不是「部分减仓」，
                    # 交给持仓归零检测分支安全退出——否则 amount_to_precision(0.0)
                    # 被 ccxt 拒绝（< 最小精度 0.001），监控线程崩溃 + 误报资金安全告警。
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
                                # G2（P0 Batch A）：create 紧前关闭态复核——失败流入既有 not-allowed 分支
                                if allowed:
                                    _g2_ok, _g2_reason = self._final_pre_create_check(
                                        symbol, batch_id, sl_identity, desc='部分减仓换挂止损')
                                    if not _g2_ok:
                                        allowed, gate_reason = False, _g2_reason
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
                                # G2（P0 Batch A）：create 紧前关闭态复核——失败流入既有 not-allowed 分支
                                if allowed:
                                    _g2_ok, _g2_reason = self._final_pre_create_check(
                                        symbol, batch_id, tp_identity, desc='部分减仓换挂止盈')
                                    if not _g2_ok:
                                        allowed, gate_reason = False, _g2_reason
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
                # 🔥 v6.4-P3（G3）：保护单维护前生命周期守卫——sibling 仍有持仓时僵尸线程
                # 不会命中归零分支，必须在此重证生存资格（绝不维护/补挂已清理批次的 SL/TP）。
                _lc3 = self._monitor_lifecycle_check(latest_all, latest_b_data)
                if _lc3 == 'exit':
                    print(f"  └─ 🛬 [生命周期] 批次 {batch_id} 已从账本消失/停用，监控线程退出（保护单维护终止）")
                    break
                if _lc3 == 'unknown':
                    print(f"  └─ ⏸️ [生命周期] 账本 UNKNOWN（损坏），本轮保护单维护跳过")
                    continue

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

                # ===== P0（2026-08-28 限价平仓竞态）Batch A 风控冻结 =====
                # 批次已进入平仓流程（close_phase≥1 唯一权威 P0-1；legacy pending_close
                # 保守兼容 belt）→ 本轮跳过全部 SL/TP 补挂与维护（R14/首挂/换挂/降级恢复
                # 全部位于下方——孤儿 TP 事故的补挂通道在此封死）。冻结点位于成交检测与
                # 持仓归零分支（结算/退出路径）之后；循环头部 sleep 保证不忙等。
                _b_close_phase = int((latest_b_data or {}).get('close_phase', 0) or 0)
                if _b_close_phase >= 1 or (latest_b_data or {}).get('pending_close'):
                    _close_reason = ((latest_b_data or {}).get('close_reason')
                                     or 'settlement_stuck')  # 缺失 = 遗留冻结 → fail-noisy
                    # 🔥 v6.4-P2（Fix C）：console 冻结提示节流——状态变化立即打印，
                    # 持续不变每 300s heartbeat 一条（此前每周期无条件 print 实盘刷屏 70+ 行；
                    # 「3 次后静默」约定只覆盖 TG 通道，console 从未限流）。
                    # 签名含 close_op_id（ChatGPT P2 边界）：同批次新事务即使 reason/phase
                    # 相同也视为新事件立即打印，且退出冻结后旧缓存不会吞掉新事务首报。
                    _close_op = (latest_b_data or {}).get('close_op_id') or ''
                    _fps = self._freeze_print_state.get(batch_id) or ('', 0, '', 0.0)
                    if _close_reason != _fps[0] or _b_close_phase != _fps[1] \
                            or _close_op != _fps[2] or time.time() - _fps[3] >= 300:
                        print(f"  └─ 🧊 [P0 冻结] 批次 {batch_id} 处于平仓流程"
                              f"(close_phase={_b_close_phase}, reason={_close_reason})，"
                              f"本轮跳过保护单维护")
                        self._freeze_print_state[batch_id] = (_close_reason, _b_close_phase,
                                                              _close_op, time.time())
                    # 🔒 v6.2-r4：FREEZE_QUIET_REASONS（market_confirming /
                    # limit_pending_normal）之外一律周期 critical——
                    # limit_creating 是 transient，crash 重启后必须 loud（M25）。
                    # v6.4：partial_resize_pending 进 quiet（可自动续跑的确定性中间态）；
                    # partial_closing 不进 quiet（transient，重启必须 loud）
                    # P5：limit_cancel_restore_pending 同属「可自动续跑的确定性
                    # 中间态」（归属已 durable，恢复由 _resume_closecancel_restore 续跑）
                    if _close_reason not in ('market_confirming', 'limit_pending_normal',
                                             'partial_resize_pending',
                                             'limit_cancel_restore_pending'):
                        if time.time() - self._freeze_alerted.get(batch_id, 0) >= 3600:
                            self._freeze_alerted[batch_id] = time.time()
                            self.send_tg_notification(
                                f"🚨【资金安全】批次平仓流程卡死，保护单停止维护！\n"
                                f"🆔 批次: `{batch_id}`\n"
                                f"🧊 close_phase={_b_close_phase}, reason={_close_reason}\n"
                                f"⚠️ 该批次的 SL/TP 不再被补挂 / 换挂 / 降级恢复。\n"
                                f"💡 请人工核对持仓与挂单，必要时手动平仓。",
                                level='critical')
                    # 🔥 v6.4-P1 + P5：partial_resize_pending / limit_cancel_restore_pending
                    # 运行期自愈调度（60s 节流，R7 守恒 terminal 停机 / R8 首见只登记——
                    # 见 _maybe_runtime_resume_partial；路由在 _resume_partial_resize 内）
                    if _b_close_phase == 1 and _close_reason in (
                            'partial_resize_pending', 'limit_cancel_restore_pending'):
                        self._maybe_runtime_resume_partial(
                            symbol, batch_id,
                            (latest_b_data or {}).get('close_op_id'))
                    # 🔥 P5c（ChatGPT 二复审 Blocker 3）：phase=2 finalizer 运行期接管
                    # （settled 已认领未完成 → 60s 节流定期续跑，不再依赖重启）
                    elif _b_close_phase == 2 \
                            and (latest_b_data or {}).get('settled_by_limit_close') \
                            and (latest_b_data or {}).get('limit_close_order_id'):
                        self._maybe_runtime_finalize_limit(
                            symbol, batch_id,
                            (latest_b_data or {}).get('close_op_id'))
                    continue

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

                    # P0 Batch B：converge 证明后才 clear；未收敛不 break，下轮重试
                    _proof = self._converge_batch_orders_before_clear(symbol, batch_id)
                    if _proof is not None and self.clear_batch_state(symbol, batch_id, proof=_proof):
                        break
                    print(f"  └─ ⚠️ [B] 批次 {batch_id} 本轮未收敛（UNKNOWN/撤单失败），保留待下轮重试")

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

                    # P0 Batch B：converge 证明后才 clear；未收敛不 break，下轮重试
                    _proof = self._converge_batch_orders_before_clear(symbol, batch_id)
                    if _proof is not None and self.clear_batch_state(symbol, batch_id, proof=_proof):
                        break
                    print(f"  └─ ⚠️ [B] 批次 {batch_id} 本轮未收敛（UNKNOWN/撤单失败），保留待下轮重试")

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
                                    # G2（P0 Batch A）：create 紧前关闭态复核——失败流入既有 not-allowed 分支
                                    if allowed:
                                        _g2_ok, _g2_reason = self._final_pre_create_check(
                                            symbol, batch_id, sl_identity, desc='补挂止损单')
                                        if not _g2_ok:
                                            allowed, gate_reason = False, _g2_reason
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
                                            # G2（P0 Batch A）：create 紧前关闭态复核——失败流入既有 not-allowed 分支
                                            if allowed:
                                                _g2_ok, _g2_reason = self._final_pre_create_check(
                                                    symbol, batch_id, recovery_identity, desc='降级恢复止损单')
                                                if not _g2_ok:
                                                    allowed, gate_reason = False, _g2_reason
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
                                # G2（P0 Batch A）：create 紧前关闭态复核——失败流入既有 not-allowed 分支
                                if allowed:
                                    _g2_ok, _g2_reason = self._final_pre_create_check(
                                        symbol, batch_id, tp_identity, desc='补挂止盈单')
                                    if not _g2_ok:
                                        allowed, gate_reason = False, _g2_reason
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

            # 🔥 P5e（ChatGPT 四复审 P0）：finally 清理前置统一守卫——
            #   1) settled=True → 第一清理入口优先路由共享 finalizer（PnL 落盘门
            #      是 settled 批次清理的唯一守卫；无论成败，后续两段旧清理全部短路）；
            #   2) manual_review 冻结 → 不清理只告警（两段旧清理全部短路）。
            # 否则线程异常退出时，下方 pending_close 段会绕过 PnL 门静默 clear。
            # 🔥 P5f（ChatGPT 五复审 P0-2）：fail-closed 授权 —— 读取异常/冻结态/
            # settled 一律不授权旧清理；settled 由 finalizer 独占（成败都不清）。
            _fin_decision, _fin_snap = self._finally_cleanup_decision(symbol, batch_id)
            if _fin_decision == 'finalizer':
                try:
                    _ok_ff, _msg_ff = self._finalize_limit_full_fill(
                        symbol, batch_id, _fin_snap[3])
                    print(f"  └─ {'✅' if _ok_ff else '⚠️'} [P5 finalizer] "
                          f"finally 接管批次 {batch_id}: {_msg_ff}")
                except Exception as _ffe:
                    print(f"  └─ ⚠️ [P5 finalizer] finally 接管异常: {_ffe}")
            elif _fin_decision == 'skip' and _fin_snap \
                    and _fin_snap[1] == 'limit_cancel_manual_review':
                print(f"  └─ 🧊 [P5] 批次 {batch_id} 人工核对冻结"
                      f"（manual_review），finally 跳过清理")
            elif _fin_decision == 'classify':
                # crash-before-marker：写入冻结态前异常退出——统一分型路由
                # （撤单+FULL_FILL→finalizer / 否则冻结）；无论成败都**不清理**
                _ok_rt, _msg_rt = self._route_zero_position_limit_close(symbol, batch_id)
                print(f"  └─ {'🧊' if _ok_rt else '⚠️'} [P5] 批次 {batch_id} "
                      f"finally 统一分型（限价在途+仓位归零）: {_msg_rt}")
                if not _ok_rt:
                    self.send_tg_notification(
                        f"🚨【资金安全】批次 `{batch_id}` 监控退出时限价平仓分型未完成"
                        f"（{_msg_rt}），已保持冻结不清理，请人工核对。",
                        level='critical')

            # 🔥 S32/A1：程序撤单/正常清理路径兜底撤销限价平仓单（防 clear 时残留孤儿）
            # 🔥 P5h：必须位于清理授权判定之后——分类/冻结/结算路径下，在途限价单
            # 是合法事务的一部分，兜底撤单会中断真实交易（非零持仓崩溃即误撤）
            if _fin_decision == 'allow':
                try:
                    all_states_tmp = self.load_all_states()
                    if all_states_tmp.get(symbol, {}).get(batch_id, {}):
                        self._cancel_limit_close_order(symbol, batch_id)
                except Exception:
                    pass

            # 清理程序撤单标记和批次状态（如果是程序撤单导致的退出）
            if _fin_decision == 'allow':
                try:
                    all_states = self.load_all_states()
                    b_data = all_states.get(symbol, {}).get(batch_id, {})
                    if b_data:
                        # 如果是程序撤单或 pending_close 标记，清理批次
                        if b_data.get('is_programmatic_cancel') or b_data.get('pending_close'):
                            # 🔥 执行前二次校验：授权快照必须与当前账本一致（TOCTOU）
                            if not self._cleanup_authorization_still_valid(
                                    symbol, batch_id, _fin_snap):
                                print(f"  └─ ⚠️ [P5] 批次 {batch_id} 状态已迁移，"
                                      f"finally 清理授权失效（跳过清理）")
                            else:
                                # P0 Batch B：converge 证明后才 clear。v6.2-P0-2：finally 里加
                                # 有限重试（3 次 × 2s）——撤单状态在交易所有传播短窗口，
                                # 单次 UNKNOWN 不应直接把批次留成 close-in-flight。
                                _proof = None
                                for _attempt in range(3):
                                    _proof = self._converge_batch_orders_before_clear(symbol, batch_id)
                                    if _proof is not None:
                                        break
                                    if _attempt < 2:
                                        time.sleep(2)
                                if _proof is not None \
                                        and self.clear_batch_state(
                                            symbol, batch_id, proof=_proof,
                                            authorization=_fin_snap):
                                    print(f"  └─ 🧹 程序撤单，批次状态已清理（proof 收敛通过）")
                                else:
                                    print(f"  └─ ⚠️ [B] 程序撤单批次 {batch_id} 本轮未收敛"
                                          f"（UNKNOWN/撤单失败/授权失效），保留状态待重启恢复重试")
                except Exception as e:
                    print(f"  └─ ⚠️ 清理程序撤单标记失败: {e}")

            # 检查是否有持仓，如果没有则清理批次状态
            # 🔥 v6.4-P3（G1 配套）：生命周期守卫——批次已不在账本（G1/G2/G3 exit 路径）
            # 时，兜底 census 与清理全部无意义 → 直接跳过（零 API 收尾）。
            _fin_all = self.load_all_states()
            _fin_b = _fin_all.get(symbol, {}).get(batch_id, {})
            # 🔥 第二段清理同样二次校验（授权后状态可能已迁移）
            _fin_authorized = (_fin_decision == 'allow'
                               and self._cleanup_authorization_still_valid(
                                   symbol, batch_id, _fin_snap))
            if _fin_decision == 'allow' and not _fin_authorized:
                print(f"  └─ ⚠️ [P5] 批次 {batch_id} 状态已迁移，"
                      f"finally 持仓归零清理授权失效（跳过清理）")
            if _fin_authorized and isinstance(_fin_b, dict) and _fin_b.get('is_active'):
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
                    # 🔥 P5d（ChatGPT 三复审 Blocker 2 端到端）：settled 批次清理的
                    # 唯一守卫是共享 finalizer（PnL 落盘门）——线程异常退出也不得
                    # 经 finally 旁路 converge+clear（否则成交记录永久丢失）
                    if b_data.get('settled_by_limit_close') \
                            and b_data.get('limit_close_order_id'):
                        self._finalize_limit_full_fill(
                            symbol, batch_id, b_data['limit_close_order_id'])
                    elif b_data:
                        # P0 Batch B：converge 证明后才 clear；finally 无循环可重试，
                        # 未收敛则保留状态 + 告警（持仓已归零，仅剩订单面收敛）
                        _proof = self._converge_batch_orders_before_clear(symbol, batch_id)
                        # 🔥 P5g：清理执行边界校验——converge 是交易所 I/O 窗口，
                        # 期间事务可能已推进（settled/manual_review），clear 前必须重校
                        if _proof is not None \
                                and self.clear_batch_state(
                                    symbol, batch_id, proof=_proof,
                                    authorization=_fin_snap):
                            print(f"  └─ 🧹 无持仓，已清理批次状态（proof 收敛通过）")
                        else:
                            print(f"  └─ ⚠️ [B] 无持仓批次 {batch_id} 本轮未收敛"
                                  f"（UNKNOWN/撤单失败/授权失效），保留状态待重启恢复重试")
                elif current_pos is None:
                    print(f"  └─ ⚠️ 持仓查询失败(UNKNOWN)，保留批次状态不清理")
                else:
                    # 🔥 v6.2-P0-2（实盘 2026-09-01 17:4x）：symbol 级聚合持仓不能归属本批次——
                    # 零成交 + 程序撤单收尾的批次自身持仓恒为 0，聚合 > 0 只可能来自其他批次
                    # （实例：f1e135 被新批次 29ca35 的 0.001 卡成 zombie，进而以 close-in-flight
                    # 阻塞同方向 single-flight，挡死真实活仓平仓）。此时继续走 converge/clear 收尾。
                    _zb = (self.load_all_states().get(symbol, {}) or {}).get(batch_id, {}) or {}
                    if (_zb.get('pending_close') or _zb.get('is_programmatic_cancel')) \
                            and int(_zb.get('last_filled_count', 0) or 0) == 0:
                        print(f"  └─ ℹ️ 聚合持仓 {current_pos} 属于其他批次（本批次零成交已撤单），继续收敛清理")
                        _proof = self._converge_batch_orders_before_clear(symbol, batch_id)
                        if _proof is not None and self.clear_batch_state(symbol, batch_id, proof=_proof):
                            print(f"  └─ 🧹 零成交撤单批次已清理（proof 收敛通过）")
                        else:
                            print(f"  └─ ⚠️ [B] 零成交撤单批次 {batch_id} 本轮未收敛，保留状态待重启恢复重试")
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
                    # G2（P0 Batch A）：create 紧前关闭态复核——失败流入既有 not-allowed 分支
                    if allowed:
                        _g2_ok, _g2_reason = self._final_pre_create_check(
                            symbol, batch_id, identity, desc='预生成止损单')
                        if not _g2_ok:
                            allowed, gate_reason = False, _g2_reason
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
                            # G3b（P0 Batch A，G3 集成 #9）：写 CONFIRMED 前持锁复核 close_phase
                            _g3 = self._commit_protection_with_g3(
                                symbol, batch_id, identity, new_sl_order['id'], 'conditional',
                                desc='预生成止损单')
                            if _g3 == 'g3_triggered':
                                # create 已发生但批次已进入平仓/已清理 → G3a 收敛（锁外）
                                self._g3a_converge_race_order(
                                    symbol, batch_id, identity, new_sl_order['id'], 'conditional',
                                    desc='预生成止损单')
                            else:
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
                    # G2（P0 Batch A）：create 紧前关闭态复核——失败流入既有 not-allowed 分支
                    if allowed:
                        _g2_ok, _g2_reason = self._final_pre_create_check(
                            symbol, batch_id, identity, desc='兜底止损单')
                        if not _g2_ok:
                            allowed, gate_reason = False, _g2_reason
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
                            # G3b（P0 Batch A，G3 集成 #10）：写 CONFIRMED 前持锁复核 close_phase
                            _g3 = self._commit_protection_with_g3(
                                symbol, batch_id, identity, new_sl_order['id'], 'conditional',
                                desc='兜底止损单')
                            if _g3 == 'g3_triggered':
                                # create 已发生但批次已进入平仓/已清理 → G3a 收敛（锁外）
                                self._g3a_converge_race_order(
                                    symbol, batch_id, identity, new_sl_order['id'], 'conditional',
                                    desc='兜底止损单')
                            else:
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
                # G2（P0 Batch A）：create 紧前关闭态复核——失败流入既有 not-allowed 分支
                if allowed:
                    _g2_ok, _g2_reason = self._final_pre_create_check(
                        symbol, batch_id, identity, desc='预生成止盈单')
                    if not _g2_ok:
                        allowed, gate_reason = False, _g2_reason
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
                        # G3b（P0 Batch A，G3 集成 #11）：写 CONFIRMED 前持锁复核 close_phase
                        _g3 = self._commit_protection_with_g3(
                            symbol, batch_id, identity, new_tp_order['id'], 'conditional',
                            desc='预生成止盈单')
                        if _g3 == 'g3_triggered':
                            # create 已发生但批次已进入平仓/已清理 → G3a 收敛（锁外）
                            self._g3a_converge_race_order(
                                symbol, batch_id, identity, new_tp_order['id'], 'conditional',
                                desc='预生成止盈单')
                        else:
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

    # ==================== v6.2 helpers（13 个，staged）====================
    def _begin_close_request_if_active(self, symbol: str, batch_id: str,
                                           close_reason: str):
            """原子 BEGIN：取得本次 close transaction 的**唯一所有权**。

            ChatGPT 终审 §二/§三：「CAS 本身写得对，但发生得太晚了……问题发生在
            close 开始阶段」。TG callback 走 `run_in_executor` 起新线程、
            `close_position_market` 入口无 phase==0 检查 → 双击/重复 callback
            可以让两个线程都看到 phase=0、都写 phase=1、都去下单 →
            第二张单可能平到**另一个批次**的仓位。close_op_id CAS 只能在事后
            阻止"谁还能回滚"，阻止不了"两个人都已取得下单资格"。

            本 helper 是完整事务的第一段：
                atomic BEGIN → exchange action → verify → atomic rollback/settle

            语义（全部在 `_state_lock` 内一次性完成，锁内零交易所 API）：
              1. batch 存在且 is_active
              2. close_phase == 0（严格，不允许 1/2/3）
              3. pending_close 为假
              4. 无 settled_by_limit_close 事实（已发生的事实绝不降级）
              5. 🔒 v5 §六：同 symbol + 同方向，**除本批次外**没有任何批次处于
                 close_phase>=1 或 pending_close —— 一次只允许一个自动 close
                 transaction 在途。理由：正在关闭的其他批次（尤其 limit_pending）
                 仓位可能 100% 仍在场，若允许并行，coverage 推理要同时处理多个
                 MARKET/LIMIT close，比"禁止并行"复杂且更易错。
              全部通过 → 生成 uuid → 写 close_phase=1/pending_close=True/
              is_programmatic_cancel=True/close_op_id/close_reason → _persist_states

            返回 (ok, close_op_id, reason, snapshot) 四元组：
              ok=True  → snapshot 是**本次 claim 所依据的 batch 副本**
                         （锁内 dict(b)，与落盘内容逐字段一致）。
                         🔑 v6（ChatGPT 终审 §一）：调用方**必须**以这份快照为
                         唯一基线重算本次 transaction 的全部 batch-derived
                         变量（见 _derive_close_txn_vars）。
              ok=False → snapshot 为 None，调用方**立即返回，绝不发出任何
                         交易所订单**。

            ⚠️ 为什么必须把快照返出来（v6 修正的窗口）：
            生产源码顺序是：入口 L6964-6967 先算 last_filled_count /
            current_filled_amount → fetch ticker → 才 BEGIN（L6980-6984）。
            而监控线程在检测到新成交后会更新 last_filled_count 并
            save_batch_state（L6226 / L6245 / L6255）。于是：
                T1 /close 入口: last_filled_count=1, current=0.001
                监控线程: 新 ENTRY 成交 → last_filled_count=2（落盘）
                T1 BEGIN  : 锁内看到最新 → claim 成功
                T1 下单   : 若仍用旧 current_filled_amount → 只平 0.001 ❌
            claim 与 transaction snapshot 必须绑定，BEGIN 才算完整。

            ⚠️ 为什么 op_id 用 uuid4 而不是毫秒时间戳（ChatGPT §三）：
            时间戳恰恰在"双击并发"这种最需要区分 identity 的场景下可能碰撞；
            trader_260725.py L12 已 `import uuid`，无需新增依赖。
            ⚠️ 为什么 op_id 必须在**这里**生成：v4 把生成放在"执行市价平仓"段
            （L7003 附近），但 close_phase=1 的落盘在 L6983（更早）→ 按文档拼
            起来是 NameError。BEGIN 让"生成 + claim + 落盘"成为同一个原子步骤。
            """
            if not close_reason:
                return False, '', 'missing_close_reason（BEGIN 必须带分型原因）', None
            with self._state_lock:
                try:
                    all_states = self.load_all_states()  # 锁内重读，禁旧快照（G3b 范式）
                except Exception as e:
                    return False, '', f'state_unreadable（{e}）', None
                b = (all_states.get(symbol, {}) or {}).get(batch_id)
                if not isinstance(b, dict):
                    return False, '', 'batch_missing', None
                if not b.get('is_active', True):
                    return False, '', 'batch_inactive', None
                if int(b.get('close_phase', 0) or 0) != 0:
                    return False, '', (f'close_phase_not_zero（disk='
                                       f'{b.get("close_phase")}，已有平仓事务在途）'), None
                if b.get('pending_close'):
                    return False, '', 'pending_close_already_set', None
                if b.get('settled_by_limit_close'):
                    return False, '', 'settled_fact_present（结算事实已发生，绝不重启）', None

                side = b.get('side') or 'BUY'
                for _bid, _bd in (all_states.get(symbol) or {}).items():
                    if _bid == batch_id or not isinstance(_bd, dict):
                        continue
                    if (_bd.get('side') or 'BUY') != side:
                        continue
                    if int(_bd.get('close_phase', 0) or 0) >= 1 or _bd.get('pending_close'):
                        return False, '', (f'same_side_close_inflight（同方向批次 {_bid} '
                                           f'已有平仓事务在途，一次只允许一个）'), None

                op_id = uuid.uuid4().hex
                b['close_phase'] = 1
                b['pending_close'] = True
                b['is_programmatic_cancel'] = True
                b['close_op_id'] = op_id
                b['close_reason'] = close_reason
                # 🔒 v6.2-r4（crash window）：limit_creating 是 transient 态，
                # 正常几秒内 durable commit 为 limit_pending_normal。进程内 seed
                # 冻结告警 grace → monitor poll 撞上 transient 窗口不误报；
                # 进程 crash 后内存 dict 消失 → 重启后磁盘仍 limit_creating
                # 立即 loud（grace 自动失效，恰好只对真崩溃 loud）。
                if not hasattr(self, '_freeze_alerted') or not isinstance(self._freeze_alerted, dict):
                    self._freeze_alerted = {}
                _prev_freeze_alerted = self._freeze_alerted.get(batch_id)
                if close_reason == 'limit_creating':
                    self._freeze_alerted[batch_id] = time.time()
                # 🔒 v6.1（ChatGPT 交叉审核 P0-1）：写盘成功必须成为 claim 成功的
                # 一部分。_persist_states 契约是 -> bool（生产 L1340：账本损坏
                # 主动 return False，写盘异常 return False）。若忽略返回值：
                #   T1 锁内 claim OP1 → 写盘失败 → 函数仍 ok=True → T1 去下 MARKET
                #   磁盘实际仍 phase=0 → T2 重读磁盘再次 claim OP2 → 第二张 MARKET
                # 「未取得唯一所有权者绝不发交易所订单」就此击穿。
                if not self._persist_states(all_states):
                    if close_reason == 'limit_creating':
                        # persist 失败 → 磁盘未变，grace 一并回滚，不留幽灵窗口
                        if _prev_freeze_alerted is None:
                            self._freeze_alerted.pop(batch_id, None)
                        else:
                            self._freeze_alerted[batch_id] = _prev_freeze_alerted
                    return False, '', ('claim_persist_failed（状态写盘失败，'
                                       '视为未取得所有权，绝不发单）'), None
                # 🔑 v6：把刚 claim 的副本交还调用方作为 transaction 唯一基线
                return True, op_id, 'claimed', dict(b)

    def _derive_close_txn_vars(self, snapshot: dict, batch_id: str):
            """从 BEGIN 返回的 claimed snapshot 派生本次 close transaction 的变量。

            🔑 v6（ChatGPT 终审 §一）：atomic BEGIN 的最后半步 —— claim 与
            transaction snapshot 必须绑定。调用方 BEGIN 成功后**必须**立即调用
            本函数，并以返回的 vars 覆盖入口算出的同名局部变量。

            为什么必须整套一起换（不能只换 last_filled_count）：
            生产结算段有 `target_amounts[i] * filled_details[i] for i in
            range(last_filled_count)`。若 last_filled_count 用新值（2）而
            filled_details 仍是旧值（长度 1）→ **IndexError**；反之则平均成本
            与实际层数不符 → PnL 失真。因此下列 10 个字段必须同源。

            🔑 10 个 batch-derived 字段的完整清单（v6 自查补齐）：
            ChatGPT 复审 §一 点名了 4 个，但生产监控线程那次落盘是**一整个
            update 块**（trader_260725.py L6231-6254），一次性写入 8 个字段：
                L6236 entry_orders   L6243 params_base   L6246 filled_details
                L6239 current_sl_id  L6244 is_hedge_mode L6247 total_entry_fee
                L6240 tp_order_id    L6245 last_filled_count
            加上由它们派生的 current_filled_amount、以及入口 L6967 读的 side，
            共 **10 个**。其中 entry_orders / tp_order_id / current_sl_id 三个
            最容易漏：它们不参与"算平多少"，而参与"撤哪些单"。
            漏掉 current_sl_id 的具体事故面：
                /close 入口读 current_sl_id = SL_1
                监控线程滚动止损/保本移 SL → current_sl_id = SL_2 并落盘
                BEGIN claim 的是 SL_2，但调用方若仍撤 SL_1
                → SL_1 早已被监控线程撤掉 → cancel 抛 OrderNotFound
                → `except Exception: pass` 静默吞掉 → **SL_2 成为孤儿单**，
                  随后 clear_batch_state 抹掉批次 → 永久无主。
            这正是本项目一直在猎杀的「孤儿保护单」类型，所以这三个字段
            **必须进入本函数的返回值**（契约完整、可机器校验），而不是
            依赖调用方"恰好把 target_b_data 整个换成 _claimed"。

            返回 (ok, vars, why)：
              ok=True  → vars 为 dict，**exactly 11 个键**（v6.1 措辞修正：
                         10 个 raw snapshot 字段 + 1 个派生量 current_filled_amount）
              ok=False → 账本残缺/类型异常，调用方必须**回滚 BEGIN 并 Fail-Closed**
                         （此时绝不能带着残缺台账去下单）
            why 取值：'snapshot_not_dict' / 'no_filled_amount'
                     / 'ledger_broken（{异常}）' / 'filled_details_short（缺 N 层成交明细）'
                     / 'target_amounts_short（计划层 < 已成交层）'      ← v6.1 P0-2
                     / 'side_invalid（非 BUY/SELL）'                     ← v6.1 P0-2
                     / 'entry_orders_missing（有未成交层但缺失）'        ← v6.1 D6b 收窄
                     / 'entry_orders_short（长度与已成交层/计划层不一致）' ← v6.1 D6b 收窄
                       （自审 F-1：仅 0<len==last_filled_count 的 🗑️ 截断签名放行）

            ⚠️ 本函数只读，不触碰 self、不调交易所、不取锁。
            """
            if not isinstance(snapshot, dict):
                return False, None, 'snapshot_not_dict'
            try:
                last_filled_count = int(snapshot.get('last_filled_count', 0) or 0)
                target_amounts = snapshot.get('target_amounts', []) or []
                filled_details = snapshot.get('filled_details', []) or []
                # v6.4：全量平仓按净仓位执行（partial 后 gross 含已减部分，reduce-only 会被截断）
                current_filled_amount = float(sum(target_amounts[:last_filled_count])) \
                    - float(snapshot.get('realized_reduce_amount', 0.0) or 0.0)
                total_entry_fee = float(snapshot.get('total_entry_fee', 0.0) or 0.0)
            except (TypeError, ValueError) as e:
                return False, None, f'ledger_broken（{e}）'

            if last_filled_count <= 0 or current_filled_amount <= 0:
                return False, None, 'no_filled_amount（claimed 快照显示无需平仓）'
            if len(filled_details) < last_filled_count:
                return False, None, (f'filled_details_short（缺 '
                                     f'{last_filled_count - len(filled_details)} 层成交明细）')
            # 🔒 v6.1（ChatGPT 交叉审核 P0-2）：对称长度校验。Python 切片不因
            # 长度不足报错 —— last_filled_count=2 而 target_amounts=[0.001] 时
            # sum(target_amounts[:2]) 静默得到 0.001（应平 0.002 只派生 0.001）
            # → 少平 → 按单确认这 0.001 完整成交 → ENTRY gate 认为前两层都已
            # 成交 → gate=True → 撤 TP/SL → clear → 实际残留 LONG 0.001。
            # 这正是「少平仓位 → gate 假通过 → 撤保护」，必须 Fail-Closed。
            if len(target_amounts) < last_filled_count:
                return False, None, (f'target_amounts_short（台账计划层 '
                                     f'{len(target_amounts)} < 已成交层 '
                                     f'{last_filled_count}）')
            # 🔒 v6.2（向量完整性门）：filled_details 与 target_amounts 必须等长
            # （正常初始化同长全 0，成交才写价）。长度不等 = 台账不完整
            # （含截断批次重启后被重灌成 len(entry_orders) 的损坏态）
            # → tail 有无成交价无从证明，Fail-Closed。
            if len(filled_details) != len(target_amounts):
                return False, None, (f'filled_details_shape_invalid（成交明细长度 '
                                     f'{len(filled_details)} != 计划层数 '
                                     f'{len(target_amounts)}——台账不完整，'
                                     '无法证明尾段无成交，请人工 reconcile）')

            def _finite_pos_dv(v):
                return (isinstance(v, (int, float)) and not isinstance(v, bool)
                        and v == v and v != float('inf') and v != float('-inf')
                        and v > 0)

            def _finite_zero_dv(v):
                return (isinstance(v, (int, float)) and not isinstance(v, bool)
                        and v == v and v != float('inf') and v != float('-inf')
                        and v == 0)

            # 🔒 v6.2（INV-3a 执行硬门 / prefix integrity）：监控用纯计数保存
            # last_filled_count，重启还会把 filled_layers 前 N 位压平
            # （生产 L4568-4570）—— 内存 bitmap 不可靠，filled_details 是唯一
            # 持久化事实源：prefix 必须全为 finite 正数（成交价），
            # tail 必须全为 exact 0（未成交）。任何其他值 = 台账损坏。
            if not all(_finite_pos_dv(v) for v in filled_details[:last_filled_count]):
                return False, None, ('ledger_invalid（前 '
                                     f'{last_filled_count} 层存在非有限/非正数成交价——'
                                     '台账损坏，请人工核对）')
            if not all(_finite_zero_dv(v) for v in filled_details[last_filled_count:]):
                return False, None, ('entry_fill_hole（成交位不连续：'
                                     f'第 {last_filled_count} 层之后存在成交价或非法值'
                                     '——prefix 假设失效，自动平仓口径不可信，'
                                     '请人工核对持仓与台账）')
            # 🔒 v6.1（P0-2 附带）：side 是平仓方向与 positionSide 的来源，
            # 非法值绝不能默认成 BUY（反向开仓风险）。
            side = snapshot.get('side')
            if side not in ('BUY', 'SELL'):
                return False, None, f'side_invalid（{side!r}，必须是 BUY/SELL）'
            # 🔒 v6.1（ChatGPT 交叉审核：D6b 收窄）：entry_orders 缺失只在
            # 「可证明不存在未成交计划层」时才允许归零（全部层已成交、无单可撤，
            # 此时 pending_ids 本就为空）。否则 missing → [] → pending_ids=[]
            # → ENTRY gate 恒 True —— 又是一个 UNKNOWN → EMPTY。
            # 🔒 v6.1（送审前交叉自审 F-1）：short 校验再收窄——🗑️ 按钮
            # （cancel_open_orders，生产 L6896-6897）只截断 entry_orders 到
            # last_filled_count、不动 target_amounts，len(_eo)==last_filled_count
            # 是生产自己创造的合法状态（未成交层已被有意移除，pending_ids 恒空，
            # gate 无单可撤自然通过），必须放行。只拦两种真残缺：
            #   ① len(_eo) < last_filled_count（已成交层 ID 都丢失 = 账本损坏）
            #   ② last_filled_count < len(_eo) < len(target_amounts)（部分截断，
            #      无任何生产路径产生 = 可疑中间态）
            # 另：len(_eo)==last_filled_count==0 也拦（0<len 条件不满足）——
            # 「一层未成交且 ID 全空」与 🗑️ 签名形似但无生产来源，Fail-Closed。
            if len(target_amounts) > last_filled_count:
                _eo = snapshot.get('entry_orders')
                if not isinstance(_eo, list):
                    return False, None, ('entry_orders_missing（存在 '
                                         f'{len(target_amounts) - last_filled_count} '
                                         '个未成交计划层，但 entry_orders 缺失/非列表）')
                if len(_eo) < len(target_amounts) and not (0 < len(_eo) == last_filled_count):
                    return False, None, (f'entry_orders_short（entry_orders 长度 '
                                         f'{len(_eo)} 与已成交层数 {last_filled_count} /'
                                         f' 计划层数 {len(target_amounts)} 不一致，'
                                         '未成交层无法逐 ID 归因）')

            return True, {
                'last_filled_count': last_filled_count,
                'target_amounts': target_amounts,
                'current_filled_amount': current_filled_amount,
                'filled_details': filled_details,
                'total_entry_fee': total_entry_fee,
                'side': side,  # v6.1：上方已严格校验 ∈ {BUY, SELL}
                'params_base': snapshot.get('params_base') or {},
                'is_hedge_mode': bool(snapshot.get('is_hedge_mode', False)),
                # ── v6 自查补齐：参与「撤哪些单」的三个字段 ──────────────
                # 不进本函数就会退化成「靠调用方恰好把 target_b_data 整个换成
                # _claimed」——正确但不可校验。见上方 docstring 的 SL_2 孤儿链。
                'entry_orders': snapshot.get('entry_orders') or [],
                'tp_order_id': snapshot.get('tp_order_id'),
                'current_sl_id': snapshot.get('current_sl_id'),
            }, 'ok'

    def _rollback_close_request_if_current(self, symbol: str, batch_id: str,
                                               close_op_id: str):
            """受控逆向迁移的唯一入口：原子回滚本次 close 请求的临时状态。

            范式复用 trader_260725.py::_commit_protection_with_g3（L3464，G3b）：
              持 _state_lock → 锁内 load_all_states() 重读最新磁盘（禁旧快照，
              消灭 TOCTOU）→ 同一锁段内判定 + 修改 + _persist_states。

            回滚资格（全部满足才执行，任一不满足拒绝）：
              1. batch 仍存在
              2. disk.close_op_id == 我这次的 close_op_id   ← 操作身份，证明
                 "这是我的那一个 1"，不是别人正在推进的流程
              3. disk.close_phase 仍为 1                    ← 没有别的线程推进过
              4. 无 settled_by_limit_close 事实             ← 已发生的事实绝不降级

            只改三个字段：close_phase=0 / pending_close=False / is_programmatic_cancel=False。
            （close_op_id/close_reason 保留作为取证痕迹，供人工核对。）

            边界（G3b 契约）：_state_lock 非重入 → 锁内禁止调 save_batch_state /
            _update_registry（内部再取锁会死锁），直接操作 dict + _persist_states；
            锁内零交易所 API。

            返回 (ok: bool, reason: str)。
            """
            with self._state_lock:
                try:
                    all_states = self.load_all_states()  # 硬约束：锁内重读，禁旧快照
                except Exception as e:
                    return False, f'state_unreadable（{e}）'
                b = (all_states.get(symbol, {}) or {}).get(batch_id)
                if b is None:
                    return False, 'batch_missing'
                disk_op_id = b.get('close_op_id') or ''
                if disk_op_id != (close_op_id or ''):
                    return False, (f'op_id_mismatch（disk={disk_op_id!r} ≠ '
                                   f'mine={close_op_id!r}，已有其他操作接管）')
                if int(b.get('close_phase', 0) or 0) != 1:
                    return False, 'phase_changed（close_phase 已被推进，非本次请求）'
                if b.get('settled_by_limit_close'):
                    return False, 'settled_fact_present（结算事实已发生，绝不降级）'
                b['close_phase'] = 0
                b['pending_close'] = False
                b['is_programmatic_cancel'] = False
                # 🔒 v6.1（P0-1 同型）：写盘失败绝不能报告「已回滚」——否则 TG
                # 告诉用户「监控恢复了」，磁盘却仍是 close_phase=1（监控冻结）。
                if not self._persist_states(all_states):
                    return False, ('rollback_persist_failed（回滚写盘失败，'
                                   '磁盘仍为 close_phase=1）')
                return True, 'rolled_back'

    def _set_close_reason_if_current(self, symbol: str, batch_id: str,
                                         close_op_id: str, reason: str):
            """把 close_reason 切换为异常态的 CAS 写入（ENTRY gate 失败等场景）。

            🔒 v6.1（ChatGPT 交叉审核 R1-§六）：市价 ENTRY gate 失败时若只发
            critical 而不更新 close_reason，批次将永远停留在 BEGIN 写入的
            'market_confirming' → 冻结监控（改动 4 的分型白名单）只 print、
            不再周期 critical —— 与已批准的「异常冻结 fail-noisy」直接矛盾。

            CAS 范围与 BEGIN / rollback 同原则：只有本批次仍属于**本次**事务
            （close_op_id 匹配 + close_phase>=1）时才写入，绝不覆盖别人已推进
            的状态。写盘失败返回 False（P0-1：持久化结果必须显式）。

            返回 (ok, why)。why ∈ 'reason_set' / 'missing_reason' /
            'batch_missing' / 'op_id_mismatch' / 'not_in_close' /
            'state_unreadable' / 'persist_failed'。
            """
            if not reason:
                return False, 'missing_reason'
            with self._state_lock:
                try:
                    all_states = self.load_all_states()  # 锁内重读，禁旧快照
                except Exception as e:
                    return False, f'state_unreadable（{e}）'
                b = (all_states.get(symbol, {}) or {}).get(batch_id)
                if not isinstance(b, dict):
                    return False, 'batch_missing'
                if (b.get('close_op_id') or '') != (close_op_id or ''):
                    return False, 'op_id_mismatch（已有其他操作接管，不覆盖）'
                if int(b.get('close_phase', 0) or 0) < 1:
                    return False, 'not_in_close（无在途平仓事务）'
                # 🔒 v6.2-r4（P0-2）：first-abnormal-wins——第一个真正的 failure
                # reason 赢。REASON_TRANSITION_SOURCES（normal + limit_creating
                # transient）之外的 reason 已是异常根因，generic except 的
                # settlement_error 不得覆盖第一现场。返回 True：磁盘已 abnormal，
                # 本次切换目标已达成。（授权看 state/组，解释看 reason。）
                transition_sources = ('market_confirming', 'limit_pending_normal',
                                      'limit_creating',
                                      'partial_closing', 'partial_resize_pending')  # v6.4：成对注册
                cur_reason = b.get('close_reason')
                if cur_reason not in transition_sources:
                    return True, f'reason_already_abnormal（{cur_reason}）'
                b['close_reason'] = reason
                if not self._persist_states(all_states):
                    return False, 'persist_failed（reason 写盘失败）'
                return True, 'reason_set'

    def _read_position_amt(self, symbol: str, side: str, is_hedge_mode: bool) -> float | None:
            """读取【symbol + 持仓方向】的持仓绝对值。

            返回 None = 查询失败（不可判定）→ 调用方必须 Fail-Closed。
            返回 0.0  = 该方向无敞口。

            ⚠️ 读的是 symbol+方向【总敞口】，不是本批次敞口（D-006 同方向最多 3 批）。
              禁止单独用作放行判据——2026-08-29 探针实证（G:/tmp/probe_position_shape.py）：
              side 传错同样返回 0.0，与「已平仓」物理不可区分。

            v5（ChatGPT 终审 §八-1）：`positions` **非 list 一律返回 None**。
            v4 的 `for pos in positions if isinstance(positions, list) else []`
            对 dict/tuple/异常结构返回 total=0.0，仍是 UNKNOWN→ZERO 的同型退化
            （虽然多数情况下导致"不发单"，但按项目纪律必须严格 Fail-Closed）。
            """
            try:
                positions = self._safe_api_call(self.exchange.fetch_positions, [symbol])
            except Exception as e:
                print(f"  ⚠️ 读取持仓失败: {e}")
                return None
            if positions is None:
                # 非异常的 None 返回同样不可判定，绝不能退化成 0.0（C-1 同型漏洞）
                print("  ⚠️ 读取持仓失败：fetch_positions 返回 None（非异常）")
                return None
            if not isinstance(positions, list):
                # v5：非 list 结构（dict/tuple/异常载荷）同样不可判定
                print(f"  ⚠️ 读取持仓失败：fetch_positions 返回非列表结构"
                      f"（{type(positions).__name__}），不可判定")
                return None
            target = 'long' if side == 'BUY' else 'short'
            want_raw = symbol.replace('/', '').split(':')[0]
            total = 0.0
            for pos in positions:
                info = pos.get('info', {}) or {}
                if pos.get('symbol') != symbol and info.get('symbol') != want_raw:
                    continue
                if is_hedge_mode:
                    ps = str(pos.get('side') or info.get('positionSide') or '').lower()
                    if ps not in (target, 'both'):
                        continue
                try:
                    _v = abs(float(pos.get('contracts', 0) or pos.get('positionAmt', 0) or 0))
                except (TypeError, ValueError):
                    return None
                # 🔒 v6.2-r4（P0）：NaN/Inf = 不可判定，绝不能当数值参与 coverage
                # 比较（NaN 与任何数比较全 False → UNKNOWN→PASS fail-open）。
                if _v != _v or _v == float('inf') or _v == float('-inf'):
                    print(f"  ⚠️ 读取持仓失败：contracts 值非有限数（{_v}），不可判定")
                    return None
                total += _v
            return total

    def _fetch_close_order_state(self, order_id, symbol, retry_not_found: int = 3,
                                     not_found_delay: float = 2.0, order_kind: str = 'normal'):
            """按单查询平仓单，返回 (state, order)。state ∈ {'success','not_found','unknown'}。

            复用 trader_260725.py::_verify_order_created（L3368）的既有三态语义：
              success   → 订单真实存在，order 可用
              not_found → OrderNotFound（重试排除可见性延迟后仍不存在）
              unknown   → 其他异常 → 调用方必须 Fail-Closed（UNKNOWN ≠ EMPTY）

            平仓单走 'normal' 端点（不带 params={'stop': True}）。
            not_found 必须重试后再定案（2026-08-29 事件 3 实证：4/4 单 0 秒 verify
            全部 OrderNotFound 假阴性 → 曾致 12 处误判 24 个孤儿单）。
            """
            # 🔒 v6.2-r4（P0 端点路由）：order_kind 参数化（对齐生产
            # _verify_order_created L3377 的既有范式）。'normal' = 普通限价/市价单
            # （默认，MARKET/LIMIT 现状零变化，且保持原调用形态——不显式传 params）；
            # 'conditional' = STOP/TAKE_PROFIT 条件单，fetch_order 必须带
            # params={'stop': True} 走 algo 端点，否则恒 not_found 假阴性
            # （C5 事故根因：12 处误判 24 孤儿单）。
            try:
                if order_kind == 'conditional':
                    order = self._safe_api_call(self.exchange.fetch_order, order_id, symbol,
                                                params={'stop': True}, retries=1)
                else:
                    order = self._safe_api_call(self.exchange.fetch_order, order_id, symbol, retries=1)
                return 'success', order
            except ccxt.OrderNotFound:
                for _ in range(retry_not_found):
                    time.sleep(not_found_delay)
                    try:
                        if order_kind == 'conditional':
                            order = self._safe_api_call(
                                self.exchange.fetch_order, order_id, symbol,
                                params={'stop': True}, retries=1)
                        else:
                            order = self._safe_api_call(
                                self.exchange.fetch_order, order_id, symbol, retries=1)
                        return 'success', order
                    except ccxt.OrderNotFound:
                        continue
                    except Exception:
                        return 'unknown', None
                return 'not_found', None
            except Exception:
                return 'unknown', None

    def _confirm_close_filled(self, symbol: str, side: str, is_hedge_mode: bool,
                                  order_id, expected: float, pos_before: float | None = None,
                                  attempts: int = 3, delay: float = 0.6,
                                  order_kind: str = 'normal'):
            """确认【这张平仓单】的成交事实。返回 (verdict, detail, filled_amount)。

            verdict（六态，ChatGPT 终审 §一 批准方向 + §五 收紧 TERMINAL_ZERO）：
              'CONFIRMED_FULL'  → 完整成交 → 放行撤 SL/TP
              'TERMINAL_ZERO'   → **唯一有资格回滚**的状态。v5 收紧后只可能是：
                                  status ∈ (canceled, expired, rejected)
                                  + 权威 filled 字段**明确存在**且 == 0。
                                  v4 的 `filled = float(order.get('filled') or 0)`
                                  会把 filled 缺失/None 变成 0 → 又是一个小型
                                  UNKNOWN→ZERO（ChatGPT 终审 §五）；且
                                  closed/filled + filled==0 本身是矛盾组合，
                                  v5 一律判 UNKNOWN，不给回滚资格。
              'PARTIAL'         → filled > 0 但不足 → **绝不回滚**（仓位已真实变化，
                                  回滚=把"已部分平掉"伪装成"未平过"）→ 保持保护单
                                  + critical 人工接管
              'PENDING'         → new/open/active → **绝不回滚**（订单活着，稍后可能
                                  成交；回滚后订单再成交，状态机已回 ACTIVE 却无人管）
              'UNKNOWN'         → 查询异常 / 字段不可判定 → 不回滚 + critical
              'NOT_CONFIRMED'   → create 已返回 ID 但 fetch 查不到（重试后仍 OrderNotFound）
                                  → **绝不回滚**。复用 _verify_order_created 的语义：
                                  not_found = NOT_CONFIRMED（不 Commit），绝不是
                                  "证明订单没成交可以放心反向操作"。

            核心不变量：只要 create_order() 成功返回了有效 order_id，
            close_order_placed=True 就不再改回 False。回滚不再操作这个标志，
            而是通过 _rollback_close_request_if_current 的 close_op_id CAS。

            判据为什么必须是「订单维度」：v2 的 delta（总敞口减少量）无法归因——
            另一批次 SL 成交 / 用户手动平仓 / ADL 都会让总敞口下降 → 假确认 → 裸仓。
            fetch_order 回答「我这张单成交了多少」，天然免疫他方行为。
            delta 已降级为 CONFIRMED_FULL 后的二级交叉校验（仅告警不阻断）。
            """
            if expected is None or expected <= 0:
                return 'UNKNOWN', f"参数不可判定（expected={expected}）", None

            # B-03：有效预期 = min(台账量, pos_before)。台账量可能大于实际剩余
            # （上次部分成交未同步 / 用户手动减仓），若直接用台账量判，仓位真实
            # 归零也永远判不通过 → 永久不可平。
            if pos_before is not None and pos_before > 0:
                eff_expected = min(expected, pos_before)
            else:
                eff_expected = expected
            tol = 1e-8 + abs(eff_expected) * 1e-6
            zero_tol = 1e-12

            n = max(1, attempts)
            last_detail = ''
            for i in range(n):
                state, order = self._fetch_close_order_state(order_id, symbol,
                                                             order_kind=order_kind)

                if state == 'success':
                    if not isinstance(order, dict):
                        return 'UNKNOWN', f"订单结构异常（{type(order).__name__}）", None
                    status = str(order.get('status') or '').lower()

                    # ── v5（§五）：权威 filled 必须明确存在，否则 UNKNOWN
                    if 'filled' not in order or order.get('filled') is None:
                        return 'UNKNOWN', (f"订单 {order_id} 的 filled 字段缺失"
                                           f"（status={status!r}）——无法证明零成交，"
                                           f"无回滚资格"), None
                    try:
                        filled = float(order['filled'])
                    except (TypeError, ValueError):
                        return 'UNKNOWN', (f"订单 {order_id} 的 filled 字段不可解析"
                                           f"（{order.get('filled')!r}）——无回滚资格"), None

                    if status in ('closed', 'filled'):
                        if filled >= eff_expected - tol:
                            detail = (f"订单 {order_id} 已成交 filled={filled}"
                                      f"（有效预期 {eff_expected}，台账 {expected}），status={status}")
                            # 二级交叉校验（B-01 处置 2）：按单已确认成交，再看敞口是否
                            # 真的相应减少。仅告警不阻断——多批次下其他批次的减仓会让
                            # 这里出现正常的不匹配，阻断会把正常路径卡死。
                            if pos_before is not None:
                                after = self._read_position_amt(symbol, side, is_hedge_mode)
                                if after is not None and (pos_before - after) < eff_expected - tol:
                                    print(f"  ⚠️ [交叉校验] 订单已成交但敞口未见相应减少："
                                          f"before={pos_before} after={after} "
                                          f"预期减少>={eff_expected}（多批次下可能正常，请人工留意）")
                            return 'CONFIRMED_FULL', detail, filled
                        if filled > zero_tol:
                            # 部分成交：仓位已真实变化，绝不回滚
                            return 'PARTIAL', (
                                f"订单 {order_id} 部分成交 filled={filled} < 预期 {eff_expected}"
                                f"（status={status}）。仓位已变化，不可回滚；"
                                f"保持保护单，需人工接管剩余 {eff_expected - filled}"), filled
                        # v5（§五）：closed/filled 却 zero filled = 矛盾组合 → UNKNOWN
                        return 'UNKNOWN', (
                            f"订单 {order_id} 状态为 {status} 但权威 filled=0（矛盾/异常组合，"
                            f"预期 {eff_expected}）——按 UNKNOWN 处理，无回滚资格，"
                            f"转人工核对"), None

                    if status in ('canceled', 'expired', 'rejected'):
                        if filled > zero_tol:
                            return 'PARTIAL', (
                                f"订单 {order_id} 终态 {status} 但已成交 filled={filled} > 0"
                                f"（预期 {eff_expected}）。仓位已变化，不可回滚"), filled
                        # v5：唯一可回滚门 —— 权威 filled 明确存在且 == 0
                        return 'TERMINAL_ZERO', (
                            f"订单 {order_id} 终态 {status} 且权威 filled=0"
                            f"（预期 {eff_expected}）——唯一可回滚状态"), 0.0

                    if status in ('new', 'open', 'active', 'pending', 'partially_filled'):
                        last_detail = (f"订单 {order_id} 仍在活动中：status={status} "
                                       f"filled={filled}（预期 {eff_expected}）")
                        if i < n - 1:
                            time.sleep(delay)
                            continue
                        # 订单活着 → 绝不回滚（回滚后它再成交就无人管辖）
                        return 'PENDING', last_detail, filled if filled > zero_tol else None

                    # 未知 status 字符串：不猜，按 UNKNOWN
                    return 'UNKNOWN', f"订单 {order_id} 状态不可识别：status={status!r}", None

                elif state == 'not_found':
                    # create 已返回 ID → fetch 查不到 ≠ 没成交。
                    # _verify_order_created 的 not_found 语义是 NOT_CONFIRMED（不 Commit），
                    # 不是"证明订单不存在可以反向操作"。
                    return 'NOT_CONFIRMED', (
                        f"订单 {order_id} create 返回了 ID，但 fetch_order 重试后仍查不到"
                        f"（NOT_CONFIRMED，绝不回滚）"), None
                else:  # unknown
                    last_detail = f"查询订单 {order_id} 失败（结果未知）"
                    if i < n - 1:
                        time.sleep(delay)
                        continue
                    return 'UNKNOWN', f"查询订单 {order_id} 连续 {n} 次失败，结果未知", None

            return 'UNKNOWN', (last_detail or f"订单 {order_id} 确认流程异常结束"), None

    def _survey_same_side_batches(self, symbol: str, side: str,
                                      target_batch_id: str):
            """勘察同 symbol + 同方向的批次分布。

            返回 (others_count, sum_all, blocking_count)：
              others_count    = 除 target 外，同方向且台账>0 的其他批次数
              sum_all         = **含 target 在内**的同方向批次台账合计（coverage 需
                                要覆盖"仍需保留的 tracked exposure"，见下）
              blocking_count  = 除 target 外，已进入平仓流程（close_phase>=1 或
                                pending_close）的其他批次数
            返回 (-1, -1, -1) = 无法判定 → 调用方必须 Fail-Closed。

            🔑 v5 修正（ChatGPT 终审 §一）：v4 在这里 `continue` 掉所有
            close_phase>=1 / pending_close 的批次，**包括 target 自己**。
            但真实调用顺序是：BEGIN 先写 close_phase=1（落盘）→ 才调
            _close_amount_guard → _survey_same_side_batches。于是 target 被自己
            的过滤条件排除，决定性例子重跑一遍会再次放行：

                A(target) 0.001 [close_phase=1 → 被排除]
                B         0.001
                actual    0.001
                → v4: sum_all=0.001, actual=0.001 → actual < sum_all 为 False → 放行 ❌
                → v5: sum_all=0.002, actual=0.001 → Fail-Closed ✅

            为什么 sum_all 必须含 target（coverage 不变量的推导，ChatGPT 原文批准）：
                actual >= sum_tracked
                平掉 L_target 后： actual - L_target >= sum_tracked - L_target
                → 剩余所有 tracked batches 仍有足够实际仓位覆盖。
            若 sum_tracked 不含 target，这个推导不成立。

            §六：其他批次处于 close_phase>=1 时，其仓位可能 100% 仍在场
            （limit_pending_normal 可挂数小时）→ 从 coverage 角度仍需占用储备。
            v5 把它们计入 sum_all（保守）并单独暴露 blocking_count；BEGIN 的
            同方向单飞检查已让这种情况理论不可达，若仍出现说明并发窗口或人工
            改过状态 → 调用方按 Fail-Closed 处理。
            """
            try:
                all_states = self.load_all_states()
            except Exception as e:
                print(f"  ⚠️ 勘察同方向批次失败（无法判定归因）: {e}")
                return -1, -1, -1
            batches = all_states.get(symbol, {}) or {}
            others = 0
            blocking = 0
            sum_all = 0.0

            def _topology_ok(amounts, details, n):
                # 🔒 v6.2-r5：shape + finite-positive prefix + exact-zero tail。
                # 局部闭包实现，刻意不提取成模块级符号（不新增 helper）。
                if not isinstance(amounts, list) or not isinstance(details, list):
                    return False
                if n < 0 or n > len(amounts) or len(details) != len(amounts):
                    return False

                def _finite_pos(v):
                    return (isinstance(v, (int, float)) and not isinstance(v, bool)
                            and v == v and v != float('inf') and v != float('-inf')
                            and v > 0)

                def _finite_zero(v):
                    return (isinstance(v, (int, float)) and not isinstance(v, bool)
                            and v == v and v != float('inf') and v != float('-inf')
                            and v == 0)

                return (all(_finite_pos(v) for v in details[:n])
                        and all(_finite_zero(v) for v in details[n:]))
            for bid, b in batches.items():
                if not isinstance(b, dict):
                    continue
                if b.get('side', 'BUY') != side:
                    continue
                # 🔒 v6.2-r5（P0）：coverage 证明依赖所有同方向台账可信。
                # 对每个纳入 coverage 的批次（含 target）先验证 fill topology
                # （与 _derive_close_txn_vars 硬门同一套判据），再求和。
                try:
                    _n = int(b.get('last_filled_count', 0) or 0)
                except (TypeError, ValueError):
                    return -1, -1, -1
                _ta = b.get('target_amounts') or []
                _fd = b.get('filled_details') or []
                if not _topology_ok(_ta, _fd, _n):
                    print(f"  ⚠️ 勘察中止：批次 {bid} 成交位拓扑损坏"
                          f"（lfc={_n}，amounts={len(_ta)}，details={len(_fd)}），"
                          f"coverage 不可证明（Fail-Closed，人工 reconcile）")
                    return -1, -1, -1
                try:
                    # v6.4：coverage 按净仓位计算（partial 后 gross 高估 tracked → 误判 conflict）
                    filled = max(0.0, float(sum(_ta[:_n]))
                                 - float(b.get('realized_reduce_amount', 0.0) or 0.0))
                except (TypeError, ValueError):
                    # 🔒 v6.2-r6（R3-h3）：其他批次 target_amounts 含字符串等
                    # 非法值 → 按 helper 契约返回不可判定，而不是向上抛异常
                    print(f"  ⚠️ 勘察中止：批次 {bid} target_amounts 含非法值，"
                          f"coverage 不可证明（Fail-Closed，人工 reconcile）")
                    return -1, -1, -1
                if filled != filled or filled == float('inf') or filled == float('-inf'):
                    print(f"  ⚠️ 勘察中止：批次 {bid} 计划量合计非有限数（{filled}），"
                          f"不可判定（Fail-Closed）")
                    return -1, -1, -1
                if _n > 0 and filled <= 0:
                    # 声明 lfc>0 却算出 <=0 = 账本矛盾，coverage 不可证明
                    # （旧代码 continue 会静默跳过 → 假 coverage）
                    print(f"  ⚠️ 勘察中止：批次 {bid} 声明 {_n} 层成交但计划量合计 "
                          f"{filled} <= 0，账本矛盾（Fail-Closed，人工 reconcile）")
                    return -1, -1, -1
                if filled <= 0:
                    # lfc==0 的合法零敞口批次：无 coverage 占用，跳过
                    continue
                # v5：target 与任何 close_phase 的批次都计入 coverage
                sum_all += filled
                if bid == target_batch_id:
                    continue
                if int(b.get('close_phase', 0) or 0) >= 1 or b.get('pending_close'):
                    blocking += 1
                others += 1
            return others, sum_all, blocking

    def _close_amount_guard(self, symbol: str, side: str, is_hedge_mode: bool,
                                ledger_amount: float, batch_id: str):
            """下单数量守卫（v5：coverage 不变量，ChatGPT 终审 §一 批准 + §六）。

            规则：
              读取失败 / 勘察失败 → None（Fail-Closed 不发单，B-09 已获批准）
              blocking_count > 0  → None（同方向另有在途平仓事务，理论不可达；
                                    若发生说明并发窗口或人工改过状态 → Fail-Closed）
              单批次方向（others == 0）：
                actual >= ledger → 按台账平
                actual <  ledger → 按实测平（min 的合法域：归因唯一成立，B-03）
              多批次方向：
                actual < 台账合计（**含本批**） → 归因冲突，禁止自动平（Fail-Closed）
                actual >= 台账合计 → 按台账平

            返回 (amount, detail)。amount=None → 调用方必须 Fail-Closed 不发单。
            """
            # 🔒 v6.2-r4（P0）：三个量全部必须为有限非负数，否则 Fail-Closed
            # （NaN 会让下方所有比较变 False → UNKNOWN→PASS）。
            def _finite_nonneg(v):
                return (isinstance(v, (int, float)) and not isinstance(v, bool)
                        and v == v and v != float('inf') and v != float('-inf')
                        and v >= 0)

            if not _finite_nonneg(ledger_amount) or ledger_amount <= 0:
                return None, (f"台账量非法（{ledger_amount}），无法确定平仓数量"
                              f"（Fail-Closed，不发单）")
            actual = self._read_position_amt(symbol, side, is_hedge_mode)
            if actual is None:
                return None, "读取实际持仓失败，无法确定平仓数量（Fail-Closed，不发单）"
            if not _finite_nonneg(actual):
                return None, (f"实际持仓值非有限数（{actual}），不可判定"
                              f"（Fail-Closed，不发单）")
            tol = 1e-8 + abs(ledger_amount) * 1e-6

            others, sum_all, blocking = self._survey_same_side_batches(symbol, side, batch_id)
            if others < 0:
                return None, "同方向批次勘察失败，归因不可判定（Fail-Closed，不发单）"
            if not _finite_nonneg(sum_all):
                return None, "同方向台账合计非有限数（Fail-Closed，不发单）"
            if blocking > 0:
                return None, (f"同方向另有 {blocking} 个批次正处于平仓流程中，"
                              f"其实际仓位占用不可判定（BEGIN 应已拒绝，出现即说明并发窗口"
                              f"或人工改过状态）→ 禁止自动平仓（Fail-Closed，人工 reconcile）")

            if others == 0:
                # 单批次：归因唯一，min 是合法域
                if actual >= ledger_amount - tol:
                    if actual <= 0 and ledger_amount <= 0:
                        return 0.0, "台账与实测敞口均为 0，无需平仓"
                    return ledger_amount, f"单批次方向，总敞口 {actual} ≥ 台账 {ledger_amount}，按台账量平仓"
                if actual <= 0:
                    return 0.0, f"实际敞口为 0（台账 {ledger_amount}），无需平仓"
                return actual, (f"单批次方向，台账 {ledger_amount} > 实测 {actual}，"
                                f"归因唯一成立，按实测 {actual} 平仓")

            # 多批次：总敞口 vs 台账合计（含本批）
            if actual < sum_all - tol:
                return None, (f"归因冲突：总敞口 {actual} < 同方向批次台账合计 {sum_all}"
                              f"（本批 {ledger_amount} + 其他 {others} 批）——账本与交易所已漂移，"
                              f"总量数据不能证明 batch 归属，禁止自动平仓"
                              f"（Fail-Closed，critical + 人工 reconcile）")
            if actual > sum_all + tol:
                print(f"  ⚠️ [归因] 总敞口 {actual} > 台账合计 {sum_all}：存在未跟踪敞口，"
                      f"平本批台账量不会侵占其他批次，但请人工留意多余敞口的来源")
            return ledger_amount, (f"多批次方向但台账合计 {sum_all} ≤ 总敞口 {actual}，"
                                   f"归属成立，按台账量 {ledger_amount} 平仓")

    def _verify_entry_order_terminal(self, order_id, symbol: str,
                                         attempts: int = 3, delay: float = 0.8):
            """逐 ID 确认单个 ENTRY 挂单已消失（事务事实按 ID 归因，与平仓确认同原则）。

            返回 verdict ∈ {'gone','filled','open','unknown'}：
              gone    → canceled/expired/rejected（**交易所明确返回终态对象**）
              filled  → ENTRY 在等待期间成交了 → 仓位已变化，必须中断放行流程
              open    → 仍然活着
              unknown → 查询失败 / OrderNotFound，不可判定

            🔒 v6（ChatGPT 终审 §二 小点）：OrderNotFound 从 gone 改为 unknown。
            G3a 的「-2011/Unknown order = 已收敛」只对**撤销**这个目标成立
            （目标 = 这张单不再挂着）。而本 helper 服务于 ENTRY gate，需要证明的
            是「这张 ENTRY 没有成交」—— 生产 L1992 的既有认知明确写着：
                # 订单确实不存在（已撤销/已成交/已过期）→ 安全清除
            「已成交」就在其中。OrderNotFound 能证明「不用再 cancel 了」，
            证明不了「它没有成交」。三种可能里只有一种是安全的 → Fail-Closed。

            ✅ 正常路径不受影响：生产 L4151 实证「自愈 fetch 已撤销订单返回
            status=canceled 对象（不抛 OrderNotFound）」→ 正常撤单后 fetch 会
            拿到 canceled，仍走 gone。只有真正查不到的异常路径才 Fail-Closed。
            """
            for i in range(max(1, attempts)):
                try:
                    order = self._safe_api_call(
                        self.exchange.fetch_order, order_id, symbol,
                        params={'stop': True}, retries=1)
                except ccxt.OrderNotFound:
                    # 🔒 v6：不存在 ≠ 未成交（可能已成交）→ Fail-Closed
                    return 'unknown', None
                except Exception:
                    return 'unknown', None
                if order is None:
                    # _safe_api_call 静默失败（限流/网络）→ 未知，绝不当成"已消失"
                    if i < attempts - 1:
                        time.sleep(delay)
                        continue
                    return 'unknown', None
                status = str((order or {}).get('status') or '').lower()
                if status in ('canceled', 'expired', 'rejected'):
                    return 'gone', order
                if status in ('closed', 'filled'):
                    return 'filled', order
                if i < attempts - 1:
                    time.sleep(delay)
                    continue
                return 'open' if status else 'unknown', order
            return 'unknown', None

    def _cancel_and_verify_entry_orders(self, symbol: str, batch_id: str,
                                            b_data: dict, last_filled_count: int) -> bool:
            """平仓成功后撤未成交 ENTRY 并做交易所侧验证。

            ⚠️ v5 契约（ChatGPT 终审 §四）：**返回值必须被调用方当作 clear gate**。
            返回 False 时调用方必须 raise，绝不继续进入
            `_converge_batch_orders_before_clear()` / `clear_batch_state()`。
            否则最坏链是：
                helper 正确识别 UNKNOWN → return False
                → 调用方忽略 → legacy converge 的 `fetch_open_orders(...) or []`
                  把 None 变成 [] → EMPTY → 生成 proof → clear
            等于"前门修好、后门又放回来"。

            🚨 v6 调用顺序契约（ChatGPT 终审 §二）：本 helper 必须在**撤销 SL/TP
            之前**完成。市价路径正确次序：
                MARKET 按单 CONFIRMED_FULL
                → 撤未成交 ENTRY（本 helper）
                → 逐 ID 确认 ENTRY 全部安全终结（本 helper）
                → 只有 gate=True 才撤 TP / SL
                → 结算 / converge / clear
            v5 把它放在撤 TP→撤 SL 之后，形成这条事故链：
                MARKET 平掉 0.001 → 未撤的 ENTRY 恰好成交 0.001 → 又产生
                LONG 0.001 → 先撤 TP → 先撤 SL → 才 verify ENTRY 发现成交
                → raise → 批次冻结，但仓位已无 SL/TP 保护（裸仓）。
            gate 在前时，同一场景的后果是：批次冻结 + **SL/TP 仍在位** + critical。

            双缺陷修复（ChatGPT 终审 §三）：
              1. `fetch_open_orders(...) or []` —— 与 C-1 完全同型的假确认：
                 None → [] → remaining_ids 空 → still_alive 空 → ✅"全部清零"。
                 实际查询根本没给出有效结果。UNKNOWN → EMPTY，而本 helper 的安全
                 意义恰恰是"证明 ENTRY 不会重新开仓"。
              2. 只用 open_orders 快照判清零，违反项目"事务事实按 ID 归因"原则
                 （L3371：Verify 必须用 fetch_order）。
            """
            entry_orders = b_data.get('entry_orders', []) or []
            if (len(b_data.get('target_amounts', []) or []) > last_filled_count
                    and len(entry_orders) == last_filled_count):
                # 🔒 v6.2（P0 修正 / D-1 裁定）：legacy 截断形状 → registry 链恢复
                # （不靠 open-orders 快照：快照看不见已成交的遗失 ENTRY）。
                # symbol-wide orphan scan 已删除（同 symbol 其他合法批次会被误判）。
                rec_ids, recoverable, _chain = self._pending_entry_ids_for_gate(
                    symbol, batch_id, b_data, last_filled_count)
                if not recoverable:
                    self.send_tg_notification(
                        f"🚨【资金安全】截断台账无法与 protection_registry 对上链！\n"
                        f"🆔 批次: {batch_id}\n"
                        f"⚠️ registry ENTRY 链与 target_amounts/entry_orders 前缀不一致，"
                        f"不能归因缺失层，请立即人工 reconcile！",
                        level='critical')
                    return False
                pending_ids = list(rec_ids)
            else:
                pending_ids = [oid for idx, oid in enumerate(entry_orders)
                               if idx >= last_filled_count and oid]

            # 🔒 v6.2-r6（P1 blocker）：**已知程序终态 ENTRY 不再重复撤单/验证**。
            #   `PROGRAMMATIC_CANCELED` 是此前一次「cancel 成功 + 按 ID verifier=gone」
            #   之后持久化下来的**事实**。若仍把它当 pending 重查：
            #     Binance 对历史已撤单返回 -2011 / OrderNotFound
            #     → verifier 按 v6 已批准纪律（OrderNotFound → unknown，绝不当成没成交）
            #     → gate=False + critical
            #     → 正常 close 被永久挡死（🗑️ 撤单之后再平仓即命中；MARKET 更糟：
            #        已成交后才进 gate，已知终态被查成 unknown → 批次冻结）。
            #   方向与 legacy 无 registry 相反：legacy 是**没有事实 → 不能猜**；
            #   此处是**已有确定事实 → 不该再假装不知道**。两者不矛盾。
            #   scope 最小化：只豁免 PROGRAMMATIC_CANCELED（证明链最干净），
            #   **不豁免** ABSENT / FAILED。
            def _known_terminal_entry_ids():
                reg = b_data.get('protection_registry') or {}
                if not isinstance(reg, dict):
                    return set()
                out = set()
                for _ident, _e in reg.items():
                    if not isinstance(_e, dict):
                        continue
                    if _e.get('role') != 'ENTRY':
                        continue
                    if _e.get('state') != 'PROGRAMMATIC_CANCELED':
                        continue
                    _oid = _e.get('order_id')
                    if _oid:
                        out.add(str(_oid))
                return out

            _skip_ids = _known_terminal_entry_ids()
            if _skip_ids:
                _before = len(pending_ids)
                pending_ids = [oid for oid in pending_ids if str(oid) not in _skip_ids]
                if len(pending_ids) != _before:
                    print(f"  └─ ℹ️ 跳过 {_before - len(pending_ids)} 张已确认程序终结的 ENTRY"
                          f"（registry=PROGRAMMATIC_CANCELED，不再重复撤单/验证）")

            if not pending_ids:
                return True

            for order_id in pending_ids:
                try:
                    self._safe_api_call(self.exchange.cancel_order, order_id, symbol,
                                        params={'stop': True})
                    print(f"  └─ 已撤销开仓挂单: {order_id}")
                except Exception as e:
                    if '-2011' in str(e) or 'Unknown order' in str(e):
                        print(f"  └─ 开仓挂单 {order_id} 已不存在（视为已撤）")
                    else:
                        print(f"  └─ ⚠️ 撤销开仓挂单失败: {order_id} ({e})（由逐 ID 验证阶段定案）")

            # ── 第 1 层：open_orders 快照（v4：禁 or []，None/非 list = Fail-Closed）
            try:
                remaining = self._safe_api_call(
                    self.exchange.fetch_open_orders, symbol, params={'stop': True})
            except Exception as e:
                remaining = None
                print(f"  └─ ⚠️ 撤单后交易所快照查询异常: {e}")
            if remaining is None or not isinstance(remaining, list):
                self.send_tg_notification(
                    f"🚨【资金安全】平仓后 ENTRY 校验失败（快照不可判定）！\n"
                    f"🆔 批次: {batch_id}\n"
                    f"⚠️ fetch_open_orders 返回 {type(remaining).__name__}，"
                    f"无法确认残留 ENTRY 是否已清零，请立即人工核对！",
                    level='critical')
                return False

            remaining_ids = {str(o.get('id')) for o in remaining if isinstance(o, dict)}
            still_alive = [oid for oid in pending_ids if str(oid) in remaining_ids]
            if still_alive:
                print(f"  └─ 🚨 撤单后交易所仍存在 ENTRY: {still_alive}")
                self.send_tg_notification(
                    f"🚨【资金安全】平仓成功后仍有未撤销的开仓条件单！\n"
                    f"🆔 批次: {batch_id}\n📌 残留订单: {still_alive}\n"
                    f"⚠️ 这些挂单成交后将形成无保护仓位，请立即人工处理！",
                    level='critical')
                return False

            # ── 第 2 层：逐 ID fetch_order 终态确认
            for oid in pending_ids:
                verdict, _order = self._verify_entry_order_terminal(oid, symbol)
                if verdict == 'gone':
                    continue
                if verdict == 'filled':
                    detail = f"ENTRY {oid} 在平仓等待期间成交（仓位已变化）"
                elif verdict == 'open':
                    detail = f"ENTRY {oid} 撤单后仍存活"
                else:
                    detail = f"ENTRY {oid} 终态无法判定（查询失败）"
                print(f"  └─ 🚨 ENTRY 逐 ID 验证未通过: {detail}")
                self.send_tg_notification(
                    f"🚨【资金安全】平仓后 ENTRY 逐 ID 验证未通过！\n"
                    f"🆔 批次: {batch_id}\n📌 {detail}\n"
                    f"⚠️ 可能形成无保护仓位，请立即人工核对持仓与挂单！",
                    level='critical')
                return False

            print(f"  └─ ✅ ENTRY 撤单已交易所侧校验通过"
                  f"（快照 + 逐 ID 终态，{len(pending_ids)} 个全部确认消失）")
            return True

    def _pending_entry_ids_for_gate(self, symbol: str, batch_id: str,
                                        b_data: dict, last_filled_count: int):
            """gate 的 pending ENTRY ID 视图：从 claimed 台账内 registry 恢复 ID 链。

            🔒 v6.2：registry 的 layer 是【原始 signal.entries 层号】（创建循环
            enumerate(signal.entries) + 跳层 continue），而 entry_orders 只 append
            成功层 = 压缩列表，两者坐标系不同。先例
            _rebuild_entry_orders_from_registry 按 entry['layer'] 升序重建——
            本 helper 复刻同一坐标系：
              ① role=ENTRY 且有 order_id（不限 state：真实创建过的单）
              ② layer 非 int / bool / 重复 → Fail-Closed（绝不静默解释成 layer 0）
              ③ 按 layer 升序 → chain
              ④ 一致性证明：len(chain) == len(target_amounts)
                 且 entry_orders == chain[:len(entry_orders)]
              ⑤ 待验证 = chain[last_filled_count:]
            只读 claimed 快照（D-5），零交易所 API、零新增生产依赖。
            返回 (ids: list, recoverable: bool, chain: list)。
            """
            reg = b_data.get('protection_registry', {})
            target_amounts = b_data.get('target_amounts', []) or []
            entry_orders = b_data.get('entry_orders', []) or []
            if not isinstance(reg, dict):
                return [], False, []
            confirmed = []
            seen_layers = set()
            for _identity, entry in reg.items():
                if not isinstance(entry, dict) or entry.get('role') != 'ENTRY':
                    continue
                oid = entry.get('order_id')
                if not oid:
                    continue
                layer = entry.get('layer')
                if not isinstance(layer, int) or isinstance(layer, bool):
                    return [], False, []
                if layer in seen_layers:
                    return [], False, []
                seen_layers.add(layer)
                confirmed.append((layer, str(oid)))
            confirmed.sort(key=lambda x: x[0])
            chain = [oid for _, oid in confirmed]
            if len(chain) != len(target_amounts):
                return [], False, chain
            if entry_orders != chain[:len(entry_orders)]:
                return [], False, chain
            return chain[last_filled_count:], True, chain

    def _commit_limit_close_order_if_current(self, symbol: str, batch_id: str,
                                                 close_op_id: str, order_id: str,
                                                 limit_price: float, price_mode: str):
            """限价平仓单 ID 的 durable commit（专用窄入口，禁止泛化）。

            🔒 v6.2（INV-3）：durable commit = normal-state transition。
            迁移：limit_creating →（本 helper）→ limit_pending_normal。
            校验：op_id CAS + close_phase==1 + pending_close + 未被限价成交结算
            + **迁移源状态 == limit_creating**（否则 abnormal→normal 违反
            first-abnormal-wins）。只允许写 4 个字段（3 个订单字段 + reason）。
            锁内 load → 校验 → 修改 → _persist_states 必返回 True。
            返回 (ok, why)。why ∈ 'state_unreadable（{异常}）' / 'batch_missing'
            / 'op_id_mismatch' / 'not_in_close' / 'already_settled'
            / 'reason_changed' / 'persist_failed' / 'committed'。
            """
            with self._state_lock:
                try:
                    all_states = self.load_all_states()
                except Exception as e:
                    return False, f'state_unreadable（{e}）'
                b = (all_states.get(symbol, {}) or {}).get(batch_id)
                if not isinstance(b, dict):
                    return False, 'batch_missing'
                if (b.get('close_op_id') or '') != (close_op_id or ''):
                    return False, 'op_id_mismatch（已有其他操作接管，不覆盖）'
                if int(b.get('close_phase', 0) or 0) != 1 or not b.get('pending_close'):
                    return False, 'not_in_close'
                if b.get('settled_by_limit_close'):
                    return False, 'already_settled'
                if b.get('close_reason') != 'limit_creating':
                    return False, 'reason_changed（迁移源状态不符）'
                b['limit_close_order_id'] = order_id
                b['limit_close_price'] = limit_price
                b['limit_close_mode'] = price_mode
                b['close_reason'] = 'limit_pending_normal'
                if not self._persist_states(all_states):
                    return False, 'persist_failed'
                return True, 'committed'

    # ==================== 新增：取消挂单 ====================

    def cancel_open_orders(self, batch_id: str):
        """取消指定批次的所有未成交开仓条件单（v6.2 正式 diff 改动 1 候选 AFTER）。"""
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
        # 🔒 v6.2（D-4 语义降级）：pending_count 仅表示「待撤尝试区大小」，
        # 不再表示「仍有 pending 单」——entry_orders 永不压缩后，尾段可能
        # 全是已终态 ID（重撤幂等，-2011 由 verifier 吸收）。
        pending_count = len(entry_orders) - last_filled_count

        if pending_count <= 0:
            return False, f"ℹ️ 批次 `{batch_id}` 没有未成交的挂单"

        cancel_requested_ids = []
        requested_layers = []
        unresolved_ids = []
        already_terminal_ids = []
        # 🔒 v6.2：三套统计严格分离（动作 ≠ 事实 ≠ 归因）：
        #   cancel_requested_ids  = cancel 调用成功返回（动作统计，仅日志/文案）
        #   programmatic_gone_ids = cancel 成功 + verifier=gone → 可写 registry 归因
        #   unresolved_ids        = verifier 非 gone（未确认终态，Fail-Closed 源）
        #   already_terminal_ids  = cancel 异常但 verifier=gone（事实 gone，
        #                           但无法证明是本程序终结 → 不写归因）
        programmatic_gone_ids = []

        # 🔒 v6.2-r6（P1）：已知程序终态 ENTRY 不进「待撤尝试区」。
        #   `PROGRAMMATIC_CANCELED` = 此前一次「cancel 成功 + 按 ID verifier=gone」
        #   持久化下来的事实。重复按 🗑️ 时若仍去 cancel/verify 这些历史已撤单，
        #   Binance 返回 -2011/OrderNotFound → verifier 判 unknown →
        #   unresolved_ids 非空 → 第二次 🗑️ 被报成「部分失败/失败」的假失败。
        #   只豁免 PROGRAMMATIC_CANCELED（不豁免 ABSENT / FAILED）。
        _known_terminal = set()
        for _ident, _e in (target_b_data.get('protection_registry') or {}).items():
            if not isinstance(_e, dict) or _e.get('role') != 'ENTRY':
                continue
            if _e.get('state') != 'PROGRAMMATIC_CANCELED':
                continue
            _oid = _e.get('order_id')
            if _oid:
                _known_terminal.add(str(_oid))

        # 🔒 v6.2（INV-3a）：从最高层往最低层撤 + 遇阻即停。
        # canceled 层恒在所有 active 层之上 → 成交位保持前缀连续，
        # 不主动制造 hole（last_filled_count 的 prefix 假设才成立）。
        for idx in reversed(range(last_filled_count, len(entry_orders))):
            order_id = entry_orders[idx]
            if str(order_id) in _known_terminal:
                # 已确认程序终结：不撤、不验证、不计入任何失败统计
                print(f"  └─ ℹ️ 第 {idx + 1} 层已确认程序终结"
                      f"（registry=PROGRAMMATIC_CANCELED），跳过")
                continue
            _cancel_ok = False
            try:
                self._safe_api_call(self.exchange.cancel_order, order_id, target_symbol,
                                    params={'stop': True})
                _cancel_ok = True
                cancel_requested_ids.append(order_id)
                requested_layers.append(idx + 1)
                print(f"  └─ 已请求撤销第 {idx + 1} 层挂单: {order_id}")
            except Exception as e:
                # 🔒 v6.2：**所有** cancel 异常（含 -2011/网络失败）一律交给
                # verifier 定案——不区分异常类型、不在 verifier 前下结论。
                print(f"  └─ ⚠️ 撤销第 {idx + 1} 层挂单请求异常: {order_id} ({e})，"
                      f"交由逐 ID 验证定案")
            # 🔒 v6.2（INV-1v2）：每层统一 verifier——
            # cancel 成功返回 ≠ terminal 事实（_safe_api_call 只透传底层结果）。
            verdict, _vo = self._verify_entry_order_terminal(order_id, target_symbol)
            if verdict == 'gone':
                if _cancel_ok:
                    programmatic_gone_ids.append(order_id)
                else:
                    already_terminal_ids.append(order_id)
                continue
            if verdict == 'filled':
                self.send_tg_notification(
                    f"🚨【资金安全】ENTRY 在撤单前已成交！\n"
                    f"🆔 批次: `{batch_id}`\n📌 订单: {order_id}\n"
                    f"⚠️ 高层成交 → 成交位可能已不连续（hole），"
                    f"已停止向更低层撤单，请立即人工核对持仓与台账！",
                    level='critical')
            unresolved_ids.append(order_id)
            break  # filled / open / unknown 一律停止（不制造 hole）

        if not programmatic_gone_ids and not already_terminal_ids and unresolved_ids:
            # 全部未确认终态：台账原样，批次保持现状，监控继续管辖这些层
            return False, (f"⚠️ 批次 `{batch_id}` 挂单全部未确认终态"
                           f"（{len(unresolved_ids)} 张，ID 已保留），"
                           f"请重试或人工核对")

        # 🔒 v6.2（ΔE1）：归因 order-ID scoped——不写 batch-global sticky
        # is_programmatic_cancel（棘轮字段，且会永久关闭 SL/TP 自动补挂）。
        for order_id in programmatic_gone_ids:
            _ident = self._find_registry_identity_by_order_id(target_symbol, batch_id,
                                                              order_id)
            if _ident:
                self._update_registry(target_symbol, batch_id, _ident,
                                      state='PROGRAMMATIC_CANCELED',
                                      order_id=order_id, id_known=True,
                                      terminated_reason='cancel_open_orders')
            else:
                print(f"  └─ ⚠️ 撤单归因：{order_id} 在 registry 中无 identity"
                      f"（撤单事实已完成，归因降级为 manual 语义）")

        # 🔒 v6.2（D-4）：entry_orders 作为 positional/audit ledger 永不压缩——
        # 已终态的 ID 一并留在原位，层号零漂移；terminal 与 pending 状态
        # 不再由 list 长度推断。
        target_b_data['entry_orders'] = list(entry_orders)

        pending_sl = target_b_data.get('pending_sl_orders', [])
        pending_sl = [i for i in pending_sl if i < last_filled_count]
        target_b_data['pending_sl_orders'] = pending_sl

        current_持仓 = sum(target_b_data.get('target_amounts', [])[:last_filled_count])

        if last_filled_count > 0:
            self.save_batch_state(target_symbol, batch_id, target_b_data)
            result_msg = (
                f"🗑️ **撤单完成**\n\n"
                f"🆔 批次：`{batch_id}`\n"
                f"🪙 标的：`{target_symbol}`\n"
                f"📊 本轮待撤尝试：{pending_count} 层\n"
                f"├─ 已确认终态：{len(programmatic_gone_ids) + len(already_terminal_ids)} 张\n"
                f"├─ 未确认终态：{len(unresolved_ids)} 张（ID 已保留）\n"
                f"📊 当前持仓：{current_持仓}\n\n"
                f"💡 {last_filled_count} 层已成交，止盈止损单已保留，监控继续运行"
            )
        else:
            # 🔒 v6.2：zero-filled 终止标志的前置 = 逐 ID terminal 确认。
            # 旧代码无条件 entry_orders=[] + pending_close/close_phase=1，
            # 而 monitor 只看磁盘标志 → 活 ENTRY 失去管辖。
            unresolved = []
            _filled_found = False
            for order_id in entry_orders:
                if str(order_id) in _known_terminal:
                    # 已确认程序终结：再 verify 只会 OrderNotFound→unknown，永不收敛
                    continue
                verdict, _vo = self._verify_entry_order_terminal(order_id, target_symbol)
                if verdict == 'gone':
                    continue
                unresolved.append((order_id, verdict))
                if verdict == 'filled':
                    _filled_found = True
            if _filled_found:
                self.send_tg_notification(
                    f"🚨【资金安全】撤单确认期间发现 ENTRY 已成交！\n"
                    f"🆔 批次: `{batch_id}`\n"
                    f"⚠️ 批次不再是无持仓状态，已保持 ACTIVE 由监控接管，"
                    f"请立即人工核对持仓！",
                    level='critical')
            if unresolved:
                # 绝不写 pending_close/close_phase/is_programmatic_cancel
                self.save_batch_state(target_symbol, batch_id, target_b_data)
                return False, (f"⚠️ 批次 `{batch_id}` 撤单后仍有 "
                               f"{len(unresolved)} 张 ENTRY 未确认终态"
                               f"（{[u[0] for u in unresolved]}，ID 已保留，监控继续）\n"
                               f"💡 请重试或人工核对")
            # 全部 gone → 才获得终止资格
            target_b_data['pending_sl_orders'] = []
            target_b_data['pending_close'] = True
            target_b_data['close_phase'] = 1
            self.save_batch_state(target_symbol, batch_id, target_b_data)

            result_msg = (
                f"🗑️ **撤单完成**\n\n"
                f"🆔 批次：`{batch_id}`\n"
                f"🪙 标的：`{target_symbol}`\n"
                f"📊 已确认终态：{len(entry_orders)} 张\n"
                f"📊 当前持仓：0\n\n"
                f"💡 批次已无成交层，监控将自然退出"
            )

        if unresolved_ids:
            # 🔒 v6.2（D-3 裁定）：部分失败就是失败（统计口径 = 事实，非动作）
            return False, (f"⚠️ 批次 `{batch_id}` 撤单部分完成："
                           f"{len(programmatic_gone_ids) + len(already_terminal_ids)} 张已确认终态，"
                           f"{len(unresolved_ids)} 张未确认终态（{unresolved_ids}，"
                           f"ID 已完整保留，监控继续）\n💡 请重试或人工核对")

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
        # ⚠️ v6（你的 §一）：入口**不再派生任何 transaction 变量**。
        # 原 L6964-6967 在入口就算好 last_filled_count / current_filled_amount，
        # 但监控线程（L6226/L6245/L6255）会在其间更新它们并落盘 —— BEGIN 声称的
        # 是最新状态，下单参数却来自 BEGIN 之前的旧快照。claim 与 transaction
        # 必须绑定，所以全部变量改为 BEGIN 之后、用 claimed 快照派生。
        # 🔥 修复漏洞1b（保留）：先取市价，成功后再动批次状态。
        try:
            ticker = self._safe_api_call(self.exchange.fetch_ticker, target_symbol)
            current_price = float(ticker.get('last') or ticker.get('close') or 0.0)
        except Exception as e:
            return False, f"❌ 获取市价失败: {e}"

        # 🆕 atomic BEGIN（改动 3v6）：claim + 落盘 + **返回 claimed 快照**
        _begin_ok, close_op_id, _begin_why, _claimed = self._begin_close_request_if_active(
            target_symbol, batch_id, 'market_confirming')
        if not _begin_ok:
            # 未取得所有权 → **绝不发出任何交易所订单**（改动 3v6-1）
            self.send_tg_notification(
                f"🚨【资金安全】市价平仓未启动：未取得平仓事务所有权。\n"
                f"🆔 批次: `{batch_id}`\n💡 原因: {_begin_why}\n"
                f"⚠️ 未发出任何订单，请人工核对批次状态。",
                level='critical')
            return False, f"❌ 市价平仓未启动（{_begin_why}）"

        # 🔑 v6（你的 §一）：以 BEGIN 锁内 claim 的快照为**唯一基线**派生本次
        # transaction 的全部 batch-derived 变量。
        # 必须整套同源：结算段有 `target_amounts[i] * filled_details[i] for i in
        # range(last_filled_count)` —— 若层数用新值而明细用旧值 → IndexError。
        _vars_ok, _txn_vars, _vars_why = self._derive_close_txn_vars(_claimed, batch_id)
        if not _vars_ok:
            # claimed 快照显示无需平仓 / 账本残缺 → 撤销这次 claim 再退出
            _rb_ok, _rb_why = self._rollback_close_request_if_current(
                target_symbol, batch_id, close_op_id)
            self.send_tg_notification(
                f"🚨【资金安全】市价平仓中止：claimed 快照不能用于下单。\n"
                f"🆔 批次: `{batch_id}`\n💡 原因: {_vars_why}\n"
                f"🔄 回滚本次平仓标记: {'成功' if _rb_ok else '失败（' + _rb_why + '）'}\n"
                f"⚠️ 未发出任何订单，请人工核对账本与批次状态。",
                level='critical')
            return False, f"❌ 市价平仓中止（{_vars_why}）"

        target_b_data = _claimed
        last_filled_count = _txn_vars['last_filled_count']
        target_amounts = _txn_vars['target_amounts']
        current_filled_amount = _txn_vars['current_filled_amount']
        side = _txn_vars['side']
        # 计算均价和预估盈亏（以 claimed 快照为准；v6.4 净成本/剩余 fee 分摊）
        filled_details = target_b_data.get('filled_details', [])
        total_cost = sum([target_amounts[i] * filled_details[i] for i in range(last_filled_count)])
        total_entry_fee = target_b_data.get('total_entry_fee', 0.0)
        _net_cost_m = total_cost - float(target_b_data.get('realized_reduce_cost', 0.0) or 0.0)
        _gross_cost_m = _net_cost_m + float(target_b_data.get('realized_reduce_cost', 0.0) or 0.0)
        # v6.4：剩余 fee 按 cost 比例分摊（fee ∝ notional，非 qty）
        _fee_rem = float(total_entry_fee or 0.0) * _net_cost_m / _gross_cost_m \
            if _gross_cost_m > 0 else 0.0
        avg_price = (_net_cost_m + _fee_rem) / current_filled_amount \
            if current_filled_amount > 0 else 0

        if side == 'BUY':
            gross_pnl = (current_price - avg_price) * current_filled_amount
        else:
            gross_pnl = (avg_price - current_price) * current_filled_amount

        # 估算平仓手续费（市价 = Taker）
        exit_fee = current_price * current_filled_amount * TAKER_FEE_RATE
        total_fees = total_entry_fee + exit_fee
        net_pnl = gross_pnl - total_fees

        # 执行市价平仓
        close_order_placed = False    # 订单已创建（仅此而已）
        close_position_confirmed = False  # 仓位已真实减少（交易所侧事实）
        try:
            # 🆕 平仓确认·第 1 步：平仓【前】取本方向敞口基数。
            pos_before = self._read_position_amt(
                target_symbol, side, target_b_data.get('is_hedge_mode', False))
            if pos_before is None:
                # B-09（已批准）：Fail-Closed 不发单 + critical。
                self.send_tg_notification(
                    f"🚨【资金安全】市价平仓中止：无法读取实际持仓敞口，"
                    f"无法确定安全平仓数量（Fail-Closed，未发单）。\n"
                    f"🆔 批次: `{batch_id}`\n⚠️ 请人工在交易所核对并平仓！",
                    level='critical')
                raise RuntimeError("平仓前读取持仓敞口失败（Fail-Closed：不发出平仓单）")

            # 🔥 归因守卫（§一 v5 修正后）：sum_all **含本批次**
            close_amount, _amt_detail = self._close_amount_guard(
                target_symbol, side, target_b_data.get('is_hedge_mode', False),
                current_filled_amount, batch_id)
            if not close_amount:
                # 归因冲突 / 读取失败 / 同方向在途：绝不猜归属，转人工 reconcile
                self.send_tg_notification(
                    f"🚨【资金安全】市价平仓中止（归因守卫）：{_amt_detail}\n"
                    f"🆔 批次: `{batch_id}`\n"
                    f"⚠️ 账本与交易所可能已漂移，请先 reconcile 再人工处置！",
                    level='critical')
                raise RuntimeError(f"平仓数量守卫拦截（{_amt_detail}）")
            print(f"  └─ {_amt_detail}")

            # 🔥 修复漏洞1：先市价平仓，成功后再撤 SL/TP
            close_side = 'sell' if side == 'BUY' else 'buy'
            # 🔥 A 修复（2026-08-29 -4061 事故）：与限价平仓共用 params_base 派生；
            # 双向持仓 → positionSide，单向 → reduceOnly，不同时塞两个参数。
            order_params = target_b_data['params_base'].copy()
            if target_b_data.get('is_hedge_mode', False):
                order_params['positionSide'] = 'LONG' if side == 'BUY' else 'SHORT'
            else:
                order_params['reduceOnly'] = True

            order = self._safe_api_call(
                self.exchange.create_order,
                symbol=target_symbol,
                type='MARKET',
                side=close_side,
                amount=close_amount,
                params=order_params,
                retries=1
            )
            # ⚠️ 铁律：仅表示【订单已创建】，置 True 后**绝不改回 False**。
            close_order_placed = True

            close_order_id = order.get('id') if isinstance(order, dict) else None
            if not close_order_id:
                raise RuntimeError("平仓单已提交但未返回订单 ID，无法按单确认成交")

            # 🆕 平仓确认（六态）：fetch_order(order_id) 按单归因。
            _verdict, _detail, _filled = self._confirm_close_filled(
                target_symbol, side, target_b_data.get('is_hedge_mode', False),
                close_order_id, close_amount, pos_before)

            if _verdict == 'CONFIRMED_FULL':
                close_position_confirmed = True
                confirmed_filled_amount = float(_filled or close_amount)
            elif _verdict == 'TERMINAL_ZERO':
                # 唯一可回滚状态（canceled/expired/rejected + 权威 filled == 0）。
                _rb_ok, _rb_why = self._rollback_close_request_if_current(
                    target_symbol, batch_id, close_op_id)
                if _rb_ok:
                    print(f"  └─ 🔄 平仓单未成交，已原子回滚（{_rb_why}），"
                          f"批次回 ACTIVE，SL/TP 继续在位保护")
                    self.send_tg_notification(
                        f"ℹ️ [程序撤单] 市价平仓单未成交（{_detail}），"
                        f"已原子回滚，批次回 ACTIVE。\n🆔 批次: `{batch_id}`")
                    return False, f"❌ 市价平仓未成交（已回滚）: {_detail}"
                # 🔒 v6.2（改动 6.3）：rollback 被拒 → CAS 写 rollback_rejected
                _rs_ok, _rs_why = self._set_close_reason_if_current(
                    target_symbol, batch_id, close_op_id, 'rollback_rejected')
                raise RuntimeError(
                    f"平仓单未成交且回滚被拒绝（{_rb_why}），转人工处置"
                    + ('' if _rs_ok else
                       f"；⚠️ close_reason 切换失败（{_rs_why}），"
                       "冻结告警可能静默，请立即人工核查！"))
            else:
                # PARTIAL / PENDING / UNKNOWN / NOT_CONFIRMED —— 一律**不回滚**。
                # 🔒 v6.2（改动 6.2）：统一 CAS 写异常态。
                _rs_ok, _rs_why = self._set_close_reason_if_current(
                    target_symbol, batch_id, close_op_id, 'market_confirm_unknown')
                raise RuntimeError(
                    f"市价平仓单结果未确认（{_verdict}）：{_detail}。"
                    f"不回滚，保持冻结等人工处置"
                    + ('' if _rs_ok else
                       f"；⚠️ close_reason 切换失败（{_rs_why}），"
                       "冻结告警可能不再周期触发，请立即人工核查！"))

            # 🔥 v6（§二）：先撤未成交 ENTRY 并逐 ID 验证，通过后才撤保护单。
            # 返回值必须成为 clear gate。
            _entries_ok = self._cancel_and_verify_entry_orders(
                target_symbol, batch_id, target_b_data, last_filled_count)
            if not _entries_ok:
                # 🛡️ SL/TP 仍在位，仓位保护没有丢失。
                _rs_ok, _rs_why = self._set_close_reason_if_current(
                    target_symbol, batch_id, close_op_id, 'market_entry_unknown')
                self.send_tg_notification(
                    f"🚨【资金安全】市价平仓已成交，但 ENTRY 收敛未确认！\n"
                    f"🆔 批次: `{batch_id}`\n"
                    f"🛡️ SL/TP **已保留未撤**，仓位仍有保护\n"
                    f"🚫 批次保持冻结（close_phase=1），本轮禁止进入 clear\n"
                    f"⚠️ 请立即人工核对残留开仓单与持仓！"
                    + ('' if _rs_ok else
                       f"\n⚠️ close_reason 切换失败（{_rs_why}），"
                       "冻结告警可能不再周期触发"),
                    level='critical')
                return False, ("❌ 市价平仓已成交但 ENTRY 收敛未确认"
                               "（SL/TP 保留，批次冻结待人工处置）")

            # 仓位已按单确认成交 **且 ENTRY 已确认清零** — 现在才安全撤销保护单
            # 🔑 ID 取自 `_txn_vars`（= claimed 快照）。
            _tp_terminal_ok = False
            if _txn_vars.get('tp_order_id'):
                try:
                    self._safe_api_call(self.exchange.cancel_order, _txn_vars['tp_order_id'],
                                        target_symbol, params={'stop': True})
                    _tp_terminal_ok = True
                    print(f"  └─ 已撤销止盈单: {_txn_vars['tp_order_id']}")
                except Exception:
                    pass

            _sl_terminal_ok = False
            if _txn_vars.get('current_sl_id'):
                try:
                    self._safe_api_call(self.exchange.cancel_order, _txn_vars['current_sl_id'],
                                        target_symbol, params={'stop': True})
                    _sl_terminal_ok = True
                    print(f"  └─ 已撤销止损单: {_txn_vars['current_sl_id']}")
                except Exception:
                    pass

            # 🔒 v6.2（改动 7）：只有各自 cancel 正常返回的那一张才写
            # PROGRAMMATIC_CANCELED。异常（含 -2011/OrderNotFound）= 不知道它为何
            # 消失 → 不记「程序已终结」，交 converge / 后续事实判断。
            for _oid, _ok in ((_txn_vars.get('tp_order_id'), _tp_terminal_ok),
                              (_txn_vars.get('current_sl_id'), _sl_terminal_ok)):
                if not _oid or not _ok:
                    continue
                _ident = self._find_registry_identity_by_order_id(target_symbol, batch_id, _oid)
                if _ident:
                    self._update_registry(target_symbol, batch_id, _ident,
                                          state='PROGRAMMATIC_CANCELED',
                                          order_id=_oid, id_known=True,
                                          terminated_reason='close_requested_canceled')

            # ══ 结算（confirmed_filled_amount 贯穿）══
            actual_price = float(order.get('average') or order.get('price') or current_price)

            if side == 'BUY':
                actual_gross_pnl = (actual_price - avg_price) * confirmed_filled_amount
            else:
                actual_gross_pnl = (avg_price - actual_price) * confirmed_filled_amount

            actual_exit_fee = actual_price * confirmed_filled_amount * TAKER_FEE_RATE
            actual_total_fees = total_entry_fee + actual_exit_fee
            actual_net_pnl = actual_gross_pnl - actual_total_fees

            capital_base = avg_price * confirmed_filled_amount if confirmed_filled_amount > 0 else 1
            net_pnl_pct = (actual_net_pnl / capital_base) * 100 if capital_base > 0 else 0.0

            pnl_emoji = "🟢" if actual_net_pnl >= 0 else "🔴"

            # 🔥 A1：市价平仓前撤销限价平仓单（场景C：已挂限价单 → 用户 /close）
            self._cancel_limit_close_order(target_symbol, batch_id)

            # P0 Batch A（§2.1）：市价结算完成 → close_phase=2（CLOSE_SETTLING）
            try:
                _settle_states = self.load_all_states()
                _settle_b = _settle_states.get(target_symbol, {}).get(batch_id, {})
                if _settle_b:
                    _settle_b['close_phase'] = 2
                    self.save_batch_state(target_symbol, batch_id, _settle_b)
            except Exception:
                pass

            # P0 Batch B（D-B5）：平仓成功照报 + 附残单收敛提示；clear 须 converge
            _cleanup_pending = False
            _proof = self._converge_batch_orders_before_clear(target_symbol, batch_id)
            if _proof is None or not self.clear_batch_state(target_symbol, batch_id, proof=_proof):
                _cleanup_pending = True

            result_msg = (
                f"📊 **[市价平仓结算]**\n\n"
                f"🆔 **批次号**：`{batch_id}`\n"
                f"🪙 **标的**：`{target_symbol}`\n"
                f"📊 **方向**：`{side}`\n"
                f"📊 **平仓模式**：市价单 (Taker {TAKER_FEE_RATE * 100:.2f}%)\n"
                f"📊 **持仓**：`{confirmed_filled_amount}` (实际成交)\n"
                f"📈 **持仓均价**：`{avg_price:.2f}` USDT\n"
                f"💵 **平仓均价**：`{actual_price:.2f}` USDT\n"
                f"📊 **名义盈亏**：`{actual_gross_pnl:+.2f}` USDT\n"
                f"💸 **总手续费**：`{actual_total_fees:.4f}` USDT\n"
                f"{pnl_emoji} **最终净盈亏**：`{actual_net_pnl:+.2f}` USDT (`{net_pnl_pct:+.2f}%`)"
            )
            if _cleanup_pending:
                # 🔒 v6.2（改动 6.6）：cleanup=PENDING → CAS 写 settlement_stuck
                _rs_ok, _rs_why = self._set_close_reason_if_current(
                    target_symbol, batch_id, close_op_id, 'settlement_stuck')
                result_msg += (
                    f"\n\n⚠️ **[残单收敛提示]** 市价平仓已成交（持仓=SUCCESS），"
                    f"但批次清理暂未收敛（cleanup=PENDING，proof 未通过）。"
                    f"系统仍会继续尝试收敛；若告警持续存在，请人工检查残单。"
                    + ('' if _rs_ok else
                       f"\n⚠️ close_reason 切换失败（{_rs_why}），请人工关注该批次。"))

            print(f"\n{result_msg}")

            # 🔥 记录已实现盈亏 + 附带剩余持仓快照
            _pnl_partial = confirmed_filled_amount < current_filled_amount - 1e-12
            self._record_realized_pnl(batch_id, target_symbol, side, confirmed_filled_amount,
                                      avg_price, actual_price, actual_net_pnl, "市价平仓",
                                      pnl_partial=_pnl_partial)
            self._notify_snapshot(batch_id)

            return True, result_msg

        except Exception as e:
            # P0 Batch A（回滚收紧）：平仓单已创建成功后的异常 = 结算/簿记失败，
            # 绝不回滚 close_phase/flags。
            if close_order_placed:
                # 🔒 v6.2（改动 6.4）：post-create except → CAS 写 settlement_error
                _rs_ok, _rs_why = self._set_close_reason_if_current(
                    target_symbol, batch_id, close_op_id, 'settlement_error')
                self.send_tg_notification(
                    f"🚨【资金安全】市价平仓单已发出但后续结算异常（未回滚关闭标记）！\n"
                    f"🆔 批次: {batch_id}\n💡 原因: {str(e)[:150]}\n"
                    + ('' if _rs_ok else
                       f"⚠️ close_reason 切换失败（{_rs_why}），冻结告警可能静默\n")
                    + f"⚠️ 请立即人工核对持仓与挂单！",
                    level='critical')
                return False, f"❌ 市价平仓结算异常（平仓单已创建，close_phase 保持）: {e}"
            # 平仓单未创建：CAS 原子回滚（§二）
            try:
                _rb_ok, _rb_why = self._rollback_close_request_if_current(
                    target_symbol, batch_id, close_op_id)
            except Exception as _rb_err:
                _rb_ok, _rb_why = False, f'CAS 调用异常（{_rb_err}）'
            if _rb_ok:
                print(f"  └─ 🔄 平仓失败回滚：CAS 原子回滚成功（{_rb_why}），"
                      f"已清除 is_programmatic_cancel/pending_close/close_phase，监控线程恢复保护")
            else:
                # 🔒 v6.2（改动 6.5）：pre-create rollback 失败 → CAS 写 abnormal
                _rs_ok, _rs_why = self._set_close_reason_if_current(
                    target_symbol, batch_id, close_op_id, 'txn_aborted_rollback_failed')
                self.send_tg_notification(
                    f"🚨【资金安全】市价平仓失败且回滚被拒绝！\n批次: `{batch_id}`\n"
                    f"原因: {_rb_why}\n"
                    + ('' if _rs_ok else
                       f"⚠️ close_reason 切换失败（{_rs_why}），"
                       "冻结告警可能静默，请立即人工处置！\n")
                    + f"请立即检查仓位是否仍有 SL 保护！",
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

    # ==================== P0 Batch B：converge 收敛证明生产者 + proof 门 ====================

    def _get_amount_precision(self, symbol: str) -> float:
        """D-B1：position_zero 容差 = 交易所 amount precision（不用裸 epsilon）。
        precision 为整数（小数位数）→ 10^-n；为浮点（步长）→ 步长本身；
        市场信息缺失 → 1e-8（保守兜底）。"""
        try:
            markets = getattr(self.exchange, 'markets', None)
            m = markets.get(symbol) if isinstance(markets, dict) else None
            prec = (m.get('precision') or {}).get('amount') if isinstance(m, dict) else None
            if isinstance(prec, bool):
                prec = None
            if isinstance(prec, int):
                return float(10 ** -max(int(prec), 0))
            if isinstance(prec, float) and prec > 0:
                return float(prec)
        except Exception:
            pass
        return 1e-8

    def _batch_has_active_exposure(self, b_data: dict) -> bool:
        """修正1（ChatGPT 终审）：proof scope 校验看当前活跃敞口，不看历史痕迹
        （settled_by_limit_close 等标记不参与）。已成交数量>0 / 限价平仓单在场 /
        pending_close → 需要 scope=FULL；否则 PRE_ENTRY 即可。纯状态字段判定，零 API。"""
        if not isinstance(b_data, dict):
            return False
        try:
            ta = b_data.get('target_amounts') or []
            n = int(b_data.get('last_filled_count') or 0)
            filled = float(sum(ta[:n])) if n > 0 else 0.0
        except (TypeError, ValueError):
            filled = 0.0
        return bool(filled > 0 or b_data.get('limit_close_order_id')
                    or b_data.get('pending_close'))

    def _converge_alert(self, key, msg: str, level: str = 'critical') -> None:
        """B1 告警闸门：同键最多 3 轮后静默（安全不变量：防持续告警触发交易所
        API 熔断）。⚠️ MagicMock 坑（第 7 次实证防御）：_converge_alert_counts 必须
        isinstance 校验后兜底真实 dict（getattr MagicMock 非 dict → 告警静默丢失）。"""
        counts = getattr(self, '_converge_alert_counts', None)
        if not isinstance(counts, dict):
            counts = {}
        try:
            n = int(counts.get(key, 0)) + 1
        except (TypeError, ValueError):
            n = 1
        counts[key] = n
        self._converge_alert_counts = counts
        if n <= 3:
            self.send_tg_notification(msg, level=level)
        else:
            print(f"🔇 [B] converge 告警同键 3 轮已满，静默: {key}")

    def _converge_cancel_order(self, order_id, symbol: str) -> str:
        """B1 撤单幂等三态返回：'canceled'（撤成功）/ 'absent'（-2011/Unknown order
        = 已离开交易所 = 事实终态视同成功，同 _cancel_limit_close_order L6865 惯例）/
        'failed'（其他异常 → 不收敛，下轮重试）。cancel 统一带 stop 参数（项目全库惯例）。"""
        try:
            self._safe_api_call(self.exchange.cancel_order, order_id, symbol,
                                params={'stop': True})
            return 'canceled'
        except Exception as e:
            if isinstance(e, ccxt.OrderNotFound) or '-2011' in str(e) \
                    or 'Unknown order' in str(e):
                return 'absent'
            print(f"  └─ ⚠️ [B1] 撤单失败 {order_id}: {e}")
            return 'failed'

    def _verify_clear_proof(self, symbol: str, batch_id: str, proof, b_data: dict):
        """B2 proof 门纯验证（调用方已持 _state_lock，本函数零 I/O 零 API）。
        合法返回 None；非法返回拒绝原因字符串（调用方锁外发 critical 告警）。
        Fail-Closed：缺键/类型不符/批次不符/position_zero 非 True/
        exchange_scan≠zero（CONVERGENCE_UNKNOWN 禁 clear）/scope 与当前敞口
        不匹配（修正1）全拒绝。"""
        if not isinstance(proof, dict):
            return 'proof 缺失或非 dict（需 converge 生成的收敛证明）'
        for _k in ('batch_id', 'symbol', 'scope', 'position_zero',
                   'state_ids_resolved', 'exchange_scan'):
            if _k not in proof:
                return f'proof 缺键 {_k}'
        if proof.get('batch_id') != batch_id or str(proof.get('symbol')) != str(symbol):
            return 'proof 批次/交易对不匹配'
        if proof.get('position_zero') is not True:
            return 'position_zero 非 True'
        if proof.get('exchange_scan') != 'zero':
            return (f"exchange_scan={proof.get('exchange_scan')} ≠ zero"
                    f"（CONVERGENCE_UNKNOWN 禁 clear）")
        _scope = proof.get('scope')
        if _scope not in ('FULL', 'PRE_ENTRY'):
            return f'scope={_scope} 非法'
        if self._batch_has_active_exposure(b_data) and _scope != 'FULL':
            return '当前存在活跃敞口，PRE_ENTRY proof 不足（修正1）'
        return None

    def _converge_batch_orders_before_clear(self, symbol: str, batch_id: str):
        """B1（converge proof 生产者）：clear 前把本批次相关的交易所现实收敛为
        可证明状态。返回 proof dict（可直接提交 clear_batch_state）或 None
        （未收敛，调用方下轮重试，绝不半途 clear）。
        纪律（G-B9 调用栈可审计）：本函数禁止调用 clear_batch_state——收敛与
        清理职责分离，clear 唯一入口在 proof 门之后。
        执行序列：
          ① 两源扫描（normal + params={'stop':True}，任一异常=CONVERGENCE_UNKNOWN→None）
          ② D-B1 贡献扣减 position 核验（贡献>容差 → 持仓仍在，拒绝清理）
          ③ L1/L2/L3 分级处置：L1=本批次已知 id 自动撤；L2=无主单匹配本批次
             registry intent 自动撤；L3=无主单只列示告警不撤不阻塞（D-B4）
          ④ D-B2 单次复扫（撤后仍见本批次相关单 → None，下轮重试，不做无限重试）
          ⑤ D-B3 未决条目三条件终态化 ABSENT
          ⑥ 产出 proof（scope 按当前活跃敞口判定，修正1）"""
        try:
            all_states = self.load_all_states()
        except Exception as e:
            print(f"⚠️ [B1] 读取状态失败: {e}")
            return None
        b_data = (all_states.get(symbol) or {}).get(batch_id)
        if not isinstance(b_data, dict):
            return None  # 批次已不存在（可能已被并发 clear），无需收敛
        # ① 两源扫描
        try:
            _normal = self._safe_api_call(self.exchange.fetch_open_orders, symbol) or []
            _stops = self._safe_api_call(self.exchange.fetch_open_orders, symbol,
                                         params={'stop': True}) or []
        except Exception as e:
            self._converge_alert(('scan_unknown', symbol, batch_id),
                                  f"🚨【资金安全】批次 `{batch_id}`({symbol}) 收敛扫描失败"
                                  f"（CONVERGENCE_UNKNOWN），本轮不 clear，下周期重试。\n"
                                  f"错误: {e}", level='critical')
            return None
        _open_map = {}
        for _o in list(_normal) + list(_stops):
            if isinstance(_o, dict) and _o.get('id'):
                _open_map[str(_o['id'])] = _o
        open_orders = list(_open_map.values())
        # ② D-B1 贡献扣减：symbol 持仓（绝对值）− 其他活跃批次已成交贡献
        _side = b_data.get('side') or 'BUY'
        try:
            pos_amt = self._get_current_position_amt(
                symbol, bool(b_data.get('is_hedge_mode')), side=_side)
        except Exception:
            pos_amt = None
        _others_filled = 0.0
        _owned_ids = set()
        try:
            for _bid, _bd in (all_states.get(symbol) or {}).items():
                if not isinstance(_bd, dict):
                    continue
                _owned_ids.update(str(_i) for _i in self._collect_batch_order_ids(_bd) if _i)
                if _bid != batch_id and _bd.get('is_active'):
                    _ta = _bd.get('target_amounts') or []
                    _n = int(_bd.get('last_filled_count') or 0)
                    if _n > 0:
                        try:
                            # v6.4：其他批次按净仓位计入（partial 后 gross 高估 → 本批次贡献被低估）
                            _others_filled += max(0.0, float(sum(_ta[:_n]))
                                                  - float(_bd.get('realized_reduce_amount', 0.0) or 0.0))
                        except (TypeError, ValueError):
                            pass
        except Exception as e:
            print(f"⚠️ [B1] 跨批次预计算异常: {e}")
            return None
        try:
            _contribution = float(pos_amt) - _others_filled
        except (TypeError, ValueError):
            _contribution = None
        if pos_amt is None or _contribution is None:
            self._converge_alert(('pos_unknown', symbol, batch_id),
                                  f"🚨【资金安全】批次 `{batch_id}`({symbol}) 持仓查询失败"
                                  f"（UNKNOWN ≠ EMPTY），本轮不 clear。", level='critical')
            return None
        _tolerance = self._get_amount_precision(symbol)
        _position_zero = (_contribution <= 0) or (abs(_contribution) <= _tolerance)
        if not _position_zero:
            self._converge_alert(('position_residual', symbol, batch_id),
                                  f"🚨【资金安全】批次 `{batch_id}`({symbol}) 清理前持仓核验失败："
                                  f"本批次持仓贡献 {_contribution} > 容差 {_tolerance}"
                                  f"（D-B1 贡献扣减法），不 clear。", level='critical')
            return None
        # ③ L1/L2/L3 分级处置
        _my_l1 = {str(_i) for _i in self._collect_batch_order_ids(b_data) if _i}
        _my_registry = b_data.get('protection_registry') or {}
        _pending_intents = []
        for _ident, _ent in _my_registry.items():
            if not isinstance(_ent, dict) or _ent.get('state') in _REGISTRY_TERMINAL_STATES:
                continue
            if isinstance(_ent.get('intent'), dict):
                _pending_intents.append((_ident, _ent['intent']))
        _l1_canceled, _l2_canceled, _l3_orphans = [], [], []
        _l2_matched_idents = set()
        for _o in open_orders:
            _oid = str(_o.get('id'))
            if _oid in _my_l1:
                _res = self._converge_cancel_order(_oid, symbol)
                if _res == 'failed':
                    self._converge_alert(('l1_cancel_fail', symbol, batch_id, _oid),
                                          f"🚨【资金安全】批次 `{batch_id}`({symbol}) "
                                          f"L1 撤单失败（{_oid}），不 clear，下轮重试。",
                                          level='critical')
                    return None
                _l1_canceled.append(_oid)
                try:
                    _ident = self._find_registry_identity_by_order_id(symbol, batch_id, _oid)
                    if _ident:
                        self._update_registry(symbol, batch_id, _ident,
                                              state='PROGRAMMATIC_CANCELED',
                                              order_id=_oid, id_known=True,
                                              terminated_reason='converge_l1_canceled')
                except Exception as _e:
                    print(f"⚠️ [B1] L1 registry 终态化失败 {_oid}: {_e}")
            elif _oid in _owned_ids:
                continue  # 他批次资产（L1 归属全集内），绝不碰
            else:
                _matched = None
                for _ident, _intent in _pending_intents:
                    try:
                        if self._order_matches_intent(_o, _intent, symbol):
                            _matched = _ident
                            break
                    except Exception:
                        continue
                if _matched is not None:
                    _l2_matched_idents.add(_matched)
                    _res = self._converge_cancel_order(_oid, symbol)
                    if _res == 'failed':
                        self._converge_alert(('l2_cancel_fail', symbol, batch_id, _oid),
                                              f"🚨【资金安全】批次 `{batch_id}`({symbol}) "
                                              f"L2 撤单失败（{_oid}），不 clear，下轮重试。",
                                              level='critical')
                        return None
                    _l2_canceled.append(_oid)
                    try:
                        self._update_registry(symbol, batch_id, _matched,
                                              state='PROGRAMMATIC_CANCELED',
                                              order_id=_oid, id_known=True,
                                              terminated_reason='converge_l2_canceled')
                    except Exception as _e:
                        print(f"⚠️ [B1] L2 registry 终态化失败 {_oid}: {_e}")
                else:
                    _l3_orphans.append({'id': _oid, 'type': _o.get('type'),
                                        'side': _o.get('side'), 'amount': _o.get('amount'),
                                        'stopPrice': _o.get('stopPrice')})
        # ④ D-B2 单次复扫：撤单后重扫两源，本批次相关单必须清零
        try:
            _n2 = self._safe_api_call(self.exchange.fetch_open_orders, symbol) or []
            _s2 = self._safe_api_call(self.exchange.fetch_open_orders, symbol,
                                      params={'stop': True}) or []
        except Exception as e:
            self._converge_alert(('rescan_unknown', symbol, batch_id),
                                  f"🚨【资金安全】批次 `{batch_id}`({symbol}) 撤单后复扫失败"
                                  f"（CONVERGENCE_UNKNOWN），本轮不 clear，下周期重试。\n"
                                  f"错误: {e}", level='critical')
            return None
        _seen2 = {}
        for _o in list(_n2) + list(_s2):
            if isinstance(_o, dict) and _o.get('id'):
                _seen2[str(_o['id'])] = _o
        _l1_hit = set(_l1_canceled) | set(_l2_canceled)
        for _o in _seen2.values():
            _oid = str(_o.get('id'))
            if _oid in _my_l1 or _oid in _l1_hit:
                print(f"⚠️ [B1] 复扫仍见本批次单 {_oid}（撤单竞态），"
                      f"本轮不 clear 下轮重试（D-B2 单次复扫）")
                return None
            for _ident, _intent in _pending_intents:
                try:
                    if self._order_matches_intent(_o, _intent, symbol):
                        print(f"⚠️ [B1] 复扫仍见本批次 L2 匹配单 {_oid}，本轮不 clear 下轮重试")
                        return None
                except Exception:
                    continue
        # L3 告警（D-B4：不阻塞 clear，仅列示人工处置）
        if _l3_orphans:
            _l3_ids = ', '.join(str(_x.get('id')) for _x in _l3_orphans)
            self._converge_alert(('l3_orphans', symbol, batch_id),
                                  f"🚨【资金安全】批次 `{batch_id}`({symbol}) 收敛时发现 "
                                  f"{len(_l3_orphans)} 笔无主挂单（L3：不自动撤、不阻塞清理，"
                                  f"请人工核查处置）：{_l3_ids}", level='critical')
        # ⑤ D-B3：未决条目三条件终态化 ABSENT
        #（position_zero ✓ + 复扫本批次清零 ✓ + 该条目无 L2 匹配 ✓）
        for _ident, _intent in _pending_intents:
            if _ident in _l2_matched_idents:
                continue
            try:
                self._update_registry(symbol, batch_id, _ident, state='ABSENT',
                                      terminated_reason='converge_absent')
            except Exception as _e:
                print(f"⚠️ [B1] D-B3 ABSENT 终态化失败 {_ident}: {_e}")
        # ⑥ 产出 proof
        _scope = 'FULL' if self._batch_has_active_exposure(b_data) else 'PRE_ENTRY'
        _state_ids_resolved = sorted(
            _my_l1 | {str(_ent.get('order_id')) for _ent in _my_registry.values()
                      if isinstance(_ent, dict) and _ent.get('order_id')})
        proof = {
            'batch_id': batch_id, 'symbol': symbol, 'checked_at': time.time(),
            'scope': _scope, 'position_zero': True,
            'state_ids_resolved': _state_ids_resolved,
            'exchange_scan': 'zero',
            'l1_canceled': sorted(set(_l1_canceled)),
            'l2_canceled': sorted(set(_l2_canceled)),
            'l3_orphans': _l3_orphans,
        }
        print(f"✅ [B1] 批次 {batch_id}({symbol}) 收敛证明生成："
              f"L1={len(proof['l1_canceled'])} L2={len(proof['l2_canceled'])} "
              f"L3={len(_l3_orphans)} scope={_scope}")
        return proof

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
        # ⚠️ v6（你的 §一）：入口**不再派生任何 transaction 变量**。
        # 原 L6964-6967 在入口就算好 last_filled_count / current_filled_amount，
        # 但监控线程（L6226/L6245/L6255）会在其间更新它们并落盘 —— BEGIN 声称的
        # 是最新状态，下单参数却来自 BEGIN 之前的旧快照。claim 与 transaction
        # 必须绑定，所以全部变量改为 BEGIN 之后、用 claimed 快照派生。
        # 🔥 修复漏洞1b（保留）：先取市价，成功后再动批次状态。
        try:
            ticker = self._safe_api_call(self.exchange.fetch_ticker, target_symbol)
            current_price = float(ticker.get('last') or ticker.get('close') or 0.0)
            bid = float(ticker.get('bid') or current_price)
            ask = float(ticker.get('ask') or current_price)
        except Exception as e:
            return False, f"❌ 获取市价失败: {e}"

        # 🆕 atomic BEGIN（改动 3v6）：claim + 落盘 + **返回 claimed 快照**
        _begin_ok, close_op_id, _begin_why, _claimed = self._begin_close_request_if_active(
            target_symbol, batch_id, 'limit_creating')
        if not _begin_ok:
            # 未取得所有权 → **绝不发出任何交易所订单**（改动 3v6-1）
            self.send_tg_notification(
                f"🚨【资金安全】限价平仓未启动：未取得平仓事务所有权。\n"
                f"🆔 批次: `{batch_id}`\n💡 原因: {_begin_why}\n"
                f"⚠️ 未发出任何订单，请人工核对批次状态。",
                level='critical')
            return False, f"❌ 限价平仓未启动（{_begin_why}）"

        # 🔑 v6（你的 §一）：以 BEGIN 锁内 claim 的快照为**唯一基线**派生本次
        # transaction 的全部 batch-derived 变量。
        # 必须整套同源：结算段有 `target_amounts[i] * filled_details[i] for i in
        # range(last_filled_count)` —— 若层数用新值而明细用旧值 → IndexError。
        _vars_ok, _txn_vars, _vars_why = self._derive_close_txn_vars(_claimed, batch_id)
        if not _vars_ok:
            # claimed 快照显示无需平仓 / 账本残缺 → 撤销这次 claim 再退出
            _rb_ok, _rb_why = self._rollback_close_request_if_current(
                target_symbol, batch_id, close_op_id)
            self.send_tg_notification(
                f"🚨【资金安全】限价平仓中止：claimed 快照不能用于下单。\n"
                f"🆔 批次: `{batch_id}`\n💡 原因: {_vars_why}\n"
                f"🔄 回滚本次平仓标记: {'成功' if _rb_ok else '失败（' + _rb_why + '）'}\n"
                f"⚠️ 未发出任何订单，请人工核对账本与批次状态。",
                level='critical')
            return False, f"❌ 限价平仓中止（{_vars_why}）"

        target_b_data = _claimed
        last_filled_count = _txn_vars['last_filled_count']
        target_amounts = _txn_vars['target_amounts']
        current_filled_amount = _txn_vars['current_filled_amount']
        side = _txn_vars['side']
        # 确定挂单价格（以 claimed 快照为准）
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
        filled_details = target_b_data.get('filled_details', [])
        total_cost = sum([target_amounts[i] * filled_details[i] for i in range(last_filled_count)])
        total_entry_fee = target_b_data.get('total_entry_fee', 0.0)
        # v6.4：净成本/剩余 fee 按 cost 比例分摊（partial 后 gross 均价虚高会误报亏损警告）
        _rr_cost_l = float(target_b_data.get('realized_reduce_cost', 0.0) or 0.0)
        _net_cost_l = total_cost - _rr_cost_l
        _gross_cost_l = total_cost
        _fee_rem_l = float(total_entry_fee or 0.0) * _net_cost_l / _gross_cost_l \
            if _gross_cost_l > 0 else 0.0
        avg_price = (_net_cost_l + _fee_rem_l) / current_filled_amount \
            if current_filled_amount > 0 else 0

        if side == 'BUY' and limit_price <= avg_price:
            print(f"⚠️ 警告：平仓价 {limit_price} 不高于均价 {avg_price}，可能亏损")
        elif side == 'SELL' and limit_price >= avg_price:
            print(f"⚠️ 警告：平仓价 {limit_price} 不低于均价 {avg_price}，可能亏损")

        # 执行限价平仓
        close_order_placed = False  # P0 Batch A（回滚收紧）：平仓单是否已创建成功
        try:
            # 🆕 v6.1（P0-3）：限价路径「尝试撤 ENTRY」升级为撤销确认 gate。
            _entries_ok = self._cancel_and_verify_entry_orders(
                target_symbol, batch_id, target_b_data, last_filled_count)
            if not _entries_ok:
                # 🛡️ 此时**平仓单还没挂**，仓位零变化 —— 优先 CAS 回滚让监控恢复。
                _rb_ok, _rb_why = self._rollback_close_request_if_current(
                    target_symbol, batch_id, close_op_id)
                _rs_ok, _rs_why = True, ''
                if not _rb_ok:
                    _rs_ok, _rs_why = self._set_close_reason_if_current(
                        target_symbol, batch_id, close_op_id, 'limit_entry_unknown')
                self.send_tg_notification(
                    f"🚨【资金安全】限价平仓中止：ENTRY 收敛未确认！\n"
                    f"🆔 批次: `{batch_id}`\n"
                    f"🛡️ 未挂平仓单，TP/SL 全程未动，仓位保护完整\n"
                    f"🔄 回滚本次平仓标记: {'成功（监控已恢复）' if _rb_ok else '失败（' + _rb_why + '）'}\n"
                    f"⚠️ 请人工核对残留开仓单后再重新发起平仓！"
                    + ('' if _rs_ok else
                       f"\n⚠️ close_reason 切换失败（{_rs_why}），"
                       "冻结告警可能不再周期触发"),
                    level='critical')
                return False, ("❌ 限价平仓中止：ENTRY 收敛未确认"
                               "（未挂平仓单，TP/SL 保留）")

            # 🔒 v6.2（改动 9.0）：TP factual gate —— create LIMIT 之前的硬门。
            _tp_old_id = target_b_data.get('tp_order_id')
            if _tp_old_id:
                _tp_cancel_note = 'cancel 正常返回'
                try:
                    self._safe_api_call(self.exchange.cancel_order, _tp_old_id, target_symbol,
                                        params={'stop': True})
                    print(f"  └─ 已撤销旧止盈单: {_tp_old_id}")
                except Exception as _tp_cancel_e:
                    # cancel 动作结果（成功/异常）都不下结论，事实由六态确认器定案。
                    _tp_cancel_note = f'cancel 异常（{_tp_cancel_e}）'
                _tp_verdict, _tp_detail, _tp_filled = self._confirm_close_filled(
                    target_symbol, side,
                    target_b_data.get('is_hedge_mode', False),
                    _tp_old_id, current_filled_amount, current_filled_amount,
                    order_kind='conditional')
                if _tp_verdict != 'TERMINAL_ZERO':
                    # CONFIRMED_FULL/PARTIAL = 仓位事实已变化；PENDING = 仍活着；
                    # UNKNOWN/NOT_CONFIRMED = 无法证明安全 —— 一律 Fail-Closed。
                    _rs_ok, _rs_why = self._set_close_reason_if_current(
                        target_symbol, batch_id, close_op_id, 'limit_tp_unresolved')
                    self.send_tg_notification(
                        f"🚨【资金安全】限价平仓中止：旧止盈单 {_tp_old_id} 未确认安全终结！\n"
                        f"🆔 批次: `{batch_id}`\n"
                        f"📌 {_tp_cancel_note}；六态判定 = {_tp_verdict}（{_tp_detail}）\n"
                        f"⚠️ TP 可能仍在场或已触发成交——此时挂 LIMIT 会减到同方向"
                        f" aggregate 敞口（错平其他批次）\n"
                        f"🚫 未发出任何平仓单，批次冻结待人工处置\n"
                        + ('' if _rs_ok else
                           f"⚠️ close_reason 切换失败（{_rs_why}），冻结告警可能静默\n")
                        + f"💡 请人工核对该 TP 与当前持仓后再决定处置",
                        level='critical')
                    return False, (f"❌ 限价平仓中止（旧 TP 六态={_tp_verdict}，"
                                   f"已冻结防止错平其他批次）")

            # 🔒 v6.2（改动 9.0b）：create 紧前 coverage guard（锁外调用）。
            safe_amount, _amt_detail = self._close_amount_guard(
                target_symbol,
                side,
                target_b_data.get('is_hedge_mode', False),
                current_filled_amount,
                batch_id,
            )
            if safe_amount is None or abs(safe_amount - current_filled_amount) > 1e-8:
                _rs_ok, _rs_why = self._set_close_reason_if_current(
                    target_symbol, batch_id, close_op_id, 'limit_amount_conflict')
                self.send_tg_notification(
                    f"🚨【资金安全】限价平仓中止：平仓数量与 aggregate 敞口冲突！\n"
                    f"🆔 批次: `{batch_id}`\n"
                    f"📌 {_amt_detail}\n"
                    f"📌 台账量 {current_filled_amount}，guard 判定 {safe_amount}\n"
                    f"⚠️ 此时挂 LIMIT 可能减到属于其他批次的剩余敞口（错平）\n"
                    f"🚫 未发出任何平仓单，批次冻结待人工 reconcile\n"
                    + ('' if _rs_ok else
                       f"⚠️ close_reason 切换失败（{_rs_why}），冻结告警可能静默\n"),
                    level='critical')
                return False, (f"❌ 限价平仓中止（coverage 冲突，"
                               f"已冻结防止错平其他批次）")

            # 挂限价平仓单（C5：create_order 非幂等，禁止盲重）
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
            close_order_placed = True  # P0 Batch A：平仓单已创建

            order_id = order['id']

            # 🔒 v6.2（改动 9.2 / INV-3）：durable commit 取代原 save_batch_state。
            _cm_ok, _cm_why = self._commit_limit_close_order_if_current(
                target_symbol, batch_id, close_op_id, order_id, limit_price, price_mode)
            if not _cm_ok:
                # B-lite：尝试撤掉这张无人管辖的活单，用既有六态确认器按事实定案；
                # 无论结果如何都不自动 rollback（close_order_placed 纪律）。
                #
                # 🔒 GREEN 终审 P0 修正（endpoint 一致性）：
                #   本单是 create_order(type='LIMIT') 建出的**普通限价单**（normal endpoint），
                #   不是 STOP/TAKE_PROFIT 条件单。r5 规格此处写 params={'stop': True}
                #   会把 cancel 路由到 algo/conditional 端点 → 真单撤不掉 →
                #   「刚创建但无法 durable commit 的无人管辖 LIMIT」安全网被削弱。
                #   生产既有 _cancel_limit_close_order() 对 limit_close_order_id 用的正是
                #   cancel_order(id, symbol) **不带 stop=True**（项目端点契约：
                #   conditional → stop=True / normal → 不传 params）。
                try:
                    self._safe_api_call(self.exchange.cancel_order, order_id,
                                        target_symbol)
                    _cx_note = "已发出撤单请求"
                except Exception as _cx_e:
                    _cx_note = f"撤单请求异常（{_cx_e}）"
                # 确认端点与本单类型一致：普通 LIMIT → normal（显式写出便于审计）
                _verdict_c, _detail_c, _filled_c = self._confirm_close_filled(
                    target_symbol, side,
                    target_b_data.get('is_hedge_mode', False),
                    order_id, current_filled_amount, 0.0,
                    order_kind='normal')
                _rs_ok, _rs_why = self._set_close_reason_if_current(
                    target_symbol, batch_id, close_op_id, 'limit_persist_failed')
                self.send_tg_notification(
                    f"🚨【资金安全】限价平仓单 {order_id} 账本写盘失败，已按 B-lite 处置！\n"
                    f"🆔 批次: `{batch_id}`\n💰 价格: {limit_price}\n"
                    f"🔄 {_cx_note}；六态确认 = {_verdict_c}（{_detail_c}）\n"
                    + ('' if _rs_ok else
                       f"⚠️ close_reason 切换失败（{_rs_why}），冻结告警可能静默\n")
                    + f"🚫 批次保持冻结（close_phase=1），绝不自动回滚，"
                      f"请立即人工核对该限价单与持仓！",
                    level='critical')
                return False, (f"❌ 限价平仓单账本写盘失败"
                               f"（六态={_verdict_c}），批次冻结待人工处置")

            # 🔥 N14 落盘（P0 Batch A）：平仓单已创建成功 → 已撤销的 TP 写入
            # registry PROGRAMMATIC_CANCELED 终态（孤儿 TP 事故通道封死）。
            if _tp_old_id:
                _tp_identity = self._find_registry_identity_by_order_id(
                    target_symbol, batch_id, _tp_old_id)
                if _tp_identity:
                    self._update_registry(target_symbol, batch_id, _tp_identity,
                                          state='PROGRAMMATIC_CANCELED',
                                          order_id=_tp_old_id, id_known=True,
                                          terminated_reason='close_requested_canceled')
                    print(f"  └─ [N14] TP registry 已终结: {_tp_identity} (close_requested_canceled)")

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
            # 🔥 P5k：与恢复路径**共用**同一加锁 helper（防 auth 恢复 reconcile
            # 与本次挂单并发 → 同订单两个监控 / 互相抹掉所有权）
            self._start_limit_close_monitor_once(
                (target_symbol, batch_id, close_op_id),
                (target_symbol, batch_id, order_id, current_filled_amount, avg_price,
                 total_entry_fee, side, last_filled_count, target_amounts, filled_details))

            return True, result_msg

        except Exception as e:
            # P0 Batch A（回滚收紧）：平仓单已创建成功后的异常 = 簿记/线程失败，
            # 绝不回滚 close_phase/flags——否则冻结解除、R14 补挂通道复活。
            if close_order_placed:
                # 🔒 v6.2（改动 9.3）：限价 outer except → CAS 写 settlement_error
                _rs_ok, _rs_why = self._set_close_reason_if_current(
                    target_symbol, batch_id, close_op_id, 'settlement_error')
                self.send_tg_notification(
                    f"🚨【资金安全】限价平仓单已挂出但后续流程异常（未回滚关闭标记）！\n"
                    f"🆔 批次: {batch_id}\n💡 原因: {str(e)[:150]}\n"
                    + ('' if _rs_ok else
                       f"⚠️ close_reason 切换失败（{_rs_why}），冻结告警可能静默\n")
                    + f"⚠️ 限价单仍在交易所，请立即人工核对！",
                    level='critical')
                return False, f"❌ 限价平仓后续流程异常（平仓单已创建，close_phase 保持）: {e}"
            # 🔥 修复漏洞1b：失败回滚 — 清除 flags，恢复监控线程保护能力
            try:
                _rb_ok, _rb_why = self._rollback_close_request_if_current(
                    target_symbol, batch_id, close_op_id)
            except Exception as _rb_err:
                _rb_ok, _rb_why = False, f'CAS 调用异常（{_rb_err}）'
            if _rb_ok:
                print(f"  └─ 🔄 挂限价单失败回滚：CAS 原子回滚成功（{_rb_why}），"
                      f"已清除 is_programmatic_cancel/pending_close/close_phase，监控线程恢复保护")
            else:
                # 🔒 v6.2（改动 9.3 else）：rollback 失败 → CAS 写 abnormal
                _rs_ok, _rs_why = self._set_close_reason_if_current(
                    target_symbol, batch_id, close_op_id, 'txn_aborted_rollback_failed')
                self.send_tg_notification(
                    f"🚨【资金安全】限价平仓失败且回滚被拒绝！\n批次: `{batch_id}`\n"
                    f"原因: {_rb_why}\n"
                    + ('' if _rs_ok else
                       f"⚠️ close_reason 切换失败（{_rs_why}），请立即人工处置！\n")
                    + f"请立即检查仓位是否仍有 SL 保护！",
                    level='critical')
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
                    # 🔥 P5：FULL_FILL 共享幂等 finalizer（与 /closecancel 命令共用；
                    # CAS 认领 + PnL 按订单去重 + converge + clear，接管语义见函数 docstring）
                    # 🔥 P5c（ChatGPT 二复审 Blocker 3）：必须检查返回值——可重试失败
                    # （PnL 落盘失败/订单查询失败）保持轮询接管，绝不无条件 break
                    _ok_f, _msg_f = self._finalize_limit_full_fill(
                        symbol, batch_id, order_id, order=order)
                    if _ok_f:
                        break
                    print(f"  └─ ⚠️ [限价平仓监控] finalizer 未完成（{_msg_f}），"
                          f"保持轮询接管")
                    continue

                elif status == 'canceled' or status == 'expired':
                    # 🔥 P5（R8 本源缺陷闭环）：外部取消不再只清字段——进入共享
                    # 四态裁决（含部分成交归属 + 恢复 ACTIVE），与 /closecancel 同路径
                    print(f"⚠️ [限价平仓监控] 批次 {batch_id} 限价单已取消/过期，进入终态裁决...")
                    try:
                        _ok_a, _msg_a = self._adjudicate_closed_limit_close(
                            symbol, batch_id, order_id)
                        print(f"  └─ {'✅' if _ok_a else '⚠️'} [限价裁决] 批次 {batch_id}: {_msg_a}")
                        if not _ok_a:
                            self.send_tg_notification(
                                f"🚨【资金安全】限价平仓单已取消但批次未恢复 批次 `{batch_id}`\n"
                                f"💡 原因: {_msg_a}\n请人工核对，或重试 /closecancel。",
                                level='critical')
                    except Exception as _adj_e:
                        print(f"  └─ ⚠️ [限价裁决] 异常: {_adj_e}")
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