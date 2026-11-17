---
version: alpha
name: OTool Esptool UI
description: A compact Windows workbench for identifying and programming ESP devices.
colors:
  primary: "#2560e0"
  background: "#f0f2f5"
  surface: "#ffffff"
  text: "#1a2333"
  auto: "#7c3aed"
  blocked: "#6b7280"
  on-status: "#ffffff"
  release-fill: "#ffe4f0"
  release-text: "#89204d"
typography:
  sans:
    fontFamily: "Microsoft YaHei UI, Segoe UI, sans-serif"
    fontSize: "13px"
  mono:
    fontFamily: "Consolas, Menlo, DejaVu Sans Mono, monospace"
rounded:
  DEFAULT: "7px"
  card: "12px"
  badge: "4px"
spacing:
  card-inset: "14px"
  card-gap: "8px"
components:
  device-card: {}
  device-menu: {}
  corner-ribbon: {}
  progress-bar: {}
---

# OTool Esptool UI Design System

## Overview

This is a product tool for engineers repeatedly connecting, identifying, and programming ESP boards at a Windows desktop. The existing reference is a bench instrument: compact metadata, separate COM badges, blue action buttons, and per-device logs. Evidence comes from README.md, src/styles.py, src/device_card.py, and the user's device-card screenshots and requirements of 2026-11-11.

The UI language is Simplified Chinese with established technical identifiers such as COM, eFuse, AUTO and BLOCK. No new market, Japanese locale, dark theme, branding, or navigation redesign is introduced. Dense cards must remain easy to compare. Avoid marketing treatments, oversized whitespace, or changing control positions when a status changes.

Runtime styling remains canonical (mapping model B); this document records accepted values. src/styles.py owns shared backgrounds, text and buttons. OtoolEsptoolUI._apply_style in src/main_window.py owns workbench/card/menu states. CornerRibbon in src/device_card.py owns the AUTO/BLOCK colors, diagonal geometry, and text. DeviceCard is the shared card component; new features extend it rather than creating an alternate card.

## Colors

Primary blue identifies actions and the device-menu icon. Neutral gray supports the workbench and card borders. A purple AUTO ribbon and gray BLOCK ribbon use white text; the words communicate the state in addition to color. BLOCK takes visual and behavioral precedence over AUTO. A blocked card uses a gray border and disabled action controls while its menu remains available for recovery.

## Typography

Use the existing Chinese-capable desktop sans stack. Device titles are 14px and bold; technical logs keep the existing monospace stack. Full names are available as tooltips and accessible names. Remove the redundant trailing COM number from the name, retaining the separate COM badge.

## Layout

Cards retain their 380px base width, 14px horizontal padding, 8px internal gaps, and existing scalable log height. The title consumes remaining space between a fixed menu button and fixed NEW/COM badges. Truncate only when needed: retain the last model word, use exactly six asterisks, and fit as much prefix as the measured font allows. For an unusually long final word retain its rightmost characters.

Workbenches continue to use the main content scroll area and existing FlowLayout. A ribbon is an overlay in the lower left corner; it must not move the log, controls, footer, or other cards.

## Elevation & Depth

Card hierarchy uses borders and pale state surfaces. Menus use native Qt popup layering and collision handling. Do not add card shadows or animated decoration.

## Shapes

Keep 12px card corners, 7px buttons, and 4px badges. The corner ribbon is deliberately diagonal, matching the position requested in the second user screenshot.

## Components

- DeviceTitleLabel owns measured name omission, full tooltip, and accessible name; ordinary metadata continues to use ElidedLabel.
- DeviceCard.menu_button is a keyboard-operable QToolButton with a three-line SVG icon, hover/pressed/focus states, accessible name, and tooltip. Both it and the name support the same context menu.
- QMenu owns keyboard navigation, checked states, submenu placement to the right when space permits, Escape, and focus restoration. The first item is 屏蔽 with 按端口屏蔽 / 按设备屏蔽; the second is checkable 自动烧录.
- CornerRibbon owns purple AUTO and gray BLOCK, both with bold white text. The overlay does not intercept mouse input.
- Existing action buttons, QProgressBar, logs, status labels, native file dialogs and error messages keep their established owners.
- Global scrollbars remain owned by the application stylesheet; component-specific log geometry is an existing exception.

## Do's and Don'ts

- Keep a blocked card visible with its menu usable so the rule can be removed.
- Enforce exclusions in the shared policy and task entry points as well as disabled buttons.
- Cancel stale automatic callbacks when a card disappears, is blocked, or is stopped.
- Do not persist AUTO arming across application restarts.
- Do not use a generic device description or VID/PID alone to identify every adapter of that model as one device.

## Automatic flash failure feedback

The existing card status and progress text show “失败，10 秒后重试” during an automatic retry wait. Keep the flash action in Stop mode so users can cancel without waiting for another connection attempt. Preserve the prior failure log. On disarm, restore idle action buttons and show that the retry was canceled. Successful devices stay complete; unchanged polling does not flash them again.

## Automatic flash completion and recent sources

The completed card retains its success tone and 100% progress while showing the compact status “成功，待移除” and progress text “烧录完成，请拔出设备”. A chip-information probe runs on a 3-second timer and reads the actual chip MAC; an enumerated USB adapter is not evidence that its attached chip is present. Two consecutive no-response results confirm chip removal. Port contention or unexpected tool errors leave the state unchanged. Split operator messages into short readable log lines within the existing fixed-width card. Confirmed removal changes the status to “就绪，待接入” and progress text to “已就绪，等待下一台设备接入”; keep that card and its log visible. Continue probing for the next chip and reuse the station's card/log rather than adding an unbounded list of completed cards.

FlashEntryRow extends its existing QMenu: the two picker commands stay first, followed by a real separator and up to 16 recent sources in most-recent-use order. Prefix each source with bin or build. Bound labels to 56 characters and 420 logical pixels; use six middle asterisks and preserve the parent directory and filename/model tail when possible. Full paths remain in tooltips. Missing files/build manifests are disabled with an explanation. Reuse Qt popup collision, keyboard, focus and checked-state behavior rather than adding a custom overlay.

## Device history and phase timing

The checkable “显示历史” button follows “全部擦除” and precedes “全部停止”. It is hidden by default and remembers the user's last choice. The burn page owns a horizontal splitter: the existing device flow remains on the left, with a 350–420px white history sidebar on the right. Other workbenches keep their existing layouts. The sidebar owns its scroll area; it must not compress cards vertically or make expanded content overlap neighboring records.

FlashHistoryPanel and HistoryDeviceCard in src/flash_history_widget.py are the canonical history components. Group by normalized chip MAC, with the latest attempt first and separate unrecognized attempts when no MAC was read. Hide attempts without an identified chip model and valid MAC by default. A checkable “显示全部” button at the sidebar's lower right includes them; this is session-local view state and never deletes or changes saved records. Report the hidden attempt count beside the button. Search and summary counts follow the current filter. Show 40 device groups initially with explicit load more, within the local store's 1000-attempt cap. Empty, no-match and file-error states remain within the same sidebar. Corrupt history stays untouched and new records remain in memory with a visible save explanation.

Collapsed history cards use four compact rows: chip model followed immediately by “-” and the complete, selectable MAC with the result badge at the far right; start time, port, attempt count and elapsed seconds; proportional duration bar; firmware tooltip and detail toggle. Keep 10px horizontal insets, 8px top / 7px bottom insets, 4px internal gaps and 6px between cards. MAC uses 11px Consolas and is never truncated or moved to a separate row. Expanded details retain the per-attempt timeline and three-column phase figures without overlapping adjacent cards.

PHASES in src/flash_timing.py owns the phase order, Chinese labels and colors: gray preparation, blue connection, amber erase, violet write, teal verify, cyan reset, slate finish. A 10px proportional bar displays aggregated measured durations. Hovering any segment shows its name, seconds, percentage and total; its accessible name exposes phase durations and keyboard focus has a visible outline. Expanded attempts use a compact three-column legend. Colors supplement phase/result text. Actual esptool method boundaries and monotonic clocks determine duration; waiting for removal and retry backoff are excluded. Production entry points load only real local records and show an honest empty state when no records exist. Synthetic sample generators and screenshot previews live exclusively under tests/.

## GitHub release update

The `NEW!` button sits immediately before the version badge inside the existing footer panel. The 62 × 28 logical-pixel pill uses the release colors from `src/styles.py`. Keep the other badges right-aligned when it appears and account for its width when stacking the footer on narrow windows. Three small bursts play once for 2.4 seconds inside the pill when it first becomes visible. Use the Windows client-area animation preference.

`ReleaseUpdateDialog` uses the workbench's standard Qt modal and primary action styling. Show the version pair, scrollable release notes and a fixed progress area. Focus “以后再说” on open and restore focus to the footer button on dismissal. Keep GitHub navigation separate from the release notes, which render Markdown with resource loading disabled.
