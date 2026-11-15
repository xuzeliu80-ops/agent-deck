import json
import os
import xml.etree.ElementTree as ET
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from textual.widgets import Input, ListView, Static

from aw.core import Store, decide, is_active
from aw.tui import DecisionNote, Detail, UndoDecision, WatchApp
from fixtures import fixture_store, jsonl, make_job


class TuiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = fixture_store(Path(self.tmp.name))

    async def test_tab_bars_wide_narrow_and_resize(self):
        app = WatchApp(self.store, no_notify=True, no_anim=True)
        async with app.run_test(size=(160, 45)) as pilot:
            for width in (160, 100, 119, 120, 70, 160):
                await pilot.resize_terminal(width, 45)
                await pilot.pause()
                tabs = app.query_one("#tab-buttons")
                self.assertEqual(tabs.display, width < 120)
                self.assertFalse(app.query_one("#nav").display)
                if width >= 120:
                    self.assertEqual(tabs.region.height, 0)
                    self.assertEqual(app.query_one("#columns").region.y,
                                     app.query_one("#top").region.bottom)
                else:
                    self.assertTrue(all(app.query_one(f"#tab-{i}").visible for i in range(3)))

    async def test_decision_children_fit_dialog_after_resize(self):
        app = WatchApp(self.store, no_notify=True, no_anim=True)
        async with app.run_test(size=(160, 45)) as pilot:
            await pilot.press("d")
            for width, height in ((160, 45), (100, 32), (70, 24), (160, 45)):
                await pilot.resize_terminal(width, height)
                await pilot.pause()
                dialog = app.screen.query_one("#note-dialog")
                self.assertEqual(dialog.region.width, min(90, width - 8))
                for child in dialog.query("*"):
                    with self.subTest(size=(width, height), child=child.id):
                        self.assertGreaterEqual(child.region.x, dialog.region.x)
                        self.assertGreaterEqual(child.region.y, dialog.region.y)
                        self.assertLessEqual(child.region.right, dialog.region.right)
                        self.assertLessEqual(child.region.bottom, dialog.region.bottom)
            await pilot.click("#choose-other")
            await pilot.pause()
            self.assertFalse(app.screen.query_one("#choice-input", Input).disabled)

    async def test_navigation_detail_refresh_and_history(self):
        app = WatchApp(self.store, no_notify=True)
        async with app.run_test(size=(160, 45)) as pilot:
            self.assertEqual(app.pane_index, 0)
            self.assertEqual(app.selected().key, "high")
            self.assertGreater(app.selected().styles.background.a, 0)
            await pilot.press("down")
            self.assertEqual(app.selected().key, "normal")
            await pilot.press("enter")
            self.assertIsInstance(app.screen, DecisionNote)
            await pilot.press("escape", "right")
            self.assertEqual(app.pane_index, 1)
            await pilot.press("down")
            key = app.selected().key
            await pilot.press("r")
            self.assertEqual(app.selected().key, key)
            await app.refresh_data()
            self.assertEqual(app.selected().key, key)
            await pilot.press("enter")
            self.assertIsInstance(app.screen, Detail)
            self.assertIn("最近 15 步", app.screen.body)
            await pilot.press("escape", "tab")
            self.assertEqual(app.pane_index, 2)
            await pilot.press("left")
            self.assertEqual(app.pane_index, 1)
            self.assertEqual(len([r for r in app.query_one("#list-1").children if r.kind == "job"]), 5)
            await pilot.press("h")
            self.assertTrue(app.history)
            self.assertEqual(len([r for r in app.query_one("#list-1").children if r.kind == "job"]), 8)
            await pilot.press("ctrl+c")
            self.assertFalse(app.is_running)

    async def test_decision_note_and_cancel(self):
        app = WatchApp(self.store, no_notify=True)
        async with app.run_test(size=(160, 45)) as pilot:
            await pilot.press("d")
            self.assertIsInstance(app.screen, DecisionNote)
            await pilot.press("escape")
            self.assertEqual(app.selected().data["status"], "open")
            await pilot.press("d")
            await pilot.press("2")
            app.screen.query_one("#note-input", Input).value = "已和团队确认"
            await pilot.press("enter")
            await pilot.pause()
            data = json.loads(self.store.board.read_text())
            high = next(d for d in data["decisions"] if d["id"] == "high")
            self.assertEqual(high["status"], "decided")
            self.assertEqual(high["note"], "已和团队确认")
            self.assertEqual(app.selected().key, "high")
            self.assertEqual(high["choice"], "继续检查")

    async def check_progress_decide(self, count):
        data = json.loads(self.store.board.read_text())
        # Keep unrelated open decisions and a decided item in the same project.
        data["decisions"].append(dict(data["decisions"][2], project="后端服务"))
        for i in range(count):
            data["decisions"].append(dict(data["decisions"][0],
                                          id=f"progress-{i}", project="后端服务"))
        self.store.board.write_text(json.dumps(data, ensure_ascii=False))
        before = self.store.board.read_bytes()
        app = WatchApp(self.store, no_notify=True, no_anim=True)
        async with app.run_test(size=(160, 45)) as pilot:
            view = app.query_one("#list-0", ListView)
            view.index = next(i for i, r in enumerate(view.children)
                              if r.kind == "progress" and r.data["project"] == "后端服务")
            await pilot.pause()
            progress = app.selected()
            matches = [r for r in view.children if r.kind == "decision"
                       and r.data.get("project") == "后端服务" and r.data["status"] == "open"]
            await pilot.press("d")
            if count == 1:
                self.assertIsInstance(app.screen, DecisionNote)
                self.assertEqual(app.screen.decision["id"], matches[0].key)
                await pilot.press("escape")
            elif count > 1:
                self.assertEqual(len(app.screen_stack), 1)
                self.assertIs(app.selected(), matches[0])
                self.assertEqual(str(app.query_one("#status", Static).render()),
                                 f"该项目有 {count} 条待拍板，已跳到第一条，按 Enter 拍板")
                await pilot.press("d")
                self.assertIsInstance(app.screen, DecisionNote)
                self.assertEqual(app.screen.decision["id"], matches[0].key)
                await pilot.press("escape")
            else:
                self.assertEqual(len(app.screen_stack), 1)
                self.assertIs(app.selected(), progress)
                self.assertEqual(str(app.query_one("#status", Static).render()),
                                 "这是进度条目；该项目没有待拍板事项")
            self.assertEqual(self.store.board.read_bytes(), before)
            self.assertFalse(self.store.state.exists())

    async def test_progress_decide_single_open_decision(self):
        await self.check_progress_decide(1)

    async def test_progress_decide_multiple_open_decisions(self):
        await self.check_progress_decide(2)

    async def test_progress_decide_no_open_decisions(self):
        await self.check_progress_decide(0)

    async def test_empty_note_and_input_shortcut_letters(self):
        app = WatchApp(self.store, no_notify=True)
        async with app.run_test() as pilot:
            await pilot.press("d")
            await pilot.press("1")
            await pilot.press("a", "d", "h", "q", "r", "1")
            self.assertEqual(app.screen.query_one("#note-input", Input).value, "adhqr1")
            app.screen.query_one("#note-input", Input).value = ""
            await pilot.press("enter")
            await pilot.pause()
            high = next(d for d in json.loads(self.store.board.read_text())["decisions"] if d["id"] == "high")
            self.assertEqual(high["status"], "decided")
            self.assertEqual(high["note"], "")
            self.assertEqual(high["choice"], "开始")

    async def test_died_readonly_and_history_placement(self):
        import time
        now = time.time()
        old = make_job(self.store.jobs, "old-died", state="died", now=now-30000)
        os.utime(old / "output.txt", (now-30000, now-30000))
        app = WatchApp(self.store, no_notify=True)
        async with app.run_test(size=(160, 45)) as pilot:
            await pilot.press("right")
            view = app.query_one("#list-1", ListView)
            self.assertNotIn("old-died", [r.key for r in view.children])
            view.index = next(i for i, r in enumerate(view.children) if r.kind == "job" and r.data["state"] == "died")
            await pilot.pause()
            await pilot.press("a")
            self.assertFalse(self.store.state.exists())
            await pilot.press("h")
            view.index = next(i for i, r in enumerate(view.children) if r.key == "old-died")
            await pilot.pause()
            row = app.selected()
            self.assertFalse(is_active(row.data))
            self.assertIn(" 失联 ", row.card_text.plain)
            self.assertLess(next(i for i, r in enumerate(view.children) if r.key == "done"), view.index)
            await pilot.press("a")
            self.assertFalse(self.store.state.exists())
            self.assertFalse(self.store.state.exists())

    async def test_compact_cards_keep_badge_and_preview_on_one_line(self):
        app = WatchApp(self.store, no_notify=True)
        async with app.run_test(size=(160, 45)) as pilot:
            for size in ((160, 45), (100, 32), (120, 45), (70, 24)):
                await pilot.resize_terminal(*size)
                if app.pane_index == 0:
                    await pilot.press("right")
                for j in app.snapshot_data["jobs"]:
                    j["title"] = "超长中文标题" * 30
                    j["latest"] = "命令：echo " + "超长命令" * 50
                # Keep the mocked snapshot stable across resize/refresh.
                with patch.object(self.store, "snapshot", return_value=app.snapshot_data):
                    await app.refresh_data()
                    await pilot.pause()
                    view = app.query_one("#list-1", ListView)
                    for row in view.children:
                        if row.kind != "job":
                            continue
                        expected = (3 if row.data.get("purpose") else 2) if is_active(row.data) else 2
                        self.assertEqual(row.query_one(Static).size.height, expected, (size, row.key))
                        self.assertEqual(row.size.height, expected)
                        if not is_active(row.data):
                            self.assertIn("NEW", row.card_text.plain.splitlines()[0])
                        self.assertIn("…", row.card_text.plain.splitlines()[0])
                decision = next(r for r in app.query_one("#list-0").children if r.kind == "decision")
                self.assertEqual(decision.styles.padding.bottom, 0)
                self.assertFalse(decision.card_text.plain.endswith("\n"))

    async def test_agy_detail_last_sixty_lines_and_summary_tail(self):
        response = "已更新 schema_check.py，正在校验。\n\n" + "\n".join(f"过程 {i}" for i in range(80)) + "\n\n合入主干并推进集成回归。"
        make_job(self.store.jobs, "agy-summary", agent="antigravity", state="ok",
                 output=jsonl([dict(event="result", result=dict(response=response))]))
        app = WatchApp(self.store, no_notify=True)
        async with app.run_test(size=(160, 45)) as pilot:
            await pilot.press("right", "right")
            view = app.query_one("#list-2", ListView)
            view.index = next(i for i, r in enumerate(view.children) if r.key == "agy-summary")
            await pilot.pause()
            self.assertIn("合入主干并推进集成回归", app.selected().card_text.plain)
            self.assertNotIn("已更新", app.selected().card_text.plain)
            await pilot.press("enter")
            result = app.screen.body.split("最终回复 / 结果（末 60 行）\n", 1)[1]
            self.assertEqual(result, "\n".join(response.splitlines()[-60:]))

    async def test_full_shell_command_survives_in_details(self):
        command = "/bin/zsh -lc 'echo first\necho second'"
        make_job(self.store.jobs, "shell-detail", output=jsonl([dict(type="item.completed", item=dict(id="cmd", type="command_execution", command=command))]))
        app = WatchApp(self.store, no_notify=True)
        async with app.run_test() as pilot:
            await pilot.press("right")
            view = app.query_one("#list-1", ListView)
            view.index = next(i for i, r in enumerate(view.children) if r.key == "shell-detail")
            await pilot.pause()
            self.assertEqual(app.selected().data["latest"], "运行命令")
            await pilot.press("enter")
            self.assertIn(command, app.screen.body)

    async def test_narrow_tabs_and_resize(self):
        app = WatchApp(self.store, no_notify=True)
        async with app.run_test(size=(100, 32)) as pilot:
            self.assertTrue(app.has_class("narrow"))
            for i in range(3):
                pane = app.query_one(f"#pane-{i}")
                self.assertEqual(pane.display, i == 0)
            await pilot.press("tab")
            self.assertEqual(app.pane_index, 1)
            self.assertTrue(app.query_one("#pane-1").display)
            self.assertLessEqual(app.query_one("#pane-1").region.right, 100)
            await pilot.press("enter")
            self.assertIsInstance(app.screen, Detail)
            await pilot.press("escape")
            await pilot.resize_terminal(160, 45)
            await pilot.pause()
            self.assertFalse(app.has_class("narrow"))
            self.assertTrue(all(app.query_one(f"#pane-{i}").display for i in range(3)))
            await pilot.resize_terminal(70, 24)
            self.assertTrue(app.has_class("narrow"))
            self.assertLessEqual(app.query_one("#pane-1").region.right, 70)

    async def test_selection_survives_new_task_and_decision(self):
        app = WatchApp(self.store, no_notify=True)
        async with app.run_test() as pilot:
            await pilot.press("down")
            key = app.selected().key
            data = json.loads(self.store.board.read_text())
            data["decisions"].insert(0, dict(id="new-high", priority="high", created="2020", status="open", title="新事项"))
            self.store.board.write_text(json.dumps(data))
            await app.refresh_data()
            self.assertEqual(app.selected().key, key)

    async def test_bad_board_visible(self):
        self.store.board.write_text("{")
        app = WatchApp(self.store, no_notify=True)
        async with app.run_test() as pilot:
            self.assertIn("Claude 看板读取失败", str(app.query_one("#status", Static).render()))
            await pilot.press("right", "enter")
            self.assertIsInstance(app.screen, Detail)

    async def test_refresh_and_resize_while_details_open(self):
        app = WatchApp(self.store, no_notify=True)
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.press("right", "enter")
            await app.refresh_data()
            self.assertIsInstance(app.screen, Detail)
            await pilot.resize_terminal(160, 45)
            self.assertFalse(app.has_class("narrow"))
            await pilot.press("escape")
            self.assertEqual(app.selected().data["state"], "running")

    async def test_missing_decision_during_input_reports_error(self):
        app = WatchApp(self.store, no_notify=True)
        async with app.run_test() as pilot:
            await pilot.press("d")
            data = json.loads(self.store.board.read_text())
            data["decisions"] = [d for d in data["decisions"] if d["id"] != "high"]
            self.store.board.write_text(json.dumps(data))
            await pilot.press("1", "enter")
            await pilot.pause()
            self.assertIn("拍板未保存", str(app.query_one("#status", Static).render()))
            self.assertEqual(len(app.screen_stack), 1)

    async def test_screenshot_chinese_width_and_offline_fonts(self):
        from capture_shots import offline_svg
        with patch.dict(os.environ):
            os.environ.pop("NO_COLOR", None)
            app = WatchApp(self.store, no_notify=True)
        async with app.run_test(size=(100, 32)) as pilot:
            path = app.save_screenshot(filename="shot.svg", path=self.tmp.name)
            offline_svg(path)
            svg = Path(path).read_text()
            self.assertNotIn("https://cdnjs", svg)
            self.assertIn("#ff838b", svg)
            root = ET.fromstring(svg)
            heading = next(node for node in root.iter() if node.tag.endswith("text") and node.text == "待你拍板")
            self.assertAlmostEqual(float(heading.attrib["textLength"]), 97.6)
            self.assertEqual(heading.attrib["lengthAdjust"], "spacingAndGlyphs")

    async def test_five_second_automatic_refresh(self):
        app = WatchApp(self.store, no_notify=True)
        with patch.object(self.store, "snapshot", wraps=self.store.snapshot) as snapshot:
            async with app.run_test() as pilot:
                snapshot.assert_called_once()
                await pilot.pause(5.1)
                self.assertGreaterEqual(snapshot.call_count, 2)

    async def test_notifications_baseline_transition_and_disabled(self):
        for no_notify in (True, False):
            app = WatchApp(self.store, no_notify=no_notify)
            with patch("aw.tui.notify_macos") as notify:
                async with app.run_test() as pilot:
                    notify.assert_not_called()
                    data = json.loads(self.store.board.read_text())
                    data["decisions"].append(dict(id="new-"+str(no_notify), status="open", title="新决策"))
                    self.store.board.write_text(json.dumps(data))
                    await app.refresh_data()
                    self.assertEqual(notify.call_count, 0 if no_notify else 1)
                    await app.refresh_data()
                    self.assertEqual(notify.call_count, 0 if no_notify else 1)


    async def test_other_answer_requires_choice_and_keeps_digits(self):
        app = WatchApp(self.store, no_notify=True)
        async with app.run_test(size=(100, 32)) as pilot:
            before = self.store.board.read_bytes()
            await pilot.press("d")
            app.screen.save()
            self.assertIn("请先选择", str(app.screen.query_one("#choice-error").render()))
            self.assertEqual(self.store.board.read_bytes(), before)
            await pilot.click("#choose-other")
            await pilot.press("1", "2", "a", "d", "u", "q", "enter")
            self.assertEqual(app.screen.query_one("#choice-input", Input).value, "12aduq")
            self.assertEqual(app.screen.focused.id, "note-input")
            await pilot.press("9", "enter")
            d = next(d for d in json.loads(self.store.board.read_text())["decisions"] if d["id"] == "high")
            self.assertEqual((d["choice"], d["note"]), ("12aduq", "9"))
            self.assertIs(d["seen_by_claude"], False)

    async def test_no_options_answer_and_blank_note(self):
        data = json.loads(self.store.board.read_text())
        data["decisions"][1]["options"] = []
        self.store.board.write_text(json.dumps(data))
        app = WatchApp(self.store, no_notify=True)
        async with app.run_test() as pilot:
            await pilot.press("d")
            self.assertEqual(app.screen.focused.id, "choice-input")
            await pilot.press("enter")
            self.assertEqual(len(app.screen_stack), 2)
            app.screen.query_one("#choice-input", Input).value = "采用方案 A；灰度发布"
            await pilot.press("enter", "enter")
            d = next(d for d in json.loads(self.store.board.read_text())["decisions"] if d["id"] == "high")
            self.assertEqual((d["choice"], d["note"]), ("采用方案 A；灰度发布", ""))

    async def test_ninth_option_shortcut_and_tenth_by_keyboard(self):
        data = json.loads(self.store.board.read_text())
        data["decisions"][1]["options"] = [f"选项 {i}" for i in range(1, 11)]
        self.store.board.write_text(json.dumps(data))
        app = WatchApp(self.store, no_notify=True)
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.press("d", "9")
            self.assertEqual(app.screen.choice, "选项 9")
            await pilot.press("escape", "d")
            app.screen.query_one("#option-9").focus()
            await pilot.press("enter", "enter")
            self.assertEqual(next(d for d in json.loads(self.store.board.read_text())["decisions"] if d["id"] == "high")["choice"], "选项 10")

    async def test_unseen_section_detail_and_claude_read_disappears(self):
        app = WatchApp(self.store, no_notify=True)
        async with app.run_test() as pilot:
            await pilot.press("d", "2")
            app.screen.query_one("#note-input", Input).value = "再查一下"
            await pilot.press("enter")
            self.assertEqual(app.selected().key, "high")
            rows = list(app.query_one("#list-0").children)
            self.assertEqual(sum(r.key == "high" for r in rows), 1)
            self.assertGreater(next(i for i, r in enumerate(rows) if r.key == "high"), next(i for i, r in enumerate(rows) if r.key == "unseen"))
            self.assertIn("已选：继续检查", app.selected().card_text.plain)
            await pilot.press("enter")
            self.assertIn("选择：继续检查\n备注：再查一下", app.screen.body)
            await pilot.press("escape")
            data = json.loads(self.store.board.read_text())
            next(d for d in data["decisions"] if d["id"] == "high")["seen_by_claude"] = True
            self.store.board.write_text(json.dumps(data))
            await app.refresh_data()
            self.assertNotIn("unseen", [r.key for r in app.query_one("#list-0").children])
            self.assertNotIn("high", [r.key for r in app.query_one("#list-0").children])
            await pilot.press("h")
            self.assertIn("high", [r.key for r in app.query_one("#list-0").children])

    async def test_undo_unseen_confirm_cancel_and_history(self):
        app = WatchApp(self.store, no_notify=True)
        async with app.run_test() as pilot:
            await pilot.press("d", "1", "enter")
            before = self.store.board.read_bytes()
            await pilot.press("u")
            self.assertIsInstance(app.screen, UndoDecision)
            # Default Enter cancels; it must not silently undo.
            await pilot.press("enter")
            self.assertEqual(self.store.board.read_bytes(), before)
            await pilot.press("u", "y")
            restored = app.selected().data
            self.assertEqual(restored["status"], "open")
            self.assertNotIn("choice", restored)
            self.assertNotIn("unseen", [r.key for r in app.query_one("#list-0").children])
            await pilot.press("h")
            view = app.query_one("#list-0", ListView)
            view.index = next(i for i, r in enumerate(view.children) if r.key == "old")
            await pilot.pause()
            await pilot.press("u", "y")
            self.assertEqual(app.selected().data["status"], "open")

    async def test_undo_missing_item_reports_error(self):
        decide(self.store.board, "high", "开始")
        app = WatchApp(self.store, no_notify=True)
        async with app.run_test() as pilot:
            view = app.query_one("#list-0", ListView)
            view.index = next(i for i, r in enumerate(view.children) if r.key == "high")
            await pilot.pause()
            await pilot.press("u")
            data = json.loads(self.store.board.read_text())
            data["decisions"] = [d for d in data["decisions"] if d["id"] != "high"]
            self.store.board.write_text(json.dumps(data))
            await pilot.press("y")
            self.assertIn("撤销未保存", str(app.query_one("#status", Static).render()))

    async def test_animation_timer_only_headers_and_no_anim(self):
        for no_anim in (False, True):
            app = WatchApp(self.store, no_notify=True, no_anim=no_anim)
            with patch.object(self.store, "snapshot", wraps=self.store.snapshot) as snapshot:
                async with app.run_test(size=(160, 45)) as pilot:
                    rows = list(app.query_one("#list-0").children)
                    selected = app.selected()
                    before = app.query_one("#header-0").render()
                    before = (before.plain, before.spans)
                    await pilot.pause(2.5)
                    self.assertEqual(snapshot.call_count, 1)
                    self.assertEqual(app.pet_frame, 0 if no_anim else 3)
                    self.assertEqual(list(app.query_one("#list-0").children), rows)
                    self.assertIs(app.selected(), selected)
                    after = app.query_one("#header-0").render()
                    after = (after.plain, after.spans)
                    self.assertEqual(before == after, no_anim)
                    if not no_anim:
                        await pilot.press("d")
                        app.animate_pets()
                        self.assertIsInstance(app.screen, DecisionNote)
                        self.assertEqual(snapshot.call_count, 1)

    async def test_pet_headers_wide_and_compact_tabs(self):
        from aw.pets import COLORS, header_text
        from aw.core import pet_status
        app = WatchApp(self.store, no_notify=True, no_anim=True)
        async with app.run_test(size=(160, 45)) as pilot:
            for i, agent in enumerate(("claude", "codex", "antigravity")):
                state, label, summary = pet_status(app.snapshot_data, agent)
                header = app.query_one(f"#header-{i}", Static)
                self.assertEqual(header.size.height, 6)
                self.assertEqual(str(header.render()), header_text(agent, ("Claude", "GPT", "Gemini")[i], state, label, summary).plain)
                self.assertIn(summary, str(header.render()))
            await pilot.resize_terminal(70, 24)
            nav = str(app.query_one("#nav").render())
            for agent in ("claude", "codex", "antigravity"):
                state = pet_status(app.snapshot_data, agent)[0]
                button = app.query_one(f"#tab-{("claude", "codex", "antigravity").index(agent)}")
                self.assertIn("●", str(button.label))
                self.assertEqual(button.label.spans[0].style, "bold " + COLORS[agent])
            self.assertFalse(app.query_one("#header-0").display)

    async def test_decision_dialog_narrow_layout_and_recommendation(self):
        app = WatchApp(self.store, no_notify=True, no_anim=True)
        async with app.run_test(size=(70, 24)) as pilot:
            await pilot.press("d")
            self.assertIn("推荐", str(app.screen.query_one("#option-0").label))
            dialog = app.screen.query_one("#note-dialog")
            self.assertGreaterEqual(dialog.region.y, 0)
            self.assertLessEqual(dialog.region.bottom, 24)
            save = app.screen.query_one("#save-note")
            self.assertLessEqual(save.region.bottom, dialog.region.bottom)
            await pilot.press("down", "enter")
            self.assertEqual(app.screen.choice, "继续检查")
            await pilot.press("enter")
            self.assertEqual(len(app.screen_stack), 1)


if __name__ == "__main__":
    unittest.main()
