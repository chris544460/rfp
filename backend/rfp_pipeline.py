#!/usr/bin/env python3
"""Convenience script to run slot detection and answer application.

Previously this pipeline only supported DOCX files.  It has been
refactored to look up concrete handler implementations based on the
source file's extension so that other document types can be supported in
future without altering this script.
"""

import argparse
import json
import os
import sys
import tempfile
import importlib
from typing import Callable, Optional

from .rfp_handlers import get_handlers


def main() -> None:
    ap = argparse.ArgumentParser(
        description=(
            "Run slot detection then apply answers.  The handler is selected "
            "based on the source file's extension; answers JSON optional if "
            "using --generate"
        ),
    )
    ap.add_argument("source_path", help="Path to the source document")
    ap.add_argument(
        "answers_json",
        nargs="?",
        help="Path to answers JSON; omit when using --generate",
    )
    ap.add_argument("-o", "--out", help="Output file (defaults to Answered<ext>)")
    ap.add_argument("--slots", help="Optional path to write detected slots JSON")
    ap.add_argument(
        "--mode", choices=["replace", "append", "fill"], default="fill", help="Answer write mode"
    )
    ap.add_argument(
        "--generate",
        metavar="MODULE:FUNC",
        help="Optional answer generator to call when an answer is missing or when no answers JSON is provided",
    )
    ap.add_argument(
        "--debug",
        dest="debug",
        action="store_true",
        default=True,
        help="Verbose debug output (default on)",
    )
    ap.add_argument(
        "--no-debug",
        dest="debug",
        action="store_false",
        help="Disable debug output",
    )
    if len(sys.argv) == 1:
        ap.print_help()
        sys.exit(1)
    args = ap.parse_args()

    if args.debug:
        print("[rfp_pipeline] starting pipeline")
    # Determine file handlers based on extension
    if not os.path.isfile(args.source_path):
        print(f"Error: '{args.source_path}' does not exist.", file=sys.stderr)
        sys.exit(1)
    ext = os.path.splitext(args.source_path)[1].lower()
    try:
        extract_slots, apply_answers = get_handlers(ext)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    # Default output path
    out_path = args.out or f"Answered{ext}"

    # Step 1: detect slots
    if args.debug:
        print("[rfp_pipeline] extracting slots")
    try:
        slots_payload = extract_slots(args.source_path)
    except Exception as e:  # pragma: no cover - defensive
        print(f"Error: failed to extract slots: {e}", file=sys.stderr)
        sys.exit(1)

    # Save slots JSON
    if args.slots:
        slots_path = args.slots
        with open(slots_path, "w", encoding="utf-8") as f:
            json.dump(slots_payload, f, indent=2, ensure_ascii=False)
    else:
        fd, slots_path = tempfile.mkstemp(prefix="slots_", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(slots_payload, f)

    # If no answers JSON and no generator specified, just print slots and exit
    if not args.answers_json and not args.generate:
        if args.debug:
            print("[rfp_pipeline] no answers JSON or generator; exiting after slot detection")
        print(json.dumps(slots_payload, indent=2, ensure_ascii=False))
        if not args.slots:
            os.unlink(slots_path)
        return

    # Optional answer generator
    gen_callable: Optional[Callable[[str], str]] = None
    gen_name = ""
    if args.generate:
        if ":" not in args.generate:
            print("Error: --generate requires MODULE:FUNC", file=sys.stderr)
            if not args.slots:
                os.unlink(slots_path)
            sys.exit(1)
        mod_name, func_name = args.generate.split(":", 1)
        try:
            module = importlib.import_module(mod_name)
            gen_callable = getattr(module, func_name)
            if not callable(gen_callable):
                raise AttributeError
            gen_name = args.generate
            if args.debug:
                print(f"[rfp_pipeline] loaded generator {gen_name}")
        except Exception as e:  # pragma: no cover - defensive
            print(f"Error: failed to load generator {args.generate}: {e}", file=sys.stderr)
            if not args.slots:
                os.unlink(slots_path)
            sys.exit(1)

    # Step 2: apply answers
    if args.debug:
        print("[rfp_pipeline] applying answers")
    try:
        summary = apply_answers(
            args.source_path,
            slots_path,
            args.answers_json or "",
            out_path,
            mode=args.mode,
            generator=gen_callable,
            gen_name=gen_name,
        )
    except Exception as e:  # pragma: no cover - defensive
        print(f"Error: failed to apply answers: {e}", file=sys.stderr)
        if not args.slots:
            os.unlink(slots_path)
        sys.exit(1)

    if args.debug:
        for k, v in summary.items():
            print(f"{k}: {v}")
    else:
        print(f"Wrote {out_path}")

    if not args.slots:
        os.unlink(slots_path)


if __name__ == "__main__":
    main()
