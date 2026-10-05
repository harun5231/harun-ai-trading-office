"""Fixed-origin HTTPS transport. No redirects, response-body logs or raw exceptions."""
import json
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler
from urllib.error import HTTPError
from .core import Review

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None

def unique(pairs):
    out={}
    for k,v in pairs:
        if k in out:raise ValueError('DUPLICATE_FIELD')
        out[k]=v
    return out

def request(method,url,headers=None,body=None,timeout=60):
    payload=None if body is None else json.dumps(body,separators=(',',':')).encode()
    req=Request(url,data=payload,method=method,headers=headers or {})
    try:
        opener=build_opener(ProxyHandler({}),NoRedirect())
        try:res=opener.open(req,timeout=timeout)
        except HTTPError as e:res=e
        with res:
            code=res.code;hint=res.headers.get('Retry-After','')
            if code!=200:return code,{'retry-after':hint},None
            data=res.read(4_000_001)
            if len(data)>4_000_000:raise ValueError('SIZE')
            parsed=json.loads(data,parse_float=str,object_pairs_hook=unique)
            return code,{},parsed
    except Exception:raise Review('HTTP_RESULT_UNCERTAIN') from None
