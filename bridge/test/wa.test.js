import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { connectionHint, runSupervision, superviseAction } from "../src/wa.js";

// A connection that went down at t=1000, observed 45s later (one watchdog tick).
const DOWN_AT = 1000;
const ONE_TICK = DOWN_AT + 45_000;
const EXIT_AFTER = 900_000;

/** A live, healthy connection — override one field per test. */
function state(overrides) {
  return {
    connected: false,
    loggedOut: false,
    connecting: false,
    reconnectPending: false,
    disconnectedSince: DOWN_AT,
    now: ONE_TICK,
    exitAfterMs: EXIT_AFTER,
    ...overrides,
  };
}

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

describe("superviseAction", () => {
  it("leaves a live connection alone", () => {
    assert.equal(superviseAction(state({ connected: true })), "none");
  });

  it("does not restart a revoked session", () => {
    // Only a human scanning a fresh QR can fix a logout; exiting would just
    // spin the container forever.
    assert.equal(
      superviseAction(state({ loggedOut: true, now: DOWN_AT + 10 * EXIT_AFTER })),
      "none",
    );
  });

  it("waits while a connection attempt is in flight", () => {
    // Firing here would build a second socket on top of the one being created.
    assert.equal(superviseAction(state({ connecting: true })), "none");
  });

  it("waits while a reconnect is already scheduled", () => {
    assert.equal(superviseAction(state({ reconnectPending: true })), "none");
  });

  it("forces a reconnect when the chain died with nothing pending", () => {
    // The 2026-07-28 outage: disconnected, no timer, no attempt, forever.
    assert.equal(superviseAction(state({})), "reconnect");
  });

  it("forces a reconnect when the disconnection predates any timestamp", () => {
    assert.equal(superviseAction(state({ disconnectedSince: null })), "reconnect");
  });

  it("exits once the outage outlasts the deadline", () => {
    assert.equal(superviseAction(state({ now: DOWN_AT + EXIT_AFTER })), "exit");
  });

  it("keeps reconnecting when the exit deadline is disabled", () => {
    assert.equal(
      superviseAction(state({ exitAfterMs: 0, now: DOWN_AT + 10 * EXIT_AFTER })),
      "reconnect",
    );
  });
});

/** Collects what the supervisor logged, so tests can assert on the record. */
function recorder() {
  const events = [];
  const push = (entry) => events.push(entry);
  return { events, warn: push, fatal: push, info: push };
}

describe("runSupervision", () => {
  it("restarts the chain and says so when it finds it dead", () => {
    const log = recorder();
    const calls = [];
    runSupervision({
      state: state({ sawQr: false }),
      log,
      reconnect: () => calls.push("reconnect"),
      exit: () => calls.push("exit"),
    });

    assert.deepEqual(calls, ["reconnect"]);
    assert.ok(log.events.some((e) => e.event === "wa_reconnect_forced"));
  });

  it("exits after the deadline and reports how long the outage lasted", () => {
    const log = recorder();
    const calls = [];
    runSupervision({
      state: state({ sawQr: false, now: DOWN_AT + EXIT_AFTER }),
      log,
      reconnect: () => calls.push("reconnect"),
      exit: (code) => calls.push(`exit:${code}`),
    });

    assert.deepEqual(calls, ["exit:1"]);
    const record = log.events.find((e) => e.event === "wa_supervisor_exit");
    assert.equal(record.disconnected_ms, EXIT_AFTER);
  });

  it("stays completely silent while connected", () => {
    const log = recorder();
    const calls = [];
    runSupervision({
      state: state({ connected: true, sawQr: true }),
      log,
      reconnect: () => calls.push("reconnect"),
      exit: () => calls.push("exit"),
    });

    assert.deepEqual(calls, []);
    assert.deepEqual(log.events, []);
  });

  it("keeps warning about a revoked session without ever restarting", () => {
    const log = recorder();
    const calls = [];
    runSupervision({
      state: state({ loggedOut: true, sawQr: true, now: DOWN_AT + 10 * EXIT_AFTER }),
      log,
      reconnect: () => calls.push("reconnect"),
      exit: () => calls.push("exit"),
    });

    assert.deepEqual(calls, []);
    assert.ok(log.events.some((e) => e.event === "wa_not_connected"));
  });
});
