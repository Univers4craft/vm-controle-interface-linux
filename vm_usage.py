"""Suivi d'usage par VM : durée totale, nb de connexions, dernière session.

Persistance JSON. Le chemin du fichier et les helpers de chargement /
sauvegarde sont fournis à la construction pour éviter une dépendance
circulaire avec vmshell.
"""

from __future__ import annotations

import time


class UsageTracker:
    """Enregistre durée totale, nb de connexions, dernière session par VM."""

    def __init__(self, path, loader, saver):
        """`path` = Path du JSON; `loader(path, default)`; `saver(path, data)`."""
        self._path = path
        self._loader = loader
        self._saver = saver
        self._data = loader(path, {})
        self._open = {}   # cid -> (start_ts, name)

    def start(self, conn):
        self._open[conn["id"]] = (time.time(), conn.get("name", "?"))

    def end(self, cid):
        info = self._open.pop(cid, None)
        if not info:
            return
        start, name = info
        elapsed = max(0, int(time.time() - start))
        rec = self._data.setdefault(cid, {
            "name": name, "total_sec": 0, "count": 0, "last": 0})
        rec["name"] = name
        rec["total_sec"] += elapsed
        rec["count"] += 1
        rec["last"] = int(time.time())
        try:
            self._saver(self._path, self._data)
        except OSError:
            pass

    def end_all(self):
        for cid in list(self._open.keys()):
            self.end(cid)

    def stats_for(self, cid):
        return self._data.get(cid, {"total_sec": 0, "count": 0, "last": 0})

    def all_stats(self):
        return dict(self._data)

    def total_sec(self):
        return sum(r.get("total_sec", 0) for r in self._data.values())

    def total_count(self):
        return sum(r.get("count", 0) for r in self._data.values())
