# OTool workbench interaction contract

The business brief is the user's device-card request dated 2026-11-11. README.md documents the maintained product behavior. Existing irreversible eFuse safeguards in src/efuse_batch_dialog.py remain authoritative for that workflow; changing card preferences does not bypass them.

## Canonical UI Map

| Capability | Canonical owner | Source of truth | Allowed variants | Verification |
|---|---|---|---|---|
| Device menu | DeviceCard + OtoolEsptoolUI._create_device_menu | User brief, DESIGN.md | Button click or name/icon context menu | tests/test_device_policy_ui.py; native Qt popup inspection |
| Select/Listbox | Existing QComboBox and QMenu controls | Existing desktop UI, DESIGN.md | Qt-owned popup geometry and keyboard model | Native desktop keyboard/popup checks |
| Form | Existing FlashEntryRow + validate_flash_entries | README.md, src/esptool_commands.py | Single, global and per-port automatic burn share validation | tests/test_idf_build_ui.py, tests/test_main_review.py |
| Scrollbar | BASE_STYLESHEET and OtoolEsptoolUI._apply_style | DESIGN.md | Existing per-log geometry | Desktop screenshot inspection |
| Feedback | Existing status_label, card result/log, QMessageBox | README.md, DESIGN.md | Inline status for preferences; existing dialogs for input/I/O failure | tests/test_device_policy_ui.py |
| Device exclusions | DevicePolicyStore + PortOwnershipRegistry | User brief | By port or by USB identity | tests/test_device_policy_ui.py |
| Automatic completion | AutoFlashCycle + OtoolEsptoolUI removal monitors | User request dated 2026-11-11, README.md | Global and per-port automatic modes | tests/test_auto_flash_removal.py |
| Recent firmware sources | FirmwareHistoryStore + FlashEntryRow.browse_menu | User supplement dated 2026-11-11, README.md | bin files and build directories share one 16-item MRU | tests/test_firmware_history.py |
| Device burn history | FlashHistoryStore + FlashHistoryPanel + HistoryDeviceCard | User history request dated 2026-11-11, DESIGN.md | MAC groups, search and expandable attempts in the burn-page sidebar | tests/test_flash_history_ui.py; desktop screenshots |
| Phase duration | FlashRunTimer + bootstrap.instrument_esptool_phases + PhaseDurationBar | Actual esptool loader calls | Same recording for manual, global and per-port automatic flash attempts | tests/test_flash_history_ui.py |
| Release update | NewReleaseButton + ReleaseUpdateDialog + apply_update.ps1 | GitHub Releases and current user request | Windows standalone EXE can replace itself. Source runs link to the release page. | tests/test_app_update.py and Windows EXE replacement |

This is a native PyQt6 application, with no web router, browser forms, web date picker, server paging, or remote CRUD lifecycle in the touched workflow. Native Qt accessibility and keyboard controls apply.

## Release update flow

Check the official GitHub repository once the workbench is visible. A higher stable release reveals “NEW!” beside the current version. “以后再说” closes the dialog and leaves the same release available for the session. “开始更新” downloads and validates the release asset before requesting the existing safe shutdown. Active work or serial connections keep the application open and show the reason. A separate PowerShell helper waits for the application and its bootloader to exit, replaces the executable and reopens the selected workbench. Only a Windows access-denied result triggers UAC for file replacement. The helper keeps the previous executable as a backup and attempts to restore it if the new process exits before the workbench is ready.

## Device preference flow

| Operation | Trigger | Pending | Success | Failure / recovery |
|---|---|---|---|---|
| Open menu | Click the three-line button, right-click name/button, or keyboard menu navigation | None | Qt popup; first submenu 屏蔽, second checkbox 自动烧录 | Escape closes and restores focus |
| Block / unblock | Toggle the matching port/device checkbox | Atomic local file write | BLOCK ribbon, disabled card actions, shared task exclusion; removing all matching rules restores actions | Failed write leaves prior rules unchanged and displays an actionable message |
| Arm a port | Check 自动烧录 | Cancellable 100ms trigger using current firmware settings | AUTO ribbon; only this COM port participates independently of global auto mode; refresh is enabled | Invalid input uses normal validation; cancellation/block/stop revokes pending triggers |
| Reconnect on an armed port | A refresh sees a new card at that COM port | Same validated trigger as global mode | Same independent worker and progress/result flow as manual flashing | Detection defaults to 3 seconds; failed automatic attempts wait 10 seconds and retry; imported chip locks remain enforced |

## State and identity rules

Block rules persist in the user's local configuration directory. A port rule matches that COM name regardless of adapter. A device rule follows USB serial + VID/PID across COM changes; without a serial it uses VID/PID + physical connection location; without either unique identity it is explicitly limited to the current port. No rule is inferred from a generic model name alone.

Blocked cards remain listed for recovery. Polling may observe presence/absence but does not refresh their metadata or submit work. Manual actions, global/per-port automatic flash, flash/erase-all, verification and eFuse batch queues, and terminal reconnect respect the shared exclusions. Running serial work must finish or stop before a matching rule can be changed; irreversible eFuse work is never force-killed by blocking.

AUTO arming lasts only for the current process and binds to a COM port. Explicitly checking it schedules an attempt for the current card, then subsequent new connections at that port. Existing global auto behavior continues to discover new cards. Ordinary refresh does not repeatedly burn a successfully completed card. Failed automatic attempts release their worker/port claim and wait 10 seconds before retrying with the original command and target lock. The card shows the wait and retains a Stop button. Only the connection/chip detection call has a 3-second deadline; erase/write/verify are outside it. Disarm, block, removal and Stop revoke pending retries and stale callbacks. Manual tasks do not enter this repeating loop. BLOCK overrides AUTO. Disarming a port cancels its pending trigger; global disarming leaves explicitly armed ports in effect. “全部停止” cancels all pending triggers and running reversible flash work through the existing stop path.

If the exclusion file is corrupt/unreadable, serial work fails closed with a visible explanation and configuration path. A failed save never makes the UI claim a rule was committed.

## Successful automatic flash cycle

Only a normal successful automatic flash enters the removal-wait phase. Each completed station has a cancellable 3-second timer. Enumeration locates the adapter, then a registry-owned esptool read-mac worker opens it and reads actual chip information. A surviving USB adapter alone cannot prove chip presence. Matching MAC means the completed chip remains present; a different MAC identifies its replacement. Two consecutive no-response results confirm removal when the adapter remains online; a missing adapter also confirms removal. Busy ports, process watchdog expiry and unexpected errors do not count as no-response results. No probes overlap, blocked devices are excluded, and explicit manual tasks cancel the background probe before claiming the port. Refreshing, rearming or clearing cards cannot bypass the pending removal requirement. Other stations remain independent.

Keep the completed card and detection log after removal, including when auto-refresh sees the disconnection first. Once ready, continue probing until a chip MAC is read. Its connection reuses the station card/log and dispatches one normal validated automatic attempt. A read-mac probe uses the established reset-to-ROM connection path and resets the chip afterward; it does not erase or write firmware. Cancelling automatic mode, blocking, Stop or closing the application stops the monitor; rearming resumes the pending removal check. Manual flashing does not enter this loop.

## Device history flow

“显示历史” is unchecked on a fresh installation. Toggling it persists ui_preferences.json atomically; save failure retains the previous choice and reports the error. Closing the sidebar is equivalent to unchecking the button. Other tabs do not show the burn history sidebar. Search and expanded groups are session-local view state. The lower-right checkable “显示全部” button is unchecked at each launch: incomplete attempts without both a chip model and valid MAC are hidden, remain stored, and reappear when checked. Toggling this filter retains search text and expanded groups, resets the visible page limit, and recomputes visible counts and averages. An all-hidden empty state explains how to reveal the records. New records and periodic refreshes retain the selected filter for the current session.

Every dispatched flash attempt, including retries, receives its own start timestamp, monotonic total, result and phase spans. Parse identity only from the current attempt's output so a prior device's MAC is never inherited. Running records update from actual worker events and the visible sidebar updates once a second. Completed attempts preserve their result; interrupted processes recovered after restart are labeled interrupted. Write flash_history.json atomically beside device_policies.json, retaining the newest 1000 attempts. MAC groups survive COM changes. Unknown-MAC attempts remain separate. A corrupt or unreadable file shows an inline explanation and is not overwritten; new records remain in memory until storage is repaired.

Duration covers preparation, connection, erase, write, verification, reset and finish calls actually observed in the worker. Aggregate repeated phase spans for proportional segments and per-phase totals. Waiting for the operator to unplug and the ten-second retry delay are excluded. Each retry is a separate attempt. Normal Python, package and launcher entry points use the same full application; there is no production demo mode or generated sample history. UI verification uses test-only fixtures, mocks or synthetic worker output and does not flash attached boards.

## Recent firmware source flow

Valid file selections, path edits, successful build imports and actual flash attempts update the shared source history. The most recent source is first, duplicate canonical paths are moved rather than copied, and bin/build entries share a maximum of 16. Persist atomically beside device_policies.json as firmware_history.json. A failed write preserves the previous record and reports that the current selection is still valid.

The separator is always below the two established picker commands. Empty history shows a disabled “暂无使用记录”. Full canonical paths are retained in action data and tooltips, while only the display label is shortened. A missing bin or build manifest disables that action. Selecting a bin restores its path and filename-derived address; selecting a build uses the existing atomic import flow and rereads its current manifest, with no implicit flashing.

## Accessibility, locality, and verification

Names use plain text, a full tooltip, and a full accessible name. The icon is a native focusable button with a Chinese accessible label. Native QMenu/QAction provide checked state, arrow-key submenu access, Escape and viewport collision behavior. Disabled actions cannot dispatch work. Ribbon text provides a non-color state cue.

tests/test_device_policy_ui.py covers persistent recovery, changed COM identity, port replacement, failed writes, exclusions, menu state, and canceled automatic work without hardware. Existing integration tests cover the real esptool worker with offline firmware operations. Desktop UI review must inspect default/AUTO/BLOCK cards, opened submenus, long names and a narrow window. Hardware erase/write/eFuse commands are not triggered during UI verification.
