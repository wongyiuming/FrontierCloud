import os
import yt_dlp

channel_url = 'https://www.youtube.com/@%E7%BE%BD%E6%B1%9F-f4k/playlists'
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MEDIA_DIR = os.path.join(BASE_DIR, "data", "media")
FFMPEG_PATH = r"C:\Program Files\ffmpeg-2024-11-28-git-bc991ca048-full_build\bin"
NODE_EXECUTABLE_PATH = r"C:\Program Files\nodejs\node.exe"

os.makedirs(MEDIA_DIR, exist_ok=True)


def sanitize_filename(name):
    """过滤文件名或文件夹名称中的非法字符（保留路径分隔符）"""
    return "".join(c for c in name if c not in r':*?"<>|')


def main():
    ydl_opts = {
        'format': 'bestaudio/best',
        'ffmpeg_location': FFMPEG_PATH,
        'outtmpl': os.path.join(MEDIA_DIR, '%(playlist_title)s', '%(title)s.%(ext)s'),
        'download_archive': os.path.join(MEDIA_DIR, 'archive.txt'),
        'noplaylist': False,
        'no_plugins': True,
        'remote_components': ['ejs:github'],
        'javascript_executable': NODE_EXECUTABLE_PATH,
        'rm_cachedir': True,
        'http_headers': {
            'User-Agent': (
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                ' (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36'
            ),
            'Accept-Language': 'en-US,en;q=0.9',
        },
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '191',
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