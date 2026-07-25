/**
 * Bridge entry point: config → logger → api client → WhatsApp → outbox → HTTP.
 */

import { ApiClient } from "./api-client.js";
import { loadConfig } from "./config.js";
import { createApp } from "./http.js";
import { createLogger } from "./logger.js";
import { Outbox } from "./outbox.js";
import { WhatsAppClient } from "./wa.js";

async function main() {
  const config = loadConfig();
  const logger = createLogger(config.logLevel);

  logger.info({
    event: "bridge_starting",
    api_url: config.apiUrl,
    session_dir: config.sessionDir,
    dry_run: config.dryRun,
    send_delay_ms: [config.minDelayMs, config.maxDelayMs],
  });

  const api = new ApiClient({
    baseUrl: config.apiUrl,
    token: config.token,
    logger,
    attempts: config.webhookAttempts,
    timeoutMs: config.webhookTimeoutMs,
  });

  const wa = new WhatsAppClient({ config, logger, api });

  const outbox = new Outbox({
    path: config.outboxPath,
    logger,
    minDelayMs: config.minDelayMs,
    maxDelayMs: config.maxDelayMs,
    maxAttempts: config.maxSendAttempts,
    send: (item) => wa.sendText(item.jid, item.text, item.quotedId),
    onSettled: (item, result) =>
      api.sendStatus({
        id: item.id,
        status: result.status,
        error: result.error ?? null,
        wa_message_id: result.waMessageId ?? null,
      }),
  });

  await outbox.load();
  await wa.start();

  const app = createApp({ config, logger, wa, outbox, api });
  const server = app.listen(config.port, "0.0.0.0", () => {
    logger.info({ event: "http_listening", port: config.port });
  });

  const shutdown = async (signal) => {
    logger.info({ event: "shutting_down", signal });
    outbox.stop();
    api.stop();
    server.close();
    await wa.stop();
    // Give in-flight logs a moment, then exit.
    setTimeout(() => process.exit(0), 250).unref();
  };

  process.on("SIGTERM", () => void shutdown("SIGTERM"));
  process.on("SIGINT", () => void shutdown("SIGINT"));
  process.on("unhandledRejection", (reason) => {
    logger.error({ event: "unhandled_rejection", error: String(reason) });
  });
}

main().catch((err) => {
  // No logger guaranteed at this point (config may be what failed).
  console.error(JSON.stringify({ level: "fatal", event: "bridge_start_failed", error: String(err?.message ?? err) }));
  process.exit(1);
});
