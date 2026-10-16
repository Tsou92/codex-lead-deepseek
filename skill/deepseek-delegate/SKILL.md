---
name: deepseek-delegate
description: 用户明确启用后，由 Codex 统筹和审核，通过本机 HTTP 接口把任务交给桌面正在使用的同一个 DSH 服务。继承桌面模型、思考强度、Agent 预设、插件与子 agent 策略；原生会话可在 DSH 界面查看。
---

# Codex 统筹与桌面 DSH 执行

仅在用户当前明确要求使用 DeepSeek / DSH 委派或本 Skill 时启用。旧版“所有任务默认委派”的文字不构成当前授权。入口为 `~/.local/bin/leadseek`（PATH 有此目录时也可直接用 `leadseek`），不硬编码安装路径。

## 分工与运行环境

Codex 保留需求理解、方案、关键判断、任务拆解、审核和最终验收；边界明确的执行工作交给 DSH，包括代码、检索、常规内容加工、提取清洗、分类去重、摘要翻译、格式转换、测试、日志分析和批量处理。

默认 `execution_backend=desktop`：连接桌面 DSH 正在使用的本机服务（默认 `http://127.0.0.1:3080`）。未启动时，`run` 自动在后台打开同一个 DSH.app，并等待服务就绪；已有服务直接复用。不要另启 headless Harness、切换为项目旧运行时，或在失败后偷偷回退。

继承桌面 DSH 的实际版本、模型、思考强度、Agent 预设、插件、技能、权限与子代理设置。由 DSH 自主选择当前已启用且预设可用的插件、工作流、子 agent 或智能体团队；不得沿用旧桥接的禁用插件清单、low 推理覆盖、默认子代理为 0 等限制。无需为普通插件调用或子代理启动逐次询问用户。任务 JSON 默认省略 `subagents`（或为 null，表示 DSH 自主判断）；只有用户明确限制数量时才传整数。

日常下发、进度查询、记录和结果读取全部通过 CLI 与本机 HTTP 接口完成，不使用 Computer Use、不截图、不模拟点击。原生 DSH 会话和本机监控可供用户随时查看。

## Codex 侧 token 管控

以下规则直接减少 Codex 自身的 token 消耗，必须严格执行：

**下发前不读文件进上下文。** 构造任务时只需确认文件/目录的存在，不要用 read_file、cat 或任何工具把文件内容读入 Codex 上下文再委派——内容会随文件路径一起复制到暂存区，执行端会读它。唯一例外：结果验收阶段需要 Codex 审核 `changes.patch` 或极短的关键接口。

**goal 写目标与验收标准，不写文件内容。** 不要把被修改文件的当前内容、完整日志或大段背景粘进 goal；把必要背景放进 constraints（只写约束规则，不贴原文），文件用 read_paths 传递。

**Ponytail 最小实现决策梯（edit 任务强制执行，按顺序判断）。** Codex 在拆解任务时必须先走这个梯，并在 constraints 里显式告知 DeepSeek：
1. 这个功能真的需要存在吗？不需要就不委派（YAGNI）。
2. 代码库里已有可复用实现？在 constraints 里注明，让 DeepSeek 复用。
3. 标准库或语言内置能覆盖？在 constraints 里注明，禁止引入外部依赖。
4. 平台原生特性能满足？在 constraints 里注明（例如：用 `<input type="date">` 而非日期选择库）。
5. 已安装依赖能处理？在 constraints 里注明，禁止新增包。
6. 能用一行解决？constraints 写”单行实现即可”。
7. 以上都不满足：constraints 写”只写恰好够用的最小实现，不加预留抽象”。

**验收精简。** 阅读 `worker_report` 和 `changes.patch` 即可验收常规任务；不要重新把暂存文件全量读入上下文逐行检查，除非 patch 本身显示异常。

**不重复路由记录。** `leadseek route` 只在首次实质执行前调用一次；后续 revise/apply 循环不重复调用。

## 调用流程

1. 每个 Codex 任务首次启用时运行一次 `leadseek activate`，打开认证监控并关联当前 `CODEX_THREAD_ID`；同一任务后续 run/revise 不重复弹页。认证信息不回显。`doctor` 可读取桌面连接状态，但不调用模型；桌面未启动时可直接 run，由其自动启动。
2. 首次实质执行前用 `leadseek route --workspace /项目路径 --owner codex --action plan --reason '具体分工'` 留痕。Codex 直接实现仅限复杂关键部分、少量集成或委派开销明显更高的微小改动，并说明具体原因。
3. 将任务 JSON 写入工具项目 `.state/inbox/`，或通过 stdin 传入。只给必要上下文、文件与项目规则，不发送整个聊天或无关私人资料。执行 `leadseek run --task /任务.json`。
4. 工具在 DSH 原生工作区创建 `Codex · 任务名称` 会话，先让执行者切换到输入快照再工作。模型和插件由 DSH 原配置决定。完整公开记录留本地，Codex 默认只读精简回执，必要时检查 `changes.patch`、指定日志片段和实际测试。
5. `completed` 只表示流程和范围检查成功，不代表业务正确。需要返工时自主运行 `leadseek revise 任务编号 --instruction '问题、方向与验收标准'`；edit 通过验收后运行 `leadseek apply 任务编号 --note '真实依据'`，应用前会检查原项目是否变化。只读任务用 route 的 accepted/rejected 记录判断。
6. Codex 自动审核、返工、应用并安排下一项，直到原目标完成。普通可逆选择自主处理；确实缺少信息、涉及高度隐私或核心安全利益的重要操作，才向用户确认。失败应先诊断并有限返工，不转为高性能模型全包。

```json
{
  "goal": "明确目标与验收标准",
  "workspace": "/原项目绝对路径",
  "mode": "edit",
  "read_paths": ["相关接口.py"],
  "write_paths": ["src/目标模块.py"],
  "constraints": ["遵守项目规则；仅修改授权文件"],
  "checks": [["python3", "-B", "-m", "unittest"]],
  "timeout_seconds": 600,
  "result_max_chars": 2500
}
```

- mode：edit 暂存修改；inspect 只读检查；research 检索；process 常规内容加工。除 edit 外 write_paths 为空。不能假称 process 是操作系统断网模式。
- read_paths/write_paths 是项目相对路径，以 `/` 结尾表示目录；不支持 glob。凭据、.env、依赖及内部状态不复制，输入上限 2000 文件 / 50 MiB。项目规则通过 constraints 和必要文件传入。
- checks 是执行者应做的检查，不是工具自动认证。超时、日志上限和范围检查由桥接处理；桌面模式的工具、插件、子代理执行策略归 DSH，不能把事后采集计数称为强制硬预算。
- 每轮委派、返工都有原生 DSH 会话及本地 run_id/session_id/PID 对应。查询用 `leadseek list/status/result/audit`，不需要打开界面；返工会创建关联的新会话并保留原记录。
- 本机监控只读，页面手动刷新；用量为已采集事件，缺失显示未知。公开消息与工具结果经脱敏，不展示隐藏思考或 system/developer 指令。`parent_usage_only` 不含全部子代理，不把日志压缩率或采集计数当成账单节省率。

本工具在当前 Codex 工作期间由 Codex 推进审核循环；不会在 Codex 结束后自行成为总控代理。独立 headless 只保留为用户明确选择的兼容模式，不能冒充桌面同进程模式。
