"""
╔══════════════════════════════════════════════════════════════════╗
║   RMA Client Agent  —  runs on the managed machine              ║
║   Platforms : Windows (full) · Linux (full) · Android (stub)    ║
╚══════════════════════════════════════════════════════════════════╝

DEPENDENCIES (Windows/Linux):
  pip install websocket-client pyautogui pillow psutil

DEPENDENCIES (Android – Kivy):
  buildozer or python-for-android with kivy, requests, websocket-client

USAGE:
  python rma_client.py

The values below are injected by the bot when you use "Generate Client".
Replace them manually if running standalone.
"""

import asyncio
import base64
import hashlib
import hmac
import io
import json
import logging
import os
import platform
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from typing import Any, Dict, Optional

# ─── CONFIG (injected by bot at generation time) ──────────────────────────────
WS_URL        = "ws://__WS_URL__/ws"          # e.g. "ws://192.168.1.10:8765/ws"
SHARED_SECRET = "__SHARED_SECRET__"
DEVICE_TOKEN  = "__DEVICE_TOKEN__"
DEVICE_ID     = str(uuid.uuid5(uuid.NAMESPACE_DNS, socket.gethostname() + DEVICE_TOKEN))
RECONNECT_DELAY_MIN = 5     # seconds
RECONNECT_DELAY_MAX = 60

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("rma_client")

# ─── Platform Detection ───────────────────────────────────────────────────────

PLATFORM = platform.system().lower()   # "windows" | "linux" | "darwin"
IS_ANDROID = "ANDROID_ARGUMENT" in os.environ or "ANDROID_ROOT" in os.environ

if IS_ANDROID:
    PLATFORM = "android"

# ─── Lazy Imports (only load what the platform supports) ─────────────────────

def _try_import(module: str):
    try:
        return __import__(module)
    except ImportError:
        return None


_pyautogui  = None
_psutil     = _try_import("psutil")
_PIL        = _try_import("PIL")

if PLATFORM in ("windows", "linux", "darwin"):
    _pyautogui = _try_import("pyautogui")


# ─── HMAC Signing ─────────────────────────────────────────────────────────────

def _sign(payload: dict) -> str:
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    return hmac.new(SHARED_SECRET.encode(), body.encode(), hashlib.sha256).hexdigest()


def _verify(payload: dict, sig: str) -> bool:
    expected = _sign(payload)
    return hmac.compare_digest(expected, sig)


# ─── Command Handlers ─────────────────────────────────────────────────────────

class CommandRouter:
    """Maps command strings to handler functions."""

    def __init__(self):
        self._handlers = {}
        self._register_all()

    def _register_all(self):
        if PLATFORM != "android":
            self.register("screenshot",   self._screenshot)
            self.register("processes",    self._processes)
            self.register("kill_process", self._kill_process)
            self.register("shell",        self._shell)
            self.register("exec_file",    self._exec_file)
            self.register("sysinfo",      self._sysinfo)
            self.register("listdir",      self._listdir)
            self.register("hotkey",       self._hotkey)
            self.register("lock_screen",  self._lock_screen)
            self.register("volume",       self._volume)
            self.register("shutdown",     self._shutdown)
            self.register("reboot",       self._reboot)
        else:
            # Android stubs
            self.register("screenshot",  self._android_screenshot)
            self.register("sysinfo",     self._sysinfo)
            self.register("shell",       self._shell)

    def register(self, name: str, fn):
        self._handlers[name] = fn

    async def dispatch(self, cmd: str, params: dict) -> dict:
        handler = self._handlers.get(cmd)
        if handler is None:
            return {"status": "error", "error": f"Unknown command: {cmd}"}
        try:
            result = await asyncio.get_event_loop().run_in_executor(
                None, lambda: handler(params)
            )
            return {"status": "ok", "data": result}
        except Exception as e:
            log.error("Handler '%s' raised: %s", cmd, e, exc_info=True)
            return {"status": "error", "error": str(e)}

    # ── Screenshot ────────────────────────────────────────────────────────────

    def _screenshot(self, params: dict) -> str:
        """Capture full screen → return base64 PNG."""
        if _pyautogui:
            img = _pyautogui.screenshot()
        elif _PIL:
            from PIL import ImageGrab
            img = ImageGrab.grab()
        else:
            raise RuntimeError("No screenshot library available (install pyautogui or pillow)")

        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        return base64.b64encode(buf.getvalue()).decode()

    def _android_screenshot(self, params: dict) -> str:
        """Android screenshot via screencap (requires root or ADB)."""
        try:
            result = subprocess.run(
                ["screencap", "-p"],
                capture_output=True, timeout=10
            )
            return base64.b64encode(result.stdout).decode()
        except Exception:
            return ""   # graceful degradation

    # ── Processes ─────────────────────────────────────────────────────────────

    def _processes(self, params: dict) -> list:
        """Return list of running processes sorted by CPU usage."""
        if not _psutil:
            return self._fallback_processes()
        procs = []
        for proc in _psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent", "status"]):
            try:
                info = proc.info
                procs.append({
                    "pid":    info["pid"],
                    "name":   info["name"] or "?",
                    "cpu":    round(info.get("cpu_percent") or 0.0, 2),
                    "mem":    round(info.get("memory_percent") or 0.0, 2),
                    "status": info.get("status", ""),
                })
            except (_psutil.NoSuchProcess, _psutil.AccessDenied):
                continue
        return sorted(procs, key=lambda p: p["cpu"], reverse=True)[:80]

    def _fallback_processes(self) -> list:
        """Fallback for systems without psutil."""
        if PLATFORM == "windows":
            out = subprocess.run(
                ["tasklist", "/fo", "csv", "/nh"],
                capture_output=True, text=True, timeout=10
            ).stdout
            procs = []
            for line in out.splitlines():
                parts = line.strip('"').split('","')
                if len(parts) >= 2:
                    procs.append({"pid": parts[1], "name": parts[0], "cpu": 0.0})
            return procs
        else:
            out = subprocess.run(
                ["ps", "aux", "--sort=-%cpu"],
                capture_output=True, text=True, timeout=10
            ).stdout
            procs = []
            for line in out.splitlines()[1:]:
                parts = line.split(None, 10)
                if len(parts) >= 11:
                    procs.append({"pid": parts[1], "name": parts[10][:40], "cpu": float(parts[2] or 0)})
            return procs[:60]

    # ── Kill Process ──────────────────────────────────────────────────────────

    def _kill_process(self, params: dict):
        pid = int(params["pid"])
        if _psutil:
            proc = _psutil.Process(pid)
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except _psutil.TimeoutExpired:
                proc.kill()
        elif PLATFORM == "windows":
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], timeout=10)
        else:
            os.kill(pid, 9)
        return {"killed": pid}

    # ── Shell ─────────────────────────────────────────────────────────────────

    def _shell(self, params: dict) -> dict:
        """Execute a shell command and return stdout/stderr."""
        command = params.get("command", "")
        shell   = True
        if PLATFORM == "windows":
            args = ["cmd", "/c", command]
        else:
            args = ["/bin/sh", "-c", command]

        result = subprocess.run(
            args,
            capture_output=True, text=True,
            timeout=120, shell=False,
        )
        return {
            "stdout":     result.stdout[:8192],
            "stderr":     result.stderr[:2048],
            "returncode": result.returncode,
        }

    # ── Execute File ──────────────────────────────────────────────────────────

    def _exec_file(self, params: dict) -> dict:
        """Receive a file (base64), write to temp dir, execute it."""
        filename  = params.get("filename", "payload")
        data_b64  = params.get("data_b64", "")
        file_data = base64.b64decode(data_b64)

        # Write to a temp directory
        tmpdir  = tempfile.mkdtemp(prefix="rma_")
        fpath   = os.path.join(tmpdir, filename)
        with open(fpath, "wb") as f:
            f.write(file_data)

        # Make executable on Linux/macOS
        if PLATFORM != "windows":
            os.chmod(fpath, 0o755)

        ext = os.path.splitext(filename)[1].lower()

        # Choose launcher based on extension
        if PLATFORM == "windows":
            if ext in (".exe",):
                cmd = [fpath]
            elif ext in (".bat", ".cmd"):
                cmd = ["cmd", "/c", fpath]
            elif ext in (".ps1",):
                cmd = ["powershell", "-ExecutionPolicy", "Bypass", "-File", fpath]
            elif ext in (".py",):
                cmd = [sys.executable, fpath]
            else:
                cmd = [fpath]
        else:
            if ext == ".py":
                cmd = [sys.executable, fpath]
            elif ext == ".sh":
                cmd = ["/bin/bash", fpath]
            else:
                cmd = [fpath]

        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120, cwd=tmpdir
        )
        return {
            "stdout":     result.stdout[:4096],
            "stderr":     result.stderr[:1024],
            "returncode": result.returncode,
        }

    # ── System Info ───────────────────────────────────────────────────────────

    def _sysinfo(self, params: dict) -> dict:
        info = {
            "os":       platform.platform(),
            "hostname": socket.gethostname(),
            "username": os.environ.get("USERNAME") or os.environ.get("USER", "unknown"),
            "local_ip": self._local_ip(),
        }
        if _psutil:
            mem  = _psutil.virtual_memory()
            disk = _psutil.disk_usage("/")
            cpu_freq = _psutil.cpu_freq()
            info.update({
                "cpu_model": platform.processor() or "N/A",
                "cpu_pct":   _psutil.cpu_percent(interval=1),
                "ram_total": round(mem.total / (1024**3), 2),
                "ram_pct":   mem.percent,
                "disk_free": round(disk.free / (1024**3), 2),
                "uptime":    self._uptime(),
            })
        return info

    def _local_ip(self) -> str:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        except Exception:
            return "N/A"
        finally:
            try: s.close()
            except Exception: pass

    def _uptime(self) -> str:
        if not _psutil:
            return "N/A"
        seconds = int(time.time() - _psutil.boot_time())
        h, r   = divmod(seconds, 3600)
        m, s   = divmod(r, 60)
        return f"{h}h {m}m {s}s"

    # ── List Directory ────────────────────────────────────────────────────────

    def _listdir(self, params: dict) -> list:
        path  = params.get("path", os.path.expanduser("~"))
        path  = os.path.realpath(path)
        items = []
        try:
            entries = sorted(os.scandir(path), key=lambda e: (not e.is_dir(), e.name.lower()))
            for entry in entries[:80]:
                try:
                    stat   = entry.stat()
                    size_b = stat.st_size
                    if size_b >= 1024**3:
                        size_s = f"{size_b/1024**3:.1f}G"
                    elif size_b >= 1024**2:
                        size_s = f"{size_b/1024**2:.1f}M"
                    elif size_b >= 1024:
                        size_s = f"{size_b/1024:.1f}K"
                    else:
                        size_s = f"{size_b}B"
                    items.append({
                        "name":   entry.name,
                        "is_dir": entry.is_dir(),
                        "size":   size_s,
                    })
                except PermissionError:
                    items.append({"name": entry.name, "is_dir": False, "size": "?"})
        except PermissionError as e:
            items.append({"name": f"Access denied: {e}", "is_dir": False, "size": ""})
        return items

    # ── Hotkey ────────────────────────────────────────────────────────────────

    def _hotkey(self, params: dict):
        """Send a key combination (e.g. Alt+F4)."""
        keys = params.get("keys", [])
        if not keys:
            return {}
        if _pyautogui:
            _pyautogui.hotkey(*keys)
        elif PLATFORM == "windows":
            # Fallback: SendKeys via PowerShell
            ps_keys = "+".join(f"%{{F4}}" if k == "f4" else k for k in keys)
            subprocess.run(
                ["powershell", "-Command",
                 f'[System.Windows.Forms.SendKeys]::SendWait("{ps_keys}")'],
                timeout=5
            )
        return {"sent": keys}

    # ── Lock Screen ───────────────────────────────────────────────────────────

    def _lock_screen(self, params: dict):
        if PLATFORM == "windows":
            import ctypes
            ctypes.windll.user32.LockWorkStation()
        elif PLATFORM == "linux":
            subprocess.run(["xdg-screensaver", "lock"], timeout=5)
        elif PLATFORM == "darwin":
            subprocess.run([
                "osascript", "-e",
                'tell application "System Events" to keystroke "q" '
                'using {command down, control down}'
            ], timeout=5)
        return {}

    # ── Volume ────────────────────────────────────────────────────────────────

    def _volume(self, params: dict):
        direction = params.get("direction", "up")
        if _pyautogui:
            key = "volumeup" if direction == "up" else "volumedown"
            for _ in range(5):
                _pyautogui.press(key)
        elif PLATFORM == "windows":
            from ctypes import cast, POINTER
            try:
                from comtypes import CLSCTX_ALL
                from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
                devices = AudioUtilities.GetSpeakers()
                interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
                volume = cast(interface, POINTER(IAudioEndpointVolume))
                current = volume.GetMasterVolumeLevelScalar()
                delta = 0.10 if direction == "up" else -0.10
                volume.SetMasterVolumeLevelScalar(
                    max(0.0, min(1.0, current + delta)), None
                )
            except ImportError:
                # pycaw not available — use nircmd if present
                cmd = "nircmd.exe changesysvolume 6553" if direction == "up" else "nircmd.exe changesysvolume -6553"
                subprocess.run(cmd.split(), timeout=5)
        elif PLATFORM == "linux":
            delta = "+5%" if direction == "up" else "-5%"
            subprocess.run(["amixer", "-q", "sset", "Master", delta], timeout=5)
        return {}

    # ── Shutdown / Reboot ─────────────────────────────────────────────────────

    def _shutdown(self, params: dict):
        log.warning("SHUTDOWN command received!")
        if PLATFORM == "windows":
            subprocess.Popen(["shutdown", "/s", "/t", "5"])
        else:
            subprocess.Popen(["shutdown", "-h", "+0"])
        return {}

    def _reboot(self, params: dict):
        log.warning("REBOOT command received!")
        if PLATFORM == "windows":
            subprocess.Popen(["shutdown", "/r", "/t", "5"])
        else:
            subprocess.Popen(["shutdown", "-r", "+0"])
        return {}


# ─── WebSocket Client ─────────────────────────────────────────────────────────

class RMAClient:
    def __init__(self):
        self.router  = CommandRouter()
        self._ws     = None
        self._stop   = threading.Event()

    def run(self):
        """Entry point — run the event loop."""
        asyncio.run(self._run_loop())

    async def _run_loop(self):
        delay = RECONNECT_DELAY_MIN
        while not self._stop.is_set():
            try:
                await self._connect_and_serve()
                delay = RECONNECT_DELAY_MIN   # reset on clean exit
            except Exception as e:
                log.error("Connection error: %s", e)
            log.info("Reconnecting in %ds…", delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, RECONNECT_DELAY_MAX)

    async def _connect_and_serve(self):
        """Open WebSocket, authenticate, then serve commands."""
        try:
            import websockets
        except ImportError:
            # Try websocket-client as synchronous fallback
            await self._connect_ws_client()
            return

        log.info("Connecting to %s …", WS_URL)
        async with websockets.connect(WS_URL, ping_interval=20) as ws:
            self._ws = ws
            log.info("Connected.")
            await self._do_hello(ws)
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                asyncio.create_task(self._handle_message(ws, msg))

    async def _connect_ws_client(self):
        """Synchronous websocket-client backend wrapped in executor."""
        import websocket as _wsc

        loop = asyncio.get_event_loop()

        def _run_sync():
            ws = _wsc.WebSocket()
            ws.connect(WS_URL)
            log.info("Connected (sync ws).")
            # Sync hello
            hello = self._build_hello()
            ws.send(json.dumps(hello))
            resp_raw = ws.recv()
            resp = json.loads(resp_raw)
            log.info("Auth response: %s", resp.get("msg", ""))

            while not self._stop.is_set():
                try:
                    ws.settimeout(30)
                    raw = ws.recv()
                    msg = json.loads(raw)
                    result = asyncio.run_coroutine_threadsafe(
                        self.router.dispatch(msg.get("cmd",""), msg.get("params",{})),
                        loop
                    ).result(timeout=120)
                    response = {
                        "req_id": msg.get("req_id",""),
                        "status": result.get("status","ok"),
                        "data":   result.get("data"),
                    }
                    response["sig"] = _sign(response)
                    ws.send(json.dumps(response))
                except Exception as e:
                    log.error("Sync ws error: %s", e)
                    break
            ws.close()

        await loop.run_in_executor(None, _run_sync)

    def _build_hello(self) -> dict:
        hello = {
            "req_id": str(uuid.uuid4()),
            "cmd":    "hello",
            "params": {
                "device_id": DEVICE_ID,
                "hostname":  socket.gethostname(),
                "platform":  PLATFORM,
                "username":  os.environ.get("USERNAME") or os.environ.get("USER", "unknown"),
                "token":     DEVICE_TOKEN,
            },
        }
        hello["sig"] = _sign(hello)
        return hello

    async def _do_hello(self, ws):
        """Send authentication handshake."""
        hello = self._build_hello()
        await ws.send(json.dumps(hello))
        raw  = await asyncio.wait_for(ws.recv(), timeout=15)
        resp = json.loads(raw)
        if resp.get("status") != "ok":
            raise ConnectionError(f"Auth rejected: {resp.get('msg','')}")
        log.info("Authenticated as device %s", DEVICE_ID[:8])

    async def _handle_message(self, ws, msg: dict):
        """Process one inbound command message."""
        req_id = msg.get("req_id", "")
        cmd    = msg.get("cmd", "")
        params = msg.get("params", {})

        # Verify server signature
        recv_sig = msg.pop("sig", "")
        if not _verify(msg, recv_sig):
            log.warning("Invalid signature on message (cmd=%s)", cmd)
            return

        log.info("CMD: %s  params=%s", cmd, list(params.keys()))
        result = await self.router.dispatch(cmd, params)

        response = {
            "req_id": req_id,
            "status": result.get("status", "ok"),
            "data":   result.get("data"),
            "error":  result.get("error"),
        }
        response["sig"] = _sign(response)

        try:
            import websockets
            await ws.send(json.dumps(response))
        except Exception as e:
            log.error("Failed to send response: %s", e)

    def stop(self):
        self._stop.set()


# ─── Android (Kivy) Stub ──────────────────────────────────────────────────────

def _run_android():
    """
    Android entry point using Kivy for a minimal background service.
    For full Android support, compile with buildozer / python-for-android.

    Requirements in buildozer.spec:
      requirements = kivy,websocket-client,requests,pillow

    The Kivy App runs a background thread with the WebSocket client.
    """
    try:
        from kivy.app import App
        from kivy.uix.label import Label
        from kivy.clock import Clock

        class RMAAndroidApp(App):
            def build(self):
                self._client = RMAClient()
                # Start client in background thread
                t = threading.Thread(target=self._client.run, daemon=True)
                t.start()
                lbl = Label(
                    text="RMA Agent running\nMinimise to keep active",
                    font_size="18sp",
                )
                return lbl

            def on_stop(self):
                self._client.stop()

        RMAAndroidApp().run()

    except ImportError:
        log.error("Kivy not available — cannot run Android UI")
        # Headless fallback (e.g. in Termux)
        client = RMAClient()
        client.run()


# ─── Startup: Background / Hidden ────────────────────────────────────────────

def _setup_autostart():
    """
    Optional: register the client to start on login.
    Call this once during first deployment.
    """
    if PLATFORM == "windows":
        import winreg
        exe_path = sys.executable if not getattr(sys, "frozen", False) else sys.executable
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0, winreg.KEY_SET_VALUE
        )
        winreg.SetValueEx(key, "RMAClient", 0, winreg.REG_SZ,
                          f'"{exe_path}" "{os.path.abspath(__file__)}"')
        winreg.CloseKey(key)
        log.info("Windows autostart registered.")

    elif PLATFORM == "linux":
        autostart_dir = os.path.expanduser("~/.config/autostart")
        os.makedirs(autostart_dir, exist_ok=True)
        desktop_file = os.path.join(autostart_dir, "rma_client.desktop")
        with open(desktop_file, "w") as f:
            f.write(
                "[Desktop Entry]\n"
                "Type=Application\n"
                "Name=RMA Client\n"
                f"Exec={sys.executable} {os.path.abspath(__file__)}\n"
                "Hidden=false\n"
                "NoDisplay=false\n"
                "X-GNOME-Autostart-enabled=true\n"
            )
        log.info("Linux autostart .desktop created.")


# ─── Entry Point ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Autostart setup: uncomment if you want to register on first run
    # _setup_autostart()

    if IS_ANDROID:
        _run_android()
    else:
        client = RMAClient()
        try:
            client.run()
        except KeyboardInterrupt:
            client.stop()
            log.info("Client stopped.")
