/**
 * The WhatsApp connection itself (Baileys, multi-device).
 *
 * Responsibilities are deliberately narrow: stay connected, hand inbound
 * messages to the API, send what the outbox asks for. No business logic —
 * that all lives in the Python handlers.
 */

import { randomUUID } from "node:crypto";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { basename, extname, join } from "node:path";

// Baileys is published as CommonJS; Node's interop exposes makeWASocket as the
// default export and everything else as named exports.
import makeWASocket, {
  Browsers,
  DisconnectReason,
  downloadMediaMessage,
  fetchLatestBaileysVersion,
  makeCacheableSignalKeyStore,
  useMultiFileAuthState,
} from "@whiskeysockets/baileys";
import qrcode from "qrcode-terminal";

import { toWebhookPayload } from "./extract.js";
import { LruCache, trimForQuote } from "./quoted-cache.js";

const MAX_RECONNECT_DELAY_MS = 60_000;
const WATCHDOG_INTERVAL_MS = 45_000;
const OCTET_STREAM = "application/octet-stream";

/**
 * A filename safe to write inside the media directory.
 *
 * Everything a remote sender controls is stripped: directory components,
 * leading dots, and anything outside a conservative character set. The
 * message id prefix keeps two files of the same name apart.
 */
export function safeMediaName(messageId, filename) {
  const base = basename(String(filename ?? "")).replace(/[^A-Za-z0-9._-]/g, "_");
  const cleaned = base.replace(/^\.+/, "").slice(0, 120) || "piece-jointe";
  // No dot is allowed in the id half: a message id never contains one, and
  // permitting it would let "../" survive as ".._".
  const id = String(messageId ?? "").replace(/[^A-Za-z0-9_-]/g, "_") || randomUUID();
  return `${id}-${cleaned}`;
}

/** True when the bridge is willing to put this attachment on disk. */
export function shouldDownload(payload, { extensions, maxBytes }) {
  if (payload?.type !== "document" || !payload.filename) return false;
  if (payload.media_size && payload.media_size > maxBytes) return false;
  if (extensions.length === 0) return true;
  return extensions.includes(extname(payload.filename).toLowerCase());
}

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

      if (shouldDownload(payload, this.#config.media)) {
        payload.media_path = await this.#download(waMessage, payload);
      }

      await this.#api.sendWebhook(payload);
    }
  }

  /**
   * Put an inbound attachment on the shared media volume.
   *
   * A download failure is never fatal: the payload simply reaches the API
   * without a path, and the handler reports the problem to the sender rather
   * than the message vanishing.
   *
   * @returns the absolute path, or null when the download failed.
   */
  async #download(waMessage, payload) {
    const target = join(this.#config.media.inboxDir, safeMediaName(payload.id, payload.filename));
    try {
      const buffer = await downloadMediaMessage(
        waMessage,
        "buffer",
        {},
        { logger: this.#log, reuploadRequest: this.#sock.updateMediaMessage },
      );
      if (buffer.length > this.#config.media.maxBytes) {
        // fileLength is sender-declared; the real size is only known here.
        throw new Error(`fichier trop volumineux (${buffer.length} octets)`);
      }
      await mkdir(this.#config.media.inboxDir, { recursive: true });
      await writeFile(target, buffer);
      this.#log.info({
        event: "media_downloaded",
        message_id: payload.id,
        filename: payload.filename,
        bytes: buffer.length,
      });
      return target;
    } catch (err) {
      this.#log.error({
        event: "media_download_failed",
        message_id: payload.id,
        filename: payload.filename,
        error: String(err?.message ?? err),
      });
      return null;
    }
  }

  async sendDocument(jid, { path, filename, caption = "", mimetype = OCTET_STREAM }) {
    if (this.#config.dryRun) {
      this.#log.info({ event: "send_dry_run", jid, document: path, filename });
      return `dry-${randomUUID()}`;
    }

    if (!this.#sock || !this.#connected) {
      throw new Error("WhatsApp socket is not connected");
    }

    const document = await readFile(path);
    const result = await this.#sock.sendMessage(jid, {
      document,
      fileName: filename || basename(path),
      mimetype,
      ...(caption ? { caption } : {}),
    });
    return result?.key?.id ?? null;
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
