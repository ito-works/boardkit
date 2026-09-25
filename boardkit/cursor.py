"""boardkit.cursor — UIState, pure key handling, overlay/expansion bookkeeping"""
from __future__ import annotations

from dataclasses import dataclass, replace

# curses key codes as plain ints so the pure key handler (and its tests)
# never import curses; the loop translates curses_mod constants to these.
KEY_DOWN, KEY_UP, KEY_ENTER, KEY_RESIZE = 258, 259, 343, 410
KEY_LEFT, KEY_RIGHT = 260, 261      # only meaningful to a board that maps them
# likewise: the loop translates the curses constants to these, so a board's
# bindings never depend on one curses build's numbering (jibot-code#gqfj)
KEY_HOME, KEY_END, KEY_NPAGE, KEY_PPAGE, KEY_BTAB = 262, 360, 338, 339, 353
KEY_CLICK = 1001        # a plain button-1 click (mouse_event carries its cell)

PAGE_ROWS = 8           # page_down / page_up when the board leaves the move to the engine

# y9nb: the STANDARD keymap. Every boardkit board answers these ACTION NAMES;
# the keys are fixed. A board may bind one of these keys to its own handler
# for the SAME name (dash-tui walks a grid on j/k/h/l — jibot-code#hh1g), or
# space / x to "noop" when it has no marks; anything else keymap_problem()
# refuses. j/k/space/x/enter/? are performed by handle_key itself when the
# board does not map them; the rest are emitted for the board's on_action,
# and app.tui_loop applies default_move() for first/last/page when the board
# answers noop.
STANDARD_ACTIONS: dict[int, str] = {
    ord("j"): "cursor_down", KEY_DOWN: "cursor_down",
    ord("k"): "cursor_up", KEY_UP: "cursor_up",
    ord("h"): "cursor_left", KEY_LEFT: "cursor_left",
    ord("l"): "cursor_right", KEY_RIGHT: "cursor_right",
    9: "panel_next", KEY_BTAB: "panel_prev",                 # Tab / Shift-Tab
    KEY_NPAGE: "page_down", 4: "page_down",                  # PgDn / ^d
    KEY_PPAGE: "page_up", 21: "page_up",                     # PgUp / ^u
    ord("g"): "cursor_first", KEY_HOME: "cursor_first",
    ord("L"): "cursor_last", KEY_END: "cursor_last",
    ord(" "): "mark", ord("x"): "mark",
    KEY_ENTER: "toggle", 10: "toggle", 13: "toggle",
    ord("?"): "help",
    ord("["): "board_prev", ord("]"): "board_next",
    # 6swq: scroll the open expansion a line, so one taller than the screen
    # can be read to its end; the loop applies them (expand.scroll_expansion)
    ord("J"): "expansion_down", ord("K"): "expansion_up",
}
# the engine's own keys: a board's map never reaches them
ENGINE_KEYS: frozenset[int] = frozenset((KEY_RESIZE, 27, ord("q"), ord("r"), ord("["), ord("]")))
_EMITTED = frozenset(("cursor_left", "cursor_right", "panel_next", "panel_prev",
                      "page_down", "page_up", "cursor_first", "cursor_last",
                      "board_prev", "board_next"))
_ENGINE_MOVES = frozenset(("cursor_first", "cursor_last", "page_down", "page_up"))


def _key_name(key: int) -> str:
    return f"'{chr(key)}'" if 32 < key < 127 else str(key)


def keymap_problem(extra) -> str | None:
    """Why a board's `extra_keys` is refused, or None. Checked once, by the
    loop, before curses takes the screen. A standard key may carry only its
    own action name (space / x also "noop"); the engine's keys carry nothing."""
    for key in sorted(extra or {}):
        name = extra[key]
        if key in ENGINE_KEYS:
            return f"key {_key_name(key)} is the engine's"
        std = STANDARD_ACTIONS.get(key)
        if std is None:
            continue
        allowed = (std, "noop") if std == "mark" else (std,)
        if name not in allowed:
            return f"standard key {_key_name(key)} bound to '{name}'; its action is '{std}'"
    return None


def default_move(ui: UIState, action: str) -> UIState:
    """The engine's own cursor move for first / last / page — applied by the
    loop when the board answered one of those actions with noop. Any other
    action returns `ui` itself."""
    if action not in _ENGINE_MOVES:
        return ui
    last = max(0, ui.items_len - 1)
    if action == "cursor_first":
        return replace(ui, cursor=0)
    if action == "cursor_last":
        return replace(ui, cursor=last)
    step = PAGE_ROWS if action == "page_down" else -PAGE_ROWS
    return replace(ui, cursor=min(last, max(0, ui.cursor + step)))


# --- overlay ownership -------------------------------------------------------
# Each overlay open gets a token; the detail fetch thread delivers
# (token, lines) through a queue the main tick drains. Results whose token
# is not the live one (Esc, ?, another enter happened since) are dropped.

def new_overlay() -> dict:
    return {"lines": None, "token": 0, "inflight": False, "counter": 0, "key": None,
            "pending": {}, "scroll": 0}


def open_overlay(overlay: dict, lines) -> int:
    """`lines` None = loading (expansion renders 'loading…')."""
    overlay["counter"] += 1
    overlay["token"] = overlay["counter"]
    overlay["lines"] = list(lines) if lines is not None else None
    overlay["inflight"] = False
    overlay["scroll"] = 0                          # 6swq: every open starts at the top
    return overlay["token"]


def close_overlay(overlay: dict) -> None:
    overlay["counter"] += 1
    overlay["token"] = overlay["counter"]          # invalidates in-flight fetch
    overlay["lines"] = None
    overlay["inflight"] = False
    overlay["key"] = None
    overlay["scroll"] = 0


def apply_detail_result(overlay: dict, token: int, lines: list[str]) -> bool:
    """Deliver a fetch result; False when the token is stale (dropped)."""
    if token != overlay["token"] or not overlay["inflight"]:
        return False
    overlay["lines"] = list(lines)
    overlay["inflight"] = False
    return True


# --- pure key handling -------------------------------------------------------

@dataclass(frozen=True)
class UIState:
    items_len: int = 0
    cursor: int = 0
    overlay: str | None = None        # None | "help"
    gen: int = 0
    quit: bool = False
    cursor_key: str | None = None     # the item key the cursor is anchored to
    # marked item keys (multi-select) — view-state, appended LAST so
    # existing positional constructions (dash-tui vendors this) keep binding
    marked: frozenset = frozenset()
    # 4vwr: the item KEY `z` took the zoom on, or None. The engine has no
    # notion of a panel — the board maps the key to the panel that paints
    # it (marshalboard's render._sel_panel reads the key's prefix) — so a
    # row leaving on a refresh does NOT unzoom; the panel emptying does,
    # and the BOARD says so by refusing the zoom (app.tui_loop's
    # `with_zoom` hook). Appended last, same rule as `marked`.
    zoom: str | None = None
    boards: int | None = None         # y9nb: `board: …` windows on this server, None = unknown / not in tmux


def clamp_ui(ui: UIState, items_len: int) -> UIState:
    cur = min(max(0, ui.cursor), max(0, items_len - 1))
    return replace(ui, items_len=items_len, cursor=cur)


def resolve_cursor(ui: UIState, its: list, prune_marks: bool = True) -> UIState:
    """Re-resolve the cursor index against a fresh items() list: the
    anchored key wins (rows above may have come or gone); a vanished key
    falls back to the nearest index. Always re-anchors to the row under
    the resulting index. Marks are pruned to keys still on the board —
    a marked row that left is silently dropped.

    `prune_marks=False` keeps them: while a zoom is up (4vwr) `its` is ONE
    panel's rows, so "not in `its`" means HIDDEN, not gone, and pruning
    against it would destroy every mark in the panels the zoom covers.
    The loop passes False for exactly those frames; the unzoom's ordinary
    prune then collects any row that really did leave meanwhile."""
    if prune_marks and ui.marked:
        live = {it.key for it in its}
        if not ui.marked <= live:
            ui = replace(ui, marked=frozenset(ui.marked & live))
    if ui.cursor_key is not None:
        for i, it in enumerate(its):
            if it.key == ui.cursor_key:
                return replace(ui, items_len=len(its), cursor=i)
    ui = clamp_ui(ui, len(its))
    return replace(ui, cursor_key=its[ui.cursor].key if its else None)


def handle_key(ui: UIState, key: int, extra: dict | None = None) -> tuple[UIState, str | None]:
    """Pure: (new ui, action). Actions: quit / refresh / resize / help /
    toggle / close / one of STANDARD_ACTIONS' names / None. The cursor
    walks j/k/arrow-down/arrow-up itself and is always clamped; h/l, Tab /
    Shift-Tab, PgUp/PgDn/^u/^d and g/L/Home/End are emitted as the standard
    action name for the board's on_action (or the loop's default_move when
    the board answers noop) rather than moved here. `extra` maps
    board-specific key codes to action strings and is checked before every
    key the board may want for itself — the j/k/arrow walk included, so a
    board whose rows form a grid or two columns can move the cursor
    spatially through an Action("cursor", key) of its own — but after
    KEY_RESIZE, Esc, q, r and `[`/`]`, which stay the engine's (a board
    cannot map those). `handle_key` itself stays permissive: it does not
    refuse a board's rebinding of a standard key to a different meaning —
    that refusal is `keymap_problem`, applied once by the loop before curses
    takes the screen. Space/x toggle the cursor row's key in `marked` unless
    the board maps them. J / K (6swq) emit expansion_down / expansion_up,
    which the loop answers by scrolling the open expansion. `z` (4vwr) toggles `zoom` between None and the
    cursor's item key — the board turns that key into the panel it paints,
    and may REFUSE the zoom (app.tui_loop's `with_zoom` hook); it is checked
    after `extra` like the walk, so a board that wants `z` for itself still
    wins."""
    ui = clamp_ui(ui, ui.items_len)
    last = max(0, ui.items_len - 1)
    if key == -1:
        return ui, None
    if key == KEY_RESIZE:
        return ui, "resize"
    if key == 27:
        return replace(ui, overlay=None), "close"
    if key == ord("q"):
        return replace(ui, quit=True), "quit"
    if key == ord("r"):
        return replace(ui, gen=ui.gen + 1), "refresh"
    if key in (ord("["), ord("]")):                 # y9nb: the engine's, like q / r
        return ui, STANDARD_ACTIONS[key]
    if extra and key in extra:
        return ui, extra[key]
    if key == ord("z"):
        # 4vwr: the panel zoom, tmux `prefix z`. Anchored to the cursor's
        # ITEM KEY, never dereferenced here — the board resolves it to a
        # panel. No cursor row (an empty board): nothing to zoom.
        return replace(ui, zoom=None if ui.zoom else ui.cursor_key), None
    if key in (ord("j"), KEY_DOWN):
        return replace(ui, cursor=min(last, ui.cursor + 1)), None
    if key in (ord("k"), KEY_UP):
        return replace(ui, cursor=max(0, ui.cursor - 1)), None
    if key in (ord(" "), ord("x")):
        if ui.cursor_key is None:
            return ui, None
        m = (ui.marked - {ui.cursor_key} if ui.cursor_key in ui.marked
             else ui.marked | {ui.cursor_key})
        return replace(ui, marked=frozenset(m)), None
    if key in (ord("J"), ord("K")):                 # 6swq: the loop scrolls the expansion
        return ui, STANDARD_ACTIONS[key]
    if key in (KEY_ENTER, 10, 13):
        if ui.items_len == 0:
            return ui, None
        return ui, "toggle"
    if key == ord("?"):
        return replace(ui, overlay="help"), "help"
    name = STANDARD_ACTIONS.get(key)
    if name in _EMITTED:                            # y9nb: h/l/Tab/page/first/last → the board
        return ui, name
    return ui, None


def mouse_event(curses_mod) -> tuple[int, tuple[int, int] | None]:
    """Translate the pending KEY_MOUSE event to (key, (y, x) | None):
    KEY_UP / KEY_DOWN for the wheel, ord(" ") for a modifier click (the
    mark toggle), KEY_CLICK for a plain button-1 click, -1 otherwise.
    The cell rides along for the clicks so the loop can resolve it
    against the frame's hit map (8dpg).

    Natural-scroll mapping (Joi): swipe down -> cursor down. Swipe down
    reaches ncurses as wheel-up = BUTTON4. Swipe up is wheel-down, and
    mouse-v1 ncurses has no button-5 bit: getmouse() RAISES for it, so a
    failed getmouse is treated as the wheel-down event. (ncurses also
    errors on an empty event queue; with the mask set in _tui_loop the
    only such event seen in practice is wheel-down — a spurious one costs
    a single cursor-up.) Any other button -> -1 (ignored)."""
    try:
        _id, x, y, _z, bstate = curses_mod.getmouse()
    except curses_mod.error:
        return KEY_UP, None
    if bstate & getattr(curses_mod, "BUTTON5_PRESSED", 0):
        return KEY_UP, None
    if bstate & getattr(curses_mod, "BUTTON4_PRESSED", 0):
        return KEY_DOWN, None
    mods = (getattr(curses_mod, "BUTTON_SHIFT", 0)
            | getattr(curses_mod, "BUTTON_CTRL", 0)
            | getattr(curses_mod, "BUTTON_ALT", 0))
    # CLICKED / PRESSED (and the DOUBLE / TRIPLE merges ncurses reports
    # for fast repeats, each one click here) — never RELEASED, which would
    # double-fire the slow clicks ncurses fails to resolve into one CLICKED.
    edges = (getattr(curses_mod, "BUTTON1_CLICKED", 0)
             | getattr(curses_mod, "BUTTON1_PRESSED", 0)
             | getattr(curses_mod, "BUTTON1_DOUBLE_CLICKED", 0)
             | getattr(curses_mod, "BUTTON1_TRIPLE_CLICKED", 0))
    if bstate & edges:
        return (ord(" ") if bstate & mods else KEY_CLICK), (int(y), int(x))
    return -1, None


def mouse_key(curses_mod) -> int:
    """The key-only view of mouse_event: wheel -> KEY_UP / KEY_DOWN,
    modifier click -> the mark toggle; a plain click reads as -1 here
    (dash-tui vendors this API — the loop uses mouse_event instead)."""
    key, _pos = mouse_event(curses_mod)
    return -1 if key == KEY_CLICK else key


def toggle_expanded(exp: dict, key: str) -> str | None:
    """Accordion: the new expanded key (or None when `key` was open)."""
    return None if exp.get("key") == key else key
