"""Unit tests for pure helpers: URL allow-listing, number clamping, config/topic validation."""
import json
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import common  # noqa: E402
from controller import as_number, safe_youtube_url  # noqa: E402


# ---- safe_youtube_url -------------------------------------------------------
@pytest.mark.parametrize("url,expected", [
    ("https://www.youtube.com/watch?v=jfKfPfyJRdk", "https://www.youtube.com/watch?v=jfKfPfyJRdk"),
    ("http://youtube.com/watch?v=x", "https://youtube.com/watch?v=x"),  # upgraded to https
    ("  https://youtu.be/abc  ", "https://youtu.be/abc"),
    ("https://M.YouTube.com/", "https://m.youtube.com/"),               # host lower-cased
    ("https://music.youtube.com/watch?v=1#t=3", "https://music.youtube.com/watch?v=1"),  # fragment dropped
])
def test_safe_url_accepts_youtube(url, expected):
    assert safe_youtube_url(url) == expected


@pytest.mark.parametrize("url", [
    "https://evil.example/",
    "https://youtube.com.evil.example/",
    "https://evilyoutube.com/",
    "javascript:alert(1)",
    "file:///C:/Windows/win.ini",
    "ftp://youtube.com/",
    "//youtube.com/watch",
    "",
    "https://[::1/",                              # urlsplit ValueError
    r"https://www.youtube.com\@evil.example/",
    # Python's urlsplit sees host www.youtube.com, but browsers (WHATWG) treat "\" as "/"
    # and would navigate to evil.com. Must be rejected.
    r"https://evil.com\@www.youtube.com/x",
    "https://evil.example@www.youtube.com/",      # userinfo
    "https://user:pw@www.youtube.com/",
    "https://www.youtube.com:8443/",              # explicit port
    "https://www.youtube.com:99999/",             # invalid port -> ValueError path
    "https://www.you\ttube.com/",                 # control char
    "https://www.youtube.com /x",                 # embedded space
])
def test_safe_url_rejects_others(url):
    assert safe_youtube_url(url) is None


def test_safe_url_output_never_has_userinfo_or_backslash():
    for url in [r"https://www.youtube.com/watch?v=a\b","https://a@youtube.com/"]:
        out = safe_youtube_url(url)
        assert out is None or ("\\" not in out and "@" not in out.split("/")[2])


# ---- as_number --------------------------------------------------------------
@pytest.mark.parametrize("value,expected", [
    (30, 30), ("1.5", 1.5), (-20, -20), (5000, 3600), (-5000, -3600),
    (None, 10), ("abc", 10), ([], 10), ({}, 10),
    (10**400, 10),           # float() raises OverflowError -> default
    (True, 10), (False, 10), # bools are not numbers here
    ("nan", 10), (float("nan"), 10),
])
def test_as_number_clamps_and_defaults(value, expected):
    assert as_number(value, 10, -3600, 3600) == expected


@pytest.mark.parametrize("value", ["inf", "-inf", float("inf")])
def test_as_number_infinite_is_clamped(value):
    n = as_number(value, 1, 0.25, 2)
    assert math.isfinite(n) and 0.25 <= n <= 2


# ---- load_config ------------------------------------------------------------
@pytest.fixture
def cfg_file(tmp_path, monkeypatch):
    f = tmp_path / "config.json"
    monkeypatch.setattr(common, "CONFIG_FILE", f)
    monkeypatch.delenv("YTR_TOPIC", raising=False)
    monkeypatch.delenv("NTFY_SERVER", raising=False)
    return f


def test_load_config_creates_random_topic(cfg_file):
    cfg = common.load_config()
    assert cfg_file.exists()
    saved = json.loads(cfg_file.read_text())
    assert saved["topic"] == cfg["topic"] and cfg["topic"].startswith("ytr-")
    assert len(cfg["topic"]) >= 20 and common.TOPIC_RE.fullmatch(cfg["topic"])
    assert cfg["server"] == "https://ntfy.sh"
    assert cfg["status_topic"] == cfg["topic"] + "-status"
    # stable across loads
    assert common.load_config()["topic"] == cfg["topic"]


def test_load_config_env_overrides(cfg_file, monkeypatch):
    monkeypatch.setenv("YTR_TOPIC", "my_topic-1")
    monkeypatch.setenv("NTFY_SERVER", "https://ntfy.example.org/")
    cfg = common.load_config()
    assert cfg["topic"] == "my_topic-1"
    assert cfg["server"] == "https://ntfy.example.org"
    assert cfg["status_topic"] == "my_topic-1-status"


def test_status_topic_fits_64_chars(cfg_file, monkeypatch):
    monkeypatch.setenv("YTR_TOPIC", "a" * 64)
    cfg = common.load_config()
    assert len(cfg["status_topic"]) <= 64
    assert cfg["status_topic"].endswith("-status")
    assert common.TOPIC_RE.fullmatch(cfg["status_topic"])


@pytest.mark.parametrize("bad", ["a" * 65, "has space", "slash/topic", "dot.topic", "abc\n", "ü"])
def test_load_config_rejects_bad_topic(cfg_file, monkeypatch, bad):
    monkeypatch.setenv("YTR_TOPIC", bad)
    with pytest.raises(SystemExit):
        common.load_config()


# ---- results selector (regression: new YouTube lockup layout returned 0 results) ----
def test_result_selector_covers_both_layouts_everywhere():
    import controller
    assert "ytd-video-renderer a#video-title" in controller.RESULT_SEL
    assert "yt-lockup-view-model a.ytLockupMetadataViewModelTitle" in controller.RESULT_SEL
    # STATUS_JS must use the same selector (substituted as a JS string literal)
    assert json.dumps(controller.RESULT_SEL) in controller.STATUS_JS
    assert "RESULT_SEL" not in controller.STATUS_JS
