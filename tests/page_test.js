// Tests of the status page's logic (src/sily/page.html, its <script id=logic>), run
// under node by tests/test_sily_page.py.
//
//   node tests/page_test.js GROUP           run one group of cases
//   node tests/page_test.js --groups A,B,C  check that the groups are exactly these
//
// Exit status 0 when every case of the group passed, 1 otherwise (each failure
// printed); a group without cases or unknown fails.

"use strict";
// The page shows times in the phone's time zone: the cases are written for Paris
// (UTC+1 in winter, UTC+2 in summer), set before any date is built.
process.env.TZ = "Europe/Paris";
const assert = require("assert");
const fs = require("fs");
const path = require("path");

const html = fs.readFileSync(path.join(__dirname, "..", "src", "sily", "page.html"), "utf8");
const code = html.match(/<script id=logic>([\s\S]*?)<\/script>/)[1];
const NAMES = ["cat", "uplink", "derive", "summary", "alarms", "diff", "fresh", "load", "save", "step", "header",
  "banner", "cards", "nodeTable", "layout", "edges", "graph", "logHtml", "box", "MARGIN_DB", "DELIVERY_MIN",
  "DELIVERY_N", "WINDOW_S", "BATTERY_MV", "DUTY_MAX", "STALE_S", "LOG_MAX", "R", "GW"];
const P = new Function(code + "\nreturn {" + NAMES.join(",") + "};")();

// --- Fixtures: a gateway and four nodes --------------------------------------------
// 1 ok, 1 hop; 2 ok, 2 hops via 1; 3 late, 1 hop; 4 unreachable (parent 9, never heard).

const T0 = Date.UTC(2026, 10, 28, 9, 0, 0);

function node(a, o) {
  return Object.assign({address: a, name: "box" + a, firmware: "0.1", state: "ok", heard_s: 5, age_s: 5,
    presences: 10, restarts: 0, parent: 0, hops: 1, parent_changes: 0, parent_age_s: null, flags: 0,
    battery: 3900, mode: "deployment", topology_age_s: 30, links: [[0, 15, 1, 100]]}, o);
}

function nodes() {
  return [
    node(1, {links: [[0, 18, 1, 100], [2, 12, 0, 97], [3, 9, 0, 90]]}),
    node(2, {parent: 1, hops: 2, battery: 3700, links: [[1, 12, 1, 98], [0, 4, 1, 60]]}),
    node(3, {state: "late", age_s: 100, heard_s: 100, battery: 3800, links: [[0, 11, 1, 99]]}),
    node(4, {parent: 9, hops: null, links: [[9, 7, 1, 80]]}),
  ];
}

function nb(a, margin) {
  return {address: a, rank: 2, margin, last_margin: margin, etx: 130, d_in: 99, age_s: 3, parent: 0,
    frames: 50, rssi: -80, rssi_mean: -81, snr_mean: 60};
}

function origin(a, rx, exp) {
  return {address: a, boot: 1, received: rx, expected: exp, duplicates: 0, reordered: 0, late: 0, stale: 0,
    boots: 1, samples: 3, median_ms: 50, p90_ms: 90, max_ms: 120, offset_t: 0, offset_ms: 0,
    delivery: exp ? rx / exp : null};
}

function gateway(o, list) {
  const b = Object.assign({role: "gateway", address: 0, name: "base", boot: 12, battery: 4100, mode: "deployment",
    attached: true, heap: 120000, utc: T0, time_s: 3725, parent: null, rank: 0, grounded: true,
    neighbours: [nb(1, 150), nb(3, 120)], foreign: [0, 0],
    counters: {tx_frames: 10, rx_frames: 20, queue: 0, duty_ms: 2000, duty_budget_ms: 10000, journal_lines: 5,
      journal_errors: 0},
    traffic: {role: "receiver", nodes: [origin(1, 100, 100), origin(2, 50, 50), origin(3, 40, 40)], malformed: 0,
      items_lost: 0, nodes_beyond: 0}}, o);
  return {box: b, mesh: {period_s: 30, beyond: 0, nodes: list || nodes()}};
}

function nodeBox(o) {
  return {box: Object.assign({role: "node", address: 2, name: "box2", boot: 3, battery: 3900, mode: "deployment",
    attached: true, heap: 90000, utc: null, time_s: 59, parent: 1, rank: 3, grounded: true,
    neighbours: [nb(1, 120), nb(0, 40)], foreign: [0, 0],
    counters: {queue: 0, duty_ms: 100, duty_budget_ms: 10000},
    traffic: {role: "sender", period_s: 10, size: 10, submitted: 5, accepted: 5, refused: 0, failed: 0}}, o)};
}

// A gateway answer with node a's fields replaced.
function withNode(a, o, list) {
  return gateway({}, (list || nodes()).map(n => (n.address == a ? Object.assign({}, n, o) : n)));
}

function view(j, now) {
  return P.derive(P.fresh(), j, now || T0);
}

const keys = A => A.map(x => x.k).sort();
const texts = S => S.log.map(e => e[2]);

// A store kept in memory, or failing on every access.
function memStore() {
  const m = {};
  return {getItem: k => (k in m ? m[k] : null), setItem: (k, v) => (m[k] = String(v)), m};
}
const badStore = {getItem: () => { throw new Error("denied"); }, setItem: () => { throw new Error("full"); }};

// Run answers [j, t] through step; return the state.
function run(list, store, S) {
  S = S || P.fresh();
  for (const [j, t] of list) P.step(S, j, t, store || null);
  return S;
}

const G = {};

G.summary = {
  categories() {
    const c = view(gateway()).nodes.map(n => n.c);
    assert.deepStrictEqual(c, ["ok", "ok", "late", "unreachable"]);
    assert.strictEqual(P.cat(node(5, {state: "silent", hops: null})), "silent");
  },
  uplink_lower_of_both_readings() {
    const v = view(gateway());
    // Node 1: its entry 18 dB, the gateway's reading 15.0 dB: 15. Node 2: its entry for parent 1.
    assert.deepStrictEqual(v.nodes.map(n => n.up), [15, 12, 11, 7]);
    assert.strictEqual(P.uplink(node(5, {links: [[0, -128, 1, 50]]}), {}), null);
  },
  counts_weakest_lowest_duty() {
    const s = P.summary(view(gateway()));
    assert.deepStrictEqual([s.ok, s.late, s.silent, s.unreachable], [2, 1, 0, 1]);
    assert.strictEqual(s.up[0], 7);
    assert.strictEqual(s.up[1].address, 4);
    assert.strictEqual(s.bat[0], 3700);
    assert.strictEqual(s.bat[1].address, 2);
    assert.strictEqual(s.duty, 0.2);
    assert.deepStrictEqual(s.all, [190, 190]);
  },
  silent_nodes_left_out_of_weakest_and_lowest() {
    const s = P.summary(view(withNode(4, {state: "silent", battery: 3100})));
    assert.strictEqual(s.silent, 1);
    assert.strictEqual(s.up[0], 11);
    assert.strictEqual(s.bat[0], 3700);
  },
  banner_text() {
    const v = view(gateway()), h = P.banner(v, P.alarms(v, null));
    for (const t of ["2 ok", "1 late", "0 silent", "1 unreachable", "weakest uplink 7 dB (4 box4)",
      "lowest battery 3.70 V (2 box2)", "duty 20.0 %", "test -, all 100.0 %"]) assert.ok(h.includes(t), t);
    assert.ok(h.startsWith('<div class="ban l2">'), h.slice(0, 30));
  },
};

G.recent = {
  window_deltas() {
    const S = P.fresh(), g = (rx, exp) => gateway({traffic: {role: "receiver", nodes: [origin(1, rx, exp)]}});
    P.derive(S, g(100, 100), T0);
    P.derive(S, g(110, 112), T0 + 60000);
    let v = P.derive(S, g(118, 122), T0 + 120000);
    assert.deepStrictEqual(v.rec, [18, 22]);
    assert.deepStrictEqual(v.nodes[0].rec, [18, 22]);
    assert.strictEqual(v.span, 120);
    // 5 min later, the first two answers are out of the window: from the third.
    v = P.derive(S, g(130, 132), T0 + 120000 + P.WINDOW_S * 1000);
    assert.deepStrictEqual(v.rec, [12, 10]);
  },
  first_answer_has_none() {
    const v = view(gateway());
    assert.deepStrictEqual(v.rec, [0, 0]);
    assert.strictEqual(v.nodes[0].rec, undefined);
  },
  counters_going_down_ignored() {
    const S = P.fresh(), g = (rx, exp) => gateway({traffic: {role: "receiver", nodes: [origin(1, rx, exp)]}});
    P.derive(S, g(100, 100), T0);
    const v = P.derive(S, g(5, 6), T0 + 5000);
    assert.deepStrictEqual(v.rec, [0, 0]);
  },
  gateway_reboot_restarts_the_window() {
    const g = (boot, rx, exp) => gateway({boot, traffic: {role: "receiver", nodes: [origin(1, rx, exp)]}});
    const S = run([[g(12, 100, 100), T0], [g(12, 110, 110), T0 + 5000], [g(13, 2, 4), T0 + 10000]]);
    assert.deepStrictEqual(S.v.rec, [0, 0]);
    P.step(S, g(13, 4, 8), T0 + 15000, null);
    assert.deepStrictEqual(S.v.rec, [2, 4]);
  },
};

G.alarms = {
  fixture() {
    const A = P.alarms(view(gateway()), null);
    // 4 unreachable; its uplink 7 dB and node 1's? 15: no. Node 2's 12, node 3's 11: no.
    assert.deepStrictEqual(keys(A), ["m4", "u4"]);
    assert.strictEqual(A[0].l, 2);
    assert.strictEqual(A[0].k, "u4");
  },
  silent_only_the_silent_alarm() {
    const A = P.alarms(view(withNode(4, {state: "silent", battery: 3000})), null);
    assert.deepStrictEqual(keys(A), ["s4"]);
  },
  flags_but_not_first_report() {
    const A = P.alarms(view(withNode(1, {flags: 0x7f})), null);
    assert.deepStrictEqual(keys(A).filter(k => k[0] == "f"), ["f1A", "f1D", "f1K", "f1Q", "f1R"]);
    assert.strictEqual(A.find(x => x.k == "f1K").l, 2);
    assert.strictEqual(A.find(x => x.k == "f1D").l, 1);
  },
  thresholds() {
    const ks = (o, b) => keys(P.alarms(view(withNode(1, o, b)), null)).filter(k => !k.endsWith("4"));
    assert.deepStrictEqual(ks({links: [[0, P.MARGIN_DB, 1, 100]]}), []);
    assert.deepStrictEqual(ks({links: [[0, P.MARGIN_DB - 1, 1, 100]]}), ["m1"]);
    assert.deepStrictEqual(ks({battery: P.BATTERY_MV}), []);
    assert.deepStrictEqual(ks({battery: P.BATTERY_MV - 20}), ["b1"]);
  },
  hysteresis() {
    const at = (m, prev) => keys(P.alarms(view(withNode(1, {links: [[0, m, 1, 100]]})), prev)).includes("m1");
    assert.strictEqual(at(10, null), false);
    assert.strictEqual(at(10, {m1: 1}), true); // raised: cleared only at MARGIN_DB + 1
    assert.strictEqual(at(11, {m1: 1}), false);
  },
  recent_delivery() {
    const g = rx => gateway({traffic: {role: "receiver", nodes: [origin(1, rx, 100)]}});
    const S = P.fresh();
    P.derive(S, gateway({traffic: {role: "receiver", nodes: [origin(1, 0, 0)]}}), T0);
    const N = P.DELIVERY_N;
    let v = P.derive(S, gateway({traffic: {role: "receiver", nodes: [origin(1, N - 3, N - 1)]}}), T0 + 5000);
    assert.ok(!keys(P.alarms(v, null)).includes("d1"), "too few messages to judge");
    assert.strictEqual(N * P.DELIVERY_MIN, N - 1, "one loss in N: at the threshold");
    const S1 = P.fresh();
    P.derive(S1, gateway({traffic: {role: "receiver", nodes: [origin(1, 0, 0)]}}), T0);
    v = P.derive(S1, gateway({traffic: {role: "receiver", nodes: [origin(1, N - 1, N)]}}), T0 + 5000);
    assert.ok(!keys(P.alarms(v, null)).includes("d1"), "one loss in N");
    v = P.derive(S, g(90), T0 + 10000);
    assert.deepStrictEqual(keys(P.alarms(v, null)).filter(k => k == "d1" || k == "rec"), ["d1", "rec"]);
    v = P.derive(P.fresh(), g(100), T0);
    assert.ok(!keys(P.alarms(v, null)).includes("d1"));
  },
  gateway_duty_and_battery() {
    const v = view(gateway({battery: 3400, counters: {duty_ms: 8500, duty_budget_ms: 10000, queue: 0}}));
    const A = P.alarms(v, null);
    assert.ok(keys(A).includes("duty") && keys(A).includes("bat"));
    assert.strictEqual(A.find(x => x.k == "duty").l, 2);
    assert.strictEqual(A.find(x => x.k == "duty").a, 0);
    const ok = P.alarms(view(gateway({counters: {duty_ms: 7900, duty_budget_ms: 10000}})), null);
    assert.ok(!keys(ok).includes("duty"));
  },
  graph_full() {
    const j = gateway();
    j.mesh.beyond = 3;
    assert.ok(keys(P.alarms(view(j), null)).includes("beyond"));
  },
  node_box_detached() {
    const A = P.alarms(view(nodeBox({attached: false})), null);
    assert.deepStrictEqual(keys(A), ["det"]);
    assert.deepStrictEqual(keys(P.alarms(view(nodeBox()), null)), []);
  },
};

G.events = {
  first_answer_marks_the_start() {
    const S = run([[gateway(), T0]]);
    assert.deepStrictEqual(texts(S), ["watching gateway 0 base, 4 nodes"]);
    assert.strictEqual(S.log[0][0], T0);
  },
  states_restart_parent_flags() {
    const a = nodes(), b = withNode(3, {state: "silent"}, a).mesh.nodes, c = withNode(3, {state: "ok", restarts: 1}, b).mesh.nodes;
    const d = withNode(2, {parent: 0, hops: 1, flags: 4, links: [[0, 14, 1, 99]]}, c).mesh.nodes;
    const S = run([[gateway({}, a), T0], [gateway({}, b), T0 + 5000], [gateway({}, c), T0 + 10000],
      [gateway({}, d), T0 + 15000], [withNode(2, {flags: 0}, d), T0 + 20000]]);
    assert.deepStrictEqual(texts(S).slice(0, 6), [
      "cleared: node 2 box2 flag D",
      "alarm: node 2 box2 flag D duty > 80 %",
      "node 2 box2 parent 1 to 0",
      "node 3 box3 restarted",
      "node 3 box3 ok again",
      "node 3 box3 silent",
    ]);
    assert.deepStrictEqual(S.log.slice(0, 6).map(e => [e[0] - T0, e[1]]),
      [[20000, 0], [15000, 1], [15000, 0], [10000, 1], [10000, 0], [5000, 2]]);
  },
  late_and_unreachable() {
    const S = run([[gateway(), T0], [withNode(1, {state: "late"}), T0 + 5000],
      [withNode(1, {hops: null, parent: 7}), T0 + 10000]]);
    assert.deepStrictEqual(texts(S).slice(0, 3), ["node 1 box1 parent 0 to 7", "node 1 box1 unreachable", "node 1 box1 late"]);
    assert.deepStrictEqual(S.log.slice(1, 3).map(e => e[1]), [2, 1]);
  },
  new_node_heard() {
    const S = run([[gateway(), T0], [gateway({}, nodes().concat([node(6, {parent: 1, hops: 2})])), T0 + 5000]]);
    assert.strictEqual(texts(S)[0], "node 6 box6 heard, ok, 2 hops");
  },
  gateway_reboot() {
    const S = run([[gateway(), T0], [withNode(1, {restarts: 2}), T0 + 5000],
      [gateway({boot: 13}, nodes().slice(0, 1)), T0 + 10000]]);
    assert.deepStrictEqual(texts(S).slice(0, 2), ["node 1 box1 heard, ok, 1 hop", "gateway rebooted (boot 12 to 13)"]);
    assert.strictEqual(texts(S)[2], "node 1 box1 restarted 2 times");
  },
  alarm_raised_then_cleared_once() {
    const low = withNode(1, {battery: 3300}), S = run([[gateway(), T0], [low, T0 + 5000], [low, T0 + 10000],
      [gateway(), T0 + 15000]]);
    assert.deepStrictEqual(texts(S).slice(0, 2), ["cleared: node 1 box1 battery", "alarm: node 1 box1 battery 3.30 V"]);
  },
  node_page() {
    const S = run([[nodeBox(), T0], [nodeBox({attached: false}), T0 + 5000],
      [nodeBox({parent: 0}), T0 + 10000], [nodeBox({parent: 1}), T0 + 15000]]);
    assert.deepStrictEqual(texts(S), ["parent 0 to 1", "attached, parent 0", "detached", "watching box 2 box2"]);
  },
  log_bounded_newest_first() {
    const list = [[gateway(), T0]];
    for (let i = 1; i <= 2 * P.LOG_MAX; i++) list.push([withNode(1, {restarts: i}), T0 + 5000 * i]);
    const S = run(list);
    assert.strictEqual(S.log.length, P.LOG_MAX);
    assert.strictEqual(S.log[0][0], T0 + 5000 * 2 * P.LOG_MAX);
    assert.ok(S.log.every((e, i) => i == 0 || e[0] <= S.log[i - 1][0]));
  },
};

G.gaps = {
  no_answer() {
    const S = run([[gateway(), T0], [gateway(), T0 + 5000], [gateway(), T0 + 5000 + P.STALE_S * 1000 + 1]]);
    assert.deepStrictEqual(texts(S).slice(0, 1), ["gap: no data since 10:00:05 (no answer)"]);
    assert.strictEqual(S.log[0][1], 1);
  },
  within_the_limit_none() {
    const S = run([[gateway(), T0], [gateway(), T0 + P.STALE_S * 1000]]);
    assert.strictEqual(S.log.length, 1);
  },
  page_hidden() {
    const S = run([[gateway(), T0]]);
    S.why = "page hidden or phone locked";
    run([[gateway(), T0 + 60000], [gateway(), T0 + 125000]], null, S);
    assert.deepStrictEqual(texts(S).slice(0, 2), ["gap: no data since 10:01:00 (no answer)",
      "gap: no data since 10:00:00 (page hidden or phone locked)"]);
  },
  reload_from_the_store() {
    const st = memStore();
    run([[gateway(), T0], [withNode(3, {state: "silent"}), T0 + 5000]], st);
    const S = run([[withNode(3, {state: "ok"}), T0 + 300000]], st);
    assert.deepStrictEqual(texts(S).slice(0, 3), ["node 3 box3 ok again", "gap: no data since 10:00:05 (page closed)",
      "node 3 box3 silent"]);
  },
  another_box() {
    const st = memStore(), S = run([[gateway(), T0], [nodeBox(), T0 + 5000], [gateway(), T0 + 60000]], st);
    assert.deepStrictEqual(texts(S), ["gap: no data since 10:00:00 (another box watched)", "watching gateway 0 base, 4 nodes"]);
    assert.ok(st.m["sily:n2"] && st.m["sily:g0"]);
  },
};

G.storage = {
  bounded_and_restored() {
    const st = memStore(), list = [[gateway(), T0]];
    for (let i = 1; i <= 300; i++) list.push([withNode(1, {restarts: i}), T0 + 5000 * i]);
    const S = run(list, st), o = JSON.parse(st.m["sily:g0"]);
    assert.strictEqual(o.log.length, P.LOG_MAX);
    assert.strictEqual(o.t, S.t);
    const L = P.load(st, "g0");
    assert.deepStrictEqual(L.log, S.log);
    assert.deepStrictEqual(L.p, S.p);
  },
  failing_store_still_works() {
    const S = run([[gateway(), T0], [withNode(3, {state: "silent"}), T0 + 5000]], badStore);
    assert.strictEqual(texts(S)[0], "node 3 box3 silent");
    assert.deepStrictEqual(P.load(badStore, "g0"), {});
    assert.deepStrictEqual(P.load(null, "g0"), {});
  },
  corrupt_entry_ignored() {
    const st = memStore();
    st.setItem("sily:g0", "{not json");
    assert.deepStrictEqual(texts(run([[gateway(), T0]], st)), ["watching gateway 0 base, 4 nodes"]);
    st.setItem("sily:g0", JSON.stringify({t: T0, p: {boot: 12}, log: []}));
    assert.strictEqual(P.load(st, "g0").p, null);
  },
};

G.graph = {
  layers() {
    const L = P.layout(view(gateway())), p = L.pos;
    assert.deepStrictEqual(p[0], [P.GW / 2, 26]);
    assert.ok(p[1][1] == p[3][1] && p[1][1] > p[0][1], "hop 1 below the gateway");
    assert.ok(p[2][1] > p[1][1], "hop 2 below hop 1");
    assert.ok(L.top > p[2][1] && p[4][1] > L.top, "unreachable apart, below the rest");
    assert.ok(L.h > p[4][1] + P.R);
  },
  edges_by_kind() {
    const v = view(gateway()), E = P.edges(v, P.layout(v).pos), k = {};
    for (const key in E) k[key] = [E[key][0], E[key][1]];
    assert.deepStrictEqual(k, {
      "0-1": [0, "15dB"], "1-2": [0, "12dB"], "0-3": [0, "11dB"], "0-2": [1, ""], "1-3": [2, ""],
    });
  },
  delivery_label_when_low() {
    const v = view(withNode(1, {links: [[0, 18, 1, 80]]})), E = P.edges(v, P.layout(v).pos);
    assert.strictEqual(E["0-1"][1], "15dB·80%");
  },
  svg_structure() {
    const v = view(gateway()), s = P.graph(v, P.alarms(v, null));
    const count = re => (s.match(re) || []).length;
    assert.strictEqual(count(/<line class=e0 /g), 3);
    assert.strictEqual(count(/<path class=e1 /g), 1);
    assert.strictEqual(count(/<path class=e2 /g), 1);
    assert.strictEqual(count(/<g class="n /g), 5);
    assert.strictEqual(count(/<g class="n unreachable">/g), 1);
    assert.strictEqual(count(/<g class="n late">/g), 1);
    assert.strictEqual(count(/<circle class=x /g), 1); // the alarms of node 4
    assert.strictEqual(count(/<rect class=ap /g), 1);
    // An unquoted value before "/>" takes the slash: the element stays open, hiding the rest.
    assert.strictEqual(count(/=[^"\s>]+\/>/g), 0, s);
  },
  twenty_nodes_apart_and_inside() {
    const list = [];
    for (let a = 1; a <= 20; a++) list.push(node(a, a <= 14 ? {} : {parent: a - 14, hops: 2}));
    list[19].hops = null;
    const v = view(gateway({}, list)), L = P.layout(v), pts = Object.values(L.pos);
    assert.strictEqual(pts.length, 21);
    for (const [x, y] of pts) assert.ok(x - P.R >= 0 && x + P.R <= P.GW && y - P.R >= 0 && y + P.R + 13 <= L.h, x + "," + y);
    for (let i = 0; i < pts.length; i++)
      for (let k = 0; k < i; k++)
        assert.ok(Math.hypot(pts[i][0] - pts[k][0], pts[i][1] - pts[k][1]) >= 3 * P.R, "overlap " + pts[i] + " " + pts[k]);
    assert.ok(P.graph(v, []).startsWith('<svg viewBox="0 0 360 '));
  },
};

G.html = {
  escaped_everywhere() {
    const bad = '<img src=x onerror=alert(1)>"', v = view(withNode(1, {name: bad, mode: bad}));
    const S = run([[gateway(), T0], [withNode(1, {name: bad, state: "late"}), T0 + 5000]]);
    for (const h of [P.cards(v, []), P.nodeTable(v), P.graph(v, P.alarms(v, null)), P.banner(v, P.alarms(v, null)),
      P.logHtml(S.log), P.box(Object.assign(nodeBox().box, {traffic: {role: "sender", period_s: bad}}))])
      assert.ok(!h.includes("<img") && !h.includes('"' + "onerror"), h.slice(0, 80));
  },
  cards_content() {
    const v = view(withNode(2, {flags: 5, restarts: 1})), h = P.cards(v, P.alarms(v, null)); // D and B: D only
    assert.strictEqual((h.match(/<div class="card /g) || []).length, 4);
    assert.ok(h.includes('<div class="card late"><b>3 box3</b>'));
    assert.ok(h.includes("<b>2 box2</b> <i>D</i><br>ok · heard 5 s<br>2 hops via 1<br>up 12 dB · 98 %"));
    assert.ok(h.includes("1 restart · 0 parent changes"));
  },
  log_dates() {
    const now = Date.now(), h = P.logHtml([[now, 1, "x"], [Date.UTC(2020, 0, 2, 3, 4, 5), 0, "y"],
      [Date.UTC(2020, 6, 1, 22, 30, 0), 2, "z"]]);
    assert.ok(h.startsWith("<li class=l1>" + new Date(now).toTimeString().slice(0, 8) + " x"));
    assert.ok(h.includes("<li class=l0>01-02 04:04:05 y"));
    assert.ok(h.includes("<li class=l2>07-02 00:30:00 z")); // the local date, past midnight in summer
    assert.strictEqual(P.logHtml([]), "<li>no event yet");
  },
  header_and_node_banner() {
    const h = P.header(gateway().box);
    assert.strictEqual(h[0], "Gateway 0 base");
    assert.ok(h[1].endsWith("clock 10:00:00 UTC+1"));
    assert.ok(P.header(Object.assign(gateway().box, {utc: Date.UTC(2026, 6, 1, 9, 0, 0)}))[1].endsWith("clock 11:00:00 UTC+2"));
    assert.ok(P.header(Object.assign(gateway().box, {utc: null}))[1].endsWith("clock not set"));
    process.env.TZ = "America/St_Johns"; // a zone behind UTC by hours and minutes
    try {
      assert.ok(P.header(gateway().box)[1].endsWith("clock 05:30:00 UTC-3:30"));
    } finally {
      process.env.TZ = "Europe/Paris";
    }
    const v = view(nodeBox({attached: false}));
    const b = P.banner(v, P.alarms(v, null));
    assert.ok(b.startsWith('<div class="ban l2">') && b.includes("DETACHED") && b.includes("this box detached"));
  },
};

function main(argv) {
  if (argv[0] == "--groups") {
    const want = argv[1].split(",").sort(), have = Object.keys(G).sort();
    if (want.join() == have.join()) return 0;
    console.error("page_test: groups " + have.join(",") + ", expected " + want.join(","));
    return 1;
  }
  const g = G[argv[0]], names = g ? Object.keys(g) : [];
  if (!names.length) {
    console.error("page_test: no case in group " + argv[0]);
    return 1;
  }
  let failed = 0;
  for (const n of names) {
    try {
      g[n]();
    } catch (e) {
      failed++;
      console.error("page_test FAIL " + argv[0] + "." + n + ": " + (e.stack || e));
    }
  }
  return failed ? 1 : 0;
}

process.exitCode = main(process.argv.slice(2));
