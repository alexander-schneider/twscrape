import pytest

from twscrape.utils import parse_cookies, to_old_obj


def test_cookies_parse():
    val = "abc=123; def=456; ghi=789"
    assert parse_cookies(val) == {"abc": "123", "def": "456", "ghi": "789"}

    val = '{"abc": "123", "def": "456", "ghi": "789"}'
    assert parse_cookies(val) == {"abc": "123", "def": "456", "ghi": "789"}

    val = '[{"name": "abc", "value": "123"}, {"name": "def", "value": "456"}, {"name": "ghi", "value": "789"}]'
    assert parse_cookies(val) == {"abc": "123", "def": "456", "ghi": "789"}

    val = "eyJhYmMiOiAiMTIzIiwgImRlZiI6ICI0NTYiLCAiZ2hpIjogIjc4OSJ9"
    assert parse_cookies(val) == {"abc": "123", "def": "456", "ghi": "789"}

    val = "W3sibmFtZSI6ICJhYmMiLCAidmFsdWUiOiAiMTIzIn0sIHsibmFtZSI6ICJkZWYiLCAidmFsdWUiOiAiNDU2In0sIHsibmFtZSI6ICJnaGkiLCAidmFsdWUiOiAiNzg5In1d"
    assert parse_cookies(val) == {"abc": "123", "def": "456", "ghi": "789"}

    val = '{"cookies": {"abc": "123", "def": "456", "ghi": "789"}}'
    assert parse_cookies(val) == {"abc": "123", "def": "456", "ghi": "789"}

    with pytest.raises(ValueError, match=r"Invalid cookie value: .+"):
        val = "{invalid}"
        assert parse_cookies(val) == {}


def test_to_old_obj_user_handles_non_dict_nested_fields():
    doc = to_old_obj(
        {
            "__typename": "User",
            "legacy": None,
            "id_str": 123,
            "avatar": [],
            "location": "Berlin",
            "privacy": "public",
            "verification": [],
            "profile_bio": "bio",
        }
    )

    assert doc["id_str"] == "123"
    assert doc["id"] == 123
    assert doc["profile_image_url_https"] == ""
    assert doc["location"] == "Berlin"
    assert doc["description"] == ""


def test_to_old_obj_user_maps_current_profile_fields():
    doc = to_old_obj(
        {
            "__typename": "User",
            "rest_id": "12345",
            "core": {"screen_name": "testuser", "name": "Test User"},
            "avatar": {"image_url": "https://example.com/avatar.jpg"},
            "banner": {"image_url": "https://example.com/banner.jpg"},
            "profile_bio": {
                "description": "A test bio",
                "entities": {"description": {}, "url": {}},
            },
            "action_counts": {"favorites_count": 11},
            "relationship_counts": {"followers": 12, "following": 13},
            "tweet_counts": {"media_tweets": 14, "tweets": 15},
            "pinned_items": {"tweet_ids_str": ["67890"]},
        }
    )

    assert doc["profile_banner_url"] == "https://example.com/banner.jpg"
    assert doc["entities"] == {"description": {}, "url": {}}
    assert doc["favourites_count"] == 11
    assert doc["followers_count"] == 12
    assert doc["friends_count"] == 13
    assert doc["media_count"] == 14
    assert doc["statuses_count"] == 15
    assert doc["pinned_tweet_ids_str"] == ["67890"]


def test_to_old_obj_tweet_casts_fallback_id_str():
    doc = to_old_obj({"__typename": "Tweet", "legacy": None, "id_str": 456})

    assert doc["id_str"] == "456"
    assert doc["id"] == 456
    assert doc["conversation_id_str"] == "456"
