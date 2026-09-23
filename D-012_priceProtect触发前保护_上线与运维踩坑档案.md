# D-012 priceProtect 触发前保护 —— 上线与运维踩坑档案

- 日期：2026-09-23
- 状态：已上线（存量 4 单按方案 A 保留 `priceProtect=false`，新批次起生效）
- 分支：frozen-archive（本地提交，未推送）
- 关联遗留：D-011 设计草案是否入库仍待定

## 1. 背景与决策

- 静态审计（已闭环）：`trader_260725.py` 全部 16 处 `create_order` 均源自
  L5407 单一 `params_base['workingType'] = 'MARK_PRICE'`，零硬编码遗漏。
- 实盘探测（已闭环）：4 张存量 STOP_MARKET 条件单 raw 回显全部
  `workingType: MARK_PRICE`、`priceProtect: false`。
- `priceProtect` 语义（官方 javadoc + FAQ + exchangeInfo 交叉验证）：
  每单参数（非账户级开关）；当 mark 与 last 偏离超过该品种 `triggerProtect`
  阈值（BTCUSDT 0.0500 即 5%）时暂停触发，收敛后自动恢复；
  与 `workingType=MARK_PRICE` 互补，正常行情期零成本。
- 选型：**方案 A** —— 存量 4 单不动、不改 `trade_state.json`、不重挂
  （币安无 ModifyAlgoOrder，重挂会破坏 bot 的 entry_orders/保护注册表同步），
  保护从下一批信号起生效。

## 2. 代码改动（trader_260725.py，3 行）

```diff
  params_base['workingType'] = 'MARK_PRICE'
+ # 触发前保护：mark/last 偏离超过该品种 triggerProtect 阈值（如 BTCUSDT 5%）时
+ # 暂停触发、收敛后自动恢复；与 workingType=MARK_PRICE 互补，正常期零成本。
+ params_base['priceProtect'] = True
  params_base['leverage'] = signal.leverage
```

- 16 处下单点（开/平/SL/TP/补挂/resize/用户 /tp /sl /be/撤单重挂）全走
  `params_base.copy()` → 全覆盖。
- `python -m py_compile` 通过。

## 3. 部署前交易所实测（写测试，闭环后删）

以 bot 同款参数挂 1 张远价测试单（触发价 mark+20%、0.001 BTC）：

| 步骤 | 结果 |
|---|---|
| 挂单 | `algoId 3000002212092966`，STOP_MARKET / CONDITIONAL / NEW |
| 回显 | **`priceProtect: true`**、**`workingType: MARK_PRICE`** |
| 撤单 | `stop:true` 路径成功 |
| 复核 | 条件单张数回 4 = 基线，零泄漏 |

测试脚本 `_test_priceprotect.py`、只读复核脚本 `_chk_orders.py` 均已删除。

## 4. 重启部署与验收（2026-09-23 22:58）

标准路径：`schtasks /End` → `schtasks /Run /TN CryptoBot-Autostart`。

| 验收项 | 结果 |
|---|---|
| 心跳 | `bot_alive=true`，watchdog_pid=38136 / bot_pid=37712，restarts=0 |
| 单实例锁 | `.bot_instance.lock = 26040`（真身上锁） |
| 新代码铁证 | py-spy 栈 `_start_monitoring: trader L7080`（原 7077 + 3 行位移） |
| 恢复链 | 批次接管、杠杆重设、监控注入、系统 READY |
| 存量 4 单 | 原样健在：MARK_PRICE ✓、priceProtect=false（方案 A 预期）✓ |
| 业务栈 | TG 长轮询 + 日报线程 + 监控线程 + 限流观测循环全部活跃 |

## 5. 踩坑档案（重点）

### 坑 #1：本机时钟快 1~2s → 签名请求 -1021

- 现象：独立脚本挂单报
  `InvalidNonce {"code":-1021,"msg":"Timestamp ... ahead of the server's time"}`；
  手写 `ex.options['timeDifference']` 无效。
- 根因：本机与币安服务器偏差约 1~2s；bot 自身靠启动时
  `load_time_difference` + 每轮调用自校准，独立脚本没做。
- 解法（抄 bot 姿势，trader L330-334 + L458）：

```python
cfg = {"options": {"defaultType": "future", "fetchCurrencies": False,
                   "adjustForTimeDifference": True, "recvWindow": 20000}, ...}
ex = ccxt.binanceusdm(cfg)
ex.load_markets()
ex.load_time_difference()   # 签名请求前必做
```

- 后续：可选 `w32tm /resync` 收时钟偏差（bot 已自校准，不急）。

### 坑 #2：ccxt 4.5.68 隐式方法名与统一符号

- `public_get_premiumIndex` / `public_get_premium_index` 均不存在；
  该端点隐式方法为 `fapipublic_get_premiumindex`（fapi 前缀 + 全小写）。
- 取 mark 价优先用高层方法：`ex.fetch_mark_price(sym)['price']`
  （底层即 /fapi/premiumIndex）。
- 统一符号是 **`BTC/USDT:USDT`**，不是 `BTCUSDT/USDT:USDT`。

### 坑 #3（重要误判复盘）：redirector stub 被当成"殭屍进程/守护失守"

- 现象五连（当时全部矛盾）：
  1. `.venv\Scripts\python.exe` 进程 1.1MB / 0 CPU；
  2. py-spy 报 `Failed to find python version`；
  3. cmdline 显示 venv 解释器、sys.executable 规则却推不出该父子关系；
  4. 五层链：bat → .venv watchdog → base watchdog → .venv bot → base bot；
  5. 心跳 `bot_pid` 指向"壳"进程而非 py-spy 有完整业务栈的真身。
- 真相：`.venv\Scripts\python.exe` 是 **redirector stub** —— 调用即以
  base exe + venv site 注入重新拉起真身、自己留守原地等子退出。
  该五层结构两次独立诞生完全同构 = 系统正常形态。
- 撤回：据此发出的"殭屍占坑 / watchdog 盯殭屍、活 bot 失守 P1"判断**全部作废**；
  守护关系（watchdog poll 壳 = 等真身）实际有效。
- 判读要诀：
  - 壳（1.1MB / py-spy 读不出）= 设计形态，不是卡死；
  - 真身 = 能 py-spy dump 出 monitoring/TG 栈、且 `bot_instance.lock`
    里 `os.getpid()` 的那个进程；
  - 心跳 `bot_pid` 记的是壳，属正常，勿据此改 watchdog。

### 坑 #4：杀进程树后 schtasks 实例悬挂，/Run 静默空转

- 现象：全链 `taskkill` 清零后，`schtasks /Run /TN CryptoBot-Autostart`
  返回 SUCCESS，且 INFO 显示 `currently running`，但 40s 内无任何新进程。
- 根因：杀进程树不会通知任务计划程序，旧实例状态悬挂 → 单实例规则
  静默拒绝新实例，`/Run` 变 no-op。
- 解法：**先 `/End` 清悬挂，再 `/Run`**：

```powershell
schtasks /End /TN CryptoBot-Autostart
Start-Sleep 2
schtasks /Run /TN CryptoBot-Autostart
# 验收三件套：.heartbeat.json 新 pid + .bot_instance.lock 真身 pid + py-spy 栈
```

### 坑 #5：无 ModifyAlgoOrder → 存量单不可原地改

- 币安合约 Modify Order 仅限 LIMIT；条件单无改单端点。
- 故方案 A 是唯一低风险路径：存量 4 单保持 `priceProtect=false`，
  不重挂、不改台账；下一批信号起的挂单自动带 `priceProtect=true`。
- 验证方式：下批成交后任查一张 raw 单，确认 `priceProtect: true` 落地。

## 6. 运维复用模板

- 只读复核三件套：`fetch_open_orders(sym, params={'stop': True})`
  看张数/workingType/priceProtect 字段 + `fetch_positions` +
  `.heartbeat.json` / `.bot_instance.lock` / py-spy 栈。
- 独立写脚本纪律：先抄 bot 的代理（仅 `httpsProxy`）、时间同步、
  ccxt 配置三件套；**用后即删**（先例：_probe_workingtype.py、
  _test_priceprotect.py、_chk_orders.py）。
- run_commands 单次执行上限 30s，禁止在一条命令里塞 `sleep 40`
  以上等待（本次超时截断过一次，改用分段查询）。

## 7. 遗留与后续

1. 下一批次成交后验单：确认新单 `priceProtect: true`（任意 raw 查询即可）。
2. 开机自启 logon 触发 100% 验证需一次真实重启；bat 全流程已两次实证
   （21:31 巡检处置自动走通、22:58 手动 /Run 走通）。
3. 可选：`w32tm /resync` 收 1~2s 本机时钟偏差。
4. D-011 设计草案是否入库仍待定（保持未跟踪）。
5. 本地 frozen-archive 领先 origin 若干条未推送（含本次 2 条），
   推送时机由维护者决定。
