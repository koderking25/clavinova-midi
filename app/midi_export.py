"""Standard MIDI File writer tuned for digital piano playback.

Format 0 (a single track holding every channel) because every digital piano and
keyboard that reads MIDI files plays it. The file starts with a GM System On reset, sets each
instrument half a beat later, and holds the first note until one beat in so
the reset has settled before anything sounds.
"""
import bisect
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


def clamp_bpm(bpm):
    return float(min(240.0, max(40.0, bpm)))


class TimeGrid:
    """Turns seconds in the recording into MIDI ticks.

    Without beats: one fixed tempo, as before. With beats (the times the beat falls in the
    performance): every beat becomes exactly one quarter note, and the tempo changes on each
    beat to match how long that beat really lasted. The notes still sound at exactly the
    moments they were played, but they now sit in the right place in the bar, so the
    piano can draw a readable score. `downbeat` (0 to 3) says which of the first four
    beats starts a bar, so bar lines land where the music's bars are.
    """

    def __init__(self, bpm, beats=None, downbeat=0, beats_per_bar=4):
        self.bpm = clamp_bpm(bpm)
        self.per_bar = 3 if beats_per_bar == 3 else 4
        self.beats = None
        if beats is not None and len(beats) >= 8:
            b = [float(v) for v in beats]
            if all(b2 - b1 >= 0.2 for b1, b2 in zip(b, b[1:])):      # never faster than 300 BPM
                self.beats = b
        if self.beats:
            b = self.beats
            first = b[1] - b[0]
            need = LEAD_IN_TICKS + b[0] / first * PPQ             # song start stays one beat in
            bar = self.per_bar * PPQ
            k = int(downbeat) % self.per_bar
            self.t0 = int(-(-(need + k * PPQ) // bar) * bar - k * PPQ)   # smallest bar-aligned start
        else:
            self.t0 = None

    def tick(self, t):
        t = float(t)
        if not self.beats:
            return LEAD_IN_TICKS + max(0, int(round(t * self.bpm / 60.0 * PPQ)))
        b = self.beats
        if t < b[0]:
            x = self.t0 - (b[0] - t) / (b[1] - b[0]) * PPQ
        elif t >= b[-1]:
            x = self.t0 + (len(b) - 1) * PPQ + (t - b[-1]) / (b[-1] - b[-2]) * PPQ
        else:
            i = bisect.bisect_right(b, t) - 1
            x = self.t0 + (i + (t - b[i]) / (b[i + 1] - b[i])) * PPQ
        return max(0, int(round(x)))

    def tempos(self):
        """(tick, microseconds per quarter note) for every tempo change, the first at tick 0."""
        if not self.beats:
            return [(0, mido.bpm2tempo(self.bpm))]
        b = self.beats
        out = [(0, int(round((b[1] - b[0]) * 1e6)))]
        for i in range(1, len(b) - 1):
            out.append((self.t0 + i * PPQ, int(round((b[i + 1] - b[i]) * 1e6))))
        return out

    def first_bpm(self):
        return round(60e6 / self.tempos()[0][1], 1)


def _grid(grid):
    return grid if isinstance(grid, TimeGrid) else TimeGrid(grid)


def part_ticks(part, grid):
    """Notes as (on_tick, off_tick, pitch, velocity), with same-pitch overlaps resolved.

    A note-off for a pitch silences that pitch on the channel, so if two notes of
    the same pitch overlap, the first must end where the second begins.
    """
    grid = _grid(grid)
    by_pitch = {}
    for n in part.notes:
        pitch = int(n.pitch)
        if not 0 <= pitch <= 127:
            continue
        on = grid.tick(n.start)
        off = grid.tick(n.end)
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


def write_smf(parts, path, bpm=120.0, title="Song", beats=None, downbeat=0, beats_per_bar=4):
    grid = TimeGrid(bpm, beats, downbeat, beats_per_bar)
    ev = []                                            # (tick, priority, seq, message)

    def add(tick, prio, msg):
        ev.append((tick, prio, len(ev), msg))

    add(0, 0, mido.MetaMessage("track_name", name=ascii_text(title)))
    for tick, tempo in grid.tempos():
        add(tick, 0, mido.MetaMessage("set_tempo", tempo=tempo))
    add(0, 0, mido.MetaMessage("time_signature", numerator=grid.per_bar, denominator=4))
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
            add(grid.tick(t), 5,
                mido.Message("control_change", channel=ch, control=int(cc), value=int(min(127, max(0, val)))))
        for on, off, pitch, vel in part_ticks(part, grid):
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


TIMING_TOLERANCE = 0.002          # seconds: every note must sound within 2 ms of when it was played


def verify_smf(path, parts, bpm, beats=None, downbeat=0, beats_per_bar=4):
    """Read the file back and check it holds exactly the notes we meant to write.

    Also plays the file's own tempo changes forward and checks every note would sound within
    2 ms of the moment it was played, so a beat grid can never change how a song feels.
    Returns a list of problems; empty means the file is good.
    """
    problems = []
    grid = TimeGrid(bpm, beats, downbeat, beats_per_bar)
    try:
        mid = mido.MidiFile(path)
    except Exception as e:  # noqa: BLE001
        return [f"file does not parse as MIDI: {e}"]
    if mid.type != 0 or len(mid.tracks) != 1:
        problems.append(f"expected format 0 with one track, got type {mid.type} with {len(mid.tracks)} tracks")
    tick, sounding, got = 0, {}, {}
    sysex_first = None
    clock, tempo, on_second = 0.0, 500000, {}           # playback seconds of every note-on tick
    for msg in mid.tracks[0]:
        clock += msg.time * tempo / 1e6 / mid.ticks_per_beat
        tick += msg.time
        if msg.type == "set_tempo":
            tempo = msg.tempo
        if msg.type == "note_on" and msg.velocity > 0:
            on_second.setdefault(tick, clock)
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
        want = part_ticks(part, grid)
        have = sorted(got.get(part.channel, []))
        if [w[:3] for w in want] != [h[:3] for h in have]:
            problems.append(f"{part.name}: wrote {len(want)} notes but read back {len(have)} (or timings differ)")
    # Timing: where each note lands in playback, measured from where the song itself starts.
    start_tick = grid.tick(0.0)
    if on_second:
        ticks = sorted(on_second)
        seconds = [on_second[t] for t in ticks]
        base = _second_at(start_tick, ticks, seconds, grid)
        worst = 0.0
        for part in parts:
            for n in part.notes:
                t = grid.tick(n.start)
                if t in on_second and 0 <= int(n.pitch) <= 127:
                    worst = max(worst, abs((on_second[t] - base) - float(n.start)))
        if worst > TIMING_TOLERANCE:
            problems.append(f"a note would sound {worst * 1000:.1f} ms away from when it was played")
    return problems


def _second_at(tick, ticks, seconds, grid):
    """Playback second of any tick, using the grid's own tempo changes."""
    clock, last_tick, tempo = 0.0, 0, None
    for change_tick, change_tempo in grid.tempos():
        if change_tick > tick:
            break
        if tempo is not None:
            clock += (change_tick - last_tick) * tempo / 1e6 / PPQ
        last_tick, tempo = change_tick, change_tempo
    return clock + (tick - last_tick) * tempo / 1e6 / PPQ
