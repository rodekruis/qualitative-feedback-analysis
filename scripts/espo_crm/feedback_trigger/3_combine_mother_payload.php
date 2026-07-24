<?php // Do not copy this tag into EspoCRM

// Detect sensitive
// Codes
// Summarize one item

// SUMMARIZE
$motherPayload = string\concatenate(
    '{',
    '"feedback_record": ', $$recordString, ', ',
    '"confidence_threshold": 0.1 ',
    '}'
);
$baseUrl= ext\appSecret\get('QFA_API_BASE_URL');
$urlSummarize = string\concatenate($baseUrl, '/v1/summarize');

// ASSIGN CODES
$motherPayload = string\concatenate(
    '{',
    '"feedback_record": ', $$recordString, ', ',
    '"coding_levels": {"root_codes": ', $$codesString, '}, ',
    '"max_codes": 1, ',
    '"confidence_threshold": 0.1 ',
    '}'
);
$baseUrl= ext\appSecret\get('QFA_API_BASE_URL');
$urlAssignCodes = string\concatenate($baseUrl, '/v1/assign-codes');

// DETECT SENSITIVE
$motherPayload = string\concatenate(
    '{',
    '"feedback_record": ', $$recordString, ', ',
    '"confidence_threshold": 0.1 ',
    '}'
);

$baseUrl= ext\appSecret\get('QFA_API_BASE_URL');
$urlDetectSensitive  = string\concatenate($baseUrl, '/v1/detect-sensitive');