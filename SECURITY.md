# Security Policy

## Scope

`clockwork` renders scheduler artifacts. It must never become a place to store
live secrets, crontab exports with credentials, or host-specific private data.

## Reporting

Report security issues privately to the repository owner instead of opening a
public issue with exploit details.

## Handling Rules

- Keep secrets in external env files or secret stores, not in manifests.
- Use generic paths and placeholder usernames in tracked examples unless an
  exact path is required to explain the workflow.
- Treat generated unit files and crontab snippets as reviewable text artifacts;
  do not add hidden shell expansion or remote download behavior to install
  flows.
