"""Dictée vocale : détection moteur STT + transcription locale.

Module pur : aucune dépendance GTK. Cherche whisper / whisper-cpp /
nerd-dictation dans PATH et dans ~/.local/bin.
"""

from __future__ import annotations

import os
import shutil
import subprocess


def stt_available():
    """Retourne le nom (ou chemin) du moteur STT dispo, ou None.
    Recherche aussi dans ``~/.local/bin`` (où ``pip install --user``
    dépose la commande ``whisper`` sans nécessiter un redémarrage de
    shell)."""
    for cmd in ("whisper-cpp", "whisper.cpp", "whisper", "nerd-dictation"):
        p = shutil.which(cmd)
        if p:
            return p
    user_bin = os.path.expanduser("~/.local/bin")
    for cmd in ("whisper-cpp", "whisper", "nerd-dictation"):
        p = os.path.join(user_bin, cmd)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def stt_install_check():
    """Diagnostic rapide pour la fenêtre d'installation. Retourne un
    dict avec les pré-requis détectés."""
    return {
        "pip":     bool(shutil.which("pip3") or shutil.which("pip")),
        "python":  bool(shutil.which("python3")),
        "ffmpeg":  bool(shutil.which("ffmpeg")),
        "arecord": bool(shutil.which("arecord")),
        "engine":  stt_available(),
    }


def stt_transcribe(wav_path):
    """Tente de transcrire un fichier WAV en français. Retourne le
    texte ou None si aucun moteur dispo / erreur."""
    eng = stt_available()
    if not eng:
        return None
    eng_name = os.path.basename(eng)
    try:
        if "whisper-cpp" in eng_name or "whisper.cpp" in eng_name:
            r = subprocess.run(
                [eng, "-f", wav_path, "-l", "fr", "-nt", "-np",
                 "-otxt", "-of", wav_path],
                capture_output=True, text=True, timeout=120)
            txt_path = wav_path + ".txt"
            if os.path.isfile(txt_path):
                with open(txt_path) as f:
                    return f.read().strip()
            return (r.stdout or "").strip()
        if eng_name == "whisper":
            r = subprocess.run(
                [eng, wav_path, "--language", "French",
                 "--model", "tiny", "--output_format", "txt",
                 "--output_dir", os.path.dirname(wav_path) or "."],
                capture_output=True, text=True, timeout=180)
            base = os.path.splitext(os.path.basename(wav_path))[0]
            txt_path = os.path.join(
                os.path.dirname(wav_path) or ".", base + ".txt")
            if os.path.isfile(txt_path):
                with open(txt_path) as f:
                    return f.read().strip()
            return (r.stdout or "").strip()
    except (subprocess.SubprocessError, OSError):
        pass
    return None
