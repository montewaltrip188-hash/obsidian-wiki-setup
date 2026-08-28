# Obsidian LLM Wiki 一键安装包

基于 [Karpathy LLM Wiki 方法论](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)，用 AI 帮你构建和维护个人知识库。

## 包含内容

- Obsidian 知识库模板（wiki 架构 + CLAUDE.md Schema）
- 39 个精选插件（含 Claudian、Dataview、Excalidraw 等）
- 100+ 模板
- 3 篇使用指南

## 安装前准备

1. **下载并安装 [Obsidian](https://obsidian.md/download)**（如果还没装）
2. **购买 DeepSeek API**：前往 https://platform.deepseek.com ，充值 10 元（约可用 10 天）
3. **从本仓库的公开 Release 下载压缩包**，解压到任意目录。下载脚本不需要也不会携带访问令牌

## 激活与密钥安全

- 安装器只接受 `WIKI2.<payload>.<signature>` 格式的 RSA 签名激活码，旧版共享秘密格式不再兼容。
- Windows 和 macOS 均隐藏激活码输入，并校验签名、产品、版本、有效期和撤销 ID。
- 客户端包只包含 `activation-public-key.xml` 公钥和撤销清单，不包含签发私钥或批量签发产物。
- 签发私钥必须保存在仓库目录之外；一旦私钥泄露，应轮换密钥并重新发布客户端公钥。

## 一键安装

### Windows

1. 解压下载的 zip 文件
2. 右键 `setup-win.ps1` → **使用 PowerShell 运行**
   - 如果提示执行策略限制，在 PowerShell 中运行：
     ```powershell
     powershell -ExecutionPolicy Bypass -File setup-win.ps1
     ```
3. 按提示输入 DeepSeek API Key，等待安装完成

### Mac

1. 解压下载的 zip 文件
2. 打开终端，进入解压目录，执行：
   ```bash
   chmod +x setup-mac.sh && ./setup-mac.sh
   ```
3. 按提示输入 DeepSeek API Key，等待安装完成

## 安装后

1. 打开 Obsidian → **打开文件夹作为库** → 选择安装时指定的目录（默认 `文档/ObsidianVault`）
2. 弹出「信任此库的插件」时点击 **信任并启用**
3. 阅读库中「使用指南」文件夹的三篇文档：
   - LLM Wiki 知识库使用指南
   - Obsidian 入门指南
   - Obsidian 插件速查手册

## 知识库架构

```
vault/
├── raw/          # 原始素材（剪藏的文章、笔记）
├── wiki/         # AI 生成和维护的知识库
│   ├── sources/      # 素材摘要
│   ├── entities/     # 实体（人物、工具）
│   ├── concepts/     # 概念（理论、方法论）
│   └── comparisons/  # 对比分析
├── output/       # AI 问答产物
├── 使用指南/      # 入门文档
└── CLAUDE.md     # AI 操作规范
```

## 常见问题

**Q: API Key 在哪获取？**
A: https://platform.deepseek.com → 注册 → API Keys → 创建

**Q: 安装脚本报错怎么办？**
A: 截图错误信息发给我，远程帮你解决

**Q: 可以用其他 AI 模型吗？**
A: 可以，修改 `~/.claude/settings.json` 中的模型配置即可
