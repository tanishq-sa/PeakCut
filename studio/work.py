"""Stop work cooperatively, terminate its processes, and reject replies that arrive late."""
import contextlib
import contextvars
import os
import signal
import subprocess
import threading
import time


class Cancelled(RuntimeError):
    pass


class Task:
    def __init__(self, key, timeout=None, parent=None):
        self.key, self.parent = key, parent
        self.stopped = threading.Event()
        self.deadline = time.monotonic() + timeout if timeout is not None else None

    def check(self):
        if self.parent:
            self.parent.check()
        if self.stopped.is_set():
            raise Cancelled("Stopped by you")
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise Cancelled("This work took too long and was stopped. Try again.")


_current = contextvars.ContextVar("peakcut_work", default=None)
_tasks = {}
_lock = threading.RLock()


@contextlib.contextmanager
def task(key, timeout=None):
    parent = _current.get()
    if parent and parent.key == key:
        parent.check()
        yield parent
        return
    current = Task(key, timeout, parent)
    with _lock:
        previous = _tasks.get(key)
        if previous:
            previous.stopped.set()
        _tasks[key] = current
    token = _current.set(current)
    try:
        current.check()
        yield current
        current.check()
    finally:
        _current.reset(token)
        with _lock:
            if _tasks.get(key) is current:
                _tasks.pop(key, None)


def cancel(key):
    with _lock:
        current = _tasks.get(key)
        if current:
            current.stopped.set()


def check():
    current = _current.get()
    if current:
        current.check()


def wait(seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        check()
        time.sleep(min(.1, max(0, end - time.monotonic())))
    check()


def run(cmd, timeout=None, runner=subprocess.run, **kwargs):
    if _current.get() is None:
        return runner(cmd, timeout=timeout, **kwargs)
    check()
    kwargs.pop("capture_output", None)
    kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if os.name != "nt":
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(cmd, **kwargs)
    end = time.monotonic() + timeout if timeout else None
    try:
        while True:
            check()
            if end is not None and time.monotonic() >= end:
                raise subprocess.TimeoutExpired(cmd, timeout)
            try:
                stdout, stderr = proc.communicate(timeout=.1)
                check()
                return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
            except subprocess.TimeoutExpired:
                continue
    finally:
        if proc.poll() is None:
            if os.name != "nt":
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                proc.kill()
            proc.communicate()
