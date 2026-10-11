#!/usr/bin/env python3
"""华为路由器儿童上网管控——云 MQTT 信封通用工具库（设备无关）。
⚠️ p50_open_games.py 没有 __main__ 保护，import 即执行'开35分钟'全流程（已两次误触发）。
任何脚本需要辅助函数时一律 import 本模块，禁止 import p50_open_games。

协议备忘（全部实测坐实，详见 git log 与 docs/）：
- 分类名单: POST childModelApps {data:{denyAll,type,device,apps,scene}, action:'update'}
    type=0 游戏总开关（开=denyAll:1+目录全量游戏ID；关=denyAll:0+apps:[]；
           denyAll:1+apps:[] 无效）type=2 视频 type=3 社交 type=4 游戏单独禁止
    GET 必须带 devid，否则返回空/聚合；type=0 GET 恒为 []（状态只读得到 homepage.gameDenyCount）
- 分类开关: POST childHomepage {data:{game:0/1,device,scene,...}, action:'gameUpdate'}
    （与 App 总开关不同层，作用存疑——优先用 type=0）
- 时段: POST childFrame {data:{id:'',timeFrom,timeTo,全周1,today:0,enable,scene,device}, action:'newCreate'}
    delete 端点 errcode=5002 不可用，禁用=newUpdate enable=0（整帧覆盖，必须带实读字段）
- 应用目录: GET childAppList?devid=N → {appInfo:[{id,categ,plat}]}（无名称）
    categ=4 游戏(153) 16 视频(41) 2 社交? 4096 学习（P50 目录实测）
- 全部设备列表: GET childHomepage → body 为全部设备记录数组（hostName/actualName 可作匹配键）
"""
import asyncio, json, os, pickle, re, sys, uuid
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent / "ha-huawei-smarthome" / "custom_components"
sys.path.insert(0, str(REPO))
from huawei_smarthome.api.client import SmartHomeDiscoveryApi  # noqa: E402
from huawei_smarthome.mqtt_client import HuaweiMqttClient  # noqa: E402
from huawei_smarthome.const import (  # noqa: E402
    OBSERVED_MQTT_FILTER, OBSERVED_MQTT_PORT, OBSERVED_MQTT_SUBSCRIPTION_QOS)
from huawei_smarthome.domain.models import MqttConnectionSettings  # noqa: E402
from huawei_smarthome.auth.huawei import HuaweiSmartHomeAuthProvider  # noqa: E402

def _load_config():
    """config.json（仓库根目录，gitignored）：router_dev_id/home_id/huid/default_device"""
    for base in (Path(__file__).resolve().parent, Path(__file__).resolve().parent.parent):
        f = base / "config.json"
        if f.exists():
            try:
                return json.loads(f.read_text())
            except Exception:
                pass
    return {}


_CFG = _load_config()
ROUTER = _CFG.get("router_dev_id") or os.environ.get("HILINK_ROUTER_ID", "")
P50 = str(_CFG.get("default_device") or "1")
HP = f'/devices/{ROUTER}/services/gateway/ntwk/childHomepage'
CF = f'/devices/{ROUTER}/services/gateway/ntwk/childFrame'
CMA = f'/devices/{ROUTER}/services/gateway/ntwk/childModelApps'
CAL = f'/devices/{ROUTER}/services/gateway/ntwk/childAppList'
P50 = '8'  # 历史默认设备；新代码请显式传 dev

# 分类名 → childModelApps type
CATEGORY = {'games': 4, 'video': 2, 'social': 3}
MASTER_TYPE = 0  # 游戏总开关
DAYS = ('sunday', 'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday')


async def get_session():
    sf = Path(__file__).resolve().parent / 'state' / 'session.pkl'
    session = pickle.loads(sf.read_bytes())
    provider = HuaweiSmartHomeAuthProvider(device_id=session.device_id or str(uuid.uuid4()).upper(),
        device_name=session.device_name or 'huawei-smarthome', pushtmid=session.pushtmid)
    if not session.is_oauth_valid(60): session = await provider.async_refresh_oauth(session)
    if not session.is_hms_valid(60): session = await provider.async_refresh_hms_lite(session)
    sf.write_bytes(pickle.dumps(session))
    return session


async def with_mqtt(session):
    api = SmartHomeDiscoveryApi()
    await api.async_login_message_center(session)
    route = await api.async_select_cloud_route(session)
    settings = MqttConnectionSettings(broker_host=route.smart_home_host, broker_port=OBSERVED_MQTT_PORT,
        client_id=str(uuid.uuid4()).upper(), subscription_filters=(OBSERVED_MQTT_FILTER,),
        subscription_qos=OBSERVED_MQTT_SUBSCRIPTION_QOS, username=session.user_id,
        password=session.oauth_access_token, control_source=session.device_name,
        use_tls=True, keepalive=60, clean_session=True)
    mqtt = HuaweiMqttClient()
    inbox = []
    def on_msg(*args):
        for a in args:
            if isinstance(a, bytes): inbox.append(a.decode('utf-8', 'replace'))
    mqtt.set_message_handler(on_msg)
    await mqtt.async_start(settings)
    for _ in range(30):
        if mqtt.state.name == 'RUNNING': break
        await asyncio.sleep(1)
    return mqtt, inbox


def env(s, m, t, b):
    return {"header": {"mode": "ACK", "method": m, "requestId": str(uuid.uuid4()),
        "from": f"/users/{s.user_id}", "ctrlSrcType": "2", "ctrlSrc": s.device_name,
        "to": t, "accessToken": s.oauth_access_token, "ctrlType": 1,
        "timestamp": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")}, "body": b}


async def pub(mqtt, s, t, b, inbox):
    """POST 写入并等响应，返回 errcode 字符串（'no-resp'=无响应视为失败）"""
    inbox.clear()
    await mqtt.async_publish(topic=OBSERVED_MQTT_FILTER,
        payload=json.dumps(env(s, 'POST', t, b), separators=(',', ':')).encode(), qos=1)
    await asyncio.sleep(8)
    for line in inbox:
        if 'errcode' in line:
            m = re.search(r'"errcode":(\d+)', line)
            return m.group(1) if m else '0'
    return 'no-resp'


async def read_sid(mqtt, s, inbox, sid):
    """GET 一个服务（可带 query），按响应 header.from 匹配，返回 body"""
    inbox.clear()
    await mqtt.async_publish(topic=OBSERVED_MQTT_FILTER,
        payload=json.dumps(env(s, 'GET', f'/devices/{ROUTER}/services/{sid}', {}),
                           separators=(',', ':')).encode(), qos=1)
    base = sid.split('?')[0]
    for _ in range(20):
        await asyncio.sleep(1)
        for line in inbox:
            i = line.find('{')
            if i < 0: continue
            try:
                js = json.loads(line[i:])
                if base in json.dumps(js.get('header', {})):
                    return js.get('body')
            except Exception: continue
    return None


# ---------- 设备层 ----------

async def list_devices(mqtt, s, inbox):
    """全部受控设备记录（homepage 返回数组）"""
    body = await read_sid(mqtt, s, inbox, 'gateway/ntwk/childHomepage')
    return body if isinstance(body, list) else []


def resolve_dev(records, key):
    """按 序号/主机名/实际名 模糊匹配设备，返回 (device_id, record) 或 (None, None)"""
    key = str(key).strip()
    for d in records:
        if d.get('device') == key:
            return d.get('device'), d
    kl = key.lower()
    for d in records:
        names = f"{d.get('hostName','')} {d.get('actualName','')}".lower()
        if kl in names:
            return d.get('device'), d
    return None, None


# ---------- 读取原语 ----------

async def get_home(mqtt, s, inbox, dev):
    """单设备 homepage 记录"""
    body = await read_sid(mqtt, s, inbox, f'gateway/ntwk/childHomepage?devid={dev}&scene=0')
    if isinstance(body, list):
        for d in body:
            if d.get('device') == str(dev): return d
    elif isinstance(body, dict) and body.get('device') == str(dev):
        return body
    return None


async def get_p50_home(mqtt, s, inbox, dev=P50):
    """兼容旧调用"""
    return await get_home(mqtt, s, inbox, dev)


async def get_frames(mqtt, s, inbox, dev):
    body = await read_sid(mqtt, s, inbox, f'gateway/ntwk/childFrame?devid={dev}&scene=0')
    return (body or {}).get('frames', []) if isinstance(body, dict) else []


async def get_frame(mqtt, s, inbox, fid, dev=P50):
    for f in await get_frames(mqtt, s, inbox, dev):
        if f['id'] == str(fid): return f
    return None


async def get_apps(mqtt, s, inbox, typ, dev=P50):
    body = await read_sid(mqtt, s, inbox, f'gateway/ntwk/childModelApps?type={typ}&devid={dev}&scene=0')
    return (body or {}).get('apps', []) if isinstance(body, dict) else None


async def get_catalog(mqtt, s, inbox, dev):
    """应用目录 {appInfo:[{id,categ,plat}]}"""
    body = await read_sid(mqtt, s, inbox, f'gateway/ntwk/childAppList?devid={dev}&scene=0')
    return (body or {}).get('appInfo', []) if isinstance(body, dict) else []


async def game_catalog_ids(mqtt, s, inbox, dev):
    return [str(a['id']) for a in await get_catalog(mqtt, s, inbox, dev) if a.get('categ') == 4]


async def get_client_list(mqtt, s, inbox):
    """路由器全量连接客户端（在线+离线历史，含未建档设备）。
    来源：gateway/system/HostInfo?filterAndroid=true&isSupportHostZip=true
    （App Devices 页同一数据通道，2026-10-11 Frida hook App MQTT publish 实锤）。
    body 为主机记录数组：MACAddress/HostName/IPAddress/InterfaceType/Frequency/
    Active/IconType/DevBrands/IsGuest/AccessRecord/OfflineRecord/TxKBytes/RxKBytes..."""
    body = await read_sid(mqtt, s, inbox, 'gateway/system/HostInfo?filterAndroid=true&isSupportHostZip=true')
    return body if isinstance(body, list) else []


# ---------- 写入原语 ----------

async def set_games_master(mqtt, s, inbox, dev, deny):
    """游戏总开关（type=0）。开=denyAll:1+全量名单；关=denyAll:0+空名单。"""
    apps = await game_catalog_ids(mqtt, s, inbox, dev) if deny else []
    body = {"data": {"denyAll": 1 if deny else 0, "type": MASTER_TYPE,
                     "device": str(dev), "apps": apps, "scene": 0}, "action": "update"}
    return await pub(mqtt, s, CMA, body, inbox)


async def set_category_list(mqtt, s, inbox, dev, typ, apps, deny_all=1):
    """写分类名单（整表覆盖）。typ: 2 视频 3 社交 4 游戏单独禁止…"""
    body = {"data": {"denyAll": deny_all, "type": typ, "device": str(dev),
                     "apps": [str(a) for a in apps], "scene": 0}, "action": "update"}
    return await pub(mqtt, s, CMA, body, inbox)


async def frame_add(mqtt, s, inbox, dev, time_from, time_to, days=None, enable=1):
    """加允许时段，返回 (errcode, 新帧id或None)。days: 缺省全周。"""
    flags = {d: (1 if days is None else 1 if d in days else 0) for d in DAYS}
    frame = {"id": "", "device": str(dev), "timeFrom": time_from, "timeTo": time_to,
             "today": 0, "enable": enable, "scene": 0, **flags}
    ec = await pub(mqtt, s, CF, {"data": frame, "action": "newCreate"}, inbox)
    newf = None
    for f in await get_frames(mqtt, s, inbox, dev):
        if f['timeFrom'] == time_from and f['timeTo'] == time_to:
            newf = f['id']
    return ec, newf


async def frame_set_enable(mqtt, s, inbox, dev, fid, enable):
    """禁用/启用时段帧。newUpdate 是整帧覆盖：先读真实字段再只改 enable。"""
    cur = await get_frame(mqtt, s, inbox, fid, dev)
    if not cur:
        return 'no-frame', None
    ec = await pub(mqtt, s, CF, {"data": dict(cur, device=str(dev), scene=0, enable=enable),
        "action": "newUpdate"}, inbox)
    return ec, cur
