# Telegram 配置导入候选：反馈处理记录

## 来源与范围

本候选在独立 `fix/telegram-config-review` 分支上，以发布线 `36a39ebfda8b17176128c13dbbbeedc33bf990f1` 为基线。

只读核对原 `fix/telegram-config-import` 工作树的未提交改动后，移植其 Telegram 配置函数、main 启动固化及过期邮件配置横幅说明；原工作树未被修改、提交、清理或搬走。未混入 PR #18 TGDIAG 或 PR #19 通知治理。

## 逐项反馈

1. **核心固化顺序缺测试：采纳并修复。** 实际调用 `bot_runner.main()`，项目配置 A 与继承配置 B 不同；在单实例锁、配置横幅、两个请求对象、Application token/build、run_polling 边界均断言环境和模块变量已经是 A。网络、真实锁、交易实例和 polling 均由测试替身隔离。另测文件缺失、token 错误和 `--5` 在任何启动动作之前失败，原环境/全局值不被半更新。
2. **生产目录仍是旧实现：接受事实，但不采纳“直接移植到生产”。** 这是尚未部署的候选，不是此次工作树未实现。未确认生产部署组合包含本候选；不能因此宣称生产已修复。实际部署需单独审批、保留获批 TG 配置隔离、TGDIAG、worker 等 overlay，不得将仓库 bot_runner.py 整份覆盖生产版。
3. **鉴权场景 fixture 泄漏全局变量：采纳并修复。** 场景 S15 始终在 `mock.patch.object` 中使用明确的测试身份，不再条件赋值全局。测试验证原值为 None/已配置时均恢复，并验证场景抛错也恢复。缺配置启动失败有独立 main 测试，不用 fixture 冒充配置验收。
4. **`--5` 被接受：采纳并修复。** loader 与导入期的继承 ID 解析使用 `-?[0-9]+`，保持原有负数/数字 ID 兼容，但不接受多重负号。main 在更改环境之前完成整数转换；无效继承 ID 不阻断模块导入，无效项目 ID 仍拒绝启动。

## 验证

- 新配置测试覆盖读取、缺文件、无效 token/ID、实际 main 固化顺序、startup fail-closed、新进程无 TG 配置/无效继承 ID 的导入，以及 fixture 成功/异常两路恢复。
- 针对性回归：50 passed，46 subtests passed，1 deselected（四个测试文件：config、notification_email_policy、email_channels、m2_m4_maintenance）。
- 未筛选的扩展回归曾得到 50 passed / 1 failed：`test_m3_risk_keys_match_runtime_parse_and_process_value` 要求当前工作树 `.env` 显式包含风险参数；隔离工作树没有该文件，失败于缺少 `RISK_MAX_ACTIVE_BATCHES`。没有修改该测试或复制生产 `.env`；后续明确排除这一环境依赖项，不声称完整门禁通过。
- `test_auth_blocked.py` 是场景脚本，非 pytest 测试。清空继承 TG 配置后执行，15 场景全部通过（包含 S15）。
- 语法编译及 `git diff --check` 通过。
- 主验证进程有非 loopback 网络/生产目录写入护栏；新进程 import 验证仅导入模块，不启动 main、网络客户端或交易引擎。测试未读取/复制生产凭证，未发真实邮件或交易所请求。

## 发布状态

生产 `G:\my-crypto-bot` 与旧候选保持原样。没有 merge、deploy、重启或账户操作。

本记录仅证明隔离候选处理了三项代码/测试反馈；生产发布及现场接受仍未完成。后续部署验证应证明被批准的配置改动已按文件级组合进入实际运行源，而不是仅凭分支存在或 Bot 存活认定成功。
