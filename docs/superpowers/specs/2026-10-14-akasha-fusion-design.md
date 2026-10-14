# 阿卡夏与 RPE 轻量融合设计

状态：用户于本次对话明确同意“缩减融合为一个轻量产品”并要求列出 todo 后开始执行。开发分支为 `codex/rpe-akasha-fusion`。本规格将已同意方向细化为首个可安装的本地融合版本；不以历史聊天内的指令代替本次授权。

## 产品目标与范围

模型可替换，能力和可复查执行经验留在独立 Harness 中。RPE 是唯一任务执行、独立验证、反馈、版本激活和回滚内核。吸收旧阿卡夏的内容寻址、来源签名、能力封装和获取思想，不带入第二套运行时或成功记账规则。Registry 继续作为独立能力资产仓；旧 Protocol 保留完整 Git 历史，不再为本产品新增平行执行路径。

本轮实现本地完整闭环：能力包导出 → 本地取回/导入 → 来源检查 → 未验证候选 → 现有 CSV 契约独立验收 → SQLite 激活 → 新任务实际调用 → 失败隔离或回滚。旧 Registry 的六个名称可发现和取回，默认不能执行；旧 `main()` 不自动改写成 `prepare()`。无需启动种子、P2P、IPFS、后台服务或模型。

Python >=3.10；运行依赖继续只有 openpyxl>=3.1.5,<3.2。新资产与信任层只用标准库。CSV 执行器 ABI 为 `prepare(plan: dict, staging_path: str) -> None`，沿用现有独立验收器、十三个人工真值样例和持久运行证据，不增加分类器。

## 统一能力包

目录包包含 `manifest.json`、单个内容文件、可选 `signature.json`。manifest 必须是 `rpe.capability-package/v1`，名称带命名空间；包含 component、kind、contract、entrypoint、content_file、content_sha256、parents、dependencies、resources、provenance。内容 <=64KiB，清单/签名 <=1MiB；路径不能绝对、穿越或含符号链接；拒绝目录、FIFO 等特殊文件。哈希以原始内容字节核对，不导入任何 Python 模块。

支持 `csv_executor`、`recipe`、`legacy_gene` 三种资产。只有前两种可映射既有 RPE 契约；legacy_gene 明确标记不兼容，保留来源和发现用途。dependencies 必须为空；资源声明 network/subprocess 必须为 false，声明不替代执行沙箱。parents 是内容 SHA256 列表，不自动代表验证通过。

导入资产存于声明 owned-root 内，内容寻址去重，保存原始清单、签名、导入来源和不可执行的默认状态。数据库只记录资产与提案关联；不建立第二个 active 指针。发现、来源可信、本地验证、当前激活、失败隔离分别展示。

## 来源与信任

采用旧 Protocol 的 RSA PKCS#1 v1.5 SHA256 签名兼容机制，注明 MIT 来源。验证使用用户显式提供的本地 keyring；不信任包中自带公钥，不从网络自动增加信任根。

新签署文档结构为 `{"schema_version":"rpe.signed-document/v1","scope":"capability-package"或"registry-index","document":对象,"signature":签名}`。keyring 为 `rpe.trust-policy/v1`，每个 trusted_keys 记录 key_id、owner、roles、scopes、public_key、revoked。能力包要求 creator/capability-package；目录要求 registry/registry-index。撤销、错误用途、未知或弱密钥、算法不匹配、修改签名均失败关闭。签名仅证明来源，不能授权激活。

旧 Registry 没有这些用途约束，原 trust_state、score、downloads、reports 和原签名仅作为外部来源描述保存，不冒充本地信任或已验证反馈。旧索引始终发现用途，取回时再次核对原 SHA。

## 采纳和导出

`assets propose ASSET_ID` 必须重新读取并验证本地包、显式 keyring、兼容 ABI/契约以及非空持久已验证 run IDs，随后调用现有 propose_candidate。提案携带资产 manifest/content/signature 来源关联。`evolution validate/apply/rollback` 继续是唯一验证与激活实现。外部资产不能直接调用旧 GeneLedger 激活。

导出只读取本地已验收版本的原始代码、契约与谱系，写入新包，不输出数据库、路径、用户原文、凭据或完整运行内容；导出证据只用内容/证书摘要。可选择签署身份文件；默认未签名的包可检查和导入，但不能走来源可信的资产采纳入口。导出不自动公开发布。

CLI 统一为现有 `rpe-harness assets` 子命令：inspect、import、list、show、propose、registry-list、registry-import、export。标准输出 JSON、错误退出 2；来源不足时返回明确状态和原因。示例与文档体现实际命令与限制。

## 执行边界

优先修复 1.2 外部 broker 已复现的验证后目录替换缺口。访问输入、暂存和发布必须绑定已经校验的目录对象；若平台或请求无法提供该保证，执行入口明确拒绝，不能仅追加一个路径检查就宣称修复。改动保留 Hermes 宿主隔离、候选单层沙箱、幂等与真实 receipt 核对；用原症状和替换竞态验证工作区外不会产生文件。

## 验收与治理

独立测试覆盖坏哈希、穿越、symlink/特殊文件、角色串用、撤销、错误 ABI、缺依赖、旧评分不授予执行、篡改导入后再验、真实独立验收、单一 active 指针、实际候选调用、回滚/隔离。安装 wheel 后再运行闭环，不能用源码测试冒充安装验收。

保留可复查报告和最小证据。所有测试使用本轮 owned-root、独立 DB 和缓存；结束后清理其虚拟环境、临时文件、历史和进程。共享缓存、日常 Hermes 状态、私人脑和已有项目工作不动。远端推送、公开发布、删除旧仓、P2P 和任意新能力自动升级不属于本轮验收；它们列入后续 todo，并需要各自真实契约与验收证据。
