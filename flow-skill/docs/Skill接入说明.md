# Flow-Skill 业务 Skill 接入说明

可直接复制的标准入口模板见：[`SKILL.md.template`](./SKILL.md.template)。业务作者应以该模板为基础，
替换尖括号占位符，并补充本Skill的业务范围、约束和Task说明。

## 1. 适用范围

本文面向需要将多步骤业务 Skill 接入 Flow-Skill 的作者。

适合使用 Flow-Skill 的场景：

- 业务包含多个有顺序或依赖关系的步骤；
- 中间步骤需要 Agent 使用原生工具和推理能力；
- 流程中包含用户输入或人工确认；
- 需要保存 Task 状态并支持任务级恢复；
- 需要条件分支、失败重试和运行记录。

普通知识指导、单步骤操作或一次工具调用不需要接入 Flow-Skill。

Flow-Skill 的职责边界：

```text
业务 Skill：定义业务流程、任务说明、校验规则和业务脚本
Flow-Skill：管理任务顺序、状态、分支、重试、恢复和持久化
Agent：执行当前 Task
SQLite：保存 Run、Task 和事件状态
Workspace：保存输入、中间文件和最终产物
```

## 2. 接入前提

运行环境必须使用 Python 3.10 或更高版本，并能够导入 `flow_skill`。

### 从 GitHub 子目录安装

当前包位于 `zl1019/common_tools` 的 `flow-skill` 子目录：

```powershell
python -m pip install "flow-skill @ git+https://github.com/zl1019/common_tools.git@master#subdirectory=flow-skill"
```

生产环境应固定提交SHA或发布标签：

```powershell
python -m pip install "flow-skill @ git+https://github.com/zl1019/common_tools.git@4f59abe89ca06da720f5c6cb949886f5e0a672e4#subdirectory=flow-skill"
```

### 从本地源码安装

```powershell
python -m pip install -e "C:\path\to\common_tools\flow-skill"
```

### 验证安装

```powershell
python -c "import flow_skill; print(flow_skill.__version__)"
flow-skill --help
```

不要把包安装目录当作运行数据目录。SQLite数据库和Workspace应放在业务Skill的
持久化Workspace中。

如果业务Skill使用 `pyproject.toml` 管理依赖，建议固定版本或提交：

```toml
dependencies = [
    "flow-skill @ git+https://github.com/zl1019/common_tools.git@4f59abe89ca06da720f5c6cb949886f5e0a672e4#subdirectory=flow-skill"
]
```

业务 Skill 不应复制 Flow-Skill 的调度代码。多个业务 Skill 应依赖同一个版本化的
`flow-skill` 包。

建议固定依赖版本：

```toml
dependencies = [
    "flow-skill==0.1.0"
]
```

## 3. 标准目录结构

```text
my-business-skill/
├── SKILL.md
├── workflow.py
├── tasks/
│   ├── 01-input.md
│   ├── 02-process.md
│   ├── 03-confirm.md
│   └── 04-output.md
├── scripts/
│   └── deterministic_operation.py
└── references/
    └── business-rules.md
```

各部分职责：

| 文件 | 职责 |
|---|---|
| `SKILL.md` | 说明何时使用该Skill，以及Agent如何执行Flow循环 |
| `workflow.py` | 定义Task、依赖、分支、重试参数和结果校验器 |
| `tasks/*.md` | 描述单个Task的目标、输入、执行要求、输出和完成标准 |
| `scripts/*.py` | 实现可确定执行的业务计算或数据处理 |
| `references/` | 保存业务规则、数据口径和参考资料 |

不要把运行状态和业务产物写入Skill安装目录。

## 4. 定义工作流

业务 Skill 必须在 `workflow.py` 中创建唯一的 `Flow` 实例：

```python
from flow_skill import Flow, ValidationResult


def validate_collect(result, ctx):
    if not isinstance(result, dict):
        return ValidationResult.reject("结果必须是JSON对象")
    if not result.get("data_file"):
        return ValidationResult.reject("缺少data_file")
    return ValidationResult.accept()


def validate_run_completion(result, ctx):
    if not ctx.output("generate_output"):
        return ValidationResult.reject("最终产物缺失")
    return ValidationResult.accept()


flow = Flow(
    "my-business-skill",
    version="1.0",
    completion_gate=validate_run_completion,
)

flow.human(
    id="input_parameters",
    instruction="tasks/01-input.md",
    mode="input",
)

flow.agent(
    id="collect",
    instruction="tasks/02-collect.md",
    after="input_parameters",
    validator=validate_collect,
    stale_after=1800,
    max_retries=1,
)

flow.human(
    id="confirm",
    instruction="tasks/03-confirm.md",
    after="collect",
    mode="approval",
)

flow.agent(
    id="generate_output",
    instruction="tasks/04-output.md",
    after="confirm",
    when=lambda ctx: ctx.output("confirm", "decision") == "approve",
    stale_after=1800,
)


if __name__ == "__main__":
    raise SystemExit(flow.cli())
```

### 4.1 Flow命名

- Flow名称应与业务Skill目录名一致；
- 只能使用英文字母、数字、点、下划线和连字符；
- 流程发生不兼容变化时必须提升 `version`；
- 已创建的Run必须使用创建时对应的Flow名称和版本恢复。

### 4.2 Task ID

- 同一Flow内必须唯一；
- 使用稳定的英文标识，例如 `collect_data`；
- 已有Run仍可能恢复时，不得修改或复用Task ID；
- Task展示名称和详细说明放在对应Markdown文件中，不放入Task ID。

## 5. Task类型

### 5.1 Agent Task

由Agent执行工具调用、信息整理、内容生成或业务判断：

```python
flow.agent(
    id="analyze",
    instruction="tasks/analyze.md",
    after="collect",
    validator=validate_analyze,
    stale_after=1800,
    max_retries=2,
)
```

参数说明：

| 参数 | 说明 |
|---|---|
| `id` | Task唯一标识 |
| `instruction` | Task说明文件路径 |
| `after` | 前置Task ID或Task ID列表 |
| `when` | 条件函数，返回真时执行，返回假时跳过 |
| `validator` | 结果校验函数 |
| `stale_after` | 遗留运行任务识别阈值，默认1800秒 |
| `max_retries` | 普通执行错误允许自动重试的次数 |
| `metadata` | 传给Agent的额外结构化信息 |

### 5.2 Human Task

由Agent负责向用户提问并等待真实回复：

```python
flow.human(
    id="confirm",
    instruction="tasks/confirm.md",
    after="analyze",
    mode="approval",
    validator=validate_confirm,
)
```

支持两种模式：

- `input`：用户补充文本、参数或文件信息；
- `approval`：用户批准或拒绝当前结果。

Human Task与普通Skill具有相同的信任边界。Flow-Skill保存Agent转交的用户结果，
但不独立验证用户身份。生产发布、付款、删除等高风险操作仍需底层系统独立授权。

## 6. Task说明文件规范

每个Task应有独立说明文件，并至少包含：

```markdown
# Task名称

## 目标

说明本Task要完成的业务结果。

## 输入

- 前序Task的哪些输出
- 需要读取的业务文件或规则
- Workspace中的输入位置

## 执行要求

1. 必须遵循的业务规则
2. 允许使用的工具或脚本
3. 异常和数据缺失的处理方式

## 输出

- 需要写入的产物路径
- 产物数据结构

## 提交结果

给出提交给submit_task_result的JSON结构。

## 完成标准

说明什么结果可以通过validator。
```

Task说明必须自包含，不得依赖Agent记住此前对话中的隐式要求。

## 7. 结果校验器

Agent提交结果不等于Task自动完成。`validator` 是 Flow-Skill 的“完成任务检查门禁”
（Completion Gate）：只有门禁通过，Task才允许进入 `completed`，流程才可以继续领取后续Task。

业务Skill必须为每个有业务产出的Agent Task配置 `validator`；只有纯粹的人工转交Task
可以不配置，且仍需校验用户输入结构的Task应配置validator。

门禁至少检查以下内容：

- 结果JSON结构和必填字段；
- 声明的文件是否真实存在；
- 文件是否位于当前Run Workspace内；
- 记录数量、汇总值和业务规则是否一致；
- 产物是否满足后续Task的输入要求。

门禁失败时：

- Task不能进入 `completed`；
- 流程不能推进到后续Task；
- `submit_task_result` 返回 `rejected` 和具体错误；
- Agent必须修正当前Task后重新提交；
- 门禁不应通过Agent自报的 `status` 绕过。

示例：

```python
def validate_result(result, ctx):
    errors = []

    if not isinstance(result, dict):
        return ValidationResult.reject("结果必须是JSON对象")

    if not result.get("output_file"):
        errors.append("缺少output_file")

    if errors:
        return ValidationResult(accepted=False, errors=errors)

    return ValidationResult.accept()
```

校验器签名：

```python
validator(result, ctx)
```

`ctx` 提供：

```python
ctx.run_id
ctx.inputs
ctx.workspace
ctx.output("前序task_id")
ctx.output("前序task_id", "字段名")
```

校验器应优先验证可观察事实：

- 必需字段是否存在；
- 文件是否存在于当前Run Workspace；
- 文件结构、记录数量和摘要值是否一致；
- 业务数据是否满足明确规则。

校验失败后Task保持当前状态，Agent根据返回的 `errors` 修正并重新提交。

## 7.1 Run完成门禁

除了Task级validator，业务Skill可以配置Flow级 `completion_gate`：

```python
def validate_run_completion(result, ctx):
    final_result = ctx.output("generate_output")
    if not final_result:
        return ValidationResult.reject("最终Task没有提交产物")
    return ValidationResult.accept()


flow = Flow(
    "my-business-skill",
    version="1.0",
    completion_gate=validate_run_completion,
)
```

Run中所有必要Task完成或跳过后，Flow-Skill先执行 `completion_gate`：

- 通过：Run进入 `completed`；
- 失败：Run进入 `failed`，返回 `completion_gate_rejected` 和错误列表；
- 未配置时：默认通过，不建议用于有最终产物的业务Skill。

Run完成门禁适合检查最终产物是否存在、多个Task结果是否一致、最终汇总是否完整，
不能替代单个Task的validator。

## 8. SKILL.md执行协议

业务Skill的 `SKILL.md` 必须要求Agent遵循以下循环。

### 8.1 创建Run

每次独立业务请求创建一个Run：

```text
python workflow.py create-run --inputs-file <request-json> --data-dir <data-dir>
```

Agent必须保存返回的 `run_id`。

`<data-dir>` 必须位于可持久化的用户Workspace中，不能放在Skill安装目录。

### 8.2 获取Task

```text
python workflow.py get-next-task --run-id <run-id> --data-dir <data-dir>
```

Agent必须根据返回的 `status` 处理：

| status | Agent行为 |
|---|---|
| `task_ready` | 只执行返回的当前Task，完成后提交结果 |
| `waiting_user` | 向用户展示问题，尚未回复时停止当前执行轮次 |
| `waiting` | 停止当前执行轮次并保存Run ID |
| `completed` | 汇总最终结果并结束 |
| `failed` | 报告失败Task和原因，不执行后续Task |

### 8.3 提交Agent Task成功结果

```text
python workflow.py submit-task-result --run-id <run-id> --task-id <task-id> --result-type execution_result --result-file <result-json> --data-dir <data-dir>
```

Agent不得提交 `completed`、`failed` 等目标状态。Task状态由Flow-Skill根据提交内容、
validator和重试策略决定。

### 8.4 提交Agent Task执行错误

```text
python workflow.py submit-task-result --run-id <run-id> --task-id <task-id> --result-type execution_error --error-file <error-json> --data-dir <data-dir>
```

错误文件示例：

```json
{
  "type": "source_unavailable",
  "message": "无法读取数据源"
}
```

如果尚未超过 `max_retries`，Task重新进入 `pending`；否则Task和Run进入 `failed`。

### 8.5 提交用户输入

Human Task的 `mode="input"` 使用：

```text
--result-type user_input
```

Human Task的 `mode="approval"` 使用：

```text
--result-type user_decision
```

批准结果建议采用统一结构：

```json
{
  "decision": "approve",
  "comment": "用户确认当前结果"
}
```

业务Skill必须明确允许的 `decision` 值，并通过validator检查。

### 8.6 继续循环

每次 `submit-task-result` 被接受后，Agent必须再次调用 `get-next-task`，直到返回
`waiting`、`waiting_user`、`completed` 或 `failed`。

### 8.7 Agent退出门禁

Agent不得在完成一个Task后直接结束。结束当前执行轮次前，必须完成以下检查：

1. 如果最近一次返回为 `task_ready`，必须执行该Task并成功调用 `submit-task-result`；
2. 提交结果后，必须再次调用 `get-next-task`；
3. 只有以下状态允许退出当前轮次：
   - `completed`：Run已完成，可以正常结束；
   - `waiting_user`：需要用户输入、审批或重试决定，可以暂停，但必须保留 `run_id`；
   - `failed`：Run已失败，报告错误后可以结束；
   - `waiting`：可以结束当前轮次，但必须报告返回的 `reason` 并保留 `run_id`；
4. `task_ready` 状态禁止退出；
5. 不能根据文件已生成、推理已完成或Task数量已执行完自行判断Run结束；
6. 如果Task结果尚未得到 `submit-task-result` 的成功响应，禁止退出。

推荐的退出判断：

```python
response = get_next_task(run_id)

if response.status in {"task_ready", "waiting"}:
    # 必须继续执行，不能退出
    continue_flow_protocol(response)
elif response.status in {"completed", "waiting_user", "waiting", "failed"}:
    # 可以结束当前执行轮次；waiting_user/waiting必须保留run_id并说明原因
    if response.status == "waiting":
        report_waiting_reason(response.reason)
    stop_current_round()
else:
    # 未知状态不得静默退出，应报告协议错误
    report_protocol_error(response)
```

### 8.8 已运行Task的信息

当 `get_next_task` 返回：

```json
{
  "status": "waiting",
  "reason": "task_already_running"
}
```

返回的 `task` 会包含：

```json
{
  "task_id": "collect",
  "task_name": "数据收集",
  "started_at": "2026-11-21T10:00:00+00:00",
  "elapsed_seconds": 1250,
  "stale_after_seconds": 1800,
  "retry_count": 0
}
```

其中 `elapsed_seconds` 是本次调用时计算的已运行秒数，不是后台实时计时。若超过
`stale_after_seconds`，后续调用会转为 `waiting_user`，询问是否重新执行整个Task。

## 9. 返回给Agent的Task数据

`task_ready` 返回内容包含：

```json
{
  "status": "task_ready",
  "run_id": "run_xxx",
  "task": {
    "task_id": "analyze",
    "task_status": "running",
    "type": "agent",
    "instruction": "tasks/analyze.md",
    "retry_count": 0,
    "max_retries": 2,
    "stale_after_seconds": 1800,
    "inputs": {
      "run": {},
      "task": {},
      "dependencies": {}
    },
    "workspace": {
      "root": "...",
      "inputs": ".../inputs",
      "working": ".../working/analyze",
      "outputs": ".../outputs"
    }
  }
}
```

Agent应使用返回的路径，不自行推测其他Run的目录。

## 10. Workspace与产物规范

每个Run使用独立Workspace：

```text
<data-dir>/
└── <flow-name>/
    ├── workflow.db
    └── runs/
        └── <run-id>/
            ├── inputs/
            ├── working/
            │   └── <task-id>/
            └── outputs/
```

接入要求：

- 原始输入写入 `inputs/`；
- Task临时文件写入自己的 `working/<task-id>/`；
- 验收通过的业务产物写入 `outputs/`；
- SQLite只保存状态、JSON结果和文件路径，不保存大型文件内容；
- Task提交结果中的文件路径优先使用相对于Run Workspace的路径；
- 后续Task通过前序Task提交的文件路径读取产物；
- 不覆盖其他Task已经完成的产物；
- 重试Task时必须清理或隔离此前未完成的临时文件。

## 11. 条件分支

条件通过 `when` 定义：

```python
flow.agent(
    id="publish",
    instruction="tasks/publish.md",
    after="confirm",
    when=lambda ctx: ctx.output("confirm", "decision") == "approve",
)
```

条件返回假时Task进入 `skipped`。当前实现中，`completed` 和 `skipped` 都视为依赖已结束，
因此后续Task如需只在批准后运行，也必须设置自己的 `when`，不能只依赖 `after`。

## 12. 遗留运行Task恢复

`stale_after` 不是强制执行期限，而是遗留运行Task识别阈值。

```text
Agent领取Task
→ Task进入running
→ Agent未提交结果
→ 后续再次调用get-next-task
→ 如果超过stale_after，Flow-Skill询问用户是否整Task重试
```

重要规则：

- Flow-Skill没有后台定时器；
- 只有调用 `get-next-task` 时才检查遗留Task；
- Flow-Skill不会主动终止Agent；
- 正常执行超过阈值后提交的结果仍可被验收；
- 遗留Task不会自动重试；
- 用户确认旧执行已停止并选择重试后，整个Task重新执行；
- V1不支持旧执行和新执行并存时的结果仲裁。

用户重试决定：

```json
{
  "decision": "retry"
}
```

用户放弃决定：

```json
{
  "decision": "abort"
}
```

均使用 `user_decision` 提交。

## 12.1 断点续跑与重新执行边界

Flow-Skill支持任务级断点续跑，不恢复Agent内部推理进度。

例如流程为：

```text
a → b → c → d → e
```

如果运行状态为：

```text
a completed
b completed
c completed
d failed
e pending
```

恢复已有Run时：

- 已完成的 `a`、`b`、`c` 不重复执行；
- 默认只重试失败的 `d`；
- `d`成功后继续执行 `e`；
- 不会自动从 `b` 回退重跑 `b-c-d-e`。

当前版本从 `b` 开始重跑的方式是创建新的Run，并在业务输入中指定需要的起始范围。
V1没有提供任意Task回退接口，也不自动清除前序Task的已完成状态。

如果未来增加 `restart_from_task(run_id, task_id)`，必须明确将指定Task及其后继Task
重置为 `pending`，保留其前置Task的结果，并记录一条重启事件；不能直接修改SQLite状态
而跳过事件记录和业务产物清理。

## 12.2 数据库状态与接口状态

数据库状态和 `get_next_task` 返回状态可以保持一致的语义，但不应强行共用同一枚举：

| 层级 | 状态 | 含义 |
|---|---|---|
| Run数据库 | `running` | Run仍可继续或存在运行中的Task |
| Run数据库 | `waiting` | Run等待用户或外部条件 |
| Run数据库 | `completed` | Run完成门禁通过 |
| Run数据库 | `failed` | Run失败或完成门禁失败 |
| `get_next_task` | `task_ready` | 本次调用领取到一个Task |
| `get_next_task` | `waiting_user` | 本次调用需要用户输入、审批或重试决定 |
| `get_next_task` | `waiting` | 本次调用暂时不能领取Task，并返回具体原因 |
| `get_next_task` | `completed` | Run数据库状态为`completed` |
| `get_next_task` | `failed` | Run数据库状态为`failed` |

推荐把 `get_next_task` 的返回视为数据库状态的“动作结果投影”：

```text
数据库 Run=running + 有可领取Task       → task_ready
数据库 Run=running + Task仍在运行       → waiting(task_already_running)
数据库 Run=waiting + 需要用户处理       → waiting_user
数据库 Run=completed                   → completed
数据库 Run=failed                       → failed
```

这样数据库仍保存事实状态，Agent通过接口状态决定本轮是执行、询问、暂停还是退出。

## 13. V1约束

- 同一Run严格串行执行；
- 不支持多Agent并行领取Task；
- 不保存独立Attempt；
- 只支持Task级恢复，不恢复Agent内部推理进度；
- 不提供后台调度、主动通知或Agent唤醒；
- 不提供分布式状态存储；
- SQLite应位于单机本地磁盘，不应放在SMB或NFS共享目录；
- 跨机器业务文件同步由执行平台负责；
- 高风险副作用必须由业务系统保证权限、审批和幂等性。

## 14. 接入检查清单

接入完成前逐项确认：

- [ ] Flow名称与Skill名称一致且符合命名规则；
- [ ] Flow版本已明确；
- [ ] 每个Task ID唯一且稳定；
- [ ] 每个Task都有独立、完整的说明文件；
- [ ] Task依赖和条件分支符合业务预期；
- [ ] Human Task明确使用 `input` 或 `approval`；
- [ ] 关键Task配置了结果validator；
- [ ] Agent不直接提交目标状态；
- [ ] SKILL.md包含完整的获取、执行、提交循环；
- [ ] Run数据目录位于持久化Workspace而非Skill目录；
- [ ] 产物路径限制在当前Run Workspace；
- [ ] 写操作具备业务幂等或人工确认机制；
- [ ] 已说明 `stale_after` 只在后续调度时检查；
- [ ] 已说明用户拒绝后的流程行为；
- [ ] 已说明V1不支持旧执行与重试执行并存。
