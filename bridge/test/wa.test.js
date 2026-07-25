import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { connectionHint } from "../src/wa.js";

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
