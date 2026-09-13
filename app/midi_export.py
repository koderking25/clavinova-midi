"""Standard MIDI File writer tuned for Yamaha Clavinova playback.

Format 0 (a single track holding every channel) because every Clavinova that
reads SMF plays it. The file starts with a GM System On reset, sets each
instrument half a beat later, and holds the first note until one beat in so
the reset has settled before anything sounds.
"""
from dataclasses import dataclass, field

import mido

PPQ = 480
SETUP_TICK = PPQ // 2
LEAD_IN_TICKS = PPQ


@dataclass
class Note:
    start: float
    end: float
    pitch: int
    velocity: int


@dataclass
class Part:
    name: str
    channel: int                                   # 0-15, 9 is the drum channel
    program: int = 0                               # GM program 0-127
    notes: list = field(default_factory=list)      # list[Note]
    pedal: list = field(default_factory=list)      # list[(time_s, value 0-127)]
    volume: int = 100


def ascii_text(s, limit=60):
    import unicodedata
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    s = " ".join("".join(c for c in s if 32 <= ord(c) < 127).split())
    return s[:limit].strip() or "Song"


def _tick(t, bpm):
    return max(0, int(round(float(t) * bpm / 60.0 * PPQ)))


def part_ticks(part, bpm):
    """Notes as (on_tick, off_tick, pitch, velocity), with same-pitch overlaps resolved.

    A note-off for a pitch silences that pitch on the channel, so if two notes of
    the same pitch overlap, the first must end where the second begins.
    """
    by_pitch = {}
    for n in part.notes:
        pitch = int(n.pitch)
        if not 0 <= pitch <= 127:
            continue
        on = LEAD_IN_TICKS + _tick(n.start, bpm)
        off = LEAD_IN_TICKS + _tick(n.end, bpm)
        if off <= on:
            off = on + 1
        vel = int(min(127, max(1, round(n.velocity))))
        by_pitch.setdefault(pitch, []).append([on, off, pitch, vel])
    out = []
    for notes in by_pitch.values():
        notes.sort(key=lambda x: (x[0], -x[3]))
        kept = []
        for n in notes:
            if kept and kept[-1][0] == n[0]:
                continue                                  # duplicate onset, keep the louder one
            if kept and kept[-1][1] > n[0]:
                kept[-1][1] = n[0]
            kept.append(n)
        out += [tuple(n) for n in kept if n[1] > n[0]]
    return sorted(out)


def write_smf(parts, path, bpm=120.0, title="Song"):
    bpm = float(min(240.0, max(40.0, bpm)))
    ev = []                                            # (tick, priority, seq, message)

    def add(tick, prio, msg):
        ev.append((tick, prio, len(ev), msg))

    add(0, 0, mido.MetaMessage("track_name", name=ascii_text(title)))
    add(0, 0, mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(bpm)))
    add(0, 0, mido.MetaMessage("time_signature", numerator=4, denominator=4))
    add(0, 1, mido.Message("sysex", data=[0x7E, 0x7F, 0x09, 0x01]))        # GM System On
    for part in parts:
        ch = part.channel
        if ch != 9:
            add(SETUP_TICK, 2, mido.Message("control_change", channel=ch, control=0, value=0))
            add(SETUP_TICK, 2, mido.Message("control_change", channel=ch, control=32, value=0))
        add(SETUP_TICK, 3, mido.Message("program_change", channel=ch, program=int(part.program) & 127))
        add(SETUP_TICK, 4, mido.Message("control_change", channel=ch, control=7, value=int(part.volume)))
        add(SETUP_TICK, 4, mido.Message("control_change", channel=ch, control=11, value=127))
        add(SETUP_TICK, 4, mido.Message("control_change", channel=ch, control=64, value=0))
        add(SETUP_TICK, 4, mido.Message("control_change", channel=ch, control=67, value=0))
        for event in part.pedal:
            # (time, value) is the sustain pedal; (time, controller, value) names the pedal,
            # which is how the soft pedal (67) travels alongside it.
            t, cc, val = (event[0], 64, event[1]) if len(event) == 2 else event
            add(LEAD_IN_TICKS + _tick(t, bpm), 5,
                mido.Message("control_change", channel=ch, control=int(cc), value=int(min(127, max(0, val)))))
        for on, off, pitch, vel in part_ticks(part, bpm):
            add(on, 7, mido.Message("note_on", channel=ch, note=pitch, velocity=vel))
            add(off, 6, mido.Message("note_off", channel=ch, note=pitch, velocity=0))
    last = max((e[0] for e in ev), default=0)
    ev.sort(key=lambda e: (e[0], e[1], e[2]))
    track = mido.MidiTrack()
    now = 0
    for tick, _, _, msg in ev:
        track.append(msg.copy(time=tick - now))
        now = tick
    track.append(mido.MetaMessage("end_of_track", time=last + PPQ - now))
    mid = mido.MidiFile(type=0, ticks_per_beat=PPQ)
    mid.tracks.append(track)
    mid.save(path)
    return path


def verify_smf(path, parts, bpm):
    """Read the file back and check it holds exactly the notes we meant to write.

    Returns a list of problems; empty means the file is good.
    """
    problems = []
    bpm = float(min(240.0, max(40.0, bpm)))
    try:
        mid = mido.MidiFile(path)
    except Exception as e:  # noqa: BLE001
        return [f"file does not parse as MIDI: {e}"]
    if mid.type != 0 or len(mid.tracks) != 1:
        problems.append(f"expected format 0 with one track, got type {mid.type} with {len(mid.tracks)} tracks")
    tick, sounding, got = 0, {}, {}
    sysex_first = None
    for msg in mid.tracks[0]:
        tick += msg.time
        if msg.type == "sysex" and sysex_first is None:
            sysex_first = tick
        if msg.type == "note_on" and msg.velocity > 0:
            key = (msg.channel, msg.note)
            if key in sounding:
                problems.append(f"overlapping note {key} at tick {tick}")
            sounding[key] = (tick, msg.velocity)
        elif msg.type == "note_off" or (msg.type == "note_on" and msg.velocity == 0):
            key = (msg.channel, msg.note)
            if key in sounding:
                on, vel = sounding.pop(key)
                got.setdefault(msg.channel, []).append((on, tick, msg.note, vel))
    if sounding:
        problems.append(f"{len(sounding)} notes never switched off")
    if sysex_first != 0:
        problems.append("GM reset is not the first event")
    for part in parts:
        want = part_ticks(part, bpm)
        have = sorted(got.get(part.channel, []))
        if [w[:3] for w in want] != [h[:3] for h in have]:
            problems.append(f"{part.name}: wrote {len(want)} notes but read back {len(have)} (or timings differ)")
    return problems
