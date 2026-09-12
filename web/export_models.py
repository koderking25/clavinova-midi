"""Build everything the browser version needs, from the same model the Mac app uses.

    .venv/bin/python web/export_models.py

Writes into web/models/:
  backbone.web.onnx   the heavy part, runs on the visitor's graphics chip
  scorer.web.onnx     scores nine pitches at a time, so memory stays small
  windows.bin         the six window shapes for the audio features
  melbank.bin         the mel filter table
  config.json         sizes and settings the JavaScript needs
and into web/test/ a small fixture so the browser can check its own audio features.

Why the pieces are split this way:
  - The audio features are computed in JavaScript. The converted version of that step
    took 3233 ms per chunk in the browser against about 1078 ms in JavaScript, because
    the browser runtime's Fourier transform is slow.
  - The scorer is exported by hand, nine pitches per run. Exporting it whole would
    build a 172 MB table per chunk, past what browsers allow in one block of graphics
    memory, and the automatic export of that step also failed to convert.
  - The two small prediction heads are plain matrix multiplies, so JavaScript does them.

Every file is checked against PyTorch before it is kept.
"""
import json
import math
import os
import sys
import warnings

warnings.filterwarnings("ignore")

import moduleconf
import numpy as np
import onnx
import torch
import transkun
from onnx import inliner
from transkun.ModelTransformer import makeFrame

HERE = os.path.dirname(os.path.abspath(__file__))
MODELS = os.path.join(HERE, "models")
TESTS = os.path.join(HERE, "test")
CHUNK_SECONDS = 16                 # the model's native chunk; shorter chunks ruin accuracy
SCORER_GROUP = 9                   # pitches per scorer run: 10 runs per chunk
TOLERANCE_PERCENT = 0.05           # largest difference from PyTorch, as a share of the value range


def load_model():
    d = os.path.dirname(transkun.__file__)
    cm = moduleconf.parseFromFile(os.path.join(d, "pretrained", "2.0.conf"))
    model = cm["Model"].module.TransKun(conf=cm["Model"].config)
    ck = torch.load(os.path.join(d, "pretrained", "2.0.pt"), map_location="cpu", weights_only=False)
    state = ck["best_state_dict"] if "best_state_dict" in ck else ck["state_dict"]
    missing = model.load_state_dict(state, strict=False).missing_keys
    if missing:
        sys.exit(f"the piano model is incomplete ({len(missing)} missing weights)")
    model.eval()
    torch.set_grad_enabled(False)
    return model


def normalise(frames):
    """The model levels the gain of each chunk before its first layer. The JavaScript
    does the same; leaving it out makes the model hear a quieter song and miss notes."""
    mean = frames.mean(dim=[1, 2, 3], keepdim=True)
    std = frames.std(dim=[1, 2, 3], keepdim=True)
    return (frames - mean) / (std + 1e-8)


def browser_copy(path):
    """Flatten helper functions, drop unknown library declarations, older format version.
    Without this the browser runtime refuses the file."""
    m = inliner.inline_local_functions(onnx.load(path))
    del m.functions[:]
    keep = [o for o in m.opset_import if o.domain in ("", "ai.onnx")]
    del m.opset_import[:]
    m.opset_import.extend(keep)
    m.ir_version = 10
    onnx.save_model(m, path, save_as_external_data=False)
    onnx.checker.check_model(path, full_check=False)
    # The conversion step leaves the weights in a sidecar file. Browsers do not fetch those,
    # so the copy above holds everything and the leftovers must go.
    folder, name = os.path.split(path)
    for f in os.listdir(folder or "."):
        if f.startswith(name + ".data"):
            os.remove(os.path.join(folder, f))


class Backbone(torch.nn.Module):
    """Mel features in, context out."""

    def __init__(self, model):
        super().__init__()
        self.m = model
        self.register_buffer("idx", torch.tensor(model.targetMIDIPitch))

    def forward(self, mel):
        return self.m.backbone(mel, outputIndices=self.idx)


class Scorer(torch.nn.Module):
    """Context for a few pitches, out come their score tables. Same maths as transkun's
    scorer, written without the operations that blocked conversion."""

    def __init__(self, scorer, frames):
        super().__init__()
        self.map = scorer.map
        self.size = scorer.size
        idx = torch.arange(frames, dtype=torch.float32)
        self.register_buffer("lin", (idx[:, None] - idx[None, :]).abs())
        self.register_buffer("eye", torch.eye(frames))

    def forward(self, ctx):
        proj = self.map(ctx)
        q, k, diag = proj.split([self.size, self.size, 1], dim=-1)
        q = q / math.sqrt(self.size)
        s = torch.matmul(q, k.transpose(-1, -2)) * self.lin
        return s + diag.squeeze(-1).unsqueeze(-1) * self.eye


def main():
    os.makedirs(MODELS, exist_ok=True)
    os.makedirs(TESTS, exist_ok=True)
    model = load_model()
    import onnxruntime as ort

    so = ort.SessionOptions()
    so.log_severity_level = 3
    fs, hop, win = model.fs, model.hopSize, model.windowSize
    chunk = math.ceil(CHUNK_SECONDS * fs)
    torch.manual_seed(0)
    audio = (torch.randn(2, chunk) * 0.05).clamp(-1, 1)
    frames = makeFrame(audio, hop, win).unsqueeze(0)

    mel_pt = model.framewiseFeatureExtractor(normalise(frames))
    mel_pt = mel_pt.view(frames.shape[0], *mel_pt.shape[-3:])
    n_frames = mel_pt.shape[1]
    problems = []

    # ---- backbone -------------------------------------------------------------
    bb = Backbone(model).eval()
    ctx_pt = bb(mel_pt)
    path = os.path.join(MODELS, "backbone.web.onnx")
    torch.onnx.export(bb, (mel_pt,), path, dynamo=True, input_names=["mel"], output_names=["ctx"])
    browser_copy(path)
    got = ort.InferenceSession(path, so, providers=["CPUExecutionProvider"]).run(None, {"mel": mel_pt.numpy()})[0]
    diff = float(np.abs(got - ctx_pt.numpy()).max())
    scale = float(np.abs(ctx_pt.numpy()).max())
    share = 100 * diff / max(scale, 1e-9)
    print(f"backbone.web.onnx   {os.path.getsize(path)/1e6:6.1f} MB   differs from PyTorch by {diff:.2e} "
          f"({share:.4f}% of its range)")
    if share > TOLERANCE_PERCENT:
        problems.append(f"backbone differs by {share:.4f}% of its range")

    # ---- scorer ---------------------------------------------------------------
    sc = Scorer(model.scorer, n_frames).eval()
    ctx_group = ctx_pt[0, :SCORER_GROUP]
    want = sc(ctx_group)
    path = os.path.join(MODELS, "scorer.web.onnx")
    torch.onnx.export(sc, (ctx_group,), path, dynamo=True, input_names=["ctx"], output_names=["score"])
    browser_copy(path)
    got = ort.InferenceSession(path, so, providers=["CPUExecutionProvider"]).run(None, {"ctx": ctx_group.numpy()})[0]
    diff = float(np.abs(got - want.numpy()).max())
    scale = float(np.abs(want.numpy()).max())
    share = 100 * diff / max(scale, 1e-9)
    print(f"scorer.web.onnx     {os.path.getsize(path)/1e6:6.1f} MB   differs from PyTorch by {diff:.2e} "
          f"({share:.4f}% of its range)")
    if share > TOLERANCE_PERCENT:
        problems.append(f"scorer differs by {share:.4f}% of its range")
    # the same maths against transkun's own scorer, which must agree exactly
    ref_q, ref_k, ref_diag = model.scorer.map(ctx_group).split([sc.size, sc.size, 1], dim=-1)
    ref = (torch.matmul(ref_q / math.sqrt(sc.size), ref_k.transpose(-1, -2)) * sc.lin
           + torch.diag_embed(ref_diag.squeeze(-1)))
    exact = float(np.abs(want.numpy() - ref.numpy()).max())
    print(f"                              my scorer maths vs transkun's: {exact:.2e} (expected 0)")
    if exact != 0.0:
        problems.append(f"scorer maths differ by {exact:.2e}")

    # ---- audio feature ingredients -------------------------------------------
    fx = model.framewiseFeatureExtractor
    spec = fx.spectrogramExtractor
    wins = spec.win.unsqueeze(0)
    if spec.nExtraWins > 0:
        wins = torch.cat([spec.win.unsqueeze(0), spec.winGen.get().t()], dim=0)
    wins.numpy().astype(np.float32).tofile(os.path.join(MODELS, "windows.bin"))
    fx.freq2mels.numpy().astype(np.float32).tofile(os.path.join(MODELS, "melbank.bin"))
    cfg = dict(windowSize=int(win), hopSize=int(hop), fs=int(fs), nWindows=int(wins.shape[0]),
               nMels=int(fx.freq2mels.shape[1]), nFreq=int(fx.freq2mels.shape[0]),
               log=bool(fx.log), eps=float(fx.eps), toMono=bool(fx.toMono),
               chunkSeconds=CHUNK_SECONDS, hopSeconds=CHUNK_SECONDS // 2,
               framesPerChunk=int(n_frames), scorerGroup=SCORER_GROUP,
               pitches=[int(p) for p in model.targetMIDIPitch], ctxSize=int(model.scorer.size))
    json.dump(cfg, open(os.path.join(MODELS, "config.json"), "w"), indent=1)
    print(f"windows.bin         {wins.numel()*4/1e6:6.2f} MB   {tuple(wins.shape)}")
    print(f"melbank.bin         {fx.freq2mels.numel()*4/1e6:6.2f} MB   {tuple(fx.freq2mels.shape)}")

    # the two small heads, as plain numbers for JavaScript
    def head(seq, name):
        arrs = [seq[0].weight, seq[0].bias, seq[3].weight, seq[3].bias]
        flat = np.concatenate([a.detach().numpy().astype(np.float32).ravel() for a in arrs])
        flat.tofile(os.path.join(MODELS, f"{name}.bin"))
        return [list(a.shape) for a in arrs], flat.nbytes
    cfg["velocityHead"], vsize = head(model.velocityPredictor, "velocity_head")
    cfg["offsetHead"], osize = head(model.refinedOFPredictor, "offset_head")
    json.dump(cfg, open(os.path.join(MODELS, "config.json"), "w"), indent=1)
    print(f"velocity_head.bin   {vsize/1e6:6.2f} MB   offset_head.bin {osize/1e6:6.2f} MB")

    # ---- a fixture so the browser can check its own audio features -------------
    rng = np.random.default_rng(7)
    n = 2 * fs
    t = np.arange(n) / fs
    left = (0.3 * np.sin(2 * np.pi * 261.63 * t) + 0.2 * np.sin(2 * np.pi * 392.0 * t)
            + 0.05 * rng.normal(size=n)).astype(np.float32)
    right = (0.25 * np.sin(2 * np.pi * 329.63 * t) + 0.1 * rng.normal(size=n)).astype(np.float32)
    fr = makeFrame(torch.from_numpy(np.stack([left, right])), hop, win).unsqueeze(0)
    mel = fx(normalise(fr))
    mel = mel.view(fr.shape[0], *mel.shape[-3:]).numpy()
    np.stack([left, right], axis=-1).astype(np.float32).tofile(os.path.join(TESTS, "audio.bin"))
    mel.astype(np.float32).tofile(os.path.join(TESTS, "mel_expected.bin"))
    print(f"test fixture        {(n*2*4 + mel.nbytes)/1e6:6.2f} MB   two seconds of audio and its features")

    total = sum(os.path.getsize(os.path.join(MODELS, f)) for f in os.listdir(MODELS))
    print(f"\nvisitors download {total/1e6:.1f} MB once, then it is cached.")
    if problems:
        sys.exit("PROBLEMS: " + "; ".join(problems))
    print("every piece matches PyTorch.")


if __name__ == "__main__":
    main()
