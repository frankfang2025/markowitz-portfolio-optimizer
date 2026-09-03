FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY markowitz_web.py markowitz_etf_optimizer_v2.py ./

# Job state lives in an in-memory dict guarded by a threading.Lock, and each
# optimization runs on a background thread. More than one worker process would
# put the job in one process and the status poll in another, so the run must
# stay single-process; concurrency comes from threads instead.
ENV PORT=8801
EXPOSE 8801
CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:${PORT} --workers 1 --threads 8 --timeout 600 markowitz_web:app"]
