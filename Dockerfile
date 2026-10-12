FROM python:3.11-slim
ENV PIPER_MODEL=/app/models/hi_IN-priyamvada-medium.onnx \
    PIPER_CONFIG=/app/models/hi_IN-priyamvada-medium.onnx.json \
    PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg curl ca-certificates libespeak-ng1 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN mkdir -p /app/models && \
    curl -fL --retry 5 --retry-delay 2 'https://huggingface.co/rhasspy/piper-voices/resolve/main/hi/hi_IN/priyamvada/medium/hi_IN-priyamvada-medium.onnx' -o /app/models/hi_IN-priyamvada-medium.onnx && \
    curl -fL --retry 5 --retry-delay 2 'https://huggingface.co/rhasspy/piper-voices/resolve/main/hi/hi_IN/priyamvada/medium/hi_IN-priyamvada-medium.onnx.json' -o /app/models/hi_IN-priyamvada-medium.onnx.json
COPY bot.py .
CMD ["python", "bot.py"]
