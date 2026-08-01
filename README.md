# AstrBot HTML Render Plugin

[![AstrBot](https://img.shields.io/badge/AstrBot-Plugin-blue)](https://github.com/Soulter/AstrBot)
[![Python](https://img.shields.io/badge/Python-3.9+-green)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-yellow)](LICENSE)

把 AI 返回的文本、Markdown 或自定义 HTML 渲染成图片发送，支持 GIF、数学公式、模板热更新和自动渲染。

## 功能

- 支持外部 HTML 模板，修改 `templates/*.html` 后重新渲染即可生效
- 支持 Markdown 渲染，保留普通换行、代码块、列表、引用等结构
- 支持 LaTeX 数学公式，兼容 `$...$`、`$$...$$`、`\(...\)`、`\[...\]`
- 支持 `<render gif>` 录制 CSS 动画为 GIF
- 支持自动渲染全部回复，并可用 `auto_render_min_length` 跳过过短文本
- 支持按用户切换默认模板
- 支持模板选择策略：固定 / 每次随机 / 轮流切换（`template_strategy`）
- 支持模板变量：`{{date}}`、`{{time}}`、`{{weekday}}`、`{{sender_name}}`、`{{sender_id}}`、`{{platform}}`
- 支持可独立启停的子 HTML 面板：AI 只输出固定 JSON 协议，插件负责套用面板 HTML/CSS
- 子面板支持多个同时启用、按需选择、字段白名单、枚举模式和安全十六进制主色调
- 本地字体离线加载：Google Fonts 请求由 `fonts/` 目录直接响应，无需外网

## 内置模板一览

| 模板 | 风格 | 适合场景 |
| ---- | ---- | -------- |
| 玻璃花房 | 极光渐层 + 玻璃拟态 | 日常问答、清新场景 |
| 暖窗手札 | 信纸 + 邮票火漆（含收件人变量） | 温情向、书信体 |
| 水墨青韵 | 宣纸立轴 + 远山朱印 | 诗词、古风、国学 |
| 霓虹终端 | 赛博朋克终端窗口（自带动画） | 技术内容、科幻 |
| 复古印刷 | 老报纸铅字排印（含日期变量） | 资讯播报、正式感 |
| 像素旅人 | 复古 RPG 对话框 + HP/MP 状态栏 | 游戏向、轻松日常 |
| 魔女特辑 / 奶油蝴蝶结 / 手账 / 随时间变化动态面板 / nightsky / novel | 原有模板 | — |

## 重要变更

- 插件不再内置任何默认模板，也不会自动生成 `card/dialogue/novel`
- `templates` 目录里如果没有任何 `.html` 模板，渲染会直接报错
- `default_template` 和 `auto_render_template` 如果配置了不存在的模板，也会直接报错
- 仪表盘中的模板配置项会根据 `templates` 目录自动生成下拉选项，不需要手动输入模板名

## 安装

1. 安装插件到 `data/plugins/astrbot_plugin_html_render`

2. 安装依赖：

```bash
python -m pip install -r requirements.txt
python -m playwright install chromium
```

其中，`playwright` 是 Python 调用库，Chromium 是它实际使用的浏览器内核，两者需要分别安装。插件启动时会自动尝试执行浏览器内核安装命令；如果因为网络、权限或运行环境问题没有安装成功，请在运行 AstrBot 的同一个 Python 环境中手动执行上面的两条命令。

Windows 如果使用虚拟环境，请先激活该环境，或直接指定 AstrBot 使用的 Python：

```powershell
.\venv\Scripts\python.exe -m pip install -r requirements.txt
.\venv\Scripts\python.exe -m playwright install chromium
```

如果 `playwright install chromium` 下载失败，可以在能正常联网的环境中重试；安装成功后，Playwright 会将 Chromium 保存到本机用户缓存目录，插件无需额外配置浏览器路径。

3. 在插件目录准备至少一个模板文件：

```text
astrbot_plugin_html_render/
  templates/
    your_template.html
  avatars/
    女主.png
    男主.jpg
```

模板文件中需要包含 `{{content}}` 占位符，例如：

```html
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <style>
    body {
      margin: 0;
      padding: 24px;
      background: #f6f2e9;
      font-family: "Microsoft YaHei", sans-serif;
    }
    .card {
      background: white;
      border-radius: 16px;
      padding: 24px;
      line-height: 1.8;
      box-shadow: 0 12px 32px rgba(0, 0, 0, 0.12);
    }
  </style>
</head>
<body>
  <div class="card">{{content}}</div>
</body>
</html>
```

## 配置示例

```yaml
inject_prompt: true

# 模板选择
default_template: ""               # 不填则自动使用第一个可用模板
template_strategy: "fixed"         # fixed=固定, random=每次随机, round_robin=轮流切换
auto_render_template: ""           # 不填则回落到当前默认模板
auto_dialogue_detection: true
dialogue_quote_threshold: 1
dialogue_quote_pattern: "[\"'“”‘’「」『』]"

# 渲染参数
# 主模板会在该视口宽度的可用区域内自动拉伸，避免宽度变大后卡片仍固定在 520px
render_width: 600
render_scale: 2
enable_markdown: true
enable_math: true

# 自动渲染
auto_render_all: true
auto_render_min_length: 20         # 低于该长度不自动渲染
enable_auto_detect: true
auto_merge_renders: true
merged_template: ""                # 不填时优先沿用同一显式模板，否则回落当前默认模板
preserve_text_for_context: true

# GIF
gif_duration: 3.0
gif_fps: 15
gif_scale: 2

# 背景图
background_image: ""
background_image_strategy: "fixed" # fixed=固定, round_robin=轮询, random=随机
background_render_mode: "ambient" # ambient=页面氛围背景, watermark=正文内容水印
background_opacity: 0.17          # 透明度，取值 0-1
```

### 子 HTML 面板与行内模板

配置文件中的 `sub_html_panels` 使用和世界书相同的 `template_list` 结构。每个条目可以是状态面板，也可以是行内子模板：状态面板按 `panel_id` 去重并保留最近状态；行内子模板不保存状态、不去重，每个标签都在原文出现的位置展开。多个行内子模板可以混用，同一个行内子模板也可以重复使用。

面板模板放在插件目录内，例如内置的 `sub_html_panels/R18沉浸状态栏.html`。AI 不需要重复输出 HTML，只需输出：

```html
<panel id="r18_status">{"mode":"rescue","theme_color":"#39c7a4","character":"塞西莉亚","affection":72}</panel>
```

当面板的输出时机选择“每次回复”时，首次输出完整数据，后续可以只发送变化字段：`<panel id="r18_status" update>{"affection":80}</panel>`；没有变化时也应发送 `<panel id="r18_status" update>{}</panel>`。如果主模型漏掉该面板，插件会参考当前回复和最近状态独立请求一次补全；补全失败时会自动插入空 `update` 并沿用最近状态，因此该模式会保证每轮展示。插件会按会话保留未变化字段；同一回复中同一面板出现多次时只保留最后一次。面板协议不会写入模型上下文，旧历史中的 `update` 数据也会在下一次请求前清理。

字段规范每行格式为 `字段键|显示名|类型|说明|选项`，类型支持 `text`、`number`、`percent`、`boolean`、`choice`、`color` 和 `image`。其中 `choice` 的选项写成 `key=名称,key2=名称2`；`color` 只接受 `#RGB` 或 `#RRGGBB`，`image` 只从插件的 `avatars/` 文件夹读取图片。模板可用 `{{color:字段键}}`、`{{image:字段键}}`，以及 `{{value:字段键}}`、`{{percent:字段键}}`、`{{token:字段键}}`。

内置的“头像对话”是行内子模板，模板文件为 `sub_html_panels/头像对话.html`。把头像放进插件目录的 `avatars/` 文件夹，支持 PNG、JPG、JPEG、WEBP 和 GIF。AI 在正文需要的位置输出：

```html
<render template="手账">
<scene>雨停后，庭院里只剩下水声。</scene>

<panel id="avatar_dialogue">{"accent":"#b8242a","avatar":"女主.png","name":"苏雪见","speech":"你终于来了……","inner":"她没有再躲开视线。"}</panel>

<act>他收起雨伞，向她走近。</act>

<panel id="avatar_dialogue">{"accent":"#2e9e8f","avatar":"男主.jpg","name":"林默","speech":"我答应过你。","inner":"这次不会再失约。"}</panel>
</render>
```

`<panel>` 标签的位置就是组件在最终图片中的位置；不要把所有子模板集中到回复末尾。头像文件名只会在 `avatars/` 目录内解析，路径越界或文件不存在时会安全降级为空头像。

插件会自动分析当前主模板内的 CSS 配色，提取最匹配的推荐主题色与协调色板，并随子面板协议暴露给模型。当子面板包含 `color` 字段且当前情境允许自定义配色时，模型会被明确要求优先使用与主模板一致或接近的色调；如果状态语义需要明显区分，仍可选择其他颜色。

R18 面板内置 `punishment` / `rescue` 两种模式，并允许 AI 通过 `theme_color` 按情境选择主色调。需要完全不同的视觉风格时，直接为另一个面板配置不同的 `.html` 文件即可；也可以用配置里的“自定义 HTML”覆盖文件内容。

新增的 `sub_html_panels/私密经验记录.html` 是深紫游戏菜单风格的精简预设：顶部保留淫乱等级、淫欲经验、贞操和经历人数；经验区只包含自慰、性相关服务、肛交、卖春、羞耻、性交、浴精和乱交 8 项；底部提供口部、胸部、私处和臀部 4 个身体部位的文字描述。它在默认配置中关闭，可在“子 HTML 面板”中启用。

内置的 `sub_html_panels/甜心猫咪状态面板.html` 仿照柔和的角色好感度卡片设计，包含角色短句、心情值、好感度、互动提醒和底部寄语。该示例在默认配置中处于关闭状态，可在“子 HTML 面板”中启用；`theme_color` 不填写时使用粉色 `#f472a8`，也可传入任意合法十六进制颜色：

```html
<panel id="cat_status">{"theme_color":"#f472a8","message":"可以一直陪着我吗……？","mood":82,"liking":92,"reminder_1":"记得摸摸她的头哦","reminder_2":"多和她聊天，她会更依赖你","reminder_3":"带她去吃好吃的","reminder_4":"不要突然消失","footer_quote":"你是小猫最重要的人喵～"}</panel>
```

## 使用方式

### 命令

- `/测试 <文本>`：测试当前模板渲染效果
- `/切换 <模板名或ID>`：切换自己的默认模板
- `/查看`：查看当前可用模板列表
- `/预览模板 <模板名或ID> [文本]`：临时预览指定模板
- `/html开`、`/html关`、`/html状态`、`/html重置`：管理当前会话的 HTML 渲染开关

`/html关` 会停止当前会话的渲染提示词注入、自动渲染和渲染历史修正；`/html重置` 会移除会话覆盖，恢复全局默认状态。

### 手动渲染

````html
<render template="your_template">
第一行
第二行

```python
print("Hello")
```

行内公式 $a^2+b^2=c^2$
</render>
````

### GIF 渲染

```html
<render gif>
<style>
.bar {
  width: 120px;
  height: 12px;
  border-radius: 999px;
  background: #ddd;
  animation: pulse 1.2s ease-in-out infinite;
}
@keyframes pulse {
  0%, 100% { transform: scaleX(0.6); opacity: 0.4; }
  50% { transform: scaleX(1); opacity: 1; }
}
</style>
<div class="bar"></div>
</render>
```

## 模板策略与模板变量

- `template_strategy` 控制自动渲染时默认模板的挑选方式：`fixed` 固定使用 `default_template`；`random` 每次在全部模板中随机；`round_robin` 逐条轮换
- 用户通过 `/切换` 设置的个人模板优先级高于策略；显式 `<render template="...">` 永远最优先
- 模板文件中可以直接写以下占位符，渲染前自动替换：
  - `{{date}}` → `2026年07月26日`　`{{time}}` → `21:30`　`{{weekday}}` → `星期日`
  - `{{sender_name}}` → 触发渲染的用户昵称（取不到时为"朋友"）
  - `{{sender_id}}`、`{{platform}}` → 用户 ID 与平台名
- 占位符替换发生在正文插入之前，正文中出现同名字面量不会被误替换

## 本地字体说明

- 模板里的 `fonts.googleapis.com` 样式表请求会被渲染器拦截，并直接用 `fonts/google_fonts_original.css` 响应；字体文件请求（`fonts.gstatic.com`）再由 `fonts/manifest.json` 映射到本地 `.woff2`，全程无需外网
- 首次使用或新增字体时运行 `python download_fonts.py` 重新生成缓存
- 若无本地缓存，字体请求会被阻断并回退系统字体（与旧行为一致）

## Markdown 与公式说明

- Markdown 路径已经修复“代码块导致其它内容换行丢失”的问题
- 普通单换行会保留下来，不会再因为 fenced code block 挤成一段
- 数学公式通过前端 MathJax 渲染，需要运行环境能访问 `https://cdn.jsdelivr.net`

## 背景图说明

- `background_image` 通过配置页下拉框选择插件目录里的图片资源
- `background_image_strategy` 控制背景图切换策略：`fixed` 使用当前选中的图片，`round_robin` 和 `random` 会在全部可用背景图中切换
- `background_render_mode: ambient` 会把图片作为页面外层氛围背景，适合做边缘衬底
- `background_render_mode: watermark` 会把图片压到 `.content` 内容区内部作为正文水印，能绕开大多数模板自带的实底容器
- `background_opacity` 用来控制背景图透明度，`watermark` 模式通常 `0.10-0.25` 比较合适

## 注意事项

- 没有模板文件时，插件不会再偷偷回退到内置样式
- 模板文件请统一保存为 UTF-8 编码
- 如果配置了模板名，文件名必须与配置完全一致，不需要写 `.html`
- 自定义 HTML 模式下，检测到 `<style>` 后会跳过 Markdown 处理，直接嵌入模板渲染
