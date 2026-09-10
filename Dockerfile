FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.6 /uv /uvx /bin/

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY transcribe.py .

# Create the PEP 723 script environment without running the model download.
RUN uv sync --script transcribe.py

# The script defaults to 127.0.0.1 because it has no auth; a container must bind wider to be reachable.
ENV HOST=0.0.0.0
ENV HF_HOME=/models

VOLUME ["/models"]
EXPOSE 8000

# Lower priority keeps the host responsive during transcription.
CMD ["nice", "-n", "19", "uv", "run", "--offline", "--script", "transcribe.py"]
