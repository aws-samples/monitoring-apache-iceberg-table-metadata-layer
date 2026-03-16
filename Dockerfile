FROM python:3.10-slim

WORKDIR /app

COPY src/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ /app/src/

ENV PYTHONPATH=/app

CMD ["python", "src/main.py"]
