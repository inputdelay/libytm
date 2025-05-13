from flask import Flask, request, Response, send_file
from flask_caching import Cache
from flask_cors import CORS
from ytmusicapi import YTMusic
import requests
import subprocess
import os
import sys
import io
import urllib.parse
import yt_dlp
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv

# Load environment variables from .env file
if os.path.exists('.env'):
    load_dotenv()

# Ensure cache directories exist
os.makedirs("cache", exist_ok=True)
os.makedirs("cache/segments", exist_ok=True)

# Initialize yt-dlp and ytmusicapi
# yt_dlp.YoutubeDL() instance isn't strictly needed globally if only used in subprocess.run
# ytdlp = yt_dlp.YoutubeDL() # Keep for reference, but not used directly below
ytmusic = YTMusic()

# Flask app configuration for caching
config = {
    "CACHE_TYPE": "SimpleCache",
    "CACHE_DEFAULT_TIMEOUT": 120 # Cache timeout for routes using @cache.cached
}
app = Flask(__name__)
app.config.from_mapping(config)

# --- UPDATED CORS CONFIGURATION ---
# List of allowed origins, including common local dev addresses and both production forms
allowed_origins = [
    "http://localhost:8000",
    "http://localhost:8080", # Common alternative local dev port
    "http://127.0.0.1:8000", # Also useful for local testing
    "http://127.0.0.1:8080",
    "https://pulsing.netlify.app",    # Production origin without trailing slash
    "https://pulsing.netlify.app/",   # Production origin with trailing slash
    # Add other allowed origins here if needed
]

# Apply CORS to all routes (*) with the specified list of allowed origins
CORS(app, resources={r"/*": {"origins": allowed_origins}})
# --- END UPDATED CORS CONFIGURATION ---


cache = Cache(app)

# Dictionary to store segment information: {segment_filename: {original_url, temp_path, status, timestamp}}
segment_cache = {}
# Thread pool for downloading segments
segment_download_executor = ThreadPoolExecutor(max_workers=4)
# Lock for accessing segment_cache
segment_cache_lock = threading.Lock()

TEMP_SEGMENT_DIR = os.path.join("cache", "segments")
SEGMENT_PURGE_INTERVAL = 60 * 30 # Purge every 30 minutes
SEGMENT_LIFETIME = 60 * 60 * 3 # 3 hours

def download_segment_task(segment_filename, original_url, temp_path):
    """Downloads a single TS segment and updates the cache."""
    # print(f"Starting download for segment {segment_filename} from {original_url}") # Uncomment for verbose segment logging
    try:
        # Add a timeout for fetching individual segments
        response = requests.get(original_url, stream=True, timeout=10)
        response.raise_for_status()
        with open(temp_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
        with segment_cache_lock:
            if segment_filename in segment_cache:
                segment_cache[segment_filename]['status'] = 'downloaded'
                segment_cache[segment_filename]['timestamp'] = time.time()
                # print(f"Segment {segment_filename} downloaded successfully.") # Uncomment for verbose segment logging
            else:
                 # This case should ideally not happen if logic is correct, but good to log
                 print(f"warning: segment {segment_filename} finished download but was removed from cache?")
                 # Clean up the downloaded file if its entry is gone
                 if os.path.exists(temp_path):
                     try: os.remove(temp_path); print(f"cleaned up orphaned segment file {temp_path}")
                     except OSError as e: print(f"error cleaning up orphaned segment file {temp_path}: {str(e)}")

    except requests.exceptions.RequestException as e:
        print(f"error downloading segment {segment_filename} from {original_url}: {str(e)}")
        with segment_cache_lock:
            if segment_filename in segment_cache:
                segment_cache[segment_filename]['status'] = 'failed'
                segment_cache[segment_filename]['timestamp'] = time.time() # Update timestamp even on failure

    except Exception as e:
         print(f"unexpected error in segment download task {segment_filename}: {str(e)}")
         with segment_cache_lock:
            if segment_filename in segment_cache:
                segment_cache[segment_filename]['status'] = 'failed'
                segment_cache[segment_filename]['timestamp'] = time.time()


def start_segment_downloads(segments_info):
    """Submits segment download tasks to the thread pool."""
    for segment_filename, original_url, temp_path in segments_info:
        segment_download_executor.submit(download_segment_task, segment_filename, original_url, temp_path)
        # print(f"Submitted download task for {segment_filename}") # Uncomment for verbose segment logging


def purge_old_segments():
    """Background task to periodically remove old segment files and cache entries."""
    print("Starting segment purging thread...")
    while True:
        current_time = time.time()
        to_purge = []
        with segment_cache_lock:
            # Create a list of items to purge first, then modify the cache
            # This avoids "dictionary changed size during iteration" errors
            segment_filenames_to_check = list(segment_cache.keys())
            for segment_filename in segment_filenames_to_check:
                 info = segment_cache.get(segment_filename) # Get the info again in case it changed
                 if info and current_time - info.get('timestamp', 0) > SEGMENT_LIFETIME:
                    to_purge.append((segment_filename, info['temp_path']))
                    # Remove from cache immediately inside the lock
                    del segment_cache[segment_filename]


        # Now purge the files outside the lock
        for segment_filename, temp_path in to_purge:
            try:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
                    print(f"purged old segment file: {temp_path}")
            except OSError as e:
                print(f"error purging segment file {temp_path}: {str(e)}")
            except Exception as e:
                 print(f"unexpected error purging segment file {temp_path}: {str(e)}")


        time.sleep(SEGMENT_PURGE_INTERVAL)

# Start the background purging thread
purging_thread = threading.Thread(target=purge_old_segments, daemon=True)
purging_thread.start()


# Note: The original get_audio function using yt-dlp to download a single file (mp3/opus/m4a)
# is kept, but the frontend is using the HLS streamHLS endpoint, so this route might not be used often.
def get_audio(video_url, id):
    """Downloads a single audio file using yt-dlp."""
    print(f"Starting single audio download for {video_url} (ID: {id})")
    cmd = [
        sys.executable, "-m", "yt_dlp",
        video_url,
        "-x", # Extract audio
        "--audio-format", "best", # Choose best audio format available
        "--no-playlist", # Ensure only the single video is processed
        "--force-keyframes-at-chapters", # Might help with seeking
        "--output", f"{os.getcwd()}/cache/{id}.%(ext)s" # Output filename format
    ]
    cookies_path = os.environ.get('COOKIES')
    if cookies_path and os.path.exists(cookies_path): # Check if cookies file exists
        cmd.extend(['--cookies', cookies_path])
        print(f"Using cookies from: {cookies_path}")
    else:
        print("No valid COOKIES environment variable or file found, proceeding without cookies.")

    print(f"Executing yt-dlp command: {' '.join(cmd)}")
    # Added a timeout for the entire yt-dlp download process
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=120) # Increased timeout for download

    print("yt-dlp stdout:", result.stdout)
    print("yt-dlp stderr:", result.stderr)


    if result.returncode != 0:
        error_output = result.stderr or result.stdout or "Unknown yt-dlp error"
        raise Exception(f"yt-dlp failed: {error_output}")

    # Find the downloaded file based on the expected output format
    # yt-dlp outputs the final filename to stdout if successful
    downloaded_file = None
    # Attempt to parse stdout for the destination path
    for line in result.stdout.splitlines():
        if "[ExtractAudio]" in line and "Destination:" in line:
             parts = line.split("Destination:")
             if len(parts) > 1:
                 downloaded_file = parts[1].strip()
                 print(f"Parsed destination from stdout: {downloaded_file}")
                 break
        if "[Merger]" in line and "Destination:" in line: # Sometimes Merged (e.g. video+audio or formats)
             parts = line.split("Destination:")
             if len(parts) > 1:
                 downloaded_file = parts[1].strip()
                 print(f"Parsed destination from stdout (Merger): {downloaded_file}")
                 break


    # Fallback if parsing stdout fails or file doesn't exist at reported path
    if not downloaded_file or not os.path.exists(downloaded_file):
         print("Could not find downloaded file path from yt-dlp output or file doesn't exist. Searching cache dir...")
         cache_dir = os.path.join(os.getcwd(), "cache")
         # List files starting with the ID and ending with common audio extensions
         potential_files = [f for f in os.listdir(cache_dir) if f.startswith(f"{id}.") and (f.endswith('.opus') or f.endswith('.m4a') or f.endswith('.mp3') or f.endswith('.aac') or f.endswith('.webm') or f.endswith('.ogg'))]
         # Sort by modified time descending to get the most recent one
         potential_files.sort(key=lambda x: os.path.getmtime(os.path.join(cache_dir, x)), reverse=True)

         if potential_files:
             downloaded_file = os.path.join(cache_dir, potential_files[0])
             print(f"Found potential file in cache dir: {downloaded_file}")

         if not downloaded_file or not os.path.exists(downloaded_file):
              raise Exception(f"yt-dlp finished without error but could not find downloaded file for ID: {id}. Looked for {id}.* in cache. stdout: {result.stdout[:500]}, stderr: {result.stderr[:500]}")


    return downloaded_file # Return the path to the downloaded file


@app.route("/")
def hi():
    return {"hello":"this is a libytm instance"}

# Proxy route for images and potentially other assets
# NOTE: This route is covered by the updated CORS config.
@app.route("/lh3Proxy/<path:url>")
def lh3(url:str):
    # Decode the URL path segment first
    decoded_url = urllib.parse.unquote(url)

    # Basic check if it looks like a full URL
    if not decoded_url.startswith("http://") and not decoded_url.startswith("https://"):
         print(f"Attempted proxy access with non-absolute URL: {decoded_url}")
         return {"error":"Provided path is not a valid absolute URL."}, 422


    # Add more checks if needed based on allowed external domains
    allowed_domains = ("googleusercontent.com", "ytimg.com", "googlevideo.com", "i.ytimg.com")
    # Check if the hostname ends with any of the allowed domains
    # This is slightly more robust than startswith on the whole URL
    try:
        parsed_url = urllib.parse.urlparse(decoded_url)
        if not parsed_url.hostname or not parsed_url.hostname.endswith(allowed_domains):
             print(f"Attempted proxy access to disallowed domain: {parsed_url.hostname} (from {decoded_url})")
             return {"error":"Access to this external URL's domain is not allowed via proxy."}, 422
    except Exception as e:
         print(f"Error parsing URL {decoded_url}: {str(e)}")
         return {"error":"Invalid URL format."}, 422


    print(f"Proxying request for: {decoded_url}")
    headers = {
       "Accept":'*/*',
       # Identify your service, recommended for external requests
       "User-Agent":"Mozilla/5.0 (compatible; InputDelayMusic/1.0; +https://pulsing.netlify.app)"
    }
    try:
        # Use a timeout for the proxy request
        res = requests.get(decoded_url, headers=headers, timeout=15) # Added timeout
        res.raise_for_status() # Raise an exception for bad status codes (4xx or 5xx)

        # Pass through relevant headers, especially Content-Type
        response_headers = {
            "Content-Type": res.headers.get("Content-Type"),
            "Content-Length": res.headers.get("Content-Length"), # Optional: Helps browser know size
            "Cache-Control": res.headers.get("Cache-Control", "public, max-age=3600"), # Pass through or set a default
        }
        # Remove None values
        response_headers = {k: v for k, v in response_headers.items() if v is not None}

        return Response(res.content, res.status_code, response_headers)

    except requests.exceptions.Timeout:
        print(f"Timeout proxying URL {decoded_url}")
        return {"error": "Proxy request to external resource timed out."}, 504 # Gateway Timeout

    except requests.exceptions.RequestException as e:
        print(f"Error proxying URL {decoded_url}: {str(e)}")
        return {"error": f"Failed to fetch external resource: {str(e)}"}, 502 # Bad Gateway or Internal Server Error

    except Exception as e:
        print(f"Unexpected error in proxy route for {decoded_url}: {str(e)}")
        return {"error": "Internal server error during proxy request."}, 500


@cache.cached(timeout=300)
@app.route("/song/<id>")
def getSong(id):
    # Increased retry logic and added more robust error handling
    tries = 0
    song = None
    while tries < 5:
        try:
            # Ensure signatureTimestamp is correctly obtained if necessary
            # ytmusicapi v1.0.0+ handles this automatically, but keeping the call is safe
            # sig_timestamp = ytmusic.get_signatureTimestamp() # Not needed in recent versions
            song = ytmusic.get_song(videoId=id) # signatureTimestamp might not be needed
            if song and song.get("videoDetails"):
                break # Successfully got details
            print(f"Attempt {tries+1}: get_song returned data but no videoDetails for ID {id}. Response keys: {song.keys() if song else 'None'}")
        except Exception as e:
            print(f"Attempt {tries+1}: Error fetching song {id}: {str(e)}")
        tries += 1
        time.sleep(0.5) # Wait a bit before retrying

    if song and song.get("videoDetails"):
        # Add thumbnail proxying here
        video_details = song["videoDetails"]
        if video_details.get("thumbnail", {}).get("thumbnails"):
             # Assuming the best thumbnail is the last one in the list (often highest resolution)
             thumbnails = video_details["thumbnail"]["thumbnails"]
             if thumbnails:
                 best_thumbnail_url = thumbnails[-1].get("url")
                 if best_thumbnail_url:
                      # Encode the *original* thumbnail URL for the proxy
                      encoded_proxy_url = urllib.parse.quote_plus(best_thumbnail_url)
                      # Replace the original URL with the proxy URL
                      video_details["thumbnail"]["thumbnails"] = [{"url": f"{request.url_root.rstrip('/')}/lh3Proxy/{encoded_proxy_url}"}]
                      # Also update the main 'thumbnail' field if it exists
                      if 'thumbnail' in video_details and 'url' in video_details['thumbnail']:
                           video_details['thumbnail']['url'] = f"{request.url_root.rstrip('/')}/lh3Proxy/{encoded_proxy_url}"

        return video_details
    elif song is not None:
         # Got a response, but missing videoDetails (might indicate geo restriction or API change)
         return {"error":"Could not find song details (API response missing 'videoDetails'). The song might be unavailable or restricted."}, 404
    else:
        # Did not get any valid response after retries
        return {"error":"Could not fetch song details from API after multiple retries. Check logs for API errors."}, 500


@cache.cached(timeout=300)
@app.route("/playlist/<id>")
def getPlaylist(id):
    try:
        # ytmusic.get_playlist handles missing playlists by raising an exception
        pl = ytmusic.get_playlist(playlistId=id)
        if pl:
             # Optional: Proxy playlist thumbnails too
             if pl.get("thumbnails"):
                 for thumb in pl["thumbnails"]:
                     if thumb.get("url"):
                         encoded_proxy_url = urllib.parse.quote_plus(thumb["url"])
                         thumb["url"] = f"{request.url_root.rstrip('/')}/lh3Proxy/{encoded_proxy_url}"
             if pl.get("tracks"):
                 for track in pl["tracks"]:
                      if track.get("thumbnail", {}).get("thumbnails"):
                           thumbnails = track["thumbnail"]["thumbnails"]
                           if thumbnails:
                                best_thumbnail_url = thumbnails[-1].get("url")
                                if best_thumbnail_url:
                                     encoded_proxy_url = urllib.parse.quote_plus(best_thumbnail_url)
                                     track["thumbnail"]["thumbnails"] = [{"url": f"{request.url_root.rstrip('/')}/lh3Proxy/{encoded_proxy_url}"}]
                                     if 'thumbnail' in track and 'url' in track['thumbnail']:
                                          track['thumbnail']['url'] = f"{request.url_root.rstrip('/')}/lh3Proxy/{encoded_proxy_url}"


             return pl
        else:
            # Should not happen based on ytmusicapi behavior, but added for safety
            return {"error":"could not find playlist or playlist is empty"}, 404
    except Exception as e:
        print(f"Error fetching playlist {id}: {str(e)}")
        # Check if the exception is likely a "not found" from ytmusicapi
        error_str = str(e).lower()
        if "private or does not exist" in error_str or "invalid playlist id" in error_str or "404" in error_str:
             return {"error":"Could not find playlist (it might be private or does not exist)."}, 404
        else:
             return {"error":"Internal Server Error","errorDetails":str(e)}, 500

# HLS STREAMING ENDPOINTS
# These are covered by the updated CORS configuration

@app.route("/song/<id>/streamHLS.m3u8")
def getstream_experimental(id):
    """Fetches the m3u8 playlist for a song and rewrites segment URLs."""
    # yt-dlp command to get the HLS playlist URL for the best audio stream
    cmd = [
        sys.executable, "-m", "yt_dlp",
        f"https://youtube.com/watch?v={id}",
        "-f", "bestaudio[ext=m4a]/bestaudio[ext=opus]/bestaudio", # Prioritize m4a/opus, then any best audio
        "--no-playlist", # Ensure only the single video is processed
        "-g" # Print the direct URL
    ]
    cookies_path = os.environ.get('COOKIES')
    if cookies_path and os.path.exists(cookies_path): # Check if cookies file exists
        cmd.extend(['--cookies', cookies_path])
        print(f"Using cookies for yt-dlp stream URL fetch from: {cookies_path}")
    else:
        print("No valid COOKIES environment variable or file found for stream URL fetch.")

    print(f"Executing yt-dlp command for stream URL: {' '.join(cmd)}")
    # Added timeout for yt-dlp execution itself
    try:
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=15, check=True) # Added check=True to raise exception on non-zero exit
        m3u8_url = result.stdout.strip()
    except subprocess.CalledProcessError as e:
         error_output = e.stderr or e.stdout or "Unknown yt-dlp error getting stream URL"
         print(f"yt-dlp failed with exit code {e.returncode} for stream URL of {id}: {error_output}")
         return {"error": f"Failed to get streaming URL for song (yt-dlp error): {error_output[:300]}..."}, 500
    except subprocess.TimeoutExpired:
         print(f"yt-dlp timed out getting stream URL for {id}.")
         return {"error": "Timed out getting streaming URL."}, 504 # Gateway Timeout
    except Exception as e:
         print(f"Unexpected error running yt-dlp for stream URL of {id}: {str(e)}")
         return {"error": f"Internal server error getting streaming URL: {str(e)}"}, 500


    print("yt-dlp stdout (stream URL):", m3u8_url)
    # print("yt-dlp stderr (stream URL):", result.stderr) # Can be noisy, uncomment if needed

    if not m3u8_url.startswith("http"):
         print(f"Error: yt-dlp returned non-http URL: {m3u8_url}")
         return {"error": "Failed to get a valid streaming URL from YouTube."}, 500

    print(f"Fetching m3u8 playlist from: {m3u8_url}")
    headers = {
       # Use a more standard User-Agent for fetching the HLS manifest
       "User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36",
       "Accept":"application/x-mpegURL, application/vnd.apple.mpegurl, */*",
       "Referer": f"https://music.youtube.com/watch?v={id}" # Referer might be important
    }

    try:
        # --- FIX APPLIED HERE: Added timeout and specific exception handling ---
        # This request fetches the HLS manifest file from YouTube's servers
        m3u8_response = requests.get(m3u8_url, headers=headers, timeout=15) # ADDED TIMEOUT
        m3u8_response.raise_for_status() # Raise HTTPError for bad responses (4xx or 5xx)
        m3u8_content = m3u8_response.text
        print(f"Successfully fetched m3u8 playlist for {id}.")

    # --- Specific exception handling for Timeout ---
    except requests.exceptions.Timeout:
        print(f"Timeout while fetching m3u8 playlist from {m3u8_url}")
        # Return a Gateway Timeout error as the external service (YouTube) didn't respond in time
        return {"error": "Request to fetch streaming playlist timed out."}, 504

    except requests.exceptions.RequestException as e:
        # This handles other requests errors like HTTPError, ConnectionError etc.
        print(f"Failed to download m3u8 playlist from {m3u8_url}: {str(e)}")
        return {"error": f"Failed to download streaming playlist: {str(e)}"}, 500

    except Exception as e:
        # Catch any other unexpected errors during the fetch or initial processing
        print(f"Unexpected error processing m3u8 for {id}: {str(e)}")
        return {"error": f"Internal server error processing playlist: {str(e)}"}, 500

    # --- Parse and rewrite the playlist ---
    new_m3u8_lines = []
    segments_to_download = []
    # Use urljoin to robustly get the base URL of the manifest
    base_url = urllib.parse.urljoin(m3u8_url, '.')

    for line in m3u8_content.splitlines():
        line = line.strip()
        if line.startswith("#"):
            new_m3u8_lines.append(line)
        elif line.endswith(".ts"):
            # Use urljoin to get the absolute URL of the segment
            original_ts_url = urllib.parse.urljoin(base_url, line)

            # Generate a unique filename for the cached segment
            segment_filename_uuid = uuid.uuid4().hex
            segment_filename = f"{segment_filename_uuid}.ts"
            temp_path = os.path.join(TEMP_SEGMENT_DIR, segment_filename)

            # Add segment info to the cache
            with segment_cache_lock:
                # Add a check to prevent adding if already exists (shouldn't happen with UUID)
                # or if purge thread removed it recently.
                if segment_filename not in segment_cache:
                     segment_cache[segment_filename] = {
                        'original_url': original_ts_url,
                        'temp_path': temp_path,
                        'status': 'pending', # 'pending', 'downloading', 'downloaded', 'failed'
                        'timestamp': time.time() # Timestamp when added/last accessed/status changed
                    }
                     # print(f"Added {segment_filename} to cache (pending)") # Uncomment for verbose segment logging
                else:
                     print(f"warning: segment {segment_filename} already in cache when processing m3u8?")


            # Rewrite the segment URL in the playlist to point back to our Flask app
            # Use request.url_root to get the correct base URL (e.g., https://your-railway-app.railway.app/)
            new_segment_url = f"{request.url_root.rstrip('/')}/song/{id}/segment/{segment_filename}"
            new_m3u8_lines.append(new_segment_url)

            # Add to list for background downloading
            segments_to_download.append((segment_filename, original_ts_url, temp_path))

    # Start downloading the segments in the background
    # The frontend player will request them when needed, and serve_segment will wait if necessary
    if segments_to_download:
         print(f"Starting background downloads for {len(segments_to_download)} segments.")
         start_segment_downloads(segments_to_download)
    else:
        print("No segments found in the m3u8 playlist.")
        # This might indicate an invalid playlist was returned by YouTube/yt-dlp
        return {"error": "No playable segments found in the streaming playlist."}, 500


    # Return the modified m3u8 playlist to the frontend
    # Use the correct MIME type for M3U8 playlists
    print(f"Returning modified m3u8 playlist for {id}.")
    return Response("\n".join(new_m3u8_lines), 200, {"Content-Type": "application/x-mpegURL"})

@app.route("/song/<id>/segment/<segment_filename>")
def serve_segment(id, segment_filename):
    """Serves a cached HLS segment, waiting for download if necessary."""
    # print(f"Received request for segment {segment_filename} (song {id})") # Uncomment for verbose segment logging
    wait_start_time = time.time()
    wait_timeout = 45 # Max seconds to wait for a segment to download

    # --- Wait Loop ---
    # Poll the segment_cache status with a timeout
    while True:
        with segment_cache_lock:
            segment_info = segment_cache.get(segment_filename)

        if not segment_info:
             print(f"Segment {segment_filename} not found in cache.")
             return "Segment Not Found", 404

        status = segment_info['status']
        # print(f"Segment {segment_filename} status: {status}") # Uncomment for verbose segment logging

        if status == 'downloaded':
            # Found it and it's ready!
            break
        elif status == 'failed':
            print(f"Segment {segment_filename} download previously failed.")
            return "Segment Download Failed", 500
        elif status == 'pending': # 'downloading' status could also be pending for this logic
            # Still waiting, check timeout
            if (time.time() - wait_start_time) > wait_timeout:
                print(f"Timeout waiting for segment {segment_filename} download.")
                # Mark as failed on timeout
                with segment_cache_lock:
                    if segment_filename in segment_cache: # Check again before modifying
                         segment_cache[segment_filename]['status'] = 'failed' # Mark as failed on timeout
                         segment_cache[segment_filename]['timestamp'] = time.time()
                return "Segment Download Timeout", 504 # Gateway Timeout

            # Wait a bit before checking again
            time.sleep(0.1) # Wait 100ms

        else:
            # Unknown status
            print(f"Segment {segment_filename} has unknown status: {status}")
            return "Internal Segment Error", 500

    # --- Serve the downloaded file ---
    # We broke out of the loop because status is 'downloaded'
    try:
        # Update timestamp as it's being accessed, extending its cache life
        with segment_cache_lock:
             # Check if it's still in cache and downloaded state before updating/serving
             current_info = segment_cache.get(segment_filename)
             if current_info and current_info['status'] == 'downloaded':
                current_info['timestamp'] = time.time()
                segment_file_path = current_info['temp_path']
             else:
                 # Status changed or removed while we were out of the lock?
                 print(f"Segment {segment_filename} state changed unexpectedly before serving.")
                 # This could happen if the purge thread ran just before acquiring the lock
                 return "Segment State Changed or Removed", 404


        if not os.path.exists(segment_file_path):
             # File disappeared between check and send_file
             print(f"error: segment file {segment_file_path} disappeared before sending.")
             # Mark as failed if file is gone
             with segment_cache_lock:
                  if segment_filename in segment_cache:
                      segment_cache[segment_filename]['status'] = 'failed'
                      segment_cache[segment_filename]['timestamp'] = time.time()
             return "Segment File Not Found On Disk", 404


        # print(f"Serving segment file: {segment_file_path}") # Uncomment for verbose segment logging
        # Use mimetype video/mp2t for MPEG-2 Transport Stream segments
        return send_file(segment_file_path, mimetype="video/mp2t")

    except FileNotFoundError:
        # This should ideally be caught by the os.path.exists check, but as a fallback
        print(f"error: send_file reported FileNotFoundError for {segment_file_path}")
        return "Segment File Not Found (send_file)", 404
    except Exception as e:
        print(f"Unexpected error serving segment {segment_filename}: {str(e)}")
        return {"error": f"Internal error serving segment: {str(e)}"}, 500


# Note: The /song/<id>/stream route uses yt-dlp for a single download
# and is unlikely to be used by the HLS-based frontend player.
# Keeping it for completeness.
@cache.cached(timeout=14400) # Longer cache for the single file download
@app.route("/song/<id>/stream")
def getAudio(id):
    """Provides a direct audio file download/stream (opus/m4a/mp3) using yt-dlp."""
    print(f"Request for single audio stream for song ID: {id}")
    try:
        # Check for existing cached files with common extensions
        cache_dir = os.path.join(os.getcwd(), "cache")
        # List files starting with the ID and ending with common audio extensions
        potential_files = [f for f in os.listdir(cache_dir) if f.startswith(f"{id}.") and (f.endswith('.opus') or f.endswith('.m4a') or f.endswith('.mp3') or f.endswith('.aac') or f.endswith('.webm') or f.endswith('.ogg'))]

        if potential_files:
            # Found an existing cached file, pick the first one (could add logic to pick best extension, e.g., opus)
            cached_file_path = os.path.join(cache_dir, potential_files[0])
            print(f"Serving cached audio file: {cached_file_path}")
            # Guess mimetype based on extension
            mimetype = "audio/mpeg" # Default
            if cached_file_path.endswith('.opus'): mimetype = 'audio/opus'
            elif cached_file_path.endswith('.m4a'): mimetype = 'audio/mp4' # Or audio/aac
            elif cached_file_path.endswith('.mp3'): mimetype = 'audio/mpeg'
            elif cached_file_path.endswith('.aac'): mimetype = 'audio/aac'
            elif cached_file_path.endswith('.webm'): mimetype = 'audio/webm'
            elif cached_file_path.endswith('.ogg'): mimetype = 'audio/ogg'

            return send_file(cached_file_path, mimetype=mimetype)
        else:
            print(f"Cached audio file not found for ID {id}, downloading...")
            # Download the audio file using the get_audio helper
            # The helper function handles potential errors internally and raises exceptions
            downloaded_file_path = get_audio(f"https://youtube.com/watch?v={id}", id=id)

            # After get_audio runs, check again if a file exists (it should now)
            if os.path.exists(downloaded_file_path):
                 print(f"Downloaded audio file: {downloaded_file_path}, serving...")
                 mimetype = "audio/mpeg" # Default, or guess again
                 if downloaded_file_path.endswith('.opus'): mimetype = 'audio/opus'
                 elif downloaded_file_path.endswith('.m4a'): mimetype = 'audio/mp4'
                 elif downloaded_file_path.endswith('.mp3'): mimetype = 'audio/mpeg'
                 elif downloaded_file_path.endswith('.aac'): mimetype = 'audio/aac'
                 elif downloaded_file_path.endswith('.webm'): mimetype = 'audio/webm'
                 elif downloaded_file_path.endswith('.ogg'): mimetype = 'audio/ogg'
                 return send_file(downloaded_file_path, mimetype=mimetype)
            else:
                 # This case indicates an issue with get_audio not saving the file correctly
                 raise Exception("get_audio function failed to create the output file.")

    except FileNotFoundError:
        print(f"Error: Audio file not found after download attempt for ID {id}.")
        return {"error": "Audio file not found after processing."}, 500
    except subprocess.TimeoutExpired:
        print(f"yt-dlp download timed out for ID {id}.")
        return {"error": "Audio download timed out."}, 504
    except Exception as e:
        print(f"Error getting or serving audio stream for ID {id}: {str(e)}")
        # Include the exception type for better debugging
        return {"error": f"Could not get audio stream: {type(e).__name__}: {str(e)}"}, 500


@cache.cached(timeout=300)
@app.route("/song/<id>/lyrics")
def getLyrics(id):
    # Fetches song details first (might already be cached by getSong route)
    # Use the getSong function to benefit from its error handling and potential caching
    song_details_response = getSong(id)
    # Check if getSong returned an error response dictionary
    if isinstance(song_details_response, tuple) and song_details_response[1] != 200:
        # Return the error response from getSong
        return song_details_response
    elif not isinstance(song_details_response, dict):
         # getSong returned something unexpected
         print(f"Error: getSong returned unexpected type: {type(song_details_response)}")
         return {"error": "Failed to get song details for lyrics."}, 500


    try:
        songDetails = song_details_response
        artist_name = songDetails.get("author", "Unknown Artist")
        track_name = songDetails.get("title", "Unknown Title")

        print(f"Fetching lyrics from lrclib for song '{track_name}' by '{artist_name}' (ID: {id})")

        # Use parameters in requests.get
        lyrics_params = {
            "artist_name": artist_name,
            "track_name": track_name,
            # Include album if available, lrclib supports it
            "album_name": songDetails.get("album", {}).get("name", "")
        }
        # Filter out empty values
        lyrics_params = {k: v for k, v in lyrics_params.items() if v}

        # Add timeout to the external lyrics request
        lyrics_response = requests.get("https://lrclib.net/api/get", params=lyrics_params, timeout=10)
        lyrics_response.raise_for_status() # Raise HTTPError for bad responses (4xx or 5xx)

        lyrics_data = lyrics_response.json()

        # lrclib returns { "lyrics": "", "syncedLyrics": "", ...} for no lyrics found,
        # or a dictionary with data if found. Check if syncedLyrics is present and not empty.
        if lyrics_data and lyrics_data.get("syncedLyrics"):
            print(f"Synced lyrics found for '{track_name}'")
            return lyrics_data
        else:
            # Lyrics not found or lrclib returned a structure indicating no lyrics
            print(f"Synced lyrics not found on lrclib for '{track_name}'")
            # Return 404 specifically if lrclib says no lyrics
            return {"error": "Synced lyrics not found for this song on lrclib."}, 404

    except requests.exceptions.Timeout:
         print(f"Timeout while fetching lyrics from lrclib for '{track_name}'")
         return {"error": "Request to fetch lyrics timed out."}, 504

    except requests.exceptions.RequestException as e:
        print(f"Error fetching lyrics from lrclib: {str(e)}")
        # Check for specific HTTP errors from lrclib if needed, but 502 is good generic proxy error
        return {"error": f"Failed to fetch lyrics from external service: {str(e)}"}, 502

    except Exception as e:
        print(f"Unexpected error fetching lyrics for {id}: {str(e)}")
        return {"error":"Internal Server Error fetching lyrics","errorDetails":str(e)}, 500

@cache.cached(timeout=300)
@app.route("/song/<id>/ytmLyrics")
def getYTMLyrics(id):
     # Fetches song details first (might already be cached)
    song_details_response = getSong(id)
    # Check if getSong returned an error response dictionary
    if isinstance(song_details_response, tuple) and song_details_response[1] != 200:
        # Return the error response from getSong
        return song_details_response
    elif not isinstance(song_details_response, dict):
         # getSong returned something unexpected
         print(f"Error: getSong returned unexpected type: {type(song_details_response)}")
         return {"error": "Failed to get song details for YTM lyrics."}, 500


    try:
        # Get the watch playlist first to find the lyrics browseId
        print(f"Attempting to get watch playlist for lyrics browseId for {id}")
        # Use radio=False and limit=1 as we only need the lyrics id
        watch_playlist = ytmusic.get_watch_playlist(videoId=id, radio=False, limit=1)

        lyrics_browse_id = watch_playlist.get("lyrics")
        if not lyrics_browse_id:
            print(f"No lyrics browse ID found in watch playlist for {id}")
            return {"error":"Could not find official YouTube Music lyrics browse ID for this song."}, 404

        print(f"Found lyrics browse ID: {lyrics_browse_id}. Fetching lyrics...")
        # Fetch lyrics using the browseId
        lyrics_data = ytmusic.get_lyrics(browseId=lyrics_browse_id, timestamps=True)

        # ytmusicapi get_lyrics returns a dict like {'lyrics': '...', 'source': '...'}
        # or possibly just {'lyrics': None, 'source': None} if not found.
        # Check if 'lyrics' key exists and is not None/empty string
        if lyrics_data and lyrics_data.get("lyrics"):
             print(f"Successfully fetched YTM lyrics for {id}.")
             return lyrics_data
        else:
            print(f"YTMusic API returned no lyrics data for browse ID {lyrics_browse_id}")
            return {"error":"Could not retrieve official YouTube Music lyrics data."}, 404

    except Exception as e:
        print(f"Error fetching YTM lyrics for {id}: {str(e)}")
        # Check for specific errors indicating no lyrics are available from ytmusicapi
        error_str = str(e)
        if "No lyrics found" in error_str or "could not find lyrics" in error_str: # Example specific error strings
             return {"error":"Official YouTube Music lyrics are not available for this song."}, 404
        # Check if it looks like a private/deleted video issue from the watch playlist step
        elif "private or does not exist" in error_str.lower() or "invalid video id" in error_str.lower():
            return {"error":"Could not get YTM lyrics (song might be private or unavailable)."}, 404
        else:
             return {"error":"Internal Server Error fetching YouTube Music lyrics","errorDetails":str(e)}, 500


@cache.cached(timeout=300)
@app.route("/song/<id>/radio")
def getRadio(id):
    # Fetches song details first (might already be cached)
    song_details_response = getSong(id)
    # Check if getSong returned an error response dictionary
    if isinstance(song_details_response, tuple) and song_details_response[1] != 200:
        # Return the error response from getSong
        return song_details_response
    elif not isinstance(song_details_response, dict):
         # getSong returned something unexpected
         print(f"Error: getSong returned unexpected type: {type(song_details_response)}")
         return {"error": "Failed to get song details for radio."}, 500


    try:
        print(f"Fetching radio playlist for song {id}")
        # The radio=True parameter is key here
        radio = ytmusic.get_watch_playlist(videoId=id, radio=True, limit=50)
        # ytmusicapi get_watch_playlist returns a dict containing playlist info and tracks
        # Check if 'playlistId' and 'tracks' are present and tracks list is not empty
        if radio and radio.get("playlistId") and radio.get("tracks"):
             print(f"Successfully fetched radio playlist for {id}. Playlist ID: {radio['playlistId']} with {len(radio['tracks'])} tracks.")
             # Optional: Proxy thumbnails in the radio response as well
             if radio.get("tracks"):
                 for track in radio["tracks"]:
                      if track.get("thumbnail", {}).get("thumbnails"):
                           thumbnails = track["thumbnail"]["thumbnails"]
                           if thumbnails:
                                best_thumbnail_url = thumbnails[-1].get("url")
                                if best_thumbnail_url:
                                     encoded_proxy_url = urllib.parse.quote_plus(best_thumbnail_url)
                                     track["thumbnail"]["thumbnails"] = [{"url": f"{request.url_root.rstrip('/')}/lh3Proxy/{encoded_proxy_url}"}]
                                     if 'thumbnail' in track and 'url' in track['thumbnail']:
                                          track['thumbnail']['url'] = f"{request.url_root.rstrip('/')}/lh3Proxy/{encoded_proxy_url}"

             return radio
        else:
            print(f"YTMusic API returned no radio playlist or no tracks for {id}. Response keys: {radio.keys() if radio else 'None'}")
            return {"error":"Could not generate a radio playlist for this song."}, 404

    except Exception as e:
        print(f"Error fetching radio playlist for {id}: {str(e)}")
        # Check if it looks like a private/deleted video issue
        error_str = str(e).lower()
        if "private or does not exist" in error_str or "invalid video id" in error_str:
             return {"error":"Could not get radio playlist (song might be private or unavailable)."}, 404
        else:
             return {"error":"Internal Server Error fetching radio playlist","errorDetails":str(e)}, 500

@app.route("/search/<q>")
@app.route("/search/<q>/songs")
@cache.cached(timeout=300)
def search(q):
    print(f"Performing search for query: '{q}'")
    try:
        # Use a timeout for the search request as well
        # ytmusicapi doesn't have a built-in timeout for search, so this is a limitation
        # You might need to implement a separate thread/timeout mechanism if ytmusicapi.search itself hangs
        # For now, relying on potential underlying network timeouts from requests within ytmusicapi
        # and the Gunicorn worker timeout as a last resort.
        results = ytmusic.search(query=q, filter="songs", limit=32)

        # ytmusicapi search returns a list directly, no need to check for KeyError like get_song
        if results is not None and isinstance(results, list):
             print(f"Search for '{q}' returned {len(results)} results.")
             # Optional: Proxy thumbnails in search results
             for result in results:
                  if result.get("thumbnail", {}).get("thumbnails"):
                        thumbnails = result["thumbnail"]["thumbnails"]
                        if thumbnails:
                             best_thumbnail_url = thumbnails[-1].get("url")
                             if best_thumbnail_url:
                                  encoded_proxy_url = urllib.parse.quote_plus(best_thumbnail_url)
                                  result["thumbnail"]["thumbnails"] = [{"url": f"{request.url_root.rstrip('/')}/lh3Proxy/{encoded_proxy_url}"}]
                                  if 'thumbnail' in result and 'url' in result['thumbnail']:
                                       result['thumbnail']['url'] = f"{request.url_root.rstrip('/')}/lh3Proxy/{encoded_proxy_url}"

             return results
        else:
             print(f"Search for '{q}' returned unexpected data type: {type(results)}. Data: {results}")
             return {"error": "Search returned results in an unexpected format."}, 500

    except Exception as e:
        print(f"Error during search for '{q}': {str(e)}")
        # Check for common ytmusicapi errors during search if needed
        return {"error":"Internal Server Error during search","errorDetails":str(e)}, 500

# Add a simple health check endpoint
@app.route("/health")
def health_check():
    # Could add checks for ytmusicapi/yt-dlp responsiveness if needed
    return {"status": "ok", "message": "API is running"}

if __name__ == '__main__':
    # Consider using a production WSGI server like Gunicorn in production
    # For local testing:
    # Make sure debug is False in production
    # app.run(debug=True, port=5000)
    # To match railway behavior more closely for local testing, run with gunicorn:
    # gunicorn -w 4 -b 0.0.0.0:5000 app:app --timeout 60
    print("Warning: Running with Flask development server. Use a WSGI server like Gunicorn for production.")
    app.run(debug=True, host='0.0.0.0', port=os.environ.get('PORT', 5000)) # Use PORT env var if available