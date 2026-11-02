# SimConfigManager Hub — tiny image, no npm packages needed
FROM node:22-alpine
WORKDIR /app
COPY package.json ./
COPY src ./src
COPY public ./public
ENV PORT=8780 HOST=0.0.0.0 SCM_HUB_DATA=/data SCM_HUB_CONFIG=/data/config.json
VOLUME ["/data"]
EXPOSE 8780
USER node
HEALTHCHECK --interval=30s --timeout=5s CMD wget -qO- http://127.0.0.1:8780/health >/dev/null || exit 1
CMD ["node", "src/server.js"]
