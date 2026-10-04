"""
Plays the same JARVIS line in several British voices/settings so you can pick the closest one.
Run:  python voice_test.py
"""
import asyncio, os, tempfile, time
import edge_tts
import pygame

LINE = ("Good evening, sir. All systems are online. I've taken the liberty of checking the weather; "
        "it's clear tonight. Shall I begin?")

# (number, voice, rate, pitch, description)
CANDIDATES = [
    (1, "en-GB-RyanNeural",   "-5%",  "-4Hz",  "Ryan, current setting"),
    (2, "en-GB-RyanNeural",   "-10%", "-10Hz", "Ryan, deeper and slower"),
    (3, "en-GB-RyanNeural",   "+0%",  "-15Hz", "Ryan, much deeper, normal speed"),
    (4, "en-GB-ThomasNeural", "-5%",  "-4Hz",  "Thomas, smooth older butler"),
    (5, "en-GB-ThomasNeural", "-8%",  "-10Hz", "Thomas, deeper and calmer"),
    (6, "en-GB-ThomasNeural", "+5%",  "+0Hz",  "Thomas, crisp and quick"),
    (7, "en-GB-OliverNeural", "-5%",  "-6Hz",  "Oliver, softer British"),
    (8, "en-GB-AlfieNeural",  "-5%",  "-6Hz",  "Alfie, younger British"),
]


async def make(voice, rate, pitch, path):
    await edge_tts.Communicate(LINE, voice, rate=rate, pitch=pitch).save(path)


def play(path):
    pygame.mixer.music.load(path)
    pygame.mixer.music.play()
    while pygame.mixer.music.get_busy():
        time.sleep(0.05)
    pygame.mixer.music.unload()


def main():
    pygame.mixer.init()
    tmp = tempfile.gettempdir()
    for n, voice, rate, pitch, desc in CANDIDATES:
        path = os.path.join(tmp, f"jarvis_test_{n}.mp3")
        print(f"\n#{n}  {desc}")
        print(f"     EDGE_VOICE={voice}\n     EDGE_RATE={rate}\n     EDGE_PITCH={pitch}")
        try:
            asyncio.run(make(voice, rate, pitch, path))
            play(path)
        except Exception as e:
            print(f"     (couldn't play: {e})")
        time.sleep(0.8)
    print("\nDone. Copy the 3 lines under your favorite into .env, then restart JARVIS.")


if __name__ == "__main__":
    main()
