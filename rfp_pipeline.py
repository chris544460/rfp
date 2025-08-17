#!/usr/bin/env python3
"""Convenience script to run slot detection and answer application in one step."""
import argparse
import json
import os
import sys
import tempfile
import importlib
from typing import Callable

from rfp_docx_slot_finder import extract_slots_from_docx
from rfp_docx_apply_answers import apply_answers_to_docx


def main():
    ap = argparse.ArgumentParser(
        description="Run slot finder then apply answers; answers JSON optional if using --generate"
    )
    ap.add_argument("docx_path", help="Path to the source .docx file")
    ap.add_argument(
        "answers_json",
        nargs="?",
        help="Path to answers JSON; omit when using --generate",
    )
    ap.add_argument("-o", "--out", default="Answered.docx", help="Output .docx file")
    ap.add_argument("--slots", help="Optional path to write detected slots JSON")
    ap.add_argument("--mode", choices=["replace", "append", "fill"], default="fill", help="Answer write mode")
    ap.add_argument(
        "--generate",
        metavar="MODULE:FUNC",
        help="Optional answer generator to call when an answer is missing or when no answers JSON is provided",
    )
    ap.add_argument("--debug", action="store_true", help="Verbose debug output")
    if len(sys.argv) == 1:
        ap.print_help()
        sys.exit(1)
    args = ap.parse_args()

    # Validate input docx
    if not os.path.isfile(args.docx_path):
        print(f"Error: '{args.docx_path}' does not exist.", file=sys.stderr)
        sys.exit(1)
    if not args.docx_path.lower().endswith(".docx"):
        print(f"Error: '{args.docx_path}' is not a .docx file.", file=sys.stderr)
        sys.exit(1)

    # Step 1: detect slots
    try:
        slots_payload = extract_slots_from_docx(args.docx_path)
    except Exception as e:
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
        print(json.dumps(slots_payload, indent=2, ensure_ascii=False))
        if not args.slots:
            os.unlink(slots_path)
        return

    # Optional answer generator
    gen_callable: Callable[[str], str] | None = None
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
        except Exception as e:
            print(f"Error: failed to load generator {args.generate}: {e}", file=sys.stderr)
            if not args.slots:
                os.unlink(slots_path)
            sys.exit(1)

    # Step 2: apply answers
    try:
        summary = apply_answers_to_docx(
            args.docx_path,
            slots_path,
            args.answers_json or "",
            args.out,
            mode=args.mode,
            generator=gen_callable,
            gen_name=gen_name,
        )
    except Exception as e:
        print(f"Error: failed to apply answers: {e}", file=sys.stderr)
        if not args.slots:
            os.unlink(slots_path)
        sys.exit(1)

    if args.debug:
        for k, v in summary.items():
            print(f"{k}: {v}")
    else:
        print(f"Wrote {args.out}")

    if not args.slots:
        os.unlink(slots_path)


if __name__ == "__main__":
    main()
