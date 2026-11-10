# Feature Specification: Bark Desktop Client for Windows

**Feature Branch**: `001-bark-desktop-client`

**Created**: 2026-11-10

**Status**: Draft

**Input**: User description: "create a pc version client for Finb/bark, run as background service, support mini icon as system tray, windows first, linux macos in future version, support basic feature (text/image) first, use pypush register to apple push service"

**Technical context (user-supplied)**: Implemented in Python; GUI toolkit selected for low memory use (no embedded browser engine); `pypush` consumed as a dependency for device registration.

## Clarifications

### Session 2026-11-10

- Q: How should the PC obtain and connect the device key that senders push to? → A: The client registers its push identity with the configured Bark server (public or self-hosted) through that server's device-registration API, and the server forwards pushes to it.
- Q: When the PC was off, asleep, or the app was fully quit, should notifications from that period still be recovered the next time it starts? → A: No — only notifications received while connected appear; anything sent while off is lost.
- Q: Should the client run only while a user is signed in, or also before sign-in as a system-level service? → A: Per-user background app that starts at sign-in, runs invisibly in the tray, and stops when the user signs out.
- Q: Should v1 ship as an installable per-user application, a portable folder, or both? → A: Portable folder/archive the user runs directly, with optional autostart configured from the tray menu.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Receive Bark notifications on the Windows desktop (Priority: P1)

A user who already uses Bark on iPhone (or any sender that calls a Bark-compatible push API) installs the desktop client on Windows. It keeps running in the background after they close its window. When a notification is pushed to the user's Bark device key, it appears as a native Windows notification with the sender's title and body, within seconds, without the user having to open the app.

**Why this priority**: This is the entire product. Without reliable background delivery, nothing else in the client has value.

**Independent Test**: With the client running and minimized, push a text message via a Bark-compatible API using the client's own device key; verify a native Windows notification shows the correct title/body while the client window is closed.

**Acceptance Scenarios**:

1. **Given** the client is set up and running in the background, **When** a text notification is pushed to the user's device key, **Then** a native Windows notification appears showing the pushed title and body within 10 seconds of the push being accepted by the push infrastructure.
2. **Given** the client window is closed (tray-only), **When** a notification arrives, **Then** the notification still appears and the tray icon indicates the new unread count.
3. **Given** the client process is not running, **When** a notification is pushed, **Then** the push provider's device state is not corrupted, the message is not recovered later, and the client's status makes clear when it was last connected so the user can tell a gap occurred.
4. **Given** the user clicks the desktop notification, **When** the notification carries a target link, **Then** the default browser opens that link.

---

### User Story 2 - Set up the client as a Bark device (Priority: P1)

On first launch, the user completes a short guided setup: the client performs anonymous device activation to obtain a push-receiving identity (no Apple account or password is needed or stored), registers that identity's push token with the chosen Bark server, and produces a Bark device key plus the server address to use. The user can copy that key, show it as a QR code, or export it as a share link to paste into senders.

**Why this priority**: Without a device identity and a device key, the user cannot receive anything. This is a hard prerequisite for Story 1.

**Independent Test**: On a clean machine, complete the wizard with no account credentials at all and confirm a valid device key and QR code are produced, then use them from a sender to deliver the first successful test notification.

**Acceptance Scenarios**:

1. **Given** a clean machine with no existing configuration or unpacked folder, **When** the user starts the client, **Then** a setup wizard explains what will happen, runs anonymous device activation, registers the device key with the chosen server, and completes without asking for an Apple account or password.
2. **Given** registration succeeded, **When** setup finishes, **Then** the user is shown their device key, the Bark server address in use, and a QR code, each copyable.
3. **Given** setup finished, **When** the user sends a test notification from the client, **Then** the client reports whether the test push was accepted, without the user needing an external tool.
4. **Given** the activation service rejects or times out the activation request, **When** the user retries, **Then** the client starts a fresh activation attempt without leaving a partial identity behind and keeps any previously working identity usable.
5. **Given** registration fails (activation service error, network error, server rejection, invalid token), **When** the failure occurs, **Then** the user sees an actionable message in plain language and no partial/duplicate device identity is left behind.

---

### User Story 3 - Review recent notifications from the tray (Priority: P2)

The user misses a desktop toast, or wants to look back at what was pushed. From the tray icon they open a compact list of recent notifications, newest first, with unread markers, and can open one to read its full content and any attached image. They can clear unread counts and delete entries.

**Why this priority**: Desktop toasts are transient. A durable, glanceable history is what makes the client a usable inbox rather than a leaky pipe.

**Independent Test**: Push several notifications, dismiss the toasts, then open the history from the tray and confirm all of them are listed, ordered, readable, and removable.

**Acceptance Scenarios**:

1. **Given** three notifications have arrived, **When** the user left-clicks the tray icon, **Then** a window opens showing the three entries newest-first with unread indicators.
2. **Given** the history window is open, **When** the user selects an entry, **Then** the full title, body, timestamp, group, and image thumbnail (if any) are displayed.
3. **Given** unread entries exist, **When** the user opens the history or chooses "mark all as read", **Then** the tray unread badge resets to zero.
4. **Given** the client has been restarted, **When** the history window opens, **Then** previously stored notifications are still present.
5. **Given** the sender issues a delete/recall request for a notification, **When** it is received, **Then** the corresponding entry is removed from the client's history and tray.

---

### User Story 4 - Receive and view image notifications (Priority: P2)

A push carries an image reference (a custom icon/image URL, or an image embedded in markdown content). The client shows a thumbnail in the notification and in history, and the user can open the full-size image in a viewer or save it locally. If the image cannot be fetched, the text notification is still delivered with a clear placeholder.

**Why this priority**: Image pushes are the second most common Bark use case (dashboards, monitoring snapshots, album art, screenshots) and the user explicitly asked for text plus image support in v1.

**Independent Test**: Push a notification with an image URL that is reachable, and another whose image URL is unreachable; confirm the first renders a thumbnail that opens full size, and the second still delivers its text with a placeholder.

**Acceptance Scenarios**:

1. **Given** a notification arrives with a reachable image URL, **When** the notification is displayed, **Then** a thumbnail is shown alongside the text and can be opened full-size.
2. **Given** a notification arrives with an unreachable or invalid image URL, **When** it is displayed, **Then** the text is still delivered, no error dialog interrupts the user, and the entry clearly indicates the image is unavailable.
3. **Given** an image has already been fetched once, **When** the same image URL arrives again, **Then** it is not downloaded a second time and renders immediately.
4. **Given** a large image is offered, **When** it is displayed, **Then** the client remains responsive and the thumbnail is bounded in size.
5. **Given** a full-size image is open, **When** the user chooses save or copy, **Then** the image is written to a user-chosen location or placed on the clipboard successfully.

---

### User Story 5 - Tray-first, unobtrusive operation (Priority: P3)

The user treats the client as a background utility: it starts automatically at sign-in if enabled, lives only in the system tray, restores its window on double-click, and exposes quick actions (open, test push, copy device key, connection status, quit). It never requires the user to keep a window open, and it uses negligible CPU while idle.

**Why this priority**: This is what makes the product pleasant as a daily background service, but the core receive/history value already stands without it.

**Independent Test**: Enable start-at-sign-in, reboot or sign out/in, confirm the client appears only as a tray icon, that quick actions work, and that idle resource usage stays near zero.

**Acceptance Scenarios**:

1. **Given** the user enables "start with Windows", **When** the machine restarts and the user signs in, **Then** the client is running and visible only in the tray, without an elevated prompt.
2. **Given** the client is running, **When** the user double-clicks the tray icon, **Then** the history window is restored and focused.
3. **Given** the client runs continuously for a day, **When** no notifications arrive, **Then** it consumes negligible CPU and a stable, modest amount of memory.
4. **Given** the connection to the push infrastructure is lost, **When** the client detects it, **Then** the tray icon reflects a degraded state and reconnects automatically without user action.
5. **Given** the user chooses quit from the tray, **When** they confirm, **Then** the process exits cleanly and stops receiving notifications (no orphaned background processes).

---

### Edge Cases

- **Push identity becomes invalid** (expired/revoked registration, Apple-side change): the client must detect authentication failures, mark itself as disconnected in the tray, and offer re-registration in one guided step without losing history or settings.
- **Duplicate registration attempts**: repeated setup or re-login must not create several device identities or leave orphaned entries in the server; only the active identity is used.
- **Two-factor or account prompts**: the activation path requires neither; if a service ever demands an interactive account confirmation, the client must fail with a clear explanation and offer retry instead of hanging or opening an unexpected authorisation window.
- **Activation service errors or timeouts**: retry is allowed but must be rate-limited, must reuse a cached activation when valid, and must not create a partial identity.
- **Machine sleeps or loses network**: on resume, the client resumes receiving without user action. Notifications sent while asleep or disconnected are not recovered (by decision), so the client MUST show the last-connected time and MUST NOT display duplicates or stale orderings after resume.
- **Notification bursts** (dozens in seconds): notifications must all be recorded; desktop toasts may be coalesced or throttled to avoid flooding the screen, but nothing may be silently dropped from history.
- **Very long titles/bodies, emoji, right-to-left text, and non-Latin scripts**: must render without truncation errors or mojibake; notification text is truncated only where the OS display requires it.
- **Oversized payloads or unsupported fields**: unknown push fields are ignored; over-large payloads are recorded with a readable error rather than crashing.
- **Images**: non-image content served at an image URL, redirects, very slow hosts, huge files, and hotlink-blocked hosts must fall back to text with an unavailable-image indicator; image fetching must not block receipt of other notifications.
- **Clock changes and daylight-saving shifts**: stored timestamps and ordering remain correct.
- **Multiple monitors, high-DPI scaling, and OS theme changes**: tray icon and windows remain legible and correctly sized.
- **Full disk, read-only settings location, or corrupted local store**: the client reports the problem, continues receiving where possible, and does not silently lose the user's data.
- **Portable folder moved, renamed, or copied**: autostart entries and shortcuts that point at the old location must be detected and repaired or disabled; copying the folder must not cause two instances to fight over the same device key and history.
- **Antivirus/firewall interference or blocked outbound connections**: surfaced as a diagnosable connection error, not as silence.
- **Server-side registration refusal or outage**: a server that rejects or cannot reach its registration API must not destroy an existing working link; the user keeps receiving on the previous binding until a new one succeeds.
- **Server switch mid-flight**: changing the server while pushes are arriving must not deliver duplicates or leave the old device key silently active on the abandoned server.
- **Sign-in and account-blocking risk**: the client must warn at setup that it uses an unofficial registration path, and it must degrade with a clear explanation if the account or registration is refused.

## Requirements *(mandatory)*

### Functional Requirements

**Setup and identity**

- **FR-001**: The client MUST guide a first-time user through a setup flow that results in a working push-receiving device identity, a Bark device key, and the server address to send to.
- **FR-002**: The client MUST obtain its push-receiving identity by registering itself as an emulated Apple device through the mandated open-source registration library (pypush), of the Apple Push Notification service, because no official desktop registration path exists.
- **FR-003**: The client MUST obtain its push identity without an Apple account, password, or two-factor code, and if the activation service demands interactive authorization or rejects the request, MUST surface an actionable error and retry with backoff rather than looping.
- **FR-004**: The client MUST store all credential material locally only, protected so that another user account on the same machine cannot read it in plain form, and MUST provide a documented way to remove it completely.
- **FR-005**: After setup the client MUST display the resulting device key, the server address, and a scannable QR code, each with a one-click copy action.
- **FR-006**: Users MUST be able to trigger a self-test notification from the client and see a pass/fail result with the reason for failure.
- **FR-007**: Users MUST be able to re-run registration or replace their device identity from settings, and the client MUST NOT leave orphaned identities behind when doing so.
- **FR-030**: The client MUST complete device-key issuance by registering its push identity with the configured Bark server through that server's device-registration API, and MUST treat the server as the sole delivery path for pushes addressed to that device key.
- **FR-031**: If the configured server refuses registration or its registration API is unavailable, the client MUST report the specific failure, keep any previous working link usable, and offer retry or a different server address.

**Receiving**

- **FR-008**: The client MUST run as a per-user background process after the window is closed, remaining available to receive notifications until the user explicitly quits or signs out.
- **FR-034**: The client MUST NOT require machine-wide installation or elevated privileges, MUST keep all state inside its own folder and usable only by the signed-in user, and MUST stop receiving notifications when that user signs out.
- **FR-009**: The client MUST receive text notifications pushed by any Bark-compatible sender to its device key and present the sender's title, body, group, and target link.
- **FR-010**: The client MUST display each received notification as a native Windows notification while the client is running in the background.
- **FR-011**: The client MUST automatically reconnect to the push infrastructure after network loss, sleep/resume, or credential refresh, without user intervention.
- **FR-012**: The client MUST recognize delete/recall pushes and remove the affected notification from history.
- **FR-033**: The client MUST store only notifications received while it is connected and MUST NOT attempt to backfill, import, or synthesize messages from a period when it was not running or not connected.
- **FR-013**: The client MUST ignore push fields it does not support without failing to deliver the notification.

**Images**

- **FR-014**: The client MUST display an image referenced by a received notification (custom icon/image URL or markdown image) as a thumbnail in the notification and in history.
- **FR-015**: Users MUST be able to open an image full-size and to save it to disk or copy it to the clipboard.
- **FR-016**: The client MUST deliver the text of a notification even when its image cannot be fetched or decoded, and MUST indicate that the image is unavailable.
- **FR-017**: The client MUST cache fetched images locally so repeated pushes of the same image URL render without re-downloading, and MUST bound the cache size.

**History and tray**

- **FR-018**: The client MUST persist a local notification history that survives restarts, capped and expiring per the configured retention limits, newest-first.
- **FR-019**: The system tray icon MUST reflect unread count and connection state, and MUST offer quick actions: open history, mark all read, test notification, copy device key, open settings, and quit.
- **FR-020**: Users MUST be able to read, delete individual entries, clear the whole history, and mark read/unread.
- **FR-021**: Users MUST be able to enable or disable starting the client automatically when they sign in to Windows, using a per-user autostart entry configured from the tray or settings, and this preference MUST survive restarts.
- **FR-036**: The client MUST run as a self-contained portable build: no installer, no administrator rights, no separately installed runtime, and no machine-wide records beyond an optional per-user autostart entry.
- **FR-037**: If the portable folder is moved or renamed so the autostart entry no longer resolves, the client MUST detect the stale entry at next launch and repair or disable it with a single user confirmation.
- **FR-038**: "Uninstall" MUST be equivalent to quitting the client and deleting its folder, with the option to also delete user data; the client MUST remove its autostart entry on request.
- **FR-022**: The client MUST enforce a single running instance per user, focusing the existing instance instead of starting a second one.
- **FR-023**: Users MUST be able to silence desktop notifications temporarily (for example, a do-not-disturb period) while still recording notifications in history.

**Configuration and diagnostics**

- **FR-024**: Users MUST be able to choose between the public Bark server and a self-hosted/compatible server address, and the chosen server MUST be the one that issues the device key used by senders.
- **FR-032**: Switching servers MUST either re-issue the device key on the new server or revoke the old key before the old server is abandoned, and the client MUST make clear which server address the displayed key belongs to.
- **FR-025**: The client MUST provide a settings surface for history retention, image caching, notification behavior, and connection details.
- **FR-026**: The client MUST keep a local diagnostic log (rotated, size-limited) that records connection and registration events without recording notification bodies or credentials, and MUST let the user export it for support.
- **FR-027**: The client MUST clearly communicate connection status, last successful delivery time, and any required user action, so a user can distinguish "nothing was sent" from "I was offline and may have missed notifications".
- **FR-035**: Users MUST NOT be required to grant administrator rights; credentials, history, caches, and settings MUST stay within the user's own writable app folder, and credentials MUST be protected so that another Windows user cannot read them in plain form even if they can open that folder.

**Scope guardrails**

- **FR-028**: The client MUST be structured so that only platform-specific integration surfaces (tray, notifications, start-at-sign-in, credential storage) differ per operating system, so that macOS and Linux can be added later without re-architecting the receive, history, or image behavior.
- **FR-029**: Sending notifications from arbitrary sources, iCloud features, messaging, and encrypted (ciphertext) pushes are explicitly out of scope for this version.

### Key Entities *(include if data involved)*

- **Device Identity**: The anonymously activated emulated Apple device used to receive pushes. Holds device identifiers, the activation device certificate and private key, the assigned base push token, and lifecycle state (unregistered, registering, active, invalid). Exactly one is active at a time.
- **Bark Link**: The pairing between this device and a Bark server, established through the server's own device-registration API. Holds server address, the server-issued device key, key lifecycle state (issued, active, revoked), and the shareable link/QR payload. One active link per app folder; re-registration or a server change replaces it.
- **Notification**: A received message. Holds title, body, group, target link, level/priority, sender-provided timestamp, received timestamp, read state, and reference to an image (if any).
- **Notification Image**: A cached image associated with a notification. Holds source URL, local cache location, fetch state, and last-used time for eviction.
- **Settings**: User preferences. Holds retention limits, image cache limits, notification behavior, start-at-sign-in choice, active server, and silence periods.
- **Diagnostic Log**: Rotation-limited local event record with no message content and no credentials.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A new user completes setup and receives their first notification on the desktop in under 5 minutes, with no external tooling required beyond the sender.
- **SC-002**: At least 95% of notifications accepted by the push infrastructure are shown on the desktop within 10 seconds while the client is running and the machine is awake.
- **SC-003**: Notifications remain deliverable for at least 7 consecutive days of uninterrupted background operation without requiring a restart.
- **SC-004**: Closing the client window never stops delivery: 100% of notifications pushed after window close are still delivered while the process runs.
- **SC-005**: Image-carrying notifications display their thumbnail within 3 seconds when the image host responds in under 1 second, and unreachable images never prevent their text from being delivered (100% of cases).
- **SC-006**: Idle resource use stays under 1% average CPU and under 150 MB of memory over a 24-hour run.
- **SC-007**: The client recovers automatically from network interruption and machine sleep, resuming delivery within 60 seconds of connectivity returning, in at least 95% of interruptions.
- **SC-008**: History remains complete and correctly ordered across restarts: 100% of stored notifications are present and readable after restart in testing.
- **SC-009**: Users can perform the four most common tray actions (open history, copy device key, test notification, quit) with at most two interactions each.
- **SC-010**: Fewer than 1 in 20 delivered notifications produces a user-visible error or requires the user to intervene.

- **SC-011**: A user can go from downloading the package to receiving a notification with zero installation steps and no administrator prompt, and can completely remove the client by deleting one folder.
- **SC-012**: A deliberately moved portable folder still autostarts or prompts to repair within one launch, in at least 95% of attempts.

## Assumptions

- **Distribution**: v1 ships as a portable, self-contained folder/archive containing the runtime and dependencies; there is no installer, no auto-update mechanism, and no system-wide registration. Autostart is optional and configured by the user.
- **Portable data placement**: All state lives in a subfolder of the portable app folder, so the app can be moved or deleted as a unit. Because a portable folder may be copied or shared, credentials are additionally protected by a per-user secret so that another Windows account cannot read them.
- **Platform scope**: Windows 10 and Windows 11 (x64 and ARM64) are the v1 target; macOS and Linux are future versions and only require that the architecture keeps platform integration separable.
- **Automation and notifications**: Bark users already send notifications through the existing Bark ecosystem (iPhone app, API, scripts, monitoring tools). The desktop client is a receiver, not a sender of arbitrary messages.
- **Account requirement**: No Apple account is required. Push reception relies on anonymous device activation, which yields a device certificate and a push token without credentials. The client never asks for or stores an Apple ID or password.
- **Activation credential reuse**: The activation certificate, private key, device identifiers, and assigned push token are cached locally and reused across restarts and reconnects, so the activation service is contacted only when no valid cached activation exists.
- **Registration mechanism**: Per the feature request, device registration is performed with the open-source `pypush` library emulating an Apple device. This is an unofficial path; it is treated as a replaceable dependency behind a single boundary, with no guarantee of permanent availability.
- **Image semantics**: Bark transports images by URL only, never as binary attachments. "Image support" therefore means rendering and caching images referenced by a notification, not accepting uploaded files.
- **Default retention**: History keeps 500 notifications or 30 days, whichever is reached first; the image cache is capped at 200 MB. Both are user-adjustable.
- **Default notification behavior**: Native Windows notifications are shown for every received message unless the user silences them; toasts may be coalesced when a burst arrives faster than the OS can display.
- **Single user, single device**: One identity and one history per Windows user profile; no synchronization of history between machines or across Windows user accounts.
- **Session model**: The client is a per-user application, not a Windows service. Notifications are only collected while that user is signed in and the client is running; no component runs at the boot screen or during a locked, signed-out state.
- **No administrator rights**: Operation is per-user, with no elevation, no machine-wide service registration, and no system-wide changes; the only machine-visible side effect is an optional per-user autostart entry.
- **No offline backfill**: Recovery of messages sent while the client was not connected is a non-goal for v1 (by decision). "Delivery" therefore means live delivery to a running client; the spec does not promise an unbroken notification record across downtime.
- **Connectivity**: The client assumes a working internet connection and access to the Bark server and Apple push endpoints; constrained corporate networks may block it.
- **Local storage**: The client stores its data inside its own portable folder; the folder must be writable by the signed-in user, on a local disk (not a read-only or network share), with at least 500 MB free.
- **Security posture**: Credentials are only ever used to receive the user's own notifications; the client never transmits them anywhere except to the services they belong to, and notification content is never sent off the machine.
- **Excluded for v1**: encrypted (ciphertext) Bark pushes, rich interactive notification actions, notification replies, multi-device sync, offline message backfill from server history, and macOS/Linux builds.

## Dependencies

- Apple Push Notification service availability and acceptance of emulated-device registration.
- The `pypush` project as the mandated registration mechanism ([pypush](https://github.com/JJTech0130/pypush)).
- A Bark-compatible server (public `Finb/bark-server` deployment or self-hosted) that will accept the client's device key.
- Native Windows integration surfaces for tray icons, notifications, and secure local credential storage.
- A self-contained portable build of the application and its dependencies, so no system-wide runtime installation is required.

## Risks and Constraints

- **Policy risk**: Anonymous device activation and acting as an Apple push client are outside Apple's supported usage. The client must warn users at setup, must not spam the activation service (reuse cached activations, rate-limit retries), and must not claim official Apple support. Failure modes must degrade to a clear explanation rather than retry loops.
- **Upstream instability**: The registration library is explicitly unstable. Registration behavior must be isolated so it can be updated or replaced without touching receive, history, or UI logic.
- **Credential sensitivity**: Stored credentials grant access to receiving the user's pushes; leakage is a privacy incident. Protect at rest, never log, and provide a full local wipe.
- **Notification privacy on a shared machine**: Desktop toasts may reveal content on a shared or projected screen; the user must be able to hide preview text from notifications.
- **Language and dependency constraints**: The client is implemented in Python and must consume `pypush` as a dependency rather than vendoring or reimplementing it. The GUI toolkit must be chosen for a small memory footprint (native-controls or similarly lightweight, no bundled browser engine), because idle memory is a first-class goal; any toolkit that cannot meet the idle-memory target is rejected.
- **Long-running Python process constraints**: Because the app runs for days in the background, the design must avoid accumulating memory, must not depend on a foreground console window, and must survive toolkit event-loop quirks during sleep/resume and network changes.
- **Portable packaging constraints**: A portable build means the user's data location must be discoverable at runtime rather than assumed from an install path, and the autostart entry must be maintainable when the folder moves. Shipping an installer or an auto-updater is rejected for v1.
- **No service component**: Installing a Windows service is rejected for v1; a service would need a separate per-session agent to show tray UI and toasts, and would collect notifications for a user who is not present.
