FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV HOST=0.0.0.0 SCREENING_DB=/data/screening.db
# Optional: set APP_PASSWORD in the host's dashboard to require a login. Without it the site is open to anyone with the address.
CMD ["python", "app.py"]
