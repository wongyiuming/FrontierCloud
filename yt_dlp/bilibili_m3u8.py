import os
import yt_dlp

channel_url = 'https://space.bilibili.com/50687441/favlist?fid=4077706241&ftype=create'

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MEDIA_DIR = os.path.join(BASE_DIR, "data", "media")
base_folder = MEDIA_DIR


FFMPEG_PATH = r'C:\ffmpeg\bin'
RAW_REQUEST_DATA = {
    'headers': {
        'accept': 'text/css,*/*;q=0.1',
        'accept-language': 'zh-CN,zh;q=0.7',
        'cache-control': 'no-cache',
        'pragma': 'no-cache',
        'priority': 'u=0, i',
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
    },
    'referrer': (
        'https://www.youtube.com/watch?v=G5RpJwCJDqc&list=PLZDOE0t8W-ShvEpkH5ZkZPjtpIvkvYObk'
    ),
    'body': 'Null',
    'method': 'GET',
    'mode': 'cors',
    'credentials': 'include',
}

folder_name = ''.join(c for c in base_folder if c not in r'/:*?"<>|')
os.makedirs(base_folder, exist_ok=True)


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
    pl_title_clean = ''.join(c for c in pl_title if c not in r'/:*?"<>|').strip()
    if not pl_title_clean:
      pl_title_clean = 'Default_Collection'

    playlist_folder = os.path.join(base_folder, pl_title_clean)
    os.makedirs(playlist_folder, exist_ok=True)

    pl_url = pl.get('url') or pl.get('webpage_url') or channel_url

    print(f'\n========================================')
    print(f'开始下载合集/收藏夹: {pl_title_clean}')
    print(f'========================================')

    ydl_opts = {
        'format': 'bestvideo+bestaudio/best',
        'merge_output_format': 'mp4',

        'http_headers': RAW_REQUEST_DATA['headers'],
        # 'referer': RAW_REQUEST_DATA.get('referer'),
        'ffmpeg_location': FFMPEG_PATH,
        'outtmpl': os.path.join(folder_name, '%(playlist_title)s/%(title)s.%(ext)s'),

        # ⬇️ 核心修正点：移除错误的 codec 键和 Postprocessor 中的不兼容键
        'postprocessors': [{
            # 'FFmpegVideoConvertor' 只需要做简单的容器转换，如果需要强制编码，
            # 它应该使用 'FFmpegVideoConvertor' 键，但不需要 codec 参数
            'key': 'FFmpegVideoConvertor',
            'preferedformat': 'mp4',
            # 'preferredcodec' 是一个不合法的顶层参数，必须删除
        }],
        'ignoreerrors': True,
        'quiet': False,
        'external_downloader': 'ffmpeg',
        'external_downloader_args': ['-threads', '4'],  # 示例：使用 4 个线程

        'retries': 10,
        'fragment_retries': 10,
        'buffer_size': '1024K',
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
      ydl.download([pl_url])


if __name__ == '__main__':
  main()