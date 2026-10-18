------------------------------ MODULE Silence ------------------------------
(***************************************************************************)
(* Silence detection of one periodic sender by one listener, in discrete  *)
(* time (ticks). MESH_DESIGN §7 (periodic timers jittered by ±10 %), §9    *)
(* (candidate iff heard within 2 presence periods, root beacon at Imax),  *)
(* §16 (root beacon, K_MISS).                                               *)
(*                                                                         *)
(* Sender, one of:                                                         *)
(* - Timer = "trickle": the root beacon as coded, a Trickle timer at Imax  *)
(*   (= the presence period T): contiguous intervals of length T, the      *)
(*   beacon at a random offset in [T/2, T) (trickle.py:171-176, 189-193).  *)
(*   With Resets, the timer may be reset at any time (a solicitation, an   *)
(*   older version: dodag.py:376, 407): intervals restart at Imin and      *)
(*   double up to T (trickle.py:104-119, 160-161); below Imax the          *)
(*   advertisement may be suppressed, when k = 2 frames of the version     *)
(*   were heard in the interval (trickle.py:184-196), a choice left to the *)
(*   environment here.                                                     *)
(* - Timer = "periodic": a timer re-armed at every firing with T ± 10 %    *)
(*   (jittered, reports.py:99-102, 474): the presence report as coded, and *)
(*   the root beacon of fix (1a).                                          *)
(* A frame goes on air up to D ticks after its timer fires (queue, carrier *)
(* sense, duty cycle); the timer is re-armed from the firing, not from the *)
(* transmission (reports.py:474).                                          *)
(*                                                                         *)
(* Listener: drops the sender when its age reaches Th (strict rule "heard  *)
(* within Th": candidate iff age < Th, parents.py:153; the drop happens at *)
(* heard + Th, dodag.py:512-521). The environment loses at most MaxRun     *)
(* consecutive frames.                                                     *)
(*                                                                         *)
(* Properties (K: number of consecutive losses that must be detected):     *)
(* - P1 (MaxRun = K - 1): K - 1 consecutive lost frames never make the     *)
(*   listener drop the sender. K = 2 for parents (§9), 3 for the root's    *)
(*   silent-node alarm (K_MISS, §13, §16).                                 *)
(* - P2 (MaxRun = K): a frame heard after K consecutive losses finds the   *)
(*   sender already dropped. The drop happens Th after the last frame      *)
(*   heard, by construction: Th is the detection bound.                    *)
(* - P0 (MaxRun = 0): without loss the sender is never dropped (P1 checks  *)
(*   it when MaxRun = 0).                                                  *)
(***************************************************************************)
EXTENDS Integers

CONSTANTS
    T,        \* presence period (= Trickle Imax), ticks; a multiple of 10
    Timer,    \* "trickle" or "periodic" (see above)
    Th,       \* silence threshold, ticks: candidate iff age < Th
    K,        \* consecutive losses to detect (K - 1 tolerated)
    MaxRun,   \* the environment loses at most MaxRun consecutive frames
    D,        \* maximum on-air delay after the timer fires, ticks
    Resets,   \* TRUE: Trickle resets of the sender (Timer = "trickle")
    Imin      \* Trickle Imin, ticks

J == T \div 10                          \* ±10 % (JITTER_PERCENT, reports.py:94)

Min(a, b) == IF a < b THEN a ELSE b
Min2(x)   == IF x < T THEN x ELSE T              \* interval doubling capped at Imax

ASSUME T >= 10 /\ T % 10 = 0 /\ Timer \in {"trickle", "periodic"}
ASSUME D >= 0 /\ D < T - J /\ Th > 0 /\ K >= 1 /\ MaxRun \in 0..K
ASSUME Imin >= 2 /\ Imin < T

VARIABLES
    age,          \* ticks since the listener last heard the sender (Th = dropped)
    run,          \* frames lost since the last one heard
    lastRun,      \* at the last reception: frames lost just before it
    lastDropped,  \* at the last reception: sender dropped before it
    tx,           \* ticks until the pending frame goes on air, -1 if none
    nxt,          \* periodic: ticks until the timer fires
    ilen,         \* trickle: interval length
    pos,          \* trickle: ticks elapsed in the interval
    at,           \* trickle: offset of the decision in the interval
    decided,      \* trickle: decision of this interval taken
    first         \* trickle: first interval after a reset (not "at Imax")

vars == <<age, run, lastRun, lastDropped, tx, nxt, ilen, pos, at, decided, first>>

TypeOK ==
    /\ age \in 0..Th /\ run \in 0..MaxRun /\ lastRun \in 0..MaxRun
    /\ lastDropped \in BOOLEAN /\ tx \in -1..D /\ nxt \in 0..(T + J)
    /\ ilen \in Imin..T /\ pos \in 0..T /\ at \in 0..(T - 1)
    /\ decided \in BOOLEAN /\ first \in BOOLEAN

\* Initial state: the listener has just heard a frame; the sender is in
\* steady state (Trickle at Imax, or a timer armed at the previous firing,
\* the frame having left d <= D ticks after it).
Init ==
    /\ age = 0 /\ run = 0 /\ lastRun = 0 /\ lastDropped = FALSE /\ tx = -1
    /\ IF Timer = "periodic"
       THEN /\ nxt \in (T - J - D)..(T + J)
            /\ ilen = T /\ pos = 0 /\ at = 0 /\ decided = TRUE /\ first = FALSE
       ELSE /\ nxt = 0 /\ ilen = T /\ first = FALSE /\ decided = TRUE
            /\ at \in (T \div 2)..(T - 1) /\ pos = at

\* --- Sender, periodic timer -------------------------------------------------
Fire ==
    /\ Timer = "periodic" /\ nxt = 0 /\ tx = -1
    /\ \E j \in -J..J, d \in 0..D : nxt' = T + j /\ tx' = d
    /\ UNCHANGED <<age, run, lastRun, lastDropped, ilen, pos, at, decided, first>>

\* --- Sender, Trickle timer (trickle.py) --------------------------------------
AtImax == ilen = T /\ ~first                       \* trickle.py:98-100

Decide ==
    /\ Timer = "trickle" /\ pos = at /\ ~decided /\ tx = -1
    /\ decided' = TRUE
    /\ IF AtImax
       THEN tx' \in 0..D                           \* root beacon, never suppressed
       ELSE tx' \in {-1} \cup (0..D)               \* sent, or suppressed (k = 2)
    /\ UNCHANGED <<age, run, lastRun, lastDropped, nxt, ilen, pos, at, first>>

IntervalEnd ==                                     \* trickle.py:158-162
    /\ Timer = "trickle" /\ pos = ilen
    /\ ilen' = Min2(2 * ilen)
    /\ pos' = 0 /\ decided' = FALSE /\ first' = FALSE
    /\ at' \in (ilen' \div 2)..(ilen' - 1)          \* trickle.py:174-175
    /\ UNCHANGED <<age, run, lastRun, lastDropped, tx, nxt>>

Reset ==                                           \* trickle.py:104-119
    /\ Timer = "trickle" /\ Resets
    /\ ~(ilen = Imin /\ first /\ ~decided)         \* pending Imin decision kept
    /\ ilen' = Imin /\ pos' = 0 /\ first' = TRUE /\ decided' = FALSE
    /\ at' \in (Imin \div 2)..(Imin - 1)
    /\ UNCHANGED <<age, run, lastRun, lastDropped, tx, nxt>>

\* --- Channel and listener -----------------------------------------------------
Air ==
    /\ tx = 0 /\ tx' = -1
    /\ \/ /\ run < MaxRun                          \* lost
          /\ run' = run + 1
          /\ UNCHANGED <<age, lastRun, lastDropped>>
       \/ /\ age' = 0 /\ run' = 0                  \* heard
          /\ lastRun' = run /\ lastDropped' = (age >= Th)
    /\ UNCHANGED <<nxt, ilen, pos, at, decided, first>>

\* One tick, once every event due at the current tick has been handled.
Tick ==
    /\ tx # 0
    /\ IF Timer = "periodic" THEN nxt > 0
       ELSE pos < ilen /\ ~(pos = at /\ ~decided)
    /\ age' = Min(age + 1, Th)                     \* reaching Th: dropped
    /\ tx' = IF tx > 0 THEN tx - 1 ELSE tx
    /\ nxt' = IF Timer = "periodic" THEN nxt - 1 ELSE nxt
    /\ pos' = IF Timer = "trickle" THEN pos + 1 ELSE pos
    /\ UNCHANGED <<run, lastRun, lastDropped, ilen, at, decided, first>>

Next == Fire \/ Decide \/ IntervalEnd \/ Reset \/ Air \/ Tick

Spec == Init /\ [][Next]_vars

\* --- Properties ----------------------------------------------------------------
P1 == age < Th                                     \* with MaxRun = K - 1 (or 0: P0)
P2 == lastRun >= K => lastDropped                  \* with MaxRun = K

\* Non-vacuity: must be violated (a frame heard after K losses is reachable).
P2Witness == lastRun < K
=============================================================================
