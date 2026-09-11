"""Self-test: proves the app still turns known music into the right notes.

Run:  .venv/bin/python tests/selftest.py

It writes two short songs from known notes, plays them through the Mac's
built-in General MIDI sounds, runs the recordings through the real pipeline,
and scores the MIDI that comes out against the notes that went in. The scorer
is checked on known answers first, so a broken scorer cannot pass a broken app.
"""
import copy
import random
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

import mir_eval  # noqa: E402
import numpy as np  # noqa: E402
import pretty_midi  # noqa: E402

import pipeline  # noqa: E402

PROG = [(57, [57, 60, 64]), (53, [53, 57, 60]), (48, [48, 52, 55]), (55, [55, 59, 62])]
BPM = 100
BEAT = 60 / BPM


def piano_song():
    random.seed(7)
    pm = pretty_midi.PrettyMIDI(initial_tempo=BPM)
    pno = pretty_midi.Instrument(program=0, name="Piano")
    t = 0.0
    for bar in range(10):
        _, triad = PROG[bar % 4]
        for i in range(8):
            p = [triad[0] - 12, triad[1] - 12, triad[2] - 12, triad[1] - 12][i % 4]
            st = t + i * BEAT / 2
            pno.notes.append(pretty_midi.Note(random.randint(45, 70), p, st, st + BEAT / 2 * 0.9))
        for b in (0, 2):
            st = t + b * BEAT
            for p in triad:
                pno.notes.append(pretty_midi.Note(random.randint(60, 85), p + 12, st, st + BEAT * 1.8))
        for b in range(4):
            st = t + b * BEAT
            p = random.choice(triad) + 24 + random.choice([0, 2, 0])
            pno.notes.append(pretty_midi.Note(random.randint(80, 110), p, st, st + BEAT * 0.9))
        t += 4 * BEAT
    pm.instruments.append(pno)
    return pm


def band_song():
    random.seed(7)
    pm = pretty_midi.PrettyMIDI(initial_tempo=BPM)
    drums = pretty_midi.Instrument(program=0, is_drum=True, name="Drums")
    bass = pretty_midi.Instrument(program=33, name="Bass")
    keys = pretty_midi.Instrument(program=0, name="Piano")
    lead = pretty_midi.Instrument(program=73, name="Flute")
    t = 0.0
    for bar in range(10):
        root, triad = PROG[bar % 4]
        for i in range(8):
            st = t + i * BEAT / 2
            drums.notes.append(pretty_midi.Note(70, 42, st, st + 0.1))
            if i in (0, 3, 4):
                drums.notes.append(pretty_midi.Note(100, 36, st, st + 0.1))
            if i in (2, 6):
                drums.notes.append(pretty_midi.Note(100, 38, st, st + 0.1))
        for b in range(4):
            st = t + b * BEAT
            bass.notes.append(pretty_midi.Note(95, root - 12, st, st + BEAT * 0.8))
        for b in (0, 2):
            st = t + b * BEAT
            for p in triad:
                keys.notes.append(pretty_midi.Note(70, p, st, st + BEAT * 1.8))
        for b in range(4):
            st = t + b * BEAT
            p = random.choice(triad) + 12 + random.choice([0, 2])
            lead.notes.append(pretty_midi.Note(95, p, st, st + BEAT * 0.9))
        t += 4 * BEAT
    pm.instruments += [drums, bass, keys, lead]
    return pm


def pitched(pm, keep=lambda i: True):
    iv, hz = [], []
    for ins in pm.instruments:
        if ins.is_drum or not keep(ins):
            continue
        for n in ins.notes:
            iv.append([n.start, max(n.end, n.start + 0.01)])
            hz.append(pretty_midi.note_number_to_hz(n.pitch))
    return np.array(iv).reshape(-1, 2), np.array(hz)


def note_f1(ref, est):
    (ri, rp), (ei, ep) = ref, est
    if not len(ri) or not len(ei):
        return 0.0
    return mir_eval.transcription.precision_recall_f1_overlap(ri, rp, ei, ep, offset_ratio=None)[2]


def hits_f1(ref_times, est_times):
    if not len(ref_times) or not len(est_times):
        return 0.0
    return mir_eval.onset.f_measure(np.array(sorted(ref_times)), np.array(sorted(est_times)), window=0.05)[0]


def without_lead_in(path, bpm):
    pm = pretty_midi.PrettyMIDI(str(path))
    lead = 60.0 / bpm
    for ins in pm.instruments:
        for n in ins.notes:
            n.start = max(0.0, n.start - lead)
            n.end = max(n.start + 1e-3, n.end - lead)
    return pm


def main():
    results = []

    def check(name, value, minimum):
        ok = value >= minimum
        results.append(ok)
        print(f"  {'PASS' if ok else 'FAIL'}  {name:42s} {value:.3f}  (needs {minimum})")

    print("1. The scorer itself (known answers)")
    ref = pitched(piano_song())
    wrong = (ref[0], ref[1] * 2 ** (1 / 12))
    check("identical notes score 1.0", note_f1(ref, ref), 0.999)
    check("notes a semitone off score 0 (shown as 1-score)", 1 - note_f1(ref, wrong), 0.999)

    tmp = Path(tempfile.mkdtemp(prefix="clavinova-selftest-"))
    try:
        bank = pipeline.APPLE_GM if Path(pipeline.APPLE_GM).exists() else pipeline.FALLBACK_SF
        songs = {"piano": piano_song(), "band": band_song()}
        for name, pm in songs.items():
            pm.write(str(tmp / f"{name}.mid"))
            subprocess.run([pipeline.FLUIDSYNTH, "-ni", "-q", "-g", "0.8", "-r", "44100", "-F",
                            str(tmp / f"{name}.wav"), bank, str(tmp / f"{name}.mid")],
                           capture_output=True, check=True, stdin=subprocess.DEVNULL)

        lib = tmp / "lib"
        work = tmp / "work"

        def run(song, mode, in_child=False):
            up = tmp / f"upload-{song}-{mode}.wav"
            shutil.copy(tmp / f"{song}.wav", up)
            job = pipeline.Job(title=f"{song} {mode}", mode=mode, source="upload", upload_path=str(up), duration_hint=27)
            res = (pipeline.run_in_child(job, work, lib) if in_child
                   else pipeline.process(job, work, lib, lambda j: None))
            return res, without_lead_in(lib / res["file"], res["bpm"])

        print("2. Solo piano recording mode (made in its own process, as the app does)")
        res, out = run("piano", "piano", in_child=True)
        check("piano notes", note_f1(pitched(songs["piano"]), pitched(out)), 0.95)
        check("tempo within 3 BPM of 100", 1.0 if abs(res["bpm"] - 100) <= 3 else 0.0, 1.0)

        print("3. Piano version mode (band song)")
        _, out = run("band", "arrange")
        check("all pitched notes", note_f1(pitched(songs["band"]), pitched(out)), 0.75)

        print("4. Full band mode")
        _, out = run("band", "band")
        ref = songs["band"]
        check("bass", note_f1(pitched(ref, lambda i: i.name == "Bass"), pitched(out, lambda i: i.program == 33)), 0.9)
        check("chords and melody", note_f1(pitched(ref, lambda i: i.name in ("Piano", "Flute")),
                                           pitched(out, lambda i: i.program in (0, 73))), 0.7)
        rd = [i for i in ref.instruments if i.is_drum][0]
        od = [n for i in out.instruments if i.is_drum for n in i.notes]
        for label, pitches, gm in (("kick", {36}, 36), ("snare", {38}, 38), ("hi-hat", {42}, 42)):
            check(f"drums: {label}", hits_f1([n.start for n in rd.notes if n.pitch in pitches],
                                             [n.start for n in od if n.pitch == gm]), 0.9)
        left = [p for p in work.iterdir()] if work.exists() else []
        check("no temporary files left behind", 1.0 if not left else 0.0, 1.0)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    passed = sum(results)
    print(f"\n{passed} of {len(results)} checks passed.")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
