# D3 发布候选合同 v1

D3 只接受已获人工批准版本的 D2 计划：`bundle_version` 必须为 `2.1.0`，`release_state` 仍为 `unreleased_candidate`，`next_action` 必须为 `run_approval_required`。D3 不 tag、push、创建 Release 或切换 stable。

## 真机公共接缝

`release/d3_candidate.py preflight` 只读绑定 D2 seal、`plan_id`、候选长度与 SHA-256、候选 ID、bundle 版本、三仓 commit、目标运行时和解释器。`run` 只能在目标真实操作系统和架构上继续，随后使用候选内置 Python 完成锁定依赖检查、合成关键词 Query、Skill 事务安装、公共 verify、undo-check 与 undo；候选、运行时树和合成 Vault 不得漂移。

`.github/workflows/d3-macos-candidate.yml` 使用标准托管 runner `macos-15-intel` 验收 `macos-x64`，使用 `macos-15` 验收 `macos-arm64`。控制 Job 从三个精确提交重建两次可复现候选；所有第三方 Action 锁定完整 commit，不使用仓库 secret。两份完成回执分别通过 GitHub artifact attestation 建立 workflow、repository、commit、runner 与产物之间的可验证来源关系。正式接受前必须运行 `gh attestation verify`，并用 `--signer-workflow montewaltrip188-hash/obsidian-wiki-setup/.github/workflows/d3-macos-candidate.yml` 限定签发工作流。

## 发布候选与签名

三平台 `d3-candidate-acceptance` 回执必须全部为 `completed`，并精确绑定同一 D2 `plan_id`、候选 ID、候选 SHA-256、bundle `2.1.0` 和 `cpython-3.12.14+20260825`。macOS 回执还必须包含当前安装器 commit 与 GitHub run 来源。

`release/d3_release.py prepare` 必须先用 `gh attestation verify --bundle` 验证两份 macOS 回执及其离线 Sigstore bundle，并锁定仓库、signer workflow、source commit、signer commit 和非自托管 runner。通过后组装两个候选 ZIP、两份 SBOM、D2 计划、三份回执和两份 attestation bundle，共 10 个逐文件记录。规范化 `release-manifest.json` 要求 `RSA-SHA256-PKCS1-v1_5` 分离签名，并从仓库内 `release-signing-policy.json` 写入生产公钥指纹 `key_id`。维护者在 Windows 签名机使用 `release/new-signing-key.ps1` 生成至少 3072 位 RSA：私钥为仓库外加密 PKCS#8，随机高熵口令只以当前 Windows 用户 DPAPI 密文保存；签名时由 `release/sign-manifest.ps1` 在内存中解封，口令不得进入命令行、日志或回执。生产签名私钥和 DPAPI 密文不得进入仓库、候选或 CI，仓库只保存公钥与固定信任策略。Windows 使用 `release/verify-manifest.ps1`，macOS 使用 `release/verify-manifest.sh`，`release/d3_release.py verify` 强制使用仓库固定公钥，并重新交叉绑定公钥指纹、完整文件集合、长度、SHA-256、D2 计划、三平台回执、架构、候选和证明 bundle 摘要。

## 停机门禁

任一候选漂移、回执 seal 失效、架构不匹配、Query/事务/回滚失败、GitHub artifact attestation 无法验证、生产公钥尚未确定、签名私钥风险或对外版本语义变化，都必须停止。仅当三平台回执、两份 macOS 证明、10 个文件摘要和生产 RSA 签名全部闭合，D3 才能报告发布候选验收完成；此状态仍不授权 tag、push、Release 或 stable。
