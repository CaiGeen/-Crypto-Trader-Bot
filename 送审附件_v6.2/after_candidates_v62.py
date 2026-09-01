# -*- coding: utf-8 -*-
"""v6.2 生产方法候选实现（GREEN 阶段 SUT）。

对应 `v6.2_正式diff_送审ChatGPT.md` 改动 1/2/3/4/5/6.5/9.0/9.0b 的候选 AFTER，
以可测函数形态落地（正式落地生产时按落点表回填 trader_260725.py；
**生产三文件零改动**）。R1-h 的 monitor hole 检测来自 v2.2 §Δ4 已批准规格。

依赖（由测试 FakeSelf62 绑定提供）：
  v6.2 helper 层（new_helpers_v62.py）+ 生产 ast 提取的
  _find_registry_identity_by_order_id + Fake 的 _update_registry/save_batch_state。
"""


# ── 改动 1：cancel_open_orders（producer #1）─────────────────────

def cancel_open_orders_v62(self, batch_id: str):
    """取消指定批次的所有未成交开仓条件单（v6.2 正式 diff 改动 1 候选 AFTER）。"""
    all_states = self.load_all_states()
    target_symbol = None
    target_b_data = None

    for symbol, symbol_batches in all_states.items():
        if batch_id in symbol_batches and symbol_batches[batch_id].get('is_active'):
            target_symbol = symbol
            target_b_data = symbol_batches[batch_id]
            break

    if not target_b_data:
        return False, f"❌ 未找到处于活跃状态的批次号 `{batch_id}`"

    entry_orders = target_b_data.get('entry_orders', [])
    last_filled_count = target_b_data.get('last_filled_count', 0)
    # 🔒 v6.2（D-4 语义降级）：pending_count 仅表示「待撤尝试区大小」，
    # 不再表示「仍有 pending 单」——entry_orders 永不压缩后，尾段可能
    # 全是已终态 ID（重撤幂等，-2011 由 verifier 吸收）。
    pending_count = len(entry_orders) - last_filled_count

    if pending_count <= 0:
        return False, f"ℹ️ 批次 `{batch_id}` 没有未成交的挂单"

    cancel_requested_ids = []
    requested_layers = []
    unresolved_ids = []
    already_terminal_ids = []
    # 🔒 v6.2：三套统计严格分离（动作 ≠ 事实 ≠ 归因）：
    #   cancel_requested_ids  = cancel 调用成功返回（动作统计，仅日志/文案）
    #   programmatic_gone_ids = cancel 成功 + verifier=gone → 可写 registry 归因
    #   unresolved_ids        = verifier 非 gone（未确认终态，Fail-Closed 源）
    #   already_terminal_ids  = cancel 异常但 verifier=gone（事实 gone，
    #                           但无法证明是本程序终结 → 不写归因）
    programmatic_gone_ids = []

    # 🔒 v6.2-r6（P1）：已知程序终态 ENTRY 不进「待撤尝试区」。
    #   `PROGRAMMATIC_CANCELED` = 此前一次「cancel 成功 + 按 ID verifier=gone」
    #   持久化下来的事实。重复按 🗑️ 时若仍去 cancel/verify 这些历史已撤单，
    #   Binance 返回 -2011/OrderNotFound → verifier 判 unknown →
    #   unresolved_ids 非空 → 第二次 🗑️ 被报成「部分失败/失败」的假失败。
    #   只豁免 PROGRAMMATIC_CANCELED（不豁免 ABSENT / FAILED）。
    _known_terminal = set()
    for _ident, _e in (target_b_data.get('protection_registry') or {}).items():
        if not isinstance(_e, dict) or _e.get('role') != 'ENTRY':
            continue
        if _e.get('state') != 'PROGRAMMATIC_CANCELED':
            continue
        _oid = _e.get('order_id')
        if _oid:
            _known_terminal.add(str(_oid))

    # 🔒 v6.2（INV-3a）：从最高层往最低层撤 + 遇阻即停。
    # canceled 层恒在所有 active 层之上 → 成交位保持前缀连续，
    # 不主动制造 hole（last_filled_count 的 prefix 假设才成立）。
    for idx in reversed(range(last_filled_count, len(entry_orders))):
        order_id = entry_orders[idx]
        if str(order_id) in _known_terminal:
            # 已确认程序终结：不撤、不验证、不计入任何失败统计
            print(f"  └─ ℹ️ 第 {idx + 1} 层已确认程序终结"
                  f"（registry=PROGRAMMATIC_CANCELED），跳过")
            continue
        _cancel_ok = False
        try:
            self._safe_api_call(self.exchange.cancel_order, order_id, target_symbol,
                                params={'stop': True})
            _cancel_ok = True
            cancel_requested_ids.append(order_id)
            requested_layers.append(idx + 1)
            print(f"  └─ 已请求撤销第 {idx + 1} 层挂单: {order_id}")
        except Exception as e:
            # 🔒 v6.2：**所有** cancel 异常（含 -2011/网络失败）一律交给
            # verifier 定案——不区分异常类型、不在 verifier 前下结论。
            print(f"  └─ ⚠️ 撤销第 {idx + 1} 层挂单请求异常: {order_id} ({e})，"
                  f"交由逐 ID 验证定案")
        # 🔒 v6.2（INV-1v2）：每层统一 verifier——
        # cancel 成功返回 ≠ terminal 事实（_safe_api_call 只透传底层结果）。
        verdict, _vo = self._verify_entry_order_terminal(order_id, target_symbol)
        if verdict == 'gone':
            if _cancel_ok:
                programmatic_gone_ids.append(order_id)
            else:
                already_terminal_ids.append(order_id)
            continue
        if verdict == 'filled':
            self.send_tg_notification(
                f"🚨【资金安全】ENTRY 在撤单前已成交！\n"
                f"🆔 批次: `{batch_id}`\n📌 订单: {order_id}\n"
                f"⚠️ 高层成交 → 成交位可能已不连续（hole），"
                f"已停止向更低层撤单，请立即人工核对持仓与台账！",
                level='critical')
        unresolved_ids.append(order_id)
        break  # filled / open / unknown 一律停止（不制造 hole）

    if not programmatic_gone_ids and not already_terminal_ids and unresolved_ids:
        # 全部未确认终态：台账原样，批次保持现状，监控继续管辖这些层
        return False, (f"⚠️ 批次 `{batch_id}` 挂单全部未确认终态"
                       f"（{len(unresolved_ids)} 张，ID 已保留），"
                       f"请重试或人工核对")

    # 🔒 v6.2（ΔE1）：归因 order-ID scoped——不写 batch-global sticky
    # is_programmatic_cancel（棘轮字段，且会永久关闭 SL/TP 自动补挂）。
    for order_id in programmatic_gone_ids:
        _ident = self._find_registry_identity_by_order_id(target_symbol, batch_id,
                                                          order_id)
        if _ident:
            self._update_registry(target_symbol, batch_id, _ident,
                                  state='PROGRAMMATIC_CANCELED',
                                  order_id=order_id, id_known=True,
                                  terminated_reason='cancel_open_orders')
        else:
            print(f"  └─ ⚠️ 撤单归因：{order_id} 在 registry 中无 identity"
                  f"（撤单事实已完成，归因降级为 manual 语义）")

    # 🔒 v6.2（D-4）：entry_orders 作为 positional/audit ledger 永不压缩——
    # 已终态的 ID 一并留在原位，层号零漂移；terminal 与 pending 状态
    # 不再由 list 长度推断。
    target_b_data['entry_orders'] = list(entry_orders)

    pending_sl = target_b_data.get('pending_sl_orders', [])
    pending_sl = [i for i in pending_sl if i < last_filled_count]
    target_b_data['pending_sl_orders'] = pending_sl

    current_持仓 = sum(target_b_data.get('target_amounts', [])[:last_filled_count])

    if last_filled_count > 0:
        self.save_batch_state(target_symbol, batch_id, target_b_data)
        result_msg = (
            f"🗑️ **撤单完成**\n\n"
            f"🆔 批次：`{batch_id}`\n"
            f"🪙 标的：`{target_symbol}`\n"
            f"📊 本轮待撤尝试：{pending_count} 层\n"
            f"├─ 已确认终态：{len(programmatic_gone_ids) + len(already_terminal_ids)} 张\n"
            f"├─ 未确认终态：{len(unresolved_ids)} 张（ID 已保留）\n"
            f"📊 当前持仓：{current_持仓}\n\n"
            f"💡 {last_filled_count} 层已成交，止盈止损单已保留，监控继续运行"
        )
    else:
        # 🔒 v6.2：zero-filled 终止标志的前置 = 逐 ID terminal 确认。
        # 旧代码无条件 entry_orders=[] + pending_close/close_phase=1，
        # 而 monitor 只看磁盘标志 → 活 ENTRY 失去管辖。
        unresolved = []
        _filled_found = False
        for order_id in entry_orders:
            if str(order_id) in _known_terminal:
                # 已确认程序终结：再 verify 只会 OrderNotFound→unknown，永不收敛
                continue
            verdict, _vo = self._verify_entry_order_terminal(order_id, target_symbol)
            if verdict == 'gone':
                continue
            unresolved.append((order_id, verdict))
            if verdict == 'filled':
                _filled_found = True
        if _filled_found:
            self.send_tg_notification(
                f"🚨【资金安全】撤单确认期间发现 ENTRY 已成交！\n"
                f"🆔 批次: `{batch_id}`\n"
                f"⚠️ 批次不再是无持仓状态，已保持 ACTIVE 由监控接管，"
                f"请立即人工核对持仓！",
                level='critical')
        if unresolved:
            # 绝不写 pending_close/close_phase/is_programmatic_cancel
            self.save_batch_state(target_symbol, batch_id, target_b_data)
            return False, (f"⚠️ 批次 `{batch_id}` 撤单后仍有 "
                           f"{len(unresolved)} 张 ENTRY 未确认终态"
                           f"（{[u[0] for u in unresolved]}，ID 已保留，监控继续）\n"
                           f"💡 请重试或人工核对")
        # 全部 gone → 才获得终止资格
        target_b_data['pending_sl_orders'] = []
        target_b_data['pending_close'] = True
        target_b_data['close_phase'] = 1
        self.save_batch_state(target_symbol, batch_id, target_b_data)

        result_msg = (
            f"🗑️ **撤单完成**\n\n"
            f"🆔 批次：`{batch_id}`\n"
            f"🪙 标的：`{target_symbol}`\n"
            f"📊 已确认终态：{len(entry_orders)} 张\n"
            f"📊 当前持仓：0\n\n"
            f"💡 批次已无成交层，监控将自然退出"
        )

    if unresolved_ids:
        # 🔒 v6.2（D-3 裁定）：部分失败就是失败（统计口径 = 事实，非动作）
        return False, (f"⚠️ 批次 `{batch_id}` 撤单部分完成："
                       f"{len(programmatic_gone_ids) + len(already_terminal_ids)} 张已确认终态，"
                       f"{len(unresolved_ids)} 张未确认终态（{unresolved_ids}，"
                       f"ID 已完整保留，监控继续）\n💡 请重试或人工核对")

    return True, result_msg


# ── 改动 3：monitor zero-filled 退出判据只看 pending_close ───────

def monitor_zero_filled_exit_v62(disk_batch, batch_filled_count):
    """生产 L4858-4865 候选 AFTER（返回 True = break 退出）。

    🔒 v6.2：退出资格只看 pending_close。is_programmatic_cancel 是
    False→True 棘轮（L1627-1629），pop+save 清不掉；它已彻底退出归因
    用途（权威归因 = protection_registry 的 order-ID 事实），仅作为
    legacy 辅助字段存在，不得再参与任何生命周期或订单事件判断。"""
    if batch_filled_count == 0:
        if disk_batch and disk_batch.get('pending_close', False):
            return True
    return False


# ── 改动 4：monitor ENTRY canceled 检测归因 order-scoped ─────────

def entry_detection_v62(self, symbol, batch_id, latest_b_data_check, order_id, idx):
    """生产 L4749-4763 候选 AFTER（返回 manual_canceled_detected）。"""
    _en_ident = self._find_registry_identity_by_order_id(symbol, batch_id, order_id)
    _en_reg = ((latest_b_data_check.get('protection_registry') or {})
               .get(_en_ident) if _en_ident else None)
    _en_prog = (isinstance(_en_reg, dict)
                and _en_reg.get('state') == 'PROGRAMMATIC_CANCELED')
    if _en_prog:
        print(f"ℹ️ [程序撤单] 第 {idx + 1} 层开仓条件单已由程序终结 (ID: {order_id})")
        return False    # 不设置 manual_canceled_detected
    print(f"⚠️ 🛑 [手动撤单提醒] 第 {idx + 1} 层开仓条件单被撤销 (ID: {order_id})")
    self.send_tg_notification(
        f"⚠️ 🛑 **[撤单提醒]** 批次 `{batch_id}` 第 {idx + 1} 层条件单已被手动撤销/失效。"
    )
    return True
    # registry 无条目 → 按 manual 语义提醒（Fail-Noisy：宁可误报也不静默）


# ── 改动 5：SL / TP 消费者归因 order-scoped（state-only）─────────

def sl_attribution_v62(self, symbol, batch_id, latest_b_data_check,
                       current_sl_id, user_modified):
    """生产 L5324-5337 候选 AFTER（返回 need_recover_sl）。

    🔒 v6.2：归因 order-ID scoped。batch-global sticky bool 会因一次 🗑️
    永久为 True，导致 SL 被外部撤销时永不补挂。授权只认 registry state
    （与 _assert_create_allowed 同规范），不解析 terminated_reason 字符串。"""
    _sl_ident = self._find_registry_identity_by_order_id(symbol, batch_id, current_sl_id)
    _sl_reg = ((latest_b_data_check.get('protection_registry') or {})
               .get(_sl_ident) if _sl_ident else None)
    _sl_prog = (isinstance(_sl_reg, dict)
                and _sl_reg.get('state') == 'PROGRAMMATIC_CANCELED')
    if _sl_prog:
        print(f"ℹ️ [程序撤单] 批次 {batch_id} 止损单已由程序终结 (ID: {current_sl_id})")
        return False
    if user_modified:
        print(f"ℹ️ [用户主动修改] 批次 {batch_id} 止损单已被用户撤销，不再自动补挂")
        return False
    print(f"⚠️ ⚠️ [风控异常] 止损单已在外部撤销，准备按策略自动补挂...")
    return True


def tp_attribution_v62(self, symbol, batch_id, latest_b_data_check,
                       tp_order_id, user_modified):
    """生产 L5465-5478 候选 AFTER（返回 need_recover_tp，与 SL 完全对称）。"""
    _tp_ident = self._find_registry_identity_by_order_id(symbol, batch_id, tp_order_id)
    _tp_reg = ((latest_b_data_check.get('protection_registry') or {})
               .get(_tp_ident) if _tp_ident else None)
    _tp_prog = (isinstance(_tp_reg, dict)
                and _tp_reg.get('state') == 'PROGRAMMATIC_CANCELED')
    if _tp_prog:
        print(f"ℹ️ [程序撤单] 批次 {batch_id} 止盈单已由程序终结 (ID: {tp_order_id})")
        return False
    if user_modified:
        print(f"ℹ️ [用户主动修改] 批次 {batch_id} 止盈单已被用户撤销，不再自动补挂")
        return False
    print(f"⚠️ ⚠️ [风控异常] 止盈单已在外部撤销，准备按策略自动补挂...")
    return True


# ── 改动 2：monitor 手动撤单路径（producer #2）───────────────────

def producer2_v62(self, symbol, batch_id, entry_orders, filled_layers,
                  canceled_layers, batch_filled_count):
    """生产 L4826-4846 候选 AFTER（返回 cancelled_count）。

    🔒 v6.2：高→低 + 每层 verifier + 遇阻即停；canceled_layers 只在
    verifier=gone 后置位（否则 filled 层被 L4668-4669 永久挡在成交识别外）；
    台账 entry_orders 原样保留（删除旧 L4842-4843 截断）；不写 sticky flag；
    归因按单落 registry。"""
    cancelled_count = 0
    programmatic_gone_this_round = []
    for idx in reversed(range(len(entry_orders))):
        if filled_layers[idx] or canceled_layers[idx]:
            continue
        order_id = entry_orders[idx]
        _cancel_ok = False
        try:
            self._safe_api_call(self.exchange.cancel_order, order_id, symbol,
                                params={'stop': True})
            _cancel_ok = True
            print(f"  └─ 已请求撤销第 {idx + 1} 层挂单: {order_id}")
        except Exception as e:
            # 所有异常一律交 verifier 定案（同 producer #1）
            print(f"  └─ ⚠️ 撤销第 {idx + 1} 层挂单请求异常: {order_id} ({e})，"
                  f"交由逐 ID 验证定案")
        verdict, _vo = self._verify_entry_order_terminal(order_id, symbol)
        if verdict == 'gone':
            # bitmap 只在「交易所确认 gone」之后才置位
            canceled_layers[idx] = True
            cancelled_count += 1
            if _cancel_ok:
                programmatic_gone_this_round.append(order_id)
            continue
        if verdict == 'filled':
            self.send_tg_notification(
                f"🚨【资金安全】ENTRY 在撤单前已成交！\n"
                f"🆔 批次: {batch_id}\n📌 订单: {order_id}\n"
                f"⚠️ 已停止向更低层撤单；该层保持未标记状态，"
                f"下一轮监控将按成交事实入账，请立即人工核对持仓与台账！",
                level='critical')
        break

    # 台账 entry_orders 原样保留（不截断、不写 sticky flag）
    latest_all = self.load_all_states()
    latest_b_data = latest_all.get(symbol, {}).get(batch_id, {})
    if latest_b_data:
        self.save_batch_state(symbol, batch_id, latest_b_data)
    for _oid in programmatic_gone_this_round:
        _ident = self._find_registry_identity_by_order_id(symbol, batch_id, _oid)
        if _ident:
            self._update_registry(symbol, batch_id, _ident,
                                  state='PROGRAMMATIC_CANCELED',
                                  order_id=_oid, id_known=True,
                                  terminated_reason='monitor_cancel_remaining_entries')
    return cancelled_count


# ── 改动 6.5：outer except pre-create rollback 失败统一 CAS ──────

def outer_except_precreate_v62(self, target_symbol, batch_id, close_op_id, err):
    """送审稿 L622-650 候选 AFTER（close_order_placed=False 分支）。

    🔒 v6.2（INV-2）：任何 close_order_placed=False 异常 → 尝试 CAS rollback
    → 成功回 ACTIVE / 失败必须 CAS 写 txn_aborted_rollback_failed + critical
    （rollback 失败只发 critical 会让 reason 停在正常态 → 冻结告警静默）。"""
    try:
        _rb_ok, _rb_why = self._rollback_close_request_if_current(
            target_symbol, batch_id, close_op_id)
    except Exception as _rb_err:
        _rb_ok, _rb_why = False, f'CAS 调用异常（{_rb_err}）'
    if _rb_ok:
        print(f"  └─ 🔄 平仓失败回滚：CAS 原子回滚成功（{_rb_why}）")
    else:
        _rs_ok, _rs_why = self._set_close_reason_if_current(
            target_symbol, batch_id, close_op_id, 'txn_aborted_rollback_failed')
        self.send_tg_notification(
            f"🚨【资金安全】市价平仓失败且回滚被拒绝！\n批次: `{batch_id}`\n"
            f"原因: {_rb_why}\n"
            + ('' if _rs_ok else
               f"⚠️ close_reason 切换失败（{_rs_why}），"
               "冻结告警可能静默，请立即人工处置！\n")
            + f"请立即检查仓位是否仍有 SL 保护！",
            level='critical')
    return False, f"❌ 市价平仓失败: {err}"


# ── 改动 9.0 / 9.0b：限价 TP factual gate + coverage guard ───────

def limit_tp_gate_v62(self, target_symbol, batch_id, close_op_id,
                            target_b_data, current_filled_amount):
    """改动 9.0 的完整版：gate 判定 + 阻断时的 CAS/critical（返回 (proceed, verdict)）。"""
    _tp_old_id = target_b_data.get('tp_order_id')
    if not _tp_old_id:
        return True, 'no_tp'
    try:
        self._safe_api_call(self.exchange.cancel_order, _tp_old_id, target_symbol,
                            params={'stop': True})
    except Exception as _tp_cancel_e:
        print(f"  └─ ⚠️ 撤销旧止盈单请求异常: {_tp_old_id} ({_tp_cancel_e})，"
              f"事实由六态确认器定案")
    _tp_verdict, _tp_detail, _tp_filled = self._confirm_close_filled(
        target_symbol, target_b_data.get('side'),
        target_b_data.get('is_hedge_mode', False),
        _tp_old_id, current_filled_amount, current_filled_amount,
        order_kind='conditional')
    if _tp_verdict == 'TERMINAL_ZERO':
        return True, 'TERMINAL_ZERO'
    _rs_ok, _rs_why = self._set_close_reason_if_current(
        target_symbol, batch_id, close_op_id, 'limit_tp_unresolved')
    self.send_tg_notification(
        f"🚨【资金安全】限价平仓中止：旧止盈单 {_tp_old_id} 未确认安全终结！\n"
        f"🆔 批次: `{batch_id}`\n"
        f"📌 六态判定 = {_tp_verdict}（{_tp_detail}）\n"
        f"⚠️ TP 可能仍在场或已触发成交——此时挂 LIMIT 会减到同方向"
        f" aggregate 敞口（错平其他批次）\n"
        f"🚫 未发出任何平仓单，批次冻结待人工处置\n"
        + ('' if _rs_ok else
           f"⚠️ close_reason 切换失败（{_rs_why}），冻结告警可能静默\n")
        + f"💡 请人工核对该 TP 与当前持仓后再决定处置",
        level='critical')
    return False, _tp_verdict


def limit_coverage_gate_v62(self, target_symbol, batch_id, close_op_id,
                            target_b_data, current_filled_amount):
    """正式 diff 改动 9.0b 候选 AFTER：create 紧前 coverage guard。

    🔒 v6.2（P0）：LIMIT 与 MARKET 同等 coverage 安全等级——复用
    _close_amount_guard（多批次 actual < 台账合计 → None）；LIMIT 本轮
    不做 partial-PnL 传播，safe_amount != 台账量也一律 Fail-Closed。
    锁外调用（guard 内部有 fetch_positions；_state_lock 非重入 +
    锁内零交易所 API 铁律），create 紧前执行。"""
    safe_amount, _amt_detail = self._close_amount_guard(
        target_symbol,
        target_b_data.get('side'),
        target_b_data.get('is_hedge_mode', False),
        current_filled_amount,
        batch_id,
    )
    if safe_amount is None or abs(safe_amount - current_filled_amount) > 1e-8:
        _rs_ok, _rs_why = self._set_close_reason_if_current(
            target_symbol, batch_id, close_op_id, 'limit_amount_conflict')
        self.send_tg_notification(
            f"🚨【资金安全】限价平仓中止：平仓数量与 aggregate 敞口冲突！\n"
            f"🆔 批次: `{batch_id}`\n"
            f"📌 {_amt_detail}\n"
            f"📌 台账量 {current_filled_amount}，guard 判定 {safe_amount}\n"
            f"⚠️ 此时挂 LIMIT 可能减到属于其他批次的剩余敞口（错平）\n"
            f"🚫 未发出任何平仓单，批次冻结待人工 reconcile\n"
            + ('' if _rs_ok else
               f"⚠️ close_reason 切换失败（{_rs_why}），冻结告警可能静默\n"),
            level='critical')
        return False, safe_amount
    return True, safe_amount


# ── R1-h：monitor hole 检测（v2.2 §Δ4 已批准规格的候选实现）──────

def monitor_hole_check_v62(self, batch_id, filled_layers, batch_filled_count):
    """成交位不连续（hole）检测：prefix 假设失效必须 critical 暴露。

    观测层（_derive_close_txn_vars 的 hole 硬门是执行层），两者同时存在。
    返回发出的 hole critical 数。"""
    _hole_idx = [i for i in range(min(batch_filled_count, len(filled_layers)))
                 if not filled_layers[i]]
    if not _hole_idx:
        return 0
    self.send_tg_notification(
        f"🚨【资金安全】批次成交位不连续（hole）！\n"
        f"🆔 批次: {batch_id}\n"
        f"📌 已成交 {batch_filled_count} 层，但层位 {_hole_idx} 未成交\n"
        f"⚠️ 按层数平仓/结算的数量口径可能失真，请人工核对持仓与台账！",
        level='critical')
    return 1
