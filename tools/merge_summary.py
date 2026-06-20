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
    of cells (os-family + python version) it appeared in.
  * a Markdown report to $GITHUB_STEP_SUMMARY (or --md-summary PATH): union size,
    per-cell counts, platform-exclusive API counts, a Windows-only sample, and the
    added/removed deltas between the oldest and newest minor in the matrix.

Stdlib only, no third-party deps. All file I/O is UTF-8.
    python merge_summary.py CELLS_DIR [-o stdlib_api_union.jsonl] [--md-summary PATH]
"""
from __future__ import annotations
import sys, os, re, json, glob, argparse
from collections import defaultdict

# Filenames look like stdlib_api_ubuntu-latest_py3.14.jsonl. The os has no "_py" and
# the version no underscore, so a non-greedy split on the single "_py" is unambiguous.
CELL_RE = re.compile(r"^stdlib_api_(?P<os>.+?)_py(?P<ver>[^_]+)\.jsonl$")
FAM_ORDER = {"linux": 0, "macos": 1, "windows": 2}
SAMPLE = 25     # cap rows in the Markdown sample tables


def os_family(label):
    o = label.lower()
    if o.startswith(("ubuntu", "linux")):        return "linux"
    if o.startswith(("macos", "mac", "darwin")): return "macos"
    if o.startswith(("windows", "win")):         return "windows"
    return o

def ver_key(v):
    """'3.14' -> (3, 14); tolerant of '3.15.0a1' and junk."""
    nums = re.findall(r"\d+", v)
    return tuple(int(n) for n in nums[:2]) if nums else (0,)

def fmt_ver(vk):
    return ".".join(str(n) for n in vk)


class Cell:
    def __init__(self, path, os_label, ver):
        self.path = path
        self.os_label = os_label
        self.family = os_family(os_label)
        self.ver = ver
        self.vk = ver_key(ver)
        self.cell_id = f"{self.family}-py{ver}"
        self.records = {}       # qualname -> record (last wins within a cell)
        self.malformed = 0

    def load(self):
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    self.records[rec["qualname"]] = rec
                except (json.JSONDecodeError, KeyError, TypeError):
                    self.malformed += 1     # tolerate a half-written dump from a crashed cell


def discover(cells_dir):
    cells = []
    for path in sorted(glob.glob(os.path.join(cells_dir, "stdlib_api_*_py*.jsonl"))):
        m = CELL_RE.match(os.path.basename(path))
        if not m:
            print(f"  ! unrecognized file name, skipping: {path}", file=sys.stderr)
            continue
        c = Cell(path, m["os"], m["ver"])
        c.load()
        cells.append(c)
    return cells


def aggregate(cells):
    present_cells = defaultdict(set)    # qualname -> {cell_id}
    present_fam = defaultdict(set)      # qualname -> {family}
    present_vk = defaultdict(set)       # qualname -> {version_key}
    for c in cells:
        for qn in c.records:
            present_cells[qn].add(c.cell_id)
            present_fam[qn].add(c.family)
            present_vk[qn].add(c.vk)

    union = sorted(present_cells)

    # Walk cells oldest -> newest and let the newer cell overwrite, so the union's base
    # record carries the newest minor's signature/docstring.
    union_rec = {}
    for c in sorted(cells, key=lambda c: (c.vk, FAM_ORDER.get(c.family, 99))):
        for qn, rec in c.records.items():
            union_rec[qn] = rec
    for qn in union:
        rec = dict(union_rec[qn])
        rec["cells"] = sorted(present_cells[qn])
        rec["n_cells"] = len(rec["cells"])
        union_rec[qn] = rec

    return union, union_rec, present_fam, present_vk


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cells_dir", help="directory of stdlib_api_<os>_py<ver>.jsonl files")
    ap.add_argument("-o", "--output", default="stdlib_api_union.jsonl")
    ap.add_argument("--md-summary", metavar="PATH",
                    help="write the Markdown report here (defaults to $GITHUB_STEP_SUMMARY)")
    args = ap.parse_args()

    cells = discover(args.cells_dir)
    union, union_rec, present_fam, present_vk = aggregate(cells)

    # Always write the union file (even empty) so the upload step has an artifact;
    # newline="\n" so the artifact is byte-identical regardless of which runner ran us.
    with open(args.output, "w", encoding="utf-8", newline="\n") as f:
        for qn in union:
            f.write(json.dumps(union_rec[qn]) + "\n")

    # Platform-exclusive: present on exactly one OS family across the matrix.
    families = sorted({c.family for c in cells}, key=lambda fm: FAM_ORDER.get(fm, 99))
    exclusive = {fm: [] for fm in families}
    for qn in union:
        fams = present_fam[qn]
        if len(fams) == 1:
            exclusive.setdefault(next(iter(fams)), []).append(qn)

    # Deltas between the oldest and newest minor actually present.
    vks = sorted({c.vk for c in cells})
    added, removed, oldest, newest = [], [], None, None
    if len(vks) >= 2:
        oldest, newest = vks[0], vks[-1]
        added = [qn for qn in union if newest in present_vk[qn] and oldest not in present_vk[qn]]
        removed = [qn for qn in union if oldest in present_vk[qn] and newest not in present_vk[qn]]

    report(cells, union, union_rec, exclusive, families, added, removed, oldest, newest, args)


def _sample_table(lines, title, qualnames, union_rec):
    lines += [f"### {title} ({len(qualnames)})", "", "| qualname | kind |", "| --- | --- |"]
    for qn in sorted(qualnames)[:SAMPLE]:
        lines.append(f"| `{qn}` | {union_rec[qn].get('kind', '?')} |")
    if len(qualnames) > SAMPLE:
        lines.append(f"| … (+{len(qualnames) - SAMPLE} more) | |")
    lines.append("")


def report(cells, union, union_rec, exclusive, families, added, removed, oldest, newest, args):
    rows = sorted(cells, key=lambda c: (c.vk, FAM_ORDER.get(c.family, 99)))

    L = ["# stdlib introspection — cross-platform union", ""]
    if not cells:
        L += ["> **No cell artifacts were found.** Every matrix cell failed to produce a dump, or the download step pulled nothing.", ""]
    else:
        vers = sorted({fmt_ver(c.vk) for c in cells}, key=ver_key)
        L += [f"Aggregated **{len(cells)}** cells — families: {', '.join(families)}; Python: {', '.join(vers)}.", "",
              f"**Union surface: {len(union)} unique qualnames.**", ""]

        L += ["## Per-cell entity counts", "",
              "| cell | os | python | entities |", "| --- | --- | --- | ---: |"]
        for c in rows:
            note = f" ⚠️ {c.malformed} bad lines" if c.malformed else ""
            L.append(f"| `{c.cell_id}` | {c.os_label} | {c.ver} | {len(c.records)}{note} |")
        L.append("")

        L += ["## Platform-exclusive APIs", "",
              "Qualnames that appear on exactly one OS family across the whole matrix.", "",
              "| platform | exclusive qualnames |", "| --- | ---: |"]
        for fm in families:
            L.append(f"| {fm} | {len(exclusive.get(fm, []))} |")
        L.append("")

        win = exclusive.get("windows", [])
        if win:
            _sample_table(L, "Windows-only sample", win, union_rec)

        if oldest is not None:
            lo, hi = fmt_ver(oldest), fmt_ver(newest)
            L += [f"## Version deltas ({lo} → {hi})", "",
                  f"- **Added** — present on {hi}, absent on {lo}: **{len(added)}**",
                  f"- **Removed** — present on {lo}, absent on {hi}: **{len(removed)}**", ""]
            if added:
                _sample_table(L, f"Added since {lo}", added, union_rec)
            if removed:
                _sample_table(L, f"Removed by {hi}", removed, union_rec)
        else:
            L += ["## Version deltas", "",
                  "_Need at least two Python minors in the matrix to compute deltas._", ""]

    md_path = args.md_summary or os.environ.get("GITHUB_STEP_SUMMARY")
    if md_path:
        with open(md_path, "a", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(L) + "\n")

    print(f"\n=== union summary ========================================")
    print(f"aggregated {len(cells)} cells -> {len(union)} unique qualnames")
    for c in rows:
        extra = f"  ({c.malformed} malformed lines)" if c.malformed else ""
        print(f"  {c.cell_id:18s} {len(c.records):6d} entities{extra}")
    for fm in families:
        print(f"  {fm}-exclusive APIs : {len(exclusive.get(fm, []))}")
    if oldest is not None:
        print(f"  added {len(added)} / removed {len(removed)}  "
              f"({fmt_ver(oldest)} -> {fmt_ver(newest)})")
    print(f"wrote {len(union)} records -> {args.output}")
    print("=" * 58)


if __name__ == "__main__":
    main()
