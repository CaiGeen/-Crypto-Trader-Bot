#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
【只读】交易所 ↔ 账本 逐单核对器（v1.1.1）

⚠️ 本文件是**离线只读验证工具**，不是生产代码。
   工作树里出现对它的修改，不构成生产变更。
   生产交易逻辑只存在于：trader_260725.py / bot_runner.py / watchdog.py。

用途：
  1) 验证启动恢复 / 异常重启接管历史批次时，不会向交易所重复挂出任何单
  2) 作为「强杀测试」的 before/after 硬判据：比较的是 **ID 集合完全相等**，
     而不只是数量相等（数量相等但 ID 全换 = 重新挂单，必须判失败）

v1.1.1 修正（2026-08-29，归档编号 v1.1.1 / 修复 Binance 时间偏移导致的只读核对器误失败）：
  * 症状：`ccxt.base.errors.InvalidNonce: binanceusdm {"code":-1021,
           "msg":"Timestamp for this request was 1000ms ahead of the server's time."}`
  * 根因：`options={'adjustForTimeDifference': True}` **只在收到错误后重试时生效**，
          首个 `load_markets()` 不受保护 → 首次请求即被 -1021 拒绝，脚本无法运行。
  * 修复：建实例后显式调用 `ex.load_time_difference()`（try/except 包裹，失败不致命）。
          生产 trader 启动时本就主动同步服务器时间，故本缺陷只影响自建工具。
  * 影响面：仅本脚本，零交易逻辑改动。

v1.1 修正（v1.0 的两个误报来源，均为脚本缺陷，非生产问题）：
  * 保护单（SL / TP）存在 batch 的 `current_sl_id` / `tp_order_id`，
    不在 `entry_orders` 里 → v1.0 会误报「交易所多出孤儿单」
  * 已成交的 entry 条件单会从交易所 algo 列表消失（正常），
    对应 `filled_details[i] != 0` → v1.0 会误报「账本多出」

安全约束（硬性）：
  * 仅调用 fetch_open_orders（普通端点 + stop=True 条件单端点）与 fetch_positions
  * 绝不调用 create_order / cancel_order / 任何写操作
  * 绝不写入 trade_state.json 等任何状态文件（--snapshot 只写指定的输出文件）

用法：
  python verify_no_duplicate_orders.py                          # 单次核对
  python verify_no_duplicate_orders.py --snapshot before.json   # 记录基线
  python verify_no_duplicate_orders.py --compare  before.json   # 与基线严格对比
"""
import argparse
import json
import os
import sys

ROOT = r'G:\my-crypto-bot'
sys.path.insert(0, ROOT)

# --- 尝试加载 .env（与生产同源） ---
try:
    from dotenv import load_dotenv
    for cand in (os.path.join(ROOT, '.env'),):
        if os.path.exists(cand):
            load_dotenv(cand, override=False)
            print(f"[env] 已加载 {cand}")
            break
except Exception:
    pass

api_key = os.getenv("BINANCE_API_KEY")
# 注意：本项目 .env 的键名是 BINANCE_SECRET（不是 BINANCE_API_SECRET）
api_secret = os.getenv("BINANCE_SECRET") or os.getenv("BINANCE_API_SECRET")
if not api_key or api_secret is None:
    print("❌ 未取得 API 凭据（BINANCE_API_KEY / BINANCE_SECRET），无法核对")
    print("   提示：生产进程由 PyCharm 注入环境变量；本脚本为独立进程，需 .env 或显式导出")
    sys.exit(2)

proxy_url = os.getenv("BINANCE_PROXY") or None

import ccxt  # noqa: E402

SYMBOL = 'BTCUSDT'


def collect_ledger(state):
    """账本侧：收集「此刻应当仍然挂在交易所」的订单 ID 全集。

    返回 (expect_alive, detail, settled)
      expect_alive: {order_id: (batch_id, 角色, 说明)}
      settled:      [(order_id, batch_id, 层号)] 已成交、预期已不在交易所的 entry 单
    """
    expect_alive = {}
    settled = []

    for _sym, batches in state.items():
        for bid, b in batches.items():
            if b.get('is_active') is False:
                continue

            filled = b.get('filled_details') or []

            # --- entry 条件单 ---
            for i, eo in enumerate(b.get('entry_orders', []) or []):
                if isinstance(eo, dict):
                    oid = str(eo.get('order_id') or eo.get('id') or '')
                    trig = eo.get('trigger_price') or eo.get('price')
                else:
                    oid = str(eo or '')
                    trig = None
                if not oid:
                    continue

                is_filled = False
                if i < len(filled):
                    try:
                        is_filled = float(filled[i] or 0) > 0
                    except (TypeError, ValueError):
                        is_filled = bool(filled[i])

                if is_filled:
                    settled.append((oid, bid, i))
                else:
                    expect_alive[oid] = (bid, 'ENTRY', f'层{i} 触发价={trig}')

            # --- 保护单（v1.1 新增：此前遗漏，导致误报孤儿单）---
            sl = b.get('current_sl_id')
            if sl:
                expect_alive[str(sl)] = (bid, 'SL', '当前止损单')
            tp = b.get('tp_order_id')
            if tp:
                expect_alive[str(tp)] = (bid, 'TP', '当前止盈单')

    return expect_alive, settled


def classify(o):
    """把交易所订单归类，便于人读。

    注意：ccxt 归一化后 `type` 常退化为 'market'/'limit'，
    真实类型在 `info['type']`（如 TAKE_PROFIT_MARKET / STOP_MARKET）里。
    """
    side = (o.get('side') or '').lower()
    info = o.get('info') or {}
    stop_px = info.get('stopPrice') or o.get('stopPrice')
    otype = ((info.get('type') or o.get('type') or '')).upper()
    if 'TAKE_PROFIT' in otype:
        return 'TP', stop_px
    if 'STOP' in otype:
        return 'SL', stop_px
    if side == 'sell':
        return 'SELL?', stop_px
    return 'ENTRY', stop_px


def collect_meta():
    """采集本机维度：进程树 + 状态文件指纹（用于强杀前后对比）。"""
    import hashlib
    import subprocess
    import time

    meta = {'captured_at': time.strftime('%Y-%m-%d %H:%M:%S'),
            'unix_ts': time.time(), 'processes': [], 'files': {}}

    # --- 进程树（只取 python.exe，含 PID/PPID/启动时刻/命令行） ---
    try:
        ps = (
            "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
            "ForEach-Object { $_.ProcessId.ToString() + '|' + "
            "$_.ParentProcessId.ToString() + '|' + $_.CreationDate + '|' + "
            "$_.CommandLine }"
        )
        out = subprocess.run(['powershell', '-NoProfile', '-Command', ps],
                             capture_output=True, text=True, timeout=60)
        for line in (out.stdout or '').splitlines():
            line = line.strip()
            if not line or '|' not in line:
                continue
            parts = line.split('|', 3)
            if len(parts) < 4:
                continue
            meta['processes'].append({
                'pid': int(parts[0]), 'ppid': int(parts[1]),
                'created': parts[2], 'cmd': parts[3][-60:],
            })
    except Exception as e:
        meta['processes'] = [{'error': str(e)}]

    # --- 状态文件指纹（只读 stat + md5，绝不修改） ---
    for name in ('trade_state.json', 'trade_tombstones.json', 'trade_stats.json'):
        p = os.path.join(ROOT, name)
        try:
            st = os.stat(p)
            with open(p, 'rb') as f:
                digest = hashlib.md5(f.read()).hexdigest()[:12]
            meta['files'][name] = {
                'size': st.st_size,
                'mtime': time.strftime('%H:%M:%S', time.localtime(st.st_mtime)),
                'md5_12': digest,
            }
        except FileNotFoundError:
            meta['files'][name] = {'error': 'not found'}
    return meta


def build_snapshot(ex, state):
    """采集一次完整快照（只读）。"""
    cond = ex.fetch_open_orders(SYMBOL, params={'stop': True})
    normal = ex.fetch_open_orders(SYMBOL)
    try:
        poss = ex.fetch_positions([SYMBOL])
        live = [p for p in poss if abs(float(p.get('contracts') or 0)) > 0]
        pos = [{'symbol': p.get('symbol'), 'side': p.get('side'),
                'contracts': p.get('contracts'),
                'entryPrice': p.get('entryPrice')} for p in live]
    except Exception as e:
        pos = [{'error': str(e)}]

    expect_alive, settled = collect_ledger(state)

    return {
        'cond': [{'id': str(o['id']), 'type': o.get('type'),
                  'side': o.get('side'),
                  'stopPx': (o.get('info') or {}).get('stopPrice'),
                  'qty': o.get('amount')} for o in cond],
        'normal': [{'id': str(o['id']), 'type': o.get('type'),
                    'side': o.get('side'), 'price': o.get('price'),
                    'qty': o.get('amount')} for o in normal],
        'positions': pos,
        'ledger_expect_alive': {k: {'batch': v[0], 'role': v[1], 'desc': v[2]}
                                for k, v in expect_alive.items()},
        'ledger_settled': [{'id': a, 'batch': b, 'layer': c}
                           for a, b, c in settled],
        'batches': {bid: {'is_active': bb.get('is_active'),
                          'last_filled_count': bb.get('last_filled_count'),
                          'close_phase': bb.get('close_phase')}
                    for _s, bs in state.items() for bid, bb in bs.items()},
        'meta': collect_meta(),
    }


def report_meta_diff(base, snap):
    """对比本机维度：PID 变更 + 账本完好性。"""
    print("\n" + "=" * 74)
    print("[6] 本机维度对比（进程 + 状态文件）")
    print("=" * 74)
    rc = 0
    bm, am = base.get('meta', {}), snap.get('meta', {})

    bp = {p.get('pid') for p in bm.get('processes', []) if 'pid' in p}
    ap = {p.get('pid') for p in am.get('processes', []) if 'pid' in p}
    new_pids = ap - bp
    gone_pids = bp - ap
    print(f"    before PIDs: {sorted(bp)}")
    print(f"    after  PIDs: {sorted(ap)}")
    print(f"    新增 {sorted(new_pids)}  消失 {sorted(gone_pids)}")
    if not new_pids:
        print("    ❌ 无任何新进程 → bot_runner 未被重新拉起")
        rc = 1

    print("\n    状态文件：")
    for name in sorted(set(bm.get('files', {})) | set(am.get('files', {}))):
        b = bm.get('files', {}).get(name, {})
        a = am.get('files', {}).get(name, {})
        if 'error' in a:
            print(f"      ❌ {name}: {a['error']}")
            rc = 1
            continue
        same = b.get('md5_12') == a.get('md5_12')
        flag = '（内容未变）' if same else '（内容已变）'
        print(f"      {name:24} {b.get('size','?'):>7}B/{b.get('mtime','?')} "
              f"→ {a.get('size','?'):>7}B/{a.get('mtime','?')} {flag}")
        if a.get('size', 0) == 0:
            print(f"        ❌ {name} 为空文件 → 账本被截断！")
            rc = 1
    return rc


def report(snap, compare_base=None):
    rc = 0
    cond = snap['cond']
    ex_ids = {c['id'] for c in cond}
    led = snap['ledger_expect_alive']
    led_ids = set(led)
    settled = {s['id'] for s in snap['ledger_settled']}

    print("\n" + "=" * 74)
    print("[1] 交易所 BTCUSDT（只读）")
    print("=" * 74)
    print(f"    条件单(algo): {len(cond)}")
    for c in cond:
        role, stop_px = classify(c)
        owner = led.get(c['id'])
        tag = f"← 账本 {owner['batch'][-14:]} {owner['role']}" if owner else "← ⚠️ 账本无记录"
        print(f"      - {c['id']}  {role:6} side={c['side']:4} "
              f"stopPx={stop_px}  qty={c['qty']}  {tag}")
    print(f"    普通单: {len(snap['normal'])}")
    for n in snap['normal']:
        print(f"      - {n['id']}  side={n['side']} price={n['price']} qty={n['qty']}")

    print(f"\n[2] 持仓: {len(snap['positions'])} 条")
    for p in snap['positions']:
        print(f"      - {p}")

    print(f"\n[3] 账本预期在交易所的订单: {len(led_ids)}")
    for oid, v in sorted(led.items(), key=lambda x: (x[1]['batch'], x[1]['role'])):
        print(f"      - {oid}  {v['role']:6} {v['batch'][-14:]}  {v['desc']}")
    if snap['ledger_settled']:
        print(f"    （已成交、预期不在交易所: {len(snap['ledger_settled'])}）")
        for s in snap['ledger_settled']:
            print(f"      - {s['id']}  {s['batch'][-14:]} 层{s['layer']} ✅已成交")

    # ---- 差集 ----
    only_ex = ex_ids - led_ids
    only_led = led_ids - ex_ids

    print("\n" + "=" * 74)
    print("[4] 差集判定")
    print("=" * 74)
    print(f"    交易所 {len(ex_ids)}  |  账本预期 {len(led_ids)}")

    if only_ex:
        print(f"    ❌ 交易所多出 {len(only_ex)} 个（账本无记录 → 疑似重复挂单 / 孤儿单）:")
        for oid in sorted(only_ex):
            c = next((x for x in cond if x['id'] == oid), None)
            print(f"        {oid}  side={c['side'] if c else '?'} "
                  f"stopPx={c['stopPx'] if c else '?'}")
        rc = 1
    else:
        print("    ✅ 交易所无任何账本外的条件单 → 无重复挂单、无孤儿单")

    if only_led:
        print(f"    ❌ 账本预期存在但交易所查不到 {len(only_led)} 个:")
        for oid in sorted(only_led):
            v = led[oid]
            print(f"        {oid}  {v['role']}  批次={v['batch']}  {v['desc']}")
        rc = 1
    else:
        print("    ✅ 账本预期的每一个单在交易所都能查到")

    if snap['normal']:
        print(f"    ⚠️ 交易所存在 {len(snap['normal'])} 个普通单（非条件单），请人工确认")
        if rc == 0:
            rc = 1

    # ---- before / after 严格对比 ----
    if compare_base is not None:
        print("\n" + "=" * 74)
        print(f"[5] 与基线 {compare_base} 严格对比（ID 集合相等，而非数量相等）")
        print("=" * 74)
        try:
            with open(compare_base, encoding='utf-8') as f:
                base = json.load(f)
        except Exception as e:
            print(f"    ❌ 基线读取失败: {e}")
            return 1

        b_ids = {c['id'] for c in base['cond']}
        a_ids = {c['id'] for c in snap['cond']}

        print(f"    before 条件单 {len(b_ids)} 个")
        print(f"    after  条件单 {len(a_ids)} 个")

        added = a_ids - b_ids
        removed = b_ids - a_ids
        if added:
            print(f"    ❌ 新增 {len(added)} 个 → 恢复过程重新挂单了:")
            for oid in sorted(added):
                print(f"        + {oid}")
            rc = 1
        if removed:
            # 已成交导致的消失是合法的：需核对是否落在 settled 里
            legit = {s['id'] for s in snap['ledger_settled']}
            bad = removed - legit
            if bad:
                print(f"    ❌ 消失 {len(bad)} 个（非成交原因）:")
                for oid in sorted(bad):
                    print(f"        - {oid}")
                rc = 1
            if removed & legit:
                print(f"    ℹ️ 消失 {len(removed & legit)} 个属正常（期间真实成交）")
        if not added and not removed:
            print("    ✅ 条件单 ID 集合完全相等 → 未重新挂单")
        elif not added and removed and not (removed - {s['id'] for s in snap['ledger_settled']}):
            print("    ✅ 无新增单，消失均为真实成交 → 未重新挂单")

        def role_map(side_snap):
            """把交易所 ID 映射成 (batch, role)；无归属的记为 ('?', 'ORPHAN')。"""
            led = side_snap['ledger_expect_alive']
            out = {}
            for c in side_snap['cond']:
                v = led.get(c['id'])
                out[c['id']] = (v['batch'], v['role']) if v else ('?', 'ORPHAN')
            return out

        b_role, a_role = role_map(base), role_map(snap)
        b_roleset = sorted(b_role.values())
        a_roleset = sorted(a_role.values())

        if b_roleset == a_roleset and not added and not removed:
            print("    ✅ 角色集合与 ID 集合均完全相等")
        else:
            print(f"    before 角色集合: {b_roleset}")
            print(f"    after  角色集合: {a_roleset}")
            if b_roleset == a_roleset:
                print("    ℹ️ 角色集合相同但 ID 有变化 → 发生了 replace（重挂同角色单）。")
                print("       需人工判别来源：①程序自发的 SL/TP 同步维护  ②恢复过程重新挂单。")
                print("       判据：查看终端日志，恢复阶段不应出现任何『已挂出』字样。")
            else:
                rc = 1

        bpos = {(p.get('symbol'), p.get('side'), str(p.get('contracts')))
                for p in base.get('positions', [])}
        apos = {(p.get('symbol'), p.get('side'), str(p.get('contracts')))
                for p in snap.get('positions', [])}
        if bpos != apos:
            print(f"    ℹ️ 持仓发生变化（期间成交属正常）: before={bpos} after={apos}")

        rc |= report_meta_diff(base, snap)

    print("\n" + "=" * 74)
    if rc == 0:
        print("✅ 结论：账本与交易所逐单一致 —— 接管未产生重复挂单 / 无孤儿单")
    else:
        print("❌ 结论：存在差异，请人工核实上方明细")
    print("=" * 74)
    return rc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--snapshot', help='把本次快照写入指定 JSON 文件（只写该文件）')
    ap.add_argument('--compare', help='与指定基线 JSON 严格对比（ID 集合相等）')
    args = ap.parse_args()

    cfg = {
        'apiKey': api_key,
        'secret': api_secret,
        'enableRateLimit': True,
        'timeout': 30000,
        'options': {'defaultType': 'future', 'adjustForTimeDifference': True,
                    'recvWindow': 20000},
    }
    if proxy_url:  # 与生产同源，避免 IP 变化触发风控
        cfg['proxies'] = {'http': proxy_url, 'https': proxy_url}
        print(f"[env] 使用代理: {proxy_url}")
    ex = ccxt.binanceusdm(cfg)
    # 与生产同源：启动先同步服务器时间，避免 -1021 InvalidNonce
    # （adjustForTimeDifference 只在出错重试时生效，首个 load_markets 不受保护）
    try:
        ex.load_time_difference()
    except Exception as e:  # 时间同步失败不致命，继续（可能本机时间本就准）
        print(f"[warn] 服务器时间同步失败（继续）: {type(e).__name__}: {e}")
    ex.load_markets()

    print("=" * 74)
    print("交易所 ↔ 账本 逐单只读核对器 v1.1")
    print("=" * 74)

    with open(os.path.join(ROOT, 'trade_state.json'), encoding='utf-8') as f:
        state = json.load(f)

    snap = build_snapshot(ex, state)

    if args.snapshot:
        out = args.snapshot
        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
        with open(out, 'w', encoding='utf-8') as f:
            json.dump(snap, f, ensure_ascii=False, indent=2)
        print(f"\n[snapshot] 已写入 {out}")
        print(f"           条件单 {len(snap['cond'])} / 普通单 {len(snap['normal'])} "
              f"/ 持仓 {len(snap['positions'])}")

    return report(snap, compare_base=args.compare)


if __name__ == '__main__':
    sys.exit(main())
