# 生产 → 候选：83-hunk 逐块语义核对（供独立复审）

> **历史快照记录**：本报告针对 `3077a80` / `969B8138…8DD4`，不是后续修订候选的通过证明。独立隔离探针已将 U2、U3（跨锁补证）、U5 裁为阻断；下一轮定点修复与验证见 `REVIEW_FIX_U1_U5.md`。以下83项及U0–U8保留原核对记录，不能用来宣称新候选硬门关闭。

## 对象与结论口径

- 源码评审 head：`3077a800b4831cfbe8e6c206ca2f4283de2dc050`（PR #8）。
- 生产 SHA256：`98DA5139B67E14EA6A6F3207DDA5E8E05DAB8C29A8E0C73ECFD731DAA5AE65EC`。
- 候选 SHA256：`969B8138F30FEBB33F712603A54D8A2B3F787270D656BE6D8D296D3DD9DD8DD4`；Git blob `06123e55b33c2bd77b3c54fedbcb1aedd40c6fbb`。
- 用完整源码重新生成 `SequenceMatcher(autojunk=False).get_grouped_opcodes(3)`，得到 **83/83 个 hunk**，编号对应 `PROD_TO_CANDIDATE_HUNK_INDEX.md`。个别边界因匹配上下文差异相差一行，不改变块编号或内容。
- **83/83 已逐块检查；不等于 83/83 通过。** 每块修复归属与行为变化见下表；无法确认的边界见 U0–U8，交独立复审裁定。
- 本次仅阅读源码、对比差异、检查已有测试断言与 AST；未修改引擎、测试或生产配置，未合并、未部署。候选仍冻结。
- “对应修复”是源码及仓内回归用例能支持的归属，不以注释、测试绿灯代替原始逐轮批准证据。完整历史审批追溯尚未证明（U0）。

## 修复组与现役验证线索

| 组 | 修复目标 | 直接测试候选源码的主要线索 |
|---|---|---|
| F2/F3 | 真实成交价、身份/数量证据、缺成本待补证、幂等结算、预算退避 | `test_f2f3_fill_cost.py` T1–T19；`test_monitor_resume.py` c25–c27 |
| R1/R1b/R1c | 先撤 ENTRY 后核事实再撤保护、无单号意图解决证据、订单层绑定及热收编 | `test_r1_entry_before_fact_check.py`；`test_converge_post_cancel.py`；`test_monitor_resume.py` c8–c19 |
| R2/R2b | algoId→actualOrderId 身份链、真实触发事实、普通订单身份/终态/数量 | `test_converge_post_cancel.py`（含 A1 原始夹具、symbol 正负对照、完整性/预算场景） |
| R3/R3b/R4 | 有界同代次续跑、超限内存锁/残余风险、统一 clear 门、单一外层 finally | `test_monitor_resume.py` c1–c7、r4a/r4b；`test_p5_closecancel.py` |
| per-read | 用本次读取结论保护写盘、生命周期、clear 与墓碑终结证据 | `test_monitor_resume.py` c20–c24；`test_d009_state_persistence.py`；已有状态写盘/事务测试 |
| 心跳 | OSError 就地隔离，心跳失败不杀交易监控 | `test_heartbeat_isolation.py`；`test_monitor_resume.py` |

`test_close_confirmation_v6/v62.py` 测仓内 helper/文档代码块，`test_v62_red_first.py` 测旧基线，**不能充当当前引擎覆盖证明**。本轮不继续扩展测试整理范围。

## 逐块记录

“核对”表示已读实际删增代码并核对相邻控制流；带 U 编号的项有待独立确认，不应标为批准。

| # | 生产→候选起始行 | 对应修复 | 实际行为变化、保留约束及核对结果 |
|---|---|---|---|
| 1 | 99→99 | R1/R2b/R5 | 新增局部不重试错误码、订单终态集、500 条满页阈值及 symbol 归一化。追踪消费者：不重试参数只有 converge 撤单使用；终态集/满页阈值用于撤后证据；三处身份链先去 settle 后缀再去分隔符，错交易对仍拒绝。与身份/分页回归相符；不修改全局终态守卫。 |
| 2 | 1361→1390 | F2/F3，R1–R6/P1 | 新增六个 helper：解析响应身份/方向/数量/真实价；记录证据及待办列表；锁外有界补证、锁内写价/补费；结算门同时拦数量未知与成本未知；待结算载荷/phase/reason 一次写；finalizer 重读账本、缓存费用、dedup PnL 后再清理。删除触发价冒充成本的替代链在 #55。T1–T19 有直接断言。并发重读后身份/计划量与事务代际是否仍一致，不能仅凭队列存在证明（U3）。 |
| 3 | 1375→1946 | F2/F3 预算说明 | 只扩充 `_resolve_order_fees` docstring；区分成功端点调用与包装层重试，解释费用缓存失败退避在 finalizer 而非解析器内。无执行变化。 |
| 4 | 1399→1983 | F2/F3-R3 | algo 映射查询显式 `retries=2`；成功解析及失败降级流程未变。 |
| 5 | 1410→1996 | F2/F3-R3 | 手续费 `fetch_my_trades` 查询显式 `retries=2`；订单号过滤及数量验收未变。 |
| 6 | 1446→2032 | F2/F3-R3 | 普通/algo 权威数量兜底都显式 `retries=2`；只收窄该调用重试预算。 |
| 7 | 1533→2121 | per-read | 数量冲突 BEGIN 锁内读取改为 ex 三元组，保留 active/订单代际/phase 条件；与 #8 成对。 |
| 8 | 1561→2149 | per-read | 同次 corruption 传给 BEGIN 持久化；损坏占位数据不得因共享标志复位而覆盖磁盘；原失败返回保留。 |
| 9 | 1597→2185 | per-read | 数量冲突 finalizer 在 PnL 后推进前重读 ex 三元组；既有 op_id CAS 保留。 |
| 10 | 1605→2193 | per-read | 推进 manual_review 的写盘使用 #9 同源状态；失败继续保留 dedup 后续跑语义。 |
| 11 | 2388→2976 | 局部撤单不重试 | `_safe_api_call` 增加 `no_retry_patterns=()` 参数，默认调用面不改变，参数由包装层消费而非转发端点。 |
| 12 | 2447→3036 | 局部撤单不重试 | 错误字符串转小写后命中指定模式直接重新抛出；只有 #76 传入，其他调用仍走原鉴权/网络重试分支。 |
| 13 | 2619→3216 | per-read 文档 | 解释共享 corruption flag 的并发局限及写盘调用迁移；不改读取实现。AST 实数 27 个 persist 调用均传 `read_corrupt`。 |
| 14 | 2686→3286 | per-read | `_persist_states` 增可选 `read_corrupt=None`，保留旧直调兼容，不改原子写/备份流程。 |
| 15 | 2703→3303 | per-read | 显式本次 corruption 优先于共享字段；仅未传时回退。正常空账本不因“空”被拒绝，损坏 True 拒写；不是载荷启发式。 |
| 16 | 2739→3346 | R1c 并发写保护 | `save_batch_state` 增 `allow_chain_shrink=False`；默认保护绑定链，唯一显式 opt-out 在 #55 有意收缩路径。 |
| 17 | 2750→3358 | R1c / 缺价保护 | 增数组拒写状态、有限正价 helper 与同锁对齐契约；网络告警安排在锁外。此处先建立保护框架，执行逻辑在 #19。 |
| 18 | 2769→3396 | per-read | save 锁内读取改 ex 三元组；墓碑存在性、降级拒写及 merge 入口保留。 |
| 19 | 2782→3409 | R1c / F2/F3 / 外审7 | 默认在状态锁内按订单身份合并并校验数组；对齐失败拒写并锁外去重 critical。merge 后已成交层缺价优先恢复证据价格，否则登记 cost_pending；保护块异常拒写。最后持久化传 #18 本次 corruption。c14–c19/c25/c27 提供断言。代价是错长数组导致所有带链写入拒绝；可信价格/旧 qty_unverified 的升级边界不能由此块单独确认（U4）。 |
| 20 | 2887→3637 | per-read / F2/F3-P1 / R3b | clear 用本次不可读结果及详情，未知不再幂等 True；proof 后同锁拒绝未记 PnL 载荷、内存 no-clear latch、持久超限标记或残余风险。原授权 CAS/墓碑流程保留。c7/c24/T17 是直接线索。 |
| 21 | 2969→3759 | per-read | clear 删除写盘使用 #20 同源 corruption，仍要求 durable 写成功。 |
| 22 | 2981→3771 | per-read / R3 | 失败文案用本次读状态；只有 durable 删除成功才回收内存锁，失败/拒绝不回收。不存在账本的幂等早返回不新增副作用。 |
| 23 | 3838→4637 | 外审7 `/tp` | 在校验和撤单前检查 cost_pending 及已成交前缀每层有限正价；失败 critical+False，旧 SL/TP 不动。c26 直接覆盖，未改 TP 定价公式。 |
| 24 | 4312→5144 | F2/F3 `/be` | 在撤旧/建新前阻断成本待补证或前缀缺有效价；已有 SL 保持。与 c25/T5 相符，非法层字段可抛出而不是继续撤单；不是新增自动修复。 |
| 25 | 5156→6021 | per-read | partial close 减仓归账锁内读改 ex，op/phase CAS 保留。 |
| 26 | 5172→6037 | per-read | partial close 写入传 #25 同源 corruption，失败不确认净仓进度。 |
| 27 | 5254→6119 | per-read | resize 收编 SL 的锁内读改 ex；订单代际条件不变。 |
| 28 | 5263→6128 | per-read | resize 收编 SL id+stage 同次写传 #27 状态；不改撤/建顺序。 |
| 29 | 5359→6224 | per-read | resize 分腿提交前改 ex 重读；事务 CAS 不变。 |
| 30 | 5372→6237 | per-read | 分腿写传 #29 corruption；最终 ACTIVE 迁移前另一次 ex 重读，两个临界区分别绑定本次读。 |
| 31 | 5395→6260 | per-read | resize 最终 ACTIVE/stage/限价镜像清理写传 #30 最后读取的 corruption；失败保留事务态。 |
| 32 | 5476→6341 | per-read | closecancel TP registry rearm 的锁内读改 ex，原 exception 分类保留。 |
| 33 | 5508→6373 | per-read | rearm 持久化传 #32 同源状态；原既有 rearm 条件不变。 |
| 34 | 5522→6387 | per-read | closecancel 成交归属提交前改 ex 读；原净成本分摊、op 校验不变。 |
| 35 | 5542→6407 | per-read | 归属/restore_pending 同次写传 #34 corruption；落盘失败不报 attributed。 |
| 36 | 5619→6484 | R3 异常分类/超限 | 新增只读恢复分类；人工冻结、结算态不续跑；普通活跃/平仓在途/遗留 pending_close 可有限续跑。新增进程内 no-clear 锁及残余风险登记：先锁再读写，持久化失败不恢复清账授权。跨重启保证依赖持久标记，不是内存锁持久化。通知前早返回/分类再同步窗口需关注 U6；c1–c7 是直接线索。 |
| 37 | 5640→6657 | F2/F3-P1/R5 | finally 决策新增 cost_pending reason 或非空待结算载荷即 skip；未把超限提前 skip，保留限价分型/迁移入口。清账超限门另在 #20/#58–61。 |
| 38 | 5685→6725 | per-read | 限价人工冻结登记锁内读改 ex，原 phase/reason 条件保留。 |
| 39 | 5707→6747 | per-read | 冻结登记写传 #38 corruption；写成功后的原告警节流不变。 |
| 40 | 5856→6896 | F2/F3 / per-read | 限价全量成交 finalizer 在认领/结算前调用成本门；缺成本维持 phase2 待续跑，不记 PnL。门后锁内重新读 ex（不用补证前快照）。 |
| 41 | 5872→6918 | per-read | 限价 finalizer 认领写传 #40 同次读状态，原 claimed 幂等行为保留。 |
| 42 | 5971→7017 | per-read | 生命周期守卫增加可选 `read_corrupt`；兼容原两参测试桩。 |
| 43 | 5979→7026 | per-read | 守卫先判本次损坏→unknown，再判可信缺失→exit；共享字段只作未传参数回退。主循环 G1/G2/G3 及恢复同步接线见 #55/#56；c20/c21 验证竞态。 |
| 44 | 5999→7052 | per-read | settlement_reported CAS 的读取与写入均迁移同源三元组；persist-before-send、至多一次报告的原取舍保留。 |
| 45 | 6795→7848 | per-read | execute_signal 的 PROVEN-CLEAN 空骨架停用锁内读改 ex，原“发现绑定/未决则拒绝 clean”条件保留。 |
| 46 | 6808→7861 | per-read | 停用写传 #45 corruption，False 不报 CLEAN_REJECT。 |
| 47 | 7453→8506 | per-read | G3 保护提交写使用已有 `_ledger_corrupted` 本次读变量；persist_failed 返回保留，不新增订单权限。 |
| 48 | 7650→8703 | per-read | registry 更新锁内读改 ex；仍拒绝批次消失。 |
| 49 | 7688→8741 | per-read | registry 更新写传 #48 状态，计数/状态迁移逻辑不变。 |
| 50 | 7757→8810 | per-read | registry 多字段事务锁内读改 ex；原空事务幂等保留。 |
| 51 | 7783→8836 | per-read | registry 事务写传 #50 状态，返回落盘结果；没有替调用方新增成功授权。 |
| 52 | 8611→9664 | per-read 终结证据 | `_monitor_terminal_evidence` 改用本次读取损坏判据，未知账本不能凭有效墓碑确认已终结；可读缺批次后仍走墓碑校验。c22 是直接竞态负例。 |
| 53 | 8762→9818 | 心跳事故隔离 | 新增 `_safe_write_progress`：只捕 OSError（含 PermissionError），首报队列失败也隔离，失败不刷新成功心跳、不写 monitor_error，300s print 节流；非 OSError 仍外抛。未动健康模块/TG 实现。 |
| 54 | 8856→9969 | F2/F3 恢复 | 监控启动处理 cost_pending_settling 调真实 finalizer，失败短暂等待，不重建订单。与 T8/T12 等恢复路径对应。 |
| 55 | 8930→10050 | R4/R1c/F2/F3/per-read | 大块不是“纯缩进”：外层 try/finally 包主循环，内层每轮 except 允许续跑；G1/G2/G3 使用本次读；心跳两处改安全包装；ENTRY 热收编只扩展一致前缀，继承已记账层数/费用；成交不再用委托价/触发价或 price_to_precision 冒充真实价，分 confirmed/cost_pending/qty_unverified；待补证先回填；新增数组对齐与冻结下事实入账；成本待结算在归零/维护前转 finalizer，SL/TP 触发结算先成本门。保留原批次/订单未知、保护确认、限价事务冻结控制流。有意收缩是唯一 allow_chain_shrink opt-out。#56 共同构成一次 monitor 重构，不按重排行数宣布等价。相关 T1–T19/c8–c21 可支持目标行为；U3/U4/U6/U8 尚不能排除。 |
| 56 | 9723→12436 | R4/R3，监控结构迁移 | 巨量删除包含原监控维护后半段在 #55 的迁移，不代表删除 SL/TP 管理。通过去行首空白辅助差异再回原源码核控制流：两处 `_persist_guard_arrays` 拒绝时不落盘；正常周期仍按订单/持仓/保护/durable 状态交接；异常优先 owner+分类→最多3次续跑（>600s重置），同步进度只升不降/pending并集/失败计数max；不续跑才告警+owner写 monitor_error+break；处置异常及 BaseException 自然经过唯一外层 finally。r4a/r4b 检查结构与处置层 KeyboardInterrupt。finally 内异常的完整收尾能力是另外边界（U6），并未因 finally 结构自动证明。 |
| 57 | 11202→12772 | F2/F3-R5 | finally 对 skip+cost_pending 仅说明移交，不调用 converge/clear；实际授权由 #37 控制。 |
| 58 | 11226→12801 | R3 | 收尾加入账本超限标志或当前进程内锁合并闸门；读异常也禁止 clear。只撤销清账授权，分类/迁移继续。`load_all_states` 的损坏占位不会抛异常，局部 read_ok 不代表读取可信，最终 clear per-read 兜底；不能把本段独立称全能读取守卫（U6）。 |
| 59 | 11249→12860 | R3 | 程序撤单收尾先看超限门，超限即便 proof 到手也不 clear；原有限 converge 重试保留。 |
| 60 | 11301→12916 | R3 | 零仓位收尾同样禁止超限 clear，仍保留授权快照 CAS。 |
| 61 | 11321→12939 | R3 | 聚合仓来自其他批次/本批零成交的第三个清理点同样拦超限；未放宽归属/proof 条件。 |
| 62 | 11969→13590 | per-read | close BEGIN 锁内读改 ex，原 active/phase/reason/op 事务条件保留。 |
| 63 | 12017→13638 | per-read | close BEGIN 持久化传 #62 状态；persist失败保持原 grace回滚与无订单权限。 |
| 64 | 12125→13746 | F2/F3 安全退出 | derive 一票否决 qty_reconcile_pending；成本前缀从“必须正价”改为“正价或持久化 cost_pending 证据支持的 exact0”，尾部仍 exact0。证据需状态/层范围/有限数量/计划容差，已有方向/id冲突拒绝。但缺 side/id 并未强制拒绝，不能称四类证据全部齐全（U1）；T5/T9 支持规范输入，不证明缺字段边界。 |
| 65 | 12213→13894 | per-read | close rollback 锁内读改 ex，原 op_id CAS 保留。 |
| 66 | 12232→13913 | per-read | rollback 写传 #65 状态，失败仍说明磁盘phase1、不报已恢复。 |
| 67 | 12258→13939 | per-read | close reason 迁移锁内读改 ex，首异常根因/事务owner条件保留。 |
| 68 | 12280→13961 | per-read | reason 写传 #67 状态；原异常原因不覆盖规则保留。 |
| 69 | 12563→14244 | F2/F3-R1 | survey topology 增成本证据可放行集合参数，不改数组形态、范围、尾部exact0。 |
| 70 | 12581→14267 | F2/F3-R1 | topology prefix 接受证据支持exact0；局部 `_cost_ok_of` 复制状态/量/方向/id检查，使成本未知仍能安全退出。与 #64 有输入类型差别（strict numeric vs `_fee_float`）及缺字段容忍（U1）。 |
| 71 | 12597→14335 | F2/F3-R1 | survey 对每批把证据集合接入topology，无法证明coverage仍 -1三元组；不是无条件放开零成本。 |
| 72 | 12970→14708 | per-read | 限价close Commit锁内读改ex；订单/事务身份校验不变。 |
| 73 | 12988→14726 | per-read | limit id/price/mode/reason一次写传#72状态；失败不报committed。 |
| 74 | 13426→15164 | F2/F3-P1 市价结算 | MARKET确认成交后，限价撤单移到成本判断前；成本门不就绪先仅取出场费、原子登记待结算，不记PnL/不clear。门通过后重新读取同事务账本，验证证据队列/derive/净成本，再计算均价与入场费，拒绝旧0成本首记。失败转待核验结算，无法登记则critical/False。T16/T17/T18覆盖规范路径；future并发 op/账本迁移与finalizer CAS一致性待U3。 |
| 75 | 13449→15325 | 市价代码迁移 | 删除原靠后 `_cancel_limit_close_order` 调用，已搬到#74成本分流前，不是漏撤限价单；仍在MARKET确认之后，并非“先撤再平”的顺序改造。 |
| 76 | 13627→15500 | 局部撤单不重试 | converge撤单传#1错误码，命中即重抛，由本函数原三态转absent/canceled/failed；不是从撤单absent推断未成交。与#77核事实协同。 |
| 77 | 13662→15536 | R1/R1b/R1c/R2/R5 | 新增贡献扣减唯一实现（#79旧逻辑抽取）、时间窗口完整性、ENTRY绑定、数组守卫、无单号意图解决证据、撤后事实复核、algo身份链与普通订单成交证据。先重读/重算持仓，再按未入场vs已入场分型；未知/正向成交拒proof；普通单身份+终态+有限量，algo真实trigger/actualOrderId而非缺actualQty造0；满页/过期/非法响应拒clear。核完785新增行。已成交批次分支不逐单核新增ENTRY（U2）；非法时间id窗口fallback（U7）；数组缺价及身份信任与#19/#64关联（U1/U4）。 |
| 78 | 13673→16332 | R1 契约说明 | docstring新增“归属→撤ENTRY→撤后事实→撤保护”顺序；实际执行在#80–82，不以说明替代代码。 |
| 79 | 13710→16374 | R1 贡献扣减复用 | 旧贡献扣减代码移入#77 helper，调用方按fail分类保留原拒绝/告警；原同方向贡献/反方向不扣/未知方向拒绝保持。原非法数值与负贡献口径未在本轮重新设计，属于继承边界。 |
| 80 | 13784→16416 | R1 两阶段收敛 | 将entry_orders并入L1和证据集，无法归类先视保护单延后撤；他批owned ids排除保留。entry阶段匹配ENTRY即撤，protect阶段前先裁无法归属+未决ENTRY、收集无单号意图并真实核事实，失败return None保留SL。新calls是调用点计数而非HTTP流水（U5/U8）。 |
| 81 | 13827→16558 | R1 证据接线 | L2匹配ENTRY撤成功后加入证据ids与matched identities，后续逐单/无单号分流可看到该单；不只是registry标终态。 |
| 82 | 13834→16568 | R1c / 两阶段收敛 | L2 ENTRY写registry后尝试绑定订单与层、转既有监控链；失败打印，返回值未作新的proof硬门。保护phase保留L2匹配撤/L3只列示；复扫两源的原 `or []` 保留，只增计数。绑定失败接续和None复扫边界未确认（U2/U5）。 |
| 83 | 13884→16652 | proof事实来源/预算 | proof.position_zero从硬编码True改为已核 `_post_fact.ok`；scope仍按本次开始读的b_data描述、不拿改scope放行。新增post证据、调用点计数与包装/HTTP分层预算说明。文案“不重试-2013”与实际仅局部cancel传参不完全相符、复扫失败计数非发起即计，需U8复核。 |

## 未能确认项（交独立复审；不自动视为新缺陷或批准）

### U0：历史批准归属证据不足（全部 hunk）

源码和测试足以解释“做了什么”，不能独立证明每个累计变更都曾获逐轮批准。现有发布说明/注释中的R编号与测试名称仅是归属线索。本报告未逐页验证冻结v8 ZIP内全部原始评审裁决，故不能声称“83块均已审、无夹带”这一发布硬门已经完成。请独立复审结合冻结包原始裁决确认；本轮无需用户参与代码判断。

### U1：成本待补证的身份完整性（#64、#69–71）

`_derive_close_txn_vars` 与 survey 的成本例外只在 side/eid **存在且冲突**时拒绝；缺 side、缺 order_id，乃至部分链形态，未都成为拒绝条件。规范生产写入方会提供这些字段，但能否把旧/坏/并发持久证据视为可信未证明。两处数值判据也不完全同形：derive `_fee_float` 可解析数值字符串，survey只认非bool的int/float。请核“正常写入不变量是否足够”，不要把文档“完全一致”当事实，也不要只凭T5/T9正反例扩充成全部输入覆盖。

### U2：已有成交批次与新ENTRY/绑定失败（#77、#80–82）

`_post_cancel_fact_check` 的 `_has_fill` 分支在 last_filled_count>0 且filled_details够长、持仓贡献归零时返回ok；不会像未入场分支那样逐一核本轮所有ENTRY ids的撤后量。若旧成交已平、另一ENTRY在撤单窗口成交且聚合贡献读到旧事实，是否会漏新成交需独立检查。`_bind_converged_entry_order` 返回False也未被调用点用作proof硬门；其失败可能只影响接续，而非所有clear场景。已有满成交/部分成交竞态用例主要不能单独证明全部“已有前缀+新增层”组合。

### U3：补证/待结算跨锁与事务CAS（#2、#55、#74）

补证在锁外使用旧entry id/计划量查询，锁内重读后检查队列成员但未明确重新比较id/目标量；finalizer `_persist_payload` 更新也未校验本次payload op/dedup仍属同代事务。`_defer_settlement_for_cost` 只在当前/请求op均非空且不同才拒绝。是否已有单飞/链单调约束足以排除这些迁移窗口，需要独立复审，不能仅凭重复调用幂等测试确认并发CAS完整。

### U4：save缺价自愈的证据信任/升级（#19、#55、#77）

save可用fill_evidence中的有效price恢复缺价，未在这块再次核同订单/数量/status；无法恢复时直接更新status为cost_pending，也没有同步重派生qty_reconcile_pending。这是否能把旧qty_unverified证据换成成本待补证，或遗留数组修复是否只允许既有可信状态，需要结合merge规则/上游状态来源确认。已有c25–27锁住缺价/异常拒写，但不能自动等同所有证据污染组合。

### U5：两源扫描/复扫的遗留None→空（#82 相邻未改代码）

converge初扫与复扫仍有 `fetch_open_orders(...) or []`；API异常拒绝，但静默None被当空，未被本次ENTRY gate新测试覆盖（该测试是另一个函数）。属于生产已存在逻辑，本轮只重排调用次序/加事实核验；新增事实核验能否完整弥补未知挂单面尚不能确认。不能因为历史ENTRY gate的 `or []` 消融已通过就声称converge也无同型边界。

### U6：finally/超限/分类读取的保证边界（#36、#55–61）

外层finally保证进入收尾一次，不保证收尾内部（如remove_batch、决策读取等）异常后剩余语句全部执行或原异常不被替换；r4b只证明特定处置层异常穿出会进入finally。收尾read_ok=True也不区分`load_all_states`静默损坏占位，最终clear的per-read门仍拒写，但不能称整个收尾都由本次读守卫。恢复分类与后续同步分开读，第二次读只检active，冻结状态是否可在窗口变化需核。超限登记批次缺失/读异常早返回不发本函数critical，虽内存锁已置，人工处置告警是否由外层充分补足未确认。

### U7：成交窗口fallback及外部事实（#77）

`_fill_window_cover` 对无法解析/未来batch_id返回最近6.9天且complete=True；规范batch_id生成器与时间解释（本机time.mktime）是否覆盖所有历史/迁移批次不能由本函数证明。超过窗口/满500拒绝的收紧是明确的，但“未满页即完整”还依赖交易所实际响应语义，本轮不新增在线探测。不能将仓内fake返回等同生产交易所完整性实证。

### U8：预算/日志声明与实现（#53、#55–56、#80–83）

调用点计数并非底层HTTP请求数；rescan两次在都成功后才加2，失败路径不能说均发起即计。普通fetch_order默认重试5，不带局部no_retry参数，因此proof文案把-2013一概算一次尝试不准确。心跳恢复是print，持续失败提醒也是print；并非每300s重新发TG。监控部分save返回未检查仍print已保存是继承/重排中的可观测性边界，不应把日志当durable证据。相关预算/通知口径请独立复核，不擅自改候选。

## 实际核验与未执行项

本轮只读核验：

1. 两端字节SHA256、PR源码head及引擎blob确认；见对象段。
2. 原始源码完整83块对比（`autojunk=False`），逐块查看删增内容；#55/#56另做去行首空白、过滤整行注释的辅助diff，再回源码核原始缩进/try/continue/break/finally。辅助diff不被用作控制流等价证明。
3. 候选AST：`_persist_states` 调用 **27** 个，**0** 个遗漏 `read_corrupt`；传 `no_retry_patterns` 的调用 **1** 个（converge撤单）；monitor带finalbody的Try **1** 个（L10069，15个顶层收尾语句）。
4. 回归定位：阅读/搜索上述现役测试断言与关键接线，不以历史helper测试作为候选引擎证明。

本轮**未重跑门禁、未加测试、未执行交易所/生产API、未消融**。沿用但不重算的历史结果：head `3077a80` 干净worktree门禁rc=0，63/63脚本PASS，179 passed + 32 subtests；ChatGPT独立ENTRY测试10/10及内存消融5失败/rc1。critical独立断言只覆盖None，不声称四种输入均逐项核告警。

### 未跟踪历史产物的范围说明

`送审附件_v6.1/canonical_tmp/` 中的 `new_helpers_v3_canonical.py`、`new_helpers_v3_entry_canonical.py`、`new_helpers_v4_canonical.py` 是既有未跟踪历史产物。它们未用于本轮83-hunk核对，也不是U1/U4的对照证据，不纳入现役测试或本轮提交/交付范围。本轮不审计其生成链、不裁定其权威性，也不删除这些文件；本报告的代码判断以生产与冻结候选实际源码为准。

## 交付裁定请求

请独立复审：核83项实际差异及U0–U8，分别标明已属原裁决边界、需要补证、或确需重新开启修复的项。**在独立裁定完成前，本报告仅表示逐块阅读完成；不宣告发布硬门关闭。** 保持候选冻结，仅替换引擎的既定部署范围不变，bot_runner与生产TG不动；继续不合并、不部署。
