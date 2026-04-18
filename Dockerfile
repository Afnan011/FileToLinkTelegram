FROM python:3.11-slim

WORKDIR /app

# Copy only requirements first for better layer caching
COPY requirements.txt .
RUN pip install --upgrade pip && pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application
COPY . .

CMD ["python", "-m", "FileStream"]