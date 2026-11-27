"""Same-origin WebUI server for the Atomic Server public API.

GET /api/models returns opaque model IDs and labels. GET /api/image-model
reports the local image model state and a display label. GET /api/config returns
the gallery URL and whether the local gallery and image generation are enabled.
GET /api/gallery lists local PNGs. GET /api/gallery/<id> serves an image.
DELETE /api/gallery/<id> removes an image and its thumbnail.
POST /api/chat accepts a model ID and messages and emits delta, done, and error SSE events.
POST /api/images takes a prompt and returns one image as binary data.

Set ATC_API_URL to the upstream origin and choose ATC_API_TOKEN or
ATC_API_TOKEN_FILE. Prefer a mode 0600 file outside the checkout owned by the
service account. ATC_WEBUI_HOST and ATC_WEBUI_PORT default to 127.0.0.1 and
8080. Set ATC_WEBUI_PUBLIC_ORIGIN for a reverse proxy or wildcard bind.
ATC_WEBUI_IMAGE_MODEL and ATC_WEBUI_GALLERY_URL are optional.
ATC_WEBUI_CONTROL_DATA_DIR optionally enables local image job progress.
ATC_WEBUI_GALLERY_DIR optionally exposes an Atomic image directory.
"""

import base64
import hashlib
import heapq
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
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import TCPServer, ThreadingMixIn
from urllib.parse import urlsplit
from model_control import ControlError, ModelControl

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
MAX_GALLERY_CLIENTS = 4
IO_TIMEOUT = 15
REQUEST_TIMEOUT = 180
IMAGE_TIMEOUT = 600
CONTROL_TIMEOUT = 5
IMAGE_POLL_INTERVAL = 0.5
IMAGE_CHUNK_SIZE = 48 * 1024
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
IMAGE_JOB_ID = re.compile(r"[0-9a-f]{32}")
GALLERY_ID = r"[0-9a-f]{32}-[0-9]{2}"
GALLERY_FILENAME = re.compile(rf"({GALLERY_ID})\.png")
GALLERY_IMAGE_ROUTE = re.compile(rf"/api/gallery/({GALLERY_ID})(/thumbnail)?")
GALLERY_CURSOR = re.compile(rf"before=(-?[0-9]{{1,20}})\.({GALLERY_ID})")
GALLERY_PAGE_SIZE = 24
IMAGE_PHASES = ("queued", "encoding", "sampling", "decoding", "postprocessing", "saving")
BEARER_TOKEN = re.compile(r"[A-Za-z0-9._~+/\-]+=*")
PATH_SEPARATOR = re.compile(r"[/\\]")
PATH_DELIMITER = re.compile(r"""[\s"'<>()[\]{},;]""")
DRIVE_PREFIX = re.compile(r"[A-Za-z]:$")
SAFE_MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._ -]{0,79}")
SAFE_IMAGE_MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._ :()+-]{0,95}")
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


MODEL_CONTROL_LAN = tuple(map(ipaddress.ip_network, (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7",
)))


def model_control_peer(host, lan_enabled):
    address = ipaddress.ip_address(host)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_loopback or (
        lan_enabled and any(address in network for network in MODEL_CONTROL_LAN)
    )


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
    image_model: str | None
    gallery_url: str | None
    token: str = field(repr=False)
    control_data_dir: Path | None = None
    gallery_dir: Path | None = None
    model_control: bool = False
    atc_binary: str = "atc"
    atc_data_folder: Path | None = None

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
            control_dir = env.get("ATC_WEBUI_CONTROL_DATA_DIR")
            if control_dir and not Path(control_dir).is_absolute():
                raise ConfigurationError
            gallery_dir = env.get("ATC_WEBUI_GALLERY_DIR")
            if gallery_dir and not Path(gallery_dir).is_absolute():
                raise ConfigurationError("Set ATC_WEBUI_GALLERY_DIR to an absolute path.")
            model_control = env.get("ATC_WEBUI_MODEL_CONTROL") == "1"
            atc_binary = env.get("ATC_WEBUI_ATC_BIN") or "atc"
            atc_data = env.get("ATC_WEBUI_ATC_DATA_FOLDER")
            if model_control and (
                not is_loopback(upstream.hostname)
                or bool(public_origin) == is_loopback(address)
            ):
                raise ConfigurationError(
                    "Model controls require a local Atomic API and either a loopback WebUI "
                    "without a public origin or a LAN WebUI with an explicit origin."
                )
            if model_control and (
                (atc_binary != "atc" and not Path(atc_binary).is_absolute())
                or (atc_data and not Path(atc_data).is_absolute())
            ):
                raise ConfigurationError("Set ATC CLI and data-folder overrides to absolute paths.")
            return cls(
                address, port, origin(upstream), public_origin,
                env.get("ATC_WEBUI_IMAGE_MODEL"), gallery, token,
                Path(control_dir) if control_dir else None,
                Path(gallery_dir) if gallery_dir else None,
                model_control, atc_binary, Path(atc_data) if atc_data else None,
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


def cancel_on_disconnect(client, cancel, finished):
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
                cancel()
                return
    except (OSError, ValueError):
        return


class AtomicImageControl:
    """Server-only access to the local Atomic daemon from an operator-selected data directory."""

    def __init__(self, directory):
        try:
            descriptor = os.open(directory / "atc/run/daemon.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as metadata:
                info = os.fstat(metadata.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
                    raise ConfigurationError
                raw = metadata.read(16385)
                if len(raw) > 16384:
                    raise ConfigurationError
                daemon = json.loads(raw)
            if not isinstance(daemon, dict) or daemon.get("state") != "ready":
                raise ConfigurationError
            pid = daemon.get("pid")
            if type(pid) is not int or pid <= 0:
                raise ConfigurationError
            os.kill(pid, 0)
            self.url = parse_url(daemon.get("control_url", ""))
            if (self.url.scheme != "http" or not ipaddress.ip_address(self.url.hostname).is_loopback
                    or self.url.path not in {"", "/"}):
                raise ConfigurationError
            self.token = read_token({"ATC_API_TOKEN_FILE": directory / "atomic-core/control-token"})
        except (OSError, TypeError, ValueError, OverflowError, RecursionError):
            raise APIError(502, "image_control_unavailable") from None

    def call(self, route, payload=None):
        connection = http.client.HTTPConnection(self.url.hostname, self.url.port, timeout=CONTROL_TIMEOUT)
        deadline = None
        try:
            connection.connect()
            deadline = threading.Timer(CONTROL_TIMEOUT, close_socket, args=(connection.sock,))
            deadline.daemon = True
            deadline.start()
            headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
            body = None if payload is None else json.dumps(payload).encode()
            connection.request("GET" if body is None else "POST", "/atomic/v1/diffusion" + route, body, headers)
            with connection.getresponse() as response:
                if response.status in {401, 403, 405}:
                    raise APIError(502, "image_control_unavailable")
                data = WebUIHandler.upstream_json(response, MAX_DISCOVERY)
                if not isinstance(data, dict):
                    raise APIError(502, "invalid_upstream_response")
                if response.status != 200:
                    self.raise_job_error(data.get("error"))
                return data
        except (OSError, http.client.HTTPException, ValueError, RecursionError):
            raise APIError(502, "image_control_unavailable") from None
        finally:
            if deadline is not None:
                deadline.cancel()
            connection.close()

    @staticmethod
    def raise_job_error(error):
        code = error.get("code") if isinstance(error, dict) else None
        if code in ("MODEL_NOT_LOADED", "MODEL_INCOMPATIBLE", "NOT_CONFIGURED", "ENGINE_MISSING"):
            raise APIError(503, "image_model_not_loaded")
        if code in ("JOB_BUSY", "GPU_BUSY", "QUEUE_FULL"):
            raise APIError(503, "image_busy")
        if code == "CANCELLED":
            raise APIError(409, "image_cancelled")
        if code in ("OUT_OF_MEMORY", "INSUFFICIENT_MEMORY"):
            raise APIError(502, "image_out_of_memory")
        raise APIError(502, "upstream_request_failed")


def image_progress(job):
    upstream = job.get("progress")
    if not isinstance(upstream, dict):
        return {"phase": "queued" if job["state"] == "queued" else "encoding"}
    progress = {}
    if upstream.get("phase") in IMAGE_PHASES:
        progress["phase"] = upstream["phase"]
    for key in ("step", "totalSteps"):
        value = upstream.get(key)
        if type(value) is int and 0 <= value <= 86400:
            progress[key] = value
    eta = upstream.get("etaSeconds")
    if type(eta) in (int, float) and 0 <= eta <= 86400:
        progress["etaSeconds"] = eta
    fraction = upstream.get("fraction")
    if type(fraction) in (int, float) and 0 <= fraction <= 1:
        progress["fraction"] = fraction
    return progress


def read_png_file(directory_fd, filename):
    image_fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
    with os.fdopen(image_fd, "rb") as image:
        if not stat.S_ISREG(os.fstat(image.fileno()).st_mode):
            raise APIError(502, "invalid_upstream_response")
        return image.read(MAX_IMAGE + 1)


def gallery_png(directory, job_id, tokens):
    """Read the completed job's first PNG for the generation preview."""
    try:
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            body = read_png_file(directory_fd, f"{job_id}-00.png")
        finally:
            os.close(directory_fd)
    except (OSError, ValueError):
        raise APIError(502, "invalid_upstream_response") from None
    return strip_png_metadata(body, tokens)


def strip_png_metadata(body, tokens):
    """Keep the PNG's pixels and transparency while omitting its embedded recipe."""
    if len(body) > MAX_IMAGE:
        raise APIError(502, "upstream_response_too_large")
    if not body.startswith(PNG_SIGNATURE) or any(token.encode("ascii") in body for token in tokens):
        raise APIError(502, "invalid_upstream_response")
    view = memoryview(body)
    position = len(PNG_SIGNATURE)
    preview = bytearray(PNG_SIGNATURE)
    while position + 12 <= len(body):
        end = position + int.from_bytes(view[position:position + 4], "big") + 12
        if end > len(body):
            break
        kind = body[position + 4:position + 8]
        if kind in {b"IHDR", b"PLTE", b"IDAT", b"tRNS", b"IEND"}:
            preview.extend(view[position:end])
        if kind == b"IEND":
            if any(token.encode("ascii") in preview for token in tokens):
                break
            return preview
        position = end
    raise APIError(502, "invalid_upstream_response")


def gallery_entries(directory_fd, before):
    with os.scandir(directory_fd) as entries:
        for entry in entries:
            match = GALLERY_FILENAME.fullmatch(entry.name)
            if not match:
                continue
            try:
                info = entry.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            key = info.st_mtime_ns, match[1]
            if stat.S_ISREG(info.st_mode) and 0 < info.st_size <= MAX_IMAGE and (before is None or key < before):
                yield key


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
        self.model_control = (
            ModelControl(config.atc_binary, config.atc_data_folder, config.upstream, config.token)
            if config.model_control else None
        )
        self.slots = threading.BoundedSemaphore(MAX_CLIENTS)
        self.image_slots = threading.BoundedSemaphore(MAX_IMAGE_CLIENTS)
        self.gallery_slots = threading.BoundedSemaphore(MAX_GALLERY_CLIENTS)
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
        try:
            super().process_request_thread(request, client_address)
        finally:
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

    def handle(self):
        self.connection_deadline = None
        self.set_connection_deadline(REQUEST_TIMEOUT)
        try:
            super().handle()
        finally:
            self.connection_deadline.cancel()

    def set_connection_deadline(self, seconds):
        if self.connection_deadline is not None:
            self.connection_deadline.cancel()
        self.connection_deadline = threading.Timer(seconds, close_socket, args=(self.connection,))
        self.connection_deadline.daemon = True
        self.connection_deadline.start()

    def log_message(self, format, *args):
        pass

    def send_error(self, code, message=None, explain=None):
        self.send_json(code, {"error": "invalid_http_request"})

    def send_headers(self, status, media_type, length=None, *, same_origin=False):
        self.send_response(status)
        self.send_header("Content-Type", media_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' blob:; frame-ancestors 'none'; base-uri 'none'")
        if same_origin:
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        if length is not None:
            self.send_header("Content-Length", str(length))
        self.end_headers()

    def send_json(self, status, value):
        body = json.dumps(value).encode("utf-8")
        self.send_headers(status, "application/json", len(body))
        self.wfile.write(body)

    def check_origin(self, require_same_origin):
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
            if not allowed or (require_same_origin and origins and origins[0] != expected):
                raise ConfigurationError
            if require_same_origin and self.headers.get("Sec-Fetch-Site") in {"cross-site", "same-site"}:
                raise ConfigurationError
        except ValueError:
            raise APIError(403, "origin_forbidden") from None

    def do_GET(self):
        self.dispatch()

    def do_POST(self):
        self.dispatch()

    def do_DELETE(self):
        self.dispatch()

    def dispatch(self):
        try:
            write = self.command != "GET"
            path, separator, query = self.path.partition("?")
            gallery_list = not write and path == "/api/gallery"
            gallery_image = None if separator else GALLERY_IMAGE_ROUTE.fullmatch(path)
            self.check_origin(write or gallery_list or gallery_image is not None)
            if path.startswith("/api/model-control/"):
                if separator:
                    raise APIError(404, "route_not_found")
                self.manage_models(path.removeprefix("/api/model-control/"), write)
            elif self.command == "DELETE":
                if not gallery_image or gallery_image[2]:
                    raise APIError(404, "route_not_found")
                self.delete_gallery_image(gallery_image[1])
            elif write:
                if self.path == "/api/chat":
                    self.chat(self.read_json())
                elif self.path == "/api/images":
                    self.generate_image(self.read_json())
                else:
                    raise APIError(404, "route_not_found")
            elif self.path == "/api/models":
                self.discover_models()
            elif self.path == "/api/image-model":
                self.report_image_model()
            elif self.path == "/api/config":
                self.send_json(200, {
                    "gallery_url": self.server.config.gallery_url,
                    "gallery_enabled": self.server.config.gallery_dir is not None,
                    "image_enabled": self.server.config.image_model != "",
                    "model_control_enabled": self.model_control_allowed(),
                })
            elif gallery_list:
                self.list_gallery(query if separator else None)
            elif gallery_image:
                self.send_gallery_image(gallery_image[1], thumbnail=gallery_image[2] is not None)
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
        except (APIError, ControlError) as error:
            try:
                self.send_json(error.status, {"error": error.code})
            except (ConnectionError, TimeoutError):
                return
        except (ConnectionError, TimeoutError):
            return

    def model_control_allowed(self):
        config = self.server.config
        lan_enabled = bool(config.public_origin) and not is_loopback(config.address)
        return (
            self.server.model_control is not None
            and model_control_peer(self.client_address[0], lan_enabled)
            and not any(name.lower() == "forwarded" or name.lower().startswith("x-forwarded-")
                        or name.lower() in {"via", "x-real-ip"} for name in self.headers)
        )

    def manage_models(self, route, write):
        if not self.model_control_allowed():
            raise APIError(404, "route_not_found")
        self.check_origin(True)
        if write and not self.headers.get("Origin"):
            raise APIError(403, "origin_forbidden")
        payload = self.read_json() if self.command == "POST" else {}
        response = self.server.model_control.dispatch(route, self.command, payload)
        self.send_json(200, response)

    @contextmanager
    def open_gallery(self):
        directory = self.server.config.gallery_dir
        if directory is None:
            raise APIError(404, "route_not_found")
        try:
            directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError:
            raise APIError(503, "gallery_unavailable") from None
        try:
            yield directory_fd
        finally:
            os.close(directory_fd)

    def list_gallery(self, query):
        with self.open_gallery() as directory_fd:
            before = None
            if query is not None:
                match = GALLERY_CURSOR.fullmatch(query)
                if not match:
                    raise APIError(400, "invalid_gallery_cursor")
                before = int(match[1]), match[2]
            try:
                page = heapq.nlargest(GALLERY_PAGE_SIZE + 1, gallery_entries(directory_fd, before))
            except OSError:
                raise APIError(503, "gallery_unavailable") from None
        images = page[:GALLERY_PAGE_SIZE]
        next_cursor = None
        if len(page) > GALLERY_PAGE_SIZE:
            modified, image_id = images[-1]
            next_cursor = f"{modified}.{image_id}"
        self.send_json(200, {
            "images": [{"id": image_id, "modified_ms": modified // 1_000_000} for modified, image_id in images],
            "next_cursor": next_cursor,
        })

    def send_gallery_image(self, image_id, thumbnail):
        # A thumbnail fallback can buffer a full-sized image.
        if not self.server.gallery_slots.acquire(timeout=1):
            raise APIError(503, "gallery_busy")
        try:
            body = self.read_gallery_image(image_id, thumbnail)
            self.send_headers(200, "image/png", len(body), same_origin=True)
            self.wfile.write(body)
        finally:
            self.server.gallery_slots.release()

    def read_gallery_image(self, image_id, thumbnail):
        filename = f"{image_id}.png"
        tokens = (self.server.config.token,)
        with self.open_gallery() as directory_fd:
            try:
                if thumbnail:
                    info = os.stat(filename, dir_fd=directory_fd, follow_symlinks=False)
                    if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_IMAGE:
                        raise APIError(404, "gallery_image_unavailable")
                    try:
                        return strip_png_metadata(read_png_file(directory_fd, f"{image_id}.thumb.png"), tokens)
                    except (OSError, APIError):
                        pass
                return strip_png_metadata(read_png_file(directory_fd, filename), tokens)
            except (OSError, APIError):
                raise APIError(404, "gallery_image_unavailable") from None

    def delete_gallery_image(self, image_id):
        filename = f"{image_id}.png"
        thumbnail = f"{image_id}.thumb.png"
        with self.open_gallery() as directory_fd:
            try:
                info = os.stat(filename, dir_fd=directory_fd, follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode):
                    raise APIError(404, "gallery_image_unavailable")
                # Remove the thumbnail first so a cleanup failure leaves the original intact.
                try:
                    info = os.stat(thumbnail, dir_fd=directory_fd, follow_symlinks=False)
                    if stat.S_ISREG(info.st_mode):
                        os.unlink(thumbnail, dir_fd=directory_fd)
                except FileNotFoundError:
                    pass
                os.unlink(filename, dir_fd=directory_fd)
            except FileNotFoundError:
                raise APIError(404, "gallery_image_unavailable") from None
            except OSError:
                raise APIError(503, "gallery_delete_failed") from None
        self.send_headers(204, "application/json")

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

    def upstream(self, route, consume, payload=None, *, watch_disconnect=False, response_timeout=None):
        config = self.server.config
        url = urlsplit(config.upstream)
        connection_type = http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
        connection = connection_type(url.hostname, url.port, timeout=IO_TIMEOUT)
        deadline = None
        expired = threading.Event()
        finished = threading.Event() if watch_disconnect else None
        try:
            connection.connect()
            upstream_socket = connection.sock

            def expire():
                expired.set()
                close_socket(upstream_socket)

            deadline = threading.Timer(
                REQUEST_TIMEOUT if response_timeout is None else response_timeout, expire,
            )
            deadline.daemon = True
            deadline.start()
            if finished is not None:
                threading.Thread(
                    target=cancel_on_disconnect,
                    args=(self.connection, lambda: close_socket(upstream_socket), finished),
                    daemon=True,
                ).start()
            headers = {"Authorization": f"Bearer {config.token}"}
            body = None
            if payload is not None:
                body = json.dumps(payload).encode("utf-8")
                headers["Content-Type"] = "application/json"
            connection.request("GET" if body is None else "POST", route, body, headers)
            if response_timeout is not None:
                upstream_socket.settimeout(response_timeout)
            with connection.getresponse() as response:
                if response.status in {401, 403}:
                    raise APIError(502, "upstream_authentication_failed")
                if response.status in {404, 405, 501}:
                    raise APIError(502, "upstream_route_unsupported")
                if response.status == 503 and route == "/v1/images/generations":
                    self.raise_if_image_model_unavailable(response)
                if response.status != 200:
                    raise APIError(502, "upstream_request_failed")
                return consume(response)
        except TimeoutError:
            raise APIError(504, "upstream_timeout") from None
        except (OSError, http.client.HTTPException, ValueError, RecursionError):
            if expired.is_set():
                raise APIError(504, "upstream_timeout") from None
            raise APIError(502, "invalid_upstream_response") from None
        except APIError:
            if expired.is_set():
                raise APIError(504, "upstream_timeout") from None
            raise
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

    def raise_if_image_model_unavailable(self, response):
        try:
            data = self.upstream_json(response, MAX_STREAM_LINE)
        except (APIError, ValueError, RecursionError):
            return
        error = data.get("error") if isinstance(data, dict) else None
        if isinstance(error, dict) and error.get("code") == "model_not_loaded":
            raise APIError(503, "image_model_not_loaded")

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

    def report_image_model(self):
        config = self.server.config
        if config.control_data_dir is None:
            self.send_json(200, {"state": "unknown", "name": None})
            return
        control = AtomicImageControl(config.control_data_dir)
        model = control.call("/status").get("model")
        state = model.get("state") if isinstance(model, dict) else None
        name = None
        if state == "loaded":
            loaded = model.get("loaded")
            modality = loaded.get("modality") if isinstance(loaded, dict) else None
            if modality == "image":
                state = "running"
                for key in ("displayName", "modelId"):
                    label = loaded.get(key)
                    if (isinstance(label, str) and SAFE_IMAGE_MODEL_NAME.fullmatch(label)
                            and config.token not in label and control.token not in label):
                        name = label
                        break
            else:
                state = "stopped" if modality == "video" else "unknown"
        elif state == "unloaded":
            state = "stopped"
        elif state not in ("loading", "unloading", "failed"):
            state = "unknown"
        self.send_json(200, {"state": state, "name": name})

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
        if model == "":
            raise APIError(400, "image_model_unavailable")
        prompt = payload.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise APIError(400, "invalid_prompt")
        if not self.server.image_slots.acquire(blocking=False):
            raise APIError(503, "image_busy")
        try:
            # Allow time to connect upstream and send the final response to the client.
            self.set_connection_deadline(IMAGE_TIMEOUT + 2 * IO_TIMEOUT)
            control_dir = self.server.config.control_data_dir
            if (control_dir is not None and model is None
                    and self.headers.get("Accept") == "text/event-stream"):
                self.stream_image_job(AtomicImageControl(control_dir), prompt)
            else:
                self.send_image(model, prompt)
        finally:
            self.server.image_slots.release()

    def stream_image_job(self, control, prompt):
        deadline = time.monotonic() + IMAGE_TIMEOUT
        stopped = threading.Event()
        finished = threading.Event()
        threading.Thread(
            target=cancel_on_disconnect, args=(self.connection, stopped.set, finished), daemon=True,
        ).start()
        job_route = None
        terminal = False
        streaming = False
        try:
            defaults = control.call("/capabilities").get("defaults")
            if not isinstance(defaults, dict) or not all(
                type(defaults.get(key)) is expected
                for key, expected in (("width", int), ("height", int), ("steps", int))
            ) or type(defaults.get("cfgScale")) not in (int, float):
                raise APIError(502, "invalid_upstream_response")
            request = {
                key: defaults[key]
                for key in ("width", "height", "steps", "cfgScale", "guidance", "samplingMethod", "flowShift")
                if key in defaults
            }
            if stopped.is_set():
                return
            job_id = control.call("/jobs", {**request, "prompt": prompt, "batchSize": 1}).get("jobId")
            if not isinstance(job_id, str) or not IMAGE_JOB_ID.fullmatch(job_id):
                raise APIError(502, "invalid_upstream_response")
            job_route = f"/jobs/{job_id}"
            if stopped.is_set():
                return
            self.send_headers(200, "text/event-stream; charset=utf-8")
            streaming = True
            self.send_event("progress", {"phase": "queued"})
            while not stopped.is_set():
                if time.monotonic() >= deadline:
                    raise APIError(504, "upstream_timeout")
                job = control.call(job_route).get("job")
                if not isinstance(job, dict) or job.get("id") != job_id:
                    raise APIError(502, "invalid_upstream_response")
                state = job.get("state")
                if state in ("failed", "cancelled"):
                    terminal = True
                    if state == "cancelled":
                        raise APIError(409, "image_cancelled")
                    control.raise_job_error(job.get("error"))
                if state == "completed":
                    terminal = True
                    outputs = job.get("outputs")
                    output = outputs[0] if isinstance(outputs, list) and outputs else None
                    path = output.get("path") if isinstance(output, dict) else None
                    if (not isinstance(path, str) or not os.path.isabs(path)
                            or os.path.basename(path) != f"{job_id}-00.png"
                            or output.get("id") != f"{job_id}-00"):
                        raise APIError(502, "invalid_upstream_response")
                    image = gallery_png(os.path.dirname(path), job_id, (self.server.config.token, control.token))
                    for position in range(0, len(image), IMAGE_CHUNK_SIZE):
                        chunk = base64.b64encode(image[position:position + IMAGE_CHUNK_SIZE]).decode("ascii")
                        self.send_event("image", {"data": chunk})
                    self.send_event("done", {})
                    return
                if state not in ("queued", "generating"):
                    raise APIError(502, "invalid_upstream_response")
                self.send_event("progress", image_progress(job))
                stopped.wait(IMAGE_POLL_INTERVAL)
        except APIError as error:
            if not streaming:
                raise
            if not stopped.is_set():
                self.send_event("error", {"error": error.code})
        finally:
            finished.set()
            if job_route is not None and not terminal:
                try:
                    control.call(f"{job_route}/cancel", {})
                except APIError:
                    print("Atomic image job cancellation could not be confirmed.", file=sys.stderr)

    def send_image(self, model, prompt):
        payload = {"prompt": prompt, "n": 1, "response_format": "b64_json"}
        if model is not None:
            payload["model"] = model
        data = self.upstream(
            "/v1/images/generations",
            lambda response: self.upstream_json(response, MAX_IMAGE),
            payload,
            response_timeout=IMAGE_TIMEOUT,
            watch_disconnect=self.headers.get("X-ATC-Cancel-On-Disconnect") == "1",
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
        if decoded.startswith(PNG_SIGNATURE):
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
