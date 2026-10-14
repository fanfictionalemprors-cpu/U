FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    HF_HOME=/app/hf-cache \
    HF_HUB_CACHE=/app/hf-cache \
    TRANSFORMERS_CACHE=/app/hf-cache \
    CHUNK_CHARS=350 \
    TORCH_NUM_THREADS=2 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libsndfile1 \
    libgomp1 \
    libstdc++6 \
    git \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .

# Install a matching CPU-only PyTorch and torchaudio pair.
RUN pip install --no-cache-dir \
    torch==2.5.1 torchaudio==2.5.1 \
    --index-url https://download.pytorch.org/whl/cpu

RUN pip install --no-cache-dir -r requirements.txt

# Install the Parler-TTS library.
RUN pip install --no-cache-dir \
    git+https://github.com/huggingface/parler-tts.git

# Verify the native audio libraries before starting the bot.
RUN python -c "import torch, torchaudio, soundfile; print('Torch:', torch.__version__); print('Torchaudio:', torchaudio.__version__); print('Audio libraries OK')"

COPY bot.py .

CMD ["python", "bot.py"]
