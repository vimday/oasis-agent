"""Read original checkpoint proofs while writing diagnostic captures only temporarily."""

import fcntl
from collections.abc import Iterator
from contextlib import ExitStack, closing, contextmanager
from pathlib import Path

from ..evidence.store import EvidenceRecord, EvidenceStore, ReadOnlyEvidenceStore
from ..models.common import EvidenceRef


@contextmanager
def precheck_evidence(
    temporary: Path, retained: Path, state_lock: Path
) -> Iterator["PrecheckEvidenceStore"]:
    """Refuse a running strategy before reading its evidence or spending RPC quota.

    Hold its existing lock shared for the diagnostic, so its exclusive writer
    cannot restart while the read snapshot is open. Never create a missing lock.
    """
    with ExitStack() as resources:
        if state_lock.exists():
            lock = resources.enter_context(state_lock.open("rb"))
            fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        evidence = resources.enter_context(closing(PrecheckEvidenceStore(temporary, retained)))
        yield evidence


class PrecheckEvidenceStore(EvidenceStore):
    """A temporary writable layer over one optional existing read-only snapshot.

    Retained records take precedence: a corrupted original must never be repaired
    silently by an ephemeral duplicate. Only an absent key permits a lookup in
    the temporary layer. Missing original files are never created; an existing
    state that requires their proofs will consequently fail checkpoint replay.
    """

    def __init__(self, temporary: Path, retained: Path) -> None:
        if temporary.exists() or temporary.resolve() == retained.resolve():
            raise ValueError("precheck requires a new separate temporary evidence store")
        self.retained = ReadOnlyEvidenceStore(retained) if retained.exists() else None
        try:
            super().__init__(temporary)
        except Exception:
            if self.retained is not None:
                self.retained.close()
            raise

    def get(self, reference: EvidenceRef) -> bytes:
        if self.retained is not None:
            try:
                return self.retained.get(reference)
            except KeyError:
                pass
        return super().get(reference)

    def get_record(self, reference: EvidenceRef) -> EvidenceRecord:
        if self.retained is not None:
            try:
                return self.retained.get_record(reference)
            except KeyError:
                pass
        return super().get_record(reference)

    def close(self) -> None:
        try:
            super().close()
        finally:
            if self.retained is not None:
                self.retained.close()
