"""Automatic mode choice: can the app tell a solo piano recording from everything else?

    .venv/bin/python tests/test_auto_mode.py            # tuning songs only
    .venv/bin/python tests/test_auto_mode.py --final    # also the held-out songs, once

Labels were written before anything was measured, and every third song (in a fixed order) is held
out: the rule is chosen on the others and judged on those. Songs whose type I was unsure of are left
out rather than guessed. Measurements are cached in /tmp/auto-mode-cache, because separation is slow.
"""
import hashlib
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "app"))
import pipeline  # noqa: E402

D = Path.home() / "Downloads"
CACHE = Path("/tmp/auto-mode-cache")
CACHE.mkdir(exist_ok=True)

SOLO_PIANO = [
    "Yiruma - River Flows in You 4.mp3",
    "Debussy - Clair de Lune 4.mp3",
    "Debussy - Arabesque No. 1 4.mp3",
    "Starry Night (Piano) 4.mp3",
    "Andrea Vanzo - Valzer d'Inverno.mp3",
    "Tony Ann – ICARUS [Virtuosic Piano Solo].mp3",
    "Gibran Alcocer - Idea 10 (Sheet Music).mp3",
    "Mrs Magic - Strawberry Guy (Piano Version).mp3",
    "Bruno Mars - When I Was Your Man (Instrumental Original) 4.mp3",
]
NOT_SOLO_PIANO = [
    "Oasis - Wonderwall (Lyrics) 4.mp3",
    "Lewis Capaldi - Someone You Loved (Lyrics) 4.mp3",
    "Hoobastank - The Reason (Lyrics) (1).mp3",
    "Bastille - Pompeii (Lyric Video) 4.mp3",
    "Billie Jean - Michael Jackson (Lyrics).mp3",
    "Bon Jovi - Livin' On A Prayer 4.mp3",
    "Survivor - Eye Of The Tiger (Lyrics) 4.mp3",
    "Tracy Chapman - Fast Car (Official Music Video) 4.mp3",
    "John Legend - All of Me (Lyrics) 4.mp3",
    "Sam Smith - Stay With Me (Lyric Video) 4.mp3",
    "Elvis Presley - Can't Help Falling in Love (Lyrics) 4.mp3",
    "Journey - Don't Stop Believin' (Official Audio) 4.mp3",
    "Christina Perri - A Thousand Years 4.mp3",
    "Lukas Graham - 7 Years 4.mp3",
    "Coldplay - Sparks 4.mp3",
    "Billy Joel - Honesty (Audio) 4.mp3",
    "Talking to the Moon - Bruno Mars - violin cover - Daniel Jang (128k).mp3",
    "Debussy Arabesque for Violin and Piano _ Malwina Sosnowski, Violin & Benyamin Nuss, Piano.mp3",
    "The Beatles - Yesterday (Instrumental Mix).mp3",
]
SONGS = [(n, "piano") for n in SOLO_PIANO] + [(n, "arrange") for n in NOT_SOLO_PIANO]
SONGS.sort(key=lambda s: hashlib.sha1(s[0].encode()).hexdigest())      # a fixed order, unrelated to label
HELD_OUT = {name for i, (name, _) in enumerate(SONGS) if i % 3 == 2}


def features(name):
    f = CACHE / (hashlib.sha1(name.encode()).hexdigest() + ".json")
    if f.exists():
        return json.loads(f.read_text())
    x = pipeline.decode_audio(D / name, CACHE / "decoded.wav")
    shares = pipeline.stem_shares(x)
    f.write_text(json.dumps(shares))
    return shares


if __name__ == "__main__":
    final = "--final" in sys.argv
    rows = []
    for name, label in SONGS:
        held = name in HELD_OUT
        if held and not final:
            continue
        if not (D / name).exists():
            print(f"missing: {name}")
            continue
        s = features(name)
        guess = pipeline.decide_mode(s)
        ok = guess == label
        rows.append((held, ok))
        print(f"{'HELD OUT' if held else 'tuning  '}  {name[:52]:52s} truth {label:7s} guess {guess:7s} "
              f"{'right' if ok else 'WRONG'}   voice {s['vocals']:.3f} drums {s['drums']:.3f} bass {s['bass']:.3f}")
    for group, flag in (("tuning", False), ("held out", True)):
        sel = [ok for held, ok in rows if held == flag]
        if sel:
            print(f"{group}: {sum(sel)} of {len(sel)} right")
