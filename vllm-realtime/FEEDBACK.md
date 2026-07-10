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
2. **Trickle streams are deleted when the publisher closes — unread segments are
   dropped.** Publishing faster than realtime (`--no-realtime`) pushes all
   segments and deletes the stream within milliseconds; a subscriber that
   connects even a beat later gets nothing. Paced (realtime) publishing works
   because pub and sub overlap. Worth documenting whether the orchestrator can
   buffer/retain segments for late subscribers.
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

## Suggestions

- Add a "transports" page: HTTP / SSE / **Trickle** with a one-paragraph "when to
  use each" and the raw-bytes (`TricklePublisher`/`TrickleSubscriber`) vs AV
  (`MediaPublish`/`MediaOutput`) distinction.
- Document the trickle channel mime-type contract (validated vs opaque).
- Document the streaming payment model (one-shot vs per-segment) end to end.
- Consider splitting the `av` import so Trickle-only apps don't need PyAV.

## Environment

- Mock path: built on macOS, verified end-to-end on a Linux Docker host.
- Real path: verified end-to-end with vLLM 0.24.0 (`+cu129` wheel) serving
  `mistralai/Voxtral-Mini-4B-Realtime-2602` on an RTX 4090 (24 GB, driver
  570.x/CUDA 12.8). Live speech was transcribed with word-by-word deltas
  streaming over the orchestrator-proxied WebSocket while audio was still
  being published.
