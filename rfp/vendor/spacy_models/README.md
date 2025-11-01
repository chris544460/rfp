# spaCy Models

This directory vendors the English small spaCy model `en_core_web_sm` (v3.7.1).

The model is installed in-place so it can be bundled with the project and
loaded without needing an internet connection.  If you run the tooling outside
this repository, ensure the directory is on `PYTHONPATH` or that
`SPACY_DATA` points to it:

```bash
export PYTHONPATH="$(pwd)/vendor/spacy_models:$PYTHONPATH"
# or
export SPACY_DATA="$(pwd)/vendor/spacy_models"
```

When the CLI runs from the repo root, the code automatically adds this folder
to `sys.path`, so no extra setup is needed.

Model source: <https://github.com/explosion/spacy-models/releases/tag/en_core_web_sm-3.7.1>.
