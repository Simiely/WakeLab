# AGENTS.md · 项目规则

> 📌 **文档基线**：2026-11-04（commit `待回填`）完成四件套重写
> **更新文档/代码后，请更新此行**（日期 + 新 commit hash），并在 CHANGELOG 追加版本

## 技术栈

- Windows 11（10.0.26100）+ Python 3.14.6 + Tk 8.6（tkinter）+ Pillow
- PyInstaller 6.22.3：`--onefile --windowed`，产物 `dist/WakeLab.exe`
- 单文件程序 `wake_lab.py`；打包后无第三方运行依赖

## 关键坑

- ctypes 调 dxva2/user32 时必须显式声明 `argtypes/restype`，否则 64 位下 `HANDLE/HMONITOR` 会被截断。
- `0xD6=2`（待机）在本机能力串 `D6(01 04 05)` 未声明 02，按钮必须置灰；不要绕过 gate 强行可点。
- `0x60` 切源往返的等待填 **0**（已验证），填 2000 只会更慢，不会更稳。
- `SetThreadExecutionState` 有线程语义，必须在 UI 线程调用，不能丢进 worker 线程。
- 启动路径只允许枚举显示器 + 只读能力串；启动期出现任何 `SetVCPFeature` 都是回归。
- 禁止鼠标注入/自动点击（`SetCursorPos` / `mouse_event` / `SendInput`）；验证用打桩 + `PrintWindow` 离屏截图。

## 约定

- UI、日志、注释统一中文；每颗按钮只执行清单里那一条/那一组 API，不串联、不重试、不早退。
- 版本号只在 `APP_VERSION` 一处；发版同步窗口标题、README、CHANGELOG。
- 仓库不提交 `dist/`、`build/`、`*.exe`、`*.zip`；截图只放 `docs/images/`。

## 常用命令

- 自检：`python wake_lab.py --selftest`（不打开 Tk、不访问 dxva2、不发 DDC）
- 打包：`python build_wakelab.py` → `dist/WakeLab.exe`
- 发布：tag `vX.Y.Z`，资产 `WakeLab-vX.Y.Z-win-x64.zip`（exe + 使用说明.txt）

## 详细规则

- 使用与判定方法：`docs/使用指南.md`
- 架构与问题记录：`DEVELOPMENT.md`
