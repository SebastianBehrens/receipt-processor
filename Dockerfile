FROM python:3.12-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1
ENV DJANGO_SETTINGS_MODULE config.settings

# Set work directory
WORKDIR /app

# Install system dependencies
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        vim \
        less \
        curl \
        && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy project
COPY . .

# Make entrypoint script executable
RUN chmod +x entrypoint.sh

# Create directories for data and logs
RUN mkdir -p data/database logs static staticfiles

# Set default environment variables for build
ENV SECRET_KEY=build-only-secret-key
ENV SERVER_ROOT_URL=https://localhost:8000

# Collect static files
RUN python manage.py collectstatic --noinput

# Ensure proper permissions for user 1002:1002 (set by docker-compose)
RUN chmod -R 755 /app && \
    chmod -R 777 /app/data /app/logs /app/staticfiles

# Expose port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=30s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8000/admin/ || exit 1

# Use entrypoint script
ENTRYPOINT ["./entrypoint.sh"] 