# Office Automation API

# TODO

- 🚧 **文件名包含"&"会导致功能异常**
- 🚧 **Word 文档加水印**
- 🚧 **PDF水印乱码**
- 🚧 **定义层与调用层的拆分**
- 🚧 **to_English**
- 🚧 **播放积分制**

## READY

- ✅ **图片加水印 (PNG/JPG)**
- ✅ **PDF 每页自动加水印 (居中)**

- ✅ **匿名即焚墙**
    - ✅ **基于 Redis 的消息存储**：240秒（4分钟）自动物理销毁。
    - ✅ **安全防护**：基于真实 IP 的频率限制（4分钟内限发一条）。
    - ✅ **特权管理**：管理员令牌支持（可无视限速与手动删帖）。



首次拉取：
```bash
git clone https://github.com/wongyiuming/my_FastAPI.git
cd my_FastAPI
cp .env.example .env
docker compose down && docker compose up -d --build
```

更新:
```bash
git pull https://github.com/wongyiuming/my_FastAPI.git
docker compose down && docker compose up -d --build
```


## ⚠️ Windows 部署注意事项（LTSC / Server 版本）

如果你在 **Windows LTSC** 或 **精简系统（如 Server Core）** 上运行该项目，请务必先安装：

👉 [Microsoft Visual C++ Redistributable (x64)](https://aka.ms/vs/17/release/vc_redist.x64.exe)

否则，`pymupdf`（即 `fitz`）模块会报错：

关于自我
我发现自己很容易被外部因素左右，最开始的动机是听歌，后面发现有GFW的存在听不了，当时电脑只会开机，开始自学计网方向，发现方向错了，转全栈，
接触到xray-core sing-box ng mysql redis ，然后python，pd requests  async到现在的FastAPI，
我应该没那么喜欢耀明的，但是共产党非要搞他，或许是从他身上看到自己的影子，亦或是为自己不平，
总之走到现在了，3年前的梦到今天超额实现了，不知道这3年怎么过来的，
99%的人烂尾了我带着仇恨撑到现在，感觉自己心理有问题，
我是不是报复心理特别强，我到底怎么了，为了听歌付出自己的全部吗，
我为什么会因为共针对耀明而爱上耀明呢，这是我的想法么，我好容易被左右，最近越来越心烦了