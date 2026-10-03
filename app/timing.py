"""How wrong the countdown was last time, so it can be right this time.

The estimate used to come from fixed numbers measured once, on one Mac. On a different machine, or
a busy one, it drifted: the bar crawled and the "time left" jumped around.

This keeps a correction for each step: how long it actually took against how long it was expected
to take. Next song, the plan is multiplied by the middle of recent corrections. A step that is
consistently twice as slow here is simply planned as twice as slow, and the countdown lands.

Corrections, not raw speeds, because a step costs a fixed part plus a part that grows with the
song. A ratio carries both; a speed per second does not, and would swing with song length.
"""
import json
import os
import time
from pathlib import Path

STATE = Path(os.environ.get("CLAVINOVA_STATE", Path(__file__).resolve().parent.parent / "state"))
FILE = STATE / "timings.json"
KEEP = 10                     # recent songs per step: steady, but still follows a real change
SANE = (0.2, 5.0)             # a correction outside this is a hiccup, not this Mac's speed


def _load():
    try:
        data = json.loads(FILE.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:                                    # noqa: BLE001
        return {}


def _save(data):
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        tmp = FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(FILE)
    except Exception:                                    # noqa: BLE001
        pass                                             # a lost measurement must never cost a song


def record(measured):
    """Keep how each step went. `measured` is [(key, actual_seconds, expected_seconds), ...]."""
    data = _load()
    for key, actual, expected in measured:
        if not expected or expected <= 0 or actual is None or actual < 0.2:
            continue
        ratio = actual / expected
        if not SANE[0] / 4 <= ratio <= SANE[1] * 4:      # a stall or a crash, not a speed
            continue
        got = data.get(key) or []
        got.append(round(ratio, 4))
        data[key] = got[-KEEP:]
    data["_updated"] = time.time()
    _save(data)


def correction(key):
    """How much longer this step really takes here than the plan says. 1.0 until it is known."""
    got = _load().get(key)
    if not got:
        return 1.0
    ordered = sorted(got)
    mid = len(ordered) // 2
    middle = ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2
    return min(max(middle, SANE[0]), SANE[1])


def confidence(key):
    """How much to trust it: nothing measured yet, or several runs that agree."""
    got = _load().get(key) or []
    if len(got) < 2:
        return len(got) * 0.5
    spread = max(got) / max(min(got), 1e-6)
    return 1.0 if spread < 1.5 else 0.7


def known():
    return {k: v for k, v in _load().items() if not k.startswith("_")}
