#!/usr/bin/env python3
"""
sptlrx-scaled: A wrapper for sptlrx that scales lyrics timing for slowed songs.

This version continuously monitors for song changes and automatically
scales lyrics when a slowed song is detected.
"""

import subprocess
import sys
import os
import re
import time
import signal
import requests
import shutil
from pathlib import Path

# Configuration
PLAYERS = ["edge", "chromium", "chrome", "firefox"]
SCALED_LYRICS_DIR = Path.home() / ".cache" / "sptlrx-scaled"
LOG_FILE = Path.home() / ".cache" / "sptlrx-scaled" / "debug.log"

def log(msg):
    """Write message to log file."""
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")

# Patterns to detect slowed songs
SLOWED_PATTERNS = [
    r'\((super\s*)?slowed\s*[\+&]?\s*reverb\)',   # (slowed + reverb), (super slowed + reverb)
    r'\((super\s*)?slowed\)',                      # (slowed), (super slowed)
    r'\[(super\s*)?slowed\s*[\+&]?\s*reverb\]',   # [slowed + reverb], [super slowed]
    r'\[(super\s*)?slowed\]',                      # [slowed], [super slowed]
    r'~\s*(super\s*)?slowed',                      # ~ slowed, ~ super slowed
    r'-\s*(super\s*)?slowed',                      # - slowed, - super slowed
    r'(super\s*)?slowed\s*(and|\+|&)?\s*reverb',  # slowed and reverb, super slowed + reverb
    r'(super\s*)?slowed\s*version',               # slowed version
    r'☆\s*deluxe',                                 # ☆ deluxe
    r'sped\s*down',                                # sped down
    r'pitched\s*down',                             # pitched down
]

# Global for cleanup
original_config_backed_up = False
fallback_mode = False  # True when showing music icon instead of lyrics

def cleanup(signum=None, frame=None):
    """Cleanup on exit."""
    # Show cursor again
    sys.stdout.write("\033[?25h")
    sys.stdout.flush()
    sys.exit(0)

signal.signal(signal.SIGINT, cleanup)
signal.signal(signal.SIGTERM, cleanup)

def run_playerctl(*args):
    """Run playerctl command and return output."""
    try:
        cmd = ["playerctl", f"--player={','.join(PLAYERS)}"] + list(args)
        return subprocess.check_output(cmd, stderr=subprocess.DEVNULL).decode().strip()
    except subprocess.CalledProcessError:
        return None

def get_metadata():
    """Get current song metadata."""
    title = run_playerctl("metadata", "xesam:title")
    artist = run_playerctl("metadata", "xesam:artist") 
    length = run_playerctl("metadata", "mpris:length")
    
    if not title:
        return None
    
    return {
        "title": title,
        "artist": artist or "",
        "length_us": int(length) if length else None,
        "length_sec": int(length) / 1_000_000 if length else None
    }

def is_slowed_song(title):
    """Check if the song title indicates it's a slowed version."""
    title_lower = title.lower()
    for pattern in SLOWED_PATTERNS:
        if re.search(pattern, title_lower):
            return True
    return False

def parse_title(raw_title):
    """
    Parse YouTube Music title to extract artist and song name.
    Returns (part1, part2) where the order could be:
    - Artist - Song  OR  Song - Artist
    We don't know which, so caller should try both.
    """
    # Convert fullwidth characters to ASCII (ｓｌｏｗｅｄ → slowed)
    title = raw_title
    fullwidth_to_ascii = str.maketrans(
        'ａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺ０１２３４５６７８９　',
        'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 '
    )
    title = title.translate(fullwidth_to_ascii)
    
    # Remove YouTube Music suffix FIRST
    title = re.sub(r'\s*-\s*YouTube Music$', '', title)
    
    # Remove EVERYTHING in parentheses and brackets (catches all variations)
    title = re.sub(r'\s*\([^)]*\)', '', title)  # Remove (...)
    title = re.sub(r'\s*\[[^\]]*\]', '', title)  # Remove [...]
    title = re.sub(r'\s*\{[^}]*\}', '', title)   # Remove {...}
    title = re.sub(r'\s*「[^」]*」', '', title)  # Remove Japanese quotes 「...」
    title = re.sub(r'\s*『[^』]*』', '', title)  # Remove Japanese quotes 『...』
    
    # Remove slowed patterns that aren't in brackets
    # Handle "~ Slowed..." format (common on YT)
    title = re.sub(r'\s*~\s*(super\s*)?slowed.*$', '', title, flags=re.IGNORECASE)
    # Handle "- Slowed..." at end
    title = re.sub(r'\s*-\s*(super\s*)?slowed.*$', '', title, flags=re.IGNORECASE)
    # Handle standalone "Slowed and Reverb" or "Super Slowed + Reverb"
    title = re.sub(r'\s*(super\s*)?slowed\s*(and|\+|&)?\s*reverb.*$', '', title, flags=re.IGNORECASE)
    # Handle "Slowed Version"
    title = re.sub(r'\s*(super\s*)?slowed\s*version.*$', '', title, flags=re.IGNORECASE)
    # Handle "sped down" and "pitched down"
    title = re.sub(r'\s*(sped|pitched)\s*down.*$', '', title, flags=re.IGNORECASE)
    
    # Remove non-ASCII characters that are likely junk (like 中ジ芋)
    # But keep common punctuation and accented letters
    title = re.sub(r'[^\x00-\x7F\u00C0-\u024F]+', '', title)
    
    # Clean up trailing separators and spaces
    title = re.sub(r'\s*[~\-|／/]\s*$', '', title)
    title = re.sub(r'\s+', ' ', title).strip()
    
    # Try to split into two parts using various separators
    # Common separators: " - ", "- ", " -", " ~ ", " | ", " － " (fullwidth)
    part1 = None
    part2 = title
    
    # Try separators in order of preference (most specific first)
    for sep in [' - ', ' － ', ' ~ ', ' | ', '- ', ' -', '－', ' / ', '／']:
        if sep in title:
            parts = title.split(sep, 1)
            if len(parts) == 2 and parts[0].strip() and parts[1].strip():
                part1 = parts[0].strip()
                part2 = parts[1].strip()
                break
    
    return part1, part2

def fetch_lyrics_lrclib(song, artist=None, expected_duration=None):
    """
    Fetch synced lyrics from lrclib.net API.
    Optimized for speed - minimal API calls.
    """
    
    song_lower = song.lower().strip()
    artist_lower = artist.lower().strip() if artist else None
    
    # Strategy 1: If we have artist, try exact match first (fastest)
    if artist:
        try:
            resp = requests.get("https://lrclib.net/api/search", 
                              params={"track_name": song, "artist_name": artist}, 
                              timeout=5)
            if resp.status_code == 200:
                results = resp.json()
                for r in results:
                    if r.get("syncedLyrics"):
                        track = r.get("trackName", "").lower().strip()
                        result_artist = r.get("artistName", "").lower().strip()
                        # Check both track AND artist match
                        track_matches = (track == song_lower or 
                                        track.replace(" ", "") == song_lower.replace(" ", ""))
                        artist_matches = (result_artist == artist_lower or
                                         result_artist.replace(" ", "") == artist_lower.replace(" ", ""))
                        if track_matches and artist_matches:
                            log(f"  ✓ Found lyrics by '{r.get('artistName')}'")
                            return {
                                "lyrics": r["syncedLyrics"],
                                "duration": r.get("duration"),
                                "title": r.get("trackName"),
                                "artist": r.get("artistName")
                            }
        except:
            pass
        # If artist was provided but no match found, return None
        # Let the caller try a different song/artist combination
        return None
    
    # Strategy 2: Generic search by track name only (when no artist provided)
    try:
        resp = requests.get("https://lrclib.net/api/search", 
                          params={"track_name": song}, 
                          timeout=5)
        if resp.status_code != 200:
            log(f"  ❌ API error")
            return None
            
        results = resp.json()
    except Exception as e:
        log(f"  ❌ Network error")
        return None
    
    # Filter for exact title matches with synced lyrics
    candidates = []
    for r in results:
        if not r.get("syncedLyrics"):
            continue
        track = r.get("trackName", "").lower().strip()
        if track == song_lower or track.replace(" ", "") == song_lower.replace(" ", ""):
            candidates.append(r)
    
    if not candidates:
        log(f"  ❌ No lyrics found for '{song}'")
        return None
    
    # Group by artist
    by_artist = {}
    for c in candidates:
        a = c.get("artistName", "Unknown")
        if a not in by_artist:
            by_artist[a] = []
        by_artist[a].append(c)
    
    artists = list(by_artist.keys())
    log(f"  📋 Found '{song}' by {len(artists)} artist(s): {', '.join(artists[:3])}")
    
    # Single artist = easy
    if len(artists) == 1:
        best = candidates[0]
        return {
            "lyrics": best["syncedLyrics"],
            "duration": best.get("duration"),
            "title": best.get("trackName"),
            "artist": best.get("artistName")
        }
    
    # Multiple artists - use duration to pick best match
    if expected_duration:
        min_dur = expected_duration / 1.8
        max_dur = expected_duration / 1.05
        
        matches = [(c, abs(c.get("duration", 0) - expected_duration/1.3)) 
                   for c in candidates 
                   if c.get("duration") and min_dur <= c.get("duration") <= max_dur]
        
        if matches:
            matches.sort(key=lambda x: x[1])
            best = matches[0][0]
            log(f"  ✓ Best duration match: '{best.get('artistName')}' ({best.get('duration'):.0f}s)")
            return {
                "lyrics": best["syncedLyrics"],
                "duration": best.get("duration"),
                "title": best.get("trackName"),
                "artist": best.get("artistName")
            }
    
    # No duration help - just pick first result
    best = candidates[0]
    log(f"  ⚠️  Multiple artists, using first: '{best.get('artistName')}'")
    return {
        "lyrics": best["syncedLyrics"],
        "duration": best.get("duration"),
        "title": best.get("trackName"),
        "artist": best.get("artistName")
    }

def parse_lrc_timestamp(timestamp):
    """Parse [mm:ss.xx] to milliseconds."""
    match = re.match(r'\[(\d+):(\d+)\.(\d+)\]', timestamp)
    if match:
        mins = int(match.group(1))
        secs = int(match.group(2))
        ms_str = match.group(3)
        if len(ms_str) == 2:
            ms = int(ms_str) * 10
        elif len(ms_str) == 3:
            ms = int(ms_str)
        else:
            ms = int(ms_str[:3])
        return mins * 60 * 1000 + secs * 1000 + ms
    return None

def format_lrc_timestamp(ms):
    """Format milliseconds to [mm:ss.xx]."""
    total_secs = ms / 1000
    mins = int(total_secs // 60)
    secs = total_secs % 60
    return f"[{mins:02d}:{secs:05.2f}]"

def scale_lyrics(lyrics_text, scale_factor):
    """Scale all timestamps in lyrics by the given factor."""
    lines = lyrics_text.split('\n')
    scaled_lines = []
    
    for line in lines:
        match = re.match(r'(\[\d+:\d+\.\d+\])(.*)', line)
        if match:
            timestamp = match.group(1)
            text = match.group(2)
            ms = parse_lrc_timestamp(timestamp)
            if ms is not None:
                scaled_ms = int(ms * scale_factor)
                new_timestamp = format_lrc_timestamp(scaled_ms)
                scaled_lines.append(f"{new_timestamp}{text}")
            else:
                scaled_lines.append(line)
        else:
            scaled_lines.append(line)
    
    return '\n'.join(scaled_lines)

def get_safe_filename(title):
    """Create a safe filename from title."""
    safe_name = re.sub(r'[^\w\s-]', '', title)
    safe_name = re.sub(r'\s+', ' ', safe_name).strip()
    return safe_name[:100]  # Limit length

def save_scaled_lyrics(lyrics_text, raw_title):
    """Save scaled lyrics to cache directory."""
    SCALED_LYRICS_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = get_safe_filename(raw_title)
    filepath = SCALED_LYRICS_DIR / f"{safe_name}.lrc"
    filepath.write_text(lyrics_text)
    return filepath

def clear_cache():
    """Clear all cached scaled lyrics."""
    if SCALED_LYRICS_DIR.exists():
        for f in SCALED_LYRICS_DIR.glob("*.lrc"):
            f.unlink()

def show_music_icon(message=""):
    """
    Display a centered music icon as fallback when lyrics aren't available.
    """
    import shutil as sh
    
    music_icon = "♫"
    cols, rows = sh.get_terminal_size((80, 24))
    
    # Clear and position
    sys.stdout.write("\033[2J\033[H\033[?25l")
    
    center_row = rows // 2
    center_col = (cols - len(music_icon)) // 2
    
    sys.stdout.write(f"\033[{center_row};{center_col}H")
    sys.stdout.write(f"\033[1;37m{music_icon}\033[0m")
    
    if message:
        msg_col = (cols - len(message)) // 2
        sys.stdout.write(f"\033[{center_row + 2};{msg_col}H")
        sys.stdout.write(f"\033[90m{message}\033[0m")
    
    sys.stdout.flush()

def get_position():
    """Get current playback position in milliseconds."""
    try:
        cmd = ["playerctl", f"--player={','.join(PLAYERS)}", "position"]
        pos = subprocess.check_output(cmd, stderr=subprocess.DEVNULL).decode().strip()
        return int(float(pos) * 1000)  # Convert seconds to ms
    except:
        return None

def parse_lyrics_file(filepath):
    """Parse LRC file into list of (timestamp_ms, text) tuples."""
    lines = []
    with open(filepath, 'r') as f:
        for line in f:
            line = line.strip()
            match = re.match(r'\[(\d+):(\d+)\.(\d+)\](.*)', line)
            if match:
                mins = int(match.group(1))
                secs = int(match.group(2))
                ms_str = match.group(3)
                if len(ms_str) == 2:
                    ms = int(ms_str) * 10
                elif len(ms_str) == 3:
                    ms = int(ms_str)
                else:
                    ms = int(ms_str[:3])
                timestamp_ms = mins * 60 * 1000 + secs * 1000 + ms
                text = match.group(4).strip()
                lines.append((timestamp_ms, text))
    return lines

def find_current_line(lyrics, position_ms):
    """Find the index of the current lyric line based on position."""
    current_idx = 0
    for i, (timestamp, _) in enumerate(lyrics):
        if timestamp <= position_ms:
            current_idx = i
        else:
            break
    return current_idx

def display_lyrics(lyrics, current_idx, title, artist):
    """Display lyrics centered in terminal with current line highlighted."""
    import shutil as sh
    
    cols, rows = sh.get_terminal_size((80, 24))
    
    # Clear screen
    sys.stdout.write("\033[2J\033[H\033[?25l")
    
    # Header
    header = f"{title} - {artist}" if artist else title
    if len(header) > cols - 4:
        header = header[:cols-7] + "..."
    header_col = (cols - len(header)) // 2
    sys.stdout.write(f"\033[1;{header_col}H\033[1;38;5;250m{header}\033[0m")
    
    # Calculate how many lines we can show
    available_rows = rows - 4  # Leave room for header and padding
    lines_before = available_rows // 2
    lines_after = available_rows - lines_before - 1
    
    # Get the range of lines to display
    start_idx = max(0, current_idx - lines_before)
    end_idx = min(len(lyrics), current_idx + lines_after + 1)
    
    # Center vertically
    start_row = 3
    
    for i, idx in enumerate(range(start_idx, end_idx)):
        _, text = lyrics[idx]
        row = start_row + i
        
        # Truncate if too long
        if len(text) > cols - 4:
            text = text[:cols-7] + "..."
        
        col = (cols - len(text)) // 2
        sys.stdout.write(f"\033[{row};{col}H")
        
        if idx == current_idx:
            # Current line - bold white
            sys.stdout.write(f"\033[1;37m{text}\033[0m")
        elif idx < current_idx:
            # Past lines - gray
            sys.stdout.write(f"\033[38;5;245m{text}\033[0m")
        else:
            # Future lines - dim
            sys.stdout.write(f"\033[38;5;240m{text}\033[0m")
    
    sys.stdout.flush()

def run_lyrics_display(lyrics_file, title, artist):
    """
    Run the custom lyrics display, syncing with playerctl position.
    Returns when song changes or playback stops.
    """
    lyrics = parse_lyrics_file(lyrics_file)
    if not lyrics:
        return "no_lyrics"
    
    last_idx = -1
    
    while True:
        # Check if song changed
        metadata = get_metadata()
        if not metadata or metadata['title'] != title:
            return "song_changed"
        
        # Get current position
        position = get_position()
        if position is None:
            return "no_position"
        
        # Find current line
        current_idx = find_current_line(lyrics, position)
        
        # Only redraw if line changed
        if current_idx != last_idx:
            display_lyrics(lyrics, current_idx, title, artist)
            last_idx = current_idx
        
        time.sleep(0.1)  # 100ms refresh rate

def process_song(metadata, is_slowed=False):
    """
    Process any song and prepare lyrics.
    For slowed songs, scales the timestamps.
    For normal songs, uses lyrics as-is.
    Returns True if lyrics were successfully prepared.
    """
    raw_title = metadata['title']
    current_duration = metadata['length_sec']
    metadata_artist = metadata.get('artist', '').strip() or None
    part1, part2 = parse_title(raw_title)
    
    # Build search attempts - order depends on song type
    attempts = []
    
    if is_slowed:
        # For slowed songs: artist is usually in the title
        # Format: "Song - Artist (slowed)" or "Artist - Song (slowed)"
        if part1:
            # Try part1 as song, part2 as artist (Song - Artist format)
            attempts.append((part1, part2))
            # Try part2 as song, part1 as artist (Artist - Song format)
            attempts.append((part2, part1))
        # Fallback to metadata artist
        if metadata_artist:
            if part1:
                attempts.append((part1, metadata_artist))
                attempts.append((part2, metadata_artist))
            else:
                attempts.append((part2, metadata_artist))
        # Last resort: no artist
        if part1:
            attempts.append((part1, None))
        attempts.append((part2, None))
    else:
        # For normal songs: metadata artist (uploader) is usually correct
        if metadata_artist:
            if part1:
                attempts.append((part1, metadata_artist))
                attempts.append((part2, metadata_artist))
            else:
                attempts.append((part2, metadata_artist))
        # Fallback to parsing from title
        if part1:
            attempts.append((part1, part2))
            attempts.append((part2, part1))
        # Last resort: no artist
        if part1:
            attempts.append((part1, None))
        attempts.append((part2, None))
    
    # Remove duplicates while preserving order
    seen = set()
    unique_attempts = []
    for attempt in attempts:
        if attempt not in seen:
            seen.add(attempt)
            unique_attempts.append(attempt)
    attempts = unique_attempts
    
    lyrics_data = None
    for song, artist in attempts:
        log(f"🔍 Trying: Song='{song}', Artist='{artist or 'Unknown'}'")
        lyrics_data = fetch_lyrics_lrclib(song, artist, expected_duration=current_duration if is_slowed else None)
        if lyrics_data:
            break
    
    if not lyrics_data:
        log("❌ Could not find matching lyrics")
        return False
    
    log(f"✅ Found: '{lyrics_data['title']}' by '{lyrics_data['artist']}'")
    
    original_duration = lyrics_data.get('duration')
    lyrics_text = lyrics_data['lyrics']
    
    # For slowed songs, scale the timestamps
    if is_slowed and original_duration and current_duration:
        # Sanity check: slowed songs should be LONGER than original
        if current_duration < original_duration * 0.9:
            log(f"⚠️  Durations don't match slowed pattern, using as-is")
        elif current_duration > original_duration * 2.5:
            log(f"⚠️  Scale factor too high ({current_duration/original_duration:.1f}x), using as-is")
        else:
            scale_factor = current_duration / original_duration
            log(f"⏱️  Original: {original_duration:.1f}s → Current: {current_duration:.1f}s")
            log(f"📊 Scale factor: {scale_factor:.3f}x")
            lyrics_text = scale_lyrics(lyrics_text, scale_factor)
    
    # Clear old cache and save lyrics
    clear_cache()
    lrc_path = save_scaled_lyrics(lyrics_text, raw_title)
    log(f"💾 Saved: {lrc_path.name}")
    
    return True

def main():
    global original_config_backed_up, fallback_mode
    
    # Clear old log on startup
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    if LOG_FILE.exists():
        LOG_FILE.unlink()
    log("sptlrx-scaled started")
    
    # Hide cursor
    sys.stdout.write("\033[?25l")
    sys.stdout.flush()
    
    last_title = None
    
    try:
        while True:
            metadata = get_metadata()
            
            # No music playing - show fallback icon
            if not metadata:
                fallback_mode = True
                last_title = None
                show_music_icon("No music playing")
                time.sleep(1)
                continue
            
            current_title = metadata['title']
            current_artist = metadata.get('artist', '')
            
            # Check for invalid/generic titles (YouTube Music not exposing metadata)
            if current_title.lower() in ["youtube music", "youtube", ""]:
                fallback_mode = True
                last_title = None
                show_music_icon("Waiting for song info...")
                time.sleep(1)
                continue
            
            # Check if song changed
            if current_title != last_title:
                last_title = current_title
                fallback_mode = False
                log(f"Now playing: {current_title}")
                
                # Check if it's a slowed song
                is_slowed = is_slowed_song(current_title)
                if is_slowed:
                    log("Detected slowed song")
                
                # Fetch and process lyrics
                lyrics_found = process_song(metadata, is_slowed=is_slowed)
                
                if lyrics_found:
                    log("Starting lyrics display")
                    
                    # Find the lyrics file
                    safe_name = get_safe_filename(current_title)
                    lyrics_file = SCALED_LYRICS_DIR / f"{safe_name}.lrc"
                    
                    # Run our custom lyrics display
                    result = run_lyrics_display(lyrics_file, current_title, current_artist)
                    
                    if result == "song_changed":
                        continue  # Loop will detect new song
                    elif result == "no_position":
                        show_music_icon("Playback stopped")
                        last_title = None
                        time.sleep(1)
                else:
                    # No lyrics found - show fallback icon
                    log("No lyrics - showing fallback")
                    fallback_mode = True
            
            # If in fallback mode, show the icon and wait
            if fallback_mode:
                show_music_icon("Lyrics not available")
                time.sleep(1)
            
    except KeyboardInterrupt:
        pass
    finally:
        # Show cursor again
        sys.stdout.write("\033[?25h")
        sys.stdout.flush()
        cleanup()

if __name__ == "__main__":
    main()
