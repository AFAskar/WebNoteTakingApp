FROM node:23-alpine

WORKDIR /app

# Enable pnpm via Corepack
RUN corepack enable pnpm

# Copy only dependency files first (better cache)
COPY package.json pnpm-lock.yaml ./

RUN pnpm install --frozen-lockfile

# Copy the rest of the app
COPY . .

CMD ["pnpm", "start"]
