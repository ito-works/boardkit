"""boardkit.text — cell-width aware clipping/wrapping, glyphs, control-char cleaning"""

from __future__ import annotations

import math
import re
import unicodedata

CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def clean(text) -> str:
    """Collapse every control char (\n, \t, ESC, ...) to one space."""
    return CONTROL_RE.sub(" ", str(text))


def _cw(ch: str) -> int:
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def cell_width(text: str) -> int:
    """Terminal cells occupied by `text` (W/F east-asian chars count 2)."""
    return sum(_cw(ch) for ch in text)


def clip(text: str, width: int) -> str:
    """Clip to at most `width` terminal cells (never splits a wide char)."""
    width = max(0, int(width))
    if len(text) <= width and cell_width(text) <= width:
        return text
    out = []
    used = 0
    for ch in text:
        w = _cw(ch)
        if used + w > width:
            break
        out.append(ch)
        used += w
    return "".join(out)


def to_ascii(text: str) -> str:
    """Replace every non-ASCII char with '?' so the line is pure ASCII."""
    return "".join(ch if ch.isascii() else "?" for ch in text)


GLYPHS = {
    "current": ("▶", ">"),
    "warn": ("!", "!"),
    "done": ("✓", "OK"),
    "fail": ("✗", "X"),
    "lamp_on": ("●", "*"),
    "lamp_off": ("●", "*"),       # color carries the status; shape is the same
    "node_done": ("●", "*"),
    "node_cur": ("◉", "@"),
    "node_future": ("○", "o"),
    "dash": ("─", "-"),
    "marker": ("▶", ">"),
    "badge": ("■", "#"),
    "up": ("↑", "^"),
    "ellipsis": ("…", "..."),
    "sep": (" · ", " | "),
    "bar": (" │ ", " | "),
    "expand": ("  ┆ ", "  : "),
    "more": ("…", "..."),
    "arrow": ("→", "->"),
    "clip": ("…", "~"),            # single-cell ellipsis on a clipped segment
    "retry": ("↻", "~"),           # RETRYING producer
    "claim": ("⚑", "@"),           # owner badge on a claimed queue row
    "mark": ("▌", "*"),            # marked-row margin bar (multi-select)
}

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
SPINNER_ASCII = "|/-\\"
FLIP_FRAMES = 4            # a changed row stays highlighted this many ticks

BOX = {"tl": "┌", "tr": "┐", "bl": "└", "br": "┘", "h": "─", "v": "│"}
BOX_DOUBLE = {"tl": "╔", "tr": "╗", "bl": "╚", "br": "╝", "h": "═", "v": "║"}
BOX_ASCII = {"tl": "+", "tr": "+", "bl": "+", "br": "+", "h": "-", "v": "|"}
BOX_ASCII_DOUBLE = {"tl": "+", "tr": "+", "bl": "+", "br": "+", "h": "=", "v": "|"}


def spaced(text: str) -> str:
    """Letter-spaced panel titles: 'IN FLIGHT' -> 'I N   F L I G H T'."""
    return " ".join(" " if ch == " " else ch for ch in text.upper())


def glyph(name: str, ascii_only: bool) -> str:
    pair = GLYPHS.get(name, ("?", "?"))
    return pair[1] if ascii_only else pair[0]


def spinner(frame: int, ascii_only: bool) -> str:
    """One spinner glyph for tick `frame` (braille; ASCII `|/-\\`)."""
    chars = SPINNER_ASCII if ascii_only else SPINNER
    return chars[int(frame) % len(chars)]


def countdown(now: float, asof: float | None, interval: float) -> str:
    """'Ns' until the next poll of a source last polled at `asof`; 'now'
    when due, overdue or never polled. Never negative."""
    if asof is None:
        return "now"
    remaining = float(interval) - (float(now) - float(asof))
    if remaining <= 0:
        return "now"
    return f"{int(math.ceil(remaining))}s"


def flipped(frame: int, changed_frame: dict, key) -> bool:
    """True while a row changed at changed_frame[key] is still flashing."""
    at = changed_frame.get(key)
    return at is not None and 0 <= int(frame) - int(at) < FLIP_FRAMES


def fmt_age(seconds: float | None) -> str:
    """'45s', '4m12s', '3h', '2d', or '?' when unknown."""
    if seconds is None:
        return "?"
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60}s"
    if s < 86400:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def fmt_span(seconds: float | None) -> str:
    """Duration for ETA copy: '9m', '1m30s', '30m', '2h', '2h15m'; '?' unknown."""
    if seconds is None:
        return "?"
    s = max(0, int(round(seconds)))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        m, r = divmod(s, 60)
        return f"{m}m" if r == 0 else f"{m}m{r}s"
    if s < 86400:
        h, r = divmod(s, 3600)
        return f"{h}h" if r // 60 == 0 else f"{h}h{r // 60}m"
    return f"{s // 86400}d"


def fmt_size(n: int) -> str:
    """'512B', '38KB', '1.2MB'."""
    if n < 1024:
        return f"{n}B"
    if n < 1024 * 1024:
        return f"{n // 1024}KB"
    return f"{n / (1024 * 1024):.1f}MB"


def fmt_rate(delta_bytes: float, seconds: float) -> str:
    """'0.4KB/s' for a gate-log growth sample."""
    if seconds <= 0:
        return "?KB/s"
    return f"{delta_bytes / 1024 / seconds:.1f}KB/s"


def wrap_cells(text: str, width: int) -> list[str]:
    """Greedy word wrap counting terminal CELLS — textwrap counts Python
    characters and lets wide (CJK) glyphs overflow into the clip path,
    which would silently drop the tail of the line. An over-wide word is
    hard-split at the cell boundary. Always returns at least one line."""
    lines: list[str] = []
    cur = ""
    for word in text.split(" "):
        while cell_width(word) > width:
            if cur:
                lines.append(cur)
                cur = ""
            head = ""
            for ch in word:
                if head and cell_width(head + ch) > width:
                    break
                head += ch
            lines.append(head)
            word = word[len(head):]
        cand = f"{cur} {word}" if cur else word
        if cell_width(cand) <= width:
            cur = cand
        else:
            if cur:
                lines.append(cur)
            cur = word
    if cur or not lines:
        lines.append(cur)
    return lines
