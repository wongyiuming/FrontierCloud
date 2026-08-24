import os
import yt_dlp

# Point to the /playlists tab, or use a channel page with matching options.
channel_url = 'https://www.youtube.com/@%E7%BE%BD%E6%B1%9F-f4k/playlists'
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MEDIA_DIR = os.path.join(BASE_DIR, "data", "media")
folder_name = MEDIA_DIR
FFMPEG_PATH = r"C:\ffmpeg\bin"
NODE_EXECUTABLE_PATH = r"C:\Program Files\nodejs\node.exe"

folder_name = "".join(c for c in folder_name if c not in r'/:*?"<>|')
os.makedirs(folder_name, exist_ok=True)


def main():
    ydl_opts = {
        'format': 'bestvideo+bestaudio/best',
        'merge_output_format': 'mp4',
        'ffmpeg_location': FFMPEG_PATH,
        'outtmpl': os.path.join(folder_name, '%(playlist_title)s/%(title)s.%(ext)s'),  # Group files by playlist.
        'noplaylist': False,  # Download the complete playlist.

        # Disable external plugins so the job never launches Chrome.
        'no_plugins': True,

        # Load the current decryption component through the supported list syntax.
        'remote_components': ['ejs:github'],

        # Use the local Node.js runtime for background decryption.
        'javascript_executable': NODE_EXECUTABLE_PATH,

        # Clear the yt-dlp cache before resolving network resources.
        'rm_cachedir': True,

        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
            'Accept-Language': 'en-US,en;q=0.9',
        },

        # Let yt-dlp select and fall back across its default player clients.
        'postprocessors': [{
            'key': 'FFmpegVideoConvertor',
            'preferedformat': 'mp4',
        }],
        'ignoreerrors': True,
        'quiet': False,
        'no_warnings': False,
        'retries': 3,
        'fragment_retries': 3,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([channel_url])


if __name__ == "__main__":
    main()
