"""Local Atomic Server model operations and browser-safe projections."""

import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import selectors
import subprocess
import threading
import time
from urllib.parse import urlsplit
from uuid import uuid4

OPERATIONS = frozenset({
    "engines.list", "models.catalog", "models.search", "models.files",
    "models.plan", "models.install", "models.installed", "models.load",
    "artifacts.plan", "artifacts.install", "artifacts.list", "artifacts.load",
    "managed.plan", "managed.install", "managed.list", "setup.status",
})
MANAGED = {"vllm", "sglang", "tensorrt-llm"}
GGUF = {"llamacpp", "llamacpp-upstream", "atomic-prism"}
PROVIDERS = GGUF | MANAGED | {"mlx"}
PUBLIC_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,126}(?:/[A-Za-z0-9][A-Za-z0-9_.-]{0,126})?")
PUBLIC_LABEL = re.compile(r"[\w .:+()\-·]{1,120}")
FILE_SEGMENT = re.compile(r"[\w .+()\-]{1,160}")
DIGEST = re.compile(r"[0-9a-f]{64}")
MAX_OUTPUT = 16 * 1024 * 1024
REVIEW_TTL = 900
TERMINAL = {"ready", "failed", "cancelled", "canceled", "paused", "interrupted", "blocked"}
BLOCKERS = {
    "unsupported": "This model is incompatible with the engine.",
    "legacy_artifact": "Atomic requires a newer model file.",
    "insufficient_disk_space": "There is insufficient disk space.",
}


class ControlError(Exception):
    def __init__(self, status, code):
        self.status = status
        self.code = code


def objects(value):
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def mapping(value):
    return value if isinstance(value, dict) else {}


def number(value):
    return value if type(value) is int and 0 <= value <= 2**53 - 1 else None


def byte_count(row, keys):
    for source in (row, mapping(row.get("progress"))):
        for key in keys:
            value = number(source.get(key))
            if value is not None:
                return value
    return None


def matches_api_bind(bind, hostname):
    if not isinstance(bind, str) or not bind or not hostname:
        return False
    if hostname == bind:
        return True
    try:
        listener = ipaddress.ip_address(bind)
        if hostname == "localhost":
            return listener.is_loopback or listener.is_unspecified
        target = ipaddress.ip_address(hostname)
    except ValueError:
        return False
    return target.is_loopback and (
        target == listener or (listener.is_unspecified and target.version == listener.version)
    )


class ModelControl:
    def __init__(self, binary, data_folder, upstream, token):
        self.command = [binary, "--json"]
        if data_folder:
            self.command.extend(["--data-folder", str(data_folder)])
        self.upstream = urlsplit(upstream)
        self.token = token
        self.key = secrets.token_bytes(32)
        self.lock = threading.Lock()
        self.slots = threading.BoundedSemaphore(2)
        self.selections = {}
        self.reviews = {}
        self.loads = []

    def run(self, args, timeout):
        env = {key: value for key, value in os.environ.items() if key not in {
            "ATC_API_TOKEN", "ATC_API_TOKEN_FILE", "ATC_API_URL",
        }}
        try:
            with subprocess.Popen(
                self.command + args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, env=env,
            ) as process:
                try:
                    deadline = time.monotonic() + timeout
                    output = bytearray()
                    with selectors.DefaultSelector() as selector:
                        selector.register(process.stdout, selectors.EVENT_READ)
                        while True:
                            remaining = deadline - time.monotonic()
                            if remaining <= 0 or not selector.select(remaining):
                                raise subprocess.TimeoutExpired(args, timeout)
                            chunk = os.read(process.stdout.fileno(), 65536)
                            if not chunk:
                                break
                            output.extend(chunk)
                            if len(output) > MAX_OUTPUT:
                                raise ControlError(502, "model_control_response")
                    if process.wait(timeout=max(0, deadline - time.monotonic())) != 0:
                        raise ControlError(502, "model_control_failed")
                    return json.loads(output)
                finally:
                    if process.poll() is None:
                        process.kill()
        except subprocess.TimeoutExpired:
            raise ControlError(504, "model_control_timeout") from None
        except OSError:
            raise ControlError(503, "model_control_unavailable") from None
        except (ValueError, RecursionError):
            raise ControlError(502, "model_control_response") from None

    def verify_instance(self):
        status = mapping(self.run(["status"], 5))
        api = mapping(status.get("api"))
        port = self.upstream.port or (443 if self.upstream.scheme == "https" else 80)
        if status.get("running") is not True:
            raise ControlError(503, "model_control_stopped")
        if (api.get("running") is not True or api.get("port") != port
                or not matches_api_bind(api.get("host"), self.upstream.hostname)):
            raise ControlError(503, "model_control_instance")
        return status

    def call(self, operation, payload=None, timeout=35):
        if operation not in OPERATIONS:
            raise ValueError("Unsupported Atomic operation")
        result = mapping(self.run([
            "control", "call", operation, "--non-interactive", "--input",
            json.dumps(payload or {}, ensure_ascii=True),
        ], timeout))
        if result.get("operation") != operation or "result" not in result:
            raise ControlError(502, "model_control_response")
        if result.get("status") != "ok":
            raise ControlError(502, "model_control_failed")
        return result["result"]

    def label(self, value, fallback="Model"):
        if isinstance(value, str) and PUBLIC_LABEL.fullmatch(value) and self.token not in value:
            return value
        return fallback

    def public_id(self, value):
        return isinstance(value, str) and PUBLIC_ID.fullmatch(value) and self.token not in value

    def model_filename(self, filename):
        return (
            isinstance(filename, str) and len(filename) <= 512 and self.token not in filename
            and filename.lower().endswith(".gguf")
            and all(part not in {".", ".."} and FILE_SEGMENT.fullmatch(part)
                    for part in filename.split("/"))
        )

    def handle(self, *parts):
        return hmac.new(self.key, json.dumps(parts).encode(), hashlib.sha256).hexdigest()

    def remember(self, kind, payload):
        identifier = self.handle(kind, payload)
        with self.lock:
            self.selections[identifier] = (kind, payload)
            if len(self.selections) > 4096:
                self.selections.pop(next(iter(self.selections)))
        return identifier

    def selection(self, data, kind=None):
        identifier = data.get("id")
        with self.lock:
            entry = self.selections.get(identifier) if isinstance(identifier, str) else None
        if entry is None or (kind and entry[0] != kind):
            raise ControlError(409, "model_control_stale")
        return entry

    def providers(self):
        engines = objects(mapping(self.call("engines.list")).get("engines"))
        supported = [row["engine"] for row in engines if isinstance(row.get("engine"), str)
                     and row["engine"] in PROVIDERS and (row.get("latest") or row.get("active")
                                                         or row.get("builds"))]
        providers = [engine for engine in supported if engine not in GGUF]
        if GGUF.intersection(supported):
            providers.insert(0, "gguf")
        return providers

    def browse(self, data, search=False):
        category = data.get("category")
        query = data.get("query", "").strip() if isinstance(data.get("query", ""), str) else ""
        if category not in ("text", "image") or len(query) > 160 or (search and not query):
            raise ControlError(400, "invalid_request")
        providers = self.providers() if category == "text" else []
        provider = data.get("provider") or next(iter(providers), None)
        if category == "text" and provider not in providers:
            if provider is None:
                return {"models": [], "providers": [], "provider": None}
            raise ControlError(400, "model_control_provider")
        if search and category == "text":
            payload = {"query": query}
            if provider != "gguf":
                payload["engine"] = provider
            result = self.call("models.search", payload)
        else:
            payload = {"category": category}
            if category == "text" and provider not in ("gguf", "mlx"):
                payload["engine"] = provider
            result = self.call("models.catalog", payload)
        models = []
        listing = mapping(result)
        for row in objects(listing.get("rows", listing.get("models"))):
            model = row.get("id") or row.get("repository")
            if not self.public_id(model):
                continue
            name = self.label(row.get("name") or row.get("title"), model)
            source = mapping(row.get("source"))
            if category == "image":
                transformer = mapping(source.get("transformer"))
                repo = transformer.get("repo")
                if query.lower() not in f"{model} {name} {repo}".lower():
                    continue
                variants = [{"value": v["id"], "label": self.label(v.get("label"), v["id"])}
                            for v in objects(transformer.get("quants")) if self.public_id(v.get("id"))]
                engines = source.get("engines")
                if not variants or not isinstance(engines, list) or "sdcpp" not in engines:
                    continue
                identifier = self.remember("artifact-model", {"category": "image", "model_id": model,
                                                              "variants": variants})
            elif provider == "gguf":
                if "/" not in model or source.get("format") not in (None, "gguf"):
                    continue
                identifier = self.remember("gguf-model", {"repo": model})
            elif provider == "mlx":
                if "/" not in model or (not search and source.get("format") != "mlx"):
                    continue
                identifier = self.remember("artifact-model", {"category": "text", "model_id": model})
            else:
                repo = row.get("repository") or model
                if not self.public_id(repo) or "/" not in repo:
                    continue
                identifier = self.remember("managed", {"repository": repo, "engine": provider})
            models.append({"id": identifier, "label": name})
            if len(models) == 100:
                break
        return {"models": models, "providers": providers, "provider": provider}

    def variants(self, data):
        kind, item = self.selection(data)
        if kind == "gguf-model":
            rows = self.call("models.files", item)
            variants = []
            for row in objects(rows):
                filename = row.get("filename")
                if not self.model_filename(filename):
                    continue
                request = {**item, "file": filename, "include_projector": True}
                revision = row.get("revision")
                if isinstance(revision, str) and re.fullmatch(r"[0-9a-f]{40}", revision):
                    request["revision"] = revision
                variants.append({"id": self.remember("gguf", request), "label": filename.rsplit("/", 1)[-1],
                                 "bytes": number(row.get("size"))})
                if len(variants) == 250:
                    break
            return {"variants": variants}
        if kind == "artifact-model":
            request = {key: item[key] for key in ("category", "model_id")}
            variants = item.get("variants", [{"label": "Default"}])
            return {"variants": [
                {"id": self.remember("artifact", request | ({"quant": variant["value"]}
                 if "value" in variant else {})), "label": variant["label"]} for variant in variants
            ]}
        if kind == "managed":
            return {"variants": [{"id": data["id"], "label": item["engine"]}]}
        raise ControlError(409, "model_control_stale")

    def review(self, data):
        kind, request = self.selection(data)
        operation = {"gguf": "models.plan", "artifact": "artifacts.plan", "managed": "managed.plan"}.get(kind)
        if not operation:
            raise ControlError(409, "model_control_stale")
        plan = mapping(self.call(operation, request, timeout=90))
        plan = mapping(plan.get("plan")) or plan
        digest = plan.get("digest") or plan.get("plan_digest")
        blockers = plan.get("blockers") or []
        verdict = mapping(plan.get("verdict"))
        outcome = self.label(verdict.get("outcome"), "")
        compatibility = mapping(mapping(verdict.get("compatibility")).get("verdict"))
        if kind == "managed" and type(compatibility.get("ok")) is bool:
            outcome = "Compatible" if compatibility["ok"] else "Incompatible"
        total = plan.get("total_download_bytes", plan.get("download_bytes"))
        free = number(plan.get("free_bytes"))
        messages = []
        if blockers:
            for blocker in (blockers if isinstance(blockers, list) else [])[:10]:
                code = mapping(blocker).get("code")
                message = BLOCKERS.get(code) if isinstance(code, str) else None
                if isinstance(blocker, str) and blocker.startswith("Not enough free"):
                    message = BLOCKERS["insufficient_disk_space"]
                if message:
                    messages.append(message)
            if not messages:
                messages.append("Atomic reported a compatibility or installation blocker.")
        info = {"bytes": number(total), "free_bytes": free, "blocked": bool(blockers),
                "blockers": list(dict.fromkeys(messages)), "verdict": outcome,
                "provider": self.label(plan.get("provider") or plan.get("engine"), "")}
        if not isinstance(digest, str) or not DIGEST.fullmatch(digest):
            if blockers:
                return {"id": None, **info}
            raise ControlError(502, "model_control_response")
        identifier = self.handle("review", kind, request, digest)
        with self.lock:
            now = time.monotonic()
            self.reviews = {key: value for key, value in self.reviews.items()
                            if value["expires"] > now or value["active"]}
            if identifier not in self.reviews:
                if len(self.reviews) >= 256:
                    raise ControlError(503, "model_control_busy")
                self.reviews[identifier] = {"expires": now + REVIEW_TTL, "kind": kind, "request": request,
                                            "digest": digest, "request_id": str(uuid4()), "active": False,
                                            "accepted": False, "blocked": bool(blockers)}
            else:
                self.reviews[identifier].update(expires=now + REVIEW_TTL, blocked=bool(blockers))
        return {"id": identifier, **info}

    def download(self, data):
        identifier = data.get("id")
        with self.lock:
            review = self.reviews.get(identifier) if isinstance(identifier, str) else None
            if review is None or review["expires"] <= time.monotonic() or review["blocked"]:
                raise ControlError(409, "model_control_stale")
            if review["accepted"]:
                return {"accepted": True}
            if review["active"]:
                raise ControlError(503, "model_control_busy")
            review["active"] = True
        try:
            kind = review["kind"]
            operation = {"gguf": "models.install", "artifact": "artifacts.install",
                         "managed": "managed.install"}[kind]
            payload = {"plan_digest": review["digest"], "request_id": review["request_id"]}
            if kind == "gguf":
                payload["request"] = review["request"]
            self.call(operation, payload)
            with self.lock:
                review["accepted"] = True
            return {"accepted": True}
        finally:
            with self.lock:
                review["active"] = False

    def inventory(self):
        installed = []
        mlx_artifacts = set()
        for row in objects(self.call("artifacts.list")):
            plan = mapping(row.get("plan"))
            artifact_id = row.get("id")
            category, engine = plan.get("category"), plan.get("engine")
            is_mlx = category == "text" and engine == "mlx"
            if (row.get("stage") != "ready" or not isinstance(artifact_id, str)
                    or not (category == "image" or is_mlx)):
                continue
            model_id = plan.get("model_id")
            name = self.label(plan.get("name"))
            variant = self.label(model_id.partition(":")[2], "") if isinstance(model_id, str) else ""
            if is_mlx and isinstance(model_id, str):
                mlx_artifacts.add(model_id)
            installed.append((self.handle("load", category, artifact_id), category,
                              f"{name} · {variant}" if variant else name, "mlx" if is_mlx else "sd-cpp",
                              "artifacts.load", {"id": artifact_id}))
        for row in objects(self.call("models.installed")):
            model = row.get("id") or row.get("model_id")
            provider = row.get("provider") or row.get("engine")
            if not isinstance(model, str) or not model or not isinstance(provider, str) or provider not in PROVIDERS:
                continue
            fallback = self.label(model.replace("\\", "/").rsplit("/", 1)[-1])
            alternatives = row.get("providers")
            if provider in MANAGED and isinstance(alternatives, list) and provider not in alternatives:
                continue
            providers = [provider]
            if isinstance(alternatives, list):
                providers.extend(item for item in alternatives if isinstance(item, str)
                                 and item in PROVIDERS and item not in providers)
            for engine in providers:
                if engine == "mlx" and model in mlx_artifacts:
                    continue
                installed.append((self.handle("load", "text", engine, model), "text",
                                  self.label(row.get("name"), fallback), engine,
                                  "models.load", {"provider": engine, "model_id": model}))
        return installed

    def installed(self):
        return {"models": [{"id": identifier, "category": category, "label": label, "provider": provider}
                           for identifier, category, label, provider, _, _ in self.inventory()]}

    def load(self, data):
        with self.lock:
            if self.loads and self.loads[-1]["status"] == "loading":
                raise ControlError(503, "model_control_busy")
        selected = next((row for row in self.inventory() if row[0] == data.get("id")), None)
        if selected is None:
            raise ControlError(409, "model_control_stale")
        identifier, category, label, _, operation, payload = selected
        job = {"id": str(uuid4()), "selection": identifier, "category": category, "label": label,
               "status": "loading"}
        with self.lock:
            if self.loads and self.loads[-1]["status"] == "loading":
                raise ControlError(503, "model_control_busy")
            self.loads.append(job)
            del self.loads[:-20]
        accepted = dict(job)
        threading.Thread(target=self.finish_load, args=(job, operation, payload), daemon=True).start()
        return {"load": accepted}

    def finish_load(self, job, operation, payload):
        state = "ready"
        error = None
        try:
            self.verify_instance()
            self.call(operation, payload, timeout=600)
        except ControlError as exc:
            state, error = "failed", exc.code
        with self.lock:
            job.update(status=state, error=error)

    def status(self, instance):
        setups = mapping(self.call("setup.status")).get("setups")
        downloads = []
        sources = [(setups, "text"), (self.call("managed.list"), "text"), (self.call("artifacts.list"), None)]
        tasks = {}
        for task in objects(instance.get("downloads")):
            identifier = task.get("taskId") or task.get("task_id") or task.get("id")
            if isinstance(identifier, str):
                tasks[identifier] = task
        for rows, category in sources:
            for row in objects(rows):
                plan = mapping(row.get("plan"))
                kind = category or plan.get("category")
                if kind not in ("text", "image"):
                    continue
                stage = row.get("stage") or row.get("status")
                stage = self.label(stage, "pending")
                model = plan.get("name") or plan.get("model_id") or plan.get("repository") or row.get("model_id")
                label = self.label(model.rsplit("/", 1)[-1]) if isinstance(model, str) else "Model"
                ids = [row.get("id"), row.get("setup_id"), *mapping(row.get("task_ids")).values()]
                related = [tasks[identifier] for identifier in dict.fromkeys(
                    value for value in ids if isinstance(value, str) and value in tasks
                )]
                byte_keys = ("transferred", "downloadedBytes", "downloaded_bytes")
                transferred = byte_count(row, byte_keys)
                if transferred is None and related:
                    transferred = sum(byte_count(task, byte_keys) or 0 for task in related)
                total = byte_count(plan, ("total_download_bytes", "download_bytes"))
                if total is None:
                    total = byte_count(row, ("total", "totalBytes", "total_bytes"))
                active = stage not in TERMINAL
                updated = number(row.get("updated_at")) or number(row.get("created_at")) or 0
                downloads.append((active, updated, {"category": kind, "label": label, "status": stage,
                                  "active": active, "bytes": total if stage == "ready" else transferred,
                                  "total": total}))
        downloads.sort(key=lambda item: (item[0], item[1]), reverse=True)
        with self.lock:
            loads = [dict(job) for job in reversed(self.loads)]
        return {"downloads": [job for _, _, job in downloads[:100]], "loads": loads}

    def dispatch(self, route, method, data):
        methods = {
            ("catalog", "POST"): self.browse, ("search", "POST"): lambda value: self.browse(value, True),
            ("variants", "POST"): self.variants, ("review", "POST"): self.review,
            ("download", "POST"): self.download, ("load", "POST"): self.load,
            ("installed", "GET"): lambda _: self.installed(),
        }
        handler = methods.get((route, method))
        is_status = (route, method) == ("status", "GET")
        if handler is None and not is_status:
            raise ControlError(404, "route_not_found")
        if not self.slots.acquire(blocking=False):
            raise ControlError(503, "model_control_busy")
        try:
            instance = self.verify_instance()
            return self.status(instance) if is_status else handler(data)
        finally:
            self.slots.release()
