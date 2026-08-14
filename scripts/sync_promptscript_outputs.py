#!/usr/bin/env python3
"""Sync PromptScript's Claude build into the dual-host plugin layout."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path


GENERATED_MARKER = "# promptscript-generated:"
MARKER_TIMESTAMP_RE = re.compile(
    rb"(?m)^((?:<!-- PromptScript |# promptscript-generated: ))"
    rb"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z(?= \|)"
)


def is_generated(path: Path) -> bool:
    """Return whether a file carries PromptScript's ownership marker."""
    try:
        return GENERATED_MARKER in "\n".join(
            path.read_text(encoding="utf-8").splitlines()[:50]
        )
    except (OSError, UnicodeDecodeError):
        return False


def is_managed_resource(relative: Path) -> bool:
    """Return whether a skill resource is managed without a marker."""
    return relative.parts[-2:] == ("agents", "openai.yaml")


def files_under(path: Path) -> set[Path]:
    """Return file paths relative to a directory."""
    if not path.is_dir():
        return set()
    return {file.relative_to(path) for file in path.rglob("*") if file.is_file()}


def comparable_content(content: bytes) -> bytes:
    """Ignore compiler timestamps when comparing generated outputs."""
    return MARKER_TIMESTAMP_RE.sub(rb"\1<timestamp>", content)


def has_symlink_component(path: Path, stop: Path) -> bool:
    """Return whether a destination path crosses a symlink before its root."""
    current = path.absolute()
    stop = stop.absolute()
    while True:
        if current.is_symlink():
            return True
        if current == stop or current.parent == current:
            return False
        current = current.parent


def write_generated(path: Path, content: bytes) -> None:
    """Replace a generated file atomically without following its old inode."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        temporary_name = ""
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_name:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


def adapt_claude_agent_frontmatter(content: bytes) -> bytes:
    """Keep Claude agent tools in the plugin's established scalar format."""
    text = content.decode("utf-8")
    match = re.search(
        r"(?m)^tools:\n(?P<block>  \[\n(?:    \"[^\"]+\",?\n)+  \]\n)",
        text,
    )
    if match is None:
        return content
    tools = re.findall(r'(?m)^    "([^"]+)",?$', match.group("block"))
    replacement = f"tools: {', '.join(tools)}\n"
    return (text[: match.start()] + replacement + text[match.end() :]).encode("utf-8")


def with_source_body(generated: bytes, source_body: str) -> bytes:
    """Keep PromptScript metadata while preserving the source procedure text."""
    text = generated.decode("utf-8")
    match = re.match(r"^(---\n.*?\n---)\n", text, re.S)
    if match is None:
        raise ValueError("generated Markdown file is missing frontmatter")
    body = source_body.rstrip("\n") + "\n"
    return (match.group(1) + "\n" + body).encode("utf-8")


def canonical_agent_body(root: Path, name: str) -> str:
    """Read one agent body from the canonical PromptScript source."""
    source = (root / ".promptscript" / "agents.prs").read_text(encoding="utf-8")
    match = re.search(
        rf"^  {re.escape(name)}: \{{.*?^    content: \"\"\"\n"
        r"(?P<body>.*?)^    \"\"\"\n  \}",
        source,
        re.M | re.S,
    )
    if match is None:
        raise ValueError(f"missing canonical agent body: {name}")
    return "".join(
        line[6:] if line.startswith("      ") else line
        for line in match.group("body").splitlines(keepends=True)
    )


def adapted_source_content(
    root: Path, source_file: Path, relative: Path, label: str
) -> bytes:
    """Adapt compiler output to the installable Claude plugin contract."""
    content = source_file.read_bytes()
    if label == "skills" and relative.name == "SKILL.md":
        canonical = root / ".promptscript" / "skills" / relative
        if not canonical.is_file():
            raise ValueError(f"missing canonical skill source: {canonical}")
        return with_source_body(content, canonical.read_text(encoding="utf-8").split(
            "\n---\n", 1
        )[1])
    if label == "agents" and relative.suffix == ".md":
        content = adapt_claude_agent_frontmatter(content)
        return with_source_body(content, canonical_agent_body(root, relative.stem))
    return content


def sync_tree(
    root: Path, source: Path, destination: Path, check: bool, label: str
) -> list[str]:
    """Copy generated files while preserving unmarked user files."""
    errors: list[str] = []
    source_files = files_under(source)
    destination_files = files_under(destination)

    if not source.is_dir():
        return [f"{label}: missing compiler output {source}"]

    for relative in sorted(source_files):
        source_file = source / relative
        destination_file = destination / relative
        source_content = adapted_source_content(root, source_file, relative, label)
        if has_symlink_component(destination_file, destination):
            errors.append(f"{label}: symlink destination refused {destination_file}")
            continue
        if check:
            if not destination_file.is_file():
                errors.append(f"{label}: missing {destination_file}")
            elif comparable_content(destination_file.read_bytes()) != comparable_content(
                source_content
            ):
                errors.append(f"{label}: drift in {destination_file}")
            continue
        write_generated(destination_file, source_content)

    stale = destination_files - source_files
    for relative in sorted(stale):
        destination_file = destination / relative
        if has_symlink_component(destination_file, destination):
            errors.append(f"{label}: symlink destination refused {destination_file}")
            continue
        if not is_generated(destination_file) and not is_managed_resource(relative):
            continue
        if check:
            errors.append(f"{label}: stale generated file {destination_file}")
        else:
            destination_file.unlink()

    return errors


def plugin_mcp_content(build_mcp: Path) -> bytes:
    """Adapt portable MCP output to Claude's plugin-root contract."""
    try:
        config = json.loads(build_mcp.read_text(encoding="utf-8"))
        server = config["mcpServers"]["founder-os-state"]
        server["command"] = "python3"
        server["args"] = ["${CLAUDE_PLUGIN_ROOT}/mcp/founder_os_state.py"]
        server.pop("type", None)
    except (OSError, UnicodeDecodeError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid PromptScript MCP output: {build_mcp}") from exc
    return (json.dumps(config, indent=2) + "\n").encode("utf-8")


def plugin_hooks_content(build_settings: Path) -> bytes:
    """Adapt portable Claude hooks to the plugin-root runtime manifest."""
    try:
        config = json.loads(build_settings.read_text(encoding="utf-8"))
        hooks = config["hooks"]
        for event_hooks in hooks.values():
            for matcher_group in event_hooks:
                if matcher_group.get("matcher") == ".*":
                    matcher_group.pop("matcher")
                for hook in matcher_group["hooks"]:
                    hook.pop("timeout", None)
                    command = hook["command"]
                    command = re.sub(
                        r'^if \[ -z "\$\{CLAUDE_PROJECT_DIR:-\}" \]; then '
                        r'printf .+?; exit 1; fi; cd "\$\{CLAUDE_PROJECT_DIR\}" && ',
                        "",
                        command,
                    )
                    command = re.sub(
                        r"^python3 founder-os/hooks/([A-Za-z0-9_.-]+\.py) "
                        r"# promptscript-generated:[A-Za-z0-9_-]+$",
                        r'python3 "${CLAUDE_PLUGIN_ROOT}/hooks/\1"',
                        command,
                    )
                    hook["command"] = command
    except (OSError, UnicodeDecodeError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid PromptScript hook output: {build_settings}") from exc
    return (json.dumps(config, indent=2) + "\n").encode("utf-8")


def plugin_codex_hooks_content(build_hooks: Path) -> bytes:
    """Adapt Codex project hooks to the installable plugin root."""
    try:
        config = json.loads(build_hooks.read_text(encoding="utf-8"))
        for event_hooks in config["hooks"].values():
            for matcher_group in event_hooks:
                for hook in matcher_group["hooks"]:
                    for field in ("command", "commandWindows"):
                        command = hook.get(field)
                        if not isinstance(command, str):
                            continue
                        command = re.sub(
                            r'^(?P<prefix>PROMPTSCRIPT_PROJECT_ROOT=.*?cd '
                            r'"\$PROMPTSCRIPT_PROJECT_ROOT" && )python3 '
                            r'founder-os/hooks/(?P<file>[A-Za-z0-9_.-]+\.py) '
                            r'(?P<marker># promptscript-generated:[A-Za-z0-9_-]+)$',
                            r'\g<prefix>python3 "${CODEX_PLUGIN_ROOT}/hooks/'
                            r'\g<file>" \g<marker>',
                            command,
                        )
                        command = re.sub(
                            r"^(?P<prefix>.*?& 'python3' )'founder-os/hooks/"
                            r"(?P<file>[A-Za-z0-9_.-]+\.py)' "
                            r"(?P<marker># promptscript-generated:[A-Za-z0-9_-]+)$",
                            r"\g<prefix>(Join-Path $env:CODEX_PLUGIN_ROOT "
                            r"'hooks/\g<file>') \g<marker>",
                            command,
                        )
                        hook[field] = command
    except (OSError, UnicodeDecodeError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid PromptScript Codex hook output: {build_hooks}") from exc
    return (json.dumps(config, indent=2) + "\n").encode("utf-8")


def sync_plugin(root: Path, check: bool) -> list[str]:
    """Sync the generated Claude target into the installable plugin."""
    build = root / ".promptscript" / "build" / "claude"
    plugin = root / "founder-os"
    errors: list[str] = []

    main_source = build / "CLAUDE.md"
    main_destination = plugin / "CLAUDE.md"
    if not main_source.is_file():
        errors.append(f"plugin: missing compiler output {main_source}")
    elif has_symlink_component(main_destination, plugin):
        errors.append(f"plugin: symlink destination refused {main_destination}")
    elif check:
        if (
            not main_destination.is_file()
            or comparable_content(main_destination.read_bytes())
            != comparable_content(main_source.read_bytes())
        ):
            errors.append(f"plugin: drift in {main_destination}")
    else:
        write_generated(main_destination, main_source.read_bytes())

    errors.extend(
        sync_tree(root, build / "skills", plugin / "skills", check, "skills")
    )
    errors.extend(
        sync_tree(root, build / ".claude" / "agents", plugin / "agents", check, "agents")
    )

    build_mcp = build / ".mcp.json"
    destination_mcp = plugin / ".mcp.json"
    try:
        expected_mcp = plugin_mcp_content(build_mcp)
    except ValueError as exc:
        errors.append(str(exc))
    else:
        if has_symlink_component(destination_mcp, plugin):
            errors.append(f"plugin: symlink destination refused {destination_mcp}")
        elif check:
            if not destination_mcp.is_file() or destination_mcp.read_bytes() != expected_mcp:
                errors.append(f"plugin: drift in {destination_mcp}")
        else:
            write_generated(destination_mcp, expected_mcp)

    build_settings = build / ".claude" / "settings.json"
    destination_hooks = plugin / "hooks" / "hooks.json"
    try:
        expected_hooks = plugin_hooks_content(build_settings)
    except ValueError as exc:
        errors.append(str(exc))
    else:
        if has_symlink_component(destination_hooks, plugin):
            errors.append(f"plugin: symlink destination refused {destination_hooks}")
        elif check:
            if not destination_hooks.is_file() or destination_hooks.read_bytes() != expected_hooks:
                errors.append(f"plugin: drift in {destination_hooks}")
        else:
            write_generated(destination_hooks, expected_hooks)

    build_codex_hooks = root / ".promptscript" / "build" / "codex" / ".codex" / "hooks.json"
    destination_codex_hooks = plugin / "hooks" / "codex-hooks.json"
    try:
        expected_codex_hooks = plugin_codex_hooks_content(build_codex_hooks)
    except ValueError as exc:
        errors.append(str(exc))
    else:
        if has_symlink_component(destination_codex_hooks, plugin):
            errors.append(
                f"plugin: symlink destination refused {destination_codex_hooks}"
            )
        elif check:
            if (
                not destination_codex_hooks.is_file()
                or comparable_content(destination_codex_hooks.read_bytes())
                != comparable_content(expected_codex_hooks)
            ):
                errors.append(f"plugin: drift in {destination_codex_hooks}")
        else:
            write_generated(destination_codex_hooks, expected_codex_hooks)

    return errors


def main() -> int:
    """Run the sync or drift check."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="fail when generated outputs drift")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    errors = sync_plugin(root, args.check)
    if errors:
        for error in errors:
            print(f"FAIL: {error}")
        return 1
    print("PromptScript plugin outputs are current")
    return 0


if __name__ == "__main__":
    sys.exit(main())
