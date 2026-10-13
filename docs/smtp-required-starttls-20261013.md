# 独立 SMTP 候选：必须验证 STARTTLS 后登录

## 范围与依据

独立分支 `fix/smtp-required-starttls`，基线 `36a39ebfda8b17176128c13dbbbeedc33bf990f1`。不包含 #18/#19/#20 的代码，不修改已冻结的通知组合包，不部署。

现场仅连接/握手的证据保存在 `C:\Users\Administrator\AppData\Local\Temp\opencode\notifications-composite-20261013\smtp-tls-result.json` 和 `smtp-starttls-result.json`：当前 465 隐式 TLS 收到明文 220 并出现 WRONG_VERSION_NUMBER；465/587 的显式 STARTTLS 均通过默认系统信任及主机名校验，TLSv1.3、TLS 后 EHLO 250。它证明当前路径的协议匹配情况，不证明凭证、发信或最终收件正常，也不能据此归因到某个代理/服务商层。

## 实现

`smtp_transport.send_qq_mail()` 是三个调用方共用的纯标准库传输函数：

1. SMTP 连接 `smtp.qq.com:587`。
2. EHLO 必须成功且服务器声明 STARTTLS。
3. `ssl.create_default_context()` 强制证书信任和主机名验证；STARTTLS 失败立即退出。
4. TLS 后重新 EHLO，并要求成功。
5. 仅此后调用 LOGIN，再提交邮件。

不回退 SMTP_SSL/其他端口，不关闭证书验证，不支持明文登录，不自动重试。连接、EHLO、扩展缺失、握手/证书失败时不能调用 LOGIN 或 sendmail。认证/提交失败或响应丢失时向调用方抛错，不自行再次提交。

trader、Bot 崩溃邮件、独立健康巡检三个入口都使用同一函数。原消息构建、邮件事件闸门、串行锁、异步线程、wait/join 及通知预算不改；它们的邮件策略差异也不在本候选统一。

## 成功与已知边界

- helper 正常返回表示 SMTP 接受此次消息，不表示最终进入收件箱或被阅读。
- wait=False 仍只表示发送线程提交；wait=True 仍有既有 20 秒 join 期限，不是整个 SMTP 会话必在 20 秒结束的保证。
- KNOWN_LIMITATION：既有调用方可能在等待超时/迟到结果或未确认结果之后重试，仍可能重复发信。本候选不新增重试，也不宣称修复所有调用方投递去重。SMTP 接受后 QUIT/连接失败的未知结果同样不能被猜成未发送。
- UPGRADE_TRIGGER：若迟到结果或提交后断线实际导致重复邮件，应另行交付带持久事件标识、在途预算和结果回灌的投递生命周期；不要在传输函数中无条件重发。
- 当前系统网络路径仍可能受 TUN/代理/路由影响，巡检邮件不再宣传“必然国内直连”。TLS 失败时宁可报告失败，也不泄露凭证。

## 离线验证

85 passed、21 subtests passed：`test_smtp_transport.py`、`test_email_gate.py`、`test_email_channels.py`、`test_notification_email_policy.py`、`test_r0_notify_chains.py`。

新增传输测试覆盖准确的 EHLO→STARTTLS→EHLO→LOGIN→sendmail 顺序、默认上下文验证属性、服务器不支持 TLS、TLS 被拒、握手/证书失败、TLS 后 EHLO 失败、认证失败、收件拒绝、提交后断线、连接超时，以及三个实际邮件入口的成功/失败和闸门先于连接。

鉴权专项 15 场景、队列专项 19 场景通过；语法编译和 git diff --check 通过。使用假 SMTP、假凭证及测试替身，主验证进程阻止外部网络和生产目录写入；本轮没有重新连接真实邮箱、登录或发信。

这不是完整仓库门禁，不是邮件实际送达验收。实际生产通道继续标记“邮件不可确认正常”。

## 合成与发布条件

本候选需独立定点复审。通过后重新生成包含它的新组合包，不修改或复用旧冻结包的 hash。

新增运行依赖 `smtp_transport.py` 必须随三调用方一起纳入文件白名单：bot_runner.py、trader_260725.py、健康巡检.py、smtp_transport.py；不能只替换一个调用方而遗漏 helper。保留生产 TG 配置、allowed_updates、TGDIAG、PnL worker overlay 与所有运行账本。

组合测试时，PR #19 的 `test_actionable_notifications.py` 内旧 SMTP_SSL 传输替身需改为 SMTP/STARTTLS 替身，补齐 EHLO 和 sendmail 返回；原安全/通知断言不能删除。

实际部署、正常重启和一次真实邮件自检需另行授权。没有改生产 SMTP 代码、.env、邮箱凭据、交易所白名单或鉴权锁，不会因此自动解除 AUTH_BLOCKED。
