# codex-lead-deepseek

Codex 统筹、DeepSeek 执行的本地工具。Codex 负责需求、架构、拆解和最终验收，把标准明确且可核验的代码、检索、常规文本加工等执行工作交给本机 DeepSeek Harness。当前版本 **v1.2.0**，默认以一键安装包部署到新 Mac。更完整的命令与机制说明见 [`使用说明.md`](使用说明.md)，真实执行记录见 [`验证记录.md`](验证记录.md)。

## 用途与分工

- Codex 保留：需求理解、关键判断、架构方案、复杂定位、任务拆解、最终验收。
- DeepSeek 执行：标准明确、上下文可限定、结果可检查的工作，包括代码修改、只读分析、公开资料检索、文本加工、测试执行和日志归纳。
- 路由是默认倾向，不是强制全模型路由。Codex 可以直接处理关键复杂部分、少量集成胶水或委派开销更高的微小动作；委派与接手都记录在 `.state/`，可用 `leadseek audit` 回看。
- 只读的 `inspect`/`research`/`process` 任务不写文件；`process` 禁止网页检索、直接返回成品（模型调用仍需网络，并非操作系统级断网）。

## 一键安装（推荐）

支持 macOS 13.5 或更新版本，Apple Silicon 与 Intel。

1. 用有仓库访问权限的账号登录 GitHub，打开[最新版安装包](https://github.com/Tsou92/codex-lead-deepseek/releases/latest)。
2. 下载 `codex-lead-deepseek-1.2.0-macos.zip`，核对页面附带的 SHA256，然后解压。
3. 双击 `一键安装.command`（若被拦截，先右键“打开”）。脚本会自动：
   - 使用随包、经 SHA256 核验的固定 Python 3.12 与 Node 24（Apple Silicon 和 Intel 各一份），不要求系统预装；
   - 复制到 `~/Applications/Codex-Lead-DeepSeek`（可用 `--destination` 指定别处）；这台 Mac 上若 `~/.local/bin/leadseek` 已指向同一源码，则自动原位复用，保留外置目录，不另建副本；
   - 安装或复用 Harness，接入 Codex 全局 Skill、规则和 CLI；
   - 在 TTY 中隐藏输入 DeepSeek API key：回车跳过、不覆盖既有 credentials，key 不进入聊天、命令行、环境变量或日志；
   - 打开认证监控。
4. 首次运行若被 macOS 拦截（安装包未签名、未公证）：到 **系统设置 → 隐私与安全性**，对已核对来源的安装包点“仍要打开”。不要全局关闭 Gatekeeper，也不要使用 `xattr` 绕过。
5. Codex 应用自身的安装与账号登录仍由用户按官方页面完成，本工具不会声称已自动安装或已登录；DeepSeek API 有效性没有自动验证，`doctor` 只检查运行时、凭据文件和全局入口。

### Harness 来源与网络

zip **不含** Harness，按以下顺序接入：

1. 项目已有可用 Harness：直接沿用；
2. 否则 PATH 能定位官方 `dsh`：以本机同版本本地接入；
3. 否则从官方 npm 安装 `@deepseek-ai/dsh@latest`，并记录实际安装版本。

不固定 `0.1.7`；`latest` 是官方 dist-tag，不一定等于 alpha 最高号，也不承诺兼容未来版本。首次 Harness 安装与 DeepSeek 模型调用需要网络。随包的 Python/Node 是经 SHA 核验的固定环境、可复现；Harness 则跟随官方 `latest`，两者策略不同。

无需手动安装 Git、`gh`、Homebrew、Python 或 Node。

## 高级：从 Git 克隆（可选）

需要源码开发或自定义时才走这条路；普通安装请用上面的一键流程。先 `gh auth login`，再 `gh repo clone Tsou92/codex-lead-deepseek`，进入仓库目录后同样双击/运行 `一键安装.command` 下载依赖并接入。只有确认系统已备好 Python 3.9+、Node 24+、npm 时，才可改用 `./bin/setup`（它不自动安装系统依赖）。

## 配置本机凭据

通用配置 `config.json` 中 `node` 为 `auto`、`credentials_path` 为 `~/.dsh/.credentials.yaml`。需要覆盖时在仓库根目录新建 `config.local.json`（已被 Git 忽略），只放路径和选项，密钥不要写进任何配置 JSON。

无凭据时可运行 `./bin/dsh web`，在本机 Harness 网页的 Settings → Models 填写 `DEEPSEEK_API_KEY`（**不要在聊天或任务里发送密钥**），然后在运行它的终端按 Ctrl+C 停止临时 web 服务。之后 `./bin/leadseek doctor` 只检查运行时、凭据文件和全局链接，不验证 API 是否有效。

## 用真实小任务验证

以下命令用随包 `./bin/python`（无系统 Python 时也能运行）读取 `examples/process-task.json`，把 `workspace` 改为仓库内 `examples/demo-project` 的绝对路径，再经 stdin 交给 CLI：

```sh
./bin/python - <<'PY' | ./bin/leadseek run --task -
import json, pathlib
task = json.loads(pathlib.Path("examples/process-task.json").read_text(encoding="utf-8"))
task["workspace"] = str(pathlib.Path("examples/demo-project").resolve())
print(json.dumps(task, ensure_ascii=False))
PY
```

期望的去重结果列表为 `apple banana pear kiwi`（首次出现顺序、小写、去空白）。这是 `process` 模式，不写文件、禁止网页检索。请由使用者或 Codex 核对结果，CLI 的 `completed` 只表示流程与范围检查通过，不等于业务正确。

## 在 Codex 中使用

新开一个 Codex 聊天，输入 `$deepseek-delegate` 启用。每个任务首次 activate 时，Skill 打开监控并在存在 `CODEX_THREAD_ID` 时自动关联当前会话，同一任务后续步骤不重复；这是 Skill 约束，不是 CLI 按技术去重。若直接访问 `127.0.0.1:8765` 遇到认证问题，请使用 activate（它会带上必要的本机令牌），不要手工粘贴 token URL。之后正常提出需求，适合委派的工作会默认自动调用 DeepSeek。

## 日常使用

常用命令（仓库根目录）：

```sh
./bin/leadseek doctor
./bin/leadseek list
./bin/leadseek status 任务编号
./bin/leadseek result 任务编号
./bin/test
```

任意目录可用 `~/.local/bin/leadseek`；该目录已在 PATH 时可直接输入 `leadseek`。安装器不会修改 shell 配置。

更多命令、任务 JSON 与预算边界见 [`使用说明.md`](使用说明.md) 与 [`skill/deepseek-delegate/SKILL.md`](skill/deepseek-delegate/SKILL.md)。

## 本机只读监控

运行 `./bin/monitor`（默认等价 `start`）或双击 `打开监控.command`，只在 `127.0.0.1:8765` 启动只读监控并打开系统浏览器：

```sh
./bin/monitor start [--port 8765] [--no-open]
./bin/monitor open | status | stop
./bin/monitor link --thread-id <UUID> --workspace /绝对路径
```

页面从本机 `.state/` 的公开记录读取：任务运行、下发指令、Codex 审核与返工、执行会话、工具调用、子代理、文件改动和 token，并可按已关联的 Codex 会话查看公开消息与调用。**刷新由用户手动触发**：顶栏刷新按钮重新读取当前数据，不定时刷新详情。DeepSeek 用量只统计已采集事件（含子代理），Codex 会话按去重后的整会话累计展示，两者口径不同、不能相加；未采集显示未知而不是 0。数据只在本机读取、不上传，服务没有写入 API；令牌只出现在本机启动 URL，运行期文件在 `.state/monitor/`（0600/0700）。

页面只展示公开事件和记录，不包含隐藏思考、加密内容或 system/developer 指令，也不对账单精确度或历史子代理数据补全作承诺。Codex 仍用 `route`/`apply`/`revise` 留下真实的分工、审核与验收依据。

## 更新

已安装版本更新时，下载新 Release 的 zip 并重新运行 `一键安装.command`（复用本机已有运行时与凭据，不覆盖既有 credentials）。旧任务若保存有 Harness 压缩会话，可在仓库目录运行 `./bin/import-monitor-history` 离线恢复公开事件，已有采集记录不会被覆盖，详见 [历史记录恢复](docs/monitor-history.md)。

源码方式更新：

```sh
git pull
./bin/setup
./bin/test
```

安装中途失败会保留已写入的本机配置，便于修复后重试；失败返回非零退出码，不表示安装完成。

## 移动目录或重新挂载

若更换目录或挂载路径：先在原位置运行 `./bin/install-codex --uninstall` 解除旧入口，移动后在新位置运行 `./bin/setup`。仅在新位置重复 setup 不能覆盖旧副本已建立的链接。旧符号链接冲突时不要直接覆盖其他工具入口；`--uninstall` 只解除本工具的链接和规则，保留其他全局规则与本目录产物。

## 解除

```sh
./bin/install-codex --uninstall
```

## 费用与隐私边界

- 这是调用次数与文件范围约束，不是总 token 硬上限，也没有账户同步；实际分工与用量以本机 `.state/` 记录为准。
- 执行端不加载全局 Skill 库，项目规则由 Codex 写入任务约束。
- 不要把无关私有数据或密钥放进任务；凭据只留在本机 Harness 凭据文件中。
- 规则与 Skill 无法在底层拦截 Codex 的每次模型请求或直接文件编辑，也不是操作系统级隔离或网络硬隔离。

## 已知边界

- 安装脚本在 macOS 首次可能被拦截；按“隐私与安全性”的“仍要打开”处理，本工具不提供全局关闭 Gatekeeper 的方案。
- 已在本机进行安装验证，具体结果见验证记录；尚未在第二台实体 Mac 或 Intel 机器上实测。安装器不会自动验证用户的 DeepSeek API key。

## 文档

- [`使用说明.md`](使用说明.md)：完整命令、任务模式、预算与安全边界。
- [`验证记录.md`](验证记录.md)：本机真实执行过的任务与测试记录（历史运行日志与验收记录只在原机 `.state/`，不随仓库发布）。
- [`examples/`](examples/)：edit/inspect/research/process 四类任务示例与 demo 项目。
