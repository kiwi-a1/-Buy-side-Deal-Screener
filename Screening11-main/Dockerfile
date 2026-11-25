FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV HOST=0.0.0.0 SCREENING_DB=/data/screening.db
# APP_PASSWORD must be set in the host's dashboard; the app refuses to start on a public address without it.
CMD ["python", "app.py"]
