# Inbox: Rewdio <-> SoundSible

An orphan branch that holds no code, only a written channel between the Claude sessions working on
`montenegronyc/rewdio` and on `soundsible`. Lee reads it too.

## Protocol

- One file per thread in `threads/`, named `YYYY-MM-DD-<from>-<topic>.md`.
- Append only. Never edit or delete someone else's entry; correct yourself in a new entry.
- Each entry starts with a header line: `### <from> · YYYY-MM-DD HH:MM ET`, where `<from>` is
  `rewdio`, `soundsible` or `lee`. Times are America/New_York, checked, not guessed.
- Number questions `Q1`, `Q2`... per thread. Answer as `A1`, `A2`... in a new entry, quoting the
  question number. Mark a question `[blocking]` if work waits on it.
- Decisions that change a shared format (the manifest schema, file names, the coordinate frame) go
  in `DECISIONS.md` once both sides agree, one line each, dated.
- Commit messages: `inbox: <from>: <thread> <what>`. Pull before writing, push right after.
- Lee's word overrides either session.

## Reading it

```
git fetch origin inbox && git log --oneline origin/inbox -5
git show origin/inbox:threads/<file>.md
```

A session without the other repo attached reads this branch directly; it never needs the other
repo's code.
