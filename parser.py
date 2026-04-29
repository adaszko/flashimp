from enum import IntEnum
from typing import List, Protocol

import tree_sitter_markdown
from tree_sitter import Language, Parser


class FLASHCARD(IntEnum):
    BASIC = 0
    CLOZE = 1


class Flashcard(Protocol):
    def type(self) -> FLASHCARD: ...


class Basic:
    def __init__(self, name: str, front: str, back: str):
        self._name = name
        self._front = front
        self._back = back

    def type(self) -> FLASHCARD:
        return FLASHCARD.BASIC

    def name(self) -> str:
        return self._name

    def front(self) -> str:
        return self._front

    def back(self) -> str:
        return self._back


class Cloze:
    def __init__(self, name: str, front: str):
        self._name = name
        self._front = front

    def type(self) -> FLASHCARD:
        return FLASHCARD.CLOZE

    def name(self) -> str:
        return self._name

    def front(self) -> str:
        return self._front


def make_parser():
    lang = Language(tree_sitter_markdown.language())
    return Parser(lang)


def flashcards_from_markdown(markdown: str) -> List[Flashcard]:
    parser = make_parser()
    source = markdown.encode()
    tree = parser.parse(source)

    def node_text(node) -> str:
        return source[node.start_byte:node.end_byte].decode().strip()

    flashcards = []

    for section in tree.root_node.children:
        if section.type != 'section':
            continue

        heading = next((c for c in section.children if c.type == 'atx_heading'), None)
        if heading is None:
            continue

        inline = next((c for c in heading.children if c.type == 'inline'), None)
        if inline is None:
            continue
        name = node_text(inline)

        body = [c for c in section.children if c.type != 'atx_heading']
        break_idx = next((i for i, c in enumerate(body) if c.type == 'thematic_break'), None)

        if break_idx is not None:
            front = '\n'.join(node_text(c) for c in body[:break_idx] if c.type == 'paragraph')
            back = '\n'.join(node_text(c) for c in body[break_idx + 1:] if c.type == 'paragraph')
            flashcards.append(Basic(name, front, back))
        else:
            front = '\n'.join(node_text(c) for c in body if c.type == 'paragraph')
            flashcards.append(Cloze(name, front))

    return flashcards


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
    assert cloze.type() == FLASHCARD.CLOZE
    assert cloze.name() == "Card2"
    assert cloze.front() == "foo {{c1::bar}} baz"
