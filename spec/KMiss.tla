------------------------------- MODULE KMiss -------------------------------
(***************************************************************************)
(* Local repair against the root's silent-node alarm (MESH_DESIGN §9       *)
(* repair times, finding 2: the children of a failed relay re-attach and   *)
(* send their presence before the root declares them silent; K_MISS = 4,   *)
(* the smallest count with a valid threshold, root threshold 4.5 periods;  *)
(* §13 root alarms; §16).                                                  *)
(*                                                                         *)
(* Actors: a relay X, its child C (X is C's preferred parent), another     *)
(* parent Y of C's set (always alive, always a candidate), the root.       *)
(* - C sends its presence every T ± 10 % (reports.py Reporter._tick) to    *)
(*   its preferred parent, which forwards it to the root; C overhears X's  *)
(*   forward. Bundle FALSE (the code before bundling): at once (delays     *)
(*   folded into Dchg below). Bundle TRUE (the code, MESH_DESIGN §7        *)
(*   bundling of reports): X defers it to its own next presence, which     *)
(*   carries it (Reporter.defer_until); a presence X still defers when it  *)
(*   fails is lost.                                                         *)
(* - X sends its own presence every T ± 10 %, heard by C.                  *)
(* - Other: X may also send other frames at any time (advertisements,     *)
(*   acknowledgements to its children), heard by C, carrying no bundle:    *)
(*   "none" (no such frame), "keep" (the code: X keeps deferring C's       *)
(*   presence), "flush" (a candidate not adopted: such a frame ends the    *)
(*   deferral, the presence leaving with X's next frame, folded into this  *)
(*   one).                                                                 *)
(* - X fails at any time (it stops transmitting, once).                    *)
(* - C drops X when it has not heard it for Th (parents.py:153,           *)
(*   dodag.py:512-521), switches to Y at once (parent set, no exchange)    *)
(*   and sends its presence within Dchg ticks (CHANGE_SLOTS delay,         *)
(*   Reporter.parent_changed, plus forwarding to the root: Y forwards at   *)
(*   once the presence of a child new to it); its presence period restarts *)
(*   there. Its later presences through Y reach the root at once (Y's      *)
(*   bundling, at most a period, keeps the gaps far below RootTh).         *)
(* - The root raises "C silent" when no presence of C arrived for RootTh. *)
(* No frame loss (losses are Silence's subject); X's failure is the only   *)
(* event. NoFalseAlarm: the root never declares C silent, C having        *)
(* re-attached through Y.                                                  *)
(***************************************************************************)
EXTENDS Integers

CONSTANTS
    T,        \* presence period, ticks (multiple of 10)
    Th,       \* parent silence threshold, ticks
    RootTh,   \* root silent-node threshold (K_MISS reading), ticks
    Dchg,     \* presence after a parent change, reaching the root, ticks
    Bundle,   \* BOOLEAN: X forwards C's presence with its own next presence
    Other     \* frames of X other than its presence: "none", "keep", "flush"

J == T \div 10
Min(a, b) == IF a < b THEN a ELSE b

ASSUME T >= 10 /\ T % 10 = 0 /\ Th > T + J /\ RootTh > 0 /\ Dchg >= 0
ASSUME Bundle \in BOOLEAN /\ Other \in {"none", "keep", "flush"}

VARIABLES
    cNext,    \* ticks until C's presence timer fires
    xNext,    \* ticks until X's presence timer fires
    xAlive,
    cAge,     \* ticks since C last heard X (Th: dropped)
    rootAge,  \* ticks since the root last received C's presence (RootTh: alarm)
    via,      \* C's preferred parent: "X" or "Y"
    chg,      \* ticks until C's presence after the change, -1 if none
    held      \* X defers a presence of C (Bundle)

vars == <<cNext, xNext, xAlive, cAge, rootAge, via, chg, held>>

TypeOK ==
    /\ cNext \in 0..(T + J) /\ xNext \in 0..(T + J) /\ xAlive \in BOOLEAN
    /\ cAge \in 0..Th /\ rootAge \in 0..RootTh /\ via \in {"X", "Y"}
    /\ chg \in -1..Dchg /\ held \in BOOLEAN

\* C's presence has just been forwarded by X (root and C both heard it);
\* X's own timer is at any phase. Bundle: X's presence has just carried
\* C's last one, C's timer is at any phase.
Init ==
    /\ IF Bundle
       THEN cNext \in 0..(T + J) /\ xNext \in (T - J)..(T + J)
       ELSE cNext \in (T - J)..(T + J) /\ xNext \in 0..(T + J)
    /\ xAlive = TRUE
    /\ cAge = 0 /\ rootAge = 0 /\ via = "X" /\ chg = -1 /\ held = FALSE

CPresence ==
    /\ cNext = 0
    /\ \E j \in -J..J : cNext' = T + j
    /\ IF via = "Y" THEN rootAge' = 0 /\ UNCHANGED <<cAge, held>>
       ELSE IF xAlive /\ Bundle THEN held' = TRUE /\ UNCHANGED <<rootAge, cAge>>
       ELSE IF xAlive THEN rootAge' = 0 /\ cAge' = 0 /\ UNCHANGED held  \* forwarded, overheard
       ELSE UNCHANGED <<rootAge, cAge, held>>          \* sent to a dead parent
    /\ UNCHANGED <<xNext, xAlive, via, chg>>

XPresence ==                                 \* with the presence X defers, if any
    /\ xAlive /\ xNext = 0
    /\ \E j \in -J..J : xNext' = T + j
    /\ cAge' = 0
    /\ rootAge' = IF held THEN 0 ELSE rootAge
    /\ held' = FALSE
    /\ UNCHANGED <<cNext, xAlive, via, chg>>

XOther ==
    /\ Other # "none" /\ xAlive /\ cAge > 0 /\ cAge' = 0
    /\ IF Other = "flush"
       THEN rootAge' = (IF held THEN 0 ELSE rootAge) /\ held' = FALSE
       ELSE UNCHANGED <<rootAge, held>>
    /\ UNCHANGED <<cNext, xNext, xAlive, via, chg>>

Fail ==                                      \* the presence X defers is lost
    /\ xAlive /\ xAlive' = FALSE /\ held' = FALSE
    /\ UNCHANGED <<cNext, xNext, cAge, rootAge, via, chg>>

Switch ==                                    \* X silent for Th: Y takes over
    /\ via = "X" /\ cAge >= Th
    /\ via' = "Y" /\ chg' \in 0..Dchg
    /\ UNCHANGED <<cNext, xNext, xAlive, cAge, rootAge, held>>

ChangePresence ==                            \* presence at once, period restarted
    /\ chg = 0 /\ chg' = -1 /\ rootAge' = 0
    /\ \E j \in -J..J : cNext' = T + j
    /\ UNCHANGED <<xNext, xAlive, cAge, via, held>>

Tick ==
    /\ cNext > 0 /\ (xNext > 0 \/ ~xAlive) /\ chg # 0
    /\ ~(via = "X" /\ cAge >= Th)
    /\ cNext' = cNext - 1
    /\ xNext' = IF xAlive THEN xNext - 1 ELSE xNext
    /\ cAge' = Min(cAge + 1, Th)
    /\ rootAge' = Min(rootAge + 1, RootTh)
    /\ chg' = IF chg > 0 THEN chg - 1 ELSE chg
    /\ UNCHANGED <<xAlive, via, held>>

Next == CPresence \/ XPresence \/ XOther \/ Fail \/ Switch \/ ChangePresence \/ Tick

Spec == Init /\ [][Next]_vars

NoFalseAlarm == rootAge < RootTh

\* Non-vacuity: must be violated (C does switch to Y after X fails).
SwitchWitness == via = "X"
=============================================================================
