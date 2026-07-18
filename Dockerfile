FROM mcr.microsoft.com/playwright/python:v1.40.0-jammy

WORKDIR /app

# Copy requirement files
COPY requirements.txt .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy all source files
COPY . .

# Expose port (Render injects PORT env var)
EXPOSE 8000

# Command to run the application
CMD ["sh", "-c", "uvicorn backend:app --host 0.0.0.0 --port ${PORT:-8000}"]
