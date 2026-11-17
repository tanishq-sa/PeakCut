"""A short processing history, kept with each video and shown on its card."""
import contextlib
import contextvars
import re
import time

LIMIT = 120
_current = contextvars.ContextVar("peakcut_log_job", default=None)


@contextlib.contextmanager
def capture(job):
    token = _current.set(job)
    try:
        yield
    finally:
        _current.reset(token)


def current():
    return _current.get()


def clean(message):
    text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", str(message))
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", text)
    text = re.sub(r"(https?://)[^\s/@]+:[^\s/@]+@", r"\1[hidden]@", text)
    text = re.sub(r"(https?://[^\s?#]+)[?#][^\s]*", r"\1?[hidden]", text)
    text = re.sub(r"(?i)\b(authorization\s*:\s*)(?:bearer\s+|basic\s+)?\S+", r"\1[hidden]", text)
    text = re.sub(r"(?i)\b((?:api[_-]?key|token|password|cookie|secret)\s*[:=]\s*)\S+", r"\1[hidden]", text)
    text = re.sub(r"\b(?:sk-[A-Za-z0-9_-]{16,}|AIza[A-Za-z0-9_-]{20,})", "[hidden]", text)
    return text.strip()[:2000]


def write(message, level="info", *, job=None, persist=True):
    job = job if job is not None else current()
    if job is None:
        return
    message = clean(message)
    if not message:
        return
    from . import ctx, jobs
    with jobs._lock:
        rows = job.setdefault("logs", [])
        rows.append({"time": time.time(), "level": level, "message": message})
        del rows[:-LIMIT]
        ctx.bump(job.get("user"))
        if persist:
            jobs.save(job)


def entries(job):
    """Older videos already have some download notes, even though they have no timestamped history."""
    rows = list(job.get("logs") or [])
    if not rows:
        rows = [{"time": None, "level": "info", "message": message}
                for message in (job.get("ai") or {}).get("log", [])]
        if job.get("stage"):
            rows.append({"time": None, "level": "info", "message": job["stage"]})
        if job.get("error"):
            rows.append({"time": None, "level": "error", "message": job["error"]})
        for clip in job.get("clips", []):
            if clip.get("error"):
                rows.append({"time": None, "level": "error", "message": f"{clip['id']}: {clip['error']}"})
    return [dict(row, message=clean(row["message"])) for row in rows[-LIMIT:]]
