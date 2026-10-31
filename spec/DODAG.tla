------------------------------- MODULE DODAG -------------------------------
(***************************************************************************)
(* Upward routing, detach and rejoin, DODAG versions and class 1 custody  *)
(* on a small graph (MESH_DESIGN §9 rank and parent set, loop freedom,     *)
(* DODAG version and global repair, Trickle and solicitation; §12 class 1; *)
(* design issues 3 and 5 of spec/README.md; MP5, MP10, MP11, MP12).        *)
(* Line numbers are those of src/mesh at the time of writing.             *)
(*                                                                         *)
(* Time is abstracted to events: every protocol action may happen at any  *)
(* moment and is weakly fair. What the abstraction keeps of the timers:    *)
(* - a node detaching holds for 2 Imin (no selection) and sends its       *)
(*   poison(s) before the hold ends; the hold of a node ends after the    *)
(*   first poison of each neighbour (desync <= 4 slots << 2 Imin = 64);   *)
(* - an owed explicit acknowledgement (half a timeout) comes before the   *)
(*   holding of the copy expires (2 presence periods);                    *)
(* - the silence rule (2 presence periods) removes only dead neighbours   *)
(*   (a live neighbour sends a frame every period: removing it needs two  *)
(*   consecutive lost presences, which the loss bounds below exclude or    *)
(*   which only make the node fall back on another parent);               *)
(* - holding (2 presence periods) expires only while the node has no      *)
(*   route; the duplicate cache (2 periods) never expires (loops close    *)
(*   within a few Imin);                                                   *)
(* - attached nodes may send a frame at any time (presence every period,  *)
(*   Trickle advertisements after a reset, at once after a rank error or  *)
(*   a parent change, root beacon): Announce;                             *)
(* - a node behind a newer version (Behind = "detach", the code) keeps    *)
(*   its parents for 2 Imin: its solicitation goes first, and the 2 Imin   *)
(*   end after the DODAG information owed by each neighbour of the newer   *)
(*   version (a Trickle reset answers within Imin).                        *)
(*                                                                         *)
(* Ranks are in units of one perfect hop (MIN_HOP = 8, parents.py:67);    *)
(* link costs are whole hops (ETX 1, 2, ... ; margin always good, links    *)
(* always eligible but the link to the root, Cut and Elig below). Hence    *)
(* the rank hysteresis of 4 (parents.py:72) never                          *)
(* holds a change back (every change is >= 8) and the switch threshold 12  *)
(* (parents.py:71) becomes a gain of 2 hops. MaxRank stands for 254       *)
(* (parents.py:68), MaxInc for MaxRankIncrease 56 (parents.py:69), both   *)
(* scaled down. Parent sets are not truncated to 3 members (every node    *)
(* has at most 3 neighbours).                                              *)
(*                                                                         *)
(* Link cost rises (Rise > 0): the environment raises the                 *)
(* cost of a link as seen by one node (its ETX after failed samples,       *)
(* neighbours.py:198, link_cost parents.py:109-112) by one hop at a time, *)
(* Rise hops overall (CostUp). The node reads the new cost at its next    *)
(* selection, after the next frame it hears (dodag.py:20-25, 409, 549-557).*)
(* Rise = 0: fixed costs, the earlier configurations.                     *)
(* Rank test of the candidates other than the preferred parent (Thr):     *)
(* - "rank" (the code before the fix): strictly below the node's current *)
(*   rank, the one its last selection set (not necessarily on air yet);   *)
(* - "air": strictly below the rank carried by the node's last frame;     *)
(* - "minair": strictly below the lowest of both;                          *)
(* - "low" (the code, parents.py:204, 211): strictly below L, the lowest *)
(*   rank of the node since it attached or adopted its version            *)
(*   (parents.py:381-383), never above its current rank.                  *)
(* IncAdv = TRUE: the idealised limit of "a rank increase is advertised at *)
(* once": the node's frame with its new rank is the very next step (no    *)
(* other event anywhere in between). FALSE (the code: a Trickle reset,     *)
(* dodag.py:576-577, whose advertisement k = 2 frames heard may suppress, *)
(* trickle.py:11-14): the next frame of the node comes at any later time,  *)
(* as every Announce; an advertisement sent "at once" through a queue,     *)
(* carrier sense and duty cycle is the same in this event abstraction.    *)
(*                                                                         *)
(* DODAG versions (Versions = 2; Versions = 1: one version, as the first  *)
(* model): versions are 0 and 1, the root issues version 1 once, at any   *)
(* moment (NewVersion: operator, parameter change or inconsistency; the    *)
(* policy is not modelled). Every frame carries its sender's version;      *)
(* frames of rank 255 do not propagate it (dodag.py:378-379). The DODAG    *)
(* information message (port 9) is carried by Trickle advertisements and  *)
(* the root beacon only (dio): presence, data and acknowledgement frames   *)
(* carry the version in their header only. A node adopts a version from   *)
(* the DODAG information of a neighbour of finite rank: a newer one, or    *)
(* any other one when detached (dodag.py:436-443, parents.py:207-240);    *)
(* it then takes parents of that version only, at any rank (L restarts,   *)
(* the hold and the drops are forgotten), and detaches if there is none.  *)
(* Candidates have the node's version (parents.py:162). A node hearing a  *)
(* finite rank frame of a newer version is behind (dodag.py:393-398); one *)
(* of an older version, or a solicitation, resets its Trickle timer with  *)
(* a DODAG information owed (owe, dodag.py:390-392, 424-427), as does an  *)
(* adoption and the root's new version. Data-path validation compares     *)
(* ranks of the same version only (routing.py:140-148); an upward message *)
(* of another version is forwarded.                                       *)
(*                                                                         *)
(* Frames: each frame of node s carries its rank (header, frame.py) and   *)
(* updates the entry of every neighbour that hears it, overheard frames   *)
(* included (link.py:730 before the destination test of link.py:737).     *)
(* Each neighbour may lose it; losses are bounded overall (MaxLoss) and   *)
(* per directed link (at most MaxConsec consecutive frames of s lost by   *)
(* one neighbour). Acks = "atomic": acknowledgements are not frames, may  *)
(* be lost (counted in MaxLoss only) and update no rank; Acks = "frame":  *)
(* an explicit acknowledgement is a frame heard by its destination only   *)
(* (losses counted as for any frame).                                     *)
(*                                                                         *)
(* Class 1 (reliable.py): each message has one origin; a copy at a node is *)
(* "q" (queued towards hop), "p" (sent to hop and waiting; acc "pas": the  *)
(* hop queued it and the forward will acknowledge it, "exp": an explicit  *)
(* acknowledgement is on its way, "no": the timeout will come), "h" (held, *)
(* no route) or "no". The reception at the next hop is atomic with the    *)
(* frame. Acks = "atomic" (first model): an explicit acknowledgement is   *)
(* atomic with the reception, carries no rank, and a detached node keeps  *)
(* retrying its waiting copies towards its former parent (the code before  *)
(* fix 3).                                                                 *)
(* Acks = "frame" (the code with fix 3): an explicit acknowledgement is a  *)
(* separate frame of the acknowledging node (AckFrame), heard or lost,    *)
(* ordered freely with its other frames, carrying its rank (255 when it   *)
(* is detached); a detaching node holds its waiting copies. The queue is  *)
(* unbounded (never refuses); one copy per node and message (a duplicate  *)
(* taken again replaces a copy still waiting). An error (port 4, class 1)  *)
(* is abstracted: it is "sent" when a relay fails a message, "recv" when the *)
(* origin itself fails it or when the error reaches it, which happens once *)
(* both the failing node and the origin are connected to the root (the    *)
(* error travels as class 1 to the root, then downward, reliable.py:       *)
(* 691-757; its own custody is not modelled).                             *)
(*                                                                         *)
(* Late root acknowledgements (LateAck, Acks = "frame"): the root batches *)
(* its acknowledgements, and under load its medium access can delay one   *)
(* beyond the root timeout. LateAck = "none" (the earlier model): an      *)
(* acknowledgement on its way always comes before the timeout (Timeout    *)
(* requires acc = "no"). "ignore" (the code before late acknowledgements  *)
(* were accepted): the timeout towards the root may also fire while the   *)
(* root's acknowledgement is on its way, which is then ignored (retry, or *)
(* the drop after the last attempt). "accept" (the code): the same        *)
(* timeout and retry, but the acknowledgement of the earlier transmission *)
(* stays expected (late) and, when it comes, removes the copy wherever it *)
(* is (queued, waiting, held); after the last attempt the drop waits one  *)
(* more root timeout, for the acknowledgement of that attempt. The timing *)
(* kept: an acknowledgement is late by less than one root timeout, so it  *)
(* comes (or is lost) before the next timeout of the copy, and before the *)
(* drop. The code accepts an acknowledgement of any earlier transmission  *)
(* while the message is queued, even later; the model leaves that out.    *)
(*                                                                         *)
(* Exhausted attempts (Exhaust): what the last timeout of a copy does to   *)
(* its hop. "drop" (the code before the root was kept): the hop is dropped *)
(* (Parents.next_parent), the node selects again, the copy goes to the new *)
(* preferred parent or is held. "keeproot" (the code): towards the root,   *)
(* while the root is still a candidate                                     *)
(* apart from drops (heard: entry not expired; link eligible; of the       *)
(* node's version; within the bound), the copy gets fresh attempts towards *)
(* the root instead: only the silence limit or the link's ineligibility   *)
(* removes the root. "keeplast": the same for the preferred parent, root   *)
(* or not, when it is the node's last candidate (no other candidate apart  *)
(* from drops: dropping it would detach the node); otherwise the drop.   *)
(*                                                                         *)
(* Death of the link 0 - 1 (Cut, Acks = "frame"): "none" (every earlier    *)
(* configuration); "both": once, at any moment, the link dies both ways;   *)
(* "up": only 1 -> 0 dies (the root never hears node 1, node 1 still hears *)
(* the root). A dead link loses every frame, outside the loss bounds. The *)
(* silence limit removes the root from node 1's table once the root is not *)
(* heard ("both"; Expire); node 1's failed samples towards the root make   *)
(* the link ineligible (Elig below).                                       *)
(*                                                                         *)
(* Eligibility of the link to the root (Elig, Acks = "frame"): rfail[k]   *)
(* counts k's failed samples towards the root, i.e. its transmissions to  *)
(* the root whose acknowledgement never comes: a timeout with no          *)
(* acknowledgement on its way (the frame lost at the root, or its         *)
(* acknowledgement lost at k; with LateAck = "ignore" also one ignored),   *)
(* or a late acknowledgement lost (LateAck = "accept": the failure is     *)
(* decided once the late one is out, reliable.py _defer, _decide). The     *)
(* other links stay eligible (their ETX is CostUp's, Rise).                *)
(* - "none" (every configuration before this option): only node 1's       *)
(*   timeouts towards the root after the cut count, INELIG of them make   *)
(*   the link ineligible (13 from a full acknowledgement window,          *)
(*   AckWindow; scaled to 4, more than one round of attempts); collisions *)
(*   on a live link never make it ineligible. Failure ageing is not       *)
(*   modelled: the dead link stays ineligible.                             *)
(* - "etx" (the code before A: ETX <= 4, neighbours.py:244-246, ETX_MAX     *)
(*   neighbours.py:71, etx neighbours.py:198-209): every failed sample of  *)
(*   any node towards the root counts, on a live link too, and INELIG of   *)
(*   them make the link ineligible; neither a success nor ageing removes  *)
(*   them. ETX > 4 is p_ack < 1/4 over the last 16 samples (13 failures   *)
(*   from a full window, 4 on a fresh one), scaled to 4 as for the dead    *)
(*   link; a success does not remove a failure from the window (it leaves *)
(*   only 16 samples later, beyond the few transmissions of the model),    *)
(*   and ageing (neighbours.py:189-194) needs a silence limit since the   *)
(*   newest failure, which a node failing under sustained load does not   *)
(*   reach. Pessimistic for the node (it may read its link ineligible a   *)
(*   little earlier than the code): enough for a witness of the mechanism, *)
(*   which the simulator shows (D28: ETX 5.3 at a hidden root child).      *)
(* - "reach" (remedy A, RPL neighbour unreachability detection style, the  *)
(*   node's class 1 transmissions to the root as its probes): the link to  *)
(*   the root no longer uses ETX <= 4; it is unreachable (ineligible) once *)
(*   the node's last U failed samples towards it were consecutive (no     *)
(*   acknowledgement of the root, late ones included, in between) and no  *)
(*   acknowledgement of the root came for one silence limit. Abstracted   *)
(*   as rfail: the consecutive failed samples, reset by every             *)
(*   acknowledgement of the root heard by the node, UNREACH of them make   *)
(*   it unreachable, any later acknowledgement reachable again. What the  *)
(*   abstraction keeps of the timers: the silence limit without           *)
(*   acknowledgement only makes the code's rule stricter than the model's  *)
(*   (a run reaching U in the code reaches UNREACH in the model, while the *)
(*   model's run needs no time), so a node the model never makes          *)
(*   unreachable on a live link is never made unreachable by the code     *)
(*   either, provided U dominates as UNREACH does; on a dead link no      *)
(*   acknowledgement ever comes, so the silence limit always passes, after *)
(*   the U failures (fresh attempts under keeproot keep sampling).        *)
(*   UNREACH: each failed sample on a live link costs one loss within the *)
(*   bounds (a lost frame of the node at the root, a lost acknowledgement *)
(*   or late acknowledgement at the node), and the per-link bound MaxConsec *)
(*   does not bound a run: the losses may alternate between the two       *)
(*   directions, and the frames in between (root beacons heard by the     *)
(*   node, its presences heard by the root) reset the per-link counts. A   *)
(*   run is bounded by MaxLoss only: UNREACH = MaxLoss + 1, at least       *)
(*   INELIG (more than one round of attempts, as above). The run of       *)
(*   MaxLoss failures is reached with MaxConsec = 1 (ShortFailRuns).       *)
(*   The root's ETX still sets its link cost; in the model costs move by  *)
(*   CostUp only (Rise = 0 in the "reach" configurations: cost 1), so a    *)
(*   reachable root link whose ETX exceeds 4 (cost above 32 in the code,   *)
(*   up to the bound L + 56 and beyond) is outside the model.              *)
(*                                                                         *)
(* The relay Victim may die once, at a moment it holds no message (a      *)
(* relay dying with a message it acknowledged is MP5's accepted exception, *)
(* §12, left out of the model). Victim = 0 (the root): no node dies.       *)
(*                                                                         *)
(* Fixes (design issues of spec/README.md, and the later ones):            *)
(* - Fix3 = "custody": a node other than the destination acknowledges and  *)
(*   drops a duplicate only while it holds a copy (queued, waiting or      *)
(*   held); otherwise it takes the message as new (forwards or holds it).  *)
(*   A message that comes back after the node handed it over is thus      *)
(*   forwarded again instead of being dropped as looped. A duplicate taken  *)
(*   again is acknowledged explicitly (as the code of the fix).            *)
(*   Fix3 = "refined": the same, except that a copy waiting for the         *)
(*   acknowledgement of the duplicate's sender does not count as held      *)
(*   (that sender took it; its acknowledgement may still release the copy). *)
(*   Fix3 = "retake" (rejected): a duplicate from another node than its   *)
(*   first sender (an own message: from anyone) is taken back as held; a  *)
(*   duplicate from the first sender is dropped as a retransmission.      *)
(* - Fix5 = "solicit": at the end of the detach hold the node sends its   *)
(*   solicitation and rejoins only through neighbours heard after it (the *)
(*   solicitation, rank 255, is a second poison);                         *)
(*   Fix5 = "repeat" (rejected): the poison is sent twice during the     *)
(*   hold, the rejoin rule unchanged (neighbours heard after the detach).  *)
(* - Follow = TRUE (rank following, the RPL rule, the code): the         *)
(*   preferred parent stays a candidate whatever its rank (of the node's   *)
(*   version, finite, within the bound L + MaxInc); the node's rank        *)
(*   becomes the path cost through it. FALSE (the code before the fix):    *)
(*   candidates, the preferred parent included, must be strictly below the *)
(*   node's advertised rank.                                               *)
(* - Behind = "keep" (no detach for lagging behind a version): a node     *)
(*   behind a newer version keeps selecting in its own version and keeps  *)
(*   its preferred parent when that parent has moved to the newer version  *)
(*   (heard at a finite rank, not dropped), at its own rank (ranks of      *)
(*   different versions are not compared), until it adopts the newer      *)
(*   version; it forwards through it meanwhile. Its frames, of the older  *)
(*   version, make its newer neighbours owe a DODAG information (the      *)
(*   solicitations with backoff are frames of the node like any other;    *)
(*   the code). Behind = "detach" (the code before the fix): selection     *)
(*   paused for 2 Imin, then candidates of its own version only (a class 1 *)
(*   drop selects at once).                                                *)
(* - Adopt = "fresh" (adoption after the hold): a detached node adopts    *)
(*   another version only once its hold is over (with Fix5 "solicit": from *)
(*   a DODAG information heard after its solicitation; the code). Adopt =  *)
(*   "any" (the code before the fix): at once, the hold ending; it lets a  *)
(*   detached node adopt an older version from its own stale subtree (a    *)
(*   loop).                                                                *)
(* - Rejoin = "window" (answer window, with Fix5 "solicit"): a solicitation *)
(*   of a detached node (the one ending its hold, then the periodic ones)  *)
(*   opens an answer window of one Imin, during which the node selects     *)
(*   nothing; at its close (WindowClose: an event, answers may come before *)
(*   or after it) it takes the best candidate heard after the solicitation *)
(*   ending its hold; with none, the first candidate heard afterwards. A   *)
(*   detached node adopting another version in its window keeps the       *)
(*   window and its rejoin rule (candidates heard after the solicitation). *)
(*   Rejoin = "window_rejoin": the same, but an adoption selects at once.  *)
(*   Rejoin = "first" (the code): the first candidate heard after the      *)
(*   solicitation is taken (selection at every frame heard).              *)
(*   Best = TRUE tracks the window as a ghost in "first" too (wh, rj) for  *)
(*   RejoinBest.                                                           *)
(***************************************************************************)
EXTENDS Integers, FiniteSets, TLC

CONSTANTS
    Graph,      \* "chain", "line", "diamond", "subtree", "follow", "rejoin" (see Edges)
    Victim,     \* the relay that may die once (the root: none)
    Origins,    \* origins of the class 1 messages, one message each (named by its origin)
    MaxLoss,    \* frame and acknowledgement losses overall
    MaxConsec,  \* consecutive frames of one node lost by one neighbour
    MaxRank,    \* highest usable path cost (254, scaled)
    MaxInc,     \* MaxRankIncrease (56, scaled)
    Fix3,       \* "none" (the code before fix 3), "custody", "refined" (the code) or "retake"
    Fix5,       \* "none" (the code before fix 5), "solicit" (the code) or "repeat"
    Ghosts,     \* BOOLEAN: count loop formations, rank errors, cross-version forwards (witnesses only)
    Acks,       \* "atomic" (first model) or "frame" (acknowledgements are frames, the code)
    Follow,     \* BOOLEAN: follow the preferred parent's rank increase (TRUE: the code)
    Behind,     \* node behind a newer version: "detach" (the code before the fix) or "keep"
    Versions,   \* 1 (one DODAG version) or 2 (the root may issue a new version once)
    Adopt,      \* detached node and another version: "any" (the code before the fix) or "fresh"
    Rejoin,     \* rejoin after the solicitation: "first" (the code), "window" or "window_rejoin"
    Best,       \* BOOLEAN: track the answer window (ghosts wh, rj) for RejoinBest
    Rise,       \* link cost rises, in hops overall (0: fixed costs)
    Thr,        \* rank test of the other candidates: "rank" (the code before the fix), "air", "minair" or "low" (the code)
    IncAdv,     \* BOOLEAN: a rank increase goes on air in the very next step (idealised)
    LateAck,    \* root acknowledgement later than the timeout: "none", "ignore" or "accept" (the code)
    Exhaust,    \* after the last attempt: "drop", "keeproot" (the code) or "keeplast"
    Cut,        \* death of the link 0 - 1: "none", "both" or "up" (1 -> 0 only)
    Elig        \* link to the root ineligible: "none" (after the cut only), "etx" (the code before A) or "reach" (remedy A, the code)

NONE == -1
Root == 0
INF  == MaxRank + 1        \* rank 255: no route, or poisoned
SwitchThr == 2             \* 12 / 8 = 1.5 hops: a gain of 2 whole hops (parents.py:297)
ATTEMPTS == 3              \* reliable.py:107
LoopCap == 2               \* saturation of the loop formation counter
CutChild == 1              \* the link that may die: CutChild - Root
INELIG == 4                \* failed samples making the link ineligible (scaled; Elig "none", "etx")
\* Consecutive failed samples making the link to the root unreachable (Elig "reach"):
\* beyond the loss budget, at least INELIG.
UNREACH == IF MaxLoss + 1 > INELIG THEN MaxLoss + 1 ELSE INELIG
Lim == IF Elig = "reach" THEN UNREACH ELSE INELIG

(***************************************************************************)
(* Graphs: edges <<a, b, cost>>, cost in hops (ETX).                       *)
(* - chain: 0 - 1 - 2 (link cost rises: the smallest loop).                *)
(* - line: 0 - 1 - 2 - 3; relay 1 dies: 2 and 3 are cut off.               *)
(* - diamond: 0 - 1, 0 - 2, 1 - 3, 2 - 3, 3 - 4; relay 1 dies: 3 falls    *)
(*   back on 2 (local repair), 4 follows.                                  *)
(* - subtree: 0 - 1, 1 - 2, 1 - 3, 2 - 3, 3 - 4 and a costly link 0 - 4   *)
(*   (ETX 3); relay 1 dies: 2 and 3 can loop through each other before    *)
(*   reaching the root again through 4.                                    *)
(* - follow: the diamond with a costly link 2 - 3 (ETX 2); relay 1 dies:  *)
(*   3 falls back on 2 at a higher rank (2 -> 3), reaching the rank of its *)
(*   child 4 (3), whose only neighbour it is.                              *)
(* - rejoin: 0 - 1 - 2, 2 - 3, 2 - 4, 3 and 4 children of the root through *)
(*   costly links (0 - 3 ETX 2, 0 - 4 ETX 3); relay 1 dies: 2 (rank 2)     *)
(*   detaches (3 at rank 2, 4 at rank 3 are not below it) and rejoins      *)
(*   through 3 (path cost 3) or 4 (4), a gain of one hop, below the switch *)
(*   threshold: the parent taken at the rejoin stays.                      *)
(***************************************************************************)
Edges ==
    CASE Graph = "chain"   -> {<<0, 1, 1>>, <<1, 2, 1>>}
      [] Graph = "line"    -> {<<0, 1, 1>>, <<1, 2, 1>>, <<2, 3, 1>>}
      [] Graph = "diamond" -> {<<0, 1, 1>>, <<0, 2, 1>>, <<1, 3, 1>>, <<2, 3, 1>>, <<3, 4, 1>>}
      [] Graph = "subtree" -> {<<0, 1, 1>>, <<1, 2, 1>>, <<1, 3, 1>>, <<2, 3, 1>>, <<3, 4, 1>>,
                               <<0, 4, 3>>}
      [] Graph = "follow"  -> {<<0, 1, 1>>, <<0, 2, 1>>, <<1, 3, 1>>, <<2, 3, 2>>, <<3, 4, 1>>}
      [] Graph = "rejoin"  -> {<<0, 1, 1>>, <<1, 2, 1>>, <<0, 3, 2>>, <<2, 3, 1>>, <<0, 4, 3>>,
                               <<2, 4, 1>>}

Nodes == {e[1] : e \in Edges} \cup {e[2] : e \in Edges}
\* Constant functions (computed once by TLC).
NbrF == [n \in Nodes |-> {m \in Nodes : \E e \in Edges : {e[1], e[2]} = {n, m}}]
CostF == [p \in Nodes \X Nodes |->
            IF \E e \in Edges : {e[1], e[2]} = {p[1], p[2]}
            THEN (CHOOSE e \in Edges : {e[1], e[2]} = {p[1], p[2]})[3] ELSE 0]
Nbr(n) == NbrF[n]
Cost(n, m) == CostF[<<n, m>>]          \* initial cost (the formed DODAG)
Msgs == Origins
Orig(m) == m
Vers == 0..(Versions - 1)

Min(S) == CHOOSE x \in S : \A y \in S : x <= y

ASSUME Root \in Nodes /\ Victim \in Nodes
ASSUME Origins \subseteq Nodes \ {Root}
ASSUME MaxLoss \in Nat /\ MaxConsec \in Nat \ {0} /\ MaxInc \in Nat \ {0} /\ Ghosts \in BOOLEAN
ASSUME Acks \in {"atomic", "frame"}
ASSUME Fix3 \in {"none", "retake", "custody", "refined"} /\ Fix5 \in {"none", "solicit", "repeat"}
ASSUME Follow \in BOOLEAN /\ Behind \in {"detach", "keep"} /\ Versions \in {1, 2}
ASSUME Adopt \in {"any", "fresh"}
ASSUME Rejoin \in {"first", "window", "window_rejoin"} /\ Best \in BOOLEAN
ASSUME (Rejoin # "first" \/ Best) => Fix5 = "solicit"
ASSUME Rise \in Nat /\ Thr \in {"rank", "air", "minair", "low"} /\ IncAdv \in BOOLEAN
ASSUME LateAck \in {"none", "ignore", "accept"} /\ (LateAck # "none" => Acks = "frame")
ASSUME Exhaust \in {"drop", "keeproot", "keeplast"}
ASSUME Cut \in {"none", "both", "up"} /\ (Cut # "none" => Acks = "frame" /\ CutChild \in Nbr(Root))
ASSUME Elig \in {"none", "etx", "reach"} /\ (Elig # "none" => Acks = "frame")

\* The rank of the last frame is tracked (otherwise constant).
TrackAir == Thr \in {"air", "minair"}

\* The answer window is tracked (a real window, or the ghost of RejoinBest).
Track == Rejoin # "first" \/ Best

\* Poison frames per detach (dodag.py:551-557: once).
Poisons == IF Fix5 = "repeat" THEN 2 ELSE 1

\* Shortest path costs: the formed DODAG of the initial state.
RECURSIVE SP(_, _)
SP(n, k) ==
    IF n = Root THEN 0
    ELSE IF k = 0 THEN INF
    ELSE Min({INF} \cup {SP(m, k - 1) + Cost(n, m) : m \in Nbr(n)})

InitRank == [n \in Nodes |-> SP(n, Cardinality(Nodes))]
ASSUME \A n \in Nodes : InitRank[n] <= MaxRank

(***************************************************************************)
(* Variables.                                                              *)
(***************************************************************************)
VARIABLES
    alive,
    rank,      \* advertised rank (INF = 255)
    pref,      \* preferred parent, NONE when detached
    low,       \* L, lowest rank advertised since the last detach or adoption (NONE: none)
    hold,      \* in the detach hold (2 Imin): selects nothing
    poisoned,  \* detached since a poison: candidates must be fresh
    toSend,    \* poison frames still to send
    view,      \* view[k][c]: rank of c in its last frame heard by k (NONE: absent)
    fresh,     \* fresh[k][c]: heard after k's poison (Fix5 "solicit": after its solicitation)
    dropped,   \* dropped[k][c]: dropped after failed attempts, until a new frame of c
    ver,       \* DODAG version of the node
    vview,     \* vview[k][c]: version of c's last frame of finite rank heard by k
    behind,    \* heard a frame of finite rank of a newer version, not adopted yet
    sol,       \* Behind "detach": behind and its solicitation sent
    owe,       \* a Trickle reset owes a DODAG information (older neighbour, solicitation, adoption)
    win,       \* answer window open (Track; a ghost when Rejoin = "first")
    wh,        \* wh[k][c]: c heard by k in its answer window (Best)
    rj,        \* rank taken at the rejoin in the window, NONE if none (Best)
    lost,      \* losses so far
    consec,    \* consec[s][k]: consecutive frames of s lost by k
    sub,       \* message submitted
    cp,        \* cp[m][n]: copy of m at n, [st, hop, att, acc]
    giver,     \* giver[m][n]: node m was first received from (n itself: origin); NONE: not in the cache
    delivered, \* m reached the root
    err,       \* "none", "sent" (by a relay, towards the origin), "recv" (the origin knows)
    errFrom,   \* node that sent the error
    loops,     \* loop formations, saturated at LoopCap
    rl,        \* a rejoin (attachment from rank 255) closed a loop
    rerr,      \* a rank error was detected (data-path validation)
    xv,        \* an upward message was taken by a node of another version than its sender
    dc,        \* dc[k][c]: rise of k's link cost to c, in hops (Rise)
    rises,     \* link cost rises so far
    air,       \* air[k]: rank carried by k's last frame (TrackAir; otherwise constant)
    inc,       \* inc[k]: k's rank rose since its last frame (IncAdv; otherwise FALSE)
    cut,       \* the link CutChild - Root is dead (Cut)
    rfail      \* rfail[k]: k's failed samples towards the root (Elig), saturated at Lim

routing0 == <<alive, rank, pref, low, hold, poisoned, toSend, view, fresh, dropped,
              ver, vview, behind, sol, owe, win, wh, rj>>
routing == <<routing0, dc, rises, air, inc>>
msgvars == <<sub, cp, giver, delivered, err, errFrom>>
linkvars == <<cut, rfail>>
vars == <<routing, lost, consec, msgvars, loops, rl, rerr, xv, linkvars>>

\* Link cost of k to c, as k currently estimates it.
LCost(k, c) == Cost(k, c) + dc[k][c]

Copies == [st : {"no", "q", "p", "h"}, hop : Nodes \cup {NONE}, att : 0..ATTEMPTS,
           acc : {"no", "pas", "exp"}, late : BOOLEAN]
NoCopy == [st |-> "no", hop |-> NONE, att |-> 0, acc |-> "no", late |-> FALSE]
Held   == [st |-> "h", hop |-> NONE, att |-> 0, acc |-> "no", late |-> FALSE]
Queued(h) == [st |-> "q", hop |-> h, att |-> 0, acc |-> "no", late |-> FALSE]
AllF == [c \in Nodes |-> FALSE]

TypeOK ==
    /\ alive \in [Nodes -> BOOLEAN]
    /\ rank \in [Nodes -> 0..INF] /\ pref \in [Nodes -> Nodes \cup {NONE}]
    /\ low \in [Nodes -> 0..MaxRank \cup {NONE}]
    /\ hold \in [Nodes -> BOOLEAN] /\ poisoned \in [Nodes -> BOOLEAN]
    /\ toSend \in [Nodes -> 0..2]
    /\ view \in [Nodes -> [Nodes -> 0..INF \cup {NONE}]]
    /\ fresh \in [Nodes -> [Nodes -> BOOLEAN]] /\ dropped \in [Nodes -> [Nodes -> BOOLEAN]]
    /\ ver \in [Nodes -> Vers] /\ vview \in [Nodes -> [Nodes -> Vers]]
    /\ behind \in [Nodes -> BOOLEAN] /\ sol \in [Nodes -> BOOLEAN] /\ owe \in [Nodes -> BOOLEAN]
    /\ win \in [Nodes -> BOOLEAN] /\ wh \in [Nodes -> [Nodes -> BOOLEAN]]
    /\ rj \in [Nodes -> 0..INF \cup {NONE}]
    /\ lost \in 0..MaxLoss /\ consec \in [Nodes -> [Nodes -> 0..MaxConsec]]
    /\ sub \in [Msgs -> BOOLEAN] /\ cp \in [Msgs -> [Nodes -> Copies]]
    /\ giver \in [Msgs -> [Nodes -> Nodes \cup {NONE}]]
    /\ delivered \in [Msgs -> BOOLEAN] /\ err \in [Msgs -> {"none", "sent", "recv"}]
    /\ errFrom \in [Msgs -> Nodes \cup {NONE}]
    /\ loops \in 0..LoopCap /\ rl \in BOOLEAN /\ rerr \in BOOLEAN /\ xv \in BOOLEAN
    /\ dc \in [Nodes -> [Nodes -> 0..Rise]] /\ rises \in 0..Rise
    /\ air \in [Nodes -> 0..INF] /\ inc \in [Nodes -> BOOLEAN]
    /\ cut \in BOOLEAN /\ rfail \in [Nodes -> 0..Lim]

(***************************************************************************)
(* Chains of preferred parents (dead nodes have none).                     *)
(***************************************************************************)
LiveP(P) == [n \in Nodes |-> IF alive[n] THEN P[n] ELSE NONE]

RECURSIVE Climb(_, _, _)
Climb(n, P, k) == IF k = 0 \/ n = NONE THEN n ELSE Climb(P[n], P, k - 1)

InCycle(n, P) == \E k \in 1..Cardinality(Nodes) : Climb(n, P, k) = n
Cycle(P) == \E n \in Nodes : P[n] # NONE /\ InCycle(n, P)

(***************************************************************************)
(* Death of the link CutChild - Root (Cut): a frame of s is not heard by k *)
(* (Severed), outside the loss bounds; the dead link carries no route      *)
(* either way (LinkUp). After Lim failed samples of k towards the root     *)
(* (Elig: after the cut, of CutChild only, for "none"; consecutive for     *)
(* "reach"), k's link to the root is ineligible (Inel).                    *)
(***************************************************************************)
CutPair(a, b) == {a, b} = {CutChild, Root}
Severed(s, k) == cut /\ IF Cut = "up" THEN s = CutChild /\ k = Root ELSE CutPair(s, k)
LinkUp(a, b) == b \in Nbr(a) /\ ~(cut /\ CutPair(a, b))
Inel(k, c) == c = Root /\ rfail[k] >= Lim

\* A failed sample of k towards the root (saturated); an acknowledgement of the root
\* heard by k ("reach": the run of consecutive failures ends; otherwise nothing).
RootFail(k) == IF rfail[k] < Lim THEN [rfail EXCEPT ![k] = rfail[k] + 1] ELSE rfail
RootAcked(k) == IF Elig = "reach" THEN [rfail EXCEPT ![k] = 0] ELSE rfail

\* n is attached through a chain of preferred parents to the root, over live links.
RECURSIVE ToRoot(_, _)
ToRoot(n, k) == n = Root \/ (k > 0 /\ alive[n] /\ pref[n] # NONE /\ LinkUp(n, pref[n])
                             /\ ToRoot(pref[n], k - 1))
Connected(n) == ToRoot(n, Cardinality(Nodes))

\* n is connected to the root in the graph of the live nodes and live links.
RECURSIVE Reach(_, _)
Reach(S, k) == IF k = 0 THEN S
               ELSE Reach(S \cup {m \in Nodes : alive[m] /\ \E x \in S : LinkUp(x, m)}, k - 1)
GraphConnected(n) == n \in Reach({Root}, Cardinality(Nodes))

(***************************************************************************)
(* Routing records: the state a selection reads (R, without event flags)  *)
(* and writes (with det: detached, chg: preferred parent changed, att:     *)
(* attached from rank 255).                                                *)
(***************************************************************************)
Rec(k) == [rank |-> rank[k], pref |-> pref[k], low |-> low[k], hold |-> hold[k],
           poisoned |-> poisoned[k], toSend |-> toSend[k], ver |-> ver[k], behind |-> behind[k],
           win |-> win[k]]

KeepR(R) == [rank |-> R.rank, pref |-> R.pref, low |-> R.low, hold |-> R.hold,
             poisoned |-> R.poisoned, toSend |-> R.toSend, ver |-> R.ver, behind |-> R.behind,
             win |-> R.win, det |-> FALSE, chg |-> FALSE, att |-> FALSE]

Keep(k) == KeepR(Rec(k))

(***************************************************************************)
(* Parent selection (Parents._select, parents.py:328-390).                 *)
(* Candidate (Parents.candidate_cost, parents.py:196-224): a neighbour     *)
(* of the node's version (parents.py:205) with a finite rank strictly     *)
(* below the threshold of Thr, ThrR (the code: L, parents.py:204, 211;   *)
(* any finite rank when detached;                                          *)
(* Follow: the preferred parent at any finite rank), heard within 2       *)
(* periods (an entry), heard after the poison while detached               *)
(* (parents.py:215), not dropped after failed attempts (parents.py:217),  *)
(* through an eligible link (parents.py:219; Cut: not made ineligible by   *)
(* its death),                                                             *)
(* whose path cost is within the bound min(254, L + 56) (parents.py:      *)
(* 189-194, 223-224). Behind "keep": the preferred parent moved to a      *)
(* newer version, heard at a finite rank and not dropped, at the node's   *)
(* own rank.                                                               *)
(***************************************************************************)
BoundR(R) == IF R.low = NONE THEN MaxRank
             ELSE IF R.low + MaxInc < MaxRank THEN R.low + MaxInc ELSE MaxRank
Bound(k) == BoundR(Rec(k))

\* Rank threshold of the candidates other than the preferred parent (INF when detached;
\* "minair": once the poison is on air, before which the hold selects nothing).
ThrR(k, R) ==
    CASE Thr = "rank"   -> R.rank
      [] Thr = "low"    -> IF R.low = NONE THEN R.rank ELSE R.low
      [] Thr = "air"    -> IF R.rank = INF THEN INF ELSE air[k]
      [] Thr = "minair" -> IF air[k] < R.rank THEN air[k] ELSE R.rank

CandCost(k, c, V, VV, Dr, Fr, R) ==
    LET v == V[k][c] IN
    IF v = NONE \/ v = INF \/ Dr[k][c] \/ Inel(k, c) THEN NONE
    ELSE IF VV[k][c] = R.ver
    THEN IF /\ \/ v < ThrR(k, R)
               \/ Follow /\ c = R.pref
            /\ ~R.poisoned \/ Fr[k][c]
            /\ v + LCost(k, c) <= BoundR(R)
         THEN v + LCost(k, c) ELSE NONE
    ELSE IF Behind = "keep" /\ R.behind /\ c = R.pref /\ VV[k][c] > R.ver
    THEN R.rank
    ELSE NONE

\* Selection at k from views V, VV, drops Dr, fresh flags Fr, k's record R (hold not tested).
SelCore(k, V, VV, Dr, Fr, R) ==
    LET cf == [c \in Nbr(k) |-> CandCost(k, c, V, VV, Dr, Fr, R)]
        C == {c \in Nbr(k) : cf[c] # NONE}
    IN  IF C = {}
        THEN IF rank[k] = INF THEN KeepR(R)
             ELSE \* Detach (Parents._detach, parents.py:327-340; Dodag._poison,
                  \* dodag.py:551-557): rank 255, L and set forgotten, hold.
                  [rank |-> INF, pref |-> NONE, low |-> NONE, hold |-> TRUE,
                   poisoned |-> TRUE, toSend |-> Poisons, ver |-> R.ver, behind |-> FALSE,
                   win |-> FALSE, det |-> TRUE, chg |-> TRUE, att |-> FALSE]
        ELSE LET lowest == Min({cf[c] : c \in C})
                 best == Min({c \in C : cf[c] = lowest})
                 \* the preferred parent stays unless beaten by the threshold (parents.py:293-299)
                 ch == IF R.pref \in C /\ cf[R.pref] - cf[best] < SwitchThr
                       THEN R.pref ELSE best
                 r == cf[ch]
             IN  [rank |-> r, pref |-> ch,
                  low |-> IF R.low = NONE \/ r < R.low THEN r ELSE R.low,   \* parents.py:317-318
                  hold |-> FALSE, poisoned |-> FALSE, toSend |-> 0,          \* parents.py:314-316
                  ver |-> R.ver, behind |-> R.behind, win |-> R.win,
                  det |-> FALSE, chg |-> ch # pref[k], att |-> rank[k] = INF]

\* Selection after an event at k: nothing during the hold (parents.py:275-281); the root
\* selects nothing; Behind "detach": nothing while behind (dodag.py:508); Rejoin
\* "window", "window_rejoin": a detached node selects nothing in its answer window.
InWindow(R) == Rejoin # "first" /\ R.win /\ R.rank = INF

Sel(k, V, VV, Dr, Fr, R) ==
    IF k = Root \/ R.hold \/ InWindow(R) \/ (Behind = "detach" /\ R.behind) THEN KeepR(R)
    ELSE SelCore(k, V, VV, Dr, Fr, R)

\* Selection after a class 1 drop (Parents.next_parent, parents.py:300-318: recompute
\* called directly, not through the paused Dodag step).
SelDrop(k, V, VV, Dr, Fr, R) ==
    IF k = Root \/ R.hold \/ InWindow(R) THEN KeepR(R) ELSE SelCore(k, V, VV, Dr, Fr, R)

\* LateAck = "accept": a copy keeps its late acknowledgement whatever happens to
\* it (Reliable keeps the counters of the transmissions while the message is
\* queued); a waiting copy held with the root's acknowledgement on its way now
\* expects it late. Identity otherwise (late is always FALSE).
WithLate(c2, c) ==
    [c2 EXCEPT !.late = c.late \/ (LateAck = "accept" /\ c.st = "p" /\ c.hop = Root
                                   /\ c.acc = "exp" /\ c2.st # "p")]

(***************************************************************************)
(* Effect of a selection on the queued copies of the node (Routing._changed,*)
(* routing.py:180-195): after a parent change the copies not waiting      *)
(* follow the new parent with fresh attempts (TxQueue.redirect,            *)
(* txqueue.py:508-523); after a detach they are held (Reliable.hold,       *)
(* reliable.py:629-652). Waiting copies keep their next hop.               *)
(***************************************************************************)
ReshapeCore(c, S) ==
    IF c.st = "q" /\ S.det THEN Held
    \* "frame" (the code since fix 3): waiting copies are held too, not retried
    \* towards the former parent (routing.py _changed, select(_upward, True))
    ELSE IF c.st = "p" /\ S.det /\ Acks = "frame" THEN Held
    ELSE IF c.st = "q" /\ S.chg /\ S.pref # NONE /\ c.hop # S.pref THEN Queued(S.pref)
    ELSE c

Reshape(c, S) == WithLate(ReshapeCore(c, S), c)

(***************************************************************************)
(* Committing the new routing state S (one record per node) with views V, *)
(* VV, drops Dr, fresh flags Fr (rows of detaching or attaching nodes      *)
(* cleared: poisoned_at = now, parents.py:333; cleared at attach,          *)
(* parents.py:316), owed DODAG information OW, solicitations SO, and the   *)
(* ghost variables; s, H: the sender of the frame of this step and the     *)
(* nodes that heard it (NONE, {}: no frame). Answer window (Best): wh      *)
(* records the nodes heard in the window, rj the rank taken at a rejoin   *)
(* in it; both restart when the window opens. The sender's frame carries  *)
(* its rank (air, TrackAir); inc (IncAdv): a rank increase not yet on air. *)
(***************************************************************************)
CommitH(S, V, VV, Dr, Fr, OW, SO, s, H) ==
    LET NP == [k \in Nodes |-> S[k].pref]
    IN  /\ air' = IF TrackAir /\ s # NONE THEN [air EXCEPT ![s] = rank[s]] ELSE air
        /\ inc' = IF ~IncAdv THEN inc
                  ELSE [k \in Nodes |-> k # s /\ S[k].rank # INF /\ (inc[k] \/ S[k].rank > rank[k])]
        /\ UNCHANGED <<dc, rises>>
        /\ win' = [k \in Nodes |-> S[k].win]
        /\ wh' = IF ~Best THEN wh
                 ELSE [k \in Nodes |-> IF ~S[k].win \/ ~win[k] THEN AllF
                                       ELSE IF k \in H THEN [wh[k] EXCEPT ![s] = TRUE]
                                       ELSE wh[k]]
        /\ rj' = IF ~Best THEN rj
                 ELSE [k \in Nodes |-> IF S[k].win /\ ~win[k] THEN NONE
                                       ELSE IF S[k].att THEN S[k].rank
                                       ELSE IF S[k].det THEN NONE ELSE rj[k]]
        /\ rank' = [k \in Nodes |-> S[k].rank]
        /\ pref' = NP
        /\ low' = [k \in Nodes |-> S[k].low]
        /\ hold' = [k \in Nodes |-> S[k].hold]
        /\ poisoned' = [k \in Nodes |-> S[k].poisoned]
        /\ toSend' = [k \in Nodes |-> S[k].toSend]
        /\ ver' = [k \in Nodes |-> S[k].ver]
        /\ behind' = [k \in Nodes |-> S[k].behind]
        /\ view' = V
        /\ vview' = VV
        /\ dropped' = Dr
        /\ fresh' = [k \in Nodes |-> IF ~S[k].poisoned \/ S[k].det THEN AllF ELSE Fr[k]]
        /\ owe' = OW
        /\ sol' = SO
        /\ loops' = IF Ghosts /\ loops < LoopCap /\ Cycle(LiveP(NP)) /\ ~Cycle(LiveP(pref))
                    THEN loops + 1 ELSE loops
        /\ rl' = (rl \/ \E k \in Nodes : S[k].att /\ InCycle(k, LiveP(NP)))

Commit(S, V, VV, Dr, Fr, OW, SO) == CommitH(S, V, VV, Dr, Fr, OW, SO, NONE, {})

\* Owed information and solicitations after a step without frame: dropped when detached
\* (Trickle stopped) or no longer behind.
OwKeep(S) == [k \in Nodes |-> owe[k] /\ S[k].rank # INF]
SoKeep(S) == [k \in Nodes |-> sol[k] /\ S[k].behind]

(***************************************************************************)
(* A frame of s, with header rank rank[s] and version ver[s], carrying a   *)
(* DODAG information (dio) or not, an Announce of s (ann: a solicitation  *)
(* when s is behind) or not, heard by H. Receivers update their entry of  *)
(* s (NeighbourTable.heard, neighbours.py:336-360: rank, version, a new    *)
(* counter clears a drop, parents.py:352-360), react to its version       *)
(* (Dodag._heard, dodag.py:373-399; Dodag._on_info, dodag.py:420-445) and *)
(* select again (dodag.py:505-511), or adopt its version                  *)
(* (Parents.on_new_version, parents.py:207-240). SS: the sender's own      *)
(* routing record after the frame, SF its fresh row.                       *)
(***************************************************************************)
Receivers(s) == {k \in Nbr(s) : alive[k] /\ ~Severed(s, k)}

LossOK(s, L, extra) ==
    /\ Cardinality(L) + extra <= MaxLoss - lost
    /\ \A k \in L : consec[s][k] < MaxConsec

FrameCounters(s, L, H, extra) ==
    /\ lost' = lost + Cardinality(L) + extra
    /\ consec' = IF MaxConsec >= MaxLoss THEN consec     \* no constraint: not tracked
                 ELSE [consec EXCEPT ![s] = [k \in Nodes |->
                          IF k \in L THEN consec[s][k] + 1
                          ELSE IF k \in H THEN 0 ELSE consec[s][k]]]

Effect(s, H, dio, SS, SF, ann) ==
    LET fin == rank[s] # INF
        vs == ver[s]
        \* adoption (dodag.py:440-443): newer, or any other version when detached
        \* (Adopt "fresh": once its hold is over, i.e. heard after its solicitation)
        adopt(k) == /\ Versions > 1 /\ k # Root /\ dio /\ fin
                    /\ \/ rank[k] # INF /\ vs > ver[k]
                       \/ rank[k] = INF /\ vs # ver[k] /\ (Adopt = "any" \/ ~hold[k])
        \* behind (dodag.py:393-398), attached nodes only
        newer(k) == Versions > 1 /\ k # Root /\ fin /\ vs > ver[k] /\ rank[k] # INF
        \* Trickle reset with a DODAG information owed (dodag.py:390-392, 424-427)
        older(k) == Versions > 1 /\ fin /\ vs < ver[k]
        solicit == Versions > 1 /\ ~fin /\ ~SS.hold
        V  == [k \in Nodes |-> IF k \in H THEN [view[k] EXCEPT ![s] = rank[s]] ELSE view[k]]
        VV == IF Versions = 1 \/ ~fin THEN vview
              ELSE [k \in Nodes |-> IF k \in H THEN [vview[k] EXCEPT ![s] = vs] ELSE vview[k]]
        Dr == [k \in Nodes |-> IF k \in H
                               THEN (IF adopt(k) THEN AllF ELSE [dropped[k] EXCEPT ![s] = FALSE])
                               ELSE dropped[k]]
        Fr == [k \in Nodes |-> IF k = s THEN SF
                               ELSE IF k \in H /\ poisoned[k] THEN [fresh[k] EXCEPT ![s] = TRUE]
                               ELSE fresh[k]]
        \* Rejoin "window", "window_rejoin": a detached node adopting keeps its rejoin
        \* rule (candidates heard after the solicitation, parents.py poisoned_at) and
        \* its answer window.
        R(k) == IF adopt(k)
                THEN [rank |-> INF, pref |-> NONE, low |-> NONE, hold |-> FALSE,
                      poisoned |-> Rejoin # "first" /\ rank[k] = INF /\ poisoned[k],
                      toSend |-> 0, ver |-> vs, behind |-> FALSE, win |-> win[k]]
                ELSE [Rec(k) EXCEPT !.behind = behind[k] \/ newer(k)]
        \* Rejoin "window_rejoin": an adoption selects at once, window or not.
        SelK(k) == IF adopt(k) /\ Rejoin = "window_rejoin"
                   THEN SelCore(k, V, VV, Dr, Fr, R(k))
                   ELSE Sel(k, V, VV, Dr, Fr, R(k))
        \* TLCEval: computed once (a function constructor is otherwise evaluated at each use)
        S  == TLCEval([k \in Nodes |-> IF k = s THEN SS
                                        ELSE IF k \in H THEN SelK(k)
                                        ELSE Keep(k)])
        OW == [k \in Nodes |->
                 /\ S[k].rank # INF
                 /\ IF k = s THEN owe[k] /\ ~dio
                    ELSE \/ owe[k]
                         \/ k \in H /\ (adopt(k) \/ older(k) \/ solicit)]
        SO == IF Behind = "keep" \/ Versions = 1 THEN sol
              ELSE [k \in Nodes |-> S[k].behind /\ (sol[k] \/ (k = s /\ ann))]
    IN  [V |-> V, VV |-> VV, Dr |-> Dr, Fr |-> Fr, S |-> S, OW |-> OW, SO |-> SO]

\* A frame without class 1 message.
HeaderFrame(s, SS, SF, dio, ann) ==
    \E L \in SUBSET Receivers(s) :
        LET H == Receivers(s) \ L
            E == Effect(s, H, dio, SS, SF, ann)
        IN  /\ LossOK(s, L, 0)
            /\ CommitH(E.S, E.V, E.VV, E.Dr, E.Fr, E.OW, E.SO, s, H)
            /\ FrameCounters(s, L, H, 0)
            /\ cp' = [m \in Msgs |-> [k \in Nodes |-> Reshape(cp[m][k], E.S[k])]]
            /\ UNCHANGED <<alive, sub, giver, delivered, err, errFrom, rerr, xv, linkvars>>

(***************************************************************************)
(* Announce: a frame of an alive node without class 1 message.             *)
(* - attached nodes and the root: presence (reports.py), Trickle           *)
(*   advertisement with a DODAG information (dodag.py:576-586), at once    *)
(*   after a rank error (dodag.py:344-357), root beacon (always a DODAG    *)
(*   information); with one version, dio is irrelevant (always FALSE);     *)
(* - detached, in the hold, poison due: the poison (dodag.py:493-496);     *)
(* - detached after the hold: a solicitation, once per presence period    *)
(*   (dodag.py:512-516). Rank 255 in the header in both cases. With the    *)
(*   answer window (Track), it opens one; the window (Imin) closes before  *)
(*   the next solicitation (a presence period later).                      *)
(***************************************************************************)
DioSet(s) == IF Versions = 1 \/ rank[s] = INF THEN {FALSE}
             ELSE IF s = Root THEN {TRUE} ELSE BOOLEAN

Announce1(s, dio) ==
    /\ alive[s]
    /\ \/ /\ rank[s] # INF
          /\ HeaderFrame(s, Keep(s), fresh[s], dio, TRUE)
       \/ /\ rank[s] = INF /\ hold[s] /\ toSend[s] > 0 /\ ~dio
          /\ HeaderFrame(s, [Keep(s) EXCEPT !.toSend = toSend[s] - 1], fresh[s], FALSE, TRUE)
       \/ /\ rank[s] = INF /\ ~hold[s] /\ ~dio /\ ~(Track /\ win[s])
          /\ HeaderFrame(s, [Keep(s) EXCEPT !.win = Track], fresh[s], FALSE, TRUE)

Announce(s) == \E dio \in DioSet(s) : Announce1(s, dio)

(***************************************************************************)
(* End of the detach hold (dodag.py:506-516).                              *)
(* - code before fix 5 ("none", "repeat"): select at once among the        *)
(*   neighbours heard after the poison; without candidate, solicit (same   *)
(*   step).                                                                *)
(* - "solicit": solicit first; only neighbours heard after the            *)
(*   solicitation are candidates (no selection in this step); with the     *)
(*   answer window (Track), the solicitation opens it.                     *)
(* The poison and the solicitations wait at most 4 slots (DESYNC_SLOTS),    *)
(* the hold lasts 2 Imin = 64 slots: the hold of s ends after the first     *)
(* poison of each neighbour (the coincidence of a neighbour detaching in   *)
(* the last 4 slots of the hold is left out).                              *)
(***************************************************************************)
HoldEnd(s) ==
    /\ alive[s] /\ hold[s] /\ toSend[s] = 0
    \* timing: a poison leaves within 4 slots of its detach (dodag.py:120, 555),
    \* far less than the 2 Imin hold; every neighbour's first poison is out.
    /\ \A k \in Nbr(s) : alive[k] => toSend[k] < Poisons
    /\ LET free == [Keep(s) EXCEPT !.hold = FALSE, !.win = Track]
           S0 == SelCore(s, view, vview, dropped, fresh, Rec(s))
           S == [k \in Nodes |-> IF k = s THEN S0 ELSE Keep(k)]
       IN  IF Fix5 = "solicit"
           THEN HeaderFrame(s, free, AllF, FALSE, TRUE)
           ELSE IF S0.rank # INF
           THEN /\ Commit(S, view, vview, dropped, fresh, OwKeep(S), SoKeep(S))
                /\ cp' = [m \in Msgs |-> [cp[m] EXCEPT ![s] = Reshape(cp[m][s], S0)]]
                /\ UNCHANGED <<alive, lost, consec, sub, giver, delivered, err, errFrom, rerr, xv, linkvars>>
           ELSE HeaderFrame(s, free, fresh[s], FALSE, TRUE)

(***************************************************************************)
(* End of the 2 Imin behind a newer version (Behind "detach",             *)
(* dodag.py:497-499, 508-511): after its solicitation (sol) and the       *)
(* answers owed by its newer neighbours (Trickle within Imin), the node   *)
(* selects among the candidates of its own version.                       *)
(***************************************************************************)
BehindEnd(k) ==
    /\ Behind = "detach" /\ Versions > 1
    /\ alive[k] /\ behind[k] /\ sol[k]
    /\ \A s \in Nbr(k) : (alive[s] /\ ver[s] > ver[k]) => ~owe[s]
    /\ LET S0 == SelCore(k, view, vview, dropped, fresh, [Rec(k) EXCEPT !.behind = FALSE])
           S == [x \in Nodes |-> IF x = k THEN S0 ELSE Keep(x)]
       IN  /\ Commit(S, view, vview, dropped, fresh, OwKeep(S), SoKeep(S))
           /\ cp' = [m \in Msgs |-> [cp[m] EXCEPT ![k] = Reshape(cp[m][k], S0)]]
    /\ UNCHANGED <<alive, lost, consec, sub, giver, delivered, err, errFrom, rerr, xv, linkvars>>

(***************************************************************************)
(* Close of the answer window (Rejoin "window", "window_rejoin"), one Imin *)
(* after the solicitation went on air: a node still detached selects the  *)
(* best candidate heard after the solicitation ending its hold (at a      *)
(* rejoin the switch threshold plays no part: the lowest path cost). An   *)
(* event: the answers may come before or after it. Rejoin "first" with    *)
(* Best: the ghost window closes, nothing else.                            *)
(***************************************************************************)
WindowClose(k) ==
    /\ Track /\ alive[k] /\ win[k]
    /\ LET R  == [Rec(k) EXCEPT !.win = FALSE]
           S0 == IF Rejoin # "first" /\ rank[k] = INF
                 THEN SelCore(k, view, vview, dropped, fresh, R) ELSE KeepR(R)
           S  == [x \in Nodes |-> IF x = k THEN S0 ELSE Keep(x)]
       IN  /\ Commit(S, view, vview, dropped, fresh, OwKeep(S), SoKeep(S))
           /\ cp' = [m \in Msgs |-> [cp[m] EXCEPT ![k] = Reshape(cp[m][k], S0)]]
    /\ UNCHANGED <<alive, lost, consec, sub, giver, delivered, err, errFrom, rerr, xv, linkvars>>

\* The root issues a new DODAG version (global repair, Dodag.set_version, dodag.py:359-369):
\* Trickle reset with its DODAG information owed.
NewVersion ==
    /\ Versions > 1 /\ ver[Root] < Versions - 1
    /\ ver' = [ver EXCEPT ![Root] = ver[Root] + 1]
    /\ owe' = [owe EXCEPT ![Root] = TRUE]
    /\ UNCHANGED <<alive, rank, pref, low, hold, poisoned, toSend, view, fresh, dropped,
                   vview, behind, sol, win, wh, rj, lost, consec, msgvars, loops, rl, rerr, xv,
                   dc, rises, air, inc, linkvars>>

(***************************************************************************)
(* Silence (parents.py:164, dodag.py:536-549): the entry of a dead        *)
(* neighbour, or of one whose link to the node is dead (Cut), goes; the    *)
(* node selects again.                                                     *)
(***************************************************************************)
Expire(k, c) ==
    /\ alive[k] /\ c \in Nbr(k) /\ (~alive[c] \/ Severed(c, k)) /\ view[k][c] # NONE
    /\ LET V == [view EXCEPT ![k][c] = NONE]
           S == [x \in Nodes |-> IF x = k THEN Sel(k, V, vview, dropped, fresh, Rec(k)) ELSE Keep(x)]
       IN  /\ Commit(S, V, vview, dropped, fresh, OwKeep(S), SoKeep(S))
           /\ cp' = [m \in Msgs |-> [cp[m] EXCEPT ![k] = Reshape(cp[m][k], S[k])]]
    /\ UNCHANGED <<alive, lost, consec, sub, giver, delivered, err, errFrom, rerr, xv, linkvars>>

Die ==
    /\ Victim # Root /\ alive[Victim]
    /\ \A m \in Msgs : cp[m][Victim].st = "no"
    \* its explicit acknowledgements are out (sent at once)
    /\ \A m \in Msgs, k \in Nodes : ~(cp[m][k].st = "p" /\ cp[m][k].hop = Victim /\ cp[m][k].acc = "exp")
    /\ alive' = [alive EXCEPT ![Victim] = FALSE]
    /\ UNCHANGED <<rank, pref, low, hold, poisoned, toSend, view, fresh, dropped,
                   ver, vview, behind, sol, owe, win, wh, rj, lost, consec, msgvars, loops, rl,
                   rerr, xv, dc, rises, air, inc, linkvars>>

\* The link CutChild - Root dies (Cut), once, at any moment.
CutLink ==
    /\ Cut # "none" /\ ~cut
    /\ cut' = TRUE
    /\ UNCHANGED <<routing, lost, consec, msgvars, loops, rl, rerr, xv, rfail>>

(***************************************************************************)
(* Link cost rise (Rise): k's estimate of its link to c rises by one       *)
(* hop (failed acknowledgement samples, a falling reception ratio:         *)
(* neighbours.py:198). No selection in this step: the node reads the new   *)
(* cost at its next selection, after the next frame it hears              *)
(* (dodag.py:409, 549-557).                                                *)
(***************************************************************************)
CostUp(k, c) ==
    /\ rises < Rise /\ k # Root /\ alive[k] /\ c \in Nbr(k)
    /\ dc' = [dc EXCEPT ![k][c] = dc[k][c] + 1]
    /\ rises' = rises + 1
    /\ UNCHANGED <<routing0, air, inc, lost, consec, msgvars, loops, rl, rerr, xv, linkvars>>

(***************************************************************************)
(* Class 1.                                                                *)
(***************************************************************************)

\* Submission by the origin (Reliable.send_own, reliable.py:723-736): queued
\* towards the preferred parent, held without one.
Submit(m) ==
    LET o == Orig(m) IN
    /\ ~sub[m] /\ alive[o]
    /\ sub' = [sub EXCEPT ![m] = TRUE]
    /\ cp' = [cp EXCEPT ![m][o] = IF pref[o] # NONE THEN Queued(pref[o]) ELSE Held]
    /\ giver' = [giver EXCEPT ![m][o] = o]
    /\ UNCHANGED <<routing, lost, consec, delivered, err, errFrom, loops, rl, rerr, xv, linkvars>>

(***************************************************************************)
(* Transmission of copy m by s to its next hop d (Reliable.sent,           *)
(* reliable.py:404-435), in a frame heard by H.                            *)
(* Passive acknowledgements (Reliable.overheard, reliable.py:459-471): a   *)
(* node k whose copy waits for s ("pas") is acknowledged when it overhears *)
(* this forward; it is not when it is itself the destination (overheard is *)
(* called only for frames to other nodes, link.py:737-740) nor when it     *)
(* loses the frame: it then waits for its timeout. The owed explicit       *)
(* acknowledgement of s is cancelled by the forward (reliable.py:414).     *)
(* At d (link.py:741-771):                                                 *)
(* - data-path validation (Routing.inconsistent, routing.py:140-148): the *)
(*   sender's rank, of d's version, not above d's finite rank is a rank   *)
(*   error (counted, Trickle reset, advertisement at once); class 1 is     *)
(*   forwarded anyway (routing.py:160-164); a message of another version  *)
(*   is not validated and is forwarded;                                    *)
(* - a duplicate (the duplicate cache, link.py:753-756, or an own message, *)
(*   routing.py:165-171) is acknowledged and dropped; Fix3 "retake": kept  *)
(*   as held when it came from another node than the first sender (an own  *)
(*   message: from anyone); Fix3 "custody": dropped only while d holds a   *)
(*   copy, otherwise taken as new; Fix3 "refined": a copy of d waiting for *)
(*   the acknowledgement of s does not count as held (Reliable.holding      *)
(*   with source, reliable.py:357-378);                                     *)
(* - otherwise recorded (link.py:761) and queued towards d's preferred     *)
(*   parent after this frame's selection, or held (link.py:497-537,        *)
(*   routing.py:180-195);                                                  *)
(* - at the root: delivered (duplicates filtered by its cache).           *)
(* Acknowledgement: explicit for a duplicate, a message held or delivered *)
(* (link.py:762-771; the root's batching delay is part of the timeout):    *)
(* Acks "atomic": at once, unless lost (ackLost); Acks "frame": a later   *)
(* frame of d ("exp", AckFrame), which may come before or after other     *)
(* frames of d, or be lost. Passive ("pas") for a message queued by a     *)
(* relay that had a route (link.py:532).                                  *)
(***************************************************************************)
Dup(m, d, s) ==
    CASE Fix3 = "none"    -> giver[m][d] # NONE
      [] Fix3 = "retake"  -> giver[m][d] = s
      [] OTHER            -> FALSE                  \* custody: decided below

SendData(s, m) ==
    /\ alive[s] /\ cp[m][s].st = "q"
    /\ LET d == cp[m][s].hop IN
       \E L \in SUBSET Receivers(s), ackLost \in (IF Acks = "atomic" THEN BOOLEAN ELSE {FALSE}) :
         LET H  == Receivers(s) \ L
             got == d \in H
             extra == IF ackLost THEN 1 ELSE 0
             E  == Effect(s, H, FALSE, Keep(s), fresh[s], FALSE)
             S  == E.S
             \* passive acknowledgements of the copies waiting for s
             waits(k) == cp[m][k].st = "p" /\ cp[m][k].acc = "pas" /\ cp[m][k].hop = s
             C1 == [k \in Nodes |-> IF k # s /\ waits(k)
                                    THEN (IF k \in H /\ k # d THEN NoCopy
                                          ELSE [cp[m][k] EXCEPT !.acc = "no"])
                                    ELSE cp[m][k]]
             incons == got /\ d # Root /\ rank[d] # INF /\ ver[s] = ver[d] /\ rank[s] <= rank[d]
             old  == C1[d]
             seen == giver[m][d] # NONE
             take == IF S[d].pref # NONE THEN Queued(S[d].pref) ELSE Held
             \* outcome at d: "dup" (acknowledged, dropped), "new", "retake"
             out  == CASE d = Root -> "root"
                       [] Fix3 = "custody" -> IF old.st # "no" THEN "dup" ELSE "new"
                       [] Fix3 = "refined" ->
                             \* a copy waiting for s's acknowledgement is not held
                             IF old.st = "no" \/ (old.st = "p" /\ old.hop = s) THEN "new" ELSE "dup"
                       [] ~seen -> "new"
                       [] Dup(m, d, s) -> "dup"
                       [] Fix3 = "retake" -> "retake"
                       [] OTHER -> "dup"
             atD  == CASE out = "new"    -> take
                       [] out = "retake" -> Held
                       [] OTHER          -> Reshape(old, S[d])
             \* QUEUED: acknowledged by the forward; a duplicate taken again is
             \* acknowledged explicitly (link.py of the fix: "if dup or outcome != QUEUED")
             passive == out = "new" /\ ~seen /\ pref[d] # NONE
             mine == [st |-> "p", hop |-> d, att |-> cp[m][s].att + 1,
                      acc |-> IF ~got THEN "no" ELSE IF passive THEN "pas"
                              ELSE IF Acks = "frame" THEN "exp" ELSE "no",
                      late |-> cp[m][s].late]
             released == Acks = "atomic" /\ got /\ ~passive /\ ~ackLost
         IN  /\ ackLost => (got /\ ~passive)
             /\ LossOK(s, L, extra)
             /\ CommitH(S, E.V, E.VV, E.Dr, E.Fr, E.OW, E.SO, s, H)
             /\ FrameCounters(s, L, H, extra)
             /\ cp' = [mm \in Msgs |-> [k \in Nodes |->
                          IF mm = m /\ k = s THEN (IF released THEN NoCopy ELSE mine)
                          ELSE IF mm = m /\ k = d /\ got THEN atD
                          ELSE IF mm = m THEN Reshape(C1[k], S[k])
                          ELSE Reshape(cp[mm][k], S[k])]]
             /\ giver' = IF got /\ ~seen /\ d # Root
                         THEN [giver EXCEPT ![m][d] = IF Fix3 = "retake" THEN s ELSE d]
                         ELSE giver
             /\ delivered' = IF got /\ d = Root THEN [delivered EXCEPT ![m] = TRUE] ELSE delivered
             /\ rerr' = (rerr \/ (Ghosts /\ incons))
             /\ xv' = (xv \/ (Ghosts /\ got /\ ver[s] # ver[d]))
             /\ UNCHANGED <<alive, sub, err, errFrom, linkvars>>

\* An acknowledgement frame of h to k about m (Acks = "frame"): lost by k (k's
\* copy becomes NC), or heard (the copy goes; k selects again). On a dead link
\* it is lost, outside the loss bounds.
AckLost(h, k) == IF Severed(h, k) THEN {TRUE} ELSE BOOLEAN

AckOnAir(h, k, m, lostIt, NC) ==
    IF lostIt
    THEN /\ IF Severed(h, k) THEN UNCHANGED <<lost, consec>>
            ELSE LossOK(h, {k}, 0) /\ FrameCounters(h, {k}, {}, 0)
         \* a frame of h on air, lost by its destination
         /\ air' = IF TrackAir THEN [air EXCEPT ![h] = rank[h]] ELSE air
         /\ inc' = [inc EXCEPT ![h] = FALSE]
         /\ cp' = [cp EXCEPT ![m][k] = NC]
         /\ UNCHANGED <<routing0, dc, rises, loops, rl>>
    ELSE LET E == Effect(h, {k}, FALSE, Keep(h), fresh[h], FALSE)
         IN  /\ CommitH(E.S, E.V, E.VV, E.Dr, E.Fr, E.OW, E.SO, h, {k})
             /\ FrameCounters(h, {}, {k}, 0)
             /\ cp' = [mm \in Msgs |-> [cp[mm] EXCEPT ![k] =
                          IF mm = m THEN NoCopy ELSE Reshape(cp[mm][k], E.S[k])]]

(***************************************************************************)
(* Explicit acknowledgement of k's waiting copy by its hop h: "exp" (the  *)
(* duplicate, held or delivered case), or owed by a relay that has not     *)
(* forwarded within half a timeout (Reliable.owe, reliable.py:800-810,     *)
(* 858-862: queue, duty cycle, the copy held after a detach). It may be   *)
(* lost (then k times out). Acks "atomic": owed acknowledgements only, and *)
(* they carry no rank. Acks "frame": the acknowledgement is a frame of h   *)
(* heard by k: it updates k's entry of h (rank 255 if h is detached; its  *)
(* version) and k selects again (other neighbours overhearing it are not  *)
(* modelled). Elig: an acknowledgement of the root heard ends k's run of   *)
(* failed samples ("reach"); a lost one is a failed sample at the timeout. *)
(***************************************************************************)
AckFrame(k, m) ==
    /\ alive[k] /\ cp[m][k].st = "p"
    /\ LET c == cp[m][k]
           h == c.hop
       IN  /\ alive[h]
           /\ \/ c.acc = "exp"
              \/ c.acc = "pas" /\ cp[m][h].st \in {"q", "h"}
           /\ \E lostIt \in AckLost(h, k) :
                /\ rfail' = IF h = Root /\ ~lostIt THEN RootAcked(k) ELSE rfail
                /\ IF Acks = "atomic"
                   THEN IF lostIt
                        THEN /\ lost < MaxLoss /\ lost' = lost + 1 /\ UNCHANGED <<consec, air, inc>>
                             /\ cp' = [cp EXCEPT ![m][k] = [c EXCEPT !.acc = "no"]]
                             /\ UNCHANGED <<routing0, dc, rises, loops, rl>>
                        ELSE /\ cp' = [cp EXCEPT ![m][k] = NoCopy]
                             /\ UNCHANGED <<routing, lost, consec, loops, rl>>
                   ELSE AckOnAir(h, k, m, lostIt, [c EXCEPT !.acc = "no"])
    /\ UNCHANGED <<alive, sub, giver, delivered, err, errFrom, rerr, xv, cut>>

(***************************************************************************)
(* Late root acknowledgement (LateAck = "accept", Reliable.on_ack): the    *)
(* root's acknowledgement of an earlier transmission of k's copy comes,    *)
(* whatever the copy's state (queued for its retry, waiting for the        *)
(* retry's acknowledgement, held), and removes it; or it is lost. Elig     *)
(* "etx", "reach": heard, it ends k's run of failed samples ("reach");     *)
(* lost, the earlier transmission is a failed sample (decided now).        *)
(***************************************************************************)
LateAckFrame(k, m) ==
    /\ LateAck = "accept" /\ alive[k] /\ cp[m][k].late
    /\ \E lostIt \in AckLost(Root, k) :
         /\ rfail' = IF ~lostIt THEN RootAcked(k)
                     ELSE IF Elig = "none" THEN rfail ELSE RootFail(k)
         /\ AckOnAir(Root, k, m, lostIt, [cp[m][k] EXCEPT !.late = FALSE])
    /\ UNCHANGED <<alive, sub, giver, delivered, err, errFrom, rerr, xv, cut>>

(***************************************************************************)
(* Timeout (Reliable._timeout, reliable.py:534-564): fewer than 3          *)
(* attempts: retried towards the same hop (txqueue.py:478-493); else the   *)
(* hop is dropped (Reliable._exhausted, reliable.py:566-579;               *)
(* Parents.next_parent, parents.py:300-318), the node selects again, and   *)
(* the copy goes to the new preferred parent with fresh attempts, or is    *)
(* held (HOLD) when there is none or it is the same.                       *)
(* Towards the root (LateAck): "ignore", also while the root's             *)
(* acknowledgement is on its way, which is lost to the copy; "accept",     *)
(* also while it is on its way before the last attempt (it becomes late),  *)
(* never while a late one is expected (it comes within one root timeout),  *)
(* and the drop only once the last attempt's acknowledgement is out.       *)
(* Exhaust "keeproot", "keeplast": no drop while the hop is kept (KeepHop),*)
(* the copy is queued towards it again with fresh attempts. Elig "none":   *)
(* after a dead link (Cut), each timeout of CutChild towards the root is a *)
(* failed sample (rfail); "etx", "reach": each timeout towards the root    *)
(* with no acknowledgement on its way (one becoming late is decided by     *)
(* LateAckFrame; LateAck = "ignore": an ignored one too).                  *)
(***************************************************************************)
\* h is still a candidate of s apart from drops (heard, eligible, of s's version or
\* its kept parent behind a version, within the bound).
Usable(s, h) == CandCost(s, h, view, vview, [dropped EXCEPT ![s][h] = FALSE], fresh, Rec(s)) # NONE

KeepHop(s, h) ==
    CASE Exhaust = "keeproot" -> h = Root /\ Usable(s, h)
      [] Exhaust = "keeplast" -> /\ h = pref[s] /\ Usable(s, h)
                                 /\ \A c \in Nbr(s) \ {h} : CandCost(s, c, view, vview, dropped, fresh, Rec(s)) = NONE
      [] OTHER                -> FALSE

Timeout(s, m) ==
    /\ alive[s] /\ cp[m][s].st = "p"
    /\ LET c == cp[m][s]
           h == c.hop
           inflight == c.acc = "exp" /\ h = Root
       IN  /\ \/ c.acc = "no"
              \/ LateAck = "ignore" /\ inflight
              \/ LateAck = "accept" /\ inflight /\ c.att < ATTEMPTS
           /\ ~c.late
           /\ LET failed == IF Elig = "none" THEN cut /\ s = CutChild
                            ELSE c.acc = "no" \/ (LateAck = "ignore" /\ inflight)
              IN  rfail' = IF h = Root /\ failed THEN RootFail(s) ELSE rfail
           /\ IF c.att < ATTEMPTS
              THEN /\ cp' = [cp EXCEPT ![m][s] = [c EXCEPT !.st = "q", !.acc = "no",
                                                          !.late = (LateAck = "accept" /\ inflight)]]
                   /\ UNCHANGED <<routing, loops, rl>>
              ELSE IF KeepHop(s, h)
              THEN /\ cp' = [cp EXCEPT ![m][s] = Queued(h)]
                   /\ UNCHANGED <<routing, loops, rl>>
              ELSE LET Dr == [dropped EXCEPT ![s][h] = TRUE]
                       S  == [x \in Nodes |-> IF x = s THEN SelDrop(s, view, vview, Dr, fresh, Rec(s))
                                              ELSE Keep(x)]
                       p  == S[s].pref
                   IN  /\ Commit(S, view, vview, Dr, fresh, OwKeep(S), SoKeep(S))
                       /\ cp' = [mm \in Msgs |-> [cp[mm] EXCEPT ![s] =
                                   IF mm = m THEN (IF p = NONE \/ p = h THEN Held ELSE Queued(p))
                                   ELSE Reshape(cp[mm][s], S[s])]]
    /\ UNCHANGED <<alive, lost, consec, sub, giver, delivered, err, errFrom, rerr, xv, cut>>

\* A held copy resumes once there is a route (Reliable._resume_held, reliable.py:664-689).
Resume(s, m) ==
    /\ alive[s] /\ cp[m][s].st = "h" /\ pref[s] # NONE
    /\ cp' = [cp EXCEPT ![m][s] = WithLate(Queued(pref[s]), cp[m][s])]
    /\ UNCHANGED <<routing, lost, consec, sub, giver, delivered, err, errFrom, loops, rl, rerr, xv,
                   linkvars>>

\* Holding expired without route (reliable.py:677-679): failure (reliable.py:691-712):
\* the origin's own message fails to its application, a relay's makes an error.
HoldExpire(s, m) ==
    /\ alive[s] /\ cp[m][s].st = "h" /\ pref[s] = NONE
    \* the owed acknowledgements (half a timeout) precede the holding (2 periods)
    /\ \A k \in Nodes : ~(cp[m][k].st = "p" /\ cp[m][k].acc = "pas" /\ cp[m][k].hop = s)
    /\ cp' = [cp EXCEPT ![m][s] = NoCopy]
    /\ IF s = Orig(m)
       THEN err' = [err EXCEPT ![m] = "recv"] /\ UNCHANGED errFrom
       ELSE /\ err' = [err EXCEPT ![m] = IF err[m] = "recv" THEN "recv" ELSE "sent"]
            /\ errFrom' = [errFrom EXCEPT ![m] = s]
    /\ UNCHANGED <<routing, lost, consec, sub, giver, delivered, loops, rl, rerr, xv, linkvars>>

\* The error reaches the origin (abstraction: both ends connected to the root).
ErrorArrives(m) ==
    /\ err[m] = "sent" /\ Connected(errFrom[m]) /\ Connected(Orig(m))
    /\ err' = [err EXCEPT ![m] = "recv"]
    /\ UNCHANGED <<routing, lost, consec, sub, cp, giver, delivered, errFrom, loops, rl, rerr, xv,
                   linkvars>>

(***************************************************************************)
(* Specification. Initial state: the formed DODAG (shortest paths, lower   *)
(* address on ties), every view exact, version 0 everywhere.               *)
(***************************************************************************)
InitPref(n) ==
    IF n = Root THEN NONE
    ELSE CHOOSE p \in Nbr(n) : /\ InitRank[p] + Cost(n, p) = InitRank[n]
                               /\ \A q \in Nbr(n) : InitRank[q] + Cost(n, q) = InitRank[n] => p <= q

Init ==
    /\ alive = [n \in Nodes |-> TRUE]
    /\ rank = InitRank
    /\ pref = [n \in Nodes |-> InitPref(n)]
    /\ low = InitRank
    /\ hold = [n \in Nodes |-> FALSE] /\ poisoned = [n \in Nodes |-> FALSE]
    /\ toSend = [n \in Nodes |-> 0]
    /\ view = [k \in Nodes |-> [c \in Nodes |-> IF c \in Nbr(k) THEN InitRank[c] ELSE NONE]]
    /\ fresh = [k \in Nodes |-> AllF] /\ dropped = [k \in Nodes |-> AllF]
    /\ ver = [n \in Nodes |-> 0] /\ vview = [k \in Nodes |-> [c \in Nodes |-> 0]]
    /\ behind = [n \in Nodes |-> FALSE] /\ sol = [n \in Nodes |-> FALSE]
    /\ owe = [n \in Nodes |-> FALSE]
    /\ win = [n \in Nodes |-> FALSE] /\ wh = [k \in Nodes |-> AllF] /\ rj = [n \in Nodes |-> NONE]
    /\ lost = 0 /\ consec = [s \in Nodes |-> [k \in Nodes |-> 0]]
    /\ sub = [m \in Msgs |-> FALSE] /\ cp = [m \in Msgs |-> [n \in Nodes |-> NoCopy]]
    /\ giver = [m \in Msgs |-> [n \in Nodes |-> NONE]]
    /\ delivered = [m \in Msgs |-> FALSE] /\ err = [m \in Msgs |-> "none"]
    /\ errFrom = [m \in Msgs |-> NONE]
    /\ loops = 0 /\ rl = FALSE /\ rerr = FALSE /\ xv = FALSE
    /\ dc = [k \in Nodes |-> [c \in Nodes |-> 0]] /\ rises = 0
    /\ air = InitRank /\ inc = [n \in Nodes |-> FALSE]
    /\ cut = FALSE /\ rfail = [n \in Nodes |-> 0]

Protocol ==
    \/ \E s \in Nodes : Announce(s) \/ HoldEnd(s) \/ BehindEnd(s) \/ WindowClose(s)
    \/ \E k, c \in Nodes : Expire(k, c)
    \/ \E s \in Nodes, m \in Msgs :
          SendData(s, m) \/ AckFrame(s, m) \/ Timeout(s, m) \/ Resume(s, m) \/ HoldExpire(s, m)
          \/ LateAckFrame(s, m)
    \/ \E m \in Msgs : ErrorArrives(m)

\* IncAdv: a node whose rank rose sends its frame before anything else happens.
Owed == {k \in Nodes : inc[k]}

Next ==
    IF IncAdv /\ Owed # {}
    THEN \E k \in Owed : Announce(k)
    ELSE \/ Protocol \/ Die \/ CutLink \/ NewVersion \/ \E m \in Msgs : Submit(m)
         \/ \E k, c \in Nodes : CostUp(k, c)

\* The class 1 actions of one copy are enabled in disjoint states of that copy
\* ("q", "p"/"pas", "p"/"no", "h" with or without route): weak fairness of their
\* disjunction is weak fairness of each. With LateAck, a copy waiting for the
\* root's acknowledgement ("p"/"exp") enables both AckFrame and Timeout: either
\* step moves the copy on. A late acknowledgement has its own fairness.
CopyStep(s, m) == SendData(s, m) \/ AckFrame(s, m) \/ Timeout(s, m) \/ Resume(s, m) \/ HoldExpire(s, m)

\* With versions: an owed DODAG information is eventually sent (Trickle after its
\* reset; the root beacon), and the 2 Imin behind a newer version end.
VNodes == IF Versions > 1 THEN Nodes ELSE {}
BNodes == IF Versions > 1 /\ Behind = "detach" THEN Nodes ELSE {}
\* The answer window closes.
WNodes == IF Track THEN Nodes ELSE {}
\* A late root acknowledgement comes or is lost.
LNodes == IF LateAck = "accept" THEN Nodes \ {Root} ELSE {}

Fairness ==
    /\ \A s \in Nodes : WF_vars(Announce(s)) /\ WF_vars(HoldEnd(s))
    /\ \A k \in Nodes : \A c \in Nbr(k) : WF_vars(Expire(k, c))
    /\ \A s \in Nodes \ {Root}, m \in Msgs : WF_vars(CopyStep(s, m))
    /\ \A m \in Msgs : WF_vars(ErrorArrives(m))
    /\ \A s \in VNodes : WF_vars(owe[s] /\ Announce1(s, TRUE))
    /\ \A k \in BNodes : WF_vars(BehindEnd(k))
    /\ \A k \in WNodes : WF_vars(WindowClose(k))
    /\ \A k \in LNodes, m \in Msgs : WF_vars(LateAckFrame(k, m))

Spec == Init /\ [][Next]_vars
FairSpec == Spec /\ Fairness

(***************************************************************************)
(* Properties.                                                             *)
(***************************************************************************)
Holds(n, m) == alive[n] /\ cp[m][n].st # "no"

\* MP5, safety: a submitted message is delivered, failed (error), or held by a node
\* (queued, held, or sent and waiting: sent again after its timeout, or released
\* only by the forward of its next hop, which then holds it).
NoSilentLoss ==
    \A m \in Msgs : sub[m] => (delivered[m] \/ err[m] # "none" \/ \E n \in Nodes : Holds(n, m))

\* MP5, liveness: delivered, or the origin learns the failure.
MP5Live == \A m \in Msgs : sub[m] ~> (delivered[m] \/ err[m] = "recv")

\* Late root acknowledgements, exhausted attempts: no node drops the root after
\* failed attempts, unless its link to the root died (Cut). With Exhaust = "drop",
\* every such drop takes three timeouts of one copy towards the root, each after a
\* lost frame or acknowledgement, unless an acknowledgement on its way is ignored
\* (LateAck = "ignore"): with MaxLoss < ATTEMPTS it holds otherwise; with "keeproot"
\* or "keeplast", whatever the losses.
RootKept == \A n \in Nodes : dropped[n][Root] => cut /\ n = CutChild

\* Eligibility of the root link (Elig): no node's link to the root is made
\* ineligible, unless that link died (Cut). With "etx", every failed sample counts:
\* with MaxLoss >= INELIG collisions on a live link suffice; with "reach", a run of
\* consecutive failed samples beyond the loss budget (UNREACH) is needed.
RootReachable == \A n \in Nodes : Inel(n, Root) => cut /\ n = CutChild

\* MP11.
NoCycle == ~Cycle(LiveP(pref))
NoRejoinLoop == ~rl
LoopsEnd == <>[]NoCycle
\* Ranks consistent along preferred-parent chains: every attached node's parent is
\* alive, of its version, of rank strictly lower, and seen with its actual rank.
Consistent ==
    \A n \in Nodes : (alive[n] /\ n # Root /\ pref[n] # NONE) =>
        (alive[pref[n]] /\ ver[pref[n]] = ver[n] /\ rank[pref[n]] < rank[n]
         /\ view[n][pref[n]] = rank[pref[n]])
RanksSettle == <>[]Consistent

\* MP10: once losses stop, every node connected to the root ends attached, and
\* (MP15) every node cut off ends detached.
AllAttached == \A n \in Nodes : (alive[n] /\ GraphConnected(n)) => Connected(n)
CutOffDetached == \A n \in Nodes : (alive[n] /\ ~GraphConnected(n)) => rank[n] = INF
MP10Live == <>[]AllAttached
CutOffLive == <>[]CutOffDetached
\* Global repair: every node connected to the root ends in the root's version.
AllInVersion == \A n \in Nodes : (alive[n] /\ GraphConnected(n)) => ver[n] = ver[Root]
VersionSettle == <>[]AllInVersion

\* MP12 (no flapping): the preferred parents eventually stop changing.
RoutesSettle == <>[][pref' = pref]_vars

\* All the liveness properties at once (one tableau: faster than five).
Live == MP5Live /\ LoopsEnd /\ RanksSettle /\ MP10Live /\ CutOffLive

(***************************************************************************)
(* Stability (MP10, MP12): a node detaches only when its preferred parent  *)
(* is lost. Detaches(n): n goes from a finite rank to 255 in its version   *)
(* (a detach at an adoption, without parent of the new version, is left   *)
(* out). PLost(n): its preferred parent is gone (silent: entry removed),  *)
(* advertises 255, or was dropped after failed attempts.                   *)
(* - KeepParent (rank following): a node whose preferred parent is not   *)
(*   lost and is of its version detaches only when the path cost through   *)
(*   it exceeds the bound L + MaxInc.                                      *)
(* - NoLagDetach (version lag): a node whose preferred parent is not lost  *)
(*   never detaches because that parent moved to a newer version (no      *)
(*   connected node detaches during a global repair).                     *)
(***************************************************************************)
Detaches(n) == n # Root /\ rank[n] # INF /\ rank'[n] = INF /\ ver'[n] = ver[n]
PLost(n) == pref[n] = NONE \/ view'[n][pref[n]] \in {NONE, INF} \/ dropped'[n][pref[n]]
OverBound(n) == view'[n][pref[n]] + LCost(n, pref[n]) > Bound(n)

KeepParent ==
    [][\A n \in Nodes : (Detaches(n) /\ ~PLost(n) /\ vview'[n][pref[n]] = ver[n]) => OverBound(n)]_vars
NoLagDetach ==
    [][\A n \in Nodes : (Detaches(n) /\ ~PLost(n)) => vview'[n][pref[n]] = ver[n]]_vars

(***************************************************************************)
(* Rejoin (answer window, Best): when a node's answer window closes with  *)
(* the node attached through a parent taken in that window (rj), the path *)
(* cost of that rejoin is minimal among the candidates heard in the window *)
(* (wh), each judged by its last frame heard: finite rank, of the node's  *)
(* version, not dropped, path cost within 254 (no L after a detach).       *)
(* Losses: a neighbour whose answers were all lost is not among them.      *)
(***************************************************************************)
WinBest(k) ==
    Min({INF} \cup {view[k][c] + LCost(k, c) :
                      c \in {c \in Nbr(k) : /\ wh[k][c] /\ view[k][c] \notin {NONE, INF}
                                            /\ vview[k][c] = ver[k] /\ ~dropped[k][c]
                                            /\ view[k][c] + LCost(k, c) <= MaxRank}})

RejoinBest ==
    [][\A k \in Nodes : (win[k] /\ ~win'[k] /\ rj'[k] # NONE) => rj'[k] <= WinBest(k)]_vars

\* Non-vacuity witnesses (each must be violated).
NeverDelivered == \A m \in Msgs : ~delivered[m]
NeverError == \A m \in Msgs : err[m] # "recv"
NoRankError == ~rerr
\* A copy sent again while the root's acknowledgement of an earlier transmission
\* is still expected (LateAck = "accept").
NoLateRetry == \A m \in Msgs, n \in Nodes : ~(cp[m][n].late /\ cp[m][n].st = "p")
AtMostOneLoop == loops <= 1
\* Exhaust "keeproot", "keeplast": no copy gets fresh attempts towards the root after
\* exhausting its attempts there.
NoFreshRoot ==
    [][\A m \in Msgs, n \in Nodes : ~(/\ cp[m][n].st = "p" /\ cp[m][n].hop = Root
                                      /\ cp[m][n].att = ATTEMPTS
                                      /\ cp'[m][n] = Queued(Root))]_vars
\* Elig = "reach": no run of MaxLoss consecutive failed samples towards the root (a
\* run reaching the loss budget: UNREACH = MaxLoss + 1 is tight, with MaxConsec = 1).
ShortFailRuns == \A n \in Nodes : rfail[n] < MaxLoss
\* Rank following: no node keeps its preferred parent once it hears it, in its version,
\* at a rank not below its own.
NoFollow ==
    [][\A n \in Nodes : (n # Root /\ pref[n] # NONE /\ pref'[n] = pref[n]
                         /\ view'[n][pref[n]] \notin {NONE, INF} /\ vview'[n][pref[n]] = ver[n])
                        => view'[n][pref[n]] < rank[n]]_vars
\* The bound: no node detaches because the path cost through its parent exceeds L + MaxInc.
NoBoundDetach ==
    [][\A n \in Nodes : (Detaches(n) /\ ~PLost(n) /\ vview'[n][pref[n]] = ver[n]) => ~OverBound(n)]_vars
\* Version lag: no node is ever behind a newer version; no node is attached through a
\* parent of a newer version; no upward message crosses versions.
NeverBehind == \A n \in Nodes : ~behind[n]
NoLagParent ==
    ~\E n \in Nodes : /\ alive[n] /\ pref[n] # NONE /\ view[n][pref[n]] \notin {NONE, INF}
                      /\ vview[n][pref[n]] > ver[n]
NoCrossVersion == ~xv
\* Answer window: no node rejoins in its window (Best).
NoWindowRejoin == [][\A k \in Nodes : (win[k] /\ ~win'[k]) => rj'[k] = NONE]_vars
=============================================================================
