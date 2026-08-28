# 旧客户 Vault 纳管合同 v1

没有 `.juanyong-ai/product-state.json` 的旧 Vault 不得自动假设 Base，也不得直接做二方覆盖。维护者先准备只含“已知、完整、稳定发行”产品树和 bundle manifest 的本地 catalog，再运行：

```text
vault-update legacy-plan  --vault <旧Vault> --path-policy <政策> --catalog <历史目录>
vault-update legacy-adopt --vault <旧Vault> --cache-root <外部缓存> --path-policy <政策> --catalog <历史目录> --plan <计划> --approval <人工审批>
```

`legacy-plan` 严格只读，只枚举路径政策声明的产品受管路径；它不扫描客户内容目录，也不读取 `raw/wiki/inbox/log/output` 等客户文件正文。每个历史基线都列出匹配数、缺失、修改和本地新增的不确定项，并在 `timing_ms` 分别报告 catalog、受管扫描、比较和总耗时。只有恰好一个基线完全匹配时才给出推荐，但推荐仍不等于授权。

`legacy-adopt` 必须重新计算并逐字节匹配计划，只接受绑定 `plan_id`、所选 `baseline_id` 和 Base ZIP SHA-256 的人工审批。它不改产品规则文件和客户内容，只在 Vault 内写入产品状态，并在 Vault 外部缓存官方 Base 与封口回执。存在多个精确候选或本地修改时可以列出候选，但选择哪一个 Base 属于人工门禁。

纳管完成后，普通 `plan / apply / verify / rollback` 才能执行三方升级。任何计划漂移、错误审批、非稳定历史 bundle、未知受管链接或缓存碰撞都在写 Vault 前停止。
