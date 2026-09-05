import time

import ccxt


class _Holder:
    # ══════════════════════════════════════════════════════════════════
    # 2026-08-29 -4061 事故 · v4（ChatGPT 对 v3 的终审：方向批准、diff 不批准）
    #
    # v4 四个 P0 收敛（对应 ChatGPT 终审 §一/§二/§三/§四）：
    #   P0-1  not_filled 状态过宽 → 拆六态，create 返回 ID 后绝不回滚
    #         open/partial/not_found（仅 TERMINAL_ZERO 有回滚资格）
    #   P0-2  stale snapshot 回滚 → _rollback_close_request_if_current：
    #         _state_lock 内重读磁盘 + close_op_id CAS（复用 G3b 范式 L3463）
    #   P0-3  ENTRY 校验 None→[] → 显式拦截 + 逐 ID fetch_order 终态确认
    #   P0-4  min(台账,总敞口) 归因 → 同方向多活跃批次时 Fail-Closed 不自动平
    #
    # 同时撤销：allow_flag_rollback escape hatch 与 AST 守卫（随 P0-2 的
    # 专用原子操作一起，防扩散问题整体消失——ChatGPT 终审 §二建议，采纳）。
    # ══════════════════════════════════════════════════════════════════

    # ── 判据原语（v3 保留，未改动）──────────────────────────────────

    def _read_position_amt(self, symbol: str, side: str, is_hedge_mode: bool) -> float | None:
        """读取【symbol + 持仓方向】的持仓绝对值。

        返回 None = 查询失败（不可判定）→ 调用方必须 Fail-Closed。
        返回 0.0  = 该方向无敞口。

        ⚠️ 读的是 symbol+方向【总敞口】，不是本批次敞口（D-006 同方向最多 3 批）。
          禁止单独用作放行判据——2026-08-29 探针实证（G:/tmp/probe_position_shape.py）：
          side 传错同样返回 0.0，与「已平仓」物理不可区分。
        """
        try:
            positions = self._safe_api_call(self.exchange.fetch_positions, [symbol])
        except Exception as e:
            print(f"  ⚠️ 读取持仓失败: {e}")
            return None
        if positions is None:
            # 非异常的 None 返回同样不可判定，绝不能退化成 0.0（C-1 同型漏洞）
            print("  ⚠️ 读取持仓失败：fetch_positions 返回 None（非异常）")
            return None
        target = 'long' if side == 'BUY' else 'short'
        want_raw = symbol.replace('/', '').split(':')[0]
        total = 0.0
        for pos in positions if isinstance(positions, list) else []:
            info = pos.get('info', {}) or {}
            if pos.get('symbol') != symbol and info.get('symbol') != want_raw:
                continue
            if is_hedge_mode:
                ps = str(pos.get('side') or info.get('positionSide') or '').lower()
                if ps not in (target, 'both'):
                    continue
            try:
                total += abs(float(pos.get('contracts', 0) or pos.get('positionAmt', 0) or 0))
            except (TypeError, ValueError):
                return None
        return total

    def _fetch_close_order_state(self, order_id, symbol, retry_not_found: int = 3,
                                 not_found_delay: float = 2.0):
        """按单查询平仓单，返回 (state, order)。state ∈ {'success','not_found','unknown'}。

        复用 trader_260725.py::_verify_order_created（L3368）的既有三态语义：
          success   → 订单真实存在，order 可用
          not_found → OrderNotFound（重试排除可见性延迟后仍不存在）
          unknown   → 其他异常 → 调用方必须 Fail-Closed（UNKNOWN ≠ EMPTY）

        平仓单走 'normal' 端点（不带 params={'stop': True}）。
        not_found 必须重试后再定案（2026-08-29 事件 3 实证：4/4 单 0 秒 verify
        全部 OrderNotFound 假阴性 → 曾致 12 处误判 24 个孤儿单）。
        """
        try:
            order = self._safe_api_call(self.exchange.fetch_order, order_id, symbol, retries=1)
            return 'success', order
        except ccxt.OrderNotFound:
            for _ in range(retry_not_found):
                time.sleep(not_found_delay)
                try:
                    order = self._safe_api_call(
                        self.exchange.fetch_order, order_id, symbol, retries=1)
                    return 'success', order
                except ccxt.OrderNotFound:
                    continue
                except Exception:
                    return 'unknown', None
            return 'not_found', None
        except Exception:
            return 'unknown', None

    # ── P0-1：六态确认器（v3 的 not_filled 拆开）────────────────────

    def _confirm_close_filled(self, symbol: str, side: str, is_hedge_mode: bool,
                              order_id, expected: float, pos_before: float | None = None,
                              attempts: int = 3, delay: float = 0.6):
        """确认【这张平仓单】的成交事实。返回 (verdict, detail, filled_amount)。

        verdict（v4 六态，ChatGPT 终审 §一 裁定）：
          'CONFIRMED_FULL'  → 完整成交 → 放行撤 SL/TP
          'TERMINAL_ZERO'   → 明确终态(canceled/expired/rejected/closed) 且 filled=0
                              → 唯一有资格回滚的状态
          'PARTIAL'         → filled > 0 但不足 → **绝不回滚**（仓位已真实变化，
                              回滚=把"已部分平掉"伪装成"未平过"）→ 保持保护单
                              + critical 人工接管
          'PENDING'         → new/open/active → **绝不回滚**（订单活着，稍后可能
                              成交；回滚后订单再成交，状态机已回 ACTIVE 却无人管）
                              → critical / 后续确认
          'UNKNOWN'         → 查询异常 → 不回滚 + critical（Fail-Closed）
          'NOT_CONFIRMED'   → create 已返回 ID 但 fetch 查不到（重试后仍 OrderNotFound）
                              → **绝不回滚**。复用 _verify_order_created 的语义：
                              not_found = NOT_CONFIRMED（不 Commit），绝不是
                              "证明订单没成交可以放心反向操作"。
                              v3 曾把它判成 not_filled→回滚，这是对既有三态
                              安全含义的篡改（ChatGPT 终审 §一 最后一点）。

        核心不变量：只要 create_order() 成功返回了有效 order_id，
        close_order_placed=True 就不再改回 False。回滚不再操作这个标志，
        而是通过 _rollback_close_request_if_current 的 close_op_id CAS。

        判据为什么必须是「订单维度」：v2 的 delta（总敞口减少量）无法归因——
        另一批次 SL 成交 / 用户手动平仓 / ADL 都会让总敞口下降 → 假确认 → 裸仓。
        fetch_order 回答「我这张单成交了多少」，天然免疫他方行为。
        delta 已降级为 CONFIRMED_FULL 后的二级交叉校验（仅告警不阻断）。
        """
        if expected is None or expected <= 0:
            return 'UNKNOWN', f"参数不可判定（expected={expected}）", None

        # B-03：有效预期 = min(台账量, pos_before)。台账量可能大于实际剩余
        # （上次部分成交未同步 / 用户手动减仓），若直接用台账量判，仓位真实
        # 归零也永远判不通过 → 永久不可平。
        if pos_before is not None and pos_before > 0:
            eff_expected = min(expected, pos_before)
        else:
            eff_expected = expected
        tol = 1e-8 + abs(eff_expected) * 1e-6
        zero_tol = 1e-12

        n = max(1, attempts)
        last_detail = ''
        for i in range(n):
            state, order = self._fetch_close_order_state(order_id, symbol)

            if state == 'success':
                if not isinstance(order, dict):
                    return 'UNKNOWN', f"订单结构异常（{type(order).__name__}）", None
                status = str(order.get('status') or '').lower()
                try:
                    filled = float(order.get('filled') or 0)
                except (TypeError, ValueError):
                    return 'UNKNOWN', f"filled 字段异常（{order.get('filled')!r}）", None

                if status in ('closed', 'filled'):
                    if filled >= eff_expected - tol:
                        detail = (f"订单 {order_id} 已成交 filled={filled}"
                                  f"（有效预期 {eff_expected}，台账 {expected}），status={status}")
                        # 二级交叉校验（B-01 处置 2）：按单已确认成交，再看敞口是否
                        # 真的相应减少。仅告警不阻断——多批次下其他批次的减仓会让
                        # 这里出现正常的不匹配，阻断会把正常路径卡死。
                        if pos_before is not None:
                            after = self._read_position_amt(symbol, side, is_hedge_mode)
                            if after is not None and (pos_before - after) < eff_expected - tol:
                                print(f"  ⚠️ [交叉校验] 订单已成交但敞口未见相应减少："
                                      f"before={pos_before} after={after} "
                                      f"预期减少>={eff_expected}（多批次下可能正常，请人工留意）")
                        return 'CONFIRMED_FULL', detail, filled
                    if filled > zero_tol:
                        # 部分成交：仓位已真实变化，绝不回滚
                        return 'PARTIAL', (
                            f"订单 {order_id} 部分成交 filled={filled} < 预期 {eff_expected}"
                            f"（status={status}）。仓位已变化，不可回滚；"
                            f"保持保护单，需人工接管剩余 {eff_expected - filled}"), filled
                    # closed 但 filled≈0：罕见，按终态零成交处理
                    return 'TERMINAL_ZERO', (
                        f"订单 {order_id} 终态 status={status} 且 filled=0（预期 {eff_expected}）"), 0.0

                if status in ('canceled', 'expired', 'rejected'):
                    if filled > zero_tol:
                        return 'PARTIAL', (
                            f"订单 {order_id} 终态 {status} 但已成交 filled={filled} > 0"
                            f"（预期 {eff_expected}）。仓位已变化，不可回滚"), filled
                    return 'TERMINAL_ZERO', (
                        f"订单 {order_id} 终态 {status} 且 filled=0（预期 {eff_expected}）"
                        f"——唯一可回滚状态"), 0.0

                if status in ('new', 'open', 'active', 'pending', 'partially_filled'):
                    last_detail = (f"订单 {order_id} 仍在活动中：status={status} "
                                   f"filled={filled}（预期 {eff_expected}）")
                    if i < n - 1:
                        time.sleep(delay)
                        continue
                    # 订单活着 → 绝不回滚（回滚后它再成交就无人管辖）
                    return 'PENDING', last_detail, filled if filled > zero_tol else None

                # 未知 status 字符串：不猜，按 UNKNOWN
                return 'UNKNOWN', f"订单 {order_id} 状态不可识别：status={status!r}", None

            elif state == 'not_found':
                # v4 关键变化：create 已返回 ID → fetch 查不到 ≠ 没成交。
                # _verify_order_created 的 not_found 语义是 NOT_CONFIRMED（不 Commit），
                # 不是"证明订单不存在可以反向操作"。
                return 'NOT_CONFIRMED', (
                    f"订单 {order_id} create 返回了 ID，但 fetch_order 重试后仍查不到"
                    f"（NOT_CONFIRMED，绝不回滚）"), None
            else:  # unknown
                last_detail = f"查询订单 {order_id} 失败（结果未知）"
                if i < n - 1:
                    time.sleep(delay)
                    continue
                return 'UNKNOWN', f"查询订单 {order_id} 连续 {n} 次失败，结果未知", None

        return 'UNKNOWN', (last_detail or f"订单 {order_id} 确认流程异常结束"), None

    # ── P0-4：下单数量守卫（归因规则收紧）──────────────────────────

    def _survey_same_side_batches(self, symbol: str, side: str,
                                  exclude_batch_id: str):
        """勘察同 symbol + 同方向的批次分布。

        返回 (others_count, sum_all_ledgers)：
          others_count    = 除本批次外，仍活跃（未进入平仓流程）且有已成交
                            仓位的其他批次数
          sum_all_ledgers = 全部活跃同方向批次（**含本批次**）的台账量之和
        返回 (-1, -1) = 无法判定（load_all_states 失败/字段异常）→ 调用方必须
        Fail-Closed。

        ⚠️ 为什么必须返回 sum 而不是只返回计数：ChatGPT 终审 §四 的决定性例子
        （A 台账 0.001 + B 台账 0.001，总实际 0.001，A 已被手动平掉但台账未同步）
        中 actual == ledger_A，"actual >= ledger → 按台账平"的规则根本拦不住。
        真正可检测的不变量是 **总实际敞口 vs 台账合计**：0.001 < 0.002 → 漂移
        可见 → 归因冲突成立。总量 vs 单批台账仍然无法归因（与否决 delta 同理）。
        """
        try:
            all_states = self.load_all_states()
        except Exception as e:
            print(f"  ⚠️ 勘察同方向批次失败（无法判定归因）: {e}")
            return -1, -1
        batches = all_states.get(symbol, {}) or {}
        others = 0
        sum_all = 0.0
        for bid, b in batches.items():
            if not isinstance(b, dict):
                continue
            if b.get('side', 'BUY') != side:
                continue
            if int(b.get('close_phase', 0) or 0) >= 1 or b.get('pending_close'):
                continue
            try:
                filled = float(sum((b.get('target_amounts') or [])
                                   [:int(b.get('last_filled_count', 0) or 0)]))
            except (TypeError, ValueError):
                return -1, -1
            if filled <= 0:
                continue
            sum_all += filled
            if bid != exclude_batch_id:
                others += 1
        return others, sum_all

    def _close_amount_guard(self, symbol: str, side: str, is_hedge_mode: bool,
                            ledger_amount: float, batch_id: str):
        """下单数量守卫（v4 收紧归因规则，ChatGPT 终审 §四 + 本稿再收紧）。

        规则：
          单批次方向（无其他活跃有仓批次）：
            actual >= ledger → 按台账平
            actual <  ledger → 按实测平（min 的合法域：归因唯一成立，B-03）
          多批次方向：
            actual < 台账合计（含本批） → **归因冲突，禁止自动平**（Fail-Closed）
              → 返回 None，调用方 critical + 人工 reconcile。
              这是 ChatGPT 决定性例子（A/B 各台账 0.001、总实际 0.001）唯一
              可检测的形式——actual == ledger_A 时单看本批台账无法发现漂移。
            actual >= 台账合计 → 按台账平（各批台账合计与交易所一致，归属成立）
          读取失败 / 勘察失败 → None（Fail-Closed 不发单，B-09 已获批准）

        返回 (amount, detail)。amount=None → 调用方必须 Fail-Closed 不发单。
        """
        actual = self._read_position_amt(symbol, side, is_hedge_mode)
        if actual is None:
            return None, "读取实际持仓失败，无法确定平仓数量（Fail-Closed，不发单）"
        tol = 1e-8 + abs(ledger_amount) * 1e-6

        others, sum_all = self._survey_same_side_batches(symbol, side, batch_id)
        if others < 0:
            return None, "同方向批次勘察失败，归因不可判定（Fail-Closed，不发单）"

        if others == 0:
            # 单批次：归因唯一，min 是合法域
            if actual >= ledger_amount - tol:
                if actual <= 0 and ledger_amount <= 0:
                    return 0.0, "台账与实测敞口均为 0，无需平仓"
                return ledger_amount, f"单批次方向，总敞口 {actual} ≥ 台账 {ledger_amount}，按台账量平仓"
            if actual <= 0:
                return 0.0, f"实际敞口为 0（台账 {ledger_amount}），无需平仓"
            return actual, (f"单批次方向，台账 {ledger_amount} > 实测 {actual}，"
                            f"归因唯一成立，按实测 {actual} 平仓")

        # 多批次：总量 vs 台账合计
        if actual < sum_all - tol:
            return None, (f"归因冲突：总敞口 {actual} < 同方向活跃批次台账合计 {sum_all}"
                          f"（本批 {ledger_amount} + 其他 {others} 批）——账本与交易所已漂移，"
                          f"总量数据不能证明 batch 归属，禁止自动平仓"
                          f"（Fail-Closed，critical + 人工 reconcile）")
        if actual > sum_all + tol:
            print(f"  ⚠️ [归因] 总敞口 {actual} > 台账合计 {sum_all}：存在未跟踪敞口，"
                  f"平本批台账量不会侵占其他批次，但请人工留意多余敞口的来源")
        return ledger_amount, (f"多批次方向但台账合计 {sum_all} ≤ 总敞口 {actual}，"
                               f"归属成立，按台账量 {ledger_amount} 平仓")

    # ── P0-2：原子回滚（close_op_id CAS，复用 G3b 范式）────────────

    def _rollback_close_request_if_current(self, symbol: str, batch_id: str,
                                           close_op_id: str):
        """受控逆向迁移的唯一入口：原子回滚本次 close 请求的临时状态。

        范式复用 trader_260725.py::_commit_protection_with_g3（L3463，G3b）：
          持 _state_lock → 锁内 load_all_states() 重读最新磁盘（禁旧快照，
          消灭 TOCTOU）→ 同一锁段内判定 + 修改 + _persist_states。

        回滚资格（全部满足才执行，任一不满足拒绝）：
          1. batch 仍存在
          2. disk.close_op_id == 我这次的 close_op_id   ← 操作身份，证明
             "这是我的那一个 1"，不是别人正在推进的流程
          3. disk.close_phase 仍为 1                    ← 没有别的线程推进过
          4. 无 settled_by_limit_close 事实             ← 已发生的事实绝不降级

        只改三个字段：close_phase=0 / pending_close=False / is_programmatic_cancel=False。

        边界（G3b 契约）：_state_lock 非重入 → 锁内禁止调 save_batch_state /
        _update_registry（内部再取锁会死锁），直接操作 dict + _persist_states；
        锁内零交易所 API。

        返回 (ok: bool, reason: str)。
        """
        with self._state_lock:
            all_states = self.load_all_states()  # 硬约束：锁内重读，禁旧快照
            b = (all_states.get(symbol, {}) or {}).get(batch_id)
            if b is None:
                return False, 'batch_missing'
            disk_op_id = b.get('close_op_id') or ''
            if disk_op_id != (close_op_id or ''):
                return False, (f'op_id_mismatch（disk={disk_op_id!r} ≠ '
                               f'mine={close_op_id!r}，已有其他操作接管）')
            if int(b.get('close_phase', 0) or 0) != 1:
                return False, 'phase_changed（close_phase 已被推进，非本次请求）'
            if b.get('settled_by_limit_close'):
                return False, 'settled_fact_present（结算事实已发生，绝不降级）'
            b['close_phase'] = 0
            b['pending_close'] = False
            b['is_programmatic_cancel'] = False
            self._persist_states(all_states)
            return True, 'rolled_back'

    # ── P0-3：ENTRY 撤单 + 逐 ID 终态验证 ──────────────────────────

    def _verify_entry_order_terminal(self, order_id, symbol: str,
                                     attempts: int = 3, delay: float = 0.8):
        """逐 ID 确认单个 ENTRY 挂单已消失（事务事实按 ID 归因，与平仓确认同原则）。

        返回 verdict ∈ {'gone','filled','open','unknown'}：
          gone    → canceled/expired/rejected，或 OrderNotFound（G3a 同语义：
                    -2011/Unknown order = 已收敛）
          filled  → ENTRY 在等待期间成交了 → 仓位已变化，必须中断放行流程
          open    → 仍然活着
          unknown → 查询失败，不可判定
        """
        for i in range(max(1, attempts)):
            try:
                order = self._safe_api_call(
                    self.exchange.fetch_order, order_id, symbol,
                    params={'stop': True}, retries=1)
            except ccxt.OrderNotFound:
                return 'gone', None
            except Exception:
                return 'unknown', None
            if order is None:
                # _safe_api_call 静默失败（限流/网络）→ 未知，绝不当成"已消失"
                if i < attempts - 1:
                    time.sleep(delay)
                    continue
                return 'unknown', None
            status = str((order or {}).get('status') or '').lower()
            if status in ('canceled', 'expired', 'rejected'):
                return 'gone', order
            if status in ('closed', 'filled'):
                return 'filled', order
            if i < attempts - 1:
                time.sleep(delay)
                continue
            return 'open' if status else 'unknown', order
        return 'unknown', None

    def _cancel_and_verify_entry_orders(self, symbol: str, batch_id: str,
                                        b_data: dict, last_filled_count: int) -> bool:
        """平仓成功后撤未成交 ENTRY 并做交易所侧验证（v4：P0-3 修复）。

        v3 的两个缺陷（ChatGPT 终审 §三）：
          1. `fetch_open_orders(...) or []` —— 与 C-1 完全同型的假确认：
             None → [] → remaining_ids 空 → still_alive 空 → ✅"全部清零"。
             实际查询根本没给出有效结果。UNKNOWN → EMPTY，而本 helper 的安全
             意义恰恰是"证明 ENTRY 不会重新开仓"。
          2. 只用 open_orders 快照判清零，违反项目"事务事实按 ID 归因"原则
             （L3371：Verify 必须用 fetch_order）。

        v4 修复：
          - 拦截 None / 非 list → critical + return False（Fail-Closed）
          - 撤单后逐 ID fetch_order(stop=True) 终态确认（复用 G3a 的
            OrderNotFound = 已收敛语义）
        """
        entry_orders = b_data.get('entry_orders', []) or []
        pending_ids = [oid for idx, oid in enumerate(entry_orders)
                       if idx >= last_filled_count and oid]
        if not pending_ids:
            return True

        for order_id in pending_ids:
            try:
                self._safe_api_call(self.exchange.cancel_order, order_id, symbol,
                                    params={'stop': True})
                print(f"  └─ 已撤销开仓挂单: {order_id}")
            except Exception as e:
                if '-2011' in str(e) or 'Unknown order' in str(e):
                    print(f"  └─ 开仓挂单 {order_id} 已不存在（视为已撤）")
                else:
                    print(f"  └─ ⚠️ 撤销开仓挂单失败: {order_id} ({e})（由逐 ID 验证阶段定案）")

        # ── 第 1 层：open_orders 快照（v4：禁 or []，None/非 list = Fail-Closed）
        try:
            remaining = self._safe_api_call(
                self.exchange.fetch_open_orders, symbol, params={'stop': True})
        except Exception as e:
            remaining = None
            print(f"  └─ ⚠️ 撤单后交易所快照查询异常: {e}")
        if remaining is None or not isinstance(remaining, list):
            self.send_tg_notification(
                f"🚨【资金安全】平仓后 ENTRY 校验失败（快照不可判定）！\n"
                f"🆔 批次: {batch_id}\n"
                f"⚠️ fetch_open_orders 返回 {type(remaining).__name__}，"
                f"无法确认残留 ENTRY 是否已清零，请立即人工核对！",
                level='critical')
            return False

        remaining_ids = {str(o.get('id')) for o in remaining if isinstance(o, dict)}
        still_alive = [oid for oid in pending_ids if str(oid) in remaining_ids]
        if still_alive:
            print(f"  └─ 🚨 撤单后交易所仍存在 ENTRY: {still_alive}")
            self.send_tg_notification(
                f"🚨【资金安全】平仓成功后仍有未撤销的开仓条件单！\n"
                f"🆔 批次: {batch_id}\n📌 残留订单: {still_alive}\n"
                f"⚠️ 这些挂单成交后将形成无保护仓位，请立即人工处理！",
                level='critical')
            return False

        # ── 第 2 层（v4 新增）：逐 ID fetch_order 终态确认
        for oid in pending_ids:
            verdict, _order = self._verify_entry_order_terminal(oid, symbol)
            if verdict == 'gone':
                continue
            if verdict == 'filled':
                detail = f"ENTRY {oid} 在平仓等待期间成交（仓位已变化）"
            elif verdict == 'open':
                detail = f"ENTRY {oid} 撤单后仍存活"
            else:
                detail = f"ENTRY {oid} 终态无法判定（查询失败）"
            print(f"  └─ 🚨 ENTRY 逐 ID 验证未通过: {detail}")
            self.send_tg_notification(
                f"🚨【资金安全】平仓后 ENTRY 逐 ID 验证未通过！\n"
                f"🆔 批次: {batch_id}\n📌 {detail}\n"
                f"⚠️ 可能形成无保护仓位，请立即人工核对持仓与挂单！",
                level='critical')
            return False

        print(f"  └─ ✅ ENTRY 撤单已交易所侧校验通过"
              f"（快照 + 逐 ID 终态，{len(pending_ids)} 个全部确认消失）")
        return True
