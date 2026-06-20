#!/usr/bin/env python3
"""Aggregate per-cell stdlib introspection dumps into a cross-platform union.

Takes a directory of per-cell JSONL files produced by stdlib_introspect.py across an
OS x Python-version matrix and builds the *union* of the stdlib API surface -- the
thing no single interpreter can see on its own (winreg only exists on Windows,
freshly-added APIs only exist on the newest minor, modules dropped between releases
only exist on the oldest).

Inputs are named stdlib_api_<os>_py<ver>.jsonl by the workflow; the OS label and Python
version are parsed back out of each filename.

Outputs:
  * stdlib_api_union.jsonl -- every qualname once, each record annotated with the set
    of cells (os-family + python version) it appeared in, plus added_in/removed_in
    derived from the OS-collapsed version presence.
  * a Markdown report to $GITHUB_STEP_SUMMARY (or --md-summary PATH): union size,
    per-cell counts, platform-exclusive API counts, and the per-adjacent-minor
    added/removed deltas (OS-collapsed) across the matrix.

Stdlib only, no third-party deps. All file I/O is UTF-8.
    python merge_summary.py CELLS_DIR [-o stdlib_api_union.jsonl] [--md-summary PATH]
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from collections import defaultdict

# Filenames look like stdlib_api_ubuntu-latest_py3.14.jsonl. The os has no "_py" and
# the version no underscore, so a non-greedy split on the single "_py" is unambiguous.
CELL_PATTERN = re.compile(r"^stdlib_api_(?P<os>.+?)_py(?P<ver>[^_]+)\.jsonl$")
FAMILY_ORDER = {"linux": 0, "macos": 1, "windows": 2}


def os_family(label):
    lowered = label.lower()
    if lowered.startswith(("ubuntu", "linux")):
        return "linux"
    if lowered.startswith(("macos", "mac", "darwin")):
        return "macos"
    if lowered.startswith(("windows", "win")):
        return "windows"
    return lowered


def version_key(version):
    """'3.14' -> (3, 14); tolerant of '3.15.0a1' and junk."""
    numbers = re.findall(r"\d+", version)
    return tuple(int(number) for number in numbers[:2]) if numbers else (0,)


def format_version(version_tuple):
    return ".".join(str(part) for part in version_tuple)


def version_span(present_keys, matrix_keys):
    """(added_in, removed_in) for one entity from its OS-collapsed version set.

    added_in is floored at the matrix minimum: present at the floor means it was
    added then or in some earlier, unobserved release, recorded as '<=X.Y'.
    removed_in is precise -- the first matrix minor where it disappears.
    """
    floor = matrix_keys[0]
    added_in = "<=" + format_version(floor) if floor in present_keys else format_version(min(present_keys))
    removed_in = None
    for earlier, later in zip(matrix_keys, matrix_keys[1:]):
        if earlier in present_keys and later not in present_keys:
            removed_in = format_version(later)
            break
    return added_in, removed_in


class Cell:
    def __init__(self, path, os_label, version):
        self.path = path
        self.os_label = os_label
        self.family = os_family(os_label)
        self.version = version
        self.version_key = version_key(version)
        self.cell_id = f"{self.family}-py{version}"
        self.records = {}  # qualname -> record (last wins within a cell)
        self.malformed = 0

    def load(self):
        with open(self.path, encoding="utf-8") as source_file:
            for line in source_file:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    self.records[record["qualname"]] = record
                except json.JSONDecodeError, KeyError, TypeError:
                    self.malformed += 1  # tolerate a half-written dump from a crashed cell


def discover(cells_dir):
    cells = []
    for path in sorted(glob.glob(os.path.join(cells_dir, "stdlib_api_*_py*.jsonl"))):
        match = CELL_PATTERN.match(os.path.basename(path))
        if not match:
            print(f"  ! unrecognized file name, skipping: {path}", file=sys.stderr)
            continue
        cell = Cell(path, match["os"], match["ver"])
        cell.load()
        cells.append(cell)
    return cells


def aggregate(cells):
    present_cells = defaultdict(set)  # qualname -> {cell_id}
    present_families = defaultdict(set)  # qualname -> {family}
    present_version_keys = defaultdict(set)  # qualname -> {version_key}
    for cell in cells:
        for qualname in cell.records:
            present_cells[qualname].add(cell.cell_id)
            present_families[qualname].add(cell.family)
            present_version_keys[qualname].add(cell.version_key)

    union = sorted(present_cells)

    # Walk cells oldest -> newest and let the newer cell overwrite, so the union's base
    # record carries the newest minor's signature/docstring.
    union_records = {}
    for cell in sorted(cells, key=lambda cell: (cell.version_key, FAMILY_ORDER.get(cell.family, 99))):
        for qualname, record in cell.records.items():
            union_records[qualname] = record
    matrix_keys = sorted({cell.version_key for cell in cells})
    for qualname in union:
        record = dict(union_records[qualname])
        record["cells"] = sorted(present_cells[qualname])
        record["n_cells"] = len(record["cells"])
        record["added_in"], record["removed_in"] = version_span(present_version_keys[qualname], matrix_keys)
        union_records[qualname] = record

    return union, union_records, present_families, present_version_keys


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("cells_dir", help="directory of stdlib_api_<os>_py<ver>.jsonl files")
    parser.add_argument("-o", "--output", default="stdlib_api_union.jsonl")
    parser.add_argument(
        "--md-summary", metavar="PATH", help="write the Markdown report here (defaults to $GITHUB_STEP_SUMMARY)",
    )
    args = parser.parse_args()

    cells = discover(args.cells_dir)
    union, union_records, present_families, present_version_keys = aggregate(cells)

    # Always write the union file (even empty) so the upload step has an artifact;
    # newline="\n" so the artifact is byte-identical regardless of which runner ran us.
    with open(args.output, "w", encoding="utf-8", newline="\n") as out_file:
        out_file.writelines(json.dumps(union_records[qualname]) + "\n" for qualname in union)

    # Platform-exclusive: present on exactly one OS family across the matrix.
    families = sorted({cell.family for cell in cells}, key=lambda family: FAMILY_ORDER.get(family, 99))
    exclusive = {family: [] for family in families}
    for qualname in union:
        families_present = present_families[qualname]
        if len(families_present) == 1:
            exclusive.setdefault(next(iter(families_present)), []).append(qualname)

    # Per-adjacent-minor deltas, OS-collapsed (present in a minor == present in any OS cell).
    version_keys = sorted({cell.version_key for cell in cells})
    transitions = []
    for earlier, later in zip(version_keys, version_keys[1:]):
        added = [
            qualname
            for qualname in union
            if later in present_version_keys[qualname] and earlier not in present_version_keys[qualname]
        ]
        removed = [
            qualname
            for qualname in union
            if earlier in present_version_keys[qualname] and later not in present_version_keys[qualname]
        ]
        transitions.append((earlier, later, added, removed))

    report(cells, union, exclusive, families, transitions, version_keys, args)


def report(cells, union, exclusive, families, transitions, version_keys, args):
    rows = sorted(cells, key=lambda cell: (cell.version_key, FAMILY_ORDER.get(cell.family, 99)))

    lines = ["# stdlib introspection — cross-platform union", ""]
    if not cells:
        lines += [
            "> **No cell artifacts were found.** Every matrix cell failed to produce a dump, or the download step pulled nothing.",
            "",
        ]
    else:
        versions = sorted({format_version(cell.version_key) for cell in cells}, key=version_key)
        lines += [
            f"Aggregated **{len(cells)}** cells — families: {', '.join(families)}; Python: {', '.join(versions)}.",
            "",
            f"**Union surface: {len(union)} unique qualnames.**",
            "",
        ]

        lines += ["## Per-cell entity counts", "", "| cell | os | python | entities |", "| --- | --- | --- | ---: |"]
        for cell in rows:
            note = f" ⚠️ {cell.malformed} bad lines" if cell.malformed else ""
            lines.append(f"| `{cell.cell_id}` | {cell.os_label} | {cell.version} | {len(cell.records)}{note} |")
        lines.append("")

        lines += [
            "## Platform-exclusive APIs",
            "",
            "Qualnames that appear on exactly one OS family across the whole matrix.",
            "",
            "| platform | exclusive qualnames |",
            "| --- | ---: |",
        ]
        for family in families:
            lines.append(f"| {family} | {len(exclusive.get(family, []))} |")
        lines.append("")

        if transitions:
            floor_label = format_version(version_keys[0])
            lines += [
                "## Per-version deltas (OS-collapsed, adjacent minors)",
                "",
                f"An entity is present in a minor if it appears in **any** OS cell for it. "
                f"`added_in` for entities already present in {floor_label} is recorded as "
                f"`<={floor_label}` — the matrix floor bounds it.",
                "",
                "| transition | added | removed |",
                "| --- | ---: | ---: |",
            ]
            for earlier, later, added, removed in transitions:
                lines.append(f"| {format_version(earlier)} → {format_version(later)} | {len(added)} | {len(removed)} |")
            lines.append("")
        else:
            lines += [
                "## Per-version deltas",
                "",
                "_Need at least two Python minors in the matrix to compute deltas._",
                "",
            ]

    markdown_path = args.md_summary or os.environ.get("GITHUB_STEP_SUMMARY")
    if markdown_path:
        with open(markdown_path, "a", encoding="utf-8", newline="\n") as summary_file:
            summary_file.write("\n".join(lines) + "\n")

    print("\n=== union summary ========================================")
    print(f"aggregated {len(cells)} cells -> {len(union)} unique qualnames")
    for cell in rows:
        extra = f"  ({cell.malformed} malformed lines)" if cell.malformed else ""
        print(f"  {cell.cell_id:18s} {len(cell.records):6d} entities{extra}")
    for family in families:
        print(f"  {family}-exclusive APIs : {len(exclusive.get(family, []))}")
    for earlier, later, added, removed in transitions:
        print(f"  {format_version(earlier)} -> {format_version(later)}: +{len(added)} / -{len(removed)}")
    print(f"wrote {len(union)} records -> {args.output}")
    print("=" * 58)


if __name__ == "__main__":
    main()
