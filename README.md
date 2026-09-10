# transcribe.cpp-server

An HTTP server that transcribes an uploaded audio file and returns the text as JSON.

By default, speech recognition runs locally with
[transcribe.cpp](https://pypi.org/project/transcribe-cpp/) and a Parakeet model, and no
audio leaves the machine. The operator can instead point the server at OpenAI, and then
every request is transcribed there. One engine or the other handles everything; there is
no fallback and the caller cannot choose. See [Cloud engine](#cloud-engine).

The default local model is English only. Point `MODEL_REPO` and `MODEL_FILE` at another
model for other languages.

An optional second pass sends the finished transcript to a large language model,
which tidies it and can correct names you supply. It is off unless you configure an
endpoint and the caller asks for it. See [Postprocessing](#postprocessing).

## Requirements

- `uv`
- `ffmpeg` on `PATH`

## Run

```bash
./transcribe.py
```

The server listens on `127.0.0.1:8000`. On first start it downloads the model, about
541 MB, and does not open the port until the download finishes. With the
[cloud engine](#cloud-engine) there is no model, so it starts at once.

## Use

```bash
curl --max-time 600 -F file=@audio.opus http://127.0.0.1:8000/transcribe
```

```json
{"text": "...", "processed_text": null, "engine": "local"}
```

`text` is always the raw transcript. `processed_text` holds the tidied version, or
`null` when postprocessing did not run. See [Postprocessing](#postprocessing).
`engine` is `local` or `cloud`, and says which recogniser produced `text`. See
[Cloud engine](#cloud-engine).

Any format `ffmpeg` can decode is accepted.

| Status | Body | Cause |
| --- | --- | --- |
| 200 | `{"text": "...", "processed_text": ..., "engine": ...}` | Success |
| 400 | `{"error": "missing file upload"}` | No `file` field in the form |
| 400 | `{"error": "empty upload"}` | The uploaded file is empty |
| 400 | `{"error": "unable to decode audio"}` | `ffmpeg` could not decode it. The reason is in the server log |
| 413 | `{"error": "upload exceeds 100 MB"}` | Upload over the size limit |
| 413 | `{"error": "audio too long for the cloud engine"}` | Cloud engine only. Still over 25 MB after re-encoding. See [Cloud engine](#cloud-engine) |
| 500 | `{"error": "internal server error"}` | Unexpected failure. The traceback is in the server log |
| 502 | `{"error": "transcription failed"}` | Cloud engine only. The API was unreachable, refused the request, or answered with something unreadable. The reason is in the server log |

Other errors, such as 404 for an unknown path and 405 for the wrong method, also
return a JSON body.

An upload above 1 GB is refused by waitress before the application runs, so it gets a
plain text 413 instead of a JSON one. This is deliberate. Matching the JSON body would
mean writing a gigabyte to disk before rejecting it.

With the local engine there is no limit on audio length other than the upload size cap.
The cloud engine limits what it uploads to 25 MB instead, which is about 1.8 hours once
re-encoded, and can be longer for an already compact file that is uploaded unchanged.
See [What gets uploaded](#what-gets-uploaded).

## Cloud engine

Local transcription is private but slow on a modest CPU. Setting `CLOUD_API_KEY` switches
the server to OpenAI's transcription API instead, which is much faster and understands
more languages than the default local model.

```bash
export CLOUD_API_KEY=sk-your-key-here
./transcribe.py
```

**This sends the audio itself to OpenAI.** Read that before you set the variable. The
choice belongs to the operator and is deliberately not exposed to callers, so:

- It applies to *every* request. A caller cannot ask for it and cannot opt out of it.
- There is no fallback in either direction. If the API fails, the request fails with a
  502. The local model is not used as a backstop.
- The `engine` field in each response says which recogniser ran, so a caller can at
  least tell where its audio went.

In cloud mode no local model is downloaded or loaded, so the server starts at once and
needs no model volume. `MODEL_REPO`, `MODEL_FILE`, `TRANSCRIBE_MODEL` and `IDLE_TIMEOUT`
have no effect. `ffmpeg` is still needed.

| Variable | Default | Meaning |
| --- | --- | --- |
| `CLOUD_API_KEY` | unset | Set it to use the cloud engine for every request. Unset means local. This is the only switch |
| `CLOUD_BASE_URL` | `https://api.openai.com/v1` | Base URL of an OpenAI-compatible API. `/audio/transcriptions` is appended |
| `CLOUD_MODEL` | `gpt-transcribe` | Model name sent to that API |

A server with no key cannot be selected, because the key is the switch. If you want a
recogniser on your own hardware, use the local engine.

### What gets uploaded

The API accepts files up to 25 MB, and only some formats. To stay inside both limits the
server may re-encode before uploading:

- An upload whose name ends in `.mp3`, `.mp4`, `.mpeg`, `.mpga`, `.m4a` or `.webm` and
  which is under 25 MB is uploaded unchanged, and `ffmpeg` never runs.
- Anything else is re-encoded to MP3, mono, 16 kHz, 32 kbps. That includes `.opus` and
  `.ogg`, which the API rejects outright, and `.wav`, which it accepts but which would
  waste tens of megabytes of upload for no gain in accuracy.

At that bitrate 25 MB is about 1.8 hours of audio, and 15 minutes is about 3.6 MB. If
the audio is still over 25 MB after re-encoding, the request gets a 413. Long recordings
are not split into chunks.

The upload is judged by the extension in its filename, because OpenAI decides the same
way. A file whose name disagrees with its contents is rejected by the API, which arrives
as a 502.

### Word biasing

In cloud mode the `vocabulary` field is also given to the recogniser, which prefers those
words while transcribing. This is better than correcting mistakes afterwards, because the
model uses them while it still has the audio.

```bash
curl --max-time 600 \
  -F file=@audio.opus \
  -F 'vocabulary=Stavros,Korokithakis,Harbormaster' \
  http://127.0.0.1:8000/transcribe
```

Note that `vocabulary` now does something on its own. Previously it was ignored unless
`postprocess=true` was also sent. Details, so nothing surprises you:

- Only the first 100 words are sent. A longer list makes the model start writing words
  nobody said.
- `<` and `>` are removed from each word. The API rejects the whole request if a keyword
  contains either.
- The local engine cannot do this at all, so `vocabulary` still only affects
  postprocessing there.
- Biasing needs a model that supports it. The default `gpt-transcribe` does.
  `whisper-1` does not, so with `CLOUD_MODEL=whisper-1` any request carrying a
  `vocabulary` fails with a 502.
- If you also send `postprocess=true`, the same words are used twice: once while
  transcribing and once while tidying up. That is intended.

## Postprocessing

Speech models write a wall of text. They do not punctuate well, they keep every "uh"
and "um", and they mangle names they have never seen. An optional second pass fixes
this by sending the finished transcript to a large language model.

The pass is off by default and needs two things to run: the operator sets
`LLM_BASE_URL` and `LLM_MODEL`, and the caller sends `postprocess=true`.

```bash
curl --max-time 600 \
  -F file=@audio.opus \
  -F postprocess=true \
  -F 'vocabulary=Stavros,Korokithakis,Harbormaster' \
  http://127.0.0.1:8000/transcribe
```

```json
{"text": "so stavros said uh we should ...", "processed_text": "So Stavros said we should ...", "engine": "local"}
```

| Field | Meaning |
| --- | --- |
| `postprocess` | `true` or `1` turns the pass on. Anything else, including leaving it out, turns it off |
| `vocabulary` | Optional. Words to prefer, separated by commas or newlines. Usually names, jargon, or product names. In cloud mode this also biases the recogniser itself, whether or not you ask for postprocessing. See [Word biasing](#word-biasing) |

The model is told to fix punctuation and capitalisation, break the text into
paragraphs, drop fillers and false starts, correct obvious mis-transcriptions, and
prefer your supplied words. It is told not to summarise, reword, or change the
meaning.

`text` always holds the raw transcript, so nothing you already rely on changes.
`processed_text` is `null`, and the status is still 200, in all of these cases:

- `postprocess` was not `true` or `1`
- `LLM_BASE_URL` or `LLM_MODEL` is not set
- the transcript is empty
- the transcript is longer than 30000 characters, which is far more than the
  workload this was built for. The cap exists so that an unexpected multi-hour
  upload cannot turn into a large bill
- the language model failed, timed out after 120 seconds, or answered with
  something unreadable

So a client should use `processed_text` when it is present and fall back to `text`
when it is `null`. There is no separate error to handle.

**The transcript leaves the machine when you use this.** Nothing is sent at all unless
a caller asks for the pass, and with the local engine the audio itself never leaves. But
the endpoint you configure receives the full text. If that matters, point `LLM_BASE_URL`
at a model running on your own hardware. Any server that speaks the OpenAI chat
completions API works, including Ollama, llama.cpp's server, and LM Studio, as well as
the hosted providers.

This pass is configured separately from the recogniser, and `LLM_BASE_URL` can point
somewhere completely unrelated to `CLOUD_BASE_URL`. Using the
[cloud engine](#cloud-engine) therefore does not make this pass consequence-free: it can
hand the transcript to a second provider that never saw the audio.

A language model can still ignore its instructions and reword something. Compare
`processed_text` against `text` if that would be a problem for you.

## Configuration

All configuration is by environment variable.

| Variable | Default | Meaning |
| --- | --- | --- |
| `HOST` | `127.0.0.1` | Bind address |
| `PORT` | `8000` | Bind port |
| `CLOUD_API_KEY` | unset | Set it to transcribe every request with the cloud engine instead of locally. See [Cloud engine](#cloud-engine) |
| `CLOUD_BASE_URL` | `https://api.openai.com/v1` | Base URL of an OpenAI-compatible API. `/audio/transcriptions` is appended |
| `CLOUD_MODEL` | `gpt-transcribe` | Transcription model name sent to that API |
| `IDLE_TIMEOUT` | `300` | Seconds of inactivity before the model is freed from memory. `0` keeps it loaded forever. Checked every 30 seconds, so the unload can be up to 30 seconds late. Local engine only |
| `MODEL_REPO` | `handy-computer/parakeet-unified-en-0.6b-gguf` | Hugging Face repository to download the model from. Local engine only |
| `MODEL_FILE` | `parakeet-unified-en-0.6b-Q5_K_M.gguf` | File to download from that repository. Local engine only |
| `TRANSCRIBE_MODEL` | unset | Path to a local `.gguf`. If set, it wins and Hugging Face is never contacted. Local engine only |
| `LLM_BASE_URL` | unset | Base URL of an OpenAI-compatible API, for example `https://api.openai.com/v1`. `/chat/completions` is appended. Unset disables postprocessing |
| `LLM_MODEL` | unset | Model name sent to that API. Unset disables postprocessing |
| `LLM_API_KEY` | unset | Sent as `Authorization: Bearer`. Leave unset for a local server that wants no key |
| `HF_HOME` | `~/.cache/huggingface` | Where the downloaded model is cached. Read by `huggingface_hub`, not by this server |
| `TMPDIR` | `/tmp` | Where uploads are staged before decoding. Read by Python, not by this server |

The default bind address is `127.0.0.1` because the server has no authentication.
Exposing it on a network is a deliberate act, so set `HOST` yourself.

The rest of this section describes the local engine. With `CLOUD_API_KEY` set, no model
is downloaded and none of it applies.

The model is downloaded once and cached. Later starts reuse the cached copy and
download again only if it is missing. To skip the cache validation request to
Hugging Face entirely, set `HF_HUB_OFFLINE=1` once the model is cached.

## Docker

```bash
docker build -t transcribe .
```

```bash
docker run -d -p 8000:8000 -v transcribe-models:/models transcribe
```

The image sets `HOST=0.0.0.0` and `HF_HOME=/models`. With the local engine, mount a
volume at `/models` or the model is downloaded again on every new container. The cloud
engine downloads nothing, so it needs no volume. Python dependencies are
installed at build time, so the first request does not wait for them. The image is
about 1.03 GB and does not contain the model.

## Compose

`docker-compose.yml` builds the image, mounts a `cache-models` volume at `/models`,
and publishes the port on the loopback interface.

```bash
docker compose up --build --detach
```

| Variable | Default | Meaning |
| --- | --- | --- |
| `PUBLISH_ADDR` | `127.0.0.1:8000` | Host address and port to publish. Read by Compose, not by the server |

`IDLE_TIMEOUT`, `MODEL_REPO`, `MODEL_FILE`, `CLOUD_BASE_URL`, `CLOUD_MODEL`,
`CLOUD_API_KEY`, `LLM_BASE_URL`, `LLM_MODEL` and `LLM_API_KEY` are passed through to the
container if you set them. The rest of the Configuration table is fixed by the image and
the port mapping.

With `CLOUD_API_KEY` set, no model is downloaded, so the `cache-models` volume is never
read or written. Leaving it mounted costs nothing, and anything an earlier local run put
there stays untouched.

A local language model on the host is not reachable at `127.0.0.1` from inside the
container. Use the host address that your Docker setup provides.

## Harbormaster

[Harbormaster](https://harbormaster.readthedocs.io/) finds `docker-compose.yml` on its
own, so an app stanza is all you need:

```yaml
apps:
  transcribe:
    url: https://github.com/skorokithakis/transcribe.cpp-server.git
    manage_volumes: true
    environment:
      PUBLISH_ADDR: "127.0.0.1:8000"
```

`manage_volumes: true` backs the model with a plain host directory at
`caches/transcribe/cache-models`. The volume name starts with `cache-`, so Harbormaster
treats the model as throwaway and deletes the directory if you remove the app from the
config. The next start downloads the model again, and the port stays closed until that
finishes, unless the cloud engine is configured, in which case there is no model to
download. The branch defaults to `master`, which is the branch this repository uses.

Harbormaster provides no reverse proxy, TLS or authentication, and neither does this
server, so the default publishes on loopback only. Widen `PUBLISH_ADDR` only behind
something that authenticates.

## Notes

With the local engine, speed depends heavily on the CPU. On a 2 core 15 W laptop chip,
an Intel i3-8109U, transcription runs at roughly 2.8x realtime: about 40 seconds for a
2 minute file and about 3.5 minutes for a 10 minute file. A modern many core desktop CPU
is several times faster. Measure your own hardware before relying on a number.

With the cloud engine none of that applies. The wait is the upload plus the API's own
response time, so the local CPU barely matters.

Postprocessing adds the language model's own response time on top, which is seconds
for a hosted API and much longer for a local model on a slow machine.

The server stops waiting on a stalled cloud transcription after 300 seconds, and on a
stalled postprocessing call after 120 seconds. Both apply to each network operation
rather than to the call as a whole, so a slow but steady response can outlast them.

Set client timeouts to suit. If you put a reverse proxy in front, raise its read
timeout too, since the defaults are usually 60 seconds. nginx needs
`proxy_read_timeout`.

The thread count is left at the library default, which uses every logical CPU. A
sweep from 1 to 4 threads on the machine above found nothing consistently faster, so
there is no setting for it. Local engine only.

With the local engine the server transcribes one file at a time. The speech recognition
library allows only one transcription in flight per loaded model, so concurrent requests
are queued rather than run in parallel. A caller can therefore wait for every request
ahead of it.

The cloud engine has no such limit, since the work happens elsewhere. Requests run
concurrently, up to however many threads waitress is running.

There is no authentication and no rate limiting. Do not expose it to an untrusted
network.

## License

GNU Affero General Public License v3.0. See [LICENSE](LICENSE).
