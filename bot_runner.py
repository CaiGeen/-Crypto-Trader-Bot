# bot_runner.py
import os
import re
import sys
import json
import time
import uuid
import hashlib
import atexit
import logging
import asyncio
import tempfile
import threading
from datetime import datetime
from dotenv import load_dotenv

# 1. 强制将当前文件所在目录加入 sys.path
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)


# R1 编码保护（Watchdog 安全补丁 v1，D-004 2026-08-20）：
# stdout 为 PIPE（watchdog 接管）时 Python 按本地 ANSI 代码页（cp936）编码，emoji
# print 抛 UnicodeEncodeError——曾使单实例锁拒绝路径死在 print 上（退出码 42 变 1），
# 触发 watchdog 无限重启 + 通知风暴。拒绝/告警路径必须比正常路径更稳定。
# （与 watchdog.py 同名函数一致；两进程独立启动，有意复制而非共享导入）
def make_stdout_crash_safe():
    for _stream in (sys.stdout, sys.stderr):
        try:
            if _stream is not None and hasattr(_stream, 'reconfigure'):
                _stream.reconfigure(errors='replace')
        except Exception:
            pass


make_stdout_crash_safe()

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
from telegram.error import NetworkError, RetryAfter, Conflict, BadRequest

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

# 🔥 MAX_LEVERAGE 统一配置
MAX_LEVERAGE = int(os.getenv("MAX_LEVERAGE", "100"))

# 🔥 D-010 通知事件队列（Batch 1 消费侧，2026-08-28）
# 旧 .notify 单文件 → .notify_queue/ 一事件一文件；计数持久化；3 轮失败 SILENCED；
# Markdown → 纯文本降级；失败路径不再触发汇总刷屏（E4）
# 设计稿：.workbuddy/memory/discussions/D-010_通知链路加固与2015分流_设计确认稿_v3.md（v3.1 实施约束）
NOTIFY_QUEUE_DIR = os.path.join(BASE_DIR, ".notify_queue")
NOTIFY_STATE_FILE = os.path.join(BASE_DIR, ".notify.state.json")
NOTIFY_AUDIT_LOG = os.path.join(BASE_DIR, ".notify_audit.log")
NOTIFY_MAX_ATTEMPTS = 3   # v3.1 C2：每事件最多 3 轮（每轮 Markdown→纯文本两败才计 1 轮）

# 🔥 QQ 邮箱发送串行锁（限制同时最多 1 个 SMTP 连接）
EMAIL_SEND_LOCK = threading.Lock()


def send_email_alert(text: str, subject: str = "交易告警") -> None:
    """发送 QQ 邮箱告警（独立线程异步发送，失败静默，未配置自动跳过）
    供 watchdog 通知通道（崩溃报警等）复用，逻辑与 trader 的 _send_email_alert 一致
    .env 需配置：QQ_MAIL_USER / QQ_MAIL_AUTH_CODE（QQ邮箱授权码）/ QQ_MAIL_TO（可选，默认=发件人）
    """
    mail_user = os.getenv("QQ_MAIL_USER", "").strip()
    mail_code = os.getenv("QQ_MAIL_AUTH_CODE", "").strip()
    mail_to = os.getenv("QQ_MAIL_TO", "").strip() or mail_user
    if not (mail_user and mail_code and mail_to):
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
                logging.info(f"📧 [邮件] 已发送: {subject}")
            except Exception as e:
                logging.warning(f"⚠️ [邮件] 发送失败: {e}")

    threading.Thread(target=_do_send, daemon=True).start()


# ==================== D-010 通知事件队列（Batch 1 消费侧） ====================
# v3.1 实施约束（ChatGPT 复核批准）：
#   C1 进程级原子入队：tmp 写入 → flush → os.replace；防进程崩溃半文件，不承诺断电级 durability
#   C2 有限重试 + 有界重复投递：成功删除窗口崩溃 → 极端重复 ≤1 次；失败 3 轮 → SILENCED 永停
#   C3 queue/state 双向不一致恢复：queue有state无→新建ACTIVE正常发；state有queue无→ORPHAN_STATE_IGNORED 不重发
#   C4 SILENCED 检查先于任何 Telegram 调用；SILENCED 永不自动回 ACTIVE
# event_id = 事件实例身份（文件名/计数键）；content_sha256 = 内容指纹（仅审计，不做计数键、不做去重）

def _generate_notify_event_id() -> str:
    """事件实例身份：{YYYYMMDD_HHMMSS_ffffff}_{uuid4 前 8 hex}，唯一性由随机性保证"""
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f") + "_" + uuid.uuid4().hex[:8]


def _write_notify_event_file(content: str, queue_dir: str | None = None) -> str | None:
    """C1 进程级原子入队：tmp 写入 → flush → os.replace(tmp, final)。
    只有原子替换完成后才视为"已入队"；失败返回 None（调用方自行降级）。"""
    qdir = queue_dir or NOTIFY_QUEUE_DIR
    try:
        os.makedirs(qdir, exist_ok=True)
        event_id = _generate_notify_event_id()
        final_path = os.path.join(qdir, f"{event_id}.notify")
        fd, tmp_path = tempfile.mkstemp(dir=qdir, prefix=".tmp_", suffix=".notify")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
                f.flush()
            os.replace(tmp_path, final_path)
            return event_id
        except Exception:
            try:
                os.remove(tmp_path)
            except Exception:
                pass
            raise
    except Exception as e:
        print(f"⚠️ [D-010] 通知事件入队失败: {e}")
        logging.warning(f"⚠️ [D-010] 通知事件入队失败: {e}")
        return None


def _migrate_legacy_notify(base_dir: str | None = None) -> str | None:
    """场景 6：旧单文件 .notify → .notify_queue/ 一次性迁移（进程启动时调用一次）。
    迁移成功返回 event_id；无旧文件/空文件返回 None。"""
    base = base_dir or BASE_DIR
    legacy = os.path.join(base, ".notify")
    if not os.path.isfile(legacy):
        return None
    try:
        with open(legacy, "r", encoding="utf-8") as f:
            content = f.read().strip()
        if not content:
            try:
                os.remove(legacy)
                print(f"📝 [D-010] 旧 .notify 为空，已直接移除")
            except Exception:
                pass
            return None
        event_id = _write_notify_event_file(content, queue_dir=os.path.join(base, ".notify_queue"))
        if event_id:
            try:
                os.remove(legacy)
                print(f"📝 [D-010] 旧 .notify 已迁移为事件 {event_id} 并移除原文件")
            except Exception as e:
                # 旧文件删除失败：留在原地，下一轮迁移会再入队一次（有界重复，C2 语义）
                print(f"⚠️ [D-010] 旧 .notify 迁移后删除失败（下轮重试迁移）: {e}")
            return event_id
        return None
    except Exception as e:
        print(f"⚠️ [D-010] 旧 .notify 迁移失败（保留原文件）: {e}")
        return None


def _load_notify_state(state_file: str | None = None) -> dict:
    """加载 .notify.state.json。损坏/非 dict → 重置 {}（SILENCED 记忆丢失但有界 3 轮，不崩溃）"""
    path = state_file or NOTIFY_STATE_FILE
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        return data
    except Exception as e:
        print(f"⚠️ [D-010] notify state 损坏，计数重置（最多重试 {NOTIFY_MAX_ATTEMPTS} 轮）: {e}")
        return {}


def _save_notify_state(state: dict, state_file: str | None = None) -> None:
    """原子写 state（tmp → flush → os.replace，与 trade_state.json 同惯例）"""
    path = state_file or NOTIFY_STATE_FILE
    try:
        d = os.path.dirname(path) or "."
        fd, tmp_path = tempfile.mkstemp(dir=d, prefix=".tmp_", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=1)
            f.flush()
        os.replace(tmp_path, path)
    except Exception as e:
        print(f"⚠️ [D-010] notify state 保存失败: {e}")
        try:
            os.remove(tmp_path)
        except Exception:
            pass


def _append_notify_audit(event_id: str, status: str, attempts: int,
                         evidence: str, audit_log: str | None = None) -> None:
    """SILENCED / ORPHAN_STATE_IGNORED 审计留痕：append-only 单行 TSV（D-007 归一预留）。
    best-effort：写失败仅告警不阻塞 state 主流程。"""
    path = audit_log or NOTIFY_AUDIT_LOG
    line = (f"{datetime.now().isoformat(timespec='seconds')}\t{event_id}\t"
            f"{status}\tattempts={attempts}\t{evidence}\n")
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception as e:
        print(f"⚠️ [D-010] audit 写入失败（证据仍在 state/队列文件）: {e}")


async def _send_notify_with_fallback(bot, chat_id: int, text: str) -> bool:
    """Markdown → 失败 → 纯文本（parse_mode=None）重发一次；两败才返回 False。
    与 trader send_tg_notification L346-361 主路径降级语义对齐（v3：纯文本 fallback 为唯一修复）。"""
    try:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode='Markdown')
        return True
    except Exception:
        pass
    try:
        await bot.send_message(chat_id=chat_id, text=text)   # 纯文本
        return True
    except Exception:
        return False


def _parse_notify_content(content: str) -> tuple:
    """解析事件文件内容（沿用旧 .notify 的 type|msg 格式，新旧写入方共用）"""
    if '|' in content:
        parts = content.split('|', 2)
        if len(parts) >= 2:
            return parts[0], parts[1]
    return 'unknown', content


async def _process_notify_queue_once(bot, chat_id: int,
                                     state_file: str | None = None,
                                     queue_dir: str | None = None,
                                     audit_log: str | None = None,
                                     summary_cb=None,
                                     email_cb=None) -> dict:
    """D-010 Batch 1 单轮队列消费（生产循环 10s 周期的单轮体，路径可注入供离线测试）。

    消费顺序（C4 锁死）：读队列 → 读/建 state → SILENCED 则跳过（不调 TG）→ ACTIVE 才发送。
    每轮 = Markdown → 失败 → 纯文本，两败才计 1 轮；3 轮 → SILENCED（保留文件/state/audit）。
    """
    qdir = queue_dir or NOTIFY_QUEUE_DIR
    state = _load_notify_state(state_file)
    stats = {'processed': 0, 'sent': 0, 'failed_rounds': 0, 'silenced': 0, 'skipped_silenced': 0}

    files = []
    if os.path.isdir(qdir):
        # D-010 Batch 2：排除写入中断残留的 tmp 文件（mkstemp prefix=".tmp_" suffix=".notify"
        # 同样以 .notify 结尾，不排除会被误当事件消费——写入端崩溃窗口的半文件）
        files = sorted(f for f in os.listdir(qdir)
                       if f.endswith(".notify") and not f.startswith(".tmp_"))
    queue_ids = {f[:-len(".notify")] for f in files}

    # C3 对偶面：state 有 queue 无（非 SILENCED）→ ORPHAN_STATE_IGNORED，不重发，audit 留痕后清条目
    # （SILENCED 条目按设计永久保留，即使证据文件被人工清理也保持静默记录）
    for eid in list(state.keys()):
        if eid not in queue_ids and isinstance(state[eid], dict) and state[eid].get('status') != 'SILENCED':
            _append_notify_audit(eid, 'ORPHAN_STATE_IGNORED',
                                 state[eid].get('failed_attempts', 0), '-', audit_log)
            print(f"ℹ️ [D-010] ORPHAN_STATE_IGNORED：state 有记录但队列文件不存在，不重发: {eid}")
            logging.info(f"[D-010] ORPHAN_STATE_IGNORED: {eid}")
            state.pop(eid, None)

    for fname in files:
        event_id = fname[:-len(".notify")]
        fpath = os.path.join(qdir, fname)
        evidence = f".notify_queue/{fname}"

        try:
            with open(fpath, "r", encoding="utf-8") as f:
                content = f.read().strip()
        except Exception as e:
            print(f"⚠️ [D-010] 读取事件文件失败（跳过本轮）: {fpath}: {e}")
            continue
        if not content:
            # 原子入队下不应出现半文件；出现即异常残留，清理避免每轮空转
            try:
                os.remove(fpath)
                print(f"📝 [D-010] 空事件文件已清理: {fname}")
            except Exception:
                pass
            continue

        # C3：queue 有 state 无 → 新建 ACTIVE 正常发送
        st = state.get(event_id)
        if not isinstance(st, dict):
            st = {
                'content_sha256': hashlib.sha256(content.encode('utf-8')).hexdigest(),
                'failed_attempts': 0,
                'status': 'ACTIVE',
                'first_seen': datetime.now().isoformat(timespec='seconds'),
                'last_attempt': datetime.now().isoformat(timespec='seconds'),
            }
            state[event_id] = st

        # C4：SILENCED 检查先于任何 Telegram 调用
        if st.get('status') == 'SILENCED':
            print(f"🔇 [D-010] 通知已静默，未删除告警: {event_id}")
            stats['skipped_silenced'] += 1
            continue

        notify_type, notify_msg = _parse_notify_content(content)
        stats['processed'] += 1
        ok = False

        if notify_type == 'summary_restart':
            logging.info("📊 Watchdog 重启，发送持仓汇总")
            if summary_cb is not None:
                try:
                    await summary_cb()
                    ok = True
                except Exception as e:
                    logging.warning(f"⚠️ 汇总通知发送失败: {e}")
        else:
            if notify_type == 'ip_notify':
                text = f"🌐 **IP 地址已变化！**\n\n{notify_msg}"
            elif notify_type == 'crash_alert':
                text = f"💥 **程序崩溃报警！**\n\n{notify_msg}"
            elif notify_type == 'watchdog_alert':
                # D-010 B2（W2/E5 对偶面）：watchdog 通用告警——独立类型语义（不复用
                # crash_alert 的"崩溃/重启"语义，ChatGPT 终审批定）；普通 TG 发送，无 email/汇总副作用
                text = f"🐕 **Watchdog 告警**\n\n{notify_msg}"
            elif notify_type == 'auth_blocked':
                # D-010 B2：AUTH_BLOCKED 三通道的队列事件（TG 已由 trader 直发过时为
                # 崩溃窗口重复投递，C2 语义接受）；普通 TG 发送，无 email 副作用（Email 由 trader critical 级负责）
                text = f"🔒 **鉴权封锁告警（盲区安全模式）**\n\n{notify_msg}"
            else:
                text = f"📨 **通知**\n{notify_msg}"

            ok = await _send_notify_with_fallback(bot, chat_id, text)

            if ok:
                if notify_type == 'ip_notify':
                    logging.info("📨 IP 备用通知已发送")
                elif notify_type == 'crash_alert':
                    logging.info("📨 崩溃报警已发送")
                    # 🔥 崩溃报警同步推送邮箱 + 持仓汇总（成功路径一次性，与旧行为一致）
                    (email_cb or send_email_alert)(f"💥 程序崩溃报警！\n\n{notify_msg}",
                                                   subject="💥 程序崩溃报警")
                    if summary_cb is not None:
                        try:
                            await summary_cb()
                        except Exception as e:
                            logging.warning(f"⚠️ 崩溃后汇总发送失败: {e}")

        if ok:
            # C2 DONE 语义 = 发送成功 + 删除完成；删除失败 → 下轮重发一次（有界重复）
            try:
                os.remove(fpath)
                state.pop(event_id, None)
                print(f"📝 [D-010] 通知已送达并删除事件文件: {fname}")
            except Exception as e:
                print(f"⚠️ [D-010] 删除事件文件失败（下轮将重发一次，有界）: {fname}: {e}")
            stats['sent'] += 1
        else:
            # 失败计轮：state 持久化，重启不重置（C2/C3 语义）
            st['failed_attempts'] = st.get('failed_attempts', 0) + 1
            st['last_attempt'] = datetime.now().isoformat(timespec='seconds')
            stats['failed_rounds'] += 1
            if st['failed_attempts'] >= NOTIFY_MAX_ATTEMPTS:
                st['status'] = 'SILENCED'
                _append_notify_audit(event_id, 'SILENCED', st['failed_attempts'], evidence, audit_log)
                print(f"🔇 [D-010] 通知连续 {st['failed_attempts']} 轮发送失败，已进入 SILENCED"
                      f"（停止重试，未删除告警，证据保留）: {event_id}")
                logging.warning(f"🔇 [D-010] NOTIFY_SILENCED: {event_id} attempts={st['failed_attempts']}"
                                f" evidence={evidence}")
                stats['silenced'] += 1
            else:
                print(f"⚠️ [D-010] 通知发送失败（第 {st['failed_attempts']}/{NOTIFY_MAX_ATTEMPTS} 轮），"
                      f"保留队列与计数: {event_id}")

    _save_notify_state(state, state_file)
    return stats

# 全局异步互斥锁
TRADER_LOCK = asyncio.Lock()

# 💡 用于记录用户的临时交互状态
USER_PENDING_INPUTS = {}

# 🔥 测试命令的确认状态
TEST_CONFIRM_STATE = {}

# ==================== P0-2 单实例锁（防孤儿/双实例并发） ====================
# 背景（2026-08-19 418 事故）：watchdog 手动停止路径不杀 bot_runner 子进程，
# PyCharm Ctrl+F5 重启可能残留孤儿实例 → 多实例并发 = 多倍 API 配额消耗 + 重复挂单
# v2（12:05 修正）：v1 文件锁在 taskkill 强杀（无 atexit）+ PID 复用下误判——
# 改用 Windows 命名互斥体为权威判据（内核对象，进程死亡自动释放，免疫 PID 复用）；
# 锁文件降级为纯诊断（写 PID 供人工排查，永不阻断启动）。
LOCK_FILE = os.path.join(BASE_DIR, ".bot_instance.lock")
BOT_EXIT_INSTANCE_REFUSED = 42  # 专用退出码：watchdog 据此不进入崩溃重启循环

_mutex_handle = None  # 进程存活期间持有互斥体句柄


def _release_instance_lock():
    """释放互斥体并清理诊断锁文件（正常退出时；强杀时内核自动释放互斥体）"""
    global _mutex_handle
    try:
        if _mutex_handle is not None:
            import ctypes
            from ctypes import wintypes
            kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel32.ReleaseMutex.restype = wintypes.BOOL
            kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.ReleaseMutex(_mutex_handle)
            kernel32.CloseHandle(_mutex_handle)
            _mutex_handle = None
    except Exception:
        pass
    try:
        if os.path.exists(LOCK_FILE):
            with open(LOCK_FILE, 'r') as f:
                if f.read().strip() == str(os.getpid()):
                    os.remove(LOCK_FILE)
    except Exception:
        pass


def acquire_instance_lock():
    """P0-2 v2: 单实例锁——互斥体已存在即拒绝启动（Fail-Closed，防 418 重演）
    15:28 修正：ctypes 必须用 use_last_error=True + ctypes.get_last_error()——
    直接调 kernel32.GetLastError() 会被 ctypes 内部调用覆盖（不可靠，实测漏判双实例）；
    CreateMutexW 返回 64 位 HANDLE 需显式 restype，否则句柄截断。"""
    global _mutex_handle
    if sys.platform == 'win32':
        import ctypes
        from ctypes import wintypes
        ERROR_ALREADY_EXISTS = 183
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.CreateMutexW(None, True, "Global\\my_crypto_bot_single_instance")
        err = ctypes.get_last_error()  # 唯一可靠读取方式（kernel32.GetLastError 会被 ctypes 覆盖）
        if err == ERROR_ALREADY_EXISTS:
            if handle:
                kernel32.CloseHandle(handle)
            print("❌ 检测到另一个 Bot 实例正在运行（互斥体已存在），拒绝启动！")
            print("   双实例会多倍消耗 API 配额（418 封禁风险）并可能重复挂单。")
            print("   请先用任务管理器结束已有 python.exe 实例（或通过 watchdog 正常停止）。")
            sys.exit(BOT_EXIT_INSTANCE_REFUSED)
        _mutex_handle = handle
    else:
        # 非 Windows 退化方案：锁文件 + PID 存活检测（本项目实际运行环境为 Windows）
        if os.path.exists(LOCK_FILE):
            try:
                with open(LOCK_FILE, 'r') as f:
                    old_pid = int(f.read().strip())
                os.kill(old_pid, 0)
                print(f"❌ 检测到另一个 Bot 实例正在运行 (PID: {old_pid})，拒绝启动！")
                sys.exit(BOT_EXIT_INSTANCE_REFUSED)
            except (ValueError, ProcessLookupError):
                pass
            except PermissionError:
                print(f"❌ 检测到另一个 Bot 实例正在运行 (PID 不可验证)，拒绝启动！")
                sys.exit(BOT_EXIT_INSTANCE_REFUSED)
    # 诊断锁文件（仅记录 PID 供排查，永不阻断启动）
    try:
        with open(LOCK_FILE, 'w') as f:
            f.write(str(os.getpid()))
    except Exception:
        pass
    atexit.register(_release_instance_lock)


# ==================== 启动安全检查 ====================
def print_system_safety_check(trader=None):
    """打印系统安全检查信息"""
    print("\n" + "=" * 50)
    print("🛡️  SYSTEM SAFETY CHECK")
    print("=" * 50)

    # MAX_LEVERAGE
    max_lev = int(os.getenv("MAX_LEVERAGE", "100"))
    print(f"📊 MAX_LEVERAGE: {max_lev}x ✅")

    # Binance 连接
    try:
        if trader and hasattr(trader, 'exchange'):
            print("📊 Binance API: ✅ 已连接")
    except:
        print("📊 Binance API: ⚠️ 未验证")

    # 活跃批次
    if trader:
        try:
            states = trader.load_all_states()
            active_count = 0
            for symbol, symbol_batches in states.items():
                for b_id, b_data in symbol_batches.items():
                    if b_data.get('is_active'):
                        active_count += 1
            print(f"📊 活跃批次: {active_count} 个")
        except:
            print("📊 活跃批次: ⚠️ 无法读取")

    print("=" * 50 + "\n")


# ==================== Watchdog 支持 ====================
def is_authorized(user_id: int) -> bool:
    if int(user_id) != ALLOWED_USER_ID:
        logging.warning(f"🚫 拦截到未经授权的访问请求，User ID: {user_id}")
        return False
    return True


def _strip_markdown(text: str) -> str:
    """P0 通知可靠性（2026-08-29）：BadRequest 降级纯文本时，剥离加粗与等宽标记。
    只剥 ** 和 ` —— 绝不剥下划线：批次号/文件名含 _ 必须原样可读，
    剥掉会把 batch_20260829_155232_f49f2e 变成 batch20260829155232f49f2e。
    """
    return text.replace('**', '').replace('`', '')


async def safe_reply(update: Update, text: str, parse_mode=None, reply_markup=None):
    """带网络超时重试保护的回复函数"""
    try:
        if update.message:
            return await update.message.reply_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
        elif update.callback_query and update.callback_query.message:
            return await update.callback_query.message.reply_text(text, parse_mode=parse_mode,
                                                                  reply_markup=reply_markup)
    except BadRequest as e:
        # 真错误（Markdown 格式错、消息超长等），必须保留可见性
        logging.error(f"⚠️ 回复消息给 Telegram 失败（BadRequest，请检查消息格式）: {e}")
        # 🔥 P0 通知可靠性（2026-08-29）：请求级错误（如批次号奇数下划线导致的实体未闭合）
        # → 同一消息降级纯文本重发一次，保证告警必达（不变量⑧ Fail-not-Silent）。
        # 与 trader.send_tg_notification 的降级策略对齐；按异常类型判定，不依赖错误文案。
        if parse_mode is not None:
            try:
                plain = _strip_markdown(text)
                sent = None
                if update.message:
                    sent = await update.message.reply_text(plain, reply_markup=reply_markup)
                elif update.callback_query and update.callback_query.message:
                    sent = await update.callback_query.message.reply_text(plain, reply_markup=reply_markup)
                if sent is not None:
                    logging.info("ℹ️ [TG回复] Markdown 解析失败，已降级纯文本发送")
                return sent
            except Exception as e2:
                logging.error(f"⚠️ [TG回复] 纯文本重发失败: {e2}")
    except RetryAfter as e:
        # Flood control 限流，稍后自动恢复，正常现象
        logging.debug(f"ℹ️ Telegram 回复受限（Flood control，稍后自动恢复）: {e}")
    except NetworkError as e:
        # 裸 NetworkError（Bad Gateway/ConnectError 等）网络抖动
        logging.debug(f"ℹ️ Telegram 回复失败（网络抖动，可忽略）: {e}")
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
        "🤖 交易机器人指令\n\n"
        "📊 查询\n"
        "• /status    查看活跃批次状态\n"
        "• /summary   持仓汇总与浮动盈亏\n"
        "• /system    系统运行状态\n\n"
        "📝 下单\n"
        "• /signal Symbol 方向 杠杆 入场价 数量 止损 止盈 初始止损\n"
        "• /test  Symbol 方向 杠杆   生成测试挂单\n\n"
        "🎯 管理\n"
        "• /be 批次号     一键保本\n"
        "• /close 批次号  平仓\n"
        "• /cancel 批次号 取消未成交挂单\n"
        "• /force 指纹码  放行被幂等拦截的重复信号\n"
        "• /auth_reset  解除鉴权封锁（探活→对账→自动解锁）\n\n"
        "💡 发送 JSON 信号或上传 signal.json 也可下单\n"
        "💡 批次卡片上的 [保本] [平仓] [撤单] 按钮可快速操作"
    )
    await safe_reply(update, help_msg, parse_mode=None)


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


async def system_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """查看系统运行状态"""
    if not is_authorized(update.effective_user.id):
        await safe_reply(update, "🚫 未授权的访问！")
        return

    trader = context.bot_data.get('global_trader')
    if trader is None:
        await safe_reply(update, "❌ 交易引擎尚未初始化，请稍后再试。")
        return

    try:
        # 活跃批次数
        all_states = trader.load_all_states()
        active_count = 0
        for sym, symbol_batches in all_states.items():
            for b_id, b_data in symbol_batches.items():
                if b_data.get('is_active'):
                    active_count += 1

        # 运行时长
        start_time = context.bot_data.get('bot_start_time')
        uptime_txt = "刚启动"
        if start_time:
            elapsed_sec = int((datetime.now() - start_time).total_seconds())
            if elapsed_sec < 60:
                uptime_txt = f"{elapsed_sec}秒"
            else:
                hours = elapsed_sec // 3600
                mins = (elapsed_sec % 3600) // 60
                uptime_txt = f"{hours}小时{mins}分"

        # 账户余额
        balance_txt = "获取失败"
        try:
            balance = trader._safe_api_call(trader.exchange.fetch_balance)
            usdt = balance.get('USDT', {})
            free = float(usdt.get('free', 0.0) or 0.0)
            balance_txt = f"{free:.2f} USDT"
        except Exception:
            pass

        # IP 检测开关
        ip_txt = "开启" if trader.IP_CHECK_ENABLED else "关闭"

        msg = (
            f"🖥️ **系统状态**\n"
            f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            f"⏱️ 运行时长: `{uptime_txt}`\n"
            f"💰 可用余额: `{balance_txt}`\n"
            f"📊 活跃批次: `{active_count}`\n"
            f"🌐 IP 检测: `{ip_txt}`"
        )
        await safe_reply(update, msg, parse_mode='Markdown')

    except Exception as e:
        await safe_reply(update, f"❌ 查询系统状态失败: {e}")


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
            ticker = trader._safe_api_call(trader.exchange.fetch_ticker, symbol)  # R6: 收编进保护层
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
                        current_pos = None  # R11: UNKNOWN ≠ EMPTY，查询失败不得当作无持仓

                    # 🔥 如果既没有挂单也没有持仓(已确认)，清理这个无效批次
                    if not has_pending and current_pos is not None and current_pos == 0:
                        trader.clear_batch_state(symbol, batch_id)
                        cleaned_count += 1
                        print(f"🧹 [被动清理] 清理无效批次 {batch_id}")

        if cleaned_count > 0:
            print(f"🧹 [被动清理] 共清理 {cleaned_count} 个无效批次")
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

        last_filled_count = target_b_data.get('last_filled_count', 0)
        target_amounts = target_b_data.get('target_amounts', [])
        current_filled_amount, _ = trader._batch_net_position(target_b_data)  # v6.4：净仓位

        if current_filled_amount <= 0:
            await safe_reply(update, f"⚠️ 批次 `{batch_id}` 尚未建仓，无需平仓。", parse_mode='Markdown')
            return

        try:
            ticker = trader._safe_api_call(trader.exchange.fetch_ticker, target_symbol)  # R6: 收编进保护层
            current_price = float(ticker.get('last') or ticker.get('close') or 0.0)
        except Exception:
            current_price = 0.0

        # 🔥 v6.4 rework：净成本 + cost 比例剩余 fee（与 trader 唯一口径一致，partial 后不再失真）
        _nq, net_cost = trader._batch_net_position(target_b_data)
        total_entry_fee = target_b_data.get('total_entry_fee', 0.0)
        _rr_cost = float(target_b_data.get('realized_reduce_cost', 0.0) or 0.0)
        _gross_cost = net_cost + _rr_cost
        _fee_rem = float(total_entry_fee or 0.0) * net_cost / _gross_cost \
            if _gross_cost > 0 else 0.0
        avg_price = (net_cost + _fee_rem) / current_filled_amount \
            if current_filled_amount > 0 else 0

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
    except BadRequest as e:
        logging.warning(f"⚠️ 应答 callback_query 失败（BadRequest）: {e}")
    except NetworkError as e:
        logging.debug(f"ℹ️ 应答 callback_query 失败（网络抖动）: {e}")
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

    def extract_batch_id(data: str, prefix: str) -> tuple:
        parts = data.split("_")
        remaining = parts[1:] if len(parts) > 1 else []

        if remaining and remaining[0] in ["market", "limit", "custom", "cancel", "confirm"]:
            action_type = remaining[0]
            batch_id = "_".join(remaining[1:]) if len(remaining) > 1 else ""
        else:
            action_type = None
            batch_id = "_".join(remaining) if remaining else ""

        return action_type, batch_id

    # ========== 保本 ==========
    if data.startswith("be_") and not data.startswith(("be_confirm_", "be_cancel_")):
        USER_PENDING_INPUTS.pop(chat_id, None)

        batch_id = data.replace("be_", "")

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

        last_filled_count = target_b_data.get('last_filled_count', 0)
        target_amounts = target_b_data.get('target_amounts', [])
        filled_details = target_b_data.get('filled_details', [])
        total_entry_fee = target_b_data.get('total_entry_fee', 0.0)
        current_filled_amount, _ = trader._batch_net_position(target_b_data)  # v6.4：净仓位

        if current_filled_amount <= 0:
            await update.callback_query.message.reply_text(
                f"⚠️ 批次 `{batch_id}` 尚未建仓，无法计算保本价！",
                parse_mode='Markdown'
            )
            return

        # 🔥 v6.4 rework（Blocker 2）：net_qty + net_cost 成对使用（分母 net 分子 gross = 均价失真，
        # 会直接驱动 BE 止损价格）；剩余 fee 按 cost 比例分摊（与 trader 唯一口径一致）
        net_qty, net_cost = trader._batch_net_position(target_b_data)
        _rr_cost = float(target_b_data.get('realized_reduce_cost', 0.0) or 0.0)
        _gross_cost = net_cost + _rr_cost
        _fee_rem = float(total_entry_fee or 0.0) * net_cost / _gross_cost \
            if _gross_cost > 0 else 0.0
        nominal_avg = net_cost / net_qty
        actual_avg = (net_cost + _fee_rem) / net_qty

        try:
            ticker = trader._safe_api_call(trader.exchange.fetch_ticker, target_symbol)
            current_price = float(ticker.get('last') or ticker.get('close') or 0.0)
        except Exception:
            current_price = 0.0

        side = target_b_data.get('side', 'BUY')
        fee_amount = total_entry_fee
        fee_percent = (fee_amount / (
                    nominal_avg * current_filled_amount)) * 100 if current_filled_amount > 0 and nominal_avg > 0 else 0

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
        else:
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

        all_states = trader.load_all_states()
        b_data = all_states.get(target_symbol, {}).get(batch_id, {})
        if not b_data or not b_data.get('is_active'):
            await update.callback_query.message.reply_text(f"❌ 批次 `{batch_id}` 已不存在或已过期")
            return

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
        USER_PENDING_INPUTS.pop(chat_id, None)

        action_type, batch_id = extract_batch_id(data, "close_")

        if not batch_id:
            parts = data.split("_", 1)
            if len(parts) > 1:
                batch_id = parts[1]
            else:
                await update.callback_query.message.reply_text("❌ 无效的平仓请求")
                return

        if action_type == "cancel":
            await update.callback_query.message.reply_text("❌ 已取消平仓操作。")
            return

        if action_type is None:
            await _show_close_options(update, context, batch_id)
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
            await update.callback_query.message.reply_text(f"❌ 批次 `{batch_id}` 已不存在或已过期")
            return

        last_filled_count = target_b_data.get('last_filled_count', 0)
        target_amounts = target_b_data.get('target_amounts', [])
        current_filled_amount, _ = trader._batch_net_position(target_b_data)  # v6.4：净仓位

        if current_filled_amount <= 0:
            await update.callback_query.message.reply_text(f"⚠️ 批次 `{batch_id}` 尚未建仓，无需平仓。")
            return

        if action_type == "market":
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
            result = await loop.run_in_executor(
                None,
                trader.close_position_limit,
                batch_id,
                None
            )
            if result[0]:
                await update.callback_query.message.reply_text(result[1], parse_mode='Markdown')
            else:
                await update.callback_query.message.reply_text(result[1], parse_mode='Markdown')

        elif action_type == "custom":
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
        USER_PENDING_INPUTS.pop(chat_id, None)

        action_type, batch_id = extract_batch_id(data, "cancel_")

        if not batch_id:
            parts = data.split("_", 1)
            if len(parts) > 1:
                batch_id = parts[1]
            else:
                await update.callback_query.message.reply_text("❌ 无效的撤单请求")
                return

        if action_type == "cancel":
            await update.callback_query.message.reply_text("❌ 已取消撤单操作。")
            return

        if action_type is None:
            await _show_cancel_confirmation(update, context, batch_id)
            return

        if action_type == "confirm":
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

    # ========== 保留但隐藏：改止盈/改止损 ==========
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


async def partial_close_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """🔥 v6.4-P0：/partial <BatchID> <amount>——批次指定部分平仓（最小生产入口）。

    多批次场景下的减仓必须走 bot 指定 batch（App 模糊减 aggregate 无法归属）。"""
    if not is_authorized(update.effective_user.id):
        await safe_reply(update, "🚫 未授权的访问！")
        return

    trader = context.bot_data.get('global_trader')
    if trader is None:
        await safe_reply(update, "❌ 交易引擎尚未初始化，请稍后再试。")
        return

    args = context.args
    if len(args) < 2:
        await safe_reply(update, "❌ 格式错误！正确格式：`/partial <BatchID> <数量>`",
                         parse_mode='Markdown')
        return

    batch_id = args[0]
    try:
        amount = float(args[1])
    except ValueError:
        await safe_reply(update, "❌ 数量必须为数字。")
        return

    all_states = trader.load_all_states()
    target_symbol = None
    for symbol, symbol_batches in all_states.items():
        if batch_id in symbol_batches and symbol_batches[batch_id].get('is_active'):
            target_symbol = symbol
            break

    if target_symbol is None:
        await safe_reply(update, f"❌ 未找到活跃批次 `{batch_id}`", parse_mode='Markdown')
        return

    loop = asyncio.get_running_loop()  # 🔥 v6.4 rework（Blocker 1）：与其他 handler 同款取得 running loop
    result = await loop.run_in_executor(
        None,
        trader._execute_partial_close,
        target_symbol,
        batch_id,
        amount
    )
    await safe_reply(update, result[1], parse_mode='Markdown')


async def _show_close_options(update: Update, context: ContextTypes.DEFAULT_TYPE, batch_id: str):
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
    current_filled_amount, _ = trader._batch_net_position(target_b_data)  # v6.4：净仓位

    if current_filled_amount <= 0:
        await update.callback_query.message.reply_text(f"⚠️ 批次 `{batch_id}` 尚未建仓，无需平仓。")
        return

    try:
        ticker = trader._safe_api_call(trader.exchange.fetch_ticker, target_symbol)
        current_price = float(ticker.get('last') or ticker.get('close') or 0.0)
    except Exception:
        current_price = 0.0

    # 🔥 v6.4 rework：净成本 + cost 比例剩余 fee（与 trader 唯一口径一致）
    _nq, net_cost = trader._batch_net_position(target_b_data)
    total_entry_fee = target_b_data.get('total_entry_fee', 0.0)
    _rr_cost = float(target_b_data.get('realized_reduce_cost', 0.0) or 0.0)
    _gross_cost = net_cost + _rr_cost
    _fee_rem = float(total_entry_fee or 0.0) * net_cost / _gross_cost \
        if _gross_cost > 0 else 0.0
    avg_price = (net_cost + _fee_rem) / current_filled_amount \
        if current_filled_amount > 0 else 0

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

    # ========== 检查是否为改止盈/改止损的等待输入 ==========
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
                                 "或使用 `/signal` 指令。")
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
                "`/signal BTCUSDT SELL 100 65100,65000,64900,64800 0.001,0.001,0.001,0.001 65500,65450,65400,65350 64000 65700`",
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


# ==================== D-005: 信号入口幂等去重（2026-08-27） ====================
# 背景：parser 每次解析重新生成 batch_id（parser.py L129 时间戳+uuid）→ 同一信号
# 重发 / 快捷指令双击 = 全新 batch_id → _check_existing_conflicts 批次冲突检查必然
# 不命中 → 走加仓模式重复开仓（实证）。三条入口（JSON 消息 / /signal 快捷指令 /
# 文件触发）全部汇聚 run_trader_execution，此处是唯一咽喉。
# 设计（GLM 初稿 + ChatGPT 交叉审 + 两处反驳定稿）：
#   - 指纹 = 信号全字段 dump 剔除 batch_id（每次重生成必须排除；未来新字段自动入哈希）
#   - 状态只有 EXECUTING / SUCCESS，无 FAILED——execute_signal 的 except 兜底直接
#     return None 且不清理已挂单（trader L2457-2461），"干净失败"与"部分成交后异常"
#     返回值不可分，置 FAILED 允许重发会造成仓位翻倍。改用 10 分钟时间窗自解 +
#     /force 人工放行（放行前提示核对交易所挂单）= Fail-Closed but not Fail-Stuck
#   - 每次操作 load→modify→tmp+os.replace 原子写，不留内存唯一状态（重启/多进程安全）
#   - dedup 是 best-effort 防误触防线，文件损坏时降级为空表放行；SG1/SG2 仍是硬安全闸门
DEDUP_FILE = os.path.join(BASE_DIR, "signal_dedup.json")
SIGNAL_DEDUP_WINDOW_SEC = 600           # 同指纹拦截窗口（秒）
SIGNAL_DEDUP_RETENTION_SEC = 72 * 3600  # 记录保留时长（事故回溯）
FORCE_APPROVAL_TTL_SEC = 300            # /force 放行有效期（秒）


def _signal_fingerprint(signal) -> str:
    """D-005: 信号内容指纹（sha256）。全字段参与、剔除 batch_id——batch_id 每次
    解析重新生成，入哈希会令去重失效；future 新字段自动纳入。"""
    payload = {
        'symbol': str(signal.symbol),
        'side': str(signal.side).upper(),
        'leverage': signal.leverage,
        'entries': [[float(p), float(a)] for p, a in (signal.entries or [])],
        'stop_loss_steps': [float(x) for x in (signal.stop_loss_steps or [])],
        'take_profit': float(signal.take_profit),
        'initial_stop_loss': float(signal.initial_stop_loss),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode('utf-8')).hexdigest()


class DedupCorruptedError(Exception):
    """D-005 补丁：signal_dedup.json 存在但不可读/非法。安全组件故障 → Fail-Closed：
    阻断新开仓并告警，绝不静默降级为空表（SG1 哲学：未知状态 ≠ 允许）。"""


def _load_dedup(path=None, now=None) -> dict:
    """加载去重表并清理超过保留期的记录。

    - 文件不存在：正常（首次运行/已清理）→ 空表
    - 文件存在但读不出 / JSON 非法 / 根节点非 dict：抛 DedupCorruptedError
      （安全组件损坏必须 Fail-Closed，且不得用新表覆盖损坏文件消灭证据）
    """
    now = time.time() if now is None else now
    p = path or DEDUP_FILE
    data = {}
    if os.path.exists(p):
        try:
            with open(p, 'r', encoding='utf-8') as f:
                loaded = json.load(f)
        except Exception as e:
            raise DedupCorruptedError(f"{p} 读取/解析失败: {e}")
        if not isinstance(loaded, dict):
            raise DedupCorruptedError(f"{p} 根节点不是 JSON 对象: {type(loaded).__name__}")
        data = loaded
    # 保留期清理（72h）
    pruned = {k: v for k, v in data.items()
              if isinstance(v, dict) and (now - v.get('last_seen', 0)) < SIGNAL_DEDUP_RETENTION_SEC}
    return pruned


def _save_dedup(data: dict, path=None) -> None:
    """原子写（tmp + os.replace）。调用方均在 TRADER_LOCK 内的事件循环同步段执行，
    无 await 切点 → 无并发写竞争。"""
    p = path or DEDUP_FILE
    tmp = p + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def _check_and_record_dedup(fingerprint: str, now=None, path=None):
    """检查并记录（一次完成）。返回 (allowed: bool, info: str)。
    - approved 有效期内：清除标记放行本次（一次性人工放行）
    - 同指纹 last_seen 距今 < 窗口：拒绝
    - 其余（首次 / 窗口已过）：记录 EXECUTING 并放行
    注意：尝试即记录 EXECUTING（非成功才记）——TRADER_LOCK 内双击的第二个任务
    会看到第一个任务的 EXECUTING 而被拦。
    损坏态：_load_dedup 抛 DedupCorruptedError → (False, "CORRUPT: ...")，
    不写表不覆盖，由调用方负责响亮告警。"""
    now = time.time() if now is None else now
    try:
        data = _load_dedup(path, now)
    except DedupCorruptedError as e:
        print(f"🚨 [D-005] 去重表损坏，新开仓 Fail-Closed 阻断: {e}")
        return False, f"CORRUPT: {e}"
    rec = data.get(fingerprint)
    if rec is not None:
        approved_ts = rec.get('approved_ts')
        if rec.get('approved') and approved_ts is not None \
                and (now - approved_ts) <= FORCE_APPROVAL_TTL_SEC:
            rec['approved'] = False
            rec['approved_ts'] = None
            rec['status'] = 'EXECUTING'
            rec['last_seen'] = now
            _save_dedup(data, path)
            return True, 'force-approved'
        age = now - rec.get('last_seen', 0)
        if age < SIGNAL_DEDUP_WINDOW_SEC:
            remain = int(SIGNAL_DEDUP_WINDOW_SEC - age)
            info = (f"上次执行 {int(age)} 秒前（batch: {rec.get('batch_id') or '未知'}，"
                    f"状态 {rec.get('status')}），拦截窗口剩 {remain} 秒")
            return False, info
    data[fingerprint] = {
        'status': 'EXECUTING', 'first_seen': now, 'last_seen': now,
        'batch_id': None, 'approved': False, 'approved_ts': None,
    }
    _save_dedup(data, path)
    return True, 'first-seen' if rec is None else 'window-expired'


def _mark_dedup_result(fingerprint: str, batch_id, now=None, path=None) -> None:
    """执行结束回写结果。batch_id 非空 → SUCCESS；None → 保持 EXECUTING
    （干净失败与部分成交不可分，不置 FAILED，靠时间窗自解 + /force 人工裁决）。
    损坏态：best-effort 跳过记账（闸门是执行点、此处只是记账；且绝不能让
    损坏异常掩盖 execute_signal 已成功的回执路径）。"""
    now = time.time() if now is None else now
    try:
        data = _load_dedup(path, now)
    except DedupCorruptedError as e:
        print(f"⚠️ [D-005] 回写时去重表损坏，跳过记账（不影响本次执行结果）: {e}")
        return
    rec = data.get(fingerprint)
    if rec is None:
        rec = {'first_seen': now, 'approved': False, 'approved_ts': None}
        data[fingerprint] = rec
    rec['status'] = 'SUCCESS' if batch_id else 'EXECUTING'
    rec['batch_id'] = batch_id
    rec['last_seen'] = now
    _save_dedup(data, path)


def _approve_dedup_force(short_id: str, now=None, path=None):
    """/force 放行：按指纹前缀匹配（≥4 位、必须唯一），打一次性 approved 标记。
    返回 (ok: bool, msg: str)。"""
    now = time.time() if now is None else now
    short_id = (short_id or '').strip().lower()
    if len(short_id) < 4:
        return False, "指纹短码至少需要 4 位十六进制字符"
    try:
        data = _load_dedup(path, now)
    except DedupCorruptedError as e:
        return False, (f"去重表损坏，/force 不可用（安全组件 Fail-Closed）。"
                       f"请先删除或修复 signal_dedup.json: {e}")
    matches = [k for k in data if k.lower().startswith(short_id)]
    if not matches:
        return False, f"72 小时内未找到指纹 {short_id} 对应的信号记录"
    if len(matches) > 1:
        return False, f"指纹 {short_id} 前缀匹配到 {len(matches)} 条记录，请提供更长前缀"
    rec = data[matches[0]]
    rec['approved'] = True
    rec['approved_ts'] = now
    _save_dedup(data, path)
    return True, matches[0][:8]


async def run_trader_execution(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with TRADER_LOCK:
        try:
            loop = asyncio.get_running_loop()
            trader = context.bot_data.get('global_trader')
            if trader is None:
                # 🔥 降级：如果 global_trader 尚未初始化（极端情况），临时创建
                api_key = os.getenv("BINANCE_API_KEY")
                secret = os.getenv("BINANCE_SECRET")
                proxy_url = os.getenv("BINANCE_PROXY")
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

            # SG1: B 层用户体验提示（安全边界在 trader.execute_signal；临时降级实例未跑恢复同样被拦）
            if trader is not None and not trader._ready:
                print(f"🚫 [SG1] 信号未执行（系统未就绪）: {trader._not_ready_reason}")
                await safe_reply(update,
                                 f"🚫 **信号未执行**：系统未就绪\n"
                                 f"原因: `{trader._not_ready_reason}`\n"
                                 f"历史批次恢复完成前禁止新建仓位，请稍后重新发送信号。",
                                 parse_mode='Markdown')
                return

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

            # 🔥 D-005: 幂等去重闸门——在 batch 创建之前拦截重复信号（唯一咽喉，
            # 三条入口全覆盖；TRADER_LOCK 保证串行，原子写无锁竞争）
            fingerprint = _signal_fingerprint(signal)
            allowed, dedup_info = _check_and_record_dedup(fingerprint)
            if not allowed and str(dedup_info).startswith("CORRUPT"):
                # 🔥 D-005 补丁：安全组件损坏 → Fail-Closed（仅阻断新开仓路径；
                # /tp /sl /close 与监控线程不经过此闸门，已有仓位管理不受影响）
                print(f"🚨 [D-005] 去重表损坏，信号已被 Fail-Closed 阻断: {dedup_info}")
                await safe_reply(
                    update,
                    f"🚨 **【资金安全】信号去重表损坏，新开仓已全部阻断**\n\n"
                    f"🧬 `{fingerprint[:8]}` 信号被拒（防重复开仓防线故障）\n"
                    f"❗ `{DEDUP_FILE}` 无法读取\n\n"
                    f"🛠 **恢复方法**：删除或修复 `signal_dedup.json`，"
                    f"下一条信号自动恢复，**无需重启进程**\n"
                    f"ℹ️ 期间已有仓位的止盈/止损/平仓管理不受影响",
                    parse_mode='Markdown')
                return
            if not allowed:
                short_id = fingerprint[:8]
                print(f"🚫 [D-005] 重复信号已拦截 [{short_id}]: {dedup_info}")
                await safe_reply(
                    update,
                    f"🛡 **重复信号已拦截**（D-005 幂等保护）\n\n"
                    f"🧬 指纹：`{short_id}`\n"
                    f"📊 {dedup_info}\n\n"
                    f"💡 同参数信号 {SIGNAL_DEDUP_WINDOW_SEC // 60} 分钟内视为重复"
                    f"（防快捷指令双击/信号重发导致重复开仓）。\n"
                    f"如确需再次开仓：\n"
                    f"1️⃣ 先核对交易所当前挂单与持仓（防上次执行部分成交）\n"
                    f"2️⃣ 发送 `/force {short_id}` 放行\n"
                    f"3️⃣ 在 {FORCE_APPROVAL_TTL_SEC // 60} 分钟内重发原信号",
                    parse_mode='Markdown')
                return

            batch_id = await loop.run_in_executor(None, trader.execute_signal, signal)
            # D-005: 执行结束回写（None 不置 FAILED——干净失败与部分成交不可分，
            # 保持 EXECUTING 由时间窗自解，防部分成交后重发翻倍仓位）
            _mark_dedup_result(fingerprint, batch_id)

            if batch_id:
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
                        f"📊 所有 {original_layers} 层触发价均不符合逻辑\n"
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
                print(f"\n❌ execute_signal 返回 None")
                await safe_reply(update, "⚠️ **挂单未执行**（可能触发防冲突机制或资金校验未通过）。",
                                 parse_mode='Markdown')

        except Exception as e:
            logging.exception("🚨 交易引擎运行崩溃:")
            print(f"\n❌ 异常详情: {e}")
            await safe_reply(update, f"🚨 **挂单失败/引擎异常**:\n`{str(e)}`", parse_mode='Markdown')


async def force_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """D-005: 人工放行被幂等去重拦截的信号。不直接执行任何交易——只打一次性
    approved 标记，执行仍必须走 run_trader_execution 唯一入口（防第二执行路径）。"""
    if not is_authorized(update.effective_user.id):
        await safe_reply(update, "🚫 未授权的访问！")
        return
    try:
        if not context.args or not context.args[0]:
            await safe_reply(
                update,
                "用法：`/force <指纹短码>`\n"
                "（短码见重复信号拦截提示，至少 4 位）",
                parse_mode='Markdown')
            return
        ok, msg = _approve_dedup_force(context.args[0])
        if ok:
            await safe_reply(
                update,
                f"✅ 已放行信号指纹 `{msg}`（{FORCE_APPROVAL_TTL_SEC // 60} 分钟内有效，仅一次）。\n\n"
                f"⚠️ 请确认已核对交易所当前挂单与持仓（防上次执行部分成交）。\n"
                f"👉 现在请重发原信号。",
                parse_mode='Markdown')
        else:
            await safe_reply(update, f"❌ 放行失败：{msg}", parse_mode='Markdown')
    except Exception as e:
        await safe_reply(update, f"❌ /force 处理失败: {e}")


async def auth_reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """D-010 B1：AUTH_BLOCKED 人工解除——唯一命令入口。
    走 BLIND-SAFE 恢复链（probe → RECOVERING → reconcile → clear，顺序锁死 Fail-Closed，
    trader._attempt_auth_recovery 实现），绝不直接清锁文件。
    单飞由 trader._auth_recovery_lock 保证（与启动探活互斥，防并发探活）。"""
    if not is_authorized(update.effective_user.id):
        await safe_reply(update, "🚫 未授权的访问！")
        return
    trader = context.bot_data.get('global_trader')
    if trader is None:
        await safe_reply(update, "❌ trader 未初始化，无法执行恢复")
        return
    await safe_reply(update, "🔄 开始鉴权恢复：探活（fetch_balance 单次）→ 对账 → 解锁…")
    try:
        loop = asyncio.get_running_loop()
        ok, msg = await loop.run_in_executor(None, trader._attempt_auth_recovery)
    except Exception as e:
        await safe_reply(update, f"❌ /auth_reset 执行异常: {e}")
        return
    if ok:
        await safe_reply(update, f"✅ {msg}", parse_mode='Markdown')
    else:
        await safe_reply(update, f"🔒 {msg}", parse_mode='Markdown')


async def run_trader_recovery_on_startup(trader: CryptoTrader):
    async with TRADER_LOCK:
        # 🔥 D-010 B3：启动探活（每次重启最多 1 次）——仅当 auth_blocked.json 显示锁定时执行。
        # 探活 = auth_probe=True 的 fetch_balance（不变量⑨唯一放行路径之一）；
        # 走 BLIND-SAFE 恢复链 probe → RECOVERING → reconcile → clear（顺序锁死 Fail-Closed），
        # 失败保持锁并告警（恢复链内部三通道），绝不半恢复
        try:
            auth = trader._load_auth_state()
            if auth.get('locked'):
                print(f"🔒 [D-010] 检测到 AUTH_BLOCKED 持久锁（状态={auth.get('state')}），执行启动探活恢复链…")
                loop = asyncio.get_running_loop()
                ok, msg = await loop.run_in_executor(None, trader._attempt_auth_recovery)
                if not ok:
                    trader._not_ready_reason = f"鉴权未恢复，保持盲区安全模式：{str(msg)[:150]}"
                    # 告警已由恢复链内部三通道发出（Fail-Closed but not Fail-Silent）
                    return
                print(f"✅ [D-010] 启动恢复链完成: {msg}")
        except Exception as e:
            print(f"⚠️ [D-010] 启动探活检查异常（不阻断常规恢复）: {e}")

        for attempt in range(3):
            try:
                loop = asyncio.get_running_loop()
                # B2-4: 启动校验硬锁与解锁审计（规格 §5.5 + 重启恢复表 §6.2）——
                # 必须在恢复前执行（恢复逻辑读 registry，需先保证硬锁状态正确）；
                # 校验异常不阻断恢复（Fail-Closed 由恢复本身兜底）
                try:
                    await loop.run_in_executor(
                        None, trader._validate_registry_locks_on_startup)
                except Exception as lock_e:
                    print(f"⚠️ [启动检测] 硬锁校验异常（不阻断恢复）: {lock_e}")
                recovery_result = await loop.run_in_executor(None, trader.recover_active_batches)
                if recovery_result:
                    trader._ready = True        # SG1: 唯一置位来源 = recover 明确返回 True（含 0 批次）
                    trader._not_ready_reason = ""
                    print("✅ [启动检测] 历史任务恢复校验完成！系统 READY")
                    return
                else:
                    # R3: 恢复失败不得报告成功，必须显式告警（Phase A: 让失败可见）
                    # D-009：账本损坏时 recover 已写入精确原因（含读取错误），
                    # 此处不得用泛化的"健康检查未通过"覆盖，否则运维会误查网络方向。
                    # ⚠️ 严格 is not True 判定：trader 可能是 MagicMock（测试/降级实例），
                    # 未绑定属性会返回 MagicMock——它恒 truthy，若用 `not getattr(...)`
                    # 会误判为"已损坏"而吞掉本条原因更新（SG1 场景4 回归实证）。
                    if getattr(trader, '_state_corrupted', False) is not True:
                        trader._not_ready_reason = "恢复失败：交易所健康检查未通过（recover 返回 False）"
                    print("🚨 [启动检测] 历史任务恢复失败！recover_active_batches 返回 False")
                    try:
                        trader.send_tg_notification(
                            "🚨【资金安全】启动恢复失败！recover_active_batches 返回 False\n"
                            "历史批次可能未正确恢复，请立即检查持仓和止损状态！",
                            level='critical'
                        )
                    except Exception as tg_e:
                        print(f"⚠️ TG 告警发送失败: {tg_e}")
                    return
            except Exception as e:
                if attempt < 2:
                    print(f"⚠️ [启动检测] 第 {attempt + 1} 次恢复失败，等待 10 秒后重试: {e}")
                    await asyncio.sleep(10)
                else:
                    logging.exception(f"⚠️ 检查历史活跃任务失败 (已重试 3 次): {e}")
                    trader._not_ready_reason = f"恢复异常：重试 3 次耗尽（{str(e)[:100]}）"
                    # R3-v2: 重试耗尽不得静默，必须显式告警（不变量⑧ Fail-Closed but not Fail-Silent）
                    try:
                        trader.send_tg_notification(
                            "🚨【资金安全】启动恢复异常！recover_active_batches 连续 3 次抛出异常\n"
                            f"最后一次错误: {str(e)[:200]}\n"
                            "历史批次状态未知，请立即检查持仓和止损！",
                            level='critical'
                        )
                    except Exception as tg_e:
                        print(f"⚠️ TG 告警发送失败: {tg_e}")


async def send_summary_notification(app: Application):
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


# ==================== on_post_init ====================
async def on_post_init(app: Application):
    try:
        api_key = os.getenv("BINANCE_API_KEY")
        secret = os.getenv("BINANCE_SECRET")
        proxy_url = os.getenv("BINANCE_PROXY")
        loop = asyncio.get_running_loop()

        # 🔥 记录启动时间（供 /system 查询运行时长）
        app.bot_data['bot_start_time'] = datetime.now()

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

        async def process_notifications():
            # 🔥 增加启动等待，让系统稳定
            await asyncio.sleep(5)

            # 🔥 W2 修复（D-002）：由一次性执行改为定时轮询（保留历史，D-010 在此基础上队列化）
            # 🔥 D-010 场景 6：旧单文件 .notify → .notify_queue/ 一次性迁移
            _migrate_legacy_notify()

            while True:
                try:
                    await _process_notify_queue_once(
                        app.bot, ALLOWED_USER_ID,
                        summary_cb=lambda: send_summary_notification(app),
                    )
                except Exception as e:
                    # 🔥 E4 修复（D-010）：兜底失败只记录，不再调用 send_summary_notification——
                    # 错误处理路径不得产生新的通知副作用（8-28 事故：通知链路故障时每 10s 刷汇总）
                    logging.warning(f"⚠️ 处理通知队列失败: {e}")

                # 🔥 轮询间隔 10 秒
                await asyncio.sleep(10)

        asyncio.create_task(process_notifications())

        # 🔥 打印安全检查
        print_system_safety_check(global_trader)

    except Exception as e:
        logging.error(f"⚠️ 启动时初始化全局 trader 失败: {e}")


# ==================== 全局错误处理器 ====================
async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    error = context.error

    # BadRequest 是 NetworkError 子类（库的命名怪癖），但它是真错误（格式错/消息超长等），必须保留可见性
    if isinstance(error, BadRequest):
        logging.error("❌ Telegram BadRequest（真错误）:", exc_info=error)
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
        return

    # 裸 NetworkError（Bad Gateway / ConnectError 等；TimedOut 也是其子类，一并覆盖）
    # 由 python-telegram-bot 库的 network_retry_loop 自动无限重试，属正常网络抖动
    if isinstance(error, NetworkError):
        logging.debug(f"ℹ️ Telegram 网络抖动（库自动重试）: {error}")
        return

    if isinstance(error, RetryAfter):
        logging.warning(f"⚠️ Telegram 请求受限（Flood control）: {error}")
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
    acquire_instance_lock()  # P0-2: 单实例锁，双实例直接拒绝启动

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

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("test", test_command))
    app.add_handler(CommandHandler("signal", handle_quick_signal))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("summary", summary_command))
    app.add_handler(CommandHandler("system", system_command))

    app.add_handler(CommandHandler("be", be_command))
    app.add_handler(CommandHandler("close", close_command))
    app.add_handler(CommandHandler("partial", partial_close_command))  # 🔥 v6.4-P0
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CommandHandler("force", force_command))
    # 🔥 D-010 B1：AUTH_BLOCKED 人工解除命令（BLIND-SAFE 恢复链唯一命令入口）
    app.add_handler(CommandHandler("auth_reset", auth_reset_command))

    app.add_handler(CommandHandler("tp", tp_command))
    app.add_handler(CommandHandler("sl", sl_command))

    app.add_handler(CallbackQueryHandler(button_callback_handler))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_json_or_pending_input))

    print("🚀 Telegram Bot 监听服务已启动...")
    app.run_polling(bootstrap_retries=-1)


if __name__ == "__main__":
    main()