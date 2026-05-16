"""Historique presse-papier en mémoire (max 10 entrées).

Tout reste en RAM : aucune écriture disque (mots de passe, code, etc.).
"""

from __future__ import annotations

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, GLib  # noqa: E402


class ClipboardHistory:
    """Garde les N dernières chaînes copiées dans le presse-papier local."""

    MAX = 10

    def __init__(self):
        self._items = []          # list[str], plus récent en tête.
        self._last = None
        self._started = False
        # Callback optionnel appelé (depuis le main loop) à chaque
        # nouvelle entrée. Utilisé par VMShell pour afficher un toast
        # « Synchro PP » lorsque la VM copie quelque chose.
        self.on_change = None

    def start(self):
        if self._started:
            return
        self._started = True
        # Polling clipboard : 2s suffit largement (humain ne copie pas
        # plus vite) et économise ~40 % de lectures GTK.
        GLib.timeout_add(2000, self._tick)

    def _tick(self):
        try:
            clip = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
            txt = clip.wait_for_text()
        except Exception:
            return True
        if txt and txt != self._last:
            self._last = txt
            try:
                self._items.remove(txt)
            except ValueError:
                pass
            self._items.insert(0, txt)
            del self._items[self.MAX:]
            if callable(self.on_change):
                try:
                    self.on_change(txt)
                except Exception:
                    pass
        return True

    def items(self):
        return list(self._items)

    def add(self, text):
        if not text:
            return
        try:
            self._items.remove(text)
        except ValueError:
            pass
        self._items.insert(0, text)
        del self._items[self.MAX:]

    def clear(self):
        self._items = []
        self._last = None
