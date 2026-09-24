"""Small cross-platform desktop controller for the outbound Bridge Agent.

Secrets stay in a local .env file and are never shown in the UI. The app
starts/stops bridge_agent.py, polls the Render bridge status, and notifies the
operator when Bridge or iChancy connectivity changes.
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
import webbrowser

import requests


ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"
AGENT_FILE = ROOT / "bridge_agent.py"


def load_local_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


class BridgeDesktopApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Asmar Robert Bridge")
        self.root.geometry("520x430")
        self.root.minsize(480, 390)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.env = load_local_env(ENV_FILE)
        self.process: subprocess.Popen | None = None
        self.stop_event = threading.Event()
        self.last_bridge = None
        self.last_ichancy = None
        self.last_error = None
        self._build_ui()
        self._refresh()

    def _build_ui(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        outer = ttk.Frame(self.root, padding=18)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="Asmar Robert Bridge", font=("TkDefaultFont", 18, "bold")).pack(anchor="w")
        ttk.Label(outer, text="اتصال outbound بين Render و iChancy عبر هذا الجهاز", foreground="#555").pack(anchor="w", pady=(2, 16))

        card = ttk.LabelFrame(outer, text="الحالة", padding=12)
        card.pack(fill="x")
        self.bridge_value = tk.StringVar(value="غير معروف")
        self.ichancy_value = tk.StringVar(value="غير معروف")
        self.heartbeat_value = tk.StringVar(value="—")
        self.jobs_value = tk.StringVar(value="—")
        self._status_row(card, "Bridge", self.bridge_value)
        self._status_row(card, "iChancy / Chrome", self.ichancy_value)
        self._status_row(card, "آخر Heartbeat", self.heartbeat_value)
        self._status_row(card, "Jobs", self.jobs_value)

        controls = ttk.Frame(outer)
        controls.pack(fill="x", pady=16)
        self.start_button = ttk.Button(controls, text="تشغيل Bridge", command=self.start_bridge)
        self.start_button.pack(side="left", padx=(0, 8))
        self.stop_button = ttk.Button(controls, text="إيقاف Bridge", command=self.stop_bridge, state="disabled")
        self.stop_button.pack(side="left", padx=(0, 8))
        ttk.Button(controls, text="فتح iChancy", command=lambda: webbrowser.open("https://agents.ichancy.com")).pack(side="left")

        log_frame = ttk.LabelFrame(outer, text="التنبيهات", padding=8)
        log_frame.pack(fill="both", expand=True)
        self.log = tk.Text(log_frame, height=8, state="disabled", wrap="word")
        self.log.pack(fill="both", expand=True)
        ttk.Label(outer, text="الأسرار تُقرأ من .env محليًا ولا تظهر داخل التطبيق.", foreground="#666").pack(anchor="w", pady=(10, 0))

    @staticmethod
    def _status_row(parent, label, variable):
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=3)
        ttk.Label(row, text=f"{label}:", width=18).pack(side="left")
        ttk.Label(row, textvariable=variable, font=("TkDefaultFont", 10, "bold")).pack(side="left")

    def _write_log(self, text):
        timestamp = time.strftime("%H:%M:%S")
        self.log.configure(state="normal")
        self.log.insert("end", f"[{timestamp}] {text}\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _notify(self, title, message):
        self._write_log(message)
        self.root.bell()
        # Keep the first version dependency-free; messagebox is available on
        # Windows, macOS, and Linux with Tk installed.
        if self.root.state() != "withdrawn":
            self.root.after(0, lambda: messagebox.showinfo(title, message, parent=self.root))

    def _bridge_headers(self):
        key = self.env.get("BRIDGE_SHARED_SECRET", "")
        return {"X-Bridge-Key": key} if key else {}

    def _status_url(self):
        return self.env.get("RENDER_BRIDGE_URL", "").rstrip("/") + "/bridge/status"

    def _refresh(self):
        threading.Thread(target=self._fetch_status, daemon=True).start()
        self.root.after(5000, self._refresh)

    def _fetch_status(self):
        url = self._status_url()
        if not url.startswith("http"):
            self.root.after(0, lambda: self.bridge_value.set("إعداد RENDER_BRIDGE_URL ناقص"))
            return
        try:
            response = requests.get(url, headers=self._bridge_headers(), timeout=8)
            payload = response.json()
            bridge = payload.get("bridge")
            if not bridge:
                bridge_state, ichancy, heartbeat = "غير مسجل", "—", "—"
                jobs_text = "—"
            else:
                bridge_state = bridge.get("status", "unknown")
                ichancy = "متصلة" if bridge.get("ichancy_connected") else "غير متصلة"
                heartbeat = bridge.get("last_heartbeat") or "—"
                jobs = payload.get("jobs") or {}
                jobs_text = f"pending: {jobs.get('pending', 0)} | running: {jobs.get('running', 0)}"
            self.root.after(0, lambda: self._apply_status(bridge_state, ichancy, heartbeat, None, jobs_text))
        except Exception as exc:
            error = type(exc).__name__
            self.root.after(0, lambda: self._apply_status("Render غير متاح", "—", "—", error, "—"))

    def _apply_status(self, bridge, ichancy, heartbeat, error=None, jobs_text="—"):
        self.bridge_value.set(bridge)
        self.ichancy_value.set(ichancy)
        self.heartbeat_value.set(heartbeat)
        self.jobs_value.set(jobs_text)
        if bridge != self.last_bridge and self.last_bridge is not None:
            self._notify("تغيرت حالة Bridge", f"Bridge أصبح: {bridge}")
        if ichancy != self.last_ichancy and self.last_ichancy is not None:
            self._notify("تغيرت حالة iChancy", f"iChancy أصبحت: {ichancy}")
        if error and error != self.last_error:
            self._write_log(f"تعذر قراءة الحالة: {error}")
        self.last_bridge, self.last_ichancy, self.last_error = bridge, ichancy, error

    def start_bridge(self):
        if self.process and self.process.poll() is None:
            return
        if not ENV_FILE.exists():
            messagebox.showwarning("الإعداد ناقص", f"أنشئ ملف .env محليًا أولًا:\n{ENV_FILE}", parent=self.root)
            return
        env = os.environ.copy()
        env.update(self.env)
        self.process = subprocess.Popen([sys.executable, str(AGENT_FILE)], cwd=str(ROOT), env=env)
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self._write_log("تم تشغيل Bridge Agent")

    def stop_bridge(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.process = None
        self.start_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self._write_log("تم إيقاف Bridge Agent")

    def close(self):
        self.stop_bridge()
        self.stop_event.set()
        self.root.destroy()


if __name__ == "__main__":
    window = tk.Tk()
    BridgeDesktopApp(window)
    window.mainloop()
