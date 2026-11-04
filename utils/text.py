"""Text cleaning and URL helpers."""

from __future__ import annotations

import re
from urllib.parse import urlparse

_LEADING_AT_NAME_TEMPLATE = r"^\s*[@＠]{name}\s*"
_ZERO_WIDTH = re.compile(r"[\u200b\u200c\u200d\ufeff]")
_SPACE = re.compile(r"[ \t]+")
_MENTION_TOKEN = re.compile(r"<@!?([^>\s]+)>")
_HEX_ID = re.compile(r"^[0-9A-Fa-f]{6,}$")


def extract_mention_ids(content: str) -> list[str]:
    """Return the raw targets of ``<@!target>`` style mentions in order."""

    return _MENTION_TOKEN.findall(content or "")


def _mention_replacement(
    match: re.Match[str],
    bot_name: str,
    bot_id: str,
    mention_names: dict[str, str] | None,
    bot_mention_ids: set[str] | None,
) -> str:
    target = match.group(1)
    if (
        (bot_id and target == bot_id)
        or (bot_name and target == bot_name)
        or (bot_mention_ids and target in bot_mention_ids)
    ):
        return " "
    if mention_names and target in mention_names:
        return f" @{mention_names[target]} "
    if _HEX_ID.match(target):
        # QQ encodes other members as opaque openids. Keep a visible marker so
        # the model knows a third party was mentioned, without inventing a name.
        return " @某群友 "
    return f" @{target} "


def clean_message_text(
    content: str,
    bot_name: str = "",
    bot_id: str = "",
    *,
    mention_names: dict[str, str] | None = None,
    bot_mention_ids: set[str] | None = None,
) -> str:
    """Strip @bot mentions and keep @mentions of other people intact.

    QQ may encode mentions as ``<@!openid>`` or plain ``@nickname``.  Only the
    bot's own mention is removed; mentions of other members are preserved (or
    replaced by ``@某群友`` when only an opaque openid is available), otherwise
    a question like ``@bot @Alice 是谁？`` would lose the person being asked
    about.  ``mention_names`` resolves known member openids to names, and
    ``bot_mention_ids`` holds the bot's own per-group openids.
    """

    text = _ZERO_WIDTH.sub("", content or "").strip()
    if not text:
        return ""

    text = _MENTION_TOKEN.sub(
        lambda match: _mention_replacement(
            match, bot_name, bot_id, mention_names, bot_mention_ids
        ),
        text,
    )

    # Plain-text @bot prefix, plus a leading numeric/openid form.
    changed = True
    while changed and text:
        changed = False
        new_text = text
        if bot_name:
            new_text = re.sub(
                _LEADING_AT_NAME_TEMPLATE.format(name=re.escape(bot_name)),
                "",
                new_text,
                count=1,
                flags=re.IGNORECASE,
            )
        if bot_id:
            new_text = re.sub(
                rf"^\s*[@＠]{re.escape(bot_id)}\s*", "", new_text, count=1
            )
        new_text = new_text.strip()
        if new_text != text:
            text = new_text
            changed = True

    return _SPACE.sub(" ", text).strip()


def is_http_url(value: str) -> bool:
    """Return True only for absolute http(s) URLs with a hostname."""

    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    return parsed.scheme.lower() in {"http", "https"} and bool(parsed.netloc)


def truncate_text(value: str, limit: int, suffix: str = "…") -> str:
    if limit <= 0 or len(value) <= limit:
        return value
    if len(suffix) >= limit:
        return suffix[:limit]
    return value[: limit - len(suffix)] + suffix
