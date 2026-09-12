"""Note accuracy scoring, shared by the tests and by browser-versus-Mac comparisons.

Run it directly to check the scorer itself against known answers:
    python tests/score_notes.py
"""
import sys

import mir_eval
import numpy as np
import pretty_midi


def load(src, drums=False):
    """MIDI file path or PrettyMIDI object to (intervals, pitches in Hz)."""
    pm = src if isinstance(src, pretty_midi.PrettyMIDI) else pretty_midi.PrettyMIDI(str(src))
    iv, hz = [], []
    for inst in pm.instruments:
        if inst.is_drum != drums:
            continue
        for n in inst.notes:
            iv.append([n.start, max(n.end, n.start + 0.01)])
            hz.append(pretty_midi.note_number_to_hz(n.pitch))
    return np.array(iv).reshape(-1, 2), np.array(hz)


def score(ref, est):
    """Onset accuracy (50 ms window) and onset+offset accuracy, as F1."""
    ri, rp = load(ref)
    ei, ep = load(est)
    if len(ri) == 0 or len(ei) == 0:
        return dict(n_ref=len(ri), n_est=len(ei), onset_p=0.0, onset_r=0.0, onset_f1=0.0, offset_f1=0.0)
    p, r, f, _ = mir_eval.transcription.precision_recall_f1_overlap(ri, rp, ei, ep, offset_ratio=None)
    _, _, f2, _ = mir_eval.transcription.precision_recall_f1_overlap(ri, rp, ei, ep)
    return dict(n_ref=len(ri), n_est=len(ei), onset_p=round(p, 3), onset_r=round(r, 3),
                onset_f1=round(f, 3), offset_f1=round(f2, 3))


def shifted(path, semis=0, secs=0.0, drop_every=0):
    pm = pretty_midi.PrettyMIDI(str(path))
    for inst in pm.instruments:
        kept = []
        for i, n in enumerate(inst.notes):
            if drop_every and i % drop_every == 0:
                continue
            n.pitch += semis
            n.start += secs
            n.end += secs
            kept.append(n)
        inst.notes = kept
    return pm


def metric_selftest(ref):
    """The scorer must give 1.0 for a perfect copy and 0.0 for clearly wrong notes."""
    rows = [
        ("identical", score(ref, ref)["onset_f1"], 1.0),
        ("a semitone off", score(ref, shifted(ref, semis=1))["onset_f1"], 0.0),
        ("200 ms late", score(ref, shifted(ref, secs=0.2))["onset_f1"], 0.0),
        ("half the notes missing", score(ref, shifted(ref, drop_every=2))["onset_f1"], 0.667),
    ]
    ok = True
    for name, got, want in rows:
        good = abs(got - want) < 0.01
        ok &= good
        print(f"  {'PASS' if good else 'FAIL'}  {name:24s} {got:.3f} (expected {want})")
    return ok


if __name__ == "__main__":
    ref = sys.argv[1] if len(sys.argv) > 1 else "tests/fixtures/piano2h.mid"
    print(f"scorer self-test against {ref}")
    sys.exit(0 if metric_selftest(ref) else 1)
