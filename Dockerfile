FROM python:3.12-slim

# THETA_PORT: the port the API listens on inside the container (publish the same one).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    THETA_PORT=5024

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY alembic.ini .
COPY migrations ./migrations
COPY app ./app

RUN useradd --create-home --uid 10001 theta && chown -R theta /app
USER theta

EXPOSE 5024

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os, sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://localhost:' + os.environ.get('THETA_PORT', '5024') + '/health', timeout=3).status == 200 else 1)"

# Migrations run first, so a container never serves against an out-of-date schema.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port ${THETA_PORT:-5024} --workers 2"]
