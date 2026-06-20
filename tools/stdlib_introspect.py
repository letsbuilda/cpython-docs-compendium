#!/usr/bin/env python3
"""Enumerate the standard library's public API surface by introspection.

Emits one JSON record per documentable entity (JSONL) for THIS interpreter, build,
and platform only -- what exists here, which differs across OS and Python version.

Stdlib only, no third-party deps. Run:
    python stdlib_introspect.py [-o out.jsonl] [--include-dunders] [--include-private]
                                [--md-summary PATH] [--min-entities N]

When --md-summary PATH is given (or the env var GITHUB_STEP_SUMMARY is set), the
stats are also rendered as Markdown there. --min-entities is a sanity gate: a build
that produces fewer records than that exits non-zero, because a near-empty dump means
something broke rather than a real result.

Per-type dunders are excluded by default (the docs cover them in the data model
section); --include-dunders keeps them, flagged with is_dunder=True.
"""
from __future__ import annotations
import sys
import os
import io
import json
import inspect
import pkgutil
import importlib
import warnings
import argparse
import contextlib
import platform

# Modules with import-time side effects (browser/print) or that we never document.
SKIP_MODULES = {
    "antigravity",          # opens a web browser on import
    "this",                 # prints the Zen of Python
    "__hello__", "__phello__",   # print on import
    "test",                 # CPython's own test suite
    "idlelib",              # the IDLE GUI app
    "turtledemo",           # demo scripts
    "lib2to3",              # grammar/test heavy; removed in 3.13
}
TEST_PARTS = {"test", "tests"}

def is_dunder(name):  return len(name) > 4 and name.startswith("__") and name.endswith("__")
def is_private(name): return name.startswith("_") and not is_dunder(name)

@contextlib.contextmanager
def _silenced():
    """Swallow stdout/stderr/warnings during risky imports."""
    sink = io.StringIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            yield

def safe_import(name):
    short_name = name.split(".")[-1]
    if name in SKIP_MODULES or short_name in SKIP_MODULES:
        return None
    if os.environ.get("TRACE_IMPORTS"):
        print(f"  importing {name}", file=sys.stderr, flush=True)
    try:
        with _silenced():
            return importlib.import_module(name)
    except KeyboardInterrupt:
        raise
    except BaseException:        # ImportError, platform gates, missing C libs, etc.
        return None

def roster(include_private):
    names = getattr(sys, "stdlib_module_names", None)
    if not names:
        sys.exit("Requires Python 3.10+ (needs sys.stdlib_module_names).")
    seen = set()
    for top_level in sorted(name for name in names if include_private or not name.startswith("_")):
        yield from _emit(top_level, include_private, seen)

def _emit(name, include_private, seen):
    if name in seen:
        return
    seen.add(name)
    short_name = name.split(".")[-1]
    if name in SKIP_MODULES or short_name in SKIP_MODULES or short_name in TEST_PARTS:
        return
    module = safe_import(name)
    yield name, module
    if module is not None and hasattr(module, "__path__"):
        try:
            with _silenced():
                submodules = [info.name for info in pkgutil.iter_modules(module.__path__, name + ".")]
        except Exception:
            submodules = []
        for submodule in sorted(submodules):
            submodule_short = submodule.split(".")[-1]
            if (is_private(submodule_short) and not include_private) or submodule_short in TEST_PARTS:
                continue
            yield from _emit(submodule, include_private, seen)

SEEN = {}       # id(obj) -> canonical qualname (first sighting)
RECORDS = {}
PENDING = {}    # id(obj) -> [alias qualnames seen before the canonical one]

def kind_of(entity, in_class):
    if inspect.ismodule(entity):
        return "module"
    if isinstance(entity, type):
        return "exception" if issubclass(entity, BaseException) else "class"
    if isinstance(entity, property):
        return "property"
    if inspect.isgetsetdescriptor(entity) or inspect.ismemberdescriptor(entity):
        return "descriptor"
    if inspect.isroutine(entity):      # function / builtin / method / method_descriptor
        return "method" if in_class else "function"
    return "data"

def get_signature(entity):
    try:
        return str(inspect.signature(entity)), "inspect"
    except (ValueError, TypeError):
        text_signature = getattr(entity, "__text_signature__", None)
        return (text_signature, "text_signature") if text_signature else (None, "none")

def doc_info(entity):
    has_own_doc = bool(getattr(entity, "__doc__", None))
    resolved_doc = inspect.getdoc(entity)
    first_line = ""
    if resolved_doc and resolved_doc.strip():
        first_line = resolved_doc.strip().splitlines()[0][:100]
    return has_own_doc, bool(resolved_doc), first_line

def attach(canonical, alias):
    existing = RECORDS.get(canonical)
    if existing is not None and alias != canonical and alias not in existing["aliases"]:
        existing["aliases"].append(alias)

def note_alias(entity, qualname):
    try:
        object_id = id(entity)
    except Exception:
        return
    if object_id in SEEN:
        attach(SEEN[object_id], qualname)
    else:
        PENDING.setdefault(object_id, []).append(qualname)

def record(qualname, kind, module, parent, entity, short_name):
    signature, signature_source = get_signature(entity) if kind in {"function", "method", "class", "exception"} else (None, "n/a")
    has_own_doc, has_resolved_doc, first_line = doc_info(entity)
    RECORDS[qualname] = {
        "qualname": qualname, "kind": kind, "module": module, "parent": parent,
        "is_dunder": is_dunder(short_name),
        "signature": signature, "sig_source": signature_source,
        "doc_own": has_own_doc, "doc_resolved": has_resolved_doc, "doc_firstline": first_line,
        "aliases": [],
    }

def process(entity, qualname, parent, module_name, short_name, in_class, include_dunders, include_private):
    kind = kind_of(entity, in_class)
    if kind == "data":
        # Value-typed: identity is NOT meaningful (small ints / interned strings
        # share id() across the whole stdlib), so no id-dedup and no recursion.
        if qualname not in RECORDS:
            record(qualname, kind, module_name, parent, entity, short_name)
        return
    object_id = id(entity)
    if object_id in SEEN:
        attach(SEEN[object_id], qualname)        # re-export under another name
        return
    SEEN[object_id] = qualname
    kind = kind_of(entity, in_class)
    record(qualname, kind, module_name, parent, entity, short_name)
    for alias in PENDING.pop(object_id, []):
        attach(qualname, alias)
    # Recurse into a class's OWN members only (mirrors "document where defined").
    if kind in {"class", "exception"}:
        for child_name, child_value in sorted(vars(entity).items()):
            if (is_dunder(child_name) and not include_dunders) or (is_private(child_name) and not include_private):
                continue
            process(child_value, f"{qualname}.{child_name}", qualname, module_name, child_name, True, include_dunders, include_private)

def walk_module(module, include_dunders, include_private):
    module_name = module.__name__
    # vars() (not dir()+getattr) so we don't trigger lazy __getattr__ / property side effects.
    for name, value in sorted(vars(module).items()):
        if (is_dunder(name) and not include_dunders) or (is_private(name) and not include_private):
            continue
        if inspect.ismodule(value):       # imported module ref (ast.sys, os.path); roster handles real modules
            continue
        owner = getattr(value, "__module__", None)
        if owner is not None and owner != module_name:
            note_alias(value, f"{module_name}.{name}")     # imported from elsewhere
            continue
        process(value, f"{module_name}.{name}", module_name, module_name, name, False, include_dunders, include_private)

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-o", "--output", default="stdlib_api.jsonl")
    parser.add_argument("--include-dunders", action="store_true")
    parser.add_argument("--include-private", action="store_true")
    parser.add_argument("--md-summary", metavar="PATH",
                        help="also write the summary as Markdown here "
                             "(defaults to $GITHUB_STEP_SUMMARY when that is set)")
    parser.add_argument("--min-entities", type=int, default=5000,
                        help="sanity gate: exit non-zero if fewer than N records were "
                             "produced (a near-empty dump means the build is broken)")
    parser.add_argument("--watchdog", type=int, default=int(os.environ.get("WATCHDOG_SECONDS", "0")),
                        help="if >0: dump all thread stacks and abort after N seconds "
                             "(a stdlib module that HANGS on import can't be caught by try/except)")
    args = parser.parse_args()
    if args.watchdog > 0:
        import faulthandler
        faulthandler.dump_traceback_later(args.watchdog, exit=True)

    scanned, failed = 0, []
    for name, module in roster(args.include_private):
        scanned += 1
        if module is None:
            failed.append(name)
            continue
        has_own_doc, has_resolved_doc, first_line = doc_info(module)
        RECORDS[name] = {
            "qualname": name, "kind": "module", "module": name,
            "parent": name.rpartition(".")[0] or None, "is_dunder": False,
            "signature": None, "sig_source": "n/a",
            "doc_own": has_own_doc, "doc_resolved": has_resolved_doc, "doc_firstline": first_line,
            "aliases": [],
        }
        try:
            walk_module(module, args.include_dunders, args.include_private)
        except Exception as error:
            failed.append(f"{name} (walk: {error!r})")

    records = sorted(RECORDS.values(), key=lambda entry: entry["qualname"])
    # Default encoding is cp1252 on Windows (crashes on non-ASCII docstrings) and text
    # mode there translates \n -> \r\n; pin UTF-8 + LF so every cell emits identical bytes.
    with open(args.output, "w", encoding="utf-8", newline="\n") as out_file:
        for entry in records:
            out_file.write(json.dumps(entry) + "\n")

    stats = _stats(records, scanned, failed)
    _text_summary(stats, args.output)
    markdown_path = args.md_summary or os.environ.get("GITHUB_STEP_SUMMARY")
    if markdown_path:
        _markdown_summary(stats, args.output, markdown_path)

    # Gate last, after the output and summaries are written, so a broken cell still
    # uploads its dump and renders a summary before the non-zero exit fails the job.
    sys.stdout.flush(); sys.stderr.flush()
    if len(records) < args.min_entities:
        print(f"\nSANITY GATE: only {len(records)} records "
              f"(< --min-entities {args.min_entities}); this build looks broken.",
              file=sys.stderr, flush=True)
        os._exit(1)
    os._exit(0)

def _stats(records, scanned, failed):
    from collections import Counter
    callables = [entry for entry in records if entry["kind"] in {"function", "method"}]
    return {
        "total_records": len(records),
        "scanned": scanned,
        "failed": failed,
        "kinds": Counter(entry["kind"] for entry in records),
        "total_callables": len(callables),
        "with_signature": sum(1 for entry in callables if entry["signature"]),
        "with_docstring": sum(1 for entry in records if entry["doc_resolved"]),
        "by_module": Counter(entry["qualname"].split(".")[0] for entry in records),
    }

def _percent(part, whole):
    return f"{100*part//whole}%" if whole else "n/a"

def _text_summary(stats, output_path):
    print("\n=== stdlib introspection summary =========================")
    print(f"Python {sys.version.split()[0]} on {sys.platform}")
    print(f"modules scanned        : {stats['scanned']}  ({len(stats['failed'])} not introspectable here)")
    print(f"total entities         : {stats['total_records']}")
    print("  by kind              : " + ", ".join(f"{kind}={count}" for kind, count in stats['kinds'].most_common()))
    if stats['total_callables']:
        print(f"callables w/ signature : {stats['with_signature']}/{stats['total_callables']}  ({_percent(stats['with_signature'], stats['total_callables'])})")
    if stats['total_records']:
        print(f"entities w/ docstring  : {stats['with_docstring']}/{stats['total_records']}  ({_percent(stats['with_docstring'], stats['total_records'])})")
    print("\ntop 12 modules by entity count:")
    for module_name, count in stats['by_module'].most_common(12):
        print(f"  {count:5d}  {module_name}")
    if stats['failed']:
        shown = ", ".join(stats['failed'][:18])
        more = f"  (+{len(stats['failed'])-18} more)" if len(stats['failed']) > 18 else ""
        print(f"\nnot introspectable on this build ({len(stats['failed'])}): {shown}{more}")
    print(f"\nwrote {stats['total_records']} records -> {output_path}")
    print("=" * 58)

def _markdown_summary(stats, output_path, summary_path):
    python_version = sys.version.split()[0]
    lines = [
        f"## stdlib introspection — Python {python_version} on `{sys.platform}`",
        "",
        "| metric | value |",
        "| --- | --- |",
        f"| platform | `{platform.platform()}` |",
        f"| modules scanned | {stats['scanned']} |",
        f"| modules not introspectable | {len(stats['failed'])} |",
        f"| total entities | {stats['total_records']} |",
    ]
    if stats['total_callables']:
        lines.append(f"| callables w/ signature | {stats['with_signature']}/{stats['total_callables']} ({_percent(stats['with_signature'], stats['total_callables'])}) |")
    lines.append(f"| entities w/ docstring | {stats['with_docstring']}/{stats['total_records']} ({_percent(stats['with_docstring'], stats['total_records'])}) |")

    lines += ["", "### Entities by kind", "", "| kind | count |", "| --- | --- |"]
    lines += [f"| {kind} | {count} |" for kind, count in stats['kinds'].most_common()]

    lines += ["", "### Top modules by entity count", "", "| module | entities |", "| --- | --- |"]
    lines += [f"| `{module_name}` | {count} |" for module_name, count in stats['by_module'].most_common(12)]

    failed = stats['failed']
    lines += ["", f"### Not introspectable on this build ({len(failed)})", ""]
    lines.append(", ".join(f"`{module_name}`" for module_name in sorted(failed)) if failed else "_none_")
    lines.append("")

    # Append: $GITHUB_STEP_SUMMARY is an append target, and the file is fresh per step.
    # newline="\n" keeps the summary byte-identical on the Windows runner.
    with open(summary_path, "a", encoding="utf-8", newline="\n") as summary_file:
        summary_file.write("\n".join(lines) + "\n")

if __name__ == "__main__":
    main()
