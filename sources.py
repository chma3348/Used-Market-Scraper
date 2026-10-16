"""Marketplace listing fetchers: eBay (official Browse API) and Craigslist."""

import base64
import html
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import List, Optional

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

MARKETPLACE_CURRENCY = {
    "EBAY_US": "USD",
    "EBAY_GB": "GBP",
    "EBAY_CA": "CAD",
    "EBAY_AU": "AUD",
    "EBAY_DE": "EUR",
    "EBAY_FR": "EUR",
    "EBAY_IT": "EUR",
    "EBAY_ES": "EUR",
}


@dataclass
class Listing:
    id: str          # globally unique: "<source>:<native id>"
    source: str      # "ebay" | "craigslist"
    title: str
    price: float
    url: str
    condition: str = ""
    extra: dict = field(default_factory=dict)


def _http_get(url, headers=None, timeout=30):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


# ---------------------------------------------------------------- eBay

class EbayAuthError(Exception):
    pass


def ebay_get_token(client_id: str, client_secret: str) -> str:
    """Client-credentials OAuth token for the Browse API (valid ~2h)."""
    creds = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    body = urllib.parse.urlencode(
        {"grant_type": "client_credentials",
         "scope": "https://api.ebay.com/oauth/api_scope"}
    ).encode()
    req = urllib.request.Request(
        "https://api.ebay.com/identity/v1/oauth2/token",
        data=body,
        headers={
            "Authorization": "Basic " + creds,
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())["access_token"]
    except urllib.error.HTTPError as e:
        raise EbayAuthError(
            f"eBay auth failed (HTTP {e.code}). Check ebay.client_id / "
            f"ebay.client_secret in config.json. Response: {e.read()[:300]!r}"
        )


def total_cost(listing):
    """What the buyer actually pays: price + shipping (0 when free or
    local pickup, price alone when shipping is unknown/calculated)."""
    ship = (listing.extra or {}).get("shipping")
    return listing.price + (ship or 0)


def fetch_ebay(token: str, query: str, min_price: Optional[float],
               max_price: Optional[float], marketplace: str = "EBAY_US",
               include_auctions: bool = False, limit: int = 200,
               pages: int = 1, sort: Optional[str] = None) -> List[Listing]:
    filters = []
    if min_price is not None or max_price is not None:
        lo = "" if min_price is None else f"{min_price:g}"
        hi = "" if max_price is None else f"{max_price:g}"
        currency = MARKETPLACE_CURRENCY.get(marketplace, "USD")
        filters.append(f"price:[{lo}..{hi}]")
        filters.append(f"priceCurrency:{currency}")
    buying = "{FIXED_PRICE|AUCTION}" if include_auctions else "{FIXED_PRICE}"
    filters.append(f"buyingOptions:{buying}")

    summaries = []
    for page in range(max(1, pages)):
        params = {"q": query, "limit": str(limit),
                  "offset": str(page * limit), "filter": ",".join(filters)}
        if sort:
            params["sort"] = sort
        url = ("https://api.ebay.com/buy/browse/v1/item_summary/search?"
               + urllib.parse.urlencode(params))
        raw = _http_get(url, headers={
            "Authorization": "Bearer " + token,
            "X-EBAY-C-MARKETPLACE-ID": marketplace,
        })
        batch = json.loads(raw).get("itemSummaries", [])
        summaries.extend(batch)
        if len(batch) < limit:
            break  # no further pages

    listings = []
    for item in summaries:
        opts = item.get("buyingOptions", [])
        # Auction-only listings: the actionable number is the current bid, and
        # the listing has an end time. Auctions WITH a Buy It Now option are
        # treated as fixed-price (you can buy them right now at `price`).
        auction_only = "AUCTION" in opts and "FIXED_PRICE" not in opts
        price_obj = item.get("price") or {}
        if auction_only and item.get("currentBidPrice"):
            price_obj = item["currentBidPrice"]
        try:
            price = float(price_obj["value"])
        except (KeyError, ValueError, TypeError):
            continue
        # first shipping option's cost; None = calculated/unknown shipping
        shipping = None
        ship_opts = item.get("shippingOptions") or []
        if ship_opts:
            cost = ship_opts[0].get("shippingCost") or {}
            try:
                shipping = float(cost["value"])
            except (KeyError, ValueError, TypeError):
                shipping = None
        listings.append(Listing(
            id="ebay:" + item["itemId"],
            source="ebay",
            title=item.get("title", ""),
            price=price,
            url=item.get("itemWebUrl", ""),
            condition=item.get("condition", ""),
            extra={
                "buying_options": opts,
                "auction": auction_only,
                "end_date": item.get("itemEndDate", ""),
                "shipping": shipping,
            },
        ))
    return listings


# ------------------------------------------------------ item descriptions

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _clean_text(html_text, cap=700):
    text = html.unescape(_TAG_RE.sub(" ", html_text or ""))
    return _WS_RE.sub(" ", text).strip()[:cap]


def fetch_ebay_item_description(token, native_id, marketplace="EBAY_US"):
    url = ("https://api.ebay.com/buy/browse/v1/item/"
           + urllib.parse.quote(native_id, safe=""))
    raw = _http_get(url, headers={
        "Authorization": "Bearer " + token,
        "X-EBAY-C-MARKETPLACE-ID": marketplace,
    })
    data = json.loads(raw)
    parts = [data.get("shortDescription") or "", data.get("description") or ""]
    condition_desc = data.get("conditionDescription") or ""
    if condition_desc:
        parts.insert(0, "Condition note: " + condition_desc)
    return _clean_text(" ".join(p for p in parts if p))


_CL_BODY_RE = re.compile(
    r'<section id="postingbody"[^>]*>(.*?)</section>', re.DOTALL)


def fetch_craigslist_description(url):
    page = _http_get(url, headers={"User-Agent": USER_AGENT})
    m = _CL_BODY_RE.search(page)
    if not m:
        return ""
    text = _clean_text(m.group(1))
    return text.replace("QR Code Link to This Post", "").strip()


def fetch_descriptions(listings, ebay_token, marketplace="EBAY_US"):
    """Fill listing.extra['description'] in place (best effort, '' on
    failure). Skips listings that already have one."""
    for listing in listings:
        if listing.extra.get("description") is not None:
            continue
        text = ""
        try:
            if listing.source == "ebay" and ebay_token:
                text = fetch_ebay_item_description(
                    ebay_token, listing.id.split("ebay:", 1)[1], marketplace)
            elif listing.source == "craigslist":
                text = fetch_craigslist_description(listing.url)
        except Exception:
            pass
        listing.extra["description"] = text
    return listings


# ------------------------------------------------- eBay sold (page scrape)
# eBay's public API has no sold/completed data (that's a restricted partner
# API), so sold comps come from the public sold-listings search page. Fetched
# only for on-demand price checks - never in scheduled scans. Uses the system
# curl (HTTP/2 + compression) because eBay bot-blocks plain Python HTTP.

_EBAY_DOMAIN = {
    "EBAY_US": "www.ebay.com", "EBAY_GB": "www.ebay.co.uk",
    "EBAY_CA": "www.ebay.ca", "EBAY_AU": "www.ebay.com.au",
    "EBAY_DE": "www.ebay.de", "EBAY_FR": "www.ebay.fr",
    "EBAY_IT": "www.ebay.it", "EBAY_ES": "www.ebay.es",
}

_SOLD_TITLE_RE = re.compile(r's-card__title[^>]*><span[^>]*>([^<]{3,200})<')
_SOLD_PRICE_RE = re.compile(r's-card__price[^>]*>([^<]{1,60})<')
_SOLD_LINK_RE = re.compile(r'href="?(https?://[^"\s>]*/itm/(\d+)[^"\s>]*)')
_PRICE_TOKEN_RE = re.compile(r'([A-Z]{0,3}\s?[$£€])\s?([\d.,]+)')

# Legacy search markup (still served in some regions / experiments)
_SOLD_TITLE_LEGACY_RE = re.compile(r's-item__title[^>]*>(?:<span[^>]*>)?([^<]{3,200})<')
_SOLD_PRICE_LEGACY_RE = re.compile(r's-item__price[^>]*>(?:<span[^>]*>)?([^<]{1,60})<')


def _parse_price_text(text):
    """'MXN $6,916.95' -> (6916.95, 'MXN $');  '$20.00' -> (20.0, '$')."""
    m = _PRICE_TOKEN_RE.search(html.unescape(text))
    if not m:
        return None, ""
    currency, num = m.group(1).strip(), m.group(2)
    if "," in num and "." in num:
        if num.rfind(",") > num.rfind("."):
            num = num.replace(".", "").replace(",", ".")
        else:
            num = num.replace(",", "")
    elif "," in num:
        tail = num.rsplit(",", 1)[1]
        num = num.replace(",", "." if len(tail) == 2 else "")
    try:
        return float(num), currency
    except ValueError:
        return None, ""


def _http_get_hard(url, timeout=25):
    """Fetch via system curl with browser-grade headers (HTTP/2, compressed)."""
    import subprocess
    safari_ua = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                 "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                 "Version/17.4 Safari/605.1.15")
    result = subprocess.run(
        ["curl", "-s", "--http2", "--compressed", "--max-time", str(timeout),
         "-H", f"User-Agent: {safari_ua}",
         "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
         "-H", "Accept-Language: en-US,en;q=0.9",
         "-H", "Upgrade-Insecure-Requests: 1",
         "-H", "Sec-Fetch-Dest: document", "-H", "Sec-Fetch-Mode: navigate",
         "-H", "Sec-Fetch-Site: none",
         url],
        capture_output=True, text=True, timeout=timeout + 10)
    return result.stdout


def fetch_ebay_sold(query, marketplace="EBAY_US", limit=60):
    """Recently sold eBay listings for a query. Returns (listings, currency).
    Prices are shown in the currency eBay picks for your location - the
    currency label is returned so callers can display it honestly."""
    domain = _EBAY_DOMAIN.get(marketplace, "www.ebay.com")
    url = (f"https://{domain}/sch/i.html?"
           + urllib.parse.urlencode({"_nkw": query, "LH_Sold": "1",
                                     "LH_Complete": "1", "_ipg": str(limit)}))
    page = _http_get_hard(url)
    if not page or "Error Page" in page[:2000]:
        raise RuntimeError(
            "eBay is rate-limiting sold-price lookups right now - try again "
            "in a few minutes")

    blocks = re.split(r'class="su-card', page)[1:]
    title_re, price_re = _SOLD_TITLE_RE, _SOLD_PRICE_RE
    if len(blocks) < 3:
        blocks = re.split(r'class="s-item\b', page)[1:]
        title_re, price_re = _SOLD_TITLE_LEGACY_RE, _SOLD_PRICE_LEGACY_RE

    listings, currencies, seen = [], {}, set()
    for block in blocks:
        title_m = title_re.search(block)
        price_m = price_re.search(block)
        if not title_m or not price_m:
            continue
        title = html.unescape(title_m.group(1)).strip()
        if not title or title.lower() in ("shop on ebay", "new listing"):
            continue
        price, currency = _parse_price_text(price_m.group(1))
        if price is None:
            continue
        link_m = _SOLD_LINK_RE.search(block)
        item_id = link_m.group(2) if link_m else str(abs(hash(title)))
        if item_id in seen:
            continue
        seen.add(item_id)
        currencies[currency] = currencies.get(currency, 0) + 1
        listings.append(Listing(
            id="ebay-sold:" + item_id,
            source="ebay-sold",
            title=title,
            price=price,
            url=link_m.group(1).split("?")[0] if link_m else "",
            extra={"sold": True, "currency": currency},
        ))
    if not listings:
        # A page came back but with no parseable listings - eBay serves these
        # hollow pages when it's suspicious of the client.
        raise RuntimeError(
            "eBay is rate-limiting sold-price lookups right now - try again "
            "in a few minutes")
    majority = max(currencies, key=currencies.get)
    listings = [l for l in listings if l.extra.get("currency") == majority]
    return listings, majority


# ---------------------------------------------------------- Craigslist

_CL_ITEM_RE = re.compile(
    r'<li[^>]*class="cl-static-search-result"[^>]*title="([^"]*)"(.*?)</li>',
    re.DOTALL,
)
_CL_HREF_RE = re.compile(r'href="([^"]+)"')
_CL_PRICE_RE = re.compile(r'<div class="price">\s*\$([\d,]+)')


def fetch_craigslist(site: str, query: str, min_price: Optional[float],
                     max_price: Optional[float]) -> List[Listing]:
    """Scrape Craigslist's static (no-JS) search results page."""
    params = {"query": query}
    if min_price is not None:
        params["min_price"] = str(int(min_price))
    if max_price is not None:
        params["max_price"] = str(int(max_price))
    url = (f"https://{site}.craigslist.org/search/sss?"
           + urllib.parse.urlencode(params))
    page = _http_get(url, headers={"User-Agent": USER_AGENT})

    listings = []
    for title, body in _CL_ITEM_RE.findall(page):
        href = _CL_HREF_RE.search(body)
        price = _CL_PRICE_RE.search(body)
        if not href or not price:
            continue
        link = href.group(1)
        post_id = link.rstrip("/").split("/")[-1].replace(".html", "")
        try:
            value = float(price.group(1).replace(",", ""))
        except ValueError:
            continue
        listings.append(Listing(
            id="craigslist:" + post_id,
            source="craigslist",
            title=html.unescape(title).strip(),
            price=value,
            url=link,
        ))
    return listings
