Maintain your flashcards in a markdown file. Sync periodically with Anki without losing progress.

`flashimp` interfaces directly with the Anki installation on your OS.  No additional Anki plugins or special
steps are necessary.

# Usage

 * Prerequisites
    * Working [installation of `uv`](https://docs.astral.sh/uv/getting-started/installation/) as it's used for
      dependency management
    * ⚠️ Manually created Anki profile called `flashimp` by default needs to exist ⚠️.  It's to keep flashimp
      flashcards isolated from other decks for safety.

```
$ cat <<EOF >flashcards.md
# Example Cloze Deletion Card
foo {{c1::bar}} baz
# Example Front/Back Card
Front Question
***
Back Answer
EOF
$ ./flashimp.py flashcards.md
ADD Example Cloze Deletion Card [...]
ADD Example Front/Back Card [...]
APPLY CHANGES (type YES to confirm)?
YES
$ cat flashcards.lock
{
  "deck": "flashcards",
  "notes": {
    "Example Cloze Deletion Card": {
      "mid": 1782903190095,
      "nid": 1782995851749
    },
    "Example Front/Back Card": {
      "mid": 1782903190091,
      "nid": 1782995851751
    }
  },
  "profile": "flashimp"
}
$ ./flashimp.py flashcards.md
No changes
```

# Features

 * Straightforward flashcards format
 * Flashcards with images support

# Credits

`flashimp` draws from [apy](https://github.com/lervag/apy).
