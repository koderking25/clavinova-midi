// Turning the model's scores into notes, in the browser.
//
// This is a translation of transkun's own decoding, checked line for line against it in
// Python first: same scores in, identical intervals on all 90 pitches, identical notes
// (start, end, pitch, loudness, onset and offset flags) and identical carry-over between
// chunks.
//
// The heavy arithmetic (the score tables) runs on the graphics chip, nine pitches at a
// time, so the full 172 MB table for a chunk never exists at once.

const PEDAL_PITCHES = new Set([-64, -67]);       // transkun writes pedals as negative pitches

// Error function, Cody's rational approximations, accurate to roughly the last digit a
// double can hold. The model was trained with the exact GELU, and a rough error function
// shifts the loudness the model reports, so this one is worth its length.
const ERF_A = [3.16112374387056560e0, 1.13864154151050156e2, 3.77485237685302021e2, 3.20937758913846947e3, 1.85777706184603153e-1];
const ERF_B = [2.36012909523441209e1, 2.44024637934444173e2, 1.28261652607737228e3, 2.84423683343917062e3];
const ERF_C = [5.64188496988670089e-1, 8.88314979438837594e0, 6.61191906371416295e1, 2.98635138197400131e2, 8.81952221241769090e2, 1.71204761263407058e3, 2.05107837782607147e3, 1.23033935479799725e3, 2.15311535474403846e-8];
const ERF_D = [1.57449261107098347e1, 1.17693950891312499e2, 5.37181101862009858e2, 1.62138957456669019e3, 3.29079923573345963e3, 4.36261909014324716e3, 3.43936767414372164e3, 1.23033935480374942e3];
const ERF_P = [3.05326634961232344e-1, 3.60344899949804439e-1, 1.25781726111229246e-1, 1.60837851487422766e-2, 6.58749161529837803e-4, 1.63153871373020978e-2];
const ERF_Q = [2.56852019228982242e0, 1.87295284992346047e0, 5.27905102951428412e-1, 6.05183413124413191e-2, 2.33520497626869185e-3];

function erf(x) {
  const ax = Math.abs(x);
  if (ax <= 0.46875) {
    const z = ax > 1.11e-16 ? ax * ax : 0;
    let top = ERF_A[4] * z, bot = z;
    for (let i = 0; i < 3; i++) { top = (top + ERF_A[i]) * z; bot = (bot + ERF_B[i]) * z; }
    return x * (top + ERF_A[3]) / (bot + ERF_B[3]);
  }
  let erfc;
  if (ax <= 4) {
    let top = ERF_C[8] * ax, bot = ax;
    for (let i = 0; i < 7; i++) { top = (top + ERF_C[i]) * ax; bot = (bot + ERF_D[i]) * ax; }
    erfc = (top + ERF_C[7]) / (bot + ERF_D[7]);
  } else {
    const z = 1 / (ax * ax);
    let top = ERF_P[5] * z, bot = z;
    for (let i = 0; i < 4; i++) { top = (top + ERF_P[i]) * z; bot = (bot + ERF_Q[i]) * z; }
    let r = z * (top + ERF_P[4]) / (bot + ERF_Q[4]);
    erfc = (0.5641895835477563 - r) / ax;
  }
  // The exponential is split so the rounding stays small, as in Cody's original.
  const y = Math.round(ax * 16) / 16;
  erfc *= Math.exp(-y * y) * Math.exp(-(ax - y) * (ax + y));
  return x > 0 ? 1 - erfc : erfc - 1;
}

/** Mean of a continuous Bernoulli, matching PyTorch including its flat patch near one half. */
export function cbMean(logit) {
  const p = 1 / (1 + Math.exp(-logit));
  const outside = p <= 0.499 || p > 0.501;
  if (!outside) return 0.5;
  return p / (2 * p - 1) + 1 / (Math.log1p(-p) - Math.log(p));
}

/**
 * Best set of note intervals for one pitch.
 * score: Float32Array holding a [T, T] table for this pitch, indexed (end * T + begin).
 * Returns pairs of [beginFrame, endFrame].
 */
export function viterbi(score, T, forcedStart) {
  const q = new Float32Array(T);
  const ptr = new Int32Array(T > 1 ? T - 1 : 1);
  const diag = new Float32Array(T);
  for (let i = 0; i < T; i++) diag[i] = score[i * T + i];
  q[T - 1] = diag[T - 1] > 0 ? diag[T - 1] : 0;
  for (let i = 1; i < T; i++) {
    const col = T - i - 1;                       // begin position under test
    let best = q[T - i];                         // skip this position (the noise score is zero)
    let sel = -1;
    for (let j = 0; j < i; j++) {
      const v = q[T - i + j] + score[(T - i + j) * T + col];
      if (v > best) { best = v; sel = j; }
    }
    ptr[i - 1] = sel;
    q[col] = best + (diag[col] > 0 ? diag[col] : 0);
  }
  const out = [];
  let j = forcedStart;
  while (j < T - 1) {
    const sel = ptr[T - j - 2];
    if (diag[j] > 0) out.push([j, j]);
    if (sel < 0) {
      j += 1;
    } else {
      const end = sel + j + 1;
      out.push([j, end]);
      j = end;
    }
  }
  if (diag[T - 1] > 0) out.push([T - 1, T - 1]);
  return out;
}

/** Linear, GELU, Linear. The two small heads the model ends with. */
function mlp(input, rows, head) {
  const [[h, inSize], , [outSize]] = head.shapes;
  const { w0, b0, w1, b1 } = head;
  const hidden = new Float32Array(h);
  const out = new Float32Array(rows * outSize);
  for (let r = 0; r < rows; r++) {
    const base = r * inSize;
    for (let i = 0; i < h; i++) {
      let sum = b0[i];
      const wrow = i * inSize;
      for (let k = 0; k < inSize; k++) sum += w0[wrow + k] * input[base + k];
      hidden[i] = 0.5 * sum * (1 + erf(sum / Math.SQRT2));
    }
    for (let o = 0; o < outSize; o++) {
      let sum = b1[o];
      const wrow = o * h;
      for (let k = 0; k < h; k++) sum += w1[wrow + k] * hidden[k];
      out[r * outSize + o] = sum;
    }
  }
  return out;
}

function splitHead(flat, shapes) {
  let at = 0;
  const take = (n) => { const v = flat.subarray(at, at + n); at += n; return v; };
  return {
    shapes,
    w0: take(shapes[0][0] * shapes[0][1]),
    b0: take(shapes[1][0]),
    w1: take(shapes[2][0] * shapes[2][1]),
    b1: take(shapes[3][0]),
  };
}

export class Decoder {
  static async load(base = "models/") {
    const cfg = await (await fetch(base + "config.json")).json();
    const vel = new Float32Array(await (await fetch(base + "velocity_head.bin")).arrayBuffer());
    const off = new Float32Array(await (await fetch(base + "offset_head.bin")).arrayBuffer());
    return new Decoder(cfg, splitHead(vel, cfg.velocityHead), splitHead(off, cfg.offsetHead));
  }

  constructor(cfg, velocityHead, offsetHead) {
    this.cfg = cfg;
    this.velocityHead = velocityHead;
    this.offsetHead = offsetHead;
    this.pitches = cfg.pitches;
  }

  /**
   * One chunk: context in, notes out.
   * ctx: Float32Array [nPitch, T, D]. runScorer(ctxSlice, nPitches) returns Float32Array
   * [nPitches, T, T] and is normally the converted scorer on the graphics chip.
   */
  async chunk(ctx, T, runScorer, forcedStart, lastFrameIdx, onProgress) {
    const { ctxSize: D, scorerGroup: G, hopSize, fs } = this.cfg;
    const nP = this.pitches.length;
    const paths = new Array(nP);
    for (let start = 0; start < nP; start += G) {
      const count = Math.min(G, nP - start);
      const slice = ctx.subarray(start * T * D, (start + count) * T * D);
      const scores = await runScorer(slice, count);             // [count, T, T]
      for (let p = 0; p < count; p++) {
        paths[start + p] = viterbi(scores.subarray(p * T * T, (p + 1) * T * T), T, forcedStart[start + p]);
      }
      if (onProgress) onProgress((start + count) / nP);
    }

    const rows = [];
    for (let p = 0; p < nP; p++) for (const iv of paths[p]) rows.push([p, iv[0], iv[1]]);
    if (rows.length === 0) return { notes: [], lastP: new Array(nP).fill(0) };

    const feat = new Float32Array(rows.length * D * 3);
    for (let r = 0; r < rows.length; r++) {
      const [p, a, b] = rows[r];
      const aAt = (p * T + a) * D, bAt = (p * T + b) * D, at = r * D * 3;
      for (let k = 0; k < D; k++) {
        const va = ctx[aAt + k], vb = ctx[bAt + k];
        feat[at + k] = va;
        feat[at + D + k] = vb;
        feat[at + 2 * D + k] = va * vb;
      }
    }
    const velLogits = mlp(feat, rows.length, this.velocityHead);
    const offLogits = mlp(feat, rows.length, this.offsetHead);
    const velOut = this.velocityHead.shapes[3][0];
    const offOut = this.offsetHead.shapes[3][0], half = offOut / 2;

    const frameDur = hopSize / fs;
    const notes = [];
    const lastP = new Array(nP).fill(0);
    let n = 0;
    for (let p = 0; p < nP; p++) {
      let lastEnd = 0, curLastP = 0;
      for (const [a, b] of paths[p]) {
        let velocity = 0, bestLogit = -Infinity;
        for (let v = 0; v < velOut; v++) {
          const l = velLogits[n * velOut + v];
          if (l > bestLogit) { bestLogit = l; velocity = v; }
        }
        const ofA = Math.min(0.5, Math.max(-0.5, (cbMean(offLogits[n * offOut]) - 0.5) / 0.99));
        const ofB = Math.min(0.5, Math.max(-0.5, (cbMean(offLogits[n * offOut + 1]) - 0.5) / 0.99));
        const hasOnset = a > 0 || offLogits[n * offOut + half] > 0;
        const hasOffset = b < lastFrameIdx || offLogits[n * offOut + half + 1] > 0;
        let start = (a + ofA) * frameDur;
        let end = (b + ofB) * frameDur;
        if (start < lastEnd) start = lastEnd;
        if (end < start + 1e-8) end = start + 1e-8;
        lastEnd = end;
        notes.push({ start, end, pitch: this.pitches[p], velocity, hasOnset, hasOffset });
        if (hasOffset) curLastP = b;
        n++;
      }
      lastP[p] = curLastP;
    }
    notes.sort((x, y) => x.start - y.start || x.end - y.end || x.pitch - y.pitch);
    return { notes, lastP };
  }

  /** Merge a chunk's notes into the running result, the way transkun stitches chunks. */
  static merge(byPitch, notes, beginTime) {
    for (const e of notes) {
      e.start = Math.max(e.start + beginTime, 0);
      e.end = Math.max(e.end + beginTime, e.start);
      const list = byPitch.get(e.pitch) || [];
      if (!byPitch.has(e.pitch)) byPitch.set(e.pitch, list);
      if (list.length && e.start < list[list.length - 1].end) {
        if (e.hasOnset) {
          list[list.length - 1] = e;
        } else {
          const prev = list[list.length - 1];
          prev.hasOffset = e.hasOffset;
          prev.end = Math.max(e.end, prev.end);
        }
        continue;
      }
      if (e.hasOnset) list.push(e);
    }
  }

  /** Everything after the last chunk: close open notes, drop unfinished ones, fix overlaps. */
  static finish(byPitch) {
    const all = [];
    for (const list of byPitch.values()) {
      if (list.length) list[list.length - 1].hasOffset = true;
      for (const n of list) if (n.hasOffset) all.push(n);
    }
    all.sort((a, b) => a.start - b.start || a.end - b.end || a.pitch - b.pitch);
    const seen = new Map();
    for (let i = 0; i < all.length; i++) {
      const prev = seen.get(all[i].pitch);
      if (prev !== undefined && all[prev].end > all[i].start) all[prev].end = all[i].start;
      seen.set(all[i].pitch, i);
    }
    all.sort((a, b) => a.start - b.start || a.end - b.end || a.pitch - b.pitch);
    return {
      notes: all.filter((n) => !PEDAL_PITCHES.has(n.pitch) && n.pitch > 0),
      pedals: all.filter((n) => PEDAL_PITCHES.has(n.pitch)),
    };
  }
}
