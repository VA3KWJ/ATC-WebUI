"use strict";

const elements = Object.fromEntries(
  Array.from(document.querySelectorAll("[id]"), (element) => [element.id, element]),
);
const HISTORY_KEY = "atomic-webui.history.v1";
const THEME_KEY = "atomic-webui.theme";
const MAX_CHAT_REQUEST = 256 * 1024;
const encoder = new TextEncoder();
const systemTheme = window.matchMedia("(prefers-color-scheme: light)");
const state = {
  conversations: [],
  activeId: null,
  models: [],
  loadingModels: false,
  chatRequest: null,
  imageBusy: false,
  imageEnabled: false,
  previewUrl: null,
};

const errors = {
  upstream_authentication_failed: "The Atomic API rejected its credentials. Contact the operator.",
  upstream_route_unsupported: "This operation is unsupported by the Atomic API.",
  model_unavailable: "The selected model is unavailable. Refresh models and choose another.",
  image_model_unavailable: "Image generation has no configured model.",
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
  if (!saved) return;
  try {
    const history = JSON.parse(saved);
    if (!Array.isArray(history.conversations)) throw new Error();
    state.conversations = history.conversations.filter((conversation) =>
      conversation && typeof conversation.id === "string" &&
      Array.isArray(conversation.messages) && conversation.messages.length &&
      conversation.messages.every((message) =>
        message && ["user", "assistant"].includes(message.role) &&
        typeof message.content === "string",
      ),
    );
    state.activeId = state.conversations.some(({ id }) => id === history.activeId)
      ? history.activeId : null;
  } catch {
    feedback("storage-status", "Saved conversations could not be loaded.");
  }
}

function saveHistory() {
  const conversations = state.conversations.map(({ id, messages }) => ({
    id,
    messages: messages.map(({ role, content }) => ({ role, content })),
  }));
  if (writeStorage(HISTORY_KEY, JSON.stringify({ conversations, activeId: state.activeId }))) {
    feedback("storage-status", "");
  }
}

function currentConversation() {
  return state.conversations.find(({ id }) => id === state.activeId);
}

function showView(view) {
  elements.chat.hidden = view !== "chat";
  elements.images.hidden = view !== "images";
  elements["chat-view"].setAttribute("aria-pressed", String(view === "chat"));
  elements["image-view"].setAttribute("aria-pressed", String(view === "images"));
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
      saveHistory();
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
  elements.generate.disabled = !state.imageEnabled || state.imageBusy;
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
  feedback("model-status", "Models · Checking…");
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
    feedback("model-status", count ? `Models · ${count} available` : "Models · None available",
      count ? "available" : "");
  } catch (error) {
    state.models = [];
    feedback("api-status", `API · ${error.message}`, "error");
    feedback("model-status", "Models · Availability unknown");
  } finally {
    state.loadingModels = false;
    renderModels(previous);
    updateControls();
  }
}

async function loadImageConfig() {
  try {
    const response = await request("/api/config");
    const config = await response.json();
    state.imageEnabled = config.image_enabled === true;
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
    feedback("image-status", state.imageEnabled ? "" : "Image generation has no configured model.");
  } catch (error) {
    feedback("image-status", error.message, "error");
  } finally {
    updateControls();
  }
}

function postJson(path, payload, signal) {
  return request(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
    signal,
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

async function readChat(response, onDelta) {
  const interrupted = new Error(errors.stream_interrupted);
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
        if (event === "delta" || event === "error") {
          let payload;
          try {
            payload = JSON.parse(data);
          } catch {
            throw interrupted;
          }
          if (event === "error") throw new Error(describeError(payload?.error, interrupted.message));
          if (typeof payload?.text !== "string") throw interrupted;
          onDelta(payload.text);
        }
        boundary = /\r?\n\r?\n/.exec(pending);
      }
    }
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
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
  if (!conversation) {
    // getRandomValues also works when the UI is served over plain HTTP.
    const id = Array.from(crypto.getRandomValues(new Uint32Array(4)), (part) => part.toString(16).padStart(8, "0")).join("");
    conversation = { id, messages: [] };
    state.conversations.unshift(conversation);
    state.activeId = conversation.id;
  }
  conversation.messages.push(message);
  const controller = new AbortController();
  state.chatRequest = controller;
  elements["chat-prompt"].value = "";
  feedback("chat-status", "Waiting for a reply…");
  renderConversation();
  renderHistory();
  updateControls();
  saveHistory();

  let reply = "";
  let paragraph;
  let painted = 0;
  let animationFrame = null;
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
    const response = await postJson("/api/chat", payload, controller.signal);
    await readChat(response, (delta) => {
      if (!reply && delta) feedback("chat-status", "Receiving reply…");
      reply += delta;
      animationFrame ??= requestAnimationFrame(paintReply);
    });
    feedback("chat-status", reply ? "Reply complete." : "The model returned an empty reply.");
  } catch (error) {
    if (error.status === 413) {
      conversation.messages.pop();
      if (!conversation.messages.length) {
        state.conversations = state.conversations.filter(({ id }) => id !== conversation.id);
        state.activeId = null;
      }
      elements["chat-prompt"].value = prompt;
      renderConversation();
    }
    feedback("chat-status", controller.signal.aborted ? "Reply stopped." : error.message,
      controller.signal.aborted ? "" : "error");
  } finally {
    if (animationFrame !== null) cancelAnimationFrame(animationFrame);
    paintReply();
    if (reply) conversation.messages.push({ role: "assistant", content: reply });
    state.chatRequest = null;
    elements["chat-prompt"].readOnly = false;
    saveHistory();
    renderHistory();
    updateControls();
  }
}

function clearPreview() {
  elements.preview.hidden = true;
  elements["generated-image"].removeAttribute("src");
  elements["image-caption"].textContent = "";
  if (state.previewUrl) URL.revokeObjectURL(state.previewUrl);
  state.previewUrl = null;
}

async function submitImage(event) {
  event.preventDefault();
  const prompt = elements["image-prompt"].value.trim();
  if (!prompt || !state.imageEnabled || state.imageBusy) return;
  state.imageBusy = true;
  clearPreview();
  updateControls();
  feedback("image-status", "Generating image…");
  try {
    const response = await postJson("/api/images", { prompt });
    const blob = await response.blob();
    if (!["image/png", "image/jpeg", "image/webp"].includes(blob.type)) {
      throw new Error(errors.invalid_upstream_response);
    }
    state.previewUrl = URL.createObjectURL(blob);
    elements["generated-image"].src = state.previewUrl;
    await elements["generated-image"].decode().catch(() => {
      throw new Error("The generated image could not be displayed.");
    });
    elements["image-caption"].textContent = prompt;
    elements.preview.hidden = false;
    feedback("image-status", "Image ready.");
  } catch (error) {
    clearPreview();
    feedback("image-status", error.message, "error");
  } finally {
    state.imageBusy = false;
    updateControls();
  }
}

function updateThemeLabel() {
  const current = document.documentElement.dataset.theme ?? (systemTheme.matches ? "light" : "dark");
  elements.theme.textContent = current === "light" ? "Dark theme" : "Light theme";
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
elements["new-chat"].addEventListener("click", () => {
  state.activeId = null;
  elements["chat-prompt"].value = "";
  feedback("chat-status", "");
  saveHistory();
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

loadHistory();
renderConversation();
renderHistory();
refreshModels();
loadImageConfig();
