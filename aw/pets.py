"""Render Claude's unchanged 14×12 pixel designs into 14×6 terminal cells."""
import json
from pathlib import Path

from rich.style import Style
from rich.text import Text

DATA = json.loads(Path(__file__).with_name("pets_pixel.json").read_text(encoding="utf-8"))
COLORS = {agent: DATA["palette"][key] for agent, key in
          (("claude", "O"), ("codex", "G"), ("antigravity", "B"))}
FRAME_SECONDS = DATA["frame_seconds"]
HEADER_BG = "#101721"


def pixel_rows(agent, state, tick=0, background=HEADER_BG):
    frames = DATA["frames"][DATA["names"][agent]][state]
    frame = frames[tick % len(frames)]
    palette = DATA["palette"]
    rows = []
    for y in range(0, 12, 2):
        row = Text(no_wrap=True)
        for top, bottom in zip(frame[y], frame[y + 1]):
            if top == bottom == ".":
                char, fg, bg = " ", background, background
            elif top == ".":
                char, fg, bg = "▄", palette[bottom], background
            else:
                char, fg, bg = "▀", palette[top], palette.get(bottom, background)
            row.append(char, Style(color=fg, bgcolor=bg))
        rows.append(row)
    return rows


def header_text(agent, name, state, label, summary, tick=0):
    rows = pixel_rows(agent, state, tick)
    badges = DATA["badges"][state]
    badge = badges[tick % len(badges)]
    text = Text(no_wrap=True, overflow="ellipsis")
    for i, row in enumerate(rows):
        text.append_text(row)
        text.append("  ")
        if i == 1:
            text.append(name, style="bold " + COLORS[agent])
        elif i == 2:
            text.append(label + " · " + summary, style="#aebed2")
        elif i == 3:
            text.append(badge, style=DATA["badge_colors"].get(state, COLORS[agent]))
        if i < 5:
            text.append("\n")
    return text
