import time

import ccxt


class _Holder:
    # ══════════════════════════════════════════════════════════════════
    # 2026-08-29 -4061 事故 · ChatGPT 终审「必须修 1」 · v3 重做
    #
    # v2 的 delta 判据已被交叉审查 B-01 证伪，本版改用【订单维度】判据。
    # 保留 v2 的 _read_position_amt（修掉 None→0.0 的 Fail-Closed 失效），
    # 但把它从「放行判据」降级为「交叉校验 + 数量兜底」。
    # ══════════════════════════════════════════════════════════════════

    def _read_position_amt(self, symbol: str, side: str, is_hedge_mode: bool) -> float | None:
        """读取【symbol + 持仓方向】的持仓绝对值。

        返回 None = 查询失败（不可判定）→ 调用方必须 Fail-Closed。
        返回 0.0  = 该方向无敞口。

        ⚠️ v3 修正（交叉审查 C 高危）：v2 里 `for pos in positions or []` 会把
          fetch_positions 返回 None（**非异常**）静默当成"无敞口" → 返回 0.0
          → delta = pos_before - 0.0 ≥ expected → **判「已平仓」**。
          持仓根本没动却放行 → 撤 SL/TP → 裸仓。这是 Fail-Closed 失效。
          现显式拦截 None 并同样返回 None。

        ⚠️ 2026-08-29 探针实证（G:/tmp/probe_position_shape.py）：
          · ccxt 会过滤零仓位行 → 「查不到条目」确实等价于「无敞口」。
          · 但 side 传错时同样得到 0.0，与「已平仓」**无法区分**。
            故本 helper **禁止单独用作放行判据**，只用于交叉校验与数量兜底。

        side 必须是【持仓方向】（BUY=多头 / SELL=空头），不是平仓方向。
        """
        try:
            positions = self._safe_api_call(self.exchange.fetch_positions, [symbol])
        except Exception as e:
            print(f"  ⚠️ 读取持仓失败: {e}")
            return None
        if positions is None:
            # v3 新增：非异常的 None 返回同样不可判定，绝不能退化成 0.0
            print("  ⚠️ 读取持仓失败：fetch_positions 返回 None（非异常）")
            return None
        target = 'long' if side == 'BUY' else 'short'
        want_raw = symbol.replace('/', '').split(':')[0]
        total = 0.0
        for pos in positions or []:
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

        **复用 trader_260725.py::_verify_order_created（L3368）的既有三态语义**，
        不另造一套：
          success   → 订单真实存在，order 可用
          not_found → OrderNotFound，确实不存在
          unknown   → 其他异常（网络 / 限流等）→ 调用方必须 Fail-Closed
                      （UNKNOWN ≠ EMPTY：结果未知不能被当成"不存在"）

        平仓单走 'normal' 端点（**不带** params={'stop': True}）—— 那是给
        STOP/TAKE_PROFIT 条件单用的，普通市价/限价单带 stop=True 会查错端点。

        ⚠️ create 后立即 fetch 存在 Binance 可见性延迟（事件 3 实证：4/4 单
          create 成功但 0 秒 verify 全部 OrderNotFound 假阴性；曾致 12 处误判
          → 无限重挂 24 个孤儿单）。故 not_found 必须重试后再定案。
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

    def _confirm_close_filled(self, symbol: str, side: str, is_hedge_mode: bool,
                              order_id, expected: float, pos_before: float | None = None,
                              attempts: int = 3, delay: float = 0.6):
        """确认【这张平仓单】确实成交了。返回 (verdict, detail)。

        verdict:
          'confirmed'  → 按单确认成交 ≥ 有效预期 → **放行**（可撤 SL/TP）
          'not_filled' → 订单不存在 / 未成交 / 成交不足 → **可安全回滚**
          'unknown'    → 查询失败，结果未知 → **Fail-Closed，绝不撤 SL/TP**

        ═════ 为什么主判据必须是「订单维度」而不是「持仓维度」═════
        交叉审查 B-01 实证：v2 的 delta（敞口减少量 ≥ 被平数量）**无法归因**。
        _read_position_amt 读的是 symbol+方向的**总敞口**，不是本批次敞口：
          另一批次 SL 成交 / 用户 App 手动平仓 / ADL / 另一批限价平仓成交
        都会让总敞口下降 → delta 达标 → 把别人的成交当成自己的证据
        → 撤 SL/TP → **裸仓**。

        决定性证据：delta 的「正样本」（before=0.002 after=0.001 expected=0.001）
        与「假确认样本」观测数据**完全同形**，物理不可区分；且触发条件（另一批次
        SL 成交）在剧烈行情下与"我要市价平仓"高度同时发生。

        fetch_order 按单确认则天然免疫：它回答的是「我这张单成交了多少」，
        每单独立、可归因，不受任何其他参与者影响。
        ══════════════════════════════════════════════════════════════

        B-03 修正：expected 取 min(台账量, pos_before)。台账量可能大于实际剩余
        （上次部分成交 / SL 部分平掉未同步 / 用户手动减仓 / 台账漂移），
        若直接用台账量判，即使仓位真实归零也永远判不通过 → 永久不可平。
        """
        if expected is None or expected <= 0:
            return 'unknown', f"参数不可判定（expected={expected}）"

        # B-03：有效预期 = min(台账名义量, 平仓前实测敞口)
        if pos_before is not None and pos_before > 0:
            eff_expected = min(expected, pos_before)
        else:
            eff_expected = expected
        tol = 1e-8 + abs(eff_expected) * 1e-6

        n = max(1, attempts)
        last_detail = ''
        for i in range(n):
            state, order = self._fetch_close_order_state(order_id, symbol)

            if state == 'success':
                if not isinstance(order, dict):
                    return 'unknown', f"订单结构异常（{type(order).__name__}）"
                status = str(order.get('status') or '').lower()
                try:
                    filled = float(order.get('filled') or 0)
                except (TypeError, ValueError):
                    return 'unknown', f"filled 字段异常（{order.get('filled')!r}）"

                if status in ('closed', 'filled') and filled >= eff_expected - tol:
                    detail = (f"订单 {order_id} 已成交 filled={filled}"
                              f"（有效预期 {eff_expected}，台账 {expected}），status={status}")
                    # 二级交叉校验（B-01 处置 2）：按单已确认成交，再看敞口是否真的减少。
                    # 仅告警不阻断——多批次下其他批次的减仓会让这里出现正常的不匹配。
                    if pos_before is not None:
                        after = self._read_position_amt(symbol, side, is_hedge_mode)
                        if after is not None and (pos_before - after) < eff_expected - tol:
                            print(f"  ⚠️ [交叉校验] 订单已成交但敞口未见相应减少："
                                  f"before={pos_before} after={after} "
                                  f"预期减少>={eff_expected}")
                    return 'confirmed', detail

                last_detail = (f"订单 {order_id} 未成交或成交不足："
                               f"status={status} filled={filled} 预期>={eff_expected}")
            elif state == 'not_found':
                return 'not_filled', f"订单 {order_id} 在交易所不存在（未创建成功或已撤销）"
            else:  # unknown —— 结果未知，绝不当成"没成交"，也不当成"已成交"
                last_detail = f"查询订单 {order_id} 失败（结果未知）"
                if i < n - 1:
                    time.sleep(delay)
                    continue
                return 'unknown', f"查询订单 {order_id} 连续 {n} 次失败，结果未知"

            if i < n - 1:
                time.sleep(delay)

        return 'not_filled', (last_detail or
                              f"订单 {order_id} 未成交或成交不足（预期 {eff_expected}）")

    def _close_amount_guard(self, symbol: str, side: str, is_hedge_mode: bool,
                            ledger_amount: float):
        """Q3 代码层兜底：下单数量取 min(台账量, 实测敞口)。

        目的：不依赖「超额 SELL + positionSide=LONG 会被交易所拒绝、不会反向开仓」
        这条**未经确认**的语义（文档 §四 情况 B 已自标注「待交易所语义确认，
        不用于升/降级」，而改动 1 移除 reduceOnly 后却成了方案的基石 —— 这是
        文档内部矛盾，交叉审查 B-04 指出）。

        用数量兜底替代语义依赖：只平实际存在的量，从源头杜绝超额。

        返回 (amount, detail)。读取失败返回 (None, ...) → 调用方必须 Fail-Closed
        （不发单），不能退回台账量。
        """
        actual = self._read_position_amt(symbol, side, is_hedge_mode)
        if actual is None:
            return None, "读取实际持仓失败，无法确定平仓数量（Fail-Closed，不发单）"
        amount = min(ledger_amount, actual)
        if amount <= 0:
            return 0.0, f"实际敞口为 0（台账 {ledger_amount}，实测 {actual}），无需平仓"
        if amount < ledger_amount:
            return amount, (f"台账量 {ledger_amount} 大于实测敞口 {actual}，"
                            f"按 {amount} 平仓（避免超额下单）")
        return amount, f"平仓数量 {amount}"
