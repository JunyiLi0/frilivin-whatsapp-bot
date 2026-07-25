/**
 * Calls to the Python API.
 *
 * Webhook deliveries that fail every attempt are buffered in memory and flushed
 * periodically: the api restarting (a redeploy, an OOM kill) should delay
 * messages, not lose them.
 */

const PENDING_LIMIT = 500;
const FLUSH_INTERVAL_MS = 10_000;

export class ApiClient {
  #baseUrl;
  #token;
  #log;
  #attempts;
  #timeoutMs;
  #pending = [];
  #timer = null;
  #sleep;

  constructor({ baseUrl, token, logger, attempts = 3, timeoutMs = 5000, sleepFn }) {
    this.#baseUrl = baseUrl;
    this.#token = token;
    this.#log = logger;
    this.#attempts = attempts;
    this.#timeoutMs = timeoutMs;
    this.#sleep = sleepFn ?? ((ms) => new Promise((resolve) => setTimeout(resolve, ms)));
  }

  get pendingCount() {
    return this.#pending.length;
  }

  async #post(path, body) {
    const response = await fetch(`${this.#baseUrl}${path}`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Bot-Token": this.#token,
      },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(this.#timeoutMs),
    });
    if (!response.ok) {
      const detail = await response.text().catch(() => "");
      throw new Error(`HTTP ${response.status}: ${detail.slice(0, 200)}`);
    }
    return response;
  }

  async #postWithRetries(path, body) {
    let lastError;
    for (let attempt = 1; attempt <= this.#attempts; attempt += 1) {
      try {
        return await this.#post(path, body);
      } catch (err) {
        lastError = err;
        if (attempt < this.#attempts) {
          await this.#sleep(250 * 2 ** (attempt - 1));
        }
      }
    }
    throw lastError;
  }

  /** Deliver an inbound message. Never throws: it buffers instead. */
  async sendWebhook(payload) {
    try {
      await this.#postWithRetries("/webhook", payload);
      this.#log?.info({
        event: "webhook_delivered",
        message_id: payload.id,
        type: payload.type,
        is_group: payload.is_group,
      });
      return true;
    } catch (err) {
      this.#buffer(payload);
      this.#log?.error({
        event: "webhook_failed",
        message_id: payload.id,
        pending: this.#pending.length,
        error: String(err?.message ?? err),
      });
      return false;
    }
  }

  async sendStatus(update) {
    try {
      await this.#postWithRetries("/internal/send-status", update);
    } catch (err) {
      // Purely informational for the ledger: not worth buffering.
      this.#log?.warn({
        event: "send_status_failed",
        outbound_id: update.id,
        error: String(err?.message ?? err),
      });
    }
  }

  #buffer(payload) {
    if (this.#pending.length >= PENDING_LIMIT) {
      const dropped = this.#pending.shift();
      this.#log?.error({ event: "webhook_buffer_overflow", dropped_id: dropped?.id });
    }
    this.#pending.push(payload);
    this.startFlushing();
  }

  startFlushing(intervalMs = FLUSH_INTERVAL_MS) {
    if (this.#timer) return;
    this.#timer = setInterval(() => {
      this.flush().catch(() => {});
    }, intervalMs);
    // Do not keep the event loop alive just for the retry timer.
    this.#timer.unref?.();
  }

  async flush() {
    while (this.#pending.length > 0) {
      const payload = this.#pending[0];
      try {
        await this.#post("/webhook", payload);
        this.#pending.shift();
        this.#log?.info({ event: "webhook_flushed", message_id: payload.id });
      } catch {
        return; // api still down, try again on the next tick
      }
    }
    if (this.#timer) {
      clearInterval(this.#timer);
      this.#timer = null;
    }
  }

  stop() {
    if (this.#timer) {
      clearInterval(this.#timer);
      this.#timer = null;
    }
  }
}
