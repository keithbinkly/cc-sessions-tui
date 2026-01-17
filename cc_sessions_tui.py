#!/usr/bin/env python3
"""
cc-sessions-tui - Interactive TUI for browsing and resuming Claude Code sessions

Usage:
    cc-sessions-tui           # Launch interactive browser

Controls:
    ↑/↓ or j/k  - Navigate sessions
    Enter       - Resume selected session
    q/Esc       - Quit
    r           - Refresh
    1-9         - Jump to session by number
"""

import json
import os
import re
import select
import shutil
import sys
import tty
import termios
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

# Auto-detect specstory
HAS_SPECSTORY = shutil.which('specstory') is not None

# Tags storage - separate file, easy to delete if this feature breaks things
TAGS_FILE = Path.home() / '.claude' / 'session-tags.json'

def load_tags():
    """Load tags from external file. Returns empty dict if file doesn't exist."""
    if TAGS_FILE.exists():
        try:
            with open(TAGS_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {}
    return {}

def save_tags(tags_data):
    """Save tags to external file."""
    try:
        TAGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(TAGS_FILE, 'w') as f:
            json.dump(tags_data, f, indent=2)
        return True
    except IOError:
        return False

def get_session_tags(session_id, tags_data=None):
    """Get tags for a session."""
    if tags_data is None:
        tags_data = load_tags()
    return tags_data.get(session_id, [])

def set_session_tags(session_id, tags, tags_data=None):
    """Set tags for a session."""
    if tags_data is None:
        tags_data = load_tags()
    if tags:
        tags_data[session_id] = tags
    elif session_id in tags_data:
        del tags_data[session_id]
    save_tags(tags_data)
    return tags_data

def get_all_used_tags(tags_data=None):
    """Get list of all tags that have been used (for suggestions)."""
    if tags_data is None:
        tags_data = load_tags()
    all_tags = set()
    for tags in tags_data.values():
        all_tags.update(tags)
    return sorted(all_tags)

# ANSI color codes - Developer Terminal Luxury palette
class Colors:
    RESET = '\033[0m'
    BOLD = '\033[1m'
    DIM = '\033[2m'
    ITALIC = '\033[3m'

    # Status indicators - distinct colors, same block shape
    FRESH = '\033[38;5;46m'       # Bright green (<1hr)
    RECENT = '\033[38;5;39m'      # Cyan/blue (<6hr)
    WARM = '\033[38;5;208m'       # Orange (<12hr)
    FADING = '\033[38;5;134m'     # Purple (<24hr)
    STALE = '\033[38;5;240m'      # Dark gray (older)

    # Hotkey styling
    KEY_BG = '\033[48;5;240m'     # Dark background for keys
    KEY_FG = '\033[38;5;255m'     # Bright white key text
    LABEL = '\033[38;5;250m'      # Lighter gray for labels

    # UI elements
    HEADER_FG = '\033[38;5;238m'      # Dark gray text for header
    HEADER_BG = '\033[48;5;252m'      # Light gray background
    ACCENT = '\033[38;5;75m'          # Soft blue accent
    REPO = '\033[38;5;147m'           # Lavender for repo names
    TITLE = '\033[38;5;252m'          # Soft white for titles
    MUTED = '\033[38;5;245m'          # Muted gray for secondary text

    # Selection - light background, dark text
    SELECT_BG = '\033[48;5;253m'      # Light gray background
    SELECT_BORDER = '\033[38;5;33m'   # Blue left border
    SELECT_TEXT = '\033[38;5;236m'    # Dark charcoal text
    SELECT_REPO = '\033[38;5;55m'     # Dark purple for repo on selection
    SELECT_TITLE = '\033[38;5;236m'   # Dark charcoal for title on selection

    # Summary area
    INTENT_COLOR = '\033[38;5;223m'   # Warm cream for intent
    FILES_COLOR = '\033[38;5;109m'    # Muted teal for files

    # Tag colors - darker tones for light background
    TAG_COLORS = [
        '\033[38;5;162m',  # Magenta
        '\033[38;5;31m',   # Teal
        '\033[38;5;28m',   # Forest green
        '\033[38;5;166m',  # Orange
        '\033[38;5;92m',   # Purple
        '\033[38;5;130m',  # Brown
    ]
    TAG_BG = '\033[48;5;254m'  # Very light gray background for tags

    # Legacy mappings for compatibility
    GREEN = '\033[38;5;48m'
    BLUE = '\033[38;5;39m'
    ORANGE = '\033[38;5;214m'
    YELLOW = '\033[38;5;180m'
    GRAY = '\033[38;5;242m'
    WHITE = '\033[97m'
    BG_BLUE = '\033[48;5;24m'
    BG_GRAY = '\033[48;5;237m'
    CLEAR_LINE = '\033[K'

def extract_text_content(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = []
        for block in content:
            if isinstance(block, dict) and block.get('type') == 'text':
                texts.append(block.get('text', ''))
        return ' '.join(texts)
    return ''

def clean_message(msg):
    if not msg:
        return None
    if msg.startswith('#') and 'Agent' in msg[:50]:
        return None
    if msg.startswith('You are the **'):
        return None
    if msg.startswith('Resume instructions:'):
        return None
    if msg.startswith('[Request interrupted'):
        return None
    if msg.startswith('Caveat:'):
        return None
    if msg.startswith('This session is being continued'):
        return None

    clean = msg.strip()
    clean = re.sub(r'\[Image:[^\]]+\]', '', clean)
    clean = re.sub(r'eyJ[A-Za-z0-9_-]{20,}', '[token]', clean)
    clean = re.sub(r'^\s*>\s*', '', clean)
    clean = re.sub(r'\*\*([^*]+)\*\*', r'\1', clean)
    clean = re.sub(r'\*([^*]+)\*', r'\1', clean)
    clean = re.sub(r'`([^`]+)`', r'\1', clean)
    clean = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', clean)
    clean = re.sub(r'^[-*]\s+', '', clean)
    clean = re.sub(r'^#+\s+', '', clean)
    clean = ' '.join(clean.split())

    if len(clean) < 10:
        return None
    return clean

def get_session_info(filepath):
    user_messages = []
    last_assistant_messages = []
    first_timestamp = None
    last_timestamp = None
    message_count = 0
    custom_title = None
    slug = None
    edited_files = []
    git_branch = None

    with open(filepath, 'r') as f:
        for line in f:
            try:
                entry = json.loads(line)
                ts = entry.get('timestamp')
                if ts:
                    if not first_timestamp:
                        first_timestamp = ts
                    last_timestamp = ts

                # Check for custom title
                if entry.get('type') == 'custom-title':
                    custom_title = entry.get('customTitle')

                # Get slug as fallback
                if not slug and entry.get('slug'):
                    slug = entry.get('slug')

                # Get git branch (take the first non-empty one)
                if not git_branch and entry.get('gitBranch'):
                    git_branch = entry.get('gitBranch')

                if entry.get('type') == 'user':
                    message_count += 1
                    content = extract_text_content(entry.get('message', {}).get('content', ''))
                    if content and not content.startswith('<') and not content.startswith('Caveat'):
                        user_messages.append(content[:500])

                if entry.get('type') == 'assistant':
                    msg = entry.get('message', {})
                    if isinstance(msg, dict):
                        content = extract_text_content(msg.get('content', ''))
                        if content and len(content) > 30:
                            last_assistant_messages.append(content[:500])
                            if len(last_assistant_messages) > 3:
                                last_assistant_messages.pop(0)

                        # Extract edited files from tool_use
                        raw_content = msg.get('content', [])
                        if isinstance(raw_content, list):
                            for block in raw_content:
                                if block.get('type') == 'tool_use' and block.get('name') in ('Edit', 'Write'):
                                    fp = block.get('input', {}).get('file_path', '')
                                    if fp:
                                        # Just get filename
                                        fname = fp.split('/')[-1]
                                        if fname not in edited_files:
                                            edited_files.append(fname)

            except json.JSONDecodeError:
                continue

    # Get first substantive user message (skip "resume", "continue", etc.)
    first_intent = None
    for msg in user_messages:
        msg_lower = msg.lower().strip()
        if msg_lower in ('resume', 'continue', 'lets resume', "let's resume", 'lets continue'):
            continue
        if len(msg) > 15:
            first_intent = msg
            break

    return {
        'path': filepath,
        'first_timestamp': first_timestamp,
        'last_timestamp': last_timestamp,
        'message_count': message_count,
        'user_messages': user_messages[:5],
        'last_assistant_messages': last_assistant_messages,
        'custom_title': custom_title,
        'slug': slug,
        'edited_files': edited_files,
        'first_intent': first_intent,
        'git_branch': git_branch,
    }

def get_repo_name(filepath):
    name = os.path.basename(os.path.dirname(filepath))
    name = name.replace('-Users-kbinkly-git-repos-', '')
    name = name.replace('-Users-kbinkly-', '~/')
    name = name.replace('git-repos', '')

    if 'dbt-agent' in name or name == 'dbt/agent':
        return 'dbt-agent'
    if 'data-centered' in name:
        return 'data-centered'
    if 'dbt-enterprise' in name or 'dbt-projects' in name:
        return 'dbt-enterprise'
    if 'dataviz' in name:
        return 'dataviz-studio'
    if not name or name == '/':
        return 'git-repos'

    return name

def get_repo_path(repo_name):
    """Get the actual filesystem path for a repo"""
    paths = {
        'dbt-agent': '/Users/kbinkly/git-repos/dbt-agent',
        'data-centered': '/Users/kbinkly/git-repos/data-centered',
        'dbt-enterprise': '/Users/kbinkly/git-repos/dbt_projects/dbt-enterprise',
        'dataviz-studio': '/Users/kbinkly/claude-sandboxes/dataviz-studio',
        'git-repos': '/Users/kbinkly/git-repos',
    }
    return paths.get(repo_name, '/Users/kbinkly/git-repos')

def generate_summary(session_info, max_width=80):
    """Generate Format D summary: intent line + edited files line"""

    # Line 1: First intent
    intent = session_info.get('first_intent', '')
    if intent:
        # Clean up the intent
        intent = ' '.join(intent.split())  # normalize whitespace
        if len(intent) > max_width - 3:
            intent = intent[:max_width - 6] + '...'
        intent = f'"{intent}"'
    else:
        intent = "No intent captured"

    # Line 2: Edited files (truncated to fit)
    edited = session_info.get('edited_files', [])
    if edited:
        files_str = ', '.join(edited)
        if len(files_str) > max_width - 4:
            # Truncate intelligently
            truncated = []
            length = 0
            for f in edited:
                if length + len(f) + 2 > max_width - 12:  # leave room for "..."
                    break
                truncated.append(f)
                length += len(f) + 2
            files_str = ', '.join(truncated)
            remaining = len(edited) - len(truncated)
            if remaining > 0:
                files_str += f' +{remaining} more'
        files_line = f'✎ {files_str}'
    else:
        files_line = '✎ (no files edited)'

    return {'intent': intent, 'files': files_line}

def get_status_color(mtime):
    now = datetime.now()
    if mtime > now - timedelta(hours=1):
        return Colors.FRESH, '█'    # Green block
    elif mtime > now - timedelta(hours=6):
        return Colors.RECENT, '█'   # Cyan block
    elif mtime > now - timedelta(hours=12):
        return Colors.WARM, '█'     # Orange block
    elif mtime > now - timedelta(hours=24):
        return Colors.FADING, '█'   # Purple block
    else:
        return Colors.STALE, '█'    # Gray block

def collect_sessions(hours=None):
    """Collect all sessions. If hours is set, only include sessions from that time window."""
    projects_dir = Path.home() / '.claude' / 'projects'
    cutoff = datetime.now() - timedelta(hours=hours) if hours else None
    sessions = []

    for project_dir in projects_dir.iterdir():
        if not project_dir.is_dir():
            continue
        for jsonl in project_dir.glob('*.jsonl'):
            if 'subagents' in str(jsonl) or jsonl.name.startswith('agent-'):
                continue
            mtime = datetime.fromtimestamp(jsonl.stat().st_mtime)
            if cutoff and mtime < cutoff:
                continue
            info = get_session_info(jsonl)
            if info['message_count'] > 0:
                info['repo'] = get_repo_name(str(jsonl))
                info['mtime'] = mtime
                info['size'] = jsonl.stat().st_size
                info['session_id'] = jsonl.stem
                info['summary'] = generate_summary(info)
                sessions.append(info)

    sessions.sort(key=lambda x: x['message_count'], reverse=True)
    return sessions

def get_key():
    """Get a single keypress"""
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
        # Handle escape sequences (arrow keys)
        if ch == '\x1b':
            ch2 = sys.stdin.read(1)
            if ch2 == '[':
                ch3 = sys.stdin.read(1)
                if ch3 == 'A':
                    return 'up'
                elif ch3 == 'B':
                    return 'down'
                elif ch3 == 'C':
                    return 'right'
                elif ch3 == 'D':
                    return 'left'
        return ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

def clear_screen():
    print('\033[2J\033[H', end='')

def move_cursor(row, col=1):
    print(f'\033[{row};{col}H', end='')

def render(sessions, selected_idx, message='', sort_by='msgs', page=0, per_page=15):
    clear_screen()

    # Layout constants
    WIDTH = 95
    NAME_WIDTH = 35

    # Pagination
    total_pages = (len(sessions) + per_page - 1) // per_page
    start_idx = page * per_page
    end_idx = min(start_idx + per_page, len(sessions))
    page_sessions = sessions[start_idx:end_idx]

    # Header - Title with outline style
    hdr_title = "CLAUDE SESSIONS"
    inner_width = WIDTH - 4
    print(f"  {Colors.MUTED}┌{'─' * inner_width}┐{Colors.RESET}")
    padding = inner_width - len(hdr_title) - 2
    print(f"  {Colors.MUTED}│{Colors.RESET} {Colors.BOLD}{hdr_title}{Colors.RESET}{' ' * padding} {Colors.MUTED}│{Colors.RESET}")
    print(f"  {Colors.MUTED}└{'─' * inner_width}┘{Colors.RESET}")

    # Controls - clean layout with subtle separators
    k = lambda key: f"{Colors.ACCENT}{key}{Colors.RESET}"
    sort_indicators = {'time': 't', 'msgs': 'm', 'label': 'g'}
    si = sort_indicators.get(sort_by, 't')
    print(f"  {Colors.MUTED}nav{Colors.RESET} {k('↑↓')}  {Colors.MUTED}search{Colors.RESET} {k('/')}  {Colors.MUTED}go{Colors.RESET} {k('⏎')}  {Colors.MUTED}rename{Colors.RESET} {k('r')}  {Colors.MUTED}label{Colors.RESET} {k('l')}  {Colors.MUTED}sort{Colors.RESET} {k('t')}{k('m')}{k('g')}{Colors.STALE}:{si}{Colors.RESET}  {Colors.MUTED}all{Colors.RESET} {k('a')}  {Colors.MUTED}quit{Colors.RESET} {k('q')}")

    # Column headers - aligned to data columns
    REPO_WIDTH = 14
    TITLE_WIDTH = 42
    # Right columns: msgs(4) + 2 + ago(5) + 2 + id(6) = 19 chars
    print(f"  {Colors.STALE}{'─'*(WIDTH-2)}{Colors.RESET}")
    print(f"   {Colors.MUTED}  {'repo':<{REPO_WIDTH}}  {'session':<{TITLE_WIDTH}}   {'msgs':>4} {'ago':>5}  {'id':<6}{Colors.RESET}")
    print(f"  {Colors.STALE}{'─'*(WIDTH-2)}{Colors.RESET}")

    # Sessions (paginated)
    current_group = None  # Track current label group for headers
    for i, s in enumerate(page_sessions):
        # Group headers (only in label sort mode)
        if sort_by == 'label':
            tags = s.get('tags', [])
            group = tags[0].lower() if tags else None
            if group != current_group:
                current_group = group
                group_name = group if group else '(unlabeled)'
                # Pick color for labeled groups
                if group:
                    tag_color = Colors.TAG_COLORS[0]
                else:
                    tag_color = Colors.STALE
                # Header line
                header_text = f" {group_name} "
                line_len = WIDTH - 4 - len(header_text)
                left_line = '─' * 2
                right_line = '─' * (line_len - 2)
                print(f"  {Colors.STALE}{left_line}{Colors.RESET}{tag_color}{header_text}{Colors.RESET}{Colors.STALE}{right_line}{Colors.RESET}")
        color, dot = get_status_color(s['mtime'])
        global_idx = start_idx + i  # Index in full list

        repo = s['repo'][:REPO_WIDTH].ljust(REPO_WIDTH)

        # Use custom title if available
        if s.get('custom_title'):
            display_name = s['custom_title'][:TITLE_WIDTH]
        else:
            display_name = ""
        display_name = display_name[:TITLE_WIDTH].ljust(TITLE_WIDTH)

        msgs = f"{s['message_count']:>4}"

        # Elapsed time display
        now = datetime.now()
        diff = now - s['mtime']
        minutes = int(diff.total_seconds() / 60)
        hours = int(diff.total_seconds() / 3600)
        days = int(diff.total_seconds() / 86400)

        if minutes < 60:
            time_str = f"{minutes}m"
        elif hours < 24:
            time_str = f"{hours}h"
        elif days < 7:
            time_str = f"{days}d"
        else:
            time_str = s['mtime'].strftime('%m/%d')
        time_str = f"{time_str:>5}"

        sid = s['session_id'][:6]

        # Fixed-width right columns - must match header exactly
        # msgs(4) + 2sp + ago(5) + 2sp + id(6) = 19 total
        right_cols = f"{Colors.MUTED}{msgs}  {time_str}{Colors.RESET}  {Colors.STALE}{sid:<6}{Colors.RESET}"

        tags = s.get('tags', [])

        if global_idx == selected_idx:
            # Selected row - accent border style (fixed widths to match header)
            print(f"  {Colors.SELECT_BORDER}▌{Colors.RESET}{Colors.SELECT_BG}{color}{dot} {Colors.SELECT_REPO}{repo}  {Colors.SELECT_TITLE}{display_name}  {Colors.SELECT_TEXT}{msgs}  {time_str}  {Colors.ACCENT}{sid:<6}{Colors.RESET}")
            # Show Format D summary: intent + files with refined colors
            summary = s.get('summary', {})
            intent = summary.get('intent', '')[:WIDTH-12] if isinstance(summary, dict) else str(summary)[:WIDTH-12]
            files = summary.get('files', '')[:WIDTH-14] if isinstance(summary, dict) else ''
            git_branch = s.get('git_branch', '')
            print(f"  {Colors.SELECT_BORDER}│{Colors.RESET}  {Colors.STALE}└─{Colors.RESET} {Colors.INTENT_COLOR}{intent}{Colors.RESET}")
            if files:
                print(f"  {Colors.SELECT_BORDER}│{Colors.RESET}     {Colors.FILES_COLOR}{files}{Colors.RESET}")
            # Git branch and tags on same line
            branch_part = f"{Colors.MUTED}⎇ {git_branch}{Colors.RESET}" if git_branch else ''
            tags_part = format_tags(tags, max_width=WIDTH-30) if tags else ''
            if branch_part or tags_part:
                separator = '  ' if branch_part and tags_part else ''
                print(f"  {Colors.SELECT_BORDER}│{Colors.RESET}     {branch_part}{separator}{tags_part}")
            print(f"  {Colors.SELECT_BORDER}╵{Colors.RESET}")
        else:
            print(f"   {color}{dot}{Colors.RESET} {Colors.REPO}{repo}{Colors.RESET}  {Colors.MUTED}{display_name}{Colors.RESET}  {right_cols}")

    # Message area
    if message:
        print(f"  {Colors.FRESH}▸ {message}{Colors.RESET}")
        print()

    # Footer with pagination and legend
    print(f"  {Colors.STALE}{'─'*(WIDTH-2)}{Colors.RESET}")
    legend = f"{Colors.FRESH}█{Colors.MUTED}<1h {Colors.RECENT}█{Colors.MUTED}<6h {Colors.WARM}█{Colors.MUTED}<12h {Colors.FADING}█{Colors.MUTED}<24h {Colors.STALE}█{Colors.MUTED}old{Colors.RESET}"
    if total_pages > 1:
        page_info = f"Page {page+1}/{total_pages}"
        print(f"  {Colors.MUTED}{len(sessions)} sessions{Colors.RESET}  {Colors.ACCENT}{page_info}{Colors.RESET}                            {legend}")
    else:
        print(f"  {Colors.MUTED}{len(sessions)} sessions{Colors.RESET}                                        {legend}")

def search_sessions(all_sessions, query):
    """Search sessions by name, intent, files, repo, branch, and tags."""
    query = query.lower()
    results = []
    for s in all_sessions:
        searchable = ' '.join([
            s.get('custom_title', '') or '',
            s.get('first_intent', '') or '',
            ' '.join(s.get('edited_files', []) or []),
            s.get('repo', '') or '',
            s.get('git_branch', '') or '',
            ' '.join(s.get('tags', []) or []),
        ]).lower()
        if query in searchable:
            results.append(s)
    return results

def read_input_char(fd):
    """Read a character, handling escape sequences for arrow keys."""
    ch = sys.stdin.read(1)
    if ch == '\x1b':
        # Could be escape or arrow key - read ahead with timeout
        import select
        if select.select([fd], [], [], 0.1)[0]:
            ch2 = sys.stdin.read(1)
            if ch2 == '[':
                if select.select([fd], [], [], 0.1)[0]:
                    ch3 = sys.stdin.read(1)
                    # Arrow keys - return special marker to ignore
                    if ch3 in ('A', 'B', 'C', 'D'):
                        return 'ARROW'
        # Just escape
        return 'ESC'
    return ch

def rename_session(session):
    """Rename a session by appending a custom-title entry to the JSONL."""
    prompt = f"  {Colors.ACCENT}rename:{Colors.RESET} "
    print(f"\r{prompt}", end='', flush=True)

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    new_name = ''
    try:
        tty.setraw(fd)
        while True:
            ch = read_input_char(fd)
            if ch == '\r':  # Enter - submit
                break
            elif ch == 'ESC':
                return None
            elif ch == 'ARROW':
                continue  # Ignore arrow keys
            elif ch == '\x7f' or ch == '\x08':  # Backspace
                if new_name:
                    new_name = new_name[:-1]
                    print(f"\r{prompt}{new_name} \b", end='', flush=True)
            elif ch == '\x03':  # Ctrl+C
                return None
            elif ch.isprintable():
                new_name += ch
                print(ch, end='', flush=True)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

    if not new_name.strip():
        return None

    # Append custom-title entry to the JSONL file
    filepath = session['path']
    entry = {
        'type': 'custom-title',
        'customTitle': new_name.strip(),
        'sessionId': session['session_id']
    }

    try:
        with open(filepath, 'a') as f:
            f.write(json.dumps(entry) + '\n')
        return new_name.strip()
    except Exception as e:
        return None

def edit_session_tags(session, tags_data):
    """Edit tags for a session. Returns (new_tags, updated_tags_data) or (None, None) if cancelled."""
    session_id = session['session_id']
    current_tags = get_session_tags(session_id, tags_data)
    used_tags = get_all_used_tags(tags_data)

    # Show current tags and suggestions
    current_str = ', '.join(current_tags) if current_tags else '(none)'
    suggestions = [t for t in used_tags if t not in current_tags][:5]

    print(f"\r  {Colors.MUTED}current: {current_str}{Colors.RESET}")
    if suggestions:
        print(f"  {Colors.MUTED}suggestions: {', '.join(suggestions)}{Colors.RESET}")
    print(f"  {Colors.ACCENT}tags (comma-sep):{Colors.RESET} ", end='', flush=True)

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    input_str = ''
    prompt = f"  {Colors.ACCENT}tags (comma-sep):{Colors.RESET} "
    try:
        tty.setraw(fd)
        while True:
            ch = read_input_char(fd)
            if ch == '\r':  # Enter - submit
                break
            elif ch == 'ESC':
                return None, None
            elif ch == 'ARROW':
                continue  # Ignore arrow keys
            elif ch == '\x7f' or ch == '\x08':  # Backspace
                if input_str:
                    input_str = input_str[:-1]
                    print(f"\r{prompt}{input_str} \b", end='', flush=True)
            elif ch == '\x03':  # Ctrl+C
                return None, None
            elif ch.isprintable():
                input_str += ch
                print(ch, end='', flush=True)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

    # Parse tags - split by comma, strip whitespace, remove empties
    new_tags = [t.strip() for t in input_str.split(',') if t.strip()]

    # Update tags data
    updated_data = set_session_tags(session_id, new_tags, tags_data)
    return new_tags, updated_data

def format_tags(tags, max_width=30):
    """Format tags as colored badges."""
    if not tags:
        return ''

    parts = []
    total_len = 0
    for i, tag in enumerate(tags):
        color = Colors.TAG_COLORS[i % len(Colors.TAG_COLORS)]
        badge = f"{Colors.TAG_BG}{color} {tag} {Colors.RESET}"
        badge_len = len(tag) + 2  # space + tag + space
        if total_len + badge_len > max_width and parts:
            remaining = len(tags) - len(parts)
            if remaining > 0:
                parts.append(f"{Colors.MUTED}+{remaining}{Colors.RESET}")
            break
        parts.append(badge)
        total_len += badge_len + 1

    return ' '.join(parts)

def get_search_input():
    """Get search string with visible typing."""
    query = ''
    prompt = f"  {Colors.ACCENT}search:{Colors.RESET} "
    print(f"\r{prompt}", end='', flush=True)

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        while True:
            ch = read_input_char(fd)
            if ch == '\r':  # Enter - submit search
                break
            elif ch == 'ESC':
                return None
            elif ch == 'ARROW':
                continue  # Ignore arrow keys
            elif ch == '\x7f' or ch == '\x08':  # Backspace
                if query:
                    query = query[:-1]
                    print(f"\r{prompt}{query} \b", end='', flush=True)
            elif ch == '\x03':  # Ctrl+C
                return None
            elif ch.isprintable():
                query += ch
                print(ch, end='', flush=True)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

    return query

def generate_demo_sessions():
    """Generate fake session data for demo/screenshots."""
    import random

    # dbt analytics engineering demo data - varied repos by functional area
    sessions_data = [
        # QA work (label: qa)
        {'name': 'QA_customer_cohort_metrics_phase2', 'repo': 'dbt-product', 'mins_ago': 8, 'msgs': 156, 'branch': 'feature/cohort-metrics', 'tags': ['qa'], 'intent': 'Validating 90-day active segmentation columns in int_orders models', 'files': 'int_orders__daily_agg.sql, mrt_customer_cohort__semantic.sql'},
        {'name': 'QA_subscription_revenue_pipeline', 'repo': 'dbt-finance', 'mins_ago': 95, 'msgs': 89, 'branch': 'feature/subscription-rev', 'tags': ['qa'], 'intent': 'Running dbt build and MetricFlow queries to verify revenue metrics', 'files': 'int_subscriptions__monthly.sql, sem_subscription_metrics.yml'},
        {'name': 'QA_inventory_forecast_model', 'repo': 'dbt-supply', 'mins_ago': 380, 'msgs': 201, 'branch': 'feature/inventory-fc', 'tags': ['qa'], 'intent': 'Testing forecast accuracy metrics against warehouse state', 'files': 'mrt_inventory_forecast.sql, test_forecast_accuracy.sql'},
        {'name': 'QA_payment_attribution_fix', 'repo': 'dbt-finance', 'mins_ago': 1100, 'msgs': 67, 'branch': 'fix/payment-attr', 'tags': ['qa'], 'intent': 'Verifying payment-to-order attribution logic after refactor', 'files': 'int_payments__attributed.sql, int_order_payments.sql'},

        # Pipeline development (label: pipeline)
        {'name': 'Add_retention_cohort_analysis', 'repo': 'dbt-product', 'mins_ago': 25, 'msgs': 234, 'branch': 'feature/retention', 'tags': ['pipeline'], 'intent': 'Building weekly retention cohort model with configurable lookback windows', 'files': 'int_users__cohort_base.sql, mrt_retention_cohort.sql, macros/cohort_spine.sql'},
        {'name': 'Refactor_product_hierarchy_dims', 'repo': 'dbt-product', 'mins_ago': 180, 'msgs': 145, 'branch': 'refactor/product-dims', 'tags': ['pipeline'], 'intent': 'Normalizing product category hierarchy into separate dimension tables', 'files': 'dim_product.sql, dim_category.sql, dim_subcategory.sql'},
        {'name': 'Implement_incremental_orders', 'repo': 'dbt-core', 'mins_ago': 520, 'msgs': 312, 'branch': 'feature/incremental', 'tags': ['pipeline'], 'intent': 'Converting orders fact to incremental materialization with merge strategy', 'files': 'fct_orders.sql, macros/incremental_filter.sql'},
        {'name': 'Build_supplier_performance_mart', 'repo': 'dbt-supply', 'mins_ago': 1400, 'msgs': 178, 'branch': 'feature/supplier-perf', 'tags': ['pipeline'], 'intent': 'Creating supplier scorecard mart with delivery and quality metrics', 'files': 'mrt_supplier_scorecard.sql, int_deliveries__supplier_agg.sql'},

        # Semantic layer (label: semantic)
        {'name': 'Define_revenue_metrics_v2', 'repo': 'dbt-finance', 'mins_ago': 55, 'msgs': 78, 'branch': 'feature/rev-metrics-v2', 'tags': ['semantic'], 'intent': 'Adding gross_revenue, net_revenue, and refund_rate to semantic layer', 'files': 'sem_revenue_metrics.yml, mrt_revenue__semantic_base.sql'},
        {'name': 'Add_customer_ltv_metric', 'repo': 'dbt-product', 'mins_ago': 290, 'msgs': 45, 'branch': 'feature/ltv-metric', 'tags': ['semantic'], 'intent': 'Implementing customer lifetime value with 12-month rolling window', 'files': 'sem_customer_metrics.yml, int_customers__ltv_calc.sql'},
        {'name': 'Configure_metricflow_dimensions', 'repo': 'dbt-core', 'mins_ago': 850, 'msgs': 56, 'branch': 'feature/mf-dims', 'tags': ['semantic'], 'intent': 'Setting up time spine and entity links for MetricFlow queries', 'files': 'metricflow_time_spine.sql, sem_entities.yml'},

        # Bugfixes (label: bugfix)
        {'name': 'Fix_duplicate_orders_edge_case', 'repo': 'dbt-core', 'mins_ago': 42, 'msgs': 67, 'branch': 'fix/dupe-orders', 'tags': ['bugfix'], 'intent': 'Resolving duplicate order rows from timezone boundary edge case', 'files': 'int_orders__deduped.sql, tests/test_order_uniqueness.sql'},
        {'name': 'Fix_null_handling_in_margins', 'repo': 'dbt-finance', 'mins_ago': 240, 'msgs': 34, 'branch': 'fix/null-margins', 'tags': ['bugfix'], 'intent': 'Adding coalesce for null costs causing division errors in margin calc', 'files': 'int_order_items__margins.sql'},
        {'name': 'Fix_CI_slim_manifest_overwrite', 'repo': 'dbt-core', 'mins_ago': 720, 'msgs': 89, 'branch': 'fix/ci-manifest', 'tags': ['bugfix'], 'intent': 'Using --target-path target_run to prevent production manifest overwrite', 'files': 'dbt_project.yml, .github/workflows/ci.yml'},

        # Documentation (label: docs)
        {'name': 'Document_canonical_models', 'repo': 'dbt-core', 'mins_ago': 150, 'msgs': 23, 'branch': 'docs/canonical', 'tags': ['docs'], 'intent': 'Writing model descriptions and column docs for canonical layer', 'files': 'models/canonical/_canonical__docs.yml'},
        {'name': 'Update_contribution_guidelines', 'repo': 'dbt-core', 'mins_ago': 980, 'msgs': 18, 'branch': 'docs/contrib', 'tags': ['docs'], 'intent': 'Adding PR template and branch naming conventions', 'files': 'CONTRIBUTING.md, .github/PULL_REQUEST_TEMPLATE.md'},

        # Marketing (label: marketing)
        {'name': 'Campaign_attribution_model', 'repo': 'dbt-marketing', 'mins_ago': 65, 'msgs': 134, 'branch': 'feature/attribution', 'tags': ['pipeline'], 'intent': 'Multi-touch attribution model for marketing campaigns', 'files': 'int_touchpoints.sql, mrt_campaign_attribution.sql'},
        {'name': 'Email_engagement_metrics', 'repo': 'dbt-marketing', 'mins_ago': 440, 'msgs': 67, 'branch': 'feature/email-metrics', 'tags': ['semantic'], 'intent': 'Adding open rate, click rate, unsubscribe metrics to semantic layer', 'files': 'sem_email_metrics.yml, int_email_events.sql'},

        # Untagged sessions
        {'name': 'Explore_new_source_tables', 'repo': 'dbt-core', 'mins_ago': 350, 'msgs': 45, 'branch': 'explore/new-sources', 'tags': [], 'intent': 'Profiling new vendor data tables for potential integration', 'files': 'sources/vendor_data.yml'},
        {'name': 'Ad_hoc_churn_analysis', 'repo': 'dbt-product', 'mins_ago': 1600, 'msgs': 112, 'branch': 'analysis/churn', 'tags': [], 'intent': 'One-off analysis of customer churn patterns by acquisition channel', 'files': 'analyses/churn_by_channel.sql'},
        {'name': 'Prototype_realtime_dashboard', 'repo': 'dbt-core', 'mins_ago': 2200, 'msgs': 78, 'branch': 'prototype/realtime', 'tags': [], 'intent': 'Testing streaming ingestion approach for near-realtime metrics', 'files': 'models/staging/stg_events__stream.sql'},
    ]

    now = datetime.now()
    sessions = []

    for i, s in enumerate(sessions_data):
        session_id = f"{random.randint(10000000, 99999999):08x}"
        mtime = now - timedelta(minutes=s['mins_ago'])

        sessions.append({
            'session_id': session_id,
            'repo': s['repo'],
            'custom_title': s['name'],
            'message_count': s['msgs'],
            'mtime': mtime,
            'git_branch': s['branch'],
            'tags': s['tags'],
            'summary': {'intent': s['intent'], 'files': s['files']},
            'project_path': f"/Users/demo/projects/{s['repo']}",
        })

    return sessions

def main():
    # Check for --demo flag
    demo_mode = '--demo' in sys.argv

    # Disable line wrapping for cleaner display
    sys.stdout.write('\033[?7l')
    sys.stdout.flush()

    if demo_mode:
        all_sessions = generate_demo_sessions()
    else:
        all_sessions = collect_sessions(hours=48)  # Start with 48hr, can load more
    sessions = all_sessions  # Current view (may be filtered)

    if not sessions:
        sys.stdout.write('\033[?7h')  # Re-enable before exit
        print("No sessions found in the last 48 hours.")
        return

    # Load tags from external file (skip in demo mode - tags already included)
    if demo_mode:
        tags_data = {}
    else:
        tags_data = load_tags()
        # Apply tags to sessions
        for s in sessions:
            s['tags'] = get_session_tags(s['session_id'], tags_data)

    # Default sort by time (most recent first)
    sessions.sort(key=lambda x: x['mtime'], reverse=True)

    selected = 0
    message = ''
    sort_by = 'time'  # 'time', 'msgs', or 'label'
    page = 0
    per_page = 15
    all_loaded = False  # Track if we've loaded all sessions
    search_query = None  # Active search filter

    try:
        while True:
            total_pages = (len(sessions) + per_page - 1) // per_page
            # Auto-adjust page if selected is out of view
            page = selected // per_page

            # Show search status in message if active
            display_msg = message
            if search_query and not message:
                display_msg = f"search: '{search_query}' ({len(sessions)} matches) - Esc to clear"

            render(sessions, selected, display_msg, sort_by, page, per_page)
            message = ''

            key = get_key()

            if key in ('q', '\x03'):  # q, Ctrl+C
                print('\033[?7h', end='')  # Re-enable line wrapping
                clear_screen()
                print("Goodbye!")
                break
            elif key == '/':
                # Enter search mode
                query = get_search_input()
                if query:
                    # Load all sessions for search if not already loaded
                    if not all_loaded:
                        all_sessions = collect_sessions(hours=None)
                        for s in all_sessions:
                            s['tags'] = get_session_tags(s['session_id'], tags_data)
                        all_loaded = True
                    sessions = search_sessions(all_sessions, query)
                    search_query = query
                    selected = 0
                    if not sessions:
                        message = f"No matches for '{query}'"
                        sessions = all_sessions
                        search_query = None
                    else:
                        message = f"Found {len(sessions)} matches"
            elif key == '\x1b':
                # Escape key - clear search if active
                if search_query:
                    sessions = all_sessions
                    search_query = None
                    selected = 0
                    message = 'Search cleared'
            elif key in ('j', 'down'):
                if selected < len(sessions) - 1:
                    selected += 1
                elif not all_loaded and not search_query:
                    # At the end, offer to load more
                    message = 'Loading all sessions...'
                    all_sessions = collect_sessions(hours=None)
                    for s in all_sessions:
                        s['tags'] = get_session_tags(s['session_id'], tags_data)
                    sessions = all_sessions
                    if sort_by == 'time':
                        sessions.sort(key=lambda x: x['mtime'], reverse=True)
                    all_loaded = True
                    message = f'Loaded {len(sessions)} total sessions'
            elif key in ('k', 'up'):
                selected = max(selected - 1, 0)
            elif key == 'right':
                # Next page
                if page < total_pages - 1:
                    page += 1
                    selected = page * per_page
                elif not all_loaded and not search_query:
                    message = 'Loading all sessions...'
                    all_sessions = collect_sessions(hours=None)
                    for s in all_sessions:
                        s['tags'] = get_session_tags(s['session_id'], tags_data)
                    sessions = all_sessions
                    if sort_by == 'time':
                        sessions.sort(key=lambda x: x['mtime'], reverse=True)
                    all_loaded = True
                    message = f'Loaded {len(sessions)} total sessions'
            elif key == 'left':
                # Previous page
                if page > 0:
                    page -= 1
                    selected = page * per_page
            elif key == 'R':
                all_sessions = collect_sessions(hours=48)
                tags_data = load_tags()  # Reload tags too
                for s in all_sessions:
                    s['tags'] = get_session_tags(s['session_id'], tags_data)
                sessions = all_sessions
                all_loaded = False
                search_query = None
                if sort_by == 'time':
                    sessions.sort(key=lambda x: x['mtime'], reverse=True)
                selected = 0
                message = 'Refreshed (48hr)'
            elif key == 'a':
                # Load ALL sessions
                message = 'Loading all sessions...'
                all_sessions = collect_sessions(hours=None)
                for s in all_sessions:
                    s['tags'] = get_session_tags(s['session_id'], tags_data)
                sessions = all_sessions
                search_query = None
                if sort_by == 'time':
                    sessions.sort(key=lambda x: x['mtime'], reverse=True)
                all_loaded = True
                message = f'Loaded {len(sessions)} total sessions'
            elif key == 't':
                sort_by = 'time'
                sessions.sort(key=lambda x: x['mtime'], reverse=True)
                selected = 0
                message = 'Sorted by time (newest first)'
            elif key == 'm':
                sort_by = 'msgs'
                sessions.sort(key=lambda x: x['message_count'], reverse=True)
                selected = 0
                message = 'Sorted by message count'
            elif key == 'g':
                # Group/sort by label (tagged first, then by first tag alphabetically, then time)
                sort_by = 'label'
                def label_sort_key(s):
                    tags = s.get('tags', [])
                    if not tags:
                        return (1, '', -s['mtime'].timestamp())  # Untagged last
                    return (0, tags[0].lower(), -s['mtime'].timestamp())  # Tagged first, alpha by first tag
                sessions.sort(key=label_sort_key)
                selected = 0
                tagged_count = sum(1 for s in sessions if s.get('tags'))
                message = f'Grouped by label ({tagged_count} tagged)'
            elif key == 'r':
                # Rename selected session
                s = sessions[selected]
                current_name = s.get('custom_title', '') or s.get('slug', '') or s['session_id'][:8]
                new_name = rename_session(s)
                if new_name:
                    s['custom_title'] = new_name
                    s['summary'] = generate_summary(s)  # Regenerate summary
                    message = f'Renamed to "{new_name}"'
                else:
                    message = 'Rename cancelled'
            elif key == 'l':
                # Edit labels/tags for selected session
                s = sessions[selected]
                new_tags, tags_data = edit_session_tags(s, tags_data)
                if new_tags is not None:
                    s['tags'] = new_tags
                    if new_tags:
                        message = f'Tags: {", ".join(new_tags)}'
                    else:
                        message = 'Tags cleared'
                else:
                    message = 'Tag edit cancelled'
            elif key == '\r':  # Enter
                s = sessions[selected]
                repo_path = get_repo_path(s['repo'])
                session_id = s['session_id']

                print('\033[?7h', end='')  # Re-enable line wrapping
                clear_screen()
                if HAS_SPECSTORY:
                    print(f"\n  Resuming session {session_id[:8]} in {s['repo']} (via specstory)...\n")
                    print(f"  {Colors.DIM}cd {repo_path} && specstory run --resume {session_id} --no-cloud-sync{Colors.RESET}\n")
                    os.chdir(repo_path)
                    os.execvp('specstory', ['specstory', 'run', '--resume', session_id, '--no-cloud-sync'])
                else:
                    print(f"\n  Resuming session {session_id[:8]} in {s['repo']}...\n")
                    print(f"  {Colors.DIM}cd {repo_path} && claude --resume {session_id}{Colors.RESET}\n")
                    os.chdir(repo_path)
                    os.execvp('claude', ['claude', '--resume', session_id])

    except KeyboardInterrupt:
        print('\033[?7h', end='')  # Re-enable line wrapping
        clear_screen()
        print("Goodbye!")

if __name__ == '__main__':
    main()
