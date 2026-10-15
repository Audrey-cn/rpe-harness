# 1.13 工程机制、补证时间线与适用边界

本文记录 1.13 的工程改动及无模型验证范围，不给出正式模型对照收益、最终版本验收或完成结论。首批正式对照的生产冻结提交为 `298aa06`；首批原始记录和成本由主流程保留。正式运行后发现的入口缺口及其最小修补单独记账，不能将修补后的证据回填为首批成绩。

## 三项修复的实际机制

### 1. 模型起草引用采用单选，历史持久草稿保持严格兼容

`rpe_propose` 的模型工具 schema 将每个 requirements 行的 `quote` 与 `quote_ref` 设为 `oneOf`：必须且只能选择一个。模型说明明确要求使用 `quote_ref` 时省略 `quote`，并从本次已发布的真实 request anchor 生成不含 `quote` 的示例。`source_ref` 仍为可选字段；若携带，必须符合被引用附件的原始来源对象与事实映射。

这项改动约束模型生成合同，没有重写已经收到的 draft，也没有把子串引文自动改成整段地址引文。持久提案校验继续兼容历史 `quote` 与 `quote_ref` 同时存在且完全一致的记录；地址未知、过期、引文不一致、伪来源、错误 session 或错误字段事实仍被拒绝。来源证明也不等于业务语义完整或已获用户确认。

对应实现与回归见 [hermes_review.py](../../../src/rpe_harness/integrations/hermes_review.py)、[test_review_quote_contract.py](../../../tests/test_review_quote_contract.py)。

### 2. 真实提案落盘后，复用已安装 Hermes 的原生结束回合路径

隔离 review 进程对实际安装的 `agent.turn_tool_round.compress_after_tool_results` 绑定安装进程内适配器。只有真实 terminal tool round 完成后，适配器才有机会返回 Hermes 已有的 `PostToolCompressionVerdict(end_turn=True)`。Hermes 的工具结果持久化失败检查位于该接口之前；原生 turn loop 因此可以正常 break，并继续走原生 finalizer，关闭 assistant transcript 尾部及 session，而不用再请求供应商总结一次。

本地 finisher 先重开持久工作流与事件，核对当前 agent 的 session、turn、task、API 和 tool-call 身份、真实 tool 尾行、完整单工具策略链、raw/effective 参数回执、父进程 RPC、`rpe_review_proposed`、真实 `post_tool_call` 及 `ProposalStore.show` 的提案内容和摘要。通过后只展示真实提案的 `status`、ID、摘要、missing 和 errors；可结束 `awaiting_confirmation`、`needs_context`、`unsupported` 等真实持久状态。缺回执、身份冲突或没有持久提案的内部错误会本地阻断，不生成确认或执行记录。

无技能与技能流程的 API 上限仍为 2 / 5；既往响应违反单工具协议，即使后来形成持久提案，完整策略链仍不能通过。本轮没有给旧记录重评分。适配器没有伪造 API 请求、响应、usage、用户确认或执行，也没有新增依赖、业务工具或守护进程。

真实安装组件的 provider-free smoke 使用真实插件、AIAgent loop、tool executor、冷技能 restore、原生 skill_view 和 finalizer，只将供应商 client 调用替换为受控响应，并禁止网络。直接 proposal 路径观察到 1 次 stub provider 请求；冷技能路径观察到 discover → select → view → propose 共 4 次请求，均为 0 次报告请求，并有正常结束后的 tool → assistant transcript 尾部。测试未提供 token usage，不能据此声称真实模型的调用节省、账单收益、理解质量或正式样本成功。

对应实现与回归见 [hermes_review_finish.py](../../../src/rpe_harness/integrations/hermes_review_finish.py)、[test_hermes_review_finish.py](../../../tests/test_hermes_review_finish.py)。

### 3. 原生 XLSX 执行要求可认证的真实 helper 退出证据

本轮为 1.13 原生 XLSX 执行增加显式启用的同机只读观测。它围绕真实 `terminal` 调用，使用 Darwin 的 libproc / sysctl / kqueue 读取新后代进程、PID 与启动身份、解释器路径、实际 argv、cwd、stdout fd1、启动环境及内核退出事件，并绑定已冻结的解释器、原始 skill/helper、业务输入和最终产物快照。

它不会根据 shell 字符串猜测 helper 已执行，也不会把外层 shell 的 0、stdout 中的 `exit=0` 或子进程自写的“回执”当作 helper 成功。原 helper 退出 7 后再执行成功命令而得到 shell 0，仍不能通过。错误解释器、argv、参数、cwd、fd1、启动注入环境、链接、冻结文件变化、重叠调用、缺失退出或线程未收尾，都不能补成成功证据。只发现一个 PID 或只看到正确产物也不足以通过。

控制端为每个隔离 session 生成随机认证密钥，通过一次性 pipe FD 在模型可见子进程产生前传给宿主；宿主消耗并关闭 FD，对规范化观测字段计算 HMAC-SHA256。父端随后核验签名、持久事件、调用绑定、原始技能先加载、配置与文件哈希、实际退出 0，以及独立业务 oracle。认证提高的是本机回执来源与完整性约束，不构成远端证明或独立第三方运行证明。

产物绑定的准确范围是：观测到的 helper stdout inode 上的最终字节与退出时、复核时的文件状态一致。它不证明 helper 是独占写作者；即使 HMAC 有效，后续错误业务字节仍必须由独立业务 oracle 拒绝。代码保留 `final_bytes_on_observed_stdout_inode_not_exclusive_writer` 这一范围标记。

当前可选观测仅支持已测试 Darwin ABI。进程若在登记前已经退出，或系统无法提供所需内核信息，就没有退出证明，必须保持未验证；不会退回用外层 shell 状态、补写 PID 或猜测退出码来放行。这项限制也意味着短进程可能被安全拒绝，而非保证所有正确调用都被接纳。

对应实现与验收见 [hermes_native_process.py](../../../src/rpe_harness/integrations/hermes_native_process.py)、[evaluate_native_review_execution_v113](../../../src/rpe_harness/experiments/review_native_evidence.py)、[test_native_process_evidence.py](../../../tests/test_native_process_evidence.py)。

## 原始安装包暴露的 closing race 与最小修补

最初独立安装包选集真实结果为 **149 passed / 2 failed**，保留于 `package-initial.log`、`package-initial.xml`，不能改写成通过。其中一个真实 helper 退出 7 的测试缺少 `process_returncode`；另一个业务覆写拒绝测试提前停在 `native_unique_successful_helper_process_exit_missing`，没有走到必须检验的业务错误断言。它们揭示了收尾缺证，不能归因成测试误报或靠降低断言消除。

具体竞态是：原生 terminal 已经返回，观测线程收到 stop 时，先前已登记的后代 helper 退出通知仍可能在内核队列中。原收尾路径过早关闭队列，丢掉了真实通知。这会拒绝本可核实的执行证据；当时行为仍为 fail closed，没有把失败 helper 误判为成功。

最小修补仅在 stop 后对**已登记进程**的内核退出通知做最多 1 秒、且不超过既有总体 deadline 的有界收集。stop 后不再发现新进程，不推断 shell 状态，也不制造通知或退出码；超时依然缺证拒绝，线程仍有界 join。原来的解释器、环境、冻结文件、输出、HMAC 和业务检查保持原判据。

新增回归用调度适配器延迟交付真实 kqueue 进程通知，直到进入正的 closing wait；它不伪造内核事件或退出值。旧代码得到 1 failed；修补后相关选集通过，并保留“helper 7 / shell 0 必拒绝”和业务覆写的精确错误断言。证据为 `native-close-race-red.xml`、`native-close-race-green.xml`、`native-close-stress.json` 及独立审查 `closing-race-review.json`。

## 工程验证必须按发生顺序解释

| 证据 | 实际结果 | 能支持的范围与时间点 |
| --- | --- | --- |
| `review-flow-final.log/xml` | 107 passed | 引用、原生 end_turn、既有 review / skill / bootstrap 选集；包含真实安装的单请求和冷技能四请求 smoke。 |
| `full-suite.log` | 1842 passed / 7 skipped | 完整回归发生于最后 closing-race 修补之前，不能称为最终修补后完整回归。 |
| `package-initial.log/xml` | 149 passed / 2 failed | 原始独立安装包真实暴露收尾竞态，失败记录保留。 |
| `native-close-race-red.xml` | 1 failed | 真实内核通知延迟交付的定向复现。 |
| `native-close-race-green.xml` | 54 passed | closing-race 修补后的原生、安装组件及相关验收补证。 |
| `native-close-preconditions.xml` | 9 passed | 补充核实原生证据前置状态，防止以本就缺证的 fixture 掩盖拒绝原因。 |
| `native-close-stress.json` | 20 次真实进程观测，全部匹配预期 | 退出 0 与 helper 7 / shell 0 交替；全部 observer joined；0 供应商调用。 |
| `closing-race-review.json` | 独立 11 项复核通过 | 只读核对有界收尾、真实通知和既有失败判据；不是正式业务收益证明。 |
| `package-final.log/xml`、`final-wheel-proof.json` | 152 passed；88 个模块源码 / wheel / 独立安装字节一致 | closing-race 修补后的包验证；发生于下述 bootstrap 入口缺口发现及修补之前。 |

这些数字分别对应实际执行的范围，不把不同时间点的选集相加冒充最终全量回归。原始工程文件位于本轮专属 OWNED 根；主流程负责保留脱敏交付副本与清理时的证据清单。

## 首批正式冻结后发现的 bootstrap 入口缺口及后补边界

首批运行使用冻结提交 `298aa06`。其 native draft 在独立文件形式启动 bootstrap 时，`PYTHONPATH` 按设计只提供已安装 Hermes 根，入口无法导入 `rpe_harness`。bootstrap 却无条件 import review-finish 模块，因而在进入模块内部 mode guard 之前抛出 `ModuleNotFoundError`。已有 provider-free review smoke 直接安装了适配器，没有覆盖这一原生文件入口；独立包选集当时的 `BOOTSTRAP_PATH` 又固定取测试仓库源码，不能作为 wheel 自身入口的证明。

后补生产改动只涉及 `hermes_bootstrap.py`：先在 `main` 外层检查 `RPE_HERMES_ENTRY_MODE == 'review'`，再 import / install review finish 适配器。没有为原生路径扩大 SDK 可见性或沙箱权限，也没有改 prompt、scorer、任务夹具或三个业务执行合同。测试中的 `BOOTSTRAP_PATH` 改为从实际导入的 `rpe_harness.__file__` 邻近位置解析，源环境指向 src，独立 wheel 环境指向 site-packages。

后补 RED 为 4 failed：3 项非 review 分支不得导入适配器的断言，加上真实已安装 Hermes、真实 native-clean 沙箱与实际 bootstrap 文件入口的复现。最小 guard 后该选集为 **4 passed**。真实入口 smoke 在入口明确断言 `rpe_harness` 不可导入，禁止网络，通过实际 bootstrap 分派到已安装 AIAgent 的真实 terminal/file 构造器；只替换 CLI 最终 callback，不运行 conversation、不制造 API 或 usage，网络尝试与供应商请求均为 0。它证明这个构造入口能够启动，不能代替真实模型起草验收。

生产模块哈希变化为：

- 修补前 `hermes_bootstrap.py`：`273f1fc9e6ae0b2a4a66c202e8f15cf3b490a461636eadc4a41b0488cf1f9103`。
- 修补后 `hermes_bootstrap.py`：`320b13e948e3cb89498a1556e375206e40d90a9ea7c50f0ecdefd4735f90e5cd`。

后补受影响回归 `tests/test_hermes_bootstrap.py` 与 `tests/test_hermes_review_finish.py` 合计 **34 passed**（74.08 秒）；覆盖新入口和既有实际安装的单请求 / 冷技能四请求结束路径。它仍是定向回归，不是再次完整回归。

证据为 `bootstrap-native-clean-red.log/xml`、`bootstrap-native-clean-green.log/xml`、`bootstrap-native-clean-final.log/xml` 与 `patch/bootstrap-hash-diff.json`。首批记录、失败和成本须完整保留；重新冻结、重建安装包、受影响回归以及完整四对补充批次，应由主流程分别列明版本、预算与结果，不能覆盖首批或把两批混成一次无重试样本。本文不预写补充批次结果。

## 第二批模型冻结后的 keyFD 文件入口补修

第二批正式模型证据对应提交 `ad0561a`。该批终态与导出完成后，另一个同类入口缺口得到确认：原生带签名 keyFD 的文件 bootstrap 在导入 `hermes_native_process` 时，同样没有 SDK import root。旧的完整 keyed constructor / kernel smoke 在入口先将 SDK 加入 `sys.path`，掩盖了这一缺口；第一轮无 key 的新 constructor 回归也不能证明带 key 路径已经可用。

最终生产补修仍只修改 `hermes_bootstrap.py`。当且仅当控制端提供签名 keyFD 时，bootstrap 保存当前 `sys.path`，在很小的 try 作用域内临时加入控制端给定的 `RPE_HARNESS_IMPORT_ROOT`，按规范模块名导入 observer 并消费 keyFD，然后在 finally 中原位恢复 `sys.path`。该步骤发生于 Hermes bootstrap / CLI 导入和工具子进程启动之前。原有 observer 模块及其密钥状态保留在同一 canonical `sys.modules` 实例，后续注册复用该实例；没有改环境、`PYTHONPATH`、沙箱权限、业务工具、prompt 或 scorer。

补充回归先在未修补代码上得到 **2 failed**，两个真实无 SDK 预载的文件入口都复现 `ModuleNotFoundError`。修补后有效定向结果为 **2 passed / 32.89 秒**（`bootstrap-keyed-green-final.log/xml`）。其范围包括：

- 真实安装解释器、真实 native-clean 沙箱和按文件加载的 bootstrap；入口先验要求 `rpe_harness` 不可导入，使用一次性 dummy pipe 密钥，随后经过实际 AIAgent 原生工具构造器。
- 断言 FD 已消费并关闭、FD 环境标记已移除、canonical observer 的签名状态可复用、父进程 import path 已恢复且 `PYTHONPATH` 不变；真实新子进程仍不能导入 SDK，也看不到 keyFD 环境标记。
- 修正旧完整 smoke 的 SDK 预加载，保留真实 terminal / 原始 helper / 内核退出观测。五个有签名回执覆盖成功、实际非零失败、shell 0 掩盖失败、启动注入拒绝及最终真实 helper 成功；最终原始 JSON 与独立业务 oracle 均通过，0 模型调用。

首次 GREEN 尝试的两个失败也保留在 `bootstrap-keyed-green.log/xml`：测试拦截器漏传 `import_module` 的 package 参数，以及在 bootstrap 合法改写 `sys.argv` 后读取旧参数位置。两者只修测试夹具，没有再次修改生产补丁，没有降低上述入口、FD、子进程、真实退出或业务断言。

最终 bootstrap SHA256 为 `be0e98c30eeb0a5041d46187ce61034d56660876a75a117c7b98c7cc2a6e7e5c`，相对已执行模型冻结 `ad0561a` 的生产差异仅该入口模块。最终交付冻结与 wheel 验收由主流程另记。明确保留 `model_validation_of_final_entry_patch=false`：最后这项入口修补只有工程 / provider-free 补证，没有再做第三批模型运行，不能把第二批模型证据说成对最终入口补丁的正式模型验证。

## 业务权限与未覆盖范围

执行能力仍限于既有 `tabular.deduplicate`、`json.normalize`、`xlsx.inspect` 三个合同：表格去重保持既有首条、顺序、单元格与受支持格式规则；JSON 规范化保持既有仅改结构空白的无损规则；XLSX 读取仍调用原始已信任 helper，并保留其 JSON 结构和类型。review 只产生可审阅草稿，没有自动确认，也没有直接执行入口。正式实验中的程序化语义审阅必须标注为模拟审阅，不能称作真人确认。

本轮工程证据能够说明特定协议、进程观测和安装入口的行为，不能据此宣称跨平台适用、所有短进程均可捕获、独占产物写作者、远端可信证明、真实供应商硬调用上限或普遍业务收益。正式模型对照、实际成本与未知项、独立 CLI 审阅、清理和最终交付结论，应由主流程依据各自完成后的记录另行给出。
