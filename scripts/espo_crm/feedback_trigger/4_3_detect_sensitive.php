<?php // Do not copy this tag into EspoCRM

$response = $_lastHttpResponseBody;

autoSensitive = json\retrieve($response, 'is_sensitive');
autoSensitiveExplanation = json\retrieve($response, 'explanation');