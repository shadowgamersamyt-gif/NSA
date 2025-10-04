# Use official Python image
FROM python:3.13-slim

# Set working directory
WORKDIR /app

# Copy all files
COPY . /app

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Start bot
CMD ["python", "bot.py"]
