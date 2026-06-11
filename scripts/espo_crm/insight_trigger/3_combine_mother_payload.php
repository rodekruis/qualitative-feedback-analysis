<?php // Do not copy this tag into EspoCRM
// This creates a "mother" payload that can be used to hit any of the inference routes

$outputLanguage = record\attribute('CInsight', id, 'outputLanguage');
$prompt = record\attribute('CInsight', id, 'freeTextPrompt');

$motherPayload = string\concatenate(
    '{',
    '"feedback_records": ', $$recordsString, ', ',
    '"prompt": "', $prompt, '", ',
    '"output_language": "', $outputLanguage, '", ',
    '"selected_method": "', $$selectedMethod, '", ',
    '"endpoint": "', $$endpoint, '"',
    '}'
);