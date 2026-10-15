# 自然语言任务审阅

1.10 的任务入口接受完整自然语言原文。Hermes 只负责提出要求草稿，信任的本地控制器保存并校验草稿；确认和执行是两个单独动作。用户不需要手写 JSON。

```bash
./scripts/rpe review draft --workspace /absolute/workspace --db /absolute/state.sqlite \
  --text '把 input.csv 按编号去重保留首条，另存 output.csv，保留原文件与顺序。'

./scripts/rpe review show PROPOSAL_ID --db /absolute/state.sqlite
./scripts/rpe review confirm PROPOSAL_ID --db /absolute/state.sqlite
./scripts/rpe review run CONFIRMATION_ID --db /absolute/state.sqlite
```

`draft` 也支持 `--source request.txt` 或 `--source -`，保留读入原文。默认工作目录是当前目录，默认数据库沿用 Harness 状态库。`show`、`confirm` 和 `run` 默认从不可变记录读取会话；`run` 同时读取原工作目录。不同数据库的 ID 不会互通。

每份审阅显示完整原文、逐项要求、原文引用和字符范围、字段映射、提出的默认值、标准化执行合同及未解决问题。ANSI、回车、双向文本控制符等会显示成转义文本。控制器计算引用范围；模型不能提供确认位、确认 ID 或执行授权。

引用只证明某段文字出现在原文，**不能证明自然语言语义完整**。审阅者需要对照完整原文核对遗漏、误解、路径、选项及默认值。缺字段、未映射字段、未知参数、无法执行的能力或任何已报告的未解决条款都会阻止确认。模型未报告的语义遗漏仍依赖审阅者发现；本版本不声称自动证明任意自然语言完整性。

模型收到精确 ID、字段指针和选项枚举，但 Hermes 本身不执行枚举语义校验，父层仍负责拒绝坏草稿；既定执行默认值同样属于人工确认范围，文件、路径与 helper 信任等运行前提由父层检查，不属于缺失的业务决定。

若模型在首次尚未提交的起草或技能发现阶段漏掉强制工具调用，或一次返回多个调用，隔离宿主会在任何工具分发前保存完整标准化坏响应及请求身份，最多允许一次同阶段协议恢复。原请求、失败记录与全部已观测用量保留；截断、拒绝响应、参数或语义错误、证据缺失，以及任何阶段提交后的协议偏差不适用此恢复。恢复成功仍只产生待审阅提案，不会确认或执行。

交互式 `confirm` 先显示完整审阅，再要求在终端输入 `确认`。确认只保存接受范围，不启动任务。管道输入不算交互确认。信任的外部应用可在完成自己的审阅流程后使用 `--accept-digest EXACT_PROPOSAL_SHA256 --actor REAL_REVIEWER`；默认标签是 `trusted-application:用户名`，交互式标签是 `human-cli:用户名`。测试中的 `simulated_fixture_reviewer` 是模拟审阅，不能写成人类验收。`actor` 是审阅来源标签，本身不是身份认证；嵌入应用负责认证调用者，确认接口不向模型开放。

草稿 ID 绑定原文、原始提案、父进程计算的引用、完整显示文本、标准化合同、输入摘要、能力目录、会话和工作目录对象。确认绑定此完整摘要。修改内容需要创建新草稿；旧确认不会转移到新草稿。确认和执行均重验输入、目录及路径；同一路径下替换输入或输出目录、创建既有输出、使用符号链接或撤销能力信任都会拒绝执行。

`run` 以 SQLite 原子声明消费一次确认，调用既有固定 CLI 子进程和文件描述符绑定，再复用正常执行及独立验证流程。相同确认的重复请求返回同一持久结果；进行中的并发请求返回 `running`。不同审阅任务在同一数据库下通过有限等待的工作锁串行进入固定子进程，等待时间计入执行超时。超时和已捕获的启动失败会保存终态，不会自动重新发布。真实父进程崩溃可能留下已消费而没有结果的声明；该声明保持关闭，需要人工诊断，不能把它当成新的执行授权。

这些限制是信任的本地控制器与模型之间的应用边界。具有同一用户任意代码或数据库写权限的进程不属于隔离威胁模型。原来的严格 JSON `source` 入口保持原有语法与验证语义。

C0 公共状态修正：`review draft` 未能核验提案（协议、宿主或观察失败）现在返回 `status=unverified` 和 exit 1，附固定结构 `failure={domain,code,stage,retryable:false}`。这取代旧版把技术缺证据归为 `needs_context`/exit 3 的行为。真实、持久且已核验的业务缺项提案仍为 `needs_context`/exit 3；启动前无效输入仍为 `invalid_request`/exit 2。观察收尾异常保留已知用量与进程清理信息。

`review_diagnostics` 是无授权的结构摘要：`schema_version=1`、`diagnostic_only=true`、`coverage={complete,reasons}`、最多 32 条 `responses` 和最多 32 条 `failures`。响应记录原生身份、阶段、原生 action、finish reason、refusal 字段是否存在、工具数量、应用期待工具和 RPE 判定；缺失值用 null，未知记录不当作正常或零值。它不保存正文、reasoning、工具 arguments、provider_data 内容或任意异常文本。私有 journal 上限 512 KiB，只读取普通文件；公开 envelope 上限 64 KiB。超限、坏记录、缺证据会明确标记 coverage 不完整。

父进程在临时宿主 HOME 删除前将摘要原子保存到状态库旁的 `review-diagnostics` 目录；`diagnostics_ref={path,sha256}` 给出父生成的绝对路径和文件摘要。它仅供诊断，不能替代原始恢复响应、收据、引用校验、完整提案校验或确认授权。

固定 MiMo review profile 使用 wire `tool_choice=auto`，省略未获文档保证的 `parallel_tool_calls`。应用另存 `expected_tool`，只公开当前阶段的一个工具，仍要求实际响应恰好一个正确工具，并重验阶段、消息、schema、完整 payload 摘要和全部原生/父进程收据。终态没有可用工具；原生提案收尾不再请求模型。auto 不是服务端强制单调用的证明。

业务字段未知时，模型必须省略整个键及对应指针，并在 unresolved 提问；不允许占位列名。显式“保留首条”应提交真实 `execution.keep=first` 并引用原文。未请求的默认可以省略（不映射不存在的字段），或由模型主动提交真实字段并用 quote=null 标注。省略值仅在待确认机器合同中展开，不回填原始草稿。encoding/delimiter 是 CSV 选项；字段与来源冲突仍阻止确认。

审查补充：若父进程无法安全持久化诊断，仍返回 `unverified`/exit 1，保留已观测用量与清理信息，标记 `diagnostic_persistence_failed` 和 coverage 不完整，`diagnostics_ref=null`；不返回虚构文件引用或任意异常正文。

隔离 review 在 Hermes 的原生 response-check 阶段拦截 length/content_filter/incomplete，防止 intake 前的原生续写或 fallback。此时诊断明确 `stage=response_check`、`intake=not_reached`、`native_action=null`，保留实际 finish reason。已收到的用量通过一次 adapter 自己拥有的 `api_request_error` outcome 记录：`error_type=RPEReviewResponseCheckBlocked`、`error_code=review_native_response_before_intake_blocked`。因此 `usage.api_errors` 包括这种应用层拒绝，不能等同于服务端或网络故障；不会伪造正常 post/intake verdict，也不会重复计费事件。原始参数 capture/admission 的拒绝另有固定诊断码；正文和参数不进入该诊断渠道，真实业务 needs_context 不变。

协议门禁还在原生工具参数校验前只读检查每个调用的原始参数：必须是有界、严格的 JSON object，拒绝重复键、NaN/Infinity、数值溢出、坏语法和超限；不替换原始字符串或任何业务值。仅在多调用恢复资格判定时，另外用第一次实际发布并哈希绑定的 schema 检查各调用的结构，所有工具名/原生 ID 必须有效；不支持的验证关键字阻止恢复。最终恢复证明也重验原始坏响应与原始 schema。此检查不证明任意自然语言语义完整、引用真实或业务决定正确，单个正常 JSON object 仍由原有不可变提案与来源门禁处理真实 needs_context。

同一阶段的下一次 API 请求必须对应已经记录的那一次首阶段恢复，并匹配原请求、outcome 与恢复收据；其他隐式同阶段重试在 provider 之前拒绝。合法 discover/select/view/propose 阶段推进和原有一次恢复额度不变。
