// Audio features: exactly what transkun's first layer does, written for the browser.
//
// Frames of 4096 samples every 1024 samples, six window shapes (one standard plus five
// learned), a Fourier transform of each, power, average of the two audio channels, then
// 229 mel bands and a log scale. Checked against PyTorch to 1.5e-07.
//
// Speed notes: the two audio channels ride in one Fourier transform (real and imaginary
// parts), and the mel filters are stored as short runs because they are mostly zeros.

const TWO_PI = Math.PI * 2;

class FFT {
  constructor(n) {
    this.n = n;
    this.levels = Math.log2(n) | 0;
    if (2 ** this.levels !== n) throw new Error("FFT size must be a power of two");
    this.cos = new Float32Array(n / 2);
    this.sin = new Float32Array(n / 2);
    for (let i = 0; i < n / 2; i++) {
      this.cos[i] = Math.cos((TWO_PI * i) / n);
      this.sin[i] = Math.sin((TWO_PI * i) / n);
    }
    this.rev = new Uint32Array(n);
    for (let i = 0; i < n; i++) {
      let x = i, r = 0;
      for (let j = 0; j < this.levels; j++) { r = (r << 1) | (x & 1); x >>= 1; }
      this.rev[i] = r;
    }
  }

  /** In-place complex transform of re/im (length n). */
  run(re, im) {
    const { n, rev, cos, sin } = this;
    for (let i = 0; i < n; i++) {
      const j = rev[i];
      if (j > i) {
        let t = re[i]; re[i] = re[j]; re[j] = t;
        t = im[i]; im[i] = im[j]; im[j] = t;
      }
    }
    for (let size = 2; size <= n; size *= 2) {
      const half = size / 2, step = n / size;
      for (let i = 0; i < n; i += size) {
        for (let j = i, k = 0; j < i + half; j++, k += step) {
          const l = j + half;
          const tre = re[l] * cos[k] + im[l] * sin[k];
          const tim = -re[l] * sin[k] + im[l] * cos[k];
          re[l] = re[j] - tre; im[l] = im[j] - tim;
          re[j] += tre; im[j] += tim;
        }
      }
    }
  }
}

export class Features {
  static async load(base = "models/") {
    const cfg = await (await fetch(base + "config.json")).json();
    const wins = new Float32Array(await (await fetch(base + "windows.bin")).arrayBuffer());
    const bank = new Float32Array(await (await fetch(base + "melbank.bin")).arrayBuffer());
    return new Features(cfg, wins, bank);
  }

  constructor(cfg, windows, bank) {
    this.cfg = cfg;
    this.windows = windows;                       // [nWindows, windowSize]
    this.fft = new FFT(cfg.windowSize);
    // Mel filters as runs of nonzero weights: each band touches only a slice of the spectrum.
    const { nFreq, nMels } = cfg;
    this.melStart = new Int32Array(nMels);
    this.melLen = new Int32Array(nMels);
    const weights = [];
    for (let m = 0; m < nMels; m++) {
      let first = -1, last = -1;
      for (let f = 0; f < nFreq; f++) {
        if (bank[f * nMels + m] !== 0) { if (first < 0) first = f; last = f; }
      }
      if (first < 0) { first = 0; last = -1; }
      this.melStart[m] = first;
      this.melLen[m] = last - first + 1;
      for (let f = first; f <= last; f++) weights.push(bank[f * nMels + m]);
    }
    this.melWeights = Float32Array.from(weights);
    this.melOffset = new Int32Array(nMels);
    for (let m = 1; m < nMels; m++) this.melOffset[m] = this.melOffset[m - 1] + this.melLen[m - 1];
  }

  /** Frame count for a run of samples, matching transkun's framing. */
  frameCount(nSamples) {
    return Math.ceil(nSamples / this.cfg.hopSize) + 1;
  }

  /**
   * left, right: Float32Array of one chunk (may be shorter than the chunk; the rest is silence).
   * Returns mel features laid out as [frames][mels][windows], which is what the backbone wants.
   */
  compute(left, right, nSamples = left.length) {
    const { windowSize: W, hopSize: H, nMels, nFreq, nWindows, eps, log } = this.cfg;
    const nFrames = this.frameCount(nSamples);
    const lPad = W >> 1;

    // Gain normalisation over the whole chunk, exactly as the model does before its
    // first layer: subtract the mean and divide by the spread of every frame sample,
    // both channels, padding included. Leaving this out makes the model hear a quieter
    // song and find fewer notes.
    let sum = 0, sumsq = 0;
    const count = nFrames * W * 2;
    for (let t = 0; t < nFrames; t++) {
      const base = t * H - lPad;
      for (let i = 0; i < W; i++) {
        const s = base + i;
        const a = s < 0 || s >= nSamples ? 0 : left[s];
        const b = s < 0 || s >= nSamples ? 0 : right[s];
        sum += a + b;
        sumsq += a * a + b * b;
      }
    }
    const mean = sum / count;
    const variance = Math.max(0, (sumsq - (sum * sum) / count) / (count - 1));  // same spread as PyTorch
    const norm = 1 / (Math.sqrt(variance) + 1e-8);
    const out = new Float32Array(nFrames * nMels * nWindows);
    const re = new Float32Array(W);
    const im = new Float32Array(W);
    const power = new Float32Array(nFreq);
    const logEps = Math.log(eps);
    const scale = 1 / W;                                   // matches the "ortho" transform
    for (let t = 0; t < nFrames; t++) {
      const base = t * H - lPad;                           // first sample of this frame
      for (let w = 0; w < nWindows; w++) {
        const win = w * W;
        for (let i = 0; i < W; i++) {
          const s = base + i;
          const g = this.windows[win + i];
          const inRange = s >= 0 && s < nSamples;
          // both channels ride in one transform; padding is normalised too, so it is not zero
          re[i] = ((inRange ? left[s] : 0) - mean) * norm * g;
          im[i] = ((inRange ? right[s] : 0) - mean) * norm * g;
        }
        this.fft.run(re, im);
        // Two real signals share one transform, so their combined power comes from
        // bin k and its mirror: |Z[k]|^2 + |Z[n-k]|^2 = 2(|L|^2 + |R|^2).
        for (let k = 0; k < nFreq; k++) {
          const j = k === 0 ? 0 : W - k;
          power[k] = (re[k] * re[k] + im[k] * im[k] + re[j] * re[j] + im[j] * im[j]) * 0.25 * scale;
        }
        for (let m = 0; m < nMels; m++) {
          const start = this.melStart[m], len = this.melLen[m], off = this.melOffset[m];
          let sum = 0;
          for (let i = 0; i < len; i++) sum += power[start + i] * this.melWeights[off + i];
          out[(t * nMels + m) * nWindows + w] = log ? (Math.log(sum + eps) - logEps) / -logEps : sum;
        }
      }
    }
    return { mel: out, nFrames };
  }
}
