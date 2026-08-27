from __future__ import annotations

import pytest

from yt_shorts_bot.hashtags import (
    invent_smart_title as clip_invent,
    make_catchy_title as clip_title,
)
from yt_shorts_repost_bot.hashtags import (
    invent_smart_title as repost_invent,
    make_catchy_title as repost_title,
)


@pytest.mark.parametrize("invent", [clip_invent, repost_invent])
def test_smart_title_is_not_a_copy_of_the_source(invent):
    original = "Homer Goes To Work #funny"
    rewritten = invent(
        original,
        transcript_text="He forgot his lunch and then the plant exploded.",
        info={"title": original, "description": "Homer is late again at the plant."},
    )
    assert rewritten
    assert rewritten.casefold() != "homer goes to work"
    assert "#" not in rewritten


@pytest.mark.parametrize("invent", [clip_invent, repost_invent])
def test_spoken_words_can_become_the_title(invent):
    rewritten = invent(
        "Random Vlog 12",
        transcript_text="Nobody expected the donut truck to stop here.",
    )
    assert "Nobody expected the donut truck" in rewritten


@pytest.mark.parametrize("make_title", [clip_title, repost_title])
def test_user_hashtags_are_kept_and_not_invented(make_title):
    info = {"title": "Homer Goes To Work"}
    title = make_title(
        info=info,
        transcript_text="Doh this plant is on fire again.",
        title_prefix="FUNNY",
        title_hashtags="simpsons, homer",
        smart_titles=True,
    )
    body = title.removeprefix("FUNNY ").removesuffix(" #simpsons #homer").strip()
    assert title.startswith("FUNNY ")
    assert title.endswith("#simpsons #homer")
    assert "#fyp" not in title
    assert "#shorts" not in title
    assert body != "Homer Goes To Work"


@pytest.mark.parametrize("make_title", [clip_title, repost_title])
def test_toggle_off_keeps_cleaned_source_title(make_title):
    title = make_title(
        info={"title": "Homer Goes To Work #funny"},
        title_hashtags="simpsons",
        smart_titles=False,
    )
    assert title == "Homer Goes To Work #simpsons"
