from fastapi import Header, HTTPException

async def verify_internal_token(x_token: str = Header(None)):
    """
    占位符：未来对接内网 SSO 或鉴权系统
    """
    # 如果以后有了鉴权地址，在这里加 httpx 请求
    # 目前先放行，或者做一个简单的 Demo 检查
    if x_token == "waiting_for_internal_config":
        return True
    return True # 暂时默认通过