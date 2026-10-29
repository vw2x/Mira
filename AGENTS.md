# Mira repo notes

## iOS true device automation baseline

1. 真机构建不要用 `generic/platform=iOS` 兜底验证, 要用真实设备 `destination`.
2. 真机构建前要清掉宿主机库路径污染, 固定使用 `env -u LIBRARY_PATH -u SDKROOT xcodebuild ...`.
3. 当前已验证可成功的真机命令如下:

```bash
env -u LIBRARY_PATH -u SDKROOT \
xcodebuild \
  -project <path-to-mira-repo>/ios/Mira/Mira.xcodeproj \
  -scheme Mira \
  -configuration Debug \
  -sdk iphoneos \
  -destination 'id=<device-udid>' \
  -derivedDataPath <path-to-mira-repo>/build/ios-mira-device-native-relay-derived \
  -allowProvisioningUpdates \
  -allowProvisioningDeviceRegistration \
  ENABLE_DEBUG_DYLIB=NO \
  ENABLE_PREVIEWS=NO \
  build
```

4. `libbsm` `ldl` `lresolv` 这些链接项不要为了表面报错直接删掉, 它们很可能属于 iSH / Frida 兼容链路.
5. `idb` 已验证可以连上真机并完成安装启动, 推荐顺序是 `idb connect` -> `idb install --udid` -> `idb launch --udid`.
6. `idb launch` 传启动环境变量时, 直接使用 `IDB_` 前缀, 启动到 App 内后会去掉前缀.
7. 真机后续安装验证必须坚持覆盖安装, 不要先卸载 App. iOS 提示“在删除该开发者所有 App 之前将继续信任”说明开发者信任态会随该开发者 App 保留, 先卸载可能清掉信任态并导致后续启动报 invalid code signature 或 profile has not been explicitly trusted.
8. 如果安装工具遇到内部安装错误, 默认不要自动 uninstall 重试. 只有用户明确确认可以丢失信任态和 App 容器数据时, 才允许设置脚本显式开关执行卸载重装.
9. 当前已验证可工作的自动启动示例:

```bash
idb connect <device-udid>
idb install --udid <device-udid> <path-to-mira-repo>/build/ios-mira-device-native-relay-derived/Build/Products/Debug-iphoneos/Mira.app
IDB_MIRA_RELAY_URL="http://<host-ip>:8765" \
IDB_MIRA_AUTO_CONNECT=1 \
idb launch --udid <device-udid> com.vwww.mira.ios
```

## iOS MCP and Frida baseline

- iSH 子模块固定上游 commit, Mira 补丁由 `tools/ios/prepare-ish-source.py` 幂等应用. 干净检出运行 iSH 行为测试前先执行该脚本; 直接 Xcode 或 `mira-build --target ios` 构建前先通过 `tools/ios/build-ish-libs.sh` 准备并校验静态库.
- 已安装 iSH rootfs 的 `data` 和 `meta.db` 必须整体保留. stamp 变化不允许自动删除或覆盖用户环境; 运行时更新需要独立的显式迁移流程.

1. iOS 的 MCP PTY 和 Frida 链路建立在 iSH 兼容层上, syscall 会经过翻译, 冷启动和脚本执行都比 Android 慢.
2. iOS 分析默认策略不是频繁开关 PTY, 而是优先保持一个长生命周期 session, 在同一个 session 中连续执行 `status -> list -> run -> rpc`.
3. 如果调用 Mira MCP 时没有显式传 `sessionId`, 应优先复用同一 `installId` 的活动 session, 不要每一步都 reopen.
4. iOS Frida Python 辅助逻辑不要依赖 `python3 - <<'EOF'` 这类 heredoc 注入方式, 更稳的是直接 `python3 -c` 并显式补齐 `frida-setup` 和 `PYTHONPATH`.
5. iOS Frida 等待逻辑不要依赖 `time.sleep`, 在 iSH 环境下它可能触发 `Errno 38`. 如果必须等待, 优先用 Frida 脚本即时 `send(...)`, 或由 server 内部使用更稳的轮询策略.
6. 当前已验证 iOS 可以稳定返回较大的枚举结果, 例如 `ObjC.enumerateLoadedClassesSync()` 级别的全量 class 枚举.
7. 当前已验证 iOS 可以稳定返回较大的 RPC 结果, 已测通约 256 KiB 级别字符串返回.
8. 如果 iOS 侧出现看似随机的掉线或崩溃, 先怀疑 session 生命周期和 reopen 频率, 再怀疑 Frida 脚本本身.

## Android automation baseline

1. Android 自动化入口固定使用 `<path-to-mira-repo>/mira-android`.
2. 默认链路是 build -> adb install -r -> am start.
3. Relay URL 通过 Activity extras 注入, 不走手工输入 UI.
4. 当前约定的 extras 是 `mira_relay_url` 和 `mira_auto_connect`.
5. 当前已验证可工作的自动化示例:

```bash
MIRA_ANDROID_RELAY_URL="http://<host-ip>:8765" \
<path-to-mira-repo>/mira-android
```

## Android MCP detection baseline

1. Mira 基座只集成通用能力, 不把具体检测逻辑写死进 App 代码.
2. Magisk 等环境检测默认走 `MCP -> shell script -> logcat/result` 链路, 由 AI 编写或选择脚本, 通过 Mira MCP 执行并分析结果.
3. `getattr`、`getxattr`、`/proc/<pid>` 扫描、`logcat grep audit/avc` 这类动作属于检测脚本策略, 不应沉进 Mira App 的固定组件逻辑.
4. Mira Android 侧应优先提供通用 primitive, 包括 shell 执行、PTY 会话、logcat 查看、文件投递和结果回收.
5. 具体侧信道检测应沉淀为 `tools/android/*.sh` 和 `knowledge/cases/*` 中的可复现实验, 而不是新增 hardcoded detector.
6. 对 Magisk audit side-channel 这类场景, 推荐脚本内部完成循环扫描、节流等待、logcat grep、命中后退出和结构化输出.
7. 判断结论由 MCP 执行结果和 AI 分析给出, Mira UI 只负责展示通用日志和命令输出.
