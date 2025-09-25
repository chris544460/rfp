"""Command-line helper for the structured extraction workflow.

This script guides a user through the typical pipeline:

1. Place source files (DOCX or Excel) inside ``structured_extraction/data_sources/``.
2. Convert a selected file into JSON using the parsers in
   ``structured_extraction/parser.py`` (outputs land in
   ``parsed_json_outputs/``).
3. Optionally rebuild the aggregated datasets produced by
   ``structured_extraction/prepare_data.py`` (outputs stored in
   ``structured_extraction/parsed_json_outputs/``).

Running the script presents a small interactive menu allowing the user to:

* Choose a file from ``structured_extraction/data_sources`` (or provide a custom path).
* Parse every file in ``structured_extraction/data_sources`` before regenerating prepared
  data assets.
* Regenerate prepared data assets without parsing.

The implementation keeps everything in one place so a single command
manages the otherwise multi-step process.
"""

from __future__ import annotations

import runpy
from pathlib import Path
from typing import List, Optional

from structured_extraction.parser import (
    MixedDocParser,
    process_excel_file_with_detection,
)

BASE_DIR = Path(__file__).resolve().parent
STRUCTURED_EXTRACTION_DIR = BASE_DIR / "structured_extraction"
DATA_SOURCES_DIR = STRUCTURED_EXTRACTION_DIR / "data_sources"
PARSED_OUTPUT_DIR = STRUCTURED_EXTRACTION_DIR / "parsed_json_outputs"
PREPARED_OUTPUT_DIR = PARSED_OUTPUT_DIR


def ensure_directories() -> None:
    """Make sure the expected folders exist."""

    DATA_SOURCES_DIR.mkdir(parents=True, exist_ok=True)
    PARSED_OUTPUT_DIR.mkdir(exist_ok=True)
    PREPARED_OUTPUT_DIR.mkdir(exist_ok=True)


def list_data_source_files() -> List[Path]:
    """Return the files currently available in ``structured_extraction/data_sources``."""

    return sorted(
        [p for p in DATA_SOURCES_DIR.iterdir() if p.is_file()],
        key=lambda p: p.name.lower(),
    )


def prompt_for_file() -> Optional[Path]:
    """Interactively ask the user which file to process."""

    files = list_data_source_files()
    print("\nSelect a source file:")
    if files:
        for idx, path in enumerate(files, start=1):
            print(f"  {idx}) {path.name}")
    else:
        print("  (no files in structured_extraction/data_sources yet)")

    print("  C) Enter a custom path")
    print("  Q) Cancel")

    while True:
        choice = input("Choice: ").strip().lower()
        if choice == "q":
            return None
        if choice == "c":
            custom = input("Enter full path to the file: ").strip()
            if not custom:
                print("Please enter a path or choose another option.")
                continue
            custom_path = Path(custom).expanduser().resolve()
            if custom_path.is_file():
                return custom_path
            print(f"Could not find file at: {custom_path}")
            continue
        if choice.isdigit():
            index = int(choice) - 1
            if 0 <= index < len(files):
                return files[index]
            print("Invalid selection number.")
            continue
        print("Please select one of the listed options.")


def parse_file(source_path: Path) -> None:
    """Parse DOCX or Excel files into JSON outputs."""

    suffix = source_path.suffix.lower()
    if suffix == ".docx":
        parser = MixedDocParser(str(source_path))
        records = parser.parse()
        output_path = PARSED_OUTPUT_DIR / f"{source_path.stem}.json"
        parser.to_json(str(output_path))
        print(
            f"Parsed {len(records)} records from '{source_path.name}' "
            f"into {output_path}"
        )
    elif suffix in {".xlsx", ".xls", ".xlsm", ".xlsb"}:
        process_excel_file_with_detection(str(source_path), str(PARSED_OUTPUT_DIR))
    else:
        print(
            "Unsupported file type. Please provide a DOCX or Excel file "
            f"(got '{source_path.suffix}')."
        )


def run_prepare_data() -> None:
    """Execute ``prepare_data.py`` and move its outputs into ``prepared_data``."""

    print("\nRunning structured_extraction.prepare_data ...")
    runpy.run_module("structured_extraction.prepare_data", run_name="__main__")

    for filename in ["embedding_data.json", "fine_tuning_data.json"]:
        produced = PREPARED_OUTPUT_DIR / filename
        if produced.exists():
            print(f"Output available at {produced}")
        else:
            print(f"Expected output '{filename}' was not created.")


def parse_all_data_source_files() -> None:
    """Parse every file in ``structured_extraction/data_sources`` and rebuild data."""

    files = list_data_source_files()
    if not files:
        print("\nNo files found in structured_extraction/data_sources.")
        return

    print("\nParsing all files in structured_extraction/data_sources ...")
    for path in files:
        print(f"\nProcessing {path.name}...")
        parse_file(path)

    run_prepare_data()


def prompt_for_action() -> str:
    """Ask the user what action they want to perform."""

    print("\nWhat would you like to do?")
    print("  1) Parse a file (DOCX/Excel -> JSON)")
    print("  2) Regenerate prepared data outputs")
    print("  3) Parse all files then regenerate prepared data")
    print("  Q) Quit")

    while True:
        action = input("Choice: ").strip().lower()
        if action in {"1", "2", "3", "q"}:
            return action
        print("Please choose 1, 2, 3, or Q.")


def main() -> None:
    ensure_directories()
    print("Structured Extraction CLI")
    print("==========================")

    while True:
        action = prompt_for_action()
        if action == "q":
            print("Goodbye!")
            return
        if action == "1":
            path = prompt_for_file()
            if path is None:
                continue
            parse_file(path)
        elif action == "2":
            run_prepare_data()
        elif action == "3":
            parse_all_data_source_files()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted by user.")
