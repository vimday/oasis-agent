"""OKX candle capture: strict rows, cursor paging, explicit gaps and retained raw pages."""

import hashlib
import json
import urllib.parse
from pathlib import Path

import pytest

from oasis_agent.live.okx_price import OkxError
from scripts.capture_okx_candles import capture, parse_page

H = 3_600_000
START = 1_793_000_000_000 // H * H


def candle(ts: int, close: str = "2290.5", confirmed: str = "1") -> list[str]:
    return [str(ts), "2290", "2300", "2280", close, "1.5", "3400", confirmed]


def page(rows: list[list[str]]) -> bytes:
    return json.dumps({"code": "0", "data": rows, "msg": ""}).encode()


class Pager:
    """Serves descending rows strictly older than each request's `after` cursor."""

    def __init__(self, timestamps: list[int], per_page: int = 3) -> None:
        self.timestamps, self.per_page, self.paths = sorted(timestamps, reverse=True), per_page, []

    def __call__(self, path: str) -> bytes:
        self.paths.append(path)
        after = int(urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)["after"][0])
        older = [t for t in self.timestamps if t < after][: self.per_page]
        return page([candle(t) for t in older])


def test_pages_backwards_and_retains_every_raw_page(tmp_path: Path) -> None:
    end = START + 7 * H
    pager = Pager([START + i * H for i in range(-1, 7)])
    manifest = capture(
        tmp_path / "c",
        chain_id=8453,
        token="0x" + "00" * 20,
        bar="1H",
        start_ms=START,
        end_ms=end,
        fetch=pager,
        now=lambda: 1.0,
    )
    rows = [json.loads(line) for line in (tmp_path / "c/candles.jsonl").read_text().splitlines()]
    assert [r["ts"] for r in rows] == [START + i * H for i in range(7)]
    assert manifest["missing_bars"] == 0 and manifest["rows"] == 7
    assert "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee" in pager.paths[0]
    for p in manifest["pages"]:
        raw = (tmp_path / f"c/page-{p['index']:03d}.json").read_bytes()
        assert hashlib.sha256(raw).hexdigest() == p["sha256"]


def test_gaps_and_unconfirmed_bars_stay_missing(tmp_path: Path) -> None:
    def fetch(path: str) -> bytes:
        return page([candle(START + 2 * H, confirmed="0"), candle(START)])

    manifest = capture(
        tmp_path / "c",
        chain_id=8453,
        token="0x" + "11" * 20,
        bar="1H",
        start_ms=START,
        end_ms=START + 3 * H,
        fetch=fetch,
    )
    assert manifest["rows"] == 1
    assert manifest["first_missing_ms"] == [START + H, START + 2 * H]


@pytest.mark.parametrize(
    "rows",
    [
        [candle(START), candle(START + H)],  # ascending
        [candle(START), candle(START)],  # duplicate
        [candle(START + 1)],  # unaligned
        [[str(START), "1", "0.5", "1", "1", "1", "1", "1"]],  # high below open/close
        [candle(START, close="0")],
        [candle(START, close="NaN")],
        [candle(START)[:7]],
        [[START, *candle(START)[1:]]],  # non-string ts
    ],
)
def test_malformed_pages_are_refused(rows: list[list[object]]) -> None:
    with pytest.raises(OkxError):
        parse_page(json.dumps({"code": "0", "data": rows}).encode(), H)


def test_page_ignoring_cursor_is_refused(tmp_path: Path) -> None:
    with pytest.raises(OkxError, match="cursor"):
        capture(
            tmp_path / "c",
            chain_id=8453,
            token="0x" + "11" * 20,
            bar="1H",
            start_ms=START,
            end_ms=START + 2 * H,
            fetch=lambda _p: page([candle(START + 5 * H)]),
        )


def test_window_must_be_aligned_and_never_overwrites(tmp_path: Path) -> None:
    def run(out: Path, start: int) -> None:
        capture(
            out,
            chain_id=8453,
            token="0x" + "11" * 20,
            bar="1H",
            start_ms=start,
            end_ms=START + H,
            fetch=lambda _p: page([]),
        )

    with pytest.raises(ValueError, match="bar-aligned"):
        run(tmp_path / "a", START + 1)
    assert not (tmp_path / "a").exists()
    (tmp_path / "b").mkdir()
    with pytest.raises(FileExistsError):
        run(tmp_path / "b", START)
