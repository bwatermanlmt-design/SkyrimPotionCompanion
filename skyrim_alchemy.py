#!/usr/bin/env python3
"""
Skyrim Alchemy Companion
========================
A universal, dependency-free (Python 3.8+ standard library only) companion for
Skyrim Special Edition / Anniversary Edition.

What it does
------------
1. Finds your Skyrim saves folder automatically (Windows and Linux/Proton).
2. Reads the newest ``.ess`` save file and extracts the PLAYER's inventory,
   straight from the save's binary format -- no mods, no SKSE, no game running.
3. Serves a mobile-friendly web page on your local network. Open it in your
   phone's browser (same WiFi) and it shows every potion you can brew from the
   ingredients you actually own, plus a big Refresh button (quicksave with F5,
   tap Refresh, see the new list).

How to run
----------
    python3 skyrim_alchemy.py

Then open the printed URL (e.g. http://192.168.1.42:8123/) on your phone.

How it works (the short version)
--------------------------------
A Skyrim SE save is: a small header, a screenshot, an LZ4-compressed blob, and
inside that blob: plugin load-order lists, global data tables, and "change
forms" -- per-object records of everything that differs from the base game.
Your inventory lives in the change form for the player actor (formID
0x00000014, an ACHR record). Each inventory entry stores the item's formID,
which we resolve back to a plugin + object ID using the save's own plugin
lists, then look up in ``ingredients.json``.

Verification status (2026-10-01)
--------------------------------
- Header / plugin lists / file-location table layout: from UESP's
  "Skyrim Mod:Save File Format" documentation.
- LZ4 block decompression: pure-Python implementation, unit-tested against
  hand-built LZ4 blocks.
- Change-form framing (RefID/changeFlags/type/version/lengths) and RefID
  resolution: from UESP documentation, confirmed against ReSaver.
- ACHR change-data body layout (flag bits, section order, VSVal encoding,
  inventory entry encoding, extra-data skip table): verified verbatim
  against the open-source ReSaver (FallrimTools, Apache-2.0) source --
  ChangeFormACHR.java, ChangeFormInitialData.java,
  ChangeFormInventoryItem.java, ChangeFormExtraData(Data).java, VSVal.java.
- Save-game "wstring": u16 LE length prefix + raw bytes, no terminator
  (ReSaver BufferUtil.getWStringRaw).
- NOT yet validated against a real save file. If the first run against your
  Quicksave.ess reports "player inventory not found" or fails to parse,
  that is the signal to re-check the body layout -- the framing code is
  deliberately strict so failures are loud, not silently wrong.
"""

import argparse
import http.server
import json
import os
import random
import socket
import struct
import sys
import threading
import time
import urllib.parse
import zlib
from typing import Dict, List, Optional, Tuple

APP_NAME = "Skyrim Alchemy Companion"
PORT = 8123

# Steam AppID for Skyrim Special Edition (same for Anniversary Edition).
SKYRIM_APPID = "489830"
SAVE_FOLDER_NAME = os.path.join("My Games", "Skyrim Special Edition", "Saves")

# The player's actor reference. In every save this is an ACHR change form.
PLAYER_FORMID = 0x00000014


# ---------------------------------------------------------------------------
# LZ4 block decompression (pure Python, no third-party package needed)
# ---------------------------------------------------------------------------
# Skyrim SE/AE compresses everything after the header+screenshot with LZ4 in
# *block* format: one raw LZ4 block, no frame header. The format is simple:
# a stream of "sequences": [token][literal length ext...][literals]
# [offset u16][match length ext...]. The high nibble of the token is the
# literal length, the low nibble is (match length - 4). A nibble value of 15
# means "read extension bytes, adding each; stop at the first byte != 255".
# The final sequence holds literals only (no offset/match).

def lz4_decompress_block(data: bytes, expected_size: Optional[int] = None) -> bytes:
    """Decompress a single raw LZ4 block. Raises ValueError on corrupt input."""
    out = bytearray()
    mv = memoryview(data)
    n = len(mv)
    i = 0
    while i < n:
        token = mv[i]
        i += 1

        # --- literals ---
        lit_len = token >> 4
        if lit_len == 15:
            while True:
                if i >= n:
                    raise ValueError("truncated LZ4 block (literal length)")
                b = mv[i]
                i += 1
                lit_len += b
                if b != 255:
                    break
        if i + lit_len > n:
            raise ValueError("truncated LZ4 block (literals)")
        out += mv[i:i + lit_len]
        i += lit_len

        if i >= n:
            break  # last sequence: literals only, no match follows

        # --- match ---
        if i + 2 > n:
            raise ValueError("truncated LZ4 block (match offset)")
        offset = mv[i] | (mv[i + 1] << 8)
        i += 2
        if offset == 0:
            raise ValueError("invalid LZ4 match offset 0")
        match_len = (token & 0x0F) + 4
        if (token & 0x0F) == 15:
            while True:
                if i >= n:
                    raise ValueError("truncated LZ4 block (match length)")
                b = mv[i]
                i += 1
                match_len += b
                if b != 255:
                    break
        start = len(out) - offset
        if start < 0:
            raise ValueError("LZ4 match offset before start of output")
        if offset >= match_len:
            # No overlap: bulk copy.
            out += out[start:start + match_len]
        else:
            # Overlapping match: repeat the available bytes (doubling trick).
            chunk = bytes(out[start:start + offset])
            while len(chunk) < match_len:
                need = match_len - len(chunk)
                chunk += chunk[:need]
            out += chunk

    if expected_size is not None and len(out) != expected_size:
        raise ValueError(
            "LZ4 output size %d != expected %d" % (len(out), expected_size))
    return bytes(out)


# ---------------------------------------------------------------------------
# Binary cursor over the (decompressed) save data
# ---------------------------------------------------------------------------

class Cursor:
    """Little-endian binary reader with a movable position."""

    def __init__(self, data: bytes, pos: int = 0):
        self._d = data
        self.pos = pos

    def remaining(self) -> int:
        return len(self._d) - self.pos

    def _take(self, n: int) -> bytes:
        if self.pos + n > len(self._d):
            raise ValueError(
                "unexpected end of save data at offset %d (need %d bytes)"
                % (self.pos, n))
        b = self._d[self.pos:self.pos + n]
        self.pos += n
        return b

    def u8(self) -> int:
        return self._take(1)[0]

    def u16(self) -> int:
        return struct.unpack("<H", self._take(2))[0]

    def u32(self) -> int:
        return struct.unpack("<I", self._take(4))[0]

    def i32(self) -> int:
        return struct.unpack("<i", self._take(4))[0]

    def f32(self) -> float:
        return struct.unpack("<f", self._take(4))[0]

    def raw(self, n: int) -> bytes:
        return self._take(n)

    def skip(self, n: int) -> None:
        self._take(n)  # bounds-checked discard

    def wstring(self) -> str:
        """Save-game string: u16 LE length prefix + raw bytes, no terminator.

        Verified 2026-10-01 against ReSaver's BufferUtil.getWStringRaw. The
        game writes single-byte text, so UTF-8 and Windows-1252 agree here.
        """
        length = self.u16()
        return self._take(length).decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Save-folder auto-detection
# ---------------------------------------------------------------------------

def candidate_save_dirs() -> List[str]:
    """All places a Skyrim SE/AE Saves folder could plausibly live."""
    dirs: List[str] = []
    home = os.path.expanduser("~")

    # Windows: %USERPROFILE%\Documents\My Games\Skyrim Special Edition\Saves
    for var in ("USERPROFILE", "HOMEDRIVE"):
        pass
    userprofile = os.environ.get("USERPROFILE")
    if userprofile:
        dirs.append(os.path.join(userprofile, "Documents", SAVE_FOLDER_NAME))
    # Some setups remap Documents via OneDrive.
    if userprofile:
        dirs.append(os.path.join(userprofile, "OneDrive", "Documents",
                                 SAVE_FOLDER_NAME))

    # Linux / Proton: the game's compatdata prefix. AppID 489830.
    steam_roots = [
        os.path.join(home, ".steam", "steam"),
        os.path.join(home, ".steam"),
        os.path.join(home, ".local", "share", "Steam"),
        # A custom Steam library location (common on multi-drive setups).
        os.path.join(home, "Games", "Steam"),
    ]
    for root in steam_roots:
        prefix = os.path.join(root, "steamapps", "compatdata", SKYRIM_APPID,
                              "pfx", "drive_c", "users", "steamuser",
                              "Documents")
        dirs.append(os.path.join(prefix, SAVE_FOLDER_NAME))

    # Deduplicate, keep order.
    seen = set()
    unique: List[str] = []
    for d in dirs:
        if d not in seen:
            seen.add(d)
            unique.append(d)
    return unique


def find_saves_dir(override: Optional[str] = None) -> str:
    """Return the saves directory, or raise a helpful error."""
    if override:
        if os.path.isdir(override):
            return os.path.abspath(override)
        raise SystemExit("saves dir not found: %s" % override)
    tried = []
    for d in candidate_save_dirs():
        tried.append(d)
        if os.path.isdir(d):
            return d
    raise SystemExit(
        "Could not find your Skyrim saves folder.\n"
        "Looked in:\n  " + "\n  ".join(tried) +
        "\nUse --saves-dir to point at it directly.")


def pick_save(saves_dir: str) -> str:
    """Newest .ess file; Quicksave.ess wins near-ties (it's the F5 reflex)."""
    saves = []
    for name in os.listdir(saves_dir):
        if name.lower().endswith(".ess"):
            full = os.path.join(saves_dir, name)
            try:
                saves.append((os.path.getmtime(full), name, full))
            except OSError:
                continue
    if not saves:
        raise SystemExit("No .ess save files in %s" % saves_dir)
    newest_mtime = max(m for m, _, _ in saves)
    # Prefer the quicksave if it is (essentially) the newest -- after an F5
    # it usually is, and it is the freshest picture of the inventory.
    for m, name, full in saves:
        if name.lower() == "quicksave.ess" and m >= newest_mtime - 2:
            return full
    saves.sort()
    return saves[-1][2]


# ---------------------------------------------------------------------------
# Save-file parsing
# ---------------------------------------------------------------------------
# Layout (from UESP "Skyrim Mod:Save File Format", corrected against ReSaver):
#   magic "TESV_SAVEGAME" | headerSize u32 | header | screenshot |
#   uncompressedLen u32 | compressedLen u32 | <LZ4 block: formVersion ... EOF>
# The file-location-table offsets are NOT relative to the decompressed body:
# they are relative to the file offset of the uncompressedLen field (ReSaver's
# ESS.java seeks with `INPUT.position(FLT.xxxOffset - startingOffset)` where
# startingOffset is the buffer position right after the screenshot). So:
#   body_position = flt_value - <file offset of uncompressedLen>

class SaveParseError(Exception):
    pass


def parse_header(cur: Cursor) -> dict:
    """Walk the header; we only need the screenshot size + compression type."""
    info = {}
    info["version"] = cur.u32()          # 12 for SE
    info["saveNumber"] = cur.u32()
    info["playerName"] = cur.wstring()
    info["playerLevel"] = cur.u32()
    info["playerLocation"] = cur.wstring()
    info["gameDate"] = cur.wstring()
    info["playerRaceEditorId"] = cur.wstring()
    info["playerSex"] = cur.u16()
    info["playerCurExp"] = cur.f32()
    info["playerLvlUpExp"] = cur.f32()
    cur.skip(8)                          # FILETIME
    info["shotWidth"] = cur.u32()
    info["shotHeight"] = cur.u32()
    info["compressionType"] = cur.u16()  # 0 = none, 1 = zlib, 2 = LZ4
    return info


def load_decompressed(path: str) -> Tuple[bytes, dict, int]:
    """Read the .ess file.

    Returns (decompressed_body, header_info, flt_base) where flt_base is the
    file offset of the uncompressedLen field -- the base that file-location-
    table offsets are measured from (see module comment).
    """
    with open(path, "rb") as f:
        raw = f.read()
    if raw[0:13] != b"TESV_SAVEGAME":
        raise SaveParseError("not a Skyrim save (bad magic)")
    cur = Cursor(raw, 13)
    header_size = cur.u32()
    header_start = cur.pos
    info = parse_header(cur)
    if info["version"] < 12:
        raise SaveParseError(
            "save version %d looks like Legendary Edition; "
            "only Special Edition / Anniversary Edition saves are supported"
            % info["version"])
    # The header is header_size bytes; skip any slack we didn't parse.
    cur.pos = header_start + header_size

    # Screenshot: SE stores RGBA.
    cur.skip(4 * info["shotWidth"] * info["shotHeight"])

    # Everything from here on is addressed by the file-location table
    # relative to this position.
    flt_base = cur.pos

    ctype = info["compressionType"]
    if ctype == 2:  # LZ4 block
        uncompressed_len = cur.u32()
        compressed_len = cur.u32()
        block = cur.raw(compressed_len)
        body = lz4_decompress_block(block, uncompressed_len)
    elif ctype == 1:  # zlib (documented but reportedly unused)
        uncompressed_len = cur.u32()
        compressed_len = cur.u32()
        body = zlib.decompress(cur.raw(compressed_len))
        if len(body) != uncompressed_len:
            raise SaveParseError("zlib size mismatch")
    elif ctype == 0:
        body = raw[cur.pos:]
    else:
        raise SaveParseError("unknown compressionType %r" % (ctype,))
    return body, info, flt_base


def resolve_refid(b0: int, b1: int, b2: int,
                  formid_array: List[int]) -> int:
    """Turn a 3-byte save RefID into a full 4-byte formID.

    Upper 2 bits of the first byte select the kind (per UESP):
      0 = index into the save's formIDArray (value-1; 0 means null)
      1 = base game record: 0x00 + 22-bit value (i.e. Skyrim.esm)
      2 = created record: 0xFF + 22-bit value
    """
    kind = b0 >> 6
    value = ((b0 & 0x3F) << 16) | (b1 << 8) | b2
    if kind == 0:
        if value == 0:
            return 0
        if value - 1 >= len(formid_array):
            raise SaveParseError("RefID index %d out of range" % value)
        return formid_array[value - 1]
    if kind == 1:
        return value
    if kind == 2:
        return 0xFF000000 | value
    raise SaveParseError("unknown RefID kind %d" % kind)


def split_formid(fid: int, plugins: List[str],
                 light_plugins: List[str]) -> Tuple[Optional[str], int]:
    """Split a full formID into (plugin filename, object ID).

    Regular plugin: high byte = index into the plugin list, low 3 bytes =
    object ID. ESL (light) plugin: formID looks like 0xFEiii ooo --
    0xFE marker, 12-bit ESL index, 12-bit object ID.

    NOTE: the ESL-index-into-lightPluginInfo mapping is assumed (the save
    stores light plugins in load order, matching in-game ESL indices).
    """
    if (fid >> 24) == 0xFE:
        esl_index = (fid >> 12) & 0xFFF
        plugin = (light_plugins[esl_index]
                  if esl_index < len(light_plugins) else None)
        return plugin, fid & 0xFFF
    mod_index = (fid >> 24) & 0xFF
    plugin = plugins[mod_index] if mod_index < len(plugins) else None
    return plugin, fid & 0xFFFFFF


# ---------------------------------------------------------------------------
# ACHR change-data body: player inventory
# ---------------------------------------------------------------------------
# Section order, flag bits, initial-data sizes, VSVal encoding, inventory
# entry layout, and the extra-data skip table were all verified 2026-10-01
# verbatim against ReSaver (awesmdiver/fallrimtools-resaver-renewed):
# ChangeFormACHR.java, ChangeFormInitialData.java, ChangeFormInventoryItem.java,
# ChangeFormExtraData(Data).java, ChangeFlagConstantsAchr.java,
# ChangeFormFlags.java, VSVal.java. UESP does not document these bodies
# ("work is in progress").

# ACHR change-flag bits (ReSaver ChangeFlagConstantsAchr)
_F_FORM_FLAGS = 0
_F_MOVE = 1
_F_HAVOK_MOVE = 2
_F_CELL_CHANGED = 3
_F_SCALE = 4
_F_INVENTORY = 5
_F_BASEOBJECT = 7
_F_PROMOTED = 25
_F_LEVELED_INVENTORY = 27
_F_ANIMATION = 28
# Bits that trigger the actor-level EXTRADATA section:
_F_EXTRADATA_BITS = (6, 25, 9, 11, 17, 26, 29, 30, 31, 18)
_EXTRADATA_MASK = sum(1 << b for b in _F_EXTRADATA_BITS)

# ChangeFormInitialData body sizes by initialType: 0 = empty;
# 1 = 8; 2 = 10; 3 = 4; 4 = RefID + 3 floats + 3 floats = 27;
# 5 = 27 + u8 + RefID = 31; 6 = 27 + RefID + u16 + u16 = 34.
_INITIAL_DATA_SIZES = {0: 0, 1: 8, 2: 10, 3: 4, 4: 27, 5: 31, 6: 34}


def _read_vsval(cur: Cursor) -> int:
    """Skyrim variable-size int (ReSaver VSVal): the low 2 bits of the first
    byte are a size tag -- 0: 1 byte total, 1: 2 bytes, 2|3: 3 bytes --
    and the value is the remaining bits, little-endian."""
    b0 = cur.u8()
    tag = b0 & 0x03
    if tag == 0:
        return b0 >> 2
    if tag == 1:
        return (b0 | (cur.u8() << 8)) >> 2
    return (b0 | (cur.u8() << 8) | (cur.u8() << 16)) >> 2


def _skip_wstring(cur: Cursor) -> None:
    cur.skip(cur.u16())


# Fixed payload sizes for ChangeFormExtraData entry types, transcribed from
# the switch in ReSaver's ChangeFormExtraDataData.java. Types 4/8/12 hold
# nested extra-data entries and are handled by recursion below.
_EXTRA_DATA_FIXED = {
    0: 0, 22: 0, 23: 0, 29: 0, 32: 0, 53: 0, 61: 0,   # no payload
    24: 19, 25: 13, 26: 3, 28: 3, 30: 4, 31: 1,
    33: 3, 34: 3, 35: 3, 36: 2, 37: 4, 39: 4, 40: 4,
    42: 13, 43: 28, 44: 1, 46: 5, 47: 4, 49: 28,
    56: 3, 62: 7, 69: 3, 72: 3, 73: 1, 77: 1, 79: 2,
    83: 4, 84: 1, 85: 4, 88: 7, 89: 4, 93: 4,
    101: 3, 102: 6, 104: 3, 106: 7, 112: 3, 133: 3,
    135: 12, 142: 3, 146: 3, 149: 7, 150: 1,
    155: 5, 156: 1, 157: 3, 159: 6, 160: 4, 161: 88,
    164: 3, 169: 11, 176: 8,
}


def _skip_extradata_entry(cur: Cursor) -> None:
    """Skip one ChangeFormExtraData entry (TYPE u8 + payload)."""
    t = cur.u8()
    if t in _EXTRA_DATA_FIXED:
        cur.skip(_EXTRA_DATA_FIXED[t])
        return
    if t == 4:      # nested extra-data x1
        _skip_extradata_entry(cur)
        return
    if t == 8:      # nested extra-data x2
        _skip_extradata_entry(cur)
        _skip_extradata_entry(cur)
        return
    if t == 12:     # nested extra-data x3
        for _ in range(3):
            _skip_extradata_entry(cur)
        return
    if t == 16:     # nested extra-data x4
        for _ in range(4):
            _skip_extradata_entry(cur)
        return
    if t == 27:     # VSVal-counted u32s
        for _ in range(_read_vsval(cur)):
            cur.skip(4)
        return
    if t == 50:     # NonActorMagicTarget: RefID + VSVal array of targets
        cur.skip(3)
        for _ in range(_read_vsval(cur)):
            cur.skip(3 + 1)            # RefID + u8
            _read_vsval(cur)           # unk2 (value discarded)
            cur.skip(_read_vsval(cur))  # VSVal-sized blob
        return
    if t == 52:     # VSVal-counted u64s
        for _ in range(_read_vsval(cur)):
            cur.skip(8)
        return
    if t == 68:     # VSVal-counted floats
        for _ in range(_read_vsval(cur)):
            cur.skip(4)
        return
    if t == 76:     # InfoGeneralTopic: wstring + 5 bytes + 4 RefIDs
        _skip_wstring(cur)
        cur.skip(5 + 12)
        return
    if t == 91:     # FactionChanges: VSVal array of (RefID+u8), + RefID + u8
        for _ in range(_read_vsval(cur)):
            cur.skip(4)
        cur.skip(4)
        return
    if t == 92:     # DismemberedLimbs
        cur.skip(2 + 4 + 4 + 1 + 3)
        for _ in range(_read_vsval(cur)):
            cur.skip(4)
            for _ in range(_read_vsval(cur)):
                cur.skip(3)
        return
    if t == 111:    # VSVal array of (RefID + u32 + u32)
        for _ in range(_read_vsval(cur)):
            cur.skip(11)
        return
    if t == 113:    # SayToTopicInfo (+ nested Data2)
        cur.skip(3 + 1 + 4 + 3)
        _skip_wstring(cur)
        _skip_wstring(cur)
        cur.skip(4 + 4 + 1 + 9 + 1)
        return
    if t == 120:    # VSVal array of (RefID + u32 + u8)
        for _ in range(_read_vsval(cur)):
            cur.skip(8)
        return
    if t == 136:    # VSVal array of (RefID + u32)
        for _ in range(_read_vsval(cur)):
            cur.skip(7)
        return
    if t == 140:    # VSVal array of RefID
        for _ in range(_read_vsval(cur)):
            cur.skip(3)
        return
    if t == 152:    # AttachedArrows3D
        for _ in range(_read_vsval(cur)):
            if cur.raw(3) != b"\x00\x00\x00":
                if cur.u16() != 0xFFFF:
                    cur.skip(4 + 32)
        cur.skip(4)
        return
    if t == 153:    # TextDisplayData
        r1, r2, unk = cur.raw(3), cur.raw(3), cur.i32()
        if r1 == b"\x00\x00\x00" and r2 == b"\x00\x00\x00" and unk == -2:
            _skip_wstring(cur)
        return
    if t == 174:    # GroupConstraint
        cur.skip(4 + 3)
        _skip_wstring(cur)
        _skip_wstring(cur)
        cur.skip(12 + 12 + 4 + 4)
        return
    if t == 175:    # u32-counted array of (RefID + u32)
        for _ in range(cur.u32()):
            cur.skip(7)
        return
    if t == 20:    # TextDisplayData (renamed item): 16B + u16-len name + 9B.
        # Empirically reverse-engineered from a real save (2026-10-01): two
        # player-renamed items (leather boots, leather helmet) both
        # show a 16-byte header, u16 string length, the custom name, and a
        # 9-byte trailer. ReSaver has no case for type 20 either.
        cur.skip(16)
        n = cur.u16()
        if n > 512:
            raise SaveParseError("type-20 display name too long: %d" % n)
        cur.skip(n)
        cur.skip(9)
        return
    if t == 64:    # ExtraEnchantment: RefID (3B) + u16 charge.
        cur.skip(3 + 2)
        return
    # Type 45 (LeveledCreature) embeds a full NPC parse -- not skippable.
    # Anything else is unknown to ReSaver too (it throws as well).
    raise SaveParseError("cannot skip extra-data entry type %d" % t)


def _skip_extradata(cur: Cursor) -> None:
    n = _read_vsval(cur)
    if n > 1024:  # ReSaver's hard cap
        raise SaveParseError("absurd extra-data entry count %d" % n)
    for _ in range(n):
        _skip_extradata_entry(cur)


def parse_achr_inventory(
        data: bytes, change_flags: int, refid_kind: int,
        formid_array: list = None
) -> List[Tuple[Tuple[int, int, int], int]]:
    """Walk a player ACHR change-data body; return [((b0,b1,b2), count)].

    Section order per ReSaver's ChangeFormACHR. The caller resolves the raw
    RefID bytes to formIDs with resolve_refid(). Raises SaveParseError on any
    structural mismatch -- including trailing bytes, which ReSaver also
    treats as a hard error.

    formid_array (optional): for resync validation on exploit saves.
    (2026-10-05: the player has millions of non-stackable items via exploits;
    some entries have unparseable extra data. Resync skips them.)
    """
    cur = Cursor(data, 0)

    # 1. initial data; type derived from RefID kind + flags.
    if refid_kind == 2:      # CREATED
        initial_type = 5
    elif change_flags & ((1 << _F_PROMOTED) | (1 << _F_CELL_CHANGED)):
        initial_type = 6
    elif change_flags & ((1 << _F_HAVOK_MOVE) | (1 << _F_MOVE)):
        initial_type = 4
    else:
        initial_type = 0
    if initial_type not in _INITIAL_DATA_SIZES:
        raise SaveParseError("bad ACHR initialType %d" % initial_type)
    cur.skip(_INITIAL_DATA_SIZES[initial_type])

    # 2. havok blob (VSVal-counted).
    if change_flags & (1 << _F_HAVOK_MOVE):
        cur.skip(_read_vsval(cur))

    # 3. always present.
    cur.u32()
    cur.skip(4)

    # 4-6. conditional fixed sections.
    if change_flags & (1 << _F_FORM_FLAGS):
        cur.skip(6)          # ChangeFormFlags: u32 + u16
    if change_flags & (1 << _F_BASEOBJECT):
        cur.skip(3)          # RefID
    if change_flags & (1 << _F_SCALE):
        cur.skip(4)          # float32

    # 7. actor-level extra data.
    if change_flags & _EXTRADATA_MASK:
        _skip_extradata(cur)

    # 8. the inventory itself: VSVal count, then per item RefID + i32 count
    #    + extra data (always present, may be empty).
    raw_items = []
    expected = 0
    if change_flags & ((1 << _F_INVENTORY) | (1 << _F_LEVELED_INVENTORY)):
        expected = _read_vsval(cur)
        for _ in range(expected):
            item_start = cur.pos
            try:
                b0, b1, b2 = cur.u8(), cur.u8(), cur.u8()
                count = cur.i32()
                _skip_extradata(cur)
                raw_items.append(((b0, b1, b2), count))
            except (SaveParseError, IndexError, ValueError):
                # Tolerant parse with resync (2026-10-05: exploit saves).
                # Try to find the next valid item header within 2KB.
                # A valid header: plausible RefID + i32 count + parseable extra data.
                resynced = False
                if formid_array is not None:
                    search_end = min(item_start + 2048, len(data) - 8)
                    for off in range(item_start + 1, search_end):
                        try:
                            tb0, tb1, tb2 = data[off], data[off+1], data[off+2]
                            kind = tb0 >> 6
                            if kind not in (0, 1, 2):
                                continue
                            if kind == 0:
                                val = ((tb0 & 0x3F) << 16) | (tb1 << 8) | tb2
                                if val == 0 or val - 1 >= len(formid_array):
                                    continue
                            # Trial parse: count + extra data
                            tcur = Cursor(data, off + 3)
                            tcur.i32()  # count
                            # Peek extra data count (VSVal)
                            epos = tcur.pos
                            eb0 = tcur.u8()
                            tag = eb0 & 0x03
                            if tag == 0:
                                ecount = eb0 >> 2
                            elif tag == 1:
                                ecount = (eb0 | (tcur.u8() << 8)) >> 2
                            else:
                                ecount = (eb0 | (tcur.u8() << 8) | (tcur.u8() << 16)) >> 2
                            # Sanity: 0-20 entries is plausible
                            if 0 <= ecount <= 20:
                                # Found a plausible header; resync here
                                cur.pos = off
                                resynced = True
                                break
                        except (IndexError, ValueError):
                            continue
                if not resynced:
                    # Could not resync; stop parsing (partial is better than none)
                    break
                # If resynced, the for loop continues and will retry parsing
                # at the new position. But we've already consumed one iteration;
                # decrement the loop counter by continuing (the range is fixed,
                # so we might parse fewer than expected, which is fine).
                continue

    # 9. animation blob (VSVal-counted). Only if we parsed the full inventory;
    # otherwise the cursor is misaligned and we skip it.
    if change_flags & (1 << _F_ANIMATION) and len(raw_items) == expected:
        try:
            cur.skip(_read_vsval(cur))
        except (SaveParseError, IndexError, ValueError):
            pass

    # 10. trailing bytes: the game writes more data after the animation blob
    # than ReSaver models (extra animation-graph state; 5KB-14KB observed
    # varying save to save, even on a fresh intro-cart save). The inventory
    # above parsed cleanly, so tolerate the tail instead of failing the
    # whole refresh -- this is a companion app, not a save editor.
    if cur.remaining() and len(raw_items) == expected:
        print("warning: ACHR body has %d trailing bytes after parse "
              "(ignored)" % cur.remaining(), file=sys.stderr)
    return raw_items


# ---------------------------------------------------------------------------
# Merchant chests: shop inventory from the save
# ---------------------------------------------------------------------------
# Map from save-header location name -> merchant chest formID (Skyrim.esm).
# Chest formIDs verified empirically from player saves (2026-10-03).
# Add more shops as he visits them.
MERCHANT_CHESTS = {
    "Arcadia's Cauldron": 0x0009CD46,  # Arcadia (apothecary, Whiterun)
}

# REFR change-flag bits that trigger the actor-level EXTRADATA section
# (ReSaver ChangeFormRefr.java).
_REFR_EXTRADATA_BITS = (6, 12, 29, 31, 11, 17, 25, 26, 10)
_REFR_EXTRADATA_MASK = sum(1 << b for b in _REFR_EXTRADATA_BITS)


def parse_refr_inventory(data: bytes, change_flags: int,
                         refid_kind: int) -> List[Tuple[Tuple[int, int, int], int]]:
    """Walk a REFR (container) change-data body; return [((b0,b1,b2), count)].

    Section order per ReSaver's ChangeFormRefr.java. Simpler than ACHR:
    no animation blob, no actor-level sections.
    """
    cur = Cursor(data, 0)

    # 1. initial data (same type derivation as ACHR).
    if refid_kind == 2:      # CREATED
        initial_type = 5
    elif change_flags & ((1 << 25) | (1 << 3)):  # PROMOTED | CELL_CHANGED
        initial_type = 6
    elif change_flags & ((1 << 2) | (1 << 1)):    # HAVOK_MOVE | MOVE
        initial_type = 4
    else:
        initial_type = 0
    if initial_type not in _INITIAL_DATA_SIZES:
        raise SaveParseError("bad REFR initialType %d" % initial_type)
    cur.skip(_INITIAL_DATA_SIZES[initial_type])

    # 2. havok blob.
    if change_flags & (1 << 2):
        cur.skip(_read_vsval(cur))

    # 3-5. conditional fixed sections (no always-present block for REFR).
    if change_flags & (1 << 0):
        cur.skip(6)          # ChangeFormFlags
    if change_flags & (1 << 7):
        cur.skip(3)          # RefID base object
    if change_flags & (1 << 4):
        cur.skip(4)          # float32 scale

    # 6. extra data.
    if change_flags & _REFR_EXTRADATA_MASK:
        _skip_extradata(cur)

    # 7. inventory.
    raw_items = []
    if change_flags & ((1 << 5) | (1 << 27)):
        expected = _read_vsval(cur)
        for _ in range(expected):
            try:
                b0, b1, b2 = cur.u8(), cur.u8(), cur.u8()
                count = cur.i32()
                _skip_extradata(cur)
                raw_items.append(((b0, b1, b2), count))
            except (SaveParseError, IndexError):
                break
    # 8. promotion data (RefID array) -- skip if present.
    if change_flags & (1 << 25):
        for _ in range(_read_vsval(cur)):
            cur.skip(3)
    return raw_items


def extract_merchant_inventory(path: str, location: str
                               ) -> List[Tuple[int, int]]:
    """Get the merchant chest inventory for the given location name.

    Returns [(item formID, count)] or [] if no chest is mapped / found.
    """
    chest_fid = MERCHANT_CHESTS.get(location)
    if chest_fid is None:
        return []
    import zlib
    body, _info, flt_base = load_decompressed(path)
    cur = Cursor(body, 0)
    cur.u8()  # form version
    cur.u32()  # plugin info size
    for _ in range(cur.u8()):
        cur.wstring()
    for _ in range(cur.u16()):
        cur.wstring()
    # file location table
    formid_array_off = cur.u32() - flt_base
    cur.skip(3 * 4)  # unknown, global1, global2
    change_forms_off = cur.u32() - flt_base
    cur.skip(4)  # global3
    cur.skip(3 * 4)  # table counts
    change_form_count = cur.u32()
    cur.skip(15 * 4)  # unused
    fc = Cursor(body, formid_array_off)
    formid_array = [fc.u32() for _ in range(fc.u32())]
    cc = Cursor(body, change_forms_off)
    for _ in range(change_form_count):
        b0, b1, b2 = cc.u8(), cc.u8(), cc.u8()
        change_flags = cc.u32()
        type_byte = cc.u8()
        _ver = cc.u8()
        size_class = type_byte >> 6
        if size_class == 0:
            length1, length2 = cc.u8(), cc.u8()
        elif size_class == 1:
            length1, length2 = cc.u16(), cc.u16()
        else:
            length1, length2 = cc.u32(), cc.u32()
        data = cc.raw(length1)
        if length2:
            data = zlib.decompress(data)
        if resolve_refid(b0, b1, b2, formid_array) == chest_fid:
            raw = parse_refr_inventory(data, change_flags, b0 >> 6)
            return [(resolve_refid(x0, x1, x2, formid_array), count)
                    for (x0, x1, x2), count in raw]
    return []


def extract_player_inventory(
        path: str) -> Tuple[List[Tuple[int, int]], dict, List[str], List[str]]:
    """Parse a save.

    Returns (raw_items, header_info, plugins, light_plugins) where raw_items
    is [(item formID, count), ...] from the player's ACHR change form.
    """
    body, info, flt_base = load_decompressed(path)
    cur = Cursor(body, 0)

    _form_version = cur.u8()

    # --- plugin lists (the save's load order at save time) ---
    # ReSaver PluginInfo: u32 size (excluding itself) | u8 full count |
    # full names | u16 light count | light names.
    _plugin_info_size = cur.u32()
    plugin_count = cur.u8()
    plugins = [cur.wstring() for _ in range(plugin_count)]
    light_plugin_count = cur.u16()
    light_plugins = [cur.wstring() for _ in range(light_plugin_count)]

    # --- file location table ---
    flt = {}
    flt["formIDArrayCountOffset"] = cur.u32() - flt_base
    flt["unknownTable3Offset"] = cur.u32() - flt_base
    flt["globalDataTable1Offset"] = cur.u32() - flt_base
    flt["globalDataTable2Offset"] = cur.u32() - flt_base
    flt["changeFormsOffset"] = cur.u32() - flt_base
    flt["globalDataTable3Offset"] = cur.u32() - flt_base
    flt["globalDataTable1Count"] = cur.u32()
    flt["globalDataTable2Count"] = cur.u32()
    flt["globalDataTable3Count"] = cur.u32()
    flt["changeFormCount"] = cur.u32()
    cur.skip(15 * 4)  # unused
    for key in ("formIDArrayCountOffset", "changeFormsOffset"):
        if not 0 <= flt[key] < len(body):
            raise SaveParseError(
                "file-location-table offset %s=%d is outside the %d-byte body"
                % (key, flt[key] + flt_base, len(body)))

    # --- formID array (needed to resolve RefID kind 0) ---
    fc = Cursor(body, flt["formIDArrayCountOffset"])
    formid_array_count = fc.u32()
    formid_array = [fc.u32() for _ in range(formid_array_count)]

    # --- walk the change forms, find the player's ACHR ---
    cc = Cursor(body, flt["changeFormsOffset"])
    for _ in range(flt["changeFormCount"]):
        b0, b1, b2 = cc.u8(), cc.u8(), cc.u8()
        change_flags = cc.u32()
        type_byte = cc.u8()
        _ver = cc.u8()
        size_class = type_byte >> 6
        form_type = type_byte & 0x3F
        if size_class == 0:
            length1, length2 = cc.u8(), cc.u8()
        elif size_class == 1:
            length1, length2 = cc.u16(), cc.u16()
        elif size_class == 2:
            length1, length2 = cc.u32(), cc.u32()
        else:
            raise SaveParseError("bad change-form size class")
        data = cc.raw(length1)
        if length2:
            data = zlib.decompress(data)
            if len(data) != length2:
                raise SaveParseError("change-form zlib size mismatch")

        fid = resolve_refid(b0, b1, b2, formid_array)
        if fid == PLAYER_FORMID and form_type == 1:  # 1 = ACHR
            refid_kind = b0 >> 6
            raw_items = parse_achr_inventory(data, change_flags, refid_kind,
                                             formid_array)
            items = [(resolve_refid(x0, x1, x2, formid_array), count)
                     for (x0, x1, x2), count in raw_items]
            return items, info, plugins, light_plugins

    raise SaveParseError(
        "player ACHR change form (formID 0x00000014) not found in %s" % path)


def extract_known_ingredients(path: str) -> Dict[int, int]:
    """Parse a save for discovered alchemy effects.

    Returns {ingredient_formID: bitmask} where bitmask bit j (0-3) is set
    if effect j is known. From SkyrimAlchemyHelper (GPL-2.0):
    ChangeForms with formType 16 ("Known ingredients") have 4 bytes of data,
    first byte is a bitmask for the 4 effects.

    Discovered 2026-10-05 via differential analysis: player ate a Purple
    Mountain Flower (0x00077E1E) in a test save; the diff showed a new
    type-16 ChangeForm with data 01 00 00 00 (bit 0 = first effect known).
    """
    body, info, flt_base = load_decompressed(path)
    cur = Cursor(body, 0)

    _form_version = cur.u8()
    _plugin_info_size = cur.u32()
    plugin_count = cur.u8()
    plugins = [cur.wstring() for _ in range(plugin_count)]
    light_plugin_count = cur.u16()
    light_plugins = [cur.wstring() for _ in range(light_plugin_count)]

    flt = {}
    flt["formIDArrayCountOffset"] = cur.u32() - flt_base
    flt["unknownTable3Offset"] = cur.u32() - flt_base
    flt["globalDataTable1Offset"] = cur.u32() - flt_base
    flt["globalDataTable2Offset"] = cur.u32() - flt_base
    flt["changeFormsOffset"] = cur.u32() - flt_base
    flt["globalDataTable3Offset"] = cur.u32() - flt_base
    flt["globalDataTable1Count"] = cur.u32()
    flt["globalDataTable2Count"] = cur.u32()
    flt["globalDataTable3Count"] = cur.u32()
    flt["changeFormCount"] = cur.u32()

    fc = Cursor(body, flt["formIDArrayCountOffset"])
    formid_array_count = fc.u32()
    formid_array = [fc.u32() for _ in range(formid_array_count)]

    known: Dict[int, int] = {}
    cc = Cursor(body, flt["changeFormsOffset"])
    for _ in range(flt["changeFormCount"]):
        b0, b1, b2 = cc.u8(), cc.u8(), cc.u8()
        _change_flags = cc.u32()
        type_byte = cc.u8()
        _ver = cc.u8()
        size_class = type_byte >> 6
        form_type = type_byte & 0x3F
        if size_class == 0:
            length1, length2 = cc.u8(), cc.u8()
        elif size_class == 1:
            length1, length2 = cc.u16(), cc.u16()
        elif size_class == 2:
            length1, length2 = cc.u32(), cc.u32()
        else:
            raise SaveParseError("bad change-form size class")
        data = cc.raw(length1)
        if length2:
            try:
                data = zlib.decompress(data)
            except Exception:
                pass  # not actually compressed, or corrupt; skip

        if form_type == 16 and len(data) == 4:
            try:
                fid = resolve_refid(b0, b1, b2, formid_array)
            except SaveParseError:
                continue
            if fid:
                known[fid] = data[0]  # bitmask: bit j = effect j known

    return known


# ---------------------------------------------------------------------------
# Auto-detect alchemy skill and perks from save (2026-10-05)
# ---------------------------------------------------------------------------
# Research (2026-10-05, verified against player saves):
# - Base Alchemy skill: NPC_ 0x00000007, type 9, bit 9 (0x200) → 52-byte DNAM,
#   byte 10 = Alchemy (base + racial). Skill-use level-ups NOT in save.
# - Perks: ACHR 0x00000014, type 1, search decompressed data for
#   (u8 rank + 3-byte RefID) pairs. RefID kind 1 = Skyrim.esm.
PLAYER_BASE_FORMID = 0x00000007

# Perk FormIDs (Skyrim.esm) → RefID bytes (kind 1: 0x40 | high bits)
_PERK_REFIDS = {
    # Alchemist ranks 1-5
    bytes([0x4B, 0xE1, 0x27]): ("alchemist", 1),  # 0x000BE127
    bytes([0x4C, 0x07, 0xCA]): ("alchemist", 2),  # 0x000C07CA
    bytes([0x4C, 0x07, 0xCB]): ("alchemist", 3),  # 0x000C07CB
    bytes([0x4C, 0x07, 0xCC]): ("alchemist", 4),  # 0x000C07CC
    bytes([0x4C, 0x07, 0xCD]): ("alchemist", 5),  # 0x000C07CD
    bytes([0x45, 0x82, 0x16]): ("benefactor", 1),  # 0x00058216
    bytes([0x45, 0x82, 0x17]): ("poisoner", 1),    # 0x00058217
    bytes([0x45, 0x82, 0x15]): ("physician", 1),   # 0x00058215
    bytes([0x45, 0x82, 0x1D]): ("purity", 1),      # 0x0005821D
}


def _walk_change_forms(path: str):
    """Yield (fid, form_type, change_flags, data) for each ChangeForm.

    Shared helper for auto-detect functions. Returns (body, info, plugins,
    light_plugins, formid_array) plus a generator.
    """
    body, info, flt_base = load_decompressed(path)
    cur = Cursor(body, 0)
    _form_version = cur.u8()
    _plugin_info_size = cur.u32()
    plugin_count = cur.u8()
    plugins = [cur.wstring() for _ in range(plugin_count)]
    light_plugin_count = cur.u16()
    light_plugins = [cur.wstring() for _ in range(light_plugin_count)]

    formIDArrayCountOffset = cur.u32() - flt_base
    cur.skip(3 * 4)  # unknownTable3, globalData1, globalData2
    changeFormsOffset = cur.u32() - flt_base
    cur.skip(4)  # global3
    cur.skip(3 * 4)  # table counts
    changeFormCount = cur.u32()

    fc = Cursor(body, formIDArrayCountOffset)
    formid_array = [fc.u32() for _ in range(fc.u32())]

    cc = Cursor(body, changeFormsOffset)
    for _ in range(changeFormCount):
        b0, b1, b2 = cc.u8(), cc.u8(), cc.u8()
        change_flags = cc.u32()
        type_byte = cc.u8()
        _ver = cc.u8()
        size_class = type_byte >> 6
        form_type = type_byte & 0x3F
        if size_class == 0:
            length1, length2 = cc.u8(), cc.u8()
        elif size_class == 1:
            length1, length2 = cc.u16(), cc.u16()
        elif size_class == 2:
            length1, length2 = cc.u32(), cc.u32()
        else:
            continue  # bad size class, skip
        try:
            data = cc.raw(length1)
        except (ValueError, IndexError):
            break
        if length2:
            try:
                data = zlib.decompress(data)
            except Exception:
                pass
        try:
            fid = resolve_refid(b0, b1, b2, formid_array)
        except SaveParseError:
            continue
        yield fid, form_type, change_flags, data


def extract_base_alchemy(path: str) -> int | None:
    """Get base Alchemy skill (with racial) from save.

    Returns the u8 value from NPC_ DNAM byte 10, or None if not found.
    Note: This is BASE + racial, NOT including skill-use level-ups
    (which are not stored in the save in a verified location).
    """
    for fid, form_type, change_flags, data in _walk_change_forms(path):
        if fid != PLAYER_BASE_FORMID or form_type != 9:  # 9 = NPC_
            continue
        if not (change_flags & 0x200):  # bit 9 = CHANGE_NPC_SKILLS
            return None
        try:
            cur = Cursor(data, 0)
            # Skip sections before DNAM per ReSaver ChangeFormNPC.java order:
            # bit0 (6B) → bit1 (24B) → bit6 (factions) → bit4 (3× RefID lists)
            # → bit3 (20B) → bit5 (wstring) → bit9 (DNAM 52B)
            if change_flags & 0x001:
                cur.skip(6)
            if change_flags & 0x002:
                cur.skip(24)
            if change_flags & 0x040:  # bit6: factions
                for _ in range(_read_vsval(cur)):
                    cur.skip(4)  # 3B RefID + u8 rank
            if change_flags & 0x010:  # bit4: 3× RefID lists
                for _ in range(3):
                    for _ in range(_read_vsval(cur)):
                        cur.skip(3)
            if change_flags & 0x008:
                cur.skip(20)
            if change_flags & 0x020:  # bit5: wstring
                _skip_wstring(cur)
            # Now at DNAM (52 bytes)
            dnam = cur.raw(52)
            return dnam[10]  # byte 10 = Alchemy
        except (ValueError, IndexError, SaveParseError):
            return None
    return None


def extract_perks(path: str) -> dict:
    """Get alchemy perks from save.

    Returns {alchemist_ranks: int, benefactor: bool, poisoner: bool,
             physician: bool, purity: bool}.
    Searches player ACHR decompressed data for (rank + RefID) pairs.
    """
    result = {
        "alchemist_ranks": 0,
        "benefactor": False,
        "poisoner": False,
        "physician": False,
        "purity": False,
    }
    for fid, form_type, change_flags, data in _walk_change_forms(path):
        if fid != PLAYER_FORMID or form_type != 1:  # 1 = ACHR
            continue
        # Search for each perk RefID pattern
        for refid_bytes, (perk_name, rank) in _PERK_REFIDS.items():
            # Find all occurrences
            start = 0
            while True:
                idx = data.find(refid_bytes, start)
                if idx == -1:
                    break
                # Rank byte is immediately before the RefID
                if idx > 0:
                    rank_byte = data[idx - 1]
                    if perk_name == "alchemist":
                        # Count ranks: highest rank found = total ranks
                        # (ranks are sequential, so rank 2 implies rank 1)
                        if rank_byte >= 1 and rank > result["alchemist_ranks"]:
                            result["alchemist_ranks"] = rank
                    else:
                        # For boolean perks, rank_byte >= 1 means taken
                        if rank_byte >= 1:
                            result[perk_name] = True
                start = idx + 1
        break  # only need the player ACHR
    return result


def extract_known_spells(path: str) -> List[int]:
    """Parse a save for known spells.

    Returns [spell_formID, ...]. From save format research (2026-10-05):
    ChangeForms with formType 13 have change_flags=0x40 and 1 byte of data.
    Player knows 3 spells, and there are exactly 3 type-13 ChangeForms.

    Discovered via Reddit r/skyrimmods thread: known spells/enchantments are
    tracked by a flag on the base Form record, not as a separate list.
    """
    body, info, flt_base = load_decompressed(path)
    cur = Cursor(body, 0)

    _form_version = cur.u8()
    _plugin_info_size = cur.u32()
    plugin_count = cur.u8()
    plugins = [cur.wstring() for _ in range(plugin_count)]
    light_plugin_count = cur.u16()
    light_plugins = [cur.wstring() for _ in range(light_plugin_count)]

    flt = {}
    flt["formIDArrayCountOffset"] = cur.u32() - flt_base
    flt["unknownTable3Offset"] = cur.u32() - flt_base
    flt["globalDataTable1Offset"] = cur.u32() - flt_base
    flt["globalDataTable2Offset"] = cur.u32() - flt_base
    flt["changeFormsOffset"] = cur.u32() - flt_base
    flt["globalDataTable3Offset"] = cur.u32() - flt_base
    flt["globalDataTable1Count"] = cur.u32()
    flt["globalDataTable2Count"] = cur.u32()
    flt["globalDataTable3Count"] = cur.u32()
    flt["changeFormCount"] = cur.u32()

    fc = Cursor(body, flt["formIDArrayCountOffset"])
    formid_array_count = fc.u32()
    formid_array = [fc.u32() for _ in range(formid_array_count)]

    known: List[int] = []
    cc = Cursor(body, flt["changeFormsOffset"])
    for _ in range(flt["changeFormCount"]):
        b0, b1, b2 = cc.u8(), cc.u8(), cc.u8()
        _change_flags = cc.u32()
        type_byte = cc.u8()
        _ver = cc.u8()
        size_class = type_byte >> 6
        form_type = type_byte & 0x3F
        if size_class == 0:
            length1, length2 = cc.u8(), cc.u8()
        elif size_class == 1:
            length1, length2 = cc.u16(), cc.u16()
        elif size_class == 2:
            length1, length2 = cc.u32(), cc.u32()
        else:
            raise SaveParseError("bad change-form size class")
        data = cc.raw(length1)
        if length2:
            try:
                data = zlib.decompress(data)
            except Exception:
                pass

        if form_type == 13:
            try:
                fid = resolve_refid(b0, b1, b2, formid_array)
            except SaveParseError:
                continue
            if fid:
                known.append(fid)

    return sorted(known)


def extract_known_enchantments(path: str) -> List[int]:
    """Parse a save for known enchantments (disenchanted).

    Returns [enchantment_formID, ...]. From save format research (2026-10-05):
    ChangeForms with formType 48 have change_flags=0x01 and 6 bytes of data.
    Player has disenchanted 4 items, and there are exactly 4 type-48 ChangeForms.

    Discovered via Reddit r/skyrimmods thread: known enchantments are tracked
    by a flag on the base Form record, not as a separate list.
    """
    body, info, flt_base = load_decompressed(path)
    cur = Cursor(body, 0)

    _form_version = cur.u8()
    _plugin_info_size = cur.u32()
    plugin_count = cur.u8()
    plugins = [cur.wstring() for _ in range(plugin_count)]
    light_plugin_count = cur.u16()
    light_plugins = [cur.wstring() for _ in range(light_plugin_count)]

    flt = {}
    flt["formIDArrayCountOffset"] = cur.u32() - flt_base
    flt["unknownTable3Offset"] = cur.u32() - flt_base
    flt["globalDataTable1Offset"] = cur.u32() - flt_base
    flt["globalDataTable2Offset"] = cur.u32() - flt_base
    flt["changeFormsOffset"] = cur.u32() - flt_base
    flt["globalDataTable3Offset"] = cur.u32() - flt_base
    flt["globalDataTable1Count"] = cur.u32()
    flt["globalDataTable2Count"] = cur.u32()
    flt["globalDataTable3Count"] = cur.u32()
    flt["changeFormCount"] = cur.u32()

    fc = Cursor(body, flt["formIDArrayCountOffset"])
    formid_array_count = fc.u32()
    formid_array = [fc.u32() for _ in range(formid_array_count)]

    known: List[int] = []
    cc = Cursor(body, flt["changeFormsOffset"])
    for _ in range(flt["changeFormCount"]):
        b0, b1, b2 = cc.u8(), cc.u8(), cc.u8()
        _change_flags = cc.u32()
        type_byte = cc.u8()
        _ver = cc.u8()
        size_class = type_byte >> 6
        form_type = type_byte & 0x3F
        if size_class == 0:
            length1, length2 = cc.u8(), cc.u8()
        elif size_class == 1:
            length1, length2 = cc.u16(), cc.u16()
        elif size_class == 2:
            length1, length2 = cc.u32(), cc.u32()
        else:
            raise SaveParseError("bad change-form size class")
        data = cc.raw(length1)
        if length2:
            try:
                data = zlib.decompress(data)
            except Exception:
                pass

        if form_type == 48:
            try:
                fid = resolve_refid(b0, b1, b2, formid_array)
            except SaveParseError:
                continue
            if fid:
                known.append(fid)

    return sorted(known)


# ---------------------------------------------------------------------------
# Ingredient database + potion math
# ---------------------------------------------------------------------------

def load_ingredient_db(path: str):
    """Load ingredients.json.

    Returns (lookup, ingredients) where lookup maps
    (plugin_filename.lower(), object_id_int) -> ingredient dict.
    """
    with open(path, "r", encoding="utf-8") as f:
        db = json.load(f)
    lookup: Dict[Tuple[str, int], dict] = {}
    for ing in db["ingredients"]:
        for fm in ing.get("formids", []):
            try:
                obj_id = int(fm["objectID"], 16)
            except (ValueError, KeyError, TypeError):
                continue
            lookup[(fm["plugin"].lower(), obj_id)] = ing
    return lookup, db["ingredients"]


def inventory_to_ingredients(raw_items: List[Tuple[int, int]],
                             plugins: List[str], light_plugins: List[str],
                             lookup: Dict[Tuple[str, int], dict]
                             ) -> List[dict]:
    """Map raw (formID, count) pairs to named ingredients with effects."""
    owned: Dict[str, dict] = {}
    for fid, count in raw_items:
        plugin, obj_id = split_formid(fid, plugins, light_plugins)
        if plugin is None or count <= 0:
            continue
        ing = lookup.get((plugin.lower(), obj_id))
        if ing is None:
            continue  # not an alchemy ingredient; ignore
        name = ing["name"]
        if name in owned:
            owned[name]["count"] += count
        else:
            owned[name] = {"name": name, "count": count,
                           "effects": list(ing["effects"])}
    return sorted(owned.values(), key=lambda d: d["name"].lower())


# ---------------------------------------------------------------------------
# Potion pricing
# ---------------------------------------------------------------------------
# Standard gold value of a single-effect potion brewed at 100 Alchemy with
# no perks, per UESP's "Skyrim:Alchemy Effects" table (its last column).
# A potion's price is the sum of its effects' values. This is deliberately
# skill-agnostic: relative ranking is what matters for v1, and every
# player's Alchemy skill/perks scale all potions the same way, so the order
# barely moves. (Fortify Persuasion isn't on UESP's table; it uses the same
# MGEF pattern as the other Fortify-skill effects, so it gets their value.)
EFFECT_VALUES = {
    "Cure Disease": 21,
    "Cure Poison": 3,
    "Damage Health": 3,
    "Damage Magicka": 52,
    "Damage Magicka Regen": 265,
    "Damage Stamina": 43,
    "Damage Stamina Regen": 159,
    "Fear": 120,
    "Fortify Alteration": 47,
    "Fortify Barter": 48,
    "Fortify Block": 118,
    "Fortify Carry Weight": 208,
    "Fortify Conjuration": 75,
    "Fortify Destruction": 151,
    "Fortify Enchanting": 14,
    "Fortify Health": 82,
    "Fortify Heavy Armor": 55,
    "Fortify Illusion": 94,
    "Fortify Light Armor": 55,
    "Fortify Lockpicking": 25,
    "Fortify Magicka": 71,
    "Fortify Marksman": 118,
    "Fortify One-handed": 118,
    "Fortify Persuasion": 118,
    "Fortify Pickpocket": 118,
    "Fortify Restoration": 118,
    "Fortify Smithing": 82,
    "Fortify Sneak": 118,
    "Fortify Stamina": 71,
    "Fortify Two-handed": 118,
    "Frenzy": 107,
    "Invisibility": 261,
    "Light": 25,
    "Lingering Damage Health": 86,
    "Lingering Damage Magicka": 71,
    "Lingering Damage Stamina": 12,
    "Night Eye": 38,
    "Paralysis": 285,
    "Ravage Health": 6,
    "Ravage Magicka": 15,
    "Ravage Stamina": 24,
    "Regenerate Health": 177,
    "Regenerate Magicka": 177,
    "Regenerate Stamina": 177,
    "Resist Fire": 86,
    "Resist Frost": 86,
    "Resist Magic": 51,
    "Resist Poison": 118,
    "Resist Shock": 86,
    "Restore Health": 21,
    "Restore Magicka": 25,
    "Restore Stamina": 25,
    "Slow": 247,
    "Spell Absorption": 380,
    "Waterbreathing": 100,
    "Weakness to Fire": 48,
    "Weakness to Frost": 40,
    "Weakness to Magic": 51,
    "Weakness to Poison": 51,
    "Weakness to Shock": 56,
}


def potion_price(effects, skill=100, alchemist_ranks=0,
                 benefactor=False, poisoner=False, physician=False) -> int:
    """Sum of per-effect values, adjusted for skill and perks (2026-10-05).

    Uses UESP's formula structure: value scales with magnitude, which scales
    with skill and perk multipliers. Beneficial vs harmful effects get
    different perk bonuses (Benefactor/Poisoner), and Restore effects get
    Physician. This changes RELATIVE ranking, not just absolute values.
    """
    # Effects flagged as beneficial in Skyrim's data (get Benefactor bonus).
    BENEFICIAL = {
        "Cure Disease", "Cure Poison",
        "Fortify Alteration", "Fortify Barter", "Fortify Block",
        "Fortify Carry Weight", "Fortify Conjuration", "Fortify Destruction",
        "Fortify Enchanting", "Fortify Health", "Fortify Heavy Armor",
        "Fortify Illusion", "Fortify Light Armor", "Fortify Lockpicking",
        "Fortify Magicka", "Fortify Marksman", "Fortify One-handed",
        "Fortify Persuasion", "Fortify Pickpocket", "Fortify Restoration",
        "Fortify Smithing", "Fortify Sneak", "Fortify Stamina",
        "Fortify Two-handed",
        "Regenerate Health", "Regenerate Magicka", "Regenerate Stamina",
        "Resist Fire", "Resist Frost", "Resist Magic", "Resist Poison",
        "Resist Shock",
        "Restore Health", "Restore Magicka", "Restore Stamina",
        "Invisibility", "Light", "Night Eye", "Waterbreathing",
        "Spell Absorption",
    }
    # Restore effects get Physician bonus (stacks with Benefactor).
    RESTORE = {"Restore Health", "Restore Magicka", "Restore Stamina"}

    # Skill factor: simplified from UESP's (1 + (skill/100)*0.5) component.
    # At 100 skill this is 1.5; at 36 it's ~1.18. Relative scaling matters.
    skill_factor = 1 + (skill / 100) * 0.5
    # Normalize to 100-skill baseline so default values match EFFECT_VALUES.
    skill_norm = skill_factor / 1.5

    alch_mult = 1 + (0.2 * alchemist_ranks)

    total = 0
    for e in effects:
        base = EFFECT_VALUES.get(e, 0)
        if base == 0:
            continue
        # Perk multiplier for this specific effect.
        mult = alch_mult
        if e in BENEFICIAL:
            if benefactor:
                mult *= 1.25
            if e in RESTORE and physician:
                mult *= 1.25
        else:  # harmful
            if poisoner:
                mult *= 1.25
        # UESP value formula uses ^1.1 exponent on (magnitude*duration).
        # We approximate by applying it to the combined multiplier.
        adjusted = base * skill_norm * (mult ** 1.1)
        total += adjusted
    return int(total)


def potion_price_base(effects) -> int:
    """Legacy: sum of standard values at 100 skill, no perks."""
    return sum(EFFECT_VALUES.get(e, 0) for e in effects)


def _make_potion(effects, ings, skill=15, alchemist_ranks=0,
                 benefactor=True, poisoner=True, physician=True):
    """Build potion dict with dynamic value (player stats as defaults)."""
    return {
        "effects": effects,
        "ingredients": [g["name"] for g in ings],
        "counts": [g["count"] for g in ings],
        # How many you can brew before one stack runs out:
        "batches": min(g["count"] for g in ings),
        "n": len(ings),
        "price": potion_price(effects, skill=skill,
                             alchemist_ranks=alchemist_ranks,
                             benefactor=benefactor, poisoner=poisoner,
                             physician=physician),
    }


def brewable_by_effect(owned: List[dict], skill=15, alchemist_ranks=0,
                       benefactor=True, poisoner=True,
                       physician=True) -> List[dict]:
    """Find brewable potions by grouping ingredients by effect.

    Player insight (2026-10-05): there are only ~20-50 valuable effects.
    Group owned ingredients by effect, then check pairs within each group.
    O(E * I_e^2) not O(n^3) — and no 933KB table needed.

    Potion values use the given alchemy skill/perks (2026-10-05).

    For each pair sharing at least one effect, the potion has ALL effects
    shared by the pair (matching in-game alchemy).
    """
    # Group ingredient indices by effect
    effect_to_idxs = {}
    for i, o in enumerate(owned):
        for eff in o.get("effects", []):
            effect_to_idxs.setdefault(eff, []).append(i)

    seen_pairs = set()
    potions = []
    effsets = [set(o.get("effects", [])) for o in owned]

    for eff, idxs in effect_to_idxs.items():
        if len(idxs) < 2:
            continue
        # Check all pairs in this effect group
        for a in range(len(idxs)):
            for b in range(a + 1, len(idxs)):
                i, j = idxs[a], idxs[b]
                key = (min(i, j), max(i, j))
                if key in seen_pairs:
                    continue
                seen_pairs.add(key)
                shared = effsets[i] & effsets[j]
                if shared:
                    ings = [owned[i], owned[j]]
                    # Batch count = min available
                    batches = min(owned[i]["count"], owned[j]["count"])
                    potions.append({
                        "effects": sorted(shared),
                        "price": potion_price(sorted(shared)),
                        "ingredients": [owned[i]["name"], owned[j]["name"]],
                        "batches": batches,
                        "n": 2,
                    })

    potions.sort(key=lambda p: (-p["price"], -len(p["effects"]),
                                p["ingredients"]))
    return potions


def load_potion_table(path: str) -> List[dict]:
    """Load the pre-computed valuable potion table (top_potions.json).

    Generated offline via compute_potions on all 191 ingredients.
    Each entry: {effects, price, ingredients: [names]}.
    """
    import os
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return json.load(f)


def brewable_from_table(owned: List[dict], potion_table: List[dict]) -> List[dict]:
    """Find potions the player can brew.

    owned: [{name, effects, count}]
    Returns potions sorted by price desc, with batch counts based on
    available ingredient quantities. O(T) not O(n^3).
    """
    owned_counts = {o["name"]: o["count"] for o in owned}
    result = []
    for p in potion_table:
        ings = p["ingredients"]
        # Check if player has all ingredients (with count > 0)
        if not all(owned_counts.get(ing, 0) > 0 for ing in ings):
            continue
        # Batch count = min available across ingredients
        # (simplified: assumes 1 unit per ingredient per potion)
        batches = min(owned_counts.get(ing, 0) for ing in ings)
        if batches <= 0:
            continue
        result.append({
            "effects": p["effects"],
            "price": p["price"],
            "ingredients": ings,
            "batches": batches,
            "n": len(ings),
        })
    # Already sorted by price in the table, but ensure it
    result.sort(key=lambda p: -p["price"])
    return result


def shopping_by_effect(owned: List[dict], ing_db: dict, gold: int,
                      exclude=frozenset(), force_include=frozenset()) -> dict:
    """Build shopping list via effect-grouping (2026-10-05 approach).

    For each ingredient NOT owned, if it shares an effect with something the player
    owns, it's a candidate. Rank by profit. O(I*E) not O(n^3).
    """
    owned_names = {o["name"] for o in owned}
    owned_by_effect = {}
    for o in owned:
        for eff in o.get("effects", []):
            owned_by_effect.setdefault(eff, []).append(o["name"])

    suggestions = {}
    for ing in ing_db.values():
        name = ing["name"]
        if name in owned_names:
            continue
        if name in exclude and name not in force_include:
            continue
        effects = ing.get("effects", [])
        # Find the best pairing: for each shared effect, find an owned
        # ingredient with that effect, compute the pair's potion price.
        best_price = 0
        best_pair = None
        best_effects = []
        ing_effset = set(effects)
        for eff in effects:
            if eff not in owned_by_effect:
                continue
            for owned_name in owned_by_effect[eff]:
                # Find the owned ingredient's full effect set
                owned_ing = next((o for o in owned if o["name"] == owned_name), None)
                if not owned_ing:
                    continue
                shared = ing_effset & set(owned_ing.get("effects", []))
                if not shared:
                    continue
                price = potion_price(sorted(shared))
                if price > best_price:
                    best_price = price
                    best_pair = owned_name
                    best_effects = sorted(shared)
        if best_price == 0:
            continue
        cost = ing.get("base_value", 5) * 3
        profit = best_price * SELL_FACTOR - cost
        if profit <= 0 and name not in force_include:
            continue
        if name not in suggestions or profit > suggestions[name]["profit"]:
            suggestions[name] = {
                "name": name,
                "cost": cost,
                "best_price": best_price,
                "profit": profit,
                "effects": effects,
                "pairs_with": best_pair,
                "potion_effects": best_effects,
            }

    ranked = sorted(suggestions.values(), key=lambda s: -s["profit"])
    buys = []
    total_cost = 0
    for s in ranked:
        if total_cost + s["cost"] <= gold or not buys:
            buys.append({
                "name": s["name"],
                "cost": s["cost"],
                "qty": 1,
                "effects": s["effects"],
                "pairs_with": s["pairs_with"],
                "potion_effects": s["potion_effects"],
                "potion_price": s["best_price"],
            })
            total_cost += s["cost"]
            if total_cost >= gold and len(buys) >= 10:
                break

    skip = [{"name": s["name"], "cost": s["cost"], "best_price": s["best_price"]}
            for s in ranked if s["name"] not in {b["name"] for b in buys}]

    return {
        "buys": buys,
        "total_cost": total_cost,
        "total_value": sum(s["best_price"] for s in ranked[:len(buys)]),
        "total_sell": int(sum(s["best_price"] for s in ranked[:len(buys)]) * SELL_FACTOR),
        "skip": skip[:20],
        "gold": gold,
        "steps": [],  # No brew steps for the simplified shopping list
        "phases": [],
    }


def shopping_from_table(owned: List[dict], potion_table: List[dict],
                        ing_db: dict, gold: int,
                        exclude=frozenset(), force_include=frozenset()) -> dict:
    """Build a shopping list from the pre-computed potion table.

    Finds valuable potions player is exactly 1 ingredient away from brewing,
    ranks by profit. O(T) not O(n^3).
    Returns dict with buys, total_cost, etc. (compatible with shop plan format).
    """
    owned_counts = {o["name"]: o["count"] for o in owned}
    suggestions = {}  # name -> {name, cost, best_price, profit, effects}

    for p in potion_table:
        ings = p["ingredients"]
        # Skip if player can already brew it
        if all(owned_counts.get(ing, 0) > 0 for ing in ings):
            continue
        # Find missing ingredients
        missing = [ing for ing in ings if owned_counts.get(ing, 0) == 0]
        if len(missing) != 1:
            continue
        name = missing[0]
        if name in exclude and name not in force_include:
            continue
        ing_data = ing_db.get(name)
        if not ing_data:
            continue
        cost = ing_data.get("base_value", 5) * 3
        profit = p["price"] * SELL_FACTOR - cost
        # Only suggest if profitable (or force-included)
        if name not in force_include and profit <= 0:
            continue
        # Keep the best (highest profit) suggestion per ingredient
        if name not in suggestions or profit > suggestions[name]["profit"]:
            suggestions[name] = {
                "name": name,
                "cost": cost,
                "best_price": p["price"],
                "profit": profit,
                "effects": ing_data["effects"],
            }

    # Sort by profit descending
    ranked = sorted(suggestions.values(), key=lambda s: -s["profit"])

    # Build buys list (respecting gold)
    buys = []
    total_cost = 0
    for s in ranked:
        if total_cost + s["cost"] <= gold or not buys:
            # Buy at least one even if over gold (so list isn't empty)
            buys.append({
                "name": s["name"],
                "cost": s["cost"],
                "qty": 1,
                "effects": s["effects"],
            })
            total_cost += s["cost"]
            if total_cost >= gold and len(buys) >= 5:
                break

    # Don't-buy list: ingredients that didn't make the cut
    skip = []
    for s in ranked:
        if s["name"] not in {b["name"] for b in buys}:
            skip.append({
                "name": s["name"],
                "cost": s["cost"],
                "best_price": s["best_price"],
            })

    return {
        "buys": buys,
        "total_cost": total_cost,
        "total_value": sum(s["best_price"] for s in ranked[:len(buys)]),
        "total_sell": int(sum(s["best_price"] for s in ranked[:len(buys)]) * SELL_FACTOR),
        "skip": skip[:20],  # Top 20 skips
        "gold": gold,
    }


def compute_potions(owned: List[dict]) -> List[dict]:
    """Every 2- and 3-ingredient combo sharing at least one effect.

    A triple yields every effect shared by at least two of the three
    ingredients, matching in-game alchemy. Sorted by price descending.

    Complexity is O(n^3) in the ingredient count, but n is the number of
    distinct owned ingredients with count > 0 (a few dozen at most), and
    each step is a couple of tiny set intersections -- a few thousand
    combos, well under a second.
    """
    potions = []
    n = len(owned)
    effsets = [set(o["effects"]) for o in owned]
    for i in range(n):
        for j in range(i + 1, n):
            shared = effsets[i] & effsets[j]
            if shared:
                potions.append(_make_potion(sorted(shared),
                                            [owned[i], owned[j]],
                                            skill=skill,
                                            alchemist_ranks=alchemist_ranks,
                                            benefactor=benefactor,
                                            poisoner=poisoner,
                                            physician=physician))
    for i in range(n):
        ei = effsets[i]
        for j in range(i + 1, n):
            ej = effsets[j]
            ij = ei & ej
            for k in range(j + 1, n):
                ek = effsets[k]
                # Effect appears in the potion if >=2 ingredients share it.
                shared = ij | (ei & ek) | (ej & ek)
                if shared:
                    potions.append(_make_potion(
                        sorted(shared), [owned[i], owned[j], owned[k]],
                        skill=skill, alchemist_ranks=alchemist_ranks,
                        benefactor=benefactor, poisoner=poisoner,
                        physician=physician))
    potions.sort(key=lambda p: (-p["price"], -len(p["effects"]),
                                p["ingredients"]))
    return potions


def greedy_brew_total_static(ranked_potions: List[dict],
                             counts: dict, min_price: int = 100) -> int:
    """Best achievable total potion value from the ingredient counts.

    Uses brew_allocation (below), not a single greedy pass.
    ranked_potions must be sorted by price descending (compute_potions
    output). Used for the inventory-only baseline in the shop section.
    """
    total, _ = brew_allocation(ranked_potions, counts, min_price)
    return total


def _greedy_brew(potions: List[dict], counts: dict, min_price: int = 100,
                 banned=None) -> Tuple[int, list]:
    """One greedy pass over price-sorted potions.

    Returns (total_value, [brewed potion dicts]) in brew order. If banned
    is given (a potion dict), that potion is skipped -- used by
    brew_allocation to explore alternatives.
    """
    counts = dict(counts)
    brewed, total = [], 0
    for p in potions:
        if p["price"] < min_price:
            break
        if p is banned:
            continue
        ings = p["ingredients"]
        if all(counts.get(i, 0) > 0 for i in ings):
            for i in ings:
                counts[i] -= 1
            brewed.append(p)
            total += p["price"]
    return total, brewed


def brew_allocation(potions: List[dict], counts: dict,
                    min_price: int = 100) -> Tuple[int, list]:
    """Brew plan maximizing total potion value ("total haul").

    Plain greedy (best potion first) has a blind spot: a 3-ingredient
    potion always outranks its 2-ingredient subsets (the game can only
    add/strengthen effects), so greedy grabs the 3-pot even when its
    ingredients would brew for more as 2-pots, stranding value.

    Fix, in two phases (both deterministic):
    1. Ruin-and-recreate: for each distinct potion in the greedy plan,
       re-run greedy with that potion banned; keep improvements. Repeat
       until no single ban improves.
    2. Randomized greedy polish: re-run greedy many times over
       price order perturbed by fixed-seed noise, keeping the best.
       (Fixed seed => same input always gives same output.)
    Never worse than plain greedy. On a real 17-ingredient shop stock
    this reached the exact optimum (verified by branch-and-bound).
    """
    best_total, best_brewed = _greedy_brew(potions, counts, min_price)
    improved = True
    while improved:
        improved = False
        seen = set()
        for p in best_brewed:
            if id(p) in seen:
                continue
            seen.add(id(p))
            total, brewed = _greedy_brew(potions, counts, min_price,
                                        banned=p)
            if total > best_total:
                best_total, best_brewed = total, brewed
                improved = True
                break
    iters = max(400, min(2500, 900000 // max(1, len(potions))))
    rng = random.Random(99)
    indexed = list(potions)
    for _ in range(iters):
        order = sorted(indexed,
                       key=lambda p: -(p["price"] + rng.uniform(0, 150)))
        total, brewed = _greedy_brew(order, counts, min_price)
        if total > best_total:
            best_total, best_brewed = total, brewed
    return best_total, best_brewed


# Merchant reality checks (Empirical numbers at low Speech):
#  - Buy price ~= base_value x 3 (merchant markup).
#  - Sell price ~= potion list value x SELL_FACTOR (merchants pay ~1/4-1/3).
# Potion list value still matters beyond gold: Alchemy XP scales with it,
# and higher Alchemy means pricier potions and better sell prices later --
# so the plan reports both gold and XP value, not gold alone.
SELL_FACTOR = 0.3


def phase_shop_plan(steps: List[dict], buys: List[dict],
                    gold: int) -> Tuple[List[dict], List[dict]]:
    """Split a shop plan into gold-affordable phases.

    Player can't always afford the whole buy list at once. Each phase buys
    the most profitable potions that fit in current gold, brews them,
    sells them, and rolls the proceeds into the next phase:
      grab what you can afford -> brew these -> sell -> buy more -> ...
    Phases are ordered for maximum gp per visit: the most profitable
    potions come first, so quitting after any phase banks the most gold.
    Steps are tagged with their phase number (1-based, in place); steps
    that stay unaffordable even after selling everything get phase None.
    Returns (phases, unaffordable_steps). A single phase means the whole
    list is affordable at once.
    """
    if gold is None or gold < 0:
        for s in steps:
            s["phase"] = 1
        return [], []
    cost_map = {b["name"]: b["cost"] for b in buys}
    annotated = []
    for idx, s in enumerate(steps):
        buy_cost = sum(cost_map.get(i["name"], 0)
                       for i in s["ingredients"] if i["bought"])
        revenue = int(s["potion_price"] * SELL_FACTOR)
        profit = revenue - buy_cost
        annotated.append([idx, s, buy_cost, revenue, profit])
    # Most profitable first: maximum gp per visit.
    annotated.sort(key=lambda x: -x[4])
    phases = []
    remaining = annotated
    current_gold = gold
    n = 1
    while remaining:
        in_phase = []
        phase_cost = 0
        rest = []
        for item in remaining:
            if phase_cost + item[2] <= current_gold:
                in_phase.append(item)
                phase_cost += item[2]
            else:
                rest.append(item)
        if not in_phase:
            break
        phase_revenue = sum(item[3] for item in in_phase)
        buy_names = set()
        for _, s, _, _, _ in in_phase:
            for ing in s["ingredients"]:
                if ing["bought"]:
                    buy_names.add(ing["name"])
        phase_buys = sorted(
            (b for b in buys if b["name"] in buy_names),
            key=lambda b: b["name"])
        gold_after = current_gold - phase_cost + phase_revenue
        phases.append({
            "n": n,
            "buys": [{"name": b["name"], "cost": b["cost"], "qty": b["qty"],
                      "available": b["available"]} for b in phase_buys],
            "cost": phase_cost,
            "revenue": phase_revenue,
            "gold_before": current_gold,
            "gold_after": gold_after,
            "ranks": sorted(item[0] + 1 for item in in_phase),
        })
        for _, s, _, _, _ in in_phase:
            s["phase"] = n
        current_gold = gold_after
        remaining = rest
        n += 1
    unaffordable = [s for _, s, _, _, _ in remaining]
    for s in unaffordable:
        s["phase"] = None
    return phases, unaffordable


def shop_recommendations(owned: List[dict], chest_items: List[Tuple[str, int, int]],
                         player_gold: int, ing_db: dict,
                         min_price: int = 100,
                         exclude: frozenset = frozenset(),
                         force_include: frozenset = frozenset()) -> dict:
    """Build a buy-and-brew plan from the merchant's chest.

    owned: Player's ingredient dicts (name, effects, count).
    chest_items: [(ingredient name, base_value, count_available)] from the
        merchant chest.
    ing_db: name -> ingredient dict (for effects of items player doesn't own).
    exclude: ingredient names to skip (Player tapped X: not actually for
        sale in the live barter menu).
    force_include: ingredient names to buy even if not profitable
        (Player tapped + on the Don't-buy list: they want it anyway).
    Returns {"steps", "buys", "total_cost", "total_value", "total_sell"}:
      buys: [{name, cost}] in buy order -- one unit each.
      steps: potions unlocked by the purchases, in brew order; each step is
        {potion_effects, potion_price, ingredients: [{name, bought}]} where
        bought=True marks ingredients bought this trip (vs from inventory).
      total_cost: gold spent. total_value: listed value of ALL potions
        brewed from the combined pool. total_sell: realistic gold back
        when selling those potions (list value x SELL_FACTOR) -- this is
        the number that decides whether buying is worth it.
      skip: [{name, cost, best_price}] -- stock that didn't make the buy
        list, with the best potion price found (explicit Don't-buy list).
    Buy price ~= base_value x 3 (typical low-Speech merchant markup).
    Gold sequencing is handled by phase_shop_plan (phases in the payload),
    not by limiting the buy set here.
    """
    pool = {o["name"]: {"effects": o["effects"], "count": o["count"]}
            for o in owned}
    owned_names = set(pool)
    # Save owned counts before we add bought units to the pool.
    owned_counts = {n: v["count"] for n, v in pool.items()}
    # Drop anything player marked not-actually-for-sale.
    chest_items = [(n, bv, a) for n, bv, a in chest_items if n not in exclude]

    def effects_of(name):
        if name in pool:
            return pool[name]["effects"]
        d = ing_db.get(name)
        return d["effects"] if d else None

    # Rank chest ingredients by (best potion value - buy cost), evaluated
    # against the pool plus EVERY chest ingredient at once: the buys are
    # a set, so bought ingredients can combine with each other, not just
    # with the starting inventory. One compute_potions call covers all.
    rank_pool = {n: {"effects": v["effects"], "count": v["count"]}
                 for n, v in pool.items()}
    for name, base_value, avail in chest_items:
        if avail <= 0:
            continue
        hypo_effects = effects_of(name)
        if not hypo_effects:
            continue
        if name in rank_pool:
            rank_pool[name]["count"] += 1
        else:
            rank_pool[name] = {"effects": hypo_effects, "count": 1}
    rank_list = [{"name": n, "effects": v["effects"], "count": v["count"]}
                 for n, v in rank_pool.items()]
    ranked_potions = compute_potions(rank_list)
    ranked = []
    skip_info = {}
    for name, base_value, avail in chest_items:
        if avail <= 0:
            continue
        cost = base_value * 3
        hypo_effects = effects_of(name)
        if not hypo_effects:
            continue
        best = next((p for p in ranked_potions
                     if name in p["ingredients"] and p["price"] >= min_price),
                    None)
        # Worth buying only if realistic sell proceeds beat the price,
        # unless player force-included it (tapped + on Don't-buy: they want
        # it anyway). Gold sequencing is handled by phase_shop_plan.
        # (List value still drives Alchemy XP -- reported separately.)
        if name in force_include or (
                best and best["price"] * SELL_FACTOR > cost):
            profit = (best["price"] * SELL_FACTOR - cost) if best else 0
            ranked.append((profit, name, cost, hypo_effects))
        else:
            skip_info[name] = {
                "name": name, "cost": cost,
                "best_price": best["price"] if best else 0,
            }
    ranked.sort(reverse=True)

    # Take the full profitable buy set; phase_shop_plan sequences it
    # by gold affordability.
    buys = []
    for _, name, cost, hypo_effects in ranked:
        avail = next(a for n, _, a in chest_items if n == name)
        buys.append({"name": name, "cost": cost, "effects": hypo_effects,
                     "available": avail, "unit_cost": cost})
    bought_names = {b["name"] for b in buys}

    # Brew the combined pool greedily, best potion first.
    # Add ALL available units to the pool -- brew_allocation will use
    # what it needs; we prune to actual usage below.
    for b in buys:
        name = b["name"]
        avail = b["available"]
        if name in pool:
            pool[name]["count"] += avail
        else:
            pool[name] = {"effects": b["effects"], "count": avail}
    pool_list = [{"name": n, "effects": v["effects"], "count": v["count"]}
                 for n, v in pool.items() if v["count"] > 0]
    # Brew for maximum total value (see brew_allocation); the displayed
    # steps are the brewed potions that use bought ingredients.
    total_value, brewed = brew_allocation(
        compute_potions(pool_list),
        {n: v["count"] for n, v in pool.items() if v["count"] > 0},
        min_price)
    # Prune buys to ingredients the plan actually brews -- never pay for
    # stock that goes unused. Count actual usage to set buy quantities.
    from collections import Counter
    used_counts = Counter()
    for p in brewed:
        used_counts.update(p["ingredients"])
    # For bought ingredients, qty = bought units actually used.
    # (brew_allocation uses from the combined pool; owned stock is used
    # first, so bought_used = total_used - owned_count, capped at available.)
    pruned_buys = []
    for b in buys:
        name = b["name"]
        total_used = used_counts.get(name, 0)
        owned_count = owned_counts.get(name, 0)
        bought_used = max(0, total_used - owned_count)
        bought_used = min(bought_used, b["available"])
        if bought_used > 0:
            b["qty"] = bought_used
            b["cost"] = b["unit_cost"] * bought_used
            pruned_buys.append(b)
    buys = pruned_buys
    bought_names = {b["name"] for b in buys}
    # Ranked by potion value, highest first: #1 brews first for max XP.
    brewed = sorted(brewed, key=lambda p: -p["price"])
    steps = []
    for p in brewed:
        ings = p["ingredients"]
        if any(i in bought_names and i not in owned_names for i in ings):
            steps.append({
                "potion_effects": p["effects"],
                "potion_price": p["price"],
                "ingredients": [
                    {"name": i,
                     "bought": i in bought_names and i not in owned_names}
                    for i in ings],
            })
    # Split into gold-affordable phases (see phase_shop_plan); steps get
    # a "phase" tag, phases carry the buy/brew/sell sequence.
    phases, _ = phase_shop_plan(steps, buys, player_gold)
    # Explicit Don't-buy list: stock that failed the profitability filter,
    # with the best potion price found so player sees why.
    skip = sorted(skip_info.values(), key=lambda s: s["name"])
    return {
        "steps": steps,
        "buys": [{"name": b["name"], "cost": b["cost"], "qty": b["qty"],
                  "available": b["available"]} for b in buys],
        "total_cost": sum(b["cost"] for b in buys),
        "total_value": total_value,
        "total_sell": int(total_value * SELL_FACTOR),
        "phases": phases,
        "gold": player_gold,
        "skip": skip,
    }


# ---------------------------------------------------------------------------
# HTTP server + mobile web UI
# ---------------------------------------------------------------------------
# Design note: the potion math (combos, prices) runs in Python (unit-tested,
# stdlib-only) and the page is a renderer plus a brew queue. The queue lives
# entirely in page JavaScript: the server ships every viable 2- and
# 3-ingredient combo computed from the full inventory, and the page filters
# that list against a mutable "remaining" pool -- a combo is brewable iff
# every ingredient still has at least 1 left. Queueing deducts, removing
# restores, and the list re-renders against what's left. No combo search
# runs in JS, so it stays snappy on a phone.

PAGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Skyrim Alchemy Companion</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 0 0 4rem 0;
    font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
    background: #14110d; color: #e8ddc9;
    font-size: 18px; line-height: 1.45;
  }
  header { padding: 0.9rem 1rem 0.4rem; border-bottom: 1px solid #3a3226; }
  header h1 { margin: 0 0 0.5rem; font-size: 1.35rem; }
  header button, .btn {
    font-size: 1rem; padding: 0.55rem 0.9rem; border-radius: 0.5rem;
    border: 1px solid #6b5d43; background: #2a241a; color: #ffe9b0;
    cursor: pointer; min-height: 44px;
  }
  header button:active, .btn:active { background: #3a3226; }
  #status { margin-top: 0.4rem; font-size: 0.85rem; color: #b7a888; }
  #error {
    display: none; margin: 0.6rem 1rem 0; padding: 0.6rem;
    background: #4d1f1f; border: 1px solid #a33; border-radius: 0.5rem;
  }
  nav.tabs { display: flex; gap: 0.4rem; padding: 0.7rem 1rem 0; }
  nav.tabs button {
    flex: 1; font-size: 1.05rem; padding: 0.7rem 0.4rem; border-radius: 0.6rem 0.6rem 0 0;
    border: 1px solid #3a3226; border-bottom: none;
    background: #1d1913; color: #b7a888; cursor: pointer; min-height: 48px;
  }
  nav.tabs button.active { background: #2a241a; color: #ffe9b0; font-weight: bold; }
  .threshold-bar {
    display: flex; align-items: center; gap: 0.8rem;
    padding: 0.6rem 1rem; color: #c9b98a; font-size: 0.9rem;
  }
  .threshold-bar input[type="range"] { flex: 1; max-width: 320px; accent-color: #c9a227; }
  .threshold-bar #minval-show { font-weight: bold; color: #ffe9b0; }
  .effpills { display: flex; flex-wrap: wrap; gap: 0.3rem; margin-top: 0.3rem; }
  .effpill {
    font-size: 0.75rem; padding: 0.15rem 0.5rem; border-radius: 1rem;
    background: #1a1610; color: #6b5f45; border: 1px solid #3a3226;
    cursor: pointer; user-select: none;
  }
  .effpill.known { background: #2a3a1a; color: #b0d080; border-color: #5a7a3a; }
  .brewtag {
    font-size: 0.85rem; padding: 0.05rem 0.45rem; border-radius: 1rem;
    background: #1a1610; color: #e8ddc9; border: 1px solid #3a3226;
    margin: 0 0.2rem 0.15rem 0; display: inline-block; white-space: nowrap;
  }
  .brewtag.bought { background: #3a2f14; color: #ffd968; border-color: #8a6d2f; }
  /* Ingredient that would reveal a new effect if brewed (2026-10-05).
     The effect text itself is NOT colored; the ingredient gets a background. */
  .ing-discovery { background: #2a4a2a; padding: 0.1rem 0.4rem;
    border-radius: 0.25rem; }
  /* Disenchant tab (2026-10-05) */
  .disenchant-row { display: flex; justify-content: space-between;
    align-items: center; padding: 0.5rem; border-bottom: 1px solid #2a2a2a; }
  /* Alchemy settings (2026-10-05) */
  .alchemy-settings { margin: 0.5rem 1rem; padding: 0.5rem;
    border: 1px solid #3a3a3a; border-radius: 0.25rem; }
  .alchemy-settings summary { cursor: pointer; font-weight: bold; }
  .settings-grid { display: flex; flex-wrap: wrap; gap: 0.8rem;
    margin-top: 0.5rem; align-items: center; }
  .settings-grid label { display: flex; align-items: center; gap: 0.3rem; }
  .settings-grid button { padding: 0.3rem 1rem; border-radius: 0.25rem;
    border: 1px solid #5a5a5a; background: #2a4a2a; color: #e0e0e0;
    cursor: pointer; }
  .excl-x { color: #ff8a8a; text-decoration: none; font-weight: bold;
    cursor: pointer; font-size: 0.85rem; }
  .shopstep { border-bottom: 1px solid #2a2a2a; padding: 0.45rem 0; }
  .shopstep-h { font-weight: bold; margin-bottom: 0.3rem; }
  .shopsub { margin: 0.35rem 0 0.35rem 0.75rem; }
  .discover-badge {
    display: inline-block; font-size: 0.75rem; padding: 0.1rem 0.5rem;
    border-radius: 1rem; background: #3a2a1a; color: #ffcc80;
    margin-left: 0.5rem;
  }
  main { padding: 0 1rem; }
  section.tab { display: none; }
  section.tab.active { display: block; }

  /* Brew queue */
  #queue {
    margin: 0.7rem 0; padding: 0.6rem 0.7rem;
    background: #1e2a1a; border: 1px solid #4a6b43; border-radius: 0.6rem;
  }
  #queue h3 { margin: 0 0 0.4rem; font-size: 1rem; color: #cfe8c9; }
  #queue .q-total { color: #ffe9b0; font-weight: bold; }
  #queue .q-row {
    display: flex; align-items: center; gap: 0.5rem;
    padding: 0.45rem 0; border-top: 1px solid #33402e; font-size: 0.95rem;
  }
  #queue .q-row .q-info { flex: 1; }
  #queue .q-row .q-price { color: #ffd97a; font-weight: bold; white-space: nowrap; }
  #queue .q-empty { color: #8a9a86; font-size: 0.9rem; }
  .qbtn {
    font-size: 1rem; padding: 0.35rem 0.7rem; border-radius: 0.45rem;
    border: 1px solid #6b5d43; background: #2a241a; color: #ffe9b0;
    cursor: pointer; min-width: 44px; min-height: 44px;
  }

  /* Controls */
  .controls { display: flex; gap: 0.5rem; margin: 0.7rem 0; flex-wrap: wrap; }
  .controls input[type="search"], .controls select {
    font-size: 1rem; padding: 0.55rem; border-radius: 0.5rem;
    border: 1px solid #6b5d43; background: #1d1913; color: #e8ddc9;
    min-height: 44px;
  }
  .controls input[type="search"] { flex: 2; min-width: 8rem; }
  .controls select { flex: 1; min-width: 8rem; }

  /* Potion table */
  table.potions { width: 100%; border-collapse: collapse; margin-bottom: 1rem; }
  table.potions th {
    text-align: left; font-size: 0.85rem; text-transform: uppercase;
    letter-spacing: 0.03em; color: #b7a888; padding: 0.5rem 0.35rem;
    border-bottom: 2px solid #3a3226; cursor: pointer; user-select: none;
    white-space: nowrap;
  }
  table.potions th.sorted { color: #ffe9b0; }
  table.potions td {
    padding: 0.55rem 0.35rem; border-bottom: 1px solid #2a241a;
    vertical-align: top;
  }
  table.potions td.price {
    color: #ffd97a; font-weight: bold; white-space: nowrap;
    font-size: 1.05rem;
  }
  table.potions td.ings { font-size: 0.95rem; }
  table.potions td.ings .have { color: #8a9a86; font-size: 0.85rem; }
  table.potions td.n { text-align: center; color: #b7a888; }
  table.potions td.effects { font-size: 0.8rem; color: #9a8f76; }
  table.potions td.brew { text-align: right; white-space: nowrap; }
  .brewbtn {
    font-size: 1.2rem; line-height: 1; padding: 0.4rem 0.8rem;
    border-radius: 0.5rem; border: 1px solid #4a6b43;
    background: #2a3a24; color: #d8ffd0; cursor: pointer;
    min-width: 52px; min-height: 48px;
  }
  .brewbtn:active { background: #3a522f; }
  .empty { padding: 1.2rem; color: #8a9a86; text-align: center; }
  .more { padding: 0.8rem; color: #8a9a86; text-align: center; font-size: 0.9rem; }

  details.ings { margin-top: 1rem; }
  details.ings summary { cursor: pointer; font-size: 1.05rem; padding: 0.5rem 0; }
  .ing {
    display: flex; justify-content: space-between;
    padding: 0.35rem 0; border-bottom: 1px solid #2a241a; font-size: 0.95rem;
  }
  .ing .count { color: #b7a888; }
  .ing.depleted { color: #6b6355; text-decoration: line-through; }
  .ing.depleted .count { color: #6b6355; }
  .badge {
    display: inline-block; min-width: 1.6em; text-align: center;
    background: #3a3226; border-radius: 1em; padding: 0.1em 0.5em;
    font-size: 0.85em;
  }
  h2 { font-size: 1.1rem; margin: 0.8rem 0 0.2rem; }
  .hint { font-size: 0.85rem; color: #8a9a86; margin: 0.2rem 0 0.6rem; }
</style>
</head>
<body>
<header>
  <h1>&#9875; Skyrim Alchemy Companion</h1>
  <button id="refresh">&#8635; Refresh from latest save</button>
  <button id="shutdown" title="Stop the companion server">&#9199; Quit</button>
  <div id="status">Loading&hellip;</div>
</header>
<div id="error"></div>

<div id="queue" hidden>
  <h3>&#129514; Brew Queue <span class="badge" id="q-count">0</span>
    &nbsp;<span class="q-total" id="q-total"></span>
    <button class="qbtn" id="q-clear" style="float:right">Clear</button>
  </h3>
  <div id="q-list"></div>
</div>

<nav class="tabs">
  <button id="tabbtn-price" class="active">&#128176; Potions</button>
  <button id="tabbtn-disenchant">&#128142; Enchantments</button>
</nav>

<main>
  <section id="tab-price" class="tab active">
    <div class="threshold-bar">
      <label for="minval">Min value: <span id="minval-show">0</span>g</label>
      <input type="range" id="minval" min="0" max="1000" step="25" value="0">
    </div>
    <details class="alchemy-settings">
      <summary>⚗️ Alchemy skill & perks (affects potion values)</summary>
      <div id="alch-detected" class="hint" style="margin-bottom:0.5rem"></div>
      <div class="settings-grid">
        <label>Skill: <input type="number" id="alch-skill" min="15" max="100" value="15" style="width:4rem"></label>
        <label>Alchemist ranks: <input type="number" id="alch-ranks" min="0" max="5" value="0" style="width:3rem"></label>
        <label><input type="checkbox" id="perk-benefactor"> Benefactor</label>
        <label><input type="checkbox" id="perk-poisoner"> Poisoner</label>
        <label><input type="checkbox" id="perk-physician"> Physician</label>
        <button id="alch-apply">Apply</button>
        <button id="alch-reset-detected" title="Reset to values detected from save">Reset to detected</button>
      </div>
    </details>
    <div class="controls">
      <input id="search" type="search" placeholder="Filter: effect or ingredient&hellip;" autocomplete="off">
      <select id="ingfilter"><option value="">All ingredients</option></select>
    </div>
    <h2 id="potion-h">Potions you can brew</h2>
    <div class="hint">Tap a column header to sort. Tap <b>+</b> to queue a brew &mdash; ingredients are deducted and the list updates.</div>
    <div id="potions"></div>
    <details class="ings">
      <summary id="ing-h">Your ingredients</summary>
      <div id="ingredients"></div>
    </details>
  </section>

  <section id="tab-disenchant" class="tab">
    <h2>Known Enchantments</h2>
    <div class="hint">Enchantments you've learned via disenchanting. Auto-detected from save.</div>
    <div id="known-enchantments"></div>
  </section>
</main>

<script>
"use strict";
const $ = (id) => document.getElementById(id);
const ROW_CAP = 400;  // max potion rows rendered at once (keeps phones snappy)

// ---------------------------------------------------------------- state
const S = {
  data: null,        // last good server payload (the parsed "total" inventory)
  remaining: {},     // ingredient name -> count left after queued brews
  queue: [],         // [{key, price, ingredients, effects, qty}]
  tab: "price",
  sort: { key: "price", dir: -1 },        // price tab: price desc by default
  esort: { key: "price", dir: -1 },       // effect tab sort
  ingFilter: "",
  effectFilter: "",
  search: "",
  minValue: parseInt(localStorage.getItem("skyrim-minval") || "0", 10) || 0,
  // knownEffects is populated from the save file in resetState().
  // Maps ingredient name -> list of known effect indexes (0-3).
  knownEffects: {},
};

// Per-shop "not actually for sale" exclusions (player taps X on a buy).
function getExcluded(location) {
  const all = JSON.parse(localStorage.getItem("skyrim-shop-excluded") || "{}");
  return new Set(all[location] || []);
}
function setExcluded(location, set) {
  const all = JSON.parse(localStorage.getItem("skyrim-shop-excluded") || "{}");
  if (set.size) all[location] = [...set];
  else delete all[location];
  localStorage.setItem("skyrim-shop-excluded", JSON.stringify(all));
}
// Force-include: Player tapped + on the Don't-buy list (wants it anyway).
function getForced(location) {
  const all = JSON.parse(localStorage.getItem("skyrim-shop-forced") || "{}");
  return new Set(all[location] || []);
}
function setForced(location, set) {
  const all = JSON.parse(localStorage.getItem("skyrim-shop-forced") || "{}");
  if (set.size) all[location] = [...set];
  else delete all[location];
  localStorage.setItem("skyrim-shop-forced", JSON.stringify(all));
}

function isKnown(ingName, effect) {
  // S.knownEffects maps name -> list of known effect INDEXES (0-3) from the save.
  const knownIdx = S.knownEffects[ingName];
  if (!knownIdx) return false;
  const ingData = S.data.ingredients.find((g) => g.name === ingName);
  if (!ingData) return false;
  const idx = ingData.effects.indexOf(effect);
  return idx !== -1 && knownIdx.indexOf(idx) !== -1;
}

// How many unknown effects would brewing this potion reveal?
function discoveryValue(p) {
  let n = 0;
  for (const ing of p.ingredients) {
    const ingData = S.data.ingredients.find((g) => g.name === ing);
    if (!ingData) continue;
    for (const e of p.effects) {
      if (ingData.effects.indexOf(e) !== -1 && !isKnown(ing, e)) n++;
    }
  }
  return n;
}

function showError(msg) {
  const el = $("error");
  if (msg) { el.textContent = msg; el.style.display = "block"; }
  else { el.style.display = "none"; }
}

// ------------------------------------------------------------ queue logic
function brewable(p) {
  for (const nm of p.ingredients) {
    if ((S.remaining[nm] || 0) < 1) return false;
  }
  return true;
}

function batchesLeft(p) {
  let m = Infinity;
  for (const nm of p.ingredients) m = Math.min(m, S.remaining[nm] || 0);
  return m;
}

function potionKey(p) {
  return p.ingredients.join("\x01") + "\x02" + p.effects.join("\x01");
}

function brew(p) {
  if (!brewable(p)) return;
  for (const nm of p.ingredients) S.remaining[nm]--;
  const k = potionKey(p);
  const q = S.queue.find((e) => e.key === k);
  if (q) q.qty++;
  else S.queue.push({ key: k, price: p.price, ingredients: p.ingredients.slice(),
                      effects: p.effects.slice(), qty: 1 });
  renderAll();
}

function unbrew(i) {
  const q = S.queue[i];
  for (const nm of q.ingredients) S.remaining[nm]++;
  q.qty--;
  if (q.qty <= 0) S.queue.splice(i, 1);
  renderAll();
}

function clearQueue() {
  for (const q of S.queue)
    for (let n = 0; n < q.qty; n++)
      for (const nm of q.ingredients) S.remaining[nm]++;
  S.queue = [];
  renderAll();
}

// ---------------------------------------------------------------- sorting
function sortPotions(list, st) {
  const dir = st.dir;
  const key = st.key;
  const val = (p) => {
    if (key === "price") return p.price;
    if (key === "n") return p.n;
    if (key === "ingredients") return p.ingredients.join(" ").toLowerCase();
    if (key === "effects") return p.effects.join(" ").toLowerCase();
    return 0;
  };
  list.sort((a, b) => {
    const va = val(a), vb = val(b);
    if (va < vb) return -dir;
    if (va > vb) return dir;
    // Tie-break: richer first, then fewer ingredients, then name.
    if (a.price !== b.price) return b.price - a.price;
    if (a.n !== b.n) return a.n - b.n;
    return a.ingredients.join(" ").localeCompare(b.ingredients.join(" "));
  });
  return list;
}

function sortArrow(col, st) {
  return st.key === col ? (st.dir === 1 ? " ▲" : " ▼") : "";
}

// ---------------------------------------------------------------- tables
const COLS = [
  ["price", "Price"],
  ["ingredients", "Ingredients"],
  ["n", "#"],
  ["effects", "Effects"]
];

function potionTable(potions, st, onSort, showCap) {
  const wrap = document.createElement("div");
  if (!potions.length) {
    wrap.innerHTML = "<div class='empty'>Nothing brewable matches. " +
      "Brew or clear the queue, or change the filter.</div>";
    return wrap;
  }
  const table = document.createElement("table");
  table.className = "potions";
  const thead = document.createElement("thead");
  const hr = document.createElement("tr");
  for (const [col, label] of COLS) {
    const th = document.createElement("th");
    th.textContent = label + sortArrow(col, st);
    if (st.key === col) th.className = "sorted";
    th.addEventListener("click", () => onSort(col));
    hr.appendChild(th);
  }
  const thb = document.createElement("th");  // brew-button column
  hr.appendChild(thb);
  thead.appendChild(hr);
  table.appendChild(thead);

  const tb = document.createElement("tbody");
  const shown = showCap ? potions.slice(0, ROW_CAP) : potions;
  for (const p of shown) {
    const tr = document.createElement("tr");
    const tdP = document.createElement("td");
    tdP.className = "price";
    tdP.textContent = p.price;
    tr.appendChild(tdP);

    const tdI = document.createElement("td");
    tdI.className = "ings";
    // Highlight ingredients that would reveal a new effect (2026-10-05):
    // if brewing this potion would discover an unknown effect for a specific
    // ingredient, give that ingredient a different background. Do NOT color
    // the effect text itself.
    tdI.innerHTML = p.ingredients.map((nm) => {
      const ingData = S.data.ingredients.find((g) => g.name === nm);
      let wouldDiscover = false;
      if (ingData) {
        for (const eff of p.effects) {
          if (ingData.effects.indexOf(eff) !== -1 && !isKnown(nm, eff)) {
            wouldDiscover = true;
            break;
          }
        }
      }
      const cls = wouldDiscover ? "ing-discovery" : "";
      return "<span class='" + cls + "'>" + escapeHtml(nm) + "</span>" +
        " <span class='have'>(" + (S.remaining[nm] || 0) + " left)</span>";
    }).join("<br>");
    tr.appendChild(tdI);

    const tdN = document.createElement("td");
    tdN.className = "n";
    tdN.textContent = p.n;
    tr.appendChild(tdN);

    const tdE = document.createElement("td");
    tdE.className = "effects";
    // Effect text is NOT colored (2026-10-05). The ingredient highlight
    // above shows what would be discovered.
    tdE.innerHTML = p.effects.map((eff) => escapeHtml(eff)).join(" + ");
    tr.appendChild(tdE);

    const tdB = document.createElement("td");
    tdB.className = "brew";
    const bb = document.createElement("button");
    bb.className = "brewbtn";
    bb.textContent = "+";
    bb.title = "Queue this brew (" + batchesLeft(p) + "x available)";
    bb.setAttribute("aria-label", "Brew " + p.ingredients.join(", "));
    bb.addEventListener("click", () => brew(p));
    tdB.appendChild(bb);
    tr.appendChild(tdB);
    tb.appendChild(tr);
  }
  table.appendChild(tb);
  wrap.appendChild(table);
  if (showCap && potions.length > ROW_CAP) {
    const m = document.createElement("div");
    m.className = "more";
    m.textContent = "…and " + (potions.length - ROW_CAP) +
      " more. Use the filter to narrow it down.";
    wrap.appendChild(m);
  }
  return wrap;
}

function escapeHtml(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;")
          .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

// ---------------------------------------------------------------- render
function filteredPricePotions() {
  const q = S.search.trim().toLowerCase();
  let list = S.data.potions.filter(brewable).filter((p) => p.price >= S.minValue);
  if (S.ingFilter)
    list = list.filter((p) => p.ingredients.indexOf(S.ingFilter) !== -1);
  if (q)
    list = list.filter((p) =>
      (p.effects.join(" ") + " " + p.ingredients.join(" "))
        .toLowerCase().indexOf(q) !== -1);
  return sortPotions(list, S.sort);
}


function onSortPrice(col) {
  if (S.sort.key === col) S.sort.dir = -S.sort.dir;
  else S.sort = { key: col, dir: col === "price" ? -1 : 1 };
  renderPriceTab();
}


function renderPriceTab() {
  const list = filteredPricePotions();
  const el = $("potions");
  el.innerHTML = "";

  // "One ingredient away" section at the top (2026-10-05):
  // If you had X, it would combine with Y for a Zg potion.
  const oneAway = (S.data.shop && S.data.shop.plan && S.data.shop.plan.buys) || [];
  if (oneAway.length) {
    const hdr = document.createElement("h3");
    hdr.textContent = "One ingredient away — consider picking up:";
    hdr.style.marginTop = "0";
    el.appendChild(hdr);
    const ul = document.createElement("ul");
    ul.style.listStyle = "none";
    ul.style.paddingLeft = "0";
    oneAway.slice(0, 5).forEach((b) => {
      const li = document.createElement("li");
      li.style.marginBottom = "8px";
      li.innerHTML = "If you had <b>" + escapeHtml(b.name) + "</b> (" + b.cost + "g), " +
        "it would combine with <b>" + escapeHtml(b.pairs_with || "?") + "</b> " +
        "for a <b>" + b.potion_price + "g</b> " +
        escapeHtml((b.potion_effects || []).join(" + ")) + " potion.";
      ul.appendChild(li);
    });
    el.appendChild(ul);
    const hr = document.createElement("hr");
    el.appendChild(hr);
  }

  $("potion-h").innerHTML = "Potions you can brew " +
    "<span class='badge'>" + list.length + "</span>";
  el.appendChild(potionTable(list, S.sort, onSortPrice, true));
}


function renderQueue() {
  const box = $("queue");
  const n = S.queue.reduce((a, q) => a + q.qty, 0);
  const total = S.queue.reduce((a, q) => a + q.qty * q.price, 0);
  if (!n) { box.hidden = true; return; }
  box.hidden = false;
  $("q-count").textContent = n;
  $("q-total").textContent = "Total value: " + total + " gold";
  const el = $("q-list");
  el.innerHTML = "";
  S.queue.forEach((q, i) => {
    const row = document.createElement("div");
    row.className = "q-row";
    const info = document.createElement("div");
    info.className = "q-info";
    info.innerHTML = "<b>" + q.qty + "×</b> " +
      q.ingredients.map(escapeHtml).join(" + ") +
      "<br><span style='font-size:0.8rem;color:#8a9a86'>" +
      q.effects.map(escapeHtml).join(" + ") + "</span>";
    const pr = document.createElement("div");
    pr.className = "q-price";
    pr.textContent = (q.qty * q.price) + " g";
    const rm = document.createElement("button");
    rm.className = "qbtn";
    rm.textContent = "−";
    rm.title = "Remove one (restores ingredients)";
    rm.setAttribute("aria-label", "Remove one from queue");
    rm.addEventListener("click", () => unbrew(i));
    row.appendChild(info); row.appendChild(pr); row.appendChild(rm);
    el.appendChild(row);
  });
}

// ---------------------------------------------------------------- disenchant tab (2026-10-05)
// Shows known enchantments auto-detected from save (Type-48 ChangeForms).
function renderDisenchant() {
  const enchEl = $("known-enchantments");
  const knownEnchs = S.data.known_enchantments || [];
  if (knownEnchs.length) {
    enchEl.innerHTML = "<div class='hint'>You know " + knownEnchs.length +
      " enchantments:</div>" +
      knownEnchs.map((e) => "<div class='disenchant-row'><div><b>" +
        escapeHtml(e.name) + "</b> <span class='have'>" +
        escapeHtml(e.formid) + "</span></div></div>").join("");
  } else {
    enchEl.innerHTML = "<div class='empty'>No known enchantments detected.</div>";
  }
}

function renderIngredients() {
  $("ing-h").textContent =
    "Your ingredients (" + S.data.ingredients.length + ") — tap effects to mark known";
  const el = $("ingredients");
  el.innerHTML = "";
  for (const g of S.data.ingredients) {
    const left = S.remaining[g.name] || 0;
    const row = document.createElement("div");
    row.className = "ing" + (left <= 0 ? " depleted" : "");
    const n = document.createElement("span");
    n.textContent = g.name;
    const c = document.createElement("span");
    c.className = "count";
    c.textContent = "x" + left + " left";
    row.appendChild(n); row.appendChild(c);
    // Effect pills: show known status from the save (read-only).
    const effs = document.createElement("div");
    effs.className = "effpills";
    for (const e of g.effects) {
      const pill = document.createElement("span");
      pill.className = "effpill" + (isKnown(g.name, e) ? " known" : "");
      pill.textContent = e;
      effs.appendChild(pill);
    }
    row.appendChild(effs);
    el.appendChild(row);
  }
}

function renderStatus() {
  const d = S.data;
  $("status").textContent =
    d.save_name + "  |  " + d.save_time +
    "  |  " + d.ingredients.length + " ingredients" +
    "  |  " + d.potions.length + " recipes";
}

function fillIngredientFilter() {
  const sel = $("ingfilter");
  const cur = S.ingFilter;
  // Keep the "All ingredients" option, rebuild the rest.
  sel.innerHTML = "<option value=''>All ingredients</option>";
  for (const g of S.data.ingredients) {
    const o = document.createElement("option");
    o.value = g.name; o.textContent = g.name + " (" + (S.remaining[g.name] || 0) + ")";
    sel.appendChild(o);
  }
  sel.value = cur;
}

function renderAll() {
  renderStatus();
  renderQueue();
  fillIngredientFilter();
  renderPriceTab();
  renderIngredients();
}

function setTab(which) {
  S.tab = which;
  ["price", "disenchant"].forEach((t) => {
    $("tabbtn-" + t).className = which === t ? "active" : "";
    $("tab-" + t).className = "tab" + (which === t ? " active" : "");
  });
  if (which === "disenchant") renderDisenchant();
}

// ---------------------------------------------------------------- data
function resetState(data) {
  S.data = data;
  S.remaining = {};
  for (const g of data.ingredients) S.remaining[g.name] = g.count;
  S.queue = [];
  S.sort = { key: "price", dir: -1 };
  S.ingFilter = "";
  S.search = "";
  $("search").value = "";
  // Known effects come from the save file (authoritative).
  // Maps ingredient name -> list of known effect indexes (0-3).
  S.knownEffects = data.known_effects || {};
  // Keep a valid effect selection if possible.
  if (data.potions.length) {
    const top = data.potions.slice().sort((a, b) => b.price - a.price)[0];
  }
}

// ---------------------------------------------------------------- alchemy settings (2026-10-05)
// Skill and perks affect potion values. Stored in localStorage, sent to
// backend via query params so prices are calculated with your stats.
function getAlchemySettings() {
  return {
    skill: parseInt(localStorage.getItem("skyrim-alch-skill") || "15", 10) || 15,
    ranks: parseInt(localStorage.getItem("skyrim-alch-ranks") || "0", 10) || 0,
    benefactor: localStorage.getItem("skyrim-alch-benefactor") === "1",
    poisoner: localStorage.getItem("skyrim-alch-poisoner") === "1",
    physician: localStorage.getItem("skyrim-alch-physician") === "1",
  };
}
function saveAlchemySettings(s) {
  localStorage.setItem("skyrim-alch-skill", String(s.skill));
  localStorage.setItem("skyrim-alch-ranks", String(s.ranks));
  localStorage.setItem("skyrim-alch-benefactor", s.benefactor ? "1" : "0");
  localStorage.setItem("skyrim-alch-poisoner", s.poisoner ? "1" : "0");
  localStorage.setItem("skyrim-alch-physician", s.physician ? "1" : "0");
}
function initAlchemySettingsUI() {
  const s = getAlchemySettings();
  $("alch-skill").value = s.skill;
  $("alch-ranks").value = s.ranks;
  $("perk-benefactor").checked = s.benefactor;
  $("perk-poisoner").checked = s.poisoner;
  $("perk-physician").checked = s.physician;
  // Show detected values from save (2026-10-05: auto-detect).
  updateDetectedDisplay();
}

function updateDetectedDisplay() {
  const el = $("alch-detected");
  const d = (S.data && S.data.detected) || {};
  const parts = [];
  if (d.base_skill != null) {
    parts.push("Base skill: " + d.base_skill + " (racial incl., level-ups not in save)");
  }
  const perks = [];
  if (d.alchemist_ranks) perks.push("Alchemist " + d.alchemist_ranks);
  if (d.benefactor) perks.push("Benefactor");
  if (d.poisoner) perks.push("Poisoner");
  if (d.physician) perks.push("Physician");
  if (d.purity) perks.push("Purity");
  if (perks.length) {
    parts.push("Detected perks: " + perks.join(", "));
  } else if (d.base_skill != null) {
    parts.push("No alchemy perks detected");
  }
  el.textContent = parts.length ? "Detected from save: " + parts.join(" | ") : "";
}

function resetToDetected() {
  const d = (S.data && S.data.detected) || {};
  if (d.base_skill != null) {
    $("alch-skill").value = d.base_skill;
  }
  $("alch-ranks").value = d.alchemist_ranks || 0;
  $("perk-benefactor").checked = !!d.benefactor;
  $("perk-poisoner").checked = !!d.poisoner;
  $("perk-physician").checked = !!d.physician;
  // Save and reload
  const s = {
    skill: parseInt($("alch-skill").value, 10) || 15,
    ranks: parseInt($("alch-ranks").value, 10) || 0,
    benefactor: $("perk-benefactor").checked,
    poisoner: $("perk-poisoner").checked,
    physician: $("perk-physician").checked,
  };
  saveAlchemySettings(s);
  load();
}

async function load() {
  const btn = $("refresh");
  btn.disabled = true;
  btn.textContent = "Reading save…";
  // Send this shop's exclusions and force-includes (from the last known location).
  let url = "/api/data";
  const params = [];
  const lastLoc = localStorage.getItem("skyrim-shop-loc");
  if (lastLoc) {
    const excl = getExcluded(lastLoc);
    const forced = getForced(lastLoc);
    if (excl.size) params.push("exclude=" + encodeURIComponent([...excl].join("\\n")));
    if (forced.size) params.push("force=" + encodeURIComponent([...forced].join("\\n")));
    params.push("shop=" + encodeURIComponent(lastLoc));
  }
  // Alchemy skill/perks for dynamic potion values.
  const alch = getAlchemySettings();
  params.push("alch_skill=" + alch.skill);
  params.push("alch_ranks=" + alch.ranks);
  params.push("alch_benefactor=" + (alch.benefactor ? "1" : "0"));
  params.push("alch_poisoner=" + (alch.poisoner ? "1" : "0"));
  params.push("alch_physician=" + (alch.physician ? "1" : "0"));
  if (params.length) url += "?" + params.join("&");
  try {
    const r = await fetch(url, { cache: "no-store" });
    const data = await r.json();
    if (!data.ok) { showError(data.error || "Unknown error"); }
    else {
      showError(null);
      resetState(data); renderAll();
    }
  } catch (e) {
    showError("Could not reach the companion server: " + e);
  }
  btn.disabled = false;
  btn.innerHTML = "&#8635; Refresh from latest save";
}

$("refresh").addEventListener("click", load);
$("shutdown").addEventListener("click", async () => {
  if (!confirm("Stop the Alchemy Companion server?")) return;
  try {
    await fetch("/api/shutdown");
  } catch (e) {
    // Server is shutting down; fetch will fail. That's expected.
  }
  document.body.innerHTML = "<div style='padding:2rem;text-align:center'>" +
    "<h2>Companion stopped.</h2>" +
    "<p>You can close this tab.</p></div>";
});
$("search").addEventListener("input", () => {
  S.search = $("search").value;
  renderPriceTab();  // cheap: re-render from the last good payload
});
$("ingfilter").addEventListener("change", () => {
  S.ingFilter = $("ingfilter").value;
  renderPriceTab();
});
$("tabbtn-price").addEventListener("click", () => setTab("price"));
$("tabbtn-disenchant").addEventListener("click", () => setTab("disenchant"));
$("q-clear").addEventListener("click", clearQueue);
// Alchemy settings: init from localStorage, Apply saves and reloads.
initAlchemySettingsUI();
$("alch-apply").addEventListener("click", () => {
  const s = {
    skill: parseInt($("alch-skill").value, 10) || 15,
    ranks: parseInt($("alch-ranks").value, 10) || 0,
    benefactor: $("perk-benefactor").checked,
    poisoner: $("perk-poisoner").checked,
    physician: $("perk-physician").checked,
  };
  // Clamp.
  s.skill = Math.max(15, Math.min(100, s.skill));
  s.ranks = Math.max(0, Math.min(5, s.ranks));
  saveAlchemySettings(s);
  initAlchemySettingsUI();  // reflect clamped values
  load();  // reload with new stats
});
$("alch-reset-detected").addEventListener("click", resetToDetected);
// Min-value slider: persistent via localStorage.
$("minval").value = S.minValue;
$("minval-show").textContent = S.minValue;
$("minval").addEventListener("input", () => {
  S.minValue = parseInt($("minval").value, 10) || 0;
  $("minval-show").textContent = S.minValue;
  localStorage.setItem("skyrim-minval", String(S.minValue));
  renderAll();
});
load();
</script>
</body>
</html>
"""


class CompanionState:
    """Holds config + last-good payload for the HTTP handlers."""

    def __init__(self, saves_dir: str, db_path: str):
        self.saves_dir = saves_dir
        self.lookup, self.ingredients = load_ingredient_db(db_path)
        self.ing_db = {ing["name"]: ing for ing in self.ingredients}
        # Enchantment name mapping (FormID -> name) for auto-detected
        # known enchantments (2026-10-05).
        self.ench_names = {}
        try:
            names_path = os.path.join(os.path.dirname(db_path),
                                      "enchantment_names.json")
            with open(names_path, "r", encoding="utf-8") as f:
                names_data = json.load(f)
            self.ench_names = names_data.get("enchantments", {})
        except (OSError, ValueError):
            pass
        # ESM mappings (FormID -> EDID) as fallback for enchantment names.
        # (2026-10-05: not all enchantments have manual names.)
        self.esm_ench = {}
        try:
            esm_path = os.path.join(os.path.dirname(db_path),
                                    "esm_mappings.json")
            with open(esm_path, "r", encoding="utf-8") as f:
                esm_data = json.load(f)
            for fid_str, info in esm_data.items():
                if info.get("type") == "ENCH" and info.get("edid"):
                    # Normalize: "0x%08X" format (lowercase 0x, uppercase digits)
                    norm = fid_str.upper().replace("0X", "0x")
                    self.esm_ench[norm] = info["edid"]
        except (OSError, ValueError):
            pass
        self.lock = threading.Lock()
        self.last_good = None

    def _ench_name_from_edid(self, edid: str) -> str:
        """Convert ESM EDID to readable name.

        e.g. 'EnchArmorFortifyAlchemyBase' -> 'Fortify Alchemy'
             'EnchWeaponFrostDamageBase' -> 'Frost Damage'
        """
        # Remove prefix
        name = edid
        for prefix in ("EnchArmor", "EnchWeapon", "Ench"):
            if name.startswith(prefix):
                name = name[len(prefix):]
                break
        # Remove suffixes
        for suffix in ("Base", "01", "02", "03", "04", "05"):
            if name.endswith(suffix):
                name = name[:-len(suffix)]
        # Split CamelCase: FortifyAlchemy -> Fortify Alchemy
        import re
        name = re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', name)
        name = re.sub(r'(?<=[A-Z])(?=[A-Z][a-z])', ' ', name)
        return name.strip() or edid

    def _get_detected_alchemy(self, save_path: str) -> dict:
        """Auto-detect alchemy skill base and perks from save.

        Returns {base_skill: int|None, alchemist_ranks: int,
                 benefactor: bool, poisoner: bool, physician: bool,
                 purity: bool}.
        Base skill is from NPC_ DNAM (includes racial, excludes level-ups).
        Perks are from ACHR RefID search. Failures return defaults.
        (2026-10-05: auto-detect with manual override.)
        """
        result = {
            "base_skill": None,
            "alchemist_ranks": 0,
            "benefactor": False,
            "poisoner": False,
            "physician": False,
            "purity": False,
        }
        try:
            base = extract_base_alchemy(save_path)
            if base is not None:
                result["base_skill"] = base
        except Exception:
            pass
        try:
            perks = extract_perks(save_path)
            result.update(perks)
        except Exception:
            pass
        return result

    def read_current(self, exclude=frozenset(), exclude_shop="",
                     force=frozenset(), force_shop="",
                     alch_skill=15, alch_ranks=0,
                     alch_benefactor=True, alch_poisoner=True,
                     alch_physician=True) -> dict:
        """Parse the newest save; return the /api/data payload dict."""
        save_path = pick_save(self.saves_dir)
        # Store alchemy stats for potion pricing.
        self.alch_skill = alch_skill
        self.alch_ranks = alch_ranks
        self.alch_benefactor = alch_benefactor
        self.alch_poisoner = alch_poisoner
        self.alch_physician = alch_physician
        try:
            raw_items, info, plugins, light_plugins = \
                extract_player_inventory(save_path)
        except (SaveParseError, ValueError) as e:
            return {"ok": False,
                    "error": "Save parse failed: %s" % e}
        except OSError as e:
            return {"ok": False,
                    "error": "Could not read save file: %s" % e}
        # Discovered alchemy effects from the save (2026-10-05 request).
        # Maps ingredient name -> list of known effect indexes (0-3).
        known_effects = {}
        try:
            known_raw = extract_known_ingredients(save_path)
            for fid, bitmask in known_raw.items():
                plugin, obj_id = split_formid(fid, plugins, light_plugins)
                if plugin is None:
                    continue
                ing = self.lookup.get((plugin.lower(), obj_id))
                if ing is None:
                    continue
                name = ing["name"]
                known_effects[name] = [j for j in range(4)
                                       if bitmask & (1 << j)]
        except Exception:
            pass  # known effects are optional; don't break the page
        # Known spells and enchantments from save (2026-10-05).
        # Type 13 = spells, Type 48 = enchantments. Flags on base Form.
        known_spells = []
        known_enchantments = []
        try:
            known_spells = ["0x%08X" % fid
                            for fid in extract_known_spells(save_path)]
        except Exception:
            pass
        try:
            for fid in extract_known_enchantments(save_path):
                fid_str = "0x%08X" % fid
                # Priority: manual names > ESM EDID > FormID
                # (2026-10-05: fill in missing names from ESM)
                name = self.ench_names.get(fid_str)
                if not name:
                    edid = self.esm_ench.get(fid_str)
                    if edid:
                        name = self._ench_name_from_edid(edid)
                    else:
                        name = fid_str
                known_enchantments.append({
                    "formid": fid_str,
                    "name": name,
                })
        except Exception:
            pass
        owned = inventory_to_ingredients(raw_items, plugins, light_plugins,
                                         self.lookup)
        # Effect-grouping approach (2026-10-05): O(E*I^2) not O(n^3).
        # No pre-computed table needed.
        potions = brewable_by_effect(
            owned, skill=self.alch_skill, alchemist_ranks=self.alch_ranks,
            benefactor=self.alch_benefactor, poisoner=self.alch_poisoner,
            physician=self.alch_physician)
        # Shopping list via effect-grouping (2026-10-05 approach).
        # No merchant detection, no pre-computed table, no O(n^3).
        shop = None
        gold = next((c for f, c in raw_items if f == 0xF), 0)
        recs = shopping_by_effect(owned, self.ing_db, gold,
                                  exclude=exclude, force_include=force)
        # Baseline: quick greedy estimate of inventory-only brew value.
        # (Don't use brew_allocation here — its 400 randomized iterations
        # over 4,916 table potions hangs. This is just for the shop's
        # "nets Xg" display, not the actual brew plan.)
        _counts = {o["name"]: o["count"] for o in owned}
        baseline = 0
        for p in potions:
            if p["price"] < 100:
                break
            ings = p["ingredients"]
            if all(_counts.get(i, 0) > 0 for i in ings):
                for i in ings:
                    _counts[i] -= 1
                baseline += p["price"]
        shop = {"gold": gold,
                "baseline_value": baseline,
                "baseline_sell": int(baseline * SELL_FACTOR),
                "plan": recs}
        payload = {
            "ok": True,
            "save_name": os.path.basename(save_path),
            "save_time": time.strftime("%Y-%m-%d %H:%M",
                                        time.localtime(
                                            os.path.getmtime(save_path))),
            "player": info.get("playerName", "?"),
            "ingredients": owned,
            "potions": potions,
            "shop": shop,
            "known_effects": known_effects,
            "known_spells": known_spells,
            "known_enchantments": known_enchantments,
            "detected": self._get_detected_alchemy(save_path),
        }
        with self.lock:
            self.last_good = payload
        return payload


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "SkyrimAlchemyCompanion/1.0"

    def log_message(self, fmt, *args):  # quieter logging
        sys.stderr.write("[http] " + fmt % args + "\n")

    def _send(self, body: bytes, ctype: str):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        state: CompanionState = self.server.state  # type: ignore
        if self.path == "/":
            self._send(PAGE_HTML.encode("utf-8"),
                       "text/html; charset=utf-8")
        elif self.path == "/api/data" or self.path.startswith("/api/data?"):
            qs = urllib.parse.urlparse(self.path).query
            args = urllib.parse.parse_qs(qs)
            excl_raw = args.get("exclude", [""])[0]
            for_shop = args.get("shop", [""])[0]
            exclude = frozenset(e for e in excl_raw.split("\n") if e)
            force_raw = args.get("force", [""])[0]
            force = frozenset(e for e in force_raw.split("\n") if e)
            # Alchemy skill/perks for dynamic potion values (2026-10-05).
            try:
                alch_skill = int(args.get("alch_skill", ["15"])[0])
            except ValueError:
                alch_skill = 15
            try:
                alch_ranks = int(args.get("alch_ranks", ["0"])[0])
            except ValueError:
                alch_ranks = 0
            alch_benefactor = args.get("alch_benefactor", ["0"])[0] == "1"
            alch_poisoner = args.get("alch_poisoner", ["0"])[0] == "1"
            alch_physician = args.get("alch_physician", ["0"])[0] == "1"
            payload = state.read_current(
                exclude=exclude, exclude_shop=for_shop,
                force=force, force_shop=for_shop,
                alch_skill=alch_skill, alch_ranks=alch_ranks,
                alch_benefactor=alch_benefactor,
                alch_poisoner=alch_poisoner,
                alch_physician=alch_physician)
            body = json.dumps(payload).encode("utf-8")
            self._send(body, "application/json")
        elif self.path == "/api/shutdown":
            # Graceful shutdown for manually-run instances.
            # (Not used when running as a service.)
            self._send(b'{"ok": true}', "application/json")
            import threading
            threading.Thread(target=self.server.shutdown, daemon=True).start()
        else:
            self.send_error(404, "not found")


def lan_ip() -> str:
    """Best-guess LAN address for the phone URL (no traffic is sent)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=APP_NAME)
    ap.add_argument("--saves-dir",
                    help="override the auto-detected saves folder")
    ap.add_argument("--save",
                    help="parse one specific .ess file (with --once)")
    ap.add_argument("--db", default=None,
                    help="path to ingredients.json "
                         "(default: next to this script)")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--once", action="store_true",
                    help="parse the newest save, print JSON, exit (no server)")
    args = ap.parse_args(argv)

    script_dir = os.path.dirname(os.path.abspath(__file__))
    db_path = args.db or os.path.join(script_dir, "ingredients.json")
    if not os.path.isfile(db_path):
        sys.stderr.write("ingredients.json not found at %s\n" % db_path)
        return 2

    if args.once:
        lookup, _ = load_ingredient_db(db_path)
        path = args.save or pick_save(find_saves_dir(args.saves_dir))
        raw_items, info, plugins, light_plugins = \
            extract_player_inventory(path)
        owned = inventory_to_ingredients(raw_items, plugins, light_plugins,
                                         lookup)
        print(json.dumps({"save": os.path.basename(path),
                          "ingredients": owned,
                          "potions": compute_potions(owned)}, indent=2))
        return 0

    saves_dir = find_saves_dir(args.saves_dir)
    state = CompanionState(saves_dir, db_path)

    server = http.server.ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    server.state = state  # type: ignore

    print("=" * 60)
    print(APP_NAME)
    print("Saves folder: %s" % saves_dir)
    print("Ingredients DB: %d entries" % len(state.ingredients))
    print()
    print("On your phone (same WiFi), open:")
    print("    http://%s:%d/" % (lan_ip(), args.port))
    print()
    print("Quicksave with F5 in-game, then tap Refresh on the page.")
    print("Press Ctrl+C to stop.")
    print("=" * 60)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
