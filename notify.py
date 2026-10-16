"""Push notifications via ntfy.sh - free, no account, instant phone push."""

import urllib.request


def send(topic, title, message, click_url=None, priority="high", tags="moneybag"):
    headers = {
        "Title": title.encode("utf-8", "ignore").decode("latin-1", "ignore"),
        "Priority": priority,
        "Tags": tags,
    }
    if click_url:
        headers["Click"] = click_url
    req = urllib.request.Request(
        "https://ntfy.sh/" + topic,
        data=message.encode("utf-8"),
        headers=headers,
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.status == 200
