import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    setupFiles: ["./tests/no-network.ts"],
    globals: true,
    environment: "node",
    include: ["tests/**/*.test.ts"],
  },
});
