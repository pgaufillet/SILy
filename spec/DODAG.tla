------------------------------- MODULE DODAG -------------------------------
(***************************************************************************)
(* Upward routing, detach and rejoin, and class 1 custody on a small      *)
(* graph (MESH_DESIGN §9 rank and parent set, loop freedom, Trickle and    *)
(* solicitation; §12 class 1; §18 design issues 3 and 5; MP5, MP10, MP11). *)
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
(*   a parent change, root beacon): Announce.                             *)
(*                                                                         *)
(* Ranks are in units of one perfect hop (MIN_HOP = 8, parents.py:58);    *)
(* link costs are whole hops (ETX 1, 2, ... ; margin always good, links    *)
(* always eligible). Hence the rank hysteresis of 4 (parents.py:278) never *)
(* holds a change back (every change is >= 8) and the switch threshold 12  *)
(* (parents.py:62) becomes a gain of 2 hops. MaxRank stands for 254       *)
(* (parents.py:59), MaxInc for MaxRankIncrease 56 (parents.py:60), both   *)
(* scaled down. One DODAG version (versions, global repair and the        *)
(* parameters of the DODAG information are not modelled). Parent sets are *)
(* not truncated to 3 members (every node has at most 3 neighbours).       *)
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
(* retrying its waiting copies towards its former parent (the M3-12 code). *)
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
(* 494-558; its own custody is not modelled).                             *)
(*                                                                         *)
(* The relay Victim may die once, at a moment it holds no message (a      *)
(* relay dying with a message it acknowledged is MP5's accepted exception, *)
(* §12, left out of the model).                                            *)
(*                                                                         *)
(* Fixes (MESH_DESIGN §18):                                               *)
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
(***************************************************************************)
EXTENDS Integers, FiniteSets

CONSTANTS
    Graph,      \* "line", "diamond", "subtree" (see Edges)
    Victim,     \* the relay that may die once
    Origins,    \* origins of the class 1 messages, one message each (named by its origin)
    MaxLoss,    \* frame and acknowledgement losses overall
    MaxConsec,  \* consecutive frames of one node lost by one neighbour
    MaxRank,    \* highest usable path cost (254, scaled)
    MaxInc,     \* MaxRankIncrease (56, scaled)
    Fix3,       \* "none" (the code), "custody", "refined" or "retake"
    Fix5,       \* "none" (the code), "solicit" or "repeat"
    Ghosts,     \* BOOLEAN: count loop formations and rank errors (witnesses only)
    Acks        \* "atomic" (first model) or "frame" (acknowledgements are frames)

NONE == -1
Root == 0
INF  == MaxRank + 1        \* rank 255: no route, or poisoned
SwitchThr == 2             \* 12 / 8 = 1.5 hops: a gain of 2 whole hops (parents.py:268)
ATTEMPTS == 3              \* reliable.py:72
LoopCap == 2               \* saturation of the loop formation counter

(***************************************************************************)
(* Graphs: edges <<a, b, cost>>, cost in hops (ETX).                       *)
(* - line: 0 - 1 - 2 - 3; relay 1 dies: 2 and 3 are cut off.               *)
(* - diamond: 0 - 1, 0 - 2, 1 - 3, 2 - 3, 3 - 4; relay 1 dies: 3 falls    *)
(*   back on 2 (local repair), 4 follows.                                  *)
(* - subtree: 0 - 1, 1 - 2, 1 - 3, 2 - 3, 3 - 4 and a costly link 0 - 4   *)
(*   (ETX 3); relay 1 dies: 2 and 3 can loop through each other before    *)
(*   reaching the root again through 4.                                    *)
(***************************************************************************)
Edges ==
    CASE Graph = "line"    -> {<<0, 1, 1>>, <<1, 2, 1>>, <<2, 3, 1>>}
      [] Graph = "diamond" -> {<<0, 1, 1>>, <<0, 2, 1>>, <<1, 3, 1>>, <<2, 3, 1>>, <<3, 4, 1>>}
      [] Graph = "subtree" -> {<<0, 1, 1>>, <<1, 2, 1>>, <<1, 3, 1>>, <<2, 3, 1>>, <<3, 4, 1>>,
                               <<0, 4, 3>>}

Nodes == {e[1] : e \in Edges} \cup {e[2] : e \in Edges}
\* Constant functions (computed once by TLC).
NbrF == [n \in Nodes |-> {m \in Nodes : \E e \in Edges : {e[1], e[2]} = {n, m}}]
CostF == [p \in Nodes \X Nodes |->
            IF \E e \in Edges : {e[1], e[2]} = {p[1], p[2]}
            THEN (CHOOSE e \in Edges : {e[1], e[2]} = {p[1], p[2]})[3] ELSE 0]
Nbr(n) == NbrF[n]
Cost(n, m) == CostF[<<n, m>>]
Msgs == Origins
Orig(m) == m

Min(S) == CHOOSE x \in S : \A y \in S : x <= y

ASSUME Root \in Nodes /\ Victim \in Nodes \ {Root}
ASSUME Origins \subseteq Nodes \ {Root}
ASSUME MaxLoss \in Nat /\ MaxConsec \in Nat \ {0} /\ MaxInc \in Nat \ {0} /\ Ghosts \in BOOLEAN
ASSUME Acks \in {"atomic", "frame"}
ASSUME Fix3 \in {"none", "retake", "custody", "refined"} /\ Fix5 \in {"none", "solicit", "repeat"}

\* Poison frames per detach (dodag.py:523-529: once).
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
    low,       \* L, lowest rank advertised since the last detach (NONE: none)
    hold,      \* in the detach hold (2 Imin): selects nothing
    poisoned,  \* detached since a poison: candidates must be fresh
    toSend,    \* poison frames still to send
    view,      \* view[k][c]: rank of c in its last frame heard by k (NONE: absent)
    fresh,     \* fresh[k][c]: heard after k's poison (Fix5 "solicit": after its solicitation)
    dropped,   \* dropped[k][c]: dropped after failed attempts, until a new frame of c
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
    rerr       \* a rank error was detected (data-path validation)

routing == <<alive, rank, pref, low, hold, poisoned, toSend, view, fresh, dropped>>
msgvars == <<sub, cp, giver, delivered, err, errFrom>>
vars == <<routing, lost, consec, msgvars, loops, rl, rerr>>

Copies == [st : {"no", "q", "p", "h"}, hop : Nodes \cup {NONE}, att : 0..ATTEMPTS,
           acc : {"no", "pas", "exp"}]
NoCopy == [st |-> "no", hop |-> NONE, att |-> 0, acc |-> "no"]
Held   == [st |-> "h", hop |-> NONE, att |-> 0, acc |-> "no"]
Queued(h) == [st |-> "q", hop |-> h, att |-> 0, acc |-> "no"]
AllF == [c \in Nodes |-> FALSE]

TypeOK ==
    /\ alive \in [Nodes -> BOOLEAN]
    /\ rank \in [Nodes -> 0..INF] /\ pref \in [Nodes -> Nodes \cup {NONE}]
    /\ low \in [Nodes -> 0..MaxRank \cup {NONE}]
    /\ hold \in [Nodes -> BOOLEAN] /\ poisoned \in [Nodes -> BOOLEAN]
    /\ toSend \in [Nodes -> 0..2]
    /\ view \in [Nodes -> [Nodes -> 0..INF \cup {NONE}]]
    /\ fresh \in [Nodes -> [Nodes -> BOOLEAN]] /\ dropped \in [Nodes -> [Nodes -> BOOLEAN]]
    /\ lost \in 0..MaxLoss /\ consec \in [Nodes -> [Nodes -> 0..MaxConsec]]
    /\ sub \in [Msgs -> BOOLEAN] /\ cp \in [Msgs -> [Nodes -> Copies]]
    /\ giver \in [Msgs -> [Nodes -> Nodes \cup {NONE}]]
    /\ delivered \in [Msgs -> BOOLEAN] /\ err \in [Msgs -> {"none", "sent", "recv"}]
    /\ errFrom \in [Msgs -> Nodes \cup {NONE}]
    /\ loops \in 0..LoopCap /\ rl \in BOOLEAN /\ rerr \in BOOLEAN

(***************************************************************************)
(* Chains of preferred parents (dead nodes have none).                     *)
(***************************************************************************)
LiveP(P) == [n \in Nodes |-> IF alive[n] THEN P[n] ELSE NONE]

RECURSIVE Follow(_, _, _)
Follow(n, P, k) == IF k = 0 \/ n = NONE THEN n ELSE Follow(P[n], P, k - 1)

InCycle(n, P) == \E k \in 1..Cardinality(Nodes) : Follow(n, P, k) = n
Cycle(P) == \E n \in Nodes : P[n] # NONE /\ InCycle(n, P)

RECURSIVE ToRoot(_, _)
ToRoot(n, k) == n = Root \/ (k > 0 /\ alive[n] /\ pref[n] # NONE /\ ToRoot(pref[n], k - 1))
Connected(n) == ToRoot(n, Cardinality(Nodes))

\* n is connected to the root in the graph of the live nodes.
RECURSIVE Reach(_, _)
Reach(S, k) == IF k = 0 THEN S
               ELSE Reach(S \cup {m \in Nodes : alive[m] /\ \E x \in S : m \in Nbr(x)}, k - 1)
GraphConnected(n) == n \in Reach({Root}, Cardinality(Nodes))

(***************************************************************************)
(* Parent selection (Parents._select, parents.py:244-296).                 *)
(* Candidate (Parents.candidate_cost, parents.py:149-162): a neighbour     *)
(* with a finite rank strictly below the node's advertised rank (any       *)
(* finite rank when detached), heard within 2 periods (an entry), heard   *)
(* after the poison while detached (parents.py:155), not dropped after    *)
(* failed attempts (parents.py:157), whose path cost is within the bound  *)
(* min(254, L + 56) (parents.py:142-147, 162).                             *)
(***************************************************************************)
Bound(k) == IF low[k] = NONE THEN MaxRank
            ELSE IF low[k] + MaxInc < MaxRank THEN low[k] + MaxInc ELSE MaxRank

CandCost(k, c, V, Dr, Fr) ==
    IF /\ V[k][c] # NONE /\ V[k][c] # INF /\ V[k][c] < rank[k]
       /\ (~poisoned[k] \/ Fr[k][c]) /\ ~Dr[k][c]
       /\ V[k][c] + Cost(k, c) <= Bound(k)
    THEN V[k][c] + Cost(k, c) ELSE NONE

Keep(k) == [rank |-> rank[k], pref |-> pref[k], low |-> low[k], hold |-> hold[k],
            poisoned |-> poisoned[k], toSend |-> toSend[k],
            det |-> FALSE, chg |-> FALSE, att |-> FALSE]

\* Selection at k from views V, drops Dr, fresh flags Fr (hold not tested).
SelCore(k, V, Dr, Fr) ==
    LET cf == [c \in Nbr(k) |-> CandCost(k, c, V, Dr, Fr)]
        cost(c) == cf[c]
        C == {c \in Nbr(k) : cf[c] # NONE}
    IN  IF C = {}
        THEN IF rank[k] = INF THEN Keep(k)
             ELSE \* Detach (Parents._detach, parents.py:298-310; Dodag._poison,
                  \* dodag.py:523-529): rank 255, L and set forgotten, hold.
                  [rank |-> INF, pref |-> NONE, low |-> NONE, hold |-> TRUE,
                   poisoned |-> TRUE, toSend |-> Poisons,
                   det |-> TRUE, chg |-> TRUE, att |-> FALSE]
        ELSE LET lowest == Min({cf[c] : c \in C})
                 best == Min({c \in C : cf[c] = lowest})
                 \* the preferred parent stays unless beaten by the threshold (parents.py:262-270)
                 ch == IF pref[k] \in C /\ cost(pref[k]) - cost(best) < SwitchThr
                       THEN pref[k] ELSE best
                 r == cost(ch)
             IN  [rank |-> r, pref |-> ch,
                  low |-> IF low[k] = NONE \/ r < low[k] THEN r ELSE low[k],   \* parents.py:288-289
                  hold |-> FALSE, poisoned |-> FALSE, toSend |-> 0,           \* parents.py:285-287
                  det |-> FALSE, chg |-> ch # pref[k], att |-> rank[k] = INF]

\* Selection after an event at k: nothing during the hold (parents.py:249-252); the root selects nothing.
Sel(k, V, Dr, Fr) == IF k = Root \/ hold[k] THEN Keep(k) ELSE SelCore(k, V, Dr, Fr)

(***************************************************************************)
(* Effect of a selection on the queued copies of the node (Routing._changed,*)
(* routing.py:172-184): after a parent change the copies not waiting      *)
(* follow the new parent with fresh attempts (TxQueue.redirect,            *)
(* txqueue.py:503-518); after a detach they are held (Reliable.hold,       *)
(* reliable.py:434-455). Waiting copies keep their next hop.               *)
(***************************************************************************)
Reshape(c, S) ==
    IF c.st = "q" /\ S.det THEN Held
    \* "frame" (the code since fix 3): waiting copies are held too, not retried
    \* towards the former parent (routing.py _changed, select(_upward, True))
    ELSE IF c.st = "p" /\ S.det /\ Acks = "frame" THEN Held
    ELSE IF c.st = "q" /\ S.chg /\ S.pref # NONE /\ c.hop # S.pref THEN Queued(S.pref)
    ELSE c

(***************************************************************************)
(* Committing the new routing state S (one record per node) with views V, *)
(* drops Dr, fresh flags Fr (rows of detaching or attaching nodes cleared: *)
(* poisoned_at = now, parents.py:304; cleared at attach, parents.py:287), *)
(* and the ghost variables.                                                *)
(***************************************************************************)
Commit(S, V, Dr, Fr) ==
    LET NP == [k \in Nodes |-> S[k].pref]
    IN  /\ rank' = [k \in Nodes |-> S[k].rank]
        /\ pref' = NP
        /\ low' = [k \in Nodes |-> S[k].low]
        /\ hold' = [k \in Nodes |-> S[k].hold]
        /\ poisoned' = [k \in Nodes |-> S[k].poisoned]
        /\ toSend' = [k \in Nodes |-> S[k].toSend]
        /\ view' = V
        /\ dropped' = Dr
        /\ fresh' = [k \in Nodes |-> IF ~S[k].poisoned \/ S[k].det THEN AllF ELSE Fr[k]]
        /\ loops' = IF Ghosts /\ loops < LoopCap /\ Cycle(LiveP(NP)) /\ ~Cycle(LiveP(pref))
                    THEN loops + 1 ELSE loops
        /\ rl' = (rl \/ \E k \in Nodes : S[k].att /\ InCycle(k, LiveP(NP)))

(***************************************************************************)
(* A frame of s, with header rank hr (rank[s]), lost by the neighbours in  *)
(* L, heard by H. Receivers update their entry of s (NeighbourTable.heard, *)
(* neighbours.py:292-318: rank, a new counter clears a drop, parents.py:  *)
(* 322-330) and select again (dodag.py:361, 481-487). SS: the sender's    *)
(* own routing record after the frame.                                     *)
(***************************************************************************)
Receivers(s) == {k \in Nbr(s) : alive[k]}

LossOK(s, L, extra) ==
    /\ Cardinality(L) + extra <= MaxLoss - lost
    /\ \A k \in L : consec[s][k] < MaxConsec

FrameRouting(s, H, SS, V, Dr, Fr) ==
    [k \in Nodes |-> IF k = s THEN SS ELSE IF k \in H THEN Sel(k, V, Dr, Fr) ELSE Keep(k)]

FrameCounters(s, L, H, extra) ==
    /\ lost' = lost + Cardinality(L) + extra
    /\ consec' = IF MaxConsec >= MaxLoss THEN consec     \* no constraint: not tracked
                 ELSE [consec EXCEPT ![s] = [k \in Nodes |->
                          IF k \in L THEN consec[s][k] + 1
                          ELSE IF k \in H THEN 0 ELSE consec[s][k]]]

\* A frame without class 1 message. SS: sender's routing record; SF: sender's fresh row.
HeaderFrame(s, SS, SF) ==
    \E L \in SUBSET Receivers(s) :
        LET H  == Receivers(s) \ L
            V  == [k \in Nodes |-> IF k \in H THEN [view[k] EXCEPT ![s] = rank[s]] ELSE view[k]]
            Dr == [k \in Nodes |-> IF k \in H THEN [dropped[k] EXCEPT ![s] = FALSE] ELSE dropped[k]]
            Fr == [k \in Nodes |-> IF k = s THEN SF
                                   ELSE IF k \in H /\ poisoned[k] THEN [fresh[k] EXCEPT ![s] = TRUE]
                                   ELSE fresh[k]]
            S  == FrameRouting(s, H, SS, V, Dr, Fr)
        IN  /\ LossOK(s, L, 0)
            /\ Commit(S, V, Dr, Fr)
            /\ FrameCounters(s, L, H, 0)
            /\ cp' = [m \in Msgs |-> [k \in Nodes |-> Reshape(cp[m][k], S[k])]]
            /\ UNCHANGED <<alive, sub, giver, delivered, err, errFrom, rerr>>

(***************************************************************************)
(* Announce: a frame of an alive node without class 1 message.             *)
(* - attached nodes and the root: presence (reports.py), Trickle           *)
(*   advertisement (dodag.py:433-435, 548-555), advertisement at once after *)
(*   a rank error (dodag.py:329-342), root beacon;                         *)
(* - detached, in the hold, poison due: the poison (dodag.py:469-472);     *)
(* - detached after the hold: a solicitation, once per presence period    *)
(*   (dodag.py:488-492). Rank 255 in the header in both cases.             *)
(***************************************************************************)
Announce(s) ==
    /\ alive[s]
    /\ \/ /\ rank[s] # INF
          /\ HeaderFrame(s, Keep(s), fresh[s])
       \/ /\ rank[s] = INF /\ hold[s] /\ toSend[s] > 0
          /\ HeaderFrame(s, [Keep(s) EXCEPT !.toSend = toSend[s] - 1], fresh[s])
       \/ /\ rank[s] = INF /\ ~hold[s]
          /\ HeaderFrame(s, Keep(s), fresh[s])

(***************************************************************************)
(* End of the detach hold (dodag.py:482-492).                              *)
(* - code ("none", "repeat"): select at once among the neighbours heard    *)
(*   after the poison; without candidate, solicit (same step).             *)
(* - "solicit": solicit first; only neighbours heard after the            *)
(*   solicitation are candidates (no selection in this step).              *)
(* The poison and the solicitations wait at most 4 slots (DESYNC_SLOTS),    *)
(* the hold lasts 2 Imin = 64 slots: the hold of s ends after the first     *)
(* poison of each neighbour (the coincidence of a neighbour detaching in   *)
(* the last 4 slots of the hold is left out).                              *)
(***************************************************************************)
HoldEnd(s) ==
    /\ alive[s] /\ hold[s] /\ toSend[s] = 0
    \* timing: a poison leaves within 4 slots of its detach (dodag.py:106, 527),
    \* far less than the 2 Imin hold; every neighbour's first poison is out.
    /\ \A k \in Nbr(s) : alive[k] => toSend[k] < Poisons
    /\ LET free == [Keep(s) EXCEPT !.hold = FALSE]
           S0 == SelCore(s, view, dropped, fresh)
       IN  IF Fix5 = "solicit"
           THEN HeaderFrame(s, free, AllF)
           ELSE IF S0.rank # INF
           THEN /\ Commit([k \in Nodes |-> IF k = s THEN S0 ELSE Keep(k)], view, dropped, fresh)
                /\ cp' = [m \in Msgs |-> [cp[m] EXCEPT ![s] = Reshape(cp[m][s], S0)]]
                /\ UNCHANGED <<alive, lost, consec, sub, giver, delivered, err, errFrom, rerr>>
           ELSE HeaderFrame(s, free, fresh[s])

(***************************************************************************)
(* Silence (parents.py:153, dodag.py:512-521): a dead neighbour's entry   *)
(* goes; the node selects again.                                           *)
(***************************************************************************)
Expire(k, c) ==
    /\ alive[k] /\ c \in Nbr(k) /\ ~alive[c] /\ view[k][c] # NONE
    /\ LET V == [view EXCEPT ![k][c] = NONE]
           S == [x \in Nodes |-> IF x = k THEN Sel(k, V, dropped, fresh) ELSE Keep(x)]
       IN  /\ Commit(S, V, dropped, fresh)
           /\ cp' = [m \in Msgs |-> [cp[m] EXCEPT ![k] = Reshape(cp[m][k], S[k])]]
    /\ UNCHANGED <<alive, lost, consec, sub, giver, delivered, err, errFrom, rerr>>

Die ==
    /\ alive[Victim]
    /\ \A m \in Msgs : cp[m][Victim].st = "no"
    \* its explicit acknowledgements are out (sent at once)
    /\ \A m \in Msgs, k \in Nodes : ~(cp[m][k].st = "p" /\ cp[m][k].hop = Victim /\ cp[m][k].acc = "exp")
    /\ alive' = [alive EXCEPT ![Victim] = FALSE]
    /\ UNCHANGED <<rank, pref, low, hold, poisoned, toSend, view, fresh, dropped,
                   lost, consec, msgvars, loops, rl, rerr>>

(***************************************************************************)
(* Class 1.                                                                *)
(***************************************************************************)

\* Submission by the origin (Reliable.send_own, reliable.py:526-539): queued
\* towards the preferred parent, held without one.
Submit(m) ==
    LET o == Orig(m) IN
    /\ ~sub[m] /\ alive[o]
    /\ sub' = [sub EXCEPT ![m] = TRUE]
    /\ cp' = [cp EXCEPT ![m][o] = IF pref[o] # NONE THEN Queued(pref[o]) ELSE Held]
    /\ giver' = [giver EXCEPT ![m][o] = o]
    /\ UNCHANGED <<routing, lost, consec, delivered, err, errFrom, loops, rl, rerr>>

(***************************************************************************)
(* Transmission of copy m by s to its next hop d (Reliable.sent,           *)
(* reliable.py:336-364), in a frame heard by H.                            *)
(* Passive acknowledgements (Reliable.overheard, reliable.py:366-376): a   *)
(* node k whose copy waits for s ("pas") is acknowledged when it overhears *)
(* this forward; it is not when it is itself the destination (overheard is *)
(* called only for frames to other nodes, link.py:737-740) nor when it     *)
(* loses the frame: it then waits for its timeout. The owed explicit       *)
(* acknowledgement of s is cancelled by the forward (reliable.py:345-346). *)
(* At d (link.py:741-771):                                                 *)
(* - data-path validation (Routing.inconsistent, routing.py:136-144): the *)
(*   sender's rank not above d's finite rank is a rank error (counted,     *)
(*   Trickle reset, advertisement at once); class 1 is forwarded anyway    *)
(*   (routing.py:156-160);                                                 *)
(* - a duplicate (the duplicate cache, link.py:753-756, or an own message, *)
(*   routing.py:161-163) is acknowledged and dropped; Fix3 "retake": kept  *)
(*   as held when it came from another node than the first sender (an own  *)
(*   message: from anyone); Fix3 "custody": dropped only while d holds a   *)
(*   copy, otherwise taken as new; Fix3 "refined": a copy of d waiting for *)
(*   the acknowledgement of s does not count as held (Reliable.holding      *)
(*   with source, reliable.py of the fix);                                  *)
(* - otherwise recorded (link.py:761) and queued towards d's preferred     *)
(*   parent after this frame's selection, or held (link.py:497-537,        *)
(*   routing.py:172-184);                                                  *)
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
             V  == [k \in Nodes |-> IF k \in H THEN [view[k] EXCEPT ![s] = rank[s]] ELSE view[k]]
             Dr == [k \in Nodes |-> IF k \in H THEN [dropped[k] EXCEPT ![s] = FALSE] ELSE dropped[k]]
             Fr == [k \in Nodes |-> IF k \in H /\ poisoned[k] THEN [fresh[k] EXCEPT ![s] = TRUE]
                                    ELSE fresh[k]]
             S  == FrameRouting(s, H, Keep(s), V, Dr, Fr)
             \* passive acknowledgements of the copies waiting for s
             waits(k) == cp[m][k].st = "p" /\ cp[m][k].acc = "pas" /\ cp[m][k].hop = s
             C1 == [k \in Nodes |-> IF k # s /\ waits(k)
                                    THEN (IF k \in H /\ k # d THEN NoCopy
                                          ELSE [cp[m][k] EXCEPT !.acc = "no"])
                                    ELSE cp[m][k]]
             incons == got /\ d # Root /\ rank[d] # INF /\ rank[s] <= rank[d]
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
                              ELSE IF Acks = "frame" THEN "exp" ELSE "no"]
             released == Acks = "atomic" /\ got /\ ~passive /\ ~ackLost
         IN  /\ ackLost => (got /\ ~passive)
             /\ LossOK(s, L, extra)
             /\ Commit(S, V, Dr, Fr)
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
             /\ UNCHANGED <<alive, sub, err, errFrom>>

(***************************************************************************)
(* Explicit acknowledgement of k's waiting copy by its hop h: "exp" (the  *)
(* duplicate, held or delivered case), or owed by a relay that has not     *)
(* forwarded within half a timeout (Reliable.owe, reliable.py:603-613,     *)
(* 643-647: queue, duty cycle, the copy held after a detach). It may be   *)
(* lost (then k times out). Acks "atomic": owed acknowledgements only, and *)
(* they carry no rank. Acks "frame": the acknowledgement is a frame of h   *)
(* heard by k: it updates k's entry of h (rank 255 if h is detached) and  *)
(* k selects again (other neighbours overhearing it are not modelled).    *)
(***************************************************************************)
AckFrame(k, m) ==
    /\ alive[k] /\ cp[m][k].st = "p"
    /\ LET c == cp[m][k]
           h == c.hop
       IN  /\ alive[h]
           /\ \/ c.acc = "exp"
              \/ c.acc = "pas" /\ cp[m][h].st \in {"q", "h"}
           /\ \E lostIt \in BOOLEAN :
                IF lostIt
                THEN /\ IF Acks = "atomic"
                        THEN lost < MaxLoss /\ lost' = lost + 1 /\ UNCHANGED consec
                        ELSE LossOK(h, {k}, 0) /\ FrameCounters(h, {k}, {}, 0)
                     /\ cp' = [cp EXCEPT ![m][k] = [c EXCEPT !.acc = "no"]]
                     /\ UNCHANGED <<routing, loops, rl>>
                ELSE IF Acks = "atomic"
                THEN /\ cp' = [cp EXCEPT ![m][k] = NoCopy]
                     /\ UNCHANGED <<routing, lost, consec, loops, rl>>
                ELSE LET V  == [view EXCEPT ![k][h] = rank[h]]
                         Dr == [dropped EXCEPT ![k][h] = FALSE]
                         Fr == IF poisoned[k] THEN [fresh EXCEPT ![k][h] = TRUE] ELSE fresh
                         S  == [x \in Nodes |-> IF x = k THEN Sel(k, V, Dr, Fr) ELSE Keep(x)]
                     IN  /\ Commit(S, V, Dr, Fr)
                         /\ FrameCounters(h, {}, {k}, 0)
                         /\ cp' = [mm \in Msgs |-> [cp[mm] EXCEPT ![k] =
                                      IF mm = m THEN NoCopy ELSE Reshape(cp[mm][k], S[k])]]
    /\ UNCHANGED <<alive, sub, giver, delivered, err, errFrom, rerr>>

(***************************************************************************)
(* Timeout (Reliable._timeout, reliable.py:409-430): fewer than 3          *)
(* attempts: retried towards the same hop (txqueue.py:476-491); else the   *)
(* hop is dropped (Parents.next_parent, parents.py:216-234), the node      *)
(* selects again, and the copy goes to the new preferred parent with fresh *)
(* attempts, or is held (HOLD) when there is none or it is the same.       *)
(***************************************************************************)
Timeout(s, m) ==
    /\ alive[s] /\ cp[m][s].st = "p" /\ cp[m][s].acc = "no"
    /\ LET c == cp[m][s]
           h == c.hop
       IN  IF c.att < ATTEMPTS
           THEN /\ cp' = [cp EXCEPT ![m][s] = [c EXCEPT !.st = "q"]]
                /\ UNCHANGED <<routing, loops, rl>>
           ELSE LET Dr == [dropped EXCEPT ![s][h] = TRUE]
                    S  == [x \in Nodes |-> IF x = s THEN Sel(s, view, Dr, fresh) ELSE Keep(x)]
                    p  == S[s].pref
                IN  /\ Commit(S, view, Dr, fresh)
                    /\ cp' = [mm \in Msgs |-> [cp[mm] EXCEPT ![s] =
                                IF mm = m THEN (IF p = NONE \/ p = h THEN Held ELSE Queued(p))
                                ELSE Reshape(cp[mm][s], S[s])]]
    /\ UNCHANGED <<alive, lost, consec, sub, giver, delivered, err, errFrom, rerr>>

\* A held copy resumes once there is a route (Reliable._resume_held, reliable.py:467-490).
Resume(s, m) ==
    /\ alive[s] /\ cp[m][s].st = "h" /\ pref[s] # NONE
    /\ cp' = [cp EXCEPT ![m][s] = Queued(pref[s])]
    /\ UNCHANGED <<routing, lost, consec, sub, giver, delivered, err, errFrom, loops, rl, rerr>>

\* Holding expired without route (reliable.py:480-483): failure (reliable.py:494-515):
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
    /\ UNCHANGED <<routing, lost, consec, sub, giver, delivered, loops, rl, rerr>>

\* The error reaches the origin (abstraction: both ends connected to the root).
ErrorArrives(m) ==
    /\ err[m] = "sent" /\ Connected(errFrom[m]) /\ Connected(Orig(m))
    /\ err' = [err EXCEPT ![m] = "recv"]
    /\ UNCHANGED <<routing, lost, consec, sub, cp, giver, delivered, errFrom, loops, rl, rerr>>

(***************************************************************************)
(* Specification. Initial state: the formed DODAG (shortest paths, lower   *)
(* address on ties), every view exact.                                     *)
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
    /\ lost = 0 /\ consec = [s \in Nodes |-> [k \in Nodes |-> 0]]
    /\ sub = [m \in Msgs |-> FALSE] /\ cp = [m \in Msgs |-> [n \in Nodes |-> NoCopy]]
    /\ giver = [m \in Msgs |-> [n \in Nodes |-> NONE]]
    /\ delivered = [m \in Msgs |-> FALSE] /\ err = [m \in Msgs |-> "none"]
    /\ errFrom = [m \in Msgs |-> NONE]
    /\ loops = 0 /\ rl = FALSE /\ rerr = FALSE

Protocol ==
    \/ \E s \in Nodes : Announce(s) \/ HoldEnd(s)
    \/ \E k, c \in Nodes : Expire(k, c)
    \/ \E s \in Nodes, m \in Msgs :
          SendData(s, m) \/ AckFrame(s, m) \/ Timeout(s, m) \/ Resume(s, m) \/ HoldExpire(s, m)
    \/ \E m \in Msgs : ErrorArrives(m)

Next == Protocol \/ Die \/ \E m \in Msgs : Submit(m)

\* The class 1 actions of one copy are enabled in disjoint states of that copy
\* ("q", "p"/"pas", "p"/"no", "h" with or without route): weak fairness of their
\* disjunction is weak fairness of each.
CopyStep(s, m) == SendData(s, m) \/ AckFrame(s, m) \/ Timeout(s, m) \/ Resume(s, m) \/ HoldExpire(s, m)

Fairness ==
    /\ \A s \in Nodes : WF_vars(Announce(s)) /\ WF_vars(HoldEnd(s))
    /\ \A k \in Nodes : \A c \in Nbr(k) : WF_vars(Expire(k, c))
    /\ \A s \in Nodes \ {Root}, m \in Msgs : WF_vars(CopyStep(s, m))
    /\ \A m \in Msgs : WF_vars(ErrorArrives(m))

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

\* MP11.
NoCycle == ~Cycle(LiveP(pref))
NoRejoinLoop == ~rl
LoopsEnd == <>[]NoCycle
\* Ranks consistent along preferred-parent chains: every attached node's parent is
\* alive, of rank strictly lower, and seen with its actual rank.
Consistent ==
    \A n \in Nodes : (alive[n] /\ n # Root /\ pref[n] # NONE) =>
        (alive[pref[n]] /\ rank[pref[n]] < rank[n] /\ view[n][pref[n]] = rank[pref[n]])
RanksSettle == <>[]Consistent

\* MP10: once losses stop, every node connected to the root ends attached, and
\* (MP15) every node cut off ends detached.
AllAttached == \A n \in Nodes : (alive[n] /\ GraphConnected(n)) => Connected(n)
CutOffDetached == \A n \in Nodes : (alive[n] /\ ~GraphConnected(n)) => rank[n] = INF
MP10Live == <>[]AllAttached
CutOffLive == <>[]CutOffDetached

\* All the liveness properties at once (one tableau: faster than five).
Live == MP5Live /\ LoopsEnd /\ RanksSettle /\ MP10Live /\ CutOffLive

\* Non-vacuity witnesses (each must be violated).
NeverDelivered == \A m \in Msgs : ~delivered[m]
NeverError == \A m \in Msgs : err[m] # "recv"
NoRankError == ~rerr
AtMostOneLoop == loops <= 1
=============================================================================
