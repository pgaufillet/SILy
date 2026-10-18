# Link and time: TLA+ models of the mesh

TLA+ models of the link estimation and timing rules of the mesh
(MESH_DESIGN §7, §9, §12 class 1, §16), checked with TLC. They cover the
three defects found by the M3-12 host scenarios (MESH_DESIGN §18, items 1, 2
and 6) and the provisional choices made to fix them. A routing model,
`DODAG` (last section), covers items 3 and 5. Each defect has a
configuration of the current design that must fail with a counterexample;
the choices have configurations that must hold, plus configurations just
beyond each bound that must fail (the bounds are tight).

## Running

```sh
make spec            # every configuration, each against its expected outcome
make spec-pass       # only the configurations that must hold
make spec-defects    # only the configurations that must fail
sh spec/run.sh NAME  # one configuration, cfg/NAME.cfg
```

`TLA_JAR` (default `~/.local/lib/tla2tools.jar`), `WORKERS` (default
`auto`) and `OUT` (logs and counterexample traces, default
`$TMPDIR/sily-spec`) can be set. Each output line gives the verdict (`ok`
when the outcome is the expected one), the configuration, the expected
outcome, the distinct states and the time; the exit status is 1 if any
outcome differs from its expectation. A full run takes about 35 minutes on
8 cores, most of it in `LinkCut` and the liveness configurations of `DODAG`.

Each `cfg/<Module>_<case>.cfg` checks `<Module>.tla`. Its first line says
what it checks; its second line is the expectation read by `run.sh`:
`\* expect: holds` or `\* expect: violated <Name>`. Witnesses (`*_witness`)
check that a property is not vacuous.

## Models

All models are in discrete time and integer arithmetic, and quote the code
they mirror (file:line, `src/mesh`).

| Module | Covers | Time unit |
|---|---|---|
| `Estimation` | shared operators: reception and acknowledgement windows, ETX, link cost, rank hysteresis (`neighbours.py`, `parents.py`) | — |
| `Silence` | a periodic sender (root beacon or presence) and a listener applying a silence threshold: P1 (K − 1 lost frames tolerated), P2 (K lost frames detected); K = 2 for a parent, K = 3 for the root's silent-node alarm | T = 20 ticks |
| `KMiss` | a relay X failing, its child C switching to another parent of its set and reporting, the root's silent-node alarm | T = 20 ticks |
| `AckWindow` | a relay's acknowledgement window, ETX and advertised rank under lost acknowledgements: P4, bad-link detection | samples |
| `LinkCut` | a link cut (both ways, or one way) and its return: P3 | T = 4 ticks |

### Properties

- **P1** (`Silence`): K − 1 consecutive lost periodic frames never make the
  listener drop the sender (P0: no loss at all).
- **P2** (`Silence`): a frame heard after K consecutive losses finds the
  sender dropped; the drop happens Th after the last frame heard.
- **NoFalseAlarm** (`KMiss`): the root never declares C silent when C's
  preferred parent fails and C re-attaches through another parent.
- **P3** (`LinkCut`, `LinkBack`): from Bound ticks after the end of a cut
  on, the link is usable again (eligible, heard, not dropped), for ever.
- **P4** (`AckWindow`): one lost acknowledgement keeps the relay's
  advertised rank below Rank0 + 8 (Rank0: its rank on a perfect link), so a
  child one perfect hop below keeps it as a candidate. `P4Strong`: the rank
  does not move (a child whose rank lags its cost by up to 3 may sit at
  Rank0 + 5). `P4Step`: no single step raises the rank by 8 or more.
- **DetectAny / DetectFresh** (`AckWindow`): KAny consecutive failures make
  a link ineligible whatever the history; KFresh do on a fresh window.

## Provisional choices modelled

1. **Root beacon** on a periodic timer jittered by ±10 % (§7), independent
   of Trickle (`Timer = "periodic"`), and **parent silence threshold
   Th = 5T/2**, strict (50 ticks; 10 in `LinkCut`). `D` is the largest
   variation of the on-air delay of a periodic frame (queue, carrier sense,
   duty cycle).
2. **One acknowledgement sample per frame** (one port 3 entry acknowledges
   every class 1/2 message of the frame): one lost acknowledgement is one
   failed sample, whatever the aggregation (`M = 1`).
3. **Presence reports are passively acknowledged** (§9): the node overhears
   its preferred parent forward its presence, one sample per presence
   (`LinkCut`, `Presence = TRUE`); not when the parent is the root.
4. **Failures age out**: once the newest failure is Th old, the failures
   are removed from the window and its successes kept (`Aging = "failure"`,
   `Forget = "failures"`, delay Th).
5. **Minimum sample count**: p_ack replaces 1/d_in² only from `NMin`
   samples; 4 is the smallest value keeping P4Strong. **Latch** (proposed
   mitigation of the d_in / p_ack switch): once the entry has held NMin
   samples, p_ack is used whenever the window is not empty.

## Results

| Item | Result | Configurations |
|---|---|---|
| D for P1 (one loss tolerated) | D ≤ 5 ticks = T/4 (gap 2 × 22 + D < 50); 6 fails | `Silence_fixed_P1_D5`, `_D6` |
| D for P2 (two losses detected) | D ≤ 4 ticks = T/5 (gap 3 × 18 − D ≥ 50); holds at D = 0, fails at D = 5 | `Silence_fixed_P2_D0`, `_D4`, `_D5` |
| Detection bound | Th = 5T/2 after the last frame heard | `Silence_fixed_P2_*` |
| P3, idle (1 frame of X per period), with or without presence samples | 10 ticks = Th = 2.5 periods after the cut; 9 fails | `LinkCut_idle*` |
| P3, busy (4 frames of X per period) | 10 ticks; 9 fails; same with the latch | `LinkCut_busy*` |
| Minimum samples (P4 with one sample per lost acknowledgement) | NMin = 3 for P4 (rank + 4), NMin = 4 for P4Strong; NMin = 2 fails | `AckWindow_nmin*` |
| Bad-link detection (NMin = 4, with or without latch) | 4 consecutive failures on a fresh window, 13 whatever the history (as the code); 3 and 12 do not suffice | `AckWindow_nmin4_detect*`, `AckWindow_latch_detect` |
| Root threshold Tr, no false alarm (report within 1 tick) | Tr ≥ 74 ticks = 3.7T (22 + 50 + 1 < Tr) | `KMiss_Tr74`, `KMiss_Tr73` |
| Tr, 2 lost presences tolerated | Tr ≥ 67 ticks = 3.35T | `Silence_root_P1_Tr67`, `_Tr66` |
| Tr, 3 lost presences detected | Tr ≤ 72 ticks = 3.6T | `Silence_root_P2_Tr72`, `_Tr73` |
| Tr band | **empty** (74 > 72), at D = 0 | `KMiss_Tr72` |
| `K_MISS` = 4 (owner, session 23): Tr = 90 ticks = 4.5T | no false alarm; 3 lost tolerated up to D = 1 (T/20), 4 detected at D = 0 | `KMiss_Tr90`, `Silence_root4_P1_Tr90`, `_D1`, `_D2`, `Silence_root4_P2_Tr90`, `_D1` |
| Per-message samples (code) | one lost acknowledgement of an 8-message frame detaches the children even on a full window | `AckWindow_permessage_P4_full` |
| d_in / p_ack switch (anomaly 6) | without latch, an asymmetric link (d_in 10/16, p_ack good) jumps from rank 11 to 21 when a failure ages out at the 4th sample; with the latch P4Step holds and P3, P4, detection are unchanged | `AckWindow_nmin4_din_step`, `AckWindow_latch_*` |

## Abstractions (what is not modelled)

- **Radio**: no propagation, SNR or margin (eligibility is the ETX part,
  margin assumed good); losses are chosen by the environment within a stated
  budget, not drawn with a probability. Carrier sense, queueing and duty
  cycle appear only as the on-air delay bound `D` (`Silence`); `KMiss` and
  `LinkCut` have no on-air delay beyond `Dchg` (`KMiss`).
- **Time**: ticks of T/20 (`Silence`, `KMiss`) or T/4 (`LinkCut`). The
  bounds are linear in the periods, so the extreme cases are exact at these
  scales; class 1 timeouts and forwarding delays are instantaneous or folded
  into `Dchg`. No clock drift.
- **Topology**: one sender and one listener (`Silence`); one relay, one
  child, one alternative parent and the root (`KMiss`); one node and its only
  parent (`LinkCut`, `AckWindow`). Parent-set ordering, the switch threshold
  of 12, the L + 56 bound (never reached by one hop of cost ≤ 32), the detach
  hold, poisoning, solicitations and DODAG versions are not modelled; detach
  is "not usable" (`LinkCut`) or rank 255 (`AckWindow`).
- **Trickle**: only the root's timer as coded, its suppression below Imax
  left to the environment; Imin = 2 ticks.
- **Class 1**: one frame in flight; attempts, drop after 3 failed attempts,
  re-admission when a new frame of the parent is heard. No queue, holding,
  rerouting or root acknowledgement batching.
- **Presence** (`LinkCut`): every T ticks exactly, without jitter; the
  parent's forwarding frame is not counted as one of its frames
  (conservative for P3).
- **Root alarm**: K_MISS is not implemented in the code; it is modelled as
  a silence threshold `Tr` on C's presence at the root (`RootTh`).

## Open points (owner)

- **Root threshold Tr** (decided, owner, session 23: `K_MISS` = 4, Tr =
  4.5T, about 135 s at SF7–8 and 270 s at SF9): with Th = 5T/2 the band for
  `K_MISS` = 3 is empty (no false alarm after a relay failure needs
  Tr ≥ 3.7T, detecting 3 lost presences Tr ≤ 3.6T). Checked at Tr = 90
  ticks (`Silence_root4_*`, `KMiss_Tr90`): no false alarm after a relay
  failure, 3 lost presences tolerated up to an on-air delay of T/20
  (violated at T/10), 4 detected without delay; with any delay, 4 lost
  presences may be detected one period late (5 always are). Residual: the
  root alarm tolerates little on-air delay; to measure on the bench.
- **P2 vs on-air delay**: with Th = 5T/2, P1 tolerates D ≤ T/4 but P2 only
  D ≤ T/5: between the two, two lost frames can go undetected until the
  next frame (detection then takes one more period).
- **Bad links seen only through presence**: from a full window, 13 lost
  presences (13 periods) are needed before the link becomes ineligible on
  its ETX; with class 1 traffic the 3-attempt drop acts within seconds.
- **Root acknowledgement batching**: one lost batch frame acknowledges
  several frames of the same relay, hence several failed samples even with
  one sample per frame.
- **Latch**: proposed mitigation of the d_in / p_ack switch; without it,
  anomaly 6 remains (bounded to a loss at the NMin-th sample of a link whose
  d_in and p_ack disagree).

# DODAG: routing, repair and class 1 custody

`DODAG.tla` models upward routing on a small graph — rank and parent
selection, detach and poison, hold, solicitation and rejoin, data-path
validation, a relay death — together with class 1 custody (hop-by-hop
acknowledgement, duplicate cache, 3 attempts and parent drop, holding,
errors). It covers MESH_DESIGN §18 items 3 (a looped class 1 message is
lost without error) and 5 (single poison) against MP5, MP10 and MP11.
Configurations `cfg/DODAG_<graph>_<variant>_<check>.cfg`.

## Scope and abstractions

- **Graphs** (4–5 nodes, root 0, initial DODAG formed on shortest paths,
  views exact): `line` 0-1-2-3 (relay 1 dies: 2 and 3 cut off);
  `diamond` 0-1, 0-2, 1-3, 2-3, 3-4 (relay 1 dies: 3 repairs through 2);
  `subtree` 0-1, 1-2, 1-3, 2-3, 3-4 plus a costly 0-4 (ETX 3) (relay 1
  dies: 2 and 3 can loop through each other before rejoining through 4).
- **Ranks** in whole hops (MIN_HOP 8 = 1): the hysteresis of 4 never holds a
  change back, the switch threshold 12 is a gain of 2 hops; 254 and
  MaxRankIncrease 56 scaled to `MaxRank` = 6 and `MaxInc` = 3. Links always
  eligible (no ETX estimation: `AckWindow`/`LinkCut` cover it). Parent sets
  not truncated to 3 (degree ≤ 3). One DODAG version: versions, global
  repair and the DODAG information parameters are left out.
- **Time** is abstracted to events, every protocol action weakly fair. Kept
  of the timers: the hold (2 Imin) ends after every neighbour's first poison
  (desync ≤ 4 slots); owed explicit acknowledgements (half a timeout) come
  before a holding expires (2 periods); holding expires only without route;
  the duplicate cache never expires; only dead neighbours are removed by the
  silence rule (a live one would need 2 lost presences in a row). Attached
  nodes may send a frame at any time (presence, Trickle advertisement,
  advertisement after a rank error, root beacon): no Imin/period bound is
  checked, only orders of events.
- **Frames** carry the sender's rank and update every neighbour that hears
  them, overheard data frames included. **Losses**: per receiver, at most
  `MaxLoss` overall and at most `MaxConsec` consecutive frames of one node
  lost by one neighbour.
- **Acknowledgements** (`Acks`): `"atomic"` (first model, the M3-12 code):
  an explicit acknowledgement is atomic with the reception, may be lost
  (counted in `MaxLoss` only) and carries no rank; a detached node keeps
  retrying its waiting copies towards its former parent. `"frame"` (the code
  with fix 3): an explicit acknowledgement (duplicate, held, delivered,
  owed) is a separate frame of the acknowledging node, heard or lost like
  any frame (both loss bounds), ordered freely with that node's other
  frames — before or after the message it may hand back — and carrying its
  rank (255 when detached: every rank-255 frame advertises the poison, not
  only the poison and the solicitation); a detaching node holds its waiting
  copies. Only the destination of the acknowledgement hears it
  (pessimistic: neighbours overhearing it are not modelled).
- **Class 1**: one message per origin in `Origins`; per node a copy queued,
  waiting (passively acknowledged by the next hop's forward, overheard, not
  when the forward is addressed to the waiting node itself — as the code —
  or explicitly), held, or none. One copy per node and message: a duplicate
  taken again replaces a copy still waiting (the code keeps both; the
  waiting one only adds a retry). Unbounded queues. **Errors** are abstracted: "sent" by a
  relay whose holding expires, received when the failing relay and the
  origin are both connected to the root (or at once when the origin itself
  fails); the error's own custody is not modelled.
- The relay dies only while it holds no message (dying with an acknowledged
  message is MP5's accepted exception, §12).

### Properties

- **NoSilentLoss** (MP5, safety): a submitted message is delivered, has an
  error, or a live node holds a copy.
- **MP5Live**: every submitted message is delivered or its origin receives
  an error (weak fairness, bounded losses).
- **NoCycle**: no cycle of preferred parents; **NoRejoinLoop**: no rejoin
  (attachment from rank 255) closes a cycle; **AtMostOneLoop**: loops form at
  most once (ghost counter).
- **LoopsEnd** (MP11): `<>[]NoCycle`; **RanksSettle** (MP11): eventually
  every attached node's parent is alive, of strictly lower rank, and seen at
  its actual rank.
- **MP10Live**: eventually every node connected to the root (graph of live
  nodes) is attached through a chain to the root; **CutOffLive**: every node
  cut off ends detached.
- Witnesses: loops form (`NoCycle` violated), messages are delivered
  (`NeverDelivered`), errors reach origins (`NeverError`), data-path
  validation fires (`NoRankError`).

## Fixes modelled

- **Fix 3, custody** (`Fix3 = "custody"`): a node other than the destination
  acknowledges and drops a duplicate only while it holds a copy (queued,
  waiting or held); a message it has handed over and that comes back is
  taken as new (forwarded, or held without route) and acknowledged
  explicitly. The destination (root) still filters duplicates.
- **Fix 3, refined custody** (`Fix3 = "refined"`, the implemented rule,
  `Reliable.holding(key, source)`): the same, except that a copy waiting for
  the acknowledgement of the node the duplicate comes from does not count as
  held: that node took the message, and its acknowledgement, not atomic with
  the reception, may still release the copy.
- Fix 3, retake (`"retake"`, rejected): a duplicate from another node than
  the one it first came from (an own message: from anyone) is taken back as
  held; one from the first sender is dropped as a retransmission.
- **Fix 5, solicit** (`Fix5 = "solicit"`): at the end of the detach hold the
  node sends its solicitation and rejoins only through neighbours heard after
  it (the solicitation, rank 255, is a second poison).
- Fix 5, repeat (`"repeat"`, rejected): the poison is sent twice during the
  hold, the rejoin rule unchanged.

## Results

| Item | Result | Configurations |
|---|---|---|
| Issue 3, current design | one lost poison suffices: 1 dies, 2 detaches, its poison is lost by 3; 3 sends its message to 2 (held, acknowledged); 2's hold ends and it rejoins through 3 (loop 2→3→2); 2 forwards the message to 3, which drops it as its own looped message and acknowledges: no copy, no error | `DODAG_line_current_MP5`, `DODAG_subtree_current_MP5` |
| Issue 5, current design | one lost poison: the detached node rejoins through its own child, a loop (line, subtree); with 2 losses, never 2 in a row on a link, the loop forms again in the other direction after the first one is broken, ranks 3 → 4 → 5 (cycling, bounded by MaxRank only) | `DODAG_line_current_loop`, `DODAG_subtree_current_loop`, `DODAG_line_current_cycling` |
| Fix 5 (solicit) | with at most 1 consecutive loss per link (2 losses overall): no loop at all, and no silent loss even without fix 3 (line, subtree); 2 consecutive losses (poison and solicitation) let the rejoin loop again: the tolerance is exactly 1 consecutive loss | `DODAG_line_solicit_nocycle`, `DODAG_subtree_solicit_nocycle`, `DODAG_line_solicit_consec2`, `DODAG_line_solicit_MP5` |
| Fix 5 variant (repeat) | no loop with ≤ 1 consecutive loss, but a silent loss without fix 3: a neighbour heard after the detach and detached since (its own poison lost) is taken as parent, and the message returns to it | `DODAG_line_repeat_nocycle`, `DODAG_line_repeat_MP5` |
| Fix 3 (custody), atomic acknowledgements | no silent loss, loops included, with up to 2 losses in any pattern (3 on the line with both fixes), alone or with fix 5 | `DODAG_line_custody_MP5`, `DODAG_line_fixed_MP5` |
| Fix 3 (custody), acknowledgements as frames | silent loss (subtree, fix 5, 2 losses: poison and solicitation of 3 lost by 2): 2 rejoins through 3, which rejoins through 2 (loop); 2 sends the message to 3 (held, explicit acknowledgement pending), 3 hands it back, 2 detaches and holds; after its rejoin through 3, 2 sends it again; 3, still waiting for 2's acknowledgement, holds a copy and drops it; the two pending acknowledgements then release both copies | `DODAG_subtree_frame_custody_MP5` |
| Fix 3 refined, acknowledgements as frames | no silent loss (subtree ≤ 2 losses in any pattern; diamond, two messages, ≤ 2; line ≤ 3); with ≤ 1 consecutive loss per link no loop either | `DODAG_*_frame_refined_MP5`, `DODAG_subtree_frame_refined_nocycle` |
| Fix 3 variant (retake) | silent loss: the copy taken back is later sent to a former loop member whose first sender it is, which drops it as a retransmission | `DODAG_line_retake_MP5` |
| Both fixes, liveness | MP5Live, LoopsEnd, RanksSettle, MP10Live, CutOffLive hold, atomic (diamond ≤ 2 losses, line without MP5Live) and as frames with the refined rule (diamond ≤ 2, subtree ≤ 1, line without MP5Live) | `DODAG_*_fixed_live`, `DODAG_*_frame_refined_live` |
| Current design, diamond | liveness holds (local repair only, no detach) | `DODAG_diamond_current_live` |
| Partition | a relay holding a message when it is cut off fails it after the holding; its error cannot reach the origin, cut off too (accepted as an MP5 exception, owner, session 23) | `DODAG_line_partition_MP5Live` |
| Non-vacuity (frames, refined) | loops form with 2 consecutive losses even with both fixes; messages delivered, errors received, rank errors detected | `DODAG_subtree_frame_witness_*` |

**Loop bound (MP11).** With fix 5 and at most one consecutive loss per link
no loop forms (checked on the line and the subtree graph). Beyond, a loop
is broken by the first frame of its inconsistent member heard by its child
in the loop: around a cycle ranks cannot all decrease, so some member's
parent advertises a rank not below the member's own, and on hearing it the
member leaves it (candidates must be strictly lower, parents.py:151). With
at most `MaxConsec` consecutive losses per link the loop survives at most
`MaxConsec` frames of that parent; that parent has just raised its rank
(or attached), which resets Trickle and, after a parent change, sends a
presence at once, so with traffic the loop lasts one forwarding delay
(data-path validation, advertisement at once), idle at most one Imin per
heard advertisement and one presence period when Trickle is suppressed.
`LoopsEnd` and `RanksSettle` check the eventual part.

## Open points (owner)

- **Fix 3 in the code**: implemented with the refined rule
  (`Reliable.holding(key, source)` in `LinkEngine._receive` and
  `Routing._forward`; a duplicate taken again is acknowledged explicitly),
  which the model checks with acknowledgements as frames. The first rule
  (any copy held) is unsound as soon as acknowledgements are not atomic.
  Cost: a lost acknowledgement whose retransmission reaches a relay that has
  already handed the message over makes one duplicate travel the rest of the
  path (filtered by the root). MESH_DESIGN §9 (data-path validation) and §12
  (duplicate cache) to update.
- **Code changes for fix 5**: `Dodag._routing` (dodag.py:480-492): at the
  end of the hold, solicit without selecting; the hold lasts until the
  solicitation goes on air (`Dodag._sent`), which sets
  `Parents.poisoned_at` to that instant, so that only neighbours heard after
  it are candidates (parents.py:155). Cost: one solicitation (≈ 15 B) and the
  neighbours' answers when a candidate was already heard during the hold;
  otherwise the solicitation was sent anyway. Rejoin within one Imin after
  the hold, as the §9 repair table already assumes. In the code every frame
  of a detached node (acknowledgements, solicitations) carries rank 255 and
  acts as a poison; the model's frame mode includes its acknowledgements
  (data retries towards the former parent no longer exist). MESH_DESIGN §9
  (detach) and the M3-11 reading "the poison is sent once" to update.
- **Partition**: a relay cut off from the root while holding a class 1
  message fails it after 2 periods; its error is held then dropped (an error
  about an error is never generated): the origin, cut off as well, never
  learns. Not one of MP5's accepted exceptions (class 2 recovers it).
- **Abstractions to keep in mind**: no timing bound is checked (events
  only); losses bounded by counts, not probabilities; errors not routed;
  versions and global repair left out; whole-hop link costs (no count-up
  through hysteresis or fractional ETX); acknowledgement frames heard by
  their destination only; liveness with acknowledgements as frames checked
  on the subtree graph with 1 loss only (≈ 9 minutes).
