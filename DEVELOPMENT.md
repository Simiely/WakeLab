# DEVELOPMENT · 架构与问题记录

## 项目概览

WakeLab 是 MonitorBrightness「亮屏阶梯」的拆解测试台：把每一种点亮手段做成一颗独立按钮，用于在同一台/不同显示器上测量「动作耗时」和「是否真的点亮」。

- 交付形态：单文件 `wake_lab.py` → PyInstaller onefile `WakeLab.exe`
- 平台：Windows 10/11 x64
- 版本：`APP_VERSION`（当前 1.1.0），窗口标题与 CHANGELOG 同步
- 硬约束：不串联、不重试、不早退；启动零 DDC 写；不使用鼠标注入/自动点击

## 架构说明

### `wake_lab.py` 分区

1. **DDC/CI 层**（`_dxva2` / `read_vcp` / `write_vcp` / `enumerate_targets` / `_capabilities`）
   - 全部走 `dxva2.dll`；显式声明 `argtypes/restype`。
   - 只读能力串用于 UI 门槛，不参与动作执行。
2. **系统调用层**（`broadcast_monitor_power` / `set_thread_execution_state`）
   - `SendMessageTimeoutW(HWND_BROADCAST, WM_SYSCOMMAND, SC_MONITORPOWER, …)` 是系统级广播。
   - `SetThreadExecutionState` 保持/释放显示，线程语义。
3. **按钮清单**（`ACTION_SPECS`）
   - 单一事实来源：按钮 id、分组、显示名、真实 API 文本；`--selftest` 也读它。
4. **门槛逻辑**（`gate_d6` / `d6_button_text` / `d6_enabled`）
   - 依据能力串 `D6(...)` 的声明启用 0xD6=4/5/2；未声明置灰，部分声明可点但逐台跳过。
5. **UI**（`Card` / `Button` / `Label` / `ScrollArea` / `WakeLabApp._build_ui`）
   - Pillow 4x 超采样圆角贴图；深色 ttk 复选框用 image element 重绘。
   - 0x60 分组含绿色徽标与实测说明行。
6. **执行与队列**（`on_action` / `_action_worker` / `_readback_worker` / `_poll_queue`）
7. **离线自检**（`run_selftest`）与入口 `main`

### 一次点击的数据流

```
click → on_action(kind)
      → 记录目标序号 + 能力串快照
      → 后台线程 enumerate_targets()
      → _execute_action(kind, picked, …)   # 只跑这一条
      → _readback(picked)                  # 0xD6/0x10/0x60 各一次
      → queue → UI 显示 动作耗时 / 含读回总耗时 / 读回值
```

`es_hold` / `es_release` 例外：`SetThreadExecutionState` 在 UI 线程执行，再开线程做读回。

### 为什么按钮互不串联

主程序需要「自动升级」，测试台需要「可归因」。如果按钮内部自动重试或串联，就无法判断到底哪一步点亮了屏、耗时算谁的。因此每颗按钮只执行 `ACTION_SPECS` 里写死的那条/那组调用。

## 关键问题与方案

### 问题：0x60 切源往返等待 2000 ms 的假设是错的

**TL;DR**：切源往返不需要等待，填 0 ms 就能点亮；多填只会更慢。

- 问题：早期默认等待 2000 ms，误以为切回原源前必须给面板留缓冲。
- 根因：点亮由「切走再切回」的脉冲触发，等待不是必要条件。
- 解决：默认值改为 0，并在 UI 标注「✓ 已验证 · 0 等待可点亮」；说明写清「等待必须填 0」。
- 实测：两台 Legion R27qe Gen2、目标 DISPLAY2，动作 183–188 ms；含读回 368–372 ms；0x60 读回回到原值 15。
- 预防：换新显示器先按 0 测；只有 0 失败时才逐档加大等待。

### 问题：0xD6=2 按钮置灰

**TL;DR**：能力串 `D6(01 04 05)` 没有声明 02（待机），按声明置灰而不是盲写。

- 问题：用户看到「0xD6 = 2→1（未声明 02）」无法点击。
- 根因：MCCS 能力串只声明支持 01/04/05；DXVA2 对未声明值可能返回失败或行为未定义。
- 解决：`gate_d6` 按 `caps_power()` 结果决定 enabled；部分声明时逐台跳过并写日志。
- 预防：不要为了「能点」绕过门槛；要测待机需换一台声明 02 的屏。

### 问题：64 位下 DDC 句柄被截断

**TL;DR**：ctypes 未声明 `restype` 时 `HANDLE` 按 int32 返回，高地址句柄失效。

- 问题：`GetPhysicalMonitorsFromHMONITOR` 返回的句柄在后续调用中偶发失败。
- 根因：默认 restype 为 `c_int`，64 位句柄被截断。
- 解决：`PHYSICAL_MONITOR` 用 `wintypes.HANDLE`，所有 dxva2 函数显式 `argtypes/restype`。
- 预防：新增 Win32 调用必须先写 `argtypes/restype`。

### 问题：SetThreadExecutionState 放到 worker 线程无效

**TL;DR**：该 API 作用于调用线程，worker 线程结束后状态即失效。

- 问题：保持显示按钮偶发「执行成功但屏仍休眠」。
- 根因：执行状态绑定线程；后台线程退出后约束消失。
- 解决：`es_hold` / `es_release` 在 UI 线程调用，只把读回放后台。
- 预防：凡是有线程语义的 API，先确认调用线程生命周期。

### 问题：验证时不能真发 DDC 写、不能抢前台

**TL;DR**：用打桩断言 API 参数，用 PrintWindow 离屏截图验证 UI。

- 问题：WakeLab 的按钮会真实改显示器状态，自动化验证有黑屏风险。
- 根因：DDC 写与系统广播都有副作用，鼠标注入还会干扰用户。
- 解决：
  - 动作测试 `_wakelab_stub_test.py` 替换 `write_vcp/read_vcp/broadcast_monitor_power/set_thread_execution_state`，断言调用序列与参数。
  - UI 截图用 `PrintWindow` + `HWND_BOTTOM`/`NOACTIVATE`，不调用 `SetCursorPos/mouse_event/SendInput`。
- 预防：任何新验证脚本先确认「零 DDC 写、零输入注入、不抢前台」。

### 问题：深色 ttk Checkbutton 残留白块

**TL;DR**：clam 主题的 indicator 不受 `configure` 控制，必须替换 image element。

- 问题：深色卡片上复选框出现白色方块。
- 根因：ttk indicator 由主题元素绘制。
- 解决：`install_dark_tickbox()` 用 Pillow 生成 off/on/disabled 三态图，`element_create("Dark.tickbox", "image", …)` 并重写 layout。
- 预防：深色 ttk 控件优先检查 element 而非 style.configure。

### 问题：onefile 体积偏大

**TL;DR**：Pillow 的多余插件占体积，构建脚本按模块排除。

- 问题：初版 exe 体积偏大。
- 根因：PyInstaller 收集 Pillow 全部解码插件与 numpy 等未用模块。
- 解决：`build_wakelab.py` 的 `EXCLUDES` 排除 JPEG/WebP/TIFF/numpy 等；仅保留 UI 所需。
- 结果：v1.1.0 exe 14,909,381 B。

## 验证与发布

- 源码自检：`python wake_lab.py --selftest` → 20/20
- 打包后自检：`dist\WakeLab.exe --selftest` → 20/20（写 `selftest.txt`）
- 动作打桩：14/14，覆盖 11 个动作与部分声明/未就绪跳过
- UI：`docs/images/wakelab-ui.png`（676×1067，PrintWindow）
- 发布：tag `v1.1.0`，资产 `WakeLab-v1.1.0-win-x64.zip`；发布后从公开直链下载复核 exe 的字节数与 SHA256
