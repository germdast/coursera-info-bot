FROM python:3.11-slim

# Security + smaller image
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY src ./src
COPY README.md ./

# Run
CMD ["python", "-m", "src.bot"]
