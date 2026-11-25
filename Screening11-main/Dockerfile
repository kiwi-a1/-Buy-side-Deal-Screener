FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV HOST=0.0.0.0 SCREENING_DB=/data/screening.db
# No login by default. To require one set REQUIRE_LOGIN=1 and APP_PASSWORD in the host's dashboard.
CMD ["python", "app.py"]
