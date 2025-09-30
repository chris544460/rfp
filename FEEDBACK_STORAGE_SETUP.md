# Local Feedback Storage

The Streamlit app writes user feedback through `feedback_storage.FeedbackStore`,
which now appends each record to a local NDJSON file. This keeps the feedback
workflow fully functional without requiring any cloud credentials.

- The default log path is `feedback_log.ndjson` in the project root. Adjust the
  path by passing a different `Path` to `build_feedback_store` in the app if
  desired.
- Entries are newline-delimited JSON objects that match the fields expected by
  the Streamlit forms, making the log easy to parse or import elsewhere.

### Manual Verification

- Use `python manual_feedback_runner.py --dry-run` to inspect the payload that
  will be written.
- Run `python manual_feedback_runner.py` to append a test record to
  `manual_feedback_log.ndjson` (or provide `--local-log` to pick a different
  path).
- Add `--show-traceback` to include a full stack trace if writing to the local
  file fails.

The `azure-feedback` branch retains the previous Azure Blob integration for when
remote logging should be re-enabled.
