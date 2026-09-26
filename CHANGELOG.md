# 更新日志

本文只记录发布版本的主要变化；真实验收证据见 [`验证记录.md`](验证记录.md)。

## 1.2.0 — 2026-09-26

### 新增

- 一键跨 Mac 部署：从 Releases/latest 下载 `codex-lead-deepseek-1.2.0-macos.zip`（附 SHA256），解压双击 `一键安装.command` 完成安装与接入。
- 随包提供经 SHA256 核验的固定 Python 3.12 与 Node 24（Apple Silicon 与 Intel），无需系统预装 Python/Node/Git/gh/Homebrew。
- Harness 不再随 zip 固定：优先复用已有可用 Harness，其次本地接入 PATH 中的官方 `dsh`，最后从官方 npm 安装 `@deepseek-ai/dsh@latest` 并记录实际版本。
- TTY 隐藏输入 DeepSeek API key：回车跳过、不覆盖既有 credentials，key 不进入聊天/命令行/环境变量/日志。
- Skill 每任务首次 activate 时打开监控并关联当前会话，后续步骤不重复。

### 变更

- README 以一键安装为主路径，Git clone 调整为高级可选；新 clone 同样运行 `一键安装.command` 下载依赖。
- 示例命令由系统 `python3` 改为随包 `./bin/python`，适配无系统 Python 的机器。
- 监控刷新改为用户手动触发（顶栏刷新按钮），不再定时刷新详情。
- 更新文档中原“固定 Harness 版本、需先手动装依赖”的说明，改为当前的按需接入策略。

### 修复

- 适配现代与旧版 headless，并完善工具结果的处理与导出。

### 说明

- Codex 应用安装/登录仍由用户按官方页面完成；API 有效性不做自动验证，`doctor` 仅做结构检查。
- 未签名/未公证安装包被 macOS 拦截时，按系统设置隐私与安全性的“仍要打开”处理，不提供全局关闭 Gatekeeper/xattr 方案。
- 正式 zip 由 Codex 构建并验收；本版尚未在第二台实体 Mac 或 Intel 上实测。

## 1.1.0 — 2026-09-25

### 新增

- 本机只读监控：任务筛选、状态、审核记录、原始指令、公开会话、工具参数与结果、子代理树、文件改动和 token 展示。
- 可按已关联的 Codex 会话查看公开消息与调用。
- 自动 5 秒刷新，不调用模型。
- 支持后台 `bin/monitor` 启动，或双击 `打开监控.command`。
- 历史恢复：可通过本机压缩 archive 离线导入旧任务的公开事件。

### 修复

- 修复同一 turn 累计快照重复 fallback 问题，并新增 6 个回归用例。

### 说明

- 关联的 Codex 用量为整会话累计，不是单个任务费用或省费率；缺失与截断数据会显式提示。
- 页面仅展示公开消息，不输出隐藏思考与 system/developer 指令；运行日志留在 `.state`，不推送 GitHub。
