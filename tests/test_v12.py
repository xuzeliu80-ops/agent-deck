"""v1.2 acceptance: every write targets a generated temporary board."""
import copy
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from aw import core
from aw.pets import DATA
from fixtures import make_board


class V12CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.board = Path(self.tmp.name) / "board.json"
        make_board(self.board)

    def read(self):
        return json.loads(self.board.read_text())

    def high(self):
        return next(d for d in self.read()["decisions"] if d["id"] == "high")

    def test_selected_option_writes_exact_text_and_handshake(self):
        data = self.read()
        data["decisions"][1]["options"][1] = "  继续检查（含原始空格）  "
        self.board.write_text(json.dumps(data))
        choice = data["decisions"][1]["options"][1]
        core.decide(self.board, "high", choice, "  先复核边界  ", option_index=1)
        d = self.high()
        self.assertEqual(d["status"], "decided")
        self.assertEqual(d["choice"], choice)
        self.assertEqual(d["note"], "先复核边界")
        self.assertEqual(d["decided_by"], "user@aw")
        self.assertIs(d["seen_by_claude"], False)
        self.assertIsNotNone(core.datetime.fromisoformat(d["decided_at"]).tzinfo)

    def test_free_answer_blank_note_and_blank_answer_rejected(self):
        before = self.board.read_bytes()
        for blank in ("", " \n "):
            with self.assertRaises(ValueError):
                core.decide(self.board, "high", blank)
            self.assertEqual(self.board.read_bytes(), before)
        core.decide(self.board, "high", "先等外部数据")
        self.assertEqual(self.high()["choice"], "先等外部数据")
        self.assertEqual(self.high()["note"], "")

    def test_changed_option_cannot_save_stale_selection(self):
        data = self.read()
        data["decisions"][1]["options"] = ["更新后的选项"]
        self.board.write_text(json.dumps(data))
        before = self.board.read_bytes()
        for index in (-1, 0, 8):
            with self.assertRaises(ValueError):
                core.decide(self.board, "high", "开始", option_index=index)
            self.assertEqual(self.board.read_bytes(), before)

    def test_undo_removes_answer_metadata_preserves_source(self):
        original = self.high()
        core.decide(self.board, "high", "开始", "可撤销")
        core.undo_decision(self.board, "high")
        restored = self.high()
        self.assertEqual(restored.pop("updated_by"), "user")
        self.assertRegex(restored.pop("updated"), r"^\d{4}-\d\d-\d\dT\d\d:\d\d$")
        self.assertEqual(restored, original)
        self.assertEqual(self.read()["progress"], make_board(Path(self.tmp.name) / "other.json")["progress"])

    def test_undo_invalid_or_failed_write_keeps_board(self):
        before = self.board.read_bytes()
        for key in ("missing", "high"):
            with self.assertRaises(ValueError):
                core.undo_decision(self.board, key)
            self.assertEqual(self.board.read_bytes(), before)
        with patch.object(core.os, "replace", side_effect=OSError("unavailable")):
            with self.assertRaises(OSError):
                core.undo_decision(self.board, "old")
        self.assertEqual(self.board.read_bytes(), before)

    def test_undo_retries_latest_board_preserving_claude_updates(self):
        core.decide(self.board, "high", "开始")
        fsync = core.os.fsync
        changed = []
        def external(fd):
            fsync(fd)
            if not changed:
                data = self.read()
                data["decisions"].append(dict(id="new", status="open", title="Claude 新事项"))
                data["progress"].append(dict(project="新项目"))
                self.board.write_text(json.dumps(data))
                changed.append(True)
        with patch.object(core.os, "fsync", side_effect=external):
            core.undo_decision(self.board, "high")
        self.assertEqual(self.high()["status"], "open")
        self.assertEqual(self.read()["decisions"][-1]["id"], "new")
        self.assertEqual(self.read()["progress"][-1]["project"], "新项目")

    def test_unseen_explicit_false_sorted_and_dump_choice_note(self):
        core.decide(self.board, "high", "开始", "今天执行")
        data = self.read()
        data["decisions"] += [dict(id="read", status="decided", seen_by_claude=True),
                              dict(id="pending", status="open", seen_by_claude=False),
                              dict(id="later", status="decided", seen_by_claude=False, decided_at="2099")]
        self.assertEqual([d["id"] for d in core.decisions_unseen(data["decisions"])], ["later", "high"])
        self.board.write_text(json.dumps(data))
        store = core.Store(Path(self.tmp.name) / "jobs", self.board)
        dump = core.dump_snapshot(store.snapshot())
        self.assertIn("刚拍板（Claude 未读）", dump)
        self.assertIn("选择：开始 · 备注：今天执行", dump)
        self.assertNotIn(DATA["frames"]["fox"]["busy"][0][0], dump)

    def test_unseen_cli_board_only_no_textual_and_no_writes(self):
        core.decide(self.board, "high", "开始", "立即执行")
        before = self.board.read_bytes()
        script = '''import importlib.abc,sys,runpy
class Block(importlib.abc.MetaPathFinder):
 def find_spec(self, fullname, path=None, target=None):
  if fullname.startswith("textual"): raise RuntimeError("textual imported")
sys.meta_path.insert(0,Block())
sys.argv=["aw"]+sys.argv[1:]
runpy.run_module("aw",run_name="__main__")
'''
        run = subprocess.run([sys.executable, "-c", script, "--decisions-unseen", "--board", str(self.board), "--jobs", "/no-such-jobs"], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        result = json.loads(run.stdout)
        self.assertEqual([d["id"] for d in result], ["high"])
        self.assertEqual(result[0]["choice"], "开始")
        self.assertEqual(result[0]["note"], "立即执行")
        self.assertEqual(self.board.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.board.parent.iterdir()), ["board.json"])

    def test_unseen_cli_bad_board_returns_error_not_empty_success(self):
        self.board.write_text("{")
        run = subprocess.run([sys.executable, "-m", "aw", "--decisions-unseen", "--board", str(self.board)], capture_output=True, text=True)
        self.assertEqual(run.returncode, 1)
        self.assertEqual(run.stdout, "")
        self.assertIn("读取失败", run.stderr)


class PetTests(unittest.TestCase):
    def snapshot(self, *jobs, decisions=None, progress=None):
        return dict(now=100, jobs=list(jobs), decisions=decisions or [], progress=progress or [])

    def job(self, state, agent="codex", ended=1, archived=False):
        return dict(state=state, agent=agent, ended=ended, started=-3000, archived=archived)

    def test_every_frame_original_data_shape_and_size(self):
        for pet, states in DATA["frames"].items():
            self.assertEqual(set(states), {"error", "busy", "idle", "happy"})
            for state, frames in states.items():
                self.assertEqual(len(frames), 4 if state == "busy" else 1)
                for frame in frames:
                    self.assertEqual(len(frame), 12)
                    self.assertTrue(all(len(row) == 14 for row in frame))
                    self.assertTrue(set("".join(frame)) <= set(DATA["palette"]) | {"."})
                if state == "busy":
                    self.assertNotEqual(frames[0], frames[3])

    def test_agent_states_and_priority_per_column(self):
        for agent in ("codex", "antigravity"):
            with self.subTest(agent=agent):
                running = self.job("running", agent)
                ok = self.job("ok", agent)
                fail = self.job("fail", agent)
                self.assertEqual(core.pet_status(self.snapshot(), agent)[0], "idle")
                self.assertEqual(core.pet_status(self.snapshot(ok), agent)[0], "happy")
                self.assertEqual(core.pet_status(self.snapshot(ok, running), agent)[0], "busy")
                self.assertEqual(core.pet_status(self.snapshot(ok, running, fail), agent)[0], "error")
                for state in ("stalled", "died"):
                    self.assertEqual(core.pet_status(self.snapshot(self.job(state, agent)), agent)[0], "error")
                self.assertEqual(core.pet_status(self.snapshot(self.job("fail", agent, ended=-2000)), agent)[0], "idle")
                self.assertEqual(core.pet_status(self.snapshot(running), agent)[2], "运行 1")
                other = "codex" if agent == "antigravity" else "antigravity"
                self.assertEqual(core.pet_status(self.snapshot(fail), other)[0], "idle")

    def test_new_endings_drive_pet_even_beyond_recent_two(self):
        jobs = [self.job("ok", ended=i) for i in (3, 4, 5)]
        jobs.append(self.job("fail", ended=1))
        self.assertEqual(core.pet_status(self.snapshot(*jobs), "codex"),
                         ("error", "出事", "运行 0 · 新完成 4"))
        for job in jobs:
            job["ended"] = -2000
        self.assertEqual(core.pet_status(self.snapshot(*jobs), "codex"),
                         ("idle", "空闲", "运行 0"))

    def test_claude_waiting_priority_and_unseen_ack(self):
        unseen = dict(status="decided", seen_by_claude=False)
        opened = dict(status="open")
        data = self.snapshot(decisions=[unseen])
        self.assertEqual(core.pet_status(data, "claude"), ("happy", "刚完成", "待拍板 0 · 新拍板 1"))
        data["decisions"].append(opened)
        self.assertEqual(core.pet_status(data, "claude"), ("busy", "等你", "待拍板 1 · 新拍板 1"))
        data["progress"] = [dict(status="blocked")]
        self.assertEqual(core.pet_status(data, "claude")[0], "error")
        data["decisions"] = []
        data["progress"] = [dict(status="running")]
        self.assertEqual(core.pet_status(data, "claude")[:2], ("busy", "忙碌"))
        data["progress"] = []
        data["decisions"] = [dict(status="decided", seen_by_claude=True), dict(status="decided")]
        self.assertEqual(core.pet_status(data, "claude")[0], "idle")
