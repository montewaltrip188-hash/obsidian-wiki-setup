# 三仓发布编排合同 v1

D2 的公共入口位于 `release/orchestrator.py`：

```text
orchestrator.py plan   --product-repo <repo> --product-ref <40位commit> --skill-repo <repo> --skill-ref <40位commit> --installer-repo <repo> --installer-ref <40位commit> --workspace <新目录>
orchestrator.py status --workspace <计划目录>
```

`plan` 只从三个来源仓库读取精确 Git 对象，在全新的外部 workspace 中分别建立 detached clone；来源工作树的未提交内容不会进入候选。它用冻结的安装器 Builder 为 Windows 与 macOS 各构建两次候选，执行完整性验证并要求 candidate ZIP 与 Vault ZIP 字节可复现，然后生成绑定三仓 commit/tree、候选 ID、资产 SHA-256 和唯一 `plan_id` 的封口回执。

D2 不 commit、tag、push，不创建 Release，不上传或覆盖安装资产，也不更新 stable 指针。bundle 仍为 `unreleased_candidate` 或版本未分配时，`next_action` 必须是 `version_approval_required`；这只是人工门禁状态，不能由 plan 自动改变版本语义。

`status` 严格只读，复核回执 seal、三个 detached clone 的 HEAD/tree/clean 状态和两平台四套候选资产。任何文件缺失、字节漂移、clone 漂移或回执篡改都返回 blocked，不重写状态来掩盖故障。
