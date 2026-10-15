# 1.10 独立代码审阅

审阅日期：2026-10-15。审阅者为独立 review 代理，未参与生产实现，也未修改其他代理的实现或测试。

**结论：在本报告说明的范围及信任模型内，没有发现尚未解决的 P1/P2 问题。** 此结论允许继续执行发布验证，不等于已完成真实模型实验、真人使用验收、wheel 验收或最终 owned-root 清理。

## 固定审阅版本

- 基线：`c6a8075066544ad01260d6af2714a00426754baa`。
- 最终审阅提交：`e2eb78a6780442cd3c4b0cf583e90f96cf81df99`。
- 包含核心 `89a0353`、Hermes `b45b0dc`、根 CLI `bda2d80`、对照实现 `9695239` 及工作锁说明修正 `e2eb78a`。
- 核查依据：`docs/superpowers/specs/2026-10-15-rpe-v110-design.md` 与对应实施计划。审阅时 `.gitattributes` 尚有两行 1.10 夹具文本属性的待提交修改；该改动不改变运行逻辑。`.v110-owned-root` 为本轮临时根指针。

主要文件的 SHA256：

| 文件 | SHA256 |
|---|---|
| `src/rpe_harness/proposal.py` | `ba2c0b23e0e46906b94714e4a73e6d86f560b935fd277abac0a0b8894b0afb64` |
| `src/rpe_harness/proposal_cli.py` | `95c780d335fb829ddd5c2f851553a2491bb1cde21bfddd48dbcfea92ee7bb4c5` |
| `src/rpe_harness/integrations/hermes_review.py` | `a7e088f0cefa0de89d2990b474a91cc6a1924946ba546644394019dac83fc44a` |
| `src/rpe_harness/integrations/hermes_session.py` | `8e9771f35f72a9e4f095ee6aab42472836ef604dbb79e4ab250b5ff07a6860ca` |
| `src/rpe_harness/integrations/hermes_bootstrap.py` | `c034b04c5ac6bbee4397cd45f53c4e59d6ddd7eebecf84e1650df4bdb71b80b1` |
| `src/rpe_harness/experiments/review_comparison.py` | `dd181d25208c77b2da7a44e276d6b78be2dc9137357d0761458043969a35e8fd` |
| `src/rpe_harness/cli.py` | `fe2a91f6ee98bde84982511379f5e6f4b75ab91f84f0fb1002358d2737e0b021` |
| `tests/test_review_comparison.py` | `9c2771a904767b307d4037a43833edf32611129e12fdc9188bb69f41680bfe87` |

## 审阅判断

1. **原文、草案、合同与确认绑定。** `proposal.py:70` 起限制要求行与字段映射，引用须为原文精确子串，范围由父进程计算。`create_proposal` 保留原文及原始草案，完整展示默认值和标准化合同；缺项、未知参数、不支持选项或已报告的 unresolved 不会成为可确认草案。`show`、`_minimal`、`confirm`、`resolve` 重验摘要、会话、来源、目录、输入及机器合同。确认只绑定已显示的完整摘要，不授予另一个草案权限。
2. **可信确认与业务执行分开。** 模型只有 `rpe_propose`；交互确认须先显示完整审阅再输入“确认”，非交互自动化须提供完整摘要并记录真实应用标签。`actor` 只作来源记录，调用方认证由可信宿主负责。该 API 不承诺抵抗同 UID 的任意代码或数据库写权限。
3. **实际执行继续复用确定性管线。** `ProposalStore.execute` 在创建进程前原子消费确认；重复调用复用终态或返回已消费的进行中状态。不同审阅任务通过有界的每数据库工作锁串行进入执行器；取得锁后再次重验，随后绑定文件描述符和已审阅的目录对象。捕获的启动失败或超时形成持久终态，清理使用所属进程身份。工作锁是明确的资源及并发策略，不是对 RunStore 活跃租约失效的修补；现有恢复代码会检查 live flock lease。
4. **Hermes 历史和失败边界。** review 请求投影只包含固定公开合同与父进程保存的本次原文；transport 复用 clean 的限定参数，不接收隐藏消息渠道。实际 SDK/HTTP 入口再次核对最终请求，插件不可用、入口缺失或后续覆盖都会在 provider 前终止。持久核验会重新检查 API、工具、RPC、来源、草案和父进程记录；没有把 clean 的自动执行 handler 接入 review。
5. **对照及公开证据。** 两臂使用同一自然语言、夹具、公开起草格式、冻结环境和技能；原生保留工具发现。模拟审阅仅接受或拒绝实际草案，原生执行接收原样接受的候选，不注入 oracle 或补写答案。顺序无语义差异的唯一约束标志及组合键只在评分副本中规范化，确认及公开候选仍保留原提案。业务正确、起草忠实、批准与执行完整链分别记录。公开导出仅保留公共原文、经过验证的候选及引用、摘要、成本和正确业务产物，未知模型说明文字仅留摘要；离线校验可在私有数据库删除后重算来源、产物和内部确认/成本绑定。

## 审阅中发现并关闭的问题

曾发现一项 P2 计时口径问题：窄阶段计时没有覆盖重复 freeze、门禁验证、原生草案持久化和部分 resolve 工作，不能用阶段耗时之和代表完整策略耗时。

最终实现加入 `full_trial_elapsed_seconds`，覆盖每次完整试验及门禁、确认、执行和评分；另用不可变 driver receipt 覆盖入口、freeze、编排和汇总。摘要明确标注三个重叠口径，禁止相加，缺失完整试验值保持 null。新测试用可控时钟验证“起草 2 秒 + 门禁 3 秒”得到完整试验 5 秒、窄阶段 2 秒，避免将窄阶段数字当总成本。该项在最终版本关闭。

## 独立验证证据

最终固定版本的针对性命令：

```bash
PYTHONDONTWRITEBYTECODE=1 \
TMPDIR=/Users/audrey/项目/缓存/test/rpe-v110-37f23ae55b694cb3912f32f88684cfe2/review-independent \
/Users/audrey/项目/缓存/test/rpe-v110-37f23ae55b694cb3912f32f88684cfe2/venv/bin/python -B -m pytest \
  -p no:cacheprovider \
  --basetemp=/Users/audrey/项目/缓存/test/rpe-v110-37f23ae55b694cb3912f32f88684cfe2/review-independent/pinned-pytest \
  tests/test_proposal.py tests/test_proposal_cli.py tests/test_hermes_review.py \
  tests/test_review_entry_cli.py tests/test_review_comparison.py -q
```

结果：**81 passed in 15.48s，退出码 0，无 skip。** 包含实际已安装 Hermes SDK 的无网络加载、原生工具验证与调用、HTTP MockTransport 最终请求检查，以及业务 worker、导出及删除私有库后的离线复验。SDK mock provider 测试不等于真实模型会话。

额外独立脚本放在本轮 owned-root 的 `review-independent/probe_boundaries.py`，通过公共 API 和真实固定 CLI worker 验证：

- 两个不同确认共享一个数据库并发执行：两个 `succeeded`、两个不同 run_id、数据库恰有两条 run；数值字面量 `1.20e+2` 和字符串转义 `\u4e2d` 保留。
- 在执行已声明且仍等待工作锁时修改输入：取得锁后重验失败，无输出；重复请求返回相同持久失败结果。
- 将已确认输入替换为 FIFO：约 0.003 秒即拒绝，未声明或创建业务输出。

所有审阅探针、测试临时文件均位于本轮 owned-root 的 `review-independent` 子目录；未调用付费模型、未运行完整测试集、未启动日常 Hermes 服务，也未清理用户日常状态。最终全量回归、安装包验收、真实 pilot/formal 结果及整轮清理应由各自验收记录另行提供。

## 结论的限制

仍只有既有三种业务能力。引用证明出处，人工审阅接受模型的理解；本审阅不把引用覆盖或夹具评分写成任意自然语言语义完整性的证明。对照审阅为 `simulated_fixture_reviewer`，不是真人确认体验或真人耗时验证。公开运行身份是已记录的运行检查，不是第三方密码学证明。未知 token/cache、真人审阅时间和账单金额应继续保持 null；完整流程差异不能归因于单一模块。
