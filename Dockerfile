FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Copy example config if no config exists
RUN cp -n config.example.yaml config.yaml || true

EXPOSE 8080

CMD ["python3", "webapp.py"]
