# transcribe.cpp-server

An HTTP server that transcribes an uploaded audio file and returns the text as JSON.

By default, speech recognition runs locally with
[transcribe.cpp](https://pypi.org/project/transcribe-cpp/) and a Parakeet model, and no
audio leaves the machine. The operator can instead point the server at Soniox, and then
every request is transcribed there. If the cloud is down or rate limited, the request
falls back to the local engine. The caller cannot choose which engine runs. See
[Cloud engine](#cloud-engine).

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
541 MB, and does not open the port until the download finishes. This happens in both
modes, because the [cloud engine](#cloud-engine) uses the local model as a fallback.

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
| 500 | `{"error": "internal server error"}` | Unexpected failure. The traceback is in the server log |
| 502 | `{"error": "transcription failed"}` | Cloud engine only. The API refused the request (4xx other than 429), the job failed, the request timed out, or the answer was unreadable. The reason is in the server log. 429, 5xx and connection failures fall back to the local engine instead |

Other errors, such as 404 for an unknown path and 405 for the wrong method, also
return a JSON body.

An upload above 1 GB is refused by waitress before the application runs, so it gets a
plain text 413 instead of a JSON one. This is deliberate. Matching the JSON body would
mean writing a gigabyte to disk before rejecting it.

With the local engine there is no limit on audio length other than the upload size cap.
The cloud engine adds no limit of its own, but Soniox refuses audio longer than 300
minutes, which arrives as a 502. See [What gets uploaded](#what-gets-uploaded).

## Cloud engine

Local transcription is private but slow on a modest CPU. Setting `SONIOX_API_KEY` switches
the server to the Soniox async API instead, which is much faster and detects the language
of each request automatically.

```bash
export SONIOX_API_KEY=snx-your-key-here
./transcribe.py
```

**This sends the audio itself to Soniox.** Read that before you set the variable. The
choice belongs to the operator and is deliberately not exposed to callers, so:

- It applies to *every* request. A caller cannot ask for it and cannot opt out of it.
- If the API answers with 429 or a 5xx, or the connection fails, the request is
  transcribed locally instead and the response has `engine` set to `local`. The audio
  then goes nowhere new, so the operator's choice of where audio goes still holds. The
  local model is English only, so a fallback cannot transcribe other languages.
- Any other failure, such as a 4xx, a job Soniox reports as failed, a timeout, or an
  unreadable answer, still fails with a 502. There is no local-to-cloud fallback.
- The `engine` field in each response says which recogniser ran, so a caller can at
  least tell where its audio went.

Each request uploads its audio, starts a transcription job, polls it about once a second,
and downloads the transcript. The server then tries to delete the audio and the job,
on success, on failure, and before a fallback runs locally. Soniox otherwise keeps uploaded audio for 30 days. If the
cleanup itself fails, the reason is in the server log and the caller still gets its
result.

Because of the fallback, cloud mode still downloads the local model at startup. The server
does not open the port until the download finishes, and it needs the same model volume
as a local run. The model is loaded into memory only when a request falls back, and
`IDLE_TIMEOUT` frees it again. `MODEL_REPO`, `MODEL_FILE`, `TRANSCRIBE_MODEL` and
`IDLE_TIMEOUT` therefore apply in cloud mode too. `ffmpeg` is needed in both modes.

A fallback loses vocabulary biasing. Local runs are serialized, so during a cloud outage
requests queue behind each other.

| Variable | Default | Meaning |
| --- | --- | --- |
| `SONIOX_API_KEY` | unset | Set it to use the cloud engine for every request. Unset means local. This is the only switch |

The model is fixed to `stt-async-v6`, with no setting for it. A server with no key
cannot be selected, because the key is the switch. If you want a recogniser on your own
hardware, use the local engine.

### What gets uploaded

Soniox accepts `aac`, `aiff`, `amr`, `asf`, `flac`, `mp3`, `ogg`, `opus`, `wav`, `webm`,
`m4a` and `mp4`. An upload whose filename ends in one of those is sent unchanged, and
`ffmpeg` never runs.

Anything else is re-encoded to MP3, mono, 16 kHz, 32 kbps, because Soniox does not list
it as supported. If `ffmpeg` cannot read the file either, the request fails with a 400. Long
recordings are not split into chunks.

The extension in the filename only decides whether the server re-encodes. Soniox detects
the format from the contents. If it cannot decode the file, the request fails with a 502.

### Word biasing

In cloud mode the `vocabulary` field is also given to the recogniser as context terms,
which makes it prefer those words while transcribing. This is better than correcting
mistakes afterwards, because the model uses them while it still has the audio.

```bash
curl --max-time 600 \
  -F file=@audio.opus \
  -F 'vocabulary=Stavros,Korokithakis,Harbormaster' \
  http://127.0.0.1:8000/transcribe
```

Note that `vocabulary` now does something on its own. Previously it was ignored unless
`postprocess=true` was also sent. Details, so nothing surprises you:

- Only the first 100 words are sent.
- `<` and `>` are removed from each word.
- The local engine cannot do this at all, so when a request falls back to it (or you
  run without `SONIOX_API_KEY`), `vocabulary` only affects postprocessing.
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
If `postprocess` was not `true` or `1`, `processed_text` is `null`.

If you asked for the pass, `processed_text` is always a string. It holds the raw
transcript, the same as `text`, and the status is still 200, in all of these cases:

- `LLM_BASE_URL` or `LLM_MODEL` is not set
- the transcript is empty
- the transcript is longer than 30000 characters, which is far more than the
  workload this was built for. The cap exists so that an unexpected multi-hour
  upload cannot turn into a large bill
- the language model failed, was rate-limited, timed out after 120 seconds, or
  answered with something unreadable

So a client that asked for the pass can always use `processed_text`. There is no
separate error to handle. The server logs the reason when the pass fails.

**The transcript leaves the machine when you use this.** Nothing is sent at all unless
a caller asks for the pass, and with the local engine the audio itself never leaves. But
the endpoint you configure receives the full text. If that matters, point `LLM_BASE_URL`
at a model running on your own hardware. Any server that speaks the OpenAI chat
completions API works, including Ollama, llama.cpp's server, and LM Studio, as well as
the hosted providers.

This pass is configured separately from the recogniser, and `LLM_BASE_URL` can point
somewhere completely unrelated to Soniox. Using the
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
| `SONIOX_API_KEY` | unset | Set it to transcribe every request with the cloud engine instead of locally. See [Cloud engine](#cloud-engine) |
| `IDLE_TIMEOUT` | `300` | Seconds of inactivity before the model is freed from memory. `0` keeps it loaded forever. Checked every 30 seconds, so the unload can be up to 30 seconds late |
| `MODEL_REPO` | `handy-computer/parakeet-unified-en-0.6b-gguf` | Hugging Face repository to download the model from |
| `MODEL_FILE` | `parakeet-unified-en-0.6b-Q5_K_M.gguf` | File to download from that repository |
| `TRANSCRIBE_MODEL` | unset | Path to a local `.gguf`. If set, it wins and Hugging Face is never contacted |
| `LLM_BASE_URL` | unset | Base URL of an OpenAI-compatible API, for example `https://api.openai.com/v1`. `/chat/completions` is appended. Unset disables postprocessing |
| `LLM_MODEL` | unset | Model name sent to that API. Unset disables postprocessing |
| `LLM_API_KEY` | unset | Sent as `Authorization: Bearer`. Leave unset for a local server that wants no key |
| `HF_HOME` | `~/.cache/huggingface` | Where the downloaded model is cached. Read by `huggingface_hub`, not by this server |
| `TMPDIR` | `/tmp` | Where uploads are staged before decoding. Read by Python, not by this server |

The default bind address is `127.0.0.1` because the server has no authentication.
Exposing it on a network is a deliberate act, so set `HOST` yourself.

The rest of this section describes the local engine, which also runs as the cloud
fallback.

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

The image sets `HOST=0.0.0.0` and `HF_HOME=/models`. Mount a volume at `/models` or the
model is downloaded again on every new container. This applies in both modes, since the
cloud engine downloads the local model for the fallback. Python dependencies are installed at
build time, so the first request does not wait for them. The image is about 1.03 GB and
does not contain the model.

## Compose

`docker-compose.yml` builds the image, mounts a `cache-models` volume at `/models`,
and publishes the port on the loopback interface.

```bash
docker compose up --build --detach
```

| Variable | Default | Meaning |
| --- | --- | --- |
| `PUBLISH_ADDR` | `127.0.0.1:8000` | Host address and port to publish. Read by Compose, not by the server |

`IDLE_TIMEOUT`, `MODEL_REPO`, `MODEL_FILE`, `SONIOX_API_KEY`, `LLM_BASE_URL`,
`LLM_MODEL` and `LLM_API_KEY` are passed through to the container if you set them. The
rest of the Configuration table is fixed by the image and the port mapping.

The `cache-models` volume is used in both modes, because the cloud engine still downloads
the local model for its fallback.

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
finishes, in both modes. The branch defaults to `master`, which is the branch this
repository uses.

Harbormaster provides no reverse proxy, TLS or authentication, and neither does this
server, so the default publishes on loopback only. Widen `PUBLISH_ADDR` only behind
something that authenticates.

## Notes

With the local engine, speed depends heavily on the CPU. On a 2 core 15 W laptop chip,
an Intel i3-8109U, transcription runs at roughly 2.8x realtime: about 40 seconds for a
2 minute file and about 3.5 minutes for a 10 minute file. A modern many core desktop CPU
is several times faster. Measure your own hardware before relying on a number.

With the cloud engine the wait is the upload plus Soniox's own processing time, so the
local CPU barely matters, except when a request falls back to the local engine.

Postprocessing adds the language model's own response time on top, which is seconds
for a hosted API and much longer for a local model on a slow machine.

The server gives a cloud transcription 300 seconds in total, covering the upload, the
polling and the transcript download. A stalled postprocessing call is cut off after 120
seconds, which applies to that one network operation rather than to the pass as a whole.

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
concurrently, up to however many threads waitress is running, until one falls back to the
local engine, which is serialized like any other local run.

There is no authentication and no rate limiting. Do not expose it to an untrusted
network.

## License

GNU Affero General Public License v3.0. See [LICENSE](LICENSE).
