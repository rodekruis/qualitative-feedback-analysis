<?php // Do not copy this tag into EspoCRM
// Find the related feedback records
$$backendIDs = record\findRelatedMany('CInsight', id, 'feedbackDatas', 9999, 'createdAt', 'desc');

// Initialize loop variables
$i = 0;
$recordsString = '';
$count = array\length($$backendIDs);

// Loop through real records
while($i < $count) {
  $$backendID = array\at($$backendIDs, $i);

  // Fetch feedback description early to check if it's empty
  $feedbackDescription = record\attribute('CFeedbackData', $$backendID, 'feedbackDescription');

  // 1. SKIP IF EMPTY OR NULL
  if ($feedbackDescription != null && $feedbackDescription != '') {

    $feedbackID = record\attribute('CFeedbackData', $$backendID, 'feedbackFormID');
    $codingLevel1 = record\attribute('CFeedbackData', $$backendID, 'codingLevel1Name');    
    $codingLevel2 = record\attribute('CFeedbackData', $$backendID, 'codingLevel2Name');    
    $codingLevel3 = record\attribute('CFeedbackData', $$backendID, 'codingLevel3Name');   
    $createdAt = record\attribute('CFeedbackData', $$backendID, 'createdAt');

    // Clean linebreaks from the feedback description
    $feedbackDescription = string\replace($feedbackDescription, "\n", " ");
    $feedbackDescription = string\replace($feedbackDescription, "\r", "");
    
    // 2. REPLACE DOUBLE QUOTES WITH A HYPHEN (-)
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
    $record = string\concatenate(
      '{"content": "', $feedbackDescription, '", "id": "', $feedbackID, '", ', 
      '"metadata": ', $metadata, '}'
    );

    // Add a comma between records
    // Checks if $recordsString is empty rather than relying on $i == 0
    if ($recordsString == '') {
        $recordsString = $record;
    } else {
        $recordsString = string\concatenate($recordsString, ',', $record);
    }
  }

  $i = $i + 1;
}

$$recordsString = string\concatenate('[', $recordsString, ']');