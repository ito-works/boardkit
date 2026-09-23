"""boardkit.windows — the `board: …` tmux windows on THIS board's server (y9nb).

`[` / `]` in any board select the previous / next window whose name starts
with BOARD_PREFIX, on the server the board itself runs on — the socket path
in $TMUX, passed as `tmux -S <path>` — never an assumed `-L kwt`. Outside
tmux there is nothing to switch and every call says so. BoardCount polls
the same list on a daemon thread so the loop can hand the board the count
without a subprocess on the paint path.

`list_boards` separates "tmux did not answer" (None) from "no board
windows" ([]): a failed poll must not turn a real count into 0, and a
failed switch must say what actually happened.
"""
from __future__ import annotations

import os
import subprocess
import threading
import time

BOARD_PREFIX = "board: "
_TIMEOUT = 5


def server_args(environ=os.environ) -> list[str]:
    """`["-S", <socket path>]` from $TMUX (`path,pid,index`), or [] outside tmux."""
    raw = (environ.get("TMUX") or "").split(",")[0]
    return ["-S", raw] if raw else []


def _tmux(args: list[str], run, environ) -> str | None:
    """stdout of `tmux <server> <args>`, or None when tmux would not answer."""
    argv = ["tmux"] + server_args(environ) + list(args)
    try:
        p = run(argv, env=dict(environ), timeout=_TIMEOUT,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    if p.returncode != 0 or p.stdout is None:
        return None
    out = p.stdout
    return out.decode("utf-8", "replace") if isinstance(out, bytes) else str(out)


def list_boards(run=subprocess.run, environ=os.environ) -> list[tuple[str, str]] | None:
    """(window_id, window_name) for every window on this server whose name
    starts with BOARD_PREFIX, in tmux's own order — deduped by window id,
    first seen wins, since a window linked into two sessions is listed once
    per session. [] when tmux answered and there are none, and [] outside
    tmux (no $TMUX, so there is no server to ask). None when the server
    $TMUX names did not answer: it is gone, errored, or timed out. A
    malformed row is dropped."""
    if not server_args(environ):
        return []
    out = _tmux(["list-windows", "-a", "-F", "#{window_id}\t#{window_name}"], run, environ)
    if out is None:
        return None
    boards, seen = [], set()
    for line in out.splitlines():
        wid, sep, name = line.partition("\t")
        if sep and wid.startswith("@") and name.startswith(BOARD_PREFIX) and wid not in seen:
            seen.add(wid)
            boards.append((wid, name))
    return boards


def current_window(run=subprocess.run, environ=os.environ) -> str | None:
    """The window id of $TMUX_PANE, or None."""
    pane = environ.get("TMUX_PANE") or ""
    if not server_args(environ) or not pane.startswith("%"):
        return None
    out = _tmux(["display-message", "-p", "-t", pane, "#{window_id}"], run, environ)
    wid = (out or "").strip()
    return wid if wid.startswith("@") else None


def switch_board(forward: bool, *, run=subprocess.run, environ=os.environ) -> str:
    """Select the next (forward) / previous board window; returns the footer
    notice. From a window that is not itself a board window, `]` goes to
    the first board and `[` to the last."""
    if not server_args(environ):
        return "not inside tmux: no board windows to switch"
    boards = list_boards(run=run, environ=environ)
    if boards is None:
        return "tmux did not answer"
    if not boards:
        return "no board windows on this server"
    if len(boards) == 1:
        return "no other board window (1 board)"
    cur = current_window(run=run, environ=environ)
    ids = [wid for wid, _ in boards]
    if cur in ids:
        i = (ids.index(cur) + (1 if forward else -1)) % len(ids)
    else:
        i = 0 if forward else len(ids) - 1
    wid, name = boards[i]
    argv = ["tmux"] + server_args(environ) + ["switch-client", "-t", wid]
    try:
        p = run(argv, env=dict(environ), timeout=_TIMEOUT,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"tmux switch-client failed: {exc}"
    if p.returncode != 0:
        return f"tmux switch-client failed: rc={p.returncode}"
    return f"{name} ({i + 1}/{len(boards)})"


class BoardCount:
    """How many board windows this server has, polled every `interval_s` on
    a daemon thread. `.value` is None until the first poll lands and stays
    None outside tmux (the thread never starts there). A poll tmux does not
    answer leaves the last known count in place — the footer must not flash
    0 boards because one list-windows timed out."""

    def __init__(self, interval_s: float = 5.0, *, run=subprocess.run,
                 environ=os.environ, clock=time):
        self._interval, self._run, self._env, self._clock = interval_s, run, dict(environ), clock
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.value: int | None = None

    def start(self) -> None:
        if not server_args(self._env) or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="board-count", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)

    def _loop(self) -> None:
        while not self._stop.is_set():
            boards = list_boards(run=self._run, environ=self._env)
            if boards is not None:      # a poll tmux did not answer leaves the
                self.value = len(boards)   # last known count alone, never 0
            self._stop.wait(self._interval)
