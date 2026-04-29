#!/usr/bin/env bash

# Read JSON input from stdin
input=$(cat)

# Extract values
model=$(echo "$input" | jq -r '.model.display_name')
style=$(echo "$input" | jq -r '.output_style.name // "default"')
cost=$(echo "$input" | jq -r '.cost.total_cost_usd // 0')

# Read effort level from settings file (default if missing)
effort=$(jq -r '.effortLevel // "default"' ~/.claude/settings.json 2>/dev/null || echo "default")

# Build the status line: model (effort) | output style
status="$model ($effort) | output style: $style"

# Add session cost
cost_fmt=$(printf "$%.2f" "$cost")
status="$status | $cost_fmt"

# Add context percentage with progress bar if available
used=$(echo "$input" | jq -r '.context_window.used_percentage // empty')
if [ -n "$used" ]; then
    bar_length=10
    filled=$(printf "%.0f" $(echo "$used / 10" | bc -l))
    if [ "$filled" -lt 0 ]; then filled=0; fi
    if [ "$filled" -gt "$bar_length" ]; then filled=$bar_length; fi
    empty=$((bar_length - filled))

    bar=""
    for ((i=0; i<filled; i++)); do bar="${bar}█"; done
    for ((i=0; i<empty; i++)); do bar="${bar}░"; done

    used_fmt=$(printf "%.0f" "$used")
    status="$status | ctx [${bar}] ${used_fmt}%"
fi

# Attempt to get session limits from claude usage command
# Cache the result for 60 seconds to avoid repeated calls
cache_file="/tmp/claude-usage-cache-$(whoami).txt"
cache_timeout=60

if [ -f "$cache_file" ]; then
    cache_age=$(($(date +%s) - $(stat -c %Y "$cache_file" 2>/dev/null || echo 0)))
    if [ "$cache_age" -gt "$cache_timeout" ]; then
        rm -f "$cache_file"
    fi
fi

if [ ! -f "$cache_file" ]; then
    # Try to run claude usage and parse output
    # Timeout after 1 second to avoid blocking
    timeout 1s claude usage 2>/dev/null | grep -E "(Messages|Resets in)" > "$cache_file" 2>/dev/null || true
fi

# Parse cached usage info if available
if [ -f "$cache_file" ] && [ -s "$cache_file" ]; then
    daily_pct=$(grep -oP "Daily.*?\(\K[0-9]+(?=%\))" "$cache_file" 2>/dev/null || echo "")
    hourly_pct=$(grep -oP "Hourly.*?\(\K[0-9]+(?=%\))" "$cache_file" 2>/dev/null || echo "")
    reset_time=$(grep -oP "Resets in \K[^$]*" "$cache_file" 2>/dev/null | tr -d '\n' || echo "")

    # Add to status line
    if [ -n "$daily_pct" ] || [ -n "$hourly_pct" ]; then
        limit_info=""
        if [ -n "$hourly_pct" ]; then
            limit_info="H:${hourly_pct}%"
        fi
        if [ -n "$daily_pct" ]; then
            [ -n "$limit_info" ] && limit_info="$limit_info "
            limit_info="${limit_info}D:${daily_pct}%"
        fi
        if [ -n "$reset_time" ]; then
            limit_info="$limit_info (${reset_time})"
        fi
        status="$status | $limit_info"
    fi
fi

echo "$status"
