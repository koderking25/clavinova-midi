// The whole transcription, chunk by chunk, in the browser.
//
// Verified against the Mac app on the test song: the same 180 notes, loudness identical on
// every one, largest timing difference 0.0 ms. That was on the processor path. The graphics
// chip is faster but takes numerical shortcuts, which shifts a few notes, so "exact" is the
// default and "fast" is offered with that written on it.

import { Features } from "./features.js";
import { Decoder } from "./decode.js";

const RUNTIME = "https://cdn.jsdelivr.net/npm/onnxruntime-web@1.29.0/dist/";

export const BACKENDS = {
  exact: { provider: "wasm", label: "Exact", note: "Matches the Mac app note for note." },
  fast: { provider: "webgpu", label: "Faster", note: "Uses the graphics chip. A few notes can differ slightly." },
};

/** Turn any audio file into two channels of 44.1 kHz samples, levelled like the Mac app. */
export async function decodeAudio(arrayBuffer, targetRate = 44100) {
  const ctx = new (window.AudioContext || window.webkitAudioContext)();
  let buffer;
  try {
    buffer = await ctx.decodeAudioData(arrayBuffer.slice(0));
  } finally {
    ctx.close();
  }
  if (buffer.sampleRate !== targetRate) {
    const off = new OfflineAudioContext(Math.min(2, buffer.numberOfChannels),
                                        Math.ceil((buffer.duration * targetRate)), targetRate);
    const src = off.createBufferSource();
    src.buffer = buffer;
    src.connect(off.destination);
    src.start();
    buffer = await off.startRendering();
  }
  const left = buffer.getChannelData(0);
  const right = buffer.numberOfChannels > 1 ? buffer.getChannelData(1) : left;
  const n = left.length;

  // Same levelling the Mac app applies: about -20 dBFS, never clipping.
  let peak = 0, sumsq = 0;
  for (let i = 0; i < n; i++) {
    const a = Math.abs(left[i]), b = Math.abs(right[i]);
    if (a > peak) peak = a;
    if (b > peak) peak = b;
    sumsq += left[i] * left[i] + right[i] * right[i];
  }
  const rms = Math.sqrt(sumsq / (2 * n));
  if (peak < 1e-4 || rms < 1e-5) throw new Error("That audio is silent.");
  if (n < 2 * targetRate) throw new Error("That audio is shorter than 2 seconds.");
  const gain = Math.min(0.95 / peak, 0.1 / rms);
  const l = new Float32Array(n), r = new Float32Array(n);
  for (let i = 0; i < n; i++) { l[i] = left[i] * gain; r[i] = right[i] * gain; }
  return { left: l, right: r, sampleRate: targetRate, seconds: n / targetRate };
}

export class Engine {
  static async load({ base = "models/", backend = "exact", onStatus = () => {} } = {}) {
    const provider = (BACKENDS[backend] || BACKENDS.exact).provider;
    onStatus("Starting the engine");
    const ort = await import(RUNTIME + "ort.webgpu.min.mjs");
    ort.env.wasm.wasmPaths = RUNTIME;
    // The runtime prints its own housekeeping notes as console errors, which would bury a
    // real problem. Only genuine failures get through.
    ort.env.logLevel = "fatal";
    // More than one core needs shared memory, which browsers allow only when the page is
    // served with the two cross origin headers (see web/serve.py).
    ort.env.wasm.numThreads = self.crossOriginIsolated ? (navigator.hardwareConcurrency || 4) : 1;

    onStatus("Loading the listening model");
    const [features, decoder] = await Promise.all([Features.load(base), Decoder.load(base)]);
    const grab = async (file) => new Uint8Array(await (await fetch(base + file)).arrayBuffer());
    // The backbone arrives in pieces, because no host will serve a 63 MB file as one lump.
    const joinParts = async (file, count) => {
      const pieces = await Promise.all(
        Array.from({ length: count }, (_, i) => grab(`${file}.part${i}`)));
      const total = pieces.reduce((n, p) => n + p.length, 0);
      const all = new Uint8Array(total);
      let at = 0;
      for (const p of pieces) { all.set(p, at); at += p.length; }
      return all;
    };
    const backboneBytes = features.cfg.backboneParts
      ? await joinParts("backbone.web.onnx", features.cfg.backboneParts)
      : await grab("backbone.web.onnx");
    const backbone = await ort.InferenceSession.create(backboneBytes, { executionProviders: [provider] });
    const scorer = await ort.InferenceSession.create(await grab("scorer.web.onnx"), { executionProviders: [provider] });
    return new Engine(ort, features, decoder, backbone, scorer, provider);
  }

  constructor(ort, features, decoder, backbone, scorer, provider) {
    Object.assign(this, { ort, features, decoder, backbone, scorer, provider });
    this.cfg = features.cfg;
  }

  get cores() {
    return this.ort.env.wasm.numThreads;
  }

  /** left/right: levelled 44.1 kHz samples. Returns notes and pedal presses. */
  async run(left, right, { onProgress = () => {}, shouldStop = () => false } = {}) {
    const cfg = this.cfg;
    const fs = cfg.fs, hop = cfg.hopSize;
    const padSeconds = cfg.chunkSeconds - cfg.hopSeconds;
    const pad = Math.ceil(padSeconds * fs);
    const n = left.length;
    const total = n + 2 * pad;
    const padded = (src) => {
      const out = new Float32Array(total);
      out.set(src, pad);
      return out;
    };
    const L = padded(left), R = padded(right);
    const segSize = Math.ceil(cfg.chunkSeconds * fs);
    const stepSize = Math.ceil((cfg.hopSeconds * fs) / hop) * hop;
    const lastFrameIdx = Math.round(segSize / hop);
    const nP = cfg.pitches.length;
    const chunkCount = Math.ceil(total / stepSize);

    let forced = new Array(nP).fill(Math.floor((padSeconds * fs) / hop));
    const byPitch = new Map();
    const runScorer = async (slice, count) => {
      const out = await this.scorer.run({
        ctx: new this.ort.Tensor("float32", slice, [count, cfg.framesPerChunk, cfg.ctxSize]),
      });
      return out[Object.keys(out)[0]].data;
    };

    const started = performance.now();
    let index = 0;
    for (let i = 0; i < total; i += stepSize) {
      if (shouldStop()) throw new Error("stopped");
      const end = Math.min(i + segSize, total);
      const l = new Float32Array(segSize), r = new Float32Array(segSize);
      l.set(L.subarray(i, end));
      r.set(R.subarray(i, end));
      const { mel, nFrames } = this.features.compute(l, r, segSize);
      const ctxOut = await this.backbone.run({
        mel: new this.ort.Tensor("float32", mel, [1, nFrames, cfg.nMels, cfg.nWindows]),
      });
      const ctx = ctxOut[Object.keys(ctxOut)[0]].data;
      const { notes, lastP } = await this.decoder.chunk(ctx, nFrames, runScorer, forced, lastFrameIdx);
      Decoder.merge(byPitch, notes, i / fs - padSeconds);
      forced = lastP.map((k) => Math.max(k - Math.floor(stepSize / hop), 0));
      index++;
      const done = index / chunkCount;
      const elapsed = (performance.now() - started) / 1000;
      onProgress({ done, chunk: index, chunks: chunkCount, elapsed, eta: done > 0 ? elapsed * (1 - done) / done : null });
      await new Promise((ok) => setTimeout(ok, 0));            // let the page redraw
    }
    const { notes, pedals } = Decoder.finish(byPitch);
    return { notes, pedals, chunks: chunkCount, seconds: n / fs, elapsed: (performance.now() - started) / 1000 };
  }
}
