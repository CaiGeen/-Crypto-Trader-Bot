#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""邮件告警发送闸门（纯标准库）。

三条 .env 开关：
  EMAIL_ALERT_ENABLED=true/false              邮件总手动开关（缺省 true，兼容旧行为）
  EMAIL_ALERT_ONLY_WITH_POSITION=true/false   仅在存在活跃批次时发送（缺省 false）
  DAILY_REPORT_EMAIL_ENABLED=true/false       每日结算报告邮件独立开关（缺省 true）

R0 事件维度（2026-09-25，交叉审查 D / F10 修复）：
  should_send_email 新增 event 参数，按事件语义分三层裁决：
    1. 总开关关闭        → 一律不发（reason=disabled）
    2. 资金安全致命事件  → **豁免持仓闸门与状态可读性**（reason=fatal_event）
    3. 每日结算报告      → 由 DAILY_REPORT_EMAIL_ENABLED 独立裁决（reason=daily_report）
    4. 其余通用事件      → 沿用 EMAIL_ALERT_ONLY_WITH_POSITION 持仓闸门
  修复前的致命缺陷：唯一开关同时管辖日报/崩溃/鉴权封锁/健康巡检，
  空仓（或 trade_state.json 不可读）时会把资金安全告警一并吞掉。

持仓判定刻意只读本地 ``trade_state.json``，不调用交易所 API：
  任一 symbol -> batch 的 ``is_active`` 为真即视为“有活跃批次”。
文件缺失、不可解析或结构不符合预期时返回不允许发送（Fail-Silent for generic email），
但本模块不触碰 Telegram，也不触碰交易状态。
"""
import json
import os
import threading
from collections.abc import Mapping

# 资金安全致命事件白名单：无论有无持仓、无论 trade_state.json 是否可读，一律放行。
# 依据：交叉审查 D（be1d44a）F10/F11 —— 巡检与崩溃链过去正是被持仓闸门静默吞掉。
FATAL_EVENTS = frozenset({
    "critical",            # trader 资金安全级告警（含 AUTH_BLOCKED 触发的 critical）
    "auth_blocked",        # 鉴权封锁 / 盲区安全模式
    "crash",               # Watchdog 崩溃报警
    "startup_breaker",     # Watchdog 启动熔断（已停止自动重启）
    "health",              # 健康巡检（心跳陈旧 / bot 失活 / 进度停滞 / 代理不可达）
    "liquidation_proximity",  # 距强平过近（R2 预留）
    "protection_lost",     # 保护单确认失效（R2 预留）
})

# 每日结算报告：固定运营通知，不受持仓闸门约束
DAILY_REPORT_EVENT = "daily_report"

# 已知事件全集（含通用与非致命事件）。用于识别**拼写错误**的 event：
# 致命事件拼错会静默降级为持仓闸门 → 空仓时被吞，且日志里只能看到模糊的
# no_active_positions，难以定位。仅告警，不改变裁决语义（保持向后兼容）。
KNOWN_EVENTS = FATAL_EVENTS | {
    "generic",
    DAILY_REPORT_EVENT,
    "ip_change",
    "selftest",
    "unknown",
}

DEFAULT_STATE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "trade_state.json"
)

_TRUE_VALUES = {"1", "true", "yes", "on", "y"}
_FALSE_VALUES = {"0", "false", "no", "off", "n"}


def _env_value(env, key: str, default: str = "") -> str:
    if env is None:
        return os.getenv(key, default)
    if isinstance(env, Mapping):
        value = env.get(key, default)
    else:
        value = getattr(env, "get", lambda _key, _default: default)(key, default)
    return default if value is None else str(value)


def _as_bool(value, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _TRUE_VALUES:
        return True
    if text in _FALSE_VALUES:
        return False
    return default


def email_enabled(env=None) -> bool:
    """邮件总手动开关；未配置时保持旧行为（发送）。"""
    return _as_bool(_env_value(env, "EMAIL_ALERT_ENABLED", "true"), True)


def email_only_with_position(env=None) -> bool:
    """是否只在存在活跃批次时发送邮件；未配置时保持旧行为（不加持仓闸）。"""
    return _as_bool(
        _env_value(env, "EMAIL_ALERT_ONLY_WITH_POSITION", "false"), False
    )


def daily_report_email_enabled(env=None) -> bool:
    """每日结算报告邮件独立开关（R0 新增，缺省 true）。

    日报是固定运营通知，不应被 EMAIL_ALERT_ONLY_WITH_POSITION 静默 —— 否则空仓期
    交易员会完全失去「昨日无操作 / 余额多少 / 机器是否待命」这条唯一留痕。
    """
    return _as_bool(
        _env_value(env, "DAILY_REPORT_EMAIL_ENABLED", "true"), True
    )


def count_active_positions(state_path: str | None = None, env=None) -> int | None:
    """统计本地状态中的活跃批次数；无法读取时返回 None。"""
    path = state_path or DEFAULT_STATE_FILE
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return None
        count = 0
        for symbol_batches in data.values():
            if not isinstance(symbol_batches, dict):
                continue
            for batch in symbol_batches.values():
                if isinstance(batch, dict) and batch.get("is_active"):
                    count += 1
        return count
    except Exception:
        return None


def should_send_email(env=None, state_path: str | None = None,
                      event: str = "generic") -> tuple[bool, str]:
    """返回 ``(是否允许发送, 原因码)``。

    R0 事件维度（2026-09-25）：
      event ∈ FATAL_EVENTS  → 资金安全致命事件，**豁免持仓闸门与状态可读性**
      event == "daily_report" → 由 DAILY_REPORT_EMAIL_ENABLED 独立裁决
      其余（generic）        → 沿用 EMAIL_ALERT_ONLY_WITH_POSITION 持仓闸门

    原因码：
      disabled / daily_report_disabled / fatal_event / daily_report /
      position_gate_disabled / state_unreadable / no_active_positions /
      active_positions
    """
    if not email_enabled(env):
        return False, "disabled"

    ev = str(event or "generic").strip().lower()
    if ev not in KNOWN_EVENTS:
        # D7：拼写错误的致命事件会静默降级为持仓闸门（空仓即被吞），
        # 且日志只会显示 no_active_positions，难以定位 —— 必须让原因可读。
        print(f"⚠️ [邮件闸门] 未知 event={ev!r}，按普通事件(持仓闸门)裁决；"
              f"若属资金安全事件请补入 email_gate.FATAL_EVENTS")
    if ev in FATAL_EVENTS:
        # 致命事件：持仓与状态可读性都不参与裁决（无持仓 ≠ 无风险；状态文件坏 ≠ 无风险）
        return True, "fatal_event"
    if ev == DAILY_REPORT_EVENT:
        if not daily_report_email_enabled(env):
            return False, "daily_report_disabled"
        return True, "daily_report"

    if not email_only_with_position(env):
        return True, "position_gate_disabled"

    count = count_active_positions(state_path, env)
    if count is None:
        return False, "state_unreadable"
    if count <= 0:
        return False, "no_active_positions"
    return True, "active_positions"


# ==================== M1：发送结果三态 + 同进程在途去重 ====================
# 背景（2026-09-25，第七轮复审 / ChatGPT 复核 f0df163）：
#   实盘巡检路径实测一封成功邮件耗时 31s（16:37:23 → 16:37:54），而两条发送路径都用
#   ``t.join(timeout=20)`` 判定「已确认」——于是会出现「先报未确认 → 线程后来成功 →
#   下一轮重试」→ 同一封邮件到达两次，且状态被记成未送达。
#   「把 join 改 35s」只降低概率，不构成修复；缩短 join 反而更容易假未确认。
#   本模块提供两个原语，发送方据此实现：
#     1) 结果三态：失败 / 超时但线程仍在途 / 已确认送达（三者不可混为一谈）；
#     2) 在途去重：同一 (event, subject) 在途未结束时**不启动第二封**，
#        避免重复投递；线程真正结束后槽位自动释放。
#   边界（必须接受的限制）：进程崩溃时在途邮件的最终命运不可知，
#   外部投递无法做到绝对不重复 —— 这不由本模块解决，也不得声称已解决。

MAIL_RESULT_CONFIRMED = "confirmed"                    # SMTP 明确接受
MAIL_RESULT_FAILED = "failed"                          # 线程已结束且发送失败
MAIL_RESULT_TIMEOUT_IN_FLIGHT = "timeout_in_flight"    # join 超时，线程仍在途（不等于失败）
MAIL_RESULT_SUPPRESSED_IN_FLIGHT = "suppressed_in_flight"  # 前一封仍在途，本次未启动
MAIL_RESULT_SKIPPED = "skipped"                        # 被闸门/未配置拦下，未进入 SMTP

_INFLIGHT_LOCK = threading.Lock()
# key -> 发送线程；值为该线程对象，用于「只释放自己」的竞态安全释放
_MAIL_INFLIGHT: dict = {}
# key -> 最近一次终态结果（仅用于观测/测试，不参与裁决）
_MAIL_LAST_RESULT: dict = {}


def mail_content_key(event, subject, text) -> str:
    """内容指纹：同主题但内容不同的告警**绝不能**被判为同一封（ChatGPT 复核 a8db596 阻断项 1）。

    背景：所有 ``event="critical"`` 资金安全告警共用同一主题「🚨 资金安全告警」，
    若只按 (event, subject) 占槽，第一封在途期间会把**内容不同的**后续资金安全告警
    一并抑制，且无补发安排 → 不同事件失去邮件兜底。
    """
    import hashlib
    payload = f"{subject or ''}\n{text or ''}".encode("utf-8", "replace")
    return "content:" + hashlib.sha1(payload).hexdigest()[:16]


def mail_send_key(event, subject, dedup_key=None, text=None) -> tuple:
    """在途去重的键。

    - ``dedup_key`` 给了（调用方持有稳定身份，如崩溃队列的 event_id）→ 用它；
    - 否则退回**内容指纹**（主题+正文哈希）——默认即安全：内容不同就不算同一封。
    """
    ev = str(event or "generic").strip().lower()
    if dedup_key:
        return (ev, "dk:" + str(dedup_key))
    return (ev, mail_content_key(event, subject, text))


def claim_mail_slot(key, thread_obj) -> bool:
    """尝试占用在途槽位；已被占用返回 False（调用方**不得**再启动发送线程）。"""
    with _INFLIGHT_LOCK:
        if key in _MAIL_INFLIGHT:
            return False
        _MAIL_INFLIGHT[key] = thread_obj
        return True


def release_mail_slot(key, thread_obj=None) -> None:
    """释放槽位（只释放与 thread_obj 匹配的那一个，防误释放别人的槽）。"""
    with _INFLIGHT_LOCK:
        current = _MAIL_INFLIGHT.get(key)
        if current is None:
            return
        if thread_obj is not None and current is not thread_obj:
            return
        _MAIL_INFLIGHT.pop(key, None)


def mail_in_flight(key) -> bool:
    """该 key 当前是否有仍在途的发送。"""
    with _INFLIGHT_LOCK:
        return key in _MAIL_INFLIGHT


def record_mail_result(key, result: str) -> None:
    """记录最近一次结果，供日志/测试观测。"""
    with _INFLIGHT_LOCK:
        _MAIL_LAST_RESULT[key] = result


def mail_last_result(key):
    with _INFLIGHT_LOCK:
        return _MAIL_LAST_RESULT.get(key)


def mail_last_result_for(event, subject, dedup_key=None, text=None):
    """按与发送方相同的规则取最近一次结果（供调用方区分失败/超时在途/已确认）。"""
    return mail_last_result(mail_send_key(event, subject, dedup_key, text))


def reset_mail_state_for_test() -> None:
    """仅测试用：清空在途槽位与结果记录。"""
    with _INFLIGHT_LOCK:
        _MAIL_INFLIGHT.clear()
        _MAIL_LAST_RESULT.clear()