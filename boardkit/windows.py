"""boardkit.windows — the `board: …` herdr tabs (y9nb; herdr since rh2y).

`[` / `]` in any board focus the previous / next herdr tab whose label
starts with BOARD_PREFIX, across every workspace of the herdr server the
board itself runs under (the socket herdr names in $HERDR_SOCKET_PATH).
Outside a herdr pane ($HERDR_PANE_ID unset) there is nothing to switch and
every call says so. BoardCount polls the same list on a daemon thread so
the loop can hand the board the count without a subprocess on the paint
path.

`list_boards` separates "herdr did not answer" (None) from "no board tabs"
([]): a failed poll must not turn a real count into 0, and a failed switch
must say what actually happened.

`herdr_json` is the one way boardkit talks to herdr: `herdr <args>`, its
JSON reply's `result` object, or None when herdr did not answer with one.
`herdr_request` is the exception, for a socket API method the CLI does not
expose (`pane.focus`, by pane id: `herdr pane focus` only moves to a
neighbour).
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time

BOARD_PREFIX = "board: "
_TIMEOUT = 5


def inside(environ=os.environ) -> bool:
    """Is this process running in a herdr pane?"""
    return bool(environ.get("HERDR_PANE_ID"))


def _herdr(args: list[str], run, environ, timeout: float):
    try:
        return run(["herdr"] + list(args), env=dict(environ), timeout=timeout,
                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None


def herdr_ok(args: list[str], run, environ, timeout: float = _TIMEOUT) -> bool:
    """Did `herdr <args>` run and exit 0? For the verbs that print nothing
    (`pane run`)."""
    p = _herdr(args, run, environ, timeout)
    return p is not None and p.returncode == 0


def herdr_json(args: list[str], run, environ, timeout: float = _TIMEOUT) -> dict | None:
    """The `result` object of `herdr <args>`'s JSON reply, or None when herdr
    did not run, exited non-zero, or answered something that is not an
    object with a `result` object in it."""
    p = _herdr(args, run, environ, timeout)
    if p is None:
        return None
    out = getattr(p, "stdout", None)
    if p.returncode != 0 or out is None:
        return None
    if isinstance(out, bytes):
        out = out.decode("utf-8", "replace")
    try:
        j = json.loads(out)
    except ValueError:
        return None
    res = j.get("result") if isinstance(j, dict) else None
    return res if isinstance(res, dict) else None


def herdr_request(method: str, params: dict, environ, timeout: float = _TIMEOUT) -> dict | None:
    """The `result` object of one herdr socket API request, sent as one
    JSON line to the server at $HERDR_SOCKET_PATH (the server this pane runs
    under), or None when no socket is named, the server did not answer, or
    it answered an error or another request's reply."""
    path = environ.get("HERDR_SOCKET_PATH") or ""
    if not path:
        return None
    rid = f"boardkit:{method}:{os.getpid()}"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(path)
            s.sendall((json.dumps({"id": rid, "method": method, "params": params}) + "\n").encode())
            buf = b""
            while b"\n" not in buf and len(buf) < 1 << 20:
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
    except OSError:
        return None
    try:
        j = json.loads(buf.split(b"\n", 1)[0].decode("utf-8", "replace"))
    except ValueError:
        return None
    if not isinstance(j, dict) or j.get("id") != rid or j.get("error") is not None:
        return None
    res = j.get("result")
    return res if isinstance(res, dict) else None


def list_boards(run=subprocess.run, environ=os.environ) -> list[tuple[str, str]] | None:
    """(tab_id, label) for every herdr tab whose label starts with
    BOARD_PREFIX, in herdr's own order. [] when herdr answered and there are
    none, and [] outside a herdr pane. None when herdr did not answer. A
    malformed row is dropped."""
    if not inside(environ):
        return []
    res = herdr_json(["tab", "list"], run, environ)
    tabs = res.get("tabs") if res is not None else None
    if not isinstance(tabs, list):
        return None
    boards, seen = [], set()
    for t in tabs:
        if not isinstance(t, dict):
            continue
        tid, label = t.get("tab_id"), t.get("label")
        if (isinstance(tid, str) and tid and isinstance(label, str)
                and label.startswith(BOARD_PREFIX) and tid not in seen):
            seen.add(tid)
            boards.append((tid, label))
    return boards


def current_tab(run=subprocess.run, environ=os.environ) -> str | None:
    """The tab id holding $HERDR_PANE_ID, asked of herdr (a pane can be moved
    to another tab after it starts, so $HERDR_TAB_ID may be stale), or None."""
    pane = environ.get("HERDR_PANE_ID") or ""
    if not pane:
        return None
    res = herdr_json(["pane", "get", pane], run, environ)
    p = res.get("pane") if res is not None else None
    tid = p.get("tab_id") if isinstance(p, dict) else None
    return tid if isinstance(tid, str) and tid else None


def switch_board(forward: bool, *, run=subprocess.run, environ=os.environ) -> str:
    """Focus the next (forward) / previous board tab; returns the footer
    notice. From a tab that is not itself a board tab, `]` goes to the
    first board and `[` to the last."""
    if not inside(environ):
        return "not inside herdr: no board tabs to switch"
    boards = list_boards(run=run, environ=environ)
    if boards is None:
        return "herdr did not answer"
    if not boards:
        return "no board tabs in herdr"
    if len(boards) == 1:
        return "no other board tab (1 board)"
    cur = current_tab(run=run, environ=environ)
    ids = [tid for tid, _ in boards]
    if cur in ids:
        i = (ids.index(cur) + (1 if forward else -1)) % len(ids)
    else:
        i = 0 if forward else len(ids) - 1
    tid, name = boards[i]
    if herdr_json(["tab", "focus", tid], run, environ) is None:
        return f"herdr tab focus {tid} failed"
    return f"{name} ({i + 1}/{len(boards)})"


class BoardCount:
    """How many board tabs herdr has, polled every `interval_s` on a daemon
    thread. `.value` is None until the first poll lands and stays None
    outside a herdr pane (the thread never starts there). A poll herdr does
    not answer leaves the last known count in place — the footer must not
    flash 0 boards because one tab list timed out."""

    def __init__(self, interval_s: float = 5.0, *, run=subprocess.run,
                 environ=os.environ, clock=time):
        self._interval, self._run, self._env, self._clock = interval_s, run, dict(environ), clock
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.value: int | None = None

    def start(self) -> None:
        if not inside(self._env) or self._thread is not None:
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
            if boards is not None:      # a poll herdr did not answer leaves the
                self.value = len(boards)   # last known count alone, never 0
            self._stop.wait(self._interval)
