"""boardkit.expand — inline row expansion with background fetch, token ownership,
dedupe of in-flight fetches, and a caller-owned cache."""
from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from typing import Callable

from boardkit.cursor import open_overlay
from boardkit.text import glyph


@dataclass
class Expander:
    cache_key: tuple                      # dedupe + cache identity
    fetch: Callable[[], list]             # runs on a background thread; returns lines
    on_error: Callable[[str], list]       # lines to show when fetch raises


def open_expansion(key: str, lines_now, expander: Expander | None, exp: dict,
                    detail_q: queue.Queue, cache: dict) -> threading.Thread | None:
    """Expand row `key`: sets exp["key"]; content is rendered synchronously
    when `lines_now` is given or the expander's cache_key is already cached,
    else fetched by `expander.fetch()` in a short-lived daemon thread. A
    fetch already in flight for the same cache_key is re-owned instead of
    spawning a second thread. Returns the thread or None."""
    exp["key"] = key
    if lines_now is not None or expander is None:
        open_overlay(exp, lines_now if lines_now is not None else [])
        return None
    ckey = expander.cache_key
    if ckey in cache:
        open_overlay(exp, cache[ckey])
        return None
    pending = exp.setdefault("pending", {})
    if ckey in pending:
        # A fetch for this very key is in flight: re-own its token instead
        # of spawning another thread (Enter-Enter-Enter on a slow fetch).
        exp["token"] = pending[ckey]
        exp["lines"] = None
        exp["inflight"] = True
        return None
    token = open_overlay(exp, None)
    exp["inflight"] = True
    pending[ckey] = token

    def run():
        try:
            lines = expander.fetch()
            cache[ckey] = lines
        except (SystemExit, KeyboardInterrupt):
            raise
        except BaseException as exc:
            lines = expander.on_error(str(exc))
        pending.pop(ckey, None)
        detail_q.put((token, lines))

    t = threading.Thread(target=run, name="expand-fetch", daemon=True)
    t.start()
    return t


# --- scrolling an open expansion (jibot-code#6swq) ---------------------------
# Boards window their rows on the cursor row and let its expansion follow,
# so an expansion taller than the screen is cut at the bottom and the cursor
# cannot reach the rest (navigation moves between rows). The engine keeps a
# scroll offset in the expansion dict instead, moved by J / K. By default it
# hands the board only the lines from that offset on, so a board scrolls
# without any code of its own, one given line per step. A board that wraps
# each line into several screen rows itself (repoman-board) sets
# `scrolls_expansion_rows = True`: it is handed every line as ScrolledLines
# and cuts its own wrapped rows with scroll_rows(), so a step is one screen
# row there too and a single long line still scrolls.
#
# Either way hidden lines give way to ONE marker line counting them, so the
# first step hides two lines (the marker takes the second's place) and the
# view is one line shorter per step: the bottom of the screen always gains
# exactly one new line per J.

def scroll_marker(hidden: int, ascii_only: bool = False) -> str:
    word = "line" if hidden == 1 else "lines"
    return f"{glyph('up', ascii_only)} {hidden} {word} above (K scrolls up, J down)"


class ScrolledLines(list):
    """The open expansion's lines, whole, for a board that wraps them itself.
    `scroll` is the engine's offset in that board's screen rows; the board's
    scroll_rows() call records in `max_scroll` the deepest offset its rows
    allow, which the engine clamps J against."""

    def __init__(self, lines, scroll: int = 0):
        super().__init__(lines)
        self.scroll = scroll
        self.max_scroll = None


def _max_scroll(n: int) -> int:
    # the deepest scroll still leaves the last line on screen under the marker
    return max(0, n - 2)


def _limit(exp: dict) -> int:
    view = exp.get("view")
    if isinstance(view, ScrolledLines) and view.max_scroll is not None:
        return view.max_scroll
    return _max_scroll(len(exp.get("lines") or ()))


def scroll_expansion(exp: dict, delta: int) -> bool:
    """Move the open expansion's offset by `delta` steps, clamped. False
    (nothing moved) when nothing is open, it is still loading, or it is
    already at that end."""
    if exp.get("key") is None or not exp.get("lines"):
        return False
    top = _limit(exp)
    old = min(exp.get("scroll", 0), top)
    new = min(max(0, old + delta), top)
    exp["scroll"] = new
    return new != old


def expansion_view(exp: dict, ascii_only: bool = False, rows: bool = False):
    """What the board is handed for the open expansion; None while loading.
    `rows` (the board's scrolls_expansion_rows): every line, as
    ScrolledLines. Otherwise all the lines at offset 0, else a marker line
    counting the hidden lines above, then the rest."""
    lines = exp.get("lines")
    exp["view"] = None
    if lines is None:
        return None
    if rows:
        view = ScrolledLines(lines, exp.get("scroll", 0))
        exp["view"] = view
        return view
    s = min(exp.get("scroll", 0), _max_scroll(len(lines)))
    if s <= 0:
        return list(lines)
    hidden = s + 1                                  # the marker covers one more
    return ["  " + scroll_marker(hidden, ascii_only)] + list(lines[hidden:])


def scroll_rows(rows: list, lines, marker_row) -> list:
    """A scrolls_expansion_rows board's wrapped expansion `rows` cut at the
    engine's offset, which rides on `lines` (ScrolledLines; anything else
    leaves `rows` alone). `marker_row(hidden)` builds the marker row. Records
    the deepest offset `rows` allow on `lines` for the engine's clamp."""
    if not isinstance(lines, ScrolledLines):
        return rows
    top = _max_scroll(len(rows))
    lines.max_scroll = top
    s = min(lines.scroll, top)
    if s <= 0:
        return rows
    hidden = s + 1
    return [marker_row(hidden)] + list(rows[hidden:])
