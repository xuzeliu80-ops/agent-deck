"""Right-click actions, real hit testing, IME-safe close form and mouse protocol."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from textual import events
from textual._xterm_parser import XTermParser
from textual.drivers.linux_driver import LinuxDriver
from textual.widgets import Button, Static

from aw import core
from aw.tui import CloseDecision, Confirm, ContextMenu, MenuTarget, WatchApp
from fixtures import fixture_store
from mouse_helpers import menu_action, right_click


class MouseProtocolTests(unittest.TestCase):
    def test_sgr_left_and_right_buttons_have_correct_cell_coordinates(self):
        parser = XTermParser()
        for code, button, event_type in ((0, 1, events.MouseDown), (2, 3, events.MouseDown)):
            event = list(parser.feed(f"\x1b[<{code};19;7M"))[0]
            self.assertIsInstance(event, event_type)
            self.assertEqual((event.button, event.x, event.y), (button, 18, 6))
            released = list(parser.feed(f"\x1b[<{code};19;7m"))[0]
            self.assertIsInstance(released, events.MouseUp)
            self.assertEqual(released.button, button)

    def test_terminal_driver_enables_reporting_and_sgr(self):
        driver = object.__new__(LinuxDriver)
        driver._mouse = True
        with patch.object(driver, "write") as write, patch.object(driver, "flush"):
            driver._enable_mouse_support()
        values = [call.args[0] for call in write.call_args_list]
        self.assertIn("\x1b[?1000h", values)
        self.assertIn("\x1b[?1006h", values)

    def test_cli_explicitly_enables_mouse(self):
        from aw.__main__ import main
        with patch("sys.argv", ["aw"]), patch("aw.tui.WatchApp") as app:
            main()
        app.return_value.run.assert_called_once_with(mouse=True)

    def test_menu_actions_match_type_and_archive_state(self):
        app = WatchApp(no_notify=True)
        cases = [("progress", {"status": "running"}, False, {"status", "edit", "detail", "archive"}),
                 ("decision", {"status": "open"}, False, {"decide", "close"}),
                 ("decision", {"status": "decided"}, False, {"undo"}),
                 *[("job", {"state": s}, False, {"detail", "hide"}) for s in core.TERMINAL],
                 *[("job", {"state": s}, False, {"detail"}) for s in ("running", "stalled")],
                 ("progress", {}, True, {"restore", "detail"}),
                 ("job", {"state": "fail"}, True, {"restore", "detail"})]
        for kind, data, archived, expected in cases:
            with self.subTest(kind=kind, data=data, archived=archived):
                self.assertEqual({a for a, _ in app.context_options(MenuTarget(kind, "key", data, archived))}, expected)
        self.assertEqual({a for a, _ in app.context_options(None)}, {"clear", "archives"})


class ContextInteractionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = fixture_store(Path(self.tmp.name))

    def app(self):
        return WatchApp(self.store, no_notify=True, no_anim=True)

    def row(self, app, pane, key):
        return next(r for r in app.query_one(f"#list-{pane}").children if r.key == key)

    def read_board(self):
        return json.loads(self.store.board.read_text())

    async def test_left_click_only_selects_and_marks_read_in_another_pane(self):
        app = self.app()
        async with app.run_test(size=(160, 45)) as p:
            await p.click("#toggle-history")
            row = self.row(app, 2, "antigravity-4-fail")
            row.scroll_visible(animate=False)
            await p.pause()
            self.assertIn("NEW", row.card_text.plain)
            await p.click(row.query_one(Static), offset=(3, 0))
            self.assertEqual(len(app.screen_stack), 1)
            self.assertEqual((app.pane_index, app.selected().key), (2, row.key))
            self.assertNotIn("NEW", row.card_text.plain)
            self.assertTrue(next(j for j in self.store.snapshot()["jobs"] if j["id"] == row.key)["read"])

    async def test_right_click_selects_hit_row_and_opens_type_menu(self):
        app = self.app()
        async with app.run_test(size=(160, 45)) as p:
            for pane, key, actions in ((0, "后端服务", {"status", "edit", "detail", "archive"}),
                                       (0, "high", {"decide", "close"}),
                                       (1, "codex-0-running", {"detail"}),
                                       (2, "antigravity-4-fail", {"detail", "hide"})):
                if key.endswith("fail"):
                    await app.action_history()
                row = self.row(app, pane, key)
                await right_click(p, row.query_one(Static), offset=(3, 0))
                self.assertIsInstance(app.screen, ContextMenu)
                self.assertEqual((app.pane_index, app.selected().key), (pane, key))
                self.assertEqual({b.id.removeprefix("menu-") for b in app.screen.query(Button)}, actions)
                await p.press("escape")

    async def test_status_submenu_updates_clicked_project_after_refresh_reorders(self):
        app = self.app()
        async with app.run_test(size=(100, 32)) as p:
            await menu_action(p, self.row(app, 0, "后端服务"), "status")
            self.assertIsInstance(app.screen, ContextMenu)
            self.assertEqual({b.id for b in app.screen.query(Button)}, {"menu-status-0", "menu-status-1", "menu-status-2", "menu-status-3", "menu-back"})
            data = self.read_board()
            data["progress"].reverse()
            data["progress"][1]["stage"] = "Claude 同时改阶段"
            self.store.board.write_text(json.dumps(data))
            await app.refresh_data()
            await p.click("#menu-status-3")
            await p.pause()
            values = {d["project"]: d for d in self.read_board()["progress"]}
            self.assertEqual(values["后端服务"]["status"], "done")
            self.assertEqual(values["后端服务"]["stage"], "Claude 同时改阶段")
            self.assertEqual(values["aw 终端看板"]["status"], "running")
            self.assertEqual(values["后端服务"]["updated_by"], "user")

    async def test_secondary_menu_back_and_escape_never_write(self):
        app = self.app()
        before = self.store.board.read_bytes()
        async with app.run_test(size=(70, 24)) as p:
            await menu_action(p, self.row(app, 0, "后端服务"), "status")
            await p.click("#menu-back")
            self.assertIsInstance(app.screen, ContextMenu)
            self.assertTrue(app.screen.query("#menu-archive"))
            await p.press("escape")
            self.assertEqual(len(app.screen_stack), 1)
            self.assertTrue(app.is_running)
        self.assertEqual(self.store.board.read_bytes(), before)

    async def test_blank_panel_global_menu_never_uses_highlighted_row(self):
        app = self.app()
        async with app.run_test(size=(160, 45)) as p:
            # Use a truly empty list area, while a progress card is highlighted.
            other = Path(self.tmp.name) / "empty"
            other.mkdir()
            app.store.jobs = other
            await app.refresh_data()
            view = app.query_one("#list-2")
            await right_click(p, view, offset=(8, 10))
            self.assertIsInstance(app.screen, ContextMenu)
            self.assertEqual({b.id for b in app.screen.query(Button)}, {"menu-clear", "menu-archives"})
            await p.click("#menu-archives")
            self.assertTrue(app.archives)
            await right_click(p, app.query_one("#list-2"), offset=(8, 10))
            self.assertEqual(str(app.screen.query_one("#menu-archives", Button).label), "返回")
            await p.click("#menu-archives")
            self.assertFalse(app.archives)

    async def test_popup_clamps_edges_and_outside_click_is_consumed(self):
        app = self.app()
        async with app.run_test(size=(70, 24)) as p:
            pane = app.query_one("#pane-0")
            await right_click(p, pane, offset=(pane.region.width-1, pane.region.height-1))
            self.assertIsInstance(app.screen, ContextMenu)
            box = app.screen.query_one("#context-menu").region
            self.assertGreaterEqual(box.x, 0)
            self.assertGreaterEqual(box.y, 0)
            self.assertLessEqual(box.right, 70)
            self.assertLessEqual(box.bottom, 24)
            key = app.selected().key
            await p.click(offset=(0, 0))
            self.assertEqual(len(app.screen_stack), 1)
            self.assertEqual(app.selected().key, key)

    async def test_right_click_on_menu_button_does_not_activate(self):
        app = self.app()
        async with app.run_test(size=(160, 45)) as p:
            await right_click(p, self.row(app, 0, "后端服务"))
            await right_click(p, app.screen.query_one("#menu-archive"))
            self.assertIsInstance(app.screen, ContextMenu)
            await p.click("#menu-archive")
            self.assertIsInstance(app.screen, Confirm)
            await right_click(p, app.screen.query_one("#confirm-yes"))
            self.assertIsInstance(app.screen, Confirm)
            await p.click("#confirm-no")
            self.assertIn("后端服务", [d["project"] for d in self.read_board()["progress"]])

    async def test_removed_letter_bindings_are_inert_and_exit_is_ctrl_c(self):
        app = self.app()
        async with app.run_test(size=(160, 45)) as p:
            row = self.row(app, 0, "后端服务")
            row.scroll_visible(animate=False)
            await p.pause()
            await p.click(row)
            before = self.store.board.read_bytes()
            await p.press("e", "c", "x", "X", "v", "q")
            self.assertEqual(len(app.screen_stack), 1)
            self.assertFalse(app.archives)
            self.assertTrue(app.is_running)
            self.assertEqual(self.store.board.read_bytes(), before)
            self.assertEqual(str(app.query_one("#keys", Static).render()), "右键条目打开操作菜单")
            await p.press("ctrl+c")
            self.assertFalse(app.is_running)

    async def test_close_note_chinese_enter_does_not_save_until_mouse_confirm(self):
        app = self.app()
        async with app.run_test(size=(70, 24)) as p:
            before = self.store.board.read_bytes()
            await menu_action(p, self.row(app, 0, "high"), "close")
            self.assertIsInstance(app.screen, CloseDecision)
            await p.click("#close-closed")
            await p.click("#note-input")
            await p.press("用", "户", "已", "处", "理", "enter")
            self.assertIsInstance(app.screen, CloseDecision)
            self.assertEqual(self.store.board.read_bytes(), before)
            await p.click("#save-note")
            await p.pause()
            entry = next(d for d in self.read_board()["decisions"] if d["id"] == "high")
            self.assertEqual((entry["choice"], entry["note"], entry["seen_by_claude"]), ("关闭", "用户已处理", False))

    async def test_close_can_skip_optional_note_and_default_to_handled(self):
        app = self.app()
        async with app.run_test(size=(160, 45)) as p:
            await menu_action(p, self.row(app, 0, "high"), "close")
            await p.click("#save-note")
            await p.pause()
            entry = next(d for d in self.read_board()["decisions"] if d["id"] == "high")
            self.assertEqual((entry["choice"], entry["note"]), ("已处理", ""))

    async def test_scrolled_coordinates_after_resize_hide_correct_job(self):
        app = self.app()
        async with app.run_test(size=(160, 45)) as p:
            await p.click("#toggle-history")
            app.action_pane(2 - app.pane_index)
            await p.resize_terminal(70, 24)
            row = self.row(app, 2, "antigravity-7-ok")
            await menu_action(p, row, "hide")
            self.assertIsInstance(app.screen, Confirm)
            await p.click("#confirm-yes")
            await p.pause()
            jobs = {j["id"]: j for j in self.store.snapshot()["jobs"]}
            self.assertTrue(jobs["antigravity-7-ok"]["hidden"])
            self.assertFalse(jobs["antigravity-6-ok"]["hidden"])

