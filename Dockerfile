FROM mcr.microsoft.com/playwright/python:v1.49.1-jammy

WORKDIR /app

# Copy requirement files
COPY requirements.txt .

# Install system dependencies
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

# Install python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browsers and OS dependencies
RUN playwright install chromium
RUN playwright install-deps chromium

# Copy all source files
COPY . .

# Expose port for API and Gradio UI
EXPOSE 7860

# Command to run the application (Gradio + FastAPI backend)
CMD ["python", "backend.py"]
