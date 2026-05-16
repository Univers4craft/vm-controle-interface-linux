"""Détection matérielle (GPU/CPU), tuning runtime et gestion des
processus d'arrière-plan (freeze/thaw + reaper SIGCHLD).

Module sans dépendance GTK. Importé tôt dans vmshell.py pour installer
le handler SIGCHLD avant que les premiers Popen ne soient lancés.

Exports principaux :
- HW_INFO            : dict mutable {"gpu_vendor", "gpu_accel", "cpu_count"}
- tune_runtime()     : tuning priorité, env GPU, audio, clavier
- _detect_gpu()      : (vendor, accel_ok)
- freeze_background_apps() / thaw_background_apps()
- _reap_zombies      : handler SIGCHLD (installé automatiquement)
"""

from __future__ import annotations

import os
import shutil
import signal as _signal
import subprocess


# ---------------------------------------------------------------------------
# Reaper SIGCHLD : évite l'accumulation de processus zombies issus des
# multiples subprocess.Popen "feu et oublie" (alertes batterie, sons,
# pkexec, etc.).
# ---------------------------------------------------------------------------
def _reap_zombies(*_):
    try:
        while True:
            pid, _st = os.waitpid(-1, os.WNOHANG)
            if pid <= 0:
                break
    except (ChildProcessError, OSError):
        pass


try:
    _signal.signal(_signal.SIGCHLD, _reap_zombies)
except (ValueError, OSError):
    # Pas critique : sur certaines plateformes / threads non principal.
    pass


# ---------------------------------------------------------------------------
# Freeze/Thaw d'applications d'arrière-plan pendant une session RDP.
# ---------------------------------------------------------------------------
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
    try:
        keep.add(os.getppid())
    except Exception:
        pass
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
        try:
            pid = int(parts[0])
        except ValueError:
            continue
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
# Auto-détection matérielle (GPU/CPU) — renseigné par tune_runtime() au
# démarrage.
# ---------------------------------------------------------------------------
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

    gpu_lines = [l for l in out.splitlines()
                 if any(k in l for k in (" vga ", "vga compatible",
                                         "3d controller",
                                         "display controller"))]
    blob = "\n".join(gpu_lines) or out
    if "nvidia" in blob:
        vendor = "nvidia"
    elif "amd" in blob or "radeon" in blob or "ati " in blob:
        vendor = "amd"
    elif "intel" in blob:
        vendor = "intel"

    accel = False
    if vendor == "nvidia":
        if shutil.which("nvidia-smi"):
            try:
                r = subprocess.run(["nvidia-smi", "-L"],
                                   stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, timeout=2)
                accel = (r.returncode == 0)
            except Exception:
                accel = False
        if not accel:
            accel = any(os.path.exists(f"/dev/dri/renderD{128 + i}")
                        for i in range(4))
    elif vendor in ("amd", "intel"):
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
        os.nice(-5)
        tuned.append("nice -5")
    except OSError:
        try:
            os.nice(-1)
            tuned.append("nice -1")
        except OSError:
            pass
    try:
        subprocess.run(
            ["ionice", "-c", "2", "-n", "0", "-p", str(os.getpid())],
            check=False, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=2)
        tuned.append("ionice best-effort")
    except OSError:
        pass

    # 2) Détection GPU + variables d'environnement adaptées.
    vendor, accel = _detect_gpu()
    HW_INFO["gpu_vendor"] = vendor
    HW_INFO["gpu_accel"] = accel

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
        os.environ.setdefault("LIBVA_DRIVER_NAME", "")
        os.environ.setdefault("VDPAU_DRIVER", "")
        os.environ.setdefault("LIBGL_ALWAYS_SOFTWARE", "0")
        os.environ.setdefault("MESA_GLTHREAD", "true")
        tuned.append(f"GPU=aucun, CPU={HW_INFO['cpu_count']} cœurs")

    # Communs.
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
