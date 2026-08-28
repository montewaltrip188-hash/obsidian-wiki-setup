# 安装候选合同 v1

`tools/install_candidate.py` 是维护者侧、仅依赖 Python 标准库的组装器。公开接缝只有：

- `plan`：只接受三个仓库各自精确的 40 位 commit，记录对应 tree 和目标平台。
- `build`：只读取已绑定 commit 的 Git blob，并且只写入一个尚不存在的 staging 目录。
- `verify`：重新计算候选 ID、逐文件 SHA-256 和确定性 ZIP，发现文件集合或任一字节变化即失败。

## staging 证据布局

```text
staging/
├── manifest.json
├── deploy-manifest.json
├── vault.zip
├── candidate.zip
└── payload/
    ├── vault/
    ├── skills/claudecode-wiki-skills/
    └── installer/
```

产品仓库是 `payload/vault/` 的唯一来源。Skill 仓库以完整目录树进入 staging，以保留核心 Skill 对共享 `references/` 和 `scripts/` 的相对引用；默认只发现三个核心 Skill，IMA 保持可选。安装器仓库提供安装入口，但旧 `vault.zip` 不能再作为产品来源。

## 客户候选归档布局

`candidate.zip` 不复制 staging 的 `payload/vault/` 或 `payload/installer/` 前缀。客户解压后可以从根目录直接运行平台入口：

```text
candidate.zip
├── manifest.json
├── deploy-manifest.json
├── vault.zip
├── setup-win.ps1 / setup-mac.sh
├── install.bat / change-model.*
├── activation-public-key.xml
├── revoked-activation-ids.txt
├── extract-vault.py
├── tools/manage_wiki_skills.py
├── tools/joint_update.py
├── scripts/manage-wiki-skills.ps1
├── scripts/manage-wiki-skills.sh
├── scripts/vault-update.ps1
├── scripts/vault-update.sh
├── scripts/joint-update.ps1
├── scripts/joint-update.sh
├── contracts/joint-update-plan.schema.json
├── contracts/joint-update-approval.schema.json
├── contracts/joint-update-receipt.schema.json
├── contracts/
├── bundle-manifest.json
├── release/bundle-release.json
├── tools/vault_update.py
└── skills/claudecode-wiki-skills/
```

`payload/**` 只作为本地构建证据参与清单与 `verify`，不进入客户 ZIP。客户 ZIP 中的 Vault 只出现一次，即根目录的 `vault.zip`。

客户候选同时携带 U1 只读检查、U2 Vault 事务和 U3 Vault + Skill 联合事务入口及合同 Schema。产品版本合同和路径策略来自 `vault.zip` 内的产品树；Skill 兼容合同来自冻结的 Skill 树；安装器仓库只保存非自引用的 `release/bundle-release.json`，Builder 再以三仓精确 commit/tree 和最终 candidate ID 生成根目录 `bundle-manifest.json`。三者缺一或不闭合时，所有命令必须停止。仓库内 bundle 仍为 `unreleased_candidate` 且版本未分配时，候选只能用于隔离验收，不能冒充 stable 发行。

`vault.zip` 是构建产物而不是仓库资产，只包含唯一顶层 `vault/`。`deploy-manifest.json` 是安装安全层的窄接口：

```json
{
  "schema_version": 1,
  "archive": {"sha256": "...", "size": 123},
  "vault": {
    "tree_sha256": "...",
    "files": [{"path": "CLAUDE.md", "sha256": "...", "size": 123}]
  }
}
```

`tree_sha256` 按 UTF-8 路径排序，逐项拼接 `path\0size\0sha256\n` 后计算 SHA-256。安装器客户 payload 使用显式平台白名单；仓库级安全扫描仍覆盖完整 installer tree。

## 失败即停止

以下任一条件都会在创建 staging 前停止：

- ref 不是精确的 40 位 commit，或 commit/tree 与计划不一致；
- 缺少产品合同或三个核心 Skill 的 `SKILL.md`；
- 路径穿越、绝对路径、大小写碰撞、Git symlink/submodule；
- Git LFS 指针、任何 tracked ZIP、疑似下载令牌或私钥；
- staging 已存在。

`verify` 还会拒绝清单之外的额外文件、缺失文件、链接和非确定性 ZIP。该合同负责组装与完整性，不负责发行签名、激活授权或覆盖已有 Vault；这些行为由上层发行与安装流程控制。

## 示例

```powershell
./scripts/install-candidate.ps1 plan `
  --product-repo <product> --product-ref <40位提交> `
  --skill-repo <skill> --skill-ref <40位提交> `
  --installer-repo <installer> --installer-ref <40位提交> `
  --platform windows --output plan.json

./scripts/install-candidate.ps1 build --plan plan.json --staging candidate
./scripts/install-candidate.ps1 verify --staging candidate
```
