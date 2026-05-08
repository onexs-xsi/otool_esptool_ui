from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import yaml

DEFAULT_VERIFY_PROFILE_NAME = "默认启动日志校验"
_SUPPORTED_ACTIONS = {
    "reset",
    "wait",
    "wait_silence",
    "send_text",
    "expect",
    "capture",
    "set_result",
    "pass",
    "fail",
    "clear_buffer",
}
_SUPPORTED_RESET_METHODS = {"serial_toggle", "esptool_run", "none"}
_SUPPORTED_MATCH_MODES = {"all", "any", "none"}
_SUPPORTED_MATCH_TYPES = {"contains", "regex"}
_SUPPORTED_PARITIES = {"N", "E", "O", "M", "S"}
_TEMPLATE_PATTERN = re.compile(r"\{\{\s*([a-zA-Z_][\w.-]*)\s*\}\}")


@dataclass(slots=True)
class VerifyPattern:
    pattern: str
    description: str = ""
    name: str = ""
    group: int = 1


@dataclass(slots=True)
class VerifySerialConfig:
    baudrate: int = 115200
    bytesize: int = 8
    parity: str = "N"
    stopbits: float = 1.0
    timeout_ms: int = 80
    encoding: str = "utf-8"
    newline: str = "\r\n"
    max_buffer_chars: int = 20000


@dataclass(slots=True)
class VerifyStep:
    action: str
    label: str = ""
    duration_ms: int = 0
    text: str = ""
    append_newline: bool = False
    char_delay_ms: int = 0
    timeout_ms: int = 3000
    patterns: list[VerifyPattern] = field(default_factory=list)
    match_mode: str = "all"
    match_type: str = "contains"
    case_sensitive: bool = False
    method: str = "serial_toggle"
    hold_ms: int = 120
    release_wait_ms: int = 200
    retry_count: int = 0
    retry_delay_ms: int = 0


@dataclass(slots=True)
class VerifyProfile:
    name: str
    description: str = ""
    serial: VerifySerialConfig = field(default_factory=VerifySerialConfig)
    steps: list[VerifyStep] = field(default_factory=list)


@dataclass(slots=True)
class VerifyMatchResult:
    satisfied: bool
    matched_patterns: list[str]
    pending_patterns: list[str]


def _coerce_int(value: Any, field_name: str, minimum: int | None = None) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} 必须是整数") from exc
    if minimum is not None and result < minimum:
        raise ValueError(f"{field_name} 不能小于 {minimum}")
    return result


def _coerce_stopbits(value: Any) -> float:
    try:
        stopbits = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("serial.stopbits 必须是 1 / 1.5 / 2") from exc
    if stopbits not in {1.0, 1.5, 2.0}:
        raise ValueError("serial.stopbits 仅支持 1 / 1.5 / 2")
    return stopbits


def _load_yaml_dict(text: str, *, error_prefix: str) -> dict[str, Any]:
    try:
        data = yaml.safe_load(text) if text.strip() else {}
    except yaml.YAMLError as exc:
        raise ValueError(f"{error_prefix} YAML 解析失败：{exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{error_prefix} 顶层必须是 YAML 字典")
    return data


def _parse_pattern(raw: Any, *, step_desc: str, index: int) -> VerifyPattern:
    if isinstance(raw, str):
        pattern = raw.strip()
        description = ""
        name = ""
        group = 1
    elif isinstance(raw, dict):
        if "pattern" in raw:
            pattern = str(raw.get("pattern", "")).strip()
            description = str(raw.get("description", "")).strip()
            name = str(raw.get("name", "")).strip()
            group = _coerce_int(raw.get("group", 1), f"patterns[{index}].group", minimum=0)
        elif len(raw) == 1:
            only_key, only_value = next(iter(raw.items()))
            pattern = str(only_key).strip() if only_value in (None, "") else ""
            description = ""
            name = ""
            group = 1
        else:
            pattern = ""
            description = ""
            name = ""
            group = 1
    else:
        raise ValueError(f"{step_desc} 的第 {index} 个 patterns 项必须是字符串或字典")
    if not pattern:
        raise ValueError(f"{step_desc} 的第 {index} 个 patterns 项不能为空")
    return VerifyPattern(pattern=pattern, description=description, name=name, group=group)


def _parse_serial_config(raw: Any, *, profile_name: str) -> VerifySerialConfig:
    if raw is None:
        return VerifySerialConfig()
    if not isinstance(raw, dict):
        raise ValueError(f"配置“{profile_name}”的 serial 必须是字典")

    baudrate = _coerce_int(raw.get("baudrate", 115200), "serial.baudrate", minimum=1)
    bytesize = _coerce_int(raw.get("bytesize", 8), "serial.bytesize", minimum=5)
    if bytesize not in {5, 6, 7, 8}:
        raise ValueError("serial.bytesize 仅支持 5 / 6 / 7 / 8")

    parity = str(raw.get("parity", "N")).strip().upper() or "N"
    if parity not in _SUPPORTED_PARITIES:
        raise ValueError("serial.parity 仅支持 N / E / O / M / S")

    timeout_ms = _coerce_int(raw.get("timeout_ms", 80), "serial.timeout_ms", minimum=1)
    max_buffer_chars = _coerce_int(
        raw.get("max_buffer_chars", 20000),
        "serial.max_buffer_chars",
        minimum=256,
    )
    encoding = str(raw.get("encoding", "utf-8")).strip() or "utf-8"
    newline = str(raw.get("newline", "\r\n"))

    return VerifySerialConfig(
        baudrate=baudrate,
        bytesize=bytesize,
        parity=parity,
        stopbits=_coerce_stopbits(raw.get("stopbits", 1)),
        timeout_ms=timeout_ms,
        encoding=encoding,
        newline=newline,
        max_buffer_chars=max_buffer_chars,
    )


def _parse_step(raw: Any, *, profile_name: str, index: int) -> VerifyStep:
    if not isinstance(raw, dict):
        raise ValueError(f"配置“{profile_name}”的第 {index} 个步骤必须是字典")

    action = str(raw.get("action", "")).strip()
    if action not in _SUPPORTED_ACTIONS:
        supported = ", ".join(sorted(_SUPPORTED_ACTIONS))
        raise ValueError(
            f"配置“{profile_name}”的第 {index} 个步骤 action 无效：{action or '<空>'}；"
            f"支持：{supported}"
        )

    step = VerifyStep(
        action=action,
        label=str(raw.get("label", "")).strip(),
        duration_ms=_coerce_int(raw.get("duration_ms", 0), f"步骤 {index}.duration_ms", minimum=0),
        text=str(raw.get("text", "")),
        append_newline=bool(raw.get("append_newline", False)),
        char_delay_ms=_coerce_int(raw.get("char_delay_ms", 0), f"步骤 {index}.char_delay_ms", minimum=0),
        timeout_ms=_coerce_int(raw.get("timeout_ms", 3000), f"步骤 {index}.timeout_ms", minimum=1),
        match_mode=str(raw.get("match_mode", "all")).strip().lower() or "all",
        match_type=str(raw.get("match_type", "contains")).strip().lower() or "contains",
        case_sensitive=bool(raw.get("case_sensitive", False)),
        method=str(raw.get("method", "serial_toggle")).strip().lower() or "serial_toggle",
        hold_ms=_coerce_int(raw.get("hold_ms", 120), f"步骤 {index}.hold_ms", minimum=0),
        release_wait_ms=_coerce_int(raw.get("release_wait_ms", 200), f"步骤 {index}.release_wait_ms", minimum=0),
        retry_count=_coerce_int(raw.get("retry_count", 0), f"步骤 {index}.retry_count", minimum=0),
        retry_delay_ms=_coerce_int(raw.get("retry_delay_ms", 0), f"步骤 {index}.retry_delay_ms", minimum=0),
    )

    step_desc = f"配置“{profile_name}”的第 {index} 个 expect 步骤"
    if step.match_mode not in _SUPPORTED_MATCH_MODES:
        raise ValueError(f"步骤 {index}.match_mode 仅支持 all / any / none")
    if step.match_type not in _SUPPORTED_MATCH_TYPES:
        raise ValueError(f"步骤 {index}.match_type 仅支持 contains / regex")
    if step.method not in _SUPPORTED_RESET_METHODS:
        raise ValueError(f"步骤 {index}.method 仅支持 serial_toggle / esptool_run / none")

    if action == "wait" and step.duration_ms <= 0:
        raise ValueError(f"配置“{profile_name}”的第 {index} 个 wait 步骤必须提供 duration_ms > 0")
    if action == "wait_silence":
        if step.duration_ms <= 0:
            raise ValueError(f"配置“{profile_name}”的第 {index} 个 wait_silence 步骤必须提供 duration_ms > 0")
        if step.timeout_ms < step.duration_ms:
            raise ValueError(f"配置“{profile_name}”的第 {index} 个 wait_silence 步骤要求 timeout_ms >= duration_ms")
    if action == "send_text" and not step.text and not step.append_newline:
        raise ValueError(
            f"配置“{profile_name}”的第 {index} 个 send_text 步骤至少需要 text 或 append_newline=true"
        )
    if action in {"expect", "capture"}:
        raw_patterns = raw.get("patterns")
        if not isinstance(raw_patterns, list) or not raw_patterns:
            raise ValueError(f"{step_desc} 必须提供非空 patterns 列表")
        step.patterns = [
            _parse_pattern(item, step_desc=step_desc, index=pattern_index)
            for pattern_index, item in enumerate(raw_patterns, start=1)
        ]
    if action == "capture":
        if step.match_mode not in {"all", "any"}:
            raise ValueError(f"配置“{profile_name}”的第 {index} 个 capture 步骤仅支持 match_mode=all/any")
        for pattern in step.patterns:
            if not pattern.name:
                raise ValueError(
                    f"配置“{profile_name}”的第 {index} 个 capture 步骤中，每个 patterns 项都必须提供 name"
                )
    if action == "set_result" and not step.text.strip():
        raise ValueError(f"配置“{profile_name}”的第 {index} 个 set_result 步骤必须提供 text")

    return step


def _pattern_to_serializable(pattern: VerifyPattern) -> str | dict[str, Any]:
    if not pattern.description and not pattern.name and pattern.group == 1:
        return pattern.pattern
    result: dict[str, Any] = {"pattern": pattern.pattern}
    if pattern.name:
        result["name"] = pattern.name
    if pattern.group != 1:
        result["group"] = pattern.group
    if pattern.description:
        result["description"] = pattern.description
    return result


def parse_verify_profile(name: str, raw: Any) -> VerifyProfile:
    profile_name = str(name).strip() or DEFAULT_VERIFY_PROFILE_NAME
    if not isinstance(raw, dict):
        raise ValueError(f"配置“{profile_name}”必须是字典")

    profile = VerifyProfile(
        name=profile_name,
        description=str(raw.get("description", "")).strip(),
        serial=_parse_serial_config(raw.get("serial"), profile_name=profile_name),
        steps=[
            _parse_step(step_raw, profile_name=profile_name, index=index)
            for index, step_raw in enumerate(raw.get("steps") or [], start=1)
        ],
    )
    if not profile.steps:
        raise ValueError(f"配置“{profile_name}”至少需要一个步骤")
    return profile


def load_verify_profiles_from_text(text: str) -> dict[str, VerifyProfile]:
    data = _load_yaml_dict(text, error_prefix="检验配置")
    raw_profiles = data.get("verify_profiles", data)
    if not isinstance(raw_profiles, dict) or not raw_profiles:
        raise ValueError("未找到 verify_profiles 配置")
    return {
        str(name).strip() or DEFAULT_VERIFY_PROFILE_NAME: parse_verify_profile(str(name), raw)
        for name, raw in raw_profiles.items()
    }


def load_single_profile_from_text(name: str, text: str) -> VerifyProfile:
    profile_name = name.strip() or DEFAULT_VERIFY_PROFILE_NAME
    data = _load_yaml_dict(text, error_prefix="检验脚本")
    if not data:
        raise ValueError("检验脚本为空")

    raw_profiles = data.get("verify_profiles")
    if isinstance(raw_profiles, dict) and raw_profiles:
        if profile_name in raw_profiles:
            return parse_verify_profile(profile_name, raw_profiles[profile_name])
        if len(raw_profiles) == 1:
            _, raw_profile = next(iter(raw_profiles.items()))
            return parse_verify_profile(profile_name, raw_profile)
        raise ValueError("脚本中包含多个 verify_profiles，请先选择单个配置")

    if any(key in data for key in ("steps", "serial", "description")):
        return parse_verify_profile(profile_name, data)

    if len(data) == 1:
        maybe_name, maybe_profile = next(iter(data.items()))
        if isinstance(maybe_profile, dict) and any(
            key in maybe_profile for key in ("steps", "serial", "description")
        ):
            return parse_verify_profile(str(maybe_name), maybe_profile)

    raise ValueError("当前脚本既不是单个 profile，也不包含 verify_profiles")


def profile_to_dict(profile: VerifyProfile) -> dict[str, Any]:
    serial_dict: dict[str, Any] = {
        "baudrate": profile.serial.baudrate,
        "bytesize": profile.serial.bytesize,
        "parity": profile.serial.parity,
        "stopbits": int(profile.serial.stopbits)
        if float(profile.serial.stopbits).is_integer()
        else profile.serial.stopbits,
        "timeout_ms": profile.serial.timeout_ms,
        "encoding": profile.serial.encoding,
        "newline": profile.serial.newline,
        "max_buffer_chars": profile.serial.max_buffer_chars,
    }

    steps: list[dict[str, Any]] = []
    for step in profile.steps:
        item: dict[str, Any] = {"action": step.action}
        if step.label:
            item["label"] = step.label
        if step.retry_count > 0:
            item["retry_count"] = step.retry_count
        if step.retry_delay_ms > 0:
            item["retry_delay_ms"] = step.retry_delay_ms
        if step.action == "reset":
            item["method"] = step.method
            if step.hold_ms != 120:
                item["hold_ms"] = step.hold_ms
            if step.release_wait_ms != 200:
                item["release_wait_ms"] = step.release_wait_ms
        elif step.action == "wait":
            item["duration_ms"] = step.duration_ms
        elif step.action == "wait_silence":
            item["duration_ms"] = step.duration_ms
            item["timeout_ms"] = step.timeout_ms
        elif step.action == "send_text":
            item["text"] = step.text
            if step.append_newline:
                item["append_newline"] = True
            if step.char_delay_ms > 0:
                item["char_delay_ms"] = step.char_delay_ms
        elif step.action in {"expect", "capture"}:
            item["timeout_ms"] = step.timeout_ms
            item["match_mode"] = step.match_mode
            item["match_type"] = step.match_type
            if step.case_sensitive:
                item["case_sensitive"] = True
            item["patterns"] = [
                _pattern_to_serializable(pattern)
                for pattern in step.patterns
            ]
        elif step.action == "set_result":
            item["text"] = step.text
        elif step.action in {"pass", "fail"}:
            if step.text:
                item["text"] = step.text
        steps.append(item)

    result: dict[str, Any] = {
        "description": profile.description,
        "serial": serial_dict,
        "steps": steps,
    }
    return result


def profile_to_yaml_text(profile: VerifyProfile) -> str:
    return yaml.safe_dump(
        profile_to_dict(profile),
        allow_unicode=True,
        sort_keys=False,
    )


def profiles_to_config_dict(profiles: dict[str, VerifyProfile]) -> dict[str, Any]:
    return {
        "verify_profiles": {
            name: profile_to_dict(profile)
            for name, profile in profiles.items()
        }
    }


def describe_step(step: VerifyStep) -> str:
    if step.label:
        return step.label
    if step.action == "reset":
        return f"复位设备（{step.method}）"
    if step.action == "wait":
        return f"等待 {step.duration_ms} ms"
    if step.action == "wait_silence":
        return f"等待串口静默 {step.duration_ms} ms"
    if step.action == "send_text":
        preview = step.text.replace("\r", "\\r").replace("\n", "\\n")
        return f"发送串口输入：{preview or '<换行>'}"
    if step.action == "expect":
        return f"等待匹配 {len(step.patterns)} 个字段"
    if step.action == "capture":
        names = [pattern.name for pattern in step.patterns if pattern.name]
        preview = " / ".join(names[:3]) or "参数"
        return f"读取参数：{preview}"
    if step.action == "set_result":
        preview = step.text.replace("\r", " ").replace("\n", " ").strip()
        return f"设置结果：{preview[:24] or '<空模板>'}"
    if step.action == "pass":
        preview = step.text.replace("\r", " ").replace("\n", " ").strip()
        return f"主动通过：{preview[:24] or '结束并标记通过'}"
    if step.action == "fail":
        preview = step.text.replace("\r", " ").replace("\n", " ").strip()
        return f"主动失败：{preview[:24] or '结束并标记失败'}"
    return "清空缓冲区"


def render_template_text(template: str, values: dict[str, Any]) -> str:
    def _replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            raise KeyError(key)
        return str(values[key])

    return _TEMPLATE_PATTERN.sub(_replace, template)


def evaluate_match(text: str, step: VerifyStep) -> VerifyMatchResult:
    if step.action != "expect":
        raise ValueError("evaluate_match 仅支持 expect 步骤")

    matched_patterns: list[str] = []
    pending_patterns: list[str] = []
    haystack = text if step.case_sensitive else text.lower()
    flags = 0 if step.case_sensitive else re.IGNORECASE

    for pattern in step.patterns:
        token = pattern.pattern if step.case_sensitive else pattern.pattern.lower()
        if step.match_type == "regex":
            matched = re.search(pattern.pattern, text, flags) is not None
        else:
            matched = token in haystack
        if matched:
            matched_patterns.append(pattern.pattern)
        else:
            pending_patterns.append(pattern.pattern)

    if step.match_mode == "all":
        satisfied = not pending_patterns
    elif step.match_mode == "any":
        satisfied = bool(matched_patterns)
    else:
        satisfied = not matched_patterns

    return VerifyMatchResult(
        satisfied=satisfied,
        matched_patterns=matched_patterns,
        pending_patterns=pending_patterns,
    )


def build_default_profile(name: str = DEFAULT_VERIFY_PROFILE_NAME) -> VerifyProfile:
    return VerifyProfile(
        name=name,
        description=(
            "复位后等待启动日志，先检查启动关键字，再通过串口发送命令并校验响应。"
        ),
        serial=VerifySerialConfig(),
        steps=[
            VerifyStep(
                action="reset",
                label="复位设备并捕获启动日志",
                method="serial_toggle",
            ),
            VerifyStep(
                action="expect",
                label="检查启动日志",
                timeout_ms=5000,
                match_mode="all",
                match_type="contains",
                patterns=[
                    VerifyPattern("boot:"),
                    VerifyPattern("rst:"),
                ],
            ),
            VerifyStep(
                action="clear_buffer",
                label="清空已读日志",
            ),
            VerifyStep(
                action="wait",
                label="等待命令行准备",
                duration_ms=300,
            ),
            VerifyStep(
                action="send_text",
                label="发送 version 命令",
                text="version",
                append_newline=True,
            ),
            VerifyStep(
                action="wait_silence",
                label="等待版本输出结束",
                duration_ms=200,
                timeout_ms=1500,
            ),
            VerifyStep(
                action="capture",
                label="读取版本号",
                timeout_ms=3000,
                match_mode="any",
                match_type="regex",
                retry_count=1,
                retry_delay_ms=150,
                patterns=[
                    VerifyPattern(
                        pattern=r"version[:= ]+([^\r\n]+)",
                        name="version",
                        description="版本号",
                    ),
                    VerifyPattern(
                        pattern=r"app version[:= ]+([^\r\n]+)",
                        name="version",
                        description="APP 版本号",
                    ),
                ],
            ),
            VerifyStep(
                action="set_result",
                label="显示版本读取结果",
                text="版本={{version}}",
            ),
        ],
    )
