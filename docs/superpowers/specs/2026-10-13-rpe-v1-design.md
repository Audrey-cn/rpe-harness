# RPE-Harness 1.0 设计与验收

用户授权：重新列出开发方向与进度，自主调用、开发、测试本机 CLI Agent，交付基础可用 1.0，并清理本次临时历史与缓存。采用可靠本地执行闭环；先验证工程能力，再研究持续学习收益。

## 交付范围

1. MCSI 兼容旧分类；来源、会话、约束和环境事实完整往返。未知任务、格式或能力不匹配、禁用/休眠、缺事实、低匹配分均升级，不能执行错误技能。
2. 内置 CSV/XLSX 按显式 key_columns 去重，只支持 keep=first。禁止覆盖输出，保留原输入和记录顺序。CSV 紧凑输出；XLSX 保留行坐标、清空重复记录，因此可能留下空行。公式保留为公式，只验证结构，不计算公式值。复杂对象、空关键列、未知约束明确拒绝。
3. 独立验证器重新读取输入与临时产物，检查首条语义、顺序、非关键列、公式、源 SHA256。验证通过再无覆盖发布。仅注册的 Python handler 可以执行，SKILL.md 不获得执行权。
4. SQLite 保存事件原文、请求 hash、运行阶段、结果、验证证据、产物 hash 和使用统计。事件 ID 幂等，同 ID 不同内容冲突。重启可查询；未终结运行只能标为 interrupted，不能推断成功。并发运行通过持久化租约防止重复执行。
5. CLI：run、events submit、runs list/show、serve、benchmark。stdout 为 JSON，失败有运行 ID。Hermes 仅旁路 JSON 协议，不宣称真实宿主插件接入。
6. HTTP 使用有界 JSON、连接超时、并发请求和明确错误；默认监听回环地址。数据目录与数据库显式配置，安装包包含可用内置能力，导入模块不创建数据库。
7. 固定人工任务真值、简单元数据基线和真实文件闭环分别评估；不把同源合成场景或微基准当作真实收益。保留原研究实验并标注其局限。

## 模块契约

- core：CausalState.validate/to_dict/from_event、TaskType.parse；facts 追加字段。SkillEntry.enabled/capabilities 追加字段；Router(graph=...) 统一过滤。RoutingResult 追加 confidence/reason/missing_preconditions。
- execution：prepare_deduplication(input_path, output_path, key_columns, *, sheet=None, keep='first', encoding='utf-8-sig', delimiter=',', constraints=None) -> dict。结果含 staging_path、input_path、output_path、input_sha256、format、sheet、key_columns、input_rows/output_rows/removed_rows、encoding/delimiter。
- verification：verify_deduplication(prepared: dict) -> dict，含 passed、checks、input_sha256、output_sha256；重新读源与 staging，不信任准备阶段统计。
- execution：publish_output(prepared) 无覆盖原子发布；cleanup_staging(prepared) 只删除该暂存文件。
- storage：RunStore.claim_event(source, event_id, payload) -> (record, fresh)；mark_running/run_step/finish/get/list/usage/skill state。SQL 成功状态约束验证通过。
- runtime：HarnessRuntime(data_dir=None, db_path=None)；run(payload, source='local') -> record、route、reload、stats。配置与内置技能在此统一加载。

## 请求

schema_version=1；event_id 非空；state 为标准 MCSI；execution 提供绝对 input_path/output_path、非空唯一 key_columns、keep=first、可选 sheet/encoding/delimiter。输入格式与扩展名必须一致；required_capability=tabular.deduplicate。用户 facts 不作为文件事实证明。

## 验收

状态与路由回归通过；CSV/XLSX 正确产物与污染产物拒绝；异常、重放、冲突、并发、重启正确；非法和慢 HTTP 请求不阻塞健康检查；wheel 新环境执行及查询成功；固定真值路由与文件任务报告；独立审查无未处理严重缺陷；临时 Agent 会话、数据库、缓存、环境和进程按归属清理。

## 后续路线

1.1：真实 Hermes 宿主事件接入与原生基线、更多执行器、人工生命周期反馈。1.2：独立冷门场景、多轮跨会话收益与实际 token/成本。2.0：技能 Consolidator 和因果图学习，需先建立证据、回滚与审批机制。
