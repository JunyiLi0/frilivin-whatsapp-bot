import assert from "node:assert/strict";
import { mkdtemp, readFile, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, it } from "node:test";

import { Outbox, backoffMs, randomDelay } from "../src/outbox.js";

const noSleep = () => Promise.resolve();

async function tempPath() {
  const dir = await mkdtemp(join(tmpdir(), "outbox-"));
  return join(dir, "outbox.json");
}

/** Waits until `predicate` holds, so tests never race the drain loop. */
async function until(predicate, timeoutMs = 2000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (predicate()) return;
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
  throw new Error("condition not met before timeout");
}

describe("randomDelay", () => {
  it("stays within bounds", () => {
    assert.equal(randomDelay(2000, 8000, () => 0), 2000);
    assert.equal(randomDelay(2000, 8000, () => 1), 8000);
    assert.equal(randomDelay(2000, 8000, () => 0.5), 5000);
  });
});

describe("backoffMs", () => {
  it("grows exponentially and is capped", () => {
    assert.equal(backoffMs(1), 2000);
    assert.equal(backoffMs(2), 4000);
    assert.equal(backoffMs(10), 30_000);
  });
});

describe("Outbox", () => {
  it("sends queued messages and reports them as sent", async () => {
    const sent = [];
    const settled = [];
    const outbox = new Outbox({
      path: await tempPath(),
      minDelayMs: 0,
      maxDelayMs: 0,
      send: (item) => {
        sent.push(item);
        return Promise.resolve(`wa-${item.id}`);
      },
      onSettled: (item, result) => settled.push([item.id, result.status, result.waMessageId]),
      sleepFn: noSleep,
    });

    const id = await outbox.enqueue({ id: "out-1", jid: "x@g.us", text: "hello" });
    await until(() => settled.length === 1);

    assert.equal(id, "out-1");
    assert.equal(sent.length, 1);
    assert.deepEqual(settled[0], ["out-1", "sent", "wa-out-1"]);
    assert.equal(outbox.size, 0);
  });

  it("generates an id when none is supplied", async () => {
    const outbox = new Outbox({
      path: await tempPath(),
      minDelayMs: 0,
      maxDelayMs: 0,
      send: () => Promise.resolve("wa-x"),
      sleepFn: noSleep,
    });

    const id = await outbox.enqueue({ jid: "x@g.us", text: "hello" });
    assert.match(id, /^out-[0-9a-f]{32}$/);
  });

  it("keeps FIFO order", async () => {
    const order = [];
    const outbox = new Outbox({
      path: await tempPath(),
      minDelayMs: 0,
      maxDelayMs: 0,
      send: (item) => {
        order.push(item.id);
        return Promise.resolve("wa");
      },
      sleepFn: noSleep,
    });

    await outbox.enqueue({ id: "a", jid: "j", text: "1" });
    await outbox.enqueue({ id: "b", jid: "j", text: "2" });
    await outbox.enqueue({ id: "c", jid: "j", text: "3" });
    await until(() => outbox.size === 0);

    assert.deepEqual(order, ["a", "b", "c"]);
  });

  it("retries a failing send then gives up and reports it", async () => {
    const settled = [];
    let calls = 0;
    const outbox = new Outbox({
      path: await tempPath(),
      minDelayMs: 0,
      maxDelayMs: 0,
      maxAttempts: 3,
      send: () => {
        calls += 1;
        return Promise.reject(new Error("not connected"));
      },
      onSettled: (item, result) => settled.push([item.id, result.status, result.error]),
      sleepFn: noSleep,
    });

    await outbox.enqueue({ id: "out-2", jid: "x@g.us", text: "boom" });
    await until(() => settled.length === 1);

    assert.equal(calls, 3);
    assert.deepEqual(settled[0], ["out-2", "failed", "not connected"]);
    assert.equal(outbox.size, 0);
  });

  it("recovers after a transient failure", async () => {
    const settled = [];
    let calls = 0;
    const outbox = new Outbox({
      path: await tempPath(),
      minDelayMs: 0,
      maxDelayMs: 0,
      maxAttempts: 5,
      send: () => {
        calls += 1;
        return calls < 3 ? Promise.reject(new Error("flaky")) : Promise.resolve("wa-ok");
      },
      onSettled: (item, result) => settled.push(result.status),
      sleepFn: noSleep,
    });

    await outbox.enqueue({ id: "out-3", jid: "x@g.us", text: "hi" });
    await until(() => settled.length === 1);

    assert.deepEqual(settled, ["sent"]);
    assert.equal(calls, 3);
  });

  it("persists the queue to disk", async () => {
    const path = await tempPath();
    let release;
    const blocked = new Promise((resolve) => {
      release = resolve;
    });

    const outbox = new Outbox({
      path,
      minDelayMs: 0,
      maxDelayMs: 0,
      send: () => blocked,
      sleepFn: noSleep,
    });

    await outbox.enqueue({ id: "out-4", jid: "x@g.us", text: "persisted" });
    const written = JSON.parse(await readFile(path, "utf8"));

    assert.equal(written.length, 1);
    assert.equal(written[0].id, "out-4");
    assert.equal(written[0].text, "persisted");

    release("wa-done");
    await until(() => outbox.size === 0);
  });

  it("restores a queue left behind by a restart", async () => {
    const path = await tempPath();
    await writeFile(
      path,
      JSON.stringify([{ id: "out-5", jid: "x@g.us", text: "survivor", attempts: 0 }]),
      "utf8",
    );

    const sent = [];
    const outbox = new Outbox({
      path,
      minDelayMs: 0,
      maxDelayMs: 0,
      send: (item) => {
        sent.push(item.id);
        return Promise.resolve("wa");
      },
      sleepFn: noSleep,
    });

    await outbox.load();
    await until(() => sent.length === 1);
    assert.deepEqual(sent, ["out-5"]);
  });

  it("starts empty when the queue file is corrupt", async () => {
    const path = await tempPath();
    await writeFile(path, "{not json", "utf8");

    const outbox = new Outbox({
      path,
      send: () => Promise.resolve("wa"),
      sleepFn: noSleep,
      logger: { error: () => {}, info: () => {} },
    });

    await outbox.load();
    assert.equal(outbox.size, 0);
  });
});
