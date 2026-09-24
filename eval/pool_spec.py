"""Planted counts and shared names for the analyze feedback pool.

``upload_prompts.py`` and ``upload_pool.py`` import from here so a
distribution change has one place to edit. ``DECOYS`` is the number of
records that sound like a protection concern but need no urgent referral.
``FAMILY_LABEL`` maps each prompt family to the record label or labels
that family reads. ``created`` timestamps must fall inside
``WINDOW_START`` and ``WINDOW_END`` so a newest-first sort does not
group planted records by date. The time part of ``created`` also
prevents ties in that sort.
"""

from __future__ import annotations

from datetime import datetime

RECORDS_DATASET = "feedback/records-en-v1"
VOCAB_DATASET = "feedback/vocab-v1"

WINDOW_START = datetime(2026, 6, 1, 0, 0, 0)
WINDOW_END = datetime(2026, 8, 31, 23, 59, 59)
CREATED_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

BANNED_WORDS = ("PSEA", "safeguarding", "exploitation", "referral")

# Common English words banned from vocabulary keywords because they would
# false-match ordinary text — "minor" would credit the unaccompanied-minors
# group for the words "a minor theme".
STOPLIST = ("minor", "ill", "far", "age", "single", "general")

DECOYS = 4

FAMILY_LABEL: dict[str, tuple[str, ...]] = {
    "themes": ("theme",),
    "needs": ("need",),
    "protection": ("urgent_kind", "decoy"),
    "complaints": ("complaint_about",),
    "praise": ("praise_about",),
    "rumours": ("rumour",),
    "promise_gap": ("promise_gap",),
    "access_barrier": ("access_barrier",),
    "suggestions": ("suggestion",),
    "information": ("info_request",),
}

# Record metadata field -> vocabulary "kind" for that field's values.
# ``urgent_kind`` and ``decoy`` are excluded: their keywords live on the
# records themselves, not in the shared vocabulary. ``groups`` is a list
# field, so its vocabulary kind is the singular "group".
VOCAB_LABELS: dict[str, str] = {
    label: label
    for labels in FAMILY_LABEL.values()
    for label in labels
    if label not in ("urgent_kind", "decoy")
} | {"groups": "group"}

# How many records carry each label value. ``groups`` counts a record
# once per group it lists, because ``groups`` is a list. Each
# ``urgent_kind`` value is one record.
PLANTED: dict[str, dict[str, int]] = {
    "theme": {
        "food": 30,
        "shelter": 20,
        "documents_and_legal_information": 13,
        "health": 8,
        "cash": 5,
        "water_and_hygiene": 5,
        "protection": 10,
        "praise": 6,
        "rumours": 6,
        "suggestions": 6,
        "transport": 2,
    },
    "need": {"food": 18, "shelter": 12, "health": 6, "water": 5},
    "complaint_about": {
        "food_distribution": 8,
        "cash_transfer_delays": 5,
        "sanitation_facilities": 1,
    },
    "rumour": {"cash_assistance_eligibility": 5, "resettlement_priority_list": 1},
    "suggestion": {"extend_distribution_hours": 5, "add_more_latrines": 1},
    "praise_about": {"health_post_staff": 5, "shelter_conditions": 1},
    "info_request": {"documents_and_legal_status": 10},
    "promise_gap": {
        "onward_transport_not_provided": 5,
        "resettlement_timeline_not_kept": 1,
    },
    "access_barrier": {
        "distance_to_distribution_point": 6,
        "no_wheelchair_accessible_route": 1,
    },
    "urgent_kind": {
        "child_labor_risk": 1,
        "trafficking_risk": 1,
        "minor_at_risk": 1,
        "domestic_violence": 1,
        "missing_unaccompanied_minor": 1,
        "sex_for_aid": 1,
    },
    "groups": {
        "children": 12,
        "women": 10,
        "single_headed_households": 6,
        "older_people": 6,
        "disabilities": 4,
        "chronically_ill": 4,
        "lgbtqi": 3,
        "unaccompanied_minors": 3,
        "men_and_boys": 3,
    },
}
