/**
 * Persisted outbound queue.
 *
 * `POST /send` returns 202 as soon as a message lands here, and the human-like
 * 2-8 s delay is applied by this loop afterwards — that is what keeps the
 * worker's 2 s budget intact.
 *
 * The queue is mirrored to disk (in the session volume) after every state
 * change: a purely in-memory queue would silently drop pending messages on
 * `docker compose restart`.
 */

import { randomUUID } from "node:crypto";
import { mkdir, readFile, rename, writeFile } from "node:fs/promises";
import { dirname } from "node:path";

export function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export function randomDelay(minMs, maxMs, random = Math.random) {
  return Math.round(minMs + random() * Math.max(0, maxMs - minMs));
}

export function backoffMs(attempt) {
  return Math.min(30_000, 1000 * 2 ** attempt);
}

export class Outbox {
  #path;
  #log;
  #minDelayMs;
  #maxDelayMs;
  #maxAttempts;
  #send;
  #onSettled;
  #sleep;
  #queue = [];
  #draining = false;
  #stopped = false;

  constructor({
    path,
    logger,
    minDelayMs = 2000,
    maxDelayMs = 8000,
    maxAttempts = 5,
    send,
    onSettled = () => {},
    sleepFn = sleep,
  }) {
    this.#path = path;
    this.#log = logger;
    this.#minDelayMs = minDelayMs;
    this.#maxDelayMs = maxDelayMs;
    this.#maxAttempts = maxAttempts;
    this.#send = send;
    this.#onSettled = onSettled;
    this.#sleep = sleepFn;
  }

  get size() {
    return this.#queue.length;
  }

  get pending() {
    return [...this.#queue];
  }

  /** Restore anything that was still queued when the process last stopped. */
  async load() {
    try {
      const raw = await readFile(this.#path, "utf8");
      const parsed = JSON.parse(raw);
      this.#queue = Array.isArray(parsed) ? parsed : [];
      if (this.#queue.length > 0) {
        this.#log?.info({ event: "outbox_restored", count: this.#queue.length });
        this.#kick();
      }
    } catch (err) {
      if (err.code !== "ENOENT") {
        // A corrupt file must not stop the bridge from booting.
        this.#log?.error({ event: "outbox_load_failed", error: String(err?.message ?? err) });
      }
      this.#queue = [];
    }
  }

  async enqueue({ id, jid, text, quotedId = null }) {
    const item = {
      id: id || `out-${randomUUID().replace(/-/g, "")}`,
      jid,
      text,
      quotedId,
      attempts: 0,
      queuedAt: new Date().toISOString(),
    };
    this.#queue.push(item);
    await this.#persist();
    this.#kick();
    return item.id;
  }

  stop() {
    this.#stopped = true;
  }

  #kick() {
    if (this.#draining || this.#stopped) return;
    this.#drain().catch((err) => {
      this.#log?.error({ event: "outbox_drain_crashed", error: String(err?.message ?? err) });
      this.#draining = false;
    });
  }

  async #drain() {
    this.#draining = true;
    try {
      while (this.#queue.length > 0 && !this.#stopped) {
        const item = this.#queue[0];
        await this.#sleep(randomDelay(this.#minDelayMs, this.#maxDelayMs));
        if (this.#stopped) break;

        try {
          const waMessageId = await this.#send(item);
          this.#queue.shift();
          await this.#persist();
          this.#settle(item, { status: "sent", waMessageId });
        } catch (err) {
          const error = String(err?.message ?? err);
          item.attempts += 1;
          if (item.attempts >= this.#maxAttempts) {
            this.#queue.shift();
            await this.#persist();
            this.#log?.error({
              event: "send_gave_up",
              outbound_id: item.id,
              jid: item.jid,
              attempts: item.attempts,
              error,
            });
            this.#settle(item, { status: "failed", error });
          } else {
            await this.#persist();
            this.#log?.warn({
              event: "send_retry",
              outbound_id: item.id,
              jid: item.jid,
              attempt: item.attempts,
              error,
            });
            await this.#sleep(backoffMs(item.attempts));
          }
        }
      }
    } finally {
      this.#draining = false;
    }
  }

  #settle(item, result) {
    try {
      const outcome = this.#onSettled(item, result);
      if (outcome && typeof outcome.catch === "function") {
        outcome.catch((err) =>
          this.#log?.error({ event: "settle_failed", error: String(err?.message ?? err) }),
        );
      }
    } catch (err) {
      this.#log?.error({ event: "settle_failed", error: String(err?.message ?? err) });
    }
  }

  async #persist() {
    try {
      await mkdir(dirname(this.#path), { recursive: true });
      const temporary = `${this.#path}.tmp`;
      await writeFile(temporary, JSON.stringify(this.#queue), "utf8");
      // Rename is atomic on the same filesystem: no half-written queue file.
      await rename(temporary, this.#path);
    } catch (err) {
      this.#log?.error({ event: "outbox_persist_failed", error: String(err?.message ?? err) });
    }
  }
}
