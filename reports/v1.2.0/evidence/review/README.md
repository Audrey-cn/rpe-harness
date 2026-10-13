# 独立审查与安装后崩溃证据的历史范围

本目录原样保留修复前发现、当时复审、独立探针源和原安装候选的崩溃恢复记录。原候选 wheel SHA256 为 `bee526c7cb37844d2036602a3732b678c1d365f6e832d33c936733afbc47e1fc`。当时的 7 项独立复审、17 项重点回归和 3 项安装后 owner-crash 检查都通过；初次脚本错误的退出码断言也独立保留。

**这些是旧快照的结论，不能提前称为最终发布无阻断。** 后来的真实 36 会话发现了“tool_choice=none 后额外调用仍被执行”的入口 guard 缺口，最终代码将补齐 `integrations/hermes.py`。根任务还需补独立 guard 复审、最终全套回归和新版精确 wheel 安装验证。此目录不重新解释或覆盖原审查人的历史结论。

## 保留内容

- [修复前独立审查](independent/independent-review-initial.md)：缺陷与当时的行号。
- [当时的独立复审](independent/independent-review.md)：适用于 [31 个 Python 源文件 hash 清单](recheck/reviewed-source-sha256.json) 的特定旧快照；包含后来追加的原安装候选崩溃结果。
- [初始不安全行为探针](independent/test_adversarial.py) 和 [原 control observer 探针](independent/control_probe.py)：历史复现源。前者断言的是旧缺陷成立，不是供最新产品通过的正向回归。
- [正确预期的独立复审探针](recheck/test_recheck.py)：检查拒绝、隔离、原子切换、回退和沙箱边界。
- [原安装候选的三阶段崩溃证据](installed-crash/installed-crash-evidence.json)、[初次退出码断言问题](installed-crash/probe-initial-assertion-record.json) 和 [复现脚本](installed-crash/reproduce_installed_crash.py)。
- [保留文件清单与 SHA256](retention-manifest.json)：原文件逐字节复制，未执行探针、模型或 API 调用。

报告原有的 `../recheck/`、`../installed-crash/` 和同目录文件引用保持目录关系。`pytest-own-final`、`pytest-targeted-final` 等文字表示当时临时运行目录；本目录按要求不保留这些目录、SQLite、原始聊天历史或缓存。JSON 中的绝对路径同样是历史归属记录，不表示这些临时路径在发布后仍存在。

## 复现范围

探针源保留原始路径和断言以供审查，不在证据整理过程中执行。源码复审探针依赖仓库的 `tests/test_candidates.py`；迁移机器时应在审阅后设置自己的源码/临时根路径。原始失败探针应与对应旧快照结合解释，不能把预期缺陷失败当作当前产品缺陷。

安装后崩溃脚本必须复制到新的、仓库外的隔离根的 `review/installed-crash/`，并在该根的 `release-venv/` 安装要验证的确切 wheel。随后从复制后的脚本目录以该 venv 的 Python `-I -B` 运行。脚本断言运行位于仓库外，并以其目录祖先定位 `release-venv`，所以**不要直接从本仓库的证据目录执行**。它会在所选隔离根内创建独立夹具、数据库和新报告；保留于此的旧报告不得被覆盖成新版验证结果。
