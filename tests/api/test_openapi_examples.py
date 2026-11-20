"""Swagger request examples must stay valid for their request models (#425)."""

import pytest

from qfa.api.openapi_examples import (
    ANALYZE_BULK_EXAMPLES,
    ASSIGN_CODES_EXAMPLES,
    DETECT_SENSITIVE_EXAMPLES,
    SUMMARIZE_BULK_EXAMPLES,
    SUMMARIZE_COMMUNITY_MEETING_EXAMPLES,
    SUMMARIZE_EXAMPLES,
)
from qfa.api.schemas import (
    ApiAnalyzeRequest,
    ApiAssignCodesRequest,
    ApiBulkInferenceRequestBase,
    ApiDetectSensitiveRequest,
    ApiSummarizeBulkRequest,
    ApiSummarizeCommunityMeetingRequest,
    ApiSummarizeRequest,
)

ENDPOINTS = {
    "/v1/analyze-bulk": (ApiAnalyzeRequest, ANALYZE_BULK_EXAMPLES),
    "/v1/summarize-bulk": (ApiSummarizeBulkRequest, SUMMARIZE_BULK_EXAMPLES),
    "/v1/summarize": (ApiSummarizeRequest, SUMMARIZE_EXAMPLES),
    "/v1/summarize-community-meeting": (
        ApiSummarizeCommunityMeetingRequest,
        SUMMARIZE_COMMUNITY_MEETING_EXAMPLES,
    ),
    "/v1/assign-codes": (ApiAssignCodesRequest, ASSIGN_CODES_EXAMPLES),
    "/v1/detect-sensitive": (ApiDetectSensitiveRequest, DETECT_SENSITIVE_EXAMPLES),
}

CASES = [
    (path, name, model, example["value"])
    for path, (model, examples) in ENDPOINTS.items()
    for name, example in examples.items()
]


@pytest.mark.parametrize(
    ("path", "name", "model", "value"),
    CASES,
    ids=[f"{c[0]}:{c[1]}" for c in CASES],
)
def test_example_is_valid_for_request_model(path, name, model, value):
    model.model_validate(value)


@pytest.mark.parametrize("path", ENDPOINTS)
def test_every_endpoint_offers_a_choice(path):
    assert len(ENDPOINTS[path][1]) >= 2


@pytest.mark.parametrize(
    ("model", "examples"),
    [
        (ApiAnalyzeRequest, ANALYZE_BULK_EXAMPLES),
        (ApiSummarizeBulkRequest, SUMMARIZE_BULK_EXAMPLES),
    ],
)
def test_bulk_default_example_mixes_linked_record_types(
    model: type[ApiBulkInferenceRequestBase], examples
):
    request = model.model_validate(next(iter(examples.values()))["value"])

    records = request.feedback_records
    assert {r.record_type for r in records} == {"feedback", "community_meeting"}
    assert all(r.url_id for r in records)
    assert request.espo_feedback_base_url
    assert request.espo_meeting_base_url


def test_openapi_exposes_examples_for_every_endpoint(test_app):
    spec = test_app.openapi()

    for path, (_, examples) in ENDPOINTS.items():
        content = spec["paths"][path]["post"]["requestBody"]["content"]
        assert list(content["application/json"]["examples"]) == list(examples)
