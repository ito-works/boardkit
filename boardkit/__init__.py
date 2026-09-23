"""boardkit: the curses-free TUI engine behind marshal-board and dash-tui.

text     cell-width aware clipping/wrapping, glyphs, control-char cleaning
styles   256/8-colour style tables (curses attrs resolved lazily)
layout   tiers, boxes, viewport scrolling
cursor   UIState, pure key handling, overlay/expansion bookkeeping
workers  Runner, SourceWorker, Coalescer, Gen
expand   background expansion fetches with token ownership
app      the curses loop, clipboard, tmux jump, url open
windows  the board: … tmux windows on this server: list, switch ([ / ]), count
context  the registry: name, schema, snapshot path, launch command, window name
launcher board <context>: one tmux window per context, selected when it exists

Nothing here imports curses at module level.

Published to https://github.com/ito-works/boardkit by ops/repoman/bin/boardkit-publish after each landing (S3d).
"""

# y9nb: the contract S3's dash-tui pins against (the standard keymap,
# [ / ], the registry)
__version__ = "2026.09.22"
