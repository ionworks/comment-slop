"""Firing-rate bands per language.

A band is a FLOOD ALARM, not a quality gate. Precision and recall need labels;
firing rate does not, and it catches the failure labels would only reveal
later -- flooding a language until the hook gets muted. Never quote a band as
evidence of precision.

Bands are +/-8 points around the measurement recorded when this landed. Skips
when a corpus is absent, following the existing pattern for the model sweep.
"""

import fnmatch
import functools
import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import comment_slop as cs

APP = Path(os.path.expanduser("~/Documents/git-repos/ionworks/app/ionworks-app"))
DIFFSOL = Path(os.path.expanduser("~/Documents/git-repos/diffsol"))
# Dockerfiles are a handful per repo, so theirs is drawn repo-wide.
REPOS = Path(os.path.expanduser("~/Documents/git-repos"))
DOCKER_GLOBS = (
    "Dockerfile",
    "Dockerfile.*",
    "*.dockerfile",
    "Containerfile",
    "Containerfile.*",
)
SHELL_GLOBS = tuple(f"*{ext}" for ext in sorted(cs.SH_EXT))

# (label, root, globs, kind, expected %). Each figure is a measurement, not a
# target: re-measure and rewrite it when a tier changes.
BANDS = [
    # Every figure rose 0.3-1.0 when the NEVER markers became uppercase-only:
    # `bug` and `note` as ordinary words had been exempting their comments.
    ("python plain", APP, ("*.py",), cs.PLAIN, 21.8),
    ("python doc", APP, ("*.py",), cs.DOC, 5.2),
    ("ts plain", APP / "frontend" / "src", ("*.ts", "*.tsx"), cs.PLAIN, 38.6),
    ("ts doc", APP / "frontend" / "src", ("*.ts", "*.tsx"), cs.DOC, 6.0),
    ("yaml plain", APP, ("*.yml", "*.yaml"), cs.PLAIN, 37.0),
    ("rust plain", DIFFSOL, ("*.rs",), cs.PLAIN, 18.5),
    # Merge exemption scoped to PLAIN holds this near 9.6; on doc blocks it split
    # `///` runs on their headings and took it to 4.5.
    ("rust doc", DIFFSOL, ("*.rs",), cs.DOC, 9.6),
    # 11.5 here, but REPOS is whatever this machine has checked out; the marker
    # change moved it +0.0, so the original measurement stands.
    ("dockerfile plain", REPOS, DOCKER_GLOBS, cs.PLAIN, 16.9),
    # Shell's 40% density ceiling is not in this figure: `block_fires` reads
    # one comment and density is regional. It is in the dogfood log instead.
    ("shell plain", APP, SHELL_GLOBS, cs.PLAIN, 32.0),
]
TOLERANCE = 8.0
LIMIT = 400


@functools.cache
def _matching(root: Path, globs: tuple[str, ...]) -> tuple[Path, ...]:
    """Cached because the bands come in pairs: `python plain` and `python doc`
    walk the identical tree, as do the ts and rust pairs, so half the
    traversals were repeats. Kept apart from `files_for` so the skips below stay
    outside the cache."""
    return tuple(
        sorted(
            Path(p)
            for p in cs.walk_source(str(root))
            if any(fnmatch.fnmatch(os.path.basename(p), g) for g in globs)
        )
    )


def files_for(root: Path, globs: tuple[str, ...]) -> list[Path]:
    """A strided sample, which is correct HERE but not for the differential.

    A firing rate is a proportion, and a sample estimates a proportion. The
    tree-sitter differential asks whether a disagreement exists anywhere, which
    a sample can only be lucky about: stride it, and the one disagreeing file
    in 555 lands in the half never read.
    """
    if not root.is_dir():
        pytest.skip(f"corpus absent: {root}")
    out = _matching(root, globs)
    if not out:
        pytest.skip(f"no files matching {globs} under {root}")
    stride = max(1, len(out) // LIMIT)
    return list(out[::stride][:LIMIT])


def firing_rate(files: list[Path], kind: str) -> tuple[float, int]:
    scored = fired = 0
    for path in files:
        try:
            src = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if len(src) > cs.MAX_BYTES:
            continue
        raw = [c for c in cs.extract(src, str(path)) if c.kind == kind]
        if not raw:
            continue
        for b in cs._group_blocks(raw, src.split("\n")):
            if not cs.classified_body(b):
                continue
            scored += 1
            fired += cs.block_fires(b)
    if scored < 50:
        pytest.skip(f"only {scored} scorable blocks; too few to measure")
    return 100.0 * fired / scored, scored


@pytest.mark.parametrize(
    "label,root,globs,kind,expected", BANDS, ids=[b[0] for b in BANDS]
)
def test_firing_rate_is_within_band(label, root, globs, kind, expected):
    rate, n = firing_rate(files_for(root, globs), kind)
    assert abs(rate - expected) <= TOLERANCE, (
        f"{label}: {rate:.1f}% over {n} blocks, expected "
        f"{expected:.1f}% +/-{TOLERANCE:.0f}. A jump means a tier started "
        f"firing structurally; a drop means coverage was lost. This is a "
        f"flood alarm, not a precision measurement."
    )
