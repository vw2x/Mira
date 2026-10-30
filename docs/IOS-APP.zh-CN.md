<p align="right">
  <a href="./IOS-APP.md">English</a> | 简体中文
</p>

# iOS App(苹果移动应用)

本文记录 Mira iOS App 的当前状态和启动方式。当前 iOS 侧已经接入 Relay(中继服务), PTY(伪终端), 远程 View(界面视图) 上传, 设备指标采样和浏览器快捷输入。

## 当前能力

已完成:

1. `ios/Mira/Mira.xcodeproj` Xcode project(Xcode 项目)。
2. SwiftUI(Swift 声明式界面框架) 首页, 对齐 Android(安卓系统) 控制页。
3. Relay URL(中继地址) 输入框, `Connect Relay` 和 `Disconnect` 按钮。
4. iOS 原生 relay 控制通道, 支持浏览器打开 PTY(伪终端) 会话。
5. App 自身窗口 View(界面视图) 通过 H.264(视频编码标准) + WebSocket(网页套接字) 上传到 `/ws/screen/device`。
6. CPU(中央处理器), 内存和网络速率指标每 1 秒通过 `device.metrics` 上报。
7. 浏览器远程画面支持点击, 文本输入, 粘贴, 复制, 全选, 清空, Backspace(退格) 和 Delete(删除)。
8. 根目录 `./mira-ios` 启动入口。

当前边界:

1. 只渲染 Mira App 自己的 key window(主窗口), 不使用 ReplayKit(苹果屏幕录制框架), 不采集系统全屏。
2. 远程点击使用公开 UIKit(苹果界面框架) 能力尽量激活命中的控件, 不做私有系统触摸注入。
3. 原生 C relay 的控制通道仍以 `ws://` 为主, Swift 层远程画面 WebSocket 支持 `ws://` 和 `wss://` URL(统一资源定位符)。

## 目录

```text
ios/
  Mira/
    Mira.xcodeproj/
    Mira/
      App/
        MiraApp.swift
        ContentView.swift
        MiraControlViewModel.swift
      NativeBridge/
        Mira-Bridging-Header.h
        MiraNativeStatus.swift
        MiraRemoteServices.swift
      Resources/
        README.md
```

Native bridge(原生桥接层) 还会引用:

```text
native/bridge/ios/mira_pty_ios_shim.h
native/bridge/ios/mira_pty_ios_shim.c
native/bridge/ios/mira_ios_relay.c
```

## 打开方式

### Xcode 打开

```bash
open ios/Mira/Mira.xcodeproj
```

在 Xcode 中选择:

```text
Mira -> iPhone 17 Pro -> Run
```

### 命令行启动

```bash
./mira-ios
```

脚本会执行:

1. 检查 Xcode 命令行工具。
2. 确保 iOS Simulator(iOS 模拟器) 已启动。
3. 使用 `xcodebuild` 构建 Debug(调试) 版本。
4. 使用 `simctl install` 安装到 booted simulator(已启动模拟器)。
5. 使用 `simctl launch` 打开 App。

可选环境变量:

```bash
MIRA_IOS_DEVICE="iPhone 17 Pro" ./mira-ios
MIRA_IOS_SCHEME="Mira" ./mira-ios
MIRA_IOS_BUNDLE_ID="com.vwww.mira.ios" ./mira-ios
```

### 真机自动化链路

如果要把真机链路做成完全自动化, 推荐把 `idb` 作为依赖:

1. 安装 `idb-companion` 和 `fb-idb`。
2. 使用 `./mira-ios --device` 做 build 和 install。
3. 通过 `idb launch` 启动 App, 并把 Relay URL 通过环境变量注入到 App 启动环境。

官方安装文档:

- [idb Installation](https://fbidb.io/docs/installation/)
- [idb Commands](https://fbidb.io/docs/commands/)

Mira iOS App 支持读取以下启动环境变量:

1. `MIRA_RELAY_URL`: 启动后写入首页 Relay URL 输入框对应状态。
2. `MIRA_AUTO_CONNECT`: 为 `1` 时, App 启动后自动执行 `Connect Relay`。

配合 `idb` 的自动化示例:

```bash
idb connect <device-udid>
IDB_MIRA_RELAY_URL="http://<电脑局域网IP>:8765" \
IDB_MIRA_AUTO_CONNECT=1 \
idb --udid <device-udid> launch com.vwww.mira.ios
```

如果直接使用仓库脚本, 可开启真机自动启动并注入 Relay URL:

```bash
git submodule update --init --recursive
bash ./tools/ios/build-frida-musl-devkit.sh
MIRA_IOS_AUTO_LAUNCH_DEVICE=1 \
MIRA_IOS_RELAY_URL="http://<电脑局域网IP>:8765" \
./mira-ios --device
```

### fresh clone 恢复说明

这条链路不是 clone 完就一定能直接跑起来, 首次真机恢复时通常还要补齐下面几类前置物:

1. `third_party/ish` 子模块.
2. iSH 真机静态库, 例如 `build/ios-ish-device-libs/.../libish.a`.
3. iSH host tools, 例如 `build/ios-ish-host-tools/ish` 和 `build/ios-ish-host-tools/tools/fakefsify`.
4. Frida musl devkit, 默认输出到 `build/frida/devkit/16.0.7/linux-x86-musl`.

如果 `./mira-ios --device` 卡在 `Prepare iSH RootFS`, 先优先检查:

1. 当前 shell 是否带了 `LIBRARY_PATH` 或 `SDKROOT` 这类宿主机路径污染.
2. iSH host tools 是否是在干净环境里构建的.

当前已验证可工作的真机构建基线是:

```bash
env -u LIBRARY_PATH -u SDKROOT \
xcodebuild \
  -project ios/Mira/Mira.xcodeproj \
  -scheme Mira \
  -configuration Debug \
  -sdk iphoneos \
  -destination 'id=<device-udid>' \
  -derivedDataPath build/ios-mira-device-native-relay-derived \
  -allowProvisioningUpdates \
  -allowProvisioningDeviceRegistration \
  ENABLE_DEBUG_DYLIB=NO \
  ENABLE_PREVIEWS=NO \
  build
```

安装和启动基线是:

```bash
idb connect <device-udid>
idb install --udid <device-udid> build/ios-mira-device-native-relay-derived/Build/Products/Debug-iphoneos/Mira.app
IDB_MIRA_RELAY_URL="http://<电脑局域网IP>:8765" \
IDB_MIRA_AUTO_CONNECT=1 \
idb launch --udid <device-udid> com.vwww.mira.ios
```

### unsigned IPA 归档

统一构建入口 `./mira-build --target ios --ios-device-id <device-udid>` 会复用真机 `xcodebuild` 产物目录:

```text
build/ios-mira-device-native-relay-derived/Build/Products/Debug-iphoneos/Mira.app
```

然后做两层归档:

1. 直接把 `Mira.app` 打成 `dist/ios/Mira.app.zip`
2. 按 iOS 标准分发目录组装 `Payload/Mira.app`, 再压成 `dist/ios/Mira-unsigned.ipa`

这里的 unsigned IPA 不只是省略导出签名和发布签名流程. `mira-build` 会先把已构建出的 device `.app` 复制到临时 staging 目录, 排除 `.env.example` 以外的敏感 `.env` 变体, 移除其中所有 Mach-O 文件的嵌入签名, 删除 `_CodeSignature` 和嵌入式 provisioning profile, 再从同一份已清理副本生成 `.app.zip` 与 `.ipa`. 原始 Xcode 构建产物保持不变; 签名状态不明确、签名移除失败或归档校验失败时, 不会用未完成的归档覆盖现有产物.

如果是 CI 或没有连接真机的环境, 可以改用:

```bash
./mira-build --target ios --ios-destination-mode generic
```

这条链路会走 `generic/platform=iOS`, 适合做统一打包和产物归档. 真机专属安装验证仍建议保留在本地人工验收阶段.

## Relay 联调路径

1. 启动 Mira relay 服务端。
2. 在 iOS App 输入 Relay URL(中继地址) 并点击 `Connect Relay`。
3. 浏览器打开 relay console(控制台), 设备列表应出现 iOS 设备。
4. 选择设备后, 左侧显示 iOS App 自身画面, 右侧 Web Terminal(网页终端) 可打开 PTY 会话。
5. 底部 INFO(信息) 面板展示 CPU, MEM(内存), NET(网络) 动态曲线。
6. 在远程画面中点击输入框后, 可通过浏览器键盘输入文字, 粘贴, 复制, 全选和删除。

## 首页 UI 对齐 Android

Android 和 iOS 首页保持相同信息架构:

1. 左侧大标题 `Mira`。
2. 右侧小字 `by vw2x`。
3. 中间 Relay URL 输入框。
4. `Connect Relay` 按钮。
5. `Disconnect` 按钮。
6. monospaced(等宽字体) 状态文本。
