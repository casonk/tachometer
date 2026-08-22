# Changelog

## Unreleased

- Fixed the package being unimportable on Windows. `profile.py` imported the
  Unix-only `resource` module at module scope, so `import tachometer` raised
  `ModuleNotFoundError: No module named 'resource'` — despite the package
  advertising `Operating System :: OS Independent`. The import is now guarded
  the same way `psutil` already was, and the rusage-derived metrics report
  `None` where the platform cannot supply them rather than a fabricated zero.
- Added the shared `install-check` workflow, which installs the package on
  Linux, macOS and Windows and runs the console script. It is what found the
  import failure above.
- Initial `clockwork` scaffold for declarative cron and systemd rendering.
- Documented tachometer disk red lights as the trigger contract for the shared traction-control disk-pressure remediation agent.
