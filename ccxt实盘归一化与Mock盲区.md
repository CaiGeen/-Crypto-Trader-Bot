# ccxt 实盘归一化与 Mock 盲区对照（v1.0）

> **日期**：2026-08-21 ｜ **来源**：事件3通知风暴根因分析 + F1/F3/F3b 修复实证 ｜ **状态**：可复用规则
> **一句话**：测试 Mock 全用 Binance 原始格式、从没模拟 ccxt 实盘归一化产物 → **B2-1 测试全绿但实盘全挂**（4 条有效保护单全部误判 MISMATCH → 4 封 critical 邮件 + 4 条 critical TG）。

---

## 一、ccxt 实盘归一化对照表（Binance USDM，本项目的权威事实）

ccxt 会把交易所原始返回**归一化**为通用结构。本项目（`trader_260725.py`）本地持久化用的是**交易所原始格式**（Binance 风格），二者差异是大量"测试绿/实盘挂"事故的根源。

| 字段 | 本地/原始格式（Binance） | ccxt 实盘归一化产物 | 坑点 | 正确做法 |
|---|---|---|---|---|
| `symbol` | `'BTCUSDT'` | `'BTC/USDT:USDT'` | 去分隔符后 = `'BTCUSDTUSDT'`（**结算币 USDT 重复**）≠ 本地 `'BTCUSDT'` | 按 **base+quote 提取**：`s.split('/')[0] + s.split('/')[1].split(':')[0]` |
| `type`（条件单） | `'STOP_MARKET'` / `'TAKE_PROFIT_MARKET'` | 顶层归一为 `'market'` | 顶层 type 对条件单**恒失效**，不能用于比对 | 优先用 `info.type`（Binance 原始字段）还原；顶层 `market` 且无 `info.type` → **跳过 type 比对** |
| `reduceOnly` | `true` / `false`（bool） | Binance `info` 里是字符串 `'true'` / `'false'` | Python `bool('false') == True`（**经典陷阱**） | 统一 `_as_bool` 转换（仅当值恰为 `'true'` 才 True） |
| `status`（已撤销单） | — | 返回 `status='canceled'` 的订单**对象**，**不抛 OrderNotFound** | "fetch 到订单 ≠ 订单有效"；旧代码当 FOUND 进 intent 比对 → 假 MISMATCH | fetch 后 `status ∉ {new, open, active}` → 视为**已终结**（ABSENT），不进 intent 比对/不 cancel/不告警 |

**权威归一化实现**（F1，`trader_260725.py _order_matches_intent` 内 `_norm_sym`）：

```python
def _norm_sym(s):
    s = str(s or '').upper()
    if '/' in s:
        base, rest = s.split('/', 1)
        quote = rest.split(':', 1)[0]
        return (base + quote).replace('_', '')
    return s.replace('/', '').replace(':', '').replace('_', '')
# 'BTC/USDT:USDT' -> 'BTCUSDT'  ✅  |  'BTCUSDT' -> 'BTCUSDT'  ✅
# 去分隔符版（错误）：'BTC/USDT:USDT' -> 'BTCUSDTUSDT'  ❌ 结算币重复
```

---

## 二、归一化逻辑单一实现原则（F3 半成品教训）

**规则**：同一归一化逻辑**必须单一实现并复用**，禁止在多个文件各写一份"看起来一样"的版本。

**事故**：F3 修 `reconcile_pre_launch.py` 时，`_norm_symbol` 用了"去分隔符"版（少处理结算币），而 trader F1 的 `_norm_sym` 是 base+quote 版。结果 4c/4d 的 `has_position` 恒 False → **即使持仓真实存在（1.191 contracts）也误报"本地有成交但交易所无持仓"**（2026-08-21 实证）。

**防御**：
1. 跨文件 symbol 比对，全部复用 `_norm_sym`（或复制完全相同实现 + 单测锁定输出）
2. 归一化函数必须有单测锁定：输入 `'BTC/USDT:USDT'` → 输出 `'BTCUSDT'`（防"去分隔符版"回归）
3. 新写任何 symbol 比对代码，先跑一遍对照表两条用例再提交

---

## 三、Mock 盲区检查清单（测试侧，防"全绿实盘挂"）

写 mock 订单/持仓/成交时，逐项自问是否模拟了 **ccxt 归一化产物**（而非 Binance 原始格式）：

- [ ] `symbol` 用 `'BTC/USDT:USDT'`（不是 `'BTCUSDT'`）→ 验证归一化比对路径
- [ ] 条件单 `type='market'` + `info.type='STOP_MARKET'` → 验证 info.type 还原路径
- [ ] 条件单 `type='market'` 且**无** `info.type` → 验证跳过 type 比对路径
- [ ] `reduceOnly` 为字符串 `'true'` / `'false'` → 验证 `_as_bool`
- [ ] 已撤销单返回 `status='canceled'` **对象**（不抛异常）→ 验证 F2 生命周期分层（ABSENT）
- [ ] `side` 反例（应拒）与正例（应过）都覆盖
- [ ] 既有测试的 fixture 若一直用 Binance 原始格式，**补一组 ccxt 归一化形态用例**（本项目：`test_protection_chain_fix.py t_f1_intent_mapping` F1a-F1f 为范本）

**测试基建铁律**（第 3 次实证）：MagicMock 未绑定 helper → 自动 mock 返回 truthy → 走假路径。凡被测函数调用的 helper，必须在 `_bind_*` 中显式绑定真实实现（`hasattr` 保护）。

---

## 四、相关提交

| 提交 | 内容 |
|---|---|
| `73fdc60` | F1 `_order_matches_intent` 重写（_norm_sym / info.type / _as_bool）+ F2 生命周期分层 + F4b 启动窗口降级 |
| `ebbdc54` | F3b reconcile `_norm_symbol` 对齐 F1（修结算币重复恒误报） |

**延伸**：启动窗口通知降级模式（F4b）——`_process_start_ts` 记录启动时刻，`updated_at < start_ts` 的历史条目 MISMATCH 降级 print 不告警；升级告警（连续 10 轮查不到）不降级；start_ts 缺失/非数值保守不降级（宁多告警不漏告警）。
