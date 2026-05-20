import argparse
import platform
import shutil
import subprocess
import sys
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

SYSTEM = platform.system()

def sudo_write(path: Path, value: str) -> None:
    """Write value to sysfs path via sudo tee and suppressing echo"""
    subprocess.run(
        ["sudo", "tee", str(path)],
        input=value, text=True, check=True, stdout=subprocess.DEVNULL,
    )


def is_windows_admin() -> bool:
    if SYSTEM != "Windows":
        return False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


class EnvironmentSetup(ABC):
    def __enter__(self):
        try:
            self.setup()
        except BaseException:
            self.restore()
            raise
        return self

    def __exit__(self, *_):
        self.restore()
    
    @abstractmethod
    def setup(self) -> None: ...

    @abstractmethod
    def restore(self) -> None: ...


class LinuxEnvironmentSetup(EnvironmentSetup):
    """cpu performance governor, turbo disable, smt disable, cset, aslr disable, tmpfs, stop noisy services"""
    BEGIN = [
        "snapd", "apt-daily.timer", "apt-daily-upgrade.timer",
        "apt-daily.service", "apt-daily-upgrade.service", "unattended-updates"
    ]
    END = ["snapd", "apt-daily.timer", "apt-daily-upgrade.timer", "unattended-upgrades"]

    def __init__(self, benchmark_cpus : str, test_path: Path, bin_dir: Path) -> None:
        self.benchmark_cpus = benchmark_cpus
        self.saved_governor = "powersave"
        self.turbo_path: Optional[Path] = None
        self.saved_turbo: Optional[str] = None
        self.smt_path = Path("/sys/devices/system/cpu/smt/control")
        self.saved_smt : Optional[str] = None
        self.aslr_path = Path("/proc/sys/kernel/randomize_va_space")
        self.saved_aslr : Optional[str] = None
        self.test_path = test_path
        self.bin_dir = bin_dir

    def setup(self) -> None:
        gov_path = Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor")
        if gov_path.exists():
            self.saved_governor = gov_path.read_text().strip()
        subprocess.run(
            ["sudo", "cpupower", "frequency-set", "-g", "performance"],
            check=True,
        )
        # TODO: disable boost mode for AMD also
        # Skipping for now, as we currently don't have access to AMD CPU
        # https://www.kernel.org/doc/html/latest/admin-guide/pm/cpufreq.html#the-boost-file-in-sysfs
        intel = Path("/sys/devices/system/cpu/intel_pstate/no_turbo")
        if intel.exists():
            self.turbo_path, turbo_disable, self.saved_turbo = intel, "1", "0"
        # Disable turbo
        if self.turbo_path and turbo_disable:
            try:
                sudo_write(self.turbo_path, turbo_disable)
            except subprocess.CalledProcessError:
                print(f"WARN: could not write {self.turbo_path}")
        # Stop noisy services
        subprocess.run(["sudo", "systemctl", "stop", *self.BEGIN], check=False)
        # Shield cpus
        if shutil.which("cset"):
            subprocess.run(["sudo", "cset", "shield", "-c", self.benchmark_cpus, "-k", "on"])
        # Disable SMT
        if self.smt_path.exists():
            self.saved_smt = self.smt_path.read_text().strip()
            if self.saved_smt == "on":
                try:
                    sudo_write(self.smt_path, "off")
                except subprocess.CalledProcessError:
                    print(f"WARN: could not write {self.smt_path}")
        # Disable ASLR
        if self.aslr_path.exists():
            self.saved_aslr = self.aslr_path.read_text().strip()
            try:
                sudo_write(self.aslr_path, "0")
            except subprocess.CalledProcessError:
                print(f"WARN: could not write to {self.aslr_path}")
        print("=== Linux env: performance governor, turbo off, services stopped")
        if shutil.which("cset"):
            print(f"=== LInux env: cset shield active on cpus {self.benchmark_cpus} ===")

    def restore(self) -> None:
        # Restore SMT
        if self.smt_path.exists() and self.saved_smt:
            try:
                sudo_write(self.smt_path, self.saved_smt)
            except subprocess.CalledProcessError:
                print(f"WARN: could not write {self.smt_path}")
        # Disable cpu shield
        if shutil.which("cset"):
            subprocess.run(["sudo", "cset", "shield", "--reset"], check=False)
        # Restore cpu governor
        subprocess.run(
            ["sudo", "cpupower", "frequency-set", "-g", self.saved_governor],
            check=True,
        )
        # Restore turbo
        # TODO: restore boost mode for AMD here
        if self.turbo_path and self.saved_turbo:
            try:
                sudo_write(self.turbo_path, self.saved_turbo)
            except subprocess.CalledProcessError:
                print(f"WARN: could not write {self.turbo_path}")
        # Restore ASLR
        if self.aslr_path.exists() and self.saved_aslr:
            try:
                sudo_write(self.aslr_path, self.saved_aslr)
            except subprocess.CalledProcessError:
                print(f"WARN: could not write {self.aslr_path}")
        # Restart stopped services
        subprocess.run(["sudo", "systemctl", "start", *self.END], check=False)
        print("=== Linux env: restored ===")


class MacEnvironmentSetup(EnvironmentSetup):
    """caffeinate -dimsu keeps the system awake for the duration of benchmarking"""
    def setup(self) -> None:
        self.proc = subprocess.Popen(["caffeinate", "-dimsu"])
        print("=== Mac env: caffeinate started")

    def restore(self) -> None:
        self.proc.terminate()
        self.proc.wait()
        print("=== Mac env: caffeinate stopped ===")
    
class WindowsEnvironmentSetup(EnvironmentSetup):
    """Ultimate/high performance plan, defender exclusions, services stopped"""
    END = ["SysMain", "WSearch", "DoSvc", "BITS"]

    def __init__(self, repo_root: Path) -> None:
        self.winmm = None
        self.repo_root = repo_root

    def setup(self) -> None:
        if not is_windows_admin():
            print("WARN: not running as Administrator, env setup may partially fail")
        try:
            subprocess.run(["powercfg", "/changename", "SCHEME_MIN", "Ultimate Performance"], check=True)
            subprocess.run(["powercfg", "/setactive", "SCHEME_MIN"], check=True)
        except subprocess.CalledProcessError:
            try:
                subprocess.run(["powercfg", "/setactive", "SCHEME_MIN"])
            except subprocess.CalledProcessError as e:
                pass
        for cmd in [
            ["powercfg", "/setacvalueindex", "SCHEME_CURRENT", "SUB_PROCESSOR", "PROCTHROTTLEMIN", "100"],
            ["powercfg", "/setacvalueindex", "SCHEME_CURRENT", "SUB_PROCESSOR", "PROCTHROTTLEMAX", "100"],
            ["powercfg", "/setacvalueindex", "SCHEME_CURRENT", "SUB_PROCESSOR", "PERFBOOSTMODE", "0"],
            ["powercfg", "/setacvalueindex", "SCHEME_CURRENT", "SUB_PROCESSOR", "CPMINCORES", "100"],
            ["powercfg", "/setactive", "SCHEME_CURRENT"],
        ]:
            try:
                subprocess.run(cmd, check=True)
            except subprocess.CalledProcessError as e:
                print(f"WARN: {e}")
        try:
            subprocess.run(["powershell", "-Command", f"Add-MpPreference -ExclusionPath '{self.repo_root}'"], check=True)
        except subprocess.CalledProcessError as e:
            print(f"WARN: could not add Defender exclusion: {e}")
        for svc in self.END:
            subprocess.run(["sc", "stop", svc], capture_output=True)
        try:
            import ctypes
            self.winmm = ctypes.WinDLL("winmm")
            self.winmm.timeBeginPeriod(1)
        except Exception as e:
            print(f"WARN: could not set 1ms timer resolution: {e}")
        print("=== Windows env: Performance plan, Defender exclusions, services stopped, 1ms timer ===")
    
    def restore(self) -> None:
        for cmd in [
            ["powercfg", "/setacvalueindex", "SCHEME_CURRENT", "SUB_PROCESSOR", "PERFBOOSTMODE", "1"],
            ["powercfg", "/setacvalueindex", "SCHEME_CURRENT", "SUB_PROCESSOR", "PROCTHROTTLEMIN", "5"],
            ["powercfg", "/setacvalueindex", "SCHEME_CURRENT", "SUB_PROCESSOR", "PROCTHROTTLEMAX", "100"],
            ["powercfg", "/setacvalueindex", "SCHEME_CURRENT", "SUB_PROCESSOR", "CPMINCORES", "0"],
            ["powercfg", "/setactive", "SCHEME_CURRENT"],
            ["powercfg", "/setactive", "SCHEME_BALANCED"],
        ]:
            subprocess.run(cmd, capture_output=True)
        if is_windows_admin():
            try:
                subprocess.run(["powershell", "-Command", f"Remove-MpPreference -ExclusionPath '{self.repo_root}'"], check=True)
            except subprocess.CalledProcessError:
                pass
        for svc in self.END:
            subprocess.run(["sc", "start", svc], capture_output=True)
        if self.winmm:
            try:
                self.winmm.timeEndPeriod(1)
            except Exception:
                pass
        print("=== Windows env: restored ===")


class NullEnvironmentSetup(EnvironmentSetup):
    def setup(self) -> None: pass
    def restore(self) -> None: pass


def make_env(args) -> EnvironmentSetup:
    if args.skip_env_setup:
        return NullEnvironmentSetup()
    if SYSTEM=="Linux":
        # |--llvm-project
        # |----llvm
        # |--build
        repo_root = Path(args.repo_root).resolve() if args.repo_root else Path.cwd().parents[4]
        build_dir = Path(args.build_dir).resolve() if args.build_dir else repo_root / "build"
        benchmark_cpus = args.benchmark_cpus or "2,4,6,8"
        return LinuxEnvironmentSetup(
            benchmark_cpus=benchmark_cpus,
            test_path=repo_root/Path(args.test_path),
            bin_dir=build_dir/"bin"
        )
    if SYSTEM=="Darwin":
        return MacEnvironmentSetup()
    if SYSTEM=="Windows":
        repo_root = Path(args.repo_root).resolve() if args.repo_root else Path.cwd()
        return WindowsEnvironmentSetup(repo_root=repo_root)
    return NullEnvironmentSetup()


class BenchmarkRunner(ABC):
    """Platform-specific hyperfine execution wrapper"""
    def run(
            self, lit: Path, test_path: Path, workers: int,
            warmup: int, runs: int, results_file: Path
    ) -> None:
        cmd_str = self.lit_cmd_str(lit, test_path, workers)
        hyp = self.build_hyperfine_cmd(cmd_str, warmup, runs, results_file)
        print(f"=== Benchmarking: {test_path} (j{workers}) ===")
        subprocess.run(self.wrap_command(hyp), check=True)

    def build_hyperfine_cmd(
            self, cmd_str: str, warmup: int, runs: int, results_file: Path
    ) -> list:
        return [
            "hyperfine",
            "--ignore-failure",
            "--warmup", str(warmup),
            "--runs", str(runs),
            "--export-markdown", str(results_file),
            cmd_str
        ]

    def lit_cmd_str(self, lit: Path, test_path: Path, workers: int) -> str:
        if SYSTEM=="Windows":
            return f"python '{lit}' '{test_path}' -j{workers} --no-progress-bar"
        return f"'{lit}' '{test_path}' -j{workers} --no-progress-bar"

    @abstractmethod
    def wrap_command(self, cmd: list) -> list: ...


class LinuxBenchmarkRunner(BenchmarkRunner):
    def __init__(self, benchmark_cpus: str, use_cset: bool) -> None:
        self.cpus = benchmark_cpus
        self.use_cset = use_cset

    def wrap_command(self, cmd: list) -> list:
        if self.use_cset:
            return ["sudo", "cset", "shield", "--exec", "--"] + cmd
        return ["sudo", "nice", "-n", "-20", "taskset", "-c", self.cpus] + cmd
    

class MacBenchmarkRunner(BenchmarkRunner):
    def wrap_command(self, cmd: list) -> list:
        return cmd # TODO: use nice(??)


class WindowsBenchmarkRunner(BenchmarkRunner):
    def __init__(self, affinity_mask: str = "FFF") -> None:
        self.affinity_mask = affinity_mask

    def wrap_command(self, cmd: list) -> list:
        inner = " ".join(str(c) for c in cmd)
        return ["cmd", "/c", f"start '' /B /Wait /High /Affinity {self.affinity_mask} {inner}"]


def make_runner(args, cset_available: bool) -> BenchmarkRunner:
    if SYSTEM=="Linux":
        benchmark_cpus = args.benchmark_cpus or "2,4,6,8"
        if not cset_available:
            print("WARN: cset not found; using taskset (install: sudo apt install cpuset)")
        return LinuxBenchmarkRunner(benchmark_cpus, use_cset=cset_available)
    if SYSTEM=="Darwin":
        return MacBenchmarkRunner()
    if SYSTEM=="Windows":
        return WindowsBenchmarkRunner(affinity_mask=args.affinity_mask)


def cmake_cmd(build_dir: Path, llvm_src: Path, compiler_rt: bool) -> list:
    runtimes = "compiler-rt" if compiler_rt else ""
    targets = "X86;Aarch64" if (SYSTEM=="Darwin" and platform.machine()=="arm64") else "X86"
    cmd = [
        "cmake", "-S", str(llvm_src), "-B", str(build_dir),
        "-G", "Ninja",
        "-DCMAKE_BUILD_TYPE=Release",
        f"-DLLVM_TARGETS_TO_BUILD={targets}",
        f"-DLLVM_ENABLE_PROJECTS={'clang' if compiler_rt else ''}",
        f"-DLLVM_ENABLE_RUNTIMES={runtimes}",
        "-DLLVM_INCLUDE_TESTS=ON",
        "-DLLVM_BUILD_TESTS=OFF",
        "-DLLVM_ENABLE_ASSERTIONS=OFF"
    ]
    if compiler_rt:
        cmd.append("-DCOMPILER_RT_BUILD_SANITIZERS=ON")
    if SYSTEM=="Linux":
        cmd += ["-DCMAKE_C_COMPILER=clang", "-DCMAKE_CXX_COMPILER=clang++", "-DLLVM_ENABLE_LLD=ON"]
    elif SYSTEM=="Windows":
        cmd += ["-DCMAKE_C_COMPILER=cl", "-DCMAKE_CXX_COMPILER=cl"]
    return cmd

def build(build_dir: Path, llvm_src: Path, compiler_rt: bool, workers: int) -> None:
    if (build_dir / "build.ninja").exists():
        print(f"=== Build already configured (delete {build_dir} to reconfigure) ===")
    else:
        print("=== Configuring build ===")
        subprocess.run(cmake_cmd(build_dir, llvm_src, compiler_rt), check=True)
    print(f"=== Building tools (-j{workers}) ===")
    ninja_targets = ["llvm-test-depends"]
    if compiler_rt:
        ninja_targets = ["clang", "llvm-test-depends", "runtimes"]
    for target in ninja_targets:
        subprocess.run(["ninja", "-C", str(build_dir), f"-j{workers}", target], check=True)
    llc = build_dir / "bin" / ("llc.exe" if SYSTEM == "Windows" else "llc")
    if llc.exists():
        r = subprocess.run([str(llc), "--version"], capture_output=True, text=True)
        if r.stdout:
            print(r.stdout.splitlines()[0])
    lit = lit_path(build_dir)
    subprocess.run([sys.executable, str(lit), "--version"] if SYSTEM == "Windows" else [str(lit), "--version"], check=True)


def lit_path(build_dir: Path) -> Path:
    return build_dir / "bin" / "llvm-lit"

def check_tools(skip_build: bool) -> None:
    needed = ["hyperfine"]
    if not skip_build:
        needed += ["cmake", "ninja"]
        if SYSTEM=="Linux":
            needed.append("clang")
    missing = [t for t in needed if not shutil.which(t)]
    if missing:
        sys.exit(f"ERROR: missing required dtools: {', '.join(missing)}")
    if SYSTEM=="Windows" and not skip_build and not shutil.which("cl"):
        print("WARN: cl.exe not found; must run from x64 Native Tools Command Prompt")

def os_defaults() -> Tuple[int, int]:
    """(warmup, runs)"""
    return 5, 10

def main() -> None:
    default_warmup, default_runs = os_defaults()
    parser = argparse.ArgumentParser(
        description="lit benchmarking script",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=("Examples:\n"
                "\tpython benchmark.py --test-path llvm-project/llvm/test/CodeGen/X86 --workers 4\n"
                "\tpython benchmark.py --test-path llvm-project/llvm/test/CodeGen/X86 --workers 4 --label baseline\n"
                "\tpython benchmark.py --test-path llvm-project/llvm/test/CodeGen/X86 --workers 4 --skip-build\n"
                f"\nPlatform: {SYSTEM} Defaults: --warmup {default_warmup} --runs {default_runs}")
    )
    parser.add_argument(
        "--test-path", required=True, metavar="PATH",
        help="Lit test directory, relative to --repo-root",
    )
    parser.add_argument(
        "--workers", type=int, default=4, metavar="N",
        help="Parallel lit workers (default: 4)",
    )
    parser.add_argument(
        "--label", default="run", metavar="STR",
        help='Results directory suffix; use "baseline" before changes (default: run)',
    )
    parser.add_argument(
        "--warmup", type=int, default=default_warmup, metavar="N",
        help=f"Hyperfine warmup runs (default: {default_warmup} on {SYSTEM})",
    )
    parser.add_argument(
        "--runs", type=int, default=default_runs, metavar="N",
        help=f"Hyperfine benchmark runs (default: {default_runs} on {SYSTEM})",
    )
    parser.add_argument(
        "--build-dir", default=None, metavar="PATH",
        help="Build directory (default: <repo-root>/build)",
    )
    parser.add_argument(
        "--repo-root", default=None, metavar="PATH",
        help="Repo root; must contain llvm-project/ (default: cwd)",
    )
    parser.add_argument(
        "--compiler-rt", action="store_true",
        help="Include compiler-rt in build via LLVM_ENABLE_RUNTIMES (needed for asan benchmark)",
    )
    parser.add_argument(
        "--skip-build", action="store_true",
        help="Skip cmake/ninja; assume build/ dir is already built",
    )
    parser.add_argument(
        "--skip-env-setup", action="store_true",
        help="Skip CPU scaling and service changes",
    )
    parser.add_argument(
        "--benchmark-cpus", default=None, metavar="RANGE",
        help="Linux: CPU range for taskset/cset shield (default: '2,4,6,8')",
    )
    parser.add_argument(
        "--affinity-mask", default="FFF", metavar="HEX",
        help="Windows: CPU affinity mask in hex (default: 'FFF' for P-cores 0-11)",
    )
    args = parser.parse_args()
    repo_root = Path(args.repo_root).resolve() if args.repo_root else Path.cwd().parents[4]
    build_dir = Path(args.build_dir).resolve() if args.build_dir else repo_root / "build"
    llvm_src = repo_root / "llvm-project" / "llvm"
    test_path = repo_root / args.test_path
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    results_dir = repo_root / "results" / f"{timestamp}-{args.label}"
    check_tools(args.skip_build)
    if not args.skip_build:
        if not llvm_src.exists():
            sys.exit(f"ERROR: llvm source not found at {llvm_src}\n")
        build(build_dir, llvm_src, args.compiler_rt, args.workers)
    lit = lit_path(build_dir)
    if args.skip_build and not lit.exists():
        sys.exit(f"ERROR: lit binary not found at {lit}. Build first and check --build-dir.")
    results_dir.mkdir(parents=True, exist_ok=True)
    cset_available = SYSTEM=="Linux" and bool(shutil.which("cset"))
    runner = make_runner(args, cset_available)
    with make_env(args):
        runner.run(
            lit, test_path, args.workers,
            args.warmup, args.runs,
            results_dir/"hyperfine.md"
        )
    print("\n=== Done ===")
    print(f"Results: {results_dir}")
    for f in sorted(results_dir.iterdir()):
        print(f"\t{f.name}")
    
if __name__=="__main__":
    main()
