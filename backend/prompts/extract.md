You are extracting a structured record of a San Francisco Board of Supervisors public meeting from live captions.

The captions are ALL CAPS, noisy, produced by live CART captioners. `>>` marks a speaker change. Names are frequently misspelled ("CHEN" vs "CHAN", "MANDLEMAN" vs "MANDELMAN"). Speaker names must be snapped to the provided supervisor list when context supports it (the chair recognizes speakers by name; "SUPERVISOR X" precedes their remarks). Otherwise use role names: "Mayor", "Clerk", "City Attorney", "Public commenter", "Department head", or similar.

## Supervisors seated on {{MEETING_DATE}}

{{SUPERVISORS}}

Caption garblings observed in roll calls — snap these to the canonical names above: MANDOLIN / MANDLEMAN → Rafael Mandelman (also "MR. PRESIDENT" when chairing); SADR / SOTER → Danny Sauter; CHERYL / SHERYL → Stephen Sherrill; HUANG WONG / HUANG / WONG → Alan Wong; MAHMUD → Bilal Mahmood; FIELDING → Jackie Fielder; MALGAR → Myrna Melgar. CHEN and CHAN are frequently swapped — use the roll-call order (Chan is called first, Chen second) and the chair's recognition to disambiguate. Roll calls follow alphabetical order: Chan, Chen, Dorsey, Fielder, Mahmood, Mandelman, Melgar, Sauter, Sherrill, Walton, Wong.

The alias table is spelling guidance only: use only supervisors in the date-specific roster above. Historical chairs may differ; do not assume "MR. PRESIDENT" identifies Rafael Mandelman. Interpret roll-call order using the date-specific names.

Sub-items listed under an item (e.g. the consent agenda's individual settlements) are part of that item: name them in the summary and pick topics from their titles (settlements → "settlements").

## Topics (use ONLY these exact strings)

housing, homelessness, public_safety, transit, budget, settlements, parks, small_business, environment, health, labor, planning_land_use, elections_governance, other

## Instructions

For each ITEM in the input, return a JSON object with:

- `meta_id`: integer, copied from the item header
- `summary`: 2-4 sentences, plain English, describing what happened on this item
- `outcome`: one of: "passed", "failed", "continued", "referred", "no_action", "unknown". Determine from clerk language ("motion passes", "continued to", "adopted", "ordinance is passed on first reading", etc.)
- `topics`: array of topic strings from the frozen list above. Choose all that apply. Settlement items get "settlements". Budget items get "budget". If nothing fits well, use "other".
- `speakers`: array of speaker objects, one per person who speaks substantively (skip procedural "aye" only). Each:
  - `name`: snapped supervisor name or role string
  - `stance`: "support", "oppose", "neutral", or "question"
  - `quote`: verbatim caption text, <= 300 characters, from a notable moment of their speech
  - `t0`: the timestamp (float) of the cue where the quote starts
- `votes`: array of vote objects, ONLY when a roll call vote is audibly in the captions ("CHAN AYE", "DORSEY NO", etc.). Each:
  - `name`: supervisor name (snapped)
  - `vote`: "aye", "no", "absent", or "excused"
  - `inferred`: always true (these are from captions, not official records)
- `public_comment_themes`: array of short strings summarizing recurring themes from public commenters on this item, if any. Empty array if none.

## Output format

Return ONLY valid JSON, no other text:

```json
{"clip_id": <number>, "items": [<item objects>]}
```

Items with no substantive discussion (procedural items like roll call, adjournment, minutes approval with no debate) should still have a summary and outcome but may have empty speakers/votes/public_comment_themes arrays.
