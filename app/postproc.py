import copy
import numpy as np
GHOST = {12, -12, 24, -24, 19, -19}
def ghost_octave(ns, dv=6, maxdur=0.15):
    out = []
    for n in ns:
        if (n.end - n.start) < maxdur and any(m is not n and abs(m.start - n.start) < 0.05 and (m.pitch - n.pitch) in GHOST and m.velocity >= n.velocity + dv for m in ns):
            continue
        out.append(n)
    return out
def rel_vel(ns, frac):
    if not ns: return ns
    med = np.median([n.velocity for n in ns]); return [n for n in ns if n.velocity >= frac * med]
def mono(ns):
    ns = sorted(ns, key=lambda n: n.start); groups = []
    for n in ns:
        if groups and n.start - groups[-1][0].start < 0.06: groups[-1].append(n)
        else: groups.append([n])
    out = [copy.copy(max(g, key=lambda n: n.velocity)) for g in groups]
    for a, b in zip(out, out[1:]): a.end = min(a.end, b.start)
    return [n for n in out if n.end - n.start > 0.03]


def fade_out(notes, pedal, end_at, fade=6.0):
    """End a shortened song gently instead of cutting it off mid-bar.

    A hard cut at five minutes leaves notes hanging and the sustain pedal down, which on a piano
    sounds like the file broke. This takes the last few seconds down in loudness, stops every note
    at the cut, and lifts both pedals.

    Only loudness is faded, because a piano note cannot be turned down once it is struck: notes
    that begin inside the fade are played softer, and notes already sounding are left alone and
    simply stop at the end.
    """
    if not end_at or end_at <= 0:
        return notes, pedal
    fade = max(0.0, min(fade, end_at / 2))
    fade_from = end_at - fade
    quietest = 0.28                                   # still audible, clearly on the way out

    kept = []
    for n in notes:
        if n.start >= end_at - 0.05:
            continue                                  # starts at or after the cut: it never happens
        end = min(n.end, end_at)
        if end - n.start < 0.05:
            continue                                  # what is left is too short to hear as a note
        velocity = n.velocity
        if fade > 0 and n.start > fade_from:
            through = (n.start - fade_from) / fade    # 0 at the start of the fade, 1 at the end
            velocity = int(round(n.velocity * (1 - through * (1 - quietest))))
        kept.append(type(n)(n.start, end, n.pitch, max(1, min(127, velocity))))

    pedals = [(t, number, value) for t, number, value in (pedal or []) if t < end_at - 0.05]
    for number in (64, 67):                           # sustain and soft, both released at the end
        if any(p[1] == number for p in pedals):
            pedals.append((max(0.0, end_at - 0.05), number, 0))
    pedals.sort()
    return kept, pedals
