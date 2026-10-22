"""Read-only OKX REST integration. Fixed GET routes; no trading SDK."""
import base64
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import hmac
import json
import math
import re
import time
from urllib.parse import urlencode
from urllib.request import build_opener, HTTPRedirectHandler, HTTPSHandler, Request

ORIGIN = 'https://www.okx.com'
TICKER = '/api/v5/market/ticker'
CONFIG = '/api/v5/account/config'
POSITIONS = '/api/v5/account/positions'
BALANCE = '/api/v5/account/balance'
INSTRUMENTS = '/api/v5/public/instruments'
PUBLIC_ROUTES = frozenset((TICKER, INSTRUMENTS))
GET_ROUTES = frozenset((TICKER, CONFIG, POSITIONS, BALANCE, INSTRUMENTS))
DEFAULT_OKX = {'inst_id': 'BTC-USDT', 'inst_type': 'SPOT', 'simulated': False,
               'position_stop_loss_percent': 3., 'daily_loss_percent': 3.,
               'timeout_seconds': 5.}


def validate_options(data=None):
    if data is None:
        return DEFAULT_OKX.copy()
    if not isinstance(data, dict):
        raise ValueError('OKX 设置必须是对象')
    result = {key: data.get(key, value) for key, value in DEFAULT_OKX.items()}
    if not isinstance(result['inst_id'], str):
        raise ValueError('交易对格式无效')
    result['inst_id'] = result['inst_id'].strip().upper()
    if len(result['inst_id']) > 64 or not re.fullmatch(r'[A-Z0-9]+(?:-[A-Z0-9]+){1,5}', result['inst_id']):
        raise ValueError('交易对格式无效，例如 BTC-USDT')
    # Keep legacy IDs verbatim (apart from the established case normalization).
    # The canonical ID wins over stale/absent auxiliary market metadata.
    result['inst_type'] = 'SWAP' if result['inst_id'].endswith('-SWAP') else 'SPOT'
    if type(result['simulated']) is not bool:
        raise ValueError('模拟盘设置必须为 true 或 false')
    for key in ('position_stop_loss_percent', 'daily_loss_percent', 'timeout_seconds'):
        n = result[key]
        if type(n) not in (int, float) or not math.isfinite(n) or not 0 < n <= 1000:
            raise ValueError('OKX 比例或超时设置无效')
    if not .5 <= result['timeout_seconds'] <= 8:
        raise ValueError('OKX 单次超时必须为 0.5～8 秒')
    return result


@dataclass(repr=False)
class Credentials:
    api_key: str = field(default='', repr=False)
    secret_key: str = field(default='', repr=False)
    passphrase: str = field(default='', repr=False)

    @property
    def present(self):
        return any((self.api_key, self.secret_key, self.passphrase))

    @property
    def complete(self):
        return all((self.api_key, self.secret_key, self.passphrase))


def load_credentials(external):
    try:
        data = json.loads((external / 'okx.local.json').read_text(encoding='utf-8-sig'))
        if not isinstance(data, dict):
            raise ValueError()
        values = [data.get(name, '') for name in ('api_key', 'secret_key', 'passphrase')]
        if any(not isinstance(s, str) or len(s) > 1024 or '\n' in s or '\r' in s for s in values):
            raise ValueError()
        return Credentials(*(s.strip() for s in values))
    except FileNotFoundError:
        return Credentials()
    except (OSError, ValueError, TypeError):
        raise ValueError('本地 OKX 凭据文件无法读取，请在 FX设置 中重新保存') from None


def number(value):
    if isinstance(value, bool) or value in ('', None):
        return None
    try:
        n = float(value)
        return n if math.isfinite(n) else None
    except (ValueError, TypeError, OverflowError):
        return None


class OKXError(Exception):
    """Only constant, non-sensitive messages may cross the worker boundary."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass
class Snapshot:
    price: float | None = None
    equity: float | None = None
    account_upl_usd: float | None = None
    worst_upl_percent: float | None = None
    account_scope: str | None = None
    private_enabled: bool = False
    permission_warning: str | None = None


class OKXClient:
    def __init__(self, options, credentials=None, transport=None):
        self.options = validate_options(options)
        self.credentials = credentials or Credentials()
        self.opener = build_opener(NoRedirect(), HTTPSHandler())
        self.transport = transport
        self.permission_checked = False
        self.private_enabled = False
        self.permission_warning = None
        self.scope = None

    def _get(self, route, query=None, private=False, deadline=None):
        if route not in GET_ROUTES or (route in PUBLIC_ROUTES) == private:
            raise OKXError('不允许的接口')
        valid_query = ((route == TICKER and isinstance(query, dict) and set(query) == {'instId'}
                        and query['instId'] == self.options['inst_id'])
                       or (route == INSTRUMENTS and isinstance(query, dict) and set(query) == {'instType'}
                           and query['instType'] in ('SPOT', 'SWAP')))
        if (query and not valid_query) or (route in PUBLIC_ROUTES and not valid_query):
            raise OKXError('不允许的参数')
        path = route + ('?' + urlencode(query) if query else '')
        headers = {'Accept': 'application/json', 'User-Agent': 'KurumiPet/1.0'}
        if self.options['simulated']:
            headers['x-simulated-trading'] = '1'
        if private:
            if not self.credentials.complete:
                raise OKXError('本地凭据不完整')
            timestamp = datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
            signature = base64.b64encode(hmac.new(self.credentials.secret_key.encode(),
                                        (timestamp+'GET'+path).encode(), hashlib.sha256).digest()).decode()
            headers.update({'OK-ACCESS-KEY': self.credentials.api_key,
                            'OK-ACCESS-SIGN': signature, 'OK-ACCESS-TIMESTAMP': timestamp,
                            'OK-ACCESS-PASSPHRASE': self.credentials.passphrase})
        # Older external configs contain 3s. Keep those files untouched while
        # giving slow but healthy connections at least a 5s request budget.
        timeout = max(5., self.options['timeout_seconds'])
        if deadline is not None:
            timeout = min(timeout, deadline-time.monotonic())
            if timeout <= 0:
                raise OKXError('连接超时')
        try:
            if self.transport:
                payload = self.transport(path, headers, timeout)
            else:
                request = Request(ORIGIN+path, headers=headers, method='GET')
                with self.opener.open(request, timeout=timeout) as response:
                    if response.geturl() != ORIGIN+path:
                        raise OKXError('连接异常')
                    # The complete public catalog is larger than a ticker or
                    # account response. Bound it separately without truncation.
                    limit = (8 if route == INSTRUMENTS else 1) * 1024 * 1024
                    raw = response.read(limit+1)
                    if len(raw) > limit:
                        raise OKXError('响应过大')
                    payload = json.loads(raw)
            if not isinstance(payload, dict) or str(payload.get('code')) != '0' or not isinstance(payload.get('data'), list):
                raise OKXError('接口返回错误')
            return payload['data']
        except OKXError:
            raise
        except Exception:
            # Never propagate HTTP errors, response text, headers or credentials.
            raise OKXError('连接或数据读取失败') from None

    def instruments(self, inst_type):
        return self._get(INSTRUMENTS, {'instType': inst_type})

    def poll_ticker(self):
        data = self._get(TICKER, {'instId': self.options['inst_id']})
        try:
            quote = data[0]
            price, timestamp = number(quote.get('last')), number(quote.get('ts'))
            if (quote.get('instId') != self.options['inst_id'] or price is None or price <= 0
                    or timestamp is None or not -5 <= time.time()-timestamp/1000 <= 60):
                raise ValueError()
        except (IndexError, AttributeError, ValueError, TypeError):
            raise OKXError('行情无效或已过期') from None
        return Snapshot(price)

    def poll(self):
        """Combined compatibility helper; GUI uses two independent workers."""
        snapshot = self.poll_ticker()
        if self.credentials.present:
            account = self.poll_account()
            account.price = snapshot.price
            return account
        return snapshot

    def poll_account(self):
        deadline = time.monotonic()+3*max(5., self.options['timeout_seconds'])
        snapshot = Snapshot()
        if not self.credentials.present:
            return snapshot
        if not self.permission_checked:
            config = self._get(CONFIG, private=True, deadline=deadline)
            try:
                permissions = {s.strip().lower() for s in config[0]['perm'].split(',')}
                uid = str(config[0].get('uid', ''))
            except (KeyError, IndexError, TypeError, AttributeError):
                permissions, uid = set(), ''
            self.private_enabled = permissions == {'read_only'} and bool(uid)
            if permissions & {'trade', 'withdraw'}:
                self.permission_warning = '检测到交易或提币权限，建议改成只读。账户读取未启用，公共行情仍可使用。'
            elif not self.private_enabled:
                self.permission_warning = '无法确认密钥只读权限，账户读取未启用。请检查本地密钥设置。'
            self.scope = hashlib.sha256((uid+str(self.options['simulated'])).encode()).hexdigest()[:20] if uid else None
            self.permission_checked = True
        snapshot.permission_warning = self.permission_warning
        snapshot.private_enabled = self.private_enabled
        if not self.private_enabled:
            return snapshot
        positions = self._get(POSITIONS, private=True, deadline=deadline)
        balance = self._get(BALANCE, private=True, deadline=deadline)
        try:
            equity = number(balance[0]['totalEq'])
            if equity is None or equity < 0:
                raise ValueError()
            ratios = []
            for position in positions:
                size, ratio = number(position.get('pos')), number(position.get('uplRatio'))
                if size is not None and size != 0 and ratio is not None:
                    ratios.append(ratio*100)
            snapshot.equity = equity
            snapshot.account_upl_usd = number(balance[0].get('upl'))
            snapshot.worst_upl_percent = min(ratios) if ratios else None
            snapshot.account_scope = self.scope
        except (IndexError, KeyError, ValueError, TypeError, AttributeError):
            raise OKXError('账户数据无效') from None
        return snapshot
