#!/bin/bash
# Session VMShell — lancement minimaliste sans environnement de bureau.
# Démarre uniquement un gestionnaire de fenêtres léger + l'app VMShell.

export XDG_SESSION_TYPE=x11
export XDG_CURRENT_DESKTOP=VMShell
export GDK_BACKEND=x11

# D-Bus utilisateur (clipboard, notifications éventuelles)
if [ -z "$DBUS_SESSION_BUS_ADDRESS" ]; then
    eval "$(dbus-launch --sh-syntax --exit-with-session)"
fi

# Curseur visible
xsetroot -cursor_name left_ptr 2>/dev/null || true

# Gestionnaire de fenêtres minimal (nécessaire pour que fullscreen GTK
# soit honoré). Openbox est léger et déjà présent.
openbox &
WM_PID=$!

# VMShell — quand l'utilisateur ferme l'app, on termine la session.
python3 /home/damien/Documents/partage-vm/vmshell/vmshell.py

kill $WM_PID 2>/dev/null
