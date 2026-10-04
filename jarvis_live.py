"""
JARVIS LIVE - real-time voice conversation (English + Arabic), like the movie.
Talk naturally, interrupt him mid-sentence, and he controls your PC with the same tools as jarvis.py.

Run:  python jarvis_live.py
Use headphones (or set HALF_DUPLEX=1) so JARVIS doesn't hear himself and interrupt himself.
"""
import os, io, sys, time, base64, asyncio

import sounddevice as sd
from PIL import Image
from google import genai
from google.genai import types

# Reuse the tools, contacts and safety checks from the main JARVIS
from jarvis import TOOLS, run_tool, CONTACTS, OS_NAME
import hud
import voice_fx
import json
from datetime import datetime

API_KEY = os.environ["GEMINI_API_KEY"]
VOICE = os.getenv("LIVE_VOICE", "Charon")          # try also: Iapetus, Sadaltager, Rasalgethi, Orus
HALF_DUPLEX = os.getenv("HALF_DUPLEX", "0") == "1"
USE_HUD = os.getenv("HUD", "1") == "1"
USE_EDGE = os.getenv("VOICE_ENGINE", "gemini").lower() in ("ryan", "elevenlabs")   # custom voice engines

# Google renames Live models often, so we try them in order until one connects
MODELS = [m for m in [os.getenv("LIVE_MODEL"),
                      "gemini-3.8-live",
                      "gemini-3.1-flash-live-preview",
                      "gemini-2.5-flash-native-audio-preview-12-2025"] if m]

IN_RATE, OUT_RATE, CHUNK = 16000, 24000, 1024

client = genai.Client(api_key=API_KEY)

def make_config():
    system = f"""You are JARVIS, Tony Stark's AI: calm, witty, loyal, slightly sarcastic British butler. Call the user "sir".
VOICE AND DELIVERY (very important):
- Speak English with a crisp, refined, upper-class British accent (Received Pronunciation), like a polished English butler.
- Calm, smooth, unhurried, slightly low pitch. Never excited, never loud, never American.
- Dry, understated wit. Precise word choice: "Right away, sir." "Very good, sir." "As you wish." "I've taken the liberty of..."
- Short, elegant sentences with natural pauses. In Arabic, keep the same calm, polite, composed tone.
You are in a LIVE voice conversation on the user's {OS_NAME} computer. Current time: {datetime.now():%A %d %B %Y, %H:%M}.
- Answer in the language the user speaks (English or Arabic, including Jordanian dialect).
- Keep it natural and short, like real conversation. Never read out lists, code, or links.
- Actually DO tasks with your tools instead of explaining how.
- Be FAST: use the most direct tool (open_app, play_youtube, media, press_keys shortcuts), chain several actions in a row, and don't ask permission for normal tasks. Only take a screenshot when you truly need to see the screen.
- If a tool replies NOT DONE YET, ask the user out loud in one sentence; when they say yes, call it again with confirmed=true.
- To call someone, use make_call with their number from contacts. If you don't know the number, ask the user once and
  save_contact it; for a business, find the number with Google Search.
- Say who you're calling in a few words, then call. Stay silent during the call.
Known contacts: {json.dumps(CONTACTS, ensure_ascii=False)}"""

    return types.LiveConnectConfig(
    response_modalities=["AUDIO"],
    system_instruction=system,
    tools=([types.Tool(google_search=types.GoogleSearch())] if os.getenv("WEB_LOOKUP", "1") == "1" else []) + [types.Tool(function_declarations=[
        types.FunctionDeclaration(name=t["name"], description=t["description"],
                                  parameters_json_schema=t["input_schema"]) for t in TOOLS])],
    speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(
        prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=VOICE))),
    input_audio_transcription=types.AudioTranscriptionConfig(),
    output_audio_transcription=types.AudioTranscriptionConfig(),
    context_window_compression=types.ContextWindowCompressionConfig(sliding_window=types.SlidingWindow()),
)

play_q: asyncio.Queue = asyncio.Queue()
last_play = 0.0
speaker = None


def say_line(who, text):
    """Print live captions, switching label when the speaker changes."""
    global speaker
    if who != speaker:
        print(f"\n{who}: ", end="")
        speaker = who
    print(text, end="", flush=True)
    hud.send({"t": "cap", "who": who, "text": text})


def jarvis_talking():
    return not play_q.empty() or time.time() - last_play < 0.4


def png_b64_to_jpeg(b64):
    img = Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=70)
    return buf.getvalue()


async def mic_loop(session):
    loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue()

    def callback(indata, frames, t, status):
        loop.call_soon_threadsafe(q.put_nowait, bytes(indata))

    with sd.RawInputStream(samplerate=IN_RATE, channels=1, dtype="int16", blocksize=CHUNK, callback=callback):
        while True:
            data = await q.get()
            if hud.muted or (HALF_DUPLEX and jarvis_talking()):
                continue
            hud.audio("mic", data)
            await session.send_realtime_input(audio=types.Blob(data=data, mime_type=f"audio/pcm;rate={IN_RATE}"))


async def play_loop():
    global last_play
    stream = sd.RawOutputStream(samplerate=OUT_RATE, channels=1, dtype="int16")
    stream.start()
    try:
        while True:
            data = voice_fx.process(await play_q.get())
            await asyncio.to_thread(stream.write, data)
            hud.audio("out", data)
            last_play = time.time()
    finally:
        stream.stop()
        stream.close()


async def handle_tools(session, tool_call):
    responses = []
    for fc in tool_call.function_calls:
        args = dict(fc.args or {})
        print(f"\n  -> {fc.name}({json.dumps(args, ensure_ascii=False)[:150]})")
        hud.send({"t": "tool", "name": fc.name, "args": args})
        out = await asyncio.to_thread(run_tool, fc.name, args)
        hud.send({"t": "tool_done", "name": fc.name})
        if fc.name == "make_call" and str(out).startswith("Phone Link is dialing"):
            hud.muted = True                    # so JARVIS doesn't hear or answer the call
            hud.send({"t": "muted", "on": True})
            print("\n[mic muted for your call - press MIC on the HUD to unmute after you hang up]")
        if isinstance(out, dict) and "image" in out:
            await session.send_realtime_input(video=types.Blob(data=png_b64_to_jpeg(out["image"]), mime_type="image/jpeg"))
            out = "Screenshot sent to you as an image frame (1280px wide; click() uses those coordinates)."
        responses.append(types.FunctionResponse(id=fc.id, name=fc.name, response={"result": str(out)[:4000]}))
    await session.send_tool_response(function_responses=responses)


async def recv_loop(session):
    while True:
        async for resp in session.receive():
            if resp.data and not USE_EDGE:
                play_q.put_nowait(resp.data)
            sc = resp.server_content
            if sc:
                if sc.interrupted:              # you talked over him -> stop speaking right away
                    if edge:
                        edge.interrupt()
                    hud.send({"t": "interrupted"})
                    while not play_q.empty():
                        play_q.get_nowait()
                if sc.input_transcription and sc.input_transcription.text:
                    say_line("You", sc.input_transcription.text)
                if sc.output_transcription and sc.output_transcription.text:
                    say_line("JARVIS", sc.output_transcription.text)
                    if edge:
                        edge.feed(sc.output_transcription.text)
                if edge and sc.turn_complete:
                    edge.flush()
            if resp.tool_call:
                asyncio.create_task(handle_tools(session, resp.tool_call))


async def run_session(model, greet):
    async with client.aio.live.connect(model=model, config=make_config()) as session:
        print(f"[connected: {model}]  Just talk. Ctrl+C to quit.")
        hud.send({"t": "status", "online": True, "model": model})
        if greet:
            await session.send_realtime_input(text="Greet me in one short line as JARVIS coming online.")
        async with asyncio.TaskGroup() as tg:
            tg.create_task(mic_loop(session))
            tg.create_task(recv_loop(session))
            tg.create_task(play_loop())
            if edge:
                tg.create_task(edge.run())
            if hud.commands is not None:
                tg.create_task(ui_loop(session))


async def ui_loop(session):
    """Text typed into the HUD goes straight to JARVIS."""
    while True:
        text = await hud.commands.get()
        say_line("You", text)
        await session.send_realtime_input(text=text)


edge = None


async def main():
    global edge
    if USE_EDGE:
        from edge_voice import EdgeVoice
        edge = EdgeVoice(play_q)
    if hud.already_running():
        print("JARVIS is already running - bringing the HUD back up.")
        hud.open_window()
        return
    if USE_HUD:
        await hud.start()
    i, greet = 0, True
    while i < len(MODELS):
        start = time.time()
        try:
            await run_session(MODELS[i], greet)
        except (KeyboardInterrupt, asyncio.CancelledError):
            raise
        except BaseException as e:
            if time.time() - start < 5:          # failed instantly -> probably wrong model name, try next
                print(f"\n[{MODELS[i]} failed: {str(e)[:150]}]")
                i += 1
                continue
            hud.send({"t": "status", "online": False})
            print("\n[session ended, reconnecting...]")  # long session timed out -> reconnect quietly
            greet = False
            await asyncio.sleep(1)
    print("\nNo Live model worked. Check your GEMINI_API_KEY, or set LIVE_MODEL in .env to a model listed in AI Studio.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nJARVIS offline. Goodbye, sir.")
