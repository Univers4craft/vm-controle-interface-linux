"""Estimation autonomie batterie (depuis /sys/class/power_supply).

Module pur : aucune dépendance GTK. Lecture cachée ~5 s pour éviter
de relire /sys/ à chaque tick UI (quickbar, popup alim, watch tick,
miniatures vivantes...).
"""

from __future__ import annotations

import os
import time


# Cache module-level : (timestamp_monotonic, (seconds_or_None, mode_or_None))
_BAT_CACHE = {"t": 0.0, "val": (None, None)}
_BAT_CACHE_TTL = 5.0


def estimate_battery_seconds():
    """Estime le temps restant (s) en se basant sur energy_now / power_now
    ou charge_now / current_now selon ce que le noyau expose.
    Retourne (seconds, mode) avec mode = 'discharge'|'charge'|'full'|None.
    Résultat caché ~5 s."""
    now = time.monotonic()
    if now - _BAT_CACHE["t"] < _BAT_CACHE_TTL:
        return _BAT_CACHE["val"]
    val = _estimate_battery_seconds_uncached()
    _BAT_CACHE["t"] = now
    _BAT_CACHE["val"] = val
    return val


def _estimate_battery_seconds_uncached():
    try:
        base = "/sys/class/power_supply"
        if not os.path.isdir(base):
            return None, None
        for name in sorted(os.listdir(base)):
            if not name.startswith("BAT"):
                continue
            d = os.path.join(base, name)

            def _r(fname):
                try:
                    with open(os.path.join(d, fname)) as f:
                        return int(f.read().strip())
                except (OSError, ValueError):
                    return None

            status = ""
            try:
                with open(os.path.join(d, "status")) as f:
                    status = f.read().strip().lower()
            except OSError:
                pass
            if status == "full":
                return 0, "full"

            # En décharge : energy_now / power_now (Wh/W → h).
            energy = _r("energy_now")
            power = _r("power_now")
            if energy is not None and power and power > 0:
                if status.startswith("charg") and not status.startswith("not"):
                    energy_full = _r("energy_full") or energy
                    remain = max(0, energy_full - energy)
                    return int(remain * 3600 / power), "charge"
                return int(energy * 3600 / power), "discharge"

            # Fallback charge_now / current_now.
            charge = _r("charge_now")
            current = _r("current_now")
            if charge is not None and current and current > 0:
                if status.startswith("charg") and not status.startswith("not"):
                    cfull = _r("charge_full") or charge
                    remain = max(0, cfull - charge)
                    return int(remain * 3600 / current), "charge"
                return int(charge * 3600 / current), "discharge"
    except OSError:
        pass
    return None, None


def fmt_duration(sec):
    """3725 → '1h02'  ·  120 → '2 min'  ·  None → '—'."""
    if sec is None or sec < 0:
        return "—"
    if sec < 60:
        return f"{sec}s"
    m = sec // 60
    if m < 60:
        return f"{m} min"
    h = m // 60
    return f"{h}h{m % 60:02d}"
