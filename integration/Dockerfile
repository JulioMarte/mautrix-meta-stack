FROM python:3.13-alpine

WORKDIR /app
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY . /app

ENV PYTHONUNBUFFERED=1
EXPOSE 8080
# Access logs are intentionally disabled because webhook/proxy resolver routes contain deployment secrets.
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--threads", "4", "--timeout", "70", "--error-logfile", "-", "final_app:application"]
