# Changelog — comment-slop

All notable changes to this package are documented here. The format
is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this package follows [Semantic Versioning](https://semver.org/).

<!-- New release sections are prepended below by the release-packages skill. -->

## [0.1.0] - 2026-08-26

First public release. The detector has been running as a write-time hook and
commit gate in the Ionworks monorepo since 2026-07; this packages it so other
projects can install it rather than vendor it.

### Added
- `comment-slop` console script and `python -m comment_slop`, over the same
  engine the hook runs: `--all`, `--since REF`, `--include-unreviewed`,
  `--hook`, and `--llm`.
- Comment extractors for Python, JavaScript/JSX/TSX, TypeScript, YAML, TOML,
  Rust, shell and Dockerfiles, each checked against a tree-sitter grammar by
  the differential suite in the `oracle` extra.
- Detection tiers spanning restated code, edit narration, history, benchmark
  snapshots, decorative banners, process leaks and comment density, calibrated
  against reference corpora rather than guessed. 95% precision / 85% recall
  over 121 human labels.
- `run_hook.sh`, the Claude Code `PostToolUse` wrapper, which re-derives its
  exit code from the detector's output so a broken interpreter cannot report
  itself as findings or block the edit loop.

### Notes
- No runtime dependencies, and none are planned: the detector runs on every
  edit and falls back to whatever `python3` is on `PATH`, so an import is a way
  for a broken environment to break editing.
