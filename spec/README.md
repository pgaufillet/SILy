# Link and time: TLA+ models of the mesh

TLA+ models of the link estimation and timing rules of the mesh
(MESH_DESIGN §7, §9, §12 class 1, §16), checked with TLC. The host
scenarios of upward routing found six design issues, numbered as found and
quoted by number below and in the models, each fixed since:

1. one lost root beacon made the root's children drop it, which rebuilt
   the DODAG: root beacon on its own timer and parent silence limit of 2.5
   periods, MESH_DESIGN §9 *Link estimation*, *Trickle and control
   traffic*;
2. acknowledgement failures never aged: failure ageing, §9 *Link
   estimation*;
3. a looped class 1 message was lost without error: custody of duplicates,
   §12 *Class 1*;
4. presence was refused above 80 % duty-cycle use, which hid the flag
   reporting it: flag kept until a presence goes on air, §11 *Mesh ports*;
5. a single lost poison let a detached node rejoin through its own
   descendants: solicitation before the rejoin, §9 *Loop freedom*;
6. one lost root acknowledgement made a relay's rank jump: one sample per
   frame, one failed sample per batching delay towards the root, `p_ack`
   from 4 samples, §9 *Link estimation*.

The link and time models cover issues 1, 2 and 6, the choices made to fix
them, and the options for a root beacon tolerating two consecutive losses.
A routing model, `DODAG` (last section), covers issues 3 and 5, rank
following, DODAG version lag and the rejoin after a solicitation. Each
defect has a configuration of the current design that
must fail with a counterexample; the choices have configurations that must
hold, plus configurations just beyond each bound that must fail (the bounds
are tight).

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
outcome differs from its expectation. A full run takes about 75 minutes on
8 cores, most of it in `LinkCut`, the liveness configurations of `DODAG`
and its two-version safety configurations on the diamond (≈ 8 and 3
minutes; with the answer window and its ghost variables ≈ 15 minutes on 4
cores), plus about 1.5 hours for the link cost rise configurations
(`DODAG_line_low_*`, `DODAG_subtree_low_nocycle`: 20 to 35 minutes each),
and about 65 minutes for the link death configurations (`DODAG_*_cutlive`:
1.5 to 17 minutes each; `DODAG_diamond_reachall_cutlive` 17, its
liveness check slow for its 24 000 states, `DODAG_line_reachall_cutlive`
12), plus 10 minutes for `DODAG_diamond_reachall_nocycle`, and about
52 minutes for the root sequence configurations (`DODAG_*_babel_*`: 1 s
to 11 minutes each: `DODAG_diamond_babel_cutuplive` 11, `DODAG_star_babel_live`
10, `DODAG_star2_babel_safety` 9).

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

## Choices modelled

1. **Root beacon** on a periodic timer jittered by ±10 % (§7), independent
   of Trickle (`Timer = "periodic"`), and **parent silence threshold
   Th = 5T/2**, strict (50 ticks; 10 in `LinkCut`). `D` is the largest
   variation of the on-air delay of a periodic frame (queue, carrier sense,
   duty cycle).
2. **One acknowledgement sample per frame** (one port 3 entry acknowledges
   every class 1/2 message of the frame): one lost acknowledgement is one
   failed sample, whatever the aggregation (`M = 1`). Towards the root,
   **one failed sample per batching delay**: a frame that ended within one
   batching delay of the frame of the last failed sample towards the root
   gives no failed sample, so that one lost batch acknowledging several
   frames of the relay is one failure (`M = 1` again); the attempts of one
   message are a root timeout apart, longer than the batching delay, so
   each still fails once.
3. **Presence reports are passively acknowledged** (§9): the node overhears
   its preferred parent forward its presence, one sample per presence
   (`LinkCut`, `Presence = TRUE`); not when the parent is the root.
4. **Failures age out**: once the newest failure is Th old, the failures
   are removed from the window and its successes kept (`Aging = "failure"`,
   `Forget = "failures"`, delay Th).
5. **Minimum sample count**: p_ack replaces 1/d_in² only from `NMin`
   samples; 4 is the smallest value keeping P4Strong. **Latch** (mitigation
   of the d_in / p_ack switch): once the entry has held NMin
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
| `K_MISS` = 4 (chosen, the band for 3 being empty): Tr = 90 ticks = 4.5T | no false alarm; 3 lost tolerated up to D = 1 (T/20), 4 detected at D = 0 | `KMiss_Tr90`, `Silence_root4_P1_Tr90`, `_D1`, `_D2`, `Silence_root4_P2_Tr90`, `_D1` |
| Per-message samples (before choice 2) | one lost acknowledgement of an 8-message frame detaches the children even on a full window | `AckWindow_permessage_P4_full` |
| Per-frame failures in a lost root batch (without the one-per-batching-delay rule of choice 2) | a lost batch acknowledging 2 frames of the relay (`M = 2`) on a fresh window takes the rank from 8 to 16 (P4 violated, NMin = 4 with latch) | `AckWindow_rootbatch_P4` |
| Root beacon every half period (T = 10 ticks, presence period 20), Th unchanged (50) | 3 consecutive lost beacons tolerated up to D = 5 ticks (4 × 11 + D < 50), 6 fails; 2 tolerated up to the model's largest D, 8 ticks; 4 lost may go undetected until the next beacon (5 × 9 < 50); 5 detected up to D = 4, 5 fails | `Silence_half_*` |
| Root beacon every 2/3 period (T = 20 ticks, presence period 30, Th = 75) | 2 consecutive lost beacons tolerated up to D = 8 ticks = 0.27 presence period (3 × 22 + D < 75), 9 fails; 3 lost drop the root | `Silence_twothirds_*` |
| Root parent with its own threshold, beacon every period | 2 lost beacons tolerated from Th = 67 ticks = 3.35T (D = 0; 66 fails); at Th = 70 (3.5T) up to D = 3 (4 fails), 3 lost detected up to D = 2 (3 fails) | `Silence_rootth*` |
| Root parent threshold against the root alarm (cut link to the root, report within 1 tick) | Th = 70: false alarm at Tr = 90 (K_MISS = 4), none from Tr = 94 = 4.7T (93 fails); Th = 67: false alarm at Tr = 90 (the band is empty) | `KMiss_rootth*` |
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

## Choices from the models

- **Root threshold Tr**: `K_MISS` = 4, Tr = 4.5T, about 135 s at SF7–8 and
  270 s at SF9 (MESH_DESIGN §16). With Th = 5T/2 the band for `K_MISS` = 3
  is empty (no false alarm after a relay failure needs Tr ≥ 3.7T, detecting
  3 lost presences Tr ≤ 3.6T), so 4 is the smallest count with a valid
  threshold. Checked at Tr = 90 ticks (`Silence_root4_*`, `KMiss_Tr90`): no
  false alarm after a relay failure, 3 lost presences tolerated up to an
  on-air delay of T/20 (violated at T/10), 4 detected without delay; with
  any delay, 4 lost presences may be detected one period late (5 always
  are).
- **P2 vs on-air delay**: with Th = 5T/2, P1 tolerates D ≤ T/4 but P2 only
  D ≤ T/5: between the two, two lost frames can go undetected until the
  next frame (detection then takes one more period). A known limit,
  stated in MESH_DESIGN §9 (silence limit).
- **Root beacon every half period** (MESH_DESIGN §9, Trickle and control
  traffic): with one beacon per period a
  child of the root drops it after two consecutive lost beacons (gap up to
  3 × 1.1 = 3.3 periods > 2.5); having no other candidate of lower rank, it
  detaches with its subtree. A beacon every half period (±10 %) keeps the
  silence threshold, hence the detection of a dead root or a cut link
  (2.5 periods after the last frame heard, idle switch time unchanged), and
  tolerates 3 consecutive lost beacons up to an on-air delay of T/4 (2 up to
  0.4 T); its cost is one more beacon per period at the root (24 B, 7–14
  s/h at SF7–12, 2–4 % of the root's budget; at SF8 it takes the presence
  share of the channel, already at 99 %, to 103 %). A beacon every 2/3
  period tolerates exactly 2 lost beacons (up to D = 0.27 period) for half
  that cost. A threshold of its own for a parent that is the root needs
  ≥ 3.35 periods to tolerate 2 losses, which with a report within T/20
  raises the root alarm on a root child that re-attaches after a cut (no false alarm needs Tr > 1.1 + Th + report
  delay: 4.7 periods at Th = 3.5), and lengthens the idle switch of a root
  child by one period. The half period is therefore the choice: it
  tolerates the most losses without changing any detection bound.
- **Latch**: the mitigation of the d_in / p_ack switch, in the design
  (MESH_DESIGN §9, link estimation); without it, anomaly 6 remains (bounded
  to a loss at the NMin-th sample of a link whose d_in and p_ack disagree).
- **One failed sample per batching delay towards the root** (MESH_DESIGN
  §9, link estimation; §12, class 1): one lost root batch frame
  acknowledges several frames of a relay; counted per frame, it gives as
  many failed samples (`M` > 1: `AckWindow_rootbatch_P4` violates P4 with
  2), so that congestion at the root would read as a bad link. Counted
  once, it is the case P4 checks (`AckWindow_nmin4_P4`,
  `AckWindow_latch_P4`: P4 and P4Strong hold). Cost: separate frame losses
  within one batching delay count once (a lossy link towards the root
  reads slightly better); detection is unchanged, the attempts of one
  message being a root timeout apart.

## Open points

The same points appear in MESH_DESIGN §18.

- **Root alarm and on-air delay**: the root alarm at Tr = 4.5T tolerates
  3 lost presences only up to an on-air delay variation of T/20; to measure
  on the bench.
- **Bad links seen only through presence**: from a full window, 13 lost
  presences (13 periods) are needed before the link becomes ineligible on
  its ETX; with class 1 traffic the 3-attempt drop acts within seconds.
  Candidate rule, not modelled: a link whose newest 4 samples are failures
  is ineligible (4 = the fresh-window bound, `AckWindow_nmin4_detect`);
  one success or the ageing of the failures lifts it. Whether idle bad
  links matter is for the bench measurements (loss patterns).

# DODAG: routing, repair and class 1 custody

`DODAG.tla` models upward routing on a small graph — rank and parent
selection, detach and poison, hold, solicitation and rejoin, data-path
validation, a relay death, DODAG versions and global repair — together
with class 1 custody (hop-by-hop acknowledgement, duplicate cache, 3
attempts and parent drop, holding, errors). It covers issues 3 (a looped
class 1 message is lost without error) and 5 (single poison) of the first
section,
and later defects of the readings: a child cannot follow its preferred
parent's rank increase (it detaches), a node behind a new DODAG version
detaches after one lost advertisement, a node rejoins through the first
neighbour that answers its solicitation, not the best, a node
whose link cost rises takes its own descendant as parent without any
loss, a late root acknowledgement, ignored, makes a node drop the
root without any loss, and three frames of a root child lost at the root
(collisions, no failed link) make it drop the root, its only candidate,
and detach, and a detached node rejoins through a neighbour still at a
finite rank but cut off from the root (a rootless DODAG whose ranks count
up); against MP5, MP10, MP11 and MP12.
Configurations `cfg/DODAG_<graph>_<variant>_<check>.cfg`.

## Scope and abstractions

- **Graphs** (4–5 nodes, root 0, initial DODAG formed on shortest paths,
  views exact): `line` 0-1-2-3 (relay 1 dies: 2 and 3 cut off);
  `diamond` 0-1, 0-2, 1-3, 2-3, 3-4 (relay 1 dies: 3 repairs through 2);
  `subtree` 0-1, 1-2, 1-3, 2-3, 3-4 plus a costly 0-4 (ETX 3) (relay 1
  dies: 2 and 3 can loop through each other before rejoining through 4);
  `follow`: the diamond with a costly 2-3 (ETX 2) (relay 1 dies: 3 falls
  back on 2 at rank 3, the rank of its child 4, whose only neighbour it
  is); `rejoin` 0-1, 1-2, 2-3, 2-4 and costly links 0-3 (ETX 2), 0-4 (ETX
  3) (relay 1 dies: 2 detaches and rejoins through 3, path cost 3, or 4,
  path cost 4, a gain below the switch threshold); `switch` 0-1, 0-2, 1-3
  and a costly 2-3 (ETX 3) (no death: 3 drops 1, falls back on 2, keeps
  it as its last candidate, then hears 1 again, 2 hops cheaper: the
  keep followed by a switch); `star` 0-1, 1-2, 1-3, 2-3 (a relay and two
  of its Nodes hearing each other; relay 1 dies: both cut off, each still
  hearing the other at a finite rank until its own silence limit);
  `star2` 0-1, 0-2, 2-3 and a costlier 1-3 (ETX 2) (two relays, Node 3
  under relay 2; relay 2 dies and the uplink 1 → 0 dies, `Cut = "up"`:
  relay 1 still hears the root but cannot reach it). `Victim = 0`: no
  node dies.
- **Ranks** in whole hops (MIN_HOP 8 = 1): the hysteresis of 4 never holds a
  change back, the switch threshold 12 is a gain of 2 hops; 254 and
  MaxRankIncrease 56 scaled to `MaxRank` = 6 and `MaxInc` = 3. Links always
  eligible (no ETX estimation: `AckWindow`/`LinkCut` cover it), except the
  link to the root (`Cut`, `Elig`, below). Parent sets
  not truncated to 3 (degree ≤ 3). Link costs are fixed (`Rise = 0`, a
  rank then rises only by a local repair after a relay death, `follow`
  graph) or rise (`Rise` > 0, below): the environment raises a node's
  cost to one neighbour by one hop at a time, `Rise` hops overall, at any
  moment (its ETX after failed samples); the node reads it at its next
  selection, after the next frame it hears, as the code does. Costs never
  fall back.
- **Versions** (`Versions = 2`; `1`: one version, the first model):
  versions 0 and 1, the root issues 1 once at any moment (the policy
  deciding when, at most one version per 4 periods, is not modelled). Every
  frame carries its sender's version; frames of rank 255 do not propagate
  it. The DODAG information message rides on advertisements only (Trickle,
  root beacon: `dio`), not on presence, data or acknowledgement frames; its
  parameters, the regional profile, versions 8 apart and the unconfigured
  boot state are not modelled. A node adopts the version of a DODAG
  information from a neighbour of finite rank: a newer one, or any other
  one when detached; it then takes parents of that version only, at any
  rank (L, the hold and the drops forgotten). A finite-rank frame of a
  newer version makes an attached node *behind*; one of an older version,
  a solicitation, an adoption and the root's new version make the node owe
  a DODAG information (`owe`: the Trickle reset), which weak fairness
  eventually sends (Trickle suppression not modelled). Data-path
  validation compares ranks of the same version only.
- **Time** is abstracted to events, every protocol action weakly fair. Kept
  of the timers: the hold (2 Imin) ends after every neighbour's first poison
  (desync ≤ 4 slots); owed explicit acknowledgements (half a timeout) come
  before a holding expires (2 periods); holding expires only without route;
  the duplicate cache never expires; only dead neighbours are removed by the
  silence rule (a live one would need 2 lost presences in a row). Attached
  nodes may send a frame at any time (presence, Trickle advertisement,
  advertisement after a rank error, root beacon): no Imin/period bound is
  checked, only orders of events. A node behind a newer version (the code
  before the fix, `Behind = "detach"`) keeps its parents for 2 Imin, which end after its solicitation and the
  DODAG information owed by each of its newer neighbours (answers within
  Imin); a class 1 drop selects at once, as `Parents.next_parent` does.
- **Frames** carry the sender's rank and update every neighbour that hears
  them, overheard data frames included. **Losses**: per receiver, at most
  `MaxLoss` overall and at most `MaxConsec` consecutive frames of one node
  lost by one neighbour.
- **Acknowledgements** (`Acks`): `"atomic"` (first model, the code before
  fix 3):
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
- **Late root acknowledgements** (`LateAck`, with `Acks = "frame"`): the
  root batches its acknowledgements, and under load its medium access can
  delay one beyond the root timeout. `"none"` (the earlier model, every
  configuration before this option): an acknowledgement on its way always
  comes before the timeout. `"ignore"` (the code before late
  acknowledgements were accepted): the timeout towards the root may also
  fire while the root's acknowledgement is on its way, which is then
  ignored (retry, or the drop after the last attempt). `"accept"` (the
  code): the same timeout and retry, but the acknowledgement of the
  earlier transmission stays expected (late) and, when it comes, removes
  the copy wherever it is (queued, waiting, held); after the last attempt
  the drop waits for the acknowledgement of that attempt. Timing kept: an
  acknowledgement is late by less than one root timeout, so it comes (or
  is lost) before the next timeout of the copy and before the drop. The
  code accepts an acknowledgement of any of the last 3 transmissions
  while the message is queued, even later; the model leaves that out, and
  the failed samples (`AckWindow`) too.
- **Exhausted attempts** (`Exhaust`): what the last timeout of a copy does
  to its hop. `"drop"` (the code before `"keeproot"`, every configuration
  before this option): the hop is dropped (`Parents.next_parent`), the
  node selects again, the copy goes to the new preferred parent with fresh
  attempts or is held; under the test against L the root is a root
  child's only candidate, so the drop is a detach. `"keeproot"` (the
  code): towards the root, while the root is still a candidate apart from
  drops (heard, i.e. its entry not expired; link eligible; within the
  bound), the copy is queued towards the root again with fresh attempts
  instead; only the silence limit or the link's ineligibility removes the
  root. `"keeplast"`: the
  same for the preferred parent, root or not, when it is the node's last
  candidate (dropping it would detach the node); otherwise the drop.
  `"keepboth"` (remedy A extended to relays): the root as `"keeproot"`
  (kept even with other candidates), and the preferred parent as
  `"keeplast"` when it is the node's last candidate. The keep is decided
  at each exhaustion from the state of that moment, never remembered: a
  candidate heard since takes the kept parent's place by the switch
  threshold at the next selection, or otherwise makes the next exhaustion
  drop it, the copy going to the new parent with fresh attempts
  (`KeepOnlyLast`). The random retry delay that goes with it in the code
  is timing only, outside this untimed model.
- **Link death** (`Cut`, with `Acks = "frame"`): `"none"` (every
  configuration before this option); `"both"`: the link 0 – 1 dies once,
  at any moment, both ways; `"up"`: only 1 → 0 dies (the root no longer
  hears node 1, node 1 still hears the root); `"relayup"`: only the
  uplink of a hop-2 node to relay 1 dies (2 → 1 on the line, 3 → 1 on the
  diamond). A dead link loses every
  frame and acknowledgement, outside the loss bounds, and carries no route
  (`MP10Live`, `CutOffLive` and error delivery read the graph of live
  links). The silence limit removes a neighbour no longer heard (`Expire`,
  as for a dead relay). Node 1's failed samples towards the root make the
  link ineligible (`Elig`, below).
- **Eligibility of the link to the root** (`Elig`, with `Acks =
  "frame"`): a node's failed samples towards the root (`rfail`) are its
  transmissions to the root whose acknowledgement never comes: a timeout
  with no acknowledgement on its way (frame lost at the root, or its
  acknowledgement lost at the node), or a late acknowledgement lost (the
  failure decided once it is out). Other links stay eligible.
  `"none"` (every configuration before this option): only node 1's
  timeouts towards the root after the death count, and `INELIG` = 4 of
  them make the link ineligible (13 from a full window, `AckWindow`;
  scaled to 4, more than one round of attempts, so that `"keeproot"`
  gives fresh attempts at least once); failure ageing is not modelled, the
  dead link stays ineligible; collisions on a live link never make it
  ineligible. `"etx"` (the code before remedy A, ETX ≤ 4): every failed sample of any node
  towards the root counts, live link included, and `INELIG` of them make
  it ineligible; neither successes (a failure leaves the 16-sample window
  only 16 samples later) nor ageing (a silence limit after the newest
  failure, not reached under sustained load) remove them: slightly
  pessimistic, enough for a witness of D28's mechanism. `"reach"`
  (remedy A of D28, RPL neighbour unreachability detection style, the
  node's class 1 transmissions to the root as probes): the link to the
  root is unreachable (ineligible) once the node's last U failed samples
  were consecutive and no acknowledgement of the root came for one silence
  limit; any acknowledgement of the root (late included) makes it
  reachable again. Modelled as a run of consecutive failed samples, reset
  by every acknowledgement of the root the node hears, `UNREACH` of them
  making the link unreachable; the silence limit only makes the code's
  rule stricter (a dead link never acknowledges, so it always passes). Each
  failed sample on a live link costs a loss, and a run is bounded by
  `MaxLoss` only, not by `MaxConsec`: the losses alternate between the two
  directions (a frame lost at the root, then an acknowledgement lost at
  the node) and the frames in between (root beacons, the node's
  presences) reset the per-link counts. Hence `UNREACH` = `MaxLoss` + 1,
  at least `INELIG` (4 with ≤ 3 losses, as `"none"` on the dead link).
  The root's link cost is unchanged (fixed, `Rise = 0`). `"reachetx"`
  (the code of A): the root link as `"reach"`, every other link as
  `"etx"` (each failed sample towards a relay counts, `INELIG` of them
  make the link ineligible). `"reachall"` (A extended to relays): every
  link as the root's under `"reach"`, ETX ≤ 4 no longer an eligibility
  test; the failed samples are counted per link (`rfail[k][c]`), and every
  acknowledgement of c heard by k ends the run: explicit, late (root
  only), or passive (k overhears c's forward of its message). The same
  arithmetic: a failed sample towards a live relay over a live link costs
  one loss (k's frame lost at c, c's explicit acknowledgement or its
  forward lost at k; c's holding expires only after the acknowledgements
  it owes, and a dying relay holds no message), so `UNREACH` = `MaxLoss`
  + 1 again keeps every live link reachable. The code's cap of the link
  cost at ETX 4 (cost 32) on the links it judges by reachability (the
  root's, the last candidate's) is outside the model (fixed costs).
- **Class 1**: one message per origin in `Origins`; per node a copy queued,
  waiting (passively acknowledged by the next hop's forward, overheard, not
  when the forward is addressed to the waiting node itself — as the code —
  or explicitly), held, or none. One copy per node and message: a duplicate
  taken again replaces a copy still waiting (the code keeps both; the
  waiting one only adds a retry). Unbounded queues. **Errors** are abstracted: "sent" by a
  relay whose holding expires, received when the failing relay and the
  origin are both connected to the root (or at once when the origin itself
  fails); the error's own custody is not modelled.
- **Root sequence** (`Seq`, `MaxSeq`): the root increments a sequence at
  each beacon (`NewSeq`, `MaxSeq` times, no wrap-around: the code compares
  in serial arithmetic and forgets a sequence that has not advanced for 50
  periods, MESH_DESIGN §9; with `MaxSeq` ≥ 2 without end and weakly fair:
  once the root is at `MaxSeq` and every sequence still compared is at
  least 1, all go down by one, which keeps every comparison, so that a
  liveness check does not run out of increments; a node `MaxSeq` behind
  holds the root back); the frames of `Announce` and `HoldEnd`
  (presence, DODAG information, poison, solicitation) carry the sender's
  sequence, data and acknowledgement frames do not; every receiver of a
  frame reads it, whatever the frame's link destination (`sv`: the
  sequence of each neighbour's last frame heard; the code must read
  overheard frames too, MESH_DESIGN §9). The design's solicitation is an
  empty port 9 message without the sequence (MESH_DESIGN §11): the model
  is more permissive there, though its solicitation only repeats the
  poison's sequence (a detached node keeps its own) and differs only when
  the poison was lost; the safety argument does not depend on which
  frames carry the sequence. A node holds the sequence of its
  preferred parent's last frame when newer than its own. `"none"` (every
  configuration before this option, `MaxSeq` = 0; with `MaxSeq` > 0 the
  sequence is a ghost read by `NoStaleRejoin` only): the code before
  remedy C. `"babel"` (remedy C, Babel's feasibility condition): a
  neighbour other than the preferred parent is a candidate with a newer
  sequence (any rank within the rank limit: the newer sequence restarts
  L), or the same sequence and a rank below L, never an older one; L and
  the sequence are kept across a detach, only a newer sequence restarts
  L. **Grounding**: a detached node is ungrounded (`fl`), and an attached
  one becomes so (`Float`) once its sequence has not advanced for the
  grounding limit G; the timer abstraction: G exceeds the longest gap
  between advances at a node that the root's sequence still reaches down
  its chain of preferred parents (MESH_DESIGN §9), so only a node it no
  longer reaches floats, at any later moment (a dead or detached node on
  the chain, or a parent whose frames no longer reach the node; a dead
  uplink still lets the sequence down: the relay stays grounded and its
  reachability leaves the parent). A newer sequence of
  its preferred parent grounds it again, even from a floating parent:
  the design forbids that (a floating neighbour's sequence grounds no
  one, MESH_DESIGN §9), the model is more permissive (below). An ungrounded node keeps its
  routing state but forwards no upward message: queued copies are held,
  a waiting one is held at its timeout (no retry, no drop), one it
  receives is held and acknowledged explicitly; `HoldExpire` may fail
  them, `Resume` waits for the grounding. The hold counted from the loss
  of grounding, not renewed at a hand-over, is a time bound, outside this
  untimed model; so is the floating flag of the frame header (a floating
  neighbour not a candidate, its sequence grounding no one, children
  floating with their parent), left out (`Float` may come at any moment):
  the model is more permissive there, and the safety argument (a
  descendant never feasible) does not use the flag, but what rests on it
  is not checked: the hold bound
  (MESH_DESIGN §12: a node grounds again only through a parent that is
  not floating) and the dissolution of a cut-off structure as its nodes
  float. `"babel"` requires
  `Thr = "low"`, one version, acknowledgements as frames and fix 5.
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
  nodes and links) is attached through a chain of live links to the root; **CutOffLive**: every node
  cut off ends detached; **VersionSettle**: eventually every node connected
  to the root is in the root's version.
- **RoutesSettle** (MP12, no flapping): eventually no preferred parent
  changes (`<>[][pref' = pref]_vars`). `RanksSettle` (above) requires the
  parent to be of the node's version.
- **KeepParent** (MP10, MP12, action property): a node detaches (finite rank
  to 255, in its version) only when its preferred parent is lost — gone
  silent, heard at rank 255, dropped after failed attempts, or its link
  made ineligible while that parent or the link is dead — or, when
  that parent is of its version, when the path cost through it exceeds the
  bound L + MaxInc. A node whose preferred parent stays attached within the
  bound never detaches.
- **RejoinBest** (MP12, action property): when a node's answer window
  closes with the node attached through a parent taken in that window, the
  path cost of that rejoin is the lowest among the candidates heard in the
  window, each judged by its last frame heard (finite rank, of the node's
  version, not dropped, within 254). With losses, a neighbour whose answers
  were all lost is not among them.
- **NoLagDetach** (MP10, action property): a node whose preferred parent is
  not lost never detaches because that parent moved to a newer version: no
  connected node detaches during a global repair.
- **RootKept** (`LateAck`, `Exhaust`): no node drops the root after
  failed attempts, unless its link to the root died (`Cut`). With
  `"drop"`, every such drop takes three timeouts of one copy towards the
  root, each after a lost frame or acknowledgement, unless an
  acknowledgement on its way is ignored: with `MaxLoss` < 3 it must hold;
  with `"keeproot"` or `"keeplast"`, whatever the losses.
- **RootReachable** (`Elig`): no node's link to the root is made
  ineligible, unless that link died (`Cut`). **LinksReachable**: the same
  for every link, unless the neighbour or the link died.
- **LastKept** (`Exhaust = "keepboth"`, action property): a node detaches
  while its preferred parent, root or relay, is alive over a live link
  and heard at a finite rank of its version only beyond the bound; unlike
  `KeepParent`, a drop after failed attempts or an ineligible link is no
  excuse: collisions alone never detach a node from its last candidate.
- **KeepOnlyLast** (action property): a copy gets fresh attempts towards
  a relay after its last attempt only while no other neighbour is a
  candidate (the keep is not sticky).
- **MP5Decided** (MP5 under partition): every submitted message ends
  delivered or failed by some node (its error may not reach an origin cut
  off with it): no copy is retried for ever. **CutOffLiveTraffic**:
  `CutOffLive` once the cut child has a message queued after the death
  (its transmissions are the only probes of a one-way dead uplink it
  still hears).
- Witnesses: loops form (`NoCycle` violated), messages are delivered
  (`NeverDelivered`), errors reach origins (`NeverError`), data-path
  validation fires (`NoRankError`); a node keeps its parent heard at a rank
  not below its own (`NoFollow`), a node detaches on the bound
  (`NoBoundDetach`), a node stays attached through a parent of a newer
  version (`NoLagParent`), a class 1 message is taken across versions
  (`NoCrossVersion`), a node rejoins at the close of its answer window
  (`NoWindowRejoin`), a copy is retried while the root's acknowledgement
  of an earlier transmission is still expected (`NoLateRetry`), a copy
  gets fresh attempts towards the root after its last attempt
  (`NoFreshRoot`), or towards a relay (`NoFreshRelay`), a run of `MaxLoss`
  consecutive failed samples towards the root (`ShortFailRuns`), or towards
  a relay (`ShortRelayRuns`), a node switches away from the relay it kept
  as its last candidate (`NoSwitchAfterKeep`, ghost `kp` with `Ghosts`), an
  ungrounded node keeps its parent and holds a message (`NeverFloatHold`).
- **NoStaleRejoin** (rootless DODAG, action property): no node rejoins
  (attaches from rank 255) through a neighbour whose chain of preferred
  parents no longer reaches the root (a dead or detached node, or a cycle,
  on it; read after the step) and which holds no root sequence newer than
  the node's own: it knows nothing of the root the node did not know when
  it detached. A dead link not yet detected does not make the chain stale
  (no node can tell it; its reachability does, `Elig`). With `Seq =
  "none"` the sequence is a ghost. With `"babel"` its newer-sequence
  exception restates the feasibility guard: what it checks is that no
  node rejoins through a stale neighbour at the same sequence and below
  its L, which no configured graph (line, diamond, `star`, `star2`)
  offers; it checks rejoins from rank 255 only, not parent switches
  without a detach (results, limits).

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
- **Rank following** (`Follow = TRUE`, the RPL rule, the code): the preferred parent
  stays a candidate whatever its rank — of the node's version, finite, not
  dropped, path cost within L + MaxInc — and the node's rank becomes the
  path cost through it; the other candidates must still pass the rank
  test (`Thr` below). `FALSE` (the code before the fix): every candidate, the
  preferred parent included.
- **No detach behind a version** (`Behind = "keep"`, the code): a node behind a newer
  version keeps selecting in its own version and keeps its preferred
  parent once that parent has moved to the newer version (heard at a
  finite rank, not dropped), at its own rank (ranks of different versions
  are not compared), until it adopts the newer version; it forwards
  through it meanwhile, and its frames, of the older version, make its
  newer neighbours owe a DODAG information (the solicitations with backoff
  are frames of the node like any other). `"detach"` (the code before the fix): no
  selection for 2 Imin, then candidates of its own version only.
- **Adoption after the hold** (`Adopt = "fresh"`, the code): a detached node adopts
  another version only once its hold is over, i.e. from a DODAG
  information heard after its solicitation (the rejoin rule of fix 5).
  `"any"` (the code before the fix): at once, ending its hold.
- No detach behind a version with adoption at once (`Behind = "keep"`,
  `Adopt = "any"`, rejected): the adoption loop below remains.
- **Answer window** (`Rejoin = "window"`): a solicitation of a detached
  node (the one ending its hold, then the periodic ones) opens an answer
  window of one Imin during which the node selects nothing; at its close
  (`WindowClose`, an event: answers may come before or after it) the node
  takes the candidate of lowest path cost heard after the solicitation
  ending its hold (no switch threshold at a rejoin); with none, the first
  candidate heard afterwards. A detached node that adopts another version
  in its window keeps the window and the rejoin rule (candidates heard
  after the solicitation). `"first"` (the code): selection at every frame
  heard, the first candidate is taken. `"window_rejoin"` (rejected): the
  window for the rejoin only, an adoption selects at once. `Best = TRUE`
  tracks the window as ghost variables (`wh`: neighbours heard in it, `rj`:
  rank taken at a rejoin in it), with `"first"` too, for `RejoinBest`.
- **Rank test of the other candidates** (`Thr`): a candidate other
  than the preferred parent must have a rank strictly below `"rank"` (the
  code before the fix: the node's current rank, set by its last selection,
  on air or not), `"air"` (the rank of the node's last frame), `"minair"`
  (the lowest of both) or `"low"` (**retained**, the code: L, the lowest
  rank the node has had since it attached or adopted its version, never
  above its current rank).
  `IncAdv = TRUE` (rejected) is the idealised limit of "a rank increase is
  advertised at once": the node's frame with the new rank is the very next
  step; `FALSE`, the code, leaves it to any later moment (a Trickle
  advertisement or the next presence), which is also what an advertisement
  queued at once is in this event abstraction.
- **Root kept after exhausted attempts** (`Exhaust = "keeproot"`, the
  code, with a random retry delay against lockstep retries): the root is
  not dropped while it is heard and its link eligible; the copy gets fresh
  attempts towards it. `"keeplast"` (variant): the preferred parent, root
  or not, is kept while it is the last candidate. `"keepboth"` (remedy A
  extended to relays, the code): both. `"drop"`: the code
  before.
- **Root link unreachability** (`Elig = "reach"`, remedy A of D28, the
  code): the link to the root leaves the candidates only after a
  run of consecutive failed samples with no acknowledgement of the root
  (and, in the code, a silence limit without one), instead of ETX > 4
  (`"etx"`, the code before). `"none"`: the earlier model, ineligible only after
  the link's death. `"reachetx"`: the code of A, relay links still by
  ETX ≤ 4. `"reachall"` (A extended to relays): every link by
  reachability; the code limits it to the preferred parent while it is
  the last candidate (open points).
- **Root sequence with feasibility** (`Seq = "babel"`, remedy C, to be
  implemented): candidates other than the preferred parent feasible in
  Babel's sense against the root sequence and L, L and the sequence kept
  across a detach; ungrounded nodes (detached, or floating) hold their
  upward messages. `"none"`: the code, L restarting at each rejoin.

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
| Partition | a relay holding a message when it is cut off fails it after the holding; its error cannot reach the origin, cut off too (accepted as an MP5 exception: with no link, no failure can travel; class 2 recovers it) | `DODAG_line_partition_MP5Live` |
| Non-vacuity (frames, refined) | loops form with 2 consecutive losses even with both fixes; messages delivered, errors received, rank errors detected | `DODAG_subtree_frame_witness_*` |

Rank following and DODAG versions, on the code since fixes 3 and 5
(refined custody, solicitation, acknowledgements as frames):

| Item | Result | Configurations |
|---|---|---|
| Rank following, current design | no loss needed: relay 1 dies, 3 falls back on 2 at rank 3 (link cost 2), 4 (rank 3, only neighbour 3) drops 3, no longer strictly below it, and detaches although 3 stays attached; its subtree follows | `DODAG_follow_current_keep` |
| Rank following (fix) | 4 follows 3 (rank 3 → 4); no loop with at most 1 consecutive loss per link, no silent loss with 2 losses in any pattern, no detach while the parent stays attached within the bound (follow, subtree); MP5Live, MP10, LoopsEnd, RanksSettle, RoutesSettle (follow ≤ 2 losses, subtree ≤ 1, line ≤ 2 without MP5Live) | `DODAG_follow_follow_*`, `DODAG_subtree_follow_*`, `DODAG_line_follow_live` |
| Rank following, loops | with 2 consecutive losses loops still form; the rank rule no longer breaks them (each member follows the other's increase): a loop ends by a switch to a candidate better by the threshold (subtree: 3 to 4) or when a member's path cost exceeds L + MaxInc and it detaches (line: ranks 3 → 6, then 3 detaches) | `DODAG_subtree_follow_witness_loop`, `DODAG_line_follow_witness_bound` |
| Version lag, current design | 1 loss: the root issues version 1, 1 adopts it from the beacon; 2 hears a presence of 1 in version 1, is behind and solicits; 1's DODAG information (its only one in the 2 Imin, Trickle restarted by the solicitation) is lost; 2 detaches, 3 follows (poison). Without loss no node detaches | `DODAG_line_versions_lag`, `DODAG_line_versions_noloss` |
| Adoption during the hold, current design (found by TLC) | no loss: 3 adopts version 1 from 1, which dies before 3 hears 2 in version 1; 3 detaches; during its hold its child 4, still in version 0, advertises; 3 adopts version 0 from it (any version when detached, the hold ended) and attaches through its own child (loop 3 → 4 → 3). Without loss the advertisement must fall between the detach and the poison (≤ 4 slots); with the poison lost by 4, anywhere in the 2 Imin hold | `DODAG_diamond_versions_adopt` |
| All three fixes, two versions | no node detaches during a global repair (NoLagDetach, KeepParent); no loop with at most 1 consecutive loss per link; no silent loss (line ≤ 2 losses any pattern, diamond with relay death ≤ 1); MP5Live (line), MP10, LoopsEnd, RanksSettle, VersionSettle, RoutesSettle (line ≤ 2 losses, diamond with relay death ≤ 1, no message) | `DODAG_line_keep_*`, `DODAG_diamond_keep_*` |
| Rejected: no detach behind a version, adoption at once | the adoption loop remains, as in the code (the loss in the trace plays no part) | `DODAG_diamond_keep_adoptany` |
| Non-vacuity (versions) | a node lags behind the new version attached through a parent of that version, and its class 1 message is taken across versions | `DODAG_line_keep_witness_*` |

Rejoin after the solicitation, on the code since the version fixes (rank
following, no detach behind a version, adoption after the hold):

| Item | Result | Configurations |
|---|---|---|
| Rejoin, current design | no loss: relay 1 dies, 2 detaches and solicits; 4 answers first and 2 rejoins through it (path cost 4); 3 answers in the window (path cost 3), one hop better, below the switch threshold: 2 stays on the worse path | `DODAG_rejoin_current_best` |
| Answer window (fix) | the rejoin takes the best candidate heard in the window; no loop with at most 1 consecutive loss per link, no silent loss with 2 losses, no detach while the parent stays attached (rejoin graph, one version; subtree graph); with two versions, adoption in the window included, no loop, no detach behind a version (diamond); MP5Live, MP10, LoopsEnd, RanksSettle, CutOffLive, RoutesSettle (rejoin ≤ 2 losses, subtree ≤ 1), VersionSettle with versions (diamond ≤ 1, no message) | `DODAG_rejoin_window_*`, `DODAG_subtree_window_*`, `DODAG_diamond_window_*` |
| Rejected: window for the rejoin only | a detached node adopting the new version in its window selects at once: it takes the first answerer of that version (4), and 3 answers after | `DODAG_rejoin_windowrejoin_best` |
| Non-vacuity (window) | a node rejoins at the close of its window | `DODAG_rejoin_window_witness_rejoin` |

Link cost rises, on the code since the answer window, `Rise` hops
of cost rise overall:

| Item | Result | Configurations |
|---|---|---|
| Loop after a link cost rise, current design | no loss, chain 0-1-2, 4 hops of rise on 1's link to 0: at a frame of 0, 1 reads 2 of them and its rank goes 1 → 3, not put on air; after 2 more, the path through 0 (5) passes the bound L + MaxInc = 4; at the next frame of 0, 1 drops 0 and takes 2, heard at its old rank 2 < 3 (path cost 3): loop 1 → 2 → 1, 2 having heard nothing of 1 since (3 hops: no loop). Siblings, subtree graph, 1 hop of rise and relay 1's death, no loss: 1's rank rises to 2 and goes on air, 2 and 3 follow (rank 3) without sending it; 1 dies and each takes the other, seen at 2 < 3 (loop 2 → 3 → 2), the case seen in the simulator. Fewest hops of rise for a loop without loss with relay 1's death: line 2, follow 3, diamond and rejoin 4 | `DODAG_chain_current_loop`, `DODAG_subtree_current_rise` |
| Rejected: rank increase advertised at once | in the event model an advertisement queued at once still leaves the window between the increase and its frame: the trace above (1's frame is due, 0's frame comes first). At its idealised limit (`IncAdv`: the frame is the very next step, which no radio gives) no loop without loss, a loop with one loss (the advertisement lost by 2, which keeps rank 2) | `DODAG_chain_current_loop`, `DODAG_chain_advertise_nocycle`, `DODAG_chain_advertise_loss` |
| Rejected: test against the rank on air, or the lowest of it and the current rank | no loss: 1 puts rank 4 on air, 2 follows (rank 5) but has not sent it; 1 still hears 2 at 2 < 4 and takes it. A child's rank comes from a rank the node sent, but the node may hear an older rank of the child still: the last rank sent bounds nothing | `DODAG_chain_air_loop`, `DODAG_chain_minair_loop` |
| Retained: test against L | no loop with at most 1 consecutive loss per link: chain, Rise 4, ≤ 2 losses (12 790 states, 7 s); line, relay 1 dies, a message, Rise 2, ≤ 2 losses, also no silent loss and no detach while the parent stays attached (2 986 432 states, 23 min); subtree, relay 1 dies, Rise 1, ≤ 2 losses, also KeepParent (722 276 states, 36 min). No loop without loss, relay 1 dies, Rise 4 (checked once, not kept: line 16 s, diamond 2 min, follow 3 min, rejoin 5 min, subtree 3.6 million states, 50 min). Liveness: MP5Live, MP10, LoopsEnd, RanksSettle, CutOffLive, RoutesSettle on the chain, Rise 4, a message, ≤ 2 losses in any pattern (102 365 states, 9 min); LoopsEnd, RanksSettle, MP10Live, CutOffLive, RoutesSettle on the line, relay 1 dies, a message, Rise 2, ≤ 2 losses in any pattern (879 344 states, 22 min) and on the subtree graph, relay 1 dies, Rise 1, ≤ 1 loss (24 930 states, 2 min). With fixed costs, every earlier `DODAG` configuration run with this test (`Thr = "low"`) keeps its expected outcome: the 35 that hold still hold, the 27 defects and witnesses still fail on the same property (loops after two consecutive losses included: the rule does not cover views older than an attach) | `DODAG_*_low_*` |

Late root acknowledgements, on the code since the test against L (`Thr =
"low"`):

| Item | Result | Configurations |
|---|---|---|
| Late acknowledgement ignored (the code before) | no loss at all: the timeouts fire while the root's acknowledgements are on their way, and after 3 attempts a node drops the root | `DODAG_line_lateignore_drop` |
| Late acknowledgement accepted (fix) | no silent loss and the root never dropped with ≤ 2 losses in any pattern (line, relay 1 dies; diamond, relay 1 dies, two messages: 280 s); MP5Live, LoopsEnd, RanksSettle, MP10Live (line, no death; diamond, relay 1 dies; ≤ 2 losses in any pattern) | `DODAG_*_lateack_MP5`, `DODAG_*_lateack_live` |
| Non-vacuity | a retry goes while the acknowledgement of the earlier transmission is still expected | `DODAG_line_lateack_witness_retry` |
| Earlier configurations | given `LateAck = "none"`, the earlier model (state count unchanged where compared: 18 839) | the 74 other `DODAG_*` |

Exhausted attempts and link death, on the code since late
acknowledgements (`LateAck = "accept"`):

| Item | Result | Configurations |
|---|---|---|
| Root dropped after collisions, the code before (`Exhaust = "drop"`) | no failed link, no death: 3's message reaches 1; 1's three transmissions to the root are lost there (hidden terminals colliding again at each retry); 1 drops the root, its only candidate under L, and detaches, its subtree following (910 states, 4 s) | `DODAG_line_drop_rootloss` |
| Root kept (`"keeproot"`) | ≤ 3 losses in any pattern: no silent loss, the root never dropped (line, relay 1 dies: 18 669 states, 10 s; diamond, relay 1 dies, a message of 4: 20 446 states, 56 s); ≤ 3 losses, ≤ 1 consecutive per link: no loop, no rejoin loop, KeepParent (line, relay 1 dies: 84 119 states, 45 s); MP5Live, LoopsEnd, RanksSettle, MP10Live (line, no death, ≤ 3 losses: 1 987 states, 19 s) | `DODAG_*_keeproot_MP5`, `DODAG_line_keeproot_nocycle`, `DODAG_line_keeproot_live` |
| Root kept, dead link | the root is not kept on a dead link: link 0 – 1 dead both ways, the silence limit removes the root; line, ≤ 2 losses: LoopsEnd, RanksSettle, CutOffLive, no silent loss (262 927 states, 9 min); diamond, a message of 4, ≤ 1 loss: also MP5Live and MP10Live, 1 rejoining through 3 (20 765 states, 4 min). Link 1 → 0 dead, 1 still hearing the root: its failed timeouts make the link ineligible; diamond, ≤ 1 loss: MP5Live, LoopsEnd, RanksSettle, no silent loss (4 457 states, 61 s). The root is dropped only over the dead link (`RootKept`) | `DODAG_*_keeproot_cutlive`, `DODAG_diamond_keeproot_cutuplive` |
| Limit, not of the option (the same with `"drop"`) | link 1 → 0 dead and no class 1 traffic through 1: nothing reads the dead link (presence to the root gives no sample), 1 stays attached over it and MP10Live fails | `DODAG_diamond_keeproot_cutup_idle` |
| Last candidate kept (`"keeplast"`) | the same results on the line (MP5: 7 361 states, 8 s; nocycle: 47 324, 37 s; live: 1 157, 17 s; dead link, ≤ 1 loss: 43 997, 82 s) and on the diamond (MP5: 12 593 states, 42 s; dead link both ways: 20 765, 4 min) | `DODAG_*_keeplast_*` |
| Non-vacuity | a copy gets fresh attempts towards the root after its last attempt: after 3 losses (line), or with no loss over the dead link 1 → 0 (diamond) | `DODAG_line_keeproot_witness_fresh`, `DODAG_diamond_keeproot_witness_cutfresh` |
| Earlier configurations | given `Exhaust = "drop"`, `Cut = "none"`, the earlier model (state counts unchanged where compared: `DODAG_line_lateignore_drop` 13, `DODAG_line_lateack_witness_retry` 10, `DODAG_chain_low_nocycle` 12 790, `DODAG_line_lateack_MP5` 6 307, `DODAG_line_lateack_live` 413) | the 80 other `DODAG_*` |

Eligibility of the link to the root, on the code since the root kept
(`Exhaust = "keeproot"`, `LateAck = "accept"`):

| Item | Result | Configurations |
|---|---|---|
| Root link ineligible after collisions, the code before remedy A (`Elig = "etx"`, D28) | no death, no dead link, line, ≤ 4 losses, ≤ 1 consecutive per link: 1's frames to the root lost there and the root's acknowledgements lost at 1, alternately; after 4 failed samples (`INELIG`) 1's link to the root is ineligible (36 683 states, 58 s), and 1 detaches although the root is heard, not dropped and within the bound, its subtree following (`KeepParent`, 44 619 states, 74 s) | `DODAG_line_etx_rootloss`, `DODAG_line_etx_detach` |
| Root link unreachable, remedy A (`"reach"`) | the same budget (line, ≤ 4 losses, ≤ 1 consecutive): the link never unreachable, the root never dropped, KeepParent, no silent loss (52 708 states, 2 min); ≤ 3 losses in any pattern: no silent loss, the root never dropped, its link never unreachable (line, relay 1 dies: 18 674 states, 10 s; diamond, relay 1 dies, a message of 4: 20 481 states, 51 s); ≤ 3 losses, ≤ 1 consecutive: no loop, no rejoin loop, KeepParent (line, relay 1 dies: 84 131 states, 41 s); MP5Live, LoopsEnd, RanksSettle, MP10Live (line, no death, ≤ 3 losses: 1 992 states, 13 s) | `DODAG_line_reach_rootloss`, `DODAG_*_reach_MP5`, `DODAG_line_reach_nocycle`, `DODAG_line_reach_live` |
| Remedy A, dead link | the dead link is still left: link 0 – 1 dead both ways, line, ≤ 2 losses: LoopsEnd, RanksSettle, CutOffLive, no silent loss (262 475 states, 7 min); diamond, a message of 4, ≤ 1 loss: also MP5Live and MP10Live, 1 rejoining through 3 (20 517 states, 4 min). Link 1 → 0 dead, 1 still hearing the root's beacons but no acknowledgement: the run of failed samples makes the link unreachable; diamond, ≤ 1 loss: MP5Live, LoopsEnd, RanksSettle, no silent loss (4 269 states, 42 s). Root dropped and link unreachable only over the dead link (`RootKept`, `RootReachable`) | `DODAG_*_reach_cutlive`, `DODAG_diamond_reach_cutuplive` |
| Limit, not of A (the same with `"none"`) | link 1 → 0 dead and no class 1 traffic through 1: no probe, 1 stays attached over it and MP10Live fails | `DODAG_diamond_reach_cutup_idle` |
| Tightness of `UNREACH` | line, no death, ≤ 3 losses, ≤ 1 consecutive per link: a run of 3 consecutive failed samples on a live link (a frame of 1 lost at the root, then the root's acknowledgement lost at 1, then again): `MaxConsec` bounds no run, `MaxLoss` + 1 is the smallest safe `UNREACH` | `DODAG_line_reach_witness_run` |
| Non-vacuity | a copy gets fresh attempts towards the root after its last attempt: after 3 losses (line), or with no loss over the dead link 1 → 0 (diamond) | `DODAG_line_reach_witness_fresh`, `DODAG_diamond_reach_witness_cutfresh` |
| Earlier configurations | given `Elig = "none"`, the earlier model (state counts unchanged where compared with one worker: `DODAG_line_drop_rootloss` 727, `DODAG_line_lateignore_drop` 13; with all workers: `DODAG_line_lateack_witness_retry` 10, `DODAG_line_lateack_MP5` 6 307, `DODAG_line_lateack_live` 413, `DODAG_line_keeproot_MP5` 18 669, `DODAG_line_keeproot_live` 1 987, `DODAG_diamond_keeproot_cutuplive` 4 457, `DODAG_line_custody_MP5` 19 706) | the 97 other `DODAG_*` |

Remedy A extended to relays (`Exhaust = "keepboth"`, `Elig =
"reachall"`), on the code of A (`LateAck = "accept"`):

| Item | Result | Configurations |
|---|---|---|
| Relay dropped after collisions, the code of A (`"keeproot"`, `"reachetx"`) | no death, no dead link, line, 3 losses: 3's three transmissions to relay 2 lost there; 3 drops 2, its only candidate under L, and detaches although 2 is alive, heard, its link live (`LastKept`, 462 states, 3 s): the start of the star collapse in the large-network suite | `DODAG_line_reachetx_relaydrop` |
| Last candidate kept, relay links by ETX (`"keepboth"`, `"reachetx"`) | line, ≤ 4 losses, ≤ 1 consecutive per link: 3's frames lost at 2 and 2's acknowledgements lost at 3, alternately; after 4 failed samples 3's link to 2 is ineligible and 3 detaches, with no drop (`LastKept`, 35 944 states, 69 s): keeping the relay is not enough while its link is judged by ETX | `DODAG_line_keepbothetx_detach` |
| Last candidate kept, relay links always eligible (`"keepboth"`, `"reach"`) | line, the link 2 → 1 dead, 2 still hearing 1, no loss: nothing removes 1, 2 retries 3's message towards it for ever (`MP5Decided`, 40 states, 2 s): keeping the last candidate needs a test that a dead uplink fails | `DODAG_line_keepboth_relaycut` |
| A extended to relays (`"keepboth"`, `"reachall"`) | the budget of the ETX witness (line, ≤ 4 losses, ≤ 1 consecutive): no link unreachable, the root never dropped, `LastKept`, `KeepParent`, `KeepOnlyLast`, no silent loss (36 381 states, 88 s); ≤ 3 losses in any pattern, relay 1 dies: no silent loss, the root never dropped, no live link unreachable, `LastKept`, `KeepOnlyLast` (line: 51 114 states, 22 s; diamond, a message of 4: 19 386 states, 63 s); ≤ 3 losses, ≤ 1 consecutive, relay 1 dies: no loop, no rejoin loop, `KeepParent`, `LastKept`, no live link unreachable (line: 206 281 states, 86 s; diamond, a message of 4: 250 804 states, 10 min, the diamond no-cycle check `"keeplast"` lacked); MP5Live, LoopsEnd, RanksSettle, MP10Live (line, no death, ≤ 3 losses: 1 178 states, 14 s) | `DODAG_line_reachall_relayloss`, `DODAG_*_reachall_MP5`, `DODAG_*_reachall_nocycle`, `DODAG_line_reachall_live` |
| A extended, dead links | link 0 – 1 dead both ways: line, ≤ 2 losses: LoopsEnd, RanksSettle, CutOffLive, no silent loss (370 379 states, 12 min); diamond, a message of 4, ≤ 1 loss: also MP5Live, MP10Live (23 913 states, 17 min). Link 1 → 0 dead: diamond, ≤ 1 loss: MP5Live, LoopsEnd, RanksSettle (4 897 states, 61 s). Relay uplink dead, the relay still heard: line, 2 → 1, ≤ 1 loss: the message delivered or failed (`MP5Decided`; the partition keeps its error from 3), LoopsEnd, RanksSettle, `CutOffLiveTraffic`, `LastKept` (3 675 states, 34 s); diamond, 3 → 1, a message of 4, ≤ 1 loss: MP5Live, LoopsEnd, RanksSettle, `LastKept` (1 035 states, 19 s). A link unreachable only over the dead link (`LinksReachable`) | `DODAG_*_reachall_cutlive`, `DODAG_diamond_reachall_cutuplive`, `DODAG_*_reachall_relaycut` |
| Keep not sticky | `switch` graph, 6 losses: 3 drops 1 after 3 failed attempts and falls back on 2 (path cost 4), keeps 2 after 3 more as its last candidate, then hears 1 again (no longer dropped) and switches to it, 2 hops cheaper; the kept copy follows with fresh attempts (`NoSwitchAfterKeep`, 9 655 states, 29 s). Without a cheaper path the next exhaustion drops the kept parent instead (`KeepOnlyLast`, checked above) | `DODAG_switch_reachall_witness_switch` |
| Tightness of `UNREACH` on relay links | line, ≤ 3 losses, ≤ 1 consecutive: a run of 3 consecutive failed samples towards relay 2 on a live link (4 313 states, 6 s) | `DODAG_line_reachall_witness_run` |
| Non-vacuity | a copy gets fresh attempts towards a relay after its last attempt: after 3 losses (line, 478 states), or with no loss over the dead uplink 2 → 1 (42 states) | `DODAG_line_reachall_witness_fresh`, `DODAG_line_reachall_witness_relaycutfresh` |
| Earlier configurations | given `"none"`, `"etx"`, `"reach"` and no relay cut, the earlier model (`rfail` indexed by link, only the root column moves; `KeepParent`'s exception for a link made ineligible over a dead link is reached only with `"reachall"`): state counts unchanged where compared (`DODAG_line_drop_rootloss` 727 and `DODAG_line_lateignore_drop` 13 with one worker; `DODAG_line_lateack_MP5` 6 307, `DODAG_line_lateack_live` 413, `DODAG_line_keeproot_MP5` 18 669, `DODAG_line_keeproot_live` 1 987, `DODAG_diamond_keeproot_cutuplive` 4 457, `DODAG_line_custody_MP5` 19 706, `DODAG_line_reach_MP5` 18 674, `DODAG_line_reach_live` 1 992, `DODAG_diamond_reach_cutuplive` 4 269, `DODAG_line_keeplast_MP5` 7 361, `DODAG_diamond_keeplast_MP5` 12 593, `DODAG_line_reach_nocycle` 84 131, `DODAG_line_reach_rootloss` 52 708); outcomes unchanged for `DODAG_line_etx_detach`, `DODAG_line_etx_rootloss`, `DODAG_line_reach_witness_run`, `DODAG_diamond_reach_witness_cutfresh` | the 111 other `DODAG_*` |

Root sequence with feasibility (remedy C, `Seq = "babel"`), on the code of
A extended to relays (`Exhaust = "keepboth"`, `Elig = "reachall"`,
`LateAck = "accept"`), one root sequence increment (`MaxSeq = 1`):

| Item | Result | Configurations |
|---|---|---|
| Rejoin through a stale node, the code (`"none"`, the sequence a ghost) | `star`, relay 1 dies, no loss: 2 drops the silent relay and detaches, its hold ends with its solicitation, 3 answers at rank 2 (its own parent, the dead relay, not yet silent for it) and 2 rejoins through it at rank 3: a rejoin into a part cut off from the root, through a node knowing nothing newer of the root (6 states; 135 states, 2 s). Repeated as each node's silence limit passes, L restarting at each rejoin, it is the rootless DODAG counting up of the simulator's star. `star2`, no loss: relay 2 dies, the uplink 1 → 0 dies, 1's message probes it; after `UNREACH` failed samples 1 leaves the root, detaches, solicits, and rejoins through Node 3, still under the dead relay 2, at rank 4 (16 states; 4 363 states, 7 s): the relay hearing the root directly joins the stale Node | `DODAG_star_reachall_stalerejoin`, `DODAG_star2_reachall_stalerejoin` |
| Loops on views older than an attach, the code | line, relay 1 dies, 2 losses on one link: 2 detaches, 3 loses both its poison and its solicitation, 2 rejoins through 3 (loop 2 → 3 → 2; 6 states; 127 states, 2 s) | `DODAG_line_reachall_consec2` |
| Remedy C, safety | the losses below in any pattern (no separate bound on consecutive ones), one root sequence increment (`MaxSeq = 1` in every safety configuration): no loop, no rejoin loop, `NoStaleRejoin` (no rejoin from rank 255 through a stale node at the same sequence and below L, a case these graphs do not offer), no silent loss, the root never dropped, no live link unreachable, `LastKept`, `KeepOnlyLast` (and `KeepParent` on line and star). `star`, relay 1 dies, a message of 3, ≤ 2 losses (377 311 states, 4 min 14 s); line, relay 1 dies, a message of 3, ≤ 3 losses (538 462 states, 7 min 10 s); `star2`, relay 2 dies and the uplink 1 → 0 dies, a message of 1, ≤ 1 loss: relay 1 stays detached rather than joining Node 3 at the same sequence (286 352 states, 9 min 11 s); diamond, relay 1 dies, a message of 4, ≤ 1 loss (165 474 states, 7 min 43 s; with ≤ 2 losses stopped after 7 min, beyond the time budget, no violation in the 188 487 states explored) | `DODAG_*_babel_safety`, `DODAG_diamond_babel_MP5` |
| Remedy C, liveness | line, no death, a message of 3, ≤ 3 losses in any pattern: MP5Live, LoopsEnd, RanksSettle, MP10Live, no live link unreachable (10 244 states, 2 min 43 s); `star`, relay 1 dies, a message of 3, ≤ 1 loss: the message ends delivered or failed (`MP5Decided`, the partition exception), LoopsEnd, RanksSettle, CutOffLive (102 898 states, 10 min); diamond, the uplink 1 → 0 dies, the root sequence incremented without end, a message of 4, no loss: MP5Live, LoopsEnd, RanksSettle, `NoStaleRejoin` (59 758 states, 11 min; renormalisation counts no view of the root, which selects no parent: with its view of 1 over the dead uplink counted, the root stops issuing sequences and the run violates MP5Live, 1 never seeing a sequence newer than its own). With `MaxSeq = 1` (line, `star`) a node ungrounded once the single increment has reached it never grounds again, so MP5Live (`MP5Decided`) holds through its held messages failing; delivery after grounding again is checked only by `DODAG_diamond_babel_cutuplive` (`MaxSeq = 2`) | `DODAG_*_babel_live`, `DODAG_diamond_babel_cutuplive` |
| Non-vacuity | `star`, no loss: an ungrounded node keeps its parent and holds the message (128 states, 2 s) | `DODAG_star_babel_witness_floathold` |
| Limits | a cut-off neighbour at the node's sequence and below its L stays feasible (MESH_DESIGN §9: a Node at depth 3 hearing a depth-2 Node cut off with it, a sibling of its parent; no configured graph has one): within one sequence a node moves only below its L, which never rises, so ranks do not count up, and the structure dissolves as its nodes float (G, the floating flag), which this untimed model does not check; a dead parent is feasible by sequence and rank, excluded only by the rejoin rule (neighbours heard after the solicitation); `NoStaleRejoin` checks rejoins from rank 255 only, not parent switches without a detach; a cut-off neighbour with a newer sequence (it heard the dead relay once more) stays feasible at any rank: `NoStaleRejoin` excepts it, at most one such attachment per distinct newer sequence of the cut-off part (the node takes the parent's sequence), ending as that part floats or at a silence limit; the bound of the hold counted from the loss of grounding (a time property) and the floating flag are outside the untimed model, which grounds a node through a floating parent's newer sequence (the design does not, and the hold bound rests on it); `SeqVals` takes in the sequences of dead nodes and the views of dead or cut-off neighbours, never compared again: with a death and `MaxSeq` ≥ 2 the renormalisation stops after at most `MaxSeq` steps and the fairness on `NewSeq` becomes empty (no current configuration is affected; one with a death and `MaxSeq` ≥ 2 would be); with values in 0..`MaxSeq` no node is ever more than `MaxSeq` behind, so wrap-around and the forget rule are not exercised; with `MaxSeq` = 1 the increments run out: a node grounded again only by a newer sequence then fails its held messages (allowed by MP5Live) instead of delivering them, and a relay left detached over its dead uplink at the root's last sequence never sees a newer one from its former child and never rejoins, so its error never travels (`DODAG_diamond_babel_cutuplive` with `MaxSeq` = 1 violates MP5Live; with endless increments, `MaxSeq` = 2, the relay rejoins through 3 once 3's sequence passes its own) | — |
| Earlier configurations | given `Seq = "none"`, `MaxSeq = 0`, the earlier model: state counts unchanged where compared (`DODAG_line_lateack_MP5` 6 307, `DODAG_line_reach_MP5` 18 674, `DODAG_line_keeplast_MP5` 7 361, `DODAG_line_reachall_MP5` 51 114, `DODAG_line_reachall_live` 1 178, `DODAG_diamond_reachall_cutuplive` 4 897, `DODAG_line_reachall_witness_relaycutfresh` 42, `DODAG_line_keepboth_relaycut` 40, `DODAG_line_solicit_consec2` 127) | the 120 other `DODAG_*` |

**Why L.** Every rank a node puts on air since it attached is at least
L; a child's rank is the rank it heard from its parent plus a link cost
(≥ 8, more than the hysteresis of 3 it may lag), so it is strictly
above every rank of its parent it ever used. A node switching to a
candidate c has `rank(c) < L(node)`, and c's ranks since its attach are
≥ L(c), so `L(c) ≤ rank(c) < L(node)`: down any chain of preferred
parents, the ranks a descendant advertised since its attach are above the
L of each of its ancestors, and an ancestor testing against its L never takes it,
whatever was lost or not yet sent, as long as no view predates the
ancestor's attach (fix 5: one consecutive loss tolerated). This is the
feasibility condition of DUAL (EIGRP), L playing the feasible distance and
the detach-hold-rejoin its active state. The current rank fails because it
rises without the descendants knowing; the rank on air fails because the
descendants' ranks reach the node later than the node's rank reaches them.
Cost: a node whose rank has risen above L moves only to a neighbour below
L, otherwise it follows its parent (to the bound) or detaches and rejoins,
L restarting. The first local repair after a parent loss is unchanged
when the node was at its lowest rank (its current rank is then L).

**Loop bound (MP11).** With fix 5 and at most one consecutive loss per link
no loop forms (checked on the line and the subtree graph) as long as link
costs do not rise; with rises, only with the candidates tested against L
(above). Beyond, a loop
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

**Loop bound with rank following.** Following the preferred parent creates
no loop by itself (no loop with at most one consecutive loss, follow and
subtree graphs, and with versions on the line and diamond), but it removes
the rank rule's break above: in a loop formed by two consecutive losses,
each member follows its parent's increase. The loop then ends by a switch
to a candidate better by the threshold, or by the bound: ranks climb one
link cost per advertisement heard until one member exceeds L + 56 and
detaches (the RPL count-to-bound). With traffic, data-path validation
advertises at once at each turn; idle, each increase resets Trickle, so
the climb of 56 / 8 = 7 hops takes about 7 Imin (11 s at SF7, 37 s at SF9).

**Versions and loops.** A node takes parents of its own version only, and
a lagging node keeps only a parent of a newer version: while views are
current, versions never decrease up a chain, so a lagging node kept on its
parent cannot close a cycle. The cycle found comes from the other
direction: a detached node adopting an older version from its own stale
subtree during its hold. Adopting only after the hold (after the
solicitation, a second poison) removes it with at most one consecutive
loss per link, as fix 5 does for rejoins in one version.

## From the models to the code

How each fix is implemented, what it costs, and the limits accepted.

- **Fix 3 in the code**: implemented with the refined rule
  (`Reliable.holding(key, source)` in `LinkEngine._receive` and
  `Routing._forward`; a duplicate taken again is acknowledged explicitly),
  which the model checks with acknowledgements as frames. The first rule
  (any copy held) is unsound as soon as acknowledgements are not atomic.
  Cost: a lost acknowledgement whose retransmission reaches a relay that has
  already handed the message over makes one duplicate travel the rest of the
  path (filtered by the root). MESH_DESIGN §9 (data-path validation) and §12
  (duplicate cache) describe it.
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
  (detach) describes it; the poison is still sent once (a lost one is
  covered by the solicitation that ends the hold).
- **Partition**: a relay cut off from the root while holding a class 1
  message fails it after 2 periods; its error is held then dropped (an error
  about an error is never generated): the origin, cut off as well, never
  learns. Accepted as an exception to MP5 (MESH_DESIGN §2, §12): with no
  link at all no failure can travel; class 2 recovers it.
- **Code changes for rank following**: `Parents.candidate_cost`
  (parents.py:162): the rank test `n.rank >= self.rank` does not apply to
  the preferred parent (`n.address == self.preferred`); its path cost stays
  bounded by min(254, L + max_increase) (parents.py:172-173), so it leaves
  the candidates, and the node detaches without another one, only beyond
  the bound. `_select` needs no change: a followed rise is at least one link
  cost (≥ 8, above the hysteresis of 4, parents.py:307), the rank increase
  resets Trickle (`RANK_INCREASED`) and the members not below the new rank
  leave the set (parents.py:320-323). Cost: a loop formed despite fix 5 (two
  consecutive losses) is no longer broken by the rank rule but by a switch
  or by the bound (above). MESH_DESIGN §9 (candidates, bounded rank
  increase) excepts the preferred parent from the rank test.
- **Code changes for no detach behind a version**: `Dodag._heard`
  (dodag.py:393-398) marks the node behind without a 2 Imin limit
  (`BEHIND_IMIN`, dodag.py:123, and the end of `_behind_until` at
  dodag.py:497-499 go); `Dodag._routing` (dodag.py:508) no longer pauses the
  selection while behind; `Parents` gets a behind flag (set by `Dodag`,
  cleared by `on_new_version` and `_detach`), and `candidate_cost`
  (parents.py:162) accepts, while behind, the preferred parent whose entry
  has a newer version (finite rank, heard within the silence limit, not
  dropped) at the node's own rank (`self.rank`, no comparison across
  versions); the solicitation (`_ask`, dodag.py:401-407) repeats with a
  backoff (Imin, doubling, capped at the presence period) until the node
  adopts. Forwarding across versions is unchanged (routing.py:146).
- **Code changes for adoption after the hold**: `Dodag._on_info`
  (dodag.py:441): a detached node adopts another version only when
  `Parents` is neither in its hold nor waiting for its solicitation
  (`hold_until is None and not soliciting`); `Parents.on_new_version`
  (parents.py:223-225) then no longer ends a hold. MESH_DESIGN §9 (loop
  freedom, across versions) states the rule.
- **Code changes for the answer window**: `Parents.solicited`
  (parents.py:225-237) opens a window, `window_until = now + window_ms`
  (Imin + 8 slots, set by `Dodag._apply_info` beside `hold_ms`), for
  every solicitation of a detached node that goes on air (`Dodag._sent`,
  dodag.py:420-429: the one ending the hold, the periodic ones of
  dodag.py:529-533, the boot one), and only the one ending the hold sets
  `poisoned_at`; `Parents._select` (parents.py:302-313) returns no event
  while the node is detached and `now < window_until`, then selects (at a
  rejoin the lowest path cost wins: `best[0]`, no switch threshold, as
  now); `Dodag._watch` (dodag.py:553-566) and `_routing` (dodag.py:523-524)
  wake the selection at `window_until`, as at `hold_until`.
  `Parents.on_new_version` (parents.py:239-272) keeps the window (its
  `_select` honours it), so that an adoption in the window waits for its
  close. At boot the first DODAG information is still adopted at once (the
  parameters), the selection waits for the window of the boot
  solicitation (not modelled: the model has no boot). The rejoin comes at
  the close instead of the first answer. A Trickle answer leaves within
  [Imin/2, Imin) of its reset, plus its desync and time on air: a window of
  exactly Imin can miss the latest answers (those after the close are taken
  only by the switch threshold), hence the 8 slots of margin, which the
  detach + rejoin time of MESH_DESIGN §9 includes (detach; Trickle,
  solicitation).
- **Code changes for the loop after a link cost rise**: `Parents.candidate_cost`
  (parents.py:204, 211): the rank test of a candidate other than the
  preferred parent compares with L, ``lowest`` (never above ``rank``;
  detached, ``lowest`` is None and any finite rank passes, as before);
  `_select` (parents.py:385-388): the secondary members kept are those
  below the new ``lowest`` (set at parents.py:381-383), not below
  ``rank``, so that the set never holds a member the next selection
  would refuse. No new state, nothing on air, no change to Trickle: the
  rule holds whatever is suppressed or lost. MESH_DESIGN §9 (bounded rank
  increase, candidates) states it with L instead of the current rank.
  Simulator (`make large RUN="relay-death star 1"`, a diagnostic outside
  the repository): every one of the 31 switches that closed a cycle took a
  neighbour heard at a rank not below the node's L (often two siblings at
  the same L, both risen, taking each other), 29 of them with the node's
  rank above the rank of its last frame; with the test against L patched
  in, no cycle on that run nor on `lossy geometric 1` (10 before); on
  `relay-death star 2` the 25 cycles became 4, all from one rejoin through
  a node that still saw the rejoining node at its rank from before its
  detach (residual below). Class 1 delivery fell on these runs
  (`lossy geometric 1`: 289 → 282 delivered; the star runs, failing already
  under the hidden-terminal collapse, also lost messages silently and broke
  MP10 once): the restriction costs fewer local repairs and more
  detaches, a cost accepted for loop freedom; the rule is now in the
  code. Host tests:
  `BelowLowest` (tests/test_parents.py) and `LinkCostRise`
  (tests/test_dodag_scenario.py: on the scripted line, B's frames lost at
  A only, B's class 1 retries raise its ETX in bursts; with the test
  against the current rank, B took C, heard at its rank from before it
  followed B's increase, and the cycle B → C → B formed on 41 of 56 seeds
  tried, none with the test against L).
- **Code changes for late root acknowledgements**: `Reliable`
  (reliable.py) keeps, per queue slot, the frame counters of the
  message's last 3 transmissions to the root (`_record`); an
  acknowledgement from the root of any of them removes the message
  whatever its state (`on_ack`: waiting, queued for its retry, held, or
  waiting after the last attempt). The retry still goes at the timeout
  (since then after a random delay, below).
  A frame's failed sample towards the root is decided one root timeout
  after its timeout (`_defer`, `_decide`, then the one failed sample per
  batching delay rule), and an acknowledgement of that frame arriving
  before cancels it and counts as a success (`_late`); after the last
  attempt the message waits one more root timeout (state `_LATE`) before
  the next hop after failure. Cost: per queue slot 3 counters, plus a
  table of one pending failure per slot; the root is dropped one root
  timeout later after a real loss (MESH_DESIGN §9 repair table), a real
  loss is read one root timeout later, and real-loss recovery is not
  slowed (the retry is unchanged). MESH_DESIGN §12 (late root
  acknowledgement) describes it; host tests `LateRootAcknowledgements`
  and `RootSamples` (tests/test_reliable.py).
- **Code changes for the root kept** (`Exhaust = "keeproot"`):
  `Parents.next_parent` (parents.py) returns the root instead of dropping
  it while `candidate_cost(n, now, drops=False)` holds (heard within the
  silence limit, eligible link, of the node's version or kept behind it,
  within the bound: `Usable`); `Reliable._exhausted` (reliable.py) gives
  the message fresh attempts when the next hop after failure is the hop
  itself (`KeepHop`), still after the `_LATE` wait. Any other hop is
  dropped as before. With it, a **random retry delay**
  (`Reliable._retry`, uniform in [0, w), one draw per timed-out frame,
  state `_DELAY`; w = 32 slots doubled per frame of the node timed out
  since its last acknowledgement, that one included, at most 3 times):
  timing only, outside the model; the timeouts and the deferred failure
  decisions stay anchored on the frame's end. Cost: a failed link to the
  root is left at its ineligibility (13 failed samples from a full
  window) or at the silence limit, later than after 3 attempts
  (MESH_DESIGN §9 repair table); every retry waits up to 64 to 256 slots
  more. MESH_DESIGN §12 (retry delay, retry backoff, upward failure)
  describes it; host tests `KeepRoot` (tests/test_parents.py),
  `RetryDelay`, `RetryBackoff`, `KeptNextHop` (tests/test_reliable.py), and
  `LocalRepair.test_root_kept_after_exhausted_attempts_until_its_link_is_unreachable`
  (tests/test_routing.py).
- **Code changes for A on relays** (`Exhaust = "keepboth"`, `Elig =
  "reachall"`, implemented with reachability limited to the last
  candidate): `Parents.next_parent` (parents.py) also returns the
  preferred parent instead of dropping it when it is still a candidate
  apart from the drop and no other neighbour is a candidate
  (`Parents.last`: `LastCand`), decided at each exhaustion, never
  remembered (`KeepOnlyLast`, `DODAG_switch_reachall_witness_switch`);
  `Reliable._exhausted` then gives fresh attempts as for the root.
  `Parents.candidate_cost` judges the preferred parent's link by
  reachability alone, without ETX ≤ 4, while it is the last candidate
  (the other candidates first, by the ordinary rule), its link cost
  capped at 32 (`Neighbour.link_etx`). Taking a relay as the preferred
  parent restarts its reachability (`NeighbourTable.restart_reach`, run
  and acknowledgement time as at the entry's creation), outside the model
  (no time there): a link back after a cut, or left unreachable at
  ETX ≤ 4, would otherwise be taken by the ETX rule at the rejoin and
  left at the next selection, before its first probe is acknowledged
  (`make seeds`: `AdoptionDuringTheHold` on 4 of 24 shifts; requiring
  reachability on every link instead fails `AdoptionDuringTheHold` and
  `LoopCustody` on 6). The run and the acknowledgement time
  were already kept per neighbour; every acknowledgement of a neighbour
  ends its run: a success sample (explicit or passive), an explicit entry
  from any neighbour whatever the frame (`Reliable.on_ack`), a passive
  one whose frame was sampled already (`Reliable._acknowledged`).
  Without reachability on relay links a relay kept as the last candidate
  over a one-way dead uplink is retried for ever
  (`DODAG_line_keepboth_relaycut`); with relay links still by ETX ≤ 4,
  collisions make them ineligible and detach the node
  (`DODAG_line_keepbothetx_detach`). Host tests `KeepLast`
  (tests/test_parents.py), `RelayReachability` (tests/test_neighbours.py),
  `RelayRun` (tests/test_reliable.py),
  `LocalRepair.test_only_relay_parent_kept_after_exhausted_attempts_until_its_link_is_unreachable`
  (tests/test_routing.py), `PresenceAcknowledgement`
  (tests/test_reports.py).
- **Code changes for remedy C** (`Seq = "babel"`, to implement): the root
  increments an 8-bit sequence at each beacon (`Dodag`); port 9 DODAG
  information and the node's own presence (always in mode 6, also beside
  a bundle of forwarded reports) carry the sequence the node holds
  (`reports.py`, `dodag.py` codecs; MESH_DESIGN §11), a solicitation
  none; the sequence is read from every frame heard, overheard ones
  included (today a frame addressed to another node only feeds
  `overheard`, passive acknowledgements, `link.py` receive path: the
  parent's presence goes to the grandparent, a neighbour's to its own
  parent, so a node at hop 2 or more would never read it and would float
  after G), and the neighbour table keeps each neighbour's last sequence,
  the parent's included (`neighbours.py`); `Parents` keeps
  the sequence of its preferred parent, takes a newer one from the
  parent's frame unless the frame carries the floating flag (a floating
  neighbour's sequence grounds no one) and then restarts ``lowest``, keeps both across
  `_detach`, and `candidate_cost` tests feasibility (newer sequence at
  any rank within 254, or the same and a rank below ``lowest``; never
  older; the preferred parent followed as now) and excludes neighbours
  heard with the floating flag; grounding: ungrounded while detached, or
  once the sequence has not advanced for (2.5 + min(8, ⌈rank / 8⌉))
  presence periods, or while the preferred parent's frames carry the
  flag; header bit 0 set while ungrounded (`frame.py`); `Reliable` holds
  upward class 1/2 messages while ungrounded (no forward, no retry, no
  drop of the parent; arrivals held and acknowledged explicitly) and fails
  them 2 presence periods after the loss of grounding, the deadline not
  restarted by an arrival nor a rejoin, cleared when grounded again; a
  sequence not advanced for 50 periods (at most 111 increments) is
  forgotten with ``lowest``; the
  root at boot takes the sequence after the newest heard while it
  listens; a new DODAG version restarts the comparison. What the model
  does not check and the simulator must: the hold bound (a message in a
  rootless part is delivered or failed within about 2 G + 2 periods of
  the cut for the Nodes of a dead relay, one more G per further frame of
  the relay lost, MESH_DESIGN §12, whatever the hand-overs), the
  dissolution of a cut-off structure as its nodes float, the absence of
  false floats on grounded paths (the sequence read from overheard
  frames), the grounding delay after a rejoin at the same sequence, and
  a node ungrounded, grounded again, its held message delivered.
- **Abstractions to keep in mind**: no timing bound is checked (events
  only); losses bounded by counts, not probabilities; errors not routed;
  two versions only, issued once, Trickle suppression and the DODAG
  information parameters left out; whole-hop link costs (no count-up
  through hysteresis or fractional ETX; cost rises in whole hops, only
  with `Rise` > 0, never falling back); acknowledgement frames heard by their
  destination only; liveness with acknowledgements as frames checked on the
  subtree graph with 1 loss only (≈ 4 minutes), with versions on the line
  (≤ 2 losses) and on the diamond with a relay death (≤ 1 loss, no
  message: with a message the safety check alone has 300 000 states).

## Open points

- **Remedy A of D28 in the code** (`Elig = "reach"`): implemented
  (`neighbours.py` `Neighbour.reachable`, `eligible`, `link_etx`,
  `NeighbourTable.answered`; `reliable.py` `on_ack`; `parents.py`
  `candidate_cost`): the link to the root unreachable after U = 4
  consecutive failed samples (the acknowledgement window's, one per
  batching delay towards the root) and one silence limit since the last
  acknowledgement of the root (any explicit entry of the root for the
  node, late included; the entry's creation), read at selection time; any
  acknowledgement ends the run, failure ageing too; the root's link cost
  capped at 32 (ETX 4). The model shows that no per-link bound on
  consecutive losses bounds a run (`DODAG_line_reach_witness_run`): the
  silence limit without acknowledgement, not U, carries the argument that
  a live root is kept; the code's cost cap is not in the model (whole-hop
  fixed costs). Measured on `forming star 1` (`make large`): at the hidden
  root child 0101 the root's acknowledgements arrive at most 34 s apart
  until 309 s, then none at all (the root acknowledges 117 of its frames
  between 240 and 390 s, 3 batches reach it; root frames, beacons
  included, 0 to 2 per 30 s): the root is left at 384 s, one silence
  limit after the last one, instead of 348 s under ETX ≤ 4; the hidden
  Nodes had begun detaching at 273 s (their relay parent dropped). A
  keeps a root that answers now and then, not one drowned at the root
  child: the star collapse remains (rootless DODAG, remedy C).
- **A extended to relays in the code** (`Exhaust = "keepboth"`, `Elig =
  "reachall"` limited to the last candidate): the MP12 question of the
  model is decided by the limit. With ETX ≤ 4 no longer an eligibility
  test on any link and every cost capped at 32, a link of ETX 10 would
  cost as one of ETX 4 and stay a candidate; it would lose preference only
  to a candidate cheaper by the switch threshold (12), so a preferred
  parent at ETX 10 would be kept over an alternative one hop deeper at
  ETX 2 (gain 8), and a fresh selection could pick it. The code waives
  ETX ≤ 4 only for the root and for the preferred parent while it is the
  node's last candidate (the other links still by ETX ≤ 4, so not
  circular), which keeps the star's case (the hop-2 Nodes have no other
  candidate under L) and MP12's quality preference elsewhere. Between
  `"reachetx"` and `"reachall"`: the root and the last candidate as under
  `"reachall"`, the other links as under `"reachetx"`; the model does not
  tell them apart (fixed costs; one candidate on the line). Measured on
  `forming star 1` (`make large`): no hop-2 Node detaches at 273 s any
  more (their relay 0101 kept). 0101's queue fills from 280 s (64/64,
  refusing, so unacknowledged) and it hears no root frame from 270 s (the
  root's frames collide at 0101 with the Nodes' retries; the root hears
  0101 throughout); the hop-2 Nodes leave 0101 unreachable from 330 s (no
  acknowledgement of it for a silence limit) and 0101 the root at 345 s
  (silent for a silence limit); its poison detaches every Node at 350 s.
  The same again at 453 s. Each time the network re-forms through 0101
  within 35 s, without loop: every message delivered (283 of 289, 6
  refused), the run passes, where the code of A lost 25 messages
  silently (rootless DODAG, 4 loops). The collapse still starts (the root
  drowned at the hidden root child), remedy C (rootless DODAG) stays open
  for the other seeds. Cost measured on `relay-death star-domain` (3 of 3
  seeds fail, all passed on the code of A): after the relay's death the
  Nodes, cut off from the root, form a rootless DODAG where two Nodes
  are each other's preferred parent and last candidate; kept after their
  exhausted attempts (full queues refusing), the loop lasts until the
  bound L + 56 ends it, 27 to 43 s against the 26 s of MP11, and 1 to 3
  messages still circulate at the end; the drops after 3 attempts broke
  these loops sooner. Remedy C again.
- **Views older than an attach** (residual of the cost rise fix): the test against L
  assumes no node holds a view of an ancestor older than that ancestor's
  last attach (rank 255 heard since). Fix 5 ensures it with at most one
  consecutive loss per link; in the simulator's star (hidden-terminal collapse) a
  descendant that heard none of the node's frames since its detach (poison
  and solicitation included) takes part in a loop after the rejoin, and L,
  restarted, no longer excludes it. With remedy C (`Seq = "babel"`) L is
  kept across the detach and a descendant's sequence is never newer: no
  loop with up to 3 (line) or 2 (`star`) losses in any pattern; closed in
  the code once C is implemented.
- **Global repair wave**: a solicitation of finite rank (only a node behind
  a newer version sends one) answered by newer neighbours only, so that the
  node's own children no longer answer it in the window of its parent's
  advertisement (MESH_DESIGN §9, global repair wave; §18). To decide, then
  to model (`Versions = 2`) and check on the seed runs.
