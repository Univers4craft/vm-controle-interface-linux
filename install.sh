#!/bin/bash
# ============================================================================
# VMShell — installeur autonome
# ----------------------------------------------------------------------------
# Installe toutes les dépendances (Xorg, openbox, LightDM, freerdp, GTK, ...)
# puis déploie VMShell comme SEULE session disponible à l'écran de login.
#
# Usage :   sudo ./install.sh
# ============================================================================

set -e

# ----------------------------------------------------------------------------
# Options en ligne de commande
#   --autologin USER   connecte automatiquement USER en session VMShell
#   --no-autologin     (défaut) écran de login LightDM
# ----------------------------------------------------------------------------
AUTOLOGIN_USER=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --autologin)
            AUTOLOGIN_USER="${2:-}"
            if [[ -z "$AUTOLOGIN_USER" ]]; then
                echo "ERREUR : --autologin requiert un nom d'utilisateur." >&2
                exit 1
            fi
            shift 2
            ;;
        --autologin=*)
            AUTOLOGIN_USER="${1#*=}"
            shift
            ;;
        --no-autologin)
            AUTOLOGIN_USER=""
            shift
            ;;
        -h|--help)
            cat <<EOF
Usage : sudo $0 [--autologin USER]

  --autologin USER   ouvre automatiquement la session VMShell pour USER
                     (kiosque sans écran de login)
  --no-autologin     comportement par défaut (écran de login LightDM)
EOF
            exit 0
            ;;
        *)
            echo "Option inconnue : $1" >&2
            exit 1
            ;;
    esac
done

if [[ $EUID -ne 0 ]]; then
    echo "Ce script doit être lancé en root :  sudo $0" >&2
    exit 1
fi

if [[ -n "$AUTOLOGIN_USER" ]] && ! id "$AUTOLOGIN_USER" >/dev/null 2>&1; then
    echo "ERREUR : l'utilisateur « $AUTOLOGIN_USER » n'existe pas." >&2
    exit 1
fi

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="/opt/vmshell"

echo "[vmshell] === Installation VMShell ==="
echo "[vmshell] source : $SRC_DIR"

# ----------------------------------------------------------------------------
# 1. Détection du gestionnaire de paquets
# ----------------------------------------------------------------------------
if   command -v apt-get >/dev/null 2>&1; then PM=apt
elif command -v dnf     >/dev/null 2>&1; then PM=dnf
elif command -v pacman  >/dev/null 2>&1; then PM=pacman
else
    echo "[vmshell] ERREUR : aucun gestionnaire de paquets supporté (apt/dnf/pacman)." >&2
    exit 2
fi
echo "[vmshell] gestionnaire détecté : $PM"

# ----------------------------------------------------------------------------
# 2. Installation des dépendances
# ----------------------------------------------------------------------------
echo "[vmshell] installation des dépendances système…"

case "$PM" in
    apt)
        export DEBIAN_FRONTEND=noninteractive
        apt-get update -y
        apt-get install -y --no-install-recommends \
            xserver-xorg xinit openbox lightdm \
            python3 python3-gi python3-xlib \
            gir1.2-gtk-3.0 gir1.2-vte-2.91 gir1.2-gdkx11-3.0 \
            xdotool x11-xserver-utils dbus-x11 xdg-utils \
            pulseaudio pulseaudio-utils \
            freerdp3-x11 \
            openssh-client \
            libglib2.0-bin pciutils util-linux \
            ca-certificates fonts-dejavu-core \
            || apt-get install -y --no-install-recommends freerdp2-x11
        ;;
    dnf)
        dnf install -y \
            xorg-x11-server-Xorg xorg-x11-xinit openbox lightdm \
            python3 python3-gobject python3-xlib \
            gtk3 vte291 \
            xdotool xset xsetroot dbus-x11 xdg-utils \
            pulseaudio pulseaudio-utils \
            freerdp \
            openssh-clients \
            glib2 pciutils util-linux \
            ca-certificates dejavu-sans-fonts
        ;;
    pacman)
        pacman -Sy --noconfirm \
            xorg-server xorg-xinit openbox lightdm \
            python python-gobject python-xlib \
            gtk3 vte3 \
            xdotool xorg-xset xorg-xsetroot dbus xdg-utils \
            pulseaudio pulseaudio-alsa libpulse \
            freerdp \
            openssh \
            glib2 pciutils util-linux \
            ca-certificates ttf-dejavu
        ;;
esac

# ----------------------------------------------------------------------------
# 3. Copie des fichiers de l'application
# ----------------------------------------------------------------------------
echo "[vmshell] déploiement de l'application dans $APP_DIR"
install -d "$APP_DIR"
install -m 0644 "$SRC_DIR/vmshell.py"  "$APP_DIR/vmshell.py"
install -m 0644 "$SRC_DIR/vmshell.css" "$APP_DIR/vmshell.css"

# Wrapper de session — pointe vers /opt/vmshell (path stable)
cat > /usr/local/bin/vmshell-session <<'EOF'
#!/bin/bash
# Session VMShell — kiosque RDP léger.
export XDG_SESSION_TYPE=x11
export XDG_CURRENT_DESKTOP=VMShell
export GDK_BACKEND=x11

if [ -z "$DBUS_SESSION_BUS_ADDRESS" ]; then
    eval "$(dbus-launch --sh-syntax --exit-with-session)"
fi

xsetroot -cursor_name left_ptr 2>/dev/null || true
openbox &
WM_PID=$!
python3 /opt/vmshell/vmshell.py
kill $WM_PID 2>/dev/null
EOF
chmod 0755 /usr/local/bin/vmshell-session

# ----------------------------------------------------------------------------
# 4. Entrée xsession — VMShell sera la seule proposée
# ----------------------------------------------------------------------------
echo "[vmshell] enregistrement de la session graphique"
install -d /usr/share/xsessions
cat > /usr/share/xsessions/vmshell.desktop <<'EOF'
[Desktop Entry]
Name=VMShell
Comment=Session VMShell — gestionnaire de connexions distantes
Exec=/usr/local/bin/vmshell-session
TryExec=/usr/local/bin/vmshell-session
Type=Application
DesktopNames=VMShell
EOF

# Cacher toutes les autres sessions (les .desktop existants).
echo "[vmshell] masquage des autres sessions de bureau"
shopt -s nullglob
for f in /usr/share/xsessions/*.desktop /usr/share/wayland-sessions/*.desktop; do
    case "$(basename "$f")" in
        vmshell.desktop) ;;
        *)
            grep -q '^NoDisplay=true' "$f" 2>/dev/null || \
                echo "NoDisplay=true" >> "$f"
            ;;
    esac
done
shopt -u nullglob

# ----------------------------------------------------------------------------
# 5. Configuration LightDM — VMShell par défaut, autologin optionnel
# ----------------------------------------------------------------------------
echo "[vmshell] configuration LightDM"
install -d /etc/lightdm/lightdm.conf.d
{
    echo "[Seat:*]"
    echo "user-session=vmshell"
    echo "allow-guest=false"
    echo "greeter-hide-users=false"
    if [[ -n "$AUTOLOGIN_USER" ]]; then
        echo "autologin-user=$AUTOLOGIN_USER"
        echo "autologin-user-timeout=0"
        echo "autologin-session=vmshell"
    fi
} > /etc/lightdm/lightdm.conf.d/50-vmshell.conf

# Groupe autologin requis par LightDM sur certaines distros (Debian/Ubuntu).
if [[ -n "$AUTOLOGIN_USER" ]]; then
    if ! getent group autologin >/dev/null; then
        groupadd -r autologin 2>/dev/null || true
    fi
    if getent group autologin >/dev/null; then
        usermod -aG autologin "$AUTOLOGIN_USER" 2>/dev/null || true
    fi
fi

# ----------------------------------------------------------------------------
# 6. Activation du service graphique
# ----------------------------------------------------------------------------
if command -v systemctl >/dev/null 2>&1; then
    echo "[vmshell] activation de lightdm + cible graphique"
    systemctl enable lightdm.service        >/dev/null 2>&1 || true
    systemctl set-default graphical.target  >/dev/null 2>&1 || true
fi

# ----------------------------------------------------------------------------
# 7. Fin
# ----------------------------------------------------------------------------
cat <<EOF

[vmshell] === Installation terminée ===

  • Session installée :    /usr/share/xsessions/vmshell.desktop
  • Lanceur :              /usr/local/bin/vmshell-session
  • Application :          /opt/vmshell/
EOF
if [[ -n "$AUTOLOGIN_USER" ]]; then
    echo "  • Autologin :            $AUTOLOGIN_USER (mode kiosque)"
else
    echo "  • Autologin :            désactivé (écran de login LightDM)"
fi
cat <<EOF

Pour démarrer maintenant :
    sudo systemctl start lightdm

EOF
