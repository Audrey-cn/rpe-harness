# 1.10 首轮 pilot 后修复的独立审阅

审阅日期：2026-10-15。审阅提交：`aa5d9a51aed97ba77753ac28df083bc6347e3b31`，包含 Hermes 修复 `d0e7ec8` 与 review 对照修复 `aa5d9a5`。比较基线为保存首轮负结果的 `dfe1280`；此前整体代码审阅见 `independent-review.md`。

**结论：本轮修复范围内，没有发现尚未解决的 P1/P2 问题。** 这是源码、无网络 SDK 与针对性探针结论，不是新一轮真实模型成功或正式对照完成的结论。

## 原始结果保持不变

直接读取已提交的 `pilot-original/summary.json`，并比较 `dfe1280..aa5d9a5` 下整个 `reports/v1.10.0/pilot-original`，确认该目录无变更：2 次 pilot 起草、9 次观测 API 请求、72,390 tokens、0 个批准、0 个接受、0 次原生业务执行，正式试验次数仍为 0。原始失败没有被修复代码追溯改分。

原生 pilot 的 `draft.json` 为 1,839 字节。本次只读检查确认它仍与原始 `write_file` 参数的内容字节一致。先前调试中被修改的是 `check_draft.py` 临时脚本，不能描述成修改、修复或替换了原始草案。

## 修复边界

### Hermes 参数出处

实际宿主会在 pre-tool 观察之后对参数做 schema coercion。对于本次 malformed 的数组外观字符串，官方实现把原字符串包成单元素数组；它没有恢复出正确的要求行或空 unresolved。

修复增加独立的 raw capture 和 effective checked receipt，分别绑定真实会话、API、tool-call 身份、原始摘要、有效摘要及相互 receipt。pre-tool 摘要必须等于 raw；post-tool 仅允许两个已知 SDK 路径的 raw 或 effective 摘要，且事后门禁仍核验完整出处链和 RPC 参数。原来的实际 API、pre-tool、RPC 和 parent receipt 身份要求没有被删除。

允许的非恒等转换仅限 `requirements`、`unresolved` 的字符串变成包含同一个原字符串的单元素数组。它不解析字符串，不修补语义，不改变执行字段。未知转换在 RPC 前被拒绝。非恒等转换后的结果另有父进程及事后门禁双重限制，不得成为 `awaiting_confirmation`。实际 malformed 例子保持 `needs_context`、0 个合法要求行、不可确认且没有业务输出。

### 无 tool-call ID 的原生写入流

新的 `evaluate_native_review_draft` 只用于 review 起草。先调用原门禁；仅在原门禁明确返回 `native_output_invocation_missing` 时使用补充证明。1.8/1.9 及原生业务执行门禁没有修改。

补充证明要求：原有 `_native_calls` 的持久 API/tool 身份和 pre/post/stream 参数链全部成立；日志位于该试验 owned session；完整 stdout tool-use 签名与已观察流一致；恰好一次写到 `draft.json`；参数内容与最后草案字节完全相同；只有一条成功的持久 post 与该参数摘要相符；writer use/result 顺序配对没有并发、缺项或重复；writer 结果明确 verified、路径和字节数完全相符。出现部分 ID、重复签名、重复结果、交错 writer、错误路径、变化字节或失败 post 都拒绝。

证明名称为 `unique_native_write_signature`，记录参数/内容/路径摘要和日志位置；没有生成或补写 stdout 的 tool-call ID。没有放宽持久观察中的真实 ID 要求。

### 未核验门禁的结果

review 和 native 分支都会先保存有界的门禁状态与错误码，核验失败时停止后续处理，不再执行 `show(None)`。尚未实际审阅的 `draft_faithful` 保持 null。错误文字只允许短标识符，其余诊断被脱敏，避免公开未知模型文本。

## 独立验证

在本轮 owned-root 的 `review-independent` 下编写独立探针，没有修改实现者的生产代码或测试。运行：

```bash
PYTHONPATH=/Users/audrey/.codex/worktrees/rpe-v110/rpe-harness/src:/Users/audrey/.codex/worktrees/rpe-v110/rpe-harness/tests \
PYTHONDONTWRITEBYTECODE=1 \
TMPDIR=/Users/audrey/项目/缓存/test/rpe-v110-37f23ae55b694cb3912f32f88684cfe2/review-independent \
/Users/audrey/项目/缓存/test/rpe-v110-37f23ae55b694cb3912f32f88684cfe2/venv/bin/python -B -m pytest \
  -p no:cacheprovider \
  --basetemp=/Users/audrey/项目/缓存/test/rpe-v110-37f23ae55b694cb3912f32f88684cfe2/review-independent/postpilot-pytest \
  tests/test_hermes_review.py tests/test_review_comparison.py \
  /Users/audrey/项目/缓存/test/rpe-v110-37f23ae55b694cb3912f32f88684cfe2/review-independent/test_postpilot_independent.py -q
```

结果：**58 passed in 18.01s，退出码 0，无 skip。** 其中 10 个独立探针额外验证 malformed 包装仍不可确认；raw receipt、effective receipt、pre 与 post 任一关键链接被篡改均拒绝；部分 ID、重复 writer 结果、交错 writer 和失败的持久 post 均拒绝。实际已安装 Hermes SDK 的无网络测试覆盖合法和 malformed 两种调用，不以 mock handler 替代 coercion 路径。

另对保留的原生 pilot stdout 和数据库做一次只读诊断，仅重查“是否真实写出这份草案”的出处，不调用模型、确认或业务执行，也不保存新评分。新门禁得到：

- `proof_kind=unique_native_write_signature`；原始日志零基位置为 `[16, 17]`。
- 参数 SHA256：`4fe14a04afc53fd2ea2080168700c3e47ce63c4bdd32119ad1abde7aea750602`。
- 内容 SHA256：`79d83cd7588f9e94584a8140da4a30446861ede6a0fec9e54fc5d1c0c44ae225`。
- 原始 pilot 报告仍为 failed、未批准、未执行；该只读诊断不能作为新增成功试验计入。

## 固定源码摘要

| 文件 | SHA256 |
|---|---|
| `src/rpe_harness/integrations/hermes_review.py` | `2284bfc6787ef21456c3d7781a63de520fc38a643432e2be0eb225185c46358d` |
| `src/rpe_harness/experiments/review_comparison.py` | `aaabeafef975e428ccfb37fc832dbba885ea2e41da361563ffd2d6c9e7cc402d` |
| `tests/test_hermes_review.py` | `b511fc79c781f0136f32bb35a859698372f69887797479e3d70e8b080b9279f5` |
| `tests/test_review_comparison.py` | `ca677c2cf4704ef5074d0e077ac3bafef36ae0c70f3608440fbc9efba12a9c64` |
| 未修改的 `src/rpe_harness/experiments/address_comparison.py` | `2a1b92174c9ac058b5e20c317129aafe681833fa6dff70404dcb35fccd03645c` |

本审阅未调用模型或网络 provider、未运行完整测试集、未重新打包，也未修改或清理用户日常状态。根代理的最终回归、安装包、冻结与后续真实实验需要独立记录。malformed 草案仍可能发生，模型质量改善没有由此次出处修复证明；模拟审阅和真实用户语义审阅的边界保持不变。
