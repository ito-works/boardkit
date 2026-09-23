"""boardkit.panels — boxed panels in a flow layout, grids, panel focus and
the z zoom (kata jibot-code#tjq1, routing board slice 11).

A Panel is a boxed region with its title in the top border, holding a Grid
(header row, a rule under it, fixed columns between light vertical rules,
one line per row, a cell too long for its column truncated with an ellipsis)
or plain lines. `flow` lays panels out the way the approved mockup does: each
panel takes the width its content needs, panels fill bands left to right, a
band is as tall as its tallest panel, the last panel of a band stretches to
the right edge, and a panel wider than the screen is clipped at its border.

PanelNav is the board side of the keys, over the engine's standard keymap:
Tab / Shift-Tab move the focus between panels (the engine emits
panel_next / panel_prev), j / k move the row cursor inside the focused panel
(the board binds them to their own names so a zoom can scroll instead),
h / l move a column cursor where a panel asks for one, and `z` / Esc are the
engine's zoom (jibot-code#4vwr): the board's `with_zoom` accepts a zoom only
on the focused panel, so the zoom never outlives a focus change. The items a
board hands the engine are the focused panel's rows, and they stay the same
while zoomed, so z (or Esc) returns with the focus and cursor where they were.

Curses-free; nothing here reads or writes anything.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from boardkit import layout as L
from boardkit.cursor import PAGE_ROWS
from boardkit.text import cell_width, clip, glyph

RULE = {False: ("─", "│"), True: ("-", "|")}       # (rule, column separator) by ascii_only
FOCUS_STYLE = "amber"
# under --ascii, layout.fit_row turns every other non-ASCII character into
# "?"; these common ones get a one-cell ASCII form first, so a width measured
# in the Unicode text still holds
ASCII_FORMS = str.maketrans({"·": "|", "—": "-", "…": "~", "→": ">", "↳": ">", "█": "#"})


def _ascii_row(row: list) -> list:
    return [(t.translate(ASCII_FORMS), s) for t, s in row]


@dataclass(frozen=True)
class Col:
    title: str
    width: int
    right: bool = False


def _cell(value) -> tuple:
    return value if isinstance(value, tuple) else (str(value), "")


def fit_cell(text: str, width: int, right: bool, ascii_only: bool) -> str:
    """`text` in exactly `width` cells: padded, or truncated with the
    single-cell ellipsis. Never wrapped."""
    if width <= 0:
        return ""
    if cell_width(text) > width:
        text = clip(text, width - 1) + glyph("clip", ascii_only)
    pad = " " * (width - cell_width(text))
    return pad + text if right else text + pad


@dataclass
class Grid:
    cols: list
    rows: list
    footer: list | None = None          # one styled row under the grid, never measured
    sep: str | None = None              # None = " │ " (" | " under --ascii)
    # each row's identity (a lane name, a seat, a role): the item key the
    # cursor and a zoom anchor to, so a reload that reorders or drops rows
    # never moves them onto another entity. None = the row's position.
    keys: list | None = None

    def _sep(self, ascii_only: bool) -> str:
        return self.sep if self.sep is not None else f" {RULE[ascii_only][1]} "

    def width(self, ascii_only: bool = False) -> int:
        if not self.cols:
            return 0
        return sum(c.width for c in self.cols) + cell_width(self._sep(ascii_only)) * (len(self.cols) - 1)

    def height(self) -> int:
        return 2 + len(self.rows) + (1 if self.footer else 0)

    def _line(self, cells, ascii_only: bool, sel_row: bool, col: int | None) -> list:
        sep = self._sep(ascii_only)
        out = []
        for i, c in enumerate(self.cols):
            text, style = _cell(cells[i]) if i < len(cells) else ("", "")
            seg = (fit_cell(text, c.width, c.right, ascii_only), style)
            if sel_row and (col is None or col == i):
                seg = (seg[0], L.sel(style))
            out.append(seg)
            if i < len(self.cols) - 1:
                out.append((sep, L.sel("box") if sel_row and col is None else "box"))
        return out

    def render(self, cursor: int | None = None, col: int | None = None,
               ascii_only: bool = False) -> list:
        head = [(fit_cell(c.title, c.width, c.right, ascii_only), "hdr") for c in self.cols]
        sep = self._sep(ascii_only)
        header = []
        for i, seg in enumerate(head):
            header.append(seg)
            if i < len(head) - 1:
                header.append((sep, "box"))
        out = [header, [(RULE[ascii_only][0] * self.width(ascii_only), "box")]]
        for r, cells in enumerate(self.rows):
            out.append(self._line(cells, ascii_only, cursor == r, col))
        if self.footer:
            out.append(list(self.footer))
        return out


@dataclass
class Panel:
    key: str
    title: str
    grid: Grid | None = None
    lines: list = field(default_factory=list)       # styled rows, when there is no grid
    zoom: "Panel | None" = None                     # what z shows for the whole panel
    detail: Callable | None = None                  # (row, col) -> Panel: a row's zoom
    cols_nav: int = 0                               # first column of the column cursor; 0 = none

    def rows(self) -> int:
        """How many cursor rows the panel has: its grid rows, else none."""
        return len(self.grid.rows) if self.grid is not None else 0

    def row_keys(self) -> list:
        n = self.rows()
        keys = self.grid.keys if self.grid is not None and self.grid.keys is not None else None
        if keys is None or len(keys) != n:
            return [str(i) for i in range(n)]
        return [str(k) for k in keys]

    def natural_width(self, ascii_only: bool = False) -> int:
        if self.grid is not None:
            w = self.grid.width(ascii_only)
        else:
            w = max((L.row_width(r) for r in self.lines), default=0)
        return max(w, cell_width(self.title) + 2)

    def natural_height(self) -> int:
        return self.grid.height() if self.grid is not None else max(1, len(self.lines))

    def body(self, cursor: int | None = None, col: int | None = None,
             ascii_only: bool = False) -> list:
        if self.grid is not None:
            return self.grid.render(cursor, col, ascii_only)
        return [list(r) for r in self.lines]

    def box(self, width: int, height: int, focused: bool = False, cursor: int | None = None,
            col: int | None = None, ascii_only: bool = False) -> list:
        """The boxed panel, exactly `width` cells wide and `height` rows tall
        (body rows past the height are cut; the band's extra rows are blank)."""
        body = self.body(cursor, col, ascii_only)[:max(0, height - 2)]
        body += [[] for _ in range(max(0, height - 2 - len(body)))]
        rows = _box(self.title, body, width, ascii_only)
        if width < 8:
            # the plain-rows fallback has no borders, so one row fewer
            rows = (rows + [[(" " * width, "")]] * height)[:height]
        elif focused:
            rows = _focus(rows)
        return rows


def _box(title: str, body: list, width: int, ascii_only: bool) -> list:
    """layout.box, every row exactly `width` cells: under 8 cells layout.box
    falls back to unbordered rows, which are fitted and padded here."""
    head = [(title, "title")]
    if ascii_only:
        head, body = _ascii_row(head), [_ascii_row(r) for r in body]
    rows = L.box(head, body, width, ascii_only)
    if width < 8:
        rows = [L.pad_row(L.fit_row(r, width, ascii_only), width) for r in rows]
    return rows


def _focus(rows: list) -> list:
    """The outer border in the focus style: the top and bottom rows and each
    body row's two edge segments — never a grid's own rules inside."""
    def recolor(seg):
        return (seg[0], FOCUS_STYLE if seg[1] == "box" else seg[1])
    out = []
    for i, r in enumerate(rows):
        if i in (0, len(rows) - 1) or len(r) < 2:
            out.append([recolor(s) for s in r])
        else:
            out.append([recolor(r[0])] + list(r[1:-1]) + [recolor(r[-1])])
    return out


def flow(panels: list, width: int, ascii_only: bool = False) -> list:
    """Bands of (panel, width): each panel its natural width plus the box
    (capped at `width`), left to right with a one-cell gap, a new band when
    the next one does not fit, the last of each band stretched to the edge."""
    bands, cur, used = [], [], 0
    for p in panels:
        pw = min(p.natural_width(ascii_only) + 4, width)
        if cur and used + 1 + pw > width:
            bands.append(cur)
            cur, used = [], 0
        cur.append([p, pw])
        used += pw + (1 if len(cur) > 1 else 0)
    if cur:
        bands.append(cur)
    for band in bands:
        used = sum(w for _p, w in band) + len(band) - 1
        band[-1][1] += max(0, width - used)
    return [[(p, w) for p, w in band] for band in bands]


def compose(panels: list, width: int, focus: str | None = None, cursor: int | None = None,
            col: int | None = None, ascii_only: bool = False) -> tuple:
    """(rows, sel_y, band_y): the bands' boxes joined side by side, every
    row exactly `width` cells; sel_y is the screen row of the focused
    panel's cursor row (None without one), band_y the top of its band."""
    rows: list = []
    sel_y = band_y = None
    for band in flow(panels, width, ascii_only):
        h = max(p.natural_height() for p, _w in band) + 2
        boxes = []
        for p, w in band:
            mine = p.key == focus
            cur = cursor if mine and p.rows() else None
            boxes.append(p.box(w, h, focused=mine, cursor=cur, col=col if mine else None,
                               ascii_only=ascii_only))
            if mine:
                band_y = len(rows)
                if cur is not None and 0 <= cur < p.rows():
                    sel_y = len(rows) + 1 + 2 + cur          # top border, header, rule
        for i in range(h):
            line: list = []
            for n, b in enumerate(boxes):
                if n:
                    line.append((" ", ""))
                line += b[i]
            rows.append(line)
    return rows, sel_y, band_y


def viewport(total: int, avail: int, band_y: int | None, sel_y: int | None) -> int:
    """The first row to show when `total` rows are taller than `avail`:
    the focused panel's band top, moved down just far enough that the cursor
    row is on screen."""
    if avail <= 0 or total <= avail:
        return 0
    off = min(band_y or 0, total - avail)
    if sel_y is not None and sel_y >= off + avail:
        off = sel_y - avail + 1
    if sel_y is not None and sel_y < off:
        off = sel_y
    return max(0, min(off, total - avail))


def zoom_rows(panel: Panel, width: int, avail: int, pos: int, ascii_only: bool = False) -> list:
    """The zoom view: `panel` boxed across the full width, its body windowed
    on `pos` (the selected row, highlighted, when the body is a grid)."""
    if avail <= 0:
        return []
    grid = panel.grid is not None and panel.rows() > 0
    body = panel.body(cursor=pos if grid else None, ascii_only=ascii_only)
    head = 2 if panel.grid is not None else 0
    tail = 1 if panel.grid is not None and panel.grid.footer else 0
    fixed, rest = body[:head], body[head:len(body) - tail]
    last = body[len(body) - tail:] if tail else []
    room = max(0, avail - 2 - len(fixed) - len(last))
    sel = min(max(0, pos), max(0, len(rest) - 1)) if rest else None
    shown = fixed + L.window_rows(rest, room, sel) + last
    return _box(panel.title, shown, width, ascii_only)[:avail]


def item_key(panel: Panel, row_key: str | None) -> str:
    """`<panel key>:<row key>`; a panel key carries no ":" of its own."""
    return f"{panel.key}:{'' if row_key is None else row_key}"


def split_key(key) -> tuple:
    """(panel key, row key | None) from an item key."""
    if not isinstance(key, str) or ":" not in key:
        return None, None
    pk, _, r = key.partition(":")
    return pk, (r or None)


class PanelNav:
    """Focus, remembered rows, the column cursor and the zoom position for
    one set of panels. Panels are named by key, so a reload that reorders or
    drops one never moves the focus to a different panel silently: a focus
    whose panel left falls back to the first."""

    def __init__(self):
        self.focus: str | None = None
        self.rows: dict = {}             # panel key -> (remembered row key, its index)
        self.col: int = 0
        self.zoom_pos: int = 0
        self._zoom_key: str | None = None

    def focused(self, panels: list) -> Panel | None:
        for p in panels:
            if p.key == self.focus:
                return p
        if panels:
            self.focus = panels[0].key
            return panels[0]
        return None

    def _row(self, p: Panel) -> str | None:
        """The panel's remembered row key: the same row when it is still
        there, else the row now at its old position."""
        keys = p.row_keys()
        if not keys:
            return None
        key, idx = self.rows.get(p.key, (None, 0))
        return key if key in keys else keys[min(max(0, idx), len(keys) - 1)]

    def items(self, panels: list) -> list:
        """The engine's item keys: the focused panel's rows, or one key for
        a panel without rows (so the engine has a cursor to zoom on)."""
        p = self.focused(panels)
        if p is None:
            return []
        keys = p.row_keys()
        return [item_key(p, k) for k in keys] if keys else [item_key(p, None)]

    def current_key(self, panels: list) -> str | None:
        p = self.focused(panels)
        if p is None:
            return None
        return item_key(p, self._row(p))

    def remember(self, panels: list, cursor: int) -> None:
        p = self.focused(panels)
        keys = p.row_keys() if p is not None else []
        if keys and 0 <= cursor < len(keys):
            self.rows[p.key] = (keys[cursor], cursor)
        if p is not None and p.cols_nav and p.grid is not None:
            self.col = min(max(self.col, p.cols_nav), len(p.grid.cols) - 1)

    def column(self, panels: list) -> int | None:
        p = self.focused(panels)
        return self.col if p is not None and p.cols_nav else None

    def accept_zoom(self, zoom, panels: list):
        """The zoom a with_zoom hook should keep: one on the focused panel
        (and, for a row detail, a row it still has), else None."""
        if zoom is None:
            self._zoom_key = None
            return None
        p = self.focused(panels)
        pk, row = split_key(zoom)
        # the row matters only to a row detail: a whole-panel zoom stays up
        # when the row the cursor was on leaves the panel on a reload
        gone = row is not None and p is not None and p.detail is not None and row not in p.row_keys()
        if p is None or pk != p.key or gone:
            self._zoom_key = None
            return None
        if zoom != self._zoom_key:
            self._zoom_key, self.zoom_pos = zoom, 0
        return zoom

    def zoom_view(self, panels: list, zoom) -> Panel | None:
        """The Panel a zoom key shows: the row's detail where the panel has
        one, else the panel's own zoom, else the panel itself."""
        pk, row = split_key(zoom)
        p = next((q for q in panels if q.key == pk), None)
        if p is None:
            return None
        keys = p.row_keys()
        if p.detail is not None and row in keys:
            return p.detail(keys.index(row), self.col if p.cols_nav else None)
        return p.zoom or p

    def on_action(self, name: str, ui, panels: list, zoomed: bool):
        """The engine Action for a panel key, or None when `name` is not one
        this handles (the board then answers as it would anyway). While
        zoomed, row moves scroll the zoom and leave the cursor alone."""
        from boardkit.app import Action
        p = self.focused(panels)
        if p is None:
            return None
        stay = Action("cursor", ui.cursor_key) if ui.cursor_key else Action("notice", "")
        if name in ("panel_next", "panel_prev"):
            keys = [q.key for q in panels]
            i = keys.index(p.key)
            self.focus = keys[(i + (1 if name == "panel_next" else -1)) % len(keys)]
            return Action("cursor", self.current_key(panels))
        if zoomed:
            step = {"cursor_down": 1, "cursor_up": -1, "page_down": PAGE_ROWS, "page_up": -PAGE_ROWS,
                    "cursor_first": -10 ** 9, "cursor_last": 10 ** 9}.get(name)
            if step is None:
                return stay if name in ("cursor_left", "cursor_right") else None
            self.zoom_pos = max(0, self.zoom_pos + step)
            return stay
        if name in ("cursor_down", "cursor_up"):
            keys = p.row_keys()
            if not keys:
                return stay
            r = min(len(keys) - 1, max(0, ui.cursor + (1 if name == "cursor_down" else -1)))
            return Action("cursor", item_key(p, keys[r]))
        if name in ("cursor_left", "cursor_right") and p.cols_nav and p.grid is not None:
            last = len(p.grid.cols) - 1
            self.col = min(last, max(p.cols_nav, self.col + (1 if name == "cursor_right" else -1)))
            return stay
        return None
