"""
╔══════════════════════════════════════════════════════════════════╗
║   RMA Bot — Remote Machine Administration via Telegram           ║
║   Author  : Generated for personal fleet management             ║
║   Stack   : aiogram 3.x · asyncio · aiohttp WebSocket server   ║
╚══════════════════════════════════════════════════════════════════╝

IMPORTANT – LEGAL / ETHICAL NOTICE:
  This tool is designed for administering machines you OWN or have
  explicit written authorisation to manage. Deploying it on any
  device without the owner's consent is illegal in most jurisdictions.

SETUP:
  pip install aiogram aiohttp cryptography pyinstaller
  export BOT_TOKEN="your_telegram_bot_token"
  export ADMIN_ID="your_telegram_user_id"
  export WS_HOST="0.0.0.0"   # or your public IP / domain
  export WS_PORT="8765"
  export SHARED_SECRET="change_me_32_chars_minimum_secret"
  python bot.py
"""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
import uuid
from datetime import datetime
from typing import Any, Dict, Optional

import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    Document,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

# ─── Config ───────────────────────────────────────────────────────────────────

BOT_TOKEN     = os.environ.get("BOT_TOKEN", "вот тут токен")
ADMIN_ID      = int(os.environ.get("ADMIN_ID", "тут айди"))          # Your Telegram user ID
WS_HOST       = os.environ.get("WS_HOST", "0.0.0.0")
WS_PORT       = int(os.environ.get("WS_PORT", "8765"))
SHARED_SECRET = os.environ.get("SHARED_SECRET", "тут пароль")
MAX_DEVICES   = 50     # hard cap per admin session

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("rma_bot")

# ─── Device Registry ──────────────────────────────────────────────────────────

class DeviceInfo:
    """Represents one connected remote machine."""
    def __init__(self, device_id: str, hostname: str, platform: str,
                 username: str, ws: web.WebSocketResponse):
        self.device_id  = device_id
        self.hostname   = hostname
        self.platform   = platform      # "windows" | "android" | "linux"
        self.username   = username
        self.ws         = ws
        self.connected_at = datetime.utcnow()
        self.last_seen  = time.time()
        self.pending: Dict[str, asyncio.Future] = {}   # req_id → Future

    @property
    def label(self) -> str:
        plat_icon = {"windows": "🖥", "android": "📱", "linux": "🐧"}.get(self.platform, "💻")
        return f"{plat_icon} {self.hostname} ({self.username})"

    @property
    def is_alive(self) -> bool:
        return not self.ws.closed and (time.time() - self.last_seen < 60)


class DeviceRegistry:
    """Thread-safe in-memory device registry."""
    def __init__(self):
        self._devices: Dict[str, DeviceInfo] = {}
        self._lock = asyncio.Lock()

    async def register(self, device: DeviceInfo) -> None:
        async with self._lock:
            if len(self._devices) >= MAX_DEVICES:
                raise RuntimeError("Device limit reached")
            # Remove stale connection for the same device_id
            if device.device_id in self._devices:
                old = self._devices[device.device_id]
                if not old.ws.closed:
                    await old.ws.close()
            self._devices[device.device_id] = device

    async def unregister(self, device_id: str) -> None:
        async with self._lock:
            self._devices.pop(device_id, None)

    def get(self, device_id: str) -> Optional[DeviceInfo]:
        return self._devices.get(device_id)

    def all(self) -> list:
        return list(self._devices.values())

    def alive(self) -> list:
        return [d for d in self._devices.values() if d.is_alive]


REGISTRY = DeviceRegistry()

# ─── Messaging Helpers ────────────────────────────────────────────────────────

def _sign_message(payload: dict, secret: str) -> str:
    """HMAC-SHA256 signature for WebSocket messages."""
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    sig  = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    return sig


async def send_command(device: DeviceInfo, cmd: str,
                       params: Optional[dict] = None,
                       timeout: float = 30.0) -> dict:
    """
    Send a JSON command to the client and await its response.
    Returns the response payload or raises TimeoutError / ConnectionError.
    """
    if device.ws.closed:
        raise ConnectionError(f"Device {device.device_id} is disconnected")

    req_id  = str(uuid.uuid4())
    payload = {"req_id": req_id, "cmd": cmd, "params": params or {}}
    payload["sig"] = _sign_message(payload, SHARED_SECRET)

    loop   = asyncio.get_event_loop()
    future = loop.create_future()
    device.pending[req_id] = future

    try:
        await device.ws.send_json(payload)
        return await asyncio.wait_for(future, timeout=timeout)
    except asyncio.TimeoutError:
        device.pending.pop(req_id, None)
        raise TimeoutError(f"Command '{cmd}' timed out after {timeout}s")
    finally:
        device.pending.pop(req_id, None)


# ─── FSM States ───────────────────────────────────────────────────────────────

class AdminStates(StatesGroup):
    waiting_exec_file   = State()   # waiting for user to upload a file to run
    waiting_shell_input = State()   # waiting for custom shell command text


# ─── Keyboards ────────────────────────────────────────────────────────────────

def kb_main_menu() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="📋 Device List",  callback_data="list_devices")
    b.button(text="➕ Generate Client", callback_data="gen_client")
    b.button(text="📦 Build Installer", callback_data="build_installer")
    b.button(text="ℹ️ Bot Status",    callback_data="bot_status")
    b.adjust(2)
    return b.as_markup()


def kb_device_list(devices: list) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for dev in devices:
        status = "🟢" if dev.is_alive else "🔴"
        b.button(
            text=f"{status} {dev.label}",
            callback_data=f"dev:{dev.device_id}"
        )
    b.button(text="🔄 Refresh",  callback_data="list_devices")
    b.button(text="🏠 Main Menu", callback_data="main_menu")
    b.adjust(1)
    return b.as_markup()


def kb_device_control(device_id: str) -> InlineKeyboardMarkup:
    d = device_id
    b = InlineKeyboardBuilder()
    b.button(text="📸 Screenshot",      callback_data=f"cmd:screenshot:{d}")
    b.button(text="📊 Processes",        callback_data=f"cmd:processes:{d}")
    b.button(text="💻 Shell Command",    callback_data=f"cmd:shell:{d}")
    b.button(text="📁 Execute File",     callback_data=f"cmd:exec_file:{d}")
    b.button(text="🔌 Alt+F4",           callback_data=f"cmd:altf4:{d}")
    b.button(text="⚠️ Shutdown",         callback_data=f"cmd:shutdown:{d}")
    b.button(text="🔃 Reboot",           callback_data=f"cmd:reboot:{d}")
    b.button(text="🔒 Lock Screen",      callback_data=f"cmd:lock:{d}")
    b.button(text="📂 File Browser",     callback_data=f"cmd:filebrowser:{d}")
    b.button(text="🔊 Volume Up",        callback_data=f"cmd:vol_up:{d}")
    b.button(text="🔇 Volume Down",      callback_data=f"cmd:vol_down:{d}")
    b.button(text="🌐 System Info",      callback_data=f"cmd:sysinfo:{d}")
    b.button(text="◀ Back to Devices",   callback_data="list_devices")
    b.adjust(2)
    return b.as_markup()


def kb_process_list(processes: list, device_id: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for proc in processes[:25]:    # Telegram inline limit
        pid  = proc.get("pid", "?")
        name = proc.get("name", "unknown")[:22]
        cpu  = proc.get("cpu", 0.0)
        b.button(
            text=f"❌ [{pid}] {name} ({cpu:.1f}%)",
            callback_data=f"kill:{pid}:{device_id}"
        )
    b.button(text="🔄 Refresh",             callback_data=f"cmd:processes:{device_id}")
    b.button(text="◀ Back to Device",       callback_data=f"dev:{device_id}")
    b.adjust(1)
    return b.as_markup()


def kb_confirm(action: str, device_id: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ Confirm",  callback_data=f"confirm:{action}:{device_id}")
    b.button(text="❌ Cancel",   callback_data=f"dev:{device_id}")
    b.adjust(2)
    return b.as_markup()


# ─── Router ───────────────────────────────────────────────────────────────────

router = Router()


def _is_admin(user_id: int) -> bool:
    return ADMIN_ID != 0 and user_id == ADMIN_ID


# ─── /start & /menu ───────────────────────────────────────────────────────────

@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    if not _is_admin(message.from_user.id):
        await message.answer("⛔ Unauthorised.")
        return
    await message.answer(
        "🖥 <b>Remote Machine Administration</b>\n\n"
        f"WebSocket server: <code>{WS_HOST}:{WS_PORT}</code>\n"
        f"Connected devices: <b>{len(REGISTRY.alive())}</b>\n\n"
        "Select an action below:",
        reply_markup=kb_main_menu(),
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("menu"))
async def cmd_menu(message: Message) -> None:
    await cmd_start(message)


# ─── Main Menu Callbacks ──────────────────────────────────────────────────────

@router.callback_query(F.data == "main_menu")
async def cb_main_menu(query: CallbackQuery) -> None:
    if not _is_admin(query.from_user.id):
        await query.answer("Unauthorised", show_alert=True)
        return
    await query.message.edit_text(
        "🖥 <b>Remote Machine Administration</b>\n\n"
        f"Connected devices: <b>{len(REGISTRY.alive())}</b>",
        reply_markup=kb_main_menu(),
        parse_mode=ParseMode.HTML,
    )
    await query.answer()


@router.callback_query(F.data == "bot_status")
async def cb_bot_status(query: CallbackQuery) -> None:
    if not _is_admin(query.from_user.id):
        await query.answer("Unauthorised", show_alert=True)
        return
    all_devs   = REGISTRY.all()
    alive_devs = REGISTRY.alive()
    text = (
        "📊 <b>Bot Status</b>\n\n"
        f"• Total registered devices : <b>{len(all_devs)}</b>\n"
        f"• Online devices            : <b>{len(alive_devs)}</b>\n"
        f"• WS Server                 : <code>{WS_HOST}:{WS_PORT}</code>\n"
        f"• Bot uptime clock          : {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC\n"
    )
    b = InlineKeyboardBuilder()
    b.button(text="🏠 Main Menu", callback_data="main_menu")
    await query.message.edit_text(text, reply_markup=b.as_markup(), parse_mode=ParseMode.HTML)
    await query.answer()


# ─── Device List ──────────────────────────────────────────────────────────────

@router.callback_query(F.data == "list_devices")
async def cb_list_devices(query: CallbackQuery) -> None:
    if not _is_admin(query.from_user.id):
        await query.answer("Unauthorised", show_alert=True)
        return
    devices = REGISTRY.all()
    if not devices:
        b = InlineKeyboardBuilder()
        b.button(text="🏠 Main Menu", callback_data="main_menu")
        await query.message.edit_text(
            "📋 <b>No devices connected yet.</b>\n\n"
            "Deploy the client script on your machines to see them here.",
            reply_markup=b.as_markup(),
            parse_mode=ParseMode.HTML,
        )
    else:
        await query.message.edit_text(
            f"📋 <b>Connected Devices</b>  ({len(devices)} total, {len(REGISTRY.alive())} online)",
            reply_markup=kb_device_list(devices),
            parse_mode=ParseMode.HTML,
        )
    await query.answer()


# ─── Device Detail ────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("dev:"))
async def cb_device_detail(query: CallbackQuery) -> None:
    if not _is_admin(query.from_user.id):
        await query.answer("Unauthorised", show_alert=True)
        return
    device_id = query.data.split(":", 1)[1]
    dev = REGISTRY.get(device_id)
    if not dev:
        await query.answer("Device not found", show_alert=True)
        return
    status = "🟢 Online" if dev.is_alive else "🔴 Offline"
    text = (
        f"🖥 <b>{dev.hostname}</b>\n\n"
        f"• Status   : {status}\n"
        f"• Platform : {dev.platform.capitalize()}\n"
        f"• User     : <code>{dev.username}</code>\n"
        f"• ID       : <code>{dev.device_id[:16]}…</code>\n"
        f"• Connected: {dev.connected_at.strftime('%Y-%m-%d %H:%M')} UTC\n"
    )
    await query.message.edit_text(
        text, reply_markup=kb_device_control(device_id), parse_mode=ParseMode.HTML
    )
    await query.answer()


# ─── Command Dispatcher ───────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("cmd:"))
async def cb_command(query: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(query.from_user.id):
        await query.answer("Unauthorised", show_alert=True)
        return

    parts     = query.data.split(":", 2)
    cmd_name  = parts[1]
    device_id = parts[2]
    dev       = REGISTRY.get(device_id)

    if not dev:
        await query.answer("Device not found", show_alert=True)
        return
    if not dev.is_alive:
        await query.answer("⚠️ Device appears offline", show_alert=True)
        return

    # ── Commands that need confirmation ──
    if cmd_name in ("shutdown", "reboot"):
        icon = "⚠️" if cmd_name == "shutdown" else "🔃"
        await query.message.edit_text(
            f"{icon} <b>Confirm {cmd_name.upper()}</b> on <code>{dev.hostname}</code>?",
            reply_markup=kb_confirm(cmd_name, device_id),
            parse_mode=ParseMode.HTML,
        )
        await query.answer()
        return

    # ── Screenshot ────────────────────────────────────────────────────────
    if cmd_name == "screenshot":
        await query.answer("📸 Capturing…")
        await query.message.edit_text(f"📸 Capturing screenshot from <b>{dev.hostname}</b>…",
                                      parse_mode=ParseMode.HTML)
        try:
            resp = await send_command(dev, "screenshot", timeout=20)
            img_b64 = resp.get("data", "")
            img_bytes = base64.b64decode(img_b64)
            ts = datetime.utcnow().strftime("%H%M%S")
            await query.message.answer_photo(
                BufferedInputFile(img_bytes, filename=f"screen_{ts}.png"),
                caption=f"📸 <b>{dev.hostname}</b> — {datetime.utcnow().strftime('%H:%M:%S')} UTC",
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            await query.message.answer(f"❌ Screenshot failed: <code>{e}</code>",
                                       parse_mode=ParseMode.HTML)
        await query.message.answer(f"Device: <b>{dev.label}</b>",
                                   reply_markup=kb_device_control(device_id),
                                   parse_mode=ParseMode.HTML)
        return

    # ── Process List ──────────────────────────────────────────────────────
    if cmd_name == "processes":
        await query.answer("📊 Fetching processes…")
        await query.message.edit_text("📊 Fetching process list…")
        try:
            resp      = await send_command(dev, "processes", timeout=15)
            processes = resp.get("data", [])
            # Sort by CPU desc
            processes = sorted(processes, key=lambda p: p.get("cpu", 0), reverse=True)
            count     = len(processes)
            await query.message.edit_text(
                f"📊 <b>Process List</b> on <b>{dev.hostname}</b>  ({count} running)\n"
                f"Click a process name to kill it:",
                reply_markup=kb_process_list(processes, device_id),
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            await query.message.answer(f"❌ Process fetch failed: <code>{e}</code>",
                                       parse_mode=ParseMode.HTML)
        return

    # ── Shell Command ─────────────────────────────────────────────────────
    if cmd_name == "shell":
        await state.update_data(device_id=device_id)
        await state.set_state(AdminStates.waiting_shell_input)
        b = InlineKeyboardBuilder()
        b.button(text="❌ Cancel", callback_data=f"dev:{device_id}")
        await query.message.edit_text(
            f"💻 <b>Shell Command</b> on <b>{dev.hostname}</b>\n\n"
            "Send the command as a text message (e.g. <code>ipconfig /all</code>):",
            reply_markup=b.as_markup(),
            parse_mode=ParseMode.HTML,
        )
        await query.answer()
        return

    # ── Execute File ──────────────────────────────────────────────────────
    if cmd_name == "exec_file":
        await state.update_data(device_id=device_id)
        await state.set_state(AdminStates.waiting_exec_file)
        b = InlineKeyboardBuilder()
        b.button(text="❌ Cancel", callback_data=f"dev:{device_id}")
        await query.message.edit_text(
            f"📁 <b>Execute File</b> on <b>{dev.hostname}</b>\n\n"
            "Upload the file (EXE, BAT, PS1, PY, etc.) to execute on the remote machine.\n"
            "<i>⚠️ Only run scripts you trust on your own machines.</i>",
            reply_markup=b.as_markup(),
            parse_mode=ParseMode.HTML,
        )
        await query.answer()
        return

    # ── Alt+F4 ────────────────────────────────────────────────────────────
    if cmd_name == "altf4":
        await query.answer("Sending Alt+F4…")
        try:
            resp = await send_command(dev, "hotkey", {"keys": ["alt", "f4"]}, timeout=10)
            await query.message.answer(f"✅ Alt+F4 sent to <b>{dev.hostname}</b>",
                                       parse_mode=ParseMode.HTML)
        except Exception as e:
            await query.message.answer(f"❌ Failed: <code>{e}</code>", parse_mode=ParseMode.HTML)
        return

    # ── Lock ──────────────────────────────────────────────────────────────
    if cmd_name == "lock":
        await query.answer("🔒 Locking…")
        try:
            await send_command(dev, "lock_screen", timeout=10)
            await query.message.answer(f"🔒 Screen locked on <b>{dev.hostname}</b>",
                                       parse_mode=ParseMode.HTML)
        except Exception as e:
            await query.message.answer(f"❌ Lock failed: <code>{e}</code>", parse_mode=ParseMode.HTML)
        return

    # ── Volume ────────────────────────────────────────────────────────────
    if cmd_name in ("vol_up", "vol_down"):
        direction = "up" if cmd_name == "vol_up" else "down"
        await query.answer(f"🔊 Volume {direction}…")
        try:
            await send_command(dev, "volume", {"direction": direction}, timeout=10)
            icon = "🔊" if direction == "up" else "🔇"
            await query.message.answer(f"{icon} Volume {direction} on <b>{dev.hostname}</b>",
                                       parse_mode=ParseMode.HTML)
        except Exception as e:
            await query.message.answer(f"❌ Volume failed: <code>{e}</code>", parse_mode=ParseMode.HTML)
        return

    # ── System Info ───────────────────────────────────────────────────────
    if cmd_name == "sysinfo":
        await query.answer("🌐 Gathering system info…")
        try:
            resp = await send_command(dev, "sysinfo", timeout=20)
            info = resp.get("data", {})
            text = (
                f"🌐 <b>System Info</b> — <b>{dev.hostname}</b>\n\n"
                f"• OS        : {info.get('os', 'N/A')}\n"
                f"• CPU       : {info.get('cpu_model', 'N/A')}\n"
                f"• CPU usage : {info.get('cpu_pct', 0):.1f}%\n"
                f"• RAM total : {info.get('ram_total', 0):.1f} GB\n"
                f"• RAM used  : {info.get('ram_pct', 0):.1f}%\n"
                f"• Disk free : {info.get('disk_free', 0):.1f} GB\n"
                f"• Uptime    : {info.get('uptime', 'N/A')}\n"
                f"• IP (local): {info.get('local_ip', 'N/A')}\n"
            )
            await query.message.edit_text(text,
                                          reply_markup=kb_device_control(device_id),
                                          parse_mode=ParseMode.HTML)
        except Exception as e:
            await query.message.answer(f"❌ Sysinfo failed: <code>{e}</code>", parse_mode=ParseMode.HTML)
        return

    # ── File Browser ──────────────────────────────────────────────────────
    if cmd_name == "filebrowser":
        await query.answer("📂 Listing files…")
        try:
            resp  = await send_command(dev, "listdir", {"path": "."}, timeout=15)
            items = resp.get("data", [])
            lines = [f"📂 <b>File Browser</b> — <b>{dev.hostname}</b>\n"]
            for item in items[:40]:
                icon = "📁" if item.get("is_dir") else "📄"
                lines.append(f"{icon} {item['name']}  ({item.get('size','?')})")
            await query.message.edit_text(
                "\n".join(lines),
                reply_markup=kb_device_control(device_id),
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            await query.message.answer(f"❌ File browser failed: <code>{e}</code>",
                                       parse_mode=ParseMode.HTML)
        return

    await query.answer(f"⚙️ Unknown command: {cmd_name}", show_alert=True)


# ─── Confirmation Handler ─────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("confirm:"))
async def cb_confirm(query: CallbackQuery) -> None:
    if not _is_admin(query.from_user.id):
        await query.answer("Unauthorised", show_alert=True)
        return
    _, action, device_id = query.data.split(":", 2)
    dev = REGISTRY.get(device_id)
    if not dev:
        await query.answer("Device not found", show_alert=True)
        return
    try:
        await query.answer(f"⚡ Executing {action}…")
        await send_command(dev, action, timeout=15)
        icons = {"shutdown": "⚠️", "reboot": "🔃"}
        await query.message.edit_text(
            f"{icons.get(action,'✅')} <b>{action.upper()}</b> command sent to <b>{dev.hostname}</b>.",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        await query.message.edit_text(
            f"❌ <b>{action}</b> failed on <b>{dev.hostname}</b>: <code>{e}</code>",
            parse_mode=ParseMode.HTML,
        )


# ─── Kill Process ─────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("kill:"))
async def cb_kill_process(query: CallbackQuery) -> None:
    if not _is_admin(query.from_user.id):
        await query.answer("Unauthorised", show_alert=True)
        return
    _, pid_str, device_id = query.data.split(":", 2)
    dev = REGISTRY.get(device_id)
    if not dev:
        await query.answer("Device not found", show_alert=True)
        return
    try:
        await query.answer(f"❌ Killing PID {pid_str}…")
        resp = await send_command(dev, "kill_process", {"pid": int(pid_str)}, timeout=10)
        await query.message.answer(
            f"❌ Process PID <code>{pid_str}</code> killed on <b>{dev.hostname}</b>.",
            parse_mode=ParseMode.HTML,
        )
        # Refresh process list
        resp2     = await send_command(dev, "processes", timeout=15)
        processes = sorted(resp2.get("data", []), key=lambda p: p.get("cpu", 0), reverse=True)
        await query.message.edit_reply_markup(
            reply_markup=kb_process_list(processes, device_id)
        )
    except Exception as e:
        await query.message.answer(f"❌ Kill failed: <code>{e}</code>", parse_mode=ParseMode.HTML)


# ─── FSM: Shell Command Input ─────────────────────────────────────────────────

@router.message(AdminStates.waiting_shell_input)
async def fsm_shell_input(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    data      = await state.get_data()
    device_id = data.get("device_id")
    dev       = REGISTRY.get(device_id)
    if not dev:
        await message.answer("❌ Device disconnected.")
        await state.clear()
        return

    cmd_text = message.text.strip()
    await state.clear()
    status_msg = await message.answer(
        f"💻 Running: <code>{cmd_text}</code> on <b>{dev.hostname}</b>…",
        parse_mode=ParseMode.HTML,
    )
    try:
        resp   = await send_command(dev, "shell", {"command": cmd_text}, timeout=60)
        output = resp.get("data", {})
        stdout = output.get("stdout", "")[:4000]
        stderr = output.get("stderr", "")[:1000]
        rc     = output.get("returncode", "?")
        text   = (
            f"💻 <b>Shell Result</b> on <b>{dev.hostname}</b>\n"
            f"<code>$ {cmd_text}</code>\n\n"
        )
        if stdout:
            text += f"<b>STDOUT:</b>\n<code>{stdout}</code>\n\n"
        if stderr:
            text += f"<b>STDERR:</b>\n<code>{stderr}</code>\n\n"
        text += f"Return code: <code>{rc}</code>"
        await status_msg.edit_text(text, parse_mode=ParseMode.HTML)
    except Exception as e:
        await status_msg.edit_text(f"❌ Shell failed: <code>{e}</code>", parse_mode=ParseMode.HTML)

    await message.answer(f"Device: <b>{dev.label}</b>",
                         reply_markup=kb_device_control(device_id),
                         parse_mode=ParseMode.HTML)


# ─── FSM: Execute File Upload ─────────────────────────────────────────────────

@router.message(AdminStates.waiting_exec_file)
async def fsm_exec_file(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    if not message.document:
        await message.answer("❌ Please upload a file (document).")
        return

    data      = await state.get_data()
    device_id = data.get("device_id")
    dev       = REGISTRY.get(device_id)
    if not dev:
        await message.answer("❌ Device disconnected.")
        await state.clear()
        return

    await state.clear()
    doc: Document = message.document
    status_msg = await message.answer(
        f"📁 Uploading <b>{doc.file_name}</b> to <b>{dev.hostname}</b>…",
        parse_mode=ParseMode.HTML,
    )

    try:
        # Download the file content from Telegram
        from aiogram import Bot as BotClass
        bot_instance: BotClass = message.bot
        file_info = await bot_instance.get_file(doc.file_id)
        file_bytes_io = await bot_instance.download_file(file_info.file_path)
        file_bytes = file_bytes_io.read()

        # Send encoded file to client
        resp = await send_command(dev, "exec_file", {
            "filename":   doc.file_name,
            "data_b64":   base64.b64encode(file_bytes).decode(),
        }, timeout=90)

        result = resp.get("data", {})
        await status_msg.edit_text(
            f"📁 <b>Execute File Result</b> on <b>{dev.hostname}</b>\n\n"
            f"File: <code>{doc.file_name}</code>\n"
            f"Return code: <code>{result.get('returncode','?')}</code>\n\n"
            f"<code>{result.get('stdout','')[:2000]}</code>",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        await status_msg.edit_text(f"❌ Exec file failed: <code>{e}</code>",
                                   parse_mode=ParseMode.HTML)

    await message.answer(f"Device: <b>{dev.label}</b>",
                         reply_markup=kb_device_control(device_id),
                         parse_mode=ParseMode.HTML)


# ─── Client Generation ────────────────────────────────────────────────────────

@router.callback_query(F.data == "gen_client")
async def cb_gen_client(query: CallbackQuery) -> None:
    if not _is_admin(query.from_user.id):
        await query.answer("Unauthorised", show_alert=True)
        return
    await query.answer("Generating client…")

    # Generate a unique device token (for future per-device auth)
    device_token = secrets.token_hex(16)
    client_code  = _generate_client_script(device_token)

    await query.message.answer_document(
        BufferedInputFile(client_code.encode(), filename="rma_client.py"),
        caption=(
            f"🐍 <b>RMA Client Script</b>\n\n"
            f"Device token: <code>{device_token}</code>\n\n"
            f"<b>Windows deployment:</b>\n"
            f"<code>pip install websocket-client pyautogui pillow psutil pyinstaller\n"
            f"pyinstaller --onefile --noconsole rma_client.py</code>\n\n"
            f"Edit <code>WS_URL</code> and <code>SHARED_SECRET</code> at the top of the script.\n"
            f"<i>⚠️ Deploy only on machines you own.</i>"
        ),
        parse_mode=ParseMode.HTML,
    )


@router.callback_query(F.data == "build_installer")
async def cb_build_installer(query: CallbackQuery) -> None:
    if not _is_admin(query.from_user.id):
        await query.answer("Unauthorised", show_alert=True)
        return
    spec_code = _generate_pyinstaller_spec()
    await query.message.answer_document(
        BufferedInputFile(spec_code.encode(), filename="rma_client.spec"),
        caption=(
            "📦 <b>PyInstaller .spec File</b>\n\n"
            "Place alongside <code>rma_client.py</code> and run:\n"
            "<code>pyinstaller rma_client.spec</code>\n\n"
            "Produces a standalone <code>rma_client.exe</code> in <code>dist/</code>."
        ),
        parse_mode=ParseMode.HTML,
    )
    await query.answer()


# ─── Client Script Generator ──────────────────────────────────────────────────

def _generate_client_script(device_token: str) -> str:
    """Return the full source of the client script as a string."""
    # We read the template from disk (client/rma_client.py)
    template_path = os.path.join(os.path.dirname(__file__), "client", "rma_client.py")
    if os.path.exists(template_path):
        with open(template_path, "r", encoding="utf-8") as f:
            template = f.read()
        # Inject the bot-server config
        template = template.replace("__DEVICE_TOKEN__", device_token)
        template = template.replace("__WS_URL__", f"ws://{WS_HOST}:{WS_PORT}/ws")
        template = template.replace("__SHARED_SECRET__", SHARED_SECRET)
        return template
    return f"# Template not found. Expected at: {template_path}"


def _generate_pyinstaller_spec() -> str:
    return """\
# -*- mode: python ; coding: utf-8 -*-
# RMA Client — PyInstaller spec
# Run: pyinstaller rma_client.spec

a = Analysis(
    ['rma_client.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=['PIL', 'psutil', 'websocket', 'pyautogui'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='rma_client',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,       # set True for debugging
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
"""


# ─── WebSocket Server ─────────────────────────────────────────────────────────

async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    """
    Handles a single client WebSocket connection.
    Auth handshake → register device → relay commands → cleanup.
    """
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=64 * 1024 * 1024)
    await ws.prepare(request)

    device: Optional[DeviceInfo] = None
    log.info("WebSocket connection from %s", request.remote)

    try:
        # ── Auth handshake (first message must be a hello) ────────────────
        try:
            raw = await asyncio.wait_for(ws.receive(), timeout=15)
        except asyncio.TimeoutError:
            log.warning("Auth timeout from %s", request.remote)
            await ws.close(code=1008, message=b"Auth timeout")
            return ws

        if raw.type != aiohttp.WSMsgType.TEXT:
            await ws.close(code=1008, message=b"Expected JSON hello")
            return ws

        hello = json.loads(raw.data)
        # Verify HMAC
        recv_sig = hello.pop("sig", "")
        expected_sig = _sign_message(hello, SHARED_SECRET)
        if not hmac.compare_digest(recv_sig, expected_sig):
            log.warning("Invalid signature from %s", request.remote)
            await ws.close(code=1008, message=b"Invalid signature")
            return ws

        if hello.get("cmd") != "hello":
            await ws.close(code=1008, message=b"Expected hello command")
            return ws

        params    = hello.get("params", {})
        device_id = params.get("device_id") or str(uuid.uuid4())
        hostname  = params.get("hostname", "unknown")[:64]
        platform  = params.get("platform", "unknown").lower()
        username  = params.get("username", "unknown")[:32]

        device = DeviceInfo(device_id, hostname, platform, username, ws)
        await REGISTRY.register(device)
        log.info("Device registered: %s (%s@%s)", device_id[:8], username, hostname)

        # Acknowledge
        await ws.send_json({"req_id": hello.get("req_id", ""), "status": "ok",
                            "msg": "Authenticated"})

        # Notify admin in Telegram
        bot: Bot = request.app["bot"]
        if ADMIN_ID:
            try:
                await bot.send_message(
                    ADMIN_ID,
                    f"🟢 <b>New device connected</b>\n\n"
                    f"• Host     : <b>{hostname}</b>\n"
                    f"• Platform : {platform.capitalize()}\n"
                    f"• User     : <code>{username}</code>\n"
                    f"• ID       : <code>{device_id[:16]}…</code>",
                    reply_markup=kb_device_list(REGISTRY.all()),
                    parse_mode=ParseMode.HTML,
                )
            except Exception as notify_err:
                log.warning("Failed to notify admin: %s", notify_err)

        # ── Main message loop ─────────────────────────────────────────────
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue

                device.last_seen = time.time()
                req_id = data.get("req_id")
                if req_id and req_id in device.pending:
                    fut = device.pending.pop(req_id)
                    if not fut.done():
                        fut.set_result(data)

            elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                break

    except Exception as e:
        log.error("WebSocket error: %s", e, exc_info=True)

    finally:
        if device:
            await REGISTRY.unregister(device.device_id)
            log.info("Device unregistered: %s", device.device_id[:8])
            # Cancel pending futures
            for fut in device.pending.values():
                if not fut.done():
                    fut.cancel()
            # Notify admin
            bot: Bot = request.app.get("bot")
            if bot and ADMIN_ID and device:
                try:
                    await bot.send_message(
                        ADMIN_ID,
                        f"🔴 <b>Device disconnected</b>: <b>{device.hostname}</b>",
                        parse_mode=ParseMode.HTML,
                    )
                except Exception:
                    pass

    return ws


# ─── Application Factory ──────────────────────────────────────────────────────

async def build_app(bot: Bot, dp: Dispatcher) -> web.Application:
    app = web.Application()
    app["bot"] = bot
    app.router.add_get("/ws", ws_handler)

    # Graceful startup / shutdown
    async def on_startup(_app):
        log.info("Starting Telegram bot polling…")
        asyncio.create_task(dp.start_polling(bot, handle_signals=False))

    async def on_shutdown(_app):
        log.info("Shutting down…")
        await bot.session.close()

    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    return app


# ─── Entry Point ──────────────────────────────────────────────────────────────

async def main():
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("ERROR: Set BOT_TOKEN environment variable before running.")
        return
    if ADMIN_ID == 0:
        print("ERROR: Set ADMIN_ID environment variable to your Telegram user ID.")
        return
    if SHARED_SECRET == "CHANGE_ME_BEFORE_DEPLOYMENT_32ch":
        print("WARNING: Using default SHARED_SECRET — change it in production!")

    bot = Bot(token=BOT_TOKEN)
    dp  = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    app = await build_app(bot, dp)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, WS_HOST, WS_PORT)
    await site.start()
    log.info("RMA Bot running  |  WebSocket: ws://%s:%s/ws", WS_HOST, WS_PORT)

    try:
        await asyncio.Event().wait()   # run forever
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
