from __future__ import annotations

import base64
import json
import os
import pickle
import re
import sqlite3
import sys
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
