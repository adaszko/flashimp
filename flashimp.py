#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "anki>=25.9.2",
#     "mistletoe>=1.5.1",
# ]
# ///
from __future__ import annotations

import argparse
import dataclasses
import json
import sqlite3
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import List, Protocol, cast

import mistletoe
from anki.collection import Collection
from anki.errors import NotFoundError
from anki.notes import Note, NoteId
from mistletoe import Document
from mistletoe.block_token import Heading, Paragraph, ThematicBreak

PROFILE_DEFAULT = "flashimp"


class UnknownModel(RuntimeError):
    def __init__(self, message):
        self.message = message
        super().__init__(self.message)


class LockedNotFound(RuntimeError):
    def __init__(self, message):
        self.message = message
        super().__init__(self.message)


class MODEL(StrEnum):
    BASIC = "Basic"
    CLOZE = "Cloze"


class Flashcard(Protocol):
    # Model name in Anki
    def model(self) -> MODEL: ...

    # Unique ID given by human
    def human_given_id(self) -> str: ...

    # Respective field values for the given model
    def fields(self) -> list[str]: ...


class Basic(Flashcard):
    def __init__(self, human_given_id: str, front: str, back: str):
        self._id = human_given_id
        self._front = front
        self._back = back

    def model(self) -> MODEL:
        return MODEL.BASIC

    def fields(self) -> list[str]:
        return [self._front, self._back]

    def human_given_id(self) -> str:
        return self._id

    def front(self) -> str:
        return self._front

    def back(self) -> str:
        return self._back


class Cloze(Flashcard):
    def __init__(self, human_given_id: str, text: str, back_extra: str = ""):
        self._id = human_given_id
        self._text = text
        self._back_extra = back_extra

    def model(self) -> MODEL:
        return MODEL.CLOZE

    def fields(self) -> list[str]:
        return [self._text, self._back_extra]

    def human_given_id(self) -> str:
        return self._id

    def text(self) -> str:
        return self._text

    def back_extra(self) -> str:
        return self._back_extra


@dataclass
class LockedNote:
    mid: int
    nid: int


@dataclass
class Lockfile:
    profile: str
    deck: str
    notes: dict[str, LockedNote]  # id -> (nid, notetypeid)


class Action(Protocol):
    def apply(self, col: Collection, lockfile: Lockfile): ...
    def __str__(self) -> str: ...


class ActionAdd(Action):
    def __init__(self, human_given_id: str, model_id: int, note: Note):
        self.human_given_id = human_given_id
        self.model_id = model_id
        self.note = note

    def __str__(self) -> str:
        return f"ADD {self.human_given_id} {self.note.items()}"

    def apply(self, col: Collection, lockfile: Lockfile):
        col.addNote(self.note)
        lockfile.notes[self.human_given_id] = LockedNote(
            mid=self.model_id, nid=self.note.id
        )


class ActionUpdate(Action):
    def __init__(self, human_given_id: str, note: Note):
        self.human_given_id = human_given_id
        self.note = note

    def __str__(self) -> str:
        return f"UPDATE {self.human_given_id} {self.note.items()}"

    def apply(self, col: Collection, lockfile: Lockfile):
        lockfile  # silence unused var warning
        col.update_note(self.note)


def _token_text(token) -> str:
    if hasattr(token, "children") and token.children:
        return "".join(_token_text(c) for c in token.children)
    if hasattr(token, "content"):
        return token.content
    return ""


def flashcards_from_markdown(markdown: str) -> List[Flashcard]:
    doc = Document(markdown)

    if doc.children is None:
        return []

    # Group document children into per-card buckets: (heading, [body tokens])
    groups: list[tuple] = []
    for token in doc.children:
        if isinstance(token, Heading) and token.level == 1:
            groups.append((token, []))
        elif groups:
            groups[-1][1].append(token)

    flashcards = []
    for heading, body in groups:
        card_id = _token_text(heading)
        break_idx = next(
            (i for i, t in enumerate(body) if isinstance(t, ThematicBreak)), None
        )
        if break_idx is not None:
            front = "\n".join(
                _token_text(t) for t in body[:break_idx] if isinstance(t, Paragraph)
            )
            back = "\n".join(
                _token_text(t)
                for t in body[break_idx + 1 :]
                if isinstance(t, Paragraph)
            )
            flashcards.append(Basic(card_id, front, back))
        else:
            text = "\n".join(_token_text(t) for t in body if isinstance(t, Paragraph))
            flashcards.append(Cloze(card_id, text))

    return flashcards


def html_from_markdown(markdown: str) -> str:
    return mistletoe.markdown(markdown)


def get_profiles(base_path: Path) -> list[str]:
    prefs_db_path = base_path / "prefs21.db"

    if not prefs_db_path.exists():
        raise FileNotFoundError(prefs_db_path)

    # Load metadata and profiles from database
    conn = sqlite3.connect(prefs_db_path)
    try:
        profiles = conn.execute(
            "select name from profiles where name != '_global'"
        ).fetchall()
    finally:
        conn.close()

    return [p[0] for p in profiles]


def plan(
    col: Collection,
    flashcards: list[Flashcard],
    deck_id: int,
    locked_notes: dict[str, LockedNote],
) -> list[Action]:
    actions = []

    locked_not_found = {}
    for fc in flashcards:
        human_given_id = fc.human_given_id()
        if human_given_id in locked_notes:
            note_id = locked_notes[human_given_id].nid
            try:
                note = col.get_note(NoteId(note_id))
            except NotFoundError:
                locked_not_found[human_given_id] = note_id
                continue
            updated_fields = [html_from_markdown(f) for f in fc.fields()]
            if note.fields == updated_fields:
                continue
            note.fields = updated_fields
            action = ActionUpdate(human_given_id, note)
        else:
            model = col.models.by_name(fc.model())
            if model is None:
                raise UnknownModel(fc.model())
            assert len(model["flds"]) == len(fc.fields()), (
                model["flds"],
                fc.fields(),
            )
            note = col.new_note(model)
            note_type = note.note_type()
            assert note_type is not None, note_type
            note_type["did"] = deck_id
            note.fields = [html_from_markdown(f) for f in fc.fields()]
            action = ActionAdd(human_given_id, model["id"], note)
        actions.append(action)

    if len(locked_not_found) > 0:
        raise LockedNotFound(locked_not_found)

    return actions


def apply(
    col: Collection,
    actions: list[Action],
    input_lockfile: Lockfile | None,
    initial_profile: str,
    initial_deck: str,
) -> Lockfile:
    if input_lockfile is None:
        output_lockfile = Lockfile(profile=initial_profile, deck=initial_deck, notes={})
    else:
        output_lockfile = Lockfile(
            profile=input_lockfile.profile,
            deck=input_lockfile.deck,
            notes=input_lockfile.notes.copy(),
        )

    for action in actions:
        action.apply(col, output_lockfile)

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


def do_main(
    markdown_file_path: Path,
    col: Collection,
    lockfile_path: Path,
    lockfile: Lockfile | None,
    initial_profile: str,
    initial_deck: str,
):
    markdown = markdown_file_path.read_text()
    flashcards = flashcards_from_markdown(markdown)
    if lockfile is None:
        locked_notes = {}
    else:
        locked_notes = lockfile.notes

    deck_id = col.decks.id(initial_deck, create=True)
    assert deck_id is not None
    actions = plan(col, flashcards, deck_id, locked_notes)

    if len(actions) == 0:
        print("No changes")
        return

    for a in actions:
        print(a)
    response = input("APPLY CHANGES (type YES to confirm)? ")
    if response != "YES":
        return

    new_lockfile = apply(col, actions, lockfile, initial_profile, initial_deck)

    if new_lockfile == lockfile:
        return

    d = dataclasses.asdict(new_lockfile)
    js = json.dumps(d, indent=2, sort_keys=True)
    lockfile_path.write_text(js)


def make_arg_parser(anki_dir: Path) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--anki",
        help=f"Anki base directory (defaults to {anki_dir})",
        type=Path,
        default=anki_dir,
    )
    parser.add_argument(
        "--profile",
        help=f"Anki profile name on first import (defaults to {PROFILE_DEFAULT})",
        default=PROFILE_DEFAULT,
    )
    parser.add_argument(
        "--deck",
        help="Anki deck name on first import (defaults to MARKDOWN_FILE basename)",
    )
    parser.add_argument(
        "--lockfile",
        help="Lockfile path",
        type=Path,
    )
    parser.add_argument(
        "MARKDOWN_FILE",
        type=Path,
        help="Markdown file containing flashcards",
        nargs="?",
    )
    parser.add_argument("--selftest", action="store_true")
    return parser


def selftest():
    import inspect

    current_module = sys.modules[__name__]
    fns = inspect.getmembers(current_module, inspect.isfunction)
    test_fns = [f for (name, f) in fns if name.startswith("test_")]
    for fn in test_fns:
        fn()


def main() -> int:
    anki_dir = Path.home() / "Library/Application Support/Anki2"

    parser = make_arg_parser(anki_dir)
    args = parser.parse_args()

    if args.selftest:
        return selftest()

    if args.MARKDOWN_FILE is None:
        parser.print_help()
        return 1

    if args.lockfile is None:
        lockfile_path = Path(
            "{}.lock".format(args.MARKDOWN_FILE.name.removesuffix(".md"))
        )
    else:
        lockfile_path = Path(args.lockfile)

    lockfile = read_lockfile(lockfile_path)
    if lockfile is None:
        if args.profile is None:
            print(
                "Lockfile does not exist, --profile is required; possible values:",
                get_profiles(args.anki),
            )
            return 1
    else:
        if args.profile != PROFILE_DEFAULT:
            print("warning: lockfile exists, ignoring --profile")
        args.profile = lockfile.profile
    if args.deck is None:
        args.deck = args.MARKDOWN_FILE.name.removesuffix(".md")

    collection_db_path = args.anki / args.profile / "collection.anki2"
    col = Collection(str(collection_db_path))

    exitcode = 0
    try:
        do_main(
            args.MARKDOWN_FILE,
            col,
            lockfile_path,
            lockfile,
            args.profile,
            args.deck,
        )
    except LockedNotFound as e:
        print("Flashcards exist in the lock file but not in the database")
        for id, nid in e.args[0].items():
            print(f"{id}: {nid}")
        print(f"Try deleting {lockfile_path} file")
        exitcode = 1
    finally:
        col.close()

    return exitcode


def test_flashcards_from_markdown():
    fcs = flashcards_from_markdown("""
# Id1
Front
***
Back
# Id2
foo {{c1::bar}} baz
""")

    assert len(fcs) == 2
    basic = fcs[0]
    assert basic.model() == MODEL.BASIC
    basic = cast(Basic, basic)
    assert basic.human_given_id() == "Id1"
    assert basic.front() == "Front"
    assert basic.back() == "Back"
    assert basic.fields() == ["Front", "Back"]

    cloze = fcs[1]
    assert cloze.model() == MODEL.CLOZE
    cloze = cast(Cloze, cloze)
    assert cloze.human_given_id() == "Id2"
    assert cloze.text() == "foo {{c1::bar}} baz"
    assert cloze.back_extra() == ""
    assert cloze.fields() == ["foo {{c1::bar}} baz", ""]


def test_html_from_markdown():
    html = html_from_markdown("""foo **bar** baz""")
    assert html.strip() == "<p>foo <strong>bar</strong> baz</p>"


def test_arg_parser():
    anki_dir = Path.home() / "Library/Application Support/Anki2"
    parser = make_arg_parser(anki_dir)
    args = parser.parse_args(["--profile", "experiments", "flashcards.md"])
    assert args.profile == "experiments"
    assert args.MARKDOWN_FILE == Path("flashcards.md")


if __name__ == "__main__":
    sys.exit(main())
