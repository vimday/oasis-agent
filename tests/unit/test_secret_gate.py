import pytest

from scripts.check_secrets import unreviewed_candidates


def test_secret_gate_fails_new_candidate_even_in_previously_reviewed_file():
    current = {
        "version": "1.5.0",
        "results": {
            "src/example.py": [
                {"type": "Secret Keyword", "hashed_secret": "a" * 40, "line_number": 2}
            ]
        },
    }
    baseline = {
        "scanner_version": "1.5.0",
        "reviewed_nonsecrets": [
            {
                "path": "src/example.py",
                "type": "Secret Keyword",
                "hashed_secret": "b" * 40,
                "classification": "public_or_synthetic_nonsecret",
                "reason": "fixture",
            }
        ],
    }
    assert unreviewed_candidates(current, baseline) == [("src/example.py", 2, "Secret Keyword")]
    baseline["reviewed_nonsecrets"][0]["hashed_secret"] = "a" * 40
    assert unreviewed_candidates(current, baseline) == []
    current["version"] = "future"
    with pytest.raises(ValueError):
        unreviewed_candidates(current, baseline)
