        close_order_placed = False    # 订单已创建（仅此而已）
        close_position_confirmed = False  # 仓位已真实减少（交易所侧事实）
        # ⚠️ close_op_id 由改动 3v5 的 atomic BEGIN 提供（锁内 uuid4 生成 + claim +
        # 落盘），**不在这里生成**。
        # v4 把它放在本段是明确的 integration bug：生产真实顺序是
        # close_phase=1 落盘（L6983）在前、本段（L7003）在后 → 按 v4 拼起来
        # NameError。BEGIN 让"生成 + claim + 落盘"成为同一个原子步骤。
        try:
            # 🆕 平仓确认·第 1 步：平仓【前】取本方向敞口基数。
            # 用途仅两件事：① `_close_amount_guard` 的归因判断 ② 成交后的二级
            # 交叉校验。**不是放行判据**（放行判据 = fetch_order 按单归因）。
            pos_before = self._read_position_amt(
                target_symbol, side, target_b_data.get('is_hedge_mode', False))
            if pos_before is None:
                # B-09（你已批准）：Fail-Closed 不发单 + critical。
                # 阻断的是【自动平仓】，不是【人工平仓】；不发单不造成资金损失。
                self.send_tg_notification(
                    f"🚨【资金安全】市价平仓中止：无法读取实际持仓敞口，"
                    f"无法确定安全平仓数量（Fail-Closed，未发单）。\n"
                    f"🆔 批次: `{batch_id}`\n⚠️ 请人工在交易所核对并平仓！",
                    level='critical')
                raise RuntimeError("平仓前读取持仓敞口失败（Fail-Closed：不发出平仓单）")

            # 🔥 归因守卫（§一 v5 修正后）：sum_all **含本批次**，
            # 因此"target 已 close_phase=1 后被自己排除"的退化不再可能。
            close_amount, _amt_detail = self._close_amount_guard(
                target_symbol, side, target_b_data.get('is_hedge_mode', False),
                current_filled_amount, batch_id)
            if not close_amount:
                # 归因冲突 / 读取失败 / 同方向在途：绝不猜归属，转人工 reconcile
                self.send_tg_notification(
                    f"🚨【资金安全】市价平仓中止（归因守卫）：{_amt_detail}\n"
                    f"🆔 批次: `{batch_id}`\n"
                    f"⚠️ 账本与交易所可能已漂移，请先 reconcile 再人工处置！",
                    level='critical')
                raise RuntimeError(f"平仓数量守卫拦截（{_amt_detail}）")
            print(f"  └─ {_amt_detail}")

            # 🔥 修复漏洞1：先市价平仓，成功后再撤 SL/TP
            close_side = 'sell' if side == 'BUY' else 'buy'
            # 🔥 A 修复（2026-08-29 -4061 事故）：与限价平仓（L7578-7582）共用
            # params_base 派生；双向持仓 → positionSide，单向 → reduceOnly，
            # 不同时塞两个参数。
            order_params = target_b_data['params_base'].copy()
            if target_b_data.get('is_hedge_mode', False):
                order_params['positionSide'] = 'LONG' if side == 'BUY' else 'SHORT'
            else:
                order_params['reduceOnly'] = True

            order = self._safe_api_call(
                self.exchange.create_order,
                symbol=target_symbol,
                type='MARKET',
                side=close_side,
                amount=close_amount,
                params=order_params,
                retries=1
            )
            # ⚠️ 铁律（§一）：仅表示【订单已创建】，置 True 后**绝不改回 False**。
            # 回滚资格由六态判据决定，不再操作本标志。
            close_order_placed = True

            close_order_id = order.get('id') if isinstance(order, dict) else None
            if not close_order_id:
                # 拿不到 id 就无法按单归因 → 绝不放行撤 SL/TP（UNKNOWN 处置）
                raise RuntimeError("平仓单已提交但未返回订单 ID，无法按单确认成交")

            # 🆕 平仓确认·第 2 步（六态）：fetch_order(order_id) 按单归因。
            #   CONFIRMED_FULL / TERMINAL_ZERO / PARTIAL / PENDING / UNKNOWN /
            #   NOT_CONFIRMED —— 只有前两者改变流程走向，其余一律不回滚。
            _verdict, _detail, _filled = self._confirm_close_filled(
                target_symbol, side, target_b_data.get('is_hedge_mode', False),
                close_order_id, close_amount, pos_before)

            if _verdict == 'CONFIRMED_FULL':
                close_position_confirmed = True
                # （§五）：结算数量以确认后的成交事实为准，不再用台账名义量
                confirmed_filled_amount = float(_filled or close_amount)
            elif _verdict == 'TERMINAL_ZERO':
                # 唯一可回滚状态（canceled/expired/rejected + **权威 filled 明确
                # 存在且 == 0**）。回滚 = close_op_id CAS 原子操作（改动 3v5-4），
                # 不再碰 close_order_placed，也不再依赖锁外旧快照。
                _rb_ok, _rb_why = self._rollback_close_request_if_current(
                    target_symbol, batch_id, close_op_id)
                if _rb_ok:
                    print(f"  └─ 🔄 平仓单未成交，已原子回滚（{_rb_why}），"
                          f"批次回 ACTIVE，SL/TP 继续在位保护")
                    self.send_tg_notification(
                        f"ℹ️ [程序撤单] 市价平仓单未成交（{_detail}），"
                        f"已原子回滚，批次回 ACTIVE。\n🆔 批次: `{batch_id}`")
                    # 直接 return：不进 except（那里会因 close_order_placed=True
                    # 走不回滚+critical——但本分支回滚已成功，无需双报）
                    return False, f"❌ 市价平仓未成交（已回滚）: {_detail}"
                # CAS 拒绝 = 状态已被其他操作接管 → 绝不强行覆盖，走 critical
                raise RuntimeError(
                    f"平仓单未成交且回滚被拒绝（{_rb_why}），转人工处置")
            else:
                # PARTIAL / PENDING / UNKNOWN / NOT_CONFIRMED —— 一律**不回滚**：
                #   PARTIAL       仓位已真实变化，回滚 = 伪装"没平过"
                #   PENDING       订单还活着，回滚后它再成交就无人管辖
                #   UNKNOWN       查询失败 / filled 不可判定（UNKNOWN ≠ EMPTY）
                #   NOT_CONFIRMED create 有 ID 但 fetch 不到 ≠ 没成交
                # → 保持 close_phase=1 + close_reason='market_confirm_unknown'，
                #   由冻结告警（改动 4）+ 本函数 except 的 critical 通道兜住。
                try:
                    self.save_batch_state(target_symbol, batch_id,
                                          {'close_reason': 'market_confirm_unknown'})
                except Exception:
                    pass
                raise RuntimeError(
                    f"市价平仓单结果未确认（{_verdict}）：{_detail}。"
                    f"不回滚，保持冻结等人工处置")

            # 仓位已按单确认成交 — 现在才安全撤销保护单
            if target_b_data.get('tp_order_id'):
                try:
                    self._safe_api_call(self.exchange.cancel_order, target_b_data['tp_order_id'], target_symbol,
                                        params={'stop': True})
                    print(f"  └─ 已撤销止盈单: {target_b_data['tp_order_id']}")
                except Exception:
                    pass

            if target_b_data.get('current_sl_id'):
                try:
                    self._safe_api_call(self.exchange.cancel_order, target_b_data['current_sl_id'], target_symbol,
                                        params={'stop': True})
                    print(f"  └─ 已撤销止损单: {target_b_data['current_sl_id']}")
                except Exception:
                    pass

            # 🔥 C 修复（平仓事务顺序）：撤未成交 ENTRY 从「平仓前」移到「按单确认成交后」。
            # 🔥 v5（§四）：**返回值必须成为 clear gate** —— 忽略返回值的话，
            # helper 正确识别出的 UNKNOWN 会被 legacy converge 的
            # `fetch_open_orders(...) or []` 从后门变回 EMPTY → 继续生成 proof → clear。
            _entries_ok = self._cancel_and_verify_entry_orders(
                target_symbol, batch_id, target_b_data, last_filled_count)
            if not _entries_ok:
                raise RuntimeError(
                    "持仓已平，但 ENTRY 收敛未确认；保持批次关闭态，禁止进入 clear")

        except Exception as e:
            # P0 Batch A（回滚收紧）：平仓单已创建成功后的异常 = 结算/簿记失败，
            # 绝不回滚 close_phase/flags——否则"平仓后失败误回滚"会让冻结解除、
            # 保护单复活。
            # ⚠️ v5（§七）：本块是 L7115-7143 的**完整原文 + CAS 替换**，
            # 不再使用 `...` 占位（占位块不得声称"可直接套用"）。
            if close_order_placed:
                self.send_tg_notification(
                    f"🚨【资金安全】市价平仓单已发出但后续结算异常（未回滚关闭标记）！\n"
                    f"🆔 批次: {batch_id}\n💡 原因: {str(e)[:150]}\n"
                    f"⚠️ 请立即人工核对持仓与挂单！",
                    level='critical')
                return False, f"❌ 市价平仓结算异常（平仓单已创建，close_phase 保持）: {e}"
            # 🔥 修复漏洞1b：失败回滚 —— 改为 CAS 原子回滚（§二）
            try:
                _rb_ok, _rb_why = self._rollback_close_request_if_current(
                    target_symbol, batch_id, close_op_id)
            except Exception as _rb_err:
                _rb_ok, _rb_why = False, f'CAS 调用异常（{_rb_err}）'
            if _rb_ok:
                print(f"  └─ 🔄 平仓失败回滚：CAS 原子回滚成功（{_rb_why}），"
                      f"已清除 is_programmatic_cancel/pending_close/close_phase，监控线程恢复保护")
            else:
                print(f"  └─ ⚠️ 回滚被拒绝: {_rb_why}（状态已被其他操作接管，需人工检查）")
                self.send_tg_notification(
                    f"🚨【资金安全】市价平仓失败且回滚被拒绝！\n批次: `{batch_id}`\n"
                    f"原因: {_rb_why}\n请立即检查仓位是否仍有 SL 保护！",
                    level='critical')
            return False, f"❌ 市价平仓失败: {e}"
