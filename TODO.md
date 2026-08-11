# TODO

## Packaging

- Support saving Cookie locally in a file, with clear safety guidance.
- [x] Add PyInstaller build scripts and placeholder app icons.
- [x] Build and smoke-test macOS `DanmuQueue.app`.
- [x] Build and verify macOS DMG installer.
- Verify Windows `DanmuQueue.exe` build on Windows.
- Optional: replace placeholder icon with polished production artwork.

## Queue Display

- [x] Add a clean overlay page for live software browser sources.
- [x] Default queue display format should be one person per line:

```text
1. Name
2. Name
```

## Queue Management

- [x] Support hiding a queued person from the overlay without removing the export record.
- [x] Support adding a note/score from the overlay and preserving it in export data.
- [x] Poll recent danmaku history during disconnect/reconnect gaps as a best-effort fallback.
- Add an operator page that only shows queued people.
- Support marking a queued person as completed/done from that page.

## Export

- [x] Support TXT export.
- [x] TXT export format should include queue number, name, and optional note:

```text
1. Name 3-2
2. Name
```
