"""
实盘恢复前对账脚本（只读，不修改任何状态）

用途：
  1. 读取本地 trade_state.json（批次/SL/TP/registry）
  2. 连接 Binance USDM 期货，获取所有未结订单（双通道：normal + stop=True）
  3. 获取当前持仓
  4. 交叉对账并报告差异

运行方式：
  cd G:\my-crypto-bot
  .venv\Scripts\python.exe reconcile_pre_launch.py

环境变量（与 bot_runner 共用）：
  BINANCE_API_KEY, BINANCE_SECRET, BINANCE_PROXY（可选）

安全保证：
  - 仅调用 fetch_open_orders / fetch_positions / fetch_balance（只读 API）
  - 不调用任何 create/cancel/edit
  - 不写入任何本地文件
"""

import os
import sys
import json
import ccxt
from dotenv import load_dotenv
from pathlib import Path

# ==================== 配置 ====================
# Q13（2026-10-04）：CWD 相对 → 脚本相对绝对路径。跑错目录会读到空/错账本，
# 本地批次全盲 = fail-open（反向核对的成因之一），与标准启动链 CWD 钉死一致。
STATE_FILE = str(Path(__file__).resolve().parent / "trade_state.json")


def _norm_side(s):
    """方向归一（Q13）：BUY/buy→long，SELL/sell→short，BID/ASK 归一，其余小写原样。"""
    v = str(s or '').strip().lower()
    return {'buy': 'long', 'sell': 'short', 'bid': 'long',
            'ask': 'short'}.get(v, v)


def load_local_state():
    """读取本地 trade_state.json"""
    if not os.path.exists(STATE_FILE):
        print("⚠️ trade_state.json 不存在")
        return {}
    with open(STATE_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)

def create_exchange():
    """创建 ccxt.binanceusdm 实例（与 CryptoTrader.__init__ 一致）"""
    api_key = os.getenv("BINANCE_API_KEY")
    secret = os.getenv("BINANCE_SECRET")
    proxy_url = os.getenv("BINANCE_PROXY")

    if not api_key or not secret:
        print("❌ 缺少环境变量 BINANCE_API_KEY / BINANCE_SECRET")
        sys.exit(1)

    config = {
        'apiKey': api_key,
        'secret': secret,
        'enableRateLimit': True,
        'timeout': 30000,
        'options': {
            'defaultType': 'future',
            'fetchCurrencies': False,
            'adjustForTimeDifference': True,
            'recvWindow': 20000,
        }
    }
    if proxy_url:
        config['proxies'] = {'http': proxy_url, 'https': proxy_url}

    return ccxt.binanceusdm(config)

def fetch_all_open_orders(exchange, symbols):
    """双通道获取未结订单：normal + stop=True（P0-F1 同款修复）"""
    all_orders = {}
    errors = []

    for symbol in set(symbols):
        # 通道 1：normal
        try:
            normal_orders = exchange.fetch_open_orders(symbol)
            for o in normal_orders:
                all_orders[o['id']] = o
        except Exception as e:
            errors.append(f"normal/{symbol}: {e}")

        # 通道 2：stop=True（algo 条件单）
        try:
            stop_orders = exchange.fetch_open_orders(symbol, params={'stop': True})
            for o in stop_orders:
                all_orders[o['id']] = o
        except Exception as e:
            errors.append(f"stop/{symbol}: {e}")

    return list(all_orders.values()), errors

def fetch_positions(exchange):
    """获取当前持仓"""
    try:
        positions = exchange.fetch_positions()
        # 只保留有持仓的
        active = []
        for p in positions:
            contracts = float(p.get('contracts', 0) or 0)
            if contracts != 0:
                active.append(p)
        return active, []
    except Exception as e:
        return [], [str(e)]

def main():
    # ===== 加载 .env 文件 =====
    env_path = Path(__file__).parent / '.env'
    if env_path.exists():
        load_dotenv(env_path)
        print(f"✅ 已加载环境变量: {env_path}")
    else:
        print(f"⚠️ 未找到 .env 文件: {env_path}")
        print("   请确保 BINANCE_API_KEY 和 BINANCE_SECRET 已设置")

    print("=" * 70)
    print("  实盘恢复前对账（只读）")
    print("=" * 70)

    # ===== 1. 本地状态 =====
    print("\n📋 [1/4] 读取本地 trade_state.json ...")
    local_state = load_local_state()
    if not local_state:
        print("  ✅ 本地无活跃批次（trade_state.json 为空或不存在）")
        local_batches = []
        local_symbols = set()
    else:
        local_batches = []
        local_symbols = set()
        for symbol, batches in local_state.items():
            local_symbols.add(symbol)
            for batch_id, b_data in batches.items():
                if not b_data.get('is_active', False):
                    continue
                local_batches.append({
                    'symbol': symbol,
                    'batch_id': batch_id,
                    'side': b_data.get('side'),
                    'last_filled_count': b_data.get('last_filled_count', 0),
                    'batch_total_amount': b_data.get('batch_total_amount', 0),
                    'current_sl_id': b_data.get('current_sl_id'),
                    'tp_order_id': b_data.get('tp_order_id'),
                    'entry_orders': b_data.get('entry_orders', []),
                    'protection_registry': b_data.get('protection_registry', {}),
                    'pending_sl_orders': b_data.get('pending_sl_orders', []),
                    'sl_fail_count': b_data.get('sl_fail_count', {}),
                    'target_amounts': b_data.get('target_amounts', []),
                })
        print(f"  活跃批次数: {len(local_batches)}")
        for b in local_batches:
            sl_id = b['current_sl_id'] or 'None'
            tp_id = b['tp_order_id'] or 'None'
            reg_keys = list(b['protection_registry'].keys()) if b['protection_registry'] else []
            print(f"  - {b['symbol']} {b['batch_id']}: {b['last_filled_count']}层成交, "
                  f"SL={sl_id}, TP={tp_id}, registry={len(reg_keys)}条, "
                  f"pending_sl={b['pending_sl_orders']}")

    # ===== 2. 交易所未结订单 =====
    print(f"\n📡 [2/4] 连接 Binance USDM 期货，获取未结订单（双通道）...")
    exchange = create_exchange()
    print(f"  API Key: {os.getenv('BINANCE_API_KEY', '')[:8]}...")

    # 获取所有需要查询的 symbol（本地批次涉及的 + 全量）
    symbols_to_query = local_symbols if local_symbols else []
    # 如果本地有活跃批次，也查无 symbol 的全量（可能漏掉非本地 symbol 的孤儿单）
    # 但为安全起见，只查本地涉及的 symbol + 额外全量扫描
    all_open_orders = []
    order_errors = []

    if symbols_to_query:
        all_open_orders, order_errors = fetch_all_open_orders(exchange, symbols_to_query)
        # Q13（原 163-165 注释承诺「额外全量扫描」但未实现）：只查本地 symbol 时，
        # 本地之外的孤儿单逃过 4a。补双通道全量扫描 + 去重合并；失败进 order_errors
        #（UNKNOWN 不得通过）。
        try:
            _merged = {o['id']: o for o in all_open_orders}
            for o in exchange.fetch_open_orders():
                _merged.setdefault(o['id'], o)
            for o in exchange.fetch_open_orders(params={'stop': True}):
                _merged.setdefault(o['id'], o)
            all_open_orders = list(_merged.values())
        except Exception as e:
            order_errors.append(f"fetch_all_fullscan: {e}")
    else:
        # 本地无批次，但仍需全量扫描确认无残留
        try:
            all_open_orders = exchange.fetch_open_orders()
            # 也查 stop=True 通道
            stop_orders = exchange.fetch_open_orders(params={'stop': True})
            # 合并去重
            seen_ids = {o['id'] for o in all_open_orders}
            for o in stop_orders:
                if o['id'] not in seen_ids:
                    all_open_orders.append(o)
        except Exception as e:
            order_errors.append(f"fetch_all: {e}")

    if order_errors:
        print(f"  ⚠️ 获取订单时出现异常（{len(order_errors)} 个）:")
        for e in order_errors:
            print(f"    - {e}")
        print("  ⚠️ 异常可能导致漏查！建议排查网络/API 状态后重试。")

    print(f"  未结订单总数（双通道合并去重）: {len(all_open_orders)}")
    if all_open_orders:
        print(f"  明细:")
        for o in all_open_orders:
            otype = o.get('type', '?')
            oside = o.get('side', '?')
            ostatus = o.get('status', '?')
            oprice = o.get('stopPrice') or o.get('price', '?')
            oamount = o.get('amount', '?')
            print(f"    ID={o['id']}  symbol={o['symbol']}  type={otype}  side={oside}  "
                  f"status={ostatus}  price={oprice}  amount={oamount}")

    # ===== 3. 当前持仓 =====
    print(f"\n📊 [3/4] 获取当前持仓 ...")
    positions, pos_errors = fetch_positions(exchange)
    if pos_errors:
        print(f"  ⚠️ 获取持仓失败: {pos_errors}")
    else:
        print(f"  活跃持仓数: {len(positions)}")
        for p in positions:
            symbol = p.get('symbol', '?')
            side = p.get('side', '?')
            contracts = float(p.get('contracts', 0) or 0)
            entry_price = p.get('entryPrice', '?')
            unrealized_pnl = p.get('unrealizedPnl', '?')
            print(f"    {symbol}  side={side}  contracts={contracts}  "
                  f"entry={entry_price}  uPnL={unrealized_pnl}")

    # ===== 4. 交叉对账 =====
    print(f"\n🔍 [4/4] 交叉对账分析 ...")
    issues = []

    # Q13：UNKNOWN（拉取失败）必须进结论——只 print 不进 issues 会打印
    # 「✅ 对账通过」且 rc=0（错误安全结论）。结果未知 ≠ 结果一致。
    if order_errors:
        issues.append(
            f"🚨 [UNKNOWN] 获取未结订单失败/不完整（{len(order_errors)} 处）"
            f"——结果未知，禁止判定对账通过：" + "；".join(order_errors[:3]))
    if pos_errors:
        issues.append(
            f"🚨 [UNKNOWN] 获取持仓失败（{len(pos_errors)} 处）"
            f"——结果未知，禁止判定对账通过：" + "；".join(pos_errors[:3]))
    if order_errors or pos_errors:
        print("  ⚠️ [UNKNOWN] 拉取不完整：依赖失败数据源的交叉对账明细已跳过"
              "（避免误导性结论），修复网络/API 后重跑本脚本。")

    # 4a. 交易所订单 vs 本地状态
    local_order_ids = set()
    for b in local_batches:
        local_order_ids.update(b['entry_orders'])
        if b['current_sl_id']:
            local_order_ids.add(b['current_sl_id'])
        if b['tp_order_id']:
            local_order_ids.add(b['tp_order_id'])
        if b['protection_registry']:
            for reg_entry in b['protection_registry'].values():
                if reg_entry.get('order_id'):
                    local_order_ids.add(reg_entry['order_id'])

    exchange_order_ids = {o['id'] for o in all_open_orders}

    # 交易所有但本地不知道的 = 潜在孤儿单
    unknown_on_exchange = exchange_order_ids - local_order_ids
    if unknown_on_exchange:
        for oid in unknown_on_exchange:
            o = next((x for x in all_open_orders if x['id'] == oid), None)
            otype = o.get('type', '?') if o else '?'
            issues.append(f"🚨 交易所存在本地不认识的订单: ID={oid} type={otype}（潜在孤儿单）")

    # 4b. 本地有 SL/TP ID 但交易所没有 = 保护缺失
    # Q13：订单拉取失败时跳过——部分失败会把「没拉到」误报成「已撤销/成交」（假明细）
    if order_errors:
        print("  ⚠️ [Q13] 订单拉取不完整，跳过 4b 存在性核对（结论已由 UNKNOWN 承担）")
    else:
        for b in local_batches:
            if b['current_sl_id'] and b['current_sl_id'] not in exchange_order_ids:
                issues.append(f"⚠️ {b['symbol']} {b['batch_id']}: local_sl_id={b['current_sl_id']} "
                              f"在交易所未找到（SL 可能已被撤销/成交，本地状态需更新）")
            if b['tp_order_id'] and b['tp_order_id'] not in exchange_order_ids:
                issues.append(f"⚠️ {b['symbol']} {b['batch_id']}: local_tp_id={b['tp_order_id']} "
                              f"在交易所未找到（TP 可能已被撤销/成交，本地状态需更新）")

    # 4c. 本地有持仓但无 SL/TP = 裸仓风险
    # F3（2026-08-21）：本地 key 'BTCUSDT' vs ccxt 'BTC/USDT:USDT' 归一化比对（防误判无持仓）
    # F3b（2026-08-21）：与 trader F1 _norm_sym 对齐——按 base+quote 提取，
    #   修"去分隔符版把 'BTC/USDT:USDT' 变成 'BTCUSDTUSDT'（结算币重复）→ 4d 恒误报无持仓"
    def _norm_symbol(s):
        s = str(s or '').upper()
        if '/' in s:
            base, rest = s.split('/', 1)
            quote = rest.split(':', 1)[0]
            return (base + quote).replace('_', '')
        return s.replace('/', '').replace(':', '').replace('_', '')

    order_by_id = {o['id']: o for o in all_open_orders}
    _sl_cover = {}   # R2b：（symbol归一, 实际持仓方向）→ {SL订单id: 有效覆盖量}

    for b in local_batches:
        batch_side_n = _norm_side(b.get('side'))
        # R4（独立复审）：hedge 模式下同 symbol 可有 long+short 两个持仓，
        # 旧“首个 symbol 匹配 + break”会用 long 持仓误报 SELL 批次方向冲突（健康假阳性）。
        # 改按同 symbol + 同方向匹配；只有当该 symbol 下全部非零持仓都与批次反向时才报冲突。
        matching = [p for p in positions
                    if _norm_symbol(p.get('symbol')) == _norm_symbol(b['symbol'])
                    and float(p.get('contracts', 0) or 0) != 0]
        same_dir = [p for p in matching if batch_side_n and _norm_side(p.get('side')) == batch_side_n]
        diff_dir = [p for p in matching if batch_side_n and _norm_side(p.get('side'))
                    and _norm_side(p.get('side')) != batch_side_n]
        has_position = False
        pos_side_n = ''
        if same_dir:
            has_position = True
            pos_side_n = batch_side_n
        elif matching and diff_dir:
            has_position = True
            pos_side_n = _norm_side(diff_dir[0].get('side'))
            # Q13（原 275-277 死分支）：pos 'LONG' vs batch 'BUY' 直比恒不等 →
            # 旧代码 pass 掉了。归一后核对：方向冲突 = 状态损坏（恢复会反向/双开）。
            issues.append(f"🚨 {b['symbol']} {b['batch_id']}: 持仓方向 {pos_side_n}"
                          f"与批次方向 {batch_side_n} 不一致（状态损坏，恢复前需人工判定）")

        if has_position and not b['current_sl_id']:
            issues.append(f"🚨 {b['symbol']} {b['batch_id']}: 有持仓但 current_sl_id=None（裸仓风险！"
                          f"恢复后监控线程将自动补挂 SL，但恢复前需确认）")
        if has_position and not b['tp_order_id']:
            issues.append(f"⚠️ {b['symbol']} {b['batch_id']}: 有持仓但 tp_order_id=None（无止盈单）")

        # Q13：SL 有效性/方向/覆盖量核验——存在 ≠ 有效（4b 只验 id 在场）。
        # 订单数据不完整时 sl_o 缺席 → 跳过（UNKNOWN 已承担结论，不造假明细）。
        if has_position and b['current_sl_id']:
            sl_o = order_by_id.get(b['current_sl_id'])
            if sl_o is not None:
                sl_type = str(sl_o.get('type') or '').upper()
                if 'STOP' not in sl_type:
                    issues.append(f"🚨 {b['symbol']} {b['batch_id']}: SL {b['current_sl_id']} "
                                  f"type={sl_o.get('type')} 非止损类型（SL 无效，不提供保护）")
                # R2（独立复审）：SL 单本身必须与批次同 symbol——id 命中 ≠ 保护本批。
                # 旧实现只看 id、不看 symbol，SL 挂在别的 symbol 也判“有效”。
                sl_sym_n = _norm_symbol(sl_o.get('symbol'))
                if sl_sym_n and sl_sym_n != _norm_symbol(b['symbol']):
                    issues.append(f"🚨 {b['symbol']} {b['batch_id']}: SL {b['current_sl_id']} "
                                  f"symbol={sl_o.get('symbol')!r} 与批次 symbol 不一致（身份错配，"
                                  f"SL 不保护本批仓位）")
                sl_side = str(sl_o.get('side') or '').lower()
                expect_side = ('sell' if pos_side_n == 'long'
                               else 'buy' if pos_side_n == 'short' else '')
                if not sl_side:
                    # R3（独立复审）：side 缺失 → 方向不可核验，不再静默放行（fail-closed）。
                    issues.append(f"🚨 {b['symbol']} {b['batch_id']}: SL {b['current_sl_id']} "
                                  f"缺 side 字段（方向不可核验，按无效保护处理）")
                elif expect_side and sl_side != expect_side:
                    issues.append(f"🚨 {b['symbol']} {b['batch_id']}: SL {b['current_sl_id']} "
                                  f"方向错误（持仓 {pos_side_n} 期望 side={expect_side}，"
                                  f"实际 {sl_side}）——触发将反向开仓/无法止损")
                # R7（外部评审反例）：positionSide + 平仓语义核验——对齐仓库现有
                # 止损判据 _check_protection_order_validity（trader 7188-7200）：
                #   positionSide 在场（对冲单/交易所标记）→ 必须与持仓方向一致
                #   （long 持仓挂 positionSide=SHORT 的单平的是空仓 = 零保护，
                #   修前 rc=0 放行）；对冲批次缺 positionSide → 方向不可核验；
                #   单向无 positionSide → reduceOnly/closePosition 至少一个 true
                #   （否则是开仓单非保护单）。不通过 → 逐批 issue 且不计入 R2b 聚合。
                _sl_info = sl_o.get('info') or {}
                _pside = str(_sl_info.get('positionSide') or '').strip().upper()
                _sl_close_ok = True
                _close_reason = ''
                if _pside:
                    if pos_side_n and _pside != pos_side_n.upper():
                        _sl_close_ok = False
                        _close_reason = (f"positionSide={_pside} 与持仓 "
                                         f"{pos_side_n.upper()} 不一致（该单平的是 "
                                         f"{_pside} 仓，不保护本持仓，无效保护）")
                elif bool(b.get('is_hedge_mode')):
                    _sl_close_ok = False
                    _close_reason = "缺 positionSide 字段（对冲单方向不可核验，按无效保护处理）"
                else:
                    _ro = str(_sl_info.get('reduceOnly') or '').lower()
                    _cp = str(_sl_info.get('closePosition') or '').lower()
                    if _ro != 'true' and _cp != 'true':
                        _sl_close_ok = False
                        _close_reason = ("缺平仓语义（info.reduceOnly/closePosition 均非 true，"
                                         "非保护单）")
                if not _sl_close_ok:
                    issues.append(f"🚨 {b['symbol']} {b['batch_id']}: SL {b['current_sl_id']} "
                                  f"{_close_reason}")
                expect_amt = 0.0
                try:
                    t_amt = b.get('target_amounts') or []
                    if t_amt:
                        expect_amt = sum(float(x) for x in t_amt[:b['last_filled_count']])
                    elif b['last_filled_count'] > 0:
                        expect_amt = float(b.get('batch_total_amount') or 0)
                except (TypeError, ValueError):
                    expect_amt = 0.0
                try:
                    sl_amt = float(sl_o.get('amount') or 0)
                    # R3（独立复审）：NaN / inf 使比较恒假 → 旧代码静默放行。
                    if sl_amt != sl_amt or sl_amt in (float('inf'), float('-inf')):
                        raise ValueError('non-finite amount')
                except (TypeError, ValueError):
                    sl_amt = 0.0
                    issues.append(f"🚨 {b['symbol']} {b['batch_id']}: SL {b['current_sl_id']} "
                                  f"amount 缺失/非法（{sl_o.get('amount')!r}），覆盖量不可核验")
                # 账本口径对照（CE4）：本批 SL 必须覆盖本批已成交目标量（多批同才向
                # 各自覆盖——不要求每个批次都覆盖总仓）。实际总仓聚合核验见下方 R2b。
                if expect_amt > 0 and sl_amt + 1e-12 < expect_amt:
                    issues.append(f"🚨 {b['symbol']} {b['batch_id']}: SL {b['current_sl_id']} "
                                  f"覆盖不足（SL amount={sl_amt} < 已成交 {expect_amt}）"
                                  f"——部分仓位无保护")
                # R2b（外部评审复核结论）：有效 SL 需累计到「symbol + 方向」的
                # 持仓对照表——多批次同向持仓各自持有独立 SL 是健康配置；但按
                # SL id 去重后的总有效覆盖量必须 ≥ 同方向实际总仓。
                # R7：平仓语义核验（_sl_close_ok）不通过的 SL 不得计入覆盖。
                if (same_dir and 'STOP' in sl_type
                        and sl_sym_n and sl_sym_n == _norm_symbol(b['symbol'])
                        and sl_side and expect_side and sl_side == expect_side and sl_amt > 0
                        and _sl_close_ok):
                    _sl_cover.setdefault((_norm_symbol(b['symbol']), pos_side_n), {})[b['current_sl_id']] = sl_amt

    # R2b（外部评审复核结论）：「symbol + 方向」维度的有效 SL 总和必须 ≥ 实际
    # 持仓量；SL id 去重防止同一保护单计入两批。逐批口径见上方 CE4。
    for (_sym, _side), _sl_map in _sl_cover.items():
        _total = sum(_sl_map.values())
        _pos = 0.0
        for p in positions:
            if _norm_symbol(p.get('symbol')) == _sym and _norm_side(p.get('side')) == _side:
                _pos += float(p.get('contracts', 0) or 0)
        if _pos > 0 and _total + 1e-12 < _pos:
            issues.append(f"🚨 {_sym} {_side}: 有效 SL 覆盖不足（合计 {_total} < 实际持仓 {_pos}）"
                          f"——部分仓位无保护")

    # 4d. 本地有批次但交易所无持仓 = 残留状态
    # Q13：持仓拉取失败时跳过——positions=[] 会把「没拉到」误报成「已手动平仓」（假明细）
    if pos_errors:
        print("  ⚠️ [Q13] 持仓拉取不完整，跳过 4d 残留核对（结论已由 UNKNOWN 承担）")
    else:
        for b in local_batches:
            _b_side = _norm_side(b.get('side'))
            has_position = any(
                _norm_symbol(p.get('symbol')) == _norm_symbol(b['symbol'])
                and (not _b_side or _norm_side(p.get('side')) == _b_side)
                and float(p.get('contracts', 0) or 0) != 0
                for p in positions
            )
            if not has_position and b['last_filled_count'] > 0:
                issues.append(f"⚠️ {b['symbol']} {b['batch_id']}: 本地有 {b['last_filled_count']} 层成交记录"
                              f"但交易所无持仓（仓位可能已手动平仓，本地状态需清理）")

    # 4e. 【反向核对 Q13】交易所有持仓 → 本地必须有对应活跃批次。
    #     4c/4d 只遍历 local_batches：本地空/漏（含 STATE_FILE 跑错目录）时恒静默 = fail-open。
    for p in positions:
        p_contracts = float(p.get('contracts', 0) or 0)
        if p_contracts == 0:
            continue
        p_side = _norm_side(p.get('side'))
        covered = [b for b in local_batches
                   if _norm_symbol(b['symbol']) == _norm_symbol(p.get('symbol'))]
        if not covered:
            issues.append(f"🚨 [反向核对] 交易所有持仓 {p.get('symbol')} "
                          f"side={p.get('side') or '?'} contracts={p_contracts}，"
                          f"本地无对应活跃批次——裸仓/孤儿持仓（无人接管）！恢复前必须查明来源")
            continue
        if p_side and not any(_norm_side(b.get('side')) == p_side for b in covered):
            issues.append(f"🚨 [反向核对] 交易所有持仓 {p.get('symbol')} side={p_side}，"
                          f"本地活跃批次方向={sorted({_norm_side(b.get('side')) or '?' for b in covered})}"
                          f"——方向不匹配（状态损坏/对账键错位）")

    # ===== 汇总 =====
    print(f"\n{'=' * 70}")
    if not issues:
        print("  ✅ 对账通过：交易所状态与本地状态一致，无孤儿单，无裸仓")
        print("  ✅ 可以进入实盘恢复流程")
    else:
        print(f"  🚨 发现 {len(issues)} 个问题，需处理后再恢复实盘：")
        for i, issue in enumerate(issues, 1):
            print(f"  {i}. {issue}")
    print(f"{'=' * 70}")

    return 0 if not issues else 1

if __name__ == '__main__':
    sys.exit(main())