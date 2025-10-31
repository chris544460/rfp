#!/usr/bin/env bash

# export_azure.sh
#
# Generate the blended FAISS index and an Azure AI Search JSON payload
# containing the same blended embeddings.

set -euo pipefail

EMBED_FILE="${1:-structured_extraction/parsed_json_outputs/embedding_data.json}"
OUTPUT_DIR="${2:-vector_store/blend}"
AZURE_JSON="${3:-vector_store/blend/azure_payload.json}"
WORKERS="${WORKERS:-4}"
MODEL="${MODEL:-text-embedding-ada-002}"
WEIGHT="${WEIGHT:-0.65}"

python3 embed/encode.py \
  --file "${EMBED_FILE}" \
  --output "${OUTPUT_DIR}" \
  --workers "${WORKERS}" \
  --model "${MODEL}" \
  --weight "${WEIGHT}" \
  --azure-output "${AZURE_JSON}"

