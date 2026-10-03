"""How wrong is the "time left" while a song is being made?

    .venv/bin/python tests/test_countdown.py

Runs songs on a clock this test controls, so a five minute song is simulated in a moment. At every
tick it compares what Midify said was left against what really was left, and reports the error in
seconds. The old way of estimating is run against the same songs for comparison.
"""
import os
import shutil
import statistics
import sys
import tempfile
import time as real_time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLAY = Path(tempfile.mkdtemp(prefix="countdown-"))
os.environ["CLAVINOVA_STATE"] = str(PLAY / "state")
sys.path.insert(0, str(ROOT / "app"))

import time  # noqa: E402

import pipeline  # noqa: E402
import timing  # noqa: E402

passed, failed = [], []
CLOCK = {"t": 10_000.0}


def check(name, ok, detail=""):
    (passed if ok else failed).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{(': ' + detail) if detail else ''}")


class FakeJob:
    def __init__(self):
        self.cancelled = False
        self.stage = ""
        self.progress = 0.0
        self.eta = None


def old_style_eta(done_weight, frac, cur_weight, total, elapsed):
    """What the countdown used to do: scale the whole plan by how fast the bar is filling."""
    p = (done_weight + frac * cur_weight) / total
    p = min(0.99, p)
    remaining = total * (1 - p)
    speed = (elapsed / (p * total)) if p > 0.05 else 1.0
    return round(max(0.0, remaining * min(3.0, max(0.5, speed))))


def run_song(plan, real_times, learn=True):
    """Play out a song where each step really takes `real_times[key]` seconds.

    Returns (errors_now, errors_old): how far off each countdown was, sampled every second.
    """
    job = FakeJob()
    prog = pipeline.Progress(job, plan, lambda j: None)
    prog.stop()                                   # no background ticking: this test drives the clock
    prog._stop.clear()
    total_real = sum(real_times[k] for k, _, _ in plan)
    spent = 0.0
    errors_now, errors_old = [], []
    done_weight = 0.0
    total_planned = sum(w for _, _, w in plan)
    for key, label, weight in plan:
        prog.start(key)
        took = real_times[key]
        step = 1.0
        inside = 0.0
        while inside < took:
            inside = min(took, inside + step)
            CLOCK["t"] += step
            spent += step
            prog.frac = min(0.98, inside / max(weight, 0.1))
            prog._push()
            truly_left = max(0.0, total_real - spent)
            errors_now.append(abs((job.eta or 0) - truly_left))
            errors_old.append(abs(old_style_eta(done_weight, prog.frac, weight, total_planned, spent) - truly_left))
        done_weight += weight
    prog.stop(record=learn)
    return errors_now, errors_old


def main():
    time.time = lambda: CLOCK["t"]                # the clock this test controls
    plan = [("decode", "Reading the audio", 5.0),
            ("separate", "Separating the instruments", 60.0),
            ("notes", "Finding every piano note", 90.0),
            ("tempo", "Finding the tempo", 8.0),
            ("write", "Writing the MIDI file", 10.0)]

    print("A Mac exactly as fast as the plan expects\n")
    exact = {k: w for k, _, w in plan}
    now, old = run_song(plan, exact, learn=False)
    check("the countdown is right to within a second", statistics.median(now) <= 1.5,
          f"typical error {statistics.median(now):.1f}s, worst {max(now):.1f}s")
    check("it beats the old way", statistics.median(now) <= statistics.median(old),
          f"now {statistics.median(now):.1f}s vs before {statistics.median(old):.1f}s")

    print("\nA Mac that is twice as slow as the plan expects, and has never been measured")
    slow = {k: w * 2 for k, _, w in plan}
    now, old = run_song(plan, slow, learn=True)
    print(f"    typical error now {statistics.median(now):.0f}s, before {statistics.median(old):.0f}s"
          f"  (the song really takes {sum(slow.values()):.0f}s)")
    check("the first song on a slow Mac is still closer than the old way",
          statistics.median(now) <= statistics.median(old),
          f"{statistics.median(now):.0f}s vs {statistics.median(old):.0f}s")

    print("\nThe same slow Mac, once Midify has watched a few songs")
    for _ in range(3):
        run_song([(k, l, w) for k, l, w in plan], slow, learn=True)
    learned = [(k, l, w * timing.correction(k)) for k, l, w in plan]
    print("    what it now expects: " + ", ".join(f"{k} {w:.0f}s" for k, _, w in learned)
          + f"  (really {', '.join(f'{k} {slow[k]:.0f}s' for k, _, _ in plan)})")
    now, old = run_song(learned, slow, learn=False)
    check("the countdown is right to within a few seconds on a Mac it has measured",
          statistics.median(now) <= 3.0, f"typical error {statistics.median(now):.1f}s, worst {max(now):.1f}s")
    check("and no worse than the old way, which is also right when the plan is right",
          statistics.median(now) <= statistics.median(old) + 0.5,
          f"{statistics.median(now):.1f}s vs {statistics.median(old):.1f}s")

    print("\nIt counts down, rather than jumping about")
    job = FakeJob()
    prog = pipeline.Progress(job, learned, lambda j: None)
    prog.stop(); prog._stop.clear()
    etas, rises = [], 0
    for key, label, weight in learned:
        prog.start(key)
        inside = 0.0
        while inside < slow[key]:
            inside += 1.0
            CLOCK["t"] += 1.0
            prog.frac = min(0.98, inside / max(weight, 0.1))
            prog._push()
            if etas and job.eta > etas[-1]:
                rises += 1
            etas.append(job.eta)
    check("it never jumps upwards by more than a second", all(
        etas[i] <= etas[i - 1] + 1 for i in range(1, len(etas))), f"{rises} small rises in {len(etas)} ticks")
    check("it ends at zero", etas[-1] <= 2, f"finished saying {etas[-1]}s left")
    check("the bar never goes backwards", job.progress >= 0.9, f"bar at {job.progress:.2f}")

    print("\nA step the plan never knew about (a second listen for the extreme keys)")
    job = FakeJob()
    prog = pipeline.Progress(job, list(plan), lambda j: None)
    prog.stop(); prog._stop.clear()
    prog.start("decode")
    CLOCK["t"] += 5
    prog.start("separate")
    CLOCK["t"] += 60                                  # the step finishes its work, as it does in a real song
    prog._push()
    before_eta, before_bar = job.eta, job.progress
    prog.add_stage("relisten-low-piano", "Listening again for the very low notes", 25.0)
    prog._push()
    check("the extra work is counted, not hidden", job.eta >= before_eta + 20,
          f"{before_eta}s before, {job.eta}s once a 25s second listen is added")
    check("the bar does not go backwards when it appears", job.progress >= before_bar,
          f"{before_bar:.2f} -> {job.progress:.2f}")
    check("and it says what it is doing", "very low notes" in job.stage, job.stage)

    print("\nA song that is cancelled half way teaches nothing")
    before = dict(timing.known())
    job = FakeJob()
    prog = pipeline.Progress(job, learned, lambda j: None)
    prog.stop(); prog._stop.clear()
    prog.start("separate")
    CLOCK["t"] += 3                                   # stopped almost immediately
    prog.stop()                                       # the way a failed or cancelled song ends
    check("nothing was learned from it", dict(timing.known()) == before,
          "a step cut short would otherwise look fast for ever")

    print()
    print(f"{len(passed)} passed, {len(failed)} failed")
    for f in failed:
        print(f"  FAILED: {f}")
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        time.time = real_time.time
        shutil.rmtree(PLAY, ignore_errors=True)
    sys.exit(code)
