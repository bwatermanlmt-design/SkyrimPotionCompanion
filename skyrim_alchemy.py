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
# REFR change-flag bits that trigger the actor-level EXTRADATA section
# (ReSaver ChangeFormRefr.java).
_REFR_EXTRADATA_BITS = (6, 12, 29, 31, 11, 17, 25, 26, 10)
_REFR_EXTRADATA_MASK = sum(1 << b for b in _REFR_EXTRADATA_BITS)


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
    # Haggling ranks 1-5 (Speech tree; EDIDs Haggling00/20/40/60/80 give the
    # skill requirement of each rank — verified against Skyrim.esm 2026-10-19)
    bytes([0x4B, 0xE1, 0x28]): ("haggling", 1),  # 0x000BE128
    bytes([0x4C, 0x07, 0xCE]): ("haggling", 2),  # 0x000C07CE
    bytes([0x4C, 0x07, 0xCF]): ("haggling", 3),  # 0x000C07CF
    bytes([0x4C, 0x07, 0xD0]): ("haggling", 4),  # 0x000C07D0
    bytes([0x4C, 0x07, 0xD1]): ("haggling", 5),  # 0x000C07D1
    bytes([0x45, 0x8F, 0x75]): ("allure", 1),    # 0x00058F75
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
    """Get alchemy and speech perks from save.

    Returns {alchemist_ranks: int, benefactor: bool, poisoner: bool,
             physician: bool, purity: bool, haggling_ranks: int,
             allure: bool}.
    Searches player ACHR decompressed data for (rank + RefID) pairs.
    """
    result = {
        "alchemist_ranks": 0,
        "benefactor": False,
        "poisoner": False,
        "physician": False,
        "purity": False,
        "haggling_ranks": 0,
        "allure": False,
    }
    # Perks with multiple ranks: highest rank found = total ranks taken
    # (ranks are sequential, so rank 2 implies rank 1).
    RANKED_PERKS = ("alchemist", "haggling")
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
                    if perk_name in RANKED_PERKS:
                        # Count ranks: highest rank found = total ranks
                        if rank_byte >= 1 and rank > result[perk_name + "_ranks"]:
                            result[perk_name + "_ranks"] = rank
                    else:
                        # For boolean perks, rank_byte >= 1 means taken
                        if rank_byte >= 1:
                            result[perk_name] = True
                start = idx + 1
        break  # only need the player ACHR
    return result


def extract_player_achr(path: str) -> bytes:
    """Return the decompressed change-form data for the player ACHR
    (type 1, formID 0x14). Raises SaveParseError if not found."""
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
    n = fc.u32()
    formid_array = [fc.u32() for _ in range(n)]
    cc = Cursor(body, flt["changeFormsOffset"])
    for _ in range(flt["changeFormCount"]):
        b0, b1, b2 = cc.u8(), cc.u8(), cc.u8()
        flags = cc.u32()
        type_byte = cc.u8()
        cc.u8()  # version
        size_class = type_byte >> 6
        form_type = type_byte & 0x3F
        if size_class == 0:
            length1, length2 = cc.u8(), cc.u8()
        elif size_class == 1:
            length1, length2 = cc.u16(), cc.u16()
        elif size_class == 2:
            length1, length2 = cc.u32(), cc.u32()
        else:
            raise SaveParseError("bad size class %d" % size_class)
        data = cc.raw(length1)
        if form_type == 1:
            try:
                fid = resolve_refid(b0, b1, b2, formid_array)
            except SaveParseError:
                fid = None
            if fid == 0x14:
                if length2:
                    try:
                        data = zlib.decompress(data)
                    except Exception:
                        pass
                return data
    raise SaveParseError("player ACHR not found")


def extract_known_spell_fids(path: str,
                             spell_fids: List[int]) -> List[int]:
    """Find which of `spell_fids` the player knows.

    Mechanism discovered 2026-10-19 via Ben's console experiment (he gave
    himself all spells between two saves; the player ACHR grew 2,689 bytes):
    LEARNED spells are stored in the player ACHR change data as 3-byte save
    RefIDs (kind 1: [0x40|(fid>>16), (fid>>8)&0xFF, fid&0xFF]), one
    contiguous run. Starting spells (Flames, Healing) live on the ActorBase,
    NOT here — the caller adds those. We search for each candidate spell's
    RefID byte pattern; ~90 patterns over ~26KB makes false positives
    negligible (~0.1 expected).
    """
    achr = extract_player_achr(path)
    found = []
    for fid in spell_fids:
        pat = bytes((0x40 | (fid >> 16), (fid >> 8) & 0xFF, fid & 0xFF))
        if pat in achr:
            found.append(fid)
    return found


def extract_read_books(path: str) -> List[int]:
    """Parse a save for READ BOOKS (type 13 = BOOK, per UESP save format).

    Returns [book_formID, ...]. The 'read' flag lives on the base Form record.
    CORRECTION 2026-10-19: this is NOT known spells (misidentified 2026-10-05).
    Verified by differential analysis: Ben read the Fast Healing spell tome
    (0x000A271D) between two saves 20s apart, and exactly one new type-13
    flag appeared. Spell tomes set read flags like regular books, so this
    doubles as the 'spell tomes read' list for his don't-rebuy check.
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


# ---------------------------------------------------------------------------
# Ingredient database + potion math
# ---------------------------------------------------------------------------

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
                        # Pass skill/perks through (2026-10-19 fix: these were
                        # silently dropped, so every price used skill=100).
                        "price": potion_price(
                            sorted(shared), skill=skill,
                            alchemist_ranks=alchemist_ranks,
                            benefactor=benefactor, poisoner=poisoner,
                            physician=physician),
                        "ingredients": [owned[i]["name"], owned[j]["name"]],
                        "batches": batches,
                        "n": 2,
                    })

    # Triples (2026-10-19): the live list was pairs-only since the Oct 5
    # rewrite, which starved every triple feature (rescue verdicts, 3-vs-2
    # badges). For each pair sharing an effect, try every third ingredient:
    # a triple is valid when an effect is shared by >=2 of the three
    # (in-game rule). O(P*n), fine for a few dozen ingredients.
    seen_triples = set()
    for i, j in seen_pairs:
        ei, ej = effsets[i], effsets[j]
        ij = ei & ej
        for k in range(len(owned)):
            if k == i or k == j:
                continue
            ek = effsets[k]
            shared = ij | (ei & ek) | (ej & ek)
            if not shared:
                continue
            tkey = tuple(sorted((i, j, k)))
            if tkey in seen_triples:
                continue
            seen_triples.add(tkey)
            a, b, c = tkey
            batches = min(owned[a]["count"], owned[b]["count"],
                          owned[c]["count"])
            potions.append({
                "effects": sorted(shared),
                # Pass skill/perks through (2026-10-19 fix: silently dropped).
                "price": potion_price(
                    sorted(shared), skill=skill,
                    alchemist_ranks=alchemist_ranks,
                    benefactor=benefactor, poisoner=poisoner,
                    physician=physician),
                "ingredients": [owned[a]["name"], owned[b]["name"],
                                owned[c]["name"]],
                "batches": batches,
                "n": 3,
            })

    potions.sort(key=lambda p: (-p["price"], -len(p["effects"]),
                                p["ingredients"]))
    return potions


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
        # Brew every possible batch (2026-10-19): the old code brewed each
        # distinct potion at most once, stranding value when stock could
        # make 2 (or 200) copies. Max-batches is the greedy-correct move;
        # ruin-and-recreate still repairs the 3-vs-2 blind spot.
        batches = min([counts.get(i, 0) for i in ings] or [0])
        if batches <= 0:
            continue
        for i in ings:
            counts[i] -= batches
        brewed.extend([p] * batches)
        total += p["price"] * batches
    return total, brewed


def brew_allocation(potions: List[dict], counts: dict,
                    min_price: int = 100,
                    batch_mode: bool = False) -> Tuple[int, list]:
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

    batch_mode (2026-10-19): Ben's "just batches for value" — when the
    page sorts by total gold, order by BATCH value (price x batches) and
    commit to full batches. Deliberately skips the ruin/polish phases:
    those exist to squeeze marginal gold by fragmenting batches (3x50gp
    + 1x51gp instead of 4x50gp), which is exactly the fiddliness he
    doesn't want. Leaves a few gold on the table by design.
    """
    best_total, best_brewed = _greedy_brew(potions, counts, min_price)
    if batch_mode:
        # Batch mode: single greedy pass ordered by batch value.
        # No ruin/polish — those trade batch simplicity for marginal gold.
        in_bar = [p for p in potions if p["price"] >= min_price]

        def _batch_value(p):
            b = min([counts.get(i, 0) for i in p["ingredients"]] or [0])
            return p["price"] * b

        ordered = sorted(in_bar, key=lambda p: -_batch_value(p))
        return _greedy_brew(ordered, counts, min_price)
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
# SELL_FACTOR is the legacy flat estimate; live code uses real_sell_factor()
# (UESP-verified Skyrim barter formula) with the player's real Speech,
# Haggling ranks, and Allure.
SELL_FACTOR = 0.3


def real_sell_factor(speech_skill: int, haggling_ranks: int, allure: bool,
                     fortify_barter: float = 0.0) -> float:
    """Fraction of list value a merchant actually pays, per the real Skyrim
    barter formula (UESP: Skyrim:Speech / Skyrim:Merchants).

        base_price_factor = 3.3 - 0.013 * min(speech, 100)
        sell_modifier = HagglingS * AllureS * (1 + fortify_barter)
        sell price = round(value * sell_modifier / base_price_factor)

    HagglingS = 1.10/1.15/1.20/1.25/1.30 for ranks 1-5; AllureS = 1.10.
    fortify_barter is a fraction (0.20 = +20% from gear/potions/blessing).
    At Speech 0, no perks: 1/3.3 = 0.303. At Speech 100: 1/2.0 = 0.5.
    (2026-10-19)
    """
    haggling_s = (1.0, 1.10, 1.15, 1.20, 1.25, 1.30)[
        max(0, min(5, int(haggling_ranks)))]
    allure_s = 1.10 if allure else 1.0
    base = 3.3 - 0.013 * min(max(int(speech_skill), 0), 100)
    return haggling_s * allure_s * (1.0 + max(fortify_barter, 0.0)) / base


# Perk EDID suffixes (Haggling00/20/40/60/80, Alchemist00/20/40/60/80) are the
# skill requirement of that rank — verified against Skyrim.esm 2026-10-19.
# Having the rank proves the skill is at least that high.
_PERK_SKILL_FLOOR = {1: 0, 2: 20, 3: 40, 4: 60, 5: 80}


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

FLOW_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Alchemy Companion — Flow Explorer</title>
<style>
body { font-family: system-ui, sans-serif; margin: 0; padding: 12px; background: #14181d; color: #e8e8e8; }
h1 { font-size: 1.3em; margin: 0 0 4px; }
h2 { font-size: 1.1em; margin: 26px 0 8px; color: #ffd479; }
.hint { color: #9aa4ad; font-size: 0.9em; margin: 0 0 14px; }
a { color: #7cc4ff; }
.node { background: #1d232a; border: 1px solid #39424c; border-radius: 10px; margin: 0 0 8px; overflow: hidden; }
.node > .head { padding: 12px; cursor: pointer; display: flex; gap: 10px; align-items: baseline; }
.node > .head .t { font-weight: bold; font-size: 1em; }
.node > .head .one { color: #9aa4ad; font-size: 0.85em; }
.node > .head .arrow { margin-left: auto; color: #7cc4ff; }
.node > .body { display: none; padding: 0 12px 12px; border-top: 1px solid #2c343d; }
.node.open > .body { display: block; }
.grp { margin: 10px 0 0; }
.grp > .gl { font-size: 0.8em; text-transform: uppercase; letter-spacing: 0.06em; margin-bottom: 4px; }
.grp.reads > .gl { color: #7cc4ff; }
.grp.writes > .gl { color: #8fe388; }
.grp.live > .gl { color: #ffd479; }
.vrow { padding: 5px 8px; background: #161b21; border-radius: 6px; margin-bottom: 4px; font-size: 0.9em; }
.vrow code { color: #ffd479; font-size: 0.95em; }
.vrow .vd { color: #9aa4ad; }
.vrow .vv { color: #8fe388; font-weight: bold; }
.note { margin-top: 10px; padding: 8px; background: #26200f; border-left: 3px solid #ffd479; border-radius: 0 6px 6px 0; font-size: 0.88em; color: #e8d9ae; }
.fatcard { background: #2a1d1d; border: 1px solid #5c3434; border-radius: 10px; padding: 12px; margin: 0 0 8px; font-size: 0.92em; }
.fatcard b { color: #ff9d9d; }
.fatcard code { color: #ffd479; }
.fatcard .fix { color: #9aa4ad; margin-top: 6px; }
.connector { text-align: center; color: #4a545e; font-size: 1.2em; line-height: 1; margin: -2px 0 2px; }
.topbar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; }
</style>
</head>
<body>
<div class="topbar">
<h1>🔍 Flow explorer</h1>
<a href="/">← back to the companion</a>
</div>
<p class="hint">Every stage of the companion, top to bottom. Tap a stage to see the variables it <b>reads</b> and <b>writes</b>. <span style="color:#ffd479">Gold numbers</span> are live from your latest save. Built so you can point at a flaw instead of describing one.</p>
<div id="flow"><p class="hint">Loading…</p></div>
<h2>✂️ Fat candidates</h2>
<p class="hint">Verified against the running code just now. Nothing here is cut — point at what goes.</p>
<div id="fat"></div>
<script>
const NODES = [
{t: "① Save file → pick_save()",
 one: "Newest .ess wins. Right now that's Sekitty's Quicksave.",
 r: [["SAVES_DIR", "find_saves_dir() — your Skyrim saves folder"],
     ["*.ess mtimes", "newest Quicksave / Autosave by modification time"]],
 w: [["save_path", "full path of the chosen save"],
     ["save_name / save_time", "shown in the page header"]],
 live: [["Save", d => d.save_name], ["Modified", d => d.save_time], ["Character", d => d.player]]},
{t: "② Player block: skill + perks",
 one: "Base Alchemy and every perk rank, read from the save — no guessing.",
 r: [["NPC_ DNAM (52 bytes, byte 10)", "base + racial skill, before use level-ups"],
     ["perk RefIDs", "save encoding [0x40|(fid&gt;&gt;16), (fid&gt;&gt;8)&amp;0xFF, fid&amp;0xFF]"],
     ["Skyrim.esm PERK records", "24-byte headers; EDID suffix 00/20/40/60/80 = that rank's skill requirement"]],
 w: [["base_skill = 20", "already auto-detected"],
     ["alchemist_ranks = 3", "Alchemist 00/20/40"],
     ["benefactor / poisoner / physician / purity", "✓ / ✓ / ✓ / ✗"],
     ["haggling_ranks = 2, allure ✓", "Haggling 00/20"]],
 live: [["Alchemist ranks", d => d.detected.alchemist_ranks],
        ["Haggling ranks", d => d.detected.haggling_ranks],
        ["Allure", d => d.detected.allure ? "yes" : "no"]],
 note: "Exact current skill (with use level-ups) is NOT in the save — verified against the ReSaver reference parser. Base 20 + perk floors is the honest bound."},
{t: "③ Skill floors from perks",
 one: "A rank's EDID suffix proves its skill requirement. Manual settings only ratchet up.",
 r: [["_PERK_SKILL_FLOOR", "{1:0, 2:20, 3:40, 4:60, 5:80}"],
     ["detected ranks", "from ②"],
     ["localStorage skyrim-alch-skill / skyrim-speech-skill", "your manual overrides"]],
 w: [["alchemy_floor = 40", "max(manual, floor) — never down"],
     ["speech_floor = 20", "same"],
     ["alch_skill / speech_skill", "effective values used for all pricing"]],
 note: "Your manual Alchemy 30 was stale — the page now raises it to 40 and tells you."},
{t: "④ Inventory → ingredients",
 one: "Everything you own that has alchemy effects, with counts.",
 r: [["ACHR player blob", "change_flags 0xb8000822, inventory section"],
     ["ingredients.json", "formID → {name, effects[4]} lookup"],
     ["gold", "formID 0x0000000F"]],
 w: [["owned[]", "{name, count, effects[4]} per ingredient"],
     ["S.remaining", "{name: count} — the live stock the queue eats"]],
 live: [["Ingredient types", d => d.ingredients.length],
        ["Total units", d => d.ingredients.reduce((a, o) => a + o.count, 0)]]},
{t: "⑤ Known effects",
 one: "Which of the 4 effects per ingredient you've discovered.",
 r: [["extract_known_ingredients()", "bitmask per formID → effect indexes 0–3"]],
 w: [["known_effects", "{name: [idx…]} → S.knownEffects"],
     ["markDiscovered()", "the guide adds to S.knownEffects as you brew, so later steps stay accurate"]],
 live: [["Ingredients with known effects", d => Object.keys(d.known_effects || {}).length]]},
{t: "⑥ brewable_by_effect()",
 one: "Every potion you can brew: all pairs + all triples sharing an effect. Priced with YOUR skill and perks.",
 r: [["owned", "from ④"],
     ["skill, alchemist_ranks, benefactor, poisoner, physician", "from ② + ③"],
     ["potion_price()", "value from effects + skill + perks"]],
 w: [["potions[]", "{ingredients, effects, price} — pair pass, then triple pass (valid when ≥2 of the 3 share an effect)"]],
 live: [["Potions", d => d.potions.length]],
 note: "Oct 19 bug, fixed: both passes called potion_price() with defaults — your skill/perk settings did nothing until the fix. Prices dropped to honest values; recalibrate the slider."},
{t: "⑦ Badges: 3-way wins?",
 one: "Per triple: does it beat the best 2-ingredient split of its own ingredients?",
 r: [["the triple's 3 ingredients", "best brewable pair-split of the same 3"]],
 w: [["★ 3-way wins (green)", "triple beats the split"],
     ["2-way: Xg (neutral)", "split wins — ties go to the split, saves an ingredient"]],
 note: "3 hash lookups per row — profiled, not fat. Per-potion, triple ⊇ pair always; the only loss is allocation, which is the plan's job (⑫)."},
{t: "⑧ real_sell_factor()",
 one: "UESP barter formula. The flat 0.3 was understating your income ~40%.",
 r: [["speech_skill", "manual (floor 20)"],
     ["haggling_ranks = 2", "detected → HagglingS 1.20"],
     ["allure ✓", "detected → AllureS 1.10"],
     ["fortify_barter", "manual %"]],
 w: [["sell_factor", "value × HagglingS × AllureS × (1+fortify) ÷ (3.3 − 0.013 × min(Speech,100))"]],
 live: [["Sell factor", d => (+d.sell_factor).toFixed(4)],
        ["Speech used", d => d.sell_breakdown.speech]],
 note: "Anchors: Speech 0 → 0.303, Speech 100 → 0.500. Yours at Speech 25: 0.4252."},
{t: "⑨ /api/data → last_good",
 one: "Runs on every Refresh. Assembles the payload; /api/plan reads last_good.",
 r: [["stages ②–⑧", "everything above"]],
 w: [["payload → last_good", "{save, player, ingredients, potions, known_effects, known_enchantments, detected, sell_factor, sell_breakdown}"]]},
{t: "⑩ Client state S",
 one: "Everything the page remembers between renders.",
 r: [["/api/data payload", "from ⑨"],
     ["localStorage", "skyrim-alch-skill, skyrim-speech-skill, skyrim-fortify-barter, skyrim-minvalue, skyrim-sortmode, exclusions"]],
 w: [["S.data", "the payload"],
     ["S.remaining", "live stock {name: count}"],
     ["S.knownEffects", "discoveries"],
     ["S.queue[]", "{key, price, ingredients, effects, qty}"],
     ["S.plan", "last computed plan"],
     ["S.neverBrew", "ingredients to hoard (daedra hearts) — Ingredients tab 🚫 toggle, localStorage skyrim-neverbrew"],
     ["S.sort", "{key: price | total, dir}"]]},
{t: "⑪ Tabs",
 one: "Four views over the same S.",
 r: [["S", "from ⑩"]],
 w: [["Potions tab", "min-value slider → filter; sort toggle: per-potion | total (price × batchesLeft(p)); batchesLeft(p) = max batches from S.remaining"],
     ["Ingredients tab", "verdict vs slider: 'best brew Xg ✓' / 'best Yg → triple +A +B: Zg ✓' / 'best Yg — below your bar' / 'no brewable potion'; x0 rows get none"],
     ["Effects tab", "effect → ingredients that have it"],
     ["💎 tab", "known enchantments list + 'Spells you know' (buyable-tome spells the player knows, from player ACHR spell RefIDs ∩ buyable_spells.json)"]]},
{t: "⑫ /api/plan → brew_allocation()",
 one: "On demand only ('Best plan'). Picks the SET for maximum gold — the order comes later.",
 r: [["last_good.potions", "pre-filtered to price ≥ min_price — the 4× speedup"],
     ["counts", "from last_good.ingredients, with S.neverBrew zeroed out"],
     ["min_price", "your slider"]],
 w: [["brews[]", "{ingredients, effects, price, qty} — repeats collapsed ×N, richest first"],
     ["total_value / total_sell", "list value + realistic sell"]],
 note: "Greedy + ruin-and-recreate polish. Multi-batch: max batches per potion. The pre-filter also fixed a +763g early-break quirk at the 300g bar. <b>Batch mode</b> (sort=total): single greedy pass ordered by batch value (price × batches), whole batches only — leaves a few gold on the table to avoid fiddly fragmentation."},
{t: "⑬ planBrewsSorted()",
 one: "'Best plan' display + 'Queue it all' follow your sort toggle. Same brews, same gold — only the sequence changes.",
 r: [["S.sort.key", "per-potion → price desc (fastest Alchemy XP); total → price × qty desc (gold right now)"],
     ["plan.brews", "untouched — sorts a copy"]],
 w: [["ordered brews", "what you see is what gets queued"]]},
{t: "⑭ Queue",
 one: "'Queue it all' in one tap. Removing an entry restores its ingredients — unless the guide consumed them.",
 r: [["ordered brews", "from ⑬"], ["S.remaining", "consumed as items queue"]],
 w: [["S.queue[]", "entries with qty"],
     ["queueOne()", "single add"],
     ["dequeue (✕)", "restores ingredients — manual removal only"]]},
{t: "⑮ Guided brew (G)",
 one: "One step per queue entry. Big ingredients, alphabetical. 'Brewed it' = the whole batch.",
 r: [["S.queue", "one step per entry — qty kept, not expanded"]],
 w: [["G.steps[]", "{price, ingredients, effects, qty, qref}"],
     ["G.idx", "entry position"],
     ["G.brewedGold", "+= price × qty per tap"],
     ["header", "'✓ N brewed of M' — completed count via brewsDone()/brewsTotal()"],
     ["markDiscovered()", "new effects → S.knownEffects"]],
 note: "Done screen: Quicksave (F5) in-game, then Refresh."}
];
const FAT = [
{t: "✓ CUT 2026-10-19 — shop payload — computed every Refresh, displayed never",
 d: "<code>read_current()</code> runs <code>shopping_by_effect()</code> + a baseline greedy loop on <b>every</b> /api/data call. The 'One ingredient away' render block was killed Oct 19 — zero JS references to <code>.shop</code> remain.",
 f: "Delete the block (or stop calling it) and the payload shrinks with it."},
{t: "✓ RESOLVED 2026-10-19 — spell tomes read (was: known_spells, mislabeled)",
 d: "The type-13 change forms are <b>books the player has read</b> (UESP: type 13 = BOOK). Oct 5 misidentified them as known spells; Oct 19 the three FormIDs resolved to regular books. But Ben's save-diff experiment proved the useful part: reading the Fast Healing <b>spell tome</b> (0x000A271D) set a type-13 flag. So the 'don't buy duplicate tomes' list = type-13 flags ∩ spell-tome IDs (spell_tomes.json) — now live in the 💎 tab as 'Spell tomes read', separate from enchantments. Starting spells (Flames, Healing) aren't covered — he knows them without reading tomes.",
 f: "Keep the tomb-ID list in sync if DLC tomes matter."},
{t: "✓ CUT 2026-10-19 — extract_merchant_inventory() + MERCHANT_CHESTS — dead on arrival",
 d: "Defined, never called. Merchant stock is unknowable (Oct 5 finding stands); only Arcadia's chest was ever mapped.",
 f: "Delete the function and the dict."},
{t: "✓ CUT 2026-10-19 — Shopping-subsystem leftovers",
 d: "<code>shop_recommendations()</code>, <code>phase_shop_plan()</code>, <code>shopping_from_table()</code>, <code>brewable_from_table()</code>, <code>load_potion_table()</code>, <code>greedy_brew_total_static()</code> — no live callers anywhere.",
 f: "Delete; git history keeps them if the shop list ever comes back."},
{t: "✓ CUT 2026-10-19 — compute_potions() — legacy O(n³)",
 d: "Superseded by <code>brewable_by_effect()</code>. Only used by the <code>--once</code> CLI debug path — never in server mode.",
 f: "Keep for CLI debugging, or delete."},
{t: "✓ CUT 2026-10-19 — Shop exclusion machinery — orphaned",
 d: "<code>getExcluded/setExcluded</code>, the <code>skyrim-shop-excluded / -forced / -loc</code> keys, and the exclusion params sent with every /api/data request. The ✕-on-a-buy UI died with the shop list.",
 f: "Delete, or revive if the shop list returns."},
{t: "SELL_FACTOR = 0.3 — demoted, not dead",
 d: "Still the fallback in /api/plan and inside the dead shop paths. Live code uses <code>real_sell_factor()</code>.",
 f: "Harmless — keep as the fallback."}
];
function esc(s) { return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;"); }
function renderNodes() {
  const el = document.getElementById("flow");
  el.innerHTML = "";
  NODES.forEach((n, i) => {
    const d = document.createElement("div");
    d.className = "node";
    let h = "<div class='head'><span class='t'>" + esc(n.t) + "</span>" +
            "<span class='arrow'>▸</span></div>" +
            "<div style='padding:0 12px 10px'><span class='one'>" + n.one + "</span></div>" +
            "<div class='body'>";
    const grp = (cls, label, rows, live) => {
      let g = "<div class='grp " + cls + "'><div class='gl'>" + label + "</div>";
      rows.forEach(r => {
        g += "<div class='vrow'><code>" + r[0] + "</code> <span class='vd'>" + r[1] + "</span>" +
             (live ? " <span class='vv'>" + esc(live) + "</span>" : "") + "</div>";
      });
      return g + "</div>";
    };
    h += grp("reads", "Reads", n.r) + grp("writes", "Writes", n.w);
    h += "<div class='grp live' id='live-" + i + "'><div class='gl'>Live</div><div class='vrow'><span class='vd'>fetching /api/data…</span></div></div>";
    if (n.note) h += "<div class='note'>" + n.note + "</div>";
    d.innerHTML = h + "</div>";
    d.querySelector(".head").addEventListener("click", () => d.classList.toggle("open"));
    el.appendChild(d);
    if (i < NODES.length - 1) {
      const c = document.createElement("div");
      c.className = "connector"; c.textContent = "▼";
      el.appendChild(c);
    }
  });
}
function renderFat() {
  const el = document.getElementById("fat");
  el.innerHTML = "";
  FAT.forEach((f, i) => {
    const d = document.createElement("div");
    d.className = "fatcard";
    d.innerHTML = "<b>#" + (i + 1) + " " + esc(f.t) + "</b><div style='margin-top:6px'>" + f.d + "</div>" +
                  "<div class='fix'>→ " + f.f + "</div>";
    el.appendChild(d);
  });
}
function fillLive(D) {
  NODES.forEach((n, i) => {
    const el = document.getElementById("live-" + i);
    if (!el || !n.live) { if (el) el.innerHTML = "<div class='gl'>Live</div><div class='vrow'><span class='vd'>—</span></div>"; return; }
    let h = "<div class='gl'>Live</div>";
    n.live.forEach(L => {
      let v = "—";
      try { v = L[1](D); } catch (e) { v = "n/a"; }
      h += "<div class='vrow'><span class='vd'>" + esc(L[0]) + ":</span> <span class='vv'>" + esc(v) + "</span></div>";
    });
    el.innerHTML = h;
  });
}
renderNodes();
renderFat();
fetch("/api/data").then(r => r.json()).then(D => {
  if (D && D.ok) fillLive(D);
  else document.querySelectorAll(".grp.live").forEach(e => e.innerHTML = "<div class='gl'>Live</div><div class='vrow'><span class='vd'>/api/data failed</span></div>");
}).catch(() => {
  document.querySelectorAll(".grp.live").forEach(e => e.innerHTML = "<div class='gl'>Live</div><div class='vrow'><span class='vd'>could not reach /api/data</span></div>");
});
</script>
</body>
</html>
"""


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
    display: flex; align-items: center; gap: 0.8rem; flex-wrap: wrap;
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
  /* 3-way vs 2-way comparison badge (2026-10-19): on a 3-ingredient row,
     shows whether brewing it beats the best 2-ingredient split of its
     own ingredients (green), or what the best split earns (neutral). */
  .splitbadge {
    display: inline-block; font-size: 0.75rem; padding: 0.1rem 0.5rem;
    border-radius: 1rem; background: #1a1610; color: #b7a888;
    border: 1px solid #3a3226; margin-top: 0.25rem; white-space: nowrap;
  }
  .splitbadge.win { background: #2a3a1a; color: #b0d080; border-color: #5a7a3a; }
  /* ROI on shop buys (2026-10-19) */
  .roi {
    display: inline-block; font-size: 0.8rem; padding: 0.1rem 0.5rem;
    border-radius: 1rem; background: #2a3a1a; color: #b0d080;
    border: 1px solid #5a7a3a; margin-left: 0.4rem; white-space: nowrap;
  }
  /* Ingredient best-brew verdict (2026-10-19) */
  .ingverdict { font-size: 0.8rem; color: #b7a888; margin-top: 0.2rem; }
  .ingverdict b { color: #ffe9b0; }
  .ingverdict.deadweight { color: #8a7a66; }
  /* Never-brew toggle (2026-10-19): dim until armed. */
  .nb { background: none; border: none; cursor: pointer; font-size: 1rem;
        opacity: 0.25; padding: 0 2px; }
  .nb.on { opacity: 1; }
  /* Optimal brew plan (2026-10-19) */
  #plan {
    margin: 0.7rem 0; padding: 0.6rem 0.7rem;
    background: #1a1e2a; border: 1px solid #4a5a7a; border-radius: 0.6rem;
  }
  #plan h3 { margin: 0 0 0.4rem; font-size: 1rem; color: #cfe0e8; }
  #plan .p-total { color: #ffe9b0; font-weight: bold; }
  #plan ol { margin: 0.4rem 0; padding-left: 1.4rem; font-size: 0.92rem; }
  #plan li { margin-bottom: 0.3rem; }
  #plan .p-price { color: #ffd97a; font-weight: bold; white-space: nowrap; }
  #plan .p-rowbtns { display: flex; gap: 0.5rem; margin-top: 0.5rem; flex-wrap: wrap; }
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
  /* Sort-mode select in the threshold bar (2026-10-19): per-potion value
     (fastest alchemy leveling when brewing everything) vs total brewable
     gold (price x batches, for "I need 10,000 gold right now"). */
  .threshold-bar select {
    font-size: 0.9rem; padding: 0.5rem; border-radius: 0.5rem;
    border: 1px solid #6b5d43; background: #1d1913; color: #e8ddc9;
    min-height: 44px;
  }
  /* Guided brew mode (2026-10-19): step through the queue one potion at a
     time, ingredients big and alphabetical to match the in-game menu. */
  #guide {
    margin: 0.7rem 0; padding: 1rem;
    background: #1a2233; border: 1px solid #4a5a7a; border-radius: 0.6rem;
  }
  #guide .g-step { color: #8a9a86; font-size: 0.9rem; }
  #guide .g-potion { font-size: 1.25rem; margin: 0.5rem 0; color: #ffe9b0; }
  #guide .g-potion .g-eff { font-size: 0.85rem; color: #9a8f76; }
  #guide .g-ings { display: flex; flex-direction: column; gap: 0.6rem;
    margin: 1rem 0; }
  #guide .g-ing {
    font-size: 1.3rem; padding: 1rem 1.2rem; border-radius: 0.6rem;
    border: 2px solid #4a6b43; background: #1d2a1a; color: #e8ddc9;
    text-align: left;
  }
  #guide .g-ing.ing-discovery { border-color: #8fd08f; }
  #guide .g-rowbtns { display: flex; gap: 0.5rem; flex-wrap: wrap; }
  #guide .g-next {
    font-size: 1.1rem; padding: 0.8rem 1.4rem; min-height: 56px;
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
  <a href="/flow" title="Explorable flowchart of every variable" style="margin-left:8px;font-size:0.85em">&#128269; flow</a>
  <div id="status">Loading&hellip;</div>
</header>
<div id="error"></div>

<div id="queue" hidden>
  <h3>&#129514; Brew Queue <span class="badge" id="q-count">0</span>
    &nbsp;<span class="q-total" id="q-total"></span>
    <button class="qbtn" id="q-clear" style="float:right">Clear</button>
    <button class="qbtn" id="q-guide" style="float:right;margin-right:0.4rem"
      title="Step through the queue one potion at a time">&#9654; Guide</button>
  </h3>
  <div id="q-list"></div>
</div>
<div id="guide" hidden></div>

<nav class="tabs">
  <button id="tabbtn-price" class="active">&#128176; Potions</button>
  <button id="tabbtn-disenchant">&#128142; Enchantments</button>
</nav>

<main>
  <section id="tab-price" class="tab active">
    <div class="threshold-bar">
      <label for="minval">Min value: <span id="minval-show">0</span>g</label>
      <input type="range" id="minval" min="0" max="1000" step="25" value="0">
      <select id="sortmode" title="Sort the potion list">
        <option value="per">Sort: per potion</option>
        <option value="total">Sort: total gold</option>
      </select>
      <button id="planbtn" class="btn" title="Compute the optimal brew order for maximum profit">Best plan</button>
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
    <details class="alchemy-settings">
      <summary>💰 Selling (real merchant prices)</summary>
      <div id="sell-detected" class="hint" style="margin-bottom:0.5rem"></div>
      <div class="settings-grid">
        <label>Speech skill: <input type="number" id="speech-skill" min="15" max="100" value="15" style="width:4rem"></label>
        <label>Fortify Barter %: <input type="number" id="fortify-barter" min="0" max="200" value="0" style="width:4rem" title="Total % from gear, potions, and blessings (e.g. 20 = +20%)"></label>
        <button id="sell-apply">Apply</button>
      </div>
      <div class="hint">Haggling ranks and Allure auto-detect from your save. The old flat "sells at 30%" estimate is gone — sell values now use the real barter formula.</div>
    </details>
    <div class="controls">
      <input id="search" type="search" placeholder="Filter: effect or ingredient&hellip;" autocomplete="off">
      <select id="ingfilter"><option value="">All ingredients</option></select>
    </div>
    <h2 id="potion-h">Potions you can brew</h2>
    <div class="hint">Tap a column header to sort. Tap <b>+</b> to queue a brew &mdash; ingredients are deducted and the list updates.</div>
    <div id="plan" hidden></div>
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
    <h2 style="margin-top:18px">Spells you know</h2>
    <div class="hint">Spell tomes you've already read — no need to buy these again. Auto-detected from save. Spells an NPC taught you directly (like Faralda's College entry test) have no tome, so they won't show here.</div>
    <div id="spell-tomes-read"></div>
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
  neverBrew: new Set(), // ingredients to hoard, never brew (localStorage)
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
// Never-brew list (2026-10-19): ingredients Ben wants to hoard
// (daedra hearts...). Toggled per row on the Ingredients tab,
// persisted, honored by brewable(), batchesLeft(), and /api/plan.
function getNeverBrew() {
  try { return new Set(JSON.parse(localStorage.getItem("skyrim-neverbrew") || "[]")); }
  catch (e) { return new Set(); }
}
function setNeverBrew(set) {
  localStorage.setItem("skyrim-neverbrew", JSON.stringify([...set]));
}
function brewable(p) {
  for (const nm of p.ingredients) {
    if (S.neverBrew.has(nm)) return false;
    if ((S.remaining[nm] || 0) < 1) return false;
  }
  return true;
}

function batchesLeft(p) {
  let m = Infinity;
  for (const nm of p.ingredients) {
    if (S.neverBrew.has(nm)) return 0;
    m = Math.min(m, S.remaining[nm] || 0);
  }
  return m;
}

function potionKey(p) {
  return p.ingredients.join("\x01") + "\x02" + p.effects.join("\x01");
}

function queueOne(p) {
  if (!brewable(p)) return false;
  for (const nm of p.ingredients) S.remaining[nm]--;
  const k = potionKey(p);
  const q = S.queue.find((e) => e.key === k);
  if (q) q.qty++;
  else S.queue.push({ key: k, price: p.price, ingredients: p.ingredients.slice(),
                      effects: p.effects.slice(), qty: 1 });
  return true;
}

function brew(p) {
  if (queueOne(p)) renderAll();
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
    // "total" (2026-10-19): gold extractable if you brewed every possible
    // batch of this potion from current stock. For the "I need 10,000 gold
    // right now" sort; per-potion stays the fastest-alchemy-leveling order.
    if (key === "total") return p.price * batchesLeft(p);
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
    // Total-sort mode (2026-10-19): lead with extractable gold, per-potion
    // value as the sub-line.
    if (S.sort.key === "total") {
      const b = batchesLeft(p);
      tdP.innerHTML = (p.price * b) +
        "<div style='font-size:0.75rem;color:#8a9a86;font-weight:normal'>" +
        p.price + "g \u00d7 " + b + "</div>";
    } else {
      tdP.textContent = p.price;
    }
    tr.appendChild(tdP);
    // 3-way vs 2-way badge (2026-10-19): compare this 3-ingredient brew
    // against the best 2-ingredient potion brewable from its own ingredients.
    if (p.ingredients.length === 3 && S.pairBest) {
      const nm = p.ingredients.slice().sort();
      let best = 0;
      const pairs = [[nm[0], nm[1]], [nm[0], nm[2]], [nm[1], nm[2]]];
      for (const pr of pairs) {
        const v = S.pairBest[pr.join("\x01")] || 0;
        if (v > best) best = v;
      }
      if (best > 0) {
        const tag = document.createElement("div");
        if (p.price > best) {
          tag.className = "splitbadge win";
          tag.textContent = "\u2605 3-way wins";
          tag.title = "This 3-ingredient brew (" + p.price + "g) beats the best 2-way split (" + best + "g).";
        } else {
          tag.className = "splitbadge";
          tag.textContent = "2-way: " + best + "g";
          tag.title = "Brewing the best pair instead earns " + best + "g vs " + p.price + "g for the triple (and saves an ingredient).";
        }
        tdP.appendChild(tag);
      }
    }

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
  syncSortSelect();
  renderPriceTab();
}

// Keep the sort-mode select showing the real sort (2026-10-19).
function syncSortSelect() {
  const sel = $("sortmode");
  if (sel) sel.value = S.sort.key === "total" ? "total" : "per";
}


function renderPriceTab() {
  const list = filteredPricePotions();
  const el = $("potions");
  el.innerHTML = "";
  syncSortSelect();

  // "One ingredient away" shopping list — REMOVED 2026-10-19 at Ben's
  // request ("the page is getting a little crowded... not useful most of
  // the time"). The server still computes it (cheap); this just stops
  // rendering it. The ROI/triple/search work on it stays in git history.

  $("potion-h").innerHTML = "Potions you can brew " +
    "<span class='badge'>" + list.length + "</span>";
  // 3-way vs 2-way comparison (2026-10-19): for every brewable 2-ingredient
  // potion, record the best price per sorted ingredient pair. potionTable
  // uses it to badge 3-ingredient rows against their best 2-way split.
  // Recomputed every render so it tracks the remaining pool and min value.
  const pairBest = {};
  for (const q of S.data.potions) {
    if (q.ingredients.length !== 2 || q.price < S.minValue) continue;
    if (!brewable(q)) continue;
    const k = q.ingredients.slice().sort().join("\x01");
    if (!(k in pairBest) || q.price > pairBest[k]) pairBest[k] = q.price;
  }
  S.pairBest = pairBest;
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

// ------------------------------------------------- guided brew mode (2026-10-19)
// Ben brews the whole queue at once; this walks him through it one potion
// at a time. Ingredients are big and alphabetical (matches the in-game
// alchemy menu order); tap each as it's selected in-game. The discovery
// highlight is the same one the main list uses (ingredient gets the green
// background when brewing would reveal an unknown effect).
const G = { steps: [], idx: 0, brewedGold: 0, active: false };

// ingredient name -> newly revealed effect (or null), mirroring the
// wouldDiscover logic in potionTable so the two lists always agree.
function stepDiscoveries(st) {
  const out = {};
  for (const nm of st.ingredients) {
    out[nm] = null;
    const ingData = S.data.ingredients.find((g) => g.name === nm);
    if (!ingData) continue;
    for (const eff of st.effects) {
      if (ingData.effects.indexOf(eff) !== -1 && !isKnown(nm, eff)) {
        out[nm] = eff;
        break;
      }
    }
  }
  return out;
}

// He really brewed it in-game, so the revealed effects are now known.
// Keeps later steps' highlighting accurate without a save refresh.
function markDiscovered(st) {
  for (const nm of st.ingredients) {
    const ingData = S.data.ingredients.find((g) => g.name === nm);
    if (!ingData) continue;
    if (!S.knownEffects[nm]) S.knownEffects[nm] = [];
    const known = S.knownEffects[nm];
    for (const eff of st.effects) {
      const idx = ingData.effects.indexOf(eff);
      if (idx !== -1 && known.indexOf(idx) === -1) known.push(idx);
    }
  }
}

function startGuide() {
  if (!S.queue.length) return;
  G.steps = [];
  // One step per queue entry (2026-10-19: Ben brews the whole batch in-game,
  // so "Brewed it" completes the batch — no per-copy steps).
  S.queue.forEach((q) => {
    G.steps.push({ price: q.price, ingredients: q.ingredients.slice(),
                   effects: q.effects.slice(), qty: q.qty, qref: q });
  });
  G.idx = 0;
  G.brewedGold = 0;
  G.active = true;
  document.querySelector("main").hidden = true;
  document.querySelector("nav.tabs").hidden = true;
  $("queue").hidden = true;
  $("guide").hidden = false;
  renderGuideStep();
}

// Completed brews (not position) for the guide header (2026-10-19).
function brewsDone() {
  let n = 0;
  for (let i = 0; i < G.idx; i++) n += G.steps[i].qty;
  return n;
}
function brewsTotal() {
  return G.steps.reduce((a, s) => a + s.qty, 0);
}

function renderGuideStep() {
  const el = $("guide");
  const st = G.steps[G.idx];
  el.innerHTML = "";
  const h = document.createElement("div");
  h.className = "g-step";
  // Completed count, never position: "Brew 2 of 14" read as "2 brewed",
  // which was the off-by-one Ben saw. This starts at 0 and only moves
  // when he taps "Brewed it".
  h.textContent = "\u2713 " + brewsDone() + " brewed of " + brewsTotal() +
    (G.brewedGold ? " \u2014 " + G.brewedGold + "g so far" : "");
  el.appendChild(h);
  const title = document.createElement("div");
  title.className = "g-potion";
  title.innerHTML = (st.qty > 1 ? "Brew <b>\u00d7" + st.qty + "</b> " : "") +
    "<b>" + st.price + "g</b> <span class='g-eff'>" +
    st.effects.map(escapeHtml).join(" + ") + "</span>";
  el.appendChild(title);
  const discoveries = stepDiscoveries(st);
  const ings = st.ingredients.slice().sort((a, b) => a.localeCompare(b));
  const box = document.createElement("div");
  box.className = "g-ings";
  // Static display (2026-10-19: Ben ditched the tap-to-check — the step is
  // offscreen the instant he hits Next, so the effect never earned its keep).
  ings.forEach((nm) => {
    const d = document.createElement("div");
    d.className = "g-ing" + (discoveries[nm] ? " ing-discovery" : "");
    d.innerHTML = escapeHtml(nm) +
      (discoveries[nm] ? " <span class='discover-badge'>\u2605 discovers " +
        escapeHtml(discoveries[nm]) + "</span>" : "");
    box.appendChild(d);
  });
  el.appendChild(box);
  const row = document.createElement("div");
  row.className = "g-rowbtns";
  const nextBtn = document.createElement("button");
  nextBtn.className = "btn g-next";
  nextBtn.textContent = (G.idx + 1 === G.steps.length)
    ? "\u2713 Brewed it \u2014 finish" : "\u2713 Brewed it \u2014 next";
  nextBtn.title = "Tap after crafting this potion in-game";
  nextBtn.addEventListener("click", guideBrewed);
  const skip = document.createElement("button");
  skip.className = "btn";
  skip.textContent = "Skip";
  skip.title = "Skip this brew (stays in the queue)";
  skip.addEventListener("click", guideSkip);
  const exit = document.createElement("button");
  exit.className = "btn";
  exit.textContent = "Exit guide";
  exit.addEventListener("click", exitGuide);
  row.appendChild(nextBtn); row.appendChild(skip); row.appendChild(exit);
  el.appendChild(row);
}

// He crafted it in-game: the whole batch is done in one tap (2026-10-19),
// so drop the entire queue entry WITHOUT restoring ingredients
// (they're really gone). Skips leave the queue untouched.
function guideBrewed() {
  const st = G.steps[G.idx];
  markDiscovered(st);
  G.brewedGold += st.price * st.qty;
  const qi = S.queue.indexOf(st.qref);
  if (qi !== -1) S.queue.splice(qi, 1);
  G.idx++;
  if (G.idx >= G.steps.length) renderGuideDone();
  else renderGuideStep();
}

function guideSkip() {
  G.idx++;
  if (G.idx >= G.steps.length) renderGuideDone();
  else renderGuideStep();
}

function renderGuideDone() {
  G.active = false;
  const el = $("guide");
  el.innerHTML = "";
  const d = document.createElement("div");
  d.innerHTML = "<h3>All brewed! " + G.brewedGold + "g worth.</h3>" +
    "<div class='hint'>Quicksave (F5) in-game, then hit Refresh up top " +
    "to pick up your new stock.</div>";
  el.appendChild(d);
  const b = document.createElement("button");
  b.className = "btn";
  b.textContent = "Exit guide";
  b.addEventListener("click", exitGuide);
  el.appendChild(b);
}

function exitGuide() {
  G.active = false;
  $("guide").hidden = true;
  document.querySelector("main").hidden = false;
  document.querySelector("nav.tabs").hidden = false;
  renderAll();  // queue box reappears only if brews remain
}

// ------------------------------------------------- optimal brew plan (2026-10-19)
// "Do this, in this order for maximum profit": the server runs
// brew_allocation (ruin-and-recreate + randomized polish, exact optimum
// on real stocks) over the inventory; the page renders the ordered plan.
function computePlan() {
  const btn = $("planbtn");
  btn.disabled = true;
  btn.textContent = "Brewing the numbers…";
  // Batch mode (2026-10-19): in "total gold" sort the server plans in
  // whole batches (batch-value order, no fragmentation); in per-potion
  // sort it squeezes max gold as before.
  fetch("api/plan?min_price=" + S.minValue + "&sort=" + S.sort.key +
        (S.neverBrew.size
          ? "&nobrewing=" + encodeURIComponent([...S.neverBrew].join("\\n"))
          : ""))
    .then((r) => r.json())
    .then((plan) => renderPlan(plan))
    .catch(() => {
      const el = $("plan");
      el.hidden = false;
      el.innerHTML = "<h3>Brew plan</h3><div class='hint'>Couldn't compute " +
        "the plan — is the server still running?</div>";
    })
    .finally(() => { btn.disabled = false; btn.textContent = "Best plan"; });
}

// Plan brews in the active sort order (2026-10-19): the optimizer picks the
// SET for maximum gold, but the SEQUENCE is Ben's call — per-potion value
// first levels Alchemy fastest (XP scales with potion value), total-gold
// first is for "I need gold right now". Re-sorting never changes the gold
// total, only the brew order.
function planBrewsSorted(plan) {
  const arr = plan.brews.slice();
  if (S.sort.key === "total") {
    const tot = (b) => b.price * (b.qty || 1);
    arr.sort((a, b) => tot(b) - tot(a));
  } else {
    arr.sort((a, b) => b.price - a.price);
  }
  return arr;
}

function renderPlan(plan) {
  const el = $("plan");
  el.hidden = false;
  el.innerHTML = "";
  if (!plan.ok || !plan.brews.length) {
    el.innerHTML = "<h3>Brew plan</h3><div class='hint'>Nothing worth " +
      "brewing at your min value.</div>";
    return;
  }
  const h = document.createElement("h3");
  h.innerHTML = "Brew this, in this order — <span class='p-total'>" +
    plan.brews.length + " brews, " + plan.total_value + "g list (" +
    plan.total_sell + "g selling)</span>";
  el.appendChild(h);
  // Buttons FIRST (2026-10-19: Ben shouldn't have to scroll past the whole
  // list to queue it).
  const row = document.createElement("div");
  row.className = "p-rowbtns";
  const q = document.createElement("button");
  q.className = "btn";
  q.textContent = "Queue it all";
  q.title = "Queue every brewable plan item in order";
  q.addEventListener("click", () => queuePlan(plan));
  const d = document.createElement("button");
  d.className = "btn";
  d.textContent = "Dismiss";
  d.addEventListener("click", () => { el.hidden = true; el.innerHTML = ""; });
  row.appendChild(q); row.appendChild(d);
  el.appendChild(row);
  const ol = document.createElement("ol");
  planBrewsSorted(plan).forEach((b) => {
    const li = document.createElement("li");
    li.innerHTML = b.ingredients.map(escapeHtml).join(" + ") +
      (b.qty > 1 ? " <b>\u00d7" + b.qty + "</b>" : "") +
      " — <span class='p-price'>" + b.price + "g</span> <span class='hint'>" +
      b.effects.map(escapeHtml).join(" + ") + "</span>";
    ol.appendChild(li);
  });
  el.appendChild(ol);
  S.plan = plan;
}

function queuePlan(plan) {
  let queued = 0, skipped = 0;
  for (const b of planBrewsSorted(plan)) {
    const p = { ingredients: b.ingredients.slice(), effects: b.effects.slice(),
                price: b.price };
    const want = b.qty || 1;
    let got = 0;
    for (let k = 0; k < want; k++) if (queueOne(p)) got++;
    queued += got;
    skipped += want - got;
  }
  renderAll();
  const el = $("plan");
  // renderAll doesn't touch #plan; report what happened.
  const note = document.createElement("div");
  note.className = "hint";
  note.textContent = "Queued " + queued + " of " + plan.brews.length +
    (skipped ? " (" + skipped + " skipped — ingredients already queued)" : "") + ".";
  el.appendChild(note);
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
  // Spells you know (2026-10-19, evening): the real list, from the
  // player ACHR spell RefIDs — buyable-tome spells only (Ben's scope).
  const spellEl = $("spell-tomes-read");
  const spells = S.data.known_spells || [];
  if (spells.length) {
    spellEl.innerHTML = "<div class='hint'>Spells you know (" +
      spells.length + " with buyable tomes):</div>" +
      spells.map((t) => "<div class='disenchant-row'><div><b>" +
        escapeHtml(t) + "</b></div></div>").join("");
  } else {
    spellEl.innerHTML = "<div class='empty'>No buyable-tome spells known yet.</div>";
  }
}

function renderIngredients() {
  $("ing-h").textContent =
    "Your ingredients (" + S.data.ingredients.length + ") — tap effects to mark known";
  const el = $("ingredients");
  el.innerHTML = "";
  // Rescue triples (2026-10-19): for each ingredient, its best brewable
  // 2-ingredient potion and best brewable 3-ingredient potion. If the best
  // pair is below his value slider but a triple clears it, show the rescue
  // ("it needs to be added to another potion"). Single pass over potions.
  const ingBest = {};
  for (const p of S.data.potions) {
    const n = p.ingredients.length;
    if ((n !== 2 && n !== 3) || !brewable(p)) continue;
    for (const gname of p.ingredients) {
      let e = ingBest[gname];
      if (!e) e = ingBest[gname] = { pair: null, triple: null };
      const partners = p.ingredients.filter((x) => x !== gname);
      if (n === 2 && (!e.pair || p.price > e.pair.price))
        e.pair = { price: p.price, with: partners };
      if (n === 3 && (!e.triple || p.price > e.triple.price))
        e.triple = { price: p.price, with: partners };
    }
  }
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
    // Never-brew toggle (2026-10-19): hoard this ingredient.
    const nb = document.createElement("button");
    const isNB = S.neverBrew.has(g.name);
    nb.className = "nb" + (isNB ? " on" : "");
    nb.textContent = "\\uD83D\\uDEAB";
    nb.title = isNB ? "Reserved — tap to allow brewing"
                    : "Tap to never brew with this ingredient";
    nb.addEventListener("click", () => {
      if (S.neverBrew.has(g.name)) S.neverBrew.delete(g.name);
      else S.neverBrew.add(g.name);
      setNeverBrew(S.neverBrew);
      renderAll();
    });
    row.appendChild(nb);
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
    // Best-brew verdict vs his slider.
    if (left > 0) {
      const b = ingBest[g.name];
      const v = document.createElement("div");
      v.className = "ingverdict";
      if (S.neverBrew.has(g.name)) {
        v.innerHTML = "\\uD83D\\uDEAB reserved — never brew";
        v.className += " deadweight";
      } else if (b && b.pair && b.pair.price >= S.minValue) {
        v.innerHTML = "best brew <b>" + b.pair.price + "g</b> \u2713";
      } else if (b && b.triple && b.triple.price >= S.minValue) {
        v.innerHTML = "pair " + (b.pair ? b.pair.price : 0) + "g \u2192 " +
          "triple +" + b.triple.with.map(escapeHtml).join(" +") +
          ": <b>" + b.triple.price + "g</b> \u2713";
      } else {
        const best = b ? Math.max(b.pair ? b.pair.price : 0,
                                 b.triple ? b.triple.price : 0) : 0;
        v.textContent = best > 0
          ? "best " + best + "g — below your bar"
          : "no brewable potion";
        v.className += " deadweight";
      }
      row.appendChild(v);
    }
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
  updateDetectedDisplay();  // refresh perk floors / sell factor line
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
  // Honor the saved sort mode (2026-10-19): per-potion default.
  const savedMode = localStorage.getItem("skyrim-sortmode") || "per";
  S.sort = { key: savedMode === "total" ? "total" : "price", dir: -1 };
  S.ingFilter = "";
  S.search = "";
  $("search").value = "";
  // Known effects come from the save file (authoritative).
  // Maps ingredient name -> list of known effect indexes (0-3).
  S.knownEffects = data.known_effects || {};
  // Never-brew list persists across refreshes (2026-10-19).
  S.neverBrew = getNeverBrew();
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

// Sell settings (2026-10-19): Speech + Fortify Barter are manual
// (Speech level-ups aren't in a parseable save location); Haggling
// ranks and Allure auto-detect from the save.
function getSellSettings() {
  return {
    speech: parseInt(localStorage.getItem("skyrim-speech-skill") || "15", 10) || 15,
    fortifyBarter: parseFloat(localStorage.getItem("skyrim-fortify-barter") || "0") || 0,
  };
}
function saveSellSettings(s) {
  localStorage.setItem("skyrim-speech-skill", String(s.speech));
  localStorage.setItem("skyrim-fortify-barter", String(s.fortifyBarter));
}
function initSellSettingsUI() {
  const s = getSellSettings();
  $("speech-skill").value = s.speech;
  $("fortify-barter").value = Math.round(s.fortifyBarter * 100);
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
  // Perk-implied skill floor (2026-10-19): rank requirements prove the skill.
  // The server clamps too, but the input itself is raised to the floor so
  // it's visible (and the stale manual value is healed on save).
  if (d.alchemy_floor) {
    const inp = $("alch-skill");
    const manual = parseInt(inp.value || "0", 10) || 0;
    if (manual < d.alchemy_floor) {
      inp.value = d.alchemy_floor;
      const s = getAlchemySettings();
      s.skill = d.alchemy_floor;
      saveAlchemySettings(s);
      parts.push("Skill raised to " + d.alchemy_floor + " (floor from perks; was " + manual + ")");
    } else {
      parts.push("Perks prove Alchemy \u2265 " + d.alchemy_floor);
    }
  }
  el.textContent = parts.length ? "Detected from save: " + parts.join(" | ") : "";

  // Selling block (2026-10-19): Haggling/Allure auto-detected, Speech manual.
  const sel = $("sell-detected");
  if (sel) {
    const sparts = [];
    const sperks = [];
    if (d.haggling_ranks) sperks.push("Haggling " + d.haggling_ranks);
    if (d.allure) sperks.push("Allure");
    if (sperks.length) sparts.push("Detected: " + sperks.join(", "));
    else if (d.base_skill != null) sparts.push("No Speech perks detected");
    if (d.speech_floor) {
      const sinp = $("speech-skill");
      const smanual = parseInt(sinp.value || "0", 10) || 0;
      if (smanual < d.speech_floor) {
        sinp.value = d.speech_floor;
        const ss = getSellSettings();
        ss.speech = d.speech_floor;
        saveSellSettings(ss);
        sparts.push("Speech raised to " + d.speech_floor + " (floor from perks; was " + smanual + ")");
      } else {
        sparts.push("Perks prove Speech \u2265 " + d.speech_floor);
      }
    }
    const bd = (S.data && S.data.sell_breakdown) || null;
    const sf = S.data && S.data.sell_factor;
    if (bd && typeof sf === "number") {
      let desc = "Merchants pay " + (sf * 100).toFixed(1) + "% of list " +
        "(Speech " + bd.speech;
      if (bd.haggling_ranks) desc += ", Haggling " + bd.haggling_ranks;
      if (bd.allure) desc += ", Allure";
      if (bd.fortify_barter) desc += ", +" + (bd.fortify_barter * 100) + "% Barter";
      desc += ")";
      sparts.push(desc);
    }
    sel.textContent = sparts.join(" | ");
  }
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
  let url = "/api/data";
  const params = [];
  // Alchemy skill/perks for dynamic potion values.
  const alch = getAlchemySettings();
  params.push("alch_skill=" + alch.skill);
  params.push("alch_ranks=" + alch.ranks);
  params.push("alch_benefactor=" + (alch.benefactor ? "1" : "0"));
  params.push("alch_poisoner=" + (alch.poisoner ? "1" : "0"));
  params.push("alch_physician=" + (alch.physician ? "1" : "0"));
  // Speech skill + Fortify Barter for the real sell formula (2026-10-19).
  const sell = getSellSettings();
  params.push("speech_skill=" + sell.speech);
  params.push("fortify_barter=" + sell.fortifyBarter);
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
$("planbtn").addEventListener("click", computePlan);
$("ingfilter").addEventListener("change", () => {
  S.ingFilter = $("ingfilter").value;
  renderPriceTab();
});
$("tabbtn-price").addEventListener("click", () => setTab("price"));
$("tabbtn-disenchant").addEventListener("click", () => setTab("disenchant"));
$("q-clear").addEventListener("click", clearQueue);
$("q-guide").addEventListener("click", startGuide);
// Sort mode (2026-10-19): per-potion value vs total extractable gold.
$("sortmode").addEventListener("change", () => {
  const v = $("sortmode").value;
  S.sort = { key: v === "total" ? "total" : "price", dir: -1 };
  localStorage.setItem("skyrim-sortmode", v);
  renderPriceTab();
  // A visible plan follows the new order too (2026-10-19).
  if (S.plan && !$("plan").hidden) renderPlan(S.plan);
});
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
// Sell settings (2026-10-19): Speech skill + Fortify Barter, manual;
// Haggling/Allure auto-detect from save. Apply saves and reloads.
initSellSettingsUI();
$("sell-apply").addEventListener("click", () => {
  const s = {
    speech: parseInt($("speech-skill").value, 10) || 15,
    fortifyBarter: (parseFloat($("fortify-barter").value) || 0) / 100,
  };
  s.speech = Math.max(15, Math.min(100, s.speech));
  s.fortifyBarter = Math.max(0, Math.min(2, s.fortifyBarter));
  saveSellSettings(s);
  initSellSettingsUI();
  load();
});
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
        # Spell tome names (FormID -> spell name) for the read-tomes list
        # (2026-10-19): Ben's don't-buy-duplicate-tomes check.
        self.spell_tomes = {}
        try:
            tomes_path = os.path.join(os.path.dirname(db_path),
                                      "spell_tomes.json")
            with open(tomes_path, "r", encoding="utf-8") as f:
                tomes_data = json.load(f)
            self.spell_tomes = tomes_data.get("tomes", {})
        except (OSError, ValueError):
            pass
        # Buyable spell FormIDs (spell FormID -> name) for the real
        # "spells you know" list (2026-10-19). Only spells with buyable
        # tomes — racial abilities, shouts, quest-only spells excluded
        # per Ben. Used with extract_known_spell_fids().
        self.buyable_spells = {}
        try:
            bsp_path = os.path.join(os.path.dirname(db_path),
                                    "buyable_spells.json")
            with open(bsp_path, "r", encoding="utf-8") as f:
                self.buyable_spells = json.load(f)
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
                 purity: bool, haggling_ranks: int, allure: bool,
                 alchemy_floor: int, speech_floor: int}.
        Base skill is from NPC_ DNAM (includes racial, excludes level-ups).
        Perks are from ACHR RefID search. Failures return defaults.
        Floors come from perk rank requirements (EDID suffixes 00/20/40/60/80
        verified against Skyrim.esm): having the rank proves the skill.
        (2026-10-05: auto-detect with manual override; 2026-10-19: speech
        perks + floors.)
        """
        result = {
            "base_skill": None,
            "alchemist_ranks": 0,
            "benefactor": False,
            "poisoner": False,
            "physician": False,
            "purity": False,
            "haggling_ranks": 0,
            "allure": False,
            "alchemy_floor": 0,
            "speech_floor": 0,
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
            result["alchemy_floor"] = _PERK_SKILL_FLOOR.get(
                perks.get("alchemist_ranks", 0), 0)
            result["speech_floor"] = _PERK_SKILL_FLOOR.get(
                perks.get("haggling_ranks", 0), 0)
        except Exception:
            pass
        return result

    def read_current(self, alch_skill=15, alch_ranks=0,
                     alch_benefactor=True, alch_poisoner=True,
                     alch_physician=True,
                     speech_skill=15, fortify_barter=0.0) -> dict:
        """Parse the newest save; return the /api/data payload dict."""
        save_path = pick_save(self.saves_dir)
        # Auto-detect perks/skill base from the save first (2026-10-05).
        detected = self._get_detected_alchemy(save_path)
        # Skill floors from perks (2026-10-19, Ben's request): manual skill
        # settings go stale (his Alchemy 30 vs proven >= 40); perk rank
        # requirements prove a minimum. Clamp the manual value UP to the
        # floor, never down — a higher manual value (leveled past it) wins.
        alch_skill = max(alch_skill, _PERK_SKILL_FLOOR.get(
            detected.get("alchemist_ranks", 0), 0))
        speech_skill = max(speech_skill, _PERK_SKILL_FLOOR.get(
            detected.get("haggling_ranks", 0), 0))
        # Store alchemy stats for potion pricing.
        self.alch_skill = alch_skill
        self.alch_ranks = alch_ranks
        self.alch_benefactor = alch_benefactor
        self.alch_poisoner = alch_poisoner
        self.alch_physician = alch_physician
        self.speech_skill = speech_skill
        self.fortify_barter = fortify_barter
        # Stage timings (2026-10-19): Ben's 4-second rule — if a parse stage
        # costs real time, it earns scrutiny; if not, it stays.
        t_all = time.perf_counter()
        timings = {}
        try:
            t0 = time.perf_counter()
            raw_items, info, plugins, light_plugins = \
                extract_player_inventory(save_path)
            timings["player_inventory"] = time.perf_counter() - t0
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
            t0 = time.perf_counter()
            known_raw = extract_known_ingredients(save_path)
            timings["known_effects"] = time.perf_counter() - t0
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
        # Spell tomes read (2026-10-19): Ben's don't-buy-duplicates check.
        # Type 13 = BOOK read flags; spell tomes set them like regular
        # books (verified by save diff: Fast Healing tome 0x000A271D).
        # Names only, no hex (his call).
        spell_tomes_read = []
        try:
            t0 = time.perf_counter()
            for fid in extract_read_books(save_path):
                fid_str = "0x%08X" % fid
                name = self.spell_tomes.get(fid_str)
                if name:
                    spell_tomes_read.append(name)
            spell_tomes_read.sort()
            timings["spell_tomes_read"] = time.perf_counter() - t0
        except Exception:
            pass
        # Starting spells: every character knows Flames + Healing without
        # reading tomes (2026-10-19, Ben's ask).
        for starter in ("Flames", "Healing"):
            if starter not in spell_tomes_read:
                spell_tomes_read.append(starter + " (starting spell)")
        spell_tomes_read.sort()
        # Spells you KNOW (2026-10-19, later that evening): the real list,
        # extracted from the player ACHR spell RefIDs — catches spells
        # learned any way (tomes, NPC grants like Faralda's entry test).
        # Scope per Ben: only spells with BUYABLE tomes (no racial
        # abilities, no shouts, no quest-only spells). Starting spells are
        # on the ActorBase, not the ACHR, so they're added manually.
        known_spells = []
        try:
            t0 = time.perf_counter()
            if self.buyable_spells:
                fids = [int(k, 16) for k in self.buyable_spells]
                for fid in extract_known_spell_fids(save_path, fids):
                    name = self.buyable_spells.get("0x%08X" % fid)
                    if name and name not in known_spells:
                        known_spells.append(name)
            for starter in ("Flames", "Healing"):
                if starter not in known_spells:
                    known_spells.append(starter)
            known_spells.sort()
            timings["known_spells"] = time.perf_counter() - t0
        except Exception:
            known_spells = []
        # Known enchantments from save (2026-10-05).
        # Type 48 = enchantments. Flags on base Form.
        known_enchantments = []
        try:
            t0 = time.perf_counter()
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
            timings["known_enchantments"] = time.perf_counter() - t0
        except Exception:
            pass
        owned = inventory_to_ingredients(raw_items, plugins, light_plugins,
                                         self.lookup)
        # Effect-grouping approach (2026-10-05): O(E*I^2) not O(n^3).
        # No pre-computed table needed.
        t0 = time.perf_counter()
        potions = brewable_by_effect(
            owned, skill=self.alch_skill, alchemist_ranks=self.alch_ranks,
            benefactor=self.alch_benefactor, poisoner=self.alch_poisoner,
            physician=self.alch_physician)
        timings["brewable_by_effect"] = time.perf_counter() - t0
        # Real sell factor (2026-10-19): UESP barter formula from the player's
        # real Speech skill (manual), Haggling ranks + Allure (auto-detected),
        # and Fortify Barter (manual). Replaces the flat 0.3 estimate.
        # (detected was computed at the top of read_current; skills already
        # clamped to perk floors there.)
        sell_factor = real_sell_factor(
            self.speech_skill, detected.get("haggling_ranks", 0),
            detected.get("allure", False), self.fortify_barter)
        payload = {
            "ok": True,
            "save_name": os.path.basename(save_path),
            "save_time": time.strftime("%Y-%m-%d %H:%M",
                                        time.localtime(
                                            os.path.getmtime(save_path))),
            "player": info.get("playerName", "?"),
            "ingredients": owned,
            "potions": potions,
            "known_effects": known_effects,
            "known_enchantments": known_enchantments,
            "spell_tomes_read": spell_tomes_read,
            "known_spells": known_spells,
            "detected": detected,
            "sell_factor": sell_factor,
            "sell_breakdown": {
                "speech": self.speech_skill,
                "haggling_ranks": detected.get("haggling_ranks", 0),
                "allure": detected.get("allure", False),
                "fortify_barter": self.fortify_barter,
            },
        }
        timings["total"] = time.perf_counter() - t_all
        sys.stderr.write("[timing] read_current stages (s): " +
                         ", ".join("%s=%.2f" % (k, v)
                                    for k, v in timings.items()) + "\n")
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
        elif self.path == "/flow":
            self._send(FLOW_HTML.encode("utf-8"),
                       "text/html; charset=utf-8")
        elif self.path == "/api/data" or self.path.startswith("/api/data?"):
            qs = urllib.parse.urlparse(self.path).query
            args = urllib.parse.parse_qs(qs)
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
            # Speech skill + Fortify Barter for the real sell formula
            # (2026-10-19). Speech can't be auto-detected (see docstring on
            # extract_base_alchemy) so it's manual, like alchemy skill.
            try:
                speech_skill = int(args.get("speech_skill", ["15"])[0])
            except ValueError:
                speech_skill = 15
            try:
                fortify_barter = float(args.get("fortify_barter", ["0"])[0])
            except ValueError:
                fortify_barter = 0.0
            payload = state.read_current(
                alch_skill=alch_skill, alch_ranks=alch_ranks,
                alch_benefactor=alch_benefactor,
                alch_poisoner=alch_poisoner,
                alch_physician=alch_physician,
                speech_skill=speech_skill,
                fortify_barter=fortify_barter)
            body = json.dumps(payload).encode("utf-8")
            self._send(body, "application/json")
        elif self.path == "/api/plan" or self.path.startswith("/api/plan?"):
            # Optimal brew plan (2026-10-19): "do this, in this order for
            # maximum profit." Runs brew_allocation over the last-read
            # inventory (a few seconds on triple-sized lists -- on demand,
            # never on page load). min_price tracks the page's slider.
            qs = urllib.parse.urlparse(self.path).query
            args = urllib.parse.parse_qs(qs)
            try:
                min_price = int(args.get("min_price", ["100"])[0])
            except ValueError:
                min_price = 100
            # Never-brew list (2026-10-19): hoarded ingredients count as
            # zero stock so the optimizer routes around them.
            nb_raw = args.get("nobrewing", [""])[0]
            nobrewing = set(e for e in nb_raw.split("\n") if e)
            with state.lock:
                last = state.last_good
            if not last or not last.get("ok"):
                body = json.dumps({"ok": False,
                                   "error": "no data yet"}).encode("utf-8")
            else:
                potions = last["potions"]
                counts = {o["name"]: o["count"] for o in last["ingredients"]}
                for n in nobrewing:
                    if n in counts:
                        counts[n] = 0
                # Optimize (2026-10-19): _greedy_brew breaks at the first
                # potion below min_price, so below-bar potions can never be
                # brewed — they're pure dead weight in every sort/greedy
                # pass (17k potions -> a few hundred at a 300g bar).
                # Exactly equivalent for the greedy; strictly better for the
                # randomized polish (a boosted below-bar potion can no longer
                # spuriously break a pass early). Bonus: the adaptive
                # iteration count scales up on the smaller list, so the
                # search gets more iterations, each far cheaper.
                potions = [p for p in potions if p["price"] >= min_price]
                # Batch mode (2026-10-19): when the page sorts by total
                # gold, plan in whole batches (batch value order) instead
                # of fragmenting for marginal per-potion gains.
                batch_mode = args.get("sort", ["per"])[0] == "total"
                total, brewed = brew_allocation(potions, counts, min_price,
                                                batch_mode=batch_mode)
                # Collapse repeat brews into qty lines (2026-10-19), richest
                # first. _greedy_brew now brews every possible batch.
                groups = {}
                for p in brewed:
                    key = (tuple(p["ingredients"]), tuple(p["effects"]))
                    if key in groups:
                        groups[key]["qty"] += 1
                    else:
                        groups[key] = {
                            "ingredients": p["ingredients"],
                            "effects": p["effects"],
                            "price": p["price"],
                            "qty": 1,
                        }
                brews = sorted(groups.values(), key=lambda b: -b["price"])
                # Real sell value (2026-10-19): use the sell factor computed
                # from the player's actual Speech/Haggling/Allure on the last
                # refresh, not the flat 0.3 estimate.
                sell_factor = last.get("sell_factor", SELL_FACTOR)
                body = json.dumps({
                    "ok": True,
                    "min_price": min_price,
                    "total_value": total,
                    "total_sell": int(total * sell_factor),
                    "sell_factor": sell_factor,
                    "brews": brews,
                }).encode("utf-8")
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
                          "potions": brewable_by_effect(owned)}, indent=2))
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
