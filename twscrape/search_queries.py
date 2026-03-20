from datetime import datetime, timedelta, timezone


def build_stock_cashtag_query(
    ticker: str,
    since: datetime,
    min_faves: int = 2,
    *,
    until: datetime | None = None,
    lang: str = "en",
    exclude_links: bool = True,
) -> str:
    ticker = ticker.strip().upper().removeprefix("$")
    if not ticker:
        raise ValueError("ticker must not be empty")

    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    else:
        since = since.astimezone(timezone.utc)

    if until is None:
        until = datetime.now(timezone.utc) + timedelta(days=1)
    elif until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    else:
        until = until.astimezone(timezone.utc)

    parts = [
        f"${ticker}",
        f"min_faves:{min_faves}",
    ]
    if lang:
        parts.append(f"lang:{lang}")
    parts.extend(
        [
            f"until:{until.strftime('%Y-%m-%d')}",
            f"since:{since.strftime('%Y-%m-%d')}",
        ]
    )
    if exclude_links:
        parts.append("-filter:links")
    return " ".join(parts)
