"""
Aggregate parsed Excel QA JSON into embedding and fine-tuning datasets.

This script runs after the structured extraction pipeline saves per-question JSON
files under `parsed_json_outputs/`.  It produces two consolidated artifacts:

* `embedding_data.json` - consumed by `backend.embeddings.encode` to refresh the vector store.
* `fine_tuning_data.json` - a SQuAD-style payload for extractive QA experiments.
"""

from __future__ import annotations

import glob
import json
from pathlib import Path
from typing import Dict, List

BASE_DIR = Path(__file__).resolve().parent
PARSED_DIR = BASE_DIR / "parsed_json_outputs"
INPUT_FOLDER = PARSED_DIR
EMBEDDING_JSON = PARSED_DIR / "embedding_data.json"
FINE_TUNE_JSON = PARSED_DIR / "fine_tuning_data.json"


def build_outputs(input_folder: Path = INPUT_FOLDER) -> None:
    """Collect parsed JSON files and emit embedding/fine-tune datasets."""

    input_folder.mkdir(parents=True, exist_ok=True)

    embedding_data: List[Dict[str, object]] = []
    fine_tuning_data: Dict[str, object] = {"version": "0.1", "data": []}

    all_files = [
        path
        for path in glob.glob(str(input_folder / "*.json"))
        if Path(path).name not in {EMBEDDING_JSON.name, FINE_TUNE_JSON.name}
    ]

    qa_counter = 0
    for filepath in all_files:
        print(f"Loading {filepath}...")
        try:
            with open(filepath, "r", encoding="utf-8") as handle:
                records = json.load(handle)
        except Exception as exc:
            print(f"[WARN] Skipping (invalid JSON): {exc}")
            continue

        if not isinstance(records, list):
            print("[WARN] Skipping (expected a list of records).")
            continue

        for rec in records:
            rec_id = rec.get("id", "")
            question = rec.get("question", "")
            answers = rec.get("answers", []) or []
            section = rec.get("section", "")
            tags = rec.get("tags", [])
            source = rec.get("source", "")

            # Build embedding entries (one per answer)
            for ans_idx, ans_text in enumerate(answers):
                qa_counter += 1
                embedding_data.append(
                    {
                        "text": ans_text,
                        "metadata": {
                            "id": rec_id,
                            "answer_index": ans_idx,
                            "section": section,
                            "tags": tags,
                            "source": source,
                            "question": question,
                        },
                    }
                )

            # Build SQuAD-style entry
            paragraphs = []
            for ans_idx, ans_text in enumerate(answers):
                paragraphs.append(
                    {
                        "context": ans_text,
                        "qas": [
                            {
                                "id": f"{rec_id}_ans{ans_idx}",
                                "question": question,
                                "answers": [
                                    {
                                        "text": ans_text,
                                        "answer_start": 0,
                                    }
                                ],
                                "is_impossible": False,
                            }
                        ],
                    }
                )

            if paragraphs:
                title = section if section else rec_id or "untitled"
                fine_tuning_data["data"].append({"title": title, "paragraphs": paragraphs})

    print(f"\nTotal Q&A pairs processed: {qa_counter}")
    print(f"Writing {len(embedding_data)} passages to {EMBEDDING_JSON}...")
    with open(EMBEDDING_JSON, "w", encoding="utf-8") as handle:
        json.dump(embedding_data, handle, indent=2, ensure_ascii=False)

    print(f"Writing {len(fine_tuning_data['data'])} records to {FINE_TUNE_JSON}...")
    with open(FINE_TUNE_JSON, "w", encoding="utf-8") as handle:
        json.dump(fine_tuning_data, handle, indent=2, ensure_ascii=False)

    print("Done.")


if __name__ == "__main__":
    build_outputs()
    # To generate outputs from a different directory:
    #     build_outputs(Path("/path/to/parsed_json_outputs"))
