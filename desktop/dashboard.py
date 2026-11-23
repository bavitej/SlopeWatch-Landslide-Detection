"""SlopeWatch — STM32 Black Pill landslide instrumentation dashboard.

Requirements: Python 3.10+, pyserial, standard-library tkinter.
Run: pip install -r requirements.txt && python dashboard.py
Desktop labels distinguish MPU-derived vibration from the unresponsive Omron D7S.
No risk classification here is validated for public safety decisions.
"""
from __future__ import annotations

import csv
import json
import queue
import threading
import time
import tkinter as tk
from collections import deque
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    serial = None
    list_ports = None

BG = "#08111f"
PANEL = "#101c2e"
PANEL2 = "#14233a"
BORDER = "#243851"
WHITE = "#eef6ff"
MUTED = "#93a8c2"
CYAN = "#33d5ef"
BLUE = "#7295ff"
GREEN = "#44d3a3"
AMBER = "#f4bd62"
RED = "#ff697e"
VIOLET = "#b7a3ff"
F = "Segoe UI"


def label(parent, text, size=10, color=WHITE, bold=False, bg=None, **kwargs):
    return tk.Label(parent, text=text, font=(F, size, "bold" if bold else "normal"),
                    fg=color, bg=bg or parent.cget("bg"), **kwargs)


def panel(parent, pad=15):
    holder = tk.Frame(parent, bg=PANEL, highlightbackground=BORDER,
                      highlightthickness=1, bd=0)
    inner = tk.Frame(holder, bg=PANEL)
    inner.pack(fill="both", expand=True, padx=pad, pady=pad)
    return holder, inner


def format_num(value, unit="", digits=2, invalid=-1):
    if value is None or not isinstance(value, (int, float)) or value == invalid:
        return "—"
    return f"{value:.{digits}f}{unit}"


class SlopeWatch:
    def __init__(self, root):
        self.root = root
        root.title("SlopeWatch | Landslide Sensor Dashboard")
        root.configure(bg=BG)
        root.geometry("1300x840")
        root.minsize(990, 650)

        self.ser = None
        self.stop_event = threading.Event()
        self.worker = None
        self.events = queue.Queue(maxsize=300)
        self.latest = None
        self.mode_pending = False
        self.records = []
        self.trend = deque(maxlen=300)
        self.mode_var = tk.StringVar(value="DEMO")
        self.port_var = tk.StringVar(value="COM3")
        self.connect_text = tk.StringVar(value="DISCONNECTED")
        self.status_text = tk.StringVar(value="WAITING FOR LIVE DATA")
        self.status_reason = tk.StringVar(value="Connect the STM32 USB serial port to start monitoring.")
        self.kpis = {}
        self.details = {}
        self.runtime_log = tk.StringVar(value="No device connected")
        self.threshold_info = tk.StringVar()
        self.make_layout()
        self.refresh_ports()
        self.update_threshold_text()
        self.root.after(100, self.process_events)
        self.root.protocol("WM_DELETE_WINDOW", self.close)

    def make_button(self, parent, text, command, bg=PANEL2, fg=WHITE, padx=14):
        b = tk.Button(parent, text=text, command=command,
                      bg=bg, fg=fg, activebackground=BORDER,
                      activeforeground=WHITE, relief="flat", borderwidth=0,
                      cursor="hand2", font=(F, 10, "bold"),
                      padx=padx, pady=9)
        return b

    def make_layout(self):
        header = tk.Frame(self.root, bg=BG)
        header.pack(fill="x", padx=24, pady=(16, 12))
        title_group = tk.Frame(header, bg=BG)
        title_group.pack(fill="x", anchor="w")
        label(title_group, "◈  SLOPEWATCH", 21, CYAN, True, BG).pack(anchor="w")
        label(title_group, "LANDSLIDE MONITORING  /  INSTRUMENTATION LAB", 9, MUTED,
              True, BG).pack(anchor="w", pady=(3, 0))

        conn = tk.Frame(header, bg=BG)
        conn.pack(fill="x", anchor="w", pady=(9, 0))
        label(conn, "PORT", 9, MUTED, True, BG).pack(side="left", padx=(0, 7))
        self.port_select = ttk.Combobox(conn, textvariable=self.port_var,
                                        width=12, state="normal")
        self.port_select.pack(side="left", padx=(0, 7))
        self.make_button(conn, "↻", self.refresh_ports, padx=9).pack(side="left", padx=(0, 7))
        self.connect_button = self.make_button(conn, "Connect", self.toggle_connection, CYAN, BG)
        self.connect_button.pack(side="left", padx=(0, 13))
        label(conn, "MODE", 9, MUTED, True, BG).pack(side="left", padx=(0, 7))
        self.mode_select = ttk.Combobox(conn, textvariable=self.mode_var,
                                        values=["DEMO", "RESEARCH"], width=11,
                                        state="readonly")
        self.mode_select.pack(side="left")
        self.mode_select.bind("<<ComboboxSelected>>", self.change_mode)

        top = tk.Frame(self.root, bg=BG)
        top.pack(fill="x", padx=24)
        status_box, sin = panel(top, 15)
        status_box.pack(fill="x")
        bar = tk.Frame(sin, bg=PANEL)
        bar.pack(fill="x")
        self.status_label = label(bar, "WAITING FOR LIVE DATA", 18, WHITE, True, PANEL)
        self.status_label.pack(side="left")
        self.connection_label = label(bar, "●  DISCONNECTED", 10, MUTED, True, PANEL)
        self.connection_label.pack(side="right")
        label(sin, "Research-informed experimental classification — not a public landslide warning system",
              10, MUTED, bg=PANEL).pack(anchor="w", pady=(4, 0))
        label(sin, "LIVE INTERPRETATION", 9, CYAN, True, PANEL).pack(anchor="w", pady=(11, 1))
        self.reason_label = label(sin, "Connect the STM32 USB serial port to start monitoring.",
                                  11, WHITE, bg=PANEL, wraplength=1250, justify="left")
        self.reason_label.pack(anchor="w")

        scroll_area = tk.Frame(self.root, bg=BG)
        scroll_area.pack(fill="both", expand=True, padx=13, pady=(8, 6))
        self.outer_canvas = tk.Canvas(scroll_area, bg=BG, highlightthickness=0)
        scrollbar = ttk.Scrollbar(scroll_area, orient="vertical", command=self.outer_canvas.yview)
        self.outer_canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.outer_canvas.pack(side="left", fill="both", expand=True)
        self.scroll_inner = tk.Frame(self.outer_canvas, bg=BG)
        self.canvas_window = self.outer_canvas.create_window((0, 0), window=self.scroll_inner,
                                                             anchor="nw")
        self.scroll_inner.bind("<Configure>", lambda _: self.outer_canvas.configure(
            scrollregion=self.outer_canvas.bbox("all")))
        self.outer_canvas.bind("<Configure>", self.resize_scroll_content)
        self.outer_canvas.bind_all("<MouseWheel>", self.mousewheel)

        content = tk.Frame(self.scroll_inner, bg=BG)
        content.pack(fill="both", expand=True, padx=11, pady=8)
        label(content, "LIVE METRICS", 10, MUTED, True, BG).pack(anchor="w", pady=(0, 8))
        metrics = tk.Frame(content, bg=BG)
        metrics.pack(fill="x")
        for i in range(3):
            metrics.grid_columnconfigure(i, weight=1, uniform="m")
        metric_defs = [
            ("tilt_a", "MPU A  ·  TILT", "Δ from initial orientation", CYAN),
            ("tilt_b", "MPU B  ·  TILT", "Δ from initial orientation", BLUE),
            ("disp", "ROD TIP  ·  GEOMETRIC EST.", "250 mm fixed-pivot model", VIOLET),
            ("vibration", "VIBRATION  ·  PEAK", "MPU accelerometer high-pass", AMBER),
            ("moisture", "SOIL  ·  RELATIVE WETNESS", "Calibrate dry and wet endpoints", GREEN),
            ("rate", "TILT RATE  ·  HOURLY", "Available after 60 min", CYAN),
        ]
        for i, (key, name, sub, accent) in enumerate(metric_defs):
            card, inner = panel(metrics, 13)
            card.grid(row=i // 3, column=i % 3, sticky="nsew", padx=5, pady=5)
            label(inner, name, 10, MUTED, True, PANEL).pack(anchor="w")
            value = label(inner, "—", 24, accent, True, PANEL)
            value.pack(anchor="w", pady=(7, 4))
            label(inner, sub, 9, MUTED, bg=PANEL).pack(anchor="w")
            self.kpis[key] = value

        charts = tk.Frame(content, bg=BG)
        charts.pack(fill="x", pady=(12, 0))
        for i in range(2): charts.grid_columnconfigure(i, weight=1, uniform="charts")
        c1, inner1 = panel(charts, 12)
        c1.grid(row=0, column=0, padx=5, sticky="nsew")
        label(inner1, "TILT HISTORY", 12, WHITE, True, PANEL).pack(anchor="w")
        label(inner1, "MPU A (cyan)   ·   MPU B (blue)  /  degrees", 9,
              MUTED, bg=PANEL).pack(anchor="w", pady=(1, 8))
        self.tilt_chart = tk.Canvas(inner1, height=180, bg=PANEL, highlightthickness=0)
        self.tilt_chart.pack(fill="x")
        c2, inner2 = panel(charts, 12)
        c2.grid(row=0, column=1, padx=5, sticky="nsew")
        label(inner2, "VIBRATION HISTORY", 12, WHITE, True, PANEL).pack(anchor="w")
        label(inner2, "Peak rapid acceleration / g — measured by MPU6050", 9,
              MUTED, bg=PANEL).pack(anchor="w", pady=(1, 8))
        self.vibration_chart = tk.Canvas(inner2, height=180, bg=PANEL, highlightthickness=0)
        self.vibration_chart.pack(fill="x")

        lower = tk.Frame(content, bg=BG)
        lower.pack(fill="x", pady=(12, 0))
        lower.grid_columnconfigure(0, weight=1, uniform="lower")
        lower.grid_columnconfigure(1, weight=1, uniform="lower")
        venn_box, venn_content = panel(lower, 12)
        venn_box.grid(row=0, column=0, sticky="nsew", padx=5)
        label(venn_content, "THREE-SIGNAL DECISION DIAGRAM", 12, WHITE, True, PANEL).pack(anchor="w")
        label(venn_content, "Circle highlights show which indicators are currently active.",
              9, MUTED, bg=PANEL).pack(anchor="w")
        self.venn = tk.Canvas(venn_content, bg=PANEL, height=320, highlightthickness=0)
        self.venn.pack(fill="x")
        self.venn_result = label(venn_content, "TILT: —   •   MOISTURE: —   •   VIBRATION: —",
                                 10, MUTED, bg=PANEL)
        self.venn_result.pack(anchor="w", pady=(5, 0))

        rules_box, rules_inner = panel(lower, 12)
        rules_box.grid(row=0, column=1, sticky="nsew", padx=5)
        label(rules_inner, "HOW THE ALERT IS DECIDED", 12, WHITE, True, PANEL).pack(anchor="w")
        label(rules_inner, "T = sustained tilt trend   M = calibrated wet soil   V = recent vibration",
              9, MUTED, bg=PANEL, wraplength=580, justify="left").pack(anchor="w", pady=(4, 10))
        rules = [
            ("HIGH ALERT", "High tilt indicator, regardless of other signals", RED),
            ("WARNING", "T + (M or V)", AMBER),
            ("CAUTION", "T alone, or M + V", AMBER),
            ("DISTURBANCE", "V only", CYAN),
            ("WET CONDITION", "M only", BLUE),
            ("NORMAL", "No active indicators; moisture calibrated", GREEN),
            ("PARTIAL DATA", "No alarm, but soil not calibrated", MUTED),
            ("SENSOR FAULT", "An MPU is unavailable", RED),
            ("BASELINE", "Research mode has less than 60 min data", MUTED),
        ]
        for status, rule, color in rules:
            row = tk.Frame(rules_inner, bg=PANEL)
            row.pack(fill="x", pady=4)
            label(row, "●", 11, color, True, PANEL).pack(side="left", padx=(0, 7))
            label(row, status, 9, WHITE, True, PANEL, width=17,
                  anchor="w").pack(side="left")
            label(row, rule, 9, MUTED, bg=PANEL, wraplength=320,
                  justify="left").pack(side="left")

        details = tk.Frame(content, bg=BG)
        details.pack(fill="x", pady=(12, 0))
        details.grid_columnconfigure(0, weight=1, uniform="details")
        details.grid_columnconfigure(1, weight=1, uniform="details")
        threshold_box, threshold_inner = panel(details, 14)
        threshold_box.grid(row=0, column=0, sticky="nsew", padx=5)
        label(threshold_inner, "THRESHOLD REFERENCE", 12, WHITE, True, PANEL).pack(anchor="w")
        self.threshold_label = label(threshold_inner, "", 10, MUTED, bg=PANEL,
                                     justify="left", wraplength=565)
        self.threshold_label.pack(anchor="w", pady=(9, 10))
        label(threshold_inner, "Research mode compares 60-minute tilt magnitude trends with published site-specific hourly thresholds. These are not universal safety limits.",
              9, AMBER, bg=PANEL, wraplength=565, justify="left").pack(anchor="w")

        sensor_box, sensor_inner = panel(details, 14)
        sensor_box.grid(row=0, column=1, sticky="nsew", padx=5)
        label(sensor_inner, "SENSOR DETAIL & CALIBRATION", 12, WHITE, True, PANEL).pack(anchor="w")
        for key, heading in [
            ("angle", "Roll / pitch"),
            ("displacement", "Estimated displacement A / B"),
            ("vibrations", "MPU vibration peaks A / B"),
            ("soil", "Soil raw ADC / endpoints"),
            ("readcounts", "Read success / error counts"),
            ("baseline", "Hourly baseline"),
        ]:
            line = tk.Frame(sensor_inner, bg=PANEL)
            line.pack(fill="x", pady=4)
            label(line, heading, 9, MUTED, bg=PANEL, width=29,
                  anchor="w").pack(side="left")
            detail = label(line, "—", 9, WHITE, bg=PANEL, anchor="w")
            detail.pack(side="left")
            self.details[key] = detail
        calrow = tk.Frame(sensor_inner, bg=PANEL)
        calrow.pack(anchor="w", pady=(12, 0))
        self.make_button(calrow, "Capture DRY", lambda: self.send("CAL_DRY")).pack(side="left", padx=(0, 6))
        self.make_button(calrow, "Capture WET", lambda: self.send("CAL_WET")).pack(side="left", padx=(0, 6))
        self.make_button(calrow, "Clear", lambda: self.send("RESET_SOIL")).pack(side="left")
        label(sensor_inner, "Dry/wet calibration resets when the board restarts. Keep sensor electronics dry.",
              9, MUTED, bg=PANEL, wraplength=590).pack(anchor="w", pady=(9, 0))

        bottom = tk.Frame(content, bg=BG)
        bottom.pack(fill="x", pady=(12, 14))
        self.make_button(bottom, "Export captured CSV", self.export_csv).pack(side="right", padx=(8, 0))
        self.make_button(bottom, "Clear graphs", self.clear_graphs).pack(side="right")
        self.log_label = label(bottom, "No device connected", 9, MUTED, bg=BG,
                               wraplength=1000, justify="left")
        self.log_label.pack(side="left", anchor="w")
        label(content, "SOURCE NOTES  •  USGS: site-specific hydrology + movement monitoring  |  2020 Water: 0.03 and 0.10°/h local tilt rates  |  All warnings are experimental",
              9, MUTED, bg=BG, wraplength=1400).pack(anchor="w", padx=7, pady=(0, 10))
        self.draw_venn(False, False, False)
        self.draw_charts()

    def resize_scroll_content(self, event):
        self.outer_canvas.itemconfigure(self.canvas_window, width=event.width)

    def mousewheel(self, event):
        self.outer_canvas.yview_scroll(int(-1 * event.delta / 120), "units")

    def refresh_ports(self):
        ports = [p.device for p in list_ports.comports()] if list_ports else []
        self.port_select["values"] = ports
        if ports and (self.port_var.get() not in ports):
            self.port_var.set("COM3" if "COM3" in ports else ports[0])

    def toggle_connection(self):
        if self.ser is not None:
            self.disconnect()
            return
        if serial is None:
            messagebox.showerror("Missing dependency", "Install pyserial:\npython -m pip install pyserial")
            return
        port = self.port_var.get().strip()
        if not port:
            messagebox.showerror("No port", "Choose a COM port.")
            return
        try:
            self.ser = serial.Serial(port=port, baudrate=115200, timeout=0.25,
                                     write_timeout=1)
        except Exception as exc:
            messagebox.showerror("Connection failed", str(exc))
            return
        self.stop_event.clear()
        self.worker = threading.Thread(target=self.read_loop, daemon=True)
        self.worker.start()
        self.connect_button.configure(text="Disconnect", bg=RED, fg=BG)
        self.connection_label.configure(text=f"●  CONNECTED: {port}", fg=GREEN)
        self.log_label.configure(text=f"Connected to {port}; awaiting JSON telemetry")
        self.mode_pending = True

    def disconnect(self):
        self.stop_event.set()
        ser = self.ser
        self.ser = None
        if ser:
            try:
                ser.close()
            except Exception:
                pass
        self.connect_button.configure(text="Connect", bg=CYAN, fg=BG)
        self.connection_label.configure(text="●  DISCONNECTED", fg=MUTED)
        self.log_label.configure(text="Serial connection closed")

    def send(self, message):
        if self.ser is None:
            messagebox.showinfo("Not connected", "Connect the Black Pill first.")
            return
        try:
            self.ser.write((message + "\n").encode("ascii"))
            self.log_label.configure(text=f"Sent to STM32: {message}")
        except Exception as exc:
            self.log_label.configure(text=f"Command failed: {exc}")

    def change_mode(self, event=None):
        self.update_threshold_text()
        self.mode_pending = True
        if self.ser is not None:
            self.send("MODE " + self.mode_var.get())
            self.mode_pending = False

    def read_loop(self):
        ser = self.ser
        while not self.stop_event.is_set() and ser:
            try:
                line = ser.readline().decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                if line.startswith("#"):
                    self.enqueue(("debug", line[1:].strip()))
                elif line.startswith("{"):
                    item = json.loads(line)
                    if isinstance(item, dict):
                        self.enqueue(("data", item))
            except (OSError, ValueError, json.JSONDecodeError, UnicodeError) as exc:
                self.enqueue(("debug", f"Serial: {exc}"))
                time.sleep(0.4)
                if not ser.is_open:
                    break
            except Exception as exc:
                self.enqueue(("debug", f"Serial: {exc}"))
                break
        self.enqueue(("ended", "Reader stopped"))

    def enqueue(self, item):
        try:
            self.events.put_nowait(item)
        except queue.Full:
            try:
                self.events.get_nowait()
                self.events.put_nowait(item)
            except queue.Empty:
                pass

    def process_events(self):
        new_data = False
        for _ in range(50):
            try:
                kind, item = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "data":
                self.latest = item
                if self.mode_pending and self.ser is not None:
                    self.mode_pending = False
                    self.send("MODE " + self.mode_var.get())
                self.records.append(item.copy())
                if len(self.records) > 120000:
                    self.records = self.records[-100000:]
                self.trend.append(item)
                new_data = True
            elif kind == "debug":
                self.log_label.configure(text=item)
            elif kind == "ended" and self.ser is not None:
                self.disconnect()
        if new_data:
            self.update_display()
        self.root.after(100, self.process_events)

    def update_threshold_text(self):
        if self.mode_var.get() == "DEMO":
            text = ("DEMO  •  warning tilt ≥ 1.0° for 3 s\n"
                    "DEMO  •  high tilt ≥ 3.0° for 3 s\n"
                    "VIBRATION  •  peak ≥ 0.090 g; held for 3 s\n"
                    "WETNESS  •  calibrated relative wetness ≥ 70%\n"
                    "MPU SAMPLING  •  25 ms  |  stream: 500 ms\n"
                    "WARNING  •  tilt + moisture or recent vibration")
        else:
            text = ("RESEARCH-INSPIRED  •  use 60-minute tilt change\n"
                    "CAUTION RATE  •  ≥ 0.030° per hour\n"
                    "HIGH RATE  •  ≥ 0.100° per hour\n"
                    "VIBRATION  •  peak ≥ 0.090 g; supporting evidence\n"
                    "WETNESS  •  calibrated relative wetness ≥ 70%\n"
                    "RATE NOT AVAILABLE until 60 min of valid data")
        self.threshold_label.configure(text=text)

    def update_display(self):
        d = self.latest or {}
        mode = d.get("mode", self.mode_var.get())
        if mode in ("DEMO", "RESEARCH") and mode != self.mode_var.get():
            # The requested mode can lag behind by one serial frame.
            pass
        aok, bok = d.get("a_ok", False), d.get("b_ok", False)
        self.kpis["tilt_a"].configure(text=format_num(d.get("a_tilt"), "°", 2) if aok else "OFFLINE")
        self.kpis["tilt_b"].configure(text=format_num(d.get("b_tilt"), "°", 2) if bok else "OFFLINE")
        da = d.get("a_disp", -1)
        db = d.get("b_disp", -1)
        self.kpis["disp"].configure(text=format_num(max(da, db), " mm") if aok and bok else "—")
        self.kpis["vibration"].configure(text=format_num(d.get("vib_peak"), " g", 4))
        self.kpis["moisture"].configure(
            text=format_num(d.get("soil_pct"), "%", 0) if d.get("soil_cal") else "NOT CALIBRATED")
        ra, rb = d.get("rate_a", -1), d.get("rate_b", -1)
        self.kpis["rate"].configure(
            text=format_num(max(ra, rb), "°/h", 3) if d.get("rate_ready") else "BASELINE…")
        status = d.get("status", "UNKNOWN")
        color = {
            "NORMAL": GREEN, "PARTIAL DATA": MUTED, "HIGH ALERT": RED, "WARNING": AMBER,
            "CAUTION": AMBER, "SENSOR FAULT": RED,
            "DISTURBANCE": CYAN, "WET CONDITION": BLUE,
            "COLLECTING BASELINE": VIOLET,
        }.get(status, WHITE)
        self.status_label.configure(text=status, fg=color)
        reasons = {
            "NORMAL": "No measured indicator currently crosses its configured rule.",
            "PARTIAL DATA": "No active tilt/vibration flag, but soil is not calibrated, so wetness cannot be evaluated.",
            "HIGH ALERT": "The highest configured tilt criterion is active. This is a prototype flag, not a field safety determination.",
            "WARNING": "A tilt trend is active together with wetness and/or a recent vibration.",
            "CAUTION": "A tilt trend is active, or wet soil and vibration occur together.",
            "DISTURBANCE": "A short high-pass acceleration event occurred in the last 3 seconds.",
            "WET CONDITION": "Relative wetness is above the locally calibrated threshold; movement trend is not active.",
            "SENSOR FAULT": "One or both MPU6050 sensors is unavailable; movement indicators are incomplete.",
            "COLLECTING BASELINE": f"Research mode is accumulating a one-hour reference ({d.get('baseline_min', 0)}/60 minutes). No hazard classification yet.",
        }
        self.reason_label.configure(text=reasons.get(status, "Waiting for a valid condition."))
        self.details["angle"].configure(text=(
            f"A: {format_num(d.get('a_roll'), '°')} / {format_num(d.get('a_pitch'), '°')}   "
            f"B: {format_num(d.get('b_roll'), '°')} / {format_num(d.get('b_pitch'), '°')}"))
        self.details["displacement"].configure(text=f"{format_num(da, ' mm')}   /   {format_num(db, ' mm')}")
        self.details["vibrations"].configure(text=f"{format_num(d.get('a_vib'), ' g', 4)}   /   {format_num(d.get('b_vib'), ' g', 4)}")
        self.details["soil"].configure(text=(
            f"{d.get('soil_raw', '—')}  /  dry {d.get('dry_raw', '—')}  wet {d.get('wet_raw', '—')}"))
        self.details["readcounts"].configure(text=(
            f"A {d.get('a_reads', 0)} / {d.get('a_fail', 0)}   "
            f"B {d.get('b_reads', 0)} / {d.get('b_fail', 0)}"))
        self.details["baseline"].configure(text=(
            f"{d.get('baseline_min', 0)} / 60 min — "
            f"{'rate ready' if d.get('rate_ready') else 'collecting'}"))
        t, m, v = bool(d.get("tilt_flag")), bool(d.get("moist_flag")), bool(d.get("vib_flag"))
        self.draw_venn(t, m, v)
        self.venn_result.configure(text=f"TILT: {'YES' if t else 'NO'}   •   "
                                        f"MOISTURE: {'YES' if m else 'NO'}   •   "
                                        f"VIBRATION: {'YES' if v else 'NO'}")
        self.draw_charts()

    def draw_chart(self, canvas, keys, colors, threshold=None):
        canvas.delete("all")
        width = canvas.winfo_width()
        if width < 200: width = 660
        height = 180
        left, right, top, bottom = 47, width - 18, 10, 155
        series = []
        for key in keys:
            points = [(i, row.get(key, -1)) for i, row in enumerate(self.trend)
                      if isinstance(row.get(key), (int, float)) and row[key] >= 0]
            series.append(points)
        visible_values = [v for line in series for _, v in line]
        ymax = max(visible_values + ([threshold] if threshold else []) + [0.05])
        ymax *= 1.2
        canvas.create_rectangle(left, top, right, bottom, fill=PANEL, outline="")
        for i in range(5):
            y = top + i * (bottom - top) / 4
            canvas.create_line(left, y, right, y, fill=BORDER, width=1)
            value = ymax * (1 - i / 4)
            canvas.create_text(36, y, text=f"{value:.2f}", fill=MUTED, font=(F, 8))
        if threshold is not None and ymax > 0:
            y = bottom - (threshold / ymax) * (bottom - top)
            canvas.create_line(left, y, right, y, fill=AMBER, dash=(5, 4), width=1)
            canvas.create_text(right - 4, max(top + 10, y - 10), anchor="e",
                               text=f"threshold {threshold:.3f}", fill=AMBER, font=(F, 8))
        n = max(1, len(self.trend) - 1)
        for points, color in zip(series, colors):
            coords = []
            for i, val in points:
                x = left + i * (right - left) / n
                y = bottom - (val / ymax) * (bottom - top)
                coords.extend((x, y))
            if len(coords) >= 4:
                canvas.create_line(*coords, fill=color, width=2, smooth=False)
        if not self.trend:
            canvas.create_text((left + right) / 2, 85, text="Waiting for measurements…",
                               fill=MUTED, font=(F, 11))
        canvas.create_text(left, 170, anchor="w", text="older", fill=MUTED, font=(F, 8))
        canvas.create_text(right, 170, anchor="e", text="latest", fill=MUTED, font=(F, 8))

    def draw_charts(self):
        self.draw_chart(self.tilt_chart, ["a_tilt", "b_tilt"], [CYAN, BLUE])
        self.draw_chart(self.vibration_chart, ["a_vib", "b_vib"], [AMBER, VIOLET], 0.09)

    def draw_venn(self, tilt, moisture, vibration):
        c = self.venn
        c.delete("all")
        w = c.winfo_width()
        if w < 350: w = 610
        cx = w / 2
        circles = [
            (cx - 95, 143, tilt, CYAN, "TILT", -38, -38),
            (cx + 95, 143, moisture, GREEN, "MOISTURE", 38, -38),
            (cx, 222, vibration, VIOLET, "VIBRATION", 0, 47),
        ]
        for x, y, active, color, name, dx, dy in circles:
            r = 108
            c.create_oval(x-r, y-r, x+r, y+r,
                          fill=("#193d48" if name == "TILT" else
                                "#1d423a" if name == "MOISTURE" else "#332944") if active else PANEL2,
                          outline=color if active else BORDER, width=3 if active else 2)
        for x, y, active, color, name, dx, dy in circles:
            c.create_text(x+dx, y+dy, text=name,
                          fill=color if active else MUTED,
                          font=(F, 11, "bold"))
        c.create_text(cx, 163, text="T + M + V", fill=WHITE, font=(F, 10, "bold"))
        c.create_text(cx, 183, text="combined", fill=MUTED, font=(F, 9))

    def clear_graphs(self):
        self.trend.clear()
        self.draw_charts()
        self.log_label.configure(text="Graph history cleared; captured data kept for CSV export")

    def export_csv(self):
        if not self.records:
            messagebox.showinfo("No data", "Connect to the board and capture measurements first.")
            return
        path = filedialog.asksaveasfilename(title="Save captured telemetry", defaultextension=".csv",
                                            filetypes=[("CSV files", "*.csv")],
                                            initialfile="slopewatch_telemetry.csv")
        if not path: return
        try:
            keys = list(dict.fromkeys(k for row in self.records for k in row.keys()))
            with open(Path(path), "w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=keys)
                writer.writeheader()
                writer.writerows(self.records)
            self.log_label.configure(text=f"Saved {len(self.records)} records to {path}")
        except OSError as exc:
            messagebox.showerror("Save failed", str(exc))

    def close(self):
        self.disconnect()
        self.root.destroy()


def main():
    root = tk.Tk()
    style = ttk.Style()
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    style.configure("TCombobox", padding=6, fieldbackground=PANEL2,
                    background=PANEL2, foreground=WHITE, arrowcolor=CYAN)
    SlopeWatch(root)
    root.mainloop()


if __name__ == "__main__":
    main()