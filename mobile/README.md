# Internship Monitor — phone app (Expo / React Native)

Receives a push notification the moment a new internship is found, opens the job,
and takes you to the real application page.

There is a second client in [`web/`](../web) — an installable browser feed. Both
read the same API. This one is the one that can ring your phone.

---

## What is proven here, and what needs your phone

Everything in this directory is tested (`npm run test:run`, 122 tests): the
permission flow, token registration, the request the backend sends to Expo,
notification-tap routing, cold-start replay, deep links, duplicate suppression,
and every error state.

**One thing cannot be tested without you.** `expo-notifications` wraps native
modules that do not exist in Node, and this build environment has no iOS or
Android simulator — and a simulator could not mint a push token anyway. So no
automated test here proves that *Apple or Google actually wake your handset*. That
last hop is yours, and the steps below are exactly it. Nothing else in the system
depends on you doing this: the backend, the storage, the dedup and the email path
are all verified without it.

---

## Run it on your phone (about five minutes)

### 1. Install Expo Go

App Store / Play Store → **Expo Go**.

### 2. Point the app at your API

The app needs a URL it can reach *from the phone* — `localhost` means the phone
itself, so a laptop's `localhost:8000` will not do. Either deploy the API (see the
root [README](../README.md)) or expose your local one with a tunnel.

Edit `app.json`:

```json
"extra": {
  "apiBaseUrl": "https://your-api-host",
  "apiWriteToken": "the same value as API_WRITE_TOKEN in your backend .env"
}
```

The write token guards `POST /devices/register` only. A token shipped inside an app
bundle is weak by construction — anyone with the build can read it — and it is
acceptable here only because this is a single-user deployment and that endpoint
merely adds a push target. See "Remaining manual configuration" in the root README.

### 3. Start it

```bash
cd mobile
npm install     # first time only
npm start       # or: make serve-mobile
```

Scan the QR code with the Camera app (iOS) or Expo Go (Android).

### 4. Grant notification permission

The app asks on first launch. If you decline, the feed still works and a notice
appears at the top explaining why nothing will arrive — permission is reset by
deleting and reinstalling Expo Go, or in the OS settings for it.

On success the app registers an `ExponentPushToken[...]` with your backend. Check
it landed:

```bash
python -m jobmonitor.cli devices          # lists registered push targets
```

### 5. Switch the backend to the Expo transport

In the backend's `.env`:

```bash
PUSH_TRANSPORT=expo
```

Nothing else is required. `EXPO_ACCESS_TOKEN` is needed only if you have turned on
Expo's "enhanced security for push notifications" for your project.

### 6. Send yourself one

```bash
# Sends the most recent stored job through the configured push transport and
# prints the deep link it carries, so you can check what you tapped resolved to.
python -m jobmonitor.cli push-test

# Or a real poll, which alerts on anything genuinely new:
python -m jobmonitor.cli poll --company Stripe
```

To isolate the delivery hop even further, use Expo's own tool:
<https://expo.dev/notifications> — paste the token, set `data` to
`{"job_id": "<a real job id>", "deep_link": "https://your-app/jobs/<url-encoded id>"}`,
and send. Tapping it must open that job's detail screen. If it opens the feed
instead, the payload's `job_id`/`deep_link` is the thing to look at — that is the
exact failure the tests in `src/__tests__/routes.test.ts` cover.

---

## Standalone build (optional)

Expo Go is enough for everyday use. A standalone app needs an EAS project:

```bash
npx eas-cli@latest login
npx eas-cli@latest init          # writes extra.eas.projectId into app.json
npx eas-cli@latest build --profile preview --platform ios   # or android
```

`registerForPushNotifications()` already passes `projectId` through, so a token is
minted correctly in both Expo Go and a standalone build.

### If you upgrade the Expo SDK

This app targets **SDK 52**, where Expo Go still supports push notifications on
both platforms. From **SDK 53 onward, Android push in Expo Go is removed** — you
need a development build (`npx expo run:android`, or an EAS build) for Android
pushes. iOS in Expo Go continues to work. Nothing in this app changes; it is a
property of Expo Go, and it is the one upgrade footgun worth knowing about.

---

## Commands

```bash
npm start            # Expo dev server (QR code)
npm run android      # open in an Android emulator/device
npm run ios          # open in an iOS simulator/device (macOS)
npm run test:run     # the 122 tests
npm test             # the same, in watch mode
npm run typecheck    # tsc --noEmit
npm run export       # bundle with Metro + Hermes, no device needed
```

From the repo root: `make test-mobile`, `make serve-mobile`, `make mobile-bundle`.

---

## How it is put together

```
index.ts              registerRootComponent
src/App.tsx           two-state router (feed | job) + notification listeners
src/notifications.ts  permission → Expo push token → POST /devices/register
src/routes.ts         notification payload → screen; /jobs/<id> ↔ job id
src/api.ts            the jobs API client (validates every record)
src/validate.ts       backend records are untrusted; bad ones are dropped
src/components/       JobList, JobDetail, empty/error/loading states
```

Three things worth knowing:

**Routing is a two-state machine, not a navigation library.** The only route that
has to work is the one a push notification produces. Keeping it explicit makes
that path directly testable, which matters more here than a stack animation.

**The detail screen always fetches its own job.** A tap can land there with no feed
loaded at all — that is the cold-start case — so it never depends on the list.

**The backend is distrusted.** Records are validated one by one so a single bad row
cannot blank the feed, and an apply URL that is not `http(s)` is refused outright:
a `javascript:` URL from a compromised upstream job board must never become a
tappable button.
