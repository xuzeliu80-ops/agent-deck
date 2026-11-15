"""v1.4: actionable progress, board age precision, and warning boundaries."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from rich.console import Console
from textual.widgets import ListView, Static

from aw.core import board_age, board_age_text, pet_status, progress_label
from aw.tui import DecisionNote, Detail, WatchApp, decision_text, progress_text
from fixtures import fixture_store


NOW_DT = datetime(2026, 11, 13, 13, tzinfo=timezone.utc)
NOW = NOW_DT.timestamp()


def ago(seconds):
    return (NOW_DT - timedelta(seconds=seconds)).isoformat()


def style_at(text, needle):
    return text.get_style_at_offset(Console(), text.plain.index(needle))


class BoardTimeTests(unittest.TestCase):
    def test_iso_duration_bands(self):
        for seconds, expected in ((0, '0 分钟'), (59, '0 分钟'), (60, '1 分钟'),
                                  (3599, '59 分钟'), (3600, '1 小时'),
                                  (86399, '23 小时'), (86400, '1 天'),
                                  (259200, '3 天'), (432000, '5 天')):
            with self.subTest(seconds=seconds):
                self.assertEqual(board_age_text(board_age(ago(seconds), NOW)), expected)

    def test_local_naive_datetime_and_date_midnight(self):
        # Fixed local now: no reliance on the machine's current clock or UTC offset.
        local_now = datetime(2026, 11, 13, 13).timestamp()
        self.assertEqual(board_age('2026-11-13T12:00', local_now), (3600, False))
        self.assertEqual(board_age('2026-11-13', local_now), (13 * 3600, True))
        self.assertEqual(board_age_text(board_age('2026-11-13', local_now)), '今天')
        self.assertEqual(board_age_text(board_age('2026-11-11', local_now)), '2 天')
        self.assertEqual(board_age('2026-11-13T12:00:00', local_now), (3600, False))

    def test_aware_offsets_and_z(self):
        for value in ('2026-11-13T20:00+08:00', '2026-11-13T04:00-08:00',
                      '2026-11-13T12:00Z'):
            self.assertEqual(board_age(value, NOW), (3600, False))

    def test_invalid_timestamps_are_silent_in_both_cards(self):
        for invalid in (None, '', 'yesterday', '2026', '2026-02-30',
                        '2026-11-13T99:00', '2026-11-13junk', 42, {}, []):
            with self.subTest(value=invalid):
                self.assertIsNone(board_age(invalid, NOW))
                d = decision_text(dict(title='决策', status='open', created=invalid), NOW)
                p = progress_text(dict(project='项目', status='running', updated=invalid), now=NOW)
                self.assertNotIn('已等', d.plain)
                self.assertNotIn('前更新', p.plain)
                self.assertNotIn('可能过时', p.plain)

    def test_future_dates_clamp_to_zero(self):
        self.assertEqual(board_age(ago(-3600), NOW), (0, False))
        local_now = datetime(2026, 11, 13, 13).timestamp()
        self.assertEqual(board_age_text(board_age('2026-11-14', local_now)), '今天')

    def test_decision_thresholds_only_color_wait_then_whole_title(self):
        for hours, seconds, warning, whole_red in ((24, 0, False, False), (24, 1, True, False),
                                                  (72, 0, True, False), (72, 1, True, True)):
            with self.subTest(hours=hours, seconds=seconds):
                d = decision_text(dict(title='决策', project='项目', priority='normal', status='open',
                                       created=ago(hours * 3600 + seconds)), NOW)
                self.assertEqual('!' in d.plain.splitlines()[0], warning)
                self.assertEqual(style_at(d, '已等').color.triplet.hex,
                                 '#ff838b' if warning else '#99aac2')
                self.assertEqual(style_at(d, '决策').color is not None, whole_red)
                if whole_red:
                    self.assertEqual(style_at(d, '·').color.triplet.hex, '#ff838b')
                    self.assertEqual(style_at(d, '决策').color.triplet.hex, '#ff838b')
                self.assertEqual(style_at(d, '项目').color.triplet.hex, '#99aac2')

    def test_decided_history_has_no_growing_wait_warning(self):
        d = decision_text(dict(title='决策', status='decided', created=ago(400000), choice='继续'), NOW)
        self.assertNotIn('已等', d.plain)
        self.assertNotIn('!', d.plain)

    def test_running_stale_boundary_and_other_states(self):
        for status in ('running', 'done', 'waiting_user', 'blocked'):
            for seconds in (86400, 86401):
                with self.subTest(status=status, seconds=seconds):
                    p = progress_text(dict(project='项目', status=status, updated=ago(seconds)), now=NOW)
                    stale = status == 'running' and seconds > 86400
                    self.assertIn('1 天前更新', p.plain.splitlines()[0])
                    self.assertEqual('可能过时' in p.plain, stale)
                    self.assertEqual(style_at(p, '前更新').color.triplet.hex,
                                     '#8096b3' if stale else '#99aac2')

    def test_date_only_cards_do_not_invent_hour_precision(self):
        now = datetime(2026, 11, 13, 13).timestamp()
        for date, expected in (('2026-11-13', '今天'), ('2026-11-12', '1 天')):
            d = decision_text(dict(title='决策', status='open', created=date), now)
            p = progress_text(dict(project='项目', status='running', updated=date), now=now)
            self.assertIn(expected, d.plain.splitlines()[0])
            self.assertIn(expected, p.plain.splitlines()[0])
            self.assertNotIn('小时', d.plain + p.plain)
            self.assertNotIn('今天前', p.plain)

    def test_long_titles_keep_age_visible_at_card_width(self):
        d = decision_text(dict(title='很长的标题' * 30, status='open', created=ago(3600)), NOW, 46)
        p = progress_text(dict(project='很长的项目' * 30, status='running', updated=ago(3600)), now=NOW, width=46)
        for text, expected in ((d, '已等 1 小时'), (p, '1 小时前更新')):
            first = text.plain.splitlines()[0]
            self.assertIn(expected, first)
            self.assertLessEqual(TextWidth(first), 46)
            self.assertIn('…', first)

    def test_progress_labels_count_only_this_projects_open_decisions(self):
        p = dict(project='文档站点', status='waiting_user')
        decisions = [dict(project='别处', status='open'), dict(project='文档站点', status='decided')]
        self.assertEqual(progress_label(p, decisions), '等你')
        self.assertEqual(style_at(progress_text(p, decisions, NOW), '等你').color.triplet.hex, '#e6d6a9')
        for count in (1, 2):
            decisions.append(dict(project='文档站点', status='open'))
            self.assertEqual(progress_label(p, decisions), f'等你拍板 ×{count}')
            self.assertEqual(style_at(progress_text(p, decisions, NOW), '等你拍板').color.triplet.hex, '#ffd37a')
        for status, label in (('running', '进行中'), ('blocked', '受阻'), ('done', '完成'), ('paused_external', 'paused_external')):
            self.assertEqual(progress_label(dict(project='无决策', status=status), decisions), label)

    def test_pet_count_does_not_treat_waiting_progress_as_decisions(self):
        snapshot = dict(jobs=[], decisions=[dict(status='decided', seen_by_claude=False)],
                        progress=[dict(project=str(i), status='waiting_user') for i in range(5)])
        self.assertEqual(pet_status(snapshot, 'claude')[2], '待拍板 0 · 新拍板 1')
        snapshot['decisions'].append(dict(status='open'))
        self.assertEqual(pet_status(snapshot, 'claude')[2], '待拍板 1 · 新拍板 1')


def TextWidth(value):
    from rich.cells import cell_len
    return cell_len(value)


class ProgressInteractionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = fixture_store(Path(self.tmp.name))
        self.snapshot = self.store.snapshot(now=NOW)
        self.progress = next(p for p in self.snapshot['progress'] if p['project'] == '后端服务')
        self.progress['updated'] = ago(23 * 3600)

    async def select_progress(self, app, pilot):
        view = app.query_one('#list-0', ListView)
        view.index = next(i for i, row in enumerate(view.children) if row.kind == 'progress' and row.key == '后端服务')
        await pilot.pause()
        return app.selected()

    async def test_waiting_without_open_enter_and_click_show_full_detail(self):
        # Same-project decided and other-project open entries must not misroute.
        self.snapshot['decisions'].append(dict(id='past', project='后端服务', status='decided'))
        before = self.store.board.read_bytes()
        app = WatchApp(self.store, no_notify=True, no_anim=True)
        with patch.object(self.store, 'snapshot', return_value=self.snapshot), patch.object(app, 'action_decide', wraps=app.action_decide) as decide:
            async with app.run_test(size=(160, 45)) as pilot:
                row = await self.select_progress(app, pilot)
                self.assertIn('后端服务 · 等你', row.card_text.plain)
                self.assertNotIn('等你拍板', row.card_text.plain)
                await pilot.press('enter')
                self.assertIsInstance(app.screen, Detail)
                for field in ('stage', 'next', 'updated'):
                    self.assertIn(self.progress[field], app.screen.body)
                await pilot.press('escape')
                await pilot.click(row)
                self.assertEqual(len(app.screen_stack), 1)
                await pilot.press('enter')
                self.assertIsInstance(app.screen, Detail)
                decide.assert_not_called()
                await pilot.press('enter')
                self.assertEqual(len(app.screen_stack), 1)
        self.assertEqual(self.store.board.read_bytes(), before)
        self.assertFalse(self.store.state.exists())

    async def test_waiting_one_open_enter_opens_matching_decision(self):
        self.snapshot['decisions'].append(dict(id='matching', title='拍板', project='后端服务', status='open'))
        app = WatchApp(self.store, no_notify=True, no_anim=True)
        with patch.object(self.store, 'snapshot', return_value=self.snapshot):
            async with app.run_test(size=(160, 45)) as pilot:
                row = await self.select_progress(app, pilot)
                self.assertIn('等你拍板 ×1', row.card_text.plain)
                await pilot.press('enter')
                self.assertIsInstance(app.screen, DecisionNote)
                self.assertEqual(app.screen.decision['id'], 'matching')

    async def test_waiting_multiple_open_enter_jumps_then_opens(self):
        self.snapshot['decisions'] += [dict(id=f'match-{i}', title='拍板', project='后端服务', status='open') for i in range(2)]
        app = WatchApp(self.store, no_notify=True, no_anim=True)
        with patch.object(self.store, 'snapshot', return_value=self.snapshot):
            async with app.run_test(size=(160, 45)) as pilot:
                row = await self.select_progress(app, pilot)
                self.assertIn('等你拍板 ×2', row.card_text.plain)
                await pilot.press('enter')
                self.assertEqual(app.selected().key, 'match-0')
                await pilot.press('enter')
                self.assertIsInstance(app.screen, DecisionNote)
                self.assertEqual(app.screen.decision['id'], 'match-0')

    async def test_refresh_keeps_selection_and_recomputes_age_and_open_label(self):
        app = WatchApp(self.store, no_notify=True, no_anim=True)
        with patch.object(self.store, 'snapshot', return_value=self.snapshot):
            async with app.run_test(size=(160, 45)) as pilot:
                row = await self.select_progress(app, pilot)
                self.assertIn('23 小时前更新', row.card_text.plain)
                self.snapshot['now'] += 2 * 3600
                self.progress['status'] = 'running'
                await app.refresh_data()
                self.assertIs(app.selected(), row)
                self.assertIn('可能过时', row.card_text.plain)
                await pilot.resize_terminal(100, 32)
                self.assertIn('可能过时', row.card_text.plain)
                self.progress['status'] = 'waiting_user'
                self.snapshot['decisions'].append(dict(id='new', title='拍板', project='后端服务', status='open'))
                await app.refresh_data()
                self.assertEqual(app.selected().key, '后端服务')
                self.assertIn('等你拍板 ×1', app.selected().card_text.plain)
                await pilot.press('enter')
                self.assertIsInstance(app.screen, DecisionNote)


if __name__ == '__main__':
    unittest.main()
