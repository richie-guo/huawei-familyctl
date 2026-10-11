#!/usr/bin/env python3
"""smart-life 云端链路 POC：华为账号 OAuth 登录 + 智慧生活设备清单（复用 ha-huawei-smarthome 纯 stdlib 实现）。

两段式驱动（适配非交互终端）：
  1) python3 poc_cloud_login.py begin --account <邮箱/手机号> --state ./state
     （密码走环境变量 HUAKEI_PW / HUAWEI_PW；触发短信验证码后退出，会话态落盘）
  2) python3 poc_cloud_login.py complete --code <验证码> --state ./state
     （完成登录 → 拉设备清单 → 原始响应体落盘供挖路由器接口）
  3) python3 poc_cloud_login.py list --state ./state
     （复用已存 session 再列一次设备，token 过期自动刷新）

安全：state/ 内为 pickle 的凭据态，0600 权限，勿入库（.gitignore 已加）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import pickle
import secrets
import sys
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parent / "ha-huawei-smarthome" / "custom_components"
sys.path.insert(0, str(REPO))

from huawei_smarthome.api.client import SmartHomeDiscoveryApi  # noqa: E402
from huawei_smarthome.api.transport import HttpResponse, UrllibHttpTransport  # noqa: E402
from huawei_smarthome.auth.huawei import HuaweiSmartHomeAuthProvider  # noqa: E402
from huawei_smarthome.auth.interface import ChallengeChannel  # noqa: E402

STATE_DIR_DEFAULT = Path(__file__).resolve().parent / "state"
PROVIDER_PKL = "provider.pkl"
SESSION_PKL = "session.pkl"
IDENTITY_JSON = "identity.json"
RAW_DIR = "raw"


def _state_dir(state: str) -> Path:
    d = Path(state).resolve()
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    return d


def _load_or_create_identity(d: Path, account: str) -> dict:
    """账号绑定稳定身份（华为风控要求 device_id/pushtmid 稳定，照抄参考实现的生成规则）。"""
    f = d / IDENTITY_JSON
    if f.exists():
        return json.loads(f.read_text())
    identity = {
        "account": account,
        "device_id": str(uuid.uuid4()).upper(),
        "device_name": "huawei-smarthome",
        "pushtmid": secrets.token_hex(32),
    }
    f.write_text(json.dumps(identity, indent=1))
    os.chmod(f, 0o600)
    return identity


def _provider(d: Path, identity: dict) -> HuaweiSmartHomeAuthProvider:
    return HuaweiSmartHomeAuthProvider(
        device_id=identity["device_id"],
        device_name=identity["device_name"],
        pushtmid=identity["pushtmid"],
    )


class CapturingTransport(UrllibHttpTransport):
    """包一层：把发往 smarthome.hicloud.com 的请求/响应原文落盘，供后续挖路由器接口。"""

    def __init__(self, raw_dir: Path):
        super().__init__()
        self._raw = raw_dir
        self._raw.mkdir(parents=True, exist_ok=True)
        self._seq = 0

    async def request(self, method: str, url: str, headers=None, body=None, timeout: float = 20.0) -> HttpResponse:
        resp = await super().request(method, url, headers, body, timeout)
        self._seq += 1
        name = f"{self._seq:03d}_{method.lower()}_{url.split('//', 1)[1].split('?')[0].replace('/', '_')[:90]}"
        (self._raw / (name + ".req.txt")).write_text(
            f"{method} {url}\n\n" + "\n".join(f"{k}: {v}" for k, v in (headers or {}).items()),
            encoding="utf-8",
        )
        try:
            (self._raw / (name + ".resp.json")).write_text(
                resp.body.decode("utf-8", "replace"), encoding="utf-8"
            )
        except Exception:
            pass
        return resp


def _pick_channel(channels, prefer: str):
    kinds = {"sms": ("sms",), "email": ("email",)}
    order = kinds[prefer]
    for kind in order:
        for c in channels:
            if c.kind == kind:
                return c
    return channels[0] if channels else None


async def cmd_begin(args) -> int:
    d = _state_dir(args.state)
    account = args.account
    password = os.environ.get("HUAWEI_PW") or args.password
    if not account or not password:
        print("需要 --account 与密码（环境变量 HUAWEI_PW 或 --password）", file=sys.stderr)
        return 2
    identity = _load_or_create_identity(d, account)
    provider = _provider(d, identity)
    start = await provider.async_begin_login(account, password)
    if start.session is not None:
        (d / SESSION_PKL).write_bytes(pickle.dumps(start.session))
        print("无需挑战，直接登录成功，session 已存。可直接跑 complete --code SKIP 或 list")
        return 0
    ch = start.challenge
    if ch is None:
        print("登录既无 session 也无 challenge（异常）", file=sys.stderr)
        return 1
    print(f"挑战类型: {ch.challenge_type} / {ch.challenge_name}")
    for c in ch.channels:
        print(f"  可用通道: kind={c.kind} name={c.name} account_type={c.account_type} sent={c.sent}")
    channel = _pick_channel(ch.channels, args.channel)
    if channel is None:
        print("无可用挑战通道", file=sys.stderr)
        return 1
    await provider.async_select_challenge_channel(channel)
    print(f"验证码已下发：{channel.kind}（{channel.name}）")
    (d / PROVIDER_PKL).write_bytes(pickle.dumps(provider))
    os.chmod(d / PROVIDER_PKL, 0o600)
    print(f"provider 状态已存 {d/PROVIDER_PKL}，下一步: complete --code <验证码>")
    return 0


async def cmd_complete(args) -> int:
    d = _state_dir(args.state)
    pf = d / PROVIDER_PKL
    if not pf.exists():
        print("没有 provider 状态，先跑 begin", file=sys.stderr)
        return 2
    provider: HuaweiSmartHomeAuthProvider = pickle.loads(pf.read_bytes())
    if args.code.upper() == "SKIP":
        session = pickle.loads((d / SESSION_PKL).read_bytes())
    else:
        session = await provider.async_complete_challenge(args.code)
        (d / SESSION_PKL).write_bytes(pickle.dumps(session))
        os.chmod(d / SESSION_PKL, 0o600)
        pf.unlink(missing_ok=True)
    print(
        f"登录成功: user_id={session.user_id} base_url={session.base_url} "
        f"hms_token={'Y' if session.hms_access_token else 'N'} "
        f"oauth_token={'Y' if session.oauth_access_token else 'N'}"
    )
    await _list_devices(session, d)
    return 0


async def cmd_list(args) -> int:
    d = _state_dir(args.state)
    sf = d / SESSION_PKL
    if not sf.exists():
        print("没有 session，先 begin/complete", file=sys.stderr)
        return 2
    session = pickle.loads(sf.read_bytes())
    await _list_devices(session, d)
    (d / SESSION_PKL).write_bytes(pickle.dumps(session))
    return 0


async def cmd_detail(args) -> int:
    """拉指定设备（默认路由器）的 v2 detail，原始响应落盘（找 Profile/rule schema）。"""
    d = _state_dir(args.state)
    sf = d / SESSION_PKL
    if not sf.exists():
        print("没有 session，先 begin/complete", file=sys.stderr)
        return 2
    session = pickle.loads(sf.read_bytes())
    api = SmartHomeDiscoveryApi(transport=CapturingTransport(d / RAW_DIR))
    dev_id = args.dev_id
    if not dev_id:
        snap = await api.async_get_snapshot(session)
        for dev in snap.devices:
            if dev.model and "YGJN" in dev.model:
                dev_id = dev.dev_id
                break
        if not dev_id:
            print("未找到路由器（YGJN-*），请用 --dev_id 指定", file=sys.stderr)
            return 1
    detail = await api.async_get_device_detail(session, dev_id)
    print(f"detail: {detail.name!r} model={detail.model!r} prod={detail.prod_id}")
    ss = sorted(detail.service_states)
    print(f"service_states（{len(ss)}）: {ss}")
    print(f"原始响应已存 {d/RAW_DIR}/（找 v2_devices 详情与 profile schema）")
    return 0


async def _list_devices(session, d: Path) -> None:
    api = SmartHomeDiscoveryApi(transport=CapturingTransport(d / RAW_DIR))
    snap = await api.async_get_snapshot(session)
    print(f"\n家庭 {len(snap.homes)} 个：")
    for h in snap.homes:
        print(f"  home_id={h.home_id} name={h.name!r}")
    print(f"设备 {len(snap.devices)} 台：")
    for dev in snap.devices:
        print(
            f"  [{dev.dev_id}] {dev.name!r} model={dev.model!r} prod={dev.prod_id} "
            f"online={dev.online} room={dev.room_name!r} fw={dev.firmware_version!r}"
        )
    print(f"原始响应体已存 {d/RAW_DIR}/（devices/homes 快照，供挖路由器接口）")


async def cmd_mqtt(args) -> int:
    """验证命令通道：消息中心登录 → 选云路由 → MQTT(TLS 8883) 连接订阅。只听不发。"""
    d = _state_dir(args.state)
    sf = d / SESSION_PKL
    if not sf.exists():
        print("没有 session，先 begin/complete", file=sys.stderr)
        return 2
    session = pickle.loads(sf.read_bytes())
    from huawei_smarthome.client import HuaweiSmartHomeClient  # 仅借用其 MQTT 连接流程
    api = SmartHomeDiscoveryApi(transport=CapturingTransport(d / RAW_DIR))
    mc = await api.async_login_message_center(session)
    print("message-center login OK，keys:", sorted(mc.__dict__) if hasattr(mc, "__dict__") else type(mc))
    route = await api.async_select_cloud_route(session)
    print(f"cloud route: host={route.smart_home_host}")
    from huawei_smarthome.mqtt_client import HuaweiMqttClient
    from huawei_smarthome.const import OBSERVED_MQTT_FILTER, OBSERVED_MQTT_PORT, OBSERVED_MQTT_SUBSCRIPTION_QOS
    from huawei_smarthome.domain.models import MqttConnectionSettings
    settings = MqttConnectionSettings(
        broker_host=route.smart_home_host,
        broker_port=OBSERVED_MQTT_PORT,
        client_id=str(uuid.uuid4()).upper(),
        subscription_filters=(OBSERVED_MQTT_FILTER,),
        subscription_qos=OBSERVED_MQTT_SUBSCRIPTION_QOS,
        username=session.user_id,
        password=session.oauth_access_token,
        control_source=session.device_name,
        use_tls=True,
        keepalive=60,
        clean_session=True,
    )
    mqtt = HuaweiMqttClient()
    got = asyncio.Event()
    mqtt.set_message_handler(lambda payload: (got.set(), print(f"  <- 收到推送 {len(payload)}B"))[1])
    await mqtt.async_start(settings)
    try:
        for _ in range(30):
            if mqtt.state.name == "RUNNING":
                break
            await asyncio.sleep(1)
        print(f"MQTT 状态: {mqtt.state.name}（broker={route.smart_home_host}:{OBSERVED_MQTT_PORT} TLS）")
        try:
            await asyncio.wait_for(got.wait(), timeout=10)
        except asyncio.TimeoutError:
            print("（10s 内无推送——正常，安静即健康）")
    finally:
        await mqtt.async_stop()
    (d / SESSION_PKL).write_bytes(pickle.dumps(session))
    print("命令通道验证完成（未发送任何设备命令）")
    return 0


async def cmd_listen(args) -> int:
    """旁听云命令总线（订阅 command 主题，只听不发）：App 驱动界面时抓真实信封。"""
    d = _state_dir(args.state)
    sf = d / SESSION_PKL
    if not sf.exists():
        print("没有 session，先 begin/complete", file=sys.stderr)
        return 2
    session = pickle.loads(sf.read_bytes())
    api = SmartHomeDiscoveryApi(transport=UrllibHttpTransport())
    await api.async_login_message_center(session)
    route = await api.async_select_cloud_route(session)
    from huawei_smarthome.mqtt_client import HuaweiMqttClient
    from huawei_smarthome.const import OBSERVED_MQTT_FILTER, OBSERVED_MQTT_PORT, OBSERVED_MQTT_SUBSCRIPTION_QOS
    from huawei_smarthome.domain.models import MqttConnectionSettings
    settings = MqttConnectionSettings(
        broker_host=route.smart_home_host,
        broker_port=OBSERVED_MQTT_PORT,
        client_id=str(uuid.uuid4()).upper(),
        subscription_filters=(OBSERVED_MQTT_FILTER,),
        subscription_qos=OBSERVED_MQTT_SUBSCRIPTION_QOS,
        username=session.user_id,
        password=session.oauth_access_token,
        control_source=session.device_name,
        use_tls=True,
        keepalive=60,
        clean_session=True,
    )
    out = open(args.out, "w", buffering=1)
    import time as _t
    mqtt = HuaweiMqttClient()

    def on_msg(payload: bytes):
        line = f"{_t.strftime('%H:%M:%S')} {len(payload)}B {payload.decode('utf-8', 'replace')}"
        print(line, flush=True)
        out.write(line + "\n")

    mqtt.set_message_handler(on_msg)
    await mqtt.async_start(settings)
    for _ in range(30):
        if mqtt.state.name == "RUNNING":
            break
        await asyncio.sleep(1)
    print(f"LISTENING broker={route.smart_home_host}:{OBSERVED_MQTT_PORT} filter={OBSERVED_MQTT_FILTER}", flush=True)
    try:
        await asyncio.sleep(args.seconds)
    finally:
        await mqtt.async_stop()
        out.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p1 = sub.add_parser("begin", help="发起登录并触发验证码")
    p1.add_argument("--account", required=True)
    p1.add_argument("--password", default="")
    p1.add_argument("--channel", default="sms", choices=["sms", "email"])
    p1.add_argument("--state", default=str(STATE_DIR_DEFAULT))
    p2 = sub.add_parser("complete", help="提交验证码完成登录并列设备")
    p2.add_argument("--code", required=True)
    p2.add_argument("--state", default=str(STATE_DIR_DEFAULT))
    p3 = sub.add_parser("list", help="复用 session 列设备")
    p3.add_argument("--state", default=str(STATE_DIR_DEFAULT))
    p4 = sub.add_parser("detail", help="拉设备 v2 详情（默认路由器），找 profile schema")
    p4.add_argument("--dev_id", default="")
    p4.add_argument("--state", default=str(STATE_DIR_DEFAULT))
    p5 = sub.add_parser("mqtt", help="验证命令通道（消息中心+MQTT 连接，只听不发）")
    p5.add_argument("--state", default=str(STATE_DIR_DEFAULT))
    p6 = sub.add_parser("listen", help="旁听云命令总线（App 驱动界面时抓真实信封）")
    p6.add_argument("--seconds", type=int, default=120)
    p6.add_argument("--out", default="mqtt-capture.log")
    p6.add_argument("--state", default=str(STATE_DIR_DEFAULT))
    args = ap.parse_args()
    return asyncio.run(
        {"begin": cmd_begin, "complete": cmd_complete, "list": cmd_list, "detail": cmd_detail,
         "mqtt": cmd_mqtt, "listen": cmd_listen}[args.cmd](args)
    )


if __name__ == "__main__":
    sys.exit(main())
