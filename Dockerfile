FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
RUN mkdir -p data
ENV PORT=5000
CMD ["sh", "-c", "gunicorn -b 0.0.0.0:$PORT app:app"]
