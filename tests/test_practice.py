"""Practice versions: slower, transposed, with a count-in, and made easier.

    .venv/bin/python tests/test_practice.py

Known answers throughout: a song is built here with notes chosen on purpose, each tool is run on
it, and the result is read back and compared note by note. It then does the same to a real song
from the songs folder, read only, to be sure it works on files Midify did not make.
"""
import shutil
import sys
import tempfile
from pathlib import Path

import mido

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
import practice  # noqa: E402

PLAY = Path(tempfile.mkdtemp(prefix="practice-"))
passed, failed = [], []


def check(name, ok, detail=""):
    (passed if ok else failed).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{(': ' + detail) if detail else ''}")


def notes_of(path):
    """(start tick, pitch, velocity, channel) for every note in a file."""
    mid = mido.MidiFile(path)
    out, t = [], 0
    for msg in mido.merge_tracks(mid.tracks):
        t += msg.time
        if msg.type == "note_on" and msg.velocity > 0:
            out.append((t, msg.note, msg.velocity, msg.channel))
    return out


def seconds_of(path):
    return mido.MidiFile(path).length


def a_song(path, bpm=120):
    """A tune over block chords, so there is something to thin out and a melody to protect."""
    mid = mido.MidiFile(ticks_per_beat=480)
    track = mido.MidiTrack()
    track.append(mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(bpm), time=0))
    track.append(mido.MetaMessage("time_signature", numerator=4, denominator=4, time=0))
    tune = [72, 74, 76, 77, 79, 77, 76, 74]
    chord = [48, 52, 55, 60, 64]
    for bar in range(8):
        for beat in range(4):
            melody = tune[(bar * 4 + beat) % len(tune)]
            on = [melody] + chord
            for i, p in enumerate(on):
                track.append(mido.Message("note_on", note=p, velocity=90 if p == melody else 64,
                                          channel=0, time=0))
            for i, p in enumerate(on):
                track.append(mido.Message("note_off", note=p, velocity=0, channel=0,
                                          time=460 if i == 0 else 0))
            track.append(mido.Message("note_on", note=36, velocity=1, channel=0, time=20))
            track.append(mido.Message("note_off", note=36, velocity=0, channel=0, time=0))
    mid.tracks.append(track)
    mid.save(path)
    return path


def main():
    song = a_song(PLAY / "song.mid")
    original, original_len = notes_of(song), seconds_of(song)
    print(f"A test song: {len(original)} notes, {original_len:.1f} seconds, 120 bpm\n")

    print("Slower")
    out = PLAY / "slow.mid"
    said = practice.slower(song, out, 70)
    slow = notes_of(out)
    check("every note is still there", len(slow) == len(original), f"{len(slow)} notes")
    check("the written positions are untouched", [n[0] for n in slow] == [n[0] for n in original])
    ratio = seconds_of(out) / original_len
    check("it really plays slower, by the amount asked", abs(ratio - 100 / 70) < 0.02,
          f"{seconds_of(out):.1f}s instead of {original_len:.1f}s, {ratio:.2f} times as long")
    check("it says what it did", "70%" in said, said)
    for bad in (0, 500):
        try:
            practice.slower(song, PLAY / "x.mid", bad)
            check(f"a silly speed ({bad}%) is refused", False, "it accepted it")
        except ValueError as e:
            check(f"a silly speed ({bad}%) is refused", True, str(e))

    print("\nIn another key")
    out = PLAY / "up.mid"
    said = practice.transpose(song, out, 3)
    up = notes_of(out)
    check("every note moved by exactly three semitones",
          [n[1] for n in up] == [n[1] + 3 for n in original], said)
    check("nothing runs off the keyboard", all(21 <= n[1] <= 108 for n in up))
    check("the timing is unchanged", [n[0] for n in up] == [n[0] for n in original])
    edge = a_song(PLAY / "edge.mid")
    m = mido.MidiFile(edge)
    for msg in m.tracks[0]:
        if msg.type in ("note_on", "note_off"):
            msg.note = 106                                   # right at the top of the keyboard
    m.save(edge)
    said = practice.transpose(edge, PLAY / "edgeup.mid", 12)
    check("notes that would fall off the top are brought back",
          all(21 <= n[1] <= 108 for n in notes_of(PLAY / "edgeup.mid")), said)

    print("\nCount-in")
    out = PLAY / "count.mid"
    said = practice.count_in(song, out, 4)
    counted = notes_of(out)
    clicks = [n for n in counted if n[3] == practice.CLICK_CHANNEL]
    music = [n for n in counted if n[3] != practice.CLICK_CHANNEL]
    check("there are four clicks", len(clicks) == 4, f"{len(clicks)} clicks")
    check("the first click is louder than the rest", clicks[0][1] != clicks[1][1])
    check("the song itself is unchanged, just later",
          [(n[1], n[2]) for n in music] == [(n[1], n[2]) for n in original])
    shift = music[0][0] - original[0][0]
    check("it starts exactly four beats later", shift == 4 * 480, f"moved by {shift} ticks")
    check("the clicks come before the song", max(n[0] for n in clicks) <= music[0][0])
    check("it still plays at the right speed",
          abs(seconds_of(out) - (original_len + 2.0)) < 0.25, f"{seconds_of(out):.1f}s")

    print("\nMade easier")
    out = PLAY / "easy.mid"
    said = practice.simplify(song, out, keep=3)
    easy = notes_of(out)
    check("there are fewer notes", len(easy) < len(original), said)

    def melody_line(ns, real_moments_only=False):
        """The top note at each moment: the tune.

        A moment holding nothing but one faint, ultra short note is filler, not a tune note, so it
        is not counted when deciding whether the tune survived."""
        by_time, how_many = {}, {}
        for t, p, v, c in ns:
            by_time[t] = max(by_time.get(t, 0), p)
            how_many[t] = how_many.get(t, 0) + 1
        if real_moments_only:
            by_time = {t: p for t, p in by_time.items() if how_many[t] > 1}
        return by_time

    before_tune, after_tune = melody_line(original, real_moments_only=True), melody_line(easy)
    kept_tune = sum(1 for t, p in before_tune.items() if after_tune.get(t) == p)
    check("every note of the tune is still there",
          kept_tune == len(before_tune), f"{kept_tune} of {len(before_tune)} moments")
    most = max(sum(1 for n in easy if n[0] == t) for t in {n[0] for n in easy})
    check("never more than three notes at once", most <= 3, f"the thickest chord has {most}")
    check("no note was invented", set(n[1] for n in easy) <= set(n[1] for n in original))
    check("the song is still the same length",
          abs(seconds_of(out) - original_len) < 0.2, f"{seconds_of(out):.1f}s vs {original_len:.1f}s")
    check("the quiet filler notes went first", 36 not in [n[1] for n in easy])
    check("it is a format 0 file, the kind every piano reads", mido.MidiFile(out).type == 0)

    print("\nOn a format 0 file, which is what Midify itself writes")
    # Every song Midify saves is format 0: one track, no room to add another. A count-in that
    # appends a track cannot be saved at all, and this went unnoticed because the test song above
    # is format 1.
    flat = PLAY / "flat.mid"
    one = mido.MidiFile(a_song(PLAY / "src0.mid"))
    merged = mido.MidiFile(type=0, ticks_per_beat=one.ticks_per_beat)
    merged.tracks.append(mido.merge_tracks(one.tracks))
    merged.save(flat)
    check("the test file really is format 0", mido.MidiFile(flat).type == 0)
    flat_notes, flat_len = notes_of(flat), seconds_of(flat)
    for kind, call in (("count-in", lambda o: practice.count_in(flat, o, 4)),
                       ("slower", lambda o: practice.slower(flat, o, 70)),
                       ("transposed", lambda o: practice.transpose(flat, o, 3)),
                       ("easier", lambda o: practice.simplify(flat, o))):
        out = PLAY / f"flat-{kind}.mid"
        try:
            said = call(out)
            ok = out.is_file() and len(notes_of(out)) > 0
            check(f"{kind} works on a format 0 song", ok, said)
        except Exception as e:                                  # noqa: BLE001
            check(f"{kind} works on a format 0 song", False, f"{type(e).__name__}: {e}")
    counted0 = notes_of(PLAY / "flat-count-in.mid")
    music0 = [n for n in counted0 if n[3] != practice.CLICK_CHANNEL]
    check("the count-in still has four clicks",
          len([n for n in counted0 if n[3] == practice.CLICK_CHANNEL]) == 4)
    check("and the song still starts four beats later",
          music0[0][0] - flat_notes[0][0] == 4 * 480, f"moved {music0[0][0] - flat_notes[0][0]} ticks")
    check("and it is still a format 0 file the piano will read",
          mido.MidiFile(PLAY / "flat-count-in.mid").type == 0)

    print("\nOn a real song from your songs folder (read only)")
    # Skip the hidden "._" files macOS leaves beside real ones: they are not MIDI files, and the
    # app's own song list skips them too.
    real = sorted(f for f in (Path.home() / "Music" / "Midify").glob("*.mid")
                  if not f.name.startswith("."))
    if not real:
        print("  (no songs in the folder, skipping)")
    else:
        src = PLAY / "real.mid"
        shutil.copyfile(real[0], src)
        before = notes_of(src)
        print(f"  using {real[0].name}: {len(before)} notes, {seconds_of(src):.0f} seconds")
        s1 = practice.slower(src, PLAY / "r-slow.mid", 60)
        check("slower works on a real song",
              len(notes_of(PLAY / "r-slow.mid")) == len(before) and seconds_of(PLAY / "r-slow.mid") > seconds_of(src), s1)
        s2 = practice.transpose(src, PLAY / "r-up.mid", -2)
        moved = notes_of(PLAY / "r-up.mid")
        check("transposing works on a real song",
              all(21 <= n[1] <= 108 for n in moved) and len(moved) == len(before), s2)
        s3 = practice.simplify(src, PLAY / "r-easy.mid")
        after = notes_of(PLAY / "r-easy.mid")
        check("making it easier works on a real song", 0 < len(after) <= len(before), s3)
        check("and it does not make the song longer",
              seconds_of(PLAY / "r-easy.mid") <= seconds_of(src) + 0.5,
              f"{seconds_of(PLAY / 'r-easy.mid'):.0f}s vs {seconds_of(src):.0f}s")
        check("the real song file itself was not touched", notes_of(src) == before)

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
