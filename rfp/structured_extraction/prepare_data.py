import json
import glob
from pathlib import Path

######################################
# 1) CONFIGURATION
######################################

BASE_DIR        = Path(__file__).resolve().parent
PARSED_DIR      = BASE_DIR / "parsed_json_outputs"
INPUT_FOLDER    = PARSED_DIR                 # folder with the original JSON files
EMBEDDING_JSON  = PARSED_DIR / "embedding_data.json"    # output for vector search
FINE_TUNE_JSON  = PARSED_DIR / "fine_tuning_data.json"  # output for extractive QA fine-tuning

######################################
# 2) SET UP CONTAINERS
######################################

# For embedding search: a list of passages + metadata
embedding_data = []

# For fine-tuning: SQuAD-style structure
fine_tuning_data = {
  "version": "0.1",
  "data": []
}

######################################
# 3) READ & MERGE
######################################

PARSED_DIR.mkdir(parents=True, exist_ok=True)

all_files  = [
    path
    for path in glob.glob(str(INPUT_FOLDER / "*.json"))
    if Path(path).name not in {EMBEDDING_JSON.name, FINE_TUNE_JSON.name}
]
qa_counter = 0

for filepath in all_files:
    print(f"Loading {filepath}...")
    with open(filepath, "r", encoding="utf-8") as f:
        try:
            records = json.load(f)
        except Exception as e:
            print(f"⚠️ Skipping (invalid JSON): {e}")
            continue

    if not isinstance(records, list):
        print("⚠️ Skipping (expected a list of records).")
        continue

    for rec in records:
        rec_id    = rec.get("id", "")
        question  = rec.get("question", "")
        answers   = rec.get("answers", [])
        section   = rec.get("section", "")
        tags      = rec.get("tags", [])
        source    = rec.get("source", "")

        # Build embedding entries (one per answer)
        for ans_idx, ans_text in enumerate(answers):
            qa_counter += 1

            embedding_data.append({
              "text": ans_text,
              "metadata": {
                "id":            rec_id,
                "answer_index":  ans_idx,
                "section":       section,
                "tags":          tags,
                "source":        source,
                "question":      question
              }
            })

        # Build SQuAD-style entry (one record ⇒ one 'title' with multiple paragraphs)
        paragraphs = []
        for ans_idx, ans_text in enumerate(answers):
            paragraphs.append({
              "context": ans_text,
              "qas": [
                {
                  "id":             f"{rec_id}_ans{ans_idx}",
                  "question":       question,
                  "answers": [
                    {
                      "text":         ans_text,
                      "answer_start": 0
                    }
                  ],
                  "is_impossible": False
                }
              ]
            })

        if paragraphs:
            title = section if section else rec_id or "untitled"
            fine_tuning_data["data"].append({
              "title":      title,
              "paragraphs": paragraphs
            })

######################################
# 4) WRITE OUTPUTS
######################################

print(f"\nTotal Q&A pairs processed: {qa_counter}")
print(f"Writing {len(embedding_data)} passages to {EMBEDDING_JSON}...")
with open(EMBEDDING_JSON, "w", encoding="utf-8") as f:
    json.dump(embedding_data, f, indent=2, ensure_ascii=False)

print(f"Writing {len(fine_tuning_data['data'])} records to {FINE_TUNE_JSON}...")
with open(FINE_TUNE_JSON, "w", encoding="utf-8") as f:
    json.dump(fine_tuning_data, f, indent=2, ensure_ascii=False)

print("✅ Done.")
