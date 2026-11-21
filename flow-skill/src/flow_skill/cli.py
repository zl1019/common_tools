"""CLI adapters for Flow-Skill."""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Sequence

from .errors import FlowSkillError, ValidationError
from .workflow import Flow


def _json_value(raw: str | None, file_path: str | None, *, default: Any = None) -> Any:
    if raw is not None and file_path is not None:
        raise ValidationError("Use either an inline JSON value or a JSON file, not both")
    if file_path is not None:
        return json.loads(Path(file_path).read_text(encoding="utf-8"))
    if raw is not None:
        return json.loads(raw)
    return default


def _write_json(value: Any, *, stream: Any = sys.stdout) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2), file=stream)


def _add_data_dir(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--data-dir",
        default=".flow-data",
        help="Root directory for SQLite state and run workspaces",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="flow-skill")
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create-run", help="Create a new workflow run")
    create.add_argument("--run-id")
    create.add_argument("--inputs-json")
    create.add_argument("--inputs-file")
    _add_data_dir(create)

    get_next = commands.add_parser("get-next-task", help="Atomically get the next task")
    get_next.add_argument("--run-id", required=True)
    _add_data_dir(get_next)

    submit = commands.add_parser("submit-task-result", help="Submit task output or error")
    submit.add_argument("--run-id", required=True)
    submit.add_argument("--task-id", required=True)
    submit.add_argument(
        "--result-type",
        required=True,
        choices=["execution_result", "execution_error", "user_decision", "user_input"],
    )
    submit.add_argument("--result-json")
    submit.add_argument("--result-file")
    submit.add_argument("--error-json")
    submit.add_argument("--error-file")
    _add_data_dir(submit)

    status = commands.add_parser("status", help="Show persisted run and task state")
    status.add_argument("--run-id", required=True)
    _add_data_dir(status)

    events = commands.add_parser("events", help="Show the append-only event history")
    events.add_argument("--run-id", required=True)
    _add_data_dir(events)
    return parser


def run_flow_cli(flow: Flow, argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        if args.command == "create-run":
            inputs = _json_value(args.inputs_json, args.inputs_file, default={})
            if not isinstance(inputs, dict):
                raise ValidationError("Run inputs must be a JSON object")
            run_id = flow.create_run(
                inputs=inputs,
                run_id=args.run_id,
                data_dir=args.data_dir,
            )
            _write_json({"status": "created", "run_id": run_id})
            return 0

        if args.command == "get-next-task":
            response = flow.get_next_task(args.run_id, data_dir=args.data_dir)
            _write_json(response.to_dict())
            return 0

        if args.command == "submit-task-result":
            result = _json_value(args.result_json, args.result_file)
            error = _json_value(args.error_json, args.error_file)
            if error is not None and not isinstance(error, dict):
                raise ValidationError("Error payload must be a JSON object")
            response = flow.submit_task_result(
                args.run_id,
                args.task_id,
                result_type=args.result_type,
                result=result,
                error=error,
                data_dir=args.data_dir,
            )
            _write_json(response.to_dict())
            return 0

        if args.command == "status":
            _write_json(flow.status(args.run_id, data_dir=args.data_dir))
            return 0

        if args.command == "events":
            _write_json(flow.events(args.run_id, data_dir=args.data_dir))
            return 0
    except (FlowSkillError, OSError, json.JSONDecodeError) as exc:
        _write_json(
            {"status": "error", "error_type": type(exc).__name__, "message": str(exc)},
            stream=sys.stderr,
        )
        return 2
    return 2


def _load_module_from_file(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"flow_skill_user_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise ValidationError(f"Cannot load workflow file '{path}'")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_flow(reference: str) -> Flow:
    if reference.lower().endswith(".py"):
        module_reference = reference
        selected_attribute = "flow"
    else:
        module_reference, separator, attribute = reference.rpartition(":")
        if not separator:
            module_reference = reference
            selected_attribute = "flow"
        else:
            selected_attribute = attribute
    candidate = Path(module_reference)
    if candidate.suffix == ".py" or candidate.exists():
        module = _load_module_from_file(candidate.resolve())
    else:
        module = importlib.import_module(module_reference)
    value = getattr(module, selected_attribute, None)
    if not isinstance(value, Flow):
        raise ValidationError(
            f"'{reference}' does not resolve to a flow_skill.Flow instance"
        )
    return value


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(argv) if argv is not None else sys.argv[1:]
    loader = argparse.ArgumentParser(
        prog="flow-skill",
        description="Run a Flow-Skill workflow definition",
        add_help=False,
    )
    loader.add_argument("--workflow", required=True, help="module:attribute or workflow.py:flow")
    loader.add_argument("-h", "--help", action="store_true")
    known, remaining = loader.parse_known_args(arguments)
    try:
        flow = load_flow(known.workflow)
        if known.help and not remaining:
            remaining = ["--help"]
        return run_flow_cli(flow, argv=remaining)
    except (FlowSkillError, OSError, ImportError, AttributeError) as exc:
        _write_json(
            {"status": "error", "error_type": type(exc).__name__, "message": str(exc)},
            stream=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

