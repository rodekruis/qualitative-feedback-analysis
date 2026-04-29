# Ubiquitous Language

This document defines the language used in the QFA project.

The defined terms must be used consistently throughout the project, in code, documentation,
user and developer communication.

This ensures that everybody speaks the same language, and the same concept has the same
name in all contexts such as code, documentation, dashboards and training material. This document does not attempt to standardise local job titles or local labels used by National Societies; it standardises system-level concepts.

## Definitions

| Preferred term | Rationale | Avoid / replace with |
|---------------|-----------|----------------------|
| Feedback record | Neutral and system‑agnostic; works well for qualitative, unstructured data. A feedback record contains the feedback description and other metadata and represents one coherent unit of feedback. | Document, case, ticket, entry, item |
| Feedback | The umbrella term in IFRC guidance, covering all types of input from community members; not a type by itself. | Question, complaint, case |
| Feedback description | Qualitative, unstructured content that focuses on meaning rather than channel or format and avoids channel‑specific framing. | Message, note, transcript |
| Feedback source | Refers to who or what generated the feedback (human or technical), independent of how it entered the system. | Channel, platform (only) |
| Feedback metadata | Contextual attributes that describe a feedback record for governance, analysis, and responsibility, including operational and ethical flags. | Extra fields, system data |
| Feedback dataset | Analytical abstraction that supports aggregation, filtering, and ML workflows across storage backends; may be derived or time‑bound. | Database, table |
| Coding | Standard qualitative analysis practice emphasising interpretation; a single feedback record may be coded multiple times. | Tagging only |
| Coding framework | Flexible, evolving structure for organising codes; supports iteration and learning over fixed classification. | Taxonomy, list of tags |
| Auto‑coding | Tool‑neutral term for automated analysis; produces suggested codes that require human validation. | AI tagging |
| Manual coding | Explicitly denotes human interpretation, enabling clear mixed human–AI workflows. | Human tagging |
| Community member | This is the person giving feedback to a Red Cross/Red Crescent National Society. People‑centred term aligned with IFRC CEA commitments, emphasising rights and participation; never referred to as a user. | Beneficiary, client |
| QFA user | Umbrella term for any person using QFA outputs or workflows to analyse feedback and produce insights, clearly distinct from community members. | Operator, end‑user (generic), beneficiary, client |
| Insight | A human‑interpretable analytical output derived from patterns across multiple feedback records; provisional and subject to revision. | Finding (too vague), conclusion (too strong) |

## Feedback types
A single feedback record may fall under multiple feedback types.

1. Question
Requests for information or clarification.
2. Suggestion
Recommendations or ideas to improve services, communication, or processes. [preparecenter.org]
3. Concern
Expressions of worry or perceived problems that may require follow-up. [preparecenter.org]
4. Complaint
Explicit negative feedback about services, behaviour, access, or quality. [preparecenter.org]
5. Rumour / misinformation / misconception
Unverified or incorrect information circulating in communities that may affect behaviour, access, safety, or trust.

Any feedback record, regardless of type, may carry a sensitivity flag, indicating that it requires special handling due to protection, safeguarding, or ethical considerations.

## QFA language conventions

- Use **feedback** as the umbrella term for all input from community members.
- Each individual input is a **feedback record**.
- Feedback records are people-centred, not organisation-centred.
- Avoid ticket, case, or call-centre language.
- Language should be neutral, non-judgmental and channel-agnostic.

## Analysis and processing terminology

- Feedback is **coded**, not tagged.
- Coding may be **manual** or **auto-coded**.
- Analysis focuses on **themes**, **trends**, and **insights**, not single records only.


## Avoid the following terms

- Case (legal / protection connotation)
- Ticket (call-centre framing)
- Beneficiary complaint (disempowering, outdated)
- User (for community members)
- Noise or irrelevant feedback
