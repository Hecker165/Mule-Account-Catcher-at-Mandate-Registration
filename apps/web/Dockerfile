FROM node:22-bookworm-slim

WORKDIR /app

# Install dependencies from lock file first for caching
COPY package.json package-lock.json* ./
RUN npm install

# Copy source code
COPY . .

EXPOSE 3000

CMD ["npm", "run", "dev", "--", "--hostname", "0.0.0.0"]
