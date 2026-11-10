# Selling price sheet

Built from ~2,100 live eBay listings fetched 2026-11-04/10 via the Browse API
and screened by hand, cross-checked against CameraWorth sold data.

→ [**method.md**](method.md) for how these were derived and when to refresh them.
→ [**sold-log.md**](sold-log.md) to record what things actually go for.

**How these numbers were set.** eBay asking prices for vintage gear run high —
the boards are dominated by Japanese sellers grading everything "[MINT]" /
"[Exc+5]" who sit on stock for months. The listings that actually move are near
the **20th–25th percentile** of asks, not the median. CameraWorth's F3 eBay sold
median (£328 ≈ $430) against my $550 ask median confirms the ratio. Every "list"
price below is set against that, then adjusted for the condition you described.

**Local pricing logic.** On eBay you keep ~82% of the list price minus postage.
That net is the right floor for a Facebook/Craigslist price: matching it leaves
you even while still reading as a discount to the buyer.

`net` = list − 13.25% final value fee − $0.40 − postage (free shipping built in).

---

## Nikon

| Item | Condition | eBay list | Realistic | **Net** | **Local** |
|---|---|---|---|---|---|
| **F3 body** | brassed, shutter OK, not film-tested | $375 | $320–360 | $307 | $330 *(take $290)* |
| ├ **MD-4 motor drive** | working | $75 | $60–75 | $51 | $65 |
| └ **SB-16 speedlight** | working | $38 | $30–38 | $22 | $33 |
| *F3 as one kit — not recommended* | | *$450* | *$390–440* | *$368* | *$400 (take $350)* |
| **Nikon F Photomic FTN** | film-tested, working, + leather case | $235 | $190–230 | $185 | $200 *(take $165)* |
| **50mm f/1.4 pre-AI** | good for age | $105 | $85–105 | $80 | $90 *(take $70)* |
| **50mm f/1.8 AI-S** — *pancake* | good, not mint | $160 | $130–160 | $128 | $140 *(take $115)* |
| **50mm f/1.8 AI-S** — *standard barrel* | good, not mint | $95 | $75–95 | $72 | $80 *(take $65)* |
| **35-105mm f/3.5-4.5 AI-S** | good, not mint | $80 | $65–80 | $57 | $70 *(take $50)* |
| **Nikon EM** | body only | $45 | $30–40 | $27 | $40 *(take $30)* |

## Minolta

| Item | Condition | eBay list | Realistic | **Net** | **Local** |
|---|---|---|---|---|---|
| **X-370 + MD 50mm f/1.7** | tested & working | $85 | $65–85 | $58 | $65 *(take $50)* |

## Canon / EF

| Item | Condition | eBay list | Realistic | **Net** | **Local** |
|---|---|---|---|---|---|
| **Tamron SP AF 20-40mm f/2.7-3.5** | EF mount | $175 | $140–170 | $138 | $150 *(take $125)* |
| **Canon EF 50mm f/1.8 STM** | used | $105 | $90–100 | $81 | $85 *(take $70)* |
| **Canon EOS Rebel Ti** | crack by flash, otherwise fine | $45 | $35–45 | $25 | $40 *(take $30)* |
| *bundle: Rebel Ti + 50mm STM* | | *$150* | *$130–150* | *$115* | *$130* |

## Polaroid / large format

| Item | Condition | eBay list | Realistic | **Net** | **Local** |
|---|---|---|---|---|---|
| **Tominon 127mm f/4.7 + 75mm f/4.5, Copal #1 Press** | shutter has a diaphragm | $135 | $100-125 | $103 | eBay only |
| *same, diaphragm-less shutter* | | *$95* | *$75-95* | *$69* | eBay only |

These are Polaroid MP-4 / CU-5 copy-camera lenses. The market is thin - about
11 listings for a 127mm Tominon site-wide - so expect weeks, not days. Closest
comp: "2 Polaroid lenses in shutter (75mm + 135mm) plus single shutter" at $130,
and the 127mm is more desirable than that 135mm.

The two lenses are not equal. The **127mm f/4.7** is the useful one: Tessar-type,
covers 4x5 at infinity, the one large-format shooters recommend out of this
system. The **75mm f/4.5** is a macro lens rated to 4x magnification that will
not cover 4x5 at infinity - narrower buyer pool. Keep them together anyway;
split listings sell slower and net about the same.

**Check the aperture ring before pricing.** With a diaphragm the lens is usable
on a 4x5 and holds $135. Diaphragm-less, the shutter only fires wide open and is
good for little but front-mounting another lens - drop to ~$95. Many MP-4
shutters shipped without one. Fire it at several speeds too; these go sticky
after decades, and "tested, fires at all speeds" is worth real money here.

**Title it for the search, not the object** - this market is fragmented by
naming, which is why it looks invisible online:

> Polaroid MP-4 Tominon 127mm f/4.7 + 75mm f/4.5 Copal #1 Press Shutter Large Format 4x5 Macro


---

## Totals

| | list | **you net** |
|---|---|---|
| Everything, sold as 12 listings | $1,658 | **$1,272** |
| …if the 50/1.8 is the standard barrel | $1,593 | $1,215 |
| …if the F3 turns out to be an HP | $1,728 | $1,332 |
| …if the Copal shutter is diaphragm-less | $1,618 | $1,238 |

eBay fees and postage swallow **$359, about 23%**. That is the number to keep in
mind every time a local buyer offers cash.

---

## Strategy

**Sell these on eBay — the buyers are not local.**
- *Tamron 20-40mm EF* — the standout. Only 8 EF copies on the whole board, and
  EF asks ~60% over the Nikon-mount version ($158 vs $100 median) because EF
  adapts to every mirrorless system. Lead the title with "77mm, full-frame,
  f/2.7" — that is what the people hunting it search for.
- *F3, Nikon F, the AI-S glass* — national buyer pool, poor local pool.

**Part the F3 out, do not sell it as a kit.** Body + MD-4 + flash separately
nets ~$407 against ~$411 as a kit — a wash on paper, except the body alone sells
far faster. Most F3 buyers do not want the MD-4; it is heavy and eats 8 AAs.

**Bundle the Rebel Ti with the 50mm STM.** Separately that is $45 + $105 across
two listings; together at $150 it is one listing, one shipment, one fee —
$115 net against $108 — and it moves much faster, because beginners want a
ready-to-shoot kit. The crack is far easier to sell past with a lens attached.
Do **not** put the Tamron in that bundle; it is worth more to an adapter buyer
who will never want a Rebel Ti.

**Locally, bundle hard; on eBay, split.** A $70 pre-AI 50/1.4 is a miserable
Marketplace listing — niche manual glass barely moves locally. "Nikon F3 kit +
3 lenses, $700" is an attractive one. Bodies carry local listings; loose lenses
do not.

**Two low-value items.** The EM and the Rebel Ti net ~$25 each alone. That is an
hour of photographing, listing, packing and a post office run for $25. Sell them
locally for cash, or bundle them.

---

## Before you list

- **Check the F3's LCD first — it is worth more than the film test.** Two
  listings bracket the entire market: "Read LCD Defect" at $330 against "Film
  Tested, Bright LCD, No LCD bleed" at $449. A $119 spread, most of it the LCD.
  Bright with no edge bleed? Put *"bright LCD, no bleed"* in the title. Faded?
  Disclose it and expect the bottom of the range.
- **Check what the winder actually is.** The F3's only winder is the MD-4
  motor drive. Anything labelled AW-1, MD-E or MD-12 belongs to a different
  body — the MD-E is the Nikon EM's — and will not fit the F3 at all. That is
  a $75 item against a $25 one.
- **Confirm which F3 you have.** The HP (high-eyepoint) finder is worth $50–80
  more than the eye-level. Prices above assume eye-level.
- **Confirm which 50mm f/1.8 AI-S you have.** This is a 2× spread. The compact
  *pancake* (short barrel) is the collectible one at ~$160; the longer standard
  barrel is common at ~$95.
- **Say "film tested" wherever it is true.** It is worth real money on the
  Nikon F and the X-370 — untested X-370s sell in the $25–50 band because of
  the well-known capacitor failure.
- **Check the Copal shutter for an aperture ring.** Roughly $40 of difference.

Confirmed: the Canon 50mm is the STM, the X-370 is tested and working, and the
Tamron 20-40mm is the EF-mount version.

## Sources

- CameraWorth — [Nikon F3](https://www.cameraworth.com/n/nikon/f3/), [Nikon F](https://www.cameraworth.com/n/nikon/f/)
- [KEH — Nikon F3HP](https://www.keh.com/shop/nikon-f3hp-35mm-slr-camera.html), [KEH — Tamron 20-40mm EF](https://www.keh.com/shop/tamron-20-40mm-f-2-7-3-5-aspherical-if-sp-166d-lens-for-canon-ef-mount-77.html)
- [Lenstip — Tamron SP AF 20-40mm specs](https://lenstip.com/629-Tamron_SP_AF_20-40_mm_f_2.7-3.5_Aspherical_(IF)-lens_specifications.html)

---

## Revisions

- **2026-11-10** — F3 body $400 → $375, MD-4 $80 → $75, flash $40 → $38.
  Re-fetched and segmented the board by claimed condition rather than lumping
  it: graded "MINT/Exc+" bodies (n=99, p25 $470) are a different market from
  honest untested ones (n=11, p25 $430), and the untested bodies that actually
  look movable cluster at $350–390 — one of them an HP at $350. The original
  $400 sat above that cluster. Kit price cut to $450 after a $300 listing for
  "F3 body + SB-16 flash and more" turned up, confirming kits carry no premium.
- **2026-11-07** — Added the Polaroid Copal / Tominon set.
- **2026-11-05** — First version, 11 items.

---

*Prices are estimates from asking data, not sold data — assume ±15%. See
[method.md](method.md) for why, and log real sales in
[sold-log.md](sold-log.md) so the next version of this sheet is better.*
