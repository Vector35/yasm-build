#!/usr/bin/env python3

import argparse
import os
import shlex
import shutil
import subprocess
import sys
import zipfile
import glob

from math import ceil
from pathlib import Path

msvc_build = "14.28"
vs_version = "2019"
yasm_version = '1.3.0'


def remove_dir(path: os.PathLike):
    if sys.platform == 'win32':
        # Windows being Windows. Not doing this as a recursive delete from the shell will yield
        # "access denied" errors. Even deleting the individual files from the terminal does this.
        # Somehow, deleting this way works correctly.
        subprocess.call(f'rmdir /S /Q "{path}"', shell=True)
    else:
        shutil.rmtree(path)


def keychain_unlocker():
    keychain_unlocker = os.environ["HOME"] + "/unlock-keychain"
    if os.path.exists(keychain_unlocker):
        return subprocess.call([keychain_unlocker]) == 0
    return False


def mac_sign(path, deep=True, force=False):
    if not keychain_unlocker():
        return False

    args = ["codesign"]
    if deep:
        args += ["--deep"]
    if force:
        args += ["-f"]
    args += ["--options", "runtime", "--timestamp", "-s", "Developer ID"]
    if path.endswith(".dmg"):
        args.append(path)
    else:
        for f in glob.glob(path):
            args.append(f)
    return subprocess.call(args) == 0


def signWindowsFiles(path):
    timeServers = [r"http://timestamp.digicert.com", r"http://timestamp.comodoca.com/rfc3161"]
    signTool = r"c:\Program Files (x86)\Windows Kits\10\bin\10.0.18362.0\x64\signtool.exe"
    signingCert = os.path.expandvars(r"%USERPROFILE%\signingcerts\codesign.pfx")
    for timeServer in timeServers:
        proc = subprocess.run(
            [signTool, "sign", "/fd", "sha256", "/f", signingCert, "/tr", timeServer, "/td", "sha256", path],
            shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if proc.returncode == 0:
            print("Signed {}".format(path))
            return True
        else:
            print("Signing {} with timeserver: {} failed. Trying next server. {}".format(path, timeServer,
                                                                                         proc.stdout.decode('charmap')))
    print("Failed to sign file %s" % path)
    return False


parser = argparse.ArgumentParser(description="Build and install yasm")
parser.add_argument("--no-clone", help="skip cloning the yasm source code",
                    action="store_true")
parser.add_argument("--no-clean", dest='clean', action='store_false', default=True, help="Don't clean before building")
parser.add_argument("--no-prompt", dest='prompt', action='store_false', default=True, help="Don't wait for user prompt")
parser.add_argument("--no-install", dest='install', action='store_false', default=True, help="Don't install build products to your home folder")
parser.add_argument("--patch", action='append', help="patch the source before building")
parser.add_argument("--universal", help="build for both x86_64 and arm64 (arm64 Mac host only)", action="store_true")
parser.add_argument("--sign", dest='sign', help="sign all executables", action="store_true")

if not sys.platform.startswith("win"):
    parser.add_argument("-j", "--jobs", dest='jobs', default=ceil(os.cpu_count() * 1.1),
                        help="Number of build threads (Defaults to 1.1*cpu_count)")

args = parser.parse_args()

if sys.platform.startswith("win"):
    make_cmd = "ninja"
    parallel = []
    cmake_generator_array = ["-G", "Ninja"]

    # Import vcvars from Visual Studio
    vcvars = subprocess.check_output(
        fR"""call "C:\Program Files (x86)\Microsoft Visual Studio\{vs_version}\Professional\VC\Auxiliary\Build\vcvars64.bat" -vcvars_ver={msvc_build} && set""",
        shell=True)
    for line in vcvars.split(b'\r\n'):
        line = line.strip()
        if b'=' not in line:
            continue
        parts = line.split(b'=')
        key = parts[0].decode()
        value = b'='.join(parts[1:]).decode()
        os.environ[key] = value
else:
    make_cmd = "ninja"
    parallel = ["-j", str(args.jobs)]
    cmake_generator_array = ["-G", "Ninja"]

sysroot = None
if sys.platform == 'darwin':
    if Path('/Library/Developer/CommandLineTools/SDKs/MacOSX.sdk').exists():
        if Path('/Applications/Xcode.app').exists():
            print(
                "!! Xcode and CommandLineTools both installed. Defaulting to CommandLineTools but the build may fail.")
        sysroot = '/Library/Developer/CommandLineTools/SDKs/MacOSX.sdk'
    else:
        sysroot = subprocess.check_output(['xcode-select', '-p']).decode().strip()
        sysroot += '/Platforms/MacOSX.platform/Developer/SDKs/MacOSX.sdk'

    print("Sysroot is                   " + sysroot)

base_dir = Path(__file__).resolve().parent
yasm_dir = base_dir / "build"
source_path = yasm_dir / "src"
build_path = source_path / "build"
artifact_path = base_dir / "artifacts"
install_path = yasm_dir / "install" / "yasm" / yasm_version
patches_path = base_dir / 'patches'

print(f"Build path will be           {build_path}")
print(f"Build products path will be  {install_path}")

print(f"Clean build directory:       {'YES' if args.clean else 'NO'}")
print(f"Universal build:             {'YES' if args.universal else 'NO'}")
print(f"Install to home directory:   {'YES' if args.install else 'NO'}")
print(f"Codesigning:                 {'YES' if args.sign else 'NO'}")
print("")

patches = []
for patch in sorted(patches_path.iterdir()):
    if patch.suffix == '.patch':
        resolved_path = patch.resolve()
        patches.append(patch.resolve())

for patch in patches:
    print(f"Apply patch: {patch}")

if args.prompt and input("\nIs this correct (y/n)? ") != "y":
    print("Aborted")
    sys.exit(1)

if not artifact_path.exists():
    artifact_path.mkdir(parents=True)

if args.clean:
    # Clean existing files
    for f in artifact_path.glob('*'):
        f.unlink()

    if build_path.exists():
        remove_dir(build_path)

    if (base_dir / "CMakeCache.txt").exists():
        (base_dir / "CMakeCache.txt").unlink()

if install_path.exists():
    if args.prompt and input("\nAn install already exists at the target location. Overwrite? ") != "y":
        print("Aborted")
        sys.exit(1)

if not args.no_clone:
    print("\nCloning yasm...")
    if not yasm_dir.exists():
        yasm_dir.mkdir()
    if source_path.exists():
        remove_dir(source_path)
    if subprocess.call(
            ["git", "clone", "https://github.com/yasm/yasm", "--branch", f"v{yasm_version}",
             "--depth", "1", source_path]) != 0:
        print("Failed to clone yasm git repository")
        sys.exit(1)
    if subprocess.call(["git", "checkout", f"v{yasm_version}"], cwd=source_path) != 0:
        print(f"Failed to check out tag 'v{yasm_version}'")
        sys.exit(1)

    for patch in patches:
        print(f"Applying patch {patch}...")
        if subprocess.call(["git", "apply", patch], cwd=source_path) != 0:
            print("Failed to patch source")
            sys.exit(1)

print("\nConfiguring yasm...")
if not build_path.exists():
    build_path.mkdir()

cmake_params = []
cmake_params.append(('CMAKE_BUILD_TYPE', 'Release'))
cmake_params.append(('BUILD_SHARED_LIBS', 'OFF'))
cmake_params.append(('CMAKE_INSTALL_PREFIX', install_path))

if sys.platform == 'darwin':
    if sysroot is not None:
        cmake_params.append(('CMAKE_OSX_SYSROOT', sysroot))

    cmake_params.append(("CMAKE_OSX_DEPLOYMENT_TARGET", "10.14"))
    if args.universal:
        cmake_params.append(("CMAKE_OSX_ARCHITECTURES", "arm64;x86_64"))


cmake_params_array = []
for option, value in cmake_params:
    cmake_params_array.append("-D{}={}".format(option, value))

print(
    ' '.join(shlex.quote(a) for a in ["cmake", str(source_path)] + cmake_params_array + cmake_generator_array))
if subprocess.call(["cmake", str(source_path)] + cmake_params_array + cmake_generator_array,
                   cwd=build_path) != 0:
    print("Failed to configure yasm build")
    sys.exit(1)

print("\nBuilding yasm...")
if install_path.exists():
    remove_dir(install_path)
install_path.mkdir(parents=True)

if subprocess.call([make_cmd] + parallel, cwd=build_path) != 0:
    print("yasm failed to build")
    sys.exit(1)


print("\nInstalling yasm...")
if subprocess.call([make_cmd, "install"], cwd=build_path) != 0:
    print("yasm failed to install")
    sys.exit(1)


if args.sign:
    if sys.platform == 'darwin':
        # Look for all Mach-O files in the installation
        for root, dirs, files in os.walk(install_path):
            for file in files:
                file_path = os.path.join(root, file)
                if not os.access(file_path, os.X_OK):
                    continue

                # Check for Mach-O signature
                if not open(file_path, 'rb').read(4) == b"\xca\xfe\xba\xbe":
                    continue

                if not mac_sign(file_path, False, True):
                    print(f"Failed to sign {file_path}")
                    sys.exit(1)
    elif sys.platform.startswith("win"):
        # Look for all exe/dll files in the installation
        for root, dirs, files in os.walk(install_path):
            for file in files:
                if file.endswith(".exe") or file.endswith(".dll"):
                    file_path = os.path.join(root, file)
                    if not signWindowsFiles(file_path):
                        print(f"Failed to sign {file_path}")
                        sys.exit(1)

print("\nCreating archive...")
with zipfile.ZipFile(artifact_path / f'yasm-{yasm_version}.zip', 'w', zipfile.ZIP_DEFLATED) as z:
    for root, dirs, files in os.walk(install_path):
        relpath = root.replace(str(install_path), "")
        relpath = relpath.strip('\/')
        for file in files:
            print(f"Adding {relpath}/{file}...")
            file_path = os.path.join(root, file)
            arc_name = os.path.join("yasm", yasm_version, relpath, file)
            info = zipfile.ZipInfo(arc_name)
            info.compress_type = zipfile.ZIP_DEFLATED

            if os.access(file_path, os.X_OK):
                info.external_attr = 0o755 << 16  # -rwxr-xr-x
            else:
                info.external_attr = 0o644 << 16  # -rwxr--r--

            with open(file_path, 'rb') as f:
                z.writestr(info, f.read())

if args.clean:
    print("Cleaning up...")
    remove_dir(source_path)
