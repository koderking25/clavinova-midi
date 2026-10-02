"""Practice versions of a song: slower, in another key, with a count-in, or made easier to play.

These work on any MIDI file, including hand-made ones downloaded from BitMidi, not only songs
Midify made. Nothing here needs the AI or any disk space worth speaking of: a MIDI file is tiny.

Slower changes the tempo, not the notes, so the bar lines and the written rhythm stay exactly as
they were. That matters on a digital piano that shows a score.
"""
import copy

import mido

LOWEST, HIGHEST = 21, 108          # a full 88-key piano, A0 to C8
CLICK_CHANNEL = 9                  # channel 10 in the usual numbering: the drum channel
CLICK, CLICK_ACCENT = 37, 76       # side stick, and a high wood block for beat one


def _is_note_on(msg):
    return msg.type == "note_on" and msg.velocity > 0


def _is_note_off(msg):
    return msg.type == "note_off" or (msg.type == "note_on" and msg.velocity == 0)


def slower(path_in, path_out, percent):
    """Play at a percentage of the written speed. 70 means seven tenths as fast.

    The notes keep their written positions; only the tempo changes, so the score reads the same."""
    if not 20 <= percent <= 200:
        raise ValueError("Choose a speed between 20 and 200 percent.")
    mid = mido.MidiFile(path_in)
    found = 0
    for track in mid.tracks:
        for msg in track:
            if msg.type == "set_tempo":
                msg.tempo = int(msg.tempo * 100 / percent)
                found += 1
    if not found:                                   # a file with no tempo at all plays at 120
        mid.tracks[0].insert(0, mido.MetaMessage("set_tempo", tempo=int(500000 * 100 / percent), time=0))
    mid.save(path_out)
    return f"Saved at {percent}% speed. The notes and bar lines are unchanged."


def transpose(path_in, path_out, semitones):
    """Move the whole song up or down, keeping drums where they are."""
    if not -12 <= semitones <= 12:
        raise ValueError("Choose between 12 semitones down and 12 up.")
    mid = mido.MidiFile(path_in)
    moved, out_of_range = 0, 0
    for track in mid.tracks:
        for msg in track:
            if msg.type in ("note_on", "note_off") and getattr(msg, "channel", 0) != CLICK_CHANNEL:
                new = msg.note + semitones
                if new < LOWEST or new > HIGHEST:
                    new = msg.note + (12 if new < LOWEST else -12) * ((abs(semitones) // 12) + 1)
                    new = max(LOWEST, min(HIGHEST, new))
                    out_of_range += 1
                msg.note = new
                moved += 1
    mid.save(path_out)
    note = f" {out_of_range} note(s) ran off the end of the keyboard and were moved an octave." if out_of_range else ""
    way = "up" if semitones > 0 else "down"
    return f"Saved {abs(semitones)} semitone(s) {way}.{note}"


def count_in(path_in, path_out, beats=4):
    """Put a few clicks in front, so you know when to come in.

    Midify's own songs are format 0, which means one track and no room to add another, so the
    clicks are woven into the track that is already there (tests/test_practice.py)."""
    if not 1 <= beats <= 8:
        raise ValueError("A count-in can be 1 to 8 beats.")
    mid = mido.MidiFile(path_in)
    ticks = mid.ticks_per_beat
    offset = beats * ticks

    def clicks():
        out = []
        for beat in range(beats):
            pitch = CLICK_ACCENT if beat == 0 else CLICK
            at = beat * ticks
            out.append((at, 1, mido.Message("note_on", channel=CLICK_CHANNEL, note=pitch, velocity=100)))
            out.append((at + 30, 0, mido.Message("note_off", channel=CLICK_CHANNEL, note=pitch, velocity=0)))
        return out

    if mid.type == 0:
        events, t, seen_note = [], 0, False
        for msg in mid.tracks[0]:
            t += msg.time
            # Tempo and key stay at the very start; everything that sounds moves back by the count-in.
            delay = offset if (seen_note or msg.type in ("note_on", "note_off")) else 0
            if msg.type in ("note_on", "note_off"):
                seen_note = True
            events.append((t + delay, 1 if msg.type == "note_on" else 0, msg.copy()))
        events.extend(clicks())
        events.sort(key=lambda e: (e[0], e[1]))
        track = mido.MidiTrack()
        last = 0
        for when, _kind, msg in events:
            msg.time = when - last
            last = when
            track.append(msg)
        out = mido.MidiFile(type=0, ticks_per_beat=ticks)
        out.tracks.append(track)
        out.save(path_out)
        return f"Saved with a {beats} beat count-in before the song starts."

    extra = mido.MidiTrack()
    last = 0
    for when, _kind, msg in sorted(clicks(), key=lambda e: (e[0], e[1])):
        msg.time = when - last
        last = when
        extra.append(msg)
    for track in mid.tracks:
        for msg in track:
            if msg.type in ("note_on", "note_off"):
                msg.time += offset
                break
    mid.tracks.append(extra)
    mid.save(path_out)
    return f"Saved with a {beats} beat count-in before the song starts."


def _events_in_seconds(mid):
    """Every note as (start_tick, end_tick, pitch, velocity, channel), ignoring drums."""
    notes, open_notes, t = [], {}, 0
    for msg in mido.merge_tracks(mid.tracks):
        t += msg.time
        ch = getattr(msg, "channel", 0)
        if ch == CLICK_CHANNEL:
            continue
        if _is_note_on(msg):
            open_notes.setdefault((ch, msg.note), []).append((t, msg.velocity))
        elif _is_note_off(msg):
            stack = open_notes.get((ch, msg.note))
            if stack:
                start, vel = stack.pop(0)
                notes.append([start, t, msg.note, vel, ch])
    return sorted(notes, key=lambda n: (n[0], n[2]))


def simplify(path_in, path_out, keep=3):
    """An easier version to play: the tune in full, with the accompaniment thinned out.

    Every melody note (the top line) is kept exactly as it is. Underneath it, chords are reduced to
    at most `keep` notes at a time, preferring the lowest note (the bass) and the notes furthest
    from the ones already kept, and very short filler notes are dropped."""
    if not 2 <= keep <= 6:
        raise ValueError("Keep between 2 and 6 notes at a time.")
    mid = mido.MidiFile(path_in)
    notes = _events_in_seconds(mid)
    if not notes:
        raise ValueError("That file has no notes in it.")
    ticks = mid.ticks_per_beat
    short = ticks // 12                                   # a thirty-second note at the written speed
    window = max(1, ticks // 8)

    groups, current = [], [notes[0]]
    for n in notes[1:]:
        if n[0] - current[0][0] <= window:
            current.append(n)
        else:
            groups.append(current)
            current = [n]
    groups.append(current)

    # A note that is both very short and much quieter than the rest is filler, not melody. Without
    # this, a stray ghost note alone in its moment becomes "the tune" and survives (tests/test_practice.py).
    loudness = sorted(n[3] for n in notes)
    typical = loudness[len(loudness) // 2]
    faint = max(12, int(typical * 0.35))

    def is_filler(n):
        return (n[1] - n[0]) <= short and n[3] < faint

    kept = []
    for group in groups:
        group.sort(key=lambda n: n[2])
        real = [n for n in group if not is_filler(n)]
        if not real:
            continue                                       # nothing here but filler
        group = real
        melody = group[-1]                                 # the top note is the tune
        chosen = [melody]
        rest = [n for n in group[:-1] if (n[1] - n[0]) > short]
        if rest:
            chosen.append(rest[0])                         # the bass under it
            for n in rest[1:]:
                if len(chosen) >= keep:
                    break
                if all(abs(n[2] - c[2]) >= 3 for c in chosen):     # skip notes that add nothing
                    chosen.append(n)
        kept.extend(chosen)

    # Format 0, one track, exactly as Midify saves its own songs: every digital piano reads it.
    track = mido.MidiTrack()
    for msg in mido.merge_tracks(mid.tracks):
        if msg.is_meta and msg.type in ("set_tempo", "time_signature", "key_signature", "track_name"):
            track.append(msg.copy(time=0))

    events = []
    for start, end, pitch, vel, ch in kept:
        events.append((start, 1, mido.Message("note_on", note=pitch, velocity=vel, channel=ch, time=0)))
        events.append((max(end, start + 1), 0, mido.Message("note_off", note=pitch, velocity=0, channel=ch, time=0)))
    events.sort(key=lambda e: (e[0], e[1]))
    last = 0
    for when, _kind, msg in events:
        msg.time = when - last
        last = when
        track.append(msg)
    out = mido.MidiFile(type=0, ticks_per_beat=ticks)
    out.tracks.append(track)
    out.save(path_out)
    removed = len(notes) - len(kept)
    share = round(100 * removed / max(1, len(notes)))
    return (f"Saved an easier version: {len(kept)} notes instead of {len(notes)}, {share}% fewer. "
            "The tune itself is untouched.")
