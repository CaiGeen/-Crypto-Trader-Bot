"""
市价平仓「仓位归零确认」语义测试（ChatGPT 2026-08-29 终审「必须修 1」）。

命题：
    create_order() 成功 ≠ 仓位已平。
    撤 SL/TP 之前必须有交易所侧事实确认。

本测试用【探针抓到的真实 ccxt 数据形态】离线驱动被测实现：
    G:/tmp/probe_position_shape.py 实测确认
      · 账户 dualSidePosition=True（双向持仓）
      · ccxt 归一化后 side='long' / info.positionSide='LONG'
      · ccxt 过滤零仓位行 → 无仓位时返回 []（不是返回 contracts=0 的行）

方法（沿用项目纪律）：
    ast 从目标文件原样提取 _read_position_amt / _confirm_position_reduced，
    注入 FakeExchange（回放真实数据形态）执行 —— 零网络、零 API、零写盘。

用法：
    python test_position_close_confirmation.py [impl.py]
    默认 impl = G:/tmp/new_helpers_after.py（提议实现）
    负向对照：传 G:/tmp/new_helpers_naive.py → 必须 rc=1
    改动落地后：传 trader_260725.py → 必须 rc=0

⚠️ 本文件为离线验证工具，非生产代码；工作树出现对它的修改不构成生产变更。
"""
import ast
import pathlib
import sys
import time

HERE = pathlib.Path(__file__).parent
DEFAULT_IMPL = pathlib.Path(r'G:\tmp\new_helpers_after.py')
SYM = 'BTCUSDT'          # 生产 trade_state.json 的键形态（探针已验证两种形态都可用）
NEED = ('_read_position_amt', '_confirm_position_reduced')


# ── 真实数据形态（照抄探针实测输出）─────────────────────────────────
def P(side: str, amt: float) -> dict:
    return {'symbol': 'BTC/USDT:USDT', 'side': side, 'contracts': amt,
            'info': {'symbol': 'BTCUSDT', 'positionSide': side.upper(),
                     'positionAmt': f'{amt:.3f}'}}


LONG_001 = [P('long', 0.001)]
LONG_002 = [P('long', 0.002)]
LONG_0015 = [P('long', 0.0015)]
EMPTY = []          # ccxt 过滤零仓位行后返回 []
API_ERR = RuntimeError('timeout')


class FakeExchange:
    """回放序列；用尽后**重复最后一个值**（不是抛异常）。

    为什么要重复而不是报错：被测实现最多轮询 3 次，若第 2/3 次读到
    "sequence exhausted"，失败原因会被误报成「读取失败」，掩盖真实机制
    —— 尤其 S5（方向传错）必须靠 delta=0 判不通过，而不是靠 API 错误。
    """

    def __init__(self, seq):
        self.seq = list(seq)
        self.last = None
        self.calls = 0

    def fetch_positions(self, symbols=None):
        self.calls += 1
        if self.seq:
            self.last = self.seq.pop(0)
        v = self.last
        if isinstance(v, Exception):
            raise v
        return v if v is not None else []


class FakeSelf:
    def __init__(self, seq):
        self.exchange = FakeExchange(seq)

    def _safe_api_call(self, fn, *a, **kw):
        return fn(*a, **kw)


def load_impl(path: pathlib.Path):
    src = path.read_text(encoding='utf-8')
    tree = ast.parse(src)
    ns = {'time': time}
    got = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name in NEED:
            exec(ast.get_source_segment(src, n), ns)
            got[n.name] = ns[n.name]
    missing = [f for f in NEED if f not in got]
    if missing:
        print("=" * 74)
        print(f"❌ RED：目标文件缺少 {missing} —— 改动尚未落地。")
        print("   这是预期的失败（落地后必须转 rc=0），不是缺陷。")
        sys.exit(1)
    return got


def run(impl, before_data, after_seq, side='BUY', expected=0.001,
        attempts=3, delay=0.0):
    sf = FakeSelf([before_data] + list(after_seq))
    # 被测函数内部通过 self._read_position_amt 调用 —— 必须绑到 fake self 上
    def _bind(name):
        def _f(*a, **k):
            return impl[name](sf, *a, **k)
        return _f

    sf._read_position_amt = _bind('_read_position_amt')
    before = impl['_read_position_amt'](sf, SYM, side, True)
    ok, detail = impl['_confirm_position_reduced'](
        sf, SYM, side, True, before, expected, attempts, delay)
    return ok, detail, before


def main():
    target = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_IMPL
    print(f"被测实现: {target}")
    impl = load_impl(target)
    print()

    cases = []

    # S1 单批次全平：0.001 → 0
    ok, detail, _ = run(impl, LONG_001, [EMPTY])
    cases.append(('S1 单批次全平 0.001→0', ok, True, detail))

    # S2 平仓未成交：0.001 → 0.001
    ok, detail, _ = run(impl, LONG_001, [LONG_001])
    cases.append(('S2 平仓未成交（仓位未变）', ok, False, detail))

    # S3 多批次同方向（关键）：0.002 → 0.001，本批只平 0.001
    #    「仓位必须归零」的朴素判据在这里会 100% 误判
    ok, detail, _ = run(impl, LONG_002, [LONG_001], expected=0.001)
    cases.append(('S3 多批次同向 0.002→0.001（平 0.001）', ok, True, detail))

    # S4 读取失败 → Fail-Closed
    ok, detail, _ = run(impl, LONG_001, [API_ERR, API_ERR, API_ERR])
    cases.append(('S4 平仓后读取连续失败', ok, False, detail))

    # S5 方向传错（陷阱）：持仓是 LONG，却按 SELL 读 → 读数恒为 0
    #    朴素判据会在这里给出【假确认】→ 持仓在场却撤 SL/TP = 裸仓
    ok, detail, _ = run(impl, LONG_001, [LONG_001], side='SELL')
    cases.append(('S5 方向传错（陷阱，必须判不通过）', ok, False, detail))

    # S6 传播延迟：第一次仍读到旧值，第二次归零 → 轮询应救回来
    ok, detail, _ = run(impl, LONG_001, [LONG_001, EMPTY], attempts=3, delay=0.0)
    cases.append(('S6 亚秒级传播延迟（轮询救回）', ok, True, detail))

    # S7 部分成交：0.002 → 0.0015，只减了 0.0005 < 0.001
    ok, detail, _ = run(impl, LONG_002, [LONG_0015], expected=0.001)
    cases.append(('S7 仅部分成交（减 0.0005 < 0.001）', ok, False, detail))

    allok = True
    for name, got, want, detail in cases:
        good = (got == want)
        allok &= good
        mark = '✅' if good else '❌'
        print(f"{mark} {name}")
        print(f"     got={got} want={want}  | {detail}")

    print()
    print("=" * 74)
    if allok:
        print(f"✅ 仓位归零确认语义全部通过（{len(cases)}/{len(cases)}）")
        return 0
    print("❌ 存在不符预期场景（上方 ❌ 项）")
    return 1


if __name__ == '__main__':
    sys.exit(main())
