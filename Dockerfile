FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy bot
COPY tournament_bot.py .
COPY run_bot.sh .
RUN chmod +x run_bot.sh

# Run with auto-restart wrapper
CMD ["bash", "run_bot.sh"]
