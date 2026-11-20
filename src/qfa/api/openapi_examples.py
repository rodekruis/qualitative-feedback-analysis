"""Pre-filled request bodies for the Swagger UI "Try it out" dropdown (#425).

Each route passes one of these dicts to ``Body(openapi_examples=...)``. The
first entry is what Swagger pre-fills. Every record is made up: never paste
real EspoCRM feedback or meeting notes here. ``tests/api/test_openapi_examples.py``
validates every example against its request model.
"""

from typing import Any, cast

from fastapi.openapi.models import Example
from pydantic import BaseModel

from qfa.api.schemas import (
    ApiAnalyzeRequest,
    ApiAssignCodesRequest,
    ApiSummarizeRequest,
)

_FEEDBACK_BASE_URL = "https://espocrm.example.org/#CFeedbackData/view"
_MEETING_BASE_URL = "https://espocrm.example.org/#CCommunityMeetingData/view"


def _schema_examples(model: type[BaseModel]) -> list[dict[str, Any]]:
    """The model's own ``json_schema_extra`` examples, kept as simple choices."""
    extra = cast(dict[str, Any], model.model_config.get("json_schema_extra", {}))
    return cast(list[dict[str, Any]], extra.get("examples", []))


def _feedback(
    id: str, url_id: str, created: str, codes: tuple[str, str, str], content: str
) -> dict[str, Any]:
    return {
        "record_type": "feedback",
        "id": id,
        "url_id": url_id,
        "content": content,
        "metadata": {
            "created": created,
            "coding_level_1": codes[0],
            "coding_level_2": codes[1],
            "coding_level_3": codes[2],
        },
    }


def _meeting(
    id: str, url_id: str, metadata: dict[str, str], notes: str
) -> dict[str, Any]:
    return {
        "record_type": "community_meeting",
        "id": id,
        "url_id": url_id,
        "meetingNotes": notes,
        "metadata": metadata,
    }


_MIXED_RECORDS: list[dict[str, Any]] = [
    _feedback(
        "Form-10231",
        "6650a1b2c3d4e5f601",
        "2025-03-03T08:14:00Z",
        ("Request or suggestion", "Water", "Waiting times"),
        "We queue at the water point behind the school from six in the morning."
        " The tank is often empty by ten, so families who work in the fields"
        " get nothing. Please bring a second truck or fill the tank twice.",
    ),
    _feedback(
        "Form-10232",
        "6650a1b2c3d4e5f602",
        "2025-03-04T11:40:00Z",
        ("Encouragement or praise", "Health", "Mobile clinic"),
        "Thank you for the mobile clinic on Tuesday. The nurse explained the"
        " medicine for my son very clearly. But they ran out of paracetamol"
        " before noon and many people went home without it.",
    ),
    _feedback(
        "Form-10235",
        "6650a1b2c3d4e5f603",
        "2025-03-06T16:05:00Z",
        ("Question", "Cash assistance", "Eligibility"),
        "My neighbour received the cash transfer and I did not, but we have the"
        " same family size. Nobody told me why. Who can I ask about the list?",
    ),
    _feedback(
        "Form-10240",
        "6650a1b2c3d4e5f604",
        "2025-03-10T09:22:00Z",
        ("Request or suggestion", "Shelter", "Repairs"),
        "The tarpaulins from December are torn after the wind last week. Rain"
        " comes into the tent at night. Older people in block C are sick.",
    ),
    _meeting(
        "CM-00412",
        "6650a1b2c3d4e5f6a1",
        {
            "created": "2025-03-05T14:00:00Z",
            "dateOfMeeting": "2025-03-05",
            "methodOfCollection": "Community meeting",
            "format": "In person",
            "project": "Flood response 2025",
            "location": "Riverside settlement, block B",
            "ageGroup": "18-59",
            "gender": "Mixed",
            "groupSize": "21-50",
        },
        "About 35 residents attended. Main topic: water. People said the tank"
        " empties before mid-morning and that the queue is long and unsafe for"
        " children. Women asked for separate queue times. Second topic: cash."
        " Several people do not understand how the beneficiary list was made"
        " and asked for a public explanation. Volunteers agreed to share the"
        " criteria at the next meeting.",
    ),
    _meeting(
        "CM-00418",
        "6650a1b2c3d4e5f6a2",
        {
            "created": "2025-03-12T10:30:00Z",
            "dateOfMeeting": "2025-03-12",
            "methodOfCollection": "Focus group discussion",
            "format": "In person",
            "project": "Flood response 2025",
            "location": "Riverside settlement, block C",
            "ageGroup": "60+",
            "gender": "Female",
            "groupSize": "11-20",
        },
        "Fourteen older women took part. Shelter is their biggest worry: torn"
        " tarpaulins, cold nights and coughing. They thanked the mobile clinic"
        " team but said the clinic is too far to walk to for those with"
        " walking problems. They asked for a home visit once a month.",
    ),
]


_MEETING_NOTES = """\
<p><strong>Community meeting - Riverside settlement, block B</strong></p>
<p>Facilitators: two Red Cross volunteers. Attendance: about 40 people, mostly
women with children, some older men. The village leader opened the meeting.</p>
<p><strong>1. Water</strong></p>
<ul>
<li>The water tank behind the school is empty by mid-morning most days.</li>
<li>People who work in the fields in the morning come back to an empty tank.</li>
<li>Queues start before sunrise. Parents worry about children who wait alone
in the dark.</li>
<li>Request: a second delivery in the afternoon, and separate queue times for
women.</li>
</ul>
<p><strong>2. Cash assistance</strong></p>
<ul>
<li>Several families do not know why they are not on the beneficiary list.</li>
<li>Some think the list was made by the leader's family. Volunteers said the
list follows household criteria, but people asked to see them in writing.</li>
<li>Action: the cash team will present the criteria at the next meeting.</li>
</ul>
<p><strong>3. Health</strong></p>
<ul>
<li>People are happy with the mobile clinic but it runs out of paracetamol
and oral rehydration salts.</li>
<li>Older people cannot walk to the clinic site. Request for home visits.</li>
</ul>
<p><strong>4. Shelter</strong></p>
<ul>
<li>Tarpaulins from December are torn. Rain comes in at night.</li>
</ul>
<p>Next meeting: in two weeks, same place. Volunteers will report back on water
and cash.</p>
"""


ANALYZE_BULK_EXAMPLES: dict[str, Example] = {
    "mixed_espocrm": {
        "summary": "Feedback and community meetings, with record links",
        "description": (
            "Four feedback records and two community meetings, as the EspoCRM"
            " flowchart sends them. Record ids in the output become links."
        ),
        "value": {
            "feedback_records": _MIXED_RECORDS,
            "prompt": (
                "What are the main problems people report, and which ones come"
                " up in both feedback and community meetings?"
            ),
            "mode": "single_pass",
            "period": "week",
            "output_language": "en",
            "espo_feedback_base_url": _FEEDBACK_BASE_URL,
            "espo_meeting_base_url": _MEETING_BASE_URL,
        },
    },
    "simple": {
        "summary": "Two short feedback records",
        "value": _schema_examples(ApiAnalyzeRequest)[0],
    },
}

SUMMARIZE_BULK_EXAMPLES: dict[str, Example] = {
    "mixed_espocrm": {
        "summary": "Feedback and community meetings, with record links",
        "description": (
            "Four feedback records and two community meetings. Record ids in"
            " the summary become links."
        ),
        "value": {
            "feedback_records": _MIXED_RECORDS,
            "output_language": "en",
            "espo_feedback_base_url": _FEEDBACK_BASE_URL,
            "espo_meeting_base_url": _MEETING_BASE_URL,
        },
    },
    "simple": {
        "summary": "Two short feedback records",
        "value": {
            "feedback_records": [
                {"id": "doc-001", "content": "The water point is always empty."},
                {"id": "doc-002", "content": "The clinic staff were very kind."},
            ],
        },
    },
}

SUMMARIZE_EXAMPLES: dict[str, Example] = {
    "long_feedback": {
        "summary": "One long feedback record",
        "value": _schema_examples(ApiSummarizeRequest)[0],
    },
    "short_feedback": {
        "summary": "One short feedback record",
        "value": {
            "feedback_record": {
                "id": "Form-10232",
                "content": "The mobile clinic ran out of paracetamol before noon.",
            },
        },
    },
}

SUMMARIZE_COMMUNITY_MEETING_EXAMPLES: dict[str, Example] = {
    "long_meeting_notes": {
        "summary": "Long HTML meeting notes, with a record link",
        "description": (
            "Notes as EspoCRM's rich-text editor stores them. The HTML is"
            " reduced to plain text before the model sees it."
        ),
        "value": {
            "community_meeting_record": {
                "id": "CM-00412",
                "url_id": "6650a1b2c3d4e5f6a1",
                "meetingNotes": _MEETING_NOTES,
                "metadata": {
                    "created": "2025-03-05T14:00:00Z",
                    "dateOfMeeting": "2025-03-05",
                    "methodOfCollection": "Community meeting",
                    "format": "In person",
                    "project": "Flood response 2025",
                    "location": "Riverside settlement, block B",
                    "ageGroup": "18-59",
                    "gender": "Mixed",
                    "groupSize": "21-50",
                },
            },
            "espo_meeting_base_url": _MEETING_BASE_URL,
        },
    },
    "short_meeting_notes": {
        "summary": "Short plain-text notes",
        "value": {
            "community_meeting_record": {
                "id": "CM-00418",
                "meetingNotes": (
                    "Fourteen older women attended. Torn tarpaulins and cold"
                    " nights are the main worry. They asked for home visits."
                ),
            },
        },
    },
}

_assign_codes_simple = _schema_examples(ApiAssignCodesRequest)

ASSIGN_CODES_EXAMPLES: dict[str, Example] = {
    "realistic_feedback": {
        "summary": "Feedback record with the bundled coding framework",
        "value": {
            "coding_levels": _assign_codes_simple[0]["coding_levels"],
            "feedback_record": {
                "id": "Form-10235",
                "content": (
                    "My neighbour received the cash transfer and I did not, but"
                    " we have the same family size. Nobody told me why. Who can"
                    " I ask about the list?"
                ),
                "metadata": {"created": "2025-03-06T16:05:00Z"},
            },
            "max_codes": 3,
            "confidence_threshold": 0.3,
        },
    },
    "covid_quote": {
        "summary": "Short quote from the COVID-19 codebook",
        "value": _assign_codes_simple[0],
    },
}

DETECT_SENSITIVE_EXAMPLES: dict[str, Example] = {
    "sensitive": {
        "summary": "Feedback that reports misconduct",
        "description": "The response lists sensitivity types.",
        "value": {
            "feedback_record": {
                "id": "Form-10251",
                "content": (
                    "A man who hands out the food vouchers told me I would only"
                    " get my family's voucher if I paid him 20 dollars. When I"
                    " said I had no money he said I could come to his house in"
                    " the evening instead. Other women in block A say the same."
                    " I am afraid to report it because he is the cousin of the"
                    " camp leader."
                ),
                "metadata": {"created": "2025-03-14T07:50:00Z"},
            },
        },
    },
    "not_sensitive": {
        "summary": "Ordinary feedback",
        "value": {
            "feedback_record": {
                "id": "Form-10240",
                "content": "The tarpaulins are torn and rain comes into the tent.",
            },
        },
    },
}
