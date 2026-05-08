#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# VMShell — Gestionnaire de connexions distantes (RDP / SSH).
# Copyright (C) 2026  Damien
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""
VMShell — Gestionnaire de connexions distantes (RDP / SSH).
Single-window GTK3 dark UI, fullscreen.
"""

import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

import gi
# ---------------------------------------------------------------------------
# Imports critiques GTK / VTE / GdkX11 — si absents, on tente d'installer
# automatiquement puis on échoue proprement avec un message clair.
# ---------------------------------------------------------------------------
def _bootstrap_gi():
    try:
        gi.require_version("Gtk", "3.0")
        gi.require_version("Gdk", "3.0")
        gi.require_version("Vte", "2.91")
        gi.require_version("GdkX11", "3.0")
        return True
    except (ValueError, ImportError):
        return False

if not _bootstrap_gi():
    print("[vmshell] Composants GTK/VTE absents — tentative d'installation…",
          flush=True)
    pm = None
    for cmd in ("apt-get", "dnf", "pacman"):
        if shutil.which(cmd):
            pm = cmd
            break
    if pm:
        sudo = ["sudo", "-n"] if os.geteuid() != 0 and shutil.which("sudo") else []
        if pm == "apt-get":
            args = sudo + ["apt-get", "install", "-y", "--no-install-recommends",
                           "python3-gi", "gir1.2-gtk-3.0", "gir1.2-vte-2.91",
                           "gir1.2-gdkx11-3.0"]
        elif pm == "dnf":
            args = sudo + ["dnf", "install", "-y",
                           "python3-gobject", "gtk3", "vte291"]
        else:
            args = sudo + ["pacman", "-S", "--noconfirm", "--needed",
                           "python-gobject", "gtk3", "vte3"]
        try:
            subprocess.run(args, check=False, timeout=300)
        except Exception:
            pass
        if not _bootstrap_gi():
            sys.stderr.write(
                "[vmshell] ERREUR : GTK 3 / VTE 2.91 / GdkX11 indisponibles.\n"
                "Lancez : sudo " + " ".join(args[1 if sudo else 0:]) + "\n")
            sys.exit(2)
    else:
        sys.stderr.write(
            "[vmshell] ERREUR : composants GTK manquants et aucun "
            "gestionnaire de paquets connu (apt/dnf/pacman).\n")
        sys.exit(2)

from gi.repository import GLib, Gdk, GdkX11, Gtk, Pango, Vte  # noqa: E402

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
APP_DIR     = Path(__file__).resolve().parent
CSS_FILE    = APP_DIR / "vmshell.css"
CONFIG_DIR  = Path(os.path.expanduser("~/.config/vmshell"))
CONNS_FILE  = CONFIG_DIR / "connections.json"
SETTINGS_FILE = CONFIG_DIR / "settings.json"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def ensure_config_dir():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(CONFIG_DIR, 0o700)
    except OSError:
        pass

def load_json(path: Path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except (OSError, json.JSONDecodeError) as e:
        # Fichier corrompu : on l'archive avec un suffixe horodaté pour ne
        # rien perdre, puis on repart sur le défaut. Évite tout crash au
        # démarrage si l'utilisateur a édité manuellement et cassé le JSON.
        try:
            backup = path.with_suffix(path.suffix +
                                      f".broken-{int(time.time())}")
            os.rename(path, backup)
            print(f"[vmshell] {path.name} corrompu ({e}) → "
                  f"sauvegardé en {backup.name}", flush=True)
        except OSError:
            pass
        return default

def save_json_atomic(path: Path, data):
    ensure_config_dir()
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)

def proto_pill(proto: str):
    return ("RDP", "pill-rdp") if proto == "rdp" else ("SSH", "pill-ssh")

def os_glyph(os_name: str):
    return {
        "windows": "🪟",   # fenêtre
        "linux":   "🐧",   # pingouin
        "macos":   "🍎",   # pomme
    }.get((os_name or "").lower(), "💻")

SESSION_LOG = None  # set after CONFIG_DIR exists

def log_session(event: str, conn: dict, extra: str = ""):
    """Append a session event to ~/.config/vmshell/sessions.log"""
    global SESSION_LOG
    if SESSION_LOG is None:
        SESSION_LOG = CONFIG_DIR / "sessions.log"
    try:
        line = f"{datetime.now().isoformat(timespec='seconds')}\t{event}\t{conn.get('name','?')}\t{conn.get('protocol','?')}\t{conn.get('host','?')}:{conn.get('port','?')}\t{extra}\n"
        with open(SESSION_LOG, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass

def proto_initial(proto: str):
    return "R" if proto == "rdp" else "S"

def greeting_for_hour(h):
    if 5  <= h < 12: return "Bonjour"
    if 12 <= h < 18: return "Bon après-midi"
    if 18 <= h < 23: return "Bonsoir"
    return "Bonne nuit"

def find_xfreerdp():
    for cmd in ("xfreerdp3", "xfreerdp"):
        p = shutil.which(cmd)
        if p:
            return p
    return None

# ---------------------------------------------------------------------------
# Background app suspend/resume (SIGSTOP/SIGCONT) during RDP sessions.
# Only well-known heavy GUI apps are touched. Never touch system bits.
# ---------------------------------------------------------------------------
import signal as _signal

_FROZEN_PIDS = set()

# Process *comm* names safe to freeze. Anything not in here is left alone.
_FREEZE_WHITELIST = {
    "code", "code-insiders",
    "firefox", "firefox-esr",
    "chrome", "chromium", "brave", "vivaldi", "opera",
    "thunderbird",
    "discord", "Discord",
    "slack", "Slack",
    "telegram-desktop", "Telegram",
    "spotify",
    "vlc", "mpv", "smplayer",
    "obs",
    "steam", "steamwebhelper",
    "java", "idea", "pycharm", "phpstorm", "webstorm",
    "blender", "gimp", "inkscape",
    "libreoffice",
}

def _own_pid_set():
    """PIDs we must never freeze: ourselves, our parent, our children."""
    keep = {os.getpid()}
    try: keep.add(os.getppid())
    except Exception: pass
    return keep

def freeze_background_apps():
    """SIGSTOP whitelisted user apps to free CPU during RDP."""
    keep = _own_pid_set()
    try:
        out = subprocess.check_output(
            ["ps", "-eo", "pid,comm"], stderr=subprocess.DEVNULL,
            timeout=2).decode().splitlines()[1:]
    except Exception:
        return
    frozen = []
    for line in out:
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        try: pid = int(parts[0])
        except ValueError: continue
        if pid in keep or pid in _FROZEN_PIDS:
            continue
        comm = parts[1].strip()
        if comm not in _FREEZE_WHITELIST:
            continue
        try:
            os.kill(pid, _signal.SIGSTOP)
            _FROZEN_PIDS.add(pid)
            frozen.append(f"{comm}({pid})")
        except OSError:
            pass
    if frozen:
        print(f"[vmshell] freeze: {', '.join(frozen[:8])}"
              + (f" +{len(frozen)-8}…" if len(frozen) > 8 else ""),
              flush=True)

def thaw_background_apps():
    """SIGCONT all previously frozen apps."""
    if not _FROZEN_PIDS:
        return
    for pid in list(_FROZEN_PIDS):
        try:
            os.kill(pid, _signal.SIGCONT)
        except OSError:
            pass
    print(f"[vmshell] thaw: {len(_FROZEN_PIDS)} processus", flush=True)
    _FROZEN_PIDS.clear()

# ---------------------------------------------------------------------------
# Global Escape grabber
# ---------------------------------------------------------------------------
class EscapeGrabber:
    """Grabs the Escape key globally via Xlib (works even when an embedded
    X client like xfreerdp owns the keyboard focus)."""

    def __init__(self, on_escape):
        self._on_escape = on_escape
        self._thread = None
        self._stop = False
        self._dpy = None
        self._keycode = None

    def start(self):
        if self._thread is not None:
            return
        try:
            from Xlib import display, X
            from Xlib import XK
        except Exception as e:
            print(f"[vmshell] Xlib indisponible: {e}", flush=True)
            return
        try:
            self._dpy = display.Display()
            root = self._dpy.screen().root
            self._keycode = self._dpy.keysym_to_keycode(XK.XK_Escape)
            self._do_grab()
            root.change_attributes(event_mask=X.KeyPressMask)
            self._dpy.sync()
        except Exception as e:
            print(f"[vmshell] grab Escape échoué: {e}", flush=True)
            self._dpy = None
            return
        print("[vmshell] grab Escape global actif", flush=True)
        self._stop = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _do_grab(self):
        from Xlib import X
        root = self._dpy.screen().root
        for mods in (0, X.Mod2Mask, X.LockMask, X.Mod2Mask | X.LockMask):
            try:
                root.grab_key(self._keycode, mods, 1,
                              X.GrabModeAsync, X.GrabModeAsync)
            except Exception:
                pass

    def regrab(self):
        if self._dpy is None or self._keycode is None:
            return
        try:
            self._do_grab()
            self._dpy.sync()
        except Exception:
            pass

    def _run(self):
        from Xlib import X
        while not self._stop:
            try:
                if self._dpy.pending_events() == 0:
                    time.sleep(0.05)
                    continue
                ev = self._dpy.next_event()
                if ev.type == X.KeyPress:
                    print("[vmshell] Escape global capté", flush=True)
                    GLib.idle_add(self._on_escape)
            except Exception:
                time.sleep(0.1)

    def stop(self):
        self._stop = True

# ---------------------------------------------------------------------------
# Connection model
# ---------------------------------------------------------------------------
def new_conn(name="", protocol="rdp", host="", port=None, user="",
             password="", os_name="windows", group="", favorite=False):
    return {
        "id":        uuid.uuid4().hex,
        "name":      name,
        "protocol":  protocol,         # "rdp" | "ssh"
        "host":      host,
        "port":      int(port) if port else (3389 if protocol == "rdp" else 22),
        "user":      user,
        "password":  password,
        "os":        os_name,
        "group":     group,
        "favorite":  bool(favorite),
        "maintenance": False,
    }

# ---------------------------------------------------------------------------
# Connection dialog
# ---------------------------------------------------------------------------
class ConnectionDialog(Gtk.Dialog):
    def __init__(self, parent, conn=None):
        title = "Modifier la connexion" if conn else "Nouvelle connexion"
        super().__init__(title=title, transient_for=parent, modal=True)
        self.set_default_size(460, 540)
        self.set_decorated(True)
        self.set_position(Gtk.WindowPosition.CENTER_ON_PARENT)
        self.set_type_hint(Gdk.WindowTypeHint.DIALOG)
        self.set_keep_above(True)
        box = self.get_content_area()
        box.set_border_width(16)
        box.set_spacing(10)

        self._conn = conn or new_conn()

        def lbl(text):
            l = Gtk.Label(label=text, xalign=0)
            l.get_style_context().add_class("form-label")
            return l

        self.e_name = Gtk.Entry()
        self.e_name.set_text(self._conn["name"])
        self.e_name.set_placeholder_text("Ex. Serveur principal")

        self.cb_proto = Gtk.ComboBoxText()
        self.cb_proto.append("rdp", "RDP — Bureau à distance")
        self.cb_proto.append("ssh", "SSH — Terminal")
        self.cb_proto.set_active_id(self._conn["protocol"])

        self.cb_os = Gtk.ComboBoxText()
        for k, v in (("windows", "Windows"), ("linux", "Linux"), ("macos", "macOS")):
            self.cb_os.append(k, v)
        self.cb_os.set_active_id(self._conn.get("os") or "windows")

        self.e_host = Gtk.Entry()
        self.e_host.set_text(self._conn["host"])
        self.e_host.set_placeholder_text("hôte ou IP")

        self.s_port = Gtk.SpinButton.new_with_range(1, 65535, 1)
        self.s_port.set_value(int(self._conn["port"]))

        self.e_user = Gtk.Entry()
        self.e_user.set_text(self._conn["user"])
        self.e_user.set_placeholder_text("utilisateur")

        self.e_pwd = Gtk.Entry()
        self.e_pwd.set_text(self._conn.get("password", ""))
        self.e_pwd.set_visibility(False)
        self.e_pwd.set_placeholder_text("mot de passe (optionnel)")

        self.e_group = Gtk.Entry()
        self.e_group.set_text(self._conn.get("group", ""))
        self.e_group.set_placeholder_text("Étiquette / catégorie")

        self.cb_fav = Gtk.CheckButton(label="Marquer comme favori")
        self.cb_fav.set_active(self._conn.get("favorite", False))

        self.cb_maint = Gtk.CheckButton(label="Maintenance (masquer le statut en ligne)")
        self.cb_maint.set_active(self._conn.get("maintenance", False))

        grid = Gtk.Grid(row_spacing=8, column_spacing=10)
        grid.attach(lbl("Nom"),         0, 0, 1, 1); grid.attach(self.e_name,  1, 0, 3, 1)
        grid.attach(lbl("Protocole"),   0, 1, 1, 1); grid.attach(self.cb_proto, 1, 1, 1, 1)
        grid.attach(lbl("OS"),          2, 1, 1, 1); grid.attach(self.cb_os,    3, 1, 1, 1)
        grid.attach(lbl("Hôte"),        0, 2, 1, 1); grid.attach(self.e_host,  1, 2, 1, 1)
        grid.attach(lbl("Port"),        2, 2, 1, 1); grid.attach(self.s_port,  3, 2, 1, 1)
        grid.attach(lbl("Utilisateur"), 0, 3, 1, 1); grid.attach(self.e_user,  1, 3, 3, 1)
        grid.attach(lbl("Mot de passe"),0, 4, 1, 1); grid.attach(self.e_pwd,   1, 4, 3, 1)
        grid.attach(lbl("Groupe"),      0, 5, 1, 1); grid.attach(self.e_group, 1, 5, 3, 1)
        grid.attach(self.cb_fav,        1, 6, 3, 1)
        grid.attach(self.cb_maint,      1, 7, 3, 1)
        for w in (self.e_name, self.e_host, self.e_user, self.e_pwd, self.e_group):
            w.set_hexpand(True)
        box.add(grid)

        self.add_button("Annuler", Gtk.ResponseType.CANCEL)
        save_btn = self.add_button("Enregistrer", Gtk.ResponseType.OK)
        save_btn.get_style_context().add_class("chip-primary")

        # Auto-adjust default port when protocol toggles.
        self.cb_proto.connect("changed", self._on_proto_changed)
        self.show_all()

    def _on_proto_changed(self, cb):
        proto = cb.get_active_id() or "rdp"
        if int(self.s_port.get_value()) in (22, 3389):
            self.s_port.set_value(3389 if proto == "rdp" else 22)

    def get_connection(self):
        c = dict(self._conn)
        c["name"]        = self.e_name.get_text().strip() or "Sans nom"
        c["protocol"]    = self.cb_proto.get_active_id() or "rdp"
        c["os"]          = self.cb_os.get_active_id() or "windows"
        c["host"]        = self.e_host.get_text().strip()
        c["port"]        = int(self.s_port.get_value())
        c["user"]        = self.e_user.get_text().strip()
        c["password"]    = self.e_pwd.get_text()
        c["group"]       = self.e_group.get_text().strip()
        c["favorite"]    = self.cb_fav.get_active()
        c["maintenance"] = self.cb_maint.get_active()
        return c

# ---------------------------------------------------------------------------
# Console page
# ---------------------------------------------------------------------------
class _Session:
    """Holds the state of a single open VM session."""
    def __init__(self, conn):
        self.id = uuid.uuid4().hex
        self.conn = conn
        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.box.set_hexpand(True); self.box.set_vexpand(True)
        self.socket = None
        self.vte = None
        self.proc = None
        self.started_at = time.time()
        self.latency_ms = None


class ConsolePage(Gtk.Box):
    """Hosts one or several VM sessions (RDP/SSH).
    Sessions stay alive when the user goes back to the grid; user can
    open a new session and switch between them via the ESC menu.
    No visible chrome; ESC opens a separate popup window."""

    def __init__(self, on_close, on_request_new=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.get_style_context().add_class("console-bg")
        self._on_close = on_close
        self._on_request_new = on_request_new
        self._sessions = []     # list[_Session]
        self._current = None    # current _Session
        self._menu_win = None
        self._menu_box = None
        # Performance profile: "tranquille" (quality) or "gamer" (fluid).
        try:
            _s = load_json(SETTINGS_FILE, {})
            self._perf_profile = _s.get("perf_profile", "tranquille")
        except Exception:
            self._perf_profile = "tranquille"

        # Stage : Gtk.Stack pour pouvoir empiler plusieurs sessions sans
        # jamais reparenter les GtkSocket (XEmbed ne survit pas à un
        # reparent → après quelques switchs, l'écran ne réagit plus).
        self._stage = Gtk.Stack()
        self._stage.set_transition_type(
            Gtk.StackTransitionType.NONE)
        self._stage.set_hexpand(True); self._stage.set_vexpand(True)
        self.pack_start(self._stage, True, True, 0)

    def _show_floating_menu_button(self):
        return  # disabled: keyboard-only menu

    def _hide_floating_menu_button(self):
        return  # disabled: keyboard-only menu

    def _ensure_menu_window(self):
        if self._menu_win is not None:
            return
        w = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
        w.set_decorated(False)
        w.set_resizable(False)
        w.set_skip_taskbar_hint(True)
        w.set_skip_pager_hint(True)
        w.set_type_hint(Gdk.WindowTypeHint.DIALOG)
        w.set_keep_above(True)
        # Modal : le WM met le focus dessus et masque les clics qui
        # iraient au xfreerdp embarqué (sinon le premier clic est avalué).
        w.set_modal(True)
        w.set_accept_focus(True)
        w.set_focus_on_map(True)
        w.set_position(Gtk.WindowPosition.CENTER_ALWAYS)
        # Transparent window background so CSS panel handles all visuals.
        screen = w.get_screen()
        visual = screen.get_rgba_visual()
        if visual is not None:
            w.set_visual(visual)
        w.set_app_paintable(True)
        w.connect("delete-event", lambda *_: w.hide() or True)
        w.connect("key-press-event", self._on_menu_key)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.get_style_context().add_class("shortcut-panel")
        w.add(box)
        self._menu_win = w
        self._menu_box = box

    def _on_menu_key(self, _w, event):
        if event.keyval == Gdk.KEY_Escape:
            self.toggle_menu()
            return True
        return False

    # -- Public ------------------------------------------------------------
    def start(self, conn):
        """Open a new session (in addition to existing ones) and switch to it."""
        s = _Session(conn)
        self._sessions.append(s)
        self._switch_to(s)
        if conn["protocol"] == "rdp":
            self._start_rdp(s)
        else:
            self._start_ssh(s)
        self._show_floating_menu_button()

    def has_sessions(self):
        return len(self._sessions) > 0

    def _switch_to(self, s):
        # Empile la session dans le Stack si pas déjà là, puis bascule
        # dessus. AUCUN reparent → XEmbed reste valide.
        if s.box.get_parent() is None:
            self._stage.add_named(s.box, s.id)
        s.box.show_all()
        self._stage.set_visible_child(s.box)
        self._current = s

    def _close_session(self, s):
        if s.proc and s.proc.poll() is None:
            try: s.proc.terminate()
            except OSError: pass
        log_session("end", s.conn)
        if s in self._sessions:
            self._sessions.remove(s)
        # Retire la box du Stack si elle y est encore.
        if s.box.get_parent() is self._stage:
            self._stage.remove(s.box)
        if self._sessions:
            self._switch_to(self._sessions[-1])
        else:
            self._current = None
            for child in self._stage.get_children():
                self._stage.remove(child)
            self._hide_floating_menu_button()
            thaw_background_apps()
            if callable(self._on_close):
                self._on_close()

    def close(self):
        """Close the current session (or all remaining if forced)."""
        self._hide_menu()
        if self._current:
            self._close_session(self._current)

    def close_all(self):
        self._hide_menu()
        for s in list(self._sessions):
            if s.proc and s.proc.poll() is None:
                try: s.proc.terminate()
                except OSError: pass
            log_session("end", s.conn)
        self._sessions.clear()
        self._current = None
        for child in self._stage.get_children():
            self._stage.remove(child)
        self._hide_floating_menu_button()
        thaw_background_apps()
        if callable(self._on_close):
            self._on_close()

    def is_menu_open(self):
        return self._menu_win is not None and self._menu_win.get_visible()

    def _release_menu_grab(self):
        """Libère les saisies clavier/pointeur posées par toggle_menu."""
        try:
            seat = getattr(self, "_menu_seat", None)
            if seat is not None:
                seat.ungrab()
            self._menu_seat = None
        except Exception:
            pass
        try:
            if self._menu_win is not None:
                Gtk.grab_remove(self._menu_win)
        except Exception:
            pass

    def _hide_menu(self):
        if self._menu_win is not None and self._menu_win.get_visible():
            self._release_menu_grab()
            self._menu_win.hide()

    def toggle_menu(self):
        self._ensure_menu_window()
        if self._menu_win.get_visible():
            # Libère les saisies avant de masquer la popup.
            try:
                seat = getattr(self, "_menu_seat", None)
                if seat is not None:
                    seat.ungrab()
                self._menu_seat = None
            except Exception:
                pass
            try:
                Gtk.grab_remove(self._menu_win)
            except Exception:
                pass
            self._menu_win.hide()
        else:
            self._populate_menu()
            top = self.get_toplevel()
            if isinstance(top, Gtk.Window):
                self._menu_win.set_transient_for(top)
            self._menu_win.show_all()
            # Présente AVANT le grab, sinon le grab échoue (fenêtre pas
            # encore mappée).
            self._menu_win.present_with_time(Gdk.CURRENT_TIME)
            # Vol forcer le focus clavier+pointeur sur la popup pour que
            # les clics ne soient plus interceptés par xfreerdp.
            def _force_focus():
                gdk_win = self._menu_win.get_window()
                if gdk_win is None:
                    return False
                try:
                    gdk_win.focus(Gdk.CURRENT_TIME)
                except Exception:
                    pass
                # Saisie via GdkSeat (X11/Wayland) : redirige clavier+
                # pointeur vers cette fenêtre pendant qu'elle est visible.
                try:
                    display = gdk_win.get_display()
                    seat = display.get_default_seat()
                    seat.grab(gdk_win,
                              Gdk.SeatCapabilities.ALL,
                              True,    # owner_events
                              None, None, None, None)
                    self._menu_seat = seat
                except Exception:
                    self._menu_seat = None
                # Plus une grab GTK pour les événements internes.
                try:
                    Gtk.grab_add(self._menu_win)
                except Exception:
                    pass
                return False
            GLib.idle_add(_force_focus)

    def _force_remote_redraw(self):
        return False

    # -- Menu --------------------------------------------------------------
    def _populate_menu(self):
        for c in self._menu_box.get_children():
            self._menu_box.remove(c)
        conn = (self._current.conn if self._current else {}) or {}
        title = Gtk.Label(label=f"{conn.get('name', 'Session')}", xalign=0)
        title.get_style_context().add_class("form-title")
        sub = Gtk.Label(
            label=f"{conn.get('host','')}:{conn.get('port','')}  ·  "
                  f"{conn.get('protocol','').upper()}", xalign=0)
        sub.get_style_context().add_class("form-sub")
        self._menu_box.pack_start(title, False, False, 0)
        self._menu_box.pack_start(sub,   False, False, 0)

        # ---- SESSIONS section ----
        if len(self._sessions) > 0:
            sep0 = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
            sep0.set_margin_top(6); sep0.set_margin_bottom(6)
            self._menu_box.pack_start(sep0, False, False, 0)
            hdr0 = Gtk.Label(label="SESSIONS OUVERTES", xalign=0)
            hdr0.get_style_context().add_class("nav-section")
            self._menu_box.pack_start(hdr0, False, False, 0)
            for s in self._sessions:
                row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                              spacing=4)
                # Bouton principal : bascule sur la session.
                b = Gtk.Button()
                b.get_style_context().add_class("menu-item")
                if s is self._current:
                    b.get_style_context().add_class("active")
                marker = "●" if s is self._current else "○"
                lbl = Gtk.Label(
                    label=f"{marker}  {s.conn.get('name','?')}  "
                          f"({s.conn.get('protocol','?').upper()})",
                    xalign=0)
                lbl.set_hexpand(True)
                b.add(lbl)
                b.set_hexpand(True)
                b.connect("clicked",
                          lambda _w, _s=s: self._on_session_click(_s))
                # Click molette / droit ferme la session.
                b.connect("button-press-event",
                          lambda _w, e, _s=s: self._on_session_btn(e, _s))
                row.pack_start(b, True, True, 0)

                # Vrai bouton de fermeture (et non plus un Label).
                close_btn = Gtk.Button(label="✕")
                close_btn.get_style_context().add_class("menu-item")
                close_btn.get_style_context().add_class("chip-danger")
                close_btn.set_tooltip_text("Fermer cette session")
                close_btn.connect(
                    "clicked",
                    lambda _w, _s=s: self._on_session_close(_s))
                row.pack_end(close_btn, False, False, 0)

                self._menu_box.pack_start(row, False, False, 0)

            new_btn = Gtk.Button(label="+  Ouvrir une autre VM")
            new_btn.get_style_context().add_class("menu-item")
            new_btn.connect("clicked", lambda *_: self._on_request_new_session())
            self._menu_box.pack_start(new_btn, False, False, 0)

            # ---- Presse-papier partagé -------------------------------
            paste_btn = Gtk.Button()
            paste_btn.get_style_context().add_class("menu-item")
            paste_row = Gtk.Box(
                orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            paste_lbl = Gtk.Label(
                label="📋  Coller le presse-papier dans la VM",
                xalign=0)
            paste_lbl.set_hexpand(True)
            paste_kbd = Gtk.Label(label="Ctrl  Shift  V")
            paste_kbd.get_style_context().add_class("kbd")
            paste_row.pack_start(paste_lbl, True, True, 0)
            paste_row.pack_end(paste_kbd, False, False, 0)
            paste_btn.add(paste_row)
            paste_btn.set_tooltip_text(
                "Injecte le contenu du presse-papier local dans "
                "la VM courante (utile si la synchro RDP automatique "
                "ne fonctionne pas).")
            paste_btn.connect(
                "clicked", lambda *_: self._paste_clipboard_into_vm())
            self._menu_box.pack_start(paste_btn, False, False, 0)

        sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        sep.set_margin_top(6); sep.set_margin_bottom(6)
        self._menu_box.pack_start(sep, False, False, 0)

        # ---- INFOS DE CONNEXION ----
        if self._current is not None:
            hdr_i = Gtk.Label(label="INFOS DE CONNEXION", xalign=0)
            hdr_i.get_style_context().add_class("nav-section")
            self._menu_box.pack_start(hdr_i, False, False, 0)

            grid = Gtk.Grid()
            grid.set_column_spacing(14); grid.set_row_spacing(4)
            grid.set_margin_start(4); grid.set_margin_top(2); grid.set_margin_bottom(4)

            def _add(row, label, value, value_class=None):
                k = Gtk.Label(label=label, xalign=0)
                k.get_style_context().add_class("form-sub")
                v = Gtk.Label(label=value, xalign=0)
                v.set_selectable(True)
                if value_class:
                    v.get_style_context().add_class(value_class)
                grid.attach(k, 0, row, 1, 1)
                grid.attach(v, 1, row, 1, 1)

            s = self._current
            c = s.conn
            elapsed = int(time.time() - s.started_at)
            h, rem = divmod(elapsed, 3600)
            m, sec = divmod(rem, 60)
            uptime = f"{h:02d}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"
            started = datetime.fromtimestamp(s.started_at).strftime("%H:%M:%S")

            # Latency (cached on session, refresh in background)
            self._refresh_latency_async(s)
            if s.latency_ms is None:
                lat_str, lat_cls = "mesure…", None
            else:
                lat_str = f"{s.latency_ms} ms"
                lat_cls = ("kpi-good"  if s.latency_ms < 30 else
                           "kpi-warn"  if s.latency_ms < 80 else
                           "kpi-bad")

            running = (s.proc is not None and s.proc.poll() is None)
            etat_str = "● Actif" if running else "○ Déconnecté"
            etat_cls = "kpi-good" if running else "kpi-bad"

            proto = c.get("protocol", "?").upper()
            os_name = (c.get("os") or "—").capitalize()

            _add(0, "État",       etat_str, etat_cls)
            _add(1, "Protocole",  f"{proto}  ·  {os_name}")
            _add(2, "Hôte",       f"{c.get('host','')}:{c.get('port','')}")
            _add(3, "Utilisateur", c.get("user", "—"))
            _add(4, "Ouverte à",  started)
            _add(5, "Durée",      uptime)
            _add(6, "Latence",    lat_str, lat_cls)

            if proto == "RDP":
                # Quality / redirections summary.
                disp = Gdk.Display.get_default()
                mon = disp.get_primary_monitor() or disp.get_monitor(0)
                geo = mon.get_geometry()
                gpu_v = (HW_INFO.get("gpu_vendor") or "aucun").upper()
                gpu_ok = HW_INFO.get("gpu_accel")
                gpu_str = f"{gpu_v} (accélération)" if gpu_ok else f"{gpu_v} (CPU only)"
                if self._perf_profile == "gamer":
                    if gpu_ok:
                        mode_str = "🎮 Gamer  ·  H.264 + RemoteFX (GPU)"
                        aff_str  = f"{geo.width}×{geo.height}  ·  AVC420 32 bpp"
                    else:
                        mode_str = "🎮 Gamer  ·  RemoteFX léger (CPU)"
                        aff_str  = f"{geo.width}×{geo.height}  ·  RFX 16 bpp"
                    aud_str  = "Pulse  ·  faible latence"
                else:
                    if gpu_ok:
                        mode_str = "🌙 Tranquille  ·  qualité max (GPU)"
                        aff_str  = f"{geo.width}×{geo.height}  ·  AVC444 32 bpp"
                    else:
                        mode_str = "🌙 Tranquille  ·  qualité (CPU)"
                        aff_str  = f"{geo.width}×{geo.height}  ·  RFX 32 bpp"
                    aud_str  = "Pulse  ·  micro activé"
                _add(7, "Mode",      mode_str)
                _add(8, "Affichage", aff_str)
                _add(9, "Audio",     aud_str)
                _add(10, "Matériel", gpu_str)
                _add(11, "Partages", "Presse-papiers · /home · imprimantes · USB · carte à puce")
                _add(12, "Reconnexion", "auto (5 essais)")

            self._menu_box.pack_start(grid, False, False, 0)

        # ---- PC HÔTE -------------------------------------------------
        host_btn = Gtk.Button()
        host_btn.get_style_context().add_class("menu-item")
        host_row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        host_lbl = Gtk.Label(
            label="💻  Mon PC (batterie, luminosité, volume…)",
            xalign=0)
        host_lbl.set_hexpand(True)
        host_row.pack_start(host_lbl, True, True, 0)
        host_btn.add(host_row)
        host_btn.set_tooltip_text(
            "Affiche les paramètres de ton ordinateur Linux : "
            "batterie restante, mode énergie, luminosité, volume.")
        host_btn.connect("clicked", lambda *_: self._open_host_panel())
        self._menu_box.pack_start(host_btn, False, False, 0)

        # ---- MODE DE PERFORMANCE ----
        hdr_p = Gtk.Label(label="MODE DE PERFORMANCE", xalign=0)
        hdr_p.get_style_context().add_class("nav-section")
        hdr_p.set_margin_top(6)
        self._menu_box.pack_start(hdr_p, False, False, 0)

        prow = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        prow.set_margin_start(4); prow.set_margin_end(4)

        def _mk_profile_btn(name, label, sublabel):
            b = Gtk.Button()
            b.get_style_context().add_class("chip")
            if self._perf_profile == name:
                b.get_style_context().add_class("chip-active")
            v = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            t = Gtk.Label(label=label, xalign=0.5)
            t.get_style_context().add_class("form-title")
            d = Gtk.Label(label=sublabel, xalign=0.5)
            d.get_style_context().add_class("form-sub")
            v.pack_start(t, False, False, 0)
            v.pack_start(d, False, False, 0)
            b.add(v)
            b.connect("button-press-event",
                      lambda *_: (self._set_profile(name), True)[1])
            return b

        prow.pack_start(_mk_profile_btn(
            "tranquille", "🌙  Tranquille",
            "Qualité max · audio HD · micro"), True, True, 0)
        prow.pack_start(_mk_profile_btn(
            "gamer", "🎮  Gamer",
            "Fluidité max · 16 bpp · micro coupé"), True, True, 0)
        self._menu_box.pack_start(prow, False, False, 0)

        hint = Gtk.Label(
            label="Le changement relance la session RDP active.",
            xalign=0)
        hint.get_style_context().add_class("form-sub")
        hint.set_margin_top(2)
        self._menu_box.pack_start(hint, False, False, 0)

        # ---- AUTO-DIAGNOSTIC ----
        try:
            diag = DIAG  # global rempli par run_self_check()
        except NameError:
            diag = None
        if diag and diag["results"]:
            sep_d = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
            sep_d.set_margin_top(6); sep_d.set_margin_bottom(6)
            self._menu_box.pack_start(sep_d, False, False, 0)

            hdr_d = Gtk.Label(
                label=f"AUTO-DIAGNOSTIC  ·  {diag['ok']} OK · "
                      f"{diag['warn']} ⚠  ·  {diag['fail']} ✗",
                xalign=0)
            hdr_d.get_style_context().add_class("nav-section")
            self._menu_box.pack_start(hdr_d, False, False, 0)

            # Affiche en priorité les FAIL puis WARN. Cache les OK pour
            # garder le panneau lisible (sauf si tout est OK).
            shown = [r for r in diag["results"] if r[0] in ("FAIL", "WARN")]
            if not shown:
                shown = [("OK", "Toutes les vérifications sont passées", "")]

            for level, label_, detail in shown[:8]:
                line = Gtk.Label(xalign=0)
                line.set_line_wrap(True)
                icon = {"OK": "✅", "WARN": "⚠", "FAIL": "✗"}[level]
                txt = f"{icon}  <b>{GLib.markup_escape_text(label_)}</b>"
                if detail:
                    txt += f"  <span alpha='65%'>· {GLib.markup_escape_text(detail)}</span>"
                line.set_markup(txt)
                cls = {"OK": "kpi-good", "WARN": "kpi-warn",
                       "FAIL": "kpi-bad"}[level]
                line.get_style_context().add_class(cls)
                self._menu_box.pack_start(line, False, False, 0)

        sep2 = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        sep2.set_margin_top(6); sep2.set_margin_bottom(6)
        self._menu_box.pack_start(sep2, False, False, 0)

        # Bottom actions.
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        resume = Gtk.Button(label="Reprendre")
        resume.get_style_context().add_class("chip")
        resume.connect("button-press-event",
                       lambda *_: (self.toggle_menu(), True)[1])
        back = Gtk.Button(label="←  Fermer cette session")
        back.get_style_context().add_class("chip")
        back.get_style_context().add_class("chip-danger")
        back.connect("button-press-event",
                     lambda *_: (self.close(), True)[1])
        row.pack_start(resume, True, True, 0)
        row.pack_start(back,   True, True, 0)
        self._menu_box.pack_start(row, False, False, 0)

        self._menu_box.show_all()

    def _on_session_click(self, s):
        if s is not self._current:
            self._switch_to(s)
        self.toggle_menu()

    def _on_session_close(self, s):
        """Ferme la session demandée depuis le menu."""
        was_current = (s is self._current)
        self._close_session(s)
        # Rafraîchit le menu si toujours visible et qu'il reste des sessions.
        if self._menu_win is not None and self._menu_win.get_visible():
            if self._sessions:
                self._populate_menu()
                self._menu_win.show_all()
            else:
                self._hide_menu()
        # Si on a fermé la session courante, on ressort du menu.
        if was_current and not self._sessions:
            self._hide_menu()

    def _set_profile(self, name):
        if name not in ("tranquille", "gamer"):
            return
        if name == self._perf_profile:
            return
        self._perf_profile = name
        try:
            data = load_json(SETTINGS_FILE, {})
            data["perf_profile"] = name
            save_json_atomic(SETTINGS_FILE, data)
        except Exception:
            pass
        # Apply freeze policy when switching while a session is open.
        if name != "gamer":
            thaw_background_apps()
        # Apply immediately by restarting the current RDP session.
        s = self._current
        if s is not None and s.conn.get("protocol") == "rdp":
            self._restart_session(s)
        if self._menu_win is not None and self._menu_win.get_visible():
            self._populate_menu()

    def _restart_session(self, s):
        """Kill xfreerdp and relaunch with the new profile, in place."""
        # Terminate the running process.
        if s.proc and s.proc.poll() is None:
            try:
                s.proc.terminate()
                s.proc.wait(timeout=2)
            except Exception:
                try: s.proc.kill()
                except Exception: pass
        s.proc = None
        # Drop old socket and rebuild a fresh one.
        for child in list(s.box.get_children()):
            s.box.remove(child)
        s.socket = None
        s.started_at = time.time()
        s.latency_ms = None
        # Hide menu so the user sees the reconnection.
        self._hide_menu()
        self._start_rdp(s)

    def _refresh_latency_async(self, s):
        """Probe TCP latency in a background thread (non-blocking)."""
        if getattr(s, "_lat_inflight", False):
            return
        # Cooldown : on ne re-sonde pas plus d'une fois toutes les 5 s.
        last = getattr(s, "_lat_last", 0)
        if time.time() - last < 5:
            return
        s._lat_inflight = True

        def probe():
            t0 = time.time()
            ok = False
            try:
                with socket.create_connection(
                        (s.conn["host"], int(s.conn["port"])), timeout=1.5):
                    ok = True
            except OSError:
                ok = False
            dt = int((time.time() - t0) * 1000) if ok else None

            def commit():
                s.latency_ms = dt
                s._lat_inflight = False
                s._lat_last = time.time()
                # PAS de _populate_menu() ici : reconstruire toute la popup
                # pendant que l'utilisateur clique avale ses clics. La latence
                # se rafraîchira à la prochaine ouverture du menu.
                return False
            GLib.idle_add(commit)

        threading.Thread(target=probe, daemon=True).start()

    def _on_session_btn(self, event, s):
        # Middle-click or right-click closes the session.
        if event.button in (2, 3):
            self._close_session(s)
            self._populate_menu()  # refresh popup
            return True
        return False

    def _on_request_new_session(self):
        self._hide_menu()
        if callable(self._on_request_new):
            self._on_request_new()

    def _paste_clipboard_into_vm(self):
        """Injecte le presse-papier local dans la session courante.
        - SSH (VTE)  : utilise Vte.Terminal.paste_clipboard().
        - RDP         : tape le texte via xdotool dans la fenêtre xfreerdp.
        Sert de secours quand la synchro presse-papier RDP automatique
        (+clipboard) n'est pas disponible côté VM (Windows sans RDP
        clipboard, GPO, etc.). Dans la majorité des cas, un simple
        Ctrl+V dans la VM suffit déjà.
        """
        s = self._current
        if s is None:
            return
        # Récupère le presse-papier local.
        clip = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        text = clip.wait_for_text() or ""
        if not text:
            return
        # Ferme la popup pour rendre la VM focus.
        self._hide_menu()

        # SSH : on colle directement dans VTE.
        if s.conn.get("protocol") == "ssh" and s.vte is not None:
            try:
                s.vte.feed_child(text.encode("utf-8"))
            except Exception:
                try:
                    s.vte.paste_clipboard()
                except Exception:
                    pass
            return

        # RDP : on tape le texte via xdotool dans la fenêtre embarquée.
        if not shutil.which("xdotool"):
            return
        # Petit délai pour laisser le focus revenir à xfreerdp.
        def _do_type():
            try:
                subprocess.Popen(
                    ["xdotool", "type", "--delay", "8", "--", text])
            except OSError:
                pass
            return False
        GLib.timeout_add(180, _do_type)

    # -- Panneau "PC HÔTE" ------------------------------------------------
    def _open_host_panel(self):
        """Popup avec les paramètres réels de l'ordinateur local :
        batterie, mode énergie (avec lecture live de la fréquence CPU),
        luminosité (via logind D-Bus, vraiment fonctionnelle),
        volume (avec bleep de retour à chaque changement)."""
        self._hide_menu()
        win = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
        win.set_title("Mon PC")
        win.set_modal(True)
        win.set_default_size(560, 0)
        win.set_position(Gtk.WindowPosition.CENTER_ON_PARENT)
        try:
            top = self.get_toplevel()
            if isinstance(top, Gtk.Window):
                win.set_transient_for(top)
        except Exception:
            pass
        win.get_style_context().add_class("menu-popup")

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        outer.set_margin_start(18); outer.set_margin_end(18)
        outer.set_margin_top(16);   outer.set_margin_bottom(16)
        win.add(outer)

        title = Gtk.Label(label="💻  Mon PC", xalign=0)
        title.get_style_context().add_class("section-title")
        outer.pack_start(title, False, False, 0)
        sub = Gtk.Label(
            label="Paramètres réels de ton ordinateur Linux",
            xalign=0)
        sub.get_style_context().add_class("form-sub")
        outer.pack_start(sub, False, False, 0)

        # ---- BATTERIE ---------------------------------------------------
        bat_pct, bat_status = self._read_battery()
        bat_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        bat_hdr = Gtk.Label(label="🔋  Batterie", xalign=0)
        bat_hdr.get_style_context().add_class("nav-section")
        bat_box.pack_start(bat_hdr, False, False, 0)
        bat_bar = None
        if bat_pct is None:
            bat_lbl = Gtk.Label(label="Aucune batterie détectée.", xalign=0)
            bat_lbl.get_style_context().add_class("form-sub")
            bat_box.pack_start(bat_lbl, False, False, 0)
        else:
            bat_bar = Gtk.ProgressBar()
            bat_bar.set_show_text(True)
            self._refresh_bat_bar(bat_bar)
            bat_box.pack_start(bat_bar, False, False, 0)
        outer.pack_start(bat_box, False, False, 0)

        # ---- MODE ÉNERGIE ----------------------------------------------
        cur_profile, profiles = self._read_power_profile()
        pwr_chips = {}
        live_lbl = None
        if profiles:
            pwr_box = Gtk.Box(
                orientation=Gtk.Orientation.VERTICAL, spacing=4)
            pwr_hdr = Gtk.Label(label="⚙  Mode énergie", xalign=0)
            pwr_hdr.get_style_context().add_class("nav-section")
            pwr_box.pack_start(pwr_hdr, False, False, 0)
            pwr_row = Gtk.Box(
                orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            pwr_row.set_homogeneous(True)
            for p in profiles:
                emoji = {"power-saver": "🌙",
                         "balanced":    "⚖",
                         "performance": "🚀"}.get(p, "•")
                # Libellés courts pour que les 3 chips tiennent côte à côte.
                lbl = {"power-saver": "Éco",
                       "balanced":    "Équilibré",
                       "performance": "Perf"}.get(p, p)
                b = Gtk.Button()
                b.get_style_context().add_class("chip")
                if p == cur_profile:
                    b.get_style_context().add_class("chip-active")
                inner = Gtk.Box(
                    orientation=Gtk.Orientation.VERTICAL, spacing=0)
                e_lbl = Gtk.Label(label=emoji)
                e_lbl.get_style_context().add_class("form-title")
                t_lbl = Gtk.Label(label=lbl)
                t_lbl.get_style_context().add_class("form-sub")
                inner.pack_start(e_lbl, False, False, 0)
                inner.pack_start(t_lbl, False, False, 0)
                b.add(inner)
                b.set_hexpand(True)
                b.connect(
                    "clicked",
                    lambda _w, _p=p: self._apply_power_profile(_p, pwr_chips))
                pwr_chips[p] = b
                pwr_row.pack_start(b, True, True, 0)
            pwr_box.pack_start(pwr_row, False, False, 0)

            # Lecture live du système (CPU GHz + EPP) pour PROUVER l'effet.
            live_lbl = Gtk.Label(xalign=0)
            live_lbl.get_style_context().add_class("form-sub")
            self._refresh_live_cpu(live_lbl)
            pwr_box.pack_start(live_lbl, False, False, 0)
            outer.pack_start(pwr_box, False, False, 0)

        # ---- LUMINOSITÉ -------------------------------------------------
        bl_name, bl_cur, bl_max = self._read_brightness()
        if bl_name is not None and bl_max:
            br_box = Gtk.Box(
                orientation=Gtk.Orientation.VERTICAL, spacing=2)
            br_hdr = Gtk.Label(label="💡  Luminosité", xalign=0)
            br_hdr.get_style_context().add_class("nav-section")
            br_box.pack_start(br_hdr, False, False, 0)
            adj = Gtk.Adjustment(
                value=bl_cur, lower=max(1, int(bl_max * 0.05)),
                upper=bl_max, step_increment=max(1, bl_max // 20),
                page_increment=max(1, bl_max // 10))
            scale = Gtk.Scale(
                orientation=Gtk.Orientation.HORIZONTAL, adjustment=adj)
            scale.set_draw_value(False)
            scale.set_hexpand(True)
            # Debounce : on n'appelle logind qu'au plus tous les 60 ms
            # pour ne pas saturer le bus pendant le drag.
            self._br_pending = None
            scale.connect(
                "value-changed",
                lambda w, _bl=bl_name: self._set_brightness_debounced(
                    _bl, int(w.get_value())))
            br_box.pack_start(scale, False, False, 0)
            outer.pack_start(br_box, False, False, 0)

        # ---- RÉSEAU BAS-LATENCE -----------------------------------------
        net_state = self._read_network_state()
        net_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        net_hdr = Gtk.Label(label="🌐  Réseau bas-latence", xalign=0)
        net_hdr.get_style_context().add_class("nav-section")
        net_box.pack_start(net_hdr, False, False, 0)
        net_status = Gtk.Label(xalign=0)
        net_status.set_line_wrap(True)
        net_status.get_style_context().add_class("form-sub")
        net_box.pack_start(net_status, False, False, 0)
        net_btn = Gtk.Button()
        net_btn.get_style_context().add_class("chip")
        net_box.pack_start(net_btn, False, False, 0)
        self._refresh_network_chip(net_btn, net_status)
        net_btn.connect(
            "clicked",
            lambda _w: self._toggle_network_boost(net_btn, net_status))
        outer.pack_start(net_box, False, False, 0)

        # ---- VOLUME -----------------------------------------------------
        vol = self._read_volume()
        vol_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        vol_hdr = Gtk.Label(label="🔊  Volume", xalign=0)
        vol_hdr.get_style_context().add_class("nav-section")
        vol_box.pack_start(vol_hdr, False, False, 0)
        if vol is None:
            no_vol = Gtk.Label(
                label="Contrôle du volume indisponible "
                      "(installe pactl/pulseaudio-utils).",
                xalign=0)
            no_vol.get_style_context().add_class("form-sub")
            vol_box.pack_start(no_vol, False, False, 0)
        else:
            adj_v = Gtk.Adjustment(
                value=vol, lower=0, upper=150,
                step_increment=5, page_increment=10)
            scale_v = Gtk.Scale(
                orientation=Gtk.Orientation.HORIZONTAL, adjustment=adj_v)
            scale_v.set_draw_value(True)
            scale_v.set_value_pos(Gtk.PositionType.RIGHT)
            scale_v.set_hexpand(True)
            for mark in (0, 50, 100):
                scale_v.add_mark(mark, Gtk.PositionType.BOTTOM, None)
            self._vol_last_bleep = 0.0
            scale_v.connect(
                "value-changed",
                lambda w: self._set_volume_with_bleep(int(w.get_value())))
            vol_box.pack_start(scale_v, False, False, 0)
        outer.pack_start(vol_box, False, False, 0)

        # ---- Fermer -----------------------------------------------------
        close = Gtk.Button(label="Fermer")
        close.get_style_context().add_class("chip")
        close.connect("clicked", lambda *_: win.destroy())
        outer.pack_start(close, False, False, 6)

        # ---- Tick live --------------------------------------------------
        # Rafraîchit batterie + CPU live tant que la fenêtre est ouverte.
        def _tick():
            if not win.get_visible():
                return False
            if bat_bar is not None:
                self._refresh_bat_bar(bat_bar)
            if live_lbl is not None:
                self._refresh_live_cpu(live_lbl)
            return True
        GLib.timeout_add(1500, _tick)

        win.show_all()

    # -- Helpers système hôte --------------------------------------------
    def _refresh_bat_bar(self, bar):
        pct, status = self._read_battery()
        if pct is None:
            return
        bar.set_fraction(max(0.0, min(1.0, pct / 100.0)))
        s = (status or "inconnu").lower()
        if "charg" in s and "not" not in s:
            icon = "⚡"
        elif "full" in s:
            icon = "🔌"
        else:
            icon = "🔋"
        bar.set_text(f"{icon}  {pct}%  ·  {status or 'inconnu'}")

    def _refresh_live_cpu(self, label):
        # Fréquence moyenne + EPP du CPU0 = preuve que le mode est actif.
        try:
            freqs = []
            base = "/sys/devices/system/cpu"
            for entry in sorted(os.listdir(base)):
                if not entry.startswith("cpu") or not entry[3:].isdigit():
                    continue
                p = os.path.join(base, entry, "cpufreq", "scaling_cur_freq")
                if os.path.isfile(p):
                    try:
                        with open(p) as f:
                            freqs.append(int(f.read().strip()))
                    except (OSError, ValueError):
                        pass
            avg_ghz = (sum(freqs) / len(freqs) / 1_000_000) if freqs else 0.0
            mx_ghz  = (max(freqs) / 1_000_000) if freqs else 0.0
        except OSError:
            avg_ghz = mx_ghz = 0.0
        epp = ""
        try:
            with open("/sys/devices/system/cpu/cpu0/cpufreq/"
                      "energy_performance_preference") as f:
                epp = f.read().strip()
        except OSError:
            pass
        turbo = ""
        try:
            with open("/sys/devices/system/cpu/intel_pstate/no_turbo") as f:
                turbo = "off" if f.read().strip() == "1" else "on"
        except OSError:
            pass
        parts = [f"CPU live : {avg_ghz:.2f} GHz moy · {mx_ghz:.2f} GHz max"]
        if epp:
            parts.append(f"EPP={epp}")
        if turbo:
            parts.append(f"turbo {turbo}")
        label.set_text("  ·  ".join(parts))

    def _read_battery(self):
        try:
            base = "/sys/class/power_supply"
            if not os.path.isdir(base):
                return None, None
            for name in sorted(os.listdir(base)):
                if not name.startswith("BAT"):
                    continue
                cap_p = os.path.join(base, name, "capacity")
                st_p  = os.path.join(base, name, "status")
                if not os.path.isfile(cap_p):
                    continue
                with open(cap_p) as f:
                    pct = int(f.read().strip())
                status = ""
                if os.path.isfile(st_p):
                    with open(st_p) as f:
                        status = f.read().strip()
                return pct, status
        except (OSError, ValueError):
            pass
        return None, None

    def _read_power_profile(self):
        if shutil.which("powerprofilesctl"):
            try:
                cur = subprocess.check_output(
                    ["powerprofilesctl", "get"],
                    text=True, timeout=2).strip()
                lst = subprocess.check_output(
                    ["powerprofilesctl", "list"],
                    text=True, timeout=2)
                profs = []
                for line in lst.splitlines():
                    line = line.strip()
                    for p in ("performance", "balanced", "power-saver"):
                        if line.startswith(p + ":") or line == p + ":":
                            if p not in profs:
                                profs.append(p)
                if not profs:
                    profs = ["power-saver", "balanced", "performance"]
                return cur, profs
            except (subprocess.SubprocessError, OSError):
                pass
        return None, []

    def _apply_power_profile(self, profile, chips):
        """Change le profil énergie ET met à jour visuellement les chips
        sans fermer le panneau. Tente aussi des actions complémentaires
        (turbo, dpm GPU) si possible — silencieusement si refusé."""
        if shutil.which("powerprofilesctl"):
            try:
                subprocess.run(
                    ["powerprofilesctl", "set", profile],
                    check=False, timeout=3)
            except (subprocess.SubprocessError, OSError):
                pass
        # Mise en évidence du chip actif.
        for p, b in chips.items():
            ctx = b.get_style_context()
            if p == profile:
                ctx.add_class("chip-active")
            else:
                ctx.remove_class("chip-active")
        # Petit son de confirmation.
        self._play_bleep()

    def _read_brightness(self):
        """Retourne (nom_subsystem, valeur, max). Privilégie intel_backlight."""
        try:
            base = "/sys/class/backlight"
            if not os.path.isdir(base):
                return None, 0, 0
            entries = sorted(os.listdir(base))
            if not entries:
                return None, 0, 0
            name = entries[0]
            with open(os.path.join(base, name, "max_brightness")) as f:
                bmax = int(f.read().strip())
            try:
                with open(os.path.join(base, name, "actual_brightness")) as f:
                    bcur = int(f.read().strip())
            except OSError:
                with open(os.path.join(base, name, "brightness")) as f:
                    bcur = int(f.read().strip())
            return name, bcur, bmax
        except (OSError, ValueError):
            return None, 0, 0

    def _set_brightness_debounced(self, bl_name, value):
        """Coalesce les appels rapides pendant un drag du slider."""
        self._br_pending = (bl_name, int(max(1, value)))
        if getattr(self, "_br_timer", None):
            return
        def _flush():
            self._br_timer = None
            if self._br_pending is None:
                return False
            n, v = self._br_pending
            self._br_pending = None
            self._set_brightness_now(n, v)
            return False
        # 50 ms : assez réactif visuellement, sans saturer logind.
        self._br_timer = GLib.timeout_add(50, _flush)

    def _set_brightness_now(self, bl_name, value):
        """Utilise logind D-Bus (org.freedesktop.login1.Session.SetBrightness)
        — pas besoin de sudo, ça change vraiment l'écran."""
        try:
            subprocess.run(
                ["busctl", "call",
                 "org.freedesktop.login1",
                 "/org/freedesktop/login1/session/auto",
                 "org.freedesktop.login1.Session",
                 "SetBrightness", "ssu",
                 "backlight", bl_name, str(int(value))],
                check=False, timeout=2,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL)
        except (subprocess.SubprocessError, OSError):
            # Fallback : écriture directe (nécessite groupe video + udev).
            try:
                with open(f"/sys/class/backlight/{bl_name}/brightness",
                          "w") as f:
                    f.write(str(int(value)))
            except OSError:
                pass

    def _read_volume(self):
        if not shutil.which("pactl"):
            return None
        try:
            out = subprocess.check_output(
                ["pactl", "get-sink-volume", "@DEFAULT_SINK@"],
                text=True, timeout=2)
            for tok in out.replace(",", ".").split():
                if tok.endswith("%"):
                    try:
                        return int(tok.rstrip("%"))
                    except ValueError:
                        continue
        except (subprocess.SubprocessError, OSError):
            pass
        return None

    def _set_volume_with_bleep(self, percent):
        if not shutil.which("pactl"):
            return
        p = max(0, min(150, int(percent)))
        try:
            subprocess.Popen(
                ["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{p}%"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL)
        except OSError:
            return
        # Bleep de retour, mais pas plus d'un toutes les 120 ms pendant
        # un drag continu pour éviter une cacophonie.
        now = time.monotonic()
        if now - getattr(self, "_vol_last_bleep", 0) >= 0.12:
            self._vol_last_bleep = now
            self._play_bleep()

    def _play_bleep(self):
        """Joue le son standard 'audio-volume-change' du thème freedesktop.
        Essaie canberra-gtk-play (libcanberra), sinon paplay sur le .oga,
        sinon un beep terminal en dernier recours."""
        if shutil.which("canberra-gtk-play"):
            try:
                subprocess.Popen(
                    ["canberra-gtk-play", "-i", "audio-volume-change"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL)
                return
            except OSError:
                pass
        oga = "/usr/share/sounds/freedesktop/stereo/audio-volume-change.oga"
        if os.path.isfile(oga) and shutil.which("paplay"):
            try:
                subprocess.Popen(
                    ["paplay", oga],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL)
                return
            except OSError:
                pass

    # -- Réseau bas-latence ----------------------------------------------
    def _read_network_state(self):
        """Retourne dict avec l'état actuel : congestion control, qdisc,
        wifi power_save, interface active."""
        st = {"cc": "?", "qdisc": "?", "wifi_ps": None,
              "iface": None, "iface_type": None}
        try:
            with open(
                    "/proc/sys/net/ipv4/tcp_congestion_control") as f:
                st["cc"] = f.read().strip()
        except OSError:
            pass
        try:
            with open("/proc/sys/net/core/default_qdisc") as f:
                st["qdisc"] = f.read().strip()
        except OSError:
            pass
        # Interface par défaut.
        try:
            out = subprocess.check_output(
                ["ip", "-o", "route", "show", "default"],
                text=True, timeout=1)
            for tok in out.split():
                if tok == "dev":
                    idx = out.split().index("dev")
                    st["iface"] = out.split()[idx + 1]
                    break
        except (subprocess.SubprocessError, OSError, ValueError):
            pass
        # Type WiFi/Ethernet via nmcli.
        if st["iface"] and shutil.which("nmcli"):
            try:
                out = subprocess.check_output(
                    ["nmcli", "-t", "-f", "DEVICE,TYPE", "d"],
                    text=True, timeout=1)
                for line in out.splitlines():
                    parts = line.split(":")
                    if len(parts) >= 2 and parts[0] == st["iface"]:
                        st["iface_type"] = parts[1]
                        break
            except (subprocess.SubprocessError, OSError):
                pass
        # WiFi power_save (via iwconfig si dispo).
        if st["iface_type"] == "wifi" and shutil.which("iwconfig"):
            try:
                out = subprocess.check_output(
                    ["iwconfig", st["iface"]],
                    text=True, timeout=1,
                    stderr=subprocess.DEVNULL)
                for line in out.splitlines():
                    if "Power Management" in line:
                        st["wifi_ps"] = (
                            "off" if "off" in line.lower() else "on")
                        break
            except (subprocess.SubprocessError, OSError):
                pass
        return st

    def _is_network_boosted(self, st=None):
        if st is None:
            st = self._read_network_state()
        boosted = (st.get("cc") == "bbr" and st.get("qdisc") == "fq")
        if st.get("iface_type") == "wifi":
            boosted = boosted and (st.get("wifi_ps") == "off")
        return boosted

    def _refresh_network_chip(self, btn, status_lbl):
        st = self._read_network_state()
        active = self._is_network_boosted(st)
        # Vide les enfants existants.
        for c in btn.get_children():
            btn.remove(c)
        if active:
            btn.set_label("✅  Bas-latence actif  ·  rétablir")
            ctx = btn.get_style_context()
            ctx.remove_class("chip-active")
            ctx.add_class("chip-good")
        else:
            btn.set_label("🚀  Activer le mode bas-latence")
            ctx = btn.get_style_context()
            ctx.remove_class("chip-good")
            ctx.add_class("chip-active")
        wifi_txt = ""
        if st.get("iface_type") == "wifi":
            ps = st.get("wifi_ps") or "?"
            wifi_txt = f"  ·  WiFi power-save : {ps}"
        status_lbl.set_text(
            f"Interface : {st.get('iface') or '?'}  "
            f"({st.get('iface_type') or '?'}){wifi_txt}\n"
            f"TCP : {st.get('cc')}  ·  qdisc : {st.get('qdisc')}")

    def _toggle_network_boost(self, btn, status_lbl):
        """Bascule le mode bas-latence. Demande pkexec une seule fois."""
        st = self._read_network_state()
        active = self._is_network_boosted(st)

        # Action via pkexec — un seul prompt grâce au heredoc.
        if not active:
            script = r'''
set +e
# 1) Coupe le wifi power-save sur tout NM.
for cn in $(nmcli -t -f NAME,TYPE c show 2>/dev/null \
        | awk -F: '$2=="802-11-wireless"{print $1}'); do
    nmcli c modify "$cn" 802-11-wireless.powersave 2 2>/dev/null
done
for dev in $(nmcli -t -f DEVICE,TYPE d 2>/dev/null \
        | awk -F: '$2=="wifi"{print $1}'); do
    iwconfig "$dev" power off 2>/dev/null
done
# 2) Active TCP BBR (best latency under load).
modprobe tcp_bbr 2>/dev/null
sysctl -w net.ipv4.tcp_congestion_control=bbr >/dev/null 2>&1
sysctl -w net.core.default_qdisc=fq           >/dev/null 2>&1
sysctl -w net.ipv4.tcp_notsent_lowat=16384    >/dev/null 2>&1
sysctl -w net.ipv4.tcp_low_latency=1          >/dev/null 2>&1
sysctl -w net.core.netdev_budget=600          >/dev/null 2>&1
sysctl -w net.core.busy_poll=50               >/dev/null 2>&1
sysctl -w net.core.busy_read=50               >/dev/null 2>&1
echo OK
'''
        else:
            script = r'''
set +e
# Restaure les valeurs Linux par défaut (Ubuntu/Mint).
for cn in $(nmcli -t -f NAME,TYPE c show 2>/dev/null \
        | awk -F: '$2=="802-11-wireless"{print $1}'); do
    nmcli c modify "$cn" 802-11-wireless.powersave 0 2>/dev/null
done
for dev in $(nmcli -t -f DEVICE,TYPE d 2>/dev/null \
        | awk -F: '$2=="wifi"{print $1}'); do
    iwconfig "$dev" power on 2>/dev/null
done
sysctl -w net.ipv4.tcp_congestion_control=cubic    >/dev/null 2>&1
sysctl -w net.core.default_qdisc=fq_codel           >/dev/null 2>&1
sysctl -w net.ipv4.tcp_notsent_lowat=4294967295     >/dev/null 2>&1
sysctl -w net.ipv4.tcp_low_latency=0                >/dev/null 2>&1
sysctl -w net.core.netdev_budget=300                >/dev/null 2>&1
sysctl -w net.core.busy_poll=0                      >/dev/null 2>&1
sysctl -w net.core.busy_read=0                      >/dev/null 2>&1
echo OK
'''
        # Désactive temporairement le bouton pour éviter double-clic.
        btn.set_sensitive(False)

        def _run():
            ok = False
            try:
                r = subprocess.run(
                    ["pkexec", "sh", "-c", script],
                    capture_output=True, text=True, timeout=20)
                ok = (r.returncode == 0 and "OK" in r.stdout)
            except (subprocess.SubprocessError, OSError):
                ok = False

            def _done():
                btn.set_sensitive(True)
                self._refresh_network_chip(btn, status_lbl)
                self._play_bleep()
                if not ok:
                    status_lbl.set_text(
                        status_lbl.get_text()
                        + "\n⚠ Échec : authentification refusée ou "
                        "pkexec indisponible.")
                return False
            GLib.idle_add(_done)

        threading.Thread(target=_run, daemon=True).start()

    def _shortcuts_for(self, conn):
        proto = conn.get("protocol", "rdp")
        os_name = (conn.get("os") or "").lower()
        if proto == "ssh":
            return [
                ("Interrompre la commande",     "ctrl+c"),
                ("Effacer l'écran",             "ctrl+l"),
                ("Mettre en arrière-plan",      "ctrl+z"),
                ("Fin de fichier (logout)",     "ctrl+d"),
                ("Recherche dans l'historique", "ctrl+r"),
            ]
        if os_name == "linux":
            return [
                ("Verrouiller la session",      "super+l"),
                ("Activités / vue d'ensemble",  "super"),
                ("Afficher le bureau",          "super+d"),
                ("Explorateur de fichiers",     "super+e"),
                ("Changer de fenêtre",          "alt+Tab"),
                ("Fermer la fenêtre",           "alt+F4"),
                ("Quitter l'application",       "ctrl+q"),
                ("Ouvrir un terminal",          "ctrl+alt+t"),
                ("Tuiler à gauche",             "super+Left"),
                ("Tuiler à droite",             "super+Right"),
                ("Maximiser",                   "super+Up"),
                ("Minimiser",                   "super+Down"),
                ("Bureau précédent",            "ctrl+alt+Left"),
                ("Bureau suivant",              "ctrl+alt+Right"),
                ("Capture d'écran",             "Print"),
                ("Capture de zone",             "shift+Print"),
            ]
        if os_name == "macos":
            return [
                ("Spotlight",                   "super+space"),
                ("Mission Control",             "ctrl+Up"),
                ("Changer d'application",       "super+Tab"),
                ("Fermer la fenêtre",           "super+w"),
                ("Quitter l'application",       "super+q"),
            ]
        # Default: Windows.
        return [
            ("Ctrl + Alt + Suppr",          "ctrl+alt+Delete"),
            ("Ctrl + Alt + Fin (RDP)",      "ctrl+alt+End"),
            ("Verrouiller la session",      "super+l"),
            ("Afficher le bureau",          "super+d"),
            ("Explorateur de fichiers",     "super+e"),
            ("Exécuter",                    "super+r"),
            ("Menu utilisateurs avancés",   "super+x"),
            ("Changer de fenêtre",          "alt+Tab"),
            ("Fermer la fenêtre",           "alt+F4"),
            ("Gestionnaire des tâches",     "ctrl+shift+Escape"),
            ("Capture d'écran (zone)",      "super+shift+s"),
        ]

    def _send_keys(self, combo):
        """Send a key combo to the remote window via xdotool."""
        def do_send():
            try:
                # The xfreerdp plug already has keyboard focus by default
                # since it grabs it. Just inject keys via XTest.
                subprocess.Popen(["xdotool", "key", combo])
                print(f"[vmshell] sent '{combo}'", flush=True)
            except OSError as e:
                print(f"[vmshell] xdotool error: {e}", flush=True)
            return False

        # Small delay so the popup has time to fully hide.
        GLib.timeout_add(120, do_send)

    # -- RDP ---------------------------------------------------------------
    def _start_rdp(self, s):
        conn = s.conn
        bin_ = find_xfreerdp()
        if not bin_:
            self._show_error(s, "xfreerdp3 introuvable. Installez-le pour le RDP.")
            return
        s.socket = Gtk.Socket()
        s.socket.set_hexpand(True); s.socket.set_vexpand(True)
        s.socket.set_can_focus(True)
        s.box.pack_start(s.socket, True, True, 0)
        s.box.show_all()
        s.socket.realize()
        # Ensure keyboard input flows into the embedded RDP plug.
        def _focus_plug():
            try:
                s.socket.grab_focus()
                if s.socket.get_plug_window():
                    s.socket.get_plug_window().focus(Gdk.CURRENT_TIME)
            except Exception:
                pass
            return False
        GLib.timeout_add(150, _focus_plug)

        screen = Gdk.Screen.get_default()
        sw = screen.get_width() if screen else 1920
        sh = screen.get_height() if screen else 1080

        def launch():
            try:
                with socket.create_connection(
                        (conn["host"], int(conn["port"])), timeout=2.5):
                    pass
            except OSError as e:
                log_session("failed", conn, str(e))
                GLib.idle_add(self._show_error, s,
                              f"Hôte injoignable : {conn['host']}:{conn['port']}")
                return
            xid = s.socket.get_id()
            home = os.path.expanduser("~")
            profile = (self._perf_profile or "tranquille").lower()
            cmd = [bin_,
                   f"/v:{conn['host']}:{conn['port']}",
                   f"/u:{conn['user']}",
                   f"/size:{sw}x{sh}",
                   "/cert:ignore",
                   "/dynamic-resolution",
                   "+clipboard",
                   f"/drive:home,{home}",
                   "/printer",
                   "/usb:auto",
                   "/smartcard",
                   "/auto-reconnect",
                   "/auto-reconnect-max-retries:5"]
            if profile == "gamer":
                hw_ok = bool(HW_INFO.get("gpu_accel"))
                if hw_ok:
                    # GPU dispo → H.264 progressif + RemoteFX vidéo (décodage assisté).
                    # Pas de "thin-client:on" : il coalesce les frames et
                    # rend la sélection au clic gauche saccadée.
                    cmd += ["/network:broadband",
                            "/bpp:32",
                            "/gfx:AVC420:on,progressive:on",
                            "+rfx",
                            "/rfx-mode:video",
                            "+compression",
                            "+async-update",
                            "+async-channels",
                            "/frame-ack:2",
                            "/sound:sys:pulse,quality:medium,latency:40",
                            "-wallpaper", "-themes", "-aero",
                            "-menu-anims", "-window-drag", "-fonts"]
                else:
                    # Pas de GPU → décodage CPU uniquement, on allège tout :
                    # RemoteFX seul (codec optimisé CPU), 16 bpp, sans AVC.
                    cmd += ["/network:broadband",
                            "/bpp:16",
                            "+rfx",
                            "/rfx-mode:image",
                            "+compression",
                            "+async-update",
                            "+async-channels",
                            "/frame-ack:4",
                            "/sound:sys:pulse,quality:low,latency:60",
                            "-wallpaper", "-themes", "-aero",
                            "-menu-anims", "-window-drag", "-fonts",
                            "-decorations"]
            else:
                if HW_INFO.get("gpu_accel"):
                    # Mode tranquille avec GPU : qualité max, AVC444 32bpp.
                    cmd += ["/network:lan",
                            "/bpp:32",
                            "/gfx:AVC444:on,progressive:on",
                            "-compression",
                            "/sound:sys:pulse",
                            "/microphone:sys:pulse"]
                else:
                    # Pas de GPU : on reste qualité confort mais sans AVC444
                    # (qui sature un CPU modeste). RemoteFX 32bpp suffit.
                    cmd += ["/network:lan",
                            "/bpp:32",
                            "+rfx",
                            "+compression",
                            "/sound:sys:pulse",
                            "/microphone:sys:pulse"]
            cmd.append(f"/parent-window:{xid}")
            if conn.get("password"):
                cmd.append(f"/p:{conn['password']}")
            try:
                s.proc = subprocess.Popen(cmd)
                log_session("start", conn)
                if profile == "gamer":
                    freeze_background_apps()
            except OSError as e:
                log_session("failed", conn, str(e))
                GLib.idle_add(self._show_error, s, f"Échec : {e}")

        def when_ready():
            a = s.socket.get_allocation()
            if a.width > 100 and a.height > 100:
                launch()
                return False
            return True
        GLib.timeout_add(50, when_ready)

    # -- SSH ---------------------------------------------------------------
    def _start_ssh(self, s):
        conn = s.conn
        s.vte = Vte.Terminal()
        s.vte.set_hexpand(True); s.vte.set_vexpand(True)
        try:
            s.vte.set_font(Pango.FontDescription("Monospace 11"))
        except Exception:
            pass
        s.box.pack_start(s.vte, True, True, 0)
        s.box.show_all()

        target = f"{conn['user']}@{conn['host']}" if conn.get("user") else conn["host"]
        argv = ["ssh", "-p", str(conn["port"]), target]
        try:
            s.vte.spawn_async(
                Vte.PtyFlags.DEFAULT, None, argv, None,
                GLib.SpawnFlags.SEARCH_PATH, None, None,
                -1, None, None, None,
            )
        except Exception as e:
            self._show_error(s, f"Échec SSH : {e}")

    # -- Error -------------------------------------------------------------
    def _show_error(self, s, msg):
        for c in s.box.get_children():
            s.box.remove(c)
        b = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        b.set_valign(Gtk.Align.CENTER); b.set_halign(Gtk.Align.CENTER)
        ico = Gtk.Label(label="⚠"); ico.get_style_context().add_class("console-loading-icon")
        lab = Gtk.Label(label=msg); lab.get_style_context().add_class("console-loading")
        b.pack_start(ico, False, False, 0); b.pack_start(lab, False, False, 0)
        s.box.pack_start(b, True, True, 0)
        s.box.show_all()


# ---------------------------------------------------------------------------
# Animated background (colorful blobs)
# ---------------------------------------------------------------------------
import math  # noqa: E402

class AnimatedBackground(Gtk.DrawingArea):
    """Static colorful gradient background (no animation, no CPU cost)."""

    BLOBS = [
        # (color rgb, x_ratio, y_ratio, radius_ratio, alpha_peak)
        ((0.22, 0.27, 0.55), 0.15, 0.20, 0.55, 0.55),  # deep blue
        ((0.36, 0.22, 0.55), 0.85, 0.18, 0.50, 0.50),  # deep violet
        ((0.50, 0.18, 0.38), 0.78, 0.85, 0.50, 0.45),  # muted plum
        ((0.10, 0.40, 0.40), 0.18, 0.85, 0.55, 0.45),  # muted teal
        ((0.45, 0.30, 0.18), 0.55, 0.55, 0.40, 0.30),  # warm amber-brown
    ]

    def __init__(self):
        super().__init__()
        self.set_hexpand(True); self.set_vexpand(True)
        self.connect("draw", self._on_draw)

    def _on_draw(self, _w, cr):
        a = self.get_allocation()
        W, H = a.width, a.height
        if W <= 0 or H <= 0:
            return False
        try:
            import cairo
            grad = cairo.LinearGradient(0, 0, W, H)
            grad.add_color_stop_rgb(0.0, 0.078, 0.090, 0.137)   # #141723
            grad.add_color_stop_rgb(0.5, 0.106, 0.106, 0.165)
            grad.add_color_stop_rgb(1.0, 0.137, 0.094, 0.180)
            cr.set_source(grad)
            cr.rectangle(0, 0, W, H)
            cr.fill()

            cr.set_operator(cairo.OPERATOR_OVER)
            for (r, g, b), bx, by, rr, peak in self.BLOBS:
                cx, cy = bx * W, by * H
                radius = rr * max(W, H)
                rg = cairo.RadialGradient(cx, cy, 0, cx, cy, radius)
                rg.add_color_stop_rgba(0.0, r, g, b, peak)
                rg.add_color_stop_rgba(0.6, r, g, b, peak * 0.30)
                rg.add_color_stop_rgba(1.0, r, g, b, 0.0)
                cr.set_source(rg)
                cr.rectangle(0, 0, W, H)
                cr.fill()
        except Exception:
            cr.set_source_rgb(0.078, 0.090, 0.137)
            cr.paint()
        return False


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------
class VMShell(Gtk.Window):

    def __init__(self):
        super().__init__(title="VMShell")
        self.get_style_context().add_class("root")
        self.set_default_size(1280, 800)
        self.set_decorated(False)
        self.set_app_paintable(True)
        self.fullscreen()
        self.connect("destroy", Gtk.main_quit)
        self.connect("key-press-event", self._on_key)

        ensure_config_dir()
        self._connections = load_json(CONNS_FILE, [])
        self._settings    = load_json(SETTINGS_FILE, {"view_mode": "grid"})
        self._filter      = ""
        self._nav_mode    = "all"          # all | favorites | history | settings
        self._view_mode   = self._settings.get("view_mode", "grid")
        self._status      = {}             # id -> "online"|"offline"|"checking"|...
        self._nav_buttons = {}
        self._search_entry = None

        self._apply_css()
        self._build_ui()
        self._render()

        GLib.timeout_add_seconds(1, self._tick_clock)
        GLib.timeout_add(300,  lambda: (self._check_all_statuses(), False)[1])
        GLib.timeout_add_seconds(60, lambda: (self._check_all_statuses(), True)[1])

        # Surveillance batterie : alerte à 10 %, action critique à 5 %.
        self._bat_warned_low = False
        self._bat_warned_critical = False
        GLib.timeout_add_seconds(30, self._battery_watch_tick)
        # Premier check rapide au démarrage (one-shot, ne repropage pas).
        GLib.timeout_add(2000,
                         lambda: (self._battery_watch_tick(), False)[1])

    # ---- Surveillance batterie ----------------------------------------
    def _read_battery_state(self):
        try:
            base = "/sys/class/power_supply"
            if not os.path.isdir(base):
                return None, None
            for name in sorted(os.listdir(base)):
                if not name.startswith("BAT"):
                    continue
                cap_p = os.path.join(base, name, "capacity")
                st_p  = os.path.join(base, name, "status")
                if not os.path.isfile(cap_p):
                    continue
                with open(cap_p) as f:
                    pct = int(f.read().strip())
                status = ""
                if os.path.isfile(st_p):
                    with open(st_p) as f:
                        status = f.read().strip()
                return pct, status
        except (OSError, ValueError):
            pass
        return None, None

    def _battery_watch_tick(self):
        pct, status = self._read_battery_state()
        if pct is None:
            return True
        on_battery = ("charg" not in status.lower() or
                      status.lower().startswith("not"))
        # On ne déclenche les alertes que si on est sur batterie.
        if not on_battery or status.lower() == "full":
            # Reset des flags dès qu'on rebranche / charge.
            if pct >= 25:
                self._bat_warned_low = False
                self._bat_warned_critical = False
            return True

        # ---- 5 % : ACTION CRITIQUE ------------------------------------
        if pct <= 5 and not self._bat_warned_critical:
            self._bat_warned_critical = True
            self._bat_warned_low = True  # évite le double pop-up
            # 1) bascule en éco.
            try:
                if shutil.which("powerprofilesctl"):
                    subprocess.run(
                        ["powerprofilesctl", "set", "power-saver"],
                        check=False, timeout=3)
            except (subprocess.SubprocessError, OSError):
                pass
            # 2) luminosité à 30 % via logind.
            try:
                base = "/sys/class/backlight"
                if os.path.isdir(base):
                    entries = sorted(os.listdir(base))
                    if entries:
                        bl_name = entries[0]
                        with open(os.path.join(
                                base, bl_name, "max_brightness")) as f:
                            bmax = int(f.read().strip())
                        target = max(1, int(bmax * 0.30))
                        subprocess.run(
                            ["busctl", "call",
                             "org.freedesktop.login1",
                             "/org/freedesktop/login1/session/auto",
                             "org.freedesktop.login1.Session",
                             "SetBrightness", "ssu",
                             "backlight", bl_name, str(target)],
                            check=False, timeout=2,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
            except (subprocess.SubprocessError, OSError, ValueError):
                pass
            # 3) pop-up de mise en garde.
            self._battery_alert(
                level="critical",
                title="⚠  Batterie critique",
                msg=(f"Il ne reste que <b>{pct}%</b> de batterie.\n\n"
                     "L'ordinateur risque de s'éteindre prochainement.\n\n"
                     "Mode économie d'énergie activé et luminosité "
                     "abaissée à 30%. Branche le secteur dès que possible."))
            return True

        # ---- 10 % : avertissement -------------------------------------
        if pct <= 10 and not self._bat_warned_low:
            self._bat_warned_low = True
            self._battery_alert(
                level="warning",
                title="⚠  Batterie faible",
                msg=(f"Il reste <b>{pct}%</b> de batterie.\n\n"
                     "Pense à brancher l'ordinateur sur le secteur."))
        return True

    def _battery_alert(self, level, title, msg):
        """Pop-up modal grand format pour alerte batterie."""
        win = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
        win.set_title("Batterie")
        win.set_modal(True)
        win.set_default_size(560, 0)
        win.set_position(Gtk.WindowPosition.CENTER_ON_PARENT)
        try:
            win.set_transient_for(self)
        except Exception:
            pass
        win.set_keep_above(True)
        win.get_style_context().add_class("menu-popup")

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        outer.set_margin_start(24); outer.set_margin_end(24)
        outer.set_margin_top(20);   outer.set_margin_bottom(20)
        win.add(outer)

        t = Gtk.Label(xalign=0)
        t.set_markup(f"<span size='xx-large' weight='bold'>{title}</span>")
        cls = "kpi-bad" if level == "critical" else "kpi-warn"
        t.get_style_context().add_class(cls)
        outer.pack_start(t, False, False, 0)

        body = Gtk.Label(xalign=0)
        body.set_line_wrap(True)
        body.set_markup(msg)
        outer.pack_start(body, False, False, 0)

        btn = Gtk.Button(label="J'ai compris")
        btn.get_style_context().add_class("chip")
        if level == "critical":
            btn.get_style_context().add_class("chip-danger")
        btn.connect("clicked", lambda *_: win.destroy())
        outer.pack_start(btn, False, False, 6)

        # Petit son pour attirer l'attention.
        try:
            sound = ("dialog-warning" if level == "warning"
                     else "dialog-error")
            if shutil.which("canberra-gtk-play"):
                subprocess.Popen(
                    ["canberra-gtk-play", "-i", sound],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL)
        except OSError:
            pass

        win.show_all()
        win.present()

    # ---- CSS -----------------------------------------------------------
    def _apply_css(self):
        if not CSS_FILE.exists():
            return
        provider = Gtk.CssProvider()
        try:
            provider.load_from_path(str(CSS_FILE))
        except GLib.Error as e:
            print("CSS error:", e, file=sys.stderr)
            return
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_USER,
        )

    # ---- UI ------------------------------------------------------------
    def _build_ui(self):
        self._stack = Gtk.Stack()
        self._stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT)
        self._stack.set_transition_duration(520)
        self.add(self._stack)

        # --- Grid page (overlay for toast + animated background) ---
        overlay = Gtk.Overlay()
        self._stack.add_named(overlay, "grid")

        # Animated colorful background.
        self._bg = AnimatedBackground()
        overlay.add(self._bg)

        root_h = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        overlay.add_overlay(root_h)
        overlay.set_overlay_pass_through(root_h, False)

        root_h.pack_start(self._build_sidebar(), False, False, 0)
        root_h.pack_start(self._build_main(),    True,  True,  0)

        # Toast revealer
        self._toast_rev = Gtk.Revealer()
        self._toast_rev.set_transition_type(Gtk.RevealerTransitionType.SLIDE_UP)
        self._toast_rev.set_transition_duration(220)
        self._toast_rev.set_halign(Gtk.Align.CENTER)
        self._toast_rev.set_valign(Gtk.Align.END)
        self._toast_rev.set_margin_bottom(28)
        self._toast_lbl = Gtk.Label(label="")
        self._toast_lbl.get_style_context().add_class("toast")
        self._toast_rev.add(self._toast_lbl)
        overlay.add_overlay(self._toast_rev)

        # --- Console page ---
        self._console = ConsolePage(
            on_close=self._close_console,
            on_request_new=self._request_new_session,
        )
        self._stack.add_named(self._console, "console")

        # Global Escape grabber: works even when xfreerdp owns the keyboard.
        self._esc_grab = EscapeGrabber(self._on_global_escape)
        self._console._app_esc_regrab = self._esc_grab.regrab

        self._stack.set_visible_child_name("grid")

    # ---- Sidebar -------------------------------------------------------
    def _build_sidebar(self):
        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        side.get_style_context().add_class("sidebar")
        side.set_size_request(240, -1)
        side.set_hexpand(False)

        brand = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        dot = Gtk.Label(label="◆"); dot.get_style_context().add_class("brand-dot")
        dot.set_xalign(0.5); dot.set_yalign(0.5)
        name = Gtk.Label(label="V M S H E L L"); name.get_style_context().add_class("brand")
        name.set_xalign(0)
        brand.pack_start(dot, False, False, 0); brand.pack_start(name, False, False, 0)
        brand.set_margin_bottom(14); brand.set_margin_start(4)
        side.pack_start(brand, False, False, 0)

        def section(title):
            l = Gtk.Label(label=title.upper(), xalign=0)
            l.get_style_context().add_class("nav-section")
            side.pack_start(l, False, False, 0)

        def nav(key, label, badge=None):
            b = Gtk.Button()
            b.get_style_context().add_class("nav-item")
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            lab = Gtk.Label(label=label, xalign=0); lab.set_hexpand(True)
            row.pack_start(lab, True, True, 0)
            if badge is not None:
                bdg = Gtk.Label(label=str(badge))
                bdg.get_style_context().add_class("nav-badge")
                row.pack_end(bdg, False, False, 0)
            b.add(row)
            b.connect("clicked", lambda *_: self._set_nav(key))
            self._nav_buttons[key] = b
            side.pack_start(b, False, False, 0)
            return b

        total  = len(self._connections)
        favs   = sum(1 for c in self._connections if c.get("favorite"))

        section("Connexions")
        nav("all",        "Toutes les connexions", total)

        # spacer
        side.pack_start(Gtk.Label(), True, True, 0)

        cta = Gtk.Button(label="+  Nouvelle connexion")
        cta.get_style_context().add_class("nav-cta")
        cta.connect("clicked", lambda *_: self._on_add())
        side.pack_start(cta, False, False, 0)

        # Profile chip
        prof = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        prof.get_style_context().add_class("profile")
        user = os.environ.get("USER", "user")
        avatar = Gtk.Label(label=user[:2].upper())
        avatar.get_style_context().add_class("profile-avatar")
        avatar.set_xalign(0.5); avatar.set_yalign(0.5)
        names = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        n_lab = Gtk.Label(label=user.capitalize(), xalign=0); n_lab.get_style_context().add_class("profile-name")
        r_lab = Gtk.Label(label="Administrateur",  xalign=0); r_lab.get_style_context().add_class("profile-role")
        names.pack_start(n_lab, False, False, 0); names.pack_start(r_lab, False, False, 0)
        prof.pack_start(avatar, False, False, 0); prof.pack_start(names, True, True, 0)
        side.pack_start(prof, False, False, 0)

        return side

    # ---- Main panel ----------------------------------------------------
    def _build_main(self):
        main = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        main.set_margin_top(22); main.set_margin_bottom(16)
        main.set_margin_start(24); main.set_margin_end(24)

        # Header
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        user = os.environ.get("USER", "")
        self._greet = Gtk.Label(
            label=f"{greeting_for_hour(datetime.now().hour)}, {user}",
            xalign=0)
        self._greet.get_style_context().add_class("greeting")
        sub = Gtk.Label(label="Prêt à vous connecter ?", xalign=0)
        sub.get_style_context().add_class("subtitle")
        left.pack_start(self._greet, False, False, 0)
        left.pack_start(sub, False, False, 0)
        head.pack_start(left, False, False, 0)

        # Search center
        search_wrap = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        search_wrap.set_halign(Gtk.Align.CENTER); search_wrap.set_hexpand(True)
        self._search_entry = Gtk.SearchEntry()
        self._search_entry.get_style_context().add_class("search")
        self._search_entry.set_placeholder_text("Rechercher une connexion…")
        self._search_entry.set_size_request(420, -1)
        self._search_entry.connect("search-changed", self._on_search)
        kbd = Gtk.Label(label="Ctrl  K")
        kbd.get_style_context().add_class("kbd")
        kbd.set_tooltip_text("Raccourci : Ctrl+K pour rechercher")
        search_wrap.pack_start(self._search_entry, False, False, 0)
        search_wrap.pack_start(kbd, False, False, 0)
        head.pack_start(search_wrap, True, True, 0)

        # Right: clock + new session chip
        self._clock = Gtk.Label(label="")
        self._clock.get_style_context().add_class("clock")
        head.pack_start(self._clock, False, False, 0)

        # "Reprendre session" chip — visible only if sessions are open.
        self._resume_btn = Gtk.Button(label="↩  Reprendre session")
        self._resume_btn.get_style_context().add_class("chip")
        self._resume_btn.get_style_context().add_class("chip-primary")
        self._resume_btn.connect("clicked", lambda *_: self._resume_console())
        self._resume_btn.set_no_show_all(True)
        head.pack_start(self._resume_btn, False, False, 0)

        logout_btn = Gtk.Button(label="↪  Déconnexion")
        logout_btn.get_style_context().add_class("chip")
        logout_btn.connect("clicked", lambda *_: self._on_logout())

        shutdown_btn = Gtk.Button(label="⏼  Éteindre")
        shutdown_btn.get_style_context().add_class("chip")
        shutdown_btn.get_style_context().add_class("chip-danger")
        shutdown_btn.connect("clicked", lambda *_: self._on_shutdown())

        head.pack_start(logout_btn, False, False, 0)
        head.pack_start(shutdown_btn, False, False, 0)

        main.pack_start(head, False, False, 0)
        self._tick_clock()

        # KPI row
        self._kpi_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self._kpi_row.set_margin_top(20)
        main.pack_start(self._kpi_row, False, False, 0)

        # Section header
        sec = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        sec.set_margin_top(20); sec.set_margin_bottom(10)
        sec_l = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self._sec_title = Gtk.Label(label="Vos connexions", xalign=0)
        self._sec_title.get_style_context().add_class("section-title")
        self._sec_sub = Gtk.Label(label="Sélectionnez une connexion pour démarrer", xalign=0)
        self._sec_sub.get_style_context().add_class("section-sub")
        sec_l.pack_start(self._sec_title, False, False, 0)
        sec_l.pack_start(self._sec_sub,   False, False, 0)
        sec.pack_start(sec_l, True, True, 0)

        self._toggle_grid = Gtk.Button(label="▦"); self._toggle_grid.get_style_context().add_class("view-toggle")
        self._toggle_list = Gtk.Button(label="≡"); self._toggle_list.get_style_context().add_class("view-toggle")
        self._toggle_grid.connect("clicked", lambda *_: self._set_view("grid"))
        self._toggle_list.connect("clicked", lambda *_: self._set_view("list"))
        toggles = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        toggles.pack_start(self._toggle_grid, False, False, 0)
        toggles.pack_start(self._toggle_list, False, False, 0)
        sec.pack_end(toggles, False, False, 0)
        main.pack_start(sec, False, False, 0)

        # Scrollable content area
        self._scroller = Gtk.ScrolledWindow()
        self._scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._scroller.set_hexpand(True); self._scroller.set_vexpand(True)
        self._content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self._scroller.add(self._content)
        main.pack_start(self._scroller, True, True, 0)

        # Bouton "État système" en bas à gauche.
        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        footer.set_margin_top(6)
        self._status_btn = Gtk.Button()
        self._status_btn.get_style_context().add_class("chip")
        self._status_btn.set_relief(Gtk.ReliefStyle.NONE)
        self._refresh_status_button_label()
        self._status_btn.connect("clicked",
                                 lambda *_: self._open_status_dialog())
        footer.pack_start(self._status_btn, False, False, 0)
        # Espace flexible pour que le bouton reste collé à gauche.
        footer.pack_start(Gtk.Box(), True, True, 0)
        main.pack_start(footer, False, False, 0)

        return main

    # ---- État système (diagnostic) -------------------------------------
    def _refresh_status_button_label(self):
        try:
            d = DIAG
        except NameError:
            self._status_btn.set_label("●  État système")
            self._status_btn.show()
            return
        if d["fail"]:
            icon, cls = "✗", "chip-danger"
            txt = f"{icon}  État : {d['fail']} erreur(s)"
            persistent = True
        elif d["warn"]:
            icon, cls = "⚠", "chip-warn"
            txt = f"{icon}  État : {d['warn']} avertissement(s)"
            persistent = True
        else:
            icon, cls = "✓", "chip-good"
            txt = f"{icon}  État : tout est OK ({d['ok']})"
            persistent = False  # disparaîtra au bout de 12 s
        self._status_btn.set_label(txt)
        ctx = self._status_btn.get_style_context()
        for c in ("chip-good", "chip-warn", "chip-danger"):
            ctx.remove_class(c)
        ctx.add_class(cls)
        # Affiche le bouton, et programme un auto-masquage si tout va bien.
        self._status_btn.show()
        # Annule un timer précédent si on rafraîchit avant l'expiration.
        prev = getattr(self, "_status_hide_timer", 0)
        if prev:
            try: GLib.source_remove(prev)
            except Exception: pass
            self._status_hide_timer = 0
        if not persistent:
            def _hide_status():
                try:
                    self._status_btn.hide()
                except Exception:
                    pass
                self._status_hide_timer = 0
                return False  # one-shot
            self._status_hide_timer = GLib.timeout_add_seconds(
                12, _hide_status)

    def _open_status_dialog(self):
        dlg = Gtk.Dialog(title="État système & journaux",
                         transient_for=self, modal=True)
        dlg.set_default_size(820, 580)
        dlg.add_button("Fermer", Gtk.ResponseType.CLOSE)

        # Bouton Re-vérifier dans la barre d'action.
        recheck_btn = dlg.add_button("↻ Re-vérifier",
                                     Gtk.ResponseType.APPLY)
        recheck_btn.get_style_context().add_class("chip")

        notebook = Gtk.Notebook()
        dlg.get_content_area().pack_start(notebook, True, True, 0)
        dlg.get_content_area().set_margin_top(8)
        dlg.get_content_area().set_margin_bottom(8)
        dlg.get_content_area().set_margin_start(10)
        dlg.get_content_area().set_margin_end(10)

        def _make_tab(title, build_fn):
            sc = Gtk.ScrolledWindow()
            sc.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
            sc.set_vexpand(True); sc.set_hexpand(True)
            inner = build_fn()
            sc.add(inner)
            notebook.append_page(sc, Gtk.Label(label=title))
            return sc

        # ---- Onglet 1 : Diagnostic ------------------------------------
        def build_diag():
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            box.set_margin_top(10); box.set_margin_bottom(10)
            box.set_margin_start(12); box.set_margin_end(12)
            try:
                d = DIAG
            except NameError:
                d = {"results": [], "ok": 0, "warn": 0, "fail": 0}
            recap = Gtk.Label(xalign=0)
            recap.set_markup(
                f"<b>{d['ok']}</b> ✅   ·   "
                f"<b>{d['warn']}</b> ⚠   ·   "
                f"<b>{d['fail']}</b> ✗")
            recap.get_style_context().add_class("greeting")
            box.pack_start(recap, False, False, 4)

            for level, label_, detail in d["results"]:
                row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                              spacing=10)
                ico = {"OK": "✅", "WARN": "⚠", "FAIL": "✗"}[level]
                cls = {"OK": "kpi-good", "WARN": "kpi-warn",
                       "FAIL": "kpi-bad"}[level]
                il = Gtk.Label(label=ico)
                il.get_style_context().add_class(cls)
                row.pack_start(il, False, False, 0)
                lab = Gtk.Label(xalign=0)
                lab.set_line_wrap(True)
                txt = f"<b>{GLib.markup_escape_text(label_)}</b>"
                if detail:
                    txt += f"   <span alpha='65%'>· {GLib.markup_escape_text(detail)}</span>"
                lab.set_markup(txt)
                row.pack_start(lab, True, True, 0)
                box.pack_start(row, False, False, 0)

            if not d["results"]:
                empty = Gtk.Label(
                    label="Aucun diagnostic disponible.",
                    xalign=0)
                box.pack_start(empty, False, False, 0)

            # ---- Correctifs proposés (pilotes / paquets manquants) ----
            fixes = d.get("fixes", [])
            if fixes:
                sep = Gtk.Separator(
                    orientation=Gtk.Orientation.HORIZONTAL)
                sep.set_margin_top(10); sep.set_margin_bottom(6)
                box.pack_start(sep, False, False, 0)

                title = Gtk.Label(xalign=0)
                title.set_markup(
                    "<b>🔧 Correctifs proposés</b>"
                    "   <span alpha='65%'>· cliquez pour ouvrir un "
                    "terminal et lancer l'installation</span>")
                box.pack_start(title, False, False, 2)

                pm_now, _pm_exe = _detect_pm()

                def _make_run_cb(fix_):
                    def _cb(_btn):
                        cmd, pm = build_fix_command(fix_)
                        if not cmd:
                            md = Gtk.MessageDialog(
                                transient_for=dlg, modal=True,
                                message_type=Gtk.MessageType.WARNING,
                                buttons=Gtk.ButtonsType.OK,
                                text="Gestionnaire de paquets non détecté")
                            md.format_secondary_text(
                                "Aucun apt/dnf/pacman trouvé sur ce "
                                "système — installation manuelle requise.")
                            md.run(); md.destroy(); return
                        ok = open_terminal(
                            cmd, title=f"Installation : {fix_['title']}")
                        if not ok:
                            md = Gtk.MessageDialog(
                                transient_for=dlg, modal=True,
                                message_type=Gtk.MessageType.ERROR,
                                buttons=Gtk.ButtonsType.OK,
                                text="Aucun terminal disponible")
                            md.format_secondary_text(
                                "Lancez manuellement :\n\n" + cmd)
                            md.run(); md.destroy()
                    return _cb

                for fix in fixes:
                    pkgs_for_pm = fix["pkgs"].get(pm_now or "", [])
                    row = Gtk.Box(
                        orientation=Gtk.Orientation.HORIZONTAL,
                        spacing=10)
                    row.set_margin_top(2); row.set_margin_bottom(2)

                    lab = Gtk.Label(xalign=0)
                    lab.set_line_wrap(True)
                    txt = (f"<b>{GLib.markup_escape_text(fix['title'])}</b>")
                    if pkgs_for_pm:
                        txt += ("\n<small><span alpha='70%'>"
                                + GLib.markup_escape_text(
                                    " ".join(pkgs_for_pm))
                                + "</span></small>")
                    if fix.get("note"):
                        txt += ("\n<small><i>"
                                + GLib.markup_escape_text(fix['note'])
                                + "</i></small>")
                    lab.set_markup(txt)
                    row.pack_start(lab, True, True, 0)

                    btn = Gtk.Button(label="⬇ Installer")
                    btn.get_style_context().add_class("chip")
                    btn.get_style_context().add_class("chip-warn")
                    if not pkgs_for_pm or not pm_now:
                        btn.set_sensitive(False)
                        btn.set_tooltip_text(
                            "Aucun paquet défini pour ce gestionnaire.")
                    else:
                        btn.connect("clicked", _make_run_cb(fix))
                    row.pack_start(btn, False, False, 0)
                    box.pack_start(row, False, False, 0)
            return box

        # ---- Helper générique : afficher un fichier journal -----------
        def _make_log_view(path, empty_msg):
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            box.set_margin_top(10); box.set_margin_bottom(10)
            box.set_margin_start(12); box.set_margin_end(12)
            head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                           spacing=8)
            ph = Gtk.Label(xalign=0)
            ph.set_markup(f"<small>{GLib.markup_escape_text(str(path))}</small>")
            head.pack_start(ph, True, True, 0)

            def _on_clear(_btn):
                try:
                    open(path, "w").close()
                    tv.get_buffer().set_text("(journal vidé)")
                except OSError as e:
                    tv.get_buffer().set_text(f"Erreur : {e}")

            def _on_open_dir(_btn):
                try:
                    subprocess.Popen(["xdg-open",
                                      str(Path(path).parent)])
                except OSError:
                    pass

            clr = Gtk.Button(label="Vider")
            clr.get_style_context().add_class("chip")
            clr.connect("clicked", _on_clear)
            opn = Gtk.Button(label="Ouvrir le dossier")
            opn.get_style_context().add_class("chip")
            opn.connect("clicked", _on_open_dir)
            head.pack_start(clr, False, False, 0)
            head.pack_start(opn, False, False, 0)
            box.pack_start(head, False, False, 0)

            tv = Gtk.TextView()
            tv.set_editable(False)
            tv.set_monospace(True)
            tv.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
            buf = tv.get_buffer()
            try:
                if os.path.exists(path) and os.path.getsize(path):
                    with open(path, "r", encoding="utf-8",
                              errors="replace") as fh:
                        # Limite à ~256 Ko pour rester rapide.
                        fh.seek(0, os.SEEK_END)
                        size = fh.tell()
                        fh.seek(max(0, size - 256_000))
                        if size > 256_000:
                            buf.set_text("… (début tronqué) …\n"
                                         + fh.read())
                        else:
                            buf.set_text(fh.read())
                else:
                    buf.set_text(empty_msg)
            except OSError as e:
                buf.set_text(f"Erreur de lecture : {e}")

            sc = Gtk.ScrolledWindow()
            sc.set_policy(Gtk.PolicyType.AUTOMATIC,
                          Gtk.PolicyType.AUTOMATIC)
            sc.set_vexpand(True); sc.set_hexpand(True)
            sc.add(tv)
            box.pack_start(sc, True, True, 0)
            return box

        # ---- Onglet 2 : Sessions --------------------------------------
        sessions_log = CONFIG_DIR / "sessions.log"
        # ---- Onglet 3 : Crashes ---------------------------------------
        crash_log = CONFIG_DIR / "crash.log"

        _make_tab("🩺  Diagnostic", build_diag)
        _make_tab("📜  Sessions",
                  lambda: _make_log_view(sessions_log,
                                         "Aucune session enregistrée."))
        _make_tab("⚠  Crashes",
                  lambda: _make_log_view(crash_log,
                                         "Aucun crash enregistré. 🎉"))

        dlg.show_all()

        # Boucle interactive : re-vérifier sans fermer.
        while True:
            resp = dlg.run()
            if resp == Gtk.ResponseType.APPLY:
                try:
                    run_self_check()
                except Exception as e:
                    print(f"[vmshell] re-check erreur : {e}", flush=True)
                # Reconstruit le contenu de l'onglet diagnostic.
                page = notebook.get_nth_page(0)
                child = page.get_child()  # viewport
                if child is not None:
                    page.remove(child)
                page.add(build_diag())
                page.show_all()
                self._refresh_status_button_label()
                continue
            break
        dlg.destroy()

    # ---- Render --------------------------------------------------------
    def _render(self):
        self._render_kpis()
        self._render_nav_active()
        self._render_view_toggles()
        # Toggle "Reprendre session" chip based on console state.
        if hasattr(self, "_resume_btn") and hasattr(self, "_console"):
            if self._console.has_sessions():
                self._resume_btn.show()
            else:
                self._resume_btn.hide()
        for c in self._content.get_children():
            self._content.remove(c)

        items = self._visible_connections()

        if self._nav_mode == "settings":
            self._content.pack_start(self._build_settings_panel(), False, False, 0)
            self._sec_title.set_text("Paramètres")
            self._sec_sub.set_text("Préférences locales")
            self._content.show_all(); return

        if self._nav_mode == "history":
            self._sec_title.set_text("Historique")
            self._sec_sub.set_text("Bientôt disponible")
            self._content.pack_start(self._build_empty(
                "🕒", "Pas d'historique", "Vos connexions récentes apparaîtront ici."),
                False, False, 0)
            self._content.show_all(); return

        if self._nav_mode == "favorites":
            self._sec_title.set_text("Favoris")
            self._sec_sub.set_text("Vos accès rapides")
        else:
            self._sec_title.set_text("Vos connexions")
            self._sec_sub.set_text("Sélectionnez une connexion pour démarrer")

        if not items:
            self._content.pack_start(self._build_empty(
                "🖥",
                "Aucune connexion" if not self._filter else "Aucun résultat",
                "Cliquez sur «  Nouvelle connexion » pour commencer."
                if not self._filter else "Affinez votre recherche."),
                False, False, 0)
        elif self._view_mode == "grid":
            flow = Gtk.FlowBox()
            flow.set_valign(Gtk.Align.START)
            flow.set_halign(Gtk.Align.START)
            flow.set_max_children_per_line(6)
            flow.set_selection_mode(Gtk.SelectionMode.NONE)
            flow.set_homogeneous(False)
            flow.set_row_spacing(14); flow.set_column_spacing(14)
            for conn in items:
                flow.add(self._build_card(conn))
            self._content.pack_start(flow, False, False, 0)
        else:
            for conn in items:
                self._content.pack_start(self._build_row(conn), False, False, 0)

        self._content.show_all()

    def _render_nav_active(self):
        for k, btn in self._nav_buttons.items():
            ctx = btn.get_style_context()
            if k == self._nav_mode:
                ctx.add_class("nav-item-active")
            else:
                ctx.remove_class("nav-item-active")

    def _render_view_toggles(self):
        for k, btn in (("grid", self._toggle_grid), ("list", self._toggle_list)):
            ctx = btn.get_style_context()
            if k == self._view_mode:
                ctx.add_class("active")
            else:
                ctx.remove_class("active")

    def _render_kpis(self):
        for c in self._kpi_row.get_children():
            self._kpi_row.remove(c)
        total = len(self._connections)
        active = sum(1 for sid, st in self._status.items() if st == "connecting")
        online = sum(1 for sid, st in self._status.items() if st == "online")
        avail = int((online / total) * 100) if total else 0
        cards = [
            ("🖥", str(total), "Connexions", "Total configurées", "kpi-blue"),
            ("📈", str(active),"Sessions",   "En cours",          "kpi-violet"),
            ("✓",  f"{avail}%","Disponibilité","Systèmes en ligne","kpi-green"),
        ]
        for ico, val, title, sub, klass in cards:
            self._kpi_row.pack_start(self._build_kpi(ico, val, title, sub, klass),
                                     True, True, 0)
        self._kpi_row.show_all()

    # ---- Filtering -----------------------------------------------------
    def _visible_connections(self):
        out = []
        f = self._filter.lower()
        for c in self._connections:
            if self._nav_mode == "favorites" and not c.get("favorite"):
                continue
            if f and f not in (c["name"] + " " + c["host"] + " " + c.get("user","")
                               + " " + c.get("group","")).lower():
                continue
            out.append(c)
        return out

    # ---- Builders ------------------------------------------------------
    def _build_kpi(self, ico, val, title, sub, klass):
        card = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        card.get_style_context().add_class("kpi-card")
        card.get_style_context().add_class(klass)
        i = Gtk.Label(label=ico); i.get_style_context().add_class("kpi-icon")
        i.set_xalign(0.5); i.set_yalign(0.5)
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        v = Gtk.Label(label=val,   xalign=0); v.get_style_context().add_class("kpi-value")
        t = Gtk.Label(label=title, xalign=0); t.get_style_context().add_class("kpi-title")
        s = Gtk.Label(label=sub,   xalign=0); s.get_style_context().add_class("kpi-sub")
        col.pack_start(v, False, False, 0); col.pack_start(t, False, False, 0); col.pack_start(s, False, False, 0)
        card.pack_start(i, False, False, 0); card.pack_start(col, True, True, 0)
        return card

    def _build_card(self, conn):
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        card.get_style_context().add_class("vm-card")
        card.set_size_request(260, 220)

        # top row
        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        ico = Gtk.Label(label=os_glyph(conn.get("os")))
        ico.get_style_context().add_class("vm-icon")
        if conn["protocol"] == "ssh":
            ico.get_style_context().add_class("vm-icon-ssh")
        else:
            os_class = (conn.get("os") or "").lower()
            if os_class in ("windows", "linux", "macos"):
                ico.get_style_context().add_class(f"vm-icon-{os_class}")
        ico.set_xalign(0.5); ico.set_yalign(0.5)
        top.pack_start(ico, False, False, 0)
        top.pack_start(Gtk.Label(), True, True, 0)

        star = Gtk.Button(label="★" if conn.get("favorite") else "☆")
        star.get_style_context().add_class("star")
        if conn.get("favorite"):
            star.get_style_context().add_class("star-on")
        star.connect("clicked", lambda *_: self._toggle_fav(conn))
        top.pack_start(star, False, False, 0)

        ptxt, pclass = proto_pill(conn["protocol"])
        pill = Gtk.Label(label=ptxt); pill.get_style_context().add_class("pill"); pill.get_style_context().add_class(pclass)
        top.pack_start(pill, False, False, 0)

        kebab = Gtk.Button(label="⋯")
        kebab.get_style_context().add_class("chip-icon")
        kebab.get_style_context().add_class("kebab")
        kebab.connect("clicked", lambda b: self._open_kebab(b, conn))
        top.pack_start(kebab, False, False, 0)
        card.pack_start(top, False, False, 0)

        # name + group
        name_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        name = Gtk.Label(label=conn["name"], xalign=0)
        name.get_style_context().add_class("vm-name")
        name.set_ellipsize(Pango.EllipsizeMode.END)
        name_row.pack_start(name, True, True, 0)
        if conn.get("group"):
            tag = Gtk.Label(label=conn["group"]); tag.get_style_context().add_class("tag")
            name_row.pack_end(tag, False, False, 0)
        card.pack_start(name_row, False, False, 0)

        meta_user = Gtk.Label(label=f"👤  {conn['user'] or '—'}@{conn['host']}:{conn['port']}",
                              xalign=0)
        meta_user.get_style_context().add_class("vm-meta")
        meta_user.set_ellipsize(Pango.EllipsizeMode.END)
        card.pack_start(meta_user, False, False, 0)

        # status row
        st = self._status.get(conn["id"], "checking")
        if conn.get("maintenance"):
            st = "maintenance"
        s_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        dot = Gtk.Label(); dot.get_style_context().add_class("status-dot")
        dot.get_style_context().add_class(f"status-{st}")
        dot.set_size_request(8, 8)
        txt = Gtk.Label(label=self._status_label(st), xalign=0)
        txt.get_style_context().add_class("status-text")
        txt.get_style_context().add_class(f"status-{st}")
        s_row.pack_start(dot, False, False, 0); s_row.pack_start(txt, False, False, 0)
        card.pack_start(s_row, False, False, 0)

        card.pack_start(Gtk.Label(), True, True, 0)

        cta = Gtk.Button(label="Se connecter  →")
        cta.get_style_context().add_class("card-cta")
        cta.connect("clicked", lambda *_: self._connect(conn))
        card.pack_end(cta, False, False, 0)

        # whole card click to connect
        ev = Gtk.EventBox(); ev.add(card)
        ev.set_above_child(False)
        ev.connect("button-press-event", lambda w, e: self._on_card_click(e, conn))
        return ev

    def _on_card_click(self, e, conn):
        if e.type == Gdk.EventType._2BUTTON_PRESS:
            self._connect(conn)
        return False

    def _build_row(self, conn):
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        row.get_style_context().add_class("vm-row")

        ico = Gtk.Label(label=os_glyph(conn.get("os"))); ico.get_style_context().add_class("vm-icon")
        if conn["protocol"] == "ssh":
            ico.get_style_context().add_class("vm-icon-ssh")
        else:
            os_class = (conn.get("os") or "").lower()
            if os_class in ("windows", "linux", "macos"):
                ico.get_style_context().add_class(f"vm-icon-{os_class}")
        row.pack_start(ico, False, False, 0)

        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        n = Gtk.Label(label=conn["name"], xalign=0); n.get_style_context().add_class("vm-name")
        m = Gtk.Label(label=f"{conn['user'] or '—'}@{conn['host']}:{conn['port']}", xalign=0)
        m.get_style_context().add_class("vm-meta")
        col.pack_start(n, False, False, 0); col.pack_start(m, False, False, 0)
        row.pack_start(col, True, True, 0)

        ptxt, pclass = proto_pill(conn["protocol"])
        pill = Gtk.Label(label=ptxt); pill.get_style_context().add_class("pill"); pill.get_style_context().add_class(pclass)
        row.pack_start(pill, False, False, 0)

        st = "maintenance" if conn.get("maintenance") else self._status.get(conn["id"], "checking")
        dot = Gtk.Label(); dot.get_style_context().add_class("status-dot")
        dot.get_style_context().add_class(f"status-{st}")
        dot.set_size_request(8, 8)
        row.pack_start(dot, False, False, 0)

        cta = Gtk.Button(label="Se connecter  →")
        cta.get_style_context().add_class("card-cta")
        cta.connect("clicked", lambda *_: self._connect(conn))
        row.pack_start(cta, False, False, 0)

        kebab = Gtk.Button(label="⋯")
        kebab.get_style_context().add_class("chip-icon"); kebab.get_style_context().add_class("kebab")
        kebab.connect("clicked", lambda b: self._open_kebab(b, conn))
        row.pack_start(kebab, False, False, 0)
        return row

    def _build_empty(self, icon, title, sub):
        b = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        b.set_valign(Gtk.Align.CENTER); b.set_halign(Gtk.Align.CENTER)
        b.set_margin_top(80)
        i = Gtk.Label(label=icon); i.get_style_context().add_class("empty-icon")
        t = Gtk.Label(label=title); t.get_style_context().add_class("empty-title")
        s = Gtk.Label(label=sub);   s.get_style_context().add_class("empty-sub")
        for w in (i, t, s):
            b.pack_start(w, False, False, 0)
        return b

    def _build_settings_panel(self):
        b = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        b.get_style_context().add_class("form-card")
        b.set_margin_top(8)

        title = Gtk.Label(label="Préférences", xalign=0)
        title.get_style_context().add_class("form-title")
        sub = Gtk.Label(label=f"Configuration enregistrée dans {CONFIG_DIR}", xalign=0)
        sub.get_style_context().add_class("form-sub")
        b.pack_start(title, False, False, 0)
        b.pack_start(sub,   False, False, 0)

        info = Gtk.Label(
            label=(f"• xfreerdp : {find_xfreerdp() or 'NON INSTALLÉ'}\n"
                   f"• xdotool  : {shutil.which('xdotool') or 'NON INSTALLÉ'}\n"
                   f"• Connexions : {len(self._connections)}"),
            xalign=0)
        info.get_style_context().add_class("vm-meta")
        b.pack_start(info, False, False, 0)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        quit_btn = Gtk.Button(label="Quitter VMShell")
        quit_btn.get_style_context().add_class("chip"); quit_btn.get_style_context().add_class("chip-danger")
        quit_btn.connect("clicked", lambda *_: Gtk.main_quit())
        row.pack_start(quit_btn, False, False, 0)
        b.pack_start(row, False, False, 0)
        return b

    # ---- Status helpers ------------------------------------------------
    def _status_label(self, st):
        return {
            "online":      "En ligne",
            "offline":     "Hors ligne",
            "checking":    "Vérification…",
            "connecting":  "Connecté",
            "maintenance": "Maintenance",
        }.get(st, "Inconnu")

    def _check_all_statuses(self):
        for c in list(self._connections):
            self._status.setdefault(c["id"], "checking")
            t = threading.Thread(target=self._probe, args=(c,), daemon=True)
            t.start()

    def _probe(self, conn):
        try:
            with socket.create_connection((conn["host"], int(conn["port"])), timeout=1.5):
                st = "online"
        except OSError:
            st = "offline"
        GLib.idle_add(self._set_status, conn["id"], st)

    def _set_status(self, cid, st):
        # Don't override an active session.
        if self._status.get(cid) == "connecting":
            return False
        self._status[cid] = st
        self._render_kpis()
        # Avoid full re-render when only dots change in current view.
        for child in self._content.get_children():
            self._content.remove(child)
        self._render()
        return False

    # ---- Actions -------------------------------------------------------
    def _set_nav(self, mode):
        self._nav_mode = mode
        self._render()

    def _set_view(self, view):
        self._view_mode = view
        self._settings["view_mode"] = view
        save_json_atomic(SETTINGS_FILE, self._settings)
        self._render()

    def _on_quit(self):
        d = Gtk.MessageDialog(transient_for=self, modal=True,
                              message_type=Gtk.MessageType.QUESTION,
                              buttons=Gtk.ButtonsType.YES_NO,
                              text="Quitter VMShell ?")
        d.set_keep_above(True)
        resp = d.run()
        d.destroy()
        if resp == Gtk.ResponseType.YES:
            Gtk.main_quit()

    def _confirm(self, text):
        d = Gtk.MessageDialog(transient_for=self, modal=True,
                              message_type=Gtk.MessageType.QUESTION,
                              buttons=Gtk.ButtonsType.YES_NO,
                              text=text)
        d.set_keep_above(True)
        resp = d.run()
        d.destroy()
        return resp == Gtk.ResponseType.YES

    def _on_logout(self):
        if not self._confirm("Se déconnecter de la session ?"):
            return
        # Tente plusieurs méthodes selon l'environnement.
        for cmd in (["loginctl", "terminate-user", os.environ.get("USER", "")],
                    ["gnome-session-quit", "--logout", "--no-prompt"],
                    ["cinnamon-session-quit", "--logout", "--no-prompt"],
                    ["pkill", "-KILL", "-u", os.environ.get("USER", "")]):
            try:
                subprocess.Popen(cmd)
                Gtk.main_quit()
                return
            except (OSError, FileNotFoundError):
                continue
        self._toast("Déconnexion impossible.")

    def _on_shutdown(self):
        if not self._confirm("Éteindre l'ordinateur ?"):
            return
        for cmd in (["systemctl", "poweroff"],
                    ["shutdown", "-h", "now"],
                    ["pkexec", "shutdown", "-h", "now"]):
            try:
                subprocess.Popen(cmd)
                return
            except (OSError, FileNotFoundError):
                continue
        self._toast("Extinction impossible.")

    def _on_search(self, entry):
        self._filter = entry.get_text()
        self._render()

    def _on_add(self):
        dlg = ConnectionDialog(self)
        dlg.present()
        if dlg.run() == Gtk.ResponseType.OK:
            self._connections.append(dlg.get_connection())
            self._save_conns()
            self._toast("Connexion ajoutée.")
            self._refresh_sidebar_badges()
            self._render()
            self._check_all_statuses()
        dlg.destroy()

    def _on_edit(self, conn):
        dlg = ConnectionDialog(self, conn)
        dlg.present()
        if dlg.run() == Gtk.ResponseType.OK:
            updated = dlg.get_connection()
            for i, c in enumerate(self._connections):
                if c["id"] == conn["id"]:
                    self._connections[i] = updated
                    break
            self._save_conns()
            self._toast("Connexion mise à jour.")
            self._refresh_sidebar_badges()
            self._render()
            self._check_all_statuses()
        dlg.destroy()

    def _duplicate(self, conn):
        c = dict(conn)
        c["id"] = uuid.uuid4().hex
        c["name"] = conn["name"] + " (copie)"
        self._connections.append(c)
        self._save_conns()
        self._toast("Connexion dupliquée.")
        self._refresh_sidebar_badges()
        self._render()

    def _delete(self, conn):
        d = Gtk.MessageDialog(transient_for=self, modal=True,
                              message_type=Gtk.MessageType.QUESTION,
                              buttons=Gtk.ButtonsType.YES_NO,
                              text=f"Supprimer « {conn['name']} » ?")
        if d.run() == Gtk.ResponseType.YES:
            self._connections = [c for c in self._connections if c["id"] != conn["id"]]
            self._status.pop(conn["id"], None)
            self._save_conns()
            self._toast("Connexion supprimée.")
            self._refresh_sidebar_badges()
            self._render()
        d.destroy()

    def _toggle_fav(self, conn):
        for c in self._connections:
            if c["id"] == conn["id"]:
                c["favorite"] = not c.get("favorite", False)
                break
        self._save_conns()
        self._refresh_sidebar_badges()
        self._render()

    def _connect(self, conn):
        self._status[conn["id"]] = "connecting"
        self._render_kpis()
        self._stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT)
        self._stack.set_visible_child_name("console")
        self._console.start(conn)
        self._esc_grab.start()

    def _on_global_escape(self):
        if self._stack.get_visible_child_name() == "console":
            self._console.toggle_menu()
        return False

    def _close_console(self):
        for cid in list(self._status.keys()):
            if self._status[cid] == "connecting":
                self._status[cid] = "checking"
        self._stack.set_transition_type(Gtk.StackTransitionType.SLIDE_RIGHT)
        self._stack.set_visible_child_name("grid")
        self._render()
        self._check_all_statuses()

    def _request_new_session(self):
        """Called from ConsolePage popup: go back to grid WITHOUT closing
        the existing sessions, so the user can pick another VM to add."""
        self._stack.set_transition_type(Gtk.StackTransitionType.SLIDE_RIGHT)
        self._stack.set_visible_child_name("grid")
        self._console._hide_floating_menu_button()
        self._render()

    def _resume_console(self):
        """Switch back to the console (existing sessions stay alive)."""
        if self._console.has_sessions():
            self._stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT)
            self._stack.set_visible_child_name("console")
            self._console._show_floating_menu_button()

    def _open_kebab(self, btn, conn):
        pop = Gtk.Popover.new(btn)
        pop.set_position(Gtk.PositionType.BOTTOM)
        b = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        for label, cb, danger in (
            ("Modifier",  lambda *_: self._on_edit(conn),     False),
            ("Dupliquer", lambda *_: self._duplicate(conn),   False),
            ("Supprimer", lambda *_: self._delete(conn),      True),
        ):
            it = Gtk.Button(label=label)
            it.get_style_context().add_class("menu-item")
            if danger:
                it.get_style_context().add_class("menu-item-danger")
            it.connect("clicked", lambda _w, _cb=cb: (pop.popdown(), _cb())[1])
            b.pack_start(it, False, False, 0)
        pop.add(b); b.show_all()
        pop.popup()

    # ---- Save / refresh -----------------------------------------------
    def _save_conns(self):
        save_json_atomic(CONNS_FILE, self._connections)

    def _refresh_sidebar_badges(self):
        # Find badge labels in nav buttons and update.
        total = len(self._connections)
        favs  = sum(1 for c in self._connections if c.get("favorite"))
        for key, count in (("all", total), ("favorites", favs)):
            btn = self._nav_buttons.get(key)
            if not btn:
                continue
            box = btn.get_child()
            for child in box.get_children():
                if isinstance(child, Gtk.Label) and child.get_xalign() == 1.0:
                    pass
            # Re-build content for simplicity.
            for c in box.get_children():
                box.remove(c)
            label = {"all": "Toutes les connexions", "favorites": "Favoris"}[key]
            lab = Gtk.Label(label=label, xalign=0); lab.set_hexpand(True)
            box.pack_start(lab, True, True, 0)
            bdg = Gtk.Label(label=str(count))
            bdg.get_style_context().add_class("nav-badge")
            box.pack_end(bdg, False, False, 0)
            box.show_all()

    # ---- Toast / clock / keys -----------------------------------------
    def _toast(self, msg):
        self._toast_lbl.set_text(msg)
        self._toast_rev.set_reveal_child(True)
        GLib.timeout_add(2200, lambda: (self._toast_rev.set_reveal_child(False), False)[1])

    def _tick_clock(self):
        if hasattr(self, "_clock"):
            now = datetime.now()
            days = ["lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim."]
            months = ["janv.", "févr.", "mars", "avr.", "mai", "juin",
                      "juil.", "août", "sept.", "oct.", "nov.", "déc."]
            txt = f"{days[now.weekday()]} {now.day} {months[now.month-1]}  ·  {now.strftime('%H:%M')}"
            self._clock.set_text(txt)
        return True

    def _on_key(self, _w, e):
        kv = e.keyval
        ctrl = bool(e.state & Gdk.ModifierType.CONTROL_MASK)
        shift = bool(e.state & Gdk.ModifierType.SHIFT_MASK)
        # Ctrl+Shift+V dans une session : injecte le presse-papier local.
        if (ctrl and shift and kv in (Gdk.KEY_V, Gdk.KEY_v)
                and self._stack.get_visible_child_name() == "console"
                and self._console.has_sessions()):
            self._console._paste_clipboard_into_vm()
            return True
        # Ctrl+K or Ctrl+F -> focus search
        if ctrl and kv in (Gdk.KEY_k, Gdk.KEY_f):
            if self._search_entry:
                self._search_entry.grab_focus()
            return True
        # Ctrl+N -> new connection
        if ctrl and kv == Gdk.KEY_n:
            self._on_add(); return True
        # Esc -> in console: open shortcuts menu (don't close session)
        if kv == Gdk.KEY_Escape and self._stack.get_visible_child_name() == "console":
            self._console.toggle_menu(); return True
        # F11 toggle fullscreen
        if kv == Gdk.KEY_F11:
            if self.is_active():
                self.unfullscreen() if self._is_fs() else self.fullscreen()
            return True
        # Ctrl+Q quit
        if e.state & Gdk.ModifierType.CONTROL_MASK and kv == Gdk.KEY_q:
            Gtk.main_quit(); return True
        return False

    def _is_fs(self):
        w = self.get_window()
        if not w:
            return True
        return bool(w.get_state() & Gdk.WindowState.FULLSCREEN)


# ---------------------------------------------------------------------------
# Auto-détection matérielle (GPU/CPU) — renseigné par tune_runtime() au démarrage.
HW_INFO = {
    "gpu_vendor": None,    # "nvidia" | "amd" | "intel" | None
    "gpu_accel":  False,   # True si le pilote est réellement utilisable
    "cpu_count":  os.cpu_count() or 2,
}


def _detect_gpu():
    """Return (vendor, accel_ok). Cherche un GPU exploitable.

    Étapes :
      1) lspci pour identifier le vendeur dominant
      2) vérifier qu'un /dev/dri/renderD* existe (Intel/AMD VAAPI)
         ou que `nvidia-smi` répond (NVIDIA propriétaire)
    En cas d'absence ou d'échec → (vendor_or_None, False) → fallback CPU.
    """
    vendor = None
    try:
        out = subprocess.check_output(
            ["lspci"], stderr=subprocess.DEVNULL, timeout=2
        ).decode(errors="ignore").lower()
    except Exception:
        out = ""

    # Lignes contenant "vga" / "3d" / "display" pour ne pas confondre
    # avec un chipset audio NVIDIA, par exemple.
    gpu_lines = [l for l in out.splitlines()
                 if any(k in l for k in (" vga ", "vga compatible",
                                         "3d controller", "display controller"))]
    blob = "\n".join(gpu_lines) or out
    if "nvidia" in blob:
        vendor = "nvidia"
    elif "amd" in blob or "radeon" in blob or "ati " in blob:
        vendor = "amd"
    elif "intel" in blob:
        vendor = "intel"

    accel = False
    if vendor == "nvidia":
        # Pilote propriétaire installé ?
        if shutil.which("nvidia-smi"):
            try:
                r = subprocess.run(["nvidia-smi", "-L"],
                                   stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, timeout=2)
                accel = (r.returncode == 0)
            except Exception:
                accel = False
        # Sinon : peut-être nouveau (open source) → présence d'un render node.
        if not accel:
            accel = any(os.path.exists(f"/dev/dri/renderD{128 + i}")
                        for i in range(4))
    elif vendor in ("amd", "intel"):
        # VAAPI passe par /dev/dri/renderD*
        accel = any(os.path.exists(f"/dev/dri/renderD{128 + i}")
                    for i in range(4))

    return vendor, accel


def tune_runtime():
    """Best-effort, user-space performance tuning at startup.

    Multi-PC : auto-détection GPU + fallback CPU si absent ou pilote KO.
    No sudo; safe to run on every launch.
    """
    tuned = []

    # 1) Process priority.
    try:
        os.nice(-5); tuned.append("nice -5")
    except OSError:
        try:
            os.nice(-1); tuned.append("nice -1")
        except OSError:
            pass
    try:
        subprocess.run(["ionice", "-c", "2", "-n", "0", "-p", str(os.getpid())],
                       check=False, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=2)
        tuned.append("ionice best-effort")
    except OSError:
        pass

    # 2) Détection GPU + variables d'environnement adaptées.
    vendor, accel = _detect_gpu()
    HW_INFO["gpu_vendor"] = vendor
    HW_INFO["gpu_accel"]  = accel

    if vendor == "nvidia" and accel:
        os.environ.setdefault("LIBVA_DRIVER_NAME", "nvidia")
        os.environ.setdefault("VDPAU_DRIVER", "nvidia")
        os.environ.setdefault("__GL_THREADED_OPTIMIZATIONS", "1")
        os.environ.setdefault("__GL_SYNC_TO_VBLANK", "0")
        os.environ.setdefault("__GL_YIELD", "USLEEP")
        tuned.append("GPU=NVIDIA(accel)")
    elif vendor == "amd" and accel:
        os.environ.setdefault("LIBVA_DRIVER_NAME", "radeonsi")
        os.environ.setdefault("VDPAU_DRIVER", "radeonsi")
        tuned.append("GPU=AMD(accel)")
    elif vendor == "intel" and accel:
        os.environ.setdefault("LIBVA_DRIVER_NAME", "iHD")
        os.environ.setdefault("VDPAU_DRIVER", "va_gl")
        tuned.append("GPU=Intel(accel)")
    else:
        # Pas de GPU exploitable → mode CPU.
        # Désactive toute tentative VAAPI/VDPAU (libs râlent sinon).
        os.environ.setdefault("LIBVA_DRIVER_NAME", "")
        os.environ.setdefault("VDPAU_DRIVER", "")
        os.environ.setdefault("LIBGL_ALWAYS_SOFTWARE", "0")
        # Active la parallélisation CPU des threads OpenGL.
        os.environ.setdefault("MESA_GLTHREAD", "true")
        # Limite l'overhead audio.
        tuned.append(f"GPU=aucun, CPU={HW_INFO['cpu_count']} cœurs")

    # Communs — sans VBLANK pour éviter le tearing fixe.
    os.environ.setdefault("MESA_GLTHREAD", "true")
    os.environ.setdefault("vblank_mode", "0")
    os.environ.setdefault("CLUTTER_VBLANK", "none")
    os.environ.setdefault("GDK_GL", "always")

    # 3) PulseAudio low-latency.
    os.environ.setdefault("PULSE_LATENCY_MSEC", "30")

    # 4) Cinnamon: désactiver l'unredirect du compositeur en plein écran.
    if shutil.which("gsettings"):
        try:
            subprocess.run(
                ["gsettings", "set", "org.cinnamon.muffin",
                 "unredirect-fullscreen-windows", "true"],
                check=False, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=2)
            tuned.append("muffin unredirect")
        except OSError:
            pass

    # 5) Clavier : répétition rapide.
    if shutil.which("xset"):
        try:
            subprocess.run(["xset", "r", "rate", "250", "30"],
                           check=False, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=2)
        except OSError:
            pass

    print(f"[vmshell] tune: {', '.join(tuned) if tuned else 'rien'}",
          flush=True)


# ---------------------------------------------------------------------------
# Auto-diagnostic au démarrage : vérifie les dépendances et l'environnement.
# Affiche un récap [OK] / [WARN] / [FAIL] et stocke le résultat dans
# DIAG pour affichage éventuel dans le panneau ESC.
DIAG = {"results": [], "fail": 0, "warn": 0, "ok": 0, "fixes": []}


def _diag_add(level, label, detail=""):
    """level: 'OK' | 'WARN' | 'FAIL'."""
    DIAG["results"].append((level, label, detail))
    if   level == "OK":   DIAG["ok"]   += 1
    elif level == "WARN": DIAG["warn"] += 1
    elif level == "FAIL": DIAG["fail"] += 1


def _diag_add_fix(fix_id, title, pkgs_per_pm, post_cmd=None, note=""):
    """Enregistre un correctif proposable à l'utilisateur.

    fix_id      : identifiant court (anti-doublons)
    title       : libellé affiché ("Pilotes NVIDIA", ...)
    pkgs_per_pm : {"apt":[...], "dnf":[...], "pacman":[...]}
    post_cmd    : commande shell additionnelle à exécuter (str), facultative
                  (ex.: activer un service)
    note        : texte explicatif court
    """
    if any(f["id"] == fix_id for f in DIAG["fixes"]):
        return
    DIAG["fixes"].append({
        "id": fix_id, "title": title,
        "pkgs": pkgs_per_pm, "post": post_cmd, "note": note,
    })


def run_self_check():
    """Vérifications systématiques au lancement. Ne bloque jamais : remonte
    juste des avertissements pour l'utilisateur."""
    DIAG["results"].clear()
    DIAG["fixes"].clear()
    DIAG["ok"] = DIAG["warn"] = DIAG["fail"] = 0

    # --- Système ----------------------------------------------------------
    sess = os.environ.get("XDG_SESSION_TYPE", "?")
    if sess == "x11":
        _diag_add("OK", "Session X11", sess)
    elif sess == "wayland":
        _diag_add("WARN", "Session Wayland",
                  "xfreerdp et la capture Escape fonctionnent mieux en X11.")
    else:
        _diag_add("WARN", "Session inconnue", sess or "(vide)")

    if os.environ.get("DISPLAY"):
        _diag_add("OK", "DISPLAY", os.environ["DISPLAY"])
    else:
        _diag_add("FAIL", "DISPLAY absent",
                  "Aucun serveur X détecté — l'affichage ne marchera pas.")

    # --- Audio (testé en premier pour éviter les faux warnings binaires) -
    audio_ok = False
    try:
        r = subprocess.run(["pactl", "info"], stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, timeout=2)
        if r.returncode == 0:
            audio_ok = True
    except (OSError, subprocess.TimeoutExpired):
        pass

    # --- Binaires requis --------------------------------------------------
    # Pour les optionnels : si un service équivalent fonctionne (ex.
    # pipewire-pulse fournit pulseaudio), on n'affiche aucun warning.
    REQ = [
        ("xfreerdp3 ou xfreerdp", find_xfreerdp(), True,  True),
        ("xdotool",   shutil.which("xdotool"),   True,  True),
        ("openbox",   shutil.which("openbox"),   False, True),
        ("xset",      shutil.which("xset"),      False, True),
        ("xsetroot",  shutil.which("xsetroot"),  False, True),
        ("ionice",    shutil.which("ionice"),    False, True),
        ("gsettings", shutil.which("gsettings"), False, True),
        ("ssh",       shutil.which("ssh"),       False, True),
        # pulseaudio : ne déclencher un warning QUE si l'audio ne marche pas.
        ("pulseaudio",shutil.which("pulseaudio"),False, not audio_ok),
    ]
    for name, path, required, show_if_missing in REQ:
        if path:
            _diag_add("OK", f"Binaire {name}", path)
        elif required:
            _diag_add("FAIL", f"Binaire {name} manquant", "indispensable")
        elif show_if_missing:
            _diag_add("WARN", f"Binaire {name} manquant", "optionnel")
        # sinon : silencieux (couvert par autre chose)

    # --- Modules Python ---------------------------------------------------
    try:
        import gi  # noqa: F401
        gi.require_version("Gtk", "3.0")
        gi.require_version("Vte", "2.91")
        from gi.repository import Gtk, Vte  # noqa: F401
        _diag_add("OK", "GTK 3 + VTE 2.91", "PyGObject")
    except Exception as e:
        _diag_add("FAIL", "GTK/VTE PyGObject", str(e))

    try:
        from Xlib import display as _xdisp  # noqa: F401
        _diag_add("OK", "python-xlib", "capture Escape disponible")
    except Exception as e:
        _diag_add("WARN", "python-xlib indisponible",
                  f"raccourci Escape global désactivé ({e})")

    # --- Audio (récap basé sur le test fait plus haut) -------------------
    if audio_ok:
        _diag_add("OK", "Serveur audio actif",
                  "PulseAudio/PipeWire — redirection son OK")
    else:
        _diag_add("WARN", "Serveur audio inactif",
                  "le son distant ne sera pas redirigé")

    # --- GPU / accélération vidéo ----------------------------------------
    v = HW_INFO.get("gpu_vendor")
    if v and HW_INFO.get("gpu_accel"):
        _diag_add("OK", f"GPU {v.upper()}", "accélération vidéo dispo")
    elif v:
        _diag_add("WARN", f"GPU {v.upper()} sans accélération",
                  "pilote/firmware manquant — fallback CPU")
        # Proposer l'installation du pilote correspondant.
        if v == "nvidia":
            _diag_add_fix(
                "drv-nvidia", "Pilotes NVIDIA + VAAPI/VDPAU",
                {"apt":   ["nvidia-driver", "libva-glx-nvidia",
                           "vdpau-driver-all"],
                 "dnf":   ["akmod-nvidia",
                           "xorg-x11-drv-nvidia-cuda",
                           "libva-utils"],
                 "pacman":["nvidia", "nvidia-utils", "libva-utils"]},
                note="Redémarrage requis après installation.")
        elif v == "amd":
            _diag_add_fix(
                "drv-amd", "Pilotes AMD (Mesa VAAPI/VDPAU)",
                {"apt":   ["mesa-va-drivers", "mesa-vdpau-drivers",
                           "libva-mesa-driver"],
                 "dnf":   ["mesa-va-drivers", "mesa-vdpau-drivers"],
                 "pacman":["libva-mesa-driver", "mesa-vdpau"]},
                note="Redémarrer la session graphique après installation.")
        elif v == "intel":
            _diag_add_fix(
                "drv-intel", "Pilote Intel iHD (VAAPI)",
                {"apt":   ["intel-media-va-driver-non-free",
                           "i965-va-driver", "vainfo"],
                 "dnf":   ["intel-media-driver", "libva-intel-driver",
                           "libva-utils"],
                 "pacman":["intel-media-driver", "libva-intel-driver",
                           "libva-utils"]},
                note="Redémarrer la session graphique après installation.")
    else:
        _diag_add("WARN", "Aucun GPU détecté",
                  f"mode CPU pur, {HW_INFO.get('cpu_count')} cœurs")

    # --- Réseau (résolution DNS basique) ---------------------------------
    try:
        socket.gethostbyname("www.google.com")
        _diag_add("OK", "Réseau", "DNS résout, internet accessible")
    except OSError as e:
        _diag_add("WARN", "Réseau",
                  f"DNS/internet indisponible ({e}) — RDP local OK")

    # --- USB redirection (informatif) ------------------------------------
    if os.path.isdir("/dev/bus/usb"):
        try:
            buses = os.listdir("/dev/bus/usb")
            _diag_add("OK", "Bus USB",
                      f"{len(buses)} contrôleur(s) — /usb:auto opérationnel")
        except OSError:
            _diag_add("WARN", "Bus USB illisible",
                      "permissions insuffisantes")
    else:
        _diag_add("WARN", "Pas de /dev/bus/usb",
                  "redirection USB indisponible")

    # --- Stockage : répertoire de config accessible ----------------------
    cfg_dir = os.path.dirname(SETTINGS_FILE)
    try:
        os.makedirs(cfg_dir, exist_ok=True)
        test = os.path.join(cfg_dir, ".write_test")
        with open(test, "w") as fh:
            fh.write("ok")
        os.remove(test)
        _diag_add("OK", "Config persistante", cfg_dir)
    except OSError as e:
        _diag_add("FAIL", "Config non inscriptible", f"{cfg_dir} : {e}")

    # --- Correctif global : binaires/modules manquants ------------------
    try:
        miss_pkgs, miss_pm = _missing_packages()
        if miss_pkgs and miss_pm:
            pm_map = {miss_pm: list(miss_pkgs)}
            _diag_add_fix(
                "deps-missing",
                f"Dépendances manquantes ({len(miss_pkgs)} paquet"
                f"{'s' if len(miss_pkgs) > 1 else ''})",
                pm_map,
                note="Binaires/modules détectés comme absents.")
    except Exception:
        pass

    # --- Affichage récap --------------------------------------------------
    print("[vmshell] === Auto-check ===", flush=True)
    for level, label, detail in DIAG["results"]:
        tag = {"OK": " OK ", "WARN": "WARN", "FAIL": "FAIL"}[level]
        suffix = f" — {detail}" if detail else ""
        print(f"[vmshell] [{tag}] {label}{suffix}", flush=True)
    print(f"[vmshell] === {DIAG['ok']} OK · "
          f"{DIAG['warn']} avertissements · "
          f"{DIAG['fail']} erreurs ===", flush=True)


# ---------------------------------------------------------------------------
# Auto-réparation : tente d'installer ce qui manque via le gestionnaire de
# paquets natif. Sans sudo on ne fait que lister ce qu'il faudrait.
#
# Mapping  binaire-attendu -> nom de paquet par distro.
_PKG_MAP = {
    "xfreerdp3": {"apt": "freerdp3-x11", "dnf": "freerdp",
                  "pacman": "freerdp"},
    "xdotool":   {"apt": "xdotool", "dnf": "xdotool", "pacman": "xdotool"},
    "openbox":   {"apt": "openbox", "dnf": "openbox", "pacman": "openbox"},
    "xset":      {"apt": "x11-xserver-utils", "dnf": "xset",
                  "pacman": "xorg-xset"},
    "xsetroot":  {"apt": "x11-xserver-utils", "dnf": "xsetroot",
                  "pacman": "xorg-xsetroot"},
    "ionice":    {"apt": "util-linux", "dnf": "util-linux",
                  "pacman": "util-linux"},
    "gsettings": {"apt": "libglib2.0-bin", "dnf": "glib2",
                  "pacman": "glib2"},
    "ssh":       {"apt": "openssh-client", "dnf": "openssh-clients",
                  "pacman": "openssh"},
    "pactl":     {"apt": "pulseaudio-utils", "dnf": "pulseaudio-utils",
                  "pacman": "libpulse"},
    "lspci":     {"apt": "pciutils", "dnf": "pciutils",
                  "pacman": "pciutils"},
}

# Modules Python -> paquet système (les pip-installs sont à éviter sur
# distro packagée).
_PYMOD_PKG = {
    "Xlib":   {"apt": "python3-xlib", "dnf": "python3-xlib",
               "pacman": "python-xlib"},
    "gi":     {"apt": "python3-gi", "dnf": "python3-gobject",
               "pacman": "python-gobject"},
}


def _detect_pm():
    for pm, exe in (("apt", "apt-get"), ("dnf", "dnf"), ("pacman", "pacman")):
        if shutil.which(exe):
            return pm, exe
    return None, None


def _missing_packages():
    """Renvoie la liste de paquets à installer pour les binaires/modules
    manquants, en fonction du gestionnaire de paquets détecté."""
    pm, _ = _detect_pm()
    if not pm:
        return [], None

    pkgs = set()

    # Binaires.
    for bin_name, table in _PKG_MAP.items():
        # Cas xfreerdp3 : accepter aussi xfreerdp seul.
        if bin_name == "xfreerdp3":
            if shutil.which("xfreerdp3") or shutil.which("xfreerdp"):
                continue
        elif shutil.which(bin_name):
            continue
        if pm in table:
            pkgs.add(table[pm])

    # Modules Python.
    for mod, table in _PYMOD_PKG.items():
        try:
            __import__(mod)
            continue
        except Exception:
            pass
        if pm in table:
            pkgs.add(table[pm])

    return sorted(pkgs), pm


def auto_install_missing(allow=True):
    """Installe les paquets manquants via le PM système. Nécessite sudo
    non-interactif (NOPASSWD) ou que l'on tourne déjà en root, sinon
    on se contente d'afficher la commande à lancer.
    Renvoie True si au moins un paquet a été (ré)installé.
    """
    if not allow:
        return False
    if os.environ.get("VMSHELL_SKIP_AUTOINSTALL") == "1":
        return False

    pkgs, pm = _missing_packages()
    if not pkgs:
        return False

    print(f"[vmshell] paquets manquants ({pm}): {' '.join(pkgs)}",
          flush=True)

    # Construire la commande d'install non-interactive.
    if pm == "apt":
        cmd = ["apt-get", "install", "-y", "--no-install-recommends", *pkgs]
        env_extra = {"DEBIAN_FRONTEND": "noninteractive"}
    elif pm == "dnf":
        cmd = ["dnf", "install", "-y", *pkgs]
        env_extra = {}
    elif pm == "pacman":
        cmd = ["pacman", "-S", "--noconfirm", "--needed", *pkgs]
        env_extra = {}
    else:
        return False

    is_root = (os.geteuid() == 0)
    has_sudo = bool(shutil.which("sudo"))

    if not is_root:
        if not has_sudo:
            _diag_add("WARN", "Auto-install impossible",
                      "non-root et sudo absent")
            print("[vmshell] -> lancez : sudo " + " ".join(cmd), flush=True)
            return False
        # Vérifie un sudo non-interactif (NOPASSWD).
        try:
            r = subprocess.run(["sudo", "-n", "true"],
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=2)
            if r.returncode != 0:
                _diag_add("WARN", "Auto-install nécessite un mot de passe",
                          "lancez la commande affichée en console")
                print("[vmshell] -> lancez : sudo " + " ".join(cmd),
                      flush=True)
                return False
        except (OSError, subprocess.TimeoutExpired):
            return False
        cmd = ["sudo", "-n", *cmd]

    print("[vmshell] installation : " + " ".join(cmd), flush=True)
    env = os.environ.copy()
    env.update(env_extra)
    try:
        r = subprocess.run(cmd, env=env, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as e:
        _diag_add("FAIL", "Échec auto-install", str(e))
        return False
    if r.returncode == 0:
        _diag_add("OK", "Auto-install",
                  f"{len(pkgs)} paquet(s) installé(s) : {', '.join(pkgs)}")
        return True
    _diag_add("FAIL", "Échec auto-install",
              f"code retour {r.returncode}")
    return False


# ---------------------------------------------------------------------------
# Lancement d'une commande dans un terminal externe (visible).
# Utilisé pour proposer l'installation des pilotes / dépendances avec un
# retour visuel à l'utilisateur (sortie + demande de mot de passe sudo).
_TERMINALS = [
    # (binaire, args avant la commande, doit_passer_un_seul_arg_string)
    ("x-terminal-emulator", ["-e"],          False),
    ("gnome-terminal",      ["--", "bash", "-c"], True),
    ("konsole",             ["-e", "bash", "-c"], True),
    ("xfce4-terminal",      ["-e"],          True),
    ("mate-terminal",       ["-e"],          True),
    ("tilix",               ["-e"],          True),
    ("kitty",               ["bash", "-c"],  True),
    ("alacritty",           ["-e", "bash", "-c"], True),
    ("foot",                ["bash", "-c"],  True),
    ("xterm",               ["-e", "bash", "-c"], True),
    ("st",                  ["-e", "bash", "-c"], True),
]


def open_terminal(shell_cmd, title="vmshell"):
    """Ouvre un terminal et y exécute `shell_cmd` (string bash).
    Retourne True si un terminal a pu être lancé.
    """
    # On rajoute un "appuyez sur Entrée" final pour laisser l'utilisateur
    # lire le résultat de l'installation.
    full = (
        f'echo "── {title} ──"; '
        f'{shell_cmd}; rc=$?; echo; '
        f'echo "[Code retour : $rc]"; '
        f'echo "Appuyez sur Entrée pour fermer…"; read _'
    )
    for binary, args, single_arg in _TERMINALS:
        if not shutil.which(binary):
            continue
        try:
            if single_arg:
                cmd = [binary, *args, full]
            else:
                cmd = [binary, *args, "bash", "-lc", full]
            subprocess.Popen(cmd, start_new_session=True)
            return True
        except OSError:
            continue
    print("[vmshell] aucun émulateur de terminal trouvé pour : "
          + shell_cmd, flush=True)
    return False


def build_fix_command(fix):
    """Construit la commande shell installant un correctif donné.
    Retourne (cmd_str, pm) ou (None, None) si PM indispo."""
    pm, _ = _detect_pm()
    if pm is None:
        return None, None
    pkgs = fix["pkgs"].get(pm)
    if not pkgs:
        # Pas de paquets pour ce gestionnaire : skip.
        return None, pm
    if pm == "apt":
        install = ("DEBIAN_FRONTEND=noninteractive apt-get update && "
                   "DEBIAN_FRONTEND=noninteractive apt-get install -y "
                   "--no-install-recommends " + " ".join(pkgs))
    elif pm == "dnf":
        install = "dnf install -y " + " ".join(pkgs)
    elif pm == "pacman":
        install = "pacman -S --noconfirm --needed " + " ".join(pkgs)
    else:
        return None, pm
    # Préfixe sudo si on n'est pas root.
    if os.geteuid() != 0:
        install = "sudo " + install
    if fix.get("post"):
        install += " && " + fix["post"]
    return install, pm


def _crash_log(exc):
    """Enregistre la trace d'erreur dans un fichier de crash et l'affiche."""
    import traceback
    try:
        ensure_config_dir()
    except Exception:
        pass
    msg = "".join(traceback.format_exception(type(exc), exc,
                                             exc.__traceback__))
    print("[vmshell] ===== CRASH =====\n" + msg, file=sys.stderr, flush=True)
    try:
        crash_path = CONFIG_DIR / "crash.log"
        with open(crash_path, "a", encoding="utf-8") as fh:
            fh.write(f"\n----- {datetime.now().isoformat()} -----\n")
            fh.write(msg)
        print(f"[vmshell] log crash → {crash_path}", flush=True)
    except Exception:
        pass
    return msg


def _show_crash_dialog(msg):
    """Tente d'afficher une fenêtre GTK avec l'erreur. Si même GTK est HS,
    on retombe sur stderr (déjà imprimé)."""
    try:
        d = Gtk.MessageDialog(
            transient_for=None, modal=True,
            message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.CLOSE,
            text="VMShell — erreur fatale")
        d.format_secondary_text(
            "Une erreur a empêché le démarrage.\n"
            "Détails sauvegardés dans ~/.config/vmshell/crash.log\n\n"
            + msg.splitlines()[-1] if msg else "")
        d.run()
        d.destroy()
    except Exception:
        pass


def main():
    # Exception hook global : capte tout ce qui ne serait pas pris ailleurs.
    def _hook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.exit(0)
        m = _crash_log(exc)
        _show_crash_dialog(m)
    sys.excepthook = _hook

    try:
        tune_runtime()
    except Exception as e:
        print(f"[vmshell] tune_runtime a échoué (non bloquant) : {e}",
              flush=True)

    try:
        run_self_check()
        if auto_install_missing():
            run_self_check()
    except Exception as e:
        print(f"[vmshell] auto-check a échoué (non bloquant) : {e}",
              flush=True)

    import atexit as _atexit
    try:
        _atexit.register(thaw_background_apps)
    except Exception:
        pass

    try:
        win = VMShell()
        win.show_all()
        Gtk.main()
    except Exception as e:
        m = _crash_log(e)
        _show_crash_dialog(m)
        sys.exit(1)


if __name__ == "__main__":
    main()
