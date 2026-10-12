FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# dev — для app; dev,ml — для воркера с эмбеддингами.
ARG EXTRAS=dev

WORKDIR /app

# CPU-сборка torch: без CUDA образ меньше на несколько гигабайт.
RUN if echo "$EXTRAS" | grep -q ml; then \
      pip install torch --index-url https://download.pytorch.org/whl/cpu; \
    fi

COPY pyproject.toml ./
COPY app/__init__.py app/__init__.py
RUN pip install -e ".[$EXTRAS]"

COPY . .
