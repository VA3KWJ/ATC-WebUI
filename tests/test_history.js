"use strict";

const assert = require("node:assert/strict");
const { webcrypto } = require("node:crypto");
const { readFileSync } = require("node:fs");
const { resolve } = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = readFileSync(resolve(__dirname, "../static/app.js"), "utf8");
const html = readFileSync(resolve(__dirname, "../static/index.html"), "utf8");
const historyKey = "atomic-webui.history.v1";

function element(id = "") {
  return {
    id, value: "", dataset: {},
    addEventListener() {}, append() {}, replaceChildren() {}, setAttribute() {},
  };
}

function sharedStorage(saved, canWrite = () => true) {
  const storage = Object.create({
    getItem(key) { return Object.hasOwn(this, key) ? this[key] : null; },
    setItem(key, value) {
      if (!canWrite()) throw new Error("Storage unavailable");
      this[key] = value;
    },
    removeItem(key) { delete this[key]; },
  });
  if (saved) storage.setItem(historyKey, JSON.stringify(saved));
  return storage;
}

function tab(localStorage, reply) {
  const nodes = Array.from(html.matchAll(/\bid="([^"]+)"/g), ([, id]) => element(id));
  const context = vm.createContext({
    localStorage, TextEncoder, TextDecoder, AbortController, crypto: webcrypto,
    document: {
      querySelectorAll: () => nodes, createElement: () => element(), createTextNode: (text) => text,
    },
    window: { matchMedia: () => ({}) },
    fetch: async (path, options) => {
      assert.equal(path, "/api/chat");
      assert.equal(options.headers["X-ATC-Cancel-On-Disconnect"], "1");
      return reply();
    },
    requestAnimationFrame: () => 1,
    cancelAnimationFrame() {},
  });
  const run = (code) => vm.runInContext(code, context);
  run(source.slice(0, source.indexOf("const storedTheme =")));
  run('elements.model.value = "mock-model"; loadHistory();');
  return {
    send(prompt) {
      context.prompt = prompt;
      return run('elements["chat-prompt"].value = prompt; submitChat({ preventDefault() {} })');
    },
    state() {
      return JSON.parse(run("JSON.stringify({ conversations: state.conversations, activeId: state.activeId })"));
    },
  };
}

function reply(text) {
  const delta = JSON.stringify({ text });
  return new Response(`event: delta\ndata: ${delta}\n\nevent: done\ndata: {}\n\n`, {
    headers: { "Content-Type": "text/event-stream" },
  });
}

test("tabs preserve independent conversations even when replies finish out of order", async () => {
  const storage = sharedStorage();
  let finishFirst;
  const first = tab(storage, () => new Promise((resolve) => { finishFirst = resolve; }));
  const second = tab(storage, () => reply("second answer"));
  const firstRequest = first.send("first question");
  await second.send("second question");
  finishFirst(reply("first answer"));
  await firstRequest;

  const { conversations, activeId } = tab(storage).state();
  const contents = conversations.map(({ messages }) => messages.map(({ content }) => content));
  assert.deepEqual(contents.sort(), [
    ["first question", "first answer"], ["second question", "second answer"],
  ]);
  assert.equal(activeId, second.state().activeId);
});

test("two tabs append to a legacy conversation while preserving its saved transcript", async () => {
  const original = { id: "legacy", messages: [{ role: "user", content: "saved question" }] };
  const storage = sharedStorage({ conversations: [original], activeId: original.id });
  const first = tab(storage, () => reply("first answer"));
  const second = tab(storage, () => reply("second answer"));
  await first.send("first question");
  await second.send("second question");

  const { conversations, activeId } = tab(storage).state();
  assert.equal(activeId, original.id);
  assert.equal(conversations.length, 1);
  assert.equal(conversations[0].id, original.id);
  const [saved, ...added] = conversations[0].messages.map(({ content }) => content);
  assert.equal(saved, "saved question");
  assert.deepEqual([added.slice(0, 2), added.slice(2)].sort(), [
    ["first question", "first answer"], ["second question", "second answer"],
  ]);
  assert.deepEqual(JSON.parse(storage.getItem(historyKey)).conversations, [original]);
});

test("a rejected oversized request leaves no saved turn", async () => {
  const storage = sharedStorage();
  const first = tab(storage, () => Response.json({ error: "request_too_large" }, { status: 413 }));
  await first.send("rejected message");
  assert.deepEqual(tab(storage).state(), { conversations: [], activeId: null });
});

test("a later successful save retries turns kept in memory during a storage failure", async () => {
  let canWrite = false;
  const storage = sharedStorage(null, () => canWrite);
  const first = tab(storage, () => reply("answer"));
  await first.send("first question");
  assert.deepEqual(tab(storage).state().conversations, []);
  canWrite = true;
  await first.send("second question");
  const { conversations, activeId } = tab(storage).state();
  assert.equal(activeId, conversations[0].id);
  assert.deepEqual(conversations[0].messages.map(({ content }) => content), [
    "first question", "answer", "second question", "answer",
  ]);
});
