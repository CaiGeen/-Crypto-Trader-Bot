# Isolated Windows atomic-replace diagnostic

Date: 2026-10-13. This is a controlled mechanism test, not proof of the process
responsible for any historical production event.

## Environment and isolation

- Python 3.11.1
- Windows 10, version `10.0.17763`, build `17763`
- 30-minute experiment ran only under the system temporary directory:
  `%LOCALAPPDATA%\Temp\opencode\health-replace-30m-wj1ob7fe`
- No production path, credentials, network, repository state, ACL changes, or
  antivirus settings were accessed or modified. The temporary original report
  and summary remain at that location.

## Results

- Concurrent ordinary Python reader and high-frequency same-directory
  `os.replace` writer:
  - 158,651 replace attempts; 68,756 succeeded; 89,895 failed.
  - All 89,895 writer failures were `PermissionError`, `winerror=5`, `errno=13`.
  - Reader observed 110,797 `PermissionError` opens and 189 `FileNotFoundError`
    opens under this deliberately high contention.
- Holding a normal Python read handle open across replacement:
  - 158,651 attempts; 0 succeeded; all 158,651 failed with
    `PermissionError`, `winerror=5`, `errno=13`.
- Control calibration with `CreateFileW` sharing read/write but not delete:
  - 25/25 replacement attempts failed with expected `WinError 5`.
  - The concurrent reader remained active during post-release attempts; only
    8/25 subsequent replacements succeeded. This is not a clean no-reader
    control and should not be interpreted as one.
- The final target file remained present.

## Interpretation and limits

The experiment validates that an open destination handle which does not allow
delete sharing can cause Windows `os.replace` to fail with the same
`WinError 5`. The production health巡检 reader uses ordinary `open()` at
`健康巡检.py:177-182`, and `bot_runner.py:2934` periodically replaces
`control.json`; that makes a reader-held target a plausible mechanism.

The stress test used far higher read/write overlap than production. Its failure
ratio is **not** a production failure-rate estimate, and it does not establish
which process held the target during the historical incidents. Production
attribution remains unconfirmed. If the error naturally recurs, collect
Process Monitor evidence filtered to the exact target path and time window,
including the failing rename result and the process/handle sharing behavior.

No runtime fix, retry, read-sharing change, ACL change, antivirus exclusion,
production reproduction, restart, or deployment was performed.
