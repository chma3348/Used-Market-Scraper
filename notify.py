"""Push notifications via ntfy.sh - free, no account, instant phone push."""

import re
import urllib.request

# ntfy caps the body; stay well under it so a long find-list is never cut off
# mid-URL (a truncated link is worse than a missing one).
MAX_BODY = 3800


def _hdr(s):
    """Smuggle UTF-8 through a latin-1 HTTP header - ntfy decodes it back."""
    return str(s).encode("utf-8", "ignore").decode("latin-1", "ignore")


def _action(label, url):
    """One ntfy 'view' button. Commas and semicolons separate fields in the
    Actions header, so a label containing them would split into garbage."""
    label = re.sub(r"[,;\"]", " ", str(label)).strip()[:25]
    return f"view, {label}, {url}"


def send(topic, title, message, click_url=None, priority="high",
         tags="moneybag", actions=None):
    """actions: list of (label, url) rendered as tappable buttons. ntfy shows
    at most three, so pass the three that matter most."""
    headers = {
        "Title": _hdr(title),
        "Priority": priority,
        "Tags": tags,
    }
    if click_url:
        headers["Click"] = click_url
    if actions:
        headers["Actions"] = _hdr("; ".join(
            _action(lbl, url) for lbl, url in actions[:3] if url))
    req = urllib.request.Request(
        "https://ntfy.sh/" + topic,
        data=message[:MAX_BODY].encode("utf-8"),
        headers=headers,
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.status == 200
