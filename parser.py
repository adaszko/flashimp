from enum import IntEnum
from typing import List, Protocol

import tree_sitter_markdown
from tree_sitter import Language, Parser


class FLASHCARD(IntEnum):
    BASIC = 0
    CLOZE = 1


class Flashcard(Protocol):
    def type(self) -> FLASHCARD: ...


class Basic(Flashcard):
    def type(self) -> FLASHCARD:
        return FLASHCARD.BASIC


class Cloze(Flashcard):
    def type(self) -> FLASHCARD:
        return FLASHCARD.CLOZE


def make_parser():
    lang = Language(tree_sitter_markdown.language())
    return Parser(lang)


def flashcards_from_markdown(markdown: str) -> List[Flashcard]:
    parser = make_parser()
    tree = parser.parse(markdown.encode())
    return tree


def test_flashcards_from_markdown():
    fcs = flashcards_from_markdown("""
# Card1
Front
***
Back
# Card2
foo {{c1::bar}} baz
""")

    assert len(fcs) == 2
    basic = fcs[0]
    assert basic.type() == FLASHCARD.BASIC
    assert basic.name() == "Card1"
    assert basic.front() == "Front"
    assert basic.back() == "Back"

    cloze = fcs[1]
    assert basic.type() == FLASHCARD.CLOZE
    assert basic.name() == "Card2"
    assert basic.front() == "foo {{c1::bar}} baz"
