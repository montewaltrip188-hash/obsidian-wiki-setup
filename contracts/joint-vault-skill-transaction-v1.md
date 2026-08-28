# Vault + Wiki Skills 联合事务合同 v1

U3 把已经独立可靠的 Vault 文件事务、Skill 安装回执和派生索引恢复编排为一个组合事务。联合事务仍不改变路径所有权、旧客户纳管规则或版本语义。

## 公共命令

Windows 使用 `scripts/joint-update.ps1`，macOS/Linux 使用 `scripts/joint-update.sh`：

```text
joint-update plan     # 严格只读：组合 Vault plan、Skill plan、index-status 与严格 Query 规格
joint-update apply    # 只接受精确 plan_id、逐文件 change_sha256、Skill 与索引权限
joint-update verify   # 只读复核 Vault 回执、Skill 状态、索引签名和严格 Query
joint-update rollback # 回滚 Vault、恢复派生索引、按 undo_receipt 恢复旧 Skill
```

联合 `plan` 必须保持 Vault、外部缓存和 Skill Home 零写入。`apply` 的顺序固定为：安装目标 Skill 并取得 `undo_receipt`，应用已审批 Vault diff，执行 `index-status`，仅在获批时重建失效索引，最后运行严格 Query 并读取命中页全文确认摘要。

## 索引新鲜度

目标 Skill 的 `index-status` 是只读公共接口。目标 fingerprint 至少绑定 `index_schema_version`、`chunker_signature`、`embedder_signature` 和 `preprocessing_signature`。正文未变化但任一签名变化时，不得复用旧向量或关键词数据库。

## 失败恢复

Skill 安装、Vault apply、索引或严格 Query 任一失败时，编排器依次：

1. 按 Vault apply 回执恢复产品文件与产品状态；
2. 从 Vault 外部事务目录恢复 `.state.db`、WAL/SHM 与 FAISS index/map 的写前状态；
3. 按安装返回的 `undo_receipt` 恢复旧 Skill 激活版本；
4. 写出带 SHA-256 seal 的 `joint-failure-receipt.json`。

显式 rollback 会在任何写入前预检 Vault apply 回执、Skill `undo-check`、当前索引 after 摘要和全部索引备份。任一组件发生事务后漂移时停止，不覆盖客户的新变化。
