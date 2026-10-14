# 1.7 生成校准补充独立审计

**结论：这次单独校准留下了真实模型生成的 6,987 字节程序；保留的 native guard／API／工具身份与代码、参数摘要对应。原有 `generation_link.proven=false` 及空 `file_write_observations` 保持不变。没有资格验收、激活、冻结或生产复用验收。**

本补充只读审计 `wire-calibration` 和其实际保留代码／worker report，未调用模型、执行候选、资格验收、激活或重写原记录。机器明细为 `calibration-independent-audit.json`。此前失败实验的审计结论与费用未知项不被这次校准改写。

## 真实代码与 native 写入链

- 校准预登记 seal：`a1afa7e6aebbd18ae8eda8f0b61fc39ea44458b271e9caaf50e29d17c5c2be98`；`thinking_type=disabled`、`max_completion_tokens=8192`、timeout 180 秒、最多 6 turns、producer only；登记明确不执行 qualification，复用已存在的六个真实训练 run，不重复收取训练阶段费用。
- 独立核对预登记、report、compact record、reservation/outcome seals，worker 原报告与封装 record 的原字段一致，usage 与账本一致。保护的旧记录／代码在审计前后的 SHA256 一致。
- 实际代码 `ecff87575d8349718e68978575a9a783.py` 为 **6,987 字节**，SHA256 `52477be18715633f3739aea76d50f7d23447f62110de94a46cc300bb12022009`；与保留 report 和 native guard post 的 content hash、bytes-written 完全一致。
- 对 `{"path":"candidate.py","content":实际原始代码}` 独立重算 canonical JSON 参数 SHA256：`d083e579dc18e2398cd8d32ee405b473e0af09411fcdde852bd81a7e7cb9f6e7`。与唯一匹配 pre/post tool 的 arguments hash 一致；只保存摘要，不导出工具参数或代码正文。
- Session `20261014_222659_d4b07c` 有 2 对不同 API pre/post。API 1 固定选择 `write_file`，实际模型响应发出一次该工具，ID `call_a2188d1555ee475294316a64`；其 session/task/turn/API/tool 五个 ID 与 native guard pre/post 一一对应。guard post 为 `status=ok`、`verified=true`，记录真实写入 6,987 字节。API 2 的 tool choice 为 `none`，模型结束，无额外工具调用。
- 两次实际请求均仅提供 `write_file`、关闭 parallel tool calls、禁用 thinking、completion budget 8192；请求／已观测响应均为 `xiaomi / mimo-v2.6-pro`。

## 原评分与 CLI 证据缺口

原评分仍是 `false / no_matching_api_filewrite_observation`，原 `file_write_observations=[]`。native guard 的物理写入证据不会被回写成旧评分通过。

b7 源码的 `hermes_session._file_write_observations` 要求 CLI tool-use 和 tool-result 都有可匹配的 `tool_call_id`；`worker._generation_link` 还依赖 stream ID 与提取的 write observation。当前保留链显示 native guard 有完整 ID，但旧 CLI 提取链为空，与 CLI 缺 ID 的诊断相符。该校准的私有 host SQLite 与 raw CLI 流已按 worker 生命周期删除，审计不能再次独立查看那条 CLI 事件的具体缺失字段，也不能逐请求重算原始 provider usage。这里明确区分保留 native 证据与已删除的 CLI／DB 证据。

## 校准费用与资格边界

- 保留 worker／sealed report／compact record／账本一致报告：**2 次 API、12,345 token**，coverage complete，0 API errors、0 missing observations。
- 独立核对 aggregate 算术：10403 prompt + 1942 output = 12345 total；4259 uncached + 6144 cached = 10403 prompt。cached 不再次加入 total；reasoning=0，cache-write=null，账单金额未知。
- sanitized post observations 不保留每请求 usage；因此 **12,345 是一致且算术正确的受信保留 aggregate，未从现存 raw DB 逐请求独立重算**，不虚构此项证明。
- 若仅将这次单独校准追加到前三次已审计尝试，已知 token 下界为 784,233，完整总量仍 null；此数字不包含后续新调用。
- SQLite 中该 proposal 是 `proposed`，validation 为空，version_id 为空；对应 version 数 0、active 为空、activation 数 0、无 freeze。没有 qualification／candidate runtime probe／activation，也没有生产质量或复用证明。

## 候选代码的静态限制

AST 可解析，观察到的 imports 只有 csv/os/io。此审计没有运行候选或资格套件。

源码第 91 行先用 `O_CREAT|O_EXCL` 创建 staging file，第 99 行才进入 `_run` 验证 CSV 表头／数据；异常后再删除 staging。这个顺序不满足“无效输入拒绝之前从不创建 staging”的字面保证。它是静态源代码问题，不是本次运行测得的资格失败，候选仍未资格验收。

## b7 完整回归文件核对

- `full-wire-final.log` 完整结束：**1290 passed + 3 subtests passed，291.75 秒**。
- XML suite 记录 tests=1293，testcase 元素 1290，failure/error/skipped 均 0；suite time=291.658 秒。log/XML 统计相符。本审计未重跑测试。
- 历史测试代码 commit：`b7f3cafc4960fe1fb820e1d1ccc82f574f8c5eac`。独立逐字节比较该 Git commit 的 64 个 Python 文件与 `wheels-wire-final`，全部相同；wheel SHA256 `bac3c0769224cfbc82d9cd206ea1253128c98a9ff167bc6f8d89243aaafbce02`。
- 审计期间源码 HEAD 已是 `ef92f6b1b9f6bb70e8cad3957f98d59245429125`；当前源码与 installed 包的 `evolution/worker.py` 已不同于 b7 wheel。保留的 b7 回归不能认证后续改动；该差异在 JSON 中单独报告。

本补充证明校准的真实生成／写入事实及受信保留费用一致性，并保留原 CLI 评分缺口和未资格验收状态。没有把生成成功扩展成 1.7 学习闭环、候选质量或正式比较成功。

