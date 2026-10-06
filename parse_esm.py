#!/usr/bin/env python3
"""Parse Skyrim.esm to extract FormID -> name mappings for ARMO, WEAP, ENCH, SPEL.

Runs on Ben's laptop (where Skyrim.esm lives). Streams the file, does not
load it all into memory.

Output: JSON { "0x0010FB7C": {"name": "...", "edid": "...", "type": "ENCH"}, ... }
"""

import struct
import zlib
import json
import sys
import os

ESM_PATH = "/home/ben/Games/Steam/steamapps/common/Skyrim Special Edition/Data/Skyrim.esm"
OUTPUT_PATH = "/tmp/esm_mappings.json"

TARGET_TYPES = {b'ARMO', b'WEAP', b'ENCH', b'SPEL'}
# For a quicker run, set ONLY_ENCH=1 in the environment to parse ENCH only.
if os.environ.get("ONLY_ENCH") == "1":
    TARGET_TYPES = {b'ENCH'}


def decode_string(data):
    """Decode a null-terminated string from subrecord data."""
    if b'\x00' in data:
        data = data[:data.index(b'\x00')]
    data = data.strip()
    if not data:
        return ""
    try:
        return data.decode('utf-8')
    except UnicodeDecodeError:
        return data.decode('cp1252', errors='replace')


def parse_subrecords(data, want):
    """Parse subrecords, returning {subtype: raw_bytes} for wanted types.

    Only captures the first occurrence of each wanted subtype.
    """
    result = {}
    pos = 0
    n = len(data)
    while pos + 6 <= n:
        sub_type = data[pos:pos + 4]
        sub_size = struct.unpack('<H', data[pos + 4:pos + 6])[0]
        pos += 6

        if sub_type == b'XXXX':
            # Extended-size subrecord: 2-byte size (=4), 4-byte real size,
            # 4-byte real subtype, then data.
            if pos + 2 > n:
                break
            # xxxx_inner = struct.unpack('<H', data[pos:pos + 2])[0]
            pos += 2
            if pos + 4 > n:
                break
            sub_size = struct.unpack('<I', data[pos:pos + 4])[0]
            pos += 4
            if pos + 4 > n:
                break
            sub_type = data[pos:pos + 4]
            pos += 4

        if pos + sub_size > n:
            break

        if sub_type in want and sub_type not in result:
            result[sub_type] = data[pos:pos + sub_size]
            if len(result) == len(want):
                break

        pos += sub_size

    return result


def parse_esm():
    mappings = {}
    counts = {t.decode('ascii'): 0 for t in TARGET_TYPES}

    with open(ESM_PATH, 'rb') as f:
        # --- TES4 header record ---
        header = f.read(24)
        if len(header) < 24 or header[0:4] != b'TES4':
            raise ValueError("Not a valid .esm (missing TES4 header)")
        tes4_size = struct.unpack('<I', header[4:8])[0]
        f.seek(tes4_size, 1)  # skip TES4 data

        # --- Walk GRUPs and records linearly ---
        # Stack of (grup_end_pos, is_target_group)
        stack = []
        records_seen = 0

        while True:
            pos = f.tell()
            while stack and pos >= stack[-1][0]:
                stack.pop()

            header = f.read(24)
            if len(header) < 24:
                break  # EOF

            rec_type = header[0:4]
            size = struct.unpack('<I', header[4:8])[0]

            if rec_type == b'GRUP':
                label = header[8:12]
                group_type = struct.unpack('<i', header[12:16])[0]
                end_pos = pos + size  # GRUP size includes its own header
                is_target = (group_type == 0 and label in TARGET_TYPES)
                stack.append((end_pos, is_target))
                continue

            # Regular record
            flags = struct.unpack('<I', header[8:12])[0]
            formid = struct.unpack('<I', header[12:16])[0]

            in_target = any(is_t for _, is_t in stack)
            is_target_type = rec_type in TARGET_TYPES

            if in_target or is_target_type:
                data = f.read(size)
                if len(data) < size:
                    break  # truncated

                # Decompress if needed (flag 0x00040000)
                if flags & 0x00040000:
                    if len(data) < 4:
                        continue
                    try:
                        data = zlib.decompress(data[4:])
                    except Exception:
                        continue

                subs = parse_subrecords(data, {b'EDID', b'FULL'})
                edid = decode_string(subs.get(b'EDID', b''))
                full = decode_string(subs.get(b'FULL', b''))

                fid_str = "0x%08X" % formid
                try:
                    type_str = rec_type.decode('ascii')
                except Exception:
                    type_str = repr(rec_type)
                mappings[fid_str] = {
                    "name": full,
                    "edid": edid,
                    "type": type_str,
                }
                if type_str in counts:
                    counts[type_str] += 1

                records_seen += 1
                if records_seen % 1000 == 0:
                    print("  ...parsed %d target records" % records_seen,
                          flush=True)
            else:
                # Skip record data
                f.seek(size, 1)

    print("Done. Counts by type:", flush=True)
    for t, c in sorted(counts.items()):
        print("  %s: %d" % (t, c), flush=True)
    print("Total mappings: %d" % len(mappings), flush=True)
    return mappings


def main():
    print("Parsing %s ..." % ESM_PATH, flush=True)
    print("Target types: %s" % sorted(t.decode('ascii') for t in TARGET_TYPES),
          flush=True)
    mappings = parse_esm()
    with open(OUTPUT_PATH, 'w', encoding='utf-8') as f:
        json.dump(mappings, f, ensure_ascii=False, indent=1)
    print("Wrote %s (%d bytes)" % (OUTPUT_PATH, os.path.getsize(OUTPUT_PATH)),
          flush=True)


if __name__ == "__main__":
    main()
