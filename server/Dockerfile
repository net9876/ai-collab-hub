# syntax=docker/dockerfile:1
FROM python:3.13-slim AS build
WORKDIR /src
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
COPY pyproject.toml ./
COPY src ./src
RUN python -m pip wheel --wheel-dir /wheels .

FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8000
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app
COPY --from=build /wheels /wheels
RUN python -m pip install --no-cache-dir --no-index --find-links=/wheels collab-hub \
    && rm -rf /wheels
USER 10001
EXPOSE 8000
CMD ["python", "-m", "collab_hub"]
