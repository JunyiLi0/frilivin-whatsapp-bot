/**
 * The bridge's HTTP surface: /send, /groups, /health.
 *
 * /send answers 202 without waiting — the delay and the actual delivery happen
 * in the outbox loop.
 */

import { timingSafeEqual } from "node:crypto";

import express from "express";

const MAX_TEXT_LENGTH = 65536;

function tokensMatch(provided, expected) {
  const a = Buffer.from(String(provided ?? ""));
  const b = Buffer.from(expected);
  return a.length === b.length && timingSafeEqual(a, b);
}

export function createApp({ config, logger, wa, outbox, api }) {
  const app = express();
  app.disable("x-powered-by");
  app.use(express.json({ limit: "1mb" }));

  const requireToken = (req, res, next) => {
    if (!tokensMatch(req.get("X-Bot-Token"), config.token)) {
      logger.warn({ event: "auth_rejected", path: req.path });
      return res.status(401).json({ error: "invalid token" });
    }
    return next();
  };

  // Unauthenticated: this is what Docker's healthcheck calls.
  app.get("/health", (_req, res) => {
    const status = wa.status();
    res.json({
      status: status.connected ? "ok" : "degraded",
      ...status,
      queued: outbox.size,
      pending_webhooks: api.pendingCount,
    });
  });

  app.post("/send", requireToken, async (req, res) => {
    const { id, jid, text, quoted_id: quotedId } = req.body ?? {};

    if (typeof jid !== "string" || jid.length === 0) {
      return res.status(422).json({ error: "jid is required" });
    }
    if (typeof text !== "string" || text.length === 0) {
      return res.status(422).json({ error: "text is required" });
    }
    if (text.length > MAX_TEXT_LENGTH) {
      return res.status(422).json({ error: `text exceeds ${MAX_TEXT_LENGTH} characters` });
    }

    const outboundId = await outbox.enqueue({
      id: typeof id === "string" && id ? id : undefined,
      jid,
      text,
      quotedId: typeof quotedId === "string" && quotedId ? quotedId : null,
    });

    logger.info({ event: "send_accepted", outbound_id: outboundId, jid, queued: outbox.size });
    return res.status(202).json({ id: outboundId, queued: outbox.size });
  });

  app.get("/groups", requireToken, async (_req, res) => {
    try {
      const groups = await wa.listGroups();
      return res.json({ count: groups.length, groups });
    } catch (err) {
      return res.status(503).json({ error: String(err?.message ?? err) });
    }
  });

  app.use((_req, res) => res.status(404).json({ error: "not found" }));

  // Express identifies error handlers by arity, hence the unused 4th argument.
  app.use((err, _req, res, _next) => {
    logger.error({ event: "http_error", error: String(err?.message ?? err) });
    res.status(500).json({ error: "internal error" });
  });

  return app;
}
