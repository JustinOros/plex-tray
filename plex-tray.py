import asyncio
import importlib
import json
import logging
import os
import platform
import socket
import subprocess
import sys
import threading
import time
import uuid
import webbrowser

APP = "plex-tray"
VERSION = "1.0.0"
INSTANCE_PORT = 47654
PLEX_TV = "https://plex.tv"
REQUIRED = {
    "pystray": "pystray",
    "PIL": "Pillow",
    "requests": "requests",
    "pypresence": "pypresence",
}


def ensure_deps():
    missing = []
    for module, package in REQUIRED.items():
        try:
            importlib.import_module(module)
        except ImportError:
            missing.append(package)
    if not missing:
        return
    cmd = [sys.executable, "-m", "pip", "install", "--quiet"] + missing
    if subprocess.call(cmd) != 0:
        subprocess.check_call(cmd + ["--break-system-packages"])
    importlib.invalidate_caches()


if not getattr(sys, "frozen", False):
    ensure_deps()

import argparse
from pathlib import Path
from urllib.parse import urlencode, urlparse, urlunparse

import pypresence
import pystray
import requests
from PIL import Image, ImageDraw
from pystray import Menu, MenuItem

log = logging.getLogger(APP)

DEFAULT_STATE = {
    "url": "",
    "token": "",
    "client_id": "",
    "discord": False,
    "discord_id": "",
    "hide_title": False,
    "open_on_launch": True,
    "notifications": True,
}


def config_dir():
    override = os.environ.get("PLEX_TRAY_HOME")
    if override:
        path = Path(override)
    elif sys.platform == "win32":
        path = Path(os.environ.get("APPDATA", str(Path.home()))) / APP
    elif sys.platform == "darwin":
        path = Path.home() / "Library" / "Application Support" / APP
    else:
        base = os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
        path = Path(base) / APP
    path.mkdir(parents=True, exist_ok=True)
    return path


def setup_logging():
    handler = logging.FileHandler(config_dir() / f"{APP}.log", encoding="utf-8")
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%m/%d/%Y %I:%M:%S %p")
    )
    log.addHandler(handler)
    log.setLevel(logging.INFO)


class Store:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.data = dict(DEFAULT_STATE)
        if path.exists():
            try:
                self.data.update(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                pass

    def get(self, key):
        return self.data.get(key)

    def set(self, **values):
        with self.lock:
            self.data.update(values)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
            if sys.platform != "win32":
                os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)


class PlexError(Exception):
    def __init__(self, status, message=None):
        super().__init__(message or f"Plex returned {status}")
        self.status = status


def check(response):
    if response.status_code >= 400:
        raise PlexError(response.status_code)
    return response


def fmt_ms(ms):
    seconds = int(ms // 1000)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def label(item):
    kind = item.get("type")
    if kind == "episode":
        show = item.get("grandparentTitle", "")
        code = f"S{item.get('parentIndex', 0)}E{item.get('index', 0)}"
        return f"{show} {code} {item.get('title', '')}".strip()
    if kind == "movie" and item.get("year"):
        return f"{item.get('title', '')} ({item['year']})"
    return item.get("title", "")


def normalize_url(value):
    value = value.strip().rstrip("/")
    if not value:
        return ""
    if "://" not in value:
        value = "http://" + value
    parts = urlparse(value)
    if parts.scheme == "http" and parts.port is None and parts.hostname:
        parts = parts._replace(netloc=f"{parts.hostname}:32400")
    return urlunparse(parts)


class Plex:
    def __init__(self, store):
        self.store = store
        self.http = requests.Session()
        self.command_id = 0
        self.reset()

    def reset(self):
        self.machine_id = ""
        self.names = None
        self.names_at = 0.0
        self.mismatch_logged = False

    @property
    def base(self):
        return (self.store.get("url") or "").rstrip("/")

    def configured(self):
        return bool(self.base)

    def headers(self, extra=None):
        headers = {
            "Accept": "application/json",
            "X-Plex-Client-Identifier": self.store.get("client_id"),
            "X-Plex-Product": APP,
            "X-Plex-Version": VERSION,
            "X-Plex-Device-Name": platform.node() or APP,
            "X-Plex-Platform": platform.system(),
        }
        token = self.store.get("token")
        if token:
            headers["X-Plex-Token"] = token
        if extra:
            headers.update(extra)
        return headers

    def get(self, path, params=None, timeout=6):
        response = check(
            self.http.get(self.base + path, params=params, headers=self.headers(), timeout=timeout)
        )
        try:
            return response.json()
        except ValueError:
            return {}

    def container(self, path, params=None):
        return self.get(path, params).get("MediaContainer", {})

    def machine(self):
        if not self.machine_id:
            self.machine_id = self.container("/identity").get("machineIdentifier", "")
        return self.machine_id

    def server_name(self):
        return self.container("/").get("friendlyName", "Plex")

    def owner_names(self):
        if self.names is not None:
            return self.names
        if not self.store.get("token") or time.time() - self.names_at < 60:
            return set()
        self.names_at = time.time()
        try:
            response = check(
                requests.get(PLEX_TV + "/api/v2/user", headers=self.headers(), timeout=8)
            )
            data = response.json()
        except (requests.RequestException, PlexError, ValueError):
            return set()
        found = {data.get("username"), data.get("title"), data.get("friendlyName")}
        self.names = {n for n in found if n}
        return self.names

    def sessions(self):
        return self.container("/status/sessions").get("Metadata", []) or []

    def clients(self):
        return self.container("/clients").get("Server", []) or []

    def my_session(self):
        items = self.sessions()
        if not items:
            return None
        names = self.owner_names()
        if names:
            seen = [s.get("User", {}).get("title") for s in items]
            items = [s for s in items if s.get("User", {}).get("title") in names]
            if not items and not self.mismatch_logged:
                self.mismatch_logged = True
                log.info("no session matches account names=%s session users=%s", sorted(names), seen)
        elif len(items) != 1:
            if not self.mismatch_logged:
                self.mismatch_logged = True
                log.info("account name unknown and %d sessions active, skipping", len(items))
            return None
        if not items:
            return None
        self.mismatch_logged = False
        web = [s for s in items if "web" in s.get("Player", {}).get("product", "").lower()]
        return (web or items)[0]

    def tv_players(self):
        if not self.store.get("token"):
            return []
        try:
            response = check(
                requests.get(
                    PLEX_TV + "/api/v2/resources",
                    params={"includeHttps": 1, "includeRelay": 1},
                    headers=self.headers(),
                    timeout=8,
                )
            )
            items = response.json()
        except (requests.RequestException, PlexError, ValueError):
            return []
        found = [
            i
            for i in items
            if "player" in (i.get("provides") or "")
            and "web" in (i.get("product") or "").lower()
            and i.get("presence")
        ]
        return sorted(found, key=lambda i: i.get("lastSeenAt") or "", reverse=True)

    def web_target(self, wait=0):
        deadline = time.time() + wait
        while True:
            session = self.my_session()
            if session and "web" in session.get("Player", {}).get("product", "").lower():
                return session["Player"]["machineIdentifier"]
            for client in self.clients():
                if "web" in (client.get("product", "") + " " + client.get("name", "")).lower():
                    return client.get("machineIdentifier")
            players = self.tv_players()
            if players:
                return players[0].get("clientIdentifier")
            if time.time() >= deadline:
                return None
            time.sleep(2)

    def command(self, path, params, target):
        self.command_id += 1
        query = {"commandID": self.command_id, "type": "video"}
        query.update(params or {})
        headers = self.headers({"X-Plex-Target-Client-Identifier": target})
        response = self.http.get(self.base + path, params=query, headers=headers, timeout=8)
        if response.status_code == 404:
            try:
                known = [c.get("machineIdentifier") for c in self.clients()]
            except Exception:
                known = []
            log.warning("player not found: command=%s target=%s clients=%s", path, target, known)
            raise PlexError(404, "Plex cannot find the browser player. Keep a Plex Web tab open with Advertise as a player turned on.")
        check(response)

    def create_queue(self, rating_key, kind):
        uri = f"server://{self.machine()}/com.plexapp.plugins.library/library/metadata/{rating_key}"
        params = {
            "type": "video",
            "uri": uri,
            "repeat": 0,
            "own": 1,
            "continuous": 1 if kind == "episode" else 0,
        }
        response = check(
            self.http.post(self.base + "/playQueues", params=params, headers=self.headers(), timeout=8)
        )
        return response.json()["MediaContainer"]["playQueueID"]

    def web_url(self):
        return self.base + "/web/index.html"

    def details_url(self, rating_key):
        return (
            f"{self.web_url()}#!/server/{self.machine()}/details"
            f"?key=%2Flibrary%2Fmetadata%2F{rating_key}"
        )

    def resolve(self, item):
        kind = item.get("type")
        key = item.get("ratingKey")
        if kind in ("movie", "episode"):
            return key, item.get("viewOffset", 0), kind
        if kind == "show":
            deck = self.container(f"/library/metadata/{key}/onDeck").get("Metadata", [])
            if deck:
                return deck[0]["ratingKey"], deck[0].get("viewOffset", 0), "episode"
            leaves = self.container(f"/library/metadata/{key}/allLeaves").get("Metadata", [])
            if leaves:
                return leaves[0]["ratingKey"], 0, "episode"
        raise PlexError(404, "Nothing playable found for that item")

    def play_params(self, rating_key, offset, queue):
        parts = urlparse(self.base)
        params = {
            "providerIdentifier": "com.plexapp.plugins.library",
            "machineIdentifier": self.machine(),
            "protocol": parts.scheme,
            "address": parts.hostname,
            "port": parts.port or (443 if parts.scheme == "https" else 80),
            "offset": int(offset),
            "key": f"/library/metadata/{rating_key}",
            "containerKey": f"/playQueues/{queue}?window=200&own=1",
        }
        token = self.store.get("token")
        if token:
            params["token"] = token
        return params

    def session_for(self, rating_key, wait):
        deadline = time.time() + wait
        while True:
            session = self.my_session()
            if session and str(session.get("ratingKey")) == str(rating_key):
                return True
            if time.time() >= deadline:
                return False
            time.sleep(1)

    def try_play(self, params, target, rating_key, wait):
        try:
            self.command("/player/playback/playMedia", params, target)
        except PlexError as exc:
            if exc.status != 404:
                raise
            return False
        return self.session_for(rating_key, wait)

    def play(self, rating_key, offset, kind, progress=None, launch_wait=40):
        queue = self.create_queue(rating_key, kind)
        params = self.play_params(rating_key, offset, queue)
        target = self.web_target()
        if target and self.try_play(params, target, rating_key, 8):
            return True
        if progress:
            progress("Opening Plex Web in your browser")
        webbrowser.open(self.web_url())
        deadline = time.time() + launch_wait
        while time.time() < deadline:
            time.sleep(3)
            target = self.web_target()
            if target and self.try_play(params, target, rating_key, 6):
                return True
        webbrowser.open(self.details_url(rating_key))
        return False

    def continue_watching(self):
        found = []
        seen = set()
        error = None
        for path in ("/hubs/continueWatching/items", "/library/onDeck"):
            try:
                items = self.container(path, {"X-Plex-Container-Size": 15}).get("Metadata", [])
            except PlexError as exc:
                error = exc
                continue
            for item in items:
                key = item.get("ratingKey")
                if key not in seen and item.get("type") in ("movie", "episode"):
                    seen.add(key)
                    found.append(item)
        if not found and error:
            raise error
        return found[:20]

    def sections(self):
        return self.container("/library/sections").get("Directory", [])

    def recent(self, section_key):
        params = {"sort": "addedAt:desc", "X-Plex-Container-Start": 0, "X-Plex-Container-Size": 15}
        return self.container(f"/library/sections/{section_key}/all", params).get("Metadata", [])

    def search(self, query):
        hubs = self.container("/hubs/search", {"query": query, "limit": 10}).get("Hub", [])
        found = []
        for hub in hubs:
            if hub.get("type") in ("movie", "show", "episode"):
                found.extend(hub.get("Metadata", []))
        return found[:20]


def build_payload(store, session, position_ms):
    hide = store.get("hide_title")
    kind = session.get("type")
    paused = session.get("Player", {}).get("state") == "paused"
    if kind == "episode":
        details = session.get("grandparentTitle") or "Episode"
        code = f"S{session.get('parentIndex', 0)}E{session.get('index', 0)}"
        state = f"{code} {session.get('title', '')}".strip()
        generic = "Watching a show"
    else:
        details = session.get("title") or "Movie"
        state = str(session["year"]) if session.get("year") else "Movie"
        generic = "Watching a movie"
    if hide:
        details, state = generic, "Plex"
    if paused:
        state = f"Paused - {state}"
    payload = {
        "details": details[:128].ljust(2),
        "state": state[:128].ljust(2),
        "large_image": "plex",
        "large_text": "Plex",
    }
    if not paused:
        start = time.time() - position_ms / 1000.0
        payload["start"] = int(start)
        duration = session.get("duration", 0)
        if duration:
            payload["end"] = int(start + duration / 1000.0)
    return payload


class Presence:
    def __init__(self, store, loop):
        self.store = store
        self.loop = loop
        self.rpc = None
        self.client = ""
        self.last_try = 0.0
        self.sig = None
        self.start = 0

    def connect(self):
        client = self.store.get("discord_id")
        if self.rpc and self.client == client:
            return True
        self.close()
        if time.time() - self.last_try < 15:
            return False
        self.last_try = time.time()
        try:
            rpc = pypresence.Presence(client, loop=self.loop)
            rpc.connect()
        except Exception as exc:
            log.info("discord not available: %s", exc)
            return False
        self.rpc, self.client = rpc, client
        return True

    def close(self):
        if self.rpc:
            try:
                self.rpc.close()
            except Exception:
                pass
        self.rpc = None
        self.sig = None
        self.start = 0

    def clear(self):
        if self.rpc and self.sig is not None:
            try:
                self.rpc.clear()
            except Exception:
                self.close()
        self.sig = None
        self.start = 0

    def update(self, session, position_ms):
        if not (self.store.get("discord") and self.store.get("discord_id")):
            self.close()
            return
        if not session:
            self.clear()
            return
        payload = build_payload(self.store, session, position_ms)
        sig = (payload["details"], payload["state"], "start" in payload)
        start = payload.get("start", 0)
        if sig == self.sig and abs(start - self.start) < 6:
            return
        if not self.connect():
            return
        try:
            kind = getattr(pypresence, "ActivityType", None)
            try:
                if kind is not None:
                    self.rpc.update(activity_type=kind.WATCHING, **payload)
                else:
                    self.rpc.update(**payload)
            except TypeError:
                self.rpc.update(**payload)
            self.sig, self.start = sig, start
            log.info("discord presence set: %s | %s", payload["details"], payload["state"])
        except Exception as exc:
            log.info("discord update failed: %s", exc)
            self.close()


def make_icon(state):
    size = 64
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    color = (229, 160, 13, 255) if state != "idle" else (150, 150, 150, 255)
    draw.rounded_rectangle((2, 2, size - 3, size - 3), radius=14, fill=(31, 31, 31, 255))
    if state == "paused":
        draw.rectangle((20, 16, 29, 48), fill=color)
        draw.rectangle((35, 16, 44, 48), fill=color)
    else:
        draw.polygon([(22, 16), (22, 48), (48, 32)], fill=color)
    return image


def ask(title, prompt, default="", secret=False):
    if sys.platform == "darwin":
        script = (
            f"display dialog {json.dumps(prompt, ensure_ascii=False)} "
            f"with title {json.dumps(title, ensure_ascii=False)} "
            f"default answer {json.dumps(default, ensure_ascii=False)}"
        )
        if secret:
            script += " with hidden answer"
        result = subprocess.run(
            ["osascript", "-e", script, "-e", "text returned of result"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip()
    return tk_dialog(title, prompt, default, secret)


def is_dark():
    try:
        if sys.platform == "darwin":
            result = subprocess.run(
                ["defaults", "read", "-g", "AppleInterfaceStyle"], capture_output=True, text=True
            )
            return result.stdout.strip().lower() == "dark"
        if sys.platform == "win32":
            import winreg

            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
            )
            return winreg.QueryValueEx(key, "AppsUseLightTheme")[0] == 0
        for args in (
            ["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"],
            ["gsettings", "get", "org.gnome.desktop.interface", "gtk-theme"],
        ):
            result = subprocess.run(args, capture_output=True, text=True)
            if "dark" in result.stdout.lower():
                return True
        return "dark" in os.environ.get("GTK_THEME", "").lower()
    except Exception:
        return False


def enable_dark_menus():
    if sys.platform != "win32":
        return
    try:
        import ctypes

        uxtheme = ctypes.windll.uxtheme
        set_mode = uxtheme[135]
        set_mode.argtypes = [ctypes.c_int]
        set_mode(1)
        uxtheme[136]()
    except Exception:
        pass


def tk_dialog(title, prompt, default, secret):
    import tkinter as tk

    if is_dark():
        bg, fg, field, button = "#202020", "#f0f0f0", "#2d2d2d", "#3a3a3a"
    else:
        bg, fg, field, button = "#f5f5f5", "#111111", "#ffffff", "#e1e1e1"
    root = tk.Tk()
    root.title(title)
    root.configure(bg=bg)
    root.resizable(False, False)
    root.attributes("-topmost", True)
    result = {"value": None}
    tk.Label(root, text=prompt, bg=bg, fg=fg, justify="left", wraplength=380).pack(
        padx=16, pady=(16, 8), anchor="w"
    )
    var = tk.StringVar(value=default)
    entry = tk.Entry(
        root,
        textvariable=var,
        width=46,
        show="*" if secret else "",
        bg=field,
        fg=fg,
        insertbackground=fg,
        relief="flat",
        highlightthickness=1,
        highlightbackground=button,
        highlightcolor="#e5a00d",
    )
    entry.pack(padx=16, pady=4, ipady=4)

    def accept(event=None):
        result["value"] = var.get()
        root.destroy()

    def cancel(event=None):
        root.destroy()

    row = tk.Frame(root, bg=bg)
    row.pack(padx=16, pady=(8, 16), anchor="e")
    for text, command in (("Cancel", cancel), ("OK", accept)):
        tk.Button(
            row,
            text=text,
            command=command,
            bg=button,
            fg=fg,
            activebackground=field,
            activeforeground=fg,
            relief="flat",
            bd=0,
            highlightthickness=0,
            width=9,
        ).pack(side="left", padx=4)
    root.bind("<Return>", accept)
    root.bind("<Escape>", cancel)
    if sys.platform == "win32" and is_dark():
        try:
            import ctypes

            root.update()
            hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, 20, ctypes.byref(ctypes.c_int(1)), 4
            )
        except Exception:
            pass
    root.update_idletasks()
    x = (root.winfo_screenwidth() - root.winfo_width()) // 2
    y = (root.winfo_screenheight() - root.winfo_height()) // 3
    root.geometry(f"+{x}+{y}")
    entry.focus_force()
    entry.select_range(0, "end")
    root.mainloop()
    return result["value"]


class App:
    def __init__(self):
        self.store = Store(config_dir() / "state.json")
        if not self.store.get("client_id"):
            self.store.set(client_id=str(uuid.uuid4()))
        self.plex = Plex(self.store)
        self.quit = threading.Event()
        self.poll_now = threading.Event()
        self.cache_evt = threading.Event()
        self.dialog_lock = threading.Lock()
        self.icon = None
        self.poll_thread = None
        self.session = None
        self.fetched_at = 0.0
        self.last_sig = None
        self.last_cache_error = None
        self.continue_items = []
        self.libraries = []
        self.results = []
        self.images = {s: make_icon(s) for s in ("idle", "playing", "paused")}

    def notify(self, message, force=False):
        if not self.icon or not (force or self.store.get("notifications")):
            return
        try:
            self.icon.notify(message, APP)
        except Exception:
            pass

    def refresh_menu(self):
        if self.icon:
            try:
                self.icon.update_menu()
            except Exception:
                pass

    def guard(self, fn, *args):
        try:
            fn(*args)
        except PlexError as exc:
            log.warning("%s", exc)
            if exc.status == 401:
                self.notify("Plex needs a sign in. Use Settings, Sign In With Plex.", True)
            else:
                self.notify(str(exc), True)
        except requests.RequestException as exc:
            log.warning("request failed: %s", exc.__class__.__name__)
            self.notify("Could not reach the Plex server", True)
        except Exception as exc:
            log.exception("action failed")
            self.notify(f"Error: {exc}", True)

    def bg(self, fn, *args):
        def inner(icon, item):
            threading.Thread(target=self.guard, args=(fn,) + args, daemon=True).start()

        return inner

    def ask(self, title, prompt, default="", secret=False):
        with self.dialog_lock:
            return ask(title, prompt, default, secret)

    def position(self):
        session = self.session
        if not session:
            return 0
        position = session.get("viewOffset", 0)
        if session.get("Player", {}).get("state") == "playing":
            position += (time.time() - self.fetched_at) * 1000
        duration = session.get("duration", 0)
        return min(position, duration) if duration else position

    def is_playing(self):
        session = self.session
        return bool(session) and session.get("Player", {}).get("state") == "playing"

    def now_text(self):
        session = self.session
        if not session:
            if not self.plex.configured():
                return "Set a Plex URL in Settings"
            return "Nothing playing"
        prefix = "Paused: " if session.get("Player", {}).get("state") == "paused" else ""
        text = f"{prefix}{label(session)}"
        duration = session.get("duration", 0)
        if duration:
            text += f" [{fmt_ms(self.position())} / {fmt_ms(duration)}]"
        return text[:90]

    def menu_text(self, text):
        text = text[:70]
        return text.replace("&", "&&") if sys.platform == "win32" else text

    def entries(self, items):
        if not items:
            return [MenuItem("Nothing found", None, enabled=False)]
        return [MenuItem(self.menu_text(label(i)), self.bg(self.play_item, i)) for i in items]

    def flag_item(self, text, key):
        return MenuItem(
            text, self.bg(self.toggle_flag, key), checked=lambda item: bool(self.store.get(key))
        )

    def menu_items(self):
        yield MenuItem(lambda item: self.now_text(), None, enabled=False)
        yield Menu.SEPARATOR
        yield MenuItem(
            lambda item: "Pause" if self.is_playing() else "Play",
            self.bg(self.toggle),
            default=True,
        )
        yield MenuItem("Previous", self.bg(self.transport, "skipPrevious"))
        yield MenuItem("Next", self.bg(self.transport, "skipNext"))
        yield MenuItem("Back 10 Seconds", self.bg(self.seek, -10))
        yield MenuItem("Forward 30 Seconds", self.bg(self.seek, 30))
        yield MenuItem("Stop", self.bg(self.transport, "stop"))
        yield Menu.SEPARATOR
        yield MenuItem("Search...", self.bg(self.search))
        if self.results:
            yield MenuItem("Search Results", Menu(*self.entries(self.results)))
        yield MenuItem("Continue Watching", Menu(*self.entries(self.continue_items)))
        for title, items in self.libraries:
            yield MenuItem(self.menu_text(f"{title}: Recently Added"), Menu(*self.entries(items)))
        yield Menu.SEPARATOR
        yield MenuItem(
            "Discord Presence",
            Menu(
                self.flag_item("Show What I'm Watching", "discord"),
                self.flag_item("Hide Title", "hide_title"),
                MenuItem("Application ID...", self.bg(self.set_discord_id)),
            ),
        )
        yield MenuItem(
            "Settings",
            Menu(
                MenuItem("Plex URL...", self.bg(self.set_url)),
                MenuItem("Sign In With Plex...", self.bg(self.sign_in)),
                MenuItem("Enter Token...", self.bg(self.set_token)),
                Menu.SEPARATOR,
                self.flag_item("Open Plex On Launch", "open_on_launch"),
                self.flag_item("Notifications", "notifications"),
            ),
        )
        yield MenuItem("Open Plex Web", self.bg(self.open_web))
        yield MenuItem("Quit", self.on_quit)

    def target(self):
        session = self.session
        if not session:
            return None
        return session.get("Player", {}).get("machineIdentifier")

    def transport(self, command):
        target = self.target()
        if not target:
            self.notify("Nothing is playing", True)
            return
        self.plex.command(f"/player/playback/{command}", {}, target)
        self.poll_now.set()

    def toggle(self):
        session = self.session
        if not session:
            self.notify("Nothing is playing", True)
            return
        paused = session.get("Player", {}).get("state") == "paused"
        self.transport("play" if paused else "pause")

    def seek(self, seconds):
        target = self.target()
        if not target:
            self.notify("Nothing is playing", True)
            return
        offset = max(0, int(self.position() + seconds * 1000))
        self.plex.command("/player/playback/seekTo", {"offset": offset}, target)
        self.poll_now.set()

    def play_item(self, item):
        key, offset, kind = self.plex.resolve(item)
        self.notify(f"Starting {label(item)}")
        if self.plex.play(key, offset, kind, self.notify):
            self.poll_now.set()
        else:
            self.notify(
                "Opened in your browser. Turn on Advertise as a player in Plex Web settings "
                "so this app can control playback.",
                True,
            )

    def search(self):
        query = self.ask("Search Plex", "Movie or show title")
        if not query:
            return
        self.results = self.plex.search(query.strip())
        self.refresh_menu()
        if self.results:
            self.notify(f"{len(self.results)} results are under Search Results", True)
        else:
            self.notify("No results", True)

    def toggle_flag(self, key):
        value = not self.store.get(key)
        self.store.set(**{key: value})
        if key == "discord" and value and not self.store.get("discord_id"):
            self.set_discord_id()
        self.refresh_menu()

    def set_discord_id(self):
        value = self.ask(
            "Discord Application ID",
            "Application ID from discord.com/developers/applications",
            self.store.get("discord_id") or "",
        )
        if value is None:
            return
        self.store.set(discord_id=value.strip())

    def set_token(self):
        value = self.ask("Plex Token", "X-Plex-Token", "", secret=True)
        if value is None:
            return
        self.store.set(token=value.strip())
        self.plex.reset()
        self.cache_evt.set()
        self.verify()

    def set_url(self):
        value = self.ask(
            "Plex URL",
            "Plex server URL, for example http://192.168.1.10:32400",
            self.store.get("url") or "",
        )
        if value is None:
            return
        self.store.set(url=normalize_url(value))
        self.plex.reset()
        self.cache_evt.set()
        self.verify()

    def verify(self):
        if not self.plex.configured():
            return
        self.notify(f"Connected to {self.plex.server_name()}", True)

    def sign_in(self):
        client = self.store.get("client_id")
        headers = {
            "Accept": "application/json",
            "X-Plex-Client-Identifier": client,
            "X-Plex-Product": APP,
        }
        response = check(
            requests.post(PLEX_TV + "/api/v2/pins", params={"strong": "true"}, headers=headers, timeout=10)
        )
        pin = response.json()
        query = urlencode(
            {"clientID": client, "code": pin["code"], "context[device][product]": APP}
        )
        webbrowser.open("https://app.plex.tv/auth#?" + query)
        self.notify("Approve the sign in in your browser", True)
        deadline = time.time() + 180
        while time.time() < deadline and not self.quit.is_set():
            time.sleep(2)
            poll = check(
                requests.get(
                    f"{PLEX_TV}/api/v2/pins/{pin['id']}",
                    params={"code": pin["code"]},
                    headers=headers,
                    timeout=10,
                )
            )
            token = poll.json().get("authToken")
            if token:
                self.store.set(token=token)
                self.plex.reset()
                self.cache_evt.set()
                self.notify("Signed in to Plex", True)
                return
        self.notify("Sign in timed out", True)

    def open_web(self):
        if not self.plex.configured():
            self.set_url()
        if self.plex.configured():
            webbrowser.open(self.plex.web_url())

    def apply(self, session):
        self.session = session
        self.fetched_at = time.time()
        sig = None
        state = "idle"
        if session:
            state = session.get("Player", {}).get("state", "playing")
            state = "paused" if state == "paused" else "playing"
            sig = (session.get("ratingKey"), state)
        if sig == self.last_sig:
            return
        self.last_sig = sig
        if self.icon:
            self.icon.icon = self.images[state]
            self.icon.title = self.now_text()[:120] if session else APP
        self.refresh_menu()

    def poll(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        presence = Presence(self.store, loop)
        last_error = None
        try:
            while not self.quit.is_set():
                session = None
                if self.plex.configured():
                    try:
                        session = self.plex.my_session()
                        last_error = None
                    except Exception as exc:
                        message = str(exc) or exc.__class__.__name__
                        if message != last_error:
                            log.warning("poll failed: %s", message)
                            last_error = message
                self.apply(session)
                try:
                    presence.update(session, self.position())
                except Exception:
                    log.exception("presence failed")
                if self.poll_now.wait(2):
                    time.sleep(0.8)
                    self.poll_now.clear()
        finally:
            presence.close()

    def refresh_cache(self):
        if not self.plex.configured():
            return
        continue_items = self.plex.continue_watching()
        libraries = []
        for section in self.plex.sections():
            if section.get("type") in ("movie", "show"):
                libraries.append((section.get("title", ""), self.plex.recent(section["key"])))
        self.continue_items, self.libraries = continue_items, libraries
        self.refresh_menu()

    def cache_loop(self):
        while not self.quit.is_set():
            try:
                self.refresh_cache()
            except Exception as exc:
                message = str(exc) or exc.__class__.__name__
                log.warning("library refresh failed: %s", message)
                if message != self.last_cache_error:
                    self.last_cache_error = message
                    if isinstance(exc, PlexError) and exc.status == 401:
                        self.notify("Plex needs a sign in. Use Settings, Sign In With Plex.", True)
                    else:
                        self.notify(f"Library refresh failed: {message}", True)
            else:
                self.last_cache_error = None
            self.cache_evt.wait(60)
            self.cache_evt.clear()

    def setup(self, icon):
        icon.visible = True
        self.poll_thread = threading.Thread(target=self.poll, daemon=True)
        self.poll_thread.start()
        threading.Thread(target=self.cache_loop, daemon=True).start()
        if not self.plex.configured():
            self.guard(self.set_url)
        if self.plex.configured() and self.store.get("open_on_launch"):
            webbrowser.open(self.plex.web_url())

    def on_quit(self, icon, item):
        self.shutdown()

    def shutdown(self):
        self.quit.set()
        self.poll_now.set()
        self.cache_evt.set()
        if self.icon:
            self.icon.stop()

    def run(self):
        enable_dark_menus()
        self.icon =pystray.Icon(APP, self.images["idle"], APP, Menu(self.menu_items))
        self.icon.run(self.setup)
        if self.poll_thread:
            self.poll_thread.join(3)


def start_instance_server(on_quit):
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        server.bind(("127.0.0.1", INSTANCE_PORT))
    except OSError:
        server.close()
        return None
    server.listen(4)

    def loop():
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            with conn:
                try:
                    data = conn.recv(16)
                except OSError:
                    continue
            if data.strip() == b"quit":
                on_quit()

    threading.Thread(target=loop, daemon=True).start()
    return server


def send_quit():
    try:
        with socket.create_connection(("127.0.0.1", INSTANCE_PORT), timeout=2) as conn:
            conn.sendall(b"quit")
        return True
    except OSError:
        return False


def main():
    parser = argparse.ArgumentParser(prog=APP)
    parser.add_argument("--stop", action="store_true", help="stop the running instance")
    parser.add_argument("--restart", action="store_true", help="restart the running instance")
    args = parser.parse_args()
    if args.stop or args.restart:
        stopped = send_quit()
        if args.stop:
            print("plex-tray stopped" if stopped else "plex-tray is not running")
            return 0
        time.sleep(1.5)
    setup_logging()
    app = App()
    server = start_instance_server(app.shutdown)
    if server is None:
        print("plex-tray is already running")
        return 1
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
