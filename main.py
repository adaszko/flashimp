from __future__ import annotations

import json
import os
import pickle
import sqlite3
import sys
from pathlib import Path

from anki.collection import Collection

import parser


def get_last_loaded_profile(base_path: Path):
    prefs_db_path = base_path / "prefs21.db"

    if not prefs_db_path.exists():
        raise FileNotFoundError(prefs_db_path)

    # Load metadata and profiles from database
    conn = sqlite3.connect(prefs_db_path)
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


def get_collection(collection_db_path: Path) -> Collection:
    saved_cwd = os.getcwd()
    try:
        col = Collection(str(collection_db_path))
    finally:
        os.chdir(saved_cwd)
    return col


class LockedNotFound(RuntimeError):
    def __init__(self, message):
        self.message = message
        super().__init__(self.message)


def import_flashcards(
    col: Collection, markdown: str, locked_ids: dict[str, int]
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
                existing_note = col.get_note(NoteId(note_id))
            except NotFoundError as _e:
                locked_not_found[fc.id()] = note_id
                continue
            existing_note.fields = [parser.html_from_markdown(f) for f in fc.fields()]
            col.update_note(existing_note)
        else:
            model = col.models.by_name(fc.model())
            model_field_names = [field["name"] for field in model["flds"]]
            assert len(model_field_names) == len(fc.fields()), (
                model_field_names,
                fc.fields(),
            )
            notetype = col.models.current(for_deck=False)
            new_note = col.new_note(notetype)
            new_note.fields = [parser.html_from_markdown(f) for f in fc.fields()]
            col.addNote(new_note)
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


def do_main(col: Collection, lockfile_path: Path):
    markdown = Path("flashcards.md").read_text()
    locked_ids = get_locked_ids(lockfile_path)
    new_locked_ids = import_flashcards(col, markdown, locked_ids)
    js = json.dumps(new_locked_ids)
    lockfile_path.write_text(js)


def main() -> int:
    base_path = Path.home() / "Library/Application Support/Anki2"
    lockfile_path = Path("flashcards.lock")
    last_loaded_profile = get_last_loaded_profile(base_path)
    collection_db_path = get_collection_db_path(base_path, last_loaded_profile)
    col = get_collection(collection_db_path)
    exitcode = 0
    try:
        do_main(col, lockfile_path)
    except LockedNotFound as e:
        print("Flashcards exist in the lock file but not in the database")
        for id, nid in e.args[0].items():
            print(f"{id}: {nid}")
        print(f"Try deleting {lockfile_path} file")
        exitcode = 1
    finally:
        col.close()
    return exitcode


if __name__ == "__main__":
    sys.exit(main())
