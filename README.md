# cc-sessions-tui

A terminal UI for browsing and resuming [Claude Code](https://docs.anthropic.com/en/docs/claude-code) sessions.

![ccsessions-playbackfast](https://github.com/user-attachments/assets/e5470b3e-c3a5-43c5-a097-304aeb8a7924)

<img width="775" height="556" alt="cc-sessions-info" src="https://github.com/user-attachments/assets/3ffd8c25-5ba9-43c3-a190-f4ae3102b929" />

## Features

- **Browse sessions** across all your projects with color-coded recency
- **Search** by session name, intent, files, repo, branch, or tags
- **AI-generated summaries** showing intent and files touched
- **Labels/tags** for organizing sessions with visual grouping
- **Rename sessions** (persists via Claude's native custom titles)
- **Resume sessions** directly or via [specstory](https://github.com/specstory/specstory) if installed
- **Git branch display** for each session
- **Keyboard-driven** - no mouse needed

## Installation

### pipx (recommended)

```bash
pipx install cc-![Uploading ccsessions-playbackfast.gif…]()
sessions-tui
```

### pip

```bash
pip install cc-sessions-tui
```

### Direct download

```bash
curl -O https://raw.githubusercontent.com/keithbinkly/cc-sessions-tui/main/cc_sessions_tui.py
chmod +x cc_sessions_tui.py
./cc_sessions_tui.py
```

## Usage

```bash
cc-sessions
```

Or run directly:

```bash
python cc_sessions_tui.py
```

### Demo Mode

Try it out with sample data (no Claude sessions required):

```bash
cc-sessions --demo       # Fullstack development demo
cc-sessions --demo-dbt   # dbt analytics engineering demo
```

## Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `↑`/`↓` or `j`/`k` | Navigate sessions |
| `←`/`→` | Page navigation (15 sessions per page) |
| `Enter` | Resume selected session |
| `/` | Search |
| `Esc` | Clear search |
| `r` | Rename session |
| `l` | Edit labels/tags |
| `t` | Sort by time (default) |
| `m` | Sort by message count |
| `g` | Group by label |
| `a` | Load all sessions (not just last 48h) |
| `R` | Refresh |
| `q` | Quit |

## Labels

Press `l` to add comma-separated labels to any session. Labels are stored in `~/.claude/session-tags.json` (separate from Claude's data, easy to backup or reset).

When sorted by label (`g`), sessions are grouped under section headers.

## Requirements

- Python 3.8+
- macOS or Linux
- Claude Code CLI installed (`claude` command available)
- Optional: [specstory](https://github.com/specstory/specstory) for enhanced session resume

## How it works

Reads session data from `~/.claude/projects/` where Claude Code stores conversation history. Parses JSONL files to extract:

- Session metadata (timestamps, message counts)
- Custom titles (from `/title` command or rename)
- AI-generated summaries (intent + files)
- Git branch at time of session

## License

MIT
