import pytest

from jobhunt import webhooks

ID = "123456789012345678"
TOK = "aB3-_" * 13  # 65 chars
GOOD = f"https://discord.com/api/webhooks/{ID}/{TOK}"


@pytest.mark.parametrize("host", ["discord.com", "discordapp.com", "ptb.discord.com",
                                  "canary.discord.com", "DISCORD.com"])
def test_accepts_and_normalizes(host):
    wh = webhooks.parse(f"https://{host}/api/webhooks/{ID}/{TOK}")
    assert (wh.id, wh.token) == (ID, TOK)
    assert wh.url == GOOD


def test_trailing_slash_and_whitespace():
    assert webhooks.normalize(f"  {GOOD}/ \n") == GOOD


@pytest.mark.parametrize("bad", [
    GOOD.replace("https", "http"),
    f"https://example.com/api/webhooks/{ID}/{TOK}",
    f"https://discord.com.evil.com/api/webhooks/{ID}/{TOK}",
    f"https://evil.com/discord.com/api/webhooks/{ID}/{TOK}",
    f"https://discord.com/api/webhooks/{ID}/{TOK}/extra",
    f"https://discord.com/api/v10/webhooks/{ID}/{TOK}",
    GOOD + "?wait=true",
    GOOD + "#frag",
    f"https://user:pw@discord.com/api/webhooks/{ID}/{TOK}",
    f"https://discord.com@evil.com/api/webhooks/{ID}/{TOK}",
    f"https://discord.com:8443/api/webhooks/{ID}/{TOK}",
    f"https://discord.com/api/webhooks/{ID}/{'a' * 59}",
    f"https://discord.com/api/webhooks/{ID}/{'a' * 81}",
    f"https://discord.com/api/webhooks/{ID}/{'a' * 59}!",
    f"https://discord.com/api/webhooks/{'1' * 16}/{TOK}",
    f"https://discord.com/api/webhooks/{'1' * 21}/{TOK}",
    f"https://discord.com/api/webhooks/abc{ID[3:]}/{TOK}",
    "", "not a url", None, "ftp://discord.com/api/webhooks/1/2",
])
def test_rejects(bad):
    with pytest.raises(webhooks.WebhookError) as e:
        webhooks.parse(bad)
    assert TOK not in str(e.value)


def test_mask_and_repr_never_expose_token():
    assert webhooks.mask(ID) == "...5678"
    wh = webhooks.parse(GOOD)
    assert TOK not in repr(wh) and ID not in repr(wh)


def test_rejects_non_ascii_digits():
    import pytest as _pt
    from jobhunt import webhooks as _w

    with _pt.raises(_w.WebhookError):
        _w.parse("https://discord.com/api/webhooks/" + "\u0661" * 18 + "/" + "a" * 60)


def test_httpx_loggers_pinned_to_warning():
    import logging

    for name in ("httpx", "httpcore"):
        assert logging.getLogger(name).level == logging.WARNING
