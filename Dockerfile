FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# dev — для app; dev,ml — для воркера с эмбеддингами.
ARG EXTRAS=dev

WORKDIR /app

# PDF-отчёты (WeasyPrint): Pango для вёрстки и шрифты с кириллицей.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0 fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# CPU-сборка torch: без CUDA образ меньше на несколько гигабайт.
RUN if echo "$EXTRAS" | grep -q ml; then \
      pip install torch --index-url https://download.pytorch.org/whl/cpu; \
    fi

COPY pyproject.toml ./
COPY app/__init__.py app/__init__.py
RUN pip install -e ".[$EXTRAS]"

COPY . .
