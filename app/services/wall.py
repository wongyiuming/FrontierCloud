import json
import uuid
from datetime import datetime
from fastapi import Request
from app.core.redis import redis_client
from app.core.config import settings


class WallService:
    def __init__(self):
        self.PREFIX = "wall:post:"
        self.RATE_PREFIX = "wall:rate:"

    async def can_perform_action(self, request: Request, is_admin: bool) -> bool:
        """
        检查速率限制 (4分钟/次)
        """
        if is_admin: return True

        # 优先获取 Nginx 透传的真实 IP
        ip = request.headers.get("X-Real-IP") or request.client.host
        key = f"{self.RATE_PREFIX}{ip}"

        # SET NX EX: 只有 Key 不存在时才设置，并在 TTL 后自动过期
        # 返回 True 表示设置成功（未被限速），False 表示 key 已存在（限速中）
        return await redis_client.set(key, "1", ex=settings.WALL_TTL, nx=True)

    async def add_post(self, content: str, ip: str = "Unknown"):
        post_id = str(uuid.uuid4())
        data = {
            "id": post_id,
            "content": content,
            "author_ip": ip[:7] + "****",  # 简单脱敏
            "created_at": datetime.now().isoformat(),
            "comments": []
        }
        # 存入 Redis，TTL 结束自动物理删除
        await redis_client.set(f"{self.PREFIX}{post_id}", json.dumps(data), ex=settings.WALL_TTL)
        return data

    async def add_comment(self, post_id: str, content: str, ip: str):
        key = f"{self.PREFIX}{post_id}"
        post_json = await redis_client.get(key)

        # 如果帖子已过期（返回 None），则无法评论
        if not post_json:
            return None

        # 获取剩余存活时间，确保评论不会延长帖子寿命
        ttl = await redis_client.ttl(key)
        if ttl <= 0: return None

        data = json.loads(post_json)
        data["comments"].append({
            "id": str(uuid.uuid4()),
            "content": content,
            "ip": ip[:7] + "****",
            "created_at": datetime.now().isoformat()
        })

        # 写回数据，保持原有 TTL
        await redis_client.set(key, json.dumps(data), ex=ttl)
        return data

    async def get_all(self):
        # SCAN 扫描所有帖子 (生产环境数据量巨大时需改用分页)
        cursor, keys = await redis_client.scan(match=f"{self.PREFIX}*")
        if not keys: return []

        items = await redis_client.mget(keys)
        # 过滤失效数据并按时间倒序
        posts = [json.loads(i) for i in items if i]
        return sorted(posts, key=lambda x: x['created_at'], reverse=True)

    async def delete_post(self, post_id: str):
        await redis_client.delete(f"{self.PREFIX}{post_id}")


wall_service = WallService()