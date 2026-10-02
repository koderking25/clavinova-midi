"""Can Midify reach all 88 keys, A0 to C8, including the extremes?

    .venv/bin/python tests/test_range.py

Plays a chromatic run of every key on the Mac's own piano sound, transcribes it the way each
mode does, and reports exactly which keys came back. Known answer: all 88.
"""
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import pretty_midi
import soundfile as sf

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
import midi_export as mx  # noqa: E402
import pipeline  # noqa: E402
import postproc  # noqa: E402

GM = "/System/Library/Components/CoreAudio.component/Contents/Resources/gs_instruments.dls"
LOW, HIGH = 21, 108          # A0 to C8, a full 88-key piano
STEP = 0.35


def key_name(m):
    return ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"][m % 12] + str(m // 12 - 1)


def chromatic(folder, program=0, pitches=range(LOW, HIGH + 1)):
    pm = pretty_midi.PrettyMIDI(initial_tempo=120)
    inst = pretty_midi.Instrument(program=program)
    for i, p in enumerate(pitches):
        start = i * STEP
        inst.notes.append(pretty_midi.Note(96, int(p), start, start + STEP * 0.8))
    pm.instruments.append(inst)
    midi, wav = folder / "chrom.mid", folder / "chrom.wav"
    pm.write(str(midi))
    subprocess.run(["fluidsynth", "-ni", "-q", "-g", "0.8", "-r", str(pipeline.SR), "-F", str(wav), GM, str(midi)],
                   check=True, capture_output=True)
    truth = {int(p): i * STEP for i, p in enumerate(pitches)}
    return wav, truth


def found_keys(notes, truth, tol=0.25):
    """Which of the played keys came back at roughly the right moment."""
    got = set()
    for pitch, when in truth.items():
        if any(n.pitch == pitch and abs(n.start - when) <= tol for n in notes):
            got.add(pitch)
    return got


def gaps(missing):
    """Describe missing keys as ranges, so 19 missing keys read as one phrase."""
    if not missing:
        return "none"
    runs, start, prev = [], None, None
    for p in sorted(missing):
        if start is None:
            start = prev = p
        elif p == prev + 1:
            prev = p
        else:
            runs.append((start, prev)); start = prev = p
    runs.append((start, prev))
    return ", ".join(key_name(a) if a == b else f"{key_name(a)}-{key_name(b)}" for a, b in runs)


def transcribe_piano(audio):
    notes, _ = pipeline.run_transkun(audio)
    return notes


def main():
    import time
    import extremes
    folder = Path(tempfile.mkdtemp(prefix="range-"))
    fails = []
    try:
        wav, truth = chromatic(folder)
        x = pipeline.decode_audio(wav, folder / "dec.wav")

        print("A chromatic run of all 88 keys, A0 to C8.\n")
        print("What each listener reaches on its own:")
        straight, _ = pipeline.run_transkun(x)
        straight = [n for n in straight if LOW <= n.pitch <= HIGH]
        got = found_keys(straight, truth)
        print(f"  piano listener, straight                       {len(got)}/88   missing: {gaps(set(truth) - got)}")
        for label, lo, hi in (("Basic Pitch, melody limits", 80, 1400), ("Basic Pitch, bass limits", 30, 400)):
            g = found_keys(postproc.mono(pipeline.run_basic_pitch(x, folder / f"bp{lo}.wav", lo, hi)), truth)
            print(f"  {label:46s} {len(g)}/88   missing: {gaps(set(truth) - g)}")

        print("\nWith the second-listen passes (what the app now does):")
        t0 = time.time()
        full, said = extremes.reach_all_88(x, pipeline.SR, straight, transcribe_piano, mx.Note)
        took = time.time() - t0
        gotfull = found_keys(full, truth)
        print(f"  piano listener, all passes                     {len(gotfull)}/88   missing: {gaps(set(truth) - gotfull)}")
        print(f"  it says: {said!r}")
        print(f"  extra time on a {len(x) / pipeline.SR:.0f}s song: {took:.0f}s")
        if len(gotfull) < 88:
            fails.append(f"all 88 keys: reached only {len(gotfull)}")
        if len(gotfull) < len(got):
            fails.append("the second listen lost keys the straight pass had")

        kept = {(n.pitch, round(n.start, 2)) for n in straight}
        if not kept <= {(n.pitch, round(n.start, 2)) for n in full}:
            fails.append("the second listen changed or dropped notes from the straight pass")
        else:
            print("  every note from the straight pass survived unchanged: yes")

        print("\nCost control: a song that only uses the middle of the keyboard")
        wav2, truth2 = chromatic(folder, pitches=range(48, 73))
        x2 = pipeline.decode_audio(wav2, folder / "dec2.wav")
        straight2, _ = pipeline.run_transkun(x2)
        straight2 = [n for n in straight2 if LOW <= n.pitch <= HIGH]
        t0 = time.time()
        full2, said2 = extremes.reach_all_88(x2, pipeline.SR, straight2, transcribe_piano, mx.Note)
        took2 = time.time() - t0
        print(f"  extra listening asked for: {extremes.wanted(straight2)}")
        print(f"  extra time: {took2:.1f}s, notes added: {len(full2) - len(straight2)}, it says: {said2!r}")
        if took2 > 2.0 or len(full2) != len(straight2):
            fails.append(f"a middle-of-the-keyboard song paid for extra listening ({took2:.1f}s, {len(full2) - len(straight2)} notes)")

        print("\nSongs that really do use the edge keys")
        for label, pitches, want_keys in (("bass at A0 and A1", [21, 33, 21, 28], {21}),
                                          ("melody at C7 and C8", [96, 108, 103, 108], {108})):
            wav3, truth3 = chromatic(folder, pitches=pitches * 6)
            x3 = pipeline.decode_audio(wav3, folder / "dec3.wav")
            s3, _ = pipeline.run_transkun(x3)
            s3 = [n for n in s3 if LOW <= n.pitch <= HIGH]
            before = found_keys(s3, truth3)
            after_notes, said3 = extremes.reach_all_88(x3, pipeline.SR, s3, transcribe_piano, mx.Note)
            after = found_keys(after_notes, truth3)
            print(f"  {label:22s} straight {len(before)}/{len(truth3)} keys -> second listen {len(after)}/{len(truth3)}"
                  f"   {'reached ' + ', '.join(key_name(k) for k in sorted(want_keys & after)) if want_keys & after else 'still missing ' + ', '.join(key_name(k) for k in sorted(want_keys))}")
            if not want_keys <= after:
                fails.append(f"{label}: did not reach {', '.join(key_name(k) for k in sorted(want_keys - after))}")
    finally:
        import shutil
        shutil.rmtree(folder, ignore_errors=True)
    print()
    for f in fails:
        print(f"FAIL: {f}")
    print(f"{'ALL CHECKS PASSED' if not fails else str(len(fails)) + ' CHECK(S) FAILED'}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
