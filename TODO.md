# Skyrim Alchemy Companion — TODO

## Skill XP to next level — TODO (2026-10-05)
- Ben 2026-10-05: "MIGHT be fun to know how many points are needed to reach
  the next increase in every skill."
- Would show XP progress toward next skill level (e.g., "Alchemy 36 → 37:
  X more XP" or "brew N more potions").
- Requires parsing skill XP values from save file (not yet researched).

## Merchant shop mapping — DROPPED (2026-10-05)
- Ben 2026-10-05: The merchant-chest parser was brittle (only worked for
  pure-ingredient vendors like Arcadia) and required buying something to
  force the save to record the chest — "distasteful." The Shop tab is now
  a merchant-agnostic SHOPPING LIST: recommends what to buy from the full
  ingredient database, no merchant detection, no chest parsing.
- MERCHANT_CHESTS dict kept in code for reference but no longer used.

## Known enchantments — SAVE FORMAT CRACKED (2026-10-05)
- Reddit r/skyrimmods thread (Ben provided): known enchantments are a FLAG
  on the base Form record, not a separate list. That's why Type 102 hunt failed.
- Save format discovered via differential analysis:
  - Type 48 ChangeForms = known ENCHANTMENTS
    - change_flags=0x01, 6 bytes data (49 00 00 00 00 00)
- Auto-detect live: Disenchant tab shows known enchantments from save.
- CORRECTION: Type 13 is NOT spells (misidentified 2026-10-05).
  - Spell auto-detect is on hold.
- enchantments.json has 13 items:
  - Original 4: Mage's Hood (Fortify Magicka), body item (Fortify Magicka Regen),
    Iron Sword of Sparks (Shock Damage), Hide Boots of Waning Frost (Resist Frost)
  - Enchanttester 9 (2026-10-05): Iron Battleaxe of Chills (Frost Damage),
    Iron Battleaxe of Dismay (Fear), Iron Dagger of Souls (Soul Trap),
    Iron War Axe of Cold (Frost Damage), Hide Boots of Resist Shock,
    Hide Bracers of Minor Alchemy, Iron Gauntlets of Minor Archery,
    Iron Helmet of Minor Magicka, Iron Shield of Minor Blocking
- esm_mappings.json (2026-10-05): 6,677 FormID→EDID mappings parsed from
  Skyrim.esm (239MB) for ARMO/WEAP/ENCH/SPEL. Automates future database
  growth — no more manual experiments needed. Ben spent ~9hrs on manual
  mapping; this is the automation he asked for.

## Alchemy skill/perks — DYNAMIC VALUES LIVE (2026-10-05)
- Ben 2026-10-05: Potion values now account for Alchemy skill and perks
  (Alchemist ranks, Benefactor, Poisoner, Physician) using UESP formula
  structure. Changes relative ranking, not just absolute values.
- UI: collapsible settings panel, stored in localStorage, sent via API
  params. Manual input allows "what-if" perk comparisons.
- Future: auto-detect skill/perks from save as default, with manual
  override toggle for "what-if" scenarios. (Skill/perk save format not
  yet reverse-engineered.)

## Ideas parked
- Show full merchant stock in the Shop tab even when nothing is worth
  buying (currently the tab just says so). — OBSOLETE (no merchant stock anymore)
