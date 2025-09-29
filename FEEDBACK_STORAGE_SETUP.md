# Azure Feedback Storage Configuration

The Streamlit app now writes user feedback through `feedback_storage.FeedbackStore`.
The feedback logger requires Azure configuration; when the required environment
variables are set, records are written exclusively to the Azure append blob (no local
copy). If the variables are absent, attempts to log feedback raise an error so you can
provide the credentials before capturing user feedback.

- Set the environment variables below for the Streamlit runtime:
  - `AZURE_FEEDBACK_CONNECTION_STRING`: your storage account connection string.
  - `AZURE_FEEDBACK_CONTAINER`: target container name (for example, `rfp-feedback`).
- `AZURE_FEEDBACK_BLOB`: append-blob name that will hold the NDJSON log (for example, `feedback-log.ndjson`).
- Ensure the container exists. The app creates it automatically when permissions allow.
- The app also creates the append blob on first run; each feedback submission is appended as a single NDJSON line.

### Minimal Azure Tasks

1. Create (or reuse) a Storage Account that supports append blobs.
2. Create a container for feedback (access level **Private** is recommended).
3. Generate a connection string with write permissions for the container.
4. Provide the three environment variables above to the Streamlit process.

With these values set, feedback submissions append JSON lines directly to the Azure
append blob. If the configuration is missing the app stores feedback locally. If the
configuration is present but invalid, the write fails so you can spot and correct the
Azure issue immediately (no local backup is written).

### Integration Test (Optional)

- Export `RUN_LIVE_AZURE_TEST=1` alongside the feedback environment variables.
- Ensure the Azure connection details are available either through the
  environment (`AZURE_FEEDBACK_*`) or by editing `MANUAL_AZURE_TEST_CONFIG` at
  the top of `tests/test_feedback_storage.py` with temporary credentials.
- Run `pytest tests/test_feedback_storage.py -k live_azure -vv -rs` to append a
  unique record and verify the blob contents. The test reports the specific skip
  reason whenever the configuration is incomplete, and fails with the full Azure
  SDK error message if the append call is rejected.
