"""Integration tests using an isolated loopback Atomic API listener."""

import base64
import http.client
import json
import os
import queue
import socket
import tempfile
import threading
import unittest
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import server

TOKEN = "mock-secret-2Y5JpHlw"
MODEL = "/private/atomic/models/example.gguf"
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
        status, headers, chunks = self.server.responses.get(timeout=3)
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, str(value))
        self.end_headers()
        try:
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

    def enqueue(self, status, payload, **headers):
        body = json.dumps(payload).encode()
        self.responses.put((status, {"Content-Length": len(body), **headers}, [body]))

    def stream(self, *chunks, **headers):
        self.responses.put((200, {"Content-Type": "text/event-stream", **headers}, chunks))


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
            "ATC_WEBUI_IMAGE_MODEL": "/private/image-model",
            "ATC_WEBUI_GALLERY_URL": "https://gallery.example.test/",
        })
        resources.enter_context(patch.object(server, "MAX_CLIENTS", 3))
        self.ui = resources.enter_context(running(server.WebUIServer(replace(config, port=0))))

    @contextmanager
    def response(self, route, payload=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.ui.server_port, timeout=3)
        try:
            if payload is None:
                connection.request("GET", route, headers=headers or {})
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
        self.upstream.stream(self.delta("Partial"), b'data: {"error":"raw secret"}\n\n')
        messages = [{"role": "user", "content": "Hello"}]
        with self.response("/api/chat", {"model": selector, "messages": messages}) as response:
            body = response.read()
            self.assertEqual(response.status, 200)
            self.assertIn(b"Partial", body)
            self.assertIn(b"stream_interrupted", body)
            self.assertNotIn(b"raw secret", body)
            self.assertNotIn(b"event: done", body)

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
                "gallery_url": "https://gallery.example.test/", "image_enabled": True,
            })

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
        self.upstream.responses.put((200, {"Content-Length": "11"}, [resume, b'{"data":[]}']))
        with patch.object(server, "IO_TIMEOUT", 0.2):
            with self.response("/api/models") as response:
                self.assertEqual(response.status, 504)
                self.assertEqual(json.load(response)["error"], "upstream_timeout")
        resume.set()
        self.expect_call("GET", "/v1/models")

    def test_caps_concurrent_requests(self):
        resume = threading.Event()
        self.addCleanup(resume.set)
        connections = []
        try:
            for _ in range(3):
                self.upstream.responses.put((200, {"Content-Length": "11"}, [resume, b'{"data":[]}']))
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
