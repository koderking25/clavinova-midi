// Flash drive support in the browser.
//
// Chrome and Edge on a computer can write straight to a folder the visitor picks,
// which can be their flash drive. Safari and Firefox cannot, and fall back to a
// normal download. Nothing here uploads anything.

const DB = "clavinova-drive";
const STORE = "handles";
const NAME_LIMIT = 40;          // keeps song names readable on a piano's small screen

export const supported = typeof window !== "undefined" && "showDirectoryPicker" in window;

export function safeName(title, ext = ".mid") {
  const plain = (title || "")
    .normalize("NFKD")
    .replace(/[̀-ͯ]/g, "")            // strip accents
    .replace(/[^A-Za-z0-9 _()\-]+/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .replace(/^[.\-_ ]+|[.\-_ ]+$/g, "");
  return (plain.slice(0, NAME_LIMIT).trim() || "Song") + ext;
}

// ---------------------------------------------------------------- remembering

function idb() {
  return new Promise((ok, fail) => {
    const r = indexedDB.open(DB, 1);
    r.onupgradeneeded = () => r.result.createObjectStore(STORE);
    r.onsuccess = () => ok(r.result);
    r.onerror = () => fail(r.error);
  });
}

async function idbSet(key, value) {
  const db = await idb();
  await new Promise((ok, fail) => {
    const tx = db.transaction(STORE, "readwrite");
    tx.objectStore(STORE).put(value, key);
    tx.oncomplete = ok;
    tx.onerror = () => fail(tx.error);
  });
  db.close();
}

async function idbGet(key) {
  const db = await idb();
  const value = await new Promise((ok, fail) => {
    const tx = db.transaction(STORE, "readonly");
    const req = tx.objectStore(STORE).get(key);
    req.onsuccess = () => ok(req.result);
    req.onerror = () => fail(req.error);
  });
  db.close();
  return value;
}

/** Ask for the flash drive folder. Must be called from a click. */
export async function pickDrive() {
  const handle = await window.showDirectoryPicker({ id: "clavinova-drive", mode: "readwrite" });
  await idbSet("drive", handle);
  return handle;
}

/** The drive from last time, if the browser still lets us write to it. */
export async function rememberedDrive({ ask = false } = {}) {
  const handle = await idbGet("drive").catch(() => null);
  if (!handle) return null;
  let state = await handle.queryPermission({ mode: "readwrite" });
  if (state === "prompt" && ask) state = await handle.requestPermission({ mode: "readwrite" });
  return state === "granted" ? handle : null;
}

export async function forgetDrive() {
  await idbSet("drive", null).catch(() => {});
}

// ---------------------------------------------------------------- saving

async function exists(dir, name) {
  try {
    await dir.getFileHandle(name);
    return true;
  } catch {
    return false;
  }
}

/** macOS writes these next to real files. Pianos and keyboards can list them as broken songs. */
async function removeSidecar(dir, name) {
  try {
    await dir.removeEntry("._" + name);
  } catch {
    /* nothing to remove */
  }
}

/**
 * Save one MIDI file onto the drive, never overwriting a different song,
 * then read it back and check every byte.
 */
export async function saveToDrive(dir, title, bytes) {
  let name = safeName(title);
  for (let n = 2; await exists(dir, name); n++) {
    const same = await readFile(dir, name);
    if (same && same.length === bytes.length && same.every((b, i) => b === bytes[i])) {
      return { name, alreadyThere: true };
    }
    name = safeName(title).replace(/\.mid$/, "") .slice(0, NAME_LIMIT - 4) + ` ${n}.mid`;
  }
  const fh = await dir.getFileHandle(name, { create: true });
  const w = await fh.createWritable();
  await w.write(bytes);
  await w.close();
  await removeSidecar(dir, name);
  const back = await readFile(dir, name);
  if (!back || back.length !== bytes.length || !back.every((b, i) => b === bytes[i])) {
    throw new Error("The copy on the flash drive does not match. Try again, or try another drive.");
  }
  return { name, alreadyThere: false };
}

async function readFile(dir, name) {
  try {
    const f = await (await dir.getFileHandle(name)).getFile();
    return new Uint8Array(await f.arrayBuffer());
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------- tidy

/** Find the hidden files macOS leaves behind. Returns their names. */
export async function findMacClutter(dir, depth = 2) {
  const found = [];
  async function walk(d, prefix, level) {
    for await (const [name, handle] of d.entries()) {
      if (handle.kind === "directory") {
        if (level > 0 && !name.startsWith(".")) await walk(handle, prefix + name + "/", level - 1);
        continue;
      }
      if (name === ".DS_Store" || name.startsWith("._")) {
        const size = (await handle.getFile()).size;
        if (name === ".DS_Store" || size <= 64 * 1024) found.push({ dir: d, name, path: prefix + name });
      }
      if (found.length > 5000) return;
    }
  }
  await walk(dir, "", depth);
  return found;
}

export async function tidyDrive(dir, { dryRun = true } = {}) {
  const files = await findMacClutter(dir);
  let removed = 0;
  if (!dryRun) {
    for (const f of files) {
      try {
        await f.dir.removeEntry(f.name);
        removed++;
      } catch {
        /* locked or already gone */
      }
    }
  }
  return { found: files.length, removed, examples: files.slice(0, 8).map((f) => f.path) };
}

// ---------------------------------------------------------------- format check

/**
 * Guess whether the drive is formatted FAT32, which is what most digital pianos and keyboards read.
 *
 * A browser cannot ask what a drive is formatted as. But FAT32 stores file times
 * in 2 second steps, so every file it writes lands on an even second. Other
 * formats keep finer times: exFAT 10 ms, older Mac 1 second, APFS finer still.
 * So write a few small files a moment apart and look at the times that come back.
 *
 * Returns { verdict: "fat32" | "one-second" | "fine" | "unknown", samples }.
 * This is a guess from evidence, not a real format check, so the wording the
 * page shows should stay cautious.
 */
export async function guessFormat(dir, samples = 3) {
  const times = [];
  for (let i = 0; i < samples; i++) {
    const name = `.clavinova-check-${i}.tmp`;
    try {
      const fh = await dir.getFileHandle(name, { create: true });
      const w = await fh.createWritable();
      await w.write(new Uint8Array([0]));
      await w.close();
      times.push((await fh.getFile()).lastModified);
      await dir.removeEntry(name).catch(() => {});
      await removeSidecar(dir, name);
    } catch {
      return { verdict: "unknown", samples: times };
    }
    if (i < samples - 1) await new Promise((r) => setTimeout(r, 420));
  }
  if (times.length < samples) return { verdict: "unknown", samples: times };
  if (times.every((t) => t % 2000 === 0)) return { verdict: "fat32", samples: times };
  if (times.every((t) => t % 1000 === 0)) return { verdict: "one-second", samples: times };
  return { verdict: "fine", samples: times };
}

/** Free space, when the browser will say. Not every browser answers. */
export async function driveSpace() {
  try {
    const est = await navigator.storage.estimate();
    return est && est.quota ? est : null;
  } catch {
    return null;
  }
}

/** Fallback for Safari and Firefox: a normal download. */
export function downloadFile(title, bytes) {
  const blob = new Blob([bytes], { type: "audio/midi" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = safeName(title);
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
}
