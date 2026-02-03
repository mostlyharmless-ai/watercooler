---
name: pdf-to-md
description: Extract PDFs to structured markdown reference files using Claude's multimodal PDF reader
allowed-tools:
  - Task
  - Glob
  - Bash(mkdir *)
  - Bash(ls *)
  - Bash(basename *)
---

# PDF to Markdown Skill

Extract PDF documents to structured markdown reference files using Claude's native multimodal PDF reading capability. Produces high-quality markdown with proper math symbol preservation (LaTeX notation).

## Usage

```
/pdf-to-md <path>
```

**Examples**:
- `/pdf-to-md docs/lyon-2024-carfac-v2.pdf` — Single PDF
- `/pdf-to-md docs/` — All PDFs in directory
- `/pdf-to-md docs/*2024*.pdf` — Glob pattern

---

## Workflow Overview

```
┌─────────────────────────────────────────────────────────────────┐
│  1. Resolve input (single file, directory, or glob pattern)    │
│  2. Determine output location (threads repo refs/ directory)   │
│  3. Process EACH PDF SERIALLY (one at a time!):                │
│     a. Read PDF                                                 │
│     b. Write markdown immediately                               │
│     c. Move to next PDF                                         │
│  4. Report results                                              │
└─────────────────────────────────────────────────────────────────┘
```

## CRITICAL: Context Isolation via Subagents

**PDFs are large and will fill context.** Even serial processing accumulates content.

**Solution**: Spawn a **Task subagent for each PDF**. Each subagent:
1. Gets fresh context (no accumulated content)
2. Reads the PDF
3. Writes the markdown
4. Returns a brief result (title, output path, success/failure)
5. Its context is discarded — PDF content does not accumulate

```
For each PDF:
    Task(subagent_type="general-purpose", prompt="Extract <pdf> to <output>")
    → Subagent reads PDF, writes markdown, returns summary
    → Context flushed automatically
```

**DO NOT**:
- Read PDFs directly in the main conversation
- Read multiple PDFs in parallel
- Process PDFs serially without subagents (context still accumulates)

---

## Phase 1: Resolve Input

### 1.1 Single PDF

If input is a `.pdf` file path:
```bash
ls -la <path>
```

### 1.2 Directory

If input is a directory, find all PDFs:
```bash
ls <path>/*.pdf 2>/dev/null
```

### 1.3 Glob Pattern

Use Glob tool to resolve pattern:
```
Glob: <pattern>
```

---

## Phase 2: Determine Output Location

Markdown files go to the corresponding watercooler threads repository in a `refs/` subdirectory.

### 2.1 Find Threads Repository

The threads repo is parallel to the code repo with `-threads` suffix:

| Code Project | Threads Repo |
|--------------|--------------|
| `/home/user/projects/Gist` | `/home/user/projects/Gist-threads` |
| `/home/user/projects/watercooler-cloud` | `/home/user/projects/watercooler-cloud-threads` |

```bash
# Get current project directory name
basename $(pwd)

# Check if threads repo exists
ls -d ../$(basename $(pwd))-threads 2>/dev/null
```

### 2.2 Create refs/ Directory

```bash
mkdir -p /path/to/project-name-threads/refs
```

### 2.3 Fallback

If no `-threads` repo exists, create `refs/` in the current project:
```bash
mkdir -p refs
```

---

## Phase 3: Extract Each PDF via Subagent

For each PDF, spawn a Task subagent to handle extraction in isolated context.

### 3.1 Subagent Prompt Template

```
Task(
  subagent_type="general-purpose",
  description="Extract PDF to markdown",
  prompt="""
Extract this PDF to a structured markdown reference file.

**Input PDF**: <absolute-path-to-pdf>
**Output file**: <absolute-path-to-output-md>

Instructions:
1. Read the PDF using the Read tool
2. Extract metadata: title, authors, year, source (arXiv/DOI/URL)
3. Write a markdown file with this structure:

# <Paper Title>

**Authors**: <author list>
**Year**: <publication year>
**Source**: <arXiv ID, DOI, or URL>
**PDF**: <relative path to PDF>

---

<Full paper content, preserving section structure>

4. Return a JSON summary: {"title": "...", "output": "...", "success": true/false, "error": "..."}
"""
)
```

### 3.2 What the Subagent Does

The subagent has fresh context and will:
- Read the PDF using Claude's multimodal reader
- Interpret pages visually (not just text layer extraction)
- Preserve math as LaTeX: `$inline$` or `$$block$$`
- Maintain section hierarchy
- Describe figures and tables
- Write the markdown file
- Return a brief summary

### 3.3 Process Each PDF Sequentially

```python
results = []
for pdf in pdf_list:
    output_path = refs_dir / f"ref-{slugify(pdf.stem)}.md"

    # Spawn subagent - its context is isolated
    result = Task(
        subagent_type="general-purpose",
        description=f"Extract {pdf.name}",
        prompt=f"Extract {pdf} to {output_path} ..."
    )

    results.append(result)
    # Subagent context is discarded here - PDF content gone
```

### 3.4 Collect Results

Each subagent returns a brief summary. Aggregate these for the final report.

**Correct pattern**:
```
Task(PDF 1) → returns summary → Task(PDF 2) → returns summary → ...
```

**WRONG patterns**:
```
Read PDF 1, PDF 2, PDF 3 directly (context overflow)
Task(PDF 1, PDF 2, PDF 3) in parallel (still too much)
```

---

## Phase 4: Write Markdown File

### 4.1 Naming Convention

**Format**: `ref-<paper-title-in-kebab-case>.md`

- Prefix: `ref-` (for "reference")
- Title: Paper title converted to lowercase kebab-case
- Extension: `.md`
- Max length: 60 characters for title portion

**Examples**:

| PDF Filename | Paper Title | Markdown Filename |
|--------------|-------------|-------------------|
| `lyon-2024-carfac-v2.pdf` | "CARFAC v2" | `ref-carfac-v2.md` |
| `roche-2018-autoencoders-music-comparison.pdf` | "Autoencoders for Music: A Comparison" | `ref-autoencoders-for-music-a-comparison.md` |
| `hasani-2020-liquid-time-constant.pdf` | "Liquid Time-constant Networks" | `ref-liquid-time-constant-networks.md` |

### 4.2 Markdown Structure

```markdown
# <Paper Title>

**Authors**: <author list>
**Year**: <publication year>
**Source**: <arXiv ID, DOI, or URL>
**PDF**: <relative path to PDF>

---

## Abstract

<abstract text>

## 1. Introduction

<section content>

[Rest of paper sections, preserving original structure]

## References

<bibliography>
```

### 4.3 Content Guidelines

- Preserve section hierarchy (use ## for main sections, ### for subsections)
- Format equations as LaTeX: `$inline$` or `$$block$$`
- Describe figures: `**Figure N**: <description of what the figure shows>`
- Format tables using markdown table syntax where possible
- Preserve code snippets in fenced code blocks with language tags
- Keep lists and enumerations intact

---

## Phase 5: Report Results

```
## PDF Extraction Complete

### Extracted (N files)

| PDF | Markdown | Title |
|-----|----------|-------|
| `docs/lyon-2024-carfac-v2.pdf` | `refs/ref-carfac-v2.md` | CARFAC v2 |
| `docs/roche-2018-autoencoders.pdf` | `refs/ref-autoencoders-for-music.md` | Autoencoders for Music |

### Output Location
- Markdown files written to: `<project>-threads/refs/`

### Extraction Notes
- Math symbols preserved as LaTeX notation
- Figures described in context
- Tables converted to markdown format

### Skipped (N files)

| PDF | Reason |
|-----|--------|
| `docs/corrupted.pdf` | Failed to read: file corrupted |
| `docs/scanned-old.pdf` | Scanned image, low quality OCR |
```

---

## Error Handling

| Error | Response |
|-------|----------|
| **File not found** | Subagent reports error, continue with others |
| **Not a PDF** | Skip non-PDF files, note in report |
| **Corrupted PDF** | Subagent reports error, continue with others |
| **Scanned PDF (image-only)** | Subagent extracts what's possible, notes quality limitation |
| **Very large PDF (>100 pages)** | Subagent may truncate, notes in result |
| **Single PDF exceeds subagent context** | Subagent fails gracefully, reports error |
| **Context overflow in main** | You're not using subagents - STOP, use Task tool |
| **No threads repo** | Use local `refs/` directory as fallback |
| **Permission denied** | Subagent reports error, suggest checking permissions |

---

## Examples

### Single Paper Extraction

```
/pdf-to-md docs/hasani-2020-liquid-time-constant.pdf
```

**Output**:
```
## PDF Extraction Complete

### Extracted (1 file)

| PDF | Markdown | Title |
|-----|----------|-------|
| `docs/hasani-2020-liquid-time-constant.pdf` | `refs/ref-liquid-time-constant-networks.md` | Liquid Time-constant Networks |

### Output Location
- Markdown files written to: `Gist-threads/refs/`
```

### Batch Extraction (Subagent per PDF)

```
/pdf-to-md docs/
```

**Execution pattern** (each subagent runs in isolated context):
```
1. Task("Extract lyon-2024-carfac-v2.pdf")
   → Subagent reads PDF, writes refs/ref-carfac-v2.md
   → Returns: {"title": "CARFAC v2", "success": true}
   → Context flushed ✓

2. Task("Extract roche-2018-autoencoders.pdf")
   → Subagent reads PDF, writes refs/ref-autoencoders-for-music.md
   → Returns: {"title": "Autoencoders for Music", "success": true}
   → Context flushed ✓

3. Task("Extract caillon-2021-rave.pdf")
   → Subagent reads PDF, writes refs/ref-rave.md
   → Returns: {"title": "RAVE", "success": true}
   → Context flushed ✓

4. Task("Extract peeters-2004-features.pdf")
   → Subagent reads PDF, writes refs/ref-a-large-set-of-audio-features.md
   → Returns: {"title": "A Large Set of Audio Features", "success": true}
   → Context flushed ✓
```

**Final Output**:
```
## PDF Extraction Complete

### Extracted (4 files)

| PDF | Markdown | Title |
|-----|----------|-------|
| `docs/lyon-2024-carfac-v2.pdf` | `refs/ref-carfac-v2.md` | CARFAC v2 |
| `docs/roche-2018-autoencoders.pdf` | `refs/ref-autoencoders-for-music.md` | Autoencoders for Music |
| `docs/caillon-2021-rave.pdf` | `refs/ref-rave.md` | RAVE: A variational autoencoder... |
| `docs/peeters-2004-features.pdf` | `refs/ref-a-large-set-of-audio-features.md` | A Large Set of Audio Features |

### Output Location
- Markdown files written to: `project-threads/refs/`
```

---

## Integration with fetch-papers

This skill is designed to work after `/fetch-papers`:

```
# Step 1: Discover and download papers for a thread
/fetch-papers carfac-feature-vectors

# Step 2: Extract downloaded PDFs to markdown
/pdf-to-md docs/
```

The fetch-papers skill downloads PDFs to `docs/`, and this skill converts them to searchable markdown references in the threads repository.
