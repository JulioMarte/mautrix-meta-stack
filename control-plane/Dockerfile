FROM oven/bun:1.4.2 AS deps
WORKDIR /app
COPY package.json ./
RUN bun install

FROM oven/bun:1.4.2
WORKDIR /app
ENV NODE_ENV=production
COPY --from=deps /app/node_modules ./node_modules
COPY package.json tsconfig.json ./
COPY src ./src
RUN mkdir -p /data
USER bun
EXPOSE 3000
CMD ["bun", "src/index.ts"]
