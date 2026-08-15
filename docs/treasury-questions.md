# Questions for treasury

**Status: draft — not sent.** Written to be pasted into an email or Slack message.

Five questions. Each one is currently blocking something specific, and each is
answerable from records or memory without touching the app. They are ordered by
what they unblock, not by how hard they are.

Numbers below come from the 892 real Wells Fargo transactions covering
2024-07-09 to 2026-07-08. Reproduce any of them with:

```bash
.venv/Scripts/python scripts/verify_dues_schedule.py
```

---

## 1. What did each term actually charge for dues? (most urgent)

**Why it matters.** Dues are matched on an *exact* amount. If a term charged a
rate the app doesn't know, dues for that term stop being recognised — silently.
No error appears; the payments just pile up in the review queue and dues income
looks like it collapsed.

The app currently assumes **\$35.00 / \$52.50** for every term, which are the
Fall 2024 rates. The VP Treasury Handbook shows the rate changing nearly every
term (\$20/\$40 → \$25/\$40 → \$30/\$50 → \$35/\$52.50).

**What the data shows.** There are **59 incoming Zelle/Venmo payments totalling
\$1,870** that sit at documented handbook rates and are currently uncategorized:

| Term | \$20 | \$25 | \$30 | \$50 |
| --- | --- | --- | --- | --- |
| Fall 2024 | 3 | 2 | 2 | — |
| Spring 2025 | 4 | 1 | **27** | **9** |
| Fall 2025 | — | 3 | — | 2 |
| Spring 2026 | 2 | — | 4 | — |

Spring 2025 stands out: 27 payments of \$30 and 9 of \$50, which is exactly the
handbook's "\$30 / \$50" pair. They read like dues — individual people paying by
Zelle — but we are not going to book \$1,870 of income on a pattern match.

**The question:** for each term below, what were the two dues rates?

| Term | Rates charged |
| --- | --- |
| Summer 2024 | |
| Fall 2024 | |
| Spring 2025 | |
| Summer 2025 | |
| Fall 2025 | |
| Spring 2026 | |
| Summer 2026 | |
| **Fall 2026** (starts 2026-08-15) | |

Fall 2026 is the one that matters going forward — it starts now, and if the rate
changed, dues stop categorizing on day one.

**A complication worth flagging.** Spring 2025 appears to have had **both** rate
pairs in circulation: 54 payments of \$35 and 4 of \$52.50 *alongside* 27 of
\$30 and 9 of \$50. We tested recording Spring 2025 as "\$30 / \$50" and the
number of recognised dues payments went **down**, from 245 to 223 — it picked up
the \$30s and lost the \$35s.

So this is not a matter of swapping one pair for another. Either the term
genuinely accepted several amounts (early-bird vs. late, new vs. returning,
semester vs. full-year), or some of those payments are not dues at all.

**Please answer explicitly:** for each term, list *every* amount that counted as
dues, not just the headline rate. The app accepts any number of amounts per
term, so "30, 35, 50 and 52.50 were all valid in Spring 2025" is a perfectly
usable answer — and guessing at this would move real income between committees.

---

## 2. Who holds these seven cards?

Four cards have a documented owner. Seven appear in real statements with none:

**0153, 7757, 9309, 3444, 7193, 1113, 5535**

Every purchase on an unknown card loses its strongest clue about which committee
it belongs to, so those rows land in the review queue by default. History
suggests 0153 is mostly meeting food, but that is inferred from the app's own
past guesses, not from anything anyone confirmed — so it is not usable as
evidence.

For reference, the four that are known: 8408 Salena (Consulting), 8313 Annalee
(Membership), 5718 Grant (Membership), 3568 Trent (President).

**The question:** for each of the seven, who held it and which committee's
spending was it for? "Retired, no idea" is a useful answer too — it tells us to
stop trying.

---

## 3. When someone is reimbursed, which committee should it hit?

**Why it matters.** This is the single largest source of disagreement between
the app and the historical ledgers — about half of all measured mismatches.

The app books every outgoing Venmo/Zelle to **"Refunded"**, a ledger account
that sits outside budget-vs-actual. The 2023 treasurer instead booked a
reimbursement to **the committee whose expense it repaid**, so it counted
against that committee's budget. **18 labeled rows disagree** on exactly this.

These are different claims about what a budget means. If the 2023 approach was
right, committee budgets today understate what committees actually spent,
because reimbursed spending is excluded.

**The question:** when a member fronts money for a committee and gets paid back,
should that repayment count against that committee's budget, or sit outside it?

---

## 4. Should dues paid over Venmo count, given Venmo takes a cut?

Venmo deducts a fee, so dues paid that way arrive short. The historical amounts
on record are **\$24.43, \$29.34 and \$39.14** against gross rates of \$25, \$30
and \$40 — roughly 2.2% light.

Because the app matches an exact amount, these currently don't register as dues
at all. No Venmo data has been imported yet, so nothing is affected today, but
it will be on the first Venmo import.

The app can accept an amount slightly below the expected rate on Venmo rows
only. It is **switched off** right now, because booking income at an amount
nobody explicitly authorised is a treasurer's decision.

**The question:** should a Venmo payment that arrives ~2% short of the dues rate
count as dues paid in full? And is the member considered paid up, or do they owe
the difference?

---

## 5. Two smaller mapping questions

**5a. Two purpose mappings that contradict the reference table.**

| Purpose | App books it to | Reference table says | Affected |
| --- | --- | --- | --- |
| Professional Development | 7 — Consulting | 10 is Prof. Development | 7 tx, \$1,391.98 |
| Food & Drink | 5 — Membership | not documented anywhere | 82 tx, \$7,808.28 |

The original behaviour was kept so historical figures wouldn't shift. Food &
Drink is the larger exposure by a wide margin. Are both correct as they stand?

**5b. Incoming payments with a memo.** **124 payments totalling \$18,802** are
members sending money in by Zelle or Venmo and are currently uncategorized. The
app deliberately refuses to learn a rule from a person's name — a rule keyed on
one member would mis-categorise everything they ever pay — but the memo line
often says what the money was for:

| Memo mentions | Rows | Total |
| --- | --- | --- |
| merch / crewneck / hoodie / t-shirt | 13 | \$580 |
| road trip / St. Augustine | 13 | \$1,030 |
| semi / formal / ticket | 5 | \$345 |
| headshot | 6 | \$35 |

**The question:** should a memo saying "merch" or "road trip" be enough to book
the payment to that committee automatically? And for headshots — Professional
Development or Membership?

Note this only accounts for about \$2,000 of the \$18,800. The rest carries no
memo at all, and no rule will ever recover those; they need a human or a
different source of truth.

---

## What happens with the answers

1 and 2 are entered as data and take effect immediately — no code change. 3, 4,
and 5 are one-line configuration changes each, but each one moves real money
between committees, which is why none of them has been decided by inference.
