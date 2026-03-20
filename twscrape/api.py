from contextlib import aclosing
from typing import Literal

from httpx import Response

from .accounts_pool import AccountsPool
from .logger import logger, set_log_level
from .models import Tweet, User, parse_trends, parse_tweet, parse_tweets, parse_user, parse_users
from .queue_client import QueueClient
from .utils import encode_params, find_obj, get_by_path

# OP_{NAME} – {NAME} should be same as second part of GQL ID (required to auto-update script)
OP_SearchTimeline = "rkp6b4vtR9u7v3naGoOzUQ/SearchTimeline"
OP_UserByRestId = "VQfQ9wwYdk6j_u2O4vt64Q/UserByRestId"
OP_UserByScreenName = "IGgvgiOx4QZndDHuD3x9TQ/UserByScreenName"
OP_TweetDetail = "xIYgDwjboktoFeXe_fgacw/TweetDetail"
OP_Followers = "Enf9DNUZYiT037aersI5gg/Followers"
OP_Following = "ntIPnH1WMBKW--4Tn1q71A/Following"
OP_Retweeters = "i-CI8t2pJD15euZJErEDrg/Retweeters"
OP_UserTweets = "O0epvwaQPUx-bT9YlqlL6w/UserTweets"
OP_UserTweetsAndReplies = "zedqO5hg41Ox6UeAKsWWzA/UserTweetsAndReplies"
OP_ListLatestTweetsTimeline = "fb_6wmHD2dk9D-xYXOQlgw/ListLatestTweetsTimeline"
OP_BlueVerifiedFollowers = "4zBtcnE_c0v8wn1Zx0yF5Q/BlueVerifiedFollowers"
OP_UserCreatorSubscriptions = "Xe0o9ZDBW6UjJTfFn0pdLQ/UserCreatorSubscriptions"
OP_UserMedia = "U1Zgdsu2qjBi8JF74lTmJQ/UserMedia"
OP_Bookmarks = "-LGfdImKeQz0xS_jjUwzlA/Bookmarks"
OP_GenericTimelineById = "IXBKaPnXncdeAOoCEgco4A/GenericTimelineById"

GQL_URL = "https://x.com/i/api/graphql"
GQL_FEATURES = {  # search values here (view source) https://x.com/
    "articles_preview_enabled": False,
    "c9s_tweet_anatomy_moderator_badge_enabled": True,
    "communities_web_enable_tweet_community_results_fetch": True,
    "creator_subscriptions_quote_tweet_preview_enabled": False,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "freedom_of_speech_not_reach_fetch_enabled": True,
    "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
    "longform_notetweets_consumption_enabled": True,
    "longform_notetweets_inline_media_enabled": True,
    "longform_notetweets_rich_text_read_enabled": True,
    "responsive_web_edit_tweet_api_enabled": True,
    "responsive_web_enhance_cards_enabled": False,
    "responsive_web_graphql_exclude_directive_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "responsive_web_media_download_video_enabled": False,
    "responsive_web_twitter_article_tweet_consumption_enabled": True,
    "rweb_tipjar_consumption_enabled": True,
    "rweb_video_timestamps_enabled": True,
    "standardized_nudges_misinfo": True,
    "tweet_awards_web_tipping_enabled": False,
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
    "tweet_with_visibility_results_prefer_gql_media_interstitial_enabled": False,
    "tweetypie_unmention_optimization_enabled": True,
    "verified_phone_label_enabled": False,
    "view_counts_everywhere_api_enabled": True,
    "responsive_web_grok_analyze_button_fetch_trends_enabled": False,
    "premium_content_api_read_enabled": False,
    "profile_label_improvements_pcf_label_in_post_enabled": False,
    "responsive_web_grok_share_attachment_enabled": False,
    "responsive_web_grok_analyze_post_followups_enabled": False,
    "responsive_web_grok_image_annotation_enabled": False,
    "responsive_web_grok_analysis_button_from_backend": False,
    "responsive_web_jetfuel_frame": False,
    "rweb_video_screen_enabled": True,
    "responsive_web_grok_show_grok_translated_post": True,
}

KV = dict | None
TrendId = Literal["trending", "news", "sport", "entertainment"] | str


class API:
    # Note: kv is variables, ft is features from original GQL request
    pool: AccountsPool

    def __init__(
        self,
        pool: AccountsPool | str | None = None,
        debug=False,
        proxy: str | None = None,
        raise_when_no_account=False,
    ):
        if isinstance(pool, AccountsPool):
            self.pool = pool
        elif isinstance(pool, str):
            self.pool = AccountsPool(db_file=pool, raise_when_no_account=raise_when_no_account)
        else:
            self.pool = AccountsPool(raise_when_no_account=raise_when_no_account)

        self.proxy = proxy
        self.debug = debug
        if self.debug:
            set_log_level("DEBUG")

    # general helpers

    def _is_end(self, rep: Response, q: str, res: list, cur: str | None, cnt: int, lim: int):
        new_count = len(res)
        new_total = cnt + new_count

        is_res = new_count > 0
        is_cur = cur is not None
        is_lim = lim > 0 and new_total >= lim

        return rep if is_res else None, new_total, is_cur and not is_lim

    def _get_cursor(self, obj: dict, cursor_type="Bottom") -> str | None:
        if cur := find_obj(obj, lambda x: x.get("cursorType") == cursor_type):
            return cur.get("value")
        return None

    def _get_entries(self, obj: dict) -> list[dict]:
        entries = get_by_path(obj, "entries") or []
        return [
            x
            for x in entries
            if not (
                x["entryId"].startswith("cursor-")
                or x["entryId"].startswith("messageprompt-")
            )
        ]

    def _is_stalled_search_page(
        self,
        cursor: str | None,
        entries: list[dict],
        seen_cursors: set[str],
        seen_pages: set[tuple[str, ...]],
    ) -> bool:
        if cursor is not None and cursor in seen_cursors:
            return True

        page_key = tuple(x["entryId"] for x in entries)
        if page_key and page_key in seen_pages:
            return True

        if cursor is not None:
            seen_cursors.add(cursor)
        if page_key:
            seen_pages.add(page_key)

        return False

    async def _iter_unique(self, gen, parser, limit=-1):
        ids = set()
        async with aclosing(gen) as items:
            async for rep in items:
                for item in parser(rep.json(), -1):
                    if item.id in ids:
                        continue

                    ids.add(item.id)
                    yield item

                    if limit > 0 and len(ids) >= limit:
                        return

    # gql helpers

    async def _gql_items(
        self, op: str, kv: dict, ft: dict | None = None, limit=-1, cursor_type="Bottom"
    ):
        queue, cur, cnt, active = op.split("/")[-1], None, 0, True
        kv, ft = {**kv}, {**GQL_FEATURES, **(ft or {})}
        seen_cursors: set[str] = set()
        seen_pages: set[tuple[str, ...]] = set()

        async with QueueClient(self.pool, queue, self.debug, proxy=self.proxy) as client:
            while active:
                params = {"variables": kv, "features": ft}
                if cur is not None:
                    params["variables"]["cursor"] = cur
                if queue in ("SearchTimeline", "ListLatestTweetsTimeline"):
                    params["fieldToggles"] = {"withArticleRichContentState": False}
                if queue in ("UserMedia",):
                    params["fieldToggles"] = {"withArticlePlainText": False}

                if queue == "SearchTimeline":
                    rep = await client.post(f"{GQL_URL}/{op}", json=params)
                else:
                    rep = await client.get(f"{GQL_URL}/{op}", params=encode_params(params))
                if rep is None:
                    return

                obj = rep.json()
                els = self._get_entries(obj)
                cur = self._get_cursor(obj, cursor_type)

                if queue == "SearchTimeline" and self._is_stalled_search_page(
                    cur, els, seen_cursors, seen_pages
                ):
                    logger.warning("SearchTimeline pagination stalled, stopping repeated page")
                    return

                rep, cnt, active = self._is_end(rep, queue, els, cur, cnt, limit)
                if rep is None:
                    return

                yield rep

    async def _gql_item(self, op: str, kv: dict, ft: dict | None = None):
        ft = ft or {}
        queue = op.split("/")[-1]
        async with QueueClient(self.pool, queue, self.debug, proxy=self.proxy) as client:
            params = {"variables": {**kv}, "features": {**GQL_FEATURES, **ft}}
            return await client.get(f"{GQL_URL}/{op}", params=encode_params(params))

    # search

    async def search_raw(self, q: str, limit=-1, kv: KV = None):
        op = OP_SearchTimeline
        kv = {
            "rawQuery": q,
            "count": 20,
            "product": "Latest",
            "querySource": "typed_query",
            **(kv or {}),
        }
        async with aclosing(self._gql_items(op, kv, limit=limit)) as gen:
            async for x in gen:
                yield x

    async def search(self, q: str, limit=-1, kv: KV = None):
        async for x in self._iter_unique(self.search_raw(q, limit=limit, kv=kv), parse_tweets, limit):
            yield x

    async def search_user(self, q: str, limit=-1, kv: KV = None):
        kv = {"product": "People", **(kv or {})}
        async for x in self._iter_unique(self.search_raw(q, limit=limit, kv=kv), parse_users, limit):
            yield x

    # user_by_id

    async def user_by_id_raw(self, uid: int, kv: KV = None):
        op = OP_UserByRestId
        kv = {"userId": str(uid), "withSafetyModeUserFields": True, **(kv or {})}
        ft = {
            "hidden_profile_likes_enabled": True,
            "highlights_tweets_tab_ui_enabled": True,
            "creator_subscriptions_tweet_preview_api_enabled": True,
            "hidden_profile_subscriptions_enabled": True,
            "responsive_web_twitter_article_notes_tab_enabled": False,
            "subscriptions_feature_can_gift_premium": False,
            "profile_label_improvements_pcf_label_in_post_enabled": False,
        }
        return await self._gql_item(op, kv, ft)

    async def user_by_id(self, uid: int, kv: KV = None) -> User | None:
        rep = await self.user_by_id_raw(uid, kv=kv)
        return parse_user(rep) if rep else None

    # user_by_login

    async def user_by_login_raw(self, login: str, kv: KV = None):
        op = OP_UserByScreenName
        kv = {"screen_name": login, "withSafetyModeUserFields": True, **(kv or {})}
        ft = {
            "highlights_tweets_tab_ui_enabled": True,
            "hidden_profile_likes_enabled": True,
            "creator_subscriptions_tweet_preview_api_enabled": True,
            "hidden_profile_subscriptions_enabled": True,
            "subscriptions_verification_info_verified_since_enabled": True,
            "subscriptions_verification_info_is_identity_verified_enabled": False,
            "responsive_web_twitter_article_notes_tab_enabled": False,
            "subscriptions_feature_can_gift_premium": False,
            "profile_label_improvements_pcf_label_in_post_enabled": False,
        }
        return await self._gql_item(op, kv, ft)

    async def user_by_login(self, login: str, kv: KV = None) -> User | None:
        rep = await self.user_by_login_raw(login, kv=kv)
        return parse_user(rep) if rep else None

    # tweet_details

    async def tweet_details_raw(self, twid: int, kv: KV = None):
        op = OP_TweetDetail
        kv = {
            "focalTweetId": str(twid),
            "with_rux_injections": True,
            "includePromotedContent": True,
            "withCommunity": True,
            "withQuickPromoteEligibilityTweetFields": True,
            "withBirdwatchNotes": True,
            "withVoice": True,
            "withV2Timeline": True,
            **(kv or {}),
        }
        return await self._gql_item(op, kv)

    async def tweet_details(self, twid: int, kv: KV = None) -> Tweet | None:
        rep = await self.tweet_details_raw(twid, kv=kv)
        return parse_tweet(rep, twid) if rep else None

    # tweet_replies
    # note: uses same op as tweet_details, see: https://github.com/vladkens/twscrape/issues/104

    async def tweet_replies_raw(self, twid: int, limit=-1, kv: KV = None):
        op = OP_TweetDetail
        kv = {
            "focalTweetId": str(twid),
            "referrer": "tweet",
            "with_rux_injections": True,
            "includePromotedContent": True,
            "withCommunity": True,
            "withQuickPromoteEligibilityTweetFields": True,
            "withBirdwatchNotes": True,
            "withVoice": True,
            "withV2Timeline": True,
            **(kv or {}),
        }
        async with aclosing(
            self._gql_items(op, kv, limit=limit, cursor_type="ShowMoreThreads")
        ) as gen:
            async for x in gen:
                yield x

    async def tweet_replies(self, twid: int, limit=-1, kv: KV = None):
        async with aclosing(self.tweet_replies_raw(twid, limit=limit, kv=kv)) as gen:
            async for rep in gen:
                for x in parse_tweets(rep.json(), limit):
                    if x.inReplyToTweetId == twid:
                        yield x

    # followers

    async def followers_raw(self, uid: int, limit=-1, kv: KV = None):
        op = OP_Followers
        kv = {"userId": str(uid), "count": 20, "includePromotedContent": False, **(kv or {})}
        ft = {"responsive_web_twitter_article_notes_tab_enabled": False}
        async with aclosing(self._gql_items(op, kv, limit=limit, ft=ft)) as gen:
            async for x in gen:
                yield x

    async def followers(self, uid: int, limit=-1, kv: KV = None):
        async with aclosing(self.followers_raw(uid, limit=limit, kv=kv)) as gen:
            async for rep in gen:
                for x in parse_users(rep.json(), limit):
                    yield x

    # verified_followers

    async def verified_followers_raw(self, uid: int, limit=-1, kv: KV = None):
        op = OP_BlueVerifiedFollowers
        kv = {"userId": str(uid), "count": 20, "includePromotedContent": False, **(kv or {})}
        ft = {
            "responsive_web_twitter_article_notes_tab_enabled": True,
        }
        async with aclosing(self._gql_items(op, kv, limit=limit, ft=ft)) as gen:
            async for x in gen:
                yield x

    async def verified_followers(self, uid: int, limit=-1, kv: KV = None):
        async with aclosing(self.verified_followers_raw(uid, limit=limit, kv=kv)) as gen:
            async for rep in gen:
                for x in parse_users(rep.json(), limit):
                    yield x

    # following

    async def following_raw(self, uid: int, limit=-1, kv: KV = None):
        op = OP_Following
        kv = {"userId": str(uid), "count": 20, "includePromotedContent": False, **(kv or {})}
        async with aclosing(self._gql_items(op, kv, limit=limit)) as gen:
            async for x in gen:
                yield x

    async def following(self, uid: int, limit=-1, kv: KV = None):
        async with aclosing(self.following_raw(uid, limit=limit, kv=kv)) as gen:
            async for rep in gen:
                for x in parse_users(rep.json(), limit):
                    yield x

    # subscriptions

    async def subscriptions_raw(self, uid: int, limit=-1, kv: KV = None):
        op = OP_UserCreatorSubscriptions
        kv = {"userId": str(uid), "count": 20, "includePromotedContent": False, **(kv or {})}
        async with aclosing(self._gql_items(op, kv, limit=limit)) as gen:
            async for x in gen:
                yield x

    async def subscriptions(self, uid: int, limit=-1, kv: KV = None):
        async with aclosing(self.subscriptions_raw(uid, limit=limit, kv=kv)) as gen:
            async for rep in gen:
                for x in parse_users(rep.json(), limit):
                    yield x

    # retweeters

    async def retweeters_raw(self, twid: int, limit=-1, kv: KV = None):
        op = OP_Retweeters
        kv = {"tweetId": str(twid), "count": 20, "includePromotedContent": True, **(kv or {})}
        async with aclosing(self._gql_items(op, kv, limit=limit)) as gen:
            async for x in gen:
                yield x

    async def retweeters(self, twid: int, limit=-1, kv: KV = None):
        async with aclosing(self.retweeters_raw(twid, limit=limit, kv=kv)) as gen:
            async for rep in gen:
                for x in parse_users(rep.json(), limit):
                    yield x

    # user_tweets

    async def user_tweets_raw(self, uid: int, limit=-1, kv: KV = None):
        op = OP_UserTweets
        kv = {
            "userId": str(uid),
            "count": 40,
            "includePromotedContent": True,
            "withQuickPromoteEligibilityTweetFields": True,
            "withVoice": True,
            "withV2Timeline": True,
            **(kv or {}),
        }
        async with aclosing(self._gql_items(op, kv, limit=limit)) as gen:
            async for x in gen:
                yield x

    async def user_tweets(self, uid: int, limit=-1, kv: KV = None):
        async with aclosing(self.user_tweets_raw(uid, limit=limit, kv=kv)) as gen:
            async for rep in gen:
                for x in parse_tweets(rep.json(), limit):
                    yield x

    # user_tweets_and_replies

    async def user_tweets_and_replies_raw(self, uid: int, limit=-1, kv: KV = None):
        op = OP_UserTweetsAndReplies
        kv = {
            "userId": str(uid),
            "count": 40,
            "includePromotedContent": True,
            "withCommunity": True,
            "withVoice": True,
            "withV2Timeline": True,
            **(kv or {}),
        }
        async with aclosing(self._gql_items(op, kv, limit=limit)) as gen:
            async for x in gen:
                yield x

    async def user_tweets_and_replies(self, uid: int, limit=-1, kv: KV = None):
        async with aclosing(self.user_tweets_and_replies_raw(uid, limit=limit, kv=kv)) as gen:
            async for rep in gen:
                for x in parse_tweets(rep.json(), limit):
                    yield x

    # user_media

    async def user_media_raw(self, uid: int, limit=-1, kv: KV = None):
        op = OP_UserMedia
        kv = {
            "userId": str(uid),
            "count": 40,
            "includePromotedContent": False,
            "withClientEventToken": False,
            "withBirdwatchNotes": False,
            "withVoice": True,
            "withV2Timeline": True,
            **(kv or {}),
        }

        async with aclosing(self._gql_items(op, kv, limit=limit)) as gen:
            async for x in gen:
                yield x

    async def user_media(self, uid: int, limit=-1, kv: KV = None):
        async with aclosing(self.user_media_raw(uid, limit=limit, kv=kv)) as gen:
            async for rep in gen:
                for x in parse_tweets(rep, limit):
                    # sometimes some tweets without media, so skip them
                    media_count = (
                        len(x.media.photos) + len(x.media.videos) + len(x.media.animated)
                        if x.media
                        else 0
                    )

                    if media_count > 0:
                        yield x

    # list_timeline

    async def list_timeline_raw(self, list_id: int, limit=-1, kv: KV = None):
        op = OP_ListLatestTweetsTimeline
        kv = {"listId": str(list_id), "count": 20, **(kv or {})}
        async with aclosing(self._gql_items(op, kv, limit=limit)) as gen:
            async for x in gen:
                yield x

    async def list_timeline(self, list_id: int, limit=-1, kv: KV = None):
        async with aclosing(self.list_timeline_raw(list_id, limit=limit, kv=kv)) as gen:
            async for rep in gen:
                for x in parse_tweets(rep, limit):
                    yield x

    # trends

    async def trends_raw(self, trend_id: TrendId, limit=-1, kv: KV = None):
        map = {
            "trending": "VGltZWxpbmU6DAC2CwABAAAACHRyZW5kaW5nAAA",
            "news": "VGltZWxpbmU6DAC2CwABAAAABG5ld3MAAA",
            "sport": "VGltZWxpbmU6DAC2CwABAAAABnNwb3J0cwAA",
            "entertainment": "VGltZWxpbmU6DAC2CwABAAAADWVudGVydGFpbm1lbnQAAA",
        }
        trend_id = map.get(trend_id, trend_id)

        op = OP_GenericTimelineById
        kv = {
            "timelineId": trend_id,
            "count": 20,
            "withQuickPromoteEligibilityTweetFields": True,
            **(kv or {}),
        }
        async with aclosing(self._gql_items(op, kv, limit=limit)) as gen:
            async for x in gen:
                yield x

    async def trends(self, trend_id: TrendId, limit=-1, kv: KV = None):
        async with aclosing(self.trends_raw(trend_id, limit=limit, kv=kv)) as gen:
            async for rep in gen:
                for x in parse_trends(rep, limit):
                    yield x

    async def search_trend(self, q: str, limit=-1, kv: KV = None):
        kv = {
            "querySource": "trend_click",
            **(kv or {}),
        }
        async with aclosing(self.search_raw(q, limit=limit, kv=kv)) as gen:
            async for rep in gen:
                for x in parse_tweets(rep.json(), limit):
                    yield x

    # Get current user bookmarks

    async def bookmarks_raw(self, limit=-1, kv: KV = None):
        op = OP_Bookmarks
        kv = {
            "count": 20,
            "includePromotedContent": False,
            "withClientEventToken": False,
            "withBirdwatchNotes": False,
            "withVoice": True,
            "withV2Timeline": True,
            **(kv or {}),
        }
        ft = {
            "graphql_timeline_v2_bookmark_timeline": True,
        }
        async with aclosing(self._gql_items(op, kv, ft, limit=limit)) as gen:
            async for x in gen:
                yield x

    async def bookmarks(self, limit=-1, kv: KV = None):
        async with aclosing(self.bookmarks_raw(limit=limit, kv=kv)) as gen:
            async for rep in gen:
                for x in parse_tweets(rep.json(), limit):
                    yield x
