FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY bot.py /app/bot.py

# Menjalankan bot tanpa root.
RUN useradd --create-home --shell /usr/sbin/nologin botuser
USER botuser

CMD ["python", "-u", "/app/bot.py"]
