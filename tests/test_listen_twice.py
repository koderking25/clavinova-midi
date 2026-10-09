"""Listening twice, with the AI's chunk boundaries moved.

    .venv/bin/python tests/test_listen_twice.py

Why it exists: Fracture (Stephan Moccio) plays a low D at 0:14.4, 0:15.7, 3:13.4 and 3:13.7, and
Midify heard none of them, while the same eight seconds transcribed on their own came out
perfectly. The notes were falling awkwardly inside the model's 16 second chunks.

Measured before shipping, and the numbers are the reason the merge window is 150 ms:

    match within   known song        Fracture
    one pass       F1 1.000, 0 wrong   0 of 4 found
    80 ms          F1 0.997, 1 wrong   3 of 4 found
    150 ms         F1 1.000, 0 wrong   3 of 4 found
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
import numpy as np  # noqa: E402

import midi_export as mx  # noqa: E402
import pipeline  # noqa: E402

passed, failed = [], []


def check(name, ok, detail=""):
    (passed if ok else failed).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{(': ' + detail) if detail else ''}")


def main():
    silence = np.zeros((pipeline.SR * 30, 2), dtype=np.float32)
    first = [mx.Note(t, t + 0.4, 60 + (i % 5), 90) for i, t in enumerate([2.0, 5.0, 9.0, 14.0])]

    print("What the second pass adds, and what it leaves alone\n")
    # The same notes again, shifted by the padding the real pass uses, plus one the first missed.
    def second(audio):
        o = pipeline.SECOND_GRID_OFFSET
        same = [mx.Note(n.start + o, n.end + o, n.pitch, n.velocity) for n in first]
        missed = mx.Note(7.5 + o, 7.9 + o, 26, 70)          # a low D, the kind Fracture loses
        return same + [missed]

    out, added = pipeline.listen_twice(silence, first, second)
    check("the note the first pass missed is added", added == 1 and any(n.pitch == 26 for n in out),
          f"{added} added")
    check("and nothing it already had is doubled", len(out) == len(first) + 1, f"{len(out)} notes")
    check("every original note is still there, untouched",
          all(any(a.pitch == b.pitch and abs(a.start - b.start) < 1e-6 for a in out) for b in first))
    check("they come back in order", all(out[i].start <= out[i + 1].start for i in range(len(out) - 1)))

    print("\nA note that is really the same one, heard a little differently")
    def nudged(audio):
        o = pipeline.SECOND_GRID_OFFSET
        return [mx.Note(n.start + o + 0.1, n.end + o, n.pitch, n.velocity) for n in first]

    out, added = pipeline.listen_twice(silence, first, nudged)
    check("100 ms apart counts as the same note, not a new one", added == 0, f"{added} added")

    def far(audio):
        o = pipeline.SECOND_GRID_OFFSET
        return [mx.Note(n.start + o + 0.3, n.end + o, n.pitch, n.velocity) for n in first]

    out, added = pipeline.listen_twice(silence, first, far)
    check("300 ms apart is treated as a real repeat", added == len(first), f"{added} added")
    check("the line between those is 150 ms, which is what the measurements chose",
          abs(pipeline.SAME_NOTE_WITHIN - 0.15) < 1e-9, f"{pipeline.SAME_NOTE_WITHIN * 1000:.0f} ms")

    print("\nIt must never cost anyone a song")
    def broken(audio):
        raise RuntimeError("the model fell over")

    out, added = pipeline.listen_twice(silence, first, broken)
    check("if the second pass fails, the first pass is kept", out == first and added == 0)

    print("\nIt only happens when asked for")
    job = pipeline.Job(title="x", mode="piano", source="youtube")
    check("off unless you tick it", job.listen_twice is False)
    check("and the choice reaches the song's own process", "listen_twice" in pipeline.JOB_FIELDS)

    print("\nThe chunk boundaries really do move")
    check("the second pass is offset by half the gap between chunks",
          pipeline.SECOND_GRID_OFFSET == 6.0, f"{pipeline.SECOND_GRID_OFFSET}s against a 12s step")

    print()
    print(f"{len(passed)} passed, {len(failed)} failed")
    for f in failed:
        print(f"  FAILED: {f}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
