"""User operations use isolated boards/jobs; source logs must remain byte-exact."""
import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from textual.widgets import Input, ListView, Static

from aw import core
from aw.tui import CloseDecision, Confirm, Detail, EditProgress, WatchApp
from fixtures import fixture_store, make_job
from mouse_helpers import global_action, menu_action


def parallel_update(board, operation):
    if operation == "progress":
        core.update_progress(board, "后端服务", {"status": "done"})
    else:
        core.archive_progress(board, "历史任务")


class UserDataTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = fixture_store(self.root)

    def board(self):
        return json.loads(self.store.board.read_text())

    def job(self, key="codex-3-ok"):
        return next(j for j in self.store.snapshot()["jobs"] if j["id"] == key)

    def stamp(self, entry):
        self.assertEqual(entry["updated_by"], "user")
        self.assertRegex(entry["updated"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d$")

    def test_status_stage_next_preserve_other_entries_and_unknown_fields(self):
        original = self.board()
        original["progress"][1]["extra"] = {"keep": True}
        self.store.board.write_text(json.dumps(original))
        for status in core.PROGRESS_STATUSES:
            core.update_progress(self.store.board, "后端服务", dict(status=status, stage="已定", next="继续"))
            result = self.board()
            entry = result["progress"][1]
            self.assertEqual((entry["status"], entry["stage"], entry["next"]), (status, "已定", "继续"))
            self.assertEqual(entry["extra"], {"keep": True})
            self.assertEqual(result["progress"][0], original["progress"][0])
            self.assertEqual(result["decisions"], original["decisions"])
            self.stamp(entry)

    def test_progress_rejects_invented_status_duplicate_and_missing_project(self):
        before = self.store.board.read_bytes()
        for project, fields in (("后端服务", {"status": "invented"}), ("missing", {"stage": "x"}), ("后端服务", {"project": "x"})):
            with self.assertRaises(ValueError):
                core.update_progress(self.store.board, project, fields)
            self.assertEqual(self.store.board.read_bytes(), before)
        data = self.board()
        data["progress"].append(data["progress"][1].copy())
        self.store.board.write_text(json.dumps(data))
        with self.assertRaises(ValueError):
            core.archive_progress(self.store.board, "后端服务")

    def test_existing_custom_status_remains_usable(self):
        data = self.board()
        data["progress"][0]["status"] = "paused_external"
        self.store.board.write_text(json.dumps(data))
        core.update_progress(self.store.board, "后端服务", {"status": "paused_external"})
        self.assertEqual(self.board()["progress"][1]["status"], "paused_external")

    def test_archive_restore_keeps_full_record_and_does_not_change_decisions(self):
        original = self.board()
        core.archive_progress(self.store.board, "后端服务")
        data = self.board()
        self.assertNotIn("后端服务", [p["project"] for p in data["progress"]])
        archived = data["archived"][0]
        for key in ("project", "stage", "next", "status"):
            self.assertEqual(archived[key], original["progress"][1][key])
        self.stamp(archived)
        self.assertEqual(self.store.snapshot()["archived"], data["archived"])
        core.archive_progress(self.store.board, "后端服务", restore=True)
        self.assertEqual(self.board()["archived"], [])
        self.assertEqual(self.board()["decisions"], original["decisions"])
        self.stamp(self.board()["progress"][-1])

    def test_restore_collision_and_bad_archive_do_not_overwrite(self):
        data = self.board()
        data["archived"] = [data["progress"][1].copy()]
        self.store.board.write_text(json.dumps(data))
        before = self.store.board.read_bytes()
        with self.assertRaises(ValueError):
            core.archive_progress(self.store.board, "后端服务", restore=True)
        self.assertEqual(self.store.board.read_bytes(), before)
        data["archived"] = {"wrong": "shape"}
        self.store.board.write_text(json.dumps(data))
        self.assertTrue(self.store.snapshot()["errors"])
        with self.assertRaises(ValueError):
            core.archive_progress(self.store.board, "后端服务")

    def test_progress_retry_merges_concurrent_same_entry_stage_and_new_records(self):
        fsync = core.os.fsync
        calls = []
        def external_write(fd):
            fsync(fd)
            if not calls:
                data = self.board()
                data["progress"][1]["stage"] = "Claude 刚更新"
                data["progress"].append({"project": "新增", "status": "running"})
                self.store.board.write_text(json.dumps(data))
                calls.append(True)
        with patch.object(core.os, "fsync", side_effect=external_write):
            core.update_progress(self.store.board, "后端服务", {"status": "done"})
        result = self.board()
        self.assertEqual(result["progress"][1]["stage"], "Claude 刚更新")
        self.assertEqual(result["progress"][1]["status"], "done")
        self.assertEqual(result["progress"][-1]["project"], "新增")

    def test_archive_retry_keeps_latest_target_content(self):
        fsync = core.os.fsync
        calls = []
        def external_write(fd):
            fsync(fd)
            if not calls:
                data = self.board()
                data["progress"][1]["next"] = "Claude 最新下一步"
                self.store.board.write_text(json.dumps(data))
                calls.append(True)
        with patch.object(core.os, "fsync", side_effect=external_write):
            core.archive_progress(self.store.board, "后端服务")
        self.assertEqual(self.board()["archived"][0]["next"], "Claude 最新下一步")

    def test_concurrent_processes_keep_progress_and_archive(self):
        ctx = multiprocessing.get_context("spawn")
        processes = [ctx.Process(target=parallel_update, args=(str(self.store.board), op)) for op in ("progress", "archive")]
        for p in processes:
            p.start()
        for p in processes:
            p.join(10)
            self.assertEqual(p.exitcode, 0)
        self.assertEqual(self.board()["progress"][1]["status"], "done")
        self.assertEqual(self.board()["archived"][0]["project"], "历史任务")

    def test_archive_failed_rename_preserves_original_and_cleans_temps(self):
        before = self.store.board.read_bytes()
        with patch.object(core.os, "replace", side_effect=OSError("test failure")):
            with self.assertRaises(OSError):
                core.archive_progress(self.store.board, "后端服务")
        self.assertEqual(self.store.board.read_bytes(), before)
        self.assertEqual(list(self.root.glob(".aw-*.tmp")), [])

    def test_close_handshake_and_all_decision_user_edits_are_stamped(self):
        for key, choice, note in (("high", "关闭", "不再需要"), ("normal", "已处理", "")):
            core.decide(self.store.board, key, choice, note)
            entry = next(d for d in self.board()["decisions"] if d["id"] == key)
            self.assertEqual((entry["status"], entry["choice"], entry["note"]), ("decided", choice, note))
            self.assertIs(entry["seen_by_claude"], False)
            self.stamp(entry)
        core.undo_decision(self.store.board, "high")
        self.stamp(next(d for d in self.board()["decisions"] if d["id"] == "high"))

    def test_new_read_persists_reopen_and_new_ending_is_unread(self):
        job = self.job()
        self.assertTrue(core.is_new(job))
        self.store.mark_read(job)
        reopened = core.Store(self.store.jobs, self.store.board)
        current = next(j for j in reopened.snapshot()["jobs"] if j["id"] == job["id"])
        self.assertFalse(core.is_new(current))
        self.stamp(json.loads(self.store.user_state.read_text())["jobs"][self.store.job_key(job)])
        end = self.store.jobs / job["id"] / "exit_code"
        os.utime(end, (time.time(), time.time()))
        self.assertTrue(core.is_new(self.job()))

    def test_read_and_hide_never_modify_board_legacy_or_job_files(self):
        files = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        ids = [j["id"] for j in self.store.snapshot()["jobs"] if j["state"] in core.TERMINAL]
        self.store.mark_read(self.job())
        self.store.hide_jobs(ids)
        snap = self.store.snapshot()
        for agent in ("codex", "antigravity"):
            self.assertTrue(all(j["state"] not in core.TERMINAL for j in core.visible_jobs(snap, agent, True)))
            self.assertTrue(all(j["hidden"] for j in core.visible_jobs(snap, agent, hidden=True)))
        self.store.hide_jobs(ids, restore=True)
        self.assertTrue(all(not j["hidden"] for j in self.store.snapshot()["jobs"]))
        for p, before in files.items():
            self.assertEqual(p.read_bytes(), before)
        self.assertFalse(self.store.state.exists())

    def test_hide_rechecks_running_and_batch_fails_without_partial_write(self):
        with self.assertRaises(ValueError):
            self.store.hide_jobs(["codex-3-ok", "codex-0-running"])
        self.assertFalse(self.store.user_state.exists())
        with self.assertRaises(ValueError):
            self.store.hide_jobs(["../jobs"])

    def test_running_cannot_be_read_and_corrupt_state_is_not_overwritten(self):
        self.store.mark_read(self.job("codex-0-running"))
        self.assertFalse(self.store.user_state.exists())
        self.store.user_state.write_text("{")
        with self.assertRaises(ValueError):
            self.store.mark_read(self.job())
        self.assertEqual(self.store.user_state.read_text(), "{")
        self.assertTrue(any("用户状态" in e for e in self.store.snapshot()["errors"]))

    def test_job_identity_includes_source_root(self):
        self.store.mark_read(self.job())
        other_jobs = self.root / "other-jobs"
        make_job(other_jobs, "codex-3-ok", state="ok")
        other = core.Store(other_jobs, self.store.board)
        self.assertTrue(core.is_new(other.snapshot()["jobs"][0]))


class UserTuiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = fixture_store(Path(self.tmp.name))

    def select(self, app, kind, key, pane=0):
        app.pane_index = pane
        view = app.query_one(f"#list-{pane}", ListView)
        view.focus()
        view.index = next(i for i, row in enumerate(view.children) if row.kind == kind and row.key == key)
        return view.highlighted_child

    def board(self):
        return json.loads(self.store.board.read_text())

    async def test_edit_status_stage_and_unedited_next_survives_claude_change(self):
        app = WatchApp(self.store, no_notify=True, no_anim=True)
        async with app.run_test(size=(100, 32)) as pilot:
            row = self.select(app, "progress", "后端服务")
            await menu_action(pilot, row, "edit")
            self.assertIsInstance(app.screen, EditProgress)
            data = self.board()
            data["progress"][1]["next"] = "Claude 新下一步"
            self.store.board.write_text(json.dumps(data))
            await pilot.press("1")
            app.screen.query_one("#stage-input", Input).value = "阶段二：灰度验证"
            await pilot.press("enter")
            await pilot.pause()
            p = self.board()["progress"][1]
            self.assertEqual((p["status"], p["stage"], p["next"]), ("running", "阶段二：灰度验证", "Claude 新下一步"))

    async def test_progress_archive_confirm_cancel_restore_both_widths(self):
        for width in (70, 160):
            app = WatchApp(self.store, no_notify=True, no_anim=True)
            async with app.run_test(size=(width, 32)) as pilot:
                row = self.select(app, "progress", "后端服务")
                before = self.store.board.read_bytes()
                await menu_action(pilot, row, "archive")
                await pilot.click("#confirm-no")
                self.assertEqual(self.store.board.read_bytes(), before)
                await menu_action(pilot, row, "archive")
                self.assertIsInstance(app.screen, Confirm)
                await pilot.click("#confirm-yes")
                await pilot.pause()
                self.assertNotIn("后端服务", [p["project"] for p in app.snapshot_data["progress"]])
                await global_action(app, pilot, "archives")
                self.select(app, "progress", "后端服务")
                await pilot.press("enter")
                self.assertIsInstance(app.screen, Detail)
                await pilot.press("escape")
                await menu_action(pilot, app.selected(), "restore")
                await pilot.click("#confirm-yes")
                await pilot.pause()
                self.assertEqual(self.board()["archived"], [])
                await global_action(app, pilot, "archives")
                self.assertIn("后端服务", [p["project"] for p in app.snapshot_data["progress"]])

    async def test_close_with_optional_note_and_cancel(self):
        app = WatchApp(self.store, no_notify=True, no_anim=True)
        async with app.run_test(size=(100, 32)) as pilot:
            row = self.select(app, "decision", "high")
            before = self.store.board.read_bytes()
            await menu_action(pilot, row, "close")
            await pilot.press("escape")
            self.assertEqual(self.store.board.read_bytes(), before)
            await menu_action(pilot, row, "close")
            self.assertIsInstance(app.screen, CloseDecision)
            await pilot.click("#close-closed")
            app.screen.query_one("#note-input", Input).value = "用户已处理"
            await pilot.click("#save-note")
            await pilot.pause()
            d = next(d for d in self.board()["decisions"] if d["id"] == "high")
            self.assertEqual((d["choice"], d["note"], d["seen_by_claude"]), ("关闭", "用户已处理", False))

    async def test_highlight_reads_immediately_persists_and_updates_header(self):
        app = WatchApp(self.store, no_notify=True, no_anim=True)
        async with app.run_test(size=(160, 45)) as pilot:
            # Background task cards are not automatically read on startup.
            self.assertFalse(self.store.user_state.exists())
            await pilot.press("right", "h")
            row = self.select(app, "job", "codex-3-ok", 1)
            await pilot.pause()
            self.assertNotIn("NEW", row.card_text.plain)
            self.assertTrue(row.data["read"])
            await app.refresh_data()
            self.assertFalse(core.is_new(next(j for j in app.snapshot_data["jobs"] if j["id"] == row.key)))
            self.assertTrue(self.store.user_state.exists())
        reopened = WatchApp(core.Store(self.store.jobs, self.store.board), no_notify=True, no_anim=True)
        async with reopened.run_test(size=(160, 45)) as pilot:
            await pilot.press("right", "h")
            row = next(r for r in reopened.query_one("#list-1").children if r.key == "codex-3-ok")
            self.assertNotIn("NEW", row.card_text.plain)

    async def test_job_hide_restore_and_clear_all_with_confirmation(self):
        app = WatchApp(self.store, no_notify=True, no_anim=True)
        async with app.run_test(size=(160, 45)) as pilot:
            await pilot.press("right", "h")
            row = self.select(app, "job", "codex-3-ok", 1)
            await menu_action(pilot, row, "hide")
            await pilot.click("#confirm-yes")
            await pilot.pause()
            self.assertNotIn("codex-3-ok", [r.key for r in app.query_one("#list-1").children])
            await global_action(app, pilot, "archives")
            row = self.select(app, "job", "codex-3-ok", 1)
            await menu_action(pilot, row, "restore")
            await pilot.click("#confirm-yes")
            await pilot.pause()
            await global_action(app, pilot, "archives")
            await global_action(app, pilot, "clear")
            await pilot.click("#confirm-no")
            self.assertTrue(any(not j["hidden"] and j["state"] in core.TERMINAL for j in self.store.snapshot()["jobs"]))
            await global_action(app, pilot, "clear")
            self.assertIsInstance(app.screen, Confirm)
            await pilot.click("#confirm-yes")
            await pilot.pause()
            for pane in (1, 2):
                rows = [r for r in app.query_one(f"#list-{pane}").children if r.kind == "job"]
                self.assertTrue(rows)
                self.assertTrue(all(r.data["state"] in ("running", "stalled") for r in rows))

    async def test_changed_job_after_confirmation_reports_error_without_hiding(self):
        app = WatchApp(self.store, no_notify=True, no_anim=True)
        async with app.run_test(size=(160, 45)) as pilot:
            await pilot.press("right", "h")
            row = self.select(app, "job", "codex-3-ok", 1)
            await menu_action(pilot, row, "hide")
            (self.store.jobs / "codex-3-ok" / "exit_code").unlink()
            await pilot.click("#confirm-yes")
            await pilot.pause()
            self.assertIn("仍在运行", str(app.query_one("#status", Static).render()))
            self.assertFalse(next(j for j in self.store.snapshot()["jobs"] if j["id"] == "codex-3-ok")["hidden"])
