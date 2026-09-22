"""Append-only input following without real clocks, sleep or network."""

import pytest

from oasis_agent.shadow.stream import cycle_lines


def test_partial_record_waits_for_newline(tmp_path, monkeypatch):
    path = tmp_path / "input.jsonl"
    path.write_bytes(b'{"a":')

    def finish(_):
        with path.open("ab") as output:
            output.write(b"1}\n")

    monkeypatch.setattr("oasis_agent.shadow.stream.time.sleep", finish)
    stream = cycle_lines(path, follow=True)
    assert next(stream) == b'{"a":1}\n'
    stream.close()


@pytest.mark.parametrize("change", ["truncate", "rotate"])
def test_stream_replacement_rejected(tmp_path, change):
    path = tmp_path / "input.jsonl"
    path.write_bytes(b"{}\n")
    stream = cycle_lines(path, follow=True)
    assert next(stream) == b"{}\n"
    if change == "truncate":
        path.write_bytes(b"")
    else:
        path.rename(tmp_path / "old.jsonl")
        path.write_bytes(b"{}\n")
    with pytest.raises(ValueError, match=r"truncated|rotated"):
        next(stream)


def test_idle_deadline_is_failure_and_stop_file_is_clean(tmp_path, monkeypatch):
    path = tmp_path / "input.jsonl"
    path.write_bytes(b"")
    ticks = iter([0, 2])
    monkeypatch.setattr("oasis_agent.shadow.stream.time.monotonic", lambda: next(ticks))
    with pytest.raises(ValueError, match="idle"):
        next(cycle_lines(path, follow=True, idle_seconds=1))
    stop = tmp_path / "stop"
    stop.touch()
    monkeypatch.setattr("oasis_agent.shadow.stream.time.monotonic", lambda: 0)
    assert list(cycle_lines(path, follow=True, stop_file=stop)) == []


def test_oversized_input_rejected(tmp_path):
    path = tmp_path / "input.jsonl"
    path.write_bytes(b"x" * 1_000_001)
    with pytest.raises(ValueError, match="size"):
        next(cycle_lines(path))
