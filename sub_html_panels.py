from __future__ import annotations

import html
import base64
import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional


_PANEL_TAG_PATTERN = re.compile(
    r"<panel\b(?P<attrs>[^>]*)>(?P<body>[\s\S]*?)</panel\s*>",
    re.IGNORECASE,
)
_PANEL_ID_PATTERN = re.compile(
    r"\bid\s*=\s*(?:([\"'])(?P<quoted>.*?)\1|(?P<bare>[a-zA-Z][\w-]*))",
    re.IGNORECASE | re.DOTALL,
)
_PLACEHOLDER_PATTERN = re.compile(
    r"{{\s*(?P<kind>value|percent|token|color|image):(?P<key>[a-zA-Z][\w-]*)\s*}}",
    re.IGNORECASE,
)
_VALID_ID_PATTERN = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{0,63}$")
_VALID_FIELD_TYPES = {"text", "number", "percent", "boolean", "choice", "color", "image"}
_HEX_COLOR_PATTERN = re.compile(r"^#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?$")
_VALID_OUTPUT_MODES = {"always", "when_relevant", "on_change"}
_VALID_PANEL_MODES = {"stateful", "inline"}
_UPDATE_ATTRIBUTE_PATTERN = re.compile(r"(?:^|\s)update(?:\s|=|$)", re.IGNORECASE)


@dataclass(frozen=True)
class PanelField:
    key: str
    label: str
    value_type: str
    description: str
    options: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class PanelDefinition:
    panel_id: str
    name: str
    description: str
    panel_mode: str
    output_mode: str
    empty_value: str
    fields: tuple[PanelField, ...]
    html_template: str


class SubHtmlPanelManager:
    """Parse compact panel payloads and expand them into configured HTML."""

    def __init__(self, plugin_dir: str, logger=None):
        self.plugin_dir = Path(plugin_dir).resolve()
        self.logger = logger
        self._panels: dict[str, PanelDefinition] = {}
        self._raw_config_signature = ""
        self._image_asset_cache: dict[str, tuple[tuple[int, int], str]] = {}

    def reload(self, raw_items: Any) -> None:
        panels: dict[str, PanelDefinition] = {}
        if not isinstance(raw_items, list):
            self._warn("子 HTML 面板配置不是列表，已忽略")
            self._panels = panels
            raw_encoded = json.dumps(
                raw_items,
                ensure_ascii=False,
                sort_keys=True,
                default=str,
                separators=(",", ":"),
            )
            self._raw_config_signature = hashlib.sha256(
                raw_encoded.encode("utf-8")
            ).hexdigest()[:20]
            return

        for index, item in enumerate(raw_items, start=1):
            if not isinstance(item, dict) or not self._as_bool(item.get("enabled", True)):
                continue
            try:
                panel = self._parse_panel(item)
            except ValueError as exc:
                self._warn(f"第 {index} 个子 HTML 面板配置无效: {exc}")
                continue
            if panel.panel_id in panels:
                self._warn(f"子 HTML 面板 ID 重复，已忽略后者: {panel.panel_id}")
                continue
            panels[panel.panel_id] = panel

        self._panels = panels
        raw_encoded = json.dumps(
            raw_items,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
            separators=(",", ":"),
        )
        self._raw_config_signature = hashlib.sha256(
            raw_encoded.encode("utf-8")
        ).hexdigest()[:20]

    def get_enabled_panels(self) -> list[PanelDefinition]:
        return list(self._panels.values())

    def get_missing_always_panels(self, text: Optional[str]) -> list[PanelDefinition]:
        valid_panel_ids = set(self.extract_valid_panel_tags(text))
        return [
            panel
            for panel in self._panels.values()
            if panel.output_mode == "always" and panel.panel_id not in valid_panel_ids
        ]

    def extract_valid_panel_tags(
        self,
        text: Optional[str],
        allowed_panel_ids: Optional[Iterable[str]] = None,
    ) -> dict[str, str]:
        """Return the last valid tag for each enabled panel in source order."""
        source = str(text or "")
        allowed = set(allowed_panel_ids) if allowed_panel_ids is not None else None
        latest: dict[str, str] = {}
        for match in _PANEL_TAG_PATTERN.finditer(source):
            panel_id = self._extract_panel_id(match.group("attrs"))
            if panel_id not in self._panels or (allowed is not None and panel_id not in allowed):
                continue
            if self._decode_payload(match.group("body")) is None:
                continue
            latest[panel_id] = match.group(0).strip()
        return latest

    @property
    def configuration_signature(self) -> str:
        payload = []
        for panel in self._panels.values():
            payload.append(
                {
                    "id": panel.panel_id,
                    "name": panel.name,
                    "description": panel.description,
                    "panel_mode": panel.panel_mode,
                    "output_mode": panel.output_mode,
                    "empty_value": panel.empty_value,
                    "fields": [
                        {
                            "key": field.key,
                            "label": field.label,
                            "type": field.value_type,
                            "description": field.description,
                            "options": list(field.options),
                        }
                        for field in panel.fields
                    ],
                    "html_template": panel.html_template,
                }
            )
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(
            f"{self._raw_config_signature}:{encoded}".encode("utf-8")
        ).hexdigest()[:20]

    @property
    def state_signature(self) -> str:
        """Identify only changes that can make persisted panel values incompatible."""
        payload = []
        for panel in sorted(self._panels.values(), key=lambda item: item.panel_id):
            if panel.panel_mode == "inline":
                continue
            payload.append(
                {
                    "id": panel.panel_id,
                    "fields": [
                        {
                            "key": field.key,
                            "type": field.value_type,
                            "options": sorted(field.options),
                        }
                        for field in sorted(panel.fields, key=lambda item: item.key)
                    ],
                }
            )
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:20]

    @property
    def state_panel_signatures(self) -> dict[str, str]:
        """Return independent compatibility signatures for stateful panels."""
        result: dict[str, str] = {}
        for panel in self._panels.values():
            if panel.panel_mode == "inline":
                continue
            payload = {
                "id": panel.panel_id,
                "fields": [
                    {
                        "key": field.key,
                        "type": field.value_type,
                        "options": sorted(field.options),
                    }
                    for field in sorted(panel.fields, key=lambda item: item.key)
                ],
            }
            encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            result[panel.panel_id] = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:20]
        return result

    def contains_enabled_panel_tag(self, text: Optional[str]) -> bool:
        source = str(text or "")
        for match in _PANEL_TAG_PATTERN.finditer(source):
            panel_id = self._extract_panel_id(match.group("attrs"))
            if panel_id in self._panels:
                return True
        return False

    @staticmethod
    def strip_panel_markup(text: Optional[str]) -> str:
        """Remove panel payloads before assistant history is written back to context."""
        source = str(text or "")
        if not source:
            return source
        stripped = _PANEL_TAG_PATTERN.sub("", source)
        # Also remove malformed/self-closing panel tags so an invalid model
        # response cannot leak the internal protocol into future context.
        stripped = re.sub(r"<panel\b[^>]*/\s*>", "", stripped, flags=re.IGNORECASE)
        stripped = re.sub(
            r"<panel\b[^>]*>[\s\S]*$",
            "",
            stripped,
            flags=re.IGNORECASE,
        )
        stripped = re.sub(r"</panel\s*>", "", stripped, flags=re.IGNORECASE)
        stripped = re.sub(r"[ \t]*\n[ \t]*", "\n", stripped)
        stripped = re.sub(r"\n{3,}", "\n\n", stripped)
        return stripped.strip()

    def expand_panels(
        self,
        text: Optional[str],
        state_store: Optional[dict[str, dict[str, Any]]] = None,
    ) -> str:
        source = str(text or "")
        if not source or "<panel" not in source.lower():
            return source

        matches = list(_PANEL_TAG_PATTERN.finditer(source))
        if not matches:
            return source

        working_states: dict[str, dict[str, Any]] = {}
        for panel_id, payload in (state_store or {}).items():
            if isinstance(panel_id, str) and isinstance(payload, dict):
                working_states[panel_id] = dict(payload)

        valid_matches: dict[int, tuple[str, PanelDefinition, dict[str, Any]]] = {}
        latest_match_by_panel: dict[str, int] = {}
        for index, match in enumerate(matches):
            panel_id = self._extract_panel_id(match.group("attrs"))
            panel = self._panels.get(panel_id)
            if panel is None:
                self._warn(f"收到未知或未启用的子 HTML 面板，已忽略: {panel_id or '缺少 id'}")
                continue

            payload = self._parse_payload(match.group("body"), panel_id)
            if payload is None:
                continue
            known_keys = {field.key for field in panel.fields}
            payload = {key: value for key, value in payload.items() if key in known_keys}

            if panel.panel_mode == "inline":
                merged_payload = dict(payload)
            elif self._is_update_tag(match.group("attrs")):
                merged_payload = dict(working_states.get(panel_id, {}))
                merged_payload.update(payload)
            else:
                merged_payload = dict(payload)

            if panel.panel_mode == "stateful":
                working_states[panel_id] = merged_payload
            valid_matches[index] = (panel_id, panel, merged_payload)
            if panel.panel_mode == "stateful":
                latest_match_by_panel[panel_id] = index

        if state_store is not None:
            for panel_id in latest_match_by_panel:
                state_store[panel_id] = dict(working_states[panel_id])

        output: list[str] = []
        cursor = 0
        for index, match in enumerate(matches):
            output.append(source[cursor:match.start()])
            record = valid_matches.get(index)
            if record is not None:
                panel_id, panel, merged_payload = record
                if panel.panel_mode == "stateful" and latest_match_by_panel.get(panel_id) != index:
                    cursor = match.end()
                    continue
                output.append(self._render_panel(panel, merged_payload))
            cursor = match.end()
        output.append(source[cursor:])

        expanded = "".join(output)
        expanded = re.sub(r"[ \t]*\n[ \t]*", "\n", expanded)
        expanded = re.sub(r"\n{3,}", "\n\n", expanded)
        return expanded.strip()

    def build_prompt_block(
        self,
        panel_ids: Optional[Iterable[str]] = None,
        theme_palette: Optional[dict[str, Any]] = None,
    ) -> str:
        if not self._panels:
            return ""

        selected_ids = set(panel_ids) if panel_ids is not None else None
        panels = [
            panel
            for panel in self._panels.values()
            if selected_ids is None or panel.panel_id in selected_ids
        ]
        if not panels:
            return ""

        lines = [
            "## 子 HTML 面板数据协议",
            "以下状态面板和行内子模板已启用。使用时只输出紧凑 JSON 数据，禁止重新编写 HTML 或 CSS。",
            "输出时机为“每次回复”的面板必须在每轮回复中输出；其他面板按各自的输出时机决定。",
            "固定格式：<panel id=\"面板ID\">{\"字段\":\"值\"}</panel>",
            "JSON 必须是单个合法对象，不要加 Markdown 代码块、注释或未声明字段。",
            "如果本轮使用 <render>，请把 <panel> 放在 </render> 之前；否则直接放在回复末尾。",
            "状态面板每轮每个 ID 最多输出一次；行内子模板可以混用，也可以重复使用同一个 ID。",
            "行内子模板会在标签出现的位置原地展开：请把标签写在它应该显示的正文相对位置，不要统一堆到回复末尾。",
        ]

        if any(field.value_type == "image" for panel in panels for field in panel.fields):
            image_files = self.get_available_image_files()
            lines.append(
                "可用图片文件（来自 avatars/）："
                + ("、".join(image_files) if image_files else "暂无，请不要编造文件名")
            )

        palette_primary = ""
        has_color_field = any(
            field.value_type == "color"
            for panel in panels
            for field in panel.fields
        )
        if has_color_field and isinstance(theme_palette, dict):
            palette_primary = str(theme_palette.get("primary") or "").strip().lower()
            if not _HEX_COLOR_PATTERN.fullmatch(palette_primary):
                palette_primary = ""
            palette_colors = []
            for raw_color in theme_palette.get("colors") or []:
                color = str(raw_color or "").strip().lower()
                if _HEX_COLOR_PATTERN.fullmatch(color) and color not in palette_colors:
                    palette_colors.append(color)
            if palette_primary and palette_primary not in palette_colors:
                palette_colors.insert(0, palette_primary)

            if palette_primary:
                template_name = str(theme_palette.get("template") or "当前主模板").strip()
                tone = str(theme_palette.get("tone") or "协调色调").strip()
                lines.extend(
                    [
                        "",
                        "### 当前主模板配色参考",
                        f"当前主模板：{template_name}",
                        f"最匹配的色调：{tone}；推荐面板主题色：{palette_primary}",
                        f"协调色板：{'、'.join(palette_colors) or palette_primary}",
                        (
                            "当面板包含 color 类型字段（例如 theme_color），且当前情境或面板模式允许自定义配色时，"
                            "优先使用推荐主题色，或从协调色板中选择与主模板配色一致、最接近的色调；"
                            "只有状态语义确实需要明显区分时才偏离该色板。"
                        ),
                    ]
                )

        mode_guidance = {
            "always": "每次回复都必须输出。首次使用完整 JSON；之后输出 `<panel id=\"面板ID\" update>{\"变化字段\":\"新值\"}</panel>`，没有字段变化时也必须输出 `<panel id=\"面板ID\" update>{}</panel>`，未出现的字段会沿用最近状态。",
            "when_relevant": "仅在当前回复涉及该面板主题时输出。",
            "on_change": "仅当任一字段相较上下文中的最近状态发生变化时输出。",
        }
        for panel in panels:
            mode_label = "行内子模板（可重复、无状态、原地插入）" if panel.panel_mode == "inline" else "状态面板（同 ID 去重并保留最近状态）"
            output_guidance = mode_guidance[panel.output_mode]
            if panel.panel_mode == "inline" and panel.output_mode == "always":
                output_guidance = "每次回复至少输出一次；需要多处展示时可以重复输出。每个标签都填写完整 JSON，不使用 update。"
            lines.extend(
                [
                    "",
                    f"### {panel.name}（id: {panel.panel_id}）",
                    f"类型：{mode_label}",
                    panel.description or "按字段定义填写当前状态。",
                    f"输出时机：{output_guidance}",
                    "字段：",
                ]
            )
            example: dict[str, Any] = {}
            for field in panel.fields:
                options_hint = ""
                if field.options:
                    choices = "、".join(f"{key}={label}" for key, label in field.options)
                    options_hint = f" | 可选值: {choices}"
                lines.append(
                    f'- `{field.key}` | {field.label} | {field.value_type} | '
                    f'{field.description or "按当前情境填写"}{options_hint}'
                )
                if field.value_type == "color" and palette_primary:
                    example[field.key] = palette_primary
                else:
                    example[field.key] = self._example_value(field)
            example_json = json.dumps(example, ensure_ascii=False, separators=(",", ":"))
            lines.append(f'示例：<panel id="{panel.panel_id}">{example_json}</panel>')
            if panel.panel_mode == "inline":
                lines.append("可在正文需要显示的位置重复输出本标签；每次标签的数据彼此独立。")
            if panel.output_mode == "always" and panel.panel_mode == "stateful":
                lines.append(
                    f'增量示例：<panel id="{panel.panel_id}" update>{{"{panel.fields[0].key}":"仅更新这一项"}}</panel>'
                )

        return "\n".join(lines).strip()

    def _parse_panel(self, item: dict[str, Any]) -> PanelDefinition:
        panel_id = str(item.get("panel_id") or "").strip()
        if not _VALID_ID_PATTERN.fullmatch(panel_id):
            raise ValueError("panel_id 必须以字母开头，且只能包含字母、数字、下划线或短横线")

        fields = tuple(self._parse_fields(item.get("fields", [])))
        if not fields:
            raise ValueError(f"面板 {panel_id} 没有有效字段")

        html_override = str(item.get("html_template") or "").strip()
        html_template = html_override or self._load_template_file(item.get("template_file"), panel_id)
        if not html_template:
            raise ValueError(f"面板 {panel_id} 没有可用的 HTML 模板")

        panel_mode = str(item.get("panel_mode") or "stateful").strip().lower()
        if panel_mode not in _VALID_PANEL_MODES:
            panel_mode = "stateful"

        output_mode = str(item.get("output_mode") or "always").strip().lower()
        if output_mode not in _VALID_OUTPUT_MODES:
            output_mode = "always"

        raw_empty_value = item.get("empty_value")
        if raw_empty_value is None:
            empty_value = "" if panel_mode == "inline" else "未记录"
        else:
            empty_value = str(raw_empty_value).strip()
            if panel_mode == "stateful" and not empty_value:
                empty_value = "未记录"

        return PanelDefinition(
            panel_id=panel_id,
            name=str(item.get("name") or panel_id).strip() or panel_id,
            description=str(item.get("description") or "").strip(),
            panel_mode=panel_mode,
            output_mode=output_mode,
            empty_value=empty_value,
            fields=fields,
            html_template=html_template,
        )

    def _parse_fields(self, raw_fields: Any) -> Iterable[PanelField]:
        if isinstance(raw_fields, str):
            candidates = raw_fields.splitlines()
        elif isinstance(raw_fields, list):
            candidates = raw_fields
        else:
            candidates = []

        seen: set[str] = set()
        for raw_field in candidates:
            parts = [part.strip() for part in str(raw_field or "").split("|", 4)]
            if not parts or not _VALID_ID_PATTERN.fullmatch(parts[0]):
                continue
            key = parts[0]
            if key in seen:
                continue
            seen.add(key)
            label = parts[1] if len(parts) > 1 and parts[1] else key
            value_type = parts[2].lower() if len(parts) > 2 else "text"
            if value_type not in _VALID_FIELD_TYPES:
                value_type = "text"
            description = parts[3] if len(parts) > 3 else ""
            options: list[tuple[str, str]] = []
            if value_type == "choice" and len(parts) > 4:
                for raw_option in parts[4].split(","):
                    option_parts = [part.strip() for part in raw_option.split("=", 1)]
                    option_key = option_parts[0]
                    if not _VALID_ID_PATTERN.fullmatch(option_key):
                        continue
                    option_label = option_parts[1] if len(option_parts) > 1 else option_key
                    options.append((option_key, option_label or option_key))
            if value_type == "choice" and not options:
                value_type = "text"
            yield PanelField(key, label, value_type, description, tuple(options))

    def _load_template_file(self, raw_path: Any, panel_id: str) -> str:
        relative_path = str(raw_path or "").strip().replace("\\", "/")
        if not relative_path:
            return ""

        candidate = (self.plugin_dir / relative_path).resolve()
        try:
            candidate.relative_to(self.plugin_dir)
        except ValueError as exc:
            raise ValueError(f"面板 {panel_id} 的模板文件必须位于插件目录内") from exc
        if candidate.suffix.lower() != ".html" or not candidate.is_file():
            raise ValueError(f"面板 {panel_id} 的模板文件不存在: {relative_path}")
        try:
            return candidate.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ValueError(f"读取面板 {panel_id} 的模板文件失败: {exc}") from exc

    def _parse_payload(self, raw_body: str, panel_id: str) -> Optional[dict[str, Any]]:
        payload = self._decode_payload(raw_body)
        if payload is not None:
            return payload

        body = str(raw_body or "").strip()
        fenced = re.fullmatch(r"```(?:json)?\s*([\s\S]*?)\s*```", body, re.IGNORECASE)
        if fenced:
            body = fenced.group(1).strip()
        try:
            decoded = json.loads(body)
        except json.JSONDecodeError as exc:
            self._warn(f"子 HTML 面板 {panel_id} 的 JSON 无效，已忽略: {exc.msg}")
            return None
        if not isinstance(decoded, dict):
            self._warn(f"子 HTML 面板 {panel_id} 的数据必须是 JSON 对象，已忽略")
            return None
        return decoded

    @staticmethod
    def _decode_payload(raw_body: str) -> Optional[dict[str, Any]]:
        body = str(raw_body or "").strip()
        fenced = re.fullmatch(r"```(?:json)?\s*([\s\S]*?)\s*```", body, re.IGNORECASE)
        if fenced:
            body = fenced.group(1).strip()
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, TypeError):
            return None
        return payload if isinstance(payload, dict) else None

    def _render_panel(self, panel: PanelDefinition, payload: dict[str, Any]) -> str:
        display_values: dict[str, str] = {}
        percent_values: dict[str, str] = {}
        token_values: dict[str, str] = {}
        color_values: dict[str, str] = {}
        image_values: dict[str, str] = {}
        for field in panel.fields:
            display, percent, token = self._normalize_value(
                payload.get(field.key),
                field,
                panel.empty_value,
            )
            display_values[field.key] = html.escape(display, quote=True)
            percent_values[field.key] = percent
            token_values[field.key] = html.escape(token, quote=True)
            color_values[field.key] = html.escape(
                display if field.value_type == "color" else "",
                quote=True,
            )
            image_values[field.key] = html.escape(
                self._resolve_image_data_url(payload.get(field.key))
                if field.value_type == "image"
                else "",
                quote=True,
            )

        def replace_placeholder(match: re.Match[str]) -> str:
            key = match.group("key")
            kind = match.group("kind").lower()
            if kind == "percent":
                return percent_values.get(key, "0")
            if kind == "token":
                return token_values.get(key, "")
            if kind == "color":
                return color_values.get(key, "")
            if kind == "image":
                return image_values.get(key, "")
            return display_values.get(key, "")

        rendered = _PLACEHOLDER_PATTERN.sub(replace_placeholder, panel.html_template)
        meta = {
            "panel_id": panel.panel_id,
            "panel_name": panel.name,
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
        for key, value in meta.items():
            rendered = rendered.replace("{{" + key + "}}", html.escape(value, quote=True))
        return rendered.strip()

    def _resolve_image_data_url(self, raw_value: Any) -> str:
        requested = str(raw_value or "").strip().replace("\\", "/")
        if not requested:
            return ""

        asset_root = (self.plugin_dir / "avatars").resolve()
        candidate = (asset_root / requested).resolve()
        try:
            candidate.relative_to(asset_root)
        except ValueError:
            self._warn(f"子 HTML 模板图片路径越界，已忽略: {requested}")
            return ""

        mime = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".webp": "image/webp",
            ".gif": "image/gif",
        }.get(candidate.suffix.lower())
        if not mime or not candidate.is_file():
            self._warn(f"子 HTML 模板图片不存在或格式不受支持: {requested}")
            return ""

        stat = candidate.stat()
        signature = (stat.st_mtime_ns, stat.st_size)
        cache_key = candidate.relative_to(asset_root).as_posix()
        cached = self._image_asset_cache.get(cache_key)
        if cached and cached[0] == signature:
            return cached[1]

        try:
            encoded = base64.b64encode(candidate.read_bytes()).decode("ascii")
        except OSError as exc:
            self._warn(f"读取子 HTML 模板图片失败 {requested}: {exc}")
            return ""
        data_url = f"data:{mime};base64,{encoded}"
        self._image_asset_cache[cache_key] = (signature, data_url)
        return data_url

    def get_available_image_files(self) -> list[str]:
        asset_root = (self.plugin_dir / "avatars").resolve()
        if not asset_root.is_dir():
            return []
        supported = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
        return sorted(
            (
                path.relative_to(asset_root).as_posix()
                for path in asset_root.rglob("*")
                if path.is_file() and path.suffix.lower() in supported
            ),
            key=str.casefold,
        )

    @staticmethod
    def _normalize_value(value: Any, field: PanelField, empty_value: str) -> tuple[str, str, str]:
        if value is None or value == "" or (
            isinstance(value, str) and not value.strip()
        ):
            return empty_value, "0", ""

        if field.value_type in {"number", "percent"}:
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                return empty_value, "0", ""
            if not math.isfinite(numeric):
                return empty_value, "0", ""
            display = f"{numeric:g}"
            percent = max(0.0, min(100.0, numeric))
            return display, f"{percent:g}", display

        if field.value_type == "boolean":
            if isinstance(value, str):
                truthy = value.strip().lower() in {"1", "true", "yes", "on", "是", "有"}
            else:
                truthy = bool(value)
            return ("是" if truthy else "否"), ("100" if truthy else "0"), ("true" if truthy else "false")

        if field.value_type == "choice":
            choice_key = str(value).strip()
            option_map = dict(field.options)
            if choice_key not in option_map:
                return empty_value, "0", ""
            return option_map[choice_key], "0", choice_key

        if field.value_type == "color":
            color = str(value).strip()
            if not _HEX_COLOR_PATTERN.fullmatch(color):
                return "#a92e3b", "0", "a92e3b"
            normalized = color.lower()
            return normalized, "0", normalized[1:]

        if isinstance(value, (dict, list)):
            display = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        else:
            display = str(value)
        token = re.sub(r"[^a-zA-Z0-9_-]+", "-", display).strip("-")[:64]
        return display, "0", token

    @staticmethod
    def _extract_panel_id(attrs: str) -> str:
        match = _PANEL_ID_PATTERN.search(str(attrs or ""))
        if not match:
            return ""
        return str(match.group("quoted") or match.group("bare") or "").strip()

    @staticmethod
    def _is_update_tag(attrs: str) -> bool:
        return bool(_UPDATE_ATTRIBUTE_PATTERN.search(str(attrs or "")))

    @staticmethod
    def _example_value(field: PanelField) -> Any:
        if field.value_type == "choice" and field.options:
            return field.options[0][0]
        if field.value_type == "percent":
            return 50
        if field.value_type == "number":
            return 1
        if field.value_type == "boolean":
            return True
        if field.value_type == "color":
            return "#b13a55"
        if field.value_type == "image":
            return "角色头像.png"
        return f"当前{field.label}"

    @staticmethod
    def _as_bool(value: Any) -> bool:
        if isinstance(value, str):
            return value.strip().lower() not in {"", "0", "false", "off", "no"}
        return bool(value)

    def _warn(self, message: str) -> None:
        if self.logger is not None and hasattr(self.logger, "warning"):
            self.logger.warning(f"[HTML渲染] {message}")
