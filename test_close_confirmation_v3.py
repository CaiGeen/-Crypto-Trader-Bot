"""
平仓确认判据测试 v3 —— 测【订单维度】判据，并保留 v2 delta 判据做负向对照。

背景（交叉审查 B-01，2026-08-30）：
    v2 的 delta 判据（敞口减少量 ≥ 被平数量）**无法归因**，因为
    _read_position_amt 读的是 symbol+方向的**总敞口**。另一批次 SL 成交 /
    用户手动平仓 / ADL 都会让总敞口下降 → delta 达标 → 把别人的成交当成
    自己的证据 → 撤 SL/TP → **裸仓**。

    本测试用 S3 钉死这一点：本单 filled=0（根本没成交），但总敞口从
    0.002 掉到 0.001（他方平的）。
      · v3 按单确认  → not_filled   ✅ 拦住
      · v2 delta     → True（误判）  ❌ 放行 → 裸仓
    负向对照段会真的把 v2 跑一遍，证明这不是纸面推演。

被测实现：G:/tmp/new_helpers_v3.py（提议代码，**非生产代码**）
负向对照：G:/tmp/new_helpers_after.py（v2，被证伪的 delta 判据）

离线：零网络 / 零 API / 零写盘。
⚠️ 本文件为离线验证工具，非生产代码；工作树出现对它的修改不构成生产变更。
"""
import ast
import pathlib
import sys

sys.path.insert(0, r'G:\my-crypto-bot\.venv\Lib\site-packages')
import ccxt  # noqa: E402

DEFAULT_IMPL = pathlib.Path(r'G:\tmp\new_helpers_v3.py')
V2_IMPL = pathlib.Path(r'G:\tmp\new_helpers_after.py')   # 负向对照
SYM = 'BTC/USDT:USDT'


class _FakeTime:
    """把 time.sleep 变成 no-op，否则 not_found 重试会真睡 2s×3。"""
    @staticmethod
    def sleep(_s):
        return None


def _pos(side, amt):
    return [{'symbol': SYM, 'side': side, 'contracts': amt, 'info': {}}]


def _order(status, filled, oid='OID1'):
    return {'id': oid, 'status': status, 'filled': filled, 'amount': 0.001}


LONG_001 = _pos('long', 0.001)
LONG_0005 = _pos('long', 0.0005)
LONG_002 = _pos('long', 0.002)
EMPTY = []
API_ERR = ccxt.NetworkError('simulated network error')
NOT_FOUND = ccxt.OrderNotFound('simulated not found')


class FakeExchange:
    """两个序列用尽后**重复最后一个值**（不抛异常），否则失败原因会被误报成
    「读取失败」，掩盖真实机制（2026-08-29 教训）。

    ⚠️ fetch_positions **原样返回 None**（v2 版本的 `or []` 会把 None 静默转
    成"无敞口" → 判「已平仓」→ 裸仓。交叉审查 C 高危项，S8 专门测这个）。
    """

    def __init__(self, pos_seq=None, order_seq=None):
        self.pos_seq = list(pos_seq or [])
        self.order_seq = list(order_seq or [])
        self.last_pos = None
        self.last_order = None
        self.n_pos_calls = 0
        self.n_order_calls = 0

    def fetch_positions(self, symbols=None):
        self.n_pos_calls += 1
        if self.pos_seq:
            self.last_pos = self.pos_seq.pop(0)
        v = self.last_pos
        if isinstance(v, Exception):
            raise v
        return v

    def fetch_order(self, order_id, symbol, **kw):
        self.n_order_calls += 1
        if self.order_seq:
            self.last_order = self.order_seq.pop(0)
        v = self.last_order
        if isinstance(v, Exception):
            raise v
        return v


class FakeSelf:
    def __init__(self, pos_seq=None, order_seq=None):
        self.exchange = FakeExchange(pos_seq, order_seq)

    def _safe_api_call(self, fn, *a, **kw):
        kw.pop('retries', None)      # 生产签名有 retries，fake 直接忽略
        kw.pop('params', None)
        return fn(*a, **kw)


def load_impl(path, need):
    src = path.read_text(encoding='utf-8')
    tree = ast.parse(src)
    ns = {'time': _FakeTime, 'ccxt': ccxt}
    got = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name in need:
            exec(ast.get_source_segment(src, n), ns)
            got[n.name] = ns[n.name]
    missing = [f for f in need if f not in got]
    if missing:
        print("=" * 74)
        print(f"❌ RED：目标文件缺少 {missing} —— 改动尚未落地。")
        sys.exit(1)
    return got


def _bind(sf, impl, name):
    def _f(*a, **k):
        return impl[name](sf, *a, **k)
    return _f


def run_v3(impl, pos_before_data, order_seq, side='BUY', expected=0.001,
           attempts=3, extra_pos_seq=None):
    """返回 (verdict, detail, pos_before)。"""
    sf = FakeSelf([pos_before_data] + list(extra_pos_seq or []), order_seq)
    sf._read_position_amt = _bind(sf, impl, '_read_position_amt')
    sf._fetch_close_order_state = _bind(sf, impl, '_fetch_close_order_state')
    before = impl['_read_position_amt'](sf, SYM, side, True)
    verdict, detail = impl['_confirm_close_filled'](
        sf, SYM, side, True, 'OID1', expected, before, attempts, 0.0)
    return verdict, detail, before


def main():
    print(f"被测实现（v3 订单维度）: {DEFAULT_IMPL}")
    print(f"负向对照（v2 delta）  : {V2_IMPL}")
    impl = load_impl(DEFAULT_IMPL, ['_read_position_amt',
                                    '_fetch_close_order_state',
                                    '_confirm_close_filled',
                                    '_close_amount_guard'])
    print()

    cases = []

    # S1 单批次全平：订单 closed 且 filled 达标（平仓后敞口归零）
    v, d, _ = run_v3(impl, LONG_001, [_order('closed', 0.001)],
                     extra_pos_seq=[EMPTY])
    cases.append(('S1 单批次全平（filled=0.001）', v, 'confirmed', d))

    # S2 订单在场但未成交
    v, d, _ = run_v3(impl, LONG_001, [_order('open', 0.0)])
    cases.append(('S2 订单在场但未成交', v, 'not_filled', d))

    # S3 ★B-01 核心★：本单未成交，但总敞口从 0.002→0.001（他方平仓）
    #     delta 会把别人的成交当成自己的证据；按单确认必须拦住
    v, d, _ = run_v3(impl, LONG_002, [_order('open', 0.0)],
                     expected=0.001, extra_pos_seq=[LONG_001])
    cases.append(('S3 本单未成交但总敞口下降（他方减仓）', v, 'not_filled', d))

    # S4 查询异常 → unknown（绝不当成"没成交"也不当成"已成交"）
    v, d, _ = run_v3(impl, LONG_001, [API_ERR, API_ERR, API_ERR])
    cases.append(('S4 查询订单连续异常', v, 'unknown', d))

    # S5 订单确实不存在（OrderNotFound 重试后仍 not_found）
    v, d, _ = run_v3(impl, LONG_001, [NOT_FOUND] * 8)
    cases.append(('S5 订单不存在（重试后仍 not_found）', v, 'not_filled', d))

    # S6 可见性延迟：先 OrderNotFound，重试后成功（事件 3 实证场景）
    v, d, _ = run_v3(impl, LONG_001, [NOT_FOUND, _order('closed', 0.001)],
                     extra_pos_seq=[EMPTY])
    cases.append(('S6 create 后可见性延迟（重试救回）', v, 'confirmed', d))

    # S7 部分成交
    v, d, _ = run_v3(impl, LONG_002, [_order('closed', 0.0005)], expected=0.001)
    cases.append(('S7 仅部分成交（filled=0.0005 < 0.001）', v, 'not_filled', d))

    # S8 ★C 高危★：fetch_positions 返回 None（**非异常**）→ 必须返回 None 而非 0.0
    sf = FakeSelf([None], [])
    sf._read_position_amt = _bind(sf, impl, '_read_position_amt')
    got = impl['_read_position_amt'](sf, SYM, 'BUY', True)
    cases.append(('S8 fetch_positions 返回 None（非异常）', got, None,
                  f'_read_position_amt → {got!r}（必须 None，v2 会返回 0.0 → 假确认）'))

    # S9 ★B-03★：台账 0.001 但实测敞口只剩 0.0005 → expected 取 min
    v, d, _ = run_v3(impl, LONG_0005, [_order('closed', 0.0005)],
                     expected=0.001, extra_pos_seq=[EMPTY])
    cases.append(('S9 台账 0.001 > 实测 0.0005（filling=0.0005）', v, 'confirmed', d))

    # S10 _close_amount_guard：台账量大于实测 → 按下调后的数量平
    sf = FakeSelf([LONG_0005], [])
    sf._read_position_amt = _bind(sf, impl, '_read_position_amt')
    amt, detail = impl['_close_amount_guard'](sf, SYM, 'BUY', True, 0.001)
    cases.append(('S10 数量兜底：台账 0.001 / 实测 0.0005', amt, 0.0005, detail))

    # S11 _close_amount_guard：读取失败 → None（Fail-Closed 不发单）
    sf = FakeSelf([API_ERR], [])
    sf._read_position_amt = _bind(sf, impl, '_read_position_amt')
    amt, detail = impl['_close_amount_guard'](sf, SYM, 'BUY', True, 0.001)
    cases.append(('S11 数量兜底：读取失败 → 不发单', amt, None, detail))

    allok = True
    for name, got, want, detail in cases:
        good = (got == want)
        allok &= good
        mark = '✅' if good else '❌'
        print(f"{mark} {name}")
        print(f"     got={got!r} want={want!r}  | {detail}")

    # ── 负向对照：用 v2 的 delta 判据跑 S3，证明它确实误判 ──
    print()
    print("=" * 74)
    print("负向对照：v2 delta 判据在同一场景（S3）下的表现")
    print("=" * 74)
    v2 = load_impl(V2_IMPL, ['_read_position_amt', '_confirm_position_reduced'])
    sf = FakeSelf([LONG_002, LONG_001], [])
    sf._read_position_amt = _bind(sf, v2, '_read_position_amt')
    before = v2['_read_position_amt'](sf, SYM, 'BUY', True)
    ok2, detail2 = v2['_confirm_position_reduced'](
        sf, SYM, 'BUY', True, before, 0.001, 3, 0.0)
    print(f"  v2 delta  → confirmed={ok2}   ({detail2})")
    print(f"  v3 按单   → not_filled（本单 filled=0，根本没成交）")
    if ok2 is True:
        print("  ✅ 负向对照成立：v2 在他方减仓时给出【假确认】→ 会撤 SL/TP → 裸仓。")
        print("     这证明 B-01 不是纸面推演，S3 场景的判据更换是必须的。")
        neg_ok = True
    else:
        print("  ❌ 负向对照失效：v2 未复现假确认，B-01 的实证基础不成立，需重新评估。")
        neg_ok = False

    print()
    print("=" * 74)
    if allok and neg_ok:
        print(f"✅ v3 订单维度判据全部通过（{len(cases)}/{len(cases)}）+ 负向对照成立")
        return 0
    if not allok:
        print("❌ 存在不符预期场景（上方 ❌ 项）")
    if not neg_ok:
        print("❌ 负向对照未成立")
    return 1


if __name__ == '__main__':
    sys.exit(main())
