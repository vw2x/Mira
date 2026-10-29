# iSH 升级记录

## 范围

本次将 iSH 从 `52189692e5cf85dce300b6adadb26bb287f0f046` 固定升级到 `de5ab2c4b1039551203c1106e75f715cd917544b`.
保留 Android 配置、Termux 子模块、Frida 16.0.7 和现有 Python/rootfs 运行时.
上游差异见 [iSH compare](https://github.com/ish-app/ish/compare/52189692e5cf85dce300b6adadb26bb287f0f046...de5ab2c4b1039551203c1106e75f715cd917544b).

## 源码与本地补丁

`tools/ios/prepare-ish-source.py` 在构建前校验固定 commit, 再幂等应用 `tools/ios/patches/ish-mira.patch`.
因此构建后子模块工作树包含这份补丁, `git status` 显示子模块有修改是预期行为.
补丁保存在主仓库, 不依赖未发布的子模块 commit. revision 不匹配、局部已应用或与现有编辑冲突时会停止, 不覆盖修改.

补丁包含三项:

1. 修复上游 `do_mmap` 回归: 非 `MAP_FIXED` 请求给出已占用的地址提示时, 重新寻找空闲地址, 保留原映射; 空间不足返回 `ENOMEM`.
2. iSH 的 Meson/Ninja Xcode wrapper 使用构建入口提供的 `PATH`, 移除通过 `sudo` 启动登录 shell 来探测 `PATH` 的逻辑.
3. 修复既有 TTY 长输入阻塞: 非 canonical 模式下 4 KiB 缓冲区填满时, 先唤醒读者再等待空闲空间. 旧、新上游 commit 都存在该问题.

`tests/test_ios_ish_mmap.py` 编译实际生产 `do_mmap` 函数, 配合最小页表 stub 验证行为.
未修补的目标 commit 为 2 通过、2 失败; 修补后 4 项通过. 该测试覆盖映射决策, 不替代真机完整地址空间测试.

`tests/test_ios_ish_tty.py` 提取生产输入和读取函数, 用真实 pthread 条件变量验证.
旧代码在 8192 和 8193 字节输入时均超时; 修补后 4 项通过, 同时覆盖 4096 字节边界和非阻塞短写语义.

## 构建缓存

`build-ish-libs.sh --check` 和 `run-app.sh` 共用 `ish-build-state.py` 校验缓存.
有效缓存要求源码、两个必需嵌套子模块、兼容补丁、构建脚本、SDK/Xcode、工具链、架构、配置、编译参数及三个静态库的摘要全部匹配.
只有旧 `.a` 而没有 manifest 会重建. 构建开始先使旧 manifest 失效, 成功后重新核对输入并原子写入 manifest.
需要重建时只清理该配置的派生 `meson` 目录, 避免继续使用旧 SDK 或架构的 crossfile.

```bash
MIRA_ISH_SDK=iphoneos MIRA_ISH_ARCHS=arm64 \
MIRA_ISH_BUILD_DIR="$PWD/build/ios-ish-device-libs" \
tools/ios/build-ish-libs.sh
```

## rootfs 与数据保留

- rootfs archive 固定为既有 `g00712ff0a54b2839c5aa1a8ed758003ca65357dc/appstore-apk.tar.gz`, SHA256 为 `776b16416894e5a8bec220b8d4726c46bb993289e8e48fdac54a519396e40a93`.
- 完整的 v13 Python/Frida 构建产物继续复用. provenance 与运行时 stamp 分开; 旧产物标记为 `adopted-legacy`, 不声称由新 iSH 生成.
- App 中已有的 `data` 目录和非空 `meta.db` 始终作为整体保留. stamp 不同或缺失只记录待迁移, 不删除重建.
- 首次安装先复制到同一父目录下的唯一 staging, 校验后用 `renamex_np(RENAME_EXCL)` 原子发布. 失败只清理本次 staging.
- 部分缺失或类型异常会报错并保留现场. 这项检查确认目录结构, 不替代 iSH 挂载时的 SQLite 校验. 后续运行时升级需要单独设计显式迁移.

## 2026-10-29 验证

宿主回归结果为 **70 passed, 19 subtests passed**. 覆盖构建缓存失效、失败构建、rootfs 复用、补丁幂等与冲突、生产 mmap/TTY 行为及 Foundation 文件操作故障场景. 其中 Foundation 测试驱动实际文件操作, 含 10 类正常与故障场景.

```bash
git submodule update --init third_party/ish
python3 tools/ios/prepare-ish-source.py
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONDONTWRITEBYTECODE=1 \
python3 -m pytest tests -q -p no:cacheprovider
```

- iSH 三个 arm64/iphoneos 静态库构建成功, 随后的 `--check` 命中有效缓存.
- iPhone X, iOS 16.7.15, USB 连接可用. 使用该设备的真实 destination 完成签名构建和覆盖安装, 没有使用 generic destination.
- 构建复用已有 rootfs, 没有重新下载或升级 Python/Frida.
- 编译仍有既有 Swift 并发检查、弃用 API 和 iSH 汇编符号对齐警告; 本轮未展开处理.
- 初次构建的账号/签名阻塞在用户登录 Xcode 后解决. 覆盖安装后 App 正常启动, 安装标识保持不变; 全程未卸载 App 或清除容器.
- 同一 PTY 连续完成 shell、Python 3.11.14、Frida 16.0.7 status/list/run/RPC. ObjC 枚举完整返回 26,294 个类名, 大 RPC 返回 262,144 字符且 SHA256 与预期一致.
- `tools/ios/verify-ish-syscalls.py` 在真机 iSH guest 中四项通过: 已占用 mmap hint 保留原映射、getdents64 小缓冲区返回 EINVAL 且重试不丢位置、自身 proc mem seek/read、fork 后清空 POSIX timer 槽位. 该脚本只允许在 32-bit iSH guest Python 中运行, 不应直接在宿主执行.
- 最终补丁版一次投递 8,433 字节的完整 syscall 脚本成功, 退出码 0, 四项探针通过; 随后同一 PTY 继续处理命令. 未修补版本在同样长输入路径阻塞, 此现象与宿主线程回归测试一致.
- 在已初始化 rootfs 中创建验收专用文件, 记录软件包数据库摘要, 人为改变安装 stamp 后再次覆盖安装. 文件、软件包数据库及安装标识均保持不变, 改动后的 stamp 也未被覆盖. 验收后恢复原 stamp, 清理本次创建的 guest 文件.

直接运行 Xcode 或 `mira-build --target ios` 前, 先执行上面的 `build-ish-libs.sh` 命令准备并校验原生库. 签名构建使用项目约定的命令, 其中 `DEVICE_UDID` 为连接设备的真实 ID:

```bash
env -u LIBRARY_PATH -u SDKROOT xcodebuild \
  -project ios/Mira/Mira.xcodeproj -scheme Mira -configuration Debug \
  -sdk iphoneos -destination "id=$DEVICE_UDID" \
  -derivedDataPath build/ios-mira-device-native-relay-derived \
  -allowProvisioningUpdates -allowProvisioningDeviceRegistration \
  ENABLE_DEBUG_DYLIB=NO ENABLE_PREVIEWS=NO build
```

首次升级前设备容器尚未初始化 iSH rootfs, 因此数据保留实测针对第一次运行后的环境和第二次覆盖安装; 其他历史 rootfs 的兼容性仍需对应设备验收. 本轮设备覆盖范围仅为这台 iPhone X, 不外推到所有 iOS 版本或机型.

syscall 探针可通过 Mira MCP 的 `python3 -c` 投递, 或分块写入 guest 临时文件后执行. 复用同一 PTY 进行 status/list/run/RPC, 不以频繁重开会话作为正常流程. 本地验收明细保存在忽略目录 `build/ios-ish-upgrade-smoke.jsonl`, 构建日志为 `build/ios-ish-upgrade-signed-build.log`.

本次临时 Relay 使用端口 `18765`: 本机 `8765` 已有其他服务, 不应将任何返回 HTTP 200 的服务直接认作 Mira Relay.
