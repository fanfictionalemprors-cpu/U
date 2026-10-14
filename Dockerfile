FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=1
ENV OMP_NUM_THREADS=2
ENV MKL_NUM_THREADS=2

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    git \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --upgrade pip

COPY requirements.txt .

RUN pip install -r requirements.txt

RUN pip install "pocket-tts @ git+https://github.com/kyutai-labs/pocket-tts@0c2db3b"

RUN pip install torch --index-url https://download.pytorch.org/whl/cpu

RUN pip install --upgrade huggingface_hub

RUN hf download Indus-Labs/indus-pocket-tts \
    --local-dir /app/indus-pocket-tts

COPY bot.py .

ENV TTS_MODEL_DIR=/app/indus-pocket-tts

CMD ["python", "-u", "bot.py"]
