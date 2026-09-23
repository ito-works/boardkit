"""boardkit.styles — 256/8-colour style tables (curses attrs resolved lazily)"""

from __future__ import annotations

import os

# Palette: amber (bold yellow) dominates — titles, column headers, the
# current step; ok green = done/healthy; err red = bounced/blocked/
# unreachable; box = steel-blue/dim borders; hdr = white bold; dim = stale.
# "sel-<base>" composes the selection highlight with the row's own style;
# the painter maps it to A_REVERSE | attr(base). Without colors every
# style degrades to bold / dim / plain.
BASE_STYLES = ("hdr", "ok", "warn", "err", "dim", "title", "box", "amber", "")
STYLES = BASE_STYLES + ("sel", "mark", "sel-mark") + tuple(
    f"sel-{s}" for s in BASE_STYLES if s) + tuple(
    f"mark-{s}" for s in BASE_STYLES if s) + tuple(
    f"sel-mark-{s}" for s in BASE_STYLES if s)


def no_color() -> bool:
    return bool(os.environ.get("NO_COLOR"))        # empty value = unset


def style_attr(style: str, attrs: dict) -> int:
    """'sel-<x>' -> A_REVERSE | attr(x); 'mark-<x>' -> mark | attr(x);
    prefixes nest, so 'sel-mark-<base>' is the marked cursor row."""
    if style.startswith("sel-"):
        return attrs.get("sel", 0) | style_attr(style[4:], attrs)
    if style.startswith("mark-"):
        return attrs.get("mark", 0) | style_attr(style[5:], attrs)
    return attrs.get(style, 0)


def init_styles(curses_mod) -> dict:
    """Palette: amber (bold yellow) for titles / headers / the current
    step, vivid green for ok/done and vivid red for errors (256-color
    codes when the terminal has them, bold ANSI otherwise), steel-blue
    borders. Without colors every style degrades to bold / dim / plain."""
    bold, dim = curses_mod.A_BOLD, curses_mod.A_DIM
    attrs = {"hdr": bold, "dim": dim, "sel": curses_mod.A_REVERSE,
             "mark": getattr(curses_mod, "A_UNDERLINE", bold),
             "title": bold, "amber": bold, "box": dim,
             "ok": 0, "warn": 0, "err": 0, "": 0}
    if no_color() or not curses_mod.has_colors():
        return attrs
    try:
        curses_mod.start_color()
        try:
            curses_mod.use_default_colors()
            bg = -1
        except curses_mod.error:
            bg = curses_mod.COLOR_BLACK
        c256 = getattr(curses_mod, "COLORS", 8) >= 256
        ok_c = 40 if c256 else curses_mod.COLOR_GREEN    # #00d700
        # Dark saturated reds (160) trip terminal minimum-contrast and
        # render white; softening toward #d75f5f keeps luminance up.
        err_c = 167 if c256 else curses_mod.COLOR_RED     # #d75f5f
        pairs = (("ok", ok_c, bold),
                 ("warn", curses_mod.COLOR_YELLOW, 0),
                 ("err", err_c, bold),
                 ("title", curses_mod.COLOR_YELLOW, bold),
                 ("amber", curses_mod.COLOR_YELLOW, bold),
                 ("box", curses_mod.COLOR_BLUE, 0))
        for i, (name, color, extra) in enumerate(pairs, start=1):
            curses_mod.init_pair(i, color, bg)
            attrs[name] = curses_mod.color_pair(i) | extra
    except curses_mod.error:
        pass
    return attrs
