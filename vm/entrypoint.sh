#!/bin/bash
set -e

echo "[entrypoint] ==========================================="
echo "[entrypoint] Control VM Starting..."
echo "[entrypoint] ==========================================="

# Cleanup any existing locks
sudo rm -rf /tmp/.X1-lock /tmp/.X11-unix/X1 || true

# Start D-Bus session
export $(dbus-launch)
echo "[entrypoint] D-Bus session started"

# Start Xvfb (virtual display)
Xvfb :1 -screen 0 ${RESOLUTION:-1280x800x24} -ac +extension GLX +render -noreset &
sleep 2
echo "[entrypoint] Xvfb started on :1"

# Start Desktop Environment
export DISPLAY=:1
startxfce4 &
sleep 5
echo "[entrypoint] XFCE4 desktop started"

# Disable screensaver, power management, and locking at multiple levels
xset s off || true
xset -dpms || true
xset s noblank || true
xfconf-query -c xfce4-screensaver -p /saver/enabled -n -t bool -s false || true
xfconf-query -c xfce4-screensaver -p /lock-screen-status -n -t bool -s false || true
xfconf-query -c xfce4-power-manager -p /xfce4-power-manager/lock-screen-suspend-hibernate -n -t bool -s false || true
xfconf-query -c xfce4-power-manager -p /xfce4-power-manager/power-button-action -n -t int -s 3 || true
gsettings set org.gnome.desktop.screensaver lock-enabled false || true
echo "[entrypoint] Screen saver and power management disabled"

# Mark desktop files as trusted
for desktop_file in /home/controluser/Desktop/*.desktop; do
    [ -f "$desktop_file" ] || continue
    gio set "$desktop_file" metadata::trusted true || true
    chmod +x "$desktop_file" || true
done

# Enable desktop icons and configure them in XFCE
xfconf-query -c xfce4-desktop -p /desktop-icons/style -n -t int -s 2 || true
xfconf-query -c xfce4-desktop -p /desktop-icons/show-trash -n -t bool -s true || true
xfconf-query -c xfce4-desktop -p /desktop-icons/show-removable -n -t bool -s true || true
xfconf-query -c xfce4-desktop -p /desktop-icons/show-home -n -t bool -s true || true
echo "[entrypoint] Desktop icons configured"

# Set a random wallpaper dynamically across any active XFCE monitor
WALLPAPERS=("/home/controluser/wallpaper1.png" "/home/controluser/wallpaper2.png" "/home/controluser/wallpaper3.png")
SELECTED_WP=${WALLPAPERS[$RANDOM % 3]}
(sleep 5 && for prop in $(xfconf-query -c xfce4-desktop -p /backdrop -l | grep last-image); do xfconf-query -c xfce4-desktop -p $prop -s "$SELECTED_WP"; done) &

# Start VNC Server (retry until X is ready)
echo "[entrypoint] Starting VNC server..."
for i in $(seq 1 10); do
    x11vnc -display :1 -forever -nopw -rfbport 5900 -shared -localhost &
    VNC_PID=$!
    sleep 1
    if kill -0 $VNC_PID 2>/dev/null; then
        echo "[entrypoint] x11vnc started successfully on attempt $i (port 5900)"
        break
    fi
    echo "[entrypoint] x11vnc failed on attempt $i, retrying..."
    sleep 1
done

# Verify VNC is listening
netstat -tlnp 2>/dev/null | grep -E "5900|6080" || ss -tlnp | grep -E "5900|6080" || echo "[entrypoint] Note: netstat/ss not available, but VNC should be running"

echo "[entrypoint] Starting AI Agent WebSocket server..."

# Start AI Agent - it listens on port 8080
python3 /home/controluser/ai_agent.py &
AGENT_PID=$!
sleep 2
if kill -0 $AGENT_PID 2>/dev/null; then
    echo "[entrypoint] AI Agent started successfully (PID $AGENT_PID, port 8080)"
else
    echo "[entrypoint] WARNING: AI Agent may have failed to start"
fi

# Verify WebSocket server is listening
sleep 1
netstat -tlnp 2>/dev/null | grep -E "8080|8081" || ss -tlnp | grep -E "8080|8081" || echo "[entrypoint] Note: netstat/ss not available, but agent should be listening"

echo "[entrypoint] All services started. Container ready."
echo "[entrypoint] Ports: VNC=5900, noVNC=6080, Agent=8080"
echo "[entrypoint] ==========================================="

# Enable X access for pyautogui/automation
export DISPLAY=:1
xhost +local:root 2>/dev/null || xhost + 2>/dev/null || true
echo "[entrypoint] X access control configured"

# Start noVNC proxy (foreground — keeps the container alive)
while true; do
    echo "[entrypoint] Starting noVNC proxy..."
    /opt/novnc/utils/novnc_proxy --vnc localhost:5900 --listen 6080 --web /opt/novnc || true
    echo "[entrypoint] noVNC exited, restarting in 2s..."
    sleep 2
done
