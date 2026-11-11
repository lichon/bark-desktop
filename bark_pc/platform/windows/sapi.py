"""SAPI 5 voice output through raw COM — no `pywin32`, no extra dependency.

`SAPI.SpVoice` is reached as an `IDispatch` object, so this module only declares
the seven `IDispatch` vtable slots it actually calls and builds the
`VARIANT`/`DISPPARAMS` arguments itself.  `Speak` is called with `SVSFlagsAsync`,
so SAPI renders on its own thread and `speak()` returns immediately; a new line
purges whatever is still queued, which is what a notification actually wants.

Threading: a COM object belongs to the apartment that created it, so an engine
must be created and used on a single thread.  `bark_pc.tts.worker` owns that
thread; nothing here is thread safe and nothing here is imported until the first
line is spoken.

Failures raise `SapiError`, never a bare `IDispatch::Invoke` error: `Invoke` only
ever reports `DISP_E_EXCEPTION`, so the `EXCEPINFO` it fills in is read and
surfaced as SAPI's own `scode` plus description ("no audio device was found"
beats "0x80020009" in a log line).
"""

from __future__ import annotations

import ctypes
import logging
import sys
from ctypes import POINTER, byref, c_long, c_ubyte, c_uint, c_ulong, c_ushort, c_void_p, c_wchar_p

logger = logging.getLogger("bark_pc.sapi")

_FUNCTYPE = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)

CLSID_SPVOICE = "{96749377-3391-11D2-9EE3-00C04F797396}"
IID_IDISPATCH = "{00020400-0000-0000-C000-000000000046}"

CLSCTX_INPROC_SERVER = 0x1
COINIT_APARTMENTTHREADED = 0x2

DISPATCH_METHOD = 0x1
DISPATCH_PROPERTYPUT = 0x4
DISPID_PROPERTYPUT = -3

VT_EMPTY = 0
VT_I4 = 3
VT_BSTR = 8

# SVSFlagsAsync | SVSFPurgeBeforeSpeak | SVSFIsNotXML: speak in the background,
# drop anything still queued, and never parse the text as XML markup.
SPEAK_FLAGS = 0x1 | 0x2 | 0x10
PURGE_FLAGS = 0x2 | 0x10

_S_OK = 0
_S_FALSE = 1
_RPC_E_CHANGED_MODE = -2147417850  # 0x80010106: COM already up in another mode
DISP_E_EXCEPTION = 0x80020009  # the object raised; see EXCEPINFO.scode

RATE_RANGE = (-10, 10)
VOLUME_RANGE = (0, 100)

_POINTER_BYTES = ctypes.sizeof(c_void_p)
# The VARIANT union is 16 bytes on x64 (24-byte struct) and 8 bytes on x86
# (16-byte struct); SAPI walks an array of these, so the stride must be exact.
_UNION_BYTES = 16 if _POINTER_BYTES == 8 else 8


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", c_uint),
        ("Data2", c_ushort),
        ("Data3", c_ushort),
        ("Data4", c_ubyte * 8),
    ]


class _VariantValue(ctypes.Union):
    _fields_ = [
        ("lVal", c_long),
        ("bstrVal", c_void_p),
        ("pad", c_ubyte * _UNION_BYTES),
    ]


class VARIANT(ctypes.Structure):
    _fields_ = [
        ("vt", c_ushort),
        ("wReserved1", c_ushort),
        ("wReserved2", c_ushort),
        ("wReserved3", c_ushort),
        ("value", _VariantValue),
    ]


class DISPPARAMS(ctypes.Structure):
    _fields_ = [
        ("rgvarg", POINTER(VARIANT)),
        ("rgdispidNamedArgs", POINTER(c_long)),
        ("cArgs", c_uint),
        ("cNamedArgs", c_uint),
    ]


HRESULT = c_long
DISPID = c_long


class EXCEPINFO(ctypes.Structure):
    """What `IDispatch::Invoke` fills in when the object raises a COM exception.

    Without this the only thing a caller learns is `DISP_E_EXCEPTION`; the
    `scode` and `bstrDescription` are where SAPI says *why* (no audio endpoint,
    no voice installed, device busy, ...).
    """

    _fields_ = [
        ("wCode", c_ushort),
        ("wReserved", c_ushort),
        ("bstrSource", c_void_p),
        ("bstrDescription", c_void_p),
        ("bstrHelpFile", c_void_p),
        ("dwHelpContext", c_uint),
        ("pvReserved", c_void_p),
        ("pfnDeferredFillIn", c_void_p),
        ("scode", HRESULT),
    ]


class _IDispatchVtbl(ctypes.Structure):
    _fields_ = [
        ("QueryInterface", _FUNCTYPE(HRESULT, c_void_p, c_void_p, POINTER(c_void_p))),
        ("AddRef", _FUNCTYPE(c_ulong, c_void_p)),
        ("Release", _FUNCTYPE(c_ulong, c_void_p)),
        ("GetTypeInfoCount", _FUNCTYPE(HRESULT, c_void_p, POINTER(c_uint))),
        ("GetTypeInfo", _FUNCTYPE(HRESULT, c_void_p, c_uint, c_ulong, POINTER(c_void_p))),
        (
            "GetIDsOfNames",
            _FUNCTYPE(
                HRESULT,
                c_void_p,
                c_void_p,
                POINTER(c_wchar_p),
                c_uint,
                c_ulong,
                POINTER(DISPID),
            ),
        ),
        (
            "Invoke",
            _FUNCTYPE(
                HRESULT,
                c_void_p,
                DISPID,
                c_void_p,
                c_ulong,
                c_ushort,
                POINTER(DISPPARAMS),
                c_void_p,
                c_void_p,
                c_void_p,
            ),
        ),
    ]


def _variant_i4(value: int) -> VARIANT:
    variant = VARIANT()
    variant.vt = VT_I4
    variant.value.lVal = int(value)
    return variant


def _clamp(value, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return max(low, min(high, number))


def _hresult(hr: int) -> str:
    return f"0x{hr & 0xFFFFFFFF:08X}"


class SapiError(OSError):
    """The voice object rejected a call.

    `hresult` is what `IDispatch::Invoke` returned; `scode` is what SAPI itself
    reported, which is the interesting one when Invoke only wraps it as
    `DISP_E_EXCEPTION`.  Both are attributes so callers can branch on them
    instead of matching the message.
    """

    def __init__(
        self,
        member: str,
        hresult: int,
        *,
        scode: int | None = None,
        source: str = "",
        description: str = "",
    ):
        self.member = member
        self.hresult = hresult & 0xFFFFFFFF
        self.scode = self.hresult if scode is None else scode & 0xFFFFFFFF
        self.source = source
        self.description = description
        detail = f"{member} failed: {_hresult(hresult)}"
        if self.hresult == DISP_E_EXCEPTION and self.scode != self.hresult:
            detail += f" (DISP_E_EXCEPTION, scode {_hresult(self.scode)})"
        if description:
            detail += f": {description}"
        elif source:
            detail += f" [{source}]"
        super().__init__(detail)

    @property
    def from_sapi(self) -> bool:
        """True when the voice reported the failure, false for COM/marshalling faults.

        SAPI's own errors live in `FACILITY_ITF` (0x8004xxxx); anything else came
        from the COM layer and means our own plumbing is wrong.
        """
        return self.hresult == DISP_E_EXCEPTION or (self.scode & 0x1FFF0000) == 0x00040000


class SapiEngine:
    """A `SAPI.SpVoice` COM object bound to the thread that constructed it."""

    def __init__(self, *, rate: int = 0, volume: int = 100):
        self._com_initialised = False
        self._dispatch: c_void_p | None = None
        self._vtbl: _IDispatchVtbl | None = None
        self._ids: dict[str, int] = {}
        self._oleaut32 = None
        self._open()
        self.set_rate(rate)
        self.set_volume(volume)

    # -- construction ------------------------------------------------------ #
    def _open(self) -> None:
        if not sys.platform.startswith("win"):
            raise OSError("SAPI voice output is only available on Windows")
        ole32 = ctypes.windll.ole32
        ole32.CLSIDFromString.argtypes = [c_wchar_p, POINTER(GUID)]
        ole32.CLSIDFromString.restype = HRESULT
        ole32.IIDFromString.argtypes = [c_wchar_p, POINTER(GUID)]
        ole32.IIDFromString.restype = HRESULT
        ole32.CoCreateInstance.argtypes = [
            POINTER(GUID),
            c_void_p,
            c_uint,
            POINTER(GUID),
            POINTER(c_void_p),
        ]
        ole32.CoCreateInstance.restype = HRESULT
        oleaut32 = ctypes.windll.oleaut32
        oleaut32.SysAllocString.argtypes = [c_wchar_p]
        oleaut32.SysAllocString.restype = c_void_p
        oleaut32.SysFreeString.argtypes = [c_void_p]
        oleaut32.SysFreeString.restype = None

        hr = ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
        if hr in (_S_OK, _S_FALSE):
            self._com_initialised = True
        elif hr != _RPC_E_CHANGED_MODE:  # already initialised by someone else
            raise OSError(f"CoInitializeEx failed: {_hresult(hr)}")

        clsid = GUID()
        iid = GUID()
        hr = ole32.CLSIDFromString(CLSID_SPVOICE, byref(clsid))
        if hr < 0:
            self._uninit()
            raise OSError(f"CLSIDFromString(SpVoice) failed: {_hresult(hr)}")
        hr = ole32.IIDFromString(IID_IDISPATCH, byref(iid))
        if hr < 0:  # pragma: no cover - constant, cannot fail in practice
            self._uninit()
            raise OSError(f"IIDFromString(IDispatch) failed: {_hresult(hr)}")

        dispatch = c_void_p()
        hr = ole32.CoCreateInstance(
            byref(clsid), None, CLSCTX_INPROC_SERVER, byref(iid), byref(dispatch)
        )
        if hr < 0:
            self._uninit()
            raise OSError(f"CoCreateInstance(SAPI.SpVoice) failed: {_hresult(hr)}")

        self._dispatch = dispatch
        self._vtbl = ctypes.cast(dispatch, POINTER(POINTER(_IDispatchVtbl))).contents.contents
        self._oleaut32 = oleaut32

    def _uninit(self) -> None:
        if self._com_initialised:
            self._com_initialised = False
            try:
                ctypes.windll.ole32.CoUninitialize()
            except Exception:  # pragma: no cover - defensive
                pass

    # -- IDispatch plumbing ------------------------------------------------ #
    def _dispid(self, name: str) -> int:
        cached = self._ids.get(name)
        if cached is not None:
            return cached
        names = (c_wchar_p * 1)(name)
        dispid = DISPID()
        hr = self._vtbl.GetIDsOfNames(self._dispatch, None, names, 1, 0, byref(dispid))
        if hr < 0:
            raise OSError(f'GetIDsOfNames("{name}") failed: {_hresult(hr)}')
        self._ids[name] = dispid.value
        return dispid.value

    def _invoke(self, member: str, dispid: int, flags: int, params: DISPPARAMS) -> None:
        info = EXCEPINFO()
        hr = self._vtbl.Invoke(
            self._dispatch, dispid, None, 0, flags, byref(params), byref(info), None, None
        )
        if hr >= 0:
            return
        source, description = self._take_exception_text(info)
        raise SapiError(
            member,
            hr,
            scode=info.scode or info.wCode or None,
            source=source,
            description=description,
        )

    def _take_exception_text(self, info: EXCEPINFO) -> tuple[str, str]:
        """Read the exception's source/description and release its BSTRs."""
        source = ctypes.cast(info.bstrSource, c_wchar_p).value if info.bstrSource else ""
        description = (
            ctypes.cast(info.bstrDescription, c_wchar_p).value if info.bstrDescription else ""
        )
        for field in ("bstrSource", "bstrDescription", "bstrHelpFile"):
            pointer = getattr(info, field)
            if pointer:
                setattr(info, field, None)
                try:
                    self._oleaut32.SysFreeString(pointer)
                except Exception:  # pragma: no cover - defensive
                    pass
        return source or "", description or ""

    def _set_property(self, name: str, value: int) -> None:
        args = (VARIANT * 1)(_variant_i4(value))
        named = (DISPID * 1)(DISPID_PROPERTYPUT)
        params = DISPPARAMS(
            rgvarg=ctypes.cast(args, POINTER(VARIANT)),
            rgdispidNamedArgs=named,
            cArgs=1,
            cNamedArgs=1,
        )
        self._invoke(name, self._dispid(name), DISPATCH_PROPERTYPUT, params)

    def _speak(self, text: str | None, flags: int) -> None:
        # DISPPARAMS wants the arguments in reverse order: (text, flags) → [flags, text].
        args = (VARIANT * 2)()
        args[0] = _variant_i4(flags)
        bstr = None
        if text is not None:
            bstr = self._oleaut32.SysAllocString(text)
            if not bstr:  # pragma: no cover - out of memory
                raise MemoryError("SysAllocString failed")
            args[1].vt = VT_BSTR
            args[1].value.bstrVal = bstr
        else:
            args[1].vt = VT_EMPTY
        params = DISPPARAMS(
            rgvarg=ctypes.cast(args, POINTER(VARIANT)),
            rgdispidNamedArgs=None,
            cArgs=2,
            cNamedArgs=0,
        )
        try:
            self._invoke("Speak", self._dispid("Speak"), DISPATCH_METHOD, params)
        finally:
            if bstr:
                self._oleaut32.SysFreeString(bstr)

    # -- engine protocol --------------------------------------------------- #
    def speak(self, text: str) -> None:
        self._speak(text or "", SPEAK_FLAGS)

    def stop(self) -> None:
        # An empty utterance with the purge flag clears the queue and the voice.
        self._speak("", PURGE_FLAGS)

    def set_rate(self, rate: int) -> None:
        self._set_property("Rate", _clamp(rate, *RATE_RANGE))

    def set_volume(self, volume: int) -> None:
        self._set_property("Volume", _clamp(volume, *VOLUME_RANGE))

    def close(self) -> None:
        if self._dispatch is not None:
            try:
                self.stop()
            except Exception:  # pragma: no cover - shutting down anyway
                pass
            try:
                self._vtbl.Release(self._dispatch)
            except Exception:  # pragma: no cover - defensive
                pass
            self._dispatch = None
            self._vtbl = None
        self._uninit()

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"SapiEngine(open={self._dispatch is not None})"
