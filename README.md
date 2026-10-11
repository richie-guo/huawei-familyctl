# huawei-familyctl

华为路由器「儿童上网保护」命令行管控工具——自然语言友好、多设备、免模拟器、免路由器管理密码。

通过华为智慧生活云端 MQTT 信封协议直查/直控路由器儿童管控功能，支持：

- **受控设备档案**：列出所有纳入管控的设备与在线状态（`devices`）
- **全量连接客户端**：含未纳管设备，在线/离线分组，名称/IP/频段/品牌/MAC（`clients`）
- **游戏总开关**：一键全禁/放行所有游戏（逆向出的 `type=0` 真协议，`games on|off`）
- **分类应用名单**：游戏/视频/社交等分类的单独禁用名单增删查（`appmask`）
- **上网时段**：允许时段的增/停用/启用的全生命周期管理（`frames` / `frame-*`）
- **限时授予**：`grant <设备> <分钟>` 一条命令放行游戏 N 分钟，到点自动恢复原状
- **E2E 密码学栈复刻**：华为 hichain SPEKE 与 STS 协议的纯 Python 实现+自测（`e2e/`）

## 用法速览

```bash
python3 familyctl.py devices                    # 全部受控设备
python3 familyctl.py status <设备名或序号>        # 单设备全貌（开关/名单/时段/当前窗口）
python3 familyctl.py games <dev> on|off         # 游戏总开关
python3 familyctl.py appmask <dev> video add 336 # 分类名单操作
python3 familyctl.py frames <dev>               # 时段列表
python3 familyctl.py frame-add <dev> 20:00 21:00 --days sat,sun
python3 familyctl.py grant <dev> 30             # 放行游戏 30 分钟（自动恢复）
python3 familyctl.py cancel-grant <dev>         # 提前收回
python3 familyctl.py clients                    # 全量连接设备（在线+历史离线）
```

设备参数支持序号或名称模糊匹配（`p50` / `ipad` / `学习机` 均可）。

## 环境准备

1. Python 3.10+，`pip install cryptography`（仅 e2e/ 自测需要）
2. 克隆依赖的华为云协议库到本目录：
   ```bash
   git clone https://github.com/xiasi0/ha-huawei-smarthome ha-huawei-smarthome
   ```
3. 生成登录态（华为账号密码 + 二次验证，产物 `state/session.pkl`）：
   ```bash
   python3 poc_cloud_login.py begin <账号> <密码>
   python3 poc_cloud_login.py complete <验证码>   # 短信/邮箱验证码
   python3 poc_cloud_login.py list               # 确认能看到你的路由器
   ```
4. 复制 `config.example.json` 为 `config.json`，填入你的 `router_dev_id`（路由器云端设备 UUID）等。

之后所有命令自动刷新 token，无需重复登录。

## 文件说明

| 文件 | 说明 |
|---|---|
| `familyctl.py` | 统一 CLI（10 个子命令） |
| `p50_lib.py` | 协议原语库（MQTT 信封/服务读写/配置），协议要点见文件头注释 |
| `poc_cloud_login.py` | 账号登录与登录态管理（begin/complete/list/detail/mqtt/listen） |
| `relay_probe.py` | HAG 子系统命令中继探针（`/smart-life/v3/.../subsystem/instance/{id}/command`） |
| `e2e/` | hichain SPEKE 与 STS 协议纯 Python 复刻 + 自测（`python3 e2e/test_self.py`） |
| `PROTOCOL.md` | 逆向发现的协议要点汇总 |

## 安全与隐私

- `config.json`、`state/`（含登录 token）均在 `.gitignore` 中，**切勿提交或分享**
- 所有写操作带 errcode 检查与读回核验；`grant` 授予前自动快照、到期按快照恢复
- 本工具仅应操作**你自己的**路由器与**你监护的**设备

## 免责声明

本项目为个人设备互操作性研究产物，与华为官方无关；协议为静态/动态逆向所得，随时可能
因固件/云端变更失效。使用风险自负，请遵守当地法律法规。
