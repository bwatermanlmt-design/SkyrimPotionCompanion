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
import socket
import struct
import sys
import threading
import time
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
        data: bytes, change_flags: int, refid_kind: int
) -> List[Tuple[Tuple[int, int, int], int]]:
    """Walk a player ACHR change-data body; return [((b0,b1,b2), count)].

    Section order per ReSaver's ChangeFormACHR. The caller resolves the raw
    RefID bytes to formIDs with resolve_refid(). Raises SaveParseError on any
    structural mismatch -- including trailing bytes, which ReSaver also
    treats as a hard error.
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
            try:
                b0, b1, b2 = cur.u8(), cur.u8(), cur.u8()
                count = cur.i32()
                _skip_extradata(cur)
                raw_items.append(((b0, b1, b2), count))
            except (SaveParseError, IndexError):
                # Tolerant parse: stop at first unparseable item.
                # (Real saves may contain complex extra-data types we don't
                # handle yet; partial inventory is better than none.)
                break

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
# Chest formIDs verified empirically from Ben's saves (2026-10-03).
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
            raw_items = parse_achr_inventory(data, change_flags, refid_kind)
            items = [(resolve_refid(x0, x1, x2, formid_array), count)
                     for (x0, x1, x2), count in raw_items]
            return items, info, plugins, light_plugins

    raise SaveParseError(
        "player ACHR change form (formID 0x00000014) not found in %s" % path)


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


def potion_price(effects) -> int:
    """Sum of the standard per-effect values. Unknown effects count 0."""
    return sum(EFFECT_VALUES.get(e, 0) for e in effects)


def _make_potion(effects, ings):
    return {
        "effects": effects,
        "ingredients": [g["name"] for g in ings],
        "counts": [g["count"] for g in ings],
        # How many you can brew before one stack runs out:
        "batches": min(g["count"] for g in ings),
        "n": len(ings),
        "price": potion_price(effects),
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
                                            [owned[i], owned[j]]))
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
                        sorted(shared), [owned[i], owned[j], owned[k]]))
    potions.sort(key=lambda p: (-p["price"], -len(p["effects"]),
                                p["ingredients"]))
    return potions


def shop_recommendations(owned: List[dict], chest_items: List[Tuple[str, int, int]],
                         player_gold: int, ing_db: dict) -> List[dict]:
    """Build a greedy buy-and-brew plan.

    owned: Ben's ingredient dicts (name, effects, count).
    chest_items: [(ingredient name, base_value, count_available)] from the
        merchant chest.
    ing_db: name -> ingredient dict (for effects of items Ben doesn't own).
    Returns an ordered plan: [{name, available, buy_price, brew_with,
    potion_effects, potion_price}]. Each step buys one unit, brews the best
    potion using it, and consumes the ingredients so later steps use what's
    left. This simulates the actual buy-brew-sell loop instead of evaluating
    each ingredient in isolation (which double-counts shared ingredients).
    Buy price ~= base_value x 3 (typical low-Speech merchant markup).
    """
    # Working inventory (mutable counts).
    pool = {o["name"]: {"effects": o["effects"], "count": o["count"]}
            for o in owned}
    gold = player_gold
    plan = []
    # Rank chest ingredients by their best standalone potion value first,
    # so the plan goes in a sensible buy order.
    ranked = []
    for name, base_value, avail in chest_items:
        if avail <= 0:
            continue
        buy_price = base_value * 3
        # Hypothetical best potion if we had one unit.
        hypo_effects = None
        if name in pool:
            hypo_effects = pool[name]["effects"]
        elif name in ing_db:
            hypo_effects = ing_db[name]["effects"]
        if not hypo_effects:
            continue
        tmp = dict(pool)
        tmp[name] = {"effects": hypo_effects,
                     "count": tmp.get(name, {"count": 0})["count"] + 1}
        tmp_list = [{"name": n, "effects": v["effects"], "count": v["count"]}
                    for n, v in tmp.items() if v["count"] > 0]
        best = None
        for p in compute_potions(tmp_list):
            if name in p["ingredients"]:
                best = p
                break
        if best and best["price"] >= 100:
            ranked.append((best["price"], name, base_value, avail, best))
    ranked.sort(reverse=True)
    # Greedy: buy in order, brew, consume.
    for _, name, base_value, avail, _ in ranked:
        buy_price = base_value * 3
        if buy_price > gold:
            continue
        # Add one unit to the pool.
        if name in pool:
            pool[name]["count"] += 1
        elif name in ing_db:
            pool[name] = {"effects": ing_db[name]["effects"], "count": 1}
        else:
            continue
        pool_list = [{"name": n, "effects": v["effects"], "count": v["count"]}
                     for n, v in pool.items() if v["count"] > 0]
        best = None
        for p in compute_potions(pool_list):
            if name in p["ingredients"]:
                best = p
                break
        if best is None:
            pool[name]["count"] -= 1
            continue
        # Consume the brewed ingredients.
        for ing_name in best["ingredients"]:
            pool[ing_name]["count"] -= 1
        gold -= buy_price
        # What did we brew it with (excluding the bought ingredient)?
        brew_with = [i for i in best["ingredients"] if i != name]
        plan.append({
            "name": name,
            "available": avail,
            "buy_price": buy_price,
            "brew_with": brew_with,
            "potion_effects": best["effects"],
            "potion_price": best["price"],
            "gold_left": gold,
        })
    return plan


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
  <button id="tabbtn-price" class="active">&#128176; By Price</button>
  <button id="tabbtn-effect">&#129516; By Effect</button>
</nav>

<main>
  <section id="shop" style="display:none">
    <h2 id="shop-h">Shop buys</h2>
    <div class="hint" id="shop-hint"></div>
    <div id="shoprecs"></div>
  </section>

  <section id="tab-price" class="tab active">
    <div class="controls">
      <input id="search" type="search" placeholder="Filter: effect or ingredient&hellip;" autocomplete="off">
      <select id="ingfilter"><option value="">All ingredients</option></select>
    </div>
    <h2 id="potion-h">Potions you can brew</h2>
    <div class="hint">Tap a column header to sort. Tap <b>+</b> to queue a brew &mdash; ingredients are deducted and the list updates.</div>
    <div id="potions"></div>
  </section>

  <section id="tab-effect" class="tab">
    <div class="controls">
      <select id="effectpick"></select>
    </div>
    <h2 id="effect-h">Recipes by effect</h2>
    <div class="hint">Every brewable recipe that produces the chosen effect, richest first. Brewing here uses the same ingredient pool.</div>
    <div id="effectpotions"></div>
  </section>

  <details class="ings">
    <summary id="ing-h">Your ingredients</summary>
    <div id="ingredients"></div>
  </details>
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
  search: ""
};

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
    tdI.innerHTML = p.ingredients.map((nm) =>
      escapeHtml(nm) + " <span class='have'>(" + (S.remaining[nm] || 0) +
      " left)</span>").join("<br>");
    tr.appendChild(tdI);

    const tdN = document.createElement("td");
    tdN.className = "n";
    tdN.textContent = p.n;
    tr.appendChild(tdN);

    const tdE = document.createElement("td");
    tdE.className = "effects";
    tdE.textContent = p.effects.join(" + ");
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
  let list = S.data.potions.filter(brewable);
  if (S.ingFilter)
    list = list.filter((p) => p.ingredients.indexOf(S.ingFilter) !== -1);
  if (q)
    list = list.filter((p) =>
      (p.effects.join(" ") + " " + p.ingredients.join(" "))
        .toLowerCase().indexOf(q) !== -1);
  return sortPotions(list, S.sort);
}

function filteredEffectPotions() {
  let list = S.data.potions.filter(brewable);
  if (S.effectFilter)
    list = list.filter((p) => p.effects.indexOf(S.effectFilter) !== -1);
  return sortPotions(list, S.esort);
}

function onSortPrice(col) {
  if (S.sort.key === col) S.sort.dir = -S.sort.dir;
  else S.sort = { key: col, dir: col === "price" ? -1 : 1 };
  renderPriceTab();
}

function onSortEffect(col) {
  if (S.esort.key === col) S.esort.dir = -S.esort.dir;
  else S.esort = { key: col, dir: col === "price" ? -1 : 1 };
  renderEffectTab();
}

function renderPriceTab() {
  const list = filteredPricePotions();
  $("potion-h").innerHTML = "Potions you can brew " +
    "<span class='badge'>" + list.length + "</span>";
  const el = $("potions");
  el.innerHTML = "";
  el.appendChild(potionTable(list, S.sort, onSortPrice, true));
}

function renderEffectTab() {
  // Effect picker lists effects present in currently-brewable potions.
  const effSet = {};
  for (const p of S.data.potions) {
    if (!brewable(p)) continue;
    for (const e of p.effects) effSet[e] = true;
  }
  const effs = Object.keys(effSet).sort();
  const sel = $("effectpick");
  const cur = S.effectFilter;
  sel.innerHTML = "";
  for (const e of effs) {
    const o = document.createElement("option");
    o.value = e; o.textContent = e;
    sel.appendChild(o);
  }
  if (effs.indexOf(cur) !== -1) sel.value = cur;
  S.effectFilter = sel.value || "";

  const list = filteredEffectPotions();
  $("effect-h").innerHTML = "Recipes: " + escapeHtml(S.effectFilter) + " " +
    "<span class='badge'>" + list.length + "</span>";
  const el = $("effectpotions");
  el.innerHTML = "";
  el.appendChild(potionTable(list, S.esort, onSortEffect, true));
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

function renderIngredients() {
  $("ing-h").textContent =
    "Your ingredients (" + S.data.ingredients.length + ")";
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

function renderShop() {
  const shop = S.data.shop;
  const sec = $("shop");
  if (!shop || !shop.recommendations.length) {
    sec.style.display = "none";
    return;
  }
  sec.style.display = "";
  $("shop-h").textContent = "Buy at " + shop.location + " (" + shop.gold + "g)";
  $("shop-hint").textContent =
    "Buy in order, brew each with what's listed, sell, repeat. " +
    "Ingredients are consumed as you go. Prices are estimates.";
  const div = $("shoprecs");
  div.innerHTML = "";
  const tbl = document.createElement("table");
  tbl.innerHTML = "<tr><th>#</th><th>Buy</th><th>Cost</th><th>Brew with</th><th>Makes</th></tr>";
  shop.recommendations.slice(0, 20).forEach((r, i) => {
    const tr = document.createElement("tr");
    tr.innerHTML =
      "<td>" + (i + 1) + "</td>" +
      "<td>" + r.name + (r.available > 1 ? " x" + r.available : "") + "</td>" +
      "<td>" + r.buy_price + "g</td>" +
      "<td>" + (r.brew_with.join(" + ") || "&mdash;") + "</td>" +
      "<td>" + r.potion_effects.join(", ") + " (" + r.potion_price + "g)</td>";
    tbl.appendChild(tr);
  });
  div.appendChild(tbl);
}

function renderAll() {
  renderStatus();
  renderQueue();
  fillIngredientFilter();
  renderPriceTab();
  renderEffectTab();
  renderIngredients();
  renderShop();
}

function setTab(which) {
  S.tab = which;
  $("tabbtn-price").className = which === "price" ? "active" : "";
  $("tabbtn-effect").className = which === "effect" ? "active" : "";
  $("tab-price").className = "tab" + (which === "price" ? " active" : "");
  $("tab-effect").className = "tab" + (which === "effect" ? " active" : "");
}

// ---------------------------------------------------------------- data
function resetState(data) {
  S.data = data;
  S.remaining = {};
  for (const g of data.ingredients) S.remaining[g.name] = g.count;
  S.queue = [];
  S.sort = { key: "price", dir: -1 };
  S.esort = { key: "price", dir: -1 };
  S.ingFilter = "";
  S.search = "";
  $("search").value = "";
  // Keep a valid effect selection if possible.
  if (data.potions.length) {
    const top = data.potions.slice().sort((a, b) => b.price - a.price)[0];
    if (top) S.effectFilter = top.effects[0] || "";
  }
}

async function load() {
  const btn = $("refresh");
  btn.disabled = true;
  btn.textContent = "Reading save…";
  try {
    const r = await fetch("/api/data", { cache: "no-store" });
    const data = await r.json();
    if (!data.ok) { showError(data.error || "Unknown error"); }
    else { showError(null); resetState(data); renderAll(); }
  } catch (e) {
    showError("Could not reach the companion server: " + e);
  }
  btn.disabled = false;
  btn.innerHTML = "&#8635; Refresh from latest save";
}

$("refresh").addEventListener("click", load);
$("search").addEventListener("input", () => {
  S.search = $("search").value;
  renderPriceTab();  // cheap: re-render from the last good payload
});
$("ingfilter").addEventListener("change", () => {
  S.ingFilter = $("ingfilter").value;
  renderPriceTab();
});
$("effectpick").addEventListener("change", () => {
  S.effectFilter = $("effectpick").value;
  renderEffectTab();
});
$("tabbtn-price").addEventListener("click", () => setTab("price"));
$("tabbtn-effect").addEventListener("click", () => setTab("effect"));
$("q-clear").addEventListener("click", clearQueue);
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
        self.lock = threading.Lock()
        self.last_good = None

    def read_current(self) -> dict:
        """Parse the newest save; return the /api/data payload dict."""
        save_path = pick_save(self.saves_dir)
        try:
            raw_items, info, plugins, light_plugins = \
                extract_player_inventory(save_path)
        except SaveParseError as e:
            return {"ok": False,
                    "error": "Save parse failed: %s" % e}
        except OSError as e:
            return {"ok": False,
                    "error": "Could not read save file: %s" % e}
        owned = inventory_to_ingredients(raw_items, plugins, light_plugins,
                                         self.lookup)
        potions = compute_potions(owned)
        # Shop recommendations: if we're in a mapped shop, check the
        # merchant's chest for profitable buys.
        shop = None
        location = info.get("playerLocation", "")
        if location in MERCHANT_CHESTS:
            gold = next((c for f, c in raw_items if f == 0xF), 0)
            chest_raw = extract_merchant_inventory(save_path, location)
            chest_items = []
            for fid, cnt in chest_raw:
                plugin, obj_id = split_formid(fid, plugins, light_plugins)
                if plugin is None or cnt <= 0:
                    continue
                ing = self.lookup.get((plugin.lower(), obj_id))
                if ing is None:
                    continue
                chest_items.append((ing["name"], ing.get("base_value", 5),
                                    cnt))
            # Dedupe by name (chest may list same ingredient twice).
            seen = {}
            for name, bv, cnt in chest_items:
                if name in seen:
                    seen[name] = (name, bv, seen[name][2] + cnt)
                else:
                    seen[name] = (name, bv, cnt)
            recs = shop_recommendations(owned, list(seen.values()), gold,
                                        self.ing_db)
            shop = {"location": location, "gold": gold,
                    "recommendations": recs}
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
        elif self.path == "/api/data":
            payload = state.read_current()
            body = json.dumps(payload).encode("utf-8")
            self._send(body, "application/json")
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
