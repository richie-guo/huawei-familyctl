#!/usr/bin/env python3
"""HAG 子系统命令中继实测（CloudApi.controlHomeSubSystem 复刻，2026-10-11 字节码规格）。

POST {cloud}/smart-life/v3/{homeId}/subsystem/instance/{devId}/command
body: {"requestId": uuid4, "timestamp": ms, "sid": <服务名>, "data": <载荷>}
headers: Authorization Bearer <oauth> + X-AppId/X-Huid/X-RequestId + x-e2e-traceid/x-control-*
先以 gateway/ntwk/childAppList（应用目录，返回量大易核对）验证通路；成功后可探测其他 sid。
只读探测。
"""
import json
import sys
import time
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from p50_lib import get_session, ROUTER, _CFG

HOME_ID = _CFG.get("home_id", "")
DEV_ID = ROUTER
CLOUD = "https://smarthome.hicloud.com"


def relay(sid: str, data: dict | None = None, token: str | None = None):
    s = json.dumps({
        "requestId": str(uuid.uuid4()).upper(),
        "timestamp": int(time.time() * 1000),
        "sid": sid,
        "data": data or {},
    })
    req = urllib.request.Request(
        f"{CLOUD}/smart-life/v3/{HOME_ID}/subsystem/instance/{DEV_ID}/command",
        data=s.encode(), method="POST")
    req.add_header("Content-Type", "application/json;charset=UTF-8")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("X-AppId", "com.huawei.smarthome-ios")
    req.add_header("X-AppVersion", "17.0.3.320")
    req.add_header("X-Huid", str(_CFG.get("huid", "")))
    req.add_header("X-Ori-App-Name", "com.huawei.smarthome-ios")
    req.add_header("X-Language", "zh_CN")
    req.add_header("X-RequestId", str(uuid.uuid4()).upper())
    req.add_header("x-e2e-traceid", str(uuid.uuid4()).upper())
    req.add_header("x-control-source", "huawei")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:500]
    except Exception as e:
        return -1, str(e)


def main():
    session = asyncio_run_get_session()
    token = session.oauth_access_token
    sids = sys.argv[1:] or ["gateway/ntwk/childAppList?country=CN"]
    for sid in sids:
        code, body = relay(sid, {}, token)
        print(f"\n=== {sid}\nHTTP {code}\n{body[:800]}")


def asyncio_run_get_session():
    import asyncio
    return asyncio.run(get_session())


if __name__ == "__main__":
    main()
