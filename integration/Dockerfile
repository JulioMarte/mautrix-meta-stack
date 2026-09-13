FROM python:3.13-alpine

WORKDIR /app

# Keep the Coolify build lightweight and less sensitive to transient package-index
# stalls or low-memory hosts. Skipping bytecode compilation reduces the peak work
# performed by pip; retries/timeouts make network hiccups fail less eagerly.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

COPY requirements.txt ./requirements.txt
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install \
      --no-compile \
      --retries 5 \
      --timeout 60 \
      -r requirements.txt

COPY . /app

EXPOSE 8080
# NiceGUI runs on its bundled Uvicorn server with one process; access logs are disabled
# because the Chatwoot webhook path contains a deployment secret.
CMD ["python", "/app/nicegui_app.py"]
