"""
HUD bridge for JARVIS LIVE.
Serves the interface (hud_web/index.html) and streams voice levels, captions, actions and PC stats to it.
"""
import os, json, time, asyncio, threading, subprocess, platform, webbrowser, functools
import http.server
import socket

import numpy as np
from websockets.asyncio.server import serve, broadcast

try:
    import psutil
except ImportError:
    psutil = None

HTTP_PORT, WS_PORT = 8770, 8771
_HERE = os.path.dirname(os.path.abspath(__file__))
# Works whether index.html is inside a "hud_web" folder or right next to this file
WEB_DIR = next((d for d in (os.path.join(_HERE, "hud_web"), _HERE)
                if os.path.isfile(os.path.join(d, "index.html"))), os.path.join(_HERE, "hud_web"))
if not os.path.isfile(os.path.join(WEB_DIR, "index.html")):
    print(f"[HUD] index.html not found. Put it in {os.path.join(_HERE, 'hud_web')}")

clients = set()
commands = None          # asyncio.Queue of text typed into the HUD
muted = False
_last = {"mic": 0.0, "out": 0.0}
_config = {}
_keep = []               # keep references so the server/tasks aren't garbage-collected


def send(obj):
    if clients:
        broadcast(clients, json.dumps(obj, ensure_ascii=False))


def audio(kind, data, n=48):
    """kind = 'mic' (you) or 'out' (JARVIS). Sends loudness + 48 frequency bands, ~30x per second."""
    now = time.time()
    if not clients or now - _last[kind] < 0.033:
        return
    _last[kind] = now
    data = data[: len(data) // 2 * 2]
    a = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
    if a.size < 64:
        return
    level = float(np.sqrt(np.mean(a * a)))
    spec = np.abs(np.fft.rfft(a * np.hanning(a.size)))[1:]
    edges = np.logspace(0, np.log10(spec.size), n + 1)
    bands = []
    for i in range(n):
        lo = int(edges[i]) - 1
        hi = max(int(edges[i + 1]), lo + 1)
        bands.append(float(spec[lo:hi].mean()))
    bands = np.clip(np.log1p(np.array(bands)) / 4.0, 0, 1).round(3).tolist()
    send({"t": kind, "l": round(level, 4), "b": bands})


async def _ws(ws):
    global muted
    clients.add(ws)
    await ws.send(json.dumps({"t": "config", **_config}))
    try:
        async for msg in ws:
            try:
                m = json.loads(msg)
            except ValueError:
                continue
            if m.get("t") == "mute":
                muted = bool(m.get("on"))
                send({"t": "muted", "on": muted})
            elif m.get("t") == "text" and m.get("text") and commands is not None:
                commands.put_nowait(m["text"])
    finally:
        clients.discard(ws)


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def _http():
    handler = functools.partial(_Quiet, directory=WEB_DIR)
    http.server.ThreadingHTTPServer(("127.0.0.1", HTTP_PORT), handler).serve_forever()


async def _stats():
    if not psutil:
        return
    disk_path = (os.environ.get("SystemDrive", "C:") + "\\") if platform.system() == "Windows" else "/"
    psutil.cpu_percent(None)
    last, lt = psutil.net_io_counters(), time.time()
    while True:
        await asyncio.sleep(1)
        try:
            n, now = psutil.net_io_counters(), time.time()
            dt = max(now - lt, 0.001)
            vm, du, bat = psutil.virtual_memory(), psutil.disk_usage(disk_path), psutil.sensors_battery()
            send({"t": "stats",
                  "cpu": psutil.cpu_percent(None),
                  "ram": vm.percent, "ram_used": round(vm.used / 1e9, 1), "ram_total": round(vm.total / 1e9, 1),
                  "disk": du.percent, "disk_free": round(du.free / 1e9),
                  "bat": round(bat.percent) if bat else None, "plug": bat.power_plugged if bat else None,
                  "up": round((n.bytes_sent - last.bytes_sent) / dt / 1024, 1),
                  "down": round((n.bytes_recv - last.bytes_recv) / dt / 1024, 1),
                  "boot": psutil.boot_time()})
            last, lt = n, now
        except Exception:
            pass


def already_running():
    """True if another JARVIS is already using the HUD port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", WS_PORT)) == 0


def open_window():
    url = f"http://localhost:{HTTP_PORT}/index.html"
    if platform.system() == "Windows":
        # Edge "app mode" = clean window with no tabs or address bar
        subprocess.Popen(f'start msedge --app={url} --start-fullscreen', shell=True)
    else:
        webbrowser.open(url)
    return url


async def start():
    global commands, _config
    commands = asyncio.Queue()
    _config = {"unit": os.getenv("HUD_TEMP_UNIT", "celsius"),
               "lat": os.getenv("HUD_LAT"), "lon": os.getenv("HUD_LON")}
    threading.Thread(target=_http, daemon=True).start()
    _keep.append(await serve(_ws, "127.0.0.1", WS_PORT))
    _keep.append(asyncio.create_task(_stats()))

    print(f"[HUD] {open_window()}")
