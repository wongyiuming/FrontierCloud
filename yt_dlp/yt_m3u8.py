import os
import yt_dlp

# 注意：这里需要指向 /playlists 标签页，或者直接用主页配合参数
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
        'outtmpl': os.path.join(folder_name, '%(playlist_title)s/%(title)s.%(ext)s'),  # 按照播放列表分类建文件夹保存
        'noplaylist': False,  # 必须为 False，允许下载整个播放列表

        # 1. 彻底禁用所有 pip 安装的外部插件，确保 100% 不会拉起任何 Chrome 实例
        'no_plugins': True,

        # 2. 修正语法：通过列表传入正确的远程组件名称，自动同步最新解密核心
        'remote_components': ['ejs:github'],

        # 3. 指定本地 Node.js 路径配合进行后台静默解密
        'javascript_executable': NODE_EXECUTABLE_PATH,

        # 4. 路由网络请求至你的本地浏览器代理通道
        'rm_cachedir': True,

        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
            'Accept-Language': 'en-US,en;q=0.9',
        },

        # 5. 移除硬编码的 player_client，允许其依靠默认的机制进行内部多客户端流轮询降级
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