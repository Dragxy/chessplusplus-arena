# Participating in the Arena

The arena runs two `a2` binaries against each other. Two processes play the **same** game:
the side to move is decided by its own binary, and that move is relayed to the other process
so both boards stay in sync. The referee (`arena/duel.py`) only writes to your **stdin** and
reads your **stderr** — `stdout` is ignored.

To make your code arena-ready, add two things.

## 1. A `play` command on stdin

When you read `play`, your engine picks a legal move for the current player, applies it, and
prints it back as one `@MOVE` line. The move string is the normal move command from the
assignment (e.g. `move e4`, `move nf3`, `move exd5`). The opponent process gets that exact
string on its stdin and replays it.

The same `play` answer is used for **every** prompt, not just normal moves:

- normal move → `@MOVE move e4`
- ambiguity prompt (two pieces can reach the square) → answer with the source square, e.g. `@MOVE g1`
- draw offer from the opponent → answer `@MOVE yes` or `@MOVE no`

## 2. Three stderr markers

All go to **stderr**, one per line, flushed:

| Marker | When |
| --- | --- |
| `@PROMPT <colour>` | before **every** input you read (`@PROMPT white` / `@PROMPT black`) |
| `@MOVE <answer>` | right after `play` decided something |
| `RESULT state=<TOKEN> winner=<who> turns=<n>` | once, at game end |

`@PROMPT` is the clock the referee follows, so print it before every stdin read.
`winner` is `white`, `black`, or `none` (draw). `state` is your end-state name
(`WHITE_WIN`, `BLACK_WIN`, `DRAW_AGREEMENT`, …); only `winner` and `turns` are actually read.

**Why stderr and not an error?** It is not an error channel here, just a side channel.
`stdout` carries the normal game output that the grader checks, so it has to stay clean. The
referee needs a second stream to read the protocol from without touching `stdout`, and `stderr`
is the simplest one that is already separate from `stdout`. So `stderr` is the right tool — no
extra file or socket needed. In a release build you can compile these markers out entirely
or annotate under `#ifndef NDEBUG` so the grader never sees them.


The referee may set `BOT_TIME_MS` as a per-move time budget in milliseconds — read
it at startup and use it as your search limit if you want fixed think time.

## Example

Driving the bot by hand (`>` = you type into stdin, `[err]` = what it prints on stderr):

```
[err] @PROMPT white
>     play
[err] @MOVE move e4          # bot chose e2-e4 and applied it
>     move e5                # you relay black's reply, bot applies it
[err] @PROMPT white
>     play
[err] @MOVE move nf3
...
[err] @PROMPT white
>     play
[err] @MOVE move qd5         # two queens could reach d5 -> game asks which one
[err] @PROMPT white
>     play
[err] @MOVE d1               # answer the ambiguity with the source square
...
[err] RESULT state=WHITE_WIN winner=white turns=41
```

In code it might look a little like this:
```
std::cerr << "@PROMPT " << (current_player_->getColor()) << "\n";
if (input == "play")
    {
      input = ai::decidePlacement(*this, piece_id, back_rank_num - 1);
      std::cerr << "@MOVE " << input << "\n";
    }
```

and at game end:
```
std::cerr << "RESULT state=" << state_token << " winner=" << winner << " turns=" << turn_count_ << "\n";
```
where state_token and winner can be:

| state_token                                   | winner |
|-----------------------------------------------|--------|
| `WHITE_WIN`                            | white  |
| `BLACK_WIN`                              | black  |
| `RESIGNATION` | none   |
| `DRAW_AGREEMENT` |
| `DRAW_STALEMATE` |
| `DRAW_MAX_TURNS` |
| `DRAW_BOTH_KINGS` |

## Run a duel

```bash
arena/duel.py arena/bots/my_bot_v1 arena/bots/archmage_v1.1
```

Both bots play all configs in `arena/configs/`, with colours swapped for fairness (so each
config is played twice). Useful flags:

- `--time-ms <n>` — per-move budget for both bots (sets `BOT_TIME_MS`)
- `--jobs <n>` — how many games run in parallel (default 6)
- `--limit <n>` — only the first N configs, for a quick smoke test

## Where to see results

Each run writes a folder `arena/results/<botA>_vs_<botB>_<timestamp>/` containing:

- **`summary.txt`** — win/draw counts, decisive win-rate, per-colour breakdown, end states.
  This is the "did A beat B" number. It is also printed to the terminal at the end.
- **`games.csv`** — one row per game (config, end state, winner, turns).
- **`<config>_Aw.json` / `<config>_Bw.json`** — full per-game snapshots in json