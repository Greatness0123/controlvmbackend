# Use a lightweight Ubuntu base
FROM ubuntu:22.04

# Prevent interactive prompts
ENV DEBIAN_FRONTEND=noninteractive

# Install essential desktop components
RUN apt-get update && apt-get install -y \
    xfce4 \
    xfce4-goodies \
    xvfb \
    x11vnc \
    novnc \
    websockify \
    python3 \
    curl \
    git \
    sudo \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Create a non-root user
RUN useradd -m -s /bin/bash controluser && echo "controluser:control" | chpasswd && adduser controluser sudo
RUN echo 'controluser ALL=(ALL) NOPASSWD:ALL' >> /etc/sudoers

# Set up noVNC
RUN mkdir -p /opt/novnc && \
    git clone https://github.com/novnc/noVNC.git /opt/novnc && \
    ln -s /opt/novnc/vnc.html /opt/novnc/index.html

# Expose noVNC port
EXPOSE 6080

# Set up working directory
WORKDIR /home/controluser

# Entrypoint script to start services
COPY entrypoint.sh /home/controluser/entrypoint.sh
RUN chown controluser:controluser /home/controluser/entrypoint.sh && chmod +x /home/controluser/entrypoint.sh

USER controluser

# Environment variables for VNC
ENV DISPLAY=:1
ENV RESOLUTION=1280x800x24

ENTRYPOINT ["/home/controluser/entrypoint.sh"]
