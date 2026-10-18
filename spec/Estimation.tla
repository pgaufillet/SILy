----------------------------- MODULE Estimation -----------------------------
(***************************************************************************)
(* Link estimation and rank arithmetic of the mesh (MESH_DESIGN §9 link     *)
(* estimation, rank and parent set), shared by AckWindow and LinkCut.      *)
(* Pure operators in integer arithmetic, mirroring the code line by line   *)
(* (line numbers of src/mesh at the time of writing).                      *)
(*                                                                         *)
(* Windows are sequences of booleans, oldest first, newest last:           *)
(* - reception window (d_in): one element per frame counter value of the   *)
(*   neighbour, TRUE if that frame was received. Its length is rx_span and *)
(*   its TRUE count popcount16(rx_bits) (neighbours.py:121-139).           *)
(* - acknowledgement window (p_ack): one element per message sent through *)
(*   the neighbour, TRUE if acknowledged. Its length is ack_count and its  *)
(*   TRUE count popcount16(ack_bits) (neighbours.py:143-157).              *)
(* A 16-bit shift register masked to 16 bits holds exactly the newest 16   *)
(* elements, which is what Trim keeps: the encoding is exact, not an       *)
(* abstraction.                                                            *)
(***************************************************************************)
EXTENDS Integers, Sequences

WINDOW          == 16            \* neighbours.py:49
ETX_ONE         == 128           \* neighbours.py:52, fixed-point unit
ETX_MAX         == 4 * ETX_ONE   \* neighbours.py:53, eligible iff ETX <= 4
MIN_HOP         == 8             \* parents.py:58
RANK_HYSTERESIS == 4             \* parents.py:63
RANK_INFINITE   == 255           \* frame.py RANK_INFINITE
ATTEMPTS        == 3             \* reliable.py:72, class 1 attempts per next hop

Min(a, b) == IF a < b THEN a ELSE b

\* Number of TRUE elements (popcount16 of the register).
RECURSIVE Count(_)
Count(s) == IF s = <<>> THEN 0 ELSE (IF Head(s) THEN 1 ELSE 0) + Count(Tail(s))

\* n copies of b, and the newest WINDOW elements of s.
Rep(b, n) == [i \in 1..n |-> b]
Trim(s)   == IF Len(s) > WINDOW THEN SubSeq(s, Len(s) - WINDOW + 1, Len(s)) ELSE s

\* n samples b appended to an acknowledgement window (Neighbour._ack,
\* neighbours.py:154-157: shift in, mask, count saturated at 16).
Push(s, b, n) == Trim(s \o Rep(b, n))

\* Reception window after a frame whose counter is gap above the last one
\* (Neighbour._counter, neighbours.py:130-139): a gap above 16 restarts the
\* window with the frame alone; otherwise gap - 1 missing values, then it.
RxFrame(rx, gap) ==
    IF gap > WINDOW THEN <<TRUE>>
    ELSE Trim(rx \o Rep(FALSE, gap - 1) \o <<TRUE>>)

(***************************************************************************)
(* ETX in units of 1/ETX_ONE (Neighbour.etx, neighbours.py:161-172).       *)
(* nmin: acknowledgement samples needed before p_ack replaces the d_in    *)
(* estimate. The code is nmin = 1 ("if n:"); fix (6) raises it.           *)
(***************************************************************************)
EtxAck(ack) ==
    LET n == Len(ack)
        a == Count(ack)
    IN  IF a = 0 THEN (n + 1) * ETX_ONE          \* no success: as if the next one succeeded
        ELSE (n * ETX_ONE + a \div 2) \div a     \* 1 / p_ack, rounded

EtxDin(rx) ==                                    \* 1 / d_in², rounded
    LET r2 == Count(rx) * Count(rx)
        s  == Len(rx)
    IN  (s * s * ETX_ONE + r2 \div 2) \div r2

Etx(ack, rx, nmin) ==
    IF Len(ack) > 0 /\ Len(ack) >= nmin THEN EtxAck(ack) ELSE EtxDin(rx)

\* Same, with the latch of the anomaly 6 mitigation: once the entry has
\* held nmin samples, p_ack is used whenever the window is not empty.
EtxL(ack, rx, nmin, latched) ==
    IF Len(ack) > 0 /\ (Len(ack) >= nmin \/ latched) THEN EtxAck(ack) ELSE EtxDin(rx)

\* Eligibility through the ETX (Neighbour.eligible, neighbours.py:207-209);
\* the margin part is assumed satisfied (no SNR model).
EtxOk(etx) == etx <= ETX_MAX

\* round(8 x ETX), at least 8 (link_cost, parents.py:75-78).
LinkCost(etx) ==
    LET c == (etx * MIN_HOP + ETX_ONE \div 2) \div ETX_ONE
    IN  IF c > MIN_HOP THEN c ELSE MIN_HOP

\* Advertised rank with the preferred parent unchanged (Parents._select,
\* parents.py:273-279): follows the path cost only when it moved by 4 or
\* more; a new preferred parent (here: attaching from 255) takes the cost.
NewRank(rank, cost) ==
    IF rank = RANK_INFINITE THEN cost
    ELSE IF cost - rank >= RANK_HYSTERESIS \/ rank - cost >= RANK_HYSTERESIS THEN cost
    ELSE rank
=============================================================================
