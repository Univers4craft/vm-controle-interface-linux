"""Capture de raccourcis globaux via Xlib.

- EscapeGrabber : capture la touche Escape même quand un client X embarqué
  (xfreerdp) possède le focus clavier.
- HotkeyGrabber : capture de raccourcis arbitraires (ex. Super+V).

NumLock (Mod2) et CapsLock (Lock) sont automatiquement ignorés.
"""

from __future__ import annotations

import threading
import time

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import GLib  # noqa: E402


class EscapeGrabber:
    """Grabs the Escape key globally via Xlib."""

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


class HotkeyGrabber:
    """Grab générique de raccourcis globaux via Xlib (Super+V, etc.)."""

    def __init__(self):
        self._raw = []                # list[(keysym_str, modmask, cb)]
        self._bindings = []           # list[(keycode, modmask, cb)]
        self._thread = None
        self._stop = False
        self._dpy = None

    def add(self, keysym_str, modmask, callback):
        """`keysym_str` au format Xlib (ex. "V"). `modmask` ex. X.Mod4Mask."""
        self._raw.append((keysym_str, int(modmask), callback))

    def start(self):
        if self._thread is not None or not self._raw:
            return
        try:
            from Xlib import display, X, XK
        except Exception as e:
            print(f"[vmshell] Xlib indisponible (hotkeys): {e}",
                  flush=True)
            return
        try:
            self._dpy = display.Display()
            for ks_name, mods, cb in self._raw:
                ks = XK.string_to_keysym(ks_name)
                kc = self._dpy.keysym_to_keycode(ks)
                if kc == 0:
                    print(f"[vmshell] hotkey ignoré (keysym inconnu): "
                          f"{ks_name}", flush=True)
                    continue
                self._bindings.append((kc, mods, cb))
            if not self._bindings:
                self._dpy = None
                return
            self._do_grab()
            root = self._dpy.screen().root
            root.change_attributes(event_mask=X.KeyPressMask)
            self._dpy.sync()
        except Exception as e:
            print(f"[vmshell] grab hotkeys échoué: {e}", flush=True)
            self._dpy = None
            return
        print(f"[vmshell] {len(self._bindings)} hotkey(s) global(es) "
              f"actif(s)", flush=True)
        self._stop = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _do_grab(self):
        from Xlib import X
        root = self._dpy.screen().root
        extras = (0, X.Mod2Mask, X.LockMask,
                  X.Mod2Mask | X.LockMask)
        for kc, mods, _cb in self._bindings:
            for ex in extras:
                try:
                    root.grab_key(kc, mods | ex, 1,
                                  X.GrabModeAsync, X.GrabModeAsync)
                except Exception:
                    pass

    def regrab(self):
        if self._dpy is None or not self._bindings:
            return
        try:
            self._do_grab()
            self._dpy.sync()
        except Exception:
            pass

    def _run(self):
        from Xlib import X
        ignore_mask = X.Mod2Mask | X.LockMask
        while not self._stop:
            try:
                if self._dpy.pending_events() == 0:
                    time.sleep(0.05)
                    continue
                ev = self._dpy.next_event()
                if ev.type != X.KeyPress:
                    continue
                kc = ev.detail
                state = ev.state & ~ignore_mask
                for bkc, bmods, cb in self._bindings:
                    if bkc == kc and bmods == state:
                        try:
                            GLib.idle_add(cb)
                        except Exception:
                            pass
                        break
            except Exception:
                time.sleep(0.1)

    def stop(self):
        self._stop = True
