# Sherlock Web Interface - Render (free tier) friendly image
# Runs the multi-search Flask web app; sherlock runs as an internal subprocess.
FROM python:3.12-slim

WORKDIR /app

# Parallel HTTP concurrency of the sherlock subprocess. The OOM that killed
# searches ~90s in was fixed by releasing each site's future after processing
# (peak RSS now ~100MB flat on the free instance); 6 workers balances speed
# (~45-60s per full 492-site search) against in-flight response buffering.
ENV SHERLOCK_MAX_WORKERS=6
ENV PYTHONUNBUFFERED=1

# System deps are minimal; copy requirements first to benefit from layer cache.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Whole repo: web_app.py, templates/, config/, sherlock_project/ (with data.json).
COPY . .

# Own the cache dir so we can write search-result cache at runtime.
RUN mkdir -p /app/cache && chmod -R a+rwX /app

# PYTHONPATH = /app so `python -m sherlock_project.sherlock` uses THIS repo's
# data.json (with the extra sites), never a stray installed copy.
ENV PYTHONPATH=/app

# Run as a non-root user (Render free spins up as a normal user with the
# container's image entrypoint/user).
USER 10001

EXPOSE 5000

CMD ["sh", "-c", "python web_app.py"]