"""boardkit.launcher — `board [<context>]`: one herdr tab per board context
(y9nb; herdr, not tmux, since jibot-code#rh2y).

`board <context>` puts the board in a herdr tab labelled `board: <context>`
in the herdr workspace labelled `jibot`, created (cwd ~/workspaces/jibot)
when herdr has none. The tab is a SHELL with the launch line typed at it —
repoman-board-refresh relaunches a board by typing into its pane at a
shell, and a tab whose command is the board would close on `q`. An existing
tab, in any workspace, is focused, never doubled. `board` alone lists the
contexts and which tabs are open.

Inside a herdr pane ($HERDR_PANE_ID) the launcher focuses the board's tab;
outside one it names the `herdr tab focus` to run and moves nothing. Every
herdr reply is checked: a tab list that did not answer must never read as
"the tab is not open".
"""
from __future__ import annotations

import os
import shlex
import subprocess
import sys

from boardkit import context as kit_context
from boardkit.windows import herdr_json, herdr_ok, inside

WORKSPACE_LABEL = "jibot"
_TIMEOUT = 30


def _workspace_dir(environ) -> str:
    return os.path.join(environ.get("HOME") or os.path.expanduser("~"), "workspaces", "jibot")


def workspace(run, environ) -> tuple[str, str | None]:
    """("ok", workspace_id) for the first herdr workspace labelled `jibot`,
    creating it when herdr has none; ("list", None) when the workspace list
    did not answer; ("create", None) when the create failed."""
    res = herdr_json(["workspace", "list"], run, environ, _TIMEOUT)
    rows = res.get("workspaces") if res is not None else None
    if not isinstance(rows, list):
        return "list", None
    for w in rows:
        if isinstance(w, dict) and w.get("label") == WORKSPACE_LABEL:
            wid = w.get("workspace_id")
            if isinstance(wid, str) and wid:
                return "ok", wid
    res = herdr_json(["workspace", "create", "--label", WORKSPACE_LABEL,
                      "--cwd", _workspace_dir(environ), "--no-focus"], run, environ, _TIMEOUT)
    w = res.get("workspace") if res is not None else None
    wid = w.get("workspace_id") if isinstance(w, dict) else None
    return ("ok", wid) if isinstance(wid, str) and wid else ("create", None)


def find_tab(name: str, run, environ) -> tuple[str, str | None]:
    """Tri-state: ("ok", tab_id) for the tab labelled exactly `name`;
    ("ok", None) when herdr answered and has no such tab; ("failed", None)
    when the lookup itself did not answer.

    y9nb: "looked up, absent" and "lookup failed" must not collapse into one
    value — a failed list read as "absent" opens a SECOND board."""
    res = herdr_json(["tab", "list"], run, environ, _TIMEOUT)
    tabs = res.get("tabs") if res is not None else None
    if not isinstance(tabs, list):
        return "failed", None
    for t in tabs:
        if isinstance(t, dict) and t.get("label") == name:
            tid = t.get("tab_id")
            if isinstance(tid, str) and tid:
                return "ok", tid
    return "ok", None


def launch_line(repoman_dir: str, ctx, environ=os.environ) -> str:
    # kk6e S3a: an absolute launch[0] (starts with "/") or a "~"-prefixed one
    # names a program outside ops/repoman and is used as is — "~" expands
    # against `environ["HOME"]` (kit_context._expand_home falls back to
    # os.path.expanduser when HOME is absent, matching a real os.environ).
    # A relative launch[0] still joins under repoman_dir, as before.
    head = ctx.launch[0]
    if head.startswith("/"):
        program = head
    elif head.startswith("~"):
        program = kit_context._expand_home(head, environ)
    else:
        program = os.path.join(repoman_dir, head)
    argv = [program] + list(ctx.launch[1:])
    return " ".join(shlex.quote(t) for t in argv)


def _list(run, environ, out) -> int:
    # one tab list for every row: the rows cannot disagree with each other
    res = herdr_json(["tab", "list"], run, environ, _TIMEOUT)
    tabs = res.get("tabs") if res is not None else None
    labels = {}
    if isinstance(tabs, list):
        for t in tabs:
            if isinstance(t, dict) and isinstance(t.get("label"), str) and isinstance(t.get("tab_id"), str):
                labels.setdefault(t["label"], t["tab_id"])
    rc = 0
    for name in sorted(kit_context.CONTEXTS):
        ctx = kit_context.CONTEXTS[name]
        if not isinstance(tabs, list):
            where, rc = "herdr tab list failed", 3
        elif ctx.window in labels:
            where = f"open in herdr tab {labels[ctx.window]}"
        else:
            where = "not open"
        # kk6e: a schema-less context (no snapshot yet) prints "no snapshot"
        # where the schema goes.
        schema = ctx.schema if ctx.schema is not None else "no snapshot"
        out(f"{name}  {schema}  {where}")
    return rc


def main(argv=None, *, run=subprocess.run, environ=os.environ, out=print,
         err=lambda s: print(s, file=sys.stderr), repoman_dir: str | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) > 1 or (args and args[0].startswith("-")):
        err("usage: board [<context>]   (board alone lists the contexts)")
        return 2
    repoman_dir = repoman_dir or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if not args:
        return _list(run, environ, out)
    try:
        ctx = kit_context.context_for(args[0])
    except KeyError as e:
        err(f"board: {e.args[0]}")
        return 2
    status, tab = find_tab(ctx.window, run, environ)
    if status != "ok":
        err("board: herdr tab list failed")
        return 3
    if tab is not None:
        if not inside(environ):
            out(f"{ctx.window} is open in herdr tab {tab} — herdr tab focus {tab}")
            return 0
        if herdr_json(["tab", "focus", tab], run, environ, _TIMEOUT) is None:
            err(f"board: {ctx.window} is open in herdr tab {tab} but herdr tab focus"
                f" failed — herdr tab focus {tab}")
            return 3
        out(f"{ctx.window} is open in herdr tab {tab} — focused")
        return 0
    status, ws = workspace(run, environ)
    if status == "list":
        err("board: herdr workspace list failed (is the herdr server running? herdr status)")
        return 3
    if status != "ok":
        err(f"board: herdr workspace create failed for {WORKSPACE_LABEL}"
            f" (cwd {_workspace_dir(environ)})")
        return 3
    res = herdr_json(["tab", "create", "--workspace", ws, "--cwd", repoman_dir,
                      "--label", ctx.window, "--no-focus"], run, environ, _TIMEOUT)
    t = res.get("tab") if res is not None else None
    p = res.get("root_pane") if res is not None else None
    tab = t.get("tab_id") if isinstance(t, dict) else None
    pane = p.get("pane_id") if isinstance(p, dict) else None
    if not (isinstance(tab, str) and tab and isinstance(pane, str) and pane):
        err(f"board: herdr tab create failed in workspace {ws}")
        return 3
    # `herdr pane run` types the line at the pane's shell and presses Enter;
    # the text goes literally, so no token in it is read as a key name.
    if not herdr_ok(["pane", "run", pane, launch_line(repoman_dir, ctx, environ)],
                    run, environ, _TIMEOUT):
        if herdr_ok(["tab", "close", tab], run, environ, _TIMEOUT):
            err(f"board: could not type the launch line into {pane}; closed the empty tab")
        else:
            err(f"board: could not type the launch line into {pane} and could not"
                f" close it — close it by hand: herdr tab close {tab}")
        return 3
    if inside(environ) and herdr_json(["tab", "focus", tab], run, environ, _TIMEOUT) is None:
        err(f"board: {ctx.window} opened in herdr tab {tab} but herdr tab focus"
            f" failed — herdr tab focus {tab}")
        return 3
    out(f"{ctx.window} opened in herdr tab {tab} (pane {pane})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
