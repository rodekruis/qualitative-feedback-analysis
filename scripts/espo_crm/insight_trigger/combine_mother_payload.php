<?php // Do not copy this tag into EspoCRM
// This creates a "mother" payload that can be used to hit any of the inference routes

// TODO: use fields from UI
$outputLanguage = "English";
$prompt = "No prompt provided";

$motherPayload = string\concatenate(
    '{',
    '"feedback_records": ', $$recordsString, ', ',
    '"coding_framework": ', $$codesString, ', ',
    '"anonymize": true, ',
    '"prompt": "', $prompt, '", ',
    '"output_language": "', $language, '", ',
    '"max_codes": 10, ',
    '"confidence_threshold": 0.7',
    '}'
);