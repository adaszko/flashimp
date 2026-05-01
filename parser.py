from enum import StrEnum
from typing import List, Protocol, cast

import mistletoe
from mistletoe import Document
from mistletoe.block_token import Heading, Paragraph, ThematicBreak


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


def _token_text(token) -> str:
    if hasattr(token, "children") and token.children:
        return "".join(_token_text(c) for c in token.children)
    if hasattr(token, "content"):
        return token.content
    return ""


def flashcards_from_markdown(markdown: str) -> List[Flashcard]:
    doc = Document(markdown)
    children = doc.children

    flashcards = []
    i = 0
    while i < len(children):
        token = children[i]
        if isinstance(token, Heading) and token.level == 1:
            card_id = _token_text(token)
            i += 1
            body = []
            while i < len(children) and not (
                isinstance(children[i], Heading) and children[i].level == 1
            ):
                body.append(children[i])
                i += 1

            break_idx = next(
                (j for j, t in enumerate(body) if isinstance(t, ThematicBreak)), None
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
                text = "\n".join(
                    _token_text(t) for t in body if isinstance(t, Paragraph)
                )
                flashcards.append(Cloze(card_id, text))
        else:
            i += 1

    return flashcards


def html_from_markdown(markdown: str) -> str:
    return mistletoe.markdown(markdown)


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


def test_html_from_markdown():
    html = html_from_markdown("""foo **bar** baz""")
    assert html.strip() == "<p>foo <strong>bar</strong> baz</p>"
