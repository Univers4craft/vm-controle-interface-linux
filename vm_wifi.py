"""Détection et bascule Wi-Fi 2.4 / 5 / 6 GHz.

Utilise `iw` (lecture passive de la connexion courante) et `nmcli`
(scan + bascule). Tout est best-effort : si les outils sont absents
ou si la machine est sur Ethernet, les fonctions renvoient simplement
``None`` et l'appelant ignore.

API :
    current_link()      -> dict | None  (iface, ssid, freq_mhz, band,
                                          signal_dbm)
    scan_5ghz_for_ssid(ssid) -> list[dict]  (bssid, freq, signal, chan)
    suggest_5ghz()      -> dict | None  (suggestion ou None)
    switch_to_bssid(bssid, timeout=10) -> (ok: bool, message: str)
"""
from __future__ import annotations

import shutil
import subprocess
from typing import Optional


def _run(cmd, timeout=4):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout)
        if r.returncode != 0:
            return None
        return r.stdout
    except (OSError, subprocess.SubprocessError):
        return None


def _band_from_freq(freq_mhz: int) -> str:
    if freq_mhz < 3000:
        return "2.4 GHz"
    if freq_mhz < 6000:
        return "5 GHz"
    return "6 GHz"


def current_link() -> Optional[dict]:
    """Retourne les infos de la liaison Wi-Fi actuelle ou None."""
    if not shutil.which("iw"):
        return None
    # Trouver l'interface Wi-Fi active.
    out = _run(["iw", "dev"]) or ""
    iface = None
    for line in out.splitlines():
        s = line.strip()
        if s.startswith("Interface "):
            iface = s.split(None, 1)[1].strip()
            break
    if not iface:
        return None
    link = _run(["iw", "dev", iface, "link"]) or ""
    if "Not connected" in link or not link.strip():
        return None
    ssid = None
    freq = None
    signal = None
    for line in link.splitlines():
        s = line.strip()
        if s.startswith("SSID:"):
            ssid = s.split(":", 1)[1].strip()
        elif s.startswith("freq:"):
            try:
                freq = int(s.split(":", 1)[1].strip().split()[0])
            except (ValueError, IndexError):
                pass
        elif s.startswith("signal:"):
            try:
                signal = int(float(s.split(":", 1)[1].strip().split()[0]))
            except (ValueError, IndexError):
                pass
    if freq is None:
        return None
    return {
        "iface": iface,
        "ssid": ssid,
        "freq_mhz": freq,
        "band": _band_from_freq(freq),
        "signal_dbm": signal,
    }


def scan_5ghz_for_ssid(ssid: str) -> list[dict]:
    """Retourne les BSSID 5 GHz visibles pour ce SSID (triés par signal)."""
    if not shutil.which("nmcli") or not ssid:
        return []
    # Force un rescan rapide (best-effort).
    _run(["nmcli", "device", "wifi", "rescan"], timeout=3)
    out = _run(["nmcli", "-t", "-f", "BSSID,SSID,FREQ,SIGNAL,CHAN",
                "device", "wifi", "list"], timeout=6) or ""
    found = []
    for line in out.splitlines():
        # nmcli -t escape les ':' du BSSID avec '\:' donc on parse à droite.
        # Format: <BSSID>:<SSID>:<FREQ>:<SIGNAL>:<CHAN>
        parts = []
        cur = ""
        i = 0
        while i < len(line):
            if line[i] == "\\" and i + 1 < len(line):
                cur += line[i + 1]
                i += 2
                continue
            if line[i] == ":":
                parts.append(cur)
                cur = ""
                i += 1
                continue
            cur += line[i]
            i += 1
        parts.append(cur)
        if len(parts) < 5:
            continue
        bssid, sname, freq_s, sig_s, chan = parts[:5]
        if sname.strip() != ssid:
            continue
        try:
            freq = int(freq_s.replace("MHz", "").strip())
            sig = int(sig_s.strip())
        except ValueError:
            continue
        if freq < 4900:  # on garde uniquement 5 GHz et +
            continue
        found.append({
            "bssid": bssid.strip(),
            "freq_mhz": freq,
            "signal": sig,
            "chan": chan.strip(),
            "band": _band_from_freq(freq),
        })
    found.sort(key=lambda d: d["signal"], reverse=True)
    return found


def suggest_5ghz() -> Optional[dict]:
    """Si on est sur du 2.4 GHz et qu'un BSSID 5 GHz du même SSID est
    visible, retourne la meilleure suggestion. Sinon None."""
    link = current_link()
    if not link or link["band"] != "2.4 GHz" or not link.get("ssid"):
        return None
    cands = scan_5ghz_for_ssid(link["ssid"])
    if not cands:
        return None
    return {
        "current": link,
        "target": cands[0],
        "alternatives": cands[1:3],
    }


def switch_to_bssid(bssid: str, timeout: int = 10) -> tuple[bool, str]:
    """Tente une bascule vers le BSSID donné via nmcli."""
    if not shutil.which("nmcli") or not bssid:
        return False, "nmcli indisponible."
    try:
        r = subprocess.run(
            ["nmcli", "device", "wifi", "connect", bssid],
            capture_output=True, text=True, timeout=timeout)
        if r.returncode == 0:
            return True, (r.stdout.strip() or "Connecté.")
        return False, (r.stderr.strip() or r.stdout.strip()
                       or "Échec inconnu.")
    except subprocess.TimeoutExpired:
        return False, "Délai dépassé."
    except OSError as e:
        return False, str(e)
