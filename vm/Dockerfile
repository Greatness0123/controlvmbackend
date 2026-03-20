# Use a lightweight Ubuntu base
FROM ubuntu:22.04

# Prevent interactive prompts
ENV DEBIAN_FRONTEND=noninteractive

# Install essential desktop components
RUN apt-get update && apt-get install -y \
    xfce4 \
    xfce4-terminal \
    mousepad \
    xvfb \
    x11vnc \
    novnc \
    websockify \
    python3 \
    python3-pip \
    python3-tk \
    python3-dev \
    scrot \
    curl \
    git \
    sudo \
    software-properties-common \
    && add-apt-repository -y ppa:mozillateam/ppa \
    && echo 'Package: *\nPin: release o=LP-PPA-mozillateam\nPin-Priority: 1001\n' > /etc/apt/preferences.d/mozilla-firefox \
    && apt-get update && apt-get install -y firefox \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

RUN pip3 install pyautogui websockets mss Pillow

# Create a non-root user
RUN useradd -m -s /bin/bash controluser && echo "controluser:control" | chpasswd && adduser controluser sudo
RUN echo 'controluser ALL=(ALL) NOPASSWD:ALL' >> /etc/sudoers

# Set up Firefox Desktop Icon and Disable Lock Screen
RUN mkdir -p /home/controluser/Desktop && \
    echo '[Desktop Entry]\nVersion=1.0\nName=Firefox\nExec=firefox\nIcon=firefox\nType=Application\nCategories=Network;WebBrowser;' > /home/controluser/Desktop/firefox.desktop && \
    chmod +x /home/controluser/Desktop/firefox.desktop && \
    chown -R controluser:controluser /home/controluser/Desktop

# Disable screensaver and power management prompts
RUN mkdir -p /home/controluser/.config/xfce4/xfconf/xfce-perchannel-xml && \
    echo '<?xml version="1.0" encoding="UTF-8"?><channel name="xfce4-screensaver" version="1.0"><property name="saver" type="empty"><property name="enabled" type="bool" value="false"/></property></channel>' > /home/controluser/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-screensaver.xml && \
    chown -R controluser:controluser /home/controluser/.config

# Set up noVNC
RUN mkdir -p /opt/novnc && \
    git clone https://github.com/novnc/noVNC.git /opt/novnc && \
    ln -s /opt/novnc/vnc.html /opt/novnc/index.html

# Expose noVNC and Agent ports
EXPOSE 6080
EXPOSE 8080

# Set up working directory
WORKDIR /home/controluser

# Entrypoint and Agent scripts
COPY entrypoint.sh /home/controluser/entrypoint.sh
COPY ai_agent.py /home/controluser/ai_agent.py
COPY wallpaper1.png /home/controluser/wallpaper1.png
COPY wallpaper2.png /home/controluser/wallpaper2.png
COPY wallpaper3.png /home/controluser/wallpaper3.png
RUN chown controluser:controluser /home/controluser/entrypoint.sh /home/controluser/ai_agent.py /home/controluser/wallpaper*.png && \
    chmod +x /home/controluser/entrypoint.sh

USER controluser

# Environment variables for VNC
ENV DISPLAY=:1
ENV RESOLUTION=1920x1080x24

ENTRYPOINT ["/home/controluser/entrypoint.sh"]
