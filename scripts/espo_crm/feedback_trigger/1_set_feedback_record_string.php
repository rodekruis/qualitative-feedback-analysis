<?php // Do not copy this tag into EspoCRM
// Fetch real attributes
// Fetch real attributes
$feedbackDescription = record\attribute('CFeedbackData', id, 'feedbackDescription');
$codingLevel1 = record\attribute('CFeedbackData', id, 'codingLevel1Name');    
$codingLevel2 = record\attribute('CFeedbackData', id, 'codingLevel2Name');    
$codingLevel3 = record\attribute('CFeedbackData', id, 'codingLevel3Name');    
$feedbackID = record\attribute('CFeedbackData', id, 'feedbackFormID');
$createdAt = record\attribute('CFeedbackData', id, 'createdAt');

// Clean the feedback description string
$feedbackDescription = string\replace($feedbackDescription, "\n", " ");
$feedbackDescription = string\replace($feedbackDescription, "\r", "");
$feedbackDescription = string\replace($feedbackDescription, '"', '-');
// Fill metadata JSON string
$metadata = string\concatenate(
    '{',
    '"created": "', $createdAt, '", ',
    '"coding_level_1": "', $codingLevel1, '", ',
    '"coding_level_2": "', $codingLevel2, '", ', 
    '"coding_level_3": "', $codingLevel3, '"',             
    '}'
);

// Fill record-level JSON string
$$recordString = string\concatenate(
    '{"content": "', $feedbackDescription, '", "id": "', $feedbackID, '", ', 
    '"metadata": ', $metadata, '}'
);