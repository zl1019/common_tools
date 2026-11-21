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
    max_retries=2,
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
python workflow.py create-run --inputs-json '{}'
python workflow.py get-next-task --run-id RUN_ID
python workflow.py submit-task-result --run-id RUN_ID --task-id collect \
  --result-type execution-result --result-json '{}'
python workflow.py status --run-id RUN_ID
```

所有命令将结构化 JSON 输出到标准输出。默认运行数据目录为当前目录下的
`.flow-data`，也可以通过 `--data-dir` 指定。

