# comment_slop.py

A write-time hook that reports comment slop on the lines you just changed.

Detection only — it never edits a source file, and by default it never calls a
model. It runs as a `PostToolUse` hook in Claude Code, so the report goes back to
the agent that wrote the file, which is the last point where anyone still knows
whether a comment was load-bearing.

    comment-slop: backend/src/pipeline.py
      L3    delete  [restates-code]     Increment the counter
      L5    shorten [verbose]           This function takes the raw payload and, after checking that
      L9    delete  [historical]        This used to return a dict
      L11   shorten [historical]        cut: It previously sent one per row.
    Repo convention: comment the non-obvious why, not the what.
    delete: nothing in it is worth keeping. shorten: keep its fact in fewer words,
    without what `cut:` names. Or explain why a comment is load-bearing.

Each finding says what to do with the comment: delete it, or keep its fact in
fewer words. See [Delete or shorten](#delete-or-shorten).

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
| `--strict` | report every comment the change adds; see [Strict mode](#strict-mode). `COMMENT_SLOP_STRICT=1` sets it where no flag can be passed |
| `--llm` | add the model narration sweep. Exploratory; see [docs/llm-sweep.md](docs/llm-sweep.md) |

A sweep of already-committed code is therefore
`--all --include-unreviewed <paths>`; the hook itself passes neither.

## What it reports

| tier | catches | fix |
|---|---|---|
| `restates-code` | comment says what the next line already says, or names the definition it sits on: `Cleanup function` over `cleanup() {` | delete |
| `edit-narration` | narrates a completed change (`Fixed: skip None entries`). Past tense and the `verb:` changelog prefix only — an imperative opening is how a docstring summary describes the code | cut the `Fix:` label, or the sentence narrating the edit |
| `historical` | what the code used to do — needs a pronoun subject (`this used to …`), since `the pattern used to build X` is a contract, not history. A date inside a timestamp, a path or a dotted value is a format example | cut the sentence |
| `unbuilt` | code that never reached `main` — first-person edit narration (`we removed the retry wrapper`) and a road not taken (`tried a shared cache but it did not help`). Both belong in the PR, whose branch `git log` cannot resolve once it is gone | cut the sentence |
| `process-leak` | task numbers, plan references, review chatter | cut the reference |
| `benchmark` | a measured performance snapshot (`~144x fewer buffers`, `98.2% smaller`, `measured 812ms`). The unit is the discriminator, not the digit, and a cost that carries an argument (`costs 1.3 ms/file, because …`) stays silent | cut the sentence |
| `banner`, `section-label` | decorative dividers and section labels. A label over a group of statements it names, `State` over six fields, is a heading and stays | delete |
| `step-marker` | `Step 1:` and `1.` labels | cut the number |
| `bare-identifier` | a lone name that repeats the identifier below it, not one naming a group of statements | delete |
| `narrative`, `meta`, `emoji` | chatty asides, comments about comments | cut the aside |
| `conditional-narration` | short `If condition, perform action` restatements | delete |
| `code-reference` | pointers to other code that will rot | cut the pointer |
| `verbose` | plain: over 30 words / at least 3 prose lines; API-doc prose: over 90 / at least 10. Formula, matrix, worked-arithmetic and table rows spend neither the word nor prose-line budget | shorten |
| `density` | a changed region that is more comment than reference practice: 30% by default, 40% for shell, whose scope is the whole script rather than a function | shorten |
| `unreviewed` | `--include-unreviewed` only | delete |
| `strict` | `--strict` only: any comment the change adds | delete, or cut the added lines |
| `narration (model)` | `--llm` only | delete |

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

### Delete or shorten

A finding asks for one of two fixes, and they are not interchangeable: deleting a
comment that carries a fact loses the fact, and shortening one that carries
nothing keeps it.

- **`delete`**: nothing in the comment is worth keeping.
- **`shorten`**: it carries a fact, in more words than the fact needs. Where a
  tier objected to one part, the row shows that part as `cut:`.

A tier whose fix above is *cut* objects to a part of the comment. It asks to
shorten when what is left stands as a comment, and to delete when it does not.
What is left stands if no tier would delete it and it has more than
`CUT_REST_WORDS` (8) distinct content words. Fewer is a label or a narrated step,
not a fact: under `Step 6:`, `Detect connected components` goes with it. A
docstring's rest needs only a word, since there it is the summary line.

A docstring is never deleted from under its `Parameters`, `Returns`, `@param`
or other prescribed sections. The tiers read only its prose, so where those
follow, a finding that would delete the docstring asks to cut the prose instead.

### Strict mode

`--strict` reports every comment a change adds, whatever the tiers make of it.
Two kinds stay allowed. A Python docstring, which the tiers still judge. And a
comment a tool reads: a lint directive (`# noqa`, `// eslint-disable-next-line`,
`# shellcheck disable=`, `# yamllint disable`, `# hadolint ignore=`), a Rust
`// SAFETY:`, a Python encoding declaration, a YAML `# yaml-language-server:`
line, a Dockerfile `# syntax=`, a pinned action's `# v4.1.1`, since code can
need one to pass a check. JSDoc, rustdoc and `#:` are comments,
and so is an empty `#` or `//` on its own, though not a blank line inside a
comment. A changelog entry is not one, and keeps its own tiers.

The hook and the commit gate take no flags, so `COMMENT_SLOP_STRICT=1` in the
environment turns it on: start the agent with it set (`COMMENT_SLOP_STRICT=1
claude`) for a strict session, or commit with it set (`COMMENT_SLOP_STRICT=1 git
commit`) for a strict gate. Empty or `0` is off.

A comment line counts as added when the change touched it and the file now has
more comment lines with its text than it had at the comparison ref. Reindenting
a block, moving a comment within the file or editing the code beside it adds
nothing, so strict never asks for someone else's comment to go, while a second
copy pasted in with the code under it is added. Where a comment only gained
lines, the finding asks to shorten it, with the new lines as its `cut:`. A file
git reports as renamed is compared with its old path, so a rename adds nothing,
but a comment moved in from another file, such as a module split in two, counts
as added.

The change is every line since `HEAD`, as it is without strict, not only the
agent's. Commit comments of your own before a strict session, or the agent is
told to remove them.

### Changelogs

A `CHANGELOG.md` entry is held to
[PyBaMM's guideline](https://github.com/pybamm-team/PyBaMM/blob/main/CONTRIBUTING.md#changelog-entries):
a single bullet ending in its PR link. The tiers above would read an entry's
`Added …` and `previously …` as slop, so an entry takes three of its own instead:

| tier | catches | fix |
|---|---|---|
| `verbose` | over 40 words, counting a link's text but not its URL | shorten |
| `multi-line` | a wrapped entry, or one carrying a nested list | rewrite |
| `pr-link` | an entry that does not end in `([#123](https://github.com/owner/repo/pull/123))` or a bare `(#123)`, or whose `#123` is not the number it links. A GitLab `[!123](…/-/merge_requests/123)` counts | rewrite |

The bare `(#123)` is for a changelog published outside its repo, such as a
public mirror of a private monorepo, where a link would point readers at a page
they cannot open.

As everywhere, only the entries a change touches are judged, so adopting it
does not report a changelog's history.

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
| Changelog | `CHANGELOG.md` | top-level list items | — |

`.ts` is its own row because TypeScript forbids JSX there, so `<Foo>x` stays a
type assertion. Languages are recognised by path only, so an extensionless
`#!/usr/bin/env bash` script is not scanned.

## Tuning

When the hook reports something deliberate, prefer widening `NEVER` over
narrowing a tier in `TIERS`. `NEVER` is the documented escape hatch and already
covers lint directives (`noqa`, `eslint-`, `shellcheck`, …), `TODO`/`SAFETY`/
`NOTE`, licences, URLs and issue references. An exempt line does not merge with
its neighbours, so a new `NEVER` alternative silences only the comment it
matches. The exception is a sentence that runs through the exempt line: that is
one comment, and it is exempt whole. A line opening with a completed edit
(`fixed the cache logic`) continues nothing, unless the line above ends in an
auxiliary (`is`, or `is never`), a subject pronoun or a coordinator (`salted
and`): under a `TODO` with no period it would otherwise read as that marker's
predicate.

The intent markers match **uppercase only**, because each is also an ordinary
English word: `BUG: broken` is exempt and `a real lexer bug sat here` is not.
The issue key (`ABC-123`) must be shouted too, or `us-east-1` and `theta-1` read
as one; a new alternative that can match a lowercase `word-digits` token needs
the same `(?-i:...)` treatment. `UTF-8` and `SHA-256` have the key's shape and
are excluded by name, and `#123` needs a word boundary so `#020E0D` is a colour.

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

Current deterministic accuracy is **91% precision, 77% recall** over 121 human
labels, counting a finding only when it asks for the fix the label names. As a
flag on a comment the labeller would change, whatever the fix, it is 100% and
85%; [docs/evaluation.md](docs/evaluation.md) has the gap. Precision is the
figure that matters: a false positive spends the author's trust, a miss costs
nothing.

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
