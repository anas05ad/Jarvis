"""
JARVIS - personal voice assistant (English + Arabic)
Pipeline: wake word "Jarvis" -> record -> Whisper speech-to-text -> Claude (with tools) -> Edge TTS voice

Run:
    python jarvis.py          # voice mode
    python jarvis.py --text   # type instead of talk (good for testing)
"""
import os, re, io, sys, json, time, base64, asyncio, platform, subprocess, tempfile, webbrowser, smtplib
from email.message import EmailMessage
from xml.sax.saxutils import escape
from datetime import datetime

import numpy as np
import pyautogui
import edge_tts
import pygame
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

# ---------------- CONFIG ----------------
# Pick the brain: gemini (free, rate-limited) | ollama (free, unlimited, local) | grok | claude
PROVIDER = os.getenv("PROVIDER", "gemini").lower()
PROVIDERS = {
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai/", "GEMINI_API_KEY", "gemini-flash-latest"),
    "ollama": ("http://localhost:11434/v1", None, "qwen3:8b"),
    "grok":   ("https://api.x.ai/v1", "XAI_API_KEY", "grok-4-1-fast-non-reasoning"),
    "claude": ("https://api.anthropic.com/v1/", "ANTHROPIC_API_KEY", "claude-sonnet-5-5"),
}
BASE_URL, KEY_ENV, DEFAULT_MODEL = PROVIDERS[PROVIDER]
MODEL = os.getenv("MODEL", DEFAULT_MODEL)
# Small local models usually can't see images, so screen-reading is off for Ollama by default
ENABLE_SCREEN = os.getenv("ENABLE_SCREEN", "0" if PROVIDER == "ollama" else "1") == "1"
WHISPER_SIZE = os.getenv("WHISPER_MODEL", "small")      # "medium" = better Arabic, slower
SILENCE_THRESHOLD = int(os.getenv("SILENCE_THRESHOLD", "500"))
VOICE_EN = "en-GB-RyanNeural"                            # British butler
VOICE_AR = "ar-JO-TaimNeural"                            # Jordanian Arabic
OS_NAME = platform.system()                              # Windows / Darwin / Linux
SHOT_WIDTH = 1280
TEXT_MODE = "--text" in sys.argv

CONTACTS = {}
if os.path.exists("contacts.json"):
    with open("contacts.json", encoding="utf-8") as f:
        CONTACTS = json.load(f)

llm = OpenAI(base_url=BASE_URL, api_key=os.environ[KEY_ENV] if KEY_ENV else "ollama")
pygame.mixer.init()
pyautogui.FAILSAFE = True  # slam the mouse into a screen corner to abort any clicking/typing


def system_prompt():
    return f"""You are JARVIS, a calm, witty, British-butler style AI assistant running on the user's {OS_NAME} computer.
Current time: {datetime.now():%A %d %B %Y, %H:%M}.
- Reply in the SAME language the user spoke (English or Arabic).
- Your replies are spoken aloud: 1-3 short sentences, no markdown, no lists.
- Actually DO things with your tools instead of explaining how.
- Be fast: use the most direct tool (open_app, play_youtube, media, press_keys shortcuts) and chain actions without asking. Screenshot only when you must see the screen.
- If a tool says NOT DONE YET, ask the user out loud, then retry with confirmed=true when they say yes.
- If a request is unclear or risky, ask one short question first.
Known contacts (name -> email/phone): {json.dumps(CONTACTS, ensure_ascii=False)}"""


# ---------------- TOOLS ----------------
import urllib.parse, urllib.request
import pyperclip

pyautogui.PAUSE = 0.02          # default is 0.1s between every action, way too slow
S = {"type": "string"}


def _schema(props, required=None):
    return {"type": "object", "properties": props, "required": list(props) if required is None else required}


TOOLS = [
    {"name": "open_app", "description": "Open any installed app instantly by name, e.g. 'spotify', 'discord', 'calculator', 'settings', 'word'.",
     "input_schema": _schema({"name": S})},
    {"name": "close_app", "description": "Close an app by name, e.g. 'chrome', 'spotify'.",
     "input_schema": _schema({"name": S})},
    {"name": "focus_window", "description": "Bring an open window to the front using part of its title.",
     "input_schema": _schema({"title": S})},
    {"name": "open_url", "description": "Open a website.", "input_schema": _schema({"url": S})},
    {"name": "web_search", "description": "Search Google in the browser.", "input_schema": _schema({"query": S})},
    {"name": "play_youtube", "description": "Play the top YouTube result for a search right away.",
     "input_schema": _schema({"query": S})},
    {"name": "media", "description": "Media and volume control for whatever is playing.",
     "input_schema": _schema({"action": {"type": "string", "enum": ["play_pause", "next", "previous", "volume_up", "volume_down", "mute"]},
                              "steps": {"type": "integer", "description": "How many presses. Each volume step is 2%."}}, ["action"])},
    {"name": "type_text", "description": "Paste text into the focused field (works with Arabic). enter=true presses Enter after.",
     "input_schema": _schema({"text": S, "enter": {"type": "boolean"}}, ["text"])},
    {"name": "press_keys", "description": "Press a key or shortcut, e.g. ['ctrl','t'], ['win','d'], ['alt','f4'], ['ctrl','shift','esc'].",
     "input_schema": _schema({"keys": {"type": "array", "items": S}})},
    {"name": "run_command", "description": "Run a PowerShell command (Windows) and get the output. For files, folders, settings, system info. "
                                           "Risky commands (deleting, shutdown, registry, killing processes) only run with confirmed=true, after the user said yes out loud.",
     "input_schema": _schema({"command": S, "confirmed": {"type": "boolean"}}, ["command"])},
    {"name": "screenshot", "description": "Look at the screen. Slow, so only use it when you truly need to see something. Image coordinates are what click() expects.",
     "input_schema": _schema({})},
    {"name": "click", "description": "Click at x,y from the latest screenshot.",
     "input_schema": _schema({"x": {"type": "integer"}, "y": {"type": "integer"}, "double": {"type": "boolean"},
                              "button": {"type": "string", "enum": ["left", "right"]}}, ["x", "y"])},
    {"name": "make_call", "description": "Call a phone number through the user's own phone (Windows Phone Link). "
                                         "Number in international format like +18185551234. The user does the talking.",
     "input_schema": _schema({"to": S, "name": {"type": "string", "description": "Who is being called."}}, ["to"])},
    {"name": "save_contact", "description": "Save or update a contact so the user never has to give the number again. "
                                            "Use it whenever the user mentions a new person's phone number or email.",
     "input_schema": _schema({"name": S, "phone": S, "email": S}, ["name"])},
    {"name": "send_whatsapp", "description": "Send a WhatsApp message through WhatsApp Desktop. Read the recipient and message back to the user first; call with confirmed=true only after they say yes.",
     "input_schema": _schema({"to": S, "text": S, "confirmed": {"type": "boolean"}}, ["to", "text"])},
    {"name": "send_email", "description": "Send an email from Gmail. Read the recipient, subject and body back to the user first; call with confirmed=true only after they say yes.",
     "input_schema": _schema({"to": S, "subject": S, "body": S, "confirmed": {"type": "boolean"}}, ["to", "subject", "body"])},
]

if not ENABLE_SCREEN:
    TOOLS = [t for t in TOOLS if t["name"] not in ("screenshot", "click")]
OA_TOOLS = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                              "parameters": t["input_schema"]}} for t in TOOLS]

# Only these need a spoken "yes". Everything else runs instantly.
RISKY = re.compile(r"\b(remove-item|rm|rmdir|rd|del|erase|format|diskpart|shutdown|restart-computer|stop-computer|"
                   r"reg\s+(delete|add)|set-itemproperty|remove-itemproperty|bcdedit|cipher|takeown|icacls|net\s+user|"
                   r"stop-process|taskkill|uninstall|clear-recyclebin|move-item|mv|set-executionpolicy)\b", re.I)
ASK_FIRST = "NOT DONE YET. Tell the user in one sentence exactly what this will do, ask them to confirm out loud, and if they say yes call this tool again with confirmed=true."


def screen_scale():
    return pyautogui.size().width / SHOT_WIDTH


def _paste(text):
    pyperclip.copy(text)
    pyautogui.hotkey("command" if OS_NAME == "Darwin" else "ctrl", "v")


def _ps(command, timeout=60):
    if OS_NAME == "Windows":
        r = subprocess.run(["powershell", "-NoProfile", "-Command", command], capture_output=True, text=True,
                           timeout=timeout, encoding="utf-8", errors="replace",
                           creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        r = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=timeout)
    return ((r.stdout or "") + (r.stderr or "")).strip() or "(done)"


def _clean(s):
    return re.sub(r"[\"'`;&|$]", "", s).strip()


def run_tool(name, args):
    try:
        if name == "open_app":
            app = args["name"]
            if OS_NAME == "Windows":
                pyautogui.press("win")
                time.sleep(0.4)
                pyautogui.write(app, interval=0)
                time.sleep(0.7)
                pyautogui.press("enter")
            elif OS_NAME == "Darwin":
                subprocess.Popen(["open", "-a", app])
            else:
                subprocess.Popen([app])
            return f"Opened {app}"

        if name == "close_app":
            n = _clean(args["name"])
            if OS_NAME == "Windows":
                out = _ps("Get-Process | Where-Object { $_.MainWindowTitle -and ($_.ProcessName -like '*%s*' -or "
                          "$_.MainWindowTitle -like '*%s*') } | ForEach-Object { $null = $_.CloseMainWindow(); $_.ProcessName }" % (n, n))
                return f"Closed: {out}" if out != "(done)" else f"No open window matched '{n}'"
            return _ps(f"pkill -i {n}")

        if name == "focus_window":
            import pygetwindow as gw
            wins = [w for w in gw.getAllWindows() if w.title and args["title"].lower() in w.title.lower()]
            if not wins:
                return "No window with that title"
            w = wins[0]
            if w.isMinimized:
                w.restore()
            pyautogui.press("alt")      # Windows only lets apps steal focus right after a keypress
            w.activate()
            return f"Focused {w.title}"

        if name == "open_url":
            webbrowser.open(args["url"])
            return "Opened"

        if name == "web_search":
            webbrowser.open("https://www.google.com/search?q=" + urllib.parse.quote_plus(args["query"]))
            return "Searched"

        if name == "play_youtube":
            q = urllib.parse.quote_plus(args["query"])
            req = urllib.request.Request(f"https://www.youtube.com/results?search_query={q}",
                                         headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "en"})
            html = urllib.request.urlopen(req, timeout=8).read().decode("utf-8", "ignore")
            m = re.search(r'"videoId":"([\w-]{11})"', html)
            webbrowser.open(f"https://www.youtube.com/watch?v={m.group(1)}" if m else f"https://www.youtube.com/results?search_query={q}")
            return "Playing" if m else "Opened search results"

        if name == "media":
            key = {"play_pause": "playpause", "next": "nexttrack", "previous": "prevtrack",
                   "volume_up": "volumeup", "volume_down": "volumedown", "mute": "volumemute"}[args["action"]]
            pyautogui.press(key, presses=max(1, int(args.get("steps", 1))))
            return "Done"

        if name == "type_text":
            _paste(args["text"])
            if args.get("enter"):
                pyautogui.press("enter")
            return "Typed"

        if name == "press_keys":
            pyautogui.hotkey(*args["keys"])
            return "Pressed"

        if name == "run_command":
            if RISKY.search(args["command"]) and not args.get("confirmed"):
                return ASK_FIRST
            return _ps(args["command"])[:4000]

        if name == "screenshot":
            img = pyautogui.screenshot()
            img = img.resize((SHOT_WIDTH, int(SHOT_WIDTH * img.height / img.width)))
            buf = io.BytesIO()
            img.save(buf, "PNG")
            return {"image": base64.b64encode(buf.getvalue()).decode()}

        if name == "click":
            s = screen_scale()
            x, y = int(args["x"] * s), int(args["y"] * s)
            pyautogui.click(x, y, clicks=2 if args.get("double") else 1, button=args.get("button", "left"))
            return f"Clicked {x},{y}"

        if name == "make_call":
            if OS_NAME != "Windows":
                return "Phone Link calling only works on Windows."
            num = re.sub(r"[^\d+]", "", args["to"])
            os.startfile(f"tel:{num}")          # Phone Link dials through the linked phone
            return ("Phone Link is dialing. The mic is muted so JARVIS doesn't talk over the call. "
                    "If Phone Link shows a Call button instead of ringing, take a screenshot and click it.")

        if name == "save_contact":
            c = CONTACTS.setdefault(args["name"], {})
            for k in ("phone", "email"):
                if args.get(k):
                    c[k] = args[k]
            with open("contacts.json", "w", encoding="utf-8") as f:
                json.dump(CONTACTS, f, ensure_ascii=False, indent=2)
            return f"Saved {args['name']}"

        if name == "send_whatsapp":
            if not args.get("confirmed"):
                return ASK_FIRST
            num = re.sub(r"\D", "", args["to"])
            webbrowser.open(f"whatsapp://send?phone={num}&text={urllib.parse.quote(args['text'])}")
            time.sleep(4)               # wait for WhatsApp Desktop to open the chat
            pyautogui.press("enter")
            return "WhatsApp message sent"

        if name == "send_email":
            if not args.get("confirmed"):
                return ASK_FIRST
            msg = EmailMessage()
            msg["From"], msg["To"], msg["Subject"] = os.environ["GMAIL_ADDRESS"], args["to"], args["subject"]
            msg.set_content(args["body"])
            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
                s.login(os.environ["GMAIL_ADDRESS"], os.environ["GMAIL_APP_PASSWORD"])
                s.send_message(msg)
            return "Email sent"

        return f"Unknown tool {name}"
    except Exception as e:
        return f"Error: {e}"


# ---------------- BRAIN ----------------
history = []

def think(user_text):
    global history
    if len(history) > 40:
        history = []
    history.append({"role": "user", "content": user_text})
    for _ in range(15):
        resp = llm.chat.completions.create(model=MODEL, tools=OA_TOOLS,
                                           messages=[{"role": "system", "content": system_prompt()}] + history)
        m = resp.choices[0].message
        entry = {"role": "assistant", "content": m.content or ""}
        if m.tool_calls:
            entry["tool_calls"] = [{"id": tc.id, "type": "function",
                                    "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                                   for tc in m.tool_calls]
        history.append(entry)
        if not m.tool_calls:
            return re.sub(r"<think>.*?</think>", "", m.content or "", flags=re.S).strip()

        images = []
        for tc in m.tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            print(f"  -> {tc.function.name}({json.dumps(args, ensure_ascii=False)[:150]})")
            out = run_tool(tc.function.name, args)
            if isinstance(out, dict) and "image" in out:
                images.append(out["image"])
                out = "Screenshot taken, it is attached in the next message."
            history.append({"role": "tool", "tool_call_id": tc.id, "content": str(out)})
        for img in images:
            history.append({"role": "user", "content": [
                {"type": "text", "text": "Current screen:"},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img}"}}]})
    history = []
    return "That took more steps than I'm allowed, sir. Let's try it in smaller pieces."


# ---------------- VOICE OUT ----------------
def speak(text):
    if not text:
        return
    text = re.sub(r"[*#`_]", "", text)
    print(f"JARVIS: {text}")
    if TEXT_MODE and os.getenv("SPEAK_IN_TEXT_MODE", "1") == "0":
        return
    voice = VOICE_AR if re.search(r"[\u0600-\u06FF]", text) else VOICE_EN
    path = os.path.join(tempfile.gettempdir(), f"jarvis_{time.time_ns()}.mp3")
    asyncio.run(edge_tts.Communicate(text, voice).save(path))
    pygame.mixer.music.load(path)
    pygame.mixer.music.play()
    while pygame.mixer.music.get_busy():
        time.sleep(0.05)
    pygame.mixer.music.unload()
    try:
        os.remove(path)
    except OSError:
        pass


# ---------------- VOICE IN ----------------
def record_command(recorder, max_sec=15, silence_sec=1.2):
    fps = 16000 / recorder.frame_length
    frames, silent, heard = [], 0, False
    for _ in range(int(max_sec * fps)):
        pcm = recorder.read()
        frames.extend(pcm)
        rms = np.sqrt(np.mean(np.square(np.array(pcm, dtype=np.float32))))
        if rms > SILENCE_THRESHOLD:
            heard, silent = True, 0
        else:
            silent += 1
        if heard and silent > silence_sec * fps:
            break
        if not heard and silent > 5 * fps:  # nobody spoke
            break
    return np.array(frames, dtype=np.float32) / 32768.0


def main_voice():
    import pvporcupine
    from pvrecorder import PvRecorder
    from faster_whisper import WhisperModel

    print("Loading speech model (first run downloads it)...")
    whisper = WhisperModel(WHISPER_SIZE, device="auto", compute_type="int8")
    porcupine = pvporcupine.create(access_key=os.environ["PICOVOICE_ACCESS_KEY"], keywords=["jarvis"])
    recorder = PvRecorder(frame_length=porcupine.frame_length)
    speak("JARVIS online. At your service, sir.")
    try:
        while True:
            recorder.start()
            print("\n(say 'Jarvis'...)")
            while porcupine.process(recorder.read()) < 0:
                pass
            print("Listening...")
            audio = record_command(recorder)
            recorder.stop()

            segments, info = whisper.transcribe(audio, beam_size=5, vad_filter=True)
            text = "".join(s.text for s in segments).strip()
            if not text:
                continue
            print(f"You ({info.language}): {text}")
            if re.search(r"(goodbye jarvis|shut ?down jarvis|مع السلامة)", text.lower()):
                speak("Goodbye, sir.")
                break
            try:
                speak(think(text))
            except Exception as e:
                print(e)
                speak("Something went wrong on my end, sir.")
    except KeyboardInterrupt:
        pass
    finally:
        recorder.delete()
        porcupine.delete()


def main_text():
    print("JARVIS text mode. Type 'exit' to quit.")
    while True:
        text = input("\nYou: ").strip()
        if text.lower() in ("exit", "quit"):
            break
        if text:
            speak(think(text))


if __name__ == "__main__":
    main_text() if TEXT_MODE else main_voice()
