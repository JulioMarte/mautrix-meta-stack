FROM python:3.13-alpine

WORKDIR /app
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY . /app

ENV PYTHONUNBUFFERED=1
EXPOSE 8080
# NiceGUI runs on its bundled Uvicorn server with one process; access logs are disabled
# because the Chatwoot webhook path contains a deployment secret.
CMD ["python", "/app/nicegui_app.py"]
