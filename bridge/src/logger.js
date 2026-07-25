/**
 * JSON logs shaped exactly like the Python services':
 * {"level":"info","timestamp":"...","service":"bridge","event":"..."}
 */

import pino from "pino";

export function createLogger(level = "info") {
  return pino({
    level,
    // Replaces pino's default pid/hostname bindings.
    base: { service: "bridge" },
    messageKey: "event",
    timestamp: () => `,"timestamp":"${new Date().toISOString()}"`,
    formatters: {
      level: (label) => ({ level: label }),
    },
  });
}
