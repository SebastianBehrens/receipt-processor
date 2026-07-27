# Use Python 3.11 slim image as base
FROM python:3.11-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV DJANGO_SETTINGS_MODULE=config.settings
ENV BASE_DIR=/app
ENV SECRET_KEY=build-only-not-for-production
ENV DEBUG=False
ENV ALLOWED_HOSTS=localhost

# Set work directory
WORKDIR /app

# Install system dependencies
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        curl \
        vim \
        less \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first to leverage Docker layer caching
COPY requirements.txt /app/

# Install Python dependencies
RUN pip install --upgrade pip \
    && pip install -r requirements.txt

# Copy project files
COPY . /app/

# Create staticfiles directory for Django static files
RUN mkdir /app/static

# Collect static files
RUN python manage.py collectstatic --noinput

# Homelab containers run as docker-user (UID/GID 3002)
ARG APP_UID=3002
ARG APP_GID=3002
RUN groupadd --gid ${APP_GID} docker-user \
    && useradd --uid ${APP_UID} --gid ${APP_GID} --create-home --shell /bin/bash docker-user \
    && chown -R ${APP_UID}:${APP_GID} /app
USER ${APP_UID}:${APP_GID}

# Expose port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=30s --start-period=30s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Run the application
CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"] 