"""boardkit.layout — tiers, boxes, viewport scrolling"""
from __future__ import annotations

from boardkit.text import clean, cell_width, clip, to_ascii, glyph
from boardkit.text import BOX, BOX_DOUBLE, BOX_ASCII, BOX_ASCII_DOUBLE

Line = tuple[str, str]
Row = list          # list[Line]: one screen row as styled segments


def tier_for(width: int, height: int) -> str:
    """'widget' (<80 cols or <16 rows), 'compact' (<140 or <30), else
    'dashboard'."""
    if width < 80 or height < 16:
        return "widget"
    if width < 140 or height < 30:
        return "compact"
    return "dashboard"


def row_width(row: Row) -> int:
    return sum(cell_width(t) for t, _ in row)


# --- hit tags: which item a screen cell belongs to (8dpg) ---------------------
# A row that IS an items() entry carries a zero-width tag segment
# ("", "hit:<item key>") in front of its text. It paints nothing (the
# painter skips empty text), measures nothing, and select()/mark_row() wrap
# its style like any other, so `hit_key` strips those prefixes. hit_map()
# reads the final composed rows back into (y, x0, x1, key) spans: a click
# resolves to a key through the same rows the frame painted, so it can
# never pick a row the board is not showing.

HIT_PREFIX = "hit:"


def hit_tag(key: str) -> Line:
    return ("", HIT_PREFIX + str(key))


def hit_gap() -> Line:
    """A zero-width panel boundary: the span before it ends here and the
    cells after it belong to nothing until the next tag. A board that
    joins two independently laid-out columns on one screen row puts one
    at the divider, so a left item never claims right-column cells (and
    vice versa) — fresheyes round 1."""
    return ("", HIT_PREFIX)


def hit_key(style: str) -> str | None:
    """The item key a (possibly sel-/mark-wrapped) tag style names, else None."""
    while style.startswith("sel-") or style.startswith("mark-"):
        style = style[4:] if style.startswith("sel-") else style[5:]
    return style[len(HIT_PREFIX):] if style.startswith(HIT_PREFIX) else None


def hit_map(rows: list) -> list:
    """(y, x0, x1, key) for every tagged row span in composed `rows`.
    hit_gap() markers cut a row into regions (one per panel column);
    within a region a span runs from its tag's cell to the next tag (or
    the region's end), and the region's first span starts at the region's
    first cell: a row's box border, leading gap and trailing padding count
    for that row (deliberate — a click just left or right of the text is
    still a click on the line). A region with no tag (a header row, an
    empty row, a panel with nothing selectable on that screen row) yields
    nothing."""
    out: list = []
    for y, row in enumerate(rows):
        x = 0
        region_x0 = 0
        tags: list = []           # (x, key) inside the current region

        def flush(end: int) -> None:
            for i, (x0, k) in enumerate(tags):
                x1 = tags[i + 1][0] if i + 1 < len(tags) else end
                out.append((y, region_x0 if i == 0 else x0, x1, k))
            tags.clear()

        for t, s in row:
            k = hit_key(s)
            if k == "":                          # boundary
                flush(x)
                region_x0 = x
            elif k is not None:
                tags.append((x, k))
            x += cell_width(t)
        flush(x)
    return out


def hit_at(hits: list, y: int, x: int) -> str | None:
    """The item key under screen cell (y, x), or None."""
    for hy, x0, x1, k in hits:
        if hy == y and x0 <= x < x1:
            return k
    return None


def fit_row(row: Row, width: int, ascii_only: bool) -> Row:
    """The single clipping path: clean + ascii + clip every segment against
    the remaining cell budget. Zero-width hit tags pass through untouched."""
    out: Row = []
    used = 0
    ell = glyph("clip", ascii_only)
    for t, s in row:
        if not t and hit_key(s) is not None:
            out.append((t, s))
            continue
        t = clean(t)
        if ascii_only:
            t = to_ascii(t)
        fitted = clip(t, width - used)
        if fitted != t and fitted:
            # a clipped segment ends with a single-cell ellipsis, never a
            # mid-word cut; a clipped trailing row of spaces needs none
            if fitted.strip():
                fitted = clip(fitted, cell_width(fitted) - 1) + ell
        t = fitted
        if not t:
            continue
        used += cell_width(t)
        out.append((t, s))
    return out


def pad_row(row: Row, width: int, style: str = "") -> Row:
    gap = width - row_width(row)
    if gap > 0:
        return row + [(" " * gap, style)]
    return row


def flatten(row: Row) -> Line:
    row = [(t, s) for t, s in row if hit_key(s) is None]   # tags carry no style
    text = "".join(t for t, _ in row)
    for _, s in row:
        if s.startswith("sel-"):
            return (text, s)
    if any(s == "sel" for _, s in row):
        return (text, "sel")
    for _, s in row:
        if s and s != "box":
            return (text, s)
    return (text, "")


def sel(style: str) -> str:
    return f"sel-{style}" if style else "sel"


def restyle(row: Row, style: str) -> Row:
    return [(t, style) for t, _ in row]


def select(row: Row) -> Row:
    return [(t, sel(s)) for t, s in row]


def mark(style: str) -> str:
    return f"mark-{style}" if style else "mark"


def mark_row(row: Row) -> Row:
    return [(t, mark(s)) for t, s in row]


def is_marked_row(row: Row) -> bool:
    return any(s == "mark" or s.startswith("mark-") or s.startswith("sel-mark")
               for _, s in row)


def is_sel_row(row: Row) -> bool:
    return any(s == "sel" or s.startswith("sel-") for _, s in row)


def box(title: Row, body: list[Row], width: int, ascii_only: bool,
        stale_rows: bool = False, double: bool = False) -> list[Row]:
    """Boxed panel: top border carrying the title, content rows, bottom
    border. Double border for the in-flight card. Falls back to plain rows when
    the panel is too narrow."""
    if ascii_only:
        b = BOX_ASCII_DOUBLE if double else BOX_ASCII
    else:
        b = BOX_DOUBLE if double else BOX
    if width < 8:
        return [title] + body
    title = fit_row(title, width - 5, ascii_only)
    fill = width - 5 - row_width(title)
    top = [(b["tl"] + b["h"] + " ", "box")] + title + [
        (" " + b["h"] * fill + b["tr"], "box")]
    inner = width - 4
    rows = [top]
    for r in body:
        r = fit_row(r, inner, ascii_only)
        pad = inner - row_width(r)
        first = next((s for t, s in r if hit_key(s) is None), "")
        pad_style = first if first.startswith("sel") else (
            "dim" if stale_rows else "")
        # a marked row's border gap carries the mark glyph (same 2 cells)
        lead = ([(b["v"], "box"), (glyph("mark", ascii_only), "amber")]
                if is_marked_row(r) else [(b["v"] + " ", "box")])
        rows.append(lead + r + [(" " * pad, pad_style), (" " + b["v"], "box")])
    rows.append([(b["bl"] + b["h"] * (width - 2) + b["br"], "box")])
    return rows


def scroll_to_selected(rows: list[Row], avail: int, exp_len: int = 0) -> list[Row]:
    """Drop rows from the top so the selected row (and its expansion, when
    it fits) is inside `avail` rows. No selection -> unchanged."""
    if avail <= 0 or len(rows) <= avail:
        return rows
    idx = next((i for i, r in enumerate(rows) if is_sel_row(r)), None)
    if idx is None:
        return rows
    end = idx + 1 + max(0, exp_len)
    offset = max(0, min(idx, end - avail))
    return rows[offset:]


def queue_window(n: int, selected: int, rows: int) -> tuple[int, int, bool, bool]:
    """(offset, count, more_above, more_below) for a viewport of `rows`
    lines over `n` queue rows keeping `selected` visible. Marker lines
    ('... N more above/below') are taken out of `rows` when there is room."""
    rows = max(0, rows)
    if n <= rows:
        return 0, n, False, False
    data = rows - 2 if rows >= 3 else rows
    if data <= 0:
        return 0, 0, False, False
    selected = min(max(0, selected), n - 1)
    offset = min(max(0, selected - data + 1), n - data)
    return offset, data, offset > 0, offset + data < n


def window_heights(heights: list[int], selected: int, rows: int) -> tuple[int, int, bool, bool]:
    """Like queue_window but each row has its own height (one line plus
    its expansion, when open). Keeps `selected` inside the viewport (at
    least its first line)."""
    n = len(heights)
    rows = max(0, rows)
    if sum(heights) <= rows:
        return 0, n, False, False
    data = rows - 2 if rows >= 3 else rows
    if n == 0:
        return 0, 0, False, False
    if data <= 0:
        return 0, 0, False, True               # no room: still say what is hidden
    selected = min(max(0, selected), n - 1)
    # smallest offset such that rows offset..selected fit in `data`
    offset = selected
    used = heights[selected]
    while offset > 0 and used + heights[offset - 1] <= data:
        offset -= 1
        used += heights[offset]
    count = selected - offset + 1
    while offset + count < n and used + heights[offset + count] <= data:
        used += heights[offset + count]
        count += 1
    return offset, count, offset > 0, offset + count < n


def cell(text: str, w: int, right: bool) -> str:
    text = clip(text, w - 1)
    pad = w - 1 - cell_width(text)
    return (" " * pad + text + " ") if right else (text + " " * pad + " ")


# --- fixed-max panels that shrink instead of dropping (jibot-code#gqfj) --------
# A column is a list of panels in PRIORITY order (first = dropped last).
# fit_heights sizes them; window_panel cuts one panel's box to its height.

def fit_heights(needs: list, floors: list, avail: int,
                keep: int | None = None, collapsed: list | None = None) -> list:
    """Heights for a column of panels in PRIORITY order (first = cut
    last). `needs` is each panel's natural (already capped) height,
    `floors` its smallest useful height (a need of 0 is an absent panel),
    `collapsed` its collapsed forms — an int, or a descending sequence of
    heights it can fall back to in turn (default 1: a one-line title; 0 /
    empty = it cannot collapse and drops instead). `keep` is the panel
    holding the cursor.

    Everything fits → every panel gets its need. Else the panel with the
    most rows above its floor gives one up, repeatedly, so the tallest
    shrink first; a panel at its floor is never cut; ties go to the later
    panel; `keep` is cut last. When even the floors overflow, panels
    COLLAPSE from the end of the list, one level at a time (every panel
    to its first form before any to its second), keep last of all and
    only to ITS smallest form (the caller makes that the cursor row and
    its expansion), so every panel with content stays represented; only
    when even the smallest forms overflow do panels drop from the end
    (keep never — it is cut under that instead). What that frees regrows
    the survivors, keep first then priority order: a collapsed panel
    reopens to the largest of its floor and its forms that fits, then the
    open panels (keep included, first) grow a row at a time toward their
    needs, then a dropped panel comes back the same way. A panel other
    than keep therefore ends at 0, one of its collapsed forms, or between
    its floor and its need."""
    avail = max(0, int(avail))
    n = len(needs)
    h = [max(0, int(x)) for x in needs]
    f = [min(h[i], max(0, int(floors[i]))) for i in range(n)]
    c: list = []
    for i in range(n):
        raw = 1 if collapsed is None else collapsed[i]
        seq = (raw,) if isinstance(raw, int) else tuple(raw)
        levels = sorted({min(f[i], max(0, int(x))) for x in seq if int(x) > 0}, reverse=True)
        c.append(tuple(x for x in levels if x > 0) if h[i] else ())
    if sum(h) <= avail:
        return h
    if sum(f) <= avail:
        # water-fill: shed one row at a time from the largest excess
        others = [i for i in range(n) if i != keep]
        while sum(h) > avail:
            pool = [i for i in others if h[i] > f[i]]
            if not pool:
                pool = [keep] if keep is not None and h[keep] > f[keep] else []
            if not pool:
                break
            best = max(pool, key=lambda i: (h[i] - f[i], i))
            h[best] -= 1
        return h
    # the floors overflow: collapse from the end, a level at a time; keep
    # last and only to its smallest form
    h = list(f)
    for lvl in range(max((len(x) for x in c), default=0)):
        if sum(h) <= avail:
            break
        for i in range(n - 1, -1, -1):
            if sum(h) <= avail:
                break
            if i == keep or lvl >= len(c[i]):
                continue
            h[i] = min(h[i], c[i][lvl])
    if sum(h) > avail and keep is not None and 0 <= keep < n and c[keep]:
        h[keep] = min(h[keep], c[keep][-1])
    if sum(h) > avail:
        # even the smallest forms overflow: drop from the end, keep never
        for i in range(n - 1, -1, -1):
            if sum(h) <= avail:
                break
            if i == keep:
                continue
            h[i] = 0
        if sum(h) > avail and keep is not None and 0 <= keep < n:
            h[keep] = max(0, avail - (sum(h) - h[keep]))
    order = ([keep] if keep is not None and 0 <= keep < n else []) + [
        i for i in range(n) if i != keep]
    # a collapsed panel reopens to the largest of its floor and its forms
    # that fits — keep first, then priority order
    for i in order:
        if h[i] == 0 or h[i] >= f[i]:
            continue
        for target in (f[i],) + c[i]:
            if target > h[i] and avail - sum(h) >= target - h[i]:
                h[i] = target
                break
    # open panels grow one row at a time toward their needs, keep first. A
    # panel is open at its floor, or at a collapsed form tall enough to be
    # a box that scrolls (3 rows or more) — never at a one-line form
    def grow_from(i: int) -> int:
        tall = [x for x in c[i] if x >= 3]
        return min(f[i], max(tall)) if tall else f[i]
    live = [i for i in order if h[i] > 0 and h[i] >= grow_from(i)]
    grew = True
    while sum(h) < avail and grew:
        grew = False
        for i in live:
            if sum(h) >= avail:
                break
            if h[i] < needs[i]:
                h[i] += 1
                grew = True
    # a dropped panel comes back at its floor, else its largest form that
    # fits — never partial (an all-or-nothing panel would render broken, a
    # GLM box under its one-row height renders nothing) — in priority order
    for i in range(n):
        if h[i] == 0 and needs[i] > 0:
            for target in (f[i],) + c[i]:
                free = avail - sum(h)
                if target > 0 and free >= target:
                    h[i] = min(needs[i], free) if target == f[i] else target
                    break
    return h


def window_rows(rows: list, avail: int, sel: int | None = None,
                exp_len: int = 0, more=None) -> list:
    """`rows` cut to `avail` lines, windowed on `sel` (an index into
    `rows`; its `exp_len` following rows travel with it when they fit),
    with a marker line from `more(n, "above" | "below")` where rows are
    hidden. No selection: the first rows and a `below` marker. Every
    marker costs one of the `avail` lines, so under 3 lines there are no
    markers, only rows (the selected one first); the selected row itself
    always survives when avail >= 1."""
    n = len(rows)
    avail = max(0, int(avail))
    if n <= avail:
        return list(rows)
    if avail == 0:
        return []
    if more is None:
        def more(k, where):
            return [(f"   … {k} more {where}", "dim")]
    if sel is not None and not (0 <= sel < n):
        sel = None
    if avail < 3:
        # no room for a marker and a row: rows only, the selected one first
        start = 0 if sel is None else min(sel, n - avail)
        return list(rows[start:start + avail])
    if sel is None:
        count = avail - 1
        return list(rows[:count]) + [more(n - count, "below")]
    want_end = min(n, sel + 1 + max(0, exp_len))
    # every window start that shows the selected row, with the markers it
    # then needs; the best shows the most of [sel, want_end) — the row and
    # its expansion — then uses the most lines, then starts highest
    best = None
    for offset in range(0, sel + 1):
        above = 1 if offset > 0 else 0
        data = avail - above
        below = 1 if offset + data < n else 0
        data = min(data - below, n - offset)
        if data < 1 or not (offset <= sel < offset + data):
            continue
        score = (min(offset + data, want_end) - sel, above + data + below, -offset)
        if best is None or score > best[0]:
            best = (score, offset, data, above, below)
    if best is None:                                  # cannot happen at avail >= 3
        return list(rows[sel:sel + avail])
    _score, offset, data, above, below = best
    out: list = []
    if above:
        out.append(more(offset, "above"))
    out += rows[offset:offset + data]
    if below:
        out.append(more(n - offset - data, "below"))
    return out[:avail]


def window_panel(panel: list, height: int, head: int = 0,
                 exp_len: int = 0, more=None, tail: int = 0) -> list:
    """A boxed panel (top border, body, bottom border) cut to `height`
    rows: the borders, the first `head` body rows (the column header) and
    the last `tail` body rows (a summary line) stay, the rest of the body
    is windowed on its selected row (`is_sel_row`) by window_rows.
    `height` < 3 gives the top rows only."""
    height = max(0, int(height))
    if len(panel) <= height:
        return list(panel)
    if height < 3 or len(panel) < 2:
        return list(panel[:height])
    top, body, bottom = panel[0], panel[1:-1], panel[-1]
    head = max(0, min(head, len(body)))
    tail = max(0, min(tail, len(body) - head))
    fixed, rest = body[:head], body[head:len(body) - tail]
    last = body[len(body) - tail:] if tail else []
    room = height - 2 - len(fixed) - len(last)
    if room <= 0:
        return ([top] + (fixed + last)[:max(0, height - 2)] + [bottom])[:height]
    sel = next((i for i, r in enumerate(rest) if is_sel_row(r)), None)
    return [top] + fixed + window_rows(rest, room, sel, exp_len, more) + last + [bottom]
