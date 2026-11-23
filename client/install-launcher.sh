#!/usr/bin/env bash
# Adds VoxChat to your app launcher (Linux). Run it again after moving this folder.
# Remove it with: rm ~/.local/share/applications/voxchat.desktop
set -euo pipefail
here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
apps="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
# Exec needs ", `, $ and \ escaped inside its quotes (desktop entry spec).
quoted="$(printf '%s' "$here/run.sh" | sed 's/[\\"`$]/\\\\&/g')"
mkdir -p "$apps"
cat > "$apps/voxchat.desktop" <<ENTRY
[Desktop Entry]
Type=Application
Version=1.0
Name=VoxChat
GenericName=Party voice chat
Comment=Party voice chat with your friends
Exec=bash "$quoted"
Path=$here
Icon=$here/assets/voxchat.svg
Terminal=false
Categories=Network;Chat;
Keywords=party;voice;chat;
StartupNotify=true
StartupWMClass=voxchat
ENTRY
command -v update-desktop-database >/dev/null && update-desktop-database "$apps" 2>/dev/null || true
command -v kbuildsycoca6 >/dev/null && kbuildsycoca6 >/dev/null 2>&1 || true
echo "VoxChat added to your app launcher."
