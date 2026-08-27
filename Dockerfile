# Sherlock Web Interface - Render (free tier) friendly image
# Runs the multi-search Flask web app; sherlock runs as an internal subprocess.
FROM python:3.12-slim

WORKDIR /app

# Lower the sherlock subprocess's parallel HTTP concurrency so it stays within
# the 512MB free-tier RAM. 8 and 4 both ended with the instance restarting
# mid-search (~75-90s in); 2 is the diagnostic setting under test.
ENV SHERLOCK_MAX_WORKERS=2
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