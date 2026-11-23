# Flow-Skill

Flow-Skill 是面向多步骤 Agent Skill 的轻量流程控制包。

核心职责：

- Python 定义流程、依赖、条件和校验规则；
- Agent 执行当前业务 Task；
- SQLite 保存 Run、Task 和事件状态；
- Workspace 保存输入、中间文件和输出；
- 通过 `get_next_task()` 和 `submit_task_result()` 与 Agent 交互。
- 通过业务 `validator` 提供完成任务检查门禁，校验不通过不能推进流程。

V1 采用 Task 级恢复，不保存独立 Attempt。`stale_after` 是遗留运行任务识别阈值，
不是强制执行期限；系统不会运行后台定时器或主动终止 Agent。

V1 已暂时关闭失败自动重试：Agent 提交 `execution_error` 会直接让 Task 和 Run 进入
`failed`；`max_retries` 参数保留但暂不生效。

## 0.2.0 新特性：声明式输入输出契约 + 路径契约

任务可以声明输入输出（设计文档见 `docs/输入输出契约设计.md` 与 `docs/文件路径契约.md`）：

```python
from flow_skill import Flow, FileInput, FileOutput, JsonInput, JsonOutput, TextInput, ValidationResult

def validate_collect(result, ctx):
    # 契约已核验路径/格式/存在性，validator 只写业务语义
    return ValidationResult.accept()

flow = Flow("shop-report", version="1.0")   # CLI 加载时自动探测 base_dir（workflow.py 所在目录）

flow.agent(
    id="collect",
    instruction="tasks/collect.md",          # 领取时下发绝对路径，与 CWD 无关
    validator=validate_collect,
    inputs=[TextInput(name="region", source="run_inputs", key="region")],
    outputs=[
        FileOutput(name="workbook", path="outputs/run.xlsx", format="xlsx",
                   metadata={"sheets": ["collect__明细", "collect__汇总"]}),
        JsonOutput(name="summary", required={"total": int, "region": str}),
    ],
)

flow.agent(
    id="analyze",
    instruction="tasks/analyze.md",
    after="collect",
    validator=validate_collect,
    inputs=[
        FileInput(name="sales", from_task="collect", output="workbook",
                  sheet="collect__明细"),      # 装配为 workspace 内绝对路径 + sales_meta
        JsonInput(name="summary_in", from_task="collect", output="summary"),
    ],
    outputs=[JsonOutput(name="verdict", schema={
        "type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]})],
)
```

契约在三处生效：**定义期**（path 越界、`..`、from_task 不存在或不在 after 中、
sheet 前缀不归属、metadata 与 format 不匹配 → 加载即报错）；**领取期**（inputs 按声明
拍平注入 payload，`outputs_spec` 随 payload 下发，required 装配不出来任务不发放）；
**提交期**（键存在、类型 schema、文件存在、xlsx sheet 所有权与孤儿检查、md 标题/篇幅
→ 不过即 rejected）。

校验失败有重试闭环：rejected 响应携带完整 errors，累计被拒达到
`max_validation_retries`（默认 3）任务和 Run 才失败；required 输入的来源任务被
`when` 跳过时下游自动级联跳过，不会死锁。

## 数据目录与 Run 注册表

Run 数据位置不再依赖 Agent 记住路径：

- **create-run 解析链**：`--data-dir` > 环境变量 `FLOW_SKILL_DATA_DIR` >
  `Flow(default_data_dir=...)` > `.flow-data` 兜底；落库前做可写探针，不可写立即报可行动错误；
- **注册表**：create-run 成功后把 `run_id → data_dir` 写入 workflow.py 同目录的
  `.flow-registry.json`（可用 `FLOW_SKILL_REGISTRY` 改位置）；续跑命令不传 `--data-dir`
  时自动查注册表——**注册表是唯一事实源**；
- 显式 `--data-dir` 与注册位置不一致 → 报错；数据确已迁移时加 `--rebind` 改绑；
  存量 Run 用 `--register` 收编进注册表；
- 注册表按技能独立存放，多技能天然隔离；`skill_path` 指纹防止两个同名 flow
  共用一个 data_dir 造成数据混淆。

## 安装

### 从 GitHub 子目录安装

`flow-skill` 是 `zl1019/common_tools` 多包仓库中的独立包。业务 Skill 可以直接按
GitHub 子目录安装：

```powershell
python -m pip install "flow-skill @ git+https://github.com/zl1019/common_tools.git@master#subdirectory=flow-skill"
```

生产环境建议固定到提交 SHA 或发布标签，而不是直接跟随 `master`：

```powershell
python -m pip install "flow-skill @ git+https://github.com/zl1019/common_tools.git@4f59abe89ca06da720f5c6cb949886f5e0a672e4#subdirectory=flow-skill"
```

### 从本地源码安装

```powershell
python -m pip install -e "C:\path\to\common_tools\flow-skill"
```

开发或修改包源码时使用可编辑安装；只消费固定版本时使用普通安装。

### 验证安装

```powershell
python -c "import flow_skill; print(flow_skill.__version__)"
flow-skill --help
```

要求 Python 3.10 或更高版本。包只依赖 Python 标准库，不需要额外数据库服务。

安装后，业务 Skill 仍应把 `.flow-data` 或自定义 `--data-dir` 放在持久化Workspace中，
不要写入包安装目录。

## 定义流程

```python
from flow_skill import Flow, ValidationResult


def validate_result(result, ctx):
    if not isinstance(result, dict) or not result.get("output_file"):
        return ValidationResult.reject("缺少output_file")
    return ValidationResult.accept()

flow = Flow("competitor-research", version="1.0")

flow.agent(
    id="collect",
    instruction="tasks/collect.md",
    validator=validate_result,
    stale_after=1800,
)

flow.human(
    id="confirm",
    instruction="tasks/confirm.md",
    after="collect",
    mode="approval",
)

flow.agent(
    id="report",
    instruction="tasks/report.md",
    after="confirm",
    when=lambda ctx: ctx.output("confirm", "decision") == "approve",
    validator=validate_result,
)

if __name__ == "__main__":
    flow.cli()
```

## CLI

```text
python workflow.py create-run --inputs-json '{}' [--inputs-file params.json]
python workflow.py get-next-task --run-id RUN_ID [--data-dir D] [--rebind] [--register]
python workflow.py submit-task-result --run-id RUN_ID --task-id collect \
  --result-type execution_result --result-json '{}'
python workflow.py status --run-id RUN_ID
```

所有命令将结构化 JSON 输出到标准输出。0.2.0 起续跑命令**可以不传 `--data-dir`**——
注册表会自动定位 Run；显式路径与注册位置不一致时报错，确需迁移加 `--rebind`。

