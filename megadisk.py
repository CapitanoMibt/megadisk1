import os
import sys
import json
import time
import ctypes
import threading
import subprocess
import tkinter as tk
from ctypes import wintypes
from tkinter import ttk, messagebox, filedialog
import random
import csv
import binascii
import traceback

# ==========================================
# ИМПОРТ ОПЦИОНАЛЬНЫХ БИБЛИОТЕК
# ==========================================
try:
    import pystray
    from PIL import Image, ImageDraw, ImageTk
    HAS_PILLOW = True
except ImportError:
    HAS_PILLOW = False
    print("Внимание: Для карты поверхности и трея установите Pillow: pip install pillow pystray")

try:
    import matplotlib
    matplotlib.use("TkAgg")
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
    HAS_MPL = True
except ImportError:
    HAS_MPL = False
    print("Внимание: Для графиков установите matplotlib: pip install matplotlib")

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False
    print("Внимание: Для монитора I/O установите psutil: pip install psutil")

try:
    from winotify import Notification, audio
    HAS_NOTIFY = True
except ImportError:
    HAS_NOTIFY = False
    print("Внимание: Для уведомлений Windows установите winotify: pip install winotify")

# ==========================================
# КОНСТАНТЫ WIN32 API
# ==========================================
CREATE_NO_WINDOW = 0x08000000
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
FILE_FLAG_NO_BUFFERING = 0x20000000
FILE_FLAG_WRITE_THROUGH = 0x80000000
FILE_FLAG_SEQUENTIAL_SCAN = 0x08000000
IOCTL_DISK_GET_LENGTH_INFO = 0x0007405C

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

kernel32.CreateFileW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
    wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE
]
kernel32.CreateFileW.restype = wintypes.HANDLE

kernel32.ReadFile.argtypes = [
    wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID
]
kernel32.ReadFile.restype = wintypes.BOOL

kernel32.WriteFile.argtypes = [
    wintypes.HANDLE, wintypes.LPCVOID, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID
]
kernel32.WriteFile.restype = wintypes.BOOL

kernel32.SetFilePointer.argtypes = [
    wintypes.HANDLE, wintypes.LONG, ctypes.POINTER(wintypes.LONG), wintypes.DWORD
]
kernel32.SetFilePointer.restype = wintypes.DWORD

kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL

# ==========================================
# НАСТРОЙКИ ТЕМ
# ==========================================
THEMES = {
    "dark": {
        "bg": "#1e1e1e",
        "fg": "#ffffff",
        "sel": "#094771",
        "tree_bg": "#252526",
        "btn_bg": "#333333",
        "btn_active": "#555555",
        "notebook_bg": "#2d2d2d",
        "accent": "#007acc",
        "success": "#4ec9b0",
        "warning": "#ce9178"
    },
    "light": {
        "bg": "#f0f0f0",
        "fg": "#000000",
        "sel": "#0078d7",
        "tree_bg": "#ffffff",
        "btn_bg": "#e0e0e0",
        "btn_active": "#d0d0d0",
        "notebook_bg": "#e0e0e0",
        "accent": "#0066cc",
        "success": "#008800",
        "warning": "#cc5500"
    }
}

# ==========================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ==========================================
def is_admin() -> bool:
    """Проверка прав администратора."""
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False

def ps_json(script: str) -> list:
    """Выполняет PowerShell команду и возвращает JSON."""
    try:
        out = subprocess.check_output(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
            text=True, timeout=30, creationflags=CREATE_NO_WINDOW, stderr=subprocess.STDOUT
        )
        s = out.strip()
        if not s:
            return []
        data = json.loads(s)
        return data if isinstance(data, list) else [data]
    except Exception:
        return []

def format_size(n) -> str:
    """Форматирует размер в читаемый вид."""
    try:
        n = int(n)
    except Exception:
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"

def find_smartctl() -> str:
    """Ищет утилиту smartctl в системе."""
    # ОБНОВЛЕНО ДЛЯ PYINSTALLER: Проверяем встроенную папку bin внутри EXE
    if hasattr(sys, '_MEIPASS'):
        bundled_path = os.path.join(sys._MEIPASS, "bin", "smartctl.exe")
        if os.path.exists(bundled_path):
            return bundled_path

    # Проверяем стандартные пути и локальную папку bin (для запуска из исходников)
    paths = [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "bin", "smartctl.exe"),
        r"C:\Program Files\smartmontools\bin\smartctl.exe",
        r"C:\Program Files (x86)\smartmontools\bin\smartctl.exe",
        "smartctl"
    ]
    for c in paths:
        try:
            subprocess.check_output(
                [c, "--version"], stderr=subprocess.STDOUT, timeout=5, creationflags=CREATE_NO_WINDOW
            )
            return c
        except Exception:
            continue
    return None

SMARTCTL = find_smartctl()

def send_notification(title: str, message: str):
    """Отправляет системное уведомление Windows."""
    if HAS_NOTIFY:
        try:
            toast = Notification(
                app_id="MegaDisk",
                title=title,
                msg=message,
                duration="short"
            )
            toast.set_audio(audio.Default, loop=False)
            toast.show()
        except Exception:
            pass
    else:
        print(f"[УВЕДОМЛЕНИЕ] {title}: {message}")

# ==========================================
# ЯДРО: ЧТЕНИЕ SMART
# ==========================================
def read_smart(disk_id: int) -> dict:
    """Читает и парсит S.M.A.R.T. информацию с диска."""
    if not SMARTCTL:
        return {"_error": "smartctl не найден. Установите smartmontools или проверьте папку bin."}

    dev_path = f"/dev/sd{chr(97 + disk_id)}"
    
    cmd = [SMARTCTL, "-a", dev_path]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=15, creationflags=CREATE_NO_WINDOW
        )
        out = proc.stdout
    except Exception as e:
        return {"_error": f"Ошибка запуска smartctl: {e}"}

    if "Failed" in out and "Read Device" in out or not out:
        cmd = [SMARTCTL, "-d", "sat", "-a", dev_path]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=15, creationflags=CREATE_NO_WINDOW
            )
            out = proc.stdout
        except Exception:
            pass

    if not out or ("Failed" in out and "Read Device" in out):
        return {"_error": f"Не удалось открыть диск {dev_path}. Запустите от имени администратора."}

    data = {
        "model": "?", "serial": "?", "firmware": "?",
        "health": "НЕИЗВЕСТНО", "temperature": "?",
        "power_on_hours": "?", "attributes": []
    }

    in_table = False
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue

        if line.startswith("Device Model:"):
            data["model"] = line.split(":", 1)[1].strip()
        elif line.startswith("Serial Number:"):
            data["serial"] = line.split(":", 1)[1].strip()
        elif line.startswith("Firmware Version:"):
            data["firmware"] = line.split(":", 1)[1].strip()
        elif "SMART overall-health self-assessment test result:" in line:
            if "PASSED" in line:
                data["health"] = "✅ ХОРОШО"
            elif "FAILED" in line:
                data["health"] = "❌ ПЛОХО"

        if line.startswith("ID# ATTRIBUTE_NAME"):
            in_table = True
            continue
        
        if in_table:
            if "SMART Error Log" in line or "No Errors Logged" in line:
                break
            
            parts = line.split()
            if len(parts) >= 10 and parts[0].isdigit():
                attr_id = parts[0]
                name = parts[1]
                value = parts[3]
                worst = parts[4]
                thresh = parts[5]
                fail = parts[8]
                raw = " ".join(parts[9:])

                data["attributes"].append({
                    "id": attr_id, "name": name, "value": value,
                    "worst": worst, "thresh": thresh, "fail": fail, "raw": raw
                })

                if attr_id == "194":
                    data["temperature"] = raw.split()[0]
                if attr_id == "9":
                    data["power_on_hours"] = raw

    return data

# ==========================================
# ЯДРО: ТЕСТЫ И ОПЕРАЦИИ
# ==========================================
def run_benchmark(path: str, size_mb: int, block_kb: int, mode: str, progress_cb):
    """Запускает тест скорости с небуферизованным I/O."""
    total_bytes = size_mb * 1024 * 1024
    block_size = block_kb * 1024
    
    flags = FILE_FLAG_NO_BUFFERING
    if "Write" in mode:
        flags |= FILE_FLAG_WRITE_THROUGH
    if "SEQ" in mode:
        flags |= FILE_FLAG_SEQUENTIAL_SCAN

    with open(path, "wb") as f:
        f.write(b"\0" * total_bytes)
        f.flush()
        os.fsync(f.fileno())

    h = kernel32.CreateFileW(path, GENERIC_READ | GENERIC_WRITE, 0, None, OPEN_EXISTING, flags, None)
    if not h or h == wintypes.HANDLE(-1).value:
        raise OSError("Ошибка открытия файла. Нужны права администратора.")

    raw = ctypes.create_string_buffer(block_size + 4096)
    aligned = (ctypes.addressof(raw) + 4095) & ~4095
    
    done = 0
    nbytes = wintypes.DWORD(0)
    t0 = time.perf_counter()

    try:
        while done < total_bytes:
            if "RND" in mode:
                offset = random.randrange(0, total_bytes - block_size, block_size)
                kernel32.SetFilePointer(h, offset, None, 0)

            if "Read" in mode:
                ok = kernel32.ReadFile(h, ctypes.c_void_p(aligned), block_size, ctypes.byref(nbytes), None)
            else:
                ok = kernel32.WriteFile(h, ctypes.c_void_p(aligned), block_size, ctypes.byref(nbytes), None)
            
            if not ok or nbytes.value == 0:
                break
                
            done += nbytes.value
            progress_cb(int((done / total_bytes) * 100))
    finally:
        kernel32.CloseHandle(h)

    dt = time.perf_counter() - t0
    mbps = (done / (1024 * 1024)) / dt if dt > 0 else 0
    iops = (done / block_size) / dt if dt > 0 else 0
    return mbps, iops

def clone_disk(src: int, dst: int, skip_bad: bool, log_cb):
    """Посекторное клонирование диска."""
    src_path = rf"\\.\PhysicalDrive{src}"
    dst_path = rf"\\.\PhysicalDrive{dst}"
    
    h_src = kernel32.CreateFileW(src_path, GENERIC_READ, 0, None, OPEN_EXISTING, FILE_FLAG_NO_BUFFERING, None)
    if not h_src or h_src == wintypes.HANDLE(-1).value:
        log_cb("Ошибка чтения исходного диска.")
        return
        
    h_dst = kernel32.CreateFileW(dst_path, GENERIC_WRITE, 0, None, OPEN_EXISTING, FILE_FLAG_NO_BUFFERING, None)
    if not h_dst or h_dst == wintypes.HANDLE(-1).value:
        kernel32.CloseHandle(h_src)
        log_cb("Ошибка записи на целевой диск.")
        return

    size = ctypes.c_ulonglong(0)
    kernel32.DeviceIoControl(h_src, IOCTL_DISK_GET_LENGTH_INFO, None, 0, ctypes.byref(size), 8, None, None)
    total_sectors = size.value // 512
    
    raw = ctypes.create_string_buffer(1024 * 1024 + 4096)
    aligned = (ctypes.addressof(raw) + 4095) & ~4095
    nbytes = wintypes.DWORD(0)

    for i in range(0, total_sectors, 2048):
        kernel32.SetFilePointer(h_src, i * 512, None, 0)
        ok = kernel32.ReadFile(h_src, ctypes.c_void_p(aligned), 1024 * 1024, ctypes.byref(nbytes), None)
        
        if not ok or nbytes.value == 0:
            if skip_bad:
                log_cb(f"Пропущен бэд на секторе {i}")
                continue
            else:
                log_cb(f"Критическая ошибка на секторе {i}. Остановка.")
                break
                
        kernel32.SetFilePointer(h_dst, i * 512, None, 0)
        kernel32.WriteFile(h_dst, ctypes.c_void_p(aligned), nbytes.value, ctypes.byref(nbytes), None)
        
        if i % (2048 * 100) == 0:
            log_cb(f"Прогресс: {i / total_sectors * 100:.1f}%")
            
    kernel32.CloseHandle(h_src)
    kernel32.CloseHandle(h_dst)
    log_cb("Клонирование завершено.")

def secure_erase_disk(disk_id: int, progress_cb):
    """Безопасное стирание (запись нулей)."""
    path = rf"\\.\PhysicalDrive{disk_id}"
    h = kernel32.CreateFileW(path, GENERIC_READ | GENERIC_WRITE, 0, None, OPEN_EXISTING, FILE_FLAG_NO_BUFFERING, None)
    if not h or h == wintypes.HANDLE(-1).value:
        raise OSError("Не удалось открыть диск для записи.")

    size = ctypes.c_ulonglong(0)
    kernel32.DeviceIoControl(h, IOCTL_DISK_GET_LENGTH_INFO, None, 0, ctypes.byref(size), 8, None, None)
    total_sectors = size.value // 512
    
    raw = ctypes.create_string_buffer(1024 * 1024 + 4096)
    aligned = (ctypes.addressof(raw) + 4095) & ~4095
    ctypes.memset(aligned, 0, 1024 * 1024)
    nbytes = wintypes.DWORD(0)

    for i in range(0, total_sectors, 2048):
        kernel32.SetFilePointer(h, i * 512, None, 0)
        kernel32.WriteFile(h, ctypes.c_void_p(aligned), 1024 * 1024, ctypes.byref(nbytes), None)
        
        if i % (2048 * 100) == 0:
            progress_cb(int((i / total_sectors) * 100))
            
    kernel32.CloseHandle(h)
    progress_cb(100)

def carve_files(disk_id: int, output_dir: str, progress_cb, log_cb):
    """Поиск сигнатур файлов (JPEG/PDF) в неразмеченном пространстве."""
    path = rf"\\.\PhysicalDrive{disk_id}"
    h = kernel32.CreateFileW(path, GENERIC_READ, 0, None, OPEN_EXISTING, FILE_FLAG_NO_BUFFERING, None)
    if not h or h == wintypes.HANDLE(-1).value:
        raise OSError("Не удалось открыть диск.")

    size = ctypes.c_ulonglong(0)
    kernel32.DeviceIoControl(h, IOCTL_DISK_GET_LENGTH_INFO, None, 0, ctypes.byref(size), 8, None, None)
    total_sectors = size.value // 512
    
    raw = ctypes.create_string_buffer(1024 * 1024 + 4096)
    aligned = (ctypes.addressof(raw) + 4095) & ~4095
    nbytes = wintypes.DWORD(0)
    
    jpeg_sig = b"\xff\xd8\xff\xe0"
    pdf_sig = b"%PDF-"
    found_count = 0

    for i in range(0, total_sectors, 2048):
        kernel32.SetFilePointer(h, i * 512, None, 0)
        ok = kernel32.ReadFile(h, ctypes.c_void_p(aligned), 1024 * 1024, ctypes.byref(nbytes), None)
        if not ok or nbytes.value == 0:
            continue
            
        data = ctypes.string_at(aligned, nbytes.value)
        
        idx = data.find(jpeg_sig)
        if idx != -1:
            out_path = os.path.join(output_dir, f"found_{i}_{idx}.jpg")
            with open(out_path, "wb") as f:
                f.write(data[idx:idx+500000])
            log_cb(f"Найден JPEG на секторе {i + (idx//512)}")
            found_count += 1
            
        idx = data.find(pdf_sig)
        if idx != -1:
            out_path = os.path.join(output_dir, f"found_{i}_{idx}.pdf")
            with open(out_path, "wb") as f:
                f.write(data[idx:idx+500000])
            log_cb(f"Найден PDF на секторе {i + (idx//512)}")
            found_count += 1
            
        progress_cb(int((i / total_sectors) * 100))
        
    kernel32.CloseHandle(h)
    log_cb(f"Сканирование завершено. Найдено файлов: {found_count}")

def create_partition(disk_id: int, size_mb: int, fs: str = "NTFS", label: str = "DATA") -> str:
    """Создает раздел через diskpart."""
    script = f"""
    select disk {disk_id}
    create partition primary size={size_mb}
    format fs={fs} quick label="{label}"
    assign
    exit
    """
    proc = subprocess.run(["diskpart"], input=script, text=True, capture_output=True, creationflags=CREATE_NO_WINDOW)
    if proc.returncode != 0 or "DiskPart successfully" not in proc.stdout:
        return f"Ошибка: {proc.stdout}\n{proc.stderr}"
    return "Раздел создан успешно."

def delete_partition(disk_id: int, part_num: int) -> str:
    """Удаляет раздел через diskpart."""
    script = f"""
    select disk {disk_id}
    select partition {part_num}
    delete partition
    exit
    """
    proc = subprocess.run(["diskpart"], input=script, text=True, capture_output=True, creationflags=CREATE_NO_WINDOW)
    if proc.returncode != 0:
        return f"Ошибка: {proc.stdout}\n{proc.stderr}"
    return "Раздел удален."

# ==========================================
# ГЛАВНЫЙ КЛАСС ПРИЛОЖЕНИЯ
# ==========================================
class MegaDiskApp:
    def __init__(self, root):
        self.root = root
        self.root.title(f"MegaDisk Ultimate v9.1 {'[ADMIN]' if is_admin() else '[GUEST]'}")
        self.root.geometry("1400x950")
        
        self.current_theme = "dark"
        self.disks = []
        self.filtered_disks = []
        self.current_disk = None
        self.current_smart_data = None
        self.temp_history = []
        self.is_closing = False
        
        self.last_io = {}
        self.io_read_history = []
        self.io_write_history = []
        
        self.surf_image = None
        self.surf_photo = None
        self.surf_blocks = []
        
        self._build_ui()
        self._apply_theme()
        self.refresh_disks()
        self._start_auto_refresh()
        self._start_io_monitor()
        
        self.root.protocol("WM_DELETE_WINDOW", self.hide_to_tray)

    def _apply_theme(self):
        t = THEMES[self.current_theme]
        self.root.configure(bg=t["bg"])
        
        style = ttk.Style()
        style.theme_use("clam")
        
        style.configure(".", background=t["bg"], foreground=t["fg"], fieldbackground=t["tree_bg"], font=("Segoe UI", 9))
        style.configure("TFrame", background=t["bg"])
        style.configure("TLabel", background=t["bg"], foreground=t["fg"])
        style.configure("TButton", background=t["btn_bg"], foreground=t["fg"])
        style.map("TButton", background=[("active", t["btn_active"])])
        style.configure("Treeview", background=t["tree_bg"], foreground=t["fg"], fieldbackground=t["tree_bg"])
        style.map("Treeview", background=[("selected", t["sel"])])
        style.configure("TNotebook", background=t["bg"], borderwidth=0)
        style.configure("TNotebook.Tab", background=t["notebook_bg"], foreground=t["fg"], padding=[10, 5])
        style.map("TNotebook.Tab", background=[("selected", t["sel"])])
        
        if HAS_MPL and hasattr(self, 'ax'):
            self.fig.set_facecolor(t["bg"])
            self.ax.set_facecolor(t["bg"])
            self.ax.tick_params(colors=t["fg"])
            self.ax.set_ylabel("Температура (°C)", color=t["fg"])
            self.line.set_color(t["success"])
            self.canvas.draw()
            
        if HAS_MPL and hasattr(self, 'io_ax'):
            self.io_fig.set_facecolor(t["bg"])
            self.io_ax.set_facecolor(t["bg"])
            self.io_ax.tick_params(colors=t["fg"])
            self.io_canvas.draw()

    def _build_ui(self):
        paned = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        left_frame = ttk.Frame(paned, width=320)
        paned.add(left_frame, weight=1)
        
        ttk.Label(left_frame, text="Физические диски", font=("Segoe UI", 12, "bold")).pack(anchor="w", pady=(0, 5))
        
        search_frame = ttk.Frame(left_frame)
        search_frame.pack(fill=tk.X, pady=5)
        ttk.Label(search_frame, text="🔍 Поиск:").pack(side=tk.LEFT)
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", self._filter_disks)
        ttk.Entry(search_frame, textvariable=self.search_var).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        
        self.disk_tree = ttk.Treeview(left_frame, columns=("ID", "Model", "Size", "Bus"), show="headings", height=20)
        self.disk_tree.heading("ID", text="№")
        self.disk_tree.heading("Model", text="Модель")
        self.disk_tree.heading("Size", text="Объем")
        self.disk_tree.heading("Bus", text="Шина")
        
        self.disk_tree.column("ID", width=30, anchor="center")
        self.disk_tree.column("Model", width=150)
        self.disk_tree.column("Size", width=70, anchor="e")
        self.disk_tree.column("Bus", width=50, anchor="center")
        
        self.disk_tree.pack(fill=tk.BOTH, expand=True)
        self.disk_tree.bind("<<TreeviewSelect>>", self.on_disk_select)
        
        ttk.Button(left_frame, text="🔄 Обновить список", command=self.refresh_disks).pack(fill=tk.X, pady=10)

        right_frame = ttk.Frame(paned)
        paned.add(right_frame, weight=4)
        
        self.notebook = ttk.Notebook(right_frame)
        self.notebook.pack(fill=tk.BOTH, expand=True)

        self._build_tab_smart()
        self._build_tab_partitions()
        self._build_tab_benchmark()
        self._build_tab_surface()
        self._build_tab_io()
        self._build_tab_hex()
        self._build_tab_clone()
        self._build_tab_carving()
        self._build_tab_tools()
        self._build_tab_settings()

        self.status_var = tk.StringVar(value="Готово")
        ttk.Label(self.root, textvariable=self.status_var, relief=tk.SUNKEN, anchor="w").pack(side=tk.BOTTOM, fill=tk.X)

    def _build_tab_smart(self):
        tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(tab, text="S.M.A.R.T.")
        
        top = ttk.Frame(tab)
        top.pack(fill=tk.X, pady=5)
        self.smart_info = tk.StringVar(value="Выберите диск...")
        ttk.Label(top, textvariable=self.smart_info, font=("Segoe UI", 11, "bold"), foreground=THEMES[self.current_theme]["success"]).pack(side=tk.LEFT)
        
        btn_frame = ttk.Frame(top)
        btn_frame.pack(side=tk.RIGHT)
        ttk.Button(btn_frame, text="JSON", command=self.export_json).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_frame, text="CSV", command=self.export_csv).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_frame, text="HTML", command=self.export_html).pack(side=tk.LEFT, padx=2)

        cols = ("ID", "Attribute", "Val", "Worst", "Thresh", "Raw", "Status")
        self.smart_tree = ttk.Treeview(tab, columns=cols, show="headings")
        for c in cols:
            self.smart_tree.heading(c, text=c)
            self.smart_tree.column(c, width=80 if c != "Attribute" else 200, anchor="center")
        self.smart_tree.pack(fill=tk.BOTH, expand=True, pady=5)

        if HAS_MPL:
            self.fig = Figure(figsize=(8, 2), dpi=100, facecolor=THEMES[self.current_theme]["bg"])
            self.ax = self.fig.add_subplot(111)
            self.ax.set_facecolor(THEMES[self.current_theme]["bg"])
            self.ax.tick_params(colors=THEMES[self.current_theme]["fg"])
            self.ax.set_ylabel("Температура (°C)", color=THEMES[self.current_theme]["fg"])
            self.line, = self.ax.plot([], [], color=THEMES[self.current_theme]["success"], linewidth=2)
            self.canvas = FigureCanvasTkAgg(self.fig, master=tab)
            self.canvas.get_tk_widget().pack(fill=tk.X)

        t_frame = ttk.Frame(tab)
        t_frame.pack(fill=tk.X, pady=5)
        ttk.Button(t_frame, text="Короткий тест", command=lambda: self.run_smart_test("short")).pack(side=tk.LEFT, padx=5)
        ttk.Button(t_frame, text="Длинный тест", command=lambda: self.run_smart_test("long")).pack(side=tk.LEFT, padx=5)
        ttk.Button(t_frame, text="Лог ошибок", command=self.read_smart_log).pack(side=tk.LEFT, padx=5)

    def _build_tab_partitions(self):
        tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(tab, text="Разделы")
        
        self.canvas_map = tk.Canvas(tab, bg=THEMES[self.current_theme]["tree_bg"], height=80, highlightthickness=0)
        self.canvas_map.pack(fill=tk.X, pady=10)
        
        cols = ("Num", "Letter", "Type", "Size", "Offset")
        self.part_tree = ttk.Treeview(tab, columns=cols, show="headings")
        for c in cols:
            self.part_tree.heading(c, text=c)
            self.part_tree.column(c, width=120, anchor="center")
        self.part_tree.pack(fill=tk.BOTH, expand=True)
        
        b_frame = ttk.Frame(tab)
        b_frame.pack(fill=tk.X, pady=5)
        ttk.Button(b_frame, text="Создать", command=self.create_part).pack(side=tk.LEFT, padx=5)
        ttk.Button(b_frame, text="Удалить", command=self.delete_part).pack(side=tk.LEFT, padx=5)

    def _build_tab_benchmark(self):
        tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(tab, text="Тест скорости")
        
        ctrl = ttk.Frame(tab)
        ctrl.pack(fill=tk.X, pady=10)
        
        ttk.Label(ctrl, text="Папка:").pack(side=tk.LEFT)
        self.bm_dir = tk.StringVar(value=os.path.expanduser("~"))
        ttk.Entry(ctrl, textvariable=self.bm_dir, width=40).pack(side=tk.LEFT, padx=5)
        ttk.Button(ctrl, text="...", command=lambda: self.bm_dir.set(filedialog.askdirectory() or self.bm_dir.get())).pack(side=tk.LEFT)
        
        ttk.Label(ctrl, text="Размер (МБ):").pack(side=tk.LEFT, padx=(10,0))
        self.bm_size = tk.IntVar(value=512)
        ttk.Spinbox(ctrl, from_=64, to=4096, textvariable=self.bm_size, width=6).pack(side=tk.LEFT, padx=5)

        modes = ttk.Frame(tab)
        modes.pack(fill=tk.X, pady=5)
        
        self.bm_mode = tk.StringVar(value="SEQ_READ")
        modes_list = [
            ("SEQ_READ", "SEQ Чтение"), ("SEQ_WRITE", "SEQ Запись"),
            ("RND4K_READ", "RND 4K Чтение"), ("RND4K_WRITE", "RND 4K Запись")
        ]
        for m, txt in modes_list:
            ttk.Radiobutton(modes, text=txt, variable=self.bm_mode, value=m).pack(side=tk.LEFT, padx=3)
            
        self.bm_warn = tk.BooleanVar(value=False)
        ttk.Checkbutton(modes, text="⚠️ Разрешить запись", variable=self.bm_warn).pack(side=tk.LEFT, padx=20)

        self.bm_progress = ttk.Progressbar(tab, mode="determinate")
        self.bm_progress.pack(fill=tk.X, pady=15)
        
        self.bm_res_var = tk.StringVar(value="Результат: —")
        ttk.Label(tab, textvariable=self.bm_res_var, font=("Segoe UI", 16, "bold"), foreground=THEMES[self.current_theme]["warning"]).pack(anchor="w", pady=10)
        
        ttk.Button(tab, text="▶ ЗАПУСТИТЬ", command=self.run_benchmark).pack(fill=tk.X, pady=10)

    def _build_tab_surface(self):
        tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(tab, text="Карта поверхности")
        
        ctrl = ttk.Frame(tab)
        ctrl.pack(fill=tk.X, pady=5)
        ttk.Button(ctrl, text="▶ СКАНИРОВАТЬ", command=self.run_full_surface_scan).pack(side=tk.LEFT)
        self.surf_progress = ttk.Progressbar(ctrl, mode="determinate", length=300)
        self.surf_progress.pack(side=tk.LEFT, padx=10)
        
        canvas_frame = ttk.Frame(tab)
        canvas_frame.pack(fill=tk.BOTH, expand=True, pady=10)
        
        self.surf_hbar = ttk.Scrollbar(canvas_frame, orient=tk.HORIZONTAL)
        self.surf_hbar.pack(side=tk.BOTTOM, fill=tk.X)
        self.surf_vbar = ttk.Scrollbar(canvas_frame, orient=tk.VERTICAL)
        self.surf_vbar.pack(side=tk.RIGHT, fill=tk.Y)
        
        self.surf_canvas = tk.Canvas(
            canvas_frame, bg="black", highlightthickness=0,
            xscrollcommand=self.surf_hbar.set, yscrollcommand=self.surf_vbar.set
        )
        self.surf_canvas.pack(fill=tk.BOTH, expand=True)
        
        self.surf_hbar.config(command=self.surf_canvas.xview)
        self.surf_vbar.config(command=self.surf_canvas.yview)
        
        self.surf_rows = 300
        self.surf_cols = 400
        self.surf_blocks = []
        
        if HAS_PILLOW:
            self.surf_image = Image.new('RGB', (self.surf_cols, self.surf_rows), '#333333')
            self.surf_photo = ImageTk.PhotoImage(self.surf_image)
            self.surf_canvas.create_image(0, 0, anchor=tk.NW, image=self.surf_photo)
            self.surf_canvas.config(scrollregion=(0, 0, self.surf_cols, self.surf_rows))

    def _build_tab_io(self):
        tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(tab, text="Монитор I/O")
        
        if HAS_MPL:
            self.io_fig = Figure(figsize=(8, 3), dpi=100, facecolor=THEMES[self.current_theme]["bg"])
            self.io_ax = self.io_fig.add_subplot(111)
            self.io_ax.set_facecolor(THEMES[self.current_theme]["bg"])
            self.io_ax.tick_params(colors=THEMES[self.current_theme]["fg"])
            self.io_ax.set_ylabel("Скорость (МБ/с)", color=THEMES[self.current_theme]["fg"])
            
            self.io_read_line, = self.io_ax.plot([], [], color="#00ff00", label="Чтение")
            self.io_write_line, = self.io_ax.plot([], [], color="#ff0000", label="Запись")
            self.io_ax.legend(loc="upper right", facecolor=THEMES[self.current_theme]["bg"], labelcolor=THEMES[self.current_theme]["fg"])
            
            self.io_canvas = FigureCanvasTkAgg(self.io_fig, master=tab)
            self.io_canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

    def _build_tab_hex(self):
        tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(tab, text="Hex (LBA)")
        
        top = ttk.Frame(tab)
        top.pack(fill=tk.X, pady=5)
        ttk.Label(top, text="LBA (сектор):").pack(side=tk.LEFT)
        self.hex_lba = tk.StringVar(value="0")
        ttk.Entry(top, textvariable=self.hex_lba, width=15).pack(side=tk.LEFT, padx=5)
        ttk.Button(top, text="Читать", command=self.read_hex).pack(side=tk.LEFT)
        
        self.hex_text = tk.Text(tab, bg="black", fg="#00ff00", font=("Consolas", 10))
        self.hex_text.pack(fill=tk.BOTH, expand=True, pady=5)

    def _build_tab_clone(self):
        tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(tab, text="Клонирование")
        
        ttk.Label(tab, text="⚠️ ВНИМАНИЕ: Данные на целевом диске будут уничтожены!", foreground=THEMES[self.current_theme]["warning"]).pack(anchor="w", pady=10)
        
        f1 = ttk.Frame(tab)
        f1.pack(fill=tk.X, pady=5)
        ttk.Label(f1, text="Источник (№):").pack(side=tk.LEFT)
        self.clone_src = tk.StringVar()
        ttk.Entry(f1, textvariable=self.clone_src, width=10).pack(side=tk.LEFT, padx=5)
        
        ttk.Label(f1, text="Цель (№):").pack(side=tk.LEFT, padx=(10,0))
        self.clone_dst = tk.StringVar()
        ttk.Entry(f1, textvariable=self.clone_dst, width=10).pack(side=tk.LEFT, padx=5)
        
        self.clone_skip = tk.BooleanVar(value=True)
        ttk.Checkbutton(tab, text="Пропускать бэды", variable=self.clone_skip).pack(anchor="w", pady=5)
        
        ttk.Button(tab, text="▶ КЛОНИРОВАТЬ ДИСК", command=self.run_clone).pack(fill=tk.X, pady=10)
        ttk.Button(tab, text="💾 СОЗДАТЬ ОБРАЗ (.img)", command=self.create_image_backup).pack(fill=tk.X, pady=5)
        
        self.clone_log = tk.Text(tab, bg=THEMES[self.current_theme]["tree_bg"], fg=THEMES[self.current_theme]["fg"], font=("Consolas", 9), height=10)
        self.clone_log.pack(fill=tk.BOTH, expand=True)

    def _build_tab_carving(self):
        tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(tab, text="Восстановление файлов")
        
        ttk.Label(tab, text="Поиск потерянных JPEG и PDF файлов.", font=("Segoe UI", 11, "bold")).pack(anchor="w", pady=10)
        
        f1 = ttk.Frame(tab)
        f1.pack(fill=tk.X, pady=5)
        ttk.Label(f1, text="Папка для сохранения:").pack(side=tk.LEFT)
        self.carve_dir = tk.StringVar(value=os.path.expanduser("~"))
        ttk.Entry(f1, textvariable=self.carve_dir, width=40).pack(side=tk.LEFT, padx=5)
        ttk.Button(f1, text="...", command=lambda: self.carve_dir.set(filedialog.askdirectory() or self.carve_dir.get())).pack(side=tk.LEFT)
        
        ttk.Button(tab, text="🔍 НАЧАТЬ ПОИСК ФАЙЛОВ", command=self.run_carving).pack(fill=tk.X, pady=10)
        
        self.carve_progress = ttk.Progressbar(tab, mode="determinate")
        self.carve_progress.pack(fill=tk.X, pady=5)
        
        self.carve_log = tk.Text(tab, bg=THEMES[self.current_theme]["tree_bg"], fg=THEMES[self.current_theme]["fg"], font=("Consolas", 9), height=10)
        self.carve_log.pack(fill=tk.BOTH, expand=True, pady=5)

    def _build_tab_tools(self):
        tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(tab, text="Обслуживание")
        
        b_frame = ttk.Frame(tab)
        b_frame.pack(fill=tk.X, pady=10)
        ttk.Button(b_frame, text="⚡ TRIM (SSD)", command=self.run_trim).pack(side=tk.LEFT, padx=5)
        
        erase_frame = ttk.LabelFrame(tab, text="ОПАСНО: Безопасное стирание (Secure Erase)", padding=10)
        erase_frame.pack(fill=tk.X, pady=10)
        ttk.Label(erase_frame, text="Все данные будут стерты безвозвратно. Введите слово DELETE для подтверждения:").pack(anchor="w")
        
        ef_inner = ttk.Frame(erase_frame)
        ef_inner.pack(fill=tk.X, pady=5)
        self.erase_confirm = tk.StringVar()
        ttk.Entry(ef_inner, textvariable=self.erase_confirm).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        ttk.Button(ef_inner, text="СТЕРЕТЬ ДИСК", command=self.run_secure_erase).pack(side=tk.LEFT)
        
        self.log_text = tk.Text(tab, bg=THEMES[self.current_theme]["tree_bg"], fg=THEMES[self.current_theme]["fg"], font=("Consolas", 10))
        self.log_text.pack(fill=tk.BOTH, expand=True, pady=10)

    def _build_tab_settings(self):
        tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(tab, text="Настройки")
        
        ttk.Label(tab, text="Настройки интерфейса", font=("Segoe UI", 12, "bold")).pack(anchor="w", pady=10)
        
        ttk.Label(tab, text="Цветовая тема:").pack(anchor="w", pady=5)
        self.theme_var = tk.StringVar(value=self.current_theme)
        ttk.Radiobutton(tab, text="Тёмная", variable=self.theme_var, value="dark", command=self.change_theme).pack(anchor="w")
        ttk.Radiobutton(tab, text="Светлая", variable=self.theme_var, value="light", command=self.change_theme).pack(anchor="w")

    def change_theme(self):
        self.current_theme = self.theme_var.get()
        self._apply_theme()

    def _filter_disks(self, *args):
        query = self.search_var.get().lower()
        self.disk_tree.delete(*self.disk_tree.get_children())
        self.filtered_disks = []
        
        for d in self.disks:
            name = str(d.get("FriendlyName", "")).lower()
            if query in name:
                self.filtered_disks.append(d)
                self.disk_tree.insert("", tk.END, values=(
                    d.get("DeviceId"), d.get("FriendlyName")[:25],
                    format_size(d.get("Size")), d.get("BusType")
                ))

    def refresh_disks(self):
        self.status_var.set("Чтение списка дисков...")
        self.root.update_idletasks()
        self.disks = ps_json("Get-PhysicalDisk | Select-Object DeviceId,FriendlyName,MediaType,Size,BusType | ConvertTo-Json")
        self._filter_disks()

    def on_disk_select(self, event):
        sel = self.disk_tree.selection()
        if not sel:
            return
            
        idx = self.disk_tree.index(sel[0])
        self.current_disk = self.filtered_disks[idx]
        did = int(self.current_disk.get("DeviceId", 0))
        
        self.smart_tree.delete(*self.smart_tree.get_children())
        self.part_tree.delete(*self.part_tree.get_children())
        self.canvas_map.delete("all")
        self.temp_history.clear()
        
        self.surf_blocks.clear()
        if HAS_PILLOW:
            self.surf_image = Image.new('RGB', (self.surf_cols, self.surf_rows), '#333333')
            self.surf_photo = ImageTk.PhotoImage(self.surf_image)
            self.surf_canvas.create_image(0, 0, anchor=tk.NW, image=self.surf_photo)
        
        threading.Thread(target=self._load_data, args=(did,), daemon=True).start()

    def _load_data(self, disk_id):
        self.root.after(0, lambda: self.smart_info.set("Загрузка данных..."))
        smart = read_smart(disk_id)
        parts = ps_json(f"Get-Partition -DiskNumber {disk_id} | Select-Object PartitionNumber,DriveLetter,Size,Offset,Type | ConvertTo-Json")
        self.root.after(0, lambda: self._update_ui(smart, parts))

    def _update_ui(self, smart, parts):
        self.current_smart_data = smart
        
        if "_error" in smart:
            self.smart_info.set(f"Ошибка: {smart['_error']}")
        else:
            info_text = f"{smart.get('model', '?')} | Статус: {smart.get('health', '?')} | Temp: {smart.get('temperature', '?')}°C"
            self.smart_info.set(info_text)
            
            self.smart_tree.delete(*self.smart_tree.get_children())
            for a in smart.get("attributes", []):
                status = "OK" if a.get("fail") == "-" else "FAIL"
                self.smart_tree.insert("", tk.END, values=(
                    a.get("id"), a.get("name"), a.get("value"),
                    a.get("worst"), a.get("thresh"), a.get("raw"), status
                ))

        total_size = int(self.current_disk.get("Size", 1))
        self.canvas_map.update()
        c_width = self.canvas_map.winfo_width()
        colors = [THEMES[self.current_theme]["accent"], THEMES[self.current_theme]["success"], THEMES[self.current_theme]["warning"]]
        
        for i, p in enumerate(parts):
            self.part_tree.insert("", tk.END, values=(
                p.get("PartitionNumber"), p.get("DriveLetter") or "-",
                p.get("Type"), format_size(p.get("Size")), format_size(p.get("Offset"))
            ))
            
            if c_width > 10:
                x1 = (int(p.get("Offset", 0)) / total_size) * c_width
                x2 = ((int(p.get("Offset", 0)) + int(p.get("Size", 0))) / total_size) * c_width
                self.canvas_map.create_rectangle(x1, 10, x2, 60, fill=colors[i % len(colors)], outline="white")

    def _draw_surface_grid(self, index, status):
        if not HAS_PILLOW:
            return
            
        r = index // self.surf_cols
        c = index % self.surf_cols
        
        color = "#333333"
        if status == 0:
            color = "#00ff00"
        elif status == 1:
            color = "#ffff00"
        else:
            color = "#ff0000"
            
        draw = ImageDraw.Draw(self.surf_image)
        draw.rectangle([c, r, c+1, r+1], fill=color)
        
        if index % 100 == 0:
            self.surf_photo = ImageTk.PhotoImage(self.surf_image)
            self.surf_canvas.create_image(0, 0, anchor=tk.NW, image=self.surf_photo)

    def run_full_surface_scan(self):
        if not self.current_disk:
            return
            
        self.surf_blocks.clear()
        
        if HAS_PILLOW:
            self.surf_image = Image.new('RGB', (self.surf_cols, self.surf_rows), '#333333')
            self.surf_photo = ImageTk.PhotoImage(self.surf_image)
            self.surf_canvas.create_image(0, 0, anchor=tk.NW, image=self.surf_photo)
        
        did = int(self.current_disk.get("DeviceId"))
        total_size = int(self.current_disk.get("Size", 1))
        total_blocks = self.surf_rows * self.surf_cols
        block_size_bytes = total_size // total_blocks
        
        def worker():
            try:
                h = kernel32.CreateFileW(rf"\\.\PhysicalDrive{did}", GENERIC_READ, 0, None, OPEN_EXISTING, FILE_FLAG_NO_BUFFERING, None)
                if not h or h == wintypes.HANDLE(-1).value:
                    raise OSError("Нет доступа")
                
                raw = ctypes.create_string_buffer(1024 * 1024 + 4096)
                aligned = (ctypes.addressof(raw) + 4095) & ~4095
                nbytes = wintypes.DWORD(0)

                for i in range(total_blocks):
                    offset = ((i * block_size_bytes) // 512) * 512
                    t0 = time.perf_counter()
                    kernel32.SetFilePointer(h, offset, None, 0)
                    ok = kernel32.ReadFile(h, ctypes.c_void_p(aligned), 512, ctypes.byref(nbytes), None)
                    dt = (time.perf_counter() - t0) * 1000
                    
                    if not ok or nbytes.value == 0:
                        status = 2
                    elif dt > 50:
                        status = 1
                    else:
                        status = 0
                        
                    self.surf_blocks.append(status)
                    self.root.after(0, lambda idx=i, st=status: self._draw_surface_grid(idx, st))
                    self.root.after(0, lambda v=int((i + 1) / total_blocks * 100): self.surf_progress.configure(value=v))
                
                kernel32.CloseHandle(h)
                self.root.after(0, lambda: messagebox.showinfo("Готово", "Сканирование поверхности завершено!"))
            except Exception as e:
                self.root.after(0, lambda err=str(e): messagebox.showerror("Ошибка", err))
                
        threading.Thread(target=worker, daemon=True).start()

    def _start_io_monitor(self):
        if not HAS_PSUTIL or not HAS_MPL:
            return
            
        def tick():
            if self.current_disk and not self.is_closing:
                did = int(self.current_disk.get("DeviceId"))
                key = f"PhysicalDrive{did}"
                
                try:
                    io = psutil.disk_io_counters(perdisk=True)
                    if key in io:
                        current = io[key]
                        if key in self.last_io:
                            prev = self.last_io[key]
                            read_mb = (current.read_bytes - prev.read_bytes) / (1024 * 1024)
                            write_mb = (current.write_bytes - prev.write_bytes) / (1024 * 1024)
                            
                            self.io_read_history.append(read_mb)
                            self.io_write_history.append(write_mb)
                            
                            if len(self.io_read_history) > 60:
                                self.io_read_history.pop(0)
                                self.io_write_history.pop(0)
                                
                            self.io_read_line.set_data(range(len(self.io_read_history)), self.io_read_history)
                            self.io_write_line.set_data(range(len(self.io_write_history)), self.io_write_history)
                            
                            self.io_ax.relim()
                            self.io_ax.autoscale_view()
                            self.io_canvas.draw()
                            
                        self.last_io[key] = current
                except Exception:
                    pass
                    
            if not self.is_closing:
                self.root.after(1000, tick)
                
        tick()

    def read_hex(self):
        if not self.current_disk:
            return
            
        did = int(self.current_disk.get("DeviceId"))
        lba = int(self.hex_lba.get())
        
        try:
            h = kernel32.CreateFileW(rf"\\.\PhysicalDrive{did}", GENERIC_READ, 0, None, OPEN_EXISTING, FILE_FLAG_NO_BUFFERING, None)
            if not h or h == wintypes.HANDLE(-1).value:
                raise OSError("Нет доступа")
                
            raw = ctypes.create_string_buffer(512 + 4096)
            aligned = (ctypes.addressof(raw) + 4095) & ~4095
            nbytes = wintypes.DWORD(0)
            
            kernel32.SetFilePointer(h, lba * 512, None, 0)
            kernel32.ReadFile(h, ctypes.c_void_p(aligned), 512, ctypes.byref(nbytes), None)
            data = ctypes.string_at(aligned, 512)
            kernel32.CloseHandle(h)
            
            self.hex_text.delete("1.0", tk.END)
            for i in range(0, 512, 16):
                chunk = data[i:i+16]
                hex_str = " ".join(f"{b:02X}" for b in chunk)
                ascii_str = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
                self.hex_text.insert(tk.END, f"{i:04X}  {hex_str:<48}  |{ascii_str}|\n")
        except Exception as e:
            messagebox.showerror("Ошибка", str(e))

    def create_image_backup(self):
        if not self.current_disk:
            return
            
        did = int(self.current_disk.get("DeviceId"))
        path = filedialog.asksaveasfilename(defaultextension=".img", filetypes=[("Image", "*.img")])
        if not path:
            return
            
        self.clone_log.insert(tk.END, "Создание образа...\n")
        
        def worker():
            try:
                h = kernel32.CreateFileW(rf"\\.\PhysicalDrive{did}", GENERIC_READ, 0, None, OPEN_EXISTING, FILE_FLAG_NO_BUFFERING, None)
                size = ctypes.c_ulonglong(0)
                kernel32.DeviceIoControl(h, IOCTL_DISK_GET_LENGTH_INFO, None, 0, ctypes.byref(size), 8, None, None)
                total = size.value // 512
                
                raw = ctypes.create_string_buffer(1024 * 1024 + 4096)
                aligned = (ctypes.addressof(raw) + 4095) & ~4095
                nbytes = wintypes.DWORD(0)
                
                with open(path, "wb") as f:
                    for i in range(0, total, 2048):
                        kernel32.SetFilePointer(h, i * 512, None, 0)
                        kernel32.ReadFile(h, ctypes.c_void_p(aligned), 1024 * 1024, ctypes.byref(nbytes), None)
                        f.write(ctypes.string_at(aligned, nbytes.value))
                        
                kernel32.CloseHandle(h)
                self.root.after(0, lambda: self.clone_log.insert(tk.END, "Образ создан!\n"))
            except Exception as e:
                self.root.after(0, lambda err=str(e): self.clone_log.insert(tk.END, f"Ошибка: {err}\n"))
                
        threading.Thread(target=worker, daemon=True).start()

    def run_clone(self):
        src = self.clone_src.get()
        dst = self.clone_dst.get()
        if not src or not dst or src == dst:
            return
            
        self.clone_log.insert(tk.END, f"Клонирование {src} -> {dst}...\n")
        
        def worker():
            def log_cb(msg):
                self.root.after(0, lambda: self.clone_log.insert(tk.END, msg + "\n"))
            clone_disk(int(src), int(dst), self.clone_skip.get(), log_cb)
            
        threading.Thread(target=worker, daemon=True).start()

    def run_secure_erase(self):
        if not self.current_disk:
            return
            
        if self.erase_confirm.get() != "DELETE":
            messagebox.showerror("Ошибка", "Введите слово DELETE для подтверждения!")
            return
            
        did = int(self.current_disk.get("DeviceId"))
        self.log_text.insert(tk.END, "Начинаем стирание диска (запись нулей)...\n")
        
        def worker():
            try:
                def progress(p):
                    self.root.after(0, lambda: self.status_var.set(f"Стирание: {p}%"))
                secure_erase_disk(did, progress)
                self.root.after(0, lambda: self.log_text.insert(tk.END, "Диск успешно стерт!\n"))
                self.root.after(0, lambda: self.status_var.set("Готово"))
            except Exception as e:
                self.root.after(0, lambda err=str(e): self.log_text.insert(tk.END, f"Ошибка: {err}\n"))
                
        threading.Thread(target=worker, daemon=True).start()

    def run_carving(self):
        if not self.current_disk:
            return
            
        did = int(self.current_disk.get("DeviceId"))
        out_dir = self.carve_dir.get()
        self.carve_log.insert(tk.END, "Начинаем поиск файлов...\n")
        
        def worker():
            try:
                def prog(p):
                    self.root.after(0, lambda: self.carve_progress.configure(value=p))
                def log(msg):
                    self.root.after(0, lambda: self.carve_log.insert(tk.END, msg + "\n"))
                carve_files(did, out_dir, prog, log)
            except Exception as e:
                self.root.after(0, lambda err=str(e): self.carve_log.insert(tk.END, f"Ошибка: {err}\n"))
                
        threading.Thread(target=worker, daemon=True).start()

    def run_benchmark(self):
        mode = self.bm_mode.get()
        if "WRITE" in mode and not self.bm_warn.get():
            return
            
        path = os.path.join(self.bm_dir.get(), "_megadisk.tmp")
        size = self.bm_size.get()
        block = 1024 if "SEQ" in mode else 4
        
        def worker():
            try:
                mbps, iops = run_benchmark(path, size, block, mode, lambda p: self.root.after(0, lambda: self.bm_progress.configure(value=p)))
                self.root.after(0, lambda: self.bm_res_var.set(f"{mode}: {mbps:.1f} МБ/с ({iops:.0f} IOPS)"))
            except Exception as e:
                self.root.after(0, lambda err=str(e): messagebox.showerror("Ошибка", err))
            finally:
                if os.path.exists(path):
                    os.remove(path)
                    
        threading.Thread(target=worker, daemon=True).start()

    def run_trim(self):
        self.log_text.insert(tk.END, "TRIM...\n")
        threading.Thread(target=lambda: subprocess.run(["defrag", "C:", "/L"], capture_output=True), daemon=True).start()

    def export_json(self):
        if not self.current_smart_data:
            return
        path = filedialog.asksaveasfilename(defaultextension=".json")
        if path:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self.current_smart_data, f, indent=2)

    def export_csv(self):
        if not self.current_smart_data:
            return
        path = filedialog.asksaveasfilename(defaultextension=".csv")
        if path:
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["ID", "Name", "Val", "Wor", "Thr", "Raw", "Stat"])
                for a in self.current_smart_data.get("attributes", []):
                    w.writerow([a["id"], a["name"], a["value"], a["worst"], a["thresh"], a["raw"], a["fail"]])

    def export_html(self):
        if not self.current_smart_data:
            return
        path = filedialog.asksaveasfilename(defaultextension=".html")
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write("<html><body style='background:#1e1e1e;color:#fff;'><table border=1>")
                for a in self.current_smart_data.get("attributes", []):
                    f.write(f"<tr><td>{a['id']}</td><td>{a['name']}</td><td>{a['raw']}</td></tr>")
                f.write("</table></body></html>")

    def run_smart_test(self, kind):
        if not self.current_disk:
            return
        did = int(self.current_disk.get("DeviceId"))
        subprocess.run([SMARTCTL, "-t", kind, f"/dev/sd{chr(97+did)}"], creationflags=CREATE_NO_WINDOW)
        messagebox.showinfo("SMART", f"Тест {kind} запущен.")

    def read_smart_log(self):
        if not self.current_disk:
            return
        did = int(self.current_disk.get("DeviceId"))
        out = subprocess.run([SMARTCTL, "-l", "error", f"/dev/sd{chr(97+did)}"], capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
        self.hex_text.delete("1.0", tk.END)
        self.hex_text.insert(tk.END, out.stdout)

    def create_part(self):
        if not self.current_disk:
            return
        res = create_partition(int(self.current_disk.get("DeviceId")), 100000)
        messagebox.showinfo("Результат", res)

    def delete_part(self):
        sel = self.part_tree.selection()
        if not sel:
            return
        did = int(self.current_disk.get("DeviceId"))
        res = delete_partition(did, self.part_tree.item(sel[0])["values"][0])
        messagebox.showinfo("Результат", res)

    def _start_auto_refresh(self):
        def tick():
            if self.current_disk and not self.is_closing:
                smart = read_smart(int(self.current_disk.get("DeviceId")))
                if "_error" not in smart:
                    if smart.get("temperature") not in (None, "?"):
                        try:
                            temp = int(smart["temperature"])
                            self.temp_history.append(temp)
                            if len(self.temp_history) > 60:
                                self.temp_history.pop(0)
                                
                            if HAS_MPL:
                                self.line.set_data(range(len(self.temp_history)), self.temp_history)
                                self.ax.relim()
                                self.ax.autoscale_view()
                                self.canvas.draw()
                                
                            if temp > 50:
                                send_notification("Перегрев диска!", f"Температура достигла {temp}°C")
                        except Exception:
                            pass
                            
                    for a in smart.get("attributes", []):
                        if a["id"] == "5" and a["raw"].isdigit() and int(a["raw"]) > 0:
                            send_notification("Внимание! Бэд-блоки", f"На диске обнаружено {a['raw']} переназначенных секторов!")
                            break
                            
            if not self.is_closing:
                self.root.after(30000, tick)
        tick()

    def hide_to_tray(self):
        if HAS_TRAY:
            self.root.withdraw()
            img = Image.new('RGB', (64, 64), color='#1e1e1e')
            d = ImageDraw.Draw(img)
            d.rectangle([16, 16, 48, 48], fill=THEMES[self.current_theme]["accent"])
            menu = (pystray.MenuItem('Открыть', lambda: (self.tray.stop(), self.root.deiconify())), pystray.MenuItem('Выход', self.quit_app))
            self.tray = pystray.Icon("MegaDisk", img, "MegaDisk", menu)
            threading.Thread(target=self.tray.run, daemon=True).start()
        else:
            self.quit_app()

    def quit_app(self):
        self.is_closing = True
        if HAS_TRAY and hasattr(self, 'tray'):
            self.tray.stop()
        self.root.destroy()

# ==========================================
# ЗАПУСК
# ==========================================
if __name__ == "__main__":
    root = tk.Tk()
    app = MegaDiskApp(root)
    root.mainloop()