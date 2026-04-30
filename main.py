from __future__ import annotations

import base64
import json
import os
import pickle
import re
import sqlite3
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

import markdown
from bs4 import BeautifulSoup, Tag
from markdown import Markdown
from markdown.extensions import Extension
from markdown.extensions.abbr import AbbrExtension
from markdown.extensions.codehilite import CodeHiliteExtension
from markdown.extensions.def_list import DefListExtension
from markdown.extensions.fenced_code import FencedCodeExtension
from markdown.extensions.footnotes import FootnoteExtension
from markdown.postprocessors import Postprocessor
from markdown.preprocessors import Preprocessor

import parser

cfg = {
    "markdown_pygments_style": None,
    "markdown_latex_mode": None,
}


class Note:
    """A Note wrapper class"""

    def __init__(self, anki: Anki, note: ANote) -> None:
        self.a: Anki = anki
        self.n: ANote = note
        note_type = note.note_type()
        if note_type:
            self.model_name: str = note_type["name"]
        else:
            self.model_name = "__invalid-note__"
        self.field_names: list[str] = list(self.n.keys())
        self.suspended: bool = any(c.queue == -1 for c in self.n.cards())

    def delete(self) -> None:
        """Delete the note"""
        self.a.delete_notes(self.n.id)

    def get_deck(self) -> str:
        """Return which deck the note belongs to"""
        return self.a.col.decks.name(self.n.cards()[0].did)

    def set_deck(self, deck: str) -> None:
        """Move note to deck"""
        newdid = self.a.col.decks.id(deck)
        cids = [c.id for c in self.n.cards()]

        if cids and newdid:
            _ = self.a.col.set_deck(cids, newdid)
            self.a.modified = True

    def get_tag_string(self) -> str:
        """Get tag string"""
        return ", ".join(self.n.tags)


@dataclass
class NoteData:
    """Dataclass to contain data for a single note"""

    model: str
    tags: str
    fields: dict[str, str]
    markdown: bool = True
    deck: str | None = None
    nid: str | None = None
    external_id: str | None = None

    def add_to_collection(self, anki: Anki) -> NoteAddResult:
        """Add note to collection

        Returns: The new note
        """
        model = anki.set_model(self.model)
        model_field_names: list[str] = [field["name"] for field in model["flds"]]
        if len(model_field_names) != len(self.fields):
            console.print(f"Error: Not enough fields for model {self.model}!")
            anki.modified = False
            raise Abort()

        field_names = [x.replace(" (markdown)", "") for x in self.fields.keys()]
        for x, y in zip(model_field_names, field_names):
            if x != y:
                console.print(f"Warning: Inconsistent field names ({x} != {y})")

        notetype = anki.col.models.current(for_deck=False)
        new_note = anki.col.new_note(notetype)

        note_type = new_note.note_type()
        if self.deck is not None and note_type is not None:
            note_type["did"] = anki.deck_name_to_id[self.deck]

        new_note.fields = [
            convert_text_to_field(f, self.markdown) for f in self.fields.values()
        ]

        for tag in self.tags.strip().split():
            new_note.add_tag(tag)

        check_duplicate = new_note.duplicate_or_empty()
        if check_duplicate == 2:
            field_name, field_value = list(self.fields.items())[0]
            print("[red]Dupe detected: note was not added!")
            print(f"First field: {field_name}")
            print(f"First value: {field_value}")

            # Find the duplicate note
            nids = anki.col.find_notes(f'{field_name}:"{field_value}"')
            if len(nids) == 1:
                existing_note = anki.col.get_note(nids[0])
                return (Note(anki, existing_note), "duplicate")

            return (Note(anki, new_note), "duplicate")

        _ = anki.col.addNote(new_note)
        anki.modified = True
        return (Note(anki, new_note), "added")

    def update_or_add_to_collection(self, anki: Anki) -> NoteAddResult:
        """Update existing note in collection if ID is provided, otherwise add as new

        Returns: The updated or new note
        """
        if self.nid:
            try:
                from anki.notes import NoteId

                note_id = NoteId(int(self.nid))
                existing_note = anki.col.get_note(note_id)
                return self._update_note(anki, existing_note)
            except (ValueError, TypeError):
                print(
                    f"[yellow]Invalid note ID format: {self.nid}. Will create a new note.[/yellow]"
                )
            except Exception as e:
                print(
                    f"[yellow]Note with ID {self.nid} not found: {e}. Will create a new note.[/yellow]"
                )

        return self.add_to_collection(anki)

    def _update_note(self, anki: Anki, existing_note: Any) -> NoteAddResult:
        """Update an existing note with new field values

        Returns: The updated note
        """
        # Verify model match
        note_type = existing_note.note_type()
        if note_type and note_type["name"] != self.model:
            console.print(
                f"[yellow]Warning: Model mismatch. File specifies '{self.model}', note has '{note_type['name']}'.[/yellow]"
            )
            if not console.confirm("Continue with update anyway?"):
                console.print(
                    "[yellow]Update canceled. Adding as new note instead.[/yellow]"
                )
                return self.add_to_collection(anki)

        # Update tags
        existing_note.tags = self.tags.strip().split()

        # Update deck if specified
        if self.deck is not None:
            try:
                # Get first card and update its deck
                cards = existing_note.cards()
                if cards:
                    # Explicitly cast to int to satisfy mypy
                    deck_id = anki.deck_name_to_id.get(self.deck, None)
                    if deck_id is not None:  # Make sure deck_id exists and is not None
                        card_ids = [c.id for c in cards]
                        _ = anki.col.set_deck(card_ids, deck_id)
            except Exception as e:
                console.print(f"[yellow]Failed to update deck: {e}[/yellow]")

        # Update fields
        field_names = list(existing_note.keys())
        for i, field_name in enumerate(field_names):
            # Match field names from the file to the existing note
            matching_field = None
            for file_field_name, content in self.fields.items():
                clean_name = file_field_name.replace(" (markdown)", "")
                if clean_name.lower() == field_name.lower():
                    matching_field = content
                    break

            if matching_field is not None:
                existing_note.fields[i] = convert_text_to_field(
                    matching_field,
                    self.markdown,
                )

        # Save the updated note
        _ = anki.col.update_note(existing_note)
        anki.modified = True

        return (Note(anki, existing_note), "updated")


def markdown_file_to_notes(filename: str) -> list[NoteData]:
    """Parse note data from a Markdown file"""
    try:
        notes = [
            NoteData(
                model=x["model"],
                tags=x["tags"],
                fields=x["fields"],
                markdown=x["markdown"],
                deck=x["deck"],
                nid=x["nid"],
                external_id=x.get("external_id"),
            )
            for x in _parse_markdown_file(filename)
        ]
    except KeyError as e:
        console.print(f"Error {e.__class__} when parsing {filename}!")
        console.print("This may typically be due to bad Markdown formatting.")
        raise Abort() from e

    return notes


def _parse_markdown_file(filename: str) -> list[dict[str, Any]]:
    defaults: dict[str, Any] = {
        "model": "Basic",
        "markdown": True,
        "tags": "",
        "deck": None,
        "nid": None,
        "external_ids_file": None,
    }
    with open(filename, "r", encoding="utf8") as f:
        for line in f:
            match = re.match(r"#+\s*.*", line)
            if match:
                break

            match = re.match(r"([\w-]+): (.*)", line)
            if match:
                k, v = match.groups()
                k = k.lower()
                v = v.strip()
                if k in ("tag", "tags"):
                    defaults["tags"] = v.replace(",", "")
                elif k in ("markdown", "md"):
                    defaults["markdown"] = v in ("true", "yes")
                elif k == "nid":
                    defaults["nid"] = v
                elif k == "external-ids":
                    defaults["external_ids_file"] = v
                else:
                    defaults[k] = v

    external_ids_map: dict[str, dict[str, Any]] = {}
    if defaults["external_ids_file"]:
        ids_file_path = Path(filename).parent / defaults["external_ids_file"]
        if ids_file_path.exists():
            with open(ids_file_path, "r", encoding="utf8") as f:
                external_ids_map = json.load(f)

    notes: list[dict[str, Any]] = []
    current_note: dict[str, Any] = {}
    current_field: str | None = None
    is_in_codeblock = False

    if defaults["external_ids_file"] and defaults["nid"]:
        console.print(
            "[red]Error: Cannot use nid in file header when external-ids is set.[/red]"
        )
        raise Abort()

    with open(filename, "r", encoding="utf8") as f:
        for line in f:
            if is_in_codeblock:
                if current_field is not None:
                    current_note["fields"][current_field] += line
                match = re.match(r"```\s*$", line)
                if match:
                    is_in_codeblock = False
                continue

            match = re.match(r"```\w*\s*$", line)
            if match:
                is_in_codeblock = True
                if current_field is not None:
                    current_note["fields"][current_field] += line
                continue

            if current_note and current_field is None:
                match = re.match(r"(\w+): (.*)", line)
                if match:
                    k, v = match.groups()
                    k = k.lower()
                    v = v.strip()
                    if k in ("tag", "tags"):
                        current_tags = current_note.get("tags", "").strip()
                        if current_tags:
                            current_note["tags"] = (
                                f"{current_tags} {v.replace(',', '')}"
                            )
                        else:
                            current_note["tags"] = v.replace(",", "")
                    elif k in ("markdown", "md"):
                        current_note["markdown"] = v in ("true", "yes")
                    elif k == "id":
                        current_note["external_id"] = v
                        if defaults["external_ids_file"]:
                            if v in external_ids_map:
                                current_note["nid"] = str(external_ids_map[v])
                    elif k == "nid":
                        if defaults["external_ids_file"]:
                            print(
                                f"[red]Error: Cannot use {k} in note when external-ids mode is active.[/red]"
                            )
                            raise SystemExit()
                        current_note[k] = v
                    else:
                        current_note[k] = v

            match = re.match(r"(#+)\s*(.*)", line)
            if not match:
                if current_field is not None:
                    current_note["fields"][current_field] += line
                continue

            level, title = match.groups()

            if len(level) == 1:
                if current_note and current_field is not None:
                    current_note["fields"][current_field] = current_note["fields"][
                        current_field
                    ].strip()
                    notes.append(current_note)

                current_note = {"title": title, "fields": {}, **defaults}
                current_field = None
                if defaults["external_ids_file"] and not current_note.get(
                    "external_id"
                ):
                    current_note["external_id"] = str(uuid.uuid4())
                continue

            if len(level) == 2:
                if current_field is not None:
                    current_note["fields"][current_field] = current_note["fields"][
                        current_field
                    ].strip()

                if title in current_note["fields"]:
                    console.print(f"Error when parsing {filename}!")
                    raise Abort()

                current_field = title
                current_note["fields"][current_field] = ""

    # Add remaining note to list
    if current_note and current_field is not None:
        current_note["fields"][current_field] = current_note["fields"][
            current_field
        ].strip()
        notes.append(current_note)

    return notes


class Anki:
    def __init__(
        self,
        base_path: str | None = None,
        collection_db_path: str | None = None,
        profile_name: str | None = None,
        **_kwargs: dict[str, Any],
    ):
        self.modified: bool = False

        self._meta: Any = None
        self._collection_db_path: str = ""
        self._profile_name: str = profile_name or ""
        self._profile: dict[Any, Any] | None = None

        self._init_load_profile(base_path, collection_db_path)
        self._init_load_collection()
        self._init_load_config()

        self.today: int = self.col.sched.today

        self.model_name_to_id: dict[str, int] = {
            m["name"]: m["id"] for m in self.col.models.all()
        }
        self.model_names: list[str] = list(self.model_name_to_id.keys())

        self.deck_name_to_id: dict[str, int] = {
            d["name"]: d["id"] for d in self.col.decks.all()
        }
        self.deck_names: KeysView[str] = self.deck_name_to_id.keys()
        self.n_decks: int = len(self.deck_names)

    def _init_load_profile(
        self, base_path_str: str | None, collection_db_path: str | None
    ) -> None:
        """Load the Anki profile from database"""
        if base_path_str is None:
            if collection_db_path:
                self._collection_db_path = str(Path(collection_db_path).absolute())
                return

            print("Base path is not properly set!")
            raise SystemExit()

        base_path = Path(base_path_str)
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
            self._meta = pickle.loads(res.fetchone()[0])

            profiles = conn.execute(
                "select name, cast(data as blob) from profiles where name != '_global'"
            ).fetchall()
        finally:
            conn.close()

        profiles_dict = {name: pickle.loads(data) for name, data in profiles}

        if not self._profile_name:
            self._profile_name = self._meta.get(
                "last_loaded_profile_name", profiles[0][0]
            )

        self._collection_db_path = str(
            base_path / self._profile_name / "collection.anki2"
        )
        self._profile = profiles_dict[self._profile_name]

    def _init_load_collection(self) -> None:
        """Load the Anki collection"""
        from anki.collection import Collection
        from anki.errors import DBError

        # Save CWD (because Anki changes it)
        save_cwd = os.getcwd()

        try:
            self.col: Collection = Collection(self._collection_db_path)
        except AssertionError as error:
            console.print("Path to database is not valid!")
            console.print(f"path = {self._collection_db_path}")
            raise Abort() from error
        except DBError as error:
            console.print("Database is NA/locked!")
            raise Abort() from error

        # Restore CWD (because Anki changes it)
        os.chdir(save_cwd)

    @staticmethod
    def _init_load_config() -> None:
        """Load custom configuration"""
        from anki import latex

        # Update LaTeX commands
        # * Idea based on Anki addon #1546037973 ("Edit LaTeX build process")
        if "pngCommands" in cfg:
            latex.pngCommands = cfg["pngCommands"]
        if "svgCommands" in cfg:
            latex.svgCommands = cfg["svgCommands"]

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        _exc_type,
        _exc_val,
        _exc_tb,
    ) -> None:
        if self.modified:
            print("Database was modified.")
            if self._profile is not None and self._profile["syncKey"]:
                print("[blue]Remember to sync!")

        self.col.close()

    def delete_notes(self, ids: NoteId | list[NoteId]) -> None:
        """Delete notes by note ids"""
        if not isinstance(ids, list):
            ids = [ids]

        _ = self.col.remove_notes(ids)
        self.modified = True

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

    def add_notes_from_file(
        self,
        filename: str,
        tags: str = "",
        deck: str | None = None,
        respect_note_ids: bool = False,
        link_duplicates: bool = False,
    ) -> list[Note]:
        """Add notes from Markdown file

        Args:
            filename: Path to the markdown file containing notes
            tags: Additional tags to add to the notes
            deck: Default deck for notes without a deck specified
            respect_note_ids: If True, then this function looks for nid: or cid: headers
                              in the file to determine if a note should be updated
                              rather than added.
            link_duplicates: If True, when a duplicate is detected, find the existing
                             note and update the IDs file with its nid.

        Returns:
            List of notes that were updated or added
        """
        with open(filename, "r", encoding="utf-8") as f:
            original_content = f.read()

        has_missing_nids: bool = False
        notes: list[Note] = []
        external_ids_map: dict[str, str] = {}
        internal_ids_map: dict[int, str] = {}

        for idx, note_data in enumerate(markdown_file_to_notes(filename)):
            if tags:
                note_data.tags = f"{tags} {note_data.tags}"

            if deck and not note_data.deck:
                note_data.deck = deck

            has_missing_nids |= note_data.nid is None

            if respect_note_ids:
                note = note_data.update_or_add_to_collection(self)
            else:
                note = note_data.add_to_collection(self)

            if note[1] == "duplicate" and not link_duplicates:
                continue

            notes.append(note[0])

            nid = str(note[0].n.id)
            if note_data.external_id:
                external_ids_map[note_data.external_id] = nid
            else:
                internal_ids_map[idx] = nid

        if has_missing_nids:
            if len(external_ids_map) > 0:
                self._update_external_ids_file(
                    filename,
                    original_content,
                    external_ids_map,
                )

        return notes

    def _update_external_ids_file(
        self, filename: str, content: str, external_ids_map: dict[str, str]
    ) -> None:
        """Update the external IDs JSON file with new note IDs

        This function updates the external IDs file when using external-ids mode.

        Args:
            filename: Path to the markdown file
            content: Original content of the file (unused, kept for signature consistency)
            external_ids_map: Dictionary mapping external IDs to NIDs
        """
        match = re.search(r"external-ids:\s*(.+)", content)
        if not match:
            return

        ids_filename = match.group(1).strip()
        ids_file_path = Path(filename).parent / ids_filename

        existing_ids: dict[str, str] = {}
        if ids_file_path.exists():
            with open(ids_file_path, "r", encoding="utf-8") as f:
                existing_ids = json.load(f)

        existing_ids.update(external_ids_map)

        with open(ids_file_path, "w", encoding="utf-8") as f:
            json.dump(existing_ids, f, indent=2)


def _added_notes_postprocessing(
    a: Anki,
    notes: list[Note],
    action_word: Literal["Updated/added", "Added"],
) -> None:
    """Common postprocessing after 'apy add[-from-file]' or 'apy update-from-file'."""
    n_notes = len(notes)
    if n_notes == 0:
        print("No notes added or updated")
        return

    decks = [a.col.decks.name(c.did) for n in notes for c in n.n.cards()]
    n_decks = len(set(decks))
    if n_decks == 0:
        console.print("No notes added or updated")
        return

    if a.n_decks > 1:
        if n_notes == 1:
            console.print(f"{action_word} note to deck: {decks[0]}")
        elif n_decks > 1:
            console.print(f"{action_word} {n_notes} notes to {n_decks} different decks")
        else:
            console.print(f"{action_word} {n_notes} notes to deck: {decks[0]}")
    else:
        print(f"{action_word} {n_notes} notes")

    for note in notes:
        cards = note.n.cards()
        if (n := len(cards)) == 1:
            print(f"* nid: {note.n.id} / cid: {cards[0].id}")
        else:
            console.print(f"* nid: {note.n.id} / with {n} cards:")
            for card in cards:
                console.print(f"  * cid: {card.id}")


def convert_text_to_field(text: str, use_markdown: bool) -> str:
    """Convert text to Anki field html."""
    if use_markdown:
        return _convert_markdown_to_field(text)

    # Convert newlines to <br> tags
    text = text.replace("\n", "<br />")
    return _clean_html(text)


def _convert_markdown_to_field(text: str) -> str:
    """Convert Markdown to field HTML"""

    # Return input text if it only contains allowed characters
    if re.fullmatch(r"[a-zA-Z0-9æøåÆØÅ ,.?+-]*", text):
        return text

    # Prepare original markdown for restoring
    # Note: convert newlines to <br> to make text readable in the Anki viewer
    original_encoded = base64.b64encode(text.replace("\n", "<br />").encode()).decode()

    # For convenience: Escape some common LaTeX constructs
    text = text.replace(r"\\", r"\\\\")
    text = text.replace(r"\{", r"\\{")
    text = text.replace(r"\}", r"\\}")
    text = text.replace(r"*}", r"\*}")

    # Fix whitespaces in input
    text = text.replace("\xc2\xa0", " ").replace("\xa0", " ")

    # For convenience: Fix mathjax escaping
    text = text.replace(r"\[", r"\\[")
    text = text.replace(r"\]", r"\\]")
    text = text.replace(r"\(", r"\\(")
    text = text.replace(r"\)", r"\\)")

    html = markdown.markdown(
        text,
        extensions=[
            "tables",
            AbbrExtension(),
            CodeHiliteExtension(
                noclasses=True,
                linenums=False,
                pygments_style=cfg["markdown_pygments_style"],
                guess_lang=False,
            ),
            DefListExtension(),
            FencedCodeExtension(),
            FootnoteExtension(),
            MathProtectExtension(cfg["markdown_latex_mode"]),
        ],
        output_format="html",
    )

    # Parse HTML and attach original markdown
    soup = BeautifulSoup(html or "<div>&nbsp;</div>", "html.parser")
    root = soup.find()
    if isinstance(root, Tag):
        root["data-original-markdown"] = original_encoded

    return str(soup)


def _clean_html(text: str) -> str:
    """Clean up html text"""
    text = text.replace(r"&lt;", "<")
    text = text.replace(r"&gt;", ">")
    text = text.replace(r"&amp;", "&")
    text = text.replace(r"&nbsp;", " ")
    text = re.sub(r"\<b\>\s*\<\/b\>", "", text)
    text = re.sub(r"\<i\>\s*\<\/i\>", "", text)
    text = re.sub(r"\<div\>\s*\<\/div\>", "", text)
    return text.strip()


class MathProtectExtension(Extension):
    def __init__(self, markdown_latex_mode: str) -> None:
        super().__init__()
        self.markdown_latex_mode: str = markdown_latex_mode

    def extendMarkdown(self, md: Markdown) -> None:  # pyright: ignore[reportImplicitOverride]
        math_preprocessor = MathPreprocessor(md, self.markdown_latex_mode)
        math_postprocessor = MathPostprocessor(md, math_preprocessor.placeholders)

        md.preprocessors.register(math_preprocessor, "math_block_processor", 25)
        md.postprocessors.register(math_postprocessor, "math_block_restorer", 25)


class MathPreprocessor(Preprocessor):
    def __init__(self, md: Markdown, markdown_latex_mode: str) -> None:
        super().__init__(md)
        self.counter: int = 0
        self.placeholders: dict[str, str] = {}

        # Apply latex translation based on specified latex mode
        if markdown_latex_mode == "latex":
            self.fmt_display: str = "[$$]{math}[/$$]"
            self.fmt_inline: str = "[$]{math}[/$]"
        else:
            self.fmt_display = r"\[{math}\]"
            self.fmt_inline = r"\({math}\)"

    def run(self, lines: list[str]) -> list[str]:  # pyright: ignore[reportImplicitOverride]
        def replacer(match: re.Match[str]) -> str:
            placeholder = f"MATH-PLACEHOLDER-{self.counter}"
            self.counter += 1

            if matched := match.group(1):
                self.placeholders[placeholder] = self.fmt_display.format(math=matched)
            elif matched := match.group(2):
                self.placeholders[placeholder] = self.fmt_inline.format(math=matched)

            return placeholder

        pattern = re.compile(r"\$\$(.*?)\$\$|\$(.*?)\$", re.DOTALL)
        lines_joined = "\n".join(lines)
        lines_processed = pattern.sub(replacer, lines_joined)
        return lines_processed.split("\n")


class MathPostprocessor(Postprocessor):
    def __init__(self, md: Markdown, placeholders: dict[str, str]) -> None:
        super().__init__(md)
        self.placeholders: dict[str, str] = placeholders

    def run(self, text: str) -> str:  # pyright: ignore[reportImplicitOverride]
        for placeholder, math in self.placeholders.items():
            text = text.replace(placeholder, math)
        return text


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
            existing_note.fields = [convert_text_to_field(f, True) for f in fc.fields()]
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
            new_note.fields = [convert_text_to_field(f, True) for f in fc.fields()]
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
    cfg["base_path"] = Path.home() / "Library/Application Support/Anki2"
    if False:
        with Anki(**cfg) as a:
            notes = a.add_notes_from_file(
                str("flashcards-apy.md"),
                respect_note_ids=True,
            )
            _added_notes_postprocessing(a, notes, "Updated/added")
    else:
        with Anki(**cfg) as anki:
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
