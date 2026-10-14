# Registry 发布前验收（1.4）

本说明提供本地发行包和离线 CI 接入草稿。命令构建、校验本地新目录，不上传远程 Registry、不改变旧资源仓 workflow，也不激活候选或扩大信任根。

## 签名与包边界

构建方显式提供已验证版本 ID、已有数据库、creator identity、registry identity 和本地 keyring。creator 使用 `capability-package` 签名用途及 `creator` 角色；registry 使用 `registry-index` 用途及 `registry` 角色。两种用途分别验签；签名证明来源和声明绑定，业务正确性由独立 fixtures 判断。keyring 必须由使用方在本地明确管理，验证会检查其当前撤销状态，不导入发行包推荐的信任策略。

发行包包括：

```text
release-bundle/
  catalog.json
  audit.json
  packages/<asset_id>/manifest.json
  packages/<asset_id>/signature.json
  packages/<asset_id>/candidate.py 或 recipe.json
```

catalog 签名覆盖包摘要与 audit 摘要；audit 绑定原版本、证书和 creator key 摘要。audit 是发行者对来源证书与证据的见证，不能替代接收方验收。包不包含私钥、运行正文、数据库、运行 ID 或绝对状态路径。当前发行 ABI 只支持已验证 CSV 执行器/配方；旧 gene 默认供发现。1.5 的 JSON 本地候选已有专用验收与隔离路径，但尚未接入签名发布包 ABI，详见 [JSON 升级](json-upgrades.md)。

所有路径使用规范绝对路径，各级禁止 symlink；macOS 临时目录可用 `/private/var/...`。构建目标必须不存在，其父目录必须已存在。已有包、坏链接或旧报告都不会覆盖；报告路径必须在发行包之外，且其父目录已存在。

## 本地构建

把占位路径和 VERSION_ID 替换为本次明确准备的对象。`--version-id` 可重复传入，支持 1..100 个不同版本。构建重新检查当前修订、独立验证证书和仍可复查的运行证据，因此需要原数据库及证据文件可用；已验证但后来证据损坏的版本不能直接导出。

```sh
rpe-harness assets registry-build \
  --db /absolute/path/owned-state/runs.sqlite \
  --version-id VERSION_ID \
  --destination /absolute/path/release-bundle \
  --creator-identity /absolute/path/creator-identity.json \
  --registry-identity /absolute/path/registry-identity.json \
  --keyring /absolute/path/local-keyring.json
```

构建完成时 `built=true`，并附本地 integrity 结果；`published=false`、`activated=false` 表示没有远程发布和激活。临时构建目录通过校验后才独占写入目标。

构建与导出只读源数据库和证据，不迁移 schema、恢复激活或写反馈缓存。状态库父目录、构建父目录及 owned scratch 必须可信；发布前后核对持有的 stage 身份，但目录名称的 rename 不构成对任意同 UID 进程的完整隔离。检测到 stage 被替换时拒绝，并保留陌生替代目录供检查，不冒险删除它。

## 两种验证等级

```sh
rpe-harness assets registry-validate \
  --bundle /absolute/path/release-bundle \
  --keyring /absolute/path/local-keyring.json \
  --mode integrity \
  --report /absolute/path/evidence/integrity.json

rpe-harness assets registry-validate \
  --bundle /absolute/path/release-bundle \
  --keyring /absolute/path/local-keyring.json \
  --mode acceptance \
  --owned-root /absolute/path/owned-scratch \
  --report /absolute/path/evidence/acceptance.json
```

| 等级 | 实际检查 | 成功报告 |
|---|---|---|
| integrity（默认） | 签名角色/用途/撤销、版本修订、catalog/audit/package 原字节、摘要绑定、精确文件集合 | `passed=true`、`producer_validation=attested`、`independent_acceptance=not_run` |
| acceptance | 上述检查，再在显式 owned-root 下重跑已有 CSV/recipe 独立 fixtures | `passed=true`、`independent_acceptance=passed` |

两种模式始终保持 `published=false`、`activated=false`。integrity 成功不能当成独立业务验收成功；过时修订、缺文件、额外文件、symlink、坏签名或撤销均失败关闭。acceptance 的 owned-root 必须已存在，临时子目录归本次调用并在结束时清理。含 Python 执行器的 acceptance 需要 macOS 沙箱及资源观测可用；Linux 可以做 integrity，不能将缺沙箱的代码验收跳过并报告 passed。recipe 验收复用内置执行器；具体失败保留在结果中。

独立模块入口不需要打开默认运行数据库，可用于固定安装物的离线 CI：

```sh
python -B -m rpe_harness.assets.prepublish \
  --bundle /absolute/path/release-bundle \
  --keyring /absolute/path/local-keyring.json \
  --mode acceptance \
  --owned-root /absolute/path/owned-scratch \
  --report /absolute/path/evidence/prepublish.json
```

独立入口成功退出 0，校验或写报告失败退出 1；集成 assets 命令也使用非零退出码表示失败。报告仅创建新文件，不写回发行包。不要让 CI 仅打印 JSON 而忽略退出码。

## 固定 wheel 的离线 CI 示例

以下是待接入现有 CI 的 shell 示例，不是已修改的 Registry workflow。预先准备 wheel、依赖 wheelhouse、发行包、本地公钥 keyring 和新报告路径；把审核认可的 64 位 wheel SHA256 固定在 CI 配置中。不要在同一次运行中对待验证 wheel 现算 hash 再把它当成期望值。依赖 wheelhouse 另按 CI 的依赖锁管理，这个 hash 只绑定 Harness wheel。

```sh
set -eu
: "${RPE_WHEEL:?canonical absolute wheel path}"
: "${RPE_WHEEL_SHA256:?approved 64-character lowercase wheel SHA256}"
: "${RPE_WHEELHOUSE:?offline dependency wheelhouse}"
: "${RPE_BUNDLE:?canonical absolute release-bundle path}"
: "${RPE_KEYRING:?explicit local public keyring path}"
: "${RPE_VALIDATION_REPORT:?new report path outside bundle and scratch}"

CI_OWNED=$(mktemp -d "${TMPDIR:-/tmp}/rpe-prepublish.XXXXXX")
CI_OWNED=$(cd "$CI_OWNED" && pwd -P)
trap 'rm -rf "$CI_OWNED"' EXIT
export TMPDIR="$CI_OWNED" TMP="$CI_OWNED" TEMP="$CI_OWNED"
export PYTHONDONTWRITEBYTECODE=1

python3 -I -B - "$RPE_WHEEL" "$RPE_WHEEL_SHA256" <<'PY'
import hashlib, re, sys
expected = sys.argv[2]
if not re.fullmatch(r"[0-9a-f]{64}", expected):
    raise SystemExit("invalid pinned wheel hash")
digest = hashlib.sha256()
with open(sys.argv[1], "rb") as wheel:
    for chunk in iter(lambda: wheel.read(65536), b""):
        digest.update(chunk)
if digest.hexdigest() != expected:
    raise SystemExit("wheel hash mismatch")
PY

python3 -I -B -m venv "$CI_OWNED/venv"
CI_PY="$CI_OWNED/venv/bin/python"
"$CI_PY" -I -B -m pip --isolated install --no-cache-dir --no-index \
  --find-links "$RPE_WHEELHOUSE" "$RPE_WHEEL"
"$CI_PY" -I -B -m rpe_harness.assets.prepublish \
  --bundle "$RPE_BUNDLE" --keyring "$RPE_KEYRING" --mode acceptance \
  --owned-root "$CI_OWNED" --report "$RPE_VALIDATION_REPORT"
```

该例隔离安装环境、忽略用户 site/PYTHONPATH、禁用 pip 缓存和联网取依赖；退出时只清理本次随机 owned-root。报告留在显式 evidence 路径供 CI 收集，原包/keyring 保留。使用包含 Python 执行器的包时在 macOS runner 执行；缺所需能力就以非零退出码结束。接收方应另外固定并审核本地 keyring；运行此例不会上传包或自动配置远程发布。
