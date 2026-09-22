"""Every corpus must carry what the evaluator reads from it.

Five per-language corpora were committed without `body_lines`, so the default
snapshot scorer refused to run on any of them the moment a verdict was entered.
Nothing caught it because they were 0% labelled -- the schema was only exercised
by the act of using them. This asserts the contract instead.
"""

import json
from pathlib import Path
import re
import sys

import pytest

HERE = Path(__file__).resolve().parent
EVAL = HERE.parent / "eval"
sys.path.insert(0, str(HERE.parent / "src"))
sys.path.insert(0, str(EVAL))

import comment_slop as cs
import generate_corpus
import score

CASE_FILES = sorted(EVAL.glob("cases*.json"))
# `body_lines` is what `snapshot_reason` rebuilds a Block from; `code` is what
# `restates-code` needs; the rest position the case in its source file.
REQUIRED = ("file", "line", "span", "body", "body_lines", "code")


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def corpus_txt(path):
    """The labels file paired with a `cases*.json`. The one place the naming
    convention is spelled out."""
    return EVAL / f"corpus{path.name[len('cases') : -len('.json')]}.txt"


def corpora():
    return [(p, load(p)) for p in CASE_FILES]


def test_there_are_corpora_to_check():
    assert CASE_FILES, "no cases*.json found -- this test would vacuously pass"


@pytest.mark.parametrize("path", CASE_FILES, ids=lambda p: p.name)
class TestCaseSchema:
    @pytest.fixture
    def blob(self, path):
        return load(path)

    @pytest.fixture
    def cases(self, blob):
        return blob["cases"]

    def test_every_case_has_every_required_field(self, path, cases):
        missing = {
            key: [i for i, c in enumerate(cases, 1) if key not in c] for key in REQUIRED
        }
        assert not {k: v for k, v in missing.items() if v}, (
            f"{path.name} cannot be scored in snapshot mode"
        )

    def test_body_lines_is_a_list_of_strings(self, cases):
        for i, c in enumerate(cases, 1):
            bl = c["body_lines"]
            assert isinstance(bl, list), f"case {i}: {type(bl)}"
            assert all(isinstance(s, str) for s in bl), f"case {i}"

    def test_body_lines_reproduces_body(self, path, cases):
        """`body` is the flattened form, so it must fall out of `body_lines`.
        Otherwise the scored text and the labelled text are different texts.

        Flattened by `Block.body` itself, not by a copy of it: the generator
        wrote the field from that property, so a copy here would keep asserting
        a rule the scorer had stopped using."""
        for i, c in enumerate(cases, 1):
            flat = cs.Block(1, 0, tuple(c["body_lines"])).body
            assert flat == c["body"], f"case {i} in {path.name}"

    def test_span_matches_body_lines(self, path, cases):
        for i, c in enumerate(cases, 1):
            assert c["span"] == len(c["body_lines"]), f"case {i} in {path.name}"

    def test_snapshot_scoring_runs_on_every_case(self, path, blob):
        """The end the schema exists for: no case may raise, whatever the
        labels happen to be today."""
        genre = blob.get("genre")
        for i, c in enumerate(blob["cases"], 1):
            try:
                score.snapshot_reason(c, genre)
            except Exception as exc:  # noqa: BLE001 - the assertion IS the report
                pytest.fail(f"{path.name} case {i}: {exc!r}")


@pytest.mark.parametrize("path", CASE_FILES, ids=lambda p: p.name)
def test_labels_file_exists_and_numbers_line_up(path):
    """A corpus renumbered without its .txt silently mislabels every case."""
    stem = path.name[len("cases") : -len(".json")]
    txt = EVAL / f"corpus{stem}.txt"
    assert txt.exists(), f"{path.name} has no corpus{stem}.txt"
    numbers = [
        int(m.group(1)) for m in re.finditer(r"^### (\d+)\s", txt.read_text(), re.M)
    ]
    n = len(json.loads(path.read_text(encoding="utf-8"))["cases"])
    assert numbers == list(range(1, n + 1)), (
        f"corpus{stem}.txt numbers {numbers[:5]}... for {n} cases"
    )


@pytest.mark.parametrize("path", CASE_FILES, ids=lambda p: p.name)
def test_labels_use_only_the_vocabulary_the_scorer_reads(path):
    stem = path.name[len("cases") : -len(".json")]
    txt = (EVAL / f"corpus{stem}.txt").read_text(encoding="utf-8")
    verdicts = re.findall(r"^verdict:\s*(\S+)", txt, re.M)
    assert verdicts, f"corpus{stem}.txt has no verdict lines"
    assert set(verdicts) <= {"slop", "keep", "skip", "?"}, set(verdicts)


def test_corpus_txt_reveals_neither_channel_nor_reason():
    """The labels must not be anchored to the thing being measured.

    Asserted over the line shapes the GENERATOR emits, not over words: sampled
    source legitimately contains `channel_id` and `seed = _seed_single(...)`, so
    a keyword scan flags the corpus for quoting the repo accurately. The only
    lines the generator writes itself are a `### n file:line` header, the `# `
    body, indented code, and `verdict:` -- and the header carries nothing but a
    location, which may contain a space (this repo has a `dev tools/`), so the
    path is matched as "non-blank, then anything, then `:<line>`".
    """
    header = re.compile(r"^### \d+  \S.*:\d+$")
    for path in CASE_FILES:
        stem = path.name[len("cases") : -len(".json")]
        txt = (EVAL / f"corpus{stem}.txt").read_text(encoding="utf-8")
        for lineno, line in enumerate(txt.split("\n"), 1):
            if not line.strip() or line.startswith(("# ", "#\n")) or line == "#":
                continue
            if line.startswith("### "):
                assert header.match(line), (
                    f"corpus{stem}.txt:{lineno} header carries more than a "
                    f"location: {line!r}"
                )
                continue
            if line.startswith("verdict:") or line.startswith("    "):
                continue
            pytest.fail(
                f"corpus{stem}.txt:{lineno} is a line shape the "
                f"generator does not emit: {line!r}"
            )


def test_case_json_keeps_the_metadata_the_txt_must_not_show():
    """The other half of the same contract: the JSON is where `channel` and
    `seed` live, so a corpus that lost them cannot be sliced by channel."""
    for path, blob in corpora():
        if blob.get("genre") is None:
            continue  # the base corpus predates channel sampling
        assert "seed" in blob, path.name
        assert all("channel" in c for c in blob["cases"]), path.name


def test_every_genre_is_scored_as_the_kind_it_was_sampled_as():
    """`GENRE_KINDS` decides whether a case is judged as prose or as API doc,
    and a genre missing from it is silently scored as a plain comment.

    Asserted against the generator's own table rather than a list of plain
    genres kept here: that list needed an edit for every language added, which
    is precisely the edit a new genre's author forgets."""
    for path, blob in corpora():
        genre = blob.get("genre")
        if genre is None:
            continue  # the base corpus is mixed by design
        assert genre in generate_corpus.GENRES, (
            f"{path.name}: genre {genre!r} is not one the generator samples"
        )
        sampled = generate_corpus.GENRES[genre][1]
        assert score.GENRE_KINDS.get(genre, cs.PLAIN) == sampled, (
            f"{path.name}: sampled as {sampled}, scored as "
            f"{score.GENRE_KINDS.get(genre, cs.PLAIN)}"
        )
