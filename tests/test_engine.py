import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "plugins.v2"))
from openlisttransfer.engine import comparable_hashes, normalize_path, parse_mappings


def test_multiple_mappings():
    assert parse_mappings("/local/movies => /cloud/movies\n/local/tv → /cloud/tv") == [
        ("/local/movies", "/cloud/movies"), ("/local/tv", "/cloud/tv")
    ]


def test_nested_target_rejected():
    try:
        parse_mappings("/source => /source/cloud")
    except ValueError:
        pass
    else:
        raise AssertionError("nested target must be rejected")


def test_hash_algorithm_must_match():
    assert comparable_hashes({"hash_info": {"md5": "a"}}, {"hash_info": {"sha1": "a"}}) == (False, False)
    assert comparable_hashes({"hash_info": {"MD5": "ABC"}}, {"hash_info": {"md5": "abc"}}) == (True, True)


def test_normalize_path():
    assert normalize_path("cloud//movies/") == "/cloud/movies"
