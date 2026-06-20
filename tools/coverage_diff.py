#!/usr/bin/env python3
"""Diff the introspected stdlib surface against docs.python.org inventories.

For each Python minor in the union, fetch that minor's Sphinx ``objects.inv``,
keep the ``py`` domain, and split the OS-unioned introspected surface into
covered / backlog / docs-only. The target version's backlog -- the undocumented
API actually worth writing -- is the headline artifact.

    python coverage_diff.py UNION.jsonl [--target-version 3.14]
        [-o coverage_by_version.json] [--backlog-out PATH]
        [--inventory-dir DIR] [--md-summary PATH]

The inventory carries no version metadata, so it cannot say when an entity was
added/removed -- those deltas come from the matrix (merge_summary.py), not here.
"""
from __future__ import annotations
import os
import re
import json
import time
import argparse
import urllib.error
import urllib.request
from collections import Counter

import sphobjinv as soi

INVENTORY_URL = "https://docs.python.org/{version}/objects.inv"
DEV_INVENTORY_URL = "https://docs.python.org/dev/objects.inv"

# Introspected prefix -> docs-canonical prefix. The docs index posix|nt as os.*,
# posixpath|ntpath|genericpath as os.path.*, and builtins members unprefixed.
# Extend from the docs-only bucket: a cluster of real names there is usually
# another re-export alias to add.
PREFIX_ALIASES = {
    "builtins.": "",
    "posixpath.": "os.path.",
    "ntpath.": "os.path.",
    "genericpath.": "os.path.",
    "posix.": "os.",
    "nt.": "os.",
}
MODULE_ALIASES = {
    "posixpath": "os.path", "ntpath": "os.path", "genericpath": "os.path",
    "posix": "os", "nt": "os",
}

CELL_VERSION = re.compile(r"-py([0-9][^-]*)$")
TOP_MODULES = 15
MAX_SAMPLE_ROWS = 25


def version_key(version):
    numbers = re.findall(r"\d+", version)
    return tuple(int(number) for number in numbers[:2]) if numbers else (0,)

def version_label(version):
    return ".".join(str(part) for part in version_key(version))

def cell_version(cell):
    match = CELL_VERSION.search(cell)
    return version_label(match.group(1)) if match else None

def normalize(qualname):
    if qualname in MODULE_ALIASES:
        return MODULE_ALIASES[qualname]
    for prefix, replacement in PREFIX_ALIASES.items():
        if qualname.startswith(prefix):
            return replacement + qualname[len(prefix):]
    return qualname

def normalize_module(module):
    return MODULE_ALIASES.get(module, module)

def percent(part, whole):
    return round(100 * part / whole, 1) if whole else 0.0


def load_union(path):
    records = []
    with open(path, encoding="utf-8") as source_file:
        for line in source_file:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def http_get(url, attempts=4):
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "coverage-diff"})
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.read()
        except urllib.error.HTTPError:
            raise                              # 4xx/5xx: caller decides (404 -> dev fallback)
        except urllib.error.URLError:
            if attempt == attempts - 1:
                raise
            time.sleep(2 ** attempt)
    raise ValueError("attempts must be >= 1")


def documented_names(version, inventory_dir=None):
    """Return (set of py-domain names, used_dev_fallback) for one minor."""
    if inventory_dir:
        local = os.path.join(inventory_dir, f"{version}.inv")
        if os.path.exists(local):
            with open(local, "rb") as handle:
                inventory = soi.Inventory(zlib=handle.read())
            return {obj.name for obj in inventory.objects if obj.domain == "py"}, False
    used_dev = False
    try:
        data = http_get(INVENTORY_URL.format(version=version))
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        data = http_get(DEV_INVENTORY_URL)     # in-dev minor with no numbered inventory yet
        used_dev = True
    inventory = soi.Inventory(zlib=data)
    return {obj.name for obj in inventory.objects if obj.domain == "py"}, used_dev


def build_surface(union, version):
    """norm-qualname -> representative record, for one minor, OS-unioned."""
    surface = {}
    for record in union:
        if record.get("is_dunder"):
            continue
        versions = {cell_version(cell) for cell in record.get("cells", [])}
        if version not in versions:
            continue
        name = normalize(record["qualname"])
        if name not in surface or record["qualname"] == name:
            surface[name] = record
    return surface


def diff_version(union, version, inventory_dir):
    surface = build_surface(union, version)
    documented, used_dev = documented_names(version, inventory_dir)
    surface_names = set(surface)
    covered = surface_names & documented
    backlog = surface_names - documented
    docs_only = documented - surface_names

    by_module = {}
    for name, record in surface.items():
        module = normalize_module(record["module"])
        bucket = by_module.setdefault(module, {"surface": 0, "covered": 0})
        bucket["surface"] += 1
        if name in documented:
            bucket["covered"] += 1
    for bucket in by_module.values():
        bucket["coverage_pct"] = percent(bucket["covered"], bucket["surface"])

    summary = {
        "surface": len(surface_names),
        "documented": len(documented),
        "covered": len(covered),
        "backlog": len(backlog),
        "docs_only": len(docs_only),
        "coverage_pct": percent(len(covered), len(surface_names)),
        "used_dev_fallback": used_dev,
        "by_module": dict(sorted(by_module.items())),
    }
    return summary, surface, backlog, docs_only


def backlog_records(surface, backlog):
    rows = []
    for name in backlog:
        record = surface[name]
        rows.append({
            "qualname": name,
            "kind": record["kind"],
            "module": normalize_module(record["module"]),
            "signature": record.get("signature"),
            "has_docstring": bool(record.get("doc_resolved")),
            "is_data": record["kind"] == "data",
        })
    rows.sort(key=lambda row: (row["module"], row["qualname"]))
    return rows


def default_target(versions):
    return versions[-2] if len(versions) >= 2 else versions[-1]


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("union", help="stdlib_api_union.jsonl from the aggregate job")
    parser.add_argument("--target-version", help="minor to build the backlog for "
                        "(default: latest stable = second-highest in the matrix)")
    parser.add_argument("-o", "--output", default="coverage_by_version.json")
    parser.add_argument("--backlog-out", metavar="PATH", help="default: backlog_<target>.jsonl")
    parser.add_argument("--inventory-dir", metavar="DIR",
                        help="read <minor>.inv from here before fetching (offline/cached runs)")
    parser.add_argument("--md-summary", metavar="PATH",
                        help="write the Markdown report here (defaults to $GITHUB_STEP_SUMMARY)")
    args = parser.parse_args()

    union = load_union(args.union)
    versions = sorted({cell_version(cell) for record in union
                       for cell in record.get("cells", []) if cell_version(cell)},
                      key=version_key)

    results, surfaces, backlogs, docs_onlys = {}, {}, {}, {}
    for version in versions:
        summary, surface, backlog, docs_only = diff_version(union, version, args.inventory_dir)
        results[version] = summary
        surfaces[version], backlogs[version], docs_onlys[version] = surface, backlog, docs_only
        flag = "  [dev fallback]" if summary["used_dev_fallback"] else ""
        print(f"  {version:8s} surface={summary['surface']:6d} covered={summary['covered']:6d} "
              f"backlog={summary['backlog']:6d} docs-only={summary['docs_only']:6d} "
              f"({summary['coverage_pct']}%)" + flag)

    target = args.target_version or (default_target(versions) if versions else None)

    coverage = {"target_version": target, "versions": results}
    with open(args.output, "w", encoding="utf-8", newline="\n") as out_file:
        json.dump(coverage, out_file, indent=2)
        out_file.write("\n")

    backlog_path = args.backlog_out or f"backlog_{target}.jsonl"
    rows = backlog_records(surfaces.get(target, {}), backlogs.get(target, set()))
    with open(backlog_path, "w", encoding="utf-8", newline="\n") as out_file:
        for row in rows:
            out_file.write(json.dumps(row) + "\n")

    report(versions, results, target, rows, docs_onlys.get(target, set()),
           args.output, backlog_path, args)


def _sample_table(lines, title, header, names):
    lines += [f"### {title}", "", f"| {header} |", "| --- |"]
    for name in sorted(names)[:MAX_SAMPLE_ROWS]:
        lines.append(f"| `{name}` |")
    if len(names) > MAX_SAMPLE_ROWS:
        lines.append(f"| … (+{len(names) - MAX_SAMPLE_ROWS} more) |")
    lines.append("")


def report(versions, results, target, backlog_rows, docs_only, output_path, backlog_path, args):
    data_entries = sum(1 for row in backlog_rows if row["is_data"])
    lines = ["# stdlib documentation coverage", ""]
    if not versions:
        no_versions = ("> **No versions found in the union.** The aggregate step produced "
                       "no cells, or the union schema is missing `cells`.")
        lines += [no_versions, ""]
    else:
        intro = ("Coverage = introspected surface ∩ docs.python.org `py` inventory, per "
                 "minor (OS-unioned). The inventory has no version metadata; added/removed "
                 "deltas come from the matrix, not from here.")
        lines += [f"Target version (backlog): **{target}**.", "", intro, "",
                  "## Coverage by version", "",
                  "| version | surface | covered | backlog | docs-only | coverage |",
                  "| --- | ---: | ---: | ---: | ---: | ---: |"]
        for version in versions:
            stats = results[version]
            marks = " ⭐" if version == target else (" (dev)" if stats["used_dev_fallback"] else "")
            lines.append(f"| {version}{marks} | {stats['surface']} | {stats['covered']} "
                         f"| {stats['backlog']} | {stats['docs_only']} | {stats['coverage_pct']}% |")
        lines.append("")

        lines += [f"## Target backlog — {target} ({len(backlog_rows)} undocumented)", "",
                  f"Reference-entry core (callables/classes/etc.): **{len(backlog_rows) - data_entries}**; "
                  f"`data` entries: **{data_entries}**.", "",
                  "| kind | count |", "| --- | ---: |"]
        for kind, count in Counter(row["kind"] for row in backlog_rows).most_common():
            lines.append(f"| {kind} | {count} |")
        lines.append("")

        module_stats = results.get(target, {}).get("by_module", {})
        per_module = Counter(row["module"] for row in backlog_rows)
        lines += [f"## Top {TOP_MODULES} modules by undocumented count ({target})", "",
                  "| module | undocumented | surface | coverage |", "| --- | ---: | ---: | ---: |"]
        for module, count in per_module.most_common(TOP_MODULES):
            stats = module_stats.get(module, {})
            lines.append(f"| `{module}` | {count} | {stats.get('surface', '?')} "
                         f"| {stats.get('coverage_pct', '?')}% |")
        lines.append("")

        if docs_only:
            docs_only_note = ("In the inventory but not introspected: removed/renamed API the "
                              "docs still list, or names this run failed to enumerate "
                              "(normalization QA signal).")
            lines += [f"## Docs-only — {target} ({len(docs_only)})", "", docs_only_note, ""]
            _sample_table(lines, "Sample", "name", docs_only)

    markdown_path = args.md_summary or os.environ.get("GITHUB_STEP_SUMMARY")
    if markdown_path:
        with open(markdown_path, "a", encoding="utf-8", newline="\n") as summary_file:
            summary_file.write("\n".join(lines) + "\n")

    print("\n=== coverage summary =====================================")
    if versions:
        print(f"target {target}; versions: {', '.join(versions)}")
        print(f"target backlog: {len(backlog_rows)} undocumented ({data_entries} data)")
    else:
        print("no versions found in union")
    print(f"wrote {output_path} and {backlog_path}")
    print("=" * 58)


if __name__ == "__main__":
    main()
