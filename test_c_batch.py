# -*- coding: utf-8 -*-
"""
P0 平仓竞态修复 Batch C 专项测试（防回退：字段级 merge + 墓碑，2026-08-29）

依据：P0平仓竞态_修复规格_v3_终审ChatGPT.md §5/§6 + v2 §3/§5 + ChatGPT Batch C 批复
四条不变量：
  C-I   墓碑不可复活（PROGRAMMATIC_CANCED 后陈旧快照不得恢复可建单状态）
  C-II  close_phase 单向前进 0→1→2，禁止回退
  C-III 字段 merge 不能覆盖更新字段（旧快照只能补充允许字段）
  C-IV  终态 registry 与保护单创建状态一致（终态后维护路径不产生 create 意图）

真实文件 I/O：STATE_FILE / 墓碑文件均重定向临时目录（模块级直赋，finally 恢复；
惯例同 test_b2_restart_semantics）。⚠️ 实盘灰度进程在跑，绝不触碰真实
trade_state.json / trade_tombstones.json。

TC20 为回归锁（Batch A 已实现语义，RED 阶段即绿）；其余 TC 在 Batch C 实施前
应为 RED（FAIL）。
运行：.venv/Scripts/python.exe test_c_batch.py
"""
import os
import json
import tempfile
import threading
import time
from unittest import mock

import trader_260725
from trader_260725 import CryptoTrader

SYMBOL = "BTC/USDT:USDT"
RESULTS = []

IDENT_SL = "batch_c|SL|L0|LONG"
IDENT_TP = "batch_c|TP|L0|LONG"

_ORIG_STATE_FILE = trader_260725.STATE_FILE


def report(name, passed, detail=""):
    RESULTS.append((name, passed))
    print(f"\n{'=' * 60}\n[{'PASS' if passed else 'FAIL'}] {name} {detail}\n{'=' * 60}")


def _make_fake(state_file, tomb_file):
    fake = mock.MagicMock()
    fake._state_lock = threading.Lock()   # 生产同款非重入锁
    fake._api_cooldown_until = 0          # ⚠️ MagicMock 数值比较必炸：绑定真实数值
    fake._tp_breaker_alerted = None
    fake._tombstone_alerted = set()
    fake.tombstone_file = tomb_file
    fake.sent = []
    fake.send_tg_notification = lambda text, **kw: fake.sent.append(
        (kw.get('level', 'info'), str(text)))
    # 真实实现绑定（hasattr 保护：Batch C 未实施时缺失 helper → 走 MagicMock → RED）
    # P0 Batch B（2026-08-29）：clear_batch_state 内部依赖的新 helper 必须显式绑定
    # （MagicMock 坑第 8 次实证：_verify_clear_proof 未绑定 → 返回 MagicMock 恒非 None
    #  → proof 恒被拒且 _converge_alert 无副作用静默丢告警）
    for _n in ('save_batch_state', 'clear_batch_state', 'load_all_states',
               '_persist_states', '_load_tombstones', '_persist_tombstones',
               '_prune_tombstones', '_merge_batch_state',
               '_collect_batch_order_ids', '_assert_create_allowed',
               '_verify_clear_proof', '_converge_alert',
               '_batch_has_active_exposure', '_get_amount_precision',
               '_converge_cancel_order', '_converge_batch_orders_before_clear'):
        if hasattr(CryptoTrader, _n):
            setattr(fake, _n,
                    (lambda _n=_n: lambda *a, **k: getattr(CryptoTrader, _n)(fake, *a, **k))())
    # ⚠️ 不绑真实 dict → getattr MagicMock 非 dict → 告警静默丢失
    fake._converge_alert_counts = {}
    return fake


def _proof_for(batch_id, symbol=SYMBOL, scope='FULL'):
    """P0 Batch B（2026-08-29）适配：clear_batch_state 现为 proof 门（Fail-Closed），
    直调清理须提交最小合法 proof。本测试文件关注 Batch C 墓碑语义，交易所侧
    收敛由 test_b_batch.py 专项覆盖，此处 proof 仅构造门校验所需六键。"""
    return {
        'batch_id': batch_id, 'symbol': symbol, 'checked_at': time.time(),
        'scope': scope, 'position_zero': True,
        'state_ids_resolved': [], 'exchange_scan': 'zero',
        'l1_canceled': [], 'l2_canceled': [], 'l3_orphans': [],
    }


def _batch(**over):
    b = {
        'is_active': True, 'batch_id': 'batch_c', 'symbol': SYMBOL, 'side': 'BUY',
        'entry_orders': ['e1'], 'stop_steps': [55000.0], 'take_profit_price': 60000.0,
        'current_sl_id': 'sl1', 'tp_order_id': 'tp1', 'close_phase': 0,
        'batch_total_amount': 0.003, 'target_amounts': [0.003],
        'last_filled_count': 1, 'filled_details': [85000.0], 'total_entry_fee': 0.5,
        'user_modified': False, 'pending_close': False,
        'is_programmatic_cancel': False, 'settled_by_limit_close': False,
        'protection_registry': {},
    }
    b.update(over)
    return b


def _reg(state='CONFIRMED', order_id='sl1', updated_at=None, **over):
    e = {'state': state, 'order_id': order_id, 'id_known': True,
         'order_kind': 'conditional', 'role': 'SL', 'layer': 0, 'side': 'LONG',
         'intent': None, 'updated_at': updated_at if updated_at is not None else time.time()}
    e.update(over)
    return e


class _Env:
    """STATE_FILE / 墓碑文件重定向 + 恢复"""

    def __enter__(self):
        self.dir = tempfile.mkdtemp(prefix='p0c_')
        self.state_file = os.path.join(self.dir, 'trade_state.json')
        self.tomb_file = os.path.join(self.dir, 'trade_tombstones.json')
        trader_260725.STATE_FILE = self.state_file
        return self

    def __exit__(self, *a):
        trader_260725.STATE_FILE = _ORIG_STATE_FILE
        return False

    def load_state(self):
        if os.path.exists(self.state_file):
            with open(self.state_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        return {}

    def load_tomb(self):
        if os.path.exists(self.tomb_file):
            with open(self.tomb_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        return {}

    def write_state(self, data):
        with open(self.state_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False)

    def write_tomb(self, data):
        with open(self.tomb_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False)


# =====================================================================
# C-I 墓碑不可复活（TC1-TC6, TC23）
# =====================================================================
def t_tombstone():
    with _Env() as env:
        fake = _make_fake(env.state_file, env.tomb_file)
        # TC1: clear → 墓碑落盘（含 symbol/side/cleared_at/converged/known ids）
        env.write_state({SYMBOL: {'batch_c': _batch(
            protection_registry={IDENT_SL: _reg(state='PROGRAMMATIC_CANCELED', order_id='sl1')})}})
        fake.clear_batch_state(SYMBOL, 'batch_c', proof=_proof_for('batch_c'))
        tomb = env.load_tomb().get('batch_c')
        report("TC1/clear写墓碑(含converged/known_order_ids)",
               isinstance(tomb, dict) and tomb.get('symbol') == SYMBOL
               and tomb.get('side') == 'BUY' and float(tomb.get('cleared_at', 0) or 0) > 0
               and 'sl1' in (tomb.get('converged_order_ids') or [])
               and 'sl1' in (tomb.get('known_order_ids') or [])
               and 'tp1' in (tomb.get('known_order_ids') or []),
               f"(tomb={tomb!r})")

        # TC2: 复活拦截——clear 后旧快照 save → 拒绝 + critical 恰一次
        fake.sent.clear()
        fake.save_batch_state(SYMBOL, 'batch_c', _batch(close_phase=0))
        st = env.load_state().get(SYMBOL, {})
        crit = [s for s in fake.sent if s[0] == 'critical']
        ok2 = ('batch_c' not in st) and (len(crit) == 1) and ('复活' in crit[0][1])
        report("TC2/墓碑拦截复活(critical恰一次)", ok2,
               f"(state_has={'batch_c' in st}, crit={len(crit)})")

        # TC3: 告警去重（同批次进程内一次）
        fake.save_batch_state(SYMBOL, 'batch_c', _batch(close_phase=0))
        crit2 = [s for s in fake.sent if s[0] == 'critical']
        report("TC3/墓碑告警去重(进程内一次)", len(crit2) == 1,
               f"(crit_total={len(crit2)})")

    with _Env() as env:
        fake = _make_fake(env.state_file, env.tomb_file)
        # TC4: TTL 过期墓碑 → save 放行
        env.write_state({SYMBOL: {'batch_c': _batch()}})
        fake.clear_batch_state(SYMBOL, 'batch_c', proof=_proof_for('batch_c'))
        tomb = env.load_tomb()
        if 'batch_c' in tomb:  # RED 阶段 clear 未写墓碑 → 直接 FAIL 容错
            tomb['batch_c']['cleared_at'] = time.time() - 8 * 24 * 3600  # 8 天前 > TTL 7 天
            env.write_tomb(tomb)
        fake.save_batch_state(SYMBOL, 'batch_c', _batch(close_phase=0))
        st = env.load_state().get(SYMBOL, {})
        report("TC4/TTL过期墓碑放行save", 'batch_c' in st and 'batch_c' in tomb,
               f"(state_has={'batch_c' in st}, tomb_has={'batch_c' in tomb})")

    with _Env() as env:
        fake = _make_fake(env.state_file, env.tomb_file)
        # TC5: clear 幂等——重复 clear 不覆盖原墓碑 cleared_at
        env.write_state({SYMBOL: {'batch_c': _batch()}})
        fake.clear_batch_state(SYMBOL, 'batch_c', proof=_proof_for('batch_c'))
        t1 = env.load_tomb().get('batch_c', {}).get('cleared_at')
        time.sleep(0.02)
        fake.clear_batch_state(SYMBOL, 'batch_c', proof=_proof_for('batch_c'))
        t2 = env.load_tomb().get('batch_c', {}).get('cleared_at')
        report("TC5/clear幂等不覆盖墓碑", t1 is not None and t2 == t1, f"(t1={t1}, t2={t2})")

        # TC6: 墓碑文件损坏 → DEGRADED 分治（D-009 Q3，ChatGPT R3 批准）
        #   ⚠️ 旧语义已作废：Batch C 原为"损坏视同空 → save 放行不阻断主流程"。
        #     D-009 判定该语义危险：它把"不知道墓碑里有什么"解释成"确定没有墓碑"，
        #     为已清理批次的复活留下通道。新规格按"存在性由谁证明"分治：
        #       全新 batch_id → 需排除"是已清理批次复活"，墓碑损坏无法排除 → 拒绝
        #       已存在 batch_id → 存在性由 trade_state 自己证明，墓碑非必要条件 → 放行
        #   拒绝必须是优雅拒绝（不抛异常）+ critical 告警，绝不静默丢请求。
        with open(env.tomb_file, 'w', encoding='utf-8') as f:
            f.write('{broken json')
        fake.sent.clear()
        try:
            fake.save_batch_state(SYMBOL, 'other_batch', _batch(batch_id='other_batch'))
            new_written = 'other_batch' in env.load_state().get(SYMBOL, {})
            no_exc = True
        except Exception:
            new_written, no_exc = True, False
        crit6 = [s for s in fake.sent if s[0] == 'critical']
        report("TC6/墓碑损坏→全新批次拒绝(优雅拒绝+critical)",
               (not new_written) and no_exc and len(crit6) >= 1,
               f"(written={new_written}, 无异常={no_exc}, critical={len(crit6)})")

        # TC6b: 墓碑损坏 → 已存在批次仍放行（存在性由 trade_state 证明）
        env.write_state({SYMBOL: {'keep_batch': _batch(batch_id='keep_batch')}})
        try:
            fake.save_batch_state(SYMBOL, 'keep_batch',
                                  _batch(batch_id='keep_batch', close_phase=1))
            kept = 'keep_batch' in env.load_state().get(SYMBOL, {})
        except Exception:
            kept = False
        report("TC6b/墓碑损坏→已存在批次放行", kept, f"(kept={kept})")

    with _Env() as env:
        fake = _make_fake(env.state_file, env.tomb_file)
        # TC23: 墓碑只拦本批次——另一批次 save 正常
        env.write_state({SYMBOL: {'batch_c': _batch()}})
        fake.clear_batch_state(SYMBOL, 'batch_c', proof=_proof_for('batch_c'))
        fake.save_batch_state(SYMBOL, 'batch_d', _batch(batch_id='batch_d'))
        st = env.load_state().get(SYMBOL, {})
        report("TC23/墓碑只拦本批次", 'batch_d' in st and 'batch_c' not in st,
               f"(keys={list(st.keys())})")


# =====================================================================
# C-II close_phase 单向（TC7-TC8）
# =====================================================================
def t_close_phase_ratchet():
    with _Env() as env:
        fake = _make_fake(env.state_file, env.tomb_file)
        # TC7: 磁盘 2（结算）vs 旧快照 0 → 2（陈旧监控快照不得降级）
        env.write_state({SYMBOL: {'batch_c': _batch(close_phase=2, settled_by_limit_close=True)}})
        fake.save_batch_state(SYMBOL, 'batch_c', _batch(close_phase=0))
        b = env.load_state()[SYMBOL]['batch_c']
        report("TC7/close_phase不回退(2 vs 0)", int(b.get('close_phase', 0) or 0) == 2,
               f"(phase={b.get('close_phase')})")

        # TC8: max 语义（磁盘 1 vs 快照 2 → 2）
        env.write_state({SYMBOL: {'batch_c': _batch(close_phase=1)}})
        fake.save_batch_state(SYMBOL, 'batch_c', _batch(close_phase=2))
        b = env.load_state()[SYMBOL]['batch_c']
        report("TC8/close_phase取max(1,2)", int(b.get('close_phase', 0) or 0) == 2,
               f"(phase={b.get('close_phase')})")


# =====================================================================
# C-III merge 不覆盖更新字段（TC9-TC19）
# =====================================================================
def t_field_merge():
    with _Env() as env:
        fake = _make_fake(env.state_file, env.tomb_file)
        # TC9/TC10: A 类棘轮（Boolean False→True 单向）
        env.write_state({SYMBOL: {'batch_c': _batch(
            pending_close=True, is_programmatic_cancel=True, settled_by_limit_close=True)}})
        fake.save_batch_state(SYMBOL, 'batch_c', _batch(
            pending_close=False, is_programmatic_cancel=False, settled_by_limit_close=False))
        b = env.load_state()[SYMBOL]['batch_c']
        report("TC9/A类棘轮pending_close/is_programmatic_cancel/settled",
               bool(b.get('pending_close')) and bool(b.get('is_programmatic_cancel'))
               and bool(b.get('settled_by_limit_close')),
               f"(pc={b.get('pending_close')}, ipc={b.get('is_programmatic_cancel')}, "
               f"settled={b.get('settled_by_limit_close')})")

        # TC11: B 类单调账本（结算线程已计的成交/费用不被旧快照抹掉）
        env.write_state({SYMBOL: {'batch_c': _batch(
            last_filled_count=3, filled_details=[85000.0, 86000.0, 87000.0],
            total_entry_fee=1.5)}})
        fake.save_batch_state(SYMBOL, 'batch_c', _batch(
            last_filled_count=1, filled_details=[85000.0, 0.0], total_entry_fee=0.5))
        b = env.load_state()[SYMBOL]['batch_c']
        report("TC11/B类单调账本(count/fee/逐层)",
               int(b.get('last_filled_count', 0)) == 3
               and float(b.get('total_entry_fee', 0)) == 1.5
               and list(b.get('filled_details', [])) == [85000.0, 86000.0, 87000.0],
               f"(cnt={b.get('last_filled_count')}, fee={b.get('total_entry_fee')}, "
               f"fd={b.get('filled_details')})")

        # TC12: C 类——磁盘 CONFIRMED 不被旧快照 PENDING_CREATE 降级
        env.write_state({SYMBOL: {'batch_c': _batch(
            protection_registry={IDENT_SL: _reg(state='CONFIRMED', order_id='sl1')})}})
        fake.save_batch_state(SYMBOL, 'batch_c', _batch(
            protection_registry={IDENT_SL: _reg(state='PENDING_CREATE', order_id=None, id_known=False)}))
        b = env.load_state()[SYMBOL]['batch_c']
        report("TC12/C类registry保护(CONFIRMED不被降级)",
               (b.get('protection_registry') or {}).get(IDENT_SL, {}).get('state') == 'CONFIRMED',
               f"(state={(b.get('protection_registry') or {}).get(IDENT_SL, {}).get('state')})")

        # TC13: C 类终态——磁盘 PROGRAMMATIC_CANCELED 不被旧快照复活为 CONFIRMED
        env.write_state({SYMBOL: {'batch_c': _batch(
            protection_registry={IDENT_SL: _reg(state='PROGRAMMATIC_CANCELED', order_id='sl1')})}})
        fake.save_batch_state(SYMBOL, 'batch_c', _batch(
            protection_registry={IDENT_SL: _reg(state='CONFIRMED', order_id='sl1')}))
        b = env.load_state()[SYMBOL]['batch_c']
        report("TC13/C类终态不可复活(PROGRAMMATIC_CANCELED)",
               (b.get('protection_registry') or {}).get(IDENT_SL, {}).get('state') == 'PROGRAMMATIC_CANCELED',
               f"(state={(b.get('protection_registry') or {}).get(IDENT_SL, {}).get('state')})")

        # TC14: C 类 FAILED/ABSENT → updated_at 新者胜（磁盘 FAILED 旧 vs 快照 CONFIRMED 新 → 快照胜）
        env.write_state({SYMBOL: {'batch_c': _batch(
            protection_registry={IDENT_SL: _reg(state='FAILED', order_id='sl1', updated_at=100.0)})}})
        fake.save_batch_state(SYMBOL, 'batch_c', _batch(
            protection_registry={IDENT_SL: _reg(state='CONFIRMED', order_id='sl1', updated_at=200.0)}))
        b = env.load_state()[SYMBOL]['batch_c']
        report("TC14/C类FAILED/ABSENT时间戳新者胜",
               (b.get('protection_registry') or {}).get(IDENT_SL, {}).get('state') == 'CONFIRMED',
               f"(state={(b.get('protection_registry') or {}).get(IDENT_SL, {}).get('state')})")

        # TC15: G 类 user_modified 取 OR（事实字段非安全判据）
        env.write_state({SYMBOL: {'batch_c': _batch(user_modified=True)}})
        fake.save_batch_state(SYMBOL, 'batch_c', _batch(user_modified=False))
        b1 = env.load_state()[SYMBOL]['batch_c'].get('user_modified')
        env.write_state({SYMBOL: {'batch_c': _batch(user_modified=False)}})
        fake.save_batch_state(SYMBOL, 'batch_c', _batch(user_modified=True))
        b2 = env.load_state()[SYMBOL]['batch_c'].get('user_modified')
        report("TC15/G类user_modified取OR", bool(b1) and bool(b2),
               f"(disk_T→{b1}, snap_T→{b2})")

        # TC16: D 类——磁盘 id + registry 活单 + close_phase<2 → 快照 None 不抹掉
        env.write_state({SYMBOL: {'batch_c': _batch(
            tp_order_id='tp1', close_phase=0,
            protection_registry={IDENT_TP: _reg(state='CONFIRMED', order_id='tp1', role='TP')})}})
        fake.save_batch_state(SYMBOL, 'batch_c', _batch(
            tp_order_id=None, protection_registry={}))
        b = env.load_state()[SYMBOL]['batch_c']
        report("TC16/D类id镜像保留(活单未结算)",
               b.get('tp_order_id') == 'tp1', f"(tp={b.get('tp_order_id')})")

        # TC17: D 类——registry 终结态 → id 最新者胜（快照 None 胜）
        env.write_state({SYMBOL: {'batch_c': _batch(
            tp_order_id='tp1', close_phase=0,
            protection_registry={IDENT_TP: _reg(state='PROGRAMMATIC_CANCELED', order_id='tp1', role='TP')})}})
        fake.save_batch_state(SYMBOL, 'batch_c', _batch(
            tp_order_id=None, protection_registry={}))
        b = env.load_state()[SYMBOL]['batch_c']
        report("TC17/D类id镜像终结态放行清id",
               b.get('tp_order_id') is None, f"(tp={b.get('tp_order_id')})")

        # TC18: 磁盘独有字段补回（union：快照缺字段不丢）
        env.write_state({SYMBOL: {'batch_c': _batch(limit_close_order_id='lc1')}})
        snap = _batch()
        snap.pop('limit_close_order_id', None)
        fake.save_batch_state(SYMBOL, 'batch_c', snap)
        b = env.load_state()[SYMBOL]['batch_c']
        report("TC18/磁盘独有字段union补回", b.get('limit_close_order_id') == 'lc1',
               f"(lc={b.get('limit_close_order_id')})")

        # TC19: 快照新增字段正常写入
        env.write_state({SYMBOL: {'batch_c': _batch()}})
        fake.save_batch_state(SYMBOL, 'batch_c', _batch(new_runtime_field='x'))
        b = env.load_state()[SYMBOL]['batch_c']
        report("TC19/快照新增字段写入", b.get('new_runtime_field') == 'x')


# =====================================================================
# C-IV 终态与 create 一致 + G3b 联动（TC20-TC22）
# =====================================================================
def t_terminal_consistency():
    with _Env() as env:
        fake = _make_fake(env.state_file, env.tomb_file)
        # TC20（回归锁，Batch A 已实现）：registry 终态后闸门禁建（无 replace 豁免）
        env.write_state({SYMBOL: {'batch_c': _batch(
            protection_registry={IDENT_SL: _reg(state='PROGRAMMATIC_CANCELED', order_id='sl1')})}})
        allowed_no, reason_no = fake._assert_create_allowed(SYMBOL, 'batch_c', IDENT_SL)
        allowed_rp, reason_rp = fake._assert_create_allowed(
            SYMBOL, 'batch_c', IDENT_SL, replace_order_id='sl1')
        report("TC20/终态禁建无replace豁免(回归锁)",
               (not allowed_no) and (not allowed_rp),
               f"(plain={allowed_no}/{reason_no!r}, replace={allowed_rp}/{reason_rp!r})")

        # TC21: C-IV 综合链——G3b 已提交 CONFIRMED，旧快照（PENDING_VERIFY）save 后
        #       终态/已确认不回退 → 闸门仍拒建（不产生 create 意图）
        env.write_state({SYMBOL: {'batch_c': _batch(
            protection_registry={IDENT_SL: _reg(state='CONFIRMED', order_id='sl1')})}})
        fake.save_batch_state(SYMBOL, 'batch_c', _batch(
            protection_registry={IDENT_SL: _reg(state='PENDING_VERIFY', order_id='sl1')}))
        b = env.load_state()[SYMBOL]['batch_c']
        allowed, reason = fake._assert_create_allowed(SYMBOL, 'batch_c', IDENT_SL)
        report("TC21/G3b提交后旧快照不回退CONFIRMED",
               (b.get('protection_registry') or {}).get(IDENT_SL, {}).get('state') == 'CONFIRMED'
               and not allowed,
               f"(state={(b.get('protection_registry') or {}).get(IDENT_SL, {}).get('state')}, "
               f"allowed={allowed})")

        # TC22: 墓碑 TTL prune——过期移除、未过期保留
        now = time.time()
        env.write_tomb({
            'old_batch': {'symbol': SYMBOL, 'cleared_at': now - 8 * 24 * 3600},
            'new_batch': {'symbol': SYMBOL, 'cleared_at': now - 100},
        })
        fake._prune_tombstones()
        tomb = env.load_tomb()
        report("TC22/墓碑TTL prune",
               'old_batch' not in tomb and 'new_batch' in tomb,
               f"(keys={list(tomb.keys())})")


# =====================================================================
# 主入口
# =====================================================================
if __name__ == '__main__':
    print("=" * 60)
    print("P0 Batch C 专项测试（防回退：字段级 merge + 墓碑）")
    print("=" * 60)
    try:
        t_tombstone()
        t_close_phase_ratchet()
        t_field_merge()
        t_terminal_consistency()
    finally:
        trader_260725.STATE_FILE = _ORIG_STATE_FILE
    passed = sum(1 for _, p in RESULTS if p)
    total = len(RESULTS)
    print(f"\n{'#' * 60}")
    print(f"P0 Batch C 专项：{passed}/{total} 通过")
    print('#' * 60)
    import sys
    sys.exit(0 if passed == total else 1)
