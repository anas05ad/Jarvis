"""
Subtle "AI system" voice effect for JARVIS: a touch of warmth, a short metallic shimmer and a small-room tail,
like a voice coming from the house itself. Applied live to every audio chunk (24 kHz, 16-bit mono).
"""
import os
import numpy as np

ENABLED = os.getenv("VOICE_FX", "1") == "1"
AMOUNT = float(os.getenv("VOICE_FX_AMOUNT", "1.0"))
RATE = 24000

# (delay in ms, gain): very short taps = metallic shimmer, longer ones = room/speaker-system feel
TAPS = [(7, 0.16), (13, 0.11), (23, 0.08), (41, 0.06), (67, 0.04)]
_delays = [(int(RATE * ms / 1000), g * AMOUNT) for ms, g in TAPS]
_hist = np.zeros(max(d for d, _ in _delays), dtype=np.float32)
_lp = 0.0   # low-pass state for warmth


def process(data: bytes) -> bytes:
    global _hist, _lp
    if not ENABLED or not data:
        return data
    x = np.frombuffer(data[: len(data) // 2 * 2], dtype=np.int16).astype(np.float32)
    if x.size == 0:
        return data

    # echo taps that continue smoothly across chunks
    buf = np.concatenate([_hist, x])
    y = x.copy()
    n = x.size
    for d, g in _delays:
        start = _hist.size - d
        y += g * buf[start:start + n]
    _hist = buf[-_hist.size:]

    # gentle warmth: blend in a little low-passed signal
    lp = np.empty_like(y)
    a, prev = 0.12, _lp
    for i in range(0, n, 64):               # block-wise one-pole filter (fast enough in numpy)
        seg = y[i:i + 64]
        out = np.empty_like(seg)
        for j, v in enumerate(seg):
            prev += a * (v - prev)
            out[j] = prev
        lp[i:i + 64] = out
    _lp = prev
    y = 0.85 * y + 0.25 * AMOUNT * lp

    # keep the volume where it was, without clipping
    peak = np.max(np.abs(y)) or 1.0
    y *= min(1.0, 32000.0 / peak)
    return y.astype(np.int16).tobytes()
