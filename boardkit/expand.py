"""boardkit.expand — inline row expansion with background fetch, token ownership,
dedupe of in-flight fetches, and a caller-owned cache."""
from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from typing import Callable

from boardkit.cursor import open_overlay


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
