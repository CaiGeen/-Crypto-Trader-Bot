# -*- coding: utf-8 -*-
"""验收 F（事故修复 4）：外发绊线 + 测试独立临时状态目录。

修复 4 原文（报告 §8.3）：
  「外发绊线（**抛异常 + 计数断言**双轨、覆盖构造阶段与后台线程、校准用例）
    + 测试**独立临时状态目录**（构造前重定向，不删残留状态）」→ 验收 F。

八条用例的分工：
  F1  A 轨校准（DNS）：绊线必须**炸得响** —— 直接抛 EgressBlocked
  F2  A 轨校准（TCP）：socket.connect 同样被拦
  F3  B 轨校准：把异常**吞掉**之后计数仍然在（赖不掉）
  F4  后台线程覆盖：监视线程形态的外发照样被拦 + 计数
  F5  SMTP 覆盖：事故元凶通道（smtplib）被拦——含生产实际使用的 SMTP_SSL
  F10 生产级 B 轨：`_get_public_ip()` 真实 urlopen→DNS 被拦，且异常被生产代码
      的 `except Exception` 吞掉后**计数仍在**（证明"只靠不抛异常"的验收会假绿）
  F6  构造阶段：绊线在 `CryptoTrader(...)` 之前就生效 → 构造期零外发
  F7  卸载还原校准：uninstall 后 socket 方法回到原对象（否则以后装了也白装）
  F8  集成零外发：**真实监视线程 + 续跑 + 收尾**整条链路跑完，计数必须为 0
  F9  独立临时状态目录：构造前重定向、用例间互不污染、仓库账本零改动、
      残留状态目录**不删除**（转审硬约束）

生产代码零改动：绊线只 monkeypatch 测试进程内的 socket/smtplib。

运行：G:\\my-crypto-bot\\.venv\\Scripts\\python.exe test_egress_tripwire.py
"""
import hashlib
import os
import smtplib
import socket
import sys
import tempfile
import threading
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import egress_tripwire as tw

_REPO = os.path.dirname(os.path.abspath(__file__))
_PROD_FILES = ['trade_state.json', 'trade_tombstones.json', 'trade_stats.json',
               'auth_blocked.json', 'signal.json', 'signal_dedup.json']


def _prod_snapshot():
    snap = {}
    for name in _PROD_FILES:
        p = os.path.join(_REPO, name)
        try:
            with open(p, 'rb') as f:
                data = f.read()
            snap[name] = (hashlib.sha256(data).hexdigest(), len(data),
                          os.stat(p).st_mtime_ns)
        except FileNotFoundError:
            snap[name] = ('<missing>', 0, 0)
    return snap


_PROD_AT_START = _prod_snapshot()


# ───────────────────── A 轨（抛异常） ─────────────────────

def f1_tripwire_raises_on_dns():
    tw.install(raise_on_hit=True)
    tw.reset()
    try:
        socket.getaddrinfo('smtp.example.com', 465)
    except tw.EgressBlocked as e:
        assert 'getaddrinfo' in str(e), e
    else:
        raise AssertionError('A 轨失灵：DNS 外发没有被拦（绊线等于没有）')
    assert tw.count() == 1, tw.snapshot()
    assert any('DNS' in x for x in tw.events()), tw.events()


def f2_tripwire_raises_on_tcp_connect():
    tw.install(raise_on_hit=True)
    tw.reset()
    s = socket.socket()
    try:
        s.connect(('127.0.0.1', 9))
    except tw.EgressBlocked as e:
        assert 'connect' in str(e), e
    else:
        raise AssertionError('A 轨失灵：TCP 连接没有被拦')
    finally:
        s.close()
    assert tw.count() == 1, tw.snapshot()


# ───────────────────── B 轨（计数断言） ─────────────────────

def f3_count_survives_swallowed_exception():
    """双轨的意义：被测代码 `except Exception` 把异常吞掉时，B 轨仍在计数。"""
    tw.install(raise_on_hit=False)      # B 轨模式：抛最像真实网络失败的 OSError
    tw.reset()
    swallowed = []
    try:
        try:
            socket.create_connection(('127.0.0.1', 9), timeout=1)
        except OSError as e:            # 模拟被测代码的常规网络错误处理
            swallowed.append(str(e))
    finally:
        tw.install(raise_on_hit=True)
    assert swallowed, 'B 轨模式必须抛 OSError，否则无法模拟"被吞掉的外发"'
    assert not isinstance(swallowed[0], tw.EgressBlocked), \
        'B 轨模式不该抛 EgressBlocked（那样就测不到"吞掉"了）'
    assert tw.count() == 1, \
        '异常已被吞掉，但 B 轨计数必须留下痕迹: %s' % tw.snapshot()


def f4_background_thread_covered():
    """后台线程（监视线程形态）同样被拦——patch 是进程级的，不看线程。"""
    tw.install(raise_on_hit=True)
    tw.reset()
    got = {}

    def _worker():
        try:
            socket.getaddrinfo('futures.binance.com', 443)
        except BaseException as e:      # noqa: BLE001 —— 记录后在主线程断言
            got['exc'] = e

    th = threading.Thread(target=_worker, daemon=True)
    th.start()
    th.join(timeout=5)
    assert isinstance(got.get('exc'), tw.EgressBlocked), \
        '后台线程外发没被拦: %r' % (got,)
    assert tw.count() == 1, tw.snapshot()


def f5_smtp_channel_covered():
    """事故元凶通道：smtplib 构造即连（SMTP(host, port) → connect）。

    生产走的是 `smtplib.SMTP_SSL("smtp.qq.com", 465)`（trader_260725.py:1194）；
    SMTP_SSL **不覆盖** connect（继承 SMTP.connect），所以类级 patch 同样拦得住——
    这里两形态各验一次，避免"只拦了没在用的那个类"。
    """
    tw.install(raise_on_hit=True)
    tw.reset()
    try:
        smtplib.SMTP('smtp.example.com', 587, timeout=1)
    except tw.EgressBlocked as e:
        assert 'smtp' in str(e).lower(), e
    else:
        raise AssertionError('SMTP 通道没被拦（邮件风暴还能复现）')
    assert tw.count() == 1, tw.snapshot()
    # 生产实际使用的 SMTP_SSL 必须继承到同一个被 patch 的 connect
    assert smtplib.SMTP_SSL.connect is smtplib.SMTP.connect, \
        'SMTP_SSL 没有继承被 patch 的 connect（生产通道漏拦）'
    tw.reset()
    try:
        smtplib.SMTP_SSL('smtp.qq.com', 465, timeout=1)
    except tw.EgressBlocked as e:
        assert 'smtp' in str(e).lower(), e
    else:
        raise AssertionError('SMTP_SSL（生产真实通道）没被拦')
    assert tw.count() == 1, tw.snapshot()


def f10_swallowed_egress_still_counted_in_production_path():
    """生产级 B 轨校准：`_get_public_ip()` 的真实 urlopen → DNS 外发。

    生产实现里 `except Exception: pass`（trader_260725.py:771）会把绊线异常
    **吞掉**并静默返回 None —— 这正是"只靠不抛异常"的验收会假绿的原因。
    本用例断言：① 调用**没有**外抛（与生产行为一致）② 计数仍然为 1（B 轨）。
    """
    import test_monitor_resume as mres
    tw.install(raise_on_hit=True)
    tw.reset()
    tmp = tempfile.mkdtemp(prefix='f10_state_')
    t, ex = mres.make_trader(tmp)
    t.IP_CHECK_ENABLED = True                     # 打开真实外发路径
    t._get_public_ip = lambda: mres.CryptoTrader._get_public_ip(t)
    got = t._get_public_ip()                      # 真 urllib → DNS → 被绊线拦
    assert got is None, '生产路径把外发异常吞掉后应静默返回 None: %r' % (got,)
    assert tw.count() >= 1, \
        '外发已被生产代码吞掉，但 B 轨计数必须留下痕迹: %s' % tw.snapshot()
    # 命中的是**真实出网连接**（本机走代理 → ('127.0.0.1', 7890)）或 DNS 解析，
    # 二者都属于外发；只要确实记录到 socket/smtplib 层的拦截即算命中。
    assert any(x.startswith(('socket.', 'smtplib.')) for x in tw.events()), tw.events()


# ───────────────────── 构造阶段 / 卸载校准 ─────────────────────

def f6_construction_phase_covered():
    """绊线必须在**构造之前**装上，且构造期确实零外发。"""
    import test_monitor_resume as mres
    tw.install(raise_on_hit=True)
    tw.reset()
    tmp = tempfile.mkdtemp(prefix='f6_state_')
    t, ex = mres.make_trader(tmp)       # 构造阶段（CryptoTrader(...) 在这里）
    assert isinstance(t, mres.CryptoTrader)
    tw.assert_zero('构造阶段')
    assert tw.snapshot()['on'] is True, '绊线在构造期间被关掉了'


def f7_uninstall_restores_originals():
    """校准：卸载必须把原对象还回去，否则"装过"是自欺欺人。"""
    tw.uninstall()                       # 先复位到解释器原状再取基准
    orig_connect = socket.socket.connect
    orig_gai = socket.getaddrinfo
    tw.install(raise_on_hit=True)
    assert socket.socket.connect is not orig_connect, '安装未生效'
    assert socket.getaddrinfo is not orig_gai, '安装未生效'
    tw.uninstall()
    assert socket.socket.connect is orig_connect, 'socket.connect 未还原'
    assert socket.getaddrinfo is orig_gai, 'getaddrinfo 未还原'
    # 卸载后不再拦（只做身份断言，绝不真发包）
    tw.reset()
    assert tw.count() == 0


# ───────────────────── 集成：整条监控链路零外发 ─────────────────────

def f8_full_monitor_chain_zero_egress():
    """真实监视线程 + 续跑 + 收尾 全链路跑完 → 计数必须为 0。

    这是修复 4 的核心主张：前 7 条只证明"绊线能用"，这一条证明
    "被验收的那条链路确实没有外发"（而不是碰巧没被测到）。
    """
    import test_monitor_resume as mres
    tw.install(raise_on_hit=True)
    tw.reset()
    mres.b3_stale_pending_close_fill_lagging()   # 三态之一 + 成交单调 + SL 保留
    tw.assert_zero('b3 全链路')
    snap = tw.snapshot()
    assert snap['on'] is True, '跑测期间绊线被关掉了'


# ───────────────────── 独立临时状态目录 ─────────────────────

def f9_independent_tmp_state_dir():
    """转审硬约束：构造**前**重定向、用例间互不污染、仓库账本零改动、
    残留状态目录**不删除**（不清理 7700+ 个 p5_* 残留的同一条纪律）。"""
    import test_monitor_resume as mres
    import trader_260725
    import health_progress

    d1 = tempfile.mkdtemp(prefix='f9_state_a_')
    d2 = tempfile.mkdtemp(prefix='f9_state_b_')
    f1, _ = mres.make_trader(d1)
    state1 = trader_260725.STATE_FILE
    f2, _ = mres.make_trader(d2)
    state2 = trader_260725.STATE_FILE

    assert state1.startswith(d1), state1
    assert state2.startswith(d2), state2
    assert state1 != state2, '两次构造共用同一个状态文件（用例会互相污染）'
    # 构造前重定向：仓库账本在整个过程中一个字节都不能变
    assert _prod_snapshot() == _PROD_AT_START, '仓库账本被改动'
    # 残留状态目录必须还在（禁止删除）
    assert os.path.isdir(d1) and os.path.isdir(d2), '临时状态目录被删了'
    # HEALTH_DIR 也必须指向临时目录（不在候选仓库留 .bot_health 文件）
    assert health_progress.HEALTH_DIR.startswith(tempfile.gettempdir()) \
        or 'mres_health_' in health_progress.HEALTH_DIR, \
        health_progress.HEALTH_DIR
    assert not os.path.exists(os.path.join(_REPO, '.bot_health',
                                           'batch_batch_mres.json')), \
        '候选仓库里落下了 .bot_health 文件'


CASES = [
    f1_tripwire_raises_on_dns,
    f2_tripwire_raises_on_tcp_connect,
    f3_count_survives_swallowed_exception,
    f4_background_thread_covered,
    f5_smtp_channel_covered,
    f10_swallowed_egress_still_counted_in_production_path,
    f6_construction_phase_covered,
    f7_uninstall_restores_originals,
    f8_full_monitor_chain_zero_egress,
    f9_independent_tmp_state_dir,
]


def main():
    ok, failed = 0, []
    try:
        for fn in CASES:
            try:
                fn()
                ok += 1
                print('✅ %s' % fn.__name__)
            except Exception as e:
                failed.append(fn.__name__)
                print('❌ %s: %s' % (fn.__name__, e))
                traceback.print_exc()
    finally:
        tw.uninstall()                  # 还原解释器状态，别污染后续用例
    print('=' * 74)
    if failed:
        print('RED: %d/%d  失败=%s' % (ok, len(CASES), failed))
        return 1
    print('GREEN: %d/%d' % (ok, len(CASES)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
