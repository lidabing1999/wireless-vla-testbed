"""Install dependencies and prepare pinned source trees."""
import os
import platform
import re
import shutil
from pathlib import Path

from common import ROOT, RUNTIME, command, fetch, lock, output, status, sudo, write_file

PACKAGES = {
    "core": ["ca-certificates", "curl", "iproute2", "iptables", "kmod"],
    "edge": ["ca-certificates", "curl", "git", "git-lfs", "gnupg"],
    "ran": ["autoconf", "automake", "bison", "build-essential", "ca-certificates", "ccache",
            "cmake", "curl", "flex", "gcc-12", "g++-12", "git", "iproute2", "iptables",
            "iperf3", "kmod", "libboost-all-dev", "libcap-dev", "libconfig-dev", "libfftw3-dev", "libgmp-dev",
            "libnuma-dev", "libreadline-dev", "libsctp-dev", "libssl-dev", "libtool",
            "libyaml-cpp-dev", "ninja-build", "pkg-config", "python3", "zlib1g-dev"],
    "sim": ["build-essential", "ca-certificates", "curl", "ffmpeg", "git", "libegl1", "libegl-mesa0",
            "libgl1", "libglib2.0-0", "libglfw3", "libosmesa6", "python3.10", "python3.10-dev",
            "python3.10-venv"],
}


def require_linux():
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        raise ValueError("prepare and deployment target Linux x86_64")


def system_deps(*roles):
    require_linux()
    command(*sudo(["apt-get", "update"]))
    packages = sorted({package for role in roles for package in PACKAGES[role]})
    command(*sudo(["apt-get", "install", "-y", *packages]))


def prepare_asn1c(jobs):
    if override := os.environ.get("VLA_ASN1C"):
        executable = Path(override).expanduser().resolve(strict=True)
        if not os.access(executable, os.X_OK):
            raise ValueError(f"ASN.1 compiler is not executable: {executable}")
        return executable
    source = fetch("asn1c")
    revision = output("git", "-C", source, "rev-parse", "HEAD")
    prefix = RUNTIME / "ran/asn1c"
    build = RUNTIME / "ran" / f"asn1c-build-{revision[:12]}"
    executable = prefix / "bin/asn1c"
    stamp = prefix / ".revision"
    if not executable.is_file() or not stamp.is_file() or stamp.read_text().strip() != revision:
        # Run autotools against a build copy to keep the Git submodule clean.
        shutil.copytree(source, build, dirs_exist_ok=True, ignore=shutil.ignore_patterns(".git"))
        command("autoreconf", "-iv", cwd=build)
        command(build / "configure", f"--prefix={prefix}", cwd=build,
                env=os.environ | {"CFLAGS": "-O2 -fno-strict-aliasing"})
        command("make", "-j", jobs, cwd=build)
        command("make", "install", cwd=build)
        write_file(stamp, revision + "\n")
    return executable


def prepare_ran(jobs):
    with lock(RUNTIME / "locks/ran-prepare"):
        source, simde = fetch("oai"), fetch("simde")
        asn1c = prepare_asn1c(jobs)
        build = RUNTIME / "ran/build"
        env = os.environ | {"CC": os.environ.get("CC", "gcc-12"),
                            "CXX": os.environ.get("CXX", "g++-12")}
        cmake_flags = [
            "-DCMAKE_BUILD_TYPE=RelWithDebInfo", "-DE2_AGENT=OFF", "-DENABLE_TESTS=OFF",
            "-DOAI_VRTSIM=ON", "-DOAI_SIMU=ON", "-DOAI_USRP=OFF", "-DOAI_VRTSIM_TAPS_CLIENT=OFF",
            "-DAVX512=OFF", "-DAUTO_DOWNLOAD_ASN1C=OFF", "-DENABLE_TELNETSRV=OFF",
            f"-DASN1C_EXEC={asn1c}",
            f"-DCMAKE_C_FLAGS=-I{simde.parent}",
            f"-DCMAKE_CXX_FLAGS=-I{simde.parent}",
        ]
        command("cmake", "-S", source, "-B", build, "-G", "Ninja", *cmake_flags, env=env)
        command("cmake", "--build", build, "--parallel", jobs, "--target",
                "nr-softmodem", "nr-uesoftmodem", "vrtsim", "params_yaml", "params_libconfig",
                "ldpc", "ldpc_orig", "coding", "dfts")
        files = ("nr-softmodem", "nr-uesoftmodem", "libvrtsim.so", "libparams_yaml.so",
                 "libparams_libconfig.so", "libldpc.so", "libldpc_orig.so", "libcoding.so", "libdfts.so")
        for file in files:
            if not (build / file).is_file():
                raise ValueError(f"build did not produce {build / file}")


def prepare_sim():
    with lock(RUNTIME / "locks/sim-prepare"):
        fetch("libero")
        openpi = fetch("openpi")
        env = RUNTIME / "sim/venv"
        python = env / "bin/python"
        interpreter = os.environ.get("VLA_SIM_PYTHON", "python3.10")
        if output(interpreter, "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')") != "3.10":
            raise ValueError("simulation environment requires CPython 3.10")
        if not python.is_file() or status(str(python), "-m", "pip", "--version"):
            command(interpreter, "-m", "venv", env)
        command(python, "-m", "pip", "install", "--retries", "6", "pip==25.1.1")
        command(python, "-m", "pip", "install", "--retries", "6", "--no-deps",
                "--index-url", os.environ.get("VLA_TORCH_INDEX", "https://download.pytorch.org/whl/cpu"),
                "torch==2.2.0+cpu")
        command(python, "-m", "pip", "install", "--retries", "6", "-r", ROOT / "sim/requirements.lock")
        command(python, "-m", "pip", "install", "--no-deps", "-e", openpi / "packages/openpi-client")
        command(python, "-m", "pip", "check")


def stage_edge():
    context = RUNTIME / "edge/context"
    with lock(RUNTIME / "locks/edge-context"):
        upstream = {"openpi": fetch("openpi"), "lerobot": fetch("lerobot")}
        context.mkdir(parents=True, exist_ok=True)
        for name, source in upstream.items():
            dest = context / name
            patterns = (".git", "__pycache__", "*.pyc", ".venv")
            if name == "openpi":
                patterns += ("third_party",)
            shutil.copytree(source, dest, dirs_exist_ok=True, ignore=shutil.ignore_patterns(*patterns))
        for name in ("Dockerfile", "requirements.lock", "resolve_requirements.py"):
            shutil.copy2(ROOT / "edge" / name, context / name)
        wheel_dir = context / "wheels"
        wheel_dir.mkdir(exist_ok=True)
        (wheel_dir / ".keep").touch()
        if cache := os.environ.get("VLA_POLICY_WHEELHOUSE"):
            cache = Path(cache).expanduser().resolve(strict=True)
            requirements = (context / "requirements.lock").read_text()
            filenames = re.findall(r"https://files\.pythonhosted\.org/[^\s]+/([^/\s]+\.whl)", requirements)
            for filename in filenames:
                source = cache / filename
                dest = wheel_dir / filename
                if source.is_file() and not dest.exists():
                    try:
                        os.link(source, dest)
                    except OSError:
                        shutil.copyfile(source, dest)
    return context
