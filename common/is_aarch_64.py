import platform
import sys


def is_aarch64():
    machine = platform.uname()[4]
    if machine in ['aarch64']:
        return True
    else:
        return False


if is_aarch64():
    sys.path.append('common/bindings/jetson')
else:
    sys.path.append('common/bindings/x86_64')
