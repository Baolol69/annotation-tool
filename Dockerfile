FROM python:3.11-slim

WORKDIR /app

# Install system dependencies (ffmpeg for Audio)
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

# Copy requirement files
COPY requirements.txt .

# Install python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy all source files
COPY . .

# Expose port (Render sets this automatically)
EXPOSE $PORT

# Command to run the application (Uvicorn starts FastAPI which has Gradio mounted)
CMD ["sh", "-c", "uvicorn backend:app --host 0.0.0.0 --port ${PORT:-8000}"]
