# MediaHarbor · macOS 自动媒体归档（MVP v0.1.1）

插入 SD 卡或 U 盘后，自动将媒体文件复制到你选定的磁盘或文件夹。

- **图片**：统一放在 `图片/`，不按日期建目录。
- **视频**：按拍摄日期归档到 `视频/YYYY-MM-DD/`。
- **只复制不删除**：不会删除或修改 SD 卡、U 盘的原始文件。
- **内容去重**：SHA-256 + SQLite；同一个文件再次插入、改名或换来源都不会重复复制到**同一目标归档**。
- **可更换保存位置**：可以选择 Mac 内置盘或外接 SSD；新目标建立独立去重索引，**不自动迁移旧目标上的文件**。
- **中断可恢复**：复制后校验；先用隐藏临时文件写入，异常中断留下的临时文件会在下次任务开始时清理。
- **无需常驻监听服务**：使用 macOS 自带的 launchd `StartOnMount` 触发。

**状态：本地独立虚拟环境已经完成 26 项回归测试；GitHub Actions 提供 Linux/macOS 虚拟环境与 macOS 虚拟磁盘验收。真实 SD 卡连接、自动启动及隐私授权仍须在个人 Mac 上测试。**

## 1. 安装（macOS）

### 前提条件

- **macOS 15（Sequoia）及更新版本**。已在 GitHub Actions 的 macOS 15 Intel Runner 完成自动化测试；**macOS 15.7.9** 可使用相同的 `launchd StartOnMount` 触发机制，但仍需在本人 Mac 上验证真实插卡触发和隐私权限。
- **Python 3.9 或更新版本**：在终端运行 `python3 --version` 检查。macOS 不保证每台机器都自带可运行的 Python 3。没有时可以从 [python.org](https://www.python.org/downloads/macos/) 安装，或使用 Homebrew 安装 Python。
- 安装时需要已经创建并挂载目标保存文件夹。**不需要 sudo 或管理员权限**。
- 可选：安装 ExifTool，有助于更准确读取多种相机和视频的拍摄日期（Homebrew：`brew install exiftool`）。没有 ExifTool 也可以运行。

### 最简单安装方法

使用「终端」从 GitHub 获取并安装最新版，例如（如果没有 Git，可下载仓库 ZIP，解压后进入解压目录运行 `bash ./安装.command`）：

```bash
git clone https://github.com/wekingchen/MediaHarbor.git
cd MediaHarbor
bash ./安装.command
```

系统会弹出文件夹选择窗口，选择已有的归档根目录（比如 `~/Pictures/多媒体归档` 或 `/Volumes/MediaSSD/多媒体归档`）。

首次使用时，工具会在该文件夹内放置一个**隐藏身份标记** `.media-archive-target.json`，用来防止外接硬盘未连接时误写到同名的本地路径。请不要手工删除这个标记。

之后为当前用户注册 `launchd` 自动任务，Mac **登录后**每次挂载文件系统会尝试运行扫描（程序会自行筛选是否为可用来源）。

如果你远程通过 SSH 操作 Mac，不想让它弹出文件夹选择窗口，可以这样安装：

```bash
python3 installer.py --target "/Volumes/MediaSSD/多媒体归档"
```

> 默认不会在 FileVault 尚未解锁、Mac 没有登录用户会话时运行。

## 2. 如何使用

- **自动导入**：正常登录 Mac 后，插入 SD 卡或 U 盘，程序会自动检查来源和目标，开始复制。
- **管理设置**：安装后随时在「终端」运行 `bash "$HOME/Library/Application Support/MediaAutoArchive/管理设置.command"`，无需保留解压目录。可查看状态、更换目录、查看日志、手动触发扫描、添加来源白名单或停用自动扫描。
- **纯命令行**（适合远程 SSH）：

```bash
APP="$HOME/Library/Application Support/MediaAutoArchive/media_archive.py"
python3 "$APP" status
python3 "$APP" configure                            # 弹出文件夹选择器
python3 "$APP" configure --target "/Volumes/MediaSSD/多媒体归档"
python3 "$APP" scan                                 # 扫描已连接的可移动介质
python3 "$APP" scan --source "/Volumes/CAMERA_SD"   # 手动指定来源（不受来源白名单限制）
python3 "$APP" allow-source "CAMERA_SD"            # 加入自动导入白名单
python3 "$APP" deny-source "CAMERA_SD"             # 移出白名单
```

### 来源设备默认规则

为了避免把**普通外接备份硬盘**也扫描成待导入的 SD 卡，默认仅自动接收 macOS 识别为「可移动介质」的设备。

某些 U 盘、USB 读卡器或移动存储设备会被 macOS 标记为“固定介质”。如果插卡后没有自动导入，在管理菜单中输入 Finder 里显示的**磁盘名称**，将其加入白名单，或者先用 `scan --source "/Volumes/盘名"` 手动验证一次。

**不会因为插入保存素材的外接 SSD，就把它当作来源重新复制**；程序检查来源和保存目标的设备身份以及卷文件系统。

### 归档范例

```text
你选择的目录/
├── .media-archive-target.json       # 隐藏身份标记，切勿删除
├── 图片/
│   ├── IMG_001.JPG
│   ├── DSC_0023.ARW
│   └── IMG_001__a1b2c3d4e5.JPG   # 同名、内容不同自动避免覆盖
└── 视频/
    ├── 2026-11-23/
    │   └── DJI_0001.MP4
    ├── 2026-11-24/
    │   └── GX010123.MOV
    └── 日期待确认/
        └── unknown.MP4
```

## 3. 去重和日期规则

- 每个保存位置都有独立 UUID 身份；数据库在 `~/Library/Application Support/MediaAutoArchive/history.sqlite3`，不放在 SD 卡中。
- 首次选用一个**已经存在大量文件**的归档目录时，程序会先扫描并建立该目录的去重索引；文件较多时会花一些时间。
- 先按来源文件内容计算 SHA-256，查找当前保存位置的历史文件；已存在且完整的文件会跳过。
- 即使文件名不同、来源不同，只要内容完全相同，也不会在同一归档中导入第二份。
- 相同文件名但内容不同会在原名后增加内容指纹，不覆盖旧文件。
- 如果你删掉目标里的旧文件，将来再次遇到对应来源，允许重新导入。
- 视频的日期优先从 ExifTool 读取，其次尝试 macOS `mdls` 的创建时间，最后使用**源文件的最后修改日期**。如果日期不可信则归到 `日期待确认`。没有 ExifTool 时，文件系统的修改日期可能**不等于真实拍摄日期**。
- 照片不按日期分类，因此无需读取照片拍摄日期；RAW、HEIC 等常见格式按文件扩展名识别，原文件内容不会转换或压缩。
- Live Photos 的照片和 MOV 会分别归类，不做成对绑定；`.AAE`、相机其它辅助文件不导入。
- 与百度网盘的同步/备份**不在 MVP 范围内**；目前所有数据处理都在本机完成。

## 4. 重要安全规则

1. **只复制，永不自动删除源文件；没有自动弹出 SD 卡功能。**
2. 如果目标磁盘缺失、目标身份标记不匹配或不可写，任务停止；不会回退到某个本地同名文件夹。
3. 来源与目标不能是同一挂载文件系统；避免扫描目标本身导致循环复制。
4. 每个导入的文件在复制后执行 SHA-256 校验，校验通过后才记录成功。
5. 外接盘文件读取可能需要 macOS 隐私授权。在「系统设置 → 隐私与安全性」检查文件与文件夹、可移动宗卷相关授权。手动运行后成功但后台失败时，检查 Python/终端权限与程序日志。
6. 关闭工具不会删除目标素材和本地导入记录；退出登录后不会继续后台运行。

## 5. 验收方法（建议首次在测试文件夹完成）

1. 新建一个空的测试归档目录，用管理菜单将其设为保存位置。
2. 找一张装有几张图片、几段视频的测试 SD 卡，先手动运行 `scan --source "/Volumes/卡名"`，核对图片在一个目录、视频按日期分类。
3. **同一张卡再导入一次**，新增应该为 0、重复数增加。
4. 将一个文件重命名再尝试导入，确认仍然跳过；测试两个同名但内容不同的文件，确认两个都保留。
5. 改用外接 SSD 作为目标，手动断开该 SSD，再触发扫描，确认工具报错且**不会写到本地同名路径**；重新连接后再试。
6. 检查日志中成功、跳过、失败的数量；确认 SD 卡原始文件不变。

查看最近日志：

```bash
tail -n 80 "$HOME/Library/Application Support/MediaAutoArchive/activity.log"
```

> macOS 上 `launchd` 的系统触发、磁盘识别及隐私权限行为，需要在真实 Mac + SD 卡 / U 盘环境中验收。这里的自动化测试不能替代这一步。

## 6. 关闭与卸载

在解压目录执行：

```bash
python3 installer.py --uninstall
```

这只会关闭并卸载自动触发器；**不会删除图片、视频、配置、日志和去重数据库**。再次运行安装可恢复自动功能。

如果不再需要该工具，可在确认不再使用后，手动清理 `~/Library/Application Support/MediaAutoArchive`。不要误删你选择的真实归档目录。

## 7. 本地代码检查

无需额外依赖，在 macOS/Linux 下的 Python 3.9+ 执行：

```bash
python3 -m unittest discover -s tests -v
```

包含内容去重、同名不覆盖、视频分日期、原始文件保留、目标身份检查、异常复制清理、切换目标、来源限制等测试。Linux 上的测试不会模拟 macOS 真实的 launchd 或权限弹窗。

## 8. GitHub Actions 自动化验证

仓库在推送、PR 和手动运行时执行 `.github/workflows/ci.yml`：

- Ubuntu 24.04 / macOS 15 × Python 3.9 / 3.13，分别创建独立 `.venv`，语法检查、Shell 语法与 unittest。
- macOS 使用 `plutil -lint` 校验安装器生成的 `launchd` 配置。
- macOS `hdiutil` 创建并挂载独立 HFS+ 虚拟 SD 卡，执行真实跨卷导入、再次导入去重、目标目录失联保护。

**边界：** GitHub Runner 无真实读卡器和用户隐私授权弹窗；真实设备自动触发仍需在自己的 Mac 上验收。

运行本地测试：

```bash
python3 -m venv .venv
.venv/bin/python -m compileall -q media_archive.py installer.py tests
.venv/bin/python -m unittest discover -s tests -v
```

代码为标准库实现，运行主程序不依赖 `pip install`。

### 开发状态及版本边界

MVP 使用本机 SQLite 记录已导入媒体文件。SHA-256 去重与目标目录身份检查优先于复制速度；对相同大小和时间戳的文件仍检查内容指纹。外接文件系统不支持原子硬链接时使用防覆盖独占创建路径，并在失败时清理；**不能保证在突然断电时对所有 exFAT 卷做到瞬时不可见的文件提交**。如需高保证归档，建议目标盘采用 APFS。

百度网盘备份、图形菜单栏和 Live Photos 成对处理尚未实现。