# Skyrim Alchemy Companion

**Know what your ingredients are worth — before you brew.**

This is a small companion app for *Skyrim Special Edition / Anniversary
Edition*. It reads your actual in-game inventory straight out of your save
file, figures out every potion you can brew from 2 or 3 ingredients, and
shows them **sorted by gold value, richest first** — so you always know
the most valuable thing you can make right now.

Open it on your phone while you play: tap **+** on a potion to add it to
your brew queue, and the list instantly updates to show what you can still
make with what's left.

## What you get

- **Price-first potion list** — every brewable 2- and 3-ingredient recipe,
  sorted by value, with the ingredients and their remaining counts.
- **Clickable sorting** — sort by price, ingredient, number of
  ingredients, or effect. Filter by a specific ingredient or search text.
- **Brew queue** — tap **+** to queue a potion. Its ingredients are
  deducted from your pool and the whole list re-computes, so the next
  suggestion is always honest about what you can still brew. Remove items
  or clear the queue to get ingredients back; the queue shows a running
  gold total.
- **By Effect tab** — prefer shopping by effect instead of price? Pick an
  effect and see every recipe that produces it, richest first.
- **Discover tab** — ingredients are highlighted when brewing them would
  reveal a new alchemy effect you haven't discovered yet.
- **Shop tab** — a smart shopping list: what to buy (and what to skip)
  based on your current inventory and the most valuable potions you can
  brew. Gold-aware: plans purchases in phases so you buy, brew, sell,
  and return with more gold.
- **Enchantments tab** — lists all enchantments you've learned via
  disenchanting, auto-detected from your save.
- **Alchemy skill & perks** — potion values account for your Alchemy
  skill and perks (Alchemist ranks, Benefactor, Poisoner, Physician).
  Base skill and perks are auto-detected from your save; override them
  manually for what-if comparisons.
- **Live inventory** — hit *Refresh* after quicksaving (F5) and the page
  re-reads your save.

Potion values are calculated from your Alchemy skill and perks. The app
auto-detects your base skill and perks from the save; you can adjust them
manually to see how perk choices affect potion values.

## Requirements

- **Python 3** (3.8 or newer). That's the only dependency — no installs,
  no build step.
- **Skyrim Special Edition or Anniversary Edition**, on Windows or on
  Linux via Proton/Steam Play.

## Run it

**Windows:** double-click **`run.bat`**.
**Linux / macOS:** double-click **`run.sh`**
(or run `bash run.sh` from a terminal).

The launcher finds Python for you, starts the companion, and prints an
address like this:

```
On your phone (same WiFi), open:
    http://192.168.1.42:8123/
```

If double-clicking doesn't work on your system, open a terminal in this
folder and run `python3 skyrim_alchemy.py` (Windows: `py skyrim_alchemy.py`).

Then:

1. Make sure your **phone is on the same WiFi** as your PC.
2. Open the printed address in your phone's browser.
3. In Skyrim, **quicksave with F5**, then tap **Refresh** on the page.

The app always reads your **newest** save file. If you prefer a specific
one, quit the app and re-run it pointing at your saves folder:

```
python3 skyrim_alchemy.py --saves-dir "C:\path\to\Saves"
```

## Finding your saves

The app looks for your saves folder automatically in the usual places:

- Windows: `Documents\My Games\Skyrim Special Edition\Saves`
  (including the OneDrive-mapped variant)
- Linux/Proton: the Steam compatibility folders for Skyrim SE
  (`~/.steam`, `~/.local/share/Steam`, and custom library locations such
  as `~/Games/Steam`)

If it can't find them, it will tell you where it looked — use
`--saves-dir` (above) to point it at the right folder.

## Troubleshooting

- **"Could not find your Skyrim saves folder"** — your saves live
  somewhere unusual. Find the folder containing your `Quicksave.ess`,
  then re-run with `--saves-dir` pointing at it.
- **Phone can't open the page** — three usual causes:
  1. The phone isn't on the same WiFi network as the PC.
  2. A firewall is blocking the port (default 8123). Allow Python
     through, or pick another port: `python3 skyrim_alchemy.py --port 8888`.
  3. The address changed (laptops get new IPs). Re-run and use the fresh
     address it prints.
- **"No Python found" (run.bat)** — install Python 3 from
  [python.org](https://www.python.org/downloads/) and make sure
  **"Add python.exe to PATH"** is checked during installation.
- **Page shows no ingredients** — quicksave in-game with F5 first, then
  tap Refresh. The app reads saves, not your live game memory.
- **A save fails to parse** — the parser is strict about the save format
  but tolerant about unknown data: it will use every ingredient it could
  read. If you get a hard error, please report it (see below) and attach
  which save type it was (Quicksave/Autosave/manual).

## How it works (short version)

Skyrim save files (`.ess`) are LZ4-compressed. The app decompresses your
save, walks the change-list to your player character's inventory, matches
each item against a built-in database of 191 alchemy ingredients
(including Anniversary Edition creations), computes every valid 2- and
3-ingredient combination, prices each with standard effect values, and
serves the result as a phone-friendly web page. Everything runs locally —
your save never leaves your machine.

## Files

| File | What it is |
|---|---|
| `skyrim_alchemy.py` | The whole app: save parser, potion math, web server |
| `ingredients.json` | Ingredient database (names, effects, form IDs) |
| `enchantment_names.json` | Known enchantment names (FormID → name) |
| `esm_mappings.json` | FormID mappings from Skyrim.esm (auto-generated) |
| `parse_esm.py` | Script to regenerate esm_mappings.json from Skyrim.esm |
| `run.bat` / `run.sh` | Double-click launchers (Windows / Linux & macOS) |
| `README.md` | This file |
| `LICENSE` | MIT license |

## Reporting issues

Found a bug or a save it can't read? Open an issue with your Skyrim
edition (SE/AE), platform (Windows/Linux+Proton), and what you were doing
when it broke. Save files help enormously if you're comfortable sharing
one.

## License

MIT — see `LICENSE`. UESP (en.uesp.net) for the ingredient and
alchemy-value data that makes the pricing possible.
