import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  contentKey,
  extractContent,
  extractQuotedId,
  toEpochSeconds,
  toWebhookPayload,
  unwrap,
} from "../src/extract.js";

const privateKey = { id: "AAA111", remoteJid: "33612345678@s.whatsapp.net", fromMe: false };
const groupKey = {
  id: "BBB222",
  remoteJid: "120363000000000000@g.us",
  participant: "33698765432@s.whatsapp.net",
  fromMe: false,
};

describe("extractContent", () => {
  it("reads a plain conversation", () => {
    assert.deepEqual(extractContent({ conversation: "salut" }), { type: "text", text: "salut" });
  });

  it("reads an extended text message", () => {
    assert.deepEqual(extractContent({ extendedTextMessage: { text: "coucou" } }), {
      type: "text",
      text: "coucou",
    });
  });

  it("reads an image caption", () => {
    assert.deepEqual(extractContent({ imageMessage: { caption: "la photo" } }), {
      type: "image",
      text: "la photo",
    });
  });

  it("falls back to the document file name", () => {
    assert.deepEqual(extractContent({ documentMessage: { fileName: "facture.pdf" } }), {
      type: "document",
      text: "facture.pdf",
    });
  });

  it("flattens a location", () => {
    assert.deepEqual(
      extractContent({ locationMessage: { degreesLatitude: 48.85, degreesLongitude: 2.35 } }),
      { type: "location", text: "48.85,2.35" },
    );
  });

  it("labels anything unknown as other", () => {
    assert.deepEqual(extractContent({ pollCreationMessage: {} }), { type: "other", text: "" });
  });
});

describe("unwrap", () => {
  it("unwraps ephemeral messages", () => {
    const wrapped = { ephemeralMessage: { message: { conversation: "secret" } } };
    assert.deepEqual(unwrap(wrapped), { conversation: "secret" });
  });

  it("unwraps nested view-once inside ephemeral", () => {
    const wrapped = {
      ephemeralMessage: { message: { viewOnceMessageV2: { message: { conversation: "ok" } } } },
    };
    assert.deepEqual(unwrap(wrapped), { conversation: "ok" });
  });

  it("leaves a plain message alone", () => {
    assert.deepEqual(unwrap({ conversation: "x" }), { conversation: "x" });
  });
});

describe("contentKey", () => {
  it("ignores protocol-only payloads", () => {
    assert.equal(contentKey({ protocolMessage: {} }), null);
    assert.equal(contentKey({ messageContextInfo: {} }), null);
  });

  it("finds the real content next to context info", () => {
    assert.equal(contentKey({ messageContextInfo: {}, conversation: "hey" }), "conversation");
  });
});

describe("extractQuotedId", () => {
  it("digs the stanza id out of contextInfo", () => {
    const message = {
      extendedTextMessage: { text: "re", contextInfo: { stanzaId: "ORIGINAL42" } },
    };
    assert.equal(extractQuotedId(message), "ORIGINAL42");
  });

  it("returns null when nothing is quoted", () => {
    assert.equal(extractQuotedId({ conversation: "hello" }), null);
  });
});

describe("toEpochSeconds", () => {
  it("passes numbers through", () => {
    assert.equal(toEpochSeconds(1700000000), 1700000000);
  });

  it("handles protobuf Long objects", () => {
    assert.equal(toEpochSeconds({ toNumber: () => 1700000001 }), 1700000001);
  });

  it("handles numeric strings", () => {
    assert.equal(toEpochSeconds("1700000002"), 1700000002);
  });

  it("falls back to now when missing", () => {
    assert.equal(toEpochSeconds(undefined, () => 1700000003000), 1700000003);
  });
});

describe("toWebhookPayload", () => {
  it("builds the payload for a private message", () => {
    const payload = toWebhookPayload({
      key: privateKey,
      messageTimestamp: 1700000000,
      message: { conversation: "!ping" },
    });

    assert.deepEqual(payload, {
      id: "AAA111",
      from: "33612345678@s.whatsapp.net",
      chat_jid: "33612345678@s.whatsapp.net",
      is_group: false,
      timestamp: 1700000000,
      type: "text",
      text: "!ping",
      quoted_id: null,
    });
  });

  it("uses the participant as sender in a group", () => {
    const payload = toWebhookPayload({
      key: groupKey,
      messageTimestamp: 1700000000,
      message: { conversation: "bonjour" },
    });

    assert.equal(payload.is_group, true);
    assert.equal(payload.from, "33698765432@s.whatsapp.net");
    assert.equal(payload.chat_jid, "120363000000000000@g.us");
  });

  it("prefers the phone number when the chat is addressed by LID", () => {
    const payload = toWebhookPayload({
      key: {
        id: "DDD444",
        remoteJid: "118141732556813@lid",
        senderPn: "33612345678@s.whatsapp.net",
        fromMe: false,
      },
      messageTimestamp: 1700000000,
      message: { conversation: "!envoi" },
    });

    // The allowlist is written with phone numbers, so `from` has to be one.
    assert.equal(payload.from, "33612345678@s.whatsapp.net");
    // Replies still go back to the address WhatsApp actually used.
    assert.equal(payload.chat_jid, "118141732556813@lid");
  });

  it("falls back to the LID when no phone number is provided", () => {
    const payload = toWebhookPayload({
      key: { id: "EEE555", remoteJid: "118141732556813@lid", fromMe: false },
      messageTimestamp: 1700000000,
      message: { conversation: "coucou" },
    });

    assert.equal(payload.from, "118141732556813@lid");
  });

  it("prefers the participant's phone number in a LID-addressed group", () => {
    const payload = toWebhookPayload({
      key: {
        ...groupKey,
        participant: "118141732556813@lid",
        participantPn: "33698765432@s.whatsapp.net",
      },
      messageTimestamp: 1700000000,
      message: { conversation: "bonjour" },
    });

    assert.equal(payload.from, "33698765432@s.whatsapp.net");
  });

  it("ignores our own messages", () => {
    const payload = toWebhookPayload({
      key: { ...privateKey, fromMe: true },
      message: { conversation: "echo" },
    });
    assert.equal(payload, null);
  });

  it("ignores status broadcasts", () => {
    const payload = toWebhookPayload({
      key: { id: "C1", remoteJid: "status@broadcast", fromMe: false },
      message: { conversation: "story" },
    });
    assert.equal(payload, null);
  });

  it("ignores protocol traffic", () => {
    const payload = toWebhookPayload({
      key: privateKey,
      message: { protocolMessage: { type: 0 } },
    });
    assert.equal(payload, null);
  });

  it("ignores empty envelopes", () => {
    assert.equal(toWebhookPayload({ key: privateKey, message: null }), null);
    assert.equal(toWebhookPayload({}), null);
  });
});
