"""Drum-stem transcription: band onsets, loudness-adaptive, bleed split per band."""
import numpy as np
import scipy.ndimage as nd
import scipy.signal as ss
import librosa

HOP = 256
BANDS = {36: (30, 150), 38: (1500, 5000), 42: (7000, 16000)}  # kick, snare, closed hat
FLOOR = {36: 0.3, 38: 0.3, 42: 0.15}  # minimum level (vs. loudest nearby hit) when there is no clear split


def band_env(y, sr, lo, hi):
    sos = ss.butter(4, [lo, min(hi, sr / 2 - 100)], btype="band", fs=sr, output="sos")
    return librosa.feature.rms(y=ss.sosfiltfilt(sos, y), frame_length=1024, hop_length=HOP)[0]


def local_norm(env, sr, win_s=4.0):
    """Level relative to the loudest nearby hit, so quiet verses still register."""
    roll = nd.maximum_filter1d(env, size=max(3, int(win_s * sr / HOP)))
    return env / np.maximum(roll, 0.25 * env.max())


def split_threshold(levels):
    """Otsu split of candidate levels. Returns a threshold only if clearly two groups."""
    L = np.sort(np.asarray(levels))
    if len(L) < 6 or L.var() < 1e-6:
        return None
    best_k, best_between = None, -1.0
    for k in range(1, len(L)):
        a, b = L[:k], L[k:]
        between = (k / len(L)) * (1 - k / len(L)) * (b.mean() - a.mean()) ** 2
        if between > best_between:
            best_k, best_between = k, between
    a, b = L[:best_k], L[best_k:]
    eta = best_between / L.var()
    if eta > 0.75 and b.mean() - a.mean() > 0.2 and len(b) >= 0.15 * len(L):
        return (a.max() + b.min()) / 2
    return None


def transcribe_drums(y, sr, mix_rms, debug=False):
    """y: mono drum stem. mix_rms: RMS of the full song (for the no-drums gate).
    Returns list of (time_s, gm_pitch, velocity)."""
    hits = []
    if mix_rms <= 0 or np.sqrt((y ** 2).mean()) < 0.05 * mix_rms:
        return hits
    for pitch, (lo, hi) in BANDS.items():
        env = band_env(y, sr, lo, hi)
        if env.max() <= 1e-6:
            continue
        flux = np.maximum(0, np.diff(env, prepend=env[0]))
        flux /= flux.max()
        peaks = librosa.util.peak_pick(flux, pre_max=3, post_max=3, pre_avg=12, post_avg=12,
                                       delta=0.07, wait=int(0.06 * sr / HOP))
        glob = env / env.max()
        loc = local_norm(env, sr)
        cands = []
        for p in peaks:
            if glob[p:p + 4].max() < 0.06:          # far below this band's loudest hit: noise
                continue
            cands.append((p, float(loc[p:p + 4].max())))
        # Hats: snare hits add energy to the hat band, which fakes a two-group split.
        thr = None if pitch == 42 else split_threshold([c[1] for c in cands])
        floor = thr if thr is not None else FLOOR[pitch]
        if debug:
            print(f"  band {pitch}: {len(cands)} candidates, split {thr}")
        for p, lv in cands:
            if lv >= floor:
                t = librosa.frames_to_time(p, sr=sr, hop_length=HOP)
                hits.append((float(t), pitch, int(np.clip(45 + 82 * lv, 45, 127))))
    return sorted(hits)
