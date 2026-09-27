import Constants from "expo-constants";
import * as Device from "expo-device";
import * as Notifications from "expo-notifications";
import { Platform } from "react-native";

import { registerDevice } from "./api";

/** Show alerts even when the app is foregrounded - a new internship is worth it. */
export function configureForegroundBehaviour(): void {
  Notifications.setNotificationHandler({
    handleNotification: async () => ({
      shouldShowAlert: true,
      shouldPlaySound: false,
      shouldSetBadge: true,
      shouldShowBanner: true,
      shouldShowList: true,
    }),
  });
}

export interface RegistrationResult {
  status: "registered" | "denied" | "unsupported" | "error";
  token?: string;
  detail?: string;
}

function projectId(): string | undefined {
  return (
    Constants.expoConfig?.extra?.eas?.projectId ??
    (Constants as { easConfig?: { projectId?: string } }).easConfig?.projectId
  );
}

/**
 * Ask for permission, get an Expo push token, and register it with the backend.
 *
 * Returns a *result* rather than throwing: notifications failing to set up must
 * not stop the user browsing the feed, and the reason needs to be showable.
 */
export async function registerForPushNotifications(): Promise<RegistrationResult> {
  // A simulator has no push certificate, so Expo cannot mint a token there.
  if (!Device.isDevice) {
    return {
      status: "unsupported",
      detail: "Push notifications need a physical device, not a simulator.",
    };
  }

  if (Platform.OS === "android") {
    // Android requires a channel before any notification can be shown.
    await Notifications.setNotificationChannelAsync("internships", {
      name: "New internships",
      importance: Notifications.AndroidImportance.HIGH,
      lightColor: "#5b8cff",
    });
  }

  const existing = await Notifications.getPermissionsAsync();
  let status = existing.status;
  if (status !== "granted") {
    status = (await Notifications.requestPermissionsAsync()).status;
  }
  if (status !== "granted") {
    return { status: "denied", detail: "Notification permission was not granted." };
  }

  try {
    const token = await Notifications.getExpoPushTokenAsync({ projectId: projectId() });
    const deviceId = `${Platform.OS}-${Device.modelName ?? "device"}-${token.data.slice(-10)}`;
    await registerDevice(deviceId, token.data);
    return { status: "registered", token: token.data };
  } catch (cause) {
    return {
      status: "error",
      detail: cause instanceof Error ? cause.message : "Could not register for notifications.",
    };
  }
}
