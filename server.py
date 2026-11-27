"""Same-origin WebUI server for the Atomic Server public API.

GET /api/models returns opaque model IDs and labels. GET /api/config returns
the gallery URL and image availability. POST /api/chat accepts a model ID and
messages and emits delta, done, and error SSE events. POST /api/images takes
a prompt and returns one image as binary data.

Set ATC_API_URL to the upstream origin and choose ATC_API_TOKEN or
ATC_API_TOKEN_FILE. Prefer a mode 0600 file outside the checkout owned by the
service account. ATC_WEBUI_HOST and ATC_WEBUI_PORT default to 127.0.0.1 and
8080. Set ATC_WEBUI_PUBLIC_ORIGIN for a reverse proxy or wildcard bind.
ATC_WEBUI_IMAGE_MODEL and ATC_WEBUI_GALLERY_URL are optional.
"""

import base64
import hashlib
import hmac
import http.client
import ipaddress
import json
import os
import re
import secrets
import selectors
import socket
import stat
import sys
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import TCPServer, ThreadingMixIn
from urllib.parse import urlsplit

STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_ROUTES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}
MAX_REQUEST = 256 * 1024
MAX_DISCOVERY = 1024 * 1024
MAX_IMAGE = 24 * 1024 * 1024
MAX_STREAM = 4 * 1024 * 1024
MAX_STREAM_LINE = 256 * 1024
MAX_CLIENTS = 16
MAX_IMAGE_CLIENTS = 2
IO_TIMEOUT = 15
REQUEST_TIMEOUT = 180
BEARER_TOKEN = re.compile(r"[A-Za-z0-9._~+/\-]+=*")
PATH_SEPARATOR = re.compile(r"[/\\]")
PATH_DELIMITER = re.compile(r"""[\s"'<>()[\]{},;]""")
DRIVE_PREFIX = re.compile(r"[A-Za-z]:$")
SAFE_MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._ -]{0,79}")
REDACTED = "[redacted]"


class ConfigurationError(ValueError):
    pass


class APIError(Exception):
    def __init__(self, status, code):
        super().__init__(code)
        self.status = status
        self.code = code


def read_token(env):
    if ("ATC_API_TOKEN_FILE" in env) == ("ATC_API_TOKEN" in env):
        raise ConfigurationError
    if "ATC_API_TOKEN_FILE" in env:
        descriptor = os.open(env["ATC_API_TOKEN_FILE"], os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as token_file:
            info = os.fstat(token_file.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_mode & 0o077
                or not info.st_mode & stat.S_IRUSR
            ):
                raise ConfigurationError
            raw = token_file.read(4099)
            if len(raw) > 4098:
                raise ConfigurationError
            token = raw.removesuffix(b"\n").removesuffix(b"\r").decode("ascii")
    else:
        token = env["ATC_API_TOKEN"]
    if len(token) > 4096 or not BEARER_TOKEN.fullmatch(token):
        raise ConfigurationError
    return token


def parse_url(value):
    if any(character.isspace() or ord(character) < 32 for character in value):
        raise ConfigurationError
    url = urlsplit(value)
    if (
        url.scheme not in {"http", "https"}
        or not url.hostname
        or url.username is not None
        or url.query
        or url.fragment
        or "\\" in value
        or url.port == 0
    ):
        raise ConfigurationError
    return url


def is_loopback(host):
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def origin(url):
    host = url.hostname
    if ":" in host:
        host = f"[{host}]"
    default_port = 443 if url.scheme == "https" else 80
    authority = f"{host}:{url.port}" if url.port not in {None, default_port} else host
    return f"{url.scheme}://{authority}"


@dataclass(frozen=True)
class Config:
    address: str
    port: int
    upstream: str
    public_origin: str | None
    image_model: str
    gallery_url: str | None
    token: str = field(repr=False)

    @classmethod
    def from_env(cls, env):
        try:
            address = env.get("ATC_WEBUI_HOST", "127.0.0.1")
            port = int(env.get("ATC_WEBUI_PORT", "8080"))
            if not address or not 1 <= port <= 65535:
                raise ConfigurationError
            upstream = parse_url(env["ATC_API_URL"])
            if upstream.path.rstrip("/") or (
                upstream.scheme == "http" and not is_loopback(upstream.hostname)
            ):
                raise ConfigurationError
            public_origin = env.get("ATC_WEBUI_PUBLIC_ORIGIN")
            if public_origin:
                parsed_origin = parse_url(public_origin)
                if parsed_origin.path:
                    raise ConfigurationError
                public_origin = origin(parsed_origin)
            if address in {"0.0.0.0", "::"} and not public_origin:
                raise ConfigurationError("Set ATC_WEBUI_PUBLIC_ORIGIN when using a wildcard bind address.")
            gallery = env.get("ATC_WEBUI_GALLERY_URL") or None
            if gallery:
                parse_url(gallery)
            token = read_token(env)
            if gallery and token in gallery:
                raise ConfigurationError
            return cls(
                address, port, origin(upstream), public_origin,
                env.get("ATC_WEBUI_IMAGE_MODEL", ""), gallery, token,
            )
        except ConfigurationError:
            raise
        except (OSError, KeyError, ValueError, OverflowError):
            raise ConfigurationError from None


def close_socket(connection):
    try:
        connection.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


def cancel_on_disconnect(client, upstream, finished):
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(client, selectors.EVENT_READ)
            while not finished.is_set():
                if not selector.select(timeout=0.1) or finished.is_set():
                    continue
                try:
                    if client.recv(1, socket.MSG_PEEK):
                        return
                except ConnectionError:
                    pass
                close_socket(upstream)
                return
    except (OSError, ValueError):
        return


class ChatTextFilter:
    """Hold a possible token prefix and suppress paths across delta boundaries."""

    def __init__(self, token):
        self.token = token
        self.token_prefix = ""
        self.path_start = ""
        self.path_context = ""
        self.inside_path = False

    def finish(self, interrupted=False):
        if interrupted:
            self.token_prefix = ""
        return self.clean("", final=True)

    def clean(self, text, final=False):
        text = self.token_prefix + text
        self.token_prefix = ""
        pieces = text.split(self.token)
        if not final:
            tail = pieces[-1]
            start = max(0, len(tail) - len(self.token) + 1)
            index = tail.find(self.token[0], start)
            while index != -1:
                if self.token.startswith(tail[index:]):
                    self.token_prefix = tail[index:]
                    pieces[-1] = tail[:index]
                    break
                index = tail.find(self.token[0], index + 1)
        text = self.path_start + REDACTED.join(pieces)
        self.path_start = ""
        context = self.path_context + text
        offset = len(self.path_context)
        output = []
        position = 0
        while position < len(text):
            if self.inside_path:
                end = PATH_DELIMITER.search(text, position)
                if end is None:
                    break
                position = end.start()
                self.inside_path = False
            separator = PATH_SEPARATOR.search(text, position)
            if separator is None:
                output.append(text[position:])
                break
            index = separator.start()
            output.append(text[position:index])
            position = index + 1
            symbol = text[index]
            previous = context[max(0, offset + index - 7):offset + index]
            after_boundary = not previous or PATH_DELIMITER.fullmatch(previous[-1])
            after_drive = DRIVE_PREFIX.search(previous) and (
                len(previous) == 2 or PATH_DELIMITER.fullmatch(previous[-3])
            )
            after_file_scheme = symbol == "/" and previous.lower() == "file://"
            following = text[position:position + 1]
            if not following and not final and (after_boundary or after_drive or after_file_scheme):
                self.path_start = symbol
            else:
                unix_path = after_boundary and symbol == "/" and following != "/"
                unc_path = after_boundary and symbol == following == "\\"
                if following and not PATH_DELIMITER.fullmatch(following) and (
                    unix_path or unc_path or after_drive or after_file_scheme
                ):
                    output.append(REDACTED)
                    self.inside_path = True
                else:
                    output.append(symbol)
        self.path_context = (context[:-1] if self.path_start else context)[-7:]
        return "".join(output)


class WebUIServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    request_queue_size = MAX_CLIENTS

    def __init__(self, config):
        self.config = config
        self.slots = threading.BoundedSemaphore(MAX_CLIENTS)
        self.image_slots = threading.BoundedSemaphore(MAX_IMAGE_CLIENTS)
        self.selector_key = secrets.token_bytes(32)
        self.models = {}
        if ":" in config.address:
            self.address_family = socket.AF_INET6
        super().__init__((config.address, config.port), WebUIHandler)

    def server_bind(self):
        # HTTPServer.server_bind resolves the local hostname via reverse DNS.
        TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        deadline = threading.Timer(REQUEST_TIMEOUT, close_socket, args=(request,))
        deadline.daemon = True
        deadline.start()
        try:
            super().process_request_thread(request, client_address)
        finally:
            deadline.cancel()
            self.slots.release()

    def handle_error(self, request, client_address):
        print("WebUI request failed.", file=sys.stderr)

    def update_models(self, identifiers):
        models = {
            hmac.new(self.selector_key, identifier.encode(), hashlib.sha256).hexdigest():
            identifier
            for identifier in identifiers
        }
        self.models = models
        labels = []
        for selector, identifier in models.items():
            name = identifier.replace("\\", "/").rsplit("/", 1)[-1]
            if not SAFE_MODEL_NAME.fullmatch(name) or self.config.token in name:
                name = "Model"
            labels.append({"id": selector, "label": f"{name} · {selector[:8]}"})
        return labels


class WebUIHandler(BaseHTTPRequestHandler):
    timeout = IO_TIMEOUT

    def log_message(self, format, *args):
        pass

    def send_error(self, code, message=None, explain=None):
        self.send_json(code, {"error": "invalid_http_request"})

    def send_headers(self, status, media_type, length=None):
        self.send_response(status)
        self.send_header("Content-Type", media_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' blob:; frame-ancestors 'none'; base-uri 'none'")
        if length is not None:
            self.send_header("Content-Length", str(length))
        self.end_headers()

    def send_json(self, status, value):
        body = json.dumps(value).encode("utf-8")
        self.send_headers(status, "application/json", len(body))
        self.wfile.write(body)

    def check_origin(self, write):
        config = self.server.config
        hosts = self.headers.get_all("Host", [])
        origins = self.headers.get_all("Origin", [])
        if len(hosts) != 1 or len(origins) > 1:
            raise APIError(403, "origin_forbidden")
        try:
            host_url = parse_url(f"http://{hosts[0]}")
            if host_url.path:
                raise ConfigurationError
            expected = config.public_origin
            if expected:
                requested = origin(host_url._replace(scheme=urlsplit(expected).scheme))
                allowed = requested == expected
            else:
                bind = config.address
                allowed_hosts = {bind}
                if is_loopback(bind):
                    allowed_hosts.update({"localhost", "127.0.0.1", "::1"})
                allowed = (
                    host_url.hostname in allowed_hosts
                    and (host_url.port or 80) == self.server.server_port
                )
                expected = origin(host_url)
            if not allowed or (write and origins and origins[0] != expected):
                raise ConfigurationError
            if write and self.headers.get("Sec-Fetch-Site") in {"cross-site", "same-site"}:
                raise ConfigurationError
        except ValueError:
            raise APIError(403, "origin_forbidden") from None

    def do_GET(self):
        self.dispatch(write=False)

    def do_POST(self):
        self.dispatch(write=True)

    def dispatch(self, write):
        try:
            self.check_origin(write)
            if write:
                if self.path == "/api/chat":
                    self.chat(self.read_json())
                elif self.path == "/api/images":
                    self.generate_image(self.read_json())
                else:
                    raise APIError(404, "route_not_found")
            elif self.path == "/api/models":
                self.discover_models()
            elif self.path == "/api/config":
                self.send_json(200, {
                    "gallery_url": self.server.config.gallery_url,
                    "image_enabled": bool(self.server.config.image_model),
                })
            elif self.path in STATIC_ROUTES:
                filename, media_type = STATIC_ROUTES[self.path]
                try:
                    body = (STATIC_DIR / filename).read_bytes()
                except FileNotFoundError:
                    raise APIError(404, "route_not_found") from None
                self.send_headers(200, media_type, len(body))
                self.wfile.write(body)
            else:
                raise APIError(404, "route_not_found")
        except APIError as error:
            self.send_json(error.status, {"error": error.code})
        except (ConnectionError, TimeoutError):
            return

    def read_json(self):
        lengths = self.headers.get_all("Content-Length", [])
        if self.headers.get_all("Transfer-Encoding") or len(lengths) != 1:
            raise APIError(400, "invalid_request")
        length = lengths[0]
        if not re.fullmatch(r"[0-9]+", length):
            raise APIError(400, "invalid_request")
        if len(length) > len(str(MAX_REQUEST)) or int(length) > MAX_REQUEST:
            raise APIError(413, "request_too_large")
        if self.headers.get_content_type() != "application/json":
            raise APIError(415, "json_required")
        try:
            body = self.rfile.read(int(length))
            if len(body) != int(length):
                raise APIError(400, "invalid_request")
            value = json.loads(body.decode("utf-8"))
        except (ValueError, RecursionError):
            raise APIError(400, "invalid_json") from None
        if not isinstance(value, dict):
            raise APIError(400, "invalid_json")
        return value

    def upstream(self, route, consume, payload=None, *, watch_disconnect=False):
        config = self.server.config
        url = urlsplit(config.upstream)
        connection_type = http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
        connection = connection_type(url.hostname, url.port, timeout=IO_TIMEOUT)
        deadline = None
        finished = threading.Event() if watch_disconnect else None
        try:
            connection.connect()
            deadline = threading.Timer(REQUEST_TIMEOUT, close_socket, args=(connection.sock,))
            deadline.daemon = True
            deadline.start()
            if finished is not None:
                threading.Thread(
                    target=cancel_on_disconnect, args=(self.connection, connection.sock, finished),
                    daemon=True,
                ).start()
            headers = {"Authorization": f"Bearer {config.token}"}
            body = None
            if payload is not None:
                body = json.dumps(payload).encode("utf-8")
                headers["Content-Type"] = "application/json"
            connection.request("GET" if body is None else "POST", route, body, headers)
            with connection.getresponse() as response:
                if response.status in {401, 403}:
                    raise APIError(502, "upstream_authentication_failed")
                if response.status in {404, 405, 501}:
                    raise APIError(502, "upstream_route_unsupported")
                if response.status != 200:
                    raise APIError(502, "upstream_request_failed")
                return consume(response)
        except TimeoutError:
            raise APIError(504, "upstream_timeout") from None
        except (OSError, http.client.HTTPException, ValueError, RecursionError):
            raise APIError(502, "invalid_upstream_response") from None
        finally:
            if finished is not None:
                finished.set()
            if deadline:
                deadline.cancel()
            connection.close()

    @staticmethod
    def upstream_json(response, limit):
        body = response.read(limit + 1)
        if len(body) > limit:
            raise APIError(502, "upstream_response_too_large")
        if response.length not in {None, 0}:
            raise APIError(502, "invalid_upstream_response")
        return json.loads(body.decode("utf-8"))

    def discover_models(self):
        data = self.upstream(
            "/v1/models", lambda response: self.upstream_json(response, MAX_DISCOVERY)
        )
        models = data.get("data") if isinstance(data, dict) else None
        if not isinstance(models, list):
            raise APIError(502, "invalid_upstream_response")
        identifiers = dict.fromkeys(
            item["id"] for item in models
            if isinstance(item, dict) and isinstance(item.get("id"), str)
            and item["id"] and not any(0xD800 <= ord(char) <= 0xDFFF for char in item["id"])
        )
        self.send_json(200, {"models": self.server.update_models(identifiers)})

    def chat(self, payload):
        selector = payload.get("model")
        model = self.server.models.get(selector) if isinstance(selector, str) else None
        if model is None:
            raise APIError(400, "model_unavailable")
        messages = payload.get("messages")
        if not isinstance(messages, list) or not messages:
            raise APIError(400, "invalid_messages")
        clean_messages = []
        for message in messages:
            if (
                not isinstance(message, dict)
                or message.get("role") not in ("system", "user", "assistant")
                or not isinstance(message.get("content"), str)
            ):
                raise APIError(400, "invalid_messages")
            clean_messages.append({"role": message["role"], "content": message["content"]})
        self.upstream(
            "/v1/chat/completions", self.stream_chat,
            {"model": model, "messages": clean_messages, "stream": True},
            # A TCP FIN can also mean the client has only closed its write side.
            watch_disconnect=self.headers.get("X-ATC-Cancel-On-Disconnect") == "1",
        )

    def send_event(self, event, data):
        self.wfile.write(f"event: {event}\ndata: {json.dumps(data)}\n\n".encode("utf-8"))
        self.wfile.flush()

    def stream_chat(self, response):
        media_type = response.headers.get_content_type()
        if media_type != "text/event-stream":
            raise APIError(502, "invalid_upstream_response")
        self.send_headers(200, "text/event-stream; charset=utf-8")
        text_filter = ChatTextFilter(self.server.config.token)
        try:
            self.relay_chat(response, text_filter)
        except (OSError, http.client.HTTPException, ValueError, RecursionError):
            try:
                remaining = text_filter.finish(interrupted=True)
                if remaining:
                    self.send_event("delta", {"text": remaining})
                self.send_event("error", {"error": "stream_interrupted"})
            except OSError:
                pass

    def relay_chat(self, response, text_filter):
        total = 0
        event_data = []
        while True:
            line = response.readline(MAX_STREAM_LINE + 1)
            total += len(line)
            if not line or total > MAX_STREAM or len(line) > MAX_STREAM_LINE:
                raise ValueError
            line = line.rstrip(b"\r\n")
            if line.startswith(b"data:"):
                event_data.append(line[5:].removeprefix(b" "))
            if line or not event_data:
                continue
            data = b"\n".join(event_data)
            event_data.clear()
            if data == b"[DONE]":
                remaining = text_filter.finish()
                if remaining:
                    self.send_event("delta", {"text": remaining})
                self.send_event("done", {})
                return
            packet = json.loads(data.decode("utf-8"))
            if not isinstance(packet, dict) or "error" in packet:
                raise ValueError
            choices = packet.get("choices")
            if not isinstance(choices, list):
                raise ValueError
            if choices and isinstance(choices[0], dict):
                delta = choices[0].get("delta")
                content = delta.get("content") if isinstance(delta, dict) else None
                if isinstance(content, str) and content:
                    safe_text = text_filter.clean(content)
                    if safe_text:
                        self.send_event("delta", {"text": safe_text})

    def generate_image(self, payload):
        model = self.server.config.image_model
        if not model:
            raise APIError(400, "image_model_unavailable")
        prompt = payload.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise APIError(400, "invalid_prompt")
        if not self.server.image_slots.acquire(blocking=False):
            raise APIError(503, "image_busy")
        try:
            self.send_image(model, prompt)
        finally:
            self.server.image_slots.release()

    def send_image(self, model, prompt):
        data = self.upstream(
            "/v1/images/generations",
            lambda response: self.upstream_json(response, MAX_IMAGE),
            {"model": model, "prompt": prompt, "n": 1, "response_format": "b64_json"},
        )
        images = data.get("data") if isinstance(data, dict) else None
        image = images[0] if isinstance(images, list) and images else None
        encoded = image.get("b64_json") if isinstance(image, dict) else None
        if not isinstance(encoded, str):
            raise APIError(502, "invalid_upstream_response")
        try:
            decoded = base64.b64decode(encoded, validate=True)
        except ValueError:
            raise APIError(502, "invalid_upstream_response") from None
        if self.server.config.token.encode("ascii") in decoded:
            raise APIError(502, "invalid_upstream_response")
        if decoded.startswith(b"\x89PNG\r\n\x1a\n"):
            media_type = "image/png"
        elif decoded.startswith(b"\xff\xd8\xff"):
            media_type = "image/jpeg"
        elif decoded.startswith(b"RIFF") and decoded[8:12] == b"WEBP":
            media_type = "image/webp"
        else:
            raise APIError(502, "invalid_upstream_response")
        self.send_headers(200, media_type, len(decoded))
        self.wfile.write(decoded)


def main():
    try:
        config = Config.from_env(os.environ)
    except ConfigurationError as error:
        print(str(error) or "Invalid WebUI configuration.", file=sys.stderr)
        return 1
    try:
        with WebUIServer(config) as server:
            server.serve_forever()
    except KeyboardInterrupt:
        return 0
    except OSError:
        print("Unable to start the WebUI listener.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
