FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    OMP_NUM_THREADS=2 \
    MKL_NUM_THREADS=2 \
    TOKENIZERS_PARALLELISM=false

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libsndfile1 \
    git \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --upgrade pip setuptools wheel

RUN python -m pip install \
    torch \
    --index-url https://download.pytorch.org/whl/cpu

COPY requirements.txt .

RUN python -m pip install -r requirements.txt

RUN python -m pip install \
    "git+https://github.com/huggingface/parler-tts.git"

RUN python -m pip check

COPY bot.py .

CMD ["python", "-u", "bot.py"]
