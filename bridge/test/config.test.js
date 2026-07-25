import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { loadConfig } from "../src/config.js";

const base = { BOT_TOKEN: "a-token-long-enough" };

describe("loadConfig", () => {
  it("applies sane defaults", () => {
    const config = loadConfig(base);
    assert.equal(config.port, 3000);
    assert.equal(config.apiUrl, "http://api:8000");
    assert.equal(config.minDelayMs, 2000);
    assert.equal(config.maxDelayMs, 8000);
    assert.equal(config.dryRun, false);
  });

  it("rejects a missing or too short token", () => {
    assert.throws(() => loadConfig({}), /BOT_TOKEN/);
    assert.throws(() => loadConfig({ BOT_TOKEN: "short" }), /BOT_TOKEN/);
  });

  it("rejects an inverted delay range", () => {
    assert.throws(
      () => loadConfig({ ...base, SEND_DELAY_MIN_MS: "5000", SEND_DELAY_MAX_MS: "1000" }),
      /SEND_DELAY_MAX_MS/,
    );
  });

  it("ignores BRIDGE_PORT, which is the host-side port", () => {
    assert.equal(loadConfig({ ...base, BRIDGE_PORT: "3999" }).port, 3000);
  });

  it("parses booleans loosely", () => {
    assert.equal(loadConfig({ ...base, BRIDGE_DRY_RUN: "true" }).dryRun, true);
    assert.equal(loadConfig({ ...base, BRIDGE_DRY_RUN: "1" }).dryRun, true);
    assert.equal(loadConfig({ ...base, BRIDGE_DRY_RUN: "no" }).dryRun, false);
    assert.equal(loadConfig({ ...base, BRIDGE_DRY_RUN: "" }).dryRun, false);
  });

  it("strips trailing slashes from the api url", () => {
    assert.equal(loadConfig({ ...base, API_URL: "http://api:8000///" }).apiUrl, "http://api:8000");
  });
});
