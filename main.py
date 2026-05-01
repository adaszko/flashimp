from __future__ import annotations

import dataclasses
import json
import pickle
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

from anki.collection import Collection
from anki.errors import NotFoundError
from anki.notes import NoteId

import parser
from parser import Flashcard


class UnknownModel(RuntimeError):
    def __init__(self, message):
        self.message = message
        super().__init__(self.message)


class LockedNotFound(RuntimeError):
    def __init__(self, message):
        self.message = message
        super().__init__(self.message)


@dataclass
class LockedNote:
    mid: int
    nid: int


@dataclass
class Lockfile:
    profile: str
    deck: str
    notes: dict[str, LockedNote]  # id -> (nid, notetypeid)


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


def import_flashcards(
    col: Collection,
    flashcards: list[Flashcard],
    input_lockfile: Lockfile | None,
    profile: str,
    deck: str,
) -> Lockfile:
    if input_lockfile is None:
        input_notes = {}
    else:
        input_notes = input_lockfile.notes.copy()

    if input_lockfile is None:
        output_lockfile = Lockfile(profile=profile, deck=deck, notes={})
    else:
        output_lockfile = Lockfile(
            profile=input_lockfile.profile,
            deck=input_lockfile.deck,
            notes=input_notes,
        )

    locked_not_found = {}
    for fc in flashcards:
        if fc.id() in input_notes:
            note_id = input_notes[fc.id()].nid
            try:
                existing_note = col.get_note(NoteId(note_id))
            except NotFoundError as _e:
                locked_not_found[fc.id()] = note_id
                continue
            existing_note.fields = [parser.html_from_markdown(f) for f in fc.fields()]
            col.update_note(existing_note)
        else:
            model = col.models.by_name(fc.model())
            if model is None:
                raise UnknownModel(fc.model())
            model_field_names = [field["name"] for field in model["flds"]]
            assert len(model_field_names) == len(fc.fields()), (
                model_field_names,
                fc.fields(),
            )
            note = col.new_note(model)
            note.fields = [parser.html_from_markdown(f) for f in fc.fields()]
            col.addNote(note)
            output_lockfile.notes[fc.id()] = LockedNote(mid=model["id"], nid=note.id)

    if len(locked_not_found) > 0:
        raise LockedNotFound(locked_not_found)

    return output_lockfile


def read_lockfile(lockfile_path: Path) -> Lockfile | None:
    try:
        text = lockfile_path.read_text()
    except FileNotFoundError:
        return None
    obj = json.loads(text)
    notes = {
        id: LockedNote(nid=n["nid"], mid=n["mid"]) for id, n in obj["notes"].items()
    }
    result = Lockfile(profile=obj["profile"], deck=obj["deck"], notes=notes)
    return result


def do_main(col: Collection, lockfile_path: Path, profile: str, deck: str):
    markdown = Path("flashcards.md").read_text()
    lockfile = read_lockfile(lockfile_path)
    flashcards = parser.flashcards_from_markdown(markdown)
    new_locked_ids = import_flashcards(col, flashcards, lockfile, profile, deck)
    d = dataclasses.asdict(new_locked_ids)
    js = json.dumps(d, indent=2, sort_keys=True)
    lockfile_path.write_text(js)


def main() -> int:
    base_path = Path.home() / "Library/Application Support/Anki2"
    lockfile_path = Path("flashcards.lock")
    last_loaded_profile = get_last_loaded_profile(base_path)
    collection_db_path = base_path / last_loaded_profile / "collection.anki2"
    col = Collection(str(collection_db_path))
    exitcode = 0
    try:
        do_main(col, lockfile_path, last_loaded_profile, "imported-from-markdown")
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
