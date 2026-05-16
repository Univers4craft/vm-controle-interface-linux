"""Test de débit local → VM : ping ICMP + RTT TCP.

Module pur : aucune dépendance GTK. Pas de mesure de bande passante
(pas d'iperf garanti côté VM).
"""

from __future__ import annotations

import socket
import subprocess
import time


def speedtest_to(host, port, timeout=4.0):
    """Mesure ping (5 paquets ICMP) et latence d'ouverture TCP vers
    host:port. Retourne dict {ping_avg, ping_loss, tcp_ms, verdict}."""
    out = {"ping_avg": None, "ping_loss": None,
           "tcp_ms": None, "verdict": "?"}

    # Ping ICMP.
    try:
        p = subprocess.run(
            ["ping", "-c", "5", "-W", "1", "-q", host],
            capture_output=True, text=True, timeout=timeout + 6)
        for line in p.stdout.splitlines():
            if "packet loss" in line:
                for tok in line.replace(",", " ").split():
                    if tok.endswith("%"):
                        try:
                            out["ping_loss"] = float(tok[:-1])
                        except ValueError:
                            pass
            if line.startswith("rtt") or line.startswith("round-trip"):
                try:
                    nums = line.split("=")[1].strip().split()[0].split("/")
                    out["ping_avg"] = float(nums[1])
                except (IndexError, ValueError):
                    pass
    except (subprocess.SubprocessError, OSError):
        pass

    # Mesure 3× ouverture TCP.
    samples = []
    for _ in range(3):
        t0 = time.time()
        try:
            with socket.create_connection(
                    (host, int(port)), timeout=timeout):
                samples.append((time.time() - t0) * 1000.0)
        except OSError:
            pass
    if samples:
        out["tcp_ms"] = round(sum(samples) / len(samples), 1)

    # Verdict.
    avg = out["ping_avg"] if out["ping_avg"] is not None else out["tcp_ms"]
    loss = out["ping_loss"] or 0.0
    if avg is None:
        out["verdict"] = "Injoignable"
    elif loss >= 5 or avg >= 120:
        out["verdict"] = "Mauvais"
    elif avg >= 60:
        out["verdict"] = "Acceptable"
    elif avg >= 25:
        out["verdict"] = "Bon"
    else:
        out["verdict"] = "Excellent"
    return out
