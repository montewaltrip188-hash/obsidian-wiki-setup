# Obsidian LLM Wiki 安装与发行编排

本仓库是安装器与发行组装层，不再手工维护一份独立的 Vault 副本。

安装候选由三个精确 Git commit 共同组成：

- 产品仓库：提供完整 Vault，是 Vault 文件的唯一来源；
- `claudecode-wiki-skills`：提供 Wiki Skill 完整版本树；
- 本安装器仓库：提供激活、组装、验证、部署和回滚入口。

当前 D0 验收基线为产品提交 `4ea70aaf2fd8a13e5eb455263d5214f8dc5bb6eb`，以及 Wiki Skill `v2.0.1` 发布后提交 `b83e321457211c65eb26200ddcb97f45af66c160`。

## 默认交付内容

- 产品仓库精确 commit 对应的 Obsidian Vault；
- 三个核心 Skill：`design-juan-wiki`、`wiki-hybrid-search`、`ocr-and-documents`；
- `ima-skill` 仅在显式选择时安装；
- Windows 或 macOS 对应平台的安装入口；
- 逐文件 SHA-256、来源 commit、tree 和候选 ID 清单。

安装器不再额外夹带产品仓库中不存在的“100+ 模板”“3 篇使用指南”或旧 `.claude` Skills。

## 发行候选构建

维护者使用 `plan → build → verify` 三个公共接缝：

```powershell
./scripts/install-candidate.ps1 plan `
  --product-repo <产品仓库> --product-ref <40位commit> `
  --skill-repo <Skill仓库> --skill-ref <40位commit> `
  --installer-repo <安装器仓库> --installer-ref <40位commit> `
  --platform windows --output plan.json

./scripts/install-candidate.ps1 build --plan plan.json --staging candidate
./scripts/install-candidate.ps1 verify --staging candidate
```

相同输入必须生成相同的 `candidate.zip` 和 `vault.zip`。Builder 只读取 Git object，不读取源仓库的 dirty 或 untracked 文件；旧 `vault.zip`、tracked ZIP、私钥、下载令牌、LFS 指针和危险路径都会使构建失败。

## 客户安装边界

解压 `candidate.zip` 后，平台入口、`vault.zip`、部署清单、Skill 包和安装工具都位于候选根目录：

```text
candidate/
├── setup-win.ps1 / setup-mac.sh
├── vault.zip
├── deploy-manifest.json
├── extract-vault.py
├── activation-public-key.xml
├── tools/manage_wiki_skills.py
└── skills/claudecode-wiki-skills/
```

Vault 部署流程固定为：

```text
验证 manifest 与归档
→ 安全检查 ZIP 路径
→ 同级 staging 解压
→ 验证解压树摘要
→ 必要时保留原 Vault backup
→ 原子切换
→ 独立验收
```

已有 Vault 默认拒绝覆盖。只有用户显式同意时才先生成同级备份；成功后备份仍保留，清理必须使用绑定回执的独立命令。

产品仓库提交对应的文件树始终是 Vault 模板的唯一交付源。安装入口写入 `.obsidian/plugins/claudian/data.json` 属于部署完成后的受控运行时配置，不会反向成为模板来源：部署器只允许这一条白名单路径变化，并在确认其他文件仍与产品树一致后更新绑定回执；验收失败会保留升级备份并停止。

## Skill 安装位置

Wiki Skill 使用用户级版本化安装：

```text
~/.agents/packages/claudecode-wiki-skills/versions/<version>/
~/.agents/skills/<skill>
~/.claude/skills/<skill>
```

Claude Code 与 Codex 指向同一份物理版本树。安装器支持 `plan / install / verify / rollback / uninstall / undo`，不会替换整个 Skill 根目录。`install` 会在事务锁内确定真实前后状态并返回 `undo_receipt`；setup 在 Skill 验收或后续 Vault 部署失败时，只能携带该回执执行 `undo`。回执对应的 after-state、包指纹或入口指纹发生漂移时会停止，不会依据锁外旧 plan 删除其他事务的结果。

## 激活与下载安全

- 客户端仅接受 `WIKI2.<payload>.<signature>` RSA 公钥签名激活码；
- 客户包只含公钥，私钥和签发工具不得进入本仓库或候选；
- 下载脚本不包含 Gitee token、API Key 或其他客户端凭据；
- 激活码与 API Key 的交互输入均隐藏；
- 公开离线激活可以防止从客户包反推出合法激活码，但不能阻止有能力的用户修改本地脚本绕过验证。

## 当前发布门禁

D0 本地候选尚未达到公开发布条件：Wiki Skill 已能安装和发现，但关键词 Query 仍需要 Python 解释器以及锁定的 `requests / jieba / numpy` 依赖包。安装器不会在后台自动联网安装依赖，当前能力回执会明确返回 `KEYWORD_RUNTIME_UNPROVISIONED`；向量检索继续保持可选。

在确定跨平台离线 Python 运行时方案、发行版本号并完成独立安装验收前，不应把本候选标记为 stable，也不应让历史 v2.1 下载脚本冒充新的跨仓库候选。
