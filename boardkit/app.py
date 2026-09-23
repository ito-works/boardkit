"""boardkit.app — the generic curses loop, clipboard, tmux jump, url open.

`tui_loop` is marshal-board's old `_tui_loop` with every board-specific
call routed through a `Board`: the loop owns curses, the key table, the
overlay/expansion bookkeeping, the copy thread and the footer notice; the
board owns the data, the rows and what a key means.

One OPTIONAL Board hook, `with_zoom(state, zoom, width, height) ->
(state, zoom)` (4vwr): the loop calls it immediately before `layout()` to
thread the panel zoom into the state, and takes back the zoom the board
ACCEPTED — a board refuses one it cannot honour (no boxes at this size, or
the zoomed panel has no rows left) and the loop drops it. A board that does
not define it is unaffected: the loop clears the zoom instead, so `z` there
arms nothing. An expansion the zoom hides stays OPEN and unpainted, and Esc
spends a press on it; see the comment at the layout call for why closing it
automatically was tried and reverted.

`[` / `]` and the board-window count (y9nb) are the loop's: `switch` and
`board_count` are injectable, `boardkit.windows` by default.

curses is NEVER imported here: the loop receives the module as a
parameter (`curses_mod`) so headless tests drive it with a fake.
"""
from __future__ import annotations

import os
import queue
import re
import subprocess
import threading
import time
from dataclasses import dataclass, replace
from typing import Callable, Protocol

import boardkit.expand as expand_mod
from boardkit.cursor import (
    KEY_DOWN, KEY_UP, KEY_LEFT, KEY_RIGHT, KEY_ENTER, KEY_RESIZE, KEY_CLICK,
    KEY_HOME, KEY_END, KEY_NPAGE, KEY_PPAGE, KEY_BTAB, UIState, apply_detail_result,
    close_overlay, default_move, handle_key, keymap_problem, mouse_event,
    new_overlay, open_overlay, resolve_cursor, toggle_expanded,
)
from boardkit import windows as kit_windows
from boardkit.layout import hit_at, hit_map
from boardkit.styles import init_styles, style_attr
from boardkit.text import BOX, BOX_ASCII, CONTROL_RE, cell_width, clean, clip, to_ascii
from boardkit.workers import Runner, SourceTimeout

NOTICE_S = 3.0                     # footer notice lifetime (copy feedback)
CONFIRM_TIMEOUT_S = 300.0          # a confirm runner's deadline (the sweep lane probes every worktree)
CONFIRM_PROMPT_S = 30.0            # a y/N prompt older than this expires: a stale answer is no answer
_CONTROL = CONTROL_RE               # C0, DEL, C1 — exactly what text.clean scrubs
_RESULT_TAIL = 160                 # footer-sized tail of a runner's last line

# What an untrusted Action("open") value may be handed to the desktop
# opener. `open <anything>` on macOS resolves paths and custom schemes, so
# a board that hands the loop text it did not author gets exactly two
# schemes; everything else needs Action(..., trusted=True).
_OPENABLE = re.compile(r"^(https?://|obsidian:)")


# --- side effects ------------------------------------------------------------

def clipboard_copy(text: str, env: dict | None = None,
                   run=subprocess.run) -> str | None:
    """Copy `text` to the system clipboard. None on success, else the
    reason. pbcopy first (macOS system clipboard, works inside tmux),
    tmux load-buffer -w as the fallback."""
    for argv in (["pbcopy"], ["tmux", "load-buffer", "-w", "-"]):
        try:
            proc = run(argv, input=text.encode(), env=env, timeout=5,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode == 0:
            return None
    return "no clipboard helper (pbcopy / tmux load-buffer)"


def open_in_tmux(argv: list[str], env: dict | None = None, run=subprocess.run,
                 environ=os.environ) -> str:
    """Open `argv` in a tmux split beside this pane. Returns the footer
    notice — outside tmux there is no pane to split, so say so instead."""
    if not environ.get("TMUX"):
        return "not inside tmux: run " + " ".join(argv)
    try:
        p = run(["tmux", "split-window", "-h", *argv], env=env, timeout=5,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"tmux split failed: {exc}"
    return ("opened in a tmux split" if p.returncode == 0
            else f"tmux split failed: rc={p.returncode}")


# A jump target is either a tmux pane id or a plain session name: nothing
# that could read as a flag, a target-syntax (`:` / `.`, which tmux itself
# never leaves in a session name) or a control sequence reaches `-t`.
_PANE_TARGET = re.compile(r"%\d+\Z")
_SESSION_TARGET = re.compile(r"[^\s%:.\-\x00-\x1f\x7f][^\s%:.\x00-\x1f\x7f]*\Z")


def jump_to_tmux(target: str, env: dict | None = None, run=subprocess.run,
                 environ=os.environ) -> str:
    """Switch this tmux client to `target`: a pane id (`%N`) selects the
    pane, then its window, then switches the client to its session; a
    session name just switches the client (`=name` = exact match, since
    -t prefix-matches otherwise). Returns the footer notice.

    Outside tmux there is no client to switch, so the notice names the
    `tmux attach` to run instead. select-pane / select-window act on the
    server, not a client, so for a pane target they still run: the attach
    then lands on that pane. `attach -t` takes a session, never a bare
    pane id (tmux only parses window/pane syntax past a `:` or `.`), so
    the pane's session is looked up for the hint (fresheyes round 1)."""
    target = str(target)
    pane = bool(_PANE_TARGET.fullmatch(target))
    if not pane and not _SESSION_TARGET.fullmatch(target):
        return "not a tmux target"
    inside = bool(environ.get("TMUX"))
    cmds = []
    if pane:
        cmds += [["tmux", "select-pane", "-t", target],
                 ["tmux", "select-window", "-t", target]]
    if inside:
        cmds.append(["tmux", "switch-client", "-t", target if pane else "=" + target])
    for argv in cmds:
        try:
            p = run(argv, env=env, timeout=5,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError) as exc:
            return f"tmux jump failed: {exc}"
        if p.returncode != 0:
            return f"tmux jump failed: rc={p.returncode}"
    if inside:
        return f"jumped to {target}"
    session = _pane_session(target, env, run) if pane else target
    if session is None:
        return f"not inside tmux: pane {target} selected; tmux attach to its session"
    return f"not inside tmux: tmux attach -t ={session}"


def _pane_session(pane: str, env, run) -> str | None:
    """The session name owning tmux pane `pane`, or None when the server
    cannot say (no server, odd name)."""
    try:
        p = run(["tmux", "display-message", "-p", "-t", pane, "#{session_name}"],
                env=env, timeout=5, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    out = getattr(p, "stdout", None) or b""
    if isinstance(out, bytes):
        out = out.decode("utf-8", "replace")
    name = out.strip()
    if p.returncode != 0 or not _SESSION_TARGET.fullmatch(name):
        return None
    return name


def open_url(url: str, env: dict | None = None, run=subprocess.run) -> str:
    """Hand `url` to the desktop opener. Returns the footer notice."""
    try:
        p = run(["open", url], env=env, timeout=5,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"open failed: {exc}"
    return "opened" if p.returncode == 0 else f"open failed: rc={p.returncode}"


# --- painting ----------------------------------------------------------------

def paint(stdscr, curses_mod, rows, attrs, width: int, height: int) -> None:
    """Paint compose_rows() output: each segment with its own attr.

    `clean()` runs here too, not only in layout.fit_row: paint is the last
    gate before addstr, so a board that composes a row without going
    through fit_row still cannot put an escape sequence on the terminal.
    """
    stdscr.erase()
    for y, row in enumerate(rows[:height]):
        limit = width - 1 if y == height - 1 else width   # bottom-right cell raises
        x = 0
        for text, style in row:
            if x >= limit:
                break
            text = clip(clean(text), limit - x)
            if not text:
                continue
            try:
                stdscr.addstr(y, x, text, style_attr(style, attrs))
            except curses_mod.error:
                pass
            x += cell_width(text)


def overlay_rows(lines, width: int, height: int, ascii_only: bool) -> list:
    """Pure: boxed overlay rows (y, x, segments) for the detail / help
    overlay — a title bar, then the content, framed and cleared."""
    top = 1
    max_rows = max(0, height - top - 1)
    if width < 8 or max_rows < 2:
        return []
    inner = min(width - 6, 96)
    shown = [clip(clean(str(t)), inner) for t in lines[:max_rows - 2]]
    if not shown:
        return []
    box_w = max(cell_width(t) for t in shown)
    box_w = max(box_w, min(inner, 24))
    b = BOX_ASCII if ascii_only else BOX
    x0 = max(0, (width - box_w - 4) // 2)
    out = []
    title = shown[0]
    fill = box_w + 2 - cell_width(title) - 1
    out.append((top, x0, [(b["tl"] + b["h"] + " ", "box"), (title, "title"),
                          (" " + b["h"] * max(0, fill - 1) + b["tr"], "box")]))
    for i, text in enumerate(shown[1:], start=1):
        pad = " " * (box_w - cell_width(text))
        out.append((top + i, x0, [(b["v"] + " ", "box"), (text + pad, ""),
                                  (" " + b["v"], "box")]))
    out.append((top + len(shown), x0,
                [(b["bl"] + b["h"] * (box_w + 2) + b["br"], "box")]))
    return out


def paint_overlay(stdscr, curses_mod, lines, width, height, attrs,
                  ascii_only: bool = False) -> None:
    for y, x, segs in overlay_rows(lines, width, height, ascii_only):
        for text, style in segs:
            text = clean(text)          # last gate before addstr (see paint)
            if ascii_only:
                text = to_ascii(text)
            try:
                stdscr.addstr(y, x, text, style_attr(style, attrs) | curses_mod.A_REVERSE
                              if style == "" else style_attr(style, attrs))
            except curses_mod.error:
                pass
            x += cell_width(text)


# --- the board contract ------------------------------------------------------

@dataclass
class Action:
    """What a board-specific key means. `value` carries the payload:
    copy   str | callable -> str | (text, notice) | None   (run on a thread)
    spawn  argv list                                       (child process)
    jump   tmux target: pane id (%N) or session name       (switch-client)
    open   url str                                          (desktop opener)
    notice footer text
    cursor the item key to jump the cursor to
    expand the item key whose inline expansion to open (exactly as enter
           does; a no-op when it is already open; a notice when the key is
           not on screen)
    confirm Confirm (below): footer y/N prompt, then argv on the runner
    menu   Menu (below): footer key-picker over several Confirms; the
           listed key is the confirmation

    `trusted` waives the loop's open-scheme rule: an untrusted "open"
    value must match `_OPENABLE` (http/https/obsidian) before it reaches
    the desktop opener, so board text that came off the wire cannot turn
    into `open <arbitrary thing>`.
    """
    kind: str                       # "noop"|"copy"|"open"|"spawn"|"jump"|"notice"|"cursor"|"confirm"|"menu"
    value: object = None
    trusted: bool = False


@dataclass
class PreCheck:
    """What a Confirm's pre-check found: `text` is shown in the prompt;
    `ok=False` refuses the whole flow — a footer notice, no prompt, and
    nothing written (the clipboard included). A pre-check that raises, or
    returns anything else, refuses the same way (fail closed)."""
    text: str
    ok: bool = True


@dataclass
class Confirm:
    """Action("confirm") payload: `argv` is the FIXED list to run after
    exactly `y`, absolute program path first — the loop re-vets it with
    argv_ok() at the keypress and at the `y`. `precheck` runs on a worker
    thread (read-only by contract: nothing before `y` may write) and its
    PreCheck lands in the prompt; with `recheck` it runs again on the
    runner thread right before argv and refuses the run unless still ok.
    `copy_text` is the shell-quoted rendering of argv for a board that
    wants to copy instead of run (a remote row); the loop itself never
    writes it — a refused pre-check is a notice, nothing more."""
    label: str
    prompt: str
    argv: list
    precheck: Callable[[], PreCheck] | None = None
    recheck: bool = False
    copy_text: str | None = None
    timeout: float = CONFIRM_TIMEOUT_S
    # 715z: the follow-up COMMAND a board knows and a run's own output cannot
    # carry — `[r] resume` removes a label, and the operator then has to nudge
    # the worker with a command naming a pane only the board can see. After a
    # run that produced a result, the loop puts this on the CLIPBOARD and says
    # so beside the result. Not prompt text: a menu option's prompt is never
    # displayed, because the key IS the `y`. Not footer text either: the
    # footer is ONE row clipped to the pane width, so a command long enough to
    # be useful cannot be read there (both found by review).
    done_copy: str | None = None


@dataclass
class Menu:
    """Action("menu") payload (ssf2): a one-keystroke picker — `options` maps a
    KEYCODE to what that key does, either a `Confirm` (a mutation, run after
    the keypress with every Confirm guard) or a plain `Action` (a view: expand,
    copy, jump, a notice), which the loop dispatches exactly as it would from a
    board key. 715z's WAITING menu is mostly views, so the picker is not a
    mutation-only shape.

    The listed key IS the `y`: it is the confirmation, because the prompt
    names each key's action and a picker that then asked y/N would cost two
    keystrokes for one decision. Every other Confirm guard still applies —
    argv_ok() at open AND at the keypress, the option's `precheck` on the
    runner thread when it sets `recheck`, the loop's own process-group
    runner, and `refresh_due` afterwards.

    `precheck` (the MENU's own, distinct from the options') runs on a worker
    thread before the picker is shown: a refusal is a footer notice and the
    picker never appears, so a row that cannot be acted on does not offer a
    menu of actions. Its PreCheck text rides in the picker line.

    `prompt` is rendered verbatim — the board writes the key legend
    (`start <branch>: [c] claude  [a] astra  [esc]`) — and, unlike a
    Confirm's, never gets ` — y/N` appended.

    608v: an option may also be `Action("confirm", Confirm)` — a CHAINED
    Confirm, for a mutation whose prompt must be READ before it runs (a
    pre-check summary, a disclosure). The key closes the picker and opens that
    Confirm as a board key would: nothing runs until `y`. Chaining is not
    stacking; a `menu` Action option is still refused.

    ESC, any unlisted key and a mouse click cancel; `timeout` expires the
    picker exactly as CONFIRM_PROMPT_S expires a y/N prompt."""
    label: str
    prompt: str
    options: dict          # keycode -> Confirm | Action
    precheck: Callable[[], PreCheck] | None = None
    timeout: float = CONFIRM_PROMPT_S


# 715z: `cursor` is deliberately NOT here. The loop re-anchors the cursor from
# the numeric index AFTER the action dispatch, so a cursor action taken from a
# menu would be silently overwritten (fresheyes) — and nothing needs it. An
# allowlist that promises a kind it cannot honour is worse than a shorter one.
MENU_ACTION_KINDS = ("expand", "copy", "jump", "notice", "noop", "open")


def menu_problem(m) -> str | None:
    """None when `m` may be opened, else the reason (fail closed, before
    anything is shown): a Menu with at least one option, every option either a
    Confirm whose argv already passes argv_ok or an Action of a kind the loop
    dispatches. A `menu` Action inside a menu is refused — a picker never
    stacks another pending shape on itself. An `Action("confirm", Confirm)` is
    admitted (608v): that is a CHAIN — the picker closes first, then the
    Confirm opens with its own pre-check and y/N — and its argv is vetted here
    like any other option's."""
    if not isinstance(m, Menu):
        return "bad payload"
    if not isinstance(m.options, dict) or not m.options:
        return "no options"
    for key, opt in m.options.items():
        if not isinstance(key, int):
            return f"option key {key!r} is not a keycode"
        name = chr(key) if 32 <= key < 127 else str(key)
        if isinstance(opt, Confirm):
            err = argv_ok(opt.argv)
            if err:
                return err
            continue
        if isinstance(opt, Action):
            if opt.kind in MENU_ACTION_KINDS:
                continue
            if opt.kind == "confirm":
                # 608v: a CHAIN, not a stack. On this key the picker closes
                # and the Confirm opens exactly as a board key would open it
                # (pre-check, prompt, y/N) — one pending shape at a time. It
                # passes the same argv chokepoint at open as a key-is-y option.
                if not isinstance(opt.value, Confirm):
                    return f"option {name} is a confirm Action without a Confirm"
                err = argv_ok(opt.value.argv)
                if err:
                    return err
                continue
            return f"option {name} is an Action of kind {opt.kind!r}"
        return f"option {name} is not a Confirm or an Action"
    return None


def argv_ok(argv) -> str | None:
    """The loop's argv chokepoint: None when `argv` may run, else the
    reason. A non-empty list of non-empty str without control characters,
    whose first element is an absolute path to an executable file — a bare
    program name never reaches the runner (PATH is not a trust boundary)."""
    if not isinstance(argv, list) or not argv:
        return "argv is not a non-empty list"
    for i, a in enumerate(argv):
        if not isinstance(a, str):
            return f"argv[{i}] is not a str"
        if a == "":
            return f"argv[{i}] is empty"
        if _CONTROL.search(a):
            return f"argv[{i}] has a control character"
    prog = argv[0]
    if not os.path.isabs(prog):
        return "program is not an absolute path"
    if not (os.path.isfile(prog) and os.access(prog, os.X_OK)):
        return f"program is not executable: {prog}"
    return None


def _last_line(text) -> str:
    for line in reversed((text or "").splitlines()):
        if line.strip():
            return clip(clean(line.strip()), _RESULT_TAIL)
    return ""


_RC_OK_RE = re.compile(r"rc=0(?: · .*)?", re.DOTALL)


def run_succeeded(text) -> bool:
    """Did a confirm runner's result say the command SUCCEEDED?

    715z round 3: `Confirm.done_copy` hands the operator a follow-up command,
    and a follow-up to a `kata label rm` that did not happen is a lie — it
    tells a worker to continue while the label parking it is still on. The
    runner's contract is a footer STRING, so success is the EXACT form
    `run_confirm_argv` prints on rc 0 — `rc=0`, or `rc=0 · <tail>` — matched
    whole. Everything else fails closed: `rc=1 · …`, a `timed out after …`, a
    `failed: …` launch error, any custom runner's own wording, and (round 4) a
    prefix collision like `rc=00` or `rc=0-but-failed` that a startswith test
    would have waved through. Round 3 caught the first version of this, which
    keyed off `failed:` alone and so copied the nudge after a nonzero exit and
    after a timeout."""
    return _RC_OK_RE.fullmatch(str(text)) is not None


def run_confirm_argv(argv, env=None, timeout=CONFIRM_TIMEOUT_S, runner=None) -> str:
    """The default confirm runner: `workers.Runner` (own session per child,
    SIGTERM then SIGKILL to the whole process group at the deadline, pipes
    closed — a lane's descendants cannot outlive the timeout or hold the
    capture open), stdin closed, never raises. Footer notice: `rc=<n> ·
    <last non-empty stdout line>` on rc 0 (worktree_sweep prints `removed
    …` on stdout, its summary on stderr), else the last stderr line (its
    `FAILED …` / `refused …`, kata-dispatch's `ERR: …`), falling back to
    stdout."""
    runner = runner or Runner()
    try:
        rc, out, err = runner.run(list(argv), timeout, env=env)
    except SourceTimeout:
        return f"timed out after {int(timeout)}s"
    except (OSError, subprocess.SubprocessError) as exc:
        return f"failed: {exc}"
    tail = _last_line(out) if rc == 0 else (_last_line(err) or _last_line(out))
    return f"rc={rc}" + (f" · {tail}" if tail else "")


class Board(Protocol):
    """What `tui_loop` needs from a board. `expansion_cache` is optional:
    a board whose `expansion_for` shares a cache with the loop exposes the
    dict here so both sides key into the same store; without it the loop
    keeps its own.

    There is no `cursor_item`: the loop dispatches on the item list this
    frame's `layout` returned, so a key can never act on a row the board
    is no longer painting.

    `hit_tags` (optional, default False): the board tags its item rows
    with layout.hit_tag(), so mouse clicks resolve positionally through
    the frame's hit map. Without it the loop keeps the legacy mouse
    meanings (modifier click = space on the cursor row, plain click
    ignored) — a vendored consumer that predates tags is unaffected.

    `with_zoom(state, zoom, width, height) -> (state, zoom)` (optional,
    4vwr): the panel zoom. The loop calls it immediately BEFORE `layout`,
    because `layout` is where `items()` is built and a zoomed `items()` is
    the zoomed panel's rows only. It returns the zoom it ACCEPTED — only
    the board knows what a panel is, which tier paints boxes and whether a
    panel still has rows — and the loop writes that back, so a refusal
    removes the state instead of leaving an invisible armed mode. Without
    it the loop clears the zoom every frame, so `z` arms nothing and no
    other behaviour changes.

    `on_action`: y9nb — a cursor_first / cursor_last / page_down / page_up
    action answered with Action("noop") gets the engine's own move
    (cursor.default_move); any other answer is the board's.
    """
    help_lines: list[str]
    extra_keys: dict[int, str]                   # key code -> action name

    def tick(self, now_mono: float, now_wall: float, action) -> object: ...
    def layout(self, state, width: int, height: int) -> tuple[object, list]: ...
    def compose_rows(self, state, width: int, height: int) -> list: ...
    def with_cursor(self, state, ui: UIState, expanded: dict, notice: str) -> object: ...
    def expansion_for(self, item, state) -> tuple[list | None, object | None]: ...
    def on_action(self, name: str, item, state, ui: UIState) -> Action: ...
    def child_env(self) -> dict: ...
    def ascii_only(self, state) -> bool: ...


CORE_ACTIONS = ("quit", "close", "toggle", "help", "refresh", "resize",
                "board_prev", "board_next")      # y9nb: the last two are the engine's


_YN = " — y/N"


def confirm_footer(pending, width: int | None = None, now: float | None = None) -> str:
    """The sticky footer for a pending Confirm or Menu, by stage. With
    `width` the prompt stage keeps ` — y/N` visible whatever the prompt and
    pre-check text add up to: the painter clips the last row from the
    RIGHT, so an unbounded middle would push the one thing the human must
    see off the screen (a live run of marshal-board's resolve prompt lost it
    on a 180-col pane); the middle is clipped to make room instead.

    A Menu's own stages (`checking`, `menu`) take their text from the Menu —
    the picker line, which carries its own key legend and never gets ` —
    y/N` — and the `running` stage from the chosen option, so the footer
    says which one is running (ssf2)."""
    stage = pending["stage"]
    c = pending.get("c")
    holder = c if c is not None else pending.get("menu")
    prompt, label = clean(getattr(holder, "prompt", "")), clean(getattr(holder, "label", "action"))
    if stage == "checking":
        return f"{prompt} · checking…"
    if stage == "menu":
        # the KEY LEGEND is this stage's `y/N`: it names what each key does, so
        # it must survive clipping whatever the branch name costs (fresheyes:
        # a long branch at 80 columns hid `[a] astra` while `a` still launched
        # an agent). The legend is the tail from its first `[`; the head is
        # what gets clipped.
        text = pending.get("text") or ""
        head, mark, legend = prompt.partition("[")
        tail = (mark + legend) if mark else ""
        middle = head + (f"{text} — " if text and tail else (f" — {text}" if text else ""))
        if width is not None:
            room = max(8, width - 1 - cell_width(tail))
            middle = clip(middle, room)
        return middle + tail
    if stage == "prompt":
        text = pending.get("text") or ""
        middle = f"{prompt} — {text}" if text else prompt
        if width is not None:
            room = max(8, width - 1 - cell_width(_YN))   # the painter never draws the last cell
            middle = clip(middle, room)
        return middle + _YN
    limit = int(getattr(c, "timeout", CONFIRM_TIMEOUT_S))
    elapsed = f" {max(0, int(now - pending['since']))}s / {limit}s limit" if now is not None else ""
    return f"{label}: running…{elapsed}"


def prompt_limit(pending) -> float:
    """How long this pending prompt may sit unanswered: a Menu carries its
    own `timeout`, a Confirm takes CONFIRM_PROMPT_S (ssf2)."""
    m = pending.get("menu")
    return float(m.timeout) if m is not None else CONFIRM_PROMPT_S


# --- the loop ----------------------------------------------------------------

def tui_loop(stdscr, curses_mod, board, *, copy=None, opener=None,
             spawn=None, jump=None, confirm_run=None, switch=None,
             board_count=None, clock=time) -> None:
    """The board loop. One frame: tick -> drain expansion results -> drain
    copy notices -> drain confirm results -> measure -> layout -> resolve
    the cursor -> hand the board its cursor/expansion/notice -> paint ->
    overlay -> read one key -> dispatch. Blocking work (copy, expansion
    fetch, a confirm's pre-check and run) runs on short-lived daemon
    threads and lands back through a queue, so redraws never stall.

    A Confirm (Action "confirm") holds one `pending` slot through three
    stages — checking (pre-check on a thread), prompt (the footer asks
    y/N; exactly `y` runs, any other key cancels, 30 s and it expires),
    running (keys swallowed until the runner returns). Its argv is
    re-vetted with argv_ok() at the keypress and at the `y`, and the
    runner is the ONLY thing that mutates anything; it is reached from
    exactly one place, the `y` branch. The loop owns the runner's
    workers.Runner and reaps it on exit, so a run cannot outlive the board.
    """
    # y9nb: refused before curses takes the screen — a board whose extra_keys
    # rebinds a standard key is a bug in the board, not a runtime surprise.
    err = keymap_problem(getattr(board, "extra_keys", None))
    if err:
        raise ValueError(f"board keymap refused: {err}")
    # y9nb: one env for both defaults — an EMPTY child_env() means "inherit",
    # so it must fall back to os.environ, not be handed on as an environment
    # with no $TMUX (which reads as "outside tmux": no switch, no count).
    default_env = board.child_env() or os.environ
    if switch is None:
        switch = lambda forward, env=None: kit_windows.switch_board(
            forward, environ=env or default_env)
    if board_count is None:
        board_count = kit_windows.BoardCount(environ=default_env)
    copy = copy or clipboard_copy
    spawn = spawn or open_in_tmux
    jump = jump or jump_to_tmux
    if opener is None:
        opener = open_url
    conf_runner = Runner()             # one per loop; reaped in the finally below
    if confirm_run is None:
        def _default_run(argv, env=None, timeout=CONFIRM_TIMEOUT_S):
            return run_confirm_argv(argv, env=env, timeout=timeout, runner=conf_runner)
        confirm_run = _default_run
    try:
        curses_mod.curs_set(0)
    except curses_mod.error:
        pass
    stdscr.timeout(250)
    attrs = init_styles(curses_mod)
    if getattr(curses_mod, "mousemask", None):
        try:                                 # wheel scroll = cursor up/down
            curses_mod.mousemask(getattr(curses_mod, "ALL_MOUSE_EVENTS",
                                         getattr(curses_mod, "BUTTON4_PRESSED", 0)
                                         | (1 << 21)))
        except curses_mod.error:
            pass
    overlay = new_overlay()                  # help only
    exp = new_overlay()                      # inline expansion (key + lines)
    exp_cache = getattr(board, "expansion_cache", None)
    if exp_cache is None:
        exp_cache = {}
    detail_q: queue.Queue = queue.Queue()
    copy_q: queue.Queue = queue.Queue()
    ui = UIState()
    copy_seq = [0]                     # newest copy request owns the clipboard
    copy_lock = threading.Lock()       # staleness check + write are atomic
    keymap = {curses_mod.KEY_DOWN: KEY_DOWN, curses_mod.KEY_UP: KEY_UP,
              curses_mod.KEY_ENTER: KEY_ENTER, curses_mod.KEY_RESIZE: KEY_RESIZE,
              # getattr: a curses double that predates the side arrows (test
              # fakes here and in vendoring consumers) still drives the loop
              getattr(curses_mod, "KEY_LEFT", KEY_LEFT): KEY_LEFT,
              getattr(curses_mod, "KEY_RIGHT", KEY_RIGHT): KEY_RIGHT,
              getattr(curses_mod, "KEY_HOME", KEY_HOME): KEY_HOME,
              getattr(curses_mod, "KEY_END", KEY_END): KEY_END,
              getattr(curses_mod, "KEY_NPAGE", KEY_NPAGE): KEY_NPAGE,
              getattr(curses_mod, "KEY_PPAGE", KEY_PPAGE): KEY_PPAGE,
              getattr(curses_mod, "KEY_BTAB", KEY_BTAB): KEY_BTAB}
    action = None
    notice, notice_until = "", 0.0
    confirm_q: queue.Queue = queue.Queue()   # (token, kind, payload) from pre-check / run threads
    confirm_seq = [0]
    pending: dict | None = None   # {"c": Confirm|None, "menu": Menu|None,
                                  #  "stage": checking|prompt|menu|running, "token", "text", "since"}
    refresh_due = False # a finished run asks the next tick for a refresh

    def start_copy(value, sticky=False, interim="copying…"):
        # 715z: `sticky` keeps the finished note up like a run result instead
        # of expiring it after NOTICE_S, and `interim` lets a caller that
        # already has something on the footer — a mutation's own rc — keep it
        # there while the clipboard write happens, rather than flashing a bare
        # "copying…" over the result.
        nonlocal notice, notice_until
        copy_seq[0] += 1
        notice = interim
        notice_until = float("inf") if sticky else clock.monotonic() + NOTICE_S

        def do_copy(v=value, seq=copy_seq[0], sticky=sticky):    # seq rides the queue too
            # A slow fetch (kata show) plus pbcopy must not freeze
            # redraws; the notice lands back through copy_q. A copy
            # superseded by a newer request while it was fetching is
            # dropped whole: the staleness check and the clipboard
            # write happen under copy_lock, so a stale request can
            # never overwrite a newer clipboard or footer notice
            # (concurrent still-live requests serialize; the newer
            # one either writes last or marks the older one stale).
            text = v() if callable(v) else v
            with copy_lock:
                if seq != copy_seq[0]:
                    return
                note = None
                if isinstance(text, tuple):
                    text, note = text
                if text is None:
                    copy_q.put((note or "copy: nothing to copy", sticky, seq))
                    return
                # split("\n"), never splitlines(): the clipboard
                # payload must survive byte-for-byte for LF text — a
                # trailing newline and interior blank lines included —
                # while every in-line control char is still scrubbed.
                text = "\n".join(clean(seg)
                                 for seg in str(text).split("\n"))
                err = copy(text, env=board.child_env())
                if err:
                    note = f"copy failed: {err}"
                copy_q.put((note or f"copied {len(text)} chars", sticky, seq))

        threading.Thread(target=do_copy, name="copy", daemon=True).start()

    # ssf2: the pre-check and the run are started from TWO places now (a
    # Confirm's `y` and a Menu's chosen key), so both live here rather than
    # inline in the `y` branch. The runner is still reached from exactly one
    # function, and only after a key that names the action.
    def start_precheck(fn, tok):
        def do_check():
            try:
                res = fn()
            except Exception as exc:
                res = PreCheck(f"pre-check failed: {exc}", ok=False)
            confirm_q.put((tok, "check", res))
        threading.Thread(target=do_check, name="confirm-check", daemon=True).start()

    def start_run(cf, tok):
        def do_run(cf=cf, argv=list(cf.argv), tok=tok):
            try:
                if cf.recheck and cf.precheck is not None:
                    again = cf.precheck()
                    if not (isinstance(again, PreCheck) and again.ok):
                        why = (again.text if isinstance(again, PreCheck)
                               else "pre-check returned no result")
                        confirm_q.put((tok, "run", (False, clean(why))))
                        return
                res = confirm_run(argv, env=board.child_env(), timeout=cf.timeout)
            except Exception as exc:            # a runner/recheck that raises still lands
                confirm_q.put((tok, "run", (False, f"failed: {exc}")))
                return
            confirm_q.put((tok, "run", (True, str(res))))
        threading.Thread(target=do_run, name="confirm-run", daemon=True).start()

    # ssf2/715z: ONE place that turns an Action into an effect. The key
    # intercept for a Menu whose option is a plain Action (view, copy,
    # jump) hands it to THIS, so the open-scheme rule, the copy thread
    # and the expansion bookkeeping are not written twice.
    def dispatch_action(act, its, state):
        nonlocal notice, notice_until, pending, ui, refresh_due
        if act is None:
            pass
        elif act.kind == "copy":
            start_copy(act.value)
        elif act.kind == "spawn":
            notice = spawn(list(act.value), env=board.child_env())
            notice_until = clock.monotonic() + NOTICE_S
        elif act.kind == "jump":
            notice = jump(str(act.value), env=board.child_env())
            notice_until = clock.monotonic() + NOTICE_S
        elif act.kind == "open":
            url = clean(str(act.value))
            if act.trusted or _OPENABLE.match(url):
                notice = opener(url, env=board.child_env())
            else:
                notice = "not an openable value"
            notice_until = clock.monotonic() + NOTICE_S
        elif act.kind == "confirm":
            c = act.value
            if pending is not None:        # unreachable from a key (see the intercept); defensive
                busy = pending.get("c") or pending.get("menu")
                notice = f"busy: {getattr(busy, 'label', 'action')} {pending['stage']}"
            elif not isinstance(c, Confirm):
                notice = "confirm: bad payload"
            elif (err := argv_ok(c.argv)):
                notice = f"{c.label}: refused: {err}"
            else:
                confirm_seq[0] += 1
                pending = {"c": c, "menu": None,
                           "stage": "checking" if c.precheck else "prompt",
                           "token": confirm_seq[0], "text": "", "since": clock.monotonic()}
                if c.precheck:
                    start_precheck(c.precheck, confirm_seq[0])
            notice_until = clock.monotonic() + NOTICE_S
        elif act.kind == "menu":
            # ssf2: the picker. Every option is vetted BEFORE anything
            # is shown (menu_problem), and the Menu's own pre-check runs
            # first: a row that cannot be acted on gets a notice, never
            # a menu of actions it would refuse.
            m = act.value if isinstance(act.value, Menu) else None
            if pending is not None:        # defensive, as for a Confirm
                busy = pending.get("c") or pending.get("menu")
                notice = f"busy: {getattr(busy, 'label', 'action')} {pending['stage']}"
            elif m is None:
                notice = "menu: bad payload"
            elif (err := menu_problem(m)):
                notice = f"{m.label}: refused: {err}"
            else:
                confirm_seq[0] += 1
                pending = {"c": None, "menu": m,
                           "stage": "checking" if m.precheck else "menu",
                           "token": confirm_seq[0], "text": "", "since": clock.monotonic()}
                if m.precheck:
                    start_precheck(m.precheck, confirm_seq[0])
            notice_until = clock.monotonic() + NOTICE_S
        elif act.kind == "notice":
            notice, notice_until = str(act.value), clock.monotonic() + NOTICE_S
        elif act.kind == "cursor":
            ui = replace(ui, cursor_key=str(act.value))
        elif act.kind == "expand":
            # a board-chosen expansion (marshal-board's s on a WAITING /
            # PINNED / fold row, 4324): the toggle branch's open half.
            # 715z: an ALREADY-OPEN key is re-opened rather than ignored — a
            # board can have more than one view of a row (the WAITING menu's
            # `[v]` shows the parking comment where Enter shows the ordinary
            # expansion), and ignoring the repeat left the first view on
            # screen with expansion_for never called again. close_overlay
            # drops the in-flight token, which apply_detail_result already
            # ignores, so a slow first fetch cannot overwrite the second view.
            key = str(act.value)
            target_item = next((it for it in its if it.key == key), None)
            if target_item is None:
                notice, notice_until = "expand: row not on screen", clock.monotonic() + NOTICE_S
            else:
                close_overlay(exp)
                lines_now, expander = board.expansion_for(target_item, state)
                expand_mod.open_expansion(key, lines_now, expander,
                                          exp, detail_q, exp_cache)
        elif act.kind == "noop":
            pass
        else:
            notice, notice_until = (f"unknown action kind: {act.kind}",
                                    clock.monotonic() + NOTICE_S)


    try:
        board_count.start()         # y9nb: the first statement inside the try,
                                     # so a raise from Runner() / curs_set /
                                     # init_styles above never starts the count
                                     # thread at all, and one raised once the
                                     # loop is running still reaches this
                                     # try's own finally's stop()
        while True:
            if refresh_due and action is None:
                action, refresh_due = "refresh", False
            state = board.tick(clock.monotonic(), clock.time(), action)
            ui = replace(ui, boards=board_count.value)      # y9nb: `board: …` windows
            while True:                            # drain expansion results
                try:
                    token, lines = detail_q.get_nowait()
                except queue.Empty:
                    break
                apply_detail_result(exp, token, lines)
            while True:                            # drain copy notices
                try:
                    note, sticky, seq = copy_q.get_nowait()
                except queue.Empty:
                    break
                # round 3: the producer's staleness check cannot reach an item
                # already queued. A copy that finished while getch was blocked,
                # and was superseded before this drain, must not install its
                # notice over the newer request's — least of all a STICKY one,
                # which would then sit there until the newer copy finished.
                if seq != copy_seq[0]:
                    continue
                notice = note
                notice_until = float("inf") if sticky else clock.monotonic() + NOTICE_S
            while True:                            # drain confirm pre-check / run results
                try:
                    token, kind, payload = confirm_q.get_nowait()
                except queue.Empty:
                    break
                if pending is None or token != pending["token"]:
                    continue                       # cancelled, expired or superseded: dropped whole
                # ssf2: a Menu has no chosen option until its key is pressed,
                # so the label for a refusal comes from whichever is pending
                c = pending.get("c") or pending.get("menu")
                c_label = getattr(c, "label", "action")
                if kind == "check":
                    if isinstance(payload, PreCheck) and payload.ok:
                        # a cleared MENU pre-check opens the picker, not a y/N
                        pending["stage"] = "menu" if pending.get("menu") is not None else "prompt"
                        pending["text"] = clean(payload.text)
                        pending["since"] = clock.monotonic()
                        continue
                    text = clean(payload.text) if isinstance(payload, PreCheck) else "pre-check returned no result"
                    pending = None             # a refusal is a notice: nothing before y writes, the clipboard included
                    notice, notice_until = f"{c_label}: {text}", float("inf")
                else:                              # "run": the runner's (or the recheck's) verdict
                    done = getattr(pending.get("c"), "done_copy", None)
                    pending = None
                    ran, text = payload
                    refresh_due = True         # a run that failed may still have mutated the world: re-read
                    # 715z: the follow-up command goes to the CLIPBOARD, and
                    # the footer keeps the run's own result beside a short note
                    # that it is there. Only when the command actually ran and
                    # the runner reached it: a refused re-check has nothing to
                    # follow up, and neither does a launch that failed.
                    if ran and done and run_succeeded(text):
                        # the note goes BEFORE the run's output tail: the
                        # footer is one row and clips from the right, and a
                        # long tail would otherwise push the only word that
                        # the clipboard changed off the screen (round 3)
                        start_copy((str(done), f"{c_label}: follow-up copied · {text}"),
                                   sticky=True,
                                   interim=f"{c_label}: copying follow-up… · {text}")
                        continue
                    notice, notice_until = f"{c_label}: {text}", float("inf")
            if (pending is not None and pending["stage"] in ("prompt", "menu")
                    and clock.monotonic() - pending["since"] > prompt_limit(pending)):
                stale = getattr(pending.get("c") or pending.get("menu"), "label", "action")
                notice, notice_until = f"{stale}: prompt expired", clock.monotonic() + NOTICE_S
                pending = None
            height, width = stdscr.getmaxyx()
            # 4vwr: the zoom reaches the board BEFORE layout(), because
            # layout() is where items() is built and a zoomed items() is the
            # zoomed panel's rows only — recording it afterwards (fold_open's
            # pattern) would paint a zoomed frame one tick before items()
            # knew, which is the cursor-on-an-unpainted-row defect gqfj spent
            # four rounds on. The hook returns the zoom it ACCEPTED: only the
            # board knows what a panel is, which tier paints boxes and whether
            # a panel still has rows, so a zoom it cannot honour comes back
            # None and is dropped here rather than left armed and invisible.
            zoom_in = getattr(board, "with_zoom", None)          # optional
            if zoom_in is None:
                if ui.zoom is not None:
                    ui = replace(ui, zoom=None)     # a board that cannot zoom never holds one
            else:
                state, zoom = zoom_in(state, ui.zoom, width, height)
                if zoom != ui.zoom:
                    ui = replace(ui, zoom=zoom)
            state, its = board.layout(state, width, height)
            # An expansion opened in a panel the zoom then hides is left OPEN
            # and unpainted, and Esc spends its first press closing it. That
            # is deliberate. Closing it automatically was tried and reverted:
            # the board's own fold (marshalboard's `N quiet · M pinned`) is an
            # expansion whose rows are items ONLY while it is open, so closing
            # it removes those rows from items() — and then the prune below
            # destroys any mark on them, breaking the very promise this zoom
            # makes about hidden marks. One extra Esc press is the cheaper
            # bargain, and it costs no state at all (4vwr, rule-4 revert).
            #
            # while zoomed `its` is one panel, so a mark outside it is HIDDEN,
            # not gone: pruning against this list would destroy it (4vwr)
            ui = resolve_cursor(ui, its, prune_marks=ui.zoom is None)
            expanded = {exp["key"]: exp["lines"]} if exp["key"] else {}
            state = board.with_cursor(
                state, ui, expanded,
                confirm_footer(pending, width, clock.monotonic()) if pending is not None
                else (notice if clock.monotonic() <= notice_until else ""))
            rows = board.compose_rows(state, width, height)
            paint(stdscr, curses_mod, rows, attrs, width, height)
            if overlay["lines"] is not None:
                paint_overlay(stdscr, curses_mod, overlay["lines"], width,
                              height, attrs, board.ascii_only(state))
            stdscr.refresh()
            ch = stdscr.getch()
            if pending is not None and ch not in (-1, curses_mod.KEY_RESIZE):
                # A pending Confirm owns the keyboard: nothing here reaches
                # handle_key or the board, so a second confirm cannot stack
                # and `j` cannot move the cursor under a prompt.
                if ch == getattr(curses_mod, "KEY_MOUSE", None):
                    mouse_event(curses_mod)        # drain the event; a click answers "no"
                stage = pending["stage"]
                if stage == "menu":
                    # ssf2: the picker. A listed key is the confirmation — it
                    # goes through the SAME argv_ok + do_run path a `y` does;
                    # ESC, an unlisted key and a click (drained above) cancel.
                    m = pending["menu"]
                    opt = m.options.get(ch)
                    if opt is not None and clock.monotonic() - pending["since"] > prompt_limit(pending):
                        pending = None         # the getch window can straddle the deadline
                        notice, notice_until = f"{m.label}: prompt expired", clock.monotonic() + NOTICE_S
                    elif isinstance(opt, Action):
                        # a VIEW option (715z): the picker closes and the loop
                        # dispatches it the way it would a board key — nothing
                        # is run on the confirm runner, because nothing mutates
                        pending = None
                        dispatch_action(opt, its, state)
                    elif opt is not None:
                        err = argv_ok(opt.argv)
                        if err:
                            pending = None
                            notice, notice_until = f"{m.label}: refused: {err}", clock.monotonic() + NOTICE_S
                        else:
                            pending["c"] = opt
                            pending["stage"] = "running"
                            pending["since"] = clock.monotonic()
                            start_run(opt, pending["token"])
                    else:
                        pending = None
                        notice, notice_until = f"{m.label}: cancelled", clock.monotonic() + NOTICE_S
                elif stage == "checking":
                    # a MENU pre-checks too, and then `pending["c"]` is still
                    # None — reading a Confirm's label here crashed the whole
                    # loop on ESC (fresheyes on the branch; the C1 test pressed
                    # ESC only on a menu WITHOUT a pre-check, so it missed the
                    # one path the board actually uses)
                    if ch == 27:
                        label = getattr(pending.get("c") or pending.get("menu"), "label", "action")
                        pending = None
                        notice, notice_until = f"{label}: cancelled", clock.monotonic() + NOTICE_S
                    # any other key while checking: swallowed
                elif stage == "prompt":
                    c = pending["c"]                    # this stage is a Confirm's
                    if ch == ord("y") and clock.monotonic() - pending["since"] > CONFIRM_PROMPT_S:
                        pending = None             # the getch window can straddle the deadline: re-check here
                        notice, notice_until = f"{c.label}: prompt expired", clock.monotonic() + NOTICE_S
                    elif ch == ord("y"):
                        err = argv_ok(c.argv)
                        if err:
                            pending = None
                            notice, notice_until = f"{c.label}: refused: {err}", clock.monotonic() + NOTICE_S
                        else:
                            pending["stage"] = "running"
                            pending["since"] = clock.monotonic()
                            start_run(c, pending["token"])
                    else:
                        pending = None
                        notice, notice_until = f"{c.label}: cancelled", clock.monotonic() + NOTICE_S
                # running: keys swallowed
                ch = -1
            if ch == getattr(curses_mod, "KEY_MOUSE", None):
                ch, pos = mouse_event(curses_mod)
                if pos is not None:
                    # 8dpg: a click lands on the row painted under it — resolved
                    # through THIS frame's rows, so it can never pick a row the
                    # board is not showing. Plain click: the cursor row toggles
                    # like Enter, another row takes the cursor. Modifier click:
                    # the cursor moves to the clicked row, then the space key
                    # goes through handle_key — so a board that maps space
                    # keeps its own meaning (extra_keys precedence) and gets
                    # the clicked row as its item, and the generic mark toggles
                    # that row. The help overlay, headers and empty rows
                    # swallow both. A board that does not declare hit_tags
                    # (a vendored consumer that predates them) keeps the
                    # legacy meanings: modifier click = space on the cursor
                    # row, plain click ignored (fresheyes rounds 4/5 — the
                    # declaration, never an empty frame, decides: a tagged
                    # board's frame with nothing selectable swallows clicks).
                    # One deliberate delta from the pre-tag code: ncurses
                    # merges fast repeats into DOUBLE/TRIPLE_CLICKED, which
                    # mouse_event now counts as clicks, so a fast modifier
                    # double-click acts once instead of being dropped.
                    kind, ch = ch, -1
                    if not getattr(board, "hit_tags", False):
                        ch = -1 if kind == KEY_CLICK else kind
                    elif pos == (height - 1, width - 1):
                        pass          # the painter never draws this cell (addstr
                                      # would raise there); a click on it is void
                    else:
                        hits = hit_map(rows)
                        key = hit_at(hits, *pos) if overlay["lines"] is None else None
                        idx = next((i for i, it in enumerate(its) if it.key == key),
                                   None) if key is not None else None
                        if idx is not None and kind == KEY_CLICK:
                            if idx == ui.cursor:
                                ch = KEY_ENTER
                            else:
                                ui = replace(ui, cursor=idx, cursor_key=key)
                        elif idx is not None:
                            ui, ch = replace(ui, cursor=idx, cursor_key=key), kind
            ui, action = handle_key(ui, keymap.get(ch, ch), board.extra_keys)
            ui = replace(ui, cursor_key=its[ui.cursor].key if its else None)  # re-anchor after a move
            if action == "quit":
                return
            if action == "close":
                # the ladder, exactly: the notice is dropped UNCONDITIONALLY,
                # then one of help / expansion / marks / zoom, one rung a
                # press. (A pending prompt intercepts Esc before any of this.)
                # So Esc with a notice up and nothing else open drops the
                # notice AND unzooms — the same double effect it has always
                # had on the marks, not a new quirk of the zoom (4vwr).
                notice, notice_until = "", 0.0
                if overlay["lines"] is not None:
                    close_overlay(overlay)
                elif exp["key"] is not None:
                    close_overlay(exp)
                elif ui.marked:
                    ui = replace(ui, marked=frozenset())
                elif ui.zoom is not None:
                    ui = replace(ui, zoom=None)
            elif action == "toggle":
                item = its[ui.cursor] if its and 0 <= ui.cursor < len(its) else None
                if item is not None:
                    if toggle_expanded(exp, item.key) is None:
                        close_overlay(exp)
                    else:
                        close_overlay(exp)
                        lines_now, expander = board.expansion_for(item, state)
                        expand_mod.open_expansion(item.key, lines_now, expander,
                                                  exp, detail_q, exp_cache)
            elif action == "help":
                open_overlay(overlay, board.help_lines)
            elif action in ("board_prev", "board_next"):
                notice = switch(action == "board_next", env=board.child_env())
                notice_until = clock.monotonic() + NOTICE_S
            elif action not in CORE_ACTIONS and action is not None:
                item = its[ui.cursor] if its and 0 <= ui.cursor < len(its) else None
                act = board.on_action(action, item, state, ui)
                if act is not None and act.kind == "noop":
                    moved = default_move(ui, action)          # y9nb: first / last / page
                    if moved is not ui:
                        ui = replace(moved, cursor_key=its[moved.cursor].key if its else None)
                        act = None
                dispatch_action(act, its, state)
            # "refresh" is applied by the next tick(); "resize"/None: next tick
            # re-getmaxyx + recompose
    finally:
        board_count.stop()         # y9nb: the count thread never outlives the board
        conf_runner.close()        # a run never outlives the board (q, Ctrl-C, a crash); late starters are refused
