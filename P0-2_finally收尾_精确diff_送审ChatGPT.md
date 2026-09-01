# P0-2 finally 收尾 · 精确 diff（送 ChatGPT 确认底线）

> 改动已在工作区生产文件上，**bot 未重启 = 尚未生效**；若底线不成立，重启前可秒回退。
> 位置：`trader_260725.py` `_start_monitoring` 的 finally 收尾段（退出前清理），共两处 hunk。

---

## Hunk 1：程序撤单收尾 · converge 有限重试（已单独获批）

```diff
                         if b_data.get('is_programmatic_cancel') or b_data.get('pending_close'):
-                            # P0 Batch B：converge 证明后才 clear（finally 无循环可重试，
-                            # 未收敛则保留状态 + 告警，交由重启恢复/下轮监控收敛）
-                            _proof = self._converge_batch_orders_before_clear(symbol, batch_id)
-                            if _proof is not None and self.clear_batch_state(symbol, batch_id, proof=_proof):
+                            # P0 Batch B：converge 证明后才 clear。v6.2-P0-2：finally 里加
+                            # 有限重试（3 次 × 2s）——撤单状态在交易所有传播短窗口，
+                            # 单次 UNKNOWN 不应直接把批次留成 close-in-flight。
+                            _proof = None
+                            for _attempt in range(3):
+                                _proof = self._converge_batch_orders_before_clear(symbol, batch_id)
+                                if _proof is not None:
+                                    break
+                                if _attempt < 2:
+                                    time.sleep(2)
+                            if _proof is not None and self.clear_batch_state(symbol, batch_id, proof=_proof):
                                 print(f"  └─ 🧹 程序撤单，批次状态已清理（proof 收敛通过）")
```

## Hunk 2：零成交撤单批次 · 聚合持仓归属修正（本次待确认）

```diff
             elif current_pos is None:
                 print(f"  └─ ⚠️ 持仓查询失败(UNKNOWN)，保留批次状态不清理")
             else:
-                print(f"  └─ 📌 有持仓 {current_pos}，保留批次状态")
+                # 🔥 v6.2-P0-2（实盘 2026-09-01 17:4x）：symbol 级聚合持仓不能归属本批次——
+                # 零成交 + 程序撤单收尾的批次自身持仓恒为 0，聚合 > 0 只可能来自其他批次
+                # （实例：f1e135 被新批次 29ca35 的 0.001 卡成 zombie，进而以 close-in-flight
+                # 阻塞同方向 single-flight，挡死真实活仓平仓）。此时继续走 converge/clear 收尾。
+                _zb = (self.load_all_states().get(symbol, {}) or {}).get(batch_id, {}) or {}
+                if (_zb.get('pending_close') or _zb.get('is_programmatic_cancel')) \
+                        and int(_zb.get('last_filled_count', 0) or 0) == 0:
+                    print(f"  └─ ℹ️ 聚合持仓 {current_pos} 属于其他批次（本批次零成交已撤单），继续收敛清理")
+                    _proof = self._converge_batch_orders_before_clear(symbol, batch_id)
+                    if _proof is not None and self.clear_batch_state(symbol, batch_id, proof=_proof):
+                        print(f"  └─ 🧹 零成交撤单批次已清理（proof 收敛通过）")
+                    else:
+                        print(f"  └─ ⚠️ [B] 零成交撤单批次 {batch_id} 本轮未收敛，保留状态待重启恢复重试")
+                else:
+                    print(f"  └─ 📌 有持仓 {current_pos}，保留批次状态")
```

---

## 底线逐条对照（你要求的确认项）

| 底线 | 实现 |
|---|---|
| **clear 必须以 `_converge_batch_orders_before_clear()` 返回 proof 为前提** | ✅ 两条 clear 路径守卫均为 `if _proof is not None and self.clear_batch_state(symbol, batch_id, proof=_proof)`（原语句未动，只换了 _proof 的取得方式） |
| **绝不凭 `last_filled_count==0` / programmatic flag 直接 clear** | ✅ `lfc==0` + flag 只用于决定「是否**进入** converge/clear 收尾」（即不被聚合持仓误留成 zombie），进入后仍必须拿到 proof 才 clear；拿不到 proof → 保留状态待重启恢复重试（Fail-Closed 不变） |
| **不放松 same-side single-flight** | ✅ BEGIN gate 零改动 |
| **不新增 helper/state/checker/mutation** | ✅ 两处 hunk 共 +24/−4 行，仅用既有 `_converge_batch_orders_before_clear` / `clear_batch_state` / `time.sleep` |

**语义总结**：flag 的唯一作用 = 「豁免聚合持仓的误归属，让收尾流程有机会跑」；clear 的唯一前提 = proof。收不到 proof 就保留，与旧行为一致。
