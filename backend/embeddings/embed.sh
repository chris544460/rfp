# embed.sh

# (1) PURE "answer-only" index (w=0.0)
python3 backend/embeddings/encode.py \
  --file documents/xlsx/structured_extraction/parsed_json_outputs/embedding_data.json \
  --output backend/retrieval/vector_store/answer \
  --workers 4 \
  --model text-embedding-ada-002 \
  --weight 0.0

# (2) PURE "question-only" index (w=1.0)
python3 backend/embeddings/encode.py \
  --file documents/xlsx/structured_extraction/parsed_json_outputs/embedding_data.json \
  --output backend/retrieval/vector_store/question \
  --workers 4 \
  --model text-embedding-ada-002 \
  --weight 1.0

# (3) BLENDED index (e.g. w=0.65)
python3 backend/embeddings/encode.py \
  --file documents/xlsx/structured_extraction/parsed_json_outputs/embedding_data.json \
  --output backend/retrieval/vector_store/blend \
  --workers 4 \
  --model text-embedding-ada-002 \
  --weight 0.65
