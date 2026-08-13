FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY watcher.py .
COPY parser.py .
COPY sessions.py .
COPY exporter.py .
COPY schema.sql .
CMD ["python", "watcher.py"]
