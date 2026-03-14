#!/bin/bash

# Start Virtual Framebuffer
Xvfb :1 -screen 0 $RESOLUTION &
sleep 2

# Start Desktop Environment
startxfce4 &
sleep 2

# Start VNC Server
x11vnc -display :1 -forever -nopw -rfbport 5900 -shared &

# Start AI Agent
python3 /home/controluser/ai_agent.py &

# Start noVNC proxy
/opt/novnc/utils/novnc_proxy --vnc localhost:5900 --listen 6080
