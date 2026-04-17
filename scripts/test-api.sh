#!/usr/bin/env bash
# Test the health and analyze API endpoints.
# Usage: QFA_API_KEY=<key> ./scripts/test-api.sh [base_url]

set -euo pipefail

BASE_URL="${1:-${BASE_URL:-http://localhost:8000}}"

QFA_API_KEY="${QFA_API_KEY:-invalid-for-testing-12345}"

echo "Testing against ${BASE_URL}"

echo "=== Health check ==="
http GET "${BASE_URL}/v1/health"

echo ""
echo "=== Analyze ==="
http POST "${BASE_URL}/v1/analyze" \
  "Authorization:Bearer ${QFA_API_KEY}" \
  documents:='[
    {
      "id": "doc-001",
      "text": "The water distribution was well organized but we had to wait for three hours.",
      "metadata": {"region": "Eastern Province", "year": 2024}
    },
    {
      "id": "doc-002",
      "text": "Medical staff were very professional. Medicine supply was insufficient.",
      "metadata": {"region": "Northern Province", "year": 2024}
    }
  ]' \
  prompt="Summarize the main themes and sentiment of the feedback."


http POST "${BASE_URL}/v1/summarize" \
  "Authorization:Bearer ${QFA_API_KEY}" \
\
\
  feedback_items:='[
    {
      "content": "After the storm damaged the main supply line, a water distribution point was set up near the schoolyard with ropes and signs so people knew where to queue. Volunteers explained the ration clearly - two jerrycans per family per day - and the process felt orderly compared to the chaos in the first days. The main problem was the waiting time: many of us stood in line for more than three hours in the sun, including elderly people and parents with small children, and some had to leave before reaching the front because of work or caring for relatives at home. A few argued that those who arrived earliest should not lose out when the team stopped for breaks. People appreciated that distribution was organized, but the long wait made it hard for everyone to benefit fairly.",
      "id": "doc-001",
      "metadata": {
        "coding_level_1": "Water",
        "coding_level_2": "Distribution",
        "coding_level_3": "Waiting times",
        "created": "2024-06-01T12:00:00Z",
        "feedback_item_id": "fi-001"
      }
    },
    {
      "content": "During the mobile clinic in the settlement after the floods, the medical staff treated people with respect and explained things clearly; several of us felt reassured even though we had waited most of the morning in the heat. The nurses worked steadily and the doctor listened properly before prescribing. What frustrated many families was that essential medicines ran out before midday - especially antibiotics and chronic medication for older people - so some had to leave with prescriptions but no drugs, and others were told to come back the next day without any guarantee that stock would arrive. A few parents said their childrens fever had still not been checked by the time the team packed up. Overall the care was professional, but unless supplies match the number of people, the visit feels incomplete and people lose trust in follow-up.",
      "id": "doc-002",
      "metadata": {
        "coding_level_1": "Health",
        "coding_level_2": "Staff",
        "coding_level_3": "Supplies",
        "created": "2024-06-02T09:30:00Z",
        "feedback_item_id": "fi-002"
      }
    }
  ]' \
  output_language="English" \
  prompt="Focus on operational issues and beneficiary experience."
