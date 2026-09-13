import type { NextConfig } from "next";

// A7 exception (documented): same-origin /api proxy so browsers never call
// the API cross-origin (the API serves no CORS headers). Browsers use the
// relative default in lib/api-client.ts; absolute NEXT_PUBLIC_API_BASE_URL
// bypasses the proxy when set. No backend or contract change involved.
const API_DESTINATION = `${
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000"
}/:path*`;

const nextConfig: NextConfig = {
  reactStrictMode: true,
  output: "standalone",
  async rewrites() {
    return [{ source: "/api/:path*", destination: API_DESTINATION }];
  },
};

export default nextConfig;
