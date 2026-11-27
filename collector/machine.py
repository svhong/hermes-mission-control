"""Health of the machine Hermes runs on: CPU, temperatures, memory, disks,
network, fans, power, and the Hermes container's own share.

Everything comes from /proc and /sys, which any user can read, so this needs
no extra package and no extra permission (the collector runs sandboxed as the
Hermes user). Nothing here touches Hermes's database, so it keeps working when
the database can't be read.

Rates (CPU %, network, disk I/O) compare two readings, so the first report
after start has none; after that each is the average since the previous report
(5 s while a dashboard is open, a minute otherwise).
"""
import glob
import os
import re
import socket
import time
from collections import deque

HISTORY_S = 15 * 60       # how far back the hero's little charts go
_WHOLE_DISK = re.compile(r"^(nvme\d+n\d+|sd[a-z]+|vd[a-z]+|xvd[a-z]+|mmcblk\d+)$")
# Virtual interfaces would count the same traffic twice (containers, Tailscale).
_SKIP_NIC = re.compile(r"^(lo|docker\d*|br-.*|veth.*|virbr.*|tailscale\d*|tun\d*|wg\d*)$")
_REAL_FS = {"ext4", "ext3", "xfs", "btrfs", "f2fs", "zfs", "bcachefs"}


def _read(path, default=None):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def _int(path):
    try:
        return int(_read(path))
    except (TypeError, ValueError):
        return None


def _cpulist(text):
    """'0-3,8,10-11' -> [0, 1, 2, 3, 8, 10, 11]"""
    out = []
    for part in (text or "").split(","):
        part = part.strip()
        if "-" in part:
            lo, hi = part.split("-", 1)
            out.extend(range(int(lo), int(hi) + 1))
        elif part:
            out.append(int(part))
    return out


def static_info():
    model = None
    for line in (_read("/proc/cpuinfo", "") or "").splitlines():
        if line.startswith("model name"):
            model = " ".join(line.split(":", 1)[1].split())
            break
    os_name = None
    for line in (_read("/etc/os-release", "") or "").splitlines():
        if line.startswith("PRETTY_NAME="):
            os_name = line.split("=", 1)[1].strip().strip('"')
    p, e = _cpulist(_read("/sys/devices/cpu_core/cpus")), _cpulist(_read("/sys/devices/cpu_atom/cpus"))
    return {"host": socket.gethostname(), "os": os_name or "Linux", "kernel": os.uname().release,
            "cpu_model": model, "threads": os.cpu_count() or 1,
            "hybrid": {"p": p, "e": e} if p and e else None}


# ---- one-shot readings --------------------------------------------------------------

def cpu_times():
    """[(busy, total)] for the whole CPU (index 0) and then each thread."""
    out = []
    for line in (_read("/proc/stat", "") or "").splitlines():
        if not line.startswith("cpu"):
            break
        v = [int(x) for x in line.split()[1:9]]
        idle = v[3] + v[4]  # idle + iowait
        out.append((sum(v) - idle, sum(v)))
    return out


def meminfo():
    m = {}
    for line in (_read("/proc/meminfo", "") or "").splitlines():
        k, _, rest = line.partition(":")
        try:
            m[k] = int(rest.split()[0]) * 1024
        except (IndexError, ValueError):
            pass
    total = m.get("MemTotal")
    if not total:
        return None
    avail = m.get("MemAvailable", m.get("MemFree", 0))
    swap = m.get("SwapTotal", 0)
    return {"total": total, "used": total - avail, "percent": round((total - avail) / total * 100, 1),
            "swap_total": swap, "swap_used": swap - m.get("SwapFree", 0) if swap else 0}


def sensors():
    """Every hwmon temperature and fan, plus the ones the hero headlines."""
    temps, fans = [], []
    for hw in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        chip = _read(hw + "/name") or os.path.basename(hw)
        for inp in sorted(glob.glob(hw + "/temp*_input")):
            base = inp[:-len("_input")]
            v = _int(inp)
            if v is None or v <= -40000:
                continue
            temps.append({"chip": chip, "label": _read(base + "_label") or "",
                          "c": round(v / 1000, 1),
                          "high": _milli(base + "_max"), "crit": _milli(base + "_crit")})
        for inp in sorted(glob.glob(hw + "/fan*_input")):
            v = _int(inp)
            if v is not None:
                fans.append({"chip": chip, "label": _read(inp[:-len("_input")] + "_label") or "", "rpm": v})
    return temps, fans


def _milli(path):
    v = _int(path)
    return round(v / 1000, 1) if v and 0 < v < 150000 else None


def pick_temps(temps):
    def find(chips, label=None):
        for t in temps:
            if t["chip"] in chips and (label is None or t["label"].startswith(label)):
                return t
        return None
    cpu = (find(("coretemp",), "Package") or find(("k10temp", "zenpower"), "Tctl")
           or find(("k10temp", "zenpower", "cpu_thermal", "coretemp", "acpitz")))
    nvme = find(("nvme",), "Composite") or find(("nvme",)) or find(("drivetemp",))
    ram = next((t for t in temps if "DIMM" in t["label"].upper() or t["chip"].startswith(("spd5118", "jc42"))), None)
    return {"cpu": cpu, "nvme": nvme, "ram": ram}


def cpu_freq_mhz():
    vals = [_int(p) for p in glob.glob("/sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_cur_freq")]
    vals = [v for v in vals if v]
    return round(sum(vals) / len(vals) / 1000) if vals else None


def disks():
    """Real filesystems (one per device), fullest first is up to the viewer."""
    seen, out = set(), []
    for line in (_read("/proc/self/mounts", "") or "").splitlines():
        parts = line.split()
        if len(parts) < 3 or parts[2] not in _REAL_FS:
            continue
        dev, mnt = parts[0], parts[1].replace("\\040", " ")
        if dev in seen or mnt.startswith(("/run", "/proc", "/sys", "/dev", "/var/lib/docker", "/snap")):
            continue
        try:
            st = os.statvfs(mnt)
        except OSError:
            continue
        total = st.f_blocks * st.f_frsize
        if not total:
            continue
        seen.add(dev)
        used = total - st.f_bfree * st.f_frsize
        avail = st.f_bavail * st.f_frsize
        out.append({"mount": mnt, "device": os.path.basename(dev), "total": total, "used": used,
                    "percent": round(used / (used + avail) * 100, 1) if used + avail else 0})
    out.sort(key=lambda d: (d["mount"] != "/", d["mount"]))
    return out


def disk_bytes():
    """(read, written) bytes so far, summed over whole disks."""
    r = w = 0
    for line in (_read("/proc/diskstats", "") or "").splitlines():
        f = line.split()
        if len(f) > 9 and _WHOLE_DISK.match(f[2]):
            r += int(f[5]) * 512
            w += int(f[9]) * 512
    return r, w


def net_bytes():
    """(received, sent) bytes so far on physical interfaces."""
    rx = tx = 0
    for line in (_read("/proc/net/dev", "") or "").splitlines()[2:]:
        name, _, rest = line.partition(":")
        if _SKIP_NIC.match(name.strip()):
            continue
        f = rest.split()
        if len(f) >= 9:
            rx += int(f[0])
            tx += int(f[8])
    return rx, tx


def power():
    out = {"battery": None, "status": None, "plugged": None, "charge_limit": None, "charger_w": None}
    for ps in glob.glob("/sys/class/power_supply/*"):
        kind = _read(ps + "/type")
        if kind == "Battery" and _read(ps + "/scope") != "Device":
            out["battery"] = _int(ps + "/capacity")
            out["status"] = _read(ps + "/status")
            out["charge_limit"] = _int(ps + "/charge_control_end_threshold")
        elif kind == "Mains" and _int(ps + "/online") is not None:
            out["plugged"] = bool(_int(ps + "/online")) or bool(out["plugged"])
    # USB-C chargers: negotiated voltage (mV) x current (mA).
    for hw in glob.glob("/sys/class/hwmon/hwmon*"):
        if (_read(hw + "/name") or "").startswith("ucsi_source_psy") and _read(hw + "/device/online", "1") == "1":
            mv, ma = _int(hw + "/in0_input"), _int(hw + "/curr1_input")
            if mv and ma:
                w = round(mv * ma / 1e6)
                if w > 0 and (out["charger_w"] or 0) < w:
                    out["charger_w"] = w
                    out["plugged"] = True
    if out["battery"] is None and out["plugged"] is None:
        return None
    return out


def container_cgroup(pid):
    """The cgroup v2 folder of the container a process runs in, or None."""
    for line in (_read("/proc/%s/cgroup" % pid, "") or "").splitlines():
        if line.startswith("0::"):
            path = line[3:].strip()
            # Docker/podman: system.slice/docker-<id>.scope or docker/<id>; use the
            # container's own folder even if the process sits in a sub-folder.
            m = re.match(r"^(.*?/(?:docker|libpod)[-/][0-9a-f]{12,}(?:\.scope)?)", path)
            return "/sys/fs/cgroup" + (m.group(1) if m else path)
    return None


def container_id(cg):
    m = re.search(r"(?:docker|libpod)[-/]([0-9a-f]{12})", cg or "")
    return m.group(1) if m else None


def cgroup_stats(cg):
    if not cg or not os.path.isdir(cg):
        return None
    usage = None
    for line in (_read(cg + "/cpu.stat", "") or "").splitlines():
        if line.startswith("usage_usec"):
            usage = int(line.split()[1])
    oom = None
    for line in (_read(cg + "/memory.events", "") or "").splitlines():
        if line.startswith("oom_kill "):
            oom = int(line.split()[1])
    limit = _read(cg + "/memory.max")
    return {"cpu_usec": usage, "mem": _int(cg + "/memory.current"),
            "mem_limit": int(limit) if limit and limit.isdigit() else None,
            "oom_kills": oom, "pids": _int(cg + "/pids.current")}


# ---- the sampler ----------------------------------------------------------------------

class Machine:
    def __init__(self, gateway_pids=None):
        self.info = static_info()
        self.gateway_pids = gateway_pids  # callable -> iterable of host pids, for the container
        self.prev = None
        self.history = deque()  # (t, cpu %, cpu temp, mem %)

    def sample(self, now=None):
        now = time.time() if now is None else now
        cur = {"t": now, "cpu": cpu_times(), "disk": disk_bytes(), "net": net_bytes()}
        prev, self.prev = self.prev, cur
        info = self.info
        temps, fans = sensors()
        picked = pick_temps(temps)

        cpu = {"percent": None, "threads": info["threads"], "model": info["cpu_model"],
               "freq_mhz": cpu_freq_mhz(), "temp": _c(picked["cpu"]), "temp_high": _high(picked["cpu"])}
        try:
            cpu["load"] = [round(x, 2) for x in os.getloadavg()]
        except OSError:
            cpu["load"] = None
        dt = now - prev["t"] if prev else 0
        if prev and dt > 0 and len(prev["cpu"]) == len(cur["cpu"]):
            pct = [_pct(a, b) for a, b in zip(prev["cpu"], cur["cpu"])]
            cpu["percent"], cpu["per_thread"] = pct[0], pct[1:]
            if info["hybrid"]:
                for kind in ("p", "e"):
                    vals = [pct[1 + i] for i in info["hybrid"][kind] if i + 1 < len(pct)]
                    cpu[kind + "_percent"] = round(sum(vals) / len(vals), 1) if vals else None
                    cpu[kind + "_threads"] = len(info["hybrid"][kind])

        mem = meminfo()
        if mem:
            mem["temp"] = _c(picked["ram"])
        disk_list = disks()

        out = {
            "host": info["host"], "os": info["os"], "kernel": info["kernel"],
            "uptime_s": _uptime(), "cpu": cpu, "mem": mem,
            "disks": disk_list, "disk_temp": _c(picked["nvme"]), "disk_io": _rates(prev, cur, "disk", dt),
            "net": _rates(prev, cur, "net", dt),
            "fan_rpm": next((f["rpm"] for f in fans if f["rpm"]), fans[0]["rpm"] if fans else None),
            "fans": fans[:6], "temps": temps[:40],
            "power": power(),
            "container": self._container(now, prev),
        }
        self.history.append((round(now), cpu["percent"], cpu["temp"], mem["percent"] if mem else None))
        while self.history and self.history[0][0] < now - HISTORY_S:
            self.history.popleft()
        out["history"] = [list(h) for h in self.history]
        return out

    def _container(self, now, prev):
        try:
            pids = list(self.gateway_pids()) if self.gateway_pids else []
        except Exception:  # noqa: BLE001 - optional extra
            pids = []
        for pid in pids:
            cg = container_cgroup(pid)
            cid = container_id(cg)
            if not cid:
                continue
            stats = cgroup_stats(cg)
            if not stats:
                continue
            last = (prev or {}).get("cg")
            pct = None
            if last and last[0] == cg and stats["cpu_usec"] is not None and now > last[1]:
                pct = round(max(0.0, (stats["cpu_usec"] - last[2]) / ((now - last[1]) * 1e6)
                                / self.info["threads"] * 100), 1)
            self.prev["cg"] = (cg, now, stats["cpu_usec"] or 0)
            return {"id": cid, "cpu_percent": pct, "mem": stats["mem"], "mem_limit": stats["mem_limit"],
                    "oom_kills": stats["oom_kills"], "pids": stats["pids"]}
        return None


def _pct(a, b):
    busy, total = b[0] - a[0], b[1] - a[1]
    return round(max(0.0, min(100.0, busy / total * 100)), 1) if total > 0 else 0.0


def _rates(prev, cur, key, dt):
    if not prev or dt <= 0:
        return None
    a, b = prev[key], cur[key]
    return [round(max(0, (b[0] - a[0]) / dt)), round(max(0, (b[1] - a[1]) / dt))]


def _c(t):
    return t["c"] if t else None


def _high(t):
    return (t.get("high") or t.get("crit")) if t else None


def _uptime():
    try:
        return round(float(_read("/proc/uptime").split()[0]))
    except (AttributeError, ValueError, IndexError):
        return None
