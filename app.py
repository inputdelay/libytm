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

if os.path.exists('.env'):
    load_dotenv()

os.makedirs("cache", exist_ok=True)
os.makedirs("cache/segments", exist_ok=True)

ytdlp = yt_dlp.YoutubeDL()
ytmusic = YTMusic()

config = {
    "CACHE_TYPE": "SimpleCache",
    "CACHE_DEFAULT_TIMEOUT": 120
}
app = Flask(__name__)
app.config.from_mapping(config)
CORS(app, resources={r"/*": {"origins": ["http://localhost:8000", "https://pulsing.netlify.app/"]}})  # Allow specific origins
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
    try:
        response = requests.get(original_url, stream=True)
        response.raise_for_status()
        with open(temp_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
        with segment_cache_lock:
            if segment_filename in segment_cache:
                segment_cache[segment_filename]['status'] = 'downloaded'
                segment_cache[segment_filename]['timestamp'] = time.time()
    except requests.exceptions.RequestException as e:
        print(f"error downloading segment {segment_filename} from {original_url}: {str(e)}")
        with segment_cache_lock:
            if segment_filename in segment_cache:
                segment_cache[segment_filename]['status'] = 'failed'
                segment_cache[segment_filename]['timestamp'] = time.time()

def start_segment_downloads(segments_info):
    for segment_filename, original_url, temp_path in segments_info:
        segment_download_executor.submit(download_segment_task, segment_filename, original_url, temp_path)

def purge_old_segments():
    while True:
        current_time = time.time()
        to_purge = []
        with segment_cache_lock:
            for segment_filename, info in list(segment_cache.items()):
                if current_time - info.get('timestamp', 0) > SEGMENT_LIFETIME:
                    to_purge.append((segment_filename, info['temp_path']))
                    del segment_cache[segment_filename]

        for segment_filename, temp_path in to_purge:
            try:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
                    print(f"purged old segment file: {temp_path}")
            except OSError as e:
                print(f"error purging segment file {temp_path}: {str(e)}")

        time.sleep(SEGMENT_PURGE_INTERVAL)
purging_thread = threading.Thread(target=purge_old_segments, daemon=True)
purging_thread.start()


def get_audio(video_url, id):
    cmd = [
        sys.executable, "-m", "yt_dlp",
        video_url.replace("\"", "\0").replace(" ", ""),
        "-x", "--no-video", "-o", f"{os.getcwd()}/cache/{id}"
    ]
    cookies_path = os.environ.get('COOKIES')
    if cookies_path:
        cmd.extend(['--cookies', cookies_path])

    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    if result.returncode != 0:
        raise Exception(f"yt-dlp failed: {result.stderr}")

    return f"{os.getcwd()}/cache/{id}.mp3"

@app.route("/")
def hi():
    return {"hello":"this is a libytm instance"}

@app.route("/lh3Proxy/<path:url>")
def lh3(url:str):
    if not url.startswith("https://googleusercontent.com") and not url.startswith("https://ytimg.com") and not url.startswith("https://googlevideo.com"):
        return {"error":"no"},422
    print(urllib.parse.unquote(url))
    headers = {
       "Accept":'*/*',"User-Agent":"curl/8.13.0"
    }
    res = requests.get(urllib.parse.unquote(url), headers=headers)
    return Response(res.content,res.status_code,{"Content-Type":res.headers["Content-Type"]})

@cache.cached(timeout=300)
@app.route("/song/<id>")
def getSong(id):
    tries = 0
    while tries < 5:
        song = ytmusic.get_song(videoId=id,signatureTimestamp=ytmusic.get_signatureTimestamp())
        if song is not None:
            break
        tries += 1
    try:
        return song["videoDetails"]
    except KeyError:
        return {"error":"could not find song (if the song exists then this is a youtube bug; ask the hoster to provide cookies)"}, 404
    except Exception as e:
        return {"error":"Internal Server Error","errorDetails":e}, 500

@cache.cached(timeout=300)
@app.route("/playlist/<id>")
def getPlaylist(id):
    pl = ytmusic.get_playlist(playlistId=id)
    try:
        return pl
    except KeyError:
        return {"error":"could not find playlist"}, 404
    except Exception as e:
        return {"error":"Internal Server Error","errorDetails":e}, 500

@app.route("/song/<id>/streamHLS.m3u8")
def getstream_experimental(id):
    cmd = [
        sys.executable, "-m", "yt_dlp",
        f"https://youtube.com/watch?v={id}".replace("\"", "\0").replace(" ", ""),
        "-f", "bestaudio", "-g"
    ]
    headers = {
       "Accept":'*/*',"User-Agent":"curl/8.13.0"
    }

    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    print(result.stderr)

    if result.stdout == "":
        return "Not Found", 404

    m3u8_url = result.stdout.strip()
    try:
        m3u8_response = requests.get(m3u8_url, headers=headers)
        m3u8_response.raise_for_status()
        m3u8_content = m3u8_response.text
    except requests.exceptions.RequestException as e:
        return {"error": f"Failed to download m3u8 playlist: {str(e)}"}, 500

    new_m3u8_lines = []
    segments_to_download = []
    base_url = m3u8_url.rsplit('/', 1)[0] + '/'

    for line in m3u8_content.splitlines():
        line = line.strip()
        if line.startswith("#"):
            new_m3u8_lines.append(line)
        elif line.endswith(".ts"):
            original_ts_url = line if line.startswith("http") else base_url + line
            segment_filename = f"{uuid.uuid4().hex}.ts"
            temp_path = os.path.join(TEMP_SEGMENT_DIR, segment_filename)

            with segment_cache_lock:
                segment_cache[segment_filename] = {
                    'original_url': original_ts_url,
                    'temp_path': temp_path,
                    'status': 'pending',
                    'timestamp': time.time() # Timestamp when added to cache
                }

            new_m3u8_lines.append(f"{request.url_root}song/{id}/segment/{segment_filename}")
            segments_to_download.append((segment_filename, original_ts_url, temp_path))
    start_segment_downloads(segments_to_download)

    return Response("\n".join(new_m3u8_lines), 200, {"Content-Type": "application/x-mpegURL"}) # Standard MIME type for M3U8

@app.route("/song/<id>/segment/<segment_filename>")
def serve_segment(id, segment_filename):
    with segment_cache_lock:
        segment_info = segment_cache.get(segment_filename)

    if segment_info and segment_info['status'] == 'downloaded':
        try:
            with segment_cache_lock:
                 segment_cache[segment_filename]['timestamp'] = time.time()
            return send_file(segment_info['temp_path'], mimetype="video/mp2t")
        except FileNotFoundError:
            print(f"error: segment file not found despite status 'downloaded': {segment_info['temp_path']}")
            return "Segment Not Found", 404
    elif segment_info and segment_info['status'] == 'pending':
        wait_start_time = time.time()
        wait_timeout = 60 # seconds

        while segment_info['status'] == 'pending' and (time.time() - wait_start_time) < wait_timeout:
            time.sleep(0.5)
            with segment_cache_lock:
                segment_info = segment_cache.get(segment_filename)
                if not segment_info:
                    return "Segment Not Found", 404

        if segment_info['status'] == 'downloaded':
            try:
                with segment_cache_lock:
                     segment_cache[segment_filename]['timestamp'] = time.time()
                return send_file(segment_info['temp_path'], mimetype="video/mp2t")
            except FileNotFoundError:
                print(f"error: segment file not found after download: {segment_info['temp_path']}")
                return "Segment Not Found After Download", 404
        elif segment_info['status'] == 'failed':
             return "Segment Download Failed", 500
        else: # Timeout
            return "Segment Download Timeout", 504
    elif segment_info and segment_info['status'] == 'failed':
         return "Segment Download Failed", 500
    else:
        return "Segment Not Found", 404


@cache.cached(timeout=14400)
@app.route("/song/<id>/stream")
def getAudio(id):
    try:
        cache_path = os.path.join("cache", f"{id}.opus")
        cache_path2 = os.path.join("cache", f"{id}.m4a")
        if os.path.exists(os.path.join("cache", f"{id}")):
            os.remove(os.path.join("cache", f"{id}"))
        if os.path.exists(cache_path):
            return send_file(cache_path, mimetype="audio/mpeg")

        if os.path.exists(cache_path2):
            return send_file(cache_path2, mimetype="audio/mpeg")

        audio = get_audio(f"https://youtube.com/watch?v={id}",id=id)
        if os.path.exists(cache_path):
            return send_file(cache_path, mimetype="audio/mpeg")

        if os.path.exists(cache_path2):
            return send_file(cache_path2, mimetype="audio/mpeg")

    except Exception as e:
        return {"error": f"could not get audio: {str(e)}"},500

@cache.cached(timeout=300)
@app.route("/song/<id>/lyrics")
def getLyrics(id):
    song = ytmusic.get_song(videoId=id,signatureTimestamp=ytmusic.get_signatureTimestamp())
    songDetails = None
    try:
        songDetails = song["videoDetails"]
    except KeyError:
        return {"error":"could not find song"}, 404
    except Exception as e:
        return {"error":"Internal Server Error","errorDetails":e}, 500
    lyrics=requests.get(f"https://lrclib.net/api/get?artist_name={songDetails["author"]}&track_name={songDetails["title"]}")
    return lyrics.json()

@cache.cached(timeout=300)
@app.route("/song/<id>/ytmLyrics")
def getYTMLyrics(id):
    song = ytmusic.get_song(videoId=id,signatureTimestamp=ytmusic.get_signatureTimestamp())
    try:
        lid = ytmusic.get_watch_playlist(videoId=id,radio=True,limit=50)["lyrics"]
        lyrics = ytmusic.get_lyrics(browseId=lid,timestamps=True)
        return lyrics
    except KeyError:
        return {"error":"could not find song"}, 404
    except Exception as e:
        return {"error":"Internal Server Error","errorDetails":e}, 500

@cache.cached(timeout=300)
@app.route("/song/<id>/radio")
def getRadio(id):
    song = ytmusic.get_song(videoId=id,signatureTimestamp=ytmusic.get_signatureTimestamp())
    try:
        radio = ytmusic.get_watch_playlist(videoId=id,radio=True,limit=50)
        return radio
    except KeyError:
        return {"error":"could not find song"}, 404
    except Exception as e:
        return {"error":"Internal Server Error","errorDetails":e}, 500

@app.route("/search/<q>")
@app.route("/search/<q>/songs")
@cache.cached(timeout=300)
def search(q):
    return ytmusic.search(query=q,filter="songs",limit=32)
