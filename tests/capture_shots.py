"""Generate fixture SVGs; never touch user board or jobs.

PYTHONPATH=. ./.venv/bin/python tests/capture_shots.py
"""
import asyncio
from pathlib import Path
import shutil
import re
import os
from html import unescape
from rich.cells import cell_len
import tempfile
import time
import xml.etree.ElementTree as ET

from aw.core import Store, decide
from aw.tui import WatchApp
from fixtures import fixture_store, jsonl, make_board, make_job
import json


def offline_svg(path):
    """Keep Textual's screenshot, fix Rich's codepoint-based SVG textLength.

    Terminal cells already align correctly. Rich 15's SVG exporter uses len()
    for textLength, squeezing CJK glyphs to half their intended width.
    """
    path = Path(path)
    svg = path.read_text(encoding="utf-8")
    svg = re.sub(r"@font-face\s*\{[^}]*\}", "", svg, flags=re.S)
    svg = svg.replace("font-family: Fira Code, monospace;", 'font-family: Menlo, "PingFang SC", "Apple Braille", "Apple Symbols", monospace;')
    def correct(match):
        before, length, after, content = match.groups()
        text = unescape(content)
        width = float(length) * cell_len(text) / len(text) if text else float(length)
        return f'{before}textLength="{width:g}" lengthAdjust="spacingAndGlyphs"{after}>{content}</text>'
    svg = re.sub(r'(<text\b[^>]*?)textLength="([0-9.]+)"([^>]*)>(.*?)</text>', correct, svg, flags=re.S)
    path.write_text(svg, encoding="utf-8")


async def main():
    # Headless environments often set NO_COLOR; screenshots should show warnings.
    os.environ.pop("NO_COLOR", None)
    shots = Path(__file__).resolve().parents[1] / "_shots"
    shots.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="aw-shots-") as tmp:
        root = Path(tmp)
        fixture = fixture_store(root)
        # Six v1.5 cases together, including the old success in full history.
        captured_now = time.time()
        names = {0: "实时处理", 1: "长时无输出", 2: "心跳失联", 3: "刚完成验证", 4: "检查未通过", 5: "先前完成"}
        for directory in fixture.jobs.iterdir():
            index = int(directory.name.split("-")[1])
            if index > 5:
                shutil.rmtree(directory)  # Only generated temporary fixtures.
                continue
            (directory / "prompt.txt").write_text("【标题】" + names[index] + "\n【一句话】验证边界并汇报结果\n")
            if index in (3, 4, 5):
                result = "测试未通过：参考值超出范围。" if index == 4 else "边界验证通过，结果已写入报告。"
                if directory.name.startswith("antigravity"):
                    (directory / "output.txt").write_text(jsonl([dict(event="result", result=dict(response=result))]))
                else:
                    (directory / "last_message.txt").write_text(result)
            if index == 5:
                ended = captured_now - 40 * 60
                os.utime(directory / "exit_code", (ended, ended))
                meta = json.loads((directory / "meta.json").read_text())
                meta["started"] = ended - 180
                (directory / "meta.json").write_text(json.dumps(meta))
        decide(fixture.board, "normal", "自动", "先使用默认配置，再复核边界")
        for name, store in (("fixture", fixture),):
            for label, size in (("wide", (160, 45)), ("narrow", (100, 32))):
                app = WatchApp(store, no_notify=True, no_anim=True)
                app.history = name == "fixture"
                async with app.run_test(size=size) as pilot:
                    if label == "narrow":
                        await pilot.press("tab")
                    await pilot.pause()
                    path = app.save_screenshot(filename=f"{name}_{label}.svg", path=str(shots))
                    offline_svg(path)
                    print(path)
        states_root = root / "pets-states"
        states_root.mkdir()
        board = states_root / "board.json"
        data = make_board(board)
        data["progress"][0]["status"] = "blocked"
        data["progress"][0]["stage"] = "等待处理失败项"
        board.write_text(json.dumps(data, ensure_ascii=False))
        make_job(states_root / "jobs", "busy-codex", state="running")
        states = Store(states_root / "jobs", board, states_root / "state.json")
        for name, store in (("pets_states", states), ("decide_dialog", fixture), ("header_pixel", states)):
            app = WatchApp(store, no_notify=True, no_anim=True)
            async with app.run_test(size=(160, 45)) as pilot:
                if name == "decide_dialog":
                    await pilot.press("enter", "2")
                    app.screen.query_one("#note-input").value = "先确认边界条件，明天再开始测试"
                await pilot.pause()
                path = app.save_screenshot(filename=f"{name}.svg", path=str(shots))
                offline_svg(path)
                if name == "header_pixel":
                    # Extract only the six actual terminal header rows, retaining
                    # native glyphs, styles and clips from save_screenshot().
                    header = app.query_one("#header-0").region
                    left = header.x
                    right = app.query_one("#header-2").region.right
                    w, h = (right - left) * 12.2, header.height * 24.4
                    ns = "{http://www.w3.org/2000/svg}"
                    ET.register_namespace("", ns[1:-1])
                    root_svg = ET.fromstring(Path(path).read_text(encoding="utf-8"))
                    root_svg.attrib.update(width=f"{w:g}", height=f"{h:g}", viewBox=f"0 0 {w:g} {h:g}")
                    rows = range(header.y, header.bottom)
                    for child in list(root_svg):
                        if child.tag in (ns + "style", ns + "defs"):
                            continue
                        if child.tag != ns + "g" or "clip-path" not in child.attrib:
                            root_svg.remove(child)
                            continue
                        child.set("transform", f"translate({-left * 12.2:g}, {-header.y * 24.4:g})")
                        for element in list(child):
                            if element.tag == ns + "rect":
                                y = float(element.get("y", "0"))
                                if not header.y * 24.4 <= y < header.bottom * 24.4:
                                    child.remove(element)
                            elif element.tag == ns + "g":
                                for glyph in list(element):
                                    match = re.search(r"-line-(\d+)", glyph.get("clip-path", ""))
                                    if not match or int(match.group(1)) not in rows:
                                        element.remove(glyph)
                    root_svg.find(ns + "defs").clear()
                    # Reattach line clips used by the retained glyphs.
                    original = ET.fromstring(Path(path).read_text(encoding="utf-8"))
                    for clip in original.find(ns + "defs"):
                        match = re.search(r"-line-(\d+)$", clip.get("id", ""))
                        if "clip-terminal" in clip.get("id", "") or (match and int(match.group(1)) in rows):
                            root_svg.find(ns + "defs").append(clip)
                    Path(path).write_text(ET.tostring(root_svg, encoding="unicode"), encoding="utf-8")
                print(path)


if __name__ == "__main__":
    asyncio.run(main())
