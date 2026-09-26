FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY telegram_solidinvoice_bot.py .

# Long-polling bot: no incoming HTTP port is required.
CMD ["python", "-u", "telegram_solidinvoice_bot.py"]
