"""
Network Device Dashboard  (zero external dependencies)
--------------------------------------------------------
Monitors printers, receipt printers, computers, and phones on your LAN,
across one or more branches/locations. Checks each device on a schedule
using ICMP ping, falling back to a TCP port check (useful since many
printers/phones block ICMP but still have open ports like 9100 for
printers, or 80/443 for phones/computers).

Devices can be added, edited (including renaming and IP changes), and
removed right from the dashboard - no need to hand-edit devices.json,
though you still can.

Nothing to install to RUN this script directly - it uses only Python's
standard library. (Building the standalone .exe needs PyInstaller, but
only on the machine that builds it - see build_exe.bat.)

Run with:  python app.py   (or python3 app.py)
Then open: http://<this-machine's-ip>:5000  (it also opens automatically)
"""

import copy
import json
import os
import platform
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

# --------------------------------------------------------------------------
# Paths - aware of running as a plain script vs. a PyInstaller .exe
# --------------------------------------------------------------------------
# When frozen into an exe, bundled read-only assets (templates/static/the
# default devices.json) live in a temporary extraction folder (sys._MEIPASS)
# that disappears when the program exits. devices.json needs to live
# somewhere permanent instead, so it's kept next to the .exe itself.
if getattr(sys, "frozen", False):
    RESOURCE_DIR = sys._MEIPASS            # bundled assets (read-only)
    DATA_DIR = os.path.dirname(sys.executable)  # next to the .exe (writable, persists)
else:
    RESOURCE_DIR = os.path.dirname(os.path.abspath(__file__))
    DATA_DIR = RESOURCE_DIR

CONFIG_PATH = os.path.join(DATA_DIR, "devices.json")
DEFAULT_CONFIG_PATH = os.path.join(RESOURCE_DIR, "devices.json")  # bundled starter data
STATIC_DIR = os.path.join(RESOURCE_DIR, "static")
TEMPLATES_DIR = os.path.join(RESOURCE_DIR, "templates")
LOG_DB_PATH = os.path.join(DATA_DIR, "logs.db")  # persists next to the exe/script, same as devices.json

DEFAULT_BRANCH = "Unassigned"
LOG_RETENTION_DAYS = 7  # older check logs are pruned automatically so this doesn't grow forever

# User-changeable from the Settings popup (stored under "settings" in devices.json)
DEFAULT_SETTINGS = {
    "port": 5000,             # the number after the colon in the dashboard address
    "open_mode": "window",    # "window" = own app window, "browser" = browser tab, "none" = don't open
    "refresh_seconds": 5,     # how often every device is re-checked
    "allow_network": True,    # True = other computers on your network can view it too
}
OPEN_MODES = ("window", "browser", "none")

# In-memory current status, keyed by device name
STATUS_LOCK = threading.Lock()
CURRENT_STATUS = {}

# Guards reads/writes of devices.json so the monitor loop and the API
# never race each other mid-edit
CONFIG_LOCK = threading.Lock()

# Device types (computer, printer, ...) are edited from Settings > Device types
# and saved under "types" in devices.json. A single failed ping doesn't mean
# "offline" - lots of devices only answer on particular TCP ports - so each
# type lists the ports to try. "other" is the catch-all every device can fall
# back to, which is why it can't be deleted.
FALLBACK_TYPE_ID = "other"
ICON_KEYS = ("printer", "receipt", "computer", "phone", "tablet", "server", "router",
             "wifi", "camera", "monitor", "scanner", "speaker", "device")
DEFAULT_TYPES = [
    {"id": "computer", "name": "Computer", "icon": "computer", "ports": [445, 3389, 139, 135, 22, 80]},
    {"id": "phone", "name": "Phone", "icon": "phone", "ports": [80, 443, 5060]},
    {"id": "printer", "name": "Printer", "icon": "printer", "ports": [9100, 631, 515, 80]},
    {"id": "receipt_printer", "name": "Receipt Printer", "icon": "receipt", "ports": [9100, 631, 515, 80]},
    {"id": FALLBACK_TYPE_ID, "name": "Other", "icon": "device", "ports": [80, 443, 22]},
]
DEFAULT_PORTS_BY_TYPE = {t["id"]: list(t["ports"]) for t in DEFAULT_TYPES}

MIME_TYPES = {
    ".ico": "image/x-icon",
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
}


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
def ensure_config_exists():
    """First run of the .exe: devices.json won't exist next to it yet.
    Seed it from the bundled starter file (or an empty list) so the app
    doesn't crash on launch."""
    if os.path.exists(CONFIG_PATH):
        return
    if os.path.exists(DEFAULT_CONFIG_PATH) and DEFAULT_CONFIG_PATH != CONFIG_PATH:
        with open(DEFAULT_CONFIG_PATH, "r", encoding="utf-8-sig") as f:
            starter = f.read()
    else:
        starter = json.dumps(
            {"timeout_seconds": 1.5, "settings": DEFAULT_SETTINGS, "branches": [DEFAULT_BRANCH], "devices": []},
            indent=2,
        )
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        f.write(starter)


class ConfigError(Exception):
    """devices.json can't be read or saved right now (message is shown to the user)."""


# What the dashboard knows about the health of devices.json right now
CONFIG_STATE = {"error": None, "warnings": []}
_LAST_GOOD = {"config": None}  # last version of the file that parsed fine


def _friendly_json_error(text, e):
    """Turn Python's JSON complaint into plain English with the line that's wrong."""
    lines = text.splitlines()
    line_no = e.lineno
    snippet = lines[line_no - 1].strip() if 0 < line_no <= len(lines) else ""
    low = e.msg.lower()
    if any(ch in text for ch in "\u201c\u201d\u2018\u2019"):
        hint = ("The file contains curly quotes (\u201c \u201d). JSON only accepts straight quotes (\"). "
                "This usually happens when text is copied from Word, email or a chat app.")
    elif "//" in snippet or snippet.startswith("#") or "/*" in snippet:
        hint = "JSON doesn't allow comments (// or #). Delete that text."
    elif e.pos >= len(text.rstrip()) and ("delimiter" in low or "expecting" in low):
        hint = "The file looks unfinished - a closing } or ] is missing at the very end."
    elif "expecting value" in low and snippet[:1] in ("]", "}"):
        hint = f"There is an extra comma at the end of line {max(line_no - 1, 1)} (the last item before this closing bracket). Remove it."
    elif "expecting ',' delimiter" in low:
        hint = ("A comma is probably missing between two items on this line." if line_no <= 1 else
                f"A comma is probably missing - check the end of line {line_no - 1} "
                f"(the line above) and between items.")
    elif "expecting property name" in low:
        hint = "There is probably an extra comma after the last item in a list or block. Remove it."
    elif "expecting value" in low:
        hint = "Something is missing after a ':' or ',' - or there is an extra comma."
    elif "expecting ':' delimiter" in low:
        hint = "A ':' is missing between a name and its value."
    elif "unterminated string" in low or "invalid control character" in low:
        hint = "A quote mark (\") is missing at the end of a piece of text."
    elif "extra data" in low:
        hint = "There is extra text after the file's closing } - delete it, or look for a duplicated or extra } above."
    elif "bom" in low:
        hint = "The file has a hidden marker at the start. Re-save it as plain UTF-8."
    else:
        hint = ""
    return {
        "message": f"devices.json has a typing mistake on line {line_no}",
        "detail": e.msg,
        "line": line_no,
        "column": e.colno,
        "snippet": snippet[:200],
        "hint": hint,
        "has_backup": os.path.exists(CONFIG_PATH + ".bak"),
    }


def _fallback_config():
    return {"timeout_seconds": 1.5, "settings": dict(DEFAULT_SETTINGS),
            "branches": [DEFAULT_BRANCH], "devices": []}


def _dev_name(d):
    return str(d.get("name") or "").strip() if isinstance(d, dict) else ""


def load_config():
    """Read devices.json. If it has a mistake, keep running on the last good
    copy and record what's wrong (shown as a banner) - never crash."""
    if not os.path.exists(CONFIG_PATH):
        ensure_config_exists()
    # utf-8-sig also copes with the hidden marker Notepad/PowerShell can add
    with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
        text = f.read()
    try:
        config = json.loads(text)
        if isinstance(config, list):  # someone saved just the list of devices
            config = {"devices": config}
        if not isinstance(config, dict) or not isinstance(config.get("devices", []), list):
            raise ValueError("devices.json must contain a \"devices\" list")
    except (json.JSONDecodeError, ValueError) as e:
        if isinstance(e, json.JSONDecodeError):
            CONFIG_STATE["error"] = _friendly_json_error(text, e)
        else:
            CONFIG_STATE["error"] = {"message": "devices.json isn't set up the way the dashboard expects",
                                     "detail": str(e), "line": None, "column": None, "snippet": "",
                                     "hint": "It should look like { \"devices\": [ ... ] }.",
                                     "has_backup": os.path.exists(CONFIG_PATH + ".bak")}
        last = _LAST_GOOD["config"]
        return copy.deepcopy(last) if last is not None else _fallback_config()

    config.setdefault("devices", [])
    if "branches" not in config or not isinstance(config["branches"], list):
        # Older devices.json without an explicit branch list - default it in
        # memory; it gets saved next time something actually changes.
        config["branches"] = sorted({
            (d.get("branch") if isinstance(d, dict) else None) or DEFAULT_BRANCH
            for d in config["devices"]
        }) or [DEFAULT_BRANCH]
    CONFIG_STATE["error"] = None
    _LAST_GOOD["config"] = copy.deepcopy(config)
    return config


def read_config():
    with CONFIG_LOCK:
        return load_config()


def write_config(config):
    err = CONFIG_STATE["error"]
    if err:
        # Never overwrite a file the user is in the middle of fixing
        raise ConfigError(
            f"Your change wasn't saved: devices.json has a typing mistake"
            f"{' on line ' + str(err['line']) if err.get('line') else ''}. "
            f"Fix the file first, then try again."
        )
    with CONFIG_LOCK:
        try:
            if os.path.exists(CONFIG_PATH):
                shutil.copyfile(CONFIG_PATH, CONFIG_PATH + ".bak")  # one-step undo
        except OSError:
            pass
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
        os.replace(tmp, CONFIG_PATH)  # atomic: a reader never sees a half-written file
        _LAST_GOOD["config"] = copy.deepcopy(config)


def slugify(text):
    return re.sub(r"[^a-z0-9]+", "_", str(text or "").lower()).strip("_")


def clean_ports(raw):
    """Accepts [9100, 80] or "9100, 80". Returns (list, error_or_None)."""
    if isinstance(raw, str):
        raw = [p for p in re.split(r"[\s,;]+", raw.strip()) if p]
    if not isinstance(raw, list):
        return [], "Ports should be numbers separated by commas, like 9100, 80."
    ports = []
    for p in raw:
        try:
            n = int(p)
        except (TypeError, ValueError):
            return [], f"'{p}' isn't a port number. Use numbers between 1 and 65535, like 9100, 80."
        if not 1 <= n <= 65535:
            return [], f"Port {n} is out of range - use numbers between 1 and 65535."
        if n not in ports:
            ports.append(n)
    if len(ports) > 12:
        return [], "That's a lot of ports - keep it to 12 or fewer."
    return ports, None


def get_types(config=None):
    """The ordered list of device types (always includes the 'other' fallback)."""
    if config is None:
        config = read_config()
    types, seen = [], set()
    raw = config.get("types") if isinstance(config, dict) else None
    for t in (raw if isinstance(raw, list) else []):
        if not isinstance(t, dict):
            continue
        tid = slugify(t.get("id") or t.get("name"))
        name = str(t.get("name") or "").strip()
        if not tid or not name or tid in seen:
            continue
        ports, err = clean_ports(t.get("ports", []))
        icon = t.get("icon") if t.get("icon") in ICON_KEYS else "device"
        types.append({"id": tid, "name": name[:40], "icon": icon, "ports": [] if err else ports})
        seen.add(tid)
    if not types:
        types = copy.deepcopy(DEFAULT_TYPES)
    elif FALLBACK_TYPE_ID not in {t["id"] for t in types}:
        types.append(copy.deepcopy(DEFAULT_TYPES[-1]))
    return types


def resolve_type(raw, types):
    """Match what's written in a device's "type" (an id, a display name, or a
    plural like "printers") to a known type id. None if nothing matches."""
    key = slugify(raw)
    if not key:
        return None
    for candidate in (key, key[:-1] if key.endswith("s") else None):
        if not candidate:
            continue
        for t in types:
            if candidate == t["id"] or candidate == slugify(t["name"]):
                return t["id"]
    return None


def unique_type_id(name, taken):
    base = slugify(name) or "type"
    tid, n = base, 2
    while tid in taken:
        tid, n = f"{base}_{n}", n + 1
    return tid


def validate_devices(devices, types=None):
    """Return (usable devices, plain-English warnings). Bad entries are skipped
    for monitoring but left untouched in the file so nothing is lost."""
    types = types or get_types({})
    good, warnings, seen = [], [], set()
    ips_seen = {}
    for i, d in enumerate(devices, 1):
        if not isinstance(d, dict):
            warnings.append(f"Entry #{i} isn't a device (it should look like {{ \"name\": ..., \"ip\": ... }}) - skipped.")
            continue
        name = _dev_name(d)
        label = f"'{name}'" if name else f"Entry #{i}"
        if not name:
            warnings.append(f"{label} has no name - skipped.")
            continue
        ip = str(d.get("ip") or "").strip()
        if not is_valid_ip(ip):
            warnings.append(f"{label}: '{ip}' isn't a valid IP address (it should look like 192.168.1.50) - skipped.")
            continue
        if name in seen:
            warnings.append(f"{label} appears more than once - only the first one is used. Names must be unique.")
            continue
        if ip in ips_seen:
            warnings.append(f"{label} and '{ips_seen[ip]}' both use {ip} - one of them is probably a typo.")
        ips_seen.setdefault(ip, name)
        raw_type = resolve_type(d.get("type"), types)
        if raw_type is None:
            if str(d.get("type") or "").strip():
                warnings.append(f"{label}: unknown type '{d.get('type')}' - shown as Other. Add it under "
                                f"Settings > Device types, or use one of: {', '.join(t['name'] for t in types)}.")
            raw_type = FALLBACK_TYPE_ID
        dev = dict(d)
        dev.update(name=name, ip=ip, type=raw_type,
                   branch=str(d.get("branch") or "").strip() or DEFAULT_BRANCH)
        if d.get("port") not in (None, ""):
            try:
                port = int(d["port"])
                if not 1 <= port <= 65535:
                    raise ValueError
                dev["port"] = port
            except (TypeError, ValueError):
                warnings.append(f"{label}: port '{d.get('port')}' isn't a number between 1 and 65535 - ignored.")
                dev.pop("port", None)
        seen.add(name)
        good.append(dev)
    return good, warnings


def is_valid_ip(ip):
    parts = ip.split(".")
    if len(parts) != 4:
        return False
    for p in parts:
        if not p.isdigit() or not (0 <= int(p) <= 255):
            return False
    return True


def clean_settings(raw):
    """Validate/normalise settings. Returns (settings, error_message_or_None)."""
    out = dict(DEFAULT_SETTINGS)
    if not isinstance(raw, dict):
        raw = {}
    out.update({k: v for k, v in raw.items() if k in DEFAULT_SETTINGS})
    try:
        out["port"] = int(out["port"])
    except (TypeError, ValueError):
        return out, "Port must be a number."
    if not (1024 <= out["port"] <= 65535):
        return out, "Port must be between 1024 and 65535."
    try:
        out["refresh_seconds"] = int(out["refresh_seconds"])
    except (TypeError, ValueError):
        return out, "Refresh time must be a number of seconds."
    if not (2 <= out["refresh_seconds"] <= 300):
        return out, "Refresh time must be between 2 and 300 seconds."
    if out["open_mode"] not in OPEN_MODES:
        return out, "Unknown open mode."
    out["allow_network"] = bool(out["allow_network"])
    return out, None


def get_settings(config=None):
    if config is None:
        config = read_config()
    settings, err = clean_settings(config.get("settings"))
    return settings if not err else dict(DEFAULT_SETTINGS)


def bind_host(settings):
    return "0.0.0.0" if settings["allow_network"] else "127.0.0.1"


def get_lan_ip():
    """Best-effort: this PC's address on the local network (for the 'other computers' hint)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sk:
            sk.connect(("10.255.255.255", 1))  # no packets are actually sent
            return sk.getsockname()[0]
    except Exception:
        return None


def now_iso():
    """Clean, JS-Date-parseable UTC timestamp: 2026-10-06T19:40:00Z
    (no microseconds, no duplicated timezone marker)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# Check log (SQLite - part of Python's standard library, nothing to install)
# --------------------------------------------------------------------------
_log_db_lock = threading.Lock()


def init_log_db():
    with _log_db_lock:
        conn = sqlite3.connect(LOG_DB_PATH)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                device_name TEXT NOT NULL,
                branch TEXT NOT NULL,
                type TEXT NOT NULL,
                ip TEXT NOT NULL,
                online INTEGER NOT NULL,
                response_ms REAL,
                method TEXT,
                detail TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_device_time ON logs(device_name, timestamp)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_time ON logs(timestamp)")
        conn.commit()
        conn.close()


def write_logs(entries):
    with _log_db_lock:
        conn = sqlite3.connect(LOG_DB_PATH)
        conn.executemany(
            """INSERT INTO logs (timestamp, device_name, branch, type, ip, online, response_ms, method, detail)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (e["last_checked"], e["name"], e["branch"], e["type"], e["ip"],
                 int(bool(e["online"])), e["response_ms"], e["method"], e.get("detail", ""))
                for e in entries
            ],
        )
        conn.commit()
        conn.close()


def write_log(entry):
    with _log_db_lock:
        conn = sqlite3.connect(LOG_DB_PATH)
        conn.execute(
            """INSERT INTO logs (timestamp, device_name, branch, type, ip, online, response_ms, method, detail)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                entry["last_checked"],
                entry["name"],
                entry["branch"],
                entry["type"],
                entry["ip"],
                int(bool(entry["online"])),
                entry["response_ms"],
                entry["method"],
                entry.get("detail", ""),
            ),
        )
        conn.commit()
        conn.close()


_prune_counter = [0]


def maybe_prune_logs():
    # Cheap counter-based throttle so we're not running a DELETE on every
    # single check cycle - only every ~60th pass (roughly once every 15min
    # at the default 15s interval).
    _prune_counter[0] += 1
    if _prune_counter[0] % 60 != 0:
        return
    cutoff = (datetime.now(timezone.utc) - timedelta(days=LOG_RETENTION_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with _log_db_lock:
        conn = sqlite3.connect(LOG_DB_PATH)
        conn.execute("DELETE FROM logs WHERE timestamp < ?", (cutoff,))
        conn.commit()
        conn.close()


def query_logs(device=None, branch=None, result=None, since=None, search=None, limit=200, offset=0):
    """result: None/'all', 'online', or 'offline'. since: ISO timestamp string, only rows newer than it."""
    clauses = []
    params = []

    if device and device != "all":
        clauses.append("device_name = ?")
        params.append(device)
    if branch and branch != "all":
        clauses.append("branch = ?")
        params.append(branch)
    if result in ("online", "offline"):
        clauses.append("online = ?")
        params.append(1 if result == "online" else 0)
    if since:
        clauses.append("timestamp >= ?")
        params.append(since)
    if search:
        clauses.append("(device_name LIKE ? OR ip LIKE ?)")
        like = f"%{search}%"
        params.extend([like, like])

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    with _log_db_lock:
        conn = sqlite3.connect(LOG_DB_PATH)
        conn.row_factory = sqlite3.Row
        total = conn.execute(f"SELECT COUNT(*) FROM logs {where}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM logs {where} ORDER BY timestamp DESC, id DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
        conn.close()

    logs = [
        {
            "timestamp": r["timestamp"],
            "device_name": r["device_name"],
            "branch": r["branch"],
            "type": r["type"],
            "ip": r["ip"],
            "online": bool(r["online"]),
            "response_ms": r["response_ms"],
            "method": r["method"],
            "detail": r["detail"],
        }
        for r in rows
    ]
    return logs, total


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------
def ping_host(ip, timeout_s):
    """Returns (success, response_ms, detail) using the system ping command.

    Success requires an actual echo reply (the output must contain TTL=).
    Windows prints "Reply from <ip>: Destination host unreachable" for
    addresses nobody is using and still exits with code 0, so the exit code
    alone can't be trusted."""
    is_windows = "windows" in platform.system().lower()
    start = time.time()
    try:
        if is_windows:
            cmd = ["ping", "-n", "1", "-w", str(int(timeout_s * 1000)), ip]
        else:
            cmd = ["ping", "-c", "1", "-W", str(int(max(1, timeout_s))), ip]
        kwargs = {}
        if is_windows:
            kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW - no flashing console windows
        result = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            errors="replace", timeout=timeout_s + 2, **kwargs
        )
        elapsed_ms = round((time.time() - start) * 1000, 1)
        out = (result.stdout or "").lower()

        if result.returncode == 0 and "ttl=" in out:
            m = re.search(r"time\s*[=<]\s*([\d.]+)\s*ms", out)
            return (True, float(m.group(1)) if m else elapsed_ms, "ok")

        if "unreachable" in out:
            detail = "destination unreachable (nothing at this address)"
        elif "timed out" in out or "timeout" in out:
            detail = "timed out"
        elif "could not find host" in out or "unknown host" in out or "name or service not known" in out:
            detail = "could not resolve host"
        else:
            detail = "no reply"
        return (False, None, detail)
    except subprocess.TimeoutExpired:
        return (False, None, "command timed out")
    except FileNotFoundError:
        return (False, None, "command not found on this system")
    except Exception as e:
        return (False, None, f"error: {e}")


def tcp_check(ip, port, timeout_s):
    start = time.time()
    try:
        with socket.create_connection((ip, port), timeout=timeout_s):
            elapsed_ms = round((time.time() - start) * 1000, 1)
            return (True, elapsed_ms, "ok")
    except socket.timeout:
        return (False, None, "connection timed out")
    except ConnectionRefusedError:
        return (False, None, "connection refused (port closed)")
    except OSError as e:
        return (False, None, f"unreachable ({e.strerror or e})")
    except Exception as e:
        return (False, None, f"error: {e}")


def check_device(device, timeout_s, ports_by_type=None):
    ip = device["ip"]
    dtype = device.get("type", "computer")

    # A device-specific port override always gets tried first, then the
    # type's usual fallback ports.
    ports_to_try = []
    if device.get("port"):
        ports_to_try.append(int(device["port"]))
    for p in (ports_by_type or DEFAULT_PORTS_BY_TYPE).get(dtype, []):
        if p not in ports_to_try:
            ports_to_try.append(p)

    online, response_ms, ping_detail = ping_host(ip, timeout_s)
    method = "ping"
    detail = ping_detail

    if not online:
        if ping_detail.startswith("destination unreachable"):
            # The network itself says nothing is at this address, so TCP
            # checks would only waste time (or hit a router that answers for it).
            method = "ping"
            detail = f"ping: {ping_detail}"
        elif ports_to_try:
            with ThreadPoolExecutor(max_workers=len(ports_to_try)) as pool:
                results = list(pool.map(lambda p: (p, tcp_check(ip, p, timeout_s)), ports_to_try))
            attempts = []
            for port, (tcp_ok, tcp_ms, tcp_detail) in results:
                attempts.append(f"tcp:{port} {tcp_detail}")
                if tcp_ok and not online:
                    online, response_ms, method = True, tcp_ms, f"tcp:{port}"
                    detail = "ok"
            if not online:
                method = "ping+tcp"
                detail = f"ping: {ping_detail}; " + ", ".join(attempts)
        else:
            detail = f"ping: {ping_detail}"

    return {
        "name": device["name"],
        "ip": ip,
        "type": dtype,
        "branch": device.get("branch") or DEFAULT_BRANCH,
        "online": online,
        "response_ms": response_ms,
        "method": method,
        "detail": detail,
        "last_checked": now_iso(),
    }


# --------------------------------------------------------------------------
# Background monitor loop
# --------------------------------------------------------------------------
def monitor_loop():
    while True:
        cycle_start = time.time()
        try:
            config = read_config()
        except Exception as e:
            print(f"[monitor] Failed to load devices.json: {e}")
            time.sleep(10)
            continue

        types = get_types(config)
        ports_by_type = {t["id"]: t["ports"] for t in types}
        devices, CONFIG_STATE["warnings"] = validate_devices(config.get("devices", []), types)
        try:
            timeout_s = min(max(float(config.get("timeout_seconds", 1.5)), 0.5), 10.0)
        except (TypeError, ValueError):
            timeout_s = 1.5
        interval = get_settings(config)["refresh_seconds"]

        threads = []
        results = {}

        def worker(dev):
            try:
                r = check_device(dev, timeout_s, ports_by_type)
            except Exception as e:  # one odd entry must never silence a device
                r = {"name": dev["name"], "ip": dev["ip"], "type": dev["type"], "branch": dev["branch"],
                     "online": False, "response_ms": None, "method": "error",
                     "detail": f"check failed: {e}", "last_checked": now_iso()}
            results[dev["name"]] = r

        for dev in devices:
            t = threading.Thread(target=worker, args=(dev,))
            t.start()
            threads.append(t)
        for t in threads:
            t.join()

        with STATUS_LOCK:
            # Drop stale entries for devices that were deleted or renamed via the UI
            valid_names = {d["name"] for d in devices}
            for stale in [n for n in CURRENT_STATUS if n not in valid_names]:
                CURRENT_STATUS.pop(stale, None)

            for name, r in results.items():
                prev = CURRENT_STATUS.get(name)
                if prev and prev["online"] != r["online"]:
                    state = "back ONLINE" if r["online"] else "went OFFLINE"
                    print(f"[monitor] {name} ({r['ip']}) {state} - {r['detail']}")
                CURRENT_STATUS[name] = r

        try:
            write_logs(list(results.values()))
        except Exception as e:
            print(f"[monitor] Failed to write log entries: {e}")
        maybe_prune_logs()

        # Keep a steady cadence: sleep only for what's left of the interval
        time.sleep(max(0.5, interval - (time.time() - cycle_start)))


# --------------------------------------------------------------------------
# HTTP server (stdlib only - no Flask, nothing to install)
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # keep the console quiet; the monitor loop already prints state changes

    # ---- helpers ----
    def _send_json(self, data, code=200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path, content_type):
        try:
            with open(path, "rb") as f:
                body = f.read()
        except FileNotFoundError:
            self._send_json({"error": "Not found"}, 404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw)
        except Exception:
            return {}

    # ---- routing ----
    def do_GET(self):
        path = urlparse(self.path).path

        if path in ("/", "/index.html"):
            self._send_file(os.path.join(TEMPLATES_DIR, "index.html"), MIME_TYPES[".html"])
        elif path.startswith("/static/"):
            rel = unquote(path[len("/static/"):])
            safe_path = os.path.normpath(os.path.join(STATIC_DIR, rel))
            if not safe_path.startswith(STATIC_DIR):
                self._send_json({"error": "Forbidden"}, 403)
                return
            ext = os.path.splitext(safe_path)[1]
            self._send_file(safe_path, MIME_TYPES.get(ext, "application/octet-stream"))
        elif path == "/api/status":
            with STATUS_LOCK:
                devices = list(CURRENT_STATUS.values())
            # Merge the persisted branch list (so an empty, newly-created
            # branch still shows up) with whatever's actually in use.
            try:
                _cfg = read_config()
                persisted_branches = set(_cfg.get("branches", []))
                types_out = [{"id": t["id"], "name": t["name"], "icon": t["icon"]} for t in get_types(_cfg)]
            except Exception:
                persisted_branches, types_out = set(), []
            branches = sorted(persisted_branches | {d["branch"] for d in devices})
            summary = {
                "total": len(devices),
                "online": sum(1 for d in devices if d["online"]),
                "offline": sum(1 for d in devices if not d["online"]),
            }
            try:
                refresh = get_settings()["refresh_seconds"]
            except Exception:
                refresh = DEFAULT_SETTINGS["refresh_seconds"]
            self._send_json({"devices": devices, "summary": summary, "branches": branches,
                             "refresh_seconds": refresh,
                             "config_error": CONFIG_STATE["error"],
                             "config_warnings": CONFIG_STATE["warnings"],
                             "config_path": CONFIG_PATH,
                             "types": types_out})
        elif path == "/favicon.ico":
            self._send_file(os.path.join(RESOURCE_DIR, "dashboard.ico"), MIME_TYPES[".ico"])
        elif path == "/api/settings":
            settings = get_settings()
            settings["lan_ip"] = get_lan_ip()
            settings["running_port"] = RUNTIME.get("port")
            self._send_json(settings)
        elif path == "/api/devices":
            self._send_json(read_config())
        elif path == "/api/types":
            config = read_config()
            types = get_types(config)
            counts = {}
            for d in config.get("devices", []):
                if isinstance(d, dict):
                    tid = resolve_type(d.get("type"), types) or FALLBACK_TYPE_ID
                    counts[tid] = counts.get(tid, 0) + 1
            self._send_json({
                "types": [dict(t, device_count=counts.get(t["id"], 0), fallback=t["id"] == FALLBACK_TYPE_ID)
                          for t in types],
                "icons": list(ICON_KEYS),
            })
        elif path == "/api/branches":
            config = read_config()
            branches = config.get("branches", [])
            counts = {}
            for d in config.get("devices", []):
                b = d.get("branch") or DEFAULT_BRANCH
                counts[b] = counts.get(b, 0) + 1
            self._send_json({
                "branches": [{"name": b, "device_count": counts.get(b, 0)} for b in sorted(branches)]
            })
        elif path == "/api/logs":
            qs = parse_qs(urlparse(self.path).query)

            def qget(key, default=None):
                vals = qs.get(key)
                return vals[0] if vals else default

            device = qget("device", "all")
            branch = qget("branch", "all")
            result = qget("result", "all")
            search = qget("search", "").strip()
            range_key = qget("range", "24h")

            since = None
            now = datetime.now(timezone.utc)
            if range_key == "1h":
                since = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
            elif range_key == "24h":
                since = (now - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
            elif range_key == "7d":
                since = (now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
            # range_key == "all" (or anything else) -> since stays None

            try:
                limit = min(max(int(qget("limit", 200)), 1), 1000)
            except ValueError:
                limit = 200
            try:
                offset = max(int(qget("offset", 0)), 0)
            except ValueError:
                offset = 0

            try:
                logs, total = query_logs(
                    device=device, branch=branch, result=result,
                    since=since, search=search or None, limit=limit, offset=offset,
                )
                self._send_json({"logs": logs, "total": total, "limit": limit, "offset": offset})
            except Exception as e:
                self._send_json({"error": f"Could not read logs: {e}"}, 500)
        else:
            self._send_json({"error": "Not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/quit":
            self._send_json({"ok": True})
            threading.Timer(0.4, lambda: os._exit(0)).start()
            return
        if path == "/api/types":
            self._handle_type_create()
            return
        if path == "/api/branches":
            config = read_config()
            body = self._read_json_body()
            name = (body.get("name") or "").strip()
            if not name:
                self._send_json({"error": "Branch name can't be empty."}, 400)
                return
            branches = config.setdefault("branches", [])
            if name in branches:
                self._send_json({"error": f"Branch '{name}' already exists."}, 400)
                return
            branches.append(name)
            write_config(config)
            self._send_json({"ok": True, "branch": name})
            return
        if path != "/api/devices":
            self._send_json({"error": "Not found"}, 404)
            return

        config = read_config()
        body = self._read_json_body()
        name = (body.get("name") or "").strip()
        ip = (body.get("ip") or "").strip()
        dtype = body.get("type", "computer")
        branch = (body.get("branch") or "").strip() or DEFAULT_BRANCH

        if not name or not ip:
            self._send_json({"error": "Name and IP are required."}, 400)
            return
        if not is_valid_ip(ip):
            self._send_json({"error": f"'{ip}' doesn't look like a valid IPv4 address."}, 400)
            return
        valid_ids = sorted(t["id"] for t in get_types(config))
        if dtype not in valid_ids:
            self._send_json({"error": f"Type must be one of {valid_ids}."}, 400)
            return
        if any(_dev_name(d) == name for d in config["devices"]):
            self._send_json({"error": f"A device named '{name}' already exists."}, 400)
            return

        new_device = {"name": name, "ip": ip, "type": dtype, "branch": branch}
        if body.get("port"):
            new_device["port"] = int(body["port"])

        config["devices"].append(new_device)
        branches = config.setdefault("branches", [])
        if branch not in branches:
            branches.append(branch)
        write_config(config)
        self._send_json({"ok": True, "device": new_device})

    def do_PUT(self):
        path = urlparse(self.path).path

        if path == "/api/settings":
            self._handle_settings_update()
            return

        if path.startswith("/api/types/"):
            self._handle_type_update(unquote(path[len("/api/types/"):]))
            return

        if path.startswith("/api/branches/"):
            self._handle_branch_rename(unquote(path[len("/api/branches/"):]))
            return

        if not path.startswith("/api/devices/"):
            self._send_json({"error": "Not found"}, 404)
            return

        device_name = unquote(path[len("/api/devices/"):])
        config = read_config()
        devices = config["devices"]
        idx = next((i for i, d in enumerate(devices) if _dev_name(d) == device_name), None)

        if idx is None:
            self._send_json({"error": f"No device named '{device_name}'."}, 404)
            return

        body = self._read_json_body()
        new_name = (body.get("name") or device_name).strip()
        new_ip = (body.get("ip") or devices[idx]["ip"]).strip()
        new_type = body.get("type", devices[idx].get("type", "computer"))
        new_branch = (body.get("branch") or devices[idx].get("branch") or DEFAULT_BRANCH).strip()

        if not new_name:
            self._send_json({"error": "Name can't be empty."}, 400)
            return
        if not is_valid_ip(new_ip):
            self._send_json({"error": f"'{new_ip}' doesn't look like a valid IPv4 address."}, 400)
            return
        valid_ids = sorted(t["id"] for t in get_types(config))
        if new_type not in valid_ids:
            self._send_json({"error": f"Type must be one of {valid_ids}."}, 400)
            return
        if new_name != device_name and any(_dev_name(d) == new_name for d in devices):
            self._send_json({"error": f"A device named '{new_name}' already exists."}, 400)
            return

        devices[idx]["name"] = new_name
        devices[idx]["ip"] = new_ip
        devices[idx]["type"] = new_type
        devices[idx]["branch"] = new_branch
        if body.get("port"):
            devices[idx]["port"] = int(body["port"])
        elif "port" in body:
            devices[idx].pop("port", None)

        branches = config.setdefault("branches", [])
        if new_branch not in branches:
            branches.append(new_branch)

        write_config(config)

        # Keep the in-memory status map in sync: re-key it AND refresh every
        # field inside (including "name") so a renamed device shows correctly
        # immediately, and so it can be renamed again right away without
        # the server losing track of it. (This was the bug behind renaming
        # "getting funny" - the old code only moved the dict key, not the
        # name field stored inside it.)
        with STATUS_LOCK:
            old_status = CURRENT_STATUS.pop(device_name, None)
            if old_status:
                old_status["name"] = new_name
                old_status["ip"] = new_ip
                old_status["type"] = new_type
                old_status["branch"] = new_branch
                CURRENT_STATUS[new_name] = old_status

        self._send_json({"ok": True, "device": devices[idx]})

    def do_DELETE(self):
        path = urlparse(self.path).path

        if path.startswith("/api/branches/"):
            self._handle_branch_delete(unquote(path[len("/api/branches/"):]))
            return

        if path.startswith("/api/types/"):
            self._handle_type_delete(unquote(path[len("/api/types/"):]))
            return

        if not path.startswith("/api/devices/"):
            self._send_json({"error": "Not found"}, 404)
            return

        device_name = unquote(path[len("/api/devices/"):])
        config = read_config()
        devices = config["devices"]
        idx = next((i for i, d in enumerate(devices) if _dev_name(d) == device_name), None)

        if idx is None:
            self._send_json({"error": f"No device named '{device_name}'."}, 404)
            return

        removed = devices.pop(idx)
        write_config(config)
        with STATUS_LOCK:
            CURRENT_STATUS.pop(device_name, None)
        self._send_json({"ok": True, "removed": _dev_name(removed)})

    # ---- device types (add / rename / change icon & ports / delete) ----
    def _type_fields(self, body, existing=None):
        """Validate name / icon / ports from a request. Returns (fields, error)."""
        name = str(body.get("name", existing["name"] if existing else "") or "").strip()
        if not name:
            return None, "Give the type a name."
        if len(name) > 40:
            return None, "Keep the name under 40 characters."
        icon = body.get("icon", existing["icon"] if existing else "device")
        if icon not in ICON_KEYS:
            icon = "device"
        ports, err = clean_ports(body["ports"] if "ports" in body else (existing["ports"] if existing else []))
        if err:
            return None, err
        return {"name": name, "icon": icon, "ports": ports}, None

    def _handle_type_create(self):
        config = read_config()
        types = get_types(config)
        fields, err = self._type_fields(self._read_json_body())
        if err:
            self._send_json({"error": err}, 400)
            return
        if any(t["name"].lower() == fields["name"].lower() for t in types):
            self._send_json({"error": f"A type named '{fields['name']}' already exists."}, 400)
            return
        new_type = dict(fields, id=unique_type_id(fields["name"], {t["id"] for t in types}))
        fallback_at = next(i for i, t in enumerate(types) if t["id"] == FALLBACK_TYPE_ID)
        types.insert(fallback_at, new_type)  # keep "Other" last
        config["types"] = types
        write_config(config)
        self._send_json({"ok": True, "type": new_type})

    def _handle_type_update(self, type_id):
        config = read_config()
        types = get_types(config)
        current = next((t for t in types if t["id"] == type_id), None)
        if current is None:
            self._send_json({"error": f"No type '{type_id}'."}, 404)
            return
        fields, err = self._type_fields(self._read_json_body(), existing=current)
        if err:
            self._send_json({"error": err}, 400)
            return
        if any(t["id"] != type_id and t["name"].lower() == fields["name"].lower() for t in types):
            self._send_json({"error": f"A type named '{fields['name']}' already exists."}, 400)
            return
        current.update(fields)  # the id never changes, so devices keep following the type through a rename
        config["types"] = types
        write_config(config)
        self._send_json({"ok": True, "type": current})

    def _handle_type_delete(self, type_id):
        config = read_config()
        types = get_types(config)
        if not any(t["id"] == type_id for t in types):
            self._send_json({"error": f"No type '{type_id}'."}, 404)
            return
        if type_id == FALLBACK_TYPE_ID:
            self._send_json({"error": "'Other' is the catch-all type and can't be deleted (you can rename it)."}, 400)
            return
        reassigned = 0
        for d in config.get("devices", []):
            if isinstance(d, dict) and resolve_type(d.get("type"), types) == type_id:
                d["type"] = FALLBACK_TYPE_ID
                reassigned += 1
        config["types"] = [t for t in types if t["id"] != type_id]
        write_config(config)
        with STATUS_LOCK:
            for status in CURRENT_STATUS.values():
                if status.get("type") == type_id:
                    status["type"] = FALLBACK_TYPE_ID
        self._send_json({"ok": True, "removed": type_id, "devices_reassigned": reassigned})

    # ---- settings (port / window mode / refresh rate / network access) ----
    def _handle_settings_update(self):
        body = self._read_json_body()
        config = read_config()
        merged = get_settings(config)
        merged.update({k: v for k, v in body.items() if k in DEFAULT_SETTINGS})
        new, err = clean_settings(merged)
        if err:
            self._send_json({"error": err}, 400)
            return

        new_host = bind_host(new)
        port_changed = new["port"] != RUNTIME["port"]
        rebind = port_changed or new_host != RUNTIME["host"]

        if port_changed and not port_available(new_host, new["port"]):
            self._send_json({"error": f"Port {new['port']} is already in use by another program. Try a different number."}, 400)
            return

        config["settings"] = new
        write_config(config)
        self._send_json({"ok": True, "settings": new, "rebind": rebind, "port_changed": port_changed})

        if rebind:
            # Swap listeners after this response has gone out; the main loop
            # picks up RUNTIME["pending"] once the old server stops.
            RUNTIME["pending"] = (new_host, new["port"])
            threading.Timer(0.5, RUNTIME["server"].shutdown).start()

    # ---- branch management (rename/delete cascade to every device using them) ----
    def _handle_branch_rename(self, old_name):
        config = read_config()
        branches = config.setdefault("branches", [])
        if old_name not in branches:
            self._send_json({"error": f"No branch named '{old_name}'."}, 404)
            return

        body = self._read_json_body()
        new_name = (body.get("name") or "").strip()
        if not new_name:
            self._send_json({"error": "Branch name can't be empty."}, 400)
            return
        if new_name != old_name and new_name in branches:
            self._send_json({"error": f"Branch '{new_name}' already exists."}, 400)
            return

        branches[branches.index(old_name)] = new_name
        affected = 0
        for d in config.get("devices", []):
            if (d.get("branch") or DEFAULT_BRANCH) == old_name:
                d["branch"] = new_name
                affected += 1

        write_config(config)

        with STATUS_LOCK:
            for status in CURRENT_STATUS.values():
                if status.get("branch") == old_name:
                    status["branch"] = new_name

        self._send_json({"ok": True, "branch": new_name, "devices_updated": affected})

    def _handle_branch_delete(self, name):
        config = read_config()
        branches = config.setdefault("branches", [])
        if name not in branches:
            self._send_json({"error": f"No branch named '{name}'."}, 404)
            return
        if name == DEFAULT_BRANCH:
            self._send_json({"error": f"'{DEFAULT_BRANCH}' is the fallback branch and can't be deleted."}, 400)
            return

        branches.remove(name)
        if DEFAULT_BRANCH not in branches:
            branches.append(DEFAULT_BRANCH)

        reassigned = 0
        for d in config.get("devices", []):
            if (d.get("branch") or DEFAULT_BRANCH) == name:
                d["branch"] = DEFAULT_BRANCH
                reassigned += 1

        write_config(config)

        with STATUS_LOCK:
            for status in CURRENT_STATUS.values():
                if status.get("branch") == name:
                    status["branch"] = DEFAULT_BRANCH

        self._send_json({"ok": True, "removed": name, "devices_reassigned": reassigned})


def _safe(fn):
    def wrapper(self, *args, **kwargs):
        try:
            return fn(self, *args, **kwargs)
        except ConfigError as e:
            self._send_json({"error": str(e), "config_error": True}, 409)
        except Exception as e:
            traceback.print_exc()
            try:
                self._send_json({"error": f"Something went wrong on the server: {e}"}, 500)
            except Exception:
                pass
    return wrapper


for _m in ("do_GET", "do_POST", "do_PUT", "do_DELETE"):
    setattr(Handler, _m, _safe(getattr(Handler, _m)))


class DashboardServer(ThreadingHTTPServer):
    # On Windows, SO_REUSEADDR lets two programs share a port, which would
    # break the "already running?" check below. Only enable it elsewhere.
    allow_reuse_address = os.name != "nt"
    daemon_threads = True


RUNTIME = {"server": None, "host": None, "port": None, "pending": None}


def port_available(host, port):
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def dashboard_already_running(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/settings", timeout=1.5) as r:
            return "open_mode" in json.loads(r.read().decode("utf-8"))
    except Exception:
        return False


def find_app_browser():
    """Edge or Chrome, so the dashboard can open as its own app-style window
    (no address bar, no tabs). Returns None if neither is found."""
    candidates = []
    if os.name == "nt":
        bases = [os.environ.get(k) for k in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA")]
        for base in filter(None, bases):
            candidates += [
                os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe"),
                os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"),
            ]
    else:
        for name in ("microsoft-edge", "google-chrome", "chromium", "chromium-browser", "chrome"):
            found = shutil.which(name)
            if found:
                candidates.append(found)
    return next((c for c in candidates if os.path.exists(c)), None)


def open_dashboard(port, mode):
    url = f"http://localhost:{port}"
    if mode == "none":
        return
    if mode == "window":
        exe = find_app_browser()
        if exe:
            try:
                subprocess.Popen([exe, f"--app={url}", "--window-size=1320,880"])
                return
            except Exception:
                pass  # fall through to the normal browser
    webbrowser.open(url)


def show_fatal(message):
    print(message)
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, message, "Network Device Dashboard", 0x10)
        except Exception:
            pass


def log_crash():
    """Save the full error next to the exe so it can be sent over / looked at."""
    path = os.path.join(DATA_DIR, "dashboard-error.log")
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"\n=== {now_iso()} ===\n{traceback.format_exc()}")
    except OSError:
        pass
    return path


def serve_until_quit():
    while True:
        srv = RUNTIME["server"]
        srv.serve_forever()
        srv.server_close()
        pending = RUNTIME["pending"]
        if not pending:
            return
        RUNTIME["pending"] = None
        host, port = pending
        try:
            RUNTIME["server"] = DashboardServer((host, port), Handler)
            RUNTIME["host"], RUNTIME["port"] = host, port
        except OSError:
            # Couldn't bind the new address - go back to the previous one
            RUNTIME["server"] = DashboardServer((RUNTIME["host"], RUNTIME["port"]), Handler)


def _main():
    ensure_config_exists()
    init_log_db()

    settings = get_settings()
    host, port = bind_host(settings), settings["port"]

    server = None
    for attempt_port in range(port, port + 21):
        try:
            server = DashboardServer((host, attempt_port), Handler)
            break
        except OSError:
            # Port taken. If it's another copy of this dashboard, just show it and exit.
            if dashboard_already_running(attempt_port):
                open_dashboard(attempt_port, settings["open_mode"])
                sys.exit(0)
    if server is None:
        show_fatal(f"Couldn't start: ports {port}-{port + 20} are all in use by other programs.")
        sys.exit(1)

    if attempt_port != port:
        # Remember the port that actually worked so the address stays stable
        cfg = read_config()
        settings["port"] = attempt_port
        cfg["settings"] = settings
        write_config(cfg)

    RUNTIME.update(server=server, host=host, port=attempt_port)

    threading.Thread(target=monitor_loop, daemon=True).start()

    print(f"Network Device Dashboard running at http://localhost:{attempt_port}")
    print("Press CTRL+C to stop.")
    threading.Timer(1.0, lambda: open_dashboard(attempt_port, settings["open_mode"])).start()

    try:
        serve_until_quit()
    except KeyboardInterrupt:
        print("\nShutting down.")


if __name__ == "__main__":
    try:
        _main()
    except SystemExit:
        raise
    except Exception as e:
        log_path = log_crash()
        show_fatal(f"The dashboard hit an unexpected problem and had to stop.\n\n{type(e).__name__}: {e}\n\n"
                   f"Details were saved to:\n{log_path}")
        sys.exit(1)
