"""Integration tests using an isolated loopback Atomic API listener."""

import base64
import http.client
import json
import os
import queue
import socket
import tempfile
import threading
import time
import unittest
import zlib
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import server

TOKEN = "mock-secret-2Y5JpHlw"
MODEL = "/private/atomic/models/example.gguf"
IMAGE_MODEL = "/private/image-model"
IMAGE_JOB = "6d5ec10db58f4dcf81906760f2a0654e"
CONTROL_TOKEN = "mock-control-secret-2Y5JpHlw"
DIFFUSION = "/atomic/v1/diffusion"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRuoAAAAASUVORK5CYII="
)


class MockHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        self.reply()

    def do_POST(self):
        self.reply()

    def reply(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length)) if length else None
        self.server.calls.put((self.command, self.path, self.headers.get("Authorization"), body))
        status, headers, chunks, before_headers = self.server.responses.get(timeout=3)
        if before_headers is not None and not before_headers.wait(4):
            return
        try:
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, str(value))
            self.end_headers()
            for chunk in chunks:
                if isinstance(chunk, threading.Event):
                    if not chunk.wait(4):
                        break
                else:
                    self.wfile.write(chunk)
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass


class MockAtomicServer(ThreadingHTTPServer):
    def __init__(self):
        self.calls = queue.Queue()
        self.responses = queue.Queue()
        super().__init__(("127.0.0.1", 0), MockHandler)

    def enqueue(self, status, payload, *, before_headers=None, **headers):
        body = json.dumps(payload).encode()
        self.responses.put((status, {"Content-Length": len(body), **headers}, [body], before_headers))

    def stream(self, *chunks, **headers):
        self.responses.put((200, {"Content-Type": "text/event-stream", **headers}, chunks, None))


@contextmanager
def running(listener):
    with listener:
        worker = threading.Thread(target=listener.serve_forever, kwargs={"poll_interval": 0.01})
        worker.start()
        try:
            yield listener
        finally:
            listener.shutdown()
            worker.join()


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.env = {"ATC_API_URL": "http://127.0.0.1:8040"}

    def test_requires_exactly_one_valid_bearer_source(self):
        for overrides in (
            {},
            {"ATC_API_TOKEN": ""},
            {"ATC_API_TOKEN": "contains whitespace"},
            {"ATC_API_TOKEN": "contains\nnewline"},
            {"ATC_API_TOKEN": "føø"},
            {"ATC_API_TOKEN": TOKEN, "ATC_API_TOKEN_FILE": "/irrelevant"},
        ):
            with self.subTest(overrides=overrides):
                with self.assertRaises(server.ConfigurationError):
                    server.Config.from_env(self.env | overrides)
        config = server.Config.from_env(self.env | {"ATC_API_TOKEN": TOKEN})
        self.assertEqual(config.token, TOKEN)
        self.assertNotIn(TOKEN, repr(config))
        self.assertEqual(config.address, "127.0.0.1")
        self.assertIsNone(config.image_model)
        self.assertIsNone(config.control_data_dir)
        with self.assertRaises(server.ConfigurationError):
            server.Config.from_env(self.env | {
                "ATC_API_TOKEN": TOKEN, "ATC_WEBUI_CONTROL_DATA_DIR": "relative",
            })

    def test_token_file_must_be_readable_regular_and_owner_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "token"
            env = self.env | {"ATC_API_TOKEN_FILE": str(path)}
            with self.assertRaises(server.ConfigurationError):
                server.Config.from_env(env)
            path.write_text(TOKEN + "\n", encoding="ascii")
            for mode in (0o000, 0o640, 0o604):
                path.chmod(mode)
                with self.assertRaises(server.ConfigurationError):
                    server.Config.from_env(env)
            path.chmod(0o600)
            self.assertEqual(server.Config.from_env(env).token, TOKEN)
            path.write_bytes(b"\n")
            with self.assertRaises(server.ConfigurationError):
                server.Config.from_env(env)
            with self.assertRaises(server.ConfigurationError):
                server.Config.from_env(self.env | {"ATC_API_TOKEN_FILE": directory})
            fifo = Path(directory) / "fifo"
            os.mkfifo(fifo, 0o600)
            with self.assertRaises(server.ConfigurationError):
                server.Config.from_env(self.env | {"ATC_API_TOKEN_FILE": str(fifo)})

    def test_gallery_directory_is_optional_and_absolute(self):
        env = self.env | {"ATC_API_TOKEN": TOKEN}
        self.assertIsNone(server.Config.from_env(env).gallery_dir)
        path = Path("/atomic/data/images")
        config = server.Config.from_env(env | {"ATC_WEBUI_GALLERY_DIR": str(path)})
        self.assertEqual(config.gallery_dir, path)
        with self.assertRaisesRegex(server.ConfigurationError, "ATC_WEBUI_GALLERY_DIR"):
            server.Config.from_env(env | {"ATC_WEBUI_GALLERY_DIR": "data/images"})

    def test_upstream_must_be_an_origin_and_network_uses_https(self):
        for url in (
            "http://192.0.2.2",
            "http://api.example.test",
            "https://api.example.test/base",
            "https://user:password@api.example.test",
        ):
            with self.subTest(url=url), self.assertRaises(server.ConfigurationError):
                server.Config.from_env({"ATC_API_URL": url, "ATC_API_TOKEN": TOKEN})
        with self.assertRaises(server.ConfigurationError):
            server.Config.from_env(
                self.env | {"ATC_API_TOKEN": TOKEN, "ATC_WEBUI_HOST": "0.0.0.0"},
            )

    def test_startup_error_is_generic(self):
        with (
            patch.dict(os.environ, self.env | {"ATC_API_TOKEN": "invalid secret"}, clear=True),
            patch("sys.stderr") as stderr,
        ):
            self.assertEqual(server.main(), 1)
            stderr.write.assert_any_call("Invalid WebUI configuration.")
            self.assertNotIn("invalid secret", repr(stderr.mock_calls))

    def test_wildcard_startup_error_names_required_origin(self):
        env = self.env | {"ATC_API_TOKEN": TOKEN, "ATC_WEBUI_HOST": "0.0.0.0"}
        with patch.dict(os.environ, env, clear=True), patch("sys.stderr") as stderr:
            self.assertEqual(server.main(), 1)
            stderr.write.assert_any_call(
                "Set ATC_WEBUI_PUBLIC_ORIGIN when using a wildcard bind address."
            )


class ChatFilterTests(unittest.TestCase):
    def test_filter_handles_any_chunk_boundary_without_stripping_common_slashes(self):
        source = (
            rf"Visit https://example.test/docs or //example.test/docs. 8/2 and \n. "
            rf"Paths: (/private/model), C:\private\model, \\host\private, "
            rf"file:///private/model and {TOKEN}."
        )
        expected = (
            r"Visit https://example.test/docs or //example.test/docs. 8/2 and \n. "
            r"Paths: ([redacted]), C:[redacted], [redacted], "
            "file://[redacted] and [redacted]."
        )
        for width in range(1, len(source) + 1):
            with self.subTest(width=width):
                text_filter = server.ChatTextFilter(TOKEN)
                chunks = (source[i:i + width] for i in range(0, len(source), width))
                actual = "".join(text_filter.clean(chunk) for chunk in chunks)
                self.assertEqual(actual + text_filter.clean("", final=True), expected)


class WebUITests(unittest.TestCase):
    def setUp(self):
        resources = ExitStack()
        self.addCleanup(resources.close)
        self.upstream = resources.enter_context(running(MockAtomicServer()))
        config = server.Config.from_env({
            "ATC_API_URL": f"http://127.0.0.1:{self.upstream.server_port}",
            "ATC_API_TOKEN": TOKEN,
            "ATC_WEBUI_IMAGE_MODEL": IMAGE_MODEL,
            "ATC_WEBUI_GALLERY_URL": "https://gallery.example.test/",
        })
        resources.enter_context(patch.object(server, "MAX_CLIENTS", 3))
        self.ui = resources.enter_context(running(server.WebUIServer(replace(config, port=0))))

    @contextmanager
    def response(self, route, payload=None, headers=None, *, method=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.ui.server_port, timeout=3)
        try:
            if payload is None:
                connection.request(method or "GET", route, headers=headers or {})
            else:
                connection.request(
                    "POST", route, json.dumps(payload),
                    {"Content-Type": "application/json", **(headers or {})},
                )
            with connection.getresponse() as response:
                yield response
        finally:
            connection.close()

    def expect_call(self, method, route, payload=None):
        self.assertEqual(
            self.upstream.calls.get(timeout=1), (method, route, f"Bearer {TOKEN}", payload),
        )

    @contextmanager
    def image_control(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            daemon = root / "atc/run/daemon.json"
            daemon.parent.mkdir(parents=True)
            daemon.write_text(json.dumps({
                "state": "ready", "pid": os.getpid(), "control_url": self.ui.config.upstream,
            }))
            token = root / "atomic-core/control-token"
            token.parent.mkdir()
            token.write_text(CONTROL_TOKEN + "\n")
            token.chmod(0o600)
            config = replace(self.ui.config, image_model=None, control_data_dir=root)
            with patch.object(self.ui, "config", config):
                yield root

    def enqueue_image_job(self):
        self.upstream.enqueue(200, {"defaults": {
            "width": 1024, "height": 1024, "steps": 8, "cfgScale": 1,
        }})
        self.upstream.enqueue(200, {"jobId": IMAGE_JOB})

    def expect_control_call(self, method, route, payload=None):
        self.assertEqual(self.upstream.calls.get(timeout=1), (
            method, DIFFUSION + route, f"Bearer {CONTROL_TOKEN}", payload,
        ))

    def discover(self):
        self.upstream.enqueue(200, {"data": [
            {"id": MODEL, "owned_by": TOKEN, "path": "/private/atomic"},
        ]})
        with self.response("/api/models") as response:
            self.assertEqual(response.status, 200)
            body = response.read()
            self.assertNotIn(MODEL.encode(), body)
            self.assertNotIn(TOKEN.encode(), body)
            self.assertNotIn(b"owned_by", body)
            models = json.loads(body)["models"]
            self.assertEqual(len(models), 1)
            self.assertEqual(models[0]["label"], f'example.gguf · {models[0]["id"][:8]}')
        self.expect_call("GET", "/v1/models")
        return models[0]["id"]

    def test_image_model_status_tracks_the_local_image_session(self):
        image = {
            "modality": "image", "displayName": "Z-Image Turbo (Q4_K_M)", "modelId": "z-image:q4_k_m",
        }
        cases = (
            ({"state": "unloaded", "loaded": None}, "stopped", None),
            ({"state": "loading", "loaded": None}, "loading", None),
            ({"state": "loaded", "loaded": image}, "running", image["displayName"]),
            ({"state": "unloading", "loaded": None}, "unloading", None),
            ({"state": "loaded", "loaded": image | {"modality": "video"}}, "stopped", None),
            ({"state": "failed", "error": {"message": MODEL}}, "failed", None),
            ({"state": "loaded", "loaded": image | {"displayName": MODEL}}, "running", image["modelId"]),
            ({"state": "loaded", "loaded": image | {
                "displayName": CONTROL_TOKEN, "modelId": TOKEN,
            }}, "running", None),
            ({"state": "loaded", "loaded": None}, "unknown", None),
        )
        with self.image_control():
            for model, state, name in cases:
                with self.subTest(state=state, name=name):
                    self.upstream.enqueue(200, {
                        "model": model, "activeJob": {"request": {"prompt": "private prompt"}},
                    })
                    with self.response("/api/image-model") as response:
                        self.assertEqual(response.status, 200)
                        self.assertEqual(json.load(response), {"state": state, "name": name})
                    self.expect_control_call("GET", "/status")

    def test_image_model_status_without_control_is_unknown(self):
        with self.response("/api/image-model") as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(json.load(response), {"state": "unknown", "name": None})
        self.assertTrue(self.upstream.calls.empty())

    def test_discovery_uses_stable_opaque_ids_and_clears_unavailable_models(self):
        selector = self.discover()
        self.assertNotEqual(selector, MODEL)
        self.assertEqual(selector, self.discover())
        self.upstream.enqueue(200, {"data": []})
        with self.response("/api/models") as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(json.load(response), {"models": []})
        self.expect_call("GET", "/v1/models")
        with self.response("/api/chat", {"model": selector, "messages": []}) as response:
            self.assertEqual(response.status, 400)
            self.assertEqual(json.load(response)["error"], "model_unavailable")
        self.assertTrue(self.upstream.calls.empty())

    def test_model_labels_stay_stable_and_do_not_expose_directories(self):
        identifiers = [MODEL, "/different/directory/example.gguf", f"/private/{TOKEN}"]
        discovered = []
        for order in (identifiers, list(reversed(identifiers))):
            self.upstream.enqueue(200, {"data": [{"id": identifier} for identifier in order]})
            with self.response("/api/models") as response:
                body = response.read()
                self.assertEqual(response.status, 200)
                self.assertNotIn(b"/different/directory", body)
                self.assertNotIn(TOKEN.encode(), body)
                discovered.append(json.loads(body)["models"])
            self.expect_call("GET", "/v1/models")
        self.assertEqual(discovered[0], list(reversed(discovered[1])))
        self.assertEqual(len({model["label"] for model in discovered[0]}), 3)

    def test_chat_streams_incrementally_and_filters_fragmented_sensitive_text(self):
        selector = self.discover()
        resume = threading.Event()
        self.addCleanup(resume.set)
        self.upstream.stream(
            self.delta("Hello "), resume, self.delta("from /"), self.delta("private/model "),
            self.delta(TOKEN[:8]), self.delta(TOKEN[8:]), self.delta(" at https:/"),
            self.delta(r"/example.test/docs or 8/2 and \n"), b"data: [DONE]\n\n",
        )
        messages = [{"role": "user", "content": "Hello"}]
        with self.response("/api/chat", {"model": selector, "messages": messages, "stream": False}) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.readline(), b"event: delta\n")
            self.assertEqual(json.loads(response.readline()[6:]), {"text": "Hello "})
            self.assertEqual(response.readline(), b"\n")
            resume.set()
            rest = response.read()
        self.expect_call("POST", "/v1/chat/completions", {
            "model": MODEL, "messages": messages, "stream": True,
        })
        texts = [json.loads(line[6:])["text"] for line in rest.splitlines()
                 if line.startswith(b'data: {"text":')]
        self.assertEqual("".join(texts), r"from [redacted] [redacted] at https://example.test/docs or 8/2 and \n")
        self.assertIn(b"event: done", rest)
        self.assertNotIn(b"/private", rest)
        self.assertNotIn(TOKEN.encode(), rest)

    @staticmethod
    def delta(text):
        payload = {"choices": [{"delta": {"content": text}}], "model": MODEL, "detail": TOKEN}
        return b"data: " + json.dumps(payload).encode() + b"\n\n"

    def test_interrupted_chat_preserves_partial_text_and_uses_generic_error(self):
        selector = self.discover()
        messages = [{"role": "user", "content": "Hello"}]
        for tail in ("/", "/" + TOKEN[:8]):
            with self.subTest(tail=tail):
                self.upstream.stream(self.delta("Partial " + tail), b'data: {"error":"raw secret"}\n\n')
                with self.response("/api/chat", {"model": selector, "messages": messages}) as response:
                    body = response.read()
                    self.assertEqual(response.status, 200)
                    texts = [json.loads(line[6:])["text"] for line in body.splitlines()
                             if line.startswith(b'data: {"text":')]
                    self.assertEqual("".join(texts), "Partial /")
                    self.assertIn(b"stream_interrupted", body)
                    self.assertNotIn(b"raw secret", body)
                    self.assertNotIn(b"event: done", body)

    def test_chat_disconnect_releases_worker_while_upstream_is_idle(self):
        selector = self.discover()
        resume = threading.Event()
        self.upstream.stream(self.delta("Partial"), resume)
        messages = [{"role": "user", "content": "Hello"}]
        try:
            with self.response("/api/chat", {"model": selector, "messages": messages},
                               {"X-ATC-Cancel-On-Disconnect": "1"}) as response:
                self.assertEqual(response.readline(), b"event: delta\n")
                self.assertIn(b"Partial", response.readline())
            self.expect_call("POST", "/v1/chat/completions", {
                "model": MODEL, "messages": messages, "stream": True,
            })
            deadline = time.monotonic() + 1.5
            while time.monotonic() < deadline:
                slots = sum(self.ui.slots.acquire(blocking=False) for _ in range(server.MAX_CLIENTS))
                for _ in range(slots):
                    self.ui.slots.release()
                if slots == server.MAX_CLIENTS:
                    break
                time.sleep(0.01)
            self.assertEqual(slots, server.MAX_CLIENTS)
        finally:
            resume.set()

    def test_chat_api_accepts_half_closed_requests_without_cancellation_opt_in(self):
        selector = self.discover()
        payload = json.dumps({
            "model": selector, "messages": [{"role": "user", "content": "Hello"}],
        }).encode()
        self.upstream.stream(self.delta("Hello"), b"data: [DONE]\n\n")
        with socket.create_connection(("127.0.0.1", self.ui.server_port), timeout=2) as client:
            client.sendall((
                f"POST /api/chat HTTP/1.1\r\nHost: 127.0.0.1:{self.ui.server_port}\r\n"
                f"Content-Type: application/json\r\nContent-Length: {len(payload)}\r\n\r\n"
            ).encode() + payload)
            client.shutdown(socket.SHUT_WR)
            with http.client.HTTPResponse(client) as response:
                response.begin()
                self.assertEqual(response.status, 200)
                self.assertIn(b"event: done", response.read())

    def test_image_returns_one_binary_preview_and_no_metadata(self):
        self.upstream.enqueue(200, {"data": [
            {"b64_json": base64.b64encode(PNG).decode(), "path": MODEL},
            {"b64_json": TOKEN},
        ]})
        with self.response("/api/images", {"prompt": "draw", "n": 4}) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.getheader("Content-Type"), "image/png")
            self.assertEqual(response.read(), PNG)
        self.expect_call("POST", "/v1/images/generations", {
            "model": "/private/image-model", "prompt": "draw", "n": 1, "response_format": "b64_json",
        })
        with self.response("/api/config") as response:
            self.assertEqual(json.load(response), {
                "gallery_url": "https://gallery.example.test/", "gallery_enabled": False, "image_enabled": True,
            })

    def test_gallery_lists_bounded_pages_newest_first(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            ids = [f"{index:032x}-00" for index in range(26)]
            for index, image_id in enumerate(ids):
                path = directory / f"{image_id}.png"
                path.write_bytes(PNG)
                timestamp = 1_700_000_000_000_000_000 + index // 2 * 1_000_000
                os.utime(path, ns=(timestamp, timestamp))
            (directory / f"{ids[-1]}.thumb.png").write_bytes(PNG)
            (directory / "unrelated.png").write_bytes(PNG)
            (directory / f"{0xff:032x}-00.png").symlink_to(directory / f"{ids[-1]}.png")
            config = replace(self.ui.config, gallery_dir=directory, image_model="")
            with patch.object(self.ui, "config", config):
                with self.response("/api/config") as response:
                    settings = json.load(response)
                    self.assertTrue(settings["gallery_enabled"])
                    self.assertFalse(settings["image_enabled"])
                with self.response("/api/gallery") as response:
                    self.assertEqual(response.status, 200)
                    page = json.load(response)
                    self.assertEqual([item["id"] for item in page["images"]], ids[:1:-1])
                    self.assertEqual(page["images"][0], {"id": ids[-1], "modified_ms": 1700000000012})
                (directory / f"{ids[2]}.png").unlink()
                (directory / f"{0xab:032x}-00.png").write_bytes(PNG)
                with self.response(f"/api/gallery?before={page['next_cursor']}") as response:
                    self.assertEqual(response.status, 200)
                    page = json.load(response)
                    self.assertEqual([item["id"] for item in page["images"]], ids[1::-1])
                    self.assertIsNone(page["next_cursor"])
                with self.response("/api/gallery?before=invalid") as response:
                    self.assertEqual(response.status, 400)

    def test_gallery_serves_sanitized_images_and_falls_back_to_originals(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            image_id = f"{IMAGE_JOB}-01"
            original = directory / f"{image_id}.png"
            thumb = directory / f"{image_id}.thumb.png"
            route = f"/api/gallery/{image_id}"

            recipe = b"tEXtatomic\0/private/gallery/recipe"
            chunk = (len(recipe) - 4).to_bytes(4, "big") + recipe + zlib.crc32(recipe).to_bytes(4, "big")
            original.write_bytes(PNG[:-12] + chunk + PNG[-12:])
            def png_chunk(kind, data):
                payload = kind + data
                return len(data).to_bytes(4, "big") + payload + zlib.crc32(payload).to_bytes(4, "big")

            thumbnail = (server.PNG_SIGNATURE
                         + png_chunk(b"IHDR", bytes.fromhex("00000001000000010802000000"))
                         + png_chunk(b"IDAT", zlib.compress(b"\x00\x77\x44\xaa"))
                         + png_chunk(b"IEND", b""))
            thumb.write_bytes(thumbnail)
            config = replace(self.ui.config, gallery_dir=directory)
            with patch.object(self.ui, "config", config):
                with patch.object(server, "MAX_IMAGE", len(PNG)):
                    with self.response(route + "/thumbnail") as response:
                        self.assertEqual(response.status, 404)
                with self.response(route + "/thumbnail") as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.read(), thumbnail)
                with self.response(route) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.getheader("Content-Type"), "image/png")
                    self.assertEqual(response.getheader("Cross-Origin-Resource-Policy"), "same-origin")
                    self.assertEqual(response.read(), PNG)
                thumb.unlink()
                for bad_thumbnail in ("absent", "symlink", "corrupt"):
                    with self.subTest(thumbnail=bad_thumbnail):
                        if bad_thumbnail == "symlink":
                            thumb.symlink_to(original)
                        elif bad_thumbnail == "corrupt":
                            thumb.unlink()
                            thumb.write_bytes(b"broken png")
                        with self.response(route + "/thumbnail") as response:
                            self.assertEqual(response.status, 200)
                            self.assertEqual(response.read(), PNG)

    def test_gallery_handles_unavailable_directories_and_rejects_unsafe_requests(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder) / "images"
            route = f"/api/gallery/{IMAGE_JOB}-00"
            with self.response("/api/gallery") as response:
                self.assertEqual(response.status, 404)
            config = replace(self.ui.config, gallery_dir=directory)
            with patch.object(self.ui, "config", config):
                with self.response("/api/gallery") as response:
                    self.assertEqual(response.status, 503)
                    self.assertEqual(json.load(response), {"error": "gallery_unavailable"})
                directory.mkdir()
                with self.response("/api/gallery") as response:
                    self.assertEqual(json.load(response), {"images": [], "next_cursor": None})
                image = directory / f"{IMAGE_JOB}-00.png"
                other = directory.parent / "other.png"
                other.write_bytes(PNG)
                image.symlink_to(other)
                for path in (route, route + "/thumbnail", "/api/gallery/%2e%2e/other.png"):
                    with self.subTest(path=path), self.response(path) as response:
                        self.assertEqual(response.status, 404)
                image.unlink()
                os.mkfifo(image)
                with self.response(route) as response:
                    self.assertEqual(response.status, 404)
                for headers in ({"Origin": "https://other.example"}, {"Sec-Fetch-Site": "cross-site"}):
                    for path in ("/api/gallery", route):
                        with self.subTest(headers=headers, path=path), self.response(path, headers=headers) as response:
                            self.assertEqual(response.status, 403)

    def test_gallery_image_limit_does_not_block_the_listing(self):
        with tempfile.TemporaryDirectory() as folder:
            config = replace(self.ui.config, gallery_dir=Path(folder))
            slots = self.ui.gallery_slots
            with patch.object(self.ui, "config", config), ExitStack() as held:
                for _ in range(server.MAX_GALLERY_CLIENTS):
                    held.enter_context(slots)
                with self.response(f"/api/gallery/{IMAGE_JOB}-00") as response:
                    self.assertEqual(response.status, 503)
                    self.assertEqual(json.load(response), {"error": "gallery_busy"})
                with self.response("/api/gallery") as response:
                    self.assertEqual(response.status, 200)

    def test_gallery_delete_removes_only_the_selected_image_and_thumbnail(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            route = f"/api/gallery/{IMAGE_JOB}-00"
            original = directory / f"{IMAGE_JOB}-00.png"
            thumbnail = directory / f"{IMAGE_JOB}-00.thumb.png"
            other = directory / f"{IMAGE_JOB}-01.png"
            for file in (original, thumbnail, other):
                file.write_bytes(PNG)
            config = replace(self.ui.config, gallery_dir=directory)
            with patch.object(self.ui, "config", config):
                for path, headers, expected in (
                    (route, {"Origin": "https://other.example"}, 403),
                    (route, {"Sec-Fetch-Site": "same-site"}, 403),
                    (route + "/thumbnail", {}, 404),
                    (route + "?extra=1", {}, 404),
                    ("/api/gallery/%2e%2e/other.png", {}, 404),
                ):
                    with self.subTest(path=path, headers=headers):
                        with self.response(path, headers=headers, method="DELETE") as response:
                            self.assertEqual(response.status, expected)
                        self.assertTrue(original.is_file())
                        self.assertTrue(thumbnail.is_file())
                with self.response(route, method="DELETE") as response:
                    self.assertEqual(response.status, 204)
                self.assertFalse(original.exists())
                self.assertFalse(thumbnail.exists())
                self.assertTrue(other.is_file())
                with self.response(route, method="DELETE") as response:
                    self.assertEqual(response.status, 404)
                with self.response("/api/gallery") as response:
                    self.assertEqual([image["id"] for image in json.load(response)["images"]], [f"{IMAGE_JOB}-01"])

    def test_gallery_delete_preserves_original_on_thumbnail_cleanup_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            route = f"/api/gallery/{IMAGE_JOB}-00"
            original = directory / f"{IMAGE_JOB}-00.png"
            thumbnail = directory / f"{IMAGE_JOB}-00.thumb.png"
            thumbnail.write_bytes(PNG)
            original.symlink_to(thumbnail)
            config = replace(self.ui.config, gallery_dir=directory)
            with patch.object(self.ui, "config", config):
                with self.response(route, method="DELETE") as response:
                    self.assertEqual(response.status, 404)
                self.assertTrue(original.is_symlink())
                self.assertTrue(thumbnail.is_file())
                original.unlink()
                original.write_bytes(PNG)
                with patch.object(server.os, "unlink", side_effect=PermissionError):
                    with self.response(route, method="DELETE") as response:
                        self.assertEqual(response.status, 503)
                        self.assertEqual(json.load(response), {"error": "gallery_delete_failed"})
                self.assertTrue(original.is_file())
                self.assertTrue(thumbnail.is_file())

    def test_image_uses_loaded_model_when_no_override_is_set(self):
        payload = {"prompt": "draw", "n": 1, "response_format": "b64_json"}
        with patch.object(self.ui, "config", replace(self.ui.config, image_model=None)):
            with self.response("/api/config") as response:
                self.assertTrue(json.load(response)["image_enabled"])
            self.assertTrue(self.upstream.calls.empty())

            self.upstream.enqueue(503, {
                "error": {"code": "model_not_loaded", "message": IMAGE_MODEL},
            })
            with self.response("/api/images", {"prompt": "draw"}) as response:
                self.assertEqual(response.status, 503)
                self.assertEqual(json.load(response), {"error": "image_model_not_loaded"})
            self.expect_call("POST", "/v1/images/generations", payload)

            self.upstream.enqueue(200, {"data": [{"b64_json": base64.b64encode(PNG).decode()}]})
            with self.response("/api/images", {"prompt": "draw"}) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.read(), PNG)
            self.expect_call("POST", "/v1/images/generations", payload)

    def test_explicit_empty_image_model_disables_generation(self):
        with patch.object(self.ui, "config", replace(self.ui.config, image_model="")):
            with self.response("/api/config") as response:
                self.assertFalse(json.load(response)["image_enabled"])
            with self.response("/api/images", {"prompt": "draw"}) as response:
                self.assertEqual(response.status, 400)
                self.assertEqual(json.load(response), {"error": "image_model_unavailable"})
        self.assertTrue(self.upstream.calls.empty())

    def test_image_limit_leaves_room_for_chat_and_releases_after_completion(self):
        selector = self.discover()
        resume = threading.Event()
        image = {"data": [{"b64_json": base64.b64encode(PNG).decode()}]}
        body = json.dumps(image).encode()
        image_payload = {
            "model": "/private/image-model", "prompt": "draw", "n": 1, "response_format": "b64_json",
        }
        with ExitStack() as requests:
            requests.callback(resume.set)
            images = []
            for _ in range(server.MAX_IMAGE_CLIENTS):
                self.upstream.responses.put((200, {"Content-Length": len(body)}, [resume, body], None))
                connection = http.client.HTTPConnection("127.0.0.1", self.ui.server_port, timeout=3)
                images.append(connection)
                requests.callback(connection.close)
                connection.request("POST", "/api/images", '{"prompt":"draw"}', {
                    "Content-Type": "application/json",
                })
                self.expect_call("POST", "/v1/images/generations", image_payload)
            with self.response("/api/images", {"prompt": "try later"}) as response:
                self.assertEqual(response.status, 503)
                self.assertEqual(json.load(response), {"error": "image_busy"})
            self.upstream.stream(self.delta("Hello"), b"data: [DONE]\n\n")
            messages = [{"role": "user", "content": "Hello"}]
            with self.response("/api/chat", {"model": selector, "messages": messages}) as response:
                self.assertEqual(response.status, 200)
                self.assertIn(b"event: done", response.read())
            self.expect_call("POST", "/v1/chat/completions", {
                "model": MODEL, "messages": messages, "stream": True,
            })
            resume.set()
            for connection in images:
                with connection.getresponse() as response:
                    self.assertEqual(response.read(), PNG)
        for status in (500, 503):
            self.upstream.enqueue(status, {})
            with self.response("/api/images", {"prompt": "draw"}) as response:
                self.assertEqual(response.status, 502)
                self.assertEqual(json.load(response), {"error": "upstream_request_failed"})
            self.expect_call("POST", "/v1/images/generations", image_payload)
        self.upstream.enqueue(200, image)
        with self.response("/api/images", {"prompt": "draw"}) as response:
            self.assertEqual(response.read(), PNG)
        self.expect_call("POST", "/v1/images/generations", image_payload)

    def test_errors_and_redirects_never_forward_body_or_location(self):
        for status, error in (
            (302, "upstream_request_failed"), (401, "upstream_authentication_failed"),
            (404, "upstream_route_unsupported"), (500, "upstream_request_failed"),
        ):
            with self.subTest(status=status):
                self.upstream.enqueue(status, {"error": f"{MODEL} {TOKEN}"}, Location="/redirected")
                with self.response("/api/models") as response:
                    self.assertEqual(response.status, 502)
                    self.assertIsNone(response.getheader("Location"))
                    self.assertEqual(json.load(response), {"error": error})
                self.expect_call("GET", "/v1/models")
        self.assertTrue(self.upstream.calls.empty())

    def test_fixed_routes_and_origin_restrictions(self):
        for route in ("/server.py", "/v1/models", "/api/models?url=evil", "/static/app.js", "/.env"):
            with self.subTest(route=route), self.response(route) as response:
                self.assertEqual(response.status, 404)
        with self.response("/api/images", {"prompt": "draw"}, {"Origin": "https://other.test"}) as response:
            self.assertEqual(response.status, 403)
        with self.response("/api/models", headers={"Host": "other.test"}) as response:
            self.assertEqual(response.status, 403)
        self.assertTrue(self.upstream.calls.empty())
        for route, mime in (("/", "text/html"), ("/style.css", "text/css"), ("/app.js", "text/javascript")):
            with self.response(route) as response:
                self.assertEqual(response.status, 200)
                self.assertIn(mime, response.getheader("Content-Type"))
                self.assertIn("img-src 'self' blob:", response.getheader("Content-Security-Policy"))
                self.assertNotIn(TOKEN.encode(), response.read())

    def test_enforces_request_response_and_stream_size_limits(self):
        with self.response("/api/images", {"prompt": "draw"},
                           {"Content-Length": str(server.MAX_REQUEST + 1)}) as response:
            self.assertEqual(response.status, 413)
        self.assertTrue(self.upstream.calls.empty())
        with patch.object(server, "MAX_DISCOVERY", 20):
            self.upstream.enqueue(200, {"data": [{"id": MODEL}]})
            with self.response("/api/models") as response:
                self.assertEqual(response.status, 502)
                self.assertEqual(json.load(response)["error"], "upstream_response_too_large")
        self.expect_call("GET", "/v1/models")
        selector = self.discover()
        with patch.object(server, "MAX_STREAM_LINE", 64):
            self.upstream.stream(self.delta("too long"))
            with self.response("/api/chat", {"model": selector, "messages": [
                {"role": "user", "content": "Hello"},
            ]}) as response:
                self.assertIn(b"stream_interrupted", response.read())

    def test_upstream_timeout(self):
        resume = threading.Event()
        self.addCleanup(resume.set)
        self.upstream.responses.put((200, {"Content-Length": "11"}, [resume, b'{"data":[]}'], None))
        with patch.object(server, "IO_TIMEOUT", 0.2):
            with self.response("/api/models") as response:
                self.assertEqual(response.status, 504)
                self.assertEqual(json.load(response)["error"], "upstream_timeout")
        resume.set()
        self.expect_call("GET", "/v1/models")

    def test_image_can_wait_longer_than_chat_and_discovery(self):
        resume = threading.Event()
        self.upstream.enqueue(200, {
            "data": [{"b64_json": base64.b64encode(PNG).decode()}],
        }, before_headers=resume)
        release = threading.Timer(0.4, resume.set)
        release.start()
        try:
            with (patch.object(server, "IO_TIMEOUT", 0.15),
                  patch.object(server, "REQUEST_TIMEOUT", 0.25),
                  patch.object(server, "IMAGE_TIMEOUT", 2)):
                with self.response("/api/images", {"prompt": "draw"}) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.read(), PNG)
        finally:
            release.cancel()
            resume.set()

    def test_image_deadline_returns_timeout(self):
        resume = threading.Event()
        self.upstream.enqueue(200, {}, before_headers=resume)
        try:
            with patch.object(server, "IMAGE_TIMEOUT", 0.2):
                with self.response("/api/images", {"prompt": "draw"}) as response:
                    self.assertEqual(response.status, 504)
                    self.assertEqual(json.load(response), {"error": "upstream_timeout"})
        finally:
            resume.set()

    def test_control_image_stream_reports_sampling_and_returns_preview(self):
        with self.image_control() as output:
            recipe = b"tEXtatomic\0/private/gallery/recipe"
            chunk = (len(recipe) - 4).to_bytes(4, "big") + recipe + zlib.crc32(recipe).to_bytes(4, "big")
            (output / f"{IMAGE_JOB}-00.png").write_bytes(PNG[:-12] + chunk + PNG[-12:])
            self.enqueue_image_job()
            self.upstream.enqueue(200, {"job": {
                "id": IMAGE_JOB, "state": "generating", "request": {"prompt": "/private/prompt"},
                "progress": {"phase": "sampling", "step": 2, "totalSteps": 8,
                             "fraction": 0.25, "etaSeconds": 23.2, "unexpected": CONTROL_TOKEN},
            }})
            self.upstream.enqueue(200, {"job": {
                "id": IMAGE_JOB, "state": "completed",
                "outputs": [{"id": f"{IMAGE_JOB}-00", "path": str(output / f"{IMAGE_JOB}-00.png")}],
            }})
            with self.response("/api/images", {"prompt": "draw"}, {
                "Accept": "text/event-stream",
            }) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers.get_content_type(), "text/event-stream")
                body = response.read()
            self.assertNotIn(CONTROL_TOKEN.encode(), body)
            self.assertNotIn(b"/private/", body)
            self.assertNotIn(IMAGE_JOB.encode(), body)
            frames = [(event.removeprefix(b"event: "), json.loads(data.removeprefix(b"data: ")))
                      for event, data in (frame.splitlines() for frame in body.strip().split(b"\n\n"))]
            self.assertEqual(frames[1], (b"progress", {
                "phase": "sampling", "step": 2, "totalSteps": 8, "etaSeconds": 23.2, "fraction": 0.25,
            }))
            self.assertEqual(frames[-1], (b"done", {}))
            self.assertEqual(base64.b64decode(frames[-2][1]["data"]), PNG)
            self.expect_control_call("GET", "/capabilities")
            self.expect_control_call("POST", "/jobs", {
                "width": 1024, "height": 1024, "steps": 8, "cfgScale": 1, "prompt": "draw", "batchSize": 1,
            })
            self.expect_control_call("GET", f"/jobs/{IMAGE_JOB}")
            self.expect_control_call("GET", f"/jobs/{IMAGE_JOB}")
            self.assertTrue(self.upstream.calls.empty())

    def test_control_image_disconnect_cancels_only_its_own_job(self):
        resume = threading.Event()
        with self.image_control():
            self.enqueue_image_job()
            self.upstream.enqueue(200, {"job": {"id": IMAGE_JOB, "state": "generating"}},
                                  before_headers=resume)
            self.upstream.enqueue(200, {"cancelled": True})
            try:
                with self.response("/api/images", {"prompt": "draw"}, {
                    "Accept": "text/event-stream", "X-ATC-Cancel-On-Disconnect": "1",
                }) as response:
                    self.assertEqual(response.readline(), b"event: progress\n")
                    for _ in range(2):
                        self.upstream.calls.get(timeout=1)
                    self.expect_control_call("GET", f"/jobs/{IMAGE_JOB}")
            finally:
                resume.set()
            self.expect_control_call("POST", f"/jobs/{IMAGE_JOB}/cancel", {})

    def test_control_image_rejects_writable_daemon_metadata_before_sending_token(self):
        with self.image_control() as root:
            (root / "atc/run/daemon.json").chmod(0o664)
            with self.response("/api/images", {"prompt": "draw"}, {
                "Accept": "text/event-stream",
            }) as response:
                self.assertEqual(response.status, 502)
                self.assertEqual(json.load(response), {"error": "image_control_unavailable"})
            self.assertTrue(self.upstream.calls.empty())

    def test_image_disconnect_releases_slot_before_upstream_responds(self):
        resume = threading.Event()
        self.upstream.enqueue(200, {}, before_headers=resume)
        connection = http.client.HTTPConnection("127.0.0.1", self.ui.server_port, timeout=2)
        try:
            connection.request("POST", "/api/images", '{"prompt":"draw"}', {
                "Content-Type": "application/json", "X-ATC-Cancel-On-Disconnect": "1",
            })
            self.expect_call("POST", "/v1/images/generations", {
                "model": IMAGE_MODEL, "prompt": "draw", "n": 1, "response_format": "b64_json",
            })
            connection.close()

            self.assertTrue(self.ui.image_slots.acquire(timeout=1))
            try:
                self.assertTrue(self.ui.image_slots.acquire(timeout=1))
                self.ui.image_slots.release()
            finally:
                self.ui.image_slots.release()
        finally:
            connection.close()
            resume.set()

    def test_caps_concurrent_requests(self):
        resume = threading.Event()
        self.addCleanup(resume.set)
        connections = []
        try:
            for _ in range(3):
                self.upstream.responses.put((200, {"Content-Length": "11"}, [resume, b'{"data":[]}'], None))
                connection = http.client.HTTPConnection("127.0.0.1", self.ui.server_port, timeout=2)
                connections.append(connection)
                connection.request("GET", "/api/models")
                self.expect_call("GET", "/v1/models")
            with self.assertRaises((ConnectionResetError, http.client.RemoteDisconnected)):
                with self.response("/api/models"):
                    self.fail("a fourth request was accepted")
            self.assertTrue(self.upstream.calls.empty())
            resume.set()
            for connection in connections:
                with connection.getresponse() as response:
                    self.assertEqual(json.load(response), {"models": []})
        finally:
            resume.set()
            for connection in connections:
                connection.close()

    def test_rejects_truncated_request_even_when_prefix_is_valid_json(self):
        payload = b'{"prompt":"draw"}'
        with socket.create_connection(("127.0.0.1", self.ui.server_port), timeout=2) as client:
            client.sendall((
                f"POST /api/images HTTP/1.1\r\nHost: 127.0.0.1:{self.ui.server_port}\r\n"
                f"Content-Type: application/json\r\nContent-Length: {len(payload) + 4}\r\n\r\n"
            ).encode() + payload)
            client.shutdown(socket.SHUT_WR)
            with http.client.HTTPResponse(client) as response:
                response.begin()
                self.assertEqual(response.status, 400)
                self.assertEqual(json.load(response)["error"], "invalid_request")
        self.assertTrue(self.upstream.calls.empty())


if __name__ == "__main__":
    unittest.main()
