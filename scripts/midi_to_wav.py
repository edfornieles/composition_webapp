"""
Render a MIDI file to WAV using the same piano synthesis as the in-browser
Noise Studio generator (additive harmonics + hammer-noise attack + exponential
body decay). Output drops into composition_audio_sources/noise_studio/ so it
shows up in the Audio Library picker.

Usage:
    python3 scripts/midi_to_wav.py path/to/in.mid [--bpm 72] [--name out.wav]

Options:
    --bpm N    Override original tempo and re-time to N BPM (lossless — unlike
               audio time-stretch). Default: keep original tempo.
    --name F   Output filename inside noise_studio/. Default: derived from input.
    --gain G   Overall output gain (default 0.4 — leaves headroom for processing)

The renderer matches Web Audio Studio's piano timbre exactly so MIDI-rendered
audio sits alongside our generated ostinatos with consistent character. Run it
through the Noise Studio's PERFECT LIVES or EASTMAN preset for finished beds.
"""
from __future__ import annotations

import argparse
import math
import os
import struct
import sys
import wave
from pathlib import Path

# Project root on sys.path so this script works from any cwd
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mido  # noqa: E402
import numpy as np  # noqa: E402

SAMPLE_RATE = 44100
TAIL_SECONDS = 1.6  # exponential body decay length (matches JS DECAY_SEC)

# Partial structure — identical to noise_studio.html's renderNote().
PARTIALS = [
    (1.000, 1.00, 1.00),
    (2.005, 0.55, 0.80),
    (3.012, 0.30, 0.65),
    (4.025, 0.16, 0.50),
    (5.040, 0.08, 0.40),
    (6.060, 0.04, 0.30),
]


def midi_to_freq(note: int) -> float:
    return 440.0 * 2 ** ((note - 69) / 12)


def render_midi(midi_path: Path, target_bpm: float | None = None, gain: float = 0.4) -> bytes:
    """Render the MIDI to a mono WAV byte string at 44.1 kHz.

    Vectorized numpy synthesis — ~100-1000x faster than per-sample Python loops.
    """
    mf = mido.MidiFile(str(midi_path))

    # Collect (onset_sec, note, velocity, duration_sec) tuples across all tracks.
    # mido handles tempo events and ticks-per-beat for us via play(); but we want
    # absolute times even before playback starts, so iterate manually.
    notes: list[tuple[float, int, int, float]] = []
    # Build a flat event list with absolute time in seconds.
    events: list[tuple[float, mido.Message]] = []
    for track in mf.tracks:
        t = 0
        tempo = 500_000  # default MIDI tempo (120 BPM)
        for msg in track:
            t += msg.time
            if msg.type == "set_tempo":
                tempo = msg.tempo
            # Resolve absolute time for this msg using the current tempo.
            # (mido's natural iter does this via .play but we want notes/track.)
            sec = mido.tick2second(t, mf.ticks_per_beat, tempo)
            events.append((sec, msg))

    # Track on/off pairing per (track, channel, note) — keep it simple: per note.
    open_notes: dict[tuple[int, int], tuple[float, int]] = {}
    for sec, msg in events:
        if msg.type == "note_on" and msg.velocity > 0:
            open_notes[(msg.channel, msg.note)] = (sec, msg.velocity)
        elif msg.type in ("note_off",) or (msg.type == "note_on" and msg.velocity == 0):
            key = (msg.channel, msg.note)
            if key in open_notes:
                start, vel = open_notes.pop(key)
                duration = max(0.05, sec - start)
                notes.append((start, msg.note, vel, duration))

    # Close any dangling notes at end-of-file with a default duration.
    for (chan, note), (start, vel) in open_notes.items():
        notes.append((start, note, vel, 0.5))

    if not notes:
        raise RuntimeError("MIDI contained no playable notes.")

    # Compute end time and optionally rescale to a target BPM.
    max_end = max(start + dur for start, _, _, dur in notes) + TAIL_SECONDS
    if target_bpm is not None:
        # Estimate original "effective" tempo from the median note-spacing.
        starts = sorted(s for s, _, _, _ in notes)
        spacings = [b - a for a, b in zip(starts, starts[1:]) if b - a > 0.01]
        median_spacing = sorted(spacings)[len(spacings) // 2] if spacings else 0.5
        # Heuristic: assume the median spacing corresponds to one beat at the original
        # tempo. Scale all times by (target_beat_sec / original_beat_sec).
        original_beat_sec = median_spacing
        target_beat_sec = 60.0 / target_bpm
        scale = target_beat_sec / original_beat_sec
        notes = [(s * scale, n, v, d * scale) for s, n, v, d in notes]
        max_end *= scale

    total_samples = int(math.ceil(max_end * SAMPLE_RATE)) + int(SAMPLE_RATE * 0.5)
    buf = np.zeros(total_samples, dtype=np.float32)

    # Pre-compute the per-sample t array for the maximum-length note. Each note
    # uses a prefix of this — same memory reused for every note.
    max_note_samples = int(math.ceil(TAIL_SECONDS * SAMPLE_RATE))
    t = np.arange(max_note_samples, dtype=np.float32) / SAMPLE_RATE
    attack_env_full = np.minimum(1.0, t / 0.004)
    hammer_env_full = np.exp(-t / 0.012)
    # The hammer's noise content is the same shape; randomize once per call.
    rng = np.random.default_rng()

    # Pre-compute the decay envelopes per partial (same across all notes — they
    # only differ per-note in frequency and overall velocity scale).
    decay_envs = []
    for mult, amp, decay_mul in PARTIALS:
        dec = TAIL_SECONDS * decay_mul
        decay_envs.append((mult, amp, np.exp(-t / dec).astype(np.float32)))

    print(f"Rendering {len(notes)} notes → {max_end:.1f}s of audio at {SAMPLE_RATE} Hz ...")
    for k, (start_sec, note, vel, _dur) in enumerate(notes):
        velocity = (vel / 127.0) * 0.92
        start = int(start_sec * SAMPLE_RATE)
        if start < 0 or start >= total_samples:
            continue
        note_len = min(total_samples - start, max_note_samples)
        if note_len <= 0:
            continue
        tslice = t[:note_len]
        freq = midi_to_freq(note)
        # Hammer noise burst (different per note).
        hammer = (rng.random(note_len, dtype=np.float32) * 2 - 1) * hammer_env_full[:note_len] * 0.18
        # Sum all 6 partials. sin(2πfn × mult × t) for each partial, weighted by amp × decay envelope.
        body = np.zeros(note_len, dtype=np.float32)
        for mult, amp, env in decay_envs:
            body += amp * env[:note_len] * np.sin((2.0 * math.pi * freq * mult) * tslice)
        sample = (body * 0.20 + hammer) * velocity * attack_env_full[:note_len]
        # Mix in with soft-clip; we'll do the final clamp on the whole buffer.
        buf[start:start + note_len] += sample
        if k % 500 == 0 and k > 0:
            print(f"  {k}/{len(notes)} notes rendered")

    # Normalize and convert to 16-bit PCM.
    peak = float(np.max(np.abs(buf)))
    if peak < 1e-9:
        peak = 1.0
    buf *= (gain / peak)
    np.clip(buf, -1.0, 1.0, out=buf)
    pcm = (buf * 32767.0).astype(np.int16)
    return pcm.tobytes()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("midi", help="Input MIDI file")
    parser.add_argument("--bpm", type=float, default=None, help="Re-time output to this BPM (default: keep original)")
    parser.add_argument("--name", default=None, help="Output filename inside noise_studio/ (default: derived)")
    parser.add_argument("--gain", type=float, default=0.4)
    parser.add_argument("--out-dir", default=None,
                        help="Override destination (default: composition_audio_sources/noise_studio/)")
    args = parser.parse_args()

    midi_path = Path(args.midi).expanduser().resolve()
    if not midi_path.is_file():
        print(f"Not a file: {midi_path}", file=sys.stderr)
        return 1

    if args.out_dir:
        out_dir = Path(args.out_dir).expanduser().resolve()
    else:
        # Resolve project root from this script's location
        out_dir = Path(__file__).resolve().parent.parent / "composition_audio_sources" / "noise_studio"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_name = args.name or (midi_path.stem + (f"_{int(args.bpm)}bpm" if args.bpm else "") + ".wav")
    out_path = out_dir / out_name

    pcm = render_midi(midi_path, target_bpm=args.bpm, gain=args.gain)
    with wave.open(str(out_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm)

    print(f"Wrote {out_path}  ({len(pcm) // 2} samples, {len(pcm) / (SAMPLE_RATE * 2):.1f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
