"""Common skill integrity rules and explicit host publication profiles."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import quote, unquote, urlsplit

import yaml

from .budgets import DEFAULT_JSON_BUDGET
from .models import BrokerError, canonical_digest


@dataclass(frozen=True)
class SkillProfile:
    name: str = "mcp"
    max_skills: int = 64
    max_files: int = 512
    max_file_bytes: int = 16 * 1024 * 1024
    max_skill_bytes: int = 16 * 1024 * 1024
    max_instruction_bytes: int = 256 * 1024
    max_pages: int = 64
    unique_names: bool = False
    max_archive_bytes: int | None = None


MCP_PROFILE = SkillProfile()
OPENAI_IMPORT_PROFILE = SkillProfile(
    name="openai-import",
    max_skills=5,
    max_files=100,
    max_file_bytes=1024 * 1024,
    max_skill_bytes=5 * 1024 * 1024,
    max_pages=10,
    unique_names=True,
    max_archive_bytes=8 * 1024 * 1024,
)


class UniqueLoader(yaml.SafeLoader):
    pass


def _mapping(loader, node):
    pairs = loader.construct_pairs(node, deep=True)
    result = {}
    for key, value in pairs:
        if not isinstance(key, str) or key in result:
            raise BrokerError("skill YAML keys must be unique strings")
        result[key] = value
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def frontmatter(raw: bytes, *, limit=256 * 1024):
    try:
        if len(raw) > limit:
            raise ValueError()
        text = raw.decode("utf-8").replace("\r\n", "\n")
        lines = text.splitlines()
        if not lines or lines[0] != "---":
            raise ValueError()
        end = lines.index("---", 1)
        header = "\n".join(lines[1:end])
        if any(
            isinstance(token, (yaml.tokens.AliasToken, yaml.tokens.AnchorToken))
            for token in yaml.scan(header)
        ):
            raise ValueError()
        # Bound nesting before invoking the recursive YAML constructor.
        depth = 0
        nodes = 0
        for event in yaml.parse(header):
            nodes += 1
            if isinstance(event, (yaml.events.SequenceStartEvent, yaml.events.MappingStartEvent)):
                depth += 1
            elif isinstance(event, (yaml.events.SequenceEndEvent, yaml.events.MappingEndEvent)):
                depth -= 1
            if depth > 32 or nodes > 10_000:
                raise ValueError()
        value = yaml.load(header, Loader=UniqueLoader)
        validate_header(value)
        return value
    except (ValueError, KeyError, TypeError, UnicodeError, yaml.YAMLError, RecursionError):
        raise BrokerError("invalid skill frontmatter") from None


def validate_header(value):
    DEFAULT_JSON_BUDGET.check(value)
    if not isinstance(value, dict):
        raise BrokerError("invalid skill frontmatter")
    name = value.get("name")
    description = value.get("description")
    if (
        not isinstance(name, str)
        or len(name) > 64
        or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name)
        or not isinstance(description, str)
        or not description.strip()
        or len(description) > 1024
    ):
        raise BrokerError("invalid skill name or description")
    canonical_digest(value)


def safe_uri(uri):
    if not isinstance(uri, str) or not 1 <= len(uri) <= 4096:
        raise BrokerError("invalid skill resource URI")
    parts = urlsplit(uri)
    if not parts.scheme or not parts.netloc or parts.query or parts.fragment:
        raise BrokerError("skill resource URI must be absolute without query or fragment")
    decoded = unquote(parts.netloc + parts.path, errors="strict")
    segments = decoded.split("/")
    if (
        any(not s or s in {".", ".."} or s.endswith((" ", ".")) for s in segments)
        or any(c in decoded for c in "\\\x00:%?#")
        or any(ord(c) < 32 for c in decoded)
        or unicodedata.normalize("NFC", decoded) != decoded
        or quote(decoded, safe="/-._~") != parts.netloc + parts.path
    ):
        raise BrokerError("unsafe or noncanonical skill resource path")
    return uri


def validate_entry(entry, profile=MCP_PROFILE):
    DEFAULT_JSON_BUDGET.check(entry)
    if len(json.dumps(entry, ensure_ascii=False).encode("utf-8")) > 4 * 1024 * 1024:
        raise BrokerError("skill descriptor exceeds 4 MiB")
    if not isinstance(entry, dict):
        raise BrokerError("invalid skill entry")
    uri = safe_uri(entry.get("uri"))
    validate_header(entry.get("frontmatter"))
    name = entry["frontmatter"]["name"]
    if not uri.endswith(f"/{name}/SKILL.md") and uri != f"skill://{name}/SKILL.md":
        # Alternate schemes with the name in the authority are equally valid.
        if urlsplit(uri).netloc != name or urlsplit(uri).path != "/SKILL.md":
            raise BrokerError("skill URI must end in its frontmatter name and SKILL.md")
    resources = entry.get("resources")
    if not isinstance(resources, list) or not 1 <= len(resources) <= profile.max_files:
        raise BrokerError("skill needs a bounded complete resource manifest")
    root = uri.removesuffix("SKILL.md")
    seen = set()
    total = 0
    for item in resources:
        if not isinstance(item, dict):
            raise BrokerError("invalid skill manifest resource")
        resource = safe_uri(item.get("uri"))
        if not resource.startswith(root) or resource.casefold() in seen:
            raise BrokerError("skill paths conflict or escape their origin namespace")
        relative = unquote(resource[len(root) :])
        # Host-independent safe extraction names, including Windows devices.
        for segment in relative.split("/"):
            if re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", segment):
                raise BrokerError("skill contains a reserved filesystem name")
        seen.add(resource.casefold())
        size = item.get("size")
        limit = profile.max_instruction_bytes if resource == uri else profile.max_file_bytes
        if type(size) is not int or not 0 <= size <= limit:
            raise BrokerError("skill resource exceeds host file budget")
        digest = item.get("digest")
        if not isinstance(digest, str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
            raise BrokerError("invalid skill resource digest")
        total += size
    if uri.casefold() not in seen or total > profile.max_skill_bytes:
        raise BrokerError("skill manifest missing SKILL.md or exceeds host total budget")
    for path in seen:
        parent = path.rsplit("/", 1)[0]
        while parent.startswith(root.casefold()):
            if parent in seen:
                raise BrokerError("skill file collides with a directory")
            parent = parent.rsplit("/", 1)[0]
    return entry


def validate_catalog(entries, profile):
    """Check cross-skill host limits, including conservative ZIP overhead."""
    if len(entries) > profile.max_skills:
        raise BrokerError("too many skills for the host profile")
    names = [entry["frontmatter"]["name"] for entry in entries]
    if profile.unique_names and len(names) != len(set(names)):
        raise BrokerError("host profile requires unique skill names")
    if profile.max_archive_bytes is not None:
        bound = 22 * len(entries)
        for entry in entries:
            root = entry["uri"].removesuffix("SKILL.md")
            for resource in entry["resources"]:
                size = resource["size"]
                path_bytes = len(
                    (entry["frontmatter"]["name"] + "/" + resource["uri"][len(root) :]).encode(
                        "utf-8"
                    )
                )
                bound += size + (size // 16383 + 1) * 5 + 6 + 92 + path_bytes * 2
        if bound > profile.max_archive_bytes:
            raise BrokerError("skill set exceeds conservative host archive budget")


def approval_digest(entry):
    # Include header/size as well as the required complete URI/digest set.
    return canonical_digest(
        {
            "frontmatter": entry["frontmatter"],
            "resources": sorted(entry["resources"], key=lambda item: item["uri"]),
        }
    )


def verify_file(entry, uri, raw):
    item = next((r for r in entry["resources"] if r["uri"] == uri), None)
    if (
        item is None
        or len(raw) != item["size"]
        or ("sha256:" + hashlib.sha256(raw).hexdigest() != item["digest"])
    ):
        raise BrokerError("skill content verification failed; refresh and reapprove")
    if uri == entry["uri"] and frontmatter(raw) != entry["frontmatter"]:
        raise BrokerError("skill frontmatter differs from its catalog entry")
