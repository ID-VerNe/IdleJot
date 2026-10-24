"""Stage models shipped in the pinned RapidOCR wheel for offline operation."""
import hashlib
import importlib.util
from importlib.metadata import version,PackageNotFoundError
from pathlib import Path
import shutil
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
NAMES=('PP-OCRv6_det_small.onnx','PP-OCRv6_rec_small.onnx','ch_ppocr_mobile_v2.0_cls_mobile.onnx')


def headless_runtime():
    """Normalize pip's desktop cv2 dependency; uv installs exclude it up front."""
    conflicts=[]
    for name in ('opencv-python','opencv-contrib-python','opencv-contrib-python-headless'):
        try:version(name);conflicts.append(name)
        except PackageNotFoundError:pass
    try:ready=version('opencv-python-headless')=='5.0.0.93'
    except PackageNotFoundError:ready=False
    if conflicts or not ready:
        uv=shutil.which('uv')
        if not uv and (ROOT/'.tmp/bootstrap/bin/uv').is_file():uv=str(ROOT/'.tmp/bootstrap/bin/uv')
        if uv:
            if conflicts:subprocess.run([uv,'pip','uninstall','--python',sys.executable,*conflicts],check=True)
            command=[uv,'pip','install','--python',sys.executable,'--reinstall','--no-deps']
        else:
            subprocess.run([sys.executable,'-m','ensurepip','--upgrade'],check=True)
            if conflicts:subprocess.run([sys.executable,'-m','pip','uninstall','-y',*conflicts],check=True)
            command=[sys.executable,'-m','pip','install','--force-reinstall','--no-deps']
        subprocess.run(command+['-r',str(ROOT/'requirements/linux-opencv.txt')],check=True)
    # Fail installation if native shared libraries cannot actually load.
    subprocess.run([sys.executable,'-c','import cv2, onnxruntime; from rapidocr import RapidOCR'],check=True)


def main():
    headless_runtime()
    package=importlib.util.find_spec('rapidocr')
    if package is None:raise ValueError('请先安装 requirements/linux-ocr.txt。')
    source=Path(package.origin).parent/'models';destination=ROOT/'.cache/rapidocr'
    destination.mkdir(parents=True,exist_ok=True)
    for name in NAMES:
        path=source/name
        if not path.is_file():raise ValueError('RapidOCR 安装包缺少固定模型，请重新安装固定依赖。')
        staged=destination/(name+'.new');shutil.copyfile(path,staged)
        with path.open('rb') as src,staged.open('rb') as copy:
            if hashlib.file_digest(src,'sha256').digest()!=hashlib.file_digest(copy,'sha256').digest():raise ValueError('OCR 模型复制校验失败。')
        staged.replace(destination/name)
    print('Pinned OCR models staged for offline use.')


if __name__=='__main__':main()
