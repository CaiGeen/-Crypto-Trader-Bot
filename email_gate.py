#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""邮件告警发送闸门（纯标准库）。

三条 .env 开关：
  EMAIL_ALERT_ENABLED=true/false              邮件总手动开关（缺省 true，兼容旧行为）
  EMAIL_ALERT_ONLY_WITH_POSITION=true/false   旧配置兼容保留，不再放行普通邮件
  DAILY_REPORT_EMAIL_ENABLED=true/false       每日结算报告邮件独立开关（缺省 true）

R0 事件维度（2026-09-25，交叉审查 D / F10 修复）：
  should_send_email 按明确事件语义裁决（2026-10-12 收紧普通邮件）：
    1. 总开关关闭        → 一律不发（reason=disabled）
    2. 资金安全致命事件  → **豁免持仓闸门与状态可读性**（reason=fatal_event）
    3. 每日结算报告      → 由 DAILY_REPORT_EMAIL_ENABLED 独立裁决（reason=daily_report）
    4. 人工通道自检      → 显式 selftest 事件（不属于自动告警）
    5. 普通/未分类事件   → 拒绝邮件；保留调用方的 TG 与日志
  修复前的致命缺陷：唯一开关同时管辖日报/崩溃/鉴权封锁/健康巡检，
  空仓（或 trade_state.json 不可读）时会把资金安全告警一并吞掉。

 本模块不触碰 Telegram、交易状态或交易所 API。普通事件即使有活跃批次也不发邮件。
"""
import json
import os
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

# 已知事件全集（含明确拒绝的普通事件）；未知/拼错事件拒绝邮件并输出可定位日志。
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
      event == "selftest"    → 人工明确触发的通道自检
      其余事件              → 拒绝邮件，不以持仓或邮件标题猜测严重性

    原因码：
      disabled / daily_report_disabled / fatal_event / daily_report /
      selftest / non_actionable_event / unknown_event
    """
    if not email_enabled(env):
        return False, "disabled"

    ev = str(event or "generic").strip().lower()
    if ev not in KNOWN_EVENTS:
        # 不猜未知事件的严重性；可定位日志避免拼错的严重事件静默丢失。
        print(f"⚠️ [邮件闸门] 未知 event={ev!r}，拒绝邮件；"
              f"若属资金安全事件请补入 email_gate.FATAL_EVENTS")
        return False, "unknown_event"
    if ev in FATAL_EVENTS:
        # 致命事件：持仓与状态可读性都不参与裁决（无持仓 ≠ 无风险；状态文件坏 ≠ 无风险）
        return True, "fatal_event"
    if ev == DAILY_REPORT_EVENT:
        if not daily_report_email_enabled(env):
            return False, "daily_report_disabled"
        return True, "daily_report"

    if ev == "selftest":
        return True, "selftest"
    return False, "non_actionable_event"
