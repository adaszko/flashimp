from __future__ import annotations

import json
import os
import pickle
import sqlite3
import sys
from pathlib import Path
from typing import Self

import parser


def get_last_loaded_profile(base_path: Path):
    db_path = base_path / "prefs21.db"

    if not db_path.exists():
        console.print("Invalid base path!")
        console.print(f"path = {base_path.absolute()}")
        raise Abort()

    # Load metadata and profiles from database
    conn = sqlite3.connect(db_path)
    try:
        res = conn.execute(
            "select cast(data as blob) from profiles where name = '_global'"
        )
        _meta = pickle.loads(res.fetchone()[0])

        profiles = conn.execute(
            "select name, cast(data as blob) from profiles where name != '_global'"
        ).fetchall()
    finally:
        conn.close()

    return _meta.get("last_loaded_profile_name", profiles[0][0])


def get_collection_db_path(base_path: Path, profile_name: str):
    return str(base_path / profile_name / "collection.anki2")


def get_collection(collection_db_path: Path):
    from anki.collection import Collection
    from anki.errors import DBError

    # Save CWD (because Anki changes it)
    save_cwd = os.getcwd()

    try:
        col = Collection(str(collection_db_path))
        # Restore CWD (because Anki changes it)
        os.chdir(save_cwd)
        return col
    except AssertionError as error:
        console.print("Path to database is not valid!")
        console.print(f"path = {self._collection_db_path}")
        raise Abort() from error
    except DBError as error:
        console.print("Database is NA/locked!")
        raise Abort() from error


class Anki:
    def __init__(
        self,
        base_path: Path,
    ):
        self._collection_db_path: str = ""

        last_loaded_profile = get_last_loaded_profile(base_path)
        collection_db_path = get_collection_db_path(base_path, last_loaded_profile)
        self.col = get_collection(collection_db_path)

        self.model_name_to_id: dict[str, int] = {
            m["name"]: m["id"] for m in self.col.models.all()
        }

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        _exc_type,
        _exc_val,
        _exc_tb,
    ) -> None:
        self.col.close()

    def get_model(self, model_name: str) -> NotetypeDict | None:
        """Get model from model name"""
        from anki.models import NotetypeId

        model_id = self.model_name_to_id.get(model_name)
        if not isinstance(model_id, int):
            return None

        return self.col.models.get(NotetypeId(model_id))

    def set_model(self, model_name: str) -> NotetypeDict:
        """Set current model based on model name"""
        current = self.col.models.current(for_deck=False)
        if current["name"] == model_name:
            return current

        model = self.get_model(model_name)
        if model is None:
            print(f'Model "{model_name}" was not recognized!')
            raise SystemExit()

        self.col.models.set_current(model)
        return model


class LockedNotFound(RuntimeError):
    def __init__(self, message):
        self.message = message
        super().__init__(self.message)


def import_flashcards(
    anki_: Anki, markdown: str, locked_ids: dict[str, int]
) -> dict[str, int]:
    from anki.errors import NotFoundError
    from anki.notes import NoteId

    flashcards = parser.flashcards_from_markdown(markdown)
    # TODO partition flashcards by model to avoid switching models
    ids = locked_ids.copy()
    locked_not_found = {}
    for fc in flashcards:
        if fc.id() in locked_ids:
            note_id = locked_ids[fc.id()]
            try:
                existing_note = anki_.col.get_note(NoteId(note_id))
            except NotFoundError as _e:
                locked_not_found[fc.id()] = note_id
                continue
            existing_note.fields = [parser.html_from_markdown(f) for f in fc.fields()]
            anki_.col.update_note(existing_note)
        else:
            model = anki_.set_model(fc.model())
            model_field_names = [field["name"] for field in model["flds"]]
            assert len(model_field_names) == len(fc.fields()), (
                model_field_names,
                fc.fields(),
            )
            notetype = anki_.col.models.current(for_deck=False)
            new_note = anki_.col.new_note(notetype)
            new_note.fields = [parser.html_from_markdown(f) for f in fc.fields()]
            anki_.col.addNote(new_note)
            ids[fc.id()] = new_note.id

    if len(locked_not_found) > 0:
        raise LockedNotFound(locked_not_found)

    return ids


def get_locked_ids(lockfile_path: Path) -> dict[str, int]:
    try:
        text = lockfile_path.read_text()
    except FileNotFoundError:
        return {}
    ids = json.loads(text)
    return ids


def main() -> int:
    base_path = Path.home() / "Library/Application Support/Anki2"
    with Anki(base_path) as anki:
        markdown = Path("flashcards.md").read_text()
        lockfile_path = Path("flashcards.lock")
        locked_ids = get_locked_ids(lockfile_path)
        try:
            new_locked_ids = import_flashcards(anki, markdown, locked_ids)
        except LockedNotFound as e:
            print("Flashcards exist in the lock file but not in the database")
            for id, nid in e.args[0].items():
                print(f"{id}: {nid}")
            print(f"Try deleting {lockfile_path} file")
            return 1
        js = json.dumps(new_locked_ids)
        lockfile_path.write_text(js)
    return 0


if __name__ == "__main__":
    sys.exit(main())
