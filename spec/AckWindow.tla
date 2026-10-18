----------------------------- MODULE AckWindow -----------------------------
(***************************************************************************)
(* Acknowledgement window, ETX and advertised rank of a relay R whose      *)
(* preferred parent P (rank ParentRank, the root by default) is its only  *)
(* parent (MESH_DESIGN §9 link estimation, rank and parent set; §12 class *)
(* 1; §18 design issue 6).                                                 *)
(*                                                                         *)
(* No time: a behaviour is a sequence of acknowledgement samples. The code *)
(* records one sample per class 1/2 message (reliable.py:405 on an         *)
(* acknowledgement, reliable.py:413 on a timeout): one lost               *)
(* acknowledgement then gives M failure samples at once, the M messages of *)
(* the frame it acknowledged (one port 3 entry acknowledges every class 1  *)
(* message of a frame, §12), before any selection. The provisional choice *)
(* is one sample per frame (and per presence passively acknowledged):     *)
(* M = 1 whatever the aggregation. The retries are then acknowledged.     *)
(* After every sample R selects again (Parents._select): P stays a         *)
(* candidate while its link is eligible (ETX <= 4; margin assumed good),  *)
(* and the advertised rank follows the path cost with the hysteresis of 4 *)
(* (Estimation!NewRank). The reception window from P is fixed (DinRecv of *)
(* DinSpan, 16 of 16 by default: a perfect link).                         *)
(*                                                                         *)
(* Losses = "single": a loss happens only when the window holds no        *)
(* failure, i.e. at most one lost acknowledgement within any window (P4). *)
(* Losses = "any": any sequence of samples (detection properties).        *)
(* Losses = "none": every message acknowledged.                           *)
(* Forget (fix 2; time is not modelled, so it may happen between any two  *)
(* samples): "none" (the code); "window": the whole window is emptied (the *)
(* variant "no sample for Th"); "failures": a window holding a failure     *)
(* loses its failures, its successes are kept (the variant "failures age  *)
(* out Th after the newest one").                                         *)
(* NMin: samples before p_ack replaces the d_in estimate (1 = the code,   *)
(* neighbours.py:163-164; fix 6 raises it). Latch: once the window has    *)
(* held NMin samples, p_ack is used whenever it is not empty (mitigation  *)
(* of anomaly 6: no return to 1/d_in² after an ageing).                   *)
(*                                                                         *)
(* Properties:                                                             *)
(* - P4: R's advertised rank stays below Rank0 + 8, Rank0 being its rank  *)
(*   on a perfect link: a child at the next rank (Rank0 + 8, one perfect  *)
(*   hop below) keeps R as a candidate (rank strictly lower, parents.py:  *)
(*   151). P4Strong: the advertised rank does not change at all (no       *)
(*   ripple down the DODAG, MP12).                                        *)
(* - P4Step (temporal): no single step raises the advertised rank by a    *)
(*   hop (8) or more. With a perfect d_in it follows from P4; with an      *)
(*   imperfect d_in it shows the cost of falling back on 1/d_in² (fix 2)  *)
(*   when d_in and p_ack disagree.                                        *)
(* - DetectAny: KAny consecutive failures make the link ineligible        *)
(*   whatever the history; DetectFresh: KFresh failures on a fresh window *)
(*   do. (Independently of the ETX, 3 failed attempts of one message drop *)
(*   the parent, reliable.py:416-421, parents.py:216-234: unchanged.)     *)
(***************************************************************************)
EXTENDS Estimation

CONSTANTS
    NMin,        \* acknowledgement samples before p_ack is used
    M,           \* failure samples per lost acknowledgement
    Losses,      \* "single", "any" or "none"
    Forget,      \* fix 2: "none", "window" or "failures"
    Latch,       \* anomaly 6 mitigation: NMin applies until reached once
    ParentRank,  \* advertised rank of P
    DinRecv,     \* reception window from P: frames received ...
    DinSpan,     \* ... of DinSpan counter values
    KAny,        \* DetectAny bound
    KFresh,      \* DetectFresh bound
    InitAcks     \* successes already in the window initially (0: fresh link)

ASSUME NMin >= 1 /\ M \in 1..WINDOW /\ Losses \in {"single", "any", "none"}
ASSUME Forget \in {"none", "window", "failures"} /\ Latch \in BOOLEAN
ASSUME DinRecv \in 1..DinSpan /\ DinSpan \in 1..WINDOW /\ InitAcks \in 0..WINDOW

VARIABLES
    ack,     \* acknowledgement window (oldest first)
    rank,    \* R's advertised rank
    run,     \* failures since the last success or forget, saturated at 17
    latched  \* the window has held NMin samples (used only with Latch)

vars == <<ack, rank, run, latched>>

Rx == Rep(FALSE, DinSpan - DinRecv) \o Rep(TRUE, DinRecv)

EtxIn(a, l) == EtxL(a, Rx, NMin, Latch /\ l)
CostIn(a, l) == ParentRank + LinkCost(EtxIn(a, l))

\* Selection after a step: detach (255) when P is no longer eligible.
SelectIn(a, l, r) ==
    IF ~EtxOk(EtxIn(a, l)) THEN RANK_INFINITE ELSE NewRank(r, CostIn(a, l))

Latched(a) == latched \/ Len(a) >= NMin

\* ETX of the current window.
EtxOf(a) == EtxIn(a, latched)

Rank0 == CostIn(<<>>, FALSE)

TypeOK ==
    /\ ack \in Seq(BOOLEAN) /\ Len(ack) <= WINDOW
    /\ rank \in 0..RANK_INFINITE /\ run \in 0..(WINDOW + 1) /\ latched \in BOOLEAN

Init ==
    /\ ack = Rep(TRUE, InitAcks) /\ rank = Rank0 /\ run = 0
    /\ latched = (InitAcks >= NMin)

Sample(b, n) ==
    /\ ack' = Push(ack, b, n)
    /\ latched' = Latched(ack')
    /\ rank' = SelectIn(ack', latched', rank)
    /\ run' = IF b THEN 0 ELSE Min(run + n, WINDOW + 1)

Acked == Sample(TRUE, 1)

LostAck ==
    /\ \/ Losses = "any"
       \/ Losses = "single" /\ \A i \in 1..Len(ack) : ack[i]
    /\ Sample(FALSE, M)

ForgetWindow ==
    /\ \/ Forget = "window" /\ ack # <<>> /\ ack' = <<>>
       \/ Forget = "failures" /\ \E i \in 1..Len(ack) : ~ack[i]
          /\ ack' = Rep(TRUE, Count(ack))
    /\ rank' = SelectIn(ack', latched, rank) /\ run' = 0
    /\ UNCHANGED latched

Next == Acked \/ LostAck \/ ForgetWindow

Spec == Init /\ [][Next]_vars

P4 == rank < Rank0 + MIN_HOP
P4Strong == rank = Rank0
P4Step == [][rank' < rank + MIN_HOP]_vars
DetectAny == run >= KAny => ~EtxOk(EtxOf(ack))
DetectFresh == (run = Len(ack) /\ run >= KFresh) => ~EtxOk(EtxOf(ack))
=============================================================================
