from __future__ import annotations
import os
import ssl
import json
from datetime import datetime, timedelta
from http.client import HTTPResponse
from typing import TypeVar, Callable, Generator, Generic, Optional
import urllib.error, urllib.request, urllib.response, urllib.parse
import dateutil.parser

from console import log

def value_or_fallback(x, fallback):
    """ return fallback if x is None else x """
    return fallback if x is None else x

def try_int(x, fallback=None):
    if x is None:
        return fallback
    try:
        return int(x)
    except Exception:
        return fallback

def parse_datetime(datestr: str) -> datetime | None:
    if not datestr:
        return None
    try:
        return dateutil.parser.parse(datestr)
    except Exception:
        try:
            # 2022-03-17T16:06:57Z
            return datetime.strptime(datestr, '%Y-%m-%dT%H:%M:%S.%fZ')
        except Exception:
            return None

def get_many(d: dict, *keys, default=None):
    return next((d[key] for key in keys if key in d), default)

def flatten(list_of_lists: list[list]):
    return [val for sublist in list_of_lists for val in sublist]

T = TypeVar('T')

class Lazy(Generic[T]):
    def __init__(self, func: Callable[[], T]):
        self._func = func
        self._value: T | None = None
    
    def __call__(self) -> T:
        return self.value
    
    @cached_property
    def value(self):
        if self._value is None:
            self._value = self._func()
        return self._value

class JsonResponse:
    """Wrapper over a dict + status code / ok and message in case of not ok.
    This supports most dict read operators as well as truthiness check for 'is response ok'
    """
    def __init__(self, ok: bool, status: int, message: str, data: dict | None):
        self.ok = True if ok else False
        self.status = int(status)
        self.message = str(message)
        self.data = data if data is not None else dict()
    def keys(self): return self.data.keys()
    def values(self): return self.data.values()
    def items(self): return self.data.items()
    def get(self, key, default=None): return self.data.get(key, default)
    def __iter__(self): return self.data.__iter__
    def __contains__(self, item): return item in self.data
    def __getitem__(self, key): return self.data[key]
    def __len__(self): return len(self.data)
    def __str__(self): return str(self.data)
    def __bool__(self): return self.ok
    def __nonzero__(self): return self.ok
    def __eq__(self, other):
        if isinstance(other, JsonResponse):
            return self.ok == other.ok and self.data == other.data
        elif self.ok and isinstance(other, dict):
            return self.data == other
        else:
            return False

# context to ignore ssl cert issues
ssl_ctx = ssl.create_default_context()
ssl_ctx.check_hostname = False
ssl_ctx.verify_mode = ssl.CERT_NONE

def try_json_request(url: str, lower_keys=True, headers: dict[str,str] | None = None, body = None, method='GET'):
    try:
        log.debug(f"try to hit {url}")
        headers = headers or {}
        headers = headers.copy()

        # add default headers for send json -> receive json
        header_keys = set(k.lower() for k in headers.keys())
        if 'user-agent' not in header_keys:
            # poor man's trick for apis that let browsers through without api keys: pretend we're a browser
            headers['User-Agent'] = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/110.0'
        if 'content-type'not in header_keys:
            headers['Content-Type'] = 'application/json'
        if 'accept' not in header_keys:
            headers['Accept'] = 'application/json'

        # encode the body properly
        data = body
        if body is not None:
            content_type = headers.get('Content-Type', headers.get('content-type','')).lower()
            if isinstance(body, dict):
                if 'form-urlencoded' in content_type:
                    data = '&'.join(f'{key}={value}' for (key, value) in body.items())
                elif 'json' in content_type:
                    data = json.dumps(body)
        if isinstance(data, str):
            data = data.encode()
        # headers['Content-Length'] = len(body)
        
        req = urllib.request.Request(url, headers=headers, data=data, method=method)
        resp: HTTPResponse = urllib.request.urlopen(req, context=ssl_ctx)
        resp_body: bytes = resp.read()
        if resp.status != 200:
            return JsonResponse(False, resp.status, resp_body.decode(), {})
        raw_json: dict[str,object] = json.loads(resp_body.decode())
        if not lower_keys:
            json_data = raw_json
        else:
            json_data = {key.lower(): raw_json[key] for key in raw_json}
        return JsonResponse(True, resp.status, '', json_data)
    
    except urllib.error.HTTPError as err:
        if err.code == 404:
            log.debug(f"Not found: {url}")
        # elif err.code >= 400 and err.code < 500:
        #    return {}
        else:
            log.warn(f"{url} : {err.code} {err.reason} (body: '{body}')")
        return JsonResponse(False, err.code, err.reason, {})
    
    except Exception as ex:
        log.warn(f"Unexpected error from {url}: {ex}")
        return JsonResponse(False, -1, str(ex), {})
