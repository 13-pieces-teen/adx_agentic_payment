# syntax=docker/dockerfile:1.7
# 前端独立部署镜像 —— 只打包 Next.js web，不含后端。
# build context = frontend/ 目录本身（区别于 deploy/docker/Dockerfile.web 的 repo 根 context）。
# 后端 API 由 NEXT_PUBLIC_API_URL 指向 https://api.arena402.com，本镜像不含 api/postgres。

FROM node:24-alpine AS dependencies
WORKDIR /app
COPY package.json package-lock.json ./
RUN --mount=type=cache,target=/root/.npm \
    npm ci --ignore-scripts --no-audit --no-fund

FROM node:24-alpine AS builder
ENV NEXT_TELEMETRY_DISABLED=1
WORKDIR /app
COPY --from=dependencies /app/node_modules ./node_modules
COPY . ./

# NEXT_PUBLIC_* 在 build 期被内联进产物，所以必须在这里注入
ARG NEXT_PUBLIC_API_URL=https://api.arena402.com
ENV NEXT_PUBLIC_API_URL=${NEXT_PUBLIC_API_URL}

RUN --mount=type=cache,target=/app/.next/cache \
    npm run build \
    && npm prune --omit=dev --no-audit --no-fund

FROM node:24-alpine AS runtime
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    HOSTNAME=0.0.0.0 \
    PORT=3000
WORKDIR /app

COPY --from=builder --chown=node:node /app/package.json ./package.json
COPY --from=builder --chown=node:node /app/node_modules ./node_modules
COPY --from=builder --chown=node:node /app/.next ./.next
COPY --from=builder --chown=node:node /app/next.config.js ./next.config.js

USER node
EXPOSE 3000
CMD ["npm", "run", "start"]
