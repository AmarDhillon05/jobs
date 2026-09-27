/**
 * The push-registration flow (PRD §22 "Mobile Push").
 *
 * This is the part of the app that decides whether the user can receive an alert
 * at all, so every branch gets a test: no physical device, permission already
 * granted, permission asked for and granted, permission refused, and the backend
 * registration call failing. `expo-notifications` is mocked (jest.setup.ts), so
 * what is proven here is the app's handling - not that Apple or Google deliver a
 * push, which needs a real handset.
 */
import * as Notifications from "expo-notifications";
import { Platform } from "react-native";

import { registerDevice } from "../api";
import { configureForegroundBehaviour, registerForPushNotifications } from "../notifications";

jest.mock("../api", () => ({
  registerDevice: jest.fn(async () => undefined),
  ApiError: class ApiError extends Error {},
}));

/**
 * `expo-device` is re-mocked here with getters rather than plain fields. Babel's
 * interop copies a namespace object per importer, so a plain field assigned in
 * this file would never be seen by `notifications.ts`; a getter over shared state
 * is - which is what lets one test run the simulator branch.
 */
const mockDeviceState = { isDevice: true, modelName: "Test Phone" };
jest.mock("expo-device", () => ({
  get isDevice() {
    return mockDeviceState.isDevice;
  },
  get modelName() {
    return mockDeviceState.modelName;
  },
}));

const granted = { status: "granted" } as Notifications.NotificationPermissionsStatus;
const denied = { status: "denied" } as Notifications.NotificationPermissionsStatus;

beforeEach(() => {
  jest.clearAllMocks();
  mockDeviceState.isDevice = true;
  mockDeviceState.modelName = "Test Phone";
  (Notifications.getPermissionsAsync as jest.Mock).mockResolvedValue(granted);
  (Notifications.requestPermissionsAsync as jest.Mock).mockResolvedValue(granted);
  (Notifications.getExpoPushTokenAsync as jest.Mock).mockResolvedValue({
    data: "ExponentPushToken[abcdefghij]",
  });
  (registerDevice as jest.Mock).mockResolvedValue(undefined);
});

describe("configureForegroundBehaviour", () => {
  it("installs a handler that shows the alert even in the foreground", async () => {
    configureForegroundBehaviour();
    const handler = (Notifications.setNotificationHandler as jest.Mock).mock.calls[0][0];
    await expect(handler.handleNotification()).resolves.toMatchObject({
      shouldShowAlert: true,
      shouldSetBadge: true,
    });
  });
});

describe("registerForPushNotifications", () => {
  it("returns the token and registers it with the backend", async () => {
    const result = await registerForPushNotifications();
    expect(result).toEqual({ status: "registered", token: "ExponentPushToken[abcdefghij]" });
    expect(registerDevice).toHaveBeenCalledWith(
      expect.stringContaining("Test Phone"),
      "ExponentPushToken[abcdefghij]",
    );
  });

  it("does not ask again when permission is already granted", async () => {
    await registerForPushNotifications();
    expect(Notifications.requestPermissionsAsync).not.toHaveBeenCalled();
  });

  it("asks for permission when it has not been granted yet", async () => {
    (Notifications.getPermissionsAsync as jest.Mock).mockResolvedValue({ status: "undetermined" });
    const result = await registerForPushNotifications();
    expect(Notifications.requestPermissionsAsync).toHaveBeenCalled();
    expect(result.status).toBe("registered");
  });

  it("reports denial instead of throwing", async () => {
    (Notifications.getPermissionsAsync as jest.Mock).mockResolvedValue(denied);
    (Notifications.requestPermissionsAsync as jest.Mock).mockResolvedValue(denied);
    const result = await registerForPushNotifications();
    expect(result.status).toBe("denied");
    expect(result.detail).toMatch(/permission/i);
    expect(registerDevice).not.toHaveBeenCalled();
  });

  it("reports a simulator as unsupported and asks for nothing", async () => {
    mockDeviceState.isDevice = false;
    const result = await registerForPushNotifications();
    expect(result.status).toBe("unsupported");
    expect(result.detail).toMatch(/physical device/i);
    expect(Notifications.getPermissionsAsync).not.toHaveBeenCalled();
  });

  it("surfaces a backend registration failure as an error result", async () => {
    (registerDevice as jest.Mock).mockRejectedValue(new Error("No API write token is configured"));
    const result = await registerForPushNotifications();
    expect(result.status).toBe("error");
    expect(result.detail).toMatch(/write token/);
  });

  it("surfaces a token-minting failure as an error result", async () => {
    (Notifications.getExpoPushTokenAsync as jest.Mock).mockRejectedValue(
      new Error("No projectId found"),
    );
    const result = await registerForPushNotifications();
    expect(result).toMatchObject({ status: "error", detail: "No projectId found" });
  });

  it("creates the Android channel before showing anything, on Android only", async () => {
    const original = Platform.OS;
    Object.defineProperty(Platform, "OS", { value: "android", configurable: true });
    try {
      await registerForPushNotifications();
      expect(Notifications.setNotificationChannelAsync).toHaveBeenCalledWith(
        "internships",
        expect.objectContaining({ importance: Notifications.AndroidImportance.HIGH }),
      );
    } finally {
      Object.defineProperty(Platform, "OS", { value: original, configurable: true });
    }
  });

  it("does not create a channel on iOS", async () => {
    Object.defineProperty(Platform, "OS", { value: "ios", configurable: true });
    await registerForPushNotifications();
    expect(Notifications.setNotificationChannelAsync).not.toHaveBeenCalled();
  });
});
