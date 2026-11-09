"""Private Janus REST transport. Only this server may send SIP commands."""
import secrets
from urllib.parse import urlsplit
import httpx
from app.core.config import settings


class Gateway:
    def __init__(self):
        url = urlsplit(settings.browser_phone_gateway)
        if url.scheme != 'http' or url.hostname not in ('127.0.0.1', 'voice-gateway') or url.path.rstrip('/') != '/janus':
            raise ValueError('Voice gateway must use the isolated local transport')
        self.base = settings.browser_phone_gateway.rstrip('/')
        self.client = httpx.AsyncClient(timeout=40, trust_env=False, follow_redirects=False)
        self.session = self.handle = None

    async def post(self, janus, *, handle=False, **data):
        path = self.base
        if self.session is not None:
            path += '/' + str(self.session)
        if handle:
            path += '/' + str(self.handle)
        response = await self.client.post(path, json={'janus': janus, 'transaction': secrets.token_hex(12),
                'apisecret': settings.browser_phone_gateway_secret, **data})
        response.raise_for_status()
        result = response.json()
        if result.get('janus') == 'error':
            raise ValueError('Voice gateway rejected the request')
        return result

    async def open(self, login, password, name):
        self.session = (await self.post('create'))['data']['id']
        self.handle = (await self.post('attach', plugin='janus.plugin.sip'))['data']['id']
        await self.message({'request': 'register', 'username': f'sip:{login}@{settings.browser_phone_domain}',
                'authuser': login, 'secret': password, 'proxy': f'sip:{settings.browser_phone_domain}',
                'display_name': name[:100], 'user_agent': 'DurovOS BrowserPhone', 'register_ttl': 120})

    async def message(self, body, jsep=None):
        return await self.post('message', handle=True, body=body, **({'jsep': jsep} if jsep else {}))

    async def poll(self):
        result = await self.client.get(self.base + '/' + str(self.session), params={
            'rid': secrets.randbelow(9007199254740991), 'maxev': 10,
            'apisecret': settings.browser_phone_gateway_secret})
        result.raise_for_status()
        data = result.json()
        return data if isinstance(data, list) else [data]

    async def close(self):
        try:
            if self.session:
                await self.post('destroy')
        finally:
            await self.client.aclose()
