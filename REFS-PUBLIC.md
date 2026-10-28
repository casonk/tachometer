# REFS-PUBLIC.md - Public References

> Record external public repositories, datasets, documentation, APIs, or other
> public resources that this repository utilizes or depends on.
> This file is tracked and intentionally kept free of private or local-only details.

## Public Repositories

- No fixed external code repository is the main upstream; the repo profiles local host and repository state.

## Public Datasets and APIs

- No standing public data APIs are required; snapshots are collected from the local host, git checkout, and optional sibling sidecars.

## Documentation and Specifications

- https://psutil.readthedocs.io/ - host metric collection reference
- https://git-scm.com/docs - git CLI reference for repo snapshot metadata
- https://toml.io/en/ - TOML configuration format reference for repo profiling manifests
- https://tomli.readthedocs.io/ - TOML parser fallback used on Python versions before 3.11
- https://docs.anthropic.com/en/docs/claude-code - Claude Code reference for the local CLI status and usage-cache sidecar
- https://learn.chatgpt.com/docs/codex/cli - Codex CLI reference for the local login and session-usage sidecar
- https://docs.github.com/en/copilot/concepts/agents/about-copilot-cli - GitHub Copilot CLI reference for the local session-usage sidecar

## Notes

- GPU, Fedora, and AI-provider sidecars read local state; the provider entries above document the installed CLIs whose local caches and status commands are consumed.
