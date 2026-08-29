# -*- coding: utf-8 -*-
"""
P0 通知可靠性专项测试（2026-08-29）

背景（真实事故）：
  D-005 重复信号拦截的 Telegram 通知，因消息中裸奔的批次号含**奇数个下划线**
  （batch_20260829_160337_79d97e → 下划线 3 个），被 Telegram legacy Markdown
  解析为未闭合实体，服务器整条拒收：
      BadRequest: Can't parse entities: can't find end of the entity
                  starting at byte offset 139
  而 bot_runner.safe_reply 在 BadRequest 分支只 logging.error，**不重发**
  → 通知永久丢失（违反不变量⑧ Fail-not-Silent）。
  同类消息走 trader.send_tg_notification 则因有降级兜底而送达，
  两条通道容错能力不一致，是本次修复的动机。

修复范围（ChatGPT 裁定 P0，只做这一项）：
  safe_reply 增加 BadRequest → 纯文本降级重发，与 send_tg_notification 对齐。
  不改变 D-005/D-006 任何判定逻辑，不改变批次设计。

禁止为了让本测试通过修改 bot_runner.py / trader_260725.py 的业务逻辑。

RED/GREEN 用法：
  直接运行                       → 测当前 bot_runner.py（GREEN 目标）
  TG_FALLBACK_TARGET=<旧版路径>  → 测指定文件（RED 验证用，不触碰生产文件）
"""
import asyncio
import importlib.util
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from telegram.error import BadRequest  # noqa: E402

# 被测模块：默认生产文件；RED 验证时由 TG_FALLBACK_TARGET 指向改前备份
_TARGET = os.environ.get('TG_FALLBACK_TARGET', '').strip()
if _TARGET:
    _spec = importlib.util.spec_from_file_location('bot_runner_under_test', _TARGET)
    bot_runner = importlib.util.module_from_spec(_spec)
    sys.modules['bot_runner_under_test'] = bot_runner
    _spec.loader.exec_module(bot_runner)
    print(f"🔴 RED 模式：被测文件 = {_TARGET}")
else:
    import bot_runner  # noqa: E402
    print(f"🟢 GREEN 模式：被测文件 = {os.path.abspath(bot_runner.__file__)}")

# 真实事故消息原文（16:05:21 被拒的那一条，逐字还原 bot_runner L2239-2247 模板）
D005_MSG = (
    "🛡 **重复信号已拦截**（D-005 幂等保护）\n\n"
    "🧬 指纹：`244ce82b`\n"
    "📊 上次执行 94 秒前（batch: batch_20260829_160337_79d97e，状态 SUCCESS），拦截窗口剩 505 秒\n\n"
    "💡 同参数信号 10 分钟内视为重复（防快捷指令双击/信号重发导致重复开仓）。\n"
    "如确需再次开仓：\n"
    "1️⃣ 先核对交易所当前挂单与持仓（防上次执行部分成交）\n"
    "2️⃣ 发送 `/force 244ce82b` 放行\n"
    "3️⃣ 在 5 分钟内重发原信号"
)

BATCH_ID = "batch_20260829_160337_79d97e"


class FakeMessage:
    """真实对象（不用 MagicMock —— 本项目已 9 次踩 MagicMock 未绑定属性恒 truthy 的坑）。
    记录每一次实际发送，按开关决定是否抛 BadRequest。"""

    def __init__(self, fail_md=True, fail_plain=False):
        self.sent = []
        self.fail_md = fail_md
        self.fail_plain = fail_plain

    async def reply_text(self, text, parse_mode=None, reply_markup=None):
        if parse_mode is not None and self.fail_md:
            raise BadRequest(
                "Can't parse entities: can't find end of the entity starting at byte offset 139")
        if parse_mode is None and self.fail_plain:
            raise BadRequest("Message is too long")
        self.sent.append({'text': text, 'parse_mode': parse_mode, 'reply_markup': reply_markup})
        return f"msg_{len(self.sent)}"


def run(coro):
    return asyncio.run(coro)


def main():
    passed = 0
    total = 0

    def check(name, cond, detail=""):
        nonlocal passed, total
        total += 1
        if cond:
            passed += 1
            print(f"  ✅ {name}")
        else:
            print(f"  ❌ {name}  {detail}")

    # ---- 场景1：Markdown 被拒 → 降级纯文本送达（核心） ----
    msg = FakeMessage(fail_md=True)
    update = SimpleNamespace(message=msg, callback_query=None)
    markup = object()
    run(bot_runner.safe_reply(update, D005_MSG, parse_mode='Markdown', reply_markup=markup))
    s1 = len(msg.sent) == 1
    check("场景1 降级后实际发出 1 条", s1, f"实际 {len(msg.sent)} 条")
    if s1:
        rec = msg.sent[0]
        check("场景1 降级消息 parse_mode=None（纯文本）", rec['parse_mode'] is None,
              f"实际 {rec['parse_mode']!r}")
        check("场景1 已剥离 ** 与反引号",
              '**' not in rec['text'] and '`' not in rec['text'])
        check("场景1 批次号下划线完整保留（不变量：不得把 _ 剥掉）",
              BATCH_ID in rec['text'],
              f"实际片段: {rec['text'][:120]}")
        check("场景1 回复按钮随降级消息一起保留", rec['reply_markup'] is markup)

    # ---- 场景2：降级也失败 → 不得抛出，只记日志 ----
    msg2 = FakeMessage(fail_md=True, fail_plain=True)
    update2 = SimpleNamespace(message=msg2, callback_query=None)
    threw = False
    try:
        run(bot_runner.safe_reply(update2, D005_MSG, parse_mode='Markdown'))
    except Exception as e:
        threw = True
        print(f"     （异常: {e}）")
    check("场景2 降级也失败时不向上抛出（不中断命令处理）", not threw)
    check("场景2 降级失败后无消息残留", len(msg2.sent) == 0)

    # ---- 场景3：parse_mode=None 时 BadRequest 不重发（防无限循环） ----
    msg3 = FakeMessage(fail_md=True, fail_plain=True)
    update3 = SimpleNamespace(message=msg3, callback_query=None)
    run(bot_runner.safe_reply(update3, "纯文本消息"))
    check("场景3 parse_mode=None 时不触发降级重发", len(msg3.sent) == 0,
          f"实际 {len(msg3.sent)} 条")

    # ---- 场景4：callback_query 路径同样降级 ----
    cq_msg = FakeMessage(fail_md=True)
    update4 = SimpleNamespace(message=None, callback_query=SimpleNamespace(message=cq_msg))
    run(bot_runner.safe_reply(update4, D005_MSG, parse_mode='Markdown'))
    check("场景4 callback_query 路径降级成功",
          len(cq_msg.sent) == 1 and cq_msg.sent[0]['parse_mode'] is None
          and BATCH_ID in cq_msg.sent[0]['text'])

    # ---- 场景5：正常成功路径不被降级逻辑干扰 ----
    msg5 = FakeMessage(fail_md=False)
    update5 = SimpleNamespace(message=msg5, callback_query=None)
    run(bot_runner.safe_reply(update5, D005_MSG, parse_mode='Markdown'))
    check("场景5 首次成功时只发 1 条", len(msg5.sent) == 1)
    if len(msg5.sent) == 1:
        check("场景5 首次成功时 parse_mode 原样保留",
              msg5.sent[0]['parse_mode'] == 'Markdown')
        check("场景5 首次成功时文本未被改写（** 与 ` 保留）",
              msg5.sent[0]['text'] == D005_MSG)

    # ---- 场景6：_strip_markdown 单元行为 ----
    _strip = getattr(bot_runner, '_strip_markdown', None)
    if _strip is None:
        check("场景6 _strip_markdown 已实现", False, "被测文件缺少 _strip_markdown（P0 未落地）")
    else:
        stripped = _strip("**粗体** 与 `code` 与 batch_2026_08_29_ab")
        check("场景6 _strip_markdown 保留下划线（批次号/文件名可读）",
              "batch_2026_08_29_ab" in stripped, f"实际: {stripped}")
        check("场景6 _strip_markdown 剥掉 ** 与反引号",
              '**' not in stripped and '`' not in stripped, f"实际: {stripped}")

    # ---- 场景7：源码锚点（防回归时被误删） ----
    src_path = getattr(bot_runner, '__file__', None) or 'bot_runner.py'
    src = open(src_path, encoding='utf-8').read()
    fn_src = src[src.index('async def safe_reply'):]
    fn_src = fn_src[:fn_src.index('\ndef get_standard_markup')]
    check("场景7 safe_reply 内存在降级重发分支",
          '_strip_markdown(text)' in fn_src and 'except BadRequest' in fn_src)
    check("场景7 降级仅在 parse_mode 非 None 时触发（防无限循环）",
          'if parse_mode is not None:' in fn_src)
    check("场景7 _strip_markdown 不剥下划线（源码层硬约束）",
          "replace('**', '')" in src and "replace('`', '')" in src
          and "replace('_', '')" not in src)

    print(f"\n{'=' * 60}\nP0 TG 通知降级: {passed}/{total}")
    return 0 if passed == total else 1


if __name__ == '__main__':
    sys.exit(main())
