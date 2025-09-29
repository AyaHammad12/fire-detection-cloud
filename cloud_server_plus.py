# cloud_server_plus.py
#!/usr/bin/env python3
import socket, threading, json, time, signal, csv, os, logging, random, datetime
from collections import deque
import numpy as np
import matplotlib
matplotlib.use("TkAgg")
import matplotlib.pyplot as plt

logging.basicConfig(
    level=logging.INFO,
    format='[CLOUD] %(asctime)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger('cloud_server')

HOST = 'localhost'
PORT = 5555
MAX_CLIENTS = 10
BUFFER_SIZE = 4096

MAX_DATA_POINTS = 100
DATA_WINDOW = 20
ALARM_THRESHOLD = 60.0
SPRINKLER_THRESHOLD = 65.0

plt.style.use('ggplot')

class SensorData:
    def __init__(self, max_points=MAX_DATA_POINTS):
        self.timestamps = deque(maxlen=max_points)
        self.smoke = deque(maxlen=max_points)
        self.co2 = deque(maxlen=max_points)
        self.fire = deque(maxlen=max_points)
        self.car_temp = deque(maxlen=max_points)
        self.car_status = "UNKNOWN"
        self.alarm_status = False
        self.sprinkler_status = False

    def add_room(self, ts, smoke=None, co2=None, fire=None):
        self.timestamps.append(ts)
        self.smoke.append(0.0 if smoke is None else float(smoke))
        self.co2.append(0.0 if co2 is None else float(co2))
        self.fire.append(0.0 if fire is None else float(fire))

    def add_car(self, ts, temp=None, status=None):
        self.timestamps.append(ts)
        self.car_temp.append(0.0 if temp is None else float(temp))
        if status is not None:
            self.car_status = status

    def recent(self):
        if not self.timestamps:
            z = np.array([0.0])
            return dict(t=z, smoke=z, co2=z, fire=z, car_temp=z)
        n = min(DATA_WINDOW, len(self.timestamps))
        idx = slice(-n, None)
        return dict(
            t=np.array(list(self.timestamps)[idx]),
            smoke=np.array(list(self.smoke)[idx]) if self.smoke else np.array([]),
            co2=np.array(list(self.co2)[idx]) if self.co2 else np.array([]),
            fire=np.array(list(self.fire)[idx]) if self.fire else np.array([]),
            car_temp=np.array(list(self.car_temp)[idx]) if self.car_temp else np.array([]),
        )

    def latest(self):
        def last(dq, default=0.0):
            return dq[-1] if dq else default
        return dict(
            smoke=last(self.smoke),
            co2=last(self.co2),
            fire=last(self.fire),
            car_temp=last(self.car_temp),
            car_status=self.car_status,
            alarm=self.alarm_status,
            sprinkler=self.sprinkler_status
        )

class CloudServer:
    def __init__(self, host=HOST, port=PORT):
        self.host, self.port = host, port
        self.server_socket = None
        self.running = False
        self.clients = {}
        self.lock = threading.Lock()

        # غرفتين + سيارتين
        self.sensor_data = {
            '1200400': SensorData(),   # Room A
            '1211371': SensorData(),   # Room B
            'car1': SensorData(),      # Vehicle 1
            'car2': SensorData(),      # Vehicle 2
        }

        self.events = []
        self.packet_count = 0
        self.last_seq = 0

        self.step_mode = False
        self.wait_for_step = False
        self.step_event = threading.Event()

        self._setup_plots()
        plt.show(block=False)

        self.scenarios = {
            'fire1': self._scenario_fire1,
            'fire2': self._scenario_fire2,
            'co2_high': self._scenario_co2_high,
            'car1': self._scenario_car1,
            'car2': self._scenario_car2,
            'normal': self._scenario_normal,
            'test': self._scenario_test,
        }

        self.add_event("Starting cloud server ...")

    def _setup_plots(self):
        self.fig, self.axs = plt.subplots(4, 1, figsize=(12, 10), constrained_layout=True)

        # Room 1200400
        self.axs[0].set_title('Room 1200400'); self.axs[0].set_ylabel('Values')
        x=y=np.array([0])
        self.smoke1_line, = self.axs[0].plot(x,y,label='SMOKE')
        self.co2_1_line,  = self.axs[0].plot(x,y,label='CO2')
        self.fire1_line,  = self.axs[0].plot(x,y,label='FIRE')
        self.axs[0].axhline(ALARM_THRESHOLD, linestyle='--')
        self.axs[0].axhline(SPRINKLER_THRESHOLD, linestyle='--')
        self.axs[0].legend(); self.axs[0].set_ylim(0,100)

        # Room 1211371
        self.axs[1].set_title('Room 1211371'); self.axs[1].set_ylabel('Values')
        self.smoke2_line, = self.axs[1].plot(x,y,label='SMOKE')
        self.co2_2_line,  = self.axs[1].plot(x,y,label='CO2')
        self.fire2_line,  = self.axs[1].plot(x,y,label='FIRE')
        self.axs[1].axhline(ALARM_THRESHOLD, linestyle='--')
        self.axs[1].axhline(SPRINKLER_THRESHOLD, linestyle='--')
        self.axs[1].legend(); self.axs[1].set_ylim(0,100)

        # Car 1
        self.axs[2].set_title('Car 1 Engine Temp'); self.axs[2].set_ylabel('Temp (°C)')
        self.car1_temp_line, = self.axs[2].plot(x,y,label='ENGINE_TEMP')
        self.axs[2].legend(); self.axs[2].set_ylim(0, 130)

        # Car 2
        self.axs[3].set_title('Car 2 Engine Temp'); self.axs[3].set_ylabel('Temp (°C)')
        self.car2_temp_line, = self.axs[3].plot(x,y,label='ENGINE_TEMP')
        self.axs[3].legend(); self.axs[3].set_ylim(0, 130)

    def _update_plots(self):
        try:
            r1 = self.sensor_data['1200400'].recent()
            r2 = self.sensor_data['1211371'].recent()
            c1 = self.sensor_data['car1'].recent()
            c2 = self.sensor_data['car2'].recent()

            x1 = np.arange(len(r1['t'])); x2 = np.arange(len(r2['t']))
            x3 = np.arange(len(c1['t'])); x4 = np.arange(len(c2['t']))
            self.smoke1_line.set_data(x1, r1['smoke']); self.co2_1_line.set_data(x1, r1['co2']); self.fire1_line.set_data(x1, r1['fire'])
            self.smoke2_line.set_data(x2, r2['smoke']); self.co2_2_line.set_data(x2, r2['co2']); self.fire2_line.set_data(x2, r2['fire'])
            self.car1_temp_line.set_data(x3, c1['car_temp']); self.car2_temp_line.set_data(x4, c2['car_temp'])

            if len(x1): self.axs[0].set_xlim(max(0, x1[-1]-DATA_WINDOW), max(DATA_WINDOW, x1[-1]))
            if len(x2): self.axs[1].set_xlim(max(0, x2[-1]-DATA_WINDOW), max(DATA_WINDOW, x2[-1]))
            if len(x3): self.axs[2].set_xlim(max(0, x3[-1]-DATA_WINDOW), max(DATA_WINDOW, x3[-1]))
            if len(x4): self.axs[3].set_xlim(max(0, x4[-1]-DATA_WINDOW), max(DATA_WINDOW, x4[-1]))

            self.fig.canvas.draw_idle()
            plt.pause(0.1)
        except Exception as e:
            logger.info(f"Plot update error: {e}")

    def start(self):
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_socket.bind((self.host, self.port))
        self.server_socket.listen(MAX_CLIENTS)
        self.server_socket.settimeout(0.1)

        self.running = True
        logger.info(f"✅ Listening on {self.host}:{self.port}")

        threading.Thread(target=self._accept_loop, daemon=True).start()
        threading.Thread(target=self._ui_loop, daemon=True).start()
        signal.signal(signal.SIGINT, self._signal_handler)

        try:
            while self.running:
                time.sleep(0.1)
                self._update_plots()
        finally:
            self.stop()

    def _accept_loop(self):
        while self.running:
            try:
                sock, addr = self.server_socket.accept()
                sock.settimeout(0.1)
                with self.lock:
                    self.clients[addr] = (sock, time.time(), "UNKNOWN")
                logger.info(f"🔗 Gateway connected {addr}")
                threading.Thread(target=self._client_loop, args=(sock, addr), daemon=True).start()
            except socket.timeout:
                pass
            except Exception as e:
                logger.info(f"Accept error: {e}")

    def _client_loop(self, sock, addr):
        buffer = ""
        try:
            while self.running:
                if self.wait_for_step:
                    self.step_event.wait(); self.step_event.clear(); self.wait_for_step = False
                try:
                    data = sock.recv(BUFFER_SIZE)
                    if not data: break
                    buffer += data.decode('utf-8', errors='ignore')
                    while '\n' in buffer:
                        line, buffer = buffer.split('\n', 1)
                        self._process_message(sock, addr, line)
                except socket.timeout:
                    pass
                except Exception as e:
                    logger.info(f"Client {addr} error: {e}")
        finally:
            try: sock.close()
            except: pass
            with self.lock:
                self.clients.pop(addr, None)
            logger.info(f"🔴 Closed {addr}")

    def _process_message(self, sock, addr, line):
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            logger.info(f"Invalid JSON from {addr}: {line[:80]}")
            return

        with self.lock:
            if addr in self.clients:
                s, _, gw = self.clients[addr]
                self.clients[addr] = (s, time.time(), data.get('gateway_id', gw))

        t = data.get('type', '')
        if t == 'HEARTBEAT':
            self._on_heartbeat(data, addr)
        elif t == 'DATA':
            self._on_data(data)

    def _on_heartbeat(self, data, addr):
        seq = data.get('seq', 0)
        gw = data.get('gateway_id', 'UNKNOWN')
        logger.info(f"💓 Heartbeat seq={seq} from {gw}")

    def _on_data(self, data):
        try:
            seq = data.get('seq', 0)
            gw = data.get('gateway_id', 'UNKNOWN')
            sensors = data.get('sensors', {})
            self.last_seq = max(self.last_seq, seq)
            self.packet_count += 1
            logger.info(f"📥 DATA seq={seq} from {gw} sensors={len(sensors)}")

            ts = time.time()

            # Room 1200400
            if 'smoke1' in sensors or 'co2_1' in sensors or 'fire1' in sensors:
                smoke = float(sensors.get('smoke1', 0.0))
                co2   = float(sensors.get('co2_1', 0.0))
                fire  = float(sensors.get('fire1', 0.0))
                self.sensor_data['1200400'].add_room(ts, smoke, co2, fire)
                if smoke >= ALARM_THRESHOLD and not self.sensor_data['1200400'].alarm_status:
                    self.sensor_data['1200400'].alarm_status = True; self.add_event("🔔 ALARM_1200400 -> ON")
                if smoke >= SPRINKLER_THRESHOLD and not self.sensor_data['1200400'].sprinkler_status:
                    self.sensor_data['1200400'].sprinkler_status = True; self.add_event("💦 SPRINKLER_1200400 -> ON")

            # Room 1211371
            if 'smoke2' in sensors or 'co2_2' in sensors or 'fire2' in sensors:
                smoke = float(sensors.get('smoke2', 0.0))
                co2   = float(sensors.get('co2_2', 0.0))
                fire  = float(sensors.get('fire2', 0.0))
                self.sensor_data['1211371'].add_room(ts, smoke, co2, fire)
                if smoke >= ALARM_THRESHOLD and not self.sensor_data['1211371'].alarm_status:
                    self.sensor_data['1211371'].alarm_status = True; self.add_event("🔔 ALARM_1211371 -> ON")
                if smoke >= SPRINKLER_THRESHOLD and not self.sensor_data['1211371'].sprinkler_status:
                    self.sensor_data['1211371'].sprinkler_status = True; self.add_event("💦 SPRINKLER_1211371 -> ON")

            # Car 1
            if 'car1_temp' in sensors or 'car1_status' in sensors:
                temp = float(sensors.get('car1_temp', 25.0))
                status = sensors.get('car1_status', 'UNKNOWN')
                prev = self.sensor_data['car1'].car_status
                self.sensor_data['car1'].add_car(ts, temp, status)
                if prev != status:
                    self.add_event(f"🚗 Car1 {prev} -> {status}")

            # Car 2
            if 'car2_temp' in sensors or 'car2_status' in sensors:
                temp = float(sensors.get('car2_temp', 25.0))
                status = sensors.get('car2_status', 'UNKNOWN')
                prev = self.sensor_data['car2'].car_status
                self.sensor_data['car2'].add_car(ts, temp, status)
                if prev != status:
                    self.add_event(f"🚙 Car2 {prev} -> {status}")

        except Exception as e:
            logger.info(f"Process data error: {e}")

    def _ui_loop(self):
        try:
            while self.running:
                self._print_dashboard()
                try:
                    cmd = input()
                    if cmd.strip(): self._process_command(cmd.strip())
                except EOFError:
                    break
                except Exception as e:
                    logger.info(f"UI error: {e}")
        except Exception as e:
            logger.info(f"UI loop error: {e}")

    def _print_dashboard(self):
        os.system('cls' if os.name == 'nt' else 'clear')
        print("="*110)
        print("🔥 FIRE DETECTION CLOUD DASHBOARD (LIVE)")
        print("="*110)

        active = len(self.clients)
        print(f"Connections: {'🟢' if active else '🔴'} Active={active} | Packets={self.packet_count} | LastSeq={self.last_seq}")

        r1 = self.sensor_data['1200400'].latest()
        r2 = self.sensor_data['1211371'].latest()
        c1 = self.sensor_data['car1'].latest()
        c2 = self.sensor_data['car2'].latest()

        print("\nROOM 1200400:")
        print(f"  SMOKE={r1['smoke']:.2f} CO2={r1['co2']:.2f} FIRE={r1['fire']:.2f} ALARM={'ON' if r1['alarm'] else 'OFF'} SPRINK={'ON' if r1['sprinkler'] else 'OFF'}")
        print("\nROOM 1211371:")
        print(f"  SMOKE={r2['smoke']:.2f} CO2={r2['co2']:.2f} FIRE={r2['fire']:.2f} ALARM={'ON' if r2['alarm'] else 'OFF'} SPRINK={'ON' if r2['sprinkler'] else 'OFF'}")
        print("\nCAR1:")
        print(f"  STATUS={c1['car_status']} ENGINE_TEMP={c1['car_temp']:.2f}")
        print("\nCAR2:")
        print(f"  STATUS={c2['car_status']} ENGINE_TEMP={c2['car_temp']:.2f}")

        print("\nRecent Events:")
        for t, e in (self.events[-8:] if len(self.events)>8 else self.events):
            print(f"  {t.strftime('%Y-%m-%d %H:%M:%S')} - {e}")

        print("\nCommands: help | step | run | pause-gw | resume-gw | scenario <name> [room=<ID>] | control <dev> <on/off> | stats | export csv | events | exit")
        print("="*110)

    def _process_command(self, command):
        try:
            parts = command.split()
            cmd = parts[0].lower()

            if cmd == "help":
                print("\nAvailable commands:\n  step | run | pause-gw | resume-gw | scenario <name> [room=<ID>]\n  control <dev> <on/off> | stats | export csv | events | exit")
            elif cmd == "step":
                self.step_mode = True; self.wait_for_step = False; print("Step mode: press ENTER after each step.")
            elif cmd == "run":
                self.step_mode = False; self.wait_for_step = False; self.step_event.set(); print("Auto mode.")
            elif cmd == "pause-gw":
                self._send_control_all("PAUSE")
            elif cmd == "resume-gw":
                self._send_control_all("RESUME")
            elif cmd == "scenario" and len(parts) > 1:
                name = parts[1].lower()
                params = {}
                # optional: room=1200400 or room=1211371 for co2_high
                for p in parts[2:]:
                    if p.startswith("room="):
                        params["room"] = p.split("=",1)[1]
                if name in self.scenarios:
                    self.add_event(f"Running scenario: {name} {params if params else ''}")
                    threading.Thread(target=self.scenarios[name], args=(params,), daemon=True).start()
                else:
                    print(f"Unknown scenario: {name}")
            elif cmd == "control" and len(parts) > 2:
                dev, state = parts[1].upper(), parts[2].upper()
                self._control_device(dev, state == "ON")
            elif cmd == "stats":
                up = int(time.time() - self.events[0][0].timestamp())
                print(f"\nActive={len(self.clients)} TotalPackets={self.packet_count} LastSeq={self.last_seq} Uptime={up}s")
            elif cmd == "export" and len(parts)>1 and parts[1].lower()=="csv":
                self._export_csv()
            elif cmd == "events":
                for t,e in self.events: print(t.strftime('%Y-%m-%d %H:%M:%S'), '-', e)
                input("\nPress ENTER to continue...")
            elif cmd == "exit":
                self.running = False
            else:
                print("Unknown command. Type: help")
        except Exception as e:
            logger.info(f"Command error: {e}")

    def _send_control_all(self, command, params=None):
        params = params or {}
        msg = json.dumps({"type":"CONTROL","command":command,"params":params,"timestamp":time.time()}) + "\n"
        with self.lock:
            for addr, (sock, _, _) in list(self.clients.items()):
                try:
                    sock.sendall(msg.encode())
                    logger.info(f"Sent {command} to {addr} params={params}")
                except Exception as e:
                    logger.info(f"Send {command} to {addr} failed: {e}")

    def _control_device(self, device, state):
        if device.startswith("ALARM_"):
            room = device[6:]
            if room in self.sensor_data:
                self.sensor_data[room].alarm_status = state
                self.add_event(f"🔔 {device} -> {'ON' if state else 'OFF'}")
        elif device.startswith("SPRINK_"):
            room = device[7:]
            if room in self.sensor_data:
                self.sensor_data[room].sprinkler_status = state
                self.add_event(f"💦 {device} -> {'ON' if state else 'OFF'}")
        else:
            print(f"Unknown device: {device}")

    def _export_csv(self):
        try:
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            fn = f"fire_data_{ts}.csv"
            with open(fn, "w", newline='', encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(['Timestamp','Entity','Smoke','CO2','Fire','Car_Temp','Car_Status'])
                all_ts = sorted(set(
                    list(self.sensor_data['1200400'].timestamps) +
                    list(self.sensor_data['1211371'].timestamps) +
                    list(self.sensor_data['car1'].timestamps) +
                    list(self.sensor_data['car2'].timestamps)
                ))
                for t in all_ts:
                    dt = datetime.datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S")
                    for ent in ['1200400','1211371','car1','car2']:
                        d = self.sensor_data[ent]
                        if not d.timestamps: continue
                        idx = min(range(len(d.timestamps)), key=lambda i: abs(d.timestamps[i]-t))
                        if abs(d.timestamps[idx]-t) < 2.0:
                            w.writerow([dt, ent,
                                        (d.smoke[idx] if d.smoke else ''),
                                        (d.co2[idx] if d.co2 else ''),
                                        (d.fire[idx] if d.fire else ''),
                                        (d.car_temp[idx] if d.car_temp else ''),
                                        d.car_status])
            print(f"\nData exported to {fn}"); input("Press ENTER to continue...")
        except Exception as e:
            print(f"Export error: {e}"); input("Press ENTER to continue...")

    def add_event(self, msg):
        self.events.append((datetime.datetime.now(), msg))

    # ----- سيناريوهات -----
    def _scenario_fire1(self, params=None):
        self._send_control_all("SCENARIO", {"name":"fire1","room":"1200400"})
        base = self.sensor_data['1200400'].smoke[-1] if self.sensor_data['1200400'].smoke else 45.0
        for i in range(30):
            if not self.running: break
            ts = time.time(); val = base + min(25, i*0.8)
            self.sensor_data['1200400'].add_room(ts, val, val*10, val*0.6)
            if val>=ALARM_THRESHOLD and not self.sensor_data['1200400'].alarm_status:
                self.sensor_data['1200400'].alarm_status = True; self.add_event("🔔 ALARM_1200400 -> ON")
            if val>=SPRINKLER_THRESHOLD and not self.sensor_data['1200400'].sprinkler_status:
                self.sensor_data['1200400'].sprinkler_status = True; self.add_event("💦 SPRINKLER_1200400 -> ON")
            time.sleep(1)

    def _scenario_fire2(self, params=None):
        self._send_control_all("SCENARIO", {"name":"fire2","room":"1211371"})
        base = self.sensor_data['1211371'].smoke[-1] if self.sensor_data['1211371'].smoke else 42.0
        for i in range(30):
            if not self.running: break
            ts = time.time(); val = base + min(25, i*0.8)
            self.sensor_data['1211371'].add_room(ts, val, val*10, val*0.6)
            if val>=ALARM_THRESHOLD and not self.sensor_data['1211371'].alarm_status:
                self.sensor_data['1211371'].alarm_status = True; self.add_event("🔔 ALARM_1211371 -> ON")
            if val>=SPRINKLER_THRESHOLD and not self.sensor_data['1211371'].sprinkler_status:
                self.sensor_data['1211371'].sprinkler_status = True; self.add_event("💦 SPRINKLER_1211371 -> ON")
            time.sleep(1)

    def _scenario_co2_high(self, params=None):
        room = (params or {}).get("room","1200400")
        self._send_control_all("SCENARIO", {"name":"co2_high","room":room})
        key = '1200400' if room == '1200400' else '1211371'
        base = self.sensor_data[key].co2[-1] if self.sensor_data[key].co2 else 800.0
        for i in range(20):
            if not self.running: break
            ts = time.time(); co2v = base + 50 + i*25
            self.sensor_data[key].add_room(ts, smoke=None, co2=co2v, fire=None)
            time.sleep(1)

    def _scenario_car1(self, params=None):
        self._send_control_all("SCENARIO", {"name":"car1"})
        self.sensor_data['car1'].car_status = "RUNNING"; self.add_event("🚗 Car1 PARKED -> RUNNING")
        base = self.sensor_data['car1'].car_temp[-1] if self.sensor_data['car1'].car_temp else 25.0
        for i in range(20):
            if not self.running: break
            ts = time.time(); temp = base + min(60, i*3)
            self.sensor_data['car1'].add_car(ts, temp, "RUNNING")
            time.sleep(1)
        if self.running:
            self.sensor_data['car1'].car_status = "OVERHEATING"; self.add_event("🚗 Car1 RUNNING -> OVERHEATING")

    def _scenario_car2(self, params=None):
        self._send_control_all("SCENARIO", {"name":"car2"})
        self.sensor_data['car2'].car_status = "RUNNING"; self.add_event("🚙 Car2 PARKED -> RUNNING")
        base = self.sensor_data['car2'].car_temp[-1] if self.sensor_data['car2'].car_temp else 25.0
        for i in range(20):
            if not self.running: break
            ts = time.time(); temp = base + min(60, i*3)
            self.sensor_data['car2'].add_car(ts, temp, "RUNNING")
            time.sleep(1)
        if self.running:
            self.sensor_data['car2'].car_status = "OVERHEATING"; self.add_event("🚙 Car2 RUNNING -> OVERHEATING")

    def _scenario_normal(self, params=None):
        self._send_control_all("SCENARIO", {"name":"normal"})
        for room in ['1200400','1211371']:
            d = self.sensor_data[room]
            if d.alarm_status:
                d.alarm_status = False; self.add_event(f"🔔 ALARM_{room} -> OFF")
            if d.sprinkler_status:
                d.sprinkler_status = False; self.add_event(f"💦 SPRINKLER_{room} -> OFF")
        for car_key in ['car1','car2']:
            old = self.sensor_data[car_key].car_status
            if old != "PARKED":
                self.sensor_data[car_key].car_status = "PARKED"; self.add_event(f"🚗 {car_key.upper()} {old} -> PARKED")

    def _scenario_test(self, params=None):
        self._send_control_all("SCENARIO", {"name":"test"})
        self.add_event("Test scenario started")
        for _ in range(5):
            if not self.running: break
            self.add_event(random.choice([
                "Sensor test passed","Network OK","Gateway heartbeat stable",
                "Parameters normal","Automatic test sequence running"
            ]))
            time.sleep(1)
        self.add_event("Test scenario completed")

    def stop(self):
        if not self.running: return
        logger.info("Stopping server...")
        self.running = False
        with self.lock:
            for addr, (s, _, _) in list(self.clients.items()):
                try: s.close()
                except: pass
        if self.server_socket:
            try: self.server_socket.close()
            except: pass
        logger.info("Server stopped.")

    def _signal_handler(self, *_):
        logger.info("Signal received, shutting down...")
        self.stop()

if __name__ == "__main__":
    CloudServer().start()
