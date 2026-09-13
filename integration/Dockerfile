FROM python:3.13-alpine

WORKDIR /app
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py ./app.py

ENV PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["python", "/app/app.py"]
