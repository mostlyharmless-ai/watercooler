---
name: fetch-papers
description: Download papers referenced in a watercooler thread to docs/
allowed-tools:
  - Bash(mcp-cli *)
  - Bash(curl *)
  - Bash(mkdir *)
  - Bash(ls *)
  - Read
  - Glob
  - Grep
  - WebFetch
  - WebSearch
---

# Fetch Papers Skill

Download academic papers relevant to a watercooler thread to the `docs/` directory.

This skill goes beyond literal citation extraction — it identifies **conceptual topics** discussed in the thread and searches for foundational and recent papers on those topics.

## Usage

```
/fetch-papers <thread-topic>
```

**Example**: `/fetch-papers carfac-feature-vectors`

**Follow-up**: After downloading, use `/pdf-to-md docs/` to extract papers to markdown.

## Workflow Overview

```
┌─────────────────────────────────────────────────────────────────┐
│  1. Read thread content                                         │
│  2. Extract EXPLICIT references (arXiv, DOI, URLs)              │
│  3. Extract CONCEPTUAL TOPICS (techniques, models, algorithms)  │
│  4. Query related threads & REFERENCES.md for more context      │
│  5. WebSearch for papers on extracted topics                    │
│  6. Download available PDFs to docs/                            │
│  7. Report results                                               │
└─────────────────────────────────────────────────────────────────┘
```

---

## Phase 1: Read Thread Content

### 1.1 Check MCP Schema

```bash
mcp-cli info watercooler-cloud/watercooler_read_thread
```

### 1.2 Fetch Thread

```bash
mcp-cli call watercooler-cloud/watercooler_read_thread '{"topic": "<thread-topic>"}'
```

---

## Phase 2: Extract Explicit References

Scan the thread for direct paper references:

| Pattern Type | Regex/Pattern | Example |
|--------------|---------------|---------|
| arXiv abs URL | `arxiv\.org/abs/(\d{4}\.\d{4,5})` | `arxiv.org/abs/2404.17490` |
| arXiv ID | `arXiv:(\d{4}\.\d{4,5})` | `arXiv:2106.13898` |
| DOI | `doi\.org/[\w./%-]+` | `doi.org/10.1109/ICASSP...` |
| Direct PDF URL | `https?://[^\s]+\.pdf` | `https://example.com/paper.pdf` |
| Author citation | `Author (Year)` or `Author et al. (Year)` | `Lyon (2024)` |

---

## Phase 3: Extract Conceptual Topics (CRITICAL)

**This is where shallow extraction fails.** Read the thread carefully and identify:

### 3.1 Technical Concepts & Algorithms

Look for mentions of:
- **Neural architectures**: autoencoder, VAE, recurrent autoencoder, LSTM-AE, GRU, transformer, CfC, LTC
- **Signal processing**: spectral centroid, MFCC, mel spectrogram, filterbank, envelope, transient detection
- **Psychoacoustics**: ERB, Bark scale, critical bands, auditory filter, cochlear model
- **Learning paradigms**: unsupervised learning, self-supervised, dimensionality reduction, latent space
- **Audio/music terms**: timbre, pitch detection, onset detection, source separation

### 3.2 Identify Research Questions

What problems is the thread trying to solve?
- Feature extraction for X?
- Dimensionality reduction from Y to Z dimensions?
- Real-time constraints?
- Comparison of approaches?

### 3.3 Recognize Multi-Disciplinary Context

Threads often span multiple fields. Identify ALL disciplines involved:

**Example — carfac-feature-vectors thread touches:**
- **Neuroscience/auditory modeling** — cochlear models, CARFAC, auditory filters
- **Signal processing** — filterbanks, envelope detection, spectral analysis
- **Machine learning** — autoencoders, VAE, dimensionality reduction, latent spaces
- **Music technology** — real-time synthesis, timbre, instrument recognition
- **Embedded systems** — Bela, real-time constraints, CPU budgets

Each discipline has its own literature, terminology, and publication venues. A narrow search in one field will miss foundational work from adjacent fields.

### 3.4 Infer Relevant Publication Venues

Don't hardcode venue lists — **infer them from domain signals** in the thread:

| Domain Signal | Likely Venues |
|---------------|---------------|
| "audio effects", "synthesis", "real-time processing" | DAFX, AES, ICMC |
| "music analysis", "MIR", "genre classification" | ISMIR, SMC |
| "speech", "speaker recognition", "ASR" | Interspeech, ICASSP |
| "neural network", "deep learning", "representation learning" | NeurIPS, ICLR, ICML |
| "auditory model", "cochlea", "psychoacoustics" | JASA, Hearing Research |
| "embedded", "real-time", "low-latency" | DAC, ECRTS, NIME |

**Cross-domain work lives in unexpected places.** A paper on "neural audio synthesis" might appear in NeurIPS (ML venue) or DAFX (audio venue) or both. Search broadly.

### 3.5 Build Diverse Search Queries

Don't limit to arXiv. Formulate queries that span sources:

```
Topic: "autoencoder for audio feature reduction"

Broad queries (general search engines):
  - "autoencoder audio feature extraction"
  - "variational autoencoder music timbre"
  - "learned audio representations"

Venue-targeted queries (when domain signals are strong):
  - "site:dafx.de autoencoder"
  - "site:ismir.net timbre representation"

Open-access repositories:
  - "autoencoder audio arxiv"
  - "audio VAE openreview"

Cross-domain queries (combine terms from different fields):
  - "cochlear model machine learning"
  - "auditory filterbank neural network"
  - "perceptual audio features deep learning"
```

**Key insight**: The most valuable papers often bridge disciplines. A search for "cochlear model + neural network" might find work that neither a pure neuroscience search nor a pure ML search would surface.

---

## Phase 4: Cross-Reference Context Sources

### 4.1 Check REFERENCES.md

```bash
# Read project references if they exist
ls docs/REFERENCES.md 2>/dev/null || ls REFERENCES.md 2>/dev/null
```

Match author citations from the thread against known papers with URLs.

### 4.2 Query Related Watercooler Threads

Check schema first:
```bash
mcp-cli info watercooler-cloud/watercooler_search
```

Search for related discussions that might contain additional references:
```bash
mcp-cli call watercooler-cloud/watercooler_search '{"query": "<key-topic>", "limit": 5}'
```

Or use find_similar to discover related threads:
```bash
mcp-cli info watercooler-cloud/watercooler_find_similar
mcp-cli call watercooler-cloud/watercooler_find_similar '{"topic": "<thread-topic>", "limit": 5}'
```

### 4.3 Check Existing docs/ Directory

```bash
ls docs/*.pdf 2>/dev/null
```

Avoid re-downloading papers already present.

---

## Phase 5: Web Search for Topic Papers

Execute multiple search strategies based on the multi-disciplinary analysis from Phase 3:

### 5.1 Broad Conceptual Searches

```
WebSearch: "autoencoder audio feature extraction"
WebSearch: "variational autoencoder timbre synthesis"
WebSearch: "cochlear model neural network"
```

### 5.2 Venue-Aware Searches (when domain signals are clear)

```
WebSearch: "site:dafx.de neural audio synthesis"
WebSearch: "site:ismir.net learned audio representations"
WebSearch: "ICASSP 2024 audio autoencoder"
```

### 5.3 Cross-Domain Searches (bridge disciplines)

```
WebSearch: "auditory filterbank deep learning feature extraction"
WebSearch: "perceptual audio coding neural network"
WebSearch: "biologically inspired audio processing machine learning"
```

### 5.4 Foundational + Recent

```
WebSearch: "spectral centroid audio feature" (foundational)
WebSearch: "audio representation learning 2024" (recent advances)
```

**Guidelines**:
- **Don't default to arxiv-only** — important work lives in conference proceedings, journals, and institutional repositories
- **Search across time** — foundational papers (classics) AND recent advances (state-of-art)
- **Follow citation trails** — if a search result mentions seminal work, note it for follow-up
- **Cross-domain queries often yield the best finds** — "cochlear model + machine learning" surfaces work that neither field's search alone would find
- **Survey/comparison papers are high-value** — they summarize a field and cite the important work

---

## Phase 6: Download PDFs

### 6.1 Create Directory

```bash
mkdir -p docs
```

### 6.2 Download arXiv Papers

Convert abstract URL to PDF:
- `arxiv.org/abs/XXXX.XXXXX` → `https://arxiv.org/pdf/XXXX.XXXXX.pdf`

```bash
curl -L --max-filesize 50000000 --max-redirs 3 -o "docs/<author>-<year>-<short-title>.pdf" "https://arxiv.org/pdf/<id>.pdf"
```

### 6.3 Download Direct PDFs

```bash
curl -L --max-filesize 50000000 --max-redirs 3 -o "docs/<author>-<year>-<short-title>.pdf" "<url>"
```

### 6.4 Download Safety

- **Always** use `--max-filesize 50000000` (50MB) to prevent unbounded downloads
- **Always** use `--max-redirs 3` to prevent redirect loops
- **Only download from trusted domains**: arxiv.org, openreview.net, aclanthology.org, proceedings.mlr.press
- **Validate filenames**: Reject paths containing `..` or absolute paths
- **Never** interpolate user input directly into shell commands — use variables or `jq` for JSON construction

### 6.5 Note Paywalled Sources

Cannot auto-download — report to user:
- IEEE Xplore (`ieeexplore.ieee.org`)
- ACM Digital Library (`dl.acm.org`)
- Springer (`link.springer.com`)
- Elsevier (`sciencedirect.com`)

### 6.6 Naming Convention

`<first-author-lastname>-<year>-<short-title>.pdf`

Examples:
- `roche-2018-autoencoders-music-comparison.pdf`
- `caillon-2021-rave.pdf`
- `lyon-2024-carfac-v2.pdf`

Lowercase, hyphens for spaces, max 50 chars for title.

---

## Phase 7: Report Results

Provide a comprehensive summary:

```
## Fetched papers for thread: <topic>

### Conceptual Topics Identified
- Autoencoders for audio (VAE, LSTM-AE, DAE comparison)
- Spectral feature extraction (centroid, spread)
- Psychoacoustic band grouping (ERB, Bark)

### Downloaded (N)
| PDF | Source | Relevance |
|-----|--------|-----------|
| `docs/roche-2018-autoencoders-music.pdf` | arXiv:1806.04096 | Compares AE architectures |
| `docs/caillon-2021-rave.pdf` | arXiv:2111.05011 | VAE for audio synthesis |
| ... | ... | ... |

### Already in docs/ (N)
- `Peeters_2004_AudioFeatures.pdf` - covers spectral centroid

### Paywalled - manual download required (N)
- Patterson (1976) "Auditory filter shapes" - https://ieeexplore.ieee.org/...

### Not Found
- [topic] - no suitable open-access paper found

### Next Step
To extract downloaded PDFs to markdown reference files:
```
/pdf-to-md docs/
```

### Sources
- [Paper Title](URL)
- ...
```

---

## Example: Full Execution for "carfac-feature-vectors"

### Disciplines Identified

| Discipline | Signals in Thread |
|------------|-------------------|
| **Auditory neuroscience** | CARFAC, cochlear model, auditory filters, ERB bands |
| **Signal processing** | Filterbanks, envelope detection, spectral centroid, transients |
| **Machine learning** | Autoencoder, VAE, LSTM-AE, latent space, dimensionality reduction |
| **Music technology** | Timbre, instrument recognition, real-time synthesis |
| **Embedded systems** | Bela platform, CPU budget, real-time constraints |

### Inferred Venues

- **DAFX** — audio effects, real-time synthesis (strong signal)
- **ISMIR** — timbre, music features
- **NeurIPS/ICLR** — autoencoders, representation learning
- **JASA** — auditory models, psychoacoustics
- **NIME** — embedded audio, Bela

### Search Queries Executed

**Broad conceptual:**
1. `"autoencoder audio feature extraction"` → Roche 2018 (DAFX, also on arXiv)
2. `"variational autoencoder audio synthesis"` → RAVE paper

**Venue-targeted:**
3. `"site:dafx.de autoencoder"` → additional DAFX proceedings
4. `"ICASSP audio representation learning"` → speech/audio ML papers

**Cross-domain:**
5. `"cochlear model neural network"` → bridges auditory science + ML
6. `"auditory filterbank deep learning"` → perceptual + learned features

**Foundational:**
7. `"ERB equivalent rectangular bandwidth"` → Glasberg & Moore classics

### Result

- 5 new papers downloaded spanning ML, audio, and auditory science
- 2 paywalled papers noted (IEEE)
- Key finding: Roche 2018 comparison paper was at DAFX but also on arXiv

### Next Step

```
/pdf-to-md docs/
```

---

## Error Handling

- **Thread not found**: List available threads
- **No topics extracted**: Ask user to clarify what topics to research
- **Download fails**: Report URL and HTTP status, continue with others
- **Rate limited**: Pause between downloads, report partial results
