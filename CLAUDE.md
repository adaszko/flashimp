This is a tool for importing flashcards written in Markdown into Anki.

Keeping flashcards in Markdown format makes it easy to write and share them.

The tool is designed to be idempotent — it is meant to be run repeteadly against a folder of markdown files
and it's supposed to check reflect any updates to those files on the Anki database.

# Desired workflow

1.  I write a bunch of flashcards in `flashcards.md`
2. `./run.sh --deck book-i-just-read --dry-run flashcards.md`

It outputs the dry run plan (having checked it against the Anki database):

```
Add flashcard foo
Add flashcard bar
[...]

Summary: Add 12 cards, Update 0 cards.
```

3. `./run.sh --deck book-i-just-read flashcards.md`

It creates those flashcards in Anki and writes their ids into `.anki.json`.  It also writes the **deck name** into `.anki.json` so that it doesn't need to be specified again on subsequent runs.

4. I modify `flashcards.md` — fix some typos, reformulate some cards to make them more memorable, etc.
5. `apy import --dry-run flashcards.md`

`apy` spots I already have `.anki.json`, so it takes it into account and outputs the plan:

```
Not modified flashcard foo
Update flashcard bar
[...]

Summary: Add 0 cards, Update 4 cards.
```

6. `apy import flashcards.md`
7. I happily review the flashcards in Anki

Notes:

 * The deck name is also stored in the metadata JSON file.  This is fine since `.anki.json` isn't meant to be sharable
