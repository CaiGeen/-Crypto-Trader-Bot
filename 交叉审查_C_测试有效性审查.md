# 交叉审查 C：测试有效性审查（变异测试 + 伪断言排查）

审查日期：2026-08-30
审查对象：`test_position_close_confirmation.py` / `test_merge_rollback_semantics.py` / `test_ast_rollback_guard.py`
执行环境：`G:\my-crypto-bot\.venv\Scripts\python.exe`（Python 3.11.1），全离线、零 API、零写盘
工作树状态：**干净**（`git status --porcelain` 无 `M`/`D` 条目，生产文件与三个测试脚本一个字节都没动）

---

## 0. 结论速览

| 项目 | 结果 |
|---|---|
| 伪断言（恒真/恒假/自我实现） | **无**（但有 2 处"真空真值"显示缺陷，见 §1） |
| 变异测试：test_position_close_confirmation | 用户指定 5 项**全部有效**（rc 0→1）；补充 5 项中 **5 项存活**（无效） |
| 变异测试：test_ast_rollback_guard | 11 个绕过样本中 **3 个未被拦截**（rc=0） |
| 变异测试：test_merge_rollback_semantics | 4 个语义变异体**全部有效**；4 个机制健壮性探针中 **3 个暴露缺陷** |
| Mock 保真度 | 序列用尽后重复最后值**属实**且选择正确；但存在 **1 个高危掩盖**（None 静默转 `[]`） |
| 覆盖率缺口 | **13 项** |

> ### 明确结论
> **发现 11 个无效断言 / 守卫盲区 / 机制缺陷，必须补测，不可直接送审。**
>
> 阻塞项（按严重度）：
> 1. **【高危】`fetch_positions` 返回 `None` → 假确认**：被测实现把 `None` 当成"无敞口"返回 `0.0`，Fail-Closed 完全失效，7 个场景无一覆盖（§4 M-1）
> 2. **【高危】AST 守卫重复计数**：`ast.walk` 把嵌套函数里的同一个调用算成 2 处，1 个真实调用点即可让守卫"通过"（§3 d2）
> 3. **【中】AST 守卫看不见字符串拼接 + 转发 helper 的绕过**（§3 b2）
> 4. **【中】AST 守卫看不见 callee 名被局部同名变量遮蔽的绕过**（§3 c2）
> 5. **【中】5 个语义变异体存活**：`abs()` / symbol 过滤 / `is_hedge_mode` 过滤 / `'both'` 分支 / 轮询次数，全部无断言（§2 f~j）

---

## 1. 伪断言排查

### 1.1 恒真 / 恒假 / 自我实现断言：**无**

逐个脚本扫描 `or True` / `and True` / `assert True` / `is False or ...` / `assert 1` / 断言 mock 返回值 / 变量自赋值后断言自己 等模式，三个受审脚本**均未命中**。

逐项确认关键断言是"真断言"：

| 位置 | 断言 | 判定 |
|---|---|---|
| `test_position_close_confirmation.py:156` | `good = (got == want)`，`got` 来自被测实现返回值，`want` 是硬编码期望 | ✅ 真断言 |
| `:157` | `allok &= good`，`allok` 初值 `True`，与 bool 累积 | ✅ 真断言 |
| `:164` | `if allok: return 0 else return 1` | ✅ 真断言 |
| `:91-96` | 缺函数 → `sys.exit(1)` | ✅ 真断言 |
| `test_ast_rollback_guard.py:114-120` | 6 个 `ok_*` 全部由实际扫描结果计算 | ✅ 真断言（但见 1.2） |
| `test_merge_rollback_semantics.py:107/118/133/140/147/154` | `ok1..ok6` 全部拿 `m.get(...)` 与硬编码期望比，`is True` 用身份比较而非真值 | ✅ 真断言 |

`test_position_close_confirmation.py:104-112` 的 `_bind` 机制确认**没有**把 mock 的返回值当被测逻辑：被测的 `_confirm_position_reduced` 内部通过 `self._read_position_amt` 回调，绑定的是被测实现自己的读取函数（套在 FakeSelf 上），不是桩。

### 1.2 同类风险：真空真值（vacuous truth）—— 2 处

这两处**不是**假断言，但属于"什么都没检查却打印 True"的同一类风险，正是本项目已出过事故的模式，必须记录。

**① `test_ast_rollback_guard.py:117-118`（+ 打印 :125-126）**

```python
117:    ok_literal = all(lit for _, _, _, lit in named)
118:    ok_callee = all(c == EXPECTED_CALLEE for _, _, c, _ in named)
```

`named` 为空时 `all([])` 恒为 `True`。实测对生产文件跑（0 处调用），输出为：

```
  全部为字面量 True                    : True
  callee 全为 save_batch_state               : True
```

——一项都没检查，却打印两个 True。虽然 `ok_count` 会拦住最终判定（rc=1 正确），但显示层给出了误导性的绿灯。
**建议**：`ok_literal = bool(named) and all(...)`，`ok_callee = bool(named) and all(...)`。

**② `test_position_close_confirmation.py:165`**

```python
165:        print(f"✅ 仓位归零确认语义全部通过（{len(cases)}/{len(cases)}）")
```

`len(cases)/len(cases)` 恒为 `7/7`。仅影响显示（在 `if allok:` 分支内，不会造成假通过），但同样是"看起来被验证过"的坏味道。
**建议**：改为 `sum(1 for _, got, want, _ in cases if got == want)`。

### 1.3 顺带发现（超出本次审查范围，仅记录）

`test_auth_blocked.py:312 / 355 / 537 / 556` 存在 `lambda: events.append('x') or True` 形态。这里的 `or True` 是**给 mock 桩一个返回值**，不是断言，不构成本次定义的伪断言；但该写法与事故中的 `or True` 同形，建议统一改成显式 `return True` 的多行桩函数，避免后续被误读或误改。

---

## 2. 变异测试：test_position_close_confirmation.py

### 2.1 方法

生成器：`G:\tmp\make_mutation_fixtures.py`
—— 从 `G:\tmp\new_helpers_after.py` 原样读取，每个变异体只做**一处**最小字符串手术，且每个手术点强制 `count == 1`（`assert`），命中不唯一即判定"源码漂移、样本作废"。9 个变异体全部通过 `ast.parse` 自检。
另 `G:\tmp\mut_j_overpoll.py` 单独生成（同一手法）。

判定标准：变异体下 `test_position_close_confirmation.py` 必须从 **rc=0 变成 rc≠0**。仍然 rc=0 ⇒ 该变异对应的断言**无效**。

### 2.2 结果

#### 用户指定 5 项 —— 全部有效 ✅

| # | 变异体文件 | 改动内容（原 → 变异） | 期望 | 实测 rc | 失败场景 | 结论 |
|---|---|---|---|---|---|---|
| a | `G:/tmp/mut_a_delta_ge0.py` | `if delta >= expected - tol:` → `if delta >= 0:` | rc≠0 | **1** | S2, S5, S7 | ✅ 有效 |
| b | `G:/tmp/mut_b_tol_huge.py` | `tol = 1e-8 + abs(expected) * 1e-6` → `tol = 1e8` | rc≠0 | **1** | S2, S5, S7 | ✅ 有效 |
| c | `G:/tmp/mut_c_none_pass.py` | Fail-Closed 分支 `return False, "平仓后读取持仓失败…"` → `return True, "读取失败但放行"` | rc≠0 | **1** | S4 | ✅ 有效 |
| d | `G:/tmp/mut_d_side_swap.py` | `target = 'long' if side == 'BUY' else 'short'` → `'short' if 'BUY' else 'long'` | rc≠0 | **1** | S1, S3, S6 | ✅ 有效 |
| e | `G:/tmp/mut_e_attempts1.py` | `n = max(1, attempts)` → `n = 1` | rc≠0 | **1** | S6 | ✅ 有效 |

逐项说明：

- **(a)** 退化成"只要有减少就算通过" → S2（仓位未变，delta=0）、S5（方向传错，delta=0）、S7（减 0.0005 < 0.001）三个场景同时被判通过，被 S2/S5/S7 抓到。**S5 陷阱场景的断言是有效的。**
- **(b)** 容差放大到 1e8 → 判据形同虚设，同样被 S2/S5/S7 抓到。
- **(c)** 读取失败也算通过 → 被 S4 抓到。**Fail-Closed 断言有效。**
- **(d)** side 语义互换 → S1/S3/S6（正确方向读数全变 0）失败；S2/S5 反而"通过"（因为两边都读到 0）。**方向语义断言有效。**
  - 附带验证：**S5 的失败机制确实是 `delta=0`，不是"读取失败"** —— 变异 d 下 S5 的 detail 仍是"敞口未如预期减少"，没有变成"读取持仓失败"。这正是 docstring L52-57 声称要防的误报，实测防住了。
- **(e)** 轮询次数强制为 1 → S6 无法救回。**轮询语义断言有效。**

#### 补充 5 项 —— 全部存活 ❌（断言无效）

这部分不在用户指定清单内，是我为量化覆盖缺口额外构造的。**5 个全部 rc=0，即变异未被杀死，对应分支无任何断言。**

| # | 变异体文件 | 改动内容 | 期望 | 实测 rc | 结论 |
|---|---|---|---|---|---|
| f | `G:/tmp/mut_f_no_abs.py` | `total += abs(float(...))` → 去掉 `abs()` | rc≠0 | **0** | ❌ **无效：空头/负 contracts 无覆盖** |
| g | `G:/tmp/mut_g_no_symbol_filter.py` | 删掉 `if pos.get('symbol') != symbol and ...: continue` 整段 | rc≠0 | **0** | ❌ **无效：非目标 symbol 行无覆盖** |
| h | `G:/tmp/mut_h_ignore_hedge.py` | 删掉 `if is_hedge_mode: ... continue` 整段 | rc≠0 | **0** | ❌ **无效：反方向仓位行无覆盖** |
| i | `G:/tmp/mut_i_no_both.py` | `if ps not in (target, 'both'):` → `if ps != target:` | rc≠0 | **0** | ❌ **无效：positionSide='BOTH' 无覆盖** |
| j | `G:/tmp/mut_j_overpoll.py` | `n = max(1, attempts)` → `n = 50` | rc≠0 | **0** | ❌ **无效：轮询次数契约无断言** |

存活原因（在 §4 逐条给出补测建议）：

- **f**：7 个场景的 `contracts` 全为正数（`P('long', 0.001)` 等），没有一条空头/负仓位行，`abs()` 是否生效完全不可观测。
- **g**：7 个场景的持仓行 `symbol` 全是目标 symbol，`symbol` 过滤是否生效不可观测。
- **h**：7 个场景每个序列里只有同一方向的行，删掉 `is_hedge_mode` 过滤后读数不变。
- **i**：`P()` 生成的 `positionSide` 恒为 `side.upper()`（`'LONG'`），**没有任何一行是 `'BOTH'`**，`'both'` 分支不可观测。
- **j**：`FakeExchange.calls` 从未被断言；`delay=0.0` 让过度轮询零成本，轮询 50 次与 3 次在测试里完全等价。

#### 负向对照

| 目标 | 实测 rc | 失败场景 |
|---|---|---|
| `G:/tmp/new_helpers_naive.py`（朴素归零判据） | **1** | S3, S5, S6 |

✅ 负向对照成立：朴素实现会被 S3（多批次）/ S5（方向陷阱）/ S6（传播延迟）三处抓到，证明这三个场景的断言真的会失败。

---

## 3. 变异测试：test_ast_rollback_guard.py（守卫盲区探测）

### 3.1 方法

生成器：`G:\tmp\make_guard_mutations.py`
—— 每个样本 = 生产 `trader_260725.py` 原样 + 追加最小代码块（与 `make_guard_fixtures.py` 同一份基线，`guard_B_ok.py` 已是 rc=0）。
**关键设计**：每个绕过样本都保持「2 个白名单函数 + 恰好 2 处具名调用」的形态，只改变调用**形式**。这样 rc=0 就精确等价于"该绕过形式未被拦截"。

### 3.2 基线复现（用户声明属实 ✅）

| fixture | 期望 | 实测 rc |
|---|---|---|
| `guard_A_pristine.py` | 1 | **1** |
| `guard_B_ok.py` | 0 | **0** |
| `guard_C_third.py` | 1 | **1** |
| `guard_D_wrongfunc.py` | 1 | **1** |
| `guard_E_starstar.py` | 1 | **1** |
| `guard_E2_starstar_inline.py` | 1 | **1** |
| `guard_F_stray.py` | 1 | **1** |
| `guard_G_nonliteral.py` | 1 | **1** |

8/8 与声明一致。对生产文件跑 rc=1，失败原因正确打印为「改动尚未落地：全库 0 处调用（期望 2 处）」—— §四 要求的 RED 语义没失真。

### 3.3 绕过样本结果

| # | 样本文件 | 绕过形式 | 实测 rc | 拦截依据 | 结论 |
|---|---|---|---|---|---|
| a | `guard_mut_a_getattr.py` | `getattr(self, 'save_batch_state')('s','b',{}, allow_flag_rollback=True)` | **1** | ④ callee = `<unknown>` | ✅ 拦住 |
| b1 | `guard_mut_b1_concat.py` | `**{'allow_flag_' + 'rollback': True}`（最简） | **1** | 计数 1≠2 | ⚠️ 拦住但**原因失真** |
| b2 | `guard_mut_b2_concat_forwarder.py` | 字符串拼接 + 转发 helper，**计数守恒** | **0** | — | 🚨 **盲区** |
| b3 | `guard_mut_b3_dict_ctor.py` | `_kw = dict(allow_flag_rollback=True); f(**_kw)` | **1** | ④ callee = `dict` | ✅ 拦住（但不是靠 ⑤/⑥） |
| c1 | `guard_mut_c1_partial.py` | `_p = functools.partial(self.save_batch_state, ...); _p(allow_flag_rollback=True)` | **1** | ④ callee = `_p` | ✅ 拦住 |
| c2 | `guard_mut_c2_partial_shadow.py` | partial 绑定到**同名局部名** `save_batch_state` | **0** | — | 🚨 **盲区** |
| c3 | `guard_mut_c3_wrapper_helper.py` | `_save_with_rollback()` 包装 helper（架构上最正常的写法） | **1** | ② 位置越界 | ✅ 拦住（偏严，合法重构需同步改白名单） |
| d1 | `guard_mut_d1_nested.py` | `close_position_market` 内嵌 `_inner()`，调用在 `_inner` 里 | **1** | 计数 3≠2 | ⚠️ 拦住但**重复计数** |
| d2 | `guard_mut_d2_nested_shadow.py` | 内嵌函数**命名为 `close_position_limit`** | **0** | — | 🚨 **严重盲区** |
| d3 | `guard_mut_d3_module_lambda.py` | 模块级 `_h = lambda self: self.save_batch_state(..., allow_flag_rollback=True)` | **1** | 计数 1≠2 | ⚠️ 拦住但**调用点完全不可见** |
| d4 | `guard_mut_d4_module_level.py` | 模块级 `_G = dict(allow_flag_rollback=True)` | **1** | 计数 1≠2 | ⚠️ 拦住但**调用点完全不可见** |

### 3.4 盲区详解

#### 🚨 盲区 1：`collect()` 对嵌套函数重复计数（d2 最严重）

根因在 `test_ast_rollback_guard.py:75-86`：

```python
75:    for node in ast.walk(tree):
76:        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
77:            continue
78:        for sub in ast.walk(node):
```

`ast.walk(tree)` 会把**嵌套的 `FunctionDef` 也作为独立 node 遍历一遍**。同一个 `Call` 节点因此被登记两次：一次归属外层函数，一次归属内层函数。

实测证据（`guard_mut_d1_nested.py`，真实只有 2 处调用）：

```
① 具名关键字调用：3 处（期望 2）
  L7863  in close_position_market() -> save_batch_state()
  L7863  in _inner() -> save_batch_state()          ← 同一个 L7863，被算了两次
  L7868  in close_position_limit() -> save_batch_state()
```

实测证据（`guard_mut_d2_nested_shadow.py`，**真实只有 1 处调用**）：

```
① 具名关键字调用：2 处（期望 2）
  L7863  in close_position_market() -> save_batch_state()
  L7863  in close_position_limit() -> save_batch_state()   ← 同一行，内层函数恰好叫 close_position_limit
✅ AST 守卫通过：受控逆向迁移未扩散，且无动态绕过形式
rc=0
```

**危害**：只需 1 个真实调用点，把承载它的内层函数命名为另一个白名单函数，守卫就完整通过。数量检查 ① 和位置检查 ② 同时被绕过。
**修法**：顶层遍历改成"只取最外层函数"或在登记前对 `(lineno, col_offset)` 去重；位置归属应取**最近的外层白名单函数**，而不是所有包含它的函数。

#### 🚨 盲区 2：字符串拼接 + 转发 helper（b2）

```python
def _rollback_forwarder(self, **kw):
    return self.save_batch_state('s', 'b', {}, **kw)

def close_position_market(self):
    self.save_batch_state('s', 'b', {}, allow_flag_rollback=True)   # 合规，L7866
    self._rollback_forwarder(**{'allow_flag_' + 'rollback': True})  # 绕过，L7868 —— 完全不可见
```

- ⑤ 动态展开查不到：`kw.value` 是 `Dict`，但其 key 是 `BinOp`（`'allow_flag_' + 'rollback'`），不是 `ast.Constant` → `_dict_has_param()` 返回 False。
- ⑥ 常量散落也查不到：文件里的常量是 `'allow_flag_'` 和 `'rollback'`，都不等于完整的 `'allow_flag_rollback'`。
- 计数/位置/字面量/callee 四项检查全部通过（2 处白名单调用，字面量 True，callee 都是 `save_batch_state`）。

实测守卫只列出 L7866 和 L7871（两处合规调用），**L7868 的绕过调用一个字都没打印**，rc=0。

**修法**：⑥ 的常量检查要覆盖 `BinOp` 拼接（常量折叠 `'allow_flag_' + 'rollback'`）；更根本的是不能再依赖"字面量形态"，应改为对 `save_batch_state` 的**所有调用点**做计数白名单（含 `**展开` 且 key 非常量的情况一律视为可疑）。

#### 🚨 盲区 3：callee 名被局部同名变量遮蔽（c2）

```python
def close_position_market(self):
    save_batch_state = functools.partial(self.save_batch_state, 's', 'b', {})
    save_batch_state(allow_flag_rollback=True)   # callee 的 ast.Name.id 恰好是 save_batch_state
```

`_callee_name()` 只看 `ast.Name.id` / `ast.Attribute.attr`，不区分"这是 `self` 的方法还是局部别名"。结果计数=2、位置对、字面量 True、callee 全对、无展开、无散落 → rc=0。

**危害**：`allow_flag_rollback=True` 的实际调用点数量可以任意增长，只要每次都先做一次局部别名绑定。守卫对"调用点数量"的承诺是假的。
**修法**：callee 白名单应要求 `func` 是 `Attribute(value=Name('self'), attr='save_batch_state')`，不接受裸 `Name`。

### 3.5 次要问题：失败原因与事实不符（b1 / d3 / d4）

这三例虽然 rc=1（被拦），但打印的失败原因**指向错误的方向**，会让排查的人误判：

| 样本 | 实际 | 打印的原因 |
|---|---|---|
| b1 | 存在 1 处字符串拼接绕过 | 「调用点数量 1 ≠ 期望 2（**使用范围越界**）」 |
| d3 | 模块级 lambda 里有 1 处调用，守卫**根本没看见** | 「调用点数量 1 ≠ 期望 2（使用范围越界）」 |
| d4 | 模块级 `dict(...)` 调用，守卫**根本没看见** | 「调用点数量 1 ≠ 期望 2（使用范围越界）」 |

这与 `test_ast_rollback_guard.py:134-136` 自己声明的纪律（「尚未落地」和「越界/不合规」不能混用）是同一类问题的延伸：**"看见但形式可疑"和"根本没看见"被混为一谈**。
根因：`collect()` 的顶层循环 `for node in ast.walk(tree): if not isinstance(node, (FunctionDef, AsyncFunctionDef)): continue` —— 模块级语句、模块级 lambda、`ClassDef` 里非函数体的表达式，全部落在扫描范围之外。

**修法**：顶层循环改为对整个 tree 做一次全量 `ast.walk` 收集调用点（去重），再用"所属最外层函数"标注位置；对不属于任何函数的调用点单独归类并给出专门的失败原因。

---

## 4. 变异测试：test_merge_rollback_semantics.py

### 4.1 基线复现（用户声明属实 ✅）

| fixture | 期望 | 实测 rc | 详情 |
|---|---|---|---|
| 生产 `trader_260725.py` | 1 | **1** | 「签名中没有 allow_flag_rollback —— 改动尚未落地」 |
| `G:/tmp/merge_before.py` | 1 | **1** | 同上，可读 RED ✅ |
| `G:/tmp/merge_after.py` | 0 | **0** | 6/6 通过 |
| `G:/tmp/merge_after_broken.py` | 1 | **1** | **S2、S3 失败** —— 负向对照成立 ✅ |

`merge_after_broken.py`（白名单错误地含 `settled_by_limit_close`）实测：

```
[S2] allow=True 回滚生效且不动结算事实    : False
     → close_phase=0 pending=False prog=False settled=False  (期望 0/False/False/**True**)
[S3] 仅 settled 想降级 → 仍被棘轮挡住     : False
     → settled=False  (期望 True)
❌ 失败场景: S[2, 3]
```

✅ **负向对照有效**：S2/S3 的"结算事实不降级"断言真的会失败。

### 4.2 语义变异体 —— 4 项全部有效 ✅

生成器：`G:\tmp\make_merge_mutations.py`（同样强制 `count == 1` 唯一命中）

| 变异体文件 | 改动内容 | 期望 | 实测 rc | 失败场景 | 结论 |
|---|---|---|---|---|---|
| `merge_mut_all_rollback.py` | 白名单扩大到 `_MERGE_RATCHET_BOOL_FIELDS` 全部 3 字段 | rc≠0 | **1** | S2, S3 | ✅ 有效 |
| `merge_mut_always_snap.py` | `if allow_flag_rollback:` → `if True:` | rc≠0 | **1** | S1, S4 | ✅ 有效 |
| `merge_mut_never_rollback.py` | `if allow_flag_rollback:` → `if False:` | rc≠0 | **1** | S2 | ✅ 有效 |
| `merge_mut_phase_nomax.py` | allow=False 分支的 `close_phase` 不再取 `max` | rc≠0 | **1** | S1, S4 | ✅ 有效 |

**6 个场景的断言全部是有效的**，每一个语义分支都有对应的杀手变异体。这一项可以放心。

### 4.3 机制健壮性探针 —— 3 项暴露缺陷 ⚠️

用户特别要求验证"AST 查签名机制（声称能给出可读 RED 而非 TypeError 崩溃）是否真的健壮"。结论：**部分健壮，3 处会给出与事实不符的结论或裸崩溃。**

| 探针文件 | 构造 | 期望 | 实测 rc | 实际输出 | 结论 |
|---|---|---|---|---|---|
| `merge_mut_positional.py` | 参数写成 **positional-or-keyword**（`def f(self, disk, snap, allow_flag_rollback=False)`），功能完全正确 | 应 rc=0（或至少报"形式不符"） | **1** | 「❌ RED：目标实现的签名中没有 allow_flag_rollback —— **改动尚未落地**」 | 🚨 **误报** |
| `merge_mut_dupdef.py` | 文件里有两个 `_merge_batch_state`（诱饵在前、合规在后） | 应识别到合规版本或报错 | **1** | 「❌ RED：签名中没有 allow_flag_rollback —— **改动尚未落地**」 | 🚨 **误报** |
| `merge_mut_noconst.py` | 实现引用了生产文件里不存在的常量 | 可读 RED | **1** | **裸 `NameError` traceback**（5 层栈帧，无业务结论） | ⚠️ 崩溃式 RED |
| `merge_mut_notfound.py` | 文件里根本没有 `_merge_batch_state` | 可读 RED | **1** | 「❌ 目标文件中未找到函数 _merge_batch_state」 | ✅ 正确 |

**缺陷 1（误报"尚未落地"）—— `test_merge_rollback_semantics.py:92`**

```python
92:    has_param = any(a.arg == 'allow_flag_rollback' for a in node.args.kwonlyargs)
```

只查 `kwonlyargs`。参数若写成 positional-or-keyword（`args`）或 `**kwargs` 形式，一律判定为"没有这个参数"，进而打印「改动尚未落地」。
**后果**：改动**已经正确落地**、只是签名形式不同时，测试会说"没落地"，而且措辞是"这是预期的失败，不是缺陷"——直接把真实状态（已落地但形式不符）掩盖成预期状态。这是本审查定义的**诊断失真**，风险等级不低：它会让主代理误判落地进度。
**修法**：`has_param = any(a.arg == 'allow_flag_rollback' for a in (node.args.kwonlyargs + node.args.args + node.args.posonlyargs))`，并对"存在但不是 kw-only"给出独立结论。

**缺陷 2（诱饵优先）—— `test_merge_rollback_semantics.py:90` 与 `:54`**

```python
90:    node = next(n for n in ast.walk(ast.parse(src))
91:                if isinstance(n, FunctionDef) and n.name == FUNC_NAME)
```

`next()` 只取 BFS 序第一个。文件里若存在两个同名定义（例如遗留副本 + 新版、或类里有一个 + 模块级有一个），测试拿到的可能是错的那个，并据此报"尚未落地"。
**修法**：命中多个时应当报错（"存在 N 个同名定义，无法确定被测对象"）而不是静默取第一个。

**缺陷 3（缺失常量 → 裸崩溃）**

`build()` 在 `test_merge_rollback_semantics.py:70` 把被测函数 exec 进一个只装了生产常量的 namespace。被测实现一旦引用了生产文件里没有的常量，就会在 `main()` 的第一次 `call()` 处抛 `NameError`，输出 5 层 traceback，没有任何业务结论。
**修法**：在签名检查之后、执行场景之前，用 AST 收集被测函数引用的全局名，与 `ns` 做差集，缺失则打印「被测实现引用了 N 个生产文件中不存在的全局名：…」并 return 1。

### 4.4 顺带发现（fixture 质量，非测试缺陷）

`G:\tmp\merge_after.py` / `merge_after_broken.py` 的方法体缩进是 **12 空格**（应为 8）。根因在 `make_merge_fixtures.py:108` 的 `wrap()`：`ast.get_source_segment` 首行不带缩进，`textwrap.dedent` 后整体 +4，导致方法体相对 `class` 多了一级。语法上合法（已通过 `ast.parse` 与 6/6 执行），但**若把该样本的函数体直接粘进 `trader_260725.py`，缩进会错位一级**。建议 `wrap()` 后用 `ast.unparse` 或显式重排缩进再落盘。

---

## 5. Mock 保真度

探针：`G:\tmp\probe_mock_fidelity.py`（`import` 测试脚本复用其 `FakeExchange`/`FakeSelf`/`load_impl`，**不修改测试脚本**）

### 5.1 「序列用尽后重复最后一个值」—— 属实 ✅

```
seq 只有 1 个元素：第 1/2/3 次返回 contracts = [0.001, 0.001, 0.001]
calls = 3  → 重复最后一个值：是（与 docstring 一致）
```

`FakeExchange.fetch_positions`（`test_position_close_confirmation.py:64-71`）实测确认：序列耗尽后 `self.last` 保持不变并重复返回，不抛异常。与 `FakeExchange` docstring（L52-57）的声称一致。

**这个选择是否正确？** 对，且必要。变异体 (d) 提供了直接证据：side 语义互换后，S5 的 detail 仍是「敞口未如预期减少：before=0.0 after=0.0」，**没有**变成「读取持仓失败」。若 mock 在序列用尽时抛异常，S5 的真实失败机制（方向传错 → delta=0）会被误报成"读取失败"，正是 docstring 要防的事。docstring 的论证有实验支撑，不是空话。

### 5.2 但这个选择掩盖了什么

**① 【高危】`None` 返回被静默转成 `[]` —— 掩盖一个真实的假确认通道**

`test_position_close_confirmation.py:71`：

```python
71:        return v if v is not None else []
```

`FakeExchange` 把 `None` 归一化成 `[]`。于是：

| 探针 | `fetch_positions` 返回 | `_read_position_amt` 结果 |
|---|---|---|
| B1 | `None` | `0.0` |
| B2 | `[]` | `0.0` |

**两者在测试里完全等价**，而真实后果完全不同：

- `[]`（ccxt 过滤零仓位行后返回空）→ 确实无敞口，返回 `0.0` 正确；
- `None`（封装层/交易所异常路径返回空值，**不抛异常**）→ 被测实现 `_read_position_amt` 走 `for pos in positions or []`，零次迭代，返回 `0.0` —— **Fail-Closed 完全失效**。

端到端实测（探针 B17）：`before=0.001`，三次读取全部返回 `None`（非异常）→

```
B17 读取返回 None（非异常）  → True   敞口 0.001 → 0.0（减少 0.001，预期 0.001）
```

**`_confirm_position_reduced` 返回 `True`。** 即：仓位实际一动没动，确认函数说"已平仓"，于是撤 SL/TP → **裸仓**。这正是整个测试存在要防的那个事故，而 7 个场景没有一个覆盖它，mock 的 `None → []` 归一化进一步保证它永远测不到。

**修法（被测实现）**：`_read_position_amt` 里 `if positions is None: return None`（不可判定 ≠ 无敞口）。
**修法（测试）**：`FakeExchange` 不应做 `None → []` 归一化，应原样返回；并新增场景「读取返回 None（非异常）→ 必须 False」。

**② 过度轮询不可检测**

`FakeExchange.calls` 从未被任何场景断言。变异体 (j)（`n = 50`）实测 rc=0：轮询 50 次与 3 次在测试里完全等价（`delay=0.0` 让轮询零成本）。生产环境 `delay` 默认 0.6s，轮询 50 次意味着平仓确认最长阻塞 30s——是个真实的行为契约，但目前没有断言。

### 5.3 边界覆盖检查（用户点名的 4 项 + 扩展）

| 边界 | 是否覆盖 | 实测结果 | 判定 |
|---|---|---|---|
| `fetch_positions` 返回 `None` | ❌ 无场景 | 静默 → `0.0` | 🚨 假确认通道 |
| 返回空列表 `[]` | ✅ S1/S6 用 `EMPTY` | `0.0` | ✅ |
| 返回非目标 symbol 的行 | ❌ 无场景 | 变异 (g) 存活 | ❌ |
| 返回 `positionSide='BOTH'` 的行 | ❌ 无场景 | 变异 (i) 存活 | ❌ |
| 返回 `contracts` 为负（空头） | ❌ 无场景 | 变异 (f) 存活 | ❌ |
| `is_hedge_mode=False` | ❌ 无场景（`run()` 硬编码 `True`） | 探针 B10 可行但未被断言 | ❌ |
| `contracts` 为字符串 | ❌ 无场景 | 探针 B7 = `0.004`（可正常解析） | ⚠️ 行为正确但无断言 |
| `contracts` 键缺失 | ❌ 无场景 | 探针 B6b = `0.0` | ⚠️ 见下方观察 |

**额外观察（被测实现的潜在缺陷，非测试问题）**：`new_helpers_after.py:40` 的 `pos.get('contracts', 0) or pos.get('positionAmt', 0)` 中的 `positionAmt` 是**顶层**取值，但 ccxt 把 `positionAmt` 放在 `pos['info']` 里（见 `G:\tmp\probe_position_shape.py` 的数据结构验证）。探针 B6b 证实：只有 `info.positionAmt`、没有 `contracts` 时读数为 `0.0`。该 fallback 实际是死代码，且失败方式是"静默记 0"而不是报错。建议落地前确认。

---

## 6. 覆盖率缺口清单

「被测逻辑里存在、但没有任何场景覆盖」的分支，共 **13 项**：

| # | 缺口 | 证据 | 严重度 | 建议补测 |
|---|---|---|---|---|
| C-1 | `fetch_positions` 返回 `None`（非异常）→ 假确认 | 探针 B1/B17，`_read_position_amt` 返回 `0.0` | 🚨 高 | 新场景：读取全返 `None` → `ok` 必须 `False`；同时改 `_read_position_amt` 让 `None → None` |
| C-2 | 空头 / 负 `contracts`（`abs()` 是否生效） | 变异 (f) 存活 rc=0 | 中 | 新场景：`P('short', -0.002)` + `side='SELL'` |
| C-3 | 非目标 symbol 行（symbol 过滤） | 变异 (g) 存活 rc=0 | 中 | 新场景：混入 `ETHUSDT` 行，读数必须不变 |
| C-4 | 反方向仓位行（`is_hedge_mode` 方向过滤） | 变异 (h) 存活 rc=0 | 中 | 新场景：`side='BUY'` 但序列里混入 `short` 行 |
| C-5 | `positionSide='BOTH'`（单向持仓模式） | 变异 (i) 存活 rc=0 | 中 | 新场景：`ps='BOTH'` 的行必须计入 |
| C-6 | `is_hedge_mode=False` 全链路 | 7 个场景经 `run()` 全部硬编码 `True` | 中 | `run()` 增加 `hedge` 参数，至少 1 个场景跑 `False` |
| C-7 | `symbol` 带 `':USDT'` 后缀的形态 | `SYM` 硬编码 `'BTCUSDT'`；探针 B9 显示功能正常但无断言 | 低 | 至少 1 个场景用 `'BTC/USDT:USDT'` |
| C-8 | `expected <= 0` 的 Fail-Closed | 探针 B12/B13 返回 `False`，无场景断言 | 中 | 新场景：`expected=0` / `expected=-0.001` → 必须 `False` |
| C-9 | `pos_before is None` 的 Fail-Closed | 探针 B14 返回 `False`，无场景断言 | 中 | 新场景：`before` 读取失败 → 必须 `False` |
| C-10 | `attempts=0` 的 `max(1, ...)` 兜底 | 探针 B15 返回 `True`，无场景断言 | 低 | 新场景：`attempts=0` → 仍读 1 次 |
| C-11 | 轮询次数契约（`FakeExchange.calls`） | 变异 (j) 存活 rc=0 | 中 | 断言 `calls == min(attempts, 命中轮次)`；S6 应断言 `calls == 2` |
| C-12 | merge：`user_modified` OR 的 `snap=True` 一侧 | S6 只测了 `disk=True, snap=False` | 低 | 新场景：`disk={'user_modified': False}, snap={'user_modified': True}` → `True` |
| C-13 | merge：省略 `allow_flag_rollback`（默认值 `False` 路径） | 6 个场景全部显式传 `allow=` | 低 | 新场景：不传该参数 → 等价于 `allow=False` |

**说明**：`delta` 恰好等于 `expected` 的边界**已被覆盖** —— S1（0.001 → 0，delta=0.001，expected=0.001）就是精确相等的情形，探针 B11 确认 `True`。容差 `tol` 的等值语义有断言，这一项不是缺口。

---

## 7. 审查纪律自查

| 约束 | 遵守情况 |
|---|---|
| 不修改 `G:\my-crypto-bot` 下的生产文件与已有测试脚本 | ✅ `git status --porcelain` 无 `M`/`D` 条目，工作树干净 |
| 变异体一律写到 `G:\tmp\` 下的新文件名 | ✅ 全部 `mut_*.py` / `guard_mut_*.py` / `merge_mut_*.py` |
| 不调用真实交易所 API | ✅ 全部离线；`probe_mock_fidelity.py` 只 `import` 测试脚本的 mock，无网络 |
| 用 `.venv/Scripts/python.exe` 在 `G:\my-crypto-bot` 下执行 | ✅ 全部命令 `cd /g/my-crypto-bot && .venv/Scripts/python.exe ...` |
| Windows 风格路径 | ✅ 统一使用 `G:/tmp/xxx.py` |

### 产出文件清单

**变异体 / 探针（全部在 `G:\tmp\`）**

| 文件 | 用途 |
|---|---|
| `make_mutation_fixtures.py` | 生成 9 个 `new_helpers_after` 变异体（强制唯一命中 + `ast.parse` 自检） |
| `mut_a_delta_ge0.py` ~ `mut_i_no_both.py` | 9 个变异体 |
| `mut_j_overpoll.py` | 补充：轮询 50 次 |
| `make_guard_mutations.py` | 生成 11 个 AST 守卫绕过样本 |
| `guard_mut_a_getattr.py` ~ `guard_mut_d4_module_level.py` | 11 个绕过样本 |
| `make_merge_mutations.py` | 生成 8 个 merge 变异体/健壮性探针 |
| `merge_mut_positional.py` ~ `merge_mut_phase_nomax.py` | 8 个样本 |
| `probe_mock_fidelity.py` | Mock 保真度 + 13 项边界探针 |
| `out_guard_mut_*.txt` | 各绕过样本的完整运行输出（证据留存） |

---

## 8. 复审建议

按优先级：

1. **先修被测实现再补测**：`_read_position_amt` 必须把 `positions is None` 与 `[]` 区分开（前置 `return None`），否则 C-1 的假确认通道补了测试也只是把红灯点亮，不如直接堵住。
2. **修 AST 守卫的 3 个盲区**：嵌套函数去重（d2，最严重）、callee 必须绑定到 `self`（c2）、常量检查覆盖 `BinOp` 折叠 + 全 tree 扫描（b2 + d3/d4）。
3. **补 5 个存活变异的断言**：C-2 ~ C-5 各加 1 个场景，C-11 加 `FakeExchange.calls` 断言。
4. **修 merge 测试的机制缺陷**：`has_param` 覆盖 `args`/`posonlyargs`（消除"已落地却报未落地"）、同名定义多重命中时报错、缺失常量给可读 RED。
5. **修 2 处真空真值显示**：`test_ast_rollback_guard.py:117-118` 加 `bool(named) and`，`test_position_close_confirmation.py:165` 改成真实计数。

上述 1–3 完成并复跑变异矩阵全部转 rc≠0 后，可重新送审。
