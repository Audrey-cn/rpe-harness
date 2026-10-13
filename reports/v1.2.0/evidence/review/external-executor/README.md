# 外部执行入口独立证据与已知目录替换缺口

[36项检查的紧凑结果](broker-independent-evidence.json) 与 [XML](independent-final-2.xml) 属于源 hash 明确的32文件快照，耗时15.04秒，无模型调用、无日常写入。它覆盖认证、receipt/API策略绑定、重放、真实外层沙箱候选调用、超时回退、取消和连接关闭；不证明模型端到端验收，也不覆盖下面额外目录替换 finding。原 [33项历史检查](broker-independent-evidence-pre-socket-fix.json)、[历史XML](independent-final-1.xml)、[逐项元数据](broker-probe-detail.json)、[测试源](test_broker_independent.py) 和 [辅助源](probe_support.py) 原样保留。

[slowdrip原RED](slowdrip-red/evidence.json) 记录 context 在3.5秒后仍阻塞；36项后续结果包含主动关闭该连接的检查。这个已修连接问题与目录替换问题不同。

**额外目录替换探针仍为RED，未修复。** [原始紧凑证据](workspace-swap/evidence.json) 显示服务端验证后、外部CLI启动前替换目录，HTTP 200、result_status=succeeded，outside_output_exists=true。它实际在声明工作区外生成了人工CSV，broker源 hash 与36项结果相同。它没有模型调用或日常写入；outside_output 是人工业务结果，不是聊天内容。36项 GREEN 不能覆盖或推翻此 finding，也不能据此宣布整体安全审阅通过。

本版信任父进程、SQLite 和运行期间稳定的工作目录。Hermes 宿主保持文件沙箱，严格任务工具为rpe_task；父侧固定CLI受契约/独立验收约束，却没有相同的工作区文件沙箱，同UID外部并发目录替换未被硬隔离。路径检查、hash和事后验收没有把目录对象原子绑定。1.3应实现已校验目录文件描述符绑定的读取/暂存/发布，并独立复验目录替换和符号链接竞态。这里保留观察，不改生产源、不重跑安全实验、不把finding写成已修。

[保留清单](retention-manifest.json) 逐文件记录原路径、大小和SHA256。仅复制顶层已知compact JSON、无raw输出的XML、无凭据的测试/辅助源，以及指定slowdrip/workspace-swap evidence.json；未进入pytest目录，未复制SQLite、rawhome、cache、session或日志。JSON中的log字段仍是原历史名称，但原日志未保留；junit字段对应本目录原样XML。原绝对临时路径清理后可能失效；复现测试需要完整仓库tests及指定隔离环境，不能把本目录当作自动安全测试入口。
