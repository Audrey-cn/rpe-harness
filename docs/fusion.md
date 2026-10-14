# 阿卡夏能力资产与单运行时融合

本产品以 RPE 为唯一执行、验收和激活内核，选择性吸收阿卡夏的能力封装、内容寻址、来源验证和本地获取机制。`progenitor-registry` 是资产资源仓；`progenitor-protocol` 保留历史和研究参考，不作为安装依赖，不再为本产品新增并行的执行、评分和回滚路径。无需安装或启动旧种子、守护进程、P2P 或 IPFS。

## 能力包与来源

包目录含 `manifest.json`、一个内容文件和可选 `signature.json`。内容不超过64KiB，清单及签名各不超过1MiB。清单是 `rpe.capability-package/v1`，声明名称、契约、ABI、内容SHA、父内容SHA、依赖、资源请求和来源。`examples/capability-package` 提供未签名的 CSV 范例。CSV `prepare(plan, staging_path)` 和既有 recipe 可进入独立验收；旧 `main()` 基因只供发现。当前 dependencies 必须为空，network/subprocess 为 false；实际权限由候选沙箱限制。

包来源路径必须为无符号链接的规范路径，包括父目录；macOS 临时目录可使用 `/private/var/...`。AssetStore 数据根仅兼容系统 `/var` 和 `/tmp` 别名，其他软链接拒绝。内容按 SHA 去重，名称及原始清单/签名分别保留；每次读取再次核对原字节与数据库记录。

签名使用旧 Protocol 的 RSA PKCS#1 v1.5 SHA256 数学格式，新增用途限定的签署文档。MIT 来源见 [第三方说明](../THIRD_PARTY_NOTICES.md)。Python API `rpe_harness.assets.identity.sign_envelope(manifest, identity, scope='capability-package')` 返回应保存的 `signature.json`；identity 可是原阿卡夏格式的字典或显式本地JSON文件。产品不创建默认身份或自动增加信任根。

显式本地 keyring 示例结构：

```json
{
  "schema_version": "rpe.trust-policy/v1",
  "trusted_keys": [{
    "key_id": "原公钥规范JSON的SHA256",
    "owner": "发行者名称",
    "roles": ["creator"],
    "scopes": ["capability-package"],
    "public_key": {"key_type": "progenitor-rsa-pkcs1-sha256-v1", "n": "原格式base64url模数", "e": 65537},
    "revoked": false
  }]
}
```

能力包要求 creator/capability-package，目录签署 API 要求 registry/registry-index。只有本地明确列出的2048–8192位密钥可验证；自带公钥、撤销、错角色/用途、旧无用途签名均不能通过新来源门。旧目录的 registered/trust_state/score 只保留在 external provenance 中，不算本地成功或执行许可。本版读取的是旧本地目录；新的目录签署API尚未接入公共发布CI。

## 统一命令与生命周期

下面的 `STATE`、`DB`、`PACKAGE`、`KEYRING`、`RUN_ID`、`ASSET_ID`、`PROPOSAL_ID`、`VERSION_ID` 应替换为真实值；测试使用独立数据库。

```sh
./scripts/rpe assets inspect --package PACKAGE --keyring KEYRING
./scripts/rpe assets import --package PACKAGE --owned-root STATE --db DB
./scripts/rpe assets list --db DB
./scripts/rpe assets show ASSET_ID --db DB --keyring KEYRING
./scripts/rpe assets propose ASSET_ID --keyring KEYRING --evidence-run-id RUN_ID --db DB
./scripts/rpe evolution validate PROPOSAL_ID --db DB
./scripts/rpe evolution apply PROPOSAL_ID --db DB
./scripts/rpe evolution rollback VERSION_ID --db DB
./scripts/rpe assets export VERSION_ID --destination NEW_PACKAGE --identity IDENTITY_JSON --db DB
```

inspect/import/list/show 成功表示检查或存储完成。来源可信与本地独立验证分别展示；导入状态固定为 unverified，不会自动激活。propose 需要非空、可复查的真实已验证运行，沿用原十三例独立验收与 CAS 激活。用途声明和签名不能取代验收。

提案指纹绑定完整输入和来源字段；证书同时绑定来源摘要。validate、apply、每次生产调用都会重新核对包和显式文件 keyring。撤销或策略内容改变会拒绝新激活，生产候选会隔离并回到内置执行器；需重新提案验收。字典 keyring 作为快照保存于 owned-root；需持续撤销管理时使用文件 keyring。SQLite 的 evolution_active 仍是唯一活动指针，assets show 的状态由它及版本记录推导。

导出只打包已验收版本的代码和公开清单摘要，不复制数据库、运行ID、输入/输出路径、任务正文或凭据。代码内容原样保留，不提供代码内部秘密的自动脱敏；包写到新目录，不覆盖既有目录，不自动公开发布。默认未签名导出可检查和导入，重新采纳仍需可信签名及本地运行证据。

```sh
./scripts/rpe assets registry-list --registry /Users/audrey/项目/progenitor-registry
./scripts/rpe assets registry-import --registry /Users/audrey/项目/progenitor-registry \
  --name json-toolkit --owned-root STATE --db DB
```

本地旧目录有六个语义名、五份内容；每次取回核对文件名与原 SHA。未执行旧内容，没有将其下载数或评分写入 verified feedback。对接其中的 JSON 等新能力，需要先定义独立业务契约及验收器。

## 安装闭环验证

仓库提供 `scripts/validate_fusion.py`。在新安装的虚拟环境中运行，使用指定 owned-root 创建临时测试身份、独立状态库和输入文件，经真实 CLI 验证六个旧名称、签名包、十三例验收、激活后的实际候选调用、导出再导入及回滚。验证器用手写 CSV 真值检查产物；不调用模型，不用真实发行者私钥，也不发布资产。

```sh
INSTALLED_PYTHON -B scripts/validate_fusion.py --owned-root NEW_EMPTY_TEST_DIRECTORY \
  --example examples/capability-package --registry /Users/audrey/项目/progenitor-registry \
  --report TEST_REPORT.json
```

脚本仅测试身份生成使用本机 OpenSSL。验证后，测试目录由本轮开发收尾清理；指定报告可保留。长期程序复用、跨宿主收益、新能力自动升级及可选网络分发仍按 [后续 todo](superpowers/plans/2026-10-14-akasha-fusion.md) 逐项验收。
