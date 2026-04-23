from __future__ import annotations

import os
import pickle
import re
import sqlite3
from contextlib import contextmanager, redirect_stdout
from dataclasses import dataclass
from io import TextIOWrapper
from pathlib import Path
from types import TracebackType
from typing import Any, Generator, Self

cfg = {}


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
            console.print("[red]Dupe detected: note was not added!")
            console.print(f"First field: {field_name}")
            console.print(f"First value: {field_value}")

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
                            console.print(
                                f"[red]Error: Cannot use {k} in note when external-ids mode is active.[/red]"
                            )
                            raise Abort()
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


@contextmanager
def suppress_stdout() -> Generator[TextIOWrapper, Any, Any]:
    """A context manager that redirects stdout to devnull"""
    with open(os.devnull, "w", encoding="utf8") as fnull:
        with redirect_stdout(fnull) as out:
            yield out


class Anki:
    """My Anki collection wrapper class."""

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

        with suppress_stdout():
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
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        if self.modified:
            if cfg["auto_sync"]:
                self.sync()
            else:
                console.print("Database was modified.")
                if self._profile is not None and self._profile["syncKey"]:
                    console.print("[blue]Remember to sync!")

        self.col.close()

    def sync(self) -> None:
        """Sync collection to AnkiWeb"""
        from anki.sync import SyncAuth

        if self._profile is None:
            return

        hkey = self._profile.get("syncKey")
        if not hkey:
            return

        auth = SyncAuth(
            hkey=hkey,
            endpoint=self._profile.get("currentSyncUrl")
            or self._profile.get("customSyncUrl")
            or None,
            io_timeout_secs=self._profile.get("networkTimeout") or 30,
        )

        with Progress(
            TextColumn(
                "Syncing {task.fields[name]} [green]…[/green] {task.description}"
            ),
            SpinnerColumn(spinner_name="point", finished_text=""),
            console=console,
        ) as progress:
            t1 = progress.add_task("", total=None, name="deck")
            t2 = progress.add_task("", total=None, name="media")

            # Perform main sync
            with suppress_stdout():
                _ = self.col.sync_collection(auth, True)
            progress.update(t1, total=1, completed=1, description="[green]done!")

            # Perform media sync
            with cd(self.col.media.dir()):
                status_str = ""
                self.col.sync_media(auth)
                try:
                    while True:
                        time.sleep(0.01)
                        status = self.col.media_sync_status()
                        if p := status.progress:
                            status_str = f"{p.added}, {p.removed}, {p.checked}".lower()
                            progress.update(t2, description=f"[blue]({status_str})")
                        if not status.active:
                            break

                except Exception as error:
                    if "sync cancelled" in str(error):
                        progress.update(
                            t2,
                            total=1,
                            completed=1,
                            description="[yellow]cancelled!",
                        )
                        return
                    raise Abort() from error

                progress.update(
                    t2,
                    total=1,
                    completed=1,
                    description=f"[blue]({status_str}) [green]done!",
                )

    def check_media(self) -> None:
        """Check media (will rebuild missing LaTeX files)"""
        from anki.notes import NoteId

        with cd(self.col.media.dir()):
            with Progress(
                TextColumn("{task.description}"),
                SpinnerColumn(spinner_name="point", finished_text=""),
                console=console,
            ) as progress:
                t1 = progress.add_task("Checking media DB [green]… ", total=None)
                output = self.col.media.check()
                progress.update(
                    t1,
                    total=1,
                    completed=1,
                    description="Checking media DB [green]… done!",
                )

            if len(output.missing) + len(output.unused) == 0:
                console.print("[white]No unused or missing files found.")
                return

            for file in output.missing:
                console.print(f"[red]Missing: {file}")

            if len(output.missing) > 0 and console.confirm("Render missing LaTeX?"):
                out = self.col.media.render_all_latex()
                if out is not None:
                    nid = NoteId(out[0])
                    console.print(f"[red]Error processing note: {nid}")

                    if console.confirm("Review note?"):
                        note = Note(self, self.col.get_note(nid))
                        _ = note.review()

            for file in output.unused:
                console.print(f"[red]Unused: {file}")

            if len(output.unused) > 0 and console.confirm("Delete unused media?"):
                for file in output.unused:
                    if os.path.isfile(file):
                        os.remove(file)

    def find_notes(self, query: str) -> Generator[Note, None, None]:
        """Find notes in Collection and return Note objects"""
        return (
            Note(self, self.col.get_note(i)) for i in set(self.col.find_notes(query))
        )

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

    def rename_model(self, old_model_name: str, new_model_name: str) -> None:
        """Rename a model"""
        model = self.get_model(old_model_name)
        if not model:
            console.print("Can't rename model!")
            console.print(f"No such model: {old_model_name}")
            raise Abort()

        # Change the name
        model["name"] = new_model_name

        # Update local storage
        self.model_name_to_id = {m["name"]: m["id"] for m in self.col.models.all()}
        self.model_names = list(self.model_name_to_id.keys())

        # Save changes
        _ = self.col.models.update_dict(model)
        self.modified = True

    def list_tags(self, sort_by_count: bool = False, simple: bool = False) -> None:
        """List all tags"""
        if sort_by_count:

            def sorter(x: tuple[str, int]) -> str | int:
                return x[1]
        else:

            def sorter(x: tuple[str, int]) -> str | int:
                return x[0]

        tags = [(t, len(self.col.find_notes(f"tag:{t}"))) for t in self.col.tags.all()]

        if simple:
            for t, _ in tags:
                console.print(t)
        else:
            table = Table(show_edge=False, box=None, header_style="bold white")
            table.add_column("tag", style="cyan")
            table.add_column("notes", style="magenta", justify="right")

            for tag, n in sorted(tags, key=sorter):
                table.add_row(tag, str(n))

            console.print(table)

    def change_tags(self, query: str, tags: str, add: bool = True) -> None:
        """Add/Remove tags from notes that match query"""
        note_ids = self.col.find_notes(query)
        if add:
            _ = self.col.tags.bulk_add(note_ids, tags)
        else:
            _ = self.col.tags.bulk_remove(note_ids, tags)

        self.modified = True

    def purge_unused_tags(self) -> OpChangesWithCount:
        """Purge all unused tags"""
        return self.col.tags.clear_unused_tags()

    def edit_model_css(self, model_name: str) -> None:
        """Edit the CSS part of a given model."""
        model = self.get_model(model_name)
        if not model:
            console.print(f"Could not find model: {model_name}!")
            return

        with tempfile.NamedTemporaryFile(
            mode="w+", prefix="_apy_edit_", suffix=".css", delete=False
        ) as tf:
            _ = tf.write(model["css"])
            tf.flush()

            retcode = edit_file(tf.name)
            if retcode != 0:
                console.print(f"Editor return with exit code {retcode}!")
                return

            with open(tf.name, "r", encoding="utf8") as f:
                new_content = f.read()

        if model["css"] != new_content:
            model["css"] = new_content
            self.col.models.save(model, templates=True)
            self.modified = True

    def list_note_questions(self, query: str) -> None:
        """List first card questions for notes that match a query"""
        for note in self.find_notes(query):
            cards.print_question(note.n.cards()[0])

    def list_notes(
        self, query: str, show_cards: bool, show_raw_fields: bool, verbose: bool
    ) -> None:
        """List notes that match a query"""
        for note in self.find_notes(query):
            note.pprint(
                print_raw=show_raw_fields, list_cards=show_cards, verbose=verbose
            )

    def list_cards(self, query: str, verbose: bool) -> None:
        """List cards that match a query"""
        for cid in self.col.find_cards(query):
            cards.card_pprint(self.col.get_card(cid), verbose)

    def list_cards_as_table(self, query: str, opts_display: dict[str, bool]) -> None:
        """List cards that match a query in tabular format"""
        width = console.width - 1
        if opts_display.get("show_cid", False):
            width -= 15
        if opts_display.get("show_due", False):
            width -= 6
        if opts_display.get("show_type", False):
            width -= 9
        if opts_display.get("show_ease", False):
            width -= 5
        if opts_display.get("show_lapses", False):
            width -= 5
        if opts_display.get("show_model", False):
            width -= 25
        if opts_display.get("show_answer", False):
            width //= 2
            width -= 1
        if opts_display.get("show_deck", False):
            width -= 25

        table = Table(box=None, header_style="bold white")
        table.add_column("question")
        if opts_display.get("show_answer", False):
            table.add_column("answer")
        if opts_display.get("show_cid", False):
            table.add_column("cid", min_width=13)
        if opts_display.get("show_due", False):
            table.add_column("due", min_width=4)
        if opts_display.get("show_type", False):
            table.add_column("type", min_width=8)
        if opts_display.get("show_ease", False):
            table.add_column("ease", min_width=3)
        if opts_display.get("show_lapses", False):
            table.add_column("lapses", min_width=3)
        if opts_display.get("show_model", False):
            table.add_column("model", min_width=10)
        if opts_display.get("show_deck", False):
            table.add_column("deck", min_width=10)

        for cid in self.col.find_cards(query):
            card = self.col.get_card(cid)
            question, answer = cards.card_fields_as_md(
                card, one_line=True, max_width=width
            )

            row: list[str | Text | Markdown] = [Markdown(question)]
            if opts_display.get("show_answer", False):
                row += [Markdown(answer)]
            if opts_display.get("show_cid", False):
                row += [str(card.id)]
            if opts_display.get("show_due", False):
                row += [str(card.due)]
            if opts_display.get("show_type", False):
                card_type = ["new", "learning", "review", "relearning"][int(card.type)]
                row += [card_type]
            if opts_display.get("show_ease", False):
                row += [str(int(card.factor / 10))]
            if opts_display.get("show_lapses", False):
                row += [str(card.lapses)]
            if opts_display.get("show_model", False):
                row += [card.note_type()["name"]]
            if opts_display.get("show_deck", False):
                deck_id = card.current_deck_id()
                row += [self.col.decks.name(deck_id)]
            table.add_row(*row)

        console.print(table)

    def list_models(self) -> None:
        """List available models"""
        for name in self.model_names:
            console.print(name)

    def add_notes_with_editor(
        self,
        tags: str = "",
        model_name: str | None = None,
        deck_name: str | None = None,
        template: Note | None = None,
        respect_note_ids: bool = True,
    ) -> list[Note]:
        """Add new notes to collection with editor"""
        if template:
            input_string = str(template)
        else:
            if model_name is None or model_name.lower() == "ask":
                model_name = choose(sorted(self.model_names), "Choose model:")

            if deck_name is None:
                deck_name = self.col.decks.current()["name"]
            elif deck_name.lower() == "ask":
                deck_name = choose(sorted(self.deck_names), "Choose deck:")

            input_strings = [f"model: {model_name}"]

            if self.n_decks > 1:
                input_strings += [f"deck: {deck_name}"]

            input_strings += [f"tags: {tags}"]

            if model_name not in cfg["markdown_models"]:
                input_strings += ["markdown: false"]

            input_strings += ["\n# Note\n"]

            model = self.set_model(model_name)
            input_strings += [
                x
                for y in [[f"## {field['name']}", ""] for field in model["flds"]]
                for x in y
            ]

            input_string = "\n".join(input_strings) + "\n"

        with tempfile.NamedTemporaryFile(
            mode="w+", prefix="apy_note_", suffix=".md", delete=False
        ) as tf:
            _ = tf.write(input_string)
            tf.flush()
            retcode = edit_file(tf.name)

            if retcode != 0:
                console.print(f"Editor return with exit code {retcode}!")
                return []

            return self.add_notes_from_file(tf.name, respect_note_ids=respect_note_ids)

    def add_notes_from_file(
        self,
        filename: str,
        tags: str = "",
        deck: str | None = None,
        update_origin_file: bool = False,
        respect_note_ids: bool = False,
        link_duplicates: bool = False,
    ) -> list[Note]:
        """Add notes from Markdown file

        Args:
            filename: Path to the markdown file containing notes
            tags: Additional tags to add to the notes
            deck: Default deck for notes without a deck specified
            update_origin_file: If True, update the original file with note IDs
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

        if update_origin_file and has_missing_nids:
            if len(external_ids_map) > 0:
                self._update_external_ids_file(
                    filename,
                    original_content,
                    external_ids_map,
                )
            else:
                self._update_file_with_note_ids(
                    filename,
                    original_content,
                    internal_ids_map,
                )

        return notes

    def _update_file_with_note_ids(
        self,
        filename: str,
        content: str,
        note_id_map: dict[int, str],
    ) -> None:
        """Update the original markdown file with note IDs

        This function adds nid: headers to notes in the file that don't have them.

        Args:
            filename: Path to the markdown file
            content: Original content of the file
            note_id_map: A dict from note index to note ids for notes that were
                         added/updated
        """
        # Find all '# Note' or similar headers in the file
        note_headers = re.finditer(r"^# .*$", content, re.MULTILINE)
        note_positions = [match.start() for match in note_headers]

        if not note_positions:
            return  # No notes found in file

        # Add an extra position at the end to simplify boundary handling
        note_positions.append(len(content))

        # Extract each note's section and check if it needs to be updated
        # Keep content before first '# Note'
        updated_content: list[str] = [content[0 : note_positions[0]]]
        for i in range(len(note_positions) - 1):
            start = note_positions[i]
            end = note_positions[i + 1]

            # Get the section for this note
            section = content[start:end]

            # Check if this section already has an nid
            if re.search(r"^nid:", section, re.MULTILINE):
                # Already has an ID, keep as is
                updated_content.append(section)
            else:
                # No ID, add the note ID from our updated notes
                # We need to find where to insert the ID line (after model, tags, etc.)
                lines = section.split("\n")

                # Find a good position to insert the ID (after model, tags, deck)
                insert_pos = 1  # Default: after the first line (the title)
                for j, line in enumerate(lines[1:], 1):
                    # Look for model:, tags:, deck: lines
                    if re.match(r"^(model|tag[s]?|deck|markdown|md):", line):
                        insert_pos = j + 1  # Insert after this line

                # If we have a note ID for this position, insert it
                if i in note_id_map:
                    lines.insert(insert_pos, f"nid: {note_id_map[i]}")
                    updated_content.append("\n".join(lines))
                else:
                    # Couldn't match this section to a note, keep unchanged
                    updated_content.append(section)

        # Write back the updated content
        with open(filename, "w", encoding="utf-8") as f:
            _ = f.write("".join(updated_content))

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

    def add_notes_from_list(
        self,
        parsed_notes: list[NoteData],
        tags: str = "",
        deck: str | None = None,
    ) -> list[Note]:
        """Add new notes to collection from note list (from parsed file)"""
        notes: list[Note] = []
        for note in parsed_notes:
            if note.deck is None:
                note.deck = deck
            note.tags = f"{tags} {note.tags}"
            notes.append(note.add_to_collection(self)[0])

        return notes

    def add_notes_single(
        self,
        field_values: list[str],
        markdown: bool,
        tags: str = "",
        model_name_in: str | None = None,
        deck: str | None = None,
    ) -> Note:
        """Add new note to collection from args"""
        model_name: str
        if model_name_in:
            model = self.set_model(model_name_in)
            model_name = model_name_in
        else:
            model = self.col.models.current(for_deck=False)
            model_name = model["name"]

        field_names: list[str] = [field["name"] for field in model["flds"]]
        fields = dict(zip(field_names, field_values))

        new_note = NoteData(model_name, tags, fields, markdown, deck)
        return new_note.add_to_collection(self)[0]


def _added_notes_postprocessing(
    a: Anki,
    notes: list[Note],
    action_word: Literal["Updated/added", "Added"],
) -> None:
    """Common postprocessing after 'apy add[-from-file]' or 'apy update-from-file'."""
    n_notes = len(notes)
    if n_notes == 0:
        console.print("No notes added or updated")
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
        console.print(f"{action_word} {n_notes} notes")

    for note in notes:
        cards = note.n.cards()
        if (n := len(cards)) == 1:
            console.print(f"* nid: {note.n.id} / cid: {cards[0].id}")
        else:
            console.print(f"* nid: {note.n.id} / with {n} cards:")
            for card in cards:
                console.print(f"  * cid: {card.id}")


def main():
    cfg["base_path"] = Path.home() / "Library/Application Support/Anki2"
    with Anki(**cfg) as a:
        notes = a.add_notes_from_file(
            str("flashcards.md"),
            update_origin_file=True,
            respect_note_ids=True,
        )
        _added_notes_postprocessing(a, notes, "Updated/added")


if __name__ == "__main__":
    main()
