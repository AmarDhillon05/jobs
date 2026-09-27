/**
 * Jest setup.
 *
 * expo-notifications talks to native modules that do not exist in a Node test
 * process, so the whole module is replaced with jest mocks. That is the standard
 * approach for Expo, and it is honest about what these tests cover: the app's
 * *handling* of notifications - permission flow, token registration, tap routing -
 * not the OS delivering one. Actual delivery needs a physical device; see
 * mobile/README.md.
 */
import { jest } from "@jest/globals";
// Brings in toHaveTextContent / toBeOnTheScreen / toBeDisabled etc.
import "@testing-library/react-native/extend-expect";

jest.mock("expo-notifications", () => ({
  setNotificationHandler: jest.fn(),
  getPermissionsAsync: jest.fn(async () => ({ status: "granted" })),
  requestPermissionsAsync: jest.fn(async () => ({ status: "granted" })),
  getExpoPushTokenAsync: jest.fn(async () => ({ data: "ExponentPushToken[test-token]" })),
  setNotificationChannelAsync: jest.fn(async () => undefined),
  addNotificationReceivedListener: jest.fn(() => ({ remove: jest.fn() })),
  addNotificationResponseReceivedListener: jest.fn(() => ({ remove: jest.fn() })),
  getLastNotificationResponseAsync: jest.fn(async () => null),
  AndroidImportance: { MAX: 5, HIGH: 4, DEFAULT: 3 },
}));

jest.mock("expo-device", () => ({ isDevice: true, modelName: "Test Phone" }));

// Silence the Animated/act noise React Native emits in a Node environment.
jest.spyOn(console, "warn").mockImplementation(() => {});
