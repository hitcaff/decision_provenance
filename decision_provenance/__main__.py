"""
__main__.py — CLI for decision-provenance.

Usage:
    python -m decision_provenance verify  --db provenance.db --model loan_scorer
    python -m decision_provenance stats   --db provenance.db --model loan_scorer
    python -m decision_provenance export  --db provenance.db --model loan_scorer
    python -m decision_provenance search  --db provenance.db --model loan_scorer --label approved
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


def _get_conn(db_path: str) -> sqlite3.Connection:
    if not Path(db_path).exists():
        print(f"Error: DB not found at {db_path}")
        sys.exit(1)
    return sqlite3.connect(db_path, check_same_thread=False)


def cmd_verify(args):
    from .chain import MerkleChain
    conn = _get_conn(args.db)
    chain = MerkleChain(conn)
    ok, msg = chain.verify()
    icon = "✅" if ok else "❌"
    print(f"{icon} {msg}")
    sys.exit(0 if ok else 1)


def cmd_stats(args):
    from .chain import MerkleChain
    from .genesis import GenesisChain
    from .label_registry import LabelRegistry
    from .config_record import ConfigChain

    conn = _get_conn(args.db)
    chain   = MerkleChain(conn)
    genesis = GenesisChain(conn)
    labels  = LabelRegistry(conn)
    configs = ConfigChain(conn)

    ok, msg = chain.verify()

    print(f"\n{'='*50}")
    print(f"  decision-provenance stats")
    print(f"{'='*50}")
    print(f"  Model:        {args.model or '(all)'}")
    print(f"  Records:      {chain.record_count}")
    print(f"  Chain root:   {chain.current_root[:32]}...")
    print(f"  Integrity:    {'✅ intact' if ok else '❌ BROKEN'}")
    print()

    if args.model:
        g = genesis.current(args.model)
        if g:
            print(f"  Genesis ID:   {g.genesis_id[:16]}...")
            print(f"  Schema:       {g.schema_version}")
            print(f"  Started by:   {g.created_by} on {g.created_at}")
            print(f"  Reason:       {g.reason}")
        print()
        print(f"  Labels:       {labels.all_labels()}")
        print()
        cfgs = configs.all_configs(args.model)
        print(f"  Config versions: {len(cfgs)}")
        for c in cfgs:
            print(f"    {c['config_version']}: threshold={c['threshold']} "
                  f"by={c['changed_by']}")
    print()


def cmd_export(args):
    from .chain import MerkleChain
    conn = _get_conn(args.db)
    chain = MerkleChain(conn)
    out = args.output or "audit_log.jsonl"
    n = chain.export_jsonl(out)
    print(f"Exported {n} records to {out}")


def cmd_search(args):
    from .chain import MerkleChain
    conn = _get_conn(args.db)
    chain = MerkleChain(conn)
    results = chain.search(
        model_id=args.model,
        label_display=args.label,
        date_from=args.from_date,
        date_to=args.to_date,
        limit=args.limit,
        offset=args.offset,
    )
    total = chain.count(
        model_id=args.model,
        label_display=args.label,
        date_from=args.from_date,
        date_to=args.to_date,
    )
    print(f"Found {total} records (showing {len(results)}):\n")
    for r in results:
        print(f"  {r.get('timestamp_iso','?')}  "
              f"{r.get('label_display','?'):<12}  "
              f"score={r.get('score','?')}  "
              f"id={r.get('record_id','?')[:8]}...")


def main():
    parser = argparse.ArgumentParser(
        prog="decision_provenance",
        description="decision-provenance CLI"
    )
    parser.add_argument("--db", default="provenance.db", help="Path to SQLite DB")
    parser.add_argument("--model", default=None, help="Model ID filter")

    sub = parser.add_subparsers(dest="command")

    sub.add_parser("verify", help="Verify chain integrity")
    sub.add_parser("stats",  help="Print chain statistics")

    exp = sub.add_parser("export", help="Export audit log as JSONL")
    exp.add_argument("--output", default=None, help="Output file path")

    srch = sub.add_parser("search", help="Search records")
    srch.add_argument("--label",     default=None, help="Filter by label display string")
    srch.add_argument("--from-date", dest="from_date", default=None)
    srch.add_argument("--to-date",   dest="to_date",   default=None)
    srch.add_argument("--limit",  type=int, default=20)
    srch.add_argument("--offset", type=int, default=0)

    args = parser.parse_args()

    if args.command == "verify":
        cmd_verify(args)
    elif args.command == "stats":
        cmd_stats(args)
    elif args.command == "export":
        cmd_export(args)
    elif args.command == "search":
        cmd_search(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
