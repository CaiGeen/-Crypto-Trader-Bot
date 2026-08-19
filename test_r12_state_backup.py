# -*- coding: utf-8 -*-
"""
C3/R12 状态文件滚动备份离线验收测试（真实文件操作于临时目录，不碰运行中 Bot 的状态）

契约（ChatGPT 锁定 A + ②）：
  1: 正常保存 -> .bak = 保存前的 last-known-good 旧状态
  2: 首次保存（STATE_FILE 不存在）-> 不创建 .bak，正常保存
  3: clear 也备份（误清解药）-> .bak 含被清批次
  4: 备份失败 -> 仅警告，主保存仍完成（不改变保存契约）
  5: save/clear 共用 _persist_states（AST 结构断言：两方法体内无直接持久化代码）

用法: .venv\\Scripts\\python.exe test_r12_state_backup.py
"""
import ast
import json
import os
import sys
import tempfile
import threading
from unittest import mock

import trader_260725
from trader_260725 import CryptoTrader

SYMBOL = "BTC/USDT:USDT"
RESULTS = []


def report(name, passed, detail=""):
    RESULTS.append((name, passed))
    print(f"\n{'=' * 60}\n[{'PASS' if passed else 'FAIL'}] {name} {detail}\n{'=' * 60}")


class PersistFake:
    """最小 fake：真实 _state_lock + 委托真实 load/_persist（STATE_FILE 已被 patch 到临时目录）"""

    def __init__(self):
        self._state_lock = threading.Lock()

    def load_all_states(self):
        return CryptoTrader.load_all_states(self)

    def _persist_states(self, all_states):
        return CryptoTrader._persist_states(self, all_states)


def read_json(path):
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def scenario_1():
    """1: 正常保存 -> .bak 为保存前旧状态"""
    with tempfile.TemporaryDirectory() as d:
        state = os.path.join(d, 'trade_state.json')
        with open(state, 'w', encoding='utf-8') as f:
            json.dump({SYMBOL: {'b_old': {'is_active': True}}}, f)
        with mock.patch.object(trader_260725, 'STATE_FILE', state):
            CryptoTrader.save_batch_state(PersistFake(), SYMBOL, 'b_new', {'is_active': True})
        ok = (os.path.exists(state + '.bak')
              and read_json(state + '.bak') == {SYMBOL: {'b_old': {'is_active': True}}}
              and set(read_json(state)[SYMBOL].keys()) == {'b_old', 'b_new'})
        report("场景1: 保存前旧状态入 .bak", ok,
               f"(bak存在: {os.path.exists(state + '.bak')})")


def scenario_2():
    """2: 首次保存 -> 不创建 .bak"""
    with tempfile.TemporaryDirectory() as d:
        state = os.path.join(d, 'trade_state.json')
        with mock.patch.object(trader_260725, 'STATE_FILE', state):
            CryptoTrader.save_batch_state(PersistFake(), SYMBOL, 'b_first', {'is_active': True})
        ok = (not os.path.exists(state + '.bak')
              and read_json(state) == {SYMBOL: {'b_first': {'is_active': True}}})
        report("场景2: 首次保存无 .bak", ok,
               f"(bak存在: {os.path.exists(state + '.bak')}, 状态写入: {os.path.exists(state)})")


def scenario_3():
    """3: clear 也备份 -> .bak 含被清批次（R11 类误清解药）"""
    with tempfile.TemporaryDirectory() as d:
        state = os.path.join(d, 'trade_state.json')
        old = {SYMBOL: {'b_del': {'is_active': True, 'current_sl_id': 'sl1'}}}
        with open(state, 'w', encoding='utf-8') as f:
            json.dump(old, f)
        with mock.patch.object(trader_260725, 'STATE_FILE', state):
            CryptoTrader.clear_batch_state(PersistFake(), SYMBOL, 'b_del')
        ok = (os.path.exists(state + '.bak')
              and read_json(state + '.bak') == old          # 被清批次完整保留在 .bak
              and read_json(state) == {})                    # 主文件已清
        report("场景3: clear 备份被清状态", ok,
               f"(bak含被清批次: {os.path.exists(state + '.bak')})")


def scenario_4():
    """4: 备份失败 -> 仅警告，主保存仍完成"""
    try:
        with tempfile.TemporaryDirectory() as d:
            state = os.path.join(d, 'trade_state.json')
            with open(state, 'w', encoding='utf-8') as f:
                json.dump({SYMBOL: {'b_old': {'is_active': True}}}, f)
            copy_mock = mock.MagicMock(side_effect=OSError("模拟磁盘故障"))
            with mock.patch.object(trader_260725, 'STATE_FILE', state), \
                 mock.patch.object(trader_260725.shutil, 'copy2', copy_mock):
                CryptoTrader.save_batch_state(PersistFake(), SYMBOL, 'b_new', {'is_active': True})
            # 断言与 detail 均须在临时目录存活期内求值
            ok = (copy_mock.called                                     # 备份被尝试过
                  and 'b_new' in read_json(state)[SYMBOL])              # 主保存未被阻断
            detail = f"(备份尝试: {copy_mock.called}, 主保存含新批次: {ok})"
    except AttributeError as e:   # 修复前 trader 无 shutil 属性
        ok, detail = False, f"(异常: {e})"
    report("场景4: 备份失败不阻断主保存", ok, detail)


def scenario_5():
    """5: AST——save/clear 体内无直接持久化代码，统一走 _persist_states"""
    with open('trader_260725.py', 'r', encoding='utf-8') as f:
        tree = ast.parse(f.read())
    funcs = {n.name: n for n in ast.walk(tree)
             if isinstance(n, ast.FunctionDef) and n.name in
             ('save_batch_state', 'clear_batch_state', '_persist_states')}

    def has_persist_code(node):
        for n in ast.walk(node):
            if isinstance(n, ast.Call):
                if isinstance(n.func, ast.Attribute) and n.func.attr in ('replace', 'copy2'):
                    return True
                if isinstance(n.func, ast.Name) and n.func.id == 'NamedTemporaryFile':
                    return True
        return False

    ok = ('_persist_states' in funcs
          and not has_persist_code(funcs.get('save_batch_state', ast.Module(body=[])))
          and not has_persist_code(funcs.get('clear_batch_state', ast.Module(body=[])))
          and has_persist_code(funcs['_persist_states']))
    report("场景5: save/clear 共用 _persist_states", ok,
           f"(找到方法: {sorted(funcs.keys())})")


if __name__ == '__main__':
    scenario_1()
    scenario_2()
    scenario_3()
    scenario_4()
    scenario_5()
    print("\n" + "#" * 60)
    failed = [n for n, p in RESULTS if not p]
    if failed:
        print(f"❌ {len(failed)}/{len(RESULTS)} 个场景失败: {failed}")
        sys.exit(1)
    print(f"✅ 全部 {len(RESULTS)} 个场景通过")
    print("C3/R12 状态备份验收完成")
