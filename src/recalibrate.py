#!/usr/bin/env python3
"""Re-derive comment_slop.py's thresholds from a reference corpus.

The thresholds are percentiles of comment length, block span and comment
density in packages whose comments are accepted as good. Those packages change,
so this regenerates the table rather than leaving the constants as folklore.

Reference corpora are read from an installed site-packages, so the numbers move
with whatever is installed. Run it against the same interpreter the reviewed
code targets.

    uv run --with numpy --with scipy --with pandas python recalibrate.py
"""

from __future__ import annotations

import argparse
import ast
import os
import statistics as st
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import comment_slop as cs

DEFAULT_PACKAGES = ("numpy", "scipy", "pandas")
# Substrings of a directory path, not names, so `/test` drops `numpy/testing`
# too: a package's tests and vendored copies are not its house style.
SKIP_DIRS = ("/tests", "/test", "/_vendor", "/vendored", "/site-packages/setuptools")


def source_files(root: str, lang: str = "python") -> list[str]:
    """Every file under `root` the hook reads as `lang`, stubs aside.

    A `.pyi` is signatures with no bodies, so its functions read as all code:
    counted, they take numpy's density p95 from 27.9% to 20.7%.
    """
    out = []
    for path in cs.walk_source(root):
        if path.endswith(".pyi") or any(s in os.path.dirname(path) for s in SKIP_DIRS):
            continue
        found = cs.language_of(path)
        if found is not None and found.name == lang:
            out.append(path)
    return out


def function_scopes(src: str) -> list[range]:
    """Line ranges of every Python function.

    Production measures density over a diff hunk, so the reference percentile
    has to be measured over a scope of comparable size -- a function, not a whole
    file, which is why DENSITY_MAX reads as "of a function's lines". Only Python
    offers one cheaply; other languages fall back to the file, as they always
    did.
    """
    try:
        tree = ast.parse(src)
    except (SyntaxError, ValueError, RecursionError):
        return []
    return [
        range(n.lineno, (n.end_lineno or n.lineno) + 1)
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]


def measure(path: str) -> tuple[list[int], list[int], list[float]] | None:
    """Return (block word counts, block spans, per-scope densities).

    Every classification comes from `cs.extract`, `cs.group_blocks`,
    `cs.subject`, `cs.exempt` and `cs.density_sets` -- the functions the hook
    itself calls, not a near-copy. A copy drifts from production quietly: rebuilding
    Python comments with `lstrip("#")` leaves a `#:` body starting with `:`,
    which the veto then drops from the word percentiles, and counting every
    comment token reads a function with three `noqa` lines over nine statements
    as 23% where production reads 0%. Either way the thresholds become
    percentiles of a population the hook never sees.

    Docstrings need no `ast` pass of their own: `extract` returns them as `doc`
    comments and `density_sets` puts them in `neutral`, off both sides of the
    ratio.
    """
    try:
        src = open(path, encoding="utf-8").read()
    except (OSError, UnicodeDecodeError):
        return None
    if len(src) > cs.MAX_BYTES:
        return None
    lines = src.split("\n")
    raw = cs.extract(src, path)
    if not raw:
        return None

    words, spans = [], []
    for b in cs.group_blocks(raw):
        if b.kind != cs.PLAIN or b.inline:
            continue
        s = cs.subject(b)
        if cs.exempt(s):
            continue
        # prose_rows, not b.span: it is what the verbose tier compares
        # against, so the percentiles stay comparable to what fires.
        prose = cs.prose_rows(s.lines)
        words.append(len(cs.RAW_WORD.findall(" ".join(prose))))
        spans.append(len(prose))

    counted, neutral = cs.density_sets(raw)
    scopes = function_scopes(src) if raw[0].lang == "python" else []
    # One ratio, whichever scope it is measured over. Minimum size differs
    # because the scopes do: a stub's ratio is noise.
    whole_file = not scopes
    dens = []
    for extent in scopes or [range(1, len(lines) + 1)]:
        n_cmt, n_code = cs._density_counts(lines, extent, counted, neutral)
        big_enough = n_cmt + n_code >= 12 if whole_file else n_code >= 8
        if big_enough:
            dens.append(n_cmt / (n_cmt + n_code))
    return words, spans, dens


def percentile(values: list[float], p: float) -> float:
    return sorted(values)[min(int(len(values) * p / 100), len(values) - 1)]


def report(name: str, roots: list[str], lang: str = "python") -> None:
    words, spans, dens = [], [], []
    files = 0
    for root in roots:
        for path in source_files(root, lang):
            got = measure(path)
            if got is None:
                continue
            files += 1
            words += got[0]
            spans += got[1]
            dens += got[2]
    if not words or not dens:
        print(f"{name:<12}  no measurable source found")
        return
    tall = 100 * sum(1 for s in spans if s >= 5) / len(spans)
    print(
        f"{name:<12}{files:>7}{len(words):>9}{st.median(words):>7.0f}"
        f"{percentile(words, 95):>7.0f}{percentile(words, 98):>7.0f}"
        f"{tall:>8.1f}%{100 * percentile(dens, 95):>9.1f}%"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("packages", nargs="*", default=list(DEFAULT_PACKAGES))
    ap.add_argument(
        "--site-packages",
        default=os.path.dirname(os.path.dirname(cs.__file__)),
        help="where to look for the reference packages",
    )
    ap.add_argument(
        "--lang",
        choices=sorted(cs.LANGUAGES),
        default="python",
        help="which language's corpora to measure",
    )
    ap.add_argument(
        "--roots",
        nargs="*",
        default=None,
        help="explicit corpus roots (required for any --lang but python)",
    )
    args = ap.parse_args()

    if args.lang != "python" or args.roots:
        if not args.roots:
            ap.error(f"--lang {args.lang} needs --roots")
        roots = [os.path.expanduser(r) for r in args.roots]
        missing = [r for r in roots if not os.path.isdir(r)]
        if missing:
            ap.error(f"not a directory: {', '.join(missing)}")
        print(f"{args.lang} corpora: {', '.join(roots)}\n")
        print(
            f"{'corpus':<12}{'files':>7}{'blocks':>9}{'med':>7}{'p95':>7}"
            f"{'p98':>7}{'  >=5L':>8}{'dens p95':>9}"
        )
        print("-" * 66)
        for root in roots:
            report(os.path.basename(root.rstrip("/")) or root, [root], args.lang)
        if len(roots) > 1:
            report("combined", roots, args.lang)
        print(
            f"\nin use now: plain={cs.VERBOSE_WORDS} words/"
            f"{cs.VERBOSE_LINES} lines, doc={cs.DOC_VERBOSE_WORDS} words/"
            f"{cs.DOC_VERBOSE_LINES} lines, "
            f"DENSITY_MAX={cs.DENSITY_MAX:.0%}"
        )
        return 0

    site = args.site_packages
    for candidate in (site, *(p for p in sys.path if p.endswith("site-packages"))):
        if any(os.path.isdir(os.path.join(candidate, p)) for p in args.packages):
            site = candidate
            break

    print(f"reference corpora under {site}\n")
    print(
        f"{'corpus':<12}{'files':>7}{'blocks':>9}{'med':>7}{'p95':>7}{'p98':>7}"
        f"{'  >=5L':>8}{'dens p95':>9}"
    )
    print("-" * 66)
    found = []
    for pkg in args.packages:
        root = os.path.join(site, pkg)
        if os.path.isdir(root):
            report(pkg, [root])
            found.append(root)
        else:
            print(f"{pkg:<12}  not installed, skipped")
    if found:
        report("combined", found)

    print(
        f"\nin use now: plain={cs.VERBOSE_WORDS} words/"
        f"{cs.VERBOSE_LINES} lines, doc={cs.DOC_VERBOSE_WORDS} words/"
        f"{cs.DOC_VERBOSE_LINES} lines, "
        f"DENSITY_MAX={cs.DENSITY_MAX:.0%}"
    )
    print(
        "Set the plain word ceiling from p98 of the tightest corpus and "
        "DENSITY_MAX from its density p95. Calibrate doc ceilings against "
        "doc-specific labels and dogfood."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
