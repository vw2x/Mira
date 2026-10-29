# TODO

## 当前状态

Mira 当前 v1.0.0 是研究原型和参考实现, 重点不是做完整企业风控平台, 而是证明关键想法可行: AI 可以通过 MCP(Model Context Protocol, 模型上下文协议)进入 Android(安卓系统) 和 iOS(苹果移动系统) 的 App 沙盒视角, 读取 Skill(技能), 观察所有可能不对劲的行为, 并沉淀成本地 Case(案例)。早期 Web Terminal(网页终端) 到 PTY(伪终端) 的本机原型只作为 bridge(连接层)调试入口保留。

## 接下来一周目标

### 当前推进: iOS iSH 升级

- [x] 固定升级 iSH commit, 保留 Android 配置、Termux、Frida 16.0.7 和现有 Python/rootfs.
- [x] 修复新上游 mmap 地址提示回归, 补充修补前失败、修补后通过的行为测试.
- [x] 修复真机验收暴露的既有 TTY 长输入阻塞, 宿主线程回归及真机 8,433 字节脚本投递通过.
- [x] 以源码/工具链/参数/产物指纹判断 iSH 库缓存, 失败构建不复用旧标记.
- [x] rootfs 来源独立固定; App stamp 变化时保留已安装环境, 首次安装原子落位.
- [x] iSH arm64 库构建及真实设备 destination 的签名 App 构建通过.
- [x] iPhone X / iOS 16.7.15 完成两次覆盖安装, PTY/Python/Frida、26,294 个 ObjC 类名、256 KiB RPC、四项 syscall 及 rootfs 标记变化后的数据保留验收; 本地 70 项测试通过. 详见 [iSH 升级记录](../IOS-ISH-UPGRADE.md).

其他候选方向保留待后续逐项评估: Termux 上游可用修复、Relay/会话稳定性、系统兼容性矩阵、构建自动化、iOS 并发检查与 iSH 汇编对齐警告. Android 版本号和 Frida 17 暂不推进; APK 虚拟环境 trace 探索暂缓.

### 原有阶段目标

本周目标不是演示完整企业链路, 而是把 Mira 作为 research prototype(研究原型)和 reference implementation(参考实现)讲清楚, 并完成最小证明。

一周结束时应能表达清楚:

1. Mira 当前 App 是 demo app(演示应用), 让使用者代入自己的业务 App 场景。
2. Mira 最终更自然的形态是 Android library(Android 库), iOS library(iOS 库)或 SDK(软件开发工具包)。
3. 企业自己的情报系统, 工单系统, 服务端策略, 代码仓库, 多模型 review(审查), CI/CD(持续集成和持续交付), 灰度发布和回滚由企业自己接入。
4. Mira 只实现其中最关键的一段: 端侧沙盒观察 + MCP 接入 + Skill 分析 + 本地 Case 沉淀。
5. 这个项目首先用于学习, 研究和方法沉淀。思路比实现更重要, 但实现要足够小而可运行。

## 已完成基线

1. Android APK(安卓安装包): 已支持 Relay URL(中继地址)连接, control WebSocket(控制长连接), 按需打开真实 PTY, 会话级 BusyBox(单文件工具集)释放和四 ABI(应用二进制接口)工具箱。
2. iOS App(苹果移动应用): 已支持 Relay 连接, PTY 会话, Mira App 自身 key window(主窗口)画面上传, CPU(中央处理器), 内存和网络速率指标, 浏览器侧快捷输入。
3. Relay Console(中继控制台): 已支持设备大厅, 三栏式工作台, 远程终端, App 画面预览, 状态展示和指标展示。
4. 公网启动: `./mira-web` 已支持自动启动 Relay 并接入 cpolar(国内内网穿透服务), 也支持 `MIRA_PUBLIC_URL` 复用外部公网地址。
5. 局域网启动: `./mira-local-web` 已支持浏览器使用 localhost(本机地址), 手机使用电脑局域网 IP(网络地址)。
6. CLI(Command Line Interface, 命令行接口): 已支持 `mira-cli devices`, `mira-cli run` 和 `mira-cli shell`。
7. MCP Server(模型上下文协议服务端): 已支持设备列表, 打开终端, 执行命令, 读取输出, 快照采集, 关闭会话, resources(资源)和 prompts(提示模板)。
8. Mobile Risk Skill(移动风险技能): 已新增 `skills/mira-mobile-risk-review`, 默认 observation-first(现象优先), 记录所有可能不对劲的行为, 不做风险等级压制。

## 当前验证边界

1. Android 侧当前仅在 Android 13 设备上完成主路径验证, 其他系统版本和机型还没有系统回归.
2. iOS 侧当前主要在 iPhone X 设备上验证通过, 其他机型尤其是更早和更新的机型仍缺少覆盖.
3. 当前跨设备适配的主要卡点集中在 Frida(动态插桩工具) runtime(运行时) 兼容性, 不是所有问题都来自 Mira 自身会话链路.
4. 当前使用的 Frida 16.0.7 还不能稳定覆盖古早机型和较新机型, 后续需要持续维护版本策略和适配矩阵.

## 下一阶段方向

1. 设计 built-in agent(内置智能体) 模式, 支持服务端按策略下发分析任务, 配置版本和提示词模板.
2. 在 App 内实现最小 agent loop(智能体循环), 先聚焦单次自动分析, 不默认做常驻后台任务.
3. 定义自动分析的 report schema(报告结构) 与 evidence bundle(证据包) 格式, 让上报结果可复查, 可降级和可灰度.

## P0-必须完成

1. 收敛 README(项目说明文档)首屏定位, 明确 Mira 是研究原型和参考实现, 不是完整企业风控平台。
2. 写清楚 demo app(演示应用)和未来 library(库)或 SDK(软件开发工具包)形态的关系, 让使用者能代入自己的业务 App 场景。
3. 更新 MCP prompt(提示模板), 让 Codex 分析风险环境前先读取 `skills/mira-mobile-risk-review/SKILL.md`。
4. 增加 case 生成命令或脚本, 默认写入 `cases/<githubName>-<yyyymmdd>-<topic>/`, 不默认提交, 不默认公开。
5. 补充 iOS `/mira` 和 `/mira/proc` 文档, 明确它们是 App 视角投影和 Mira 模拟进程视图, 不是系统真实根目录或内核 procfs(进程文件系统)。
6. 补充 Android 当前进程观察指南, 首批聚焦 `/proc/self/maps`, `/proc/self/status`, `/proc/self/mountinfo`, `/proc/self/fd` 和 SELinux(Security-Enhanced Linux, 安全增强 Linux)上下文。
7. 补一份最小证明脚本, 只串起 Mira MCP, Skill 读取, Android `/proc/self/*` 或 iOS `/mira` 观察, 以及本地 case 输出, 不演示企业工单, PR(Pull Request, 代码合并请求), CI/CD 或灰度发布。
8. 将公开演示安全边界, 项目定位和企业接入设想沉淀为 README 小节和发布文章草稿。

## P1-探测强度提升

1. 建立设备和系统版本兼容性矩阵, 至少区分 Android 版本, iPhone 机型, iOS 版本, Frida 版本和当前验证结果。
2. 持续维护 Frida(动态插桩工具) 版本适配策略, 重点补齐古早机型和较新机型上的可用组合。
3. 将 Frida 兼容性问题与 Mira 自身 shell, Relay 和 workbench(工作台) 链路问题拆开记录, 避免混淆根因。
4. 基于《Android 风险环境检测》这类珍贵资料, 持续整理模拟器, 云手机, 改机, Hook(运行时劫持), 调试, 注入和系统接口异常的 risk observation(风险观察)条目。
5. 为每个风险观察补充系统原理, App 沙盒可见路径, 可能被 Hook 的层级和下一步交叉验证方式。
6. 将高层 API(Application Programming Interface, 应用程序编程接口)观察逐步补充到 native bridge(原生桥接层), syscall(系统调用)包装和更贴近运行时底层的观察点。
7. Android 侧探索签名校验后的 native probe(原生探针)组件, 包括带 inline assembly(内联汇编) 的自有 libc-compatible shared object(兼容 libc 语义的共享库), 用来交叉验证高层 API 返回值。
8. 封装更多系统提供给第三方 App 的公开接口, 例如 battery state(电池状态), charging power(充电功率), thermal state(温控状态), network path(网络路径), sensor availability(传感器可用性)和进程自身运行时状态。
9. 梳理哪些能力未来应该从 demo app 抽象到 Android library, iOS library 或 SDK, 先写边界和接口草案, 不急着做完整商业化封装。

## P2-产品化和自动化

### Android Java 架构整理

- [x] 将屏幕推流的纯数据逻辑提取到 `com.vwww.mira.screen`: `AvcBitstream` 负责 AVC 转换和 SPS, `ScreenVideoPacket` 负责 MHS1 编码. Android 编码器和连接生命周期继续由调用方管理. 用宿主 JVM 测试固定码流与协议字节, 命令: `python3 -m unittest discover -s tests -p 'test_screen_protocol.py'`.
- [x] 屏幕模块迁入 `screen` 包: `AppScreenStreamer` 负责推流生命周期, `AppScreenCapture` 负责宿主 App 画面与输入, 包内 `AvcEncoderSelector` 负责候选枚举和成功配置缓存, `AvcEncoderProfile` 保存编码配置. 保留原缓存 key、候选顺序、等待规则与宿主 App 画面范围; Android Service/MainActivity 通过公开入口调用, Application 注入轮廓刷新回调以避免采集类反向依赖具体 Service; 码流和配置实现不对包外暴露. 回归命令: `python3 -m unittest discover -s tests -p 'test_screen_*.py'`.
- [x] 运行服务更名 `MiraRuntimeService`, 同步 Manifest 和调用方并保留原 action/extras 字符串. `discovery/LanDiscoveryServer` 通过回调完成 UDP 发现与 HTTP 唤醒, 自主管理 socket、客户端和 multicast lock; 包内 `HttpRequestParser` 负责有界协议读写. close 后实例不可复用, runtime 每次启动创建新实例; 跨组件关闭不嵌套持有 runtime/server 锁. 回归命令: `python3 -m unittest tests.test_android_discovery -v`.
- [x] 命令模块迁入 `command` 包: `LocalCommandServer` 提供本机 socket 入口, `RemoteCommandHandler` 负责远端命令校验和结果返回, dispatcher/protocol/result/process 实现保持包内可见. 保留 same-UID 校验、socket 路径、文本/JSON 协议和远端 logcat 白名单. `python3 -m unittest tests.test_android_commands -v` 通过; Pixel 4 实测 shell wrappers、本机 JSON socket、远端 logcat 与屏幕推流通过.
- [x] 运行时和终端迁入 `runtime`/`terminal`: `RuntimeInstaller` 负责安装与状态, 包内 `RuntimeScripts` 生成原有脚本, `VerifiedExtraction` 校验写入; `PtyFactory`/`PtySession` 提供会话入口, 包内 `NativePtyProcess` 和 `PtyLaunchSpec` 管理 JNI 与启动参数, `SessionToolbox`/`LocalTerminalServer` 分别管理会话工具和本机终端. JNI 类路径及注册同步, 日志标签、管理标记和脚本字节保留. 宿主测试 37 项通过; 脚本提取后相关 3 项重跑通过; Pixel 4 验证本机鉴权、PTY 读写/resize、远端命令与 Python/Frida 导入, 强制安装状态恢复后 7 个脚本哈希及状态内容一致.
- [x] 建立 `runtime`, `terminal`, `relay`, `device`, `command`, `screen` 职责包. `TerminalRelayClient`/`DeviceControlClient` 分别管理终端会话与设备控制, `WebSocketConnection` 提供共享传输, `DeviceIdentity`/`DeviceMetrics` 提供设备信息. 内部实现去除重复 Mira 前缀, App 入口保留. Pixel 4 覆盖安装后 identity preferences 哈希不变, 遥测更新、PTY、远端 logcat、屏幕推流正常.
- [x] `relay/RelayEndpoint` 收敛 control/screen 的重复 URL 构造; 宿主 golden 测试覆盖 scheme、IPv6、路径前缀、已有 endpoint、尾斜杠、转义路径、query/fragment 与非法 URI, 保留原兼容语义. 回归命令: `python3 -m unittest tests.test_relay_endpoint -v`.
- [x] View 轮廓与触摸日志迁入 `screen/ViewOutlineCollector`、`MotionEventLogger`; Application 注入轮廓刷新回调以移除采集对具体 Service 的反向依赖. `RemoteInputHandler` 接管输入校验/路由/响应, Service 动态读取当前控制连接并保留 queued sender. 相关宿主测试 3 项通过; Pixel 4 实测 7 种输入的设备确认、文字/粘贴/按键/复制/全选/清空、轮廓刷新与 20 帧推流, 原输入已恢复.
- [x] 完成 [Android Java/C 边界评估](../ANDROID-ARCHITECTURE.md#screen-codec-decision): PTY/进程底层继续复用现有 C; Android `AvcBitstream`/`ScreenVideoPacket` 与 iOS Annex B/MHS1 当前输入格式、参数集注入与序号范围不同, 本轮保留 Java/Swift 实现. 若公共解析增长或测得性能/拷贝问题, 先统一输入与错误契约、序号/时间戳策略并建立跨桥 golden 测试, 再提取共享 C. MediaCodec/View/Settings/生命周期保留平台层, 具体检测策略仍放脚本.

### 产品与自动化

1. 增加 case 浏览和导出能力, 让一次风险探索能直接形成可分享的脱敏报告。
2. 增加 Skill notes(技能笔记)提炼能力, 从 case 中提取可复用判断模式, 但不默认提交 PR(Pull Request, 代码合并请求)。
3. 增加真实设备, 模拟器和云手机的基线对比样例。
4. 增加任务编排和历史执行记录, 但默认仍保留人工确认边界。
5. 增加企业认证, HMAC(基于哈希的消息认证码), 证书或账号体系, 当前默认仍依赖自托管服务边界。

## 暂缓事项

1. 不维护 apt(包管理器)软件源。
2. 不默认动态下发未审查工具包。
3. 不做系统权限绕过。
4. 不把 UI upload(界面上传)描述为系统全屏采集。
5. 不默认自动提交 PR 或公开 case。
6. 不演示完整企业风控平台, 不内置企业工单, 服务端策略, 多模型 review, CI/CD, 灰度发布或回滚系统。
