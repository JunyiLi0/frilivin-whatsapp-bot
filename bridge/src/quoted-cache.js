/**
 * Bounded id → WhatsApp message cache.
 *
 * Baileys needs the quoted *message*, not its id, to build a reply. The worker
 * only ever knows ids, so the bridge remembers the last N inbound messages —
 * trimmed down to what quoting actually requires.
 */

export class LruCache {
  #max;
  #entries = new Map();

  constructor(maxSize = 2000) {
    this.#max = Math.max(1, maxSize);
  }

  get size() {
    return this.#entries.size;
  }

  set(key, value) {
    if (this.#entries.has(key)) this.#entries.delete(key);
    this.#entries.set(key, value);
    while (this.#entries.size > this.#max) {
      // Map preserves insertion order: the first key is the oldest.
      this.#entries.delete(this.#entries.keys().next().value);
    }
  }

  get(key) {
    if (!this.#entries.has(key)) return undefined;
    const value = this.#entries.get(key);
    // Refresh recency.
    this.#entries.delete(key);
    this.#entries.set(key, value);
    return value;
  }

  has(key) {
    return this.#entries.has(key);
  }
}

/** Keep only the fields Baileys reads when quoting, so memory stays flat. */
export function trimForQuote(message) {
  return {
    key: message.key,
    message: { conversation: (message.text ?? "").slice(0, 200) },
  };
}
