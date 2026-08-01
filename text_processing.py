# text_processing.py
# 文本检测、Markdown 渲染、对话格式化、换行处理

import html as html_lib
import re
from typing import List, Optional, Tuple

from astrbot.api import logger

# ==================== Markdown 渲染支持 ====================

_markdown_renderer = None
MARKDOWN_AVAILABLE = False
_CODE_TOKEN_PREFIX = "ASTRCODETOKEN"
_INLINE_MATH_TOKEN_PREFIX = "ASTRINLINEMATHTOKEN"
_FENCED_CODE_PATTERN = re.compile(r"```[\s\S]*?```")
_INLINE_CODE_PATTERN = re.compile(r"`[^`\n]+`")
_DISPLAY_MATH_PATTERNS = [
    re.compile(r"(?<!\\)\$\$[\s\S]+?(?<!\\)\$\$", re.DOTALL),
    re.compile(r"\\\[[\s\S]+?\\\]", re.DOTALL),
    re.compile(r"\\begin\{([a-zA-Z*]+)\}[\s\S]+?\\end\{\1\}", re.DOTALL),
]
_INLINE_MATH_PATTERNS = [
    re.compile(r"(?<!\\)\$(?!\$)(.+?)(?<!\\)\$(?!\$)"),
    re.compile(r"\\\(.+?\\\)"),
]


def _make_placeholder(prefix: str, index: int) -> str:
    return f"{prefix}{index}END"


def _protect_segments(text: str, patterns: List[re.Pattern], prefix: str) -> Tuple[str, List[str]]:
    segments: List[str] = []

    def _replace(match: re.Match) -> str:
        segments.append(match.group(0))
        return _make_placeholder(prefix, len(segments) - 1)

    for pattern in patterns:
        text = pattern.sub(_replace, text)

    return text, segments


def _restore_segments(text: str, segments: List[str], prefix: str) -> str:
    for idx, segment in enumerate(segments):
        text = text.replace(_make_placeholder(prefix, idx), segment)
    return text


def _escape_math_fragment(fragment: str) -> str:
    return html_lib.escape(fragment, quote=False)


def _prepare_math_for_markdown(text: str) -> Tuple[str, List[str]]:
    """
    Protect code first, then keep LaTeX intact across Markdown rendering.
    Display math becomes raw HTML blocks before Markdown parsing;
    inline math is restored after Markdown so it can live inside emphasis, links, etc.
    """
    text, code_segments = _protect_segments(
        text, [_FENCED_CODE_PATTERN, _INLINE_CODE_PATTERN], _CODE_TOKEN_PREFIX
    )

    for pattern in _DISPLAY_MATH_PATTERNS:
        text = pattern.sub(
            lambda m: (
                "\n"
                f'<div class="astr-math-block">{_escape_math_fragment(m.group(0))}</div>'
                "\n"
            ),
            text,
        )

    inline_math_segments: List[str] = []

    def _replace_inline_math(match: re.Match) -> str:
        inline_math_segments.append(
            f'<span class="astr-math-inline">{_escape_math_fragment(match.group(0))}</span>'
        )
        return _make_placeholder(_INLINE_MATH_TOKEN_PREFIX, len(inline_math_segments) - 1)

    for pattern in _INLINE_MATH_PATTERNS:
        text = pattern.sub(_replace_inline_math, text)

    text = _restore_segments(text, code_segments, _CODE_TOKEN_PREFIX)
    return text, inline_math_segments

try:
    import mistune

    if hasattr(mistune, "create_markdown"):
        # mistune 2.x / 3.x
        try:
            _markdown_renderer = mistune.create_markdown(
                escape=False, hard_wrap=True, plugins=["table", "strikethrough"]
            )
        except (TypeError, KeyError):
            try:
                _markdown_renderer = mistune.create_markdown(
                    escape=False, hard_wrap=True
                )
            except TypeError:
                _markdown_renderer = mistune.create_markdown()
                logger.warning("HTML渲染插件: 当前 mistune 版本可能不保留内联 HTML")
    elif hasattr(mistune, "Markdown"):
        # mistune 0.x
        _markdown_renderer = mistune.Markdown()
    else:
        _markdown_renderer = mistune.html

    MARKDOWN_AVAILABLE = True
    logger.info(
        f"HTML渲染插件: mistune {getattr(mistune, '__version__', 'unknown')} 初始化成功"
    )
except ImportError:
    MARKDOWN_AVAILABLE = False
    logger.warning(
        "HTML渲染插件: mistune 未安装，Markdown 渲染功能将不可用。"
        "可通过 pip install mistune 安装。"
    )


# ==================== 文本检测 ====================


def detect_render_tag(text: str) -> List[Tuple[str, Optional[str], str, bool]]:
    """
    检测 <render> 标签。

    :return: List[(完整匹配, 模板名|None, 内容, 是否GIF)]
    """
    pattern = re.compile(r"<render\b([^>]*)>(.*?)</render>", re.DOTALL | re.IGNORECASE)
    template_pattern = re.compile(
        r"""template\s*=\s*(?:
            "([^"]+)" |
            '([^']+)' |
            \\"([^"]+)\\" |
            \\'([^']+)\\'
        )""",
        re.IGNORECASE | re.VERBOSE,
    )
    gif_pattern = re.compile(r"(?:^|\s)gif(?:\s|$)", re.IGNORECASE)

    result = []
    for match in pattern.finditer(text):
        attrs = match.group(1) or ""
        content = match.group(2).strip()

        template_name = None
        template_match = template_pattern.search(attrs)
        if template_match:
            template_name = next(
                (group for group in template_match.groups() if group),
                None,
            )

        is_gif = bool(gif_pattern.search(attrs))
        full_match = match.group(0)
        result.append((full_match, template_name, content, is_gif))

    return result


def detect_html_tags(text: str) -> bool:
    """检测是否包含 HTML 标签（排除 <render>）"""
    html_pattern = (
        r"<(?!render\b)"
        r"(div|span|p|h[1-6]|table|ul|ol|li|a|img|style|br|hr|pre|code)"
        r"\b[^>]*>"
    )
    return bool(re.search(html_pattern, text, re.IGNORECASE))


def detect_dialogue(
    text: str,
    quote_pattern: str = "[\"'“”‘’「」『』]",
    quote_threshold: int = 1,
) -> bool:
    """检测是否是对话内容（包含多个引号对）"""
    try:
        quote_threshold = max(1, int(quote_threshold))
    except (TypeError, ValueError):
        quote_threshold = 1

    try:
        quotes = re.findall(quote_pattern, text)
    except (re.error, TypeError) as exc:
        logger.warning(f"[HTML渲染] 对话引号匹配模式无效: {exc}")
        return False
    return len(quotes) >= quote_threshold * 2


def contains_math(text: str) -> bool:
    """Detect common LaTeX/math delimiters outside code blocks."""
    if not text:
        return False

    protected_text, _ = _protect_segments(
        text, [_FENCED_CODE_PATTERN, _INLINE_CODE_PATTERN], _CODE_TOKEN_PREFIX
    )
    return any(
        pattern.search(protected_text)
        for pattern in (_DISPLAY_MATH_PATTERNS + _INLINE_MATH_PATTERNS)
    )


# ==================== 换行与格式处理 ====================


def preserve_newlines(text: str) -> str:
    """
    保留文本中的换行符，将 \\n 转换为 <br> 或 <p> 标签。
    若已包含 HTML 块级标签则直接返回。
    """
    if re.search(r"<(p|div|br|table|ul|ol|li|h[1-6])\b", text, re.IGNORECASE):
        return text

    lines = text.split("\n")
    if len(lines) == 1:
        return text

    result_parts: List[str] = []
    current_paragraph: List[str] = []

    for line in lines:
        stripped = line.strip()
        if stripped:
            current_paragraph.append(stripped)
        else:
            if current_paragraph:
                result_parts.append("<br>".join(current_paragraph))
                current_paragraph = []

    if current_paragraph:
        result_parts.append("<br>".join(current_paragraph))

    if len(result_parts) > 1:
        return "".join(f"<p>{p}</p>" for p in result_parts)
    elif result_parts:
        return result_parts[0]
    else:
        return text


def nl2br(html: str) -> str:
    """
    统一换行处理：保留空行（\\n\\n → <br><br>），
    消除标签间缩进换行，保护 <style>/<script>/<pre>/<code>。
    """
    if not html:
        return html

    html = html.replace("\r\n", "\n").replace("\r", "\n")

    protected_blocks: List[str] = []

    def _protect(m: re.Match) -> str:
        protected_blocks.append(m.group(0))
        return f"__ASTR_HTML_RENDER_PROTECTED_{len(protected_blocks) - 1}__"

    html = re.sub(
        r"<style\b[^>]*>[\s\S]*?</style>", _protect, html, flags=re.IGNORECASE
    )
    html = re.sub(
        r"<script\b[^>]*>[\s\S]*?</script>", _protect, html, flags=re.IGNORECASE
    )
    html = re.sub(
        r"<pre\b[^>]*>[\s\S]*?</pre>", _protect, html, flags=re.IGNORECASE
    )
    html = re.sub(
        r"<code\b[^>]*>[\s\S]*?</code>", _protect, html, flags=re.IGNORECASE
    )

    # 消除标签间单行缩进换行
    html = re.sub(r">[ \t]*\n[ \t]*<", "><", html)

    parts = re.split(r"(<[^>]+?>)", html)
    out: List[str] = []

    for seg in parts:
        if seg.startswith("<"):
            out.append(seg)
            continue

        seg = re.sub(r"\n{3,}", "\n\n", seg)

        if seg.strip() == "":
            if seg.count("\n") >= 2:
                out.append("<br><br>")
            continue

        seg = seg.replace("\n\n", "<br><br>")
        seg = seg.replace("\n", "<br>")
        out.append(seg)

    result = "".join(out)
    result = re.sub(r"(?:<br>){3,}", "<br><br>", result)

    for i, block in enumerate(protected_blocks):
        result = result.replace(f"__ASTR_HTML_RENDER_PROTECTED_{i}__", block)

    return result


# ==================== 原始 HTML 混合内容换行处理 ====================

# 标准 HTML 块级/结构标签：它们之间的空行视为源码排版，不产生 <br>
_RAW_BLOCK_LEVEL_TAGS = frozenset({
    "html", "head", "body", "meta", "link", "title", "base",
    "div", "section", "article", "header", "footer", "main", "nav",
    "address", "aside",
    "p", "ul", "ol", "li", "dl", "dt", "dd",
    "table", "thead", "tbody", "tfoot", "tr", "td", "th",
    "caption", "colgroup", "col",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "blockquote", "hr", "br", "form", "fieldset", "legend",
    "figure", "figcaption", "details", "summary", "template",
    "canvas", "video", "audio", "iframe", "source",
    "style", "script", "noscript",
    # 插件语义标签中约定为块级的：所有内置模板都以 display:block 渲染，
    # 它们自带换行与外边距，不需要额外 <br> 分段
    "scene", "narration",
})

_RAW_PROTECTED_TOKEN_FMT = "<x-astr-raw-protected-{}/>"
_RAW_PROTECTED_TOKEN_RE = re.compile(r"<x-astr-raw-protected-(\d+)/>")


def _raw_tag_kind(tag: Optional[str]) -> str:
    """
    判断标签类型：
    - "block": 标准块级/结构标签、注释、受保护占位块（空行视为排版）
    - "inline": 行内标签或自定义语义标签（act/q/scene 等，空行视为分段）
    - "boundary": 内容开头/结尾
    """
    if tag is None:
        return "boundary"
    if tag.startswith("<!") or tag.startswith("<x-astr-raw-protected-"):
        return "block"
    m = re.match(r"</?\s*([a-zA-Z][a-zA-Z0-9-]*)", tag)
    if not m:
        return "block"
    return "block" if m.group(1).lower() in _RAW_BLOCK_LEVEL_TAGS else "inline"


def nl2br_raw_html(html: str) -> str:
    """
    针对「自带 <style> 的原始 HTML + 纯文本混合输出」的换行修复。

    浏览器会把源码中的换行折叠成空格，导致混合输出里
    <act>/<q> 等行内语义标签之间的空行分段全部挤在一起。

    处理规则（保护 <style>/<script>/<pre>/<code>/<textarea> 不受影响）：
    - 任一侧为块级标签（div/p/scene 等）的空白 → 布局自带换行，删除
    - 两侧均为行内/自定义标签时：空行（>=2 个换行）→ <br><br>，
      单个换行 → 一个空格（视为软换行）
    - 文本段内部：空行 → <br><br>，单个换行 → <br>
    - 文本与行内标签相邻处的空行 → <br><br>，单个换行 → 空格
    - 不含换行的纯空格保持原样（行内元素间距有意义）
    """
    if not html:
        return html

    html = html.replace("\r\n", "\n").replace("\r", "\n")

    protected_blocks: List[str] = []

    def _protect(m: re.Match) -> str:
        protected_blocks.append(m.group(0))
        return _RAW_PROTECTED_TOKEN_FMT.format(len(protected_blocks) - 1)

    for tag in ("style", "script", "pre", "code", "textarea"):
        html = re.sub(
            rf"<{tag}\b[^>]*>[\s\S]*?</{tag}>", _protect, html, flags=re.IGNORECASE
        )

    parts = re.split(r"(<[^>]+?>)", html)
    n = len(parts)
    out: List[str] = []

    for i, seg in enumerate(parts):
        # re.split 捕获组模式下，奇数下标为标签，偶数下标为文本
        if i % 2 == 1:
            out.append(seg)
            continue
        if seg == "":
            continue

        prev_kind = _raw_tag_kind(parts[i - 1]) if i - 1 >= 0 else "boundary"
        next_kind = _raw_tag_kind(parts[i + 1]) if i + 1 < n else "boundary"

        # 纯空白文本段（位于两个标签之间）
        # 只要一侧是块级标签，布局上必然换行（并自带外边距），
        # 无需插入 <br>；只有两侧都是行内元素时才需要人为分段
        if seg.strip() == "":
            newline_count = seg.count("\n")
            if newline_count == 0:
                out.append(seg)  # 纯空格：行内元素间距，保留
            elif prev_kind != "inline" or next_kind != "inline":
                pass  # 邻接块级标签/边界：换行由布局保证，删除源码排版空白
            elif newline_count >= 2:
                out.append("<br><br>")
            else:
                out.append(" ")
            continue

        # 含内容的文本段：分别处理首部空白、尾部空白和内部换行
        lead_match = re.match(r"^[ \t]*\n[ \t\n]*", seg)
        core_start = lead_match.end() if lead_match else 0
        trail_match = re.search(r"[ \t\n]*\n[ \t]*$", seg[core_start:])
        if trail_match:
            core_end = core_start + trail_match.start()
        else:
            core_end = len(seg)

        def _edge(ws: str, kind: str) -> str:
            if not ws or kind != "inline":
                return ""  # 邻接块级标签/内容边界：视为排版空白
            if ws.count("\n") >= 2:
                return "<br><br>"
            return " "

        core = seg[core_start:core_end]
        core = re.sub(r"\n{3,}", "\n\n", core)
        core = re.sub(r"[ \t]*\n[ \t]*\n[ \t]*", "<br><br>", core)
        core = re.sub(r"[ \t]*\n[ \t]*", "<br>", core)

        out.append(_edge(seg[:core_start], prev_kind))
        out.append(core)
        out.append(_edge(seg[core_end:], next_kind))

    result = "".join(out)
    result = re.sub(r"(?:<br>\s*){3,}", "<br><br>", result)

    def _restore(m: re.Match) -> str:
        idx = int(m.group(1))
        if 0 <= idx < len(protected_blocks):
            return protected_blocks[idx]
        return m.group(0)

    result = _RAW_PROTECTED_TOKEN_RE.sub(_restore, result)
    return result


# ==================== Markdown / 表格 转换 ====================


def markdown_to_html(text: str) -> str:
    """将 Markdown 转换为 HTML"""
    if not MARKDOWN_AVAILABLE or _markdown_renderer is None:
        return preserve_newlines(text)

    try:
        prepared_text, inline_math_segments = _prepare_math_for_markdown(text)
        html = _markdown_renderer(prepared_text)
        html = _restore_segments(html, inline_math_segments, _INLINE_MATH_TOKEN_PREFIX)
        logger.debug(
            f"[Markdown] 渲染成功，输入长度: {len(text)}, 输出长度: {len(html)}"
        )
        return html
    except Exception as e:
        logger.error(f"Markdown 渲染失败: {e}")
        import traceback

        logger.error(traceback.format_exc())
        return preserve_newlines(text)


def convert_markdown_tables(text: str) -> str:
    """
    将 Markdown 表格转换为 HTML 表格（用于混合内容场景）。

    注意：此函数当前未被调用，保留作为可选工具。
    """
    lines = text.split("\n")
    result = []
    i = 0

    while i < len(lines):
        line = lines[i]

        if "|" in line and i + 1 < len(lines):
            next_line = lines[i + 1]
            if re.match(
                r"^\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)+\|?\s*$", next_line
            ):
                table_lines = [line, next_line]
                i += 2
                while i < len(lines) and "|" in lines[i]:
                    if not re.match(
                        r"^\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$", lines[i]
                    ):
                        table_lines.append(lines[i])
                    i += 1
                result.append(_parse_markdown_table(table_lines))
                continue

        result.append(line)
        i += 1

    return "\n".join(result)


def _parse_markdown_table(lines: List[str]) -> str:
    """解析 Markdown 表格并生成 HTML"""
    if len(lines) < 2:
        return "\n".join(lines)

    def parse_row(line: str) -> List[str]:
        line = line.strip()
        if line.startswith("|"):
            line = line[1:]
        if line.endswith("|"):
            line = line[:-1]
        return [cell.strip() for cell in line.split("|")]

    header_cells = parse_row(lines[0])
    body_rows = [parse_row(line) for line in lines[2:]]

    html = [
        '<table style="border-collapse:collapse;width:100%;margin:1em 0;font-size:14px;">'
    ]

    html.append("<thead><tr>")
    for cell in header_cells:
        html.append(
            f'<th style="border:1px solid #d4c4a8;padding:8px 12px;'
            f'background:#f5f0e6;text-align:left;font-weight:600;">{cell}</th>'
        )
    html.append("</tr></thead>")

    html.append("<tbody>")
    for row in body_rows:
        html.append("<tr>")
        for cell in row:
            html.append(
                f'<td style="border:1px solid #d4c4a8;padding:8px 12px;'
                f'background:#fffef9;">{cell}</td>'
            )
        html.append("</tr>")
    html.append("</tbody></table>")

    return "".join(html)


# ==================== 对话格式化 ====================


def format_dialogue(text: str) -> str:
    """
    将文本转换为混合对话+描述的 HTML 结构。
    - 引号内容 → 对话气泡
    - 引号外内容 → 叙事描述
    """
    pattern = r'[""「]([^""」]+)[""」]'

    parts = []
    last_end = 0
    is_right = False

    for match in re.finditer(pattern, text):
        before = text[last_end : match.start()].strip()
        if before:
            before_clean = re.sub(r"^\(|\)$", "", before).strip()
            if before_clean:
                parts.append(
                    f'<div class="narration">{preserve_newlines(before_clean)}</div>'
                )

        dialogue = match.group(1).strip()
        side_class = "right" if is_right else ""
        parts.append(f'<div class="bubble {side_class}">{dialogue}</div>')
        is_right = not is_right
        last_end = match.end()

    remaining = text[last_end:].strip()
    if remaining:
        remaining_clean = re.sub(r"^\(|\)$", "", remaining).strip()
        if remaining_clean:
            parts.append(
                f'<div class="narration">{preserve_newlines(remaining_clean)}</div>'
            )

    if not parts:
        return f'<div class="narration">{preserve_newlines(text)}</div>'

    return "\n".join(parts)
