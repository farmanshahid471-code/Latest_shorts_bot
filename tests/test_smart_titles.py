"""Title/hashtag generation tests for the current make_catchy_title API.

The published title is always: {prefix} {clean-or-smart title} {user hashtags}.
Hashtags are NEVER inferred from content - only the account's explicit
title_hashtags/extra_hashtags are published. ``smart_titles`` (legacy name) only
controls whether the first coherent spoken phrase replaces the source title.
"""
from __future__ import annotations

import pytest

from yt_shorts_bot.hashtags import (
    build_hashtags as clip_hashtags,
    make_catchy_title as clip_title,
)
from yt_shorts_repost_bot.hashtags import (
    build_hashtags as repost_hashtags,
    make_catchy_title as repost_title,
)


@pytest.mark.parametrize("make_title", [clip_title, repost_title])
def test_clean_source_title_and_prefix_without_smart(make_title):
    title = make_title(
        info={"title": "Homer Goes To Work #funny"},
        title_prefix="FUNNY",
        title_hashtags="simpsons, homer",
        smart_titles=False,
    )
    assert title == "FUNNY Homer Goes To Work #simpsons #homer"


@pytest.mark.parametrize("make_title", [clip_title, repost_title])
def test_smart_title_uses_first_spoken_phrase_when_enabled(make_title):
    title = make_title(
        info={"title": "Random Vlog 12"},
        transcript_text="Nobody expected the donut truck to stop here.",
        smart_titles=True,
    )
    assert "Nobody Expected The Donut Truck" in title
    assert "Random Vlog" not in title


@pytest.mark.parametrize("make_title", [clip_title, repost_title])
def test_user_hashtags_kept_and_never_invented(make_title):
    title = make_title(
        info={"title": "Homer Goes To Work"},
        transcript_text="Doh this plant is on fire again.",
        title_prefix="FUNNY",
        title_hashtags="simpsons, homer",
        smart_titles=True,
    )
    assert title.startswith("FUNNY ")
    assert title.endswith("#simpsons #homer")
    assert "#fyp" not in title
    assert "#shorts" not in title
    assert "#viral" not in title


@pytest.mark.parametrize("make_title", [clip_title, repost_title])
def test_toggle_off_keeps_cleaned_source_title(make_title):
    title = make_title(
        info={"title": "Homer Goes To Work #funny"},
        title_hashtags="simpsons",
        smart_titles=False,
    )
    assert title == "Homer Goes To Work #simpsons"


@pytest.mark.parametrize("make_title", [clip_title, repost_title])
def test_title_stays_within_100_chars(make_title):
    title = make_title(
        info={"title": "A" * 200},
        title_hashtags="one,two,three",
        smart_titles=False,
    )
    assert len(title) <= 100


@pytest.mark.parametrize("build", [clip_hashtags, repost_hashtags])
def test_hashtags_come_only_from_user_input(build):
    tags = build(
        info={"title": "viral minecraft speedrun #gaming"},
        transcript_text="alpha beta gamma delta",
        title_hashtags="minecraft, Speedrun",
        smart_titles=True,
    )
    assert tags == ["minecraft", "speedrun"]
    assert "gaming" not in tags  # source-title hashtags are not adopted
