"""Skills consumer, live publication and host profiles using synthetic MCP servers."""

import base64
from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace

import pytest
from mcp import Client
from mcp.shared.exceptions import MCPError
from mcp.types import PaginatedRequestParams, Request

from universal_tools.budgets import JsonBudget
from universal_tools.extensions import ExtensionServer
from universal_tools.models import BrokerError
from universal_tools.skill_catalog import DirectoryParams, SkillCatalog, SkillList
from universal_tools.skill_client import SkillClient, SkillRegistry, SkillSource
from universal_tools.skill_manifest import (
    OPENAI_IMPORT_PROFILE,
    approval_digest,
    frontmatter,
    validate_catalog,
    validate_entry,
)


def package(tmp_path, name="synthetic", *, description="Synthetic fixture"):
    root = tmp_path / name
    root.mkdir(parents=True)
    (root / "SKILL.md").write_bytes(
        (
            f"---\nname: {name}\ndescription: {description}\n"
            "metadata:\n  version: 1\n---\nRead references/a.txt\n"
        ).encode()
    )
    (root / "references").mkdir()
    (root / "references" / "a.txt").write_bytes(b"exact\r\nbytes\n")
    (root / "binary.dat").write_bytes(b"\x00\xff\x01")
    return root


class CountedServer(ExtensionServer):
    def __init__(self, catalog):
        self.reads = []
        self.corrupt = False
        super().__init__("Synthetic Skills", extensions=[catalog], resource_providers=[catalog])

    async def _handle_read_resource(self, ctx, params):
        self.reads.append(str(params.uri))
        result = await super()._handle_read_resource(ctx, params)
        if self.corrupt:
            content = result.contents[0]
            if hasattr(content, "text"):
                content.text = "corrupted"
            else:
                content.blob = base64.b64encode(b"corrupted").decode()
        return result


def source(origin, server):
    @asynccontextmanager
    async def connect():
        async with Client(
            server, mode="2026-07-28", extensions=[SkillClient()], cache=None
        ) as client:
            yield client.session

    return SkillSource(origin, connect)


def approve(registry, origin, uri):
    digest = registry.describe(origin, uri)["approval_digest"]
    registry.approve(origin, uri, digest)
    return digest


async def test_lazy_reads_full_manifest_approval_exact_bytes_and_origin_isolation(tmp_path):
    first = SkillCatalog([package(tmp_path / "first")])
    second = SkillCatalog([package(tmp_path / "second", description="Different origin")])
    a, b = CountedServer(first), CountedServer(second)
    registry = SkillRegistry([source("one", a), source("two", b)])
    entries = await registry.discover("one")
    await registry.discover("two")
    assert not a.reads and not b.reads
    uri = entries[0]["uri"]
    with pytest.raises(BrokerError, match="approval"):
        await registry.read("one", uri, uri)
    digest = approve(registry, "one", uri)
    assert not a.reads  # Approval must not prefetch files.
    assert await registry.read("one", uri, uri) == first._files[uri]
    reference = uri.removesuffix("SKILL.md") + "references/a.txt"
    assert await registry.read("one", uri, reference) == b"exact\r\nbytes\n"
    assert await registry.read("one", uri, reference) == b"exact\r\nbytes\n"
    assert a.reads == [uri, reference]
    with pytest.raises(BrokerError, match="approval"):
        await registry.read("two", uri, uri)
    with pytest.raises(BrokerError, match="unlisted"):
        await registry.read("one", uri, reference + "-extra")
    approve(registry, "one", uri)
    # Even a matching cache label cannot bless changed cached bytes.
    key = next(key for key in registry._cache if key[1] == reference)
    registry._cache[key] = b"tampered"
    with pytest.raises(BrokerError, match="verification"):
        await registry.read("one", uri, reference)
    with pytest.raises(BrokerError, match="approval"):
        registry.approved_entry("one", uri)
    assert digest == approval_digest(entries[0])


async def test_manifest_change_revokes_approval_and_failed_refresh_is_closed(tmp_path):
    root = package(tmp_path)
    catalog = SkillCatalog([root])
    server = CountedServer(catalog)
    registry = SkillRegistry([source("origin", server)])
    uri = (await registry.discover("origin"))[0]["uri"]
    old = approve(registry, "origin", uri)
    (root / "new.txt").write_bytes(b"new")
    await catalog.publish([root])
    await registry.refresh("origin", uri)
    with pytest.raises(BrokerError, match="approval"):
        registry.approved_entry("origin", uri)
    with pytest.raises(BrokerError, match="changed"):
        registry.approve("origin", uri, old)
    approve(registry, "origin", uri)
    server.corrupt = True
    with pytest.raises(BrokerError, match="verification"):
        await registry.read("origin", uri, uri)
    approve(registry, "origin", uri)
    await catalog.publish([])
    with pytest.raises(BrokerError):
        await registry.refresh("origin", uri)
    with pytest.raises(BrokerError, match="approval"):
        registry.approved_entry("origin", uri)


async def test_live_publication_pagination_directory_and_stale_cursor(tmp_path):
    roots = [package(tmp_path, name) for name in ("alpha", "beta", "gamma")]
    catalog = SkillCatalog(roots, page_size=1, directory_read=True)
    server = CountedServer(catalog)
    async with Client(server, mode="2026-07-28", extensions=[SkillClient()], cache=None) as client:
        first = await client.session.send_request(
            Request(method="skills/list", params=PaginatedRequestParams()), SkillList
        )
        assert len(first.skills) == 1 and first.next_cursor
        second = await client.session.send_request(
            Request(method="skills/list", params=PaginatedRequestParams(cursor=first.next_cursor)),
            SkillList,
        )
        assert second.skills[0]["uri"] != first.skills[0]["uri"]
        directory = await catalog.read_directory(
            SimpleNamespace(protocol_version="2026-07-28"), DirectoryParams(uri="skill://alpha")
        )
        assert directory["nextCursor"] and len(directory["resources"]) == 1
        generation = catalog.generation
        revision = catalog.revision
        replacement = package(tmp_path, "delta")
        await catalog.publish([replacement], expected_revision=revision)
        assert catalog.generation == generation + 1
        # Same connected MCP client sees added and removed resource URIs.
        resources = await client.list_resources()
        assert all(
            "delta" in str(r.uri) or str(r.uri) == "broker://skills" for r in resources.resources
        )
        read = await client.read_resource("skill://delta/SKILL.md")
        assert read.contents[0].text == (replacement / "SKILL.md").read_text()
        with pytest.raises(MCPError):
            await client.read_resource("skill://alpha/SKILL.md")
        with pytest.raises(MCPError, match="cursor"):
            await client.session.send_request(
                Request(
                    method="skills/list", params=PaginatedRequestParams(cursor=first.next_cursor)
                ),
                SkillList,
            )
        with pytest.raises(BrokerError, match="revision"):
            await catalog.publish(roots, expected_revision=revision)
        assert catalog.revision != revision


async def test_federation_preserves_names_and_provenance_without_eager_reads(tmp_path):
    a = CountedServer(SkillCatalog([package(tmp_path / "a")]))
    b = CountedServer(SkillCatalog([package(tmp_path / "b")]))
    registry = SkillRegistry([source("app-a", a), source("app-b", b)])
    uri = "skill://synthetic/SKILL.md"
    for origin in ("app-a", "app-b"):
        await registry.discover(origin)
        approve(registry, origin, uri)
    catalog = SkillCatalog(directory_read=True)
    await catalog.publish(registry=registry, selections=[("app-a", uri), ("app-b", uri)])
    assert not a.reads and not b.reads
    assert len(catalog._entries) == 2
    async with Client(
        CountedServer(catalog), mode="2026-07-28", extensions=[SkillClient()], cache=None
    ) as client:
        for entry in catalog._entries.values():
            provenance = entry["_meta"]["org.codexmcpbroker/provenance"]
            assert provenance["uri"] == uri and provenance["origin"] in {"app-a", "app-b"}
            assert entry["frontmatter"]["name"] == "synthetic"
            read = await client.read_resource(entry["uri"])
            assert "name: synthetic" in read.contents[0].text
    registry.revoke_origin("app-a")
    entry = next(
        e
        for e in catalog._entries.values()
        if e["_meta"]["org.codexmcpbroker/provenance"]["origin"] == "app-a"
    )
    with pytest.raises(BrokerError, match="approval"):
        await catalog.read_bytes(entry["uri"])
    limited = SkillCatalog(profile=OPENAI_IMPORT_PROFILE)
    approve(registry, "app-a", uri)
    with pytest.raises(BrokerError, match="unique"):
        await limited.publish(registry=registry, selections=[("app-a", uri), ("app-b", uri)])
    assert limited._entries == {}  # Validation failure never partially publishes.


@pytest.mark.parametrize(
    "uri",
    [
        "skill://synthetic/../escape",
        "skill://synthetic/%2E%2E/file",
        "skill://synthetic/a%2Fb",
        "skill://synthetic/file?query",
        "skill://synthetic/file#fragment",
        "skill://synthetic/a%5Cb",
        "skill://synthetic/con.txt",
        "skill://synthetic/%00bad",
    ],
)
def test_unsafe_manifest_paths_are_rejected(tmp_path, uri):
    entry = next(iter(SkillCatalog([package(tmp_path)])._entries.values()))
    entry["resources"][1]["uri"] = uri
    with pytest.raises((BrokerError, ValueError)):
        validate_entry(entry)


@pytest.mark.parametrize(
    "header",
    [
        "name: duplicate\nname: duplicate\ndescription: x",
        "name: &a synthetic\ndescription: *a",
        "name: synthetic\ndescription: x\nmetadata: {a: .nan}",
    ],
)
def test_frontmatter_does_not_silently_normalize_conflicts(header):
    with pytest.raises(BrokerError):
        frontmatter(("---\n" + header + "\n---\nBody").encode())


def test_host_budgets_and_standard_512_file_capacity(tmp_path):
    root = package(tmp_path)
    for i in range(509):
        (root / f"support-{i}.txt").write_bytes(b"")
    catalog = SkillCatalog([root])
    assert len(next(iter(catalog._entries.values()))["resources"]) == 512
    with pytest.raises(BrokerError):
        SkillCatalog([root], profile=OPENAI_IMPORT_PROFILE)
    (root / "one-too-many").write_bytes(b"")
    with pytest.raises(BrokerError):
        SkillCatalog([root])


def test_standard_skill_instruction_can_fill_16_mib_with_bounded_frontmatter(tmp_path):
    from universal_tools.skill_manifest import verify_file

    root = tmp_path / "large-instruction"
    root.mkdir()
    header = b"---\nname: large-instruction\ndescription: Synthetic boundary\n---\n"
    raw = header + b"a" * (16 * 1024 * 1024 - len(header))
    (root / "SKILL.md").write_bytes(raw)
    catalog = SkillCatalog([root])
    entry = next(iter(catalog._entries.values()))
    verify_file(entry, entry["uri"], raw)
    assert entry["resources"][0]["size"] == 16 * 1024 * 1024
    with pytest.raises(BrokerError):
        SkillCatalog([root], profile=OPENAI_IMPORT_PROFILE)
    (root / "SKILL.md").write_bytes(raw + b"a")
    with pytest.raises(BrokerError):
        SkillCatalog([root])


def test_shape_budget_is_iterative_and_preserves_full_width_integers():
    JsonBudget().check({"integer": 2**63 - 1, "negative": -(2**63)})
    with pytest.raises(BrokerError, match="budget"):
        JsonBudget(max_nodes=3).check([1, 2, 3])
    nested = []
    for _ in range(10_000):
        nested = [nested]
    with pytest.raises(BrokerError, match="budget"):
        JsonBudget().check(nested)
    cyclic = []
    cyclic.append(cyclic)
    with pytest.raises(BrokerError):
        JsonBudget().check(cyclic)


def test_cli_skill_profile_is_explicit_and_does_not_install_or_persist(tmp_path, monkeypatch):
    from universal_tools.cli import main

    root = package(tmp_path)
    captured = []
    monkeypatch.setattr("universal_tools.cli.serve", lambda state, skills: captured.append(skills))
    state = tmp_path / "private-state"
    main(
        [
            "--state",
            str(state),
            "serve",
            "--skill-package",
            str(root),
            "--skill-profile",
            "openai-import",
            "--skill-page-size",
            "1",
            "--skill-directory-read",
        ]
    )
    assert captured[0].profile == OPENAI_IMPORT_PROFILE
    assert captured[0].settings() == {"directoryRead": True}
    assert not state.exists()


def test_openai_combined_archive_budget_and_distinct_normalized_paths(tmp_path):
    root = package(tmp_path)
    entry = next(iter(SkillCatalog([root])._entries.values()))
    entries = []
    for name in ("alpha", "beta"):
        other = deepcopy(entry)
        other["uri"] = f"skill://{name}/SKILL.md"
        other["frontmatter"]["name"] = name
        other["resources"] = [
            {**r, "uri": r["uri"].replace("synthetic", name)} for r in other["resources"]
        ]
        for index in range(5):
            other["resources"].append(
                {
                    "uri": f"skill://{name}/file-{index}.bin",
                    "size": 900_000,
                    "digest": "sha256:" + "a" * 64,
                }
            )
        validate_entry(other, OPENAI_IMPORT_PROFILE)
        entries.append(other)
    with pytest.raises(BrokerError, match="archive"):
        validate_catalog(entries, OPENAI_IMPORT_PROFILE)
    collision = deepcopy(entry)
    collision["resources"].append(
        {**collision["resources"][0], "uri": "skill://synthetic/skill.md"}
    )
    with pytest.raises(BrokerError, match="conflict"):
        validate_entry(collision)
    collision = deepcopy(entry)
    for path in ("%C3%A9.txt", "%C3%89.txt"):
        collision["resources"].append(
            {"uri": "skill://synthetic/" + path, "size": 0, "digest": "sha256:" + "a" * 64}
        )
    with pytest.raises(BrokerError, match="conflict"):
        validate_entry(collision)
