# Changelog — comment-slop

All notable changes to this package are documented here. The format
is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this package follows [Semantic Versioning](https://semver.org/).

<!-- New release sections are prepended below by the release-packages skill. -->

## [0.2.0] - 2026-09-24

### Breaking changes
- `find_findings` is renamed `analyze(src, path, added)`: the whole pipeline with
  no I/O. `scan` is its file-reading half, and its options are now keyword-only.
- `Block`, `EXTRACTORS`, `lang_key`, the `*_EXT` extension sets and `ts_comments`
  are replaced by `LANGUAGES` (one entry per language) and `language_of`.
- `classify` and `model_eligible` move to the `comment_slop_llm` module, which
  the wheel ships alongside `comment_slop`. Vendoring only `comment_slop.py` now
  drops `--llm`: it warns and reports the deterministic findings.

### Fixed
- A Python file containing an invalid escape sequence no longer loses all of its
  findings.
- `run_hook.sh` finds the detector relative to itself, so it works outside the
  repository layout it was written in rather than exiting 0 on every edit.
- `--all --include-unreviewed` no longer reports YAML version-pin notes or
  Dockerfile `# syntax=` directives as unreviewed.
- The `--llm` pre-filter applies the same language exemptions and shebang skip
  as the deterministic tiers.

## [0.1.1] - 2026-09-22

### Fixed
- Line narrowing works inside a git worktree, where the hook previously failed to
  map changed lines and fell back to scanning whole files.

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
