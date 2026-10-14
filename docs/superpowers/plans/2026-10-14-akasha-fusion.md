# 阿卡夏与 RPE 轻量融合 Todo / Implementation Plan

> For agentic workers: use superpowers:subagent-driven-development or superpowers:executing-plans; retain progress here and verify each task.

**Goal:** 在一个 RPE 产品中完成本地能力资产的导入、来源验证、独立验收、激活、实际使用、回滚闭环。

**Architecture:** RPE SQLite 为执行与激活唯一权威。轻量 assets 模块吸收阿卡夏的封装、来源与获取逻辑，Registry 保持资源仓；旧 Protocol 历史保留，不引入其运行时。

**Tech Stack:** Python >=3.10，标准库、SQLite，openpyxl>=3.1.5,<3.2；macOS 现有候选沙箱。

**Spec:** ../specs/2026-10-14-akasha-fusion-design.md

## Global Constraints

- 运行依赖继续只有 openpyxl>=3.1.5,<3.2；新资产与信任层只用标准库。
- CSV ABI 为 `prepare(plan: dict, staging_path: str) -> None`；沿用现有独立验收与真实持久证据。
- 一个执行运行时、一套验证结果、一个 SQLite 激活指针；发现、签名与外部评分不授予执行。
- 旧仓不覆盖、不删历史；不运行种子/P2P/IPFS/后台服务，不写全局环境。
- 测试、缓存、数据库、虚拟环境和临时进程全部归本轮 owned-root，收尾清理。

## Review Focus

1. 导入后更换文件、目录、软链接或特殊文件：再验失败，不能执行或向范围外写出。
2. 自带公钥、撤销密钥或 creator/registry 角色串用：失败关闭。
3. 旧评分、registered 标记、错误 ABI：仅发现，不导入成功证据或激活。
4. 重复导入、同代码不同来源、相同名称不同内容：按内容和清单区分，保留来源。
5. 导出私人路径/用户内容、安装缺 sibling：导出不泄漏，安装包自包含。

## 本轮 Todo（首个融合可用版本）

### Task 1：资产格式、本地内容仓和旧目录适配

Files: `src/rpe_harness/assets/{__init__,manifest,store,registry}.py`，`tests/test_assets.py`。
接口：`inspect_package(path)->dict`；`AssetStore(db_path,owned_root=None)` 提供 import_package(path)、list_assets()、get_asset(asset_id)、read_content(asset_id)；`read_registry(path)->list[dict]`；`import_registry_asset(store,registry_path,name)->dict`。

- [ ] 写坏哈希、路径穿越、symlink/FIFO、重复/别名、旧六名称和五内容的失败测试并确认 RED。
- [ ] 实现有限长字节读取、严格 manifest、不可执行导入、内容寻址与原 SHA 核对。
- [ ] 定向测试 GREEN，记录实际命令与范围；独立审核。

### Task 2：用途限定的来源验证和旧签名兼容

Files: `src/rpe_harness/assets/{identity,trust}.py`，`tests/test_asset_trust.py`，MIT 来源说明。
接口：`verify_envelope(envelope,keyring,*,scope,role)->dict` 返回 passed/owner/key_id/reason；`sign_envelope(document,identity,*,scope)->dict`；keyring 可为 JSON 字典或文件。

- [ ] 写未知/self-attested、撤销、弱密钥、篡改、角色和 scope 串用测试并确认 RED。
- [ ] 选择性移植现有 PKCS1-SHA256 代码，保留 MIT；不导入 sibling、不生成全局身份。
- [ ] 定向测试 GREEN，独立审核；签名可信不能改变导入默认状态。

### Task 3：外部执行目录对象绑定

Files: `src/rpe_harness/integrations/hermes_broker.py` 及必要小模块/入口，`tests/test_hermes_broker*.py`。

- [ ] 复现原目录替换症状，确认 RED；探明单层候选沙箱约束。
- [ ] 用目录文件描述符绑定或等效系统权限边界保证输入、暂存、发布；不能只增加事前检查。
- [ ] 原替换探针、合法 CSV/XLSX、幂等与已有 broker 检查 GREEN；保留真实范围与独立审核。

### Task 4：统一采纳、版本导出与 CLI

Files: `src/rpe_harness/assets/lifecycle.py`、`src/rpe_harness/cli.py`，`tests/test_asset_lifecycle.py`、CLI 测试。
接口：`propose_asset(asset_id,db_path,*,keyring,evidence_run_ids,owned_root=None)->dict`；`export_version(version_id,db_path,destination,*,identity=None)->dict`。

- [ ] 写来源不足/不兼容/篡改不采纳、可信 CSV 包经原验收后实际调用和回滚测试，确认 RED。
- [ ] 联接 Task 1/2 与现有 EvolutionStore；提案持久绑定来源，不另建 active。
- [ ] 新增 assets inspect/import/list/show/propose/registry-list/registry-import/export 命令；导出仅公开摘要。
- [ ] 真正跑通导入→提案→13例验收→激活→新任务调用→隔离/回滚→导出再导入；独立审核。

### Task 5：交付、安装验收、清理

Files: README、architecture、版本号、`reports/v1.3.0/`、示例、必要验证脚本。

- [ ] 明确仓库收缩路径、单产品边界和后续 todo；包 attribution 与迁移说明齐全。
- [ ] 完整回归、wheel 自包含安装验收、独立安装包闭环；真实 Hermes 入口验证相关改动。
- [ ] 最终独立审查、修复重要问题；逐条核对本规格。
- [ ] 保留报告与有限证据，清理本轮历史/缓存/环境/进程并核查日常状态。
- [ ] 本地提交可复查融合版本；不自动远端发布。

## 后续 Todo（不计入本轮完成）

- [ ] 新能力逐个建立业务契约与独立验收，再接入旧 JSON 工具等有用函数。
- [ ] 签名发布、密钥轮换/撤销分发与 Registry CI 正式闭环；需要实际发布授权。
- [ ] 按需求接入远端获取，之后评估可选 P2P；本地学习不依赖网络。
- [ ] 长序列程序复用、休眠再验证和持续学习收益实验，不预先宣称因果收益。
- [ ] 共享 ABI 稳定后评估旧 Protocol 归档/历史迁移与 Registry 共同版本发布；避免平行维护旧运行时。

## 进度记录

- 2026-10-14：用户已同意融合方向和开始执行；设计与 todo 建立，分支从 v1.2.0 创建。Tasks 1/2/3 文件无交集，可并行；Task 4 顺序消费 1/2 接口；Task 5 对整体验收。实现任务自行记录 RED/GREEN，控制器统一提交和审查。
