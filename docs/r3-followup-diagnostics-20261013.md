# R3 后续诊断：健康文件替换、Telegram 长轮询与隔离门禁

调查日期：2026-10-13。只读检查了 `G:\my-crypto-bot\logs\bot_20261012.log`、
`bot_20261013.log` 和 `.tg_diag.json` 的指定诊断字段；未读取 `.env`、交易状态或回滚包内容。
本节记录的只读核查本身未触发 API、外发或交易操作；验证过程偏差另见下文披露。

## `.bot_health` WinError 5

- 10/12 日志有 3 条、10/13 有 4 条 `WinError 5`：其中 6 条目标是 `control.json`，
  1 条目标是一个批次进度文件。日志显示每次使用 `.bot_health` 目录下新建的
  `.health_*.tmp` 替换目标，失败发生在 `os.replace`。
- 当前源码 `health_progress._atomic_write` 使用同目录 `tempfile.mkstemp`、写入后
  `flush`/`fsync`，再 `os.replace`。`control.json` 在本进程只有 `bot_runner` 的通知队列
  协程每轮写一次；批次进度由监控写入。健康巡检读取 control 文件。因此没有证据支持
  临时文件名冲突，也没有证据证明是应用内多个 control writer、静态 ACL 错误或特定外部
  进程占用。Windows 替换期间目标被打开、杀软/索引过滤驱动介入等仍是可能性，不是结论。
- **根因未决；不改写入策略、不增加重试或扩大异常捕获来宣称修复。** 当前降级处理只避免
  监控因健康写盘失败退出，不代表该 I/O 故障已修复。
- 安全诊断建议：在隔离 Windows 副本对同目录替换做可控复现；下一次出现时由运维只读收集
  异常的 `winerror/errno/src/dst`、目标是否存在及属性，并使用 Process Monitor 按目标路径
  捕获 `SetRenameInformationFile` 的调用进程和结果（例如 sharing/access denied），同时
  核对相关时间的 Defender/杀软事件。禁止在生产制造占用、改 ACL/排除项或尝试修复性写入。

## Telegram getUpdates

- 10/12 与 10/13 日志中，按 `getUpdates` 诊断请求/成功/异常模式筛查：10/12 没有匹配到轮询
  异常，10/13 找到 2 次异常，耗时分别为 130.02s 和 130.01s；类型链为
  `TimedOut <- ReadTimeout <- ReadTimeout <- TimeoutError <- CancelledError <- SSLWantReadError`。
  这是已尝试网络往返后的读取超时，不是连接建立超时，也不是连接池未发出请求。
- 同一只读诊断状态快照记录 138 次轮询调用、137 个 HTTP 响应且均为有效成功、136 次空结果；
  最近成功响应耗时 10.23s。仓库安装的 python-telegram-bot 22.8 `Updater.start_polling`
  默认 long-poll timeout 是 10s。因此约 10s 后返回空更新属于正常长轮询，不应计作网络失败。
- 当前请求配置是 getUpdates `connect_timeout=30s`、`read_timeout=120s`、`write_timeout=30s`。
  130s 实测与 120s read timeout 的差异需要继续从 HTTPX/SSL 阶段观测解释；现有日志不足以
  证明是代理、服务端、TLS 中间设备或网络栈根因。用户提供的 20/5044 历史比率没有在本次
  可见日志片段/当前计数中得到完整重算。**不调整代理、timeout 或 polling 策略。**
- 建议继续按现有被动探针收集足够窗口的请求计数、状态码、错误类型链和请求耗时；只把
  `poll_success` 之外的异常按类型分类，区分 `ConnectTimeout`、`ReadTimeout`、`PoolTimeout`
  及 HTTP 5xx。保留 token/URL 脱敏。若要定位代理路径，另在非生产网络环境做对照，不改生产代理。

## 完整测试门禁隔离

- `test_p1_settlement_price.py` 原来无条件对 `G:\my-crypto-bot` 的状态文件及 `.env` 做内容读取，
  这会使干净 checkout 的全门禁依赖生产路径并读取凭证文件。现改为只快照当前 checkout 中的
  测试状态文件，并去掉 `.env` 快照；没有改变交易逻辑或生产代码。
- 全量 pytest 暴露另一处本轮相关测试污染：`test_bot_runner_config` 的启动接线测试使用 mock
  Application，却启动了真实无限 updater watcher；watcher 将 `MagicMock` 放入共享诊断状态，
  使后续 `test_tg_diagnostics` 的 JSON flush 失败。现只在该配置测试中 mock watcher 并断言安装
  被调用；真实接线仍由 `test_tg_diagnostics` 覆盖。
- `test_m2_m4_maintenance` 曾要求 checkout 有机器本地 `.env`。干净 checkout 没有该文件时现将
  仅此运行环境相关检查标记为 skip；文件解析与环境遮蔽规则仍由临时 fixture 测试覆盖。
- 按 `run_test_gate.py` 正式忽略规则执行：pytest **340 passed, 1 skipped**；脚本段 **72 PASS，
  1 NOT-VERIFIED，0 FAIL**。整门禁退出码 **2 / COMPLETED-WITH-EXCEPTIONS**：
  `test_orphan_guard.py` 返回 42，因为正在运行的 Bot 持有实例互斥体；未停止生产以强行补验。
  门禁的生产哨兵没有发现 checkout 中被监控文件变化。此结果不是“全绿”。
- 完整 `run_test_gate.py` 仍有生产运行探测；在生产保持运行时如需跑门禁，必须在独立 checkout、
  清理敏感环境变量的进程中显式确认并执行，并审阅 `--allow-live` 含义。测试门禁自述其哨兵是
  事后检查而非隔离；本报告不把该哨兵当作安全隔离机制。

### 验证过程偏差披露

一次早期排查误运行了**未带门禁 ignore 参数**的裸 pytest。它在归档回测目录收集时尝试连接
Binance 公共 `GET /fapi/v1/exchangeInfo`；连接超时且未收到响应。未发出认证 API 请求、未进行
订单操作；该命令随后停止，未再运行裸 pytest。正式门禁及之后按门禁 ignore 规则的 pytest
均清除了 API/邮件凭证和代理环境变量，未触发真实交易或通知调用。此误操作不改变根因判断，
但如实记录为本轮验证边界偏差。

## 范围与未解决事项

本轮不修改健康文件运行时策略、Telegram 代理或 timeout；不修改生产、R3/R2 冻结包或回滚备份。
WinError 5 的具体 Windows 访问拒绝来源和 Telegram 偶发读取超时的网络根因仍未定位，需按上述
只读/隔离诊断方案补证后再决定是否进行运行时代码变更。
