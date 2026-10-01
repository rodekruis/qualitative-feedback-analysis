`docs/rest-api/index.md` documents an `anonymize` request field and a `used_anonymization` response field on `/v1/analyze-bulk`. Neither field exists in `schemas.py`. Pre-existing doc rot, found while working on #362.

Update the docs to match the schema, or add the fields to the schema if they were meant to exist.
