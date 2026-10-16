"""Opt-in native host adapter for coordinator-bound scratch reservations."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path

from . import physical, remote_scratch
from .host_call import bounded_result_size, digest, execute
from .host_store import CHUNK, Refused, _name
from .native_journal import NativeJournal, audit_root
from .process_identity import Identity, terminate
from .remote_api import Client, Session, _bytes, _encoded
from .remote_authority import Authority
from .shared_store import PATH_BYTES, ScratchLimits, SharedStore


class Host:
    """Keep one host incarnation's local journal; only trusted controllers invoke cleanup."""

    def __init__(
        self,
        root: Path,
        host: str,
        boot: str,
        session: str,
        config: Session,
        scratch: remote_scratch.Policy,
        storage: physical.Policy,
    ):
        self.root = root
        self.host, self.boot, self.session = (_name(v) for v in (host, boot, session))
        self.config, self.scratch, self.storage = config, scratch, storage

    def _store(self) -> SharedStore:
        return SharedStore(
            self.root,
            self.session,
            {
                "adapter": "remote-native-v1",
                "host": self.host,
                "boot": self.boot,
                "remote_profile": self.config.digest,
                "scratch": json.dumps(asdict(self.scratch), sort_keys=True),
            },
            self.config.limits,
            physical_policy=self.storage,
            prelaunch_recovery=True,
        )

    def _binding(self, receipt: remote_scratch.Receipt) -> bytes:
        owner = Authority._decode_owner(receipt.owner)
        _name(receipt.call)
        if (
            (owner.session, owner.resource.host, owner.resource.boot, owner.profile)
            != (self.session, self.host, self.boot, self.config.digest)
            or not re.fullmatch(r"[0-9a-f]{32}", receipt.token)
            or not re.fullmatch(r"[0-9a-f]{64}", receipt.request)
            or type(receipt.bytes) is not int
            or type(receipt.entries) is not int
            or (receipt.bytes, receipt.entries) != (self.scratch.bytes, self.scratch.entries)
        ):
            raise Refused("native scratch receipt binding differs")
        return json.dumps(asdict(receipt), sort_keys=True).encode()

    def _bounds(self, code: bytes, output_limit: int) -> None:
        limits = self.config.limits
        if len(code) > 65536 or type(output_limit) is not int or not 0 < output_limit <= CHUNK:
            raise Refused("request or output limit exceeds native bounds")
        if bounded_result_size(output_limit) > limits.result_bytes:
            raise Refused("maximum serialized result exceeds the result allowance")
        if (
            self.scratch.bytes
            < 2 * limits.checkpoint_bytes + 2 * CHUNK + output_limit + len(code) + 1024
            or self.scratch.entries < 2 * limits.files * (PATH_BYTES // 2 + 1) + 14
        ):
            raise Refused("scratch allowance cannot cover bounded native execution")

    def call(
        self,
        client: Client,
        owner: str,
        call: str,
        code: bytes,
        helper: Path,
        startup: Path,
        output_limit: int,
        boundary: Callable[[str], None] = lambda _: None,
    ) -> bytes:
        """Return committed results; leave both charges for explicit trusted reconciliation."""
        self._bounds(code, output_limit)
        admitted = client.call(
            "begin", self.session, owner=owner, call=call, request=_encoded(code)
        )
        if admitted.get("result") is not None:
            return _bytes(admitted["result"], self.config.limits.result_bytes)
        try:
            receipt = remote_scratch.Receipt(**admitted["scratch"])
        except (KeyError, TypeError) as error:
            raise Refused("coordinator did not provide a scratch receipt") from error
        if receipt.owner != owner or receipt.call != call:
            raise Refused("coordinator scratch admission differs")
        return self.execute(client, receipt, code, helper, startup, output_limit, boundary)

    def execute(
        self,
        client: Client,
        receipt: remote_scratch.Receipt,
        code: bytes,
        helper: Path,
        startup: Path,
        output_limit: int,
        boundary: Callable[[str], None] = lambda _: None,
    ) -> bytes:
        """Consume a receipt once; replayed or interrupted local identities cannot launch."""
        binding = self._binding(receipt)
        self._bounds(code, output_limit)
        if hashlib.sha256(code).hexdigest() != receipt.request:
            raise Refused("native request differs from receipt")
        if digest(helper) != self.config.profile.get("helper") or digest(
            startup / "index.json"
        ) != self.config.profile.get("startup_index"):
            raise Refused("native runtime differs from pinned profile")
        with self._store() as store:
            store.begin(
                receipt.token,
                binding,
                scratch=ScratchLimits(receipt.bytes, receipt.entries, self.scratch.total_bytes),
            )
            boundary("after_local_admission")
            journal = NativeJournal(store)
            work = journal.prepare(receipt.token)
            restored = work / "restored"
            limits = self.config.limits
            previous = client.restore(
                self.session,
                receipt.owner,
                restored,
                checkpoint_bytes=limits.checkpoint_bytes,
                files=limits.files,
            )
            boundary("after_remote_restore")
            result = execute(
                helper,
                restored if previous else startup,
                work,
                code,
                output_limit,
                before_spawn=lambda: journal.start(receipt.token, boundary),
                before_start=lambda child: journal.arm(receipt.token, child, boundary),
                checkpoint_limits=(limits.checkpoint_bytes, limits.files),
            )
            client.publish(
                self.session, receipt.owner, receipt.call, code, work / "candidate", result
            )
            boundary("after_remote_publication")
            return result

    def reconcile(
        self,
        receipt: remote_scratch.Receipt,
        boundary: Callable[[str], None] = lambda _: None,
    ) -> bool:
        """Stop the exact helper and remove scratch; missing local evidence refuses release."""
        binding = self._binding(receipt)
        if not (self.root / "shared.sqlite").is_file():
            raise Refused("native scratch journal is unavailable")
        with self._store() as store:
            row = store.db.execute(
                "SELECT request,status FROM calls WHERE session=? AND id=?",
                (self.session, receipt.token),
            ).fetchone()
            if row is None or row["request"] != hashlib.sha256(binding).hexdigest():
                raise Refused("native scratch receipt is not recorded")
            launch = store.db.execute(
                "SELECT identity FROM launches WHERE session=? AND call=?",
                (self.session, receipt.token),
            ).fetchone()
            if launch is not None:
                if launch["identity"] is not None:
                    terminate(Identity.decode(launch["identity"]))
                    boundary("after_helper_termination")
                NativeJournal(store).reclaim(receipt.token, boundary)
            elif row["status"] != "interrupted":
                raise Refused("native cleanup lacks a terminal identity")
            with store._transaction():
                audit_root(store)
            return True
