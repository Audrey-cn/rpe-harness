# 独立 trusted-app 后测确认与执行

该次后测已成功，run_id 为 `358f91274d8d4673949865f81b87f609`。原始真实 Hermes proposal `171678088ebf0b7b9b825698663e8eaaa0655eb6c5caa2741aa79a16e2685d02`、会话、摘要和工作目录绑定均未改变；只在 SQLite backup 副本确认并执行。actor 为 `simulated_semantic_review`，`human_confirmation=false`。

两臂原候选都仅少了 fixture 的 `preserve_original` flag。原请求没有额外要求此标志，而 json.normalize 无条件保留原输入；因此本次独立语义审阅接受这个原候选。原评分器的精确 flag 匹配结果保持 false，正式试验及 JSON 对的 primary 记录仍为 0，原 exports 未改或重导出。此后测不是原生对照，没有将任何历史失败重评为成功。

确认前没有输出；确认本身没有执行。唯一实际执行 succeeded 且 worker 清理完成；实际 durable 冻结合同与原 proposal machine contract 完全相等。独立发布后 oracle 检查原输入 hash、全部原始 JSON token 与紧凑样式；产物也与公共 fixture 的精确预期字节一致。终态 API 重放返回同一 run/result，副本数据库恰有一条业务 run，原 workspace 仅新增 output.json。原数据库主文件、WAL 业务字节、records/ledger/drivers、三份既有 exports、overall-costs 和原输入摘要前后相同；shm 仅属 SQLite 元数据。

新增 Hermes 模型调用 0、controller 执行 API 0。原 46 API 的模型记账没有改变；本次 Codex 独立语义审阅属于开发验证，其 token/账单未测，不可称整个过程无模型消耗。当地确认 0.114546s、执行 0.426560s、发布后独立 oracle 0.000715s、终态读取式重放 0.001939s，单列在 receipt，未混入原 45 阶段账本。

原模型要求行只导出出处引用、字段与文字 hash，不保留模型杂文。原 private document 的 ID/摘要在运行时重开核验；脱敏后的公共字段不能独立重建私有原文摘要。确认、durable 及来源摘要属于记录的运行核验，不是第三方密码学证明。

本目录的 stdlib 离线脚本不依赖 owned-root、工作树绝对目录或 RPE 安装；在本目录运行 `python3 verify-controller-flow.py`，或从任意位置指定 `--root`。它核验公开文档/manifest、确认绑定、原候选及引用、冻结合同的公开投影和已记录的原始摘要相等，并通过独立结构空白字节 oracle 复核 input/output。`offline-verification.json` 为实际复核结果。
