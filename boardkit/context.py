"""boardkit.context — the registry of board contexts (em7e S2a, y9nb).

A Context is registry data over the Board protocol in boardkit.app: its
name, the snapshot schema it publishes / reads, where the snapshot lives,
the launch command `board <name>` types, and the tmux window it gets. It
is NOT a second interface — the board object itself is a boardkit.app.Board.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

# y9nb: a context name is a lowercase slug by POLICY, not by a tmux limit:
# it becomes the window name (`board: <name>`), the snapshot file name
# (`<name>.json`) and a token of the launch line, and a slug needs no
# quoting or escaping in any of the three. (tmux itself would cope with a
# "#": launcher.tmux_literal doubles it for `new-window -n` and tmux stores
# the single "#" back, so an exact find_window compare would still match.)
NAME_RE = re.compile(r"[a-z0-9_-]+\Z")

STATE_DIR_ENV = "REPOMAN_BOARD_STATE_DIR"
STATE_DIR_DEFAULT = "~/.local/state/board"


def _expand_home(raw: str, env) -> str:
    # y9nb: os.path.expanduser reads the REAL $HOME, which defeats a fake
    # env passed by a test. Expand a leading "~" against the given env's
    # HOME when the env carries one; otherwise fall back to
    # os.path.expanduser (unchanged default behaviour, incl. env=os.environ).
    if raw.startswith("~") and "HOME" in env:
        # y9nb: strip HOME's trailing "/" so it joins to "/h/..." rather than
        # "/h//...". The stripped form of "/" and of "" is "", so a bare "~"
        # answers "/" and "~/x" answers "/x" — never "//x".
        home = env["HOME"].rstrip("/")
        rest = raw[1:]
        if rest == "":
            return home or "/"
        if rest.startswith("/"):
            return home + rest
    return os.path.expanduser(raw)


def state_dir(env=None) -> str:
    # (the body of marshalboard.publish.state_dir today, plus HOME-aware
    # expansion so a fake env is honoured — see _expand_home above)
    env = os.environ if env is None else env
    return _expand_home(env.get(STATE_DIR_ENV) or STATE_DIR_DEFAULT, env)


def snapshot_path(context: str = "repoman", env=None) -> str:
    return os.path.join(state_dir(env), f"{context}.json")


def window_name(context: str) -> str:
    return f"board: {context}"


@dataclass(frozen=True)
class Context:
    name: str
    schema: str | None      # None = this context publishes no snapshot yet (kk6e)
    # argv for the launch line: relative to ops/repoman, e.g. ("bin/repoman-board",);
    # or, per kk6e, absolute ("/opt/x/bin/tool", …) or "~"-prefixed to name a
    # program OUTSIDE ops/repoman as is — the launcher expands "~" against the
    # environ's HOME.
    launch: tuple

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not NAME_RE.match(self.name):
            raise ValueError(f"board context name {self.name!r} is not [a-z0-9_-]+")

    @property
    def window(self) -> str:
        return window_name(self.name)

    def snapshot(self, env=None) -> str | None:
        # kk6e: a context with no schema publishes no snapshot yet.
        if self.schema is None:
            return None
        return snapshot_path(self.name, env)


CONTEXTS: dict = {
    "repoman": Context("repoman", "repoman/1", ("bin/repoman-board",)),
    # kk6e S3a: dash-tui (command-dashboard) is launched from its own
    # checkout, outside ops/repoman. 39q0: its snapshot schema is dash/1
    # (dash.json, written once command-dashboard#1gkg lands; the refresh
    # job's hung-board check reads it through this entry).
    "dash": Context("dash", "dash/1", ("~/repos/command-dashboard/bin/dash-tui",)),
    # 1wan: the routing board reads routing-project's routing.json and
    # publishes no snapshot of its own, hence no schema.
    "routing": Context("routing", None, ("bin/routing-board",)),
}


def context_for(name: str) -> Context:
    try:
        return CONTEXTS[name]
    except KeyError:
        raise KeyError(f"no board context {name!r}; known: {', '.join(sorted(CONTEXTS))}") from None
