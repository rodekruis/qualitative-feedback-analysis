<?php // Do not copy this tag into EspoCRM

// Fetch real attributes
$feedbackDescription = record\attribute('CFeedbackData', id, 'feedbackDescription');
$feedbackID = record\attribute('CFeedbackData', id, 'feedbackFormID');
$codingLevel1 = "Placeholder";    // TODO
$codingLevel2 = "Placeholder";    // TODO
$codingLevel3 = "Placeholder";    // TODO
$createdAt = record\attribute('CFeedbackData', id, 'createdAt');

// Clean the feedback description string
$feedbackDescription = string\replace($feedbackDescription, "\n", " ");
$feedbackDescription = string\replace($feedbackDescription, "\r", "");

// Fill metadata JSON string
$metadata = string\concatenate(
'{',
    '"coding_level_1": "', $codingLevel1, '", ',
    '"coding_level_2": "', $codingLevel2, '", ',
    '"coding_level_3": "', $codingLevel3, '", ',
    '"created": "', $createdAt, '", ',
    '"feedback_record_id": "', $feedbackID, '"',
'}'
);

// Fill record-level JSON string
$$recordString = string\concatenate(
    '{"content": "', $feedbackDescription, '", "id": "', $feedbackID, '", ', 
    '"metadata": ', $metadata, '}'
);


