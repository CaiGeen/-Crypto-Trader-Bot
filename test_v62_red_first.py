# -*- coding: utf-8 -*-
"""v6.2 RED-first 测试（r2 基建修订版）。ChatGPT 授权：r5 Gate ✅ RED-first / ❌ production landing。

═══════════════════════════════════════════════════════════════════
r2 基建修订（ChatGPT 对第一批的 5 项指摘，全部修复）
═══════════════════════════════════════════════════════════════════
1. mk_batch 默认建立 ENTRY registry fixture（v6.2 归因写入的前置数据，
   缺失会让 GREEN 阶段 R1-a 因测试数据而非实现错误失败）；
2. 补 `_find_registry_identity_by_order_id`——ast 只读提取生产版
   （trader L3608-3624），v6.2 AFTER 的 finder 调用在 GREEN 可直接工作；
3. 棘轮仿真修正：先存 disk 快照再 merge（旧实现 slot.update 先把 True
   改成 False，只能模拟「字段缺失」、不能模拟「显式 False→True 棘轮」）；
4. mk_batch 默认 filled_details 拓扑合法：前 lfc 位正成交价 + 尾部 exact 0
   （旧默认全 77000 本身就是 hole，会被 r5 topology gate 正确判毁）；
5. cancel 注入支持 `cancel_by_id={'E5': ...}`（order-id 定向，RED/GREEN
   测同一事实场景，不依赖实现的高低层遍历顺序）。

环境解耦（ChatGPT：五道门前一并改）：本文件**不再 import
test_close_confirmation_v6**，自身实现 Fake/ast 提取/绑定的最小基建；
路径显式可注入（`V62_PROJECT_DIR` / `V62_HELPER_OVERRIDE` 环境变量）。

═══════════════════════════════════════════════════════════════════
RED 模式定义
═══════════════════════════════════════════════════════════════════
所有用例编码 **v6.2 正式 diff r5 规格**，当前对「旧实现」运行：
  - helper 层 = `送审附件_v6.1/new_helpers_v6.py`（v6.1 的 11 helper）；
  - 生产方法 = `trader_260725.py` 经 ast 只读提取的 `cancel_open_orders`
    与 `_find_registry_identity_by_order_id`（逐字原样）；
  - 监控片段 = 生产 L4858-4865 / L4749-4763 / L5324-5337 / L4826-4846 的
    语义等价转录（转录点在 docstring 标注）。

预期：**全部用例对旧实现失败（RED）** —— 失败即证明判别力。
任何用例对旧实现意外通过（GREEN_AGAINST_OLD）= 用例或规格不可判别，须修。

GREEN 阶段（后续）：实现源切换到 v6.2 正式 diff 代码，同批用例必须全绿；
随后按正式 diff §十一 变异清单 M11~M31 逐条验证转红。

纪律：**不修改生产三文件**（trader_260725.py / bot_runner.py / watchdog.py，
本文件仅 ast 只读提取）。
"""
import ast
import copy
import os
import textwrap
import threading

import ccxt

# ── 显式可注入路径（环境解耦）─────────────────────────────────────
PROJECT_DIR = os.environ.get('V62_PROJECT_DIR', r'G:/my-crypto-bot')
HELPER_PATH = (os.environ.get('V62_HELPER_OVERRIDE')
               or os.path.join(PROJECT_DIR, '送审附件_v6.1', 'new_helpers_v6.py'))
PROD_PATH = os.path.join(PROJECT_DIR, 'trader_260725.py')

SYM = 'BTC/USDT:USDT'


def _order(status, filled):
    return {'id': 'OID1', 'status': status, 'filled': filled, 'average': 77000.0}


POS_LONG = [{'symbol': SYM, 'side': 'long', 'contracts': 0.001,
             'info': {'symbol': 'BTCUSDT', 'positionSide': 'LONG'}}]


# ── ast 只读提取（生产方法 + v6.1 helper）────────────────────────

def _extract_functions(path, names, class_name=None):
    """从源文件 ast 提取指定函数/方法（隔离 exec，零 import 副作用）。

    class_name 给定时**真正约束到该 ClassDef 内**（r2 修正：旧实现对整棵
    AST walk、class_name 形同虚设——第二批要提取更多生产方法，假约束先修掉）：
      class_name is None → 搜全模块；
      class_name 给定   → 先定位 ClassDef(name==class_name)，只在其子树内找。
    """
    src = open(path, encoding='utf-8').read()
    tree = ast.parse(src)
    if class_name is None:
        scopes = [tree]
    else:
        scope = None
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == class_name:
                scope = node
                break
        if scope is None:
            raise LookupError(f'class {class_name} not found in {path}')
        scopes = [scope]
    out = {}
    for scope in scopes:
        for node in ast.walk(scope):
            if not isinstance(node, ast.FunctionDef):
                continue
            if node.name in names and node.name not in out:
                seg = ast.get_source_segment(src, node)
                if seg is None:
                    continue
                body = textwrap.dedent(seg)
                ns = {'time': __import__('time'), 'ccxt': ccxt,
                      'threading': threading, 'uuid': __import__('uuid')}
                exec(compile(body, path, 'exec'), ns)
                out[node.name] = ns[node.name]
    missing = [n for n in names if n not in out]
    if missing:
        raise LookupError(f'functions not found in {path}: {missing}')
    return out


V61 = _extract_functions(
    HELPER_PATH,
    ['_read_position_amt', '_fetch_close_order_state', '_confirm_close_filled',
     '_survey_same_side_batches', '_close_amount_guard',
     '_begin_close_request_if_active', '_derive_close_txn_vars',
     '_rollback_close_request_if_current', '_verify_entry_order_terminal',
     '_cancel_and_verify_entry_orders', '_set_close_reason_if_current'])

# 生产方法：cancel_open_orders（旧 producer #1，逐字原样）
OLD_cancel_open_orders = _extract_functions(
    PROD_PATH, ['cancel_open_orders'], class_name='CryptoTrader')['cancel_open_orders']

# 生产方法：_find_registry_identity_by_order_id（L3608-3624，r2 补齐的
# production-equivalent finder——v6.2 AFTER 在 GREEN 阶段会调用它）
OLD_find_registry_identity_by_order_id = _extract_functions(
    PROD_PATH, ['_find_registry_identity_by_order_id'],
    class_name='CryptoTrader')['_find_registry_identity_by_order_id']


def bind(fake, impl):
    for name, fn in impl.items():
        setattr(fake, name, fn.__get__(fake, type(fake)))


# ── Fake 基建 v2 ══════════════════════════════════════════════════

class FakeExchange62:
    """cancel_seq / cancel_by_id 双通道失败注入 + conditional/normal 端点路由。

    - cancel_by_id：order-id 定向注入（优先）。值可为单次结果 tuple，或
      **list（per-ID 序列，用尽 repeat-last）**——同一 order-id 多次调用
      得到固定脚本结果；
    - cancel_seq：按调用顺序注入，**用尽后 repeat-last**（r2 修正：last
      值持久化在实例 `_last_cancel`，不再退回 ('ok', None)）；
    - pos/order/oo 序列同样 repeat-last（`_last_pos/_last_order/_last_oo`
      实例持久化，r2 修正：旧实现 fallback 每次调用重建 = 假 repeat-last）；
    - fetch_order：params 含 stop=True → conditional_seq（algo 端点），
      否则 → order_seq（normal 端点）——建模 C5 端点路由 bug。
    """

    def __init__(self, pos_seq=None, order_seq=None, open_orders_seq=None,
                 cancel_seq=None, cancel_by_id=None, conditional_seq=None):
        self.pos_seq = list(pos_seq or [])
        self.order_seq = list(order_seq or [])
        self.oo_seq = list(open_orders_seq or [])
        self.cancel_seq = list(cancel_seq or [])
        self.cancel_by_id = dict(cancel_by_id or {})
        self._cond_seq = list(conditional_seq or [])
        self._cond_last = self._cond_seq[-1] if self._cond_seq else None
        # repeat-last 持久化（r2 修正：实例属性，不再每次调用重建 fallback）
        self._last_pos = self.pos_seq[-1] if self.pos_seq else None
        self._last_order = self.order_seq[-1] if self.order_seq else None
        self._last_oo = self.oo_seq[-1] if self.oo_seq else []
        self._last_cancel = ('ok', None)
        self.calls = []
        self.cancelled = []
        self.cancel_calls = []      # cancel 调用的 order_id 顺序（动作统计）

    def _next(self, key):
        pool, last_attr, default = {
            'pos': (self.pos_seq, '_last_pos', None),
            'order': (self.order_seq, '_last_order', None),
            'oo': (self.oo_seq, '_last_oo', []),
        }[key]
        if pool:
            v = pool.pop(0)
            setattr(self, last_attr, v)
        else:
            v = getattr(self, last_attr, default)
        if isinstance(v, Exception):
            raise v
        return v

    def fetch_positions(self, symbols=None):
        self.calls.append('fetch_positions')
        return self._next('pos')

    def fetch_open_orders(self, symbol, params=None):
        self.calls.append('fetch_open_orders')
        return self._next('oo')

    def fetch_order(self, order_id, symbol, params=None, retries=None):
        if params and params.get('stop'):
            self.calls.append(f'fetch_conditional:{order_id}')
            if self._cond_seq:
                v = self._cond_seq.pop(0)
                self._cond_last = v
            else:
                v = self._cond_last
            if isinstance(v, Exception):
                raise v
            return v
        self.calls.append(f'fetch_normal:{order_id}')
        return self._next('order')

    def cancel_order(self, order_id, symbol, params=None):
        self.calls.append(f'cancel:{order_id}')
        # 🔒 GREEN 终审：cancel endpoint routing 建模（与 fetch_order 对称）。
        # 项目端点契约：conditional（STOP/TAKE_PROFIT 条件单）→ params={'stop': True}；
        # normal（普通 LIMIT/MARKET 单）→ 不传 params。
        # 旧 'cancel:{id}' 标签保留以维持既有断言，新增端点标签供 endpoint 断言使用。
        if params and params.get('stop'):
            self.calls.append(f'cancel_conditional:{order_id}')
        else:
            self.calls.append(f'cancel_normal:{order_id}')
        self.cancelled.append(order_id)
        self.cancel_calls.append(order_id)
        if self.cancel_by_id and order_id in self.cancel_by_id:
            scripted = self.cancel_by_id[order_id]
            if isinstance(scripted, list):
                # per-ID 序列（GREEN 修正）：len>1 → pop(0)；len==1 → 返回末元素
                # 但不 pop。旧写法 pop 后把末元素写回会跳过中间项
                # （[a,b,c] → a 之后直接 c）。修正后 a → b → c → c → c...
                if len(scripted) > 1:
                    outcome = scripted.pop(0)
                else:
                    outcome = scripted[0]
            else:
                outcome = scripted          # 单次结果：同 ID 重复调用结果固定
        elif self.cancel_seq:
            outcome = self.cancel_seq.pop(0)
            self._last_cancel = outcome
        else:
            outcome = self._last_cancel
        kind, payload = outcome if isinstance(outcome, tuple) else ('ok', None)
        if kind == 'raise':
            if isinstance(payload, str):
                raise ccxt.ExchangeError(payload)
            raise payload
        return None

    def create_order(self, symbol=None, type=None, side=None, amount=None,
                     price=None, params=None, retries=None):
        self.calls.append('create_order')
        return {'id': 'OID1', 'status': 'closed', 'filled': amount, 'average': 77000.0}


class FakeSelf62:
    """_persist_states->bool 契约 + registry Fake + merge 布尔棘轮（r2 修正版）。"""

    RATCHET = ('pending_close', 'is_programmatic_cancel', 'settled_by_limit_close')
    # C 类保护集（生产 L44-47）：磁盘 registry 条目处于这些 state 时，
    # 不被陈旧快照降级/覆盖（PROGRAMMATIC_CANCELED 不可转出的落盘面）。
    MERGE_REGISTRY_PROTECTED = ('PENDING_CREATE', 'PENDING_VERIFY', 'NOT_CONFIRMED',
                                'CONFIRMED', 'MISMATCH', 'HARD_LOCK',
                                'PROGRAMMATIC_CANCELED')

    def __init__(self, exchange, states=None, persist_ok=True,
                 persist_fail_first_n=0):
        self.exchange = exchange
        self._state_lock = threading.Lock()      # 生产 L202：非重入 Lock
        self._states = states if states is not None else {}
        self.persist_ok = persist_ok
        # 🔒 r4：前 N 次 _persist_states 返回 False（之后成功）——建模
        # 「rollback 落盘失败但后续 reason CAS 成功」的时序（R2-g GREEN）。
        self._persist_fail_left = int(persist_fail_first_n)
        self.persisted = []
        self.saved = []
        self.tg_sent = []

    def _safe_api_call(self, func, *args, retries=5, delay=2, **kwargs):
        return func(*args, **kwargs)

    def load_all_states(self):
        # 🔒 r3 保真度修正：生产 load 是磁盘反序列化副本（新对象），persist 失败
        # 时磁盘不变。返回 live 引用会让「改了但 persist 失败」的状态假性生效
        # （R2-g 的磁盘 reason 判定会失真）——改为 deepcopy + persist 成功才写回。
        return copy.deepcopy(self._states)

    def _persist_states(self, all_states):
        # 🔒 GREEN 修正：历史快照用 deepcopy——避免后续嵌套 registry 修改
        # 污染已记录的 persist 历史（dict(v) 浅拷贝不够）。
        self.persisted.append(copy.deepcopy(all_states))
        if self._persist_fail_left > 0:
            self._persist_fail_left -= 1
            return False                          # 前 N 次落盘失败
        if self.persist_ok:
            self._states = all_states
        return self.persist_ok

    def save_batch_state(self, symbol, batch_id, data):
        self.saved.append((symbol, batch_id,
                           dict(data) if isinstance(data, dict) else data))
        # 🔒 GREEN 修正：先在 working 副本上 merge，_persist_states 成功才写回
        # self._states——persist 失败时 fake disk 不得被修改（生产语义：
        # load 副本 → 修改 → 落盘成功才可见）。旧实现直接改 self._states，
        # persist=False 也会「假落盘」。
        working = copy.deepcopy(self._states)
        slot = working.setdefault(symbol, {}).setdefault(batch_id, {})
        # 🔒 r2 修正：先存 disk 快照再 merge——旧实现 slot.update(data) 先把
        # True 改成 False，只能模拟「字段缺失」、不能模拟「显式 False→True 棘轮」。
        disk_before = {f: slot.get(f) for f in self.RATCHET}
        disk_registry = copy.deepcopy(slot.get('protection_registry') or {})
        if isinstance(data, dict):
            slot.update(data)
        for f in self.RATCHET:
            if disk_before.get(f) and not (data.get(f) if isinstance(data, dict) else False):
                slot[f] = True
                if isinstance(data, dict):
                    data[f] = True
        # 🔒 C 类保护（生产 merge 语义）：_update_registry 直写后的受保护
        # registry 条目，不被本次 merge 的陈旧快照 registry 覆盖。
        snap_reg = slot.get('protection_registry') or {}
        for k, dv in disk_registry.items():
            if (isinstance(dv, dict) and dv.get('state') in self.MERGE_REGISTRY_PROTECTED):
                sv = snap_reg.get(k)
                if not isinstance(sv, dict) or sv.get('state') != dv.get('state'):
                    snap_reg[k] = dv
        slot['protection_registry'] = snap_reg
        ok = self._persist_states(working)
        if ok:
            self._states = working
        return ok

    def _update_registry(self, symbol, batch_id, identity, state=None,
                         order_id=None, id_known=None, terminated_reason=None):
        # registry Fake：生产 _update_registry 的最小建模（直写 + 终态不可转出）
        b = self._states.setdefault(symbol, {}).setdefault(batch_id, {})
        reg = b.setdefault('protection_registry', {})
        entry = reg.setdefault(identity, {})
        if entry.get('state') in ('PROGRAMMATIC_CANCELED', 'ABSENT', 'FAILED'):
            return  # 终态不可转出（同态回写放行——本 Fake 无同态写需求）
        entry['state'] = state
        if order_id is not None:
            entry['order_id'] = order_id
        if id_known is not None:
            entry['id_known'] = id_known
        if terminated_reason is not None:
            entry['terminated_reason'] = terminated_reason
        entry['updated_at'] = 0.0

    def send_tg_notification(self, msg, level='info'):
        self.tg_sent.append((level, msg))


def _bind_all(fake):
    bind(fake, V61)
    bind(fake, {'cancel_open_orders': OLD_cancel_open_orders,
                '_find_registry_identity_by_order_id':
                    OLD_find_registry_identity_by_order_id})


def mk_batch(entry_orders, lfc, filled_details=None, amounts=None,
             with_registry=True, **extra):
    """批次 fixture（r2：默认拓扑合法 + ENTRY registry fixture）。

    - filled_details 默认 = 前 lfc 位正成交价 + 尾部 exact 0.0
      （r5 topology contract 的合法形态；显式传参可构造 hole/shape 损坏）；
    - with_registry=True 时为每张 entry 建立 registry identity
      （role=ENTRY / state=CONFIRMED / layer=idx / order_id）——v6.2 归因
      写入的前置数据（v6.2 只对已有 identity 做归因，不凭空创建）。
    """
    amounts = amounts if amounts is not None else [0.001] * max(len(entry_orders), 1)
    if filled_details is None:
        filled_details = ([77000.0] * min(lfc, len(entry_orders))
                          + [0.0] * max(len(entry_orders) - lfc, 0))
    b = {'side': 'BUY', 'is_active': True,
         'entry_orders': list(entry_orders),
         'last_filled_count': lfc,
         'target_amounts': list(amounts),
         'filled_details': list(filled_details),
         'total_entry_fee': 0.01,
         'tp_order_id': 'TP1', 'current_sl_id': 'SL1',
         'params_base': {}, 'is_hedge_mode': True,
         'close_phase': 0, 'pending_close': False}
    if with_registry:
        reg = {}
        for idx, oid in enumerate(entry_orders):
            reg[f'batch_A:ENTRY:{idx}:LONG'] = {
                'role': 'ENTRY', 'state': 'CONFIRMED',
                'layer': idx, 'side': 'LONG',
                'order_id': oid, 'id_known': True,
                'order_kind': 'conditional', 'updated_at': 0.0}
        b['protection_registry'] = reg
    b.update(extra)
    return b


def _e511(marker):
    return ccxt.ExchangeError(marker)


# ── 监控片段语义等价转录（转录点在 docstring 标注）──────────────

def old_monitor_exit(batch_filled_count, disk_batch):
    """生产 L4858-4865 转录：zero-filled 退出判据（break → True）。"""
    if batch_filled_count == 0:
        if disk_batch and (disk_batch.get('is_programmatic_cancel', False)
                           or disk_batch.get('pending_close', False)):
            return True
    return False


def old_sl_attribution(latest_b_data_check, current_sl_id, user_modified):
    """生产 L5324-5337 转录：SL 失效归因（返回 need_recover_sl）。

    （原片段前半的 registry ABSENT 写入与本案判定无关，略——转录点：
    is_programmatic 判定与 need_recover_sl 分支逐行保留。）"""
    is_programmatic = latest_b_data_check.get('is_programmatic_cancel', False)
    if is_programmatic:
        return False    # 程序撤单 → 不补挂
    if user_modified:
        return False
    return True         # 风控异常 → 自动补挂


def old_entry_detection(latest_b_data_check, order_id):
    """生产 L4749-4763 转录：ENTRY canceled 检测（返回 manual_canceled_detected）。"""
    is_programmatic = latest_b_data_check.get('is_programmatic_cancel', False)
    if is_programmatic:
        return False    # 不设置 manual_canceled_detected
    return True


def old_producer2(entry_orders, filled_layers, canceled_layers,
                  exchange, symbol, fake, batch_id):
    """生产 L4826-4846 转录：monitor 手动撤单处理（含 L4842-4843 截断）。"""
    cancelled_count = 0
    for idx, order_id in enumerate(entry_orders):
        if filled_layers[idx] or canceled_layers[idx]:
            continue
        try:
            exchange.cancel_order(order_id, symbol, params={'stop': True})
            canceled_layers[idx] = True
            cancelled_count += 1
        except Exception as e:
            print(f"  └─ ⚠️ 撤销第 {idx + 1} 层挂单失败: {e}")
    latest_all = fake.load_all_states()
    latest_b_data = latest_all.get(symbol, {}).get(batch_id, {})
    if latest_b_data:
        remaining_orders = [entry_orders[i] for i in range(len(entry_orders))
                            if filled_layers[i]]
        latest_b_data['entry_orders'] = remaining_orders
        latest_b_data['is_programmatic_cancel'] = True
        fake.save_batch_state(symbol, batch_id, latest_b_data)
    return cancelled_count


# ── RED 用例（编码 v6.2 r5 规格）──────────────────────────────────

def case_R1_a_positional_and_sticky():
    """R1-a：不等价场景下台账永不压缩 + 不写 sticky flag + E4 registry 归因。"""
    fx = FakeExchange62(cancel_by_id={'E5': ('raise', _e511('-2011 Unknown order'))})
    fake = FakeSelf62(fx)
    fake._states = {SYM: {'batch_A': mk_batch(
        ['E1', 'E2', 'E3', 'E4', 'E5'], 3,
        amounts=[0.001, 0.002, 0.003, 0.004, 0.005])}}
    _bind_all(fake)
    fake.cancel_open_orders('batch_A')
    b = fake._states[SYM]['batch_A']
    # v6.2 规格：positional ledger 永不压缩（E5 层号不得漂移）
    assert list(b['entry_orders']) == ['E1', 'E2', 'E3', 'E4', 'E5'], \
        f"entry_orders 被压缩为 {b['entry_orders']}"
    # v6.2 规格：不写 batch-global sticky flag（旧代码 L6872 写入）
    assert not b.get('is_programmatic_cancel'), \
        "旧实现写入了 sticky is_programmatic_cancel=True"
    # v6.2 规格：E4（cancel 成功 + verifier=gone）写 registry 归因
    reg = b.get('protection_registry') or {}
    e4 = [e for e in reg.values() if e.get('order_id') == 'E4']
    assert e4 and e4[0].get('state') == 'PROGRAMMATIC_CANCELED', \
        f"E4 未写 PROGRAMMATIC_CANCELED（registry={reg}）"


def case_R1_i_dash2011_then_filled():
    """R1-i：cancel(E5) → -2011 且 E5 实际已成交 → E4 不得被撤（高→低+verifier）。"""
    fx = FakeExchange62(
        cancel_by_id={'E5': ('raise', _e511('-2011 Unknown order'))},
        conditional_seq=[T_order_closed_0_005 := _order('closed', 0.005)],
    )
    fake = FakeSelf62(fx)
    fake._states = {SYM: {'batch_A': mk_batch(
        ['E1', 'E2', 'E3', 'E4', 'E5'], 3,
        amounts=[0.001, 0.002, 0.003, 0.004, 0.005])}}
    _bind_all(fake)
    fake.cancel_open_orders('batch_A')
    e4_cancels = fx.cancel_calls.count('E4')
    # v6.2 规格：E5 事实为 filled → 立即停止，E4 保持 active
    assert e4_cancels == 0, f"E4 被撤销 {e4_cancels} 次（应为 0）"


def case_R1_b_g_zero_filled():
    """R1-b/R1-g：zero-filled 部分失败 → 台账保留、返回 False、不写终止标志。"""
    fx = FakeExchange62(cancel_by_id={'E2': ('raise', _e511('-2011 Unknown order'))})
    fake = FakeSelf62(fx)
    fake._states = {SYM: {'batch_A': mk_batch(['E1', 'E2'], 0)}}
    _bind_all(fake)
    ret = fake.cancel_open_orders('batch_A')
    b = fake._states[SYM]['batch_A']
    # v6.2 规格：E2 未确认终态 → 台账原样（旧代码 L6925 entry_orders=[]）
    assert list(b['entry_orders']) == ['E1', 'E2'], \
        f"zero-filled 台账被清空/压缩为 {b['entry_orders']}"
    assert ret[0] is False, f"部分失败应返回 False，实际 {ret[0]!r}"
    # v6.2 规格：unresolved → 绝不写终止标志（monitor 不退出）
    assert not b.get('pending_close'), "zero-filled unresolved 写入了 pending_close"
    assert int(b.get('close_phase', 0) or 0) == 0, "zero-filled unresolved 写入了 close_phase>=1"


def case_R1_l_exit_criteria():
    """R1-l：真跑 monitor 退出判据——sticky True 不得触发 break。"""
    disk = {'is_programmatic_cancel': True, 'pending_close': False}
    # v6.2 规格：退出资格只看 pending_close
    assert old_monitor_exit(0, disk) is False, \
        "sticky is_programmatic_cancel=True 触发了 monitor break（棘轮 P0）"


def case_R1_o_sl_attribution():
    """R1-o：ENTRY-only cancel 后 SL 外部失效 → need_recover_sl 必须 True。"""
    latest = {'is_programmatic_cancel': True,          # sticky 污染源（旧模型）
              'protection_registry': {
                  'batch_A:ENTRY:4:LONG': {'role': 'ENTRY', 'state': 'CONFIRMED',
                                           'order_id': 'E4', 'layer': 4}}}
    # SL 'SLX' 在 registry 无 PROGRAMMATIC_CANCELED 记录 = 非程序终结
    need = old_sl_attribution(latest, 'SLX', user_modified=False)
    # v6.2 规格：归因只认 order-ID registry → SL 外部撤销必须自动补挂
    assert need is True, \
        "sticky bool 污染：SL 外部撤销被判为程序撤单，need_recover_sl 未置位"


def case_R1_p_entry_detection():
    """R1-p：精确归因——E5 programmatic（registry）、E4 manual（无记录）。"""
    latest = {'is_programmatic_cancel': True,          # sticky 污染源（旧模型）
              'protection_registry': {
                  'batch_A:ENTRY:4:LONG': {'role': 'ENTRY',
                                           'state': 'PROGRAMMATIC_CANCELED',
                                           'order_id': 'E5', 'layer': 4}}}
    # E4：用户手工撤销，registry 无记录 → 必须发手动撤单提醒
    detected_e4 = old_entry_detection(latest, 'E4')
    # E5：程序终结（registry 有 PROGRAMMATIC_CANCELED）→ 不提醒
    detected_e5 = old_entry_detection(latest, 'E5')
    assert detected_e4 is True, \
        "E4（用户手工撤）被 sticky bool 吞掉，未发手动撤单提醒"
    assert detected_e5 is False, "E5（程序终结）误发手动撤单提醒"


def case_R1_s_producer2_bitmap():
    """R1-s/R1-m：producer #2 verifier=filled 时不得置 canceled_layers、E4 不得撤。"""
    fx = FakeExchange62(
        cancel_by_id={'E5': ('ok', None)},
        conditional_seq=[_order('closed', 0.005)],      # E5 实际已成交
    )
    fake = FakeSelf62(fx)
    fake._states = {SYM: {'batch_A': mk_batch(
        ['E1', 'E2', 'E3', 'E4', 'E5'], 3)}}
    _bind_all(fake)
    filled_layers = [True, True, True, False, False]
    canceled_layers = [False] * 5
    old_producer2(['E1', 'E2', 'E3', 'E4', 'E5'], filled_layers, canceled_layers,
                  fx, SYM, fake, 'batch_A')
    # v6.2 规格：bitmap 只在 verifier=gone 后置位（filled 层不得标记 canceled）
    assert canceled_layers[4] is False, \
        "E5 实际已成交却被置 canceled_layers=True（下一轮成交识别被永久挡住）"
    # v6.2 规格：高→低 + verifier 定案 → E4 不得在 E5 filled 后被撤
    assert fx.cancel_calls.count('E4') == 0, \
        f"E4 被撤销 {fx.cancel_calls.count('E4')} 次（E5 filled 后应为 0）"


def case_R1_k_derive_hole():
    """R1-k：filled_details=[p,p,p,0,p]/N=4 → derive 必须 entry_fill_hole。"""
    fx = FakeExchange62()
    fake = FakeSelf62(fx)
    _bind_all(fake)
    tb = mk_batch(['E1', 'E2', 'E3', 'E4', 'E5'], 4,
                  filled_details=[77000.0, 77001.0, 77002.0, 0.0, 77003.0])
    ok, _vars, why = fake._derive_close_txn_vars(tb, 'batch_A')
    # v6.2 规格：hole → Fail-Closed，绝不产生 close amount
    assert ok is False, f"hole 台账通过 derive（why={why!r}）"
    assert 'entry_fill_hole' in why, f"why 未含 entry_fill_hole：{why!r}"


def case_R1_n_derive_shape():
    """R1-n：filled_details len=3 / target len=5 / N=3 → shape_invalid。"""
    fx = FakeExchange62()
    fake = FakeSelf62(fx)
    _bind_all(fake)
    tb = mk_batch(['E1', 'E2', 'E3'], 3,
                  filled_details=[77000.0, 77001.0, 77002.0],
                  amounts=[0.001, 0.002, 0.003, 0.004, 0.005],
                  with_registry=False)
    tb['entry_orders'] = ['E1', 'E2', 'E3']          # 截断形状（F-1 legacy）
    ok, _vars, why = fake._derive_close_txn_vars(tb, 'batch_A')
    # v6.2 规格：向量完整性门（tail 无槽位可证 → Fail-Closed）
    assert ok is False, f"shape 损坏台账通过 derive（why={why!r}）"
    assert 'filled_details_shape_invalid' in why, f"why 未含 shape_invalid：{why!r}"


def case_R1_t_derive_tail_invalid():
    """R1-t：tail 含 -1（非法值）→ derive Fail-Closed。"""
    fx = FakeExchange62()
    fake = FakeSelf62(fx)
    _bind_all(fake)
    tb = mk_batch(['E1', 'E2', 'E3', 'E4'], 2,
                  filled_details=[77000.0, 77001.0, -1.0, 77003.0])
    ok, _vars, why = fake._derive_close_txn_vars(tb, 'batch_A')
    assert ok is False, f"tail 非法值台账通过 derive（why={why!r}）"
    assert ('entry_fill_hole' in why) or ('ledger_invalid' in why), \
        f"why 未含 hole/ledger_invalid：{why!r}"


def case_R1_u_derive_prefix_nan():
    """R1-u：prefix 含 NaN → derive Fail-Closed。"""
    fx = FakeExchange62()
    fake = FakeSelf62(fx)
    _bind_all(fake)
    tb = mk_batch(['E1', 'E2', 'E3'], 2,
                  filled_details=[77000.0, float('nan'), 0.0])
    ok, _vars, why = fake._derive_close_txn_vars(tb, 'batch_A')
    assert ok is False, f"prefix NaN 台账通过 derive（why={why!r}）"
    assert ('ledger_invalid' in why) or ('entry_fill_hole' in why), \
        f"why 未含 ledger_invalid/hole：{why!r}"


def case_R3_h1_coverage_nan():
    """R3-h1：contracts=NaN → _read_position_amt=None → guard=None（UNKNOWN 不得 PASS）。"""
    nan_pos = [{'symbol': SYM, 'side': 'long',
                'contracts': float('nan'),
                'info': {'symbol': 'BTCUSDT', 'positionSide': 'LONG'}}]
    fx = FakeExchange62(pos_seq=[nan_pos])
    fake = FakeSelf62(fx)
    _bind_all(fake)
    amt = fake._read_position_amt(SYM, 'BUY', True)
    # v6.2 规格：非有限持仓值 = 不可判定 → None
    assert amt is None, f"NaN contracts 被当作数值返回：{amt!r}"
    guard_amt, _detail = fake._close_amount_guard(SYM, 'BUY', True, 0.001, 'batch_A')
    # v6.2 规格：UNKNOWN → Fail-Closed 不发单
    assert guard_amt is None, f"coverage guard 对 NaN 返回 {guard_amt!r}（fail-open）"


def case_R3_h2_survey_topology():
    """R3-h2：B 批次 hole → survey (-1,-1,-1) → guard None（cross-batch 归因不可证明）。"""
    fx = FakeExchange62(pos_seq=[
        [{'symbol': SYM, 'side': 'long', 'contracts': 1.5,
          'info': {'symbol': 'BTCUSDT', 'positionSide': 'LONG'}}]])
    fake = FakeSelf62(fx)
    _bind_all(fake)
    fake._states = {SYM: {
        'batch_A': mk_batch(['E1'], 1, filled_details=[77000.0], amounts=[1.0]),
        'batch_B': mk_batch(['B1', 'B2'], 1,
                            filled_details=[0.0, 70000.0],      # hole
                            amounts=[0.1, 10.0], with_registry=False),
    }}
    others, sum_all, blocking = fake._survey_same_side_batches(SYM, 'BUY', 'batch_A')
    # v6.2 规格：B topology 损坏 → coverage 不可证明
    assert (others, sum_all, blocking) == (-1, -1, -1), \
        f"hole 批次 B 未触发 Fail-Closed：survey={(others, sum_all, blocking)}"
    guard_amt, _detail = fake._close_amount_guard(SYM, 'BUY', True, 1.0, 'batch_A')
    assert guard_amt is None, f"guard 对 hole 批次返回 {guard_amt!r}（假 coverage）"


def case_R3_b_commit_guard_missing():
    """R3-b：v6.2 要求 `_commit_limit_close_order_if_current`（source-state guard）。"""
    fx = FakeExchange62()
    fake = FakeSelf62(fx)
    _bind_all(fake)
    # v6.2 规格：专用窄 helper 必须存在且执行 source-state guard
    assert hasattr(fake, '_commit_limit_close_order_if_current'), \
        "旧实现缺少 _commit_limit_close_order_if_current（durable commit + source-state guard）"


def case_R1_ef_recovery_missing():
    """R1-e/f：v6.2 要求 `_pending_entry_ids_for_gate`（registry 按 layer 恢复）。"""
    fx = FakeExchange62()
    fake = FakeSelf62(fx)
    _bind_all(fake)
    # v6.2 规格：registry 恢复 helper 必须存在（截断台账的唯一安全恢复路径）
    assert hasattr(fake, '_pending_entry_ids_for_gate'), \
        "旧实现缺少 _pending_entry_ids_for_gate（截断台账无法安全恢复 ENTRY ID）"


def case_R2_f_first_abnormal_wins():
    """R2-f：reason 已 abnormal 时不得被 generic except 覆盖（first-abnormal-wins）。"""
    op_id = 'd' * 32
    fx = FakeExchange62()
    fake = FakeSelf62(fx)
    _bind_all(fake)
    fake._states = {SYM: {'batch_A': dict(
        mk_batch(['E1'], 1),
        close_phase=1, pending_close=True, close_op_id=op_id,
        close_reason='market_confirm_unknown')}}                 # 已是异常态
    ok, why = fake._set_close_reason_if_current(SYM, 'batch_A', op_id, 'settlement_error')
    # v6.2 规格：不覆盖第一现场；返回 reason_already_abnormal
    assert why.startswith('reason_already_abnormal'), \
        f"abnormal reason 被覆盖（why={why!r}）——第一现场丢失"
    disk_reason = fake._states[SYM]['batch_A']['close_reason']
    assert disk_reason == 'market_confirm_unknown', \
        f"磁盘 reason 被覆盖为 {disk_reason!r}"


# ── 第二批：LIMIT 流程 / 冻结分型 / outer except（r3 复审授权范围）────

# 旧实现：v6.1 冻结告警分型（送审稿改动 4，L2404-2406 黑名单 + 3600 窗口）
OLD_FREEZE_CRITICAL_REASONS = ('market_confirm_unknown', 'market_partial',
                               'settlement_stuck',
                               'market_entry_unknown', 'limit_entry_unknown')

# 新规格：r4 双集合（正式 diff 10.5）
FREEZE_QUIET_REASONS = {'market_confirming', 'limit_pending_normal'}


def old_freeze_classify(disk_batch, batch_id, fake, now):
    """生产送审稿改动 4（L2391-2416）转录：冻结告警分型 + 3600 窗口。

    返回 'critical' / 'quiet'。黑名单外 reason（含 limit_creating——
    v6.1 根本不存在该值）→ 只 print = quiet（旧实现静默）。"""
    close_phase = int((disk_batch or {}).get('close_phase', 0) or 0)
    if close_phase < 1 and not (disk_batch or {}).get('pending_close'):
        return 'quiet'                                   # 未冻结，无告警语义
    reason = (disk_batch or {}).get('close_reason') or 'settlement_stuck'
    if reason in OLD_FREEZE_CRITICAL_REASONS:
        if now - fake._freeze_alerted.get(batch_id, 0) >= 3600:
            fake._freeze_alerted[batch_id] = now
            return 'critical'
        return 'quiet'                                   # 窗口内去重
    return 'quiet'                                       # 黑名单外 → 静默


def spec_freeze_classify(disk_batch, batch_id, fake, now):
    """v6.2-r4 规格：FREEZE_QUIET_REASONS 之外一律周期 critical（3600 窗口）。"""
    close_phase = int((disk_batch or {}).get('close_phase', 0) or 0)
    if close_phase < 1 and not (disk_batch or {}).get('pending_close'):
        return 'quiet'
    reason = (disk_batch or {}).get('close_reason') or 'settlement_stuck'
    if reason not in FREEZE_QUIET_REASONS:
        if now - fake._freeze_alerted.get(batch_id, 0) >= 3600:
            fake._freeze_alerted[batch_id] = now
            return 'critical'
        return 'quiet'                                   # 窗口内去重
    return 'quiet'


def spec_tp_gate(fake, target_symbol, target_b_data, current_filled_amount):
    """v6.2-r4 改动 9.0 规格模拟器：TP factual gate。

    cancel attempt（成功/异常都只算动作）→ conditional 端点 fetch →
    六态语义映射 → **仅 TERMINAL_ZERO 放行**。RED 阶段以规格模拟器为
    oracle（v6.1 的 _confirm_close_filled 有端点 bug，不能当 oracle）；
    GREEN 阶段由真实现替换。返回 (proceed, verdict)。"""
    _tp_old_id = target_b_data.get('tp_order_id')
    if not _tp_old_id:
        return True, 'no_tp'
    try:
        fake._safe_api_call(fake.exchange.cancel_order, _tp_old_id, target_symbol,
                            params={'stop': True})
    except Exception:
        pass                                              # 动作结果不下结论
    try:
        order = fake._safe_api_call(fake.exchange.fetch_order, _tp_old_id,
                                    target_symbol, params={'stop': True})
    except ccxt.OrderNotFound:
        return False, 'UNKNOWN'                           # OrderNotFound ≠ 没成交
    except Exception:
        return False, 'UNKNOWN'
    status = str((order or {}).get('status') or '').lower()
    filled = (order or {}).get('filled')
    if status in ('canceled', 'expired', 'rejected') and filled == 0:
        return True, 'TERMINAL_ZERO'
    if status in ('closed', 'filled'):
        return False, 'CONFIRMED_FULL'
    if status == 'open':
        return False, 'PENDING'
    return False, 'UNKNOWN'


def old_limit_tp_cancel_and_create(fake, target_symbol, target_b_data,
                                   current_filled_amount, limit_price):
    """生产 L7559-7593 逐字转录：撤 TP（-2011→事实终态）+ **无条件** create LIMIT。

    返回 (tp_terminal_ok, created)。转录点：-2011 分支、create 参数构造
    （hedge positionSide / 单向 reduceOnly）、create 调用本身。"""
    _tp_terminal_ok = False
    _tp_old_id = target_b_data.get('tp_order_id')
    side = target_b_data.get('side')
    if _tp_old_id:
        try:
            fake._safe_api_call(fake.exchange.cancel_order, _tp_old_id, target_symbol,
                                params={'stop': True})
            print(f"  └─ 已撤销旧止盈单: {_tp_old_id}")
            _tp_terminal_ok = True
        except Exception as _tp_cancel_e:
            if 'Unknown order' in str(_tp_cancel_e) or '-2011' in str(_tp_cancel_e):
                print(f"  └─ 旧止盈单 {_tp_old_id} 已不存在")
                _tp_terminal_ok = True  # 已离开交易所 = 事实终态（r3 否决的语义）
            else:
                print(f"  └─ ⚠️ 撤销旧止盈单失败（TP 可能仍在场）: {_tp_cancel_e}")

    # 旧实现：无论 _tp_terminal_ok 真伪，都继续 create LIMIT（无 TP gate）
    close_side = 'sell' if side == 'BUY' else 'buy'
    order_params = target_b_data['params_base'].copy()
    if target_b_data.get('is_hedge_mode', False):
        order_params['positionSide'] = 'LONG' if side == 'BUY' else 'SHORT'
    else:
        order_params['reduceOnly'] = True
    order = fake._safe_api_call(
        fake.exchange.create_order,
        symbol=target_symbol,
        type='LIMIT',
        side=close_side,
        amount=current_filled_amount,
        price=limit_price,
        params=order_params,
        retries=1
    )
    return _tp_terminal_ok, order


def old_outer_except_precreate(fake, target_symbol, batch_id, close_op_id, err):
    """送审稿 L622-650 转录：市价 outer except 的 pre-create 分支
    （close_order_placed=False：CAS rollback → 失败只 critical，无 reason 切换）。
    返回 (ret, )；磁盘 reason 由调用方检查。"""
    try:
        _rb_ok, _rb_why = fake._rollback_close_request_if_current(
            target_symbol, batch_id, close_op_id)
    except Exception as _rb_err:
        _rb_ok, _rb_why = False, f'CAS 调用异常（{_rb_err}）'
    if _rb_ok:
        print(f"  └─ 🔄 平仓失败回滚：CAS 原子回滚成功（{_rb_why}）")
    else:
        print(f"  └─ ⚠️ 回滚被拒绝: {_rb_why}（状态已被其他操作接管，需人工检查）")
        fake.send_tg_notification(
            f"🚨【资金安全】市价平仓失败且回滚被拒绝！\n批次: `{batch_id}`\n"
            f"原因: {_rb_why}\n请立即检查仓位是否仍有 SL 保护！",
            level='critical')
    return False, f"❌ 市价平仓失败: {err}"


def old_monitor_fill_round(entry_orders, filled_layers, canceled_layers,
                           target_amounts, fake, batch_id):
    """生产 L4659-4716 转录：成交收集轮。

    ⚠️ 旧实现不存在 hole 检测通道（本转录如实不含）——R1-h 的被测缺失：
    对 [T,T,T,F,T] 型 hole bitmap，旧监控不产生任何 critical。"""
    for idx, order_id in enumerate(entry_orders):
        if filled_layers[idx]:
            continue
        if canceled_layers[idx]:
            continue
        # 旧实现此处 fetch 补查 ENTRY 状态并更新 filled_layers/filled_details
        # （与 hole 告警通道无关，本转录省略——R1-h 断言的是告警通道缺失）
    return len([tg for tg in fake.tg_sent
                if tg[0] == 'critical'
                and ('不连续' in tg[1] or 'hole' in tg[1])])


# ── 第二批用例 ────────────────────────────────────────────────────

def case_R3_c_seed_grace():
    """R3-c：limit_creating——活进程 seed → grace；crash 重启（无 seed）→ 必须 loud。"""
    disk = {'close_phase': 1, 'pending_close': True, 'close_reason': 'limit_creating'}
    now = 1_000_000.0
    # (a) 活进程：BEGIN seed grace → spec 判 quiet（不误报）
    fx = FakeExchange62()
    seeded = FakeSelf62(fx)
    seeded._freeze_alerted = {'batch_A': now}
    seeded_disc = dict(disk)
    # spec_freeze_classify 读 _freeze_alerted（seeded）
    v = spec_freeze_classify(seeded_disc, 'batch_A', seeded, now)
    assert v == 'quiet', f"seeded grace 失效：{v!r}"
    # (b) crash 重启：内存 seed 消失 → 规格要求 critical；旧实现为 quiet → RED
    fx_old = FakeExchange62()
    fake_old = FakeSelf62(fx_old)
    fake_old._freeze_alerted = {}                        # 重启 = dict 清空
    old_v = old_freeze_classify(dict(disk), 'batch_A', fake_old, now)
    fx_new = FakeExchange62()
    fake_new = FakeSelf62(fx_new)
    fake_new._freeze_alerted = {}
    new_v = spec_freeze_classify(dict(disk), 'batch_A', fake_new, now)
    # v6.2 规格：limit_creating 不在 FREEZE_QUIET_REASONS → 立即 loud
    assert new_v == 'critical', f"crash 后 limit_creating 未 loud（spec={new_v!r}）"
    assert old_v == 'critical', \
        f"旧实现对 limit_creating 静默（{old_v!r}）——规格施加于旧实现必须 critical"


def case_R3_d_tp_cancel_network():
    """R3-d：TP cancel 网络异常 → TP 事实不明 → 禁止 create LIMIT。"""
    fx = FakeExchange62(
        cancel_by_id={'TP1': ('raise', RuntimeError('network error'))},
        conditional_seq=[_order('open', 0.001)],
    )
    fake = FakeSelf62(fx)
    fake._states = {SYM: {'batch_A': mk_batch(['E1'], 1)}}
    _bind_all(fake)
    tb = fake._states[SYM]['batch_A']
    proceed, verdict = spec_tp_gate(fake, SYM, tb, 0.001)
    assert proceed is False, f"TP 事实不明仍放行（verdict={verdict!r}）"
    old_ok, order = old_limit_tp_cancel_and_create(
        fake, SYM, tb, 0.001, 77000.0)
    # v6.2 规格：TP 事实不明 → create LIMIT 次数必须为 0
    assert fx.calls.count('create_order') == 0, \
        "旧实现无条件 create LIMIT（TP 事实不明仍挂单）"


def case_R3_e_tp_2011_fact_matrix():
    """R3-e：事实矩阵——action outcome 不重要，fact outcome 才重要。"""
    # ① -2011 + conditional fetch NOT_CONFIRMED（OrderNotFound）→ BLOCK（RED）
    fx = FakeExchange62(
        cancel_by_id={'TP1': ('raise', _e511('-2011 Unknown order'))},
        conditional_seq=[ccxt.OrderNotFound('not found')],
    )
    fake = FakeSelf62(fx)
    fake._states = {SYM: {'batch_A': mk_batch(['E1'], 1)}}
    _bind_all(fake)
    tb = fake._states[SYM]['batch_A']
    proceed, verdict = spec_tp_gate(fake, SYM, tb, 0.001)
    assert proceed is False and verdict == 'UNKNOWN', \
        f"-2011+OrderNotFound 未被阻断：{proceed!r}/{verdict!r}"
    old_ok, _order_ret = old_limit_tp_cancel_and_create(fake, SYM, tb, 0.001, 77000.0)
    assert fx.calls.count('create_order') == 0, \
        "旧实现在 -2011+UNKNOWN 下仍 create LIMIT"
    # ② -2011 + conditional fetch canceled 且 filled==0 → PROCEED（规格正向，
    #    同时是端点路由 bug 的 GREEN 期判别器：normal 端点会拿到 NOT_CONFIRMED）
    fx2 = FakeExchange62(
        cancel_by_id={'TP1': ('raise', _e511('-2011 Unknown order'))},
        conditional_seq=[_order('canceled', 0.0)],
    )
    fake2 = FakeSelf62(fx2)
    fake2._states = {SYM: {'batch_A': mk_batch(['E1'], 1)}}
    _bind_all(fake2)
    tb2 = fake2._states[SYM]['batch_A']
    proceed2, verdict2 = spec_tp_gate(fake2, SYM, tb2, 0.001)
    assert proceed2 is True and verdict2 == 'TERMINAL_ZERO', \
        f"-2011+canceled/0 应放行：{proceed2!r}/{verdict2!r}"


def case_R3_f_tp_cancel_success_but_filled():
    """R3-f：cancel(TP) 正常返回但 fetch=filled → 禁止 create LIMIT。"""
    fx = FakeExchange62(
        cancel_by_id={'TP1': ('ok', None)},
        conditional_seq=[_order('closed', 0.001)],      # TP 实际已成交
    )
    fake = FakeSelf62(fx)
    fake._states = {SYM: {'batch_A': mk_batch(['E1'], 1)}}
    _bind_all(fake)
    tb = fake._states[SYM]['batch_A']
    proceed, verdict = spec_tp_gate(fake, SYM, tb, 0.001)
    assert proceed is False and verdict == 'CONFIRMED_FULL', \
        f"TP 已成交仍放行：{proceed!r}/{verdict!r}"
    old_ok, _order_ret = old_limit_tp_cancel_and_create(fake, SYM, tb, 0.001, 77000.0)
    assert fx.calls.count('create_order') == 0, \
        "旧实现 cancel 正常返回即挂 LIMIT（TP 已成交仍重叠减仓）"


def case_R3_g_coverage_conflict():
    """R3-g：A/B 台账各 0.001、aggregate 实际 0.001 → guard conflict → 禁止挂 LIMIT。"""
    fx = FakeExchange62(pos_seq=[
        [{'symbol': SYM, 'side': 'long', 'contracts': 0.001,
          'info': {'symbol': 'BTCUSDT', 'positionSide': 'LONG'}}]])
    fake = FakeSelf62(fx)
    _bind_all(fake)
    fake._states = {SYM: {
        'batch_A': mk_batch(['E1'], 1, filled_details=[77000.0], amounts=[0.001]),
        'batch_B': mk_batch(['B1'], 1, filled_details=[77000.0], amounts=[0.001],
                            with_registry=False),
    }}
    safe_amount, detail = fake._close_amount_guard(SYM, 'BUY', True, 0.001, 'batch_A')
    # v6.1 既有 guard：多批次 actual < 台账合计 → None（归因冲突）
    assert safe_amount is None, f"coverage 冲突未检出：{safe_amount!r}"
    tb = fake._states[SYM]['batch_A']
    old_ok, _order_ret = old_limit_tp_cancel_and_create(fake, SYM, tb, 0.001, 77000.0)
    # v6.2 规格：coverage 冲突 → create LIMIT 次数必须为 0
    assert fx.calls.count('create_order') == 0, \
        "旧实现无 coverage guard，冲突下仍 create LIMIT（错平其他批次）"


def case_R1_h_monitor_hole():
    """R1-h：hole bitmap [T,T,T,F,T] → monitor 必须 critical 暴露（旧实现无通道）。"""
    fx = FakeExchange62()
    fake = FakeSelf62(fx)
    fake.tg_sent.append(('info', '🎯 买单成交提醒'))        # 旧流程正常提醒
    n = old_monitor_fill_round(['E1', 'E2', 'E3', 'E4', 'E5'],
                               [True, True, True, False, True],
                               [False, False, False, True, False],
                               [0.001] * 5, fake, 'batch_A')
    # v6.2 规格：hole 必须 critical 暴露（prefix 假设失效须人工知情）
    assert n >= 1, "旧实现对成交位不连续（hole）零告警——prefix 失效无人知情"


def case_R2_g_precreate_rollback_failed():
    """R2-g：BEGIN→pre-create 异常→rollback persist 失败 → reason 必须 CAS 写 abnormal。"""
    op_id = 'e' * 32
    fx = FakeExchange62()
    fake = FakeSelf62(fx, persist_ok=False)              # rollback 落盘失败
    fake._states = {SYM: {'batch_A': dict(
        mk_batch(['E1'], 1),
        close_phase=1, pending_close=True, close_op_id=op_id,
        close_reason='market_confirming')}}              # BEGIN 后的正常态
    _bind_all(fake)
    old_outer_except_precreate(fake, SYM, 'batch_A', op_id, RuntimeError('boom'))
    disk_reason = fake._states[SYM]['batch_A']['close_reason']
    # v6.2 规格：rollback 失败 → CAS 写 txn_aborted_rollback_failed
    assert disk_reason == 'txn_aborted_rollback_failed', \
        (f"rollback 落盘失败后 reason 停在正常态（{disk_reason!r}）"
         "——冻结告警将静默")


# ── RED 运行器 ────────────────────────────────────────────────────
CASES = [(case_R1_a_positional_and_sticky, 'red'),
         (case_R1_i_dash2011_then_filled, 'red'),
         (case_R1_b_g_zero_filled, 'red'),
         (case_R1_l_exit_criteria, 'red'),
         (case_R1_o_sl_attribution, 'red'),
         (case_R1_p_entry_detection, 'red'),
         (case_R1_s_producer2_bitmap, 'red'),
         (case_R1_k_derive_hole, 'red'),
         (case_R1_n_derive_shape, 'red'),
         (case_R1_t_derive_tail_invalid, 'red'),
         (case_R1_u_derive_prefix_nan, 'red'),
         (case_R3_h1_coverage_nan, 'red'),
         (case_R3_h2_survey_topology, 'red'),
         (case_R3_b_commit_guard_missing, 'red'),
         (case_R1_ef_recovery_missing, 'red'),
         (case_R2_f_first_abnormal_wins, 'red'),
         (case_R3_c_seed_grace, 'red'),
         (case_R3_d_tp_cancel_network, 'red'),
         (case_R3_e_tp_2011_fact_matrix, 'red'),
         (case_R3_f_tp_cancel_success_but_filled, 'red'),
         (case_R3_g_coverage_conflict, 'red'),
         (case_R1_h_monitor_hole, 'red'),
         (case_R2_g_precreate_rollback_failed, 'red')]


def main():
    print('=' * 76)
    print('v6.2 RED-first（第一批 16 + 第二批 7，r5 规格 x 旧实现）——red 预期全部失败')
    print('授权：ChatGPT r5 复审 Gate ✅ RED-first / ❌ production landing')
    print('=' * 76)
    red, unexpected_green, error, same = [], [], [], []
    for case, expected in CASES:
        try:
            case()
            if expected == 'red':
                unexpected_green.append(case.__name__)
                print(f"  [!!! GREEN_AGAINST_OLD] {case.__name__}（用例不具备判别力，须修）")
            else:
                same.append(case.__name__)
                print(f"  [SAME ✓] {case.__name__}（旧/新一致的正向规格，GREEN 期判别器）")
        except AssertionError as e:
            if expected == 'red':
                red.append(case.__name__)
                print(f"  [RED ✓] {case.__name__}: {str(e)[:96]}")
            else:
                error.append((case.__name__, f'同向正向用例失败: {str(e)[:96]}'))
                print(f"  [ERROR ✗] {case.__name__}: 同向正向用例失败")
        except Exception as e:  # noqa: BLE001
            error.append((case.__name__, repr(e)[:140]))
            print(f"  [ERROR ✗] {case.__name__}: {repr(e)[:140]}")
    print('-' * 76)
    total_red = sum(1 for _, exp in CASES if exp == 'red')
    print(f"RED（预期失败，判别力成立）: {len(red)}/{total_red}")
    print(f"SAME（旧/新一致正向，GREEN 期判别器）: {len(same)}/{len(CASES) - total_red}")
    print(f"ERROR（基建/转录缺陷，须修复）  : {len(error)}")
    print(f"GREEN_AGAINST_OLD（规格不可判别）: {len(unexpected_green)}")
    for name, err in error:
        print(f"    ERROR {name}: {err}")
    ok = (len(error) == 0 and len(unexpected_green) == 0
          and len(red) == total_red
          and len(same) == len(CASES) - total_red)
    print('=' * 76)
    if ok:
        print("✅ RED-first 两批成立：23/23 例 RED（判别力全部证成；")
        print("   R3-e 的 PROCEED 正向行已并入事实矩阵用例，作为端点路由的 GREEN 期判别器）。")
        print("   下一步：GREEN 实现（v6.2 helper 覆盖 + AFTER 块）→ 同套件全绿")
        print("   → 133 回归 → 五道门 → mutation。")
        return 0
    print("❌ RED-first 未成立——先修复 ERROR/意外 GREEN 再继续。")
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
