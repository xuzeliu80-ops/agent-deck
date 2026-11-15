"""v1.5: fixed-clock display rules and read-only task interactions."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from rich.console import Console
from textual.widgets import ListView, Static

from aw import core
from aw.tui import Detail, SPINNER, WatchApp, job_detail, job_text
from fixtures import fixture_store, jsonl, make_job

NOW = 2_000_000_000
CONSOLE = Console(color_system="truecolor")


def job(state="ok", agent="codex", age=10):
    return dict(id="fixture", agent=agent, state=state, title="边界验证", purpose="验证边界条件",
                model="test-model", effort="high", started=NOW-210, ended=NOW-age if state in ("ok", "fail", "died") else None,
                last_activity=NOW-age, elapsed_min=3.5, limit_min=30, rc="7" if state == "fail" else "0",
                archived=False, result="**检查完成**。第二句。\n\n后续建议。", result_summary="检查完成。",
                current="跑测试", prompt="原始任务", steps=["命令：python -m unittest"], tokens=123)


class DisplayTests(unittest.TestCase):
    def test_all_five_badge_text_background_and_dark_foreground(self):
        expected = {"running": ("运行中", "#3d8bfd"), "stalled": ("卡住", "#e0b13a"),
                    "died": ("失联", "#f08a3c"), "ok": ("成功", "#2fae74"), "fail": ("失败", "#e5534b")}
        for agent in ("codex", "antigravity"):
            for state, (label, color) in expected.items():
                for archived in ((False, True) if state == "died" else (False,)):
                    with self.subTest(agent=agent, state=state, archived=archived):
                        j = job(state, agent)
                        j["archived"] = archived
                        text = job_text(j, now=NOW)
                        block = " " + label + " "
                        start = text.plain.index(block)
                        for offset in range(start, start + len(block)):
                            style = text.get_style_at_offset(CONSOLE, offset)
                            self.assertEqual(style.bgcolor.triplet.hex, color)
                            self.assertEqual(style.color.triplet.hex, "#101721")
                        self.assertFalse(any(symbol in text.plain.splitlines()[0] for symbol in ("▶", "✓", "✗")))

    def test_new_time_boundary_and_title_weight(self):
        self.assertEqual(core.NEW_MINUTES, 30)
        for state in ("ok", "fail", "died"):
            for age, expected in ((0, True), (1799, True), (1800, False), (1801, False), (-1, False)):
                with self.subTest(state=state, age=age):
                    j = job(state, age=age)
                    text = job_text(j, now=NOW)
                    self.assertEqual(core.is_new(j, NOW), expected)
                    self.assertEqual(" NEW " in text.plain, expected)
                    style = text.get_style_at_offset(CONSOLE, text.plain.index(j["title"]))
                    self.assertEqual(bool(style.bold), expected)
                    if expected:
                        style = text.get_style_at_offset(CONSOLE, text.plain.index("NEW"))
                        self.assertEqual(style.bgcolor.triplet.hex, "#fff1a8")
                        self.assertEqual(style.color.triplet.hex, "#101721")
        self.assertFalse(core.is_new(job("running"), NOW))
        self.assertFalse(core.is_new(job("stalled"), NOW))
        self.assertFalse(core.is_new(dict(job(), ended=None), NOW))

    def test_bar_on_every_line_only_running_or_new(self):
        for state, age, expected in (("running", 10, True), ("stalled", 10, False), ("died", 10, True),
                                     ("died", 1801, False), ("ok", 10, True), ("fail", 10, True),
                                     ("ok", 1801, False), ("fail", 1801, False)):
            with self.subTest(state=state, age=age):
                text = job_text(job(state, age=age), now=NOW)
                self.assertTrue(all(line.startswith("▌") == expected for line in text.plain.splitlines()))
                if expected:
                    for offset, char in enumerate(text.plain):
                        if char == "▌":
                            badge_style = text.get_style_at_offset(CONSOLE, text.plain.index(" " + core.STATE_LABELS[state] + " "))
                            self.assertEqual(text.get_style_at_offset(CONSOLE, offset).color, badge_style.bgcolor)

    def test_spinner_frames_only_on_running(self):
        texts = [job_text(job("running"), now=NOW, frame=i).plain for i in range(len(SPINNER)+1)]
        self.assertEqual(texts[0], texts[-1])
        self.assertEqual(len(set(texts)), len(SPINNER))
        for state in ("stalled", "died", "ok", "fail"):
            self.assertEqual(job_text(job(state), now=NOW, frame=0), job_text(job(state), now=NOW, frame=1))

    def test_header_counts_all_recent_endings_and_hides_zero(self):
        for agent in ("codex", "antigravity"):
            jobs = [job(state, agent) for state in ("ok", "ok", "fail", "died", "running", "stalled")]
            jobs.append(job("ok", agent, 1801))
            other = "antigravity" if agent == "codex" else "codex"
            jobs.append(job("ok", other))
            data = dict(now=NOW, jobs=jobs)
            self.assertEqual(core.pet_status(data, agent)[2], "运行 2 · 新完成 4")
            data["now"] += 1801
            self.assertEqual(core.pet_status(data, agent)[2], "运行 2")
            self.assertNotIn("待看", core.pet_status(data, agent)[2])

    def test_detail_three_lines_first_paragraph_before_raw(self):
        for agent in ("codex", "antigravity"):
            body = job_detail(job(agent=agent))
            lines = body.splitlines()
            self.assertEqual(lines[:4], ["目的：验证边界条件", "结果：检查完成。第二句。",
                                       "用时：3.5 分钟 · test-model · high", ""])
            self.assertEqual(lines[4], "—— 以下为原始记录 ——")
            self.assertIn("原始任务", body)
            self.assertIn("后续建议。", body)
            self.assertIn("tokens 123", body)

    def test_failure_reason_and_missing_purpose(self):
        j = job("fail")
        j.update(purpose="", failure_reason="**连接被拒绝**。", result="")
        lines = job_detail(j).splitlines()
        self.assertEqual(lines[0], "目的：未提供目的")
        self.assertEqual(lines[1], "结果：失败（退出码 7）：连接被拒绝。")
        j["failure_reason"] = ""
        self.assertIn("未记录失败原因", job_detail(j).splitlines()[1])
        self.assertIn("心跳失联", job_detail(job("died")).splitlines()[1])

    def test_failure_events_are_available_to_detail(self):
        for agent, event in (("codex", dict(type="turn.failed", error=dict(message="额度耗尽"))),
                             ("antigravity", dict(event="result", result=dict(error=dict(message="额度耗尽"))))):
            parsed = core.parse_output(jsonl([event]), agent)
            self.assertEqual(parsed["failure_reason"], "额度耗尽")
            j = job("fail", agent)
            j.update(parsed)
            self.assertIn("额度耗尽", job_detail(j).splitlines()[1])

    def test_dump_has_new_and_expiry_without_ack_field(self):
        data = dict(now=NOW, jobs=[job()], decisions=[], progress=[], errors=[], counts=dict(codex=0, antigravity=0, decisions=0))
        self.assertIn("成功 边界验证", core.dump_snapshot(data))
        self.assertIn("NEW", core.dump_snapshot(data))
        data["now"] += 1801
        self.assertNotIn("NEW", core.dump_snapshot(data))
        self.assertNotIn("未看", core.dump_snapshot(data))

    def test_notifications_keep_existing_died_wording(self):
        changes = core.Changes()
        changes.poll(dict(jobs=[], decisions=[]))
        self.assertEqual(changes.poll(dict(jobs=[job("died")], decisions=[])), [("任务结束 · 进程失联", "边界验证")])


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = fixture_store(Path(self.tmp.name))

    def test_legacy_state_is_never_read_or_modified(self):
        self.store.state.write_text('{"acked":["codex-3-ok"]}')
        before = self.store.state.read_bytes(), self.store.state.stat().st_mtime_ns
        original = core.read_text
        def guarded(path, *args, **kwargs):
            self.assertNotEqual(Path(path), self.store.state)
            return original(path, *args, **kwargs)
        with patch.object(core, "read_text", side_effect=guarded):
            data = self.store.snapshot(NOW)
        self.assertTrue(all("acked" not in j for j in data["jobs"]))
        self.assertEqual((self.store.state.read_bytes(), self.store.state.stat().st_mtime_ns), before)

    def test_died_uses_last_activity_and_does_not_restart_new_on_refresh(self):
        directory = make_job(self.store.jobs, "lost", state="died", now=NOW)
        (directory / "heartbeat").write_text(str(NOW-200))
        os.utime(directory / "output.txt", (NOW-150, NOW-150))
        first = self.store.job_info(directory, NOW)
        later = self.store.job_info(directory, NOW+1651)
        self.assertEqual(first["ended"], NOW-150)
        self.assertEqual(later["ended"], first["ended"])
        self.assertTrue(core.is_new(first, NOW))
        self.assertFalse(core.is_new(later, NOW+1651))


class InteractionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = fixture_store(self.root)

    def fingerprint(self):
        return {str(p.relative_to(self.root)): (p.read_bytes(), p.stat().st_mtime_ns)
                for p in self.root.rglob("*") if p.is_file() and p != self.store.user_state}

    async def test_a_and_detail_close_preserve_source_and_legacy_files(self):
        # Viewing now persists reads in aw_user_state.json; source/legacy files stay immutable.
        for existing in (False, True):
            if existing:
                self.store.state.write_text('{"acked":["old-id"],"extra":"keep"}')
            before = self.fingerprint()
            app = WatchApp(self.store, no_notify=True, no_anim=True)
            async with app.run_test(size=(160, 45)) as pilot:
                await pilot.press("right", "h")
                view = app.query_one("#list-1", ListView)
                for state in ("running", "stalled", "died", "ok", "fail"):
                    view.index = next(i for i, row in enumerate(view.children) if row.kind == "job" and row.data["state"] == state)
                    await pilot.pause()
                    await pilot.press("a")
                    self.assertEqual(self.fingerprint(), before)
                    for key in ("enter", "escape", "space", "button"):
                        await pilot.press("enter")
                        self.assertIsInstance(app.screen, Detail)
                        self.assertNotIn("已看", str(app.screen.query_one("#close-detail").label))
                        if key == "button":
                            await pilot.click("#close-detail")
                        else:
                            await pilot.press(key)
                        self.assertEqual(len(app.screen_stack), 1)
                        self.assertEqual(self.fingerprint(), before)
            self.assertEqual(self.store.state.exists(), existing)

    async def test_live_frame_updates_card_without_reading_files_and_no_anim_stays_fixed(self):
        for no_anim in (False, True):
            app = WatchApp(self.store, no_notify=True, no_anim=no_anim)
            async with app.run_test(size=(160, 45)) as pilot:
                row = next(r for r in app.query_one("#list-1").children if r.kind == "job" and r.data["state"] == "running")
                with patch.object(self.store, "snapshot", wraps=self.store.snapshot) as snapshot:
                    before = row.card_text.plain
                    await pilot.pause(0.95)
                    self.assertEqual(row.card_text.plain == before, no_anim)
                    self.assertEqual(snapshot.call_count, 0)

    async def test_refresh_expires_new_and_summary_together(self):
        data = self.store.snapshot()
        j = next(j for j in data["jobs"] if j["id"] == "codex-3-ok")
        data["jobs"] = [j]
        data["now"] = NOW
        j["ended"] = NOW-1799
        with patch.object(self.store, "snapshot", side_effect=lambda: copy.deepcopy(data)):
            app = WatchApp(self.store, no_notify=True, no_anim=True)
            async with app.run_test(size=(160, 45)) as pilot:
                row = next(r for r in app.query_one("#list-1").children if r.kind == "job")
                self.assertIn("NEW", row.card_text.plain)
                self.assertIn("新完成 1", str(app.query_one("#header-1", Static).render()))
                data["now"] += 2
                await app.refresh_data()
                self.assertNotIn("NEW", row.card_text.plain)
                self.assertNotIn("▌", row.card_text.plain)
                self.assertNotIn("新完成", str(app.query_one("#header-1", Static).render()))
                self.assertFalse(self.store.state.exists())
