"""Copy a Homebrew command and every library it needs, so it runs anywhere.

ffmpeg is not optional: without it Midify cannot read an MP3 at all. Homebrew's copy reaches into
/opt/homebrew for 18 libraries, and those reach for more, so a straight copy onto a flash drive
would not run on a Mac without Homebrew.

This walks that chain, copies every library beside the command, rewrites each reference to point
inside the folder instead of /opt/homebrew, and signs the results, which Apple Silicon requires
after any change to a binary.
"""
import shutil
import subprocess
import sys
from pathlib import Path

SYSTEM = ("/usr/lib/", "/System/")              # always present on macOS; never copied


def needs(binary):
    """The libraries this binary asks for, ignoring the ones macOS always has."""
    out = subprocess.run(["otool", "-L", str(binary)], capture_output=True, text=True).stdout
    found = []
    for line in out.splitlines()[1:]:
        ref = line.strip().split(" (")[0]
        if not ref or ref.startswith(SYSTEM) or ref.startswith("@"):
            continue
        found.append(ref)
    return found


def collect(binary, seen=None):
    """Every library needed, directly or through another library.

    Kept under the name the binary asks for, not the name on disk. Homebrew points
    libavdevice.62.dylib at a file called libavdevice.62.3.101.dylib; copying it under the real
    name leaves the binary asking for a name that is not there, and it quietly keeps using
    Homebrew's copy instead."""
    seen = {} if seen is None else seen
    for ref in needs(binary):
        asked = Path(ref).name
        real = Path(ref).resolve()
        if not real.exists() or asked in seen:
            continue
        seen[asked] = real
        collect(real, seen)
    return seen


def sign(path):
    subprocess.run(["codesign", "--force", "--sign", "-", "--timestamp=none", str(path)],
                   capture_output=True)


def bundle(tool, dest_root, say=print):
    """Put `tool` and its libraries under dest_root/bin and dest_root/lib. Returns the new path."""
    source = Path(shutil.which(tool) or "")
    if not source.exists():
        raise FileNotFoundError(f"{tool} is not installed here, so it cannot be bundled")
    source = source.resolve()
    bin_dir, lib_dir = Path(dest_root) / "bin", Path(dest_root) / "lib"
    bin_dir.mkdir(parents=True, exist_ok=True)
    lib_dir.mkdir(parents=True, exist_ok=True)

    libraries = collect(source)
    say(f"  {tool}: {len(libraries)} libraries to travel with it")
    for asked, real in libraries.items():
        target = lib_dir / asked                       # under the name the binary asks for
        if not target.exists():
            shutil.copy2(real, target)

    placed = bin_dir / source.name
    shutil.copy2(source, placed)

    def repoint(binary, prefix):
        for ref in needs(binary):
            name = Path(ref).name
            if name in libraries or (lib_dir / name).exists():
                subprocess.run(["install_name_tool", "-change", ref, f"{prefix}/{name}", str(binary)],
                               capture_output=True)
        sign(binary)

    for name in libraries:
        lib = lib_dir / name
        subprocess.run(["install_name_tool", "-id", f"@loader_path/{name}", str(lib)], capture_output=True)
        repoint(lib, "@loader_path")
    repoint(placed, "@executable_path/../lib")
    return placed


def check(binary, say=print):
    """Prove it no longer reaches outside its own folder, and that it still runs."""
    left = [r for r in needs(binary) if r.startswith(("/opt/", "/usr/local/"))]
    runs = subprocess.run([str(binary), "--version"], capture_output=True, text=True)
    said = (runs.stdout or runs.stderr)
    first = said.splitlines()[:1]
    # ffmpeg prints its version and then exits unhappy about the double dash, so take the words
    # it printed as the proof that it loaded and ran.
    ok = not left and ("version" in said.lower() or runs.returncode == 0)
    say(f"  {Path(binary).name}: {'travels on its own' if not left else 'STILL NEEDS ' + ', '.join(left[:3])}"
        f", {'runs' if ok or 'version' in said.lower() else 'DOES NOT RUN'}"
        + (f" ({first[0][:48]})" if first else ""))
    return ok


if __name__ == "__main__":
    dest = Path(sys.argv[1] if len(sys.argv) > 1 else "./portable-tools")
    good = True
    for tool in ("ffmpeg", "fluidsynth"):
        try:
            placed = bundle(tool, dest)
            good = check(placed) and good
        except FileNotFoundError as e:
            print(f"  {e}")
            good = False
    total = sum(f.stat().st_size for f in Path(dest).rglob("*") if f.is_file()) / 1e6
    print(f"  altogether {total:.0f} MB")
    sys.exit(0 if good else 1)
