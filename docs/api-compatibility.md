# Atomic Server API compatibility

The WebUI targets Atomic Server 0.2.21 with Atomic Core 0.24.8. The installed versions match that target.

## Observed on the public listener

| Request | Result | WebUI implication |
| --- | --- | --- |
| API calls without authentication | HTTP `401` | The WebUI needs to authenticate upstream requests. |
| `GET /v1/models` with a valid bearer token | HTTP `200` with an empty model list | The API can be reachable while no models are available. |
| `POST /v1/images/generations` with a valid bearer token and the prompt omitted | Structured HTTP `400` error | The listener rejects requests that omit the image prompt. |

## Declared in the installed public OpenAPI document

- `POST /v1/chat/completions` is declared as the chat completion endpoint.
- Successful responses from `POST /v1/images/generations` are specified to contain base64-encoded image data.

## WebUI request behavior

- Browser chat and image requests send `X-ATC-Cancel-On-Disconnect: 1` to the WebUI. When the browser disconnects, the WebUI closes its upstream connection and releases its request slot. Direct callers use their existing connection behavior by default.
- An interrupted chat stream emits any safe buffered text before its `stream_interrupted` event. The browser retains the partial reply.
- The WebUI allows two concurrent image generation requests within its 16-connection limit. When both image slots are occupied, additional image requests receive HTTP `503` with `{"error":"image_busy"}`. Chat can use the other slots.
- With local image progress enabled and automatic model selection, browser image requests negotiate SSE. The WebUI sends `progress` events with Atomic's phase, sampling steps, completion fraction, and ETA when available. It streams the completed PNG in bounded `image` events and ends with `done` or `error`. Other clients can continue to receive a single binary image from the public API path.

## Regression verification

- All 19 Python tests passed with the local mock API, including disconnect cancellation, interrupted text, and image capacity and recovery.
- All four JavaScript tests passed under Node.js 22 in a simulated browser environment. They cover independent saves from multiple tabs, existing history, oversized requests, and retrying failed storage writes.

## Stage 4 verification with a mock Atomic API

- `python3 -m unittest discover -s tests -v` passed all 16 tests on Ubuntu 26.04 with Python 3.14 and in an isolated Ubuntu 24.04 container with Python 3.12. Coverage includes bearer authentication, token configuration and filtering, discovery, incremental and interrupted chat, image responses, upstream errors, redirects, route isolation, and resource limits.
- Headless Chromium checks passed at 1440 px and 390 px through a temporary Nginx reverse proxy. The first chat delta appeared before completion with proxy buffering disabled. History and the theme survived reload. Each image request produced one preview, and image data stayed out of conversation storage.
- A 64-request discovery run filled the default 16 concurrent request slots in four batches and confirmed excess clients were rejected. Sampled resident memory grew from about 25 MiB to a maximum of 29 MiB. Thread and descriptor counts returned to baseline after each batch.
- Independent review found that stream filtering removed ordinary slash-containing text, oversized prompts in saved conversations could block subsequent sends, and model labels depended on response order. Regression checks cover filtering across chunk boundaries, stable labels, and keeping oversized prompts editable. Chat requests include recent turns that fit the request limit while retaining the complete transcript locally.

## Stage 4 verification against the live public API

- Unauthenticated model discovery returned HTTP `401`. A temporary WebUI using the existing public API client key served its page and returned HTTP `200` with an empty model list from `GET /api/models`.
- No suitable live chat model was available, so successful live streaming remains unverified. Live image generation and its base64 response remain unverified. Creating a live gallery image requires separate approval.
