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
