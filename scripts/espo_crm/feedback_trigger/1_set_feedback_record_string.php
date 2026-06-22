<?php // Do not copy this tag into EspoCRM

// Fetch real attributes
$feedbackDescription = record\attribute('CFeedbackData', id, 'feedbackDescription');
$feedbackID = record\attribute('CFeedbackData', id, 'feedbackFormID');
$createdAt = record\attribute('CFeedbackData', id, 'createdAt');

// Clean the feedback description string
$feedbackDescription = string\replace($feedbackDescription, "\n", " ");
$feedbackDescription = string\replace($feedbackDescription, "\r", "");

// Fill metadata JSON string
$metadata = string\concatenate(
'{',
    '"created": "', $createdAt, '", ',
    '"feedback_record_id": "', $feedbackID, '"',
'}'
);

// Fill record-level JSON string
$$recordString = string\concatenate(
    '{"content": "', $feedbackDescription, '", "id": "', $feedbackID, '", ', 
    '"metadata": ', $metadata, '}'
);


