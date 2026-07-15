# Developer experience feedback — building `vllm-realtime`

Notes from building this example against the `ja/live-runner` branch of
go-livepeer and `livepeer-python-gateway`. Goal: capture what was smooth, what
was confusing, and the open questions a second builder would hit.

## What worked well

- **The example apps are a great on-ramp.** `hello-world` (dynamic registration)
  and `echo` (Trickle in/out) together gave a near-complete template.
  `create_trickle_channels(request, [...])` returning `{name, url, internal_url,
  ...}` dicts is clean, and raw WebSocket passthrough made the transcript output
  path a plain aiohttp handler.
- **Mock-first development is viable.** Because the app only needs raw PCM in and
  text out, a GPU-free mock backend exercises the entire Trickle path on a laptop.
  This decoupled "is my Livepeer plumbing correct?" from "is vLLM configured?",
  which made iteration fast.
- **Offchain → onchain is a thin overlay.** Reusing the shared
  `compose.orchestrator.yml` / `compose.onchain.yml` via `extends` meant the paid
  path was just a price flag + signer service, with zero app code changes.

## Friction / confusion

1. **Use `internal_url`, not `url`, for the runner-side subscribe.** The
   orchestrator advertises its external `serviceAddr` (e.g. `127.0.0.1:8935`) in
   the public channel `url`; from inside the app container that loops back to the
   app itself and the subscriber dies with connection-refused after max retries.
   `create_trickle_channels` also returns a runner-reachable `internal_url` —
   subscribe on that, hand `url` to the client. `echo` does this correctly, but
   nothing shouts about it and the failure mode (a subscriber that never sees a
   segment) is silent until you read orchestrator logs.
2. **Trickle channels keep no backlog: the subscriber reads the live edge, so
   publishing faster than it consumes destroys audio.** `TrickleSubscriber`
   defaults to `start_seq=-2` — join at the live edge — and an earlier index
   answers `470` ("no data at this index") instead of replaying, so segments
   published while you were behind are simply gone. Measured here: publishing
   6.5 s of speech unpaced delivered **1 of 14 segments**
   (`segments_delivered=1, latest_seq=14`), and asking for `start_seq=0`
   delivered **0** and reset to the edge. This is the right default for video —
   drop stale frames, stay current — but audio is not droppable, and nothing in
   the API signals the difference: there is no error, no gap event
   (`seq_gap_events=0`), just a transcript that comes back short. Apps that
   cannot lose bytes need app-level backpressure; this example publishes one
   segment, waits for the runner to report it consumed it over the WebSocket,
   then publishes the next. A documented "lossless vs live-edge" subscribe mode
   (or any retention window) would spare every non-video app from rediscovering
   this the hard way. Worth noting the loss *is* detectable, but only by pairing
   both sides: `TricklePublisher.get_stats().segments_completed` against
   `TrickleSubscriber.get_stats().segments_delivered`. Neither number is alarming
   alone. Nothing in the API suggests you should compare them.
3. **Importing the SDK pulls in PyAV.** `livepeer_gateway/__init__.py` imports the
   media modules at top level, so `import livepeer_gateway` (even just for
   Trickle) requires `av`. It installs transitively, but a from-source build on a
   minimal image can be surprised by it.
4. **Trickle mime types are undocumented for non-video.** `echo` uses
   `video/mp2t`. It's unclear whether the orchestrator validates mime or treats it
   as opaque relay metadata. This example uses `audio/raw` in and it relays fine;
   the contract should still be documented.
5. **vLLM `/v1/realtime` has its own protocol — it is not OpenAI Realtime.**
   Validated against vLLM 0.24.0: `session.update` requires `model` at the *top
   level* of the event (a nested `session` object silently fails validation and
   every later commit errors with `model_not_validated`); generation starts on
   `input_audio_buffer.commit` with `final: false` and the stream is flushed with
   `final: true`; output events are `transcription.delta` / `transcription.done`.
   Voxtral also interleaves empty-text deltas between words — filter them.
6. **Payment granularity for long streams is unclear.** `call_runner`'s 402
   challenge pays once at session reservation. Whether one payment authorizes an
   arbitrarily long transcription stream, or the orchestrator expects ongoing
   per-segment payment (as `lv2v.py` does with a periodic `send_payment` loop
   against the output channel), needs confirmation. This example uses
   single-payment-at-reserve; if long sessions get dropped for non-payment, port
   the `lv2v` per-segment payment loop into the client. *(Plan R4)*
7. **Serving Voxtral on a 24 GB card needs `--max-model-len`.** The model's
   native 131k context wants a ~4 GiB KV cache that doesn't fit beside the
   weights on an RTX 4090; the engine refuses to start. `--max-model-len 16384`
   is plenty for realtime transcription. Also: the PyPI `vllm` wheel ships
   CUDA-13 torch — on hosts with a 12.x driver (common on cloud GPU rentals)
   install the `+cu129` wheel from GitHub releases instead, and
   `mistral-common[soundfile]` is required at runtime for audio decode.

8. **The SDK meters Trickle, but nothing meters a WebSocket.** `get_stats()` on
   `TrickleSubscriber` / `TricklePublisher` / `MediaOutput` gives transport and
   frame counters for free, so a Trickle-in/Trickle-out app is observable without
   writing any code. An app whose *output* is a WebSocket gets none of it and has
   to hand-roll the whole half (here: `stats.py`). Two things make that sharper
   than it first looks. First, the frame counters everyone points you at
   (`MediaOutputStats.audio_frames_decoded`) only exist if you go through the AV
   decode path — this example publishes raw PCM16, so there is no decoder and no
   frame count, and audio duration has to be derived from byte totals instead.
   Second, there is no suggested shape for app-authored stats, so every WS app
   will invent its own field names for the same quantities.
9. **A real-time factor only means something once the client has backpressure.**
   Wall clock cannot drop below the audio duration while the client paces to real
   time, so RTF is pinned just above 1.0 regardless of backend speed and only
   answers "did the pipeline keep up?". Publishing unpaced to measure real
   throughput runs straight into #2 and reports confident nonsense (1 segment of
   14, zero words, RTF 1.89 — all with no error raised anywhere). Once the client
   holds the live edge until the runner acknowledges each segment, the same clip
   reports **RTF 0.27 (~3.7x realtime)** and the figure is finally real. Worth
   noting that the two modes measure different things and both are useful: paced
   gives live latency (finalize tail 0.37 s), unpaced gives throughput headroom.

## Suggestions

- Add a "transports" page: HTTP / SSE / **Trickle** with a one-paragraph "when to
  use each" and the raw-bytes (`TricklePublisher`/`TrickleSubscriber`) vs AV
  (`MediaPublish`/`MediaOutput`) distinction.
- Document the trickle channel mime-type contract (validated vs opaque).
- Document the streaming payment model (one-shot vs per-segment) end to end.
- Consider splitting the `av` import so Trickle-only apps don't need PyAV.
- Ship a `WebSocketStats` counter helper next to the Trickle ones, so apps that
  stream results over a WebSocket report the same shape instead of each inventing
  one (see #8).
- Give `TrickleSubscriber` a lossless subscribe mode, or document the live-edge
  contract loudly (see #2). Right now the safe default for video is a silent
  data-loss trap for every other media type, and the counters report success
  (`seq_gap_events=0`) while audio disappears.

## Environment

- Mock path: built on macOS, verified end-to-end on a Linux Docker host.
- Real path: verified end-to-end with vLLM 0.24.0 (`+cu129` wheel) serving
  `mistralai/Voxtral-Mini-4B-Realtime-2602` on an RTX 4090 (24 GB, driver
  570.x/CUDA 12.8). Live speech was transcribed with word-by-word deltas
  streaming over the orchestrator-proxied WebSocket while audio was still
  being published.
- Measured on that setup with 6.56 s of speech, 14 segments, no sequence gaps,
  retries or failures in either mode:
  - *Realtime-paced* (live latency): finalize tail **0.37 s**, real-time factor
    1.13x, time to first word 2.4 s. The tail is the stable figure — it
    reproduced within ~10 ms across runs; first-word moves with whatever silence
    precedes speech in the clip.
  - *Unpaced with backpressure* (throughput): wall clock **1.57 s** for 6.56 s of
    audio — real-time factor **0.24x**, i.e. ~4x faster than realtime, with an
    identical transcript. The publisher sustained 1159 kB/s (38x the paced
    30 kB/s) and still delivered 14/14 segments.
