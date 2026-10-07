import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./tests/js/setup.js"],
    include: ["tests/js/**/*.test.js"],
    exclude: ["node_modules", "dist", "build"],
  },
});
