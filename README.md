# FrontierCloud

FrontierCloud 是一个以 Docker Compose 部署的 FastAPI 媒体管理服务，包含
Nginx、MySQL、Redis、监控组件及 YouTube/Bilibili 自动同步脚本。

## 首次部署

要求：Git、Docker Engine、Docker Compose v2。生产服务器建议使用 CentOS，
并保持 SELinux 开启。

```bash
git clone https://github.com/wongyiuming/FrontierCloud.git
cd FrontierCloud
cp .env.example .env
chmod 600 .env
```

编辑 `.env`，至少完成以下事项：

- 把所有 `REPLACE_WITH_...` 替换为独立强密码或随机密钥。
- 保证 `MYSQL_URL` 中的密码与 `MYSQL_PASSWORD` 一致并经过 URL 编码。
- 设置 `ENVIRONMENT=development`、`test` 或 `production`。
- 生产环境填写域名、TLS 证书路径，并保持安全 Cookie 配置开启。
- 测试或生产环境建议设置 `COMPOSE_PROFILES=monitoring`。

创建持久目录并启动：

```bash
sudo install -d -m 0755 data data/media backups certs
sudo chown -R 10001:10001 data
sudo chmod 0700 certs

sudo docker compose config --quiet
sudo docker compose pull
sudo docker compose up -d --build --wait --wait-timeout 240
sudo docker compose ps
```

开发环境默认使用 HTTP；生产环境应通过配置域名访问 HTTPS。部署后检查：

```bash
sudo docker compose exec -T nginx nginx -t
sudo docker compose exec -T redis redis-cli ping
sudo docker compose exec -T mysql \
  sh -c 'mysqladmin ping -h 127.0.0.1 -u"$MYSQL_USER" -p"$MYSQL_PASSWORD" --silent'
curl --fail https://你的域名/api/v1/health
```

## 更新部署

更新前先看远端改动以及 `.env.example` 是否增加了变量：

```bash
git fetch origin
git log --oneline HEAD..origin/main
git diff --stat HEAD..origin/main
git diff HEAD..origin/main -- .env.example
```

补齐本机私有 `.env` 后再更新：

```bash
git pull --ff-only
sudo docker compose config --quiet
sudo docker compose pull
sudo docker compose up -d --build --wait --wait-timeout 240
sudo docker compose ps
```

不要执行 `docker compose down --volumes`，MySQL 和 Redis 的持久数据位于卷中。

## 查看管理员 Token

服务会定期签发临时管理员 Token。直接查看 Web 容器日志：

```bash
sudo docker compose logs -f web | grep ADMIN_TOKEN
```

需要立即签发时，使用 `.env` 中的 `ADMIN_BOOTSTRAP_TOKEN`，避免把密钥写入
Shell 历史：

```bash
read -r -s -p 'Bootstrap token: ' bootstrap_token
printf '\n'
curl --fail --request POST \
  --header "X-Token: ${bootstrap_token}" \
  https://你的域名/api/v1/media/admin/token/issue
unset bootstrap_token
```

Token 有有效期；过期后重新查看日志或再次签发。

## 自动同步媒体

本地或 PyCharm 使用项目 `.venv`：

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e .        # Windows
# .venv/bin/python -m pip install -e .          # CentOS
```

先检查依赖或只预览远端差异：

```bash
.venv/Scripts/python auto_download/yt_mp3.py --check
.venv/Scripts/python auto_download/yt_mp3.py --dry-run
.venv/Scripts/python auto_download/bilibili_mp3.py --dry-run
```

去掉 `--dry-run` 执行真实同步。YouTube 会读取 `@wyium` 的全部公开播放列表；
Bilibili 每次轮询配置的五个收藏夹，并以远端收藏夹名称创建 `data/media`
子目录。同步会下载缺失项、跳过已有项，并在同组来源清单齐全后删除远端已无的
本地媒体。运行前应备份 `data/media`。

成功日志只显示关键阶段、可用/最终选择质量、下载速度和汇总；失败会输出完整
traceback 与 yt-dlp debug 诊断。
