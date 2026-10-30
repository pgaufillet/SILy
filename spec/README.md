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
and about 20 minutes for the link death configurations (`DODAG_*_cutlive`:
1.5 to 9 minutes each).

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
  0.4 T); its cost is one more beacon per period at the root (23 B, 7–14
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
and detach; against MP5, MP10, MP11 and MP12.
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
  path cost 4, a gain below the switch threshold). `Victim = 0`: no node
  dies.
- **Ranks** in whole hops (MIN_HOP 8 = 1): the hysteresis of 4 never holds a
  change back, the switch threshold 12 is a gain of 2 hops; 254 and
  MaxRankIncrease 56 scaled to `MaxRank` = 6 and `MaxInc` = 3. Links always
  eligible (no ETX estimation: `AckWindow`/`LinkCut` cover it), except a
  dead link (`Cut`, below). Parent sets
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
  candidate (dropping it would detach the node); otherwise the drop. The
  random retry delay that goes with it in the code is timing only,
  outside this untimed model.
- **Link death** (`Cut`, with `Acks = "frame"`): `"none"` (every
  configuration before this option); `"both"`: the link 0 – 1 dies once,
  at any moment, both ways; `"up"`: only 1 → 0 dies (the root no longer
  hears node 1, node 1 still hears the root). A dead link loses every
  frame and acknowledgement, outside the loss bounds, and carries no route
  (`MP10Live`, `CutOffLive` and error delivery read the graph of live
  links). The silence limit removes a neighbour no longer heard (`Expire`,
  as for a dead relay). Node 1's timeouts towards the root after the death
  are failed samples, and `INELIG` = 4 of them make the link ineligible
  (13 from a full window, `AckWindow`; scaled to 4, more than one round of
  attempts, so that `"keeproot"` gives fresh attempts at least once).
  Failure ageing is not modelled: the dead link stays ineligible.
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
  nodes and links) is attached through a chain of live links to the root; **CutOffLive**: every node
  cut off ends detached; **VersionSettle**: eventually every node connected
  to the root is in the root's version.
- **RoutesSettle** (MP12, no flapping): eventually no preferred parent
  changes (`<>[][pref' = pref]_vars`). `RanksSettle` (above) requires the
  parent to be of the node's version.
- **KeepParent** (MP10, MP12, action property): a node detaches (finite rank
  to 255, in its version) only when its preferred parent is lost — gone
  silent, heard at rank 255, or dropped after failed attempts — or, when
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
  (`NoFreshRoot`).

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
  or not, is kept while it is the last candidate. `"drop"`: the code
  before.

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
  (`Reliable._retry`, uniform in [0, 32 slots), one draw per timed-out
  frame, state `_DELAY`): timing only, outside the model; the timeouts
  and the deferred failure decisions stay anchored on the frame's end.
  Cost: a failed link to the root is left at its ineligibility (13
  failed samples from a full window) or at the silence limit, later than
  after 3 attempts (MESH_DESIGN §9 repair table); every retry waits up to
  32 slots more. MESH_DESIGN §12 (retry delay, upward failure) describes
  it; host tests `KeepRoot` (tests/test_parents.py), `RetryDelay`,
  `KeptNextHop` (tests/test_reliable.py), and
  `LocalRepair.test_root_kept_after_exhausted_attempts_until_its_link_is_ineligible`
  (tests/test_routing.py).
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

- **Views older than an attach** (residual of the cost rise fix): the test against L
  assumes no node holds a view of an ancestor older than that ancestor's
  last attach (rank 255 heard since). Fix 5 ensures it with at most one
  consecutive loss per link; in the simulator's star (hidden-terminal collapse) a
  descendant that heard none of the node's frames since its detach (poison
  and solicitation included) takes part in a loop after the rejoin, and L,
  restarted, no longer excludes it.
- **Global repair wave**: a solicitation of finite rank (only a node behind
  a newer version sends one) answered by newer neighbours only, so that the
  node's own children no longer answer it in the window of its parent's
  advertisement (MESH_DESIGN §9, global repair wave; §18). To decide, then
  to model (`Versions = 2`) and check on the seed runs.
