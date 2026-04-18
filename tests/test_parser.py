import json
import os
from typing import Callable

import pytest

import twscrape.models as models_module
from twscrape import API, gather
from twscrape.models import (
    AudiospaceCard,
    BroadcastCard,
    ParseDriftError,
    PollCard,
    SummaryCard,
    Trend,
    Tweet,
    User,
    UserRef,
    parse_tweet,
    parse_tweets,
)

BASE_DIR = os.path.dirname(__file__)
DATA_DIR = os.path.join(BASE_DIR, "mocked-data")
os.makedirs(DATA_DIR, exist_ok=True)


def get_api():
    api = API()
    # To be sure all tests are mocked
    api.pool = None  # type: ignore
    return api


class FakeRep:
    text: str

    def __init__(self, text: str):
        self.text = text

    def json(self):
        return json.loads(self.text)


def fake_rep(filename: str):
    filename = filename if filename.endswith(".json") else f"{filename}.json"
    filename = filename if filename.startswith("/") else os.path.join(DATA_DIR, filename)

    with open(filename) as fp:
        return FakeRep(fp.read())


def mock_rep(fn: Callable, filename: str, as_generator=False):
    rep = fake_rep(filename)

    async def cb_rep(*args, **kwargs):
        return rep

    async def cb_gen(*args, **kwargs):
        yield rep

    assert "__self__" in dir(fn)
    cb = cb_gen if as_generator else cb_rep
    cb.__name__ = fn.__name__
    cb.__self__ = fn.__self__  # pyright: ignore
    setattr(fn.__self__, fn.__name__, cb)  # pyright: ignore


def check_tweet(doc: Tweet | None):
    assert doc is not None
    assert isinstance(doc.id, int)
    assert isinstance(doc.id_str, str)
    assert str(doc.id) == doc.id_str

    assert doc.url is not None
    assert doc.id_str in doc.url
    assert doc.user is not None

    assert isinstance(doc.conversationId, int)
    assert isinstance(doc.conversationIdStr, str)
    assert str(doc.conversationId) == doc.conversationIdStr

    if doc.inReplyToTweetId is not None:
        assert isinstance(doc.inReplyToTweetId, int)
        assert isinstance(doc.inReplyToTweetIdStr, str)
        assert str(doc.inReplyToTweetId) == doc.inReplyToTweetIdStr

    if doc.inReplyToUser:
        check_user_ref(doc.inReplyToUser)

    if doc.mentionedUsers:
        for x in doc.mentionedUsers:
            check_user_ref(x)

    obj = doc.dict()
    assert doc.id == obj["id"]
    assert doc.id_str == obj["id_str"]
    assert doc.user.id == obj["user"]["id"]

    assert "url" in obj
    assert "_type" in obj
    assert obj["_type"] == "snscrape.modules.twitter.Tweet"

    assert "url" in obj["user"]
    assert "_type" in obj["user"]
    assert obj["user"]["_type"] == "snscrape.modules.twitter.User"

    txt = doc.json()
    assert isinstance(txt, str)
    assert str(doc.id) in txt

    if doc.media is not None:
        if len(doc.media.photos) > 0:
            assert doc.media.photos[0].url is not None

        if len(doc.media.videos) > 0:
            for x in doc.media.videos:
                assert x.thumbnailUrl is not None
                assert x.duration is not None
                for v in x.variants:
                    assert v.url is not None
                    assert v.bitrate is not None
                    assert v.contentType is not None

    if doc.retweetedTweet is not None:
        try:
            assert doc.rawContent.endswith(doc.retweetedTweet.rawContent), "content should be full"
        except AssertionError as e:
            print("\n" + "-" * 60)
            print(doc.url)
            print("1:", doc.rawContent)
            print("2:", doc.retweetedTweet.rawContent)
            print("-" * 60)
            raise e

    check_user(doc.user)
    assert doc.bookmarkedCount is not None


def check_user(doc: User):
    assert doc.id is not None
    assert isinstance(doc.id, int)
    assert isinstance(doc.id_str, str)
    assert str(doc.id) == doc.id_str

    assert doc.username is not None
    assert doc.descriptionLinks is not None
    assert doc.pinnedIds is not None
    if doc.pinnedIds:
        for x in doc.pinnedIds:
            assert isinstance(x, int)

    if len(doc.descriptionLinks) > 0:
        for x in doc.descriptionLinks:
            assert x.url is not None
            assert x.text is not None
            assert x.tcourl is not None

    obj = doc.dict()
    assert doc.id == obj["id"]
    assert doc.username == obj["username"]

    txt = doc.json()
    assert isinstance(txt, str)
    assert str(doc.id) in txt


def check_user_ref(doc: UserRef):
    assert isinstance(doc.id, int)
    assert isinstance(doc.id_str, str)
    assert str(doc.id) == doc.id_str

    assert doc.username is not None
    assert doc.displayname is not None

    obj = doc.dict()
    assert doc.id == obj["id"]
    assert doc.id_str == obj["id_str"]


def check_trend(doc: Trend):
    assert doc.name is not None
    assert doc.trend_url is not None
    assert doc.trend_metadata is not None
    assert isinstance(doc.grouped_trends, list) or doc.grouped_trends is None

    assert doc.trend_url.url is not None
    assert doc.trend_url.urlType is not None
    assert isinstance(doc.trend_url.urlEndpointOptions, list)


async def test_search():
    api = get_api()
    mock_rep(api.search_raw, "raw_search", as_generator=True)

    items = await gather(api.search("elon musk lang:en", limit=20))
    assert len(items) > 0

    bookmarks_count = 0
    for doc in items:
        check_tweet(doc)
        bookmarks_count += doc.bookmarkedCount

    assert bookmarks_count > 0, "`bookmark_fields` key is changed or unluck search data"


async def test_user_by_id():
    api = get_api()
    mock_rep(api.user_by_id_raw, "raw_user_by_id")

    doc = await api.user_by_id(2244994945)
    assert doc is not None
    assert doc.id == 2244994945
    assert doc.username == "XDevelopers"

    obj = doc.dict()
    assert doc.id == obj["id"]
    assert doc.username == obj["username"]

    txt = doc.json()
    assert isinstance(txt, str)
    assert str(doc.id) in txt


async def test_user_by_login():
    api = get_api()
    mock_rep(api.user_by_login_raw, "raw_user_by_login")

    doc = await api.user_by_login("xdevelopers")
    assert doc is not None
    assert doc.id == 2244994945
    assert doc.username == "XDevelopers"

    obj = doc.dict()
    assert doc.id == obj["id"]
    assert doc.username == obj["username"]

    txt = doc.json()
    assert isinstance(txt, str)
    assert str(doc.id) in txt


async def test_user_parse_current_shape():
    doc = User.parse(
        {
            "__typename": "User",
            "id": 2030572972407435264,
            "rest_id": "2030572972407435264",
            "is_blue_verified": False,
            "core": {
                "created_at": "Sun Mar 08 09:14:58 +0000 2026",
                "name": "Shauna Fatora",
                "screen_name": "SFatora73036",
            },
            "avatar": {
                "image_url": "https://pbs.twimg.com/profile_images/2030572993135435777/N5cWNSt4_normal.jpg"
            },
            "legacy": {
                "description": "",
                "entities": {"description": {"urls": []}},
                "followers_count": 0,
                "friends_count": 2,
                "statuses_count": 5,
                "favourites_count": 0,
                "listed_count": 0,
                "media_count": 5,
                "pinned_tweet_ids_str": [],
            },
            "location": {"location": ""},
            "privacy": {"protected": False},
            "profile_bio": {"description": ""},
            "verification": {"verified": False},
        }
    )

    assert doc.username == "SFatora73036"
    assert doc.displayname == "Shauna Fatora"
    assert doc.id == 2030572972407435264
    assert doc.id_str == "2030572972407435264"
    assert doc.url == "https://x.com/SFatora73036"
    check_user(doc)


async def test_user_ref_parse_current_shape():
    doc = UserRef.parse(
        {
            "__typename": "User",
            "id": 2397143521,
            "rest_id": "2397143521",
            "core": {
                "name": "Dalton Brewer",
                "screen_name": "daltonbrewer",
            },
        }
    )

    assert doc.id == 2397143521
    assert doc.id_str == "2397143521"
    assert doc.username == "daltonbrewer"
    assert doc.displayname == "Dalton Brewer"
    check_user_ref(doc)


async def test_tweet_details():
    api = get_api()
    mock_rep(api.tweet_details_raw, "raw_tweet_details")

    doc = await api.tweet_details(1649191520250245121)
    assert doc is not None, "tweet should not be None"
    check_tweet(doc)

    assert doc.id == 1649191520250245121
    assert doc.user is not None, "tweet.user should not be None"


async def test_tweet_replies():
    api = get_api()
    mock_rep(api.tweet_replies_raw, "raw_tweet_replies", as_generator=True)

    twid = 1649191520250245121
    tweets = await gather(api.tweet_replies(twid, limit=20))
    assert len(tweets) > 0

    for doc in tweets:
        check_tweet(doc)
        assert doc.inReplyToTweetId == twid


async def test_followers():
    api = get_api()
    mock_rep(api.followers_raw, "raw_followers", as_generator=True)

    users = await gather(api.followers(2244994945))
    assert len(users) > 0

    for doc in users:
        check_user(doc)


async def test_verified_followers():
    api = get_api()
    mock_rep(api.verified_followers_raw, "raw_verified_followers", as_generator=True)

    users = await gather(api.verified_followers(2244994945))
    assert len(users) > 0

    for doc in users:
        check_user(doc)
        assert doc.blue is True, "snould be only Blue users"


async def test_subscriptions():
    api = get_api()
    mock_rep(api.subscriptions_raw, "raw_subscriptions", as_generator=True)

    users = await gather(api.subscriptions(44196397))
    assert len(users) > 0

    for doc in users:
        check_user(doc)


async def test_following():
    api = get_api()
    mock_rep(api.following_raw, "raw_following", as_generator=True)

    users = await gather(api.following(2244994945))
    assert len(users) > 0

    for doc in users:
        check_user(doc)


async def test_retweters():
    api = get_api()
    mock_rep(api.retweeters_raw, "raw_retweeters", as_generator=True)

    users = await gather(api.retweeters(1649191520250245121))
    assert len(users) > 0

    for doc in users:
        check_user(doc)


async def test_user_tweets():
    api = get_api()
    mock_rep(api.user_tweets_raw, "raw_user_tweets", as_generator=True)

    tweets = await gather(api.user_tweets(2244994945))
    assert len(tweets) > 0

    is_any_pinned = False
    for doc in tweets:
        check_tweet(doc)
        is_any_pinned = is_any_pinned or doc.id in doc.user.pinnedIds

    assert is_any_pinned, "at least one tweet should be pinned (or bad luck with data)"


async def test_user_tweets_and_replies():
    api = get_api()
    mock_rep(api.user_tweets_and_replies_raw, "raw_user_tweets_and_replies", as_generator=True)

    tweets = await gather(api.user_tweets_and_replies(2244994945))
    assert len(tweets) > 0

    for doc in tweets:
        check_tweet(doc)


async def test_raw_user_media():
    api = get_api()
    mock_rep(api.user_media_raw, "raw_user_media", as_generator=True)

    tweets = await gather(api.user_media(2244994945))
    assert len(tweets) > 0

    for doc in tweets:
        check_tweet(doc)
        assert doc.media is not None
        media_count = len(doc.media.photos) + len(doc.media.videos) + len(doc.media.animated)
        assert media_count > 0, f"{doc.url} should have media"


async def test_list_timeline():
    api = get_api()
    mock_rep(api.list_timeline_raw, "raw_list_timeline", as_generator=True)

    tweets = await gather(api.list_timeline(1494877848087187461))
    assert len(tweets) > 0

    for doc in tweets:
        check_tweet(doc)


async def test_trends():
    api = get_api()
    mock_rep(api.trends_raw, "raw_trends", as_generator=True)

    items = await gather(api.trends("sport"))
    assert len(items) > 0

    for doc in items:
        check_trend(doc)


async def test_trend_parse_without_url_endpoint_options():
    doc = Trend.parse(
        {
            "name": "Developers",
            "rank": "1",
            "trend_url": {
                "url": "twitter://trending/?trend_name=Developers",
                "urlType": "DeepLink",
            },
            "trend_metadata": {
                "url": {
                    "url": "twitter://trending/?trend_name=Developers",
                    "urlType": "DeepLink",
                },
            },
            "grouped_trends": [
                {
                    "name": "Open Source",
                    "url": {
                        "url": "twitter://trending/?trend_name=Open%20Source",
                        "urlType": "DeepLink",
                    },
                }
            ],
        }
    )

    assert doc.rank == 1
    assert doc.trend_url.urlEndpointOptions == []
    assert doc.trend_metadata.domain_context == ""
    assert doc.trend_metadata.meta_description == ""
    assert doc.trend_metadata.url.urlEndpointOptions == []
    assert doc.grouped_trends[0].url.urlEndpointOptions == []


async def test_tweet_with_video():
    api = get_api()

    files = [
        ("manual_tweet_with_video_1.json", 1671508600538161153),
        ("manual_tweet_with_video_2.json", 1671753569412820992),
    ]

    for file, twid in files:
        mock_rep(api.tweet_details_raw, file)
        doc = await api.tweet_details(twid)
        assert doc is not None
        check_tweet(doc)


async def test_issue_28():
    api = get_api()

    mock_rep(api.tweet_details_raw, "_issue_28_1")
    doc = await api.tweet_details(1658409412799737856)
    assert doc is not None
    check_tweet(doc)

    assert doc.id == 1658409412799737856
    assert doc.user is not None

    assert doc.retweetedTweet is not None
    assert doc.retweetedTweet.viewCount is not None
    assert doc.viewCount is not None  # views should come from retweetedTweet
    assert doc.viewCount == doc.retweetedTweet.viewCount
    check_tweet(doc.retweetedTweet)

    mock_rep(api.tweet_details_raw, "_issue_28_2")
    doc = await api.tweet_details(1658421690001502208)
    assert doc is not None
    check_tweet(doc)
    assert doc.id == 1658421690001502208
    assert doc.viewCount is not None

    assert doc.quotedTweet is not None
    assert doc.quotedTweet.id != doc.id
    check_tweet(doc.quotedTweet)
    assert doc.quotedTweet.viewCount is not None


async def test_issue_42():
    raw = fake_rep("_issue_42").json()
    doc = parse_tweet(raw, 1665951747842641921)
    assert doc is not None
    assert doc.retweetedTweet is not None
    assert doc.rawContent is not None
    assert doc.retweetedTweet.rawContent is not None
    assert doc.rawContent.endswith(doc.retweetedTweet.rawContent)


async def test_issue_56():
    raw = fake_rep("_issue_56").json()
    doc = parse_tweet(raw, 1682072224013099008)
    assert doc is not None
    assert len(set([x.tcourl for x in doc.links])) == len(doc.links)
    assert len(doc.links) == 5


async def test_cards():
    # Issues:
    # - https://github.com/vladkens/twscrape/issues/72
    # - https://github.com/vladkens/twscrape/issues/191

    # Check SummaryCard
    raw = fake_rep("card_summary").json()
    doc = parse_tweet(raw, 1696922210588410217)
    assert doc is not None
    assert doc.card is not None
    assert isinstance(doc.card, SummaryCard)
    assert doc.card._type == "summary"
    assert doc.card.title is not None
    assert doc.card.description is not None
    assert doc.card.url is not None

    # Check PollCard
    raw = fake_rep("card_poll").json()
    doc = parse_tweet(raw, 1780666831310877100)
    assert doc is not None
    assert doc.card is not None
    assert isinstance(doc.card, PollCard)
    assert doc.card._type == "poll"
    assert doc.card.finished is not None
    assert doc.card.options is not None
    assert len(doc.card.options) > 0
    for x in doc.card.options:
        assert x.label is not None
        assert x.votesCount is not None

    # Check BrodcastCard
    raw = fake_rep("card_broadcast").json()
    doc = parse_tweet(raw, 1790441814857826439)
    assert doc is not None and doc.card is not None
    assert doc.card._type == "broadcast"
    assert isinstance(doc.card, BroadcastCard)
    assert doc.card.title is not None
    assert doc.card.url is not None
    assert doc.card.photo is not None

    # Check AudiospaceCard
    raw = fake_rep("card_audiospace").json()
    doc = parse_tweet(raw, 1789054061729173804)
    assert doc is not None and doc.card is not None
    assert doc.card._type == "audiospace"
    assert isinstance(doc.card, AudiospaceCard)
    assert doc.card.url is not None


def test_poll_choice_images_card_is_parsed_as_poll_card():
    card = models_module._parse_card(
        {
            "card": {
                "legacy": {
                    "name": "1906814671912599552:poll_choice_images",
                    "binding_values": [
                        {
                            "key": "choice1_label",
                            "value": {"type": "STRING", "string_value": "Bullish"},
                        },
                        {
                            "key": "choice1_count",
                            "value": {"type": "STRING", "string_value": "12"},
                        },
                        {
                            "key": "choice2_label",
                            "value": {"type": "STRING", "string_value": "Bearish"},
                        },
                        {
                            "key": "choice2_count",
                            "value": {"type": "STRING", "string_value": "8"},
                        },
                        {
                            "key": "counts_are_final",
                            "value": {"type": "BOOLEAN", "boolean_value": False},
                        },
                        {
                            "key": "choice1_image",
                            "value": {
                                "type": "IMAGE",
                                "image_value": {
                                    "height": 120,
                                    "width": 120,
                                    "url": "https://example.com/bullish.jpg",
                                },
                            },
                        },
                    ],
                }
            }
        },
        "https://x.com/StockSandbox/status/2045183670500401612",
    )

    assert isinstance(card, PollCard)
    assert card.finished is False
    assert [(option.label, option.votesCount) for option in card.options] == [
        ("Bullish", 12),
        ("Bearish", 8),
    ]


def test_parse_tweets_aborts_after_too_many_item_failures(tmp_path, monkeypatch):
    tweets = {str(idx): {"id": idx} for idx in range(4)}

    monkeypatch.setattr("twscrape.models.PARSE_ERROR_DUMP_DIR", str(tmp_path))
    monkeypatch.setattr("twscrape.models.PARSE_ERROR_DUMP_LIMIT", 2)
    monkeypatch.setattr("twscrape.models.PARSE_ERROR_LIMIT_PER_RESPONSE", 3)
    models_module.PARSE_ERROR_DUMP_WRITER.reset()
    monkeypatch.setattr("twscrape.models.to_old_rep", lambda rep: {"tweets": tweets, "users": {}})
    monkeypatch.setattr(
        "twscrape.models.Tweet.parse",
        lambda obj, res: (_ for _ in ()).throw(ValueError(f"boom-{obj['id']}")),
    )

    with pytest.raises(ParseDriftError, match="3 item failures"):
        list(parse_tweets({}))

    dumps = sorted(tmp_path.iterdir())
    assert len(dumps) == 2


def test_parse_tweets_falls_back_to_embedded_author_when_users_map_is_missing(monkeypatch):
    raw = fake_rep("raw_search").json()
    old = models_module.to_old_rep(raw)

    tweet_id, tweet_obj = next(iter(old["tweets"].items()))
    user_id = tweet_obj["user_id_str"]
    old["tweets"] = {tweet_id: tweet_obj}
    old["users"] = {}

    monkeypatch.setattr("twscrape.models.to_old_rep", lambda rep: old)

    docs = list(parse_tweets(raw))

    assert len(docs) == 1
    doc = docs[0]
    assert doc.user.id_str == user_id
    assert doc.user.username == "PengellySt72806"
