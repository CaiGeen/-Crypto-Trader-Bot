# -*- coding: utf-8 -*-
"""
C5/SG4+SG4-B Create→Verify→Commit 提交一致性 —— TDD 测试（红阶段）

规格（ChatGPT APPROVED，C5 v2）：
  P0-1  所有 create_order 一律禁止盲重 —— 14 处显式 retries=1
        （create_order API 本身非幂等，统一禁止盲重，不按级别记忆）
  P0-2  A 级保护单 Create → Verify(fetch_order) → Commit
        Verify 三态（禁止退化为 True/False）：
          SUCCESS  → Commit
          NOT_FOUND(OrderNotFound) → 不 Commit + 既有失败路径告警
          UNKNOWN(NetworkError等)  → 不 Commit + critical + 不自动补单（防双单复活）
  附加  G 组（孤儿保护单超限检测/告警）：ChatGPT 裁决【移出 C5】→ SG5/D-004 独立议题。
        本文件保留场景代码供 SG5 复用，主循环 SKIP 不执行（C5 不实现孤儿检测）。

TDD 阶段预期（实测红 → 实施后绿）：
  红阶段 11 FAIL + 15 PASS（G 组 2 项 SKIP 不计入；A2/B1-B4/C/D/D2/E/F 能力缺失）
  绿阶段 25/25 PASS（G 组 2 项 SKIP；A/B/C/D/E/F 全绿 + H 组 11 项回归基线保持全绿）
  实施后修复 3 个测试问题：① 行号锚点更新（B/C 级 retries Edit 增加行数）② B3 匹配器
  扩展为接受 _safe_api_call(fetch_order) 参数形态（项目统一 API 惯例）③ make_fake stub
  _verify_order_created（恢复链场景真实 verify，成功语义被测试覆盖）

⚠️ A 级数量修正：草案 v2 写"A 级 10 处"为笔误。按当前源码独立核实（Grep create_order 全量 + 逐点读上下文），
A 级实为 11 处：1071/1182/1356/2418/2456/2903/2966/3043/3229/3282/3327。B 级 1 处(1787)、C 级 2 处(3532/3713)。
总数 14 处不变。测试以源码为准锁定 11 处 A 级。

用法: .venv\Scripts\python.exe test_sg4.py
"""
import ast
import inspect
import sys
import time
from unittest import mock

import ccxt

import trader_260725
from trader_260725 import CryptoTrader

SYMBOL = "BTC/USDT:USDT"
BATCH = "batch_sg4_001"
RESULTS = []

# A/B/C 级调用点行号（B2-6 后最终实测，Grep create_order 全量核实）
# ⚠️ 实施后行号偏移说明：B2-3 六处接入点插入仲裁闸门（+50 行）→ 以最终 Grep 为准。
#    已累计：B2-0 helper(+27) + 补挂段 verify/classify(+39) + B2-2 补挂TP落盘(+55) + B2-3 闸门(+50)
#    + B2-4 硬锁分支(+~50) + B2-5 骨架/registry更新/恢复护栏(+98)
#    + B2-6 recover自愈分支(+24) + 骨架元数据(+6) + 新helper(_self_heal_no_id/_rebuild)(+185) = +215。
A_LINES = {1101, 1220, 1402, 3125, 3172, 3663, 3815, 3940, 4215, 4356, 4492}  # 11 处保护单
B_LINES = {1906}                                                              # 1 处开仓条件单
C_LINES = {4756, 4939}                                                        # 2 处平仓单
ALL_LINES = A_LINES | B_LINES | C_LINES                                       # 14 处


def report(name, passed, detail=""):
    RESULTS.append((name, passed))
    print(f"\n{'=' * 60}\n[{'PASS' if passed else 'FAIL'}] {name} {detail}\n{'=' * 60}")


class ProbeReached(Exception):
    """第二轮轮询探针（同 C4 测试）：触发监控循环异常捕获路径结束驱动"""


# =====================================================================
# AST 辅助
# =====================================================================

def _is_attr_chain(node, parts):
    """判断 node 是否为属性链，如 ('self','exchange','create_order')。

    parts[0] 是根 Name（如 'self'），parts[1:] 是逐级属性名。
    例: self.exchange.create_order → parts=('self','exchange','create_order')
        AST = Attribute(attr='create_order', value=Attribute(attr='exchange', value=Name('self')))
    """
    cur = node
    for p in reversed(parts[1:]):
        if not isinstance(cur, ast.Attribute):
            return False
        if cur.attr != p:
            return False
        cur = cur.value
    return isinstance(cur, ast.Name) and cur.id == parts[0]


def _is_safe_api_create(node):
    """判断 Call 是否为 _safe_api_call(self.exchange.create_order, ...)"""
    if not isinstance(node, ast.Call):
        return False
    if not (isinstance(node.func, ast.Attribute) and node.func.attr == '_safe_api_call'):
        return False
    if not node.args:
        return False
    return _is_attr_chain(node.args[0], ('self', 'exchange', 'create_order'))


def _create_line(node):
    """create_order 引用所在行 = node.args[0].lineno。

    AST Call 节点 lineno 是 `self._safe_api_call(` 起始行（比 create_order 引用行少 1），
    而 A/B/C_LINES 锚点与草案/Grep 均以 create_order 引用行为准。"""
    return node.args[0].lineno


def _find_function(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def _enclosing_function(tree, node):
    """返回包含 node 行号的最内层函数节点"""
    best, best_span = None, None
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        end = getattr(fn, 'end_lineno', None) or node.lineno
        if fn.lineno <= node.lineno <= end:
            if best_span is None or end < best_span:
                best, best_span = fn, end
    return best


def _is_verify_call(node):
    """B2-0：verify 接入同时认直调 _verify_order_created 与统一入口 _verify_and_update_registry"""
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
        return False
    return node.func.attr in ('_verify_order_created', '_verify_and_update_registry')


def _count_a_verify_coverage(tree):
    """A 级调用点中，所在函数体内已接入 _verify_order_created 的数量"""
    covered = []
    for node in ast.walk(tree):
        if not _is_safe_api_create(node):
            continue
        if _create_line(node) not in A_LINES:
            continue
        fn = _enclosing_function(tree, node)
        has_verify = bool(fn) and any(_is_verify_call(c) for c in ast.walk(fn))
        if has_verify:
            covered.append(_create_line(node))
    return covered


def _src_tree():
    """解析整个文件源码 → 节点 lineno 为文件绝对行号（与 A/B/C_LINES 锚点一致）。

    不能用 inspect.getsource(CryptoTrader)：其行号是类内相对行号，与锚点集不匹配。"""
    with open(inspect.getsourcefile(CryptoTrader), encoding='utf-8') as f:
        return ast.parse(f.read())


# =====================================================================
# A 组：AST —— 14 处 create_order 全部显式 retries=1
# =====================================================================

def scenario_ast_retries():
    tree = _src_tree()
    create_calls = [n for n in ast.walk(tree) if _is_safe_api_create(n)]

    # A1: 调用点总数 = 14（防漏改/多改；PASS 基线）
    report("A/调用点总数=14", len(create_calls) == 14,
           f"(实际: {len(create_calls)}, 行号: {sorted(_create_line(n) for n in create_calls)})")

    # 行号集一致性校验（防行号漂移导致误判）
    actual_lines = {_create_line(n) for n in create_calls}
    report("A/行号锚点齐全", ALL_LINES == actual_lines,
           f"(缺失: {sorted(ALL_LINES - actual_lines)}, 多余: {sorted(actual_lines - ALL_LINES)})")

    # A2: 14 处全部显式 retries=1（红阶段：0/14）
    ok_lines, missing = [], []
    for n in create_calls:
        ret = [k for k in n.keywords if k.arg == 'retries']
        if ret and isinstance(ret[0].value, ast.Constant) and ret[0].value.value == 1:
            ok_lines.append(_create_line(n))
        else:
            missing.append(_create_line(n))
    report("A/14处显式retries=1", len(ok_lines) == len(create_calls),
           f"(满足: {len(ok_lines)}/{len(create_calls)} → [TDD红] 缺失: {sorted(missing)})")

    # A3: 无任何 create 调用显式 retries≠1（PASS 基线：当前均未显式）
    bad = []
    for n in create_calls:
        ret = [k for k in n.keywords if k.arg == 'retries']
        if ret and not (isinstance(ret[0].value, ast.Constant) and ret[0].value.value == 1):
            bad.append(_create_line(n))
    report("A/无retries≠1", not bad, f"(违规行: {bad})")


# =====================================================================
# B 组：AST —— A 级 Verify 接入完整性
# =====================================================================

def scenario_ast_verify_integration():
    tree = _src_tree()

    # B1: helper 定义存在（红阶段：不存在）
    helper = _find_function(tree, '_verify_order_created')
    report("B/helper定义存在", helper is not None,
           "(未找到 → [TDD红] _verify_order_created 未实现)")

    if helper is not None:
        # B2: helper 零写 API（只读：不得 create_order/cancel_order）
        bad_calls = []
        for n in ast.walk(helper):
            if isinstance(n, ast.Call):
                name = None
                if isinstance(n.func, ast.Attribute):
                    name = n.func.attr
                elif isinstance(n.func, ast.Name):
                    name = n.func.id
                if name in ('create_order', 'cancel_order'):
                    bad_calls.append(name)
        report("B/helper零写API", not bad_calls, f"(违规写调用: {bad_calls})")

        # B3: helper 使用 fetch_order（Verify 必须 fetch_order，不用 open_orders 快照）
        # 两种形态均接受：直接 self.exchange.fetch_order(...) 调用，或作为 _safe_api_call 首参
        # （项目统一 API 惯例：所有交易所调用经 _safe_api_call 包裹）
        uses_fetch = any(
            (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == 'fetch_order')
            or (isinstance(n, ast.Attribute) and n.attr == 'fetch_order')
            for n in ast.walk(helper))
        report("B/helper用fetch_order", uses_fetch, "(未找到 → [TDD红] Verify 未接入 fetch_order)")
    else:
        report("B/helper零写API", False, "[TDD红] helper 未实现，跳过")
        report("B/helper用fetch_order", False, "[TDD红] helper 未实现，跳过")

    # B4: A 级 11 处所在函数内均已接入 verify 调用（红阶段：0/11）
    covered = _count_a_verify_coverage(tree)
    report("B/A级11处verify接入", len(covered) >= len(A_LINES),
           f"(覆盖: {sorted(covered)}/{len(A_LINES)} → [TDD红] 未接入: {sorted(A_LINES - set(covered))})")


# =====================================================================
# C/D/E/F 组：helper 三态语义（未绑定调用，红阶段 AttributeError=预期失败）
# =====================================================================

def _make_verify_fake(fetch_behavior):
    """构造 verify helper 的 self 基座：_safe_api_call 直调 fn，exchange.fetch_order 按场景配置"""
    fake = mock.MagicMock()
    ex = mock.MagicMock()
    if isinstance(fetch_behavior, Exception):
        ex.fetch_order.side_effect = fetch_behavior
    else:
        ex.fetch_order.return_value = fetch_behavior
    fake.exchange = ex
    fake._safe_api_call = lambda fn, *a, **k: fn(*a, **k)
    return fake


def _call_verify(fake, order_id='o1'):
    """未绑定调用 _verify_order_created；红阶段 helper 不存在 → 抛 AttributeError"""
    fn = getattr(CryptoTrader, '_verify_order_created')
    return fn(fake, order_id, SYMBOL)


def _verify_expect(name, fetch_behavior, expected, extra_desc=""):
    try:
        got = _call_verify(_make_verify_fake(fetch_behavior))
    except AttributeError as e:
        report(name, False, f"[TDD红] _verify_order_created 未实现: {e}")
        return
    except Exception as e:
        report(name, False, f"verify 抛出异常（非三态返回）: {type(e).__name__}: {e}")
        return
    report(name, got == expected, f"(期望={expected!r}, 实际={got!r} {extra_desc})")


def scenario_verify_three_state():
    """C/D/E/F 组：SUCCESS / UNKNOWN / NOT_FOUND 三态 + 语义互斥"""
    # C: SUCCESS → 'success'（fetch_order 正常返回订单）
    _verify_expect("C/SUCCESS→success", {'id': 'o1', 'status': 'NEW'}, 'success',
                   "→ 应 Commit")
    # D: NetworkError → 'unknown'（不 Commit + critical + 不自动补单）
    _verify_expect("D/NetworkError→unknown", ccxt.NetworkError("net down"), 'unknown',
                   "→ 应 不Commit + critical + 不自动补单")
    # D2: 具体子类 RequestTimeout 同样 → 'unknown'
    _verify_expect("D2/RequestTimeout→unknown", ccxt.RequestTimeout("timeout"), 'unknown')
    # E: OrderNotFound → 'not_found'（不 Commit + 既有失败路径）
    _verify_expect("E/OrderNotFound→not_found", ccxt.OrderNotFound("order gone"), 'not_found',
                   "→ 应 不Commit + 既有失败路径告警")

    # F: 三态语义互斥（UNKNOWN ≠ NOT_FOUND ≠ SUCCESS，禁混淆）
    try:
        s = _call_verify(_make_verify_fake({'id': 'o1'}))
        u = _call_verify(_make_verify_fake(ccxt.NetworkError("x")))
        nf = _call_verify(_make_verify_fake(ccxt.OrderNotFound("x")))
    except AttributeError as e:
        report("F/三态互斥", False, f"[TDD红] _verify_order_created 未实现: {e}")
        return
    distinct = len({s, u, nf}) == 3
    report("F/三态互斥", distinct,
           f"(SUCCESS={s!r}, UNKNOWN={u!r}, NOT_FOUND={nf!r} → 必须两两不同)")
    report("F/UNKNOWN≠NOT_FOUND", u != nf and u != 'not_found' and nf != 'unknown',
           f"(UNKNOWN={u!r} 不得被当作 NOT_FOUND={nf!r} —— UNKNOWN ≠ EMPTY)")


# =====================================================================
# G 组：孤儿保护单超限 → critical 告警 + 零仲裁（集成，驱动监控第一轮）
# =====================================================================

def _make_sl_order(**kw):
    base = {'id': 'sl_1', 'side': 'SELL', 'amount': 0.01,
            'info': {'reduceOnly': 'true', 'closePosition': 'false', 'positionSide': 'BOTH'}}
    base.update(kw)
    for k in ('reduceOnly', 'closePosition', 'positionSide'):
        if k in kw:
            base['info'][k] = kw[k]
    return base


def _make_tp_order(**kw):
    base = {'id': 'tp_1', 'side': 'SELL', 'amount': 0.01,
            'info': {'reduceOnly': 'true', 'closePosition': 'false', 'positionSide': 'BOTH'}}
    base.update(kw)
    for k in ('reduceOnly', 'closePosition', 'positionSide'):
        if k in kw:
            base['info'][k] = kw[k]
    return base


def make_fake(states, open_orders):
    """MagicMock 基座 + 显式 stub 监控循环依赖（同 C4 测试）"""
    fake = mock.MagicMock()
    fake.load_all_states = lambda: states
    fake._safe_api_call = lambda fn, *a, **k: fn(*a, **k)

    ex = mock.MagicMock()
    ex.amount_to_precision.side_effect = lambda s, v: v
    ex.price_to_precision.side_effect = lambda s, v: v
    ex.fetch_open_orders.return_value = open_orders
    ex.fetch_positions.return_value = []
    ex.fetch_ticker.return_value = {'last': 100.0}
    ex.cancel_order.return_value = {}
    ex.create_order.return_value = {'id': 'new_order'}
    fake.exchange = ex

    fake._get_current_position_amt = lambda *a, **k: None
    fake._calculate_monitoring_interval = lambda: 60.0
    fake._sync_time_if_needed = lambda: None
    fake._check_ip_periodically = lambda: None
    fake.last_ip_check_time = time.time()
    fake.IP_CHECK_INTERVAL = 300.0
    fake._active_monitors_lock = mock.MagicMock()
    fake._active_monitors = set()
    fake._sg3_alerted = set()

    fake.sent = []
    fake.saved = []
    fake.send_tg_notification = lambda text, **kw: fake.sent.append((kw.get('level', 'info'), str(text)))
    fake.save_batch_state = lambda s, b, d: fake.saved.append(dict(d))
    # ⚠️ MagicMock 属性陷阱：getattr(fake, '_api_cooldown_until', 0) 在 MagicMock 上
    # 返回自动 mock（吞掉默认值）→ time.time() < MagicMock 抛 TypeError（B2-3 闸门会检查 cooldown）。
    # 必须显式置 0，否则真实 _assert_create_allowed 在 cooldown 比较处崩溃。
    fake._api_cooldown_until = 0
    fake._cancel_remaining_entries = lambda *a, **k: None
    fake._cancel_limit_close_order = lambda *a, **k: None
    fake.clear_batch_state = lambda *a, **k: None
    fake._record_realized_pnl = lambda *a, **k: None
    fake._notify_snapshot = lambda *a, **k: None
    fake._check_protection_order_validity = (
        lambda ord, expected_side, is_hedge_mode, position_side, required_amount:
        CryptoTrader._check_protection_order_validity(
            fake, ord, expected_side, is_hedge_mode, position_side, required_amount))
    # C5/SG4：恢复链场景需真实 verify（fake.exchange.fetch_order 为 MagicMock 自动成功 → 'success'）
    fake._verify_order_created = (
        lambda order_id, symbol: CryptoTrader._verify_order_created(fake, order_id, symbol))
    # B2-3：Create 仲裁闸门必须绑定真实实现，否则自动 mock 返回 0 个值 →
    # `allowed, reason = fake._assert_create_allowed(...)` 解包崩溃被 except 吞 → create 路径不执行。
    if hasattr(CryptoTrader, '_assert_create_allowed'):
        fake._assert_create_allowed = (
            lambda s, b, i, **k: CryptoTrader._assert_create_allowed(fake, s, b, i, **k))
    return fake


def make_states(user_modified=False):
    return {
        SYMBOL: {
            BATCH: {
                'is_active': True,
                'side': 'BUY',
                'current_sl_id': 'sl_1',
                'tp_order_id': 'tp_1',
                'user_modified': user_modified,
                'stop_steps': [55000.0],
                'take_profit_price': 60000.0,
                'pending_sl_orders': [],
            }
        }
    }


def run_monitor(fake):
    """驱动 _start_monitoring 第一轮（同 C4 测试：探针结束驱动，异常路径正常返回）"""
    with mock.patch.object(trader_260725.time, 'sleep', side_effect=[None, ProbeReached()]):
        CryptoTrader._start_monitoring(
            fake, SYMBOL, BATCH,
            entry_orders=['entry_1'],
            stop_steps=[55000.0], take_profit_price=60000.0,
            current_sl_id='sl_1', tp_order_id='tp_1',
            batch_total_amount=0.01, target_amounts=[0.01],
            params_base={}, is_hedge_mode=False, side='BUY',
            last_filled_count=1, filled_details=[100.0],
            total_entry_fee=0.0, pending_sl_orders=[],
            prepared_tp_params=None, layer_sl_params=None,
        )


def scenario_orphan_detect():
    """G 组：状态记 1 SL + 1 TP，open_orders 有 2 SL + 2 TP → 超限 → critical(SG5) 告警 + 零仲裁"""
    states = make_states(user_modified=False)
    orders = [
        _make_sl_order(),               # sl_1 有效（状态记录）
        _make_sl_order(id='sl_2'),      # 孤儿 SL
        _make_tp_order(),               # tp_1 有效（状态记录）
        _make_tp_order(id='tp_2'),      # 孤儿 TP
    ]
    fake = make_fake(states, orders)
    run_monitor(fake)

    orphan_alerts = [t for lv, t in fake.sent if lv == 'critical' and 'SG5' in t]
    cancel_n = fake.exchange.cancel_order.call_count
    create_n = fake.exchange.create_order.call_count
    report("G/孤儿超限critical告警", len(orphan_alerts) >= 1,
           f"(SG5告警: {len(orphan_alerts)} → [TDD红] 孤儿检测未实现)")
    report("G/只告警不仲裁", cancel_n == 0 and create_n == 0,
           f"(cancel: {cancel_n}, create: {create_n} → 检测≠自动删除，仲裁留 D-004)")


# =====================================================================
# H 组：C4/恢复链回归（PASS 基线，不强行制造红）
# =====================================================================

def _sg3_alerts(fake):
    return [t for lv, t in fake.sent if 'SG3' in t]


def scenario_sl_invalid_recover():
    """H1: SL invalid + user_modified=False → 恢复链触发（C4 既有能力 → 应 PASS）"""
    states = make_states(user_modified=False)
    orders = [
        _make_sl_order(reduceOnly='false'),   # SL 无效
        _make_tp_order(),                     # TP 有效
    ]
    fake = make_fake(states, orders)
    run_monitor(fake)

    cancel_n = fake.exchange.cancel_order.call_count
    create_n = fake.exchange.create_order.call_count
    types = [c.kwargs.get('type') for c in fake.exchange.create_order.call_args_list]
    sg3 = _sg3_alerts(fake)
    report("H1/SL无效恢复链触发", cancel_n >= 1 and create_n >= 1,
           f"(cancel: {cancel_n}, create: {create_n})")
    report("H1/恢复创建STOP_MARKET", 'STOP_MARKET' in types, f"(类型: {types})")
    report("H1/SG3告警已发", len(sg3) >= 1, f"(SG3: {len(sg3)})")


def scenario_all_valid_noop():
    """H2: SL/TP 均有效 → 零操作零告警（C4 既有能力 → 应 PASS）"""
    states = make_states(user_modified=False)
    orders = [_make_sl_order(), _make_tp_order()]
    fake = make_fake(states, orders)
    run_monitor(fake)

    report("H2/全有效零撤单", fake.exchange.cancel_order.call_count == 0,
           f"(cancel: {fake.exchange.cancel_order.call_count})")
    report("H2/全有效零下单", fake.exchange.create_order.call_count == 0,
           f"(create: {fake.exchange.create_order.call_count})")
    report("H2/全有效零告警", len(_sg3_alerts(fake)) == 0,
           f"(SG3: {len(_sg3_alerts(fake))})")


def scenario_user_modified():
    """H3: SL invalid + user_modified=True → 零操作仍告警（C4 既有能力 → 应 PASS）"""
    states = make_states(user_modified=True)
    orders = [
        _make_sl_order(reduceOnly='false'),
        _make_tp_order(),
    ]
    fake = make_fake(states, orders)
    run_monitor(fake)

    report("H3/user_modified零撤单", fake.exchange.cancel_order.call_count == 0,
           f"(cancel: {fake.exchange.cancel_order.call_count})")
    report("H3/user_modified零下单", fake.exchange.create_order.call_count == 0,
           f"(create: {fake.exchange.create_order.call_count})")
    report("H3/user_modified仍告警", len(_sg3_alerts(fake)) >= 1,
           f"(SG3: {len(_sg3_alerts(fake))})")


def scenario_tp_invalid_recover():
    """H4: TP invalid → need_recover_tp → 恢复链 TAKE_PROFIT_MARKET（C4 既有能力 → 应 PASS）"""
    states = make_states(user_modified=False)
    orders = [
        _make_sl_order(),                    # SL 有效
        _make_tp_order(side='BUY'),          # TP 方向反 → 无效
    ]
    fake = make_fake(states, orders)
    run_monitor(fake)

    create_n = fake.exchange.create_order.call_count
    cancel_n = fake.exchange.cancel_order.call_count
    types = [c.kwargs.get('type') for c in fake.exchange.create_order.call_args_list]
    report("H4/TP无效恢复链触发", create_n >= 1 and cancel_n >= 1,
           f"(cancel: {cancel_n}, create: {create_n})")
    report("H4/恢复创建TAKE_PROFIT_MARKET", 'TAKE_PROFIT_MARKET' in types, f"(类型: {types})")


if __name__ == '__main__':
    print("#" * 60)
    print("C5/SG4+SG4-B Create→Verify→Commit 提交一致性测试")
    print("状态: 绿阶段（25/25 PASS + G 组 2 项 SKIP 移出 C5 → SG5/D-004）")
    print("#" * 60)

    scenario_ast_retries()          # A 组
    scenario_ast_verify_integration()  # B 组
    scenario_verify_three_state()   # C/D/E/F 组
    # G 组 scenario_orphan_detect() 已 SKIP —— 孤儿保护单检测/告警移出 C5 → SG5/D-004（ChatGPT 裁决）
    print("\n" + "=" * 60)
    print("[SKIP] G/孤儿保护单检测/告警 —— 移出 C5 实施范围 → SG5/D-004 独立议题")
    print("[SKIP] G/只告警不仲裁 —— 同上，仲裁留 D-004")
    print("=" * 60)
    scenario_sl_invalid_recover()   # H1
    scenario_all_valid_noop()       # H2
    scenario_user_modified()        # H3
    scenario_tp_invalid_recover()   # H4

    print("\n" + "#" * 60)
    failed = [n for n, p in RESULTS if not p]
    passed = [n for n, p in RESULTS if p]
    print(f"❌ FAIL {len(failed)}/{len(RESULTS)}: {failed}")
    print(f"✅ PASS {len(passed)}/{len(RESULTS)} (基线): {passed}")
    if failed:
        print("→ 红阶段成立：能力缺失已锁定，可进入最小修改实施")
        sys.exit(1)
    print("⚠️ 无 FAIL —— 红阶段不成立：规格对应能力已存在，需复核测试是否锁错了规格")
