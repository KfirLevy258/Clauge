# AGENTS.md

The instructions for working in this repository are in **[CLAUDE.md](CLAUDE.md)**,
which is the single canonical copy. Read it before making any change.

It is one file rather than two because the rules that matter here are about the
hardware and the shipped product, not about which assistant is reading them: a
public repository that must carry no session trailers, a daemon that must stay
dependency-free, a serial buffer that must never be logged, and boards in
customers' hands that a careless flash can brick.
