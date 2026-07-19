FROM mcr.microsoft.com/playwright/python:v1.49.1-jammy

WORKDIR /app

# Copy requirement files
COPY requirements.txt .

# Install system dependencies
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browsers and OS dependencies
RUN playwright install chromium
RUN playwright install-deps chromium

# Copy all source files
COPY . .

# Expose port for Hugging Face Spaces (defaults to 7860)
EXPOSE 7860

# Command to run the application
CMD ["sh", "-c", "uvicorn backend:app --host 0.0.0.0 --port ${PORT:-7860} --loop asyncio"]
