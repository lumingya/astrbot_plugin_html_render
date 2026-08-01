# main.py
# 插件入口：HtmlRenderPlugin 主类 + 命令 + 事件处理

import asyncio
import hashlib
import json
import os
import random
import re
import sys
import unicodedata
import uuid
import base64
from typing import Dict, List, Optional
from PIL import Image as PILImage

# AstrBot 以 data.plugins.<plugin>.main 的包路径加载插件。使用包内导入，
# 避免把插件目录加入全局 sys.path 后污染其他插件的模块解析。
_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api.message_components import Image, Plain
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.provider.entities import ProviderRequest
from astrbot.core.star.star_tools import StarTools

if __package__:
    from .renderer import html_to_image_playwright, init_browser, close_browser
    from .sub_html_panels import SubHtmlPanelManager
    from .template_manager import TemplateManager
    from . import text_processing as _text_processing
    from .text_processing import (
        detect_render_tag,
        detect_html_tags,
        detect_dialogue,
        preserve_newlines,
        nl2br,
        markdown_to_html,
        format_dialogue,
    )
else:
    # 兼容仓库自检脚本直接以 ``main`` 模块导入的场景。
    if _PLUGIN_DIR not in sys.path:
        sys.path.insert(0, _PLUGIN_DIR)
    from renderer import html_to_image_playwright, init_browser, close_browser
    from sub_html_panels import SubHtmlPanelManager
    from template_manager import TemplateManager
    import text_processing as _text_processing
    from text_processing import (
        detect_render_tag,
        detect_html_tags,
        detect_dialogue,
        preserve_newlines,
        nl2br,
        markdown_to_html,
        format_dialogue,
    )


def _contains_math(content: str) -> bool:
    """Backward-compatible math detection so old cached modules won't break startup."""
    detector = getattr(_text_processing, "contains_math", None)
    if callable(detector):
        return detector(content)

    if not content:
        return False

    return bool(
        re.search(r"(?<!\\)\$(?!\$).+?(?<!\\)\$(?!\$)", content, re.DOTALL)
        or re.search(r"(?<!\\)\$\$[\s\S]+?(?<!\\)\$\$", content, re.DOTALL)
        or re.search(r"\\\(.+?\\\)", content, re.DOTALL)
        or re.search(r"\\\[[\s\S]+?\\\]", content, re.DOTALL)
        or re.search(r"\\begin\{([a-zA-Z*]+)\}[\s\S]+?\\end\{\1\}", content, re.DOTALL)
    )


def _avatar_dialogue_panel_config() -> dict:
    return {
        "enabled": True,
        "name": "头像对话",
        "panel_id": "avatar_dialogue",
        "panel_mode": "inline",
        "description": "在剧情或普通回复中需要角色说话时使用。标签写在台词应出现的位置；同一回复可以混用多个子模板，也可以重复使用本模板。",
        "output_mode": "when_relevant",
        "fields": [
            "accent|强调色|color|与当前主模板协调的边框和姓名颜色",
            "avatar|头像|image|从 avatars 文件夹选择与角色对应的图片文件名",
            "name|角色名|text|当前说话角色的姓名或称呼",
            "speech|台词|text|角色说出的内容，不要自行添加引号",
            "inner|内心活动|text|可选；角色未说出口的想法，没有则留空",
        ],
        "empty_value": "",
        "template_file": "sub_html_panels/头像对话.html",
        "html_template": "",
        "__template_key": "avatar_dialogue",
    }


@register(
    "astrbot_plugin_html_render",
    "lumingya",
    "将 AI 返回的 HTML/CSS 内容渲染成精美图片发送",
    "1.8.0",
)
class HtmlRenderPlugin(Star):
    _SESSION_RENDER_ENABLED_PREFIX = "_html_render_session_enabled::"
    _HISTORY_ASSISTANT_CONTENT_OVERRIDE_EXTRA_KEY = "_history_assistant_content_override"
    _HISTORY_ASSISTANT_DROP_EXTRA_KEY = "_history_assistant_drop"
    _CHATROOM_HISTORY_PREFIX = "You are now in a chatroom. The chat history is as follows:"
    _PIC_TAG_PATTERN = re.compile(
        r'<pic\b[^>]*\bprompt=(["\']).*?\1[^>]*?/?>\s*(?:</pic>)?',
        re.DOTALL | re.IGNORECASE,
    )
    _PIC_UNCLOSED_TAG_PATTERN = re.compile(
        r'<pic\b[^>]*\bprompt=(["\']).*?</pic>',
        re.DOTALL | re.IGNORECASE,
    )
    _LORA_TAG_PATTERN = re.compile(
        r'<lora\b[^>]*\bpicks=(["\']).*?\1[^>]*/?>',
        re.DOTALL | re.IGNORECASE,
    )

    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        # 显式传入插件名，不依赖 inspect 调用栈推断 star_map 元数据。
        # 这对热重载和启动时的插件加载顺序尤其重要。
        self.DATA_DIR = os.path.normpath(
            StarTools.get_data_dir("astrbot_plugin_html_render")
        )
        self.IMAGE_CACHE_DIR = os.path.join(self.DATA_DIR, "html_render_cache")

        # 模板管理器
        template_dir = os.path.join(os.path.dirname(__file__), "templates")
        self.template_mgr = TemplateManager(template_dir)
        self._ensure_builtin_inline_templates()
        self.sub_panel_mgr = SubHtmlPanelManager(_PLUGIN_DIR, logger=logger)
        self._panel_state_cache: Dict[str, Dict[str, dict]] = {}
        self._panel_state_signature = ""
        self._panel_state_panel_signatures: Dict[str, str] = {}
        self._panel_state_generations: Dict[str, int] = {}
        self._panel_state_active_ids: set[str] = set()
        self._panel_state_meta_loaded = False
        self._panel_state_meta_dirty = False
        self._refresh_sub_html_panels()

        # 用户默认模板设置（用户ID -> 模板名）
        self.user_default_template: Dict[str, str] = {}
        self._session_render_enabled: Dict[str, Optional[bool]] = {}

        # GIF 配置
        self.gif_duration = self._coerce_float(
            config.get("gif_duration", 3.0),
            3.0,
            minimum=0.1,
        )
        self.gif_fps = self._coerce_int(
            config.get("gif_fps", 15),
            15,
            minimum=1,
        )
        # 背景图缓存（按相对路径缓存 data URL 和尺寸）
        self._bg_asset_cache: Dict[str, tuple[str, tuple[int, int]]] = {}
        self._bg_image_size: Optional[tuple[int, int]] = None
        self._bg_round_robin_index = 0
        # 模板轮询索引（template_strategy = round_robin 时使用）
        self._tpl_round_robin_index = 0
        # 延迟删除任务的强引用，防止任务被垃圾回收后静默取消
        self._pending_delete_tasks: set = set()
        self._horror_template_pattern = re.compile(
            r"(恐怖|惊悚|诡异|阴森|噩梦|鬼|亡灵|血|病栋|午夜|深夜|低语|尖叫|尸|诅咒|怪谈)"
        )

    def _ensure_builtin_inline_templates(self) -> None:
        """Migrate the built-in avatar dialogue into existing panel lists once."""
        migration_key = "_avatar_dialogue_inline_migrated"
        raw_items = self.config.get("sub_html_panels", [])
        if not isinstance(raw_items, list):
            return

        already_present = any(
            isinstance(item, dict) and item.get("panel_id") == "avatar_dialogue"
            for item in raw_items
        )
        if not already_present and not self.config.get(migration_key, False):
            raw_items.append(_avatar_dialogue_panel_config())

        if not self.config.get(migration_key, False):
            self.config[migration_key] = True
            save_config = getattr(self.config, "save_config", None)
            if callable(save_config):
                try:
                    save_config()
                except Exception as exc:
                    logger.warning(f"[HTML渲染] 保存头像对话子模板迁移配置失败: {exc}")

    @staticmethod
    def _has_comfy_prompt_context(event: AstrMessageEvent) -> bool:
        return bool(
            event.get_extra("comfy_cleaned_text") is not None
            or hasattr(event, "_comfy_extracted_prompt")
            or hasattr(event, "_comfy_segments")
        )

    def _should_preserve_comfy_prompt_in_history(self, event: AstrMessageEvent) -> bool:
        if not self._has_comfy_prompt_context(event):
            return False

        comfy_meta = self.context.get_registered_star("astrbot_plugin_comfyui_pro")
        comfy_config = getattr(comfy_meta, "config", None) if comfy_meta else None
        if isinstance(comfy_config, dict):
            llm_settings = comfy_config.get("llm_settings", {})
            if isinstance(llm_settings, dict):
                return not bool(llm_settings.get("discard_prompt_from_history", False))

        comfy_instance = getattr(comfy_meta, "star_cls", None) if comfy_meta else None
        discard_prompt_from_history = getattr(
            comfy_instance,
            "discard_prompt_from_history",
            None,
        )
        if isinstance(discard_prompt_from_history, bool):
            return not discard_prompt_from_history

        return True

    @classmethod
    def _prepend_prompt_before_chatroom_history(cls, system_prompt: str, injected_block: str) -> str:
        """Ensure injected instructions stay ahead of the chatroom-history wrapper."""
        injected_block = (injected_block or "").strip()
        if not injected_block:
            return system_prompt

        system_prompt = system_prompt or ""
        insert_at = system_prompt.find(cls._CHATROOM_HISTORY_PREFIX)
        if insert_at == -1:
            return f"{injected_block}\n\n{system_prompt}".strip()

        prefix = system_prompt[:insert_at].rstrip()
        suffix = system_prompt[insert_at:].lstrip()
        if prefix:
            return f"{prefix}\n\n{injected_block}\n\n{suffix}"
        return f"{injected_block}\n\n{suffix}"

    @classmethod
    def _extract_comfy_history_tags(cls, text: Optional[str]) -> list[str]:
        matches: list[tuple[int, str]] = []
        occupied_spans: list[tuple[int, int]] = []
        source = str(text or "")
        for pattern in (cls._LORA_TAG_PATTERN, cls._PIC_TAG_PATTERN, cls._PIC_UNCLOSED_TAG_PATTERN):
            for match in pattern.finditer(source):
                start, end = match.span()
                if any(not (end <= span_start or start >= span_end) for span_start, span_end in occupied_spans):
                    continue
                tag = match.group(0).strip()
                if tag:
                    matches.append((start, tag))
                    occupied_spans.append((start, end))
        matches.sort(key=lambda item: item[0])
        return [tag for _, tag in matches]

    @classmethod
    def _merge_comfy_tags_into_history_text(
        cls,
        history_text: Optional[str],
        raw_text: Optional[str],
    ) -> str:
        merged_text = str(history_text or "").strip()
        missing_tags: list[str] = []
        for tag in cls._extract_comfy_history_tags(raw_text):
            if tag in merged_text or tag in missing_tags:
                continue
            missing_tags.append(tag)

        if not merged_text:
            return "\n".join(missing_tags).strip()
        if not missing_tags:
            return merged_text
        return "\n".join([merged_text, *missing_tags]).strip()

    @classmethod
    def _extract_text_from_message_content(cls, content) -> Optional[str]:
        if isinstance(content, str):
            return content

        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                extracted = cls._extract_text_from_message_content(item)
                if extracted:
                    parts.append(extracted)
            return "\n".join(parts).strip() or None

        if isinstance(content, dict):
            item_type = str(content.get("type") or "").strip().lower()
            if item_type in {"text", "input_text", "output_text"}:
                text_value = content.get("text")
                if isinstance(text_value, str):
                    return text_value

            for key in ("text", "content", "value"):
                value = content.get(key)
                extracted = cls._extract_text_from_message_content(value)
                if extracted:
                    return extracted

        return None

    @classmethod
    def _unwrap_assistant_payload_text(cls, text: Optional[str]) -> str:
        source = str(text or "").strip()
        if not source:
            return ""

        candidates = [source]
        fenced_match = re.fullmatch(
            r"```(?:json)?\s*([\s\S]*?)\s*```",
            source,
            re.IGNORECASE,
        )
        if fenced_match:
            candidates.insert(0, fenced_match.group(1).strip())

        for candidate in candidates:
            if not candidate or candidate[0] not in "{[":
                continue

            try:
                payload = json.loads(candidate)
            except json.JSONDecodeError:
                continue

            if isinstance(payload, dict):
                role = str(payload.get("role") or "").strip().lower()
                if role not in {"", "assistant"}:
                    continue
                extracted = None
                if "content" in payload:
                    extracted = cls._extract_text_from_message_content(payload.get("content"))
                elif "message" in payload:
                    extracted = cls._extract_text_from_message_content(payload.get("message"))
                elif role == "assistant" and "text" in payload:
                    extracted = cls._extract_text_from_message_content(payload.get("text"))
            else:
                extracted = cls._extract_text_from_message_content(payload)

            if extracted:
                return extracted.strip()

        return source

    @classmethod
    def _normalize_render_source_text(
        cls,
        text: Optional[str],
        *,
        strip_pic: bool = True,
        strip_lora: bool = True,
    ) -> str:
        normalized = cls._unwrap_assistant_payload_text(text)
        normalized = cls._strip_internal_markup(
            normalized,
            strip_pic=strip_pic,
            strip_lora=strip_lora,
        )
        return normalized.strip()

    # ==================== 生命周期 ====================

    async def initialize(self):
        try:
            os.makedirs(self.IMAGE_CACHE_DIR, exist_ok=True)
            self._cleanup_cache()
            await self.template_mgr.load_templates()
            self._refresh_template_schema_options()
            self._require_available_templates()
            self.template_mgr.update_template_id_map()
            await self._ensure_playwright()
            # 预启动浏览器实例（后续渲染复用，避免首次渲染等待）
            await init_browser()
            logger.info("HTML 渲染插件初始化完成")
        except Exception as e:
            logger.error(f"HTML 渲染插件初始化失败: {e}")
            if isinstance(e, FileNotFoundError):
                raise

    async def _ensure_playwright(self):
        logger.info("HTML渲染插件: 检查 Playwright 依赖...")
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "playwright", "install", "chromium",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await process.communicate()
            if process.returncode != 0:
                logger.error(f"Playwright Chromium 安装失败: {stderr.decode('utf-8', errors='ignore')}")
        except Exception as e:
            logger.error(f"执行命令失败: {e}")

    async def terminate(self):
        await close_browser()
        logger.info("HTML 渲染插件已停止")

    def _get_background_image_strategy(self) -> str:
            strategy = str(self.config.get("background_image_strategy", "fixed") or "fixed").strip().lower()
            if strategy not in {"fixed", "round_robin", "random"}:
                return "fixed"
            return strategy

    def _select_background_image(self) -> str:
            configured_image = str(self.config.get("background_image", "") or "").strip()
            strategy = self._get_background_image_strategy()
            available_images = self._get_available_background_images()

            if strategy == "fixed":
                return configured_image

            if not available_images:
                return ""

            if strategy == "random":
                return random.choice(available_images)

            image_path = available_images[self._bg_round_robin_index % len(available_images)]
            self._bg_round_robin_index += 1
            return image_path

    def _get_bg_data_url(self) -> str:
            """按配置选择背景图片并转为 base64 Data URL。"""
            bg_config = self._select_background_image()
            if not bg_config:
                self._bg_image_size = None
                return ""

            bg_path = os.path.join(_PLUGIN_DIR, bg_config)
            if not os.path.isfile(bg_path):
                logger.warning(f"[HTML渲染] 背景图片不存在: {bg_path}，将使用默认背景")
                self._bg_image_size = None
                return ""

            cached_asset = self._bg_asset_cache.get(bg_config)
            if cached_asset:
                self._bg_image_size = cached_asset[1]
                return cached_asset[0]

            try:
                ext = os.path.splitext(bg_path)[1].lower()
                mime_map = {
                    ".jpg": "image/jpeg",
                    ".jpeg": "image/jpeg",
                    ".png": "image/png",
                    ".webp": "image/webp",
                    ".gif": "image/gif",
                }
                mime = mime_map.get(ext, "image/png")
                with PILImage.open(bg_path) as img:
                    image_size = (max(1, img.width), max(1, img.height))
                with open(bg_path, "rb") as f:
                    encoded = base64.b64encode(f.read()).decode("utf-8")
                data_url = f"data:{mime};base64,{encoded}"
                self._bg_asset_cache[bg_config] = (data_url, image_size)
                self._bg_image_size = image_size
                logger.info(f"[HTML渲染] 背景图片已加载: {bg_config} ({mime})")
            except Exception as e:
                logger.warning(f"[HTML渲染] 读取背景图片失败: {e}")
                self._bg_image_size = None
                return ""

            return data_url

    def _inject_math_assets(self, html_content: str) -> str:
            """为包含数学公式的页面注入 MathJax 资源。"""
            if 'id="astrbot-mathjax-script"' in html_content:
                return html_content

            math_assets = """
<style>
.astr-math-inline,
.astr-math-block {
  max-width: 100%;
}
.astr-math-block {
  display: block;
  margin: 0.9em 0;
  overflow-x: auto;
  overflow-y: hidden;
  text-align: center;
}
mjx-container,
mjx-container * {
  word-break: normal !important;
  overflow-wrap: normal !important;
}
mjx-container[jax="SVG"] {
  max-width: 100%;
}
.astr-math-block mjx-container[jax="SVG"] {
  display: inline-block !important;
  margin: 0 auto !important;
}
</style>
<script>
window.__ASTR_MATH_READY__ = false;
window.MathJax = {
  tex: {
    inlineMath: [['$', '$'], ['\\\\(', '\\\\)']],
    displayMath: [['$$', '$$'], ['\\\\[', '\\\\]']],
    processEscapes: true,
    processEnvironments: true,
    packages: {'[+]': ['ams', 'noerrors', 'noundefined']}
  },
  svg: {
    fontCache: 'global'
  },
  options: {
    skipHtmlTags: ['script', 'noscript', 'style', 'textarea', 'pre', 'code']
  },
  startup: {
    pageReady: () => MathJax.startup.defaultPageReady().then(() => {
      window.__ASTR_MATH_READY__ = true;
    })
  }
};
</script>
<script
  id="astrbot-mathjax-script"
  defer
  src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-svg.js"
  onerror="window.__ASTR_MATH_READY__ = true;"
></script>
"""

            if "</head>" in html_content:
                return html_content.replace("</head>", math_assets + "</head>", 1)

            return math_assets + html_content

    def _get_background_render_mode(self) -> str:
            mode = str(self.config.get("background_render_mode", "ambient") or "ambient").strip().lower()
            if mode not in {"ambient", "watermark"}:
                return "ambient"
            return mode

    def _get_background_opacity(self, render_mode: str) -> float:
            default_opacity = 0.17
            raw_value = self.config.get("background_opacity", default_opacity)
            opacity = self._coerce_float(raw_value, default_opacity)
            return max(0.0, min(1.0, opacity))

    def _get_background_aspect_ratio(self) -> str:
            if self._bg_image_size and self._bg_image_size[0] > 0 and self._bg_image_size[1] > 0:
                return f"{self._bg_image_size[0]} / {self._bg_image_size[1]}"
            return "1 / 1"

    def _inject_background_image(self, html_content: str, bg_data_url: str, render_mode: str) -> str:
            """Inject the configured background as a real backdrop layer."""
            if not bg_data_url or 'id="astrbot-custom-bg-style"' in html_content:
                return html_content

            aspect_ratio = self._get_background_aspect_ratio()
            opacity = self._get_background_opacity(render_mode)
            if render_mode == "watermark":
                bg_assets = f"""
<style id="astrbot-custom-bg-style">
html {{
  background: transparent !important;
}}
body {{
  position: relative !important;
  background: transparent !important;
}}
.content {{
  position: relative !important;
  isolation: isolate !important;
  z-index: 0;
}}
.content::before {{
  content: "";
  position: absolute;
  top: 18px;
  left: 50%;
  width: calc(100% + 20px);
  max-width: calc(100% + 20px);
  aspect-ratio: {aspect_ratio};
  height: auto;
  transform: translateX(-50%) scale(1.015);
  transform-origin: center top;
  z-index: 0;
  pointer-events: none;
  background-image: url("{bg_data_url}");
  background-size: 100% auto;
  background-position: center top;
  background-repeat: no-repeat;
  opacity: {opacity};
  filter: saturate(0.92) contrast(0.97);
  mix-blend-mode: multiply;
}}
.content > * {{
  position: relative;
  z-index: 1;
}}
</style>
"""
            else:
                bg_assets = f"""
<style id="astrbot-custom-bg-style">
html {{
  background: transparent !important;
}}
body {{
  position: relative !important;
  isolation: isolate !important;
  background: transparent !important;
}}
body::before {{
  content: "";
  position: absolute;
  inset: 0;
  z-index: -2;
  pointer-events: none;
  background-image: url("{bg_data_url}");
  background-size: 102% auto;
  background-position: center top;
  background-repeat: repeat-y;
  background-attachment: scroll;
  opacity: {opacity};
  filter: blur(6px) saturate(0.95);
  transform: scale(1.015);
  transform-origin: center top;
}}
body::after {{
  content: "";
  position: absolute;
  inset: 0;
  z-index: -1;
  pointer-events: none;
  background:
    linear-gradient(180deg, rgba(255,255,255,0.20), rgba(255,255,255,0.12)),
    radial-gradient(circle at top, rgba(255,255,255,0.16), rgba(255,255,255,0.03) 55%);
}}
body > * {{
  position: relative;
  z-index: 1;
}}
</style>
"""

            if "</head>" in html_content:
                return html_content.replace("</head>", bg_assets + "</head>", 1)

            return bg_assets + html_content

    def _cleanup_cache(self, max_age_seconds: int = 300):
        """清理缓存目录中的过期文件"""
        import time
        now = time.time()
        count = 0
        try:
            for f in os.listdir(self.IMAGE_CACHE_DIR):
                fp = os.path.join(self.IMAGE_CACHE_DIR, f)
                if os.path.isfile(fp) and (now - os.path.getmtime(fp)) > max_age_seconds:
                    os.remove(fp)
                    count += 1
            if count:
                logger.info(f"[HTML渲染] 已清理 {count} 个缓存文件")
        except Exception as e:
            logger.warning(f"[HTML渲染] 清理缓存失败: {e}")

    def _schedule_delete(self, *paths):
        """延迟删除文件（给消息发送留足时间，多图模式下图片生成耗时较长）"""
        async def _delete():
            await asyncio.sleep(300)
            for p in paths:
                try:
                    if os.path.exists(p):
                        os.remove(p)
                except Exception:
                    pass
        task = asyncio.create_task(_delete())
        # 保持强引用：裸 create_task 的返回值若不保存，任务可能被 GC 静默丢弃
        self._pending_delete_tasks.add(task)
        task.add_done_callback(self._pending_delete_tasks.discard)

    # ==================== 工具方法 ====================

    def _get_user_id(self, event: AstrMessageEvent) -> str:
        try:
            if hasattr(event, 'get_sender_id') and callable(event.get_sender_id):
                return str(event.get_sender_id())
            if hasattr(event, 'sender') and hasattr(event.sender, 'user_id'):
                return str(event.sender.user_id)
            return str(event.unified_msg_origin)
        except Exception:
            return "default_user"

    def _get_platform_name(self, event: AstrMessageEvent) -> str:
        try:
            if hasattr(event, "get_platform_name") and callable(event.get_platform_name):
                value = event.get_platform_name()
                if value:
                    return str(value)
        except Exception:
            pass
        return "unknown"

    def _get_session_scope(self, event: AstrMessageEvent) -> tuple[str, str]:
        platform_name = self._get_platform_name(event)

        group_id = ""
        try:
            if hasattr(event, "get_group_id") and callable(event.get_group_id):
                group_id = str(event.get_group_id() or "").strip()
            elif hasattr(event, "group_id"):
                group_id = str(getattr(event, "group_id") or "").strip()
        except Exception:
            group_id = ""

        if group_id and group_id != "0":
            return (
                f"group::{platform_name}::{group_id}",
                f"群聊 {group_id} ({platform_name})",
            )

        user_id = ""
        try:
            if hasattr(event, "get_sender_id") and callable(event.get_sender_id):
                user_id = str(event.get_sender_id() or "").strip()
            elif hasattr(event, "user_id"):
                user_id = str(getattr(event, "user_id") or "").strip()
        except Exception:
            user_id = ""

        if user_id and user_id != "0":
            return (
                f"private::{platform_name}::{user_id}",
                f"私聊 {user_id} ({platform_name})",
            )

        fallback = str(getattr(event, "unified_msg_origin", "") or "global").strip()
        return (f"session::{fallback}", fallback)

    def _render_enabled_kv_key(self, session_key: str) -> str:
        return f"{self._SESSION_RENDER_ENABLED_PREFIX}{session_key}"

    @staticmethod
    def _normalize_optional_bool(value) -> Optional[bool]:
        if value is None:
            return None
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        text = str(value).strip().lower()
        if text in {"1", "true", "on", "yes", "enabled", "enable"}:
            return True
        if text in {"0", "false", "off", "no", "disabled", "disable"}:
            return False
        return None

    @staticmethod
    def _coerce_int(
        value,
        default: int,
        minimum: Optional[int] = None,
        maximum: Optional[int] = None,
    ) -> int:
        try:
            coerced = int(value)
        except (TypeError, ValueError):
            return default

        if minimum is not None:
            coerced = max(minimum, coerced)
        if maximum is not None:
            coerced = min(maximum, coerced)
        return coerced

    @staticmethod
    def _coerce_float(
        value,
        default: float,
        minimum: Optional[float] = None,
        maximum: Optional[float] = None,
    ) -> float:
        try:
            coerced = float(value)
        except (TypeError, ValueError):
            return default

        if minimum is not None:
            coerced = max(minimum, coerced)
        if maximum is not None:
            coerced = min(maximum, coerced)
        return coerced

    async def _get_session_render_override(self, event: AstrMessageEvent) -> Optional[bool]:
        session_key, _ = self._get_session_scope(event)
        if session_key in self._session_render_enabled:
            return self._session_render_enabled[session_key]

        stored_value = await self.get_kv_data(self._render_enabled_kv_key(session_key), None)
        normalized = self._normalize_optional_bool(stored_value)
        self._session_render_enabled[session_key] = normalized
        return normalized

    async def _is_render_enabled_for_session(self, event: AstrMessageEvent) -> bool:
        override = await self._get_session_render_override(event)
        if override is not None:
            return override
        return True

    async def _set_session_render_enabled(
        self,
        event: AstrMessageEvent,
        enabled: Optional[bool],
    ) -> tuple[str, str]:
        session_key, session_label = self._get_session_scope(event)
        kv_key = self._render_enabled_kv_key(session_key)
        normalized = self._normalize_optional_bool(enabled)
        self._session_render_enabled[session_key] = normalized
        if normalized is None:
            await self.delete_kv_data(kv_key)
        else:
            await self.put_kv_data(kv_key, normalized)
        return session_key, session_label

    async def _build_render_status_message(self, event: AstrMessageEvent) -> str:
        session_key, session_label = self._get_session_scope(event)
        session_override = await self._get_session_render_override(event)
        effective = await self._is_render_enabled_for_session(event)
        override_label = "无"
        if session_override is not None:
            override_label = "开启" if session_override else "关闭"
        lines = [
            "HTML 渲染状态",
            f"会话: {session_label}",
            f"会话键: {session_key}",
            f"当前状态: {'开启' if effective else '关闭'}",
            f"会话覆盖: {override_label}",
            f"全局提示词注入: {'开启' if self.config.get('inject_prompt', True) else '关闭'}",
            f"全局自动渲染: {'开启' if self.config.get('auto_render_all', True) else '关闭'}",
            "命令: /html开  /html关  /html状态  /html重置",
        ]
        return "\n".join(lines)

    def _build_assistant_history_override(
        self,
        text: str,
        rendered_via_plugin: bool,
    ) -> Optional[str]:
        if not rendered_via_plugin:
            return None

        if self.config.get("preserve_text_for_context", True):
            cleaned = str(text or "").strip()
            return cleaned or "[图片]"

        return "[图片]"

    @classmethod
    def _strip_render_markup(cls, text: str) -> str:
        if not text:
            return text
        text = re.sub(r'</?render\b[^>]*>', '\n\n', text, flags=re.IGNORECASE)
        text = re.sub(r'[ \t]*\n[ \t]*', '\n', text)
        text = re.sub(r'\n{3,}', '\n\n', text)
        return text.strip()

    @classmethod
    def _has_history_cleanup_markup(cls, text: Optional[str]) -> bool:
        source = str(text or "")
        if not source:
            return False
        return bool(
            re.search(r'</?render\b', source, re.IGNORECASE)
            or cls._PIC_TAG_PATTERN.search(source)
            or cls._PIC_UNCLOSED_TAG_PATTERN.search(source)
            or cls._LORA_TAG_PATTERN.search(source)
            or re.search(r'</?panel\b', source, re.IGNORECASE)
            or re.search(
                r'</?(think|ctx|context_summary)\b',
                source,
                re.IGNORECASE,
            )
        )

    def _clean_assistant_history_text(
        self,
        event: AstrMessageEvent,
        history_source: Optional[str],
        original_text: Optional[str] = None,
    ) -> str:
        preserve_comfy_prompt = self._should_preserve_comfy_prompt_in_history(event)
        clean_text = self._unwrap_assistant_payload_text(history_source)
        if preserve_comfy_prompt:
            clean_text = self._merge_comfy_tags_into_history_text(
                clean_text,
                original_text,
            )
        clean_text = self._strip_render_markup(clean_text)
        self._refresh_sub_html_panels()
        clean_text = self.sub_panel_mgr.strip_panel_markup(clean_text)
        clean_text = self._strip_internal_markup(
            clean_text,
            strip_pic=not preserve_comfy_prompt,
            strip_lora=not preserve_comfy_prompt,
        )
        return clean_text.strip()

    def _get_auto_render_min_length(self) -> int:
        return self._coerce_int(
            self.config.get("auto_render_min_length", 20),
            20,
            minimum=0,
        )

    def _refresh_template_schema_options(self):
        schema = getattr(self.config, "schema", None)
        if not isinstance(schema, dict):
            return

        templates = self._get_available_templates()
        template_options = [""] + templates

        field_labels = {
            "default_template": ["自动使用第一个可用模板"] + templates,
            "auto_render_template": ["回落到当前默认模板"] + templates,
            "merged_template": ["回落到当前默认模板"] + templates,
        }

        for field_name, empty_label in field_labels.items():
            field_meta = schema.get(field_name)
            if not isinstance(field_meta, dict):
                continue
            field_meta["options"] = template_options
            field_meta["enum"] = template_options
            field_meta["labels"] = empty_label

        bg_field_meta = schema.get("background_image")
        if isinstance(bg_field_meta, dict):
            background_images = self._get_available_background_images()
            bg_field_meta["options"] = [""] + background_images
            bg_field_meta["enum"] = [""] + background_images
            bg_field_meta["labels"] = ["不使用自定义背景"] + background_images

    def _refresh_sub_html_panels(self) -> None:
        if not isinstance(getattr(self, "_panel_state_generations", None), dict):
            self._panel_state_generations = {}
        if not hasattr(self, "_panel_state_meta_dirty"):
            self._panel_state_meta_dirty = False
        manager = getattr(self, "sub_panel_mgr", None)
        if manager is None:
            self.sub_panel_mgr = SubHtmlPanelManager(_PLUGIN_DIR, logger=logger)
            manager = self.sub_panel_mgr
        previous_signature = getattr(self, "_panel_state_signature", "")
        previous_panel_signatures = getattr(self, "_panel_state_panel_signatures", {})
        previous_active_ids = set(getattr(self, "_panel_state_active_ids", set()))
        manager.reload(self.config.get("sub_html_panels", []))
        current_signature = manager.state_signature
        current_panel_signatures = manager.state_panel_signatures
        current_active_ids = set(current_panel_signatures)
        changed_ids = {
            panel_id
            for panel_id in (set(previous_panel_signatures) | current_active_ids)
            if previous_panel_signatures.get(panel_id) != current_panel_signatures.get(panel_id)
        }
        removed_ids = previous_active_ids - current_active_ids
        invalidated_ids = {
            panel_id
            for panel_id, signature in previous_panel_signatures.items()
            if current_panel_signatures.get(panel_id) != signature
        }
        if previous_signature and (changed_ids or removed_ids):
            for state in self._panel_state_cache.values():
                for panel_id in changed_ids | removed_ids:
                    state.pop(panel_id, None)
            self._panel_state_cache = {
                scope: state
                for scope, state in self._panel_state_cache.items()
                if state
            }
            for panel_id in invalidated_ids:
                self._panel_state_generations[panel_id] = (
                    int(self._panel_state_generations.get(panel_id, 0)) + 1
                )
            self._panel_state_meta_dirty = True
            logger.info("[HTML渲染] 已按面板隔离子 HTML 面板状态变更")
        self._panel_state_signature = current_signature
        self._panel_state_panel_signatures = current_panel_signatures
        self._panel_state_active_ids = current_active_ids

    def _strip_panel_markup_from_request_context(self, req: ProviderRequest) -> None:
        """Keep internal panel protocol out of the model-visible conversation context."""
        if isinstance(getattr(req, "system_prompt", None), str):
            req.system_prompt = self.sub_panel_mgr.strip_panel_markup(req.system_prompt)
        contexts = getattr(req, "contexts", None)
        if not isinstance(contexts, list):
            return

        def clean(value):
            if isinstance(value, str):
                return self.sub_panel_mgr.strip_panel_markup(value)
            if isinstance(value, list):
                return [clean(item) for item in value]
            if isinstance(value, dict):
                copied = dict(value)
                for key in ("content", "text", "value"):
                    if key in copied:
                        copied[key] = clean(copied[key])
                return copied
            return value

        for index, message in enumerate(contexts):
            contexts[index] = clean(message)

    async def _build_panel_state_context(self, scope: str) -> str:
        """Expose current stateful panel values so the model can emit reliable updates."""
        state = await self._get_panel_state(scope)
        stateful_ids = {
            panel.panel_id
            for panel in self.sub_panel_mgr.get_enabled_panels()
            if panel.panel_mode == "stateful"
        }
        visible_state = {
            panel_id: payload
            for panel_id, payload in state.items()
            if panel_id in stateful_ids and isinstance(payload, dict)
        }
        if not visible_state:
            return ""
        encoded = json.dumps(
            visible_state,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
        if len(encoded) > 12000:
            encoded = encoded[:12000] + "..."
        return (
            "### 当前已保存的状态面板数据\n"
            "以下 JSON 是插件保存的最近状态。生成 stateful 面板时，未变化字段应沿用；"
            "只有变化字段才使用 update，普通完整标签必须保留所有需要的字段。\n"
            f"{encoded}"
        )

    def _get_panel_state_scope(self, event: Optional[AstrMessageEvent] = None) -> str:
        if event is not None:
            origin = str(getattr(event, "unified_msg_origin", "") or "").strip()
            if origin:
                return origin
            try:
                user_id = self._get_user_id(event)
            except Exception:
                user_id = ""
            if user_id:
                return user_id
        return "global"

    def _panel_state_kv_key(self, scope: str) -> str:
        scope_digest = hashlib.sha256(str(scope).encode("utf-8")).hexdigest()[:24]
        return f"_html_render_panel_state::v2::{scope_digest}"

    def _panel_state_generation_key(self, scope: str) -> str:
        scope_digest = hashlib.sha256(str(scope).encode("utf-8")).hexdigest()[:24]
        return f"_html_render_panel_state::v2::generations::{scope_digest}"

    @staticmethod
    def _panel_state_meta_key() -> str:
        return "_html_render_panel_state::v2::meta"

    async def _ensure_panel_state_meta(self) -> None:
        """Load persisted panel generations and invalidate panels disabled while offline."""
        generations = getattr(self, "_panel_state_generations", None)
        if not isinstance(generations, dict):
            generations = self._panel_state_generations = {}
        active_ids = set(getattr(self, "_panel_state_active_ids", set()))
        stored = await self.get_kv_data(self._panel_state_meta_key(), None)
        if isinstance(stored, dict):
            persisted_generations = stored.get("generations", {})
            if isinstance(persisted_generations, dict):
                for panel_id, generation in persisted_generations.items():
                    try:
                        generations[str(panel_id)] = max(
                            int(generation),
                            int(generations.get(str(panel_id), 0)),
                        )
                    except (TypeError, ValueError):
                        continue
            persisted_active = {
                str(panel_id)
                for panel_id in (stored.get("active_ids", []) or [])
                if isinstance(panel_id, str)
            }
            if not getattr(self, "_panel_state_meta_dirty", False):
                removed_ids = persisted_active - active_ids
                for panel_id in removed_ids:
                    generations[panel_id] = (
                        int(generations.get(panel_id, 0)) + 1
                    )
                    self._panel_state_meta_dirty = True
        self._panel_state_meta_loaded = True
        if self._panel_state_meta_dirty or not isinstance(stored, dict):
            await self.put_kv_data(
                self._panel_state_meta_key(),
                {
                    "active_ids": sorted(active_ids),
                    "generations": dict(generations),
                },
            )
            self._panel_state_meta_dirty = False

    def _legacy_panel_state_kv_key(self, scope: str) -> str:
        scope_digest = hashlib.sha256(str(scope).encode("utf-8")).hexdigest()[:24]
        return (
            "_html_render_panel_state::"
            f"{self.sub_panel_mgr.configuration_signature}::{scope_digest}"
        )

    async def _get_panel_state(self, scope: Optional[str]) -> Dict[str, dict]:
        await self._ensure_panel_state_meta()
        normalized_scope = str(scope or "global")
        cached = self._panel_state_cache.get(normalized_scope)
        if cached is not None:
            return cached

        state_key = self._panel_state_kv_key(normalized_scope)
        stored = await self.get_kv_data(state_key, None)
        migrated_legacy_state = False
        if stored is None:
            legacy_key = self._legacy_panel_state_kv_key(normalized_scope)
            if legacy_key != state_key:
                stored = await self.get_kv_data(legacy_key, None)
                migrated_legacy_state = stored is not None
        if stored is None:
            stored = {}
        if isinstance(stored, dict) and isinstance(stored.get("panels"), dict):
            stored = stored["panels"]
        stored_generations = await self.get_kv_data(self._panel_state_generation_key(normalized_scope), {})
        if not isinstance(stored_generations, dict):
            stored_generations = {}
        known_panel_ids = getattr(self, "_panel_state_panel_signatures", {})
        current_generations = getattr(self, "_panel_state_generations", {})

        def generation_matches(panel_id: str) -> bool:
            try:
                stored_generation = int(stored_generations.get(panel_id, 0) or 0)
                current_generation = int(current_generations.get(panel_id, 0) or 0)
            except (TypeError, ValueError):
                return False
            return stored_generation == current_generation

        state = {
            str(panel_id): dict(payload)
            for panel_id, payload in (stored.items() if isinstance(stored, dict) else [])
            if isinstance(panel_id, str)
            and isinstance(payload, dict)
            and (not known_panel_ids or panel_id in known_panel_ids)
            and generation_matches(panel_id)
        }
        self._panel_state_cache[normalized_scope] = state
        if migrated_legacy_state:
            await self.put_kv_data(state_key, {"panels": state})
            await self.put_kv_data(
                self._panel_state_generation_key(normalized_scope),
                {panel_id: current_generations.get(panel_id, 0) for panel_id in state},
            )
            logger.info("[HTML渲染] 已迁移旧版子 HTML 面板状态")
        return state

    async def _save_panel_state(self, scope: Optional[str], state: Dict[str, dict]) -> None:
        normalized_scope = str(scope or "global")
        self._panel_state_cache[normalized_scope] = state
        await self.put_kv_data(
            self._panel_state_kv_key(normalized_scope),
            {"panels": state},
        )
        await self.put_kv_data(
            self._panel_state_generation_key(normalized_scope),
            {
                panel_id: getattr(self, "_panel_state_generations", {}).get(panel_id, 0)
                for panel_id in state
            },
        )

    @staticmethod
    def _insert_panel_tags(text: Optional[str], panel_tags: List[str]) -> str:
        source = str(text or "").strip()
        block = "\n".join(tag.strip() for tag in panel_tags if tag and tag.strip())
        if not block:
            return source

        closing_tags = list(re.finditer(r"</render\s*>", source, re.IGNORECASE))
        if closing_tags:
            insert_at = closing_tags[-1].start()
            before = source[:insert_at].rstrip()
            after = source[insert_at:].lstrip()
            return f"{before}\n{block}\n{after}".strip()
        return f"{source}\n{block}".strip()

    async def _supplement_missing_always_panels(
        self,
        event: AstrMessageEvent,
        completion_text: Optional[str],
    ) -> tuple[str, List[str]]:
        """Request missing always-panel data once, then guarantee a local fallback tag."""
        source = str(completion_text or "").strip()
        self._refresh_sub_html_panels()
        missing_panels = self.sub_panel_mgr.get_missing_always_panels(source)
        if not missing_panels:
            return source, []

        missing_ids = [panel.panel_id for panel in missing_panels]
        panel_scope = self._get_panel_state_scope(event)
        panel_state = await self._get_panel_state(panel_scope)
        relevant_state = {
            panel_id: panel_state.get(panel_id, {})
            for panel_id in missing_ids
        }
        recent_reply = self.sub_panel_mgr.strip_panel_markup(source)
        recent_reply = self._strip_render_markup(
            self._normalize_render_source_text(recent_reply)
        )
        if len(recent_reply) > 8000:
            recent_reply = (
                recent_reply[:4000].rstrip()
                + "\n\n[中间内容已截断]\n\n"
                + recent_reply[-4000:].lstrip()
            )

        generated_tags: dict[str, str] = {}
        provider = self.context.get_using_provider(event.unified_msg_origin)
        if provider is None:
            logger.warning(
                "[HTML渲染] 每次回复面板缺失，但当前没有可用模型；将沿用本地状态: %s",
                ", ".join(missing_ids),
            )
        else:
            palette_template = self._get_default_template(self._get_user_id(event))
            explicit_templates = {
                template_name
                for _, template_name, _, _ in detect_render_tag(source)
                if template_name and self._has_template(template_name)
            }
            if len(explicit_templates) == 1:
                palette_template = explicit_templates.pop()
            protocol = self.sub_panel_mgr.build_prompt_block(
                missing_ids,
                theme_palette=self._extract_template_palette(palette_template),
            )
            state_json = json.dumps(
                relevant_state,
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            )
            system_prompt = (
                f"{protocol}\n\n"
                "你现在只负责补全主回复遗漏的“每次回复”面板。"
                "必须为上面列出的每个面板输出且只输出一个合法 <panel> 标签；"
                "禁止输出解释、正文、Markdown 或 <render> 标签。"
                "已有状态存在时优先使用 update 只写变化字段；"
                "无法判断变化时也要输出 update 空对象，不得省略面板。"
            )
            prompt = (
                "下面是插件保存的面板状态：\n"
                f"{state_json}\n\n"
                "下面是刚生成但漏掉面板的主回复：\n"
                f"{recent_reply or '（空回复）'}\n\n"
                f"请补全这些面板：{', '.join(missing_ids)}"
            )
            try:
                response = await provider.text_chat(
                    prompt=prompt,
                    contexts=[],
                    system_prompt=system_prompt,
                )
                fallback_text = str(
                    getattr(response, "completion_text", "") or ""
                ).strip()
                generated_tags = self.sub_panel_mgr.extract_valid_panel_tags(
                    fallback_text,
                    missing_ids,
                )
                if generated_tags:
                    logger.info(
                        "[HTML渲染] 已通过二次模型请求补全面板: %s",
                        ", ".join(generated_tags),
                    )
                else:
                    logger.warning(
                        "[HTML渲染] 二次模型请求未返回合法面板，将沿用本地状态: %s",
                        ", ".join(missing_ids),
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "[HTML渲染] 二次模型请求补全面板失败，将沿用本地状态: %s",
                    exc,
                )

        supplemented_tags = [
            generated_tags.get(
                panel.panel_id,
                (
                    f'<panel id="{panel.panel_id}">{{}}</panel>'
                    if panel.panel_mode == "inline"
                    else f'<panel id="{panel.panel_id}" update>{{}}</panel>'
                ),
            )
            for panel in missing_panels
        ]
        return self._insert_panel_tags(source, supplemented_tags), supplemented_tags

    def _get_available_templates(self) -> List[str]:
        getter = getattr(self.template_mgr, "get_available_templates", None)
        if callable(getter):
            templates = getter()
            if isinstance(templates, list):
                return templates
        return []

    def _get_available_background_images(self) -> List[str]:
        image_exts = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
        results: List[str] = []

        for root, _, files in os.walk(_PLUGIN_DIR):
            for filename in files:
                if os.path.splitext(filename)[1].lower() not in image_exts:
                    continue
                abs_path = os.path.join(root, filename)
                rel_path = os.path.relpath(abs_path, _PLUGIN_DIR)
                results.append(rel_path.replace("\\", "/"))

        return sorted(set(results))

    def _require_available_templates(self) -> List[str]:
        getter = getattr(self.template_mgr, "require_available_templates", None)
        if callable(getter):
            return getter()

        templates = self._get_available_templates()
        if templates:
            return templates

        template_dir = getattr(self.template_mgr, "TEMPLATE_DIR", os.path.join(_PLUGIN_DIR, "templates"))
        raise FileNotFoundError(
            f"未找到任何模板文件，请先在 {template_dir} 中放入至少一个 .html 模板"
        )

    def _has_template(self, template_name: Optional[str]) -> bool:
        if not template_name:
            return False

        checker = getattr(self.template_mgr, "has_template", None)
        if callable(checker):
            return bool(checker(template_name))

        return template_name in self._get_available_templates()

    def _should_skip_auto_render(self, text: str) -> bool:
        """
        Skip auto-render for short plain replies.
        Explicit <render> blocks are always honored.
        """
        if not text or self._has_explicit_render_request(text):
            return False

        threshold = self._get_auto_render_min_length()
        if threshold <= 0:
            return False

        visible_text = re.sub(r"<[^>]+>", "", text)
        visible_text = re.sub(r"\s+", "", visible_text)
        return len(visible_text) < threshold

    def _is_auto_detect_enabled(self) -> bool:
        return bool(self.config.get("enable_auto_detect", True))

    def _is_auto_render_all_enabled(self) -> bool:
        return bool(self.config.get("auto_render_all", True))

    def _get_gif_scale(self) -> int:
        return self._coerce_int(
            self.config.get("gif_scale", 2),
            2,
            minimum=1,
        )

    def _get_configured_template_name(self, key: str) -> Optional[str]:
        value = self.config.get(key, "")
        if value is None:
            return None
        value = str(value).strip()
        return value or None

    def _resolve_existing_template(self, template_name: Optional[str], source: str) -> Optional[str]:
        if not template_name:
            return None
        if self._has_template(template_name):
            return template_name
        raise ValueError(f"{source} 指向的模板不存在: {template_name}")

    def _get_template_strategy(self) -> str:
        strategy = str(self.config.get("template_strategy", "fixed") or "fixed").strip().lower()
        if strategy not in {"fixed", "random", "round_robin"}:
            return "fixed"
        return strategy

    def _get_default_template(
        self,
        user_id: Optional[str] = None,
        for_render: bool = False,
    ) -> str:
        """
        解析默认模板。
        :param for_render: 为 True 时表示用于实际渲染，此时才应用
            template_strategy（random / round_robin）；提示词注入等
            需要稳定结果的场景保持 False。
        """
        available = self._require_available_templates()

        if user_id:
            user_template = self.user_default_template.get(user_id)
            if user_template and self._has_template(user_template):
                return user_template
            if user_template:
                self.user_default_template.pop(user_id, None)
                logger.warning(
                    f"[HTML渲染] 用户 {user_id} 的默认模板不存在，已清除失效配置: {user_template}"
                )

        # 用户未固定个人模板时，渲染阶段按策略在全部模板中挑选
        if for_render:
            strategy = self._get_template_strategy()
            if strategy == "random":
                chosen = random.choice(available)
                logger.info(f"[HTML渲染] 模板策略 random -> {chosen}")
                return chosen
            if strategy == "round_robin":
                chosen = available[self._tpl_round_robin_index % len(available)]
                self._tpl_round_robin_index += 1
                logger.info(f"[HTML渲染] 模板策略 round_robin -> {chosen}")
                return chosen

        configured_default = self._get_configured_template_name("default_template")
        resolved_default = self._resolve_existing_template(
            configured_default,
            "default_template",
        )
        if resolved_default:
            return resolved_default

        return available[0]

    def _get_merged_template(
        self,
        user_id: Optional[str] = None,
        render_matches: Optional[List[tuple[str, Optional[str], str, bool]]] = None,
    ) -> str:
        explicit_templates = [
            template_name
            for _, template_name, _, _ in render_matches or []
            if template_name
        ]
        if (
            render_matches
            and len(explicit_templates) == len(render_matches)
            and len(set(explicit_templates)) == 1
        ):
            try:
                return self._resolve_existing_template(
                    explicit_templates[0],
                    "render template",
                )
            except ValueError as exc:
                logger.warning(f"[HTML渲染] {exc}，将回退到默认模板")

        merged_template = self._get_configured_template_name("merged_template")
        if merged_template:
            try:
                return self._resolve_existing_template(
                    merged_template,
                    "merged_template",
                )
            except ValueError as exc:
                logger.warning(f"[HTML渲染] {exc}，将回退到默认模板")

        return self._get_default_template(user_id, for_render=True)

    def _build_current_template_prompt_block(self, current_template: str) -> str:
        prompt = self.template_mgr.extract_builtin_prompt(current_template)
        if not prompt:
            return ""

        return "\n".join(
            [
                "## 当前模板专属指令",
                f"当前用户偏好的模板是: **{current_template}**",
                (
                    f"除非用户在本轮明确要求其他模板，否则 <render> 标签必须使用 "
                    f"template=\"{current_template}\"，或省略 template 让系统使用当前模板。"
                ),
                "",
                f"### 模板「{current_template}」的专属指令",
                prompt,
            ]
        ).strip()

    def _extract_template_palette(self, template_name: str) -> Optional[dict]:
        extractor = getattr(self.template_mgr, "extract_color_palette", None)
        if not callable(extractor):
            return None
        try:
            return extractor(template_name)
        except Exception as exc:
            logger.warning(f"[HTML渲染] 提取模板 {template_name} 配色失败: {exc}")
            return None

    def _select_template(self, content: str, specified_template: Optional[str] = None, user_id: Optional[str] = None) -> str:
        available = self._require_available_templates()

        if specified_template:
            return self._resolve_existing_template(specified_template, "specified template")

        if user_id and user_id in self.user_default_template:
            user_tpl = self.user_default_template[user_id]
            if user_tpl in available:
                return user_tpl
            self.user_default_template.pop(user_id, None)
            logger.warning(f"[HTML渲染] 已移除失效的用户模板配置: {user_tpl}")

        if "猩红噩梦" in available and self._horror_template_pattern.search(content):
            return "猩红噩梦"

        if self.config.get("auto_dialogue_detection", True):
            quote_pat = self.config.get("dialogue_quote_pattern", "[\"'“”‘’「」『』]")
            quote_thr = self._coerce_int(
                self.config.get("dialogue_quote_threshold", 1),
                1,
                minimum=1,
            )
            try:
                is_dialogue = detect_dialogue(content, quote_pat, quote_thr)
            except re.error as exc:
                logger.warning(f"[HTML渲染] 对话引号匹配模式无效: {exc}")
                is_dialogue = False
            if is_dialogue and "dialogue" in available:
                return "dialogue"

        if self._is_auto_render_all_enabled():
            fallback = self._get_configured_template_name("auto_render_template")
            resolved_fallback = self._resolve_existing_template(
                fallback,
                "auto_render_template",
            )
            if resolved_fallback:
                return resolved_fallback

        return self._get_default_template(user_id, for_render=True)

    _WEEKDAY_NAMES = ("一", "二", "三", "四", "五", "六", "日")

    def _build_template_vars(self, event: Optional[AstrMessageEvent] = None) -> Dict[str, str]:
        """
        构造模板变量。模板中可用 {{date}}、{{time}}、{{weekday}}、
        {{sender_name}}、{{sender_id}}、{{platform}} 占位符，渲染前自动替换。
        """
        import datetime

        now = datetime.datetime.now()
        template_vars: Dict[str, str] = {
            "date": now.strftime("%Y年%m月%d日"),
            "time": now.strftime("%H:%M"),
            "weekday": f"星期{self._WEEKDAY_NAMES[now.weekday()]}",
            "sender_name": "朋友",
            "sender_id": "",
            "platform": "",
        }

        if event is None:
            return template_vars

        try:
            sender_name = ""
            if hasattr(event, "get_sender_name") and callable(event.get_sender_name):
                sender_name = str(event.get_sender_name() or "").strip()
            if not sender_name and hasattr(event, "sender"):
                sender_name = str(
                    getattr(event.sender, "nickname", "") or ""
                ).strip()
            if sender_name:
                template_vars["sender_name"] = sender_name
        except Exception:
            pass

        try:
            template_vars["sender_id"] = self._get_user_id(event)
        except Exception:
            pass
        template_vars["platform"] = self._get_platform_name(event)
        return template_vars

    def _fill_template_vars(
        self,
        template: str,
        extra_vars: Optional[Dict[str, str]] = None,
    ) -> str:
        """在插入 {{content}} 之前替换模板变量（不会影响正文中的同名字面量）"""
        template_vars = self._build_template_vars(None)
        if extra_vars:
            template_vars.update(extra_vars)
        for key, value in template_vars.items():
            placeholder = "{{" + key + "}}"
            if placeholder in template:
                template = template.replace(placeholder, str(value))
        return template

    @staticmethod
    def _inject_responsive_layout(html: str) -> str:
        """Make the outer page follow the configured render viewport width.

        Built-in main templates intentionally use a compact fixed-width card and
        ``body { display: inline-block; }``. That works at the default viewport,
        but increasing ``render_width`` otherwise only enlarges the screenshot
        canvas and leaves a large horizontal gutter around the card. The first
        body child is the page shell in every main template, so a single scoped
        override keeps all existing template artwork while allowing the shell to
        stretch to the available body width.
        """
        if 'id="astrbot-responsive-layout"' in html:
            return html

        responsive_css = """
<style id="astrbot-responsive-layout">
html,
body {
  width: 100% !important;
  min-width: 0 !important;
  max-width: none !important;
}

body {
  display: block !important;
}

body > :first-child {
  width: 100% !important;
  min-width: 0 !important;
  max-width: 100% !important;
  margin-left: auto !important;
  margin-right: auto !important;
}
</style>
"""

        if "</head>" in html:
            return html.replace("</head>", responsive_css + "</head>", 1)
        return responsive_css + html

    def _apply_template(
        self,
        content: str,
        template_name: str,
        is_raw_html: bool = False,
        extra_vars: Optional[Dict[str, str]] = None,
    ) -> str:
        """
        应用模板。
        :param is_raw_html: 若为 True，跳过 markdown/nl2br 处理，直接嵌入原始 HTML
        :param extra_vars: 模板变量（{{sender_name}} 等），在内容插入前替换
        """
        template = self.template_mgr.load_template(template_name)
        template = self._inject_responsive_layout(template)
        template = self._fill_template_vars(template, extra_vars)

        if is_raw_html:
            # 内容自带完整 HTML+CSS，不做 markdown 等文本处理。
            # 但混合输出（HTML 卡片 + 纯文本/语义标签）中的换行会被浏览器
            # 折叠，导致纯文本部分挤在一起，需保留空行分段。
            raw_nl_fixer = getattr(_text_processing, "nl2br_raw_html", None)
            if callable(raw_nl_fixer):
                content = raw_nl_fixer(content)
            return template.replace("{{content}}", content)

        if template_name == "dialogue":
            content = format_dialogue(content)
        else:
            if self.config.get("enable_markdown", True):
                content = markdown_to_html(content)
                return template.replace("{{content}}", content)
            else:
                content = preserve_newlines(content)

        content = nl2br(content)
        return template.replace("{{content}}", content)

    # ==================== 渲染核心 ====================

    async def _render_content(
        self,
        content: str,
        specified_template: Optional[str],
        user_id: Optional[str] = None,
        is_gif: bool = False,
        extra_vars: Optional[Dict[str, str]] = None,
    ):
        """
        执行渲染。
        GIF 模式返回 List[Image]（静态图 + GIF），普通模式返回单个 Image。
        失败返回 None。
        """
        try:
            template_name = self._select_template(content, specified_template, user_id)
            logger.info(
                f"[HTML渲染] 使用模板: {template_name}, "
                f"指定模板: {specified_template or '未指定'}, GIF模式: {is_gif}"
            )

            # 检测内容是否自带 <style> 标签，若有则为完整 HTML，跳过文本处理
            has_own_style = bool(re.search(r'<style\b', content, re.IGNORECASE))
            full_html = self._apply_template(
                content,
                template_name,
                is_raw_html=has_own_style,
                extra_vars=extra_vars,
            )
            if self.config.get("enable_math", True) and _contains_math(content):
                full_html = self._inject_math_assets(full_html)
            # 注入自定义背景图（转为 base64 内嵌，避免 Playwright 沙箱限制）
            bg_data_url = self._get_bg_data_url()
            if bg_data_url:
                bg_render_mode = self._get_background_render_mode()
                full_html = self._inject_background_image(full_html, bg_data_url, bg_render_mode)
            # GIF 模式始终用 .jpg 作为主输出（JPEG体积远小于PNG，渲染更快）
            filename_base = f"render_{uuid.uuid4().hex[:12]}"
            output_path = os.path.join(self.IMAGE_CACHE_DIR, f"{filename_base}.jpg")

            width = self._coerce_int(
                self.config.get("render_width", 600),
                600,
                minimum=1,
            )
            if is_gif:
                scale = self._get_gif_scale()
            else:
                scale = self._coerce_int(
                    self.config.get("render_scale", 2),
                    2,
                    minimum=1,
                )

            success = await html_to_image_playwright(
                html_content=full_html,
                output_image_path=output_path,
                scale=scale,
                width=width,
                is_gif=is_gif,
                duration=self.gif_duration,
                fps=self.gif_fps,
            )

            if not success:
                return None

            if is_gif:
                results = []
                delete_paths = []
                if os.path.exists(output_path):
                    results.append(Image.fromFileSystem(output_path))
                    delete_paths.append(output_path)
                gif_path = os.path.join(self.IMAGE_CACHE_DIR, f"{filename_base}.gif")
                if os.path.exists(gif_path):
                    results.append(Image.fromFileSystem(gif_path))
                    delete_paths.append(gif_path)
                if delete_paths:
                    self._schedule_delete(*delete_paths)
                return results if results else None
            else:
                if os.path.exists(output_path):
                    img = Image.fromFileSystem(output_path)
                    self._schedule_delete(output_path)
                    return img
                return None
        except Exception as e:
            logger.error(f"渲染过程异常: {e}")
            import traceback
            logger.error(traceback.format_exc())
            raise

    async def _process_text(
        self,
        text: str,
        user_id: Optional[str] = None,
        extra_vars: Optional[Dict[str, str]] = None,
        panel_scope: Optional[str] = None,
    ) -> List:
        self._refresh_sub_html_panels()
        panel_state = await self._get_panel_state(panel_scope or user_id)
        previous_panel_state = {
            panel_id: dict(payload)
            for panel_id, payload in panel_state.items()
            if isinstance(payload, dict)
        }
        text = self.sub_panel_mgr.expand_panels(text, state_store=panel_state)
        if panel_state != previous_panel_state:
            await self._save_panel_state(panel_scope or user_id, panel_state)
        components: List = []
        render_matches = detect_render_tag(text)

        if render_matches:
            if (
                self.config.get("auto_merge_renders", True)
                and not any(is_gif for _, _, _, is_gif in render_matches)
            ):
                merged_template = self._get_merged_template(user_id, render_matches)
                merged_content = self._strip_render_tags(text)
                if self._is_meaningful_text_fragment(merged_content):
                    try:
                        merged_result = await self._render_content(
                            merged_content,
                            merged_template,
                            user_id,
                            False,
                            extra_vars=extra_vars,
                        )
                    except Exception as exc:
                        logger.warning(f"[HTML渲染] 合并渲染异常，回退到分段渲染: {exc}")
                    else:
                        if merged_result:
                            if isinstance(merged_result, list):
                                components.extend(merged_result)
                            else:
                                components.append(merged_result)
                            return components
                        logger.warning("[HTML渲染] 合并渲染失败，回退到分段渲染")

            # 有 <render> 标签：按标签分割，每个块用指定模板渲染
            # 标签之间/之后的剩余内容也一并渲染（不发纯文本）
            logger.info(f"[HTML渲染] 检测到 {len(render_matches)} 个 <render> 标签")
            remaining = text
            last_template = None

            for full_match, tpl_name, content, is_gif in render_matches:
                parts = remaining.split(full_match, 1)
                before = parts[0]
                remaining = parts[1] if len(parts) > 1 else ""

                # render 块之前的文本：也渲染成图片
                # 过滤掉 HTML 注释、纯空白、纯符号等无意义内容
                before_clean = before.strip() if before else ""
                if self._is_meaningful_text_fragment(before_clean):
                    before_result = await self._render_content(
                        before.strip(), last_template or tpl_name, user_id, False,
                        extra_vars=extra_vars,
                    )
                    if before_result:
                        if isinstance(before_result, list):
                            components.extend(before_result)
                        else:
                            components.append(before_result)
                    else:
                        logger.warning("[HTML渲染] render块之间的内容渲染失败，跳过")

                # render 块本身
                if self._is_trivial_render_payload(content):
                    logger.info("[HTML渲染] render块内容为空或仅含符号，已跳过")
                    if tpl_name:
                        last_template = tpl_name
                    continue
                result = await self._render_content(
                    content, tpl_name, user_id, is_gif, extra_vars=extra_vars
                )
                if result:
                    if isinstance(result, list):
                        components.extend(result)
                    else:
                        components.append(result)
                else:
                    logger.warning(f"[HTML渲染] render块渲染失败，模板: {tpl_name}")

                if tpl_name:
                    last_template = tpl_name

            # 最后一个 render 块之后的剩余文本：也渲染成图片
            if self._is_meaningful_text_fragment(remaining):
                remaining_result = await self._render_content(
                    remaining.strip(), last_template, user_id, False,
                    extra_vars=extra_vars,
                )
                if remaining_result:
                    if isinstance(remaining_result, list):
                        components.extend(remaining_result)
                    else:
                        components.append(remaining_result)
                else:
                    logger.warning("[HTML渲染] render标签后的剩余内容渲染失败，跳过")

        else:
            # 无 <render> 标签：整体用默认模板渲染
            logger.info("[HTML渲染] 无 <render> 标签，整体渲染")
            result = await self._render_content(
                text.strip(), None, user_id, False, extra_vars=extra_vars
            )
            if result:
                if isinstance(result, list):
                    components.extend(result)
                else:
                    components.append(result)
            else:
                logger.warning("[HTML渲染] 整体渲染失败，跳过")

        return components

    def _detect_should_render(self, text: str, has_render_tag: bool) -> bool:
        if has_render_tag:
            return False
        if not self._is_auto_detect_enabled():
            return False
        return detect_html_tags(text)

    def _should_use_original_text_for_render(
        self,
        original_text: str,
        plain_texts: List[str],
    ) -> bool:
        """
        某些链路会把 Plain 文本截断到第一个 HTML 标签之前，
        导致 result.chain 里丢失 <render> 块。此时回退到 LLM 原始输出。
        """
        if not original_text:
            return False

        original_clean = self._normalize_render_source_text(original_text)
        if not original_clean:
            return False

        self._refresh_sub_html_panels()
        original_render_matches = detect_render_tag(original_clean)
        original_has_panel = self.sub_panel_mgr.contains_enabled_panel_tag(original_clean)

        plain_clean = "\n".join(
            self._normalize_render_source_text(text)
            for text in plain_texts
            if text and text.strip()
        ).strip()
        plain_render_matches = detect_render_tag(plain_clean) if plain_clean else []
        plain_has_panel = (
            self.sub_panel_mgr.contains_enabled_panel_tag(plain_clean)
            if plain_clean
            else False
        )
        return bool(
            (original_render_matches and not plain_render_matches)
            or (original_has_panel and not plain_has_panel)
        )

    @staticmethod
    def _strip_render_tags(text: str) -> str:
        if not text:
            return text
        text = re.sub(r"</?render\b[^>]*>", "\n\n", text, flags=re.IGNORECASE)
        text = re.sub(r"[ \t]*\n[ \t]*", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    @staticmethod
    def _is_meaningful_text_fragment(text: str) -> bool:
        if not text:
            return False

        cleaned = re.sub(r'<!--.*?-->', '', text, flags=re.DOTALL)
        cleaned = HtmlRenderPlugin._strip_internal_markup(cleaned).strip()
        if not cleaned:
            return False

        if re.search(r'<\s*/?\s*[A-Za-z!][^>]*>', cleaned):
            return True

        visible_text = re.sub(r'<[^>]+>', '', cleaned)
        return not HtmlRenderPlugin._is_punctuation_or_symbol_only(visible_text)

    def _has_explicit_render_request(self, text: str) -> bool:
        self._refresh_sub_html_panels()
        if self.sub_panel_mgr.contains_enabled_panel_tag(text):
            return True
        has_render_tag = bool(detect_render_tag(text))
        return has_render_tag or self._detect_should_render(text, has_render_tag)

    @staticmethod
    def _strip_internal_markup(
        text: str,
        strip_pic: bool = True,
        strip_lora: bool = True,
    ) -> str:
        if not text:
            return text

        if strip_pic:
            text = HtmlRenderPlugin._PIC_TAG_PATTERN.sub("", text)
            text = HtmlRenderPlugin._PIC_UNCLOSED_TAG_PATTERN.sub("", text)
            text = re.sub(r'</pic\s*>', '', text, flags=re.IGNORECASE)
        if strip_lora:
            text = HtmlRenderPlugin._LORA_TAG_PATTERN.sub("", text)
            text = re.sub(r'</lora\s*>', '', text, flags=re.IGNORECASE)
        text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(
            r'<context_summary>.*?</context_summary>',
            '',
            text,
            flags=re.DOTALL | re.IGNORECASE,
        )
        text = re.sub(r'</?ctx>', '', text, flags=re.IGNORECASE)
        return text

    @staticmethod
    def _is_punctuation_or_symbol_only(text: str) -> bool:
        compact = re.sub(r'\s+', '', text or '')
        if not compact:
            return True
        return all(unicodedata.category(ch)[0] in {"P", "S"} for ch in compact)

    @classmethod
    def _extract_visible_text(cls, text: str) -> str:
        if not text:
            return ""

        text = re.sub(r'<!--.*?-->', '', text, flags=re.DOTALL)
        text = cls._strip_internal_markup(text)
        text = re.sub(r'<[^>]+>', '', text)
        text = re.sub(r'\s+', '', text)
        return text

    @classmethod
    def _is_trivial_render_payload(cls, text: str) -> bool:
        if not text:
            return True

        cleaned = re.sub(r'<!--.*?-->', '', text, flags=re.DOTALL)
        cleaned = cls._strip_internal_markup(cleaned).strip()
        if not cleaned:
            return True

        if re.search(r'<\s*/?\s*[A-Za-z!][^>]*>', cleaned):
            return False

        visible_text = re.sub(r'<[^>]+>', '', cleaned)
        visible_text = re.sub(r'\s+', '', visible_text)
        if not visible_text:
            return True

        return cls._is_punctuation_or_symbol_only(visible_text)

    # ==================== 命令 ====================

    @filter.command("html状态", aliases=["html_status"])
    async def cmd_html_status(self, event: AstrMessageEvent):
        yield event.plain_result(await self._build_render_status_message(event))

    @filter.command("html开", aliases=["html_on"])
    async def cmd_html_on(self, event: AstrMessageEvent):
        _, session_label = await self._set_session_render_enabled(event, True)
        status = await self._build_render_status_message(event)
        yield event.plain_result(f"✅ 已开启当前会话的 HTML 渲染\n会话: {session_label}\n\n{status}")

    @filter.command("html关", aliases=["html_off"])
    async def cmd_html_off(self, event: AstrMessageEvent):
        _, session_label = await self._set_session_render_enabled(event, False)
        status = await self._build_render_status_message(event)
        yield event.plain_result(f"🛑 已关闭当前会话的 HTML 渲染\n会话: {session_label}\n\n{status}")

    @filter.command("html重置", aliases=["html_reset"])
    async def cmd_html_reset(self, event: AstrMessageEvent):
        _, session_label = await self._set_session_render_enabled(event, None)
        status = await self._build_render_status_message(event)
        yield event.plain_result(f"♻️ 当前会话已恢复默认 HTML 渲染状态\n会话: {session_label}\n\n{status}")

    @filter.command("测试", aliases=["test"])
    async def cmd_test_render(self, event: AstrMessageEvent):
        full_msg = event.message_str.strip()
        full_msg = re.sub(r'\[At:\d+\]\s*', '', full_msg).strip()
        parts = full_msg.split(None, 1)
        text = parts[1].strip() if len(parts) > 1 else ""

        user_id = self._get_user_id(event)

        if not text:
            try:
                tpl = self._get_default_template(user_id)
            except Exception as e:
                yield event.plain_result(f"渲染失败：{e}")
                return
            text = TemplateManager.get_default_test_content(tpl)
        elif text.strip().lower() == "gif":
            text = TemplateManager.get_gif_test_content()
            logger.info("[HTML渲染] 使用 GIF 弹幕测试内容")

        template_vars = self._build_template_vars(event)
        if '<render' in text:
            try:
                comps = await self._process_text(
                    text,
                    user_id,
                    extra_vars=template_vars,
                    panel_scope=self._get_panel_state_scope(event),
                )
            except Exception as e:
                yield event.plain_result(f"渲染失败：{e}")
                return
            filtered = [c for c in comps if not (isinstance(c, Plain) and not c.text.strip())]
            if filtered:
                yield event.chain_result(filtered)
            else:
                yield event.plain_result("❌ 渲染失败，请检查日志获取详细信息")
        else:
            try:
                tpl = self._get_default_template(user_id)
                image = await self._render_content(
                    text, tpl, user_id, False, extra_vars=template_vars
                )
            except Exception as e:
                yield event.plain_result(f"渲染失败：{e}")
                return
            if image:
                yield event.chain_result([image])
            else:
                yield event.plain_result("❌ 渲染失败，请检查日志获取详细信息")

    @filter.command("切换", aliases=["switch"])
    async def cmd_switch_template(self, event: AstrMessageEvent):
        full_msg = event.message_str.strip()
        full_msg = re.sub(r'\[At:\d+\]\s*', '', full_msg).strip()
        parts = full_msg.split(None, 1)
        arg = parts[1].strip() if len(parts) > 1 else ""

        user_id = self._get_user_id(event)
        try:
            current = self._get_default_template(user_id)
        except Exception:
            current = "未设置"
        available = self._get_available_templates()
        if not available:
            yield event.plain_result(
                f"渲染失败：未找到任何模板文件，请先在 {self.template_mgr.TEMPLATE_DIR} 中放入至少一个 .html 模板"
            )
            return

        self.template_mgr.update_template_id_map()

        if not arg:
            yield event.plain_result(
                f"🔄 切换渲染模板\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"用法: /切换 <模板名或ID>\n"
                f"当前模板: {current}\n\n"
                f"示例:\n  /切换 <模板名>\n  /切换 1\n\n"
                f"使用 /查看 查看可用模板列表"
            )
            return

        template_name = None
        try:
            tid = int(arg)
            template_name = self.template_mgr.template_id_map.get(tid)
        except ValueError:
            pass

        if not template_name:
            if arg in available:
                template_name = arg

        if not template_name:
            yield event.plain_result(f"❌ 未找到模板: {arg}\n\n请使用 /查看 查看可用模板列表")
            return

        self.user_default_template[user_id] = template_name
        logger.info(f"[HTML渲染] 用户 {user_id} 切换默认模板: {current} -> {template_name}")
        yield event.plain_result(f"✅ 已切换默认模板为: {template_name}")
    @filter.command("探针gif", aliases=["probegif"])
    async def cmd_probe_gif(self, event: AstrMessageEvent):
        """诊断 GIF 渲染问题：截取多帧并保存为独立图片"""
        from playwright.async_api import async_playwright
        from template_manager import TemplateManager

        html_content = TemplateManager.get_gif_test_content()
        # 移除 <render gif> 标签，只保留 HTML
        html_content = re.sub(r'<render[^>]*>', '', html_content)
        html_content = re.sub(r'</render>', '', html_content)

        yield event.plain_result("🔍 开始 GIF 渲染探针，请稍候...")

        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch()
                context = await browser.new_context(
                    device_scale_factor=2,
                    viewport={"width": 600, "height": 800},
                )
                page = await context.new_page()
                await page.set_content(html_content, wait_until="networkidle")

                # 展开视口
                content_h = await page.evaluate("document.body.scrollHeight")
                await page.set_viewport_size({"width": 600, "height": max(content_h, 200)})
                await asyncio.sleep(1.0)

                # 检查弹幕元素是否存在
                danmu_count = await page.evaluate("document.querySelectorAll('.danmu-line').length")
                logger.info(f"[探针] 弹幕元素数量: {danmu_count}")

                # 检查弹幕元素的实际位置和样式
                danmu_info = await page.evaluate("""() => {
                    const items = document.querySelectorAll('.danmu-line');
                    return Array.from(items).map((el, i) => {
                        const rect = el.getBoundingClientRect();
                        const style = getComputedStyle(el);
                        return {
                            index: i,
                            text: el.textContent.substring(0, 20),
                            x: Math.round(rect.x),
                            y: Math.round(rect.y),
                            width: Math.round(rect.width),
                            height: Math.round(rect.height),
                            visible: rect.width > 0 && rect.height > 0,
                            animation: style.animation,
                            animationPlayState: style.animationPlayState,
                            transform: style.transform,
                            left: style.left,
                            opacity: style.opacity,
                            display: style.display,
                        };
                    });
                }""")

                for info in danmu_info:
                    logger.info(f"[探针] 弹幕#{info['index']}: "
                               f"text='{info['text']}' "
                               f"pos=({info['x']},{info['y']}) "
                               f"size={info['width']}x{info['height']} "
                               f"visible={info['visible']} "
                               f"animation='{info['animation']}' "
                               f"state='{info['animationPlayState']}' "
                               f"transform='{info['transform']}' "
                               f"left='{info['left']}'")

                # 截取 3 帧，间隔 1 秒
                probe_images = []
                for i in range(3):
                    shot_path = os.path.join(self.IMAGE_CACHE_DIR, f"probe_frame_{i}.png")
                    await page.screenshot(path=shot_path, full_page=True)
                    probe_images.append(Image.fromFileSystem(shot_path))
                    logger.info(f"[探针] 已截取第 {i+1} 帧")
                    if i < 2:
                        await asyncio.sleep(1.0)

                await browser.close()

            # 发送 3 帧截图
            result_chain = [Plain(f"🔍 探针结果：检测到 {danmu_count} 个弹幕元素\n详细信息请查看控制台日志\n\n以下是间隔1秒的3帧截图：")]
            result_chain.extend(probe_images)
            yield event.chain_result(result_chain)

        except Exception as e:
            logger.error(f"[探针] 失败: {e}")
            import traceback
            logger.error(traceback.format_exc())
            yield event.plain_result(f"❌ 探针失败: {e}")
    @filter.command("预览模板", aliases=["previewtpl", "tplpreview"])
    async def cmd_preview_template(self, event: AstrMessageEvent):
        full_msg = event.message_str.strip()
        full_msg = re.sub(r'\[At:\d+\]\s*', '', full_msg).strip()
        parts = full_msg.split(None, 2)
        arg = parts[1].strip() if len(parts) > 1 else ""
        text = parts[2].strip() if len(parts) > 2 else ""

        if not arg:
            yield event.plain_result("📖 用法: /预览模板 <模板名或ID> [文本]\n示例: /预览模板 <模板名> 晚风穿过旧街，灯火一盏盏亮起来。")
            return

        available = self._get_available_templates()
        if not available:
            yield event.plain_result(
                f"渲染失败：未找到任何模板文件，请先在 {self.template_mgr.TEMPLATE_DIR} 中放入至少一个 .html 模板"
            )
            return

        self.template_mgr.update_template_id_map()
        template_name = None
        try:
            tid = int(arg)
            template_name = self.template_mgr.template_id_map.get(tid)
        except ValueError:
            pass
        if not template_name and arg in available:
            template_name = arg
        if not template_name:
            yield event.plain_result(f"❌ 未找到模板: {arg}")
            return

        user_id = self._get_user_id(event)
        if not text:
            text = TemplateManager.get_default_test_content(template_name)
        try:
            image = await self._render_content(
                text,
                template_name,
                user_id,
                False,
                extra_vars=self._build_template_vars(event),
            )
        except Exception as e:
            yield event.plain_result(f"渲染失败：{e}")
            return
        if image:
            yield event.chain_result([Plain(f"🖼️ 模板预览: {template_name}"), image])
        else:
            yield event.plain_result("❌ 模板预览失败，请检查日志")

    @filter.command("查看", aliases=["templates"])
    async def cmd_list_templates(self, event: AstrMessageEvent):
        available = self._get_available_templates()
        if not available:
            yield event.plain_result("❌ 当前没有可用的模板")
            return

        self.template_mgr.update_template_id_map()
        user_id = self._get_user_id(event)
        try:
            current = self._get_default_template(user_id)
        except Exception:
            current = "未设置"

        lines = ["📋 可用模板列表", "━━━━━━━━━━━━━━━━━━", ""]
        for idx in sorted(self.template_mgr.template_id_map.keys()):
            name = self.template_mgr.template_id_map[idx]
            marker = " ← 当前" if name == current else ""
            lines.append(f"  {idx}. {name}{marker}")

        lines.append("")
        lines.append("━━━━━━━━━━━━━━━━━━")
        lines.append("使用方法:")
        lines.append("  /切换 <ID或名称>      切换默认模板")
        lines.append("  /测试 <文本>          测试渲染效果")
        lines.append("  /预览模板 <ID或名称> [文本]  临时预览指定模板")

        yield event.plain_result("\n".join(lines))

    # ==================== 事件钩子 ====================

    @filter.on_llm_request()
    async def on_llm_req(self, event: AstrMessageEvent, req: ProviderRequest):
        self._refresh_sub_html_panels()
        self._strip_panel_markup_from_request_context(req)
        if not await self._is_render_enabled_for_session(event):
            return

        has_enabled_panels = bool(self.sub_panel_mgr.get_enabled_panels())
        inject_render_prompt = bool(self.config.get("inject_prompt", True))
        if not inject_render_prompt and not has_enabled_panels:
            return

        available_templates = self._get_available_templates()
        if not available_templates:
            return

        template_list = ", ".join(available_templates)
        user_id = self._get_user_id(event)
        current_template = self._get_default_template(user_id)
        example_template = current_template
        panel_prompt = self.sub_panel_mgr.build_prompt_block(
            theme_palette=self._extract_template_palette(current_template),
        )
        panel_state_prompt = ""
        if has_enabled_panels:
            panel_state_prompt = await self._build_panel_state_context(
                self._get_panel_state_scope(event),
            )

        instruction = f"""
## HTML 渲染功能

### 背景说明
你的回复会被渲染系统自动转换成精美图片发送给用户。渲染系统的工作原理是：
1. 系统解析你回复中的 <render> 标签，提取内容
2. 将内容嵌入到模板 HTML 的 {{{{content}}}} 占位符位置
3. 使用无头浏览器将完整 HTML 截图为图片发送

因此，你需要理解两种内容模式：

**模式A - 模板内容（常用）**：你只需输出纯文本和语义标签，系统自动套用模板样式。适用于日常对话、小说创作、角色扮演等。

**模式B - 自定义HTML内容**：你自己编写 <style> 和 HTML 结构，系统检测到 <style> 标签后会跳过文本处理，直接将你的 HTML 嵌入模板容器中渲染。适用于用户要求制作特殊页面、数据可视化、自定义排版等场景。

**两种模式不冲突**，都需要用 <render> 标签包裹，区别仅在于内容是否自带 <style>。也可以在同一个 <render> 内混合使用（见下方"混合输出"规则）。

### 语义标签（模式A使用）
在模板内容中，使用以下语义标签可以让渲染效果更丰富：
- <q>对话内容</q> → 对话台词，显示为引号样式
- <inner>想法</inner> → 内心活动，显示为灰色斜体
- <act>动作</act> → 动作描写，显示为特殊颜色
- <scene>场景</scene> → 场景环境描写，显示为独立段落块
- <aside>旁白</aside> → 叙述性旁白，居中显示

### 混合输出（模式B卡片 + 模式A正常回复同屏）
当你需要在一次回复中既展示自定义 HTML 卡片、又进行正常对话/剧情时，必须遵守以下规则，否则你的样式会和模板样式互相冲突，产生文字错乱、划线、配色诡异等问题：

1. **CSS 只能用类选择器**：所有自定义样式都写成 `.my-panel`、`.my-panel .row` 这类**类选择器**，并且全部作用在你自己的容器 div 内。
2. **禁止全局样式**：不要写 `body`、`html`、`p`、`div`、`span` 等全局/元素选择器的样式规则——模板已经定义了整体配色和排版，你的全局规则会与之叠加冲突。
3. **禁止重定义语义标签样式**：绝对不要在 <style> 里为 scene、act、q、inner、aside 写样式。模板已为它们配好了完整样式（颜色、气泡、下划线、间距等），你再定义一次会导致两套样式叠加，出现类似"文字被划掉"、"配色错乱"的效果。
4. **语义标签必须裸露在顶层**：正常回复部分的 <scene>/<act>/<q>/<inner>/<aside> 直接写在 <render> 内的顶层，**不要**把它们包进你自定义的 div 容器里，也不要给这部分套背景色块——让模板来渲染它们。
5. **配色交给模板**：你无法预知用户当前用的是深色还是浅色模板，所以自定义卡片配色尽量中性柔和（半透明背景、跟随文字颜色），不要为整个页面强加深色/浅色主题背景。
6. **段落间用空行分隔**：<act>、<q> 等相邻的行内语义标签之间空一行，系统会自动转换为换行分段。

**混合输出正确示例：**
<render>
<style>
.status-panel {{ border: 1px solid rgba(128,128,128,0.35); border-radius: 12px; padding: 16px; margin-bottom: 8px; }}
.status-panel .row {{ display: flex; justify-content: space-between; padding: 4px 0; font-size: 13px; }}
.status-panel .label {{ opacity: 0.7; }}
</style>
<div class="status-panel">
  <div class="row"><span class="label">状态</span><span>示例数值</span></div>
  <div class="row"><span class="label">好感度</span><span>100</span></div>
</div>

<scene>自定义卡片展示完毕，下面回归正常剧情。</scene>

<act>她合上手中的面板，抬起头看向对方。</act>

<q>以上就是这次的状态汇报。</q>

<inner>接下来该说点什么呢……</inner>
</render>

注意示例中：卡片 CSS 全部是类选择器且没有碰 body 和语义标签；语义标签直接裸露在顶层、彼此之间空一行。

### <render> 标签语法
```
<render>内容</render>                          — 使用用户默认模板
<render template="模板名">内容</render>         — 指定模板
<render gif>内容</render>                       — 默认模板 + 生成GIF动图
<render template="模板名" gif>内容</render>      — 指定模板 + 生成GIF动图
```
可用模板: {template_list}
不指定 template 时，系统自动使用用户的默认模板。

### GIF 动图模式
当你在 <render> 标签中加入 `gif` 属性时，系统会：
1. 先生成一张完整的静态截图（PNG）
2. 自动检测页面中带有 CSS 动画（@keyframes）的区域
3. 对该动画区域录制多帧并合成为 GIF 动图
4. 同时发送静态图和 GIF 给用户

使用 GIF 模式时，你必须使用**模式B（自定义HTML）**，在 <style> 中定义 @keyframes 动画。系统会自动检测动画并录制。

典型应用场景：弹幕滚动效果、文字逐帧出现、元素移动/渐变动画等。

GIF 示例结构：
<render gif>
<style>
.container {{ /* 容器样式 */ }}
.animated-item {{
    animation: myAnimation 6s linear infinite;
}}
@keyframes myAnimation {{
    0% {{ transform: translateX(0); }}
    100% {{ transform: translateX(-100%); }}
}}
</style>
<div class="container">
    <div class="animated-item">滚动的内容</div>
</div>
</render>

### 重要规则（按优先级排列）
1. **（最高优先级）标签完整性**：如果使用 <render> 标签，所有内容必须在标签内部，标签外不要遗留任何内容。回复的结尾必须是 </render> 闭合标签。
   即：
   <render template="模板名">
   所有回复内容都在这里
   </render>

2. **禁止代码块包裹**：不要用 ```html ``` 或任何 ``` 代码块包裹你的输出。直接写内容即可，代码块标记会导致内容被当作纯文本展示而无法渲染。

3. **内容完整性**：你的所有回复内容都会被渲染成图片，请确保所有内容（包括状态面板、角色信息等）都在回复中完整输出。

4. **模式B注意事项**：当用户要求你输出自定义 HTML（如制作页面、卡片、可视化等），使用模式B——在 <render> 内部直接写 <style> 和 HTML 标签。不要把 HTML 放在 <render> 标签外面。

### 示例

**模式A示例（模板内容）：**
<render template="{example_template}">
<scene>月光如水，洒落在寂静的庭院中。</scene>

林晓站在门口，望着眼前的身影，心跳不由得加速起来。

<act>她缓缓转过身来</act>，月光勾勒出她清冷的轮廓。

<q>你怎么会在这里？</q>

<inner>不对，这个时间他不应该出现才对……</inner>

他没有回答，只是静静地看着她。

<aside>命运的齿轮，从这一刻开始转动。</aside>
</render>

**模式B示例（自定义HTML）：**
<render>
<style>
.my-card {{ background: #fff; border-radius: 12px; padding: 24px; box-shadow: 0 4px 12px rgba(0,0,0,0.1); }}
.my-card h2 {{ color: #333; margin-bottom: 12px; }}
</style>
<div class="my-card">
    <h2>自定义卡片标题</h2>
    <p>这里是自定义排版的内容。</p>
</div>
</render>
"""
        injected_sections = []

        if inject_render_prompt:
            injected_sections.append(instruction.strip())

            current_template_prompt = self._build_current_template_prompt_block(current_template)
            if current_template_prompt:
                injected_sections.append(current_template_prompt)
                logger.info(f"[HTML渲染] 已注入当前模板专属提示词: {current_template}")

        if panel_prompt:
            injected_sections.append(panel_prompt)
            logger.info(
                f"[HTML渲染] 已注入 {len(self.sub_panel_mgr.get_enabled_panels())} 个子 HTML 面板协议"
            )

        if panel_state_prompt:
            injected_sections.append(panel_state_prompt)

        req.system_prompt = self._prepend_prompt_before_chatroom_history(
            req.system_prompt,
            "\n\n".join(section for section in injected_sections if section),
        )

    @filter.on_llm_response(priority=40)
    async def on_llm_response(self, event: AstrMessageEvent, resp):
        if not await self._is_render_enabled_for_session(event):
            if resp and getattr(resp, "completion_text", None):
                source = str(resp.completion_text)
                event.set_extra("html_render_original_text", source)
                if self._has_history_cleanup_markup(source):
                    event.set_extra(
                        self._HISTORY_ASSISTANT_CONTENT_OVERRIDE_EXTRA_KEY,
                        self._clean_assistant_history_text(event, source, source),
                    )
            return

        if resp:
            supplemented_text, supplemented_panel_tags = (
                await self._supplement_missing_always_panels(
                    event,
                    resp.completion_text,
                )
            )
            if supplemented_panel_tags:
                resp.completion_text = supplemented_text
                resp.result_chain = MessageChain().message(supplemented_text)

                comfy_cleaned_text = event.get_extra("comfy_cleaned_text")
                if comfy_cleaned_text is not None:
                    event.set_extra(
                        "comfy_cleaned_text",
                        self._insert_panel_tags(
                            comfy_cleaned_text,
                            supplemented_panel_tags,
                        ),
                    )

            if not resp.completion_text:
                return

            preserve_comfy_prompt = self._should_preserve_comfy_prompt_in_history(event)
            cleaned = event.get_extra("comfy_cleaned_text")
            text_to_save = resp.completion_text if preserve_comfy_prompt else (cleaned if cleaned else resp.completion_text)
            event.set_extra("html_render_original_text", text_to_save)

            if event.get_extra(self._HISTORY_ASSISTANT_DROP_EXTRA_KEY, False):
                return

            history_source_override = event.get_extra(
                self._HISTORY_ASSISTANT_CONTENT_OVERRIDE_EXTRA_KEY,
            )
            has_history_override = history_source_override is not None
            history_source = history_source_override if has_history_override else text_to_save
            cleaned_history_text = self._clean_assistant_history_text(
                event,
                history_source,
                resp.completion_text,
            )
            should_set_history_override = (
                has_history_override
                and cleaned_history_text != str(history_source_override or "").strip()
            ) or (
                not has_history_override
                and self._has_history_cleanup_markup(history_source)
            )
            if should_set_history_override:
                event.set_extra(
                    self._HISTORY_ASSISTANT_CONTENT_OVERRIDE_EXTRA_KEY,
                    cleaned_history_text,
                )

    @filter.on_decorating_result(priority=40)
    async def on_decorating_result(self, event: AstrMessageEvent):
        if not await self._is_render_enabled_for_session(event):
            return

        result = event.get_result()
        if not result or not result.chain:
            return

        is_llm_result = result.is_llm_result() if hasattr(result, "is_llm_result") else False
        has_existing_images = any(isinstance(item, Image) for item in result.chain)
        original_text = event.get_extra("html_render_original_text")

        # 回退机制：当其他插件（如主动消息插件）绕过标准 LLM 链路时，
        # on_llm_response 不会被触发，html_render_original_text 不会被设置。
        # 此时从 chain 中的 Plain 组件提取文本作为渲染源。
        if not original_text:
            plain_texts = []
            for item in result.chain:
                if isinstance(item, Plain) and item.text and item.text.strip():
                    plain_texts.append(item.text)
            if not plain_texts:
                return
            if not is_llm_result:
                has_explicit_render = any(
                    self._has_explicit_render_request(text)
                    for text in plain_texts
                    if text and text.strip()
                )
                if not has_explicit_render:
                    return
            original_text = "\n".join(plain_texts)
            logger.debug("[HTML渲染] 未找到 original_text extra，从消息链中提取文本进行渲染")

        user_id = self._get_user_id(event)
        panel_scope = self._get_panel_state_scope(event)
        plain_texts = [
            item.text for item in result.chain
            if isinstance(item, Plain) and item.text and item.text.strip()
        ]
        render_source_override = None
        if is_llm_result and original_text and self._should_use_original_text_for_render(original_text, plain_texts):
            render_source_override = self._normalize_render_source_text(original_text)
            logger.info("[HTML渲染] Plain 消息链缺少 <render> 块，改用 LLM 原始输出进行渲染")

        # 渲染消息链
        new_chain: List = []
        render_override_consumed = False
        auto_render_all = self._is_auto_render_all_enabled()
        rendered_via_plugin = False
        template_vars = self._build_template_vars(event)
        for item in result.chain:
            if isinstance(item, Plain):
                # 清理可能残留的 <pic> 和 <think> 标签
                if render_source_override is not None:
                    if render_override_consumed:
                        continue
                    text_to_render = render_source_override
                    render_override_consumed = True
                else:
                    text_to_render = self._normalize_render_source_text(item.text)
                if text_to_render:
                    if self._is_trivial_render_payload(text_to_render):
                        logger.info("[HTML渲染] 检测到仅含符号的渲染残片，已跳过")
                        continue
                    has_explicit_render = self._has_explicit_render_request(text_to_render)
                    if not auto_render_all and not has_explicit_render:
                        new_chain.append(Plain(text_to_render))
                        continue
                    if has_existing_images and not has_explicit_render:
                        new_chain.append(Plain(text_to_render))
                        continue
                    if not is_llm_result and not has_explicit_render:
                        new_chain.append(Plain(text_to_render))
                        continue
                    if self._should_skip_auto_render(text_to_render):
                        new_chain.append(Plain(text_to_render))
                        continue
                    try:
                        comps = await self._process_text(
                            text_to_render,
                            user_id,
                            extra_vars=template_vars,
                            panel_scope=panel_scope,
                        )
                    except Exception as e:
                        new_chain.append(Plain(f"渲染失败：{e}"))
                        continue
                    if comps:
                        rendered_via_plugin = True
                    new_chain.extend(comps)
            else:
                new_chain.append(item)

        if render_source_override is not None and not render_override_consumed:
            if not self._is_trivial_render_payload(render_source_override):
                try:
                    comps = await self._process_text(
                        render_source_override,
                        user_id,
                        extra_vars=template_vars,
                        panel_scope=panel_scope,
                    )
                except Exception as e:
                    new_chain.append(Plain(f"渲染失败：{e}"))
                else:
                    if comps:
                        rendered_via_plugin = True
                    new_chain.extend(comps)
        result.chain = new_chain

        if not is_llm_result:
            return

        # 核心历史保存发生在结果装饰之前，这里需要在渲染完成后修正最后一条 assistant。
        # 如果其他插件（如 context_undo）设置了 skip_history_save=True，则跳过，
        # 避免把插件自身的回复（如"已撤回..."）错误保存为 assistant 历史。
        if event.get_extra("skip_history_save"):
            logger.debug("[HTML渲染] skip_history_save=True，跳过历史记录保存")
            return
        if event.get_extra(self._HISTORY_ASSISTANT_DROP_EXTRA_KEY, False):
            logger.debug("[HTML渲染] assistant 历史已标记跳过，取消手动历史回写")
            return

        try:
            conv_mgr = self.context.conversation_manager
            unified_msg_origin = event.unified_msg_origin
            conv_id = await conv_mgr.get_curr_conversation_id(unified_msg_origin)

            if not conv_id:
                return

            conversation = await conv_mgr.get_conversation(unified_msg_origin, conv_id)
            if not conversation:
                return

            try:
                history = json.loads(conversation.history) if conversation.history else []
            except json.JSONDecodeError:
                history = []

            history_source_override = event.get_extra(
                self._HISTORY_ASSISTANT_CONTENT_OVERRIDE_EXTRA_KEY,
            )
            has_history_override = history_source_override is not None
            history_source = history_source_override if has_history_override else original_text
            source_text = str(history_source or "").strip()
            clean_text = self._clean_assistant_history_text(
                event,
                history_source,
                original_text,
            )

            needs_history_correction = (
                rendered_via_plugin
                or has_history_override
                or clean_text != source_text
            )
            if not needs_history_correction:
                return

            history_text = self._build_assistant_history_override(
                clean_text,
                rendered_via_plugin,
            )
            if history_text is None:
                history_text = clean_text
            if not history_text:
                if history and history[-1].get("role") == "assistant":
                    history.pop()
                    await conv_mgr.update_conversation(
                        unified_msg_origin=unified_msg_origin,
                        conversation_id=conv_id,
                        history=history,
                    )
                    logger.info("[HTML渲染] 已移除仅含内部标记的 assistant 历史记录")
                return

            assistant_content = [{"type": "text", "text": history_text}]
            if history and history[-1].get("role") == "assistant":
                history[-1]["content"] = assistant_content
                history[-1].pop("tool_calls", None)
                history[-1].pop("tool_call_id", None)
            else:
                history.append({"role": "assistant", "content": assistant_content})

            await conv_mgr.update_conversation(
                unified_msg_origin=unified_msg_origin,
                conversation_id=conv_id,
                history=history,
            )
            logger.info(
                f"[HTML渲染] 已修正 assistant 历史记录，当前历史条数: {len(history)}"
            )
        except Exception as e:
            logger.error(f"[HTML渲染] 保存历史记录失败: {e}")
            import traceback
            logger.error(traceback.format_exc())
