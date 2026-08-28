# 客户 Vault 只读更新合同 v1

U1 只建立升级判断和人工审批前的建议层，不包含 `apply` 命令，也不会修改客户 Vault。

## 三层版本合同

- 产品仓库的 `schema/runtime-contract.json` 声明产品 Schema 版本、所需 Skill 版本范围和经过验证的 Skill commit。
- Skill 仓库的 `COMPATIBILITY.json` 声明当前运行时版本及其支持的产品 Schema 范围。
- 安装器的 `release/bundle-release.json` 声明 bundle 版本与发布状态；Builder 将它与三仓精确来源合成为候选根目录的 `bundle-manifest.json`。

三份合同必须互相闭合。`release_state` 不是 `stable`，或 `bundle_version` 尚未分配时，`check` 和 `plan` 必须以 `BUNDLE_VERSION_UNASSIGNED` 停止，不能把本地候选误当作客户升级包。

## 路径所有权

产品仓库的 `schema/update-policy.json` 使用“首条匹配生效”的显式规则：

- `product_merge`：产品治理文件，只生成三方对比建议；
- `product_replace`：产品程序资产，只生成替换建议；
- `customer_config`：客户配置，保留；
- `customer_business`：客户知识、日志、输出等业务内容，保留且不扫描；
- `derived_rebuildable`：索引和缓存不从产品包覆盖，只能按失效指纹决定是否重建；
- `local_private`：密钥、归档和临时状态，完全排除且不扫描；
- `seed_once`：仅首次安装提供，升级时保留客户现状；
- `migration_only`：路径或字段语义变化只允许由后续版本化迁移包提出；
- `unmanaged`：未纳管路径默认保留。

`plan` 只枚举 base/target 产品树，以及策略中逐项列出的产品管理路径；它不会遍历 `raw/`、`wiki/`、`inbox/`、`log/`、`output/` 等客户内容目录。

## 客户产品状态

受管 Vault 将来由 `.juanyong-ai/product-state.json` 记录已安装产品基线、bundle、Skill 和迁移历史。U1 不创建该文件：

- 文件不存在：返回 `legacy_adoption_required`，等待旧客户纳管方案和人工批准；
- 文件存在但无效：返回 `PRODUCT_STATE_INVALID`；
- 文件有效：`status` 返回 `managed`，`check` 才能比较目标版本。

这意味着旧客户不会因为运行只读检查而被自动纳管。

## 公共命令

```powershell
./scripts/vault-update.ps1 status --vault <客户Vault>

./scripts/vault-update.ps1 check `
  --vault <客户Vault> `
  --product-contract <runtime-contract.json> `
  --skill-compatibility <COMPATIBILITY.json> `
  --bundle-manifest <bundle-manifest.json>

./scripts/vault-update.ps1 plan `
  --vault <客户Vault> --base-root <已安装产品基线> --target-root <目标产品树> `
  --path-policy <update-policy.json> `
  --product-contract <runtime-contract.json> `
  --skill-compatibility <COMPATIBILITY.json> `
  --bundle-manifest <bundle-manifest.json>
```

macOS/Linux 使用 `./scripts/vault-update.sh`，参数相同。成功回执写到标准输出，阻断回执写到标准错误并返回退出码 2。

## 三方决策矩阵

| base | local | target | 决策 |
|---|---|---|---|
| A | A | B | `update_candidate` |
| A | B | A | `preserve_local` |
| A | B | B | `no_op_converged` |
| A | B | C | `conflict` |
| 不存在 | 不存在 | B | `add_candidate` |
| 不存在 | B | 不存在 | `preserve_local` |
| A | A | 不存在 | `delete_candidate` |
| A | B | 不存在 | `conflict_delete` |

新增、更新、删除和冲突候选均标记 `requires_approval: true`。U1 只输出绑定输入内容的 `plan_id`、逐文件哈希和可用的文本 diff；没有任何执行入口。
