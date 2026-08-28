# 客户 Vault 事务更新合同 v1

U2 延续 U1 的路径所有权和旧客户纳管边界，只为 Fresh Install 建立受管状态，并为已有受管 Vault 提供审批绑定的事务更新。缺少产品状态的旧客户仍返回 `legacy_adoption_required`，不会自动纳管。

## 外部事务资产

以下资产必须位于 Vault 外部缓存根目录：

- `baselines/<product-tree>.zip`：确定性 Base 快照，摘要写入产品状态；
- `locks/<vault-cache-key>.lock`：同一 Vault 的排他更新锁；
- `backups/<vault-cache-key>/<transaction-id>/`：写前文件、原产品状态和带摘要回执；
- `backups/<vault-cache-key>/<transaction-id>/rollback-attempts/<rollback-id>/`：回滚前目标态保护，用于回滚失败后的自动恢复。

`vault_id` 的既有非空字符串语义保持不变；当它不是安全文件名时，安装器只在外部路径层使用 `sha256-<digest>` 作为 `vault-cache-key`，不会改写客户产品状态。

缓存位于 Vault 内、Base 缺失、摘要不符、ZIP 路径越界或大小写碰撞时必须停止。缓存不是客户内容的新来源；产品仓库树仍是唯一目标交付源。

## 公共命令

```powershell
./scripts/vault-update.ps1 fresh-install `
  --vault <新Vault> --product-root <已部署产品树> --cache-root <外部缓存> `
  --path-policy <update-policy.json> --product-contract <runtime-contract.json> `
  --skill-compatibility <COMPATIBILITY.json> --bundle-manifest <bundle-manifest.json>

./scripts/vault-update.ps1 plan `
  --vault <受管Vault> --cache-root <外部缓存> --target-root <目标产品树> `
  --path-policy <update-policy.json> --product-contract <runtime-contract.json> `
  --skill-compatibility <COMPATIBILITY.json> --bundle-manifest <bundle-manifest.json>

./scripts/vault-update.ps1 apply `
  --vault <受管Vault> --cache-root <外部缓存> --target-root <目标产品树> `
  --path-policy <update-policy.json> --product-contract <runtime-contract.json> `
  --skill-compatibility <COMPATIBILITY.json> --bundle-manifest <bundle-manifest.json> `
  --plan <update-plan.json> --approval <update-approval.json>

./scripts/vault-update.ps1 verify --vault <受管Vault> --receipt <事务回执>
./scripts/vault-update.ps1 rollback --vault <受管Vault> --cache-root <外部缓存> --receipt <apply回执>
```

macOS/Linux 使用 `./scripts/vault-update.sh`，参数相同。

## 审批绑定

计划同时绑定 bundle candidate、产品状态摘要、路径策略摘要以及每一项变更的 `change_sha256`。审批必须逐项、同序、完整匹配全部 `requires_approval` 变更；不接受部分审批或额外路径。`delete_candidate` 必须额外设置 `allow_deletes: true`。任何本地、目标、策略、状态或计划变化都会使原审批失效。

## 写入与恢复

`apply` 只处理 `product_merge` 与 `product_replace` 路径。它在排他锁内再次计算计划、保存写前快照、逐文件原子替换、核验结果，最后更新产品状态并写出带内容摘要的 apply 回执。客户目录和未纳管路径不进入计划或备份。

`rollback` 只接受位于对应事务备份目录中的完整 apply 回执。执行前必须验证当前 Vault 仍等于 apply 后状态，并一次性校验全部写前备份；随后为当前目标态建立恢复快照。若回滚中途失败，必须自动恢复到回滚前目标态。成功时生成独立 rollback 回执，原 apply 回执保持不变。

## 主要阻断码

| 场景 | 阻断码 |
|---|---|
| Fresh Install 与产品树不一致 | `FRESH_INSTALL_PRODUCT_DRIFT` |
| 缓存位于 Vault 内 | `CACHE_ROOT_INSIDE_VAULT` |
| 计划输入漂移 | `PLAN_STALE` / `PLAN_STALE_DURING_APPLY` |
| 审批不完整或不匹配 | `APPROVAL_MISMATCH` |
| 删除未单独授权 | `DELETE_NOT_APPROVED` |
| 三方冲突 | `PLAN_HAS_CONFLICTS` |
| 同一 Vault 已有事务 | `UPDATE_LOCK_BUSY` |
| Base 缺失或损坏 | `BASELINE_CACHE_MISSING` / `BASELINE_CACHE_DIGEST_MISMATCH` |
| apply 失败且已恢复 | `APPLY_FAILED_ROLLED_BACK:*` |
| 回执被修改 | `TRANSACTION_RECEIPT_TAMPERED` |
| 回滚目标已有新变化 | `ROLLBACK_TARGET_DRIFT` |
| 备份缺失或损坏 | `BACKUP_MISSING` / `BACKUP_DIGEST_MISMATCH` |
| rollback 失败且已恢复 | `ROLLBACK_FAILED_RESTORED:*` |
| 管理路径或其父级是链接 | `MANAGED_PATH_LINK_UNSUPPORTED` |
| 写前发生底层文件系统错误 | `APPLY_FAILED_BEFORE_WRITE` / `IO_ERROR` |

故障矩阵同时覆盖目标树漂移、错误审批、未授权删除、三方冲突、锁竞争、首文件写入后失败、产品状态写入后失败、回执篡改、回滚前客户漂移、旧回执重放、备份损坏以及回滚中途失败。所有自动恢复都会先确认当前内容仍是事务的 before/after 已知状态；若检测到未知并发修改，停止自动覆盖并升级为人工恢复，而不是吞掉客户的新变化。

测试故障注入变量只有同时设置 `JUNYONG_AI_TEST_MODE=1` 时才生效，不属于公开客户接口。
