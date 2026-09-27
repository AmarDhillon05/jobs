import "@testing-library/jest-dom/vitest";

// jsdom has no fetch by default in some versions; make sure tests always stub it
// explicitly rather than accidentally hitting the network.
if (!globalThis.fetch) {
  globalThis.fetch = (() => {
    throw new Error("fetch was called without being stubbed in this test");
  }) as unknown as typeof fetch;
}
