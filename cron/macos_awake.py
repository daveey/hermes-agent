"""Read macOS power state without forking a large gateway. [daveey]

Cache ABI bindings only: every call queries current assertions/power/lid state.
IOPMCopyAssertionsStatus and IOPSGetProvidingPowerSourceType are the APIs used by
pmset itself. Copy/Create objects are released; Get results are borrowed.
"""
from contextlib import contextmanager
import ctypes as C
from functools import lru_cache


class _Power:
    def __init__(self):
        self.cf = C.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
        self.io = C.CDLL('/System/Library/Frameworks/IOKit.framework/IOKit')
        pointer, uint = C.c_void_p, C.c_uint32
        signatures = (
            (self.cf, 'CFRelease', None, [pointer]),
            (self.cf, 'CFGetTypeID', C.c_ulong, [pointer]),
            (self.cf, 'CFStringCreateWithCString', pointer, [pointer, C.c_char_p, uint]),
            (self.cf, 'CFDictionaryGetValue', pointer, [pointer, pointer]),
            (self.cf, 'CFNumberGetTypeID', C.c_ulong, []),
            (self.cf, 'CFNumberGetValue', C.c_bool, [pointer, C.c_long, pointer]),
            (self.cf, 'CFBooleanGetTypeID', C.c_ulong, []),
            (self.cf, 'CFBooleanGetValue', C.c_bool, [pointer]),
            (self.cf, 'CFStringGetCString', C.c_bool, [pointer, C.c_void_p, C.c_long, uint]),
            (self.io, 'IOPMCopyAssertionsStatus', C.c_int32, [C.POINTER(pointer)]),
            (self.io, 'IOPSCopyPowerSourcesInfo', pointer, []),
            (self.io, 'IOPSGetProvidingPowerSourceType', pointer, [pointer]),
            (self.io, 'IOServiceMatching', pointer, [C.c_char_p]),
            (self.io, 'IOServiceGetMatchingService', uint, [uint, pointer]),
            (self.io, 'IORegistryEntryCreateCFProperty', pointer, [uint, pointer, pointer, uint]),
            (self.io, 'IOObjectRelease', C.c_int32, [uint]),
        )
        for lib, name, result, args in signatures:
            fn = getattr(lib, name)
            fn.restype, fn.argtypes = result, args

    @contextmanager
    def owned(self, value):
        try:
            yield value
        finally:
            if value:
                self.cf.CFRelease(value)

    def key(self, text):
        value = self.cf.CFStringCreateWithCString(None, text, 0x08000100)
        if not value:
            raise OSError('cannot allocate power-state key')
        return self.owned(value)

    def active(self):
        result = C.c_void_p()
        status = self.io.IOPMCopyAssertionsStatus(C.byref(result))
        with self.owned(result.value) as assertions:
            if status or not assertions:
                raise OSError('cannot read power assertions')
            with self.key(b'UserIsActive') as key:
                value = self.cf.CFDictionaryGetValue(assertions, key)
                if not value:
                    return False  # pmset likewise falls through on a missing assertion
                number = C.c_int32()
                if (self.cf.CFGetTypeID(value) != self.cf.CFNumberGetTypeID()
                        or not self.cf.CFNumberGetValue(value, 3, C.byref(number))):
                    raise OSError('invalid power assertion')
                return number.value == 1

    def on_ac(self):
        with self.owned(self.io.IOPSCopyPowerSourcesInfo()) as info:
            if not info:
                raise OSError('cannot read power sources')
            value = self.io.IOPSGetProvidingPowerSourceType(info)
            buf = C.create_string_buffer(128)
            if not value or not self.cf.CFStringGetCString(value, buf, len(buf), 0x08000100):
                raise OSError('cannot read providing power source')
            return buf.value == b'AC Power'

    def lid_closed(self):
        matching = self.io.IOServiceMatching(b'IOPMrootDomain')
        if not matching:
            raise OSError('cannot create root-domain matching dictionary')
        # IOServiceGetMatchingService consumes the matching dictionary, even on failure.
        entry = self.io.IOServiceGetMatchingService(0, matching)
        if not entry:
            raise OSError('cannot read power root domain')
        try:
            with self.key(b'AppleClamshellState') as key:
                with self.owned(self.io.IORegistryEntryCreateCFProperty(entry, key, None, 0)) as value:
                    if not value:
                        return False  # desktops have no clamshell
                    if self.cf.CFGetTypeID(value) != self.cf.CFBooleanGetTypeID():
                        raise OSError('invalid clamshell property')
                    return bool(self.cf.CFBooleanGetValue(value))
        finally:
            self.io.IOObjectRelease(entry)

    def awake(self):
        return self.active() or (self.on_ac() and not self.lid_closed())


@lru_cache(maxsize=1)
def _bindings():
    try:
        return _Power()
    except (OSError, AttributeError):
        return None  # unavailable ABI: retain the existing command-line fallback


def fully_awake():
    """Current verdict, or None if native APIs are unavailable; read errors propagate."""
    power = _bindings()
    return None if power is None else power.awake()
