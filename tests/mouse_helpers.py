"""Coordinate-based mouse helpers for this installed Textual Pilot version."""
from textual.events import Click, MouseDown, MouseUp


async def right_click(pilot, widget=None, offset=(2, 0)):
    if widget is not None and not isinstance(widget, str):
        widget.scroll_visible(animate=False)
        await pilot.pause()
    # Pilot.click only exposes button 1 in this version. Use the same event
    # dispatcher, including hit testing, for a complete button-3 click.
    return await pilot._post_mouse_events([MouseDown, MouseUp, Click],
                                         widget, offset=offset, button=3)


async def menu_action(pilot, row, action):
    await right_click(pilot, row)
    await pilot.click("#menu-" + action)


async def global_action(app, pilot, action):
    # Panel border is visible background even when its list is fully populated.
    pane = app.query_one(f"#pane-{app.pane_index}")
    await right_click(pilot, pane, offset=(0, 1))
    await pilot.click("#menu-" + action)
