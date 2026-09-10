# Clavinova MIDI Maker

Type a song name (or drop in a recording) and get a MIDI file your Yamaha
Clavinova CVP-503 can play from a USB flash drive.

## Set it up (first time only)

Needs a Mac with Apple Silicon (M1 or newer), [Homebrew](https://brew.sh), and
about 2.5 GB of free space.

```bash
git clone https://github.com/koderking25/clavinova-midi.git
cd clavinova-midi
./setup.sh
```

`setup.sh` installs ffmpeg, fluid-synth, deno and uv with Homebrew, then Python
3.11 and the exact package versions this app was tested with
(`requirements.lock`). It downloads the AI models and finishes by running the
self-test. It takes about 10 minutes. You can also skip this step: the first
double-click on the app runs it for you.

## Start it

Double-click **Clavinova MIDI Maker.command** in this folder. A Terminal window
opens (keep it open while you use the app) and your browser opens the app at
http://127.0.0.1:8765. Close the Terminal window to stop it.

The first start takes about 20 seconds while the AI models load.

## Use it

1. **Pick how it should sound**
   - **Piano version** (best for most songs): drums are removed, the
     instruments become piano, and the sung melody goes in the right hand.
   - **Solo piano recording**: the most accurate. Use it for recordings that
     are only a piano (piano covers, classical pieces).
   - **Full band**: melody, chords, bass and drums on separate Clavinova parts.
     More experimental.
   - In the piano modes you can put each hand on its own part (right hand on
     channel 1, left hand on channel 2), so you can turn one hand off on the
     Clavinova and play it yourself.
2. **Find a song**: type the name and artist. Adding "piano" finds piano
   covers, which give the cleanest results. If BitMidi already has a hand-made
   MIDI of the song, it is listed first with a link. Those are usually better
   than any automatic version: download it there, then drop the .mid file on
   the app to send it to your flash drive.
3. **Or drop a file**: MP3, M4A, WAV, FLAC and most other audio or video files.
4. **Send to flash drive**: plug the drive in, click *Send to flash drive*,
   then *Eject* before unplugging.

On the Clavinova, plug the drive into its USB port and pick the song from the
USB tab of the song list.

## Where things go

- Finished songs: `~/Music/Clavinova MIDI/` (previews and details are kept in a
  hidden `.clavinova` folder next to them).
- *Remove* moves a song to the Mac's Trash. Nothing is deleted outright.

## Flash drive tips

- The drive should be formatted **MS-DOS (FAT32)**. The app shows a green dot
  for drives the Clavinova can read, and a warning for exFAT or Mac formats.
- Macs leave hidden files (like `._Song.mid`) on drives, which the Clavinova can
  list as broken songs. The app never creates them when it copies, and
  **Tidy** removes any that are already there.
- File names are kept short and plain (letters, numbers, spaces) so they read
  well on the Clavinova's screen.

## What to expect

Automatic transcription is not perfect. On test songs with known notes:

| Mode | Accuracy (notes found correctly) |
| --- | --- |
| Solo piano recording | 100% |
| Piano version of a band song | about 85% |
| Full band: bass | 85 to 97% |
| Full band: kick and snare | 87 to 100% |

Real recordings with reverb, singing and crowded mixes will score lower than
these tests. Piano covers work best of all.

Songs can be up to 15 minutes long. Measured on this Mac (M1, 8 GB):

| Mode | Time for a 5-minute song |
| --- | --- |
| Solo piano recording | about 2 to 3 minutes |
| Piano version | about 6 minutes |
| Full band | about 5 minutes |

A song uses at most about 3.7 GB of memory while it is being made, so close
other big apps if your Mac feels slow.

## If something goes wrong

- **YouTube downloads fail**: YouTube changes often. Click *Update downloader*
  at the bottom of the page. The app updates and restarts itself.
- **"Your Mac is almost out of space"**: each song needs a few hundred MB while
  it is being made. Free up a few GB.
- **Error details** are saved in `state/errors.log`.
- **Check that everything still works**: in Terminal, run

  ```bash
  cd ~/clavinova-midi && .venv/bin/python tests/selftest.py
  ```

  It makes test songs from known notes, runs them through the app, and prints
  PASS or FAIL for each check.

## How it works

- Downloads: yt-dlp (audio only), with a pause between requests.
- Instrument separation: Demucs (htdemucs).
- Piano notes and sustain pedal: Transkun.
- Melody and bass: Spotify's Basic Pitch.
- Drums: a band-split onset detector written for this app.
- Output: Standard MIDI File format 0 with a General MIDI reset at the start,
  which every Clavinova that reads MIDI files can play. Every file is read back
  and checked before it is saved.
