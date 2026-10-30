"""Windows executable parsing and host compatibility helpers. [daveey]"""

from pathlib import Path
from typing import Optional


def _windows_user_runnable_pe_machines() -> Optional[set]:
    """Query native and emulated user-mode machine support. [daveey]"""
    import ctypes
    from ctypes import wintypes
    from hermes_cli.main_desktop import (
        _kernel32, _PE_MACHINE_ARM64, _PE_MACHINE_AMD64, _PE_MACHINE_I386,
        _MACHINE_ATTRIBUTE_USER_ENABLED,
    )
    kernel32 = _kernel32()
    kernel32.GetMachineTypeAttributes.argtypes = [wintypes.USHORT, ctypes.POINTER(ctypes.c_int)]
    kernel32.GetMachineTypeAttributes.restype = ctypes.c_long

    runnable = set()
    for machine in (_PE_MACHINE_ARM64, _PE_MACHINE_AMD64, _PE_MACHINE_I386):
        attributes = ctypes.c_int(0)
        if kernel32.GetMachineTypeAttributes(machine, ctypes.byref(attributes)):
            continue
        if attributes.value & _MACHINE_ATTRIBUTE_USER_ENABLED:
            runnable.add(machine)
    return runnable or None


def _expected_windows_pe_machines() -> set:
    """PE machines this Windows host can load, retaining facade patch seams. [daveey]"""
    from hermes_cli.main_desktop import (
        sys, _windows_user_runnable_pe_machines, _windows_native_machine,
        _PE_MACHINE_ARM64, _PE_MACHINE_AMD64, _PE_MACHINE_I386,
    )
    if sys.platform == "win32":
        try:
            runnable = _windows_user_runnable_pe_machines()
        except (OSError, AttributeError, TypeError, ValueError):
            runnable = None
        if runnable:
            return runnable
    machine = _windows_native_machine().upper()
    if machine in ("AMD64", "X86_64", "X64"):
        return {_PE_MACHINE_AMD64, _PE_MACHINE_I386}
    if machine in ("ARM64", "AARCH64"):
        return {_PE_MACHINE_ARM64, _PE_MACHINE_AMD64}
    if machine in ("X86", "I386", "I486", "I586", "I686"):
        return {_PE_MACHINE_I386}
    return {_PE_MACHINE_AMD64, _PE_MACHINE_ARM64, _PE_MACHINE_I386}


def _parse_pe_machine(path: Path) -> int:
    """Read COFF machine field, rejecting incomplete executable structures. [daveey]"""
    import struct
    try:
        file_size = path.stat().st_size
    except OSError as exc:
        raise ValueError(f"unreadable: {exc}")
    if file_size < 512:
        raise ValueError(f"file is only {file_size} bytes — far too small to be a Windows executable")
    with path.open("rb") as fh:
        head = fh.read(64)
        if len(head) < 64 or head[:2] != b"MZ":
            raise ValueError(
                "missing MZ header — not a Windows executable (a truncated or non-binary file saved as .exe?)"
            )
        e_lfanew = struct.unpack_from("<I", head, 0x3C)[0]
        if e_lfanew <= 0 or e_lfanew + 24 > file_size:
            raise ValueError("corrupt DOS header: PE header offset points past end of file")
        fh.seek(e_lfanew)
        pe_head = fh.read(24)
        if len(pe_head) < 24 or pe_head[:4] != b"PE\x00\x00":
            raise ValueError("missing PE signature — corrupt executable header")
        machine, n_sections = struct.unpack_from("<HH", pe_head, 4)
        size_of_optional = struct.unpack_from("<H", pe_head, 20)[0]
        fh.seek(e_lfanew + 24 + size_of_optional)
        max_section_end = 0
        for _ in range(n_sections):
            section = fh.read(40)
            if len(section) < 40:
                raise ValueError("truncated PE section table")
            size_of_raw, pointer_to_raw = struct.unpack_from("<II", section, 16)
            max_section_end = max(max_section_end, pointer_to_raw + size_of_raw)
        if file_size < max_section_end:
            raise ValueError(
                f"truncated executable: file is {file_size} bytes but its PE sections extend to {max_section_end} bytes"
            )
    return machine
