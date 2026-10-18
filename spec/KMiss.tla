------------------------------- MODULE KMiss -------------------------------
(***************************************************************************)
(* Local repair against the root's silent-node alarm (MESH_DESIGN §9       *)
(* repair times, finding 2: the children of a failed relay re-attach and   *)
(* send their presence before the root declares them silent; K_MISS = 4,   *)
(* root threshold 4.5 periods, owner session 23; §13 root alarms; §16).    *)
(*                                                                         *)
(* Actors: a relay X, its child C (X is C's preferred parent), another     *)
(* parent Y of C's set (always alive, always a candidate), the root.       *)
(* - C sends its presence every T ± 10 % (reports.py:99-102, 474) to its   *)
(*   preferred parent, which forwards it at once to the root (forwarding   *)
(*   delays folded into Dchg below); C overhears X's forward.              *)
(* - X sends its own presence every T ± 10 %, heard by C.                  *)
(* - X fails at any time (it stops transmitting, once).                    *)
(* - C drops X when it has not heard it for Th (parents.py:153,           *)
(*   dodag.py:512-521), switches to Y at once (parent set, no exchange)    *)
(*   and sends its presence within Dchg ticks (CHANGE_SLOTS delay,         *)
(*   reports.py:376-383, plus forwarding to the root); its presence period *)
(*   restarts there (reports.py:474).                                       *)
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
    Dchg      \* presence after a parent change, reaching the root, ticks

J == T \div 10
Min(a, b) == IF a < b THEN a ELSE b

ASSUME T >= 10 /\ T % 10 = 0 /\ Th > T + J /\ RootTh > 0 /\ Dchg >= 0

VARIABLES
    cNext,    \* ticks until C's presence timer fires
    xNext,    \* ticks until X's presence timer fires
    xAlive,
    cAge,     \* ticks since C last heard X (Th: dropped)
    rootAge,  \* ticks since the root last received C's presence (RootTh: alarm)
    via,      \* C's preferred parent: "X" or "Y"
    chg       \* ticks until C's presence after the change, -1 if none

vars == <<cNext, xNext, xAlive, cAge, rootAge, via, chg>>

TypeOK ==
    /\ cNext \in 0..(T + J) /\ xNext \in 0..(T + J) /\ xAlive \in BOOLEAN
    /\ cAge \in 0..Th /\ rootAge \in 0..RootTh /\ via \in {"X", "Y"}
    /\ chg \in -1..Dchg

\* C's presence has just been forwarded by X (root and C both heard it);
\* X's own timer is at any phase.
Init ==
    /\ cNext \in (T - J)..(T + J) /\ xNext \in 0..(T + J) /\ xAlive = TRUE
    /\ cAge = 0 /\ rootAge = 0 /\ via = "X" /\ chg = -1

CPresence ==
    /\ cNext = 0
    /\ \E j \in -J..J : cNext' = T + j
    /\ IF via = "Y" THEN rootAge' = 0 /\ UNCHANGED cAge
       ELSE IF xAlive THEN rootAge' = 0 /\ cAge' = 0   \* forwarded, overheard
       ELSE UNCHANGED <<rootAge, cAge>>                \* sent to a dead parent
    /\ UNCHANGED <<xNext, xAlive, via, chg>>

XPresence ==
    /\ xAlive /\ xNext = 0
    /\ \E j \in -J..J : xNext' = T + j
    /\ cAge' = 0
    /\ UNCHANGED <<cNext, xAlive, rootAge, via, chg>>

Fail ==
    /\ xAlive /\ xAlive' = FALSE
    /\ UNCHANGED <<cNext, xNext, cAge, rootAge, via, chg>>

Switch ==                                    \* X silent for Th: Y takes over
    /\ via = "X" /\ cAge >= Th
    /\ via' = "Y" /\ chg' \in 0..Dchg
    /\ UNCHANGED <<cNext, xNext, xAlive, cAge, rootAge>>

ChangePresence ==                            \* presence at once, period restarted
    /\ chg = 0 /\ chg' = -1 /\ rootAge' = 0
    /\ \E j \in -J..J : cNext' = T + j
    /\ UNCHANGED <<xNext, xAlive, cAge, via>>

Tick ==
    /\ cNext > 0 /\ (xNext > 0 \/ ~xAlive) /\ chg # 0
    /\ ~(via = "X" /\ cAge >= Th)
    /\ cNext' = cNext - 1
    /\ xNext' = IF xAlive THEN xNext - 1 ELSE xNext
    /\ cAge' = Min(cAge + 1, Th)
    /\ rootAge' = Min(rootAge + 1, RootTh)
    /\ chg' = IF chg > 0 THEN chg - 1 ELSE chg
    /\ UNCHANGED <<xAlive, via>>

Next == CPresence \/ XPresence \/ Fail \/ Switch \/ ChangePresence \/ Tick

Spec == Init /\ [][Next]_vars

NoFalseAlarm == rootAge < RootTh

\* Non-vacuity: must be violated (C does switch to Y after X fails).
SwitchWitness == via = "X"
=============================================================================
