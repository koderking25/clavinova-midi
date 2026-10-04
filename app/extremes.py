"""Reaching all 88 keys, A0 to C8, without making songs slower.

The listeners have a sweet spot in the middle of the keyboard. On a chromatic run of every key
(tests/test_range.py) the piano model reaches 82 of 88: it misses A0 at the bottom and G#7 to C8 at
the top. Basic Pitch misses the bottom six and C8.

The fix costs no disk space: play the song to the model an octave shifted, so the extreme keys land
where it hears best, then shift the notes back. An octave up recovers the bottom, an octave down
recovers the top.

The expensive part is deciding when to bother. Two spectral detectors were tried and both failed
against controls, because the harmonics of ordinary notes sit in the extreme bands: a rock drum
track had more low energy than a song actually playing A0, and a middle-of-the-keyboard run had a
sharper tone above 2.3 kHz than a real C8. So the gate is the transcription itself. If the straight
pass found nothing near the bottom or top of the keyboard, there is nothing out there to look for.
When it did, only those few seconds are heard again, not the whole song.
"""
LOW_ZONE = 26            # D1 and below: measured as the keys the straight pass cannot reach.
#                          Wider zones (a whole bottom octave) add notes the straight pass can
#                          already hear, which is work and risk for nothing.
HIGH_ZONE = 100          # E7 and above: same measurement at the top of the keyboard.
# Two different questions, and they need two different answers:
#  - which keys are worth ACCEPTING from a second listen (the zones above: only what the straight
#    pass cannot reach, so nothing it can already hear is second-guessed), and
#  - when it is worth LISTENING AGAIN at all (below: a note anywhere near the end of the keyboard
#    is the clue, because the keys we are hunting are the ones the straight pass cannot report).
# Tying these together broke it: with the gate as tight as the zone, a song playing A0 and A1 was
# heard as A1 only, which did not reach the gate, so A0 was never looked for.
LOW_GATE = 33            # a straight-pass note at A1 or below is a reason to look under it
HIGH_GATE = 90           # F#6 or above is a reason to look above it
PAD = 4.0                # seconds either side. Generous on purpose: the keys the straight pass cannot
#                          hear are played beyond its last audible note, so a tight window misses
#                          exactly what we are looking for (it stopped at 29.7s while C8 was at 30.4s).
MAX_EXTRA = 20.0         # at most this many seconds of extra listening per end of the keyboard.
#                          At 60 this doubled the time on real four minute songs (+99% on a rock
#                          recording) for about fifty notes. Twenty keeps the gain and a third of
#                          the cost, because extreme notes cluster rather than spread.


def wanted(notes):
    """Which zones the straight pass gives a reason to look into."""
    return {"low": any(n.pitch <= LOW_GATE for n in notes),
            "high": any(n.pitch >= HIGH_GATE for n in notes)}


# Where to listen in the band, once the gate has decided it is worth listening at all.
LOUD_BAND = {"low": (24.0, 70.0), "high": (2300.0, 5000.0)}


def loud_moments(audio, sr, zone, most=12):
    """The moments with the most energy at that end of the keyboard, loudest first.

    Used only to choose where to listen again, never to decide whether to: that call belongs to
    wanted(), because energy in these bands is also where ordinary notes keep their harmonics.
    Picking a moment wrongly costs a few seconds; it cannot add a wrong note, because a note is
    only kept when it lands in the zone and the straight pass does not already have it.

    Reported by a listener who found Fracture missing its low D: the piano model heard three notes
    near the bottom, all in one place, so the second listen looked there and nowhere else, while
    the D1s at 14s and 3:13 went unexamined."""
    import numpy as np
    mono = audio.mean(axis=1) if audio.ndim > 1 else audio
    frame, hop = 16384, sr // 4
    if len(mono) < frame * 2:
        return []
    lo, hi = LOUD_BAND[zone]
    scores = []
    window = np.hanning(frame)
    for start in range(0, len(mono) - frame, hop):
        seg = mono[start:start + frame] * window
        spec = np.abs(np.fft.rfft(seg))
        freqs = np.fft.rfftfreq(frame, 1 / sr)
        band = (freqs >= lo) & (freqs <= hi)
        scores.append((float(spec[band].sum() / (spec.sum() + 1e-9)), start / sr))
    if not scores:
        return []
    scores.sort(reverse=True)
    picked = []
    for _score, when in scores:
        if all(abs(when - already) > PAD * 2 for already in picked):
            picked.append(when)
        if len(picked) >= most:
            break
    return picked


def segments(notes, zone, song_length, audio=None, sr=None):
    """The moments worth hearing again, best first.

    What the straight pass heard near the edge of the keyboard comes first, because that is the
    model's own evidence. Then the places where that end of the keyboard is loudest in the
    recording. Ordering matters: there is only so much extra listening allowed, and the loudest
    moment in a song is often near the end, where a first-come ordering would never reach it."""
    low = zone == "low"
    ranked = [(0, n.start) for n in notes
              if (n.pitch <= LOW_GATE if low else n.pitch >= HIGH_GATE)]
    if audio is not None and sr:
        ranked += [(i + 1, when) for i, when in enumerate(loud_moments(audio, sr, zone))]
    if not ranked:
        return []
    ranked.sort(key=lambda pair: pair[0])

    chosen, total = [], 0.0
    for _priority, when in ranked:
        if total >= MAX_EXTRA:
            break
        start, end = max(0.0, when - PAD), min(song_length, when + PAD)
        if any(start < c_end and end > c_start for c_start, c_end in chosen):
            continue                                   # already covered by a window we kept
        end = min(end, start + (MAX_EXTRA - total))
        if end - start < 0.5:
            continue
        chosen.append((start, end))
        total += end - start
    chosen.sort()
    return chosen


def shift(audio, sr, semitones):
    """The song, moved by whole semitones, by playing it faster or slower."""
    import librosa
    import numpy as np
    factor = 2 ** (semitones / 12)
    mono = audio.mean(axis=1) if audio.ndim > 1 else audio
    y = librosa.resample(np.ascontiguousarray(mono), orig_sr=sr, target_sr=int(sr / factor))
    return np.stack([y, y], axis=1), factor


def merge(base, extra, zone, lowest=21, highest=108):
    """Add extreme notes the straight pass could not hear, and nothing else.

    Only notes inside the zone count, and only when the straight pass does not already have that
    key at that moment, so the middle of the keyboard is never touched."""
    keep = list(base)
    added = 0
    for n in extra:
        if not lowest <= n.pitch <= highest:
            continue
        if not (n.pitch <= LOW_ZONE if zone == "low" else n.pitch >= HIGH_ZONE):
            continue
        if any(b.pitch == n.pitch and abs(b.start - n.start) < 0.08 for b in keep):
            continue
        keep.append(n)
        added += 1
    keep.sort(key=lambda n: (n.start, n.pitch))
    return keep, added


def reach_all_88(audio, sr, base, transcribe, make, progress=None, lowest=21, highest=108):
    """Fold in the extreme keys the straight pass cannot reach.

    transcribe(audio) -> notes, for whichever listener the caller uses.
    make(start, end, pitch, velocity) -> a note of the caller's own kind.
    Returns (notes, plain sentence or None).
    """
    notes = list(base)
    song_length = len(audio) / sr
    found = {"low": 0, "high": 0}
    ask = wanted(base)
    for zone, semis in (("low", 12), ("high", -12)):
        if not ask[zone]:
            continue
        spans = segments(base, zone, song_length, audio, sr)
        if not spans:
            continue
        if progress:
            progress(zone, sum(e - s for s, e in spans))
        for start, end in spans:
            try:
                piece = audio[int(start * sr):int(end * sr)]
                if len(piece) < sr // 4:
                    continue
                moved, factor = shift(piece, sr, semis)
                heard = transcribe(moved)
                put_back = [make(start + n.start * factor, start + n.end * factor, n.pitch - semis, n.velocity)
                            for n in heard]
                notes, gained = merge(notes, put_back, zone, lowest, highest)
                found[zone] += gained
            except Exception:                       # an extra pass must never cost anyone a song
                continue
    bits = [f"{found[z]} very {word} {'note' if found[z] == 1 else 'notes'}"
            for z, word in (("low", "low"), ("high", "high")) if found[z]]
    return notes, ("Heard " + " and ".join(bits) + " on a second listen" if bits else None)
