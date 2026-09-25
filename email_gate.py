#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""邮件告警发送闸门（纯标准库）。

两条 .env 开关：
  EMAIL_ALERT_ENABLED=true/false              邮件总手动开关（缺省 true，兼容旧行为）
  EMAIL_ALERT_ONLY_WITH_POSITION=true/false   仅在存在活跃批次时发送（缺省 false）

持仓判定刻意只读本地 ``trade_state.json``，不调用交易所 API：
  任一 symbol -> batch 的 ``is_active`` 为真即视为“有持仓/有活跃批次”。
  文件缺失、不可解析或结构不符合预期时返回不允许发送（Fail-Silent for email），
  但本模块不触碰 Telegram，也不触碰交易状态。
"""
import json
import os
from collections.abc import Mapping

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


def should_send_email(env=None, state_path: str | None = None) -> tuple[bool, str]:
    """返回 ``(是否允许发送, 原因码)``。

    原因码：
      disabled / position_gate_disabled / state_unreadable /
      no_active_positions / active_positions
    """
    if not email_enabled(env):
        return False, "disabled"
    if not email_only_with_position(env):
        return True, "position_gate_disabled"

    count = count_active_positions(state_path, env)
    if count is None:
        return False, "state_unreadable"
    if count <= 0:
        return False, "no_active_positions"
    return True, "active_positions"