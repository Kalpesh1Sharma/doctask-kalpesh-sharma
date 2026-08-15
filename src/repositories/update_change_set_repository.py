from dataclasses import dataclass
from datetime import datetime
from typing import cast

from psycopg import Connection
from psycopg.types.json import Jsonb
from pydantic import JsonValue

from domain.update_review import (
    UpdateChangeSetManifest,
    update_change_set_fingerprint,
    validate_stored_update_change_set,
)

_UPDATE_CHANGE_SET_COLUMNS = """
    id,
    run_id,
    baseline_run_id,
    trigger_document_version_id,
    preparation_fingerprint,
    review_state_fingerprint,
    render_input_fingerprint,
    manifest,
    change_set_fingerprint,
    created_at
"""


@dataclass(frozen=True)
class UpdateChangeSetRecord:
    change_set_id: str
    run_id: str
    baseline_run_id: str
    trigger_document_version_id: str
    preparation_fingerprint: str
    review_state_fingerprint: str
    render_input_fingerprint: str
    manifest: JsonValue
    change_set_fingerprint: str
    created_at: datetime

    @classmethod
    def from_row(cls, row: tuple[object, ...]) -> "UpdateChangeSetRecord":
        return cls(
            change_set_id=str(row[0]),
            run_id=str(row[1]),
            baseline_run_id=str(row[2]),
            trigger_document_version_id=str(row[3]),
            preparation_fingerprint=cast(str, row[4]),
            review_state_fingerprint=cast(str, row[5]),
            render_input_fingerprint=cast(str, row[6]),
            manifest=cast(JsonValue, row[7]),
            change_set_fingerprint=cast(str, row[8]),
            created_at=cast(datetime, row[9]),
        )

    def to_manifest(self) -> UpdateChangeSetManifest:
        return validate_stored_update_change_set(self.manifest, self.change_set_fingerprint)


class UpdateChangeSetRepository:
    """Connection-scoped persistence for immutable UPDATE change-set rows."""

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def create(self, manifest: UpdateChangeSetManifest) -> UpdateChangeSetRecord:
        manifest_value = manifest.model_dump(mode="json")
        row = self._connection.execute(
            f"""
            INSERT INTO update_change_sets (
                run_id,
                baseline_run_id,
                trigger_document_version_id,
                preparation_fingerprint,
                review_state_fingerprint,
                render_input_fingerprint,
                manifest,
                change_set_fingerprint
            )
            VALUES (%s::uuid, %s::uuid, %s::uuid, %s, %s, %s, %s::jsonb, %s)
            RETURNING {_UPDATE_CHANGE_SET_COLUMNS}
            """,
            (
                manifest.run_id,
                manifest.baseline_run_id,
                manifest.trigger_document_version_id,
                manifest.preparation_fingerprint,
                manifest.review_state_fingerprint,
                manifest.render_input_fingerprint,
                Jsonb(manifest_value),
                update_change_set_fingerprint(manifest),
            ),
        ).fetchone()
        assert row is not None
        return UpdateChangeSetRecord.from_row(row)

    def get_by_id(self, change_set_id: str) -> UpdateChangeSetRecord | None:
        row = self._connection.execute(
            f"""
            SELECT {_UPDATE_CHANGE_SET_COLUMNS}
            FROM update_change_sets
            WHERE id = %s::uuid
            """,
            (change_set_id,),
        ).fetchone()
        return UpdateChangeSetRecord.from_row(row) if row is not None else None

    def get_by_run_id(self, run_id: str) -> UpdateChangeSetRecord | None:
        row = self._connection.execute(
            f"""
            SELECT {_UPDATE_CHANGE_SET_COLUMNS}
            FROM update_change_sets
            WHERE run_id = %s::uuid
            """,
            (run_id,),
        ).fetchone()
        return UpdateChangeSetRecord.from_row(row) if row is not None else None
