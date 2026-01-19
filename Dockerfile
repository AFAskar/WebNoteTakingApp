FROM node:23-alpine

WORKDIR /app

# Enable pnpm via Corepack
RUN corepack enable pnpm

# Copy only dependency files first (better cache)
COPY package.json pnpm-lock.yaml ./

RUN pnpm install --frozen-lockfile

# Copy the rest of the app
COPY . .

RUN addgroup -g 1001 -S nodejs && \
    adduser -S nodejs -u 1001 && \
    chown -R nodejs:nodejs /app

USER nodejs

EXPOSE 3000
CMD ["pnpm", "start"]
