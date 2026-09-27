import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: { port: 5173 },
  // `appType: "spa"` (the default) gives history-API fallback, which is what lets
  // a notification deep link to /jobs/<id> load the app directly.
  build: { outDir: "dist", sourcemap: true },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/setupTests.ts"],
    // Playwright specs live in e2e/ and are run by `npm run test:e2e`.
    exclude: ["e2e/**", "node_modules/**", "dist/**"],
    coverage: { reporter: ["text"], include: ["src/**"] },
  },
});
