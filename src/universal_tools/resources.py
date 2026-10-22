"""Durable app resource ownership, independent of MCP and live management endpoints.

Records never authorize adopting an arbitrary process. Applications explicitly declare
ownership and cleanup policy. Process creation identity is rechecked on an OS handle
before termination; a stale PID, attached app or uncertain intent is never sufficient.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
import uuid
from contextlib import ExitStack, contextmanager
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Literal

from pydantic import Field, field_validator, model_validator

from .instance_lock import InstanceLock
from .management import default_registry, private_directory, read_json
from .models import ID_PATTERN, StrictModel, canonical_digest

TOKEN = r"^[0-9a-f]{32}$"
MAX_RESOURCES = 4096
TERMINAL = {"released", "terminated", "failed"}


class ProcessIdentity(StrictModel):
    host_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    pid: int = Field(ge=1, le=2**32 - 1)
    created: str = Field(pattern=r"^[0-9]{1,24}$")
    executable: str = Field(min_length=1, max_length=32768)

    @field_validator("created")
    @classmethod
    def canonical_creation(cls, value):
        return str(int(value))

    @field_validator("executable")
    @classmethod
    def absolute_image(cls, value):
        if "\0" in value or not (
            PureWindowsPath(value).is_absolute() or PurePosixPath(value).is_absolute()
        ):
            raise ValueError("process executable must be an absolute image path")
        return os.path.normcase(os.path.normpath(value))


class CleanupReceipt(StrictModel):
    request_id: str = Field(pattern=TOKEN)
    expected_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    outcome: Literal["dispatching", "terminated", "already_exited", "uncertain"]
    message: str = Field(max_length=512)


class ResourceReconciliation(StrictModel):
    actor_owner_id: str = Field(pattern=TOKEN)
    actor: ProcessIdentity
    previous_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence: str = Field(min_length=1, max_length=1024)
    recorded_at: float = Field(allow_inf_nan=False)


class ResourceRecord(StrictModel):
    schema_version: Literal[1]
    resource_id: str = Field(pattern=TOKEN)
    app_id: str = Field(pattern=ID_PATTERN)
    owner_id: str = Field(pattern=TOKEN)
    owner: ProcessIdentity
    label: str = Field(min_length=1, max_length=256)
    kind: str = Field(default="process", pattern=ID_PATTERN)
    ownership: Literal["private", "attached", "shared"]
    cleanup_mode: Literal["terminate", "report"] = "report"
    state: Literal["launching", "active", "released", "terminated", "failed", "uncertain"]
    process: ProcessIdentity | None = None
    members: list[ProcessIdentity] = Field(default_factory=list, max_length=256)
    containment: Literal["app_owned", "windows_job"] = "app_owned"
    recovery_barrier: str | None = Field(default=None, max_length=512)
    receipt: CleanupReceipt | None = None
    reconciliation: ResourceReconciliation | None = None
    barrier_resolution: ResourceReconciliation | None = None
    created_at: float = Field(allow_inf_nan=False)
    updated_at: float = Field(allow_inf_nan=False)

    @field_validator("schema_version", mode="before")
    @classmethod
    def integer_version(cls, value):
        if type(value) is not int or value != 1:
            raise ValueError("resource schema_version must be integer 1")
        return value

    @model_validator(mode="after")
    def policy(self):
        if self.ownership != "private" and self.cleanup_mode != "report":
            raise ValueError("attached and shared resources are report-only")
        if self.process is not None and self.process == self.owner:
            raise ValueError("a resource cannot be its own owner process")
        if any(
            identity.host_id != self.owner.host_id
            for identity in [*self.members, *([self.process] if self.process else [])]
        ):
            raise ValueError("an owner and its process resources must belong to the same host")
        if self.ownership == "private" and any(
            int(identity.created) < int(self.owner.created)
            for identity in [*self.members, *([self.process] if self.process else [])]
        ):
            raise ValueError("private processes cannot predate their owning supervisor")
        return self

    @property
    def revision(self):
        return canonical_digest(self.model_dump(mode="json"))


def durable_json(path, value):
    """Flush record bytes before atomic publication; intents precede effects."""
    fd, temporary = tempfile.mkstemp(prefix=".resource-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=True, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class ResourceLedger:
    """Private bounded JSON inventory with serialized mutation and cleanup receipts.

    The process driver is injectable for isolated tests. The current native driver
    supports Windows; unsupported platforms report unknown instead of guessing.
    """

    def __init__(self, registry=None, *, driver=None, max_active_per_app=32):
        self.directory = Path(registry or default_registry()).absolute() / "resources"
        if type(max_active_per_app) is not int or not 1 <= max_active_per_app <= 256:
            raise ValueError("invalid active-resource limit")
        self.maximum = max_active_per_app
        if driver is None:
            from .windows_resources import ProcessDriver

            driver = ProcessDriver()
        self.driver = driver
        self._mutex = threading.RLock()

    def _path(self, resource_id):
        if not isinstance(resource_id, str) or not re.fullmatch(TOKEN, resource_id):
            raise ValueError("invalid resource identity")
        return self.directory / f"{resource_id}.json"

    @contextmanager
    def _locked(self):
        private_directory(self.directory.parent)
        private_directory(self.directory)
        lock = self.directory / "ledger.lock"
        if lock.exists() and (
            lock.is_symlink() or getattr(lock.stat(), "st_file_attributes", 0) & 0x400
        ):
            raise ValueError("resource lock must be an ordinary file")
        with self._mutex, InstanceLock(lock):
            yield

    def get(self, resource_id):
        record = ResourceRecord.model_validate(read_json(self._path(resource_id)))
        if record.resource_id != resource_id:
            raise ValueError("resource identity differs from its filename")
        return record

    def records(self):
        if not self.directory.exists():
            return []
        if (
            self.directory.is_symlink()
            or getattr(self.directory.stat(), "st_file_attributes", 0) & 0x400
        ):
            raise ValueError("resource directory must not be a link or junction")
        paths = []
        for path in self.directory.iterdir():
            if path.suffix == ".json":
                paths.append(path)
                if len(paths) > MAX_RESOURCES:
                    raise ValueError("resource history capacity exceeded")
        # Invalid records block cleanup/start admission; never silently skip a claim.
        return [self.get(path.stem) for path in sorted(paths)]

    def _save(self, record):
        value = record.model_dump(mode="json")
        if len(json.dumps(value, ensure_ascii=True, allow_nan=False).encode("utf-8")) + 1 > 32768:
            raise ValueError("resource record exceeds its byte budget")
        durable_json(self._path(record.resource_id), value)

    def _replace(self, record, **changes):
        value = {**record.model_dump(mode="json"), **changes, "updated_at": time.time()}
        replacement = ResourceRecord.model_validate(value)
        self._save(replacement)
        return replacement

    def owner(self, app_id, *, owner_id=None):
        if not re.fullmatch(ID_PATTERN, app_id):
            raise ValueError("invalid application identity")
        identity = ProcessIdentity.model_validate(self.driver.capture(os.getpid()))
        generation = owner_id or uuid.uuid4().hex
        if not re.fullmatch(TOKEN, generation):
            raise ValueError("invalid owner identity")
        return ResourceOwner(self, app_id, generation, identity)

    def _current_owner(self, record, owner):
        if (
            record.app_id != owner.app_id
            or record.owner_id != owner.owner_id
            or record.owner != owner.identity
            or ProcessIdentity.model_validate(self.driver.capture(os.getpid())) != owner.identity
        ):
            raise ValueError("resource mutation requires its exact current owner")

    def _conflict(self, record, records):
        identities = set(record.members)
        if record.process:
            identities.add(record.process)
        return bool(identities) and any(
            other.resource_id != record.resource_id
            and other.state not in TERMINAL
            and (other.process in identities or bool(set(other.members) & identities))
            for other in records
        )

    def plan(self, resource_id):
        return self._plan(self.get(resource_id), self.records())

    def _plan(self, record, records, *, owner_states=None, process_claims=None):
        if owner_states is not None and record.owner in owner_states:
            owner_state = owner_states[record.owner]
        else:
            owner_state = self.driver.state(record.owner.model_dump())
            if owner_states is not None:
                owner_states[record.owner] = owner_state
        process_state = (
            self.driver.state(record.process.model_dump()) if record.process else "unknown"
        )
        reason = "Ready for explicit cleanup"
        eligible = True
        if record.state in TERMINAL:
            eligible, reason = False, "Resource is already retired"
        elif record.receipt is not None:
            eligible, reason = False, "Cleanup was already claimed; inspect its receipt"
        elif record.recovery_barrier or record.state == "uncertain":
            eligible, reason = False, "App-owned recovery is required"
        elif record.ownership != "private" or record.cleanup_mode != "terminate":
            eligible, reason = False, "Attached/shared/report-only resources cannot be terminated"
        elif owner_state not in {"absent", "different"}:
            eligible, reason = False, "Owner is live or its identity cannot be verified"
        elif record.process is None or process_state == "unknown":
            eligible, reason = False, "Process identity is unavailable; no PID may be guessed"
        elif (
            any(process_claims.get(i, 0) > 1 for i in {record.process, *record.members})
            if process_claims is not None
            else self._conflict(record, records)
        ):
            eligible, reason = False, "Another retained resource claim references this process"
        return {
            "resource_id": record.resource_id,
            "app_id": record.app_id,
            "owner_id": record.owner_id,
            "label": record.label,
            "revision": record.revision,
            "owner_state": owner_state,
            "process_state": process_state,
            "eligible": eligible,
            "reason": reason,
            "record": record.model_dump(mode="json"),
        }

    def snapshot(self):
        from collections import Counter

        records = self.records()
        claims = Counter(
            identity
            for r in records
            if r.state not in TERMINAL
            for identity in ({r.process, *r.members} - {None})
        )
        owner_states = {}
        return [
            self._plan(r, records, owner_states=owner_states, process_claims=claims)
            for r in records
        ]

    def cleanup(self, resource_id, expected_revision, request_id, *, _owner=None, _group=None):
        if not isinstance(request_id, str) or not re.fullmatch(TOKEN, request_id):
            raise ValueError("invalid cleanup request identity")
        with self._locked(), ExitStack() as stack:
            record = self.get(resource_id)
            if record.receipt:
                if (
                    record.receipt.request_id == request_id
                    and record.receipt.expected_revision == expected_revision
                ):
                    return record.receipt.model_dump()
                raise ValueError("cleanup already claimed; do not replay or replace its request")
            if record.revision != expected_revision:
                raise ValueError("resource record changed; review a new cleanup plan")
            if any(r.receipt and r.receipt.request_id == request_id for r in self.records()):
                raise ValueError("cleanup request identity belongs to another resource")
            plan = self.plan(resource_id)
            if _owner is not None:
                self._current_owner(record, _owner)
                # App-owned normal teardown still obeys all other admission gates.
                plan["eligible"] = (
                    record.ownership == "private"
                    and record.cleanup_mode == "terminate"
                    and record.state == "active"
                    and not record.recovery_barrier
                    and record.process is not None
                    and not self._conflict(record, self.records())
                )
            if not plan["eligible"]:
                raise ValueError(plan["reason"])
            if _group is not None and (
                _owner is None
                or record.containment != "windows_job"
                or ProcessIdentity.model_validate(_group.identity) != record.process
            ):
                raise ValueError("process tree does not belong to this exact resource owner")
            if _group is not None:
                members = [ProcessIdentity.model_validate(i) for i in _group.identities()]
                candidate = ResourceRecord.model_validate(
                    {**record.model_dump(), "members": [i.model_dump() for i in members]}
                )
                if self._conflict(candidate, self.records()):
                    raise ValueError(
                        "another resource owner references a member of this process tree"
                    )
                if candidate.members != record.members:
                    record = self._replace(record, members=[i.model_dump() for i in members])
            handle = None
            if plan["process_state"] == "alive":
                # Hold the exact checked OS process, not just its recyclable PID.
                handle = stack.enter_context(self.driver.open(record.process.model_dump()))
            elif plan["process_state"] not in {"absent", "different"}:
                raise ValueError("process identity is unavailable")
            receipt = CleanupReceipt(
                request_id=request_id,
                expected_revision=expected_revision,
                outcome="dispatching",
                message="Cleanup intent recorded; outcome not yet verified",
            )
            pending = self._replace(record, state="uncertain", receipt=receipt.model_dump())
            try:
                if _group is not None:
                    _group.stop()
                elif handle:
                    handle.terminate()
                    if not handle.wait(5000):
                        raise RuntimeError("process did not stop before the cleanup deadline")
                result = CleanupReceipt(
                    request_id=request_id,
                    expected_revision=expected_revision,
                    outcome="terminated" if handle or _group is not None else "already_exited",
                    message="Owned process tree stopped"
                    if _group is not None
                    else (
                        "Exact recorded process exited" if handle else "Recorded process is gone"
                    ),
                )
                self._replace(pending, state="terminated", receipt=result.model_dump())
            except Exception:
                result = CleanupReceipt(
                    request_id=request_id,
                    expected_revision=expected_revision,
                    outcome="uncertain",
                    message="Inspect actual app/resource state; cleanup must not be replayed",
                )
                self._replace(pending, receipt=result.model_dump())
            return result.model_dump()


class ResourceOwner:
    """An app supervisor generation. It cannot adopt another generation's resources."""

    def __init__(self, ledger, app_id, owner_id, identity):
        self.ledger = ledger
        self.app_id, self.owner_id, self.identity = app_id, owner_id, identity
        self._launches = {}
        self._closing = False

    def begin_launch(self, label, *, kind="process", ownership="private", cleanup_mode="report"):
        ledger = self.ledger
        with ledger._locked():
            if self._closing:
                raise ValueError("resource owner is closing; startup admission is fenced")
            records = ledger.records()
            if len(records) >= MAX_RESOURCES:
                raise ValueError(
                    "resource history capacity reached; preserve/archive prior evidence"
                )
            if (
                sum(r.app_id == self.app_id and r.state not in TERMINAL for r in records)
                >= ledger.maximum
            ):
                raise ValueError(
                    "active app resource limit reached; recover existing resources first"
                )
            now = time.time()
            record = ResourceRecord(
                schema_version=1,
                resource_id=uuid.uuid4().hex,
                app_id=self.app_id,
                owner_id=self.owner_id,
                owner=self.identity,
                label=label,
                kind=kind,
                ownership=ownership,
                cleanup_mode=cleanup_mode,
                state="launching",
                created_at=now,
                updated_at=now,
            )
            ledger._current_owner(record, self)
            if ledger._path(record.resource_id).exists():
                raise ValueError(
                    "resource identity already exists; existing evidence was preserved"
                )
            ledger._save(record)
            return record

    def bind_process(self, resource_id, pid, *, containment="app_owned"):
        ledger = self.ledger
        with ledger._locked():
            record = ledger.get(resource_id)
            ledger._current_owner(record, self)
            if record.state != "launching" or record.process is not None:
                raise ValueError("resource already bound or no longer awaiting launch")
            identity = ProcessIdentity.model_validate(ledger.driver.capture(pid))
            if identity == self.identity:
                raise ValueError("owner cannot register itself as its resource")
            if record.ownership == "private" and int(identity.created) < int(self.identity.created):
                raise ValueError("a private process cannot predate its owning supervisor")
            candidate = ResourceRecord.model_validate(
                {**record.model_dump(), "process": identity.model_dump()}
            )
            if record.ownership == "private" and ledger._conflict(candidate, ledger.records()):
                raise ValueError("another retained claim already references this process")
            return ledger._replace(
                record, process=identity.model_dump(), state="active", containment=containment
            )

    def remember_process(
        self, pid, label, *, ownership="attached", cleanup_mode="report", kind="process"
    ):
        record = self.begin_launch(label, ownership=ownership, cleanup_mode=cleanup_mode, kind=kind)
        return self.bind_process(record.resource_id, pid)

    def set_recovery_barrier(self, resource_id, reason):
        if not isinstance(reason, str) or not reason:
            raise ValueError("a recovery barrier needs an explicit app-owned reason")
        with self.ledger._locked():
            record = self.ledger.get(resource_id)
            self.ledger._current_owner(record, self)
            return self.ledger._replace(record, recovery_barrier=reason)

    def resolve_recovery_barrier(self, resource_id, expected_revision, evidence):
        """App-only attestation of inspected state; no manager shortcut or write replay."""
        with self.ledger._locked():
            record = self.ledger.get(resource_id)
            self.ledger._current_owner(record, self)
            if record.revision != expected_revision or not record.recovery_barrier:
                raise ValueError("a reviewed unresolved barrier is required")
            if record.receipt is not None:
                raise ValueError("cleanup receipts cannot be cleared by barrier resolution")
            attestation = ResourceReconciliation(
                actor_owner_id=self.owner_id,
                actor=self.identity,
                previous_revision=record.revision,
                evidence=evidence,
                recorded_at=time.time(),
            )
            return self.ledger._replace(
                record, recovery_barrier=None, barrier_resolution=attestation.model_dump()
            )

    def release_attachment(self, resource_id):
        with self.ledger._locked():
            record = self.ledger.get(resource_id)
            self.ledger._current_owner(record, self)
            if record.ownership == "private":
                raise ValueError("private software must be stopped/verified, not forgotten")
            return self.ledger._replace(record, state="released")

    def terminate_private(self, resource_id, expected_revision, request_id):
        record = self.ledger.get(resource_id)
        if record.containment == "windows_job":
            raise ValueError("close the owned launch handle to stop its entire process tree")
        return self.ledger.cleanup(resource_id, expected_revision, request_id, _owner=self)

    def reconcile(self, resource_id, expected_revision, evidence):
        """App-only attestation after independent inspection; never clears write journals.

        A private process must already be gone. Attached/shared records release only
        the departed owner's claim. Existing uncertain receipts/barriers are retained.
        This is deliberately absent from the manager CLI/GUI and MCP surface.
        """
        ledger = self.ledger
        with ledger._locked():
            record = ledger.get(resource_id)
            if record.app_id != self.app_id or record.revision != expected_revision:
                raise ValueError("reconciliation requires the same app and reviewed revision")
            if ProcessIdentity.model_validate(ledger.driver.capture(os.getpid())) != self.identity:
                raise ValueError("reconciliation requires the exact current supervisor")
            owner_state = ledger.driver.state(record.owner.model_dump())
            current_owner = record.owner == self.identity and record.owner_id == self.owner_id
            if not current_owner and owner_state not in {"absent", "different"}:
                raise ValueError("another recorded owner is live or cannot be verified")
            if record.reconciliation is not None or record.state in TERMINAL:
                raise ValueError("resource has already been retired/reconciled")
            if record.ownership == "private" and record.process is not None:
                if ledger.driver.state(record.process.model_dump()) not in {"absent", "different"}:
                    raise ValueError("inspect/stop the private process before attesting cleanup")
            attestation = ResourceReconciliation(
                actor_owner_id=self.owner_id,
                actor=self.identity,
                previous_revision=record.revision,
                evidence=evidence,
                recorded_at=time.time(),
            )
            return ledger._replace(
                record, state="released", reconciliation=attestation.model_dump()
            )

    def launch(self, argv, label, *, allow_termination=False, **options):
        """Launch a private, kill-on-owner-exit Windows tree after durable reservation.

        Apps must explicitly authorize forced teardown for this resource and preserve
        native document/write evidence themselves. This API does not attach to user
        software or provide inherited stdio for an MCP transport.
        """
        if allow_termination is not True:
            raise ValueError("the app must explicitly authorize owned-tree termination")
        record = self.begin_launch(label, cleanup_mode="terminate")
        group = None
        try:
            group = self.ledger.driver.create_suspended(argv, **options)
            record = self.bind_process(record.resource_id, group.pid, containment="windows_job")
            group.resume()
        except BaseException:
            if group is not None:
                group.close()
            # The worker never executes before the identity record is durable. A
            # remaining launch intent after a journal failure stays visible/blocked.
            with self.ledger._locked():
                current = self.ledger.get(record.resource_id)
                self.ledger._replace(current, state="failed")
            raise
        launch = OwnedLaunch(self, group, record.resource_id)
        self._launches[record.resource_id] = launch
        return launch

    def close(self):
        ledger = self.ledger
        with ledger._locked():
            self._closing = True
            records = [
                r
                for r in ledger.records()
                if r.app_id == self.app_id
                and r.owner_id == self.owner_id
                and r.owner == self.identity
                and r.state not in TERMINAL
            ]
            for record in records:
                if record.recovery_barrier or record.receipt or record.state != "active":
                    raise ValueError("app-owned recovery blocks closing this resource owner")
                if record.ownership != "private":
                    raise ValueError(
                        "detach/release owned attachments and shared leases before close"
                    )
                if record.cleanup_mode == "report" and (
                    record.process is None
                    or ledger.driver.state(record.process.model_dump())
                    not in {"absent", "different"}
                ):
                    raise ValueError("report-only private software needs app-owned close/readback")
        for launch in list(self._launches.values()):
            launch.close()
        for record in records:
            current = ledger.get(record.resource_id)
            if current.state in TERMINAL:
                continue
            if current.cleanup_mode == "terminate":
                self.terminate_private(current.resource_id, current.revision, uuid.uuid4().hex)
            else:
                self.reconcile(
                    current.resource_id, current.revision, "Observed private process exit on close"
                )

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class OwnedLaunch:
    def __init__(self, owner, group, resource_id):
        self.owner, self.group, self.resource_id = owner, group, resource_id
        self._receipt = None

    @property
    def pid(self):
        return self.group.pid

    def close(self):
        if self._receipt is not None:
            return self._receipt
        record = self.owner.ledger.get(self.resource_id)
        receipt = self.owner.ledger.cleanup(
            self.resource_id,
            record.revision,
            uuid.uuid4().hex,
            _owner=self.owner,
            _group=self.group,
        )
        self._receipt = receipt
        self.group.close()
        self.owner._launches.pop(self.resource_id, None)
        return receipt

    def refresh(self):
        """Persist a current membership snapshot; the job covers unsampled descendants too."""
        ledger = self.owner.ledger
        with ledger._locked():
            record = ledger.get(self.resource_id)
            ledger._current_owner(record, self.owner)
            if record.state != "active":
                raise ValueError("only an active process tree can publish members")
            members = [ProcessIdentity.model_validate(i) for i in self.group.identities()]
            if members != record.members:
                return ledger._replace(record, members=[i.model_dump() for i in members])
            return record

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
