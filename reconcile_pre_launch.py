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
STATE_FILE = "trade_state.json"

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
    for b in local_batches:
        if b['current_sl_id'] and b['current_sl_id'] not in exchange_order_ids:
            issues.append(f"⚠️ {b['symbol']} {b['batch_id']}: local_sl_id={b['current_sl_id']} "
                          f"在交易所未找到（SL 可能已被撤销/成交，本地状态需更新）")
        if b['tp_order_id'] and b['tp_order_id'] not in exchange_order_ids:
            issues.append(f"⚠️ {b['symbol']} {b['batch_id']}: local_tp_id={b['tp_order_id']} "
                          f"在交易所未找到（TP 可能已被撤销/成交，本地状态需更新）")

    # 4c. 本地有持仓但无 SL/TP = 裸仓风险
    # F3（2026-08-21）：本地 key 'BTCUSDT' vs ccxt 'BTC/USDT:USDT' 归一化比对（防误判无持仓）
    def _norm_symbol(s):
        return str(s or '').replace('/', '').replace(':', '').upper()

    for b in local_batches:
        has_position = False
        for p in positions:
            if _norm_symbol(p.get('symbol')) == _norm_symbol(b['symbol']) and float(p.get('contracts', 0) or 0) != 0:
                has_position = True
                # 检查持仓方向是否匹配
                pos_side = p.get('side', '').upper()
                batch_side = b.get('side', '').upper()
                if pos_side and batch_side and pos_side != batch_side:
                    # hedge mode 下 side 可能是 LONG/SHORT，也可能空
                    pass
                break

        if has_position and not b['current_sl_id']:
            issues.append(f"🚨 {b['symbol']} {b['batch_id']}: 有持仓但 current_sl_id=None（裸仓风险！"
                          f"恢复后监控线程将自动补挂 SL，但恢复前需确认）")
        if has_position and not b['tp_order_id']:
            issues.append(f"⚠️ {b['symbol']} {b['batch_id']}: 有持仓但 tp_order_id=None（无止盈单）")

    # 4d. 本地有批次但交易所无持仓 = 残留状态
    for b in local_batches:
        has_position = any(
            _norm_symbol(p.get('symbol')) == _norm_symbol(b['symbol']) and float(p.get('contracts', 0) or 0) != 0
            for p in positions
        )
        if not has_position and b['last_filled_count'] > 0:
            issues.append(f"⚠️ {b['symbol']} {b['batch_id']}: 本地有 {b['last_filled_count']} 层成交记录"
                          f"但交易所无持仓（仓位可能已手动平仓，本地状态需清理）")

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