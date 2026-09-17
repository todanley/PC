# UnboundComputerUse — 简历摘要

面向简历/求职材料的一页可引用摘要。可以直接放入项目条目，按需删改与排序。

## 一句话介绍

**UnboundComputerUse** —— 视觉优先、跨平台（macOS + Windows）的 computer-use 智能体，通过大模型对原始截图推断出鼠标 / 键盘动作来驱动任意桌面与 Web 应用。整个项目约 **9,800 行代码**，包括 Python / PySide6 客户端、Cloudflare Worker + D1/R2 后端，以及营销站点。

## 架构设计

- 设计了一套**视觉优先的智能体循环**：每一步只依赖一张 JPEG 截图，由视觉大模型决定下一个动作 —— 无需 DOM、无需可访问性树 —— 因此能够统一适用于原生应用、浏览器、游戏和远程桌面。每次点击后用 200×200 px 的像素差分验证，识别未生效的空点击。
- 构建了**供应商抽象的视觉层**，可在 Anthropic Claude、Google Gemini（REST + OpenAI-兼容接口）、Moonshot Kimi（OpenAI-兼容）以及一个由 Playwright 驱动的 `gemini.google.com` 浏览器兜底路径之间通过环境变量切换；最贵与最便宜供应商之间的成本相差约 25 倍。
- 实现了**跨平台输入 / 屏幕适配层**（`platform_layer/mac.py` + `win.py`），在 macOS 上封装 Quartz CGEvent，在 Windows 上封装 pyautogui / pywin32 / mss，对上暴露统一的 `Input` / `Screen` API。
- 打包为 **Windows 单文件 `.exe`** 与 **macOS 已签名 `.app`**，构建时通过自研管线用 `@@PLACEHOLDER@@` 占位符 + sed 替换的方式把 Bridge 相关密钥注入到构建产物中。

## 元素定位 —— Set-of-Mark、OCR 与 UI Automation

- 实现了 **Set-of-Mark (SoM) 标记集提示**（Yang 等人，2023 年）的一个轻量级混合版本：用 **RapidOCR**（通过 **ONNX Runtime** 运行 PP-OCR 中英文模型）识别带文本的控件，用 **OpenCV** 的边缘轮廓检测出仅由图标构成的控件，再融合 Windows **UI Automation (UIA)** 提取视觉难以区分的语义控件（如 `···` 溢出菜单、极小的图标按钮、桌面启动图标等）。
- 编写了一个 **492 行的 Windows UIA 遍历器**：按 ControlType 过滤、时间预算受限的 BFS（1 秒预算、深度 25）、对 Chrome 页面的 `Document` 节点走优化快速路径。通过 `--force-renderer-accessibility=complete` 识别 Chromium 渲染器 —— 让**整个 DOM 都以 UIA 节点形式暴露出来**，同时保留像素精度的包围盒。
- 设计了一个原创的 **"过宽 UIA 元素剔除规则"**：当一个 UIA 元素的包围盒空间上包含了两个或更多个 OCR 文本框的中心点，则在 dedup 之前直接丢弃这个外层元素 —— 修复了"一个 `<a>` 链接包住四个可点击标签"这种常见失效模式（典型例子：抖音个人主页里 `<a>` 同时包含 `关注 N`、直播中标识、`粉丝 N`、`获赞`）。
- 将视觉模型的任务从**坐标回归**转化为**从编号中选一个元素**，显著降低了在密集中文 UI 上的误点率。

## 验证码破解

- 实现了对**滑块拼图、点选特定图形、拖入阴影、旋转至正向、图片九宫格、"我不是机器人"**等验证码的通用解法，全过程不依赖任何硬编码的验证码求解器 —— 由大模型从屏幕上的中文 / 英文提示词识别验证码类型，再用通用的 `drag` / `click` 原语按拼图几何关系完成操作。
- 在 200% 入口缩放下用滑块验证码对 Gemini 3-Pro 与 Gemini 3.5-Flash 做了 A/B 测试：Flash 大约 5 次尝试内收敛，Pro 试到第 18 次仍在拖动距离上震荡；将中国区发布版默认模型切换到 Flash，单轮成本下降约 4 倍。
- 加入了**入口缩放自动策略**：进入页面时先重置缩放，再上调至 200%，让极小的验证码字形在经过视觉编码器降采样后仍然可读。

## 行为拟人化（反检测）

- 构建了 **9 项输入拟人化原语**（`humanize.py`）以对抗浏览器端的行为指纹：带垂直方向偏移的三次贝塞尔鼠标轨迹 + 亚像素微抖动；符合 **Fitts 定律**的移动时长模型（约 600 px/s 峰值速度 + ±20% 抖动）；**对英文双字符组合（bigram）加权的对数正态按键间隔**；概率性错字 + QWERTY 相邻键纠错；点击前 hover；空闲光标微移；缓入缓出计时；子任务小憩模拟。
- 实现了**滑动窗口的动作速率上限**（默认 25 次/分钟）与**跨运行持久化的每小时任务计数器**（JSON 文件存于 `%LOCALAPPDATA%` / `~/Library/Application Support`），防止失控循环触发平台反滥用系统。
- 加入了**循环中断启发式**：对最近 10 帧截图算 average-hash 的 Hamming 距离，当当前截图与最近 10 帧中至少 5 帧的 Hamming 距离 ≤6 时强制判定 `stuck` —— 避免在坏路径上白白烧掉 API 额度。

## 稳健性工程

- **分片 MP4 屏幕录制**：每个约 1 秒的 GOP 都是一个独立可索引的自包含片段，因此即便进程被 `TerminateProcess` 硬杀，录像文件仍然可解码；而默认的 `moov` 结尾式布局在 `--timeout` 杀进程后只能得到几个 G 的 `mdat`、任何播放器都无法读取的废文件。
- **运行内 done-list**：模型每完成一项就上报 `done_item`；runner 每一步都会把整个已完成列表以 "✅ ALREADY PROCESSED" 的形式回灌到 prompt 中，因此长列表遍历任务能扛住列表滚动被重置、验证码打断、页面刷新等情况。
- **环境卫生原语**：智能体启动前无条件 `Shell.MinimizeAll`（修复 `Win+R` 击键落到错误前台窗口的问题）、启动前先关闭已存在的目标应用实例、始终使用键盘启动器（Win+R / Cmd+Space）而不是点击任务栏图标，避免在 Chrome / Edge / 爱奇艺等圆形图标上误选到错误应用。
- 每回合调试配对：`mark_NN.png` + `turn_NN.md` 落盘，方便针对任意一帧同时查看当轮的模型请求 / 响应。

## 后端（Cloudflare Worker + D1 + R2）

- 用 **TypeScript** 写了一个 **Cloudflare Worker**，对外暴露 OpenAI 兼容的 Gemini 接口，并加入 bearer token 鉴权 + 预付费钱包计量：每次请求 → 校验 token → 余额已耗尽则返回 402 → 转发到上游 → 从 Gemini 的 `usage` 中计量真实用量（推理 token 按输出价率计费）→ 应用运营方基点加价 → 对 D1 执行原子 `UPDATE ... RETURNING` → 通过响应头 `X-Quota-Remaining-Usd` 回吐余额。
- 构建了一个**面向中国大陆的下载代理**：由于 `github.com` / `objects.githubusercontent.com` 在中国大陆时常被封锁 / 限速，Worker 先用 PAT 发起 GitHub Releases API 的两段式重定向流程（带鉴权 302 → 无鉴权访问签名 S3 URL），再把私有仓库产物通过 Cloudflare 边缘节点流式回传给用户。
- 搭建了 **GitHub Actions 矩阵构建**（`macos-14` + `windows-latest`）→ PyInstaller → 上传到 R2（带版本号 + `-latest` 别名双 key）→ 生成 `version.json` 清单 → 用 wrangler 部署 Astro 营销站点到 Cloudflare Pages，一切由 tag push 触发。
- 在 Mac 开发阶段还搭过一个**已退役的 FastAPI + cloudflared 桥**（每 IP 滑动窗口速率限制、通过 `asyncio.Lock` 原子写 tokens.json、逐请求 access log），随后迁移到 Worker 上。
- **设计了远程优先的 RAG 层**，直接嵌入现有 Worker 请求路径，客户端零额外往返、零包体积增量：把 `app/knowledge.md` 按 `## App:` 标题以及跨站点通用规则切成 300–500 token 的 chunk，正文存 **D1**，向量存 **Cloudflare Vectorize**（按从截图 OCR / 地址栏推断出的当前应用做元数据过滤），每回合服务端取 top-K，然后在转发上游前拼装最终 prompt。用相关的 300–500 token 片段替换当前 `knowledge.md` 约 4K token 的整段注入 —— 显著降低每回合的 token 成本，客户端感知不到任何延迟变化。明确否决了本地优先方案（SQLite + `sqlite-vec` + 设备端 MiniLM ONNX）—— 判断依据是：本项目每次视觉调用都必须联网，"离线能力"并不成立，本地化只会平白增加复杂度。写入流程是一个 `pnpm knowledge:push` 脚本（`worker/` 目录下的 Node/TS），diff D1、通过托管嵌入 API 嵌入变更 chunk 后原子写入 D1 + Vectorize —— 修一条知识几秒钟上线，无需重新构建客户端。

## 客户端 UI（PySide6，约 950 行）

- 暗色主题的桌面 UI，带**逐回合调试面板**：把截图缩略图 + 用户 prompt + 模型响应作为一张卡片渲染，并附带 "Replay move to (x, y)" 按钮 —— 一键把真实光标平滑移动到模型选定的坐标，用于像素级校对。
- **无边框、置顶的悬浮状态气泡**：智能体运行时展示实时步骤状态，让演示观众能看到每一步而不遮挡工作区；点击气泡可以恢复主窗口，也可以拖拽移动位置。
- 钱包余额一栏使用**脱敏 token 预览**（`pc_xxxx…yyyy`），即便演示时开启录屏也不会把完整 token 曝光。
- 通过 `aboutToQuit` + `closeEvent` 铺设了**干净的关闭链路**：若视觉调用正阻塞在 `urllib` 的 socket recv 上无法及时取消，直接 `os._exit` 硬退出，避免 `QThread` 析构器看到还在运行的线程时触发 SIGABRT。

## 回归测试

- 编写了 **5 个基线任务的测试套件**（`tools/baseline_tests.sh`），覆盖所有生产路径：中文社交图谱遍历（抖音关注列表按属性筛选并取关）、带鉴权的 web 邮件（Gmail 给自己发信）、复杂第三方站点导航（悉尼 → 广州机票查询）、验证码破解、按粉丝数筛选后向 10 位大 V 发中文私信。每个任务都通过 headless 的 `run_and_review.py` 驱动，落盘保存截图、标注帧、逐回合模型 dump 与整段运行的 MP4。

## 技术栈

**语言：** Python 3.11、TypeScript、PowerShell、Bash、Astro / HTML / CSS
**AI / CV：** Claude、Gemini、Kimi / Moonshot、RapidOCR、ONNX Runtime、OpenCV
**框架：** PySide6 / Qt、FastAPI、Playwright、PyInstaller
**基础设施：** Cloudflare Workers、D1（SQLite）、R2、Pages、cloudflared 隧道、GitHub Actions
**平台 API：** Windows UI Automation（通过 comtypes）、pywin32 / Win32（SendInput、AttachThreadInput、ShowWindow、`Shell.MinimizeAll`）、Quartz CGEvent、mss、ffmpeg、`--force-renderer-accessibility`

## 成果导向的表述（如果你的简历更偏"结果说话"）

- 独立开发并交付了一个跨平台、视觉优先的 computer-use 智能体（**约 1 万行代码**），能只凭截图 + 视觉大模型可靠地驱动任意桌面与 Web 软件，包括中国原生应用与四大主流验证码。
- 实现了一条**无需检测面暴露的元素定位管线**，融合 OCR、边缘轮廓提议与 Windows UI Automation，显著降低了密集多语种 UI 上的误点率，并将 Chromium DOM 作为一等公民接入到可访问性结构中。
- 通过在验证码路径上对视觉模型做 A/B 测试，把中国发布版默认模型从 Gemini Pro 切到 Flash，在不损失任务成功率的前提下把单轮 API 成本降低了**约 4 倍**。
- 设计了一整套**基于 Cloudflare 的后端**（Worker + D1 钱包 + R2 + Pages + 通过边缘反代的私有仓库下载），能够绕过 GitHub 在中国大陆的时断时续，稳定分发给国内用户，同时支持原子化的预付费计量与按模型的差异化加价。
- **在架构决策上敢于反直觉** —— 把 RAG 层设计为**远程优先、嵌入现有 Worker 请求路径**，而不是照搬桌面 AI 应用常见的本地优先模式；判断依据是本项目每回合都需要联网做视觉调用，"离线能力"根本不成立，本地化只会徒增复杂度。副产物：`pnpm knowledge:push` 几秒钟就能上线一条新知识，客户端完全不需要重新构建。
