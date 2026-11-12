"""Single heavy-model slot and child-process lifecycle.

Rules enforced here (from ``docs/00_MASTER_ARCH.md`` §7):

* Exactly **one** ``AI_HEAVY`` owner at a time. The slot is a process-wide
  non-blocking lock owned by the server, so two UI clicks cannot start two
  models even across threads.
* A subprocess always runs in its own process group; cancellation escalates
  cooperative cancel -> SIGTERM (group) -> SIGKILL (group) -> ``wait()``.
* The slot is released only after the child has been reaped, so "slot free"
  always means "no model resident".
"""

from __future__ import annotations

import os
import fcntl
import resource
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, TextIO


class HeavySlotBusy(RuntimeError):
    code = "HEAVY_SLOT_BUSY"


class WorkerTimeout(RuntimeError):
    code = "WORKER_TIMEOUT"


class WorkerReapFailed(RuntimeError):
    code = "WORKER_ORPHAN_REAP_FAILED"


@dataclass
class HeavySlot:
    """Machine-wide single-owner lock for AI_HEAVY work."""

    lock_path: Path = field(
        default_factory=lambda: Path("/tmp") / f"phuthanhai-ai-heavy-{os.getuid()}.lock"
    )
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _owner: str | None = None
    _acquired_at: float | None = None
    _guard: threading.Lock = field(default_factory=threading.Lock)
    _file_handle: TextIO | None = None

    def try_acquire(self, owner: str) -> bool:
        """Non-blocking acquire. Returns False when another owner holds the slot."""

        if not self._lock.acquire(blocking=False):
            return False
        handle: TextIO | None = None
        try:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            handle = self.lock_path.open("a+")
            os.chmod(self.lock_path, 0o600)
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            if handle is not None:
                handle.close()
            self._lock.release()
            return False
        with self._guard:
            self._file_handle = handle
            self._owner = owner
            self._acquired_at = time.monotonic()
        return True

    def acquire(self, owner: str, *, timeout: float | None = None) -> None:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if self.try_acquire(owner):
                return
            if deadline is not None and time.monotonic() >= deadline:
                raise HeavySlotBusy(f"heavy slot held by {self.owner!r}")
            time.sleep(0.02)

    def release(self, owner: str | None = None) -> None:
        with self._guard:
            if owner is not None and self._owner not in (None, owner):
                raise HeavySlotBusy(f"slot owned by {self._owner!r}, not {owner!r}")
            handle = self._file_handle
            self._file_handle = None
            self._owner = None
            self._acquired_at = None
        if handle is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()
        try:
            self._lock.release()
        except RuntimeError as exc:  # released twice: programmer error, not user error
            raise RuntimeError("heavy slot released while not held") from exc

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    @property
    def owner(self) -> str | None:
        return self._owner

    def snapshot(self) -> dict[str, Any]:
        return {
            "busy": self.busy,
            "owner": self._owner,
            "held_s": None if self._acquired_at is None else round(time.monotonic() - self._acquired_at, 3),
        }


#: The server owns exactly one slot for the whole process.
HEAVY_SLOT = HeavySlot()


@dataclass
class Capture:
    """Result of a bounded child-process run."""

    argv: list[str]
    returncode: int
    stdout: str
    stderr: str
    elapsed_s: float
    peak_rss_bytes: int | None
    timed_out: bool = False

    def json_events(self) -> list[dict[str, Any]]:
        import json

        events: list[dict[str, Any]] = []
        for line in self.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            events.append(json.loads(line))
        return events


def _rss_bytes(pid: int) -> int | None:
    """Read RSS of a live process without third-party packages.

    ``ps`` is invoked with an argv list (never a shell) and the pid is an int we
    created ourselves.
    """

    try:
        out = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(int(pid))],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = out.stdout.strip()
    if not text:
        return None
    try:
        return int(text.split()[0]) * 1024
    except (ValueError, IndexError):
        return None


def _reaped_children_peak_rss_bytes() -> int | None:
    """Best available fallback after a child exits before live RSS sampling."""

    try:
        peak = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    except (OSError, ValueError):
        return None
    if peak <= 0:
        return None
    # macOS reports bytes; Linux and the other supported Unix targets use KiB.
    return int(peak if sys.platform == "darwin" else peak * 1024)


class ProcessRegistry:
    """Owns spawned children and guarantees they are reaped on shutdown."""

    def __init__(self) -> None:
        self._children: dict[int, subprocess.Popen[bytes]] = {}
        self._guard = threading.Lock()

    def spawn(
        self,
        argv: Iterable[str],
        *,
        env: dict[str, str] | None = None,
        cwd: Path | None = None,
    ) -> subprocess.Popen[bytes]:
        arg_list = [str(part) for part in argv]
        popen = subprocess.Popen(  # noqa: S603 - argv list, shell=False by default
            arg_list,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=str(cwd) if cwd else None,
            start_new_session=True,  # own process group -> group kill is safe
        )
        with self._guard:
            self._children[popen.pid] = popen
        return popen

    def run_captured(
        self,
        argv: Iterable[str],
        *,
        env: dict[str, str] | None = None,
        cwd: Path | None = None,
        input_bytes: bytes | None = None,
        timeout_s: float = 300.0,
        poll_interval_s: float = 0.1,
    ) -> Capture:
        arg_list = [str(part) for part in argv]
        started = time.monotonic()
        popen = self.spawn(arg_list, env=env, cwd=cwd)
        peak_rss: int | None = None
        timed_out = False

        def sample() -> None:
            nonlocal peak_rss
            while True:
                value = _rss_bytes(popen.pid)
                if value is not None and (peak_rss is None or value > peak_rss):
                    peak_rss = value
                if popen.poll() is not None:
                    break
                time.sleep(poll_interval_s)

        sampler = threading.Thread(target=sample, name="rss-sampler", daemon=True)
        sampler.start()
        try:
            stdout, stderr = popen.communicate(input=input_bytes, timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            self._terminate(popen)
            stdout, stderr = popen.communicate()
        finally:
            sampler.join(timeout=2)
            with self._guard:
                self._children.pop(popen.pid, None)

        if peak_rss is None:
            peak_rss = _reaped_children_peak_rss_bytes()

        return Capture(
            argv=arg_list,
            returncode=popen.returncode,
            stdout=(stdout or b"").decode("utf-8", "replace"),
            stderr=(stderr or b"").decode("utf-8", "replace"),
            elapsed_s=round(time.monotonic() - started, 3),
            peak_rss_bytes=peak_rss,
            timed_out=timed_out,
        )

    def _terminate(self, popen: subprocess.Popen[bytes], *, grace_s: float = 5.0) -> None:
        """SIGTERM the process group, wait, then SIGKILL if still alive."""

        if popen.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(popen.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            popen.wait(timeout=grace_s)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(os.getpgid(popen.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            popen.wait(timeout=5)
        except subprocess.TimeoutExpired as exc:
            raise WorkerReapFailed(f"child {popen.pid} survived SIGKILL") from exc

    def shutdown(self, *, grace_s: float = 5.0) -> list[int]:
        """Terminate and reap every child; returns pids that were terminated."""

        with self._guard:
            children = list(self._children.values())
        terminated: list[int] = []
        for popen in children:
            if popen.poll() is None:
                self._terminate(popen, grace_s=grace_s)
                terminated.append(popen.pid)
        with self._guard:
            self._children.clear()
        return terminated

    def discard(self, pid: int) -> None:
        """Forget a child that was waited on outside ``run_captured``."""

        with self._guard:
            self._children.pop(int(pid), None)

    @property
    def active_count(self) -> int:
        with self._guard:
            return sum(1 for child in self._children.values() if child.poll() is None)


PROCESSES = ProcessRegistry()
