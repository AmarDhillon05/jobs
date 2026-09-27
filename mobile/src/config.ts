import Constants from "expo-constants";

interface Extra {
  apiBaseUrl?: string;
  apiWriteToken?: string;
}

function extra(): Extra {
  // expoConfig is the modern field; manifest2/manifest cover older runtimes and
  // Expo Go, where the same values arrive under a different key.
  const config = Constants.expoConfig?.extra ?? (Constants as { manifest?: { extra?: Extra } }).manifest?.extra;
  return (config ?? {}) as Extra;
}

/** Where the jobs API lives. Override per-build in app.json -> extra.apiBaseUrl. */
export const API_BASE: string = (extra().apiBaseUrl ?? "http://localhost:8000").replace(/\/$/, "");

/**
 * Token for the write endpoint (device registration).
 *
 * Shipping a shared token in an app bundle is weak by construction - anyone with
 * the .ipa can read it. It is acceptable here only because the endpoint it guards
 * merely adds a push target for this single-user deployment. Recorded in
 * README.md "Remaining manual configuration"; a real multi-user build wants a
 * proper auth flow.
 */
export const API_WRITE_TOKEN: string = extra().apiWriteToken ?? "";
