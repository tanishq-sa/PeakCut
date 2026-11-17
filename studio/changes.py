"""Small dashboard updates, with a complete response when an old version is unknown."""
import collections
import copy
import threading

_snapshots = collections.OrderedDict()
_lock = threading.Lock()


def response(uid, token, current, previous):
    snapshot = copy.deepcopy(current)
    with _lock:
        old = _snapshots.get((uid, previous))
        _snapshots[(uid, token)] = snapshot
        _snapshots.move_to_end((uid, token))
        while len(_snapshots) > 32:
            _snapshots.popitem(last=False)
    if old is None:
        return current
    result = {"v": token, "delta": True}
    for field in ("jobs", "creators"):
        before = {row["id"]: row for row in old[field]}
        after = {row["id"]: row for row in snapshot[field]}
        result[field] = [row for key, row in after.items() if before.get(key) != row]
        result["removed_" + field] = [key for key in before if key not in after]
    for field in ("accounts", "ai", "unread"):
        if snapshot[field] != old[field]:
            result[field] = snapshot[field]
    return result
