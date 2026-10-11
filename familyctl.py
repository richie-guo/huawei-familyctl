#!/usr/bin/env python3
"""familyctl——华为路由器儿童上网管控统一 CLI（设备无关，多设备通用）。

设备参数 <dev> 支持 序号(8) 或名称模糊匹配(P50/ipad/学习机)，见 devices 子命令。
所有写操作带 errcode 检查；改名单/总开关后自动读回核验。

用法:
  familyctl.py devices                          # 全部受控设备（管控档案）
  familyctl.py clients                          # 路由器全量连接设备（MQTT 直查 gateway/system/HostInfo，秒级）
  familyctl.py status <dev>                     # 单设备管控全貌
  familyctl.py games <dev> on|off|status        # 游戏总开关（type=0 真协议）
  familyctl.py appmask <dev> <games|video|social|typeN> list|add <id...>|remove <id...>
  familyctl.py frames <dev>                     # 时段列表（标注当前生效）
  familyctl.py frame-off <dev> <id> [id...]     # 禁用时段帧
  familyctl.py frame-on  <dev> <id> [id...]     # 启用时段帧
  familyctl.py frame-add <dev> <HH:MM> <HH:MM> [--days sun,mon,...]  # 缺省全周
  familyctl.py grant <dev> <分钟>               # 临时放行游戏N分钟，到点自动恢复
  familyctl.py cancel-grant <dev>               # 提前收回授予
"""
import asyncio, json, re, subprocess, sys, time
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from p50_lib import (get_session, with_mqtt, list_devices, resolve_dev, get_home, get_client_list,
                     get_frames, get_frame, get_apps, game_catalog_ids,
                     set_games_master, set_category_list, frame_add, frame_set_enable,
                     CATEGORY)

STATE = HERE / 'state'
DAY_ALIAS = {'sun': 'sunday', 'mon': 'monday', 'tue': 'tuesday', 'wed': 'wednesday',
             'thu': 'thursday', 'fri': 'friday', 'sat': 'saturday'}

# ---------- 模拟器遗留工具（clients 已改 MQTT 直查，以下仅 adb 辅助保留） ----------

EMU_CONTAINER = 'phonics-emu'
EMU_AVD = 'phonics3'
ADB = ['docker', 'exec', EMU_CONTAINER, '/sdk/platform-tools/adb']


def _adb(*args, timeout=30):
    return subprocess.run(ADB + list(args), capture_output=True, text=True, timeout=timeout).stdout


def _dump_texts():
    """uiautomator dump 并按顺序返回可见文本节点（失败返回空表）"""
    subprocess.run(ADB + ['shell', 'rm', '-f', '/sdcard/ui.xml'], capture_output=True)
    r = subprocess.run(ADB + ['shell', 'uiautomator', 'dump', '/sdcard/ui.xml'],
                       capture_output=True, text=True, timeout=60)
    if 'dumped' not in (r.stdout + r.stderr):
        return []
    xml = _adb('shell', 'cat', '/sdcard/ui.xml')
    return re.findall(r'text="([^"]+)"', xml)


async def cmd_clients_mqtt(mqtt, s, inbox):
    """全量连接客户端（在线+离线历史，含未建档）——MQTT 直查
    gateway/system/HostInfo（App Devices 页同一数据通道，无需模拟器）"""
    hosts = await get_client_list(mqtt, s, inbox)
    online = [h for h in hosts if h.get('Active')]
    offline = [h for h in hosts if not h.get('Active')]
    print(f"共 {len(hosts)} 台客户端（在线 {len(online)} / 离线 {len(offline)}）")
    for tag, group in (("在线", online), ("离线", offline)):
        if not group: continue
        print(f"-- {tag} --")
        for h in group:
            name = h.get('ActualName') or h.get('HostName') or h.get('IconType') or '(未命名)'
            ip = h.get('IPAddress', '-')
            band = h.get('InterfaceType') or h.get('Frequency') or '-'
            brand = h.get('DevBrands', '')
            guest = ' [访客]' if h.get('IsGuest') else ''
            print(f"  {name:<26} {ip:<14} {band:<7} {brand:<10} {h.get('MACAddress','')}{guest}")


async def cmd_devices(mqtt, s, inbox):
    for d in await list_devices(mqtt, s, inbox):
        online = '在线' if d.get('active') else '离线'
        print(f"device={d.get('device'):>2}  {d.get('hostName','?'):<22} {d.get('actualName',''):<24} "
              f"{online}  游戏全禁={'是' if d.get('gameDenyCount', 0) > 0 else '否'}"
              f"(gameDenyCount={d.get('gameDenyCount')})")


async def cmd_status(mqtt, s, inbox, dev, rec):
    home = await get_home(mqtt, s, inbox, dev) or rec or {}
    frames = await get_frames(mqtt, s, inbox, dev)
    cat = len(await game_catalog_ids(mqtt, s, inbox, dev))
    n = home.get('gameDenyCount', 0)
    print(f"{home.get('hostName','?')} (device={dev}, {home.get('actualName','')}, "
          f"{'在线' if home.get('active') else '离线'})")
    print(f"游戏总开关: {'全禁' if n >= cat and cat else '未全禁'}"
          f"（gameDenyCount={n}/目录{cat}）")
    for name, typ in (('游戏单独禁止', 4), ('视频', 2), ('社交', 3)):
        apps = await get_apps(mqtt, s, inbox, typ, dev)
        print(f"{name}名单(type={typ}): {len(apps) if apps is not None else '读取失败'}款")
    hit, f = in_window(frames)
    print(f"时段: 共{len(frames)}条，当前{'在✅' if hit else '不在'}允许时段"
          + (f"（{f['timeFrom']}-{f['timeTo']}）" if hit else ""))
    for fr in sorted(frames, key=lambda x: int(x['id'])):
        days = ''.join(zh[0] for zh, en in
                       [('日', 'sunday'), ('一', 'monday'), ('二', 'tuesday'), ('三', 'wednesday'),
                        ('四', 'thursday'), ('五', 'friday'), ('六', 'saturday')] if fr.get(en) == 1)
        mark = ' ←当前生效' if hit and fr is f else ''
        print(f"  id={fr['id']:>2} {fr['timeFrom']}-{fr['timeTo']} "
              f"{'启用' if fr['enable'] else '停用'} 周{days}{mark}")


async def cmd_games(mqtt, s, inbox, dev, action):
    if action == 'status':
        home = await get_home(mqtt, s, inbox, dev) or {}
        cat = len(await game_catalog_ids(mqtt, s, inbox, dev))
        n = home.get('gameDenyCount', 0)
        print(f"{'全禁' if n >= cat and cat else '未全禁'}（gameDenyCount={n}/目录{cat}）")
        return
    deny = action == 'on'
    ec = await set_games_master(mqtt, s, inbox, dev, deny)
    home = await get_home(mqtt, s, inbox, dev) or {}
    n = home.get('gameDenyCount', '?')
    ok = ec == '0' and ((deny and n and n > 0) or (not deny and n == 0))
    print(f"游戏总开关={'开(全禁)' if deny else '关(放行)'} errcode={ec} "
          f"读回gameDenyCount={n} {'✅' if ok else '⚠️ 未确认'}")


async def cmd_appmask(mqtt, s, inbox, dev, cat, action, ids):
    typ = CATEGORY.get(cat, None)
    if typ is None:
        try: typ = int(cat)
        except ValueError:
            print(f"未知分类 {cat}，可选: games/video/social 或数字type"); sys.exit(2)
    cur = await get_apps(mqtt, s, inbox, typ, dev) or []
    if action == 'list':
        print(f"type={typ} 名单 {len(cur)}款: {cur}")
        return
    cur = [str(a) for a in cur]
    if action == 'add':
        new = cur + [i for i in ids if i not in cur]
    elif action == 'remove':
        new = [i for i in cur if i not in ids]
    else:
        print(f"未知动作 {action}"); sys.exit(2)
    if len(new) > 23 and typ == 4:
        print(f"⚠️ 服务端名单上限 23 款（errcode=9003），当前将写入 {len(new)} 款")
    ec = await set_category_list(mqtt, s, inbox, dev, typ, new)
    back = await get_apps(mqtt, s, inbox, typ, dev) or []
    ok = ec == '0' and sorted(str(a) for a in back) == sorted(new)
    print(f"type={typ} 名单 {len(cur)}→{len(new)}款 errcode={ec} 读回{len(back)}款 {'✅' if ok else '⚠️ 不一致'}")


async def cmd_frames(mqtt, s, inbox, dev):
    await cmd_status(mqtt, s, inbox, dev, None)


async def cmd_frame_toggle(mqtt, s, inbox, dev, fids, enable):
    for fid in fids:
        ec, cur = await frame_set_enable(mqtt, s, inbox, dev, fid, enable)
        after = await get_frame(mqtt, s, inbox, fid, dev)
        ok = ec == '0' and after and after['enable'] == enable
        print(f"id={fid} {'启用' if enable else '停用'} errcode={ec} "
              f"读回enable={after['enable'] if after else '?'} {'✅' if ok else '⚠️'}")


async def cmd_frame_add(mqtt, s, inbox, dev, t1, t2, day_args):
    days = None
    if day_args:
        days = set()
        for a in day_args.split(','):
            a = a.strip().lower()
            full = DAY_ALIAS.get(a)
            if full is None and a in DAY_ALIAS.values():
                full = a
            if full is None:
                print(f"非法星期: {a}（可用 sun,mon,tue,wed,thu,fri,sat 或全称）"); sys.exit(2)
            days.add(full)
    ec, fid = await frame_add(mqtt, s, inbox, dev, t1, t2, days)
    print(f"加时段 {t1}-{t2} 帧 id={fid} errcode={ec} "
          f"{'✅' if ec == '0' and fid else '⚠️ 未读到新帧'}")


# ---------- grant：临时放行游戏 N 分钟 ----------

def _meta_path(dev): return STATE / f'grant_{dev}.json'


async def do_grant_writes(mqtt, s, inbox, dev, minutes):
    home = await get_home(mqtt, s, inbox, dev) or {}
    pre_count = home.get('gameDenyCount', 0)
    pre_list = [str(a) for a in (await get_apps(mqtt, s, inbox, 4, dev) or [])]
    cat = await game_catalog_ids(mqtt, s, inbox, dev)
    now = datetime.now()
    end = now + timedelta(minutes=minutes)
    print(f"授予前: gameDenyCount={pre_count}(目录{len(cat)}) type=4名单{len(pre_list)}款")

    ec = await set_games_master(mqtt, s, inbox, dev, False)
    print(f"① 总开关关 errcode={ec}");  assert ec == '0', '总开关关闭失败'
    ec = await set_category_list(mqtt, s, inbox, dev, 4, [])
    print(f"② 清单独禁止名单 errcode={ec}");  assert ec == '0', '清名单失败'
    ec, fid = await frame_add(mqtt, s, inbox, dev, now.strftime('%H:%M'), end.strftime('%H:%M'))
    print(f"③ 加允许时段 {now:%H:%M}-{end:%H:%M} 帧 id={fid} errcode={ec}")
    assert ec == '0' and fid, '加时段失败'

    meta = {'dev': dev, 'fid': fid, 'pre_list': pre_list, 'pre_count': pre_count,
            'cat_len': len(cat), 'end': end.strftime('%F %T')}
    STATE.mkdir(exist_ok=True)
    _meta_path(dev).write_text(json.dumps(meta, ensure_ascii=False))
    return meta


async def restore_grant_sync(mqtt, s, inbox, dev):
    meta = json.loads(_meta_path(dev).read_text())
    ec, _ = await frame_set_enable(mqtt, s, inbox, dev, meta['fid'], 0)
    print(f"[restore] 停用帧 id={meta['fid']} errcode={ec}")
    ec = await set_category_list(mqtt, s, inbox, dev, 4, meta['pre_list'])
    print(f"[restore] 名单回写{len(meta['pre_list'])}款 errcode={ec}")
    pre, clen = meta['pre_count'], meta['cat_len']
    if pre >= clen and clen:
        ec = await set_games_master(mqtt, s, inbox, dev, True)
        print(f"[restore] 总开关恢复全禁 errcode={ec}")
    elif pre > 0:
        print(f"[restore] ⚠️ 授予前总开关为部分禁用(gameDenyCount={pre}/{clen})，无法精确还原；"
              f"按安全侧处理：恢复全禁")
        ec = await set_games_master(mqtt, s, inbox, dev, True)
        print(f"[restore] 总开关全禁 errcode={ec}")
    else:
        print("[restore] 授予前总开关为放行，保持放行")
    home = await get_home(mqtt, s, inbox, dev) or {}
    print(f"[restore] 终态 gameDenyCount={home.get('gameDenyCount')}")
    _meta_path(dev).unlink(missing_ok=True)


async def cmd_grant(mqtt, s, inbox, dev, minutes):
    if _meta_path(dev).exists():
        print(f"设备 {dev} 已有进行中的授予（{_meta_path(dev).name}），先 cancel-grant"); sys.exit(2)
    meta = await do_grant_writes(mqtt, s, inbox, dev, minutes)
    restorer = STATE / f'grant_restore_{dev}.py'
    restorer.write_text(
        "import sys, time\n"
        f"time.sleep({minutes * 60 + 15})\n"
        f"sys.path.insert(0, r'{HERE}')\n"
        "import asyncio\n"
        "from p50_lib import get_session, with_mqtt\n"
        f"from familyctl import restore_grant_sync\n"
        "async def run():\n"
        "    s = await get_session()\n"
        "    mqtt, inbox = await with_mqtt(s)\n"
        f"    await restore_grant_sync(mqtt, s, inbox, '{dev}')\n"
        "    await mqtt.async_stop()\n"
        "asyncio.run(run())\n")
    log = open(STATE / f'grant_restore_{dev}.log', 'a')
    pid = subprocess.Popen([sys.executable, str(restorer)], stdout=log, stderr=log,
                           start_new_session=True).pid
    meta['pid'] = pid
    _meta_path(dev).write_text(json.dumps(meta, ensure_ascii=False))
    print(f"✅ 游戏 已放行 {minutes} 分钟（至 {meta['end']}），到点自动恢复；"
          f"恢复进程 pid={pid}，提前收回: familyctl.py cancel-grant {dev}")


async def cmd_cancel_grant(mqtt, s, inbox, dev):
    if not _meta_path(dev).exists():
        print(f"设备 {dev} 无进行中的授予"); return
    meta = json.loads(_meta_path(dev).read_text())
    pid = meta.get('pid')
    if pid:
        subprocess.run(['pkill', '-f', f'grant_restore_{dev}.py'], capture_output=True)
        print(f"已终止恢复进程(pid={pid} 若仍在)")
    await restore_grant_sync(mqtt, s, inbox, dev)
    print("✅ 授予已提前收回")


async def main():
    a = sys.argv[1:]
    if not a or a[0] in ('-h', '--help', 'help'):
        print(__doc__); return
    cmd, rest = a[0], a[1:]
    s = await get_session()
    mqtt, inbox = await with_mqtt(s)
    try:
        if cmd == 'devices':
            await cmd_devices(mqtt, s, inbox); return
        if cmd == 'clients':
            await cmd_clients_mqtt(mqtt, s, inbox); return
        need_dev = cmd not in ()
        if need_dev and not rest:
            print(f"缺少设备参数，用法见 help"); sys.exit(2)
        records = await list_devices(mqtt, s, inbox)
        if cmd == 'grant':
            dev, _ = pick(records, rest[0]); await cmd_grant(mqtt, s, inbox, dev, int(rest[1])); return
        if cmd == 'cancel-grant':
            dev, _ = pick(records, rest[0]); await cmd_cancel_grant(mqtt, s, inbox, dev); return
        dev, rec = pick(records, rest[0])
        if cmd == 'status': await cmd_status(mqtt, s, inbox, dev, rec)
        elif cmd == 'games':
            if len(rest) < 2 or rest[1] not in ('on', 'off', 'status'):
                print("用法: games <dev> on|off|status"); sys.exit(2)
            await cmd_games(mqtt, s, inbox, dev, rest[1])
        elif cmd == 'appmask':
            # appmask <dev> <cat> list | appmask <dev> <cat> add|remove <id...>
            if len(rest) < 3:
                print("用法: appmask <dev> <games|video|social|typeN> list|add|remove [id...]"); sys.exit(2)
            await cmd_appmask(mqtt, s, inbox, dev, rest[1], rest[2], rest[3:])
        elif cmd == 'frames': await cmd_frames(mqtt, s, inbox, dev)
        elif cmd == 'frame-off': await cmd_frame_toggle(mqtt, s, inbox, dev, rest[1:], 0)
        elif cmd == 'frame-on': await cmd_frame_toggle(mqtt, s, inbox, dev, rest[1:], 1)
        elif cmd == 'frame-add':
            days = None
            if '--days' in rest:
                i = rest.index('--days'); days = rest[i + 1]; rest = rest[:i] + rest[i + 2:]
            if len(rest) < 3:
                print("用法: frame-add <dev> <HH:MM> <HH:MM> [--days sun,mon]"); sys.exit(2)
            await cmd_frame_add(mqtt, s, inbox, dev, rest[1], rest[2], days)
        else:
            print(f"未知命令 {cmd}"); sys.exit(2)
    finally:
        await mqtt.async_stop()


if __name__ == '__main__':
    asyncio.run(main())
