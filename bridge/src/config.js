/**
 * Environment configuration, validated once at startup.
 *
 * Note on ports: BRIDGE_PORT in .env is the *host* port published by Docker.
 * Inside the container the bridge always listens on 3000, otherwise changing
 * BRIDGE_PORT would break the port mapping.
 */

const INTERNAL_PORT = 3000;

function int(value, fallback) {
  const parsed = Number.parseInt(value ?? "", 10);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function bool(value, fallback = false) {
  if (value === undefined || value === "") return fallback;
  return ["1", "true", "yes", "on"].includes(String(value).toLowerCase());
}

export function loadConfig(env = process.env) {
  const token = env.BOT_TOKEN ?? "";
  if (token.length < 8) {
    throw new Error(
      "BOT_TOKEN is missing or too short. Copy .env.example to .env and set it (openssl rand -hex 32).",
    );
  }

  const minDelayMs = int(env.SEND_DELAY_MIN_MS, 2000);
  const maxDelayMs = int(env.SEND_DELAY_MAX_MS, 8000);
  if (maxDelayMs < minDelayMs) {
    throw new Error("SEND_DELAY_MAX_MS must be greater than or equal to SEND_DELAY_MIN_MS");
  }

  return {
    port: INTERNAL_PORT,
    token,
    logLevel: env.LOG_LEVEL ?? "info",
    apiUrl: (env.API_URL ?? "http://api:8000").replace(/\/+$/, ""),
    sessionDir: env.WA_SESSION_DIR ?? "/data/session",
    outboxPath: env.OUTBOX_PATH ?? "/data/outbox.json",
    browserName: env.WA_BROWSER_NAME ?? "frilivin-bot",
    quotedCacheSize: int(env.QUOTED_CACHE_SIZE, 2000),
    dryRun: bool(env.BRIDGE_DRY_RUN, false),
    minDelayMs,
    maxDelayMs,
    // Retries for a single outbound message before it is reported as failed.
    maxSendAttempts: int(env.SEND_MAX_ATTEMPTS, 5),
    // Retries for one webhook delivery before it goes to the pending buffer.
    webhookAttempts: int(env.WEBHOOK_ATTEMPTS, 3),
    webhookTimeoutMs: int(env.WEBHOOK_TIMEOUT_MS, 5000),
  };
}
