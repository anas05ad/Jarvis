"""
Custom voice for JARVIS LIVE.
VOICE_ENGINE=ryan       -> free Microsoft Edge British voice (en-GB-RyanNeural by default)
VOICE_ENGINE=elevenlabs -> ElevenLabs voice (needs ELEVEN_API_KEY and ELEVEN_VOICE_ID)
Gemini still does the thinking and listening; its words are spoken sentence by sentence
with Microsoft's British voice en-GB-RyanNeural (Arabic uses ar-JO-TaimNeural).
"""
import os, re, io, json, asyncio, urllib.request

import numpy as np
import pygame
import edge_tts

VOICE_EN = os.getenv("EDGE_VOICE", "en-GB-RyanNeural")
VOICE_AR = os.getenv("EDGE_VOICE_AR", "ar-JO-TaimNeural")
RATE = os.getenv("EDGE_RATE", "-5%")      # a little slower = calmer, more butler-like
PITCH = os.getenv("EDGE_PITCH", "-4Hz")   # a little deeper
ENGINE = os.getenv("VOICE_ENGINE", "ryan").lower()
ELEVEN_KEY = os.getenv("ELEVEN_API_KEY", "")
ELEVEN_VOICE = os.getenv("ELEVEN_VOICE_ID", "")
ELEVEN_MODEL = os.getenv("ELEVEN_MODEL", "eleven_flash_v2_5")     # fast, and speaks Arabic too
ELEVEN_STABILITY = float(os.getenv("ELEVEN_STABILITY", "0.6"))     # higher = calmer, more even
ARABIC = re.compile(r"[\u0600-\u06FF]")
SENTENCE = re.compile(r"^(.+?[.!?؟…])\s+", re.S)


def _mp3_to_pcm24k(mp3: bytes) -> bytes:
    """Decode Edge's MP3 to 24 kHz 16-bit mono, the same format Gemini's voice used."""
    if not pygame.mixer.get_init():
        pygame.mixer.init()
    snd = pygame.mixer.Sound(file=io.BytesIO(mp3))
    arr = pygame.sndarray.array(snd)
    freq = pygame.mixer.get_init()[0]
    a = arr.astype(np.float32)
    if np.issubdtype(arr.dtype, np.floating):
        a *= 32767
    if a.ndim == 2:
        a = a.mean(axis=1)
    if freq != 24000 and a.size:
        n = int(a.size * 24000 / freq)
        a = np.interp(np.linspace(0, a.size - 1, n), np.arange(a.size), a)
    return np.clip(a, -32768, 32767).astype(np.int16).tobytes()


def _eleven_pcm24k(text: str) -> bytes:
    """ElevenLabs returns raw 24 kHz 16-bit PCM directly, no decoding needed."""
    req = urllib.request.Request(
        f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_VOICE}?output_format=pcm_24000",
        data=json.dumps({"text": text, "model_id": ELEVEN_MODEL,
                         "voice_settings": {"stability": ELEVEN_STABILITY, "similarity_boost": 0.8}}).encode(),
        headers={"xi-api-key": ELEVEN_KEY, "Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.read()
    except urllib.error.HTTPError as err:
        raise RuntimeError(f"ElevenLabs {err.code}: {err.read().decode(errors='ignore')[:200]}")


class EdgeVoice:
    def __init__(self, play_q: asyncio.Queue):
        self.play_q = play_q
        self.q: asyncio.Queue = asyncio.Queue()
        self.buf = ""
        self.gen = 0          # bumps on every interruption so stale sentences are dropped

    def feed(self, text: str):
        """Add a piece of JARVIS's reply; every finished sentence is sent to be spoken."""
        self.buf += text
        while (m := SENTENCE.match(self.buf)):
            self.q.put_nowait((self.gen, m.group(1).strip()))
            self.buf = self.buf[m.end():]

    def flush(self):
        if self.buf.strip():
            self.q.put_nowait((self.gen, self.buf.strip()))
        self.buf = ""

    def interrupt(self):
        self.gen += 1
        self.buf = ""
        while not self.q.empty():
            self.q.get_nowait()

    async def run(self):
        while True:
            gen, text = await self.q.get()
            if gen != self.gen:
                continue
            try:
                if ENGINE == "elevenlabs":
                    pcm = await asyncio.to_thread(_eleven_pcm24k, text)
                    if gen != self.gen:
                        continue
                    for i in range(0, len(pcm), 4800):
                        self.play_q.put_nowait(pcm[i:i + 4800])
                    continue
                voice = VOICE_AR if ARABIC.search(text) else VOICE_EN
                mp3 = b""
                async for chunk in edge_tts.Communicate(text, voice, rate=RATE, pitch=PITCH).stream():
                    if chunk["type"] == "audio":
                        mp3 += chunk["data"]
                pcm = await asyncio.to_thread(_mp3_to_pcm24k, mp3)
            except Exception as e:
                print(f"\n[voice] {e}")
                continue
            if gen != self.gen:       # you interrupted while it was being generated
                continue
            for i in range(0, len(pcm), 4800):   # 0.1 s pieces so interrupting stays snappy
                self.play_q.put_nowait(pcm[i:i + 4800])
