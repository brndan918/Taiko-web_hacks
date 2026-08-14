import ctypes
import re
import sys
import threading
import time
import unicodedata
from typing import Any, Dict, List, Optional, Tuple

import pyperclip
import requests

# ---------- 配置 ----------
SONG_API_URL = "https://cjdgrevival.com/api/songs"
SONG_BASE_URL = "https://cjdgrevival.com/songs/{song_id}/main.tja"

# 玩家選擇:
PLAYER = "1"  # None = 不分 P1/P2 ; "1" = 只抓 P1 ; "2" = 只抓 P2

# --- 不可配置(請勿修改，除非改變代碼邏輯) ---
DRY_RUN = False
KEY_DON = "f"
KEY_DON_LEFT = "j"
KEY_KA = "d"
LEAD_MS = 0.0
AUTO_HIT_HZ = 30.0
AUTO_HIT_DEFAULT_DUR_S = 1.0

# --- 新增與調整的氣球行為設定 ---
BALLOON_HIT_ADJUST = 1
ENFORCE_BALLOON_HIT_LIMIT = True

# ---------- 外部歌曲資料 ----------
def fetch_song_list() -> List[Dict[str, Any]]:
    """抓取並整理 cjdgrevival 歌曲 JSON；只保留記憶體中的歌曲資料。"""
    print("🌐 正在取得歌曲列表...")
    response = requests.get(SONG_API_URL, timeout=20)
    response.raise_for_status()
    raw_data = response.json()

    songs = [
        {
            "id": song.get("id"),
            "title": (song.get("title_lang") or {}).get("ja")
            or song.get("title", ""),
            "title_en": song.get("title", ""),
            "url": SONG_BASE_URL.format(song_id=song.get("id")),
            "courses": song.get("courses") or {},
        }
        for song in raw_data
        if "id" in song
    ]
    del raw_data
    print(f"✅ 歌曲列表完成：{len(songs)} 首")
    return songs


def _normalize_song_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    text = re.sub(r"\s+", " ", text).strip().casefold()
    return text


def read_clipboard_text() -> str:
    """讀取 Windows 剪貼簿；失敗時回傳空字串。"""
    try:
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        try:
            return root.clipboard_get().strip()
        finally:
            root.destroy()
    except Exception as exc:
        print(f"⚠️ 讀取剪貼簿失敗：{exc}")
        return ""


def find_song_by_clipboard(songs: List[Dict[str, Any]], clipboard_text: str) -> Optional[Dict[str, Any]]:
    """先精確比對，再做保守的包含比對，避免誤選。"""
    query = _normalize_song_text(clipboard_text)
    if not query:
        return None

    exact_candidates = []
    contains_candidates = []

    for song in songs:
        title = _normalize_song_text(song.get("title", ""))
        title_en = _normalize_song_text(song.get("title_en", ""))
        names = {name for name in (title, title_en) if name}

        if query in names:
            exact_candidates.append(song)
            continue

        if any(query in name or name in query for name in names):
            contains_candidates.append(song)

    if exact_candidates:
        return exact_candidates[0]
    if len(contains_candidates) == 1:
        return contains_candidates[0]
    if contains_candidates:
        contains_candidates.sort(
            key=lambda song: min(
                len(_normalize_song_text(song.get("title", ""))),
                len(_normalize_song_text(song.get("title_en", ""))) or 10**9,
            )
        )
        return contains_candidates[0]
    return None


# ---------- Windows SendInput wrapper (ctypes) ----------
IS_WINDOWS = sys.platform.startswith("win")

if IS_WINDOWS:
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    PUL = ctypes.POINTER(ctypes.c_ulong)

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", ctypes.c_ushort),
            ("wScan", ctypes.c_ushort),
            ("dwFlags", ctypes.c_ulong),
            ("time", ctypes.c_ulong),
            ("dwExtraInfo", PUL),
        ]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [
            ("uMsg", ctypes.c_ulong),
            ("wParamL", ctypes.c_short),
            ("wParamH", ctypes.c_ushort),
        ]

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", ctypes.c_long),
            ("dy", ctypes.c_long),
            ("mouseData", ctypes.c_ulong),
            ("dwFlags", ctypes.c_ulong),
            ("time", ctypes.c_ulong),
            ("dwExtraInfo", PUL),
        ]

    class INPUT_UNION(ctypes.Union):
        _fields_ = [
            ("ki", KEYBDINPUT),
            ("mi", MOUSEINPUT),
            ("hi", HARDWAREINPUT),
        ]

    class INPUT(ctypes.Structure):
        _fields_ = [
            ("type", ctypes.c_ulong),
            ("union", INPUT_UNION),
        ]

    INPUT_KEYBOARD = 1
    KEYEVENTF_EXTENDEDKEY = 0x0001
    KEYEVENTF_KEYUP = 0x0002
    KEYEVENTF_UNICODE = 0x0004
    KEYEVENTF_SCANCODE = 0x0008

    VK_SHIFT = 0x10
    VK_CONTROL = 0x11
    VK_ESCAPE = 0x1B
    VK_RETURN = 0x0D
    VK_MBUTTON = 0x04
    VK_C = 0x43

    # 明確指定 ctypes 型別，避免 VkKeyScanW 收到整數時出現：
    # TypeError: unicode string expected instead of int instance
    user32.VkKeyScanW.argtypes = [ctypes.c_wchar]
    user32.VkKeyScanW.restype = ctypes.c_short
    user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
    user32.GetAsyncKeyState.restype = ctypes.c_short

    def vk_from_char(ch: str) -> Tuple[int, int]:
        """使用 VkKeyScanW 取得 virtual-key 與 shift 狀態。"""
        if not ch:
            return 0, 0
        ch = ch[0]
        vk_and_state = user32.VkKeyScanW(ch)
        if vk_and_state == -1:
            return 0, 0
        vk = vk_and_state & 0xFF
        state = (vk_and_state >> 8) & 0xFF
        return vk, state

    def make_key_input(vk: int, flags: int = 0) -> INPUT:
        ki = KEYBDINPUT()
        ki.wVk = int(vk)
        ki.wScan = 0
        ki.dwFlags = flags
        ki.time = 0
        ki.dwExtraInfo = None
        inp = INPUT()
        inp.type = INPUT_KEYBOARD
        inp.union.ki = ki
        return inp

    def send_inputs(inputs: List[INPUT]):
        if not inputs:
            return
        n = len(inputs)
        arr = (INPUT * n)(*inputs)
        sent = user32.SendInput(n, ctypes.byref(arr), ctypes.sizeof(INPUT))
        if sent != n:
            raise OSError(f"SendInput 失敗：要求 {n} 個，實際送出 {sent} 個")

    def press_vk(vk: int):
        inputs = [make_key_input(vk, 0), make_key_input(vk, KEYEVENTF_KEYUP)]
        send_inputs(inputs)

    def press_vk_batch(vk_list: List[int]):
        valid_vks = [int(vk) for vk in vk_list if vk]
        if not valid_vks:
            return
        inputs: List[INPUT] = []
        for vk in valid_vks:
            inputs.append(make_key_input(vk, 0))
        for vk in valid_vks:
            inputs.append(make_key_input(vk, KEYEVENTF_KEYUP))
        send_inputs(inputs)

    def send_char(ch: str):
        vk, state = vk_from_char(ch)
        if vk == 0:
            ki_down = KEYBDINPUT(0, ord(ch), KEYEVENTF_UNICODE, 0, None)
            ki_up = KEYBDINPUT(0, ord(ch), KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, None)
            inp1 = INPUT()
            inp1.type = INPUT_KEYBOARD
            inp1.union.ki = ki_down
            inp2 = INPUT()
            inp2.type = INPUT_KEYBOARD
            inp2.union.ki = ki_up
            send_inputs([inp1, inp2])
            return

        need_shift = (state & 1) != 0
        inputs: List[INPUT] = []
        if need_shift:
            inputs.append(make_key_input(VK_SHIFT, 0))
        inputs.append(make_key_input(vk, 0))
        inputs.append(make_key_input(vk, KEYEVENTF_KEYUP))
        if need_shift:
            inputs.append(make_key_input(VK_SHIFT, KEYEVENTF_KEYUP))
        send_inputs(inputs)

    def press_ctrl_c():
        send_inputs(
            [
                make_key_input(VK_CONTROL, 0),
                make_key_input(VK_C, 0),
                make_key_input(VK_C, KEYEVENTF_KEYUP),
                make_key_input(VK_CONTROL, KEYEVENTF_KEYUP),
            ]
        )

    def press_escape():
        press_vk(VK_ESCAPE)

    def press_enter():
        press_vk(VK_RETURN)

    def is_middle_button_down() -> bool:
        return bool(user32.GetAsyncKeyState(VK_MBUTTON) & 0x8000)

else:
    try:
        import keyboard as _keyboard_non_windows
    except Exception:
        _keyboard_non_windows = None

    def send_char(ch: str):
        if _keyboard_non_windows is None:
            raise RuntimeError("非 Windows 平台且無 keyboard 模組，無法送鍵。")
        _keyboard_non_windows.send(ch)

    def press_vk_batch(vk_list: List[int]):
        for vk in vk_list:
            if vk:
                send_char(chr(vk))

    def press_ctrl_c():
        if _keyboard_non_windows is None:
            raise RuntimeError("非 Windows 平台且無 keyboard 模組，無法送 Ctrl+C。")
        _keyboard_non_windows.send("ctrl+c")

    def press_escape():
        if _keyboard_non_windows is None:
            raise RuntimeError("非 Windows 平台且無 keyboard 模組，無法送 Esc。")
        _keyboard_non_windows.send("esc")

    def press_enter():
        if _keyboard_non_windows is None:
            raise RuntimeError("非 Windows 平台且無 keyboard 模組，無法送 Enter。")
        _keyboard_non_windows.send("enter")

    def is_middle_button_down() -> bool:
        return False


# 試圖載入 keyboard 模組（用來監聽熱鍵/開始/停止）
try:
    import keyboard as _keyboard
    keyboard_module = _keyboard
except Exception:
    keyboard_module = None
    print("警告: 找不到 'keyboard' 套件 某些功能可能不可用")
    print("警告: 無法進行按鍵偵測。")


class KeySender:
    def __init__(self):
        self.windows = IS_WINDOWS

    def send(self, key: str):
        if not key:
            return
        if self.windows:
            for ch in key:
                send_char(ch)
        else:
            send_char(key)

    def send_simultaneous(self, keys: List[str]):
        if not keys:
            return
        if self.windows:
            vk_list = []
            unresolved = []
            for key in keys:
                vk, _state = vk_from_char(key)
                if vk:
                    vk_list.append(vk)
                else:
                    unresolved.append(key)

            try:
                if vk_list:
                    press_vk_batch(vk_list)
                for key in unresolved:
                    send_char(key)
            except Exception:
                for key in keys:
                    try:
                        send_char(key)
                    except Exception as exc:
                        print(f"退回逐鍵送鍵失敗 key={key} err={exc}")
        else:
            for key in keys:
                send_char(key)


input_sender = KeySender()
perf_ns = time.perf_counter_ns

# ---------- 外部歌曲選擇 / UI ----------
def show_song_selected_feedback(song: Dict[str, Any]):
    """短暫顯示成功選取的歌曲，讓 Esc/Enter 動作有明確視覺回饋。"""
    try:
        import tkinter as tk

        root = tk.Tk()
        root.title("歌曲選取")
        root.geometry("520x110")
        root.resizable(False, False)
        root.attributes("-topmost", True)
        root.configure(bg="#171717")

        _MAX_TITLE_LEN = 6

        title = song.get("title") or song.get("title_en") or "未知歌曲"
        label = tk.Label(
            root,
            text = f"✓ 已選取 {title[:_MAX_TITLE_LEN] + '...' if len(title) > _MAX_TITLE_LEN else title}\n用下拉選單選擇難度 約5秒內會出現",
            fg="white",
            bg="#171717",
            font=("Microsoft JhengHei", 18, "bold"),
            justify="center",
        )
        label.pack(expand=True, fill="both", padx=10, pady=10)
        root.update_idletasks()
        root.geometry(
            f"520x110+{max((root.winfo_screenwidth() - 520) // 2, 0)}+"
            f"{max((root.winfo_screenheight() - 110) // 2, 0)}"
        )
        root.update()
        time.sleep(1.8)
    except Exception as exc:
        print(f"視覺提示顯示失敗：{exc}")
    finally:
        try:
            root.destroy()
        except Exception:
            pass


def _course_display_name(course: str) -> str:
    mapping = {
        "easy": "簡單",
        "normal": "普通",
        "hard": "困難",
        "oni": "魔王",
        "ura": "裏魔王",
        "edit": "裏魔王",
    }
    return mapping.get(course.lower(), course)


def detect_tja_courses(tja_source: bytes) -> List[str]:
    """只讀記憶體內 TJA，找出實際存在的 COURSE。"""
    text = tja_source.decode("utf-8-sig", errors="ignore")
    found: List[str] = []
    course_map = {
        "0": "Easy",
        "easy": "Easy",
        "1": "Normal",
        "normal": "Normal",
        "2": "Hard",
        "hard": "Hard",
        "3": "Oni",
        "oni": "Oni",
        "4": "Edit",
        "edit": "Edit",
        "ura": "Edit",
    }

    for raw in text.splitlines():
        line = strip_inline_comment(raw)
        if not line.upper().startswith("COURSE:"):
            continue
        raw_course = line.split(":", 1)[1].strip()
        normalized = course_map.get(raw_course.lower(), raw_course)
        if normalized not in found:
            found.append(normalized)

    return found


def select_course_ui(song: Dict[str, Any], available_courses: List[str]) -> Optional[str]:
    """強制建立前景 UI，僅顯示下載後 TJA 實際存在的難度。"""
    if not available_courses:
        print("⚠️ TJA 中沒有找到任何 COURSE，無法選擇難度。")
        return None

    try:
        import tkinter as tk
        from tkinter import ttk

        result: Dict[str, Optional[str]] = {"course": None}
        root = tk.Tk()
        root.title("選擇太鼓難度")
        root.geometry("520x230")
        root.resizable(False, False)
        root.attributes("-topmost", True)
        root.protocol("WM_DELETE_WINDOW", root.destroy)

        title_text = song.get("title") or song.get("title_en") or "未知歌曲"
        tk.Label(
            root,
            text="已載入歌曲，請選擇難度",
            font=("Microsoft JhengHei", 15, "bold"),
        ).pack(pady=(18, 6))
        tk.Label(
            root,
            text=title_text,
            font=("Microsoft JhengHei", 11),
            wraplength=470,
        ).pack(pady=(0, 12))

        combo_values = [
            f"{course} ({_course_display_name(course)})" for course in available_courses
        ]
        selected_value = tk.StringVar(value=combo_values[0])
        combo = ttk.Combobox(
            root,
            textvariable=selected_value,
            values=combo_values,
            state="readonly",
            width=42,
        )
        combo.pack(pady=6)

        def confirm():
            raw = selected_value.get().split(" (", 1)[0].strip()
            result["course"] = raw
            root.destroy()

        ttk.Button(root, text="確定", command=confirm).pack(pady=18)
        combo.focus_set()
        root.focus_force()
        root.grab_set()
        combo.event_generate("<<ComboboxSelected>>")
        root.update_idletasks()
        root.deiconify()
        root.attributes("-topmost", True)
        root.after(50, root.focus_force)
        root.after(50, combo.focus_set)
        root.wait_window()
        return result["course"]
    except Exception as exc:
        print(f"⚠️ 難度 UI 建立失敗：{exc}")
        return None


def verify_tja_exists(url: str) -> bool:
    """先用 HEAD 確認 URL 存在，不先下載內容。"""
    try:
        response = requests.head(
            url,
            allow_redirects=True,
            timeout=(5, 10),
        )
        if 200 <= response.status_code < 300:
            return True
        print(f"⚠️ TJA URL 無法確認存在：HTTP {response.status_code}")
        return False
    except requests.RequestException as exc:
        print(f"⚠️ TJA URL 檢查失敗：{exc}")
        return False


def download_tja_to_memory(url: str) -> Optional[bytes]:
    """確認存在後才下載 TJA，下載後只保留 bytes 在記憶體。"""
    if not verify_tja_exists(url):
        return None

    try:
        print("⬇️ 正在下載 TJA 到記憶體...")
        response = requests.get(url, timeout=(5, 30))
        response.raise_for_status()
        data = response.content
        print(f"✅ TJA 載入完成：{len(data):,} bytes")
        return data
    except requests.RequestException as exc:
        print(f"❌ TJA 下載失敗：{exc}")
        return None


def wait_for_song_selection(songs: List[Dict[str, Any]], shutdown_event: threading.Event) -> Optional[Dict[str, Any]]:
    """等待中鍵；中鍵事件只在目前不演奏時處理。"""
    if not IS_WINDOWS:
        print("⚠️ 目前的中鍵選歌流程使用 Windows GetAsyncKeyState；非 Windows 無法啟用。")
        return None

    print("🖱️ 等待中鍵選歌...")
    previous_middle = False

    while not shutdown_event.is_set():
        pressed = is_middle_button_down()
        if pressed and not previous_middle:
            print("🖱️ 偵測到中鍵 → 模擬 Ctrl+C，讀取目前選取歌曲...")
            try:
                press_ctrl_c()
            except Exception as exc:
                print(f"⚠️ 模擬 Ctrl+C 失敗：{exc}")
                previous_middle = pressed
                time.sleep(0.08)
                continue

            time.sleep(0.06)
            clipboard_text = read_clipboard_text()
            song = find_song_by_clipboard(songs, clipboard_text)

            pyperclip.copy("目前無選取任何歌曲")

            if song is None:
                print(f"❌ JSON 中找不到歌曲：{clipboard_text!r}")
            else:
                print(
                    f"✅ 找到歌曲：{song.get('title') or song.get('title_en')} "
                    f"(id={song.get('id')})"
                )
                show_song_selected_feedback(song)
                try:
                    # 成功命中後再送 UI 控制鍵，避免沒找到歌曲時誤切換介面。
                    press_escape()
                    time.sleep(0.03)
                    press_enter()
                except Exception as exc:
                    print(f"⚠️ Esc / Enter 模擬失敗：{exc}")
                return song

        previous_middle = pressed
        time.sleep(0.02)

    return None


# ---------- Parsing TJA ----------
def strip_inline_comment(s: str) -> str:
    return re.sub(r"//.*", "", s).strip()


def preprocess_branch_blocks(lines: List[str]) -> List[str]:
    """
    展開 #BRANCHSTART 區塊
    分歧優先順序：E → M → N
    """
    out: List[str] = []
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]
        up = line.upper().strip()

        if up.startswith("#BRANCHSTART"):
            i += 1
            branches = {"N": [], "M": [], "E": []}
            current = None

            while i < n:
                cur = lines[i]
                up2 = cur.upper().strip()

                if up2 == "#N":
                    current = "N"
                elif up2 == "#M":
                    current = "M"
                elif up2 == "#E":
                    current = "E"
                elif up2.startswith("#BRANCHEND"):
                    i += 1
                    break
                else:
                    if current is not None:
                        branches[current].append(cur)

                i += 1

            if branches["M"]:
                chosen = branches["M"]
            elif branches["E"]:
                chosen = branches["E"]
            else:
                chosen = branches["N"]

            out.extend(chosen)
            continue

        out.append(line)
        i += 1

    return out


def _coerce_tja_lines(source: Any) -> List[str]:
    if isinstance(source, bytes):
        text = source.decode("utf-8-sig", errors="ignore")
    elif isinstance(source, str):
        # 保留原本「傳入檔案路徑」的能力；歌曲流程則直接傳 bytes，不會寫檔。
        with open(source, "r", encoding="utf-8-sig", errors="ignore") as f:
            text = f.read()
    else:
        raise TypeError("TJA source 必須是 bytes 或檔案路徑 str")
    return [strip_inline_comment(line) for line in text.splitlines() if strip_inline_comment(line)]


def parse_tja(source: Any, desired_course: str = None):
    """
    解析 TJA，回傳 dict: { offset_ms, initial_bpm, events }
    支援直接從記憶體 bytes 讀取，不建立暫存 TJA 檔案。
    """
    if isinstance(source, bytes):
        print("正在讀取記憶體中的 TJA...")
    else:
        print(f"正在讀取檔案: {source}")

    try:
        lines = _coerce_tja_lines(source)
    except (FileNotFoundError, OSError) as exc:
        print(f"錯誤：找不到或無法讀取 TJA：{exc}")
        return {"events": [], "initial_bpm": 120, "offset_ms": 0, "balloons": []}
    except Exception as exc:
        print(f"錯誤：TJA 讀取失敗：{exc}")
        return {"events": [], "initial_bpm": 120, "offset_ms": 0, "balloons": []}

    # --- header (Global) ---
    offset_s = 0.0
    initial_bpm = 120.0
    balloons: List[int] = []

    for l in lines:
        upper_l = l.upper()
        if upper_l.startswith("COURSE:"):
            break
        if upper_l.startswith("OFFSET:"):
            try:
                offset_s = float(l.split(":", 1)[1])
            except Exception:
                pass
        elif upper_l.startswith("BPM:"):
            try:
                initial_bpm = float(l.split(":", 1)[1])
            except Exception:
                pass
        elif upper_l.startswith("BALLOON:"):
            nums = re.findall(r"\d+", l)
            balloons.extend([int(n) for n in nums])

    # --- 偵測 COURSE 列表 ---
    course_map = {
        "0": "Easy",
        "Easy": "Easy",
        "1": "Normal",
        "Normal": "Normal",
        "2": "Hard",
        "Hard": "Hard",
        "3": "Oni",
        "Oni": "Oni",
        "4": "Edit",
        "Edit": "Edit",
        "Ura": "Edit",
    }

    found_courses = []
    for l in lines:
        if l.upper().startswith("COURSE:"):
            val = l.split(":", 1)[1].strip()
            mapped = course_map.get(val, val)
            if mapped not in found_courses:
                found_courses.append(mapped)

    if desired_course is None or (isinstance(desired_course, str) and desired_course.strip() == ""):
        preferred_order = ["Edit", "Oni", "Hard", "Normal", "Easy"]
        chosen = None
        for p in preferred_order:
            if p in found_courses:
                chosen = p
                break
        if chosen is None and "Edit" in found_courses:
            chosen = "Edit"
        if chosen is None:
            chosen = "Oni"
        target_course_norm = chosen.lower()
        print(f"自動偵測 COURSE: {found_courses} → 選擇 '{chosen}'")
    else:
        target_course_norm = course_map.get(str(desired_course), str(desired_course)).lower()

    # --- 找 COURSE 區塊 & 讀取 Course 內的 Metadata ---
    target_lines = []
    target_course_norm = target_course_norm.lower()
    current_course = None
    capturing = False
    found_course_block = False

    course_balloons_found = False
    temp_course_balloons = []

    desired_player_str = None
    if PLAYER is not None:
        desired_player_str = str(PLAYER)

    for l in lines:
        upper_l = l.upper()

        if upper_l.startswith("COURSE:"):
            val = l.split(":", 1)[1].strip()
            current_course = course_map.get(val, val).lower()
            capturing = False
            continue

        if current_course == target_course_norm:
            found_course_block = True

            if upper_l.startswith("BALLOON:"):
                if not course_balloons_found:
                    temp_course_balloons = []
                    course_balloons_found = True

                nums = re.findall(r"\d+", l)
                temp_course_balloons.extend([int(n) for n in nums])
                continue

            if upper_l.startswith("#START"):
                m = re.match(r"#START(?:\s+P?([12]))?\b", upper_l, flags=re.IGNORECASE)
                block_player = m.group(1) if m else None

                if block_player is None:
                    capturing = True
                else:
                    if desired_player_str is not None and block_player == desired_player_str:
                        capturing = True
                    else:
                        capturing = False
                continue

            if upper_l.startswith("#END"):
                capturing = False
                continue

            if capturing:
                target_lines.append(l)

    if course_balloons_found:
        balloons = temp_course_balloons

    if not found_course_block:
        print(f"警告: 譜面可能有錯誤")
    if not target_lines:
        print("警告: 譜面可能有錯誤")

    processed_target_lines = preprocess_branch_blocks(target_lines)

    # --- 解析音符 (保持原邏輯) ---
    events: List[Tuple[float, str]] = []
    cur_bpm = initial_bpm
    meas_num = 4
    meas_den = 4
    song_time_s = 0.0
    current_measure_segments = []

    for line in processed_target_lines:
        line = line.strip()
        if not line:
            continue

        if line.startswith("#"):
            cmd = line.upper()
            if cmd.startswith("#BPMCHANGE"):
                try:
                    cur_bpm = float(line.split()[1])
                except Exception:
                    pass
            elif cmd.startswith("#MEASURE"):
                try:
                    mn, md = line.split()[1].split("/")
                    meas_num = int(mn)
                    meas_den = int(md)
                except Exception:
                    pass
            elif cmd.startswith("#DELAY"):
                try:
                    song_time_s += float(line.split()[1])
                except Exception:
                    pass
            continue

        temp_str = line
        while "," in temp_str:
            segment_str, temp_str = temp_str.split(",", 1)
            current_measure_segments.append((segment_str, cur_bpm))

            full_notes_str = ""
            for seg_s, _ in current_measure_segments:
                full_notes_str += "".join(c for c in seg_s if c.isalnum())
            total_notes = len(full_notes_str)

            if total_notes > 0:
                measure_beats = 4.0 * (meas_num / meas_den)
                beats_per_char = measure_beats / total_notes

                for seg_s, seg_bpm in current_measure_segments:
                    clean_seg = [c for c in seg_s if c.isalnum()]
                    if not clean_seg:
                        continue
                    char_duration = beats_per_char * (60.0 / seg_bpm)
                    for char in clean_seg:
                        if char != "0":
                            events.append((song_time_s, char))
                        song_time_s += char_duration
            else:
                measure_beats = 4.0 * (meas_num / meas_den)
                duration = measure_beats * (60.0 / cur_bpm)
                song_time_s += duration

            current_measure_segments = []

        if temp_str:
            current_measure_segments.append((temp_str, cur_bpm))

    final_events: List[Tuple[float, Any]] = []
    for t, ch in events:
        final_t = t - offset_s
        if final_t >= 0:
            final_events.append((final_t, ch))
    final_events.sort(key=lambda x: x[0])

    return {
        "offset_ms": offset_s * 1000,
        "initial_bpm": initial_bpm,
        "events": final_events,
        "balloons": balloons,
    }


# ---------- TEMPO 調整 ----------
TEMPO_ADJUST_MS = 0.0
tempo_lock = threading.Lock()


def adjust_tempo_ms(delta_ms: float):
    global TEMPO_ADJUST_MS
    with tempo_lock:
        TEMPO_ADJUST_MS += delta_ms
        print(f"音符偏移對準已調整 {delta_ms:+.1f} ms")


# ---------- 自動連打事件生成 ----------
def augment_events_with_auto_hits(
    events: List[Tuple[float, str]],
    balloons_param: List[int],
    hit_hz: float = AUTO_HIT_HZ,
    default_dur: float = AUTO_HIT_DEFAULT_DUR_S,
):
    """
    掃過原始 events，對於遇到 '5','6','7' 的位置，產生從該時間到下一個 '8' 之前的自動連打時間戳（don）
    對於 5/6，會等到後面第一個 0 才開始。
    """
    new_events: List[Tuple[float, Any]] = []
    n = len(events)

    for ev in events:
        new_events.append(ev)

    balloon_counter = 0
    balloon_hits: Dict[int, int] = {}
    balloon_mapping: Dict[int, int] = {}
    balloon_expected: Dict[int, int] = {}

    for i, (t, ch) in enumerate(events):
        if ch not in ("5", "6", "7"):
            continue

        end_time = None
        for j in range(i + 1, n):
            if events[j][1] == "8":
                end_time = events[j][0]
                break
        if end_time is None:
            end_time = t + default_dur

        if ch in ("5", "6"):
            start_time = None
            for j in range(i + 1, n):
                if events[j][1] == "0":
                    start_time = events[j][0]
                    break
            if start_time is None:
                start_time = t
            balloon_idx = None

            interval = 1.0 / hit_hz
            k = 0
            while True:
                ts = start_time + k * interval
                if ts >= end_time:
                    break
                payload = {
                    "type": "auto_hit",
                    "origin": ch,
                    "balloon_idx": balloon_idx,
                }
                new_events.append((ts, payload))
                k += 1

        elif ch == "7":
            balloon_counter += 1
            balloon_idx = balloon_counter

            if len(balloons_param) >= balloon_idx:
                raw_target = balloons_param[balloon_idx - 1]
            else:
                raw_target = None

            if raw_target is not None:
                try:
                    target_hits = max(0, int(raw_target) + int(BALLOON_HIT_ADJUST))
                except Exception:
                    target_hits = None
            else:
                target_hits = None

            if target_hits == 0:
                balloon_hits[balloon_idx] = 0
                balloon_mapping[balloon_idx] = raw_target
                balloon_expected[balloon_idx] = 0
                continue

            if raw_target is not None:
                balloon_mapping[balloon_idx] = raw_target
                balloon_expected[balloon_idx] = target_hits if target_hits is not None else 0
            else:
                balloon_mapping[balloon_idx] = None

            balloon_hits[balloon_idx] = 0

            interval = 1.0 / hit_hz
            if target_hits is not None:
                # 保留原程式：明確有目標值時依目標數產生，並受時間區間限制。
                start_time = t
                for k in range(target_hits):
                    ts = start_time + k * interval
                    if ts >= end_time:
                        break
                    payload = {
                        "type": "auto_hit",
                        "origin": ch,
                        "balloon_idx": balloon_idx,
                    }
                    new_events.append((ts, payload))
            else:
                start_time = t
                k = 0
                while True:
                    ts = start_time + k * interval
                    if ts >= end_time:
                        break
                    payload = {
                        "type": "auto_hit",
                        "origin": ch,
                        "balloon_idx": balloon_idx,
                    }
                    new_events.append((ts, payload))
                    k += 1

    new_events.sort(key=lambda x: x[0])
    return new_events, balloon_hits, balloon_mapping, balloon_expected


# ---------- 排程與執行 ----------
def wait_until_ns(target_ns, stop_event=None, shutdown_event=None):
    while True:
        if stop_event is not None and stop_event.is_set():
            return False
        if shutdown_event is not None and shutdown_event.is_set():
            return False

        now = perf_ns()
        remaining_ns = target_ns - now
        if remaining_ns <= 0:
            return True
        if remaining_ns > 5_000_000:
            sleep_s = (remaining_ns - 1_000_000) / 1_000_000_000.0
            time.sleep(max(0.0005, sleep_s))
        else:
            time.sleep(0)
            continue


def start_stop_listener(start_event: threading.Event, stop_event: threading.Event, shutdown_event: threading.Event):
    if keyboard_module is None:
        # print("警告: 某python套件不可用 請檢察")
        return

    s_pressed_once = False
    while not shutdown_event.is_set():
        try:
            keyboard_module.wait("s")
        except Exception:
            return

        if shutdown_event.is_set():
            return

        if not s_pressed_once:
            start_event.set()
            s_pressed_once = True
            print("偵測到 's' → 開始播放。")
        else:
            stop_event.set()
            print("偵測到第二次 's' → 停止播放並回到選歌。")
            return


def esc_shutdown_listener(shutdown_event: threading.Event, current_stop_event_ref: Dict[str, Optional[threading.Event]]):
    """全程監聽 Esc；0.3 秒內雙擊即結束整個程式。"""
    if keyboard_module is None:
        # print("警告：keyboard 模組不可用，雙擊 Esc 關閉功能不可用。")
        return

    last_esc = 0.0
    while not shutdown_event.is_set():
        try:
            keyboard_module.wait("esc")
        except Exception:
            return

        now = time.monotonic()
        if now - last_esc <= 0.3:
            print("⛔ 偵測到 0.3 秒內雙擊 Esc → 停止整個程式。")
            shutdown_event.set()
            current_stop_event = current_stop_event_ref.get("event")
            if current_stop_event is not None:
                current_stop_event.set()
            return
        last_esc = now


def play_events(
    events: List[Tuple[float, Any]],
    keymap=None,
    dry_run=False,
    lead_ms=0.0,
    stop_event: threading.Event = None,
    shutdown_event: threading.Event = None,
    alternate_hands=True,
    start_with="right",
):
    """支援 auto_hit payload 的 play_events；其原本按鍵/節奏邏輯維持。"""
    if keymap is None:
        keymap = {"1": KEY_DON, "3": KEY_DON, "2": KEY_KA, "4": KEY_KA, "5": KEY_DON}

    right_hand_map = {"1": "f", "3": "f", "2": "d", "4": "d", "5": "f", "6": "f"}
    left_hand_map = {"1": "j", "3": "j", "2": "k", "4": "k", "5": "j", "6": "j"}

    if start_with not in ("right", "left"):
        start_with = "right"

    if dry_run:
        print("DRY RUN: 只列印前 200 個事件（若過多）")
        hand = start_with
        with tempo_lock:
            _adj_ms = TEMPO_ADJUST_MS
        _ = hand, _adj_ms, perf_ns()
        for idx, (t, payload) in enumerate(events[:200]):
            rel_ms = t * 1000.0
            if isinstance(payload, str):
                print(f"[{idx:04}] +{rel_ms:.3f} ms -> note '{payload}' (orig)")
            elif payload.get("type") == "auto_hit":
                print(
                    f"[{idx:04}] +{rel_ms:.3f} ms -> AUTO_HIT from "
                    f"'{payload['origin']}' balloon_idx={payload['balloon_idx']}"
                )
            else:
                print(f"[{idx:04}] +{rel_ms:.3f} ms -> payload {payload}")
        if len(events) > 200:
            print("... (省略其餘事件)")
        return

    if input_sender is None:
        print("警告: 無法進行自動演奏。")
        return

    try:
        now = perf_ns()
        if lead_ms > 0:
            start_time_ns = now + int(lead_ms * 1_000_000)
        else:
            if events:
                first_t = events[0][0]
                margin_ns = 2_000_000
                start_time_ns = now - int(first_t * 1_000_000_000) + margin_ns
            else:
                start_time_ns = now

        print(
            f"準備開始：現在 perf_counter_ns()={perf_ns()}, "
            f"start_time_ns={start_time_ns}, 事件數={len(events)}"
        )

        current_hand = start_with
        balloon_hits: Dict[int, int] = {}

        for idx, (t, payload) in enumerate(events):
            if stop_event is not None and stop_event.is_set():
                print("偵測到停止指令，提前終止播放。")
                break
            if shutdown_event is not None and shutdown_event.is_set():
                print("偵測到雙擊 Esc，提前終止播放。")
                break

            with tempo_lock:
                adj_ms = TEMPO_ADJUST_MS

            target_ns = start_time_ns + int((t + adj_ms / 1000.0) * 1_000_000_000)
            ok = wait_until_ns(
                target_ns,
                stop_event=stop_event,
                shutdown_event=shutdown_event,
            )
            if not ok:
                print("播放在等待期間被中斷。")
                break

            if isinstance(payload, str):
                ch = payload
                if ch in ("5", "6", "7"):
                    continue

                if ch == "3":
                    keys_to_send = ["f", "j"]
                elif ch == "4":
                    keys_to_send = ["d", "k"]
                else:
                    if alternate_hands:
                        key = (
                            right_hand_map.get(ch)
                            if current_hand == "right"
                            else left_hand_map.get(ch)
                        )
                        keys_to_send = [key]
                        current_hand = "left" if current_hand == "right" else "right"
                    else:
                        keys_to_send = [keymap.get(ch)]

                if len(keys_to_send) == 1:
                    key = keys_to_send[0]
                    if key is not None:
                        try:
                            input_sender.send(key)
                        except Exception as exc:
                            print(
                                f"送鍵失敗 idx={idx} ch={ch} key={key} err={exc}"
                            )
                else:
                    try:
                        input_sender.send_simultaneous(keys_to_send)
                    except Exception:
                        for key in keys_to_send:
                            try:
                                input_sender.send(key)
                            except Exception as exc2:
                                print(
                                    f"退回逐鍵送鍵失敗 key={key} err={exc2}"
                                )

            else:
                payload = dict(payload)
                if payload.get("type") != "auto_hit":
                    continue

                balloon_idx = payload.get("balloon_idx")
                key = KEY_DON if current_hand == "right" else KEY_DON_LEFT
                current_hand = "left" if current_hand == "right" else "right"

                try:
                    input_sender.send(key)
                except Exception as exc:
                    print(
                        f"自動連打送鍵失敗 time={t:.6f} key={key} err={exc}"
                    )

                if balloon_idx is not None:
                    balloon_hits[balloon_idx] = balloon_hits.get(balloon_idx, 0) + 1

        if balloon_hits:
            pass
            # print("氣球擊打統計 (balloon_index -> hits):")
            # for bi, cnt in sorted(balloon_hits.items()):
            #     print(f"  balloon #{bi} -> {cnt} hits")

    except KeyboardInterrupt:
        print("使用者中斷 (KeyboardInterrupt)，播放停止。")
        return


# ---------- 主流程 ----------
def main():
    if not IS_WINDOWS:
        print("此版本的中鍵選歌 / SendInput 流程需要 Windows。")
        return

    try:
        songs = fetch_song_list()
    except requests.RequestException as exc:
        print(f"❌ 歌曲列表取得失敗：{exc}")
        return
    except Exception as exc:
        print(f"❌ 歌曲列表整理失敗：{exc}")
        return

    if not songs:
        print("❌ 歌曲列表為空，程式結束。")
        return

    shutdown_event = threading.Event()
    current_stop_event_ref: Dict[str, Optional[threading.Event]] = {"event": None}

    esc_thread = threading.Thread(
        target=esc_shutdown_listener,
        args=(shutdown_event, current_stop_event_ref),
        daemon=True,
    )
    esc_thread.start()

    hotkey_refs = []
    if keyboard_module is not None:
        try:
            hotkey_refs.append(keyboard_module.add_hotkey("x", lambda: adjust_tempo_ms(-7.5)))
            hotkey_refs.append(keyboard_module.add_hotkey("z", lambda: adjust_tempo_ms(+7.5)))
        except Exception as exc:
            print("註冊 z/x hotkey 失敗（可能權限問題）。熱鍵功能不可用。", exc)

    print("流程：載入歌曲 → 中鍵選歌 → 確認 TJA → 記憶體解析 → 選難度 → S 開始。")
    print("停止單首：第二次按 S；停止整個程式：0.3 秒內雙擊 Esc。")

    try:
        while not shutdown_event.is_set():
            # 1. 等待中鍵，這個階段才允許選歌。
            song = wait_for_song_selection(songs, shutdown_event)
            if shutdown_event.is_set():
                break
            if song is None:
                time.sleep(0.05)
                continue

            # 2. 先確認 URL，再真正下載，避免 UI 無條件卡在下載流程。
            tja_url = song["url"]
            print(f"🔗 TJA：{tja_url}")
            tja_data = download_tja_to_memory(tja_url)
            if tja_data is None:
                print("回到中鍵選歌。")
                continue

            # 3. 下載完成後才讀取實際 TJA 難度，UI 只提供存在的難度。
            available_courses = detect_tja_courses(tja_data)
            if not available_courses:
                print("❌ TJA 沒有可用 COURSE；回到中鍵選歌。")
                continue
            print(f"🎵 可用難度：{', '.join(available_courses)}")

            selected_course = select_course_ui(song, available_courses)
            if shutdown_event.is_set():
                break
            if not selected_course:
                print("未選擇難度，回到中鍵選歌。")
                continue

            # 4. 直接用記憶體中的 bytes 解析，不寫入磁碟。
            print(f"🎯 已選擇難度：{selected_course}")
            print("解析 TJA 檔案中...")
            parsed = parse_tja(tja_data, desired_course=selected_course)
            events = parsed["events"]
            balloons_param = parsed.get("balloons", [])
            print(
                "✅ 解析完成。"
            )

            if len(events) == 0:
                print("沒有可播放的事件。回到中鍵選歌。")
                continue

            augmented_events, balloon_hits_template, balloon_mapping, balloon_expected = augment_events_with_auto_hits(
                events,
                balloons_param,
            )
            _ = balloon_hits_template

            # print("前 10 個事件樣本 (相對於歌曲開始，含 OFFSET)：")
            # for i, (t, payload) in enumerate(augmented_events[:10]):
            #     if isinstance(payload, str):
            #         print(f"  [{i}] +{t * 1000.0:.3f} ms -> note '{payload}'")
            #     elif payload.get("type") == "auto_hit":
            #         print(
            #             f"  [{i}] +{t * 1000.0:.3f} ms -> AUTO_HIT from "
            #             f"'{payload['origin']}' balloon_idx={payload['balloon_idx']}"
            #         )
            #     else:
            #         print(f"  [{i}] +{t * 1000.0:.3f} ms -> payload {payload}")

            # if balloon_mapping:
            #     print("氣球設定 mapping (index -> 原譜面數)：")
            #     for bi, val in sorted(balloon_mapping.items()):
            #         expected = balloon_expected.get(bi, None)
            #         print(f"  #{bi}: raw={val} -> expected_after_adjust={expected}")

            # 5. 選好難度後，才開啟本首的 S 開始/停止流程。
            start_event = threading.Event()
            stop_event = threading.Event()
            current_stop_event_ref["event"] = stop_event

            listener_thread = threading.Thread(
                target=start_stop_listener,
                args=(start_event, stop_event, shutdown_event),
                daemon=True,
            )
            listener_thread.start()

            print("說明：按 's' 開始演奏，再按一次 's' 停止本首並回到選歌。")
            print("更多熱鍵：'z' = 每次快 7.5 ms，'x' = 每次慢 7.5 ms（會累加）。")
            print("等待 's' 開始...")

            try:
                while not (start_event.is_set() or stop_event.is_set() or shutdown_event.is_set()):
                    time.sleep(0.05)
            except KeyboardInterrupt:
                shutdown_event.set()
                break

            if shutdown_event.is_set():
                break
            if stop_event.is_set() and not start_event.is_set():
                current_stop_event_ref["event"] = None
                continue

            lead_ms = LEAD_MS
            if lead_ms is None:
                lead_ms = 1500.0

            print(
                f"將在按下 's' 的時刻開始（LEAD_MS={lead_ms} ms 表示的語意已如說明）。"
                f" DRY_RUN={DRY_RUN}"
            )
            play_events(
                augmented_events,
                dry_run=DRY_RUN,
                lead_ms=lead_ms,
                stop_event=stop_event,
                shutdown_event=shutdown_event,
                alternate_hands=True,
                start_with="right",
            )

            current_stop_event_ref["event"] = None

            if shutdown_event.is_set():
                break

            print("✅ 播放結束或已被停止。重新允許中鍵選歌。")
            time.sleep(0.1)

    except KeyboardInterrupt:
        print("使用者中斷，退出。")
        shutdown_event.set()
    finally:
        current_stop_event = current_stop_event_ref.get("event")
        if current_stop_event is not None:
            current_stop_event.set()

        if keyboard_module is not None:
            try:
                keyboard_module.remove_all_hotkeys()
            except Exception:
                pass

        print("程式結束。")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("發生例外：", exc)
        raise
