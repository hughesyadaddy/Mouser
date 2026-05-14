# Mouser PR screenshots

Side branch that hosts the screenshots referenced from PR bodies on
`TomBadash/Mouser`. Kept off `master` so the PR diffs stay focused on
the actual code change.

| File | Used in PR |
|---|---|
| `scroll-page-divert-active.png` | #172 (`feat/wheel-divert-ui-badge`) |
| `scroll-page-divert-inactive.png` | #172 (`feat/wheel-divert-ui-badge`) |
| `mappings-mx4-labels.png` | #173 (`feat/per-device-button-labels`) |
| `mappings-mx3s-fallback.png` | #173 (`feat/per-device-button-labels`) |
| `about-panel.png` | #168 (`feat/macos-app-shell`) |
| `app-icon-refresh-sidebar.png` | #177 (`feat/app-icon-refresh`) -- sidebar mark in app |
| `app-icon-1024.png` | #177 (`feat/app-icon-refresh`) -- the canonical 1024 master |

All shots were captured by running the actual Mouser QML window
through `QQuickWindow.grabWindow()` against a connected MX Master 3S
(`mappings-mx4-labels.png` substitutes a synthetic MX Master 4 device
spec via the same fake-engine harness the test suite uses, since I
don't physically have an MX Master 4 on hand).
