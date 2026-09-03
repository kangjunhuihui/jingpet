# system_control.py
# 系统控制：音量（WASAPI 系统主音量，与任务栏喇叭滑块一致）、锁屏、重启。
# 全部 Windows 自带能力，零第三方依赖；命令均为列表参数，无 shell 拼接。
#
# 音量实现说明：用 ctypes 直接调 Windows Core Audio（WASAPI）COM 接口
#   IMMDeviceEnumerator → IMMDevice.Activate → IAudioEndpointVolume，
#   它控制的就是"系统主音量"（任务栏喇叭那个滑块），所有程序的声音一起变。
#   （此前用过 waveOutSetVolume，那只是传统波形输出音量，与系统主音量联动不可靠，已弃用）

import ctypes
import logging
import subprocess
import uuid
from ctypes import CFUNCTYPE, HRESULT, POINTER, c_float, c_ulong, c_void_p, wintypes

logger = logging.getLogger("jingjing.system_control")

# ---------- WASAPI 音量（COM） ----------

CLSCTX_INPROC_SERVER = 0x1
# 音频端点方向/角色：eRender=0, eConsole=0
E_RENDER = 0
E_CONSOLE = 0


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", c_ulong), ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD), ("Data4", wintypes.BYTE * 8),
    ]


def _make_guid(text: str) -> GUID:
    g = uuid.UUID(text)
    return GUID(g.time_low, g.time_mid, g.time_hi_version,
                (wintypes.BYTE * 8)(*g.bytes[8:16]))


CLSID_MMDeviceEnumerator = _make_guid("BCDE0395-E52F-467C-8E3D-C4579291692E")
IID_IMMDeviceEnumerator = _make_guid("A95664D2-9614-4F35-A746-DE8DB63617E6")
IID_IAudioEndpointVolume = _make_guid("5CDF2C82-841E-4546-9722-0CF74078229A")


def _vt(obj, index, restype, *argtypes):
    """取出 COM 对象 vtable 的第 index 个方法并绑定对象（IUnknown 占 0-2）。"""
    vtable = ctypes.cast(obj, POINTER(POINTER(c_void_p))).contents
    return CFUNCTYPE(restype, c_void_p, *argtypes)(vtable[index])


def _open_endpoint_volume():
    """
    打开默认音频端点（扬声器）的 IAudioEndpointVolume 指针。
    每次调用独立创建并 CoInitialize（幂等），调用方负责 Release。
    失败返回 None。
    """
    try:
        ole32 = ctypes.windll.ole32
        ole32.CoInitialize.argtypes = [c_void_p]
        ole32.CoInitialize(None)  # 幂等；确保当前线程已初始化 COM
        ole32.CoCreateInstance.argtypes = [
            POINTER(GUID), c_void_p, c_ulong, POINTER(GUID), POINTER(c_void_p),
        ]
        ole32.CoCreateInstance.restype = HRESULT
    except (OSError, AttributeError):
        return None

    enum = c_void_p()
    hr = ole32.CoCreateInstance(
        ctypes.byref(CLSID_MMDeviceEnumerator), None, CLSCTX_INPROC_SERVER,
        ctypes.byref(IID_IMMDeviceEnumerator), ctypes.byref(enum),
    )
    if hr != 0 or not enum:
        return None
    try:
        device = c_void_p()
        # IMMDeviceEnumerator::GetDefaultAudioEndpoint(eRender, eConsole, &device) — vtable[4]
        hr = _vt(enum, 4, HRESULT, ctypes.c_int, ctypes.c_int, POINTER(c_void_p))(
            enum, E_RENDER, E_CONSOLE, ctypes.byref(device))
        if hr != 0 or not device:
            return None
        try:
            volume = c_void_p()
            # IMMDevice::Activate(iid, CLSCTX, NULL, &volume) — vtable[3]
            hr = _vt(device, 3, HRESULT, POINTER(GUID), c_ulong, c_void_p, POINTER(c_void_p))(
                device, ctypes.byref(IID_IAudioEndpointVolume),
                CLSCTX_INPROC_SERVER, None, ctypes.byref(volume))
            if hr != 0 or not volume:
                return None
            return volume
        finally:
            _vt(device, 2, HRESULT)(device)  # IMMDevice::Release
    finally:
        _vt(enum, 2, HRESULT)(enum)  # IMMDeviceEnumerator::Release


def _release(volume) -> None:
    """释放 IAudioEndpointVolume（IUnknown::Release = vtable[2]）。"""
    try:
        _vt(volume, 2, HRESULT)(volume)
    except Exception:
        pass


# ---------- 音量 ----------

def get_volume() -> int | None:
    """读取系统主音量（0-100）；失败返回 None。"""
    volume = _open_endpoint_volume()
    if volume is None:
        return None
    try:
        level = c_float()
        # IAudioEndpointVolume::GetMasterVolumeLevelScalar — vtable[9]
        if _vt(volume, 9, HRESULT, POINTER(c_float))(volume, ctypes.byref(level)) != 0:
            return None
        return round(level.value * 100)
    except OSError:  # 无音频设备等环境下 COM 调用可能触发访问冲突，安全降级
        return None
    finally:
        _release(volume)


def set_volume(percent: int) -> bool:
    """设置系统主音量（0-100）。"""
    volume = _open_endpoint_volume()
    if volume is None:
        return False
    try:
        scalar = c_float(max(0.0, min(100.0, float(percent))) / 100.0)
        # IAudioEndpointVolume::SetMasterVolumeLevelScalar — vtable[7]
        hr = _vt(volume, 7, HRESULT, c_float, POINTER(GUID))(
            volume, scalar, None)
        return hr == 0
    except OSError:
        return False
    finally:
        _release(volume)


def volume_up(step: int = 10) -> int | None:
    """音量调大 step 个百分点，返回新音量；失败返回 None。"""
    current = get_volume()
    if current is None:
        return None
    new_volume = min(100, current + step)
    return new_volume if set_volume(new_volume) else None


def volume_down(step: int = 10) -> int | None:
    """音量调小 step 个百分点，返回新音量；失败返回 None。"""
    current = get_volume()
    if current is None:
        return None
    new_volume = max(0, current - step)
    return new_volume if set_volume(new_volume) else None


def toggle_mute() -> int | None:
    """
    静音切换：当前有声 → 静音（音量置 0），返回 0；
    当前静音 → 恢复静音前音量，返回恢复后的音量。
    实现说明：不用 IAudioEndpointVolume::SetMute（部分环境驱动会崩溃），
    而是用已验证可靠的 SetMasterVolumeLevelScalar 把音量设为 0 来实现静音，
    并进程内记忆静音前音量用于恢复。
    """
    current = get_volume()
    if current is None:
        return None
    if current > 0:
        _last_volume_before_mute[0] = current
        return 0 if set_volume(0) else None
    restore = _last_volume_before_mute[0] or 50
    new_volume = restore if set_volume(restore) else None
    _last_volume_before_mute[0] = 0
    return new_volume


# 静音前音量记忆（进程内有效）
_last_volume_before_mute = [0]


# ---------- 媒体控制（模拟多媒体键，系统全局生效） ----------
# 原理：Windows 把多媒体键（键盘上那排播放/暂停/切歌键）作为系统级全局快捷键处理，
# 但只分发给注册了 SMTC（System Media Transport Controls）的播放器；
# 很多开源/小众播放器未注册 SMTC，媒体键对它们无效。
# 补救：键盘媒体键本质上还会转成 WM_APPCOMMAND 窗口消息，不少未注册 SMTC 的
# 播放器仍会响应这条消息，因此向所有可见顶层窗口广播一份。
# 三管齐下（顺序无关，全发）：
#   1) SendInput（现代 API，注册 SMTC 的播放器）；
#   2) keybd_event（兜底）；
#   3) WM_APPCOMMAND 广播（未注册 SMTC 但处理该消息的播放器）。

VK_MEDIA_NEXT_TRACK = 0xB0  # 下一首
VK_MEDIA_PREV_TRACK = 0xB1  # 上一首
VK_MEDIA_STOP = 0xB2        # 停止
VK_MEDIA_PLAY_PAUSE = 0xB3  # 播放/暂停（切换）
KEYEVENTF_KEYUP = 0x0002
INPUT_KEYBOARD = 0x0001

# WM_APPCOMMAND 广播
WM_APPCOMMAND = 0x0319
APPCOMMAND_MEDIA_NEXTTRACK = 11
APPCOMMAND_MEDIA_PREVTRACK = 12
APPCOMMAND_MEDIA_STOP = 13
APPCOMMAND_MEDIA_PLAY_PAUSE = 14


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [("ki", _KEYBDINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUT_UNION)]


_WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def _init_appcommand_api() -> None:
    """初始化 WM_APPCOMMAND 广播的 ctypes 签名（模块导入时设置一次；非 Windows 静默跳过）。"""
    try:
        user32 = ctypes.windll.user32
        user32.EnumWindows.argtypes = [_WNDENUMPROC, wintypes.LPARAM]
        user32.EnumWindows.restype = wintypes.BOOL
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.IsWindowVisible.restype = wintypes.BOOL
        user32.SendMessageTimeoutW.argtypes = [
            wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
            wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t),
        ]
        user32.SendMessageTimeoutW.restype = ctypes.c_long
    except (OSError, AttributeError):
        pass


_init_appcommand_api()


def _sendinput_key(vk: int) -> bool:
    """用 SendInput 模拟按下+抬起一次媒体键（键码 + 键码抬起两次调用）。"""
    try:
        user32 = ctypes.windll.user32
        for flags in (0, KEYEVENTF_KEYUP):
            entry = _INPUT()
            entry.type = INPUT_KEYBOARD
            entry.u.ki.wVk = vk
            entry.u.ki.wScan = 0
            entry.u.ki.dwFlags = flags
            entry.u.ki.time = 0
            entry.u.ki.dwExtraInfo = 0
            sent = user32.SendInput(1, ctypes.byref(entry), ctypes.sizeof(_INPUT))
            if sent != 1:
                return False
        return True
    except (OSError, AttributeError):
        return False


def _keybd_event_key(vk: int) -> bool:
    """兜底：用 keybd_event 模拟按下+抬起一次媒体键。"""
    try:
        user32 = ctypes.windll.user32
        user32.keybd_event(vk, 0, 0, 0)
        user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
        return True
    except (OSError, AttributeError):
        return False


def _broadcast_appcommand(cmd: int) -> None:
    """
    向所有可见顶层窗口广播 WM_APPCOMMAND（兼容未注册 SMTC 但处理该消息的播放器）。
    用 SendMessageTimeout 带超时，避免某个窗口卡住拖死调用方；失败静默忽略。
    """
    try:
        user32 = ctypes.windll.user32
        # 低位字 = 命令，高位字 = 按键标志（0）
        lparam = (cmd & 0xFFFF) | (0 << 16)
        result = ctypes.c_size_t()

        def _send(hwnd, _lparam):
            if user32.IsWindowVisible(hwnd):
                user32.SendMessageTimeoutW(
                    hwnd, WM_APPCOMMAND, hwnd, lparam,
                    0, 200, ctypes.byref(result),
                )
            return True

        user32.EnumWindows(_WNDENUMPROC(_send), 0)
    except (OSError, AttributeError):
        pass


def _press_media_key(vk: int, appcommand: int) -> bool:
    """模拟按一次媒体键：SendInput → keybd_event → WM_APPCOMMAND 广播，全发。"""
    ok = _sendinput_key(vk) or _keybd_event_key(vk)
    _broadcast_appcommand(appcommand)
    return ok


def media_play_pause() -> bool:
    """播放/暂停切换。"""
    return _press_media_key(VK_MEDIA_PLAY_PAUSE, APPCOMMAND_MEDIA_PLAY_PAUSE)


def media_next_track() -> bool:
    """下一首。"""
    return _press_media_key(VK_MEDIA_NEXT_TRACK, APPCOMMAND_MEDIA_NEXTTRACK)


def media_prev_track() -> bool:
    """上一首。"""
    return _press_media_key(VK_MEDIA_PREV_TRACK, APPCOMMAND_MEDIA_PREVTRACK)


def media_stop() -> bool:
    """停止播放。"""
    return _press_media_key(VK_MEDIA_STOP, APPCOMMAND_MEDIA_STOP)


# ---------- 锁屏 / 重启 ----------

def lock_screen() -> bool:
    """锁定屏幕（rundll32 调用系统锁屏，无需管理员）。"""
    try:
        result = subprocess.run(
            ["rundll32.exe", "user32.dll,LockWorkStation"],
            capture_output=True, timeout=10,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def reboot_computer() -> bool:
    """立即重启电脑（与关机一致，普通用户可执行）。"""
    try:
        result = subprocess.run(["shutdown", "/r", "/t", "0"], timeout=10)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False
