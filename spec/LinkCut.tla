------------------------------ MODULE LinkCut ------------------------------
(***************************************************************************)
(* Re-eligibility of a link after a cut (MESH_DESIGN §9 link estimation;  *)
(* design issue 2 of spec/README.md; MP10, MP15). Discrete time in ticks.  *)
(*                                                                         *)
(* A node R and its only possible parent X (worst case: no other parent to *)
(* fall back on). X sends a frame every F ticks; R records it in its       *)
(* neighbour table when it hears it (NeighbourTable.heard: reception       *)
(* window from the counter gap, time last heard, neighbours.py:292-318).  *)
(* R sends class 1 frames of M messages through X whenever X is usable:   *)
(* entry present, heard within Th (parents.py:153), not dropped (parents. *)
(* py:157), eligible (ETX <= 4, margin assumed good). Each message gives  *)
(* one acknowledgement sample (reliable.py:405, 413); after 3 failed       *)
(* attempts X is dropped until a new frame of it is heard (reliable.py:    *)
(* 416-421, parents.py:216-234, 157). An entry not heard for 4 periods is  *)
(* removed (neighbours.py:328-348) and starts afresh when heard again.     *)
(* Attempts are instantaneous at the tick scale (timeouts are seconds);   *)
(* whether R has class 1 traffic at a given instant is left to the        *)
(* environment. One sample per frame (one acknowledgement entry covers    *)
(* every class 1/2 message of a frame): M = 1 in the configurations.      *)
(*                                                                         *)
(* Presence (MESH_DESIGN §9 passive acknowledgement, not in the code yet): *)
(* R sends its presence to X every T ticks while X is its parent (usable) *)
(* and overhears X forward it: one sample, successful outside the cut.    *)
(* A lost presence is not retried (class 0): it counts no attempt. X's    *)
(* forwarding frame itself is not added to X's frames (conservative: it    *)
(* would only refresh R's view of X).                                     *)
(*                                                                         *)
(* One cut per behaviour, of any length (the state space stays finite:     *)
(* every counter saturates):                                              *)
(* - "both": nothing passes either way (X not heard, nothing acknowledged);*)
(* - "up": R -> X broken, X -> R fine (X heard at 100 %, nothing           *)
(*   acknowledged): an asymmetric cut.                                    *)
(* Outside the cut every frame is heard and every message acknowledged.   *)
(*                                                                         *)
(* Fix (2), Aging (delay AckForget ticks):                                 *)
(* - "none": the code, the window never ages;                             *)
(* - "sample": the whole window is forgotten once no sample was recorded  *)
(*   for AckForget (the candidate fix first proposed);                    *)
(* - "failure": the failures are removed from the window (successes kept)  *)
(*   once the newest failure is AckForget old ("failures age out");       *)

(* With fewer than NMin samples left the ETX falls back on 1/d_in².       *)
(* Fix (6), NMin > 1: d_in estimate until NMin acknowledgement samples.   *)
(* Latch (anomaly 6 mitigation): once the entry has held NMin samples,    *)
(* p_ack is used whenever the window is not empty (reset with the entry). *)
(*                                                                         *)
(* P3 (LinkBack): from Bound ticks after the end of the cut on, X is a    *)
(* usable parent (eligible, heard, not dropped), for ever.                *)
(***************************************************************************)
EXTENDS Estimation

CONSTANTS
    T,          \* presence period, ticks
    F,          \* ticks between two frames of X
    Th,         \* silence threshold, ticks
    Aging,      \* fix 2: "none", "sample" or "failure" (see above)
    AckForget,  \* fix 2: aging delay, ticks
    NMin,       \* fix 6: samples before p_ack is used (1: the code)
    Latch,      \* anomaly 6 mitigation (see above)
    M,          \* samples per frame of R
    Presence,   \* BOOLEAN: R's presence feeds the window (passive acknowledgement)
    Bound       \* P3 bound, ticks after the cut

Expiry == 4 * T                       \* EXPIRY_PERIODS, neighbours.py:51

ASSUME F \in 1..T /\ Th > F /\ NMin >= 1 /\ M \in 1..WINDOW /\ Bound >= 1
ASSUME Aging \in {"none", "sample", "failure"} /\ AckForget >= 1
ASSUME Presence \in BOOLEAN /\ Latch \in BOOLEAN

VARIABLES
    phase,    \* "before", "cut", "after", "done" (settled after the cut)
    kind,     \* "none", "both", "up"
    clk,      \* ticks since the end of the cut (saturated at Bound)
    xph,      \* ticks until X's next frame
    exists,   \* X in R's neighbour table
    age,      \* ticks since R last heard X (saturated at Expiry)
    missed,   \* frames of X not heard since the last one heard (saturated at 16)
    rx,       \* reception window of X
    ack,      \* acknowledgement window of X
    idle,     \* aging timer: ticks since the last sample ("sample") or the
              \* newest failure ("failure"), saturated at AckForget
    tries,    \* failed attempts of the current message
    dropped,  \* X dropped after 3 failed attempts, until heard again
    pph,      \* ticks until R's next presence (0 when Presence is FALSE)
    latched   \* the entry has held NMin samples (FALSE unless Latch)

vars == <<phase, kind, clk, xph, exists, age, missed, rx, ack, idle, tries, dropped,
          pph, latched>>

TypeOK ==
    /\ phase \in {"before", "cut", "after", "done"} /\ kind \in {"none", "both", "up"}
    /\ clk \in 0..Bound /\ xph \in 0..F /\ exists \in BOOLEAN /\ age \in 0..Expiry
    /\ missed \in 0..WINDOW /\ Len(rx) <= WINDOW /\ Len(ack) <= WINDOW
    /\ idle \in 0..AckForget /\ tries \in 0..(ATTEMPTS - 1) /\ dropped \in BOOLEAN
    /\ pph \in 0..T /\ latched \in BOOLEAN

EtxX == EtxL(ack, rx, NMin, latched)

Usable == exists /\ age < Th /\ ~dropped /\ EtxOk(EtxX)

HasFailure(a) == \E i \in 1..Len(a) : ~a[i]

\* The aging timer runs on a window that can age: non-empty ("sample"),
\* holding a failure ("failure"). Otherwise idle is kept at 0 (unused).
Timed(a) ==
    CASE Aging = "sample"  -> a # <<>>
      [] Aging = "none"    -> FALSE
      [] OTHER             -> HasFailure(a)

\* The window once aged.
Aged(a) ==
    IF Aging = "sample" THEN <<>> ELSE Rep(TRUE, Count(a))

(***************************************************************************)
(* Settled: after the cut, X usable with no failure left in the window    *)
(* and a d_in estimate <= 4 as well. Every later frame is heard and every  *)
(* later message acknowledged: the reception window only improves (its    *)
(* 1/d_in² never increases), p_ack stays 1, an aged window falls back on  *)
(* d_in, X is never dropped nor expired (F < Th): X stays usable for ever. *)
(* Such a state moves at once to one absorbing state ("done"), which      *)
(* prunes the rest of the "after" phase without changing the verdict on   *)
(* LinkBack.                                                               *)
(***************************************************************************)
Settled == phase = "after" /\ Usable /\ ~HasFailure(ack) /\ EtxOk(EtxDin(rx))
Live == phase # "done" /\ ~Settled

\* R has never heard X; X's first frame is due now.
Init ==
    /\ phase = "before" /\ kind = "none" /\ clk = 0 /\ xph = 0
    /\ exists = FALSE /\ age = 0 /\ missed = 0 /\ rx = <<>> /\ ack = <<>>
    /\ idle = 0 /\ tries = 0 /\ dropped = FALSE
    /\ pph \in IF Presence THEN 0..(T - 1) ELSE {0}   \* any phase
    /\ latched = FALSE

XFrame ==
    /\ Live /\ xph = 0 /\ xph' = F
    /\ IF phase = "cut" /\ kind = "both"
       THEN /\ missed' = Min(missed + 1, WINDOW)            \* not heard
            /\ UNCHANGED <<exists, age, rx, ack, dropped>>
       ELSE /\ age' = 0 /\ missed' = 0 /\ dropped' = FALSE  \* new counter
            /\ IF exists
               THEN /\ rx' = RxFrame(rx, missed + 1)
                    /\ UNCHANGED <<exists, ack>>
               ELSE /\ exists' = TRUE /\ rx' = <<TRUE>>      \* Neighbour.reset
                    /\ ack' = <<>>
    /\ idle' = IF Timed(ack') THEN idle ELSE 0
    /\ latched' = (latched /\ exists)                     \* reset with the entry
    /\ UNCHANGED <<phase, kind, clk, tries, pph>>

\* One sample: outcome b; the ageing timer restarts on a failure, and on
\* any sample in the "sample" variant.
Sample(b) ==
    /\ ack' = Push(ack, b, M)
    /\ idle' = IF ~b \/ Aging = "sample" THEN 0
              ELSE IF Timed(ack') THEN idle ELSE 0
    /\ latched' = (Latch /\ (latched \/ Len(ack') >= NMin))

Send ==
    /\ Live /\ Usable
    /\ IF phase = "cut"
       THEN /\ Sample(FALSE)
            /\ IF tries + 1 >= ATTEMPTS
               THEN dropped' = TRUE /\ tries' = 0
               ELSE tries' = tries + 1 /\ UNCHANGED dropped
       ELSE /\ Sample(TRUE) /\ tries' = 0
            /\ UNCHANGED dropped
    /\ UNCHANGED <<phase, kind, clk, xph, exists, age, missed, rx, pph>>

\* R's presence, every T ticks; a sample only while X is its parent.
PresenceReport ==
    /\ Presence /\ Live /\ pph = 0 /\ pph' = T
    /\ IF Usable THEN Sample(phase # "cut") ELSE UNCHANGED <<ack, idle, latched>>
    /\ UNCHANGED <<phase, kind, clk, xph, exists, age, missed, rx, tries, dropped>>

CutStart ==
    /\ phase = "before" /\ phase' = "cut" /\ kind' \in {"both", "up"} /\ clk' = 0
    /\ UNCHANGED <<xph, exists, age, missed, rx, ack, idle, tries, dropped, pph, latched>>

CutEnd ==                                    \* the kind no longer matters
    /\ phase = "cut" /\ phase' = "after" /\ clk' = 0 /\ kind' = "none"
    /\ UNCHANGED <<xph, exists, age, missed, rx, ack, idle, tries, dropped, pph, latched>>

Tick ==
    /\ Live /\ xph > 0 /\ (pph > 0 \/ ~Presence)
    /\ xph' = xph - 1
    /\ pph' = IF Presence THEN pph - 1 ELSE 0
    /\ age' = Min(age + 1, Expiry)
    /\ exists' = (exists /\ age + 1 < Expiry)             \* expiry, entry removed
    /\ rx' = IF exists' THEN rx ELSE <<>>
    /\ ack' = IF ~exists' THEN <<>>
              ELSE IF Timed(ack) /\ idle + 1 >= AckForget THEN Aged(ack)
              ELSE ack
    /\ idle' = IF Timed(ack') THEN Min(idle + 1, AckForget) ELSE 0
    /\ clk' = IF phase = "after" THEN Min(clk + 1, Bound) ELSE 0
    /\ latched' = (latched /\ exists')                    \* expiry clears it
    /\ UNCHANGED <<phase, kind, missed, tries, dropped>>

Settle ==
    /\ Settled
    /\ phase' = "done" /\ kind' = "none" /\ clk' = 0 /\ xph' = 0
    /\ exists' = FALSE /\ age' = 0 /\ missed' = 0 /\ rx' = <<>> /\ ack' = <<>>
    /\ idle' = 0 /\ tries' = 0 /\ dropped' = FALSE /\ pph' = 0 /\ latched' = FALSE

Done == phase = "done" /\ UNCHANGED vars           \* no deadlock

Next == XFrame \/ Send \/ PresenceReport \/ CutStart \/ CutEnd \/ Tick \/ Settle \/ Done

Spec == Init /\ [][Next]_vars

\* P3: from Bound ticks after the cut on, X is usable.
LinkBack == (phase = "after" /\ clk >= Bound) => Usable
=============================================================================
