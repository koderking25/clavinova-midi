"""Pasting a YouTube link, and making only the first part of a long recording.

    .venv/bin/python tests/test_trim.py

Both asked for on 9 October 2026: a 26 minute jazz recording that Midify refused outright because
it was over the 15 minute limit, and links that had to be searched for by name instead of pasted.
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLAY = Path(tempfile.mkdtemp(prefix="trim-test-"))
os.environ.update(CLAVINOVA_STATE=str(PLAY / "state"), CLAVINOVA_WORK=str(PLAY / "work"),
                  CLAVINOVA_LIBRARY=str(PLAY / "library"))
sys.path.insert(0, str(ROOT / "app"))

import mido  # noqa: E402
import pretty_midi  # noqa: E402

import midi_export as mx  # noqa: E402
import pipeline  # noqa: E402
import postproc  # noqa: E402
import sources  # noqa: E402

GM = "/System/Library/Components/CoreAudio.component/Contents/Resources/gs_instruments.dls"
passed, failed = [], []


def check(name, ok, detail=""):
    (passed if ok else failed).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{(': ' + detail) if detail else ''}")


def main():
    print("Pasting a link instead of searching\n")
    good = {
        "https://www.youtube.com/watch?v=hYH80WGPet8": "hYH80WGPet8",
        "https://youtu.be/hYH80WGPet8?t=42": "hYH80WGPet8",
        "https://www.youtube.com/watch?v=hYH80WGPet8&list=PLxyz&index=4": "hYH80WGPet8",
        "https://music.youtube.com/watch?v=hYH80WGPet8": "hYH80WGPet8",
        "https://m.youtube.com/watch?v=hYH80WGPet8": "hYH80WGPet8",
        "https://www.youtube.com/shorts/hYH80WGPet8": "hYH80WGPet8",
        "https://www.youtube.com/live/hYH80WGPet8": "hYH80WGPet8",
        "https://www.youtube.com/embed/hYH80WGPet8": "hYH80WGPet8",
        "  hYH80WGPet8  ": "hYH80WGPet8",
    }
    wrong = [n for link, want in good.items() if sources.youtube_id(link) != want]
    check(f"every shape of link gives the right video ({len(good)} tried)", not wrong,
          "failed: " + ", ".join(wrong) if wrong else "")
    refused = ["https://www.youtube.com/playlist?list=PLabc", "Stephan Moccio Fracture", "",
               "https://example.com/watch?v=hYH80WGPet8x", "https://youtube.com/@someone"]
    still = [t for t in refused if sources.youtube_id(t)]
    check("things that are not a video are refused", not still, "accepted: " + ", ".join(still) if still else "")

    print("\nHow much of it to make")
    import server
    check("no length asked for means all of it", server._check_limit(None) is None)
    check("fifteen minutes is the most it will do", server._check_limit(99999) == sources.MAX_SONG_SECONDS)
    for bad, why in ((5, "too short"), ("abc", "not a number")):
        try:
            server._check_limit(bad)
            check(f"{why} is refused", False, "it was accepted")
        except Exception as e:                                   # noqa: BLE001
            check(f"{why} is refused", True, str(getattr(e, "detail", e))[:60])

    print("\nFading the end instead of stopping dead")
    notes = [mx.Note(t, t + 1.5, 60, 100) for t in [i * 2.0 for i in range(20)]]
    pedal = [(0.0, 64, 127), (10.0, 64, 0), (25.0, 64, 127), (30.0, 67, 127)]
    kept, ped = postproc.fade_out(notes, pedal, end_at=20.0, fade=6.0)
    check("nothing is left after the point you chose", max(n.end for n in kept) <= 20.0,
          f"last note ends at {max(n.end for n in kept):.1f}s")
    check("notes that had not started yet are gone", all(n.start < 20.0 for n in kept))
    faded = [n for n in kept if n.start >= 14.0]
    check("the last few seconds get quieter",
          len(faded) >= 2 and faded[-1].velocity < faded[0].velocity,
          ", ".join(f"{n.start:.0f}s:{n.velocity}" for n in faded))
    check("but never silent", all(n.velocity >= 20 for n in kept))
    check("both pedals are lifted at the end",
          sum(1 for t, number, value in ped if value == 0 and t >= 19.0) >= 1, str(ped[-2:]))
    check("no pedal is left pressed after the end", all(t < 20.0 for t, _n, _v in ped))
    untouched, _ = postproc.fade_out(notes, pedal, end_at=None)
    check("with no length chosen, nothing is changed", len(untouched) == len(notes))
    short, _ = postproc.fade_out(notes, pedal, end_at=4.0, fade=30.0)
    check("a fade longer than the song does not eat it", len(short) >= 1, f"{len(short)} notes kept")

    print("\nA real recording, cut to 20 seconds")
    pm = pretty_midi.PrettyMIDI(initial_tempo=100)
    inst = pretty_midi.Instrument(program=0)
    t = 0.0
    while t < 60:
        inst.notes.append(pretty_midi.Note(100, 60 + int(t) % 12, t, t + 0.45))
        t += 0.5
    pm.instruments.append(inst)
    pm.write(str(PLAY / "long.mid"))
    subprocess.run(["fluidsynth", "-ni", "-q", "-g", "0.8", "-r", "44100",
                    "-F", str(PLAY / "long.wav"), GM, str(PLAY / "long.mid")], check=True, capture_output=True)
    (PLAY / "work").mkdir(parents=True, exist_ok=True)
    (PLAY / "library").mkdir(parents=True, exist_ok=True)
    whole = pipeline.decode_audio(PLAY / "long.wav", PLAY / "whole.wav")
    cut = pipeline.decode_audio(PLAY / "long.wav", PLAY / "cut.wav", seconds=20)
    check("the recording really is a minute long", abs(len(whole) / pipeline.SR - 60) < 5,
          f"{len(whole) / pipeline.SR:.0f}s")
    check("asking for 20 seconds reads 20 seconds", abs(len(cut) / pipeline.SR - 20) < 0.5,
          f"{len(cut) / pipeline.SR:.1f}s")

    job = pipeline.Job(title="Cut Short", mode="piano", source="upload",
                       upload_path=str(PLAY / "long.wav"), duration_hint=60, limit_seconds=20)
    result = pipeline.process(job, PLAY / "work", PLAY / "library", lambda j: None)
    made = PLAY / "library" / result["file"]
    out = mido.MidiFile(made)
    check("a song was made", made.is_file(), result["file"])
    check("it is the length you asked for, not the whole minute", out.length <= 21.5,
          f"{out.length:.1f} seconds")
    # The file's own length includes the end marker, so check the notes themselves: not one of
    # them may begin after the point that was asked for.
    clock, last_start, past = 0, 0.0, 0
    tempo = 500000
    for msg in mido.merge_tracks(out.tracks):
        clock += msg.time
        when = mido.tick2second(clock, out.ticks_per_beat, tempo)
        if msg.type == "set_tempo":
            tempo = msg.tempo
        if msg.type == "note_on" and msg.velocity > 0:
            last_start = max(last_start, when)
            past += when > 20.2
    check("no note starts after the point you chose", past == 0,
          f"last note starts at {last_start:.1f}s, {past} past the line")
    heard = []
    clock = 0
    for msg in mido.merge_tracks(out.tracks):
        clock += msg.time
        if msg.type == "note_on" and msg.velocity > 0:
            heard.append((mido.tick2second(clock, out.ticks_per_beat, 500000), msg.velocity))
    if heard:
        early = [v for t, v in heard if t < out.length - 8]
        late = [v for t, v in heard if t >= out.length - 4]
        check("the ending is quieter than the rest",
              bool(late) and bool(early) and sum(late) / len(late) < sum(early) / len(early),
              f"before {sum(early) / max(1, len(early)):.0f}, at the end {sum(late) / max(1, len(late)):.0f}")

    print()
    print(f"{len(passed)} passed, {len(failed)} failed")
    for f in failed:
        print(f"  FAILED: {f}")
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(PLAY, ignore_errors=True)
    sys.exit(code)
