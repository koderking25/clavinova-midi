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
other big apps if your Mac feels slow. Each song is made in its own process, so
the moment it finishes, all of that memory goes back to your Mac; between songs
the app itself uses about 0.3 GB.

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

## The Mac app

The website is the demo and does solo piano only. The Mac app is the full thing:
every mode, about five times faster, song search, and the complete flash drive
handling.

Install it with one line in Terminal:

```bash
curl -fsSL https://raw.githubusercontent.com/koderking25/clavinova-midi/main/mac/install.sh | bash
```

That downloads the code, sets up its private Python environment, builds the app
and puts **Clavinova MIDI Maker** in your Applications folder. Opening it the
first time downloads the AI models, which takes about ten minutes and needs
[Homebrew](https://brew.sh) for the audio tools.

There is also a zip of the app on the
[releases page](https://github.com/koderking25/clavinova-midi/releases/latest).
It is not signed with an Apple developer account (that needs Apple's paid
programme), so macOS blocks a downloaded copy and says it "could not verify" the
app. Open it once and let it be refused, then go to **System Settings** ->
**Privacy & Security**, scroll down to the message about the app and click
**Open Anyway**. Once only.

On macOS 15 and newer, right clicking and choosing Open no longer works for this:
Apple moved the decision into System Settings. The one line install above avoids
the whole thing, because the app is built on your Mac rather than downloaded, so
it never gets flagged. To clear the flag on a copy you already downloaded:

```bash
xattr -dr com.apple.quarantine "/Applications/Clavinova MIDI Maker.app"
```

To rebuild the app after changing the code:

```bash
.venv/bin/python mac/build_app.py
```

## The browser version (no install, nothing uploaded)

`web/` holds a version that runs entirely in a browser tab. It produces the same
notes as the Mac app: on the test song, 180 notes, the same notes, loudness
identical on all 180, largest timing difference 0.0 ms.

Visitors download about 73 MB of model files once, then it is cached. Nothing is
uploaded: the song never leaves the computer it is played on.

### Build the model files

```bash
.venv/bin/python web/export_models.py
```

This converts the same model the Mac app uses and checks every piece against
PyTorch before keeping it. The files land in `web/models/` and stay out of the
repo.

### Run it locally

```bash
.venv/bin/python web/serve.py
```

Then open http://127.0.0.1:8792. That small server exists for one reason: it
sends the two headers (`Cross-Origin-Opener-Policy` and
`Cross-Origin-Embedder-Policy`) that let the page use every processor core.
Without them the browser allows one core and the work takes twice as long.
Cloudflare Pages and Workers can send the same two headers.

### Putting the website on Cloudflare

Cloudflare serves whatever you upload as the site root. Uploading this repository puts
the page at `/web/` and leaves `/` a 404, which is the one trap here. Build a package
whose root is the site itself:

```bash
.venv/bin/python web/export_models.py   # if the model files are not built yet
.venv/bin/python web/package_site.py
```

That writes `dist/midify-site/` and `dist/midify-site.zip` (about 55 MB), and refuses to
finish if `index.html` is not at the root or any file is over Cloudflare's 25 MiB limit.
In the Cloudflare dashboard, drag the zip or the folder onto the upload box.

The backbone is split into four pieces because of that 25 MiB limit, and the browser
joins them again. `_headers` sends the two cross origin headers that let the page use
every processor core; without them it still works, on one core, at about half the speed.

### Speed, measured on this Mac (M1, 8 GB, 8 cores)

| | 27 seconds of audio |
| --- | --- |
| Exact (processor, 8 cores) | 52 s, about 1.9 times the song |
| Exact (processor, 1 core) | 95 s |
| Faster (graphics chip) | 49 s |

Chunks are 16 seconds long and start every 12 seconds. The chunk length is the
model's own and shortening it ruins accuracy (1.000 at 16 s, 0.599 at 8 s). The
gap between chunks is ours, chosen by measurement: 12 s matches the 8 s default
on the piano test and is slightly better on the band test, with half the work.

### Exact or Faster

The graphics chip takes numerical shortcuts. Same pipeline, same song: 181 notes
instead of 180, and loudness matching on 82 of 180. It is offered as "Faster"
with that written on it, and "Exact" is the default.

### Flash drive in a browser

Chrome and Edge on a computer can write straight to a drive the visitor picks,
and can clear the hidden files macOS leaves behind. Safari and Firefox get a
Download button instead. A browser cannot ask how a drive is formatted, so the
page guesses from how precisely the drive stores file times (FAT32 rounds to two
seconds) and says so cautiously. **This has not been tried on a real flash drive
yet.**

### Not in the browser yet

Piano version and Full band need the instrument separation model, which is a
further 166 MB download. The browser version does the solo piano path only.
