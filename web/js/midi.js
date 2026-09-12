// Writing a Standard MIDI File in the browser, matching the Mac app's writer byte for byte
// in intent: format 0, a General MIDI reset first, instruments set half a beat later, and
// nothing sounding until one beat in so the reset has settled.
//
// The Python version of this passes a round trip test (every note read back identical) and
// refuses deliberately damaged files, so this follows it exactly.

const PPQ = 480;
const SETUP_TICK = PPQ / 2;
const LEAD_IN = PPQ;
const NAME_LIMIT = 40;

export function safeTitle(title, limit = 60) {
  const plain = (title || "")
    .normalize("NFKD")
    .replace(/[̀-ͯ]/g, "")
    .split("")
    .filter((c) => c.charCodeAt(0) >= 32 && c.charCodeAt(0) < 127)
    .join("")
    .replace(/\s+/g, " ")
    .trim();
  return plain.slice(0, limit) || "Song";
}

function vlq(n) {
  const out = [n & 0x7f];
  n >>= 7;
  while (n > 0) { out.unshift((n & 0x7f) | 0x80); n >>= 7; }
  return out;
}

function tick(seconds, bpm) {
  return Math.max(0, Math.round((seconds * bpm / 60) * PPQ));
}

/**
 * Notes of one part as [onTick, offTick, pitch, velocity], with same-pitch overlaps resolved:
 * a note-off silences that pitch on the channel, so an earlier note must end where the next begins.
 */
function partTicks(part, bpm) {
  const byPitch = new Map();
  for (const n of part.notes) {
    const pitch = Math.round(n.pitch);
    if (pitch < 0 || pitch > 127) continue;
    let on = LEAD_IN + tick(n.start, bpm);
    let off = LEAD_IN + tick(n.end, bpm);
    if (off <= on) off = on + 1;
    const velocity = Math.min(127, Math.max(1, Math.round(n.velocity)));
    if (!byPitch.has(pitch)) byPitch.set(pitch, []);
    byPitch.get(pitch).push([on, off, pitch, velocity]);
  }
  const out = [];
  for (const list of byPitch.values()) {
    list.sort((a, b) => a[0] - b[0] || b[3] - a[3]);
    const kept = [];
    for (const n of list) {
      if (kept.length && kept[kept.length - 1][0] === n[0]) continue;       // duplicate onset
      if (kept.length && kept[kept.length - 1][1] > n[0]) kept[kept.length - 1][1] = n[0];
      kept.push(n);
    }
    for (const n of kept) if (n[1] > n[0]) out.push(n);
  }
  return out.sort((a, b) => a[0] - b[0] || a[2] - b[2]);
}

/**
 * parts: [{ name, channel (0-15, 9 is drums), program, notes: [{start,end,pitch,velocity}],
 *           pedal: [[timeSeconds, value]], volume }]
 * Returns a Uint8Array holding the .mid file.
 */
export function writeMidi(parts, { bpm = 120, title = "Song" } = {}) {
  bpm = Math.min(240, Math.max(40, bpm));
  const events = [];                          // [tick, priority, order, bytes]
  const add = (t, prio, bytes) => events.push([t, prio, events.length, bytes]);
  const text = (s) => Array.from(safeTitle(s)).map((c) => c.charCodeAt(0));

  const name = text(title);
  add(0, 0, [0xff, 0x03, name.length, ...name]);
  const usPerBeat = Math.round(60000000 / bpm);
  add(0, 0, [0xff, 0x51, 0x03, (usPerBeat >> 16) & 0xff, (usPerBeat >> 8) & 0xff, usPerBeat & 0xff]);
  add(0, 0, [0xff, 0x58, 0x04, 4, 2, 24, 8]);
  add(0, 1, [0xf0, 0x05, 0x7e, 0x7f, 0x09, 0x01, 0xf7]);          // General MIDI reset

  for (const part of parts) {
    const ch = part.channel & 0x0f;
    if (ch !== 9) {
      add(SETUP_TICK, 2, [0xb0 | ch, 0, 0]);
      add(SETUP_TICK, 2, [0xb0 | ch, 32, 0]);
    }
    add(SETUP_TICK, 3, [0xc0 | ch, (part.program || 0) & 0x7f]);
    add(SETUP_TICK, 4, [0xb0 | ch, 7, Math.min(127, part.volume ?? 100)]);
    add(SETUP_TICK, 4, [0xb0 | ch, 11, 127]);
    add(SETUP_TICK, 4, [0xb0 | ch, 64, 0]);
    for (const [t, value] of part.pedal || []) {
      add(LEAD_IN + tick(t, bpm), 5, [0xb0 | ch, 64, Math.min(127, Math.max(0, Math.round(value)))]);
    }
    for (const [on, off, pitch, velocity] of partTicks(part, bpm)) {
      add(on, 7, [0x90 | ch, pitch, velocity]);
      add(off, 6, [0x80 | ch, pitch, 0]);
    }
  }

  const last = events.reduce((m, e) => Math.max(m, e[0]), 0);
  events.sort((a, b) => a[0] - b[0] || a[1] - b[1] || a[2] - b[2]);
  const track = [];
  let now = 0;
  for (const [t, , , bytes] of events) {
    track.push(...vlq(t - now), ...bytes);
    now = t;
  }
  track.push(...vlq(last + PPQ - now), 0xff, 0x2f, 0x00);

  const head = [0x4d, 0x54, 0x68, 0x64, 0, 0, 0, 6, 0, 0, 0, 1, (PPQ >> 8) & 0xff, PPQ & 0xff];
  const len = track.length;
  const trackHead = [0x4d, 0x54, 0x72, 0x6b, (len >> 24) & 0xff, (len >> 16) & 0xff, (len >> 8) & 0xff, len & 0xff];
  return new Uint8Array([...head, ...trackHead, ...track]);
}

/** Split one stream of piano notes into right and left hand parts, with the pedal on both. */
export function pianoParts(notes, pedal, splitHands, splitPoint = 60) {
  if (!splitHands) return [{ name: "Piano", channel: 0, program: 0, notes, pedal }];
  return [
    { name: "Right hand", channel: 0, program: 0, notes: notes.filter((n) => n.pitch >= splitPoint), pedal },
    { name: "Left hand", channel: 1, program: 0, notes: notes.filter((n) => n.pitch < splitPoint), pedal },
  ];
}

/** Pedal events from the decoder (negative pitches) into sustain messages. */
export function pedalEvents(pedals) {
  const out = [];
  for (const p of pedals) {
    if (p.pitch !== -64) continue;                      // -67 is the soft pedal, which we leave out
    out.push([p.start, Math.min(127, Math.max(1, p.velocity))], [p.end, 0]);
  }
  return out.sort((a, b) => a[0] - b[0]);
}

export function safeFilename(title) {
  const plain = (title || "")
    .normalize("NFKD")
    .replace(/[̀-ͯ]/g, "")
    .replace(/[^A-Za-z0-9 _()\-]+/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .replace(/^[.\-_ ]+|[.\-_ ]+$/g, "");
  return (plain.slice(0, NAME_LIMIT).trim() || "Song") + ".mid";
}
