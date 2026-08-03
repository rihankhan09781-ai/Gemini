# ── Free Gemini Pro Referral Bot — Railway Deployment ───────────────────────
FROM python:3.12-slim

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy bot source code
COPY . ./

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Run the bot in polling mode
CMD ["python", "main.py"]
