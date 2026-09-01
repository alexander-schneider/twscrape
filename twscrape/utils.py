import base64
import json
import os
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Callable, TypeVar

T = TypeVar("T")


class utc:
    @staticmethod
    def now() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def from_iso(iso: str) -> datetime:
        return datetime.fromisoformat(iso).replace(tzinfo=timezone.utc)

    @staticmethod
    def ts() -> int:
        return int(utc.now().timestamp())


async def gather(gen: AsyncGenerator[T, None]) -> list[T]:
    items = []
    async for x in gen:
        items.append(x)
    return items


def encode_params(obj: dict):
    res = {}
    for k, v in obj.items():
        if isinstance(v, dict):
            v = {a: b for a, b in v.items() if b is not None}
            v = json.dumps(v, separators=(",", ":"))

        res[k] = str(v)

    return res


def get_or(obj: dict, key: str, default_value: T = None) -> Any | T:
    for part in key.split("."):
        if part not in obj:
            return default_value
        obj = obj[part]
    return obj


def int_or(obj: dict, key: str, default_value: int | None = None):
    try:
        val = get_or(obj, key)
        return int(val) if val is not None else default_value
    except Exception:
        return default_value


# https://stackoverflow.com/a/43184871
def get_by_path(obj: dict, key: str, default=None):
    stack = [iter(obj.items())]
    while stack:
        for k, v in stack[-1]:
            if k == key:
                return v
            elif isinstance(v, dict):
                stack.append(iter(v.items()))
                break
            elif isinstance(v, list):
                stack.append(iter(enumerate(v)))
                break
        else:
            stack.pop()
    return default


def find_item(lst: list[T], fn: Callable[[T], bool]) -> T | None:
    for item in lst:
        if fn(item):
            return item
    return None


def find_or_fail(lst: list[T], fn: Callable[[T], bool]) -> T:
    item = find_item(lst, fn)
    if item is None:
        raise ValueError()
    return item


def find_obj(obj: dict, fn: Callable[[dict], bool]) -> Any | None:
    if not isinstance(obj, dict):
        return None

    if fn(obj):
        return obj

    for _, v in obj.items():
        if isinstance(v, dict):
            if res := find_obj(v, fn):
                return res
        elif isinstance(v, list):
            for x in v:
                if res := find_obj(x, fn):
                    return res

    return None


def get_typed_object(obj: dict, res: defaultdict[str, list]):
    obj_type = obj.get("__typename", None)
    if obj_type is not None:
        res[obj_type].append(obj)

    for _, v in obj.items():
        if isinstance(v, dict):
            get_typed_object(v, res)
        elif isinstance(v, list):
            for x in v:
                if isinstance(x, dict):
                    get_typed_object(x, res)

    return res


def _get_timeline_tweet_ids(obj: Any) -> set[str]:
    ids: set[str] = set()
    if isinstance(obj, dict):
        entry_id = obj.get("entryId")
        if isinstance(entry_id, str):
            if entry_id.startswith("tweet-"):
                candidate = entry_id.removeprefix("tweet-")
            elif "-tweet-" in entry_id:
                candidate = entry_id.rsplit("-tweet-", 1)[1]
            else:
                candidate = ""
            if candidate.isdigit():
                ids.add(candidate)

        for value in obj.values():
            ids.update(_get_timeline_tweet_ids(value))
    elif isinstance(obj, list):
        for value in obj:
            ids.update(_get_timeline_tweet_ids(value))

    return ids


def _merge_legacy(base: dict, legacy) -> dict:
    out = dict(base)
    if isinstance(legacy, dict):
        for key, value in legacy.items():
            out.setdefault(key, value)
    return out


def _flatten_user_v2(obj: dict) -> dict:
    flat = _merge_legacy(obj, obj.get("legacy"))
    rest_id = obj.get("rest_id") or flat.get("rest_id") or flat.get("id_str")
    flat["rest_id"] = rest_id
    flat["id_str"] = str(rest_id) if rest_id is not None else str(flat.get("id_str") or "")
    flat["id"] = int(rest_id) if rest_id is not None and str(rest_id).isdigit() else 0
    flat["legacy"] = None

    core = obj.get("core") or {}
    if isinstance(core, dict):
        for key in ("screen_name", "name", "created_at"):
            if key not in flat and key in core:
                flat[key] = core[key]

    for key in ("avatar", "privacy", "verification", "profile_bio"):
        if not isinstance(flat.get(key), dict):
            flat[key] = {}

    if not flat.get("profile_image_url_https"):
        avatar = flat["avatar"]
        if isinstance(avatar, dict):
            avatar_url = avatar.get("image_url")
            if avatar_url:
                flat["profile_image_url_https"] = avatar_url

    banner = obj.get("banner")
    if isinstance(banner, dict) and banner.get("image_url") is not None:
        flat["profile_banner_url"] = banner["image_url"]

    if not flat.get("location"):
        location_obj = obj.get("location")
        if isinstance(location_obj, dict):
            location = location_obj.get("location")
            if location is not None:
                flat["location"] = location
        elif location_obj is not None and not isinstance(location_obj, str):
            flat["location"] = ""
    elif not isinstance(flat.get("location"), (dict, str)):
        flat["location"] = ""

    if "protected" not in flat:
        protected = flat["privacy"].get("protected")
        if protected is not None:
            flat["protected"] = protected

    if "verified" not in flat:
        verified = flat["verification"].get("verified")
        if verified is not None:
            flat["verified"] = verified

    if "verified_type" not in flat:
        verified_type = flat["verification"].get("verified_type")
        if verified_type is not None:
            flat["verified_type"] = verified_type

    if "is_blue_verified" not in flat and "is_blue_verified" in obj:
        flat["is_blue_verified"] = obj["is_blue_verified"]

    profile_bio = flat["profile_bio"]
    if not flat.get("description"):
        description = profile_bio.get("description")
        if description is not None:
            flat["description"] = description
    if isinstance(profile_bio.get("entities"), dict):
        flat["entities"] = profile_bio["entities"]

    action_counts = obj.get("action_counts")
    if isinstance(action_counts, dict) and action_counts.get("favorites_count") is not None:
        flat["favourites_count"] = action_counts["favorites_count"]

    relationship_counts = obj.get("relationship_counts")
    if isinstance(relationship_counts, dict):
        if relationship_counts.get("followers") is not None:
            flat["followers_count"] = relationship_counts["followers"]
        if relationship_counts.get("following") is not None:
            flat["friends_count"] = relationship_counts["following"]

    tweet_counts = obj.get("tweet_counts")
    if isinstance(tweet_counts, dict):
        if tweet_counts.get("tweets") is not None:
            flat["statuses_count"] = tweet_counts["tweets"]
        if tweet_counts.get("media_tweets") is not None:
            flat["media_count"] = tweet_counts["media_tweets"]

    pinned_items = obj.get("pinned_items")
    if isinstance(pinned_items, dict) and isinstance(pinned_items.get("tweet_ids_str"), list):
        flat["pinned_tweet_ids_str"] = pinned_items["tweet_ids_str"]

    flat.setdefault("description", "")
    flat.setdefault("location", "")
    flat.setdefault("followers_count", 0)
    flat.setdefault("friends_count", 0)
    flat.setdefault("statuses_count", 0)
    flat.setdefault("favourites_count", 0)
    flat.setdefault("listed_count", 0)
    flat.setdefault("media_count", 0)
    flat.setdefault("profile_image_url_https", "")
    flat.setdefault("entities", {})
    flat.setdefault("pinned_tweet_ids_str", [])
    return flat


def _flatten_tweet_v2(obj: dict) -> dict:
    flat = _merge_legacy(obj, obj.get("legacy"))
    rest_id = obj.get("rest_id") or flat.get("rest_id") or flat.get("id_str")
    flat["rest_id"] = rest_id
    flat["id_str"] = str(rest_id) if rest_id is not None else str(flat.get("id_str") or "")
    flat["id"] = int(rest_id) if rest_id is not None and str(rest_id).isdigit() else 0
    flat["legacy"] = None
    if "source" not in flat and "source" in obj:
        flat["source"] = obj["source"]
    flat.setdefault("full_text", "")
    flat.setdefault("lang", "")
    flat.setdefault("reply_count", 0)
    flat.setdefault("retweet_count", 0)
    flat.setdefault("favorite_count", 0)
    flat.setdefault("quote_count", 0)
    flat.setdefault("bookmark_count", 0)
    flat.setdefault("entities", {})
    flat.setdefault("conversation_id_str", flat["id_str"])
    return flat


def to_old_obj(obj: dict):
    if not isinstance(obj, dict):
        return obj
    if obj.get("__typename") == "User":
        return _flatten_user_v2(obj)
    return _flatten_tweet_v2(obj)


def to_old_rep(obj: dict) -> dict[str, Any]:
    timeline_tweet_ids = _get_timeline_tweet_ids(obj)
    tmp = get_typed_object(obj, defaultdict(list))

    tweets = {}
    for x in tmp.get("Tweet", []):
        if "legacy" not in x:
            continue
        tweet = to_old_obj(x)
        if tweet.get("id_str"):
            tweets[str(tweet["id_str"])] = tweet

    # https://github.com/vladkens/twscrape/issues/53
    tw2 = [x["tweet"] for x in tmp.get("TweetWithVisibilityResults", []) if "legacy" in x["tweet"]]
    for x in tw2:
        tweet = to_old_obj(x)
        if tweet.get("id_str"):
            tweets[str(tweet["id_str"])] = tweet

    users = {}
    for x in tmp.get("User", []):
        if not (x.get("rest_id") or x.get("id_str")):
            continue
        user = to_old_obj(x)
        if user.get("id_str") and user.get("screen_name"):
            users[str(user["id_str"])] = user

    trends = [x for x in tmp.get("TimelineTrend", [])]
    trends = {x["name"]: x for x in trends}

    retweeted_ids = {
        str(retweeted_id)
        for tweet in tweets.values()
        for path in (
            "retweeted_status_id_str",
            "retweeted_status_result.result.rest_id",
            "retweeted_status_result.result.tweet.rest_id",
        )
        if (retweeted_id := get_or(tweet, path)) is not None
    }

    return {
        "tweets": tweets,
        "retweeted_ids": retweeted_ids,
        "timeline_tweet_ids": timeline_tweet_ids,
        "users": users,
        "trends": trends,
    }


def print_table(rows: list[dict], hr_after=False):
    if not rows:
        return

    def prt(x):
        if isinstance(x, str):
            return x

        if isinstance(x, int):
            return f"{x:,}"

        if isinstance(x, datetime):
            return x.isoformat().split("+")[0].replace("T", " ")

        return str(x)

    keys = list(rows[0].keys())
    rows = [{k: k for k in keys}, *[{k: prt(x.get(k, "")) for k in keys} for x in rows]]
    colw = [max(len(x[k]) for x in rows) + 1 for k in keys]

    lines = []
    for row in rows:
        line = [f"{row[k]:<{colw[i]}}" for i, k in enumerate(keys)]
        lines.append(" ".join(line))

    max_len = max(len(x) for x in lines)
    # lines.insert(1, "─" * max_len)
    # lines.insert(0, "─" * max_len)
    print("\n".join(lines))
    if hr_after:
        print("-" * max_len)


def parse_cookies(val: str) -> dict[str, str]:
    try:
        val = base64.b64decode(val).decode()
    except Exception:
        pass

    try:
        try:
            res = json.loads(val)
            if isinstance(res, dict) and "cookies" in res:
                res = res["cookies"]

            if isinstance(res, list):
                return {x["name"]: x["value"] for x in res}
            if isinstance(res, dict):
                return res
        except json.JSONDecodeError:
            res = val.split("; ")
            res = [x.split("=") for x in res]
            return {x[0]: x[1] for x in res}
    except Exception:
        pass

    raise ValueError(f"Invalid cookie value: {val}")


def get_env_bool(key: str, default_val: bool = False) -> bool:
    val = os.getenv(key)
    if val is None:
        return default_val
    return val.lower() in ("1", "true", "yes")
