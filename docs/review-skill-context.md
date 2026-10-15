# 1.13：公开审阅、真实技能与必要来源

用户直接给自然语言，Hermes 只起草，可信审阅者确认后确定性执行。业务任务不需要手写 JSON。以下路径需替换为自己的专属目录。

```sh
./scripts/rpe review draft --text '把 input.csv 按客户编号保留第一行去重，原顺序另存 output.csv，保留原文件。' --workspace /absolute/work --db /absolute/state.sqlite
./scripts/rpe review show PROPOSAL_ID --db /absolute/state.sqlite
./scripts/rpe review confirm PROPOSAL_ID --db /absolute/state.sqlite
./scripts/rpe review run CONFIRMATION_ID --db /absolute/state.sqlite
```

`confirm` 展示完整请求、引用、默认值和执行合同后要求终端输入“确认”。自动宿主可显式接受展示的摘要并提供真实 actor 标签；这不是模型权限，也不能把模拟审阅称作真人验证。`--json` 返回完整机器结果；draft 的 `host_observation` 包含实际用量、调用身份和进程清理结果，未观测值不会补零。

## 真实技能

先将本机 Hermes 技能建立只读快照；执行适配器需要显式信任匹配的已登记字节。

```sh
./scripts/rpe skills sync --skill-root /Users/audrey/.hermes/skills --db /absolute/state.sqlite --owned-root /absolute/catalog
./scripts/rpe skills trust productivity/xlsx --db /absolute/state.sqlite --owned-root /absolute/catalog
./scripts/rpe review draft --text '读取 input.xlsx 的各工作表名称，保持原helper的JSON结构另存 output.json，不改原文件。' --workspace /absolute/work --db /absolute/state.sqlite --catalog-root /absolute/catalog
```

配置 catalog 后，模型根据当前任务查询真实 metadata，再显式选择返回候选或 none。父层不会默认取第一个 XLSX adapter；native skill_view 加载固定内容。冷技能在本轮独立 HOME 的归档副本中由 Hermes 的 restore_skill 恢复，日常技能目录不改动。目录发现包含其他真实技能，但未登记适配器只供发现；加载文字也不授予其任意脚本执行权限。

## 必要上下文

普通用户仍直接说任务。可信宿主若已经保存用户明确给出的业务事实，可通过 `--context-sources /absolute/authorized-sources.json` 选择本次授权的来源附件；这个文件是宿主接口，不是要求用户编写任务合同。沿用已有 sources 数组，每项有 source、event_id、payload.text 和 facts。不能根据文件名或行值猜业务键；来源互冲突、缺业务决定、引用不匹配、跨会话或篡改都阻断。

原始本轮请求逐字保留；模型只得到必要字段与可核对的来源片段，不转发聊天历史。恢复只针对显式导入的来源，不扫描日常记忆。来源事实仍需要可信宿主和审阅者负责语义真实性，内容摘要只证明未被替换。提案包含逐字段出处与恢复记录，确认和执行重新核验。

## 状态与清理

draft 默认销毁本次独立 Hermes HOME、凭据副本和历史，只保留指定数据库中的任务记录。`--owner-root` 显式保留独立宿主状态，供调用方验收后自行清理；`--read-isolation-root` 和可重复的 `--deny-read-root` 限制实验读取范围。不要把日常 HOME 作为测试根目录。RPE 不新增常驻服务、分类器或通用记忆系统，运行依赖仍仅 openpyxl。

## 1.12 稳定性修复

用户仍只需提供自然语言并审阅要求清单。起草工具复用公开合同的单字符分隔符限制：逗号对应真正的 `,`，TAB 对应真正的制表字符；`comma`、`TAB` 或字面反斜杠加 t 不会被静默转换。

模型可以逐字引用，也可以选择父层给出的 `quote_ref` 原文定位标识。标识对应本次完整请求、原文中的非空行或明确附件的既有字段片段；用户看到的审阅仍展示原文、具体引用、字符范围和出处。它只解决定位，不能证明语义完整。错误的定位、与文字不一致的标识、跨来源引用仍被拒绝，原始模型草案原样保存。

原生对照使用额外只读观测，记录真实工具调用前后的 `draft.json` 字节；终端提及文件名或最终回复给出 JSON 不能冒充已发布文件。冷恢复命令明确带上安装的 Hermes 模块路径，避免被终端环境清理影响。该观测不注册新的业务工具，也不改变原生工具权限。

## 1.13：减少引用冲突与收尾开销

起草工具要求每条选择`quote`或`quote_ref`其中一种；使用定位标识时省略引用文字，父层仍展示准确原文和出处。既有历史草稿中两个字段完全一致的记录仍兼容；不一致继续阻断，不能静默删掉或替换模型原稿。

真实提案与工具回执验证、持久化后，隔离Hermes复用原生结束回合机制展示提案状态，不再为了复述状态额外请求模型。这既不会确认提案，也不会执行任务。先前违规多工具响应、缺回执或提案变动仍拒绝。

本轮macOS原生对照可选择只读观测原helper进程的实际退出状态，并将回执绑定至实际调用、固定技能/解释器/输入和最终产物。后续shell命令成功或打印成功文字不能替代helper成功。观测可能错过过短进程，缺证时保持未验证；最终产物仍独立检查，不声称输出只可能由一个进程写入。这项可选观测不扩张业务执行权限。
