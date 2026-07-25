/**
 * Turning a raw Baileys message into the flat payload the API expects.
 *
 * Pure functions on purpose — this is the part most likely to need tweaking as
 * WhatsApp adds message shapes, and it is fully covered by unit tests.
 */

const IGNORED_CHATS = new Set(["status@broadcast"]);

// Envelopes that wrap the real message instead of being one.
const WRAPPERS = [
  "ephemeralMessage",
  "viewOnceMessage",
  "viewOnceMessageV2",
  "viewOnceMessageV2Extension",
  "documentWithCaptionMessage",
  "editedMessage",
];

// Bookkeeping traffic, never business content.
const PROTOCOL_KEYS = new Set([
  "protocolMessage",
  "senderKeyDistributionMessage",
  "messageContextInfo",
]);

export function unwrap(message, depth = 0) {
  if (!message || depth > 5) return message ?? null;
  for (const wrapper of WRAPPERS) {
    if (message[wrapper]?.message) {
      return unwrap(message[wrapper].message, depth + 1);
    }
  }
  return message;
}

export function contentKey(message) {
  if (!message) return null;
  const keys = Object.keys(message).filter((key) => !PROTOCOL_KEYS.has(key));
  return keys[0] ?? null;
}

/** @returns {{type: string, text: string}} */
export function extractContent(message) {
  if (!message) return { type: "other", text: "" };

  if (typeof message.conversation === "string") {
    return { type: "text", text: message.conversation };
  }
  if (message.extendedTextMessage) {
    return { type: "text", text: message.extendedTextMessage.text ?? "" };
  }
  if (message.imageMessage) {
    return { type: "image", text: message.imageMessage.caption ?? "" };
  }
  if (message.videoMessage) {
    return { type: "video", text: message.videoMessage.caption ?? "" };
  }
  if (message.documentMessage) {
    return {
      type: "document",
      text: message.documentMessage.caption ?? message.documentMessage.fileName ?? "",
    };
  }
  if (message.audioMessage) {
    return { type: "audio", text: "" };
  }
  if (message.stickerMessage) {
    return { type: "sticker", text: "" };
  }
  if (message.locationMessage) {
    const { degreesLatitude, degreesLongitude } = message.locationMessage;
    return { type: "location", text: `${degreesLatitude ?? ""},${degreesLongitude ?? ""}` };
  }
  if (message.contactMessage) {
    return { type: "contact", text: message.contactMessage.displayName ?? "" };
  }
  if (message.reactionMessage) {
    return { type: "reaction", text: message.reactionMessage.text ?? "" };
  }
  return { type: "other", text: "" };
}

export function extractQuotedId(message) {
  if (!message) return null;
  for (const value of Object.values(message)) {
    const stanzaId = value?.contextInfo?.stanzaId;
    if (stanzaId) return stanzaId;
  }
  return null;
}

/** protobufjs hands back a Long for timestamps; normalise to seconds. */
export function toEpochSeconds(value, now = () => Date.now()) {
  if (value === null || value === undefined) return Math.floor(now() / 1000);
  if (typeof value === "number") return Math.trunc(value);
  if (typeof value === "string") {
    const parsed = Number.parseInt(value, 10);
    return Number.isFinite(parsed) ? parsed : Math.floor(now() / 1000);
  }
  if (typeof value.toNumber === "function") return Math.trunc(value.toNumber());
  if (typeof value.low === "number") return value.low;
  return Math.floor(now() / 1000);
}

/**
 * @returns the webhook payload, or null when the message must be ignored
 * (our own echoes, status updates, protocol traffic).
 */
export function toWebhookPayload(waMessage, now = () => Date.now()) {
  if (!waMessage?.key || waMessage.key.fromMe) return null;

  const chatJid = waMessage.key.remoteJid;
  if (!chatJid || IGNORED_CHATS.has(chatJid)) return null;

  const message = unwrap(waMessage.message);
  if (!message || !contentKey(message)) return null;

  const isGroup = chatJid.endsWith("@g.us");
  const { type, text } = extractContent(message);

  return {
    id: waMessage.key.id,
    from: isGroup ? (waMessage.key.participant ?? chatJid) : chatJid,
    chat_jid: chatJid,
    is_group: isGroup,
    timestamp: toEpochSeconds(waMessage.messageTimestamp, now),
    type,
    text,
    quoted_id: extractQuotedId(message),
  };
}
