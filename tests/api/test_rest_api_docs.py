"""Guard `docs/rest-api/index.md` field tables against schema drift.

The documented field names must match the wire contract exactly, in both
directions: a phantom row (documented, never sent) is as much a bug as an
undocumented new field. Issue #370 was the former — `anonymize` and
`used_anonymization` outlived their removal from the schema.

Response fields are compared against a live 200 body rather than
`model_fields`, because only the wire output settles `@computed_field`
(`pretty_output`, `quality_text`) and `Field(exclude=True)`
(`ApiSummarizeBulkResponse.output_language`, absent from both).
"""

import re
from pathlib import Path

import pytest

from qfa.api.schemas import (
    ApiAnalyzeRequest,
    ApiSummarizeBulkRequest,
    ApiSummarizeRequest,
)

from .conftest import FAKE_API_KEY

DOC_PATH = Path(__file__).resolve().parents[2] / "docs" / "rest-api" / "index.md"

# doc section path -> (request model, happy-path body)
ENDPOINTS = [
    (
        "/v1/analyze-bulk",
        ApiAnalyzeRequest,
        {
            "feedback_records": [{"id": "r1", "content": "Water access was limited."}],
            "prompt": "What are the main themes?",
        },
    ),
    (
        "/v1/summarize-bulk",
        ApiSummarizeBulkRequest,
        {"feedback_records": [{"id": "r1", "content": "Water access was limited."}]},
    ),
    (
        "/v1/summarize",
        ApiSummarizeRequest,
        {"feedback_record": {"id": "doc-1", "content": "Great service!"}},
    ),
]


def _auth_header():
    return {"Authorization": f"Bearer {FAKE_API_KEY}"}


def _section(path: str) -> str:
    """Return the body of the `## POST <path> — field reference` section."""
    doc = DOC_PATH.read_text(encoding="utf-8")
    heading = f"## POST {path} — field reference"
    assert heading in doc, f"missing doc section: {heading}"
    body = doc.split(heading, 1)[1]
    # Stop at the next `## ` heading so sibling sections don't bleed in.
    return re.split(r"^## ", body, maxsplit=1, flags=re.M)[0]


def _documented_fields(path: str, subsection: str) -> set[str]:
    """Field names in the `### <subsection>` table of an endpoint section."""
    section = _section(path)
    assert f"### {subsection}" in section, f"{path}: no ### {subsection}"
    table = section.split(f"### {subsection}", 1)[1]
    table = re.split(r"^#{3} ", table, maxsplit=1, flags=re.M)[0]
    return set(re.findall(r"^\| `([^`]+)` \|", table, re.M))


@pytest.mark.parametrize(
    ("path", "model", "body"),
    ENDPOINTS,
    ids=[e[0] for e in ENDPOINTS],
)
def test_documented_request_fields_match_schema(path, model, body):
    """Every documented request field exists on the model, and vice versa."""
    assert _documented_fields(path, "Request") == set(model.model_fields)


@pytest.mark.parametrize(
    ("path", "model", "body"),
    ENDPOINTS,
    ids=[e[0] for e in ENDPOINTS],
)
@pytest.mark.asyncio
async def test_documented_response_fields_match_wire(path, model, body, client):
    """Every documented response field is returned, and vice versa."""
    resp = await client.post(path, json=body, headers=_auth_header())
    assert resp.status_code == 200, resp.text
    assert _documented_fields(path, "Response (200 OK)") == set(resp.json())
