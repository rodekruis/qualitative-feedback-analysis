#!/usr/bin/env bash
# Ensure a UTF-8 locale so bash ${#var} counts characters, not bytes.
# This matters for the → glyph used in reset suffixes.
export LANG="${LANG:-en_US.UTF-8}"
export LC_ALL="${LC_ALL:-en_US.UTF-8}"

# Read JSON input from stdin
input=$(cat)

# Extract values
model=$(echo "$input" | jq -r '.model.display_name')
style=$(echo "$input" | jq -r '.output_style.name // "default"')

# Read effort level from settings file (default if missing)
effort=$(jq -r '.effortLevel // "default"' ~/.claude/settings.json 2>/dev/null || echo "default")

# Build the status line: model (effort) | style
status="$model ($effort) | $style"

# ---------------------------------------------------------------------------
# inline_bar LABEL PCT [RESET_SUFFIX]
#
# Renders a bracketed progress bar where the fill boundary is shown via two
# distinct 256-color backgrounds rather than block characters:
#   - filled portion  → dark-gray background  (256-color index BG_FILLED)
#   - unfilled portion → light-gray background (256-color index BG_EMPTY)
# Default foreground color is preserved throughout (\033[48;5;Nm sets bg only;
# \033[49m resets bg only).  Brackets [ ] sit outside all color escapes.
#
# Minimum inner width: MIN_WIDTH printable chars (padded with trailing spaces).
# This prevents very short segments from being hard to read as progress bars.
#
# Split arithmetic uses bash integer math with round-half-up:
#   split = (pct * total + 50) / 100  — then clamped to [1, total-1] when
# partially filled so the boundary is always visible.
# bash ${#var} counts Unicode code points in a UTF-8 locale (set at top of
# script), so multi-byte chars like → count as 1 — safe for split indexing.
#
# Color indices to tweak:
#   BG_FILLED=236  — xterm-256 dark  gray (r48  g48  b48)
#   BG_EMPTY =250  — xterm-256 light gray (r188 g188 b188)
# ---------------------------------------------------------------------------
inline_bar() {
    local label="$1"
    local pct="$2"
    local reset_suffix="${3:-}"

    # 256-color background indices — adjust here to re-tune the palette
    local BG_FILLED=236   # dark  gray: filled   portion
    local BG_EMPTY=250    # light gray: unfilled portion
    local MIN_WIDTH=10    # minimum printable chars inside the brackets

    # Build the inner text (no ANSI, just printable chars)
    local pct_int
    pct_int=$(printf "%.0f" "$pct")
    local inner="${label} ${pct_int}%"
    [ -n "$reset_suffix" ] && inner="${inner} ${reset_suffix}"

    # Pad inner to at least MIN_WIDTH with trailing spaces, then add one
    # space of breathing room on each side → padded is always ≥ MIN_WIDTH+2
    local inner_len=${#inner}
    if [ "$inner_len" -lt "$MIN_WIDTH" ]; then
        local pad=$(( MIN_WIDTH - inner_len ))
        local spaces
        printf -v spaces '%*s' "$pad" ''
        inner="${inner}${spaces}"
    fi
    local padded=" ${inner} "
    local total=${#padded}

    # Compute split point: how many chars of $padded to paint in BG_FILLED.
    # Three cases:
    #   pct==0   → split=0 (nothing filled)
    #   pct==100 → split=total (fully filled)
    #   else     → round-half-up, then clamp to [1, total-1]
    #              round-half-up in integer arithmetic: (pct*total + 50) / 100
    #              min=1: with two-tone backgrounds a 1-char dark cell is visible
    #              max=total-1: trailing space stays light until truly 100%
    local split
    if [ "$pct_int" -eq 0 ]; then
        split=0
    elif [ "$pct_int" -ge 100 ]; then
        split=$total
    else
        split=$(( (pct_int * total + 50) / 100 ))
        [ "$split" -lt 1 ] && split=1
        [ "$split" -ge "$total" ] && split=$(( total - 1 ))
    fi

    local filled_text="${padded:0:$split}"
    local empty_text="${padded:$split}"

    # Structure: [  BG_FILLED<filled_text>  BG_EMPTY<empty_text>  RESET_BG  ]
    printf '[%b%s%b%s%b]' \
        "\033[48;5;${BG_FILLED}m" "${filled_text}" \
        "\033[48;5;${BG_EMPTY}m"  "${empty_text}" \
        "\033[49m"
}

# Format a Unix epoch reset timestamp for the 5h limit: local time-of-day "→HH:MM"
format_reset_5h() {
    local epoch="$1"
    date -d "@${epoch}" "+→%H:%M" 2>/dev/null || true
}

# Format a Unix epoch reset timestamp for the 7d limit: "→Nd HH:MM" or "→Www HH:MM"
# Uses "→Nd HH:MM" when reset is ≥24h away (shorter), otherwise "→Www HH:MM"
format_reset_7d() {
    local epoch="$1"
    local now
    now=$(date +%s)
    local diff=$(( epoch - now ))
    if [ "$diff" -le 0 ]; then
        date -d "@${epoch}" "+→%H:%M" 2>/dev/null || true
    elif [ "$diff" -ge 86400 ]; then
        local days=$(( diff / 86400 ))
        local time_part
        time_part=$(date -d "@${epoch}" "+%H:%M" 2>/dev/null || true)
        printf "→%dd %s" "$days" "$time_part"
    else
        date -d "@${epoch}" "+→%a %H:%M" 2>/dev/null || true
    fi
}

# ---------------------------------------------------------------------------
# Git worktree segment
#
# Shows main worktree branch and, when running inside a secondary worktree,
# the current worktree basename + its branch.
# Format (secondary): [ main:<branch> | wt:<basename>@<branch> ]
# Format (main):      [ main:<branch> ]
# Detached HEAD: shows short SHA (7 chars) prefixed with #
#
# Cached in /tmp for 2 seconds, keyed by uid + repo root, so separate repos
# and separate users each get their own cache entry.
# ---------------------------------------------------------------------------
git_segment() {
    # Resolve current worktree root — if this fails we're not in a git repo
    local cwd_root
    cwd_root=$(git rev-parse --show-toplevel 2>/dev/null) || return

    # Cache key: uid + sanitised repo root path
    local cache_key
    cache_key=$(printf '%s' "$cwd_root" | tr -cs 'a-zA-Z0-9' '_')
    local cache_file="/tmp/claude-git-wt-$(id -u)-${cache_key}.txt"
    local now
    now=$(date +%s)

    # Serve from cache if it exists and is <2 seconds old
    if [ -f "$cache_file" ]; then
        local age=$(( now - $(stat -c %Y "$cache_file" 2>/dev/null || echo 0) ))
        if [ "$age" -lt 2 ]; then
            cat "$cache_file"
            return
        fi
    fi

    # Parse git worktree list --porcelain.
    # Records are separated by blank lines; the first record is always the
    # main worktree.  We track two records: main (index 0) and whichever
    # record's worktree path matches cwd_root.
    local main_path="" main_branch="" main_sha="" main_detached=0
    local cur_path=""  cur_branch=""  cur_sha=""  cur_detached=0
    local record_index=0
    local in_path="" in_branch="" in_sha="" in_detached=0

    # Inline flush: called on blank line or after the final record.
    # Reads in_* variables, writes main_* / cur_* as appropriate.
    _gs_flush() {
        [ -z "$in_path" ] && return
        if [ "$record_index" -eq 0 ]; then
            main_path="$in_path"; main_branch="$in_branch"
            main_sha="$in_sha";   main_detached=$in_detached
        fi
        if [ "$in_path" = "$cwd_root" ]; then
            cur_path="$in_path"; cur_branch="$in_branch"
            cur_sha="$in_sha";   cur_detached=$in_detached
        fi
        record_index=$(( record_index + 1 ))
        in_path=""; in_branch=""; in_sha=""; in_detached=0
    }

    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in
            worktree\ *)  in_path="${line#worktree }" ;;
            branch\ *)    in_branch="${line#branch refs/heads/}" ;;
            HEAD\ *)      in_sha="${line#HEAD }" ;;
            detached)     in_detached=1 ;;
            "")           _gs_flush ;;
        esac
    done < <(git worktree list --porcelain 2>/dev/null)
    _gs_flush   # flush final record (no trailing blank line in porcelain output)
    unset -f _gs_flush

    # Nothing parsed — git not available or not a repo
    [ -z "$main_path" ] && return

    # Format a ref for display: branch name, or #<short-sha> if detached.
    local main_ref cur_ref
    if [ "$main_detached" -eq 1 ]; then
        main_ref="#${main_sha:0:7}"
    elif [ -n "$main_branch" ]; then
        main_ref="$main_branch"
    else
        main_ref="${main_sha:0:7}"
    fi

    local result
    if [ "$cwd_root" = "$main_path" ] || [ -z "$cur_path" ]; then
        # Standing in the main worktree, or current root not found in list
        result="main:${main_ref}"
    else
        if [ "$cur_detached" -eq 1 ]; then
            cur_ref="#${cur_sha:0:7}"
        elif [ -n "$cur_branch" ]; then
            cur_ref="$cur_branch"
        else
            cur_ref="${cur_sha:0:7}"
        fi
        local wt_name
        wt_name=$(basename "$cur_path")
        result="main:${main_ref} | wt:${wt_name}@${cur_ref}"
    fi

    # Write to cache and emit
    printf '[%s]' "$result" | tee "$cache_file"
}

git_seg=$(git_segment 2>/dev/null)

# Add context percentage with inline bar if available
used=$(echo "$input" | jq -r '.context_window.used_percentage // empty')
if [ -n "$used" ]; then
    status="$status | $(inline_bar "ctx" "$used")"
fi

# Add subscription rate limits from JSON input (5-hour session and 7-day weekly)
five_pct=$(echo "$input"   | jq -r '.rate_limits.five_hour.used_percentage // empty')
five_reset=$(echo "$input" | jq -r '.rate_limits.five_hour.resets_at // empty')
week_pct=$(echo "$input"   | jq -r '.rate_limits.seven_day.used_percentage // empty')
week_reset=$(echo "$input" | jq -r '.rate_limits.seven_day.resets_at // empty')

if [ -n "$five_pct" ]; then
    five_suffix=""
    [ -n "$five_reset" ] && five_suffix=$(format_reset_5h "$five_reset")
    status="$status | $(inline_bar "5h" "$five_pct" "$five_suffix")"
fi
if [ -n "$week_pct" ]; then
    week_suffix=""
    [ -n "$week_reset" ] && week_suffix=$(format_reset_7d "$week_reset")
    status="$status | $(inline_bar "7d" "$week_pct" "$week_suffix")"
fi

# Emit line 1 (model/style/bars), then line 2 (git segment) only if present.
# If Claude Code clips multi-line status to one row, move git_seg back to
# line 1 by appending: status="$status | $git_seg" before the printf.
if [ -n "$git_seg" ]; then
    printf "%s\n%s\n" "$status" "$git_seg"
else
    printf "%s\n" "$status"
fi
