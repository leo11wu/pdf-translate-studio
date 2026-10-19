"""Build the pinned OpenCV release with only this app's image modules.

Build tools: scikit-build==0.18.1 cmake==4.2.3 ninja==1.13.0 wheel==0.45.1.
The output is a local wheel; no global Python installation is modified.
"""
import os
from pathlib import Path
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[1]
OPENCV = "5.0.0.93"
OPTIONS = [
    "-DBUILD_LIST=core,imgproc,imgcodecs,python3", "-DBUILD_SHARED_LIBS=OFF",
    "-DWITH_FFMPEG=OFF", "-DWITH_AVFOUNDATION=OFF", "-DWITH_GSTREAMER=OFF",
    "-DWITH_QT=OFF", "-DWITH_GTK=OFF", "-DWITH_VTK=OFF", "-DWITH_OPENCL=OFF",
    "-DWITH_OPENEXR=OFF", "-DWITH_OPENJPEG=OFF", "-DWITH_JASPER=OFF",
    "-DWITH_AVIF=OFF", "-DWITH_WEBP=OFF", "-DWITH_LAPACK=OFF", "-DWITH_ITT=OFF",
    "-DBUILD_opencv_apps=OFF", "-DBUILD_TESTS=OFF", "-DBUILD_PERF_TESTS=OFF",
    "-DBUILD_EXAMPLES=OFF", "-DBUILD_JAVA=OFF", "-DCMAKE_OSX_ARCHITECTURES=arm64",
    "-DCMAKE_OSX_DEPLOYMENT_TARGET=14.0", "-DCMAKE_BUILD_TYPE=Release",
]

if __name__ == "__main__":
    env = dict(os.environ, ENABLE_HEADLESS="1", ENABLE_CONTRIB="0",
               CMAKE_ARGS=" ".join(OPTIONS), CMAKE_GENERATOR="Ninja",
               CMAKE_BUILD_PARALLEL_LEVEL="8", MAKEFLAGS="-j8",
               PATH=str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"])
    destination = ROOT / ".runtime/slim-wheels"
    destination.mkdir(parents=True, exist_ok=True)
    archives = ROOT / ".runtime/wheels"
    archives.mkdir(parents=True, exist_ok=True)
    archive = archives / f"opencv_python_headless-{OPENCV}.tar.gz"
    if not archive.exists():
        subprocess.run([sys.executable, "-m", "pip", "download", "--no-deps", "--no-build-isolation",
                        "--no-binary=:all:", "--dest", str(archives), f"opencv-python-headless=={OPENCV}"], check=True)
    unpacked = ROOT / ".runtime/slim-opencv-source"
    unpacked.mkdir(exist_ok=True)
    with tarfile.open(archive) as tar:
        source = unpacked / Path(tar.getmembers()[0].name).parts[0]
        if not source.exists():
            for member in tar.getmembers():
                target = (unpacked / member.name).resolve()
                if not target.is_relative_to(unpacked.resolve()) or not (member.isfile() or member.isdir()):
                    raise RuntimeError("Unexpected source archive member")
                tar.extract(member, path=unpacked)
    # Upstream's stub generator assumes Feature2D exists even in a subset build.
    # Do not advertise or require typing stubs that this subset cannot generate.
    # This packaging-only patch does not change any image-processing code.
    setup = source / "setup.py"
    text = setup.read_text().replace('[ r"python/cv2/py.typed" ]', '[]')
    text = text.replace('rearrange_cmake_output_data["cv2.typing"] = ["python/cv2" + r"/typing/.*\\.py"]',
                        'pass  # Slim build omits unavailable generated typing stubs')
    setup.write_text(text)
    subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation",
                    "--cache-dir", str(ROOT / ".runtime/pip-cache"), "--wheel-dir", str(destination), str(source)],
                   cwd=ROOT, env=env, check=True)
