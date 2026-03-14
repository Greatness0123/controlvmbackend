#!/bin/bash
set -e

# Start Xvfb (virtual display)
Xvfb :1 -screen 0 ${RESOLUTION:-1280x800x24} &
sleep 1

# Start Desktop Environment
export DISPLAY=:1
startxfce4 &
sleep 2

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
