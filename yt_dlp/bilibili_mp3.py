import os
import yt_dlp

channel_url = 'https://space.bilibili.com/50687441/favlist?fid=4086690941&ftype=create'

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MEDIA_DIR = os.path.join(BASE_DIR, "data", "media")
base_folder = MEDIA_DIR

FFMPEG_PATH = r"C:\Program Files\ffmpeg-2024-11-28-git-bc991ca048-full_build\bin"
RAW_REQUEST_DATA = {
    'headers': {
        'accept': 'text/css,*/*;q=0.1',
        'accept-language': 'zh-CN,zh;q=0.7',
        'cache-control': 'no-cache',
        'pragma': 'no-cache',
        'priority': 'u=0, i',
        'referer': 'https://www.bilibili.com/',
        'sec-ch-ua': (
            '"Chromium";v="146", "Not-A.Brand";v="24", "Brave";v="146"'
        ),
        'sec-ch-ua-arch': '"x86"',
        'sec-ch-ua-bitness': '"64"',
        'sec-ch-ua-full-version-list': (
            '"Chromium";v="146.0.0.0", "Not-A.Brand";v="24.0.0.0",'
            ' "Brave";v="146.0.0.0"'
        ),
        'sec-ch-ua-mobile': '?0',
        'sec-ch-ua-model': '""',
        'sec-ch-ua-platform': '"Windows"',
        'sec-ch-ua-platform-version': '"19.0.0"',
        'sec-ch-ua-wow64': '?0',
        'sec-fetch-dest': 'empty',
        'sec-fetch-mode': 'no-cors',
        'sec-fetch-site': 'same-origin',
        'sec-gpc': '1',
    }
}

os.makedirs(base_folder, exist_ok=True)


def clean_filename(name):
    """过滤文件名或文件夹名中的非法字符"""
    return "".join(c for c in name if c not in r'/:*?"<>|').strip()


def main():
    extract_opts = {
        'extract_flat': 'in_playlist',
        'noplaylist': False,
        'http_headers': RAW_REQUEST_DATA['headers'],
    }

    print('正在获取B站合集/列表信息...')
    with yt_dlp.YoutubeDL(extract_opts) as ydl:
        info = ydl.extract_info(channel_url, download=False)

    if not info:
        print('未能获取到页面信息。')
        return

    playlists = []
    if 'entries' in info:
        # 判断是否为多P列表或合集
        for entry in info['entries']:
            if entry.get('_type') == 'playlist' or entry.get('extractor_key') in [
                'Bilibili',
                'BilibiliPlaylist',
                'BilibiliSpace',
            ]:
                playlists.append(entry)

    # 如果本身就是合集或多P页面（info是playlist类型）
    if not playlists and info.get('_type') == 'playlist':
        playlists = [info]

    if not playlists:
        # 单个视频降级处理
        playlists = [{
            'title': info.get('title') or 'Default_Collection',
            'url': channel_url,
        }]

    for pl in playlists:
        pl_title = pl.get('title') or 'Default_Collection'
        pl_title_clean = clean_filename(pl_title)
        if not pl_title_clean:
            pl_title_clean = 'Default_Collection'

        playlist_folder = os.path.join(base_folder, pl_title_clean)
        os.makedirs(playlist_folder, exist_ok=True)

        pl_url = pl.get('url') or pl.get('webpage_url') or channel_url

        print(f'\n========================================')
        print(f'开始增量下载合集/收藏夹: {pl_title_clean}')
        print(f'========================================')

        archive_file = os.path.join(playlist_folder, 'archive.txt')

        ydl_opts = {
            'format': 'bestaudio/best',
            'http_headers': RAW_REQUEST_DATA['headers'],
            'ffmpeg_location': FFMPEG_PATH,
            'outtmpl': os.path.join(playlist_folder, '%(title)s.%(ext)s'),
            'download_archive': archive_file,  # 开启增量去重记录文件
            'noplaylist': False,
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '0',
            }],
            'ignoreerrors': True,
            'quiet': False,
            'external_downloader': 'ffmpeg',
            'external_downloader_args': ['-threads', '4'],
            'retries': 10,
            'fragment_retries': 10,
            'buffer_size': '1024K',
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([pl_url])


if __name__ == '__main__':
    main()