# Graph Visualization

Interactive visualization for Watercooler baseline graphs using force-directed layouts.

## Overview

The `visualize_graph.py` script generates an interactive HTML visualization of your Watercooler threads as a directed graph. It uses [pyvis](https://pyvis.readthedocs.io/) (a vis.js wrapper) for force-directed layout with spring-relaxation physics.

**Features:**
- Interactive exploration (zoom, pan, drag nodes)
- Color-coded nodes by type and status
- Hover tooltips with metadata
- Dark and light themes
- Auto-opens in browser

## Installation

```bash
# Install visualization dependencies
pip install watercooler-cloud[visualization]

# Or install directly
pip install pyvis networkx
```

## Quick Start

```bash
# Generate visualization from baseline graph
python scripts/visualize_graph.py --input /path/to/baseline-graph --open

# Output to custom file
python scripts/visualize_graph.py -i /tmp/graph -o my-graph.html

# Use light theme
python scripts/visualize_graph.py -i /tmp/graph --light
```

## Input Format (Baseline Graph)

The script expects a directory containing two JSONL files produced by the memory pipeline:

### nodes.jsonl

One JSON object per line representing threads and entries:

```json
{"id": "thread:feature-auth", "type": "thread", "topic": "feature-auth", "status": "OPEN", "title": "Auth Refactor", "ball": "Claude", "entry_count": 5}
{"id": "entry:feature-auth:1", "type": "entry", "entry_id": "feature-auth:1", "title": "Initial analysis", "entry_type": "Note", "agent": "Claude", "role": "implementer"}
```

**Thread node fields:**
| Field | Description |
|-------|-------------|
| `id` | Unique identifier (`thread:<topic>`) |
| `type` | Always `"thread"` |
| `topic` | Thread topic slug |
| `status` | Thread status (`OPEN`, `CLOSED`, `IN_REVIEW`, `BLOCKED`) |
| `title` | Thread title |
| `ball` | Current ball owner |
| `entry_count` | Number of entries |
| `last_updated` | Last modification timestamp |
| `summary` | AI-generated summary (optional) |

**Entry node fields:**
| Field | Description |
|-------|-------------|
| `id` | Unique identifier (`entry:<topic>:<index>`) |
| `type` | Always `"entry"` |
| `entry_id` | Entry identifier (`<topic>:<index>`) |
| `title` | Entry title |
| `entry_type` | Type (`Note`, `Plan`, `Decision`, `PR`, `Closure`) |
| `agent` | Agent that created the entry |
| `role` | Agent role (`planner`, `implementer`, etc.) |
| `timestamp` | Entry creation time |
| `summary` | AI-generated summary (optional) |
| `file_refs` | Referenced files (optional) |
| `pr_refs` | Referenced PRs (optional) |

### edges.jsonl

One JSON object per line representing relationships:

```json
{"source": "thread:feature-auth", "target": "entry:feature-auth:1", "type": "contains"}
{"source": "entry:feature-auth:1", "target": "entry:feature-auth:2", "type": "followed_by"}
```

**Edge types:**
| Type | Description |
|------|-------------|
| `contains` | Thread contains an entry |
| `followed_by` | Entry is followed by another entry |

## Command-Line Options

```
usage: visualize_graph.py [-h] --input INPUT [--output OUTPUT] [--open]
                          [--height HEIGHT] [--width WIDTH] [--light]

Interactive visualization for baseline graph

options:
  -h, --help            show this help message and exit
  --input INPUT, -i INPUT
                        Path to graph directory (contains nodes.jsonl, edges.jsonl)
  --output OUTPUT, -o OUTPUT
                        Output HTML file (default: graph.html)
  --open                Open in browser after generating
  --height HEIGHT       Canvas height (default: 900px)
  --width WIDTH         Canvas width (default: 100%)
  --light               Use light theme instead of dark
```

## Visual Encoding

### Node Colors

**Threads (by status):**
- Green: OPEN
- Gray: CLOSED
- Orange: IN_REVIEW
- Red: BLOCKED

**Entries (by type):**
- Blue: Note
- Purple: Plan
- Deep Orange: Decision
- Cyan: PR
- Blue Gray: Closure

### Node Shapes

- **Diamond**: Threads
- **Circle**: Entries

### Node Sizes

- Threads scale with entry count (more entries = larger)
- Entries have fixed size

### Edge Styles

- **Solid**: `contains` (thread -> entry)
- **Dashed**: `followed_by` (entry -> entry)

## Generating the Baseline Graph

The baseline graph is created by the memory pipeline. See [baseline-graph.md](baseline-graph.md) for details on running the pipeline.

```bash
# Run the full memory pipeline
python -m watercooler_memory.pipeline run \
  --threads-dir /path/to/threads-repo \
  --work-dir /tmp/pipeline-output

# Visualize the output
python scripts/visualize_graph.py \
  --input /tmp/pipeline-output/graph/baseline \
  --open
```

## Examples

### Basic Usage

```bash
# Generate from pipeline output
python scripts/visualize_graph.py -i /tmp/wc-pipeline/graph/baseline -o threads.html --open
```

### Light Theme for Presentations

```bash
python scripts/visualize_graph.py -i /tmp/graph --light -o presentation.html
```

### Custom Dimensions

```bash
python scripts/visualize_graph.py -i /tmp/graph --height 1200px --width 1600px
```

## Interaction Tips

Once the HTML file opens in your browser:

- **Zoom**: Scroll wheel
- **Pan**: Click and drag on background
- **Move nodes**: Click and drag individual nodes
- **View details**: Hover over nodes for tooltips
- **Select multiple**: Ctrl+click or drag to select
- **Navigation**: Use on-screen navigation buttons (if enabled)

The graph uses live physics simulation - nodes will settle into stable positions automatically.

## Troubleshooting

### "Missing required dependencies"

Install the visualization extras:
```bash
pip install watercooler-cloud[visualization]
```

### Empty graph

Check that the input directory contains `nodes.jsonl` and `edges.jsonl`:
```bash
ls /path/to/graph/
# Should show: nodes.jsonl  edges.jsonl
```

### Graph too large / slow

For graphs with 1000+ nodes, consider:
- Filtering threads before visualization
- Using a more powerful browser
- Reducing the window size

## See Also

- [baseline-graph.md](baseline-graph.md) - Memory pipeline and graph generation
