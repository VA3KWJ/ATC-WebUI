"use strict";

const elements = Object.fromEntries(
  Array.from(document.querySelectorAll("[id]"), (element) => [element.id, element]),
);
const HISTORY_KEY = "atomic-webui.history.v1";
const TURN_PREFIX = "atomic-webui.turn.";
const ACTIVE_KEY = "atomic-webui.active";
const pendingHistoryWrites = new Map();
const THEME_KEY = "atomic-webui.theme";
const MAX_CHAT_REQUEST = 256 * 1024;
const MAX_IMAGE_BYTES = 24 * 1024 * 1024;
const IMAGE_MODEL_REFRESH_MS = 10000;
const GALLERY_IMAGE_ID = /^[0-9a-f]{32}-[0-9]{2}$/;
const galleryDate = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });
const encoder = new TextEncoder();
const systemTheme = window.matchMedia("(prefers-color-scheme: light)");
const state = {
  conversations: [],
  activeId: null,
  models: [],
  loadingModels: false,
  chatRequest: null,
  imageRequest: null,
  imageEnabled: false,
  previewUrl: null,
  gallery: { enabled: false, loaded: false, nextCursor: null, request: null },
};

const errors = {
  upstream_authentication_failed: "The Atomic API rejected its credentials. Contact the operator.",
  upstream_route_unsupported: "This operation is unsupported by the Atomic API.",
  model_unavailable: "The selected model is unavailable. Refresh models and choose another.",
  image_model_unavailable: "Image generation is disabled in the WebUI configuration.",
  image_model_not_loaded: "The requested image model is unavailable. Load it in Atomic Server and try again.",
  image_control_unavailable: "Could not reach Atomic's local image job API. Check the WebUI configuration.",
  image_cancelled: "Image generation stopped.",
  image_out_of_memory: "Atomic ran out of memory while generating the image.",
  image_stream_interrupted: "Image progress was interrupted. Try again.",
  image_busy: "The server is already generating images. Try again shortly.",
  gallery_unavailable: "Could not read Atomic’s image library. Try refreshing the gallery.",
  gallery_image_unavailable: "This image is no longer available. Refresh the gallery.",
  gallery_delete_failed: "Could not delete this image. Check that the WebUI can write to the image directory.",
  invalid_prompt: "Enter an image prompt.",
  invalid_messages: "The conversation could not be sent. Start a new conversation and try again.",
  request_too_large: "The request is too large. Shorten your message and try again.",
  upstream_response_too_large: "The Atomic API response exceeded the size limit.",
  upstream_timeout: "The Atomic API took too long to respond.",
  stream_interrupted: "The reply was interrupted. Any received text has been kept.",
  invalid_upstream_response: "The Atomic API sent an invalid response.",
  upstream_request_failed: "The Atomic API could not complete the request.",
  route_not_found: "This operation is unsupported by the WebUI server.",
};

function describeError(code, fallback) {
  return Object.hasOwn(errors, code) ? errors[code] : fallback;
}

function feedback(id, message, status = "") {
  elements[id].textContent = message;
  elements[id].dataset.state = status;
}

function readStorage(key) {
  try {
    return localStorage.getItem(key);
  } catch {
    feedback("storage-status", "Browser storage is unavailable. Changes will last for this session.");
    return null;
  }
}

function writeStorage(key, value) {
  try {
    localStorage.setItem(key, value);
    return true;
  } catch {
    feedback("storage-status", "Could not save changes to browser storage.");
    return false;
  }
}

function loadHistory() {
  const saved = readStorage(HISTORY_KEY);
  let legacyActiveId = null;
  if (saved) {
    try {
      const history = JSON.parse(saved);
      if (!Array.isArray(history.conversations)) throw new Error();
      state.conversations = history.conversations.filter((conversation) =>
        conversation && typeof conversation.id === "string" && validMessages(conversation.messages),
      );
      legacyActiveId = history.activeId;
    } catch {
      feedback("storage-status", "Saved conversations could not be loaded.");
    }
  }
  try {
    const conversations = new Map(state.conversations.map((conversation) => [conversation.id, conversation]));
    const newConversations = [];
    const keys = Object.keys(localStorage).filter((key) => key.startsWith(TURN_PREFIX)).sort();
    for (const key of keys) {
      try {
        const turn = JSON.parse(localStorage.getItem(key));
        if (!turn || typeof turn.id !== "string" || !validMessages(turn.messages)) continue;
        let conversation = conversations.get(turn.id);
        if (!conversation) {
          conversation = { id: turn.id, messages: [] };
          conversations.set(turn.id, conversation);
          newConversations.push(conversation);
        }
        conversation.messages.push(...turn.messages);
      } catch {
        feedback("storage-status", "Some saved messages could not be loaded.");
      }
    }
    state.conversations = [...newConversations.reverse(), ...state.conversations];
    const activeId = localStorage.getItem(ACTIVE_KEY) ?? legacyActiveId;
    state.activeId = conversations.has(activeId) ? activeId : null;
  } catch {
    feedback("storage-status", "Saved conversations could not be loaded.");
  }
}

function validMessages(messages) {
  return Array.isArray(messages) && messages.length > 0 && messages.every((message) =>
    message && ["user", "assistant"].includes(message.role) && typeof message.content === "string",
  );
}

function randomId() {
  // getRandomValues also works when the UI is served over plain HTTP.
  return Array.from(crypto.getRandomValues(new Uint32Array(4)), (part) =>
    part.toString(16).padStart(8, "0"),
  ).join("");
}

let lastTurnTime = 0;

function newTurnKey() {
  lastTurnTime = Math.max(Date.now(), lastTurnTime + 1);
  return TURN_PREFIX + String(lastTurnTime).padStart(16, "0") + "." + randomId();
}

function saveTurn(key, id, messages) {
  saveHistoryEntry(key, JSON.stringify({ id, messages }));
}

function saveHistoryEntry(key, value) {
  pendingHistoryWrites.set(key, value);
  for (const [entry, pending] of pendingHistoryWrites) {
    try {
      if (pending === null) localStorage.removeItem(entry);
      else localStorage.setItem(entry, pending);
      pendingHistoryWrites.delete(entry);
    } catch {
      // Retry pending entries the next time history is saved.
    }
  }
  feedback("storage-status", pendingHistoryWrites.size ? "Could not save changes to browser storage." : "");
}

function saveActiveConversation() {
  saveHistoryEntry(ACTIVE_KEY, state.activeId ?? "");
}

function currentConversation() {
  return state.conversations.find(({ id }) => id === state.activeId);
}

function showView(view) {
  elements.chat.hidden = view !== "chat";
  elements.images.hidden = view !== "images";
  elements["models-panel"].hidden = view !== "models";
  elements["models-view"].setAttribute("aria-pressed", String(view === "models"));
  elements["chat-view"].setAttribute("aria-pressed", String(view === "chat"));
  elements["image-view"].setAttribute("aria-pressed", String(view === "images"));
  if (view === "images" && !state.gallery.loaded && !state.gallery.request) loadGallery();
}

function renderHistory() {
  elements.history.replaceChildren();
  for (const conversation of state.conversations) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = conversation.messages[0].content.replace(/\s+/g, " ").slice(0, 75);
    button.title = button.textContent;
    button.disabled = Boolean(state.chatRequest);
    if (conversation.id === state.activeId) button.setAttribute("aria-current", "true");
    button.addEventListener("click", () => {
      state.activeId = conversation.id;
      elements["chat-prompt"].value = "";
      feedback("chat-status", "");
      saveActiveConversation();
      showView("chat");
      renderConversation();
      renderHistory();
      elements["chat-prompt"].focus();
    });
    elements.history.append(button);
  }
}

function appendMessage(role, content) {
  const article = document.createElement("article");
  article.className = `message ${role}`;
  const heading = document.createElement("h2");
  heading.textContent = role === "user" ? "You" : "Assistant";
  const paragraph = document.createElement("p");
  paragraph.textContent = content;
  article.append(heading, paragraph);
  elements.messages.append(article);
  elements.welcome.hidden = true;
  return paragraph;
}

function scrollMessages() {
  elements.messages.scrollTop = elements.messages.scrollHeight;
}

function renderConversation() {
  elements.messages.replaceChildren();
  const messages = currentConversation()?.messages ?? [];
  for (const { role, content } of messages) appendMessage(role, content);
  elements.welcome.hidden = messages.length > 0;
  scrollMessages();
}

function updateControls() {
  const chatting = Boolean(state.chatRequest);
  elements.model.disabled = chatting || state.loadingModels || !state.models.length;
  elements.send.disabled = elements.model.disabled || !elements.model.value;
  elements.stop.hidden = !chatting;
  elements.stop.disabled = Boolean(state.chatRequest?.signal.aborted);
  elements["new-chat"].disabled = chatting;
  elements.refresh.disabled = chatting || state.loadingModels;
  elements.generate.disabled = !state.imageEnabled || Boolean(state.imageRequest);
  elements["stop-image"].hidden = !state.imageRequest;
  elements["stop-image"].disabled = Boolean(state.imageRequest?.signal.aborted);
  elements["image-progress"].hidden = !state.imageRequest;
}

function renderModels(previous) {
  elements.model.replaceChildren();
  for (const { id, label } of state.models) {
    const option = document.createElement("option");
    option.value = id;
    option.textContent = label;
    elements.model.append(option);
  }
  if (!state.models.length) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "No models";
    elements.model.append(option);
  } else if (state.models.some(({ id }) => id === previous)) {
    elements.model.value = previous;
  }
}

async function request(path, options) {
  let response;
  try {
    response = await fetch(path, { ...options, credentials: "same-origin" });
  } catch (error) {
    if (options?.signal?.aborted) throw error;
    throw new Error("Could not reach the WebUI server. Try again.");
  }
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    let fallback = "The request failed. Try again.";
    if (response.status === 401 || response.status === 403) {
      fallback = "Authentication or access was denied.";
    } else if (response.status === 404 || response.status === 405 || response.status === 501) {
      fallback = errors.route_not_found;
    }
    const error = new Error(describeError(body?.error, fallback));
    error.status = response.status;
    throw error;
  }
  return response;
}

async function refreshModels() {
  if (state.loadingModels || state.chatRequest) return;
  state.loadingModels = true;
  const previous = elements.model.value;
  updateControls();
  feedback("api-status", "API · Checking…");
  feedback("model-status", "Chat models · Checking…");
  try {
    const response = await request("/api/models");
    const body = await response.json();
    if (!Array.isArray(body.models) || !body.models.every((model) =>
      model && typeof model.id === "string" && model.id &&
      typeof model.label === "string",
    )) {
      throw new Error("Model discovery returned an invalid response.");
    }
    state.models = body.models;
    feedback("api-status", "API · Available", "available");
    const count = state.models.length;
    feedback("model-status", count ? `Chat models · ${count} available` : "Chat models · None available",
      count ? "available" : "");
  } catch (error) {
    state.models = [];
    feedback("api-status", `API · ${error.message}`, "error");
    feedback("model-status", "Chat models · Availability unknown");
  } finally {
    state.loadingModels = false;
    renderModels(previous);
    updateControls();
    refreshChatAfterLoad();
  }
}

const imageModelStatus = {
  running: ["Running", "available"],
  stopped: ["Not running", ""],
  loading: ["Diffusion engine loading…", ""],
  unloading: ["Diffusion engine unloading…", ""],
  failed: ["Diffusion engine load failed", "error"],
  unknown: ["Status unavailable", ""],
};

let checkingImageModel = false;
async function refreshImageModel() {
  if (checkingImageModel || document.hidden) return;
  checkingImageModel = true;
  let status = "unknown";
  let name;
  try {
    const response = await request("/api/image-model");
    const body = await response.json();
    if (Object.hasOwn(imageModelStatus, body?.state)) status = body.state;
    if (status === "running" && typeof body.name === "string") name = body.name;
  } catch {
    status = "unknown";
  } finally {
    checkingImageModel = false;
  }
  const [label, appearance] = imageModelStatus[status];
  const message = `Local image model · ${label}${name ? ` · ${name}` : ""}`;
  if (elements["image-model-status"].textContent !== message) {
    feedback("image-model-status", message, appearance);
  }
}

async function loadImageConfig() {
  try {
    const response = await request("/api/config");
    const config = await response.json();
    elements["models-view"].hidden = config.model_control_enabled !== true;
    state.imageEnabled = config.image_enabled === true;
    state.gallery.enabled = config.gallery_enabled === true;
    elements["local-gallery"].hidden = !state.gallery.enabled;
    if (!elements.images.hidden) loadGallery();
    if (typeof config.gallery_url === "string") {
      try {
        const galleryUrl = new URL(config.gallery_url);
        if (["http:", "https:"].includes(galleryUrl.protocol)) {
          elements.gallery.href = galleryUrl.href;
          elements.gallery.hidden = false;
        }
      } catch {
        elements.gallery.hidden = true;
      }
    }
    feedback("image-status", state.imageEnabled ? "" : errors.image_model_unavailable);
  } catch (error) {
    feedback("image-status", error.message, "error");
  } finally {
    updateControls();
  }
}

function galleryCard({ id, modified_ms }) {
  const date = new Date(modified_ms);
  const label = galleryDate.format(date);
  const imageUrl = `/api/gallery/${id}`;
  const card = document.createElement("figure");
  card.dataset.imageId = id;
  const link = document.createElement("a");
  link.href = imageUrl;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  link.setAttribute("aria-label", `Open full-size image from ${label} in a new tab`);
  const image = document.createElement("img");
  image.loading = "lazy";
  image.alt = "Generated image";
  image.addEventListener("error", () => { image.src = imageUrl; }, { once: true });
  image.src = `${imageUrl}/thumbnail`;
  const caption = document.createElement("figcaption");
  const time = document.createElement("time");
  time.dateTime = date.toISOString();
  time.textContent = label;
  caption.append(time);
  link.append(image);
  const actions = document.createElement("div");
  actions.className = "gallery-actions";
  const save = document.createElement("a");
  save.className = "secondary action-link";
  save.href = imageUrl;
  save.download = `generated-${id}.png`;
  save.textContent = "Save";
  save.setAttribute("aria-label", `Save image from ${label}`);
  const remove = document.createElement("button");
  remove.className = "secondary";
  remove.type = "button";
  remove.textContent = "Delete";
  remove.setAttribute("aria-label", `Delete image from ${label}`);
  remove.addEventListener("click", async () => {
    if (!window.confirm("Permanently delete this image from Atomic’s library?")) return;
    const hadFocus = card.contains(document.activeElement);
    remove.disabled = true;
    try {
      await request(imageUrl, { method: "DELETE" });
      const gallery = state.gallery;
      gallery.request?.abort();
      const grid = elements["gallery-grid"];
      const currentCard = grid.querySelector(`[data-image-id="${id}"]`);
      if (currentCard?.contains(document.activeElement) || (hadFocus && document.activeElement === document.body)) {
        elements["gallery-refresh"].focus();
      }
      currentCard?.remove();
      const hasCards = grid.childElementCount > 0;
      if (!hasCards && gallery.nextCursor) loadGallery();
      else feedback("gallery-status", hasCards ? "Image deleted." : "No generated images yet.");
    } catch (error) {
      feedback("gallery-status", error.message, "error");
      remove.disabled = false;
      if (document.activeElement === document.body && card.isConnected) remove.focus();
    }
  });
  actions.append(save, remove);
  const media = document.createElement("div");
  media.className = "gallery-media";
  media.append(link, actions);
  card.append(media, caption);
  return card;
}

async function loadGallery(append = false) {
  const gallery = state.gallery;
  if (!gallery.enabled || (append && (!gallery.nextCursor || gallery.request))) return;
  gallery.request?.abort();
  const controller = new AbortController();
  gallery.request = controller;
  const grid = elements["gallery-grid"];
  const more = elements["gallery-more"];
  const query = append ? `?before=${encodeURIComponent(gallery.nextCursor)}` : "";
  more.disabled = true;
  grid.setAttribute("aria-busy", "true");
  feedback("gallery-status", "Loading images…");
  try {
    const response = await request(`/api/gallery${query}`, { signal: controller.signal });
    const page = await response.json();
    controller.signal.throwIfAborted();
    if (!Array.isArray(page?.images) || !page.images.every((image) =>
      typeof image?.id === "string" && GALLERY_IMAGE_ID.test(image.id) &&
      Number.isInteger(image.modified_ms) && Math.abs(image.modified_ms) <= 8.64e15
    ) || (page.next_cursor !== null && typeof page.next_cursor !== "string")) {
      throw new Error("The gallery returned an invalid response.");
    }
    const cards = page.images.map(galleryCard);
    if (append) grid.append(...cards);
    else grid.replaceChildren(...cards);
    gallery.nextCursor = page.next_cursor;
    gallery.loaded = true;
    feedback("gallery-status", !append && !cards.length ? "No generated images yet." : "");
  } catch (error) {
    if (!controller.signal.aborted) feedback("gallery-status", error.message, "error");
  } finally {
    if (gallery.request === controller) {
      gallery.request = null;
      grid.setAttribute("aria-busy", "false");
      more.hidden = !gallery.nextCursor;
      more.disabled = false;
    }
  }
}

function postJson(path, payload, options) {
  return request(path, {
    ...options,
    method: "POST",
    headers: { "Content-Type": "application/json", ...options?.headers },
    body: JSON.stringify(payload),
  });
}

function chatPayload(model, history, message) {
  // Keep the complete transcript locally and include as many recent turns as fit.
  const size = (value) => encoder.encode(JSON.stringify(value)).length;
  let bytes = size({ model, messages: [message] });
  if (bytes > MAX_CHAT_REQUEST) return null;
  let start = history.length;
  for (let i = history.length - 1; i >= 0; i--) {
    bytes += 1 + size(history[i]);
    if (bytes > MAX_CHAT_REQUEST) break;
    if (history[i].role === "user") start = i;
  }
  return { model, messages: [...history.slice(start), message] };
}

async function readEvents(response, interruptedMessage, events, onEvent) {
  const interrupted = new Error(interruptedMessage);
  if (!response.headers.get("Content-Type")?.startsWith("text/event-stream") || !response.body) {
    throw new Error(errors.invalid_upstream_response);
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let pending = "";
  try {
    while (true) {
      const { value, done } = await reader.read().catch(() => { throw interrupted; });
      if (done) throw interrupted;
      pending += decoder.decode(value, { stream: true });
      let boundary = /\r?\n\r?\n/.exec(pending);
      while (boundary) {
        const frame = pending.slice(0, boundary.index);
        pending = pending.slice(boundary.index + boundary[0].length);
        const lines = frame.split(/\r?\n/);
        const event = lines.find((line) => line.startsWith("event:"))?.slice(6).trim();
        const data = lines.filter((line) => line.startsWith("data:"))
          .map((line) => line.slice(5).replace(/^ /, "")).join("\n");
        if (event === "done") return;
        if (event === "error" || events.includes(event)) {
          let payload;
          try {
            payload = JSON.parse(data);
          } catch {
            throw interrupted;
          }
          if (event === "error") throw new Error(describeError(payload?.error, interruptedMessage));
          onEvent(event, payload);
        }
        boundary = /\r?\n\r?\n/.exec(pending);
      }
    }
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}

function readChat(response, onDelta) {
  return readEvents(response, errors.stream_interrupted, ["delta"], (event, payload) => {
    if (typeof payload?.text !== "string") throw new Error(errors.stream_interrupted);
    onDelta(payload.text);
  });
}

async function submitChat(event) {
  event.preventDefault();
  const prompt = elements["chat-prompt"].value.trim();
  const model = elements.model.value;
  if (!prompt || !model || state.chatRequest || state.loadingModels) return;

  let conversation = currentConversation();
  const message = { role: "user", content: prompt };
  const payload = chatPayload(model, conversation?.messages ?? [], message);
  if (!payload) {
    feedback("chat-status", errors.request_too_large, "error");
    return;
  }
  elements["chat-prompt"].readOnly = true;
  const isNewConversation = !conversation;
  if (isNewConversation) {
    conversation = { id: randomId(), messages: [] };
    state.conversations.unshift(conversation);
    state.activeId = conversation.id;
  }
  const turnKey = newTurnKey();
  conversation.messages.push(message);
  const controller = new AbortController();
  state.chatRequest = controller;
  elements["chat-prompt"].value = "";
  feedback("chat-status", "Waiting for a reply…");
  renderConversation();
  renderHistory();
  updateControls();
  saveTurn(turnKey, conversation.id, [message]);
  if (isNewConversation) saveActiveConversation();

  let reply = "";
  let paragraph;
  let painted = 0;
  let animationFrame = null;
  let rejected = false;
  function paintReply() {
    animationFrame = null;
    if (painted === reply.length) return;
    const follow = !paragraph ||
      elements.messages.scrollHeight - elements.messages.scrollTop - elements.messages.clientHeight < 48;
    paragraph ??= appendMessage("assistant", "");
    paragraph.append(document.createTextNode(reply.slice(painted)));
    painted = reply.length;
    if (follow) scrollMessages();
  }

  try {
    const response = await postJson("/api/chat", payload, {
      signal: controller.signal,
      headers: { "X-ATC-Cancel-On-Disconnect": "1" },
    });
    await readChat(response, (delta) => {
      if (!reply && delta) feedback("chat-status", "Receiving reply…");
      reply += delta;
      animationFrame ??= requestAnimationFrame(paintReply);
    });
    feedback("chat-status", reply ? "Reply complete." : "The model returned an empty reply.");
  } catch (error) {
    if (error.status === 413) {
      rejected = true;
      conversation.messages.pop();
      saveHistoryEntry(turnKey, null);
      if (!conversation.messages.length) {
        state.conversations = state.conversations.filter(({ id }) => id !== conversation.id);
        state.activeId = null;
        if ((pendingHistoryWrites.get(ACTIVE_KEY) ?? readStorage(ACTIVE_KEY)) === conversation.id) {
          saveActiveConversation();
        }
      }
      elements["chat-prompt"].value = prompt;
      renderConversation();
    }
    feedback("chat-status", controller.signal.aborted ? "Reply stopped." : error.message,
      controller.signal.aborted ? "" : "error");
  } finally {
    if (animationFrame !== null) cancelAnimationFrame(animationFrame);
    paintReply();
    if (!rejected && (reply || pendingHistoryWrites.has(turnKey))) {
      const turn = [message];
      if (reply) {
        const answer = { role: "assistant", content: reply };
        conversation.messages.push(answer);
        turn.push(answer);
      }
      saveTurn(turnKey, conversation.id, turn);
    }
    state.chatRequest = null;
    refreshChatAfterLoad();
    elements["chat-prompt"].readOnly = false;
    renderHistory();
    updateControls();
  }
}

function clearPreview() {
  elements.preview.hidden = true;
  elements["generated-image"].removeAttribute("src");
  elements["open-image"].removeAttribute("href");
  elements["download-image"].removeAttribute("href");
  elements["image-caption"].textContent = "";
  if (state.previewUrl) URL.revokeObjectURL(state.previewUrl);
  state.previewUrl = null;
}

const imageExtensions = { "image/png": "png", "image/jpeg": "jpg", "image/webp": "webp" };

const imagePhases = {
  queued: "Image queued…",
  encoding: "Preparing image…",
  sampling: "Generating image…",
  decoding: "Decoding image…",
  postprocessing: "Processing image…",
  saving: "Saving image…",
};

function showImageProgress(progress) {
  const bar = elements["image-progress-bar"];
  if (Number.isFinite(progress?.fraction) && progress.fraction >= 0 && progress.fraction <= 1) {
    bar.value = progress.fraction;
  } else {
    bar.removeAttribute("value");
  }
  const phase = Object.hasOwn(imagePhases, progress?.phase) ? imagePhases[progress.phase] : imagePhases.sampling;
  const details = [phase];
  if (progress?.phase === "sampling" && Number.isInteger(progress.step) && Number.isInteger(progress.totalSteps)
      && progress.totalSteps > 0 && progress.step >= 0 && progress.step <= progress.totalSteps) {
    details.push(`${progress.step}/${progress.totalSteps} steps`);
  }
  if (Number.isFinite(progress?.etaSeconds) && progress.etaSeconds > 0) {
    details.push(`~${Math.ceil(progress.etaSeconds)}s remaining`);
  }
  const message = details.join(" · ");
  if (message !== elements["image-status"].textContent) feedback("image-status", message);
}

async function readImage(response) {
  if (!response.headers.get("Content-Type")?.startsWith("text/event-stream")) {
    feedback("image-status", "Receiving image…");
    return response.blob();
  }
  const chunks = [];
  let size = 0;
  await readEvents(response, errors.image_stream_interrupted, ["progress", "image"], (event, payload) => {
    if (event === "progress") showImageProgress(payload);
    if (event !== "image") return;
    if (typeof payload?.data !== "string" || !payload.data || payload.data.length > 64 * 1024) {
      throw new Error(errors.invalid_upstream_response);
    }
    let bytes;
    try {
      bytes = Uint8Array.from(atob(payload.data), (character) => character.charCodeAt(0));
    } catch {
      throw new Error(errors.invalid_upstream_response);
    }
    size += bytes.byteLength;
    if (size > MAX_IMAGE_BYTES) throw new Error(errors.upstream_response_too_large);
    if (!chunks.length) feedback("image-status", "Receiving image…");
    chunks.push(bytes);
  });
  if (!size) throw new Error(errors.image_stream_interrupted);
  return new Blob(chunks, { type: "image/png" });
}

async function submitImage(event) {
  event.preventDefault();
  const prompt = elements["image-prompt"].value.trim();
  if (!prompt || !state.imageEnabled || state.imageRequest) return;
  const controller = new AbortController();
  state.imageRequest = controller;
  elements["image-progress-bar"].removeAttribute("value");
  const started = performance.now();
  const updateElapsed = () => {
    const seconds = Math.floor((performance.now() - started) / 1000);
    const minutes = Math.floor(seconds / 60);
    elements["image-elapsed"].textContent = `Elapsed ${minutes}:${String(seconds % 60).padStart(2, "0")}`;
  };
  updateElapsed();
  const elapsedTimer = setInterval(updateElapsed, 1000);
  clearPreview();
  updateControls();
  feedback("image-status", "Generating image…");
  try {
    const response = await postJson("/api/images", { prompt }, {
      signal: controller.signal,
      headers: { Accept: "text/event-stream", "X-ATC-Cancel-On-Disconnect": "1" },
    });
    const blob = await readImage(response);
    controller.signal.throwIfAborted();
    if (!Object.hasOwn(imageExtensions, blob.type)) {
      throw new Error(errors.invalid_upstream_response);
    }
    state.previewUrl = URL.createObjectURL(blob);
    elements["generated-image"].src = state.previewUrl;
    await elements["generated-image"].decode().catch(() => {
      throw new Error("The generated image could not be displayed.");
    });
    controller.signal.throwIfAborted();
    elements["open-image"].href = state.previewUrl;
    elements["download-image"].href = state.previewUrl;
    elements["download-image"].download = `generated-image.${imageExtensions[blob.type]}`;
    elements["image-caption"].textContent = prompt;
    elements.preview.hidden = false;
    feedback("image-status", "Image ready.");
    loadGallery();
  } catch (error) {
    clearPreview();
    feedback("image-status", controller.signal.aborted ? "Image generation stopped." : error.message,
      controller.signal.aborted ? "" : "error");
  } finally {
    clearInterval(elapsedTimer);
    state.imageRequest = null;
    updateControls();
  }
}

function updateThemeLabel() {
  const current = document.documentElement.dataset.theme ?? (systemTheme.matches ? "light" : "dark");
  elements.theme.textContent = current === "light" ? "Dark theme" : "Light theme";
}

const modelManagement = {
  reviewing: null,
  browseVersion: 0,
  variantVersion: 0,
  reviewVersion: 0,
  pollTimer: null,
  polling: false,
  load: null,
  loadTicket: null,
  atomicLoadBusy: false,
  startingLoad: false,
  lastDownloads: null,
  refreshChat: false,
};

const modelControlErrors = {
  model_control_unavailable: "The Atomic CLI is unavailable.",
  model_control_stopped: "Atomic Server was not found in the configured data folder. Check the folder and server status.",
  model_control_instance: "Atomic Server is running, but its public API does not match this WebUI. Check ATC_API_URL and the API listener.",
  model_control_failed: "Atomic could not complete this operation. Check the local Atomic status and logs.",
  model_control_response: "Atomic returned an unexpected model-control response.",
  model_control_busy: "Model controls are busy. Try again shortly.",
  model_control_stale: "This selection has expired or is unavailable. Refresh the list and try again.",
  model_control_provider: "That engine is unavailable on this host.",
  model_control_timeout: "Atomic did not respond in time. Check its status before trying again.",
};
Object.assign(errors, modelControlErrors);

function refreshChatAfterLoad() {
  if (!modelManagement.refreshChat || state.chatRequest || state.loadingModels) return;
  modelManagement.refreshChat = false;
  refreshModels();
}

async function controlRequest(route, payload) {
  const path = "/api/model-control/" + route;
  const response = payload === undefined ? await request(path) : await postJson(path, payload);
  return response.json();
}

function fillModelSelect(select, rows, empty) {
  const selected = select.value;
  select.replaceChildren();
  for (const row of rows.length ? rows : [{ id: "", label: empty }]) {
    const option = document.createElement("option");
    option.value = row.id;
    option.textContent = row.label;
    select.append(option);
  }
  if (rows.some(({ id }) => id === selected)) select.value = selected;
  select.disabled = !rows.length;
}

function modelBytes(bytes) {
  if (!Number.isSafeInteger(bytes)) return "Size unavailable";
  const gigabytes = bytes / (1024 ** 3);
  return gigabytes >= 1 ? gigabytes.toFixed(2) + " GiB" : (bytes / (1024 ** 2)).toFixed(1) + " MiB";
}

function clearDownloadReview() {
  modelManagement.reviewing = null;
  modelManagement.reviewVersion++;
  elements["start-download"].hidden = true;
  elements["start-download"].disabled = true;
  feedback("download-review", "");
}

async function refreshVariants() {
  clearDownloadReview();
  const version = ++modelManagement.variantVersion;
  const id = elements["download-model"].value;
  fillModelSelect(elements["download-variant"], [], id ? "Checking variants…" : "Choose a model");
  elements["review-download"].disabled = true;
  if (!id) return;
  try {
    const { variants } = await controlRequest("variants", { id });
    if (version !== modelManagement.variantVersion) return;
    fillModelSelect(elements["download-variant"], variants, "No supported variants");
    elements["review-download"].disabled = !variants.length;
  } catch (error) {
    if (version !== modelManagement.variantVersion) return;
    fillModelSelect(elements["download-variant"], [], "Variants unavailable");
    feedback("download-review", error.message, "error");
  }
}

async function browseModelCatalog(search = false) {
  const version = ++modelManagement.browseVersion;
  const category = elements["model-category"].value;
  const provider = category === "text" ? elements["model-provider"].value : "";
  elements["model-provider"].hidden = category !== "text";
  elements["model-provider-label"].hidden = category !== "text";
  fillModelSelect(elements["download-model"], [], "Checking catalog…");
  refreshVariants();
  try {
    const result = await controlRequest(search ? "search" : "catalog", {
      category, provider, query: search ? elements["model-query"].value : "",
    });
    if (version !== modelManagement.browseVersion) return;
    fillModelSelect(elements["model-provider"], result.providers.map((id) => ({
      id, label: id === "gguf" ? "GGUF (automatic engine)" : id,
    })),
      "No compatible engine");
    elements["model-provider"].value = result.provider ?? "";
    fillModelSelect(elements["download-model"], result.models, "No models found");
    await refreshVariants();
  } catch (error) {
    if (version !== modelManagement.browseVersion) return;
    fillModelSelect(elements["download-model"], [], "Catalog unavailable");
    feedback("download-review", error.message, "error");
  }
}

async function reviewModelDownload() {
  clearDownloadReview();
  const version = modelManagement.reviewVersion;
  const id = elements["download-variant"].value;
  if (!id) return;
  const variant = elements["download-variant"].selectedOptions[0].textContent;
  elements["review-download"].disabled = true;
  feedback("download-review", "Reviewing download…");
  try {
    const review = await controlRequest("review", { id });
    if (version !== modelManagement.reviewVersion) return;
    modelManagement.reviewing = review.id;
    const details = [variant, modelBytes(review.bytes), review.provider, review.verdict, ...review.blockers];
    feedback("download-review", details.filter(Boolean).join(" · "), review.blocked ? "error" : "");
    elements["start-download"].hidden = review.blocked || !review.id;
    elements["start-download"].disabled = elements["start-download"].hidden;
  } catch (error) {
    if (version === modelManagement.reviewVersion) feedback("download-review", error.message, "error");
  } finally {
    if (version === modelManagement.reviewVersion) elements["review-download"].disabled = false;
  }
}

async function startModelDownload() {
  const id = modelManagement.reviewing;
  if (!id || elements["start-download"].disabled) return;
  elements["start-download"].disabled = true;
  try {
    await controlRequest("download", { id });
    if (id === modelManagement.reviewing) {
      clearDownloadReview();
      feedback("download-review", "Download accepted by Atomic.");
    }
    pollModelJobs();
  } catch (error) {
    if (id !== modelManagement.reviewing) return;
    feedback("download-review", error.message, "error");
    elements["start-download"].disabled = false;
  }
}

function updateLoadButton() {
  elements["load-model"].disabled = !elements["installed-model"].value
    || modelManagement.startingLoad || modelManagement.atomicLoadBusy || modelManagement.load?.status === "loading";
}

async function refreshInstalledModels() {
  try {
    const { models } = await controlRequest("installed");
    fillModelSelect(elements["installed-model"], models.map((model) => ({
      id: model.id, label: model.label + " · " + model.provider,
    })), "No installed models");
  } catch (error) {
    feedback("load-model-status", error.message, "error");
  } finally {
    updateLoadButton();
  }
}

async function startModelLoad() {
  if (elements["load-model"].disabled) return;
  modelManagement.startingLoad = true;
  updateLoadButton();
  feedback("load-model-status", "Starting model load…");
  try {
    const { load } = await controlRequest("load", { id: elements["installed-model"].value });
    modelManagement.loadTicket = load.id;
    if (modelManagement.load?.id !== load.id || modelManagement.load.status === "loading") {
      modelManagement.load = load;
    }
    pollModelJobs();
  } catch (error) {
    feedback("load-model-status", error.message, "error");
  } finally {
    modelManagement.startingLoad = false;
    updateLoadButton();
  }
}

async function pollModelJobs() {
  const shouldPoll = () => !elements["models-panel"].hidden || Boolean(modelManagement.loadTicket);
  if (modelManagement.polling || !shouldPoll()) return;
  clearTimeout(modelManagement.pollTimer);
  modelManagement.polling = true;
  try {
    const { downloads, loads } = await controlRequest("status");
    modelManagement.atomicLoadBusy = loads.some((entry) => entry.status === "loading");
    const load = modelManagement.loadTicket
      ? loads.find((entry) => entry.id === modelManagement.loadTicket) : loads[0];
    const rows = downloads.map((job) => {
      const row = document.createElement("li");
      row.textContent = job.label + " · " + job.status;
      if (job.bytes !== null && job.total !== null) {
        row.textContent += " · " + modelBytes(job.bytes) + " / " + modelBytes(job.total);
      }
      return row;
    });
    elements["model-downloads"].replaceChildren(...rows);
    feedback("model-jobs-status", rows.length ? "" : "No model downloads.");
    const completed = JSON.stringify(downloads.filter((job) => !job.active));
    if (modelManagement.lastDownloads !== null && completed !== modelManagement.lastDownloads) {
      refreshInstalledModels();
    }
    modelManagement.lastDownloads = completed;

    if (load) {
      const previous = modelManagement.load;
      modelManagement.load = load;
      if (load.status === "loading") feedback("load-model-status", "Loading " + load.label + "…");
      if (load.status === "failed") {
        modelManagement.loadTicket = null;
        feedback("load-model-status", describeError(load.error, "Model load failed."), "error");
      }
      if (load.status === "ready" && (previous?.id !== load.id || previous.status !== "ready")) {
        modelManagement.loadTicket = null;
        feedback("load-model-status", load.label + " loaded.", "available");
        if (load.category === "text") {
          modelManagement.refreshChat = true;
          refreshChatAfterLoad();
        } else {
          refreshImageModel();
        }
      }
    }
  } catch (error) {
    feedback("model-jobs-status", error.message, "error");
  } finally {
    modelManagement.polling = false;
    updateLoadButton();
    if (shouldPoll()) modelManagement.pollTimer = setTimeout(pollModelJobs, 3000);
  }
}

const storedTheme = readStorage(THEME_KEY);
if (["light", "dark"].includes(storedTheme)) document.documentElement.dataset.theme = storedTheme;
updateThemeLabel();
systemTheme.addEventListener("change", updateThemeLabel);
elements.theme.addEventListener("click", () => {
  const current = document.documentElement.dataset.theme ?? (systemTheme.matches ? "light" : "dark");
  const next = current === "light" ? "dark" : "light";
  document.documentElement.dataset.theme = next;
  writeStorage(THEME_KEY, next);
  updateThemeLabel();
});
elements["chat-view"].addEventListener("click", () => showView("chat"));
elements["image-view"].addEventListener("click", () => showView("images"));
elements["models-view"].addEventListener("click", () => {
  if (!elements["models-panel"].hidden) return;
  showView("models");
  browseModelCatalog();
  refreshInstalledModels().then(pollModelJobs);
});
elements["model-search"].addEventListener("submit", (event) => {
  event.preventDefault();
  browseModelCatalog(true);
});
elements["model-catalog"].addEventListener("click", () => browseModelCatalog());
elements["model-category"].addEventListener("change", () => browseModelCatalog());
elements["model-provider"].addEventListener("change", () => browseModelCatalog());
elements["download-model"].addEventListener("change", refreshVariants);
elements["download-variant"].addEventListener("change", () => {
  clearDownloadReview();
  elements["review-download"].disabled = !elements["download-variant"].value;
});
elements["review-download"].addEventListener("click", reviewModelDownload);
elements["start-download"].addEventListener("click", startModelDownload);
elements["installed-model"].addEventListener("change", updateLoadButton);
elements["load-model"].addEventListener("click", startModelLoad);
elements["refresh-installed"].addEventListener("click", refreshInstalledModels);
elements["new-chat"].addEventListener("click", () => {
  state.activeId = null;
  elements["chat-prompt"].value = "";
  feedback("chat-status", "");
  saveActiveConversation();
  showView("chat");
  renderConversation();
  renderHistory();
  elements["chat-prompt"].focus();
});
elements.refresh.addEventListener("click", refreshModels);
elements.model.addEventListener("change", updateControls);
elements["chat-form"].addEventListener("submit", submitChat);
elements.stop.addEventListener("click", () => {
  state.chatRequest?.abort();
  updateControls();
});
elements["image-form"].addEventListener("submit", submitImage);
elements["gallery-refresh"].addEventListener("click", () => loadGallery());
elements["gallery-more"].addEventListener("click", () => loadGallery(true));
elements["stop-image"].addEventListener("click", () => {
  state.imageRequest?.abort();
  updateControls();
});

loadHistory();
renderConversation();
renderHistory();
refreshModels();
refreshImageModel();
setInterval(refreshImageModel, IMAGE_MODEL_REFRESH_MS);
document.addEventListener("visibilitychange", refreshImageModel);
loadImageConfig();
