# Python 3.13 — проект требует >=3.13 (см. pyproject.toml и .python-version)
FROM python:3.13-slim

# PYTHONDONTWRITEBYTECODE — контейнер не обрастает __pycache__ в рантайме;
# PYTHONUNBUFFERED       — прогресс «Запрос 1/10 ...» печатается сразу, без буферизации;
# UV_LINK_MODE=copy      — копировать пакеты из кэша uv вместо hardlink (надёжно на overlayfs);
# PATH                   — `python` из ENTRYPOINT резолвится в интерпретатор venv от `uv sync`.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# uv ставим из PyPI и им же разворачиваем зависимости строго по uv.lock
RUN pip install --no-cache-dir uv

# Зависимости отдельным слоем: он пересобирается только при изменении pyproject.toml/uv.lock
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY main.py ./

# Запуск от непривилегированного пользователя
RUN useradd --create-home --uid 1000 app && chown -R app:app /app
USER app

# Адрес можно передать аргументами: docker compose run --rm speed-test <url> -n 5
# Если адрес не передан — скрипт спросит его в консоли.
ENTRYPOINT ["python", "main.py"]
