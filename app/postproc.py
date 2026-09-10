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
