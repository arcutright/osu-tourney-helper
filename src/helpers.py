import os
import ssl
import json
from typing import Union, Tuple
from datetime import datetime, timedelta
from http.client import HTTPResponse
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

def parse_datetime(datestr: str) -> "Union[datetime, None]":
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

def flatten(list_of_lists: "list[list]"):
    return [val for sublist in list_of_lists for val in sublist]

# context to ignore ssl cert issues
ssl_ctx = ssl.create_default_context()
ssl_ctx.check_hostname = False
ssl_ctx.verify_mode = ssl.CERT_NONE

def try_json_get(url: str, lower_keys=True):
    try:
        log.debug(f"try to hit {url}")
        headers = {
            'User-Agent' : 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/110.0',
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        }
        req = urllib.request.Request(url, headers=headers, method='GET')
        resp: HTTPResponse = urllib.request.urlopen(req, context=ssl_ctx)
        if resp.status != 200:
            return {}
        data: bytes = resp.read()
        raw_json = json.loads(data.decode())
        if not lower_keys:
            return raw_json
        else:
            return {key.lower(): raw_json[key] for key in raw_json}
    except urllib.error.HTTPError as err:
        if err.code == 404:
            log.debug(f"Not found: {url}")
            return {}
        elif err.code >= 400 and err.code < 500:
            return {}
        log.warn(f"{url} : {err.code} {err.reason}")
        return {}
    except Exception as ex:
        log.warn(f"Unexpected error from {url}: {ex}")
        return {} 
