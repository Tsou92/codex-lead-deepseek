# 本机任务监控：实现约定

用途：让使用者查看 Codex 下发的任务、实际审核与返工、DeepSeek 主代理和子代理的执行过程、会话和 token。仅本机只读，页面不触发委派、应用代码或模型调用。

## 数据与真实性

- 历史数据来自 `.state/runs/<run_id>/` 的 task/status/result/prompt/events/changes/applied，以及 `.state/decisions.jsonl`。`completed` 是执行完成，不能当成 Codex 已验收；以审核记录与 applied 文件区分待审核、已采纳、返工、失败。
- 新增 `context.json`：`codex_thread_id`（从 CODEX_THREAD_ID 自动捕获）、`previous_run_id`、`model`、`provider`。journal 同步记录环境中的有效 Codex UUID。旧任务没记录的字段显示未记录。
- 新增 `telemetry.jsonl`：每行公开可观测事件，`timestamp,type,agent_id,parent_id,session_id`，可选 `step_id,role,text,tool,call_id,input,result,status,usage,model`。type 约定 `agent_created,agent_finished,message,tool_call,tool_result,usage,subagent_started,subagent_finished,telemetry_status`。usage 使用 Harness 原生 inputTokens/outputTokens/cacheReadTokens/cacheWriteTokens/totalTokens 数值。
- telemetry 从真实 Harness 公开 hooks 采集，不能猜 hook。必须覆盖子代理；不把主代理摘要与 telemetry 再相加。每个代理每个 step 的 usage 只计一次。无 telemetry 的历史记录仅展示 headless 事件和 parent_usage_only，并明确子代理用量未完整记录。未启动子代理应显示 0；不虚构代理树。
- 不采集或展示隐藏 reasoning/thinking、加密内容、system/developer 指令。展示公开用户/助手消息、调用参数/输出、下发任务和验收记录。常见 API key、Authorization、私钥、密码字段做脱敏；不承诺通用秘密识别。
- Codex 仅读取被 context/journal 或显式关联记录引用的 thread。用 UUID 在 `CODEX_HOME/sessions`（默认 `~/.codex/sessions`）定位文件名，不读取其他会话正文。关联记录 `.state/monitor/links.json` 为 `{links:[{thread_id,workspace,linked_at}]}`。验证路径和 session_meta ID。
- Codex 首选最新 `token_usage_record.payload.thread_token_usage`，按 response_id 去重，不能把累计快照求和；仅 token_count 旧格式时标明其为上下文累计。会话用量不能硬分摊给某个 DeepSeek run。缓存输入与 reasoning 输出可能是子集，不能再加到 total。缺失显示未知，不是 0。展示统计范围，不推算账单或节省比例。
- Codex 公开日志来自 response_item 的 user/assistant message（排除 analysis 等内部 channel）与 function/custom_tool_call 和 output。不输出 world_state、session_meta 原文、turn_context 原文、compacted、reasoning。model 可从 turn_context 单独提取。记录量较大时分页，保留日期与 turn_id。

## Python 数据接口（标准库，Python 3.9+）

`src/leadseek/monitor_data.py` 提供 `MonitorStore(root, codex_home=None)`，下面方法均返回可 JSON 序列化并脱敏的对象：

- `overview(workspace=None, query='', status='', offset=0, limit=100)` -> `{updated_at,counts:{total,running,review_pending,accepted,failed},workspaces:[str],runs:[Run],total,offset,has_more,decisions:[Decision],usage:{deepseek:{...Usage,coverage},codex:{sessions:[CodexSummary]}},codex_sessions:[CodexSummary],warnings:[str]}`。统计覆盖当前筛选结果的全部任务，不受分页影响；Codex 会话累计单列，汇总用量不重复。
- `run_detail(run_id)` -> `{run,task,prompt,result,decisions,changes,agents:[Agent],usage:{...Usage,coverage},codex_thread_ids:[str],warnings:[str]}`。
- `run_events(run_id, after=0, limit=100, agent_id=None)` -> Page。telemetry 存在且有有效公开内容时使用它，否则使用 headless；不能将两份相同记录混合。历史事件无时间则 timestamp=null，序号可定位。
- `artifact(run_id, name)` -> `{name,text,truncated}`，只允许 `prompt,patch,stderr`，对应固定文件；最大 512 KiB，明确截断。
- `codex_detail(thread_id, after=0, limit=100)` -> `{session:CodexSummary,items:[Event],next_cursor,has_more,total,warnings:[str]}`。拒绝未关联的 ID。
- `link_codex(thread_id, workspace)` -> 关联记录。此函数仅供本机 CLI，不开放写入 HTTP API。校验 UUID 和绝对 workspace。

Run: `{run_id,goal,workspace,mode,status,review_status,started_at,finished_at,elapsed_seconds,previous_run_id,session_id,model,changed_file_count,tool_count,subagent_count,usage:Usage,codex_thread_id}`。review_status 枚举 `pending,accepted,revision_requested,rejected,unknown`。Agent: `{agent_id,parent_id,session_id,status,model,usage:Usage,tool_count}`。

Usage: `{input_tokens,output_tokens,cache_read_tokens,cache_write_tokens,total_tokens,source}`，未知数值为 null。Event: `{index,timestamp,type,agent_id,session_id,role,tool,call_id,text,input,result,status,usage,turn_id}`；没有的字段可省略。Page: `{items,next_cursor,has_more,total,warnings}`。

CodexSummary: `{thread_id,model,status,started_at,updated_at,usage:Usage,usage_scope,available}`。Decision 保留 journal timestamp/owner/action/reason/run_id/codex_thread_id。

所有路径须校验、拒绝 symlink 和目录穿越，不开放任意文件读取。JSONL 支持损坏行和未写完尾行；读取有上限且返回 warnings，不能无提示伪装完整结果。轮询避免反复解析大日志：按 mtime_ns/size 有界缓存；不要把整份多 MB 日志返给 overview。

## HTTP 与命令

Python 标准库 ThreadingHTTPServer，只绑定 127.0.0.1，默认端口 8765。静态页面 `web/index.html,web/app.js,web/styles.css`。无需 npm 构建和外部 CDN。

- GET `/api/overview`，query 参数 workspace,q,status,offset,limit。
- GET `/api/runs/<id>`；GET `/api/runs/<id>/events?after=&limit=&agent_id=`。
- GET `/api/runs/<id>/artifacts/<prompt|patch|stderr>`。
- GET `/api/codex/<thread_id>?after=&limit=`。
- GET `/api/health` 仅非敏感服务标识；其他 API 需要随机访问令牌，采用本机启动 URL 交换 SameSite=Strict、HttpOnly cookie，再跳转干净 URL。Host 和 Origin 校验防本机服务被外站读取。CSP、nosniff、no-store、no-referrer。不在日志记录 auth URL。
- 所有日志文本只能用 textContent 渲染，不执行 HTML；服务无任意路径/命令执行 API。非 GET（认证方案必要动作除外）拒绝。
- `leadseek monitor start [--port 8765] [--no-open]` 后台启动并返回 URL；已启动可复用。`open/status/stop`；`link --thread-id UUID --workspace PATH` 仅本地关联。内部 `serve` 支持进程启动。元数据/令牌/日志只在 `.state/monitor/`，权限 0600/0700。stop 确认健康探针及实例身份，不能只凭陈旧 PID 杀进程。绑定失败明确报错，不虚假成功。
- `bin/monitor` 和根目录 `打开监控.command` 为启动入口。start/open 使用系统浏览器；可测试 --no-open。

## 页面

中文，工作台布局。顶部任务运行/待审核/已采纳计数与连接状态；左侧可搜索筛选的任务列表；右侧当前任务详情。详情页签：概览、下发指令、Codex 审核、执行会话、子代理、文件改动、Token。提供关联 Codex 会话列表及详细消息/调用查看。全局决策流保留无 run_id 的规划和集成审核。

5 秒刷新，可暂停；保持当前选中、页签、滚动及展开内容；错误明确重试，空状态诚实。事件可按代理过滤、加载更多、展开完整可观测参数/输出；指令与会话可复制。任务 ID/状态/日期可定位。明确执行完成与验收通过不同。

视觉：面向开发者的运行工作台，浅蓝灰背景 #eef3f8，正文 #182a40，侧栏深海蓝 #173651，主操作 #146e9b，审核通过 #208568，返工 #b86a19。中文使用本机 PingFang SC/系统字体；代码用 Menlo。任务流与父子代理关系是主要视觉元素，不做营销大标题、渐变装饰或一堆相同大卡片。适应桌面和窄屏，键盘焦点可见。

## 验收

真实历史记录可查，新任务运行中可更新；至少一次含子代理的真实任务能看到子代理事件与分开计量。关键测试覆盖：usage 去重/缺失、累计 Codex token 不相加、审核状态、部分日志、路径/符号链接/未知线程拒绝、隐藏内容及常见凭据不泄露、XSS 文本、认证/Host/Origin、端口占用、陈旧 PID。浏览器实际检查筛选、详情、会话、分页、刷新及窄屏。不宣称账单精确或全量历史子代理数据可补回。
