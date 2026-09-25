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
dependencies** and none are planned: the hook falls back to whatever `python3` is
on `PATH`, so an import is a way for a broken environment to break the edit loop.

Installing is optional. The engine is one stdlib-only module, so vendoring
`src/comment_slop.py` and running it with any Python 3.11+ works identically.
`src/comment_slop_llm.py` beside it adds `--llm`; without it, `--llm` warns and
reports the deterministic findings alone.

## As a Claude Code hook

Register it as a `PostToolUse` hook on `Edit|Write`. Installed, the command is
the console script:

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Edit|Write",
        "hooks": [{ "type": "command", "command": "comment-slop --hook" }]
      }
    ]
  }
}
```

From a checkout or a vendored copy, register the wrapper beside the source
instead — `"command": "sh path/to/comment-slop/run_hook.sh"`, directly or
through a symlink. It finds the detector relative to itself, runs it under `uv`
when that is available (reading the version from your project's
`.python-version`), and falls back to `python3`. If the detector is missing it
exits 1, a non-blocking hook error, rather than reporting a clean edit.

Exit **2** means findings, **0** means clean, and any internal failure also exits
0 — the hook must never break the edit loop. The wrapper exists because every
interpreter also exits 2 for its *own* failures, which the hook protocol would
show the agent as findings; it re-derives the code from the output instead. An
installed console script has its interpreter fixed at install time, so it needs
no wrapper. See [docs/internals.md](docs/internals.md).

With no git information it scans the whole file; inside a repo it scans only
lines changed since `HEAD`, so it reports on what you just wrote rather than on
the file's history.

## As a commit gate

The write-time hook sees `Edit` and `Write`, not `Bash`: an agent editing with
`sed -i` or a heredoc is never checked. A pre-commit hook closes that, because
everything reaching a commit passes through it, whoever or whatever wrote it.

In CI, pass `--since` the **merge-base** with the target branch:

    comment-slop --since "$(git merge-base origin/main HEAD)" <paths>

A clean CI checkout matches `HEAD` everywhere, so without `--since` the gate
scans nothing and passes. Against the branch *tip* instead, `git diff` is
two-dot, and a branch behind `main` is blamed for every line `main` gained since
the fork. An unresolvable `--since` ref scans nothing rather than everything, so
fail the job if the ref is missing.

## Usage

Run it on a file, a directory, or a glob:

    comment-slop backend/src/foo.py
    comment-slop --all 'frontend/src/**/*.ts'
    comment-slop --all backend/src

Quote a glob: the tool expands `**` itself, because whether a shell recurses on
a bare `**` depends on the shell. Directories skip `node_modules`, `target`,
`dist` and the rest of the build output. Paths git reports as unchanged are
dropped before scanning; untracked files count as changed.

| flag | effect |
|---|---|
| `--hook` | read the file path from hook JSON on stdin; bounds the model sweep by wall clock |
| `--since REF` | compare against `REF` instead of `HEAD`, and scan only the files `REF` changed |
| `--all` | report every line, not only those changed since `HEAD`. **Required to sweep committed code** — without it a clean tracked file reports nothing |
| `--include-unreviewed` | also report every short comment no tier caught. High recall, low precision; for a deliberate sweep, never the hook |
| `--llm` | add the model narration sweep. Exploratory; see [docs/llm-sweep.md](docs/llm-sweep.md) |

A sweep of already-committed code is therefore
`--all --include-unreviewed <paths>`; the hook itself passes neither.

## What it reports

| tier | catches |
|---|---|
| `restates-code` | comment says what the next line already says |
| `edit-narration` | narrates a completed change (`Fixed: skip None entries`). Past tense and the `verb:` changelog prefix only — an imperative opening is how a docstring summary describes the code |
| `historical` | what the code used to do — needs a pronoun subject (`this used to …`), since `the pattern used to build X` is a contract, not history |
| `unbuilt` | code that never reached `main` — first-person edit narration (`we removed the retry wrapper`) and a road not taken (`tried a shared cache but it did not help`). Both belong in the PR, whose branch `git log` cannot resolve once it is gone |
| `process-leak` | task numbers, plan references, review chatter |
| `benchmark` | a measured performance snapshot (`~144x fewer buffers`, `98.2% smaller`, `measured 812ms`). The unit is the discriminator, not the digit, and a cost that carries an argument (`costs 1.3 ms/file, because …`) stays silent |
| `banner`, `section-label`, `step-marker` | decorative dividers and `Step 1:` labels |
| `bare-identifier` | a lone name that repeats the identifier below it |
| `narrative`, `meta`, `emoji` | chatty asides, comments about comments |
| `conditional-narration` | short `If condition, perform action` restatements |
| `code-reference` | pointers to other code that will rot |
| `verbose` | plain: over 30 words / at least 3 prose lines; API-doc prose: over 90 / at least 10 |
| `density` | a changed region that is more comment than reference practice: 30% by default, 40% for shell, whose scope is the whole script rather than a function |
| `unreviewed` | `--include-unreviewed` only |
| `narration (model)` | `--llm` only |

A **trailing** comment is exempt from the tiers whose signal is terseness —
`restates-code`, `section-label`, `bare-identifier`, `narrative`, `process-leak`,
`historical`, `unbuilt` and `conditional-narration` — because it annotates the
value beside it: `timeout = 5  # seconds`. Edit narration, banners, `Step 3:`
markers, `meta`, emoji, rotting `file.ts:120` pointers, benchmark snapshots and
length still fire on one. Density counts a line carrying both code and a comment
as a **code** line.

Doc comments are judged on the prose the author **chose** — the narrative before
the first `@tag`, rustdoc section or numpydoc header, plus every free-text
section (`Notes`, `Warnings`, `@remarks`) — and sit on neither side of the
density ratio. A parameter list is as long as the signature and an example as
long as the code it shows, so neither spends the budget.

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

`.ts` is its own row because TypeScript forbids JSX there, so `<Foo>x` stays a
type assertion. Languages are recognised by path only, so an extensionless
`#!/usr/bin/env bash` script is not scanned.

## Tuning

When the hook reports something deliberate, prefer widening `NEVER` over
narrowing a tier in `TIERS`. `NEVER` is the documented escape hatch and already
covers lint directives (`noqa`, `eslint-`, `shellcheck`, …), `TODO`/`SAFETY`/
`NOTE`, licences, URLs and issue references. An exempt line does not merge with
its neighbours, so a new `NEVER` alternative silences only the comment it
matches.

The intent markers match **uppercase only**, because each is also an ordinary
English word: `BUG: broken` is exempt and `a real lexer bug sat here` is not.
The issue key (`ABC-123`) must be shouted too, or `us-east-1` and `theta-1` read
as one; a new alternative that can match a lowercase `word-digits` token needs
the same `(?-i:...)` treatment.

Thresholds live at the top of `comment_slop.py` and are calibrated rather than
guessed — see [docs/calibration.md](docs/calibration.md) before changing one.
A language's own facts, from its extensions to its density ceiling, are one
`Lang` entry in `LANGUAGES`.

## Tests

    uv run --extra dev python -m pytest tests
    COMMENT_SLOP_ORACLE_STRICT=1 uv run --extra dev --extra oracle \
        python -m pytest tests/test_oracle_differential.py

The second is the tree-sitter differential, which checks the hand-written
lexers against real grammars over real files. Run it before touching any lexer;
[docs/internals.md](docs/internals.md) covers why it is kept apart and why it
reads every file rather than a sample.

Current deterministic accuracy is **95% precision, 85% recall** over 121 human
labels. Precision is the figure that matters: a false positive spends the
author's trust, a miss costs nothing.

## Docs

| file | contents |
|---|---|
| [docs/internals.md](docs/internals.md) | pipeline, the hook wrapper, comment kinds, span semantics, how to verify a lexer change |
| [docs/calibration.md](docs/calibration.md) | every threshold and where it came from; firing rates |
| [docs/evaluation.md](docs/evaluation.md) | the labelled corpus, sampling channels, dogfood window |
| [docs/llm-sweep.md](docs/llm-sweep.md) | the `--llm` sweep: model and prompt measurements, why it is off by default |

## Provenance

Originally developed at https://github.com/BradyPlanden/claude-comment-hook and
vendored into the Ionworks monorepo at `20d7caf`. Development continues there
and the two have diverged; this package is built from that copy, which is the
source of truth.

Released under the MIT licence — see [LICENSE](LICENSE).
