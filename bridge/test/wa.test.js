import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { connectionHint, participantNumbers } from "../src/wa.js";

describe("participantNumbers", () => {
  it("reads the phone form of each participant", () => {
    const group = {
      participants: [
        { id: "33766660673@s.whatsapp.net" },
        { id: "33612345678@s.whatsapp.net" },
      ],
    };
    assert.deepEqual(participantNumbers(group), ["33766660673", "33612345678"]);
  });

  it("prefers the phone number of a LID-addressed participant", () => {
    const group = {
      participants: [{ id: "118141732556813@lid", jid: "33766660673@s.whatsapp.net" }],
    };
    assert.deepEqual(participantNumbers(group), ["33766660673"]);
  });

  it("drops a participant known only by LID", () => {
    // A LID is numeric too: passing it off as a phone number would let it be
    // matched by someone typing digits.
    const group = { participants: [{ id: "118141732556813@lid", lid: "118141732556813@lid" }] };
    assert.deepEqual(participantNumbers(group), []);
  });

  it("strips the device suffix and deduplicates", () => {
    const group = {
      participants: [{ id: "33766660673:12@s.whatsapp.net" }, { id: "33766660673@s.whatsapp.net" }],
    };
    assert.deepEqual(participantNumbers(group), ["33766660673"]);
  });

  it("survives a group without participants", () => {
    assert.deepEqual(participantNumbers({}), []);
    assert.deepEqual(participantNumbers(null), []);
  });
});

describe("connectionHint", () => {
  it("says nothing while connected", () => {
    assert.equal(connectionHint({ connected: true, loggedOut: false, sawQr: true }), null);
  });

  it("tells the operator to re-scan after a logout", () => {
    const hint = connectionHint({ connected: false, loggedOut: true, sawQr: true });
    assert.match(hint, /wa_session/);
  });

  it("points at the phone when a QR is waiting", () => {
    const hint = connectionHint({ connected: false, loggedOut: false, sawQr: true });
    assert.match(hint, /Appareils liés/);
  });

  it("points at the network when no QR ever arrived", () => {
    const hint = connectionHint({ connected: false, loggedOut: false, sawQr: false });
    assert.match(hint, /connectivité sortante/);
  });
});
