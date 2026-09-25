# codex-lead-deepseek

Codex 统筹、DeepSeek 执行的本地工具。Codex 负责需求、架构、拆解和最终验收，把标准明确且可核验的代码、检索、常规文本加工等执行工作交给本机 DeepSeek Harness。本文面向首次在新 Mac 上使用的人；更完整的命令与机制说明见 [`使用说明.md`](使用说明.md)，真实执行记录见 [`验证记录.md`](验证记录.md)。

## 用途与分工

- Codex 保留：需求理解、关键判断、架构方案、复杂定位、任务拆解、最终验收。
- DeepSeek 执行：标准明确、上下文可限定、结果可检查的工作，包括代码修改、只读分析、公开资料检索、文本加工、测试执行和日志归纳。
- 路由是默认倾向，不是强制全模型路由。Codex 可以直接处理关键复杂部分、少量集成胶水或委派开销更高的微小动作；委派与接手都记录在 `.state/`，可用 `leadseek audit` 回看。
- 只读的 `inspect`/`research`/`process` 任务不写文件；`process` 禁止网页检索、直接返回成品（模型调用仍需网络，并非操作系统级断网）。

## 依赖

- macOS，可用的终端与网络。
- Codex（在本机 Codex 中发起委派，本工具不替代 Codex）。
- Python 3.9+（工具本身使用标准库）。
- Node.js 24+ 与 npm（运行固定版本 Harness）。
- GitHub CLI `gh`（克隆私有仓库用；`./bin/setup` 不检查 `gh`）。
- 本仓库为私有仓库，克隆前需要有该仓库访问权限的 GitHub 账号。
- `./bin/setup` 只检查 Python 3.9+、Node 24+、npm，**不会自动安装系统依赖**；缺什么需自行先装好。

## 新 Mac 上手

### 1. 登录 GitHub

```sh
gh auth login
```

已经登录过可跳过；私有仓库需要当前账号有访问权限。

### 2. 克隆仓库

```sh
gh repo clone Tsou92/codex-lead-deepseek
cd codex-lead-deepseek
```

如果克隆到了别的名字或路径，后续命令都在该仓库根目录执行即可。

### 3. 安装并接入

```sh
./bin/setup
```

`./bin/setup` 会一次完成：检查 Python 3.9+、Node 24+、npm；安装固定版本 Harness `0.1.7-alpha.2`；把 Codex 全局 Skill、规则和 CLI 接入本机。可选参数：

```sh
./bin/setup --node PATH或命令名        # 指定 node
./bin/setup --credentials 凭据文件路径  # 指定凭据文件
./bin/setup --skip-runtime            # 跳过运行时安装
```

它不会自动登录，也不会自动做 API 测试。可重复运行；若更换目录或挂载路径，需先在原位置运行 `./bin/install-codex --uninstall` 解除旧入口，移动后再在新位置的仓库根目录运行 `./bin/setup`。只在新位置重复 setup 不能覆盖旧副本已建立的链接。

### 4. 配置本机凭据

通用配置 `config.json` 中 `node` 为 `auto`、`credentials_path` 为 `~/.dsh/.credentials.yaml`。本机需要覆盖时，在仓库根目录新建 `config.local.json`（已被 Git 忽略，不会上传）。`config.local.json` 只放路径和选项（如 `node`、`credentials_path`），密钥不要写进任何配置 JSON，凭据由 Harness 凭据文件（默认 `~/.dsh/.credentials.yaml`）管理。

若本机还没有 DeepSeek 凭据：

```sh
./bin/dsh web
```

在本机 Harness 网页的 Settings → Models 里配置 DeepSeek 的 `DEEPSEEK_API_KEY`。**不要在聊天或任务里发送密钥。** 配置完在运行它的终端按 Ctrl+C 停止这个临时 web 服务（不是关闭浏览器页面），然后可运行：

```sh
./bin/leadseek doctor
```

`doctor` 只检查运行时、凭据文件和全局链接，不验证 API 是否有效。

### 5. 用真实小任务验证

下面的命令读取 `examples/process-task.json`，把 `workspace` 动态改成本仓库内 `examples/demo-project` 的绝对路径，再经 stdin 交给 CLI。整个命令可直接运行，不需要自己找绝对路径：

```sh
python3 - <<'PY' | ./bin/leadseek run --task -
import json, pathlib
task = json.loads(pathlib.Path("examples/process-task.json").read_text(encoding="utf-8"))
task["workspace"] = str(pathlib.Path("examples/demo-project").resolve())
print(json.dumps(task, ensure_ascii=False))
PY
```

期望的去重结果列表为 `apple banana pear kiwi`（首次出现顺序、小写、去空白）。这是 `process` 模式，不写文件、禁止网页检索（模型调用仍需网络，并非操作系统断网）。请由使用者或 Codex 核对返回结果，CLI 的 `completed` 只表示流程与范围检查通过，不等于业务正确。

### 6. 在 Codex 中使用

新开一个 Codex 聊天，直接输入 `$deepseek-delegate` 启用。新 Skill 需要新任务加载；如果列表未刷新，重新打开 Codex。之后正常提出需求，适合委派的工作会默认自动调用 DeepSeek。

## 日常使用

常用命令（在仓库根目录运行）：

```sh
./bin/leadseek doctor
./bin/leadseek list
./bin/leadseek status 任务编号
./bin/leadseek result 任务编号
./bin/test
```

任意目录下可使用 `~/.local/bin/leadseek`。如果该目录已在 PATH 中，也可以直接输入 `leadseek`；安装器不会修改 shell 配置。

更多命令、任务 JSON 格式与预算边界见 [`使用说明.md`](使用说明.md) 与 [`skill/deepseek-delegate/SKILL.md`](skill/deepseek-delegate/SKILL.md)。

## 本机只读监控

仓库根目录运行 `./bin/monitor`（默认等价于 `start`），或双击 `打开监控.command`，会只在 `127.0.0.1:8765` 启动只读监控并打开系统浏览器：

```sh
./bin/monitor start [--port 8765] [--no-open]
./bin/monitor open | status | stop
./bin/monitor link --thread-id <UUID> --workspace /绝对路径
```

页面从本机 `.state/` 的公开记录读取：任务运行、下发指令、Codex 审核与返工、执行会话、工具调用、子代理、文件改动和 token，并可按已关联的 Codex 会话查看公开消息与调用。DeepSeek 用量只统计已采集到的（含子代理）事件，Codex 会话按去重后的整会话累计展示，两者口径不同、不能相加；未采集的显示未知而不是 0。数据只在本机读取、不上传，服务没有任何写入 API；令牌只出现在本机启动 URL，运行期文件在 `.state/monitor/`（0600/0700）。

页面只展示公开事件和记录，不包含隐藏思考、加密内容或 system/developer 指令，也不对账单精确度或历史子代理数据补全作承诺。Codex 仍用 `route`/`apply`/`revise` 留下真实的分工、审核与验收依据。

## 更新

旧任务如果还保存有 Harness 压缩会话，可在仓库目录运行 `./bin/import-monitor-history` 离线恢复主代理与子代理的公开事件；已有采集记录不会被覆盖。详见 [历史记录恢复](docs/monitor-history.md)。

```sh
git pull
./bin/setup
./bin/test
```

拉取后重新接入并跑一次测试。

安装中途失败会保留已写入的本机配置，便于修复依赖后重试；失败会返回非零退出码，不表示安装完成。

## 移动目录或重新挂载

若更换目录或挂载路径：先在原位置运行 `./bin/install-codex --uninstall` 解除旧入口，移动后在新位置的仓库根目录运行 `./bin/setup`。仅在新位置重复 `./bin/setup` 不能覆盖旧副本已建立的链接。旧符号链接冲突时，不要直接覆盖其他工具的入口；`--uninstall` 只解除本工具的链接和规则，会保留其他全局规则与本目录产物。

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

- 首次安装不保证一次成功，请按 `./bin/leadseek doctor` 与本文验证步骤逐项确认。
- 本文所述流程尚未在第二台 Mac 上实测；写在这里的是接口约定，不是已完成的双机验证。

## 文档

- [`使用说明.md`](使用说明.md)：完整命令、任务模式、预算与安全边界。
- [`验证记录.md`](验证记录.md)：本机真实执行过的任务与测试记录（历史运行日志与验收记录只在原机 `.state/`，不随仓库发布）。
- [`examples/`](examples/)：edit/inspect/research/process 四类任务示例与 demo 项目。
