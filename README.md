# Obsidian LLM Wiki 安装与发行编排

本仓库是安装器与发行组装层，不再手工维护一份独立的 Vault 副本。

安装候选由三个精确 Git commit 共同组成：

- 产品仓库：提供完整 Vault，是 Vault 文件的唯一来源；
- `claudecode-wiki-skills`：提供 Wiki Skill 完整版本树；
- 本安装器仓库：提供激活、组装、验证、部署和回滚入口。

当前 D0 验收基线为产品提交 `4ea70aaf2fd8a13e5eb455263d5214f8dc5bb6eb`，以及 Wiki Skill `v2.0.1` 发布后提交 `b83e321457211c65eb26200ddcb97f45af66c160`。

当前 U1-R 本地候选使用产品 Schema `1.0.0` 和 Wiki Skill `2.1.0`；bundle 仍未分配版本，未 tag、push 或发布。

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

## U1：已有 Vault 的只读升级检查

已有 Vault 不再以“下载新仓库后整体搬家”为升级模型。U1 随客户候选交付三层版本合同、路径所有权策略和严格只读的 `status / check / plan`：

```powershell
./scripts/vault-update.ps1 status --vault <客户Vault>
./scripts/vault-update.ps1 check --vault <客户Vault> `
  --product-contract <runtime-contract.json> `
  --skill-compatibility <COMPATIBILITY.json> `
  --bundle-manifest <bundle-manifest.json>
./scripts/vault-update.ps1 plan --vault <客户Vault> `
  --base-root <已安装基线> --target-root <目标产品树> `
  --path-policy <update-policy.json> `
  --product-contract <runtime-contract.json> `
  --skill-compatibility <COMPATIBILITY.json> `
  --bundle-manifest <bundle-manifest.json>
```

`plan` 只读取产品管理路径，输出三方差异、逐文件哈希、审批标记和稳定 `plan_id`；客户知识、日志、输出、索引、密钥和本地状态均不扫描、不修改。旧客户缺少 `.juanyong-ai/product-state.json` 时只返回 `legacy_adoption_required`，不会自动写入状态或擅自纳管。

当前仓库内的 `release/bundle-release.json` 故意保持 `unreleased_candidate` 且不分配版本。Builder 会把它与三仓精确来源合成为客户候选根目录的 `bundle-manifest.json`，避免让仓库内文件自引用尚未产生的 commit 或 candidate ID。版本获批并把发布合同切换为 `stable` 前，`check`、`plan` 和 `apply` 都会以 `BUNDLE_VERSION_UNASSIGNED` 停止。U1 的只读边界见 `contracts/read-only-vault-update-v1.md`。

## U2：审批绑定的事务更新

U2 在 U1 的只读判断之后增加 `fresh-install / apply / verify / rollback`。Fresh Install 只为与产品树完全一致的新 Vault 建立 `.juanyong-ai/product-state.json`，Base ZIP、事务锁、备份和回执全部位于 Vault 外部缓存。`apply` 会重新计算并锁定 `plan_id`、路径策略、产品状态和逐文件 `change_sha256`，只接受与全部可执行变更精确一致的审批；删除还需要单独的 `allow_deletes`。冲突、过期计划、错误审批、缓存损坏或并发锁都会在写 Vault 前停止。

事务写入使用同目录临时文件加原子替换。任一步骤失败会按写前备份恢复；回滚本身也先验证回执与完整备份、保护当前目标态，再执行事务恢复。客户内容目录不扫描、不备份、不修改。完整命令、回执和故障语义见 `contracts/vault-update-transaction-v1.md`。

## U3：Vault + Skill 联合事务

U3 用 `scripts/joint-update.ps1`（Windows）或 `scripts/joint-update.sh`（macOS/Linux）把 Skill 切换、已审批的 Vault 产品文件 diff、索引失效判断和严格 Query 验收绑定为同一个联合 `plan_id`。公共命令为 `plan / apply / verify / rollback`；其中 `plan` 只读组合 Vault plan、Skill plan、目标 Skill 的 `index-status` 和预期命中页 SHA-256，不写 Vault、Skill Home、索引或外部缓存。

`apply` 只接受与联合计划完全一致的审批，并分别要求 `approve_skill_change` 与 `allow_index_rebuild`。执行顺序固定为：安装目标 Skill 并取得 `undo_receipt`，应用 Vault diff，检查四类索引派生签名，必要且获批时重建索引，最后运行严格 Query 并读取命中页全文核对摘要。失败时先回滚 Vault，再恢复写前索引，最后按回执撤销 Skill，并留下带 seal 的恢复回执；显式 `rollback` 也会先只读预检三个组件的当前状态和备份完整性，拒绝覆盖事务后的客户变化。完整合同见 `contracts/joint-vault-skill-transaction-v1.md`。

## U4：旧客户纳管

缺少产品状态的旧 Vault 始终返回 `legacy_adoption_required`。维护者用已知历史产品树 catalog 运行严格只读 `legacy-plan`：它只比较路径政策声明的产品受管路径，不扫描客户内容目录；只有恰好一个历史 Base 完全匹配时才给出推荐。随后仍须提供绑定 `plan_id`、Base ID 和 Base SHA-256 的人工审批，`legacy-adopt` 才会在原 Vault 写入产品状态，并把官方 Base 与回执写到 Vault 外部缓存。多个精确候选、本地修改或缺失项都会明示为不确定项，不会自动猜测。完整合同见 `contracts/legacy-adoption-v1.md`。

## D2：只读三仓发布计划

维护者使用 `release/orchestrator.py plan` 冻结产品、Skill 与安装器的三个精确 40 位 commit。编排器只读来源仓库，在新建的外部 workspace 中建立 detached clone，为 Windows 与 macOS 各构建两次并验证字节可复现候选，最后写出带 seal 的 `release-plan.json`。`status` 只读检查 clone 与候选资产是否漂移。该阶段不 commit、tag、push、创建 Release、覆盖安装资产或更新 stable 指针；未分配 bundle 版本时只返回 `version_approval_required`。完整合同见 `contracts/release-orchestrator-v1.md`。

## Skill 安装位置

Wiki Skill 使用用户级版本化安装：

```text
~/.agents/packages/claudecode-wiki-skills/versions/<version>/
~/.agents/skills/<skill>
~/.claude/skills/<skill>
```

Claude Code 与 Codex 指向同一份物理版本树。安装器支持 `plan / install / verify / rollback / uninstall / undo / undo-check`，不会替换整个 Skill 根目录。`install` 会在事务锁内确定真实前后状态，写入 state schema v2 的唯一 generation 并返回 `undo_receipt`；合法的旧 schema v1（无 generation，或带前序实现生成的合法 32 位 generation）仍可只读 plan/verify，首个 install 写事务会在验收原所有权与指纹后迁移到 v2，迁移本身也可凭回执撤销。setup 在 Skill 验收或后续 Vault 部署失败时，只能携带该回执执行 `undo`。`undo-check` 只读验证回执和当前 after-state，供联合回滚在任何写入前完成预检。`undo` 直接重算回执绑定的 after-state、generation、包指纹、入口指纹与运行时别名，不依赖可能持续失败的公共 `verify`；任一真实漂移或旧回执重放都会停止，也不会依据锁外旧 plan 删除其他事务的结果。锁释放审计写入失败只作为结果警告，不会把已经提交且已有回执的事务伪装成失败；只有 follower 已取得内核独占锁后，才会把无法解析的历史审计视为损坏的 stale audit 并覆盖，活锁不会按超时强拆。

## 激活与下载安全

- 客户端仅接受 `WIKI2.<payload>.<signature>` RSA 公钥签名激活码；
- 客户包只含公钥，私钥和签发工具不得进入本仓库或候选；
- 下载脚本不包含 Gitee token、API Key 或其他客户端凭据；
- 激活码与 API Key 的交互输入均隐藏；
- 公开离线激活可以防止从客户包反推出合法激活码，但不能阻止有能力的用户修改本地脚本绕过验证。

## 当前发布门禁

D0 本地候选尚未达到公开发布条件：Wiki Skill 已能安装和发现，但关键词 Query 仍需要 Python 解释器以及锁定的 `requests / jieba / numpy` 依赖包。安装器不会在后台自动联网安装依赖，当前能力回执会明确返回 `KEYWORD_RUNTIME_UNPROVISIONED`；向量检索继续保持可选。

在确定跨平台离线 Python 运行时方案、发行版本号并完成独立安装验收前，不应把本候选标记为 stable，也不应让历史 v2.1 下载脚本冒充新的跨仓库候选。
