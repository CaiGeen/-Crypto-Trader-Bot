# -*- coding: utf-8 -*-
"""双点验证：用同一根因模型同时预测两次真实 BadRequest 的 byte offset。

根因假设：legacy Markdown 下 batch_id 的 3 个下划线两两配对形成斜体实体，
第 3 个下划线开启的实体找不到闭合 → Telegram 报「实体起始处」的字节偏移。

- 2026-08-29 16:05:21 真实报错 offset=139（消息含 "94 秒前"）
- 2026-08-29 17:23:59 真实报错 offset=138（消息含 "2 秒前"，少 1 字节）

若模型正确，两次预测必须精确命中，且 138 处的字节必须是 '_'。
只读诊断，不触碰任何生产文件。
"""
import sys

sys.stdout.reconfigure(encoding='utf-8')

SIGNAL_DEDUP_WINDOW_SEC = 600
FORCE_APPROVAL_TTL_SEC = 300


def build_msg(short_id: str, dedup_info: str) -> str:
    """与 bot_runner L2138 生成的 dedup_info + safe_reply 消息体逐字对齐。"""
    return (
        f"\U0001F6E1 **重复信号已拦截**（D-005 幂等保护）\n\n"
        f"\U0001F9EC 指纹：`{short_id}`\n"
        f"\U0001F4CA {dedup_info}\n\n"
        f"\U0001F4A1 同参数信号 {SIGNAL_DEDUP_WINDOW_SEC // 60} 分钟内视为重复"
        f"（防快捷指令双击/信号重发导致重复开仓）。\n"
        f"如确需再次开仓：\n"
        f"1️⃣ 先核对交易所当前挂单与持仓（防上次执行部分成交）\n"
        f"2️⃣ 发送 `/force {short_id}` 放行\n"
        f"3️⃣ 在 {FORCE_APPROVAL_TTL_SEC // 60} 分钟内重发原信号"
    )


def nth_underscore_offset(msg: str, n: int) -> int:
    """返回第 n 个 '_' 的 UTF-8 字节偏移（n 从 1 起）。"""
    cnt = 0
    off = 0
    for ch in msg:
        if ch == '_':
            cnt += 1
            if cnt == n:
                return off
        off += len(ch.encode('utf-8'))
    return -1


CASES = [
    # (标签, 指纹短码, dedup_info, 真实报错 offset, 下划线序号)
    ("16:05:21 旧", "244ce82b",
     "上次执行 94 秒前（batch: batch_20260829_160337_79d97e，"
     "状态 SUCCESS），拦截窗口剩 505 秒",
     139, 3),
    ("17:23:59 新", "ce6983f0",
     "上次执行 2 秒前（batch: batch_20260829_172353_dd6431，"
     "状态 SUCCESS），拦截窗口剩 597 秒",
     138, 3),
]

print("=" * 74)
print("D-005 Markdown BadRequest 双点定量验证")
print("=" * 74)

all_ok = True
results = []
for tag, sid, info, truth, nth in CASES:
    msg = build_msg(sid, info)
    b = msg.encode('utf-8')
    pred = nth_underscore_offset(msg, nth)
    ok = (pred == truth)
    all_ok &= ok
    hit_byte = b[pred:pred + 1].decode('utf-8', 'replace') if pred >= 0 else '?'
    results.append((tag, pred, truth, hit_byte, len(b), ok))

    print(f"\n【{tag}】")
    print(f"  消息总字节数        : {len(b)}")
    print(f"  第 {nth} 个 '_' 字节偏移 : {pred}")
    print(f"  Telegram 真实报错   : {truth}")
    print(f"  该偏移处的字节      : {hit_byte!r}")
    print(f"  判定                : {'✅ 精确命中' if ok else '❌ 不命中'}")

    ctx = msg[max(0, msg.rfind('\n', 0, msg.index('_'))):]
    print(f"  下划线上下文        : ...{repr(msg[pred - 30:pred + 12])}...")

print("\n" + "=" * 74)
print("【差值归因】两次 offset 相差 1 字节，来源：")
print("  旧: 上次执行 94 秒前  → '94' 2 字节")
print("  新: 上次执行 2 秒前   → '2'  1 字节")
print("  → 139 - 138 = 1 ✅ 与观测完全一致")
print("=" * 74)

# 汇总表
print("\n【汇总】")
print(f"  {'标签':<14}{'预测':>6}{'实测':>6}{'字节':>8}{'总长':>8}    判定")
for tag, pred, truth, hb, total, ok in results:
    print(f"  {tag:<14}{pred:>6}{truth:>6}{hb:>8}{total:>8}    {'✅' if ok else '❌'}")

print(f"\n结论：根因模型 {'✅ 双点锁定（非巧合）' if all_ok else '❌ 需重新假设'}")
sys.exit(0 if all_ok else 1)
