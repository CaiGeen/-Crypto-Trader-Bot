# SMTP + 通知组合 R3 复审记录

## 审查结论

PR #21 `fix/smtp-required-starttls` 的传输实现通过定点审查：三种邮件入口共用标准库 helper；SMTP 587；EHLO 成功且具 STARTTLS 后，使用默认 SSL context 验证证书/主机名；STARTTLS 后重新 EHLO；确认 TLS 前不传凭证或邮件；不降级、不由 helper 自动重试。SMTP 接受不等于收件箱送达；连接关闭/等待超时的既有上层时序仍可能出现未知结果或重复，未声称修复投递生命周期。

PR21 head 后续为 `75493ac2458b776774de48ba76537ecdbf8a8e4a`：仅更新 SMTP egress tripwire 的注释和测试说明，使生产传输描述与 STARTTLS 一致；实际 tripwire 覆盖行为不减。

## 复审中发现并修复

- PR19 的 `test_actionable_notifications.py` 曾只 patch `SMTP_SSL`。改用 STARTTLS 后该替身会漏掉新的 `SMTP` 构造，测试有意外连接真实网络的风险。PR19 更新 head `ac5acf341e10833e3506493f0c272823045b4538`，同时 patch 两种传输，以便在 PR19 旧实现和组合后的 STARTTLS 实现中都不外连，并断言当前实际 transport 的调用/失败顺序。该测试在 PR19 单独树 17 passed，在组合树亦 17 passed。
- 组合树的 `test_tg_diagnostics.py` 接线测试原先依赖 worktree 外部的合成 `.env`。改为每个测试在 `tmp_path` 创建假项目配置；避免为测试准备未追踪的根目录配置。
- `test_egress_tripwire.py` 的 SMTP 类级拦截仍覆盖 SMTP 与 SMTP_SSL；更新过时的“SMTP_SSL 是生产路径”描述。SMTP F5 定点通过，完整 tripwire 10/10 通过。

## R3 定向验证

- 合并组合测试：**261 passed，69 subtests passed**。包含 PR21 的 transport、三个邮件调用方、闸门/失败语义、SMTP policy、通知队列、TGDIAG/config-pin、worker 与 accounting 定点测试。
- 鉴权专项 15 场景、通知队列专项 19 场景通过。
- egress tripwire：10/10；SMTP F5 验证通过。
- `py_compile` 与 `git diff --check` 通过。
- 使用假凭证、假 SMTP、合成项目 `.env`；主验证进程禁止非 loopback 网络及生产目录写入。临时合成 `.env` 已删除，不进入发布包。未连接/登录真实邮箱，未发真实测试邮件。
- **完整仓库门禁未运行。** 以上是定向代码验证，不是实际邮件送达、交易所账户或生产验收。

## 部署范围与限制

R3 运行时替换白名单：`bot_runner.py`、`trader_260725.py`、`健康巡检.py`、`smtp_transport.py`。后者为新 helper；三调用方必须原子一致部署。`tg_diagnostics.py` 保持生产原字节，不为换行/归档改写。SMTP PR #21 加入本组合；`.env`、邮箱凭据、账本、通知队列和交易所状态不复制、不回滚。

生产已有 `auth_blocked.json` 的 `-2015` 状态。用户报告已把 IP 加白，但本次未调用 `/auth_reset`、未探活/对账、未清锁。重启不得被表述为交易恢复；恢复仍须既有探活→对账→解锁流程及单独操作授权。

邮件候选采用 SMTP 587 STARTTLS。**不进行真实邮箱登录或测试发信**；真实凭据和实际投递仍未验收。R3 部署后只验证代码、启动、TG/worker 等运行情况；生产事件发生时的正常邮件通知可按代码策略发送。

R2 冻结包及 manifest 保持不变。R3 使用独立 bundle、生产字节 rollback 和新 hash；本记录不代表已合并或已部署。
