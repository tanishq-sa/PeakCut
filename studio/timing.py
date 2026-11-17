"""Seconds spent on each stage, saved with the job so slow work can be identified."""
import threading
import time

from . import ctx

_lock = threading.Lock()


def add(job, stage, seconds):
    with _lock:
        values = job.setdefault("timings", {})
        values[stage] = round(values.get(stage, 0) + max(0, seconds), 4)
    ctx.bump(job.get("user"))


def call(job, stage, fn, *args, **kwargs):
    started = time.monotonic()
    try:
        return fn(*args, **kwargs)
    finally:
        add(job, stage, time.monotonic() - started)
