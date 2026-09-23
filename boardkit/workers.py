"""boardkit.workers — Runner, SourceWorker, Coalescer, Gen"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time


class SourceTimeout(Exception):
    """A source subprocess exceeded its hard timeout (child was reaped)."""


def killpg(proc, sig) -> None:
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        pass


class Runner:
    """Real subprocess runner; tests inject fake `run` callables instead.

    Every child runs in its own session (process group) so a timeout or
    kill_all() reaps descendants that would otherwise hold the pipes open.
    """

    def __init__(self):
        self._active: set = set()
        self._lock = threading.Lock()
        self._closed = False        # set by close(): no child may start after it

    def run(self, argv: list[str], timeout: float,
            env: dict | None = None) -> tuple[int, str, str]:
        """`env=None` keeps the parent environment (Popen's default); pass a
        dict to hand the child exactly that environment instead. After
        close() the runner refuses to start anything (OSError) — and a
        child that raced the close between Popen and registration is
        killed as it registers, so nothing started here outlives a
        closed runner."""
        with self._lock:
            if self._closed:
                raise OSError("runner closed")
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            start_new_session=True,
            env=env,
        )
        with self._lock:
            self._active.add(proc)
            closed = self._closed
        if closed:                  # lost the race with close(): reap it now
            killpg(proc, signal.SIGKILL)
        try:
            try:
                out, err = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                killpg(proc, signal.SIGTERM)
                try:
                    proc.communicate(timeout=2)
                except subprocess.TimeoutExpired:
                    killpg(proc, signal.SIGKILL)
                    try:
                        proc.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        for pipe in (proc.stdout, proc.stderr):
                            try:
                                pipe.close()
                            except Exception:
                                pass
                raise SourceTimeout(f"{argv[0]} timed out after {timeout}s")
            return proc.returncode, out, err
        finally:
            with self._lock:
                self._active.discard(proc)

    def kill_all(self) -> None:
        """SIGKILL every in-flight child's process group (shutdown path)."""
        with self._lock:
            procs = list(self._active)
        for proc in procs:
            killpg(proc, signal.SIGKILL)

    def close(self) -> None:
        """kill_all() and refuse every later run(): the shutdown path for a
        runner whose callers may still be on their way in (a confirm
        thread that reached Popen as the board loop exited)."""
        with self._lock:
            self._closed = True
        self.kill_all()


class Coalescer:
    """Bounded event coalescing with an injectable clock.

    Single-threaded: main tick loop only. First on_event() after quiet arms
    a deadline clock()+delay; further events while armed do nothing; events
    during an in-progress refresh set a follow_up flag which re-arms on
    on_refresh_done(); due() is true past the deadline; on_refresh_started()
    disarms.
    """

    def __init__(self, delay: float = 2.0, clock=time.monotonic):
        self._delay = delay
        self._clock = clock
        self._deadline: float | None = None
        self._refreshing = False
        self._follow_up = False

    def on_event(self) -> None:
        if self._refreshing:
            self._follow_up = True
        elif self._deadline is None:
            self._deadline = self._clock() + self._delay

    def on_refresh_started(self) -> None:
        self._deadline = None
        self._refreshing = True

    def on_refresh_done(self) -> None:
        self._refreshing = False
        if self._follow_up:
            self._follow_up = False
            self._deadline = self._clock() + self._delay

    def due(self) -> bool:
        return self._deadline is not None and self._clock() >= self._deadline


class SourceWorker(threading.Thread):
    """Daemon thread running one source's fetch cycle on an interval.

    Each cycle captures a generation via gen_fn, runs fn, and puts
    (kind, gen, ok, payload_or_error, wallclock) on out_queue. Catches
    everything except SystemExit/KeyboardInterrupt — the worker never dies.
    refresh() wakes the loop early; stop() sets the stop flag, wakes it and
    invokes `cancel` (wired to Runner.kill_all) so an in-flight subprocess
    is reaped instead of waited for; join() is then bounded.
    """

    def __init__(self, kind: str, fn, interval: float, out_queue, gen_fn,
                 cancel=None):
        super().__init__(name=f"source-{kind}", daemon=True)
        self._kind = kind
        self._fn = fn
        self._interval = interval
        self._out = out_queue
        self._gen_fn = gen_fn
        self._cancel = cancel
        self._wake = threading.Event()
        self._stopped = False

    def run(self) -> None:
        while not self._stopped:
            g = None
            try:
                g = self._gen_fn()
                payload = self._fn()
                self._out.put((self._kind, g, True, payload, time.time()))
            except (SystemExit, KeyboardInterrupt):
                raise
            except BaseException as e:
                try:
                    self._out.put((self._kind, g, False, str(e), time.time()))
                except BaseException:
                    pass
            self._wake.wait(self._interval)
            self._wake.clear()

    def refresh(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stopped = True
        self._wake.set()
        if self._cancel is not None:
            try:
                self._cancel()
            except Exception:
                pass


class Gen:
    """Thread-safe generation counter; `r` bumps it so in-flight results
    from before the refresh are dropped by merge_result."""

    def __init__(self):
        self._v = 0
        self._lock = threading.Lock()

    def get(self) -> int:
        with self._lock:
            return self._v

    def bump(self) -> int:
        with self._lock:
            self._v += 1
            return self._v
