# Changelog

## 0.1.55
The add-on can join Darak's own tailnet server (tailnet.darak.ps) when its enrollment bundle names one; older bundles keep working as before.

## 0.1.54
Darak now checks every automation against your Home Assistant before adding it, so it can't add one that would fail.

## 0.1.53
Automation suggestions now use patterns found across the whole history: repeated sequences, daily routines, motion and presence.

## 0.1.52
Automation suggestions work on busy homes: activity is read in small windows and constantly flapping devices are ignored.

## 0.1.51
Darak can suggest automations from your home's activity and add or remove the ones you approve; it only touches automations it created.

## 0.1.50
- Home Assistant full backups no longer include the Tailscale node state, the signing key or the Darak and household managed-user password files; after restoring such a backup, enroll the hub with Darak again
- The logs command only serves this add-on's and the SSH add-ons' logs, and continuing a Home Assistant config or options flow is refused unless the flow belongs to an integration the agent may start

## 0.1.49
- Bundles darak_voice integration 1.18.0 with robot vacuum cleaning previews that name the selected rooms and cleaning modes

## 0.1.48
- Bundles darak_voice integration 1.17.0: the agent accepts the smart and smart_deep vacuum job types (Smart clean and deep smart clean on Dreame robots)

## 0.1.47
- Technician sign-in through remote access works again (0.1.46 made the technician account local-only, which Home Assistant refuses over remote access)

## 0.1.46
- Technician access ends even if the agent stops: the support session is revoked on shutdown, and the technician account can only sign in from the hub itself
- Support sessions: SSH can no longer open silently in allow mode, an approved request is refused if you switched to block meanwhile, switching to block ends a live session within about 30 seconds, and ask mode only installs the SSH key you approved

## 0.1.45
- Bundles darak_voice integration 1.16.4: voice no longer forwards free text to Home Assistant's own agent; garage, gate and door covers need the per-device security voice switch, and cannot be scheduled
- Blocked support mode now also refuses remote configuration revert

## 0.1.44
- Bundles darak_voice integration 1.16.3: a mop water level can be set when the robot was not already mopping

## 0.1.43
- Bundles darak_voice integration 1.16.2: cleaning modes use the Dreame robot-wide settings, not one room's

## 0.1.42
- Bundles darak_voice integration 1.16.1: fixes the integration failing to load (cleaning-modes websocket command schema)

## 0.1.41
- Bundles darak_voice integration 1.16.0: voice cleaning modes (suction, mop/vacuum, water level) and the cleaning-modes editor.

## 0.1.40
- Allow the vacuum segment read and vacuum room link registry update shapes through the Home Assistant WebSocket bridge.

## 0.1.39
- Bundle Darak Voice integration 1.15.0: voice room cleaning tells you when the vacuum is busy, out of water or reporting a fault instead of saying it started.

## 0.1.38
- Bundle Darak Voice integration 1.14.0: vacuum room cleaning by HA area (`vacuum.clean_area`), "clean here" using the speaking device's area, and a check that the vacuum actually started.

## 0.1.37
- Allow group helper config and options flows through the local HA proxy; restrict config entry deletion to Darak Voice and group entries.

## 0.1.36
- Bundle Darak Voice integration 1.12.0 (delayed voice commands).

## 0.1.35
- Bundle Darak Voice integration 1.11.2, which sends device capabilities (supported_features) so vacuums, fan speed, AC fan/swing, water heaters and valves can be controlled by voice.

## 0.1.34
- Bundle Darak Voice integration 1.11.1, which fixes the Try a phrase action failing on every call.

## 0.1.33
- Bundle Darak Voice integration 1.11.0 with the admin-only `darak_voice.try_phrase` preview, and allow exactly that call.
- Voice device registry updates accept vacuum, water heater, humidifier, valve, button and lock kinds.
- Voice registry writes: report Home Assistant's error code and message (bounded) instead of a generic upstream error.
- Voice device rooms: allow setting a device's area (config/device_registry/update, device_id and area_id only), needed because Home Assistant 2026.10 refuses own areas on entities without their own name.
- Voice rooms: allow setting a room's spoken names (config/area_registry/update, area_id and aliases only).

## 0.1.32
- Voice devices: read spoken names with Home Assistant's full registry entries, because Home Assistant 2026.10 no longer includes aliases in the registry list.

## 0.1.31
- Voice devices: spoken names and the voice type are written in two Home Assistant registry updates, because Home Assistant drops aliases sent together with options.

## 0.1.30
- Bundle Darak Voice integration 1.9.4: the voice catalog carries each device's owner-confirmed voice type, and new voice-devices / voice-device-set commands set spoken names, rooms and types in Home Assistant.

## 0.1.29
- Bundle Darak Voice integration 1.9.3: voice timers and alarms that ring the phone that asked.

## 0.1.28
- Bundle Darak Voice integration 1.9.2: the voice catalog carries each entity's device name, so turning a room's lights on can reach only the room's own light switch.

## 0.1.27
- Bundle `darak_oauth` 2.0.1: Google Calendar is reloaded when Home Assistant was still setting it up at boot, and the stale "re-authentication required" notice is cleared once it loads.

## 0.1.26
- Bundle the Darak Google Calendar integration (`darak_oauth`) and add the `oauth_install`, `oauth_configure` and `oauth_remove` commands. The home holds only the Google client id and a per-home Darak key; the Google client secret never reaches the device.

## 0.1.25
- Refuse support-sensitive commands in block mode; deny garage covers, allow `ha_rest` to start only the Darak Voice config-entry flow, and exclude local add-ons from backup restores.
- Trust only the Supervisor ingress proxy, persist replay IDs, and bound forwarding connections and shutdown.

## 0.1.24
- Ending SSH support removes the technician's key and restarts the SSH add-on before the forwarder stops, and never waits on a connected client.
- In ask mode SSH needs an approval that granted it, cannot outlast that approval, and the owner is told when SSH is on.
- Darak-managed Home Assistant users can no longer approve support sessions.

## 0.1.23
- Bundle Darak Voice integration 1.9.1: play the success chime with the silent voice.

## 0.1.22
- Bundle Darak Voice integration 1.9.0: concurrent device commands within one voice turn.

## 0.1.21
- Bundle Darak Voice integration 1.8.5 with illumination sensor catalog support.

## 0.1.20

- Bundle Darak Voice integration 1.8.4 with registry-backed catalog metadata.

## 0.1.19

- Bundle Darak Voice integration 1.8.3 with catalog temperature support.

## 0.1.18

- Report installed and bundled Darak Voice integration versions.
- Bundle integration 1.8.2 with dimmability metadata and brightness steps.

## 0.1.16

- Bundle Darak Voice integration 1.8.1 with corrected to-do writes.

## 0.1.15

- Allow hub-managed Assist exposure for approved entities while rejecting sensitive domains and garage/gate IDs.
- Include local voice capture and audio frontend status in voice checks.

## 0.1.14

- Return bounded Darak Voice capability data to the hub for local settings.
- Bundle Darak Voice integration 1.8.0 with deterministic Arabic home skills.

## 0.1.13

- Preserve managed Home Assistant user sign-in secrets for SSO without owner-only password changes.

## 0.1.12

- Read Home Assistant login names from the user record and block Darak accounts from approving support requests.

## 0.1.11

- Provide narrow managed Home Assistant user lifecycle and SSO commands for local homes.

## 0.1.10

- Retry the Home Assistant state check once when it times out.

## 0.1.9

- Support requests and sessions also notify your phones through the Home Assistant app.

## 0.1.8

- Darak can tell when Home Assistant is stopped, instead of reporting the add-on as offline.

## 0.1.7

- Darak can read your device list to let you choose which devices Alexa can see.
- You decide how Darak support sessions start: allow (default, with a notification), ask (approve each one in the new Darak support page), or block.
- Home Assistant shows a notification when a support session starts and ends.

## 0.1.6

- Remote access works on Home Assistant 2026.9 and later.
- Darak Voice, Alexa and MCP can repair Darak's support login without the owner account.
- Darak Voice installs and removes cleanly on HAOS.

## 0.1.5

- Health reports whether Home Assistant is running yet.
- Backups made by Darak leave out the Darak Agent add-on: smaller archives, and the agent's keys never leave the device in them.

## 0.1.4

- Enable remote access on homes that never saved HTTP settings in Home Assistant.

## 0.1.3

- Poll Supervisor backup jobs by their undashed id, so hub backups complete.
- Read the health endpoints in parallel (about 1 s instead of 3 s).
- Report host uptime from the boot timestamp.
- Report the add-on version as the agent version.

## 0.1.2

- Tolerate Tailscale status before login (no addresses yet).
- Clear the enrollment bundle only after joining the tailnet, so a failed start can be retried.

## 0.1.1

- Build from the pinned Python base; Supervisor ignores build.yaml.
- Run under with-contenv so the agent keeps its environment.
- Say how to enroll when started without an enrollment bundle.

## 0.1.0

- Initial local Home Assistant add-on.
## 0.1.17

- Allow validated Home Assistant conversation/process commands through the voice WebSocket bridge.
