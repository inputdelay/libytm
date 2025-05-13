import toga
from toga.style import Pack
from toga.style.pack import COLUMN, ROW
import threading

class LibYTMServer(toga.App):
    def startup(self):
        main_box = toga.Box(style=Pack(background_color="#1e1e2e",direction=COLUMN))
        text = toga.Label("LibYTM server is running",style=Pack(color="#cdd6f4",padding=16,padding_bottom=3, font_size=22, font_weight="bold",direction=COLUMN))
        main_box.add(text)
        text2 = toga.Label("""
In order to use the LibYTM server, open the Mujay app
and go into settings menu (gear icon), then change the
LibYTM server URL to http://localhost:5000 . 

If you are experiencing issues, revert it back to 
https://libytm.mujay.app .
                           
""",style=Pack(color="#cdd6f4",padding=16,padding_top=3, font_size=14,direction=COLUMN, flex=1))
        main_box.add(text2)

        restart_button = toga.Button(
            "Restart Server", 
            on_press=self.restart_app,
            style=Pack(padding=16, background_color="#cba6f7", color="#1e1e2e")
        )
        main_box.add(restart_button)


        self.main_window = toga.MainWindow(title=self.formal_name)
        self.main_window.content = main_box
        self.main_window.show()
        print("Main window shown.")
        
        # Start the service
        self.start_service()
        
    def restart_app(self, widget):
        print("Killing and relaunching app...")
        
        try:
            from com.chaquo.python import Python
            platform = Python.getPlatform()
            context = platform.getApplication()
            from java import jclass
            Intent = jclass('android.content.Intent')
            ComponentName = jclass('android.content.ComponentName')
            Runtime = jclass('java.lang.Runtime')
            packageManager = context.getPackageManager()
            packageName = context.getPackageName()
            intent = packageManager.getLaunchIntentForPackage(packageName)
            componentName = intent.getComponent()
            mainIntent = Intent.makeRestartActivityTask(componentName)
            mainIntent.setPackage(packageName)
            context.startActivity(mainIntent)
            Runtime.getRuntime().exit(0)
            
        except Exception as e:
            print(f"Error during app restart: {e}")
            import traceback
            print(traceback.format_exc())
    
    def start_service(self):
        try:
            from com.chaquo.python import Python
            platform = Python.getPlatform()
            context = platform.getApplication()
            if context is None:
                 raise Exception("Chaquopy platform.getApplication() returned None")
            print(f"Got application context: {type(context)}")
            service_class_name = 'org.beeware.android.LibYTMForegroundService'
            
            print(f"Attempting to load service class: {service_class_name}")
            
            from java import jclass
            service_class = jclass(service_class_name)
            intent = jclass('android.content.Intent')(context, service_class)
            print("Intent created.")
            
            if jclass('android.os.Build$VERSION').SDK_INT >= jclass('android.os.Build$VERSION_CODES').O:
                 context.startForegroundService(intent)
                 print("Attempting to start foreground service...")
            else:
                 context.startService(intent)
                 print("Attempting to start background service...")
        except Exception as e:
             print(f"Failed to start Android Service: {e}")
             raise e # cause app crash


def run_server_static():
    from flask import Flask, request, Response, send_file
    from flask_caching import Cache
    from flask_cors import CORS
    from ytmusicapi import YTMusic
    import requests
    import os
    import yt_dlp
    from dotenv import load_dotenv
    import signal
    import threading
    from werkzeug.serving import make_server
    server = None
    
    class ServerThread(threading.Thread):
        def __init__(self, app):
            threading.Thread.__init__(self, daemon=True)
            self.server = make_server('0.0.0.0', 5000, app)
            self.ctx = app.app_context()
            self.ctx.push()

        def run(self):
            print("Starting Flask server thread")
            self.server.serve_forever()

        def shutdown(self):
            print("Shutting down Flask server thread")
            self.server.shutdown()

    from dotenv import load_dotenv
    if os.path.exists('.env'):
        load_dotenv()
    try:
        os.makedirs("/data/data/app.mujay.libytm.libytm/files/cacheytm", exist_ok=True)
    except Exception as e:
        print(f"Error creating cache directory: {e}")
    ytmusic = YTMusic()
    config = {
        "CACHE_TYPE": "SimpleCache",
        "CACHE_DEFAULT_TIMEOUT": 120
    }
    app = Flask(__name__)
    app.config.from_mapping(config)
    CORS(app)
    cache = Cache(app)
    
    def get_audio(video_url, id):
        print(f"Proxying audio for {id} from libytm.mujay.app to bypass yt-dlp issues")
        response = requests.get(f"https://libytm.mujay.app/song/{id}/stream", stream=True)
        response.raise_for_status()
        cache_path = f"/data/data/app.mujay.libytm.libytm/files/cacheytm/{id}.mp3"
        with open(cache_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)   
        return cache_path
    @app.route("/")
    def hi():
        print("hi")
        return {"hello":"this is a libytm instance"}
    @app.route("/lh3Proxy/<path:url>")
    def lh3(url):
        print(f"lh3Proxy {url}")
        if "googleusercontent.com" not in url and "ytimg.com" not in url and "googlevideo.com" not in url:
            return {"error":"no"},422
        res = requests.get(url)
        return Response(res.content,200,{"Content-Type":res.headers["Content-Type"]})
    @cache.cached(timeout=300)
    @app.route("/song/<id>")
    def getSong(id):
        print(f"getSong {id}")
        tries = 0
        while tries < 5:
            song = ytmusic.get_song(videoId=id)
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
        print(f"getPlaylist {id}")
        pl = ytmusic.get_playlist(playlistId=id)
        try:
            return pl
        except KeyError:
            return {"error":"could not find playlist"}, 404
        except Exception as e:
            return {"error":"Internal Server Error","errorDetails":e}, 500
    @cache.cached(timeout=14400)
    @app.route("/song/<id>/streamHLS.m3u8")
    def getstream_experimental(id):
        return Response(f"https://libytm.mujay.app/song/{id}/streamHLS.m3u8",200,{"Content-Type":"text/plain"})
    @cache.cached(timeout=14400)
    @app.route("/song/<id>/stream")
    def getAudio(id):
        print(f"getAudio {id}")
        try:
            audio = get_audio(f"https://youtube.com/watch?v={id}",id=id)
            return send_file(audio, mimetype="audio/mpeg")
        except Exception as e:
            return {"error": f"could not get audio: {str(e)}"},500
    @cache.cached(timeout=300)
    @app.route("/song/<id>/lyrics")
    def getLyrics(id):
        print(f"getLyrics {id}")
        song = ytmusic.get_song(videoId=id)
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
        print(f"getYTMLyrics {id}")
        song = ytmusic.get_song(videoId=id)
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
        print(f"getRadio {id}")
        song = ytmusic.get_song(videoId=id)
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
        print(f"search {q}")
        return ytmusic.search(query=q,filter="songs",limit=32)
    try:
        # Use threaded server instead of app.run directly
        print("Starting server in thread mode")
        server = ServerThread(app)
        server.start()
        # Keep the main thread alive
        server.join()
    except Exception as e:
        print(f"Error running server: {e}")
        raise e # cause app crash



def main():
    return LibYTMServer()
