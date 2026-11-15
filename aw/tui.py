"""Textual presentation. All filesystem formats and writes live in core.py."""
from __future__ import annotations

from datetime import datetime
from dataclasses import dataclass
import re
import subprocess
import sys
import time

from rich.text import Text
from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, ListItem, ListView, Static

from .core import (Changes, Store, STATE_LABELS, PROGRESS_STATUSES, TERMINAL, archive_progress, update_progress, board_age, board_age_text,
                   decide, decisions_unseen, is_active, is_new, one_line, plain_text, pet_status, progress_label,
                   project_decisions, status_reason, ending_age, undo_decision, visible_jobs)
from .pets import COLORS as PET_COLORS, FRAME_SECONDS, header_text

COLORS = {"running": "#67b8ff", "stalled": "#ffd37a", "died": "#ff838b", "fail": "#ff838b", "ok": "#74ddad",
          "waiting_user": "#ffd37a", "blocked": "#ff838b", "done": "#74ddad"}


STATUS_COLORS = {"running": "#3d8bfd", "stalled": "#e0b13a", "died": "#f08a3c",
                 "ok": "#2fae74", "fail": "#e5534b"}
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def notify_macos(title, message):
    if sys.platform != "darwin":
        return
    def quote(value):
        return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ").replace("\r", " ") + '"'
    try:
        subprocess.run(["osascript", "-e", "display notification " + quote(message) + " with title " + quote(title)],
                       timeout=2, capture_output=True, check=False)
    except (OSError, subprocess.TimeoutExpired):
        pass


class Row(ListItem):
    def __init__(self, kind, key, data, text, *, now=None, decisions=()):
        super().__init__(disabled=kind == "heading")
        self.kind, self.key, self.data = kind, key, data
        self.card_text = text
        self.now, self.decisions = now, decisions
        self.frame = 0

    def compose(self):
        yield Static(self.card_text, markup=False, classes="job-card" if self.kind == "job" else "")

    def update_card(self):
        if self.kind == "job":
            self.card_text = job_text(self.data, max(1, self.content_size.width), now=self.now, frame=self.frame)
        elif self.kind == "decision":
            self.card_text = decision_text(self.data, self.now, max(1, self.content_size.width))
        elif self.kind == "progress":
            self.card_text = progress_text(self.data, self.decisions, self.now, max(1, self.content_size.width))
        self.query_one(Static).update(self.card_text)

    def on_resize(self):
        if self.is_mounted:
            self.update_card()

    def _on_click(self, event: events.Click):
        # ListItem normally activates on every mouse button. Keep selection and
        # activation separate, and consume the event before its default handler.
        event.prevent_default()
        event.stop()
        if self.kind != "heading" and event.button in (1, 3):
            self.app.select_row(self)
        if event.button == 3:
            self.app.open_context_menu(self if self.kind != "heading" else None,
                                       event.screen_x, event.screen_y)


@dataclass(frozen=True)
class MenuTarget:
    kind: str
    key: str
    data: dict
    archived: bool


class MenuButton(Button):
    def _on_click(self, event: events.Click):
        event.prevent_default()
        event.stop()
        if event.button == 1:
            self.press()


class ContextMenu(ModalScreen):
    """Mouse-only popup; the modal backdrop consumes outside clicks."""
    BINDINGS = [("escape", "cancel", "关闭菜单")]

    def __init__(self, options, x, y, title="操作"):
        super().__init__()
        self.options, self.anchor, self.menu_title = options, (x, y), title

    def compose(self):
        with VerticalScroll(id="context-menu"):
            yield Static(Text(self.menu_title, style="bold #9dccff"))
            for action, label in self.options:
                yield MenuButton(Text(label), id="menu-" + action, classes="context-option")

    def on_mount(self):
        self.place_menu()

    def on_resize(self):
        if self.is_mounted and self.query("#context-menu"):
            self.place_menu()

    def place_menu(self):
        box = self.query_one("#context-menu")
        width = min(36, self.app.size.width)
        height = min(len(self.options) + 3, self.app.size.height)
        box.styles.width, box.styles.height = width, height
        box.styles.offset = (max(0, min(self.anchor[0], self.app.size.width - width)),
                             max(0, min(self.anchor[1], self.app.size.height - height)))

    def action_cancel(self):
        self.dismiss(None)

    def on_click(self, event: events.Click):
        event.stop()
        if not self.query_one("#context-menu").region.contains(event.screen_x, event.screen_y):
            self.dismiss(None)

    @on(Button.Pressed)
    def pressed(self, event):
        event.stop()
        self.dismiss(event.button.id.removeprefix("menu-"))


class Detail(ModalScreen):
    BINDINGS = [("escape", "dismiss", "返回"),
                Binding("enter,space", "dismiss", "关闭", priority=True)]

    def __init__(self, title, body, job=False):
        super().__init__()
        self.detail_title, self.body = title, body
        self.job = job

    def compose(self):
        with Vertical(id="detail-dialog"):
            yield Static(Text(self.detail_title, style="bold #9dccff"), id="detail-title")
            with VerticalScroll(id="detail-scroll"):
                body = Text(self.body)
                separator = "—— 以下为原始记录 ——"
                if separator in self.body:
                    start = self.body.index(separator)
                    body.stylize("#8096b3", start, start + len(separator))
                yield Static(body, markup=False)
            yield Button("关闭 (Enter / Esc)", id="close-detail")

    @on(Button.Pressed, "#close-detail")
    def close(self):
        self.dismiss()


class NavigableModal(ModalScreen):
    def action_focus_next(self):
        self.focus_next()

    def action_focus_previous(self):
        self.focus_previous()


class DecisionNote(NavigableModal):
    BINDINGS = [Binding("escape", "cancel", "取消"), Binding("tab", "focus_next", "下一项", priority=True),
                Binding("shift+tab", "focus_previous", "上一项", priority=True)]

    def __init__(self, decision):
        super().__init__()
        self.decision = decision
        self.options = decision.get("options", [])
        self.option_index = None
        self.choice = None

    def compose(self):
        with Vertical(id="note-dialog"):
            yield Static(Text("拍板：" + str(self.decision.get("title", "")), style="bold"))
            yield Static(Text("背景：" + str(self.decision.get("context") or "暂无背景")), id="decision-context")
            yield Static(Text("推荐：" + str(self.decision.get("recommend") or "无"), style="#ffd37a"))
            if self.options:
                yield Static("数字 1–9 选择；Tab / ↑ ↓ 移动后 Enter 选择。")
                with VerticalScroll(id="option-list"):
                    for i, option in enumerate(self.options):
                        label = f"{i+1}. {option}" + (" （推荐）" if option == self.decision.get("recommend") else "")
                        yield Button(label, id=f"option-{i}", classes="option-button")
                    yield Button("其他：自由输入", id="choose-other", classes="option-button")
            if self.decision.get("recommend") and self.decision["recommend"] not in self.options:
                yield Static(Text("推荐：" + str(self.decision["recommend"]), style="#ffd37a"))
            yield Input(placeholder="填写其他答案" if self.options else "填写拍板答案（必填）", id="choice-input", disabled=bool(self.options))
            yield Input(placeholder="备注（可留空）；Enter 确认，Esc 取消", id="note-input")
            yield Static("", id="choice-error")
            with Horizontal(id="note-buttons"):
                yield Button("确认拍板", variant="primary", id="save-note")
                yield Button("取消", id="cancel-note")

    def on_mount(self):
        self.query_one("#option-0" if self.options else "#choice-input").focus()

    def action_cancel(self):
        self.dismiss(None)

    def choose(self, index):
        self.option_index = index
        self.choice = self.options[index] if index is not None else None
        for i in range(len(self.options)):
            self.query_one(f"#option-{i}", Button).variant = "success" if i == index else "default"
        field = self.query_one("#choice-input", Input)
        field.disabled = index is not None
        field.value = self.choice or ""
        self.query_one("#choice-error", Static).update("")
        (self.query_one("#note-input", Input) if index is not None else field).focus()

    def on_key(self, event):
        # Digits in free answers/notes belong to the input, never to shortcuts.
        if not isinstance(self.focused, Input):
            if event.key in "123456789" and len(event.key) == 1 and int(event.key) <= len(self.options):
                event.stop()
                event.prevent_default()
                self.choose(int(event.key) - 1)
            elif event.key in ("up", "down"):
                event.stop()
                event.prevent_default()
                self.focus_previous() if event.key == "up" else self.focus_next()

    def save(self):
        choice = self.choice if self.option_index is not None else self.query_one("#choice-input", Input).value.strip()
        if not choice:
            self.query_one("#choice-error", Static).update("请先选择选项或填写答案")
            return
        self.dismiss((choice, self.query_one("#note-input", Input).value, self.option_index))

    @on(Input.Submitted)
    def submitted(self, event):
        if event.input.id == "choice-input" and event.value.strip():
            self.query_one("#note-input", Input).focus()
        else:
            self.save()

    @on(Button.Pressed)
    def pressed(self, event):
        key = event.button.id
        if key.startswith("option-"):
            self.choose(int(key.split("-")[1]))
        elif key == "choose-other":
            self.choose(None)
        elif key == "save-note":
            self.save()
        elif key == "cancel-note":
            self.dismiss(None)


class UndoDecision(NavigableModal):
    BINDINGS = [("escape", "cancel", "取消"), ("y", "confirm", "确认撤销"),
                Binding("up", "focus_previous", "上一项", priority=True),
                Binding("down", "focus_next", "下一项", priority=True)]

    def __init__(self, decision):
        super().__init__()
        self.decision = decision
        self.body = "选择：" + str(decision.get("choice", "未记录")) + "\n备注：" + str(decision.get("note", ""))

    def compose(self):
        with Vertical(id="note-dialog"):
            yield Static(Text("撤销拍板：" + str(self.decision.get("title", "")), style="bold #ffd37a"))
            yield Static(Text("选择：" + str(self.decision.get("choice", "未记录")) + "\n备注：" + str(self.decision.get("note", ""))))
            yield Static("确认恢复为待拍板？本次选择和备注将删除。")
            with Horizontal(id="note-buttons"):
                yield Button("撤销此拍板", variant="warning", id="confirm-undo")
                yield Button("取消 (Esc)", id="cancel-undo")

    def on_mount(self):
        self.query_one("#cancel-undo", Button).focus()

    def action_cancel(self):
        self.dismiss(False)

    def action_confirm(self):
        self.dismiss(True)

    @on(Button.Pressed)
    def pressed(self, event):
        self.dismiss(event.button.id == "confirm-undo")


class Confirm(NavigableModal):
    BINDINGS = [("escape", "no", "取消")]

    def __init__(self, message):
        super().__init__()
        self.message = message

    def compose(self):
        with Vertical(id="note-dialog"):
            yield Static(Text(self.message))
            with Horizontal(id="note-buttons"):
                yield MenuButton("确定", id="confirm-yes", variant="warning")
                yield MenuButton("取消", id="confirm-no")

    def on_mount(self):
        self.query_one("#confirm-no").focus()

    def action_yes(self):
        self.dismiss(True)

    def action_no(self):
        self.dismiss(False)

    @on(Button.Pressed)
    def pressed(self, event):
        self.dismiss(event.button.id == "confirm-yes")


class EditProgress(NavigableModal):
    BINDINGS = [("escape", "cancel", "取消"),
                Binding("tab", "focus_next", "下一项", priority=True),
                Binding("shift+tab", "focus_previous", "上一项", priority=True)]

    def __init__(self, progress, statuses):
        super().__init__()
        self.progress = progress
        self.statuses = list(dict.fromkeys([*PROGRESS_STATUSES, *statuses]))
        self.status = progress.get("status", "running")

    def compose(self):
        with Vertical(id="note-dialog"):
            yield Static(Text("修改进度：" + str(self.progress.get("project", "")), style="bold"))
            yield Static("选择状态；Tab 移到阶段和下一步。只写回实际修改的字段。")
            with VerticalScroll(id="option-list"):
                for i, status in enumerate(self.statuses):
                    yield Button(f"{i+1}. {PROGRESS_STATUSES.get(status, status)} ({status})",
                                 variant="success" if status == self.status else "default", id=f"status-{i}", classes="option-button")
            yield Static("当前阶段")
            yield Input(value=str(self.progress.get("stage", "")), id="stage-input")
            yield Static("下一步")
            yield Input(value=str(self.progress.get("next", "")), id="next-input")
            with Horizontal(id="note-buttons"):
                yield Button("保存", id="save-progress", variant="primary")
                yield Button("取消", id="cancel-progress")

    def on_mount(self):
        self.query_one(f"#status-{self.statuses.index(self.status)}").focus()

    def action_cancel(self):
        self.dismiss(None)

    def choose(self, index):
        self.status = self.statuses[index]
        for i, status in enumerate(self.statuses):
            self.query_one(f"#status-{i}", Button).variant = "success" if status == self.status else "default"
        self.query_one("#stage-input").focus()

    def on_key(self, event):
        if not isinstance(self.focused, Input) and event.key in "123456789" and len(event.key) == 1:
            index = int(event.key) - 1
            if index < len(self.statuses):
                event.stop()
                event.prevent_default()
                self.choose(index)

    def save(self):
        fields = dict(status=self.status, stage=self.query_one("#stage-input", Input).value,
                      next=self.query_one("#next-input", Input).value)
        self.dismiss({k: v for k, v in fields.items() if v != self.progress.get(k, "")})

    @on(Input.Submitted)
    def submitted(self):
        self.save()

    @on(Button.Pressed)
    def pressed(self, event):
        key = event.button.id
        if key.startswith("status-"):
            self.choose(int(key.split("-")[1]))
        elif key == "save-progress":
            self.save()
        else:
            self.action_cancel()


class CloseDecision(NavigableModal):
    BINDINGS = [("escape", "cancel", "取消"),
                Binding("tab", "focus_next", "下一项", priority=True),
                Binding("shift+tab", "focus_previous", "上一项", priority=True)]

    def __init__(self, decision):
        super().__init__()
        self.decision = decision
        self.choice = "已处理"

    def compose(self):
        with Vertical(id="note-dialog"):
            yield Static(Text("处理/关闭：" + str(self.decision.get("title", "")), style="bold"))
            with Horizontal(id="note-buttons"):
                yield Button("已处理", id="close-handled", variant="success")
                yield Button("关闭", id="close-closed")
            yield Input(placeholder="备注可留空；中文组词完成后点确定", id="note-input")
            with Horizontal(classes="note-actions"):
                yield Button("确定（可跳过备注）", id="save-note", variant="primary")
                yield Button("取消", id="cancel-note")

    def on_mount(self):
        self.query_one("#save-note").focus()

    def action_cancel(self):
        self.dismiss(None)

    @on(Input.Submitted)
    def submitted(self):
        # Terminal protocols don't expose IME preedit. Never save on an Input
        # Enter: it may be the key used to commit a Chinese candidate.
        self.query_one("#save-note").focus()

    @on(Button.Pressed)
    def pressed(self, event):
        key = event.button.id
        if key in ("close-handled", "close-closed"):
            self.choice = "已处理" if key == "close-handled" else "关闭"
            self.query_one("#close-handled", Button).variant = "success" if key == "close-handled" else "default"
            self.query_one("#close-closed", Button).variant = "success" if key == "close-closed" else "default"
        elif key == "save-note":
            self.dismiss((self.choice, self.query_one("#note-input", Input).value, None))
        else:
            self.action_cancel()


def decision_text(d, now=None, width=80):
    text = Text(no_wrap=True, overflow="ellipsis")
    age = board_age(d.get("created"), now) if d.get("status") == "open" else None
    wait = " · 已等 " + board_age_text(age) if age else ""
    if age and age[0] > 86400:
        wait += " !"
    text.append("● " if d.get("priority") == "high" else "· ", style="#ff838b" if d.get("priority") == "high" else "#a4b0c2")
    text.append(clipped(one_line(d.get("title", "无标题")), width - 2 - Text(wait).cell_len), style="bold")
    text.append(wait, style="#ff838b" if age and age[0] > 86400 else "#99aac2")
    if age and age[0] > 72 * 3600:
        text.stylize("#ff838b")
    summary = ("已选：" + str(d.get("choice", "未记录")) if d.get("status") == "decided" else
               "推荐：" + str(d.get("recommend") or "无"))
    text.append("\n" + str(d.get("project", "")) + " · " + summary, style="#99aac2")
    return text


def progress_text(p, decisions=(), now=None, width=80):
    opened = project_decisions(p.get("project"), decisions)
    age = board_age(p.get("updated"), now)
    freshness = ""
    if age:
        duration = board_age_text(age)
        freshness = " · " + duration + ("更新" if duration == "今天" else "前更新")
    stale = p.get("status") == "running" and age is not None and age[0] > 86400
    if stale:
        freshness += " · 可能过时"
    label = " · " + progress_label(p, decisions)
    project = clipped(str(p.get("project", "")), width - Text(label + freshness).cell_len)
    color = "#ffd37a" if opened else "#e6d6a9" if p.get("status") == "waiting_user" else COLORS.get(p.get("status"), "white")
    text = Text(project + label, style=color)
    text.append(freshness, style="#8096b3" if stale else "#99aac2")
    text.append("\n" + str(p.get("stage", "")) + "\n→ " + str(p.get("next", "")), style="#cbd5e5")
    return text


def clipped(value, width):
    text = Text(value)
    text.truncate(max(0, width), overflow="ellipsis")
    return text.plain


def job_text(j, width=80, *, now=None, frame=0):
    now = j.get("snapshot_now", time.time()) if now is None else now
    active, state = is_active(j), j["state"]
    new = is_new(j, now)
    color = STATUS_COLORS.get(state, "#99aac2")
    bar = state == "running" or new
    content_width = max(0, width - (1 if bar else 0))
    if active:
        limit = j["limit_min"]
        fraction = min(1, max(0, j["elapsed_min"] / limit)) if limit and limit > 0 else 0
        filled = round(fraction * 4)
        right = f"{j['elapsed_min']:g}/{limit:g} 分钟" if limit else f"{j['elapsed_min']:g}/? 分钟"
        right += " " + "━" * filled + "─" * (4 - filled)
    else:
        right = ending_age(j, now)
    text = Text(no_wrap=True, overflow="ellipsis")

    def left_edge():
        if bar:
            text.append("▌", style=color)

    left_edge()
    if state == "running":
        text.append(SPINNER[frame % len(SPINNER)] + " ", style=color)
    text.append(" " + STATE_LABELS.get(state, state) + " ", style=f"#101721 on {color}")
    if new:
        text.append(" ")
        text.append(" NEW ", style="bold #101721 on #fff1a8")
    text.append(" ")
    prefix_width = text.cell_len
    title = clipped(one_line(j["title"], 1000), width - prefix_width - Text(right).cell_len - 1)
    text.append(title, style="bold #d8e2f1" if state == "running" or new else "#aebed2")
    text.append(" " * max(1, width - text.cell_len - Text(right).cell_len) + right, style="#99aac2")

    def line(value, style):
        text.append("\n")
        left_edge()
        text.append(clipped(value, content_width), style=style)

    if active:
        if j.get("purpose"):
            line("目的：" + j["purpose"], "#aebed2")
        line("现在：" + status_reason(j), "#ff838b" if state in ("stalled", "died") else "#d8e2f1")
    else:
        line("结果：" + (j["result_summary"] or "暂无最终回复"), "#d8e2f1")
    return text


def job_detail(j):
    """Human summary first, followed by the existing diagnostic record."""
    paragraph = re.split(r"\n\s*\n", str(j.get("result") or "").strip())[0]
    result = one_line(plain_text(paragraph), 1000)
    if j["state"] == "fail":
        reason = one_line(plain_text(j.get("failure_reason")), 1000) or result or "未记录失败原因"
        result = f"失败（退出码 {j.get('rc', '?')}）：{reason}"
    elif j["state"] == "died":
        result = status_reason(j)
    elif j["state"] in ("running", "stalled"):
        result = "尚未结束；" + status_reason(j)
    body = ("目的：" + one_line(j.get("purpose") or "未提供目的", 1000) +
            "\n结果：" + (result or "暂无最终回复") +
            f"\n用时：{j['elapsed_min']:g} 分钟 · {j['model']} · {j['effort']}\n\n" +
            "—— 以下为原始记录 ——\n" +
            f"{j['id']} · {STATE_LABELS[j['state']]}\ntokens {j['tokens']}\n\nPrompt（前 40 行）\n{j['prompt']}\n\n最近 15 步\n")
    body += "\n\n".join(j["steps"]) or "暂无可解析步骤"
    result = j["result"]
    if j["agent"] == "antigravity":
        result = "\n".join(result.splitlines()[-60:])
    body += "\n\n最终回复 / 结果" + ("（末 60 行）" if j["agent"] == "antigravity" else "") + "\n"
    body += (result or "暂无结果") if j["state"] not in ("running", "stalled") else "任务仍在运行；最新回复见最近步骤"
    return body


class WatchApp(App):
    TITLE = "aw · agents 看板"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [Binding("left", "pane(-1)", "上一栏"), Binding("right", "pane(1)", "下一栏"),
                Binding("tab", "navigate(1)", "下一栏", priority=True), Binding("shift+tab", "navigate(-1)", "上一栏", priority=True),
                ("d", "decide", "拍板"), ("u", "undo", "撤销拍板"), ("h", "history", "历史"),
                ("r", "refresh", "刷新"), Binding("ctrl+c", "quit", "退出", priority=True),
                ("escape", "escape", "连按两次退出")]
    CSS = """
    Screen { background: #101721; color: #d8e2f1; }
    #top { height: auto; min-height: 3; padding: 1 2; background: #1c2a3c; }
    #nav { display: none; height: auto; min-height: 1; padding: 0 2; background: #172333; }
    #columns { height: 1fr; layout: horizontal; }
    .pane { width: 1fr; height: 1fr; border: round #33465e; margin: 0 1; }
    .pane:focus-within { border: round #79bdff; }
    .pane-title { height: 6; padding: 0 1; text-style: bold; color: #b9d9ff; }
    ListView { height: 1fr; background: transparent; }
    ListItem { height: auto; padding: 0 1; margin-bottom: 1; background: transparent; }
    ListItem.-highlight { background: #1c2e42; }
    ListView:focus > ListItem.-highlight { background: #283f59; }
    ListItem:disabled { margin-bottom: 0; opacity: 1; padding: 0 1; border-bottom: none; color: #8096b3; }
    ListItem Static { height: auto; }
    .job-card { text-wrap: nowrap; text-overflow: ellipsis; }
    #status { height: auto; max-height: 3; padding: 0 2; color: #ffd37a; }
    #tab-buttons { display: none; height: 1; background: #172333; }
    #tab-buttons Button { width: 1fr; min-width: 10; height: 1; border: none; padding: 0; }
    #controls { height: 1; background: #1c2a3c; }
    #controls Button { height: 1; min-width: 12; border: none; padding: 0 1; margin-right: 2; }
    #keys { height: auto; min-height: 2; padding: 0 2; background: #1c2a3c; color: #aebed2; }
    .narrow #tab-buttons { display: block; }
    .narrow .pane { display: none; margin: 0 1; width: 1fr; }
    .narrow .pane-title { display: none; }
    .narrow .pane.active-pane { display: block; }
    ModalScreen { align: center middle; background: #000000 65%; }
    #detail-dialog { width: 90%; height: 90%; padding: 1 2; border: round #79bdff; background: #172333; }
    #detail-title { height: auto; max-height: 5; margin-bottom: 1; }
    #detail-scroll { height: 1fr; }
    #detail-scroll Static { height: auto; }
    #close-detail { margin-top: 1; }
    #decision-context { max-height: 4; overflow-y: auto; }
    DecisionNote, UndoDecision { padding: 0 4; }
    #note-dialog { width: 90; max-width: 100%; max-height: 95%; height: auto; padding: 1 2; border: round #ffd37a; background: #172333; }
    #note-dialog > * { width: 100%; min-width: 0; }
    #note-dialog Static { height: auto; }
    #option-list { height: auto; max-height: 12; margin-bottom: 1; }
    .option-button { width: 100%; min-width: 0; height: auto; min-height: 1; border: none; text-align: left; padding: 0 1; }
    #choice-error { color: #ff838b; }
    #note-buttons { height: auto; margin-top: 1; }
    #note-buttons Button { width: 1fr; min-width: 0; margin-right: 2; }
    .note-actions { height: auto; margin-top: 1; }
    .note-actions Button { width: 1fr; min-width: 0; margin-right: 2; }
    ContextMenu { align: left top; background: transparent; }
    #context-menu { border: round #79bdff; background: #22344b; padding: 0; }
    #context-menu Static { height: 1; padding: 0 1; }
    .context-option { width: 100%; min-width: 0; height: 1; border: none; padding: 0 1; text-align: left; }
    .context-option:hover, .context-option:focus { background: #3a587c; }
    """

    def __init__(self, store=None, no_notify=False, no_anim=False):
        super().__init__()
        self.store = store or Store()
        self.no_notify = no_notify
        self.no_anim = no_anim
        self.pet_frame = 0
        self.snapshot_data = None
        self.history = False
        self.archives = False
        self.pane_index = 0
        self.changes = Changes()
        self._refreshing = False
        self._terminal_width = None
        self._last_escape = None

    def compose(self) -> ComposeResult:
        yield Static(id="top")
        with Horizontal(id="tab-buttons"):
            for i, (agent, name) in enumerate((("claude", "Claude"), ("codex", "GPT"), ("antigravity", "Gemini"))):
                yield Button(Text("● " + name, style="bold " + PET_COLORS[agent]), id=f"tab-{i}")
        yield Static(id="nav")
        with Container(id="columns"):
            for index, title in enumerate(("Claude · 拍板与进度", "GPT · Codex", "Gemini · Antigravity")):
                with Vertical(id=f"pane-{index}", classes="pane"):
                    yield Static(title, id=f"header-{index}", classes="pane-title")
                    yield ListView(id=f"list-{index}")
        yield Static(id="status")
        with Horizontal(id="controls"):
            yield Button("刷新 (r)", id="refresh-board")
            yield Button("全部历史 (h)", id="toggle-history")
        yield Static("右键条目打开操作菜单", id="keys")

    async def on_mount(self):
        await self.refresh_data()
        self.update_layout()
        self.set_interval(5, self.refresh_data)
        if not self.no_anim:
            self.set_interval(FRAME_SECONDS, self.animate_pets)
        self.query_one("#list-0", ListView).focus()

    def on_resize(self, event):
        self._terminal_width = event.size.width
        if self.is_mounted:
            self.update_layout()

    def update_layout(self):
        self.set_class((self._terminal_width or self.size.width) < 120, "narrow")
        for i in range(3):
            self.query_one(f"#pane-{i}").set_class(i == self.pane_index, "active-pane")
        self.update_headers()

    def animate_pets(self):
        if not self.query("#tab-0"):
            return  # The animation timer may fire while the app is unmounting.
        self.pet_frame += 1
        self.update_headers()
        for row in self.query(Row):
            if row.kind == "job" and row.data["state"] == "running":
                row.frame = self.pet_frame
                row.update_card()

    def update_headers(self):
        if not self.snapshot_data:
            return
        nav = Text(no_wrap=True, overflow="ellipsis")
        narrow = self.has_class("narrow")
        for i, (agent, name) in enumerate((("claude", "Claude"), ("codex", "GPT"), ("antigravity", "Gemini"))):
            state, label, summary = pet_status(self.snapshot_data, agent)
            color = PET_COLORS[agent]
            nav.append(" ▸ " if i == self.pane_index else "   ", style="bold #9dccff")
            nav.append(name, style="bold " + color)
            self.query_one(f"#tab-{i}", Button).variant = "primary" if i == self.pane_index else "default"
            self.query_one(f"#header-{i}", Static).update(
                header_text(agent, name, state, label, summary, self.pet_frame))
        if narrow:
            agent = ("claude", "codex", "antigravity")[self.pane_index]
            _, label, summary = pet_status(self.snapshot_data, agent)
            nav.append("\n" + label + " · " + summary, style="#aebed2")
        nav.append("   · " + ("归档区（右键恢复 / 空白处右键返回）" if self.archives else "全部历史" if self.history else "最近结束各 2 条"), style="#8298b4")
        self.query_one("#nav", Static).update(nav)
        self.query_one("#toggle-history", Button).label = "最近结束 (h)" if self.history else "全部历史 (h)"

    def action_navigate(self, delta):
        if len(self.screen_stack) > 1:
            self.screen.focus_previous() if delta < 0 else self.screen.focus_next()
        else:
            self.action_pane(delta)

    def action_pane(self, delta):
        if len(self.screen_stack) > 1:
            return
        self.pane_index = (self.pane_index + delta) % 3
        self.update_layout()
        self.query_one(f"#list-{self.pane_index}", ListView).focus()
        self.read_selected(self.selected())

    @on(ListView.Highlighted)
    def highlighted(self, event):
        # Mouse focus also determines which pane decision shortcuts operate on.
        if event.list_view.has_focus:
            self.pane_index = int(event.list_view.id[-1])
            self.update_layout()
            if not self._refreshing and len(self.screen_stack) == 1:
                self.read_selected(event.item)

    def read_selected(self, row):
        if not row or row.kind != "job" or row.data["state"] not in TERMINAL or row.data.get("read"):
            return
        try:
            self.store.mark_read(row.data)
            row.data["read"] = True
            if isinstance(row, Row):
                row.update_card()
            self.update_headers()
        except (OSError, ValueError, RuntimeError, TypeError) as exc:
            self.query_one("#status", Static).update(Text("已读未保存：" + str(exc)))

    def selected(self):
        view = self.query_one(f"#list-{self.pane_index}", ListView)
        return view.highlighted_child

    def select_row(self, row):
        view = row.parent
        self.pane_index = int(view.id[-1])
        view.focus()
        view.index = list(view.children).index(row)
        self.read_selected(row)
        self.update_layout()

    def on_click(self, event: events.Click):
        if event.button != 3 or len(self.screen_stack) > 1:
            return
        widget, _ = self.get_widget_at(event.screen_x, event.screen_y)
        while widget is not None:
            if widget.has_class("pane"):
                event.stop()
                self.open_context_menu(None, event.screen_x, event.screen_y)
                return
            widget = widget.parent

    def context_options(self, target):
        if target is None:
            return [("clear", "隐藏所有已结束任务"),
                    ("archives", "返回" if self.archives else "查看归档")]
        if target.archived:
            return [("restore", "恢复"), ("detail", "查看详情")]
        if target.kind == "progress":
            return [("status", "改状态 ▶"), ("edit", "编辑阶段 / 下一步"),
                    ("detail", "查看详情"), ("archive", "归档")]
        if target.kind == "decision":
            return ([("decide", "拍板"), ("close", "处理 / 关闭")]
                    if target.data.get("status") == "open" else [("undo", "查看结果 / 撤销拍板")])
        return [("detail", "查看详情")] + ([("hide", "隐藏")] if target.data["state"] in TERMINAL else [])

    def open_context_menu(self, row, x, y):
        if len(self.screen_stack) > 1:
            return
        self._last_escape = None
        # Capture the clicked identity, never the current highlight: a refresh
        # may reorder/unmount cards while this menu remains open.
        target = MenuTarget(row.kind, row.key, row.data.copy(), self.archives) if row else None
        self.show_context_menu(target, x, y)

    def show_context_menu(self, target, x, y, statuses=None):
        options = ([(f"status-{i}", ("✓ " if value == target.data.get("status") else "  ") +
                     PROGRESS_STATUSES.get(value, value)) for i, value in enumerate(statuses)] + [("back", "‹ 返回操作菜单")]
                   if statuses is not None else self.context_options(target))
        async def picked(action):
            if action is None:
                return
            if action == "status":
                values = list(dict.fromkeys([*PROGRESS_STATUSES, *[p["status"] for p in self.snapshot_data["progress"] if isinstance(p.get("status"), str)]]))
                self.show_context_menu(target, x, y, values)
            elif action == "back":
                self.show_context_menu(target, x, y)
            elif action.startswith("status-"):
                value = statuses[int(action.split("-")[1])]
                self.save_operation(lambda: update_progress(self.store.board, target.data["project"], {"status": value}))
            elif action == "archives":
                await self.action_archives()
            elif action == "clear":
                self.action_clear_finished()
            elif action == "edit":
                self.action_edit_progress(target)
            elif action == "close":
                self.action_close_decision(target)
            elif action in ("archive", "hide", "restore"):
                self.action_archive(target, restore=target.archived)
            elif action == "decide":
                self.action_decide(target)
            elif action == "undo":
                self.action_undo(target)
            elif action == "detail":
                self.open_detail(target)
        # Own the callback on the App pump. A clicked Row can be unmounted by
        # refresh while the popup is open; its message pump must not own saves.
        self.call_later(self.push_screen, ContextMenu(options, x, y, "改状态" if statuses is not None else "操作"), picked)

    async def refresh_data(self):
        if self._refreshing:
            return
        self._refreshing = True
        try:
            data = self.store.snapshot()
            messages = self.changes.poll(data)
            if not self.no_notify:
                for title, message in messages:
                    notify_macos(title, message)
            self.snapshot_data = data
            c = data["counts"]
            top = Text(("归档区 · 右键恢复 / 空白处右键返回  " if self.archives else "") + datetime.fromtimestamp(data["now"]).strftime("%H:%M:%S") + f"  ·  GPT 运行中 {c['codex']}  ·  Gemini 运行中 {c['antigravity']}  ·  ", style="bold #d8e2f1")
            top.append(f"待拍板 {c['decisions']}", style="bold #ffd37a" if c["decisions"] else "#a4b0c2")
            self.query_one("#top", Static).update(top)
            self.query_one("#status", Static).update(Text("\n".join(data["errors"])))
            rows = [[], [], []]
            rows[0].append(Row("heading", "decisions", {}, Text("待你拍板", style="bold #ffd37a")))
            for d in data["decisions"]:
                if d.get("status") == "open":
                    rows[0].append(Row("decision", str(d.get("id", "")), d, decision_text(d, data["now"]), now=data["now"]))
            if len(rows[0]) == 1:
                rows[0].append(Row("heading", "empty-decisions", {}, "暂无待拍板事项"))
            rows[0].append(Row("heading", "progress", {}, Text("项目进度", style="bold #9dccff")))
            for i, p in enumerate(data["progress"]):
                rows[0].append(Row("progress", str(p.get("project", i)), p, progress_text(p, data["decisions"], data["now"]), now=data["now"], decisions=data["decisions"]))
            unseen = decisions_unseen(data["decisions"])
            if unseen:
                rows[0].append(Row("heading", "unseen", {}, Text("刚拍板（Claude 未读）", style="bold #74ddad")))
                for d in unseen:
                    rows[0].append(Row("decision", str(d.get("id", "")), d, decision_text(d, data["now"]), now=data["now"]))
            if self.history:
                rows[0].append(Row("heading", "history", {}, Text("已拍板历史", style="bold #9dccff")))
                for d in data["decisions"]:
                    if d.get("status") == "decided" and d not in unseen:
                        rows[0].append(Row("decision", str(d.get("id", "")), d, decision_text(d, data["now"]), now=data["now"]))
            if self.archives:
                rows[0] = [Row("heading", "archive-heading", {}, "已归档进度 · 右键恢复")]
                for p in data.get("archived", []):
                    rows[0].append(Row("progress", str(p.get("project", "")), p, progress_text(p, now=data["now"]), now=data["now"]))
            for pane, agent in ((1, "codex"), (2, "antigravity")):
                rows[pane].append(Row("heading", "live", {}, "已隐藏任务 · 右键恢复" if self.archives else "运行中 / 异常待检查"))
                done_header = False
                for j in visible_jobs(data, agent, self.history, hidden=self.archives):
                    if not is_active(j) and not done_header:
                        rows[pane].append(Row("heading", "done", {}, "全部结束历史" if self.history else "最近结束"))
                        done_header = True
                    j["snapshot_now"] = data["now"]
                    row = Row("job", j["id"], j, job_text(j, frame=self.pet_frame), now=data["now"])
                    row.frame = self.pet_frame
                    rows[pane].append(row)
                if len(rows[pane]) == 1:
                    rows[pane].append(Row("heading", "empty", {}, "暂无任务"))
            for index, new_rows in enumerate(rows):
                view = self.query_one(f"#list-{index}", ListView)
                old = view.highlighted_child
                key = (old.kind, old.key) if old else None
                old_index = view.index or 0
                # Update payload and text in place when order is unchanged: no scroll jump.
                if [(r.kind, r.key) for r in view.children] == [(r.kind, r.key) for r in new_rows]:
                    for old_row, new_row in zip(view.children, new_rows):
                        old_row.frame = new_row.frame
                        old_row.data = new_row.data
                        old_row.card_text = new_row.card_text
                        old_row.now, old_row.decisions = new_row.now, new_row.decisions
                        old_row.update_card()
                else:
                    scroll_y = view.scroll_y
                    await view.clear()
                    await view.extend(new_rows)
                    selected_index = next((i for i, r in enumerate(new_rows) if (r.kind, r.key) == key), None)
                    if selected_index is None:
                        eligible = [i for i, r in enumerate(new_rows) if r.kind != "heading"]
                        selected_index = min(eligible, key=lambda i: abs(i-old_index)) if eligible else None
                    view.index = selected_index
                    view.scroll_to(y=scroll_y, animate=False)
            self.update_layout()
        finally:
            self._refreshing = False

    async def action_refresh(self):
        if len(self.screen_stack) == 1:
            await self.refresh_data()

    async def action_history(self):
        if len(self.screen_stack) == 1:
            self.history = not self.history
            await self.refresh_data()

    @on(ListView.Selected)
    def detail(self, event):
        self.pane_index = int(event.list_view.id[-1])
        self.open_detail(event.item)

    def open_detail(self, row):
        self.read_selected(row)
        d = row.data
        if row.kind == "decision":
            if d.get("status") == "open":
                self.action_decide(row)
            else:
                self.action_undo(row)
            return
        elif row.kind == "job":
            title = d["title"]
            body = job_detail(d)
        elif row.kind == "progress":
            if not self.archives and project_decisions(d.get("project"), self.snapshot_data["decisions"]):
                self.action_decide()
                return
            title = str(d.get("project", "项目进度"))
            body = ("状态：" + progress_label(d, self.snapshot_data["decisions"]) +
                    "\n当前阶段：" + str(d.get("stage", "")) +
                    "\n下一步：" + str(d.get("next", "")) +
                    "\n更新时间：" + str(d.get("updated", "")))
        else:
            return
        self.push_screen(Detail(title, body, job=row.kind == "job"))

    def save_operation(self, operation, confirmation=None):
        async def save(confirmed=True):
            if not confirmed:
                return
            try:
                operation()
                await self.refresh_data()
            except (OSError, ValueError, RuntimeError, TypeError) as exc:
                self.query_one("#status", Static).update(Text("操作未保存：" + str(exc)))
        if confirmation:
            self.push_screen(Confirm(confirmation), save)
        else:
            self.run_worker(save())

    def action_edit_progress(self, row=None):
        if len(self.screen_stack) > 1:
            return
        row = row or self.selected()
        if not row or row.kind != "progress" or self.archives:
            self.query_one("#status", Static).update("请右键主视图中的项目进度，选择编辑")
            return
        project = row.data.get("project")
        def save(fields):
            if fields:
                self.save_operation(lambda: update_progress(self.store.board, project, fields))
        statuses = [p.get("status") for p in self.snapshot_data["progress"] if isinstance(p.get("status"), str)]
        self.push_screen(EditProgress(row.data.copy(), statuses), save)

    def action_close_decision(self, row=None):
        if len(self.screen_stack) > 1:
            return
        row = row or self.selected()
        if not row or row.kind != "decision" or row.data.get("status") != "open":
            self.query_one("#status", Static).update("请右键待拍板事项，选择处理 / 关闭")
            return
        key = row.key
        def save(answer):
            if answer:
                self.save_operation(lambda: decide(self.store.board, key, answer[0], answer[1]))
        self.push_screen(CloseDecision(row.data), save)

    def action_archive(self, row=None, restore=None):
        if len(self.screen_stack) > 1:
            return
        row = row or self.selected()
        restore = self.archives if restore is None else restore
        verb = "恢复" if restore else "归档"
        if row and row.kind == "progress":
            project = row.data.get("project")
            self.save_operation(lambda: archive_progress(self.store.board, project, restore), f"{verb}项目「{project}」？数据保留。")
        elif row and row.kind == "job" and row.data["state"] in TERMINAL:
            key = row.key
            self.save_operation(lambda: self.store.hide_jobs([key], restore), f"{verb}任务「{row.data['title']}」？只修改显示状态，保留任务日志与结果。")
        else:
            self.query_one("#status", Static).update("请选中项目进度或已结束任务；运行中/卡住任务不能归档")

    def action_clear_finished(self):
        if len(self.screen_stack) > 1:
            return
        ids = [j["id"] for j in self.snapshot_data["jobs"] if j["agent"] in ("codex", "antigravity") and j["state"] in TERMINAL and not j.get("hidden")]
        if not ids:
            self.query_one("#status", Static).update("没有可归档的已结束任务")
            return
        self.save_operation(lambda: self.store.hide_jobs(ids), f"隐藏 GPT 和 Gemini 全部 {len(ids)} 个已结束任务（包括历史）？可在归档区恢复。")

    async def action_archives(self):
        if len(self.screen_stack) == 1:
            self.archives = not self.archives
            await self.refresh_data()

    def action_decide(self, row=None):
        if len(self.screen_stack) > 1:
            return
        row = row or self.selected()
        if row and row.kind == "progress":
            view = self.query_one("#list-0", ListView)
            decisions = [(i, candidate) for i, candidate in enumerate(view.children)
                         if candidate.kind == "decision"
                         and candidate.data.get("project") == row.data.get("project")
                         and candidate.data.get("status") == "open"]
            if not decisions:
                self.query_one("#status", Static).update("这是进度条目；该项目没有待拍板事项")
                return
            if len(decisions) > 1:
                view.index = decisions[0][0]
                self.query_one("#status", Static).update(
                    f"该项目有 {len(decisions)} 条待拍板，已跳到第一条，按 Enter 拍板")
                return
            row = decisions[0][1]
        if not row or row.kind != "decision" or row.data.get("status") != "open":
            self.query_one("#status", Static).update("请先选中一条待拍板事项")
            return
        key = row.key
        async def save(answer):
            if answer is None:
                return
            try:
                decide(self.store.board, key, answer[0], answer[1], option_index=answer[2])
                await self.refresh_data()
            except (OSError, ValueError, RuntimeError, TypeError) as exc:
                self.query_one("#status", Static).update(Text("拍板未保存：" + str(exc)))
        self.push_screen(DecisionNote(row.data), save)

    def action_undo(self, row=None):
        if len(self.screen_stack) > 1:
            return
        row = row or self.selected()
        if not row or row.kind != "decision" or row.data.get("status") != "decided":
            self.query_one("#status", Static).update("请在刚拍板或 h 历史中选中一条已拍板事项")
            return
        key = row.key
        async def undo(confirmed):
            if confirmed:
                try:
                    undo_decision(self.store.board, key)
                    await self.refresh_data()
                except (OSError, ValueError, RuntimeError, TypeError) as exc:
                    self.query_one("#status", Static).update(Text("撤销未保存：" + str(exc)))
        self.push_screen(UndoDecision(row.data), undo)

    @on(Button.Pressed, "#refresh-board")
    async def click_refresh(self):
        await self.action_refresh()

    @on(Button.Pressed, "#toggle-history")
    async def click_history(self):
        await self.action_history()
        self.query_one(f"#list-{self.pane_index}", ListView).focus()

    @on(Button.Pressed, "#tab-0, #tab-1, #tab-2")
    def click_tab(self, event):
        self.action_pane(int(event.button.id[-1]) - self.pane_index)

    def on_key(self, event):
        if event.key != "escape":
            self._last_escape = None

    def action_escape(self):
        if len(self.screen_stack) > 1:
            return
        now = time.monotonic()
        if self._last_escape is not None and now - self._last_escape < 1.5:
            self.exit()
        else:
            self._last_escape = now
            self.query_one("#status", Static).update("再按一次 Esc 退出（1.5 秒内）")
