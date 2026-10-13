"""Isolated tests for watchdog's log-confirmed routine console suppression."""

import builtins
import ast
from pathlib import Path
import queue

import watchdog


class _Process:
    def __init__(self, lines):
        self.stdout = iter(lines)


def _route(monkeypatch, lines, write_ok=True):
    out = queue.Queue()
    monkeypatch.setattr(watchdog, "_CONSOLE_QUEUE", out)
    monkeypatch.setattr(watchdog, "write_bot_log", lambda _line: write_ok)
    assert watchdog.monitor_process(_Process(lines)) is True
    return list(out.queue)


def test_known_routine_lines_hidden_only_after_confirmed_log(monkeypatch):
    routine = [
        "[TGDIAG] [REQ] getUpdates 进入 do_request（尚未知是否发出）\n",
        "[TGDIAG] [REQ] getUpdates 轮询成功 状态=200 耗时=10.23s 更新数=0\n",
        "📊 [限流观测] 近60s 调用: fetch_order×2 | 估算weight≈2"
        " USED-WEIGHT 最新=10 峰值60s=20\n",
        "[TGDIAG] [TASK] updater.running=True app.running=True task.exists=True "
        "done=False cancelled=False exc=None "
        "wait_at=['networkloop.py:161:network_retry_loop']\n",
    ]
    assert _route(monkeypatch, routine) == []
    assert _route(monkeypatch, routine, write_ok=False) == routine


def test_incidents_business_multiline_and_unknown_remain_visible(monkeypatch):
    visible = [
        "🔬 [限流观测·事发快照] fetch_order 触发限流（429）\n",
        "  └─ 前60s 本程序调用面: fetch_order×2\n",
        "✅ 订单成交：BTCUSDT\n",
        "Traceback (most recent call last):\n  File \"bot.py\", line 1\n",
        "📊 [限流观测] this format is not recognized\n",
        "[TGDIAG] [REQ] getUpdates 轮询成功 状态=429 耗时=1.1s 更新数=0\n",
        "[TGDIAG] [REQ] getUpdates 异常【已尝试往返】类型链=ReadTimeout 耗时=30.00s\n",
        "[TGDIAG] [REQ] getUpdates 进入 do_request（尚未知是否发出） extra\n",
        "[TGDIAG] [TASK] updater.running=True app.running=True task.exists=True "
        "done=False cancelled=True exc=None wait_at=[]\n",
    ]
    assert _route(monkeypatch, visible) == visible


def test_write_bot_log_reports_success_and_failure_without_recursive_disk_write(
        monkeypatch, tmp_path):
    monkeypatch.setattr(watchdog, "BOT_LOG_DIR", str(tmp_path))
    monkeypatch.setattr(watchdog, "_bot_log_fh", None)
    monkeypatch.setattr(watchdog, "_bot_log_day", None)
    monkeypatch.setattr(watchdog, "_CONSOLE_QUEUE", queue.Queue())
    assert watchdog.write_bot_log("routine line") is True
    watchdog.close_bot_log()
    logs = list(tmp_path.glob("bot_*.log"))
    assert len(logs) == 1 and "routine line" in logs[0].read_text(encoding="utf-8")

    real_open = builtins.open

    def fail_log_open(path, *args, **kwargs):
        if str(path).startswith(str(tmp_path)):
            raise OSError("isolated injected write failure")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", fail_log_open)
    assert watchdog.write_bot_log("must be visible") is False
    queued = list(watchdog._CONSOLE_QUEUE.queue)
    assert any("落盘失败" in line for line in queued)

    while not watchdog._CONSOLE_QUEUE.empty():
        watchdog._CONSOLE_QUEUE.get_nowait()
    visible = "📊 [限流观测] 近60s 调用: fetch_order×1 | 估算weight≈1\n"
    assert watchdog.monitor_process(_Process([visible])) is True
    queued = list(watchdog._CONSOLE_QUEUE.queue)
    assert visible in queued
    assert not any("落盘失败" in line for line in queued)  # rate-limited


def test_protection_status_text_has_no_fixed_sla_and_keeps_existing_call_site():
    """Guard the factuality fix without asserting implementation timing."""
    source_path = Path(__file__).with_name("trader_260725.py")
    source = source_path.read_text(encoding="utf-8-sig")
    tree = ast.parse(source)
    trader_class = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "CryptoTrader"
    )
    operation = next(
        node for node in trader_class.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "execute_signal"
    )
    monitor = next(
        node for node in trader_class.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "_start_monitoring"
    )

    statement = next(
        node for node in ast.walk(operation)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "print"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
        and "止盈与止损挂单参数已预生成" in node.args[0].value
    )
    text = statement.args[0].value
    assert "识别成交后按现有流程创建并核验保护单" in text
    assert not any(sla in text for sla in ("1秒内", "1 秒内", "一秒内", "秒内"))

    # Keep the informational print at the existing execute_signal call site,
    # and verify the established monitor still owns protection creation.
    protection_calls = [
        node for node in ast.walk(monitor)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_place_prepared_orders_immediately"
    ]
    assert protection_calls
    assert statement.lineno < min(node.lineno for node in protection_calls)
