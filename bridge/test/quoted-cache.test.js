import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { LruCache, trimForQuote } from "../src/quoted-cache.js";

describe("LruCache", () => {
  it("stores and returns values", () => {
    const cache = new LruCache(10);
    cache.set("a", 1);
    assert.equal(cache.get("a"), 1);
    assert.equal(cache.get("missing"), undefined);
  });

  it("evicts the oldest entry past its size", () => {
    const cache = new LruCache(2);
    cache.set("a", 1);
    cache.set("b", 2);
    cache.set("c", 3);

    assert.equal(cache.size, 2);
    assert.equal(cache.has("a"), false);
    assert.equal(cache.get("b"), 2);
    assert.equal(cache.get("c"), 3);
  });

  it("refreshes recency on read", () => {
    const cache = new LruCache(2);
    cache.set("a", 1);
    cache.set("b", 2);
    cache.get("a"); // "a" becomes the most recent
    cache.set("c", 3);

    assert.equal(cache.has("a"), true);
    assert.equal(cache.has("b"), false);
  });

  it("never grows past one entry for a size of zero", () => {
    const cache = new LruCache(0);
    cache.set("a", 1);
    cache.set("b", 2);
    assert.equal(cache.size, 1);
  });
});

describe("trimForQuote", () => {
  it("keeps only the key and a short preview", () => {
    const trimmed = trimForQuote({ key: { id: "X" }, text: "y".repeat(500) });
    assert.deepEqual(trimmed.key, { id: "X" });
    assert.equal(trimmed.message.conversation.length, 200);
  });

  it("tolerates a message with no text", () => {
    assert.deepEqual(trimForQuote({ key: { id: "X" } }), {
      key: { id: "X" },
      message: { conversation: "" },
    });
  });
});
