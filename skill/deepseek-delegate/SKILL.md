---
name: deepseek-delegate
description: Codex 统筹、DeepSeek 默认执行。把标准明确且可核验的代码、检索、数据整理、内容加工、摘要翻译、转换、测试和批量工作交给本机 Harness；Codex 自动审核、返工并推进，减少高性能模型承担常规执行。用户要求 Codex 总管或节省 token 时使用。
---

# Codex 统筹与 DeepSeek 执行

CLI：`~/.local/bin/leadseek`（该符号链接指向本工具项目根目录下的 `bin/leadseek`）。PATH 中找不到 `leadseek` 时直接使用 `~/.local/bin/leadseek`；全局 AGENTS 规则中也记录了实际路径。无论本工具被克隆或安装到哪个路径，都通过这个全局入口调用，不要在规则或脚本里硬编码当前机器盘符或安装绝对路径。
任务说明与全部日志存放在该 CLI 符号链接真实项目根目录的 `.state/` 下（即工具项目的 `.state`）。

## 分工

Codex 保留需求理解、关键判断、架构与方案、复杂定位、任务拆解和最终验收。先路由再执行：所有标准明确、上下文可限定、结果能检查且现有工具能完成的工作默认交给 Harness，不限于代码和检索；包括提取清洗、分类去重、常规摘要翻译、格式整理、批量转换、执行测试、归纳日志等。禁止为了方便由高性能模型包办全项目。

已授权的常规工作由 Codex 自动委派、审核、返工、验证、应用并继续下一项，直到用户原目标完成，不逐轮征求同意。普通缺省与可逆选择自行判断。只有高度隐私、涉密、核心安全利益的重要或不可逆动作以及更高优先级权限边界需要确认；不扩大原任务授权。确实缺少只有用户知道的信息时不能编造。

首次实质执行前用 `leadseek route --workspace /项目路径 --owner codex --action plan --reason 'Codex 保留哪些判断；哪些执行交给 DeepSeek'` 记一条简短分工。Codex 直接写代码或做批量执行，仅限关键复杂部分、少量集成胶水或委派开销明显更高的微小动作；用 route 记录具体理由，不把”更方便”当理由。模糊需求先由 Codex 缩小不确定性，再下发执行部分。

将相关项目规则写入 constraints，必要时把项目 AGENTS.md 加入 read_paths。执行端不加载全局技能库。检索只发关键词、公共来源要求和必要背景；不要传整个对话、密钥、无关私有材料。公文遵守原有交付形式要求，敏感材料不外发。

## Codex 侧 token 管控

以下规则直接减少 Codex 自身的 token 消耗，必须严格执行：

**下发前不读文件进上下文。** 构造任务时只需确认文件/目录的存在，不要用 read_file、cat 或任何工具把文件内容读入 Codex 上下文再委派——内容会随文件路径一起复制到暂存区，执行端会读它。唯一例外：结果验收阶段需要 Codex 审核 `changes.patch` 或极短的关键接口。

**goal 写目标与验收标准，不写文件内容。** 不要把被修改文件的当前内容、完整日志或大段背景粘进 goal；把必要背景放进 constraints（只写约束规则，不贴原文），文件用 read_paths 传递。

**最小实现决策梯（edit 任务强制执行，按顺序判断）。** Codex 在拆解任务时必须先走这个梯，并在 constraints 里显式告知 DeepSeek：
1. 这个功能真的需要存在吗？不需要就不委派（YAGNI）。
2. 代码库里已有可复用实现？在 constraints 里注明，让 DeepSeek 复用。
3. 标准库或语言内置能覆盖？在 constraints 里注明，禁止引入外部依赖。
4. 平台原生特性能满足？在 constraints 里注明（例如：用 `<input type=”date”>` 而非日期选择库）。
5. 已安装依赖能处理？在 constraints 里注明，禁止新增包。
6. 能用一行解决？constraints 写”单行实现即可”。
7. 以上都不满足：constraints 写”只写恰好够用的最小实现，不加预留抽象”。

**验收精简。** 阅读 `worker_report` 和 `changes.patch` 即可验收常规任务；不要重新把暂存文件全量读入上下文逐行检查，除非 patch 本身显示异常。

**不重复路由记录。** `leadseek route` 只在首次实质执行前调用一次；后续 revise/apply 循环不重复调用。

## 调用流程

1. 每个 Codex 任务首次启用本 Skill、开始任何执行前，先运行 `~/.local/bin/leadseek activate`（PATH 中有 `leadseek` 也可直接调用）。它会启动并打开带认证的本机监控，自动从 `CODEX_THREAD_ID` 关联当前任务；底层每次调用都会实际运行启动脚本、可能再打开页面，去重只靠本指令“每个任务只运行一次”，所以同一任务的后续 run/revise 不要再执行 activate，服务中断后可重试。命令只回精简、不含 token 的 JSON；失败时返回非零和简短提示，按提示处理，不要读取或转述可能含认证 URL 的原始捕获输出。`--no-open` 只用于验证，不弹浏览器。
2. 首次使用或失败时运行 `leadseek doctor`。不要每个小任务重新安装、搜路径或读实现代码。
3. 把下面结构的任务 JSON 写入交付目录的 `.state/inbox/`（先创建目录），或经 stdin 交给 CLI。输入限定到所需文件/目录，避免 Codex 先把全部原文读入自己的上下文。
4. 执行 `leadseek run --task /绝对路径/任务.json`。命令会处理超时、结果筛选和暂存。通过宿主终端的会话句柄等待；不要因为暂时没有 stdout 重复启动。需要看进度时用 `leadseek list` 或 `leadseek status 任务编号`。
5. 只阅读 CLI 返回的精简 JSON。`worker_report` 是执行者的报告；`completed` 表示进程和范围检查成功，不等于业务正确或测试已由 Codex 验证。Codex 对照验收标准判断：合格则继续；不合格直接运行 `leadseek revise 任务编号 --instruction '具体问题、下一步方向和验收标准'`，沿用上轮暂存成果返工，无需用户介入。一般最多两轮有明确依据的返工；仍失败时 Codex 诊断难点、只接手必要部分并记录原因，之后可继续委派，不能静默转成 GPT 全包。不要盲目重试或放宽权限绕过阻断。
6. edit 任务检查 `changes.patch`，按需读取暂存文件并独立运行必要检查。通过后运行 `leadseek apply 任务编号 --note '实际验收依据'`，再做项目集成检查。原项目已变化时重新准备或整合，不能强制覆盖。research/process/inspect 合格与否用 route 的 `--action accepted` 或 `--action rejected --run-id 任务编号` 留下简短判断；检索资料必须核对版本和时效，不能由“文档没提及”推断“不支持”。
7. 自动给出下一项指令并循环上述流程，直到原目标完成。最终给用户结果及实际分工，不用等待用户催促续做。`leadseek audit --workspace /项目路径` 可查看委派、返工、验收和 Codex 接手原因。

任务格式（JSON；CLI 不接受未声明的字段）：

```json
{
  "goal": "具体任务及验收标准",
  "workspace": "/原项目绝对路径",
  "mode": "edit",
  "read_paths": ["相关接口.py", "tests/"],
  "write_paths": ["src/指定模块.py"],
  "constraints": ["保留现有接口；遵循本项目约定"],
  "checks": [["python3", "-B", "-m", "unittest"]],
  "subagents": 0,
  "timeout_seconds": 600,
  "result_max_chars": 2500
}
```

- `mode`：`edit` 暂存文件修改或生成（不限代码）；`inspect` 只读分析；`research` 公共网络检索；`process` 常规文本/数据加工，直接返回成品，禁止网页检索、不写文件（模型调用仍需网络，并非操作系统级断网）。
- `read_paths`/`write_paths`：项目相对路径，不支持 glob。以 `/` 结尾表示整个子目录；写入范围必须精确。读取 `.` 可选，但优先只给相关目录。凭据、`.env`、依赖目录、Git 内部目录不复制；输入限 2000 个文件、50 MiB。
- inspect 必须有 `read_paths`；research/process 可为空；三者 `write_paths` 必须为空。edit 必须有写入范围。文本可在 goal 中给出，也可指定输入文件；预计成品较长时按需增大 result_max_chars（上限12000），被截断时读取 final 事件原文而非整份日志。只读文件也会复制到暂存区，缺依赖时由 Codex 在原项目验证。
- `subagents` 默认为 0。确有独立并行工作时设 1–3，指定各自文件责任，禁止重复实现同一模块。执行预算插件同时限制子代理启动次数与全部代理共用的工具次数，Harness 再限制委派深度和可续接槽位。默认共 40 次工具、6 次搜索、10 次网页获取；触及预算向 Codex 返回，不换工具绕过。可在 config.local.json 中覆盖这些限制。这不是累计 token 硬上限，不能把调用数量当费用预算。
- `checks` 是交给执行者的预期检查，不是 CLI 自动认证。与任务无关的检查不运行。

## 本机只读监控

本机可运行 `./bin/monitor`（默认 `start`）或双击 `打开监控.command`，只在 `127.0.0.1:8765` 启动只读监控；子命令为 `start/open/status/stop/link`，`link` 仅本机关联 Codex 会话。用户可随时打开页面查看公开的任务、审核与返工、执行会话、工具调用、子代理和 token。已有实例在启动时会从 `CODEX_THREAD_ID` 自动关联当前会话，不需要用户手工提供。DeepSeek 用量只统计已采集事件（含子代理），Codex 整会话按去重后累计展示，两种口径不同；未采集显示未知，只在本机、不上传。页面不展示隐藏思考或内部指令，也不承诺账单精确。Codex 的 `route`/`accepted`/`revise` 应留下具体依据、发现和下一步，不能用监控数据替代真实验收证据。

## 省 token 的执行约束

一次交付足够完整的任务包；不要把任务碎成大量一行请求。公开搜索需要原始链接、日期和精简结论。原始 JSONL、思考、中间工具输出只保存在磁盘；Codex 默认不读全量日志。摘要不够时按问题定位少量证据。

这是运行在当前 Codex 工作期间的本机工具，不会在 Codex 任务结束后自行持续执行。默认路由是由全局指令和 Skill 约束 Codex 的工作行为，CLI 无法在底层拦截 Codex 的每次模型请求或文件编辑；不能宣称技术上绝对禁止其直接执行。以实际任务记录核对分工。不要把压缩率说成账单节省率；CLI 的 `parent_usage_only` 也不包括所有子代理消耗。
