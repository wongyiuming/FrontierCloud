"""Fresh-nonce authority for one exact bound retirement operation and path."""
import hmac
import re
import time

from cryptography.fernet import InvalidToken
from sqlalchemy import delete,func,insert,select

from app.services import node_retirement_schema as s
from app.services.federation import protocol as p,schema as fs
from app.services.node_retirement_state import _columns,_decode,binding
from app.services.node_retirement_protocol import PATH
from app.services.node_retirement_protocol import request as parse_request

HEADERS=('x-node-relationship','x-node-time','x-node-nonce','x-node-signature')


class AuthenticationError(p.ProtocolError):
    def __init__(self):super().__init__('retirement_authentication_invalid')


def header_shape(headers):
    try:
        items=list(headers.multi_items()) if hasattr(headers,'multi_items') else list(headers.items())
        for key in HEADERS:
            if sum(name.lower()==key for name,_ in items)!=1:raise ValueError()
        if (not p.IDENTIFIER.fullmatch(headers['x-node-relationship']) or not p.IDENTIFIER.fullmatch(headers['x-node-nonce']) or
                re.fullmatch(r'[0-9]{1,20}',headers['x-node-time']) is None or
                re.fullmatch(r'[a-f0-9]{64}',headers['x-node-signature']) is None):raise ValueError()
    except (ValueError,KeyError,TypeError,AttributeError):raise AuthenticationError() from None


class Authentication:
    def __init__(self,journal,*,wall=time.time):self.journal,self.store,self.wall=journal,journal.store,wall

    async def check(self,headers,method,path,raw,value):
        header_shape(headers)
        if method!='POST' or path!=PATH:raise AuthenticationError()
        try:
            if p.canonical(parse_request(raw))!=p.canonical(value):raise AuthenticationError()
            async with self.store.database.begin() as conn:
                node,relation=await self.journal._context(conn,headers['x-node-relationship'])
                if (node['role']!='Follower' or relation['direction']!='upstream' or relation['protocol']!=p.PROTOCOL_VERSION or
                        relation['state'] not in {'active','retiring','revoked'}):raise AuthenticationError()
                now=int(self.wall());credential=self.store.unseal(relation['credential'])
                if len(p.decode(credential))!=48:raise AuthenticationError()
                nonce=p.verify_auth(credential,headers,method,path,raw,now)
                expected,authorization=binding(node,relation,self.store,value['binding'].get('retirement_id',''))
                if p.canonical(value['binding'])!=p.canonical(expected):raise AuthenticationError()
                row=(await conn.execute(select(*_columns(s.retirements,limits={'binding_json':8192,'roots_json':8192,'receipt_json':16384})).where(
                    s.retirements.c.relationship_id==relation['relationship_id']).with_for_update())).mappings().first()
                if row is None:
                    if relation['state']!='active':raise AuthenticationError()
                else:
                    stored=_decode(row)
                    if (value['action']=='check' or stored['side']!='peer' or stored['node_id']!=node['node_id'] or p.canonical(stored['binding'])!=p.canonical(expected) or
                            not hmac.compare_digest(stored['authorization_sha256'],authorization) or
                            relation['state']!=('retiring' if stored['phase']=='retiring' else 'revoked') or
                            stored['phase']=='completed' and value['action'] not in {'status','complete'}):raise AuthenticationError()
                expired=list((await conn.execute(select(func.substr(fs.requests.c.nonce,1,33)).where(fs.requests.c.relationship_id==relation['relationship_id'],
                    fs.requests.c.expires_at<=now).order_by(fs.requests.c.expires_at,fs.requests.c.nonce).limit(32))).scalars())
                if any(not isinstance(item,str) or not p.IDENTIFIER.fullmatch(item) for item in expired):raise AuthenticationError()
                if expired:await conn.execute(delete(fs.requests).where(fs.requests.c.relationship_id==relation['relationship_id'],fs.requests.c.nonce.in_(expired)))
                if await conn.scalar(select(fs.requests.c.nonce).where(fs.requests.c.relationship_id==relation['relationship_id'],fs.requests.c.nonce==nonce)) is not None:
                    raise AuthenticationError()
                await conn.execute(insert(fs.requests).values(relationship_id=relation['relationship_id'],nonce=nonce,expires_at=now+2*p.AUTH_SKEW_SECONDS+1))
            return relation
        except AuthenticationError:raise
        except (p.ProtocolError,ValueError,TypeError,KeyError,RecursionError,InvalidToken):raise AuthenticationError() from None
