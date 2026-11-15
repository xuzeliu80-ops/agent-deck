import copy
import importlib.abc
import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from aw import core
from fixtures import agy_output, codex_output, fixture_store, jsonl, make_board, make_job


def mark_from_process(board, key):
    core.decide(board, key, "测试答案", "并发验证")


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.now = time.time()

    def test_all_job_states(self):
        store = core.Store(self.root)
        for state in ("running", "stalled", "died", "ok", "fail"):
            with self.subTest(state=state):
                p = make_job(self.root, state, state=state, now=self.now)
                self.assertEqual(store.job_info(p, self.now)["state"], state)

    def test_dead_pid_with_fresh_heartbeat_keeps_running(self):
        p = make_job(self.root)
        (p / "pid").write_text("99999999")
        self.assertEqual(core.Store().job_info(p, self.now)["state"], "running")

    def test_dead_pid_missing_heartbeat_is_died(self):
        p = make_job(self.root, state="died")
        (p / "heartbeat").unlink()
        self.assertEqual(core.Store().job_info(p, self.now)["state"], "died")

    def test_died_six_hour_boundary_and_elapsed_frozen(self):
        p = make_job(self.root, agent="antigravity", state="died", now=self.now)
        last = int(self.now) - 1000
        meta = json.loads((p / "meta.json").read_text())
        meta["started"] = last - 300
        (p / "meta.json").write_text(json.dumps(meta))
        (p / "heartbeat").write_text(str(last))
        os.utime(p / "output.txt", (last-10, last-10))
        store = core.Store()
        boundary = store.job_info(p, last + 21600)
        self.assertTrue(core.is_active(boundary))
        self.assertEqual(boundary["elapsed_min"], 5)  # Ignores agy result duration.
        archived = store.job_info(p, last + 21601)
        self.assertEqual(archived["state"], "died")
        self.assertFalse(core.is_active(archived))
        self.assertEqual(archived["ended"], last)
        self.assertEqual(archived["elapsed_min"], boundary["elapsed_min"])
        self.assertEqual(store.job_info(p, last + 99999)["elapsed_min"], 5)

    def test_died_last_activity_uses_newer_heartbeat_or_output(self):
        p = make_job(self.root, state="died", now=self.now)
        for hb_age, out_age, archived in ((22000, 1000, False), (1000, 22000, False), (22000, 23000, True)):
            with self.subTest(hb_age=hb_age, out_age=out_age):
                (p / "heartbeat").write_text(str(int(self.now-hb_age)))
                os.utime(p / "output.txt", (self.now-out_age, self.now-out_age))
                j = core.Store().job_info(p, self.now)
                self.assertEqual(j["archived"], archived)
                self.assertAlmostEqual(j["last_activity"], max(int(self.now-hb_age), self.now-out_age), places=5)
        (p / "heartbeat").unlink()
        (p / "output.txt").unlink()
        j = core.Store().job_info(p, self.now+24000)
        self.assertTrue(j["archived"])
        self.assertEqual(j["elapsed_min"], 0)

    def test_archived_died_is_sorted_with_recent_ends_and_history(self):
        store = fixture_store(self.root)
        p = make_job(store.jobs, "old-died", state="died", now=self.now-30000)
        os.utime(p / "output.txt", (self.now-30000, self.now-30000))
        data = store.snapshot(self.now)
        visible = core.visible_jobs(data, "codex")
        self.assertNotIn("old-died", [j["id"] for j in visible])
        history = core.visible_jobs(data, "codex", True)
        self.assertEqual(history[-1]["id"], "old-died")
        archived = next(j for j in history if j["id"] == "old-died")
        fresh_done = [j for j in data["jobs"] if j["agent"] == "codex" and not core.is_active(j) and j["id"] != "old-died"]
        for j in fresh_done:
            data["jobs"].remove(j)
        self.assertIn(archived, core.visible_jobs(data, "codex"))
        self.assertIn("失联 old-died", core.dump_snapshot(data))

    def test_titles(self):
        cases = [("\n【标题】你好\nother", "你好"), ("# 标题\nother", "标题"),
                 ("执行 tools/任务卡_解析.md。", "任务卡_解析.md"), ("长"*50, "长"*30), ("", "未命名任务")]
        for prompt, title in cases:
            self.assertEqual(core.title_from_prompt(prompt), title)

    def test_codex_steps_deduplicated_and_tokens(self):
        p = core.parse_output(codex_output(), "codex")
        self.assertEqual(len(p["steps"]), 3)
        self.assertIn("命令：python", p["steps"][0])
        self.assertIn("中文/core.py", p["steps"][1])
        self.assertIn("核心解析", p["result"])
        self.assertEqual(p["tokens"], 1200)

    def test_agy_steps_deltas_and_result(self):
        p = core.parse_output(agy_output(), "antigravity")
        self.assertEqual(len(p["steps"]), 4)
        self.assertIn("git diff", p["steps"][0])
        self.assertIn("结果.md", p["steps"][1])
        self.assertEqual(p["steps"][2], "回复：完成检查。")
        self.assertEqual(p["tokens"], 3456)
        self.assertEqual(p["duration_seconds"], 150)
        self.assertEqual(p["result"], "检查通过。")

    def test_agy_legacy_json(self):
        p = core.parse_output(json.dumps(dict(response="中断结果", duration_seconds=3, usage=dict(total_tokens=15))), "antigravity")
        self.assertEqual(p["result"], "中断结果")
        self.assertEqual(p["tokens"], 15)

    def test_agy_actual_path_parameter(self):
        output = jsonl([dict(event="step_update", step_update=dict(step_index=1, tool_name="replace_file_content", tool_info=dict(parameters=dict(Path="/tmp/a.py"))))])
        self.assertEqual(core.parse_output(output, "antigravity")["steps"], ["文件：/tmp/a.py"])

    def test_command_preview_unwraps_and_keeps_full_detail_steps(self):
        import shlex
        command = '/bin/zsh -lc ' + shlex.quote('python check.py --name "中文"\nsecond command')
        output = jsonl([dict(type="item.completed", item=dict(id="cmd", type="command_execution", command=command))])
        parsed = core.parse_output(output, "codex")
        self.assertEqual(parsed["steps"], ["命令：" + command])
        self.assertEqual(core.latest_step(parsed["steps"]), "运行命令")
        self.assertEqual(core.command_preview("bash -c 'zsh -lc \"echo hello\"'"), "echo hello")
        self.assertEqual(core.latest_step(["命令：echo " + "中"*300]), "运行命令")
        self.assertNotIn("\n", core.latest_step(["命令：echo first\nsecond"]))

    def test_prefer_first_sentence_in_last_three_steps_both_agents(self):
        for agent in ("codex", "antigravity"):
            with self.subTest(agent=agent):
                if agent == "codex":
                    events = [dict(type="item.completed", item=dict(id="msg", type="agent_message", text="正在校验。\n最终检查通过！"))]
                    events += [dict(type="item.completed", item=dict(id=str(i), type="command_execution", command=f"echo {i}")) for i in range(3)]
                else:
                    events = [dict(event="step_update", step_update=dict(step_index=0, step_type="agent_response", text_delta="正在校验。\n")),
                              dict(event="step_update", step_update=dict(step_index=0, step_type="agent_response", text_delta="最终检查通过！"))]
                    events += [dict(event="step_update", step_update=dict(step_index=i+1, tool_name="run_command", tool_info=dict(parameters=dict(CommandLine=f"echo {i}")))) for i in range(3)]
                steps = core.parse_output(jsonl(events[:-1]), agent)["steps"]
                self.assertEqual(core.latest_step(steps), "正在校验。")
                self.assertEqual(core.latest_step(core.parse_output(jsonl(events), agent)["steps"]), "运行命令")

    def test_agy_summary_uses_last_nonempty_paragraph(self):
        response = "已更新 schema_check.py，正在校验。\n\n" + "\n".join(f"中间步骤 {i}" for i in range(70)) + "\n\n1. 确认默认配置策略。\n2. 合入主干并推进集成回归：再进入端到端基准对照。\n\n  \n"
        output = jsonl([dict(event="result", result=dict(response=response))])
        p = make_job(self.root, agent="antigravity", state="ok", output=output)
        j = core.Store().job_info(p, self.now)
        self.assertEqual(j["result"], response)
        self.assertEqual(j["result_summary"], "合入主干并推进集成回归：再进入端到端基准对照。")
        self.assertNotIn("已更新", j["steps"][-1])

    def test_malformed_and_truncated_events(self):
        parsed = core.parse_output('broken\n[1,2]\nnull\n' + codex_output() + '{"type":', "codex")
        self.assertEqual(parsed["tokens"], 1200)

    def test_fifteen_steps(self):
        output = jsonl([dict(type="item.completed", item=dict(id=str(i), type="agent_message", text=str(i))) for i in range(40)])
        p = core.parse_output(output, "codex")
        self.assertEqual(len(p["steps"]), 15)
        self.assertEqual(p["steps"][0], "回复：25")

    def test_tail_bound_and_partial_first_line(self):
        p = self.root / "large.txt"
        p.write_text("x"*300000 + "\n" + codex_output())
        tail = core.read_tail(p)
        self.assertLessEqual(len(tail.encode()), core.TAIL_BYTES)
        self.assertNotIn("xxxxx", tail)
        self.assertEqual(core.parse_output(tail, "codex")["tokens"], 1200)

    def test_prompt_forty_lines_and_last_message(self):
        p = make_job(self.root, state="ok")
        (p / "prompt.txt").write_text("\n".join(str(i) for i in range(80)))
        (p / "last_message.txt").write_text("最终消息")
        j = core.Store().job_info(p, self.now)
        self.assertEqual(len(j["prompt"].splitlines()), 40)
        self.assertEqual(j["result"], "最终消息")
        self.assertEqual(j["result_summary"], "最终消息")

    def test_finished_cache_and_mtime_invalidation(self):
        p = make_job(self.root, state="ok")
        store = core.Store()
        with patch.object(core, "parse_output", wraps=core.parse_output) as parser:
            store.job_info(p, self.now)
            store.job_info(p, self.now)
            self.assertEqual(parser.call_count, 1)
            with (p / "output.txt").open("a") as f:
                f.write(codex_output())
            store.job_info(p, self.now)
            self.assertEqual(parser.call_count, 2)

    def test_running_not_cached(self):
        p = make_job(self.root)
        store = core.Store()
        with patch.object(core, "parse_output", wraps=core.parse_output) as parser:
            store.job_info(p, self.now)
            store.job_info(p, self.now)
            self.assertEqual(parser.call_count, 2)

    def test_board_missing_broken_and_wrong_shape(self):
        board = self.root / "board.json"
        for text in (None, "{", "[]", '{"schema":1,"decisions":[null],"progress":[]}'):
            if text is not None:
                board.write_text(text)
            data, error = core.load_board(board)
            self.assertIsNotNone(error)
            self.assertEqual(data["decisions"], [])

    def test_snapshot_order_counts_history_without_ack(self):
        store = fixture_store(self.root)
        data = store.snapshot()
        self.assertEqual(data["counts"], dict(codex=2, antigravity=2, decisions=2))
        self.assertEqual(data["decisions"][0]["id"], "high")
        self.assertEqual(len(core.visible_jobs(data, "codex")), 5)
        self.assertEqual(len(core.visible_jobs(data, "codex", True)), 8)
        self.assertTrue(all("acked" not in j for j in data["jobs"]))
        self.assertFalse(store.state.exists())

    def test_missing_jobs_and_board_are_reported(self):
        data = core.Store(self.root / "missing", self.root / "none", self.root / "s").snapshot()
        self.assertEqual(len(data["errors"]), 2)
        self.assertIn("读取失败", core.dump_snapshot(data))

    def test_decide_preserves_new_entries_and_unrelated_fields(self):
        p = self.root / "board.json"
        data = make_board(p)
        data["extra"] = dict(unknown="preserve")
        data["decisions"].append(dict(id="new", status="open"))
        p.write_text(json.dumps(data))
        core.decide(p, "high", "继续检查", "先做检查")
        result = json.loads(p.read_text())
        self.assertEqual(result["decisions"][1]["status"], "decided")
        self.assertIn("decided_at", result["decisions"][1])
        self.assertEqual(result["decisions"][1]["note"], "先做检查")
        self.assertEqual(result["decisions"][-1]["id"], "new")
        self.assertEqual(result["extra"], data["extra"])
        self.assertEqual(result["progress"], data["progress"])
        self.assertEqual(result["updated"], data["updated"])
        self.assertEqual(list(self.root.glob(".aw-*")), [])

    def test_external_update_during_temp_write_retried(self):
        p = self.root / "board.json"
        make_board(p)
        real_fsync = core.os.fsync
        changed = []
        def update_during_write(fd):
            real_fsync(fd)
            if not changed:
                data = json.loads(p.read_text())
                data["decisions"].append(dict(id="concurrent", title="Claude 新条目", status="open"))
                data["progress"].append(dict(project="并发新项目"))
                p.write_text(json.dumps(data))
                changed.append(True)
        with patch.object(core.os, "fsync", side_effect=update_during_write):
            core.decide(p, "high", "开始")
        data = json.loads(p.read_text())
        self.assertEqual(data["decisions"][-1]["id"], "concurrent")
        self.assertEqual(data["progress"][-1]["project"], "并发新项目")
        self.assertEqual(data["decisions"][1]["status"], "decided")

    def test_two_process_writers_keep_both_decisions(self):
        p = self.root / "board.json"
        make_board(p)
        ctx = multiprocessing.get_context("spawn")
        processes = [ctx.Process(target=mark_from_process, args=(str(p), key)) for key in ("normal", "high")]
        for process in processes:
            process.start()
        for process in processes:
            process.join(10)
            self.assertEqual(process.exitcode, 0)
        self.assertTrue(all(x["status"] == "decided" for x in json.loads(p.read_text())["decisions"]))

    def test_invalid_decision_does_not_write(self):
        p = self.root / "board.json"
        make_board(p)
        original = p.read_bytes()
        for key in ("missing", "old"):
            with self.assertRaises(ValueError):
                core.decide(p, key, "测试答案")
            self.assertEqual(p.read_bytes(), original)

    def test_atomic_failure_keeps_original(self):
        p = self.root / "board.json"
        make_board(p)
        original = p.read_bytes()
        with patch.object(core.os, "replace", side_effect=OSError("write failed")):
            with self.assertRaises(OSError):
                core.decide(p, "high", "开始")
        self.assertEqual(p.read_bytes(), original)
        self.assertEqual(list(self.root.glob(".aw-*")), [])

    def test_notifications_no_startup_or_duplicates(self):
        store = fixture_store(self.root)
        data = store.snapshot()
        changes = core.Changes()
        self.assertEqual(changes.poll(data), [])
        changed = copy.deepcopy(data)
        job = next(j for j in changed["jobs"] if j["state"] == "running")
        job["state"] = "ok"
        changed["decisions"].append(dict(id="new", title="新问题", status="open"))
        self.assertEqual(len(changes.poll(changed)), 2)
        self.assertEqual(changes.poll(changed), [])

    def test_notifications_no_replay_after_temporary_disappearance(self):
        data = fixture_store(self.root).snapshot()
        changes = core.Changes()
        self.assertEqual(changes.poll(data), [])
        changes.poll(dict(jobs=[], decisions=[]))
        self.assertEqual(changes.poll(data), [])

    def test_busy_board_aborts_without_overwriting_external_updates(self):
        p = self.root / "board.json"
        make_board(p)
        sync = core.os.fsync
        counter = []
        def continuous_write(fd):
            sync(fd)
            data = json.loads(p.read_text())
            counter.append(True)
            data["external_revision"] = len(counter)
            p.write_text(json.dumps(data))
        with patch.object(core.os, "fsync", side_effect=continuous_write):
            with self.assertRaises(RuntimeError):
                core.decide(p, "high", "开始")
        self.assertEqual(json.loads(p.read_text())["decisions"][1]["status"], "open")
        self.assertEqual(list(self.root.glob(".aw-*")), [])

    def test_cli_without_textual_and_no_writes(self):
        store = fixture_store(self.root)
        script = '''import importlib.abc,sys,runpy
class Block(importlib.abc.MetaPathFinder):
 def find_spec(self, fullname, path=None, target=None):
  if fullname.startswith("textual"): raise RuntimeError("textual must not be imported")
sys.meta_path.insert(0,Block())
sys.argv=["aw"]+sys.argv[1:]
runpy.run_module("aw",run_name="__main__")
'''
        for mode in ("--dump", "--json", "--decisions-unseen"):
            run = subprocess.run([sys.executable, "-c", script, mode, "--jobs", str(store.jobs), "--board", str(store.board)], capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertIn("Claude" if mode == "--dump" else ('"counts"' if mode == "--json" else "[]"), run.stdout)
        self.assertFalse(store.state.exists())

    def test_ninety_six_jobs_refresh_budget(self):
        jobs = self.root / "jobs"
        for i in range(96):
            make_job(jobs, str(i), state="ok")
        board = self.root / "board.json"
        make_board(board)
        store = core.Store(jobs, board, self.root / "state")
        store.snapshot()
        start = time.perf_counter()
        self.assertEqual(len(store.snapshot()["jobs"]), 96)
        self.assertLess(time.perf_counter()-start, 0.3)


if __name__ == "__main__":
    unittest.main()
