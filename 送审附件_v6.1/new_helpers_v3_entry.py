# -*- coding: utf-8 -*-
"""new_helpers_v3_entry.py — v3 版 _cancel_and_verify_entry_orders 的缺陷源码档案。

⚠️ 这是负向对照样本，不是可运行实现。它含有 ChatGPT 终审 §三 指认的 P0 缺陷：
   `fetch_open_orders(...) or []` 把 None（UNKNOWN）静默当成空列表（EMPTY）
   → 假确认「ENTRY 全部清零」。
   v4 测试 E1-负向 引用本文件，断言 v3 在 fetch_open_orders 返回 None 时
   仍返回 True（假确认复现）。若此对照不再返回 True，说明档案被改坏。

来源：送审文档 v3 改动 2（2026-08-30 被 v4 重写前的最后版本）。
"""


def _cancel_and_verify_entry_orders(self, symbol: str, batch_id: str,
                                    b_data: dict, last_filled_count: int) -> bool:
    """C 修复（平仓事务顺序 + ChatGPT 条件 4）：平仓成功后撤未成交 ENTRY 并做交易所侧校验。"""
    entry_orders = b_data.get('entry_orders', []) or []
    pending_ids = [oid for idx, oid in enumerate(entry_orders)
                   if idx >= last_filled_count and oid]
    if not pending_ids:
        return True

    for order_id in pending_ids:
        try:
            self._safe_api_call(self.exchange.cancel_order, order_id, symbol, params={'stop': True})
            print(f"  └─ 已撤销开仓挂单: {order_id}")
        except Exception as e:
            if '-2011' in str(e) or 'Unknown order' in str(e):
                print(f"  └─ 开仓挂单 {order_id} 已不存在（视为已撤）")
            else:
                print(f"  └─ ⚠️ 撤销开仓挂单失败: {order_id} ({e})")

    # 交易所侧校验：重新查询条件单，确认本批次 ENTRY 已全部消失
    try:
        remaining = self._safe_api_call(
            self.exchange.fetch_open_orders, symbol, params={'stop': True}) or []
        remaining_ids = {str(o.get('id')) for o in remaining if isinstance(o, dict)}
        still_alive = [oid for oid in pending_ids if str(oid) in remaining_ids]
    except Exception as e:
        print(f"  └─ ⚠️ 撤单后交易所校验失败（无法确认 ENTRY 已清零）: {e}")
        self.send_tg_notification(
            f"🚨【资金安全】平仓成功后 ENTRY 撤单校验失败！\n"
            f"🆔 批次: {batch_id}\n💡 原因: {str(e)[:150]}\n"
            f"⚠️ 无法确认残留 ENTRY 是否已清零，请立即人工核对！",
            level='critical')
        return False

    if still_alive:
        print(f"  └─ 🚨 撤单后交易所仍存在 ENTRY: {still_alive}")
        self.send_tg_notification(
            f"🚨【资金安全】平仓成功后仍有未撤销的开仓条件单！\n"
            f"🆔 批次: {batch_id}\n📌 残留订单: {still_alive}\n"
            f"⚠️ 这些挂单成交后将形成无保护仓位，请立即人工处理！",
            level='critical')
        return False

    print(f"  └─ ✅ ENTRY 撤单已交易所侧校验通过（{len(pending_ids)} 个全部清零）")
    return True
