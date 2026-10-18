# codex-lead-deepseek

当前发布版 **20261016**（2026-10-19 发布，包含 10 月 16 日功能）。Codex 负责需求、方案、拆解、审核和验收，DeepSeek Harness 执行目标明确、可核验的工作。

默认复用 **DSH.app 正在使用的同一个后台**，任务创建为 DSH 原生会话。模型、思考强度、Agent 预设、插件及子 agent 策略沿用桌面配置；日常通过本机 HTTP 下发并读取结果，不需要截图或 Computer Use。未启动时自动打开正常 DSH.app，已有服务直接复用。

## 安装

支持 macOS 13.5+，Apple Silicon 和 Intel。

1. 先安装并登录 Codex 与 **DSH.app**。本工具不包含或自动安装这两个客户端。
2. 从 [最新版 Release](https://github.com/Tsou92/codex-lead-deepseek/releases/latest) 下载 `codex-lead-deepseek-20261016-macos.zip` 和同名 `.sha256` 文件；在下载目录运行 `shasum -a 256 -c codex-lead-deepseek-20261016-macos.zip.sha256` 核对。
3. 解压后双击 `一键安装.command`。脚本核验并准备随包 Python 3.12 / Node 24，复制到 `~/Applications/Codex-Lead-DeepSeek`，接入全局 CLI、Skill 和规则，再打开认证监控。无需预装 Python、Node、Git、gh 或 Homebrew。
4. 桌面模式不安装另一套 Harness，不重新读取、创建或修改桌面密钥、模型及插件。DSH 未运行时会显示待启动，首次执行任务时自动打开。
5. 若 macOS 拦截未签名安装包，核对来源后在系统设置“隐私与安全性”中选择“仍要打开”。

已有 `~/.local/bin/leadseek` 指向当前源码时，向导原位复用，不另建副本。安装包附带两种架构的官方 Python/Node 归档，不包含 DSH.app、账号凭据或私人配置。首次模型调用仍需要网络。

## 在 Codex 中使用

新开聊天，输入 `$deepseek-delegate` 并说明需求。仅在当前明确启用后使用委派流程。Codex 自动构造任务、读取精简结果、审核差异、发返工指令并应用合格改动。

每个任务首次 `leadseek activate` 打开认证监控并关联当前 Codex 会话，同一任务后续步骤不重复。监控页面由用户手动刷新，不定时重载详情。若直接打开 `127.0.0.1:8765` 遇到认证问题，重新运行 activate，不要手工转发认证 URL。

DSH 自主选择当前已启用且预设可用的插件、工作流、子 agent 或智能体团队。任务默认省略 `subagents` 或设为 null；简单任务按需直接完成。Ponytail 七步最小实现规则在桌面和旧兼容模式共用；Codex 侧采用精简上下文、goal 只写目标和按差异验收等规则。

## 常用命令

```sh
~/.local/bin/leadseek doctor
~/.local/bin/leadseek list
~/.local/bin/leadseek status 任务编号
~/.local/bin/leadseek result 任务编号
~/.local/bin/leadseek audit --workspace /项目路径
```

全局入口在 PATH 中时可直接输入 `leadseek`；源码目录内可使用 `./bin/leadseek`。doctor 读取本机连接与链接状态，不调用模型，也不验证 DeepSeek API 账单凭据。

## 源码安装与更新

已有 Python 3.9+ 和已登录的 DSH.app 时，可以克隆本仓库后运行 `./bin/setup`。未准备 Python 的机器请使用完整 Release 安装包。

```sh
git clone https://github.com/Tsou92/codex-lead-deepseek.git
cd codex-lead-deepseek
./bin/setup
./bin/test
```

更新可下载新版安装包重新运行向导；源码安装可在工作区干净时 `git pull --ff-only` 后重新 setup。凭据与运行日志只留本机，不随 Git 同步。移动目录前先在原位置运行 `./bin/install-codex --uninstall`，再在新位置 setup；不要覆盖其他工具占用的入口。

## 运行与隐私边界

- 必要文件先复制到暂存目录，DSH 第一工具切换到快照再执行，完成后检查修改范围；Codex 审核、测试，再通过 apply 应用。原项目变化时拒绝直接覆盖。这不是操作系统级隔离。
- 本机监控展示公开消息、工具记录、父子 agent、改动、审核与已报告用量，不展示隐藏思考或 system/developer 指令。认证信息仅在内存，完整公开日志留在 `.state/`。
- 用量是已采集事件，缺失显示未知；`parent_usage_only` 不含全部子代理，也不等于账单省费测量。
- 旧 `execution_backend=headless` 仅作为明确选择的兼容模式；其中 low、工具预算和独立运行时配置不控制桌面 DSH。桌面失败不会静默回退。
- 代码和安装在本机验证；自动冷启动分支为模拟覆盖，尚未在第二台实体 Mac 或 Intel 上实测。

## 文档

- [桌面同进程模式](docs/desktop-mode.md)
- [使用说明](使用说明.md)
- [验证记录](验证记录.md)
- [Skill](skill/deepseek-delegate/SKILL.md)
- [历史记录恢复](docs/monitor-history.md)
