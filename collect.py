#!/usr/bin/env python3
"""
Collect criterion results from target/criterion into one JSON file.

Reads, for every benchmark criterion has written:
  <dir>/new/benchmark.json   -> the human readable id
  <dir>/new/estimates.json   -> mean, median, std_dev, with 95% intervals, in ns

Also records which SIMD features the CPU exposes. primus_ntt picks its NTT
backend at run time from these (scalar, AVX2, AVX-512 DQ, AVX-512 IFMA), so
a result means nothing without them.

Usage:  python3 collect.py <path-to-primus-fhe> [out.json]
"""
import json, os, sys, platform, subprocess, datetime

def run(cmd, cwd=None):
    try:
        return subprocess.check_output(cmd, cwd=cwd, stderr=subprocess.STDOUT, text=True).strip()
    except Exception as e:
        return f"unavailable: {e.__class__.__name__}"

def cpu_model():
    s = platform.system()
    if s == "Darwin":
        return run(["sysctl", "-n", "machdep.cpu.brand_string"])
    if s == "Linux":
        try:
            for line in open("/proc/cpuinfo"):
                if line.lower().startswith("model name"):
                    return line.split(":", 1)[1].strip()
        except OSError:
            pass
    return platform.processor() or "unknown"

def cpu_features():
    """The flags primus_ntt's runtime dispatch actually looks at."""
    want = ["avx2", "avx512f", "avx512dq", "avx512ifma", "neon"]
    have = set()
    s = platform.system()
    if s == "Linux":
        try:
            for line in open("/proc/cpuinfo"):
                if line.lower().startswith(("flags", "features")):
                    have |= set(line.split(":", 1)[1].split())
                    break
        except OSError:
            pass
    elif s == "Darwin":
        out = run(["sysctl", "-a"]).lower()
        if "hw.optional.avx2_0: 1" in out: have.add("avx2")
        if "hw.optional.avx512f: 1" in out: have.add("avx512f")
        if "hw.optional.avx512dq: 1" in out: have.add("avx512dq")
        if "hw.optional.avx512ifma: 1" in out: have.add("avx512ifma")
        if "hw.optional.arm.feature" in out or platform.machine() == "arm64": have.add("neon")
    feats = {f: (f in have) for f in want}
    # what U64NttTable selects, mirroring crates/primus_ntt/src/ntt/prime64/table.rs:
    # IFMA, then DQ (checked on avx512dq alone), then AVX2, else scalar.
    # On anything that is not x86_64 the table is hard-wired to scalar.
    if platform.machine().lower() not in ("x86_64", "amd64"):
        backend = "scalar"
    elif feats["avx512ifma"]:
        backend = "avx512-ifma"
    elif feats["avx512dq"]:
        backend = "avx512-dq"
    elif feats["avx2"]:
        backend = "avx2"
    else:
        backend = "scalar"
    return feats, backend

def mem_bytes():
    s = platform.system()
    if s == "Darwin":
        v = run(["sysctl", "-n", "hw.memsize"])
        return int(v) if v.isdigit() else None
    if s == "Linux":
        try:
            for line in open("/proc/meminfo"):
                if line.startswith("MemTotal"):
                    return int(line.split()[1]) * 1024
        except OSError:
            pass
    return None

def power_state():
    if platform.system() == "Darwin":
        out = run(["pmset", "-g", "batt"])
        return "AC power" if "AC Power" in out else ("battery" if "Battery Power" in out else out[:80])
    if os.environ.get("GITHUB_ACTIONS") == "true":
        return "GitHub Actions hosted runner"
    return "unknown"

def est(e, key):
    v = e.get(key) or {}
    ci = v.get("confidence_interval") or {}
    return {"point_ns": v.get("point_estimate"), "lo_ns": ci.get("lower_bound"), "hi_ns": ci.get("upper_bound")}

def main():
    if len(sys.argv) < 2:
        sys.exit("usage: collect.py <path-to-primus-fhe> [out.json]")
    repo = os.path.abspath(sys.argv[1])
    out = sys.argv[2] if len(sys.argv) > 2 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "primus-fhe-bench.json")
    root = os.path.join(repo, "target", "criterion")
    if not os.path.isdir(root):
        sys.exit(f"no criterion output at {root}. Run the benches first.")
    results = []
    for dirpath, dirnames, filenames in os.walk(root):
        if os.path.basename(dirpath) != "new" or "estimates.json" not in filenames:
            continue
        with open(os.path.join(dirpath, "estimates.json")) as fh:
            e = json.load(fh)
        bid = group = None
        bj = os.path.join(dirpath, "benchmark.json")
        if os.path.exists(bj):
            with open(bj) as fh:
                b = json.load(fh)
            bid = b.get("full_id") or b.get("title")
            group = b.get("group_id")
        if not bid:
            bid = os.path.relpath(os.path.dirname(dirpath), root)
        results.append({"id": bid, "group": group, "mean": est(e, "mean"), "median": est(e, "median"),
                        "std_dev_ns": (e.get("std_dev") or {}).get("point_estimate")})
    results.sort(key=lambda r: r["id"])
    feats, backend = cpu_features()
    # tracked files only: target/ is gitignored and holds the results themselves
    patched = run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=repo)
    meta = {
        "collected_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "repo_commit": run(["git", "rev-parse", "HEAD"], cwd=repo),
        "repo_commit_date": run(["git", "log", "-1", "--format=%cs"], cwd=repo),
        "repo_modified": bool(patched) and not patched.startswith("unavailable"),
        "repo_diff": run(["git", "diff", "--stat"], cwd=repo) if patched else "",
        "rustc": run(["rustc", "--version"]),
        "cargo": run(["cargo", "--version"]),
        "os": f"{platform.system()} {platform.release()}",
        "arch": platform.machine(),
        "cpu": cpu_model(),
        "cpu_features": feats,
        "ntt_backend_expected": backend,
        "logical_cores": os.cpu_count(),
        "memory_bytes": mem_bytes(),
        "power": power_state(),
        "runner": "github-actions" if os.environ.get("GITHUB_ACTIONS") == "true" else "local",
        "simd_nightly": os.environ.get("PRIMUS_BENCH_SIMD", "no"),
        "count": len(results),
    }
    with open(out, "w") as fh:
        json.dump({"meta": meta, "results": results}, fh, indent=2)
    print(f"wrote {len(results)} results to {out}")
    print(f"cpu: {meta['cpu']}  arch: {meta['arch']}  expected NTT backend: {backend}")
    if meta["repo_modified"]:
        print("WARNING: the repository has local changes. These are not unmodified results.")
    for r in results:
        m = r["mean"]["point_ns"]
        print(f"  {r['id'][:70]:70s} {m/1000 if m else 0:>10.2f} us")

if __name__ == "__main__":
    main()
