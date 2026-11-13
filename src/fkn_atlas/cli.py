"""Command-line entry point. Tests and fixtures are the only required dependencies."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
from .storage import Store
from .crawler import run_demo, run_scout


def main() -> None:
    parser = argparse.ArgumentParser(description="FKN Atlas — provenance-first pilot")
    sub = parser.add_subparsers(dest="action", required=True)
    for name in ("demo", "scout", "report"):
        item = sub.add_parser(name)
        item.add_argument("--db", default="runtime/atlas.sqlite")
        if name == "scout":
            item.add_argument("--max-pages", type=int, default=8)
            item.add_argument("--max-depth", type=int, default=2)
            item.add_argument("--delay", type=float, default=1.5)
    args = parser.parse_args()
    store = Store(args.db)
    try:
        if args.action == "demo":
            # Works from a freshly unpacked tree with src on PYTHONPATH.
            fixture_dir = Path(__file__).resolve().parents[2] / "fixtures"
            stats = run_demo(store, fixture_dir)
        elif args.action == "scout":
            stats = run_scout(store, args.max_pages, args.max_depth, args.delay)
        else:
            stats = store.stats()
        print(json.dumps(stats, ensure_ascii=False, indent=2))
    finally:
        store.close()
