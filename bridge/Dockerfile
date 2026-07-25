FROM node:22-bookworm-slim AS deps

# Baileys pulls libsignal directly from GitHub, so git has to be available at
# install time — the slim image does not ship it.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev && npm cache clean --force


FROM node:22-bookworm-slim AS runtime

ENV NODE_ENV=production \
    NODE_OPTIONS=--max-old-space-size=384

# The session and the outbox live here, on the wa_session volume.
RUN mkdir -p /data && chown node:node /data

WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY package.json ./
COPY src ./src

USER node
EXPOSE 3000
VOLUME ["/data"]

CMD ["node", "src/index.js"]
