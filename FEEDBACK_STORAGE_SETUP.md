# Azure Feedback Storage Configuration

The Streamlit app now writes user feedback through `feedback_storage.FeedbackStore`,
which prefers Azure Blob Storage and falls back to a local CSV file. Configure the
following to persist feedback in Azure.

- Set the environment variables below for the Streamlit runtime:
  - `AZURE_FEEDBACK_CONNECTION_STRING`: your storage account connection string.
  - `AZURE_FEEDBACK_CONTAINER`: target container name (for example, `rfp-feedback`).
  - `AZURE_FEEDBACK_BLOB`: append-blob name that will hold the CSV (for example, `feedback-log.csv`).
- Ensure the container exists. The app creates it automatically when permissions allow.
- The app also creates the append blob and writes the CSV header on first run.

### Minimal Azure Tasks

1. Create (or reuse) a Storage Account that supports append blobs.
2. Create a container for feedback (access level **Private** is recommended).
3. Generate a connection string with write permissions for the container.
4. Provide the three environment variables above to the Streamlit process.

With these values set, feedback submissions append rows directly to the Azure blob.
If the configuration is missing or invalid, the app saves feedback locally to
`feedback_log.csv` so you can troubleshoot without data loss.
