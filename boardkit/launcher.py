"""boardkit.launcher — `board [<context>]`: one tmux window per board context (y9nb).

`board <context>` puts the board where Ghosthub lists it: a window named
`board: <context>` in the jibot workspace session on the server kwt names
(`kwt open ~/workspaces/jibot --start-session --layout none --json`, the
same call `jw` makes). The window is a SHELL with the launch line typed at
it — repoman-board-refresh relaunches a board by typing into its pane at a
shell, and a window whose command is the board would close on `q`. An
existing window is selected, never doubled. `board` alone lists the
contexts and which windows are open.

Three things the shape depends on: `kwt open` runs from HOME, as `jw` does,
because a kwt open that bootstraps the tmux server pins that server's cwd;
the launch line is typed with `send-keys -l` so tmux parses no token in it
as a key name; and every tmux rc is checked, because a lookup that failed
must never read as "the window is not open".
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys

from boardkit import context as kit_context

_TIMEOUT = 30


def tmux_literal(name: str) -> str:
    """A name for tmux's -n: every `#` doubled; `##[` is kept verbatim by the
    style parser, so it goes over as the literal format `#{l:#[}` (CLAUDE.md,
    jibot-code#c6xp)."""
    return name.replace("#", "##").replace("##[", "#{l:#[}")


def _run(argv, run, environ, timeout=_TIMEOUT, cwd=None):
    try:
        return run(argv, env=dict(environ), timeout=timeout, cwd=cwd,
                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None


def _text(p) -> str:
    out = getattr(p, "stdout", b"")
    return out.decode("utf-8", "replace") if isinstance(out, bytes) else str(out or "")


def _workspace_dir(environ) -> str:
    return os.path.join(environ.get("HOME") or os.path.expanduser("~"), "workspaces", "jibot")


def workspace(run, environ) -> tuple[str, str] | None:
    """(session_name, tmux socket name) for the jibot workspace, or None.

    Run from HOME, as `jw` does (`(cd "$HOME" && kwt open ...)`): a `kwt
    open` that has to bootstrap the tmux server pins that server's working
    directory to the caller's cwd, and every later window inherits it.
    """
    d = _workspace_dir(environ)
    home = environ.get("HOME") or os.path.expanduser("~")
    p = _run(["kwt", "open", d, "--start-session", "--layout", "none", "--json"],
             run, environ, cwd=home)
    if p is None or p.returncode != 0:
        return None
    try:
        j = json.loads(_text(p))
    except ValueError:
        return None
    # y9nb: kwt's JSON is only trusted as far as it is typed. `null` and a
    # list both decode cleanly, and a non-string socket would reach tmux as
    # `-L 1`; each of those is a kwt failure, not a socket name.
    if not isinstance(j, dict):
        return None
    sess, sock = j.get("session_name"), j.get("tmux_socket_name")
    if sock is None or sock == "":
        sock = "default"
    if not isinstance(sess, str) or not sess or not isinstance(sock, str):
        return None
    if not all((c.isascii() and c.isalnum()) or c in "._-" for c in sock):
        return None
    return sess, sock


def find_window(server: list[str], name: str, run, environ
                ) -> tuple[str, tuple[str, str] | None]:
    """Tri-state: ("ok", (window_id, session_name)) for the window called
    exactly `name`; ("ok", None) when the server answered and has no such
    window; ("failed", None) when the lookup itself did not answer.

    y9nb: "looked up, absent" and "lookup failed" must not collapse into one
    value — a failed list-windows read as "absent" opens a SECOND board in a
    session that already has one.
    """
    p = _run(["tmux"] + server + ["list-windows", "-a", "-F",
                                  "#{window_id}\t#{window_name}\t#{session_name}"], run, environ)
    if p is None or p.returncode != 0:
        return "failed", None
    for line in _text(p).splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[1] == name and parts[0].startswith("@"):
            return "ok", (parts[0], parts[2])
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


def _same_server(server: list[str], run, environ) -> bool:
    """Is this process's own tmux client on `server`? (socket PATHS compared)"""
    mine = (environ.get("TMUX") or "").split(",")[0]
    if not mine:
        return False
    p = _run(["tmux"] + server + ["display-message", "-p", "#{socket_path}"], run, environ)
    return p is not None and p.returncode == 0 and _text(p).strip() == mine


def _list(server: list[str], sock: str, run, environ, out) -> int:
    rc = 0
    for name in sorted(kit_context.CONTEXTS):
        ctx = kit_context.CONTEXTS[name]
        status, hit = find_window(server, ctx.window, run, environ)
        if status != "ok":
            where, rc = f"tmux list-windows failed on tmux -L {sock}", 3
        elif hit is not None:
            where = f"open in {hit[1]} ({hit[0]})"
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
    ctx = None
    if args:
        try:
            ctx = kit_context.context_for(args[0])
        except KeyError as e:
            err(f"board: {e.args[0]}")
            return 2
    ws = workspace(run, environ)
    if ws is None:
        d = _workspace_dir(environ)
        err(f"board: kwt open failed for {d} (is it registered? kwt workspace list)")
        return 3
    sess, sock = ws
    server = ["-L", sock]
    if ctx is None:
        return _list(server, sock, run, environ, out)
    status, hit = find_window(server, ctx.window, run, environ)
    if status != "ok":
        err(f"board: tmux list-windows failed on tmux -L {sock}")
        return 3
    if hit is not None:
        wid, in_sess = hit
        if _same_server(server, run, environ):
            # y9nb: select-window first so the target window is the current
            # one in its session, then switch this client to it — the shape
            # app.jump_to_tmux uses for a pane id. tmux's target parser does
            # resolve a bare window id for switch-client, so the pair is
            # belt and braces, not a workaround.
            for cmd in ("select-window", "switch-client"):
                p = _run(["tmux"] + server + [cmd, "-t", wid], run, environ)
                if p is None or p.returncode != 0:
                    err(f"board: {ctx.window} is open in {in_sess} ({wid}) but tmux"
                        f" {cmd} failed — tmux -L {sock} attach -t ={in_sess}")
                    return 3
            out(f"{ctx.window} is open in {in_sess} ({wid}) on tmux -L {sock} — switched")
        else:
            out(f"{ctx.window} is open in {in_sess} ({wid}) on tmux -L {sock}"
                f" — tmux -L {sock} attach -t ={in_sess}")
        return 0
    p = _run(["tmux"] + server + ["new-window", "-t", f"={sess}", "-n", tmux_literal(ctx.window),
                                  "-c", repoman_dir, "-P", "-F", "#{pane_id}"], run, environ)
    pane = _text(p).strip() if p is not None and p.returncode == 0 else ""
    if not pane.startswith("%"):
        err(f"board: tmux new-window failed in {sess} on tmux -L {sock}")
        return 3
    line = launch_line(repoman_dir, ctx, environ)
    # y9nb: `-l` sends the line LITERALLY, so tmux never parses a token in it
    # as a key name; Enter goes as its own send-keys. This is the shape
    # marshalboard.refresh types a relaunch with.
    for keys in (["-l", line], ["Enter"]):
        p = _run(["tmux"] + server + ["send-keys", "-t", pane] + keys, run, environ)
        if p is not None and p.returncode == 0:
            continue
        k = _run(["tmux"] + server + ["kill-pane", "-t", pane], run, environ)
        if k is not None and k.returncode == 0:
            err(f"board: could not type the launch line into {pane};"
                " closed the empty window")
        else:
            err(f"board: could not type the launch line into {pane} and could not"
                f" close it — kill it by hand: tmux -L {sock} kill-pane -t {pane}")
        return 3
    if _same_server(server, run, environ):
        p = _run(["tmux"] + server + ["switch-client", "-t", pane], run, environ)
        if p is None or p.returncode != 0:
            err(f"board: {ctx.window} opened in {sess} ({pane}) but switch-client"
                f" failed — tmux -L {sock} attach -t ={sess}")
            return 3
    out(f"{ctx.window} opened in {sess} ({pane}) on tmux -L {sock}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
