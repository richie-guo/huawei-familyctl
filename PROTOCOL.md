# 协议要点（逆向发现汇总）

> 来自对智慧生活 App 的静态反编译与流量层验证（2026-10）。仅供互操作研究参考。

## 1. 云端 MQTT 信封

- Broker：MQTT over TLS（8883），登录后以 OAuth access token 为密码
- 命令主题：`/smartHome/signaltrans/v2/categories/command`；响应回到用户主题（共享）
- 信封：`header{mode:ACK, method:GET/POST, requestId, from:/users/{uid}, ctrlSrc, to, accessToken, ctrlType:1, timestamp}` + `body`
- `to` 即服务地址：`/devices/{路由器devId}/services/{服务sid}`；**GET/POST 的语义由路由器插件解释**

## 2. 网关服务目录（实测可用）

| 服务 sid | 作用 |
|---|---|
| `gateway/ntwk/childHomepage` | 儿童管控主页（全部受控设备状态+时段+计数影子） |
| `gateway/ntwk/childFrame` | 上网时段规则（newCreate/newUpdate/delete*） |
| `gateway/ntwk/childModelApps?type=N` | 分类名单（type=2 视频 / 3 社交 / 4 游戏单独禁用） |
| `gateway/ntwk/childAppControl` | 单设备应用控制（六分类 denyAll 开关+apps） |
| `gateway/app/router_card_info` | 路由器卡片信息 |
| `gateway/device/hostmap` / `device/topology` | 设备拓扑 MAC 图 |
| `gateway/system/HostInfo?filterAndroid=true&isSupportHostZip=true` | **全量连接客户端表**（在线+离线历史，名称/IP/频段/品牌/上下线记录） |
| `gateway/system/get_hilink_ap_list` | 路由器自身 AP 清单（注意：不是客户端表） |
| `gateway/system/onlinestate` | 在线状态 |
| `gateway/system/netInfo` | 信号信息 |

注意：`child*` 与 `HostInfo` 等**云端读数是影子层**，与路由器本地真值存在滞后/派生差异；
以名单类端点（childModelApps/HostInfo）直读为准。

## 3. 游戏总开关真协议（type=0）

```
POST gateway/ntwk/childModelApps
body: {"data":{"denyAll":1,"type":0,"device":"<dev>","apps":[<目录全量游戏ID>],"scene":0},"action":"update"}   # 全禁
     {"data":{"denyAll":0,"type":0,"device":"<dev>","apps":[],"scene":0},"action":"update"}                  # 放行
```

- 目录：`gateway/ntwk/childAppList?devid=<dev>`（id+categ 位掩码：4=游戏 16=视频 …）
- **开启必须带全量 ID 列表**；`denyAll:1 + apps:[]` 无效
- 状态判读：`childHomepage.gameDenyCount`（0=未禁 / 目录总数=全禁）

## 4. 上网时段

- 创建：`POST childFrame {"data":{id:"",timeFrom,timeTo,全周标志,today:0,enable,scene,device},"action":"newCreate"}`
- 禁用：`newUpdate` **整帧覆盖**（必须带实读字段，只改 enable）；delete 端点不可用
- 时段是白名单语义；分类开关/名单与时段是独立的两层锁

## 5. HAG 子系统命令中继（REST，实验性）

```
POST {cloud}/smart-life/v3/{homeId}/subsystem/instance/{instanceId}/command
body: {"requestId":uuid,"timestamp":ms,"sid":"gateway/...","data":{...}}
headers: Authorization: Bearer <oauth> + X-AppId/X-Huid + x-e2e-traceid/x-control-*
```
鉴权与 OAuth 会话直通；GATEWAY 节点（路由器）的主命令面仍走 MQTT。

## 6. hichain SPEKE（设备绑定/配网）

```
hashPin   = HKDF-SHA256(pin8字节, salt, "hichain_speke_base_info\0")
pubA      = (hashPin² mod p)^a mod p        # p = RFC5054 8192-bit 群
sharedKey = HKDF-SHA256(公钥幂结果.toByteArray()[1:], salt, "hichain_speke_sessionkey_info\0")
会话加密  = AES-GCM：0x00 || nonce12 || ct||tag16
```
Java `BigInteger(byte[])` 带符号语义是互操作的主要陷阱。

## 7. STS（X25519+Ed25519，对应档案 stsSession）

三轮 JSON 消息 `{publicKey,salt,extra}` → `{data,signature,extra}`；
X25519 ECDH → HKDF-SHA256 → 16B 会话密钥，AES-GCM 信封同上。
