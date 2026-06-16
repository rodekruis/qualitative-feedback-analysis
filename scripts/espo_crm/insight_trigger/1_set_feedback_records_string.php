<?php // Do not copy this tag into EspoCRM
// Find the related feedback records
$$backendIDs = record\findRelatedMany('CInsight', id, 'feedbackDatas', 9999,'createdAt', 'desc');

// Initialize loop variables
$i = 0;
$recordsString = '';
$count = array\length($$backendIDs);

// Loop through real records
while($i < $count) {
  $$backendID = array\at($$backendIDs, $i);

  // Fetch real attributes
  $feedbackDescription = record\attribute('CFeedbackData', $$backendID, 'feedbackDescription');
  $feedbackID = record\attribute('CFeedbackData', $$backendID, 'feedbackFormID');
  $codingLevel1Id = codingLevel1Id;
  $codingLevel1 = record\attribute('CCodingLevel1', $codingLevel1, 'name');
  $codingLevel2Id = codingLevel2Id;
  $codingLevel2 = record\attribute('CCodingLevel2', $codingLevel2, 'name');
  $codingLevel3Id = codingLevel3Id;
  $codingLevel3 = record\attribute('CCodingLevel3', $codingLevel3, 'name');
  $createdAt = record\attribute('CFeedbackData', $$backendID, 'createdAt');

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
  $record = string\concatenate(
      '{"content": "', $feedbackDescription, '", "id": "', $feedbackID, '", ', 
      '"metadata": ', $metadata, '}'
  );

  // Add a comma between records, except first one
  if ($i == 0) {
      $recordsString = $record;
  } else {
      $recordsString = string\concatenate($recordsString, ',', $record);
  }

  $i = $i + 1;
}

$$recordsString = string\concatenate('[', $recordsString, ']');

