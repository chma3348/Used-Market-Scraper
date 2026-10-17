---
name: scan-screen
description: Screen fetched marketplace listings for a scheduled DealFinder scan and write the verdicts to cloud/work/results.json.
allowed-tools: Read, Write, Edit, Glob, Grep
---

# Scheduled scan screening

You are the screening stage of an automated deal scan. Phase 1 already
fetched listings and removed the obvious junk for free. Your job is the
judgement call that keyword matching cannot make.

## Do exactly this

1. Read `cloud/work/candidates.json`.
2. For every search in it, judge each listing.
3. Write `cloud/work/results.json` in the schema below. Write that file even
   if you flag nothing — an empty `deals` array is a valid, useful answer.

```json
{
  "searches": [
    {
      "name": "<copy the search name exactly>",
      "note": "<one sentence on what this market looks like right now>",
      "finds": [
        {"i": 12, "what": "<3-6 words, e.g. 'F3 HP body, meter works'>"}
      ],
      "deals": [
        {"i": 7, "reason": "<ONE short clause, under 90 characters — this is read on a phone lock screen>"}
      ]
    }
  ]
}
```

`finds` is **every listing that is genuinely the item being searched for**,
cheapest first, up to 12 — the real thing, even when nowhere near a deal.
This is the list the buyer browses on their phone, so it is the most
important field here: a scan that flags no deals but returns eight real
Nikon F3 bodies has still done its job, while one that returns an empty
`finds` has shown the buyer nothing.

Include a listing in `finds` when it is the right item in the right
condition, whatever the price. Leave `finds` empty **only** when not one
listing in the batch is the real thing — "we looked at 60 and none were even
a Nikon F3" is useful and true, and far better than padding the list with
brochures. Deals are a subset: anything you flag in `deals` must also appear
in `finds`.

Keep `reason` short. It becomes a push notification, so it must read at a
glance: `"40mm GIII, $45 under the going rate"` — not a paragraph. Put the
number and the one fact that matters. The full market picture goes in `note`,
which is written to the report rather than pushed.

`i` is the listing's index **from candidates.json**. Never invent an index,
never use a marketplace id, and never include an index you did not see.

## What counts as a deal

Flag a listing only when a sharp buyer would want to act on it today:

- Clearly below what that exact item trades for, judged against the other
  listings in the same search plus what you know about the model.
- Mislabeled or vaguely titled listings whose price looks like the seller
  does not know what they have.
- Auctions still cheap near their end. Ignore auctions with days left — a low
  current bid says nothing yet. `auction_max_hours` gives the window.
- When the search has `alert_under`, anything at or under it that is genuinely
  the right item.

Most listings deserve nothing. Flagging everything is the same as flagging
nothing, because every flag becomes a phone notification.

## Judgement rules that matter here

- **Prices already include shipping.** Judge the all-in number.
- **Wrong variants are the main failure.** Model names collide: a "mju II"
  that is really the zoom, a "QL17" that is the 45mm original rather than the
  40mm GIII, an "AI-S" lens that is plain AI, a 50mm Round Metal titled like
  the 53mm. If the title does not positively show the right variant, say so
  in the reason rather than flagging it confidently.
- **Honour the search's `notes` and `condition`.** `working` means do not
  flag parts/for-repair listings; `parts` means the opposite.
- **Cheap is not the same as a deal.** A broken item at a broken-item price
  is just a price.
- **You only have titles.** You cannot see descriptions or photos, so prefer
  "worth a look" honesty over false confidence, and keep reasons concrete:
  name the model, the comparison, and the number.

## Finishing

Write the file, then state in one line how many deals you flagged across how
many searches. Do not edit any other file in the repository.
