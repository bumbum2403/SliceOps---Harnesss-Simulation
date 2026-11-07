"""The CLI's own small decisions - the ones that are not the harness."""

from __future__ import annotations

from pathlib import Path

from sliceops.cli import resolve_run


def test_a_run_id_is_enough_to_replay(tmp_path, monkeypatch):
    """Everything else in the project names a run by its id: the log line, the
    end card, the GUI dropdown. Typing that id at `replay` should work."""
    monkeypatch.setattr("sliceops.cli.RUNS_DIR", tmp_path)
    log = tmp_path / "20261101-2338-g9jd.jsonl"
    log.write_text("")

    assert resolve_run(Path("20261101-2338-g9jd")) == log


def test_a_path_still_works(tmp_path, monkeypatch):
    """A log that has been moved or renamed is still replayable, which is what
    makes a log worth attaching to a bug report."""
    monkeypatch.setattr("sliceops.cli.RUNS_DIR", tmp_path)
    moved = tmp_path / "somewhere-else.jsonl"
    moved.write_text("")

    assert resolve_run(moved) == moved


def test_an_unknown_id_is_not_guessed_at(tmp_path, monkeypatch):
    monkeypatch.setattr("sliceops.cli.RUNS_DIR", tmp_path)
    assert resolve_run(Path("nope")) is None


def test_no_argument_means_the_most_recent_run(tmp_path, monkeypatch):
    monkeypatch.setattr("sliceops.cli.RUNS_DIR", tmp_path)
    (tmp_path / "20261101-1000-aaaa.jsonl").write_text("")
    newest = tmp_path / "20261101-2000-bbbb.jsonl"
    newest.write_text("")

    assert resolve_run(None) == newest
