# -*- coding: utf-8 -*-
"""外发绊线（事故修复 4）：测试期任何真实外发 → **抛异常 + 计数**（双轨）。

背景（2026-10-06 IP 邮件风暴）
================================================================
事故里告警通道把邮箱打爆，事后复盘却发现「测试从来没有证明过零外发」——
测试用例只是**碰巧**没走到外发，或者外发被被测代码的 `except Exception`
吞掉后无人知晓。本模块把「零外发」从**碰巧**变成**可断言的事实**。

双轨设计（缺一不可）
================================================================
  A 轨 · 抛异常（raise=True，默认）
      在**发生点**立刻抛 `EgressBlocked`（RuntimeError 子类）——
      普通网络错误处理（`except OSError` / ccxt ExchangeError）接不住它，
      异常栈直接指向真正发起外发的调用点。
  B 轨 · 计数（始终开启）
      即使被测代码把异常吞掉（B 轨模式下抛的是普通 `OSError`，最像真实网络
      失败，必然会被吞），断言阶段仍能读到 `_state['count'] > 0` ——
      「吞掉的外发」照样跑不掉。
  两轨互为保险：A 轨负责**炸得响**，B 轨负责**赖不掉**。

覆盖范围
================================================================
  * 构造阶段：模块一装上就生效，`CryptoTrader(...)` 构造过程里的任何外发
    同样被拦（不是等到用例主体才装）；
  * 后台线程：patch 的是进程内 `socket` / `smtplib` 的**类与模块级函数**，
    对所有线程一律生效（监视线程、通知线程都跑不掉）；
  * 隐蔽通道：`socket.getaddrinfo`（DNS 查询本身就是外发）一并拦。

边界
================================================================
  * **只装在测试进程里**（monkeypatch），不改 `trader_260725.py` 一行；
  * 纯标准库、零网络（被拦的调用在真正发包之前就抛了）；
  * `uninstall()` 精确还原被替换的原对象（可用 F7 校准）。

典型用法
================================================================
    import egress_tripwire as tw
    tw.install()                      # 构造被测对象**之前**
    ...                               # 构造 + 用例主体（含后台线程）
    tw.assert_zero('用例名')           # 断言阶段：计数必须为 0（B 轨）
"""
import smtplib
import socket
import threading

__all__ = ['EgressBlocked', 'install', 'uninstall', 'reset', 'count',
           'events', 'assert_zero', 'snapshot']


class EgressBlocked(RuntimeError):
    """绊线触发：检测到真实外发（DNS / TCP 连接 / SMTP）。"""


_lock = threading.Lock()
_state = {'on': False, 'count': 0, 'events': [], 'raise': True}
_orig = {}


# ───────────────────── 拦截点 ─────────────────────

def _record(kind, detail):
    """B 轨：先计数（再抛），保证「已发生」永远留痕。"""
    with _lock:
        _state['count'] += 1
        if len(_state['events']) < 64:
            _state['events'].append('%s | %s' % (kind, detail))
    if _state['raise']:
        # A 轨：RuntimeError —— 网络错误处理接不住，栈直接指向调用点
        raise EgressBlocked('[外发绊线] %s 被拦截: %s' % (kind, detail))
    # B 轨模式：抛最像真实网络失败的 OSError，让被测代码的 except 照常吞掉，
    # 以便验证「吞掉之后计数还在」。
    raise OSError('[外发绊线·计数模式] %s 已被拦截: %s' % (kind, detail))


def _w_connect(self, address, *a, **k):
    _record('socket.socket.connect', repr(address))


def _w_connect_ex(self, address, *a, **k):
    _record('socket.socket.connect_ex', repr(address))
    return 1                      # EINPROGRESS 语义，永不真正发包


def _w_create_connection(address, *a, **k):
    _record('socket.create_connection', repr(address))


def _w_getaddrinfo(host, port, *a, **k):
    _record('socket.getaddrinfo(DNS)', repr(host))


def _w_gethostbyname(host):
    _record('socket.gethostbyname(DNS)', repr(host))


def _w_smtp_connect(self, host='', port=0, *a, **k):
    _record('smtplib.SMTP.connect', '%s:%s' % (host, port))


def _w_smtp_sendmail(self, fromaddr, toaddrs, msg, *a, **k):
    _record('smtplib.SMTP.sendmail', 'to=%r len=%d' % (toaddrs, len(msg)))


def _w_smtp_login(self, user, password, *a, **k):
    _record('smtplib.SMTP.login', repr(user))


_PATCHES = (
    ('socket.socket.connect', socket.socket, 'connect', _w_connect),
    ('socket.socket.connect_ex', socket.socket, 'connect_ex', _w_connect_ex),
    ('socket.create_connection', socket, 'create_connection',
     _w_create_connection),
    ('socket.getaddrinfo', socket, 'getaddrinfo', _w_getaddrinfo),
    ('socket.gethostbyname', socket, 'gethostbyname', _w_gethostbyname),
    ('smtplib.SMTP.connect', smtplib.SMTP, 'connect', _w_smtp_connect),
    ('smtplib.SMTP.sendmail', smtplib.SMTP, 'sendmail', _w_smtp_sendmail),
    ('smtplib.SMTP.login', smtplib.SMTP, 'login', _w_smtp_login),
)


# ───────────────────── 安装 / 卸载 ─────────────────────

def install(raise_on_hit=True):
    """装上绊线（幂等）。必须在**构造被测对象之前**调用。"""
    with _lock:
        _state['raise'] = bool(raise_on_hit)
        if _state['on']:
            return _state
        for name, obj, attr, fn in _PATCHES:
            key = name
            if key not in _orig:
                _orig[key] = getattr(obj, attr)
            try:
                setattr(obj, attr, fn)
            except (AttributeError, TypeError):      # 某些平台是只读描述符
                _state['events'].append('patch_failed | %s' % name)
        _state['on'] = True
        return _state


def uninstall():
    """精确还原（幂等）。"""
    with _lock:
        if not _state['on']:
            return
        for name, obj, attr, _fn in _PATCHES:
            if name in _orig:
                try:
                    setattr(obj, attr, _orig[name])
                except (AttributeError, TypeError):
                    pass
        _state['on'] = False


def reset():
    """清零计数与事件（不改变安装状态/模式）。"""
    with _lock:
        _state['count'] = 0
        _state['events'] = []


def count():
    with _lock:
        return _state['count']


def events():
    with _lock:
        return list(_state['events'])


def snapshot():
    with _lock:
        return {'on': _state['on'], 'count': _state['count'],
                'raise': _state['raise'], 'events': list(_state['events'])}


def assert_zero(label=''):
    """B 轨断言：本阶段必须零外发。"""
    snap = snapshot()
    if snap['count']:
        raise AssertionError(
            '%s 外发绊线命中 %d 次（零外发被破坏）：%s'
            % (label, snap['count'], snap['events'][:8]))
