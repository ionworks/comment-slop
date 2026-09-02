# comment_slop.py

A write-time hook that reports comment slop on the lines you just changed.

Detection only — it never edits a source file, and by default it never calls a
model. It runs as a `PostToolUse` hook in Claude Code, so the report goes back to
the agent that wrote the file, which is the last point where anyone still knows
whether a comment was load-bearing.

    comment-slop: backend/src/pipeline.py
      L42   [restates-code]     Increment the counter
      L67   [verbose]           This function takes the raw payload and, after
      L91   [historical]        Previously this returned a dict
    Repo convention: comment the non-obvious why, not the what.
    Delete or rewrite these, or explain why each is load-bearing.

## Install

    uv add comment-slop           # or: pip install comment-slop

giving you a `comment-slop` command and `python -m comment_slop`. It has **no
dependencies** and none are planned — see [Usage](#usage).

Installing is optional. The engine is one stdlib-only module, so vendoring
`src/comment_slop.py` and running it with any Python 3.11+ works identically;
that is how it runs in the repo it was written for.

### As a Claude Code hook

Register `run_hook.sh` as a `PostToolUse` hook on `Edit|Write`:

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Edit|Write",
        "hooks": [
          {
            "type": "command",
            "command": "sh \"$CLAUDE_PROJECT_DIR\"/packages/comment-slop/run_hook.sh"
          }
        ]
      }
    ]
  }
}
```

In this monorepo both layers are already wired: the write-time hook in the
tracked `.claude/settings.json` and the commit gate in
`.pre-commit-config.yaml` — see [Two layers](#two-layers). The wrapper runs on
the repo's Python, reading the version from `.python-version` rather than
restating it, since `uv` would otherwise resolve it from a cwd the hook does not
control. `uv` only makes startup faster; the wrapper falls back to whatever
`python3` is on `PATH`, including when the pinned version is unavailable, so a
broken toolchain cannot stop edits either.

Exit **2** means findings, **0** means clean. Any internal failure also exits 0 —
the hook must never break the edit loop.

That last rule is why `settings.json` runs `run_hook.sh` rather than the detector
directly. Exit 2 is the hook protocol's "block, and show this text to the agent",
and *every* interpreter also exits 2 for its own failures: `uv` on an unwritable
cache, a broken `UV_PYTHON` or an unreadable config; `python3` on a missing
script; `argparse` on a bad flag. Invoked directly, a broken toolchain reported
its own error text as comment findings and blocked the edit loop. The wrapper
re-derives the exit code from the output instead — the findings path always
opens with `comment-slop:` — and retries under `python3` when `uv` itself
failed, so a broken `uv` degrades to a working fallback rather than to silence.

With no git information it scans the whole file; inside a repo it scans only
lines changed since `HEAD`, so it reports on what you just wrote rather than on
the file's history.

## Two layers

The write-time hook covers `Edit` and `Write`, which name their file in
`tool_input.file_path`. It cannot cover `Bash`, and that gap is not small: an
agent told to edit with `sed -i`, a heredoc redirect, or a throwaway script
writes whatever it likes and the detector never sees the file.

The gap was first closed inside this hook, by asking git what the shell command
had changed and bounding it with an mtime cursor per session. That worked, and it
was the wrong layer. Recovering the file list cost ~150 lines of state, taxed
every `Bash` call in the session with three `git` calls, and still leaked: a
`mv`, a `cp -p` or a `rsync -t` preserves the old mtime, and a bulk change past
the per-call cap was dropped and never revisited.

So the real gate is **prek** (`comment-slop` in `.pre-commit-config.yaml`, via
`scripts/check-comment-slop.sh`). pre-commit hands it the staged paths, which is
the question the cursor was guessing at. Everything reaching a commit is covered
— shell writes, another agent, another harness, a human in an editor — and
nothing has to infer anything.

| | write-time hook | prek gate |
|---|---|---|
| covers | `Edit`, `Write` | anything that reaches a commit |
| file list | named in the payload | staged paths, from pre-commit |
| when | as the file is written | at `git commit`, and in CI |
| cost | ~85ms per edit | one run per commit, changed files only |

Neither is redundant. The hook reports while the agent still knows whether a
comment was load-bearing, which is the only moment that judgement is cheap. The
gate is what actually holds, because it cannot be routed around by choosing a
different tool.

### `--since` is not optional in CI

CI runs prek over a clean checkout. Every tracked file matches `HEAD` there, so
the detector — which by design reports only changed lines — finds nothing, and
the job goes green having scanned nothing at all. That is worse than having no
CI check, because the green tick reads as coverage.

`scripts/check-comment-slop.sh` therefore switches to `--since
origin/$GITHUB_BASE_REF` whenever `GITHUB_BASE_REF` is set, and **exits 1 if that
ref cannot be resolved** rather than scanning zero files. `ci-lint.yml` checks out
with `fetch-depth: 0` so the ref exists; that setting is load-bearing.

### Narrowing

Both modes drop paths git reports as unchanged before scanning them, grouped by
repository so the query costs two `git` calls rather than the two per path
`added_lines` would spend reaching the same answer. It matters because the hook
declares no `files:` — the detector's supported-extension set is the only filter,
so `prek run --all-files` hands it all 2907 supported files in this repo. Narrowed,
that run is 0.5s instead of ~56s.

Untracked files survive it: relative to the ref they are additions, and they get
the whole-file scan they always did. `--all` bypasses it entirely, since narrowing
the documented sweep of committed code would make it report nothing.

## Usage

Run it on a file, a directory, or a glob:

    comment-slop backend/src/foo.py
    comment-slop --all 'frontend/src/**/*.ts'
    comment-slop --all backend/src

Or without installing, which is what the hook and the recipes below do:

    uv run --no-project python packages/comment-slop/src/comment_slop.py backend/src/foo.py

Quote a glob: the tool expands `**` itself, because whether a shell recurses on
a bare `**` depends on the shell. Directories skip `node_modules`, `target`,
`dist` and the rest of the build output.

| flag | effect |
|---|---|
| `--hook` | read the file path from hook JSON on stdin; bounds the model sweep by wall clock |
| `--since REF` | compare against `REF` instead of `HEAD`. The CI gate — see [`--since` is not optional in CI](#--since-is-not-optional-in-ci) |
| `--all` | report every line, not only those changed since `HEAD`. **Required to sweep committed code** — without it a clean tracked file reports nothing, which is what made `--include-unreviewed` look broken |
| `--include-unreviewed` | also report every short unmatched comment. High recall, low precision. For a deliberate sweep, never the hook |
| `--llm` | add the model narration sweep. Exploratory; see [docs/llm-sweep.md](docs/llm-sweep.md) |

A sweep of already-committed code is therefore
`--all --include-unreviewed <paths>`; the hook itself passes neither.

## What it reports

| tier | catches |
|---|---|
| `restates-code` | comment says what the next line already says |
| `edit-narration` | narrates a completed change (`Fixed: skip None entries`). Past tense and the `verb:` changelog prefix only — an imperative opening is how a docstring summary describes the code |
| `historical` | what the code used to do — needs a pronoun subject (`this used to …`), since `the pattern used to build X` is a contract, not history |
| `unbuilt` | code that never reached `main` — first-person edit narration (`we removed the retry wrapper`) and a road not taken (`tried a shared cache but it did not help`). Both belong in the PR: the branch they refer to is unmerged or gone, so `git log` cannot resolve them either. Narrow by measurement — `no longer` and `is gone` are almost always present-state facts, so they are not in it |
| `process-leak` | task numbers, plan references, review chatter |
| `benchmark` | a measured performance snapshot (`~144x fewer buffers`, `98.2% smaller`, `measured 812ms`). The unit is the discriminator, not the digit: units, limits and non-latency percentiles stay silent, and so does a cost that carries an argument (`costs 1.3 ms/file, because …`) |
| `banner`, `section-label`, `step-marker` | decorative dividers and `Step 1:` labels |
| `bare-identifier` | a lone name that repeats the identifier below it |
| `narrative`, `meta`, `emoji` | chatty asides, comments about comments |
| `conditional-narration` | short `If condition, perform action` restatements |
| `code-reference` | pointers to other code that will rot |
| `verbose` | plain: over 30 words / at least 3 prose lines; API-doc prose: over 90 / at least 10 |
| `density` | a changed region that is more comment than reference practice. The ceiling is per language (`DENSITY_CEILINGS`): 30% by default, 40% for shell, whose scope is the whole script rather than a function |
| `narration (model)` | `--llm` only |

A **trailing** comment is exempt from the tiers whose signal is terseness —
`bare-identifier`, `restates-code`, `section-label`, `narrative`,
`conditional-narration`, `historical`, `unbuilt`, `process-leak` — because a trailing
comment annotates the value beside it, which is what those tiers read as a label
for the line. `timeout = 5  # seconds` is the shape, and it alone fired 133
times across this repo. What inline-ness does not excuse still fires: edit
narration, banners, `Step 3:` markers, `meta`, emoji, rotting `file.ts:120`
pointers, benchmark snapshots, and length. The `--llm` sweep skips inline
comments entirely, and this is the deterministic half of the same argument.

Density counts a line carrying both code and a comment as a **code** line, for
the same reason — see *density* in [docs/internals.md](docs/internals.md).

## Languages

| language | extensions | plain | doc |
|---|---|---|---|
| Python | `.py` `.pyi` | `#`, `#:` | docstrings |
| JS/JSX/TSX | `.js` `.jsx` `.mjs` `.cjs` `.tsx` | `//`, `/* */` | `/** */` |
| TS (no JSX) | `.ts` `.mts` `.cts` | `//`, `/* */` | `/** */` |
| YAML | `.yml` `.yaml` | `#` | — |
| TOML | `.toml` | `#` | — |
| Rust | `.rs` | `//`, `/* */` | `///` `//!` `/** */` `/*! */` |
| Shell | `.sh` `.bash` `.zsh` `.ksh` | `#` | — |
| Dockerfile | `Dockerfile`, `Dockerfile.*`, `*.dockerfile`, `Containerfile*` | `#` | — |

Shell is the only `#` language with a trailing comment form, so it is not the
Dockerfile scanner with more extensions: `#` opens a comment at the start of a
word and nowhere else (`${x#pre}`, `$((16#ff))` and `it#not` are words), quoting
carries across lines, and a heredoc body is skipped while a `<<<` herestring is
not. Extensions only — a `#!/usr/bin/env bash` script named `deploy` reaches no
extractor, because the dispatch is given a path and not the source.

`.ts` gets its own row because TypeScript forbids JSX there so that `<Foo>x` can
stay a type assertion, and `ts_comments` turns the JSX scanner off to match.
Reading a cast as an element would consume the rest of the file as element text.
Babel allows JSX in `.js`, so only the three `.ts` spellings opt out.

Doc comments are judged on the prose the author **chose** — the narrative before
the first `@tag`, rustdoc section or numpydoc header, plus every free-text
section (`Notes`, `Warnings`, `@remarks`) — and sit on neither side of the
density ratio. A prescribed section does not spend the budget: a parameter list
is as long as the signature and an example as long as the code it shows. Details
in [docs/internals.md](docs/internals.md).

Our numpydoc convention in `.claude/rules/python-docstrings.md` is compatible:
the `Parameters` / `Returns` / `Raises` sections are prescribed and unbudgeted,
and the imperative summary it mandates is not read as edit narration.

## House style

The `banner` tier reports the `# ----` section dividers used across
`backend/src/jobs/` and parts of the frontend. Those fires are accurate — they
really are decorative dividers — and are deliberately left in place rather than
suppressed. The tier only fires when you touch a divider line, so treat it as a
nudge away from the style, not a bug.

## Tuning

When the hook reports something deliberate, prefer widening `NEVER` over
narrowing `PATTERNS`. `NEVER` is the documented escape hatch and already covers
lint directives (`noqa`, `eslint-`, `shellcheck`, …), `TODO`/`SAFETY`/`NOTE`,
licences, URLs and issue references.

The intent markers match **uppercase only**, because each is also an ordinary
English word: `BUG: broken` is exempt and `a real lexer bug sat here` is not.

Widening `NEVER` is safe for the comment's neighbours: an exempt line no longer
merges, so a new `NEVER` alternative silences the comment that matches it and
nothing glued to it. It used to silence the whole grouped run.

`NEVER` is case-insensitive except the issue key (`ABC-123`), which must be
shouted. Case-folding it made `us-east-1`, `theta-1`, `libssh2-1` and
`Wasserstein-1` read as issue keys, and the veto hid every comment around them.
A new alternative that can match a lowercase `word-digits` token needs the same
`(?-i:...)` treatment.

Thresholds live at the top of `comment_slop.py` and are calibrated rather than
guessed — see [docs/calibration.md](docs/calibration.md) before changing one.
Log every wrong fire in `eval/dogfood.md`; that log is what justifies a tier
change, and it records the reasoning behind the ones already made.

## Tests

    just test-hooks                 # golden tests, ~15s
    just test-hooks-oracle          # the tree-sitter differential, ~4s (first run installs grammars)
    just test-hooks 'packages/comment-slop/tests/test_pipeline.py -v'

773 passing, 1 skipped. The one skip is the `--llm` recall check, which needs a
local model reachable. `test-hooks-oracle` skips nothing.

**`test-hooks` does not collect the differential**, deliberately: it installs no
tree-sitter, so the file would `importorskip` its way to green and a skip nobody
can act on reads as coverage. `test-hooks-oracle` is its one home, where
`COMMENT_SLOP_ORACLE_STRICT=1` turns a missing grammar or corpus into a
**failure**. CI runs both (`.github/workflows/ci-hooks.yml`), gated on
`packages/comment-slop/**` and `.claude/settings.json`. Why the exclusion lives in
`tests/conftest.py` rather than in the recipe, and why the differential reads
every matching file rather than a sample, are in
[docs/internals.md](docs/internals.md).

Its corpora default to this checkout, so TSX, TS, YAML and shell need no setup;
TOML and Dockerfiles are a handful per repo, so theirs widens to every local
checkout under `~/Documents/git-repos` when there is one. `COMMENT_SLOP_ORACLE_APP` and
`_REPOS` override those. Rust lives outside the repo — point `COMMENT_SLOP_ORACLE_RUST` at a
Rust checkout to include it, or rely on `test_rust_traps`, which carries its
source inline. **Run the differential before touching any extractor** — see
[docs/internals.md](docs/internals.md).

`ruff-check` and `ruff-format` cover `packages/` at the repo's own settings, so
they reach this package like any other. The one exemption is in `ruff.toml`: the
tests and `eval/` scripts insert `src/` on `sys.path` before importing
`comment_slop`, which E402 reads as a misplaced import and which is how they
import the engine without depending on it being installed.

Keep a constant's rationale in own-line comments **above** it. `ruff-format`
de-indents a hanging trailing comment to column 0, where `VERBOSE_WORDS`'
measurements ended up reading as `VERBOSE_LINES`' rationale.

Score against the labelled corpus with:

    just score-hooks

Every corpus carries `body_lines` — the immutable comment text captured at
sampling time — and `tests/test_corpus_schema.py` asserts that plus the rest of
the schema the scorer reads. Regenerate a corpus with `generate_corpus.py`, never
by hand.

`eval/` is **not** part of the published package. Its corpora quote the source
they were sampled from verbatim, and that source is a private monorepo, so they
stay there rather than shipping to PyPI; the figures they produce are quoted
above. Everything below applies to a monorepo checkout.

`eval/score.py` reads `COMMENT_SLOP_EVAL_REPO`, which already defaults to this
monorepo — the base corpus was sampled from it. The committed corpora record a
`source_repo` name rather than the path they came from, so scoring a genre
sampled elsewhere (both Rust genres come from `diffsol`) means pointing that
variable at the right checkout first. Only `--live` reads the source tree at
all; the default snapshot scoring works from captured comment lines.

## Docs

| file | contents |
|---|---|
| [docs/internals.md](docs/internals.md) | pipeline, comment kinds, span semantics, how to verify a lexer change |
| [docs/calibration.md](docs/calibration.md) | every threshold and where it came from; firing rates |
| [docs/evaluation.md](docs/evaluation.md) | `eval/`, the labelled corpus, sampling channels, dogfood window |
| [docs/llm-sweep.md](docs/llm-sweep.md) | the `--llm` sweep: model and prompt measurements, why it is off by default |

The original design spec and implementation plan sit in `docs/superpowers/`,
which this repo's `.gitignore` excludes along with every other Claude-generated
plan. They are present in a local clone but are not tracked here; the upstream
repo below keeps them.

Current deterministic accuracy: **95% precision, 85% recall** over 121 human
labels, scored from immutable captured comment lines. Precision is the figure
that matters — a false positive spends the author's trust, a miss costs nothing.

## Provenance

Originally developed at https://github.com/BradyPlanden/claude-comment-hook and
vendored into the Ionworks monorepo at `20d7caf`. Development continues there
and the two have diverged; this package is built from that copy, which is the
source of truth.

Released under the MIT licence — see [LICENSE](LICENSE).
