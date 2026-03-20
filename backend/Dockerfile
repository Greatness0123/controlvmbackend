# Use Python 3.11 Slim Bookworm for stability
FROM python:3.11-slim-bookworm

# Set working directory
WORKDIR /app

# Install system dependencies (including Docker CLI to interact with host)
RUN apt-get update && apt-get install -y \
    curl \
    docker.io \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Expose backend port
EXPOSE 8000

# Run the application
CMD ["python", "main.py"]
