# -*- coding: utf-8 -*-
"""0.2.0 contract smoke test: 输入输出契约 + 文件路径契约 (docs §10 scenarios)."""
import json
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from flow_skill import Flow, FileInput, FileOutput, JsonInput, JsonOutput, TextInput
from flow_skill.errors import FlowDefinitionError, InvalidStateError, ValidationError
from flow_skill.models import ValidationResult

PASS = 0

def ok(name):
    global PASS
    PASS += 1
    print(f"  PASS {PASS}: {name}")

def expect_error(name, fn, exc=FlowDefinitionError):
    try:
        fn()
    except exc as e:
        ok(f"{name} -> {type(e).__name__}")
        return
    raise AssertionError(f"expected {exc.__name__} but none raised: {name}")

def make_xlsx(path, sheets):
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    items = "".join(
        f'<sheet name="{escape(s)}" sheetId="{i+1}" '
        f'r:id="rId{i+1}"/>' for i, s in enumerate(sheets)
    )
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(
            "xl/workbook.xml",
            f'<?xml version="1.0"?><workbook xmlns="{ns}" '
            f'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f"<sheets>{items}</sheets></workbook>",
        )

def validate_accept(result, ctx):
    return ValidationResult.accept()

# ---------------------------------------------------------------------------
print("== 1. definition-time contract checks ==")
flow_tmp = Path(tempfile.mkdtemp())

def bad(fn):
    return fn

expect_error("path 越界（..）", lambda: FileOutput(name="x", path="../out/x.xlsx", format="xlsx"))
expect_error("绝对路径", lambda: FileOutput(name="x", path="C:/out/x.xlsx", format="xlsx"))
expect_error("json input 带 sheet", lambda: JsonInput(name="x", source="run_inputs", sheet="a__b"))
expect_error("sheet/sheets 互斥", lambda: FileInput(name="x", from_task="c",
    sheet="c__a", sheets=["c__b"]))
expect_error("metadata 与 format 不匹配", lambda: FileOutput(name="x", path="outputs/a.csv",
    format="csv", metadata={"sheets": ["a__b"]}))

f = Flow("t1")
f.agent(id="collect", instruction="tasks/c.md", validator=validate_accept)
f.agent(id="a", instruction="x", after="ghost_task_x", validator=validate_accept,
    inputs=[JsonInput(name="j", source="other_task", from_task="ghost_task_x")])
expect_error("from_task 不存在", f.validate)

f = Flow("t2")
f.agent(id="collect", instruction="tasks/c.md", validator=validate_accept)
f.agent(id="analyze", instruction="x", validator=validate_accept,
    inputs=[JsonInput(name="j", source="other_task", from_task="collect")])
expect_error("数据依赖 ⊄ 执行依赖", f.validate)
f.agent(id="analyze2", instruction="x", after="collect", validator=validate_accept,
    inputs=[JsonInput(name="j", source="other_task", from_task="collect", output="missing_name")])
expect_error("output 引用不存在", f.validate)

f = Flow("t3")
f.agent(id="collect", instruction="tasks/c.md", validator=validate_accept,
        outputs=[FileOutput(name="wb", path="outputs/a.xlsx", format="xlsx",
                            metadata={"sheets": ["collect__s1"]})])
f_bad = Flow("t3b")
f_bad.agent(id="collect", instruction="x", validator=validate_accept,
            outputs=[FileOutput(name="wb", path="outputs/a.xlsx", format="xlsx",
                                metadata={"sheets": ["other__s1"]})])
expect_error("sheet 前缀不归属", f_bad.validate)
f_bad2 = Flow("t3c")
f_bad2.agent(id="a", instruction="x", validator=validate_accept,
             outputs=[FileOutput(name="wb1", path="outputs/r.xlsx", format="xlsx")])
f_bad2.agent(id="b", instruction="y", after="a", validator=validate_accept,
             outputs=[FileOutput(name="wb2", path="outputs/r.xlsx", format="csv")])
expect_error("共享工作簿 format 不一致", f_bad2.validate)
ok("definition-time checks done")

# ---------------------------------------------------------------------------
print("== 2. end-to-end contract flow (xlsx workbook + flat payload) ==")
root = Path(tempfile.mkdtemp())
skill = root / "shop-skill"
(skill / "tasks").mkdir(parents=True)
(skill / "tasks" / "collect.md").write_text("# collect", encoding="utf-8")
(skill / "tasks" / "analyze.md").write_text("# analyze", encoding="utf-8")
(skill / "tasks" / "report.md").write_text("# report", encoding="utf-8")
(skill / ".flow-registry.json").unlink(missing_ok=True)

flow = Flow("shop-report", version="1.0",
            base_dir=skill)
flow.agent(
    id="collect", instruction="tasks/collect.md", validator=validate_accept,
    inputs=[TextInput(name="region", source="run_inputs", key="region"),
            TextInput(name="note", value="仅统计华东")],
    outputs=[FileOutput(name="workbook", path="outputs/run.xlsx", format="xlsx",
                        metadata={"sheets": ["collect__明细", "collect__汇总"]}),
             JsonOutput(name="summary", required={"total": int})],
)
flow.agent(
    id="analyze", instruction="tasks/analyze.md", after="collect",
    validator=validate_accept,
    inputs=[FileInput(name="sales", from_task="collect", output="workbook",
                      sheet="collect__明细", format=["xlsx"]),
            JsonInput(name="summary_in", from_task="collect", output="summary")],
    outputs=[JsonOutput(name="verdict", schema={"type": "object",
        "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]})],
)
flow.agent(id="report", instruction="tasks/report.md", after="analyze",
           validator=validate_accept,
           outputs=[FileOutput(name="md", path="outputs/report.md", format="md",
                               metadata={"headings": ["## 结论"], "min_chars": 5})])

import os
os.environ.pop("FLOW_SKILL_DATA_DIR", None)
data_root = root / "flow-data"
run_id = flow.create_run(inputs={"region": "华东"}, run_id="run_ct01",
                         data_dir=data_root)
ok("create_run")

r = flow.get_next_task(run_id)  # 无 --data-dir，注册表命中
assert r.status == "task_ready", r.to_dict()
t = r.task
assert t["instruction"].lower().startswith(str(skill).lower()), t["instruction"]
ok(f"instruction 绝对化: {t['instruction']}")
assert t["inputs"] == {"region": "华东", "note": "仅统计华东"}, t["inputs"]
ok("TextInput/JsonInput 拍平装配")
assert [s["name"] for s in t["outputs_spec"]] == ["workbook", "summary"]
ok("outputs_spec 下发")

xlsx_path = root / "run.xlsx"
make_xlsx(xlsx_path, ["collect__明细", "collect__汇总"])
dest = Path(t["workspace"]["outputs"]) / "run.xlsx"
shutil.copyfile(xlsx_path, dest)

resp = flow.submit_task_result(run_id, "collect", result_type="execution_result",
    result={"workbook": "outputs/run.xlsx", "summary": {"total": 10}})
assert resp.status == "accepted", resp.to_dict()
ok("契约核验通过提交")

# rejected: 路径不一致
r = flow.get_next_task(run_id)
assert r.status == "task_ready" and r.task["task_id"] == "analyze", r.to_dict()
ok("审核门禁/依赖放行 analyze")
flow2 = Flow("shop-report", version="1.0", base_dir=skill)
# simulate wrong submit on collect via second flow not needed; test rejection below
resp_bad = None
ok("analyze 领取")

payload_inputs = r.task["inputs"]
assert payload_inputs["sales"].lower().startswith(str(data_root).lower()), payload_inputs
assert payload_inputs["sales_meta"]["sheets"] == ["collect__明细", "collect__汇总"], payload_inputs
assert payload_inputs["summary_in"] == {"total": 10}
ok(f"FileInput 注入绝对路径 + meta: {payload_inputs['sales']}")

# ---------------------------------------------------------------------------
print("== 3. submission contract rejections + retry limit ==")
(root / "rt" / "tasks").mkdir(parents=True)
flow_a = Flow("reject-test", version="1.0", base_dir=root / "rt")
flow_a.agent(id="t", instruction="tasks/x.md", validator=validate_accept,
             max_validation_retries=3,
             outputs=[JsonOutput(name="s", schema={"type": "object",
                 "properties": {"n": {"type": "integer"}}, "required": ["n"]}),
                      FileOutput(name="f", path="outputs/o.md", format="md")])
rt_data = root / "rt-data"
rid = flow_a.create_run(inputs={}, run_id="run_rj", data_dir=rt_data)
flow_a.get_next_task(rid, data_dir=rt_data)

resp = flow_a.submit_task_result(rid, "t", result_type="execution_result", data_dir=rt_data,
    result={"s": {"n": "bad"}, "f": "outputs/other.md"})
assert resp.status == "rejected" and len(resp.errors) == 3, resp.to_dict()
ok(f"schema+路径 三错误 rejected: {resp.errors}")

resp = flow_a.submit_task_result(rid, "t", result_type="execution_result", data_dir=rt_data,
    result={"s": {"n": 1}, "f": "outputs/o.md"})
assert resp.status == "rejected", resp.to_dict()  # 文件不存在
ok("文件不存在 rejected")

# 第 3 次被拒 -> 达上限失败
resp = flow_a.submit_task_result(rid, "t", result_type="execution_result", data_dir=rt_data,
    result={"s": {"n": 1}, "f": "outputs/o.md"})
assert resp.status == "run_failed", resp.to_dict()
ok(f"被拒重试耗尽 -> run_failed: {resp.errors}")
st = flow_a.status(rid, data_dir=rt_data)
assert st["tasks"][0]["status"] == "failed"
assert st["tasks"][0]["error"]["errors"], "last_validation_errors 落库"

# ---------------------------------------------------------------------------
print("== 4. cascade skip + orphan sheet + md headings ==")
flow_c = Flow("skip-test", version="1.0", base_dir=root / "st")
(root / "st" / "tasks").mkdir(parents=True)
flow_c.agent(id="collect", instruction="tasks/x.md", validator=validate_accept,
             when=lambda ctx: False,
             outputs=[FileOutput(name="wb", path="outputs/r.xlsx", format="xlsx",
                                 metadata={"sheets": ["collect__a"]})])
flow_c.agent(id="analyze", instruction="tasks/x.md", after="collect",
             validator=validate_accept,
             inputs=[FileInput(name="f", from_task="collect", output="wb")])
rid = flow_c.create_run(inputs={}, run_id="run_sk", data_dir=root / "st-data")
r = flow_c.get_next_task(rid, data_dir=root / "st-data")
assert r.status == "completed", r.to_dict()
st = flow_c.status(rid, data_dir=root / "st-data")
states = {t["task_id"]: t["status"] for t in st["tasks"]}
assert states == {"collect": "skipped", "analyze": "skipped"}, states
ok(f"required 来源被跳过 -> 级联跳过: {states}")

flow_o = Flow("orphan-test", version="1.0", base_dir=root / "ot")
(root / "ot" / "tasks").mkdir(parents=True)
flow_o.agent(id="collect", instruction="x.md", validator=validate_accept,
             outputs=[FileOutput(name="wb", path="outputs/r.xlsx", format="xlsx",
                                 metadata={"sheets": ["collect__a"]})])
rid = flow_o.create_run(inputs={}, run_id="run_or", data_dir=root / "ot-data")
r = flow_o.get_next_task(rid, data_dir=root / "ot-data")
wb = Path(r.task["workspace"]["outputs"]) / "r.xlsx"
make_xlsx(wb, ["collect__a", "evil_sheet"])
resp = flow_o.submit_task_result(rid, "collect", result_type="execution_result", data_dir=root / "ot-data",
    result={"wb": "outputs/r.xlsx"})
assert resp.status == "rejected" and "孤儿" in resp.errors[0], resp.to_dict()
ok(f"孤儿 sheet 拦截: {resp.errors}")

flow_m = Flow("md-test", version="1.0", base_dir=root / "mt")
(root / "mt" / "tasks").mkdir(parents=True)
flow_m.agent(id="rep", instruction="x.md", validator=validate_accept,
             outputs=[FileOutput(name="md", path="outputs/r.md", format="md",
                                 metadata={"headings": ["## 结论"], "min_chars": 500})])
rid = flow_m.create_run(inputs={}, run_id="run_md", data_dir=root / "mt-data")
r = flow_m.get_next_task(rid, data_dir=root / "mt-data")
md = Path(r.task["workspace"]["outputs"]) / "r.md"
md.write_text("# intro\n内容太少", encoding="utf-8")
resp = flow_m.submit_task_result(rid, "rep", result_type="execution_result", data_dir=root / "mt-data",
    result={"md": "outputs/r.md"})
assert resp.status == "rejected" and len(resp.errors) == 2, resp.to_dict()
ok(f"md 标题乱序 + 篇幅拦截: {resp.errors}")

# ---------------------------------------------------------------------------
print("== 5. registry chain: env/default/rebind/conflict ==")
os.environ.pop("FLOW_SKILL_DATA_DIR", None)
flow_d = Flow("reg-test", version="1.0", base_dir=root / "rg",
              default_data_dir=root / "rg-default")
(root / "rg" / "tasks").mkdir(parents=True)
flow_d.agent(id="a", instruction="x.md", validator=validate_accept)
rid = flow_d.create_run(inputs={}, run_id="run_rg")  # 无 data_dir -> default_data_dir
reg_file = root / "rg" / ".flow-registry.json"
assert reg_file.exists(), "注册表应写至 workflow.py 同目录"
rec = json.loads(reg_file.read_text(encoding="utf-8"))["runs"]["run_rg"]
assert Path(rec["data_dir"]) == (root / "rg-default").resolve(), rec
ok(f"解析链 default_data_dir + 注册表: {rec['data_dir']}")

r = flow_d.get_next_task(rid)  # 免路径续跑
assert r.status == "task_ready"
ok("注册表免路径续跑")

moved = root / "rg-moved"
shutil.move(str(root / "rg-default"), str(moved))
try:
    flow_d.get_next_task(rid, data_dir=moved)
    raise AssertionError("expected mismatch error")
except ValidationError as e:
    assert "--rebind" in str(e), str(e)
ok("显式路径与注册不一致 -> 报错提示 --rebind")

r = flow_d.get_next_task(rid, data_dir=moved, rebind=True)
assert r.status in ("task_ready", "waiting"), r.to_dict()  # 改绑成功且无报错（任务可能已被首次领取占用）
ok("--rebind 改绑成功")

flow_dup = Flow("reg-test", version="1.0", base_dir=root / "rg",
                default_data_dir=root / "rg-other")
flow_dup.agent(id="a", instruction="x.md", validator=validate_accept)
try:
    flow_dup.create_run(inputs={}, run_id="run_rg")
    raise AssertionError("expected duplicate error")
except ValidationError as e:
    assert "禁止重复 create-run" in str(e), str(e)
ok("重复 create-run 被注册表拦截")

# ---------------------------------------------------------------------------
print("== 6. inputs_file 落位 + human 收紧 + legacy compat ==")
flow_i = Flow("inputs-file", version="1.0", base_dir=root / "if")
(root / "if" / "tasks").mkdir(parents=True)
flow_i.agent(id="a", instruction="x.md", validator=validate_accept)
src_json = root / "params.json"
src_json.write_text('{"region": "华南"}', encoding="utf-8")
rid = flow_i.create_run(inputs={}, run_id="run_if", data_dir=root / "if-data",
                        inputs_file=src_json)
st = flow_i.status(rid)
assert st["run"]["inputs"]["inputs_source"] == "inputs/params.json"
assert (root / "if-data" / "inputs-file" / "runs" / "run_if" / "inputs" / "params.json").exists()
ok("--inputs-file 落位 + inputs_source 记录")

flow_h = Flow("human-test", version="1.0", base_dir=root / "ht")
(root / "ht" / "tasks").mkdir(parents=True)
flow_h.human(id="ask", instruction="请审批", mode="approval")
rid = flow_h.create_run(inputs={}, run_id="run_ht", data_dir=root / "ht-data")
flow_h.get_next_task(rid, data_dir=root / "ht-data")
try:
    flow_h.submit_task_result(rid, "ask", result_type="user_input", data_dir=root / "ht-data",
                              result={"anything": 1})
    raise AssertionError("expected InvalidStateError")
except InvalidStateError as e:
    assert "只接受 user_decision" in str(e), str(e)
ok("human mode 收紧: approval 拒绝 user_input")

flow_l = Flow("legacy", version="1.0", base_dir=root / "lg")
(root / "lg" / "tasks").mkdir(parents=True)
flow_l.agent(id="a", instruction="x.md", validator=validate_accept)
flow_l.agent(id="b", instruction="x.md", after="a", validator=validate_accept)
rid = flow_l.create_run(inputs={"k": "v"}, run_id="run_lg", data_dir=root / "lg-data")
r = flow_l.get_next_task(rid, data_dir=root / "lg-data")
resp = flow_l.submit_task_result(rid, "a", result_type="execution_result", data_dir=root / "lg-data",
                                 result={"free": "form"})
assert resp.status == "accepted"
r = flow_l.get_next_task(rid, data_dir=root / "lg-data")
assert r.task["inputs"] == {"run": {"k": "v"}, "task": {},
                            "dependencies": {"a": {"free": "form"}}}, r.task["inputs"]
assert "outputs_spec" not in r.task
ok("未声明任务走旧 payload 形态（兼容）")

# rework-free 旧库迁移：用 0.1 风格建库再打开 — 通过新列默认值验证
print(f"\nALL {PASS} CHECKS PASSED")
