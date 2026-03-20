#!/bin/bash
set -e

# Start Xvfb (virtual display)
Xvfb :1 -screen 0 ${RESOLUTION:-1280x800x24} &
sleep 1

# Start Desktop Environment
export DISPLAY=:1
startxfce4 &
sleep 5

# Disable screensaver, power management, and locking at multiple levels
xset s off || true
xset -dpms || true
xset s noblank || true
xfconf-query -c xfce4-screensaver -p /saver/enabled -n -t bool -s false || true
xfconf-query -c xfce4-screensaver -p /lock-screen-status -n -t bool -s false || true
xfconf-query -c xfce4-power-manager -p /xfce4-power-manager/lock-screen-suspend-hibernate -n -t bool -s false || true
xfconf-query -c xfce4-power-manager -p /xfce4-power-manager/power-button-action -n -t int -s 3 || true
gsettings set org.gnome.desktop.screensaver lock-enabled false || true

# Mark desktop files as trusted
gio set /home/controluser/Desktop/firefox.desktop metadata::trusted true || true
chmod +x /home/controluser/Desktop/firefox.desktop

# Set a random wallpaper dynamically across any active XFCE monitor
WALLPAPERS=("/home/controluser/wallpaper1.png" "/home/controluser/wallpaper2.png" "/home/controluser/wallpaper3.png")
SELECTED_WP=${WALLPAPERS[$RANDOM % 3]}
(sleep 5 && for prop in $(xfconf-query -c xfce4-desktop -p /backdrop -l | grep last-image); do xfconf-query -c xfce4-desktop -p $prop -s "$SELECTED_WP"; done) &

# Start VNC Server (retry until X is ready)
for i in $(seq 1 10); do
    x11vnc -display :1 -forever -nopw -rfbport 5900 -shared &
    VNC_PID=$!
    sleep 1
    if kill -0 $VNC_PID 2>/dev/null; then
        echo "[entrypoint] x11vnc started successfully on attempt $i"
        break
    fi
    echo "[entrypoint] x11vnc failed on attempt $i, retrying..."
    sleep 1
done

# Start AI Agent
python3 /home/controluser/ai_agent.py &
echo "[entrypoint] AI Agent started"

# Start noVNC proxy (foreground — keeps the container alive)
# If noVNC exits, loop to restart it so the container never dies
while true; do
    echo "[entrypoint] Starting noVNC proxy..."
    /opt/novnc/utils/novnc_proxy --vnc localhost:5900 --listen 6080 || true
    echo "[entrypoint] noVNC exited, restarting in 2s..."
    sleep 2
done
