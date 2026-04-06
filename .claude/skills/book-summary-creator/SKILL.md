---
name: book-summary-creator
description: Creates a structured summary of a book
---
# Book Summary Creator Skill

Create a structured summary of a book from its content.

## Task

Generate a comprehensive but concise summary of the book content.

## Input (from conversation context)

```json
{
  "title": "Book Title",
  "author": "Author Name",
  "chapters": ["Chapter 1", "Chapter 2", ...],
  "content": "Full book content or chapter summaries..."
}
```

## Output

Write a structured summary to `summary.md` with:

### 1. Book Overview
- Title, author
- Publication year (if known)
- Genre/category

### 2. Core Thesis
- Main argument or theme
- Key contribution

### 3. Chapter Summary
- Brief summary of each chapter
- Key takeaways per chapter

### 4. Key Insights
- Top 5-10 insights from the book
- Actionable takeaways

### 5. Quotes
- Notable quotes (with page references if available)

## Output Format

```markdown
# Book Title

## Overview
...

## Core Thesis
...

## Chapter Summaries
### Chapter 1: ...
### Chapter 2: ...

## Key Insights
1. ...
2. ...

## Notable Quotes
...
```

## Notes
- Summarize concisely, don't transcribe
- Focus on actionable insights
- Write to `summary.md` in current directory
