# mouser_hook.dll — native WH_MOUSE_LL procedure

Windows runs a low-level mouse hook procedure inside the hooking process and
blocks the whole system input pipeline until it returns. Mouser's procedure
used to be Python, so it had to take the GIL, and every application's mouse
input queued behind whatever else Mouser's threads were doing. Measured on
tiny11 under load: `SendInput` 76–187 ms with Mouser running, 1.3–1.6 ms
without.

This DLL owns the procedure instead. It runs on its own thread, decides from
two masks Mouser pushes down (which events are interesting, which are
swallowed), and queues the few events Mouser acts on for a Python drain
thread to pick up off the input path. No system input event waits on the GIL.

## Build

From the repository root, on the Windows machine:

```
python native/win/build.py
```

The script uses MSVC (`cl`) when it is on `PATH` — start an *x64 Native Tools
Command Prompt* to get it — and falls back to mingw-w64 (`gcc` or
`x86_64-w64-mingw32-gcc`). It writes `mouser_hook_x64.dll` next to the source.

`build.bat` runs this automatically before packaging, and `Mouser.spec`
bundles the DLL into `dist/Mouser/`.

## Not building it is fine

The DLL is optional. When it is missing, fails to load, or reports an ABI
different from `core/native_hook_filter.ABI_VERSION`, the Windows hook falls
back to its Python procedure and behaves exactly as it did before — remaps,
gestures, and scroll inversion all keep working, just with the input latency
this DLL exists to remove. Mouser logs which procedure it installed at
startup (`[MouseHook] Hook installed (native filter)` or `(python)`).

## Verifying it on Windows

The suite cross-compiles this source and asserts its constants match
`core/native_hook_filter.py`, but it cannot *run* the procedure — that needs
Windows. Work through this by hand after a change to `mouser_hook.c`:

1. `python native/win/build.py`, then start Mouser and confirm the log says
   `Hook installed (native filter)`, not `(python)`.
2. Press each remapped button. Every mapped action must fire, and the original
   button must not leak through to the focused app.
3. Perform a side-swipe gesture on the host. Confirm the mapped action fires.
4. With scroll invert on, scroll both axes and confirm the direction flips —
   and that a fast scroll no longer stalls the cursor.
5. Switch KVM focus to another machine, then back. Gestures and remaps must
   still work on the machine holding the mouse.
6. Under load (WinStream running), check Deskflow's probe:
   `Select-String 'worst SendInput' C:\ProgramData\Deskflow\deskflow-daemon.log | Select -Last 8`
   — every sample should be well under 5000us.

Rename or delete the DLL and repeat steps 2–5 to confirm the Python fallback
still carries them.

## Keeping the two sides in step

The struct layout, event codes, and filter flags are duplicated in
`core/native_hook_filter.py`. Change one and you must change the other, and
bump `MOUSER_HOOK_ABI` / `ABI_VERSION` so an older DLL left in a build
directory is rejected instead of misread.

`tests/test_native_hook_filter.py` asserts the C source and the Python
constants agree, so a one-sided edit fails the suite rather than the machine.
