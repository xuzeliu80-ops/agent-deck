"""Generated fixtures: never touch real job directories or the Claude board."""
import json
import os
from pathlib import Path
import time


def make_job(root, name="job", agent="codex", state="running", now=None, output=None):
    now = now or time.time()
    p = Path(root) / name
    p.mkdir(parents=True, exist_ok=True)
    (p / "meta.json").write_text(json.dumps(dict(agent=agent, model="codex-example" if agent == "codex" else "gemini-example", effort="high", started=now-180, minutes=30, cwd="/tmp")))
    (p / "prompt.txt").write_text("\n【标题】" + name + " 中文任务\n【一句话】验证边界条件并写出检查结果\n请验证边界并写结果。\n", encoding="utf-8")
    (p / "pid").write_text(str(os.getpid() if state != "died" else 99999999))
    (p / "heartbeat").write_text(str(int(now - (180 if state == "died" else 2))))
    if output is None:
        output = codex_output() if agent == "codex" else agy_output()
    (p / "output.txt").write_text(output, encoding="utf-8")
    if state == "stalled":
        os.utime(p / "output.txt", (now-601, now-601))
    if state in ("ok", "fail"):
        (p / "exit_code").write_text("0" if state == "ok" else "7")
        os.utime(p / "exit_code", (now-5, now-5))
    return p


def jsonl(events):
    return "\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n"


def codex_output():
    return jsonl([
        {"type": "item.started", "item": {"id": "i1", "type": "command_execution", "command": "python -m unittest"}},
        {"type": "item.completed", "item": {"id": "i1", "type": "command_execution", "command": "python -m unittest", "status": "completed"}},
        {"type": "item.completed", "item": {"id": "i2", "type": "file_change", "changes": [{"kind": "update", "path": "中文/core.py"}]}},
        {"type": "item.completed", "item": {"id": "i3", "type": "agent_message", "text": "核心解析已完成，正在验证。"}},
        {"type": "turn.completed", "usage": {"input_tokens": 1000, "cached_input_tokens": 800, "output_tokens": 200}},
    ])


def agy_output():
    return jsonl([
        {"event": "init", "init": {"model": "gemini-example"}},
        {"event": "step_update", "step_update": {"step_index": 1, "step_type": "tool", "state": "ACTIVE", "tool_name": "run_command", "tool_info": {"parameters": {"CommandLine": "git diff --check"}}}},
        {"event": "step_update", "step_update": {"step_index": 1, "step_type": "tool", "state": "DONE", "tool_name": "run_command", "tool_info": {"parameters": {"CommandLine": "git diff --check"}}}},
        {"event": "step_update", "step_update": {"step_index": 2, "step_type": "tool", "tool_name": "write_to_file", "tool_info": {"parameters": {"TargetFile": "结果.md"}}}},
        {"event": "step_update", "step_update": {"step_index": 3, "text_delta": "完成"}},
        {"event": "step_update", "step_update": {"step_index": 3, "text_delta": "检查。"}},
        {"event": "result", "result": {"status": "SUCCESS", "response": "检查通过。", "duration_seconds": 150, "usage": {"total_tokens": 3456}}},
    ])


def make_board(path):
    data = {"schema": 1, "updated": "2026-11-12T12:00", "decisions": [
        dict(id="normal", project="配置模块", title="默认超时策略", context="待选择一个默认配置方案。", options=["自动", "手动"], recommend="自动", priority="normal", created="2026-11-11", status="open"),
        dict(id="high", project="验证", title="是否开始下一轮测试", context="静态验证通过。", options=["开始", "继续检查"], recommend="开始", priority="high", created="2026-11-12", status="open"),
        dict(id="old", project="归档", title="过去已拍板事项", context="", options=[], recommend="", priority="low", created="2026-11-10", status="decided")],
        "progress": [dict(project="aw 终端看板", stage="TUI v1 完成", next="自动测试与截图验收", status="running", updated="2026-11-12"),
                     dict(project="后端服务", stage="等待方案选择", next="验证接口契约", status="waiting_user", updated="2026-11-12"),
                     dict(project="历史任务", stage="已完成", next="归档", status="done", updated="2026-11-12")]}
    Path(path).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data


def fixture_store(root):
    from aw.core import Store
    root = Path(root)
    jobs = root / "jobs"
    now = time.time()
    for agent in ("codex", "antigravity"):
        for n, state in enumerate(("running", "stalled", "died", "ok", "fail", "ok", "ok", "ok")):
            p = make_job(jobs, f"{agent}-{n}-{state}", agent, state, now=now-n)
            if state == "stalled":
                command = "/bin/zsh -lc 'python scripts/schema_check.py --strict --report reports/中文测试结果.json\necho finished'"
                output = (jsonl([dict(type="item.started", item=dict(id="cmd", type="command_execution", command=command))]) if agent == "codex" else
                          jsonl([dict(event="step_update", step_update=dict(step_index=1, tool_name="run_command", tool_info=dict(parameters=dict(CommandLine=command))))]))
                (p / "output.txt").write_text(output, encoding="utf-8")
                os.utime(p / "output.txt", (now-601, now-601))
            if state in ("ok", "fail"):
                (p / "prompt.txt").write_text("【标题】接口迁移与回归测试框架的完整结果及后续复测任务\n", encoding="utf-8")
                if agent == "antigravity":
                    output = agy_output().replace(json.dumps("检查通过。", ensure_ascii=False),
                                                  json.dumps("已更新 schema_check.py，正在校验。\n\n合入主干并推进集成回归，再进入端到端基准对照。", ensure_ascii=False))
                    (p / "output.txt").write_text(output, encoding="utf-8")
    board = root / "board.json"
    make_board(board)
    return Store(jobs, board, root / "state.json")
