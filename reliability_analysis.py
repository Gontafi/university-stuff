#!/usr/bin/env python3
import math

# ---------------------------------------------------------------- input data
T = 720  # continuous operating period, hours

# (event, component, failure hour, repair-completed hour)
LOG = [
    (1,  "Application Server A",  38,  40),
    (2,  "Primary Database",      91,  95),
    (3,  "Campus Network",       143, 144),
    (4,  "Application Server B", 201, 203),
    (5,  "Primary Database",     287, 291),
    (6,  "Load Balancer",        356, 357),
    (7,  "Application Server A", 411, 413),
    (8,  "Campus Network",       478, 480),
    (9,  "Primary Database",     529, 534),
    (10, "Application Server B", 601, 603),
    (11, "Application Server A", 654, 655),
    (12, "Load Balancer",        689, 690),
]

# supplied component reliabilities
R = {
    "Load Balancer":        0.995,
    "Application Server A": 0.970,
    "Application Server B": 0.970,
    "Primary Database":     0.980,
    "Standby Database":     0.990,
    "Campus Network":       0.995,
}

par = lambda *r: 1 - math.prod(1 - x for x in r)   # active redundancy (OR)


def part_a():
    """Observed-data metrics: downtime, uptime, availability, MTTF/MTTR/MTBF."""
    DT = sum(r - f for _, _, f, r in LOG)
    n, UT = len(LOG), T - sum(r - f for _, _, f, r in LOG)
    A, MTTF, MTTR = UT / T, UT / n, DT / n
    MTBF, lam = MTTF + MTTR, n / UT

    print("=" * 62)
    print("PART A - RELIABILITY METRICS")
    print("=" * 62)
    print(f"Downtime     = sum(repair durations) = {DT} h")
    print(f"Uptime       = {T} - {DT}              = {UT} h")
    print(f"Availability = {UT}/{T}               = {A:.6f} = {A*100:.2f} %")
    print(f"MTTF         = uptime/failures = {UT}/{n} = {MTTF:.2f} h")
    print(f"MTTR         = downtime/failures = {DT}/{n} = {MTTR:.2f} h")
    print(f"MTBF         = MTTF + MTTR           = {MTBF:.2f} h")
    print(f"cross-check  A = MTTF/MTBF           = {MTTF/MTBF:.6f}  (matches {A:.6f})")
    print(f"Failure rate lambda = {n}/{UT}          = {lam:.6f} failures/h")
    print(f"Repair rate  mu     = {n}/{DT}           = {n/DT:.6f} repairs/h")
    print(f"R(t) = exp(-lambda*t):  R(24h) = {math.exp(-lam*24):.4f}   "
          f"R(168h) = {math.exp(-lam*168):.4f}")

    print("\nPer-component contribution:")
    print(f"{'Component':<22}{'fails':>6}{'down h':>8}{'% of DT':>9}"
          f"{'MTBF_c':>9}{'MTTR_c':>8}{'A_c':>9}")
    comp = {}
    for _, c, f, r in LOG:
        comp.setdefault(c, []).append(r - f)
    for c, d in sorted(comp.items(), key=lambda x: -sum(x[1])):
        k, s = len(d), sum(d)
        print(f"{c:<22}{k:>6}{s:>8}{100*s/DT:>8.1f}%"
              f"{(T-s)/k:>9.2f}{s/k:>8.2f}{(T-s)/T:>9.4f}")
    return DT, UT


def part_b():
    """RBD: LB -> [AppA || AppB] -> [PrimaryDB || Standby] -> Campus Network."""
    R_app = par(R["Application Server A"], R["Application Server B"])
    R_db = par(R["Primary Database"], R["Standby Database"])
    R_lb, R_net = R["Load Balancer"], R["Campus Network"]
    R_sys = R_lb * R_app * R_db * R_net

    print("\n" + "=" * 62)
    print("PART B - RELIABILITY BLOCK DIAGRAM")
    print("=" * 62)
    print("LB -> [AppA || AppB] -> [PrimaryDB || Standby] -> Network")
    print("     (series of four subsystem blocks)")
    print(f"R_app = 1-(1-0.970)(1-0.970)        = {R_app:.6f}")
    print(f"R_db  = 1-(1-0.980)(1-0.990)        = {R_db:.6f}")
    print(f"R_lb  = {R_lb}   (single component, no redundancy)")
    print(f"R_net = {R_net}   (single component, no redundancy)")
    print(f"R_sys = R_lb * R_app * R_db * R_net = {R_sys:.6f} = {R_sys*100:.2f} %")
    print(f"Q_sys = 1 - R_sys                   = {1-R_sys:.6f}"
          f"  -> {(1-R_sys)*T:.2f} h expected loss per {T} h")
    print("Block sensitivity dR_sys/dR_block (reliability bought per block):")
    for nm, val in [("Load Balancer", R_lb), ("Application block", R_app),
                    ("Database block", R_db), ("Campus Network", R_net)]:
        print(f"   {nm:<20}{R_sys/val:>9.4f}")
    return R_app, R_db, R_sys


def part_c():
    """FMEA with RPN = S x O x D, ranked in code so the order cannot slip."""
    FMEA = [  # component, failure mode, S, O, D, mitigation
        ("Primary Database",     "DB engine crash / disk I/O failure", 8, 6, 5,
         "automatic failover + replication monitoring"),
        ("Standby Database",     "standby unavailable (silent)",       9, 2, 8,
         "replication-lag monitoring, scheduled failover drills"),
        ("Application Server A", "server A crash",                     5, 5, 4,
         "A/B redundancy + health checks, root-cause fix"),
        ("Load Balancer",        "traffic distribution stops",         9, 3, 3,
         "redundant load balancer / automatic failover"),
        ("Campus Network",       "campus network failure",             9, 3, 3,
         "network redundancy, alternate connectivity"),
        ("Application Server B", "server B crash",                     5, 4, 4,
         "A/B redundancy + health checks"),
    ]
    rows = sorted(((c, m, s, o, d, s * o * d, mit) for c, m, s, o, d, mit in FMEA),
                  key=lambda r: -r[5])

    print("\n" + "=" * 62)
    print("PART C - FMEA   (RPN = S x O x D)")
    print("=" * 62)
    print(f"{'#':<3}{'Component':<22}{'S':>3}{'O':>3}{'D':>3}{'RPN':>6}   Failure mode")
    for i, (c, m, s, o, d, rpn, _) in enumerate(rows, 1):
        print(f"{i:<3}{c:<22}{s:>3}{o:>3}{d:>3}{rpn:>6}   {m}")
    print("\nTop three by RPN (mitigation order):")
    for i, (c, _, _, _, _, rpn, mit) in enumerate(rows[:3], 1):
        print(f"  {i}. {c} (RPN {rpn}) -> {mit}")
    tot = sum(r[5] for r in rows)
    print(f"Sum RPN = {tot}, mean = {tot/len(rows):.1f}")


def part_d(R_sys):
    """Fault tree: minimal cut sets and quantitative top-event probability."""
    q = {k: 1 - v for k, v in R.items()}
    CUTSETS = [
        ("Load Balancer",),
        ("Campus Network",),
        ("Application Server A", "Application Server B"),
        ("Primary Database", "Standby Database"),
    ]
    print("\n" + "=" * 62)
    print("PART D - FAULT TREE ANALYSIS")
    print("=" * 62)
    print("TOP = LB OR Network OR (AppA AND AppB) OR (PrimaryDB AND Standby)")
    print("\nMinimal cut sets:")
    tot = 0.0
    for cs in CUTSETS:
        p = math.prod(q[c] for c in cs)
        tot += p
        print(f"   order-{len(cs)}  {{{' AND '.join(cs)}}}")
        print(f"            P = {p:.8f}")
    P_top = 1 - R_sys
    print(f"\nRare-event upper bound  sum(P_cut) = {tot:.6f}")
    print(f"Exact  P(TOP) = 1 - R_sys          = {P_top:.6f} = {P_top*100:.3f} %")
    print("\nCut-set importance (share of total risk):")
    for cs in sorted(CUTSETS, key=lambda c: -math.prod(q[x] for x in c)):
        p = math.prod(q[c] for c in cs)
        print(f"   {' + '.join(cs):<46}{100*p/tot:>6.2f} %")
    print("   -> the two order-1 cut sets (single points of failure) carry "
          f"{100*(q['Load Balancer']+q['Campus Network'])/tot:.1f} % of the risk")


def part_e(R_app, R_db, R_sys):
    """Before/after comparison tying each recommendation to a number."""
    R_lb, R_net = R["Load Balancer"], R["Campus Network"]
    scen = {
        "baseline":
            R_lb * R_app * R_db * R_net,
        "S1 redundant Load Balancer":
            par(R_lb, R_lb) * R_app * R_db * R_net,
        "S2 secondary Campus Network path":
            R_lb * R_app * R_db * par(R_net, R_net),
        "S3 Primary DB root-cause fix (0.980->0.990)":
            R_lb * R_app * par(0.990, R["Standby Database"]) * R_net,
        "S4 = S1 + S2  (remove both SPOFs)":
            par(R_lb, R_lb) * R_app * R_db * par(R_net, R_net),
        "S5 = S1 + S2 + S3  (full programme)":
            par(R_lb, R_lb) * R_app * par(0.990, R["Standby Database"]) * par(R_net, R_net),
    }
    base = scen["baseline"]

    print("\n" + "=" * 62)
    print("PART E - ENGINEERING RECOMMENDATION (before / after)")
    print("=" * 62)
    print(f"{'scenario':<46}{'R_sys':>10}{'Q_sys':>11}{'h lost':>9}{'risk':>10}")
    for k, v in scen.items():
        delta = "" if k == "baseline" else f"-{100*(1-(1-v)/(1-base)):.1f} %"
        print(f"{k:<46}{v:>10.6f}{1-v:>11.6f}{(1-v)*T:>9.2f}{delta:>10}")

    s4 = 100 * (1 - (1 - scen["S4 = S1 + S2  (remove both SPOFs)"]) / (1 - base))
    s3 = 100 * (1 - (1 - scen["S3 Primary DB root-cause fix (0.980->0.990)"]) / (1 - base))
    print(f"\nRemoving the two single points of failure cuts system unreliability")
    print(f"by {s4:.1f} %, while the database root-cause fix alone gives only {s3:.1f} %,")
    print("because the database is already redundant. FMEA priority (maintenance)")
    print("and RBD/FTA priority (architecture) therefore differ and both are reported.")


if __name__ == "__main__":
    print("Assignment 1 - Reliability Engineering and Failure Analysis")
    print("Alibek Asset | ID 255175 | CSE-2503 | variant 255175-A1\n")
    part_a()
    R_app, R_db, R_sys = part_b()
    part_c()
    part_d(R_sys)
    part_e(R_app, R_db, R_sys)
