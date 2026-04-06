---
name: book-summary
description: Generate a book summary using the distillation pipeline
argument-hint: [book-path-or-url]
---
# Book Summary Command

Generate a structured summary of a book using the distillation pipeline.

## Workflow (Command → Agent → Skill)

1. **Command** (this): Orchestrates the workflow
   - Asks user for book path or URL
   - Invokes `book-agent` via Agent tool
   - Invokes `book-summary-creator` via Skill tool

2. **Agent** (book-agent): Fetches book metadata
   - Extracts title, author, chapter structure
   - Returns metadata to command

3. **Skill** (book-summary-creator): Creates summary
   - Generates structured summary from book content
   - Writes summary to output file

## Data Contract

```json
{
  "title": "Book Title",
  "author": "Author Name",
  "chapters": ["Chapter 1", "Chapter 2"],
  "summary": "Book summary text..."
}
```

## Usage
```
/book-summary path/to/book.pdf
/book-summary https://example.com/book
```
