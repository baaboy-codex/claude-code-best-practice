---
name: batch
description: Run commands across multiple files in bulk
argument-hint: [operation]
user-invocable: true
---
# Batch Skill

Run commands or operations across multiple files in bulk.

## Usage
```
/batch replace "old_text" "new_text" across *.py
/batch rename "prefix_" "suffix_" across *.md
/batch delete "TODO" across *.ts
```

## Use Cases
- Bulk find and replace across a file type
- Rename prefixes/suffixes
- Delete specific patterns
- Add imports across multiple files
- Format multiple files

## Process
1. Identify files to process
2. Show preview of changes
3. Confirm before applying
4. Execute changes
5. Report results

## Safety
- Always show preview before making changes
- Allow selective exclusion of files
- Report each file's status after processing
