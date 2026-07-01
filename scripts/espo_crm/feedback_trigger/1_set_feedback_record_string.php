<?php // Do not copy this tag into EspoCRM

// Fetch real attributes
$feedbackDescription = record\attribute('CFeedbackData', id, 'feedbackDescription');
$feedbackID = record\attribute('CFeedbackData', id, 'feedbackFormID');
$codingLevel1 = record\attribute('CFeedbackData', id, 'codingLevel1');    
$codingLevel2 = record\attribute('CFeedbackData', id, 'codingLevel2');    
$codingLevel3 = record\attribute('CFeedbackData', id, 'codingLevel3');    
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
'}'
);

// Fill record-level JSON string
$$recordString = string\concatenate(
    '{"content": "', $feedbackDescription, '", "id": "', $feedbackID, '", ', 
    '"metadata": ', $metadata, '}'
);


