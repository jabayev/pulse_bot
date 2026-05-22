FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY config.py monitor.py notifier.py main.py .

CMD ["python", "main.py"]
