from enum import StrEnum
from typing import List, Protocol, cast

import tree_sitter_markdown
from tree_sitter import Language, Parser


class MODEL(StrEnum):
    BASIC = "Basic"
    CLOZE = "Cloze"


class Flashcard(Protocol):
    # Model name in Anki
    def model(self) -> MODEL: ...

    # Unique ID given by human
    def id(self) -> str: ...

    # Respective field values for the given model
    def fields(self) -> list[str]: ...


class Basic(Flashcard):
    def __init__(self, id: str, front: str, back: str):
        self._id = id
        self._front = front
        self._back = back

    def model(self) -> MODEL:
        return MODEL.BASIC

    def fields(self) -> list[str]:
        return [self._front, self._back]

    def id(self) -> str:
        return self._id

    def front(self) -> str:
        return self._front

    def back(self) -> str:
        return self._back


class Cloze(Flashcard):
    def __init__(self, id: str, text: str, back_extra: str = ""):
        self._id = id
        self._text = text
        self._back_extra = back_extra

    def model(self) -> MODEL:
        return MODEL.CLOZE

    def fields(self) -> list[str]:
        return [self._text, self._back_extra]

    def id(self) -> str:
        return self._id

    def text(self) -> str:
        return self._text

    def back_extra(self) -> str:
        return self._back_extra


def make_parser():
    lang = Language(tree_sitter_markdown.language())
    return Parser(lang)


def flashcards_from_markdown(markdown: str) -> List[Flashcard]:
    parser = make_parser()
    source = markdown.encode()
    tree = parser.parse(source)

    def node_text(node) -> str:
        return source[node.start_byte : node.end_byte].decode().strip()

    flashcards = []

    for section in tree.root_node.children:
        if section.type != "section":
            continue

        heading = next((c for c in section.children if c.type == "atx_heading"), None)
        if heading is None:
            continue

        inline = next((c for c in heading.children if c.type == "inline"), None)
        if inline is None:
            continue
        name = node_text(inline)

        body = [c for c in section.children if c.type != "atx_heading"]
        break_idx = next(
            (i for i, c in enumerate(body) if c.type == "thematic_break"), None
        )

        if break_idx is not None:
            front = "\n".join(
                node_text(c) for c in body[:break_idx] if c.type == "paragraph"
            )
            back = "\n".join(
                node_text(c) for c in body[break_idx + 1 :] if c.type == "paragraph"
            )
            flashcards.append(Basic(name, front, back))
        else:
            front = "\n".join(node_text(c) for c in body if c.type == "paragraph")
            flashcards.append(Cloze(name, front))

    return flashcards


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
    assert basic.id() == "Id1"
    assert basic.front() == "Front"
    assert basic.back() == "Back"
    assert basic.fields() == ["Front", "Back"]

    cloze = fcs[1]
    assert cloze.model() == MODEL.CLOZE
    cloze = cast(Cloze, cloze)
    assert cloze.id() == "Id2"
    assert cloze.text() == "foo {{c1::bar}} baz"
    assert cloze.back_extra() == ""
    assert cloze.fields() == ["foo {{c1::bar}} baz", ""]
