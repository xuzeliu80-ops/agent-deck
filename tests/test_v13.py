"""v1.3 acceptance; all writes go to generated files in temporary directories."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from rich.console import Console
from rich.text import Text
from textual.widgets import Input, ListView, Static

from aw import core, pets
from aw.tui import DecisionNote, Detail, UndoDecision, WatchApp, job_text
from fixtures import fixture_store, jsonl, make_board, make_job


class ExtractionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.card = self.root / '任务卡_示例.md'
        self.card.write_text('# 示例\n\n## 目标\n\n验证输入输出的边界。\n第二个目标。\n## 条件\n其他内容')

    def test_purpose_one_liner_precedes_card_and_missing_omitted(self):
        self.assertEqual(core.purpose_from_prompt('【一句话】**修复延迟**并复测', [self.card]), '修复延迟并复测')
        self.assertEqual(core.purpose_from_prompt('执行任务卡', [self.card]), '验证输入输出的边界。')
        self.assertEqual(core.purpose_from_prompt('没有目的'), '')
        self.assertEqual(core.purpose_from_prompt('引用缺失文件', [self.root / 'missing']), '')
        self.card.write_text('## 目标\n\n## 条件\n不属于目标')
        self.assertEqual(core.purpose_from_prompt('', [self.card]), '')

    def test_resolve_absolute_relative_bare_cards_and_job_cwd(self):
        sub = self.root / '子目录'
        sub.mkdir()
        other = sub / self.card.name
        other.write_text(self.card.read_text())
        for prompt, expected in ((f'请读 `{self.card}`', self.card),
                                 ('按 `子目录/任务卡_示例.md` 执行', other),
                                 ('任务卡_示例.md，工作在 `子目录/`', self.card)):
            self.assertEqual(core.task_cards(prompt, self.root), [expected])
        self.card.unlink()
        self.assertEqual(core.task_cards('`子目录/` 的 `任务卡_示例.md`', self.root), [other])
        self.assertEqual(core.task_cards('任务卡_不存在.md', self.root), [])

    def test_progress_newest_file_last_entry_and_stale_ignored(self):
        start = time.time()
        older = self.root / '进度_一.md'
        newer = self.root / '进度_二.md'
        older.write_text('- 旧进度。')
        newer.write_text('# 进度\n- [2026-11-13 10:00] 首条记录。\n- [2026-11-13 10:01] **解析通过**，正在复测。下一句。\n')
        os.utime(older, (start + 1, start + 1))
        os.utime(newer, (start + 2, start + 2))
        self.assertEqual(core.progress_sentence([self.card], start), '解析通过，正在复测。')
        os.utime(newer, (start - 1, start - 1))
        self.assertEqual(core.progress_sentence([self.card], start), '旧进度。')
        os.utime(older, (start, start))
        self.assertEqual(core.progress_sentence([self.card], start), '')

    def test_store_progress_over_model_then_stale_falls_back(self):
        job = make_job(self.root / 'jobs')
        meta = json.loads((job / 'meta.json').read_text())
        meta['cwd'] = str(self.root)
        (job / 'meta.json').write_text(json.dumps(meta))
        (job / 'prompt.txt').write_text('执行 `任务卡_示例.md`')
        progress = self.root / '进度_示例.md'
        progress.write_text('- 已完成解析，准备验收。')
        store = core.Store()
        info = store.job_info(job, time.time())
        self.assertEqual(info['purpose'], '验证输入输出的边界。')
        self.assertEqual(info['current'], '已完成解析，准备验收。')
        os.utime(progress, (meta['started'] - 1, meta['started'] - 1))
        self.assertEqual(store.job_info(job, time.time())['current'], '核心解析已完成，正在验证。')

    def test_recent_three_model_first_sentence_or_command(self):
        steps = ['回复：**正在复核** [输入](file:///tmp/a)。第二句话。', '命令：git status', '命令：pytest tests']
        self.assertEqual(core.latest_step(steps), '正在复核 输入。')
        self.assertEqual(core.latest_step(steps + ['命令：python -m unittest']), '跑测试')
        self.assertEqual(core.latest_step([]), '等待任务输出')
        self.assertEqual(core.latest_step(['文件：/tmp/边界.py']), '改文件 边界.py')
        self.assertEqual(core.latest_step(['读取：/tmp/边界.py']), '读代码 边界.py')
        self.assertEqual(core.latest_step(['工具：unknown']), '运行命令')

    def test_human_command_all_branches_shell_and_unclosed_quote(self):
        cases = {'python -m unittest': '跑测试', 'pytest -q': '跑测试',
                 '/usr/bin/git status': 'git 操作', '/bin/zsh -lc "cat src/core.py"': '读代码 core.py',
                 'rg -n hello src/core.py': '读代码 core.py', "grep hello a.py": '读代码 a.py',
                 "sed -n '1,4p' b.py": '读代码 b.py', 'head c.py': '读代码 c.py',
                 "apply_patch a.py": '改文件 a.py', 'echo ok > 结果.md': '改文件 结果.md',
                 'ssh host': '连虚拟机', 'scp x host:y': '连虚拟机',
                 'unknown --flag': '运行命令', "cat a.py '": '读代码 a.py'}
        for command, expected in cases.items():
            with self.subTest(command=command):
                self.assertEqual(core.human_command(command), expected)

    def test_result_first_sentence_strips_markdown_keeps_links_and_raw_detail(self):
        result = '# **检查完成**：[结果](reports/结果.md)已保存。\n第二句话。'
        job = make_job(self.root / 'jobs', state='ok')
        (job / 'last_message.txt').write_text(result)
        info = core.Store().job_info(job, time.time())
        self.assertEqual(info['result_summary'], '检查完成：结果已保存。')
        self.assertEqual(info['result'], result)
        agy = make_job(self.root / 'jobs', 'agy', agent='antigravity', state='ok', output=jsonl([
            dict(event='result', result=dict(response='开场白。\n\n**最终完成**。随后是建议。'))]))
        self.assertEqual(core.Store().job_info(agy, time.time())['result_summary'], '最终完成。')
        self.assertEqual(core.first_sentence(''), '')
        self.assertEqual(core.first_sentence('已修改文件：\n- 修复拍板入口。\n- 第二点。'), '已修改文件： 修复拍板入口。')

    def test_cards_rows_time_alignment_metadata_and_red_reason(self):
        job = core.Store().job_info(make_job(self.root / 'jobs'), time.time())
        job.update(title='边界验证', purpose='验证边界', current='跑测试')
        active = job_text(job, 48)
        self.assertEqual(len(active.plain.splitlines()), 3)
        self.assertEqual(Text(active.plain.splitlines()[0]).cell_len, 48)
        self.assertIn('▌目的：验证边界\n▌现在：跑测试', active.plain)
        for word in (job['id'], job['model'], 'tokens', job['effort']):
            self.assertNotIn(word, active.plain)
        for state in ('stalled', 'died'):
            job['state'] = state
            rendered = job_text(job, 90)
            self.assertIn('现在：' + core.status_reason(job), rendered.plain)
            self.assertEqual(rendered.get_style_at_offset(Console(), len(rendered.plain)-2).color.triplet.hex, '#ff838b')
        job.update(state='ok', ended=100, snapshot_now=220, result_summary='检查完成。')
        done = job_text(job, 48).plain
        self.assertEqual(len(done.splitlines()), 2)
        self.assertIn('2 分钟前结束', done)
        self.assertIn('NEW', done)
        self.assertIn('结果：检查完成。', done)

    def test_recent_dump_cli_contains_all_five_and_no_textual(self):
        jobs = self.root / 'jobs'
        for i in range(6):
            make_job(jobs, str(i), state='ok', now=time.time()+i)
        board = self.root / 'board.json'
        make_board(board)
        result = subprocess.run([sys.executable, '-m', 'aw', '--dump', '--recent', '5', '--jobs', str(jobs), '--board', str(board)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count('结果：'), 5)
        self.assertNotIn('0 中文任务', result.stdout)
        self.assertNotIn('tokens', result.stdout)


class PixelTests(unittest.TestCase):
    def style(self, row, x):
        return row.get_style_at_offset(Console(), x)

    def test_real_fox_cell_upper_lower_colors(self):
        rows = pets.pixel_rows('claude', 'busy')
        self.assertEqual(len(rows), 6)
        self.assertTrue(all(row.cell_len == 14 for row in rows))
        self.assertEqual(rows[1].plain[3], '▀')
        # Current immutable source pixel: row 2/3, column 3 is D over O.
        self.assertEqual(self.style(rows[1], 3).color.triplet.hex, '#a9502f')
        self.assertEqual(self.style(rows[1], 3).bgcolor.triplet.hex, '#e07b4f')

    def test_transparent_both_upper_only_and_lower_only(self):
        rows = pets.pixel_rows('claude', 'busy')
        for y, x, char, fg, bg in ((0, 0, ' ', pets.HEADER_BG, pets.HEADER_BG),
                                   (0, 1, '▄', '#e07b4f', pets.HEADER_BG),
                                   (4, 1, '▀', '#fbebdd', pets.HEADER_BG)):
            self.assertEqual(rows[y].plain[x], char)
            self.assertEqual(self.style(rows[y], x).color.triplet.hex, fg)
            self.assertEqual(self.style(rows[y], x).bgcolor.triplet.hex, bg)

    def test_busy_four_frames_wrap_and_other_badges_change(self):
        for agent in pets.DATA['names']:
            self.assertNotEqual(pets.pixel_rows(agent, 'busy', 0), pets.pixel_rows(agent, 'busy', 3))
            self.assertEqual(pets.pixel_rows(agent, 'busy', 0), pets.pixel_rows(agent, 'busy', 4))
            for state in ('idle', 'happy', 'error'):
                self.assertEqual(pets.pixel_rows(agent, state, 0), pets.pixel_rows(agent, state, 1))
                a = pets.header_text(agent, agent, state, '', '', 0)
                b = pets.header_text(agent, agent, state, '', '', 1)
                self.assertNotEqual(a.plain, b.plain)
                offset = a.plain.index(pets.DATA['badges'][state][0], a.plain.index('\n') + 1)
                self.assertEqual(a.get_style_at_offset(Console(), offset).color.triplet.hex,
                                 pets.DATA['badge_colors'][state].lower())


class NoLetterFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = fixture_store(Path(self.tmp.name))

    async def test_decide_second_note_and_undo_without_letter_keys(self):
        app = WatchApp(self.store, no_notify=True, no_anim=True)
        before = json.loads(self.store.board.read_text())
        original = next(d for d in before['decisions'] if d['id'] == 'high')
        keys = []
        async with app.run_test(size=(160, 45)) as pilot:
            press = pilot.press
            async def allowed(*values):
                keys.extend(values)
                self.assertTrue(all(k in {'up', 'down', 'left', 'right', 'enter', 'space', 'tab', 'escape'} or k.isdigit() or not k.isascii() for k in values))
                await press(*values)
            # Enter directly opens decision; no intermediate details screen.
            await allowed('enter', '2')
            self.assertIsInstance(app.screen, DecisionNote)
            self.assertEqual(app.screen.focused.id, 'note-input')
            await allowed('复', '核', '后', '再', '做')
            await pilot.click('#save-note')
            d = next(d for d in json.loads(self.store.board.read_text())['decisions'] if d['id'] == 'high')
            self.assertEqual(d['choice'], original['options'][1])
            self.assertEqual(d['note'], '复核后再做')
            self.assertEqual(d['status'], 'decided')
            self.assertEqual(d['decided_by'], 'user@aw')
            self.assertIs(d['seen_by_claude'], False)
            self.assertIsNotNone(core.datetime.fromisoformat(d['decided_at']).tzinfo)
            await allowed('enter')
            self.assertIsInstance(app.screen, UndoDecision)
            await pilot.click('#confirm-undo')
            restored = next(d for d in json.loads(self.store.board.read_text())['decisions'] if d['id'] == 'high')
            self.assertEqual(restored.pop('updated_by'), 'user')
            self.assertRegex(restored.pop('updated'), r'^\d{4}-\d\d-\d\dT\d\d:\d\d$')
            self.assertEqual(restored, original)
            self.assertEqual(json.loads(self.store.board.read_text())['progress'], before['progress'])
        self.assertFalse(any(len(k) == 1 and k.isascii() and k.isalpha() for k in keys))

    async def test_mouse_entry_options_cancel_and_keyboard_undo(self):
        app = WatchApp(self.store, no_notify=True, no_anim=True)
        async with app.run_test(size=(160, 45)) as pilot:
            row = app.selected()
            await pilot.click(row.query_one(Static))
            self.assertEqual(len(app.screen_stack), 1)
            await pilot.press('enter')
            self.assertIsInstance(app.screen, DecisionNote)
            self.assertIn('静态验证通过', str(app.screen.query_one('#decision-context').render()))
            await pilot.press('down', 'enter', 'enter')
            self.assertEqual(app.selected().data['choice'], '继续检查')
            await pilot.click(app.selected().query_one(Static))
            self.assertEqual(len(app.screen_stack), 1)
            await pilot.press('enter')
            self.assertIsInstance(app.screen, UndoDecision)
            await pilot.press('up', 'enter')
            self.assertEqual(app.selected().data['status'], 'open')
            await pilot.press('enter', 'escape')
            self.assertEqual(len(app.screen_stack), 1)

    async def test_tab_confirm_keyboard_only_in_narrow_modal(self):
        app = WatchApp(self.store, no_notify=True, no_anim=True)
        async with app.run_test(size=(70, 24)) as pilot:
            await pilot.press('enter', '2', '8', 'tab')
            self.assertEqual(app.screen.focused.id, 'save-note')
            await pilot.press('enter', 'enter', 'up', 'enter')
            self.assertEqual(app.selected().data['status'], 'open')

    async def test_click_tabs_history_refresh_and_escape_double_exit(self):
        app = WatchApp(self.store, no_notify=True, no_anim=True)
        async with app.run_test(size=(70, 24)) as pilot:
            await pilot.click('#tab-1')
            self.assertEqual(app.pane_index, 1)
            self.assertFalse(app.query_one('#header-1').display)
            self.assertIn('●', str(app.query_one('#tab-1').label))
            await pilot.click('#toggle-history')
            self.assertTrue(app.history)
            with patch.object(self.store, 'snapshot', wraps=self.store.snapshot) as read:
                await pilot.click('#refresh-board')
                read.assert_called_once()
            await pilot.press('escape')
            self.assertTrue(app.is_running)
            await pilot.press('escape')
            self.assertFalse(app.is_running)

    async def test_modal_escape_does_not_count_toward_exit(self):
        app = WatchApp(self.store, no_notify=True, no_anim=True)
        async with app.run_test() as pilot:
            await pilot.press('enter', 'escape', 'escape')
            self.assertTrue(app.is_running)
            await pilot.press('right', 'escape')
            self.assertTrue(app.is_running)
            app._last_escape = time.monotonic() - 2
            await pilot.press('escape')
            self.assertTrue(app.is_running)
