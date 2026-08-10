import assert from "node:assert/strict";
import { after, describe, it } from "node:test";

import { createApp } from "../src/http.js";

const noopLogger = { info() {}, warn() {}, error() {} };

/** Starts the app on an ephemeral port; the caller gets a base URL and a stop. */
async function serve(waStatus) {
  const app = createApp({
    config: { token: "test-token-long-enough" },
    logger: noopLogger,
    wa: { status: () => waStatus },
    outbox: { size: 0 },
    api: { pendingCount: 0 },
  });

  const server = await new Promise((resolve) => {
    const s = app.listen(0, "127.0.0.1", () => resolve(s));
  });
  const { port } = server.address();
  return {
    url: `http://127.0.0.1:${port}`,
    stop: () => new Promise((resolve) => server.close(resolve)),
  };
}

const CONNECTED = {
  connected: true,
  logged_out: false,
  user: "33600000000:1@s.whatsapp.net",
  quoted_cache: 3,
  dry_run: false,
};

const DOWN = { ...CONNECTED, connected: false, user: null };

describe("GET /health", () => {
  const servers = [];
  after(async () => {
    await Promise.all(servers.map((s) => s.stop()));
  });

  it("answers 200 while WhatsApp is connected", async () => {
    const server = await serve(CONNECTED);
    servers.push(server);

    const response = await fetch(`${server.url}/health`);

    assert.equal(response.status, 200);
    assert.equal((await response.json()).status, "ok");
  });

  it("answers 503 while WhatsApp is down", async () => {
    // Docker only shows a container as unhealthy on a failing status code:
    // a 200 carrying "degraded" is what hid the 2026-07-28 outage for 12 days.
    const server = await serve(DOWN);
    servers.push(server);

    const response = await fetch(`${server.url}/health`);

    assert.equal(response.status, 503);
  });

  it("still describes the failure in the body", async () => {
    const server = await serve(DOWN);
    servers.push(server);

    const body = await (await fetch(`${server.url}/health`)).json();

    assert.equal(body.status, "degraded");
    assert.equal(body.connected, false);
    assert.equal(body.logged_out, false);
  });
});
