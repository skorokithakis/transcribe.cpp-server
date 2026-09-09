#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["transcribe-cpp", "huggingface-hub", "flask", "waitress"]
# ///

import os
import subprocess
import tempfile
import threading
import time

import transcribe_cpp
from flask import Flask, jsonify, request
from huggingface_hub import hf_hub_download
from waitress import serve
from werkzeug.exceptions import HTTPException, RequestEntityTooLarge

# Parakeet has no audio length limit. Cohere Transcribe and Canary 180M were
# considered and rejected: both are encoder-bound and reject audio longer than
# about 400 s (6.7 min) with InputTooLong. Keep that in mind before swapping.
model_path = os.environ.get("TRANSCRIBE_MODEL") or hf_hub_download(
    repo_id=os.environ.get("MODEL_REPO", "handy-computer/parakeet-unified-en-0.6b-gguf"),
    filename=os.environ.get("MODEL_FILE", "parakeet-unified-en-0.6b-Q5_K_M.gguf"),
)
idle_timeout = float(os.environ.get("IDLE_TIMEOUT", "300"))
model_lock = threading.Lock()
model = None
last_used = 0.0

MAX_UPLOAD_BYTES = 100 * 1024 * 1024

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES


@app.errorhandler(RequestEntityTooLarge)
def upload_too_large(_error):
    return jsonify(error=f"upload exceeds {MAX_UPLOAD_BYTES // 1024 // 1024} MB"), 413


@app.errorhandler(Exception)
def internal_error(error):
    if isinstance(error, HTTPException):
        return jsonify(error=error.description), error.code
    app.logger.exception("Unhandled exception")
    return jsonify(error="internal server error"), 500


def unload_idle_model():
    global model
    while True:
        time.sleep(30)
        if idle_timeout > 0:
            with model_lock:
                if model is not None and time.monotonic() - last_used > idle_timeout:
                    model.close()
                    model = None


threading.Thread(target=unload_idle_model, daemon=True).start()


@app.post("/transcribe")
def transcribe():
    global model, last_used

    if "file" not in request.files:
        return jsonify(error="missing file upload"), 400

    upload = request.files["file"]
    path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False) as temporary_file:
            path = temporary_file.name
            upload.save(temporary_file)
            if temporary_file.tell() == 0:
                return jsonify(error="empty upload"), 400
        decoded = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", path, "-f", "f32le", "-ac", "1", "-ar", "16000", "-"],
            capture_output=True,
            check=False,
        )
    finally:
        if path is not None:
            os.unlink(path)

    if decoded.returncode:
        app.logger.error("ffmpeg failed to decode audio: %s", decoded.stderr.decode(errors="replace"))
        return jsonify(error="unable to decode audio"), 400

    # One lock covers load, run and unload. The library allows at most one run
    # in flight per Model, so concurrent runs would race, and an unload during
    # a run would be a use-after-free. Requests queue here; that is intended.
    with model_lock:
        if model is None:
            model = transcribe_cpp.Model(model_path)
            last_used = time.monotonic()
        with model.session() as session:
            result = session.run(decoded.stdout)
        last_used = time.monotonic()
    return jsonify(text=result.text)


if __name__ == "__main__":
    serve(app, host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8000")))
