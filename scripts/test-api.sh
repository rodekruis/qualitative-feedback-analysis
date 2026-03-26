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
