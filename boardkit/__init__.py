"""boardkit: the curses-free TUI engine behind marshal-board and dash-tui.

text     cell-width aware clipping/wrapping, glyphs, control-char cleaning
styles   256/8-colour style tables (curses attrs resolved lazily)
layout   tiers, boxes, viewport scrolling
cursor   UIState, pure key handling, overlay/expansion bookkeeping
panels   boxed panels in a flow layout, grids, panel focus, the z zoom (tjq1)
workers  Runner, SourceWorker, Coalescer, Gen
expand   background expansion fetches with token ownership
app      the curses loop, clipboard, herdr pane jump and split, url open
windows  the board: … herdr tabs: list, switch ([ / ]), count; the herdr CLI helper
context  the registry: name, schema, snapshot path, launch command, tab label
launcher board <context>: one herdr tab per context, focused when it exists

Nothing here imports curses at module level.

Published to https://github.com/ito-works/boardkit by ops/repoman/bin/boardkit-publish after each landing (S3d).
"""

# y9nb: the contract S3's dash-tui pins against (the standard keymap,
# [ / ], the registry). 6swq: J / K joined the standard keymap, so a board
# that bound either to its own action is refused at startup.
__version__ = "2026.09.25"
