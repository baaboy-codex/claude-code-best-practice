---
name: book-fetcher
description: Instructions for extracting book metadata and structure
user-invocable: false
---
# Book Fetcher Skill

Instructions for extracting book metadata and structure.

## Task

Extract metadata from a book file (PDF, EPUB, or TXT).

## Supported Formats

| Format | Extraction Method |
|--------|------------------|
| PDF | Parse with pdfplumber or PyPDF2 |
| EPUB | Parse with epublib |
| TXT | Direct file reading |

## Extraction Steps

1. **Identify file type** from extension
2. **Extract metadata**:
   - Title
   - Author
   - Number of chapters/sections
   - Table of contents (if available)
3. **Return structured data**

## Expected Fields

```json
{
  "title": "string",
  "author": "string (default: 'Unknown')",
  "totalChapters": "number",
  "chapters": ["array of chapter names"],
  "fileType": "pdf|epub|txt",
  "filePath": "string"
}
```

## Notes
- Set user-invocable: false (agent-only domain knowledge)
- Do NOT summarize content, only extract metadata
- Handle missing metadata gracefully with defaults
