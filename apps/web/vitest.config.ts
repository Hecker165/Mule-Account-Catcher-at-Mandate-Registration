import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    // Implemented suites only; A8 appends its dashboard suite here when it lands.
    include: [
      "tests/unit/api-client.test.ts",
      "tests/unit/masking.test.ts",
      "tests/unit/risk-session-state.test.ts",
      "tests/unit/dashboard-api.test.ts",
    ],
  },
});
