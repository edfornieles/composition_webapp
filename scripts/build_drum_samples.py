#!/usr/bin/env python3
"""Synthesize drum one-shot samples for the Composition Studio.

Generates 5 kits × 8 voices × 3 round-robin variants = 120 samples. Each
voice/kit pair gets three subtly-different takes (seeded RNG) so the in-browser
sequencer can rotate through them per hit to avoid the "machine-gun" feel.

Output layout:
    djangoscrap/static/audio/drums/<kit>/<voice>_<n>.wav

Kits:
    acoustic   — warm, natural balance
    808        — sub-heavy synth (Roland TR-808 inspired)
    909        — punchier, crisper (Roland TR-909 inspired)
    lofi       — low-pass + bitcrush + slight pitch wobble
    industrial — distorted, gated, harsh

Each kit is the same synthesis function with kit-specific param tweaks. Round-
robin variants come from different RNG seeds.
"""
from __future__ import annotations
import math
import struct
import wave
from pathlib import Path

import numpy as np
from scipy import signal

SR = 44100
OUT_DIR = Path(__file__).resolve().parent.parent / "djangoscrap" / "static" / "audio" / "drums"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# Kit parameter overrides. Each entry is a partial dict applied per voice;
# missing keys fall back to "acoustic" defaults.
KITS = {
    "acoustic": {},  # baseline
    "808": {
        "kick":       {"sweep_from": 110, "sweep_to": 40, "sweep_ms": 90, "decay": 0.32, "click_amt": 0.35, "sub_amt": 0.55, "clip": 1.0, "lp": 6000},
        "snare":      {"tone_decay": 0.06, "noise_decay": 0.20, "noise_color": (700, 7000), "snap_amt": 0.25},
        "hat_closed": {"decay": 0.05, "bandpass": (8000, 12000)},
        "hat_open":   {"decay": 0.32, "bandpass": (8000, 13000)},
        "clap":       {},
        "tom_low":    {"f0": 75, "decay": 0.35},
        "tom_mid":    {"f0": 140, "decay": 0.30},
        "rim":        {},
    },
    "909": {
        "kick":       {"sweep_from": 160, "sweep_to": 55, "sweep_ms": 50, "decay": 0.20, "click_amt": 0.75, "sub_amt": 0.25, "clip": 1.6, "lp": 9000},
        "snare":      {"tone_decay": 0.05, "noise_decay": 0.10, "noise_color": (600, 9000), "snap_amt": 0.6},
        "hat_closed": {"decay": 0.025, "bandpass": (9000, 14000)},
        "hat_open":   {"decay": 0.22, "bandpass": (8500, 14000)},
        "clap":       {},
        "tom_low":    {"f0": 95, "decay": 0.22},
        "tom_mid":    {"f0": 180, "decay": 0.22},
        "rim":        {},
    },
    "lofi": {
        "_postfx": "lofi",  # applied to every voice in this kit
        "kick":       {"sweep_from": 100, "sweep_to": 45, "decay": 0.30},
        "snare":      {"tone_decay": 0.07, "noise_decay": 0.13},
        "hat_closed": {"bandpass": (6000, 9000)},
        "hat_open":   {"bandpass": (6000, 10000)},
        "clap":       {},
        "tom_low":    {},
        "tom_mid":    {},
        "rim":        {},
    },
    "industrial": {
        "_postfx": "industrial",  # applied to every voice
        "kick":       {"sweep_from": 140, "sweep_to": 35, "click_amt": 0.9, "clip": 2.5, "lp": 9000},
        "snare":      {"snap_amt": 0.8, "noise_decay": 0.18},
        "hat_closed": {"bandpass": (6000, 14000), "decay": 0.04},
        "hat_open":   {"bandpass": (6000, 14000), "decay": 0.35},
        "clap":       {},
        "tom_low":    {"f0": 80, "decay": 0.30},
        "tom_mid":    {"f0": 150, "decay": 0.28},
        "rim":        {},
    },
}

# ─── DSP helpers ───────────────────────────────────────────────────────────
def env(samples: int, attack: float, decay: float, shape: float = 1.0) -> np.ndarray:
    a = max(1, int(attack * SR))
    d = max(1, samples - a)
    atk = np.linspace(0.0, 1.0, a)
    t = np.arange(d) / SR
    dec = np.exp(-t / max(0.0005, decay)) ** shape
    return np.concatenate([atk, dec])[:samples]

def soft_clip(x, drive=1.0): return np.tanh(x * drive)
def hp(x, fc, order=4): return signal.sosfilt(signal.butter(order, fc / (SR/2), btype="highpass", output="sos"), x).astype(np.float32)
def lp(x, fc, order=4): return signal.sosfilt(signal.butter(order, fc / (SR/2), btype="lowpass", output="sos"), x).astype(np.float32)
def bp(x, lo, hi, order=4): return signal.sosfilt(signal.butter(order, [lo/(SR/2), hi/(SR/2)], btype="bandpass", output="sos"), x).astype(np.float32)

def normalize(x, target_peak=0.95):
    pk = float(np.max(np.abs(x)) or 1e-9)
    return (x * (target_peak / pk)).astype(np.float32)

def write_wav(path, x):
    x = normalize(x, 0.95)
    pcm = np.clip(x * 32767.0, -32768, 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR)
        w.writeframes(pcm.tobytes())

# Post-FX applied after voice synthesis (kit-wide).
def apply_postfx(x, kind):
    if kind == "lofi":
        # Bitcrush to ~10 bits + low-pass + slight pitch wobble.
        levels = 1024  # 10-bit
        x = np.round(x * (levels / 2)) / (levels / 2)
        x = lp(x, 6000)
        # Subtle vibrato (low-rate pitch modulation via resample).
        t = np.arange(len(x)) / SR
        mod = 1.0 + 0.003 * np.sin(2 * np.pi * 5 * t)
        # Approximate by tiny linear interpolation along varying step.
        out_idx = np.cumsum(mod)
        out_idx = out_idx - out_idx[0]
        out_idx = np.clip(out_idx, 0, len(x) - 1)
        x = np.interp(out_idx, np.arange(len(x)), x).astype(np.float32)
        return x
    elif kind == "industrial":
        # Heavy saturation + bandpass + gate.
        x = soft_clip(x, 4.0)
        x = bp(x, 200, 8000)
        # Gate: zero out below noise floor.
        env_local = np.abs(x)
        thresh = np.max(env_local) * 0.08
        x = np.where(env_local > thresh, x, x * 0.1)
        return x
    return x

# ─── Voice synthesizers ────────────────────────────────────────────────────
def kick(p=None):
    p = p or {}
    sweep_from = p.get("sweep_from", 130.0)
    sweep_to = p.get("sweep_to", 45.0)
    sweep_ms = p.get("sweep_ms", 60.0)
    decay = p.get("decay", 0.18)
    click_amt = p.get("click_amt", 0.55)
    sub_amt = p.get("sub_amt", 0.35)
    clip = p.get("clip", 1.2)
    lp_freq = p.get("lp", 8000)
    dur = max(0.30, decay * 2.0)
    n = int(dur * SR)
    t = np.arange(n) / SR
    sweep_t = np.minimum(t / (sweep_ms / 1000), 1.0)
    freq = sweep_from * np.exp(-sweep_t * np.log(sweep_from / sweep_to))
    phase = 2 * np.pi * np.cumsum(freq) / SR
    body = np.sin(phase) * env(n, 0.001, decay)
    click = (np.sin(2*np.pi*1800*t) * env(n, 0.0005, 0.006, 2.0)
             + np.random.randn(n).astype(np.float32) * env(n, 0.0001, 0.003, 2.0) * 0.6)
    click = hp(click, 600)
    sub_t = np.minimum(t / 0.025, 1.0)
    sub_freq = 70.0 * np.exp(-sub_t * np.log(70.0 / 30.0))
    sub_phase = 2 * np.pi * np.cumsum(sub_freq) / SR
    sub = np.sin(sub_phase) * env(n, 0.001, decay * 0.7) * sub_amt
    out = body * 1.0 + click * click_amt + sub
    out = soft_clip(out, clip)
    out = lp(out, lp_freq)
    return out

def snare(p=None):
    p = p or {}
    tone_decay = p.get("tone_decay", 0.08)
    noise_decay = p.get("noise_decay", 0.16)
    lo, hi = p.get("noise_color", (500, 10000))
    snap_amt = p.get("snap_amt", 0.4)
    dur = 0.4
    n = int(dur * SR)
    t = np.arange(n) / SR
    tone = (np.sin(2*np.pi*200*t)*0.6 + np.sin(2*np.pi*350*t)*0.4) * env(n, 0.001, tone_decay, 1.5)
    noise = np.random.randn(n).astype(np.float32)
    noise = bp(noise, lo, hi)
    noise *= env(n, 0.001, noise_decay)
    snap = bp(np.random.randn(n).astype(np.float32), 3000, 6000) * env(n, 0.0008, 0.025, 2.0) * snap_amt
    return soft_clip(tone * 0.5 + noise * 0.7 + snap, 1.1)

def hat_closed(p=None):
    p = p or {}
    decay = p.get("decay", 0.035)
    lo, hi = p.get("bandpass", (7500, 11000))
    dur = max(0.10, decay * 3)
    n = int(dur * SR)
    t = np.arange(n) / SR
    freqs = [320.0, 540.0, 745.0, 970.0, 1180.0, 1480.0]
    osc = np.zeros(n, dtype=np.float32)
    for f in freqs:
        osc += np.sin(2*np.pi*f*t) + 0.35 * np.sin(2*np.pi*3*f*t)
    osc /= len(freqs)
    osc = bp(osc, lo, hi)
    osc = osc * 0.7 + bp(np.random.randn(n).astype(np.float32), lo, hi + 1000) * 0.3
    osc *= env(n, 0.0005, decay, 2.5)
    return osc

def hat_open(p=None):
    p = p or {}
    decay = p.get("decay", 0.28)
    lo, hi = p.get("bandpass", (6500, 12000))
    dur = max(0.30, decay * 2)
    n = int(dur * SR)
    t = np.arange(n) / SR
    freqs = [320.0, 540.0, 745.0, 970.0, 1180.0, 1480.0]
    osc = np.zeros(n, dtype=np.float32)
    for f in freqs:
        osc += np.sin(2*np.pi*f*t) + 0.35 * np.sin(2*np.pi*3*f*t)
    osc /= len(freqs)
    osc = bp(osc, lo, hi)
    osc = osc * 0.7 + bp(np.random.randn(n).astype(np.float32), lo, hi + 1000) * 0.3
    osc *= env(n, 0.001, decay, 1.0)
    return osc

def tom(p=None):
    p = p or {}
    f0 = p.get("f0", 160.0)
    decay = p.get("decay", 0.25)
    f1 = f0 * 0.55
    dur = max(0.40, decay * 2)
    n = int(dur * SR)
    t = np.arange(n) / SR
    sweep_t = np.minimum(t / 0.15, 1.0)
    freq = f0 * np.exp(-sweep_t * np.log(f0 / f1))
    phase = 2 * np.pi * np.cumsum(freq) / SR
    body = (np.sin(phase) + 0.25 * np.sin(2 * phase)) * env(n, 0.001, decay)
    tap = bp(np.random.randn(n).astype(np.float32), 1500, 3500) * env(n, 0.0005, 0.008, 2.0) * 0.25
    return soft_clip(body + tap, 1.05)

def clap(p=None):
    dur = 0.4
    n = int(dur * SR)
    offsets_ms = [0, 11, 22, 35]
    out = np.zeros(n, dtype=np.float32)
    noise_master = bp(np.random.randn(n).astype(np.float32), 1000, 3500)
    for off_ms in offsets_ms:
        off = int(off_ms / 1000 * SR)
        burst_env = env(n, 0.0005, 0.012, 2.5)
        shifted = np.zeros(n, dtype=np.float32)
        shifted[off:] = burst_env[: n - off]
        out += noise_master * shifted
    tail = bp(np.random.randn(n).astype(np.float32), 1200, 3000) * env(n, 0.04, 0.22, 0.9) * 0.5
    return out * 0.9 + tail * 0.5

def rim(p=None):
    dur = 0.15
    n = int(dur * SR)
    t = np.arange(n) / SR
    tone = (np.sin(2*np.pi*900*t) + 0.6 * np.sin(2*np.pi*1750*t)) * env(n, 0.0003, 0.025, 2.0)
    pop = bp(np.random.randn(n).astype(np.float32), 1500, 4000) * env(n, 0.0001, 0.008, 2.5) * 0.6
    return tone * 0.7 + pop


VOICES = [
    ("kick",       kick),
    ("snare",      snare),
    ("hat_closed", hat_closed),
    ("hat_open",   hat_open),
    ("clap",       clap),
    ("tom_low",    lambda p: tom({**(p or {}), "f0": (p or {}).get("f0", 90), "decay": (p or {}).get("decay", 0.30)})),
    ("tom_mid",    lambda p: tom({**(p or {}), "f0": (p or {}).get("f0", 160), "decay": (p or {}).get("decay", 0.28)})),
    ("rim",        rim),
]

NUM_VARIANTS = 3  # round-robin samples per voice/kit

def main() -> None:
    print(f"Building drum samples into {OUT_DIR}\n")
    for kit_name, kit_params in KITS.items():
        kit_dir = OUT_DIR / kit_name
        kit_dir.mkdir(exist_ok=True)
        postfx = kit_params.get("_postfx")
        print(f"--- {kit_name} ---")
        for voice_id, voice_fn in VOICES:
            params = kit_params.get(voice_id)
            for variant in range(NUM_VARIANTS):
                # Seed per kit/voice/variant for deterministic-but-distinct samples.
                np.random.seed(hash((kit_name, voice_id, variant)) & 0xFFFFFFFF)
                x = voice_fn(params).astype(np.float32)
                if postfx:
                    x = apply_postfx(x, postfx)
                fname = f"{voice_id}_{variant}.wav"
                write_wav(kit_dir / fname, x)
            print(f"  {voice_id} × {NUM_VARIANTS}")
    print(f"\nDone — {sum(1 for _ in OUT_DIR.rglob('*.wav'))} samples total.")


if __name__ == "__main__":
    main()
