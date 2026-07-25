/**
 * The WhatsApp connection itself (Baileys, multi-device).
 *
 * Responsibilities are deliberately narrow: stay connected, hand inbound
 * messages to the API, send what the outbox asks for. No business logic —
 * that all lives in the Python handlers.
 */

import { randomUUID } from "node:crypto";
import { mkdir } from "node:fs/promises";

// Baileys is published as CommonJS; Node's interop exposes makeWASocket as the
// default export and everything else as named exports.
import makeWASocket, {
  Browsers,
  DisconnectReason,
  fetchLatestBaileysVersion,
  makeCacheableSignalKeyStore,
  useMultiFileAuthState,
} from "@whiskeysockets/baileys";
import qrcode from "qrcode-terminal";

import { toWebhookPayload } from "./extract.js";
import { LruCache, trimForQuote } from "./quoted-cache.js";

const MAX_RECONNECT_DELAY_MS = 60_000;
const WATCHDOG_INTERVAL_MS = 45_000;

/**
 * What to tell the operator when the bridge is not connected.
 *
 * Silence is the worst first-run experience: no QR and no error looks
 * identical to "everything is fine but nothing happened yet".
 *
 * @returns a hint, or null when there is nothing to report.
 */
export function connectionHint({ connected, loggedOut, sawQr }) {
  if (connected) return null;
  if (loggedOut) {
    return "Session révoquée. Supprimez le volume wa_session puis redémarrez pour re-scanner.";
  }
  if (sawQr) {
    return "QR affiché mais pas encore scanné : WhatsApp > Appareils liés > Lier un appareil.";
  }
  return (
    "Aucun QR reçu de WhatsApp. Vérifiez la connectivité sortante du serveur " +
    "(le bridge ouvre un websocket vers WhatsApp Web)."
  );
}

export class WhatsAppClient {
  #config;
  #log;
  #api;
  #sock = null;
  #saveCreds = null;
  #connected = false;
  #loggedOut = false;
  #reconnectAttempts = 0;
  #reconnectTimer = null;
  #stopped = false;
  #quoted;
  #me = null;
  #sawQr = false;
  #watchdog = null;

  constructor({ config, logger, api }) {
    this.#config = config;
    this.#log = logger;
    this.#api = api;
    this.#quoted = new LruCache(config.quotedCacheSize);
  }

  get connected() {
    return this.#connected;
  }

  status() {
    return {
      connected: this.#connected,
      logged_out: this.#loggedOut,
      user: this.#me,
      quoted_cache: this.#quoted.size,
      dry_run: this.#config.dryRun,
    };
  }

  async start() {
    await mkdir(this.#config.sessionDir, { recursive: true });
    await this.#connect();
    this.#startWatchdog();
  }

  #startWatchdog(intervalMs = WATCHDOG_INTERVAL_MS) {
    if (this.#watchdog) return;
    this.#watchdog = setInterval(() => {
      const hint = connectionHint({
        connected: this.#connected,
        loggedOut: this.#loggedOut,
        sawQr: this.#sawQr,
      });
      if (hint) this.#log.warn({ event: "wa_not_connected", hint });
    }, intervalMs);
    this.#watchdog.unref?.();
  }

  async #connect() {
    const { state, saveCreds } = await useMultiFileAuthState(this.#config.sessionDir);
    this.#saveCreds = saveCreds;

    let version;
    try {
      ({ version } = await fetchLatestBaileysVersion());
    } catch (err) {
      // Offline or blocked: fall back to the version bundled with the library.
      this.#log.warn({ event: "wa_version_fetch_failed", error: String(err?.message ?? err) });
    }

    const waLogger = this.#log.child({ component: "baileys" }, { level: "warn" });

    this.#sock = makeWASocket({
      ...(version ? { version } : {}),
      auth: {
        creds: state.creds,
        keys: makeCacheableSignalKeyStore(state.keys, waLogger),
      },
      logger: waLogger,
      browser: Browsers.ubuntu(this.#config.browserName),
      // Leave notifications on the phone alone and skip the history sync:
      // this bot only cares about what arrives from now on.
      markOnlineOnConnect: false,
      syncFullHistory: false,
      connectTimeoutMs: 30_000,
      generateHighQualityLinkPreview: false,
      shouldIgnoreJid: (jid) => jid === "status@broadcast",
    });

    this.#sock.ev.on("creds.update", saveCreds);
    this.#sock.ev.on("connection.update", (update) => this.#onConnectionUpdate(update));
    this.#sock.ev.on("messages.upsert", (event) => {
      this.#onMessages(event).catch((err) =>
        this.#log.error({ event: "inbound_handling_failed", error: String(err?.message ?? err) }),
      );
    });
  }

  #onConnectionUpdate({ connection, lastDisconnect, qr }) {
    if (qr) {
      this.#sawQr = true;
      this.#log.info({
        event: "qr_ready",
        hint: "WhatsApp > Appareils liés > Lier un appareil, puis scannez le QR ci-dessous",
      });
      // Raw stdout on purpose: a QR code is not machine-readable JSON.
      qrcode.generate(qr, { small: true });
    }

    if (connection === "open") {
      this.#connected = true;
      this.#loggedOut = false;
      this.#reconnectAttempts = 0;
      this.#me = this.#sock?.user?.id ?? null;
      this.#log.info({ event: "wa_connected", user: this.#me });
      return;
    }

    if (connection === "close") {
      this.#connected = false;
      const statusCode = lastDisconnect?.error?.output?.statusCode;

      if (statusCode === DisconnectReason.loggedOut) {
        this.#loggedOut = true;
        this.#log.fatal({
          event: "wa_logged_out",
          hint:
            "Session invalidée. Supprimez le volume wa_session " +
            "(docker compose down && docker volume rm frilivin-whatsapp-bot_wa_session) " +
            "puis redémarrez pour scanner un nouveau QR.",
        });
        return;
      }

      this.#scheduleReconnect(statusCode);
    }
  }

  #scheduleReconnect(statusCode) {
    if (this.#stopped || this.#reconnectTimer) return;

    // 515 (restartRequired) is the normal handshake right after pairing.
    const immediate = statusCode === DisconnectReason.restartRequired;
    const delay = immediate
      ? 0
      : Math.min(MAX_RECONNECT_DELAY_MS, 1000 * 2 ** this.#reconnectAttempts);
    this.#reconnectAttempts += 1;

    this.#log.warn({
      event: "wa_reconnecting",
      status_code: statusCode ?? null,
      attempt: this.#reconnectAttempts,
      delay_ms: delay,
    });

    this.#reconnectTimer = setTimeout(() => {
      this.#reconnectTimer = null;
      this.#connect().catch((err) => {
        this.#log.error({ event: "wa_reconnect_failed", error: String(err?.message ?? err) });
        this.#scheduleReconnect(statusCode);
      });
    }, delay);
  }

  async #onMessages({ messages, type }) {
    // "notify" is live traffic; "append"/"prepend" are history replays we skip.
    if (type !== "notify") return;

    for (const waMessage of messages ?? []) {
      const payload = toWebhookPayload(waMessage);
      if (!payload) continue;

      // Remember the key so a handler can answer by quoting this message.
      this.#quoted.set(payload.id, trimForQuote({ key: waMessage.key, text: payload.text }));

      await this.#api.sendWebhook(payload);
    }
  }

  async sendText(jid, text, quotedId = null) {
    if (this.#config.dryRun) {
      this.#log.info({ event: "send_dry_run", jid, quoted_id: quotedId, text });
      return `dry-${randomUUID()}`;
    }

    if (!this.#sock || !this.#connected) {
      throw new Error("WhatsApp socket is not connected");
    }

    let quoted;
    if (quotedId) {
      quoted = this.#quoted.get(quotedId);
      if (!quoted) {
        // Baileys needs the message, not its id; older ones fell out of the LRU.
        this.#log.warn({ event: "quoted_message_unknown", quoted_id: quotedId });
      }
    }

    const result = await this.#sock.sendMessage(jid, { text }, quoted ? { quoted } : {});
    return result?.key?.id ?? null;
  }

  async listGroups() {
    if (!this.#sock || !this.#connected) {
      throw new Error("WhatsApp socket is not connected");
    }
    const groups = await this.#sock.groupFetchAllParticipating();
    return Object.values(groups ?? {}).map((group) => ({
      jid: group.id,
      subject: group.subject ?? "",
      participants: group.participants?.length ?? 0,
      announce: Boolean(group.announce),
    }));
  }

  async stop() {
    this.#stopped = true;
    if (this.#reconnectTimer) {
      clearTimeout(this.#reconnectTimer);
      this.#reconnectTimer = null;
    }
    if (this.#watchdog) {
      clearInterval(this.#watchdog);
      this.#watchdog = null;
    }
    try {
      await this.#saveCreds?.();
    } catch {
      // best effort
    }
    this.#sock?.end?.(undefined);
  }
}
